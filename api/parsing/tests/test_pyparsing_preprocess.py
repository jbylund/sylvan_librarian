"""Tests for preprocess_implicit_and: converting implicit AND to explicit in search queries."""

import pytest

from api.parsing.pyparsing_based import preprocess_implicit_and
from api.parsing.tests.implicit_and_cases import TESTCASES


@pytest.mark.parametrize(
    argnames=("query", "expected"),
    argvalues=[(c["query"], c["expected"]) for c in TESTCASES],
    ids=[c["id"] for c in TESTCASES],
)
def test_preprocess_implicit_and(query: str, expected: str) -> None:
    """Preprocess converts implicit AND to explicit; before/after as given."""
    assert preprocess_implicit_and(query) == expected


@pytest.mark.parametrize(
    argnames=("query", "match"),
    argvalues=[
        ('"unclosed double', "Unmatched"),
        ("'unclosed single", "Unmatched"),
        ("/unclosed regex", "Unmatched"),
        ("name:/unclosed", "Unmatched"),
        # Escaped-slash valid regex followed by a separate unclosed regex: parity-based
        # counting would give an even slash-count and miss this — must still raise.
        (r"name:/a\/b/ type:/unclosed", "Unmatched"),
        # Unclosed regex after a plain word token (prev_tok is not a numeric operand).
        ("type:instant /unclosed", "Unmatched"),
        ("a /unclosed", "Unmatched"),
    ],
    ids=[
        "unclosed_double_quote",
        "unclosed_single_quote",
        "unclosed_regex",
        "unclosed_regex_after_attr",
        "unclosed_regex_after_escaped_slash_regex",
        "unclosed_regex_after_plain_value",
        "unclosed_regex_after_plain_word",
    ],
)
def test_preprocess_implicit_and_raises_on_invalid(query: str, match: str) -> None:
    """Invalid query (unclosed quote/regex) raises ValueError."""
    with pytest.raises(ValueError, match=match):
        preprocess_implicit_and(query)
