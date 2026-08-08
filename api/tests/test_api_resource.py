"""Comprehensive tests for APIResource class functionality."""

import multiprocessing
import os
import pathlib
import time
import unittest
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Never
from unittest.mock import MagicMock, patch

import falcon
import falcon.testing
import pytest

import api.api_resource as api_resource_module
from api.api_resource import (
    FALLBACK_SITE_NAME,
    INTERNAL_ERROR_DESCRIPTION,
    APIResource,
    _hostname_to_site_name,
    _split_words,
    hostname_to_site_name,
)
from api.enums import ResponseShape, UniqueOn
from api.middlewares.caching_middleware import CachingMiddleware
from api.settings import settings
from api.utils.routing import BoundRoute, RouteSpec, route


def make_bound_route(action: object, *, path: str = "search", positional_capacity: float = 0.0) -> BoundRoute:
    """Build a route table entry around a stub handler, for tests that patch the table wholesale.

    Args:
        action: The stub to dispatch to.
        path: Path the stub claims.
        positional_capacity: How many trailing path segments it absorbs.

    Returns:
        An entry shaped like the ones __init__ builds.
    """
    return BoundRoute(
        action=action,
        positional_capacity=positional_capacity,
        spec=RouteSpec(paths=(path,), methods=frozenset({"GET", "HEAD"}), advertise=True, ignore_unknown_params=False),
    )


def create_test_card(  # noqa: PLR0913, PLR0917
    card_id: str | None = None,
    name: str = "Test Card",
    legalities: dict | None = None,
    games: list | None = None,
    type_line: str = "Creature — Test",
    colors: list | None = None,
    color_identity: list | None = None,
    keywords: list | None = None,
    power: str | None = None,
    toughness: str | None = None,
    prices: dict | None = None,
    set_code: str = "test",
    artist: str | None = None,
    rarity: str = "common",
    collector_number: str = "1",
    edhrec_rank: int | None = None,
    **kwargs,  # noqa: ANN003
) -> dict:
    """Create a test card with default values that can be overridden.

    Args:
        card_id: Unique identifier for the card
        name: Card name
        legalities: Card legalities dict
        games: List of games the card is legal in
        type_line: Card type line
        colors: Card colors list
        color_identity: Card color identity list
        keywords: List of keywords
        power: Creature power
        toughness: Creature toughness
        prices: Price dict
        set_code: Set code
        artist: Artist name
        rarity: Card rarity
        collector_number: Collector number
        edhrec_rank: EDHREC rank
        **kwargs: Additional fields to add to the card

    Returns:
        A test card dictionary with all required fields
    """
    if legalities is None:
        legalities = {"standard": "legal", "modern": "legal"}
    if games is None:
        games = ["paper"]
    if colors is None:
        colors = ["R"]
    if color_identity is None:
        color_identity = ["R"]
    if keywords is None:
        keywords = []
    if prices is None:
        prices = {"usd": "1.00"}
    card_id = card_id or str(uuid.uuid4())
    jpg_part = f"{card_id[0]}/{card_id[1]}/{card_id}.jpg"
    card = {
        "id": card_id,
        "name": name,
        "legalities": legalities,
        "games": games,
        "type_line": type_line,
        "colors": colors,
        "color_identity": color_identity,
        "keywords": keywords,
        "power": power,
        "toughness": toughness,
        "prices": prices,
        "set": set_code,
        "artist": artist,
        "rarity": rarity,
        "collector_number": collector_number,
        "edhrec_rank": edhrec_rank,
        "image_uris": {
            # https://cards.scryfall.io/normal/front/a/7/a7af8350-9a51-437c-a55e-19f3e07acfa9.jpg?1562934732
            "small": f"https://cards.scryfall.io/small/front/{jpg_part}",
            "normal": f"https://cards.scryfall.io/normal/front/{jpg_part}",
            "large": f"https://cards.scryfall.io/large/front/{jpg_part}",
            "png": f"https://cards.scryfall.io/png/front/{jpg_part}",
            "art_crop": f"https://cards.scryfall.io/art_crop/front/{jpg_part}",
            "border_crop": f"https://cards.scryfall.io/border_crop/front/{jpg_part}",
        },
    }

    # Add any additional fields
    card.update(kwargs)

    return card


@pytest.fixture(name="patch_conn_pool")
def patch_conn_pool_fixture() -> MagicMock:
    """Patch connection pool."""
    mock_conn_pool = MagicMock()
    with patch("api.api_resource.db_utils.make_pool") as mock_pool:
        mock_pool.return_value = mock_conn_pool
        yield mock_conn_pool


class TestBaseAPIResourceTest:
    @pytest.fixture(autouse=True)
    def setUp(self, request: pytest.FixtureRequest, patch_conn_pool: MagicMock) -> None:
        """Set up test fixtures."""
        del patch_conn_pool
        self_reference = request.instance

        self_reference.mock_conn_pool = MagicMock()
        self_reference.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self_reference.api_resource._conn_pool = self_reference.mock_conn_pool


class TestAPIResourceInitializationNewStyle(TestBaseAPIResourceTest):
    """Test APIResource initialization and basic setup."""

    def test_initialization_defaults(self) -> None:
        """Test APIResource initialization with default parameters."""
        api_resource = self.api_resource
        assert api_resource._conn_pool == self.mock_conn_pool

        # Check that action map is populated
        assert "get_pid" in api_resource.routes
        assert "search" in api_resource.routes
        assert "index" in api_resource.routes

        # Check that caches are initialized
        assert hasattr(api_resource, "_query_cache")
        assert hasattr(api_resource, "_session")

    def test_initialization_with_custom_import_guard(self) -> None:
        """Test APIResource initialization with custom import guard."""
        custom_guard = multiprocessing.RLock()
        api_resource = APIResource(
            import_guard=custom_guard,
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )

        assert api_resource._import_guard == custom_guard

    def test_every_public_method_is_still_registered(self) -> None:
        """Test the marker migration left no previously-routed method behind.

        Registration used to take every public callable. This pins that the same set is still
        reachable now that each one has to opt in; step 3 is where that stops being true on purpose.
        Checked by handler rather than by key, since a handler's path need not match its name — the
        static assets are registered at the URLs they are actually requested at.
        """
        api_resource = self.api_resource
        public_methods = [
            method for method in dir(api_resource) if not method.startswith("_") and callable(getattr(api_resource, method))
        ]
        registered = {entry.action.__wrapped__.__name__ for entry in api_resource.routes.values()}

        for method in public_methods:
            assert method in registered


