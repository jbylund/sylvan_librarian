"""Tests for query balancing functionality."""

import pytest

from api.parsing import balance_partial_query


@pytest.mark.parametrize(
    argnames="original_query",
    argvalues=[
        'name:"hydr',
        '(name:"lightning',
    ],
)
def test_balance_query_integration_with_parsing(parse_query, original_query: str) -> None:
    """Test that balanced queries can be successfully parsed."""
    balanced_query = balance_partial_query(original_query)

    # Original should fail (at least for quote cases)
    if '"' in original_query and original_query.count('"') % 2 == 1:
        with pytest.raises(ValueError, match=r"(quote|lex query)"):
            parse_query(original_query)

    # Balanced should succeed
    result = parse_query(balanced_query)
    assert result is not None, f"Failed to parse balanced query: {balanced_query}"
