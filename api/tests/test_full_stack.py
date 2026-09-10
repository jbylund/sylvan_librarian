"""Requests through the same Falcon app production serves: every middleware, the sink, the error serializer.

The unit tests drive `APIResource._handle` directly or assemble one or two middlewares by hand, so a
bug that only shows up in the interaction between them -- a header a late middleware rejects after an
early one has already transformed the body, say -- passes every one of them. These build the app via
`ApiWorker.get_api`, the factory `entrypoint.py` uses, against the session postgres container.
"""

from __future__ import annotations

import logging
import multiprocessing
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import falcon
import falcon.testing
import pytest

from api.api_worker import ApiWorker
from api.settings import settings
from api.tests.support import override_attr

if TYPE_CHECKING:
    from collections.abc import Generator

    from api.api_resource import APIResource


@pytest.fixture(name="app", scope="module")
def app_fixture(postgres_container: None) -> Generator[falcon.App]:
    """The production app, schema set up against the session container, import fast-pathed.

    `last_import_time` is now so `APIResource.__init__`'s own `import_data()` call takes its fast path
    rather than fetching Scryfall bulk data; the schema is set up for real (an unset
    `schema_setup_event`) so the query-log writer and the SQL search path have their tables.
    """
    app = ApiWorker.get_api(
        import_guard=multiprocessing.RLock(),
        last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        schema_setup_event=multiprocessing.Event(),
        cache_generation=multiprocessing.Value("i", 0, lock=True),
        engine_reload_guard=multiprocessing.Lock(),
    )
    resource = _resource_of(app)
    override_attr(resource.app_context, "setup_complete", lambda: True)
    yield app
    resource.app_context.reader_pool.close()
    resource.app_context.writer_pool.close()


def _resource_of(app: falcon.App) -> APIResource:
    """The APIResource behind the app's single sink."""
    (_prefix, sink, _), *_ = app._sinks
    return sink.__self__


@pytest.fixture(name="client")
def client_fixture(app: falcon.App) -> falcon.testing.TestClient:
    return falcon.testing.TestClient(app)


def _simulate_without_user_agent(app: falcon.App, path: str) -> tuple[str, bytes]:
    """GET `path` with no User-Agent header at all.

    `simulate_get` cannot express this: `falcon.testing.create_environ` injects a default User-Agent
    whenever the caller does not name one, so the header has to be deleted from the environ.
    """
    env = falcon.testing.create_environ(path=path)
    del env["HTTP_USER_AGENT"]
    start_response = falcon.testing.StartResponseMock()
    body = b"".join(app(env, start_response))
    return start_response.status, body


class TestMissingUserAgent:
    """A request with no User-Agent header is an ordinary request.

    TimingMiddleware logged the User-Agent with `req.get_header("User-Agent", "-")`, whose second
    positional parameter is `required`, not a default: every UA-less request -- curl -H 'User-Agent:',
    many monitors, some proxies -- got a 400 "Missing header value" from the *last* middleware to run,
    after compression had already set Content-Encoding on the body it was about to discard.
    """

    def test_get_pid_without_user_agent_is_200(self, app: falcon.App) -> None:
        status, body = _simulate_without_user_agent(app, "/get_pid")
        assert status == falcon.HTTP_200, body

    def test_with_user_agent_is_still_200(self, client: falcon.testing.TestClient) -> None:
        result = client.simulate_get("/get_pid")
        assert result.status == falcon.HTTP_200


class TestResponseCacheInvalidation:
    """An import must not leave the cross-worker response cache serving the old corpus."""

    def test_bumping_the_generation_misses_the_response_cache(self, app: falcon.App, client: falcon.testing.TestClient) -> None:
        resource = _resource_of(app)
        saved = settings.enable_cache
        settings.enable_cache = True
        try:
            # robots.txt: cacheable (no no-store header) and independent of the database.
            assert client.simulate_get("/robots.txt").headers.get("X-Cache") == "miss"
            assert client.simulate_get("/robots.txt").headers.get("X-Cache") == "hit"
            resource.app_context.bump_cache_generation()
            assert client.simulate_get("/robots.txt").headers.get("X-Cache") == "miss"
        finally:
            settings.enable_cache = saved


class TestRepeatedQueryParameters:
    """`?q=a&q=b` is a 400 that names the parameter, not a 500 from inside the parser."""

    @pytest.mark.parametrize(
        argnames=["query_string", "param"],
        argvalues=[("q=a&q=b", "q"), ("unique=cards&unique=art", "unique")],
        ids=["str", "enum"],
    )
    def test_repeated_scalar_is_400(self, client: falcon.testing.TestClient, query_string: str, param: str) -> None:
        result = client.simulate_get("/search", query_string=query_string)
        assert result.status == falcon.HTTP_400, result.text
        assert result.json["description"] == f"parameter '{param}' was given more than once"


