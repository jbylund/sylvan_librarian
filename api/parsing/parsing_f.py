"""Query parsing functions for Scryfall search syntax."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import cachebox
from pyparsing import (
    CaselessKeyword,
    Combine,
    Forward,
    Group,
    Literal,
    OneOrMore,
    Optional,
    ParseException,
    ParserElement,
    QuotedString,
    Regex,
    ZeroOrMore,
    oneOf,
)

from api.parsing.card_query_nodes import CardAttributeNode, to_card_query_ast
from api.parsing.db_info import (
    COLOR_NAME_TO_CODE,
    PARSER_CLASS_TO_FIELD_INFOS,
    ParserClass,
)
from api.parsing.nodes import (
    AndNode,
    BinaryOperatorNode,
    ManaValueNode,
    NotNode,
    NumericValueNode,
    OrNode,
    Query,
    QueryNode,
    RegexValueNode,
    StringValueNode,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# Enable pyparsing packrat caching for improved performance with increased cache size
ParserElement.enable_packrat(cache_size_limit=2**13)  # 8192 cache entries

# Constants
NEGATION_TOKEN_COUNT = 2
DEFAULT_OPERATORS = oneOf(": > < >= <= = !=")
COMPARISON_OPERATORS = frozenset([":", "=", "!=", ">", "<", ">=", "<="])


def make_regex_pattern(words: Iterable[str]) -> Regex:
    """Create a regex pattern for matching words with word boundaries.

    Args:
        words: Iterable of words to match

    Returns:
        Regex parser element with case-insensitive matching and word boundaries
    """
    if not words:
        # Return a pattern that matches nothing if no words provided
        return Regex(r"(?!)", flags=re.IGNORECASE)

    # Sort by length (longest first) to avoid partial matches, then wrap with word boundaries
    pattern = r"\b(" + "|".join(sorted(words, key=len, reverse=True)) + r")\b"
    return Regex(pattern, flags=re.IGNORECASE)


def balance_partial_query(query: str) -> str:
    """Balance quotes and parentheses for typeahead searches using a stack."""
    char_to_mirror = {
        "(": ")",
        "'": "'",  # single quote is own mirror
        '"': '"',  # double quote is own mirror
        ")": "(",
    }
    unbalanced_closing_chars = {")"}

    current_stack = []
    for char in query:
        mirrored_char = char_to_mirror.get(char)
        if not mirrored_char:
            continue
        if current_stack and current_stack[-1] == mirrored_char:
            current_stack.pop()
        else:
            if char in unbalanced_closing_chars:
                msg = f"Unbalanced closing character '{char}' cannot be balanced"
                raise ValueError(msg)
            current_stack.append(char)
    # add mirrored chars to the end of the query
    while current_stack:
        char = current_stack.pop()
        mirrored_char = char_to_mirror[char]
        query += mirrored_char
    return query


def flatten_nested_operations(node: QueryNode) -> QueryNode:
    """Flatten nested operations of the same type to create canonical n-ary forms.

    For example, (A AND (B AND C)) becomes (A AND B AND C).
    """
    # This function recursively flattens nested AND/OR nodes
    if isinstance(node, AndNode):
        operands: list[QueryNode] = []
        for operand in node.operands:
            if isinstance(operand, AndNode):
                flattened = flatten_nested_operations(operand)
                operands.extend(flattened.operands)
            else:
                operands.append(flatten_nested_operations(operand))
        return AndNode(operands)
    if isinstance(node, OrNode):
        operands: list[QueryNode] = []
        for operand in node.operands:
            if isinstance(operand, OrNode):
                flattened = flatten_nested_operations(operand)
                operands.extend(flattened.operands)
            else:
                operands.append(flatten_nested_operations(operand))
        return OrNode(operands)
    if isinstance(node, NotNode):
        return NotNode(flatten_nested_operations(node.operand))
    if isinstance(node, Query):
        return Query(flatten_nested_operations(node.root))
    return node


def create_value_node(value: object) -> QueryNode:
    """Create the appropriate QueryNode type for a value.

    Returns the appropriate QueryNode type based on the input value.
    If the value is already a QueryNode, returns it directly.
    Note: AttributeNode instances are created directly by parse actions.
    """
    # If it's already a QueryNode, return it directly
    if isinstance(value, QueryNode):
        return value

    # This function determines the correct node type for a value
    if isinstance(value, int | float):
        return NumericValueNode(value)
    if isinstance(value, str):
        return StringValueNode(value)
    if isinstance(value, tuple) and value[0] == "quoted":
        return StringValueNode(value[1])
    if isinstance(value, tuple) and value[0] == "regex":
        return RegexValueNode(value[1])
    return value  # Fallback for other types


def make_binary_operator_node(tokens: list[object]) -> BinaryOperatorNode:
    """Create a BinaryOperatorNode, properly wrapping attributes and values."""
    # Used as a parse action for binary operator expressions
    left, operator, right = tokens
    return BinaryOperatorNode(create_value_node(left), operator, create_value_node(right))


def create_attribute_parser(parser_class: ParserClass) -> ParserElement:
    """Factory function to create attribute parsers with consistent parse actions.

    Args:
        parser_class: The parser class to use for this attribute.

    Returns:
        Parser element that matches attributes and creates AttributeNode instances
    """
    field_infos = PARSER_CLASS_TO_FIELD_INFOS[parser_class]

    # get all the aliases...
    aliases = set()
    for field_info in field_infos:
        aliases.update(a.lower() for a in field_info.search_aliases)
    parser = make_regex_pattern(aliases)

    def parse_action(tokens: list[str]) -> CardAttributeNode:
        """Parse action for the attribute parser."""
        matched_alias = tokens[0].lower()
        return CardAttributeNode(
            attribute_name=matched_alias,
            matched_parser_class=parser_class,
        )

    parser.setParseAction(parse_action)
    return parser


def create_condition_parser(
    attr_parser: ParserElement,
    value_parser: ParserElement,
    operators: ParserElement = DEFAULT_OPERATORS,
) -> ParserElement:
    """Factory function to create condition parsers with consistent structure.

    Args:
        attr_parser: Parser for the attribute part
        value_parser: Parser for the value part
        operators: Optional custom operators parser (defaults to standard attribute operators)

    Returns:
        Parser element that matches attribute operator value patterns
    """
    condition = attr_parser + operators + value_parser
    condition.setParseAction(make_binary_operator_node)
    return condition


def create_parsing_error(context: str, original_error: Exception, query: str = "") -> ValueError:
    """Create a standardized parsing error with helpful context.

    Args:
        context: Description of what was being parsed
        original_error: The original exception that occurred
        query: The query string being parsed (optional)

    Returns:
        A ValueError with a standardized error message
    """
    if query:
        msg = f"Parse error in {context} for query '{query}': {original_error}"
    else:
        msg = f"Parse error in {context}: {original_error}"
    return ValueError(msg)


def create_basic_parsers() -> dict[str, ParserElement]:
    """Create basic parsing elements used throughout the grammar.

    Returns:
        Dictionary containing basic parser elements
    """
    # Basic operators and keywords
    attrop = DEFAULT_OPERATORS
    arithmetic_op = oneOf("+ - * /")
    # Integer with word boundary - prevents partial matches like "1" from "1a" or "10" from "100b"
    # Word boundary ensures the number is a complete token, not part of an alphanumeric string
    integer = Regex(r"\b\d+\b").setParseAction(lambda t: int(t[0]))
    # Float must have a decimal point to distinguish from integer
    # Also uses word boundary to prevent matching prefixes
    float_number = Regex(r"\b\d+\.\d*\b").setParseAction(lambda t: float(t[0]))
    lparen = Literal("(").suppress()
    rparen = Literal(")").suppress()

    # Keywords must be recognized before regular words
    operator_and = CaselessKeyword("AND")
    operator_or = CaselessKeyword("OR")
    operator_not = Literal("-")

    # Handle quoted strings and regular words (but not keywords)
    def make_quoted_string(tokens: list[str]) -> tuple[str, str]:
        """Mark quoted strings so they're always treated as string values."""
        return ("quoted", tokens[0])

    quoted_string = (QuotedString('"', escChar="\\") | QuotedString("'", escChar="\\")).setParseAction(make_quoted_string)

    # Regex pattern parser - matches /pattern/ with escaped forward slashes
    def make_regex_pattern_value(tokens: list[str]) -> tuple[str, str]:
        r"""Mark regex patterns so they're treated as regex values.

        Note: We preserve backslashes in the pattern because they're significant in regex.
        We only convert escaped forward slashes \/ back to /.
        """
        # With unquoteResults=False, the string includes the delimiters, so strip them
        # Then convert escaped forward slashes \/ back to /
        pattern = tokens[0][1:-1]  # Strip leading and trailing /
        pattern = pattern.replace("\\/", "/")
        return ("regex", pattern)

    # Use QuotedString with forward slash delimiter
    # unquoteResults=False keeps the original string with backslashes intact
    # convertWhitespaceEscapes=False prevents \n, \t, \b, etc. from being interpreted
    regex_pattern = QuotedString("/", escChar="\\", unquoteResults=False, convertWhitespaceEscapes=False).setParseAction(
        make_regex_pattern_value,
    )

    # Word that doesn't match keywords
    def make_word(tokens: list[str]) -> str:
        """Reject reserved keywords as words."""
        word_str = tokens[0]
        if word_str.upper() in ["AND", "OR"]:
            msg = f"Reserved keyword '{word_str}' cannot be used as a search term. Use quotes if you want to search for this word literally."
            raise ValueError(msg)
        # Validate that word doesn't start or end with hyphen
        if word_str.startswith("-") or word_str.endswith("-"):
            msg = f"Word '{word_str}' cannot start or end with a hyphen. Use quotes if you want to search for this word literally."
            raise ValueError(msg)
        return word_str

    # Word parser that accepts hyphens when between alphanumeric characters
    # Pattern: starts with alphanumeric/underscore, can contain hyphens in the middle, ends with alphanumeric/underscore
    word = Regex(r"[a-zA-Z_][a-zA-Z0-9_-]*[a-zA-Z0-9_]|[a-zA-Z_]").setParseAction(make_word)

    # Create a literal number parser for numeric constants
    # Note: float_number must come before integer to match decimal numbers
    # but only matches when there's actually a decimal point
    literal_number = float_number | integer

    # For attribute values, we want the raw string
    # Use Regex to match words that may contain hyphens for string values
    # Allow values starting with digits, letters, or underscores to handle cases like "40k-model"
    string_value_word = Regex(r"[a-zA-Z0-9_][a-zA-Z0-9_-]*")

    return {
        "attrop": attrop,
        "arithmetic_op": arithmetic_op,
        "integer": integer,
        "float_number": float_number,
        "lparen": lparen,
        "rparen": rparen,
        "operator_and": operator_and,
        "operator_or": operator_or,
        "operator_not": operator_not,
        "quoted_string": quoted_string,
        "regex_pattern": regex_pattern,
        "word": word,
        "literal_number": literal_number,
        "string_value_word": string_value_word,
    }


