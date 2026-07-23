"""Targeted benchmark for negated-range narrowing (`-usd<c`, `-cn<c`, `-date`/`-year`).

`NOT(x op c) == x negate_op(op) c` is exact under this engine's null semantics (a null-valued
printing fails a direct comparison and its negation both) — `bare_range_bounds` now recognizes this
shape directly (see docs/issues/local-engine-negated-range-narrowing.md), so a negated range
narrows/composes exactly like its already-simplified equivalent, instead of falling to a full scan
regardless of orderby or permutation availability. `usd>=0.25`-shaped rows are the algebraically-
equivalent direct form, included as a target for parity (the negated form should match it, not just
beat its own past self).

    .venv/bin/python scripts/bench_negated_range_narrowing.py \
        --out benchmarks/negated-range/baseline-main-<sha>.csv

Reuses the corpus JSONL and local-build workflow from bench_bitplanes.py. `total` doubles as the
parity check — must be identical across builds for every config.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.bench_bitplanes import bench_one, load_engine  # noqa: E402

# (group, query, unique, orderby, prefer) — direction=asc, limit=100, offset=0 throughout.
CONFIGS: list[tuple[str, str, str, str, str]] = [
    # The motivating example, across orderby and unique — before this fix, none of these narrowed
    # or composed at all (Not wasn't a recognized range shape anywhere), so every row here paid a
    # full scan regardless of whether a permutation existed for the requested orderby.
    ("negated_and", "-usd<0.25 usd<5", "card", "edhrec", "default"),
    ("negated_and", "-usd<0.25 usd<5", "card", "rarity", "default"),
    ("negated_and", "-usd<0.25 usd<5", "printing", "rarity", "default"),
    ("negated_and", "-usd<0.25 usd<5", "artwork", "rarity", "default"),
    # The algebraically-equivalent direct form (usd>=0.25 usd<5) — already fine before this fix
    # (no negation involved); the negated form above should now match it, not just beat itself.
    ("equivalent_direct", "usd>=0.25 usd<5", "card", "edhrec", "default"),
    ("equivalent_direct", "usd>=0.25 usd<5", "card", "rarity", "default"),
    # Bare negated ranges, one per family.
    ("bare_negated", "-usd<50", "card", "rarity", "default"),
    ("bare_negated", "-cn<100", "card", "rarity", "default"),
    ("bare_negated", "-year>2020", "card", "rarity", "default"),
    # Controls: unaffected shapes, must not regress.
    ("control", "usd<50", "card", "rarity", "default"),
    ("control", "border:black", "card", "rarity", "default"),
    ("control", "t:creature", "card", "edhrec", "default"),
]


def main() -> None:
    """Load the corpus, time every config, and write the results CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="CSV output path")
    parser.add_argument("--window", type=float, default=5.0, help="timed seconds per config")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None, help="engine archive path (default: alongside --out)")
    args = parser.parse_args()

    rev = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    shm_path = args.shm_path or args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)

    hdr = f"{'group':<19} {'query':<20} {'unique':<9} {'orderby':<8} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<19} {query:<20} {unique:<9} {orderby:<8} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
