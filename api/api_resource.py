"""Implementation of the routes of our simple api."""

from __future__ import annotations

import inspect
import logging
import os
import threading
import time

# Imported at runtime, not under TYPE_CHECKING, because route handlers annotate parameters with it and
# ParamBinder resolves those annotations to real types at registration. Under TYPE_CHECKING the name is
# absent at runtime and resolution fails, which is a startup error by design rather than a silent loss
# of coercion. Ruff's TC003 wants it moved; the runtime-evaluated-decorators setting will make the noqa
# unnecessary once handlers carry a route decorator.
from collections.abc import Sequence  # noqa: TC003
from datetime import timedelta
from typing import TYPE_CHECKING, Any, NoReturn

import falcon
import orjson
import psycopg
from cachebox import LRUCache, TTLCache

from api.admin_resource import ADMIN_MOUNT_PREFIX, AdminContext, AdminResource
from api.app_context import AppContext
from api.enums import CardOrdering, PreferOrder, ResponseShape, SortDirection, UniqueOn
from api.middlewares.timing import record_span
from api.noscript_helpers import generate_results_count_html, generate_results_html
from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.query_budget import (
    QUERY_REGEX_REJECTED_MESSAGE,
    InvalidRegexPatternError,
    QueryBudgetExceeded,
    bounded_query_log_context,
)
from api.parsing.rewrite import InvalidDateValueError
from api.settings import settings
from api.utils import db_utils, error_monitoring
from api.utils.css_utils import build_critical_css
from api.utils.generation_cache import GenerationCache
from api.utils.page_rendering import (
    SITE_NAME_PLACEHOLDER,
    STATIC_DIR,
    build_base_html,
    build_card_html,
    read_static_bytes,
    serialize_embedded_json,
    serve_static_file,
)
from api.utils.param_binding import ParamCoercionError
from api.utils.routing import build_route_table, build_routes_listing, route
from api.utils.site_name import hostname_to_site_name
from api.utils.timer import Timer
from card_engine import FatalQueryError as _FatalQueryError
from card_engine import RetryableQueryError as _RetryableQueryError

if TYPE_CHECKING:
    from api.parsing.nodes import Query
    from api.utils.routing import BoundRoute


logger = logging.getLogger(__name__)


# Postgres prefixes every 2201B message with this, e.g. "invalid regular expression: brackets []
# not balanced". Stripped before the reason is quoted back, or the error message says it twice.
_PG_REGEX_ERROR_PREFIX = "invalid regular expression: "
# Used when Postgres gives no diagnostic message to quote (a synthesized error, mostly in tests).
_FALLBACK_REGEX_ERROR_REASON = "the pattern could not be parsed"


def regex_error_reason(message_primary: str | None) -> str:
    """Return the quotable reason from a Postgres 2201B message, without repeating its prefix.

    Args:
        message_primary: The ``diag.message_primary`` of an InvalidRegularExpression, if it has one.

    Returns:
        The reason to show the user, e.g. "brackets [] not balanced".
    """
    return (message_primary or "").removeprefix(_PG_REGEX_ERROR_PREFIX).strip() or _FALLBACK_REGEX_ERROR_REASON


def _raise_query_bad_request(*, exc_name: str, query: str, description: str, err: Exception) -> NoReturn:
    """Log and re-raise a Postgres user-error exception as a 400, in the shape every such handler needs.

    Args:
        exc_name: The Postgres exception's name, for the info log (e.g. "DatatypeMismatch").
        query: The raw search query string that triggered the error.
        description: The user-facing description for the HTTPBadRequest.
        err: The caught exception, chained onto the raised HTTPBadRequest via `from`.
    """
    logger.info("%s caught for query '%s' (%s), raising BadRequest", exc_name, query, err)
    raise falcon.HTTPBadRequest(title="Invalid Search Query", description=description) from err


# Query parameters that must not be forwarded to action handlers.
DISALLOWED_QUERY_ARGS: frozenset[str] = frozenset(["falcon_response", "request_host"])

# Body for an unhandled exception. Fixed and content-free on purpose: the frames live at throw sites
# inside query and import paths, so their locals can hold connection and query state. Diagnostics go
# to the log and the error monitor, which are not attacker-readable; the client gets this and nothing
# more. Callers must not append exception detail to it.
INTERNAL_ERROR_DESCRIPTION = "An internal error occurred."

