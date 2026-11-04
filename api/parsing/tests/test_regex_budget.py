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
        query = " ".join("o:/(?=.*draw)/" for _ in range(MAX_REGEX_LEAVES_PER_QUERY))
        parse_scryfall_query(query)

    def test_rejects_eleven_regex_leaves(self) -> None:
        query = " ".join("o:/(?=.*draw)/" for _ in range(MAX_REGEX_LEAVES_PER_QUERY + 1))
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


class TestAreWordBoundaryEscapes:
    """`o:/.../` is PostgreSQL ARE, whose word boundaries Python's `re` cannot parse.

    The budget measures patterns with `re._parser`, so without the same rewrite the
    engine applies (`card_engine/src/regex_compat.rs`) every one of these would be
    rejected at parse time as a malformed pattern -- a documented operator turned
    into a user-visible error by its own security check.
    """

    @pytest.mark.parametrize(
        "query",
        [
            r"o:/\yizzet\y/",
            r"o:/\Ynot a boundary/",
            r"name:/\mizzet\M/",
            r"o:/lifelink\Z/",
        ],
    )
    def test_accepts_are_escapes(self, query: str) -> None:
        parse_scryfall_query(query)

    @pytest.mark.parametrize(
        "query",
        [
            r"o:/[\d]\yizzet\y/",
            # `]` in the first position is a literal member (POSIX), so it does not
            # close the class -- the `\y` after the real `]` is the one to rewrite.
            r"o:/[]a]\yx/",
            r"o:/[^]a]\mx/",
        ],
    )
    def test_rewrite_reads_bracket_expressions(self, query: str) -> None:
        # Inside `[...]` an escape is an ordinary member, not a constraint, so the
        # rewrite must skip the class and resume after it. Verified against the
        # engine's copy: both spell these the same way.
        parse_scryfall_query(query)

    def test_a_word_boundary_escape_inside_a_class_stays_malformed(self) -> None:
        # `[\y]` is not a bracket expression either engine accepts -- the rewrite
        # passes it through rather than inventing a meaning, and it fails here
        # exactly as `CompiledRegex::new` fails on it.
        with pytest.raises(InvalidRegexPatternError):
            parse_scryfall_query(r"o:/[\y]lit/")

    def test_word_start_escapes_spend_lookaround_budget(self) -> None:
        # `\m`/`\M` have no linear spelling: they become lookaround on the engine,
        # which is exactly what the lookaround cap is there to bound. Three of them
        # is six lookarounds, past MAX_LOOKAROUNDS_PER_PATTERN.
        with pytest.raises(QueryBudgetExceeded) as exc_info:
            parse_scryfall_query(r"o:/\ma\mb\mc/")
        assert exc_info.value.kind == "regex_pattern"

    def test_still_rejects_a_genuinely_bad_escape(self) -> None:
        with pytest.raises(InvalidRegexPatternError):
            parse_scryfall_query(r"o:/\q/")


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
