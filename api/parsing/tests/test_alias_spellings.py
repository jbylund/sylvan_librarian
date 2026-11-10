"""Scryfall's alternate spellings for color identity and the two tag families.

Scryfall accepts three spellings per tag family — `art`/`atag`/`arttag` and
`otag`/`oracletag`/`function` — each returning identical results (verified live:
196/196/196 and 6427/6427/6427), and `ci` as a color-identity alias
(`ci<=bg` == `id<=bg`). Only one spelling per family was recognized here, so a
client forwarding Scryfall-shaped query strings verbatim hit a parse error on
the rest. Each added spelling must resolve to the same field as its canonical
form, in both parsers.

The same holds for three keyword spellings that name a column this parser already
had (measured on api.scryfall.com 2026-10-03): `edition` is a fourth spelling of
`set`, `collector` / `collectornumber` are the numeric collector number, and
`edhrec` / `edhrecrank` / `edhrec_rank` search the EDHREC rank — a column that was
sorted on and had no alias at all.
"""

from functools import partial

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.db_info import ALIAS_TO_FIELD_INFOS, ParserClass
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)

# (alias spelling, the already-supported spelling it must match)
TAG_ALIAS_CASES = [
    ("atag:squirrel", "art:squirrel"),
    ("arttag:squirrel", "art:squirrel"),
    ("oracletag:removal", "otag:removal"),
    ("function:removal", "otag:removal"),
]


@pytest.mark.parametrize(
    argnames=["alias_query", "canonical_query"],
    argvalues=TAG_ALIAS_CASES,
    ids=[q for q, _ in TAG_ALIAS_CASES],
)
def test_tag_aliases_match_canonical(alias_query: str, canonical_query: str) -> None:
    """Each Scryfall tag-alias spelling produces identical SQL to its canonical form, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(alias_query)) == generate_sql_query(parse_scryfall_query(canonical_query))
    assert generate_sql_query(parse_with_pyparsing(alias_query)) == generate_sql_query(parse_with_pyparsing(canonical_query))


CI_CASES = [
    ("ci<=bg", "id<=bg"),
    ("ci:wu", "id:wu"),
    ("ci>=rg", "identity>=rg"),
    ("t:land ci<=bg", "t:land id<=bg"),
]


@pytest.mark.parametrize(
    argnames=["ci_query", "id_query"],
    argvalues=CI_CASES,
    ids=[q for q, _ in CI_CASES],
)
def test_ci_is_an_identity_alias(ci_query: str, id_query: str) -> None:
    """`ci` produces identical SQL to the established identity aliases, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(ci_query)) == generate_sql_query(parse_scryfall_query(id_query))
    assert generate_sql_query(parse_with_pyparsing(ci_query)) == generate_sql_query(parse_with_pyparsing(id_query))


# (the new spelling, the already-supported spelling it must match)
KEYWORD_SPELLING_CASES = [
    # `edition` is `set`: `edition:khm t:god` = `e:khm t:god` (12 on Scryfall).
    ("edition:khm t:god", "e:khm t:god"),
    ("edition=khm", "set=khm"),
    ("EDITION:KHM", "s:khm"),
    ("-edition:khm t:god", "-e:khm t:god"),
    # `collector` / `collectornumber` are the numeric collector number: `collector>=390 e:khm`
    # = `cn>=390 e:khm` (17), under every comparator.
    ("collector:1", "cn:1"),
    ("collector=1", "number=1"),
    ("collectornumber:1", "cn:1"),
    ("collector>=390", "cn>=390"),
    ("collectornumber>=390", "cn>=390"),
    ("collector<5", "cn<5"),
    ("collector<=5", "cn<=5"),
    ("collector>5", "cn>5"),
    ("collector!=1", "cn!=1"),
    # ...and a column on the right of a comparison (`pow>cn e:khm` = `pow>number e:khm`, 1).
    ("pow>collector", "pow>cn"),
    ("pow>collectornumber", "pow>number"),
]


