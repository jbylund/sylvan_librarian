"""Broad-distribution query survey against the local engine build.

Two sections, both seeded:

* generated — queries from client/query_runner.py's weighted dimension table
  (the realistic-traffic bias used for load testing), mixed across shapes —
  single predicates, 2-4 way ANDs, ORs, nested parens, and negations.
* wild — real searches sampled (weight-proportionally, without replacement)
  from benchmarks/wild-queries/wild-corpus.jsonl, the cleaned Common Crawl
  harvest of scryfall.com/search URLs (see scripts/build_wild_corpus.py).
  Wild rows use the unique/order params that accompanied them in the wild,
  mapped onto what the engine supports.

Times each query against the engine and prints a distribution report:
percentiles, the slowest tail, and per-dimension / per-shape aggregates.

    .venv/bin/python scripts/survey_queries.py --out benchmarks/survey/survey.csv

Reuses the corpus JSONL and local-build workflow from bench_bitplanes.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import random
import re
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parsing import parse_scryfall_query  # noqa: E402
from client.query_runner import _DIM_NAMES, _DIM_VALUES, _DIM_WEIGHTS  # noqa: E402
from scripts.bench_bitplanes import load_engine  # noqa: E402

# tix/eur are excluded from the survey (issue #638, priority low): they are not
# part of our real search traffic, and they have no index, so they'd dominate
# the tail with rows we've decided not to optimize. NOTE: changing this list
# changes the seeded corpus — regenerate baselines when it changes.
_EXCLUDED_DIMS = {"price_tix", "price_eur"}
_DIM_WEIGHTS = [w for w, n in zip(_DIM_WEIGHTS, _DIM_NAMES, strict=False) if n not in _EXCLUDED_DIMS]
_DIM_NAMES = [n for n in _DIM_NAMES if n not in _EXCLUDED_DIMS]

# Realistic parameter biases: UI default ordering dominates; unique matches
# query_runner's 75/20/5 split. prefer/direction/offset skew hard to their
# defaults but are sampled so non-default paths stay on the radar.
_ORDERBYS = ["edhrec"] * 55 + ["usd"] * 10 + ["cmc"] * 10 + ["rarity"] * 8 + ["cubecobra"] * 7 + ["power"] * 5 + ["toughness"] * 5
_UNIQUES = ["card"] * 75 + ["printing"] * 20 + ["artwork"] * 5
_PREFERS = ["default"] * 85 + ["newest"] * 6 + ["oldest"] * 4 + ["usd_low"] * 3 + ["usd_high"] * 2
_DIRECTIONS = ["asc"] * 85 + ["desc"] * 15
_OFFSETS = [0] * 90 + [100] * 7 + [700] * 3

# Fragment pools for shapes query_runner's dimension table has no coverage of:
# arithmetic comparisons (our syntax extension) and regex predicates.
_ARITH_FRAGS = ["power+toughness>8", "cmc+1<power", "pow>=tou", "power+toughness<4", "cmc>=power", "toughness>power"]
_REGEX_FRAGS = ["name:/^gob/", "o:/draw .* cards?/", "name:/dragon$/", "o:/^flying$/", "name:/^[aeiou]/", "o:/sacrifice a/"]
_ANCHOR_P = 0.5  # chance an arith/regex fragment gets a dimension-fragment anchor

WARMUP = 3
WINDOW_S = 0.15
MAX_ITERS = 500

WILD_CORPUS = REPO_ROOT / "benchmarks/wild-queries/wild-corpus.jsonl"
# Fraction of the wild sample drawn from bare name lookups. They dominate the
# wild corpus by weight but are a single engine code path, so they get one
# sixth of the slots and operator queries (many distinct paths) get the rest.
WILD_NAME_LOOKUP_FRACTION = 1 / 6
_OP_RE = re.compile(r"[a-z]+[:<>=]", re.I)
# Wild params → engine params. Orders the engine has no sort column for
# (released, set, color) fall back to edhrec, mirroring orderby_to_col in the
# engine itself. name is Scryfall's default order, which is why it dominates
# the wild data.
_WILD_UNIQUE = {"card": "card", "prints": "printing", "art": "artwork"}
_ENGINE_ORDERS = {"cmc", "power", "rarity", "toughness", "usd", "cubecobra", "edhrec", "name"}


def sample_wild(rng: random.Random, count: int) -> list[dict]:
    """Sample `count` wild queries, weight-proportionally without replacement.

    Name lookups get WILD_NAME_LOOKUP_FRACTION of the slots; operator queries
    take the rest. Uses the Efraimidis-Spirakis reservoir key (rand ** 1/weight)
    for the weighted without-replacement draw.
    """
    ops: list[dict] = []
    names: list[dict] = []
    with WILD_CORPUS.open() as fh:
        for line in fh:
            row = json.loads(line)
            (ops if _OP_RE.search(row["q"]) else names).append(row)
    n_names = int(count * WILD_NAME_LOOKUP_FRACTION)
    picked: list[dict] = []
    for pool, k in ((ops, count - n_names), (names, n_names)):
        keyed = sorted(pool, key=lambda r: rng.random() ** (1 / r["weight"]), reverse=True)
        picked.extend(keyed[:k])
    return [
        {
            "query": r["q"],
            "shape": "wild-op" if _OP_RE.search(r["q"]) else "wild-name",
            "dims": "wild",
            "orderby": r["order"] if r["order"] in _ENGINE_ORDERS else "edhrec",
            "unique": _WILD_UNIQUE[r["unique"]],
            "prefer": "default",
            "direction": "asc",
            "offset": 0,
        }
        for r in picked
    ]


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
        ["single"] * 23
        + ["and2"] * 18
        + ["and3"] * 12
        + ["and4"] * 6
        + ["or2"] * 9
        + ["or3"] * 4
        + ["paren-or"] * 9
        + ["and-of-ors"] * 3
        + ["and-or"] * 7
        + ["neg-and"] * 4
        + ["neg-or"] * 2
        + ["arith"] * 2
        + ["regex"] * 1
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
        elif shape in ("or2", "or3"):
            picks = _pick_fragments(rng, int(shape[-1]))
            q = " or ".join(f for _, f in picks)
        elif shape == "paren-or":  # (a b) or (c d)
            picks = _pick_fragments(rng, 4)
            q = f"({picks[0][1]} {picks[1][1]}) or ({picks[2][1]} {picks[3][1]})"
        elif shape == "and-of-ors":  # (a or b) (c or d)
            picks = _pick_fragments(rng, 4)
            q = f"({picks[0][1]} or {picks[1][1]}) ({picks[2][1]} or {picks[3][1]})"
        elif shape == "and-or":  # a (b or c)
            picks = _pick_fragments(rng, 3)
            q = f"{picks[0][1]} ({picks[1][1]} or {picks[2][1]})"
        elif shape == "neg-and":  # -a b
            picks = _pick_fragments(rng, 2)
            q = f"-{picks[0][1]} {picks[1][1]}"
        elif shape == "neg-or":  # a -(b or c)
            picks = _pick_fragments(rng, 3)
            q = f"{picks[0][1]} -({picks[1][1]} or {picks[2][1]})"
        else:  # arith / regex: the special fragment alone, or anchored by one dim fragment
            frag = rng.choice(_ARITH_FRAGS if shape == "arith" else _REGEX_FRAGS)
            picks = [(shape, frag)]
            q = frag
            if rng.random() < _ANCHOR_P:
                anchor = _pick_fragments(rng, 1)
                q = f"{frag} {anchor[0][1]}"
                picks.extend(anchor)
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
                "prefer": rng.choice(_PREFERS),
                "direction": rng.choice(_DIRECTIONS),
                "offset": rng.choice(_OFFSETS),
            }
        )
    return specs


def time_query(engine: object, spec: dict) -> tuple[int, int, float, float]:
    """Return (total, n, avg_ms, min_ms) for one spec."""
    filters = parse_scryfall_query(spec["query"])
    kw = {
        "filters": filters,
        "unique": spec["unique"],
        "prefer": spec["prefer"],
        "orderby": spec["orderby"],
        "direction": spec["direction"],
        "limit": 100,
        "offset": spec["offset"],
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
    parser.add_argument("--wild", type=int, default=120, help="number of wild-corpus queries to sample")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    specs = generate(rng, args.count)
    if args.wild:
        specs.extend(sample_wild(rng, args.wild))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine = load_engine(args.corpus, args.out.with_suffix(".store"))

    rows: list[dict] = []
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["query", "shape", "dims", "unique", "orderby", "prefer", "direction", "offset", "total", "n", "avg_ms", "min_ms"]
        )
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
                    spec["prefer"],
                    spec["direction"],
                    spec["offset"],
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