# The URLs the fragments, index.html and root conventions actually request. app.min.js is a build
# artifact — *.min.js is gitignored — so it is registered like the rest but only present once the
# minifier has run, which is why the serving cases omit it.
REAL_ASSET_URLS = [
    "/favicon.ico",
    "/static/favicon.ico",
    "/robots.txt",
    "/static/styles.css",
    "/static/app.js",
    "/static/app.min.js",
    "/static/card.js",
    "/static/social-preview.webp",
]
COMMITTED_ASSET_URLS = [url for url in REAL_ASSET_URLS if url != "/static/app.min.js"]


class TestRequestDispatch(TestBaseAPIResourceTest):
    """_handle() routes both flat "static/x" action keys and positional path segments.

    Regression coverage for a dispatch rewrite that split every path on "/" to derive
    (action_word, *action_args): it broke the "static/x" actions (registered under a single
    slash-containing key, not action_word "static"), crashed on unmatched routes with extra
    segments (_raise_not_found only accepted **kwargs, not positional args), and — the one fixed
    here — returned 400 instead of 404 for any *matched* route hit with more trailing segments
    than its handler accepts (e.g. /get_pid/extra), since the mismatch surfaced as a TypeError
    inside the handler rather than being recognized as "no route matches this" beforehand.
    """

    def _dispatch(self, path: str, query_string: str = "") -> falcon.Response:
        req = falcon.Request(falcon.testing.create_environ(path=path, query_string=query_string))
        resp = falcon.Response()
        self.api_resource._handle(req, resp)
        return resp

    def test_flat_static_route_is_matched_by_full_path(self) -> None:
        resp = self._dispatch("/static/favicon.ico")
        assert resp.status == falcon.HTTP_200

    def test_positional_path_segments_reach_the_action(self) -> None:
        resp = self._dispatch("/card/eoc/104")
        assert resp.status == falcon.HTTP_200
        assert resp.content_type == "text/html"

    def test_unmatched_route_with_extra_segments_raises_not_found(self) -> None:
        with pytest.raises(falcon.HTTPNotFound):
            self._dispatch("/nonexistent/thing/other")

    def test_known_zero_arg_route_with_extra_segment_raises_not_found(self) -> None:
        # Regression: a matched action_word that can't absorb a trailing segment (get_pid takes
        # no positional args) used to reach the handler anyway, raising a TypeError that _handle
        # converted to 400 — the extra segment means the path doesn't identify anything, so this
        # should 404 like any other unmatched path.
        with pytest.raises(falcon.HTTPNotFound):
            self._dispatch("/get_pid/extra")

    def test_positional_route_with_too_many_segments_raises_not_found(self) -> None:
        # card() accepts exactly 2 positional args (set_code, collector_number); a 3rd segment
        # should 404 rather than reach the handler.
        with pytest.raises(falcon.HTTPNotFound):
            self._dispatch("/card/eoc/104/extra")

    @pytest.mark.parametrize(argnames=["path"], argvalues=[(url,) for url in REAL_ASSET_URLS])
    def test_static_asset_is_registered_at_its_real_url(self, path: str) -> None:
        # Paths are declared rather than derived from method names, so they can hold dots and no
        # path rewrite is needed.
        assert path.lstrip("/") in self.api_resource.routes

    @pytest.mark.parametrize(argnames=["path"], argvalues=[(url,) for url in COMMITTED_ASSET_URLS])
    def test_static_asset_is_served_at_its_real_url(self, path: str) -> None:
        assert self._dispatch(path).status == falcon.HTTP_200

    @pytest.mark.parametrize(
        argnames=["path"],
        argvalues=[
            ("/favicon_ico",),
            ("/static/favicon_ico",),
            ("/robots_txt",),
            ("/styles_css",),
            ("/static/styles_css",),
            ("/app_js",),
            ("/static/app_js",),
            ("/social_preview_webp",),
            ("/index_html",),
        ],
    )
    def test_underscore_spelling_is_no_longer_a_route(self, path: str) -> None:
        # Artifacts of deriving route keys from Python identifiers. Nothing ever linked to them.
        with pytest.raises(falcon.HTTPNotFound):
            self._dispatch(path)

    def test_one_handler_two_paths_shares_a_single_entry(self) -> None:
        # The favicon is requested at the root by browser convention and under /static/ by the
        # fragment, so it declares both; one @route means one entry behind both keys.
        assert self.api_resource.routes["favicon.ico"] is self.api_resource.routes["static/favicon.ico"]

    def test_dots_in_path_arguments_are_no_longer_rewritten(self) -> None:
        # The rewrite applied to the whole path, not just the action word, so a dotted collector
        # number reached the handler as 104_5.
        _entry, action_args = self.api_resource._resolve_action("card/eoc/104.5")
        assert action_args == ["eoc", "104.5"]

    def test_two_routes_claiming_one_path_fail_at_construction(self) -> None:
        # Registration is fail-closed both ways: an unmarked method is not routed, and a path two
        # methods both claim is a startup error rather than one silently shadowing the other.
        class Colliding(APIResource):
            @route(paths=("search",))
            def shadows_search(self, **_: object) -> None: ...

        with pytest.raises(RuntimeError, match="claimed by both"):
            Colliding(last_import_time=multiprocessing.Value("d", time.time(), lock=True))

    @pytest.mark.parametrize(argnames=["path"], argvalues=[("/index",), ("/index.html",)])
    def test_index_path_redirects_instead_of_erroring(self, path: str) -> None:
        # falcon.HTTPMovedPermanently subclasses HTTPStatus, which is a sibling of HTTPError rather
        # than a subclass, so the generic `except Exception` swallowed the redirect and returned a
        # 500. Falcon turns the propagated signal into a 301.
        with pytest.raises(falcon.HTTPMovedPermanently):
            self._dispatch(path)

    def test_keyword_only_injection_route_with_extra_segment_raises_not_found(self) -> None:
        # get_catalog's falcon_response is keyword-only, so the route has no positional capacity.
        # While it was positional the segment was absorbed and then silently overwritten by the
        # injected response, so a path identifying nothing still returned 200.
        assert self.api_resource.routes["get_catalog"].positional_capacity == 0
        with pytest.raises(falcon.HTTPNotFound):
            self._dispatch("/get_catalog/foo")

    def test_unconvertible_enum_query_value_is_rejected(self) -> None:
        # orderby is annotated CardOrdering. The raw string used to reach the handler unconverted;
        # binding now rejects it before the handler runs.
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self._dispatch("/search", query_string="q=bolt&orderby=nonsense")
        assert exc_info.value.title == "Invalid Parameter"
        assert "orderby" in exc_info.value.description
        assert "edhrec" in exc_info.value.description  # the message names what the enum accepts

    def test_unconvertible_int_query_value_is_rejected(self) -> None:
        # Not enum-specific: any declared type that a raw string cannot satisfy is a 400 now.
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self._dispatch("/search", query_string="q=bolt&limit=abc")
        assert exc_info.value.title == "Invalid Parameter"
        assert "limit" in exc_info.value.description

    def test_positional_capacity_computed_at_init_not_per_request(self) -> None:
        assert self.api_resource.routes["get_pid"].positional_capacity == 0
        assert self.api_resource.routes["card"].positional_capacity == 2

    def test_not_found_routes_precomputed_not_rebuilt_per_request(self) -> None:
        # _not_found_routes is built once in __init__ (see _build_routes_listing) from the fixed
        # action_map contents, not recomputed on every 404 — inspect.signature() per route isn't
        # free, and the listing can't change without action_map changing.
        listing_before = self.api_resource._not_found_routes
        with pytest.raises(falcon.HTTPNotFound) as exc_info:
            self.api_resource._raise_not_found()
        assert exc_info.value.description["routes"] is listing_before
        assert "get_pid" in listing_before
        assert "card" in listing_before

    def test_no_inspect_signature_calls_on_successful_request(self) -> None:
        with patch("api.api_resource.inspect.signature") as mock_signature:
            resp = self._dispatch("/card/a25/141")
        assert resp.status == falcon.HTTP_200
        mock_signature.assert_not_called()


