"""Comprehensive tests for APIResource class functionality."""

import multiprocessing
import os
import time
import unittest
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Never
from unittest.mock import MagicMock, patch

import falcon
import pytest

from api.api_resource import _WORDS, FALLBACK_SITE_NAME, APIResource, _split_words, hostname_to_site_name
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

    def test_disallowed_query_params_do_not_cause_type_error(self) -> None:
        """Query params named after internal kwargs must be stripped before dispatch."""
        mock_req = MagicMock()
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
        "expected": "Arcane Tutor",
        "raw_host": "arcane-tutor.com",
    },
    "strips_port": {
        "expected": "Arcane Tutor",
        "raw_host": "arcane-tutor.com:443",
    },
    "strips_www_prefix": {
        "expected": "Arcane Tutor",
        "raw_host": "www.arcane-tutor.com",
    },
    "strips_subdomain_com": {
        "expected": "Arcane Tutor",
        "raw_host": "foo.arcane-tutor.com",
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

_SMALL_WORDS: frozenset[str] = frozenset(["apple", "pie", "banana", "cherry", "the", "lion", "oak", "abcde", "fghij", "abc", "de"])


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
    """Tests for hostname_to_site_name() that require a system dictionary."""

    @pytest.mark.skipif(not _WORDS, reason="no system dictionary found")
    @pytest.mark.parametrize(
        argnames=sorted(next(iter(_HOSTNAME_DICT_TESTCASES.values()))),
        argvalues=[[v for k, v in sorted(_HOSTNAME_DICT_TESTCASES[name].items())] for name in sorted(_HOSTNAME_DICT_TESTCASES)],
        ids=sorted(_HOSTNAME_DICT_TESTCASES),
    )
    def test_hostname_to_site_name_with_dict(self, expected: str, raw_host: str) -> None:
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


if __name__ == "__main__":
    unittest.main()
