"""Public entry points for Scryfall query parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.parsing.hand_parser import parse_query as _parse_query
from api.parsing.rewrite import rewrite_query

if TYPE_CHECKING:
    from api.parsing.nodes import Query


def _regex_close_index(query: str, start: int) -> int | None:
    """Return the index of the '/' closing a regex that opened before *start*, or None if unterminated.

    Mirrors the lexer's rule in hand_parser.tokenize: scan for the next unescaped '/', and treat the
    opening '/' as arithmetic division when there is none. The two must agree on what counts as
    regex — where they disagree, the balancer "fixes" a quote the lexer never saw (#905).
    """
    pos = start
    length = len(query)
    while pos < length:
        if query[pos] == "\\" and pos + 1 < length:
            pos += 2
        elif query[pos] == "/":
            return pos
        else:
            pos += 1
    return None


def balance_partial_query(query: str) -> str:
    """Balance quotes and parentheses for typeahead searches using a stack."""
    char_to_mirror = {
        "(": ")",
        "'": "'",  # single quote is own mirror
        '"': '"',  # double quote is own mirror
        ")": "(",
    }
    unbalanced_closing_chars = {")"}
    quote_chars = {"'", '"'}

    current_stack = []
    pos = 0
    while pos < len(query):
        char = query[pos]
        pos += 1

        # When inside a quoted string, only the matching closing quote ends it.
        if current_stack and current_stack[-1] in quote_chars:
            if char == current_stack[-1]:
                current_stack.pop()
            continue

        # A closed /regex/ is opaque: the quotes and parens inside it are pattern characters, not
        # delimiters. An unterminated '/' is division, so it is left to fall through as an
        # ordinary character.
        if char == "/":
            close_index = _regex_close_index(query, pos)
            if close_index is not None:
                pos = close_index + 1
            continue

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
    while current_stack:
        char = current_stack.pop()
        mirrored_char = char_to_mirror[char]
        query += mirrored_char
    return query


def parse_scryfall_query(query: str) -> Query:
    """Parse a Scryfall search query into a card-specific AST.

    Args:
        query: The search query string to parse.

    Returns:
        A Scryfall-specific Query AST.
    """
    # parse => transform => rest: the whole rewrite pipeline runs on the common AST at this shared
    # seam, so it applies identically regardless of which parser _parse_query is.
    return rewrite_query(_parse_query(query))
