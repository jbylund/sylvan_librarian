"""Public entry points for Scryfall query parsing helpers."""

from __future__ import annotations

from api.parsing.hand_parser import _is_word_cont
from api.parsing.spans import QUOTE_CHARS, brace_close_index, find_close_index, opens_regex


def _closer_for_partial_span(dangling_escape: bool, closer: str) -> str:
    """Return the suffix that closes a span left open on a *dangling_escape* or not.

    A trailing backslash has nothing to escape yet, so appending *closer* on its own would escape
    *that* instead of ending the span — escape the backslash first.
    """
    return ("\\" if dangling_escape else "") + closer


def balance_partial_query(query: str) -> str:
    """Balance parentheses for typeahead searches, skipping over quotes, regexes, and mana symbols.

    Parentheses are the only construct that nests, so tracking depth is a counter rather than a stack.
    The opaque spans never go on it: each one is resolved to its closer and stepped over whole, which
    is what keeps the quotes, parens and metacharacters inside them from being read as structure.
    """
    open_parens = 0
    # Closer for whichever span is still open at the end of the query. Only one is ever needed,
    # because everything after an unterminated opener is span content — there is nothing left to open,
    # and nothing after it to close. That is also why the closers below can be appended before the
    # parens: an unterminated span is necessarily the innermost thing open.
    span_suffix = ""

    pos = 0
    while pos < len(query):
        char = query[pos]
        pos += 1

        # A quoted string, a /regex/ and a {mana symbol} are all opaque: the quotes and parens inside
        # them are content, not delimiters. The span rules come from api.parsing.spans so the balancer
        # and the lexer cannot drift apart — where they disagree, the balancer "fixes" a quote the
        # lexer never saw (#905).
        # An apostrophe preceded by a word character and followed by either another word character
        # or NOTHING is part of the word rather than an opening quote -- the same rule
        # _scan_word_end applies in the tokenizer, and the two must agree exactly or this emits
        # something the lexer rejects. Without the "or nothing", "urza'" balanced to "urza''",
        # which parses as `urza` AND an empty quoted string: the search widened to every card
        # containing "urza" and the explanation rendered "the name contains Urza and " with
        # nothing after the "and". (`pos` has already moved past `char`, so the character itself
        # is at `pos - 1`.)
        if char == "'" and pos - 1 > 0 and _is_word_cont(query[pos - 2]) and (pos == len(query) or _is_word_cont(query[pos])):
            continue

        if char in QUOTE_CHARS:
            close_index, dangling_escape, _ = find_close_index(query, pos, char)
            if close_index is None:
                span_suffix = _closer_for_partial_span(dangling_escape, char)
                break
            pos = close_index + 1
            continue

        # A '/' in value position opens a regex; anywhere else it is division, an ordinary character.
        if char == "/":
            if opens_regex(query, pos - 1):
                close_index, dangling_escape, _ = find_close_index(query, pos, "/")
                if close_index is None:
                    # Still being typed. Close the regex rather than reading on, or the metacharacters
                    # the user has typed so far get balanced as query structure: `o:/[)` is a partial
                    # `o:/[)]/`, not a stray ')'.
                    span_suffix = _closer_for_partial_span(dangling_escape, "/")
                    break
                pos = close_index + 1
            continue

        # A '{mana symbol}' is opaque whatever it holds, and an unterminated one gets closed for the
        # same reason an unterminated quote does: the lexer demands a '}' for every '{', so leaving it
        # open would make 'mana:{' — a prefix of 'mana:{W}' — unlexable while it is being typed. No
        # escapes exist inside a mana symbol, so there is no dangling-backslash case here.
        if char == "{":
            close_index = brace_close_index(query, pos)
            if close_index is None:
                span_suffix = "}"
                break
            pos = close_index + 1
            continue

        if char == "(":
            open_parens += 1
        elif char == ")":
            if not open_parens:
                msg = f"Unbalanced closing character '{char}' cannot be balanced"
                raise ValueError(msg)
            open_parens -= 1

    return query + span_suffix + ")" * open_parens
