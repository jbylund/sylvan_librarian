"""Tests for the boundary between the public route table and the mounted admin child.

The point of these is that the mount is a boundary rather than a URL prefix. A change that leaves the
admin handlers reachable, or advertises them, would otherwise pass every other test in the suite.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import falcon
import falcon.testing
import pytest

from api.admin_resource import ADMIN_MOUNT_PREFIX, AdminResource
from api.api_resource import APIResource
from api.tests.support import mock_app_context

# The complete public surface, as a literal. A route appearing or disappearing here is a deliberate
# act, and this is the guard that makes it one — the failure this whole change exists to prevent was
# a handler becoming reachable because registration defaulted to exposing it.
EXPECTED_PUBLIC_ROUTES = {
    "_root",
    "card",
    # The Scryfall-compatible surface (ScryfallCardsRoutes, mixed into APIResource): public by
    # design, a client points here by swapping its base URL.
    "cards",
    "cards/autocomplete",
    "cards/collection",
    "cards/named",
    "cards/random",
    "cards/search",
    "favicon.ico",
    "get_catalog",
    "get_common_keywords",
    "get_pid",
    "index",
    "index.html",
    "random_search",
    "robots.txt",
    "search",
    "static/app.js",
    "static/app.min.js",
    "static/card.js",
    "static/favicon.ico",
    "static/social-preview.webp",
    "static/styles.css",
}

# Handlers that must only ever be reachable behind the mount.
EXPECTED_ADMIN_ROUTES = {
    "backfill_cubecobra_scores",
    "backfill_prefer_scores",
    "discover_is_tags_from_syntax",
    "import_all_is_tags",
    "import_art_tags",
    "import_card_by_name",
    "import_cards_by_search",
    "import_data",
    "import_oracle_tags",
    # The rulings bulk file, behind the mount with every other import (scryfall-cards-api).
    "import_rulings",
    "ingest_cubecobra",
    "prefer_score_tuner",
    "setup_schema",
}


@pytest.fixture(name="resource")
def resource_fixture() -> APIResource:
    """An APIResource with its child mounted, against distinct mocked pools.

    reader_pool and writer_pool are injected separately (rather than left to default) so the two
    are distinct objects, matching that AdminResource reads/writes via its own pool rather than
    the one search-shaped resources use.
    """
    app_context = mock_app_context(reader_pool=MagicMock(), writer_pool=MagicMock())
    return APIResource(app_context=app_context)


class TestRouteTable:
    """What is registered, and where."""

    def test_public_surface_is_exactly_as_expected(self, resource: APIResource) -> None:
        public = {path for path in resource.routes if not path.startswith(f"{ADMIN_MOUNT_PREFIX}/")}
        assert public == EXPECTED_PUBLIC_ROUTES

    def test_admin_handlers_are_only_behind_the_mount(self, resource: APIResource) -> None:
        behind = {
            path.removeprefix(f"{ADMIN_MOUNT_PREFIX}/") for path in resource.routes if path.startswith(f"{ADMIN_MOUNT_PREFIX}/")
        }
        assert behind == EXPECTED_ADMIN_ROUTES
        assert not EXPECTED_ADMIN_ROUTES & EXPECTED_PUBLIC_ROUTES

    def test_every_admin_route_is_bound_to_the_child(self, resource: APIResource) -> None:
        # A handler left on the parent would be registered under the prefix but still reachable
        # unprefixed, which is the shape of the bug this replaced.
        for path, entry in resource.routes.items():
            if path.startswith(f"{ADMIN_MOUNT_PREFIX}/"):
                assert entry.action.__wrapped__.__self__ is resource.admin


class TestDelisting:
    """The 404 listing must not become a directory of what is behind the mount."""

    def test_no_admin_route_is_advertised(self, resource: APIResource) -> None:
        assert not [name for name in resource._not_found_routes if ADMIN_MOUNT_PREFIX in name]

    def test_listing_matches_the_public_surface(self, resource: APIResource) -> None:
        assert set(resource._not_found_routes) == EXPECTED_PUBLIC_ROUTES

    def test_mount_declares_itself_unadvertised(self, resource: APIResource) -> None:
        # Set once at the mount rather than on each handler, so forgetting it is a property of the
        # single call site instead of a hole in one route.
        for path, entry in resource.routes.items():
            assert entry.spec.advertise is not path.startswith(f"{ADMIN_MOUNT_PREFIX}/")


class TestDispatch:
    """What a client can actually reach."""

    def _client(self, resource: APIResource) -> falcon.testing.TestClient:
        app = falcon.App()
        app.add_sink(resource._handle, prefix="/")
        return falcon.testing.TestClient(app)

    @pytest.mark.parametrize(
        argnames=["name"],
        argvalues=[(name,) for name in sorted(EXPECTED_ADMIN_ROUTES)],
    )
    def test_admin_handler_is_not_reachable_unprefixed(self, resource: APIResource, name: str) -> None:
        assert self._client(resource).simulate_get(f"/{name}").status == falcon.HTTP_404

    def test_unknown_path_under_the_mount_is_indistinguishable_from_any_other(self, resource: APIResource) -> None:
        # If the mount answered differently it would confirm its own existence, which is why the
        # child contributes nothing to the listing and has no 404 of its own.
        client = self._client(resource)
        under_mount = client.simulate_get(f"/{ADMIN_MOUNT_PREFIX}/nope")
        elsewhere = client.simulate_get("/totally/bogus")
        assert under_mount.status == elsewhere.status == falcon.HTTP_404
        assert under_mount.text == elsewhere.text

    def test_bare_mount_prefix_is_a_404(self, resource: APIResource) -> None:
        assert self._client(resource).simulate_get(f"/{ADMIN_MOUNT_PREFIX}/").status == falcon.HTTP_404

    def test_public_routes_still_answer(self, resource: APIResource) -> None:
        client = self._client(resource)
        assert client.simulate_get("/get_pid").status == falcon.HTTP_200
        assert client.simulate_get("/robots.txt").status == falcon.HTTP_200


class TestSharedSurface:
    """Both resources reach into one shared AppContext rather than one holding a reference to the other."""

    def test_admin_holds_no_reference_to_the_parent(self, resource: APIResource) -> None:
        assert isinstance(resource.admin, AdminResource)
        assert not hasattr(resource.admin, "_parent")

    def test_both_resources_share_one_app_context(self, resource: APIResource) -> None:
        assert resource.admin.app_context is resource.app_context

    def test_admin_only_handles_live_on_the_child(self, resource: APIResource) -> None:
        for handle in ("_session", "_bulk_data_fetcher", "admin_context"):
            assert hasattr(resource.admin, handle), handle
            assert not hasattr(resource, handle), f"{handle} should have moved to the child"

    def test_admin_context_holds_the_import_serialisation_primitives(self, resource: APIResource) -> None:
        assert hasattr(resource.admin.admin_context, "import_guard")
        assert hasattr(resource.admin.admin_context, "schema_setup_event")

    def test_shared_signals_live_on_app_context(self, resource: APIResource) -> None:
        for handle in ("cache_generation", "last_import_time"):
            assert hasattr(resource.app_context, handle), handle

    def test_admin_reads_and_writes_via_its_own_pool(self, resource: APIResource) -> None:
        # Each side reads/writes via its own pool, so nothing here needs the reader's query cache
        # or EXPLAIN plumbing.
        assert resource.app_context.writer_pool is not resource.app_context.reader_pool
