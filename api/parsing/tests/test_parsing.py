"""Tests for query parsing functionality."""

import pytest

from api import parsing
from api.parsing import (
    AndNode,
    AttributeNode,
    BinaryOperatorNode,
    NotNode,
    NumericValueNode,
    OrNode,
    QueryNode,
    StringValueNode,
)
from api.parsing.card_query_nodes import CardAttributeNode, calculate_cmc, mana_cost_str_to_dict
from api.parsing.db_info import ParserClass


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=[
        ("cmc=3", BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "=", NumericValueNode(3))),
        (
            "cmc=3 power=3",
            AndNode(
                [
                    BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "=", NumericValueNode(3)),
                    BinaryOperatorNode(CardAttributeNode("power", ParserClass.NUMERIC), "=", NumericValueNode(3)),
                ],
            ),
        ),
        (
            "name:'power'",
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("power")),
        ),
        (
            'name:"power"',
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("power")),
        ),
        (
            "cmc+cmc<power+toughness",
            BinaryOperatorNode(
                BinaryOperatorNode(
                    CardAttributeNode("cmc", ParserClass.NUMERIC),
                    "+",
                    CardAttributeNode("cmc", ParserClass.NUMERIC),
                ),
                "<",
                BinaryOperatorNode(
                    CardAttributeNode("power", ParserClass.NUMERIC),
                    "+",
                    CardAttributeNode("toughness", ParserClass.NUMERIC),
                ),
            ),
        ),
        (
            "cmc+1<power",
            BinaryOperatorNode(
                BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "+", NumericValueNode(1)),
                "<",
                CardAttributeNode("power", ParserClass.NUMERIC),
            ),
        ),
        (
            "cmc<power+1",
            BinaryOperatorNode(
                CardAttributeNode("cmc", ParserClass.NUMERIC),
                "<",
                BinaryOperatorNode(CardAttributeNode("power", ParserClass.NUMERIC), "+", NumericValueNode(1)),
            ),
        ),
        (
            "cmc+1<power+2",
            BinaryOperatorNode(
                BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "+", NumericValueNode(1)),
                "<",
                BinaryOperatorNode(CardAttributeNode("power", ParserClass.NUMERIC), "+", NumericValueNode(2)),
            ),
        ),
        (
            "cmc+power",
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "+", CardAttributeNode("power", ParserClass.NUMERIC)),
        ),
        (
            "cmc-power",
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "-", CardAttributeNode("power", ParserClass.NUMERIC)),
        ),
        (
            "cmc + 1 < power",
            BinaryOperatorNode(
                BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "+", NumericValueNode(1)),
                "<",
                CardAttributeNode("power", ParserClass.NUMERIC),
            ),
        ),
        # Test cases for the numeric < attribute bug
        ("0<power", BinaryOperatorNode(NumericValueNode(0), "<", CardAttributeNode("power", ParserClass.NUMERIC))),
        ("1<power", BinaryOperatorNode(NumericValueNode(1), "<", CardAttributeNode("power", ParserClass.NUMERIC))),
        ("3>cmc", BinaryOperatorNode(NumericValueNode(3), ">", CardAttributeNode("cmc", ParserClass.NUMERIC))),
        (
            "0<=toughness",
            BinaryOperatorNode(NumericValueNode(0), "<=", CardAttributeNode("toughness", ParserClass.NUMERIC)),
        ),
        # Test cases for pricing attributes
        ("usd>10", BinaryOperatorNode(CardAttributeNode("usd", ParserClass.NUMERIC), ">", NumericValueNode(10))),
        ("eur<=5", BinaryOperatorNode(CardAttributeNode("eur", ParserClass.NUMERIC), "<=", NumericValueNode(5))),
        ("tix<1", BinaryOperatorNode(CardAttributeNode("tix", ParserClass.NUMERIC), "<", NumericValueNode(1))),
        ("usd=2.5", BinaryOperatorNode(CardAttributeNode("usd", ParserClass.NUMERIC), "=", NumericValueNode(2.5))),
        ("eur!=10", BinaryOperatorNode(CardAttributeNode("eur", ParserClass.NUMERIC), "!=", NumericValueNode(10))),
        ("tix>=0.5", BinaryOperatorNode(CardAttributeNode("tix", ParserClass.NUMERIC), ">=", NumericValueNode(0.5))),
        # Test cases for loyalty attributes
        ("loyalty=3", BinaryOperatorNode(CardAttributeNode("loyalty", ParserClass.NUMERIC), "=", NumericValueNode(3))),
        ("loyalty>5", BinaryOperatorNode(CardAttributeNode("loyalty", ParserClass.NUMERIC), ">", NumericValueNode(5))),
        (
            "loyalty<=7",
            BinaryOperatorNode(CardAttributeNode("loyalty", ParserClass.NUMERIC), "<=", NumericValueNode(7)),
        ),
        ("loy:4", BinaryOperatorNode(CardAttributeNode("loy", ParserClass.NUMERIC), ":", NumericValueNode(4))),
    ],
)
def test_parse_to_nodes(test_input: str, expected_ast: QueryNode) -> None:
    """Test that queries parse into the expected AST structure."""
    observed = parsing.parse_search_query(test_input).root

    # Compare the full AST structure
    assert observed == expected_ast, f"\nExpected: {expected_ast}\nObserved: {observed}"


def test_parse_simple_condition() -> None:
    """Test parsing a simple condition."""
    query = "cmc:2"
    result = parsing.parse_search_query(query)
    expected = BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), ":", NumericValueNode(2))
    assert result.root == expected


def test_parse_and_operation() -> None:
    """Test parsing AND operations."""
    query = "a AND b"
    result = parsing.parse_search_query(query).root

    assert result == AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("a")),
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("b")),
        ],
    )


