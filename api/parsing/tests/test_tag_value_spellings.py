"""Scryfall's alternate spellings for a tag *value*, as opposed to the field name.

Oracle and art tag slugs are hyphenated (`right-facing`, `removal-creature`), and Scryfall accepts
the spaced spelling of one interchangeably — verified live with `unique=art`:
`art:right-facing`, `art:"right facing"` and `art:"looks right"` all return 2633, and
`otag:removal-creature` / `otag:"creature removal"` / `otag:creature-removal` all return 7806.
Only the exact hyphenated spelling matched here, so a searcher writing the tag the way it reads —
or forwarding a Scryfall query verbatim — got zero results.

Slugifying the search term is safe because every slug in both tag dumps matches
`[a-z0-9]+(-[a-z0-9]+)*` (checked against the 11,517 art and 4,522 oracle tags), so the
normalization can only turn a miss into a hit. It is also what makes the space-spelled aliases
tag_import stores (`open mouth` -> `open-mouth`) reachable.
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.card_query_nodes import slugify_tag
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

# (spelling as written, the hyphenated spelling it must match)
TAG_VALUE_CASES = [
    ('art:"right facing"', "art:right-facing"),
    ('art:"open mouth"', "art:open-mouth"),
    ("art:Flames", "art:flames"),
    ('otag:"creature removal"', "otag:creature-removal"),
    ("otag:Removal-Creature", "otag:removal-creature"),
    ('t:creature art:"three figures"', "t:creature art:three-figures"),
]


@pytest.mark.parametrize(
    argnames=["written_query", "slug_query"],
    argvalues=TAG_VALUE_CASES,
    ids=[q for q, _ in TAG_VALUE_CASES],
)
def test_written_tag_values_match_the_slug_spelling(written_query: str, slug_query: str) -> None:
    """A tag value written with spaces or capitals produces identical SQL to its slug, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(written_query)) == generate_sql_query(parse_scryfall_query(slug_query))
    assert generate_sql_query(pyparsing_parse_str_to_query(written_query)) == generate_sql_query(pyparsing_parse_str_to_query(slug_query))


SLUGIFY_CASES = [
    ("open mouth", "open-mouth"),
    ("Open Mouth", "open-mouth"),
    ("  flames  ", "flames"),
    ("already-a-slug", "already-a-slug"),
    ("3 people", "3-people"),
    ("avacyn's collar", "avacyn-s-collar"),
    ("8-ball (marvel)", "8-ball-marvel"),
    ("107.3f x card", "107-3f-x-card"),
    ("", ""),
]


@pytest.mark.parametrize(
    argnames=["written", "expected"],
    argvalues=SLUGIFY_CASES,
    ids=[written or "empty" for written, _ in SLUGIFY_CASES],
)
def test_slugify_tag(written: str, expected: str) -> None:
    """Runs of non-alphanumerics collapse to a single hyphen, with no leading or trailing one."""
    assert slugify_tag(written) == expected
