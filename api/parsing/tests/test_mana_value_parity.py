"""`mv:even` / `mv:odd` -- the two words Scryfall takes where a mana value goes.

Measured on api.scryfall.com 2026-10-03 (corpus `mv>=0` = 33,649): `mv:even` 17,331, `mv:odd`
16,317, every spelling of the column and both of `:`/`=` alike, case and quotes immaterial. The
two are one short of the corpus because Little Girl's mana value is 0.5 and is neither -- which
is what lowering to `(mv % 2) = 0|1` answers without a special case.
"""

import re

import pytest

from api.parsing import parse_scryfall_query
from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.nodes import BinaryOperatorNode, NotNode, NumericValueNode, QueryContext


@pytest.mark.parametrize(
    argnames=("query", "alias", "remainder"),
    argvalues=[
        ("mv:even", "mv", 0),
        ("mv:odd", "mv", 1),
        ("mv=even", "mv", 0),
        ("mv=odd", "mv", 1),
        ("cmc:even", "cmc", 0),
        ("cmc=odd", "cmc", 1),
        ("manavalue:odd", "manavalue", 1),
        ("manavalue=even", "manavalue", 0),
        ("mv:EVEN", "mv", 0),
        ("MV:Odd", "mv", 1),
        ('mv:"even"', "mv", 0),
        ("mv:'odd'", "mv", 1),
    ],
)
def test_parity_lowers_to_a_remainder(query: str, alias: str, remainder: int) -> None:
    """Each spelling parses to `(mana value % 2) = remainder`, on the cmc column."""
    root = parse_scryfall_query(query).root
    assert isinstance(root, BinaryOperatorNode), f"{query!r} did not parse to a comparison: {root}"
    assert root.operator == "="
    assert root.rhs == NumericValueNode(remainder)
    assert isinstance(root.lhs, BinaryOperatorNode)
    assert root.lhs.operator == "%"
    assert root.lhs.rhs == NumericValueNode(2)
    assert isinstance(root.lhs.lhs, CardAttributeNode)
    assert root.lhs.lhs.attribute_name == "cmc"
    assert root.lhs.lhs.original_attribute == alias


@pytest.mark.parametrize(
    argnames=("query", "explanation"),
    argvalues=[
        ("mv:even", "the mana value is even"),
        ("cmc=odd", "the mana value is odd"),
        ("-mv:even", "not (the mana value is even)"),
        ("mv:odd t:creature", "the mana value is odd and the type contains creature"),
    ],
)
def test_parity_explains_as_it_was_typed(query: str, explanation: str) -> None:
    """The remainder is how the word is answered, not what the reader asked for."""
    assert parse_scryfall_query(query).to_human_explanation() == explanation


def test_parity_composes_like_any_other_term() -> None:
    """Negation is the complement and a group is a group -- nothing here is a special node."""
    negated = parse_scryfall_query("-mv:even").root
    assert isinstance(negated, NotNode)
    assert isinstance(negated.operand, BinaryOperatorNode)
    assert negated.operand.rhs == NumericValueNode(0)
    assert parse_scryfall_query("mv:even or mv:odd").to_human_explanation() == "(the mana value is even or the mana value is odd)"


def test_parity_generates_a_remainder_in_sql() -> None:
    """The SQL path takes the remainder with `mod` over `numeric`.

    PostgreSQL has no `%` for `real`, which is what the cmc column is, and a bare `%` would open
    a placeholder in a statement that runs with named parameters.
    """
    context = QueryContext()
    sql = parse_scryfall_query("mv:odd").to_sql(context)
    assert sql.startswith("(mod((card.cmc)::numeric, %(")
    assert "%" not in re.sub(r"%\(\w+\)s", "", sql), sql


@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=[
        # Only `:` and `=` take the words. Under a comparison Scryfall keeps the term and matches
        # nothing (`mv>even` is a 404 there, with no warning), which is not a parity at all.
        "mv>even",
        "mv>=odd",
        "mv<even",
        "mv<=odd",
        "mv!=even",
        # Only the mana-value column: `pow:even` is "Unknown keyword" on Scryfall.
        "pow:even",
        "tou=odd",
        "loy:even",
        # Only the two words.
        "mv:evens",
        "mv:neither",
    ],
)
def test_parity_is_the_two_words_on_one_column_under_equality(invalid_query: str) -> None:
    """Everything else stays the parse error it was."""
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(invalid_query)