def test_parse_or_operation() -> None:
    """Test parsing OR operations."""
    query = "a OR b"
    result = parsing.parse_search_query(query).root
    assert result == OrNode(
        [
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("a")),
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("b")),
        ],
    )


def test_parse_implicit_and() -> None:
    """Test parsing implicit AND operations."""
    query = "a b"
    result = parsing.parse_search_query(query).root
    assert result == AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("a")),
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("b")),
        ],
    )


def test_parse_complex_nested() -> None:
    """Test parsing complex nested queries."""
    query = "cmc:2 AND (oracle:flying OR oracle:haste)"
    result = parsing.parse_search_query(query)

    assert isinstance(result, parsing.Query)
    assert isinstance(result.root, AndNode)
    assert len(result.root.operands) == 2
    # The right side should be an OR operation
    assert isinstance(result.root.operands[1], OrNode)

    # Test with flavor text as well
    query2 = "cmc:3 AND (flavor:magic OR oracle:flying)"
    result2 = parsing.parse_search_query(query2)
    assert isinstance(result2, parsing.Query)
    assert isinstance(result2.root, AndNode)
    assert len(result2.root.operands) == 2


def test_parse_quoted_strings() -> None:
    """Test parsing quoted strings."""
    query = 'name:"Lightning Bolt"'
    observed_ast = parsing.parse_search_query(query)
    expected_ast = BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("Lightning Bolt"))
    assert observed_ast.root == expected_ast


def test_parse_set_searches() -> None:
    """Test parsing set search queries."""
    # Test full 'set:' syntax
    query = "set:iko"
    result = parsing.parse_search_query(query)
    expected = BinaryOperatorNode(CardAttributeNode("set", ParserClass.TEXT), ":", StringValueNode("iko"))
    assert result.root == expected

    # Test 's:' shorthand
    query = "s:thb"
    result = parsing.parse_search_query(query)
    expected = BinaryOperatorNode(CardAttributeNode("s", ParserClass.TEXT), ":", StringValueNode("thb"))
    assert result.root == expected

    # Test case insensitivity
    query = "SET:m21"
    result = parsing.parse_search_query(query)
    expected = BinaryOperatorNode(CardAttributeNode("SET", ParserClass.TEXT), ":", StringValueNode("m21"))
    assert result.root == expected


def test_parse_different_operators() -> None:
    """Test parsing different comparison operators."""
    operators = [">", "<", ">=", "<=", "=", "!="]

    for op in operators:
        query = f"cmc{op}3"
        result = parsing.parse_search_query(query)
        expected = BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), op, NumericValueNode(3))
        assert result.root == expected


def _generate_pricing_operator_test_cases() -> list[tuple[str, BinaryOperatorNode]]:
    """Generate test cases for pricing operators."""
    operators = [">", "<", ">=", "<=", "=", "!="]
    pricing_attrs = ["usd", "eur", "tix"]

    test_cases = []
    for attr in pricing_attrs:
        for op in operators:
            query = f"{attr}{op}5"
            expected = BinaryOperatorNode(CardAttributeNode(attr, ParserClass.NUMERIC), op, NumericValueNode(5))
            test_cases.append((query, expected))

    return test_cases


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=_generate_pricing_operator_test_cases(),
)
def test_parse_pricing_operators(test_input: str, expected_ast: BinaryOperatorNode) -> None:
    """Test parsing different comparison operators with pricing attributes."""
    result = parsing.parse_search_query(test_input)
    assert result.root == expected_ast, f"Failed for {test_input}"


def test_parse_combined_pricing_queries() -> None:
    """Test parsing combined queries with pricing attributes."""
    # Test combining pricing with other attributes
    query1 = "cmc<=3 usd<5"
    result1 = parsing.parse_search_query(query1)
    expected1 = AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "<=", NumericValueNode(3)),
            BinaryOperatorNode(CardAttributeNode("usd", ParserClass.NUMERIC), "<", NumericValueNode(5)),
        ],
    )
    assert result1.root == expected1

    # Test combining multiple pricing attributes
    query2 = "usd>10 OR eur<5"
    result2 = parsing.parse_search_query(query2)
    expected2 = OrNode(
        [
            BinaryOperatorNode(CardAttributeNode("usd", ParserClass.NUMERIC), ">", NumericValueNode(10)),
            BinaryOperatorNode(CardAttributeNode("eur", ParserClass.NUMERIC), "<", NumericValueNode(5)),
        ],
    )
    assert result2.root == expected2


@pytest.mark.parametrize(
    argnames="query",
    argvalues=["", "   ", None],
)
def test_parse_empty_query(query: str | None) -> None:
    """Test that empty/whitespace/None queries produce a TrueNode root."""
    result = parsing.parse_search_query(query)
    assert isinstance(result, parsing.Query)
    assert isinstance(result.root, parsing.TrueNode)


def test_name_vs_name_attribute() -> None:
    """Test that we can distinguish between the string 'name' and card names."""
    # This should create a BinaryOperatorNode for "name" (searching for cards with "name" in their name)
    query1 = "name"
    result1 = parsing.parse_search_query(query1)
    expected = BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("name"))
    assert result1.root == expected

    # This should create a BinaryOperatorNode for name:value (same as bare word "value")
    query2 = "name:value"
    result2 = parsing.parse_search_query(query2)
    expected = BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("value"))
    assert result2.root == expected

    # This should create a BinaryOperatorNode for cmc operations
    query3 = "cmc:3"
    result3 = parsing.parse_search_query(query3)
    expected = BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), ":", NumericValueNode(3))
    assert result3.root == expected

    # This should create a BinaryOperatorNode for other attributes
    query4 = "oracle:flying"
    result4 = parsing.parse_search_query(query4)
    expected = BinaryOperatorNode(CardAttributeNode("oracle", ParserClass.TEXT), ":", StringValueNode("flying"))
    assert result4.root == expected

    # Test flavor text search
    query5 = "flavor:magic"
    result5 = parsing.parse_search_query(query5)
    expected = BinaryOperatorNode(CardAttributeNode("flavor", ParserClass.TEXT), ":", StringValueNode("magic"))
    assert result5.root == expected


