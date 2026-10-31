"""Scryfall's colour-COUNT values: `m`, `gold`, and the four `multicolor` spellings.

`c:m` is not "the colour m" -- there is no such colour. It is Scryfall's word for MULTICOLOURED,
and it compares the NUMBER of colours in the column, which is the comparison this branch already
builds for `c>=2`. Every value and every operator was measured against api.scryfall.com on
2026-08-16; the counts, and the two operator readings that are NOT "substitute the number 2", are
written out at colors.COLOR_COUNT_NAMES.
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.card_query_nodes import _color_count_masks
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

# The colour-COUNT names, as the numeric comparison each one means. `m` is not a colour and spells
# no letters: it is Scryfall's word for MULTICOLOURED and compares the NUMBER of colours in the
# column, so the operator does not survive verbatim either. Every pair below was measured
# corpus-wide against api.scryfall.com on 2026-08-16 -- see colors.COLOR_COUNT_NAMES for the
# counts, including the two readings that are NOT "substitute the number 2": `c>m` is `c>=2` rather
# than `c>2` (4,607 against 796), and `c!=m` is `c<2` rather than `c!=2` (29,049 against 29,836).
COLOR_COUNT_CASES = [
    # every spelling of the name, on the same operator
    ("c:m", "c>=2"),
    ("c:gold", "c>=2"),
    ("c:multicolor", "c>=2"),
    ("c:multicolour", "c>=2"),
    ("c:multicolored", "c>=2"),
    ("c:multicoloured", "c>=2"),
    # every operator, on the same spelling
    ("c=m", "c>=2"),
    ("c>m", "c>=2"),
    ("c>=m", "c>=2"),
    ("c<m", "c<2"),
    ("c!=m", "c<2"),
    ("c<=m", "c>=0"),  # a tautology: `c<=m t:creature` = `t:creature` = 18,753
    # the colour aliases and the identity column take the same table
    ("color:m", "c>=2"),
    ("colors:gold", "c>=2"),
    ("id:m", "id>=2"),
    ("identity:gold", "id>=2"),
    ("ci>m", "ci>=2"),
    ("id<m", "id<2"),
    ("id!=multicoloured", "id<2"),
    ("id<=m", "id>=0"),
    # case, quoting and negation all reach the same lowering
    ("c:M", "c>=2"),
    ("c:GOLD", "c>=2"),
    ('c:"m"', "c>=2"),
    ("-c:m", "-c>=2"),
]


@pytest.mark.parametrize(
    argnames=("query", "canonical_query"),
    argvalues=COLOR_COUNT_CASES,
    ids=[q for q, _ in COLOR_COUNT_CASES],
)
def test_color_count_name_matches_number(query: str, canonical_query: str) -> None:
    """A colour-COUNT name produces exactly the SQL its numeric comparison does, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(canonical_query))
    assert generate_sql_query(pyparsing_parse_str_to_query(query)) == generate_sql_query(pyparsing_parse_str_to_query(canonical_query))


# produced_mana is the same table on a SIX-value count, intersected with "produces at least one
# value": `produces<m` = 1,143 = `produces=1` and NOT `produces<2` = 32,139, which sweeps in the
# 30,996 cards that produce nothing at all.
PRODUCED_COUNT_CASES = [
    ("produces:m", "produces>=2"),
    ("produces=gold", "produces>=2"),
    ("produces>m", "produces>=2"),
    ("produces>=multicoloured", "produces>=2"),
    ("produces<m", "produces=1"),
    ("produces!=m", "produces=1"),
    ("produces<=m", "produces>=1"),
]


