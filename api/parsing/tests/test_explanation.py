"""Tests for query explanation functionality."""

import pytest


@pytest.mark.parametrize(
    argnames=("query_str", "expected_explanation"),
    argvalues=[
        # Simple numeric comparisons
        ("power>3", "the power > 3"),
        ("toughness>3", "the toughness > 3"),
        ("cmc=3", "the mana value is 3"),
        ("mv=3", "the mana value is 3"),
        # Color identity
        ("id=g", "the color identity is Green ({G})"),
        ("id=u", "the color identity is Blue ({U})"),
        ("id=ug", "the color identity is Blue/Green ({U}{G})"),
        # Format
        ("f=m", "it's legal in Modern"),
        ("f=s", "it's legal in Standard"),
        ("format=commander", "it's legal in commander"),
        # Text searches
        ("name:lightning", "the name contains lightning"),
        ("oracle:flying", "the oracle text contains flying"),
        ("type:instant", "the type contains instant"),
        # AND combinations
        ("power>3 toughness>3", "the power > 3 and the toughness > 3"),
        ("cmc=3 power=3", "the mana value is 3 and the power is 3"),
        # OR combinations
        ("power>3 or toughness>3", "(the power > 3 or the toughness > 3)"),
        # Complex query from the issue - with parens around each AND group
        (
            "(power>3 or toughness>3) and id=g f=m",
            "(the power > 3 or the toughness > 3) and the color identity is Green ({G}) and it's legal in Modern",
        ),
        # Another complex query
        ("power>3 and (id=g or id=u)", "the power > 3 and (the color identity is Green ({G}) or the color identity is Blue ({U}))"),
        # Complex OR with AND groups - matches ((...) or (...)) pattern
        (
            "(id=g and t:bird) or (id=r and t:goblin)",
            "((the color identity is Green ({G}) and the type contains bird) or (the color identity is Red ({R}) and the type contains goblin))",
        ),
        # NOT queries
        ("-power>3", "not (the power > 3)"),
        # Rarity
        ("rarity=rare", "the rarity is rare"),
        ("r>=uncommon", "the rarity ≥ uncommon"),
        # Different operators
        ("power>=5", "the power ≥ 5"),
        ("toughness<=2", "the toughness ≤ 2"),
        ("power!=3", "the power is not 3"),
        # Color codes
        ("c=w", "the color is White ({W})"),
        ("c=b", "the color is Black ({B})"),
        # Sets
        ("set:war", "the set contains war"),
        # Artist
        ("artist:Nielsen", "the artist contains Nielsen"),
    ],
)
def test_explain_query(parse_query, query_str: str, expected_explanation: str) -> None:
    """Test that query explanation generates expected human-readable strings."""
    parsed_query = parse_query(query_str)
    explanation = parsed_query.to_human_explanation()
    assert explanation == expected_explanation


def test_explain_empty_query(parse_query) -> None:
    """Test that empty queries produce empty explanations."""
    parsed_query = parse_query("")
    explanation = parsed_query.to_human_explanation()
    # Empty query should produce empty explanation
    assert explanation == ""


@pytest.mark.parametrize(argnames="query_str", argvalues=['mana:""', 'devotion:""'])
def test_explain_empty_mana_value(parse_query, query_str: str) -> None:
    """Empty mana/devotion values must produce an empty explanation.

    An empty mana/devotion value parses to a ManaValueNode (#909), not a StringValueNode — the
    empty-value guard has to cover both or this regresses to a garbled explanation (#950).
    """
    parsed_query = parse_query(query_str)
    explanation = parsed_query.to_human_explanation()
    assert explanation == ""


@pytest.mark.parametrize(
    argnames=["query_str", "expected_explanation"],
    argvalues=[
        # `:` (contains / JSONB containment) against "" is always vacuous -- no real
        # constraint, so it explains to nothing.
        ('o:""', ""),
        ('name:""', ""),
        # `=` against "" is the opposite: a real, narrow constraint (the field is exactly
        # empty), so it must render as its own clause instead of collapsing.
        ('o=""', "the oracle text is empty"),
        ('name=""', "the name is empty"),
        ('mana=""', "the mana cost is empty"),
        ('devotion=""', "the devotion is empty"),
    ],
)
def test_explain_empty_value_operator_dependent(parse_query, query_str: str, expected_explanation: str) -> None:
    """Whether an empty value collapses to "" depends on the operator, not just the value.

    Verified against the live SQL each spelling produces: `:`/JSONB-containment against ""
    is a tautology (`LIKE '%'`, `{} <@ anything`), `=` against "" is a genuine filter
    (`oracle_text = ''`), so only the former is safe to drop from an explanation.
    """
    parsed_query = parse_query(query_str)
    explanation = parsed_query.to_human_explanation()
    assert explanation == expected_explanation