@pytest.mark.parametrize(
    argnames="operator",
    argvalues=["AND", "OR"],
)
def test_nary_operator_associativity(operator: str) -> None:
    """Test that AND operator associativity now creates the same AST structure."""
    # These should now create the same AST structure with n-ary operations
    query1 = f"a {operator} (b {operator} c)"
    query2 = f"(a {operator} b) {operator} c"

    result1 = parsing.parse_search_query(query1)
    result2 = parsing.parse_search_query(query2)

    # With n-ary operations, both should now create the same AST structure
    # Both should be: AndNode([a, b, c])
    assert result1 == result2


class TestNodes:
    def test_node_equality(self) -> None:
        assert AttributeNode("name") == AttributeNode("name")


def test_arithmetic_vs_negation_ambiguity() -> None:
    """Test that the ambiguity between arithmetic and negation is resolved correctly."""
    # These should be treated as arithmetic operations (both sides are known attributes)
    arithmetic_cases = [
        (
            "cmc-power",
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "-", CardAttributeNode("power", ParserClass.NUMERIC)),
        ),
        (
            "power-toughness",
            BinaryOperatorNode(
                CardAttributeNode("power", ParserClass.NUMERIC),
                "-",
                CardAttributeNode("toughness", ParserClass.NUMERIC),
            ),
        ),
        (
            "cmc+power",
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "+", CardAttributeNode("power", ParserClass.NUMERIC)),
        ),
    ]

    for query, expected in arithmetic_cases:
        result = parsing.parse_search_query(query)
        assert result.root == expected, f"Failed for query: {query}"

    # These should be treated as negation (one side is not a known attribute)
    negation_cases = [
        (
            "cmc -flying",
            AndNode(
                [
                    BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("cmc")),
                    NotNode(BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("flying"))),
                ],
            ),
        ),
        (
            "power -goblin",
            AndNode(
                [
                    BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("power")),
                    NotNode(BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("goblin"))),
                ],
            ),
        ),
        # Regression: value `r` (rarity alias) and `o` (oracle alias) are both in KNOWN_CARD_ATTRIBUTES,
        # but `r` here is a value (preceded by `=`), so `-` should be negation not arithmetic.
        (
            "id=r -o:enchantment",
            AndNode(
                [
                    BinaryOperatorNode(CardAttributeNode("id", ParserClass.COLOR), "=", StringValueNode("r")),
                    NotNode(BinaryOperatorNode(CardAttributeNode("o", ParserClass.TEXT), ":", StringValueNode("enchantment"))),
                ],
            ),
        ),
    ]

    for query, expected in negation_cases:
        result = parsing.parse_search_query(query)
        assert result.root == expected, f"Failed for query: {query}"


def generate_arithmetic_parser_testcases() -> list[tuple[str, BinaryOperatorNode]]:
    """Generate all 25 combinations using cross product for parametrized testing.

    Returns a list of (query, expected_ast) tuples covering all combinations of
    5 expression types: literal, literal arithmetic, mixed arithmetic,
    attribute arithmetic, and attribute.
    """
    expression_types = [
        ("1", NumericValueNode(1)),
        ("1+1", BinaryOperatorNode(NumericValueNode(1), "+", NumericValueNode(1))),
        ("cmc+1", BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "+", NumericValueNode(1))),
        (
            "cmc+power",
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "+", CardAttributeNode("power", ParserClass.NUMERIC)),
        ),
        ("power", CardAttributeNode("power", ParserClass.NUMERIC)),
    ]

    query_ast_pairs = []
    for lhs_query, lhs_ast in expression_types:
        for rhs_query, rhs_ast in expression_types:
            query = f"{lhs_query}<{rhs_query}"
            expected_ast = BinaryOperatorNode(lhs_ast, "<", rhs_ast)
            query_ast_pairs.append((query, expected_ast))

    return query_ast_pairs


@pytest.mark.parametrize(argnames=("query", "expected_ast"), argvalues=generate_arithmetic_parser_testcases())
def test_arithmetic_parser_consolidation(query: str, expected_ast: BinaryOperatorNode) -> None:
    """Test that the fully consolidated arithmetic parser rules handle all cases correctly.

    This parametrized test runs each of the 25 combinations as separate test cases,
    making it easier to identify specific failures and providing better test reporting.

    Tests all combinations of 5 expression types:
    1. literal number
    2. arithmetic with just literal numbers
    3. arithmetic with numbers and numeric attributes
    4. arithmetic with numeric attributes
    5. only numeric attribute

    This verifies that after removing redundant rules and consolidating into a single
    unified_numeric_comparison rule, all arithmetic parsing still works correctly.
    """
    observed = parsing.parse_search_query(query).root
    assert observed == expected_ast, f"Query '{query}' failed\nExpected: {expected_ast}\nObserved: {observed}"


@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=[
        "name:test and",  # Trailing AND with no right operand
        "power>1 or",  # Trailing OR with no right operand
        "cmc=3 and ()",  # Empty parentheses after AND
    ],
)
def test_invalid_queries_with_trailing_content_fail(invalid_query: str) -> None:
    """Test that queries with invalid trailing content properly fail to parse.

    This addresses issue #86 where invalid trailing content was being silently ignored.

    Note: Since issue #90, standalone numeric literals like "1" are now valid parse targets,
    so queries like "name:bolt and 1" now parse successfully (though they fail at DB level
    with datatype mismatch errors).
    """
    with pytest.raises(ValueError, match="Failed to parse query"):
        parsing.parse_scryfall_query(invalid_query)


