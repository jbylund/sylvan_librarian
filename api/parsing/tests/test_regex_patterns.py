"""Tests for regex pattern search functionality."""

import pytest

from api import parsing
from api.parsing import (
    AndNode,
    AttributeNode,
    BinaryOperatorNode,
    RegexValueNode,
)
from api.parsing.parsing_f import generate_sql_query


class TestRegexPatternParsing:
    """Test regex pattern parsing with forward-slash delimiters."""

    @pytest.mark.parametrize(
        ("query", "expected_pattern"),
        [
            ("name:/izzet/", "izzet"),
            ("o:/^{T}:/", "^{T}:"),
            (r"name:/\bizzet\b/", r"\bizzet\b"),
            ("o:/exile|destroy/", "exile|destroy"),
            (r"o:/\spp/", r"\spp"),
            ("flavor:/.*flavor.*/", ".*flavor.*"),
            ("t:/creature|instant/", "creature|instant"),
        ],
    )
    def test_parse_regex_patterns(self, query: str, expected_pattern: str) -> None:
        """Test that regex patterns are parsed correctly."""
        result = parsing.parse_scryfall_query(query)
        assert isinstance(result.root, BinaryOperatorNode)
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == expected_pattern

    def test_parse_regex_with_escaped_forward_slash(self) -> None:
        """Test that escaped forward slashes are handled correctly in regex patterns."""
        # Test pattern with escaped forward slash: /a\/b/
        query = "name:/a\\/b/"
        result = parsing.parse_scryfall_query(query)
        assert isinstance(result.root, BinaryOperatorNode)
        assert isinstance(result.root.rhs, RegexValueNode)
        # The escaped forward slash should be preserved
        assert result.root.rhs.value == "a/b"

    def test_combined_regex_and_regular_search(self) -> None:
        """Test combining regex searches with regular text searches."""
        query = "t:creature o:/^{T}:/"
        result = parsing.parse_scryfall_query(query)
        assert isinstance(result.root, AndNode)
        assert len(result.root.operands) == 2

        # First operand should be type search (not regex)
        first = result.root.operands[0]
        assert isinstance(first, BinaryOperatorNode)
        assert isinstance(first.lhs, AttributeNode)

        # Second operand should be oracle text search with regex
        second = result.root.operands[1]
        assert isinstance(second, BinaryOperatorNode)
        assert isinstance(second.rhs, RegexValueNode)
        assert second.rhs.value == "^{T}:"


class TestRegexSQLGeneration:
    """Test SQL generation for regex patterns."""

    @pytest.mark.parametrize(
        ("query", "expected_operator", "expected_pattern"),
        [
            ("name:/izzet/", "~*", "izzet"),
            ("o:/^{T}:/", "~*", "^{T}:"),
            (r"name:/\bizzet\b/", "~*", r"\bizzet\b"),
            ("flavor:/.*flavor.*/", "~*", ".*flavor.*"),
        ],
    )
    def test_regex_sql_generation(self, query: str, expected_operator: str, expected_pattern: str) -> None:
        """Test that regex patterns generate correct PostgreSQL regex SQL."""
        result = parsing.parse_scryfall_query(query)
        sql, params = generate_sql_query(result)

        # Should use PostgreSQL case-insensitive regex operator
        assert expected_operator in sql
        # Should not use ILIKE operator
        assert "ILIKE" not in sql

        # Should have exactly one parameter containing the regex pattern
        assert len(params) == 1
        param_value = next(iter(params.values()))
        assert param_value == expected_pattern

    def test_regex_on_name_attribute(self) -> None:
        """Test regex on name attribute generates correct SQL."""
        result = parsing.parse_scryfall_query(r"name:/\bizzet\b/")
        sql, params = generate_sql_query(result)

        assert "card.card_name ~*" in sql
        assert len(params) == 1
        assert r"\bizzet\b" in params.values()

    def test_regex_on_oracle_attribute(self) -> None:
        """Test regex on oracle text attribute generates correct SQL."""
        result = parsing.parse_scryfall_query("o:/^{T}:/")
        sql, params = generate_sql_query(result)

        assert "card.oracle_text ~*" in sql
        assert len(params) == 1
        assert "^{T}:" in params.values()

    def test_regex_on_flavor_attribute(self) -> None:
        """Test regex on flavor text attribute generates correct SQL."""
        result = parsing.parse_scryfall_query("flavor:/magic/")
        sql, params = generate_sql_query(result)

        assert "card.flavor_text ~*" in sql
        assert len(params) == 1
        assert "magic" in params.values()

    def test_combined_regex_and_text_search_sql(self) -> None:
        """Test SQL generation for combined regex and text searches."""
        result = parsing.parse_scryfall_query("t:creature o:/^{T}:/")
        sql, params = generate_sql_query(result)

        # Should contain both the type search and regex oracle search
        assert "card.card_types" in sql
        assert "card.oracle_text ~*" in sql

        # Should have two parameters
        assert len(params) == 2

    def test_regular_text_search_uses_lower_like(self) -> None:
        """Test that regular text searches use lower() LIKE, not regex."""
        result = parsing.parse_scryfall_query("name:lightning")
        sql, params = generate_sql_query(result)

        # Should use lower() LIKE pattern matching, not regex
        assert "lower(" in sql
        assert "LIKE" in sql
        assert "ILIKE" not in sql
        assert "~*" not in sql

        # Should have wildcards in the parameter
        param_value = next(iter(params.values()))
        assert "%" in param_value