class TestSearchFailuresAreNotCacheable:
    """A failed /search must not go out with a public Cache-Control.

    /search set `Cache-Control: public, max-age=90` before running the search, and Falcon keeps a
    header set before the handler raises -- so a cold-start 503 and an unhandled 500 both went out
    cacheable, and a CDN in front served the failure for the next 90 seconds.
    """

    def test_503_carries_no_public_cache_control(self, app: falcon.App, client: falcon.testing.TestClient) -> None:
        resource = _resource_of(app)
        with patch.object(resource, "_search", side_effect=falcon.HTTPServiceUnavailable(title="Service Unavailable")):
            result = client.simulate_get("/search", params={"q": "bolt"})
        assert result.status == falcon.HTTP_503
        assert "public" not in (result.headers.get("Cache-Control") or "")

    def test_500_carries_no_public_cache_control(self, app: falcon.App, client: falcon.testing.TestClient) -> None:
        resource = _resource_of(app)
        with (
            patch.object(resource, "_search", side_effect=RuntimeError("engine exploded")),
            patch("api.api_resource.error_monitoring.error_handler"),
        ):
            result = client.simulate_get("/search", params={"q": "bolt"})
        assert result.status == falcon.HTTP_500
        assert "public" not in (result.headers.get("Cache-Control") or "")

    def test_successful_search_is_still_cacheable(self, app: falcon.App, client: falcon.testing.TestClient) -> None:
        resource = _resource_of(app)
        with patch.object(resource, "_search", return_value={"cards": [], "total_cards": 0}):
            result = client.simulate_get("/search", params={"q": "bolt"})
        assert result.status == falcon.HTTP_200
        assert result.headers.get("Cache-Control") == "public, max-age=90"


class TestMalformedHostHeader:
    """A Host header the client controls must never be a 500.

    `req.host` raises ValueError for a non-numeric port, and the site-name lookup's urlparse raises
    for an unbalanced IPv6 bracket; both surfaced as 500 + an error-monitor notice on every route
    that derives a site name.
    """

    @pytest.mark.parametrize(argnames=["host"], argvalues=[("example.com:abc",), ("[abc",), ("a]b",), ("[::1",)])
    @pytest.mark.parametrize(argnames=["path"], argvalues=[("/",), ("/card/tst/1",)])
    def test_page_still_renders(self, client: falcon.testing.TestClient, host: str, path: str) -> None:
        result = client.simulate_get(path, host=host)
        assert result.status == falcon.HTTP_200, result.text
        assert result.headers.get("content-type", "").startswith("text/html")


class TestHotPathLogVolume:
    """One INFO record per ordinary request: TimingMiddleware's timing line.

    The hot path wrote seven to nine INFO lines per request (dispatch, handler duration, cache
    hit/miss/update, compressor selection, compression summary, engine/SQL dispatch), which is a
    measurable cost at the request rates the API serves and drowned the one line that matters.
    """

    def test_static_request_logs_one_info_line(self, client: falcon.testing.TestClient, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="api"):
            client.simulate_get("/robots.txt", headers={"Accept-Encoding": "gzip"})
        info_lines = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
        assert len(info_lines) == 1, info_lines
        assert info_lines[0].startswith("[timing]")

    def test_not_found_logs_at_info_without_a_traceback(
        self, client: falcon.testing.TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="api"):
            result = client.simulate_get("/no-such-route")
        assert result.status == falcon.HTTP_404
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert errors == [], [record.getMessage() for record in errors]
        rejected = [record for record in caplog.records if record.getMessage().startswith("Rejected ")]
        assert rejected
        assert rejected[0].exc_info is None


class TestReadyThroughTheStack:
    """The readiness probe's 503 must survive the middleware stack -- and keep its no-store."""

    def test_503_with_the_failed_check_named(self, app: falcon.App, client: falcon.testing.TestClient) -> None:
        # The fixture stubs setup_complete to True. The engine store is mocked empty rather than
        # assumed so: the engine's archive is shared by path across the whole test session, and
        # another module may have loaded fixture cards into it.
        empty_engine = MagicMock()
        empty_engine.size.return_value = 0
        saved = settings.enable_engine
        settings.enable_engine = True
        try:
            with patch.object(_resource_of(app).app_context, "engine", empty_engine):
                result = client.simulate_get("/ready")
        finally:
            settings.enable_engine = saved
        assert result.status == falcon.HTTP_503
        assert result.json["ready"] is False
        assert result.json["failed"] == ["engine_loaded"]
        assert result.headers.get("Cache-Control") == "no-store"
        assert result.headers.get("X-Cache") != "hit"

    def test_200_once_every_check_passes(self, client: falcon.testing.TestClient) -> None:
        saved = settings.enable_engine
        settings.enable_engine = False  # SQL serves everything; an empty store is not a failure
        try:
            result = client.simulate_get("/ready")
        finally:
            settings.enable_engine = saved
        assert result.status == falcon.HTTP_200
        assert result.json["ready"] is True
        assert result.headers.get("Cache-Control") == "no-store"