@pytest.mark.parametrize(
    argnames="semantically_invalid_query",
    argvalues=[
        "name:bolt and 1",  # Valid parse but semantically invalid: AND between boolean and integer
        "cmc=3 and 2",  # Valid parse but semantically invalid: AND between boolean and integer
        "power>1 or 5",  # Valid parse but semantically invalid: OR between boolean and integer
    ],
)
def test_semantically_invalid_queries_parse_but_fail_at_db_level(semantically_invalid_query: str) -> None:
    """Test that queries with standalone numeric literals parse but would fail at DB level.

    These queries are syntactically valid after issue #90 (allowing standalone numeric literals),
    but they're semantically invalid because they combine boolean expressions with bare integers.
    They should parse successfully but would fail at the database level with datatype mismatch errors.
    """
    # These should parse without errors
    parsed_query = parsing.parse_scryfall_query(semantically_invalid_query)

    # These should parse successfully (SQL generation would be tested in test_sql_gen.py)
    assert parsed_query is not None


def test_standalone_numeric_query_parses() -> None:
    """Test that standalone numeric queries like '1' parse to NumericValueNode.

    Per issue #90, queries like '1' should parse successfully to a NumericValueNode,
    but then fail at the database level with a datatype mismatch error since
    PostgreSQL expects boolean values in WHERE clauses, not integers.
    """
    # Test integer
    parsed_query = parsing.parse_scryfall_query("1")
    assert isinstance(parsed_query.root, NumericValueNode)
    assert parsed_query.root.value == 1

    # Test float
    parsed_query_float = parsing.parse_scryfall_query("2.5")
    assert isinstance(parsed_query_float.root, NumericValueNode)
    assert parsed_query_float.root.value == 2.5


@pytest.mark.parametrize(
    argnames=("input_query", "expected_ast"),
    argvalues=[
        (
            "artist:moeller",
            BinaryOperatorNode(CardAttributeNode("artist", ParserClass.TEXT), ":", StringValueNode("moeller")),
        ),
        ("a:moeller", BinaryOperatorNode(CardAttributeNode("a", ParserClass.TEXT), ":", StringValueNode("moeller"))),
        (
            'artist:"Christopher Moeller"',
            BinaryOperatorNode(CardAttributeNode("artist", ParserClass.TEXT), ":", StringValueNode("Christopher Moeller")),
        ),
        (
            "artist:nielsen",
            BinaryOperatorNode(CardAttributeNode("artist", ParserClass.TEXT), ":", StringValueNode("nielsen")),
        ),
        (
            "ARTIST:moeller",
            BinaryOperatorNode(CardAttributeNode("ARTIST", ParserClass.TEXT), ":", StringValueNode("moeller")),
        ),
    ],
)
def test_parse_artist_searches(input_query: str, expected_ast: BinaryOperatorNode) -> None:
    """Test parsing artist search queries."""
    result = parsing.parse_search_query(input_query)
    assert result.root == expected_ast


def test_parse_combined_artist_queries() -> None:
    """Test parsing combined queries with artist attributes."""
    # Test combining artist with other attributes
    query1 = "cmc<=3 artist:moeller"
    result1 = parsing.parse_search_query(query1)
    expected1 = AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "<=", NumericValueNode(3)),
            BinaryOperatorNode(CardAttributeNode("artist", ParserClass.TEXT), ":", StringValueNode("moeller")),
        ],
    )
    assert result1.root == expected1

    # Test combining multiple text attributes including artist
    query2 = "name:lightning OR artist:moeller"
    result2 = parsing.parse_search_query(query2)
    expected2 = OrNode(
        [
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("lightning")),
            BinaryOperatorNode(CardAttributeNode("artist", ParserClass.TEXT), ":", StringValueNode("moeller")),
        ],
    )
    assert result2.root == expected2


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=[
        (
            "format:standard",
            BinaryOperatorNode(CardAttributeNode("format", ParserClass.LEGALITY), ":", StringValueNode("standard")),
        ),
        ("f:modern", BinaryOperatorNode(CardAttributeNode("f", ParserClass.LEGALITY), ":", StringValueNode("modern"))),
        (
            "legal:legacy",
            BinaryOperatorNode(CardAttributeNode("legal", ParserClass.LEGALITY), ":", StringValueNode("legacy")),
        ),
        (
            "banned:standard",
            BinaryOperatorNode(CardAttributeNode("banned", ParserClass.LEGALITY), ":", StringValueNode("standard")),
        ),
        (
            "restricted:vintage",
            BinaryOperatorNode(CardAttributeNode("restricted", ParserClass.LEGALITY), ":", StringValueNode("vintage")),
        ),
        (
            'format:"Commander"',
            BinaryOperatorNode(CardAttributeNode("format", ParserClass.LEGALITY), ":", StringValueNode("Commander")),
        ),
        (
            "FORMAT:standard",
            BinaryOperatorNode(CardAttributeNode("FORMAT", ParserClass.LEGALITY), ":", StringValueNode("standard")),
        ),
        (
            "LEGAL:modern",
            BinaryOperatorNode(CardAttributeNode("LEGAL", ParserClass.LEGALITY), ":", StringValueNode("modern")),
        ),
    ],
)
def test_parse_legality_searches(test_input: str, expected_ast: QueryNode) -> None:
    """Test that legality search queries parse to expected AST nodes."""
    result = parsing.parse_search_query(test_input)
    assert result.root == expected_ast


