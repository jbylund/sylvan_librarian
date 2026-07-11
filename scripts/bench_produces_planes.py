"""Engine-vs-engine benchmark for produces: bitplanes (docs/issues/engine-produces-planes.md).

Times engine.query() for a fixed set of configs against a corpus JSONL export, so the same script
run on two builds (main baseline vs. this branch) produces directly comparable CSVs. No SQL side,
no Docker: build the engine locally (`maturin develop --release -m card_engine/Cargo.toml`) and run:

    .venv/bin/python scripts/bench_produces_planes.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/produces-planes/baseline-main.csv

Reuses the same corpus JSONL bench_bitplanes.py uses (ENGINE_COLUMNS/schema is unaffected by this
change, so there's no need to re-export from the blue DB).

The `total` column doubles as a parity check: it must be identical across builds for every config,
or the comparison is void.
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

# (group, query, unique, orderby, prefer) — direction=asc, limit=100, offset=0 throughout.
CONFIGS: list[tuple[str, str, str, str, str]] = [
    # Solo predicates, across the color spread (3.38%-3.86% of cards individually).
    # NOTE: deliberately not "produces:c" -- the parser treats the literal "c"/
    # "colorless" as an empty mask for any color-class field (matches Scryfall's
    # own color:/identity: convention: no colored pips), which for produces:
    # means it (incorrectly, pre-existing, out of scope here) matches every
    # card instead of the ~2% that produce {C}. Filed as a separate follow-up.
    ("solo", "produces:g", "card", "edhrec", "default"),
    ("solo", "produces:w", "card", "edhrec", "default"),
    ("solo", "produces:r", "card", "edhrec", "default"),
    # Exact slow-tail queries from the broad survey that motivated this (seed 42).
    ("slow-tail", "oracle:token or produces:b", "card", "edhrec", "default"),
    ("slow-tail", "set:khm or produces:r", "card", "rarity", "default"),
    ("slow-tail", "(set:snc color:rg) or (produces:c usd>1)", "printing", "cmc", "default"),
    # Negation: unlike border:/price_usd, this must narrow fine (card-invariant, exact).
    ("negation", "-produces:g", "card", "edhrec", "default"),
    # And-combos: card-invariant sibling (fully plane-compilable) and printing-space sibling
    # (mixed-space And, exercising the existing Candidates-space-conversion machinery).
    ("and-combo", "produces:g type:land", "card", "edhrec", "default"),
    ("and-combo", "produces:r set:war", "printing", "edhrec", "default"),
    # Controls: unrelated to produces:, must not regress.
    ("control", "name:soldier", "card", "edhrec", "default"),
    ("control", "cmc>6", "card", "cmc", "default"),
    ("control", "oracle:creature", "card", "edhrec", "default"),
    ("control", "border:white", "card", "edhrec", "default"),
]

WARMUP = 20
BATCH_SIZE = 2000


def main() -> None:
    """Load the corpus, time every config, and write the results CSV."""
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
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", "card_engine/src"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if dirty:
        rev += "-dirty"
    shm_path = args.shm_path or args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)

    for _, query, _, _, _ in CONFIGS:
        parse_scryfall_query(query)

    hdr = f"{'group':<12} {'query':<44} {'unique':<9} {'orderby':<8} {'prefer':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
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
                f"{group:<12} {query:<44} {unique:<9} {orderby:<8} {prefer:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
