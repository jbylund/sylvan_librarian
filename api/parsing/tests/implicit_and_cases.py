"""Shared test cases for implicit-AND behaviour, used by multiple test modules."""

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
    # Case-insensitive AND/OR keywords — normalised to uppercase
    {"query": "a and b", "expected": "a AND b", "id": "lowercase_and"},
    {"query": "a And b", "expected": "a AND b", "id": "mixed_case_and"},
    {"query": "a or b", "expected": "a OR b", "id": "lowercase_or"},
    {"query": "a Or b", "expected": "a OR b", "id": "mixed_case_or"},
    {"query": "a and b and c", "expected": "a AND b AND c", "id": "lowercase_and_chain"},
    {"query": "a or b or c", "expected": "a OR b OR c", "id": "lowercase_or_chain"},
    {"query": "a and b OR c", "expected": "a AND b OR c", "id": "lowercase_and_uppercase_or"},
    {"query": "name:foo and type:creature", "expected": "name:foo AND type:creature", "id": "lowercase_and_with_attrs"},
    {"query": "cmc>2 and power<5", "expected": "cmc>2 AND power<5", "id": "lowercase_and_comparisons"},
    {"query": "cmc>2 or power<5", "expected": "cmc>2 OR power<5", "id": "lowercase_or_comparisons"},
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
    # A mid-word apostrophe is part of the word, not an unterminated string
    {"query": "o:can't", "expected": "o:can't", "id": "apostrophe_in_value"},
    {"query": "can't stop", "expected": "can't AND stop", "id": "apostrophe_in_bare_word"},
    {"query": "o:can't t:elf", "expected": "o:can't AND t:elf", "id": "apostrophe_value_then_attr"},
    {"query": "name:Urza's -o:can't", "expected": "name:Urza's AND -o:can't", "id": "apostrophe_with_negation"},
    # Regex patterns (slash-delimited, single token)
    {"query": "name:/bolt/", "expected": "name:/bolt/", "id": "regex_single"},
    # A regex only opens in value position, so two of them means two conditions. The bare form
    # ("/foo/ /bar/") is not a supported shape — see the raises coverage in
    # test_pyparsing_preprocess.py and test_regex_patterns.py.
    {"query": "name:/foo/ o:/bar/", "expected": "name:/foo/ AND o:/bar/", "id": "two_regex"},
    {"query": "name:/bolt/ type:instant", "expected": "name:/bolt/ AND type:instant", "id": "regex_and_attr"},
    # A `/regex/` on a field that cannot run one: a plain literal is that literal, anything live is
    # rejected by both parsers.
    {"query": "t:/elf/ kw:/flying/", "expected": "t:/elf/ AND kw:/flying/", "id": "literal_regex_on_non_text_fields"},
    {"query": "t:/elf|goblin/", "expected": "t:/elf|goblin/", "id": "live_regex_on_type_rejected"},
    # Regex with escaped slash (searching for "/" in pattern, e.g. "life/death")
    {"query": r"name:/life\/death/", "expected": r"name:/life\/death/", "id": "regex_escaped_slash"},
    {"query": r"name:/a\/b/ type:/c\/d/", "expected": r"name:/a\/b/ AND type:/c\/d/", "id": "two_regex_escaped_slash"},
    # Comparison operators — no AND between attr op value
    {"query": "cmc=3", "expected": "cmc=3", "id": "cmp_eq"},
    {"query": "cmc<1r", "expected": "cmc<1r", "id": "cmp_lt_1r_no_space"},
    {"query": "cmc<1 r", "expected": "cmc<1 AND r", "id": "cmp_lt_1_r_with_space"},
    {"query": "cmc>2 power<5", "expected": "cmc>2 AND power<5", "id": "cmp_gt_lt"},
    {"query": "cmc>=3 cmc<=5", "expected": "cmc>=3 AND cmc<=5", "id": "cmp_gte_lte"},
    {"query": "color!=W", "expected": "color!=W", "id": "cmp_neq"},
    # Colour and rarity values are validated by the parser, quoted or bare
    {"query": 'c:"azorius" r:"rare"', "expected": 'c:"azorius" AND r:"rare"', "id": "quoted_color_and_rarity"},
    {"query": 'c:"xyz"', "expected": 'c:"xyz"', "id": "quoted_invalid_color_rejected"},
    {"query": "r:foo", "expected": "r:foo", "id": "invalid_rarity_rejected"},
    {"query": 'r:"foo"', "expected": 'r:"foo"', "id": "quoted_invalid_rarity_rejected"},
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
    # No comparison operator — subtraction is still binary, not negation
    {"query": "power - cmc", "expected": "power-cmc", "id": "arith_sub_attr_attr_no_cmp"},
    {"query": "cmc - power", "expected": "cmc-power", "id": "arith_sub_cmc_power_no_cmp"},
    {"query": "power - cmc>1", "expected": "power-cmc>1", "id": "arith_sub_attr_attr_space"},
    {"query": "power - cmc > 1", "expected": "power-cmc>1", "id": "arith_sub_attr_attr_spaces"},
    {"query": "toughness - power > 0", "expected": "toughness-power>0", "id": "arith_sub_toughness_power"},
    {"query": "cmc - 1 > 0", "expected": "cmc-1>0", "id": "arith_sub_attr_literal"},
    {"query": "5 - cmc > 0", "expected": "5-cmc>0", "id": "arith_sub_literal_attr"},
    # Case-insensitive numeric attribute names: uppercase/mixed-case must still be treated as
    # numeric operands so binary '-' is not mistaken for negation (exercises t.lower() fix)
    {"query": "CMC - Power > 1", "expected": "CMC-Power>1", "id": "arith_sub_uppercase_attrs"},
    {"query": "POWER - TOUGHNESS > 0", "expected": "POWER-TOUGHNESS>0", "id": "arith_sub_all_caps_attrs"},
    {"query": "Power > CMC - 1", "expected": "Power>CMC-1", "id": "cmp_rhs_arith_sub_mixed_case"},
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
    # Arithmetic subtraction on the RHS of a comparison: must NOT get AND (regression guard)
    {"query": "power>cmc-1", "expected": "power>cmc-1", "id": "cmp_rhs_arith_sub"},
    # Comparison LHS, minus, then a paren group whose comparison operator is nested one level
    # deep (#903 cause A): the group is a filter to negate, not an arithmetic term, even though
    # the ':'/'=' is invisible to a scan that only looks at depth 0.
    {"query": "cmc>1 -(t:elf)", "expected": "cmc>1 AND -(t:elf)", "id": "cmp_then_group_with_nested_comparison"},
    {
        "query": "pow=3 -(name:force or type:elf)",
        "expected": "pow=3 AND -(name:force OR type:elf)",
        "id": "cmp_then_group_with_nested_comparison_or",
    },
    {
        "query": "year:2019 -(oracle:exile or type:enchantment)",
        "expected": "year:2019 AND -(oracle:exile OR type:enchantment)",
        "id": "cmp_then_group_with_nested_comparison_year_lhs",
    },
    {"query": "power > cmc - 1", "expected": "power>cmc-1", "id": "cmp_rhs_arith_sub_spaces"},
    {"query": "toughness>=power-cmc", "expected": "toughness>=power-cmc", "id": "cmp_rhs_arith_sub_attrs"},
    {"query": "toughness >= power - cmc", "expected": "toughness>=power-cmc", "id": "cmp_rhs_arith_sub_attrs_spaces"},
    # Comparison with negation on right-hand side: power > -cmc+5 must NOT get AND
    {"query": "power>-cmc+5", "expected": "power>-cmc+5", "id": "cmp_rhs_negation"},
    {"query": "power > -cmc + 5", "expected": "power>-cmc+5", "id": "cmp_rhs_negation_spaces"},
    # Comparison then an expression starting with '-': must insert AND (not treat as arithmetic)
    {"query": "Power>2 -1+CMC<2", "expected": "Power>2 AND -1+CMC<2", "id": "cmp_then_arith_leading_minus"},
    {"query": "power>2 -cmc>0", "expected": "power>2 AND -cmc>0", "id": "cmp_then_neg_numeric_attr"},
    {"query": "power>2 -toughness>0", "expected": "power>2 AND -toughness>0", "id": "cmp_then_neg_toughness"},
    {"query": "power>2 -(cmc-1)>0", "expected": "power>2 AND -(cmc-1)>0", "id": "cmp_then_neg_paren"},
    {"query": "power>2 cmc<3", "expected": "power>2 AND cmc<3", "id": "two_comparisons_and"},
    # Negative literal as a comparison value (#891): the '-' is a sign, so no AND is inserted
    {"query": "power>-1", "expected": "power>-1", "id": "cmp_negative_literal"},
    {"query": "power > -1", "expected": "power>-1", "id": "cmp_negative_literal_spaces"},
    {"query": "power>-1.5", "expected": "power>-1.5", "id": "cmp_negative_float_literal"},
    {"query": "t:creature power>-1", "expected": "t:creature AND power>-1", "id": "attr_then_cmp_negative_literal"},
    {"query": "power>-1 t:creature", "expected": "power>-1 AND t:creature", "id": "cmp_negative_literal_then_attr"},
    {"query": "power>-1+2", "expected": "power>-1+2", "id": "cmp_negative_literal_arith_tail"},
    # Arithmetic within comparison (not after comparison RHS): must not insert AND
    {"query": "power*2 - 1 > 0", "expected": "power*2-1>0", "id": "arith_mul_sub_lit"},
    # Multi-term arithmetic chains (compact / no spaces): tokenizer absorbs as single tokens
    {
        "query": "power-cmc-1-toughness>loyalty-cmc-1",
        "expected": "power-cmc-1-toughness>loyalty-cmc-1",
        "id": "deep_arith_chain_compact",
    },
    # Two consecutive arithmetic comparisons separated by space: AND between them
    {"query": "power-cmc>1 toughness-loyalty>0", "expected": "power-cmc>1 AND toughness-loyalty>0", "id": "two_arith_comparisons"},
    {"query": "power-1>3 cmc-1<2", "expected": "power-1>3 AND cmc-1<2", "id": "two_arith_cmp_sub_lit"},
    # a-b-c-d-e>f-g-h-i -j-k+l>1+2+3 style (with numeric names, space before - signals AND)
    {
        "query": "power-cmc-1-toughness>loyalty-cmc-1 -power+toughness>1+2+3",
        "expected": "power-cmc-1-toughness>loyalty-cmc-1 AND -power+toughness>1+2+3",
        "id": "deep_arith_chain_then_arith_expr",
    },
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
    # A negated condition whose value spells an alias of the same class is still attr:value
    {"query": "-c:c", "expected": "-c:c", "id": "negated_color_value_is_alias"},
    {"query": "-id:c", "expected": "-id:c", "id": "negated_identity_value_is_alias"},
    {"query": "t:elf -c:c", "expected": "t:elf AND -c:c", "id": "attr_then_negated_color_alias_value"},
    {"query": "-r:r", "expected": "-r:r", "id": "negated_rarity_value_is_alias"},
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
    # A hyphenated word whose first half is a numeric alias is still a word, not half an arithmetic
    # expression; with a numeric term after the '-' it is arithmetic as before.
    {"query": "pow-wow", "expected": "pow-wow", "id": "hyphenated_numeric_alias_prefix"},
    {"query": "power-plant t:land", "expected": "power-plant AND t:land", "id": "hyphenated_numeric_alias_then_attr"},
    {"query": "mv-x", "expected": "mv-x", "id": "hyphenated_numeric_alias_single_letter"},
    {"query": "pow-tou>0", "expected": "pow-tou>0", "id": "numeric_alias_subtraction_comparison"},
    # Attribute value with hyphen (otag, is, oracle_tags, name)
    {"query": "name:Jace-the-mind", "expected": "name:Jace-the-mind", "id": "attr_value_hyphenated"},
    {"query": "name:test-word", "expected": "name:test-word", "id": "name_hyphenated_value"},
    {"query": "otag:dual-land", "expected": "otag:dual-land", "id": "otag_dual_land"},
    {"query": "otag:40k-model", "expected": "otag:40k-model", "id": "otag_40k_model"},
    # A number in text position keeps its spelling: leading zeros and trailing decimals are text
    {"query": "set:001", "expected": "set:001", "id": "text_value_leading_zeros"},
    {"query": "x-007", "expected": "x-007", "id": "hyphenated_name_leading_zeros"},
    {"query": "o:1.50", "expected": "o:1.50", "id": "text_value_trailing_decimal_zero"},
    {"query": "name:007 t:elf", "expected": "name:007 AND t:elf", "id": "text_value_leading_zeros_then_attr"},
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
    # A bare numeric expression with no comparison is a name search for its text (as on Scryfall),
    # never a non-boolean root; inside a comparison it stays arithmetic.
    {"query": "1996", "expected": "1996", "id": "bare_number_is_name"},
    {"query": "2.5 t:elf", "expected": "2.5 AND t:elf", "id": "bare_float_then_attr"},
    {"query": "cmc+1", "expected": "cmc+1", "id": "bare_arith_is_name"},
    {"query": "cmc+1<power", "expected": "cmc+1<power", "id": "arith_lhs_comparison"},
    {"query": "(2*power)", "expected": "(2*power)", "id": "bare_group_arith_is_name"},
    {"query": "(cmc+1)*2>3", "expected": "(cmc+1)*2>3", "id": "group_arith_operand"},
    {"query": "-1", "expected": "-1", "id": "negated_bare_number"},
    {"query": "t:elf -1", "expected": "t:elf AND -1", "id": "attr_then_negated_bare_number"},
    {"query": "cmc>2 -1", "expected": "cmc>2 AND -1", "id": "cmp_then_negated_bare_number"},
    {"query": "-(2*power)", "expected": "-(2*power)", "id": "negated_bare_group_arith"},
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
    # A comparison against any four-digit year is meaningful; only `=`/`:` get the Magic-era gate.
    {"query": "year<1991", "expected": "year<1991", "id": "year_before_magic_comparison"},
    {"query": "date<1993", "expected": "date<1993", "id": "date_before_magic_comparison"},
    {"query": "year>=2100", "expected": "year>=2100", "id": "year_far_future_comparison"},
    {"query": "year:1500", "expected": "year:1500", "id": "year_before_magic_equality_rejected"},
    {"query": "date=2099-01-01", "expected": "date=2099-01-01", "id": "date_far_future_equality_rejected"},
    {"query": "year>99999", "expected": "year>99999", "id": "year_five_digits_rejected"},
    # A partial or impossible date is an error, not a silent search for the year alone.
    {"query": "date:2020-01", "expected": "date:2020-01", "id": "date_month_without_day_rejected"},
    {"query": "date:2020-02-30", "expected": "date:2020-02-30", "id": "date_impossible_rejected"},
    # Dots in attribute values (e.g. sentence-ending period in oracle text search)
    {"query": "o:token.", "expected": "o:token.", "id": "oracle_value_trailing_dot"},
    {"query": "o:token. -o:counter", "expected": "o:token. AND -o:counter", "id": "oracle_value_trailing_dot_with_negation"},
    # Whitespace normalization (multiple spaces between tokens)
    {"query": "a   b", "expected": "a AND b", "id": "multiple_spaces"},
    {"query": "  a  b  ", "expected": "a AND b", "id": "leading_trailing_space"},
    # Empty and single-space (edge cases)
    {"query": "", "expected": "", "id": "empty"},
    {"query": "   ", "expected": "", "id": "only_spaces"},
    # Bare (unquoted) accented words — #649
    {"query": "Éowyn", "expected": "Éowyn", "id": "bare_accented_word"},
    {"query": "name:Éowyn", "expected": "name:Éowyn", "id": "attr_bare_accented_value"},
    {"query": "name:Éowyn type:creature", "expected": "name:Éowyn AND type:creature", "id": "bare_accented_value_with_attr"},
    {"query": "Éowyn Dúnedain", "expected": "Éowyn AND Dúnedain", "id": "two_bare_accented_words"},
]
