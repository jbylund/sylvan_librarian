"""Middleware for logging /search request performance to magic.query_log."""

from __future__ import annotations

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
        self._queue: queue.SimpleQueue[dict | None] = queue.SimpleQueue()
        self._writer = threading.Thread(target=self._drain, daemon=True, name="query-log-writer")
        self._writer.start()

    # ------------------------------------------------------------------
    # Background writer
    # ------------------------------------------------------------------

    def _connect(self: QueryLogMiddleware) -> psycopg.Connection:
        creds = get_pg_creds()
        conninfo = " ".join(f"{k}={v}" for k, v in creds.items())
        return psycopg.connect(conninfo)

    def _drain(self: QueryLogMiddleware) -> None:
        """Drain the queue and write entries to magic.query_log."""
        conn: psycopg.Connection | None = None
        while True:
            try:
                entry = self._queue.get()
                if entry is None:  # shutdown signal
                    break
                if conn is None or conn.closed:
                    conn = self._connect()
                with conn.cursor() as cur:
                    cur.execute(_INSERT_SQL, entry)
                conn.commit()
            except Exception:
                logger.exception("QueryLogMiddleware: failed to write log entry")
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
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
        if req.path != "/search":
            return

        media = resp.media
        if not isinstance(media, dict):
            return

        start = req.context.get("_start_time")
        total_ms = (time.monotonic() - start) * 1000 if start is not None else None

        cache_hit = bool(media.get("cache_hit", False))
        inner = media.get("inner_timings") or {}

        entry: dict = {
            "q": req.params.get("q"),
            "cache_hit": cache_hit,
            "execute_ms": inner.get("execute_query") if not cache_hit else None,
            "fetch_ms": inner.get("fetch_results") if not cache_hit else None,
            "total_ms": total_ms,
            "result_count": len(media.get("cards") or []),
            "total_cards": media.get("total_cards"),
            "had_error": not req_succeeded,
            "orderby": req.params.get("orderby"),
            "unique_by": req.params.get("unique"),
        }
        self._queue.put(entry)
