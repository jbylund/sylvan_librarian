"""Did this branch make a PLAN faster? Paired per-plan execution time between two builds.

The layer the rest of the toolkit does not cover. Every other harness reads `trials_ns` as a RATIO
against `predicted_ns` — they ask whether the cost model is right, not whether the executor got
faster. `bench_query_latency_ab.py` asks the end-user question, but a `query()` delta is the sum of
two independent effects: how fast the executor is, and which executor the router picked. So an
executor change lands in one of three places, and only this harness separates them:

- The plan got faster **and the router still picks it** — a real win, visible end to end.
- The plan got faster **and the router never picks it** — a latent win. End-to-end latency moves 0%,
  and a PR that only quotes the survey concludes, wrongly, that nothing happened.
- A `cost::plan_cost` input moved, so the router picks **differently**. End-to-end latency moves and
  none of the delta is the executor's. `result_total` will usually still match, so the parity check
  does not catch it.

What is compared, per (query, plan):

- `exec_us` — `costbench.plan_self_ns`, the plan's own run with its re-paid `prepare_candidates`
  netted back out. The toolkit's single definition, so this is comparable to the other harnesses.
- `acquire_us` / `routed_us` — the acquire step and the whole routed path, both timed by
  `explain_analyze` as shuffled participants rather than as preludes, so they are on the same footing
  as the plans.
- `picked` — whether the router chose this plan. Reported as a separate routing-shift line, because
  an executor win the router declines to use is a different claim from a win it uses.
- `result_total` — the row-count parity check. Rows whose totals disagree across builds are not
  compared; a plan that got faster by returning different rows is not faster.

Usage — same corpus, mode and seed on both sides, or nothing pairs:

    # on main
    .venv/bin/python scripts/bench_plan_execution_ab.py --sample 600 --out /tmp/main.jsonl
    # on the branch
    .venv/bin/python scripts/bench_plan_execution_ab.py --sample 600 --out /tmp/branch.jsonl
    .venv/bin/python scripts/bench_plan_execution_ab.py --compare /tmp/main.jsonl /tmp/branch.jsonl

    # or narrow to the plan under test
    .venv/bin/python scripts/bench_plan_execution_ab.py --compare A.jsonl B.jsonl --plan PrintingCompose

## Why this harness runs more trials than the rest of the toolkit

`min` over the trials is a FLOOR estimator, and at a low trial count it is a noisy one. Its error
depends on how much interference that particular run happened to see, so two runs of the same build
land at different distances above the same true floor — and since every participant in a run shares
those conditions, the error is common-mode and reads as a uniform slowdown rather than as noise.

Measured, same build and same seed, only the trial count changed:

| median µs           | 2w/7t A → B     | 6w/30t A → B     |
| ------------------- | --------------- | ---------------- |
| StreamedSelect      | 54.65 → 56.96   | 54.38 → 54.37    |
| GatheredScan        | 40.29 → 42.98   | 39.96 → 39.79    |
| acquire             |  2.67 →  2.92   |  2.67 →  2.62    |

At 2w/7t all four measurements reported "B is SLOWER" with every interval excluding zero and "faster
on 0". At 6w/30t all four report no detectable difference. The floors themselves only moved 0.5-2.6%,
so this is not `min` finding a lower value — it is the run-to-run SPREAD collapsing from 4-9% to
under 0.5% on the plans. Hence the defaults below.

Interleaving A/B/A/B and quiescing the machine are still worth doing, and are the answer to genuine
thermal drift over a long run. They were not the answer to the above.
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
from scripts.costbench import load_engine  # noqa: E402

# Identity of one measured (query, plan) observation. Everything here must match across builds for
# the pair to be comparable -- the plan included, since the same query measures every applicable one.
PAIR_KEY = ("q", "unique", "orderby", "direction", "limit", "offset", "plan")
# A plan must appear this many times in BOTH runs before its own comparison is printed. Below it the
# bootstrap interval is wider than anything the change could have done.
MIN_PAIRS_PER_PLAN = 20
# Well above the shared costbench (2, 7). Seven trials is enough to RANK plans inside one
# `explain_analyze` call, where every participant shares the same conditions; it is not enough to
# compare a plan against ITSELF in a second process, where each run's floor estimate carries its own
# error. See the module docstring for the measurement that fixes these numbers.
AB_WARMUPS = 6
AB_TRIALS = 30
# Acquire disagreeing between two runs by more than this means the two are not comparable. Most
# executor changes do not touch acquire, so its movement estimates the residual common-mode error
# left after the trial count above.
DRIFT_WARN_FRACTION = 0.02


def measure(engine: object, sampler: QuerySampler, rng: random.Random, budget: costbench.Budget) -> list[dict]:
    """One row per (query, plan) that produced a page, plus the acquire and routed times."""
    rows: list[dict] = []
    for sample in costbench.iter_samples(engine, sampler, rng, budget):
        acq = sample.res["acquire"]
        # Both are per-round participant timings from the same shuffled loop as the plans.
        acquire_us = min(acq["acquire_ns"]) / 1000.0 if acq["acquire_ns"] else None
        routed_us = min(acq["routed_ns"]) / 1000.0 if acq["routed_ns"] else None
        for p in sample.plans:
            exec_ns = costbench.plan_self_ns(p, sample.acquire)
            if exec_ns is None:
                continue
            rows.append(
                {
                    "q": sample.q,
                    **{k: sample.kw[k] for k in ("unique", "orderby", "direction", "limit", "offset")},
                    "plan": p["plan"],
                    "exec_us": exec_ns / 1000.0,
                    "acquire_us": acquire_us,
                    "routed_us": routed_us,
                    "picked": p["picked"],
                    "result_total": p["result_total"],
                    "count_source": sample.acquire["count_source"],
                }
            )
    return rows


def read_run(path: pathlib.Path) -> dict[tuple, dict]:
    """Read a recorded run back, keyed by the identity a pair needs to share."""
    import json  # noqa: PLC0415 - only the compare path needs it

    out: dict[tuple, dict] = {}
    for line in path.open():
        r = json.loads(line)
        out[tuple(r[k] for k in PAIR_KEY)] = r
    return out


def report_parity(a: dict[tuple, dict], b: dict[tuple, dict], shared: list[tuple]) -> list[tuple]:
    """Drop pairs whose row counts disagree, and say how many. Returns the pairs worth comparing."""
    mismatched = [k for k in shared if a[k]["result_total"] != b[k]["result_total"]]
    if mismatched:
        print(f"\n  !! {len(mismatched):,} of {len(shared):,} pairs returned DIFFERENT row counts and were excluded.")
        print("     A plan that got faster by returning different rows is not faster. Sample:")
        for k in mismatched[:3]:
            print(f"       {k[6]:<20} {a[k]['result_total']:>9,} -> {b[k]['result_total']:>9,}   {k[0][:60]}")
    return [k for k in shared if a[k]["result_total"] == b[k]["result_total"]]


def median_ratio(a: dict[tuple, dict], b: dict[tuple, dict], keys: list[tuple], field: str) -> float | None:
    """Median per-observation B/A for one field, or None if nothing usable paired."""
    ratios = sorted(b[k][field] / a[k][field] for k in keys if a[k].get(field) and b[k].get(field))
    return statistics.median(ratios) if ratios else None


def report_drift(a: dict[tuple, dict], b: dict[tuple, dict], shared: list[tuple], by_plan: dict[str, list[tuple]]) -> None:
    """Use acquire as a CONTROL, and refuse to let two incomparable runs read as a result.

    The backstop for what `AB_WARMUPS`/`AB_TRIALS` mostly fixes. Each run's `min`-of-trials sits some
    distance above the true floor, and that distance depends on the interference that run saw, so two
    runs of the SAME build can disagree. The error is common-mode — every participant in a run shares
    the conditions — which is exactly what makes acquire a usable control: most executor changes do
    not touch it, so its movement estimates what is left.

    At the old (2, 7) this fired hard: plans +4.2% and +6.7%, acquire +9.4%, every interval excluding
    zero on a same-build pair, and the adjusted column below correctly read "no change". At (6, 30)
    the same pair agrees to within 0.5% and this never fires. If it fires now, the two runs are not
    comparable — raise `--trials` before believing anything above.
    """
    acquire_drift = median_ratio(a, b, shared, "acquire_us")
    if acquire_drift is None or abs(acquire_drift - 1.0) < DRIFT_WARN_FRACTION:
        return
    print(f"\n  !! CONTROL MOVED — acquire disagrees by {acquire_drift - 1.0:+.1%} between these two runs.")
    print("     If this change did not touch the acquire path, these two runs are not comparable and")
    print(f"     every per-plan delta above carries the same error. Raise --trials (default {AB_TRIALS}) first;")
    print("     that is what usually closes it. Until then read the adjusted column, not the raw one.")
    print(f"\n  {'plan':<24}{'raw B/A':>10}{'÷ acquire':>12}   adjusted reading")
    for plan, keys in sorted(by_plan.items(), key=lambda kv: -len(kv[1])):
        raw = median_ratio(a, b, keys, "exec_us")
        if raw is None or len(keys) < MIN_PAIRS_PER_PLAN:
            continue
        adjusted = raw / acquire_drift
        verdict = (
            "no change once the control is removed"
            if abs(adjusted - 1.0) < DRIFT_WARN_FRACTION
            else (f"{adjusted - 1.0:+.1%} after adjustment")
        )
        print(f"  {plan:<24}{raw:>10.3f}{adjusted:>12.3f}   {verdict}")


def report_routing(a: dict[tuple, dict], b: dict[tuple, dict], shared: list[tuple]) -> None:
    """Whether the router's choice moved — the second half of acceptance for an executor change."""
    gained: collections.Counter[str] = collections.Counter()
    lost: collections.Counter[str] = collections.Counter()
    for k in shared:
        if a[k]["picked"] == b[k]["picked"]:
            continue
        (gained if b[k]["picked"] else lost)[k[6]] += 1
    if not gained and not lost:
        print("\nrouting: unchanged on every paired query — an execution delta here is the whole story.")
        return
    print("\nrouting CHANGED — the end-to-end delta is not purely this executor's:")
    for plan in sorted(set(gained) | set(lost)):
        print(f"  {plan:<24}picked on {gained[plan]:>5,} more, {lost[plan]:>5,} fewer")


