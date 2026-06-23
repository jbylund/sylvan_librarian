"""Tests for the import_card_by_name functionality."""

# ruff: noqa: PT011

import multiprocessing
import time
import unittest
from unittest.mock import MagicMock, patch

import orjson
import pytest
import requests

from api.api_resource import APIResource


class TestImportCardByName(unittest.TestCase):
    """Test cases for import_card_by_name functionality."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.mock_conn_pool = MagicMock()
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._conn_pool = self.mock_conn_pool

    def test_import_card_by_name_validates_input(self) -> None:
        """Test that import_card_by_name validates card_name parameter."""
        with pytest.raises(ValueError) as context:
            self.api_resource.import_card_by_name(card_name="")

        assert str(context.value) == "card_name parameter is required"

        with pytest.raises(ValueError) as context:
            self.api_resource.import_card_by_name(card_name=None)

        assert str(context.value) == "card_name parameter is required"

    def test_import_card_by_name_function_exists(self) -> None:
        """Test that import_card_by_name method exists and is callable."""
        assert hasattr(self.api_resource, "import_card_by_name")
        assert callable(self.api_resource.import_card_by_name)

    def test_scryfall_search_function_exists(self) -> None:
        """Test that _scryfall_search method exists."""
        assert hasattr(self.api_resource, "_scryfall_search")
        assert callable(self.api_resource._scryfall_search)

    def test_upsert_cards_function_exists(self) -> None:
        """Test that _upsert_cards method exists."""
        assert hasattr(self.api_resource, "_upsert_cards")
        assert callable(self.api_resource._upsert_cards)

    @patch("requests.Session.get")
    def test_scryfall_search_returns_empty_for_404(self, mock_get: MagicMock) -> None:
        """Test that _scryfall_search returns empty list for 404 responses."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
        mock_get.return_value = mock_response

        result = self.api_resource._scryfall_search(query="name:'NonexistentCard'")

        assert result == []

    @patch("requests.Session.get")
    def test_scryfall_search_returns_data_for_success(self, mock_get: MagicMock) -> None:
        """Test that _scryfall_search returns card data for successful responses."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = orjson.dumps(
            {
                "data": [{"name": "Lightning Bolt", "cmc": 1}],
                "has_more": False,
            },
        )
        mock_get.return_value = mock_response

        result = self.api_resource._scryfall_search(query="name:'Lightning Bolt'")

        assert result == [{"name": "Lightning Bolt", "cmc": 1}]
        mock_get.assert_called_once_with(
            "https://api.scryfall.com/cards/search",
            params={
                "q": "(name:'Lightning Bolt') (f:m or f:l or f:c or f:v) game:paper unique:prints",
                "format": "json",
            },
            timeout=30,
        )

    @patch("requests.Session.get")
    def test_scryfall_search_raises_for_request_errors(self, mock_get: MagicMock) -> None:
        """Test that _scryfall_search raises exception for request errors."""
        mock_get.side_effect = requests.RequestException("Network error")

        with pytest.raises(ValueError, match="Failed to fetch data from Scryfall API"):
            self.api_resource._scryfall_search(query="name:'Lightning Bolt'")

    @patch.object(APIResource, "_run_query")
    def test_import_card_by_name_returns_already_exists_for_existing_card(self, mock_run_query: MagicMock) -> None:
        """Test that import_card_by_name returns already_exists status for existing cards."""
        # Mock _run_query to return existing card
        mock_run_query.return_value = {"result": [{"card_name": "Lightning Bolt"}]}

        result = self.api_resource.import_card_by_name(card_name="Lightning Bolt")

        assert result["status"] == "already_exists"
        assert result["card_name"] == "Lightning Bolt"
        assert "already exists in database" in result["message"]

    @patch.object(APIResource, "_run_query")
    @patch.object(APIResource, "_scryfall_search")
    def test_import_card_by_name_returns_not_found_for_missing_card(
        self,
        mock_search: MagicMock,
        mock_run_query: MagicMock,
    ) -> None:
        """Test that import_card_by_name returns not_found status when card doesn't exist in Scryfall."""
        # Mock _run_query to return no existing card
        mock_run_query.return_value = {"result": []}

        # Mock Scryfall API to return empty list (not found)
        mock_search.return_value = []

        result = self.api_resource.import_card_by_name(card_name="NonexistentCard")

        assert result["status"] == "not_found"
        assert result["search_query"] == '!"NonexistentCard"'
        assert "No cards found for search query" in result["message"]

    @patch.object(APIResource, "_run_query")
    @patch.object(APIResource, "_scryfall_search")
    def test_import_card_by_name_returns_error_for_scryfall_exceptions(
        self,
        mock_search: MagicMock,
        mock_run_query: MagicMock,
    ) -> None:
        """Test that import_card_by_name returns error status for Scryfall API exceptions."""
        # Mock _run_query to return no existing card
        mock_run_query.return_value = {"result": []}

        # Mock Scryfall API to raise exception
        mock_search.side_effect = ValueError("API Error")

        result = self.api_resource.import_card_by_name(card_name="TestCard")

        assert result["status"] == "error"
        assert result["search_query"] == '!"TestCard"'
        assert "Error fetching cards from Scryfall" in result["message"]

    @patch.object(APIResource, "_run_query")
    @patch.object(APIResource, "_scryfall_search")
    @patch("api.card_processing.preprocess_card")
    def test_import_card_by_name_returns_filtered_out_for_invalid_cards(
        self,
        mock_preprocess: MagicMock,
        mock_search: MagicMock,
        mock_run_query: MagicMock,
    ) -> None:
        """Test that import_card_by_name returns filtered_out status for cards filtered during preprocessing."""
        # Mock _run_query to return no existing card
        mock_run_query.return_value = {"result": []}

        # Mock Scryfall API to return card data
        mock_search.return_value = [{"name": "TestCard", "legalities": {"standard": "not_legal"}}]

        # Mock preprocessing to return None (filtered out)
        mock_preprocess.return_value = []

        result = self.api_resource.import_card_by_name(card_name="TestCard")
        assert result == {
            "status": "no_cards_after_preprocessing",
            "cards_loaded": 0,
            "cards_sent": 0,
            "message": "No cards remaining after preprocessing",
            "search_query": '!"TestCard"',
        }


if __name__ == "__main__":
    unittest.main()
