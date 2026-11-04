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
        ("name:/unclosed", "Unmatched"),
        # Escaped-slash valid regex followed by a separate unclosed regex: parity-based
        # counting would give an even slash-count and miss this — must still raise.
        (r"name:/a\/b/ type:/unclosed", "Unmatched"),
        # A slash directly behind an operator opens a regex wherever the operator is, so one that
        # never closes is still an error after a stray slash elsewhere in the query.
        ("fire // o:/unclosed", "Unmatched"),
        # Behind a negation or the exact-name bang a slash has no reading at all.
        ("-/fire", "Unmatched"),
        ("!/fire", "Unmatched"),
        # Nothing but slashes is no query: Scryfall's "All of your terms were ignored."
        ("/", "Unmatched"),
        ("//", "Unmatched"),
        (" / / ", "Unmatched"),
    ],
    ids=[
        "unclosed_double_quote",
        "unclosed_single_quote",
        "unclosed_regex_after_attr",
        "unclosed_regex_after_escaped_slash_regex",
        "unclosed_regex_after_stray_slash",
        "slash_after_negation",
        "slash_after_exact_name_bang",
        "only_a_slash",
        "only_two_slashes",
        "only_spaced_slashes",
    ],
)
def test_preprocess_implicit_and_raises_on_invalid(query: str, match: str) -> None:
    """Invalid query (unclosed quote/regex) raises ValueError."""
    with pytest.raises(ValueError, match=match):
        preprocess_implicit_and(query)


@pytest.mark.parametrize(
    argnames=("query", "expected"),
    argvalues=[
        # These five raised "Unmatched / in regex pattern" until a slash that no term has taken
        # became what it is on Scryfall -- nothing. A regex still only opens in value position, so
        # none of them is a pattern: the words between the slashes are plain name words.
        ("/unclosed regex", "unclosed AND regex"),
        ("type:instant /unclosed", "type:instant AND unclosed"),
        ("a /unclosed", "a AND unclosed"),
        ("/bolt/", "bolt"),
        ("/foo/ /bar/", "foo AND bar"),
    ],
    ids=[
        "leading_slash",
        "slash_after_plain_value",
        "slash_after_plain_word",
        "bare_regex_shape",
        "two_bare_regex_shapes",
    ],
)
def test_a_slash_outside_value_position_is_dropped_not_a_regex(query: str, expected: str) -> None:
    """A slash that is neither division nor the opening of a regex is dropped from the query."""
    assert preprocess_implicit_and(query) == expected
