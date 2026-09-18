"""The state and resources shared between peer resources (APIResource, AdminResource, ...).

None of this is owned by any one resource: `reader_pool` and `engine` are read by every
search-shaped resource, `writer_pool` is used by every resource that imports or backfills data, and
`cache_generation`/`last_import_time` are the cross-worker-process signal that ties writers to
readers ("the corpus changed," "check whether setup finished"). A resource that needs one of these
takes an `AppContext` rather than reaching into a sibling resource for it.

`AppContext` itself is a per-process object: `reader_pool`/`writer_pool`/`engine` are built fresh
inside whichever worker process constructs it (sockets and the Rust engine's in-memory store can't
cross a fork the way a `multiprocessing.Value` can), while `cache_generation`/`last_import_time`/
`engine_reload_guard` are expected to be created once in the master process, before forking, and
handed to every worker's `AppContext` unchanged.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pathlib
import time
from typing import TYPE_CHECKING

import psycopg_pool

from api.parsing.set_dates import SET_RELEASE_DATES_SQL, replace_set_release_dates
from api.settings import settings
from api.utils import db_utils
from card_engine import ENGINE_COLUMNS as _ENGINE_COLUMNS
from card_engine import QueryEngine as _QueryEngine

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Lock as LockType

    from card_engine import QueryEngine

logger = logging.getLogger(__name__)

# A corpus below this size means the import never finished (or hasn't started), not that it's
# legitimately small -- see `AppContext.setup_complete`.
MIN_IMPORT_CARDS = 90_000

# How long a `setup_complete` result is trusted before re-checking the database, once the shared
# last_import_time hasn't changed in the meantime -- see `AppContext.setup_complete`.
_SETUP_COMPLETE_TTL = 60 * 60  # 1 hour

# Rows per batch streamed into the engine during a reload. The reload's memory floor is the
# Rust-side build (~305 MB), so the batch only needs to be small relative to that: ~2k rows ≈ 18 MB
# of dicts. Smaller adds round trips for no measurable gain (see
# docs/issues/00505-engine-incremental-loading.md).
_ENGINE_RELOAD_BATCH_SIZE = 2_000


def _rss_mb() -> str:
    """Return current RSS in MB as a string, or 'unknown' if /proc is unavailable."""
    try:
        with pathlib.Path("/proc/self/status").open() as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return f"{int(line.split()[1]) // 1024} MB"
    except OSError:
        pass
    return "unknown"


class AppContext:
    """Cross-cutting state and resources shared by every resource mounted on the app."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        reader_pool: psycopg_pool.ConnectionPool | None = None,
        writer_pool: psycopg_pool.ConnectionPool | None = None,
        engine: QueryEngine | None = None,
        engine_reload_guard: LockType | None = None,
        cache_generation: Synchronized | None = None,
        last_import_time: Synchronized | None = None,
    ) -> None:
        """Build the per-worker resources, or accept ones a caller already built.

        Args:
            reader_pool: Connection pool for search-shaped resources. Built via
                `db_utils.make_pool()` if not given, matching every other handle here -- a caller
                (a test, mainly) can inject one without a real pool ever opening a connection.
            writer_pool: Connection pool for resources that import or backfill data. Built the same
                way if not given; kept separate from `reader_pool` so a slow bulk write can't
                starve a live search request for a connection.
            engine: The in-process query engine both search-shaped resources read from.
            engine_reload_guard: Cross-process lock serialising engine reloads, so only one worker
                pays the reload cost at a time.
            cache_generation: Shared counter bumped to invalidate every worker's query-result cache.
            last_import_time: Shared timestamp of the last completed import.
        """
        self.reader_pool: psycopg_pool.ConnectionPool = reader_pool or db_utils.make_pool()
        self.writer_pool: psycopg_pool.ConnectionPool = writer_pool or db_utils.make_pool()
        self.engine: QueryEngine = engine if engine is not None else _QueryEngine()
        self.engine_reload_guard: LockType = engine_reload_guard or multiprocessing.Lock()
        self.cache_generation: Synchronized = cache_generation or multiprocessing.Value("i", 0)
        self.last_import_time: Synchronized = last_import_time or multiprocessing.Value("d", 0.0, lock=True)
        # Per-process only -- a local TTL cache keyed off the shared last_import_time, not itself a
        # cross-worker signal (a fresh worker starts with no cached opinion and checks for real).
        self._setup_complete_cache: tuple[bool, float, float] | None = None
        # Same shape and same reasoning for the set-code -> release-date registry the parser reads
        # (`date>=hob`): (expires_at, last_import_time it was loaded under). A fresh worker starts
        # empty and loads on its first search; an import bumps last_import_time and the next search
        # reloads, so a set imported since resolves without a restart.
        self._set_release_dates_cache: tuple[float, float] | None = None

    def setup_complete(self) -> bool:
        """Return True if the setup is complete."""
        now = time.monotonic()
        current_import_time = self.last_import_time.get_obj().value
        if self._setup_complete_cache is not None:
            result, expires_at, cached_import_time = self._setup_complete_cache
            if now < expires_at and current_import_time == cached_import_time:
                logger.debug(
                    "setup_complete cache hit: result=%s, expires in %.0fs, pid %d",
                    result,
                    expires_at - now,
                    os.getpid(),
                )
                return result
        try:
            with self.reader_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(1) AS num_cards FROM magic.cards")
                cards_found = cursor.fetchall()[0]["num_cards"]
                result = cards_found > MIN_IMPORT_CARDS
                if result:
                    logger.info("Found %d cards in pid %d", cards_found, os.getpid())
                else:
                    logger.warning(
                        "Setup not complete: found %d cards, need more than %d (pid %d)",
                        cards_found,
                        MIN_IMPORT_CARDS,
                        os.getpid(),
                    )
        except Exception as oops:
            logger.error(
                "Error checking if setup is complete (pid %d): %s: %s",
                os.getpid(),
                type(oops).__name__,
                oops,
                exc_info=True,
            )
            result = False
        self._setup_complete_cache = (result, now + _SETUP_COMPLETE_TTL, current_import_time)
        return result

    def invalidate_setup_complete(self) -> None:
        """Force the next `setup_complete()` call to re-check the database.

        Called after a write that could change the answer (an import completing), rather than
        waiting for the TTL to expire.
        """
        self._setup_complete_cache = None

    def refresh_set_release_dates(self) -> None:
        """Load every imported set's release date into the parser's registry (`api.parsing.set_dates`).

        One GROUP BY over magic.cards on `reader_pool`. Raises on a database error -- callers that
        must not fail on one (a search) go through `ensure_set_release_dates`; the import path calls
        this directly, where a failure surfaces like any other failed post-import step.
        """
        now = time.monotonic()
        current_import_time = self.last_import_time.get_obj().value
        with self.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(SET_RELEASE_DATES_SQL)
            rows = cursor.fetchall()
        replace_set_release_dates({row["code"]: row["released_at"] for row in rows})
        self._set_release_dates_cache = (now + _SETUP_COMPLETE_TTL, current_import_time)
        logger.info("Loaded release dates for %d sets (pid %d)", len(rows), os.getpid())

    def ensure_set_release_dates(self) -> None:
        """Make sure this process's set-date registry reflects the last import; never raises.

        Cached per process off the shared `last_import_time` with the same TTL as `setup_complete`,
        so the table is read once per worker per import rather than once per search. A database
        error is logged and swallowed: a search must not fail because this lookup table could not be
        refreshed, and the registry keeps whatever it last loaded (possibly nothing, in which case a
        set code in `date:` is reported as unknown -- the honest answer for what this process knows).
        Nothing is cached on failure, so the next search tries again.
        """
        now = time.monotonic()
        current_import_time = self.last_import_time.get_obj().value
        if self._set_release_dates_cache is not None:
            expires_at, cached_import_time = self._set_release_dates_cache
            if now < expires_at and current_import_time == cached_import_time:
                return
        try:
            self.refresh_set_release_dates()
        except Exception as oops:
            logger.error(
                "Error loading set release dates (pid %d): %s: %s",
                os.getpid(),
                type(oops).__name__,
                oops,
                exc_info=True,
            )

    def bump_cache_generation(self) -> None:
        """Invalidate every worker's query-result cache. Call after any write to magic.cards."""
        with self.cache_generation.get_lock():
            self.cache_generation.value += 1

    def reload_engine(self, *, force: bool = False) -> None:
        """Stream all cards from the DB into the Rust engine's card store in batches.

        A server-side cursor feeds the engine's staged reload API
        (reload_begin / add_batch / reload_commit) one batch at a time, so the
        Python-side transient is one batch of row dicts (~18 MB at 2k rows)
        instead of the whole corpus (~840 MB) — measurements in
        docs/issues/00505-engine-incremental-loading.md. The reload is guarded by a
        cross-worker lock so only one worker pays the build cost at a time.
        With force=False (cold-start warming), losers of the race return
        immediately and pick up the winner's archive via the engine's
        inode-based remap. With force=True (data just changed), callers wait
        their turn but skip the rebuild if another worker refreshed the store
        while they were waiting.

        Reads via `writer_pool`, not `reader_pool`: this is a single giant SELECT (the whole
        corpus) triggered by a write completing, and the cross-process guard means only one worker
        ever runs it at a time. Measured 2026-08-21 against a ~98k-card corpus: ~3 seconds
        end-to-end, not the "minutes" an earlier version of this docstring assumed -- so this isn't
        a high-stakes call. It's also lower-stakes than a naive pool split suggests either way: the
        in-process engine serves the large majority of search traffic directly, with no pool
        involved at all, so reader_pool contention during those ~3 seconds would only ever touch the
        minority of requests that fall back to SQL. Kept on `writer_pool` anyway on the same
        reasoning as the pool split itself (a write-triggered bulk operation belongs with other
        writes, not with reads), just without claiming a bigger effect than the numbers support.

        Args:
            force: If False, skip entirely when another worker holds the lock or the
                store is already populated. If True, wait for the lock and always
                reload (the data just changed, so the archive must be rebuilt).
        """
        if not settings.enable_engine:
            logger.debug("Engine reload skipped: feature-gated off (ENABLE_ENGINE)")
            return
        if self.engine is None:
            return
        logger.info("Engine reload requested (force=%s, pid=%d, rss=%s)", force, os.getpid(), _rss_mb())
        if not self.engine_reload_guard.acquire(block=force):
            logger.info("Engine reload already in progress in another worker, skipping (pid=%d)", os.getpid())
            return
        try:
            if not force and self.engine.size() > 0:
                # Another worker populated the store while we raced for the lock.
                return
            logger.info("Engine reload starting (force=%s, pid=%d, rss=%s)", force, os.getpid(), _rss_mb())
            cols_sql = ", ".join(f"card.{col}" for col in _ENGINE_COLUMNS)
            try:
                with self.writer_pool.connection() as conn:
                    # Named cursor => server-side: psycopg buffers one batch, not the full result.
                    with conn.cursor(name="engine_reload") as cursor:
                        cursor.itersize = _ENGINE_RELOAD_BATCH_SIZE
                        cursor.execute(f"SELECT {cols_sql} FROM magic.cards AS card")
                        if not self.engine.reload_begin():
                            # Another process published a fresh archive while we
                            # waited for the engine's write lock; it was remapped.
                            return
                        try:
                            while batch := cursor.fetchmany(_ENGINE_RELOAD_BATCH_SIZE):
                                self.engine.add_batch(batch)
                            self.engine.reload_commit()
                        except BaseException:
                            self.engine.reload_abort()
                            raise
            except psycopg_pool.PoolClosed:
                logger.debug("Connection pool closed during engine reload, skipping (pid=%d)", os.getpid())
                return
            logger.info("Engine reloaded with %d cards (pid=%d, rss=%s)", self.engine.size(), os.getpid(), _rss_mb())
        finally:
            self.engine_reload_guard.release()
