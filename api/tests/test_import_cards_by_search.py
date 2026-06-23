"""Tests for the import_cards_by_search functionality."""

# ruff: noqa: PT011

import multiprocessing
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from api.api_resource import APIResource


class TestImportCardsBySearch(unittest.TestCase):
    """Test cases for import_cards_by_search functionality."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_import_cards_by_search_function_exists(self) -> None:
        """Test that import_cards_by_search method exists and is callable."""
        assert hasattr(self.api_resource, "import_cards_by_search")
        assert callable(self.api_resource.import_cards_by_search)

    def test_import_cards_by_search_validates_input(self) -> None:
        """Test that import_cards_by_search validates search_query parameter."""
        with pytest.raises(ValueError) as context:
            self.api_resource.import_cards_by_search(search_query="")

        assert str(context.value) == "search_query parameter is required"

        with pytest.raises(ValueError) as context:
            self.api_resource.import_cards_by_search(search_query=None)

        assert str(context.value) == "search_query parameter is required"

    @patch.object(APIResource, "_scryfall_search")
    def test_import_cards_by_search_returns_not_found_for_empty_results(self, mock_search: MagicMock) -> None:
        """Test that import_cards_by_search returns not_found status when no cards are found."""
        # Mock Scryfall API to return empty list
        mock_search.return_value = []

        result = self.api_resource.import_cards_by_search(search_query="name:NonexistentCard")

        assert result["status"] == "not_found"
        assert result["search_query"] == "name:NonexistentCard"
        assert "No cards found for search query" in result["message"]
        assert result["cards_loaded"] == 0

    @patch.object(APIResource, "_scryfall_search")
    def test_import_cards_by_search_returns_error_for_scryfall_exceptions(self, mock_search: MagicMock) -> None:
        """Test that import_cards_by_search returns error status for Scryfall API exceptions."""
        # Mock Scryfall API to raise exception
        mock_search.side_effect = ValueError("API Error")

        result = self.api_resource.import_cards_by_search(search_query="name:TestCard")

        assert result["status"] == "error"
        assert result["search_query"] == "name:TestCard"
        assert "Error fetching cards from Scryfall" in result["message"]
        assert result["cards_loaded"] == 0

    @patch.object(APIResource, "_scryfall_search")
    @patch.object(APIResource, "_upsert_cards")
    def test_import_cards_by_search_returns_success_for_valid_cards(self, mock_load: MagicMock, mock_search: MagicMock) -> None:
        """Test that import_cards_by_search returns success status for valid cards."""
        # Mock Scryfall API to return card data
        mock_search.return_value = [
            {"name": "Lightning Bolt", "cmc": 1},
            {"name": "Counterspell", "cmc": 2},
        ]

        # Mock _upsert_cards to return success
        mock_load.return_value = {
            "status": "success",
            "cards_loaded": 2,
            "message": "Successfully loaded 2 cards",
        }

        result = self.api_resource.import_cards_by_search(search_query="cmc<=2")

        assert result["status"] == "success"
        assert result["search_query"] == "cmc<=2"
        assert result["cards_loaded"] == 2
        assert "Successfully loaded 2 cards" in result["message"]

    def test_import_cards_by_search_handles_artist_search_example(self) -> None:
        """Test the example artist search mentioned in the issue."""
        with (
            patch.object(self.api_resource, "_scryfall_search") as mock_search,
            patch.object(
                self.api_resource,
                "_upsert_cards",
            ) as mock_load,
        ):
            # Mock Scryfall API to return Sun Titan cards by Todd Lockwood
            mock_search.return_value = [
                {
                    "name": "Sun Titan",
                    "artist": "Todd Lockwood",
                    "set": "m12",
                    "cmc": 6,
                },
            ]

            # Mock successful loading
            mock_load.return_value = {
                "status": "success",
                "cards_loaded": 1,
                "message": "Successfully loaded 1 cards",
            }

            result = self.api_resource.import_cards_by_search(search_query="artist:lockwood game:paper sun titan")

            assert result["status"] == "success"
            assert result["search_query"] == "artist:lockwood game:paper sun titan"
            assert result["cards_loaded"] == 1
            mock_search.assert_called_once_with(query="artist:lockwood game:paper sun titan")


if __name__ == "__main__":
    unittest.main()
