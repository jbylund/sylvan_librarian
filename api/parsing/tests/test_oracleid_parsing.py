"""Tests for oracle id (oracleid:) parsing functionality."""

from __future__ import annotations

from typing import Any

import pytest

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, NotNode, OrNode, Query, QueryContext

ORACLE_ID = "43fbfeec-bcaf-48b8-befe-b7346fec5a3a"


class TestOracleIdParsing:
    """Test parsing of oracle id search queries."""

    @pytest.mark.parametrize(
        ("query", "expected_value"),
        [
            (f"oracleid:{ORACLE_ID}", ORACLE_ID),
            (f"oracle_id:{ORACLE_ID}", ORACLE_ID),
            # A well-formed id that names no card parses fine and simply matches
            # nothing, the same way set:zzz does.
            ("oracleid:deadbeef-dead-4bee-8dad-decafbadf00d", "deadbeef-dead-4bee-8dad-decafbadf00d"),
            # Validating the uuid is not the parser's business either: a malformed
            # value is an ordinary string that no stored id equals.
            ("oracleid:not-a-uuid", "not-a-uuid"),
        ],
    )
    def test_parse_oracleid_queries(self, parse_query, query: str, expected_value: str) -> None:
        """Test parsing of oracle id search queries."""
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.attribute_name == "oracle_id"
        assert binary_op.operator == ":"
        assert binary_op.rhs.value == expected_value

    def test_parse_oracleid_case_insensitive(self, parse_query) -> None:
        """Test that oracle id searches are case-insensitive."""
        query = f"oracleid:{ORACLE_ID.upper()}"
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "oracle_id"
        # The value is preserved during parsing, but will be lowercased during SQL generation
        assert binary_op.rhs.value == ORACLE_ID.upper()

    def test_parse_oracleid_with_quotes(self, parse_query) -> None:
        """Test parsing oracle id searches with quoted values."""
        query = f'oracleid:"{ORACLE_ID}"'
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "oracle_id"
        assert binary_op.rhs.value == ORACLE_ID

    def test_parse_negated_oracleid(self, parse_query) -> None:
        """Test parsing negated oracle id searches."""
        query = f"-oracleid:{ORACLE_ID}"
        result = parse_query(query)

        assert isinstance(result, Query)
        not_node = result.root
        assert isinstance(not_node, NotNode)
        binary_op = not_node.operand
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "oracle_id"
        assert binary_op.operator == ":"
        assert binary_op.rhs.value == ORACLE_ID

    @pytest.mark.parametrize("operator", [">", "<", ">=", "<=", "!="])
    def test_parse_oracleid_ordered_comparison(self, parse_query, operator: str) -> None:
        """Test ordered comparisons against the oracle id column.

        They parse the same way other string-equality columns (set/lang/layout/
        border/watermark) parse them: the operator is preserved in the tree
        rather than rejected at parse time.
        """
        result = parse_query(f"oracleid{operator}{ORACLE_ID}")

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert binary_op.lhs.attribute_name == "oracle_id"
        assert binary_op.operator == operator
        assert binary_op.rhs.value == ORACLE_ID

    def test_parse_oracleid_with_other_attributes(self, parse_query) -> None:
        """Test parsing oracle id searches combined with other attributes."""
        query = f"oracleid:{ORACLE_ID} t:creature"
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
        expected_attrs = [("oracle_id", ORACLE_ID), ("card_types", "creature")]

        # Sort both lists to compare regardless of order
        attributes.sort()
        expected_attrs.sort()
        assert attributes == expected_attrs

    def test_parse_oracleid_in_boolean_combination(self, parse_query) -> None:
        """Test parsing oracle id searches inside boolean combinations."""
        other_id = "21f45043-5419-4019-8b6c-e5294bd5f549"
        query = f"(oracleid:{ORACLE_ID} or oracleid:{other_id}) cmc=1"
        result = parse_query(query)

        assert isinstance(result, Query)
        and_node = result.root
        assert isinstance(and_node, AndNode)

        or_nodes = [op for op in and_node.operands if isinstance(op, OrNode)]
        assert len(or_nodes) == 1
        (or_node,) = or_nodes

        oracle_ids = set()
        for cond in or_node.operands:
            assert isinstance(cond, CardBinaryOperatorNode)
            assert cond.lhs.attribute_name == "oracle_id"
            assert cond.operator == ":"
            oracle_ids.add(cond.rhs.value)
        assert oracle_ids == {ORACLE_ID, other_id}

        cmc_nodes = [op for op in and_node.operands if isinstance(op, CardBinaryOperatorNode)]
        assert len(cmc_nodes) == 1
        (cmc_node,) = cmc_nodes
        assert cmc_node.lhs.attribute_name == "cmc"
        assert cmc_node.rhs.value == 1


class TestOracleIdSQLGeneration:
    """Test that oracle id searches generate exact equality SQL queries."""

    @pytest.mark.parametrize(
        ("query", "expected_value"),
        [
            (f"oracleid:{ORACLE_ID}", ORACLE_ID),
            (f"oracle_id:{ORACLE_ID}", ORACLE_ID),
            # A uuid renders lowercase through ::text, so the search value is
            # lowercased for a case-insensitive plain equality.
            (f"oracleid:{ORACLE_ID.upper()}", ORACLE_ID),
            ("oracleid:not-a-uuid", "not-a-uuid"),
        ],
    )
    def test_oracleid_generates_exact_equality_sql(self, parse_query, query: str, expected_value: str) -> None:
        """Test that oracle id searches generate exact equality SQL (not LIKE)."""
        result = parse_query(query)
        assert isinstance(result, Query)

        context: QueryContext = QueryContext()
        sql = result.to_sql(context)

        # Should generate exact equality with = operator, not pattern matching
        assert "card.oracle_id::text =" in sql
        assert "LIKE" not in sql

        # Context should contain the exact value without wildcards
        assert len(context) == 1
        param_value = next(iter(context.values()))
        assert param_value == expected_value
        assert "%" not in param_value  # No wildcards

    def test_oracleid_compares_the_column_as_text(self, parse_query) -> None:
        """The UUID column is cast, so a text-typed bound parameter has an operator.

        Postgres has no `uuid = text`, and every parameter this parser binds is
        text; comparing `oracle_id::text` also means an unparseable search value
        matches nothing instead of raising.
        """
        result = parse_query("oracleid:not-a-uuid")
        assert isinstance(result, Query)

        sql = result.to_sql(QueryContext())
        assert "card.oracle_id::text" in sql


if __name__ == "__main__":
    pytest.main([__file__])
