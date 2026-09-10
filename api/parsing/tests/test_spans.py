"""Tests for the span rules shared by the lexer and the balancers."""

from __future__ import annotations

import inspect
import re

import pytest

from api.parsing import hand_parser
from api.parsing.spans import (
    COMPARISON_TAIL_CHARS,
    QUOTE_OPENER_PRECEDERS,
    brace_close_index,
    find_close_index,
    opens_quote,
    opens_regex,
    unescape,
)

# Every distinct braced symbol in the card corpus, from mana costs and oracle text alike (60 of them,
# via `regexp_matches(mana_cost_text, '[{]([^}]*)[}]', 'g')` over magic.cards), plus the symbols on
# card types that corpus does not carry: {CHAOS} and {PW} on planar cards, {TK} and {A} from Unfinity,
# {Y}/{Z} and the half symbols from the Un-sets. The unloaded ones are deliberate — they are why the
# span rule stays "opaque up to the '}'" rather than a charset fitted to whatever one corpus holds.
_REAL_MANA_SYMBOLS = (
    # Generic, including values no printing uses yet.
    *[str(n) for n in (*range(21), 100, 1000000)],
    # Single markers.
    *"WUBRGCSXYZTQEPAH",
    # Hybrid, generic-hybrid, phyrexian, and hybrid-phyrexian.
    *("2/W", "2/U", "2/B", "2/R", "2/G"),
    *("W/U", "W/B", "U/B", "U/R", "B/R", "B/G", "R/G", "R/W", "G/W", "G/U"),
    *("C/W", "C/U", "C/B", "C/R", "C/G"),
    *("W/P", "U/P", "B/P", "R/P", "G/P"),
    *("G/U/P", "G/W/P", "R/G/P", "R/W/P"),
    # Half mana, and the multi-letter markers.
    *("HW", "HR", "CHAOS", "PW", "TK"),
)

# Matches the operator literals the lexer emits, e.g. Token(TT.OP, ">=", start, sb).
_OP_TOKEN_LITERAL = re.compile(r'Token\(TT\.OP,\s*"([^"]+)"')


def test_comparison_tail_chars_covers_every_operator_the_lexer_emits() -> None:
    """Every comparison operator must end in a character COMPARISON_TAIL_CHARS knows about.

    The balancers cannot see tokens, so they recognise value position from the character before the
    '/'. That approximation only holds while the lexer's operators all end in one of these chars, and
    nothing else forces the two to agree — hence the source scan: adding an operator that ends in a
    new character (say '~=') has to fail here rather than in the frontend, where it would show up as
    a regex the balancer mangles.
    """
    operators = set(_OP_TOKEN_LITERAL.findall(inspect.getsource(hand_parser)))
    assert operators, "found no TT.OP literals — the scan pattern no longer matches the lexer"
    assert {op[-1] for op in operators} <= COMPARISON_TAIL_CHARS


@pytest.mark.parametrize(
    argnames=["query", "slash_index", "expected"],
    argvalues=[
        ("name:/bolt/", 5, True),
        ("name: /bolt/", 6, True),  # whitespace between operator and pattern
        ("power>/x/", 6, True),
        ("power>=/x/", 7, True),
        ("power/2", 5, False),  # division
        ("/bolt/", 0, False),  # bare regex: nothing in front of the slash
        ("(t:elf) /foo/", 8, False),  # not directly after an operator
    ],
    ids=[
        "colon",
        "colon_then_space",
        "greater_than",
        "greater_or_equal",
        "division",
        "bare_leading_slash",
        "after_group",
    ],
)
def test_opens_regex(query: str, slash_index: int, expected: bool) -> None:
    """A regex opens only in value position, directly after a comparison operator."""
    assert opens_regex(query, slash_index) is expected


