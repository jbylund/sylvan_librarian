"""Tests for language (lang:) parsing functionality."""

from __future__ import annotations

from typing import Any

import pytest

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, NotNode, OrNode, Query, QueryContext


class TestLangParsing:
    """Test parsing of language search queries."""

    @pytest.mark.parametrize(
        ("query", "expected_attr", "expected_value"),
        [
            ("lang:en", "card_lang", "en"),
            ("lang:ja", "card_lang", "ja"),
            ("lang:ru", "card_lang", "ru"),
            ("lang:zhs", "card_lang", "zhs"),
            ("lang:pt", "card_lang", "pt"),
            ("language:ja", "card_lang", "ja"),
            ("language:de", "card_lang", "de"),
            # Scryfall widens to every language with the special lang:any value;
            # the parser passes it through as an ordinary string value.
            ("lang:any", "card_lang", "any"),
            # Unknown codes are not the parser's business: they parse fine and
            # simply match nothing, the same way set:zzz does.
            ("lang:zzz", "card_lang", "zzz"),
        ],
    )
    def test_parse_lang_queries(self, parse_query, query: str, expected_attr: str, expected_value: str) -> None:
        """Test parsing of language search queries."""
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.attribute_name == expected_attr
        assert binary_op.operator == ":"
        assert binary_op.rhs.value == expected_value

    def test_parse_lang_case_insensitive(self, parse_query) -> None:
        """Test that language searches are case-insensitive."""
        query = "lang:JA"
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "card_lang"
        # The value is preserved during parsing, but will be lowercased during SQL generation
        assert binary_op.rhs.value == "JA"

    def test_parse_lang_with_quotes(self, parse_query) -> None:
        """Test parsing language searches with quoted values."""
        query = 'lang:"ja"'
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "card_lang"
        assert binary_op.rhs.value == "ja"

    def test_parse_negated_lang(self, parse_query) -> None:
        """Test parsing negated language searches."""
        query = "-lang:en"
        result = parse_query(query)

        assert isinstance(result, Query)
        not_node = result.root
        assert isinstance(not_node, NotNode)
        binary_op = not_node.operand
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "card_lang"
        assert binary_op.operator == ":"
        assert binary_op.rhs.value == "en"

    @pytest.mark.parametrize("operator", [">", "<", ">=", "<=", "!="])
    def test_parse_lang_ordered_comparison(self, parse_query, operator: str) -> None:
        """Test ordered comparisons against the language column.

        They parse the same way other string-equality columns (set/layout/border/
        watermark) parse them: the operator is preserved in the tree rather than
        rejected at parse time.
        """
        result = parse_query(f"lang{operator}en")

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "card_lang"
        assert binary_op.operator == operator
        assert binary_op.rhs.value == "en"

    def test_parse_lang_with_other_attributes(self, parse_query) -> None:
        """Test parsing language searches combined with other attributes."""
        query = "lang:ja t:creature"
        result = parse_query(query)

        assert isinstance(result, Query)
        # Should be an AND operation
        and_node = result.root
        assert isinstance(and_node, AndNode)

        # Extract all conditions
        def extract_attributes(node: Any) -> list[tuple[str, Any]]:
            """Recursively extract all attribute nodes from a parse tree."""
            if isinstance(node, CardBinaryOperatorNode) and hasattr(node.lhs, "attribute_name"):
                return [(node.lhs.attribute_name, node.rhs.value)]
            if isinstance(node, AndNode):
                attrs = []
                for child in node.operands:
                    attrs.extend(extract_attributes(child))
                return attrs
            return []

        attributes = extract_attributes(result.root)
        expected_attrs = [("card_lang", "ja"), ("card_types", "creature")]

        # Sort both lists to compare regardless of order
        attributes.sort()
        expected_attrs.sort()
        assert attributes == expected_attrs

    def test_parse_lang_in_boolean_combination(self, parse_query) -> None:
        """Test parsing language searches inside boolean combinations."""
        query = "(lang:ja or lang:ru) cmc=1"
        result = parse_query(query)

        assert isinstance(result, Query)
        and_node = result.root
        assert isinstance(and_node, AndNode)

        or_nodes = [op for op in and_node.operands if isinstance(op, OrNode)]
        assert len(or_nodes) == 1
        (or_node,) = or_nodes

        lang_values = set()
        for cond in or_node.operands:
            assert isinstance(cond, CardBinaryOperatorNode)
            assert cond.lhs.attribute_name == "card_lang"
            assert cond.operator == ":"
            lang_values.add(cond.rhs.value)
        assert lang_values == {"ja", "ru"}

        cmc_nodes = [op for op in and_node.operands if isinstance(op, CardBinaryOperatorNode)]
        assert len(cmc_nodes) == 1
        (cmc_node,) = cmc_nodes
        assert cmc_node.lhs.attribute_name == "cmc"
        assert cmc_node.rhs.value == 1


class TestLangSQLGeneration:
    """Test that language searches generate exact equality SQL queries."""

    @pytest.mark.parametrize(
        ("query", "expected_value"),
        [
            ("lang:ja", "ja"),
            ("language:ru", "ru"),
            ("lang:any", "any"),
            ("lang:zzz", "zzz"),
            # lang codes are lowercased at import, so the search value is
            # lowercased for a case-insensitive plain equality.
            ("lang:JA", "ja"),
            ("LANG:EN", "en"),
        ],
    )
    def test_lang_generates_exact_equality_sql(self, parse_query, query: str, expected_value: str) -> None:
        """Test that language searches generate exact equality SQL (not LIKE)."""
        result = parse_query(query)
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should generate exact equality with = operator, not pattern matching
        assert "card.card_lang =" in sql
        assert "LIKE" not in sql

        # Context should contain the exact value without wildcards
        assert len(context) == 1
        param_value = next(iter(context.values()))
        assert param_value == expected_value
        assert "%" not in param_value  # No wildcards


if __name__ == "__main__":
    pytest.main([__file__])