@pytest.mark.parametrize(
    argnames=["query_str", "expected_explanation"],
    argvalues=[
        # `=` is real equality against the whole field on these text columns (verified
        # against the SQL: `name=X` -> `card_name = X`, `name:X` -> `LIKE %X%`, same split
        # for oracle_text/card_types/card_artist) -- Scryfall itself treats `o=`/`o:` as
        # pure synonyms, but this codebase's own `=` diverges deliberately (rewrite.py's
        # is:vanilla relies on it to express "no oracle text at all"), so the explanation
        # must say "is X", not "contains X", to describe what actually gets checked.
        ("name=bolt", "the name is bolt"),
        ("name:bolt", "the name contains bolt"),
        ("o=flying", "the oracle text is flying"),
        ("o:flying", "the oracle text contains flying"),
        ("type=goblin", "the type is goblin"),
        ("type:goblin", "the type contains goblin"),
        ("artist=nielsen", "the artist is nielsen"),
        ("artist:nielsen", "the artist contains nielsen"),
    ],
)
def test_explain_equals_vs_contains(parse_query, query_str: str, expected_explanation: str) -> None:
    """= reads as "is" (equality) and : reads as "contains" (substring), matching the SQL."""
    parsed_query = parse_query(query_str)
    explanation = parsed_query.to_human_explanation()
    assert explanation == expected_explanation


@pytest.mark.parametrize(
    argnames=["query_str", "expected_explanation"],
    argvalues=[
        # `o=""` is a real, narrow constraint (the oracle text is exactly empty) and must render as
        # its own clause, not vanish. Upstream reaches this through is:vanilla's expansion to
        # `t:creature o=""`; on this branch is:vanilla is an engine predicate (see rewrite.py), so
        # the expansion is spelled out.
        ('t:creature o=""', "the type contains creature and the oracle text is empty"),
        ('-(t:creature o="")', "not (the type contains creature and the oracle text is empty)"),
        # A typeahead balancer auto-closing a half-typed "urza'" produces `name:urza''`,
        # which parses as `name:urza AND name:''` -- the second operand uses `:` against an
        # empty value, which is always vacuous (LIKE '%' matches everything) and explains to
        # "", so it must be filtered out of the AND join rather than left as a dangling
        # connector.
        ("name:urza''", "the name contains urza"),
    ],
)
def test_explain_filters_empty_string_operand(parse_query, query_str: str, expected_explanation: str) -> None:
    """A vacuous (`:`) empty-value operand drops out of the join; a narrow (`=`) one doesn't."""
    parsed_query = parse_query(query_str)
    explanation = parsed_query.to_human_explanation()
    assert explanation == expected_explanation


def test_explain_multiple_and_conditions(parse_query) -> None:
    """Test explanation with multiple AND conditions."""
    parsed_query = parse_query("power>3 toughness>3 cmc=5")
    explanation = parsed_query.to_human_explanation()
    assert "the power > 3" in explanation
    assert "the toughness > 3" in explanation
    assert "the mana value is 5" in explanation
    assert " and " in explanation


def test_explain_nested_or_and_and(parse_query) -> None:
    """Test explanation with nested OR and AND."""
    parsed_query = parse_query("(power>3 or toughness>3) and cmc=5")
    explanation = parsed_query.to_human_explanation()
    assert "power > 3 or" in explanation
    assert "toughness > 3" in explanation
    assert "and" in explanation
    assert "mana value is 5" in explanation


def test_explain_color_combinations(parse_query) -> None:
    """Test color code expansion."""
    test_cases = [
        ("id=wubrg", "White/Blue/Black/Red/Green"),
        ("id=rg", "Red/Green"),
        ("c=ub", "Blue/Black"),
    ]
    for query_str, expected_colors in test_cases:
        parsed_query = parse_query(query_str)
        explanation = parsed_query.to_human_explanation()
        assert expected_colors in explanation


@pytest.mark.parametrize(
    argnames=["query_str", "expected_explanation"],
    argvalues=[
        # Repeated/scrambled letters dedupe and land in canonical WUBRG order, not input order.
        ("c=brgb", "the color is Black/Red/Green ({B}{R}{G})"),
        ("id=brgb", "the color identity is Black/Red/Green ({B}{R}{G})"),
        ("id=rug", "the color identity is Blue/Red/Green ({U}{R}{G})"),
        ("c=gwbur", "the color is White/Blue/Black/Red/Green ({W}{U}{B}{R}{G})"),
        # Guild/shard/wedge names show the letters they spell alongside the name, as
        # {G}{U}{R}-style bracket tokens the frontend renders into mana-font icons (#990).
        ("c=temur", "the color is Temur ({G}{U}{R})"),
        ("id=temur", "the color identity is Temur ({G}{U}{R})"),
        ("c=azorius", "the color is Azorius ({W}{U})"),
        # Single-color names expand the same as bare letter codes.
        ("c=blue", "the color is Blue ({U})"),
        ("c=colorless", "the color is Colorless ({C})"),
        ("id=green", "the color identity is Green ({G})"),
    ],
)
def test_explain_color_dedup_and_names(parse_query, query_str: str, expected_explanation: str) -> None:
    """Color explanations dedupe/canonicalize letters and expand alias names with their letters."""
    parsed_query = parse_query(query_str)
    assert parsed_query.to_human_explanation() == expected_explanation


def test_explain_format_expansion(parse_query) -> None:
    """Test format code expansion."""
    test_cases = [
        ("f=m", "Modern"),
        ("f=s", "Standard"),
        ("f=v", "Vintage"),
        ("f=l", "Legacy"),
        ("f=p", "Pauper"),
        ("f=c", "Commander"),
    ]
    for query_str, expected_format in test_cases:
        parsed_query = parse_query(query_str)
        explanation = parsed_query.to_human_explanation()
        assert expected_format in explanation
