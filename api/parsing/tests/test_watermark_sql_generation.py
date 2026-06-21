"""Tests for SQL generation of watermark searches."""

from __future__ import annotations

import pytest

from api.parsing.nodes import Query, QueryContext


class TestWatermarkSQLGeneration:
    """Test that watermark searches generate exact equality SQL queries."""

    @pytest.mark.parametrize(
        ("query", "expected_column", "expected_value"),
        [
            ("watermark:azorius", "card.card_watermark", "azorius"),
            ("watermark:dimir", "card.card_watermark", "dimir"),
            ("watermark:rakdos", "card.card_watermark", "rakdos"),
            ("watermark:gruul", "card.card_watermark", "gruul"),
            ("watermark:selesnya", "card.card_watermark", "selesnya"),
            ("watermark:orzhov", "card.card_watermark", "orzhov"),
            ("watermark:izzet", "card.card_watermark", "izzet"),
            ("watermark:golgari", "card.card_watermark", "golgari"),
            ("watermark:boros", "card.card_watermark", "boros"),
            ("watermark:simic", "card.card_watermark", "simic"),
            ("watermark:set", "card.card_watermark", "set"),
            ("watermark:planeswalker", "card.card_watermark", "planeswalker"),
        ],
    )
    def test_watermark_generate_exact_equality_sql(
        self, parse_query, query: str, expected_column: str, expected_value: str
    ) -> None:
        """Test that watermark searches generate exact equality SQL (not ILIKE)."""
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

    def test_combined_watermark_query_sql(self, parse_query) -> None:
        """Test that combined watermark queries generate correct SQL."""
        result = parse_query("watermark:azorius watermark:dimir")
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should have both exact equality conditions with AND
        assert "card.card_watermark =" in sql
        assert "AND" in sql
        assert "ILIKE" not in sql

        # Context should have two exact values without wildcards
        assert len(context) == 2
        values = list(context.values())
        assert "azorius" in values
        assert "dimir" in values
        for value in values:
            assert "%" not in str(value)

    def test_watermark_with_other_attributes_sql(self, parse_query) -> None:
        """Test that watermark combined with other attributes generates correct SQL."""
        result = parse_query("watermark:azorius border:black")
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should have both exact equality conditions with AND
        assert "card.card_watermark =" in sql
        assert "card.card_border =" in sql
        assert "AND" in sql
        assert "ILIKE" not in sql

        # Context should have two exact values without wildcards
        assert len(context) == 2
        values = list(context.values())
        assert "azorius" in values
        assert "black" in values
        for value in values:
            assert "%" not in str(value)

    @pytest.mark.parametrize(
        ("query", "expected_lowercase"),
        [
            ("watermark:AZORIUS", "azorius"),
            ("watermark:Dimir", "dimir"),
            ("watermark:RAKDOS", "rakdos"),
            ("watermark:Gruul", "gruul"),
            ("watermark:SELESNYA", "selesnya"),
            ("watermark:Orzhov", "orzhov"),
            ("watermark:IZZET", "izzet"),
            ("watermark:Golgari", "golgari"),
            ("watermark:BOROS", "boros"),
            ("watermark:Simic", "simic"),
            ("watermark:SET", "set"),
            ("watermark:PLANESWALKER", "planeswalker"),
        ],
    )
    def test_case_insensitive_watermark_searches(self, parse_query, query: str, expected_lowercase: str) -> None:
        """Test that watermark searches are case-insensitive."""
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

    def test_complex_query_with_watermark_sql(self, parse_query) -> None:
        """Test that complex queries with watermark generate correct SQL."""
        result = parse_query("watermark:azorius border:black cmc=3")
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should have all three conditions with AND
        assert "card.card_watermark =" in sql
        assert "card.card_border =" in sql
        assert "card.cmc =" in sql
        assert "AND" in sql
        assert "ILIKE" not in sql

        # Context should have three exact values without wildcards
        assert len(context) == 3
        values = list(context.values())
        assert "azorius" in values
        assert "black" in values
        assert 3 in values
        for value in values:
            assert "%" not in str(value)


if __name__ == "__main__":
    pytest.main([__file__])