@pytest.mark.parametrize(
    argnames=["spelling_query", "canonical_query"],
    argvalues=KEYWORD_SPELLING_CASES,
    ids=[q for q, _ in KEYWORD_SPELLING_CASES],
)
def test_keyword_spellings_match_canonical(spelling_query: str, canonical_query: str) -> None:
    """Each added keyword spelling produces identical SQL to the spelling already supported, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(spelling_query)) == generate_sql_query(parse_scryfall_query(canonical_query))
    assert generate_sql_query(parse_with_pyparsing(spelling_query)) == generate_sql_query(parse_with_pyparsing(canonical_query))


# The EDHREC rank had no spelling to compare against, so its SQL is pinned directly. So is
# `collector` against another column: with no string half it is always the integer column
# (`collector>=cmc e:khm` is 303 on Scryfall), where the dual `cn>=cmc` reads `cmc` as a string here.
PINNED_SQL_CASES = [
    ("collector>=cmc", "(card.collector_number_int >= card.cmc)", {}),
    ("collectornumber>pow", "(card.collector_number_int > card.creature_power)", {}),
    ("edhrec:1", "(card.edhrec_rank = %(p_int_MQ)s)", {"p_int_MQ": 1}),
    ("edhrecrank:1", "(card.edhrec_rank = %(p_int_MQ)s)", {"p_int_MQ": 1}),
    ("edhrec_rank:1", "(card.edhrec_rank = %(p_int_MQ)s)", {"p_int_MQ": 1}),
    ("edhrec=1", "(card.edhrec_rank = %(p_int_MQ)s)", {"p_int_MQ": 1}),
    ("edhrec<=10", "(card.edhrec_rank <= %(p_int_MTA)s)", {"p_int_MTA": 10}),
    ("edhrec<10", "(card.edhrec_rank < %(p_int_MTA)s)", {"p_int_MTA": 10}),
    ("edhrecrank>=5000", "(card.edhrec_rank >= %(p_int_NTAwMA)s)", {"p_int_NTAwMA": 5000}),
    ("edhrec!=1", "(card.edhrec_rank != %(p_int_MQ)s)", {"p_int_MQ": 1}),
    ("-edhrec:1", "NOT ((card.edhrec_rank = %(p_int_MQ)s))", {"p_int_MQ": 1}),
    # A column on either side: `edhrec>=cmc e:khm` = `cmc<edhrec e:khm` = `cmc<edhrecrank e:khm` (295).
    ("edhrec>=cmc", "(card.edhrec_rank >= card.cmc)", {}),
    ("cmc<edhrec", "(card.cmc < card.edhrec_rank)", {}),
    ("cmc<edhrecrank", "(card.cmc < card.edhrec_rank)", {}),
]


@pytest.mark.parametrize(
    argnames=["query", "expected_sql", "expected_params"],
    argvalues=PINNED_SQL_CASES,
    ids=[q for q, _, _ in PINNED_SQL_CASES],
)
def test_keyword_spellings_compare_the_numeric_column(query: str, expected_sql: str, expected_params: dict) -> None:
    """Every spelling of `edhrec` and `collector` compares its numeric column, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(query)) == (expected_sql, expected_params)
    assert generate_sql_query(parse_with_pyparsing(query)) == (expected_sql, expected_params)


@pytest.mark.parametrize("spelling", ["collector", "collectornumber"])
def test_collector_has_no_string_half(spelling: str) -> None:
    """`collector` names the numeric collector number only.

    `cn` and `number` are dual: `cn:1` is the integer and `cn:100b` the string. On Scryfall
    `collector:abc` is an unknown keyword where `cn:abc` is honored and matches nothing, so the
    new spellings must not be registered on the TEXT `collector_number` column.
    """
    assert {fi.parser_class for fi in ALIAS_TO_FIELD_INFOS[spelling]} == {ParserClass.NUMERIC}
    assert {fi.parser_class for fi in ALIAS_TO_FIELD_INFOS["cn"]} == {ParserClass.NUMERIC, ParserClass.TEXT}
    for parse in (parse_scryfall_query, parse_with_pyparsing):
        with pytest.raises(ValueError, match="Failed to parse query"):
            parse(f"{spelling}:abc")
