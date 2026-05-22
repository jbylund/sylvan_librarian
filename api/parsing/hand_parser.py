"""Hand-written recursive descent parser for Scryfall query syntax.

Single-pass tokenizer + parser; handles implicit AND natively without a
separate preprocessing step.  Intended as a drop-in replacement for the
pyparsing-based parse_search_query pipeline.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum, auto

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode, ExactNameNode
from api.parsing.db_info import ALIAS_TO_FIELD_INFOS, COLOR_NAME_TO_CODE, ParserClass
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

_VALID_COLOR_NAMES: frozenset[str] = frozenset(COLOR_NAME_TO_CODE)
_COLOR_LETTERS: frozenset[str] = frozenset("wubrgcWUBRGC")
_MIN_MTG_YEAR: int = 1992


def _validate_mtg_year(year: int, pos: int) -> None:
    if year < _MIN_MTG_YEAR:
        msg = f"Year must be {_MIN_MTG_YEAR} or later, got {year!r} at position {pos}"
        raise ParseError(msg)


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
    """A single lexed token with its type, value, source position, and whitespace flag."""

    type: TT
    value: str | int | float
    pos: int
    space_before: bool


_ARITH_OPS: frozenset[TT] = frozenset({TT.PLUS, TT.MINUS, TT.STAR, TT.SLASH})
_WORD_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_WORD_CONT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789.")
_DIGIT = frozenset("0123456789")
_SPACE = frozenset(" \t\r\n")


# ── Lexer ─────────────────────────────────────────────────────────────────────


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

        # {mana symbol}
        if c == "{":
            end = src.find("}", pos + 1)
            if end == -1:
                msg = f"Unclosed '{{' at position {pos}"
                raise LexError(msg)
            pos = end + 1
            tokens.append(Token(TT.MANA, src[start:pos], start, sb))
            continue

        # Quoted string
        if c in ('"', "'"):
            quote = c
            pos += 1
            chars: list[str] = []
            while pos < n:
                ch = src[pos]
                if ch == "\\" and pos + 1 < n:
                    pos += 1
                    chars.append(src[pos])
                    pos += 1
                elif ch == quote:
                    pos += 1
                    break
                else:
                    chars.append(ch)
                    pos += 1
            else:
                msg = f"Unclosed quote at position {start}"
                raise LexError(msg)
            tokens.append(Token(TT.QUOTED, "".join(chars), start, sb))
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

        # Slash: greedily try /regex/, fall back to arithmetic SLASH
        if c == "/":
            i = pos + 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    i += 2
                elif src[i] == "/":
                    pattern = src[pos + 1 : i].replace("\\/", "/")
                    pos = i + 1
                    tokens.append(Token(TT.REGEX, pattern, start, sb))
                    break
                else:
                    i += 1
            else:
                tokens.append(Token(TT.SLASH, "/", start, sb))
                pos += 1
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
            if j < n and src[j] in _WORD_CONT:
                while j < n and src[j] in _WORD_CONT:
                    j += 1
                tokens.append(Token(TT.WORD, src[pos:j], start, sb))
            elif "." in src[pos:j]:
                tokens.append(Token(TT.NUMBER, float(src[pos:j]), start, sb))
            else:
                tokens.append(Token(TT.NUMBER, int(src[pos:j]), start, sb))
            pos = j
            continue

        # Word
        if c in _WORD_START:
            j = pos + 1
            while j < n and src[j] in _WORD_CONT:
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


def _name_node(value: str) -> CardBinaryOperatorNode:
    return CardBinaryOperatorNode(CardAttributeNode("name", ParserClass.TEXT), ":", StringValueNode(value))


class Parser:
    """Recursive descent parser for Scryfall query syntax."""

    __slots__ = ("pos", "tokens")

    def __init__(self, tokens: list[Token]) -> None:
        """Initialise the parser with the token list produced by tokenize()."""
        self.tokens = tokens
        self.pos = 0

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

    def parse_expr(self) -> QueryNode:
        """Parse an OR-level expression."""
        operands = [self.parse_and_expr()]
        while self.peek().type == TT.WORD and self.peek().value.upper() == "OR":
            self.consume()
            operands.append(self.parse_and_expr())
        return operands[0] if len(operands) == 1 else OrNode(operands)

    # ── and_expr: AND-level with implicit AND ─────────────────────────────────

    def parse_and_expr(self) -> QueryNode:
        """Parse an AND-level expression, inserting implicit AND between adjacent factors."""
        operands = [self.parse_factor()]
        while self._can_start_factor():
            if self.peek().type == TT.WORD and self.peek().value.upper() == "AND":
                self.consume()
            operands.append(self.parse_factor())
        return operands[0] if len(operands) == 1 else AndNode(operands)

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
        """Parse an optionally-negated primary expression."""
        if self.peek().type == TT.MINUS:
            self.consume()
            operand = self.parse_primary()
            if isinstance(operand, BinaryOperatorNode) and operand.operator in ("+", "-", "*", "/"):
                msg = "Cannot negate an arithmetic expression"
                raise ParseError(msg)
            return NotNode(operand)
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
        self.consume()  # LPAREN
        if self.peek().type == TT.RPAREN:
            msg = "Empty parentheses are not allowed"
            raise ParseError(msg)
        inner = self.parse_expr()
        self.expect(TT.RPAREN)
        return inner

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
            if self.peek().type == TT.NUMBER:
                return CardBinaryOperatorNode(CardAttributeNode(wl, ParserClass.NUMERIC), op, self.parse_num_expr_value())
            return CardBinaryOperatorNode(CardAttributeNode(wl, ParserClass.TEXT), op, self.parse_text_value(wl))

        # ── NUMERIC attribute ──
        if pc == ParserClass.NUMERIC:
            if next_tok.type == TT.OP:
                op = self.consume().value
                return CardBinaryOperatorNode(CardAttributeNode(wl, ParserClass.NUMERIC), op, self.parse_num_expr_value())
            if next_tok.type in _ARITH_OPS and not next_tok.space_before:
                lhs = self._arith_tail(CardAttributeNode(wl, ParserClass.NUMERIC))
                lhs = self._spaced_arith_tail(lhs)
                if self.peek().type == TT.OP:
                    op = self.consume().value
                    return CardBinaryOperatorNode(lhs, op, self.parse_num_expr_value())
                return lhs  # standalone arith expression (e.g. cmc-power)
            lhs = self._spaced_arith_tail(CardAttributeNode(wl, ParserClass.NUMERIC))
            if isinstance(lhs, CardAttributeNode):
                # no arithmetic consumed → implicit name
                return _name_node(word)
            if self.peek().type == TT.OP:
                op = self.consume().value
                return CardBinaryOperatorNode(lhs, op, self.parse_num_expr_value())
            return lhs

        # ── known non-NUMERIC attribute ──
        if pc is not None and next_tok.type == TT.OP:
            op = self.consume().value
            return CardBinaryOperatorNode(CardAttributeNode(wl, pc), op, self.parse_value_for_class(pc, wl))
        if pc is not None:
            # alias recognised but no operator → might still be a hyphenated bare word (e.g. "a-b-c")
            return self.parse_hyphenated_name(word)

        # ── unknown alias → implicit name, possibly hyphenated ──
        return self.parse_hyphenated_name(word)

    def parse_number_primary(self) -> QueryNode:
        """Parse a bare numeric literal, optionally followed by an arithmetic tail and comparison."""
        tok = self.consume()  # NUMBER
        lhs: QueryNode = NumericValueNode(tok.value)
        if self.peek().type in _ARITH_OPS and not self.peek().space_before and self._num_term_start(self.peek(1)):
            lhs = self._arith_tail(lhs)
        lhs = self._spaced_arith_tail(lhs)
        if self.peek().type == TT.OP:
            op = self.consume().value
            return CardBinaryOperatorNode(lhs, op, self.parse_num_expr_value())
        return lhs  # standalone numeric literal

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

    def parse_num_expr_value(self) -> QueryNode:
        """Numeric expression in value context (spaces around arith ops are OK)."""
        lhs = self.parse_num_term()
        while self.peek().type in _ARITH_OPS and self._num_term_start(self.peek(1)):
            tok = self.peek()
            if tok.type == TT.MINUS and tok.space_before and not self.peek(1).space_before:
                break
            op = self.consume().value
            lhs = CardBinaryOperatorNode(lhs, op, self.parse_num_term())
        return lhs

    # ── implicit name (possibly hyphenated) ───────────────────────────────────

    def parse_hyphenated_name(self, first: str) -> CardBinaryOperatorNode:
        """Build an implicit name node, greedily consuming no-space MINUS+WORD/NUMBER continuations."""
        parts = [first]
        while (
            self.peek().type == TT.MINUS
            and not self.peek().space_before
            and self.peek(1).type in (TT.WORD, TT.NUMBER)
            and not self.peek(1).space_before
        ):
            self.consume()  # MINUS
            parts.append(str(self.consume().value))
        return _name_node("-".join(parts))

    # ── value parsers ─────────────────────────────────────────────────────────

    def parse_value_for_class(self, pc: ParserClass, attr: str) -> QueryNode:
        """Route to the correct value parser based on the attribute's parser class."""
        if pc == ParserClass.TEXT:
            return self.parse_text_value(attr)
        if pc == ParserClass.NUMERIC:
            return self.parse_num_expr_value()
        if pc == ParserClass.COLOR:
            return self.parse_color_value()
        if pc == ParserClass.MANA:
            return self.parse_mana_value()
        if pc in (ParserClass.RARITY, ParserClass.LEGALITY):
            return self.parse_string_value()
        if pc == ParserClass.DATE:
            return self.parse_date_value()
        if pc == ParserClass.YEAR:
            return self.parse_year_value()
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
            return RegexValueNode(str(tok.value))
        if tok.type in (TT.WORD, TT.NUMBER):
            self.consume()
            word = str(tok.value)
            # Greedily consume hyphenated continuation (no space on either side)
            while (
                self.peek().type == TT.MINUS
                and not self.peek().space_before
                and self.peek(1).type in (TT.WORD, TT.NUMBER)
                and not self.peek(1).space_before
            ):
                self.consume()
                word += "-" + str(self.consume().value)
            return StringValueNode(word)
        msg = f"Expected value for {attr!r}, got {tok.value!r} at position {tok.pos}"
        raise ParseError(msg)

    def parse_mana_value(self) -> QueryNode:
        """Parse a mana cost value: a sequence of mana symbols, words, or numbers (no gaps)."""
        tok = self.peek()
        if tok.type == TT.QUOTED:
            self.consume()
            return StringValueNode(str(tok.value))
        parts: list[str] = []
        while True:
            t = self.peek()
            if t.type == TT.MANA or t.type in (TT.WORD, TT.NUMBER):
                if parts and t.space_before:
                    break
                self.consume()
                parts.append(str(t.value))
            else:
                break
        if not parts:
            msg = f"Expected mana value at position {self.peek().pos}"
            raise ParseError(msg)
        return ManaValueNode("".join(parts).upper())

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

    def parse_color_value(self) -> QueryNode:
        """Parse a color value: a recognized color name or a combination of color letters."""
        tok = self.peek()
        if tok.type == TT.QUOTED:
            self.consume()
            return StringValueNode(str(tok.value))
        if tok.type == TT.WORD:
            val = str(tok.value)
            if val.lower() not in _VALID_COLOR_NAMES and not all(c in _COLOR_LETTERS for c in val):
                msg = f"Invalid color value {val!r} at position {tok.pos}"
                raise ParseError(msg)
            self.consume()
            return StringValueNode(val)
        msg = f"Expected color value, got {tok.value!r} at position {tok.pos}"
        raise ParseError(msg)

    def parse_date_value(self) -> QueryNode:
        """Parse a date value: YYYY or YYYY-MM-DD (hyphens must have no surrounding spaces)."""
        tok = self.peek()
        if tok.type != TT.NUMBER:
            msg = f"Expected date, got {tok.value!r} at position {tok.pos}"
            raise ParseError(msg)
        self.consume()
        year = int(tok.value)
        _validate_mtg_year(year, tok.pos)
        # Consume YYYY-MM-DD: two MINUS+NUMBER pairs without spaces
        if (
            self.peek().type == TT.MINUS
            and not self.peek().space_before
            and self.peek(1).type == TT.NUMBER
            and not self.peek(1).space_before
        ):
            self.consume()
            month_tok = self.consume()
            if (
                self.peek().type == TT.MINUS
                and not self.peek().space_before
                and self.peek(1).type == TT.NUMBER
                and not self.peek(1).space_before
            ):
                self.consume()
                day_tok = self.consume()
                month = int(month_tok.value)
                day = int(day_tok.value)
                try:
                    datetime.date(year=year, month=month, day=day)
                except ValueError as exc:
                    msg = f"Invalid date {year}-{month:02d}-{day:02d} at position {tok.pos}: {exc}"
                    raise ParseError(msg) from exc
                return StringValueNode(f"{year}-{month:02d}-{day:02d}")
        return StringValueNode(str(year))

    def parse_year_value(self) -> QueryNode:
        """Parse a year value: 4-digit integer >= 1992."""
        tok = self.peek()
        if tok.type != TT.NUMBER:
            msg = f"Expected year, got {tok.value!r} at position {tok.pos}"
            raise ParseError(msg)
        self.consume()
        year = int(tok.value)
        _validate_mtg_year(year, tok.pos)
        return StringValueNode(str(year))


# ── entry point ───────────────────────────────────────────────────────────────


def parse_query(src: str | None) -> Query:
    """Parse a Scryfall query string into a Query AST.

    Drop-in replacement for parse_search_query; handles implicit AND natively
    without a separate preprocessing pass.
    """
    if not src or not src.strip():
        return Query(TrueNode())
    try:
        tokens = tokenize(src)
    except LexError as exc:
        msg = f'Failed to lex query: "{src}"'
        raise ValueError(msg) from exc
    try:
        result = Parser(tokens).parse()
    except ParseError as exc:
        msg = f'Failed to parse query: "{src}"'
        raise ValueError(msg) from exc
    return flatten_nested_operations(result)
