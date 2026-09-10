"""Hand-written recursive descent parser for Scryfall query syntax.

Single-pass tokenizer + parser; handles implicit AND natively without a
separate preprocessing step.  Intended as a drop-in replacement for the
pyparsing-based parse_search_query pipeline.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum, auto

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode, ExactNameNode, is_valid_rarity
from api.parsing.colors import is_valid_color_value
from api.parsing.db_info import ALIAS_TO_FIELD_INFOS, ParserClass
from api.parsing.mana_symbols import first_invalid_mana_symbol
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
    regex_plain_literal,
)
from api.parsing.query_budget import MAX_GROUP_DEPTH, QueryBudgetExceeded
from api.parsing.spans import QUOTE_CHARS, brace_close_index, find_close_index, opens_quote, unescape

# ── Alias → parser-class lookup ──────────────────────────────────────────────

# Build once from db_info; prefer NUMERIC for dual-class aliases (cn, number)
# so bare integers route to the numeric branch, matching pyparsing behaviour.
_ALIAS_TO_PC: dict[str, ParserClass] = {}
for _alias, _fis in ALIAS_TO_FIELD_INFOS.items():
    _classes = {fi.parser_class for fi in _fis}
    _ALIAS_TO_PC[_alias.lower()] = ParserClass.NUMERIC if ParserClass.NUMERIC in _classes else next(iter(_classes))

# Aliases that have BOTH a NUMERIC and a TEXT mapping (only cn / number today).
# For these the value determines which branch wins: bare number → NUMERIC, else TEXT.
_DUAL_NUM_TEXT: frozenset[str] = frozenset(
    alias.lower()
    for alias, fis in ALIAS_TO_FIELD_INFOS.items()
    if any(fi.parser_class == ParserClass.NUMERIC for fi in fis) and any(fi.parser_class == ParserClass.TEXT for fi in fis)
)

_NUMERIC_ALIASES: frozenset[str] = frozenset(alias for alias, pc in _ALIAS_TO_PC.items() if pc == ParserClass.NUMERIC)

# Aliases whose field runs a `/regex/` as a regex (the free-text columns, flagged in db_info).
_REGEX_CAPABLE_ALIASES: frozenset[str] = frozenset(
    alias.lower() for alias, fis in ALIAS_TO_FIELD_INFOS.items() if any(fi.regex_capable for fi in fis)
)
REGEX_UNSUPPORTED_FIELD_MESSAGE = "regular expressions are only supported on name, oracle text, flavor text and artist"

# On Scryfall '!' is an alias for '=' on these classes only (verified live, #903 cause C) — on
# TEXT/LEGALITY it isn't an operator at all, and a trailing bang there falls through to the
# existing exact-name-prefix reading of the next factor instead.
_BANG_ALIAS_CLASSES: frozenset[ParserClass] = frozenset(
    {ParserClass.COLOR, ParserClass.MANA, ParserClass.RARITY, ParserClass.YEAR, ParserClass.DATE}
)

_MIN_MTG_YEAR: int = 1992
_MAX_YEAR: int = 2040
_MIN_FOUR_DIGIT_YEAR: int = 1000
_MAX_FOUR_DIGIT_YEAR: int = 9999
_EQUALITY_OPERATORS: frozenset[str] = frozenset({":", "="})


def validate_year(value: int | float, pos: int, operator: str) -> int:
    """Check a year value: four digits always, and for `=`/`:` one Magic could have a printing in.

    `year:1500` and `date=2099` cannot match anything, so equality gets the sanity gate. A comparison
    against any year is meaningful -- `year<1993` is "the first year", `date>=1990` is "everything",
    `year!=1500` too -- so `<`, `<=`, `>`, `>=` and `!=` only require the value to be a year at all:
    four digits, which is also the shape the SQL and engine date handling assume. Shared with the
    pyparsing oracle so both parsers draw the same line.
    """
    if isinstance(value, float):
        msg = f"Expected integer year, got {value!r} at position {pos}"
        raise ParseError(msg)
    if not (_MIN_FOUR_DIGIT_YEAR <= value <= _MAX_FOUR_DIGIT_YEAR):
        msg = f"Expected a four-digit year, got {value!r} at position {pos}"
        raise ParseError(msg)
    if operator in _EQUALITY_OPERATORS and not (_MIN_MTG_YEAR <= value <= _MAX_YEAR):
        msg = f"Year must be between {_MIN_MTG_YEAR} and {_MAX_YEAR}, got {value!r} at position {pos}"
        raise ParseError(msg)
    return value


def validate_date(year: int, month: int, day: int, pos: int) -> str:
    """Return the ISO form of a calendar date, or raise ParseError if it is not one (2020-02-30)."""
    try:
        datetime.date(year=year, month=month, day=day)
    except ValueError as exc:
        msg = f"Invalid date {year}-{month:02d}-{day:02d} at position {pos}: {exc}"
        raise ParseError(msg) from exc
    return f"{year}-{month:02d}-{day:02d}"


# ── Token types ───────────────────────────────────────────────────────────────


class TT(Enum):
    """Token type enum for the hand-written lexer."""

    WORD = auto()  # [a-zA-Z_][a-zA-Z0-9_.]*  (includes digits-then-letters like "2rr")
    NUMBER = auto()  # integer or float
    QUOTED = auto()  # "..." or '...'
    REGEX = auto()  # /pattern/
    MANA = auto()  # {W}, {2/R}, …
    OP = auto()  # : = != >= <= > <
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    LPAREN = auto()
    RPAREN = auto()
    BANG = auto()  # !  (exact-name prefix)
    EOF = auto()


@dataclass
class Token:
    """A single lexed token with its type, value, source position, whitespace flag, and source text.

    `raw` is the exact slice of the query the token came from. It differs from `value` where lexing
    normalises: a NUMBER's value is the parsed int/float, a QUOTED's is the unescaped content, a
    REGEX's is the pattern without its slashes. Anything that echoes a token back as *text* -- a bare
    expression becoming a name search -- reads `raw`, so the user's spelling survives.
    """

    type: TT
    value: str | int | float
    pos: int
    space_before: bool
    raw: str | None = None

    def __post_init__(self) -> None:
        """Default `raw` to the value's own text, which is exact for every token type but the three above."""
        if self.raw is None:
            self.raw = str(self.value)


