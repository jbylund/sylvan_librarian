"""Measure the memoize-trigger crossover for text predicates (#624 follow-up).

Query family: `cmc<=K -(oracle:NEEDLE or color:w)` — the cmc index supplies an
exact candidate set whose size K dials (the fabricated variable), while the
Not(Or(text, color)) residual is unnarrowable, so the text predicate runs
either as per-candidate contains() or, when the run_query trigger fires, as a
memoized binary search. Run against a memoize-ALWAYS build and a
memoize-NEVER build; the crossover per needle is where the curves meet, and
needles of different trigram breadth expose how it scales with bind cost.

    .venv/bin/python scripts/bench_memo_crossover.py --label always --out benchmarks/memo-crossover/always.csv
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parsing import parse_scryfall_query  # noqa: E402
from scripts.bench_bitplanes import load_engine  # noqa: E402

NEEDLES = ["deathtouch", "vigilance", "trample", "sacrifice", "draw", "target"]
CMC_STEPS = [0, 1, 2, 3, 4, 5, 6, 8]


def main() -> None:
    """Time the sweep grid and write one CSV row per (needle, K)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--window", type=float, default=0.4)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine = load_engine(args.corpus, args.out.with_suffix(".store"))

    def run(q: str) -> tuple[int, float]:
        f = parse_scryfall_query(q)
        kw = {
            "filters": f,
            "unique": "card",
            "prefer": "default",
            "orderby": "edhrec",
            "direction": "asc",
            "limit": 100,
            "offset": 0,
        }
        total = engine.query(**kw)[0]
        for _ in range(10):
            engine.query(**kw)
        n, t0 = 0, time.monotonic()
        while time.monotonic() < t0 + args.window:
            engine.query(**kw)
            n += 1
        return total, (time.monotonic() - t0) / n * 1000

    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["label", "needle", "cmc_k", "domain", "total", "avg_ms"])
        for needle in NEEDLES:
            for k in CMC_STEPS:
                domain, _ = run(f"cmc<={k}")
                q = f"cmc<={k} -(oracle:{needle} or color:w)"
                total, ms = run(q)
                writer.writerow([args.label, needle, k, domain, total, f"{ms:.4f}"])
                print(f"{args.label:<7} {needle:<11} cmc<={k} domain={domain:>6} {ms:7.3f} ms", flush=True)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
