"""Engine-vs-engine benchmark for bind-time text-predicate memoization (#624).

Same protocol as bench_bitplanes.py (corpus JSONL, local maturin build, fixed
configs, totals as a cross-build parity check). Targeted rows are full-scan
queries carrying an indexable text predicate — Or-shapes whose sibling can't
narrow (broad-guarded usd, arithmetic, devotion, 2-char names, plane-mixed
Ors). Controls are narrowable/pure-plane queries the trigger must leave
untouched, plus full-scan queries with no text predicate (pass overhead).

    .venv/bin/python scripts/bench_text_memo.py --out benchmarks/text-memo/baseline-main.csv
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
    # Targeted: full-scan Or with a broad-guarded / unindexable sibling
    ("target", "oracle:deathtouch or usd<5"),
    ("target", "oracle:draw or usd<5"),
    ("target", "oracle:draw or power+toughness>8"),
    ("target", "oracle:lifelink or devotion:g"),
    ("target", "oracle:flying or name:fi"),
    ("target", "name:dragon or usd<5"),
    ("target", "c:g or o:draw"),
    ("target", "oracle:sacrifice or frame:showcase"),
    ("target", "o:the or devotion:g"),
    ("target", "name:angel or power+toughness>9"),
    # Controls: narrowable / pure-plane / no-text full scans — must not move
    ("control", "o:draw"),
    ("control", "o:draw t:creature"),
    ("control", "c:g"),
    ("control", "name:soldier"),
    ("control", "t:merfolk name:tide"),
    ("control", "usd<5"),
    ("control", "power+toughness>8"),
    ("control", "(t:bird c:u) or (t:beast c:g)"),
    ("control", "c=uw o:draw"),
    ("control", "f:modern"),
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

    hdr = f"{'group':<8} {'query':<36} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.0f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "total", "n", "avg_ms", "min_ms"])
        for group, query in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, "card", "edhrec", "default"), args.window)
            writer.writerow([rev, group, query, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(f"{group:<8} {query:<36} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True)

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
