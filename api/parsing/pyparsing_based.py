"""pyparsing-based parser for Scryfall query syntax."""

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
    one_of,
)

from api.parsing.card_query_nodes import CardAttributeNode, ExactNameNode, to_card_query_ast
from api.parsing.db_info import (
    COLOR_NAME_TO_CODE,
    NUMERIC_CARD_ATTRIBUTES,
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
    TrueNode,
    flatten_nested_operations,
)
from api.parsing.rewrite import expand_derived_predicates

if TYPE_CHECKING:
    from collections.abc import Iterable

# Enable pyparsing packrat caching for improved performance with increased cache size
ParserElement.enable_packrat(cache_size_limit=2**13)  # 8192 cache entries

# Constants
NEGATION_TOKEN_COUNT = 2
DEFAULT_OPERATORS = one_of(": > < >= <= = !=")
COMPARISON_OPERATORS = frozenset([":", "=", "!=", ">", "<", ">=", "<="])

_NUMERIC_LITERAL_RE = re.compile(r"^\d+(\.\d+)?$")
_COMPARISON_OPERATORS = frozenset({">", "<", ">=", "<=", "=", "!=", ":"})

# Characters that make a query ineligible for the fast preprocess_implicit_and path.
_FP_UNSAFE_CHARS = frozenset("()\"'/{+*")
_FP_TERM_START_OPS = frozenset("><=!:")
_FP_TERM_END_OPS = frozenset("><=!:-")


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


def create_value_node(value: object) -> QueryNode:
    """Create the appropriate QueryNode type for a value.

    Returns the appropriate QueryNode type based on the input value.
    If the value is already a QueryNode, returns it directly.
    Note: AttributeNode instances are created directly by parse actions.
    """
    if isinstance(value, QueryNode):
        return value
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

    parser.set_parse_action(parse_action)
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
    condition.set_parse_action(make_binary_operator_node)
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
    attrop = DEFAULT_OPERATORS
    arithmetic_op = one_of("+ - * /")
    integer = Regex(r"\b\d+\b").set_parse_action(lambda t: int(t[0]))
    float_number = Regex(r"\b\d+\.\d*\b").set_parse_action(lambda t: float(t[0]))
    lparen = Literal("(").suppress()
    rparen = Literal(")").suppress()

    operator_and = CaselessKeyword("AND")
    operator_or = CaselessKeyword("OR")
    operator_not = Literal("-")

    def make_quoted_string(tokens: list[str]) -> tuple[str, str]:
        """Mark quoted strings so they're always treated as string values."""
        return ("quoted", tokens[0])

    quoted_string = (QuotedString('"', esc_char="\\") | QuotedString("'", esc_char="\\")).set_parse_action(make_quoted_string)

    def make_regex_pattern_value(tokens: list[str]) -> tuple[str, str]:
        r"""Mark regex patterns so they're treated as regex values.

        Note: We preserve backslashes in the pattern because they're significant in regex.
        We only convert escaped forward slashes \/ back to /.
        """
        pattern = tokens[0][1:-1]  # Strip leading and trailing /
        pattern = pattern.replace("\\/", "/")
        return ("regex", pattern)

    regex_pattern = QuotedString("/", esc_char="\\", unquote_results=False, convert_whitespace_escapes=False).set_parse_action(
        make_regex_pattern_value,
    )

    def make_word(tokens: list[str]) -> str:
        """Reject reserved keywords as words."""
        word_str = tokens[0]
        if word_str.upper() in ["AND", "OR"]:
            msg = f"Reserved keyword '{word_str}' cannot be used as a search term. Use quotes if you want to search for this word literally."
            raise ValueError(msg)
        if word_str.startswith("-") or word_str.endswith("-"):
            msg = f"Word '{word_str}' cannot start or end with a hyphen. Use quotes if you want to search for this word literally."
            raise ValueError(msg)
        return word_str

    # [^\W\d] is "word char that's not a digit" — i.e. any Unicode letter or underscore
    # (Python 3 `re` treats `\w`/`\W` as Unicode-aware by default for str patterns), so
    # bare words can start with accented letters like "Éowyn" (#649) without also
    # allowing a leading digit.
    word = Regex(r"[^\W\d][\w-]*\w|[^\W\d]").set_parse_action(make_word)

    literal_number = float_number | integer
    string_value_word = Regex(r"\w[\w.-]*")

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
    curly_mana_symbol = Regex(r"\{[^}]+\}")
    simple_mana_symbol = Regex(r"[0-9WUBRGCXYZwubrgcxyz]")
    mixed_mana_pattern = Combine(OneOrMore(curly_mana_symbol | simple_mana_symbol))

    def make_mana_value_node(tokens: list[str]) -> ManaValueNode:
        """Create a ManaValueNode for mana cost strings."""
        return ManaValueNode(tokens[0].upper())

    mana_value = mixed_mana_pattern.set_parse_action(make_mana_value_node)

    return {
        "mana_value": mana_value,
        "mixed_mana_pattern": mixed_mana_pattern,
    }