def test_parse_combined_legality_queries() -> None:
    """Test parsing of complex queries combining legality searches."""
    # Test AND combination
    query1 = "format:standard banned:modern"
    result1 = parsing.parse_search_query(query1)
    expected1 = AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("format", ParserClass.LEGALITY), ":", StringValueNode("standard")),
            BinaryOperatorNode(CardAttributeNode("banned", ParserClass.LEGALITY), ":", StringValueNode("modern")),
        ],
    )
    assert result1.root == expected1

    # Test OR combination
    query2 = "legal:legacy or restricted:vintage"
    result2 = parsing.parse_search_query(query2)
    expected2 = OrNode(
        [
            BinaryOperatorNode(CardAttributeNode("legal", ParserClass.LEGALITY), ":", StringValueNode("legacy")),
            BinaryOperatorNode(CardAttributeNode("restricted", ParserClass.LEGALITY), ":", StringValueNode("vintage")),
        ],
    )
    assert result2.root == expected2


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=[
        (
            "number:123",
            BinaryOperatorNode(CardAttributeNode("number", ParserClass.NUMERIC), ":", NumericValueNode(123)),
        ),
        ("cn:45", BinaryOperatorNode(CardAttributeNode("cn", ParserClass.NUMERIC), ":", NumericValueNode(45))),
        ("number:1a", BinaryOperatorNode(CardAttributeNode("number", ParserClass.TEXT), ":", StringValueNode("1a"))),
        ("cn:100b", BinaryOperatorNode(CardAttributeNode("cn", ParserClass.TEXT), ":", StringValueNode("100b"))),
        (
            'number:"123"',
            BinaryOperatorNode(CardAttributeNode("number", ParserClass.TEXT), ":", StringValueNode("123")),
        ),
        ("cn:'45a'", BinaryOperatorNode(CardAttributeNode("cn", ParserClass.TEXT), ":", StringValueNode("45a"))),
        (
            "NUMBER:123",
            BinaryOperatorNode(CardAttributeNode("NUMBER", ParserClass.NUMERIC), ":", NumericValueNode(123)),
        ),
        ("CN:45", BinaryOperatorNode(CardAttributeNode("CN", ParserClass.NUMERIC), ":", NumericValueNode(45))),
    ],
)
def test_parse_collector_number_searches(test_input: str, expected_ast: BinaryOperatorNode) -> None:
    """Test parsing of collector number searches with various aliases and formats."""
    observed = parsing.parse_search_query(test_input)
    assert observed.root == expected_ast


def test_parse_combined_collector_number_queries() -> None:
    """Test parsing of complex queries combining collector number searches."""
    # Test AND combination
    query1 = "number:123 set:dom"
    result1 = parsing.parse_search_query(query1)
    expected1 = AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("number", ParserClass.NUMERIC), ":", NumericValueNode(123)),
            BinaryOperatorNode(CardAttributeNode("set", ParserClass.TEXT), ":", StringValueNode("dom")),
        ],
    )
    assert result1.root == expected1

    # Test OR combination
    query2 = "cn:1 or cn:2"
    result2 = parsing.parse_search_query(query2)
    expected2 = OrNode(
        [
            BinaryOperatorNode(CardAttributeNode("cn", ParserClass.NUMERIC), ":", NumericValueNode(1)),
            BinaryOperatorNode(CardAttributeNode("cn", ParserClass.NUMERIC), ":", NumericValueNode(2)),
        ],
    )
    assert result2.root == expected2


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=[
        (
            "m:2{R}{G}",
            BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("2{R}{G}")),
        ),
        ("m:{15}", BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("{15}"))),
        (
            "m:{1}g{1}",
            BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("{1}G{1}")),
        ),
        (
            "m:{1}{g}{1}",
            BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("{1}{G}{1}")),
        ),
        (
            "m:{2/W}G",
            BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("{2/W}G")),
        ),
        (
            "m:{2}{R}{G}",
            BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("{2}{R}{G}")),
        ),
        (
            "m:{X}{X}{W}",
            BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("{X}{X}{W}")),
        ),
        ("m=2RRG", BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), "=", parsing.ManaValueNode("2RRG"))),
        (
            "mana:1WU",
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("1WU")),
        ),
        ("mana:WU", BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("WU"))),
        (
            "mana:{0}",
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("{0}")),
        ),
        (
            "mana:{1}{G}",
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("{1}{G}")),
        ),
        (
            "mana:{W/U}",
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("{W/U}")),
        ),
        (
            "mana=1{G}",
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), "=", parsing.ManaValueNode("1{G}")),
        ),
        (
            "mana=W{U/R}",
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), "=", parsing.ManaValueNode("W{U/R}")),
        ),
    ],
)
def test_parse_mixed_mana_notation(test_input: str, expected_ast: BinaryOperatorNode) -> None:
    """Test parsing mana cost searches with mixed notation (per Scryfall rules)."""
    observed = parsing.parse_search_query(test_input)
    assert observed.root == expected_ast


def test_parse_combined_mana_queries() -> None:
    """Test parsing combined queries with mana cost searches."""
    # Test combining mana with other attributes
    query1 = "cmc<=3 mana:{1}{G}"
    result1 = parsing.parse_search_query(query1)
    expected1 = parsing.AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "<=", NumericValueNode(3)),
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("{1}{G}")),
        ],
    )
    assert result1.root == expected1

    # Test combining multiple mana attributes
    query2 = "mana:{W} OR m:{U}"
    result2 = parsing.parse_search_query(query2)
    expected2 = parsing.OrNode(
        [
            BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("{W}")),
            BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ":", parsing.ManaValueNode("{U}")),
        ],
    )
    assert result2.root == expected2


def test_mana_cost_with_comparison_operators() -> None:
    """Test that mana cost searches work with different operators."""
    # Test colon operator (most common)
    query1 = "mana:{1}{G}"
    result1 = parsing.parse_search_query(query1)
    expected1 = BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ":", parsing.ManaValueNode("{1}{G}"))
    assert result1.root == expected1

    # Test equals operator
    query2 = "m={W}{U}"
    result2 = parsing.parse_search_query(query2)
    expected2 = BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), "=", parsing.ManaValueNode("{W}{U}"))
    assert result2.root == expected2