@pytest.mark.parametrize(
    argnames=["query", "quote_index", "expected"],
    argvalues=[
        ("'hello", 0, True),
        ("o:'draw a card'", 2, True),
        ("a 'b c'", 2, True),
        ("(o:'a", 3, True),
        ("-'bolt'", 1, True),
        ("!'Lightning Bolt'", 1, True),
        ("name='x'", 5, True),
        ("o:can't", 5, False),  # mid-word: an apostrophe, not a string
        ("Urza's", 4, False),
        ('o:"can\'t"', 2, True),  # a double quote always opens
        ("(t:elf)'x'", 7, False),  # after a closing paren: not a token start
    ],
    ids=[
        "start_of_query",
        "after_colon",
        "after_space",
        "after_paren_and_colon",
        "after_negation",
        "after_bang",
        "after_equals",
        "mid_word",
        "possessive",
        "double_quote_always",
        "after_close_paren",
    ],
)
def test_opens_quote(query: str, quote_index: int, expected: bool) -> None:
    """A "'" opens a string only at token start; a '"' always does."""
    assert opens_quote(query, quote_index) is expected


def test_quote_opener_preceders_cover_every_comparison_tail() -> None:
    """A "'" directly after any comparison operator is a value, so every operator tail must be a preceder."""
    assert COMPARISON_TAIL_CHARS <= QUOTE_OPENER_PRECEDERS


@pytest.mark.parametrize(
    argnames=["query", "expected_types"],
    argvalues=[
        ("o:can't", ["WORD", "OP", "WORD"]),
        ("can't stop", ["WORD", "WORD"]),
        ("o:'draw a card'", ["WORD", "OP", "QUOTED"]),
        ("a 'b c' d", ["WORD", "QUOTED", "WORD"]),
        ("!'Urza Saga'", ["BANG", "QUOTED"]),
        ("-'bolt'", ["MINUS", "QUOTED"]),
    ],
    ids=["mid_word", "bare_mid_word", "value_string", "bare_string", "exact_name", "negated_string"],
)
def test_lexer_reads_apostrophes_by_the_shared_rule(query: str, expected_types: list[str]) -> None:
    """`o:can't` lexes to one text value; a "'" at token start still opens a string."""
    tokens = hand_parser.tokenize(query)
    assert [tok.type.name for tok in tokens[:-1]] == expected_types


def test_lexer_keeps_the_apostrophe_in_the_word() -> None:
    tokens = hand_parser.tokenize("o:can't")
    assert tokens[2].value == "can't"
    assert tokens[2].raw == "can't"


@pytest.mark.parametrize(
    argnames=["query", "start", "expected"],
    argvalues=[
        ("name:/bolt/", 6, 10),
        ("name:/a/ o:/b/", 6, 7),  # stops at the first close, not the last slash in the query
        (r"name:/a\/b/", 6, 10),  # escaped delimiter is pattern content
        ("name:/bolt", 6, None),  # unterminated
        ("name:/a\\", 6, None),  # trailing backslash consumes the end of the query
    ],
    ids=["simple", "first_close_wins", "escaped_delimiter", "unterminated", "trailing_backslash"],
)
def test_find_close_index_for_regex(query: str, start: int, expected: int | None) -> None:
    """The close scan steps over escapes and returns None when the pattern never closes."""
    assert find_close_index(query, start, "/")[0] == expected


@pytest.mark.parametrize(
    argnames=["query", "quote", "expected"],
    argvalues=[
        ("'abc'", "'", 4),
        (r"'don\'t'", "'", 7),  # the escaped quote is content, not the close
        (r"'a\\'", "'", 4),  # an escaped backslash does not escape the quote after it
        (r'"say \"hi\""', '"', 11),
        ("'a\"b'", "'", 4),  # the other quote type is content
        ("'abc", "'", None),  # unterminated
        (r"'abc\'", "'", None),  # the only candidate close is escaped
    ],
    ids=["plain", "escaped_quote", "escaped_backslash", "double_quoted", "other_quote_type", "unterminated", "escaped_close"],
)
def test_find_close_index_for_quote(query: str, quote: str, expected: int | None) -> None:
    """A backslash escapes the next character, so it cannot end the string."""
    assert find_close_index(query, 1, quote)[0] == expected