_ARITH_OPS: frozenset[TT] = frozenset({TT.PLUS, TT.MINUS, TT.STAR, TT.SLASH})
_WORD_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
# An apostrophe continues a word (`can't`, `Urza's`); it opens a string only at token start, where
# spans.opens_quote says so -- the lexer never reaches this set for one of those.
_WORD_CONT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789.'")
_DIGIT = frozenset("0123456789")
_SPACE = frozenset(" \t\r\n")


def _is_word_start(c: str) -> bool:
    """ASCII identifier start, or any Unicode letter — accented card names, e.g. Éowyn (#649)."""
    return c in _WORD_START or c.isalpha()


def _is_word_cont(c: str) -> bool:
    """ASCII identifier continuation, or any Unicode letter (#649)."""
    return c in _WORD_CONT or c.isalpha()


# ── Lexer ─────────────────────────────────────────────────────────────────────


def _closed_quote(query: str, start: int, quote: str) -> tuple[int, str] | None:
    """Find the *quote* closing a string opened before *start* and unescape its content.

    Delegates the boundary walk to `spans.find_close_index` — the same walk the balancer uses — so
    the lexer and balancer can't drift on where an escaped quote ends (#905). `saw_escape` comes free
    from that same walk, so the common case (no backslash anywhere in the string) skips `unescape`'s
    regex pass entirely instead of running it over content that's already exactly what it should be.

    Returns (close_index, unescaped_content), or None if the string is unterminated.
    """
    close_index, _, saw_escape = find_close_index(query, start, quote)
    if close_index is None:
        return None
    content = query[start:close_index]
    return close_index, unescape(content) if saw_escape else content


def _closed_regex(query: str, start: int) -> tuple[int, str] | None:
    r"""Find the '/' closing a regex opened before *start*, unescaping only '\\/' -> '/'.

    Delegates the boundary walk to `spans.find_close_index`, the same walk the balancer uses, so the
    two can't drift on where an escaped '/' ends (#905). `saw_escape` comes free from that same walk,
    so a pattern with no backslash at all — the common case — skips the `.replace()` pass entirely.
    Every other backslash sequence (e.g. `\\d`) is left untouched for the regex engine to interpret —
    this only ever collapses an escaped slash, never a full general unescape.

    Returns (close_index, unescaped_content), or None if the pattern is unterminated.
    """
    close_index, _, saw_escape = find_close_index(query, start, "/")
    if close_index is None:
        return None
    content = query[start:close_index]
    return close_index, content.replace("\\/", "/") if saw_escape else content


class LexError(ValueError):
    """Raised when the lexer encounters an unexpected character or unclosed delimiter."""