def create_mana_parsers() -> dict[str, ParserElement]:
    """Create mana-related parsing elements.

    Returns:
        Dictionary containing mana parser elements
    """
    # Mana cost patterns - support mixed notation as per Scryfall rules
    # Simple symbols don't need braces: W, U, B, R, G, C, 1, 2, etc.
    # Complex symbols (with alternatives) must use braces: {W/U}, {2/W}, {W/U/P}

    # Individual mana components
    curly_mana_symbol = Regex(r"\{[^}]+\}")  # Complex symbols in braces: {W/U}, {2/W}
    simple_mana_symbol = Regex(r"[0-9WUBRGCXYZwubrgcxyz]")  # Simple symbols without braces: W, 1, 2

    # Mixed mana pattern: any combination of simple and complex symbols
    # Examples: {1}{G}, 1{G}, 2RR, W{U/R}, {2/W}G, etc.
    mixed_mana_pattern = Combine(OneOrMore(curly_mana_symbol | simple_mana_symbol))

    # Create ManaValueNode for mana cost strings
    def make_mana_value_node(tokens: list[str]) -> ManaValueNode:
        """Create a ManaValueNode for mana cost strings.

        Normalizes mana symbols to uppercase for consistency.
        """
        return ManaValueNode(tokens[0].upper())

    mana_value = mixed_mana_pattern.setParseAction(make_mana_value_node)

    return {
        "mana_value": mana_value,
        "mixed_mana_pattern": mixed_mana_pattern,
    }