class TestAPIResourceCoreMethods(unittest.TestCase):
    """Test core APIResource methods."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_get_pid_returns_process_id(self) -> None:
        """Test that get_pid returns the current process ID."""
        result = self.api_resource.get_pid()
        assert isinstance(result, int)
        assert result == os.getpid()


class TestAPIResourceRequestHandling(unittest.TestCase):
    """Test request handling methods."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_raise_not_found_raises_http_not_found(self) -> None:
        """Test _raise_not_found raises HTTPNotFound with route information."""
        with pytest.raises(falcon.HTTPNotFound) as exc_info:
            self.api_resource._raise_not_found()

        error = exc_info.value
        assert "Not Found" in str(error.title)
        assert "routes" in error.description

    def test_handle_returns_early_if_response_complete(self) -> None:
        """Test _handle returns early if response is already complete."""
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.path = mock_req.relative_uri = "/test"
        mock_resp = MagicMock()
        mock_resp.complete = True

        with patch("api.api_resource.logger") as mock_logger:
            self.api_resource._handle(mock_req, mock_resp)
            mock_logger.info.assert_called_with("Request already handled: %s", "/test")

    def test_handle_processes_valid_paths(self) -> None:
        """Test _handle processes valid paths correctly."""
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/get_pid"
        mock_req.params = {}
        mock_resp = MagicMock()
        mock_resp.complete = False

        with patch("api.api_resource.logger"):
            self.api_resource._handle(mock_req, mock_resp)

            # Should call get_pid method and set response media
            assert mock_resp.media is not None

    def test_handle_raises_not_found_for_invalid_paths(self) -> None:
        """Test _handle raises HTTPNotFound for invalid paths."""
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/nonexistent"
        mock_req.params = {}
        mock_resp = MagicMock()
        mock_resp.complete = False

        with pytest.raises(falcon.HTTPNotFound):
            self.api_resource._handle(mock_req, mock_resp)

    def test_disallowed_query_params_do_not_cause_type_error(self) -> None:
        """Query params named after internal kwargs must be stripped before dispatch."""
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.path = mock_req.relative_uri = "/"
        mock_req.params = {"falcon_response": "injected", "request_host": "evil.com"}
        mock_req.host = "localhost"
        mock_resp = MagicMock()
        mock_resp.complete = False

        with patch("api.api_resource.logger"):
            # Must not raise TypeError or return a 400
            self.api_resource._handle(mock_req, mock_resp)

        assert mock_resp.text is not None

    def test_handle_handles_type_errors(self) -> None:
        """Test _handle handles TypeError exceptions."""
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/search"
        mock_req.params = {"invalid_param": "value"}
        mock_resp = MagicMock()
        mock_resp.complete = False

        # Create a mock function that will raise TypeError when called with wrong args
        def mock_action_that_raises_type_error(**kwargs: Any) -> Never:
            # This simulates a function that expects specific argument types
            # and fails even after our type conversion
            msg = "Invalid parameter type after conversion"
            raise TypeError(msg)

        # Patch the action_map directly to include our mock
        with patch.object(self.api_resource, "routes", {"search": make_bound_route(mock_action_that_raises_type_error)}):
            with pytest.raises(falcon.HTTPBadRequest):
                self.api_resource._handle(mock_req, mock_resp)

    def test_handle_handles_general_exceptions(self) -> None:
        """Test _handle handles general exceptions."""
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/search"
        mock_req.params = {}
        mock_resp = MagicMock()
        mock_resp.complete = False

        def raise_error(*args: Any, **kwargs: Any) -> Never:
            msg = "Test error"
            raise Exception(msg)

        # Mock search method to raise a general exception
        with patch.object(self.api_resource, "routes", {"search": make_bound_route(raise_error)}):
            with patch("api.api_resource.error_monitoring.error_handler") as mock_error_handler:
                with pytest.raises(falcon.HTTPInternalServerError):
                    self.api_resource._handle(mock_req, mock_resp)

                # Should call error monitoring
                mock_error_handler.assert_called_once()

    def test_handle_500_body_carries_no_frame_data(self) -> None:
        """Test an unhandled exception yields an opaque 500 body.

        The frames at a throw site inside the query or import paths hold connection and query state,
        so none of it may reach the client. Nothing upstream of this handler would catch the
        regression, which is why it is asserted on the response rather than on the validation.
        """
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/search"
        mock_req.params = {}
        mock_resp = MagicMock()
        mock_resp.complete = False

        # A local whose value must never appear in the response body.
        def raise_error(*args: Any, **kwargs: Any) -> Never:
            db_password = "correct-horse-battery-staple"
            msg = f"Test error touching {db_password}"
            raise RuntimeError(msg)

        with patch.object(self.api_resource, "routes", {"search": make_bound_route(raise_error)}):
            with patch("api.api_resource.error_monitoring.error_handler"):
                with pytest.raises(falcon.HTTPInternalServerError) as excinfo:
                    self.api_resource._handle(mock_req, mock_resp)

        description = excinfo.value.description
        assert description == INTERNAL_ERROR_DESCRIPTION
        # A dict description is how frame data used to travel; a plain string cannot carry it.
        assert isinstance(description, str)

        rendered = repr(excinfo.value.to_dict())
        for leaked in ("db_password", "correct-horse-battery-staple", "stack_info", "locals", "line_no", __file__):
            assert leaked not in rendered, f"{leaked!r} reached the 500 response body"