def picked_by_query(run: dict[tuple, dict], shared: list[tuple]) -> dict[tuple, str]:
    """{query identity: the plan this run's router picked}, over the paired observations."""
    return {k[:6]: k[6] for k in shared if run[k]["picked"]}


def report_flip_pricing(a: dict[tuple, dict], b: dict[tuple, dict], shared: list[tuple]) -> None:
    """What each routing flip COST, priced common-mode inside one run.

    `report_routing` above says the pick moved and stops there, which is where a cost-model change
    gets read wrongly. The aggregate picked time across two runs cannot answer "was the flip good":
    a same-build A/A pair on this harness has drifted **+0.48%** on aggregate picked time with zero
    flips, so any aggregate under ~0.5% is unreadable and only the flips themselves carry signal.

    So each flip is priced from ONE run's measurements, where the old pick and the new pick were
    forced in the SAME `explain_analyze` call — shuffled into the same rounds under the same thermal
    conditions, so the floor-estimation error is common-mode between them and largely cancels. That
    is a per-query difference between two plans, not a difference between two processes, and it is
    the only number here that survives the drift above.

    Both runs are priced and both columns printed. They measure the same physical quantity, so a
    disagreement between the columns IS the drift, stated rather than hidden: a phase reported as
    moving 89.8 -> 104.3 us on rows whose predicted delta was exactly zero is what one column alone
    looks like when the machine wandered.
    """
    picks_a, picks_b = picked_by_query(a, shared), picked_by_query(b, shared)
    exec_a = {(k[:6], k[6]): a[k]["exec_us"] for k in shared}
    exec_b = {(k[:6], k[6]): b[k]["exec_us"] for k in shared}
    flips = sorted(q for q in picks_a if q in picks_b and picks_a[q] != picks_b[q])
    if not flips:
        print("\nflip pricing: no query changed its pick, so there is nothing to price.")
        return

    by_pair: dict[tuple[str, str], list[tuple[float, float]]] = collections.defaultdict(list)
    for q in flips:
        old, new = picks_a[q], picks_b[q]
        # Both plans must have been measured in BOTH runs, or one column would price a different
        # population from the other and the drift check below would be comparing two things.
        cells = [(exec_a.get((q, old)), exec_a.get((q, new))), (exec_b.get((q, old)), exec_b.get((q, new)))]
        if any(o is None or n is None for o, n in cells):
            continue
        by_pair[(old, new)].append((cells[0][1] - cells[0][0], cells[1][1] - cells[1][0]))

    print(f"\nFLIP PRICING — {len(flips):,} of {len(picks_a):,} paired queries changed their pick")
    print("  each priced as new_pick - old_pick within ONE run, so the two plans ran in the same rounds")
    print(f"\n  {'old -> new':<44}{'n':>6}{'A-priced':>12}{'B-priced':>12}{'A median':>11}")
    net_a = net_b = 0.0
    for (old, new), deltas in sorted(by_pair.items(), key=lambda kv: -len(kv[1])):
        tot_a = sum(d[0] for d in deltas) / 1000.0
        tot_b = sum(d[1] for d in deltas) / 1000.0
        net_a, net_b = net_a + tot_a, net_b + tot_b
        med = statistics.median(d[0] for d in deltas)
        print(f"  {old + ' -> ' + new:<44}{len(deltas):>6,}{tot_a:>+11.2f}ms{tot_b:>+11.2f}ms{med:>+10.1f}us")
    print(f"  {'NET over all flips':<44}{sum(len(v) for v in by_pair.values()):>6,}{net_a:>+11.2f}ms{net_b:>+11.2f}ms")
    print("  negative is FASTER. A and B price the same flips; the gap between the columns is drift.")


