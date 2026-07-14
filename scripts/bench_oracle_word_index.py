"""Engine-vs-engine benchmark for the oracle word index (docs/issues/00663-engine-oracle-word-index.md).

Times engine.query() for a fixed set of configs against a corpus JSONL export, so the same
script run on two builds (main baseline vs. this branch) produces directly comparable CSVs.
No SQL side, no Docker: build the engine locally (`maturin develop --release -m card_engine/Cargo.toml`)
and run:

    .venv/bin/python scripts/bench_oracle_word_index.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/oracle-word-index/baseline-main.csv

Reuses the same corpus JSONL bench_bitplanes.py uses (ENGINE_COLUMNS/schema is unaffected by
this change, so there's no need to re-export from the blue DB).

The `total` column doubles as a parity check: it must be identical across builds for every
config, or the comparison is void.
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
    # Motivating single-word needles from the design doc: common enough that
    # trigram narrowing barely narrows (each constituent trigram 70%+ dense),
    # so today's engine declines memoization and falls back to a full contains()
    # scan. The word index should resolve these exactly and cheaply instead.
    ("single-word", "o:token", "card", "edhrec", "default"),
    ("single-word", "o:creature", "card", "edhrec", "default"),
    ("single-word", "o:target", "card", "edhrec", "default"),
    ("single-word", "o:this", "card", "edhrec", "default"),
    ("single-word", "o:control", "card", "edhrec", "default"),
    ("single-word", "o:whenever", "card", "edhrec", "default"),
    ("single-word", "o:sacrifice", "card", "edhrec", "default"),
    ("single-word", "o:flying", "card", "edhrec", "default"),
    # Less common single words: already reasonably narrowed by trigrams today,
    # must not regress.
    ("single-word-narrow", "o:hexproof", "card", "edhrec", "default"),
    ("single-word-narrow", "o:planeswalker", "card", "edhrec", "default"),
    # Or-combos where an unindexable/broad sibling used to void narrowing for
    # the whole node (#624's motivating shape) — the oracle child should now
    # resolve exactly regardless of the sibling.
    ("or-combo", "o:draw or cn:100", "card", "edhrec", "default"),
    ("or-combo", "o:token or name:storm", "card", "edhrec", "default"),
    ("or-combo", "o:creature or frame:showcase", "card", "edhrec", "default"),
    # And-combos: word-index result composing with other narrowing/plane paths.
    ("and-combo", "o:creature t:artifact", "card", "edhrec", "default"),
    ("and-combo", "o:target c:g", "card", "edhrec", "default"),
    ("and-combo", "o:flying keyword:flying", "card", "edhrec", "default"),
    # Scope limits that must fall through unchanged: multi-word phrases (span
    # tokenization boundaries) and needles <=3 chars (trigram-exact already).
    ("phrase-fallthrough", 'o:"sacrifice a creature"', "card", "edhrec", "default"),
    ("phrase-fallthrough", 'o:"draw a card"', "card", "edhrec", "default"),
    ("short-fallthrough", "o:cat", "card", "edhrec", "default"),
    ("short-fallthrough", "o:fly", "card", "edhrec", "default"),
    # Selective controls: unrelated to oracle text, must not regress.
    ("control", "name:soldier", "card", "edhrec", "default"),
    ("control", "cmc>6", "card", "cmc", "default"),
    ("control", "power>4", "card", "power", "default"),
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

    # parse_scryfall_query is imported to fail fast on a syntax error in a
    # CONFIGS entry before spending time on the run below.
    for _, query, _, _, _ in CONFIGS:
        parse_scryfall_query(query)

    hdr = f"{'group':<20} {'query':<28} {'unique':<9} {'orderby':<8} {'prefer':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
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
                f"{group:<20} {query:<28} {unique:<9} {orderby:<8} {prefer:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
