"""Middleware for logging /search request performance to magic.query_log."""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING

import psycopg

from api.utils.db_utils import get_pg_creds

if TYPE_CHECKING:
    import falcon

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO magic.query_log
    (q, cache_hit, execute_ms, fetch_ms, total_ms, result_count, total_cards, had_error, orderby, unique_by)
VALUES (%(q)s, %(cache_hit)s, %(execute_ms)s, %(fetch_ms)s, %(total_ms)s,
        %(result_count)s, %(total_cards)s, %(had_error)s, %(orderby)s, %(unique_by)s)
"""


class QueryLogMiddleware:
    """Middleware that records /search performance to magic.query_log.

    Uses a background writer thread so the log insert never blocks request processing.
    One row is recorded per request; cache hits are included (with cache_hit=True and
    NULL DB timings) so query frequency can be analysed alongside raw latency.
    """

    def __init__(self: QueryLogMiddleware) -> None:
        """Start the background writer thread."""
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._writer = threading.Thread(target=self._drain, daemon=True, name="query-log-writer")
        self._writer.start()

    def stop(self: QueryLogMiddleware) -> None:
        """Signal the background writer to exit and wait for it to finish."""
        self._stop.set()
        self._writer.join()

    # ------------------------------------------------------------------
    # Background writer
    # ------------------------------------------------------------------

    _DROP_LOG_INTERVAL = 100

    def _connect(self: QueryLogMiddleware) -> psycopg.Connection | None:
        creds = get_pg_creds()
        if not creds:
            return None
        conninfo = " ".join(f"{k}={v}" for k, v in creds.items())
        return psycopg.connect(conninfo)

    def _drain(self: QueryLogMiddleware) -> None:
        """Drain the queue and write entries to magic.query_log."""
        conn: psycopg.Connection | None = None
        dropped = 0
        while not self._stop.is_set():
            try:
                entry = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if conn is None or conn.closed:
                    conn = self._connect()
                if conn is None:
                    dropped += 1
                    if dropped % self._DROP_LOG_INTERVAL == 1:
                        logger.warning("QueryLogMiddleware: no PG* env vars set — %d log entries dropped so far", dropped)
                    continue
                with conn.cursor() as cur:
                    cur.execute(_INSERT_SQL, entry)
                conn.commit()
            except Exception:
                logger.exception("QueryLogMiddleware: failed to write log entry")
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
                    conn = None
                time.sleep(1)  # brief back-off before reconnect

    # ------------------------------------------------------------------
    # Falcon middleware hook
    # ------------------------------------------------------------------

    def process_response(
        self: QueryLogMiddleware,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        """Enqueue a log entry for every /search request.

        Args:
            req: The incoming request.
            resp: The completed response.
            resource: The resource handler (unused).
            req_succeeded: Whether the request completed without an unhandled exception.
        """
        del resource
        if req.path.strip("/") != "search":
            return

        media = resp.media
        if not isinstance(media, dict):
            logger.warning("QueryLogMiddleware: unexpected response media type %s for %s", type(media), req.path)
            return

        start = req.context.get("_start_time")
        total_ms = (time.monotonic() - start) * 1000 if start is not None else None

        cache_hit = bool(media.get("cache_hit", False))
        children = (media.get("inner_timings") or {}).get("_children") or {}

        entry: dict = {
            "q": req.params.get("q"),
            "cache_hit": cache_hit,
            "execute_ms": children.get("execute_query", {}).get("_meta", {}).get("duration_ms") if not cache_hit else None,
            "fetch_ms": children.get("fetch_results", {}).get("_meta", {}).get("duration_ms") if not cache_hit else None,
            "total_ms": total_ms,
            "result_count": len(media.get("cards") or []),
            "total_cards": media.get("total_cards"),
            "had_error": not req_succeeded or (resp.status or "")[:1] in ("4", "5"),
            "orderby": req.params.get("orderby"),
            "unique_by": req.params.get("unique"),
        }
        try:
            self._queue.put(entry, timeout=0.001)
        except queue.Full:
            logger.warning("QueryLogMiddleware: log queue full, dropping entry for %s", req.params.get("q"))