@pytest.mark.parametrize(
    argnames=("lowercase_query", "uppercase_query"),
    argvalues=[
        ("mana:{x}", "mana:{X}"),
        ("mana:{x}{x}", "mana:{X}{X}"),
        ("mana:{x}{x}{w}", "mana:{X}{X}{W}"),
        ("m:{x}", "m:{X}"),
    ],
)
def test_mana_symbol_case_insensitivity(lowercase_query: str, uppercase_query: str) -> None:
    """Test that mana symbols like {x} and {X} parse to the same AST."""
    lowercase_result = parsing.parse_search_query(lowercase_query)
    uppercase_result = parsing.parse_search_query(uppercase_query)

    # Both should parse to the same AST structure
    assert lowercase_result == uppercase_result, (
        f"Lowercase query '{lowercase_query}' and uppercase query '{uppercase_query}' "
        f"should parse to the same AST.\n"
        f"Lowercase AST: {lowercase_result}\n"
        f"Uppercase AST: {uppercase_result}"
    )


def test_mana_cost_approximate_comparisons() -> None:
    """Test mana cost approximate comparisons with <, <=, >, >= operators."""
    # Test <= operator - use regular parser for AST structure validation
    query1 = "mana<={2}{R}{R}"
    result1 = parsing.parse_search_query(query1)
    expected1 = BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), "<=", parsing.ManaValueNode("{2}{R}{R}"))
    assert result1.root == expected1

    # Test < operator
    query2 = "m<{1}{G}"
    result2 = parsing.parse_search_query(query2)
    expected2 = BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), "<", parsing.ManaValueNode("{1}{G}"))
    assert result2.root == expected2

    # Test >= operator (parsing test)
    query3 = "mana>={W}{U}"
    result3 = parsing.parse_search_query(query3)
    expected3 = BinaryOperatorNode(CardAttributeNode("mana", ParserClass.MANA), ">=", parsing.ManaValueNode("{W}{U}"))
    assert result3.root == expected3

    # Test > operator
    query4 = "m>{0}"
    result4 = parsing.parse_search_query(query4)
    expected4 = BinaryOperatorNode(CardAttributeNode("m", ParserClass.MANA), ">", parsing.ManaValueNode("{0}"))
    assert result4.root == expected4


def test_mana_cost_sql_generation() -> None:
    """Test SQL generation for mana cost comparisons."""
    # Test basic equality (colon operator) - use scryfall parser for proper node types
    result1 = parsing.parse_scryfall_query("mana:{1}{G}")
    context1 = {}
    sql1 = result1.to_sql(context1)
    assert sql1 == "(%(p_dict_eydHJzogWzFdfQ)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_Mg)s)"
    assert mana_cost_str_to_dict("{1}{G}") in context1.values()
    assert calculate_cmc("{1}{G}") in context1.values()

    result1 = parsing.parse_scryfall_query("mana={1}{G}")
    context1 = {}
    sql1 = result1.to_sql(context1)
    assert sql1 == "(card.mana_cost_jsonb = %(p_dict_eydHJzogWzFdfQ)s AND card.cmc = %(p_int_Mg)s)"
    assert mana_cost_str_to_dict("{1}{G}") in context1.values()
    assert calculate_cmc("{1}{G}") in context1.values()

    # Test <= operator generates containment + cmc check
    result2 = parsing.parse_scryfall_query("mana<={2}{R}{R}")
    context2 = {}
    sql2 = result2.to_sql(context2)
    assert "card.mana_cost_jsonb <@" in sql2
    assert "card.cmc <=" in sql2
    assert mana_cost_str_to_dict("{2}{R}{R}") in context2.values()
    assert calculate_cmc("{2}{R}{R}") in context2.values()  # CMC of {2}{R}{R}

    # Test < operator includes inequality check
    result3 = parsing.parse_scryfall_query("mana<{1}{G}")
    context3 = {}
    sql3 = result3.to_sql(context3)
    assert "card.mana_cost_jsonb <@" in sql3
    assert "card.cmc <=" in sql3
    assert "card.mana_cost_jsonb <>" in sql3

    # Test >= operator reverses containment direction
    result4 = parsing.parse_scryfall_query("mana>={W}{U}")
    context4 = {}
    sql4 = result4.to_sql(context4)
    assert "<@ card.mana_cost_jsonb" in sql4
    assert "card.cmc >=" in sql4

    # Test > operator includes inequality
    result5 = parsing.parse_scryfall_query("mana>{0}")
    context5 = {}
    sql5 = result5.to_sql(context5)
    assert "<@ card.mana_cost_jsonb" in sql5
    assert "card.cmc >=" in sql5
    assert "card.mana_cost_jsonb <>" in sql5


@pytest.mark.parametrize(
    argnames=("mana_cost", "expected_cmc"),
    argvalues=[
        # Test basic braced costs
        ("{1}{G}", 2),
        ("{2}{R}{R}", 4),
        ("{W}{U}", 2),
        ("{0}", 0),
        ("{15}", 15),
        # Test hybrid costs (each counts as 1)
        ("{W/U}", 1),
        ("{2/W}", 1),
        ("{W/U/P}", 1),
        # Test X costs (X counts as 0 for CMC calculation)
        ("{X}{X}{W}", 1),
        # Test unbraced format
        ("1WU", 3),  # 1 generic + W + U
        ("2RRG", 5),  # 2 generic + R + R + G
        ("WU", 2),  # W + U
        ("11R", 12),  # 11 generic + R (consecutive digits as multi-digit)
        # Test mixed format
        ("1{G}", 2),
        ("W{U/R}", 2),
    ],
)
def test_mana_cost_cmc_calculation(mana_cost: str, expected_cmc: int) -> None:
    """Test CMC calculation for various mana costs."""
    assert calculate_cmc(mana_cost) == expected_cmc


