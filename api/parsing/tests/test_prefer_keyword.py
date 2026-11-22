"""Tests for the `prefer:` query keyword, lifted out of the tree onto the parsed Query.

Scryfall reads `prefer:usd-low` as a choice of which printing of each card to show, not as a
filter, so the keyword never reaches SQL: it is lifted onto ``Query.prefer``.
"""

from __future__ import annotations

import pytest

from api.enums import PreferOrder
from api.parsing import generate_sql_query
from api.parsing.prefer import PREFER_VALUES, prefer_order_name


@pytest.mark.parametrize(
    ("query", "expected_prefer", "expected_without"),
    [
        ("prefer:usd-low", "usd-low", ""),
        ("t:elf prefer:usd-low", "usd-low", "t:elf"),
        ("prefer:oldest t:elf", "oldest", "t:elf"),
        ("t:elf prefer:NEWEST cmc=2", "newest", "t:elf cmc=2"),
        ("t:elf prefer=usd-high", "usd-high", "t:elf"),
        ("(t:elf or t:goblin) prefer:promo", "promo", "(t:elf or t:goblin)"),
        ("t:elf prefer:default", "default", "t:elf"),
        ("t:elf", None, "t:elf"),
    ],
)
def test_prefer_is_lifted_out_of_the_tree(parse_query, query: str, expected_prefer: str | None, expected_without: str) -> None:
    """The lifted value lands on Query.prefer and the rest parses to the same tree as without it."""
    parsed = parse_query(query)
    assert parsed.prefer == expected_prefer
    assert parsed.root == parse_query(expected_without).root
    assert generate_sql_query(parsed) == generate_sql_query(parse_query(expected_without))


@pytest.mark.parametrize(
    "query",
    [
        "t:elf or prefer:usd-low",  # inside an or
        "-prefer:usd-low t:elf",  # negated
        "t:elf (t:goblin or prefer:oldest)",  # nested under an or
        "prefer:usd-low prefer:oldest",  # more than once
        "prefer:cheapest",  # unknown value
        "prefer:eur-low",  # a Scryfall value this engine has no ordering for
        "prefer>oldest",  # not a comparison
        "prefer:/usd/",  # not a regex
    ],
)
def test_misplaced_or_unknown_prefer_is_a_parse_error(parse_query, query: str) -> None:
    """A prefer: that cannot be lifted, or names no known ordering, is rejected rather than ignored."""
    with pytest.raises(ValueError, match="prefer"):
        parse_query(query)


def test_prefer_values_match_the_api_prefer_parameter() -> None:
    """Every query spelling maps onto exactly one PreferOrder member, and every member has a spelling."""
    assert {PreferOrder(prefer_order_name(value)) for value in PREFER_VALUES} == set(PreferOrder)