@pytest.mark.parametrize(
    argnames=("query", "canonical_query"),
    argvalues=PRODUCED_COUNT_CASES,
    ids=[q for q, _ in PRODUCED_COUNT_CASES],
)
def test_produced_mana_count_name_matches_number(query: str, canonical_query: str) -> None:
    """produced_mana takes the count names too, on its own operator table."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(canonical_query))
    assert generate_sql_query(pyparsing_parse_str_to_query(query)) == generate_sql_query(pyparsing_parse_str_to_query(canonical_query))


# THE FIVE/SIX SPLIT, PINNED IN BOTH DIRECTIONS so a later tidy-up cannot quietly unify them.
#
# produced_mana is the one colour-ish column whose array can literally contain "C" -- Sol Ring
# produces ["C"] while its colors and color_identity are both [] -- so a COUNT there counts
# colorless as a value and the colour columns do not. Measured against api.scryfall.com 2026-08-16:
# `produces=6` = 106 = `produces:all` (a count no five-key popcount can reach), the 481 cards
# producing colorless and nothing else answer `produces=1`, and counts 0..6 partition the corpus
# exactly. On the colour side `c:all` = `c:wubrg` = `c=5` = 60 and `c=6` is not a valid query at
# all ("Unknown color 6").
def test_produced_mana_counts_six_values_and_colors_count_five() -> None:
    """The count width differs by column, and each width is the measured one."""
    # Six bits on produced_mana: 64 masks in the enumeration, and a count of 6 is reachable.
    assert len(_color_count_masks("=", 0, bits=6)) + len(_color_count_masks(">=", 1, bits=6)) == 64
    assert _color_count_masks("=", 6, bits=6) == [0b11_1111]
    # Five on the colour columns: 32 masks, and a count of 6 can never be satisfied.
    assert len(_color_count_masks("=", 0)) + len(_color_count_masks(">=", 1)) == 32
    assert _color_count_masks("=", 6) == []
    assert _color_count_masks("=", 5) == [0b1_1111]
    # And the two columns reach DIFFERENT SQL, so the widths cannot be served by one index.
    produced_sql = generate_sql_query(parse_scryfall_query("produces>=2"))[0]
    colors_sql = generate_sql_query(parse_scryfall_query("c>=2"))[0]
    assert "magic.produced_mana_mask" in produced_sql
    assert "magic.color_identity_mask" not in produced_sql
    assert "magic.color_identity_mask" in colors_sql
    assert "magic.produced_mana_mask" not in colors_sql


# `any` is a count on produced_mana and a colour name nowhere -- "produces some mana at all".
# Measured corpus-wide against api.scryfall.com on 2026-08-28 AND against a `t:creature` second
# base, so that no equality here can be an artifact of the corpus total; every one held on both.
# The counts are written out at colors.COUNT_NAME_TO_COLUMN. `!=` is the one that does NOT read the
# way `m` does on this same column: `produces!=m` groups with `produces<m`, `produces!=any` groups
# with `produces:any`.
PRODUCED_ANY_CASES = [
    # `:` and the four operators that mean the same thing — "produces at least one kind of mana"
    ("produces:any", "produces>=1"),  # corpus 2,603; t:creature base 756
    ("produces=any", "produces>=1"),
    ("produces>any", "produces>=1"),
    ("produces>=any", "produces>=1"),
    ("produces!=any", "produces>=1"),
    # the two that do not: `<` is the cards that make no mana at all, `<=` admits one kind as well
    ("produces<any", "produces=0"),  # corpus 30,996; t:creature base 17,997
    ("produces<=any", "produces<=1"),  # corpus 32,139; t:creature base 18,369
    # case, quoting and negation all reach the same lowering
    ("produces:ANY", "produces>=1"),
    ('produces:"any"', "produces>=1"),
    ("-produces:any", "-produces>=1"),
]


@pytest.mark.parametrize(
    argnames=("query", "canonical_query"),
    argvalues=PRODUCED_ANY_CASES,
    ids=[q for q, _ in PRODUCED_ANY_CASES],
)
def test_produces_any_matches_number(query: str, canonical_query: str) -> None:
    """`produces:any` is a count, not a colour, and it filters instead of being dropped."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(canonical_query))
    assert generate_sql_query(pyparsing_parse_str_to_query(query)) == generate_sql_query(pyparsing_parse_str_to_query(canonical_query))


def test_produces_any_is_not_the_unfiltered_query() -> None:
    """The defect this fixes: the term was ignored, so the query answered its own base.

    `t:legendary t:creature produces:any` used to be a parse error here and answered the
    UNFILTERED 3,625 in the ports that drop an unparseable term, where Scryfall answers 194.
    """
    filtered = generate_sql_query(parse_scryfall_query("t:legendary t:creature produces:any"))
    unfiltered = generate_sql_query(parse_scryfall_query("t:legendary t:creature"))
    assert filtered != unfiltered
    assert filtered == generate_sql_query(parse_scryfall_query("t:legendary t:creature produces>=1"))


# `any` IS THE COLUMN'S OWN WORD, pinned in the other direction too. Scryfall does not accept it on
# the colour columns and does not match nothing there either -- it REJECTS the term and ignores it,
# which is a different answer: `c:any` on its own comes back "All of your terms were ignored", and
# `t:creature c:any` = `t:creature` = 18,753, the same for `id:any`. So it must not become a
# globally valid colour name; both parsers keep answering these the way they did before it existed.
@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=["c:any", "id:any", "color:any", "colors:any", "identity:any", "ci:any", "c>=any", "t:creature c:any"],
)
def test_any_is_rejected_on_the_color_columns(invalid_query: str) -> None:
    """`any` counts on produced_mana alone; on a colour column it stays a parse error, in both parsers."""
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(invalid_query)
    with pytest.raises(ValueError, match="Failed to parse query"):
        pyparsing_parse_str_to_query(invalid_query)


@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=["produces:anyw", "produces:wany", "produces:anyone", "c:anyone"],
)
def test_any_glued_to_other_letters_is_still_invalid(invalid_query: str) -> None:
    """`any` is a whole value, not a prefix: gluing letters to it is neither a name nor a letter set."""
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(invalid_query)
    with pytest.raises(ValueError, match="Failed to parse query"):
        pyparsing_parse_str_to_query(invalid_query)


@pytest.mark.parametrize(
    argnames=("query", "expected"),
    argvalues=[
        ("produces:any", "the number of kinds of mana produced ≥ 1"),
        ("produces<any", "the number of kinds of mana produced is 0"),
        ("produces<=any", "the number of kinds of mana produced ≤ 1"),
        ("produces>=2", "the number of kinds of mana produced ≥ 2"),
    ],
)
def test_produced_count_explanation_says_kinds_of_mana(query: str, expected: str) -> None:
    """A lowered count explains as a NUMBER OF KINDS, not as "produced mana ≥ 1" (a quantity)."""
    assert parse_scryfall_query(query).to_human_explanation() == expected


@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=["c:mw", "c:wm", "c:mc", "c:mm", "c!=mw", "id:mw", "c:mono", "produces:mw"],
)
def test_m_beside_another_color_is_still_invalid(invalid_query: str) -> None:
    """`m` beside another colour letter is neither a name nor a letter set, and stays a parse error.

    Scryfall dropped the combination outright -- it answers "Using “m” with other colors is no
    longer supported" and IGNORES the term -- so quietly reading `c:mw` as a count would answer a
    different question from the one asked.
    """
    with pytest.raises(ValueError, match="Failed to parse query"):
        parse_scryfall_query(invalid_query)
