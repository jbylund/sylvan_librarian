"""Colour and rarity values are validated by the parser, not at SQL generation inside the engine.

`r:foo` and `c:"xyz"` used to parse, then fail while the query engine serialised them -- a WARNING
engine-failure traceback for a typo, before the SQL path 400ed. Bare colours were already checked
(quoted ones were not); rarities were not checked at all. Both parsers now reject them up front,
the way parse_mana_value already does for mana.
"""

import pytest

from api.parsing import generate_sql_query
from api.parsing.nodes import StringValueNode


@pytest.mark.parametrize(
    argnames="query",
    argvalues=[
        "r:foo",
        'r:"foo"',
        "rarity:legendary",
        "r>=epic",
        "r!foo",
        'c:"xyz"',
        'c:""',
        "c:xyz",
        'id<="purple"',
        "produces:plaid",
    ],
    ids=[
        "rarity_word",
        "rarity_quoted",
        "rarity_full_alias",
        "rarity_comparison",
        "rarity_bang",
        "color_quoted",
        "color_quoted_empty",
        "color_word",
        "identity_quoted_comparison",
        "produces_word",
    ],
)
def test_unknown_color_or_rarity_is_a_parse_error(parse_query, query: str) -> None:
    with pytest.raises(ValueError, match=r"Failed to parse query|Invalid (color|rarity)"):
        parse_query(query)


@pytest.mark.parametrize(
    argnames=("query", "value"),
    argvalues=[
        ("r:mythic", "mythic"),
        ("r:m", "m"),
        ("r:Rare", "Rare"),
        ('r:"rare"', "rare"),
        ("rarity>=uncommon", "uncommon"),
        ("r!special", "special"),
        ("c:wubrg", "wubrg"),
        ("c:azorius", "azorius"),
        ("c:WU", "WU"),
        ('c:"wu"', "wu"),
        ('c:"azorius"', "azorius"),
        ("id<=esper", "esper"),
        ("c:yore-tiller", "yore-tiller"),
        ("c:colorless", "colorless"),
        ("produces:c", "c"),
    ],
)
def test_known_color_and_rarity_values_still_parse(parse_query, query: str, value: str) -> None:
    parsed = parse_query(query)
    assert parsed.root.rhs == StringValueNode(value)
    assert generate_sql_query(parsed)[0]
