"""A number inside a text value keeps the spelling the query gave it.

The lexer turns every all-digit run into a NUMBER token (`0796` is the int 796, `1.50` the float
1.5), and the hand parser built text values out of those tokens with `str(tok.value)`. So a text
value lost its leading zeros and trailing decimal zeros, while the pyparsing parser kept them:

    hand:      a:007 -> "7"      o:1.50 -> "1.5"      abc-007 -> "Abc-7"
    pyparsing: a:007 -> "007"    o:1.50 -> "1.50"     abc-007 -> "Abc-007"

The case that bites is an oracle id. A hyphen splits a UUID into WORD and NUMBER tokens, and an
all-digit piece that starts with a zero lost it: Doubling Cube's `9afd8f12-0796-4500-aaa3-10b4a46ef6ec`
came back as `9afd8f12-796-...`, which is no card's id. 720 of 38,626 oracle ids (1.9%, the
2026-08-16 bulk) have such a piece, so a search by oracle id for any of those cards matched nothing.

Numeric contexts are unchanged: `cmc=007` is still the number 7.
"""

import pytest

from api.parsing.hand_parser import TT, tokenize
from api.parsing.nodes import NumericValueNode, StringValueNode

DOUBLING_CUBE_ORACLE_ID = "9afd8f12-0796-4500-aaa3-10b4a46ef6ec"


@pytest.mark.parametrize(
    argnames=["query", "expected"],
    argvalues=[
        # Doubling Cube's oracle id: the second piece is an all-digit NUMBER token with a leading zero.
        (f"o:{DOUBLING_CUBE_ORACLE_ID}", DOUBLING_CUBE_ORACLE_ID),
        # A leading all-digit piece, and a trailing one.
        ("o:00037840-6089-42ec-8c5c-281f9f474504", "00037840-6089-42ec-8c5c-281f9f474504"),
        ("o:5c58353a-fd60-4528-bf0d-000000000001", "5c58353a-fd60-4528-bf0d-000000000001"),
        # A whole value that is one NUMBER token.
        ("a:007", "007"),
        ("s:007", "007"),
        ("o:1.50", "1.50"),
        # Several numeric pieces, as a date typed into a text field is.
        ("o:2024-02-09", "2024-02-09"),
        ("o:1.50-2.00", "1.50-2.00"),
    ],
    ids=[
        "doubling_cube_oracle_id",
        "leading_zero_first_piece",
        "leading_zero_last_piece",
        "artist",
        "set",
        "trailing_decimal_zero",
        "date_shaped",
        "hyphenated_decimals",
    ],
)
def test_text_value_keeps_number_spelling(parse_query, query: str, expected: str) -> None:
    """A text value is the query's own spelling, zeros included, from both parsers."""
    assert parse_query(query).root.rhs == StringValueNode(expected)


def test_bare_hyphenated_name_keeps_number_spelling(parse_query) -> None:
    """A bare hyphenated name glues a numeric piece as spelled, not as its value.

    Only the tail is asserted: what a bare name does with the hyphen itself is a separate question.
    """
    value = parse_query("abc-007").root.rhs.value
    assert value.endswith("007"), value


@pytest.mark.parametrize(
    argnames=["query", "expected"],
    argvalues=[
        ("cmc=007", 7),
        ("power>=1.50", 1.5),
        ("cn:007", 7),
    ],
    ids=["int", "float", "dual_class_alias"],
)
def test_numeric_value_is_still_a_number(parse_query, query: str, expected: float) -> None:
    """A NUMBER read as a number is its value: the spelling only matters where it is text."""
    assert parse_query(query).root.rhs == NumericValueNode(expected)


def test_date_value_is_unchanged(parse_query) -> None:
    """A date is parsed from the NUMBER values and re-formatted, so a zero-led month still reads right."""
    assert parse_query("date>=2024-02-09").root.rhs == StringValueNode("2024-02-09")


def test_number_token_carries_its_spelling() -> None:
    """The lexer keeps a NUMBER's source text beside its value; other tokens read as their value."""
    number, minus, word, _eof = tokenize("0796-aaa3")
    assert (number.type, number.value, number.text) == (TT.NUMBER, 796, "0796")
    assert (minus.type, minus.text) == (TT.MINUS, "-")
    assert (word.type, word.text) == (TT.WORD, "aaa3")
    decimal = tokenize("1.50")[0]
    assert (decimal.type, decimal.value, decimal.text) == (TT.NUMBER, 1.5, "1.50")