class TestSearchResponseShape(TestBaseAPIResourceTest):
    """Test the shape parameter that selects row-wise vs columnar cards payloads."""

    @pytest.fixture(autouse=True)
    def setup_search_results(self) -> None:
        """Provide a canned row-shaped search result envelope."""
        self.search_results = {
            "cards": [
                {"name": "Elvish Mystic", "power": "1", "toughness": "1"},
                {"name": "Counterspell", "power": None, "toughness": None},
            ],
            "total_cards": 2,
            "query": "test",
        }

    def test_search_defaults_to_row_shape(self) -> None:
        """Without a shape parameter, cards stay a list of card objects."""
        with patch.object(self.api_resource, "_search", return_value=self.search_results):
            result = self.api_resource.search(falcon_response=MagicMock(), q="test")
        assert result["cards"] == self.search_results["cards"]

    def test_search_columnar_shape_inverts_cards(self) -> None:
        """shape=columnar returns one list per field, keyed by field name."""
        with patch.object(self.api_resource, "_search", return_value=self.search_results):
            result = self.api_resource.search(falcon_response=MagicMock(), q="test", shape=ResponseShape.COLUMNAR)
        assert result["cards"] == {
            "name": ["Elvish Mystic", "Counterspell"],
            "power": ["1", None],
            "toughness": ["1", None],
        }
        # Envelope fields pass through untouched
        assert result["total_cards"] == 2
        assert result["query"] == "test"

    def test_search_columnar_does_not_mutate_cached_results(self) -> None:
        """_search returns cached dicts, so columnarizing must not modify them in place."""
        with patch.object(self.api_resource, "_search", return_value=self.search_results):
            self.api_resource.search(falcon_response=MagicMock(), q="test", shape=ResponseShape.COLUMNAR)
        assert isinstance(self.search_results["cards"], list)
        assert self.search_results["cards"][0] == {"name": "Elvish Mystic", "power": "1", "toughness": "1"}

    def test_handle_converts_shape_query_param(self) -> None:
        """The string 'columnar' from the query string is converted to the enum."""
        mock_req = MagicMock()
        mock_req.method = "GET"
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/search"
        mock_req.params = {"q": "test", "shape": "columnar"}
        mock_req.get_header.return_value = None
        mock_req.host = "example.com"
        mock_resp = MagicMock()
        mock_resp.complete = False

        with patch.object(self.api_resource, "_search", return_value=self.search_results):
            self.api_resource._handle(mock_req, mock_resp)
        assert mock_resp.media["cards"] == {
            "name": ["Elvish Mystic", "Counterspell"],
            "power": ["1", None],
            "toughness": ["1", None],
        }

    def test_random_search_columnar_shape(self) -> None:
        """random_search supports the same shape parameter."""
        mock_engine = MagicMock()
        mock_engine.size.return_value = 100
        mock_engine.sample_preferred.return_value = iter(self.search_results["cards"])
        with patch.object(self.api_resource, "_engine", mock_engine):
            result = self.api_resource.random_search(
                falcon_response=MagicMock(),
                num_cards=2,
                shape=ResponseShape.COLUMNAR,
            )
        assert result["cards"]["name"] == ["Elvish Mystic", "Counterspell"]
        assert result["total_cards"] == 2

    def test_columnarize_cards_empty_list(self) -> None:
        """An empty result set columnarizes to an empty object."""
        assert api_resource_module._columnarize_cards([]) == {}

    def test_columnarize_cards_preserves_field_order(self) -> None:
        """Field order follows the first card's key order."""
        cards = [{"b": 1, "a": 2}, {"b": 3, "a": 4}]
        assert list(api_resource_module._columnarize_cards(cards)) == ["b", "a"]


class TestOffsetValidation(TestBaseAPIResourceTest):
    """offset must be a non-negative integer; violations are 400s, not silent clamps."""

    def test_negative_offset_rejected(self) -> None:
        with pytest.raises(falcon.HTTPBadRequest):
            self.api_resource._validate_offset(-1)

    def test_valid_offsets_pass_through(self) -> None:
        assert self.api_resource._validate_offset(0) == 0
        assert self.api_resource._validate_offset(175) == 175


