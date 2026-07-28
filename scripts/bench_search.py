"""Benchmark engine vs SQL search paths.

Run inside the API container so both DB and card_engine are available:

    docker exec sylvan_blue-apiservice-1 python3 /app/scripts/bench_search.py

The script loads the engine from the DB (same path as production), then
times both engine.query() and _search_sql() for each query/unique combination
and prints a comparison table.
"""

from __future__ import annotations

import multiprocessing
import sys
import time

sys.path.insert(0, "/app")

from api.api_resource import APIResource
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import generate_sql_query, parse_scryfall_query
from api.utils.timer import Timer

# ─── Config ───────────────────────────────────────────────────────────────────

QUERIES: list[tuple[str, str, CardOrdering]] = [
    ("name:soldier", "name:soldier", CardOrdering.EDHREC),
    ("t:merfolk and name:tide", "t:merfolk+tide", CardOrdering.EDHREC),
    ("cmc>3", "cmc>3", CardOrdering.CMC),
    ("format:legacy", "format:legacy", CardOrdering.EDHREC),
]

UNIQUES = [UniqueOn.CARD, UniqueOn.PRINTING, UniqueOn.ARTWORK]

ENGINE_WARMUP = 20  # iterations to discard before timing
ENGINE_WINDOW = 5.0  # seconds to run the timed loop
SQL_RUNS = 15  # total SQL calls per cell
SQL_DISCARD = 3  # discard this many from the front

# ─── Setup ────────────────────────────────────────────────────────────────────

print("Connecting to DB and loading engine store…", flush=True)
api = APIResource(last_import_time=multiprocessing.Value("d", time.time(), lock=True))
api.admin._import_recent = lambda: True  # prevent import_data() from running
api._setup_complete = lambda: True

api._reload_engine(force=True)
print(f"Engine loaded: {api._engine.size():,} cards\n", flush=True)

# ─── Benchmark helpers ────────────────────────────────────────────────────────


def bench_engine(q_str: str, unique: UniqueOn, orderby: CardOrdering) -> float:
    """Return average µs per engine.query() call."""
    q = parse_scryfall_query(q_str)
    for _ in range(ENGINE_WARMUP):
        api._engine.query(
            filters=q,
            unique=str(unique),
            prefer=str(PreferOrder.DEFAULT),
            orderby=str(orderby),
            direction=str(SortDirection.ASC),
            limit=100,
        )
    n = 0
    t0 = time.monotonic()
    deadline = t0 + ENGINE_WINDOW
    while time.monotonic() < deadline:
        api._engine.query(
            filters=q,
            unique=str(unique),
            prefer=str(PreferOrder.DEFAULT),
            orderby=str(orderby),
            direction=str(SortDirection.ASC),
            limit=100,
        )
        n += 1
    return (time.monotonic() - t0) / n * 1_000  # ms


def bench_sql(q_str: str, unique: UniqueOn, orderby: CardOrdering) -> float:
    """Return average ms per _search_sql() call with cache cleared before each."""
    parsed = parse_scryfall_query(q_str)
    where_clause, base_params = generate_sql_query(parsed)
    query_explanation = parsed.to_human_explanation()
    times = []
    for _ in range(SQL_RUNS):
        api._query_cache.clear()
        t0 = time.monotonic()
        api._search_sql(
            where_clause=where_clause,
            params=dict(base_params),
            query_explanation=query_explanation,
            query=q_str,
            unique=unique,
            prefer=PreferOrder.DEFAULT,
            orderby=orderby,
            direction=SortDirection.ASC,
            limit=100,
            timer=Timer(),
        )
        times.append((time.monotonic() - t0) * 1_000)
    return sum(times[SQL_DISCARD:]) / len(times[SQL_DISCARD:])


# ─── Run ──────────────────────────────────────────────────────────────────────

header = f"{'query':<22} {'unique':<10} {'engine ms':>10} {'sql ms':>9}  winner"
print(header)
print("-" * len(header))

prev_label = ""
for q_str, label, orderby in QUERIES:
    for unique in UNIQUES:
        eng_ms = bench_engine(q_str, unique, orderby)
        sql_ms = bench_sql(q_str, unique, orderby)

        winner = f"engine {sql_ms / eng_ms:.0f}x" if eng_ms < sql_ms else f"sql    {eng_ms / sql_ms:.0f}x"

        if label != prev_label and prev_label:
            print()
        print(f"{label:<22} {unique!s:<10} {eng_ms:>10.2f} {sql_ms:>9.1f}  {winner}")
        prev_label = label

print(f"\nEngine: {ENGINE_WARMUP} warmup + {ENGINE_WINDOW:.0f}s timed window")
print(f"SQL:    {SQL_RUNS} runs, first {SQL_DISCARD} discarded, _query_cache cleared each call")