def create_color_parsers() -> dict[str, ParserElement]:
    """Create color-related parsing elements.

    Returns:
        Dictionary containing color parser elements
    """
    # Color value patterns - support both color names and letter combinations
    # Color names: white, blue, black, red, green, colorless (case-insensitive)
    color_word = make_regex_pattern(COLOR_NAME_TO_CODE)

    # Color letter pattern: any combination of w, u, b, r, g, c (case-insensitive)
    color_letter_pattern = Regex(r"[wubrgcWUBRGC]+")

    # Combined color value pattern
    color_value = color_word | color_letter_pattern

    return {
        "color_value": color_value,
    }


def create_all_condition_parsers(basic_parsers: dict, mana_parsers: dict, color_parsers: dict) -> dict[str, ParserElement]:
    """Create all condition parsers using factory functions.

    Args:
        basic_parsers: Dictionary of basic parser elements
        mana_parsers: Dictionary of mana parser elements
        color_parsers: Dictionary of color parser elements

    Returns:
        Dictionary containing all condition parser elements
    """
    # Extract needed parsers
    quoted_string = basic_parsers["quoted_string"]
    string_value_word = basic_parsers["string_value_word"]
    literal_number = basic_parsers["literal_number"]
    arithmetic_op = basic_parsers["arithmetic_op"]
    lparen = basic_parsers["lparen"]
    rparen = basic_parsers["rparen"]
    mana_value = mana_parsers["mana_value"]
    color_value = color_parsers["color_value"]

    # Create attribute parsers using factory functions
    numeric_attr_word = create_attribute_parser(ParserClass.NUMERIC)
    mana_attr_word = create_attribute_parser(ParserClass.MANA)
    rarity_attr_word = create_attribute_parser(ParserClass.RARITY)
    legality_attr_word = create_attribute_parser(ParserClass.LEGALITY)
    color_attr_word = create_attribute_parser(ParserClass.COLOR)
    text_attr_word = create_attribute_parser(ParserClass.TEXT)
    date_attr_word = create_attribute_parser(ParserClass.DATE)
    year_attr_word = create_attribute_parser(ParserClass.YEAR)

    # Build the grammar with proper precedence
    expr = Forward()

    # Define arithmetic expressions with proper precedence
    # Start with the most basic arithmetic terms
    # Only numeric attributes can be used in arithmetic expressions
    arithmetic_term = numeric_attr_word | literal_number | Group(lparen + expr + rparen)

    # Define arithmetic expressions that can be chained
    # Only match if there's at least one arithmetic operator
    arithmetic_expr = Forward()
    arithmetic_expr <<= arithmetic_term + arithmetic_op + arithmetic_term + ZeroOrMore(arithmetic_op + arithmetic_term)
    arithmetic_expr.setParseAction(make_chained_arithmetic)

    # Unified numeric comparison rule: handles all combinations of arithmetic expressions, numeric attributes, and literals
    # This consolidates the previous arithmetic_comparison and numeric_condition rules
    unified_numeric_comparison = (
        (arithmetic_expr | numeric_attr_word | literal_number)
        + DEFAULT_OPERATORS
        + (arithmetic_expr | numeric_attr_word | literal_number)
    )
    unified_numeric_comparison.setParseAction(make_binary_operator_node)

    # Create condition parsers using factory function where possible
    # For complex value types, we still need custom definitions

    # Mana condition: mana attributes with mana cost values (mana:{1}{G}, m:WU, etc.)
    # For mana attributes, try mana patterns first, then fall back to quoted strings and regular strings
    mana_value_or_string = mana_value | quoted_string | string_value_word
    mana_condition = create_condition_parser(mana_attr_word, mana_value_or_string)

    # Color condition: color attributes with color values (color:red, c:rg, id:wubr, etc.)
    color_condition = create_condition_parser(color_attr_word, color_value | quoted_string)

    # Standard string-based conditions using factory function
    regex_pattern = basic_parsers["regex_pattern"]
    rarity_condition = create_condition_parser(rarity_attr_word, quoted_string | string_value_word)
    legality_condition = create_condition_parser(legality_attr_word, quoted_string | string_value_word)
    text_condition = create_condition_parser(text_attr_word, regex_pattern | quoted_string | string_value_word)

    # Date condition: date attributes with date values (date:2025-02-02 or date:2025)
    # Accept both full date format (YYYY-MM-DD) and year format (YYYY)
    date_value = Regex(r"\d{4}(?:-\d{2}-\d{2})?")  # Matches YYYY or YYYY-MM-DD
    date_condition = create_condition_parser(date_attr_word, date_value)

    # Year condition: year attributes with 4-digit year values (year:2025)
    # Only accept 4-digit years
    year_value = Regex(r"\d{4}")  # Matches only YYYY
    year_condition = create_condition_parser(year_attr_word, year_value)

    # Attribute-to-attribute comparisons should be between attributes of the same parser class
    attr_attr_condition = (
        (numeric_attr_word + DEFAULT_OPERATORS + numeric_attr_word)
        | (mana_attr_word + DEFAULT_OPERATORS + mana_attr_word)
        | (rarity_attr_word + DEFAULT_OPERATORS + rarity_attr_word)
        | (legality_attr_word + DEFAULT_OPERATORS + legality_attr_word)
        | (color_attr_word + DEFAULT_OPERATORS + color_attr_word)
        | (text_attr_word + DEFAULT_OPERATORS + text_attr_word)
        | (date_attr_word + DEFAULT_OPERATORS + date_attr_word)
        | (year_attr_word + DEFAULT_OPERATORS + year_attr_word)
    )
    attr_attr_condition.setParseAction(make_binary_operator_node)

    # Combine all conditions with clear precedence - no more special cases needed
    condition = (
        mana_condition
        | rarity_condition
        | legality_condition
        | color_condition
        | date_condition
        | year_condition
        | unified_numeric_comparison
        | text_condition
        | attr_attr_condition
    )

    # Special rule for text attribute-colon-hyphenated-value to handle cases like "otag:dual-land" and "otag:40k-model"
    # Only text attributes should have hyphenated string values (not numeric, mana, rarity, or legality)
    # Allow values starting with digits, letters, or underscores
    hyphenated_condition = text_attr_word + Literal(":") + Regex(r"[a-zA-Z0-9_][a-zA-Z0-9_-]*")
    hyphenated_condition.setParseAction(make_binary_operator_node)

    return {
        "expr": expr,
        "arithmetic_expr": arithmetic_expr,
        "unified_numeric_comparison": unified_numeric_comparison,
        "mana_condition": mana_condition,
        "color_condition": color_condition,
        "rarity_condition": rarity_condition,
        "legality_condition": legality_condition,
        "text_condition": text_condition,
        "date_condition": date_condition,
        "year_condition": year_condition,
        "attr_attr_condition": attr_attr_condition,
        "condition": condition,
        "hyphenated_condition": hyphenated_condition,
        "numeric_attr_word": numeric_attr_word,
    }


