"""Benchmark query() (preferred-index fast path) vs query_linear() (full scan).

Run inside the API container:

    docker exec arcane_blue-apiservice-1 python3 /app/scripts/bench_preferred_index.py

Compares engine.query() (uses preferred-printing index when applicable) against
engine.query_linear() (always scans all printings with linear dedup), for a mix
of card-level queries (fast path fires) and printing-level queries (falls back).
"""

# pylint: disable=protected-access
from __future__ import annotations

import multiprocessing
import sys
import time
from typing import TYPE_CHECKING, Any

sys.path.insert(0, "/app")

from api.api_resource import APIResource
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import parse_scryfall_query

if TYPE_CHECKING:
    from collections.abc import Callable

# ─── Queries ──────────────────────────────────────────────────────────────────

QUERIES = [
    # Card-level: fast path should fire
    ("format:modern", "format:modern", CardOrdering.EDHREC, True),
    ("format:legacy", "format:legacy", CardOrdering.EDHREC, True),
    ("c:r", "c:r", CardOrdering.EDHREC, True),
    ("t:creature", "t:creature", CardOrdering.EDHREC, True),
    ("o:flying", "o:flying", CardOrdering.EDHREC, True),
    ("cmc>5", "cmc>5", CardOrdering.CMC, True),
    ("t:merfolk o:draw", "t:merfolk o:draw", CardOrdering.EDHREC, True),
    # Printing-level: fast path should NOT fire
    ("a:terese", 'a:"terese"', CardOrdering.EDHREC, False),
    ("year>2023", "year>2023", CardOrdering.EDHREC, False),
]

WARMUP = 30  # iterations discarded
WINDOW_S = 5.0  # seconds per timed window

# ─── Setup ────────────────────────────────────────────────────────────────────

print("Loading engine from DB…", flush=True)
api = APIResource(last_import_time=multiprocessing.Value("d", time.time(), lock=True))
api._import_recent = lambda: True
api._setup_complete = lambda: True
api._reload_engine(force=True)
print(f"Engine loaded: {api._engine.size():,} printings\n", flush=True)

# ─── Bench helper ─────────────────────────────────────────────────────────────


def bench(method: Callable[..., Any], q: str, unique: UniqueOn, orderby: CardOrdering) -> float:
    """Return average µs per call."""
    parsed = parse_scryfall_query(q)
    kwargs = {
        "filters": parsed,
        "unique": str(unique),
        "prefer": str(PreferOrder.DEFAULT),
        "orderby": str(orderby),
        "direction": str(SortDirection.ASC),
        "limit": 100,
    }
    for _ in range(WARMUP):
        method(**kwargs)
    n = 0
    t0 = time.monotonic()
    deadline = t0 + WINDOW_S
    while time.monotonic() < deadline:
        method(**kwargs)
        n += 1
    return (time.monotonic() - t0) / n * 1_000_000  # µs


# ─── Run ──────────────────────────────────────────────────────────────────────

HDR = f"{'query':<22} {'card-level':<11} {'query() µs':>11} {'linear µs':>11}  speedup"
print(HDR)
print("─" * len(HDR))

for label, q_str, orderby, card_level in QUERIES:
    new_us = bench(api._engine.query, q_str, UniqueOn.CARD, orderby)
    old_us = bench(api._engine.query_linear, q_str, UniqueOn.CARD, orderby)
    speedup = old_us / new_us
    flag = "✓" if card_level else "✗"
    print(f"{label:<22} {flag:<11} {new_us:>11.1f} {old_us:>11.1f}  {speedup:.2f}x")

print(f"\nWarmup: {WARMUP} calls  |  Timed window: {WINDOW_S:.0f}s per cell")