class TestSearchQueryDirectives(TestBaseAPIResourceTest):
    """In-query directives (unique:/sort:/order:/direction:/prefer:) apply before either search path.

    The fold happens in _search, upstream of the engine/SQL dispatch, so both paths receive the
    same effective parameters by construction. Scryfall semantics throughout (measured
    2026-08-07): a directive overrides its query parameter, the last repeat wins, and an unknown
    value warns and is ignored.
    """

    @contextmanager
    def _search_flags(self, *, enable_engine: bool) -> Generator[None]:
        """Force the engine gate and disable caching, restoring both afterward."""
        saved = (settings.enable_engine, settings.enable_cache)
        settings.enable_engine = enable_engine
        settings.enable_cache = False
        try:
            yield
        finally:
            settings.enable_engine, settings.enable_cache = saved

    def _engine_search(self, q: str, **params: Any) -> tuple[MagicMock, dict[str, Any]]:
        """Run _search against a stubbed engine; return the engine mock and the result."""
        mock_engine = MagicMock()
        mock_engine.size.return_value = 90
        mock_engine.query.return_value = (0, iter([]))
        with (
            patch.object(self.api_resource, "_setup_complete", lambda: True),
            patch.object(self.api_resource, "_engine", mock_engine),
            self._search_flags(enable_engine=True),
        ):
            result = self.api_resource._search(query=q, **params)
        return mock_engine, result

    def test_unique_directive_reaches_engine(self) -> None:
        """`unique:art` in the query string turns into the artwork mode the engine sees."""
        mock_engine, _ = self._engine_search("t:goblin unique:art")
        assert mock_engine.query.call_args.kwargs["unique"] == "artwork"

    def test_directive_overrides_parameter(self) -> None:
        """An inline directive beats the query parameter of the same meaning."""
        mock_engine, _ = self._engine_search("t:goblin unique:prints", unique=UniqueOn.ARTWORK)
        assert mock_engine.query.call_args.kwargs["unique"] == "printing"

    def test_last_repeated_directive_wins(self) -> None:
        """`unique:cards unique:art` dedups by artwork, matching Scryfall."""
        mock_engine, _ = self._engine_search("t:goblin unique:cards unique:art")
        assert mock_engine.query.call_args.kwargs["unique"] == "artwork"

    def test_every_directive_kind_applies(self) -> None:
        """sort:, direction:, and prefer: all reach the engine as their enum values."""
        mock_engine, _ = self._engine_search("t:goblin sort:usd direction:desc prefer:oldest")
        kwargs = mock_engine.query.call_args.kwargs
        assert kwargs["orderby"] == "usd"
        assert kwargs["direction"] == "desc"
        assert kwargs["prefer"] == "oldest"

    def test_unknown_directive_value_warns_and_keeps_parameter(self) -> None:
        """An unknown value is ignored with a Scryfall-shaped warning; the search still runs."""
        mock_engine, result = self._engine_search("t:goblin unique:bogus")
        assert mock_engine.query.call_args.kwargs["unique"] == "card"
        assert result["warnings"] == ['Unknown unique mode "bogus" was ignored']

    def test_directive_free_search_has_no_warnings_key(self) -> None:
        """The warnings key appears only when there is something to say."""
        _, result = self._engine_search("t:goblin")
        assert "warnings" not in result

    def test_overriding_repeat_warns_with_both_values(self) -> None:
        """`(cmc=5 prefer:oldest) (cmc=4 prefer:newest)` applies newest and SAYS so.

        The grouped-scope ambiguity from #872: a directive always applies to the whole
        search, so the losing spelling is named in a warning rather than silently dropped.
        """
        mock_engine, result = self._engine_search("(cmc=5 prefer:oldest) (cmc=4 prefer:newest)")
        assert mock_engine.query.call_args.kwargs["prefer"] == "newest"
        assert result["warnings"] == ["prefer:newest overrode the earlier prefer:oldest"]

    def test_sort_and_order_spellings_override_each_other(self) -> None:
        """sort: and order: set the same parameter, so a later one warns about the earlier."""
        mock_engine, result = self._engine_search("t:goblin sort:usd order:name")
        assert mock_engine.query.call_args.kwargs["orderby"] == "name"
        assert result["warnings"] == ["order:name overrode the earlier sort:usd"]

    def test_same_value_repeat_warns_nothing(self) -> None:
        """Repeating a directive with the same value has nothing surprising to report."""
        _, result = self._engine_search("t:goblin unique:art unique:art")
        assert "warnings" not in result

    def test_directive_inside_a_group_warns_about_its_scope(self) -> None:
        """A directive under an or-group applies globally, and the response says so."""
        mock_engine, result = self._engine_search("t:goblin or unique:art")
        assert mock_engine.query.call_args.kwargs["unique"] == "artwork"
        assert result["warnings"] == ["unique:art applies to the whole search, not only its group"]

    def test_sql_path_receives_effective_values(self) -> None:
        """The fold happens before dispatch, so the SQL fallback sees the same effective unique."""
        with (
            patch.object(self.api_resource, "_setup_complete", lambda: True),
            self._search_flags(enable_engine=False),
            patch.object(self.api_resource, "_search_sql", return_value={"cards": [], "total_cards": 0}) as mock_sql,
        ):
            result = self.api_resource._search(query="t:goblin unique:art unique:bogus")
        assert mock_sql.call_args.kwargs["unique"] == UniqueOn.ARTWORK
        assert result["warnings"] == ['Unknown unique mode "bogus" was ignored']


