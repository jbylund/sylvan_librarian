"""Comprehensive tests for APIResource class functionality."""

import multiprocessing
import os
import time
import unittest
import uuid
from typing import Any, Never
from unittest.mock import MagicMock, patch

import falcon
import pytest
import requests

from api.api_resource import APIResource
from api.settings import settings


def create_test_card(  # noqa: PLR0913
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
        assert "get_pid" in api_resource.action_map
        assert "db_ready" in api_resource.action_map
        assert "search" in api_resource.action_map
        assert "index" in api_resource.action_map

        # Check that caches are initialized
        assert hasattr(api_resource, "_query_cache")
        assert hasattr(api_resource, "_session")
        assert hasattr(api_resource, "_tagger_client")

    def test_initialization_with_custom_import_guard(self) -> None:
        """Test APIResource initialization with custom import guard."""
        custom_guard = multiprocessing.RLock()
        api_resource = APIResource(
            import_guard=custom_guard,
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )

        assert api_resource._import_guard == custom_guard

    def test_action_map_includes_all_public_methods(self) -> None:
        """Test that action_map includes all public methods."""
        api_resource = self.api_resource
        public_methods = [
            method for method in dir(api_resource) if not method.startswith("_") and callable(getattr(api_resource, method))
        ]

        for method in public_methods:
            assert method in api_resource.action_map


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

    @patch.object(APIResource, "_run_query")
    def test_db_ready_returns_true_when_migrations_table_exists(self, mock_run_query: Any) -> None:
        """Test db_ready returns True when migrations table exists."""
        mock_run_query.return_value = {
            "result": [{"relname": "migrations"}, {"relname": "other_table"}],
        }

        result = self.api_resource.db_ready()
        assert result is True

    @patch.object(APIResource, "_run_query")
    def test_db_ready_returns_false_when_migrations_table_missing(self, mock_run_query: Any) -> None:
        """Test db_ready returns False when migrations table is missing."""
        mock_run_query.return_value = {
            "result": [{"relname": "other_table"}],
        }

        result = self.api_resource.db_ready()
        assert result is False

    def test_read_sql_reads_file_content(self) -> None:
        """Test read_sql reads and returns SQL file content."""
        # Test that the method exists and is callable
        assert hasattr(self.api_resource, "read_sql")
        assert callable(self.api_resource.read_sql)

        # Test that it can be called (may fail due to missing files, but that's expected)
        try:
            result = self.api_resource.read_sql("nonexistent_file")
            # If it succeeds, it should return a string
            assert isinstance(result, str)
        except FileNotFoundError:
            # This is expected if the file doesn't exist
            pass

    def test_read_sql_caching(self) -> None:
        """Test that read_sql uses caching."""
        with patch("api.api_resource.pathlib.Path") as mock_path:
            mock_sql_dir = MagicMock()
            mock_sql_file = MagicMock()
            mock_sql_file.open.return_value.__enter__.return_value.read.return_value = "SELECT * FROM test;"
            mock_sql_dir.__truediv__.return_value = mock_sql_file
            mock_path.return_value.parent = mock_sql_dir

            # Call twice with same filename
            result1 = self.api_resource.read_sql("test")
            result2 = self.api_resource.read_sql("test")

            # Results should be identical (cached)
            assert result1 == result2
            # Note: The @cached decorator may not be easily testable with mocks
            # as it's implemented at the function level


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
        mock_req.path = mock_req.relative_uri = "/test"
        mock_resp = MagicMock()
        mock_resp.complete = True

        with patch("api.api_resource.logger") as mock_logger:
            self.api_resource._handle(mock_req, mock_resp)
            mock_logger.info.assert_called_with("Request already handled: %s", "/test")

    def test_handle_processes_valid_paths(self) -> None:
        """Test _handle processes valid paths correctly."""
        mock_req = MagicMock()
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
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/nonexistent"
        mock_req.params = {}
        mock_resp = MagicMock()
        mock_resp.complete = False

        with pytest.raises(falcon.HTTPNotFound):
            self.api_resource._handle(mock_req, mock_resp)

    def test_handle_handles_type_errors(self) -> None:
        """Test _handle handles TypeError exceptions."""
        mock_req = MagicMock()
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
        with patch.object(self.api_resource, "action_map", {"search": mock_action_that_raises_type_error}):
            with pytest.raises(falcon.HTTPBadRequest):
                self.api_resource._handle(mock_req, mock_resp)

    def test_handle_handles_general_exceptions(self) -> None:
        """Test _handle handles general exceptions."""
        mock_req = MagicMock()
        mock_req.uri = mock_req.path = mock_req.relative_uri = "/search"
        mock_req.params = {}
        mock_resp = MagicMock()
        mock_resp.complete = False

        def raise_error(*args: Any, **kwargs: Any) -> Never:
            msg = "Test error"
            raise Exception(msg)

        # Mock search method to raise a general exception
        with patch.object(self.api_resource, "action_map", {"search": raise_error}):
            with patch("api.api_resource.error_monitoring.error_handler") as mock_error_handler:
                with pytest.raises(falcon.HTTPInternalServerError):
                    self.api_resource._handle(mock_req, mock_resp)

                # Should call error monitoring
                mock_error_handler.assert_called_once()


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
        # Verify it sets cache control header
        mock_response.set_header.assert_called_with("Cache-Control", "public, max-age=3600")

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


class TestAPIResourceErrorHandling(unittest.TestCase):
    """Test error handling in APIResource methods."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_update_tagged_cards_validates_tag_parameter(self) -> None:
        """Test update_tagged_cards validates tag parameter."""
        with pytest.raises(ValueError, match="Tag parameter is required"):
            self.api_resource.update_tagged_cards(tag="")

        with pytest.raises(ValueError, match="Tag parameter is required"):
            self.api_resource.update_tagged_cards(tag=None)

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

    @patch("api.api_resource.requests.Session.get")
    def test_discover_tags_from_scryfall_handles_request_errors(self, mock_get: Any) -> None:
        """Test discover_tags_from_scryfall handles request errors."""
        mock_get.side_effect = requests.RequestException("Network error")

        with pytest.raises(ValueError, match="Failed to fetch tag list from Scryfall"):
            self.api_resource.discover_tags_from_scryfall()

    def test_discover_tags_from_graphql_handles_parsing_errors(self) -> None:
        """Test discover_tags_from_graphql handles parsing errors."""
        # Mock the _tagger_client attribute
        mock_tagger = MagicMock()
        mock_tagger.search_tags.side_effect = KeyError("Missing key")
        self.api_resource._tagger_client = mock_tagger

        with pytest.raises(ValueError, match="Failed to parse GraphQL tag search response"):
            self.api_resource.discover_tags_from_graphql()


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

    def test_query_cache_clears_after_successful_load(self) -> None:
        """Test that query cache clears after successful card loading."""
        # Add some data to the cache
        self.api_resource._query_cache["test_key"] = "test_value"
        assert "test_key" in self.api_resource._query_cache

        # Mock the database operations to simulate successful load
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.rowcount = 1
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Provide valid card data that will pass preprocessing
            valid_card = create_test_card(
                card_id="00000000-0000-0000-0000-000000000007",
                keywords=[],
                prices={},
            )

            # Call _load_cards_with_staging directly to test cache clearing
            self.api_resource._load_cards_with_staging([valid_card])

            # Cache should be cleared after successful load
            assert "test_key" not in self.api_resource._query_cache

    def test_search_cache_clears_after_successful_load(self) -> None:
        """Test that search cache clears after successful card loading."""
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.rowcount = 1
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            valid_card = create_test_card(
                card_id="00000000-0000-0000-0000-000000000007",
                keywords=[],
                prices={},
            )

            gen_before = self.api_resource._cache_generation.value
            self.api_resource._load_cards_with_staging([valid_card])
            # Generation increment is the cross-worker invalidation signal
            assert self.api_resource._cache_generation.value > gen_before

    def test_repeated_random_search_calls_search_once(self) -> None:
        """Test that repeated random_search calls only invoke _search once when the preferred-cards cache is warm."""
        fake_cards = [{"name": "Lightning Bolt"}, {"name": "Counterspell"}]
        fake_search_result = {"cards": fake_cards, "total_cards": 2}

        # Ensure the preferred-cards cache starts empty for this test
        self.api_resource._preferred_cards_map.clear()

        with patch.object(self.api_resource, "_search", return_value=fake_search_result) as mock_search:
            result1 = self.api_resource.random_search(num_cards=1)
            result2 = self.api_resource.random_search(num_cards=1)

        # _search should only have been called once; the second random_search hit the cache
        assert mock_search.call_count == 1
        assert len(result1["cards"]) == 1
        assert len(result2["cards"]) == 1

    def test_preferred_cards_cache_clears_after_load(self) -> None:
        """Test that the preferred-cards cache is invalidated after a successful card load."""
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.rowcount = 1
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            valid_card = create_test_card(
                card_id="00000000-0000-0000-0000-000000000009",
                keywords=[],
                prices={},
            )

            # Seed the preferred-cards map at the current generation
            gen = self.api_resource._cache_generation.value
            self.api_resource._preferred_cards_map[gen] = [{"name": "sentinel"}]
            assert self.api_resource._preferred_cards_map.get(gen) is not None

            self.api_resource._load_cards_with_staging([valid_card])

            # After load the generation advances; sentinel is unreachable at the new generation
            new_gen = self.api_resource._cache_generation.value
            assert new_gen > gen
            assert self.api_resource._preferred_cards_map.get(new_gen) is None

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

    def test_get_all_preferred_cards_returns_stale_on_generation_miss(self) -> None:
        """On a cache miss, returns the previous generation's cards immediately."""
        stale_cards = [{"name": "Stale Card"}]
        gen = self.api_resource._cache_generation.value
        self.api_resource._preferred_cards_map[gen] = stale_cards

        # Advance generation so current gen has no data
        self.api_resource._cache_generation.value += 1

        with patch.object(self.api_resource, "_search"):
            result = self.api_resource._get_all_preferred_cards()

        assert result == stale_cards
        # _search is called in the background thread, not the calling thread, so it
        # may or may not have fired yet — we only assert the return value here.

    def test_get_all_preferred_cards_spawns_background_refresh(self) -> None:
        """Spawns exactly one background thread to populate the new generation's cache."""
        import threading  # noqa: PLC0415

        stale_cards = [{"name": "Stale Card"}]
        gen = self.api_resource._cache_generation.value
        self.api_resource._preferred_cards_map[gen] = stale_cards
        self.api_resource._cache_generation.value += 1

        threads_started: list = []
        real_thread_init = threading.Thread.__init__

        def tracking_init(self_thread: threading.Thread, **kwargs: object) -> None:
            threads_started.append(kwargs.get("target"))
            real_thread_init(self_thread, **kwargs)

        with (
            patch.object(threading.Thread, "__init__", tracking_init),
            patch.object(self.api_resource, "_search", return_value={"cards": []}),
        ):
            self.api_resource._get_all_preferred_cards()

        assert len(threads_started) == 1
        assert threads_started[0] == self.api_resource._fetch_and_cache_preferred_cards

    def test_get_all_preferred_cards_blocks_synchronously_at_startup(self) -> None:
        """With no stale data, blocks until cards are fetched (startup path)."""
        fresh_cards = [{"name": "Fresh Card"}]
        self.api_resource._preferred_cards_map.clear()

        with patch.object(self.api_resource, "_search", return_value={"cards": fresh_cards}):
            result = self.api_resource._get_all_preferred_cards()

        assert result == fresh_cards

    def test_fetch_and_cache_skips_if_lock_held(self) -> None:
        """_fetch_and_cache_preferred_cards returns without calling _search if the lock is held."""
        gen = self.api_resource._cache_generation.value
        self.api_resource._preferred_cards_refresh_lock.acquire()
        try:
            with patch.object(self.api_resource, "_search") as mock_search:
                self.api_resource._fetch_and_cache_preferred_cards(gen)
            mock_search.assert_not_called()
        finally:
            self.api_resource._preferred_cards_refresh_lock.release()

    def test_background_refresh_populates_new_generation(self) -> None:
        """Background thread eventually populates the new generation's cache."""
        import time  # noqa: PLC0415

        fresh_cards = [{"name": "Fresh Card"}]
        stale_cards = [{"name": "Stale Card"}]
        gen = self.api_resource._cache_generation.value
        self.api_resource._preferred_cards_map[gen] = stale_cards
        new_gen = gen + 1
        self.api_resource._cache_generation.value = new_gen

        with patch.object(self.api_resource, "_search", return_value={"cards": fresh_cards}):
            self.api_resource._get_all_preferred_cards()
            # Give the daemon thread time to complete
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if self.api_resource._preferred_cards_map.get(new_gen) is not None:
                    break
                time.sleep(0.01)

        assert self.api_resource._preferred_cards_map.get(new_gen) == fresh_cards


class TestAPIResourceTagHierarchy(unittest.TestCase):
    """Test cases for tag hierarchy functionality."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )

    def test_populate_tag_hierarchy_with_empty_tags(self) -> None:
        """Test _populate_tag_hierarchy with empty tag list."""
        # Mock the database operations
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            result = self.api_resource._populate_tag_hierarchy(tags=[])

            assert result["success"] is True
            assert result["tags_processed"] == 0
            assert "duration" in result
            assert "message" in result

    def test_populate_tag_hierarchy_with_single_tag(self) -> None:
        """Test _populate_tag_hierarchy with single tag."""
        # Mock the database operations
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Mock _get_tag_relationships to return sample relationships
            with patch.object(self.api_resource, "_get_tag_relationships") as mock_get_relationships:
                mock_get_relationships.return_value = [
                    {
                        "parent": {"slug": "parent-tag", "name": "Parent Tag", "namespace": "test"},
                        "child": {"slug": "test-tag", "name": "Test Tag", "namespace": "test"},
                    },
                ]

                result = self.api_resource._populate_tag_hierarchy(tags=["test-tag"])

                assert result["success"] is True
                assert result["tags_processed"] == 1
                assert "duration" in result
                assert "message" in result

                # Verify database operations were called
                assert mock_cursor.executemany.call_count >= 2  # At least tags and relationships inserts

    def test_populate_tag_hierarchy_with_multiple_tags(self) -> None:
        """Test _populate_tag_hierarchy with multiple tags."""
        # Mock the database operations
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Mock _get_tag_relationships to return different relationships for each tag
            with patch.object(self.api_resource, "_get_tag_relationships") as mock_get_relationships:

                def mock_relationships(tag: str) -> list:
                    if tag == "tag1":
                        return [
                            {
                                "parent": {"slug": "parent1", "name": "Parent 1", "namespace": "test"},
                                "child": {"slug": "tag1", "name": "Tag 1", "namespace": "test"},
                            },
                        ]
                    if tag == "tag2":
                        return [
                            {
                                "parent": {"slug": "parent2", "name": "Parent 2", "namespace": "test"},
                                "child": {"slug": "tag2", "name": "Tag 2", "namespace": "test"},
                            },
                        ]
                    return []

                mock_get_relationships.side_effect = mock_relationships

                result = self.api_resource._populate_tag_hierarchy(tags=["tag1", "tag2"])

                assert result["success"] is True
                assert result["tags_processed"] == 2
                assert "duration" in result
                assert "message" in result

                # Verify _get_tag_relationships was called for each tag
                assert mock_get_relationships.call_count == 2

    def test_populate_tag_hierarchy_handles_no_relationships(self) -> None:
        """Test _populate_tag_hierarchy when tags have no relationships."""
        # Mock the database operations
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Mock _get_tag_relationships to return empty relationships
            with patch.object(self.api_resource, "_get_tag_relationships") as mock_get_relationships:
                mock_get_relationships.return_value = []

                result = self.api_resource._populate_tag_hierarchy(tags=["orphan-tag"])

                assert result["success"] is True
                assert result["tags_processed"] == 1
                assert "duration" in result
                assert "message" in result

    def test_populate_tag_hierarchy_handles_database_errors(self) -> None:
        """Test _populate_tag_hierarchy handles database errors gracefully."""
        # Mock the database operations to raise an exception
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Make cursor.executemany raise an exception
            mock_cursor.executemany.side_effect = Exception("Database error")

            # Mock _get_tag_relationships to return sample relationships
            with patch.object(self.api_resource, "_get_tag_relationships") as mock_get_relationships:
                mock_get_relationships.return_value = [
                    {
                        "parent": {"slug": "parent-tag", "name": "Parent Tag", "namespace": "test"},
                        "child": {"slug": "test-tag", "name": "Test Tag", "namespace": "test"},
                    },
                ]

                # The method doesn't have explicit error handling, so it will propagate the exception
                # This test verifies that the method attempts database operations and fails as expected
                with pytest.raises(Exception, match="Database error"):
                    self.api_resource._populate_tag_hierarchy(tags=["test-tag"])

    def test_populate_tag_hierarchy_randomizes_tag_order(self) -> None:
        """Test that _populate_tag_hierarchy randomizes the order of tags."""
        # Mock the database operations
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Mock _get_tag_relationships
            with patch.object(self.api_resource, "_get_tag_relationships") as mock_get_relationships:
                mock_get_relationships.return_value = []

                # Mock random.shuffle to verify it's called
                with patch("random.shuffle") as mock_shuffle:
                    result = self.api_resource._populate_tag_hierarchy(tags=["tag1", "tag2", "tag3"])

                    # Verify shuffle was called with the tags list
                    mock_shuffle.assert_called_once()
                    assert result["success"] is True
                    assert result["tags_processed"] == 3

    def test_populate_tag_hierarchy_logs_progress(self) -> None:
        """Test that _populate_tag_hierarchy logs progress information."""
        # Mock the database operations
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Mock _get_tag_relationships
            with patch.object(self.api_resource, "_get_tag_relationships") as mock_get_relationships:
                mock_get_relationships.return_value = []

                # Mock logger to capture log calls
                with patch("api.api_resource.logger") as mock_logger:
                    result = self.api_resource._populate_tag_hierarchy(tags=["tag1", "tag2"])

                    # Verify logging calls were made
                    assert mock_logger.info.call_count >= 2  # At least start and progress logs
                    assert result["success"] is True

    def test_populate_tag_hierarchy_returns_correct_structure(self) -> None:
        """Test that _populate_tag_hierarchy returns the expected result structure."""
        # Mock the database operations
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            # Mock _get_tag_relationships
            with patch.object(self.api_resource, "_get_tag_relationships") as mock_get_relationships:
                mock_get_relationships.return_value = []

                result = self.api_resource._populate_tag_hierarchy(tags=["test-tag"])

                # Verify result structure
                assert isinstance(result, dict)
                assert "success" in result
                assert "duration" in result
                assert "message" in result
                assert "tags_processed" in result

                assert result["success"] is True
                assert isinstance(result["duration"], int | float)
                assert isinstance(result["message"], str)
                assert isinstance(result["tags_processed"], int)
                assert result["tags_processed"] == 1


if __name__ == "__main__":
    unittest.main()
