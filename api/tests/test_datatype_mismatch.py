"""Tests for DatatypeMismatch error handling in the SQL search path.

This module tests that standalone arithmetic expressions in queries
(like "cmc+1") are handled gracefully by raising HTTPBadRequest
instead of leaking DatatypeMismatch exceptions. The handling lives in
_search_sql, so the tests call it directly (routing between the engine
and SQL paths is covered in test_parsing_errors.py).
"""

from __future__ import annotations

import multiprocessing
import time
from unittest.mock import patch

import falcon
import psycopg.errors
import pytest

from api.api_resource import APIResource
from api.tests.helpers import search_kwargs


class TestDatatypeMismatchHandling:
    """Test handling of DatatypeMismatch errors in the SQL search path."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )

    def teardown_method(self) -> None:
        """Clean up test fixtures."""
        if hasattr(self, "api_resource") and self.api_resource:
            # Close the connection pool to prevent thread pool warnings
            self.api_resource._conn_pool.close()

    def test_search_sql_handles_datatype_mismatch(self) -> None:
        """Test that DatatypeMismatch in _search_sql raises HTTPBadRequest."""
        # Mock the _run_query method to raise DatatypeMismatch
        with (
            patch.object(self.api_resource, "_run_query") as mock_run_query,
        ):
            mock_run_query.side_effect = psycopg.errors.DatatypeMismatch(
                'column "cmc" must appear in the GROUP BY clause or be used in an aggregate function',
            )

            # Call _search_sql with a problematic query and expect HTTPBadRequest
            with pytest.raises(falcon.HTTPBadRequest) as exc_info:
                self.api_resource._search_sql(**search_kwargs("cmc+1"))

            # Verify the error details
            assert exc_info.value.title == "Invalid Search Query"
            assert "cmc+1" in exc_info.value.description
            assert "invalid syntax" in exc_info.value.description.lower()

    def test_search_sql_handles_datatype_mismatch_main_query_only(self) -> None:
        """Test that DatatypeMismatch is only caught on the main query."""
        # Mock _run_query to fail on first call (main query)
        with (
            patch.object(self.api_resource, "_run_query") as mock_run_query,
        ):
            mock_run_query.side_effect = psycopg.errors.DatatypeMismatch(
                "WHERE clause must be type boolean, not type integer",
            )

            # Call _search_sql with a problematic query and expect HTTPBadRequest
            with pytest.raises(falcon.HTTPBadRequest) as exc_info:
                self.api_resource._search_sql(**search_kwargs("cmc+1", limit=100))

            # Verify the error details and that only one query was attempted
            assert exc_info.value.title == "Invalid Search Query"
            assert "cmc+1" in exc_info.value.description
            assert mock_run_query.call_count == 1

    def test_search_sql_normal_operation_unaffected(self) -> None:
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

            result = self.api_resource._search_sql(**search_kwargs("name:bolt"))

            # Verify normal operation
            assert len(result["cards"]) == 1
            assert result["cards"][0]["name"] == "Lightning Bolt"
            assert result["total_cards"] == 1
            assert result["query"] == "name:bolt"


if __name__ == "__main__":
    pytest.main([__file__])
