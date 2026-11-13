"""The Rust engine has no `games` field yet, so it declines `game:` and the SQL path serves it."""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from api.parsing import parse_scryfall_query
from card_engine import QueryEngine, RetryableQueryError

if TYPE_CHECKING:
    from collections.abc import Generator

_FIXTURE = Path(__file__).parent / "fixtures" / "engine_cards.json"


@pytest.fixture(scope="module", name="engine")
def engine_fixture() -> Generator[QueryEngine]:
    """An engine loaded with the fixture cards, on its own snapshot path."""
    shm_path = Path(tempfile.gettempdir()) / f"sylvan_librarian_test_{uuid.uuid4().hex}"
    engine = QueryEngine(shm_path=str(shm_path))
    engine.reload(json.loads(_FIXTURE.read_text()))
    yield engine
    shm_path.unlink(missing_ok=True)
    shm_path.with_suffix(".lock").unlink(missing_ok=True)


def _query(engine: QueryEngine, q: str) -> tuple[int, list]:
    return engine.query(
        filters=parse_scryfall_query(q),
        unique="card",
        prefer="default",
        orderby="edhrec",
        direction="asc",
        limit=10,
        offset=0,
        fields=["name"],
    )


@pytest.mark.parametrize("query", ["game:paper", "t:elf -game:arena"])
def test_engine_declines_game(engine: QueryEngine, query: str) -> None:
    """A retryable decline, which _search answers from SQL, rather than a wrong answer or a 500."""
    with pytest.raises(RetryableQueryError):
        _query(engine, query)


def test_engine_runs_a_query_with_prefer_lifted(engine: QueryEngine) -> None:
    """prefer: is gone from the tree, so the engine answers the rest of the query as before."""
    assert _query(engine, "cmc=1 prefer:usd-low") == _query(engine, "cmc=1")
