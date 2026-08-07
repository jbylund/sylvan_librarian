"""Implementation of the routes of our simple api."""

from __future__ import annotations

import copy
import inspect
import logging
import multiprocessing
import os
import pathlib
import threading
import time

# Imported at runtime, not under TYPE_CHECKING, because route handlers annotate parameters with it and
# ParamBinder resolves those annotations to real types at registration. Under TYPE_CHECKING the name is
# absent at runtime and resolution fails, which is a startup error by design rather than a silent loss
# of coercion. Ruff's TC003 wants it moved; the runtime-evaluated-decorators setting will make the noqa
# unnecessary once handlers carry a route decorator.
from collections.abc import Sequence  # noqa: TC003
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from typing import cast as typecast

import falcon
import orjson
import psycopg
import psycopg_pool
from cachebox import LRUCache, TTLCache
from psycopg import Connection, Cursor

from api.admin_resource import ADMIN_MOUNT_PREFIX, AdminResource
from api.enums import CardOrdering, PreferOrder, ResponseShape, SortDirection, UniqueOn
from api.middlewares.timing import record_span
from api.noscript_helpers import generate_results_count_html, generate_results_html
from api.parsing import generate_sql_query, parse_scryfall_query
from api.settings import settings
from api.utils import db_utils, error_monitoring, multiprocessing_utils
from api.utils.caching import cached
from api.utils.css_utils import build_critical_css
from api.utils.generation_cache import GenerationCache
from api.utils.page_rendering import SITE_NAME_PLACEHOLDER, STATIC_DIR, build_base_html, build_card_html
from api.utils.param_binding import ParamCoercionError
from api.utils.routing import build_route_table, build_routes_listing, route
from api.utils.site_name import hostname_to_site_name
from api.utils.timer import Timer
from card_engine import ENGINE_COLUMNS as _ENGINE_COLUMNS_FROM_MODULE
from card_engine import QueryEngine as _QueryEngine
from card_engine import QueryError as _QueryError

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from multiprocessing.synchronize import RLock as LockType

    from api.parsing.nodes import Query
    from api.utils.routing import BoundRoute


logger = logging.getLogger(__name__)


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


# Query parameters that must not be forwarded to action handlers.
DISALLOWED_QUERY_ARGS: frozenset[str] = frozenset(["falcon_response", "request_host"])

# Body for an unhandled exception. Fixed and content-free on purpose: the frames live at throw sites
# inside query and import paths, so their locals can hold connection and query state. Diagnostics go
# to the log and the error monitor, which are not attacker-readable; the client gets this and nothing
# more. Callers must not append exception detail to it.
INTERNAL_ERROR_DESCRIPTION = "An internal error occurred."

MIN_IMPORT_CARDS = 90_000
# Rows per batch streamed into the engine during a reload. The reload's memory
# floor is the Rust-side build (~305 MB), so the batch only needs to be small
# relative to that: ~2k rows ≈ 18 MB of dicts. Smaller adds round trips for no
# measurable gain (see docs/issues/00505-engine-incremental-loading.md).
_ENGINE_RELOAD_BATCH_SIZE = 2_000

# Public field name -> magic.cards column. The `fields=` vocabulary for /search. This is
# deliberately a subset of FIELD_TABLE in card_engine/src/lib.rs, not a mirror of it — not
# everything the engine can extract needs to be a public API field. Every key here must still
# have a same-named entry in FIELD_TABLE with matching semantics, so a `fields=` request for one
# of these names gets identically-shaped results regardless of which path serves it; FIELD_TABLE
# is free to have entries with no counterpart here.
RESULT_FIELD_COLUMNS: dict[str, str] = {
    "name": "card_name",
    "set_code": "card_set_code",
    "collector_number": "collector_number",
    "power": "creature_power_text",
    "toughness": "creature_toughness_text",
    "mana_cost": "mana_cost_text",
    "oracle_text": "oracle_text",
    "set_name": "set_name",
    "type_line": "type_line",
    "illustration_id": "illustration_id",
    "scryfall_id": "scryfall_id",
    "price_usd": "price_usd",
    "prefer_score": "prefer_score",
}
# `fields=None` resolves to these 9 — the fixed set every caller got before field selection
# existed. Order/membership must match DEFAULT_FIELDS in card_engine/src/lib.rs.
DEFAULT_RESULT_FIELDS: tuple[str, ...] = (
    "name",
    "set_code",
    "collector_number",
    "power",
    "toughness",
    "mana_cost",
    "oracle_text",
    "set_name",
    "type_line",
)

# default/atypical are complementary and disjoint
# so in theory we could query for one and build the other by
# querying and inverting


def set_cache_header(falcon_response: falcon.Response | None, duration: timedelta) -> None:
    """Set the Cache-Control header on a Falcon response.

    Args:
        falcon_response: The Falcon response object.
        duration: The duration of the cache in seconds.
    """
    if falcon_response is None:
        return
    seconds = int(duration.total_seconds())
    falcon_response.set_header("Cache-Control", f"public, max-age={seconds}")


def set_no_store_header(falcon_response: falcon.Response | None) -> None:
    """Set Cache-Control: no-store on a Falcon response to prevent CDN and browser caching."""
    if falcon_response is None:
        return
    falcon_response.set_header("Cache-Control", "no-store")


@cached(cache=LRUCache(maxsize=10_000))
def get_where_clause(query: str) -> tuple[str, dict]:
    """Generate SQL WHERE clause and parameters from a search query.

    Args:
        query: The search query string to parse.

    Returns:
        Tuple of (SQL WHERE clause, parameter dictionary).
    """
    parsed_query = parse_scryfall_query(query)
    return generate_sql_query(parsed_query)


