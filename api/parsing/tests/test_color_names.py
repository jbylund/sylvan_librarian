"""Scryfall's colour-NAME vocabulary: the guilds, shards, wedges and four-colour names.

`c:azorius` is a normal thing for a player to type, and this parser answered it with a parse
error — only `white`/`blue`/`black`/`red`/`green`/`colorless` and bare letter strings were
accepted. Scryfall accepts 44 names, measured one request each against api.scryfall.com
(`c:<value> e:khm`, 2026-08-16) and each then checked against its letter spelling over the whole
corpus, because Kaldheim holds exactly one card of three colours or more and would have agreed
with almost any mapping: `c:bant` = `c:gwu` = 153, `c:yore-tiller` = `c:wubr` = 62,
`c:rainbow` = `c:wubrg` = 60, `c:brown` = `c:c` = 4,300.

The list is a boundary rather than a superset: `yore`, `glint`, `dune`, `ink` and `witch` on
their own are rejected by Scryfall, so the un-hyphenated four-colour nicknames are not in its
table, and neither are `five`, `mono`, `guild`, `shard` or `wedge`.

The five Strixhaven colleges (Lorehold, Prismari, Quandrix, Silverquill, Witherbloom) were added
later, verified the same way: `c:witherbloom` = `c:bg` = 606 corpus-wide, and so on for all five.
`college`, `colleges`, and `strixhaven` are rejected by Scryfall, same boundary-not-superset rule.
"""

from functools import partial

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.colors import COLOR_ALIAS_TO_CODES
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)

# (name query, the letter query it must mean). Every pair verified live before landing.
COLOR_NAME_CASES = [
    ("c:azorius", "c:wu"),
    ("c:dimir", "c:ub"),
    ("c:rakdos", "c:br"),
    ("c:gruul", "c:rg"),
    ("c:selesnya", "c:gw"),
    ("c:orzhov", "c:wb"),
    ("c:izzet", "c:ur"),
    ("c:golgari", "c:bg"),
    ("c:boros", "c:rw"),
    ("c:simic", "c:gu"),
    # the five Strixhaven colleges
    ("c:lorehold", "c:rw"),
    ("c:prismari", "c:ur"),
    ("c:quandrix", "c:gu"),
    ("c:silverquill", "c:wb"),
    ("c:witherbloom", "c:bg"),
    ("c:bant", "c:gwu"),
    ("c:esper", "c:wub"),
    ("c:grixis", "c:ubr"),
    ("c:jund", "c:brg"),
    ("c:naya", "c:rgw"),
    ("c:abzan", "c:wbg"),
    ("c:jeskai", "c:urw"),
    ("c:sultai", "c:bgu"),
    ("c:mardu", "c:rwb"),
    ("c:temur", "c:gur"),
    ("c:yore-tiller", "c:wubr"),
    ("c:glint-eye", "c:ubrg"),
    ("c:dune-brood", "c:brgw"),
    ("c:ink-treader", "c:rgwu"),
    ("c:witch-maw", "c:gwub"),
    ("c:artifice", "c:wubr"),
    ("c:chaos", "c:ubrg"),
    ("c:aggression", "c:brgw"),
    ("c:altruism", "c:rgwu"),
    ("c:growth", "c:gwub"),
    ("c:rainbow", "c:wubrg"),
    ("c:colourless", "c:c"),
    ("c:brown", "c:c"),
    # `all` spells wubrgc, which is wubrg once the c drops out of a card_colors comparison …
    ("c:all", "c:wubrg"),
    # … and stays six values on produced_mana, where colorless is a genuine producible thing.
    ("produces:all", "produces:wubrgc"),
    ("produces:rainbow", "produces:wubrg"),
    # The identity spellings take the same vocabulary (Scryfall: id:bant e:khm == id:gwu e:khm).
    ("id:bant", "id:gwu"),
    ("identity:esper", "identity:wub"),
    ("ci<=abzan", "ci<=wbg"),
    ("c>=azorius", "c>=wu"),
    ("c!=jund", "c!=brg"),
    ("-c:temur", "-c:gur"),
]


@pytest.mark.parametrize(
    argnames=("query", "canonical_query"),
    argvalues=COLOR_NAME_CASES,
    ids=[q for q, _ in COLOR_NAME_CASES],
)
def test_color_name_matches_letters(query: str, canonical_query: str) -> None:
    """A colour name produces exactly the SQL its letter spelling does, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(canonical_query))
    assert generate_sql_query(parse_with_pyparsing(query)) == generate_sql_query(parse_with_pyparsing(canonical_query))


@pytest.mark.parametrize(
    argnames="query",
    argvalues=["c:AZORIUS", "c:Azorius", "c:BaNt", "id:YORE-TILLER"],
)
def test_color_names_are_case_insensitive(query: str) -> None:
    """Scryfall answers `c:AZORIUS e:khm` with the same 6 cards as `c:azorius e:khm`."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(query.lower()))


