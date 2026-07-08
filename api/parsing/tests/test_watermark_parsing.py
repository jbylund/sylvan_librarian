"""Tests for watermark parsing functionality."""

from __future__ import annotations

from typing import Any

import pytest

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, Query


class TestWatermarkParsing:
    """Test parsing of watermark search queries."""

    @pytest.mark.parametrize(
        ("query", "expected_attr", "expected_value"),
        [
            ("watermark:azorius", "card_watermark", "azorius"),
            ("wm:orzhov", "card_watermark", "orzhov"),
            ("watermark:dimir", "card_watermark", "dimir"),
            ("watermark:rakdos", "card_watermark", "rakdos"),
            ("watermark:gruul", "card_watermark", "gruul"),
            ("watermark:selesnya", "card_watermark", "selesnya"),
        ],
    )
    def test_parse_watermark_queries(self, parse_query, query: str, expected_attr: str, expected_value: str) -> None:
        """Test parsing of watermark search queries."""
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.attribute_name == expected_attr
        assert binary_op.operator == ":"
        assert binary_op.rhs.value == expected_value

    def test_parse_watermark_case_insensitive(self, parse_query) -> None:
        """Test that watermark searches are case-insensitive."""
        query = "watermark:AZORIUS"
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "card_watermark"
        # The value is preserved during parsing, but will be lowercased during SQL generation
        assert binary_op.rhs.value == "AZORIUS"

    def test_parse_watermark_with_quotes(self, parse_query) -> None:
        """Test parsing watermark searches with quoted values."""
        query = 'watermark:"azorius"'
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "card_watermark"
        assert binary_op.rhs.value == "azorius"

    def test_parse_combined_watermark_query(self, parse_query) -> None:
        """Test parsing combined watermark queries."""
        query = "watermark:azorius watermark:dimir"
        result = parse_query(query)

        assert isinstance(result, Query)
        # Should be an AND operation between two binary operator nodes
        and_node = result.root
        assert isinstance(and_node, AndNode)

        # Extract the two binary operator nodes from the AND
        conditions = and_node.operands
        assert len(conditions) == 2

        # Check that we have both watermark conditions
        attributes = {cond.lhs.attribute_name for cond in conditions}
        assert attributes == {"card_watermark"}

        values = {cond.rhs.value for cond in conditions}
        assert values == {"azorius", "dimir"}

    def test_parse_watermark_with_other_attributes(self, parse_query) -> None:
        """Test parsing watermark searches combined with other attributes."""
        query = "watermark:azorius border:black"
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
        expected_attrs = [("card_watermark", "azorius"), ("card_border", "black")]

        # Sort both lists to compare regardless of order
        attributes.sort()
        expected_attrs.sort()
        assert attributes == expected_attrs

    def test_parse_complex_query_with_watermark(self, parse_query) -> None:
        """Test parsing complex queries that include watermark."""
        query = "watermark:azorius border:black cmc=3"
        result = parse_query(query)

        assert isinstance(result, Query)

        # Should be nested AND operations
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
        expected_attrs = [("card_watermark", "azorius"), ("card_border", "black"), ("cmc", 3)]

        # Sort both lists to compare regardless of order
        attributes.sort()
        expected_attrs.sort()
        assert attributes == expected_attrs


if __name__ == "__main__":
    pytest.main([__file__])
