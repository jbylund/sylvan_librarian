"""Tests that both parser implementations produce identical SQL for the same queries."""

from functools import partial

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query
from api.parsing.tests.implicit_and_cases import TESTCASES

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)


def assert_parsers_agree(query: str) -> None:
    """Assert both parsers accept or reject *query* alike, and emit identical SQL when they accept."""
    hand_exc: Exception | None = None
    pyp_exc: Exception | None = None
    hand_result: tuple | None = None
    pyp_result: tuple | None = None

    try:
        hand_result = generate_sql_query(parse_scryfall_query(query))
    except Exception as exc:  # noqa: BLE001
        hand_exc = exc

    try:
        pyp_result = generate_sql_query(parse_with_pyparsing(query))
    except Exception as exc:  # noqa: BLE001
        pyp_exc = exc

    assert (hand_exc is None) == (pyp_exc is None), (
        f"Parsers disagree on validity of {query!r}: "
        f"hand_rolled={'ok' if hand_exc is None else hand_exc!r}, "
        f"pyparsing={'ok' if pyp_exc is None else pyp_exc!r}"
    )
    if hand_result is not None:
        assert hand_result == pyp_result, (
            f"Parsers produce different SQL for {query!r}:\n  hand_rolled: {hand_result}\n  pyparsing:   {pyp_result}"
        )


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[[c["query"]] for c in TESTCASES if c["query"].strip()],
    ids=[c["id"] for c in TESTCASES if c["query"].strip()],
)
def test_both_parsers_agree(query: str) -> None:
    """Both parsers must produce identical SQL for every query in TESTCASES."""
    assert_parsers_agree(query)


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        ('mana:"2WW"',),
        ('mana:"q"',),
        ('mana:"2WWQ"',),
    ],
    ids=["quoted_valid", "quoted_invalid_bare_char", "quoted_invalid_trailing_char"],
)
def test_quoted_mana_value_parity(query: str) -> None:
    """A quoted mana value is validated the same as bare/braced notation in both parsers.

    'mana:"q"' used to skip the alphabet check entirely in both parsers, resolving to an empty
    cost dict that matched every card instead of 400ing or matching nothing.
    """
    assert_parsers_agree(query)


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        ("mana:s",),
        ("mana:snow",),
        ("mana:p",),
        ("mana:hello",),
    ],
    ids=["bare_snow_valid", "bare_snow_word_invalid", "bare_phyrexian_unpaired", "bare_junk_word"],
)
def test_bare_mana_character_parity(query: str) -> None:
    """A bare mana character is validated the same as its braced form in both parsers (#954).

    pyparsing's tokenizer used to only recognize a fixed subset of bare letters as mana-shaped at
    all; anything outside it (including 's', valid braced as '{s}') fell through to a plain string
    comparison and silently matched the wrong cards instead of 400ing or agreeing with the hand
    parser's rejection.
    """
    assert_parsers_agree(query)


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        ("c!w",),
        ("c!ubg cmc>=6 f:standard",),
        ("en-kor c!w",),
        ("o:destroy o:creature o:with o:flying c!g",),
        ("cmc!3",),
        ("r!rare",),
        ("mana!2G",),
        ("year!2020",),
        ("date!2020-01-01",),
    ],
    ids=[
        "wild_color_bang",
        "wild_color_bang_with_cmc_and_legality",
        "wild_color_bang_after_hyphenated_word",
        "wild_color_bang_after_oracle_terms",
        "numeric_bang",
        "rarity_bang",
        "mana_bang",
        "year_bang",
        "date_bang",
    ],
)
def test_bang_equals_alias_parity(query: str) -> None:
    """'!' is Scryfall's '=' alias on COLOR/MANA/NUMERIC/RARITY/YEAR/DATE fields (#903 C)."""
    assert_parsers_agree(query)


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        ("c !w",),
        ("c! w",),
        ("c ! w",),
        ("cmc !3",),
        ("cmc! 3",),
        ("r !rare",),
        ("year! 2020",),
    ],
    ids=[
        "space_before_bang_color",
        "space_after_bang_color",
        "space_both_sides_color",
        "space_before_bang_numeric",
        "space_after_bang_numeric",
        "space_before_bang_rarity",
        "space_after_bang_year",
    ],
)
def test_spaced_bang_is_not_the_alias(query: str) -> None:
    """A '!' with a space on either side keeps the exact-name reading a space always had.

    Measured live: `c!w` answers 5,071 where `c !w` answers 0 (the name-and-exact-name reading)
    and `c! w` / `cmc! 3` are not the alias either. Both parsers must agree, and neither may fold
    the spaced spelling into `=` — a spaced spelling that errors (the numeric and year cases:
    `!3` cannot be an exact name) proves the same thing, since the glued spelling parses.
    """
    assert_parsers_agree(query)
    try:
        parsed = parse_scryfall_query(query)
    except ValueError:
        return  # rejected outright, which is certainly not the alias
    glued = parse_scryfall_query(query.replace(" ", ""))
    assert parsed.root.to_json() != glued.root.to_json(), "the spaced bang folded into the alias"


@pytest.mark.parametrize(
    argnames=["bang_query", "eq_query"],
    argvalues=[
        ("c!ubg", "c=ubg"),
        ("cmc!3", "cmc=3"),
        ("r!rare", "r=rare"),
        ("mana!2G", "mana=2G"),
        ("year!2020", "year=2020"),
        ("date!2020-01-01", "date=2020-01-01"),
    ],
)
def test_bang_equals_alias_matches_eq_spelling(bang_query: str, eq_query: str) -> None:
    """The '!' spelling must produce identical SQL to the '=' spelling it aliases (#903 C)."""
    assert generate_sql_query(parse_scryfall_query(bang_query)) == generate_sql_query(parse_scryfall_query(eq_query))


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        ("o:and power+toughness>10",),
        ("(o:or o:more) t:land",),
        ("(oracle:two oracle:or oracle:more oracle:opponents) type:land",),
    ],
    ids=[
        "reserved_word_value_with_arithmetic_comparison",
        "reserved_word_value_in_group",
        "reserved_word_values_repeated_in_group",
    ],
)
def test_reserved_word_as_value_parity(query: str) -> None:
    """'and'/'or' in value position is a value, not a boolean operator (#903 B).

    All 3 take the slow preprocess_implicit_and path (each has a '(' or '+'), where the
    implicit-AND tokenizer used to match CaselessKeyword("AND"/"OR") before knowing it was in
    value position, so e.g. 'o:or' silently lost its value and became 'o: OR'.
    """
    assert_parsers_agree(query)
