"""Targeted benchmark for card-space collection fields (type:/kw:/otag:) as PrintingCompose leaves.

`type:goblin or format:legacy` (unique=printing, orderby=usd) cost ~585us on the composite baseline:
the `Subtypes` leaf wasn't a compose leaf, so PrintingCompose declined, AND the near-total
`format:legacy` child tripped generic Or-narrowing's near-total guard — so it fell to a full
`GatheredScan` (gather every card, compute the usd key per card, quickselect). Identical pathology to
bare `format:commander` before #744.

This change projects a card-space collection leaf's card-id postings up to printing space (exact — a
subtype/keyword/oracle-tag is a pure card property) so an Or/And mixing it with legality/range/
border composes into an exact printing bitmap and reaches #744's orderby-range-index walk.

    .venv/bin/python scripts/bench_collection_compose.py \
        --out benchmarks/collection-compose/baseline-main-<sha>.csv

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
    # ── Motivating group: card-space collection OR near-total legality, printing/usd (and rarity) ──
    ("collection_or_legality", "type:goblin or format:legacy", "printing", "usd", "default"),
    ("collection_or_legality", "type:goblin or format:legacy", "printing", "rarity", "default"),
    ("collection_or_legality", "kw:flying or f:commander", "printing", "usd", "default"),
    ("collection_or_legality", "otag:removal or f:modern", "printing", "usd", "default"),
    # unique=card: the composed printing bitmap projects up to a card-existence bitmap.
    ("collection_or_legality", "type:goblin or format:legacy", "card", "usd", "default"),
    # ── Negation (card-space, near-total): the exact complement of the projected set ──
    ("collection_negation", "-type:goblin", "printing", "usd", "default"),
    ("collection_negation", "-type:goblin f:legacy", "printing", "usd", "default"),
    ("collection_negation", "-otag:removal", "printing", "usd", "default"),
    # ── Mid-selectivity card-space collection alone, printing/usd ──
    ("collection_mid", "type:human", "printing", "usd", "default"),
    # ── Controls: must stay flat / must not regress ──
    # #744 bare legality (already fast) — must not regress.
    ("control", "f:commander", "printing", "usd", "default"),
    # Sparse card-space collection alone: cost model keeps it on the narrowing path (A would be slow) —
    # must stay flat, proving the router doesn't route sparse composables to the walk.
    ("control", "type:goblin", "printing", "usd", "default"),
    # Card-space narrowing with a card permutation (edhrec) — unaffected by the printing compose path.
    ("control", "type:goblin", "card", "edhrec", "default"),
    # Positive near-total PRINTING-space leaf: stays on GatheredScan (its scatter estimate = full match
    # count costs high; legality/negations build from the sparse side instead). Out-of-scope known
    # limitation — tracked here so it stays flat, not silently regresses.
    ("control", "is:permanent", "printing", "usd", "default"),
    # General card-mode query, unrelated to collections.
    ("control", "t:creature or t:artifact", "card", "edhrec", "default"),
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

    hdr = f"{'group':<24} {'query':<32} {'unique':<9} {'orderby':<8} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<24} {query:<32} {unique:<9} {orderby:<8} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
