"""Public API for Scryfall query parsing and SQL generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.parsing import hand_parser
from api.parsing.pyparsing_based import (
    COMPARISON_OPERATORS,
    generate_sql_query,
    is_operator,
    parse_search_query,
    preprocess_implicit_and,
)

if TYPE_CHECKING:
    from api.parsing.nodes import Query

__all__ = [
    "COMPARISON_OPERATORS",
    "balance_partial_query",
    "generate_sql_query",
    "is_operator",
    "parse_scryfall_query",
    "parse_search_query",
    "preprocess_implicit_and",
]


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
    for char in query:
        # When inside a quoted string, only the matching closing quote ends it.
        if current_stack and current_stack[-1] in quote_chars:
            if char == current_stack[-1]:
                current_stack.pop()
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
    """Parse a Scryfall search query and convert to Scryfall-specific AST.

    Args:
        query: The search query string to parse.

    Returns:
        A Scryfall-specific Query AST.
    """
    return hand_parser.parse_query(query)
