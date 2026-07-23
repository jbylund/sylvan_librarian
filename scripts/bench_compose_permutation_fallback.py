"""Targeted benchmark for the PrintingCompose permutation-fallback idea.

`orderby=rarity`/`usd` have no card-space sort permutation (`SortCol::Rarity`/`PriceUsd` return
`None` — the representative printing depends on `prefer` and cannot be precomputed). Today that
makes `PrintingCompose`/`CardRangePopcount`/`StreamedSelect`/(`PrintingRangeScan`'s non-aligned
case) all decline outright, even though `printing_compose_fastpath` already computes an exact
composed bitmap and total *before* the permutation check — it's just thrown away. Times
engine.query() for a fixed set of (query, unique, orderby) configs so the same script run on two
builds (main baseline vs. this branch) produces directly comparable CSVs.

    .venv/bin/python scripts/bench_compose_permutation_fallback.py \
        --out benchmarks/compose-permutation-fallback/baseline-main-<sha>.csv

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
# `edhrec` rows are controls (already fast via a real permutation; must not regress). `rarity`/`usd`
# rows are the affected case for every group except `control` and `printing`+`usd` (already fast via
# PrintingRangeScan's aligned_page, since predicate and orderby are both usd).
CONFIGS: list[tuple[str, str, str, str, str]] = [
    # Bare usd range: card/artwork are broken on both rarity and usd orderby; printing is broken
    # only on rarity (usd is the aligned case, already fast).
    ("bare_usd", "usd<50", "card", "edhrec", "default"),
    ("bare_usd", "usd<50", "card", "rarity", "default"),
    ("bare_usd", "usd<50", "card", "usd", "default"),
    ("bare_usd", "usd<50", "artwork", "edhrec", "default"),
    ("bare_usd", "usd<50", "artwork", "rarity", "default"),
    ("bare_usd", "usd<50", "artwork", "usd", "default"),
    ("bare_usd", "usd<50", "printing", "edhrec", "default"),
    ("bare_usd", "usd<50", "printing", "rarity", "default"),
    ("bare_usd", "usd<50", "printing", "usd", "default"),
    # Bare cn / date ranges: same shape, no aligned-case escape hatch at all (cn/date are never the
    # orderby), so every non-edhrec row here is affected.
    ("bare_cn", "cn<100", "card", "edhrec", "default"),
    ("bare_cn", "cn<100", "card", "rarity", "default"),
    ("bare_cn", "cn<100", "artwork", "rarity", "default"),
    ("bare_cn", "cn<100", "printing", "rarity", "default"),
    ("bare_year", "year>2020", "card", "edhrec", "default"),
    ("bare_year", "year>2020", "card", "rarity", "default"),
    ("bare_year", "year>2020", "artwork", "rarity", "default"),
    # Range + plane compounds (#733's shared-witness compose case).
    ("compound_range_plane", "usd<50 border:black", "card", "edhrec", "default"),
    ("compound_range_plane", "usd<50 border:black", "card", "rarity", "default"),
    ("compound_range_plane", "usd<50 border:black", "artwork", "rarity", "default"),
    ("compound_range_plane", "cn<100 f:modern", "card", "rarity", "default"),
    # Range + range compound.
    ("compound_range_range", "cn<100 usd<50", "printing", "edhrec", "default"),
    ("compound_range_range", "cn<100 usd<50", "printing", "rarity", "default"),
    ("compound_range_range", "cn<100 usd<50", "card", "rarity", "default"),
    # Controls: plane-only (border/rarity/legality), no range leaf at all — already exact-without-
    # permutation via split_planes + prepare_candidates's own plane handling. Must stay flat.
    ("control", "border:black", "card", "rarity", "default"),
    ("control", "r:rare", "card", "rarity", "default"),
    ("control", "f:modern", "card", "usd", "default"),
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

    hdr = f"{'group':<21} {'query':<22} {'unique':<9} {'orderby':<8} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<21} {query:<22} {unique:<9} {orderby:<8} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