def make_chained_arithmetic(tokens: list[object]) -> QueryNode:
    """Create a chained arithmetic expression with left associativity.

    For example, [a, +, b, +, c] becomes ((a + b) + c)
    """
    if len(tokens) == 1:
        return create_value_node(tokens[0])

    # Start with the first term
    result = create_value_node(tokens[0])

    # Process the remaining operator-term pairs
    for i in range(1, len(tokens), 2):
        if i + 1 < len(tokens):
            operator = tokens[i]
            right_term = create_value_node(tokens[i + 1])
            result = BinaryOperatorNode(result, operator, right_term)

    return result


def parse_scryfall_query(query: str) -> Query:
    """Parse a Scryfall search query and convert to Scryfall-specific AST.

    Args:
        query: The search query string to parse.

    Returns:
        A Scryfall-specific Query AST.
    """
    generic_query = parse_search_query(query)
    return to_card_query_ast(generic_query)


@cachebox.cached(cache={})
def get_parse_expr() -> ParserElement:  # noqa: C901, PLR0915
    """Create and return the main parser expression for Scryfall search queries.

    This function builds a comprehensive parsing grammar that supports the full
    Scryfall search syntax, including:

    - Attribute-value conditions (e.g., "cmc:3", "color:red", "type:creature")
    - Comparison operators (>, <, >=, <=, =, !=, :)
    - Boolean operators (AND, OR, NOT/-)
    - Arithmetic expressions (e.g., "cmc+1", "power*2")
    - Quoted strings and regular words
    - Mana cost patterns (e.g., "{1}{G}", "WU", "{W/U}")
    - Color values (names like "red" or letters like "rg")
    - Grouped expressions with parentheses
    - Negation of conditions and factors

    The parser handles complex precedence rules where:
    - Parentheses have highest precedence
    - NOT/- negation applies to individual factors
    - AND/OR operators group operands by type for n-ary operations
    - Arithmetic expressions support chaining with left associativity
    - Different attribute types have specialized value parsers

    Returns:
        ParserElement: The main expression parser that can parse complete
            Scryfall search queries into an Abstract Syntax Tree (AST).

    Note:
        This function is cached to avoid rebuilding the complex grammar
        on every call, improving performance for repeated parsing operations.
    """
    # Create basic parsing elements using helper functions
    basic_parsers = create_basic_parsers()
    mana_parsers = create_mana_parsers()
    color_parsers = create_color_parsers()

    # Extract frequently used parsers
    lparen = basic_parsers["lparen"]
    rparen = basic_parsers["rparen"]
    operator_and = basic_parsers["operator_and"]
    operator_or = basic_parsers["operator_or"]
    operator_not = basic_parsers["operator_not"]
    word = basic_parsers["word"]
    literal_number = basic_parsers["literal_number"]

    # Build all condition parsers using helper function
    condition_parsers = create_all_condition_parsers(basic_parsers, mana_parsers, color_parsers)

    # Extract condition parsers
    expr = condition_parsers["expr"]
    arithmetic_expr = condition_parsers["arithmetic_expr"]
    condition = condition_parsers["condition"]
    hyphenated_condition = condition_parsers["hyphenated_condition"]
    attr_attr_condition = condition_parsers["attr_attr_condition"]

    # Single word (implicit name search)
    def make_single_word(tokens: list[str]) -> BinaryOperatorNode:
        """For single words, always search in the name field."""
        return BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode(tokens[0]))

    single_word = word.setParseAction(make_single_word)

    # Standalone numeric literal
    def make_numeric_literal(tokens: list[object]) -> NumericValueNode:
        """Create a NumericValueNode for standalone numeric literals."""
        return NumericValueNode(tokens[0])

    standalone_numeric = literal_number.setParseAction(make_numeric_literal)

    # Grouped expression
    def make_group(tokens: list[object]) -> object:
        """Return the grouped expression inside parentheses."""
        return tokens[0]

    group = Group(lparen + expr + rparen).setParseAction(make_group)

    # Factor: can be negated (but not arithmetic expressions)
    def handle_negation(tokens: list[object]) -> object:
        """Handle negation (NOT) for factors, disallowing arithmetic negation.

        Args:
            tokens: List of tokens to process.

        Returns:
            The processed token(s).
        """
        if len(tokens) == 1:
            return tokens[0]
        if len(tokens) == NEGATION_TOKEN_COUNT and tokens[0] == "-":
            # Don't allow negation of arithmetic expressions
            if isinstance(tokens[1], BinaryOperatorNode) and tokens[1].operator in ["+", "-", "*", "/"]:
                msg = f"Cannot negate arithmetic expressions like '{tokens[1]}'. Use parentheses if you want to negate the result of arithmetic."
                raise ValueError(msg)
            return NotNode(tokens[1])
        return tokens[0]

    # For negation, we exclude arithmetic expressions from being negated
    # Test: revert to original order to confirm this breaks it
    negatable_primary = attr_attr_condition | condition | group | single_word
    negatable_factor = Optional(operator_not) + negatable_primary
    negatable_factor.setParseAction(handle_negation)

    # Factor includes both negatable expressions and arithmetic expressions
    # SPECIAL: hyphenated_condition first to handle "otag:dual-land", then condition (includes comparisons) before standalone arithmetic
    # Note: arithmetic_comparison is now consolidated into unified_numeric_comparison within condition
    # Add standalone_numeric at the end to handle cases like "1" without operators
    factor = condition | hyphenated_condition | arithmetic_expr | negatable_factor | standalone_numeric

    # Expression with explicit AND/OR operators (highest precedence)
    def handle_operators(tokens: list[object]) -> object:
        """Handle AND/OR operators, grouping operands by operator type and building n-ary nodes.

        Args:
            tokens: List of tokens to process.

        Returns:
            The processed token(s).
        """
        if len(tokens) == 1:
            return tokens[0]
        # Group operands by operator type
        current_operands = [tokens[0]]
        current_operator = None
        for i in range(1, len(tokens), 2):
            if i + 1 < len(tokens):
                operator = tokens[i]
                right = tokens[i + 1]
                if current_operator is None:
                    # First operator, start collecting
                    current_operator = operator.upper()
                    current_operands.append(right)
                elif operator.upper() == current_operator:
                    # Same operator, add to current group
                    current_operands.append(right)
                else:
                    # Different operator, create node for current group and start new group
                    if current_operator == "AND":
                        result = AndNode(current_operands)
                    elif current_operator == "OR":
                        result = OrNode(current_operands)
                    else:
                        msg = f"Unknown operator: {current_operator}"
                        raise ValueError(msg)
                    # Start new group with the result as first operand
                    current_operands = [result, right]
                    current_operator = operator.upper()
        # Create final node for remaining operands
        if current_operator == "AND":
            return AndNode(current_operands)
        if current_operator == "OR":
            return OrNode(current_operands)
        # No operators, just return the single operand
        return current_operands[0]

    # The main expression: factors separated by AND/OR operators
    expr <<= factor + ZeroOrMore((operator_and | operator_or) + factor)
    expr.setParseAction(handle_operators)
    return expr


