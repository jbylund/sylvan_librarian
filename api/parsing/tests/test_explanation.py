"""Tests for query explanation functionality."""

import pytest

from api.parsing import parse_scryfall_query


@pytest.mark.parametrize(
    argnames=("query_str", "expected_explanation"),
    argvalues=[
        # Simple numeric comparisons
        ("power>3", "the power > 3"),
        ("toughness>3", "the toughness > 3"),
        ("cmc=3", "the mana value is 3"),
        ("mv=3", "the mana value is 3"),
        # Color identity
        ("id=g", "the color identity is Green"),
        ("id=u", "the color identity is Blue"),
        ("id=ug", "the color identity is Blue/Green"),
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
        ("(power>3 or toughness>3) and id=g f=m", "(the power > 3 or the toughness > 3) and the color identity is Green and it's legal in Modern"),
        # Another complex query
        ("power>3 and (id=g or id=u)", "the power > 3 and (the color identity is Green or the color identity is Blue)"),
        # Complex OR with AND groups - matches ((...) or (...)) pattern
        ("(id=g and t:bird) or (id=r and t:goblin)", "((the color identity is Green and the type contains bird) or (the color identity is Red and the type contains goblin))"),
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
        ("c=w", "the color is White"),
        ("c=b", "the color is Black"),
        # Sets
        ("set:war", "the set contains war"),
        # Artist
        ("artist:Nielsen", "the artist contains Nielsen"),
    ],
)
def test_explain_query(query_str: str, expected_explanation: str) -> None:
    """Test that query explanation generates expected human-readable strings."""
    parsed_query = parse_scryfall_query(query_str)
    explanation = parsed_query.to_human_explanation()
    assert explanation == expected_explanation


def test_explain_empty_query() -> None:
    """Test that empty queries produce empty explanations."""
    parsed_query = parse_scryfall_query("")
    explanation = parsed_query.to_human_explanation()
    # Empty query should produce empty explanation
    assert explanation == ""


def test_explain_multiple_and_conditions() -> None:
    """Test explanation with multiple AND conditions."""
    parsed_query = parse_scryfall_query("power>3 toughness>3 cmc=5")
    explanation = parsed_query.to_human_explanation()
    assert "the power > 3" in explanation
    assert "the toughness > 3" in explanation
    assert "the mana value is 5" in explanation
    assert " and " in explanation


def test_explain_nested_or_and_and() -> None:
    """Test explanation with nested OR and AND."""
    parsed_query = parse_scryfall_query("(power>3 or toughness>3) and cmc=5")
    explanation = parsed_query.to_human_explanation()
    assert "power > 3 or" in explanation
    assert "toughness > 3" in explanation
    assert "and" in explanation
    assert "mana value is 5" in explanation


def test_explain_color_combinations() -> None:
    """Test color code expansion."""
    test_cases = [
        ("id=wubrg", "White/Blue/Black/Red/Green"),
        ("id=rg", "Red/Green"),
        ("c=ub", "Blue/Black"),
    ]
    for query_str, expected_colors in test_cases:
        parsed_query = parse_scryfall_query(query_str)
        explanation = parsed_query.to_human_explanation()
        assert expected_colors in explanation


def test_explain_format_expansion() -> None:
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
        parsed_query = parse_scryfall_query(query_str)
        explanation = parsed_query.to_human_explanation()
        assert expected_format in explanation
