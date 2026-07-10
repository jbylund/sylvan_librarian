"""Engine-vs-engine benchmark for the permuted-bitmap order phase / exact-candidate promotion (#634).

Same protocol as scripts/bench_bitplanes.py (build locally with
`maturin develop --release -m card_engine/Cargo.toml`, run against a fixed
corpus, compare CSVs across builds) but adds an `offset` axis — #634's Part A
specifically targets deep pagination, which bench_bitplanes.py never exercises
(it hardcodes offset=0). Groups:

- `exact-*`  — filters that should be structurally exact today (once #634 Part B
  ships): pure plane predicates, card-space numeric ranges, ExactName, and
  compounds of these. These are the rows #634 is supposed to move.
- `deep-*`   — the same exact shapes at large page offsets, where Part A's
  popcount-skip should show its biggest win (O(words) instead of O(candidates)).
- `advisory-*` — filters that must NOT be promoted to all_match: oracle text
  (trigram-loose), rarity/legality narrowing-mode sources, mixed exact+advisory
  conjunctions. Included as a correctness/regression tripwire as much as a
  performance control — a promotion bug here would silently return wrong rows,
  not just run slow.
- `control-*` — selective queries unrelated to any of this, must not regress.

    .venv/bin/python scripts/bench_permuted_order.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/permuted-order/baseline-main.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import card_engine  # noqa: E402
from api.parsing import parse_scryfall_query  # noqa: E402

# (group, query, unique, orderby, prefer, offset) — direction=asc, limit=100 throughout.
CONFIGS: list[tuple[str, str, str, str, str, int]] = [
    # Exact today (colors/types via planes, numeric ranges, ExactName) —
    # #634 Part B should let these skip card_pass entirely.
    ("exact-single", "t:creature", "card", "edhrec", "default", 0),
    ("exact-single", "t:instant", "card", "edhrec", "default", 0),
    ("exact-single", "c:g", "card", "edhrec", "default", 0),
    ("exact-single", "cmc<=6", "card", "cmc", "default", 0),
    ("exact-single", "power>4", "card", "power", "default", 0),
    ("exact-single", '!"Lightning Bolt"', "card", "edhrec", "default", 0),
    ("exact-compound", "t:creature power>3", "card", "edhrec", "default", 0),
    ("exact-compound", "c:g t:creature cmc<=4", "card", "edhrec", "default", 0),
    ("exact-compound", "t:creature", "printing", "edhrec", "default", 0),
    ("exact-compound", "t:creature", "artwork", "edhrec", "default", 0),
    # Deep pagination on the same exact shapes — Part A's specific target.
    ("deep-offset", "t:creature", "card", "edhrec", "default", 5000),
    ("deep-offset", "t:creature", "card", "edhrec", "default", 15000),
    ("deep-offset", "c:g", "card", "edhrec", "default", 3000),
    ("deep-offset", "t:creature power>3", "card", "edhrec", "default", 5000),
    ("deep-offset", "cmc<=6", "card", "cmc", "default", 500),
    # Advisory sources that must stay un-promoted: oracle text (trigram-loose),
    # legality (#630 phase 2's divergent carve-out), rarity (narrowing-mode).
    # Also the correctness tripwire: mixed exact+advisory must still verify
    # the advisory part per candidate, never skip it.
    ("advisory-single", "o:draw", "card", "edhrec", "default", 0),
    ("advisory-single", "f:modern", "card", "edhrec", "default", 0),
    ("advisory-single", "r:mythic", "card", "edhrec", "default", 0),
    ("advisory-mixed", "t:creature o:draw", "card", "edhrec", "default", 0),
    ("advisory-mixed", "f:modern t:creature power>3", "card", "edhrec", "default", 0),
    ("advisory-mixed", "c:g r:mythic", "card", "edhrec", "default", 0),
    # Unrelated selective controls — must not regress.
    ("control", "name:soldier", "card", "edhrec", "default", 0),
    ("control", "t:merfolk name:tide", "card", "edhrec", "default", 0),
]

WARMUP = 20
BATCH_SIZE = 2000


def load_engine(corpus: pathlib.Path, shm_path: pathlib.Path) -> card_engine.QueryEngine:
    """Build a fresh engine store from the corpus JSONL via the staged reload API."""
    engine = card_engine.QueryEngine(str(shm_path))
    if not engine.reload_begin():
        msg = "reload_begin returned False (stale archive published concurrently?)"
        raise RuntimeError(msg)
    t0 = time.monotonic()
    batch: list[dict] = []
    with corpus.open() as fh:
        for line in fh:
            batch.append(json.loads(line))
            if len(batch) == BATCH_SIZE:
                engine.add_batch(batch)
                batch.clear()
    if batch:
        engine.add_batch(batch)
    engine.reload_commit()
    print(f"Engine loaded: {engine.size():,} printings in {time.monotonic() - t0:.1f}s", flush=True)
    return engine


def bench_one(
    engine: card_engine.QueryEngine, config: tuple[str, str, str, str, int], window: float
) -> tuple[int, int, float, float]:
    """Return (total, n, avg_ms, min_ms) for one (query, unique, orderby, prefer, offset) config."""
    query, unique, orderby, prefer, offset = config
    filters = parse_scryfall_query(query)
    kw = {
        "filters": filters,
        "unique": unique,
        "prefer": prefer,
        "orderby": orderby,
        "direction": "asc",
        "limit": 100,
        "offset": offset,
    }
    total = engine.query(**kw)[0]
    for _ in range(WARMUP):
        engine.query(**kw)
    n = 0
    best = float("inf")
    t_start = time.monotonic()
    deadline = t_start + window
    now = t_start
    while now < deadline:
        t0 = time.monotonic()
        engine.query(**kw)
        now = time.monotonic()
        best = min(best, now - t0)
        n += 1
    return total, n, (now - t_start) / n * 1_000, best * 1_000


def main() -> None:
    """Load the corpus, time every config, and write the results CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="CSV output path")
    parser.add_argument("--window", type=float, default=0.5, help="timed seconds per config")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None, help="engine archive path (default: alongside --out)")
    args = parser.parse_args()

    rev = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    shm_path = args.shm_path or args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)

    hdr = f"{'group':<16} {'query':<28} {'unique':<9} {'orderby':<8} {'prefer':<9} {'offset':>7} {'total':>7} {'avg ms':>8} {'min ms':>8}"
    print(f"\nrev {rev}, window {args.window:.1f}s per config\n{hdr}\n{'-' * len(hdr)}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rev", "group", "query", "unique", "orderby", "prefer", "offset", "total", "n", "avg_ms", "min_ms"])
        for group, query, unique, orderby, prefer, offset in CONFIGS:
            total, n, avg_ms, min_ms = bench_one(engine, (query, unique, orderby, prefer, offset), args.window)
            writer.writerow([rev, group, query, unique, orderby, prefer, offset, total, n, f"{avg_ms:.4f}", f"{min_ms:.4f}"])
            fh.flush()
            print(
                f"{group:<16} {query:<28} {unique:<9} {orderby:<8} {prefer:<9} {offset:>7} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}",
                flush=True,
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
