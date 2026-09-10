"""A NUMBER token in text position is text, and keeps the spelling the user typed.

The lexer parses `001` to the int 1 and `1.50` to the float 1.5, which is right for `cmc:1.50`
but was also what reached text values: `set:001` searched for "1", `x-007` for "x-7", `o:1.50`
for "1.5". Tokens carry their source slice (`raw`) and the text paths read that instead.
"""

import pytest

from api.parsing import generate_sql_query
from api.parsing.nodes import ManaValueNode, NumericValueNode, StringValueNode


@pytest.mark.parametrize(
    argnames=("query", "value"),
    argvalues=[
        ("set:001", "001"),
        ("o:1.50", "1.50"),
        ("name:007", "007"),
        ("o:10.0", "10.0"),
        ("otag:cycle-007", "cycle-007"),
        ("name:x-007", "x-007"),
        ("kw:007", "007"),
    ],
    ids=["set", "oracle_decimal", "name", "oracle_trailing_zero", "otag_hyphen", "name_hyphen", "keyword"],
)
def test_number_in_text_value_keeps_its_spelling(parse_query, query: str, value: str) -> None:
    assert parse_query(query).root.rhs == StringValueNode(value)


@pytest.mark.parametrize(
    argnames=("query", "name"),
    argvalues=[("x-007", "x-007"), ("007", "007"), ("1.50", "1.50"), ("a-007-b", "a-007-b")],
    ids=["hyphenated", "bare", "bare_decimal", "hyphenated_middle"],
)
def test_number_in_a_bare_name_keeps_its_spelling(parse_query, query: str, name: str) -> None:
    root = parse_query(query).root
    assert root.lhs.attribute_name == "card_name"
    assert root.rhs == StringValueNode(name)


def test_leading_zero_reaches_the_sql_pattern(parse_query) -> None:
    _sql, params = generate_sql_query(parse_query("set:001"))
    assert "001" in params.values()


def test_mana_value_keeps_its_spelling(parse_query) -> None:
    """The mana path reads raw too, so both parsers build the same node for a zero-padded generic cost."""
    assert parse_query("mana:007").root.rhs == ManaValueNode("007")


@pytest.mark.parametrize(argnames=("query", "value"), argvalues=[("cmc:001", 1), ("cmc:1.50", 1.5), ("cn:001", 1)])
def test_numeric_position_still_parses_the_number(parse_query, query: str, value: float) -> None:
    """Only text position changes: a numeric value is still the number, not its spelling."""
    assert parse_query(query).root.rhs == NumericValueNode(value)
