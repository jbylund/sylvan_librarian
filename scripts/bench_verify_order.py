"""A/B benchmark for verifier cost ordering (CARD_ENGINE_VERIFY_ORDER).

Two sections, one frozen query set:

* broad — the survey generator's realistic mix (weighted single predicates,
  2-4 way ANDs, ORs, parens, negations, arithmetic, regex) plus wild-corpus
  queries, exactly as scripts/survey_queries.py samples them. This is the
  "did anything regress" net.
* enrich — constructed shapes where child ordering should matter: full-scan
  conjunctions pairing an expensive text predicate (regex / memo-declined
  contains) with a cheap non-narrowable partner (legality, arithmetic, mana
  cost), Or chains mixing tiers, memoized-set size pairs, and controls that
  narrowing already covers (where ordering must change nothing). Conjunction
  shapes appear as spelling PAIRS — expensive-first vs cheap-first — because
  written-order sensitivity is the point: the two spellings differ on the old
  branch and must not on the new one.

Branches run in interleaved fresh subprocesses (the toggle is a
once-per-process LazyLock): old = CARD_ENGINE_VERIFY_ORDER=0 (written order),
new = defaults (cost-ordered). Totals must be identical old-vs-new for every
query — ordering is a pure speed dial.

    .venv/bin/python scripts/bench_verify_order.py run --reps 5
    .venv/bin/python scripts/bench_verify_order.py analyze
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pathlib
import random
import statistics
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.survey_queries as sq  # noqa: E402

OUTDIR = REPO_ROOT / "benchmarks/verify-order"

# Both branches set the toggle explicitly and to equal-length values: an env
# var present in one branch only shifts the process memory layout enough to
# move sub-100us queries a consistent ~15% either way (classic measurement-
# bias artifact; reproduced here on single-predicate queries the toggle
# cannot affect, and eliminated by equalizing the environment).
OLD_ENV = {"CARD_ENGINE_VERIFY_ORDER": "0"}
NEW_ENV = {"CARD_ENGINE_VERIFY_ORDER": "1"}

# Same seeded survey corpus as the standing baselines (survey_queries.py --seed 42).
BROAD_SEED = 42
BROAD_GENERATED = 400
BROAD_WILD = 120

WARMUP = 3
MAX_ITERS = 400

# Constructed enrichment shapes. Pairs are (expensive-first, cheap-first)
# spellings of the same conjunction/disjunction; singles have no order to vary.
# Families:
#   regex-*     — regex conjunct + cheap full-scan partner (legality / arith /
#                 mana cost); the issue's headline shape
#   contains-*  — memo-declined contains (common needle) + cheap partner
#   or-mixed    — Or chains mixing cost tiers (short-circuit on cheap accepts)
#   set-size    — And of two memoized/bound sets of very different sizes
#   control-*   — shapes ordering must NOT change: narrowed conjunctions
#                 (candidates already cover the cheap child), single
#                 predicates, name lookups
_PAIRS: list[tuple[str, str, str]] = [
    # family name, then the expensive-first and cheap-first spellings
    ("regex-legality", "o:/^\\{t\\}: add/ f:standard", "f:standard o:/^\\{t\\}: add/"),
    ("regex-legality", "o:/draw .* cards?/ f:pauper", "f:pauper o:/draw .* cards?/"),
    ("regex-legality-broad", "o:/^\\{t\\}: add/ f:commander", "f:commander o:/^\\{t\\}: add/"),
    ("regex-arith", "o:/draw .* cards?/ power+toughness>8", "power+toughness>8 o:/draw .* cards?/"),
    ("regex-arith", "name:/^[aeiou].*ing$/ cmc+1<power", "cmc+1<power name:/^[aeiou].*ing$/"),
    ("regex-mana", "o:/sacrifice a creature/ mana:{b}{b}", "mana:{b}{b} o:/sacrifice a creature/"),
    ("contains-legality", "o:the f:standard", "f:standard o:the"),
    ("contains-arith", "o:and power+toughness>10", "power+toughness>10 o:and"),
    ("or-mixed", "o:/enters the battlefield tapped/ or t:land", "t:land or o:/enters the battlefield tapped/"),
    ("or-mixed", "o:/^flying$/ or c:g", "c:g or o:/^flying$/"),
    ("set-size", "o:creature flavor:dragon", "flavor:dragon o:creature"),
    ("set-size", 'artist:"rebecca guay" o:enchant', 'o:enchant artist:"rebecca guay"'),
    ("control-narrowed", "o:/^\\{t\\}: add/ t:land c:g", "t:land c:g o:/^\\{t\\}: add/"),
    ("control-narrowed", "o:/storm/ cmc>9", "cmc>9 o:/storm/"),
]
_SINGLES: list[tuple[str, str]] = [
    ("control-single", "c:g"),
    ("control-single", "t:creature c:g"),
    ("control-single", '!"lightning bolt"'),
    ("control-single", "lightning"),
    ("control-single", "o:/^\\{t\\}: add/"),  # lone regex: ordering has nothing to reorder
    ("control-single", "f:modern"),
]


def enrichment_specs() -> list[dict]:
    """Build the constructed-family specs, spelling pairs tagged for symmetry analysis."""
    specs: list[dict] = []
    for pair_id, (family, expensive_first, cheap_first) in enumerate(_PAIRS):
        for variant, q in (("expensive-first", expensive_first), ("cheap-first", cheap_first)):
            specs.append({"query": q, "section": "enrich", "family": family, "pair": pair_id, "variant": variant})
    for family, q in _SINGLES:
        specs.append({"query": q, "section": "enrich", "family": family, "pair": None, "variant": None})
    for s in specs:
        s.update({"unique": "card", "prefer": "default", "orderby": "edhrec", "direction": "asc", "offset": 0})
    return specs


def build_spec_set(store: pathlib.Path, path: pathlib.Path) -> None:
    """Freeze broad (survey-sampled) + enrichment specs that parse and run."""
    import card_engine  # noqa: PLC0415 — heavy import, only run needs it

    rng = random.Random(BROAD_SEED)
    broad = sq.generate(rng, BROAD_GENERATED) + sq.sample_wild(rng, BROAD_WILD)
    for s in broad:
        s.update({"section": "broad", "family": s["shape"], "pair": None, "variant": None})
    specs = broad + enrichment_specs()
    engine = card_engine.QueryEngine(str(store))
    kept, seen = [], set()
    for spec in specs:
        if spec["query"] in seen:
            continue
        seen.add(spec["query"])
        try:
            bench_one(engine, spec, 0.0)
        except Exception as oops:  # noqa: BLE001 — wild strings include unsupported syntax
            print(f"SKIP {spec['query']!r}: {oops}")
            continue
        kept.append(spec)
    path.write_text(json.dumps(kept, indent=1) + "\n")
    n_broad = sum(1 for s in kept if s["section"] == "broad")
    print(f"spec set: {len(kept)} specs ({n_broad} broad, {len(kept) - n_broad} enrich) -> {path}")


def bench_one(engine: object, spec: dict, window: float) -> tuple[int, int, float, float]:
    """Return (total, n, median_ms, min_ms) for one query spec over a timed window."""
    from api.parsing import parse_scryfall_query  # noqa: PLC0415 — worker-only import

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


def cmd_worker(args: argparse.Namespace) -> None:
    """Time every spec in this process (one branch, one rep); append rows."""
    import card_engine  # noqa: PLC0415 — import after the parent set the env

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
                    spec["section"],
                    spec["family"],
                    "" if spec["pair"] is None else spec["pair"],
                    spec["variant"] or "",
                    total,
                    n,
                    f"{med_ms:.5f}",
                    f"{min_ms:.5f}",
                ]
            )
    print(f"  {args.branch:<4} rep{args.rep}: {len(specs)} queries", flush=True)


def cmd_run(args: argparse.Namespace) -> None:
    """Build the store + frozen spec set, then run interleaved old/new reps."""
    from scripts.bench_bitplanes import load_engine  # noqa: PLC0415 — heavy loader, workers don't need it

    store = OUTDIR / "real.store"
    queries = OUTDIR / "specs.json"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    if not store.exists():
        load_engine(args.corpus, store)
    if not queries.exists():
        build_spec_set(store, queries)
    if not args.out.exists():
        args.out.write_text("branch,rep,qid,query,section,family,pair,variant,total,n,med_ms,min_ms\n")
    for rep in range(1, args.reps + 1):
        branches = [("old", OLD_ENV), ("new", NEW_ENV)]
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


def _percentile(sorted_vals: list[float], p: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def cmd_analyze(args: argparse.Namespace) -> None:
    """Parity-check totals, then print percentile, ratio, and pair-symmetry tables."""
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
    # An interrupted run can leave a qid with rows from only one branch;
    # ratios need both, so drop the stragglers rather than KeyError.
    incomplete = [q for q in qids if (q, "old") not in med or (q, "new") not in med]
    if incomplete:
        print(f"skipping {len(incomplete)} queries with rows from only one branch (interrupted run?)", file=sys.stderr)
        qids = [q for q in qids if q not in set(incomplete)]
    old = {q: statistics.median(med[(q, "old")]) for q in qids}
    new = {q: statistics.median(med[(q, "new")]) for q in qids}
    ratios = {q: old[q] / new[q] for q in qids}

    for section in ("broad", "enrich"):
        sect = [q for q in qids if meta[q]["section"] == section]
        if not sect:
            continue
        geomean = math.exp(statistics.fmean(math.log(ratios[q]) for q in sect))
        print(f"\n=== {section}: {len(sect)} queries, geomean speedup (old/new) {geomean:.4f}x ===")
        o = sorted(old[q] for q in sect)
        n = sorted(new[q] for q in sect)
        print(f"  {'pct':<5} {'old':>9} {'new':>9}")
        for p in (50, 75, 90, 95, 99, 100):
            print(f"  p{p:<4} {_percentile(o, p):>9.3f} {_percentile(n, p):>9.3f}")
        print(f"\n  top improvements ({section}):")
        for q in sorted(sect, key=lambda q: -ratios[q])[:12]:
            r = meta[q]
            print(f"  {ratios[q]:6.2f}x  {old[q]:8.3f} -> {new[q]:8.3f} ms  total={r['total']:>6}  {r['query']!r}")
        print(f"\n  top regressions ({section}):")
        for q in sorted(sect, key=lambda q: ratios[q])[:8]:
            r = meta[q]
            print(f"  {ratios[q]:6.2f}x  {old[q]:8.3f} -> {new[q]:8.3f} ms  total={r['total']:>6}  {r['query']!r}")

    _print_pair_symmetry(qids, meta, old, new, ratios)


def _print_pair_symmetry(qids: list[str], meta: dict, old: dict, new: dict, ratios: dict) -> None:
    """Print the spelling-pair symmetry table.

    On the new branch the two spellings of a pair must cost the same; the old
    branch's spread IS the written-order bug.
    """
    pairs: dict[str, dict[str, str]] = {}
    for q in qids:
        r = meta[q]
        if r["pair"]:
            pairs.setdefault(r["pair"], {})[r["variant"]] = q
    if not pairs:
        return
    print("\n=== spelling pairs: expensive-first / cheap-first cost ratio ===")
    print(f"  {'family':<22} {'old spread':>10} {'new spread':>10} {'exp-first speedup':>18}  query")
    for variants in (kv[1] for kv in sorted(pairs.items(), key=lambda kv: int(kv[0]))):
        if set(variants) != {"expensive-first", "cheap-first"}:
            continue
        qe, qc = variants["expensive-first"], variants["cheap-first"]
        old_spread = old[qe] / old[qc]
        new_spread = new[qe] / new[qc]
        print(f"  {meta[qe]['family']:<22} {old_spread:>9.2f}x {new_spread:>9.2f}x {ratios[qe]:>17.2f}x  {meta[qe]['query']!r}")


def main() -> None:
    """Dispatch the run / worker / analyze subcommands."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "worker", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=pathlib.Path, default=OUTDIR / "verify-order.csv")
        p.add_argument("--window", type=float, default=0.15)
        if name == "run":
            p.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
            p.add_argument("--reps", type=int, default=5)
        if name == "worker":
            p.add_argument("--branch", required=True)
            p.add_argument("--rep", type=int, required=True)
            p.add_argument("--store", type=pathlib.Path, required=True)
            p.add_argument("--queries", type=pathlib.Path, required=True)
    args = parser.parse_args()
    {"run": cmd_run, "worker": cmd_worker, "analyze": cmd_analyze}[args.cmd](args)


if __name__ == "__main__":
    main()
