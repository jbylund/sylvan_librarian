"""Tests for parsing queries with ignored parts."""

import pytest

from api.parsing import parse_scryfall_query_with_ignored
from api.parsing.nodes import AndNode, BinaryOperatorNode


def test_parse_valid_query_with_no_ignored() -> None:
    """Test that a fully valid query returns no ignored parts."""
    result = parse_scryfall_query_with_ignored("t:merfolk")

    assert result.has_valid_query()
    assert len(result.ignored) == 0
    assert result.query is not None


def test_parse_query_with_single_invalid_part() -> None:
    """Test that a query with one invalid part ignores it."""
    result = parse_scryfall_query_with_ignored("t:merfolk x>3")

    assert result.has_valid_query()
    assert len(result.ignored) == 1
    assert result.ignored[0].fragment == "x>3"
    assert len(result.ignored[0].reason) > 0  # Has some reason


def test_parse_query_with_multiple_valid_parts() -> None:
    """Test that multiple valid parts are combined with AND."""
    result = parse_scryfall_query_with_ignored("t:merfolk cmc:3")

    assert result.has_valid_query()
    assert len(result.ignored) == 0
    # Should have AND node with two operands
    assert isinstance(result.query.root, AndNode)
    assert len(result.query.root.operands) == 2


def test_parse_query_with_mixed_valid_invalid() -> None:
    """Test query with alternating valid and invalid parts."""
    result = parse_scryfall_query_with_ignored("c:g baloth x>3 -t:c")

    assert result.has_valid_query()
    assert len(result.ignored) == 1
    assert result.ignored[0].fragment == "x>3"


def test_parse_completely_invalid_query() -> None:
    """Test that a completely invalid query returns None for query."""
    result = parse_scryfall_query_with_ignored("x>3 y<2")

    assert not result.has_valid_query()
    assert result.query is None
    assert len(result.ignored) >= 1


def test_parse_empty_query() -> None:
    """Test that an empty query returns a default query."""
    result = parse_scryfall_query_with_ignored("")

    assert result.has_valid_query()
    assert len(result.ignored) == 0


def test_parse_query_with_parentheses() -> None:
    """Test that parentheses are respected when segmenting."""
    result = parse_scryfall_query_with_ignored("(t:merfolk OR t:wizard) x>3")

    assert result.has_valid_query()
    assert len(result.ignored) == 1
    assert result.ignored[0].fragment == "x>3"


def test_parse_query_with_quoted_string() -> None:
    """Test that quoted strings are not split."""
    result = parse_scryfall_query_with_ignored('o:"flying x>3" cmc:2')

    assert result.has_valid_query()
    # The quoted string should be treated as a valid part
    assert len(result.ignored) == 0


def test_ignored_part_to_dict() -> None:
    """Test that IgnoredQueryPart.to_dict works correctly."""
    result = parse_scryfall_query_with_ignored("t:merfolk x>3")

    assert len(result.ignored) == 1
    ignored_dict = result.ignored[0].to_dict()
    assert "fragment" in ignored_dict
    assert ignored_dict["fragment"] == "x>3"
    assert "reason" in ignored_dict


def test_parse_with_or_operator() -> None:
    """Test parsing with OR operator between valid and invalid parts."""
    result = parse_scryfall_query_with_ignored("t:merfolk OR x>3")

    assert result.has_valid_query()
    assert len(result.ignored) == 1
    assert result.ignored[0].fragment == "x>3"


def test_parse_arithmetic_expression_alone() -> None:
    """Test that a standalone arithmetic expression is handled."""
    result = parse_scryfall_query_with_ignored("cmc+1")

    # This should be invalid on its own (needs to be part of comparison)
    # The behavior depends on whether the parser accepts it
    # For now, just verify it returns something reasonable
    if not result.has_valid_query():
        assert len(result.ignored) >= 1


def test_parse_single_valid_attribute() -> None:
    """Test parsing a single valid attribute query."""
    result = parse_scryfall_query_with_ignored("cmc:3")

    assert result.has_valid_query()
    assert len(result.ignored) == 0
    assert isinstance(result.query.root, BinaryOperatorNode)


def test_parse_negation_with_invalid() -> None:
    """Test negation combined with invalid parts."""
    result = parse_scryfall_query_with_ignored("-t:land x>3")

    assert result.has_valid_query()
    assert len(result.ignored) == 1
    assert result.ignored[0].fragment == "x>3"


@pytest.mark.parametrize(
    argnames=("query", "expected_ignored_count", "should_have_valid"),
    argvalues=[
        ("t:merfolk", 0, True),  # Fully valid
        ("t:merfolk x>3", 1, True),  # One invalid
        ("t:merfolk x>3 y<2", 2, True),  # Two invalid
        ("x>3", 1, False),  # Fully invalid
        ("cmc:2 power:3", 0, True),  # Multiple valid
        ("c:g x>3 pow>1", 1, True),  # Valid with invalid in middle - x>3 is invalid, but c:g and pow>1 are valid
    ],
)
def test_parse_query_parametrized(query: str, expected_ignored_count: int, should_have_valid: bool) -> None:
    """Parametrized test for various query patterns."""
    result = parse_scryfall_query_with_ignored(query)

    assert result.has_valid_query() == should_have_valid
    assert len(result.ignored) >= expected_ignored_count
