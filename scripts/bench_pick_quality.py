"""How often does the router pick the best plan, and when it misses, did the miss COST anything?

`bench_regret_matrix.py` answers where lost time concentrates. This answers the question in front of
it: is the router right nearly always, and are its mistakes confined to queries where the plans were
close together anyway? Those are different properties, and a model can have the second without the
first -- which is the good outcome, because a miss between two plans within 5% of each other costs
5%, however often it happens.

The measurement everything here rests on: `explain_analyze` runs EVERY candidate plan inside ONE
call, on the same warmed store, in the same process, with participants shuffled per round. So the
picked plan and the plan that should have won are measured common-mode, and their difference is
readable even though the documented cross-run noise floor is ~9%. A cross-build or cross-run
comparison could not resolve these microseconds at all.

Three views:

  HIT RATE     share of queries whose picked plan was the fastest that ran, and the same weighted by
               time, because being wrong on the expensive queries is what matters.
  MARGIN       for MISSED queries only: `picked / best`. If that sits near 1.0 the router is losing
               ties, which is close to free. If it has a tail, some misses are real.
  HEADROOM     what the margin WOULD have been -- `second_best / best` over ALL queries -- so the
               miss margin can be read against how much was on the table. A router that misses only
               where headroom is small is behaving well even at a mediocre hit rate.

`plan_self_ns` is the per-plan cost, so a plan that pays a candidate build in dispatch is charged for
it and one that reuses a router-built artifact is not. See `costbench.plan_self_ns` for why that
netting is the only comparable one.
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

#: Below this a plan's measured time is timer resolution rather than signal. Values quantize to
#: ~41.67 ns (24 MHz mach timebase), so this is ~12 ticks.
MIN_MEASURED_NS = 500.0
#: A query needs at least this many plans that actually ran for "best" to mean anything.
MIN_PLANS = 2
#: Within this ratio of the best, two plans are a tie for practical purposes and picking either is
#: not a routing error worth counting. Chosen at the documented cross-run noise floor.
TIE_BAND = 1.09
#: Cells thinner than this are not printed.
MIN_ROWS = 25
DEFAULT_N_QUERIES = 8000


def collect(engine: object, sampler: QuerySampler, rng: random.Random, budget: Budget) -> list[dict]:
    """One row per query: what was picked, what was best, and how far apart the field was."""
    rows: list[dict] = []
    for sample in iter_samples(engine, sampler, rng, budget, vary_prefer=True):
        acq = sample.acquire
        timed = {}
        for p in sample.plans:
            if not p.get("trials_ns"):
                continue  # declined or inapplicable: it never ran, so it cannot be "best"
            self_ns = plan_self_ns(p, acq)
            if self_ns and self_ns >= MIN_MEASURED_NS:
                timed[p["plan"]] = self_ns
        picked = next((p["plan"] for p in sample.plans if p.get("picked")), None)
        if picked is None or picked not in timed or len(timed) < MIN_PLANS:
            continue
        ordered = sorted(timed.values())
        best_plan = min(timed, key=lambda k: timed[k])
        predicted = {p["plan"]: p.get("predicted_ns") for p in sample.plans}
        rows.append(
            {
                "q": sample.q,
                "orderby": sample.kw["orderby"],
                "prefer": sample.kw.get("prefer", "default"),
                "offset": sample.kw["offset"],
                "limit": sample.kw["limit"],
                "predicted": predicted,
                # A query whose result is empty, or whose page starts past the end, has a fast exit
                # every plan could take -- and compose's `EmptyPage` really does, in ~1.4 us, while the
                # materializing plans grind the whole candidate set producing nothing. The model prices
                # compose INFINITY there (it predicts `Decline`), so it is never picked. Sliced out
                # because it is a distinct failure from mis-costing a plan that does real work.
                "empty_page": (picked_total := next((pp.get("result_total") or 0 for pp in sample.plans if pp.get("picked")), 0))
                == 0
                or sample.kw["offset"] >= picked_total,
                "matches": acq["matches"],
                "eval_domain": acq["eval_domain"],
                "acquire": acq["count_source"],
                "unique": sample.kw["unique"],
                "picked": picked,
                "best": best_plan,
                "picked_ns": timed[picked],
                "best_ns": ordered[0],
                # How much was on the table at all: the runner-up against the winner. A query whose
                # second-best is 1.01x the best cannot punish a mis-pick by more than 1%.
                "headroom": ordered[1] / ordered[0],
                "n_plans": len(timed),
            }
        )
    return rows


def pct(v: list[float], p: float) -> float:
    """Nearest-rank percentile; `v` must be sorted."""
    return v[min(len(v) - 1, int(p * len(v)))] if v else float("nan")


def hit_table(rows: list[dict], key, label: str) -> None:  # noqa: ANN001
    """Hit rate by slice, unweighted and time-weighted, with the lost time each slice carries."""
    groups: dict[object, list[dict]] = collections.defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    total_lost = sum(r["picked_ns"] - r["best_ns"] for r in rows) or 1.0
    print(f"\n{label}")
    print(f"  {'slice':<34} {'n':>6} {'hit%':>7} {'tie-ok%':>8} {'time-wtd hit%':>14} {'lost%':>7}")
    for name, sub in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(sub) < MIN_ROWS:
            continue
        hits = sum(1 for r in sub if r["picked"] == r["best"])
        tie_ok = sum(1 for r in sub if r["picked_ns"] / r["best_ns"] <= TIE_BAND)
        t_tot = sum(r["picked_ns"] for r in sub) or 1.0
        t_hit = sum(r["picked_ns"] for r in sub if r["picked"] == r["best"])
        lost = sum(r["picked_ns"] - r["best_ns"] for r in sub)
        print(
            f"  {name!s:<34} {len(sub):>6,} {100 * hits / len(sub):>6.1f}% {100 * tie_ok / len(sub):>7.1f}% "
            f"{100 * t_hit / t_tot:>13.1f}% {100 * lost / total_lost:>6.1f}%"
        )


def worst_misses(miss: list[dict], n: int, by: str) -> None:
    """Dump the costliest mis-picks with both plans' PREDICTIONS beside their measurements.

    The measured ratio says how bad the miss was; the PREDICTED ratio says what kind of mistake it
    is, and they are different questions:

    - predicted ratio near 1.0 -> the model saw a near-tie and lost the coin flip. Cheap to hold,
      hard to fix, and not evidence of a broken term.
    - predicted ratio well below 1.0 -> the model was CONFIDENT the picked plan was much cheaper and
      was wrong. That is a term failing, and the per-plan predicted/measured columns say whose.

    Both plans' numbers come from the same `explain_analyze` call, so the comparison is common-mode.
    """
    # Two different "worst". Absolute loss ranks what to fix for total time; RATIO ranks the
    # pathological shapes -- a 20x miss on a 30 us query costs less than a 2x miss on a 1 ms one, but
    # it is the one that says a term is structurally wrong rather than slightly mis-fit.
    rank = (lambda r: -(r["picked_ns"] - r["best_ns"])) if by == "loss" else (lambda r: -r["picked_ns"] / r["best_ns"])
    print(f"\n{'=' * 108}\nWORST {n} MIS-PICKS by {by} -- predicted vs measured, per plan\n{'=' * 108}")
    for r in sorted(miss, key=rank)[:n]:
        pick_p, best_p = r["predicted"].get(r["picked"]), r["predicted"].get(r["best"])
        pred_ratio = (pick_p / best_p) if pick_p and best_p else float("nan")
        print(
            f"\n  lost {(r['picked_ns'] - r['best_ns']) / 1000:>8.1f} us   measured {r['picked_ns'] / r['best_ns']:>7.2f}x   "
            f"predicted {pred_ratio:>6.2f}x   headroom {r['headroom']:>6.2f}x"
        )
        print(f"    {r['q'][:78]}")
        print(
            f"    {r['unique']}/{r['orderby']}/off={r['offset']}/prefer={r['prefer']}  "
            f"[{r['acquire']}]  matches={r['matches']:,} eval_domain={r['eval_domain']:,}"
        )
        for tag, plan in (("PICKED", r["picked"]), ("BEST  ", r["best"])):
            pred, meas = r["predicted"].get(plan), r["picked_ns"] if plan == r["picked"] else r["best_ns"]
            ratio = f"{pred / meas:>6.2f}" if pred else "     -"
            pred_s = f"{pred / 1000:>9.1f}" if pred else "        -"
            print(f"      {tag} {plan:<19} predicted {pred_s} us   measured {meas / 1000:>9.1f} us   p/m {ratio}")


def main() -> None:
    """Report hit rate, miss margin and available headroom."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-queries", type=int, default=DEFAULT_N_QUERIES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    parser.add_argument("--worst", type=int, default=0, help="dump this many worst misses with per-plan predictions")
    parser.add_argument(
        "--worst-by", choices=("loss", "ratio"), default="loss", help="rank the dump by absolute us lost or by picked/best"
    )
    args = parser.parse_args()

    engine = load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".pickq.store"))
    sampler = QuerySampler(args.corpus, args.mode)
    budget = Budget(sample=args.n_queries, warmups=costbench.NUM_WARMUPS, trials=costbench.NUM_TRIALS)
    rows = collect(engine, sampler, random.Random(args.seed), budget)
    if not rows:
        print("no comparable rows")
        return

    hits = [r for r in rows if r["picked"] == r["best"]]
    miss = [r for r in rows if r["picked"] != r["best"]]
    tie_ok = [r for r in rows if r["picked_ns"] / r["best_ns"] <= TIE_BAND]
    tot_picked = sum(r["picked_ns"] for r in rows)
    tot_lost = sum(r["picked_ns"] - r["best_ns"] for r in rows)

    print(f"\n{len(rows):,} queries with >= {MIN_PLANS} plans timed, mode={args.mode}\n")
    print(f"  picked the fastest plan            {100 * len(hits) / len(rows):>6.1f}%  ({len(hits):,} of {len(rows):,})")
    print(
        f"  picked within {TIE_BAND:.2f}x of fastest      {100 * len(tie_ok) / len(rows):>6.1f}%  <-- a miss inside the noise floor costs nothing"
    )
    print(f"  time-weighted hit rate             {100 * sum(r['picked_ns'] for r in hits) / tot_picked:>6.1f}%")
    print(
        f"  total time lost to mis-picks       {100 * tot_lost / tot_picked:>6.1f}%  ({tot_lost / 1e6:.1f} ms of {tot_picked / 1e6:.1f} ms)"
    )

    if miss:
        m = sorted(r["picked_ns"] / r["best_ns"] for r in miss)
        print(f"\n  MISS MARGIN (picked / best), {len(miss):,} missed queries -- how bad is a miss?")
        print(
            f"    p10 {pct(m, 0.10):.3f}   p50 {statistics.median(m):.3f}   p90 {pct(m, 0.90):.3f}   "
            f"p99 {pct(m, 0.99):.3f}   max {m[-1]:.2f}"
        )
        h_miss = sorted(r["headroom"] for r in miss)
        h_hit = sorted(r["headroom"] for r in hits)
        print("\n  HEADROOM (second-best / best) -- how much was on the table at all?")
        print(f"    on MISSED queries   p50 {statistics.median(h_miss):.3f}   p90 {pct(h_miss, 0.90):.3f}")
        print(f"    on HIT queries      p50 {statistics.median(h_hit):.3f}   p90 {pct(h_hit, 0.90):.3f}")
        print("    Misses concentrated at LOW headroom means the router loses ties, which is nearly free.")
        share = sorted((r["picked_ns"] - r["best_ns"] for r in miss), reverse=True)
        top = sum(share[: max(1, len(share) // 100)])
        print(f"\n  CONCENTRATION: the worst 1% of misses carry {100 * top / (tot_lost or 1):.1f}% of all lost time")

    hit_table(rows, lambda r: r["acquire"], "by acquire route")
    hit_table(rows, lambda r: r["picked"], "by picked plan")
    hit_table(rows, lambda r: r["unique"], "by distinct-on")
    hit_table(
        rows, lambda r: f"empty-or-past-end={r['empty_page']}", "by EMPTY PAGE -- compose exits fast, model prices it INFINITY"
    )
    if miss:
        hit_table(miss, lambda r: f"{r['picked']} -> {r['best']}", "MISSES ONLY, by transition")
    if args.worst and miss:
        worst_misses(miss, args.worst, args.worst_by)


if __name__ == "__main__":
    main()