def rewrap(query: str) -> str:
    """Normalize whitespace in a SQL query string.

    Args:
        query: The SQL query string to normalize.

    Returns:
        The query with normalized whitespace.
    """
    return " ".join(query.strip().split())


def _columnarize_cards(cards: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Convert a list of card dicts into a dict of per-field value lists.

    Every card in a result set carries the same keys (absent values are explicit
    nulls), so the transform is a pure inversion: the client rebuilds row i by
    taking element i from each field's list. Shipping one set of keys instead of
    one per card cuts the serialized payload roughly 30% raw / 9% compressed.

    Args:
        cards: Card dicts sharing a common key set.

    Returns:
        Dict mapping each field name to that field's values in card order.
    """
    keys = list(cards[0]) if cards else []
    return {k: [c[k] for c in cards] for k in keys}


class APIResource:
    """Class implementing request handling for our simple API."""

    def __init__(
        self,
        *,
        import_guard: LockType = multiprocessing_utils.DEFAULT_LOCK,
        last_import_time: Synchronized | None = None,
        schema_setup_event: EventType = multiprocessing_utils.DEFAULT_EVENT,
        cache_generation: Synchronized | None = None,
        engine_reload_guard: LockType | None = None,
    ) -> None:
        """Initialize an APIResource object, set up connection pool and action map.

        Sets up the database connection pool and action mapping for the API.
        """
        self._critical_css: str = build_critical_css(STATIC_DIR / "styles.css")
        self._conn_pool: psycopg_pool.ConnectionPool = db_utils.make_pool()
        # Build the route table from methods marked with @route, scanning the class rather than this
        # instance so nothing assigned below can become a route. Each entry carries everything
        # dispatch needs — the wrapped handler, how many positional path segments it absorbs, and
        # what it declared — computed once here rather than per-request in _handle.
        self.routes = build_route_table(self)

        self._cache_generation: Synchronized = cache_generation or multiprocessing.Value("i", 0)
        self._query_cache: GenerationCache = GenerationCache(
            factory=lambda: LRUCache(maxsize=1_000 if settings.enable_cache else 1),
            generation=self._cache_generation,
        )
        self._search_gen_cache: LRUCache = LRUCache(maxsize=1)  # generation → TTLCache
        self._last_import_time: Synchronized = last_import_time or multiprocessing.Value("d", 0.0, lock=True)
        self._engine = _QueryEngine()
        self._engine_reload_lock = threading.Lock()
        # Cross-worker guard: the full-table fetch in _reload_engine is memory-hungry,
        # so only one worker process should run it at a time (see _reload_engine).
        self._engine_reload_guard: LockType = engine_reload_guard or multiprocessing.Lock()
        logger.info("Worker with pid %d has conn pool %s", os.getpid(), self._conn_pool)

        # Mounted after the parent's own state exists, since the child reaches back for the handles
        # they share. advertise=False is set here rather than on each handler: forgetting it is then
        # a property of this one call, not a hole in one route.
        self.admin = AdminResource(self, import_guard=import_guard, schema_setup_event=schema_setup_event)
        self.routes.update(build_route_table(self.admin, prefix=ADMIN_MOUNT_PREFIX, advertise=False))
        self._not_found_routes = build_routes_listing(self.routes)

        self.admin.setup_schema()
        self.admin.import_data()  # ensures that database is setup

    def _get_timer(self, req: falcon.Request) -> Timer:
        """Get the timer for the request."""
        return req.context.setdefault("timer", Timer())

    def _set_statement_timeout(self, cursor: Cursor, statement_timeout: int) -> None:
        """Validate and set the statement timeout for a database cursor.

        PostgreSQL SET commands don't support parameterized values, so we must
        validate the value before using it in string interpolation.

        Args:
            cursor: Database cursor to execute the SET command on
            statement_timeout: The statement timeout value in milliseconds

        Raises:
            ValueError: If statement_timeout is not a non-negative integer
        """
        if not isinstance(statement_timeout, int) or statement_timeout < 0:
            msg = f"statement_timeout must be a non-negative integer, got: {statement_timeout}"
            raise ValueError(msg)
        cursor.execute(f"set statement_timeout = {statement_timeout}")

    def _resolve_action(self, path: str) -> tuple[BoundRoute | None, list[str]]:
        """Map a request path to the route that answers it.

        Args:
            path: Request path, already stripped of surrounding slashes and with dots replaced by
                underscores.

        Returns:
            The matching route and the positional path segments to pass it, or (None, []) when the
            path identifies nothing.
        """
        if path in self.routes:
            # Flat routes like "static/favicon.ico" register their full slash-containing path as
            # the route key — check that exact match before treating "/" as an arg separator.
            return self.routes[path], []

        action_word, *action_args = path.split("/")
        entry = self.routes.get(action_word)
        # A matched route that can't absorb this many trailing segments (e.g. /robots.txt/x)
        # means the path doesn't identify anything — 404, not a 400 from a TypeError inside it.
        if entry is None or len(action_args) > entry.positional_capacity:
            return None, []
        return entry, action_args

    def _handle(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Handle a Falcon request and set the response.

        Args:
        ----
            req (falcon.Request): The incoming request.
            resp (falcon.Response): The outgoing response.

        """
        if resp.complete:
            logger.info("Request already handled: %s", req.relative_uri)
            return

        path = req.path.strip("/") or "_root"

        logger.info(
            "Handling request for %s / |%s| / response id: %d",
            req.relative_uri,
            path,
            id(resp),
        )

        entry, action_args = self._resolve_action(path)
        action = self._raise_not_found
        if entry is not None:
            # A route answers only the methods it declares. Checked after the path resolves, so a
            # path that identifies nothing stays a 404 rather than reporting what it would accept.
            if req.method not in entry.spec.methods:
                raise falcon.HTTPMethodNotAllowed(allowed_methods=sorted(entry.spec.methods))
            action = entry.action

        res = None
        before = time.monotonic()
        try:
            params = {k: v for k, v in req.params.items() if k not in DISALLOWED_QUERY_ARGS}
            res = action(*action_args, falcon_response=resp, request_host=req.get_header("X-Proxy-Host") or req.host, **params)
            resp.media = res
        except ParamCoercionError as oops:
            # A value the client sent is not valid for the parameter it names. The message contains only
            # the parameter name, the value the client already supplied, and — for enums — the accepted
            # values, so it guides a fix without describing anything internal.
            logger.info("Rejected %s: %s", path, oops)
            raise falcon.HTTPBadRequest(title="Invalid Parameter", description=str(oops)) from oops
        except TypeError as oops:
            logger.error("Error handling request: %s", oops, exc_info=True)
            raise falcon.HTTPBadRequest(description=str(oops)) from oops
        except falcon.HTTPError as oops:
            logger.error("Error handling request for %s: %s", path, oops, exc_info=True)
            raise
        except falcon.HTTPStatus:
            # Not an error, so deliberately not folded into the HTTPError branch above and its
            # error-level traceback. HTTPStatus is Falcon's "return this status as-is" signal — how a
            # redirect and a 304 are expressed — and it is a sibling of HTTPError, not a subclass. It
            # only has to reach Falcon, which the generic handler below would otherwise prevent by
            # turning it into a 500.
            raise
        except Exception as oops:
            logger.error("Error handling request: %s", oops, exc_info=True)
            error_monitoring.error_handler(req, oops)
            # walk back to the lowest frame...
            # file / function / locals (if possible)
            stack_info = []
            for iframe in inspect.trace()[1:]:
                stack_info.append(
                    {
                        "file": iframe.filename,
                        "function": iframe.function,
                        "line_no": iframe.lineno,
                        "locals": {k: v for k, v in iframe.frame.f_locals.items() if error_monitoring.can_serialize(v)},
                    },
                )
            # Logged, never returned: exc_info above carries file/function/line, but not locals, and
            # a self-hoster with no HONEYBADGER_API_KEY has nowhere else to read them.
            logger.error("Stack detail for %s: %s", path, stack_info)

            raise falcon.HTTPInternalServerError(title="Server Error", description=INTERNAL_ERROR_DESCRIPTION) from oops
        finally:
            duration = (time.monotonic() - before) * 1000
            logger.info("Request duration: %.1f ms / %s", duration, resp.status)
            record_span(req, "handler", duration)
            if isinstance(res, dict):
                for span_name, span_data in res.get("outer_timings", {}).items():
                    record_span(req, span_name, span_data.get("_meta", {}).get("duration_ms", 0))

    def _raise_not_found(self, *_args: object, **_: object) -> None:
        """Raise a Falcon HTTPNotFound error with available routes."""
        raise falcon.HTTPNotFound(
            title="Not Found",
            description={
                "routes": self._not_found_routes,
            },
        )

    def _run_query(
        self,
        *,
        query: str,
        params: dict[str, Any] | None = None,
        explain: bool = True,
        statement_timeout: int = 10_000,
    ) -> dict[str, Any]:
        """Run a SQL query with optional parameters and explanation.

        Args:
        ----
            query (str): The SQL query to run.
            params (Optional[Dict[str, Any]]): Query parameters.
            explain (bool): Whether to run EXPLAIN on the query.
            statement_timeout (int): The statement timeout in milliseconds.

        Returns:
        -------
            Dict[str, Any]: Query result and metadata.

        """
        params = params or {}
        query = " ".join(query.strip().split())

        use_cache = True
        if use_cache:

            def maybe_json_dump(v: object) -> object:
                if isinstance(v, list | dict):
                    return orjson.dumps(v, option=orjson.OPT_SORT_KEYS).decode()
                return v

            # need to make params hashable... but it might contain dicts/lists/...
            hashable_params = {k: maybe_json_dump(v) for k, v in params.items()}
            cachekey = (
                query,
                frozenset(hashable_params.items()),
                explain,
            )
            cached_val = self._query_cache.get(cachekey)
            if cached_val is not None:
                return copy.deepcopy(cached_val)

        params = {k: db_utils.maybe_json(v) for k, v in params.items()}

        root_timing_key = "root_timing_key"
        timer = Timer()
        result: dict[str, Any] = {}
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            # Validate and set statement timeout
            self._set_statement_timeout(cursor, statement_timeout)
            if explain:
                explain_query = f"EXPLAIN (FORMAT JSON) {query}"
                cursor.execute(explain_query, params)
                for row in cursor.fetchall():
                    result["plan"] = row
            with timer(root_timing_key):
                with timer("execute_query"):
                    cursor.execute(query, params)
                with timer("fetch_results"):
                    result["result"] = [dict(r) for r in cursor.fetchall()]
            result["timings"] = timer.get_timings()[root_timing_key]

        if use_cache:
            self._query_cache[cachekey] = result

        return copy.deepcopy(result)

    @route()
    def get_pid(self, *, falcon_response: falcon.Response | None = None, **_: object) -> int:
        """Just return the pid of the process which served this request.

        Returns:
        -------
            int: The process ID.

        """
        set_no_store_header(falcon_response)
        return os.getpid()

    _SETUP_COMPLETE_TTL = 60 * 60  # 1 hour; also invalidated when _last_import_time changes
    _setup_complete_cache: tuple[bool, float, float] | None = None  # (result, expires_at, import_time)

    def _setup_complete(self) -> bool:
        """Return True if the setup is complete."""
        now = time.monotonic()
        current_import_time = self._last_import_time.get_obj().value
        if self._setup_complete_cache is not None:
            result, expires_at, cached_import_time = self._setup_complete_cache
            if now < expires_at and current_import_time == cached_import_time:
                logger.debug(
                    "_setup_complete cache hit: result=%s, expires in %.0fs, pid %d",
                    result,
                    expires_at - now,
                    os.getpid(),
                )
                return result
        try:
            with self._conn_pool.connection() as conn:
                conn = typecast("Connection", conn)
                with conn.cursor() as cursor:
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
        self._setup_complete_cache = (result, now + self._SETUP_COMPLETE_TTL, current_import_time)
        return result

    def _require_setup_complete(self) -> None:
        """Require that setup is complete or raise a ServiceUnavailable error."""
        if not self._setup_complete():
            logger.warning("Rejecting request in pid %d: setup is not complete", os.getpid())
            raise falcon.HTTPServiceUnavailable(
                title="Service Unavailable",
                description="Setup is not complete, please try again later.",
            ) from None

    def _trigger_background_reload_if_needed(self) -> None:
        if self._engine.size() == 0 and self._engine_reload_lock.acquire(blocking=False):

            def _bg_reload() -> None:
                try:
                    self._reload_engine()
                except Exception as e:
                    logger.error("Background engine reload failed: %s", e, exc_info=True)
                finally:
                    self._engine_reload_lock.release()

            threading.Thread(target=_bg_reload, daemon=True).start()

    def _reload_engine(self, *, force: bool = False) -> None:
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

        Args:
            force: If False, skip entirely when another worker holds the lock or the
                store is already populated. If True, wait for the lock and always
                reload (the data just changed, so the archive must be rebuilt).
        """
        if not settings.enable_engine:
            logger.debug("Engine reload skipped: feature-gated off (ENABLE_ENGINE)")
            return
        if self._engine is None:
            return
        logger.info("Engine reload requested (force=%s, pid=%d, rss=%s)", force, os.getpid(), _rss_mb())
        if not self._engine_reload_guard.acquire(block=force):
            logger.info("Engine reload already in progress in another worker, skipping (pid=%d)", os.getpid())
            return
        try:
            if not force and self._engine.size() > 0:
                # Another worker populated the store while we raced for the lock.
                return
            logger.info("Engine reload starting (force=%s, pid=%d, rss=%s)", force, os.getpid(), _rss_mb())
            cols_sql = ", ".join(f"card.{col}" for col in _ENGINE_COLUMNS_FROM_MODULE)
            try:
                with self._conn_pool.connection() as conn:
                    # Named cursor => server-side: psycopg buffers one batch, not the full result.
                    with conn.cursor(name="engine_reload") as cursor:
                        cursor.itersize = _ENGINE_RELOAD_BATCH_SIZE
                        cursor.execute(f"SELECT {cols_sql} FROM magic.cards AS card")
                        if not self._engine.reload_begin():
                            # Another process published a fresh archive while we
                            # waited for the engine's write lock; it was remapped.
                            return
                        try:
                            while batch := cursor.fetchmany(_ENGINE_RELOAD_BATCH_SIZE):
                                self._engine.add_batch(batch)
                            self._engine.reload_commit()
                        except BaseException:
                            self._engine.reload_abort()
                            raise
            except psycopg_pool.PoolClosed:
                logger.debug("Connection pool closed during engine reload, skipping (pid=%d)", os.getpid())
                return
            logger.info("Engine reloaded with %d cards (pid=%d, rss=%s)", self._engine.size(), os.getpid(), _rss_mb())
        finally:
            self._engine_reload_guard.release()

    def _resolve_result_fields(self, fields: Sequence[str] | None) -> list[str]:
        """Validate a `fields=` request against RESULT_FIELD_COLUMNS, deduping repeats.

        `None` resolves to DEFAULT_RESULT_FIELDS, mirroring `resolve_fields()` in
        card_engine/src/lib.rs so the SQL and engine paths agree on what "the usual fields" means.
        An explicit empty list is rejected rather than silently producing a fieldless SELECT.
        """
        if fields is None:
            return list(DEFAULT_RESULT_FIELDS)
        resolved = list(dict.fromkeys(fields))
        if not resolved:
            raise falcon.HTTPBadRequest(
                title="Invalid Fields",
                description="fields must include at least one field name.",
            )
        for name in resolved:
            if name not in RESULT_FIELD_COLUMNS:
                raise falcon.HTTPBadRequest(
                    title="Invalid Fields",
                    description=f"Unknown field: {name!r}",
                )
        return resolved

    @route()
    def search(  # noqa: PLR0913
        self,
        *,
        falcon_response: falcon.Response | None = None,
        # search parameters
        direction: SortDirection = SortDirection.ASC,
        fields: Sequence[str] | None = None,
        limit: int = 100,
        orderby: CardOrdering = CardOrdering.EDHREC,
        prefer: PreferOrder = PreferOrder.DEFAULT,
        q: str | None = None,
        query: str | None = None,
        shape: ResponseShape = ResponseShape.ROWS,
        unique: UniqueOn = UniqueOn.CARD,
    ) -> dict[str, Any]:
        """Run a search query and return results and metadata.

        Args:
            falcon_response: The Falcon response object (unused).
            q: Query string (alternative to query parameter).
            query: Query string (alternative to q parameter).
            direction: Sort direction ('asc' or 'desc').
            fields: Which fields to return per card (comma-separated in the query string). Defaults
                to the usual 9 (name, set_code, collector_number, power, toughness, mana_cost,
                oracle_text, set_name, type_line). See RESULT_FIELD_COLUMNS for the full vocabulary.
            limit: Maximum number of results to return.
            orderby: Field to sort by.
            shape: Shape of the "cards" list: 'rows' (list of card objects, default) or
                'columnar' (one list per field, keyed by field name — smaller on the wire).
            unique: Unique on field.
            prefer: Prefer order (oldest, newest, usd-low, usd-high, promo).

        Returns:
            Dict containing search results and metadata.
        """
        set_cache_header(falcon_response, duration=timedelta(seconds=90))
        results = self._search(
            query=query or q,
            orderby=orderby,
            direction=direction,
            fields=fields,
            limit=limit,
            unique=unique,
            prefer=prefer,
        )
        if shape == ResponseShape.COLUMNAR:
            # Shallow copy: _search returns cached dicts, which must stay row-shaped.
            results = {**results, "cards": _columnarize_cards(results["cards"])}
        return results

    def _validate_limit(self, limit: int | None) -> int | None:
        """Validate the limit and return it if valid."""
        if limit is None:
            pass
        elif isinstance(limit, int):
            if limit < 0:
                raise falcon.HTTPBadRequest(
                    title="Invalid Limit",
                    description="Limit must be a positive integer.",
                )
        else:
            raise falcon.HTTPBadRequest(
                title="Invalid Limit",
                description="Limit must be an integer.",
            )
        return limit

    def _get_where_clause(self, query: str | None) -> tuple[str, dict[str, Any]]:
        try:
            where_clause, params = get_where_clause(query)
        except ValueError as err:
            # Handle parsing errors from parse_scryfall_query
            logger.info("ValueError caught for query '%s', raising BadRequest", query)
            raise falcon.HTTPBadRequest(
                title="Invalid Search Query",
                description=f'Failed to parse query: "{query}"',
            ) from err
        return where_clause, params

    def _search(  # noqa: PLR0913
        self,
        *,
        direction: SortDirection = SortDirection.ASC,
        fields: Sequence[str] | None = None,
        limit: int = 100,
        orderby: CardOrdering = CardOrdering.EDHREC,
        prefer: PreferOrder = PreferOrder.DEFAULT,
        query: str | None = None,
        unique: UniqueOn = UniqueOn.CARD,
    ) -> dict[str, Any]:
        self._require_setup_complete()
        limit = self._validate_limit(limit)
        # Resolved once here (rather than inside _search_sql/_search_engine) so an unknown field
        # name always raises HTTPBadRequest instead of being swallowed by the engine's blanket
        # except-and-fall-back-to-SQL below.
        resolved_fields = self._resolve_result_fields(fields)

        if settings.enable_cache:
            cache_key = (direction, limit, orderby, prefer, query, unique, tuple(resolved_fields))
            gen = self._cache_generation.value
            try:
                search_cache = self._search_gen_cache[gen]
            except KeyError:
                search_cache = TTLCache(maxsize=1000, global_ttl=60)
                self._search_gen_cache[gen] = search_cache
            if cache_key in search_cache:
                return search_cache[cache_key]

        timer = Timer()

        parsed_query = None
        query = query or ""
        try:
            with timer("parse"):
                parsed_query = parse_scryfall_query(query)
        except ValueError as err:
            logger.info("ValueError caught for query '%s', raising BadRequest", query)
            raise falcon.HTTPBadRequest(
                title="Invalid Search Query",
                description=f'Failed to parse query: "{query}"',
            ) from err

        if not settings.enable_engine:
            pass  # feature-gated off: SQL serves everything, the store never loads
        elif self._engine.size() == 0:
            logger.info("Engine store empty, using SQL path for query=%r", query)
            self._trigger_background_reload_if_needed()
        else:
            try:
                result = self._search_engine(
                    parsed_query=parsed_query,
                    query=query,
                    unique=unique,
                    prefer=prefer,
                    orderby=orderby,
                    direction=direction,
                    limit=limit,
                    timer=timer,
                    fields=resolved_fields,
                )
            except Exception as e:
                logger.warning("Engine query failed for %r, falling back to SQL: %s", query, e, exc_info=True)
            else:
                if settings.enable_cache:
                    search_cache[cache_key] = result
                return result

        result = self._search_sql(
            parsed_query=parsed_query,
            query=query,
            unique=unique,
            prefer=prefer,
            orderby=orderby,
            direction=direction,
            limit=limit,
            timer=timer,
            fields=resolved_fields,
        )
        if settings.enable_cache:
            search_cache[cache_key] = result
        return result

    def _search_engine(  # noqa: PLR0913
        self,
        *,
        parsed_query: Query,
        query: str | None,
        unique: UniqueOn,
        prefer: PreferOrder,
        orderby: CardOrdering,
        direction: SortDirection,
        limit: int,
        timer: Timer,
        fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        logger.info("Searching engine for %r", query)
        query_explanation = parsed_query.to_human_explanation() if query else ""
        try:
            with timer("engine_query"):
                total_cards, cards = self._engine.query(
                    filters=parsed_query,
                    unique=str(unique),
                    prefer=str(prefer),
                    orderby=str(orderby),
                    direction=str(direction),
                    # limit=None means "no limit"; the engine requires an int, so use a large number
                    limit=limit if limit is not None else 1_000_000,
                    fields=fields,
                )
        except _QueryError as err:
            logger.info("QueryError caught for query '%s', raising BadRequest", query)
            raise falcon.HTTPBadRequest(
                title="Invalid Search Query",
                description=f'Failed to parse query: "{query}"',
            ) from err
        with timer("engine_collect"):
            cards = list(cards)
        return {
            "cards": cards,
            "compiled": "(rust engine)",
            "inner_timings": timer.get_timings(),
            "outer_timings": timer.get_timings(),
            "params": {},
            "query": query,
            "query_explanation": query_explanation,
            "total_cards": total_cards,
        }

    def _search_sql(  # noqa: PLR0913
        self,
        *,
        parsed_query: Query,
        query: str | None,
        unique: UniqueOn,
        prefer: PreferOrder,
        orderby: CardOrdering,
        direction: SortDirection,
        limit: int,
        timer: Timer,
        fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        logger.info("Searching SQL for %r", query)
        resolved_fields = self._resolve_result_fields(fields)
        query_explanation = parsed_query.to_human_explanation() if query else ""
        try:
            with timer("get_where_clause"):
                where_clause, params = generate_sql_query(parsed_query)
        except ValueError as err:
            logger.info("ValueError caught for query '%s', raising BadRequest", query)
            raise falcon.HTTPBadRequest(
                title="Invalid Search Query",
                description=f'Failed to parse query: "{query}"',
            ) from err
        sql_orderby: str = {
            # what's in the query => the db column name
            CardOrdering.CMC: "cmc",
            CardOrdering.EDHREC: "edhrec_rank",
            # lower() matches the engine, which sorts on card_name_lower
            CardOrdering.NAME: "lower(card_name)",
            CardOrdering.POWER: "creature_power",
            CardOrdering.RARITY: "card_rarity_int",
            CardOrdering.TOUGHNESS: "creature_toughness",
            CardOrdering.USD: "price_usd",
            CardOrdering.CUBECOBRA: "cubecobra_score",
        }.get(orderby, "edhrec_rank")
        sql_direction = {
            "asc": "ASC",
            "desc": "DESC",
        }.get(str(direction), "ASC")
        distinct_on = {
            UniqueOn.ARTWORK: "illustration_id",
            UniqueOn.CARD: "oracle_id",
            # there is no DISTINCT ON for printing
            # as printing is unique in the cards table
        }.get(unique)
        # Map prefer values to SQL columns and directions
        prefer_mapping = {
            PreferOrder.OLDEST: ("released_at", "ASC"),
            PreferOrder.NEWEST: ("released_at", "DESC"),
            PreferOrder.USD_LOW: ("price_usd", "ASC"),
            PreferOrder.USD_HIGH: ("price_usd", "DESC"),
            PreferOrder.PROMO: ("edhrec_rank", "ASC"),  # Use edhrec_rank as fallback for promo
            PreferOrder.DEFAULT: ("prefer_score", "DESC"),
        }
        prefer_column, prefer_direction = prefer_mapping.get(
            prefer,
            ("edhrec_rank", "ASC"),
        )
        # edhrec_rank and prefer_score are always pulled into the CTE for the ORDER BY tiebreak
        # below, whether or not the caller asked for them as output fields.
        _cte_columns = list(
            dict.fromkeys([RESULT_FIELD_COLUMNS[name] for name in resolved_fields] + ["edhrec_rank", "prefer_score"]),
        )
        _select_cols = "".join(f"\n                    {col}," for col in _cte_columns)
        _result_cols = ",\n                    ".join(f"{RESULT_FIELD_COLUMNS[name]} AS {name}" for name in resolved_fields)
        _order_by = f"""sort_value {sql_direction} NULLS LAST,
                    edhrec_rank ASC NULLS LAST,
                    prefer_score DESC NULLS LAST"""
        _count_nulls = ",\n                    ".join(f"null AS {name}" for name in resolved_fields)
        if unique == UniqueOn.PRINTING:
            # scryfall_id is the PK — every row is already unique, no dedup needed.
            # The CTE has no ORDER BY; only the LIMIT branch sorts.
            query_sql = f"""
            WITH matching_cards AS NOT MATERIALIZED (
                SELECT
                    {_select_cols}
                    {sql_orderby} AS sort_value
                FROM
                    magic.cards AS card
                WHERE
                    {where_clause}
            )
            (
                SELECT
                    null::integer AS total_cards_count,
                    {_result_cols}
                FROM
                    matching_cards
                ORDER BY
                    {_order_by}
                LIMIT
                    %(limit)s
            )
            UNION ALL
            (
                SELECT
                    COUNT(1) AS total_cards_count,
                    {_count_nulls}
                FROM
                    matching_cards
            )"""
        else:
            query_sql = f"""
            WITH distinct_cards AS (
                SELECT DISTINCT ON ({distinct_on})
                    {_select_cols}
                    {sql_orderby} AS sort_value
                FROM
                    magic.cards AS card
                WHERE
                    {where_clause}
                ORDER BY
                    {distinct_on},
                    {prefer_column} {prefer_direction} NULLS LAST,
                    prefer_score DESC NULLS LAST
            )
            (
                SELECT
                    null::integer AS total_cards_count,
                    {_result_cols}
                FROM
                    distinct_cards
                ORDER BY
                    {_order_by}
                LIMIT
                    %(limit)s
            )
            UNION ALL
            (
                SELECT
                    COUNT(1) AS total_cards_count,
                    {_count_nulls}
                FROM
                    distinct_cards
            )"""

        params["limit"] = limit
        query_sql = rewrap(query_sql)
        logger.info("Full query: %s", query_sql)
        logger.info("Params: %s", params)
        try:
            with timer("run_query"):
                result_bag = self._run_query(query=query_sql, params=params, explain=False)
        except psycopg.errors.DatatypeMismatch as err:
            # Raise BadRequest error for invalid query syntax
            # This happens with standalone arithmetic expressions like "cmc+1"
            logger.info("DatatypeMismatch caught for query '%s', raising BadRequest", query)
            raise falcon.HTTPBadRequest(
                title="Invalid Search Query",
                description=f"The search query '{query}' contains invalid syntax. "
                "Arithmetic expressions like 'cmc+1' need to be part of a comparison (e.g., 'cmc+1>3').",
            ) from err

        cards = result_bag.pop("result", [])
        count_row = cards.pop()
        total_cards = count_row["total_cards_count"]
        for icard in cards:
            icard.pop("total_cards_count")
        return {
            "cards": cards,
            "compiled": query_sql,
            "params": params,
            "query": query,
            "query_explanation": query_explanation,
            "outer_timings": timer.get_timings(),
            "inner_timings": result_bag.pop("timings"),
            "total_cards": total_cards,
        }

    @route(paths=("index", "index.html"))
    def _redirect_to_root(self, **_: object) -> None:
        """Send the legacy index paths to /.

        Raises:
            falcon.HTTPMovedPermanently: Always; these paths exist only to redirect.
        """
        msg = "/"
        raise falcon.HTTPMovedPermanently(msg)

    @route()
    def _root(  # noqa: PLR0913
        self,
        *,
        falcon_response: falcon.Response | None = None,
        request_host: str = "",
        q: str | None = None,
        query: str | None = None,
        orderby: CardOrdering | None = None,
        direction: SortDirection | None = None,
        unique: UniqueOn | None = None,
        prefer: PreferOrder | None = None,
        **_: object,
    ) -> None:
        """Return the index page, optionally with embedded search results.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
            request_host (str): Value of the Host header, used to derive the site name.
            q (str): Search query (alternative to query parameter).
            query (str): Search query (alternative to q parameter).
            orderby (CardOrdering): Field to sort by.
            direction (SortDirection): Sort direction.
            unique (UniqueOn): Unique on field.
            prefer (PreferOrder): Prefer order.

        """
        site_name = hostname_to_site_name(request_host)
        html_content = build_base_html(self._critical_css, site_name)

        # Check if we have a search query
        search_query = query or q
        if search_query:
            # Run the search server-side and embed results in the HTML
            try:
                search_results = self._search(
                    query=search_query,
                    orderby=orderby or CardOrdering.EDHREC,
                    direction=direction or SortDirection.ASC,
                    unique=unique or UniqueOn.CARD,
                    prefer=prefer or PreferOrder.DEFAULT,
                )

                # Get cards from results
                cards = search_results.get("cards", [])
                total_cards = search_results.get("total_cards", len(cards))

                # Generate server-side HTML for cards (for no-JS support)
                results_html = generate_results_html(cards) if cards else ""
                results_count_html = generate_results_count_html(total_cards, search_query) if cards else ""

                # Inject the server-side rendered HTML
                html_content = html_content.replace(
                    "<!-- SERVER_SIDE_RESULTS -->",
                    results_html,
                )

                # Inject the results count into the status message container
                if results_count_html:
                    html_content = html_content.replace(
                        "<!-- SERVER_SIDE_RESULTS_COUNT -->",
                        f'<div class="results-count">{results_count_html}</div>',
                    )

                # Convert search results to JSON and embed for JavaScript enhancement
                search_results_json = orjson.dumps(search_results).decode("utf-8")
                embedded_data = f"""// Server-side embedded search results
      window.EMBEDDED_SEARCH_RESULTS = {search_results_json};
      """
                # Replace the placeholder token with the embedded data
                html_content = html_content.replace(
                    "<!-- SERVER_SIDE_EMBEDDED_DATA -->",
                    embedded_data,
                )
                # Disable caching for pages with search results
                set_cache_header(falcon_response, duration=timedelta(seconds=90))
            except (ValueError, falcon.HTTPBadRequest, psycopg.errors.DatatypeMismatch) as err:
                # If search fails, just serve the page without embedded results
                logger.warning("Failed to embed search results: %s", err)
                set_cache_header(falcon_response, duration=timedelta(hours=1))
        else:
            # Cache for 1 hour - improves repeat visit performance
            set_cache_header(falcon_response, duration=timedelta(hours=1))

        falcon_response.text = html_content
        falcon_response.content_type = "text/html"

    @route(paths=("favicon.ico", "static/favicon.ico"))
    def favicon_ico(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the favicon.ico file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        full_filename = STATIC_DIR / "favicon.ico"
        with pathlib.Path(full_filename).open(mode="rb") as f:
            falcon_response.data = contents = f.read()
        falcon_response.content_type = "image/vnd.microsoft.icon"
        content_length = len(contents)
        logger.info("Favicon content length: %d", content_length)
        falcon_response.headers["content-length"] = content_length
        # Cache favicon for 7 days - it rarely changes
        set_cache_header(falcon_response, duration=timedelta(days=7))

    @route(paths=("static/social-preview.webp",))
    def social_preview_webp(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the social preview image."""
        if falcon_response is None:
            return
        full_filename = STATIC_DIR / "social-preview.webp"
        with full_filename.open(mode="rb") as f:
            contents = f.read()
        falcon_response.data = contents
        falcon_response.content_type = "image/webp"
        falcon_response.headers["content-length"] = len(contents)
        set_cache_header(falcon_response, duration=timedelta(days=30))

    @route(paths=("static/styles.css",))
    def styles_css(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the styles.css file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        self._serve_static_file(filename="styles.css", falcon_response=falcon_response)
        falcon_response.content_type = "text/css"
        set_cache_header(falcon_response, duration=timedelta(days=30))

    @route(paths=("static/app.js",))
    def app_js(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the app.js file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        self._serve_static_file(filename="app.js", falcon_response=falcon_response)
        falcon_response.content_type = "application/javascript"
        # Cache JavaScript for 1 hour - it changes infrequently
        set_cache_header(falcon_response, duration=timedelta(hours=1))

    @route(paths=("static/app.min.js",))
    def app_min_js(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the app.min.js file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        self._serve_static_file(filename="app.min.js", falcon_response=falcon_response)
        falcon_response.content_type = "application/javascript"
        set_cache_header(falcon_response, duration=timedelta(days=30))

    @route(paths=("robots.txt",))
    def robots_txt(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the robots.txt file."""
        if falcon_response is None:
            return
        self._serve_static_file(filename="robots.txt", falcon_response=falcon_response)
        falcon_response.content_type = "text/plain"

    @route(paths=("static/card.js",))
    def card_js(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the card.js file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        self._serve_static_file(filename="card.js", falcon_response=falcon_response)
        falcon_response.content_type = "application/javascript"
        set_cache_header(falcon_response, duration=timedelta(hours=1))

    @route()
    def card(
        self,
        set_code: str = "",
        collector_number: str = "",
        *,
        request_host: str = "",
        falcon_response: falcon.Response | None = None,
        **_: object,
    ) -> None:
        """Serve the per-card page for /card/{set_code}/{collector_number}.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
            set_code (str): The card set code extracted from the URL path.
            collector_number (str): The collector number extracted from the URL path.
            request_host (str): Host header value used to derive the site name shown in page chrome/title.
        """
        del set_code, collector_number
        if falcon_response is None:
            return
        site_name = hostname_to_site_name(request_host)
        html = build_card_html(self._critical_css)
        falcon_response.text = html.replace(SITE_NAME_PLACEHOLDER, site_name)
        falcon_response.content_type = "text/html"
        set_cache_header(falcon_response, duration=timedelta(hours=1))

    def _serve_static_file(self, *, filename: str, falcon_response: falcon.Response) -> None:
        """Serve a static file to the Falcon response.

        Args:
        ----
            filename (str): The file to serve.
            falcon_response (falcon.Response): The Falcon response to write to.

        """
        full_filename = STATIC_DIR / filename
        try:
            with pathlib.Path(full_filename).open() as f:
                falcon_response.text = f.read()
        except FileNotFoundError:
            falcon_response.status = falcon.HTTP_404
            falcon_response.text = f"File not found: {filename}"
        except PermissionError:
            falcon_response.status = falcon.HTTP_403
            falcon_response.text = f"Permission denied: {filename}"
        except OSError as e:
            falcon_response.status = falcon.HTTP_500
            falcon_response.text = f"Error reading file {filename}: {e}"

    @route()
    def get_catalog(
        self,
        *,
        falcon_response: falcon.Response | None = None,
        **_: object,
    ) -> dict[str, dict[str, int]]:
        """Get type and keyword frequency catalogs from the engine."""
        if self._engine.size() == 0:
            raise falcon.HTTPServiceUnavailable(
                title="Service Unavailable",
                description="Engine is not loaded, please try again later.",
            ) from None
        set_cache_header(falcon_response, duration=timedelta(hours=1))
        type_counts: dict[str, int] = self._engine.common_card_types()
        # tribal is the old name for kindred
        kindred_count = type_counts.get("Kindred", 0)
        if kindred_count:
            type_counts["Tribal"] = kindred_count
        keyword_counts: dict[str, int] = self._engine.common_card_keywords()
        keyword_catalog = {keyword.lower(): count for keyword, count in keyword_counts.items()}
        # Sorted keys compress ~5% smaller (adjacent keys share prefixes, so the
        # compressor's back-references stay short) and make the payload deterministic.
        # orjson preserves insertion order, so sorting here is what clients receive.
        # Sorting must happen after the Tribal alias is inserted above.
        return {
            "types": dict(sorted(type_counts.items())),
            "keywords": dict(sorted(keyword_catalog.items())),
        }

    @route()
    def get_common_keywords(self, **_: object) -> list[dict[str, Any]]:
        """Get the common keywords from the database."""
        return self._run_query(
            query=db_utils.read_sql("get_common_keywords"),
        )["result"]

    @route()
    def random_search(
        self,
        *,
        falcon_response: falcon.Response | None = None,
        num_cards: int = 1,
        shape: ResponseShape = ResponseShape.ROWS,
        **_: object,
    ) -> dict[str, Any]:
        """Return one or more random cards in the same envelope shape as search().

        Args:
            falcon_response: The Falcon response object.
            num_cards: The number of random cards to return (default is 1).
            shape: Shape of the "cards" list: 'rows' (list of card objects, default) or
                'columnar' (one list per field, keyed by field name — smaller on the wire).

        Returns:
            A dict with a "cards" key (list of card dicts) and "total_cards" key,
            matching the shape returned by search().
        """
        set_no_store_header(falcon_response)
        num_cards = min(max(num_cards, 1), 1000)
        if self._engine.size() == 0:
            self._trigger_background_reload_if_needed()
            cards = []
        else:
            cards = list(self._engine.sample_preferred(num_cards))
        total_cards = len(cards)
        if shape == ResponseShape.COLUMNAR:
            cards = _columnarize_cards(cards)
        return {"cards": cards, "total_cards": total_cards}
