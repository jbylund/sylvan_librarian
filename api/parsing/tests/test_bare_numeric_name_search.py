"""A bare numeric expression with no comparison is a name search, not a non-boolean root.

`1996`, `cmc+1` and `power - cmc` used to parse to a NumericValueNode / arithmetic node at the root,
which the SQL path rendered as `WHERE %(p)s` and Postgres rejected with a type error (a 400 for a
query Scryfall answers as a name search). Inside a comparison the same text stays arithmetic.
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.card_query_nodes import CardBinaryOperatorNode as BinaryOperatorNode
from api.parsing.db_info import ParserClass
from api.parsing.nodes import AndNode, NotNode, NumericValueNode, StringValueNode


def _name(text: str) -> BinaryOperatorNode:
    return BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode(text))


@pytest.mark.parametrize(
    argnames=["query", "literal"],
    argvalues=[
        ("1996", "1996"),
        ("2.5", "2.5"),
        ("cmc+1", "cmc+1"),
        ("cmc-3", "cmc-3"),
        ("cmc-power", "cmc-power"),
        ("power - cmc", "power-cmc"),  # spacing between tokens is not part of the name
        ("(2*power)", "2*power"),  # the parentheses were grouping, not name
        ("(2*power)-1", "(2*power)-1"),
        ("power", "power"),
    ],
    ids=["year", "float", "attr_plus_literal", "attr_minus_literal", "attr_minus_attr", "spaced", "group", "group_tail", "attr"],
)
def test_bare_numeric_expression_is_a_name_search(parse_query, query: str, literal: str) -> None:
    """The whole expression, spelled as the user typed it, becomes one name search (both parsers)."""
    assert parse_query(query).root == _name(literal)


def test_bare_number_renders_as_a_name_like() -> None:
    """The SQL is a boolean predicate on the name column, never a bare bound parameter."""
    sql, params = generate_sql_query(parse_scryfall_query("1996"))
    assert sql.startswith("(lower(card.card_name_folded) LIKE %(")
    assert list(params.values()) == ["%1996%"]


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[("cmc+1<power",), ("(2*power)-1>3",), ("(cmc+1)*2>3",), ("power - cmc > 1",), ("1<power",)],
    ids=["arith_lhs", "group_then_tail", "group_operand", "spaced", "literal_lhs"],
)
def test_numeric_expression_inside_a_comparison_stays_arithmetic(parse_query, query: str) -> None:
    """Only an expression with nothing to compare against is a name; a comparison keeps its arithmetic."""
    root = parse_query(query).root
    assert isinstance(root, BinaryOperatorNode)
    assert root.operator in {"<", ">"}
    assert getattr(root.lhs, "attribute_name", None) != "card_name"


def test_negated_bare_number_negates_a_name_search(parse_query) -> None:
    """`-1` is NOT (name contains "1"), not NOT (1)."""
    assert parse_query("-1").root == NotNode(_name("1"))


def test_spaced_minus_before_a_number_is_negation_not_subtraction(parse_query) -> None:
    """`cmc>2 -1` is two factors -- the value ends at `2` -- and the second negates a name search."""
    root = parse_query("cmc>2 -1").root
    assert isinstance(root, AndNode)
    comparison, negation = root.operands
    assert comparison.operator == ">"
    assert comparison.rhs == NumericValueNode(2)
    assert negation == NotNode(_name("1"))
