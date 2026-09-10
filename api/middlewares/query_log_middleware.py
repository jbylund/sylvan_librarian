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
from api.utils.response_telemetry import result_count_of, search_execute_ms, search_fetch_ms

if TYPE_CHECKING:
    import falcon

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO magic.query_log
    (q, cache_hit, execute_ms, fetch_ms, total_ms, result_count, total_cards, had_error, orderby, unique_by)
VALUES (%(q)s, %(cache_hit)s, %(execute_ms)s, %(fetch_ms)s, %(total_ms)s,
        %(result_count)s, %(total_cards)s, %(had_error)s, %(orderby)s, %(unique_by)s)
"""


class _QueryLogWriter:
    """Background writer: owns the DB connection, batching, and commit logic."""

    _FLUSH_EVERY_N = 50
    _FLUSH_EVERY_S = 2.0

    def __init__(self, q: queue.SimpleQueue[dict], stop: threading.Event) -> None:
        self._q = q
        self._stop = stop
        self._conn: psycopg.Connection | None = None
        self._pending: list[dict] = []
        self._last_commit = time.monotonic()

    def _connect(self) -> psycopg.Connection | None:
        creds = get_pg_creds()
        if not creds:
            return None
        return psycopg.connect(**creds)

    def _reset(self) -> None:
        with contextlib.suppress(Exception):
            if self._conn is not None:
                self._conn.close()
        self._conn = None
        self._pending.clear()
        self._last_commit = time.monotonic()

    def _flush(self) -> None:
        if not self._pending:
            return
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        if self._conn is None:
            logger.warning("QueryLogMiddleware: no PG* env vars set — dropping %d log entries", len(self._pending))
            self._pending.clear()
            return
        with self._conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, self._pending)
        self._conn.commit()
        self._pending.clear()
        self._last_commit = time.monotonic()

    def _due_for_flush(self) -> bool:
        return len(self._pending) >= self._FLUSH_EVERY_N or (time.monotonic() - self._last_commit) >= self._FLUSH_EVERY_S

    def _write(self, entry: dict) -> None:
        self._pending.append(entry)
        if self._due_for_flush():
            self._flush()

    def run(self) -> None:
        try:
            self._drain_loop()
        finally:
            with contextlib.suppress(Exception):
                self._flush()
                if self._conn is not None:
                    self._conn.close()

    def _drain_loop(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                entry = self._q.get(timeout=0.05)
            except queue.Empty:
                with contextlib.suppress(Exception):
                    self._flush()
                continue
            try:
                self._write(entry)
            except Exception:
                logger.exception("QueryLogMiddleware: failed to write log entry")
                self._reset()
                time.sleep(1)  # brief back-off before reconnect


class QueryLogMiddleware:
    """Middleware that records /search performance to magic.query_log.

    Uses a background writer thread so the log insert never blocks request processing.
    One row is recorded per request; cache hits are included (with cache_hit=True and
    NULL DB timings) so query frequency can be analysed alongside raw latency.
    """

    _MAX_PENDING = 10_000

    def __init__(self) -> None:
        """Start the background writer thread."""
        # SimpleQueue over Queue: no maxsize support, but also no Condition/Lock wait
        # machinery -- ~28x cheaper put() in a producer/consumer microbenchmark of this
        # exact pattern. Bounding is reimplemented below via _pending_upper_bound instead.
        self._queue: queue.SimpleQueue[dict] = queue.SimpleQueue()
        # A provable upper bound on the queue's true length: it counts puts since the last
        # resync and never accounts for the writer thread's concurrent gets, so it can only
        # over-count, never under-count. That makes "is there definitely room" a plain
        # compare-and-increment in the common case -- qsize() (the only call that needs to
        # touch the queue's internal lock) is paid for only once the bound reaches the cap,
        # at which point it resyncs to the real (possibly much smaller) size.
        self._pending_upper_bound = 0
        self._stop = threading.Event()
        self._writer = threading.Thread(
            target=_QueryLogWriter(self._queue, self._stop).run,
            daemon=True,
            name="query-log-writer",
        )
        self._writer.start()

    def _enqueue(self, entry: dict) -> None:
        """Push a log entry, dropping it (with a warning) once the queue is backed up."""
        if self._pending_upper_bound >= self._MAX_PENDING:
            self._pending_upper_bound = self._queue.qsize()
            if self._pending_upper_bound >= self._MAX_PENDING:
                logger.warning("QueryLogMiddleware: log queue full, dropping entry for %s", entry.get("q"))
                return
        self._queue.put_nowait(entry)
        self._pending_upper_bound += 1

    def stop(self) -> None:
        """Signal the background writer to exit and wait for it to finish."""
        self._stop.set()
        self._writer.join(timeout=3)
        if self._writer.is_alive():
            logger.warning("QueryLogMiddleware: writer thread did not exit within 3 seconds")

    def process_response(
        self,
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

        cache_hit = bool(req.context.get("cache_hit"))
        if cache_hit:
            # The cached response is rendered bytes; counts were captured at cache-store time.
            execute_ms = fetch_ms = None
            result_count = req.context.get("cached_result_count")
            total_cards = req.context.get("cached_total_cards")
        else:
            media = resp.media
            if not isinstance(media, dict):
                logger.warning("QueryLogMiddleware: unexpected response media type %s for %s", type(media), req.path)
                return
            inner_timings = media.get("inner_timings")
            execute_ms = search_execute_ms(inner_timings)
            fetch_ms = search_fetch_ms(inner_timings)
            result_count = result_count_of(resp, media)
            total_cards = media.get("total_cards")

        start = req.context.get("_start_time")
        total_ms = (time.monotonic() - start) * 1000 if start is not None else None

        entry: dict = {
            "q": req.params.get("q"),
            "cache_hit": cache_hit,
            "execute_ms": execute_ms,
            "fetch_ms": fetch_ms,
            "total_ms": total_ms,
            "result_count": result_count,
            "total_cards": total_cards,
            "had_error": not req_succeeded or (resp.status or "").startswith(("4", "5")),
            "orderby": req.params.get("orderby"),
            "unique_by": req.params.get("unique"),
        }
        self._enqueue(entry)
