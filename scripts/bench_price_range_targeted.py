"""Targeted benchmark for PR #690 (price-cents bind-time conversion + NumExpr::eval inlining).

Times engine.query() for a fixed set of configs against a corpus JSONL export, so the same
script run on two builds (main baseline vs. this branch) produces directly comparable CSVs.
Follows scripts/bench_bitplanes.py's pattern.

    .venv/bin/python scripts/bench_price_range_targeted.py \
        --out benchmarks/pr690/baseline-main.csv

The `total` column doubles as a parity check: it must be identical across builds for every
config, or the comparison is void.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.bench_bitplanes import load_engine, bench_one  # noqa: E402

# (group, query, unique, orderby, prefer) — direction=asc, limit=100, offset=0 throughout.
# usd/cn/year/date across card/printing/artwork are what the two commits in this PR target.
# "control" rows are unaffected by either change (no NumericCmp/YearCmp/price at all) and
# must not regress.
CONFIGS: list[tuple[str, str, str, str, str]] = [
    ("usd", "usd<50", "card", "edhrec", "default"),
    ("usd", "usd<50", "printing", "edhrec", "default"),
    ("usd", "usd<50", "artwork", "edhrec", "default"),
    ("usd", "usd>=50", "printing", "edhrec", "default"),
    ("cn", "cn<100", "card", "edhrec", "default"),
    ("cn", "cn<100", "printing", "edhrec", "default"),
    ("cn", "cn<100", "artwork", "edhrec", "default"),
    ("cn", "cn>=100", "card", "edhrec", "default"),
    ("year", "year>2020", "card", "edhrec", "default"),
    ("year", "year>2020", "printing", "edhrec", "default"),
    ("year", "year<2010", "card", "edhrec", "default"),
    ("date", "date>2023-01-01", "card", "edhrec", "default"),
    ("date", "date<2010-01-01", "card", "edhrec", "default"),
    ("compound", "cn<100 t:creature", "card", "edhrec", "default"),
    ("compound", "year>2020 t:creature", "card", "edhrec", "default"),
    ("compound", "usd<50 t:creature", "printing", "edhrec", "default"),
    ("compound", "f:modern usd<50", "card", "edhrec", "default"),
    ("compound", "cn<100 usd<50", "printing", "edhrec", "default"),
    ("control", "f:commander", "card", "edhrec", "default"),
    ("control", "f:legacy", "card", "edhrec", "default"),
    ("control", "f:modern", "card", "edhrec", "default"),
    ("control", "r:common or r:uncommon", "card", "edhrec", "default"),
    ("control", "-border:black", "card", "edhrec", "default"),
    ("control", "t:creature", "printing", "edhrec", "default"),
]


def main() -> None:
    """Load the corpus, time every config, and write the results CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="CSV output path")
    parser.add_argument("--window", type=float, default=8.0, help="timed seconds per config")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None, help="engine archive path (default: alongside --out)")
    args = parser.parse_args()

    rev = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    shm_path = args.shm_path or args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)

    hdr = f"{'group':<9} {'query':<26} {'unique':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<9} {query:<26} {unique:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
