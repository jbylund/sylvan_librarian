"""Engine-vs-engine benchmark for rarity bitplanes (docs/issues/00670-engine-rarity-planes.md).

Times engine.query() for a fixed set of configs against a corpus JSONL export, so the same script
run on two builds (main baseline vs. this branch) produces directly comparable CSVs. No SQL side,
no Docker: build the engine locally (`maturin develop --release -m card_engine/Cargo.toml`) and run:

    .venv/bin/python scripts/bench_rarity_planes.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/rarity-planes/baseline-main.csv

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
    # Solo predicates on the 4 planed values (common/uncommon/rare/mythic).
    ("solo", "r:common", "card", "edhrec", "default"),
    ("solo", "r:uncommon", "card", "edhrec", "default"),
    ("solo", "r:rare", "card", "edhrec", "default"),
    ("solo", "r:mythic", "card", "edhrec", "default"),
    # Near today's MAX_UNION_FRACTION=0.70 ceiling (lib.rs:2080) -- exactly where the
    # postings union already declines to a full scan, so the plane should win biggest here.
    ("ceiling", "rarity<=mythic", "card", "edhrec", "default"),
    ("ceiling", "rarity>=uncommon", "card", "edhrec", "default"),
    # Ne is unconditionally declined by rarity_candidates today (lib.rs:2089-2090) --
    # becomes just as cheap as Eq/Ge/Le once the 4 common values are planes.
    ("ne", "r!=mythic", "card", "edhrec", "default"),
    ("ne", "r!=common", "card", "edhrec", "default"),
    # Mixed plane+postings reconciliation: match set spans a planed value (rare/mythic)
    # and the postings-only tail (special/bonus).
    ("mixed-tail", "r>=rare", "card", "edhrec", "default"),
    ("mixed-tail", "-r:common", "card", "edhrec", "default"),
    # special/bonus stay on the unchanged postings path -- controls, not expected to move.
    ("tail-control", "r:special", "card", "edhrec", "default"),
    ("tail-control", "r:bonus", "card", "edhrec", "default"),
    # Negation on a planed value, no tail involvement (mythic's complement is
    # common/uncommon/rare, all planed).
    ("negation", "-r:mythic", "card", "edhrec", "default"),
    # Compound with an already-planed dimension.
    ("and-combo", "t:creature r:mythic", "card", "edhrec", "default"),
    ("and-combo", "f:modern r:mythic", "card", "edhrec", "default"),
    # printing/artwork uniques -- rarity narrows candidates the same way regardless of mode.
    ("uniques", "r:mythic", "printing", "edhrec", "default"),
    ("uniques", "r:mythic", "artwork", "edhrec", "default"),
    # Controls: unrelated to rarity, must not regress.
    ("control", "name:soldier", "card", "edhrec", "default"),
    ("control", "cmc>6", "card", "cmc", "default"),
    ("control", "oracle:creature", "card", "edhrec", "default"),
    ("control", "c:g", "card", "edhrec", "default"),
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

    hdr = f"{'group':<13} {'query':<24} {'unique':<9} {'orderby':<8} {'prefer':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
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
                f"{group:<13} {query:<24} {unique:<9} {orderby:<8} {prefer:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
