"""`game:` SQL run against real PostgreSQL, over the `games` arrays of real Scryfall printings.

The arrays are copied from api.scryfall.com (2026-11-13): Badlands VMA is online-only, Badlands 3ED is
paper-only, and Llanowar Elves DOM is in all three games.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import tuple_row

from api.parsing import generate_sql_query, parse_scryfall_query
from api.utils.db_utils import maybe_json

PRINTINGS = {
    "badlands-vma": ["mtgo"],
    "badlands-3ed": ["paper"],
    "llanowar-elves-dom": ["arena", "paper", "mtgo"],
}


@pytest.mark.usefixtures("postgres_container")
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("game:paper", {"badlands-3ed", "llanowar-elves-dom"}),
        ("game:mtgo", {"badlands-vma", "llanowar-elves-dom"}),
        ("game:arena", {"llanowar-elves-dom"}),
        ("-game:paper", {"badlands-vma"}),
        ("game:paper -game:arena", {"badlands-3ed"}),
        ("game:paper prefer:usd-low", {"badlands-3ed", "llanowar-elves-dom"}),
    ],
)
def test_game_filters_printings_by_their_games(query: str, expected: set[str]) -> None:
    """The WHERE clause keeps exactly the printings whose games array holds the game."""
    where_clause, params = generate_sql_query(parse_scryfall_query(query))
    values = ", ".join(f"(%(id_{i})s, %(games_{i})s::jsonb)" for i in range(len(PRINTINGS)))
    rows = {}
    for i, (printing, games) in enumerate(PRINTINGS.items()):
        rows[f"id_{i}"] = printing
        rows[f"games_{i}"] = maybe_json({"games": games})
    sql = f"SELECT card.id FROM (VALUES {values}) AS card(id, raw_card_blob) WHERE {where_clause}"
    bound = rows | {key: maybe_json(value) for key, value in params.items()}
    with psycopg.connect(row_factory=tuple_row) as conn, conn.cursor() as cursor:
        cursor.execute(sql, bound)
        assert {row[0] for row in cursor.fetchall()} == expected