def compare(path_a: pathlib.Path, path_b: pathlib.Path, only_plan: str | None) -> None:
    """Paired per-plan execution comparison over the observations both runs recorded."""
    a, b = read_run(path_a), read_run(path_b)
    shared = sorted(set(a) & set(b))
    if only_plan:
        shared = [k for k in shared if k[6] == only_plan]
    if not shared:
        print("no (query, plan) observations in common -- both runs need the same --mode/--sample/--seed")
        return
    shared = report_parity(a, b, shared)
    if not shared:
        return

    by_plan: dict[str, list[tuple]] = collections.defaultdict(list)
    for k in shared:
        by_plan[k[6]].append(k)

    print(f"\nEXECUTION, per plan   (A={path_a.name}  B={path_b.name})")
    for plan, keys in sorted(by_plan.items(), key=lambda kv: -len(kv[1])):
        if len(keys) < MIN_PAIRS_PER_PLAN:
            continue
        print(f"\n── {plan}  ({len(keys):,} paired queries)")
        costbench.report_paired(
            {k: a[k]["exec_us"] for k in keys},
            {k: b[k]["exec_us"] for k in keys},
            unit="µs",
            label_a=path_a.name,
            label_b=path_b.name,
        )

    # Acquire and routed are per-QUERY, not per-plan: `explain_analyze` times each once per round and
    # every plan row on that query carries the same copy. Key them by the query alone, or a query with
    # four applicable plans votes four times and narrows the interval on strength it does not have.
    for field, caption in (
        ("acquire_us", "ACQUIRE (the shared prep, per query)"),
        ("routed_us", "ROUTED (what a user actually waits for)"),
    ):
        av = {k[:6]: a[k][field] for k in shared if a[k][field] is not None}
        bv = {k[:6]: b[k][field] for k in shared if b[k][field] is not None}
        if paired := set(av) & set(bv):
            print(f"\n── {caption}  ({len(paired):,} paired queries)")
            costbench.report_paired(
                {q: av[q] for q in paired},
                {q: bv[q] for q in paired},
                unit="µs",
                label_a=path_a.name,
                label_b=path_b.name,
            )

    report_drift(a, b, shared, by_plan)
    report_routing(a, b, shared)
    report_flip_pricing(a, b, shared)
    print("\n  Acceptance for an executor change is BOTH halves: the pinned plan is faster, AND the")
    print("  router still routes to it. A win the router declines to use is a latent win — say so.")


