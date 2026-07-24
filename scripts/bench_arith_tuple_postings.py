"""Targeted benchmark for arith-expression tuple postings (`power+toughness<4`, `cmc+1<power`).

`NumExpr::Arith`-shaped numeric comparisons over only card-level integer fields
(`cmc`/`power`/`toughness`/`loyalty`) draw from a tiny joint domain (~531-564 distinct
combinations across ~31.5k cards). #743 evaluates the predicate once per distinct combination
against an `ArithTupleIndex` and unions the matching combos' card postings, instead of a full
`GatheredScan` evaluating `NumExpr::eval` per card. Negation recomputes with `Tri::False` (no
complement, so no NULL-inclusion trap). Both polarities are exact and tight in card space.

    .venv/bin/python scripts/bench_arith_tuple_postings.py \
        --out benchmarks/arith-tuple-postings/baseline-main-<sha>.csv

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
    # The two survey-identified motivating queries, at the exact unique/orderby they surfaced at
    # (docs/issues/00743). Before #743 both were full unnarrowed GatheredScans.
    ("motivating", "power+toughness<4", "card", "rarity", "default"),
    ("motivating", "cmc+1<power", "printing", "edhrec", "default"),
    # Same queries across other uniques/orderbys — the narrowing is card-space, so the win should
    # carry regardless of the distinct-on / permutation availability.
    ("motivating", "power+toughness<4", "card", "edhrec", "default"),
    ("motivating", "cmc+1<power", "card", "edhrec", "default"),
    # Negated forms — recomputed with Tri::False, not a complement (the point of the design).
    ("negated", "-power+toughness<4", "card", "rarity", "default"),
    ("negated", "-cmc+1<power", "printing", "edhrec", "default"),
    # Field-vs-field (no arith, no const) and a loyalty predicate — also newly tuple-routed, since
    # neither had a dedicated card-space arm before.
    ("other_shapes", "power<toughness", "card", "edhrec", "default"),
    ("other_shapes", "loyalty>=4", "card", "edhrec", "default"),
    # Compound: an eligible arith predicate AND'd with an unrelated (non-arith) filter — the tuple
    # narrowing should feed the And and hold up.
    ("compound", "power+toughness<4 t:creature", "card", "edhrec", "default"),
    ("compound", "cmc+1<power c:g", "card", "edhrec", "default"),
    # Controls that must hold flat:
    #   - a bare non-arith numeric comparison (dedicated single-field index, untouched path)
    ("control", "power>4", "card", "power", "default"),
    ("control", "cmc>6", "card", "cmc", "default"),
    #   - an out-of-scope arith expr (mixes printing-level usd with card-level power): must decline
    #     to the existing full-scan path, NOT get partially narrowed.
    ("control", "usd+1<power", "card", "edhrec", "default"),
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

    hdr = f"{'group':<13} {'query':<26} {'unique':<9} {'orderby':<8} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<13} {query:<26} {unique:<9} {orderby:<8} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
