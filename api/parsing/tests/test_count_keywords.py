"""Tests for Scryfall's count keywords: `prints`, `sets`, `paperprints`, `papersets`, `illustrations`, `artists`.

Measured on api.scryfall.com 2026-10-03: Lightning Bolt is `prints=77`, `sets=46`, `paperprints=68`,
`papersets=41` and `illustrations=33`; `sets=1` is `is:unique`; `artists=2` is 631 printings. Each
is an ordinary numeric column to the parser -- the counting happens at import.
"""

from __future__ import annotations

from functools import partial

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.db_info import ALIAS_TO_FIELD_INFOS, ParserClass
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)
BOTH_PARSERS = pytest.mark.parametrize("parse", [parse_scryfall_query, parse_with_pyparsing], ids=["hand", "pyparsing"])

# (keyword, the column _sync_print_counts writes it to)
KEYWORDS = [
    ("prints", "card_print_count"),
    ("sets", "card_set_count"),
    ("paperprints", "card_paper_print_count"),
    ("papersets", "card_paper_set_count"),
    ("illustrations", "card_illustration_count"),
    ("artists", "artist_count"),
]
OPERATORS = [(":", "="), ("=", "="), ("<", "<"), ("<=", "<="), (">", ">"), (">=", ">="), ("!=", "!=")]


class TestCountKeywords:
    """Each keyword is a numeric column like any other."""

    @BOTH_PARSERS
    @pytest.mark.parametrize(("keyword", "column"), KEYWORDS, ids=[k for k, _ in KEYWORDS])
    @pytest.mark.parametrize(("operator", "sql_operator"), OPERATORS, ids=[o for o, _ in OPERATORS])
    def test_every_comparator_compares_the_count_column(
        self, parse, keyword: str, column: str, operator: str, sql_operator: str
    ) -> None:
        sql, params = generate_sql_query(parse(f"{keyword}{operator}3"))

        (placeholder,) = params
        assert sql == f"(card.{column} {sql_operator} %({placeholder})s)"
        assert params[placeholder] == 3

    @BOTH_PARSERS
    @pytest.mark.parametrize(("keyword", "column"), KEYWORDS, ids=[k for k, _ in KEYWORDS])
    def test_keyword_is_case_insensitive_and_numeric_only(self, parse, keyword: str, column: str) -> None:
        sql, _ = generate_sql_query(parse(f"{keyword.upper()}>=2"))

        assert sql.startswith(f"(card.{column} >= ")
        assert {fi.parser_class for fi in ALIAS_TO_FIELD_INFOS[keyword]} == {ParserClass.NUMERIC}
        with pytest.raises(ValueError, match="Failed to parse query"):
            parse(f"{keyword}:abc")

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "expected_sql"),
        [
            # A column on either side: `prints>sets e:khm` is 119 on Scryfall, `prints=sets e:khm` 186,
            # `prints>paperprints e:khm` 98, `illustrations>=prints e:khm` 123, `artists>=cmc e:khm` 59.
            ("prints>sets", "(card.card_print_count > card.card_set_count)"),
            ("prints=sets", "(card.card_print_count = card.card_set_count)"),
            ("prints>paperprints", "(card.card_print_count > card.card_paper_print_count)"),
            ("sets>papersets", "(card.card_set_count > card.card_paper_set_count)"),
            ("illustrations>=prints", "(card.card_illustration_count >= card.card_print_count)"),
            ("artists>=cmc", "(card.artist_count >= card.cmc)"),
            ("cmc<prints", "(card.cmc < card.card_print_count)"),
            # Arithmetic, which this parser adds to Scryfall's syntax, works on them as on any number.
            ("prints-sets>=10", "((card.card_print_count - card.card_set_count) >= %(p_int_MTA)s)"),
        ],
    )
    def test_counts_compare_against_other_columns(self, parse, query: str, expected_sql: str) -> None:
        sql, _ = generate_sql_query(parse(query))

        assert sql == expected_sql

    @BOTH_PARSERS
    def test_negation_is_sql_negation(self, parse) -> None:
        """A card not yet counted is NULL and survives neither the comparison nor its negation."""
        sql, _ = generate_sql_query(parse("-prints>3"))

        assert sql.startswith("NOT ((card.card_print_count > ")

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "expected_columns"),
        [
            # The new plurals must not shadow, or be shadowed by, the keywords they extend.
            ("set:khm sets=1", ("card.card_set_code", "card.card_set_count")),
            ("s:khm sets=1", ("card.card_set_code", "card.card_set_count")),
            ("artist:rush artists=2", ("card.card_artist", "card.artist_count")),
            ("a:rush artists=2", ("card.card_artist", "card.artist_count")),
        ],
    )
    def test_plural_keywords_stay_distinct_from_set_and_artist(self, parse, query: str, expected_columns: tuple[str, str]) -> None:
        sql, _ = generate_sql_query(parse(query))

        first, second = sql.split(" AND ")
        assert expected_columns[0] in first
        assert expected_columns[1] in second

    @BOTH_PARSERS
    @pytest.mark.parametrize(("keyword", "column"), KEYWORDS, ids=[k for k, _ in KEYWORDS])
    def test_engine_json_names_the_column(self, parse, keyword: str, column: str) -> None:
        """The engine keys its NumField off `attribute_name`, so it must be the column."""
        kwargs = parse(f"{keyword}>=2").root.to_json()["kwargs"]

        assert kwargs["lhs"]["kwargs"] == {"attribute_name": column, "original_attribute": keyword}
        assert kwargs["op"] == ">="
        assert kwargs["rhs"] == {"node_type": "NumericValueNode", "kwargs": {"value": 2}}


if __name__ == "__main__":
    pytest.main([__file__])
