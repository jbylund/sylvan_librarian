"""Engine-vs-engine benchmark for devotion bit-sliced planes + TypeIndex removal.

Same protocol as bench_bitplanes.py. Targeted rows are devotion queries (the
per-card HashMap walk this replaces) across ops, saturation depths, and
compositions. Controls split in two: type-narrowing rows that must not move
when the dead TypeIndex postings are removed, and the usual selective /
broad / plane rows.

    .venv/bin/python scripts/bench_devotion.py --out benchmarks/devotion/baseline-main.csv
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

# (group, query) — unique=card, orderby=edhrec, prefer=default throughout.
CONFIGS: list[tuple[str, str]] = [
    # Targeted: pure devotion across ops and saturation depths
    ("devotion", "devotion:u"),
    ("devotion", "devotion:uu"),
    ("devotion", "devotion:uuu"),
    ("devotion", "devotion:www"),
    ("devotion", "devotion:bbb"),
    ("devotion", "devotion:uuuu"),
    ("devotion", "devotion=uu"),
    ("devotion", "-devotion:uu"),
    ("devotion", "devotion:wwu"),
    # Targeted: devotion in composition (the Or-veto class)
    ("devotion-mix", "type:instant or devotion:b"),
    ("devotion-mix", "pow>2 devotion:bbb"),
    ("devotion-mix", "c:u devotion:uu"),
    ("devotion-mix", "devotion:uu or devotion:bb"),
    ("devotion-mix", "o:draw devotion:gg"),
    # Controls: type rows (TypeIndex removal must be invisible — planes served
    # these since #637)
    ("type-ctl", "t:creature"),
    ("type-ctl", "t:instant"),
    ("type-ctl", "-t:creature"),
    ("type-ctl", "c:g t:creature"),
    ("type-ctl", "t:creature o:draw"),
    ("type-ctl", "t:goblin or t:merfolk"),
    # Controls: the usual suspects
    ("control", "c:g"),
    ("control", "name:fi"),
    ("control", "o:draw"),
    ("control", "usd<5"),
    ("control", "f:modern"),
    ("control", "name:soldier"),
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

    hdr = f"{'group':<13} {'query':<28} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "total", "n", "avg_ms", "min_ms"])
        for group, query in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, "card", "edhrec", "default"), args.window)
            writer.writerow([rev, group, query, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<13} {query:<28} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
