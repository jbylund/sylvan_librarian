"""Benchmark reservoir vs indexed random-sampling on the Rust card engine.

Plus Python random.sample on a prebuilt list (the old approach's hot path).

Run inside the API container so card_engine is available:

    docker exec arcane_blue-apiservice-1 python3 /app/scripts/bench_random.py

random.sample(list)   Old hot path: O(n) on prebuilt ~30k-dict list.   O(unique_cards) mem (always resident).
sample_reservoir()    Single-pass bounded min-heap.                     O(total x log n) time, O(n) mem.
sample_indexed()      Floyd's index set + targeted single walk.         O(total) time, O(n) mem.
"""

# pylint: disable=protected-access
from __future__ import annotations

import multiprocessing
import random
import sys
import time
import tracemalloc
from typing import TYPE_CHECKING

sys.path.insert(0, "/app")

from api.api_resource import APIResource
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import parse_scryfall_query

if TYPE_CHECKING:
    from collections.abc import Callable

# ─── Config ───────────────────────────────────────────────────────────────────

NS = [1, 12, 30, 60]
WARMUP = 50  # calls to discard before timing
WINDOW = 3.0  # seconds to run per cell
REPEATS = 3  # independent timed windows per cell (report min to reduce noise)

# ─── Setup ────────────────────────────────────────────────────────────────────

print("Connecting to DB and loading engine store…", flush=True)
api = APIResource(last_import_time=multiprocessing.Value("d", time.time(), lock=True))
api._import_recent = lambda: True
api._setup_complete = lambda: True
api._reload_engine(force=True)
total_printings = api._engine.size()
print(f"Engine loaded: {total_printings:,} printings", flush=True)

# Build the prebuilt list that the old implementation kept in memory.
# This mirrors exactly what _fetch_and_cache_preferred_cards() stored.
print("Building prebuilt card list (old approach)…", flush=True)
tracemalloc.start()
snapshot_before = tracemalloc.take_snapshot()
_, prebuilt = api._engine.query(
    filters=parse_scryfall_query(""),
    unique=str(UniqueOn.CARD),
    prefer=str(PreferOrder.DEFAULT),
    orderby=str(CardOrdering.EDHREC),
    direction=str(SortDirection.ASC),
    limit=1_000_000,
)
prebuilt = list(prebuilt)
snapshot_after = tracemalloc.take_snapshot()
tracemalloc.stop()

top_stats = snapshot_after.compare_to(snapshot_before, "lineno")
list_bytes = sum(s.size_diff for s in top_stats if s.size_diff > 0)
print(f"Prebuilt list: {len(prebuilt):,} unique cards, ~{list_bytes / 1024 / 1024:.1f} MB\n", flush=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _time_fn(fn: Callable[[], object], warmup: int, window: float) -> float:
    """Return average µs per call after warmup."""
    for _ in range(warmup):
        fn()
    n_calls = 0
    t0 = time.monotonic()
    deadline = t0 + window
    while time.monotonic() < deadline:
        fn()
        n_calls += 1
    return (time.monotonic() - t0) / n_calls * 1_000_000  # µs


def bench(n: int) -> tuple[float, float, float, float]:
    """Return (py_sample_us, preferred_us, reservoir_us, indexed_us) as min over REPEATS windows."""
    py_timings = [_time_fn(lambda: random.sample(prebuilt, n), WARMUP, WINDOW) for _ in range(REPEATS)]
    pref_timings = [_time_fn(lambda: api._engine.sample_preferred(n), WARMUP, WINDOW) for _ in range(REPEATS)]
    res_timings = [_time_fn(lambda: api._engine.sample_reservoir(n), WARMUP, WINDOW) for _ in range(REPEATS)]
    idx_timings = [_time_fn(lambda: api._engine.sample_indexed(n), WARMUP, WINDOW) for _ in range(REPEATS)]
    return min(py_timings), min(pref_timings), min(res_timings), min(idx_timings)


# ─── Run ──────────────────────────────────────────────────────────────────────

header = f"{'n':>6}  {'python sample µs':>18}  {'preferred µs':>14}  {'reservoir µs':>14}  {'indexed µs':>12}"
print(header)
print("-" * len(header))

for n in NS:
    py_us, pref_us, res_us, idx_us = bench(n)
    print(f"{n:>6}  {py_us:>18.1f}  {pref_us:>14.1f}  {res_us:>14.1f}  {idx_us:>12.1f}")

print(f"\n{WARMUP} warmup + {REPEATS}x{WINDOW:.0f}s windows, best-of-{REPEATS} reported")
print(
    f"Store: {total_printings:,} total printings  |  prebuilt list: {len(prebuilt):,} unique cards  ~{list_bytes / 1024 / 1024:.1f} MB"
)