# Public field name -> magic.cards column. The `fields=` vocabulary for /search. This is
# deliberately a subset of FIELD_TABLE in card_engine/src/lib.rs, not a mirror of it — not
# everything the engine can extract needs to be a public API field. Every key here must still
# have a same-named entry in FIELD_TABLE with matching semantics, so a `fields=` request for one
# of these names gets identically-shaped results regardless of which path serves it; FIELD_TABLE
# is free to have entries with no counterpart here.
# Pagination default: offset 0 everywhere it appears, extracted so the internal
# search methods keep it optional (per review) and route/internal defaults can
# never drift apart.
DEFAULT_OFFSET = 0
PAGINATION_BASE_TIMESTAMP = 1_409_018_789
PAGINATION_GROWTH_INTERVAL_SECONDS = 3_155


def pagination_ceiling() -> int:
    """Return the continuously growing pagination ceiling for limit and offset."""
    return int((time.time() - PAGINATION_BASE_TIMESTAMP) // PAGINATION_GROWTH_INTERVAL_SECONDS)


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
    # Card-data fields consumers need to run their own downstream filtering
    # (Scryfall JSON names and shapes): layout and rarity are plain text,
    # cmc an integer, legalities the imported {format: status} object, and
    # color_identity a WUBRG-ordered letter list (the raw column is a JSONB
    # object -- _search_sql reshapes it; the engine emits the list directly).
    "layout": "card_layout",
    "cmc": "cmc",
    "rarity": "card_rarity_text",
    "color_identity": "card_color_identity",
    "legalities": "card_legalities",
}

# Scryfall's canonical color order, used to reshape identity objects into lists.
_COLOR_ORDER: tuple[str, ...] = ("W", "U", "B", "R", "G", "C")


def _identity_letters(identity: dict[str, object] | None) -> tuple[str, ...]:
    """Reshape a JSONB color-identity object into Scryfall's WUBRG-ordered letter tuple.

    A tuple, not a list, to stay identical to the engine path: `color_identity` is served there
    from 64 cached tuples, one per color mask, which is only safe because tuples are immutable.
    JSON output is unaffected -- orjson writes either as an array.
    """
    if not identity:
        return ()
    if len(identity) == 1:
        # A single color is trivially already in WUBRG order -- no need to walk _COLOR_ORDER.
        return tuple(identity)
    return tuple(letter for letter in _COLOR_ORDER if letter in identity)


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


