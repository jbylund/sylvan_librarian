"""Tests for parsing error handling in search queries.

This module tests that parsing errors in queries (like "t=") are handled
gracefully, and that partially-valid queries (like "cmc=2 and id=") now
succeed via partial parsing with the invalid parts reported as ignored.
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
            self.api_resource._conn_pool.close()

    def test_search_partial_query_succeeds_with_ignored_parts(self) -> None:
        """Partially-valid queries succeed and report the invalid fragment."""
        query = "cmc=2 and id="

        with patch.object(self.api_resource, "_run_query") as mock_run_query:
            mock_run_query.return_value = {
                "result": [{"name": "Opt", "total_cards_count": None}, {"total_cards_count": 1}],
                "timings": {},
            }
            result = self.api_resource._search(query=query)

        assert result["total_cards"] == 1
        assert "id=" in [part["fragment"] for part in result.get("query_ignored", [])]

    @pytest.mark.parametrize(
        argnames="query",
        argvalues=[
            "cmc=2 and id=",  # valid part: cmc=2; ignored: id=
            "name:test and",  # valid part: name:test; trailing AND ignored
            "power>1 or",  # valid part: power>1; trailing OR ignored
            "cmc=3 and ()",  # valid part: cmc=3; ignored: ()
        ],
    )
    def test_search_partial_queries_succeed(self, query: str) -> None:
        """Queries with at least one valid part succeed via partial parsing."""
        with patch.object(self.api_resource, "_run_query") as mock_run_query:
            mock_run_query.return_value = {
                "result": [{"total_cards_count": 0}],
                "timings": {},
            }
            result = self.api_resource._search(query=query)

        assert "cards" in result

    @pytest.mark.parametrize(
        argnames="query",
        argvalues=[
            "t=",  # no valid part at all
            "id=",  # no valid part at all
            "x>3",  # unrecognized field with no valid part
        ],
    )
    def test_search_fully_invalid_queries_raise_bad_request(self, query: str) -> None:
        """Queries with no valid part at all still raise HTTPBadRequest."""
        with patch.object(
            self.api_resource,
            "_run_query",
            side_effect=AssertionError(f"query reached DB execution — should have failed at parse time: {query!r}"),
        ):
            with pytest.raises(falcon.HTTPBadRequest) as exc_info:
                self.api_resource._search(query=query)

        assert exc_info.value.title == "Invalid Search Query"
        assert f'Failed to parse query: "{query}"' == exc_info.value.description

    def test_search_normal_parsing_unaffected(self) -> None:
        """Test that normal queries still work correctly."""
        with patch.object(self.api_resource, "_run_query") as mock_run_query:
            mock_run_query.return_value = {
                "result": [
                    {"name": "Lightning Bolt", "total_cards_count": None},
                    {"total_cards_count": 1},
                ],
                "timings": {},
            }

            result = self.api_resource._search(query="name:bolt")

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
        argvalues=[
            "t=",  # no valid part at all
            "id=",  # no valid part at all
            "x>3",  # unrecognized field with no valid part
        ],
    )
    def test_search_raises_bad_request_for_unparseable_query(self, query: str) -> None:
        """Queries with no valid part raise HTTPBadRequest."""
        with patch.object(
            self.api_resource,
            "_run_query",
            side_effect=AssertionError(f"query reached DB execution — should have failed at parse time: {query!r}"),
        ):
            with pytest.raises(falcon.HTTPBadRequest) as exc_info:
                self.api_resource._search(query=query)
        assert exc_info.value.title == "Invalid Search Query"
        assert f'Failed to parse query: "{query}"' == exc_info.value.description


if __name__ == "__main__":
    pytest.main([__file__])
