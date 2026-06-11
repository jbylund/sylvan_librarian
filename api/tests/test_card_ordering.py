"""Tests for CardOrdering SQL wiring."""

import multiprocessing
import time
from unittest.mock import MagicMock, patch

import pytest

from api.api_resource import APIResource
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import generate_sql_query, parse_scryfall_query
from api.utils.timer import Timer


@pytest.fixture(name="api_resource")
def api_resource_fixture() -> APIResource:
    api = APIResource(last_import_time=multiprocessing.Value("d", time.time(), lock=True))
    api._import_recent = lambda: True
    return api


def _compiled_sql(api_resource: APIResource, ordering: CardOrdering) -> str:
    """Return the compiled SQL string via the SQL path directly."""
    parsed_query = parse_scryfall_query("cmc=1")
    where_clause, params = generate_sql_query(parsed_query)
    with (
        patch.object(api_resource, "_conn_pool") as mock_pool,
        patch.object(api_resource, "_setup_complete", return_value=True),
    ):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [{"total_cards_count": 0, "name": None}]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_pool.connection.return_value.__enter__.return_value = mock_conn

        result = api_resource._search_sql(
            where_clause=where_clause,
            params=params,
            query_explanation="",
            query="cmc=1",
            unique=UniqueOn.CARD,
            prefer=PreferOrder.DEFAULT,
            orderby=ordering,
            direction=SortDirection.ASC,
            limit=100,
            timer=Timer(),
        )
        return result["compiled"]


@pytest.mark.parametrize(
    argnames=("ordering", "expected_column"),
    argvalues=[
        (CardOrdering.CMC, "cmc"),
        (CardOrdering.CUBECOBRA, "cubecobra_score"),
        (CardOrdering.EDHREC, "edhrec_rank"),
        (CardOrdering.POWER, "creature_power"),
        (CardOrdering.RARITY, "card_rarity_int"),
        (CardOrdering.TOUGHNESS, "creature_toughness"),
        (CardOrdering.USD, "price_usd"),
    ],
)
def test_orderby_column_in_compiled_sql(api_resource: APIResource, ordering: CardOrdering, expected_column: str) -> None:
    """Every CardOrdering value should produce its mapped column in the compiled SQL."""
    needle = f", {expected_column} AS sort_value FROM magic.cards"
    assert needle in _compiled_sql(api_resource, ordering)
