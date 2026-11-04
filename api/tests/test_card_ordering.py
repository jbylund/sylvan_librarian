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


def _compiled_sql(
    stub_api_resource: APIResource,
    ordering: CardOrdering,
    direction: SortDirection = SortDirection.ASC,
) -> str:
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
            direction=direction,
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


@pytest.mark.parametrize(
    argnames=("ordering", "direction", "expected"),
    argvalues=[
        # A missing MAGNITUDE sorts below every value, so the direction moves it to the head
        # ascending and the tail descending.
        (CardOrdering.POWER, SortDirection.ASC, "sort_value ASC NULLS FIRST"),
        (CardOrdering.POWER, SortDirection.DESC, "sort_value DESC NULLS LAST"),
        (CardOrdering.TOUGHNESS, SortDirection.ASC, "sort_value ASC NULLS FIRST"),
        (CardOrdering.USD, SortDirection.ASC, "sort_value ASC NULLS FIRST"),
        (CardOrdering.USD, SortDirection.DESC, "sort_value DESC NULLS LAST"),
        (CardOrdering.CMC, SortDirection.ASC, "sort_value ASC NULLS FIRST"),
        # A missing RANK sorts AFTER every rank, which is the opposite end in both directions —
        # and `edhrec` is the default `order=`, so this is what an unordered search gets.
        (CardOrdering.EDHREC, SortDirection.ASC, "sort_value ASC NULLS LAST"),
        (CardOrdering.EDHREC, SortDirection.DESC, "sort_value DESC NULLS FIRST"),
    ],
)
def test_the_missing_value_side_is_per_column(
    stub_api_resource: APIResource,
    ordering: CardOrdering,
    direction: SortDirection,
    expected: str,
) -> None:
    """Scryfall puts a missing value on a side that depends on the COLUMN, not just the direction.

    Measured on api.scryfall.com over `e:khm unique=prints`, one page-1 request per (column,
    direction), 2026-08-17: `power`, `toughness` and `usd` all LEAD ascending with their nulls,
    while `edhrec` holds none on an ascending page of 175 and LEADS descending with 33 of them.

    Asserted on the compiled SQL rather than through the database because the clause is the whole
    of the behaviour: Postgres' own default is NULLS LAST for ASC and NULLS FIRST for DESC, which
    is the EDHREC row — so a regression that simply dropped the clause would leave one column
    right and every other one wrong, and only a per-column assertion can tell those apart.
    """
    assert expected in _compiled_sql(stub_api_resource, ordering, direction)
