"""Engine-vs-engine benchmark for the legality divergent-card carve-out (#667).

Same protocol as scripts/bench_permuted_order.py (build locally with
`maturin develop --release -m card_engine/Cargo.toml`, run against a fixed corpus, compare CSVs
across builds), reusing its offset-aware bench_one/load_engine directly. Groups:

- `promoted-*` — filters that should reach full plane exactness + popcount-skip once #667 ships:
  format: alone, and format: compounded with other plane-exact predicates (colors/types). These are
  the rows this issue is supposed to move -- especially at deep offsets.
- `deep-*` — the same promoted shapes at large page offsets, where the popcount-skip walk's O(words)
  behavior (flat with offset) should show up most clearly against the O(candidates) baseline.
- `advisory-*` — compounds that must NOT be promoted: format: mixed with a genuinely advisory
  residual (oracle text). Correctness tripwire as much as a performance control.
- `control-*` — banned:/restricted:/absent-format and unrelated selective queries, must not regress.

    .venv/bin/python scripts/bench_legality_divergent.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/legality-divergent/baseline-main.csv
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
from scripts.bench_permuted_order import bench_one, load_engine  # noqa: E402

# (group, query, unique, orderby, prefer, offset) — direction=asc, limit=100 throughout.
CONFIGS: list[tuple[str, str, str, str, str, int]] = [
    # format: alone, across the legal% spread -- #654's own targeted rows, now
    # candidates for all_match + popcount-skip on top of the narrowing they already got.
    ("promoted-solo", "format:modern", "card", "edhrec", "default", 0),
    ("promoted-solo", "format:standard", "card", "edhrec", "default", 0),
    ("promoted-solo", "format:pioneer", "card", "edhrec", "default", 0),
    ("promoted-solo", "-format:modern", "card", "edhrec", "default", 0),
    # #667's own cited motivating case, and variants.
    ("promoted-compound", "format:modern id:g t:creature", "card", "edhrec", "default", 0),
    ("promoted-compound", "format:modern t:creature", "card", "edhrec", "default", 0),
    ("promoted-compound", "format:modern c:g", "card", "edhrec", "default", 0),
    ("promoted-compound", "format:modern or format:pioneer", "card", "edhrec", "default", 0),
    # format + color (not identity) + type -- the literal shape reported as
    # the most common real usage pattern for this engine.
    ("promoted-compound", "format:modern c:g t:creature", "card", "edhrec", "default", 0),
    # Deep pagination on the promoted shapes -- popcount-skip's specific advantage (#634).
    ("deep-offset", "format:modern", "card", "edhrec", "default", 5000),
    ("deep-offset", "format:modern", "card", "edhrec", "default", 15000),
    ("deep-offset", "format:modern id:g t:creature", "card", "edhrec", "default", 5000),
    ("deep-offset", "format:modern t:creature", "card", "edhrec", "default", 15000),
    ("deep-offset", "format:modern c:g t:creature", "card", "edhrec", "default", 5000),
    ("deep-offset", "format:modern c:g t:creature", "card", "edhrec", "default", 15000),
    # unique=printing/artwork: Step 2 stays unique=card-only pending #656 -- must be unaffected.
    ("uniques", "format:modern t:creature", "printing", "edhrec", "default", 0),
    ("uniques", "format:modern t:creature", "artwork", "edhrec", "default", 0),
    # Must NOT promote: format: mixed with a genuinely advisory residual.
    ("advisory-mix", "format:modern o:draw", "card", "edhrec", "default", 0),
    ("advisory-mix", "format:modern oracle:trample", "card", "edhrec", "default", 0),
    # Controls: unindexed by design (banned:/restricted:/absent format), must not move.
    ("control", "banned:modern", "card", "edhrec", "default", 0),
    ("control", "restricted:vintage", "card", "edhrec", "default", 0),
    # Controls: unrelated to legality entirely.
    ("control", "name:soldier", "card", "edhrec", "default", 0),
    ("control", "cmc>6", "card", "cmc", "default", 0),
    ("control", "c:g", "card", "edhrec", "default", 0),
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

    args.out.parent.mkdir(parents=True, exist_ok=True)
    shm_path = args.shm_path or args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)

    for _, query, _, _, _, _ in CONFIGS:
        parse_scryfall_query(query)

    hdr = f"{'group':<18} {'query':<28} {'unique':<9} {'orderby':<8} {'prefer':<9} {'offset':>7} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.1f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "offset", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer, offset in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer, offset), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, offset, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(
                f"{group:<18} {query:<28} {unique:<9} {orderby:<8} {prefer:<9} {offset:>7} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}",
                flush=True,
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
