"""Tests for standalone quoted string as implicit name search."""

import pytest

from api.parsing import parse_scryfall_query
from api.parsing.card_query_nodes import CardAttributeNode, ExactNameNode
from api.parsing.db_info import ParserClass
from api.parsing.nodes import AndNode, BinaryOperatorNode, NotNode, StringValueNode


@pytest.mark.parametrize(
    argnames=("query", "expected_value"),
    argvalues=[
        ('"lightning bolt"', "lightning bolt"),
        ("'lightning bolt'", "lightning bolt"),
        ('"stormchaser\'s talent"', "stormchaser's talent"),
        ('"dragon\'s breath"', "dragon's breath"),
        ('"Black Lotus"', "Black Lotus"),
        ('"Serra Angel"', "Serra Angel"),
    ],
)
def test_standalone_quoted_string_parses_to_name_search(query: str, expected_value: str) -> None:
    """Test that a standalone quoted string is treated as an implicit name search."""
    result = parse_scryfall_query(query)
    node = result.root
    assert isinstance(node, BinaryOperatorNode), f"Expected BinaryOperatorNode, got {type(node)}"
    assert isinstance(node.lhs, CardAttributeNode)
    assert node.lhs.attribute_name == "card_name"
    assert node.lhs.matched_parser_class == ParserClass.TEXT
    assert node.operator == ":"
    assert isinstance(node.rhs, StringValueNode)
    assert node.rhs.value == expected_value


@pytest.mark.parametrize(
    argnames=("query", "expected_value"),
    argvalues=[
        ('-"lightning bolt"', "lightning bolt"),
        ("-'lightning bolt'", "lightning bolt"),
        ('-"stormchaser\'s talent"', "stormchaser's talent"),
    ],
)
def test_negated_quoted_name_search(query: str, expected_value: str) -> None:
    """Test that a negated standalone quoted string parses to NotNode wrapping a name search."""
    result = parse_scryfall_query(query)
    node = result.root
    assert isinstance(node, NotNode), f"Expected NotNode, got {type(node)}"
    inner = node.operand
    assert isinstance(inner, BinaryOperatorNode)
    assert inner.rhs.value == expected_value


@pytest.mark.parametrize(
    argnames="query",
    argvalues=[
        '"lightning bolt" cmc=1',
        'cmc=1 "lightning bolt"',
        '"Black Lotus" color:b',
    ],
)
def test_quoted_name_combined_with_other_conditions(query: str) -> None:
    """Test that standalone quoted string searches combine correctly with other conditions via AND."""
    result = parse_scryfall_query(query)
    assert isinstance(result.root, AndNode), f"Expected AndNode, got {type(result.root)}"
    assert len(result.root.operands) == 2


def test_quoted_name_does_not_interfere_with_exact_name() -> None:
    """Test that exact name search (!) still produces ExactNameNode, not BinaryOperatorNode."""
    result = parse_scryfall_query('!"stormchaser\'s talent"')
    assert isinstance(result.root, ExactNameNode), f"Expected ExactNameNode, got {type(result.root)}"
    assert result.root.value == "stormchaser's talent"
