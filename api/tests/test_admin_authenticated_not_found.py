"""An authenticated caller gets the full 404 route listing; an unauthenticated one doesn't.

Hiding admin routes from the public 404 listing (test_admin_mount.py) protects against a caller who
has not proven they hold the shared secret. It buys nothing once they have, so an authenticated
request gets the full listing instead. Since the listing shown now depends on who's asking,
these also cover the caching hazard that would create: CachingMiddleware doesn't cache 4xx
responses at all (a 404's path space is effectively unbounded -- bots, scanners, typos -- so almost
none of it repeats, and caching it would need to be partitioned by caller to avoid leaking or
masking the admin listing across callers). 2xx/3xx traffic is untouched and keeps sharing one
cache slot regardless of auth state, since nothing in this app varies a successful response by it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import falcon
import falcon.testing
import pytest

from api.admin_resource import ADMIN_MOUNT_PREFIX
from api.api_resource import APIResource
from api.middlewares.admin_auth_middleware import AdminAuthMiddleware
from api.middlewares.caching_middleware import CachingMiddleware
from api.settings import settings
from api.tests.support import mock_app_context

if TYPE_CHECKING:
    from collections.abc import Generator

TEST_PASSWORD = "correct-horse-battery-staple"

# What an UNAUTHENTICATED caller gets for a path that addresses nothing: Scryfall's error object,
# not the route listing (scryfall-sets-catalogs-symbology) -- a client pointed here instead of
# api.scryfall.com parses `code` and `details`, and a listing carries neither. Trivially, no admin
# route can leak through a body that names no routes at all. The listing -- the full one -- is
# still what an authenticated caller gets, which is the half of #966 this surface keeps.
_SCRYFALL_NOT_FOUND = {
    "object": "error",
    "code": "not_found",
    "status": 404,
    "details": "The requested object or REST method was not found.",
}

_AUTH_HEADERS = {"Authorization": "Basic d2hvZXZlcjpjb3JyZWN0LWhvcnNlLWJhdHRlcnktc3RhcGxl"}  # whoever:TEST_PASSWORD


@pytest.fixture(name="admin_password")
def admin_password_fixture() -> str:
    """Set ADMIN_PASSWORD for the duration of one test, then restore it."""
    original = settings.admin_password
    settings.admin_password = TEST_PASSWORD
    yield TEST_PASSWORD
    settings.admin_password = original


@pytest.fixture(name="resource")
def resource_fixture() -> APIResource:
    app_context = mock_app_context(reader_pool=MagicMock(), writer_pool=MagicMock())
    return APIResource(app_context=app_context)


def _client(resource: APIResource, *, cache: bool = False) -> falcon.testing.TestClient:
    middleware = [AdminAuthMiddleware()]
    if cache:
        middleware.append(CachingMiddleware())
    app = falcon.App(middleware=middleware)
    app.add_sink(resource._handle, prefix="/")
    return falcon.testing.TestClient(app)


class TestAuthenticatedNotFoundListing:
    """What an unknown path returns depends on whether the caller already authenticated."""

    def test_unauthenticated_404_omits_admin_routes(self, resource: APIResource, admin_password: str) -> None:
        del admin_password
        result = _client(resource).simulate_get("/totally/bogus")
        assert result.status == falcon.HTTP_404
        assert result.json == _SCRYFALL_NOT_FOUND

    def test_authenticated_404_includes_admin_routes(self, resource: APIResource, admin_password: str) -> None:
        del admin_password
        result = _client(resource).simulate_get("/totally/bogus", headers=_AUTH_HEADERS)
        assert result.status == falcon.HTTP_404
        assert f"{ADMIN_MOUNT_PREFIX}/setup_schema" in result.json["description"]["routes"]

    def test_wrong_credential_still_gets_the_public_listing(self, resource: APIResource, admin_password: str) -> None:
        del admin_password
        wrong = {"Authorization": "Basic d2hvZXZlcjp3cm9uZw=="}  # whoever:wrong
        result = _client(resource).simulate_get("/totally/bogus", headers=wrong)
        assert result.status == falcon.HTTP_404
        assert result.json == _SCRYFALL_NOT_FOUND

    def test_authenticated_404_under_the_mount_also_gets_the_full_listing(
        self, resource: APIResource, admin_password: str
    ) -> None:
        del admin_password
        result = _client(resource).simulate_get(f"/{ADMIN_MOUNT_PREFIX}/nope", headers=_AUTH_HEADERS)
        assert result.status == falcon.HTTP_404
        assert f"{ADMIN_MOUNT_PREFIX}/setup_schema" in result.json["description"]["routes"]


class TestNotFoundIsNeverCached:
    """404s are never cached, for anyone -- see CachingMiddleware.process_response."""

    @pytest.fixture(autouse=True)
    def _enable_cache(self) -> Generator[None]:
        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            yield

    def test_repeated_anonymous_404_is_never_a_cache_hit(self, resource: APIResource, admin_password: str) -> None:
        del admin_password
        client = _client(resource, cache=True)
        first = client.simulate_get("/totally/bogus")
        second = client.simulate_get("/totally/bogus")

        assert first.headers.get("X-Cache") == "miss"
        assert second.headers.get("X-Cache") == "miss"
        assert second.json == _SCRYFALL_NOT_FOUND

    def test_repeated_authenticated_404_is_never_a_cache_hit(self, resource: APIResource, admin_password: str) -> None:
        del admin_password
        client = _client(resource, cache=True)
        first = client.simulate_get("/totally/bogus", headers=_AUTH_HEADERS)
        second = client.simulate_get("/totally/bogus", headers=_AUTH_HEADERS)

        assert first.headers.get("X-Cache") == "miss"
        assert second.headers.get("X-Cache") == "miss"
        assert f"{ADMIN_MOUNT_PREFIX}/setup_schema" in second.json["description"]["routes"]

    def test_authenticated_404_is_not_served_to_a_later_unauthenticated_caller(
        self, resource: APIResource, admin_password: str
    ) -> None:
        del admin_password
        client = _client(resource, cache=True)
        authed = client.simulate_get("/totally/bogus", headers=_AUTH_HEADERS)
        unauthed = client.simulate_get("/totally/bogus")

        assert f"{ADMIN_MOUNT_PREFIX}/setup_schema" in authed.json["description"]["routes"]
        assert unauthed.json == _SCRYFALL_NOT_FOUND
        assert unauthed.headers.get("X-Cache") != "hit"

    def test_unauthenticated_404_does_not_mask_a_later_authenticated_caller(
        self, resource: APIResource, admin_password: str
    ) -> None:
        del admin_password
        client = _client(resource, cache=True)
        unauthed = client.simulate_get("/totally/bogus")
        authed = client.simulate_get("/totally/bogus", headers=_AUTH_HEADERS)

        assert unauthed.json == _SCRYFALL_NOT_FOUND
        assert f"{ADMIN_MOUNT_PREFIX}/setup_schema" in authed.json["description"]["routes"]
        assert authed.headers.get("X-Cache") != "hit"

    def test_public_2xx_cache_is_shared_regardless_of_auth(self, resource: APIResource, admin_password: str) -> None:
        # Unaffected by any of the above: nothing in this app varies a 2xx by auth state, so it
        # keeps sharing the one cache slot every caller maps to.
        del admin_password
        client = _client(resource, cache=True)
        anon = client.simulate_get("/robots.txt")
        authed = client.simulate_get("/robots.txt", headers=_AUTH_HEADERS)

        assert anon.headers.get("X-Cache") == "miss"
        assert authed.headers.get("X-Cache") == "hit"
