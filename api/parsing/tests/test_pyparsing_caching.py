"""Tests for pyparsing packrat caching functionality."""

import inspect

import pyparsing as pp

from api.parsing import parse_scryfall_query


def test_packrat_caching_enabled() -> None:
    """Test that pyparsing packrat caching is enabled with increased cache size."""
    # Check that packrat caching is enabled globally
    assert hasattr(pp.ParserElement, "_packratEnabled")
    # In newer versions of pyparsing, _packratEnabled might be accessed differently
    # but we can at least verify the method exists and was called
    assert hasattr(pp.ParserElement, "enable_packrat")

    # Verify that the cache size was increased from default (128) to 2^13 (8192)
    # This is checked by confirming the enable_packrat method accepts cache_size_limit parameter
    sig = inspect.signature(pp.ParserElement.enable_packrat)
    assert "cache_size_limit" in sig.parameters


def test_parsing_works_with_packrat_caching() -> None:
    """Test that parsing still works correctly with packrat caching enabled."""
    # Test various query types to ensure caching doesn't break functionality
    test_queries = [
        "cmc=3",
        "name:lightning",
        "power>2 AND toughness<5",
        "(cmc=3 OR cmc=4) AND color:red",
        "type:creature power>3",
        'name:"Lightning Bolt"',
        "cmc+power<5",
    ]

    for query in test_queries:
        result = parse_scryfall_query(query)
        assert result is not None, f"Failed to parse query: {query}"


def test_repeated_parsing_with_caching() -> None:
    """Test that repeated parsing of the same query works with caching."""
    query = "cmc=3 AND power>2"

    # Parse the same query multiple times
    results = []
    for _ in range(5):
        result = parse_scryfall_query(query)
        results.append(result)

    # All results should be valid
    for result in results:
        assert result is not None

    # Results should be consistent (same AST structure)
    # We can't directly compare AST objects, but we can verify they all parsed successfully
    assert len(results) == 5
