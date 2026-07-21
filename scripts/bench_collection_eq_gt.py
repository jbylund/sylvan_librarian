"""Targeted benchmark for #700: narrow CollectionCmp Eq/Gt via the containment index.

Times engine.query() for a fixed set of `subtype:`/`keyword:`/`otag:`/`art:`/`is:` queries against a
corpus JSONL export, so the same script run on two builds (main baseline vs. branch) produces
directly comparable CSVs. No SQL side, no Docker: build the engine locally
(`maturin develop --release -m card_engine/Cargo.toml`) and run:

    .venv/bin/python scripts/bench_collection_eq_gt.py \
        --corpus benchmarks/bitplanes/corpus.jsonl \
        --out benchmarks/collection-eq-gt/branch-<sha>.csv

Reuses `benchmarks/bitplanes/corpus.jsonl` (97,206 rows) rather than re-exporting -- confirmed its
columns are byte-identical to `card_engine.ENGINE_COLUMNS` for this branch before trusting it.

The `total` column doubles as a parity check: it must be identical across builds for every config,
or the comparison is void (this change is narrowing-only, so `=`/`>` result counts must not move).

Every `eq`/`gt` group query is paired with the corresponding `:` (Ge) query as an in-group control
(already indexed, must not regress) and, where informative, the matching un-optimizable op (`!=`/
`<`/`<=`) as an "excluded-ops" control that must stay on the full scan, unchanged.

Note: this corpus's `card_is_tags` is empty for all 97,206 rows (every `is:` query below returns 0),
so the `istags` group only exercises the exact-empty-postings path (`None if complete`), not the
`Some(v)` loose-narrowing path a populated index would hit. Kept anyway for coverage/regression
purposes; it is not expected to show a timing win.
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

# (group, query, unique, orderby, prefer) -- direction=asc, limit=100, offset=0 throughout.
CONFIGS: list[tuple[str, str, str, str, str]] = [
    # Subtypes (CollField::Subtypes, card-space, complete index). "Human" is the most common
    # subtype in the corpus (10,567 postings) -- big enough that a full scan pays real cost.
    ("subtypes", "subtype=Human", "card", "edhrec", "default"),
    ("subtypes", "subtype>Human", "card", "edhrec", "default"),
    ("subtypes", "subtype:Human", "card", "edhrec", "default"),  # control: already indexed (Ge)
    # Keywords (CollField::Keywords, card-space, complete index). "Flying" is the most common
    # keyword (9,060 postings).
    ("keywords", "keyword=Flying", "card", "edhrec", "default"),
    ("keywords", "keyword>Flying", "card", "edhrec", "default"),
    ("keywords", "kw:Flying", "card", "edhrec", "default"),  # control
    # Oracle tags (CollField::OracleTags, card-space, complete index). "removal" is a mid-size tag
    # (18,275 postings); "triggered-ability" is the broadest tag in the corpus (40,924 postings) --
    # card-space narrowing pays no broadness guard, unlike the printing-space fields below.
    ("otags", "otag=removal", "card", "edhrec", "default"),
    ("otags", "otag>removal", "card", "edhrec", "default"),
    ("otags", "otag:removal", "card", "edhrec", "default"),  # control
    ("otags", "otag=triggered-ability", "card", "edhrec", "default"),
    ("otags", "otag>triggered-ability", "card", "edhrec", "default"),
    ("otags", "otag:triggered-ability", "card", "edhrec", "default"),  # control
    # Art tags (CollField::ArtTags, printing-space, complete index). "human" (20,651 postings) sits
    # under MAX_NARROW_FRACTION's 25% broad-scatter threshold (~24k of 97,206 printings) so it
    # exercises the plain-ids path; "plane" (73,722 postings) sits well over it, exercising the
    # scatter-to-bitmap path instead.
    ("atags", "art=human", "card", "edhrec", "default"),
    ("atags", "art>human", "card", "edhrec", "default"),
    ("atags", "art:human", "card", "edhrec", "default"),  # control
    ("atags", "art=plane", "card", "edhrec", "default"),
    ("atags", "art>plane", "card", "edhrec", "default"),
    ("atags", "art:plane", "card", "edhrec", "default"),  # control
    # Is tags (CollField::IsTags, printing-space, complete index). card_is_tags is empty for every
    # row in this corpus (see module docstring) -- these only cover the exact-empty-postings path.
    ("istags", "is=spell", "card", "edhrec", "default"),
    ("istags", "is>spell", "card", "edhrec", "default"),
    ("istags", "is:spell", "card", "edhrec", "default"),  # control
    # Excluded ops (#700 out of scope: Le/Lt/Ne genuinely can't reuse the containment index) --
    # these must stay full-scan and unchanged before/after.
    ("excluded-ops", "subtype!=Human", "card", "edhrec", "default"),
    ("excluded-ops", "subtype<Human", "card", "edhrec", "default"),
    ("excluded-ops", "subtype<=Human", "card", "edhrec", "default"),
    # Unaffected controls: existing containment / non-collection queries that must not regress.
    ("control", "t:creature", "card", "edhrec", "default"),
    ("control", "cmc>6", "card", "cmc", "default"),
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

    hdr = f"{'group':<13} {'query':<24} {'unique':<9} {'orderby':<8} {'prefer':<9} {'total':>7} {'avg ms':>8} {'min ms':>8}"
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
                f"{group:<13} {query:<24} {unique:<9} {orderby:<8} {prefer:<9} {total:>7} {avg_ms:>8.3f} {min_ms:>8.3f}", flush=True
            )

    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
