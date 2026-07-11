"""Engine-vs-engine benchmark for card-level border: narrowing planes (docs/issues/engine-border-planes.md).

Times engine.query() for a fixed set of configs against a corpus JSONL export, so the same script
run on two builds (main baseline vs. this branch) produces directly comparable CSVs. No SQL side,
no Docker: build the engine locally (`maturin develop --release -m card_engine/Cargo.toml`) and run:

    .venv/bin/python scripts/bench_border_planes.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/border-planes/baseline-main.csv

Reuses the same corpus JSONL bench_bitplanes.py uses (ENGINE_COLUMNS/schema is unaffected by this
change, so there's no need to re-export from the blue DB).

The `total` column doubles as a parity check: it must be identical across builds for every config,
or the comparison is void. `border:black border:borderless` is a correctness canary, not just a
perf config — total must be 0 on both builds, proving the loose-narrowing-plus-residual-verify
design doesn't reintroduce the shared-witness false-positive bug the tight/printing-space
alternative was rejected for (see the design doc).
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
    # Selective values: real narrowing win expected (10.73%/6.53% of cards respectively).
    ("selective", "border:borderless", "card", "edhrec", "default"),
    ("selective", "border:white", "card", "edhrec", "default"),
    ("selective", "border:borderless", "printing", "edhrec", "default"),
    ("selective", "border:white", "artwork", "edhrec", "default"),
    # And-combos with a card-invariant sibling: narrowing should compose fine.
    ("and-combo", "border:white type:creature", "card", "edhrec", "default"),
    ("and-combo", "border:borderless c:g", "card", "edhrec", "default"),
    # Broad value: expected to decline (98.92% of cards have a black printing) via the existing
    # broadness guard, not a special case — must not regress vs. today's full scan.
    ("broad-decline", "border:black", "card", "edhrec", "default"),
    # Shared-witness correctness canary: two printing-varying border values ANDed can never both
    # be satisfied by the same printing. total must be 0 on both builds.
    ("correctness-canary", "border:black border:borderless", "card", "edhrec", "default"),
    ("correctness-canary", "border:white border:black", "printing", "edhrec", "default"),
    # Unindexed values: expected to stay fully residual, no change expected either way.
    ("unindexed-value", "border:gold", "card", "edhrec", "default"),
    ("unindexed-value", "border:yellow", "card", "edhrec", "default"),
    # Negation: Not only narrows through tight children, and these bits are deliberately loose —
    # expected to stay fully residual, not a regression, just not a win for this shape.
    ("negation", "-border:black", "card", "edhrec", "default"),
    ("negation", "-border:white", "card", "edhrec", "default"),
    # Selective controls: unrelated to border:, must not regress.
    ("control", "name:soldier", "card", "edhrec", "default"),
    ("control", "cmc>6", "card", "cmc", "default"),
    ("control", "oracle:creature", "card", "edhrec", "default"),
    ("control", "t:creature c:g", "card", "edhrec", "default"),
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

    hdr = f"{'group':<20} {'query':<30} {'unique':<9} {'orderby':<8} {'prefer':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
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
                f"{group:<20} {query:<30} {unique:<9} {orderby:<8} {prefer:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
