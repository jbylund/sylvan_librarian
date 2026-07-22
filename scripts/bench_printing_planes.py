"""Step-0 baseline for #724 (printing-space bitplanes): current cost of the impacted query types.

How slow are the query types #724's planes would touch, measured on the *current* build (no #724)?

Build the extension first (`maturin develop --release -m card_engine/Cargo.toml`) and run:

    .venv/bin/python scripts/bench_printing_planes.py --out benchmarks/printing-planes/main.csv

Groups:
  bare-*      — bare printing-varying-plane queries under unique=printing. #724's *standalone* win
                (popcount + walk) targets these; measured cost here is what it would remove.
  compound-*  — 2+ printing-varying leaves under unique=printing/card. The *substrate* case — only
                #724 (printing-space AND) answers these exactly; measured cost is the ceiling.
  ref-*       — references: bare-card is already fast via #667+#634 (should NOT be a target);
                artwork shows current cost (needs PR 2b before #724 helps it).

`total` is the row count (sanity). Reuses the corpus + local-build workflow from bench_bitplanes.py.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time
from typing import TYPE_CHECKING

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parsing import parse_scryfall_query  # noqa: E402
from scripts.bench_bitplanes import load_engine  # noqa: E402

if TYPE_CHECKING:
    import card_engine

WARMUP = 50
DEFAULT_WINDOW_S = 8.0
PAGE_LIMIT = 100

# Config columns: group, query, unique, orderby.
CONFIGS: list[tuple[str, str, str, str]] = [
    # --- bare printing-mode plane queries: #724's standalone (popcount+walk) targets ---
    ("bare-legality", "f:modern", "printing", "edhrec"),
    ("bare-legality", "f:standard", "printing", "edhrec"),
    ("bare-legality", "f:commander", "printing", "edhrec"),
    ("bare-border", "border:black", "printing", "edhrec"),
    ("bare-border", "border:borderless", "printing", "edhrec"),
    ("bare-rarity", "r:rare", "printing", "edhrec"),
    ("bare-rarity", "r:mythic", "printing", "edhrec"),
    # --- compound printing-mode: the substrate case (only #724 composes these exactly) ---
    ("compound-printing", "border:black r:rare", "printing", "edhrec"),
    ("compound-printing", "f:modern border:black", "printing", "edhrec"),
    ("compound-printing", "f:modern r:rare", "printing", "edhrec"),
    # --- references: bare-card already fast via #667+#634 (not a #724 target) ---
    ("ref-card-bare", "border:black", "card", "edhrec"),
    ("ref-card-bare", "f:modern", "card", "edhrec"),
    ("ref-card-compound", "border:black r:rare", "card", "edhrec"),
    ("ref-card-compound", "f:modern border:black", "card", "edhrec"),
    # --- artwork: current cost (needs PR 2b before #724 helps) ---
    ("ref-artwork", "border:black", "artwork", "edhrec"),
]


def bench_one(engine: card_engine.QueryEngine, config: tuple[str, str, str], window: float) -> tuple[int, int, float, float]:
    """Return (total, n, avg_ms, min_ms) for one (query, unique, orderby) config over a timed window."""
    query, unique, orderby = config
    filters = parse_scryfall_query(query)
    kw = {
        "filters": filters,
        "unique": unique,
        "prefer": "default",
        "orderby": orderby,
        "direction": "asc",
        "limit": PAGE_LIMIT,
        "offset": 0,
    }
    total = engine.query(**kw)[0]
    for _ in range(WARMUP):
        engine.query(**kw)
    n = 0
    best = float("inf")
    t_start = time.monotonic()
    deadline = t_start + window
    now = t_start
    while now < deadline:
        t0 = time.monotonic()
        engine.query(**kw)
        now = time.monotonic()
        best = min(best, now - t0)
        n += 1
    return total, n, (now - t_start) / n * 1_000, best * 1_000


def main() -> None:
    """Load the corpus once, then time each config over a window and write the CSV."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_S)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine = load_engine(args.corpus, args.out.with_suffix(".store"))

    hdr = f"{'group':<18} {'query':<24} {'unique':<9} {'total':>7} {'min ms':>9}"
    print(hdr)
    print("-" * len(hdr))
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["group", "query", "unique", "orderby", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby), args.window)
            writer.writerow([group, query, unique, orderby, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<18} {query:<24} {unique:<9} {total:>7} {min_ms:>9.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
