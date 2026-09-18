"""Fractional values in `cmc:` / `mv:` / `manavalue:` comparisons.

A mana value is a Decimal in Scryfall's schema, not an Integer: {HW} gives Little Girl a
cmc of exactly 0.5. Neither parser needed changing to accept `mv=0.5` — the tokenizer has
always produced a float for a literal with a decimal point (api/parsing/hand_parser.py's
NUMBER token), and `p_float_*` binding carries it through to SQL untouched. That was true
before the column could hold a fraction too, and nothing tested it, which is why it is
worth pinning: the storage change is what makes these queries mean something, and a
future "cmc is an integer, coerce it" shortcut anywhere on this path would now be a
silent regression rather than a no-op.
"""

import pytest

from api.parsing import generate_sql_query
from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.nodes import BinaryOperatorNode, NumericValueNode

testcases = {
    "cmc_equals_half": {"query": "cmc=0.5", "operator": "=", "value": 0.5},
    "mv_equals_half": {"query": "mv=0.5", "operator": "=", "value": 0.5},
    "manavalue_equals_half": {"query": "manavalue=0.5", "operator": "=", "value": 0.5},
    # ':' on a numeric attribute is Scryfall's spelling of '='; it stays ':' in the AST.
    "colon_operator": {"query": "mv:0.5", "operator": ":", "value": 0.5},
    "greater_or_equal": {"query": "mv>=0.5", "operator": ">=", "value": 0.5},
    "less_than": {"query": "mv<0.5", "operator": "<", "value": 0.5},
    "not_equals": {"query": "mv!=0.5", "operator": "!=", "value": 0.5},
    # A half step above a whole number, so the fraction is not just a leading "0.".
    "one_and_a_half": {"query": "mv=1.5", "operator": "=", "value": 1.5},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases.values()))),
    argvalues=[[v for k, v in sorted(testcases[testname].items())] for testname in sorted(testcases)],
    ids=sorted(testcases),
)
def test_fractional_mana_value_parses(parse_query, operator: str, query: str, value: float) -> None:
    """A fractional literal reaches the AST as a float, under every alias, on both parsers."""
    root = parse_query(query).root
    assert isinstance(root, BinaryOperatorNode), f"{query!r} did not parse to a comparison: {root}"
    assert isinstance(root.lhs, CardAttributeNode)
    assert root.lhs.attribute_name == "cmc"
    assert root.operator == operator
    assert root.rhs == NumericValueNode(value)


def test_fractional_mana_value_binds_as_a_float_parameter(parse_query) -> None:
    """The value stays a float all the way into the bound parameter, not rounded to an int."""
    sql, params = generate_sql_query(parse_query("mv=0.5"))
    assert "card.cmc = " in sql
    assert list(params.values()) == [0.5]
    assert all(isinstance(v, float) for v in params.values())


def test_a_whole_mana_value_still_binds_as_an_int(parse_query) -> None:
    """Unchanged for the values every card in the corpus actually has."""
    _, params = generate_sql_query(parse_query("mv=1"))
    assert list(params.values()) == [1]
    assert all(isinstance(v, int) for v in params.values())