def tokenize(src: str) -> list[Token]:  # noqa: C901, PLR0912, PLR0915
    """Lex a query string into a flat list of Tokens, terminated by an EOF token."""
    tokens: list[Token] = []
    pos = 0
    n = len(src)
    space_before = False

    while pos < n:
        if src[pos] in _SPACE:
            while pos < n and src[pos] in _SPACE:
                pos += 1
            space_before = True
            continue

        start = pos
        sb = space_before
        space_before = False
        c = src[pos]

        # {mana symbol}. brace_close_index is shared with the balancer in api.parsing.spans: both have
        # to agree that a '{...}' is opaque whatever it holds, or the balancer reads the ')' in
        # '(mana:{)})' as query structure and rejects a query the lexer accepts — #905's bug class,
        # with braces in place of quotes.
        if c == "{":
            close_index = brace_close_index(src, pos + 1)
            if close_index is None:
                msg = f"Unclosed '{{' at position {pos}"
                raise LexError(msg)
            pos = close_index + 1
            tokens.append(Token(TT.MANA, src[start:pos], start, sb))
            continue

        # Quoted string. Whether a quote opens one at all is `spans.opens_quote` -- a "'" mid-word is
        # an apostrophe, so `o:can't` is a word, not an unterminated string -- and the escape-skipping
        # walk is `spans.find_close_index`; the balancer reads both. Where the two disagree, the
        # balancer closes a quote the lexer never opened, or reads the ' in 'don\'t' as the close and
        # appends one the lexer never wanted (#905).
        if c in QUOTE_CHARS and opens_quote(src, pos):
            closed = _closed_quote(src, pos + 1, c)
            if closed is None:
                msg = f"Unclosed quote at position {start}"
                raise LexError(msg)
            close_index, content = closed
            pos = close_index + 1
            tokens.append(Token(TT.QUOTED, content, start, sb, raw=src[start:pos]))
            continue

        # Operators >= <= != : = > <  and  ! (bang)
        if c == ">":
            if pos + 1 < n and src[pos + 1] == "=":
                tokens.append(Token(TT.OP, ">=", start, sb))
                pos += 2
            else:
                tokens.append(Token(TT.OP, ">", start, sb))
                pos += 1
            continue
        if c == "<":
            if pos + 1 < n and src[pos + 1] == "=":
                tokens.append(Token(TT.OP, "<=", start, sb))
                pos += 2
            else:
                tokens.append(Token(TT.OP, "<", start, sb))
                pos += 1
            continue
        if c == "!":
            if pos + 1 < n and src[pos + 1] == "=":
                tokens.append(Token(TT.OP, "!=", start, sb))
                pos += 2
            else:
                tokens.append(Token(TT.BANG, "!", start, sb))
                pos += 1
            continue
        if c == ":":
            tokens.append(Token(TT.OP, ":", start, sb))
            pos += 1
            continue
        if c == "=":
            tokens.append(Token(TT.OP, "=", start, sb))
            pos += 1
            continue

        # Slash: a regex only opens in value position — directly after a comparison operator, which
        # is the only place the parser accepts one. Anywhere else '/' is arithmetic division.
        #
        # Without the guard the scan is greedy across the whole remaining query, so the division in
        # "power/2>1 name:/a/" swallows "2>1 name:" as a pattern and the query cannot parse at all
        # (#908). Value position is unambiguous: division needs a left operand, and the operator
        # just consumed that slot.
        if c == "/":
            prev = tokens[-1] if tokens else None
            in_value_position = prev is not None and prev.type == TT.OP
            # The escape-skipping walk here has to agree with the balancer's `spans.find_close_index`
            # on where the span ends, or one of them treats a quote as a delimiter that the other
            # treats as pattern content (#905).
            closed = _closed_regex(src, pos + 1) if in_value_position else None
            if closed is None:
                # Division, or an unterminated regex falling back to division.
                tokens.append(Token(TT.SLASH, "/", start, sb))
                pos += 1
            else:
                close_index, content = closed
                pos = close_index + 1
                tokens.append(Token(TT.REGEX, content, start, sb, raw=src[start:pos]))
            continue

        # Single-char arithmetic / grouping
        if c == "+":
            tokens.append(Token(TT.PLUS, "+", start, sb))
            pos += 1
            continue
        if c == "-":
            tokens.append(Token(TT.MINUS, "-", start, sb))
            pos += 1
            continue
        if c == "*":
            tokens.append(Token(TT.STAR, "*", start, sb))
            pos += 1
            continue
        if c == "(":
            tokens.append(Token(TT.LPAREN, "(", start, sb))
            pos += 1
            continue
        if c == ")":
            tokens.append(Token(TT.RPAREN, ")", start, sb))
            pos += 1
            continue

        # Number — if immediately followed by word chars, treat as WORD ("2rr", "40k-model" prefix)
        if c in _DIGIT:
            j = pos + 1
            while j < n and src[j] in _DIGIT:
                j += 1
            if j < n and src[j] == "." and j + 1 < n and src[j + 1] in _DIGIT:
                j += 1
                while j < n and src[j] in _DIGIT:
                    j += 1
            if j < n and _is_word_cont(src[j]):
                while j < n and _is_word_cont(src[j]):
                    j += 1
                tokens.append(Token(TT.WORD, src[pos:j], start, sb))
            elif "." in src[pos:j]:
                tokens.append(Token(TT.NUMBER, float(src[pos:j]), start, sb, raw=src[pos:j]))
            else:
                tokens.append(Token(TT.NUMBER, int(src[pos:j]), start, sb, raw=src[pos:j]))
            pos = j
            continue

        # Word
        if _is_word_start(c):
            j = pos + 1
            while j < n and _is_word_cont(src[j]):
                j += 1
            tokens.append(Token(TT.WORD, src[pos:j], start, sb))
            pos = j
            continue

        msg = f"Unexpected character {c!r} at position {pos}"
        raise LexError(msg)

    tokens.append(Token(TT.EOF, "", n, space_before))
    return tokens


# ── Parser ────────────────────────────────────────────────────────────────────


class ParseError(ValueError):
    """Raised when the parser encounters unexpected token structure."""


_ARITH_OPERATORS = frozenset("+-*/")


def _name_node(value: str) -> CardBinaryOperatorNode:
    return CardBinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode(value))


def _is_filter(node: QueryNode) -> bool:
    """False for a bare numeric expression: a literal, a numeric attribute, or arithmetic over them."""
    if isinstance(node, NumericValueNode | CardAttributeNode):
        return False
    return not (isinstance(node, BinaryOperatorNode) and node.operator in _ARITH_OPERATORS)


