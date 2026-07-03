"""Parity tests for frontend and backend query balancing fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.parsing import balance_partial_query

BALANCE_QUERIES = json.loads(
    (Path(__file__).resolve().parents[2] / "static" / "fixtures" / "balance_queries.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    argnames=("input_query", "expected_suffix"),
    argvalues=[(case["input"], case["suffix"]) for case in BALANCE_QUERIES],
    ids=[repr(case["input"]) for case in BALANCE_QUERIES],
)
def test_balance_partial_query_matches_frontend_fixture(input_query: str, expected_suffix: str | None) -> None:
    """The Python balancer must match the shared frontend fixture contract."""
    if expected_suffix is None:
        with pytest.raises(ValueError, match=r"Unbalanced closing character.*cannot be balanced"):
            balance_partial_query(input_query)
        return

    assert balance_partial_query(input_query) == input_query + expected_suffix
