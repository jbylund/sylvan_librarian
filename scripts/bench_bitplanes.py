"""Engine-vs-engine benchmark for the bitplane match phase (#630).

Times engine.query() for a fixed set of configs against a corpus JSONL export,
so the same script run on two builds (main baseline vs. bitplanes) produces
directly comparable CSVs. No SQL side, no Docker: build the engine locally
(`maturin develop --release -m card_engine/Cargo.toml`) and run:

    .venv/bin/python scripts/bench_bitplanes.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/bitplanes/baseline-main.csv

Export the corpus once from the blue DB (see docs/issues/engine-card-bitplanes.md):

    COLS=$(.venv/bin/python -c "import card_engine; print(', '.join(card_engine.ENGINE_COLUMNS))")
    docker exec sylvan_blue-postgres-1 psql -U foouser -d magic -X -At \
        -c "SELECT row_to_json(t) FROM (SELECT $COLS FROM magic.cards) t" > benchmarks/bitplanes/corpus.jsonl

The `total` column doubles as a parity check: it must be identical across
builds for every config, or the comparison is void.
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

# (group, query, unique, orderby, prefer) — direction=asc, limit=100, offset=0 throughout.
# Groups map to the #630 phases; "control" rows are selective queries that must not regress.
CONFIGS: list[tuple[str, str, str, str, str]] = [
    # Phase 1: pure-plane colors — the motivating case, across uniques and ops
    ("p1-color", "c:g", "card", "edhrec", "default"),
    ("p1-color", "c:g", "printing", "edhrec", "default"),
    ("p1-color", "c:g", "artwork", "edhrec", "default"),
    ("p1-color", "id:g", "card", "edhrec", "default"),
    ("p1-color", "c>=uw", "card", "edhrec", "default"),
    ("p1-color", "c=uw", "card", "edhrec", "default"),
    ("p1-color", "-c:g", "card", "edhrec", "default"),
    ("p1-color", "c:r or c:g", "card", "edhrec", "default"),
    # Phase 1: pure-plane types
    ("p1-type", "t:creature", "card", "edhrec", "default"),
    ("p1-type", "t:creature", "artwork", "edhrec", "default"),
    ("p1-type", "t:instant", "card", "edhrec", "default"),
    ("p1-type", "t:artifact", "card", "edhrec", "default"),
    ("p1-conj", "c:g t:creature", "card", "edhrec", "default"),
    # Phase 1: paths outside permutation streaming (gathered orderby, non-default prefer)
    ("p1-path", "c:g", "card", "usd", "default"),
    ("p1-path", "t:creature", "card", "edhrec", "usd_high"),
    # Phase 2: legality — broad (>99% legal), mid, narrow (<20% legal) formats
    ("p2-legal", "f:commander", "card", "edhrec", "default"),
    ("p2-legal", "f:legacy", "card", "edhrec", "default"),
    ("p2-legal", "f:vintage", "card", "edhrec", "default"),
    ("p2-legal", "f:modern", "card", "edhrec", "default"),
    ("p2-legal", "f:modern", "printing", "edhrec", "default"),
    ("p2-legal", "f:pioneer", "card", "edhrec", "default"),
    ("p2-legal", "f:standard", "card", "edhrec", "default"),
    ("p2-legal", "f:pauper", "card", "edhrec", "default"),
    ("p2-legal", "f:alchemy", "card", "edhrec", "default"),
    ("p2-legal", "f:oldschool", "card", "edhrec", "default"),
    # Phase 2: negated legality (format inversion) — same format spread
    ("p2-legal-not", "-f:commander", "card", "edhrec", "default"),
    ("p2-legal-not", "-f:modern", "card", "edhrec", "default"),
    ("p2-legal-not", "-f:standard", "card", "edhrec", "default"),
    ("p2-legal-not", "-f:oldschool", "card", "edhrec", "default"),
    # Phase 2: composite (#634's motivating fully-index-resolved case) and mixed
    ("p2-legal-mix", "c:g t:creature f:modern", "card", "edhrec", "default"),
    ("p2-legal-mix", "f:modern t:creature power>3", "card", "edhrec", "default"),
    ("p2-legal-mix", "f:pioneer c:ur", "card", "edhrec", "default"),
    ("p2-legal-mix", "f:modern o:draw", "card", "edhrec", "default"),
    ("p2-legal-mix", "f:modern or o:draw", "card", "edhrec", "default"),
    # Phase 2: unindexed status controls — must not regress (banned/restricted stay full-scan)
    ("p2-legal-ctl", "banned:modern", "card", "edhrec", "default"),
    ("p2-legal-ctl", "restricted:vintage", "card", "edhrec", "default"),
    # Phase 3: rarity (narrowing mode) and dense keywords
    ("p3-rarity", "r:mythic", "card", "edhrec", "default"),
    ("p3-rarity", "rarity<=mythic", "card", "edhrec", "default"),
    ("p3-rarity", "rarity>=common", "card", "edhrec", "default"),
    ("p3-keyword", "keyword:flying", "card", "edhrec", "default"),
    # #629: dense per-printing artwork group ids. usd<50/rarity>=common are
    # printing-dependent, so artwork mode can't use the all_match group-count
    # shortcut and pays per-candidate bookkeeping -- the case this issue
    # targets (issue's own acceptance: these should improve; t:creature
    # artwork above, an all_match row, must stay flat).
    ("p629-artwork", "usd<50", "artwork", "edhrec", "default"),
    ("p629-artwork", "rarity>=common", "artwork", "edhrec", "default"),
    ("p629-artwork", "usd<50", "card", "edhrec", "default"),
    ("p629-artwork", "rarity>=common", "card", "edhrec", "default"),
    # Mixed filters: plane bitmap as candidate mask, residual eval over set bits
    ("mixed", "t:creature o:draw", "card", "edhrec", "default"),
    ("mixed", "c:g o:draw", "card", "edhrec", "default"),
    ("mixed", "t:creature c:g o:draw", "card", "edhrec", "default"),
    # Or mixing plane and non-plane children: stays all-residual, must not regress
    ("or-mixed", "c:g or o:draw", "card", "edhrec", "default"),
    # Selective controls: planner must keep these off the plane path unchanged
    ("control", "name:soldier", "card", "edhrec", "default"),
    ("control", "t:merfolk name:tide", "card", "edhrec", "default"),
    ("control", "cmc>6", "card", "cmc", "default"),
    ("control", "power>4", "card", "power", "default"),
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


def bench_one(engine: card_engine.QueryEngine, config: tuple[str, str, str, str], window: float) -> tuple[int, int, float, float]:
    """Return (total, n, avg_ms, min_ms) for one (query, unique, orderby, prefer) config over a fixed timed window."""
    query, unique, orderby, prefer = config
    filters = parse_scryfall_query(query)
    kw = {
        "filters": filters,
        "unique": unique,
        "prefer": prefer,
        "orderby": orderby,
        "direction": "asc",
        "limit": 100,
        "offset": 0,
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
    parser.add_argument("--window", type=float, default=3.0, help="timed seconds per config")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None, help="engine archive path (default: alongside --out)")
    args = parser.parse_args()

    rev = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    shm_path = args.shm_path or args.out.with_suffix(".store")
    engine = load_engine(args.corpus, shm_path)

    hdr = f"{'group':<11} {'query':<26} {'unique':<9} {'orderby':<8} {'prefer':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
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
                f"{group:<11} {query:<26} {unique:<9} {orderby:<8} {prefer:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
