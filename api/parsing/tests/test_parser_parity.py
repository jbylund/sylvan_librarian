"""Tests that both parser implementations produce identical SQL for the same queries."""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.pyparsing_based import parse_search_query
from api.parsing.tests.implicit_and_cases import TESTCASES


@pytest.mark.parametrize(
    argnames=["query",],
    argvalues=[[c["query"]] for c in TESTCASES if c["query"].strip()],
    ids=[c["id"] for c in TESTCASES if c["query"].strip()],
)
def test_both_parsers_agree(query: str) -> None:
    """Both parsers must produce identical SQL for every query in TESTCASES."""
    hand_exc: Exception | None = None
    pyp_exc: Exception | None = None
    hand_result: tuple | None = None
    pyp_result: tuple | None = None

    try:
        hand_result = generate_sql_query(parse_scryfall_query(query))
    except Exception as exc:  # noqa: BLE001
        hand_exc = exc

    try:
        pyp_result = generate_sql_query(parse_search_query(query))
    except Exception as exc:  # noqa: BLE001
        pyp_exc = exc

    assert (hand_exc is None) == (pyp_exc is None), (
        f"Parsers disagree on validity of {query!r}: "
        f"hand_rolled={'ok' if hand_exc is None else hand_exc!r}, "
        f"pyparsing={'ok' if pyp_exc is None else pyp_exc!r}"
    )
    if hand_result is not None:
        assert hand_result == pyp_result, (
            f"Parsers produce different SQL for {query!r}:\n  hand_rolled: {hand_result}\n  pyparsing:   {pyp_result}"
        )
