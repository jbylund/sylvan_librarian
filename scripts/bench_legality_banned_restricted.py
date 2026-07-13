"""Engine-vs-engine benchmark for banned:/restricted: legality planes (#678).

Same protocol as scripts/bench_legality_divergent.py (build locally with
`maturin develop --release -m card_engine/Cargo.toml`, run against a fixed corpus, compare CSVs
across builds), reusing its offset-aware bench_one/load_engine directly. Groups:

- `promoted-*` — banned:/restricted: filters that should reach exact plane narrowing once #678
  ships: solo, negated, and compounded with other plane-exact predicates.
- `same-format-cross-status` / `cross-format` — shapes that must decline the plane path (shared-
  witness) both before and after this change, falling back to the (still correct) narrow_rec arm.
- `divergent` — restricted:oldschool, the one printing-varying case found in the real corpus.
- `uniques` — unique=printing/artwork must be unaffected the same way format: already is.
- `control` — format:/c:g, already exact via #676, must show zero movement from this change.

    .venv/bin/python scripts/bench_legality_banned_restricted.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/legality-banned-restricted/baseline-main.csv
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
    # Solo banned:/restricted:, across the size spread (card-level counts from
    # engine-legality-bitplanes.md's table): duel banned=91 (largest banned set),
    # vintage restricted=51 (largest restricted set), modern banned=52 (the issue's
    # own cited example), a near-zero set (alchemy banned=1).
    ("promoted-solo", "banned:duel", "card", "edhrec", "default", 0),
    ("promoted-solo", "restricted:vintage", "card", "edhrec", "default", 0),
    ("promoted-solo", "banned:modern", "card", "edhrec", "default", 0),
    ("promoted-solo", "banned:alchemy", "card", "edhrec", "default", 0),
    ("promoted-solo", "-banned:duel", "card", "edhrec", "default", 0),
    ("promoted-solo", "-restricted:vintage", "card", "edhrec", "default", 0),
    # Compounded with other plane-exact predicates -- must fully resolve to
    # all_match/popcount-skip, same as format:modern t:creature does today.
    ("promoted-compound", "banned:modern t:creature", "card", "edhrec", "default", 0),
    ("promoted-compound", "restricted:vintage c:u", "card", "edhrec", "default", 0),
    ("promoted-compound", "banned:duel or restricted:vintage", "card", "edhrec", "default", 0),
    # Same-format, cross-status: two distinct existence facts about one format --
    # must decline the plane And (shared-witness) exactly like format:A AND -format:A.
    ("same-format-cross-status", "banned:modern restricted:modern", "card", "edhrec", "default", 0),
    ("same-format-cross-status", "format:modern banned:modern", "card", "edhrec", "default", 0),
    # Cross-format: must decline regardless of which statuses are involved.
    ("cross-format", "banned:modern banned:legacy", "card", "edhrec", "default", 0),
    ("cross-format", "format:modern banned:legacy", "card", "edhrec", "default", 0),
    # The one genuinely printing-varying case found in the real corpus (21 card
    # names, all via 30th Anniversary Edition / Vintage Championship promo prints).
    ("divergent", "restricted:oldschool", "card", "edhrec", "default", 0),
    ("divergent", "-restricted:oldschool", "card", "edhrec", "default", 0),
    # unique=printing/artwork: must stay correct via the existing row-selection
    # machinery (already status-agnostic), same as format: today.
    ("uniques", "banned:modern", "printing", "edhrec", "default", 0),
    ("uniques", "restricted:oldschool", "printing", "edhrec", "default", 0),
    ("uniques", "banned:modern", "artwork", "edhrec", "default", 0),
    # Controls: format:/c:g, already plane-exact via #676 -- zero movement expected.
    ("control", "format:modern", "card", "edhrec", "default", 0),
    ("control", "c:g", "card", "edhrec", "default", 0),
    ("control", "name:soldier", "card", "edhrec", "default", 0),
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

    hdr = f"{'group':<25} {'query':<32} {'unique':<9} {'orderby':<8} {'prefer':<9} {'offset':>7} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.1f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "offset", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer, offset in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer, offset), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, offset, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(
                f"{group:<25} {query:<32} {unique:<9} {orderby:<8} {prefer:<9} {offset:>7} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}",
                flush=True,
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
