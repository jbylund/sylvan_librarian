"""Targeted bench for PR 2a (card-space idea-2, `PrintingRangeBits` for usd).

Build the extension first (`maturin develop --release -m card_engine/Cargo.toml`)
and run:

    .venv/bin/python scripts/bench_range_bits.py --out benchmarks/range-bits/main.csv

Reuses the corpus JSONL and local-build workflow from bench_bitplanes.py. A/B is
same-build off-vs-on via the CARD_ENGINE_RANGE_BITS_CARD kill-switch env var; set
it to 0 for the baseline pass and 1 (default) for the branch pass, same store.

Config tuple is (group, query, unique, orderby, offset); prefer=default,
direction=asc, limit=100 throughout. `total` doubles as a cross-build parity
check — it MUST be identical off vs on (a changed total means the fast path is
fast because it's wrong).
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

# (group, query, unique, orderby, offset). Groups:
#   target-*      — the queries PR 2a should speed up (card-mode range + composable plane)
#   control-*     — must NOT regress (printing ranges own #695; card-invariant planes; selective)
#   correctness-* — range+range card is a shared-witness case; PR 2a must keep it correct AND
#                   not wrongly compose two card-existence bitmaps (total is the parity guard)
CONFIGS: list[tuple[str, str, str, str, int]] = [
    # --- targets: bare card range (broad + narrow + deep page) ---
    ("target-usd-card", "usd<50", "card", "edhrec", 0),
    ("target-usd-card", "usd<50", "card", "edhrec", 700),
    ("target-usd-card", "usd<2", "card", "edhrec", 0),
    # PR 3: cn/date bare ranges, same plan
    ("target-cn-date", "cn<100", "card", "edhrec", 0),
    ("target-cn-date", "cn<100", "card", "edhrec", 700),
    ("target-cn-date", "year>=2015", "card", "edhrec", 0),
    ("target-cn-date", "year<2005", "card", "edhrec", 0),
    ("target-usd-plane", "usd<50 f:modern", "card", "edhrec", 0),
    ("target-usd-plane", "usd<50 t:creature", "card", "edhrec", 0),
    ("target-usd-plane", "usd<50 c:g", "card", "edhrec", 0),
    # --- controls: printing-mode ranges are #695's domain, must stay fast ---
    ("control-printing", "usd<50", "printing", "edhrec", 0),
    ("control-printing", "cn<100", "printing", "edhrec", 0),
    # --- controls: card-invariant planes (already fast, must not regress) ---
    ("control-invariant", "f:modern", "card", "edhrec", 0),
    ("control-invariant", "t:creature", "card", "edhrec", 0),
    ("control-invariant", "c:g", "card", "edhrec", 0),
    # --- controls: selective / name lookup ---
    ("control-selective", "r:rare", "card", "edhrec", 0),
    ("control-selective", '!"Sol Ring"', "card", "edhrec", 0),
    # --- correctness: range+range card (shared-witness); total is the guard ---
    ("correctness-rr", "usd<50 cn<100", "card", "edhrec", 0),
]


def bench_one(engine: card_engine.QueryEngine, config: tuple[str, str, str, int], window: float) -> tuple[int, int, float, float]:
    """Return (total, n, avg_ms, min_ms) for one (query, unique, orderby, offset) config over a timed window."""
    query, unique, orderby, offset = config
    filters = parse_scryfall_query(query)
    kw = {
        "filters": filters,
        "unique": unique,
        "prefer": "default",
        "orderby": orderby,
        "direction": "asc",
        "limit": PAGE_LIMIT,
        "offset": offset,
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
    parser.add_argument("--rev", type=str, default="")
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    shm_path = args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)
    rev = args.rev or "?"

    hdr = f"{'group':<18} {'query':<20} {'unique':<9} {'orderby':<8} {'offset':>6} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(hdr)
    print("-" * len(hdr))
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "offset", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, offset in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, offset), args.window)
            writer.writerow([rev, group, query, unique, orderby, offset, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(
                f"{group:<18} {query:<20} {unique:<9} {orderby:<8} {offset:>6} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
