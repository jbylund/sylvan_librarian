"""Implementation of the routes of our simple api."""

from __future__ import annotations

import collections
import copy
import csv
import datetime
import inspect
import itertools
import logging
import multiprocessing
import os
import pathlib
import random
import re
import secrets
import time
import urllib.parse
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any
from typing import cast as typecast

import cachebox
import falcon
import orjson
import psycopg
import requests
from cachebox import LRUCache, TTLCache
from cachebox import cached as cachebox_cached
from psycopg import Connection, Cursor

from api.card_processing import preprocess_card
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.noscript_helpers import generate_results_count_html, generate_results_html
from api.parsing import generate_sql_query, parse_scryfall_query
from api.scryfall_bulk_data_fetcher import BulkDataKey, ScryfallBulkDataFetcher
from api.settings import settings
from api.tagger_client import TaggerClient
from api.utils import db_utils, error_monitoring, multiprocessing_utils
from api.utils.timer import Timer
from api.utils.type_conversions import _get_type_name, make_type_converting_wrapper

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from multiprocessing.synchronize import RLock as LockType

    import psycopg_pool


logger = logging.getLogger(__name__)

# pylint: disable=c-extension-no-member
NOT_FOUND = 404
IMPORT_EXPORT = True
MIN_IMPORT_INTERVAL = 300
IMPORT_LOCK_TIMEOUT = 2
MIN_IMPORT_CARDS = 90_000

CUSTOM_IS_TAGS = [
    "historic",  # artifact, legendary, saga
    "pathway",  # land and name contains pathway
    "permanent",  # ...
    "reprint",
    "spell",  # ...
    "unique",  # has exactly one printing
    "old",  # 93/97 frame
    "new",  # newer frames
    "foil",  # foil version of a card
    "nonfoil",  # non-foil version of a card
    "datestamped",  # can get from the json promo_types array
    "universesbeyond",  # can get from the json promo_types array
    # I don't know how to do this, I just don't want to make the normal requests
    "booster",
    "default",
]

# default/atypical are complementary and disjoint
# so in theory we could query for one and build the other by
# querying and inverting

LAND_IS_TAGS = [
    "bikeland",
    "bondland",
    "bounceland",
    "canopyland",
    "checkland",
    "creatureland",
    "fastland",
    "fetchland",
    "filterland",
    "gainland",
    "manland",
    "painland",
    "scryland",
    "shadowland",
    "shockland",
    "slowland",
    "storageland",
    "surveilland",
    "tangoland",
    "tricycleland",
    "triland",
]
CARD_IS_TAGS = LAND_IS_TAGS + [  # noqa: RUF005
    "bear",  # easy to make custom, but also small
    "commander",
    "outlaw",  # based on creature type
    "party",  # based on creature type
    "reserved",
    "vanilla",
]


def cached(cache: Any, key: Any = None) -> Any:  # noqa: ANN401
    """Decorator that respects the settings.enable_cache flag at runtime.

    Always creates the cached function, but checks settings at call time
    to determine whether to use the cache or call the original function.
    """
    key_maker = key or cachebox.make_hash_key

    def decorator(func: Any) -> Any:  # noqa: ANN401
        cached_func = cachebox_cached(cache, key_maker=key_maker)(func)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if settings.enable_cache:
                return cached_func(*args, **kwargs)
            return func(*args, **kwargs)

        # Copy attributes from cached_func for compatibility
        wrapper.cache = cache  # type: ignore[attr-defined]
        return wrapper

    return decorator


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