@pytest.mark.parametrize(
    argnames=("mana_cost_str", "expected_dict"),
    argvalues=[
        # Basic conversions (braced format)
        ("{1}{G}", {"G": [1]}),
        ("{2}{R}{R}", {"R": [1, 2]}),
        ("{W}{U}", {"W": [1], "U": [1]}),
        ("{0}", {}),
        # Complex symbols (they should still count as single symbols)
        ("{W/U}", {"W/U": [1]}),
        ("{2/W}", {"2/W": [1]}),
        ("{X}{X}{W}", {"X": [1, 2], "W": [1]}),
        # Case sensitivity - lowercase should be converted to uppercase
        ("{g}{g}{g}", {"G": [1, 2, 3]}),
        ("{r}{u}{b}", {"R": [1], "U": [1], "B": [1]}),
        ("{w/u}", {"W/U": [1]}),
        ("{2/w}", {"2/W": [1]}),
        # Unbraced format
        ("1WU", {"W": [1], "U": [1]}),
        ("2RRG", {"R": [1, 2], "G": [1]}),
        ("WU", {"W": [1], "U": [1]}),
        # Mixed format (braced and unbraced)
        ("1{G}", {"G": [1]}),
        ("W{U/R}", {"W": [1], "U/R": [1]}),
    ],
)
def test_mana_cost_dict_conversion(mana_cost_str: str, expected_dict: dict) -> None:
    """Test mana cost to dict conversion."""
    assert mana_cost_str_to_dict(mana_cost_str) == expected_dict


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=[
        (
            "devotion:{G}",
            BinaryOperatorNode(CardAttributeNode("devotion", ParserClass.MANA), ":", parsing.ManaValueNode("{G}")),
        ),
        (
            "devotion={G}",
            BinaryOperatorNode(CardAttributeNode("devotion", ParserClass.MANA), "=", parsing.ManaValueNode("{G}")),
        ),
        (
            "devotion>={G}",
            BinaryOperatorNode(CardAttributeNode("devotion", ParserClass.MANA), ">=", parsing.ManaValueNode("{G}")),
        ),
        (
            "devotion>={G}{R}",
            BinaryOperatorNode(CardAttributeNode("devotion", ParserClass.MANA), ">=", parsing.ManaValueNode("{G}{R}")),
        ),
        (
            "devotion<={W}{U}",
            BinaryOperatorNode(CardAttributeNode("devotion", ParserClass.MANA), "<=", parsing.ManaValueNode("{W}{U}")),
        ),
        (
            "devotion>{B}",
            BinaryOperatorNode(CardAttributeNode("devotion", ParserClass.MANA), ">", parsing.ManaValueNode("{B}")),
        ),
        (
            "devotion<{R}{G}{B}",
            BinaryOperatorNode(CardAttributeNode("devotion", ParserClass.MANA), "<", parsing.ManaValueNode("{R}{G}{B}")),
        ),
    ],
)
def test_parse_devotion_searches(test_input: str, expected_ast: BinaryOperatorNode) -> None:
    """Test parsing devotion searches with various operators."""
    observed = parsing.parse_search_query(test_input)
    assert observed.root == expected_ast


def test_devotion_sql_generation() -> None:
    """Test SQL generation for devotion comparisons."""
    # Test basic equality (colon operator)
    result1 = parsing.parse_scryfall_query("devotion:{G}")
    context1 = {}
    sql1 = result1.to_sql(context1)
    assert "card.devotion" in sql1
    assert "G" in str(context1.values())

    # Test >= operator generates containment check
    result2 = parsing.parse_scryfall_query("devotion>={G}")
    context2 = {}
    sql2 = result2.to_sql(context2)
    assert "card.devotion" in sql2
    assert ">=" in sql2 or "@>" in sql2

    # Test >= operator with multiple colors
    result3 = parsing.parse_scryfall_query("devotion>={G}{R}")
    context3 = {}
    sql3 = result3.to_sql(context3)
    assert "card.devotion" in sql3
    assert "G" in str(context3.values())
    assert "R" in str(context3.values())


@pytest.mark.parametrize(
    argnames=("query", "description"),
    argvalues=[
        ("mana>{g}{g}{g}", "Braced format should work"),
        ("mana>ggg", "Unbraced format should work"),
        ("m>GGG", "Uppercase unbraced should work"),
        ("mana<=ggg", "Less than or equal with unbraced"),
        ("mana<ggg", "Less than with unbraced"),
        ("mana>=ggg", "Greater than or equal with unbraced"),
    ],
)
def test_mana_cost_string_format_comparisons(query: str, description: str) -> None:
    """Test mana cost comparisons work with both {X} and X string formats."""
    # Test that both formats parse correctly and generate SQL
    # Test that parsing works
    result = parsing.parse_scryfall_query(query)
    assert result is not None, f"Failed to parse {query}"

    # Test that SQL generation works (should not raise NotImplementedError)
    context = {}
    sql = result.to_sql(context)
    assert sql is not None, f"Failed to generate SQL for {query}"
    assert "card.mana_cost_jsonb" in sql, f"Should use JSONB containment for {query}"
    assert "card.cmc" in sql, f"Should use CMC check for {query}"
    assert context == {"p_dict_eydHJzogWzEsIDIsIDNdfQ": {"G": [1, 2, 3]}, "p_int_Mw": 3}


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=[
        ("produces:g", BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("g"))),
        (
            "produces:wu",
            BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("wu")),
        ),
        (
            "produces:wubrg",
            BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("wubrg")),
        ),
        ("produces:c", BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("c"))),
        ("produces:G", BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("G"))),
        (
            "produces:WU",
            BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("WU")),
        ),
        (
            'produces:"wu"',
            BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("wu")),
        ),
        ("PRODUCES:g", BinaryOperatorNode(CardAttributeNode("PRODUCES", ParserClass.COLOR), ":", StringValueNode("g"))),
    ],
)
def test_parse_produces_searches(test_input: str, expected_ast: BinaryOperatorNode) -> None:
    """Test parsing of produces searches for mana production."""
    observed = parsing.parse_search_query(test_input)
    assert observed.root == expected_ast


