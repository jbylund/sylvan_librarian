"""Commas and apostrophes in bare name searches (Scryfall-measured semantics).

Card names carry punctuation ("Rograkh, Son of Rohgahh", "Urza's Bauble"), and users type
it: `rograkh, son of rograkh` works on Scryfall and used to be a parse error here. The
measured rules (api.scryfall.com, 2026-08-08):

- A comma ATTACHED to a bare word is shed from the name filter ("son," filters exactly as
  "son" — identical totals).
- A comma standing alone is skipped like whitespace ("rograkh , son" == "rograkh son").
- A comma in a FIELD value stays verbatim ("t:goblin," matches nothing on Scryfall too).
- A quoted name keeps its commas.
- An apostrophe is part of a word when a word character follows ("urza's"); a leading
  apostrophe still opens a quoted string (name:'power').
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.pyparsing_based import parse_search_query

# (query with punctuation, equivalent query without it)
EQUIVALENT_CASES = [
    ("rograkh, son of rograkh", "rograkh son of rograkh"),
    ("rograkh , son", "rograkh son"),
    ("lightning bolt,", "lightning bolt"),
    ("pow=2, tou=3", "pow=2 tou=3"),
    ("t:goblin, t:elf or t:human,", "t:goblin, t:elf or t:human,"),  # self-parity smoke
]


@pytest.mark.parametrize(
    argnames=["query", "equivalent"],
    argvalues=EQUIVALENT_CASES,
    ids=[q for q, _ in EQUIVALENT_CASES],
)
def test_comma_query_filters_like_equivalent(query: str, equivalent: str) -> None:
    """A comma-bearing query produces the same SQL as its comma-free equivalent (hand parser)."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(equivalent))


ALL_CASES = [
    *[q for q, _ in EQUIVALENT_CASES],
    "urza's bauble",
    "o:urza's",
    '"son,"',
    "t:goblin,",
    "name:'power'",
]


@pytest.mark.parametrize(argnames="query", argvalues=ALL_CASES)
def test_special_char_parser_parity(query: str) -> None:
    """Both parsers agree on every punctuation-bearing query."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_search_query(query))


def test_apostrophe_stays_in_the_name_value() -> None:
    """`urza's bauble` searches for the substring with its apostrophe intact."""
    _, params = generate_sql_query(parse_scryfall_query("urza's bauble"))
    assert any("urza's" in str(value) for value in params.values())


def test_quoted_name_keeps_its_comma() -> None:
    """Explicit quotes mean verbatim: the filter keeps the comma a bare word would shed."""
    _, params = generate_sql_query(parse_scryfall_query('"son,"'))
    assert any("son," in str(value) for value in params.values())


def test_field_value_keeps_its_comma() -> None:
    """`t:goblin,` binds the value comma-included — Scryfall also matches nothing there."""
    _, params = generate_sql_query(parse_scryfall_query("t:goblin,"))
    assert ["Goblin,"] in params.values()


def test_leading_apostrophe_still_opens_a_quote() -> None:
    """name:'power' keeps its single-quoted-string reading."""
    _, params = generate_sql_query(parse_scryfall_query("name:'power'"))
    assert any("power" in str(value) for value in params.values())


def test_dangling_apostrophe_is_still_an_error() -> None:
    """An apostrophe not followed by a word character still opens an (unclosed) quote."""
    with pytest.raises(ValueError, match="Failed to lex"):
        parse_scryfall_query("urza' bauble")
    with pytest.raises(ValueError, match="Unmatched quote"):
        parse_search_query("urza' bauble")