@pytest.mark.parametrize(
    argnames=["query", "expected"],
    argvalues=[
        ("'abc", False),
        ("'abc" + "\\", True),  # nothing left to escape: a closer appended now would be escaped
        (r"'abc\\", False),  # the backslash is itself escaped
        (r"'a\'b", False),
    ],
    ids=["no_backslash", "dangling", "escaped_backslash", "escape_consumed"],
)
def test_has_dangling_escape(query: str, expected: bool) -> None:
    """A trailing backslash leaves an escape with nothing to apply to.

    `find_close_index`'s dangling-escape flag is what `has_dangling_escape` used to duplicate; none
    of these queries contain an unescaped closer, so the closer char passed here doesn't matter.
    """
    assert find_close_index(query, 1, "'")[1] is expected


@pytest.mark.parametrize(
    argnames=["query", "quote", "expected"],
    argvalues=[
        ("'abc'", "'", False),  # nothing to unescape: a caller can use the slice as-is
        (r"'don\'t'", "'", True),
        (r"'a\\'", "'", True),  # an escaped backslash still counts, even though it's not the closer
        ("'abc", "'", False),  # unterminated, and still nothing escaped
        ("'abc\\", "'", True),  # unterminated on a dangling escape, which is itself an escape seen
    ],
    ids=["no_escape", "escaped_quote", "escaped_backslash", "unterminated_no_escape", "unterminated_dangling"],
)
def test_find_close_index_reports_whether_it_saw_an_escape(query: str, quote: str, expected: bool) -> None:
    """The third element lets a caller skip unescaping entirely when the walk saw no backslash at all."""
    assert find_close_index(query, 1, quote)[2] is expected


@pytest.mark.parametrize(
    argnames=["text", "expected"],
    argvalues=[
        ("abc", "abc"),
        (r"don\'t", "don't"),
        (r"a\\b", r"a\b"),
        (r"say \"hi\"", 'say "hi"'),
    ],
    ids=["no_escapes", "escaped_quote", "escaped_backslash", "escaped_double_quotes"],
)
def test_unescape(text: str, expected: str) -> None:
    """Span content drops one level of backslash escaping, as the lexer's QUOTED token does."""
    assert unescape(text) == expected


@pytest.mark.parametrize(argnames=["symbol"], argvalues=[(sym,) for sym in _REAL_MANA_SYMBOLS], ids=_REAL_MANA_SYMBOLS)
def test_every_real_symbol_is_one_opaque_span(symbol: str) -> None:
    """A real symbol has to end at its own '}', including symbols no card in the local corpus uses.

    Pins the span rule against the vocabulary rather than against a hand-written charset: any future
    attempt to bound brace content by shape has to keep every one of these working.
    """
    query = f"mana:{{{symbol}}}"
    assert brace_close_index(query, 6) == len(query) - 1

    mana_tokens = [tok for tok in hand_parser.tokenize(query) if tok.type is hand_parser.TT.MANA]
    assert [tok.value for tok in mana_tokens] == [f"{{{symbol}}}"]


@pytest.mark.parametrize(
    argnames=["query", "start", "expected"],
    argvalues=[
        ("mana:{W}", 6, 7),
        ("mana:{2/W}", 6, 9),
        ("mana:{W}{U}", 9, 10),  # second symbol
        ("mana:{)}", 6, 7),  # content is opaque, valid or not
        ("mana:{'}", 6, 7),
        ("mana:{}", 6, 6),  # empty symbol still terminates
        ("mana:{W", 6, None),  # unterminated
        ("mana:{ and o:bolt", 6, None),
        (r"mana:{\}", 6, 7),  # no escapes inside a symbol, so the backslash does not shield the brace
    ],
    ids=[
        "simple",
        "hybrid",
        "second_symbol",
        "paren_content",
        "quote_content",
        "empty",
        "unterminated",
        "unterminated_with_junk",
        "backslash_is_not_an_escape",
    ],
)
def test_brace_close_index(query: str, start: int, expected: int | None) -> None:
    """A '{...}' ends at the next '}' whatever it holds — the rule the lexer has always used."""
    assert brace_close_index(query, start) == expected
