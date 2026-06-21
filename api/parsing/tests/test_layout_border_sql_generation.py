"""Tests for SQL generation of layout and border searches."""

from __future__ import annotations

import pytest

from api.parsing.nodes import Query, QueryContext


class TestLayoutBorderSQLGeneration:
    """Test that layout and border searches generate exact equality SQL queries."""

    @pytest.mark.parametrize(
        ("query", "expected_column", "expected_value"),
        [
            ("layout:normal", "card.card_layout", "normal"),
            ("layout:split", "card.card_layout", "split"),
            ("layout:flip", "card.card_layout", "flip"),
            ("border:black", "card.card_border", "black"),
            ("border:white", "card.card_border", "white"),
            ("border:borderless", "card.card_border", "borderless"),
        ],
    )
    def test_layout_border_generate_exact_equality_sql(
        self, parse_query, query: str, expected_column: str, expected_value: str
    ) -> None:
        """Test that layout and border searches generate exact equality SQL (not ILIKE)."""
        result = parse_query(query)
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should generate exact equality with = operator, not ILIKE
        assert "=" in sql
        assert "ILIKE" not in sql
        assert expected_column in sql

        # Context should contain the exact value without wildcards
        assert len(context) == 1
        param_value = next(iter(context.values()))
        assert param_value == expected_value
        assert "%" not in param_value  # No wildcards

    def test_name_search_uses_lower_like_pattern_matching(self, parse_query) -> None:
        """Test that regular text fields like name use lower() LIKE pattern matching."""
        result = parse_query("name:lightning")
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should generate lower() LIKE pattern matching
        assert "lower(" in sql
        assert "LIKE" in sql
        assert "ILIKE" not in sql
        assert "card.card_name" in sql

        # Context should contain wildcards
        assert len(context) == 1
        param_value = next(iter(context.values()))
        assert param_value == "%lightning%"
        assert param_value.startswith("%")
        assert param_value.endswith("%")

    def test_combined_layout_border_query_sql(self, parse_query) -> None:
        """Test that combined layout and border queries generate correct SQL."""
        result = parse_query("layout:normal border:black")
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should have both exact equality conditions with AND
        assert "card.card_layout =" in sql
        assert "card.card_border =" in sql
        assert "AND" in sql
        assert "ILIKE" not in sql

        # Context should have two exact values without wildcards
        assert len(context) == 2
        values = list(context.values())
        assert "normal" in values
        assert "black" in values
        for value in values:
            assert "%" not in str(value)

    @pytest.mark.parametrize(
        ("query", "expected_lowercase"),
        [
            ("layout:NORMAL", "normal"),
            ("layout:Split", "split"),
            ("layout:TRANSFORM", "transform"),
            ("border:BLACK", "black"),
            ("border:White", "white"),
            ("border:BORDERLESS", "borderless"),
        ],
    )
    def test_case_insensitive_layout_border_searches(self, parse_query, query: str, expected_lowercase: str) -> None:
        """Test that layout and border searches are case-insensitive."""
        result = parse_query(query)
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should generate exact equality with = operator
        assert "=" in sql
        assert "ILIKE" not in sql

        # Context should contain the lowercase value
        assert len(context) == 1
        param_value = next(iter(context.values()))
        assert param_value == expected_lowercase
        assert "%" not in param_value


if __name__ == "__main__":
    pytest.main([__file__])