def create_color_parsers() -> dict[str, ParserElement]:
    """Create color-related parsing elements.

    Returns:
        Dictionary containing color parser elements
    """
    color_word = make_regex_pattern(COLOR_NAME_TO_CODE)
    color_letter_pattern = Regex(r"[wubrgcWUBRGC]+")
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
    quoted_string = basic_parsers["quoted_string"]
    string_value_word = basic_parsers["string_value_word"]
    literal_number = basic_parsers["literal_number"]
    arithmetic_op = basic_parsers["arithmetic_op"]
    lparen = basic_parsers["lparen"]
    rparen = basic_parsers["rparen"]
    mana_value = mana_parsers["mana_value"]
    color_value = color_parsers["color_value"]

    numeric_attr_word = create_attribute_parser(ParserClass.NUMERIC)
    mana_attr_word = create_attribute_parser(ParserClass.MANA)
    rarity_attr_word = create_attribute_parser(ParserClass.RARITY)
    legality_attr_word = create_attribute_parser(ParserClass.LEGALITY)
    color_attr_word = create_attribute_parser(ParserClass.COLOR)
    text_attr_word = create_attribute_parser(ParserClass.TEXT)
    date_attr_word = create_attribute_parser(ParserClass.DATE)
    year_attr_word = create_attribute_parser(ParserClass.YEAR)

    expr = Forward()

    paren_expr_term = lparen + expr + rparen
    arithmetic_term = numeric_attr_word | literal_number | paren_expr_term
    arithmetic_expr = Forward()
    arithmetic_expr <<= arithmetic_term + arithmetic_op + arithmetic_term + ZeroOrMore(arithmetic_op + arithmetic_term)
    arithmetic_expr.set_parse_action(make_chained_arithmetic)

    numeric_comparison_lhs = arithmetic_expr | paren_expr_term | numeric_attr_word | literal_number
    unified_numeric_comparison = numeric_comparison_lhs + DEFAULT_OPERATORS + numeric_comparison_lhs
    unified_numeric_comparison.set_parse_action(make_binary_operator_node)

    mana_value_or_string = mana_value | quoted_string | string_value_word
    mana_condition = create_condition_parser(mana_attr_word, mana_value_or_string)

    color_condition = create_condition_parser(color_attr_word, color_value | quoted_string)

    regex_pattern = basic_parsers["regex_pattern"]
    rarity_condition = create_condition_parser(rarity_attr_word, quoted_string | string_value_word)
    legality_condition = create_condition_parser(legality_attr_word, quoted_string | string_value_word)
    text_condition = create_condition_parser(text_attr_word, regex_pattern | quoted_string | string_value_word)

    date_value = Regex(r"\d{4}(?:-\d{2}-\d{2})?")
    date_condition = create_condition_parser(date_attr_word, date_value)

    year_value = Regex(r"\d{4}")
    year_condition = create_condition_parser(year_attr_word, year_value)

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
    attr_attr_condition.set_parse_action(make_binary_operator_node)

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

    hyphenated_condition = text_attr_word + Literal(":") + Regex(r"\w[\w-]*")
    hyphenated_condition.set_parse_action(make_binary_operator_node)

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

    result = create_value_node(tokens[0])
    for i in range(1, len(tokens), 2):
        if i + 1 < len(tokens):
            operator = tokens[i]
            right_term = create_value_node(tokens[i + 1])
            result = BinaryOperatorNode(result, operator, right_term)
    return result


