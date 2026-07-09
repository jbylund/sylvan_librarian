"""Forced-branch crossover sweeps for the engine's cost-guard constants.

For each guard (docs/issues/engine-cost-guard-calibration.md) the two code
paths it arbitrates are forced via the CARD_ENGINE_* env overrides — read
once per process through LazyLock, so every (guard, branch, rep) runs in a
fresh subprocess — and the workload knob (exact synthetic selectivity, see
build_guard_corpus.py) is swept across the expected crossover:

- narrow   — MAX_NARROW_FRACTION: bare ``usd<x``, narrow-always vs never.
- stream   — STREAM_MIN_MATCHES: ``cmc<K`` on a permutation orderby,
             streamed vs gathered selection.
- and_skip — AND_SKIP_THRESHOLD: ``cmc<K usd<c``, include-always vs
             skip-always, on independent, correlated, and half corpora.
- bits     — BITS_PROMOTE: ``usd<x or usd>y``, bitmap scatter vs vec merge.

Every row records the query total; totals must be identical across forced
branches (the guards are pure speed dials) or the run is void — the
orchestrator aborts on any parity mismatch. Repetitions interleave the two
branches to cancel thermal/load drift; per-point stats are the median over a
fixed timed window (bench_bitplanes.bench_one pattern, median not mean, since
the box is co-tenanted).

    .venv/bin/python scripts/build_guard_corpus.py --corpus <real.jsonl>
    .venv/bin/python scripts/bench_cost_guards.py run --reps 3
    .venv/bin/python scripts/bench_cost_guards.py analyze
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import pathlib
import statistics
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import card_engine  # noqa: E402
from api.parsing import parse_scryfall_query  # noqa: E402

OUTDIR = REPO_ROOT / "benchmarks/cost-guards"
USIZE_MAX = str(2**64 - 1)

# fancy_env forces the guard's fancy branch (index narrowing / streaming /
# child inclusion / bitmap promotion); simple_env forces the fallback the
# calibration rule prefers to err toward (full scan / gather / skip / merge).
GUARDS: dict[str, dict] = {
    "narrow": {
        "fancy": ("narrow", {"CARD_ENGINE_MAX_NARROW_FRACTION": "1.0"}),
        "simple": ("scan", {"CARD_ENGINE_MAX_NARROW_FRACTION": "0.0", "CARD_ENGINE_NARROW_FLOOR": "0"}),
        "corpora": [("independent", None), ("independent-half", None)],
        "fractions": [0.02, 0.03, 0.05, 0.08, 0.12, 0.18, 0.25, 0.35, 0.5, 0.7],
    },
    "stream": {
        "fancy": ("stream", {"CARD_ENGINE_STREAM_MIN_MATCHES": "0"}),
        "simple": ("gather", {"CARD_ENGINE_STREAM_MIN_MATCHES": USIZE_MAX}),
        "corpora": [("independent", None), ("independent-half", None)],
        "card_targets": [64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 22000],
    },
    "and_skip": {
        "fancy": ("include", {"CARD_ENGINE_AND_SKIP_THRESHOLD": "0"}),
        "simple": ("skip", {"CARD_ENGINE_AND_SKIP_THRESHOLD": USIZE_MAX}),
        "corpora": [
            ("independent", "usd<0.2"),
            ("independent", "usd<0.02"),
            ("correlated", "usd<0.2"),
            ("independent-half", "usd<0.2"),
        ],
        "card_targets": [256, 512, 1024, 2048, 4096, 8192, 16384, 20000],
    },
    "bits": {
        "fancy": ("bits", {"CARD_ENGINE_BITS_PROMOTE": "0"}),
        "simple": ("vec", {"CARD_ENGINE_BITS_PROMOTE": USIZE_MAX}),
        "corpora": [("independent", None)],
        "combined": [256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536],
    },
}

CSV_FIELDS = [
    "rev",
    "guard",
    "corpus",
    "child",
    "branch",
    "rep",
    "knob",
    "query",
    "unique",
    "orderby",
    "total",
    "n",
    "med_ms",
    "min_ms",
]


def bench_one(
    engine: card_engine.QueryEngine, point: tuple[str, str, str], window: float, warmup: int
) -> tuple[int, int, float, float]:
    """Return (total, n, median_ms, min_ms) for one (query, unique, orderby) over a fixed timed window."""
    query, unique, orderby = point
    kw = {
        "filters": parse_scryfall_query(query),
        "unique": unique,
        "prefer": "default",
        "orderby": orderby,
        "direction": "asc",
        "limit": 100,
        "offset": 0,
    }
    total = engine.query(**kw)[0]
    for _ in range(warmup):
        engine.query(**kw)
    samples: list[float] = []
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        t0 = time.perf_counter_ns()
        engine.query(**kw)
        samples.append((time.perf_counter_ns() - t0) / 1e6)
    return total, len(samples), statistics.median(samples), min(samples)


def guard_points(guard: str, spec: dict, child: str | None, n_printings: int, n_cards: int) -> list[tuple[float, str, str, str]]:
    """Return (knob, query, unique, orderby) sweep points for one guard on one corpus."""
    points: list[tuple[float, str, str, str]] = []
    if guard == "narrow":
        points = [(x, f"usd<{x}", "card", "edhrec") for x in spec["fractions"]]
    elif guard == "stream":
        for t in spec["card_targets"]:
            k = min(999, max(1, round(t * 1000 / n_cards)))
            points.append((t, f"cmc<{k}", "card", "edhrec"))
    elif guard == "and_skip":
        for t in spec["card_targets"]:
            if t > 0.7 * n_cards:
                continue
            k = min(999, max(1, round(t * 1000 / n_cards)))
            points.append((t, f"cmc<{k} {child}", "card", "edhrec"))
    elif guard == "bits":
        for c in spec["combined"]:
            s = c // 2
            x, y = s / n_printings, 1 - s / n_printings
            points.append((c, f"usd<{x:.8f} or usd>{y:.8f}", "card", "edhrec"))
    return points


def cmd_worker(args: argparse.Namespace) -> None:
    """Run one (guard, corpus, branch) sweep in this process; append rows to the CSV."""
    engine = card_engine.QueryEngine(str(args.store))
    n_printings = engine.size()
    kw = {
        "filters": parse_scryfall_query("cmc>=0"),
        "unique": "card",
        "prefer": "default",
        "orderby": "edhrec",
        "direction": "asc",
        "limit": 1,
        "offset": 0,
    }
    n_cards = engine.query(**kw)[0]
    spec = GUARDS[args.guard]
    rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout.strip()
    child = args.child or ""
    if args.knobs:  # densification override: comma-separated knob values
        knobs = [float(k) for k in args.knobs.split(",")]
        spec = {**spec, "fractions": knobs, "card_targets": [int(k) for k in knobs], "combined": [int(k) for k in knobs]}
    points = guard_points(args.guard, spec, args.child or None, n_printings, n_cards)
    with args.out.open("a", newline="") as fh:
        writer = csv.writer(fh)
        for knob, query, unique, orderby in points:
            total, n, med_ms, min_ms = bench_one(engine, (query, unique, orderby), args.window, args.warmup)
            writer.writerow(
                [
                    rev,
                    args.guard,
                    args.corpus,
                    child,
                    args.branch,
                    args.rep,
                    knob,
                    query,
                    unique,
                    orderby,
                    total,
                    n,
                    f"{med_ms:.5f}",
                    f"{min_ms:.5f}",
                ]
            )
            fh.flush()
            print(
                f"  {args.guard:<9} {args.corpus:<17} {child:<9} {args.branch:<8} rep{args.rep} knob={knob:<10} total={total:>6} {med_ms:8.4f} ms",
                flush=True,
            )


def check_parity(out: pathlib.Path) -> None:
    """Abort if any (guard, corpus, query) shows different totals across branches."""
    totals: dict[tuple, set] = {}
    with out.open() as fh:
        for row in csv.DictReader(fh):
            totals.setdefault((row["guard"], row["corpus"], row["query"]), set()).add(row["total"])
    bad = {k: v for k, v in totals.items() if len(v) > 1}
    if bad:
        for k, v in bad.items():
            print(f"PARITY FAILURE: {k} -> totals {sorted(v)}", file=sys.stderr)
        sys.exit(1)
    print(f"parity OK: {len(totals)} (guard, corpus, query) groups, all totals branch-identical")


def cmd_run(args: argparse.Namespace) -> None:
    """Spawn fresh-subprocess sweeps for every guard/corpus/branch, interleaved per rep."""
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        out.write_text(",".join(CSV_FIELDS) + "\n")
    for rep in range(1, args.reps + 1):
        for guard, spec in GUARDS.items():
            if args.guards and guard not in args.guards.split(","):
                continue
            branches = [spec["fancy"], spec["simple"]]
            if rep % 2 == 0:  # alternate order to cancel drift
                branches.reverse()
            for corpus, child in spec["corpora"]:
                store = OUTDIR / f"{corpus}.store"
                for branch, env in branches:
                    cmd = [
                        sys.executable,
                        str(pathlib.Path(__file__)),
                        "worker",
                        "--guard",
                        guard,
                        "--corpus",
                        corpus,
                        "--branch",
                        branch,
                        "--rep",
                        str(rep),
                        "--store",
                        str(store),
                        "--out",
                        str(out),
                        "--window",
                        str(args.window),
                        "--warmup",
                        str(args.warmup),
                    ]
                    if child:
                        cmd += ["--child", child]
                    if args.knobs:
                        cmd += ["--knobs", args.knobs]
                    subprocess.run(cmd, check=True, env={**os.environ, **env}, cwd=REPO_ROOT)
    check_parity(out)


def crossovers(rows: list[dict], fancy: str) -> list[float]:
    """Log-interpolated knob positions where med(fancy) - med(simple) changes sign, per rep."""
    reps = sorted({r["rep"] for r in rows})
    found: list[float] = []
    for rep in reps:
        pts: dict[float, dict[str, float]] = {}
        for r in rows:
            if r["rep"] == rep:
                pts.setdefault(float(r["knob"]), {})[r["branch"]] = float(r["med_ms"])
        n_branches = 2
        knobs = sorted(k for k, v in pts.items() if len(v) == n_branches)
        diffs = [pts[k][fancy] - next(v for b, v in pts[k].items() if b != fancy) for k in knobs]
        flips = []
        for (k1, d1), (k2, d2) in zip(zip(knobs, diffs, strict=True), zip(knobs[1:], diffs[1:], strict=True), strict=False):
            if d1 == 0 or d1 * d2 < 0:
                f = d1 / (d1 - d2) if d1 != d2 else 0.0
                flips.append(math.exp(math.log(k1) + f * (math.log(k2) - math.log(k1))))
        if flips:
            found.append(statistics.median(flips))
    return found


def cmd_analyze(args: argparse.Namespace) -> None:
    """Print the per-guard crossover table (median knob position ± spread across reps)."""
    groups: dict[tuple, list[dict]] = {}
    with args.out.open() as fh:
        for row in csv.DictReader(fh):
            groups.setdefault((row["guard"], row["corpus"], row["child"]), []).append(row)
    print(f"{'guard':<9} {'corpus':<17} {'child':<9} {'crossovers (per rep)':<34} {'median':>10} {'spread':>10}")
    for (guard, corpus, child), rows in sorted(groups.items()):
        fancy = GUARDS[guard]["fancy"][0]
        xs = crossovers(rows, fancy)
        if not xs:
            print(f"{guard:<9} {corpus:<17} {child:<9} no crossover in swept range")
            continue
        med = statistics.median(xs)
        spread = (max(xs) - min(xs)) if len(xs) > 1 else 0.0
        print(f"{guard:<9} {corpus:<17} {child:<9} {', '.join(f'{x:.4g}' for x in xs):<34} {med:>10.4g} {spread:>10.4g}")


def main() -> None:
    """Dispatch the run / worker / analyze subcommands."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "worker", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=pathlib.Path, default=OUTDIR / "sweeps.csv")
        p.add_argument("--window", type=float, default=0.25)
        p.add_argument("--warmup", type=int, default=8)
        p.add_argument("--knobs", default="", help="densification override: comma-separated knob values")
        if name == "run":
            p.add_argument("--reps", type=int, default=3)
            p.add_argument("--guards", default="", help="comma-separated subset of guards to run")
        if name == "worker":
            p.add_argument("--guard", required=True, choices=GUARDS)
            p.add_argument("--corpus", required=True)
            p.add_argument("--branch", required=True)
            p.add_argument("--rep", type=int, required=True)
            p.add_argument("--store", type=pathlib.Path, required=True)
            p.add_argument("--child", default="")
    args = parser.parse_args()
    {"run": cmd_run, "worker": cmd_worker, "analyze": cmd_analyze}[args.cmd](args)


if __name__ == "__main__":
    main()
