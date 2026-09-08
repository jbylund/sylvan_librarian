"""Tests for query balancing functionality."""

import pytest

from api.parsing import balance_partial_query


@pytest.mark.parametrize(
    argnames="original_query",
    argvalues=[
        'name:"hydr',
        '(name:"lightning',
        # Typing toward "urza's": the trailing apostrophe must survive the balancer untouched
        # and still parse, rather than being closed into an empty quoted string.
        "urza'",
        "o'",
    ],
)
def test_balanced_queries_still_parse(parse_query, original_query: str) -> None:
    """Representative balanced partial queries should remain parseable.

    Shared fixture parity coverage lives in test_balance_parity.py. This test keeps a small
    end-to-end integration check that the balanced output still feeds the parser successfully.
    """
    balanced_query = balance_partial_query(original_query)

    # Original should fail (at least for quote cases)
    if '"' in original_query and original_query.count('"') % 2 == 1:
        with pytest.raises(ValueError, match=r"(quote|lex query)"):
            parse_query(original_query)

    # Balanced should succeed
    result = parse_query(balanced_query)
    assert result is not None, f"Failed to parse balanced query: {balanced_query}"


@pytest.mark.parametrize(
    argnames="original_query",
    argvalues=[
        "name:/'/",
        "name:/'s/",
        'name:/"/',
        "name:/a'b/",
        "(name:/'/",
        "name:/'/ (t:goblin",
    ],
)
def test_quotes_inside_a_regex_survive_balancing(parse_query, original_query: str) -> None:
    """Quotes inside a closed /regex/ are pattern characters, so balancing must not close them.

    The balancer used to append a stray quote here, and the browser sends the balanced string —
    so a query that parsed fine became a server-side parse error for something never typed.
    """
    balanced_query = balance_partial_query(original_query)

    assert balanced_query.count("'") == original_query.count("'")
    assert balanced_query.count('"') == original_query.count('"')
    assert parse_query(balanced_query) is not None, f"Failed to parse balanced query: {balanced_query}"


@pytest.mark.parametrize(
    argnames="original_query",
    argvalues=[
        "(mana:{W}) or t:elf",
        "mana:{2/W}{U}",
        "devotion:{U}{U}",
    ],
)
def test_mana_symbols_survive_balancing(parse_query, original_query: str) -> None:
    """A closed '{...}' is opaque, so a balanced query holding one comes back untouched."""
    balanced_query = balance_partial_query(original_query)

    assert balanced_query == original_query
    assert parse_query(balanced_query) is not None


@pytest.mark.parametrize(
    argnames="original_query",
    argvalues=[
        "mana:{)}",
        "(mana:{)})",
        "mana:{'}",
    ],
)
def test_junk_inside_a_mana_symbol_is_not_read_as_query_structure(original_query: str) -> None:
    """Brace content is opaque even when it is not a real symbol, so balancing must leave it alone.

    The balancer used to read the ')' in '(mana:{)})' as a paren and reject an already-balanced query
    outright — and the browser drops a query whose balance fails, so this never reached the server
    (#908's failure mode, with braces in place of regexes). Rejecting these is now the parser's job,
    which can say *why*; the balancer's job is only to not invent characters the user never typed.
    """
    assert balance_partial_query(original_query) == original_query


@pytest.mark.parametrize(
    argnames=["original_query", "expected"],
    argvalues=[
        ("mana:{W", "mana:{W}"),
        ("mana:{W}{U", "mana:{W}{U}"),
        ("(mana:{2/W", "(mana:{2/W})"),
    ],
)
def test_half_typed_mana_symbols_are_closed(parse_query, original_query: str, expected: str) -> None:
    """A '{' with a symbol in progress gets its '}', so typeahead works mid-symbol."""
    assert balance_partial_query(original_query) == expected
    assert parse_query(expected) is not None


@pytest.mark.parametrize(
    argnames=["original_query", "expected"],
    argvalues=[
        ("(o:'a and t:elf", "(o:'a and t:elf')"),
        ("(mana:{ and o:bolt)", "(mana:{ and o:bolt)})"),
    ],
)
def test_an_unterminated_span_swallows_the_rest_of_the_query(original_query: str, expected: str) -> None:
    """An unterminated '{' takes the rest of the query as content, exactly as an open quote does.

    Bounding the brace span by what a real mana symbol may contain would stop this, but it cannot be
    done here: the lexer demands a '}' for every '{', so declining to close one leaves a partly typed
    query unlexable. That is the same bargain already struck for quotes, and for the brace case symbol
    validation catches the result anyway — '{ AND O:BOLT)}' is not a cost, so this 400s rather than
    quietly matching nothing.
    """
    assert balance_partial_query(original_query) == expected
