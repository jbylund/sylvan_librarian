"""Pin pyparsing's regex-value-position grammar rule against spans.opens_regex.

`api.parsing.pyparsing_based` encodes "a regex only opens in value position" as its own grammar
sequence (`regex_after_op = comparison_tok + regex_raw`) rather than calling into
`api.parsing.spans.opens_regex`, the shared rule `hand_parser.tokenize` and the balancers use (#944).
Nothing else currently catches the two drifting apart — this fuzzes both across every real operator,
several whitespace runs, and every kind of position that is *not* after an operator, and asserts they
always agree.
"""

from __future__ import annotations

import inspect
import re

import pytest

from api.parsing import hand_parser
from api.parsing.pyparsing_based import _tokenize_for_implicit_and
from api.parsing.spans import opens_regex

# Same source-scan test_spans.py uses to pin COMPARISON_TAIL_CHARS: the real, current operator
# literals hand_parser.tokenize emits, so a newly added operator can't go untested here either.
_OP_TOKEN_LITERAL = re.compile(r'Token\(TT\.OP,\s*"([^"]+)"')
_OPERATORS = sorted(set(_OP_TOKEN_LITERAL.findall(inspect.getsource(hand_parser))))

# Whitespace runs the lexer itself treats as space (hand_parser._SPACE). Deliberately narrow, not
# str.isspace()'s full Unicode range: whether opens_regex should accept wider whitespace too is #951,
# a separate, open question this test isn't trying to settle.
_WHITESPACE = ("", " ", "  ", "\t")


@pytest.mark.parametrize("ws", _WHITESPACE)
@pytest.mark.parametrize("op", _OPERATORS)
def test_regex_opens_after_every_operator(op: str, ws: str) -> None:
    """Directly after any comparison operator, spans.opens_regex and pyparsing must both say yes."""
    query = f"name{op}{ws}/abc/"
    slash_index = query.index("/")
    assert opens_regex(query, slash_index) is True
    assert _tokenize_for_implicit_and(query)[-1] == "/abc/"


@pytest.mark.parametrize("ws", _WHITESPACE)
@pytest.mark.parametrize(
    "prefix",
    ["name", "(t:elf)", "name AND"],
    ids=["bare_word", "closing_paren", "and_keyword"],
)
def test_regex_does_not_open_outside_value_position(prefix: str, ws: str) -> None:
    """Anywhere but directly after an operator, spans.opens_regex and pyparsing must both say no.

    pyparsing says no by not producing a regex token: the slashes are stray and dropped (a slash no
    term has taken is nothing, as on Scryfall), and what stood between them is a plain word.
    """
    query = f"{prefix}{ws}/abc/"
    slash_index = query.index("/")
    assert opens_regex(query, slash_index) is False
    tokens = _tokenize_for_implicit_and(query)
    assert tokens[-1] == "abc"
    assert not any("/" in tok for tok in tokens)


def test_regex_does_not_open_at_start_of_query() -> None:
    """A bare regex with nothing before it is the other non-value-position case (#908)."""
    query = "/abc/"
    assert opens_regex(query, 0) is False
    assert _tokenize_for_implicit_and(query) == ["abc"]
