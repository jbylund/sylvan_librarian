"""Tests for post-rewrite regex static limits."""

from __future__ import annotations

import pytest

from api.parsing import parse_scryfall_query
from api.parsing.query_budget import QUERY_REGEX_REJECTED_MESSAGE, InvalidRegexPatternError, QueryBudgetExceeded
from api.parsing.regex_budget import MAX_PATTERN_UTF8_BYTES, MAX_REGEX_LEAVES_PER_QUERY


class TestRegexBudgetAcceptsLegitPatterns:
    @pytest.mark.parametrize(
        "query",
        [
            "o:/draw .* cards?/",
            "t:creature o:/^{T}:/",
            "name:/\\bizzet\\b/",
            "o:/(?<!non)artifact/",
            "o:/(destroy|exile) target creature/",
            "o:/\\broll(ed)?\\b.*\\b(d\\d+|die|dice)\\b/",
        ],
    )
    def test_accepts_documented_patterns(self, query: str) -> None:
        parse_scryfall_query(query)


class TestRegexLeafLimit:
    def test_accepts_ten_regex_leaves(self) -> None:
        query = " ".join("o:/(?=draw)/" for _ in range(MAX_REGEX_LEAVES_PER_QUERY))
        parse_scryfall_query(query)

    def test_rejects_eleven_regex_leaves(self) -> None:
        query = " ".join("o:/(?=draw)/" for _ in range(MAX_REGEX_LEAVES_PER_QUERY + 1))
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query(query)
        assert exc_info.value.kind == "regex_leaves"
        assert exc_info.value.user_message == QUERY_REGEX_REJECTED_MESSAGE


class TestRegexPatternLimits:
    def test_rejects_oversized_pattern(self) -> None:
        query = f"o:/.{'a' * MAX_PATTERN_UTF8_BYTES}/"
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query(query)
        assert exc_info.value.kind == "regex_pattern"

    def test_rejects_stacked_numeric_quantifiers(self) -> None:
        with pytest.raises(InvalidRegexPatternError):
            parse_scryfall_query("o:/a{10}{10}{10}{10}{10}/")

    def test_rejects_wide_alternation(self) -> None:
        alts = "|".join(f"w{i}" for i in range(500))
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query(f"o:/{alts}/")
        assert exc_info.value.kind == "regex_pattern"

    def test_plain_literal_lowering_skips_regex_budget(self) -> None:
        long_literal = "a" * (MAX_PATTERN_UTF8_BYTES + 1)
        parse_scryfall_query(f"o:/{long_literal}/")

    def test_rejects_backreferences(self) -> None:
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query("o:/(a)\\1/")
        assert exc_info.value.kind == "regex_pattern"

    def test_accepts_grouped_numeric_repeat(self) -> None:
        parse_scryfall_query("o:/(?:a{10}){10}/")

    def test_rejects_nested_numeric_repeat_product(self) -> None:
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query("o:/(?:a{50}){50}/")
        assert exc_info.value.kind == "regex_pattern"

    def test_rejects_conditional_groups(self) -> None:
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query("o:/(a)(?(1)b)/")
        assert exc_info.value.kind == "regex_pattern"

    def test_accepts_escaped_lookaround_literal(self) -> None:
        parse_scryfall_query(r"o:/\(\?=not a lookaround/")


class TestUnboundedRepeatShapes:
    """The bounded-product score cannot see `*`/`+`; these are the unbounded shapes that cost seconds.

    An unbounded repeat inside a lookaround restarts a scan at every position; a repeat nested
    inside another repeat backtracks exponentially. Both were unscored. `{m,}` at depth 1 is
    `x{m}x*` and is now judged on m like any bounded quantifier, instead of being rejected outright.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            "(?=.*)(?=.*)x",
            "(?=.*draw)(?=.*discard)",
            "(?!a+)b",
            "(?<=a{2,})b",
            "(?=(?:ab)*)c",
            "(a+)+",
            "(a*)*",
            "(a|b+)*",
            "(?:a+){3}",
            "(a{2,}){3}",
            "((a+)b)+",
        ],
        ids=[
            "two_lookaheads_dot_star",
            "and_of_substrings_idiom",
            "negative_lookahead_plus",
            "lookbehind_open_range",
            "lookahead_group_star",
            "plus_in_plus",
            "star_in_star",
            "star_over_alternation_with_plus",
            "bounded_over_plus",
            "bounded_over_open_range",
            "plus_two_levels_down",
        ],
    )
    def test_rejects_unbounded_repeat_in_lookaround_or_nested(self, pattern: str) -> None:
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query(f"o:/{pattern}/")
        assert exc_info.value.kind == "regex_pattern"

    @pytest.mark.parametrize(
        "pattern",
        [
            "(?=foo)bar",
            "(?<!non)artifact",
            "(?=a{1,3})b",
            "a+b+",
            "a*b*c?",
            r"\d{3,}",
            "(?:ab){2,}",
            "(?:a{10}){10}",
            "(ab|cd)+",
            "x{1,}",
        ],
        ids=[
            "literal_lookahead",
            "literal_lookbehind",
            "bounded_in_lookahead",
            "sibling_plus",
            "sibling_star_optional",
            "open_range_depth_one",
            "open_range_over_bounded_body",
            "bounded_product",
            "plus_over_alternation",
            "one_or_more_spelled_as_range",
        ],
    )
    def test_accepts_depth_one_unbounded_and_bounded_lookarounds(self, pattern: str) -> None:
        parse_scryfall_query(f"o:/{pattern}/")

    def test_open_range_bound_is_the_public_bound(self) -> None:
        parse_scryfall_query("o:/a{1024,}/")
        with pytest.raises(QueryBudgetExceeded):
            parse_scryfall_query("o:/a{1025,}/")


class TestRegexOperatorCoverage:
    @pytest.mark.parametrize(
        "query",
        [
            "o=/draw/",
            "o!=/draw/",
            "name=/^bolt$/",
        ],
    )
    def test_equality_operators_count_regex_leaves(self, query: str) -> None:
        parse_scryfall_query(query)

    def test_equality_operator_leaf_limit(self) -> None:
        query = " ".join("o=/draw/" for _ in range(MAX_REGEX_LEAVES_PER_QUERY + 1))
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query(query)
        assert exc_info.value.kind == "regex_leaves"

    def test_equality_operator_pattern_limit(self) -> None:
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query(f"o!=/.{'a' * MAX_PATTERN_UTF8_BYTES}/")
        assert exc_info.value.kind == "regex_pattern"
