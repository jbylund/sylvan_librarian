"""Engine-vs-engine benchmark for border card-level narrowing planes.

Run this script twice (baseline main vs branch) against the same corpus JSONL
to compare per-query timings and verify row-count parity.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parsing import parse_scryfall_query  # noqa: E402
from scripts.bench_bitplanes import bench_one, load_engine  # noqa: E402

# (group, query, unique, orderby, prefer) — direction=asc, limit=100, offset=0.
CONFIGS: list[tuple[str, str, str, str, str]] = [
    ("border-solo", "border:black", "card", "edhrec", "default"),
    ("border-solo", "border:borderless", "card", "edhrec", "default"),
    ("border-solo", "border:white", "card", "edhrec", "default"),
    ("border-plus-invariant", "border:black type:creature", "card", "edhrec", "default"),
    ("border-plus-invariant", "border:borderless type:creature", "card", "edhrec", "default"),
    ("border-plus-invariant", "border:white type:creature", "card", "edhrec", "default"),
    ("shared-witness-correctness", "border:black border:borderless", "card", "edhrec", "default"),
    ("unindexed-control", "border:gold", "card", "edhrec", "default"),
    ("unindexed-control", "border:yellow", "card", "edhrec", "default"),
    ("unique-mode", "border:black", "printing", "edhrec", "default"),
    ("unique-mode", "border:black", "artwork", "edhrec", "default"),
    ("control", "type:creature", "card", "edhrec", "default"),
    ("control", "name:angel", "card", "edhrec", "default"),
]


def main() -> None:
    """Load corpus, benchmark border-focused configs, and write CSV output."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="CSV output path")
    parser.add_argument("--window", type=float, default=3.0, help="timed seconds per config")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None, help="engine archive path (default: alongside --out)")
    args = parser.parse_args()

    rev = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", "card_engine/src", "scripts/bench_border_planes.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty:
        rev += "-dirty"
    shm_path = args.shm_path or args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)

    for _, query, _, _, _ in CONFIGS:
        parse_scryfall_query(query)

    hdr = f"{'group':<28} {'query':<34} {'unique':<9} {'orderby':<8} {'prefer':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(
                f"{group:<28} {query:<34} {unique:<9} {orderby:<8} {prefer:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}",
                flush=True,
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
