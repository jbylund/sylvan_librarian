"""Character-level span rules shared by the lexer and the query balancer.

A leaf module on purpose: ``parsing_f`` imports ``hand_parser``, so anything both need has to sit
below both of them. Keeping two copies of "where does a span start, and where does it end" is how
#905 happened — the balancer closed a quote that the lexer had already treated as content.
"""

from __future__ import annotations

import re

QUOTE_CHARS = frozenset("'\"")

# Characters after which a "'" opens a string: the start of a token. Whitespace, a group opener, the
# negation and exact-name prefixes, and the tail of every comparison operator. Anywhere else -- inside
# `can't`, after a closing paren -- an apostrophe is content. A '"' opens a string wherever it appears.
QUOTE_OPENER_PRECEDERS = frozenset(" \t\r\n(-!:=<>")

# A backslash escapes the character after it inside a quoted string, so '\'' is one string holding a
# single quote. Anything that has to find the end of a string has to know that.
_ESCAPED_CHAR = re.compile(r"\\(.)", re.DOTALL)

# Last character of every comparison operator the lexer emits (':', '=', '!=', '>=', '<=', '>', '<').
# A '/' directly after one of these is in value position, which is the only place a regex opens.
# test_spans.py pins this against the operators hand_parser actually emits.
COMPARISON_TAIL_CHARS = frozenset(":=><")


def opens_regex(query: str, slash_index: int) -> bool:
    """Return True if the '/' at *slash_index* opens a regex rather than being division.

    The character-level form of the rule in hand_parser.tokenize, for callers that have no token
    stream to look back into — the balancers, which run on partially typed queries. The lexer checks
    the same thing more precisely, by asking whether the previous token was a comparison operator.
    """
    pos = slash_index - 1
    while pos >= 0 and query[pos].isspace():
        pos -= 1
    return pos >= 0 and query[pos] in COMPARISON_TAIL_CHARS


def opens_quote(query: str, quote_index: int) -> bool:
    """Return True if the quote character at *quote_index* opens a string rather than being content.

    A '"' always does. A "'" does only at the start of a token -- at the beginning of the query or after
    one of QUOTE_OPENER_PRECEDERS -- because mid-word it is an apostrophe: `o:can't` is one word, not an
    unterminated string. The lexer reads the same rule, so the balancer cannot close a quote the lexer
    never opened (#905's failure class, with apostrophes in place of escapes).
    """
    if query[quote_index] == '"':
        return True
    return quote_index == 0 or query[quote_index - 1] in QUOTE_OPENER_PRECEDERS


def brace_close_index(query: str, start: int) -> int | None:
    """Return the index of the '}' closing a mana symbol whose content starts at *start*, or None if unterminated.

    A plain search for the next '}': no escape sequence exists inside a mana symbol, so a '{...}' is
    opaque whatever it holds. There is deliberately no charset bounding the content. Stopping the span
    at the first character no real symbol could contain looks attractive — it would keep a stray '{'
    from swallowing the rest of the query — but it cannot be done here, because the lexer demands a '}'
    for every '{': a balancer that declined to close the '{' in 'mana:{'' would leave a prefix of the
    accepted query "mana:{'}" unlexable halfway through being typed, which is #908's failure mode.

    Whether the content is a *real* symbol is a separate question, and not one a charset could settle
    in any case — every character of '{A/B/C/D}' is individually legal, so only checking the symbol as
    a whole rejects it. That check belongs wherever mana values are validated.
    """
    index = query.find("}", start)
    return None if index < 0 else index


def find_close_index(query: str, start: int, closer: str) -> tuple[int | None, bool, bool]:
    """Return where the next unescaped *closer* is, whether a dangling escape ended the walk, and whether it saw one.

    The first element is the index, or None if *closer* is never found. The second is True only when
    the walk instead ran to the end of *query* on a dangling escape. The third is True if the walk
    stepped over *any* backslash escape at all — a caller that wants the span's unescaped content can
    skip the unescape pass entirely when this is False, since content is then just ``query[start:idx]``
    unchanged. All three are already known once this one walk is done.

    A caller that only needs the index can ignore the rest; a balancer completing an unterminated span
    needs the second; a caller building unescaped content needs the third to avoid re-walking the same
    text a second time just to learn it had nothing to unescape.
    """
    pos = start
    length = len(query)
    saw_escape = False
    while pos < length:
        if query[pos] == "\\":
            saw_escape = True
            if pos + 1 >= length:
                return None, True, saw_escape
            pos += 2
        elif query[pos] == closer:
            return pos, False, saw_escape
        else:
            pos += 1
    return None, False, saw_escape


def unescape(text: str) -> str:
    r"""Resolve backslash escapes in span content, so ``a\'b`` becomes ``a'b``."""
    return _ESCAPED_CHAR.sub(r"\1", text)
