"""Hyphenated bare words whose first half is a numeric alias (`pow-wow`, `power-plant`, `mv-x`).

The NUMERIC branch of the hand parser's word dispatch used to return the bare attribute when the
'-' was not followed by a numeric term, leaving the '-' unconsumed and failing the whole query,
where the same word with a non-alias first half (`well-known`) parsed as a name.
"""

import pytest

from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.card_query_nodes import CardBinaryOperatorNode as BinaryOperatorNode
from api.parsing.db_info import ParserClass
from api.parsing.nodes import StringValueNode


def _name(text: str) -> BinaryOperatorNode:
    return BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode(text))


@pytest.mark.parametrize(
    argnames="word",
    argvalues=["pow-wow", "power-plant", "mv-x", "pow-wow-wow", "cmc-a-b", "Power-Plant", "tou-ch"],
)
def test_hyphenated_word_with_numeric_alias_prefix_is_a_name(parse_query, word: str) -> None:
    """The whole hyphenated word is one name search, spelled as typed (both parsers)."""
    assert parse_query(word).root == _name(word)


def test_hyphenated_word_with_numeric_alias_prefix_composes(parse_query) -> None:
    """It is an ordinary factor: implicit AND with what follows, negatable."""
    root = parse_query("power-plant t:land -pow-wow").root
    assert len(root.operands) == 3
    assert root.operands[0] == _name("power-plant")
    assert root.operands[2].operand == _name("pow-wow")


@pytest.mark.parametrize(
    argnames=("query", "operator"),
    argvalues=[("pow-tou>0", "-"), ("pow>tou", None), ("pow-1>2", "-"), ("power - toughness > 1", "-")],
)
def test_arithmetic_with_a_numeric_term_after_the_hyphen_is_still_arithmetic(parse_query, query: str, operator: str | None) -> None:
    """A numeric term after the '-' keeps the subtraction reading; the comparison is unchanged."""
    root = parse_query(query).root
    assert isinstance(root, BinaryOperatorNode)
    assert root.operator == ">"
    if operator is None:
        assert isinstance(root.lhs, CardAttributeNode)
    else:
        assert root.lhs.operator == operator


def test_numeric_alias_minus_number_without_comparison_is_a_name(parse_query) -> None:
    """`pow-2` has a numeric term after the '-', so it is arithmetic -- and bare arithmetic is a name search."""
    assert parse_query("pow-2").root == _name("pow-2")