@cachebox.cached(cache={})
def get_parse_expr() -> ParserElement:  # noqa: PLR0915
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

    Returns:
        ParserElement: The main expression parser that can parse complete
            Scryfall search queries into an Abstract Syntax Tree (AST).

    Note:
        This function is cached to avoid rebuilding the complex grammar
        on every call, improving performance for repeated parsing operations.
    """
    basic_parsers = create_basic_parsers()
    mana_parsers = create_mana_parsers()
    color_parsers = create_color_parsers()

    lparen = basic_parsers["lparen"]
    rparen = basic_parsers["rparen"]
    operator_and = basic_parsers["operator_and"]
    operator_or = basic_parsers["operator_or"]
    operator_not = basic_parsers["operator_not"]
    word = basic_parsers["word"]
    literal_number = basic_parsers["literal_number"]

    condition_parsers = create_all_condition_parsers(basic_parsers, mana_parsers, color_parsers)

    expr = condition_parsers["expr"]
    arithmetic_expr = condition_parsers["arithmetic_expr"]
    condition = condition_parsers["condition"]
    hyphenated_condition = condition_parsers["hyphenated_condition"]
    attr_attr_condition = condition_parsers["attr_attr_condition"]

    _word_for_exact = word.copy()
    _quoted_string_for_exact = basic_parsers["quoted_string"]
    exact_name_prefix = Literal("!").suppress()

    def make_exact_name(tokens: list[object]) -> ExactNameNode:
        """Create an ExactNameNode for exact card name matching."""
        token = tokens[0]
        value = token[1] if isinstance(token, tuple) and token[0] == "quoted" else str(token)
        return ExactNameNode(value)

    exact_name = exact_name_prefix + (_quoted_string_for_exact | _word_for_exact)
    exact_name.set_parse_action(make_exact_name)

    _implicit_name_value = basic_parsers["quoted_string"].copy() | word

    def make_implicit_name(tokens: list[object]) -> BinaryOperatorNode:
        token = tokens[0]
        value = token[1] if isinstance(token, tuple) and token[0] == "quoted" else str(token)
        return BinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode(value))

    implicit_name = _implicit_name_value.set_parse_action(make_implicit_name)

    def make_numeric_literal(tokens: list[object]) -> NumericValueNode:
        """Create a NumericValueNode for standalone numeric literals."""
        return NumericValueNode(tokens[0])

    standalone_numeric = literal_number.set_parse_action(make_numeric_literal)

    def make_group(tokens: list[object]) -> object:
        """Return the grouped expression inside parentheses."""
        return tokens[0]

    group = Group(lparen + expr + rparen).set_parse_action(make_group)

    def handle_negation(tokens: list[object]) -> object:
        """Handle negation (NOT) for factors, disallowing arithmetic negation."""
        if len(tokens) == 1:
            return tokens[0]
        if len(tokens) == NEGATION_TOKEN_COUNT and tokens[0] == "-":
            if isinstance(tokens[1], BinaryOperatorNode) and tokens[1].operator in ["+", "-", "*", "/"]:
                msg = f"Cannot negate arithmetic expressions like '{tokens[1]}'. Use parentheses if you want to negate the result of arithmetic."
                raise ValueError(msg)
            return NotNode(tokens[1])
        return tokens[0]

    negatable_primary = attr_attr_condition | condition | group | exact_name | implicit_name
    negatable_factor = Optional(operator_not) + negatable_primary
    negatable_factor.set_parse_action(handle_negation)

    factor = condition | hyphenated_condition | arithmetic_expr | negatable_factor | standalone_numeric

    def handle_and(tokens: list[object]) -> object:
        """Group AND operands into an AndNode (AND binds tighter than OR)."""
        items = list(tokens)
        if len(items) == 1:
            return items[0]
        return AndNode(items[0::2])

    def handle_or(tokens: list[object]) -> object:
        """Group OR operands into an OrNode."""
        items = list(tokens)
        if len(items) == 1:
            return items[0]
        return OrNode(items[0::2])

    and_expr = factor + ZeroOrMore(operator_and + factor)
    and_expr.set_parse_action(handle_and)

    expr <<= and_expr + ZeroOrMore(operator_or + and_expr)
    expr.set_parse_action(handle_or)
    return expr


def parse_search_query(query: str | None) -> Query:
    """Parse a search query string into a Query AST using the pyparsing grammar.

    Args:
        query: The search query string to parse. Can be None or empty.

    Returns:
        Query: A Query AST node containing the parsed query structure.
            For empty queries, returns a default query that is always true.

    Raises:
        ValueError: If parsing fails due to syntax errors or invalid operators.
    """
    original_query = query
    if query is None or not query.strip():
        return Query(TrueNode())

    query = preprocess_implicit_and(query)
    expr = get_parse_expr()

    try:
        parsed = expr.parse_string(query, parse_all=True)
        # parse => transform => rest, mirroring parse_scryfall_query so the derived-predicate
        # rewrite applies identically to both parsers (kept in lockstep by test_parser_parity).
        if parsed:
            return expand_derived_predicates(to_card_query_ast(flatten_nested_operations(Query(parsed[0]))))
        return expand_derived_predicates(to_card_query_ast(Query(BinaryOperatorNode("name", ":", ""))))
    except (ValueError, TypeError, IndexError) as e:
        msg = "main query parsing"
        raise create_parsing_error(msg, e, query) from e
    except ParseException as e:
        msg = f'Failed to parse query: "{original_query}"'
        raise ValueError(msg) from e


@cachebox.cached(cache={})
def _get_implicit_and_tokenizer() -> ParserElement:
    """Build a tokenizer for preprocess_implicit_and using the same primitives as the main grammar."""
    quoted_raw = (
        QuotedString('"', esc_char="\\", unquote_results=False) | QuotedString("'", esc_char="\\", unquote_results=False)
    ).set_parse_action(lambda t: t[0])

    regex_raw = QuotedString("/", esc_char="\\", unquote_results=False, convert_whitespace_escapes=False).set_parse_action(
        lambda t: t[0]
    )

    lparen_tok = Literal("(").set_parse_action(lambda t: t[0])
    rparen_tok = Literal(")").set_parse_action(lambda t: t[0])

    and_tok = CaselessKeyword("AND").set_parse_action(lambda t: t[0].upper())
    or_tok = CaselessKeyword("OR").set_parse_action(lambda t: t[0].upper())

    comparison_tok = one_of(">= <= != : = > <").set_parse_action(lambda t: t[0])
    arithmetic_tok = one_of("+ - * /").set_parse_action(lambda t: t[0])

    exact_name_tok = Literal("!").set_parse_action(lambda t: t[0])

    float_tok = Regex(r"\b\d+\.\d*\b").set_parse_action(lambda t: t[0])

    string_value_tok = Regex(r"\w([\w.-]*[\w.])?").set_parse_action(lambda t: t[0])

    curly_mana_symbol = Regex(r"\{[^}]+\}")
    simple_mana_symbol = Regex(r"[0-9WUBRGCXYZwubrgcxyz]")
    mana_tok = Combine(OneOrMore(curly_mana_symbol | simple_mana_symbol)).set_parse_action(lambda t: t[0])

    one_token = (
        quoted_raw
        | regex_raw
        | lparen_tok
        | rparen_tok
        | and_tok
        | or_tok
        | comparison_tok
        | arithmetic_tok
        | exact_name_tok
        | float_tok
        | string_value_tok
        | mana_tok
    )

    return Optional(OneOrMore(one_token)).set_parse_action(lambda t: t.asList() if t else [])


def _tokenize_for_implicit_and(query: str) -> list[str]:
    """Tokenize a query string for implicit AND preprocessing. Raises ValueError on invalid input."""
    if not query.strip():
        return []
    tokenizer = _get_implicit_and_tokenizer()
    try:
        result = tokenizer.parse_string(query, parse_all=True).asList()
    except ParseException as e:
        full_msg = str(e)
        msg_text = getattr(e, "msg", full_msg)
        col = getattr(e, "col", None)
        if isinstance(col, int) and 1 <= col <= len(query) and query[col - 1] in ('"', "'"):
            msg = "Unmatched quote or regex in query"
            raise ValueError(msg) from e
        location_suffix = f" at column {col}" if isinstance(col, int) else ""
        msg = f"Invalid query syntax{location_suffix}: {msg_text}"
        raise ValueError(msg) from e

    for idx, tok in enumerate(result):
        if tok != "/":
            continue
        prev_tok = result[idx - 1] if idx > 0 else None
        if idx == 0 or (not _is_numeric_operand(prev_tok) and prev_tok != ")"):
            msg = "Unmatched / in regex pattern in query"
            raise ValueError(msg)
    return result


def _is_implicit_and_operand(t: str) -> bool:
    """Return True if *t* counts as an operand for implicit-AND insertion."""
    if t in ("(", ")", "AND", "OR", "!"):
        return False
    return not is_operator(t)


def _is_numeric_operand(t: str) -> bool:
    """Return True if *t* is a numeric card attribute or a bare numeric literal."""
    return t.lower() in NUMERIC_CARD_ATTRIBUTES or bool(_NUMERIC_LITERAL_RE.match(t))


def _rhs_introduces_comparison(tokens: list[str], start_index: int) -> bool:
    """Return True if scanning forward from start_index hits a comparison operator before AND/OR/).

    Used to disambiguate binary subtraction on the RHS of a comparison.
    """
    depth = 0
    for j in range(start_index, len(tokens)):
        t = tokens[j]
        if t == "(":
            depth += 1
        elif t == ")":
            if depth == 0:
                return False
            depth -= 1
        elif t in _COMPARISON_OPERATORS and depth == 0:
            return True
        elif t in {"AND", "OR"} and depth == 0:
            return False
    return False


def preprocess_implicit_and(query: str) -> str:
    """Pre-process query to convert implicit AND operations to explicit ones.

    Tokenizes using the same grammar primitives as the main parser, inserts AND
    between consecutive operands (and around negation), then serializes with no
    extra whitespace (e.g. 'foo bar' -> 'foo AND bar', 'cmc=3' -> 'cmc=3').
    See api/parsing/tests/implicit_and_cases.py for cases and test_pyparsing_preprocess.py for the test runner.
    """
    if not any(c in _FP_UNSAFE_CHARS for c in query):
        parts = query.split()
        if parts and all(
            part.upper() in ("AND", "OR") or (part[0] not in _FP_TERM_START_OPS and part[-1] not in _FP_TERM_END_OPS)
            for part in parts
        ):
            out: list[str] = []
            for i, part in enumerate(parts):
                is_kw = part.upper() in ("AND", "OR")
                out.append(part.upper() if is_kw else part)
                if i + 1 < len(parts) and not is_kw and parts[i + 1].upper() not in ("AND", "OR"):
                    out.append("AND")
            return " ".join(out)

    tokens = _tokenize_for_implicit_and(query)
    if not tokens:
        return ""

    result: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        result.append(tok)

        if i + 1 >= len(tokens):
            i += 1
            continue

        next_tok = tokens[i + 1]

        after_operator = i > 0 and is_operator(tokens[i - 1])
        value_continuation = next_tok.startswith("{")
        skip_because_value = after_operator and value_continuation

        prev_is_comparison = i > 0 and tokens[i - 1] in _COMPARISON_OPERATORS
        is_arithmetic_minus = (
            next_tok == "-"
            and i + 2 < len(tokens)
            and (_is_numeric_operand(tok) or tok == ")")
            and (_is_numeric_operand(tokens[i + 2]) or tokens[i + 2] == "(")
            and not (prev_is_comparison and _rhs_introduces_comparison(tokens, i + 2))
        )

        need_and = (
            (_is_implicit_and_operand(tok) or tok == ")")
            and (_is_implicit_and_operand(next_tok) or next_tok in {"(", "-", "!"})
            and not skip_because_value
            and not is_arithmetic_minus
        )
        if need_and:
            result.append("AND")
        i += 1

    return "".join(f" {t} " if t in ("AND", "OR") else t for t in result)


def is_operator(token: str) -> bool:
    """Check if a token is an operator (comparison, arithmetic, or negation).

    Args:
        token: The token to check.

    Returns:
        True if the token is an operator, False otherwise.
    """
    return token in [":", ">", "<", ">=", "<=", "=", "!=", "-", "+", "*", "/"]
