"""API worker process."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import signal
from typing import TYPE_CHECKING

import falcon
import falcon.media
import orjson

from api.utils import multiprocessing_utils

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized
    from multiprocessing.synchronize import Event as EventType
    from multiprocessing.synchronize import RLock as LockType

# Set up a logger for this module
logger = logging.getLogger(__name__)

ALL_INTERFACES = "0.0.0.0"  # noqa: S104

# How long a cross-worker response-cache entry lives, in seconds. The cache key carries the cache
# generation, so an import already makes every earlier entry unreachable; the TTL is what bounds how
# long those unreachable entries occupy the arena, and how long anything the generation does not
# track (a corrected static file under a running process, say) keeps being served from cache.
SHARED_CACHE_TTL_SECONDS = 300.0


def json_error_serializer(request: falcon.Request, response: falcon.Response, exception: falcon.HTTPError) -> None:
    """An error serializer that formats Falcon HTTP errors as JSON responses.

    Args:
        request (falcon.Request): The incoming HTTP request (unused).
        response (falcon.Response): The HTTP response object to modify.
        exception (falcon.HTTPError): The exception to serialize.
    """
    del request  # request is unused, but required by the interface
    exception_dict = exception.to_dict()  # Convert the exception to a dictionary
    exception_dict = json.loads(json.dumps(exception_dict, default=str))  # Ensure all values are JSON serializable
    response.media = exception_dict  # Set the response body
    response.content_type = "application/json"  # Set the content type


def forward_sigterm_to_sigint(signum: int, frame: object) -> None:
    """Turn the SIGTERM the master sends on shutdown into the SIGINT bjoern shuts down gracefully on.

    bjoern (3.2.2) installs a libev handler for SIGINT only: it stops accepting, lets the requests in
    flight finish, and returns from bjoern.run. SIGTERM has no handler there, so its default
    disposition killed the worker mid-request -- no better than the SIGKILL it replaces. bjoern also
    calls PyErr_CheckSignals every 0.1 s from its loop, which is what lets this Python-level handler
    run in an idle worker rather than waiting for the next request.
    """
    del signum, frame
    os.kill(os.getpid(), signal.SIGINT)


class ApiWorker(multiprocessing.Process):
    """A worker process that runs a Falcon API server in a separate subprocess.

    This class is designed to be used with Python's multiprocessing module to run
    the API server in its own process, allowing for parallelism and isolation.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        host: str = ALL_INTERFACES,
        port: int = 8080,
        exit_flag: EventType | None = None,
        debug: bool = False,
        import_guard: LockType = multiprocessing_utils.DEFAULT_LOCK,
        last_import_time: Synchronized | None = None,
        schema_setup_event: EventType = multiprocessing_utils.DEFAULT_EVENT,
        cache_generation: Synchronized | None = None,
        engine_reload_guard: LockType | None = None,
    ) -> None:
        """Initialize the API worker process.

        Args:
            host (str): The host address to bind the server to. Defaults to ALL_INTERFACES.
            port (int): The port to listen on. Defaults to 8080.
            exit_flag (multiprocessing.Event | None): An optional event to signal process exit.
            import_guard (multiprocessing.RLock): An optional lock to synchronize imports.
            last_import_time (Synchronized | None): Shared value for last bulk import timestamp (Unix time).
            schema_setup_event (multiprocessing.Event): Event denoting schema setup has been completed.
            debug (bool): Whether to run in debug mode.
            cache_generation (Synchronized | None): Shared counter incremented on cache invalidation.
            engine_reload_guard (multiprocessing.Lock | None): Cross-worker lock so only one worker reloads the engine store.
        """
        super().__init__()
        self.host = host
        self.port = port
        self.exit_flag = exit_flag
        self.import_guard = import_guard
        self.last_import_time = last_import_time
        self.debug = debug
        self.schema_setup_event = schema_setup_event
        self.cache_generation = cache_generation
        self.engine_reload_guard = engine_reload_guard

    @classmethod
    def get_api(
        cls: type[ApiWorker],
        import_guard: LockType,
        last_import_time: Synchronized | None,
        schema_setup_event: EventType,
        cache_generation: Synchronized | None = None,
        engine_reload_guard: LockType | None = None,
    ) -> falcon.App:
        """Create and configure the Falcon API application.

        Returns:
            falcon.App: The configured Falcon application instance.
        """
        # Importing here (post-fork) is safer for some servers/clients than importing before forking.
        from api.admin_resource import AdminContext  # pylint: disable=import-outside-toplevel
        from api.api_resource import APIResource  # pylint: disable=import-outside-toplevel
        from api.app_context import AppContext  # pylint: disable=import-outside-toplevel
        from api.middlewares import (
            AdminAuthMiddleware,
            CachingMiddleware,
            CompressionMiddleware,
            CORSMiddleware,
            QueryLogMiddleware,
            SearchBudgetMiddleware,
            SecurityHeadersMiddleware,
            TimingMiddleware,
        )
        from api.settings import settings

        if cache_generation is None:
            # One counter for the middleware and the AppContext below: the response cache keys on
            # it, and every write to magic.cards bumps it, so the two must be the same object.
            cache_generation = multiprocessing.Value("i", 0, lock=True)

        shared_cache = None
        if settings.enable_cache:
            try:
                from shared_cache import SharedCache

                shared_cache = SharedCache(
                    path=settings.shared_cache_path,
                    maxsize=10_000,
                    n_pages=3,
                    default_ttl=SHARED_CACHE_TTL_SECONDS,
                )
                logger.info("SharedCache opened at %s pid=%d", settings.shared_cache_path, os.getpid())
            except (ImportError, OSError, TypeError):
                logger.warning("SharedCache unavailable, falling back to per-process LRUCache", exc_info=True)

        api = falcon.App(
            middleware=[
                TimingMiddleware(),
                AdminAuthMiddleware(),  # ahead of caching: a rejected admin request must never hit the cache
                SearchBudgetMiddleware(),  # ahead of logging/caching: skip parser/handler on over-budget /search
                QueryLogMiddleware(),  # process_response fires before TimingMiddleware's
                CachingMiddleware(cache=shared_cache, cache_generation=cache_generation),
                CompressionMiddleware(),
                SecurityHeadersMiddleware(),  # Add security headers to all responses
                CORSMiddleware(),  # Handle CORS requests
            ],
        )
        api.set_error_serializer(json_error_serializer)  # Use custom JSON error serializer
        # Built here, post-fork: reader/writer pools and the query engine can't cross a fork the
        # way a multiprocessing.Value/Lock can, so each worker builds its own AppContext from the
        # primitives the master created before forking.
        app_context = AppContext(
            cache_generation=cache_generation,
            engine_reload_guard=engine_reload_guard,
            last_import_time=last_import_time,
        )
        admin_context = AdminContext(import_guard=import_guard, schema_setup_event=schema_setup_event)
        sink = APIResource(app_context=app_context, admin_context=admin_context)  # Create the main API resource
        api.add_sink(sink._handle, prefix="/")  # Route all requests to the sink handler

        json_handler = falcon.media.JSONHandler(
            dumps=orjson.dumps,
            loads=orjson.loads,
        )
        extra_handlers = {
            "application/json": json_handler,
        }

        api.req_options.media_handlers.update(extra_handlers)
        api.resp_options.media_handlers.update(extra_handlers)

        return api

    def run(self) -> None:
        """Run the API server indefinitely in this process.

        This method is called when the process starts. It sets up logging, creates the API,
        and starts the Bjoern server. If an error occurs, it logs the error and sets the exit flag.
        """
        logging.basicConfig(level=logging.INFO)
        logging.info("Starting worker with pid %d", os.getpid())
        try:
            import bjoern

            app = self.get_api(
                cache_generation=self.cache_generation,
                engine_reload_guard=self.engine_reload_guard,
                import_guard=self.import_guard,
                last_import_time=self.last_import_time,
                schema_setup_event=self.schema_setup_event,
            )  # Get the Falcon app
            signal.signal(signal.SIGTERM, forward_sigterm_to_sigint)
            try:
                bjoern.run(
                    wsgi_app=app,
                    host=self.host,
                    port=self.port,
                    reuse_port=True,
                    listen_backlog=1024 * 4,
                )  # Start the Bjoern server
            except KeyboardInterrupt:
                # bjoern raises this once its loop has drained after a SIGINT: a clean shutdown.
                logger.info("Worker %d stopped after draining in-flight requests", os.getpid())
        except Exception as oops:
            logger.error("Error running server: %s", oops, exc_info=True)
            if self.exit_flag:
                self.exit_flag.set()  # Signal exit if an exit flag is provided
