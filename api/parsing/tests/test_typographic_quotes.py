"""The four typographic quotes Scryfall folds before lexing, and the only four.

A word processor or a phone keyboard turns a typed apostrophe into U+2019 and typed double quotes
into U+201C/U+201D, so pasted queries carry them constantly. This parser read them as ordinary
letters, which made a query for Gaea<U+2019>s Blessing a search for a name containing a curly
apostrophe: no rows, no error, no clue.

Measured against api.scryfall.com (2026-08-16) by putting each candidate around a phrase and
asking whether the phrase searched as ONE term. U+2018/U+2019 fold to `'` and U+201C/U+201D to
`"`; every other quotation-shaped character stays literal -- guillemets, low-9 quotes, primes,
fullwidth forms, CJK brackets, ornate quotes, backtick, acute, U+02BC.
"""

from functools import partial

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.hand_parser import fold_typographic_quotes
from api.parsing.parsing_f import balance_partial_query
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)

_LEFT_SINGLE = "\u2018"
_RIGHT_SINGLE = "\u2019"
_LEFT_DOUBLE = "\u201c"
_RIGHT_DOUBLE = "\u201d"

# (query with typographic quotes, the ASCII query it must mean)
FOLDED_CASES = [
    (f"name:{_LEFT_DOUBLE}Gaea{_RIGHT_SINGLE}s Blessing{_RIGHT_DOUBLE}", 'name:"Gaea\'s Blessing"'),
    (f"name:{_LEFT_SINGLE}Lightning Bolt{_RIGHT_SINGLE}", "name:'Lightning Bolt'"),
    (f"o:{_LEFT_DOUBLE}draw a card{_RIGHT_DOUBLE}", 'o:"draw a card"'),
    # The fold is a character substitution over the WHOLE query, not a rule about quoted regions:
    # a curly apostrophe INSIDE double quotes folds too, which is what makes Gaea's Blessing
    # findable at all.
    (f'name:"Gaea{_RIGHT_SINGLE}s Blessing"', 'name:"Gaea\'s Blessing"'),
    (f"t:creature o:{_LEFT_DOUBLE}flying{_RIGHT_DOUBLE} c:azorius", 't:creature o:"flying" c:azorius'),
]


@pytest.mark.parametrize(
    argnames=("query", "canonical_query"),
    argvalues=FOLDED_CASES,
    ids=[str(i) for i in range(len(FOLDED_CASES))],
)
def test_typographic_quotes_fold(query: str, canonical_query: str) -> None:
    """A curly-quoted query parses to exactly what its ASCII-quoted twin parses to, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(canonical_query))
    assert generate_sql_query(parse_with_pyparsing(query)) == generate_sql_query(parse_with_pyparsing(canonical_query))


# Quotation-shaped characters Scryfall does NOT fold. Asserted on the fold itself rather than on a
# parse, because several of them are not lexable at all here -- the claim being pinned is that the
# substitution table has exactly four entries, and a wider table is the way this goes wrong.
@pytest.mark.parametrize(
    argnames="candidate",
    # Spelled as escapes rather than literals: ruff's RUF001 refuses ambiguous characters in
    # source, and these are the ambiguous characters, on purpose.
    argvalues=[
        "\u00ab",  # left guillemet
        "\u00bb",  # right guillemet
        "\u2039",  # single left guillemet
        "\u203a",  # single right guillemet
        "\u201e",  # double low-9
        "\u201a",  # single low-9
        "\u2032",  # prime
        "\u2033",  # double prime
        "\u2035",  # reversed prime
        "\uff02",  # fullwidth quotation mark
        "\uff07",  # fullwidth apostrophe
        "\u300c",  # CJK corner bracket, opening
        "\u300d",  # CJK corner bracket, closing
        "\u300e",  # CJK white corner bracket, opening
        "\u300f",  # CJK white corner bracket, closing
        "\u275b",  # heavy single turned comma quotation mark ornament
        "\u275c",  # heavy single comma quotation mark ornament
        "\u275d",  # heavy double turned comma quotation mark ornament
        "\u275e",  # heavy double comma quotation mark ornament
        "`",  # grave accent
        "\u00b4",  # acute accent
        "\u02bc",  # modifier letter apostrophe
    ],
)
def test_other_quotation_marks_do_not_fold(candidate: str) -> None:
    """Everything except the four measured characters is left literal."""
    assert fold_typographic_quotes(f"name:{candidate}Bolt{candidate}") == f"name:{candidate}Bolt{candidate}"


@pytest.mark.parametrize(
    argnames=("candidate", "folded"),
    argvalues=[(_LEFT_SINGLE, "'"), (_RIGHT_SINGLE, "'"), (_LEFT_DOUBLE, '"'), (_RIGHT_DOUBLE, '"')],
)
def test_the_four_that_fold(candidate: str, folded: str) -> None:
    """The whole table, one row at a time."""
    assert fold_typographic_quotes(f"a{candidate}b") == f"a{folded}b"


def test_balance_folds_before_counting_quotes() -> None:
    """The balancer sees the folded text, or a typed opening curly quote balances to nothing."""
    assert balance_partial_query(f"name:{_LEFT_SINGLE}Lightning") == "name:'Lightning'"
    assert balance_partial_query(f"o:{_LEFT_DOUBLE}draw") == 'o:"draw"'
