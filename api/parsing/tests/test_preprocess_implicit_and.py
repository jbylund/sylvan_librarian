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
    {"query": "name:bolt", "expected": "name:bolt", "id": "single_attr_value"},
    # Already explicit AND/OR — no extra AND between them and operands
    {"query": "a AND b", "expected": "a AND b", "id": "explicit_and"},
    {"query": "a OR b", "expected": "a OR b", "id": "explicit_or"},
    {"query": "a AND b AND c", "expected": "a AND b AND c", "id": "and_chain"},
    {"query": "a OR b OR c", "expected": "a OR b OR c", "id": "or_chain"},
    {"query": "a AND b OR c", "expected": "a AND b OR c", "id": "and_or_mixed_1"},
    {"query": "a OR b AND c", "expected": "a OR b AND c", "id": "and_or_mixed_2"},
    # Attribute:value pairs — AND between pairs, not inside pair
    {"query": "name:foo type:creature", "expected": "name:foo AND type:creature", "id": "two_attr_pairs"},
    {"query": "cmc:3 power:2", "expected": "cmc:3 AND power:2", "id": "cmc_power"},
    {"query": "set:iko name:bolt", "expected": "set:iko AND name:bolt", "id": "set_name"},
    # Parentheses
    {"query": "(a b)", "expected": "(a AND b)", "id": "parens_two_words"},
    {"query": "(foo bar) baz", "expected": "(foo AND bar) AND baz", "id": "parens_then_word"},
    {"query": "a (b c)", "expected": "a AND (b AND c)", "id": "word_then_parens"},
    {"query": "(a AND b) c", "expected": "(a AND b) AND c", "id": "parens_with_and_then_word"},
    # Quoted strings (single token, AND around them)
    {"query": '"Lightning Bolt"', "expected": '"Lightning Bolt"', "id": "quoted_single"},
    {"query": 'a "b c" d', "expected": 'a AND "b c" AND d', "id": "quoted_between_words"},
    {"query": 'name:"Lightning Bolt"', "expected": 'name:"Lightning Bolt"', "id": "attr_quoted_value"},
    {"query": '"a" "b"', "expected": '"a" AND "b"', "id": "two_quoted"},
    # Single-quoted strings
    {"query": "'full art'", "expected": "'full art'", "id": "single_quoted"},
    {"query": "a 'b c' d", "expected": "a AND 'b c' AND d", "id": "single_quoted_between"},
    # Regex patterns (slash-delimited, single token)
    {"query": "name:/bolt/", "expected": "name:/bolt/", "id": "regex_single"},
    {"query": "/foo/ /bar/", "expected": "/foo/ AND /bar/", "id": "two_regex"},
    {"query": "name:/bolt/ type:instant", "expected": "name:/bolt/ AND type:instant", "id": "regex_and_attr"},
    # Comparison operators — no AND between attr op value
    {"query": "cmc=3", "expected": "cmc=3", "id": "cmp_eq"},
    {"query": "cmc<1r", "expected": "cmc<1r", "id": "cmp_lt_1r_no_space"},
    {"query": "cmc<1 r", "expected": "cmc<1 AND r", "id": "cmp_lt_1_r_with_space"},
    {"query": "cmc>2 power<5", "expected": "cmc>2 AND power<5", "id": "cmp_gt_lt"},
    {"query": "cmc>=3 cmc<=5", "expected": "cmc>=3 AND cmc<=5", "id": "cmp_gte_lte"},
    {"query": "color!=W", "expected": "color!=W", "id": "cmp_neq"},
    # Arithmetic in comparison — no AND inside expression
    {"query": "power+toughness>cmc+cmc", "expected": "power+toughness>cmc+cmc", "id": "arithmetic_comparison"},
    {
        "query": "power+toughness>cmc+cmc+1 fire",
        "expected": "power+toughness>cmc+cmc+1 AND fire",
        "id": "arithmetic_comparison_and_word",
    },
    # Negation: word then - (minus) gets AND so "-" is separate factor
    {"query": "a -b", "expected": "a AND -b", "id": "negation_word_minus"},
    {"query": "flying -t:creature", "expected": "flying AND -t:creature", "id": "negation_keyword_minus"},
    {"query": "id=r -o:enchantment", "expected": "id=r AND -o:enchantment", "id": "attr_negation_attr"},
    # Arithmetic subtraction: numeric-attr - numeric-attr must not get AND (regression guard)
    {"query": "power - cmc>1", "expected": "power-cmc>1", "id": "arith_sub_attr_attr_space"},
    {"query": "power - cmc > 1", "expected": "power-cmc>1", "id": "arith_sub_attr_attr_spaces"},
    {"query": "toughness - power > 0", "expected": "toughness-power>0", "id": "arith_sub_toughness_power"},
    {"query": "cmc - 1 > 0", "expected": "cmc-1>0", "id": "arith_sub_attr_literal"},
    {"query": "5 - cmc > 0", "expected": "5-cmc>0", "id": "arith_sub_literal_attr"},
    # Arithmetic subtraction after a closing paren (e.g. (expr)-literal>value)
    {"query": "(2*power)-1>3", "expected": "(2*power)-1>3", "id": "arith_sub_paren_minus_literal"},
    {"query": "(2*power) - 1 > 3", "expected": "(2*power)-1>3", "id": "arith_sub_paren_minus_literal_spaces"},
    {"query": "(power+toughness)-cmc>0", "expected": "(power+toughness)-cmc>0", "id": "arith_sub_paren_minus_attr"},
    {"query": "(power+toughness) - cmc > 0", "expected": "(power+toughness)-cmc>0", "id": "arith_sub_paren_minus_attr_spaces"},
    # Arithmetic subtraction with a paren group on the right (e.g. attr-(expr)>value)
    {"query": "power-(cmc-1)>2", "expected": "power-(cmc-1)>2", "id": "arith_sub_attr_minus_paren"},
    {"query": "power - (cmc - 1) > 2", "expected": "power-(cmc-1)>2", "id": "arith_sub_attr_minus_paren_spaces"},
    {"query": "(power+1)-(cmc-1)>0", "expected": "(power+1)-(cmc-1)>0", "id": "arith_sub_paren_minus_paren"},
    {"query": "(power + 1) - (cmc - 1) > 0", "expected": "(power+1)-(cmc-1)>0", "id": "arith_sub_paren_minus_paren_spaces"},
    # Comparison with negation on right-hand side: power > -cmc+5 must NOT get AND
    {"query": "power>-cmc+5", "expected": "power>-cmc+5", "id": "cmp_rhs_negation"},
    {"query": "power > -cmc + 5", "expected": "power>-cmc+5", "id": "cmp_rhs_negation_spaces"},
    # Comparison then an expression starting with '-': must insert AND (not treat as arithmetic)
    {"query": "Power>2 -1+CMC<2", "expected": "Power>2 AND -1+CMC<2", "id": "cmp_then_arith_leading_minus"},
    {"query": "power>2 -cmc>0", "expected": "power>2 AND -cmc>0", "id": "cmp_then_neg_numeric_attr"},
    {"query": "power>2 -toughness>0", "expected": "power>2 AND -toughness>0", "id": "cmp_then_neg_toughness"},
    {"query": "power>2 -(cmc-1)>0", "expected": "power>2 AND -(cmc-1)>0", "id": "cmp_then_neg_paren"},
    {"query": "power>2 cmc<3", "expected": "power>2 AND cmc<3", "id": "two_comparisons_and"},
    # Arithmetic within comparison (not after comparison RHS): must not insert AND
    {"query": "power*2 - 1 > 0", "expected": "power*2-1>0", "id": "arith_mul_sub_lit"},
    # Negation with non-numeric attribute on right: must still insert AND
    {"query": "power -type:creature", "expected": "power AND -type:creature", "id": "arith_not_text_attr"},
    # Leading arithmetic expression starting with '-': no implicit AND
    {"query": "-cmc+5>1", "expected": "-cmc+5>1", "id": "leading_arith_minus_cmc"},
    {"query": "-power>0", "expected": "-power>0", "id": "leading_arith_minus_power"},
    # Word then arithmetic expression starting with '-': implicit AND between word and expression
    {"query": "fire -cmc+5>1", "expected": "fire AND -cmc+5>1", "id": "word_then_arith_leading_minus"},
    {"query": "flying -cmc+5>1", "expected": "flying AND -cmc+5>1", "id": "word_then_arith_leading_minus_flying"},
    # Leading negation and multiple negations
    {"query": "-t:creature", "expected": "-t:creature", "id": "leading_negation"},
    {"query": "a -b -c", "expected": "a AND -b AND -c", "id": "multiple_negations"},
    # Single item in parens then word
    {"query": "(a) b", "expected": "(a) AND b", "id": "single_in_parens_then_word"},
    # Hyphenated words stay one token
    {"query": "some-word", "expected": "some-word", "id": "hyphenated_one"},
    {"query": "some-word other", "expected": "some-word AND other", "id": "hyphenated_and_word"},
    {"query": "a well-known card", "expected": "a AND well-known AND card", "id": "hyphenated_phrase"},
    # Multi-hyphen and card-like terms (from parser hyphenated-word tests)
    {"query": "old-growth-troll", "expected": "old-growth-troll", "id": "multi_hyphen_word"},
    {"query": "dual-land", "expected": "dual-land", "id": "dual_land_word"},
    {"query": "a-b-c", "expected": "a-b-c", "id": "multi_hyphen_a_b_c"},
    # Attribute value with hyphen (otag, is, oracle_tags, name)
    {"query": "name:Jace-the-mind", "expected": "name:Jace-the-mind", "id": "attr_value_hyphenated"},
    {"query": "name:test-word", "expected": "name:test-word", "id": "name_hyphenated_value"},
    {"query": "otag:dual-land", "expected": "otag:dual-land", "id": "otag_dual_land"},
    {"query": "otag:40k-model", "expected": "otag:40k-model", "id": "otag_40k_model"},
    {
        "query": "otag:cycle-shm-common-hybrid-1-drop",
        "expected": "otag:cycle-shm-common-hybrid-1-drop",
        "id": "otag_complex_hyphenated",
    },
    {"query": "oracle_tags:dual-land", "expected": "oracle_tags:dual-land", "id": "oracle_tags_dual_land"},
    {"query": "is:modal-dfc", "expected": "is:modal-dfc", "id": "is_modal_dfc"},
    # Hyphenated attr pairs with AND between
    {"query": "otag:dual-land cmc=3", "expected": "otag:dual-land AND cmc=3", "id": "otag_dual_land_and_cmc"},
    {"query": "otag:dual-land is:modal-dfc", "expected": "otag:dual-land AND is:modal-dfc", "id": "otag_dual_land_and_is_modal"},
    # Numerics
    {"query": "cmc:3.5", "expected": "cmc:3.5", "id": "numeric_float"},
    {"query": "1 2 3", "expected": "1 AND 2 AND 3", "id": "numeric_sequence"},
    # Mana symbols / curly (including complex symbols with slash)
    {"query": "c:{w}{u}", "expected": "c:{w}{u}", "id": "mana_curly"},
    {"query": "c:{W/U}", "expected": "c:{W/U}", "id": "mana_complex_slash"},
    {"query": "c:{1}{G} c:{2}{G}", "expected": "c:{1}{G} AND c:{2}{G}", "id": "mana_two_pairs"},
    # Mixed mana notation (digit + curly, letter + curly — no AND inside value)
    {"query": "m:2{R}{G}", "expected": "m:2{R}{G}", "id": "mana_mixed_2RG"},
    {"query": "mana=1{G}", "expected": "mana=1{G}", "id": "mana_eq_1G"},
    {"query": "mana=W{U/R}", "expected": "mana=W{U/R}", "id": "mana_eq_WUR"},
    # Date/year (numeric-looking values)
    {"query": "date:2025", "expected": "date:2025", "id": "date_value"},
    {"query": "year:2024", "expected": "year:2024", "id": "year_value"},
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
