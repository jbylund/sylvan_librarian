"""Tests for the `game:` query keyword: a printing is in a game when its Scryfall `games` array holds it."""

from __future__ import annotations

import pytest

from api.parsing import generate_sql_query
from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import NotNode

GAMES_SQL = "(card.raw_card_blob->'games')"


@pytest.mark.parametrize(
    ("query", "expected_game"),
    [
        ("game:paper", "paper"),
        ("game:mtgo", "mtgo"),
        ("game:arena", "arena"),
        ("game:Arena", "arena"),
        ('game:"paper"', "paper"),
    ],
)
def test_game_is_a_containment_test_on_the_printings_games(parse_query, query: str, expected_game: str) -> None:
    """`game:X` matches printings whose games array contains X (Scryfall stores the names lowercase)."""
    parsed = parse_query(query)
    assert isinstance(parsed.root, CardBinaryOperatorNode)
    assert isinstance(parsed.root.lhs, CardAttributeNode)
    sql, params = generate_sql_query(parsed)
    (placeholder,) = params
    assert sql == f"({GAMES_SQL} @> %({placeholder})s)"
    assert params[placeholder] == [expected_game]


def test_negated_game(parse_query) -> None:
    """`-game:paper` keeps the printings that are not in paper."""
    parsed = parse_query("-game:paper")
    assert isinstance(parsed.root, NotNode)
    sql, params = generate_sql_query(parsed)
    (placeholder,) = params
    assert sql == f"NOT (({GAMES_SQL} @> %({placeholder})s))"


def test_game_combines_with_other_filters(parse_query) -> None:
    """`t:elf game:arena prefer:usd-low` keeps the type filter and the game filter, and lifts prefer."""
    parsed = parse_query("t:elf game:arena prefer:usd-low")
    assert parsed.prefer == "usd-low"
    sql, _ = generate_sql_query(parsed)
    assert GAMES_SQL in sql
    assert "card_subtypes" in sql


def test_game_explanation(parse_query) -> None:
    """The explanation says where the printing is available."""
    assert parse_query("game:arena").to_human_explanation() == "it's available in arena"