def main() -> None:
    """Either measure one build to a file, or compare two such files."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=600, help="queries to measure; every applicable plan is timed on each")
    parser.add_argument("--warmups", type=int, default=AB_WARMUPS)
    parser.add_argument(
        "--trials", type=int, default=AB_TRIALS, help="a cross-process A/B needs a converged floor; see the module docstring"
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--mode", choices=MODES, default="realistic", help="executor work should be weighted like real traffic")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    parser.add_argument("--out", type=pathlib.Path, help="write per-(query, plan) rows as JSONL")
    parser.add_argument("--compare", nargs=2, type=pathlib.Path, metavar=("A.jsonl", "B.jsonl"))
    parser.add_argument("--plan", help="restrict --compare to one plan, e.g. PrintingCompose")
    args = parser.parse_args()

    if args.compare:
        compare(*args.compare, args.plan)
        return

    engine = load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".planexec.store"))
    sampler = QuerySampler(args.corpus, args.mode)
    budget = costbench.Budget(sample=args.sample, warmups=args.warmups, trials=args.trials)
    rows = measure(engine, sampler, random.Random(args.seed), budget)
    per_plan = collections.Counter(r["plan"] for r in rows)
    print(f"\nmeasured {len(rows):,} (query, plan) observations, mode={args.mode}")
    for plan, n in per_plan.most_common():
        print(f"  {plan:<24}{n:>7,}")
    if args.out:
        costbench.write_rows(args.out, rows)
    else:
        print("\n  no --out given, so nothing was recorded. Pass --out to make this run comparable.")


if __name__ == "__main__":
    main()
