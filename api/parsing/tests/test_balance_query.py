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
def test_balanced_queries_still_parse(parse_query, original_query: str) -> None:
    """Representative balanced partial queries should remain parseable.

    Shared fixture parity coverage lives in test_balance_parity.py. This test keeps a small
    end-to-end integration check that the balanced output still feeds the parser successfully.
    """
    balanced_query = balance_partial_query(original_query)

    # Original should fail (at least for quote cases)
    if '"' in original_query and original_query.count('"') % 2 == 1:
        with pytest.raises(ValueError, match=r"(quote|lex query)"):
            parse_query(original_query)

    # Balanced should succeed
    result = parse_query(balanced_query)
    assert result is not None, f"Failed to parse balanced query: {balanced_query}"


@pytest.mark.parametrize(
    argnames="original_query",
    argvalues=[
        "name:/'/",
        "name:/'s/",
        'name:/"/',
        "name:/a'b/",
        "(name:/'/",
        "name:/'/ (t:goblin",
    ],
)
def test_quotes_inside_a_regex_survive_balancing(parse_query, original_query: str) -> None:
    """Quotes inside a closed /regex/ are pattern characters, so balancing must not close them.

    The balancer used to append a stray quote here, and the browser sends the balanced string —
    so a query that parsed fine became a server-side parse error for something never typed.
    """
    balanced_query = balance_partial_query(original_query)

    assert balanced_query.count("'") == original_query.count("'")
    assert balanced_query.count('"') == original_query.count('"')
    assert parse_query(balanced_query) is not None, f"Failed to parse balanced query: {balanced_query}"
