"""Targeted benchmark for the PrintingCompose orderby-range-index walk (#744).

`format:commander`/`format:legacy` bare, `unique=printing`, `orderby=usd`/`rarity` cost ~0.5-0.6ms
even though the legality predicate excludes almost nothing (Commander drops 0.32% of printings).
Two independent causes (see docs/issues/00744-engine-compose-orderby-range-walk.md):

  1. `compose_printing_bits`'s Legality build broadcasts from the *legal* (majority) card plane — an
     effectively full O(n_cards + n_printings) pass for a near-universal format.
  2. `orderby=usd` has no card-space permutation, so paging either visits every candidate
     (`gather_composed_page`, O(n_cards)) or declines composing via `COMPOSE_GATHER_MAX_CARD_FRACTION`
     and falls back to a full `GatheredScan`.

The fix builds from whichever legality side is sparser, and (for `mode==Printing` with a
range-indexed orderby, i.e. `usd`) walks the orderby's own `PrintingRangeIndex`, terminating at
`offset+limit` matches.

    .venv/bin/python scripts/bench_compose_orderby_range_walk.py \
        --out benchmarks/compose-orderby-range-walk/baseline-main-<sha>.csv

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
# `total` doubles as the parity check (identical across builds for every row).
CONFIGS: list[tuple[str, str, str, str, str]] = [
    # ── Motivating group: near-universal legality, printing mode, usd/rarity orderby ──
    # usd is the range-indexed orderby (fixed by the new walk); rarity has no range index and rides
    # only the sparser-side build (part 1) — both listed so the split is visible.
    ("commander_printing", "f:commander", "printing", "usd", "default"),
    ("commander_printing", "f:commander", "printing", "rarity", "default"),
    ("legacy_printing", "f:legacy", "printing", "usd", "default"),
    ("legacy_printing", "f:legacy", "printing", "rarity", "default"),
    # Divergent-heavy near-majority format: exercises the sparser-side build WITH the divergent repair
    # pass still running (modern is 70.7% legal → builds from the absent side, but has divergent cards).
    ("modern_printing", "f:modern", "printing", "usd", "default"),
    ("modern_printing", "f:modern", "printing", "rarity", "default"),
    # Minority-legal format: <50% legal → still builds from the exists side (unchanged arm). Control
    # that the "pick the sparser side" branch leaves the majority-illegal case alone.
    ("pioneer_printing", "f:pioneer", "printing", "usd", "default"),
    ("pioneer_printing", "f:pioneer", "printing", "rarity", "default"),

    # ── Controls: unique=card / artwork (out of scope — prefer-dependent representative). Hold flat. ──
    ("commander_card", "f:commander", "card", "usd", "default"),
    ("commander_card", "f:commander", "card", "rarity", "default"),
    ("commander_artwork", "f:commander", "artwork", "usd", "default"),
    ("commander_artwork", "f:commander", "artwork", "rarity", "default"),
    ("legacy_card", "f:legacy", "card", "usd", "default"),

    # ── Controls: already-fast printing-mode paths. Hold flat. ──
    # Permutation orderby (edhrec) → walk_grouped_page, unaffected by either change.
    ("commander_printing_perm", "f:commander", "printing", "edhrec", "default"),
    # Aligned range: predicate and orderby both usd → PrintingRangeScan's aligned_page.
    ("aligned_usd", "usd<50", "printing", "usd", "default"),
    # Bare border compose, printing mode (precomputed plane, no legality broadcast).
    ("border_printing", "border:black", "printing", "rarity", "default"),
    # Bare usd range, printing/rarity — permutation-free gather fallback (#740), no legality build.
    ("bare_usd_printing", "usd<50", "printing", "rarity", "default"),

    # ── Controls: plane-only card path (split_planes + card popcount). Hold flat. ──
    ("plane_card", "f:modern", "card", "usd", "default"),
    ("plane_card", "f:commander", "card", "edhrec", "default"),
    ("general", "t:creature", "card", "edhrec", "default"),
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

    hdr = f"{'group':<24} {'query':<16} {'unique':<9} {'orderby':<8} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<24} {query:<16} {unique:<9} {orderby:<8} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
