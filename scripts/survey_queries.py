"""Broad-distribution query survey against the local engine build.

Generates a seeded corpus of queries from client/query_runner.py's weighted
dimension table (the realistic-traffic bias used for load testing), mixed
across shapes — single predicates, 2-4 way ANDs, ORs, nested parens, and
negations — times each against the engine, and prints a distribution report:
percentiles, the slowest tail, and per-dimension / per-shape aggregates.

    .venv/bin/python scripts/survey_queries.py --out benchmarks/survey/survey.csv

Reuses the corpus JSONL and local-build workflow from bench_bitplanes.py.
"""

from __future__ import annotations

import argparse
import csv
import math
import pathlib
import random
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parsing import parse_scryfall_query  # noqa: E402
from client.query_runner import _DIM_NAMES, _DIM_VALUES, _DIM_WEIGHTS  # noqa: E402
from scripts.bench_bitplanes import load_engine  # noqa: E402

# Realistic parameter biases: UI default ordering dominates; unique matches
# query_runner's 75/20/5 split.
_ORDERBYS = ["edhrec"] * 55 + ["usd"] * 10 + ["cmc"] * 10 + ["rarity"] * 8 + ["cubecobra"] * 7 + ["power"] * 5 + ["toughness"] * 5
_UNIQUES = ["card"] * 75 + ["printing"] * 20 + ["artwork"] * 5

WARMUP = 3
WINDOW_S = 0.15
MAX_ITERS = 500


def _pick_fragments(rng: random.Random, k: int) -> list[tuple[str, str]]:
    """Pick k distinct (dimension, fragment) pairs, dimension-weighted."""
    out: list[tuple[str, str]] = []
    frags: set[str] = set()
    while len(out) < k:
        dim = rng.choices(_DIM_NAMES, weights=_DIM_WEIGHTS, k=1)[0]
        frag = rng.choice(_DIM_VALUES[dim])
        if frag not in frags:
            frags.add(frag)
            out.append((dim, frag))
    return out


def generate(rng: random.Random, count: int) -> list[dict]:
    """Generate `count` query specs across shapes, biased to common patterns."""
    shapes = (
        ["single"] * 25
        + ["and2"] * 20
        + ["and3"] * 13
        + ["and4"] * 7
        + ["or2"] * 10
        + ["paren-or"] * 10
        + ["and-or"] * 8
        + ["neg-and"] * 5
        + ["neg-or"] * 2
    )
    specs: list[dict] = []
    seen: set[str] = set()
    while len(specs) < count:
        shape = rng.choice(shapes)
        if shape == "single":
            picks = _pick_fragments(rng, 1)
            q = picks[0][1]
        elif shape in ("and2", "and3", "and4"):
            picks = _pick_fragments(rng, int(shape[-1]))
            q = " ".join(f for _, f in picks)
        elif shape == "or2":
            picks = _pick_fragments(rng, 2)
            q = f"{picks[0][1]} or {picks[1][1]}"
        elif shape == "paren-or":  # (a b) or (c d)
            picks = _pick_fragments(rng, 4)
            q = f"({picks[0][1]} {picks[1][1]}) or ({picks[2][1]} {picks[3][1]})"
        elif shape == "and-or":  # a (b or c)
            picks = _pick_fragments(rng, 3)
            q = f"{picks[0][1]} ({picks[1][1]} or {picks[2][1]})"
        elif shape == "neg-and":  # -a b
            picks = _pick_fragments(rng, 2)
            q = f"-{picks[0][1]} {picks[1][1]}"
        else:  # neg-or: a -(b or c)
            picks = _pick_fragments(rng, 3)
            q = f"{picks[0][1]} -({picks[1][1]} or {picks[2][1]})"
        if q in seen:
            continue
        seen.add(q)
        specs.append(
            {
                "query": q,
                "shape": shape,
                "dims": "+".join(sorted({d for d, _ in picks})),
                "orderby": rng.choice(_ORDERBYS),
                "unique": rng.choice(_UNIQUES),
            }
        )
    return specs


def time_query(engine: object, spec: dict) -> tuple[int, int, float, float]:
    """Return (total, n, avg_ms, min_ms) for one spec."""
    filters = parse_scryfall_query(spec["query"])
    kw = {
        "filters": filters,
        "unique": spec["unique"],
        "prefer": "default",
        "orderby": spec["orderby"],
        "direction": "asc",
        "limit": 100,
        "offset": 0,
    }
    total = engine.query(**kw)[0]
    for _ in range(WARMUP):
        engine.query(**kw)
    n, best = 0, float("inf")
    t_start = time.monotonic()
    now = t_start
    while now < t_start + WINDOW_S and n < MAX_ITERS:
        t0 = time.monotonic()
        engine.query(**kw)
        now = time.monotonic()
        best = min(best, now - t0)
        n += 1
    return total, n, (now - t_start) / n * 1_000, best * 1_000


def percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def report(rows: list[dict]) -> None:
    """Print percentiles, the slow tail, and per-dimension / per-shape aggregates."""
    times = sorted(r["avg_ms"] for r in rows)
    print(f"\n=== {len(rows)} queries ===")
    for p in (50, 75, 90, 95, 99):
        print(f"  p{p}: {percentile(times, p):7.3f} ms")
    print(f"  max: {times[-1]:7.3f} ms")

    print("\n=== slowest 30 ===")
    for r in sorted(rows, key=lambda r: -r["avg_ms"])[:30]:
        print(f"  {r['avg_ms']:7.3f} ms  {r['total']:>6}  {r['unique']:<8} {r['orderby']:<9} [{r['shape']:<8}] {r['query']}")

    def agg(key: str) -> None:
        groups: dict[str, list[float]] = {}
        for r in rows:
            for g in r[key].split("+") if key == "dims" else [r[key]]:
                groups.setdefault(g, []).append(r["avg_ms"])
        print(f"\n=== by {key} ===")
        print(f"  {'group':<16} {'n':>4} {'median':>8} {'p90':>8} {'max':>8}")
        for g, vals in sorted(groups.items(), key=lambda kv: -percentile(sorted(kv[1]), 90)):
            sv = sorted(vals)
            print(f"  {g:<16} {len(sv):>4} {percentile(sv, 50):>8.3f} {percentile(sv, 90):>8.3f} {sv[-1]:>8.3f}")

    agg("dims")
    agg("shape")
    agg("orderby")
    agg("unique")


def main() -> None:
    """Generate the survey corpus, time it, write CSV, print the report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="CSV output path")
    parser.add_argument("--count", type=int, default=400, help="number of queries to generate")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    specs = generate(rng, args.count)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine = load_engine(args.corpus, args.out.with_suffix(".store"))

    rows: list[dict] = []
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["query", "shape", "dims", "unique", "orderby", "total", "n", "avg_ms", "min_ms"])
        for i, spec in enumerate(specs):
            try:
                total, n, avg_ms, min_ms = time_query(engine, spec)
            except Exception as oops:  # noqa: BLE001 — survey must survive odd fragments
                print(f"SKIP {spec['query']!r}: {oops}")
                continue
            rows.append({**spec, "total": total, "avg_ms": avg_ms, "min_ms": min_ms})
            writer.writerow(
                [
                    spec["query"],
                    spec["shape"],
                    spec["dims"],
                    spec["unique"],
                    spec["orderby"],
                    total,
                    n,
                    f"{avg_ms:.4f}",
                    f"{min_ms:.4f}",
                ]
            )
            if (i + 1) % 50 == 0:
                print(f"  …{i + 1}/{len(specs)}", flush=True)

    report(rows)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