def _copy_query_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a `_run_query` result that's independent of the cached/shared original.

    Callers only ever pop or reassign top-level keys -- on the outer dict (`result_bag.pop(...)`)
    and on each row (`icard.pop(...)`, `icard["color_identity"] = ...`) -- never a nested value
    in place, so copying the outer dict and each row dict one level deep is enough to make this
    call's result safe to mutate without disturbing the cache entry or a concurrent caller. A full
    `copy.deepcopy` of the whole result (including every row's nested JSONB fields) recurses far
    more than that guarantee requires.
    """
    copied = dict(result)
    if "result" in copied:
        copied["result"] = [dict(row) for row in copied["result"]]
    return copied


class APIResource:
    """Class implementing request handling for our simple API."""

    def __init__(
        self,
        *,
        app_context: AppContext | None = None,
        admin_context: AdminContext | None = None,
    ) -> None:
        """Initialize an APIResource object, set up connection pool and action map.

        Sets up the database connection pool and action mapping for the API.

        Args:
            app_context: State and resources shared with the mounted AdminResource (connection
                pools, the query engine, the cross-worker cache/import signals). Built fresh if not
                given, matching every other handle here -- a caller (a test, mainly) can inject one
                without a real pool ever opening a connection.
            admin_context: Forwarded to the mounted AdminResource untouched; APIResource has no use
                for its contents (import-serialisation primitives) and doesn't default it -- that's
                AdminResource's own job, same as `app_context`.
        """
        self._critical_css: str = build_critical_css(STATIC_DIR / "styles.css")
        self.app_context = app_context or AppContext()
        # Build the route table from methods marked with @route, scanning the class rather than this
        # instance so nothing assigned below can become a route. Each entry carries everything
        # dispatch needs — the wrapped handler, how many positional path segments it absorbs, and
        # what it declared — computed once here rather than per-request in _handle.
        self.routes = build_route_table(self)

        self._query_cache: GenerationCache = GenerationCache(
            factory=lambda: LRUCache(maxsize=1_000 if settings.enable_cache else 1),
            generation=self.app_context.cache_generation,
        )
        self._search_gen_cache: LRUCache = LRUCache(maxsize=1)  # generation → TTLCache
        self._engine_reload_lock = threading.Lock()
        logger.info("Worker with pid %d has conn pool %s", os.getpid(), self.app_context.reader_pool)

        # Mounted after the parent's own state exists, since the child reaches back for the handles
        # they share. advertise=False is set here rather than on each handler: forgetting it is then
        # a property of this one call, not a hole in one route.
        self.admin = AdminResource(app_context=self.app_context, admin_context=admin_context)
        self.routes.update(build_route_table(self.admin, prefix=ADMIN_MOUNT_PREFIX, advertise=False))
        self._not_found_routes = build_routes_listing(self.routes)
        # Shown only once AdminAuthMiddleware has stamped req.context["admin_authenticated"] —
        # see _raise_not_found. Precomputed here for the same reason as _not_found_routes:
        # inspect.signature() per route isn't free, and the table never changes after construction.
        self._authenticated_not_found_routes = build_routes_listing(self.routes, include_unadvertised=True)

        self.admin.setup_schema()
        self.admin.import_data()  # ensures that database is setup

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

    def _build_action_kwargs(self, req: falcon.Request, resp: falcon.Response, entry: BoundRoute | None) -> dict[str, Any]:
        """Assemble the keyword arguments passed to a route's action.

        Args:
            req: The incoming request.
            resp: The response the action will populate.
            entry: The matched route, or None when the path didn't resolve to anything -- in which
                case action is _raise_not_found rather than a real route handler.

        Returns:
            Keyword arguments for the action call.
        """
        params = {k: v for k, v in req.params.items() if k not in DISALLOWED_QUERY_ARGS}
        if entry is None:
            # Only _raise_not_found reads this; set after the query string so a request can't
            # spoof it via ?admin_authenticated=1 on a path that doesn't resolve to anything.
            params["admin_authenticated"] = req.context.get("admin_authenticated", False)
        params["falcon_response"] = resp
        params["request_host"] = req.get_header("X-Proxy-Host") or req.host
        return params

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
            res = action(*action_args, **self._build_action_kwargs(req, resp, entry))
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

    def _raise_not_found(self, *_args: object, admin_authenticated: bool = False, **_: object) -> None:
        """Raise a Falcon HTTPNotFound error with available routes.

        Args:
            admin_authenticated: Whether this request already carried a valid admin credential
                (AdminAuthMiddleware, via req.context). A caller who has already proven they hold
                the shared secret gains nothing from the admin routes being hidden, so they get the
                full listing instead of just the public one.
        """
        routes = self._authenticated_not_found_routes if admin_authenticated else self._not_found_routes
        raise falcon.HTTPNotFound(
            title="Not Found",
            description={
                "routes": routes,
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
                return _copy_query_result(cached_val)

        params = {k: db_utils.maybe_json(v) for k, v in params.items()}

        root_timing_key = "root_timing_key"
        timer = Timer()
        result: dict[str, Any] = {}
        with self.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            # Validate and set statement timeout
            db_utils.set_statement_timeout(cursor, statement_timeout)
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

        return _copy_query_result(result)

    @route()
    def get_pid(self, *, falcon_response: falcon.Response | None = None, **_: object) -> int:
        """Just return the pid of the process which served this request.

        Returns:
        -------
            int: The process ID.

        """
        set_no_store_header(falcon_response)
        return os.getpid()

    def _require_setup_complete(self) -> None:
        """Require that setup is complete or raise a ServiceUnavailable error."""
        if not self.app_context.setup_complete():
            logger.warning("Rejecting request in pid %d: setup is not complete", os.getpid())
            raise falcon.HTTPServiceUnavailable(
                title="Service Unavailable",
                description="Setup is not complete, please try again later.",
            ) from None

    def _trigger_background_reload_if_needed(self) -> None:
        if self.app_context.engine.size() == 0 and self._engine_reload_lock.acquire(blocking=False):

            def _bg_reload() -> None:
                try:
                    self.app_context.reload_engine()
                except Exception as e:
                    logger.error("Background engine reload failed: %s", e, exc_info=True)
                finally:
                    self._engine_reload_lock.release()

            threading.Thread(target=_bg_reload, daemon=True).start()

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
        offset: int = DEFAULT_OFFSET,
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
            limit: Maximum number of results to return. Must be between 0 and the
                continuously growing pagination ceiling (approximately 10,000 additional
                results per year). Defaults to 100.
            offset: Number of results to skip before the first returned card, in the
                same sort order the query uses -- limit/offset together give clients
                pagination over the full result set (total_cards is always the
                unpaginated count). Must be between 0 and the continuously growing
                pagination ceiling (approximately 10,000 additional results per year).
                Defaults to 0.
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
            offset=offset,
            unique=unique,
            prefer=prefer,
        )
        if shape == ResponseShape.COLUMNAR:
            # Shallow copy: _search returns cached dicts, which must stay row-shaped.
            results = {**results, "cards": _columnarize_cards(results["cards"])}
        return results

    def _validate_offset(self, offset: int) -> int:
        """Validate the offset and return it if valid."""
        ceiling = pagination_ceiling()
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 or offset > ceiling:
            raise falcon.HTTPBadRequest(
                title="Invalid Offset",
                description=f"Offset must be an integer between 0 and {ceiling}.",
            )
        return offset

    def _validate_limit(self, limit: int | None) -> int | None:
        """Validate the limit and return it if valid."""
        if limit is None:
            return None
        ceiling = pagination_ceiling()
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0 or limit > ceiling:
            raise falcon.HTTPBadRequest(
                title="Invalid Limit",
                description=f"Limit must be an integer between 0 and {ceiling}.",
            )
        return limit

    def _search(  # noqa: PLR0913
        self,
        *,
        direction: SortDirection = SortDirection.ASC,
        fields: Sequence[str] | None = None,
        limit: int = 100,
        offset: int = DEFAULT_OFFSET,
        orderby: CardOrdering = CardOrdering.EDHREC,
        prefer: PreferOrder = PreferOrder.DEFAULT,
        query: str | None = None,
        unique: UniqueOn = UniqueOn.CARD,
    ) -> dict[str, Any]:
        self._require_setup_complete()
        limit = self._validate_limit(limit)
        offset = self._validate_offset(offset)
        # Resolved once here (rather than inside _search_sql/_search_engine) so an unknown field
        # name always raises HTTPBadRequest instead of being swallowed by the engine's blanket
        # except-and-fall-back-to-SQL below.
        resolved_fields = self._resolve_result_fields(fields)

        if settings.enable_cache:
            cache_key = (direction, limit, offset, orderby, prefer, query, unique, tuple(resolved_fields))
            gen = self.app_context.cache_generation.value
            try:
                search_cache = self._search_gen_cache[gen]
            except KeyError:
                search_cache = TTLCache(maxsize=1000, global_ttl=60)
                self._search_gen_cache[gen] = search_cache
            if cache_key in search_cache:
                return search_cache[cache_key]

        timer = Timer()

        query = query or ""
        parsed_query = self._parse_search_query(query, timer)

        if not settings.enable_engine:
            pass  # feature-gated off: SQL serves everything, the store never loads
        elif self.app_context.engine.size() == 0:
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
                    offset=offset,
                    timer=timer,
                    fields=resolved_fields,
                )
            except BaseException as e:
                # BaseException, not Exception: a Rust panic anywhere under `self.app_context.engine.query`
                # surfaces as pyo3's `PanicException`, which derives from BaseException and so went
                # straight past this handler — the one whose entire job is to let an engine failure
                # degrade to the SQL path instead of failing the request. Falcon's own error handling
                # catches Exception too, so nothing of ours ran: the panic left the WSGI handler and
                # bjoern turned it into a bare 500 on a query the SQL path answers fine.
                #
                # Measured, because the obvious guess is worse than the truth and was in this comment:
                # bjoern prints the traceback and keeps serving. The worker does NOT die, so
                # `_all_workers_alive` in entrypoint.py — which tears down every worker when one dies
                # — is not in play. The cost is one wrong 500, not lost capacity.
                #
                # `pyo3_runtime.PanicException` is not imported and named directly: the module only
                # exists once a pyo3 extension has registered it, so naming it would couple this
                # fallback to the engine having loaded and to pyo3's own module layout. The two
                # BaseExceptions that must still propagate are the ones that are not failures.
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(e, _FatalQueryError):
                    log_ctx = bounded_query_log_context(query)
                    logger.info(
                        "Fatal query error from engine (%s) preview=%r digest=%s",
                        e,
                        log_ctx["query_preview"],
                        log_ctx["query_digest"],
                    )
                    raise falcon.HTTPBadRequest(
                        title="Invalid Search Query",
                        description=QUERY_REGEX_REJECTED_MESSAGE,
                    ) from e
                # _RetryableQueryError means the engine declined to build the query, not that it
                # broke — _search_engine has already logged it at info. It reaches here for
                # several different reasons (an unsupported regex feature on the linear path, an
                # attribute the engine hasn't wired a filter up for, ...), and the SQL path
                # resolves all of them correctly on its own. Re-logging it at warning with a stack
                # trace turned every keystroke inside a character class into an alertable event for
                # a user typo. Fall through quietly; anything else really is an engine failure and
                # keeps its traceback.
                declined = isinstance(e, _RetryableQueryError)
                logger.log(
                    logging.INFO if declined else logging.WARNING,
                    "Engine %s %r, falling back to SQL: %s",
                    "declined" if declined else "failed on",
                    query,
                    e,
                    exc_info=not declined,
                )
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
            offset=offset,
            timer=timer,
            fields=resolved_fields,
        )
        if settings.enable_cache:
            search_cache[cache_key] = result
        return result

    def _parse_search_query(self, query: str, timer: Timer) -> Query:
        """Parse a /search query string, turning every parse-time rejection into the 400 it deserves."""
        try:
            with timer("parse"):
                # `date:<set code>` resolves against a registry this process fills from the database;
                # a no-op after the first search per import, and never a reason for a search to fail.
                self.app_context.ensure_set_release_dates()
                return parse_scryfall_query(query)
        except QueryBudgetExceeded as err:
            log_ctx = bounded_query_log_context(query)
            logger.info(
                "Query budget exceeded (%s) preview=%r digest=%s",
                err.kind,
                log_ctx["query_preview"],
                log_ctx["query_digest"],
            )
            raise falcon.HTTPBadRequest(
                title="Invalid Search Query",
                description=err.user_message,
            ) from err
        except InvalidRegexPatternError as err:
            _raise_query_bad_request(
                exc_name="InvalidRegexPattern",
                query=query,
                description=err.user_message_for_query(query),
                err=err,
            )
        except InvalidDateValueError as err:
            # Scryfall's own sentence for `date>=zzzz`; more useful than the generic parse failure
            # below, and it discloses nothing but the value the user typed.
            _raise_query_bad_request(exc_name="InvalidDateValue", query=query, description=err.user_message, err=err)
        except ValueError as err:
            _raise_query_bad_request(exc_name="ValueError", query=query, description=f'Failed to parse query: "{query}"', err=err)

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
        offset: int = DEFAULT_OFFSET,
        fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        logger.info("Searching engine for %r", query)
        query_explanation = parsed_query.to_human_explanation() if query else ""
        try:
            with timer("engine_query"):
                total_cards, cards = self.app_context.engine.query(
                    filters=parsed_query,
                    unique=str(unique),
                    prefer=str(prefer),
                    orderby=str(orderby),
                    direction=str(direction),
                    # limit=None means "no limit"; the engine requires an int, so use a large number
                    limit=limit if limit is not None else 1_000_000,
                    offset=offset,
                    fields=fields,
                )
        except _RetryableQueryError:
            logger.info("RetryableQueryError caught for query '%s', declining to SQL", query)
            raise
        # `cards` is already a plain, freshly-built, unshared list -- card_engine's query()
        # eagerly materializes a PyList before returning, it's never a lazy iterator -- so
        # there's nothing left to collect here.
        timings = timer.get_timings()
        return {
            "cards": cards,
            "compiled": "(rust engine)",
            "inner_timings": timings,
            "outer_timings": timings,
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
        offset: int = DEFAULT_OFFSET,
        fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        logger.info("Searching SQL for %r", query)
        resolved_fields = self._resolve_result_fields(fields)
        query_explanation = parsed_query.to_human_explanation() if query else ""
        try:
            with timer("get_where_clause"):
                where_clause, params = generate_sql_query(parsed_query)
        except ValueError as err:
            _raise_query_bad_request(exc_name="ValueError", query=query, description=f'Failed to parse query: "{query}"', err=err)
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
                OFFSET
                    %(offset)s
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
                OFFSET
                    %(offset)s
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
        params["offset"] = offset
        query_sql = rewrap(query_sql)
        logger.info("Full query: %s", query_sql)
        logger.info("Params: %s", params)
        try:
            with timer("run_query"):
                result_bag = self._run_query(query=query_sql, params=params, explain=False)
        except psycopg.errors.InvalidRegularExpression as err:
            # The parser does not validate regex syntax, so Postgres is the first thing to see a bad
            # pattern. That is a user error, not a server error: typeahead balances a half-typed regex
            # into a complete one on every keystroke, so `o:/^[/` is an ordinary intermediate state.
            # Caught ahead of DataError below (InvalidRegularExpression is a subclass of it) purely
            # for this nicer, prefix-stripped message; the fallback would still catch it otherwise.
            reason = regex_error_reason(err.diag.message_primary)
            _raise_query_bad_request(
                exc_name="InvalidRegularExpression",
                query=query,
                description=f"The search query '{query}' contains an invalid regular expression: {reason}.",
                err=err,
            )
        except (psycopg.errors.DatatypeMismatch, psycopg.errors.DataError) as err:
            # DatatypeMismatch (class 42, e.g. a standalone arithmetic expression like "cmc+1" used
            # bare as a WHERE clause) and DataError (class 22, e.g. DivisionByZero from "power/0>1",
            # NumericValueOutOfRange, InvalidTextRepresentation) are Postgres's own two ways of saying
            # "this query is syntactically valid SQL but the data doesn't work" — a user error to 400,
            # not a server error to 500. Message comes straight from Postgres rather than a bespoke
            # string per error class: covers every current and future member of either class for free,
            # at the cost of a more technical-sounding message than a hand-written one per case would
            # give (see the InvalidRegularExpression handler above for that tradeoff made the other way).
            reason = (err.diag.message_primary or "").strip() or "the value is not valid for this comparison"
            _raise_query_bad_request(
                exc_name=type(err).__name__,
                query=query,
                description=f"The search query '{query}' is invalid: {reason}.",
                err=err,
            )

        cards = result_bag.pop("result", [])
        count_row = cards.pop()
        total_cards = count_row["total_cards_count"]
        reshape_identity = "color_identity" in resolved_fields
        for icard in cards:
            icard.pop("total_cards_count")
            if reshape_identity:
                # Match the engine path's shape (see FIELD_TABLE in card_engine).
                icard["color_identity"] = _identity_letters(icard["color_identity"])
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
                search_results_json = serialize_embedded_json(search_results)
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
        contents = read_static_bytes("favicon.ico")
        falcon_response.data = contents
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
        contents = read_static_bytes("social-preview.webp")
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
        serve_static_file(filename="styles.css", falcon_response=falcon_response)
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
        serve_static_file(filename="app.js", falcon_response=falcon_response)
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
        serve_static_file(filename="app.min.js", falcon_response=falcon_response)
        falcon_response.content_type = "application/javascript"
        set_cache_header(falcon_response, duration=timedelta(days=30))

    @route(paths=("robots.txt",))
    def robots_txt(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the robots.txt file."""
        if falcon_response is None:
            return
        serve_static_file(filename="robots.txt", falcon_response=falcon_response)
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
        serve_static_file(filename="card.js", falcon_response=falcon_response)
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

    @route()
    def get_catalog(
        self,
        *,
        falcon_response: falcon.Response | None = None,
        **_: object,
    ) -> dict[str, dict[str, int]]:
        """Get type and keyword frequency catalogs from the engine."""
        if self.app_context.engine.size() == 0:
            raise falcon.HTTPServiceUnavailable(
                title="Service Unavailable",
                description="Engine is not loaded, please try again later.",
            ) from None
        set_cache_header(falcon_response, duration=timedelta(hours=1))
        type_counts: dict[str, int] = self.app_context.engine.common_card_types()
        # tribal is the old name for kindred
        kindred_count = type_counts.get("Kindred", 0)
        if kindred_count:
            type_counts["Tribal"] = kindred_count
        keyword_counts: dict[str, int] = self.app_context.engine.common_card_keywords()
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
        """Get the common keywords from the database.

        Unlike /search, this has no engine-backed path -- it always queries SQL directly, so
        `explain` matters every time it's called, not just on a fallback.
        """
        return self._run_query(
            query=db_utils.read_sql("get_common_keywords"),
            explain=False,
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
        if self.app_context.engine.size() == 0:
            self._trigger_background_reload_if_needed()
            cards = []
        else:
            cards = list(self.app_context.engine.sample_preferred(num_cards))
        total_cards = len(cards)
        if shape == ResponseShape.COLUMNAR:
            cards = _columnarize_cards(cards)
        return {"cards": cards, "total_cards": total_cards}
