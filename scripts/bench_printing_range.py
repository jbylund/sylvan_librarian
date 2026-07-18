"""Targeted benchmark for the printing-mode sorted-range fastpath (PR 1).

Times engine.query() for a fixed set of configs against a corpus JSONL export, so the same
script run on two builds (main baseline vs. this branch) produces directly comparable CSVs.
Follows scripts/bench_bitplanes.py's pattern; see docs/issues/local-engine-sorted-range-fastpath.md.

    .venv/bin/python scripts/bench_printing_range.py --out benchmarks/printing-range/baseline-main.csv

The `total` column doubles as a parity check: it must be identical across builds for every
config, or the comparison is void.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import subprocess
import sys
import time
from typing import TYPE_CHECKING

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parsing import parse_scryfall_query  # noqa: E402
from scripts.bench_bitplanes import WARMUP, load_engine  # noqa: E402

if TYPE_CHECKING:
    import card_engine

# (group, query, unique, orderby, offset) — direction=asc, limit=100, prefer=default throughout.
CONFIGS: list[tuple[str, str, str, str, int]] = [
    # target: bare broad range under unique=printing (total=k + early-stop walk)
    ("target", "usd<50", "printing", "edhrec", 0),
    ("target", "cn<100", "printing", "edhrec", 0),
    ("target", "year>2020", "printing", "edhrec", 0),
    ("target", "date>2023-01-01", "printing", "edhrec", 0),
    ("target", "usd>=50", "printing", "edhrec", 0),
    ("target", "year<2010", "printing", "edhrec", 0),
    # target: order-by the range field itself (index slice + boundary-bucket sort)
    ("aligned", "usd<50", "printing", "usd", 0),
    ("aligned", "usd<50", "printing", "usd", 5000),
    # target: deep offset (should stay cheap under early-stop)
    ("deep", "usd<50", "printing", "edhrec", 5000),
    # control: card/artwork modes unchanged by PR 1 (must not regress)
    ("control", "usd<50", "card", "edhrec", 0),
    ("control", "usd<50", "artwork", "edhrec", 0),
    ("control", "cn<100", "card", "edhrec", 0),
    # control: non-range queries unaffected (must not regress)
    ("control", "t:creature", "printing", "edhrec", 0),
    ("control", "f:modern", "card", "edhrec", 0),
    ("control", "usd<50 t:creature", "printing", "edhrec", 0),
]


def bench_one(engine: card_engine.QueryEngine, config: tuple[str, str, str, int], window: float) -> tuple[int, int, float, float]:
    """(total, n, avg_ms, min_ms) over a fixed timed window; mirrors bench_bitplanes.bench_one."""
    query, unique, orderby, offset = config
    kw = {
        "filters": parse_scryfall_query(query),
        "unique": unique,
        "prefer": "default",
        "orderby": orderby,
        "direction": "asc",
        "limit": 100,
        "offset": offset,
    }
    total = engine.query(**kw)[0]
    for _ in range(WARMUP):
        engine.query(**kw)
    n, best = 0, float("inf")
    t_start = now = time.monotonic()
    deadline = t_start + window
    while now < deadline:
        t0 = time.monotonic()
        engine.query(**kw)
        now = time.monotonic()
        best = min(best, now - t0)
        n += 1
    return total, n, (now - t_start) / n * 1000, best * 1000


def main() -> None:
    """Load the corpus, time every config, and write the results CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="CSV output path")
    parser.add_argument("--window", type=float, default=8.0, help="timed seconds per config")
    args = parser.parse_args()

    rev = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine = load_engine(args.corpus, args.out.with_suffix(".store"))

    hdr = f"{'group':<8} {'query':<18} {'unique':<9} {'ord':<7} {'off':>5} {'total':>7} {'min ms':>8} {'avg ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s/config\n{hdr}\n{'-' * len(hdr)}", flush=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "offset", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, offset in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, offset), args.window)
            print(
                f"{group:<8} {query:<18} {unique:<9} {orderby:<7} {offset:>5} {total:>7,} {min_ms:>8.3f} {avg_ms:>8.3f}", flush=True
            )
            writer.writerow([rev, group, query, unique, orderby, offset, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])


if __name__ == "__main__":
    main()
