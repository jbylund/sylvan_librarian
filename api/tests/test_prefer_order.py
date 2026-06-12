"""Test cases for prefer order functionality."""

import multiprocessing
import time
import unittest
from unittest.mock import MagicMock, patch

from api.api_resource import APIResource
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import parse_scryfall_query
from api.utils.timer import Timer


class TestPreferOrder(unittest.TestCase):
    """Test cases for prefer order parameter."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )

        def always_true() -> bool:
            return True

        self.api_resource._import_recent = always_true

    def _search_sql(self, query: str, prefer: PreferOrder) -> dict:
        """Run _search_sql directly, bypassing engine dispatch."""
        parsed_query = parse_scryfall_query(query)
        with (
            patch.object(self.api_resource, "_conn_pool") as mock_pool,
            patch.object(self.api_resource, "_setup_complete", return_value=True),
        ):
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [{"total_cards_count": 0, "name": None}]
            mock_conn = MagicMock()
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_pool.connection.return_value.__enter__.return_value = mock_conn

            return self.api_resource._search_sql(
                parsed_query=parsed_query,
                query=query,
                unique=UniqueOn.CARD,
                prefer=prefer,
                orderby=CardOrdering.EDHREC,
                direction=SortDirection.ASC,
                limit=100,
                timer=Timer(),
            )

    def test_prefer_order_enum_values(self) -> None:
        """Test that PreferOrder enum has all expected values."""
        assert PreferOrder.DEFAULT == "default"
        assert PreferOrder.OLDEST == "oldest"
        assert PreferOrder.NEWEST == "newest"
        assert PreferOrder.USD_LOW == "usd_low"
        assert PreferOrder.USD_HIGH == "usd_high"
        assert PreferOrder.PROMO == "promo"

    def test_search_accepts_prefer_parameter(self) -> None:
        """Test that search method accepts prefer parameter without error."""
        result = self._search_sql("cmc=3", PreferOrder.OLDEST)
        assert result is not None
        assert "cards" in result

    def test_search_prefer_parameter_in_sql_query(self) -> None:
        """Test that prefer parameter affects SQL query generation."""
        assert "released_at" in self._search_sql("cmc=3", PreferOrder.OLDEST)["compiled"]
        assert "released_at" in self._search_sql("cmc=3", PreferOrder.NEWEST)["compiled"]
        assert "price_usd" in self._search_sql("cmc=3", PreferOrder.USD_LOW)["compiled"]
        assert "price_usd" in self._search_sql("cmc=3", PreferOrder.USD_HIGH)["compiled"]
        assert "prefer_score" in self._search_sql("cmc=3", PreferOrder.DEFAULT)["compiled"]
