"""Predicted/measured per plan and acquire route, under BOTH denominators, on picked rows.

Two agents reported ratios for the same cells that do not reconcile -- 1.767 against 1.319 for
StreamedSelect on `printing_compose`. The cause is almost certainly the DENOMINATOR, not the route:

  plan_self_ns  = executor phases PLUS `ns_prepare` on a RANGE_ACQUIRES route (`printing_compose`,
                  `printing_range_scan`, `card_range_popcount`), because on those the router only
                  ESTIMATED and dispatch pays the candidate build itself. This is what actually
                  becomes latency, and what `bench_error_attribution_weighted` scores.
  executor only = `ns_setup + ns_loop + ns_finish`, i.e. what the cost arm's terms actually describe,
                  since `plan_cost` has no build term at all.

The gap between the two IS the unpriced build -- the term Round 80 identified as ~50% of
GatheredScan's error mass. So reporting one number without saying which denominator it used makes a
plan look calibrated or broken depending on a choice nobody stated. Both are printed here, side by
side, so a round is planned against the right one.

**The columns are independent medians and do not compose.** Each is a median over rows sorted by a
different quantity, so `p/executor` divided by `p/plan_self` is NOT the prep share, and the printed
prep share will not reconcile them -- 12.6% cannot explain two ratios 1.76x apart. Compare a column
against itself across builds; never divide one column by another. An agent reading this table nearly
derived a coefficient that way.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import random
import statistics
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts import costbench  # noqa: E402
from scripts.costbench import Budget, iter_samples, load_engine, plan_self_ns  # noqa: E402

#: Below this a measured time is timer resolution, not a signal. Values quantize to ~41.67 ns ticks.
MIN_MEASURED_NS = 500.0
#: Cells thinner than this are not printed.
MIN_ROWS = 25
DEFAULT_N_QUERIES = 6000


def main() -> None:
    """Report predicted/measured per plan and acquire under both denominators."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-queries", type=int, default=DEFAULT_N_QUERIES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    args = parser.parse_args()

    engine = load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".ratios.store"))
    sampler = QuerySampler(args.corpus, args.mode)
    budget = Budget(sample=args.n_queries, warmups=costbench.NUM_WARMUPS, trials=costbench.NUM_TRIALS)

    cells: dict[tuple[str, str], list[tuple[float, float]]] = collections.defaultdict(list)
    prep_share: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for sample in iter_samples(engine, sampler, random.Random(args.seed), budget, vary_prefer=True):
        acq = sample.acquire
        picked = next((p for p in sample.plans if p.get("picked")), None)
        if picked is None or not picked.get("trials_ns"):
            continue
        self_ns = plan_self_ns(picked, acq)
        exec_ns = float(picked["ns_setup"] + picked["ns_loop"] + picked["ns_finish"])
        pred = picked.get("predicted_ns")
        if not self_ns or self_ns < MIN_MEASURED_NS or exec_ns < MIN_MEASURED_NS or not pred or pred <= 0:
            continue
        key = (picked["plan"], acq["count_source"])
        cells[key].append((pred / self_ns, pred / exec_ns))
        prep_share[key].append((self_ns - exec_ns) / self_ns)

    print(f"\npicked rows by cell, {args.n_queries:,} {args.mode} queries, prefer varied\n")
    print(f"{'plan':<18} {'acquire':<22} {'n':>6} {'p/plan_self':>12} {'p/executor':>11} {'prep share':>11}")
    for (plan, acq), vals in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        if len(vals) < MIN_ROWS:
            continue
        self_r = statistics.median(v[0] for v in vals)
        exec_r = statistics.median(v[1] for v in vals)
        share = statistics.median(prep_share[(plan, acq)])
        print(f"{plan:<18} {acq:<22} {len(vals):>6,} {self_r:>12.3f} {exec_r:>11.3f} {share:>10.1%}")
    print("\np/plan_self includes the candidate build dispatch pays on a RANGE_ACQUIRES route;")
    print("p/executor excludes it. The difference between the columns is the unpriced term.")
    print("\nThe three columns are INDEPENDENT medians over differently-ordered rows and do NOT")
    print("compose: a 12.6% prep share cannot reconcile two ratios 1.76x apart, and reading across")
    print("them arithmetically is a mistake this table has already invited once. Compare a column")
    print("against itself across builds; never divide one column by another.")


if __name__ == "__main__":
    main()
