"""Engine-vs-engine benchmark for the sorted name index (order:name + exact-name lookups).

Same protocol as bench_devotion.py. Targeted rows split in two:

* exact  — `!"Name"` lookups (the #1 real-traffic shape per the wild-query
  corpus), currently a per-card full scan with no narrowing arm.
* order-name — queries sorted by name. NOTE: on main there is no name sort
  column, so these rows silently fall back to edhrec — their main-side times
  measure the fallback, not a comparable sort. Totals must still match.

Controls are the usual suspects plus non-name orderings, which must not move.

    .venv/bin/python scripts/bench_name_order.py --out benchmarks/name-order/baseline-main.csv
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

# Each config row is (group, query, unique, orderby, prefer).
CONFIGS: list[tuple[str, str, str, str, str]] = [
    # Targeted: exact-name lookups across name lengths, hit/miss, duplicates
    ("exact", '!"Sol Ring"', "card", "edhrec", "default"),
    ("exact", '!"Lightning Bolt"', "card", "edhrec", "default"),
    ("exact", '!"Fog"', "card", "edhrec", "default"),
    ("exact", '!"Circle of Protection: Green"', "card", "edhrec", "default"),
    ("exact", '!"Ulamog, the Ceaseless Hunger"', "card", "edhrec", "default"),
    ("exact", '!"No Such Card Exists Anywhere"', "card", "edhrec", "default"),
    ("exact", '!"Sol Ring"', "printing", "edhrec", "default"),
    # Targeted: exact-name in composition (candidate algebra)
    ("exact-mix", '!"Sol Ring" or t:goblin', "card", "edhrec", "default"),
    ("exact-mix", '-!"Sol Ring" t:artifact', "card", "edhrec", "default"),
    ("exact-mix", '!"Sol Ring" usd<5', "printing", "usd", "default"),
    # Targeted: order:name (main-side = edhrec fallback, branch = real name sort)
    ("order-name", "t:creature", "card", "name", "default"),
    ("order-name", "c:g", "card", "name", "default"),
    ("order-name", "o:draw", "card", "name", "default"),
    ("order-name", "f:modern", "card", "name", "default"),
    ("order-name", "t:creature", "printing", "name", "default"),
    ("order-name", "t:creature", "artwork", "name", "default"),
    ("order-name", "name:storm", "card", "name", "default"),
    ("order-name", "t:creature", "card", "name", "newest"),
    # Controls: other orderings and the usual suspects — must not move
    ("control", "t:creature", "card", "edhrec", "default"),
    ("control", "c:g", "card", "edhrec", "default"),
    ("control", "o:draw", "card", "edhrec", "default"),
    ("control", "f:modern", "card", "edhrec", "default"),
    ("control", "name:soldier", "card", "edhrec", "default"),
    ("control", "name:fi", "card", "edhrec", "default"),
    ("control", "usd<5", "printing", "usd", "default"),
    ("control", "devotion:uu", "card", "edhrec", "default"),
    ("control", "t:creature pow>3", "card", "cmc", "default"),
]


def main() -> None:
    """Time every config against the local engine build and write the CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="CSV output path")
    parser.add_argument("--window", type=float, default=3.0, help="timed seconds per config")
    args = parser.parse_args()

    rev = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine = load_engine(args.corpus, args.out.with_suffix(".store"))

    hdr = f"{'group':<11} {'query':<34} {'uniq':<8} {'order':<7} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<11} {query:<34} {unique:<8} {orderby:<7} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
