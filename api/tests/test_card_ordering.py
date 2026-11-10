"""Tests for CardOrdering SQL wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import parse_scryfall_query
from api.utils.timer import Timer

if TYPE_CHECKING:
    from api.api_resource import APIResource


def _compiled_sql(stub_api_resource: APIResource, ordering: CardOrdering) -> str:
    """Return the compiled SQL string via the SQL path directly."""
    parsed_query = parse_scryfall_query("cmc=1")
    with (
        patch.object(stub_api_resource.app_context, "reader_pool") as mock_pool,
        patch.object(stub_api_resource.app_context, "setup_complete", return_value=True),
    ):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [{"total_cards_count": 0, "name": None}]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_pool.connection.return_value.__enter__.return_value = mock_conn

        result = stub_api_resource._search_sql(
            parsed_query=parsed_query,
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
def test_orderby_column_in_compiled_sql(stub_api_resource: APIResource, ordering: CardOrdering, expected_column: str) -> None:
    """Every CardOrdering value should produce its mapped column in the compiled SQL."""
    needle = f", {expected_column} AS sort_value FROM magic.cards"
    assert needle in _compiled_sql(stub_api_resource, ordering)


# The columns the SQL path sorts each ordering by. Written out rather than imported so that the
# mapping is asserted against something, not against itself.
EXPECTED_SORT_COLUMNS = {
    CardOrdering.ARTIST: "lower(card_artist)",
    CardOrdering.CMC: "cmc",
    CardOrdering.CUBECOBRA: "cubecobra_score",
    CardOrdering.EDHREC: "edhrec_rank",
    CardOrdering.EUR: "price_eur",
    CardOrdering.NAME: "lower(card_name)",
    CardOrdering.POWER: "creature_power",
    CardOrdering.RARITY: "card_rarity_int",
    CardOrdering.RELEASED: "released_at",
    CardOrdering.SET: "lower(card_set_code)",
    CardOrdering.TIX: "price_tix",
    CardOrdering.TOUGHNESS: "creature_toughness",
    CardOrdering.USD: "price_usd",
}


def test_every_ordering_has_an_expected_column() -> None:
    """COLOR is the one ordering whose sort key is an expression rather than a column."""
    assert set(EXPECTED_SORT_COLUMNS) | {CardOrdering.COLOR} == set(CardOrdering)


@pytest.mark.parametrize("ordering", sorted(EXPECTED_SORT_COLUMNS), ids=str)
def test_ordering_sorts_by_its_own_column(stub_api_resource: APIResource, ordering: CardOrdering) -> None:
    """`sql_orderby` falls back to edhrec_rank on a missing entry, so an unmapped ordering is silent.

    Iterating the enum rather than a list of the orderings that happen to be wired means a member
    added without its `sql_orderby` entry fails here.
    """
    needle = f", {EXPECTED_SORT_COLUMNS[ordering]} AS sort_value FROM magic.cards"
    assert needle in _compiled_sql(stub_api_resource, ordering)


def test_color_sorts_by_the_bucket_expression(stub_api_resource: APIResource) -> None:
    """Scryfall's colour order is eleven buckets, not the colour bitmask — see the CASE in api_resource."""
    compiled = _compiled_sql(stub_api_resource, CardOrdering.COLOR)
    assert "AS sort_value FROM magic.cards" in compiled
    # Colourless after every coloured bucket, and lands after that: the two parts a bitmask gets wrong.
    assert "WHEN card_types ? 'Land' THEN 10" in compiled
    assert "ELSE 9" in compiled