# Values Scryfall REJECTS, which this parser must keep rejecting: adding a name Scryfall does not
# have would answer a WIDER result than Scryfall, silently.
@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=[
        "c:yore",
        "c:glint",
        "c:dune",
        "c:ink",
        "c:witch",
        "c:five",
        "c:guild",
        "c:shard",
        "c:wedge",
        "c:nephilim",
        "c:azorius-senate",
        "c:boros-legion",
        "c:college",
        "c:colleges",
        "c:strixhaven",
    ],
)
def test_rejected_color_names_still_fail(invalid_query: str) -> None:
    """A name outside Scryfall's table is still a parse error rather than a silent widening."""
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(invalid_query)


def test_every_alias_spells_only_color_codes() -> None:
    """Every entry in the table expands to letters `get_colors_comparison_object` can read."""
    assert all(set(codes) <= set("wubrgc") for codes in COLOR_ALIAS_TO_CODES.values())


# ── slash-delimited colour values ────────────────────────────────────────────
#
# `/.../` is NOT a regex on a colour column. Scryfall reads it as ordinary value text with the
# delimiters skipped, and every pair below is an exact equality measured on api.scryfall.com
# 2026-08-28 — pinned as a TREE identity rather than as a count, which is the stronger claim:
# not merely "answers something", but "is that query".
#
# The discriminating evidence is the failure mode, which quotes the term AS TYPED and names the
# letter from WITHOUT the slashes: `c:/xyz/` is `400 All of your terms were ignored.` with
# `Invalid expression "c:/xyz/" was ignored. Unknown color "x"`, the same sentence `c:xyz` gets.
# A regex reading has nothing to say about `/white/` or `/yore-tiller/` at all.
SLASHED_COLOR_CASES = [
    ("c:/w/", "c:w", "7,105"),
    ("c:/wu/", "c:wu", "718"),
    ("c:/white/", "c:white", "7,105"),
    ("c:/yore-tiller/", "c:yore-tiller", "62"),
    ("color:/w/", "color:w", "7,105"),
    ("colour:/w/", "colour:w", "7,105"),
    ("colors:/w/", "colors:w", "7,105"),
    ("colours:/w/", "colours:w", "7,105"),
    ("id:/w/", "id:w", "7,993"),
    ("identity:/w/", "identity:w", "7,993"),
    ("ci:/w/", "ci:w", "7,993"),
    ("commander:/w/", "commander:w", "7,993"),
    ("produces:/g/", "produces:g", "1,274"),
    ("c>=/w/", "c>=w", "—"),
]


@pytest.mark.parametrize(
    argnames=["slashed", "plain", "scryfall_count"],
    argvalues=SLASHED_COLOR_CASES,
    ids=[s for s, _, _ in SLASHED_COLOR_CASES],
)
def test_slashed_color_value_is_the_undelimited_query(parse_query, slashed: str, plain: str, scryfall_count: str) -> None:
    """The delimited spelling parses to exactly the same AST as the plain one (both parsers)."""
    assert scryfall_count  # documentation, carried so the measured number lives beside the case
    assert parse_query(slashed) == parse_query(plain)


@pytest.mark.parametrize(
    argnames=["slashed", "plain", "scryfall_count"],
    argvalues=SLASHED_COLOR_CASES,
    ids=[s for s, _, _ in SLASHED_COLOR_CASES],
)
def test_slashed_color_value_generates_the_same_sql(slashed: str, plain: str, scryfall_count: str) -> None:
    """...and the SQL path answers it as a value too, so the fallback cannot diverge."""
    assert scryfall_count
    assert generate_sql_query(parse_scryfall_query(slashed)) == generate_sql_query(parse_scryfall_query(plain))
    assert generate_sql_query(parse_with_pyparsing(slashed)) == generate_sql_query(parse_scryfall_query(plain))


@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=["c:/xyz/", "id:/xyz/", "produces:/xyz/", "c:/azorius-senate/"],
)
def test_slashed_color_value_fails_like_the_undelimited_one(invalid_query: str) -> None:
    """The delimiters buy no leniency: an unknown colour is still a parse error, not a regex."""
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(invalid_query)
    with pytest.raises(ValueError, match=r"Parse error|Failed to parse query"):
        parse_with_pyparsing(invalid_query)
