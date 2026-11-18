"""`pt` / `powtou` -- Scryfall's combined power-and-toughness keyword.

Measured on api.scryfall.com 2026-10-03: both spellings take all seven operators (`pt=2` and
`powtou=2` 2,129, `pt:6` 2,724, `pt<6` 10,818, `pt<=6` 13,542, `pt>6` 5,357, `pt>=6` 8,081,
`pt!=6` 16,175) and stand on either side of a comparison with another column (`pt>pow` 18,477,
`pow>pt` 44, `mv>pt` 1,364). It is a numeric alias like any other, so the parser needs no rule of
its own: everything below is the code that already serves `pow` and `tou`.
"""

import pytest

from api.parsing import parse_scryfall_query
from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.nodes import BinaryOperatorNode, NumericValueNode, QueryContext


@pytest.mark.parametrize(
    argnames=("query", "alias", "operator", "value"),
    argvalues=[
        ("pt=2", "pt", "=", 2),
        ("pt:6", "pt", ":", 6),
        ("pt<6", "pt", "<", 6),
        ("pt<=6", "pt", "<=", 6),
        ("pt>6", "pt", ">", 6),
        ("pt>=6", "pt", ">=", 6),
        ("pt!=6", "pt", "!=", 6),
        ("powtou=2", "powtou", "=", 2),
        ("powtou:6", "powtou", ":", 6),
        ("powtou<6", "powtou", "<", 6),
        ("powtou<=6", "powtou", "<=", 6),
        ("powtou>6", "powtou", ">", 6),
        ("powtou>=6", "powtou", ">=", 6),
        ("powtou!=6", "powtou", "!=", 6),
        ("PT=2", "pt", "=", 2),
        ("pt=2.0", "pt", "=", 2.0),
        ("pt<-1", "pt", "<", -1),
    ],
)
def test_both_spellings_take_every_comparator(query: str, alias: str, operator: str, value: float) -> None:
    """Each spelling parses to a comparison on the one column."""
    root = parse_scryfall_query(query).root
    assert isinstance(root, BinaryOperatorNode), f"{query!r} did not parse to a comparison: {root}"
    assert isinstance(root.lhs, CardAttributeNode)
    assert root.lhs.attribute_name == "power_plus_toughness"
    assert root.lhs.original_attribute == alias
    assert root.operator == operator
    assert root.rhs == NumericValueNode(value)


@pytest.mark.parametrize(
    argnames=("query", "lhs", "rhs"),
    argvalues=[
        ("pt>pow", "power_plus_toughness", "creature_power"),
        ("powtou>tou", "power_plus_toughness", "creature_toughness"),
        ("pt=cmc", "power_plus_toughness", "cmc"),
        ("pow>pt", "creature_power", "power_plus_toughness"),
        ("tou<pt", "creature_toughness", "power_plus_toughness"),
        ("mv>pt", "cmc", "power_plus_toughness"),
    ],
)
def test_it_stands_on_either_side_of_a_column_comparison(query: str, lhs: str, rhs: str) -> None:
    """`pt>pow` and `mv>pt` are column-against-column, as `pow>tou` is."""
    root = parse_scryfall_query(query).root
    assert isinstance(root, BinaryOperatorNode)
    assert isinstance(root.lhs, CardAttributeNode)
    assert isinstance(root.rhs, CardAttributeNode)
    assert (root.lhs.attribute_name, root.rhs.attribute_name) == (lhs, rhs)


@pytest.mark.parametrize(
    argnames=("query", "explanation"),
    argvalues=[
        ("pt<6", "power plus toughness < 6"),
        ("powtou=2 t:creature", "power plus toughness is 2 and the type contains creature"),
        ("pt>pow", "power plus toughness > power"),
    ],
)
def test_it_explains_as_the_sum_it_is(query: str, explanation: str) -> None:
    """The explanation names what is compared, whichever spelling was typed."""
    assert parse_scryfall_query(query).to_human_explanation() == explanation


def test_the_sql_path_adds_the_two_columns() -> None:
    """There is no such column: the SQL is the sum of the two that exist."""
    context = QueryContext()
    sql = parse_scryfall_query("pt<6").to_sql(context)
    assert sql.startswith("((card.creature_power + card.creature_toughness) < ")
    assert list(context.values()) == [6]


def test_a_bare_word_is_still_a_name() -> None:
    """`pt` with no operator is a name search, as `pow` with none is."""
    root = parse_scryfall_query("pt").root
    assert isinstance(root, BinaryOperatorNode)
    assert root.lhs.attribute_name == "card_name"