class TestAPIResourceStaticFileServing(unittest.TestCase):
    """Test static file serving methods."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_serve_static_file_reads_file_content(self) -> None:
        """Test _serve_static_file reads and serves file content."""
        mock_response = MagicMock()

        with patch("api.api_resource.pathlib.Path") as mock_path:
            mock_file = MagicMock()
            mock_file.open.return_value.__enter__.return_value.read.return_value = "file content"
            mock_path.return_value = mock_file

            self.api_resource._serve_static_file(filename="test.html", falcon_response=mock_response)

            assert mock_response.text == "file content"

    def test_index_html_serves_static_file(self) -> None:
        """Test _root serves the index.html file."""
        mock_response = MagicMock()

        self.api_resource._root(falcon_response=mock_response)

        # Verify the response contains HTML content
        assert mock_response.text is not None
        assert len(mock_response.text) > 0
        assert mock_response.content_type == "text/html"
        # HTML is minified before serving (see _minify_html), which may drop attribute quotes and
        # reorder attributes — check for the substantive content rather than exact tag formatting.
        assert "autocapitalize" in mock_response.text
        assert "autocorrect" in mock_response.text
        # Verify it sets cache control header
        mock_response.set_header.assert_called_with("Cache-Control", "public, max-age=3600")
        assert "og:image" in mock_response.text
        assert "/static/social-preview.webp" in mock_response.text
        assert "og:image:width" in mock_response.text
        assert "1200" in mock_response.text
        assert "og:image:height" in mock_response.text
        assert "630" in mock_response.text

    def test_index_html_with_query_embeds_search_results(self) -> None:
        """Test _root embeds search results when query parameter is provided."""
        mock_response = MagicMock()

        # Mock the _search method to return test results
        mock_search_results = {
            "cards": [{"name": "Elvish Mystic", "set_code": "m14", "collector_number": "1"}],
            "total_cards": 1,
            "query": "elf",
        }

        with patch.object(self.api_resource, "_search", return_value=mock_search_results):
            # Call _root with a search query
            self.api_resource._root(falcon_response=mock_response, q="elf")

        # Verify the response contains HTML content
        assert mock_response.text is not None
        assert len(mock_response.text) > 0
        assert mock_response.content_type == "text/html"

        # Verify that embedded search results are in the HTML (check for assignment, not just the variable name)
        assert "window.EMBEDDED_SEARCH_RESULTS = {" in mock_response.text
        assert "Elvish Mystic" in mock_response.text

        # Verify it sets appropriate cache control header (shorter for search results)
        mock_response.set_header.assert_called_with("Cache-Control", "public, max-age=90")

    def test_index_html_with_query_embeds_results_count_in_status_message(self) -> None:
        """Test _root injects the results count into the #statusMessage container.

        Regression test: the injection code used to target a stale `id="resultsCount"` div that
        no longer exists in the template (which uses `id="statusMessage"`), so the count never
        rendered for no-JS clients.
        """
        mock_response = MagicMock()

        mock_search_results = {
            "cards": [{"name": "Elvish Mystic", "set_code": "m14", "collector_number": "1"}],
            "total_cards": 1,
            "query": "elf",
        }

        with patch.object(self.api_resource, "_search", return_value=mock_search_results):
            self.api_resource._root(falcon_response=mock_response, q="elf")

        # HTML is minified before serving, which drops attribute quotes and unescapes quotes that
        # are safe in text content (see test_index_html_serves_static_file).
        assert "id=statusMessage" in mock_response.text
        assert "<!-- SERVER_SIDE_RESULTS_COUNT -->" not in mock_response.text
        assert 'Found 1 card matching "elf"' in mock_response.text

    def test_favicon_ico_serves_binary_content(self) -> None:
        """Test favicon_ico serves binary content correctly."""
        mock_response = MagicMock()

        # Test that the method exists and is callable
        assert hasattr(self.api_resource, "favicon_ico")
        assert callable(self.api_resource.favicon_ico)

        # Test that it can be called (may fail due to missing files, but that's expected)
        try:
            self.api_resource.favicon_ico(falcon_response=mock_response)
            # If it succeeds, check that response properties were set
            assert hasattr(mock_response, "content_type")
            assert hasattr(mock_response, "headers")
        except FileNotFoundError:
            # This is expected if the file doesn't exist
            pass

    def test_social_preview_webp_serves_binary_content(self) -> None:
        """Test social_preview_webp serves binary content correctly."""
        mock_response = MagicMock()
        mock_response.headers = {}

        # Reachable at the URL index.html's og:image actually points at, and only there.
        entry = self.api_resource.routes["static/social-preview.webp"]
        assert entry.action.__wrapped__ == self.api_resource.social_preview_webp

        self.api_resource.social_preview_webp(falcon_response=mock_response)

        expected_contents = (pathlib.Path(__file__).parent.parent / "static" / "social-preview.webp").read_bytes()
        assert mock_response.data == expected_contents
        assert mock_response.content_type == "image/webp"
        assert mock_response.headers["content-length"] == len(expected_contents)
        mock_response.set_header.assert_called_with("Cache-Control", "public, max-age=2592000")

    def test_robots_txt_serves_static_file(self) -> None:
        """Test robots_txt serves the robots.txt file."""
        mock_response = MagicMock()
        expected_contents = (pathlib.Path(__file__).parent.parent / "static" / "robots.txt").read_text()

        self.api_resource.robots_txt(falcon_response=mock_response)

        assert mock_response.text == expected_contents
        assert mock_response.content_type == "text/plain"


class TestHtmlMinification(unittest.TestCase):
    """_minify_html reduces page weight.

    Must not corrupt the per-request placeholders that _build_base_html's cached output still
    needs substituted afterward (SERVER_SIDE_RESULTS, SERVER_SIDE_EMBEDDED_DATA).
    """

    def setUp(self) -> None:
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_minifies_whitespace_by_default(self) -> None:
        # minify_html also drops the redundant closing </p> (valid HTML5 tag-omission), hence
        # "<div><p>x</div>" rather than a literal whitespace-only collapse.
        assert api_resource_module._minify_html("<div>   <p>x</p>   </div>") == "<div><p>x</div>"

    def test_disabled_flag_returns_input_unchanged(self) -> None:
        original = api_resource_module._MINIFY_HTML_ENABLED
        api_resource_module._MINIFY_HTML_ENABLED = False
        try:
            html = "<div>   <p>x</p>   </div>"
            assert api_resource_module._minify_html(html) == html
        finally:
            api_resource_module._MINIFY_HTML_ENABLED = original

    def test_server_side_placeholders_survive_minification(self) -> None:
        mock_response = MagicMock()
        self.api_resource._root(falcon_response=mock_response)
        assert "<!-- SERVER_SIDE_RESULTS -->" in mock_response.text
        assert "<!-- SERVER_SIDE_EMBEDDED_DATA -->" in mock_response.text

    def test_search_results_still_embed_after_minification(self) -> None:
        mock_response = MagicMock()
        mock_search_results = {
            "cards": [{"name": "Elvish Mystic", "set_code": "m14", "collector_number": "1"}],
            "total_cards": 1,
            "query": "elf",
        }
        with patch.object(self.api_resource, "_search", return_value=mock_search_results):
            self.api_resource._root(falcon_response=mock_response, q="elf")
        assert "window.EMBEDDED_SEARCH_RESULTS = {" in mock_response.text
        assert "Elvish Mystic" in mock_response.text


class TestAPIResourceErrorHandling(unittest.TestCase):
    """Test error handling in APIResource methods."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_import_card_by_name_validates_card_name_parameter(self) -> None:
        """Test import_card_by_name validates card_name parameter."""
        with pytest.raises(ValueError, match="card_name parameter is required"):
            self.api_resource.import_card_by_name(card_name="")

        with pytest.raises(ValueError, match="card_name parameter is required"):
            self.api_resource.import_card_by_name(card_name=None)

    def test_import_cards_by_search_validates_search_query_parameter(self) -> None:
        """Test import_cards_by_search validates search_query parameter."""
        with pytest.raises(ValueError, match="search_query parameter is required"):
            self.api_resource.import_cards_by_search(search_query="")

        with pytest.raises(ValueError, match="search_query parameter is required"):
            self.api_resource.import_cards_by_search(search_query=None)


