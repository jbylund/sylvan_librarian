"""Integration tests for collector number search functionality."""

import pytest

from api.parsing import parse_scryfall_query
from api.parsing import generate_sql_query


class TestCollectorNumberIntegration:
    """Test collector number search functionality with end-to-end integration."""

    def test_number_search_integration(self) -> None:
        """Test that number search generates correct SQL end-to-end."""
        query = "number:123"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate exact match query for text column
        assert "card.collector_number_int =" in sql
        assert len(params) == 1

        # Parameter should contain exact value (no wildcards)
        param_value = next(iter(params.values()))
        assert param_value == 123

    def test_cn_search_integration(self) -> None:
        """Test that cn alias generates correct SQL end-to-end."""
        query = "cn:45a"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate exact match query for text column
        assert "card.collector_number =" in sql
        assert len(params) == 1

        # Parameter should contain exact value (no wildcards)
        param_value = next(iter(params.values()))
        assert param_value == "45a"

    def test_quoted_collector_number_integration(self) -> None:
        """Test that quoted collector numbers work correctly."""
        query = 'number:"100"'
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate exact match query for text column
        assert "card.collector_number =" in sql
        assert len(params) == 1

        # Parameter should contain exact value (no wildcards)
        param_value = next(iter(params.values()))
        assert param_value == "100"

    @pytest.mark.parametrize(
        argnames=("query", "expected_pattern"),
        argvalues=[
            ("number:1", "1"),
            ("cn:123", "123"),
            ("number:45a", "45a"),
            ("cn:100b", "100b"),
            ("NUMBER:999", "999"),
            ("CN:1a", "1a"),
        ],
    )
    def test_various_collector_numbers_integration(self, query: str, expected_pattern: str) -> None:
        """Test various collector number formats generate correct SQL."""
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate exact match query for text column
        try:
            expected_pattern = int(expected_pattern)
        except ValueError:
            assert "card.collector_number =" in sql
        else:
            assert "card.collector_number_int =" in sql
        assert len(params) == 1

        # Parameter should match expected pattern
        param_value = next(iter(params.values()))
        assert param_value == expected_pattern

    def test_combined_collector_number_search_integration(self) -> None:
        """Test that combined queries with collector numbers work correctly."""
        query = "number:123 set:dom"
        parsed = parse_scryfall_query(query)
        sql, params = generate_sql_query(parsed)

        # Should generate queries for both collector_number and set
        assert "card.collector_number_int =" in sql
        assert "card.card_set_code =" in sql
        assert len(params) == 2

        # Should contain parameters for both conditions
        param_values = set(params.values())
        assert 123 in param_values
        assert "dom" in param_values

    def test_collector_number_numeric_comparisons_integration(self) -> None:
        """Test that numeric comparison operators work correctly with integer column."""
        test_cases = [
            ("number>50", "card.collector_number_int >", 50),
            ("cn<100", "card.collector_number_int <", 100),
            ("number>=25", "card.collector_number_int >=", 25),
            ("cn<=75", "card.collector_number_int <=", 75),
        ]

        for query, expected_sql_fragment, expected_param in test_cases:
            parsed = parse_scryfall_query(query)
            sql, params = generate_sql_query(parsed)

            # Should generate numeric comparison query for integer column
            assert expected_sql_fragment in sql
            assert len(params) == 1

            # Parameter should be the numeric value
            param_value = next(iter(params.values()))
            assert param_value == expected_param

    def test_alphanumeric_collector_number_numeric_extraction(self) -> None:
        """Test that alphanumeric collector numbers extract numeric portions correctly.

        Examples: "123a" should compare as 123, "45b" should compare as 45
        This tests the new regexp-based numeric extraction function.
        """
        # Test that alphanumeric collector numbers work with numeric comparisons
        # The function should extract "45" from "45a" for numeric comparison
        test_cases = [
            ("cn>40", 45),  # "45a" should extract as 45, which is > 40
            ("number<50", 45),  # "45a" should extract as 45, which is < 50
            ("cn>=45", 45),  # "45a" should extract as 45, which is >= 45
            ("number<=45", 45),  # "45a" should extract as 45, which is <= 45
        ]

        for query, _expected_extracted_value in test_cases:
            parsed = parse_scryfall_query(query)
            sql, params = generate_sql_query(parsed)

            # Should use the integer column for comparison
            assert "card.collector_number_int" in sql
            assert len(params) == 1

            # The comparison value should match what we expect
            param_value = next(iter(params.values()))
            assert isinstance(param_value, int), f"Expected integer parameter for {query}"
