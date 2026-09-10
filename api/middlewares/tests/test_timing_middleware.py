"""Tests for TimingMiddleware's response-side duties beyond the timing line."""

from __future__ import annotations

import falcon
import falcon.testing
import pytest

from api.middlewares.timing import TimingMiddleware, is_server_error


class _CacheableThenFails:
    """A handler that sets a public Cache-Control and then raises, as /search used to."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        del req
        resp.set_header("Cache-Control", "public, max-age=90")
        raise self._error


class _Ok:
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        del req
        resp.set_header("Cache-Control", "public, max-age=90")
        resp.media = {"ok": True}


def _client(handler: object) -> falcon.testing.TestClient:
    app = falcon.App(middleware=[TimingMiddleware()])
    app.add_route("/x", handler)
    return falcon.testing.TestClient(app)


class TestServerErrorsAreNotCacheable:
    """Falcon keeps headers a handler set before raising; a 5xx must not go out with a public max-age."""

    @pytest.mark.parametrize(
        argnames=["error", "status"],
        argvalues=[
            (falcon.HTTPServiceUnavailable(title="down"), falcon.HTTP_503),
            (falcon.HTTPInternalServerError(title="broken"), falcon.HTTP_500),
        ],
        ids=["503", "500"],
    )
    def test_cache_control_is_stripped_from_5xx(self, error: Exception, status: str) -> None:
        result = _client(_CacheableThenFails(error)).simulate_get("/x")
        assert result.status == status
        assert result.headers.get("Cache-Control") is None

    def test_cache_control_survives_on_success(self) -> None:
        result = _client(_Ok()).simulate_get("/x")
        assert result.status == falcon.HTTP_200
        assert result.headers.get("Cache-Control") == "public, max-age=90"

    def test_cache_control_survives_on_4xx(self) -> None:
        """A 4xx is the route's own decision (a 400 is even deliberately cacheable); only 5xx is stripped."""
        result = _client(_CacheableThenFails(falcon.HTTPBadRequest(title="bad"))).simulate_get("/x")
        assert result.status == falcon.HTTP_400
        assert result.headers.get("Cache-Control") == "public, max-age=90"


@pytest.mark.parametrize(
    argnames=["status", "expected"],
    argvalues=[
        ("500 Internal Server Error", True),
        ("503 Service Unavailable", True),
        (502, True),
        ("200 OK", False),
        ("404 Not Found", False),
        (None, False),
    ],
)
def test_is_server_error(status: str | int | None, expected: bool) -> None:
    assert is_server_error(status) is expected