class TestAPIResourceCaching(unittest.TestCase):
    """Test caching functionality in APIResource."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Store original cache setting
        self.original_cache_setting = settings.enable_cache
        # Enable caching for these tests
        settings.enable_cache = True
        # Now create the APIResource with caching enabled
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def tearDown(self) -> None:
        """Restore original cache setting."""
        settings.enable_cache = self.original_cache_setting

    @contextmanager
    def _mock_successful_upsert(self) -> Generator[None]:
        """Mock conn pool and bulk_upsert so _upsert_cards completes successfully."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        with (
            patch.object(self.api_resource, "_conn_pool") as mock_pool,
            patch(
                "api.api_resource._bulk_upsert",
                return_value={"inserted": 1, "updated": 0, "unchanged": 0},
            ),
        ):
            mock_pool.connection.return_value.__enter__.return_value = mock_conn
            yield

    def test_query_cache_clears_after_successful_load(self) -> None:
        """Test that query cache clears after successful card loading."""
        # Add some data to the cache
        self.api_resource._query_cache["test_key"] = "test_value"
        assert "test_key" in self.api_resource._query_cache

        # Mock the database operations to simulate successful load
        with self._mock_successful_upsert():
            # Provide valid card data that will pass preprocessing
            valid_card = create_test_card(
                card_id="00000000-0000-0000-0000-000000000007",
                keywords=[],
                prices={},
            )

            # Call _upsert_cards directly to test cache clearing
            self.api_resource._upsert_cards([valid_card])

            # Cache should be cleared after successful load
            assert "test_key" not in self.api_resource._query_cache

    def test_search_cache_clears_after_successful_load(self) -> None:
        """Test that search cache clears after successful card loading."""
        with self._mock_successful_upsert():
            valid_card = create_test_card(
                card_id="00000000-0000-0000-0000-000000000007",
                keywords=[],
                prices={},
            )

            gen_before = self.api_resource._cache_generation.value
            self.api_resource._upsert_cards([valid_card])
            # Generation increment is the cross-worker invalidation signal
            assert self.api_resource._cache_generation.value > gen_before

    def test_random_search_uses_engine_sample_preferred(self) -> None:
        """random_search delegates to engine.sample_preferred() when the engine is loaded."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        fake_cards = [{"name": "Lightning Bolt"}, {"name": "Counterspell"}]
        mock_engine = MagicMock()
        mock_engine.size.return_value = 2
        mock_engine.sample_preferred.return_value = fake_cards

        with patch.object(self.api_resource, "_engine", mock_engine):
            result = self.api_resource.random_search(num_cards=2)

        mock_engine.sample_preferred.assert_called_once_with(2)
        assert result["cards"] == fake_cards
        assert result["total_cards"] == 2

    def test_random_search_returns_empty_when_engine_not_loaded(self) -> None:
        """random_search returns empty result when the engine has no cards."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        mock_engine = MagicMock()
        mock_engine.size.return_value = 0

        with (
            patch.object(self.api_resource, "_engine", mock_engine),
            patch.object(self.api_resource, "_trigger_background_reload_if_needed"),
        ):
            result = self.api_resource.random_search(num_cards=1)

        assert result == {"cards": [], "total_cards": 0}
        mock_engine.sample_preferred.assert_not_called()

    def test_get_catalog_raises_503_when_engine_empty(self) -> None:
        """get_catalog raises HTTPServiceUnavailable when engine.size() == 0.

        This is the mechanism that prevents an empty 200 from being stored by
        CachingMiddleware (which skips 5xx responses).
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        mock_engine = MagicMock()
        mock_engine.size.return_value = 0

        with patch.object(self.api_resource, "_engine", mock_engine):
            with pytest.raises(falcon.HTTPServiceUnavailable):
                self.api_resource.get_catalog()

        mock_engine.common_card_types.assert_not_called()

    def test_get_catalog_returns_maps_when_engine_loaded(self) -> None:
        """get_catalog returns {types, keywords} maps when engine is ready.

        Kindred is aliased as Tribal in the types response, so both keys appear.
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        mock_engine = MagicMock()
        mock_engine.size.return_value = 4
        mock_engine.common_card_types.return_value = {
            "Creature": 5,
            "Artifact": 2,
            "Instant": 3,
            "Kindred": 4,
        }
        mock_engine.common_card_keywords.return_value = {
            "Flying": 10,
            "Haste": 3,
        }

        with patch.object(self.api_resource, "_engine", mock_engine):
            result = self.api_resource.get_catalog()

        assert result == {
            "types": {
                "Artifact": 2,
                "Creature": 5,
                "Instant": 3,
                "Kindred": 4,
                "Tribal": 4,
            },
            "keywords": {
                "flying": 10,
                "haste": 3,
            },
        }

    def test_get_catalog_keys_are_sorted(self) -> None:
        """Catalog keys come back sorted, including the post-hoc Tribal alias.

        Sorted output is deterministic and compresses ~5% smaller (adjacent keys
        share prefixes); the engine returns arbitrary (insertion) order.
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        mock_engine = MagicMock()
        mock_engine.size.return_value = 4
        mock_engine.common_card_types.return_value = {
            "Wall": 1,
            "Kindred": 4,
            "Aurochs": 2,
            "Aura": 7,
        }
        mock_engine.common_card_keywords.return_value = {
            "Vigilance": 5,
            "Flying": 10,
            "Deathtouch": 2,
        }

        with patch.object(self.api_resource, "_engine", mock_engine):
            result = self.api_resource.get_catalog()

        assert list(result["types"]) == ["Aura", "Aurochs", "Kindred", "Tribal", "Wall"]
        assert list(result["keywords"]) == ["deathtouch", "flying", "vigilance"]

    def test_cache_clear_method_works(self) -> None:
        """Test that cache.clear() method works for cachebox caches."""
        # Test query cache clearing
        self.api_resource._query_cache["test_key"] = "test_value"
        assert "test_key" in self.api_resource._query_cache

        self.api_resource._query_cache.clear()
        assert "test_key" not in self.api_resource._query_cache

        # Test that generation increment invalidates the search gen cache
        gen_before = self.api_resource._cache_generation.value
        self.api_resource._clear_caches()
        assert self.api_resource._cache_generation.value == gen_before + 1


_HOSTNAME_TESTCASES = {
    "tolarian_acade_my": {
        "expected": "Tolarian Academy",
        "raw_host": "tolarian-acade.my",
    },
    "strips_com_tld": {
        "expected": "Sylvan Librarian",
        "raw_host": "sylvan-librarian.com",
    },
    "strips_port": {
        "expected": "Sylvan Librarian",
        "raw_host": "sylvan-librarian.com:443",
    },
    "strips_www_prefix": {
        "expected": "Sylvan Librarian",
        "raw_host": "www.sylvan-librarian.com",
    },
    "strips_subdomain_com": {
        "expected": "Sylvan Librarian",
        "raw_host": "foo.sylvan-librarian.com",
    },
    "strips_subdomain_non_strip_tld": {
        "expected": "Tolarian Academy",
        "raw_host": "foo.tolarian-acade.my",
    },
    "localhost_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "localhost",
    },
    "localhost_with_port_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "localhost:8080",
    },
    "ip_address_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "192.168.1.1",
    },
    "ip_with_port_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "192.168.1.1:5000",
    },
    "empty_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "",
    },
    "invalid_chars_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": 'evil"><script>.com',
    },
    "all_hyphens_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "----",
    },
    "all_dots_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "...",
    },
}


_SPLIT_WORDS_TESTCASES: dict[str, dict] = {
    "whole_word": {
        "s": "apple",
        "expected": ["apple"],
    },
    "two_words": {
        "s": "applepie",
        "expected": ["apple", "pie"],
    },
    "three_words": {
        "s": "applebananacherry",
        "expected": ["apple", "banana", "cherry"],
    },
    "no_split_possible": {
        "s": "xyzqwerty",
        "expected": None,
    },
    "split_prefers_middle": {
        # "the" (len 3) at position 0 and "oak" (len 3) at end; "lion" in the middle is found first
        "s": "thelionoak",
        "expected": ["the", "lion", "oak"],
    },
    "prefers_fewest_words": {
        # Two valid splits: ["abcde", "fghij"] (k=5, center) and ["abc", "de", "fghij"] (k=3).
        # Middle-out tries k=5 first and commits to the 2-word split.
        "s": "abcdefghij",
        "expected": ["abcde", "fghij"],
    },
}

_SMALL_WORDS: frozenset[str] = frozenset(
    ["apple", "pie", "banana", "cherry", "the", "lion", "oak", "abcde", "fghij", "abc", "de", "sylvan", "librarian"]
)


class TestSplitWords:
    """Tests for _split_words() using a controlled word set."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(_SPLIT_WORDS_TESTCASES.values()))),
        argvalues=[[v for k, v in sorted(_SPLIT_WORDS_TESTCASES[name].items())] for name in sorted(_SPLIT_WORDS_TESTCASES)],
        ids=sorted(_SPLIT_WORDS_TESTCASES),
    )
    def test_split_words(self, expected: list[str] | None, s: str) -> None:
        assert _split_words(s, _SMALL_WORDS) == expected