class TestRegexPatternFeatures:
    """Test specific regex features mentioned in Scryfall docs."""

    def test_anchors_start_and_end(self) -> None:
        """Test start (^) and end ($) anchors in regex patterns."""
        # Start anchor
        result = parsing.parse_scryfall_query("o:/^{T}:/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "^{T}:"

        # End anchor
        result = parsing.parse_scryfall_query("o:/draw a card$/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "draw a card$"

    def test_alternation_groups(self) -> None:
        """Test alternation groups (a|b) in regex patterns."""
        result = parsing.parse_scryfall_query("o:/exile|destroy/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "exile|destroy"

    def test_character_classes(self) -> None:
        r"""Test character classes like \d, \w, \s in regex patterns."""
        # Whitespace character class
        result = parsing.parse_scryfall_query(r"o:/\spp/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\spp"

        # Word character class
        result = parsing.parse_scryfall_query(r"o:/\w+/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\w+"

        # Digit character class
        result = parsing.parse_scryfall_query(r"o:/\d+/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\d+"

    def test_word_boundaries(self) -> None:
        r"""Test word boundary anchors (\b) in regex patterns."""
        result = parsing.parse_scryfall_query(r"name:/\bizzet\b/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == r"\bizzet\b"

    def test_brackets_and_quantifiers(self) -> None:
        """Test brackets [ab] and quantifiers .*?, +, * in regex patterns."""
        # Brackets
        result = parsing.parse_scryfall_query("o:/[Tt]ap/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "[Tt]ap"

        # Quantifiers
        result = parsing.parse_scryfall_query("o:/.*?draw.*?/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == ".*?draw.*?"

    def test_lookahead_assertions(self) -> None:
        """Test lookahead assertions (?!) in regex patterns."""
        result = parsing.parse_scryfall_query("o:/(?!non)/")
        assert isinstance(result.root.rhs, RegexValueNode)
        assert result.root.rhs.value == "(?!non)"


class TestRegexSupportedAttributes:
    """Test that regex patterns work with all supported attributes per Scryfall docs."""

    @pytest.mark.parametrize(
        ("query", "expected_attribute"),
        [
            ("name:/test/", "card_name"),
            ("oracle:/test/", "oracle_text"),
            ("o:/test/", "oracle_text"),
            ("flavor:/test/", "flavor_text"),
        ],
    )
    def test_regex_supported_on_text_attributes(self, query: str, expected_attribute: str) -> None:
        """Test that regex patterns are supported on all documented text attributes.

        Note: type: and t: attributes are JSONB arrays and regex support for them
        would require array element matching, which is not yet implemented.
        The alias ft: is not currently defined (only 'flavor' is).
        """
        result = parsing.parse_scryfall_query(query)
        sql, params = generate_sql_query(result)

        # Should contain the expected attribute in SQL
        assert expected_attribute in sql
        # Should use regex operator
        assert "~*" in sql
        # Should have exactly one parameter
        assert len(params) == 1
