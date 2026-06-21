"""Tests for LIKE pattern escaping to prevent wildcard injection."""

import pytest

from api.parsing.card_query_nodes import ExactNameNode, _escape_like_pattern
from api.parsing.nodes import QueryContext

# ---------------------------------------------------------------------------
# _escape_like_pattern unit tests
# ---------------------------------------------------------------------------

testcases_helper = {
    "backslash_escaped": {"value": "a\\b", "expected": "a\\\\b"},
    "backslash_before_percent_both_escaped": {"value": "\\%", "expected": r"\\\%"},
    "all_three_special_chars": {"value": "a\\_b%", "expected": r"a\\\_b\%"},
    "percent_escaped": {"value": "50%", "expected": r"50\%"},
    "plain_text_unchanged": {"value": "lightning", "expected": "lightning"},
    "underscore_escaped": {"value": "a_b", "expected": r"a\_b"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases_helper.values()))),
    argvalues=[[v for k, v in sorted(testcases_helper[tc].items())] for tc in sorted(testcases_helper)],
    ids=sorted(testcases_helper),
)
def test_escape_like_pattern(expected: str, value: str) -> None:
    """_escape_like_pattern produces correctly escaped LIKE patterns."""
    assert _escape_like_pattern(value) == expected


# ---------------------------------------------------------------------------
# ExactNameNode — special characters are escaped literally (no wildcard bleed)
# ---------------------------------------------------------------------------

testcases_exact_name = {
    "percent_does_not_become_wildcard": {
        "input_value": "50%",
        "expected_param": r"50\%",
    },
    "underscore_does_not_match_any_char": {
        "input_value": "A_B",
        "expected_param": r"a\_b",
    },
    "backslash_does_not_corrupt_escape_sequence": {
        "input_value": "A\\B",
        "expected_param": "a\\\\b",
    },
    "backslash_then_percent_both_escaped": {
        "input_value": "\\%",
        "expected_param": r"\\\%",
    },
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases_exact_name.values()))),
    argvalues=[[v for k, v in sorted(testcases_exact_name[tc].items())] for tc in sorted(testcases_exact_name)],
    ids=sorted(testcases_exact_name),
)
def test_exact_name_node_escapes_special_chars(expected_param: str, input_value: str) -> None:
    """ExactNameNode escapes backslash, % and _ so they never act as LIKE wildcards."""
    context: QueryContext = QueryContext()
    node = ExactNameNode(input_value)
    node.to_sql(context)
    assert list(context.values()) == [expected_param]


# ---------------------------------------------------------------------------
# Pattern matching (name:, oracle:, etc.) — special chars inside words are escaped
# but the surrounding % wildcards are preserved.
# Note: the query parser consumes backslashes, so backslash is not reachable via
# this path; it is covered by test_escape_like_pattern above.
# ---------------------------------------------------------------------------

testcases_pattern = {
    "percent_in_search_term_escaped_inside_wildcards": {
        "query": 'name:"50%"',
        "expected_param": r"%50\%%",
    },
    "underscore_in_search_term_escaped_inside_wildcards": {
        "query": 'name:"a_b"',
        "expected_param": r"%a\_b%",
    },
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases_pattern.values()))),
    argvalues=[[v for k, v in sorted(testcases_pattern[tc].items())] for tc in sorted(testcases_pattern)],
    ids=sorted(testcases_pattern),
)
def test_pattern_matching_escapes_special_chars(parse_query, expected_param: str, query: str) -> None:
    """Pattern-matching queries escape % and _ so they never act as LIKE wildcards."""
    parsed = parse_query(query)
    context: QueryContext = QueryContext()
    parsed.to_sql(context)
    assert list(context.values()) == [expected_param]