_HOSTNAME_DICT_TESTCASES: dict[str, dict] = {
    "no_dash_splits_into_words": {
        "expected": "Sylvan Librarian",
        "raw_host": "sylvanlibrarian.com",
    },
}


class TestHostnameSiteNameWithDict:
    """Tests for hostname_to_site_name() with a controlled word set patched in."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(_HOSTNAME_DICT_TESTCASES.values()))),
        argvalues=[[v for k, v in sorted(_HOSTNAME_DICT_TESTCASES[name].items())] for name in sorted(_HOSTNAME_DICT_TESTCASES)],
        ids=sorted(_HOSTNAME_DICT_TESTCASES),
    )
    def test_hostname_to_site_name_with_dict(self, monkeypatch: pytest.MonkeyPatch, expected: str, raw_host: str) -> None:
        monkeypatch.setattr("api.api_resource._WORDS", _SMALL_WORDS)
        _hostname_to_site_name.cache_clear()
        assert hostname_to_site_name(raw_host) == expected


class TestHostnameSiteName:
    """Tests for hostname_to_site_name()."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(_HOSTNAME_TESTCASES.values()))),
        argvalues=[[v for k, v in sorted(_HOSTNAME_TESTCASES[name].items())] for name in sorted(_HOSTNAME_TESTCASES)],
        ids=sorted(_HOSTNAME_TESTCASES),
    )
    def test_hostname_to_site_name(self, expected: str, raw_host: str) -> None:
        assert hostname_to_site_name(raw_host) == expected


class TestRootSiteNameInjection(unittest.TestCase):
    """Tests that _root injects the derived site name into the HTML."""

    def setUp(self) -> None:
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = MagicMock()

    def test_valid_hostname_replaces_fallback_in_html(self) -> None:
        mock_response = MagicMock()
        self.api_resource._root(falcon_response=mock_response, request_host="tolarian-acade.my")
        assert "Tolarian Academy" in mock_response.text
        assert FALLBACK_SITE_NAME not in mock_response.text

    def test_localhost_keeps_fallback_in_html(self) -> None:
        mock_response = MagicMock()
        self.api_resource._root(falcon_response=mock_response, request_host="localhost")
        assert FALLBACK_SITE_NAME in mock_response.text


class TestCardSiteNameInjection(unittest.TestCase):
    """Tests that card() injects the derived site name into card page HTML."""

    def setUp(self) -> None:
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = MagicMock()

    def test_valid_hostname_replaces_fallback_in_card_html(self) -> None:
        mock_response = MagicMock()
        self.api_resource.card(falcon_response=mock_response, request_host="tolarian-acade.my")
        assert "Tolarian Academy" in mock_response.text
        assert FALLBACK_SITE_NAME not in mock_response.text

    def test_localhost_keeps_fallback_in_card_html(self) -> None:
        mock_response = MagicMock()
        self.api_resource.card(falcon_response=mock_response, request_host="localhost")
        assert FALLBACK_SITE_NAME in mock_response.text


if __name__ == "__main__":
    unittest.main()


class TestMethodAwareCaching(TestBaseAPIResourceTest):
    """A refused method must not poison the cache for a method the route does answer.

    Before routes declared their methods, every method reached the same handler and returned the
    same response, so a cache key without the method was harmless. Declaring methods makes a
    response method-dependent: a POST to a GET-only route now 405s, and that 405 would be stored
    under the key a subsequent GET looks up.
    """

    def _client(self) -> falcon.testing.TestClient:
        app = falcon.App(middleware=[CachingMiddleware()])
        app.add_sink(self.api_resource._handle, prefix="/")
        return falcon.testing.TestClient(app)

    @contextmanager
    def _cache_enabled(self) -> Generator[None]:
        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            yield

    def test_refused_post_does_not_poison_a_later_get(self) -> None:
        client = self._client()
        with self._cache_enabled():
            refused = client.simulate_post("/get_pid")
            after = client.simulate_get("/get_pid")

        assert refused.status == falcon.HTTP_405
        assert after.status == falcon.HTTP_200
        assert after.headers.get("X-Cache") == "miss"

        # The two properties this case depends on, asserted so that changing either one fails here
        # rather than leaving the test passing for a reason it was not written to check.
        # 1. The 405 is raised before the handler runs, so it carries none of get_pid's cache
        #    headers and was therefore storable — that is what made poisoning possible at all.
        assert "no-store" not in (refused.headers.get("Cache-Control") or "")
        # 2. get_pid itself opts out of caching, so the route being poisoned is one that could
        #    never have populated that key through its own successful response.
        assert "no-store" in (after.headers.get("Cache-Control") or "")

    def test_get_is_still_cached_across_requests(self) -> None:
        # /robots.txt rather than /get_pid: get_pid sets Cache-Control: no-store, which is also why
        # it makes a good poisoning case — the 405 is raised before the handler can set that header.
        client = self._client()
        with self._cache_enabled():
            first = client.simulate_get("/robots.txt")
            second = client.simulate_get("/robots.txt")

        assert first.headers.get("X-Cache") == "miss"
        assert second.headers.get("X-Cache") == "hit"
        assert second.status == falcon.HTTP_200
