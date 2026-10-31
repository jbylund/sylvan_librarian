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


# ── mana:/…/ is a REGEX over the printed cost string ─────────────────────────
#
# Not, as the slash form's colour twin is, a value with the delimiters skipped. `mana:` is not on
# https://scryfall.com/docs/regular-expressions' keyword list; the page is incomplete, and these
# rows are what settles it — every one measured on api.scryfall.com 2026-08-28, and every one a
# result a pip-multiset reading cannot produce:
#
#   mana:/^{2}/   400 "Invalid regular expression: quantifier operand invalid." (it COMPILED it)
#   mana:/g|w/ mv=1  1,276    alternation, which is not a mana symbol
#   mana:/[wu]/ mv=1 1,193    a character class, likewise
#   mana:/}{/       26,815    every multi-symbol cost, a pure string artefact
#   mana:/rr/          404    because "{R}{R}" has no "rr" in it
#   mana:/2/         8,315    the CHARACTER, against mana:2's 19,692
#   mana:/^$/        1,350    the cards with no mana cost at all
#   mana:/^{r}$/       526    anchored, against mana:{r}'s 6,852
#   mana:/ /           435    = mana:/\/\// — a split cost is "{1}{R} // {1}{U}"
MANA_REGEX_ACCEPTED = [
    "mana:/p/",
    "mana:/}{/",
    "mana:/rr/",
    "mana:/^$/",
    "mana:/^{r}$/",
    r"mana:/\smh/",
    "mana:/g|w/",
    "mana:/[wu]/",
    "mana=/{r}/",
    "m:/rr/",
    "m=/2/",
]

# The scope is `mana`/`m` and `:`/`=`, because Scryfall's is: elsewhere the slashes go back to
# being value characters and its symbol lexer quotes them back — `mana!=/^tap/` is `Unknown mana
# symbols "/^TAP/"`, `mana>=/{r}/` is `Unknown mana symbols "//"`, and `devotion:/r/` is `Unknown
# regular expression keyword "devotion"` while `devotion:{r}` still answers 5,290.
MANA_REGEX_REFUSED = ["mana>=/{r}/", "mana!=/^tap/", "mana</{r}/", "mana<=/{r}/", "devotion:/r/", "devotion=/r/"]


@pytest.mark.parametrize(argnames=["query"], argvalues=[[q] for q in MANA_REGEX_ACCEPTED], ids=MANA_REGEX_ACCEPTED)
def test_mana_regex_parity(query: str) -> None:
    """Both parsers read `mana:/…/` as a pattern, and both emit the same `mana_cost_text ~*` SQL."""
    assert_parsers_agree(query)


@pytest.mark.parametrize(argnames=["query"], argvalues=[[q] for q in MANA_REGEX_REFUSED], ids=MANA_REGEX_REFUSED)
def test_mana_regex_scope_parity(query: str) -> None:
    """Outside the scope both parsers refuse, rather than one of them inventing a reading."""
    assert_parsers_agree(query)
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(query)


def test_mana_regex_reaches_the_cost_text_column() -> None:
    """The SQL fallback runs the pattern against `mana_cost_text`, the same string the engine reads.

    Without this the pattern falls into `mana_cost_str_to_dict`, which finds no symbols in it and
    emits the vacuously true `'{}'::jsonb <@ mana_cost_jsonb AND cmc >= 0` — every card, and the
    filter silently gone.
    """
    sql, params = generate_sql_query(parse_scryfall_query("mana:/^{r}$/"))
    assert "card.mana_cost_text ~* " in sql
    assert list(params.values()) == ["^{r}$"]
