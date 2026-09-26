"""Tests for the import_card_by_name functionality."""

# ruff: noqa: PT011

import unittest
from unittest.mock import MagicMock, patch

import orjson
import pytest
import requests

from api.admin_resource import AdminResource
from api.api_resource import APIResource
from api.tests.support import mock_app_context


class TestImportCardByName(unittest.TestCase):
    """Test cases for import_card_by_name functionality."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.app_context = mock_app_context()
        self.mock_conn_pool = self.app_context.reader_pool
        self.api_resource = APIResource(app_context=self.app_context)
        self.mock_cursor = MagicMock()
        self.mock_cursor.fetchone.return_value = None
        self.mock_conn_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = (
            self.mock_cursor
        )

    def test_import_card_by_name_validates_input(self) -> None:
        """Test that import_card_by_name validates card_name parameter."""
        with pytest.raises(ValueError) as context:
            self.api_resource.admin.import_card_by_name(card_name="")

        assert str(context.value) == "card_name parameter is required"

        with pytest.raises(ValueError) as context:
            self.api_resource.admin.import_card_by_name(card_name=None)

        assert str(context.value) == "card_name parameter is required"

    @patch("requests.Session.get")
    def test_scryfall_search_returns_empty_for_404(self, mock_get: MagicMock) -> None:
        """Test that _scryfall_search returns empty list for 404 responses."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
        mock_get.return_value = mock_response

        result = self.api_resource.admin._scryfall_search(query="name:'NonexistentCard'")

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

        result = self.api_resource.admin._scryfall_search(query="name:'Lightning Bolt'")

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
            self.api_resource.admin._scryfall_search(query="name:'Lightning Bolt'")

    def test_import_card_by_name_returns_already_exists_for_existing_card(self) -> None:
        """Test that import_card_by_name returns already_exists status for existing cards."""
        # Mock the existence-check query to find a row
        self.mock_cursor.fetchone.return_value = {"card_name": "Lightning Bolt"}

        result = self.api_resource.admin.import_card_by_name(card_name="Lightning Bolt")

        assert result["status"] == "already_exists"
        assert result["card_name"] == "Lightning Bolt"
        assert "already exists in database" in result["message"]

    @patch.object(AdminResource, "_scryfall_search")
    def test_import_card_by_name_returns_not_found_for_missing_card(
        self,
        mock_search: MagicMock,
    ) -> None:
        """Test that import_card_by_name returns not_found status when card doesn't exist in Scryfall."""
        # Mock Scryfall API to return empty list (not found)
        mock_search.return_value = []

        result = self.api_resource.admin.import_card_by_name(card_name="NonexistentCard")

        assert result["status"] == "not_found"
        assert result["search_query"] == '!"NonexistentCard"'
        assert "No cards found for search query" in result["message"]

    @patch.object(AdminResource, "_scryfall_search")
    def test_import_card_by_name_returns_error_for_scryfall_exceptions(
        self,
        mock_search: MagicMock,
    ) -> None:
        """Test that import_card_by_name returns error status for Scryfall API exceptions."""
        # Mock Scryfall API to raise exception
        mock_search.side_effect = ValueError("API Error")

        result = self.api_resource.admin.import_card_by_name(card_name="TestCard")

        assert result["status"] == "error"
        assert result["search_query"] == '!"TestCard"'
        assert "Error fetching cards from Scryfall" in result["message"]

    @patch.object(AdminResource, "_scryfall_search")
    # `admin_resource` imports the name by value (`from api.card_processing import preprocess_card`),
    # so patching it in its defining module never reached the call site — the mock was inert and the
    # test passed only because the REAL function filtered a never-legal card to []. It no longer
    # does (nothing is filtered), which is what exposed the dead patch. Patch where it is USED.
    @patch("api.admin_resource.preprocess_card")
    def test_import_card_by_name_returns_filtered_out_for_invalid_cards(
        self,
        mock_preprocess: MagicMock,
        mock_search: MagicMock,
    ) -> None:
        """Test that import_card_by_name returns filtered_out status for cards filtered during preprocessing."""
        # Mock Scryfall API to return card data
        mock_search.return_value = [{"name": "TestCard", "legalities": {"standard": "not_legal"}}]

        # Mock preprocessing to return None (filtered out)
        mock_preprocess.return_value = []

        result = self.api_resource.admin.import_card_by_name(card_name="TestCard")
        assert result == {
            "status": "no_cards_after_preprocessing",
            "cards_loaded": 0,
            "cards_sent": 0,
            "message": "No cards remaining after preprocessing",
            "search_query": '!"TestCard"',
        }


if __name__ == "__main__":
    unittest.main()