class Parser:
    """Recursive descent parser for Scryfall query syntax."""

    __slots__ = ("group_depth", "pos", "tokens")

    def __init__(self, tokens: list[Token]) -> None:
        """Initialise the parser with the token list produced by tokenize()."""
        self.tokens = tokens
        self.pos = 0
        self.group_depth = 0

    # ── token access ─────────────────────────────────────────────────────────

    def peek(self, offset: int = 0) -> Token:
        """Return the token at pos+offset without consuming it (clamps to EOF)."""
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else self.tokens[-1]

    def consume(self) -> Token:
        """Consume and return the current token."""
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, tt: TT) -> Token:
        """Consume the current token, raising ParseError if it isn't the expected type."""
        tok = self.consume()
        if tok.type != tt:
            msg = f"Expected {tt.name}, got {tok.value!r} at position {tok.pos}"
            raise ParseError(msg)
        return tok

    # ── top-level ─────────────────────────────────────────────────────────────

    def parse(self) -> Query:
        """Parse the full token stream into a Query AST."""
        if self.peek().type == TT.EOF:
            return Query(TrueNode())
        node = self.parse_expr()
        if self.peek().type != TT.EOF:
            msg = f"Unexpected {self.peek().value!r} at position {self.peek().pos}"
            raise ParseError(msg)
        return Query(node)

    # ── expr: OR-level ────────────────────────────────────────────────────────

    def parse_expr(self, *, bare_ok: bool = False) -> QueryNode:
        """Parse an OR-level expression.

        A factor that comes back as a bare numeric expression -- a literal, a numeric attribute, or
        arithmetic over them with no comparison (`1996`, `cmc+1`, `power - cmc`) -- is a name search
        for its source text, which is what Scryfall does with a bare number. Left as it was, it
        reached SQL as `WHERE %(p)s` and 400ed on a type error. The one exception is *bare_ok*: a
        parenthesised group whose whole content is one bare expression hands it back undecided,
        because the caller may still be using the group as an arithmetic operand (`(2*power)-1>3`).
        """
        disjuncts = [self._parse_conjuncts()]
        while self.peek().type == TT.WORD and self.peek().value.upper() == "OR":
            self.consume()
            disjuncts.append(self._parse_conjuncts())
        if bare_ok and len(disjuncts) == 1 and len(disjuncts[0]) == 1:
            return disjuncts[0][0][0]
        operands = [self._conjunction(conjuncts) for conjuncts in disjuncts]
        return operands[0] if len(operands) == 1 else OrNode(operands)

    # ── and_expr: AND-level with implicit AND ─────────────────────────────────

    def _parse_conjuncts(self) -> list[tuple[QueryNode, int, int]]:
        """Parse an AND-level run of factors (implicit AND between adjacent ones), each with its token span."""
        factors = [self._parse_spanned_factor()]
        while self._can_start_factor():
            if self.peek().type == TT.WORD and self.peek().value.upper() == "AND":
                self.consume()
            factors.append(self._parse_spanned_factor())
        return factors

    def _conjunction(self, factors: list[tuple[QueryNode, int, int]]) -> QueryNode:
        operands = [self._as_filter(node, start, end) for node, start, end in factors]
        return operands[0] if len(operands) == 1 else AndNode(operands)

    def _parse_spanned_factor(self) -> tuple[QueryNode, int, int]:
        start = self.pos
        node = self.parse_factor()
        return node, start, self.pos

    def _as_filter(self, node: QueryNode, start: int, end: int) -> QueryNode:
        """Return *node* unless it is a bare numeric expression, which becomes a name search for its text."""
        return node if _is_filter(node) else _name_node(self._span_text(start, end))

    def _span_text(self, start: int, end: int) -> str:
        """Source text of tokens[start:end], less the whitespace between tokens and any parentheses around the whole span.

        Dropping the whitespace makes `power - cmc` and `power-cmc` the same name search (the
        pyparsing oracle's preprocess already normalises them that way). Dropping enclosing
        parentheses makes `(2*power)` a search for `2*power`: the parentheses were grouping, not name.
        """
        while end - start >= 2 and self._parens_enclose(start, end):  # noqa: PLR2004
            start, end = start + 1, end - 1
        return "".join(tok.raw for tok in self.tokens[start:end])

    def _parens_enclose(self, start: int, end: int) -> bool:
        """True if tokens[start] is a '(' whose matching ')' is tokens[end - 1]."""
        if self.tokens[start].type is not TT.LPAREN or self.tokens[end - 1].type is not TT.RPAREN:
            return False
        depth = 0
        for tok in self.tokens[start : end - 1]:
            if tok.type is TT.LPAREN:
                depth += 1
            elif tok.type is TT.RPAREN:
                depth -= 1
                if depth == 0:
                    return False  # the opener closed early: `(2*power)-(1)`, two groups
        return True

    def _can_start_factor(self) -> bool:
        tok = self.peek()
        if tok.type in (TT.EOF, TT.RPAREN):
            return False
        if tok.type == TT.WORD:
            return tok.value.upper() != "OR"  # AND is consumed inline; OR ends the and_expr
        if tok.type == TT.MINUS:
            return tok.space_before  # space before - = negation prefix; no-space = trailing arith
        return tok.type in (TT.NUMBER, TT.QUOTED, TT.REGEX, TT.MANA, TT.LPAREN, TT.BANG)

    # ── factor: optional negation ─────────────────────────────────────────────

    def parse_factor(self) -> QueryNode:
        """Parse an optionally-negated primary expression.

        Negation is always a filter position, so a negated bare literal is resolved here rather than
        at the expr level: `cmc>2 -1` negates a name search for "1", not the integer 1. Unparenthesised
        arithmetic stays an error (`-cmc+1` reads as "negative cmc, plus one"), while a parenthesised
        group is a factor like any other and becomes a name search: `-(2*power)`.
        """
        if self.peek().type == TT.MINUS:
            self.consume()
            start = self.pos
            operand = self.parse_primary()
            if (
                isinstance(operand, BinaryOperatorNode)
                and operand.operator in _ARITH_OPERATORS
                and not self._parens_enclose(start, self.pos)
            ):
                msg = "Cannot negate an arithmetic expression"
                raise ParseError(msg)
            return NotNode(self._as_filter(operand, start, self.pos))
        return self.parse_primary()

    # ── primary ───────────────────────────────────────────────────────────────

    def parse_primary(self) -> QueryNode:
        """Parse a primary expression: group, exact-name, quoted string, word, number, or mana."""
        tok = self.peek()
        if tok.type == TT.LPAREN:
            lhs = self.parse_group()
            if self.peek().type in _ARITH_OPS and not self.peek().space_before:
                lhs = self._arith_tail(lhs)
            lhs = self._spaced_arith_tail(lhs)
            if self.peek().type == TT.OP:
                op = self.consume().value
                return CardBinaryOperatorNode(lhs, op, self.parse_num_expr_value())
            return lhs
        if tok.type == TT.BANG:
            return self.parse_exact_name()
        if tok.type == TT.QUOTED:
            self.consume()
            return _name_node(str(tok.value))
        if tok.type == TT.WORD:
            self.consume()
            return self.parse_word_primary(str(tok.value))
        if tok.type == TT.NUMBER:
            return self.parse_number_primary()
        if tok.type == TT.MANA:
            # bare mana outside attribute context — treat as implicit name
            self.consume()
            return _name_node(str(tok.value))
        msg = f"Unexpected {tok.value!r} at position {tok.pos}"
        raise ParseError(msg)

    def parse_group(self) -> QueryNode:
        """Parse a parenthesised sub-expression."""
        if self.group_depth >= MAX_GROUP_DEPTH:
            raise QueryBudgetExceeded(kind="depth")
        self.group_depth += 1
        try:
            self.consume()  # LPAREN
            if self.peek().type == TT.RPAREN:
                msg = "Empty parentheses are not allowed"
                raise ParseError(msg)
            inner = self.parse_expr(bare_ok=True)
            self.expect(TT.RPAREN)
            return inner
        finally:
            self.group_depth -= 1

    def parse_exact_name(self) -> QueryNode:
        """Parse an exact-name expression: !word or !"quoted string"."""
        self.consume()  # BANG
        tok = self.peek()
        if tok.type == TT.QUOTED:
            self.consume()
            return ExactNameNode(str(tok.value))
        if tok.type == TT.WORD:
            self.consume()
            return ExactNameNode(str(tok.value))
        msg = f"Expected word or quoted string after '!' at position {tok.pos}"
        raise ParseError(msg)

    # ── word dispatch ─────────────────────────────────────────────────────────

    def parse_word_primary(self, word: str) -> QueryNode:
        """Dispatch on whether word is a known attribute alias, keyword, or implicit name."""
        wl = word.lower()
        if wl in ("and", "or"):
            msg = f"Unexpected keyword {word!r}"
            raise ParseError(msg)

        pc = _ALIAS_TO_PC.get(wl)
        next_tok = self.peek()

        # ── dual-class alias (cn / number): dispatch on value shape ──
        if wl in _DUAL_NUM_TEXT and next_tok.type == TT.OP:
            op = self.consume().value
            if self._value_starts_number():
                return CardBinaryOperatorNode(CardAttributeNode(wl, ParserClass.NUMERIC), op, self.parse_num_expr_value())
            return CardBinaryOperatorNode(CardAttributeNode(wl, ParserClass.TEXT), op, self.parse_text_value(wl))

        # ── NUMERIC attribute ──
        if pc == ParserClass.NUMERIC:
            if next_tok.type in (TT.OP, TT.BANG):
                op = "=" if next_tok.type == TT.BANG else next_tok.value
                self.consume()
                return CardBinaryOperatorNode(CardAttributeNode(wl, ParserClass.NUMERIC), op, self.parse_num_expr_value())
            lhs: QueryNode = CardAttributeNode(wl, ParserClass.NUMERIC)
            if next_tok.type in _ARITH_OPS and not next_tok.space_before:
                lhs = self._arith_tail(lhs)
                if isinstance(lhs, CardAttributeNode):
                    # The operator had no numeric term after it, so this is not arithmetic at all:
                    # `pow-wow`, `power-plant`, `mv-x` are hyphenated bare words, read exactly as they
                    # would be if their first half were not an alias. Returning the bare attribute
                    # here used to leave the '-' unconsumed and fail the whole query.
                    return self.parse_hyphenated_name(word)
            lhs = self._spaced_arith_tail(lhs)
            if self.peek().type == TT.OP:
                op = self.consume().value
                return CardBinaryOperatorNode(lhs, op, self.parse_num_expr_value())
            # A bare attribute (`power`) or arithmetic expression (`cmc-power`): parse_expr turns it
            # into a name search unless a parenthesised group is using it as an arithmetic operand.
            return lhs

        # ── known non-NUMERIC attribute ──
        bang_alias = pc is not None and next_tok.type == TT.BANG and pc in _BANG_ALIAS_CLASSES
        if pc is not None and (next_tok.type == TT.OP or bang_alias):
            op = "=" if bang_alias else next_tok.value
            self.consume()
            return CardBinaryOperatorNode(CardAttributeNode(wl, pc), op, self.parse_value_for_class(pc, wl, op))
        if pc is not None:
            # alias recognised but no operator → might still be a hyphenated bare word (e.g. "a-b-c")
            return self.parse_hyphenated_name(word)

        # ── unknown alias → implicit name, possibly hyphenated ──
        return self.parse_hyphenated_name(word)

    def parse_number_primary(self) -> QueryNode:
        """Parse a bare numeric literal, optionally followed by an arithmetic tail and comparison.

        With no comparison the result is a bare numeric expression; parse_expr turns it into a name
        search for its source text (`1996`, `1+1`) unless a group is using it as an operand.
        """
        tok = self.consume()  # NUMBER
        lhs: QueryNode = NumericValueNode(tok.value)
        if self.peek().type in _ARITH_OPS and not self.peek().space_before and self._num_term_start(self.peek(1)):
            lhs = self._arith_tail(lhs)
        lhs = self._spaced_arith_tail(lhs)
        if self.peek().type == TT.OP:
            op = self.consume().value
            return CardBinaryOperatorNode(lhs, op, self.parse_num_expr_value())
        return lhs

    # ── arithmetic helpers ────────────────────────────────────────────────────

    def _spaced_arith_tail(self, lhs: QueryNode) -> QueryNode:
        """Consume spaced arithmetic operators (e.g. 'power - cmc', 'power + 1').

        For MINUS: requires space before the following operand too, distinguishing
        'power - cmc' (arithmetic) from 'power -cmc' (negation of next factor).
        For +, *, /: no such requirement — they have no negation ambiguity.
        """
        while True:
            tok = self.peek()
            if tok.type not in _ARITH_OPS or not tok.space_before:
                break
            if tok.type == TT.MINUS and not self.peek(1).space_before:
                break
            if not self._num_term_start(self.peek(1)):
                break
            op = self.consume().value
            lhs = CardBinaryOperatorNode(lhs, op, self.parse_num_term())
        return lhs

    def _num_term_start(self, tok: Token) -> bool:
        return tok.type in (TT.NUMBER, TT.LPAREN) or (tok.type == TT.WORD and tok.value.lower() in _NUMERIC_ALIASES)

    def _arith_tail(self, lhs: QueryNode) -> QueryNode:
        """Consume arith ops (no preceding space) and terms to build arithmetic AST."""
        while True:
            tok = self.peek()
            if tok.type not in _ARITH_OPS or tok.space_before:
                break
            if not self._num_term_start(self.peek(1)):
                break
            op = self.consume().value
            lhs = CardBinaryOperatorNode(lhs, op, self.parse_num_term())
        return lhs

    def parse_num_term(self) -> QueryNode:
        """Parse a single numeric term: a literal, a numeric attribute name, or a grouped expr."""
        tok = self.peek()
        if tok.type == TT.NUMBER:
            self.consume()
            return NumericValueNode(tok.value)
        if tok.type == TT.WORD and tok.value.lower() in _NUMERIC_ALIASES:
            self.consume()
            return CardAttributeNode(tok.value.lower(), ParserClass.NUMERIC)
        if tok.type == TT.LPAREN:
            return self.parse_group()
        msg = f"Expected numeric term, got {tok.value!r} at position {tok.pos}"
        raise ParseError(msg)

    def _value_starts_number(self) -> bool:
        """Return True if the value about to be parsed opens with a numeric literal, signed or not."""
        if self.peek().type == TT.NUMBER:
            return True
        return self.peek().type == TT.MINUS and self.peek(1).type == TT.NUMBER

    def parse_signed_num_term(self) -> QueryNode:
        """Parse a numeric term that may carry a leading '-' sign (the -1 in 'power>-1').

        Only the leading term of a value expression may be signed. A '-' there has no competing
        reading — filter negation and binary subtraction both require a preceding operand, and a
        comparison operator has just consumed that position — so no spacing rule is needed to
        disambiguate it, unlike the '-' handling in _spaced_arith_tail.
        """
        if self.peek().type == TT.MINUS and self.peek(1).type == TT.NUMBER:
            self.consume()  # MINUS
            tok = self.consume()  # NUMBER
            return NumericValueNode(-tok.value)
        return self.parse_num_term()

    def parse_num_expr_value(self) -> QueryNode:
        """Numeric expression in value context (spaces around arith ops are OK)."""
        lhs = self.parse_signed_num_term()
        while self.peek().type in _ARITH_OPS and self._num_term_start(self.peek(1)):
            tok = self.peek()
            if tok.type == TT.MINUS and tok.space_before and not self.peek(1).space_before:
                break
            op = self.consume().value
            lhs = CardBinaryOperatorNode(lhs, op, self.parse_num_term())
        return lhs

    # ── implicit name (possibly hyphenated) ───────────────────────────────────

    def parse_hyphenated_name(self, first: str) -> CardBinaryOperatorNode:
        """Build an implicit name node, greedily consuming no-space MINUS+WORD/NUMBER continuations.

        Continuations are read as source text (`raw`), so `x-007` stays `x-007`: a NUMBER's parsed
        value would have made it `x-7`.
        """
        parts = [first]
        while (
            self.peek().type == TT.MINUS
            and not self.peek().space_before
            and self.peek(1).type in (TT.WORD, TT.NUMBER)
            and not self.peek(1).space_before
        ):
            self.consume()  # MINUS
            parts.append(self.consume().raw)
        return _name_node("-".join(parts))

    # ── value parsers ─────────────────────────────────────────────────────────

    def parse_value_for_class(self, pc: ParserClass, attr: str, operator: str) -> QueryNode:
        """Route to the correct value parser based on the attribute's parser class."""
        if pc == ParserClass.TEXT:
            return self.parse_text_value(attr)
        if pc == ParserClass.NUMERIC:
            return self.parse_num_expr_value()
        if pc == ParserClass.COLOR:
            return self.parse_color_value()
        if pc == ParserClass.MANA:
            return self.parse_mana_value()
        if pc == ParserClass.RARITY:
            return self.parse_rarity_value()
        if pc == ParserClass.LEGALITY:
            return self.parse_string_value()
        if pc == ParserClass.DATE:
            return self.parse_date_value(operator)
        if pc == ParserClass.YEAR:
            return self.parse_year_value(operator)
        msg = f"Unknown parser class {pc!r}"
        raise ParseError(msg)

    def parse_text_value(self, attr: str) -> QueryNode:
        """Parse a text value: quoted string, regex, or bare word (with hyphenated continuation)."""
        tok = self.peek()
        if tok.type == TT.QUOTED:
            self.consume()
            return StringValueNode(str(tok.value))
        if tok.type == TT.REGEX:
            self.consume()
            pattern = str(tok.value)
            if attr in _REGEX_CAPABLE_ALIASES:
                return RegexValueNode(pattern)
            # Only the free-text columns run a regex. Elsewhere a `/.../` used to be taken as the
            # literal string it spelled, so `t:/elf|goblin/` matched nothing and said nothing. A
            # pattern that IS a plain literal means the literal and works as one (`kw:/flying/`);
            # anything with live metacharacters cannot be honoured and is an error worth reporting.
            literal = regex_plain_literal(pattern)
            if literal is None:
                raise ParseError(REGEX_UNSUPPORTED_FIELD_MESSAGE)
            return StringValueNode(literal)
        if tok.type in (TT.WORD, TT.NUMBER):
            self.consume()
            # A number in text position is text: `set:001` means "001", `o:1.50` means "1.50". The
            # token's parsed value would have made them "1" and "1.5".
            word = tok.raw
            # Greedily consume hyphenated continuation (no space on either side)
            while (
                self.peek().type == TT.MINUS
                and not self.peek().space_before
                and self.peek(1).type in (TT.WORD, TT.NUMBER)
                and not self.peek(1).space_before
            ):
                self.consume()
                word += "-" + self.consume().raw
            return StringValueNode(word)
        msg = f"Expected value for {attr!r}, got {tok.value!r} at position {tok.pos}"
        raise ParseError(msg)

    def parse_mana_value(self) -> QueryNode:
        """Parse a mana cost value: a sequence of mana symbols, words, or numbers (no gaps)."""
        tok = self.peek()
        if tok.type == TT.QUOTED:
            self.consume()
            value = str(tok.value).upper()
        else:
            parts: list[str] = []
            while True:
                t = self.peek()
                if t.type == TT.MANA or t.type in (TT.WORD, TT.NUMBER):
                    if parts and t.space_before:
                        break
                    self.consume()
                    parts.append(t.raw)
                else:
                    break
            if not parts:
                msg = f"Expected mana value at position {self.peek().pos}"
                raise ParseError(msg)
            value = "".join(parts).upper()
        # A mana cost can only hold certain symbols, so anything else is a query that cannot match.
        # '{Q}' is a real symbol (untap) but never appears in a cost, which is why this asks what a
        # cost may contain rather than what Magic prints. Quoting a value is just an alternate way to
        # type it (e.g. to protect spaces), not an opt-out of this check — a quoted 'mana:"q"' used to
        # skip straight to StringValueNode, so it silently matched every card via an empty cost dict.
        invalid = first_invalid_mana_symbol(value)
        if invalid is not None:
            msg = f"Invalid mana symbol {invalid!r} at position {tok.pos}"
            raise ParseError(msg)
        return ManaValueNode(value)

    def parse_string_value(self) -> QueryNode:
        """Parse a simple string value: quoted string or bare word."""
        tok = self.peek()
        if tok.type == TT.QUOTED:
            self.consume()
            return StringValueNode(str(tok.value))
        if tok.type == TT.WORD:
            self.consume()
            return StringValueNode(str(tok.value))
        msg = f"Expected string value, got {tok.value!r} at position {tok.pos}"
        raise ParseError(msg)

    def parse_rarity_value(self) -> QueryNode:
        """Parse a rarity value and check it names a rarity.

        Validated here, like mana and colour, rather than at SQL generation: an unknown rarity used to
        parse and then fail inside the query engine, logging an engine-failure traceback for a typo.
        """
        tok = self.peek()
        node = self.parse_string_value()
        if not is_valid_rarity(node.value):
            msg = f"Invalid rarity {node.value!r} at position {tok.pos}"
            raise ParseError(msg)
        return node

    def parse_color_value(self) -> QueryNode:
        """Parse a color value: a recognized color name or a combination of color letters.

        Quoted values get the same vocabulary check as bare ones -- quoting is another way to type
        the value, not an opt-out, and `c:"xyz"` used to reach the query engine before failing.
        """
        tok = self.peek()
        if tok.type == TT.QUOTED:
            self.consume()
            if not is_valid_color_value(str(tok.value)):
                msg = f"Invalid color value {tok.value!r} at position {tok.pos}"
                raise ParseError(msg)
            return StringValueNode(str(tok.value))
        if tok.type == TT.WORD:
            self.consume()
            val = str(tok.value)
            # The four-colour names are HYPHENATED (`yore-tiller`, `witch-maw`), and `-` is not a
            # word-continuation character, so the lexer hands this WORD MINUS WORD. Same greedy
            # hyphen-continuation rule parse_text_value uses (no space on either side) -- glue
            # first, validate once at the end, rather than checking membership before consuming.
            # WORD-only (not NUMBER, unlike parse_text_value's continuation): a color value is
            # never numeric, so `c:w-1` still stops at `w` and leaves `-1` for whatever comes next.
            while (
                self.peek().type == TT.MINUS
                and not self.peek().space_before
                and self.peek(1).type == TT.WORD
                and not self.peek(1).space_before
            ):
                self.consume()
                val += "-" + str(self.consume().value)
            if not is_valid_color_value(val):
                msg = f"Invalid color value {val!r} at position {tok.pos}"
                raise ParseError(msg)
            return StringValueNode(val)
        msg = f"Expected color value, got {tok.value!r} at position {tok.pos}"
        raise ParseError(msg)

    def _hyphen_number_follows(self) -> bool:
        """True if the next tokens are '-' NUMBER with no space on either side of the '-'."""
        return (
            self.peek().type == TT.MINUS
            and not self.peek().space_before
            and self.peek(1).type == TT.NUMBER
            and not self.peek(1).space_before
        )

    def parse_date_value(self, operator: str) -> QueryNode:
        """Parse a date value: YYYY or YYYY-MM-DD (hyphens must have no surrounding spaces).

        Anything between the two -- `date:2020-01` -- is an error: it used to consume the month and
        then fall through to the bare year, silently searching for something other than what was
        typed. A float where a month or day should be (`2020-1.5-01`) is an error for the same reason;
        `int()` would have truncated it.
        """
        tok = self.peek()
        if tok.type != TT.NUMBER:
            msg = f"Expected date, got {tok.value!r} at position {tok.pos}"
            raise ParseError(msg)
        self.consume()
        year = validate_year(tok.value, tok.pos, operator)
        if not self._hyphen_number_follows():
            return StringValueNode(str(year))
        self.consume()  # MINUS
        month_tok = self.consume()
        if not self._hyphen_number_follows():
            msg = f"Expected a full date YYYY-MM-DD, got {year}-{month_tok.raw} at position {tok.pos}"
            raise ParseError(msg)
        self.consume()  # MINUS
        day_tok = self.consume()
        if isinstance(month_tok.value, float) or isinstance(day_tok.value, float):
            msg = f"Expected integer month and day, got {year}-{month_tok.raw}-{day_tok.raw} at position {tok.pos}"
            raise ParseError(msg)
        return StringValueNode(validate_date(year, month_tok.value, day_tok.value, tok.pos))

    def parse_year_value(self, operator: str) -> QueryNode:
        """Parse a year value: a four-digit integer (see validate_year for the per-operator gate)."""
        tok = self.peek()
        if tok.type != TT.NUMBER:
            msg = f"Expected year, got {tok.value!r} at position {tok.pos}"
            raise ParseError(msg)
        self.consume()
        year = validate_year(tok.value, tok.pos, operator)
        return StringValueNode(str(year))


# ── entry point ───────────────────────────────────────────────────────────────


def parse_str_to_query(src: str | None) -> Query:
    """Parse a query string into a Query AST (lex + parse + flatten only)."""
    if not src or not src.strip():
        return Query(TrueNode())
    try:
        tokens = tokenize(src)
    except LexError as exc:
        msg = f'Failed to lex query: "{src}"'
        raise ValueError(msg) from exc
    except QueryBudgetExceeded:
        raise
    try:
        result = Parser(tokens).parse()
    except QueryBudgetExceeded:
        raise
    except ParseError as exc:
        msg = f'Failed to parse query: "{src}"'
        raise ValueError(msg) from exc
    return flatten_nested_operations(result)
