"""Tests for preprocess_implicit_and: converting implicit AND to explicit in search queries."""

import pytest

from api.parsing.parsing_f import preprocess_implicit_and

TESTCASES = [
    # Basic: adjacent words get AND
    {"query": "a b", "expected": "a AND b", "id": "two_words"},
    {"query": "foo bar", "expected": "foo AND bar", "id": "two_words_long"},
    {"query": "a b c", "expected": "a AND b AND c", "id": "three_words"},
    # Single token unchanged
    {"query": "a", "expected": "a", "id": "single_word"},
    {"query": "name:bolt", "expected": "name : bolt", "id": "single_attr_value"},
    # Already explicit AND/OR — no extra AND between them and operands
    {"query": "a AND b", "expected": "a AND b", "id": "explicit_and"},
    {"query": "a OR b", "expected": "a OR b", "id": "explicit_or"},
    {"query": "a AND b AND c", "expected": "a AND b AND c", "id": "and_chain"},
    {"query": "a OR b OR c", "expected": "a OR b OR c", "id": "or_chain"},
    {"query": "a AND b OR c", "expected": "a AND b OR c", "id": "and_or_mixed_1"},
    {"query": "a OR b AND c", "expected": "a OR b AND c", "id": "and_or_mixed_2"},
    # Attribute:value pairs — AND between pairs, not inside pair
    {"query": "name:foo type:creature", "expected": "name : foo AND type : creature", "id": "two_attr_pairs"},
    {"query": "cmc:3 power:2", "expected": "cmc : 3 AND power : 2", "id": "cmc_power"},
    {"query": "set:iko name:bolt", "expected": "set : iko AND name : bolt", "id": "set_name"},
    # Parentheses
    {"query": "(a b)", "expected": "( a AND b )", "id": "parens_two_words"},
    {"query": "(foo bar) baz", "expected": "( foo AND bar ) AND baz", "id": "parens_then_word"},
    {"query": "a (b c)", "expected": "a AND ( b AND c )", "id": "word_then_parens"},
    {"query": "(a AND b) c", "expected": "( a AND b ) AND c", "id": "parens_with_and_then_word"},
    # Quoted strings (single token, AND around them)
    {"query": '"Lightning Bolt"', "expected": '"Lightning Bolt"', "id": "quoted_single"},
    {"query": 'a "b c" d', "expected": 'a AND "b c" AND d', "id": "quoted_between_words"},
    {"query": 'name:"Lightning Bolt"', "expected": 'name : "Lightning Bolt"', "id": "attr_quoted_value"},
    {"query": '"a" "b"', "expected": '"a" AND "b"', "id": "two_quoted"},
    # Single-quoted strings
    {"query": "'full art'", "expected": "'full art'", "id": "single_quoted"},
    {"query": "a 'b c' d", "expected": "a AND 'b c' AND d", "id": "single_quoted_between"},
    # Regex patterns (slash-delimited, single token)
    {"query": "name:/bolt/", "expected": "name : /bolt/", "id": "regex_single"},
    {"query": "/foo/ /bar/", "expected": "/foo/ AND /bar/", "id": "two_regex"},
    {"query": "name:/bolt/ type:instant", "expected": "name : /bolt/ AND type : instant", "id": "regex_and_attr"},
    # Comparison operators — no AND between attr op value
    {"query": "cmc=3", "expected": "cmc = 3", "id": "cmp_eq"},
    {"query": "cmc>2 power<5", "expected": "cmc > 2 AND power < 5", "id": "cmp_gt_lt"},
    {"query": "cmc>=3 cmc<=5", "expected": "cmc >= 3 AND cmc <= 5", "id": "cmp_gte_lte"},
    {"query": "color!=W", "expected": "color != W", "id": "cmp_neq"},
    # Negation: word then - (minus) gets AND so "-" is separate factor
    {"query": "a -b", "expected": "a AND - b", "id": "negation_word_minus"},
    {"query": "flying -t:creature", "expected": "flying AND - t : creature", "id": "negation_keyword_minus"},
    # Hyphenated words stay one token
    {"query": "some-word", "expected": "some-word", "id": "hyphenated_one"},
    {"query": "some-word other", "expected": "some-word AND other", "id": "hyphenated_and_word"},
    {"query": "a well-known card", "expected": "a AND well-known AND card", "id": "hyphenated_phrase"},
    # Attribute value with hyphen
    {"query": "name:Jace-the-mind", "expected": "name : Jace-the-mind", "id": "attr_value_hyphenated"},
    # Numerics
    {"query": "cmc:3.5", "expected": "cmc : 3.5", "id": "numeric_float"},
    {"query": "1 2 3", "expected": "1 AND 2 AND 3", "id": "numeric_sequence"},
    # Mana symbols / curly
    {"query": "c:{w}{u}", "expected": "c : {w}{u}", "id": "mana_curly"},
    {"query": "c:{1}{G} c:{2}{G}", "expected": "c : {1}{G} AND c : {2}{G}", "id": "mana_two_pairs"},
    # Whitespace normalization (multiple spaces between tokens)
    {"query": "a   b", "expected": "a AND b", "id": "multiple_spaces"},
    {"query": "  a  b  ", "expected": "a AND b", "id": "leading_trailing_space"},
    # Empty and single-space (edge cases)
    {"query": "", "expected": "", "id": "empty"},
    {"query": "   ", "expected": "", "id": "only_spaces"},
]


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
    ],
    ids=["unclosed_double_quote", "unclosed_single_quote", "unclosed_regex"],
)
def test_preprocess_implicit_and_raises_on_invalid(query: str, match: str) -> None:
    """Invalid query (unclosed quote/regex) raises ValueError."""
    with pytest.raises(ValueError, match=match):
        preprocess_implicit_and(query)