def test_parse_combined_produces_queries() -> None:
    """Test parsing of complex queries combining produces searches."""
    # Test combining produces with other attributes
    query1 = "produces:g type:land"
    result1 = parsing.parse_search_query(query1)
    expected1 = parsing.AndNode(
        [
            BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("g")),
            BinaryOperatorNode(CardAttributeNode("type", ParserClass.TEXT), ":", StringValueNode("land")),
        ],
    )
    assert result1.root == expected1

    # Test OR with produces
    query2 = "produces:w OR produces:u"
    result2 = parsing.parse_search_query(query2)
    expected2 = parsing.OrNode(
        [
            BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("w")),
            BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), ":", StringValueNode("u")),
        ],
    )
    assert result2.root == expected2

    # Test produces with comparison operators
    query3 = "produces=wu"
    result3 = parsing.parse_search_query(query3)
    expected3 = BinaryOperatorNode(CardAttributeNode("produces", ParserClass.COLOR), "=", StringValueNode("wu"))
    assert result3.root == expected3


@pytest.mark.parametrize(
    argnames=("test_input", "expected_ast"),
    argvalues=[
        # Simple hyphenated words in name search (implicit)
        (
            "dual-land",
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("dual-land")),
        ),
        # Hyphenated word with explicit name attribute
        (
            "name:test-word",
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("test-word")),
        ),
        # Hyphenated term in oracle tag attribute
        (
            "otag:dual-land",
            BinaryOperatorNode(CardAttributeNode("otag", ParserClass.TEXT), ":", StringValueNode("dual-land")),
        ),
        # Hyphenated term in oracle_tags (full alias)
        (
            "oracle_tags:dual-land",
            BinaryOperatorNode(CardAttributeNode("oracle_tags", ParserClass.TEXT), ":", StringValueNode("dual-land")),
        ),
        # Hyphenated term in 'is' attribute
        (
            "is:modal-dfc",
            BinaryOperatorNode(CardAttributeNode("is", ParserClass.TEXT), ":", StringValueNode("modal-dfc")),
        ),
        # Numeric prefix with hyphen (40k-model)
        (
            "otag:40k-model",
            BinaryOperatorNode(CardAttributeNode("otag", ParserClass.TEXT), ":", StringValueNode("40k-model")),
        ),
        # Complex hyphenated term with multiple hyphens
        (
            "otag:cycle-shm-common-hybrid-1-drop",
            BinaryOperatorNode(
                CardAttributeNode("otag", ParserClass.TEXT),
                ":",
                StringValueNode("cycle-shm-common-hybrid-1-drop"),
            ),
        ),
        # Hyphenated word integrated with other query features
        (
            "otag:dual-land cmc=3",
            AndNode(
                [
                    BinaryOperatorNode(CardAttributeNode("otag", ParserClass.TEXT), ":", StringValueNode("dual-land")),
                    BinaryOperatorNode(CardAttributeNode("cmc", ParserClass.NUMERIC), "=", NumericValueNode(3)),
                ],
            ),
        ),
        # Multiple hyphenated words in same query
        (
            "otag:dual-land is:modal-dfc",
            AndNode(
                [
                    BinaryOperatorNode(CardAttributeNode("otag", ParserClass.TEXT), ":", StringValueNode("dual-land")),
                    BinaryOperatorNode(CardAttributeNode("is", ParserClass.TEXT), ":", StringValueNode("modal-dfc")),
                ],
            ),
        ),
        # Hyphenated word with quoted syntax (should also work)
        (
            'otag:"dual-land"',
            BinaryOperatorNode(CardAttributeNode("otag", ParserClass.TEXT), ":", StringValueNode("dual-land")),
        ),
        # Simple multi-hyphen word
        (
            "a-b-c",
            BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode("a-b-c")),
        ),
        # Trailing dot in oracle text value (sentence-ending period)
        (
            "o:token.",
            BinaryOperatorNode(CardAttributeNode("o", ParserClass.TEXT), ":", StringValueNode("token.")),
        ),
        # Trailing dot with negation
        (
            "o:token. -o:counter",
            AndNode(
                [
                    BinaryOperatorNode(CardAttributeNode("o", ParserClass.TEXT), ":", StringValueNode("token.")),
                    NotNode(BinaryOperatorNode(CardAttributeNode("o", ParserClass.TEXT), ":", StringValueNode("counter"))),
                ],
            ),
        ),
    ],
)
def test_parse_hyphenated_words(test_input: str, expected_ast: QueryNode) -> None:
    """Test that hyphenated words parse correctly into the expected AST structure.

    This test verifies that the parser correctly handles hyphenated words in various contexts:
    - Simple hyphenated words in name searches
    - Hyphenated words in attribute values (otag, is, etc.)
    - Words with numeric prefixes followed by hyphens
    - Complex hyphenated terms with multiple hyphens
    - Integration with other query features
    """
    observed = parsing.parse_search_query(test_input).root

    # Compare the full AST structure
    assert observed == expected_ast, f"\nExpected: {expected_ast}\nObserved: {observed}"


@pytest.mark.parametrize(
    argnames="invalid_query",
    argvalues=[
        "word-",  # Standalone word ending with hyphen
        "-",  # Standalone hyphen
    ],
)
def test_hyphenated_words_edge_cases_fail(invalid_query: str) -> None:
    """Test that standalone words ending with hyphens fail to parse.

    Standalone words cannot end with hyphens - this should raise a ValueError.
    Note that:
    - A leading hyphen is interpreted as the negation operator (NOT), not as part of the word.
      For example, "-flying" is parsed as NOT applied to "flying", not as a word starting with a hyphen.
    - Attribute values (e.g., 'name:test-', 'otag:test-') use different parsing rules and DO allow
      trailing hyphens since they use string_value_word which accepts any hyphen placement.
    """
    with pytest.raises(ValueError, match="Failed to parse query"):
        parsing.parse_search_query(invalid_query)
