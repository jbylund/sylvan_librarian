"""Tests for query balancing functionality."""

import pytest

from api.parsing import balance_partial_query, parse_scryfall_query


@pytest.mark.parametrize(
    argnames=("input_query", "expected_balanced"),
    argvalues=[
        # Quote balancing tests
        ('name:"hydr', 'name:"hydr"'),
        ("name:'hydr", "name:'hydr'"),
        ('name:"hydra"', 'name:"hydra"'),  # already balanced
        ("name:'hydra'", "name:'hydra'"),  # already balanced
        ('name:"hydr" power:3', 'name:"hydr" power:3'),  # mixed balanced
        ('name:"hydr power:3', 'name:"hydr power:3"'),  # quote spans multiple terms
        # Parentheses balancing tests
        ("(t:goblin c:r) or (t:beast c:g", "(t:goblin c:r) or (t:beast c:g)"),
        ("(t:goblin c:r) or (t:beast c:g)", "(t:goblin c:r) or (t:beast c:g)"),  # already balanced
        ("((cmc=3) and (power>2", "((cmc=3) and (power>2))"),
        ("(((nested", "(((nested)))"),
        # Combined quote and parentheses tests
        ('(name:"lightning', '(name:"lightning")'),
        ('(name:"lightning bolt" and (power>2', '(name:"lightning bolt" and (power>2))'),
        # Edge cases
        ("", ""),
        ("simple", "simple"),
        ("name:hydra", "name:hydra"),
        # Apostrophe inside double-quoted strings should not be treated as a quote delimiter
        ('!"stormchaser\'s talent"', '!"stormchaser\'s talent"'),  # already balanced
        ('!"stormchaser\'s tal', '!"stormchaser\'s tal"'),  # partial, needs closing "
        ("name:\"dragon's breath\"", "name:\"dragon's breath\""),  # already balanced
        ("name:\"dragon's brea", "name:\"dragon's brea\""),  # partial, needs closing "
        # Double quotes inside single-quoted strings should not be treated as quote delimiters
        ("name:'say \"hello\"'", "name:'say \"hello\"'"),  # already balanced
        ("name:'say \"hel", "name:'say \"hel'"),  # partial, needs closing '
    ],
)
def test_balance_query(input_query: str, expected_balanced: str) -> None:
    """Test that the balance_partial_query function correctly balances quotes and parentheses."""
    result = balance_partial_query(input_query)
    assert result == expected_balanced, f"Input: '{input_query}' -> Expected: '{expected_balanced}', Got: '{result}'"


@pytest.mark.parametrize(
    argnames="original_query",
    argvalues=[
        'name:"hydr',
        '(name:"lightning',
    ],
)
def test_balance_query_integration_with_parsing(original_query: str) -> None:
    """Test that balanced queries can be successfully parsed."""
    balanced_query = balance_partial_query(original_query)

    # Original should fail (at least for quote cases)
    if '"' in original_query and original_query.count('"') % 2 == 1:
        with pytest.raises(ValueError, match="Unmatched"):
            parse_scryfall_query(original_query)

    # Balanced should succeed
    result = parse_scryfall_query(balanced_query)
    assert result is not None, f"Failed to parse balanced query: {balanced_query}"


@pytest.mark.parametrize(
    argnames="input_query",
    argvalues=[
        "hello)",
        "test)more",
        ")start",
    ],
)
def test_balance_query_unbalanced_closing_chars(input_query: str) -> None:
    """Test that unbalanced closing characters raise ValueError."""
    with pytest.raises(ValueError, match=r"Unbalanced closing character.*cannot be balanced"):
        balance_partial_query(input_query)
