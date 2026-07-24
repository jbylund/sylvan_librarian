"""Targeted benchmark for exact-postings fields (`set:`/`watermark:`) as compose leaves (#746).

`set:`/`watermark:` are backed by a plain postings `TagIndex`, so before this change they were not
in `is_printing_composable`/`compose_printing_bits`'s leaf table at all: any `And` mixing them with a
range/border/rarity/legality leaf fell out of the cheap `PrintingCompose` plan to a materializing
one. `-set:dmu year:2023` (the motivating query) cost ~0.45ms vs `year:2023` alone at ~0.26ms
despite excluding only 2 of 9,234 matches. Adding `set:` (both polarities — `set_code` is non-null,
so "all-ones minus postings" is exact) and `watermark:`'s positive form (nullable, so only the
positive form composes) as compose leaves lets the whole compound stay in `PrintingCompose`.

    .venv/bin/python scripts/bench_tag_postings_compose.py \
        --out benchmarks/tag-postings-compose/baseline-main-<sha>.csv

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
    # The motivating example, across unique/orderby — before this change the `And` fell out of the
    # PrintingCompose plan because `-set:dmu` was not a compose leaf; `year:2023` alone stayed cheap.
    ("motivating", "-set:dmu year:2023", "printing", "edhrec", "default"),
    ("motivating", "-set:dmu year:2023", "card", "edhrec", "default"),
    ("motivating", "-set:dmu year:2023", "printing", "rarity", "default"),
    ("motivating", "-set:dmu year:2023", "artwork", "edhrec", "default"),
    # Positive-set compound — the same shape without the negation.
    ("positive_and", "set:dmu year:2023", "printing", "edhrec", "default"),
    ("positive_and", "set:dmu year:2023", "card", "edhrec", "default"),
    # Leaf controls: each side of the motivating `And` alone. `year:2023` already composed (range
    # leaf); `set:dmu`/`-set:dmu` are the newly-composable leaves.
    ("leaf", "year:2023", "printing", "edhrec", "default"),
    ("leaf", "set:dmu", "printing", "edhrec", "default"),
    ("leaf", "-set:dmu", "printing", "edhrec", "default"),
    ("leaf", "set:dmu", "card", "edhrec", "default"),
    # Set composed with the other exact leaves (border/rarity) — generalizes for free once `set:` is
    # a leaf, since `is_printing_composable` already recurses through `And`/`Or`.
    ("set_mixed", "set:dmu r:mythic", "printing", "edhrec", "default"),
    ("set_mixed", "-set:dmu border:black", "card", "edhrec", "default"),
    # Watermark: positive form only (nullable field — negated form is deliberately not a compose leaf
    # without a "has any watermark" known-mask). `watermark:` had no compose leaf before either.
    ("watermark", "watermark:wotc", "printing", "edhrec", "default"),
    ("watermark", "watermark:wotc year:2023", "printing", "edhrec", "default"),
    ("watermark", "watermark:wotc r:mythic", "card", "edhrec", "default"),
    # Controls: unaffected shapes, must not regress.
    ("control", "border:black", "card", "rarity", "default"),
    ("control", "t:creature", "card", "edhrec", "default"),
    ("control", "usd<50", "card", "rarity", "default"),
    ("control", "f:modern", "card", "edhrec", "default"),
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

    hdr = f"{'group':<14} {'query':<26} {'unique':<9} {'orderby':<8} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<14} {query:<26} {unique:<9} {orderby:<8} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
