"""Tests for parsing error handling in search queries.

This module tests that parsing errors in queries (like "cmc=2 and id=")
are handled gracefully by returning BadRequest errors instead of
throwing generic server errors.
"""

from __future__ import annotations

import multiprocessing
import time
from unittest.mock import patch

import falcon
import pytest

from api.api_resource import APIResource


class TestParsingErrorHandling:
    """Test handling of parsing errors in search functionality."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )

        def always_true() -> bool:
            return True

        self.api_resource._setup_complete = always_true

    def teardown_method(self) -> None:
        """Clean up test fixtures."""
        if hasattr(self, "api_resource") and self.api_resource:
            # Close the connection pool to prevent thread pool warnings
            self.api_resource._conn_pool.close()

    def test_search_handles_parsing_error_incomplete_query(self) -> None:
        """Test that parsing errors in search raise HTTPBadRequest."""
        # Test the specific case mentioned in the issue
        query = "cmc=2 and id="

        # Call _search with the problematic query and expect HTTPBadRequest
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query=query)

        # Verify the error details
        assert exc_info.value.title == "Invalid Search Query"
        assert query in exc_info.value.description
        assert f'Failed to parse query: "{query}"' == exc_info.value.description

    @pytest.mark.parametrize(
        "query",
        [
            "cmc=2 and id=",  # The original issue case
            "name:test and",  # Trailing AND
            "power>1 or",  # Trailing OR
            "cmc=3 and ()",  # Empty parentheses
        ],
    )
    def test_search_handles_parsing_error_various_cases(self, query: str) -> None:
        """Test that various parsing errors raise HTTPBadRequest."""
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query=query)

        # Verify the error details
        assert exc_info.value.title == "Invalid Search Query"
        assert query in exc_info.value.description
        assert f'Failed to parse query: "{query}"' == exc_info.value.description

    def test_search_normal_parsing_unaffected(self) -> None:
        """Test that normal queries still work correctly."""
        # Mock successful query execution
        with patch.object(self.api_resource, "_run_query") as mock_run_query:
            mock_run_query.return_value = {
                "result": [
                    {"name": "Lightning Bolt", "total_cards_count": None},
                    {"total_cards_count": 1},
                ],
                "timings": {},
            }

            result = self.api_resource._search(query="name:bolt")

            # Verify normal operation
            assert len(result["cards"]) == 1
            assert result["cards"][0]["name"] == "Lightning Bolt"
            assert result["total_cards"] == 1
            assert result["query"] == "name:bolt"


class TestSearchValidation:
    """Test _search input validation (limit, setup, bad query)."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._setup_complete = lambda: True

    def teardown_method(self) -> None:
        if hasattr(self, "api_resource") and self.api_resource:
            self.api_resource._conn_pool.close()

    def _mock_result(self) -> dict:
        return {
            "result": [{"name": "Opt", "total_cards_count": None}, {"total_cards_count": 1}],
            "timings": {},
        }

    def test_search_with_positive_limit_succeeds(self) -> None:
        """A positive limit value reaches the DB and returns results."""
        with patch.object(self.api_resource, "_run_query", return_value=self._mock_result()):
            result = self.api_resource._search(query="name:opt", limit=10)
        assert result["total_cards"] == 1

    def test_search_with_negative_limit_raises_bad_request(self) -> None:
        """A negative limit raises HTTPBadRequest."""
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query="name:opt", limit=-1)
        assert exc_info.value.title == "Invalid Limit"

    def test_search_raises_service_unavailable_when_setup_incomplete(self) -> None:
        """_search raises HTTPServiceUnavailable when setup is not complete."""
        self.api_resource._setup_complete = lambda: False
        with pytest.raises(falcon.HTTPServiceUnavailable) as exc_info:
            self.api_resource._search(query="name:opt")
        assert exc_info.value.title == "Service Unavailable"

    @pytest.mark.parametrize(
        argnames="query",
        argvalues=["t=", "cmc=2 and id=", "name:test and", "power>1 or"],
    )
    def test_search_raises_bad_request_for_unparseable_query(self, query: str) -> None:
        """Queries that fail to parse raise HTTPBadRequest."""
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query=query)
        assert exc_info.value.title == "Invalid Search Query"
        assert f'Failed to parse query: "{query}"' == exc_info.value.description


if __name__ == "__main__":
    pytest.main([__file__])
