"""Old-vs-new cost-guard constants on real data and real (wild) queries.

Validation step for the guard calibration (bench_cost_guards.py): samples a
deduplicated, weight-proportional set of wild queries (the Common Crawl
harvest of real scryfall.com searches, benchmarks/wild-queries/), runs them
against the *real* corpus export, and compares the OLD constants (forced via
CARD_ENGINE_* env overrides) against the NEW baked-in defaults in interleaved
fresh subprocesses. Totals must be identical old-vs-new for every query — the guards
are pure speed dials.

Reports per-query median ratios, the geomean speedup, and the per-rep geomean
spread.

    .venv/bin/python scripts/bench_guard_validation.py run \
        --corpus <real corpus.jsonl> --reps 5
    .venv/bin/python scripts/bench_guard_validation.py analyze
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pathlib
import random
import re
import statistics
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import card_engine  # noqa: E402
from api.parsing import parse_scryfall_query  # noqa: E402

OUTDIR = REPO_ROOT / "benchmarks/cost-guards"

# The pre-calibration constants, forced via env for the "old" branch. The
# "new" branch runs with no overrides, i.e. the defaults baked into the build.
OLD_ENV = {
    "CARD_ENGINE_MAX_NARROW_FRACTION": "0.25",
    "CARD_ENGINE_NARROW_FLOOR": "1000",
    "CARD_ENGINE_AND_SKIP_THRESHOLD": "2048",
    "CARD_ENGINE_BITS_PROMOTE": "4096",
    "CARD_ENGINE_STREAM_MIN_MATCHES": "1024",
}

WARMUP = 3
MAX_ITERS = 400

# Wild params → engine params: unique names differ, and orders the engine has
# no sort column for fall back to edhrec (mirroring orderby_to_col).
_WILD_UNIQUE = {"card": "card", "prints": "printing", "art": "artwork"}
_ENGINE_ORDERS = {"cmc", "power", "rarity", "toughness", "usd", "cubecobra", "edhrec"}
# Bare name lookups dominate the wild corpus by weight but are one engine code
# path; cap them so operator queries (many distinct paths) keep most slots.
_NAME_LOOKUP_FRACTION = 1 / 6
_OP_RE = re.compile(r"[a-z]+[:<>=]", re.IGNORECASE)


def sample_wild(rng: random.Random, wild_corpus: pathlib.Path, count: int) -> list[dict]:
    """Sample wild queries weight-proportionally without replacement (Efraimidis-Spirakis keys)."""
    ops: list[dict] = []
    names: list[dict] = []
    with wild_corpus.open() as fh:
        for line in fh:
            row = json.loads(line)
            (ops if _OP_RE.search(row["q"]) else names).append(row)
    n_names = int(count * _NAME_LOOKUP_FRACTION)
    picked: list[dict] = []
    for pool, k in ((ops, count - n_names), (names, n_names)):
        keyed = sorted(pool, key=lambda r: rng.random() ** (1 / r["weight"]), reverse=True)
        picked.extend(keyed[:k])
    return [
        {
            "query": r["q"],
            "unique": _WILD_UNIQUE[r["unique"]],
            "orderby": r["order"] if r["order"] in _ENGINE_ORDERS else "edhrec",
        }
        for r in picked
    ]


def bench_one(engine: card_engine.QueryEngine, spec: dict, window: float) -> tuple[int, int, float, float]:
    """Return (total, n, median_ms, min_ms) for one query spec over a timed window."""
    kw = {
        "filters": parse_scryfall_query(spec["query"]),
        "unique": spec["unique"],
        "prefer": spec.get("prefer", "default"),
        "orderby": spec["orderby"],
        "direction": spec.get("direction", "asc"),
        "limit": 100,
        "offset": spec.get("offset", 0),
    }
    total = engine.query(**kw)[0]
    for _ in range(WARMUP):
        engine.query(**kw)
    samples: list[float] = []
    deadline = time.monotonic() + window
    while not samples or (time.monotonic() < deadline and len(samples) < MAX_ITERS):
        t0 = time.perf_counter_ns()
        engine.query(**kw)
        samples.append((time.perf_counter_ns() - t0) / 1e6)
    return total, len(samples), statistics.median(samples), min(samples)


def build_query_set(store: pathlib.Path, wild_corpus: pathlib.Path, count: int, seed: int, path: pathlib.Path) -> None:
    """Sample, dedupe, and pre-filter wild queries; write the frozen set to JSON."""
    rng = random.Random(seed)
    specs, seen = [], set()
    for spec in sample_wild(rng, wild_corpus, count * 2):  # oversample: dedupe + parse failures shrink the pool
        if spec["query"] in seen:
            continue
        seen.add(spec["query"])
        specs.append(spec)
    engine = card_engine.QueryEngine(str(store))
    kept = []
    for spec in specs:
        try:
            bench_one(engine, spec, 0.0)
        except Exception as oops:  # noqa: BLE001 — wild strings include unsupported syntax
            print(f"SKIP {spec['query']!r}: {oops}")
            continue
        kept.append(spec)
        if len(kept) == count:
            break
    path.write_text(json.dumps(kept, indent=1) + "\n")
    print(f"query set: {len(kept)} specs -> {path}")


def cmd_worker(args: argparse.Namespace) -> None:
    """Time every query in the frozen set in this process; append rows to the CSV."""
    engine = card_engine.QueryEngine(str(args.store))
    specs = json.loads(args.queries.read_text())
    with args.out.open("a", newline="") as fh:
        writer = csv.writer(fh)
        for qid, spec in enumerate(specs):
            total, n, med_ms, min_ms = bench_one(engine, spec, args.window)
            writer.writerow(
                [
                    args.branch,
                    args.rep,
                    qid,
                    spec["query"],
                    spec["unique"],
                    spec["orderby"],
                    total,
                    n,
                    f"{med_ms:.5f}",
                    f"{min_ms:.5f}",
                ]
            )
    print(f"  {args.branch:<4} rep{args.rep}: {len(specs)} queries", flush=True)


def cmd_run(args: argparse.Namespace) -> None:
    """Build the store + frozen query set, then run interleaved old/new reps."""
    from scripts.bench_bitplanes import load_engine  # noqa: PLC0415 — heavy loader, workers don't need it

    store = OUTDIR / "real.store"
    queries = OUTDIR / "validation-queries.json"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    if not store.exists():
        load_engine(args.corpus, store)
    if not queries.exists():
        build_query_set(store, args.wild_corpus, args.count, args.seed, queries)
    if not args.out.exists():
        args.out.write_text("branch,rep,qid,query,unique,orderby,total,n,med_ms,min_ms\n")
    env_old = json.loads(args.env_old) if args.env_old else OLD_ENV
    env_new = json.loads(args.env_new) if args.env_new else {}
    for rep in range(1, args.reps + 1):
        branches = [("old", env_old), ("new", env_new)]
        if rep % 2 == 0:
            branches.reverse()
        for branch, env in branches:
            cmd = [
                sys.executable,
                str(pathlib.Path(__file__)),
                "worker",
                "--branch",
                branch,
                "--rep",
                str(rep),
                "--store",
                str(store),
                "--queries",
                str(queries),
                "--out",
                str(args.out),
                "--window",
                str(args.window),
            ]
            subprocess.run(cmd, check=True, env={**os.environ, **env}, cwd=REPO_ROOT)
    cmd_analyze(args)


def cmd_analyze(args: argparse.Namespace) -> None:
    """Parity-check totals, then print per-query ratios, geomean, and per-rep spread."""
    rows: list[dict] = []
    with args.out.open() as fh:
        rows = list(csv.DictReader(fh))
    totals: dict[str, set] = {}
    for r in rows:
        totals.setdefault(r["qid"], set()).add(r["total"])
    bad = {k: v for k, v in totals.items() if len(v) > 1}
    if bad:
        for k, v in sorted(bad.items()):
            print(f"PARITY FAILURE qid={k}: totals {sorted(v)}", file=sys.stderr)
        sys.exit(1)
    print(f"parity OK: {len(totals)} queries, totals identical old vs new")

    med: dict[tuple[str, str], list[float]] = {}  # (qid, branch) -> per-rep medians
    meta: dict[str, dict] = {}
    for r in rows:
        med.setdefault((r["qid"], r["branch"]), []).append(float(r["med_ms"]))
        meta[r["qid"]] = r
    qids = sorted({q for q, _ in med}, key=int)
    ratios: dict[str, float] = {}
    for q in qids:
        old, new = statistics.median(med[(q, "old")]), statistics.median(med[(q, "new")])
        ratios[q] = old / new
    geomean = math.exp(statistics.fmean(math.log(r) for r in ratios.values()))
    reps = sorted({int(r["rep"]) for r in rows})
    per_rep = []
    for rep in reps:
        logs = []
        for q in qids:
            o = [float(r["med_ms"]) for r in rows if r["qid"] == q and r["branch"] == "old" and int(r["rep"]) == rep]
            n = [float(r["med_ms"]) for r in rows if r["qid"] == q and r["branch"] == "new" and int(r["rep"]) == rep]
            if o and n:
                logs.append(math.log(o[0] / n[0]))
        per_rep.append(math.exp(statistics.fmean(logs)))
    print(f"geomean speedup (old/new): {geomean:.4f}x  per-rep: {', '.join(f'{g:.4f}' for g in per_rep)}")
    print("\nbiggest wins (old/new ratio):")
    for q in sorted(qids, key=lambda q: -ratios[q])[:12]:
        r = meta[q]
        print(
            f"  {ratios[q]:6.2f}x  {statistics.median(med[(q, 'old')]):8.3f} -> {statistics.median(med[(q, 'new')]):8.3f} ms  total={r['total']:>6}  {r['query']!r}"
        )
    print("\nbiggest regressions:")
    for q in sorted(qids, key=lambda q: ratios[q])[:12]:
        r = meta[q]
        print(
            f"  {ratios[q]:6.2f}x  {statistics.median(med[(q, 'old')]):8.3f} -> {statistics.median(med[(q, 'new')]):8.3f} ms  total={r['total']:>6}  {r['query']!r}"
        )


def main() -> None:
    """Dispatch the run / worker / analyze subcommands."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "worker", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=pathlib.Path, default=OUTDIR / "validation.csv")
        p.add_argument("--window", type=float, default=0.12)
        if name == "run":
            p.add_argument("--corpus", type=pathlib.Path, required=True)
            p.add_argument("--wild-corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/wild-queries/wild-corpus.jsonl")
            p.add_argument("--reps", type=int, default=5)
            p.add_argument("--count", type=int, default=180)
            p.add_argument("--seed", type=int, default=20260708)
            p.add_argument("--env-old", default="", help="JSON env dict for the 'old' branch (default: pre-calibration constants)")
            p.add_argument("--env-new", default="", help="JSON env dict for the 'new' branch (default: baked-in defaults)")
        if name == "worker":
            p.add_argument("--branch", required=True)
            p.add_argument("--rep", type=int, required=True)
            p.add_argument("--store", type=pathlib.Path, required=True)
            p.add_argument("--queries", type=pathlib.Path, required=True)
    args = parser.parse_args()
    {"run": cmd_run, "worker": cmd_worker, "analyze": cmd_analyze}[args.cmd](args)


if __name__ == "__main__":
    main()