class APIResource:
    """Class implementing request handling for our simple API."""

    def __init__(
        self,
        *,
        import_guard: LockType = multiprocessing_utils.DEFAULT_LOCK,
        last_import_time: Synchronized | None = None,
        schema_setup_event: EventType = multiprocessing_utils.DEFAULT_EVENT,
    ) -> None:
        """Initialize an APIResource object, set up connection pool and action map.

        Sets up the database connection pool and action mapping for the API.
        """
        self._bulk_data_fetcher = ScryfallBulkDataFetcher()
        self._conn_pool: psycopg_pool.ConnectionPool = db_utils.make_pool()
        # Create action map with type-converting wrappers for all public methods
        self.action_map = {}
        for method_name in dir(self):
            if method_name.startswith("_"):
                continue
            method = getattr(self, method_name)
            if callable(method):
                self.action_map[method_name] = make_type_converting_wrapper(method)
        self.action_map["index"] = make_type_converting_wrapper(self.index_html)

        # add static file serving actions
        self.action_map["static/app_js"] = self.app_js
        self.action_map["static/app_min_js"] = self.app_min_js
        self.action_map["static/favicon_ico"] = self.favicon_ico
        self.action_map["static/styles_css"] = self.styles_css

        self._query_cache = LRUCache(maxsize=1_000)
        if not settings.enable_cache:
            # cachebox doesn't support ttl=0, so we use a minimal cache when disabled
            self._query_cache = LRUCache(maxsize=1)
        self._session = requests.Session()
        self._import_guard: LockType = import_guard
        self._last_import_time: Synchronized = last_import_time or multiprocessing.Value("d", 0.0, lock=True)
        self._schema_setup_event: EventType = schema_setup_event

        version = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d")
        version = f"magic-api/{version}"
        self._session.headers.update({"User-Agent": version})
        # Initialize Tagger client for GraphQL API access
        self._tagger_client = TaggerClient()
        logger.info("Worker with pid %d has conn pool %s", os.getpid(), self._conn_pool)
        self.setup_schema()
        self.import_data()  # ensures that database is setup

    @cached(cache={}, key=lambda args, kwds: args[1] if len(args) > 1 else kwds.get("filename"))
    def read_sql(self, filename: str) -> str:
        """Read SQL content from a file with caching.

        Args:
            filename: The name of the SQL file (without .sql extension)

        Returns:
            The SQL content as a string
        """
        sql_dir = pathlib.Path(__file__).parent / "sql"
        sql_file = sql_dir / f"{filename}.sql"

        with sql_file.open(encoding="utf-8") as f:
            return f.read().strip()

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

        path = req.path.strip("/") or "index"

        if path in ("db_ready", "pid"):
            return

        logger.info(
            "Handling request for %s / |%s| / response id: %d",
            req.relative_uri,
            path,
            id(resp),
        )
        path = path.replace(".", "_")
        action = self.action_map.get(
            path,
            self._raise_not_found,
        )
        before = time.monotonic()
        try:
            res = action(falcon_response=resp, **req.params)
            resp.media = res
        except TypeError as oops:
            logger.error("Error handling request: %s", oops, exc_info=True)
            raise falcon.HTTPBadRequest(description=str(oops)) from oops
        except falcon.HTTPError as oops:
            logger.error("Error handling request for %s: %s", path, oops, exc_info=True)
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

            raise falcon.HTTPInternalServerError(
                title="Server Error",
                description={
                    "exception": str(oops),
                    "stack_info": stack_info,
                },
            ) from oops
        finally:
            duration = (time.monotonic() - before) * 1000
            logger.info("Request duration: %.1f ms / %s", duration, resp.status)

    def _raise_not_found(self, **_: object) -> None:
        """Raise a Falcon HTTPNotFound error with available routes."""
        routes = {}

        for endpoint_name, wrapped_func in self.action_map.items():
            # Get the original function from the wrapper
            original_func = wrapped_func.__wrapped__ if hasattr(wrapped_func, "__wrapped__") else wrapped_func

            # Get function signature
            sig = inspect.signature(original_func)

            # Extract docstring
            doc = original_func.__doc__ or ""

            # Parse arguments
            args = []
            kwargs = {}

            for param_name, param in sig.parameters.items():
                if param_name.startswith("_"):
                    continue
                if param_name in ("self", "falcon_response"):
                    continue

                param_info = {
                    "name": param_name,
                    "type": _get_type_name(param.annotation),
                }

                if param.default != inspect.Parameter.empty:
                    # It's a keyword argument with default
                    kwargs[param_name] = {
                        "type": _get_type_name(param.annotation),
                        "default": param.default,
                    }
                else:
                    # It's a positional argument
                    args.append(param_info)

            routes[endpoint_name] = {
                "doc": doc,
                "args": args,
                "kwargs": kwargs,
            }

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

    def get_pid(self, **_: object) -> int:
        """Just return the pid of the process which served this request.

        Returns:
        -------
            int: The process ID.

        """
        return os.getpid()

    def db_ready(self, **_: object) -> bool:
        """Return true if the db is ready.

        Returns:
        -------
            bool: True if the database is ready, False otherwise.

        """
        records = self._run_query(
            query="SELECT relname FROM pg_stat_user_tables",
        )["result"]
        existing_tables = {r["relname"] for r in records}
        return "migrations" in existing_tables

    def get_data(self) -> list[dict]:
        """Retrieve card data from cache or Scryfall API.

        Returns:
        -------
            Any: The card data (likely a list of dicts).

        """
        return self._bulk_data_fetcher.get_data_for_key(data_key=BulkDataKey.DEFAULT_CARDS)

    def setup_schema(self, *_: object, **__: object) -> None:
        """Set up the database schema and apply migrations as needed."""
        if self._schema_setup_event.is_set():
            logger.info("Schema already setup (fastpath) in pid %d", os.getpid())
            return

        filesystem_migrations = db_utils.get_migrations()

        with self._import_guard:
            if self._schema_setup_event.is_set():
                logger.info("Schema already setup (slowpath) in pid %d", os.getpid())
                return
            logger.info("Setting up schema in pid %d", os.getpid())
            # read migrations from the db dir...
            # if any already applied migrations differ from what we want
            # to apply then drop everything
            with self._conn_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(
                    """CREATE TABLE IF NOT EXISTS migrations (
                        file_name text not null,
                        file_sha256 text not null,
                        date_applied timestamp default now(),
                        file_contents text not null
                    )""",
                )
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_migrations_filename ON migrations (file_name)")
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_migrations_file_sha256 ON migrations USING HASH (file_sha256)",
                )

                cursor.execute("SELECT file_name, file_sha256 FROM migrations ORDER BY date_applied")
                applied_migrations = [dict(r) for r in cursor]

                already_applied = set()
                for applied_migration, fs_migration in zip(applied_migrations, filesystem_migrations, strict=False):
                    if applied_migration.items() <= fs_migration.items():
                        already_applied.add(applied_migration["file_sha256"])
                    else:
                        already_applied.clear()
                        cursor.execute("DELETE FROM migrations")
                        cursor.execute("DROP SCHEMA IF EXISTS magic CASCADE")
                        conn.commit()

                for imigration in filesystem_migrations:
                    file_sha256 = imigration["file_sha256"]
                    if file_sha256 in already_applied:
                        logger.info("%s was already applied...", imigration["file_name"])
                        continue
                    logger.info("Applying %s ...", imigration["file_name"])
                    cursor.execute(imigration["file_contents"])
                    cursor.execute(
                        """
                            INSERT INTO migrations
                                (  file_name  ,   file_sha256  ,   file_contents  ) VALUES
                                (%(file_name)s, %(file_sha256)s, %(file_contents)s)""",
                        imigration,
                    )
                    conn.commit()

            self._schema_setup_event.set()
            logger.info("Schema setup complete in pid %d", os.getpid())

    def _get_cards_to_insert(self) -> list[dict[str, Any]]:
        """Get the cards to insert into the database.

        For DFCs (Double-Faced Cards), each face is returned as a separate dictionary.
        The deduplication key is (scryfall_id, face_idx) to support multiple faces per printing.
        """
        all_cards = self.get_data()
        key_to_card: dict[tuple[str, int], dict[str, Any]] = {}
        for card in all_cards:
            processed_cards = preprocess_card(card)
            for processed_card in processed_cards:
                scryfall_id = processed_card["scryfall_id"]
                face_idx = processed_card["face_idx"]
                key_to_card[(scryfall_id, face_idx)] = processed_card
        return list(key_to_card.values())

    def get_stats(self, **_: object) -> dict[str, Any]:
        """Get stats about the cards."""
        to_insert = self._get_cards_to_insert()
        key_frequency = collections.Counter()
        for card in to_insert:
            key_frequency.update(k for k, v in card.items() if v not in [None, [], {}])
        return key_frequency.most_common()

    @cached(cache=TTLCache(maxsize=1, ttl=60), key=lambda _args, _kwds: None)
    def _setup_complete(self) -> True:
        """Return True if the setup is complete."""
        try:
            with self._conn_pool.connection() as conn:
                conn = typecast("Connection", conn)
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(1) AS num_cards FROM magic.cards")
                    cards_found = cursor.fetchall()[0]["num_cards"]
                    logger.info("Found %d cards in pid %d", cards_found, os.getpid())
                    return cards_found > MIN_IMPORT_CARDS
        except Exception as oops:
            logger.error("Error checking if setup is complete: %s", oops, exc_info=True)
            return False

    def _import_recent(self) -> bool:
        """Return True if a bulk import completed in the last 5 minutes (or setup is complete when no shared timestamp)."""
        if self._last_import_time is None:
            return self._setup_complete()
        # Unlocked read: c_double is atomic on typical platforms; avoids lock contention on fast path
        t = self._last_import_time.get_obj().value
        if not t:
            logger.info("No import recorded...")
            return False
        time_since_import = time.time() - t
        retval = time_since_import < MIN_IMPORT_INTERVAL
        logger.info("Last import was %d seconds ago, %s", time_since_import, retval)
        return retval

    def _run_import_under_lock(self) -> None:
        """Run the import flow; caller must hold the import lock."""
        if self._import_recent():
            logger.info("Import recent slowpath...")
            return None
        self.setup_schema()

        to_insert = self._get_cards_to_insert()

        before = time.monotonic()

        result = self._load_cards_with_staging(to_insert)

        after_transfer = time.monotonic()

        if result["status"] == "success":
            if self._last_import_time is not None:
                self._last_import_time.value = time.time()
            total_time = after_transfer - before
            rate = len(to_insert) / total_time if total_time > 0 else 0
            logger.info(
                "Loaded %d cards in %.2f seconds, rate: %.2f cards/s...",
                result["cards_loaded"],
                total_time,
                rate,
            )
            self.backfill_prefer_scores()
            self.backfill_cubecobra_scores()
            self._last_import_time.value = time.time()
            return result["sample_cards"]
        logger.error("Failed to import data: %s", result["message"])
        return None

    @cached(
        cache=TTLCache(maxsize=1, ttl=MIN_IMPORT_INTERVAL),
        key=lambda _args, _kwds: None,
    )
    def import_data(self, **_: object) -> None:
        """Import data from Scryfall and insert into the database."""
        before = time.monotonic()
        if self._import_recent():
            after = time.monotonic()
            total_time = after - before
            logger.info("Import recent fastpath took %.2f seconds in pid %d", total_time, os.getpid())
            # check without taking the lock so the majority of the time we never take the lock
            return None

        logger.info("Hitting slowpath in pid %d", os.getpid())

        import_lock = self._last_import_time.get_lock()

        acquired = import_lock.acquire(timeout=IMPORT_LOCK_TIMEOUT)
        if not acquired:
            if self._setup_complete():
                logger.info(
                    "Timed out waiting %.0fs for import lock; setup complete, skipping in pid %d",
                    IMPORT_LOCK_TIMEOUT,
                    os.getpid(),
                )
                return None
            # acquire with no timeout...
            import_lock.acquire()
        try:
            return self._run_import_under_lock()
        finally:
            import_lock.release()

    def search(  # noqa: PLR0913
        self,
        *,
        falcon_response: falcon.Response | None = None,
        # search parameters
        direction: SortDirection = SortDirection.ASC,
        limit: int = 100,
        orderby: CardOrdering = CardOrdering.EDHREC,
        prefer: PreferOrder = PreferOrder.DEFAULT,
        q: str | None = None,
        query: str | None = None,
        unique: UniqueOn = UniqueOn.CARD,
    ) -> dict[str, Any]:
        """Run a search query and return results and metadata.

        Args:
            falcon_response: The Falcon response object (unused).
            q: Query string (alternative to query parameter).
            query: Query string (alternative to q parameter).
            direction: Sort direction ('asc' or 'desc').
            limit: Maximum number of results to return.
            orderby: Field to sort by.
            unique: Unique on field.
            prefer: Prefer order (oldest, newest, usd-low, usd-high, promo).

        Returns:
            Dict containing search results and metadata.
        """
        set_cache_header(falcon_response, duration=timedelta(seconds=90))
        return self._search(
            query=query or q,
            orderby=orderby,
            direction=direction,
            limit=limit,
            unique=unique,
            prefer=prefer,
        )

    @cached(
        cache=TTLCache(maxsize=1000, ttl=60),
        key=lambda args, kwds: (args, tuple(sorted(kwds.items()))),
    )
    def _search(  # noqa: PLR0913
        self,
        *,
        direction: SortDirection = SortDirection.ASC,
        limit: int = 100,
        orderby: CardOrdering = CardOrdering.EDHREC,
        prefer: PreferOrder = PreferOrder.DEFAULT,
        query: str | None = None,
        unique: UniqueOn = UniqueOn.CARD,
    ) -> dict[str, Any]:
        if not self._setup_complete():
            raise falcon.HTTPServiceUnavailable(
                title="Service Unavailable",
                description="Setup is not complete, please try again later.",
            ) from None

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

        timer = Timer()

        try:
            with timer("get_where_clause"):
                where_clause, params = get_where_clause(query)
        except ValueError as err:
            # Handle parsing errors from parse_scryfall_query
            logger.info("ValueError caught for query '%s', raising BadRequest", query)
            raise falcon.HTTPBadRequest(
                title="Invalid Search Query",
                description=f'Failed to parse query: "{query}"',
            ) from err
        sql_orderby: str = {
            # what's in the query => the db column name
            CardOrdering.CMC: "cmc",
            CardOrdering.EDHREC: "edhrec_rank",
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
        # scryfall supports distinct:
        # cards, prints, arts
        distinct_on = {
            UniqueOn.ARTWORK: "illustration_id",
            UniqueOn.CARD: "card_name",
            UniqueOn.PRINTING: "scryfall_id",
        }.get(unique, "card_name")
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
        query_sql = f"""
            WITH distinct_cards AS (
                SELECT DISTINCT ON ({distinct_on})
                    card_artist,
                    card_name,
                    card_set_code,
                    cmc,
                    collector_number,
                    creature_power_text,
                    creature_toughness_text,
                    edhrec_rank,
                    mana_cost_text,
                    oracle_text,
                    set_name,
                    type_line,
                    prefer_score,
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
                    null AS total_cards_count,
                    card_artist,
                    card_name AS name,
                    card_set_code AS set_code,
                    cmc,
                    collector_number,
                    creature_power_text AS power,
                    creature_toughness_text AS toughness,
                    edhrec_rank,
                    mana_cost_text AS mana_cost,
                    oracle_text,
                    set_name,
                    type_line
                FROM
                    distinct_cards
                ORDER BY
                    sort_value {sql_direction} NULLS LAST,
                    edhrec_rank ASC NULLS LAST,
                    prefer_score DESC NULLS LAST
                LIMIT
                    %(limit)s
            )
            UNION ALL
            (
                SELECT
                    COUNT(1) AS total_cards_count,
                    null, null, null, null, null, null, null, null, null, null, null, null
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
            "outer_timings": timer.get_timings(),
            "inner_timings": result_bag.pop("timings"),
            "total_cards": total_cards,
        }

    def index_html(  # noqa: PLR0913
        self,
        *,
        falcon_response: falcon.Response | None = None,
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
            q (str): Search query (alternative to query parameter).
            query (str): Search query (alternative to q parameter).
            orderby (CardOrdering): Field to sort by.
            direction (SortDirection): Sort direction.
            unique (UniqueOn): Unique on field.
            prefer (PreferOrder): Prefer order.

        """
        # Read the HTML file
        full_filename = pathlib.Path(__file__).parent / "static" / "index.html"
        with pathlib.Path(full_filename).open() as f:
            html_content = f.read()

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

                # Inject the results count with proper display style
                if results_count_html:
                    html_content = html_content.replace(
                        '<div id="resultsCount" class="results-count" style="display: none"><!-- SERVER_SIDE_RESULTS_COUNT --></div>',
                        f'<div id="resultsCount" class="results-count" style="display: block">{results_count_html}</div>',
                    )

                # Convert search results to JSON and embed for JavaScript enhancement
                search_results_json = orjson.dumps(search_results, option=orjson.OPT_INDENT_2).decode("utf-8")
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

    def prefer_score_tuner(self, *, falcon_response: falcon.Response | None = None, **_: object) -> None:
        """Return the prefer score tuner page.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.

        """
        self._serve_static_file(filename="prefer_score_tuner.html", falcon_response=falcon_response)
        falcon_response.content_type = "text/html"

    def favicon_ico(self, *, falcon_response: falcon.Response | None = None) -> None:
        """Return the favicon.ico file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        full_filename = pathlib.Path(__file__).parent / "static" / "favicon.ico"
        with pathlib.Path(full_filename).open(mode="rb") as f:
            falcon_response.data = contents = f.read()
        falcon_response.content_type = "image/vnd.microsoft.icon"
        content_length = len(contents)
        logger.info("Favicon content length: %d", content_length)
        falcon_response.headers["content-length"] = content_length
        # Cache favicon for 7 days - it rarely changes
        set_cache_header(falcon_response, duration=timedelta(days=7))

    def styles_css(self, *, falcon_response: falcon.Response | None = None) -> None:
        """Return the styles.css file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        self._serve_static_file(filename="styles.css", falcon_response=falcon_response)
        falcon_response.content_type = "text/css"
        # Cache CSS for 1 hour - it changes infrequently
        set_cache_header(falcon_response, duration=timedelta(hours=1))

    def app_js(self, *, falcon_response: falcon.Response | None = None) -> None:
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

    def app_min_js(self, *, falcon_response: falcon.Response | None = None) -> None:
        """Return the app.min.js file.

        Args:
        ----
            falcon_response (falcon.Response): The Falcon response to write to.
        """
        if falcon_response is None:
            return
        self._serve_static_file(filename="app.min.js", falcon_response=falcon_response)
        falcon_response.content_type = "application/javascript"
        # Cache minified JavaScript for 1 hour - it changes infrequently
        set_cache_header(falcon_response, duration=timedelta(hours=1))

    def _serve_static_file(self, *, filename: str, falcon_response: falcon.Response) -> None:
        """Serve a static file to the Falcon response.

        Args:
        ----
            filename (str): The file to serve.
            falcon_response (falcon.Response): The Falcon response to write to.

        """
        full_filename = pathlib.Path(__file__).parent / "static" / filename
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

    def get_migrations(self, **_: object) -> list[dict[str, str]]:
        """Get the migrations from the filesystem.

        Returns:
        -------
            List[Dict[str, str]]: List of migration metadata dictionaries.

        """
        return db_utils.get_migrations()

    def get_common_card_types(
        self,
        falcon_response: falcon.Response | None = None,
        **_: object,
    ) -> list[dict[str, Any]]:
        """Get the common card types from the database."""
        set_cache_header(falcon_response, duration=timedelta(hours=1))
        return self._run_query(
            query=self.read_sql("get_common_card_types"),
        )["result"]

    def get_common_keywords(self, **_: object) -> list[dict[str, Any]]:
        """Get the common keywords from the database."""
        return self._run_query(
            query=self.read_sql("get_common_keywords"),
        )["result"]

    def backfill_prefer_scores(self, **_: object) -> dict[str, Any]:
        """Backfill prefer_score and prefer_score_components for all cards.

        This endpoint recalculates the prefer score for all existing cards based on:
        - Border color (black: 14, white: 0)
        - Frame version (2015: 42, 2003: 30)
        - Artwork popularity (logarithmic scaling: 23 * ln(count) / ln(40))
        - Rarity (common: 16, uncommon: 16, rare: 11, mythic: 0)
        - Extended art (12 points if present)
        - Highres scan (8 points if image_status='highres_scan')
        - Has paper (6 points if 'paper' in games array)
        - Language (English: 40 points)
        - Legendary frame (5 points if 'legendary' in frame_effects)
        - Non-showcase (10 points if 'showcase' not in frame_effects)
        - Finish (nonfoil: 10, foil: 5, etched: 0)
        - Artwork set (full-color: 20, black/white: 0)

        Returns:
            Dict with status and count of cards updated
        """
        logger.info("Starting prefer score backfill")

        backfill_sql = self.read_sql("backfill_prefer_scores")
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            statement_timeout = 60_000
            # Validate and set statement timeout
            self._set_statement_timeout(cursor, statement_timeout)
            cursor.execute(backfill_sql)

            # Get count of updated cards
            cursor.execute("SELECT COUNT(*) as count FROM magic.cards WHERE prefer_score IS NOT NULL")
            result = cursor.fetchone()
            count = result["count"] if result else 0

            conn.commit()

        logger.info("Prefer score backfill complete: %d cards updated", count)

        return {
            "status": "success",
            "cards_updated": count,
            "message": f"Successfully backfilled prefer scores for {count} cards",
        }

    def _fetch_cubecobra_data(self, db_oracle_ids: set[str]) -> dict[str, dict[str, Any]]:
        """Paginate the CubeCobra top-cards API and return data keyed by oracle_id.

        Returns:
            Mapping of oracle_id -> {elo, cube_count, pick_count, popularity}.
        """
        cubecobra_url = "https://cubecobra.com/tool/api/topcards/"
        page = 1

        while True:
            time.sleep(0.5)
            logger.info("Fetching CubeCobra page %d", page)
            response = self._session.get(
                cubecobra_url,
                params={"p": page, "f": "", "s": "Elo", "d": "descending"},
                timeout=30,
            )
            response.raise_for_status()
            cards = response.json().get("data") or []

            if not cards:
                logger.info("Empty page %d - done paginating CubeCobra", page)
                break

            results: dict[str, dict[str, Any]] = {}
            for card in cards:
                oracle_id = card.get("oracle_id")
                if oracle_id in db_oracle_ids:
                    results[oracle_id] = {
                        "elo": card.get("elo"),
                        "cube_count": card.get("cubeCount"),
                        "pick_count": card.get("pickCount"),
                    }

            logger.info("CubeCobra page %d: %d cards (total: %d)", page, len(cards), len(results))
            page += 1
            yield results

    def _insert_cubecobra_data(self, cubecobra_data: dict[str, dict[str, Any]]) -> int:
        """Write CubeCobra data into magic.cards, matching on oracle_id.

        Args:
            cubecobra_data: Mapping of oracle_id -> data dict from _fetch_cubecobra_data().

        Returns:
            Total number of card rows updated.
        """
        records = db_utils.maybe_json(
            [
                {
                    "elo": data["elo"],
                    "cube_count": data["cube_count"],
                    "pick_count": data["pick_count"],
                    "oracle_id": oracle_id,
                }
                for oracle_id, data in cubecobra_data.items()
            ]
        )

        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                WITH incoming AS (
                    SELECT * FROM jsonb_to_recordset(%(records)s) AS t(
                        elo real, cube_count integer, pick_count integer, oracle_id uuid
                    )
                )
                UPDATE magic.cards
                SET
                    cubecobra_elo        = incoming.elo,
                    cubecobra_cube_count = incoming.cube_count,
                    cubecobra_pick_count = incoming.pick_count
                FROM incoming
                WHERE magic.cards.oracle_id = incoming.oracle_id
                """,
                {"records": records},
            )
            total_updated = cursor.rowcount
            conn.commit()

        return total_updated

    def backfill_cubecobra_scores(self, **_: object) -> dict[str, Any]:
        """Backfill cubecobra_score for all cards.

        Computes a weighted average of per-dimension PERCENT_RANK values (each in the 0-1
        range, where 0 is best and 1 is worst) and scales the result to a 0-100 score
        (0 = best, 100 = worst).

        The per-dimension weights are treated as relative and are internally normalized so
        that their sum is 100. Callers may supply any non-negative weights; they do not need
        to sum to 1.0.

        One score per distinct card_name is computed and then propagated to all printings.

        Returns:
            Dict with status and count of cards updated.
        """
        weights = {
            "w_cube_count": 1,
            "w_edhrec": 1,
            "w_elo": 1,
            "w_pick_count": 1,
        }
        scale_factor = sum(weights.values()) / 100.0
        weights = {k: v / scale_factor for k, v in weights.items()}
        logger.info("Starting CubeCobra score backfill with weights: %s", weights)

        backfill_sql = self.read_sql("backfill_cubecobra_scores")
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            self._set_statement_timeout(cursor, 60_000)
            cursor.execute(backfill_sql, weights)
            updated_count = cursor.rowcount
            conn.commit()

        logger.info("CubeCobra score backfill complete: %d cards updated", updated_count)
        return {
            "status": "success",
            "cards_updated": updated_count,
            "weights": weights,
        }

    def ingest_cubecobra(self, **_: object) -> dict[str, Any]:
        """Fetch card data from CubeCobra and store it in magic.cards.

        Paginates the CubeCobra top-cards API, then updates all matching rows
        in magic.cards (matched on oracle_id). Cards not present in CubeCobra
        are left with NULL values for the cubecobra_* columns.

        Returns:
            Dict with status and count of rows updated.
        """
        logger.info("Starting CubeCobra ingest")
        # fetch the distinct, non-null oracle ids that are in the db
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT oracle_id FROM magic.cards WHERE oracle_id IS NOT NULL",
            )
            db_oracle_ids = {r["oracle_id"] for r in cursor.fetchall()}

        for cubecobra_page in self._fetch_cubecobra_data(db_oracle_ids):
            logger.info("Fetched %d oracle_ids from CubeCobra", len(cubecobra_page))
            cards_updated = self._insert_cubecobra_data(cubecobra_page)
        logger.info("CubeCobra ingest complete: %d card rows updated", cards_updated)

        backfill_result = self.backfill_cubecobra_scores()

        return {
            "status": "success",
            "cards_updated": cards_updated,
            "scores_backfilled": backfill_result["cards_updated"],
        }

    def update_tagged_cards(
        self,
        *,
        tag: str,
        **_: object,
    ) -> dict[str, Any]:
        """Update cards with a specific Scryfall tag.

        Args:
        ----
            tag (str): The Scryfall tag to fetch and apply to cards.

        Returns:
        -------
            Dict[str, Any]: Result summary with updated card count and tag info.

        """
        if not tag:
            msg = "Tag parameter is required"
            raise ValueError(msg)

        # Fetch cards with this tag from Scryfall API (handles pagination)
        cards = self._scryfall_search(query=f"oracletag:{tag}")
        card_names = {c["name"] for c in cards}

        if not cards:
            return {
                "tag": tag,
                "cards_updated": 0,
                "message": f"No cards found with tag '{tag}' in Scryfall API",
            }

        logger.info("Updating %d cards with tag '%s'", len(card_names), tag)
        # Update cards in database with the new tag
        updated_count = 0
        new_tag = orjson.dumps({tag: True}).decode("utf-8")
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            # Use SQL update with jsonb concatenation to add the tag
            for card_name_batch in itertools.batched(sorted(card_names), 500):
                cursor.execute(
                    """
                    UPDATE
                        magic.cards
                    SET
                        card_oracle_tags = card_oracle_tags || %(new_tag)s::jsonb
                    WHERE
                        card_name = ANY(%(card_names)s) AND
                        not(card_oracle_tags @> %(new_tag)s::jsonb)
                    """,
                    {
                        "card_names": list(card_name_batch),
                        "new_tag": new_tag,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()

        return {
            "tag": tag,
            "cards_updated": updated_count,
            "total_cards_found": len(card_names),
            "message": f"Successfully updated {updated_count} cards with tag '{tag}'",
        }

    def _add_is_tag_to_cards_or_printings(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all cards or printings matching that tag using Scryfall search.

        Args:
        ----
            is_tag (str): The is: tag to fetch and apply to cards (e.g., 'creature', 'spell').

        Returns:
        -------
            Dict[str, Any]: Result summary with updated card count and tag info.

        """
        # TODO: is tags are not based on card name, but rather specific printing
        # meaning this needs to not use unique on cards, but instead do unique printing
        # which means it's gonna be hella slow

        if not is_tag:
            msg = "is_tag parameter is required"
            raise ValueError(msg)

        if is_tag in CUSTOM_IS_TAGS:
            return self._add_is_tag_to_custom(is_tag=is_tag)
        if is_tag in CARD_IS_TAGS:
            return self._add_is_tag_to_cards(is_tag=is_tag)
        return self._add_is_tag_to_printings(is_tag=is_tag)

    def _add_is_tag_to_custom(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all custom cards matching that tag using Scryfall search."""
        # these are special cases where you can phrase the tag as a query over other properties
        logger.info("Adding is:%s to custom cards", is_tag)
        return {
            "cards_updated": 0,
            "is_tag": is_tag,
            "message": f"Custom is: tag {is_tag} is not supported",
            "total_cards_found": 0,
        }

    def _add_is_tag_to_cards(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all cards matching that tag using Scryfall search.

        Args:
        ----
            is_tag (str): The is: tag to fetch and apply to cards (e.g., 'creature', 'spell').

        Returns:
        -------
            Dict[str, Any]: Result summary with updated card count and tag info.

        """
        # Fetch cards with this is: tag from Scryfall API (handles pagination)
        cards = self._scryfall_search(query=f"is:{is_tag}", unique="cards")
        card_names = {c["name"] for c in cards}

        if not cards:
            logger.warning("No cards found with is:%s in Scryfall API", is_tag)
            return {
                "is_tag": is_tag,
                "cards_updated": 0,
                "total_cards_found": 0,
                "message": f"No cards found with is:{is_tag} in Scryfall API",
            }

        logger.info("Updating %d cards with is:%s", len(card_names), is_tag)
        # Update cards in database with the new is: tag
        updated_count = 0
        new_tag = orjson.dumps({is_tag: True}).decode("utf-8")
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            # Use SQL update with jsonb concatenation to add the is: tag
            for card_name_batch in itertools.batched(sorted(card_names), 500):
                cursor.execute(
                    """
                    UPDATE
                        magic.cards
                    SET
                        card_is_tags = card_is_tags || %(new_tag)s::jsonb
                    WHERE
                        card_name = ANY(%(card_names)s) AND
                        not(card_is_tags @> %(new_tag)s::jsonb)
                    """,
                    {
                        "card_names": list(card_name_batch),
                        "new_tag": new_tag,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()

        return {
            "is_tag": is_tag,
            "cards_updated": updated_count,
            "total_cards_found": len(card_names),
            "message": f"Successfully updated {updated_count} cards with is:{is_tag}",
        }

    def _add_is_tag_to_printings(self, *, is_tag: str) -> dict[str, Any]:
        """Add a specific is: tag to all printings matching that tag using Scryfall search.

        Args:
        ----
            is_tag (str): The is: tag to fetch and apply to printings (e.g., 'creature', 'spell').

        Returns:
        -------
            Dict[str, Any]: Result summary with updated card count and tag info.

        """
        # Fetch cards with this is: tag from Scryfall API (handles pagination)
        printings = self._scryfall_search(query=f"is:{is_tag}", unique="printings")

        if not printings:
            logger.warning("No printings found with is:%s in Scryfall API", is_tag)
            return {
                "is_tag": is_tag,
                "cards_updated": 0,
                "total_cards_found": 0,
                "message": f"No cards found with is:{is_tag} in Scryfall API",
            }

        logger.info("Updating %d printings with is:%s", len(printings), is_tag)
        # Update cards in database with the new is: tag
        updated_count = 0
        new_tag = orjson.dumps({is_tag: True}).decode("utf-8")
        scryfall_ids = {p["id"] for p in printings}
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            # Use SQL update with jsonb concatenation to add the is: tag
            for scryfall_id_batch in itertools.batched(sorted(scryfall_ids), 500):
                cursor.execute(
                    """
                    UPDATE
                        magic.cards
                    SET
                        card_is_tags = card_is_tags || %(new_tag)s::jsonb
                    WHERE
                        scryfall_id = ANY(%(scryfall_ids)s) AND
                        not(card_is_tags @> %(new_tag)s::jsonb)
                    """,
                    {
                        "scryfall_ids": list(scryfall_id_batch),
                        "new_tag": new_tag,
                    },
                )
                updated_count += cursor.rowcount
                conn.commit()

        return {
            "is_tag": is_tag,
            "cards_updated": updated_count,
            "total_cards_found": len(scryfall_ids),
            "message": f"Successfully updated {updated_count} printings with is:{is_tag}",
        }

    def discover_tags_from_scryfall(self, **_: object) -> list[str]:
        """Discover all available tags from Scryfall tagger documentation.

        Returns:
        -------
            List[str]: List of all available tag names.

        Raises:
        ------
            ValueError: If API request fails or returns invalid data.

        """
        try:
            response = self._session.get("https://scryfall.com/docs/tagger-tags", timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            msg = f"Failed to fetch tag list from Scryfall: {e}"
            raise ValueError(msg) from e

        # Extract tag names from oracletag search links
        oracletag_pattern = r'/search\?q=oracletag%3A([^"&]+)'
        matches = re.findall(oracletag_pattern, response.text)

        # URL decode the tag names and remove duplicates
        unique_tags = sorted({urllib.parse.unquote(match) for match in matches})

        logger.info("Discovered %d unique tags from Scryfall", len(unique_tags))
        return unique_tags

    def discover_tags_from_graphql(self, **_: object) -> list[str]:
        """Discover all available tags from Scryfall tagger using GraphQL API.

        This method uses the SearchTags GraphQL query to fetch all available tags.
        It paginates through all pages to get the complete list.

        Returns:
        -------
            List[str]: List of all available tag slugs.

        Raises:
        ------
            ValueError: If GraphQL request fails or returns invalid data.

        """
        tags = set()
        page = 1

        try:
            while True:
                # Fetch tags for current page
                result = self._tagger_client.search_tags(page=page)
                results = result["results"]
                if not results:
                    break

                # Extract tag slugs from results
                ignored_namespaces = ["artwork", "print"]
                tags.update(tag["slug"] for tag in results if tag["namespace"] not in ignored_namespaces)
                non_artwork_tags = [tag for tag in results if tag["namespace"] not in ignored_namespaces]
                logger.info("Discovered %d tags from GraphQL: %s", len(tags), non_artwork_tags)
                page += 1
        except (KeyError, TypeError, ValueError) as e:
            msg = f"Failed to parse GraphQL tag search response: {e}"
            raise ValueError(msg) from e

        # Remove duplicates and sort
        unique_tags = sorted(tags)
        logger.info("Discovered %d unique tags from GraphQL", len(unique_tags))
        return unique_tags

    def discover_is_tags_from_syntax(self, **_: object) -> list[str]:
        """Discover all available is: tags from Scryfall syntax documentation.

        Returns:
        -------
            List[str]: List of all available is: tag names.

        Raises:
        ------
            ValueError: If API request fails or returns invalid data.

        """
        try:
            response = self._session.get("https://scryfall.com/docs/syntax", timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            msg = f"Failed to fetch is: tags from Scryfall syntax: {e}"
            raise ValueError(msg) from e

        # Extract is: tag names from the documentation
        # Look for patterns like "is:permanent", "is:spell", etc.
        is_tag_pattern = r"is:([a-zA-Z_-]+)"
        matches = re.findall(is_tag_pattern, response.text)

        # Remove duplicates and sort
        unique_is_tags = sorted({match.lower() for match in matches})

        logger.info("Discovered %d unique is: tags from Scryfall syntax", len(unique_is_tags))
        return unique_is_tags

    def _get_tag_relationships(self, *, tag: str) -> list[dict[str, str]]:
        """Fetch list of relationships for a specific tag using Scryfall tagger GraphQL API.

        Args:
        ----
            tag (str): The tag to get relationships information for.

        Returns:
        -------
            list[dict]: List of relationships for the tag.
        """
        logger.info("Fetching relationships for %s", tag)

        def clean_tag(itag: dict) -> dict:
            return {
                "name": itag["name"],
                "namespace": itag["namespace"],
                "slug": itag["slug"],
            }

        # Use GraphQL API to get tag metadata including hierarchy
        relationships = []
        try:
            tag_data = self._tagger_client.fetch_tag(tag, include_taggings=False)
        except ValueError:
            return relationships
        ancestry = [clean_tag(parent["tag"]) for parent in tag_data.pop("ancestry") if parent.get("tag")]
        children = [clean_tag(tag) for tag in tag_data.pop("childTags")]
        tag_data = clean_tag(tag_data)

        for parent in ancestry:
            relationships.append(
                {
                    "parent": parent,
                    "child": tag_data,
                },
            )
        for child in children:
            relationships.append(
                {
                    "parent": tag_data,
                    "child": child,
                },
            )
        # remove the relationships where the parent and child are the same
        return [relationship for relationship in relationships if relationship["parent"]["slug"] != relationship["child"]["slug"]]

    def _populate_tag_hierarchy(self, *, tags: list[str]) -> dict[str, Any]:
        """Populate the tag hierarchy table with discovered tags.

        Args:
        ----
            tags (List[str]): List of tag names to process.

        Returns:
        -------
            Dict[str, Any]: Summary of the operation.

        """
        logger.info("Populating tag hierarchy")
        start_time = time.monotonic()
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            tags_in_random_order = list(tags)
            random.shuffle(tags_in_random_order)

            for idx, tag in enumerate(tags_in_random_order):
                if idx:
                    elapsed_time = time.monotonic() - start_time
                    fraction_complete = idx / len(tags_in_random_order)
                    estimated_time_remaining = (elapsed_time / fraction_complete) - elapsed_time
                    estimated_duration = datetime.timedelta(seconds=round(estimated_time_remaining, 1))
                else:
                    estimated_duration = "N/A"
                logger.info(
                    "Processing tag %d of %d: %20s (ETA: %s)",
                    idx + 1,
                    len(tags_in_random_order),
                    tag,
                    estimated_duration,
                )

                relationships = self._get_tag_relationships(tag=tag)

                parent_tags = {r["parent"]["slug"] for r in relationships}
                child_tags = {r["child"]["slug"] for r in relationships}
                all_tags = parent_tags | child_tags

                # record existence of all tags
                cursor.executemany(
                    """
                    INSERT INTO magic.tags (tag)
                    VALUES (%(tag)s)
                    ON CONFLICT (tag) DO NOTHING
                    """,
                    [{"tag": slug} for slug in all_tags],
                )

                cursor.executemany(
                    """
                    INSERT INTO magic.tag_relationships
                        (child_tag, parent_tag)
                    VALUES
                        (%(child_tag)s, %(parent_tag)s)
                    ON CONFLICT (child_tag, parent_tag)
                    DO NOTHING
                    """,
                    [
                        {
                            "child_tag": r["child"]["slug"],
                            "parent_tag": r["parent"]["slug"],
                        }
                        for r in relationships
                    ],
                )
                conn.commit()

        return {
            "duration": time.monotonic() - start_time,
            "message": "Tag hierarchy populated successfully",
            "success": True,
            "tags_processed": len(tags_in_random_order),
        }

    def discover_and_import_all_tags(
        self,
        *,
        import_cards: bool = True,
        import_hierarchy: bool = False,
        **_: object,
    ) -> dict[str, Any]:
        """Discover all Scryfall tags and optionally import their card associations.

        Args:
        ----
            import_cards (bool): Whether to import card associations for each tag.
            import_hierarchy (bool): Whether to discover and import tag hierarchy.

        Returns:
        -------
            Dict[str, Any]: Summary of the bulk import operation.

        """
        # Step 1: Discover all available tags
        logger.info("discover_and_import_all_tags: %s", locals())
        result: dict = {
            "success": True,
        }
        logger.info("Starting bulk tag discovery and import")
        try:
            all_tags = self.discover_tags_from_scryfall()
        except ValueError as e:
            result.update(
                {
                    "success": False,
                    "error": str(e),
                    "message": "Failed to discover tags from Scryfall",
                },
            )
            return result

        if not all_tags:
            return {
                "success": False,
                "message": "No tags discovered from Scryfall",
            }

        # Step 2: Import tag hierarchy if requested
        if import_hierarchy:
            result["hierarchy"] = self._populate_tag_hierarchy(tags=all_tags)

        # Step 3: Import card associations for each tag if requested
        if import_cards:
            result["card_taggings"] = self._update_all_card_taggings()

        return result

    def import_all_is_tags(self, **_: object) -> dict[str, Any]:
        """Discover and import all is: tags from Scryfall syntax documentation.

        Returns:
        -------
            Dict[str, Any]: Summary of the bulk is: tag import operation.

        """
        result: dict[str, Any] = {
            "success": True,
        }
        logger.info("Starting bulk is: tag discovery and import")

        try:
            all_is_tags = self.discover_is_tags_from_syntax()
        except ValueError as e:
            result.update(
                {
                    "success": False,
                    "error": str(e),
                    "message": "Failed to discover is: tags from Scryfall syntax",
                },
            )
            return result

        if not all_is_tags:
            return {
                "success": False,
                "message": "No is: tags discovered from Scryfall syntax",
            }

        # Import card associations for each is: tag
        start_time = time.monotonic()
        imported_tags = []
        failed_tags = []
        total_cards_updated = 0

        for idx, is_tag in enumerate(all_is_tags):
            try:
                if idx > 0:
                    elapsed_time = time.monotonic() - start_time
                    fraction_complete = idx / len(all_is_tags)
                    estimated_time_remaining = (elapsed_time / fraction_complete) - elapsed_time
                    estimated_duration = datetime.timedelta(seconds=round(estimated_time_remaining, 1))
                    logger.info(
                        "Importing is: tag %d of %d: %20s (ETA: %s)",
                        idx + 1,
                        len(all_is_tags),
                        is_tag,
                        estimated_duration,
                    )

                tag_result = self._add_is_tag_to_cards_or_printings(is_tag=is_tag)
                imported_tags.append(
                    {
                        "is_tag": is_tag,
                        "cards_updated": tag_result["cards_updated"],
                        "total_cards_found": tag_result["total_cards_found"],
                    },
                )
                total_cards_updated += tag_result["cards_updated"]

            except ValueError as e:
                logger.warning("Failed to import is: tag '%s': %s", is_tag, e)
                failed_tags.append({"is_tag": is_tag, "error": str(e)})

        result.update(
            {
                "duration": time.monotonic() - start_time,
                "discovered_is_tags": len(all_is_tags),
                "imported_is_tags": len(imported_tags),
                "failed_is_tags": len(failed_tags),
                "total_cards_updated": total_cards_updated,
                "imported_tags": imported_tags,
                "failed_tags": failed_tags,
                "message": f"Successfully imported {len(imported_tags)} is: tags, {len(failed_tags)} failed",
            },
        )

        return result

    def _update_all_card_taggings(self) -> dict[str, Any]:
        """Update all card taggings."""
        logger.info("Updating all card taggings")
        tags = self._get_all_tags()
        start_time = time.monotonic()
        for idx, tag in enumerate(tags):
            if idx:
                elapsed_time = time.monotonic() - start_time
                fraction_complete = idx / len(tags)
                estimated_time_remaining = (elapsed_time / fraction_complete) - elapsed_time
                estimated_duration = datetime.timedelta(seconds=round(estimated_time_remaining, 1))
                logger.info("Updating tag %d of %d: %20s (ETA: %s)", idx + 1, len(tags), tag, estimated_duration)
            self.update_tagged_cards(tag=tag)
        return {
            "duration": time.monotonic() - start_time,
            "message": "All card taggings updated successfully",
            "success": True,
            "tags_processed": len(tags),
        }

    def _get_all_tags(self) -> set[str]:
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT tag FROM magic.tags")
            return {r["tag"] for r in cursor.fetchall()}

    def import_card_by_name(
        self,
        *,
        card_name: str,
        **_: object,
    ) -> dict[str, Any]:
        """Import a single card by name from Scryfall API.

        Args:
        ----
            card_name (str): The exact name of the card to import.

        Returns:
        -------
            Dict[str, Any]: Result summary with import status and card info.

        """
        if not card_name:
            msg = "card_name parameter is required"
            raise ValueError(msg)

        logger.info("Importing card by name: '%s'", card_name)

        # Check if card already exists in database for backward compatibility
        existing_check = self._run_query(
            query="SELECT card_name FROM magic.cards WHERE card_name = %(card_name)s",
            params={"card_name": card_name},
            explain=False,
        )

        if existing_check["result"]:
            return {
                "card_name": card_name,
                "status": "already_exists",
                "message": f"Card '{card_name}' already exists in database",
            }

        # Use import_cards_by_search with exact name query
        return self.import_cards_by_search(search_query=f'!"{card_name}"')

    def import_cards_by_search(
        self,
        *,
        search_query: str,
        **_: object,
    ) -> dict[str, Any]:
        """Import cards from Scryfall API using any search query.

        Args:
        ----
            search_query (str): The Scryfall search query to execute.

        Returns:
        -------
            Dict[str, Any]: Result summary with import status and card info.

        """
        if not search_query:
            msg = "search_query parameter is required"
            raise ValueError(msg)

        logger.info("Importing cards by search: '%s'", search_query)

        # Fetch card data from Scryfall API using the provided search query
        try:
            cards = self._scryfall_search(query=search_query)
            if not cards:
                return {
                    "search_query": search_query,
                    "status": "not_found",
                    "message": f"No cards found for search query '{search_query}' in Scryfall API",
                    "cards_loaded": 0,
                    "sample_cards": [],
                }

        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error("Error fetching cards for search '%s' from Scryfall: %s", search_query, e)
            return {
                "search_query": search_query,
                "status": "error",
                "message": f"Error fetching cards from Scryfall: {e}",
                "cards_loaded": 0,
                "sample_cards": [],
            }

        # Insert the cards into the database using the consolidated method
        load_result = self._load_cards_with_staging(cards)

        # Add search_query to the result for consistency
        load_result["search_query"] = search_query

        return load_result

    def _scryfall_search(self, *, query: str, unique: str = "prints") -> list[dict[str, Any]]:
        """Search Scryfall API for cards matching the given query.

        This method handles pagination to get the complete list of cards and
        automatically applies filters for paper format and format legality.

        Args:
        ----
            query (str): The search query string for Scryfall.
            unique (str): The unique parameter to pass to the Scryfall API.

        Returns:
        -------
            List[Dict[str, Any]]: List of card data from Scryfall API.

        Raises:
        ------
            ValueError: If API request fails or returns invalid data.

        """
        # Add standard filters for paper format and format legality
        # Wrap original query in parentheses to ensure proper filter application
        filters = [
            "(f:m or f:l or f:c or f:v)",
            "game:paper",
            f"unique:{unique}",
        ]
        full_query = f"({query}) {' '.join(filters)}"

        base_url = "https://api.scryfall.com/cards/search"
        params = {"q": full_query, "format": "json"}
        all_cards = []

        total_cards = "?"
        try:
            while True:
                time.sleep(1 / 10)  # Rate limiting - 10 requests per second max
                logger.info(
                    "Making request to Scryfall API: %s %s (have %d of %s total cards)",
                    base_url,
                    params,
                    len(all_cards),
                    total_cards,
                )
                response = self._session.get(base_url, params=params, timeout=30)
                response.raise_for_status()
                data = orjson.loads(response.content)

                total_cards = data.get("total_cards", 1) or 1

                if "data" not in data:
                    break

                # Extract card data from current page
                page_cards = [card for card in data["data"] if card]
                all_cards.extend(page_cards)

                # Check if there are more pages
                if not data.get("has_more", False):
                    break

                # Get next page URL
                next_page = data.get("next_page")
                if not next_page:
                    break

                # Update base_url and clear params for next page
                base_url = next_page
                params = {}

        except requests.RequestException as oops:
            # Check if it's a 404 error - return empty list
            if (hasattr(oops, "response") and oops.response and oops.response.status_code == NOT_FOUND) or "404" in str(oops):
                return all_cards
            msg = f"Failed to fetch data from Scryfall API: {oops}"
            raise ValueError(msg) from oops

        return all_cards

    if IMPORT_EXPORT:

        def export_card_data(self, **_: object) -> dict[str, Any]:
            """Export card data tables to JSON files for backup/re-import.

            Exports the three main tables:
            - magic.cards
            - magic.tags
            - magic.tag_relationships

            Files are saved to /data/api/exports/{timestamp}/ directory.

            Returns:
            -------
                Dict[str, Any]: Export result with status, file paths, and counts.
            """
            logger.info("Starting card data export")

            # Create timestamped export directory
            timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")
            export_dir = pathlib.Path("/data/api/exports") / timestamp
            export_dir.mkdir(parents=True, exist_ok=True)

            try:
                with self._conn_pool.connection() as conn, conn.cursor() as cursor:
                    cursor = typecast("Cursor", cursor)

                    export_results = {
                        "cards": self._export_cards_table(cursor, export_dir),
                        "tags": self._export_tags_table(cursor, export_dir),
                        "tag_relationships": self._export_tag_relationships_table(cursor, export_dir),
                    }

                    logger.info("Export completed successfully to %s", export_dir)
                    return {
                        "status": "success",
                        "export_directory": str(export_dir),
                        "timestamp": timestamp,
                        "results": export_results,
                        "message": "Successfully exported cards, tags, and tag relationships",
                    }

            except (OSError, psycopg.Error, ValueError) as e:
                logger.error("Failed to export card data: %s", e)
                return {
                    "status": "error",
                    "message": f"Export failed: {e}",
                }

        def _export_cards_table(self, cursor: Cursor, export_dir: pathlib.Path) -> dict[str, Any]:
            """Export magic.cards table to JSON file."""
            cards_file = export_dir / "cards.json"
            logger.info("Exporting magic.cards table to %s file", cards_file)
            cursor.execute("SELECT * FROM magic.cards ORDER BY card_name")

            cards_data = [dict(row) for row in cursor.fetchall()]
            cards_count = len(cards_data)

            # Write JSON file
            with cards_file.open("w", encoding="utf-8") as f:
                f.write(orjson.dumps(cards_data, option=orjson.OPT_INDENT_2).decode("utf-8"))

            logger.info("Exported magic.cards table to %s file", cards_file)
            return {"file": str(cards_file), "count": cards_count}

        def _export_tags_table(self, cursor: Cursor, export_dir: pathlib.Path) -> dict[str, Any]:
            """Export magic.tags table to JSON file."""
            tags_file = export_dir / "tags.json"
            logger.info("Exporting tags table to %s file", tags_file)
            cursor.execute("SELECT tag FROM magic.tags ORDER BY tag")

            tags_data = [dict(row) for row in cursor.fetchall()]
            tags_count = len(tags_data)

            # Write JSON file
            with tags_file.open("w", encoding="utf-8") as f:
                f.write(orjson.dumps(tags_data, option=orjson.OPT_INDENT_2).decode("utf-8"))

            logger.info("Exported tags table to %s file", tags_file)
            return {"file": str(tags_file), "count": tags_count}

        def _export_tag_relationships_table(self, cursor: Cursor, export_dir: pathlib.Path) -> dict[str, Any]:
            """Export magic.tag_relationships table to JSON file."""
            relationships_file = export_dir / "tag_relationships.json"
            logger.info("Exporting tag_relationships table to %s file", relationships_file)
            cursor.execute(
                """
                SELECT child_tag, parent_tag
                FROM magic.tag_relationships
                ORDER BY child_tag, parent_tag
            """,
            )

            relationships_data = [dict(row) for row in cursor.fetchall()]
            relationships_count = len(relationships_data)

            # Write JSON file
            with relationships_file.open("w", encoding="utf-8") as f:
                f.write(orjson.dumps(relationships_data, option=orjson.OPT_INDENT_2).decode("utf-8"))

            logger.info("Exported tag_relationships table to %s file", relationships_file)
            return {"file": str(relationships_file), "count": relationships_count}

        def import_card_data(self, *, timestamp: str | None = None, **_: object) -> dict[str, Any]:
            """Import card data from JSON files, truncating existing data.

            Imports data from /data/api/exports/{timestamp}/ directory.
            If timestamp is not provided, uses the most recent export.

            Args:
            ----
                timestamp (str, optional): Timestamp of export to import. If None, uses latest.

            Returns:
            -------
                Dict[str, Any]: Import result with status and counts.
            """
            logger.info("Starting card data import")

            try:
                import_dir, timestamp = self._find_import_directory(timestamp)
                self._validate_import_files(import_dir)

                logger.info("Importing from directory: %s", import_dir)

                with self._conn_pool.connection() as conn, conn.cursor() as cursor:
                    cursor = typecast("Cursor", cursor)
                    conn.autocommit = False

                    try:
                        import_results = self._perform_import(cursor, import_dir)
                        conn.commit()

                        logger.info("Import completed successfully")
                        return {
                            "status": "success",
                            "timestamp": timestamp,
                            "import_directory": str(import_dir),
                            "results": import_results,
                            "message": f"Successfully imported {import_results['cards']} cards, {import_results['tags']} tags, and {import_results['tag_relationships']} tag relationships",
                        }

                    except (OSError, psycopg.Error, ValueError) as e:
                        conn.rollback()
                        raise e
                    finally:
                        conn.autocommit = True

            except (OSError, psycopg.Error, ValueError) as e:
                logger.error("Failed to import card data: %s", e)
                return {
                    "status": "error",
                    "message": f"Import failed: {e}",
                }

        def _find_import_directory(self, timestamp: str | None) -> tuple[pathlib.Path, str]:
            """Find and validate the import directory."""
            exports_dir = pathlib.Path("/data/api/exports")
            if not exports_dir.exists():
                msg = "No exports directory found at /data/api/exports"
                raise ValueError(msg)

            if timestamp:
                import_dir = exports_dir / timestamp
                if not import_dir.exists():
                    msg = f"Export directory for timestamp {timestamp} not found"
                    raise ValueError(msg)
            else:
                # Find most recent export
                try:
                    import_dir = max(
                        (d for d in exports_dir.iterdir() if d.is_dir()),
                        key=lambda d: d.name,
                    )
                except ValueError:
                    msg = "No export directories found"
                    raise ValueError(msg) from None
                timestamp = import_dir.name

            return import_dir, timestamp

        def _validate_import_files(self, import_dir: pathlib.Path) -> None:
            """Validate that all required import files exist."""
            required_files = [
                ("cards.json", import_dir / "cards.json"),
                ("tags.json", import_dir / "tags.json"),
                ("tag_relationships.json", import_dir / "tag_relationships.json"),
            ]

            missing_files = [name for name, file_path in required_files if not file_path.exists()]

            if missing_files:
                msg = f"Missing required files: {', '.join(missing_files)}"
                raise ValueError(msg)

        def _perform_import(self, cursor: Cursor, import_dir: pathlib.Path) -> dict[str, int]:
            """Perform the actual import operation."""
            # Delete data from tables in correct order (respecting foreign keys)
            logger.info("Deleting existing data")
            cursor.execute("DELETE FROM magic.tag_relationships")
            cursor.execute("DELETE FROM magic.tags")
            cursor.execute("DELETE FROM magic.cards")

            import_results = {}

            # Import tags first (no dependencies)
            logger.info("Importing tags")
            tags_file = import_dir / "tags.json"
            with tags_file.open("r", encoding="utf-8") as f:
                tags_data = orjson.loads(f.read())

            for tag_record in tags_data:
                cursor.execute("INSERT INTO magic.tags (tag) VALUES (%(tag)s)", tag_record)

            cursor.execute("SELECT COUNT(*) FROM magic.tags")
            import_results["tags"] = cursor.fetchone()["count"]

            # Import tag relationships (depends on tags)
            logger.info("Importing tag relationships")
            relationships_file = import_dir / "tag_relationships.json"
            with relationships_file.open("r", encoding="utf-8") as f:
                relationships_data = orjson.loads(f.read())

            for relationship_record in relationships_data:
                cursor.execute(
                    "INSERT INTO magic.tag_relationships (child_tag, parent_tag) VALUES (%(child_tag)s, %(parent_tag)s)",
                    relationship_record,
                )

            cursor.execute("SELECT COUNT(*) FROM magic.tag_relationships")
            import_results["tag_relationships"] = cursor.fetchone()["count"]

            # Import cards last (largest table)
            logger.info("Importing cards")
            cards_file = import_dir / "cards.json"
            with cards_file.open("r", encoding="utf-8") as f:
                cards_data = orjson.loads(f.read())

            num_cards = len(cards_data)
            page_size = 750
            num_imported = 0
            # Import cards in batches using jsonb_populate_record
            for card_batch in itertools.batched(cards_data, page_size):
                batch_json = orjson.dumps(card_batch).decode("utf-8")
                cursor.execute(
                    """
                    INSERT INTO magic.cards
                    SELECT
                        (jsonb_populate_record(null::magic.cards, value)).*
                    FROM
                        jsonb_array_elements(%s::jsonb)
                """,
                    (batch_json,),
                )
                num_imported += cursor.rowcount
                logger.info(
                    "Imported %s of %s cards (%.1f%%)",
                    f"{num_imported:,}",
                    f"{num_cards:,}",
                    num_imported / num_cards * 100,
                )

            cursor.execute("SELECT COUNT(*) FROM magic.cards")
            import_results["cards"] = cursor.fetchone()["count"]

            return import_results

    def _load_cards_with_staging(
        self,
        cards: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Load cards into the database using a randomly-named staging table.

        This method consolidates card loading functionality by:
        1. Creating a staging table with a randomly generated suffix
        2. Loading card data into the staging table using COPY for efficiency
        3. Transferring data from staging to the main magic.cards table
        4. Returning random sample cards from staging before cleanup
        5. Dropping the staging table

        Args:
        ----
            cards (List[Dict[str, Any]]): List of card data to load.

        Returns:
        -------
            Dict[str, Any]: Result with:
                - cards_loaded: number of cards successfully loaded
                - sample_cards: list of up to 10 random cards from staging
                - status: "success", "no_cards", or "database_error"
                - message: descriptive message

        """
        if not cards:
            return {
                "status": "no_cards_before_preprocessing",
                "cards_loaded": 0,
                "sample_cards": [],
                "message": "No cards provided for loading",
            }

        self.setup_schema()

        processed_cards = []
        for icard in cards:
            processed_cards.extend(preprocess_card(icard))
        cards = processed_cards

        if not cards:
            return {
                "status": "no_cards_after_preprocessing",
                "cards_loaded": 0,
                "sample_cards": [],
                "message": "No cards remaining after preprocessing",
            }

        # Generate random staging table name
        staging_suffix = secrets.token_hex(8)
        staging_table_name = f"import_staging_{staging_suffix}"

        try:
            with self._conn_pool.connection() as conn, conn.cursor() as cursor:
                statement_timeout = 30_000
                # Validate and set statement timeout
                self._set_statement_timeout(cursor, statement_timeout)

                # fetch already imported cards...
                cursor.execute("SELECT scryfall_id FROM magic.cards GROUP BY scryfall_id")
                already_imported_cards = {r["scryfall_id"] for r in cursor.fetchall()}

                # filter cards to only those which are not already imported
                num_before_filtering = len(cards)
                cards = [card for card in cards if card["scryfall_id"] not in already_imported_cards]
                logger.info("Filtered %d cards to %d cards to import", num_before_filtering, len(cards))
                if not cards:
                    logger.info("No remaining cards to import")
                    return {
                        "status": "all_cards_already_present",
                        "cards_loaded": 0,
                        "sample_cards": [],
                        "message": "All cards already present",
                    }

                page_size = 6000
                cards_loaded = cards_sent = 0
                for page in itertools.batched(cards, page_size):
                    # Create staging table with unique name
                    cursor.execute(f"CREATE TEMPORARY TABLE {staging_table_name} (card_blob jsonb)")

                    # Load cards into staging table using COPY for efficiency
                    with cursor.copy(
                        f"COPY {staging_table_name} (card_blob) FROM STDIN WITH (FORMAT csv, HEADER false)",
                    ) as copy_filehandle:
                        writer = csv.writer(copy_filehandle, quoting=csv.QUOTE_ALL)
                        writer.writerows([orjson.dumps(card, option=orjson.OPT_SORT_KEYS).decode("utf-8")] for card in page)

                    target_sample_size = 10
                    random_threshold = 2 * target_sample_size / len(page)
                    cursor.execute(
                        f"""
                        SELECT
                            (jsonb_populate_record(null::magic.cards, card_blob)).*
                        FROM
                            {staging_table_name}
                        WHERE
                            RANDOM() < {random_threshold}
                        ORDER BY RANDOM()
                        LIMIT {target_sample_size}""",
                    )
                    sample_cards = [dict(r) for r in cursor.fetchall()]

                    # Transfer from staging to main table using direct jsonb_populate_record
                    insert_query = f"""
                        INSERT INTO magic.cards
                        SELECT
                            (jsonb_populate_record(null::magic.cards, card_blob)).*
                        FROM
                            {staging_table_name}
                        ON CONFLICT DO NOTHING
                    """
                    # could we update the on conflict to replace?

                    cursor.execute(insert_query)
                    cards_sent += len(page)
                    cards_loaded += cursor.rowcount

                    # Drop the staging table
                    cursor.execute(f"DROP TABLE {staging_table_name}")
                    logger.info(
                        "%d cards loaded, %d cards sent, of %d cards",
                        cards_loaded,
                        cards_sent,
                        len(cards),
                    )

                conn.commit()

                result = {
                    "status": "success",
                    "cards_loaded": cards_loaded,
                    "sample_cards": sample_cards,
                    "message": f"Successfully loaded {cards_loaded} cards",
                }

                # Clear caches when cards are successfully loaded
                if cards_loaded > 0:
                    self._query_cache.clear()
                    # Clear the search cache by accessing its cache attribute
                    if hasattr(self._search, "cache"):
                        self._search.cache.clear()

                return result

        except (psycopg.Error, ValueError, KeyError) as e:
            logger.error("Error loading cards with staging table %s: %s", staging_table_name, e)
            # Try to clean up staging table on error
            try:
                with self._conn_pool.connection() as conn, conn.cursor() as cursor:
                    cursor.execute(f"DROP TABLE IF EXISTS {staging_table_name}")
                    conn.commit()
            except (psycopg.Error, ValueError) as cleanup_error:
                logger.warning("Failed to cleanup staging table %s: %s", staging_table_name, cleanup_error)

            return {
                "status": "database_error",
                "cards_loaded": 0,
                "sample_cards": [],
                "message": f"Error loading cards: {e}",
            }

    def random_search(self, *, num_cards: int = 1, **_: object) -> dict[str, Any]:
        """Return one or more random cards in the same envelope shape as search().

        Args:
            num_cards: The number of random cards to return (default is 1).

        Returns:
            A dict with a "cards" key (list of card dicts) and "total_cards" key,
            matching the shape returned by search().
        """
        if not self._setup_complete():
            raise falcon.HTTPServiceUnavailable(
                title="Service Unavailable",
                description="Setup is not complete, please try again later.",
            ) from None
        num_cards = min(num_cards, 1000)
        num_cards = max(num_cards, 1)

        # TODO: how to keep this query in sync with the larger search query?
        query_sql = """
        WITH cte1 AS (
            SELECT DISTINCT ON (card_name)
                scryfall_id
            FROM
                magic.cards
            ORDER BY
                card_name,
                prefer_score DESC NULLS LAST,
                scryfall_id
        ),
        cte2 AS (
            SELECT
                scryfall_id
            FROM
                cte1
            WHERE
                RANDOM() < %(card_sample_rate)s
            ORDER BY
                RANDOM()
            LIMIT %(num_cards)s
        )
        SELECT
            card_artist,
            card_name AS name,
            card_set_code AS set_code,
            cmc,
            collector_number,
            creature_power_text AS power,
            creature_toughness_text AS toughness,
            edhrec_rank,
            mana_cost_text AS mana_cost,
            oracle_text,
            set_name,
            type_line
        FROM
            cte2
        JOIN
            magic.cards
        ON
            cte2.scryfall_id = magic.cards.scryfall_id
        """
        with self._conn_pool.connection() as conn, conn.cursor() as cursor:
            for samplerate in [0.01, 1.0]:
                cursor.execute(query_sql, {"num_cards": num_cards, "card_sample_rate": samplerate})
                cards = [dict(r) for r in cursor.fetchall()]
                if len(cards) == num_cards:
                    break
        return {"cards": cards, "total_cards": len(cards)}
