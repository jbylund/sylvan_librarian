"""Tests for exact name search using the ! prefix syntax."""

import pytest

from api.parsing.card_query_nodes import ExactNameNode
from api.parsing.nodes import AndNode, NotNode, QueryContext


@pytest.mark.parametrize(
    argnames=("query", "expected_value"),
    argvalues=[
        ('!"Lightning Bolt"', "Lightning Bolt"),
        ("!bolt", "bolt"),
        ("!Counterspell", "Counterspell"),
        ('!"Black Lotus"', "Black Lotus"),
        ('!"Serra Angel"', "Serra Angel"),
        ("!sol", "sol"),
        ('!"stormchaser\'s talent"', "stormchaser's talent"),
        ('!"dragon\'s breath"', "dragon's breath"),
    ],
)
def test_exact_name_parses_to_exact_name_node(parse_query, query: str, expected_value: str) -> None:
    """Test that ! prefix queries parse into ExactNameNode with the correct value."""
    result = parse_query(query)
    assert isinstance(result.root, ExactNameNode), f"Expected ExactNameNode, got {type(result.root)}"
    assert result.root.value == expected_value, f"Expected {expected_value!r}, got {result.root.value!r}"


@pytest.mark.parametrize(
    argnames=("query", "expected_value"),
    argvalues=[
        ('-!"Lightning Bolt"', "Lightning Bolt"),
        ("-!bolt", "bolt"),
    ],
)
def test_negated_exact_name_parses_to_not_exact_name_node(parse_query, query: str, expected_value: str) -> None:
    """Test that -! prefix queries parse into NotNode(ExactNameNode(...))."""
    result = parse_query(query)
    assert isinstance(result.root, NotNode), f"Expected NotNode, got {type(result.root)}"
    assert isinstance(result.root.operand, ExactNameNode), f"Expected ExactNameNode operand, got {type(result.root.operand)}"
    assert result.root.operand.value == expected_value


@pytest.mark.parametrize(
    argnames="query",
    argvalues=[
        '!"Lightning Bolt" cmc=1',
        'cmc=1 !"Lightning Bolt"',
        '!"Black Lotus" color:b',
    ],
)
def test_exact_name_combined_with_other_conditions(parse_query, query: str) -> None:
    """Test that exact name queries combine correctly with other conditions via AND."""
    result = parse_query(query)
    assert isinstance(result.root, AndNode), f"Expected AndNode, got {type(result.root)}"
    assert len(result.root.operands) == 2
    # One of the operands should be an ExactNameNode
    exact_name_nodes = [op for op in result.root.operands if isinstance(op, ExactNameNode)]
    assert len(exact_name_nodes) == 1, "Expected exactly one ExactNameNode in AND"


@pytest.mark.parametrize(
    argnames=("query", "expected_sql", "expected_parameters"),
    argvalues=[
        (
            '!"Lightning Bolt"',
            "(lower(card.card_name) LIKE %(p_str_bGlnaHRuaW5nIGJvbHQ)s)",
            {"p_str_bGlnaHRuaW5nIGJvbHQ": "lightning bolt"},
        ),
        (
            "!bolt",
            "(lower(card.card_name) LIKE %(p_str_Ym9sdA)s)",
            {"p_str_Ym9sdA": "bolt"},
        ),
        (
            '-!"Lightning Bolt"',
            "NOT ((lower(card.card_name) LIKE %(p_str_bGlnaHRuaW5nIGJvbHQ)s))",
            {"p_str_bGlnaHRuaW5nIGJvbHQ": "lightning bolt"},
        ),
    ],
)
def test_exact_name_sql_generation(parse_query, query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that exact name queries generate the correct SQL."""
    parsed = parse_query(query)
    context: QueryContext = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"SQL mismatch: {observed_sql!r}"
    assert context == expected_parameters, f"Params mismatch: {context!r}"


def test_exact_name_node_equality() -> None:
    """Test that ExactNameNode equality comparison works correctly."""
    node1 = ExactNameNode("Lightning Bolt")
    node2 = ExactNameNode("Lightning Bolt")
    node3 = ExactNameNode("Counterspell")
    assert node1 == node2
    assert node1 != node3
    assert hash(node1) == hash(node2)
    assert hash(node1) != hash(node3)


def test_exact_name_node_repr() -> None:
    """Test that ExactNameNode has a useful repr."""
    node = ExactNameNode("Lightning Bolt")
    assert repr(node) == "ExactNameNode('Lightning Bolt')"