def parse_search_query(query: str) -> Query:
    """Parse a search query string into a Query AST.

    This function is the main entry point for parsing Scryfall-style search queries.
    It handles the complete parsing pipeline including preprocessing, parsing, and
    AST normalization.

    The function performs the following steps:
    1. Validates input and handles empty queries
    2. Preprocesses the query to convert implicit AND operations to explicit ones
    3. Uses the main parser expression to parse the query into tokens
    4. Flattens nested operations to create canonical n-ary forms
    5. Wraps the result in a Query AST node

    Args:
        query: The search query string to parse. Can be None or empty.

    Returns:
        Query: A Query AST node containing the parsed query structure.
            For empty queries, returns a default query that searches for
            empty name values.

    Raises:
        ValueError: If parsing fails due to syntax errors, invalid operators,
            or other semantic issues. The error message includes context
            about what was being parsed and where the error occurred.

    Examples:
        >>> parse_search_query("cmc:3")
        Query(BinaryOperatorNode(AttributeNode("cmc"), ":", NumericValueNode(3)))

        >>> parse_search_query("red AND creature")
        Query(AndNode([BinaryOperatorNode(AttributeNode("name"), ":", StringValueNode("red")),
                       BinaryOperatorNode(AttributeNode("name"), ":", StringValueNode("creature"))]))

        >>> parse_search_query("")
        Query(BinaryOperatorNode(AttributeNode("name"), ":", StringValueNode("")))
    """
    original_query = query
    if query is None or not query.strip():
        # Return empty query
        return Query(BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", ""))

    # Pre-process the query to handle implicit AND operations
    # Convert "a b" to "a AND b" when b is not an operator
    query = preprocess_implicit_and(query)
    expr = get_parse_expr()

    # Parse the query
    try:
        parsed = expr.parseString(query, parseAll=True)
        if parsed:
            # Flatten nested operations to create canonical n-ary forms
            return flatten_nested_operations(Query(parsed[0]))
        return Query(BinaryOperatorNode("name", ":", ""))
    except (ValueError, TypeError, IndexError) as e:
        msg = "main query parsing"
        raise create_parsing_error(msg, e, query) from e
    except ParseException as e:
        # ParseException has more specific information about where parsing failed
        # Keep backward compatibility while providing more context
        msg = f'Failed to parse query: "{original_query}"'
        raise ValueError(msg) from e


@cachebox.cached(cache={})
def _get_implicit_and_tokenizer() -> ParserElement:
    """Build a tokenizer for preprocess_implicit_and using the same primitives as the main grammar.

    Returns a parser that parses a query string and returns a list of token strings (raw,
    for serialization). Token order matches the main grammar so boundaries align.
    """
    # Quoted strings (raw including quotes)
    quoted_raw = (
        QuotedString('"', escChar="\\", unquoteResults=False) | QuotedString("'", escChar="\\", unquoteResults=False)
    ).setParseAction(lambda t: t[0])

    # Regex /.../ (raw including slashes)
    regex_raw = QuotedString("/", escChar="\\", unquoteResults=False, convertWhitespaceEscapes=False).setParseAction(lambda t: t[0])

    # Parens (not suppressed so we get them in the list)
    lparen_tok = Literal("(").setParseAction(lambda t: t[0])
    rparen_tok = Literal(")").setParseAction(lambda t: t[0])

    # Keywords (must be before word so AND/OR are not consumed as words)
    and_tok = CaselessKeyword("AND").setParseAction(lambda t: t[0].upper())
    or_tok = CaselessKeyword("OR").setParseAction(lambda t: t[0].upper())

    # Comparison: longest first so >=, <=, != before single chars
    comparison_tok = oneOf(">= <= != : = > <").setParseAction(lambda t: t[0])
    arithmetic_tok = oneOf("+ - * /").setParseAction(lambda t: t[0])

    # Float (before string_value_word so "3.5" is one token)
    float_tok = Regex(r"\b\d+\.\d*\b").setParseAction(lambda t: t[0])

    # Value/word: alphanumeric, underscores, hyphens (matches bar, 40k-model, name, 1, 2, etc.)
    # Must come before mana_tok so "bar" and "bolt" are one token, not "b" + mana.
    string_value_tok = Regex(r"[a-zA-Z0-9_][a-zA-Z0-9_-]*").setParseAction(lambda t: t[0])

    # Mana pattern (e.g. {1}{G}, {w}{u}) as one token; only after word so "bar" isn't "b" + "ar"
    curly_mana_symbol = Regex(r"\{[^}]+\}")
    simple_mana_symbol = Regex(r"[0-9WUBRGCXYZwubrgcxyz]")
    mana_tok = Combine(OneOrMore(curly_mana_symbol | simple_mana_symbol)).setParseAction(lambda t: t[0])

    # One token: try in order (longest / most specific first)
    one_token = (
        quoted_raw
        | regex_raw
        | lparen_tok
        | rparen_tok
        | and_tok
        | or_tok
        | comparison_tok
        | arithmetic_tok
        | float_tok
        | string_value_tok
        | mana_tok
    )

    # Optional so empty or whitespace-only input yields []
    return Optional(OneOrMore(one_token)).setParseAction(lambda t: t.asList() if t else [])


def _tokenize_for_implicit_and(query: str) -> list[str]:
    """Tokenize a query string for implicit AND preprocessing. Raises ValueError on invalid input."""
    if not query.strip():
        return []
    tokenizer = _get_implicit_and_tokenizer()
    try:
        # Convert ParseResults to a real list for consistency with the return type
        result = tokenizer.parseString(query, parseAll=True).asList()
    except ParseException as e:
        # Pyparsing reports unclosed quotes/regex as "Expected string enclosed in..."
        msg = "Unmatched quote or regex in query"
        raise ValueError(msg) from e

    # Detect unclosed regex: leading "/" was tokenized as arithmetic because no closing "/" found
    stripped = query.strip()
    if stripped.startswith("/") and result and result[0] == "/" and stripped.count("/") == 1:
        msg = "Unmatched / in regex pattern in query"
        raise ValueError(msg)
    return result


def preprocess_implicit_and(query: str) -> str:
    """Pre-process query to convert implicit AND operations to explicit ones.

    Tokenizes using the same grammar primitives as the main parser, inserts AND
    between consecutive operands (and around negation), then serializes with no
    extra whitespace (e.g. 'foo bar' -> 'foo AND bar', 'cmc=3' -> 'cmc=3').
    See api/parsing/tests/test_preprocess_implicit_and.py for full behavior.
    """
    tokens = _tokenize_for_implicit_and(query)
    if not tokens:
        return ""

    # Operators (comparison, arithmetic, negation): no AND before/after
    # AND, OR, (, ): no AND between them and adjacent operands in the wrong place
    result: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        result.append(tok)

        if i + 1 >= len(tokens):
            i += 1
            continue

        next_tok = tokens[i + 1]

        def is_operand(t: str) -> bool:
            if t in ("(", ")", "AND", "OR"):
                return False
            return not is_operator(t)

        # Insert AND between: two operands, operand and negation (-), ) and operand, operand and (
        # Do not insert when we're inside a value that spans multiple tokens: the full parser
        # consumes e.g. "2{r}{g}" as one value (mixed_mana_pattern), but our tokenizer emits
        # 2 and {r}{g} separately (string_value_tok before mana_tok so "bar" stays one word).
        # So when the next token starts with "{", treat it as value continuation, not a new term.
        after_operator = i > 0 and is_operator(tokens[i - 1])
        value_continuation = next_tok.startswith("{")
        skip_because_value = after_operator and value_continuation
        need_and = (is_operand(tok) or tok == ")") and (is_operand(next_tok) or next_tok in {"(", "-"}) and not skip_because_value
        if need_and:
            result.append("AND")
        i += 1

    # Serialize: no space between tokens except " AND " and " OR "
    return "".join(f" {t} " if t in ("AND", "OR") else t for t in result)


def is_operator(token: str) -> bool:
    """Check if a token is an operator (comparison, arithmetic, or negation).

    Args:
        token: The token to check.

    Returns:
        True if the token is an operator, False otherwise.
    """
    return token in [":", ">", "<", ">=", "<=", "=", "!=", "-", "+", "*", "/"]


def generate_sql_query(parsed_query: Query) -> tuple[str, dict]:
    """Generate a SQL WHERE clause string from a parsed Query AST."""
    scryfall_ast = to_card_query_ast(parsed_query)
    query_context = {}
    return scryfall_ast.to_sql(query_context), query_context
