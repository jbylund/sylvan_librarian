"""What is StreamedSelect's `CARD_PASS+FLOOR` under-count actually WORTH to routing?

The defect: `run_query_streamed` calls `filter.card_pass` in a counting pass and AGAIN in its
small-total redo pass; `cost::residual_card_pass` prices one. Graded against the realized
`card_pass_calls` counter the small-total branch reads 0.500 -- an exact 2x under-count. Under-charging
makes a plan win, so the direction is "StreamedSelect is priced too cheaply on small-total queries".

The doubt this script exists to settle is `bench_term_contributions.py`'s own number:
`SMALL_TOTAL_FLOOR_PER_CARD` is 14.1% of StreamedSelect's aggregate predicted cost and **0.0% of the
PICKED rows'**. That term is charged on exactly the branch the defect lives on. If StreamedSelect
essentially never wins on small-total queries, correcting an under-charge there makes a losing plan
lose by more and changes no routing decision.

Four measurements, in order:

  BRANCH CENSUS  How often is StreamedSelect picked while the small-total gather runs? Separately for
                 what the MODEL predicts (`stream_runs_small_gather`, which reads the estimate
                 `f.matches`) and what the EXECUTOR took (the same guard over the realized
                 `result_total`), because an estimate crossing STREAM_MIN_MATCHES makes them disagree
                 with both sides correct.

  ORACLE ARGMIN  Replace the feature with the counter -- charge `card_pass_calls * (CARD_PASS +
                 max(tier, FLOOR))` where the arm charges `eval_domain * ...` -- at SHIPPED rates, so
                 only the count changes, and re-run the argmin. This bounds EVERY proposal for this
                 term at once: no fix to the term can beat replacing it with ground truth.

  FLIP PRICING   For each flipped query, the old pick's and the new pick's measured time from the SAME
                 `explain_analyze` response. Common-mode: both ran in the same shuffled rounds in the
                 same process, so the ~9% cross-run noise floor does not apply to their difference.

  A/A CONTROL    Run the whole thing twice on one build and diff the headline numbers. Whatever moves
                 between two identical runs is the floor an A/B number has to clear. Run this script
                 twice with the same `--seed` and compare.

    PYTHONPATH=<wheel> .venv/bin/python scripts/bench_stream_card_pass_value.py \
        --corpus <corpus.jsonl> --shm-path <unique> --n-queries 3000 --mode uniform
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import statistics
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts.costbench import load_engine, sample_kwargs  # noqa: E402
from scripts.fit_cost_model import SHIPPED_RESIDUAL_FLOOR, STREAM_MIN_MATCHES  # noqa: E402

# ── the cost-model constants this script re-prices, mirrored from cost.rs ─────────────────────────
#: `cost.rs` STREAM_CARD_PASS_NS -- the per-call rate folded into the CARD_PASS+FLOOR column.
STREAM_CARD_PASS_NS = 2.47
#: `cost.rs` STREAM_RESIDUAL_FLOOR_NS, via `fit_cost_model`'s shipped table so there is one source.
STREAM_RESIDUAL_FLOOR_NS = SHIPPED_RESIDUAL_FLOOR["StreamedSelect"]

# ── the argmin mirror ─────────────────────────────────────────────────────────────────────────────
#: `PhysicalPlan::ALL`, in declaration order. The engine's ranking sort is stable, so ties break by
#: this order and the mirror must use the same one.
PLAN_ORDER = (
    "PrintingRangeScan",
    "PrintingCompose",
    "PlanePopcountOrder",
    "CardRangePopcount",
    "StreamedSelect",
    "GatheredScan",
)
#: `CandidatePlan::of(plan).is_some()` -- the plans a materialized candidate list can execute.
CANDIDATE_PLANS = frozenset({"StreamedSelect", "GatheredScan"})
#: `PlanScope::Plane` admits the candidate plans plus the one that walks the plane bitmap.
PLANE_SCOPE_PLANS = CANDIDATE_PLANS | {"PlanePopcountOrder"}
#: `Prep::scope()` keyed by the `count_source` label `Prep::count_source()` reports. The mapping is
#: exact, not a guess: `Prep::Range` is constructed with one of the three range sources and nothing
#: else, `Prep::Plane` reports "plane", `Prep::Candidates` reports "candidates". Validated every run
#: by `mirror_pick`, which must reproduce the engine's own `picked` flag on 100% of rows.
SCOPE_BY_COUNT_SOURCE = {
    "card_range_popcount": "All",
    "printing_range_scan": "All",
    "printing_compose": "All",
    "plane": "Plane",
    "candidates": "Candidates",
}

# ── measurement ───────────────────────────────────────────────────────────────────────────────────
#: `costbench`'s documented pair for comparisons made INSIDE one `explain_analyze` response, which is
#: what the flip pricing and the picked-time sums are. The A/A control reports what is left over.
NUM_WARMUPS = 2
NUM_TRIALS = 7
#: Below this a per-query difference is timer jitter, not a routing change worth naming.
NOISE_FLOOR_US = 1.0
#: Cells thinner than this are not summarised.
MIN_ROWS = 30
#: Percentiles for the counter-grading table.
PERCENTILES = (10, 25, 50, 75, 90)


def scope_admits(scope: str, plan: str) -> bool:
    """`PlanScope::admits`, mirrored."""
    if scope == "All":
        return True
    if scope == "Plane":
        return plan in PLANE_SCOPE_PLANS
    return plan in CANDIDATE_PLANS


def ranked(costs: dict[str, float]) -> list[str]:
    """The engine's ranking: ascending predicted cost, ties broken by `PhysicalPlan::ALL` order."""
    return sorted(costs, key=lambda p: (costs[p], PLAN_ORDER.index(p)))


def mirror_pick(costs: dict[str, float], scope: str) -> str | None:
    """`explain`'s own pick: the cheapest plan this acquire's dispatch arm can run."""
    for plan in ranked(costs):
        if scope_admits(scope, plan):
            return plan
    return None


def small_gather(total: float, offset: int) -> bool:
    """`cost::stream_runs_small_gather`, over whichever total is handed in.

    Given `acq["matches"]` this is the MODEL's predicate verbatim. Given the realized `result_total`
    it is `run_query_streamed`'s own guard -- the same three conditions, which is the point: the two
    disagree only when the estimate crosses STREAM_MIN_MATCHES, never because the formula is wrong.
    """
    return 0 < total <= STREAM_MIN_MATCHES and offset < total


def measured_ns(row: dict) -> float | None:
    """Min over trials -- what this plan costs to run. `None` for a plan that entered and declined."""
    return float(min(row["trials_ns"])) if row["trials_ns"] else None


def pct(values: list[float], p: int) -> float:
    """Nearest-rank percentile."""
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100.0 * len(s)))]


def collect(engine: object, sampler: QuerySampler, rng: random.Random, n_queries: int) -> list[dict]:
    """One `explain_analyze` per sampled query; every number this script reports comes from it.

    Deliberately ONE call, not `explain` + `explain_analyze`: the acquire facts, the realized counters
    and the timings then all come from the same `prefer`, which since Round 66 the acquire reads.
    """
    from api.parsing import parse_scryfall_query  # noqa: PLC0415

    rows = []
    for _ in range(n_queries):
        kw = sample_kwargs(sampler, rng, vary_prefer=True)
        q = sampler.query(rng)
        try:
            kw["filters"] = parse_scryfall_query(q)
            res = engine.explain_analyze(num_warmups=NUM_WARMUPS, num_trials=NUM_TRIALS, **kw)
        except Exception:  # noqa: BLE001, S112 - a rejected query is a skipped sample
            continue
        acq = res["acquire"]
        if not acq:
            continue
        rows.append({"q": q, "kw": {k: v for k, v in kw.items() if k != "filters"}, "acquire": acq, "plans": res["plans"]})
    return rows


#: The cost variants re-argmin'd against the shipped model, keyed by name.
#:
#:   ORACLE       the ceiling -- the term's count replaced outright by the realized counter. No
#:                proposal for this term can beat it, so a null result here is a null result for all
#:                of them, the sibling lane's included.
#:   DEFECT_ONLY  what a fix for THIS defect ships: the small-total redo runs `card_pass` a second
#:                time over the matching cards, and on those rows the counter reads an exact 2x, so
#:                doubling the term on `stream_runs_small_gather` is the correction. Gated on the
#:                MODEL's predicate, because that is all a router has at cost time.
#:   ORACLE_MODEL_SMALL / ORACLE_EXEC_SMALL
#:                the ceiling RESTRICTED to the branch the defect lives on, gated respectively on
#:                what the model predicts and on what the executor took. These attribute the ORACLE
#:                number: whatever they do not account for is the term being wrong somewhere else.
VARIANTS = ("ORACLE", "DEFECT_ONLY", "ORACLE_MODEL_SMALL", "ORACLE_EXEC_SMALL")


def stream_cost_variants(cost: float, acq: dict, ss: dict, offset: int) -> dict[str, float]:
    """StreamedSelect's cost under each variant. Shipped rates throughout -- only the COUNT moves."""
    rate = STREAM_CARD_PASS_NS + max(acq["residual_tier_ns100"] / 100.0, STREAM_RESIDUAL_FLOOR_NS)
    charged = float(acq["residual_card_pass"])  # `cost::residual_card_pass`, exposed by explain
    realized = float(ss["card_pass_calls"])
    model_small = small_gather(float(acq["matches"]), offset)
    exec_small = small_gather(float(ss["result_total"]), offset)
    oracle_delta = (realized - charged) * rate
    return {
        "ORACLE": cost + oracle_delta,
        "DEFECT_ONLY": cost + (charged if model_small else 0.0) * rate,
        "ORACLE_MODEL_SMALL": cost + (oracle_delta if model_small else 0.0),
        "ORACLE_EXEC_SMALL": cost + (oracle_delta if exec_small else 0.0),
    }


def census_row(out: dict, acq: dict, ss: dict, offset: int, pick: dict) -> None:
    """Fold one StreamedSelect row into the branch census, the counter grading and the margin table.

    `pick` carries the row's argmin context -- `scope`, the per-plan `costs`, and the `engine_pick` --
    as one value rather than three parameters.
    """
    scope, costs, engine_pick = pick["scope"], pick["costs"], pick["engine_pick"]
    model_small = small_gather(float(acq["matches"]), offset)
    exec_small = small_gather(float(ss["result_total"]), offset)
    out["model_small"] += model_small
    out["exec_small"] += exec_small
    est, real = float(acq["matches"]), float(ss["result_total"])
    if model_small != exec_small:
        out["branch_disagree"] += 1
        # All three of the guard's conditions can straddle, not just the threshold one. Named
        # separately because they are different errors: the threshold and the empty case are the
        # `matches` estimate being wrong, the page one is `matches` being wrong ABOUT the page depth.
        # None of them is this cost term.
        side = "model-only" if model_small else "exec-only"
        for cond, differs in (
            (f"across STREAM_MIN_MATCHES ({STREAM_MIN_MATCHES})", (est <= STREAM_MIN_MATCHES) != (real <= STREAM_MIN_MATCHES)),
            ("across zero matches", (est > 0) != (real > 0)),
            ("across offset past the end", (offset < est) != (offset < real)),
        ):
            if differs:
                out["disagree_why"][f"{side}: {cond}"] = out["disagree_why"].get(f"{side}: {cond}", 0) + 1
        if exec_small and real > 0:
            out["exec_only_est_ratio"].append(est / real)

    charged, realized = float(acq["residual_card_pass"]), float(ss["card_pass_calls"])
    if charged > 0:
        ratio = realized / charged
        out["ratios"]["all"].append(ratio)
        out["ratios"]["small" if exec_small else "walk"].append(ratio)
        out["ratios_by_source"].setdefault(acq["count_source"], []).append(ratio)
        key = f"{ratio:.3f}" if ratio in (0.0, 0.5, 1.0, 2.0) else "other"
        out["exact"][key] = out["exact"].get(key, 0) + 1
    elif realized > 0:
        out["exact"]["charged 0, realized > 0"] = out["exact"].get("charged 0, realized > 0", 0) + 1

    if engine_pick == "StreamedSelect":
        out["ss_picked"] += 1
        out["ss_picked_model_small"] += model_small
        out["ss_picked_exec_small"] += exec_small
    elif model_small and scope_admits(scope, "StreamedSelect"):
        margin = costs["StreamedSelect"] - costs[engine_pick]
        defect = charged * (STREAM_CARD_PASS_NS + max(acq["residual_tier_ns100"] / 100.0, STREAM_RESIDUAL_FLOOR_NS))
        if margin > 0:
            out["small_margin_ratios"].append(defect / margin)
            out["small_margin_reachable"] += defect >= margin


def analyse(rows: list[dict]) -> dict:
    """Branch census, counter grading, oracle argmin and flip pricing, in one pass over the rows."""
    out = {
        "n_rows": len(rows),
        "mirror_ok": 0,
        "mirror_bad": 0,
        "ss_offered": 0,
        "ss_picked": 0,
        "model_small": 0,
        "exec_small": 0,
        "branch_disagree": 0,
        #: Which of `stream_runs_small_gather`'s three conditions the estimate and the realized total
        #: land on opposite sides of. A cost-term fix can only reach the rows where the two AGREE, so
        #: whatever value sits behind a disagreement belongs to whatever produced `matches`, not here.
        "disagree_why": {},
        #: `matches` estimate / realized `result_total` on the rows where the EXECUTOR took the
        #: small-total gather and the model did not -- the population the oracle's value lives on.
        "exec_only_est_ratio": [],
        "ss_picked_model_small": 0,
        "ss_picked_exec_small": 0,
        "ratios": {"small": [], "walk": [], "all": []},
        #: Same ratio keyed by `count_source`. A `Prep::Range` acquire costs a materializing plan on
        #: the coarse "broad regime" estimate (see `explain`'s own doc), so if the term's error
        #: concentrates there it is an acquire-scope error wearing this term's clothes.
        "ratios_by_source": {},
        "exact": {},
        #: On rows where the MODEL predicts the small-total gather and StreamedSelect is admitted but
        #: not picked: how far it is from winning, against how large the defect is. `margin` is what
        #: its cost would have to fall by; `defect` is the whole under-charge (one extra pass over the
        #: charged count). Their ratio bounds the branch: at max << 1 no correction of this term in
        #: EITHER direction can reach the argmin there, which is a stronger claim than "0 flips today".
        "small_margin_ratios": [],
        "small_margin_reachable": 0,
        "variants": {v: {"flips": [], "before_ns": 0.0, "after_ns": 0.0, "rows": 0} for v in VARIANTS},
    }
    for row in rows:
        acq, plans = row["acquire"], row["plans"]
        offset = row["kw"]["offset"]
        by_plan = {p["plan"]: p for p in plans}
        costs = {p["plan"]: float(p["predicted_ns"]) for p in plans}
        scope = SCOPE_BY_COUNT_SOURCE[acq["count_source"]]
        engine_pick = next((p["plan"] for p in plans if p["picked"]), None)
        if mirror_pick(costs, scope) == engine_pick:
            out["mirror_ok"] += 1
        else:
            out["mirror_bad"] += 1
            continue  # a row whose argmin this script cannot reproduce cannot be re-run under an oracle

        ss = by_plan.get("StreamedSelect")
        variant_costs = {v: dict(costs) for v in VARIANTS}
        if ss is not None:
            out["ss_offered"] += 1
            for name, c in stream_cost_variants(costs["StreamedSelect"], acq, ss, offset).items():
                variant_costs[name]["StreamedSelect"] = c
            census_row(out, acq, ss, offset, {"scope": scope, "costs": costs, "engine_pick": engine_pick})

        # ── re-run the argmin under each variant ──────────────────────────────────────────────────
        old_ns = measured_ns(by_plan[engine_pick])
        for name in VARIANTS:
            acc = out["variants"][name]
            new_pick = mirror_pick(variant_costs[name], scope)
            new_ns = measured_ns(by_plan[new_pick])
            if old_ns is not None and new_ns is not None:
                acc["before_ns"] += old_ns
                acc["after_ns"] += new_ns
                acc["rows"] += 1
            if new_pick != engine_pick:
                acc["flips"].append(
                    {
                        "q": row["q"],
                        "kw": row["kw"],
                        "old": engine_pick,
                        "new": new_pick,
                        "delta_us": None if old_ns is None or new_ns is None else (new_ns - old_ns) / 1000.0,
                        "matches": acq["matches"],
                        "exec_total": ss["result_total"] if ss else None,
                        "exec_small": small_gather(float(ss["result_total"]), offset) if ss else None,
                    }
                )
    return out


def report(res: dict, label: str) -> None:
    """Print the four measurements for one run."""
    print(f"\n{'=' * 96}\n{label}\n{'=' * 96}")
    print(f"rows graded           {res['n_rows']:,}")
    print(f"argmin mirror         {res['mirror_ok']:,} reproduced / {res['mirror_bad']:,} not (must be 0)")

    print("\n-- BRANCH CENSUS (StreamedSelect) " + "-" * 62)
    offered = max(res["ss_offered"], 1)
    print(f"  offered (costed)              {res['ss_offered']:,}")
    print(f"  model says small-total        {res['model_small']:,}  ({res['model_small'] / offered:.1%} of offered)")
    print(f"  executor took small-total     {res['exec_small']:,}  ({res['exec_small'] / offered:.1%} of offered)")
    print(f"  model/executor disagree       {res['branch_disagree']:,}   a row can straddle more than one condition:")
    for key, n in sorted(res["disagree_why"].items(), key=lambda kv: -kv[1]):
        print(f"      {key:<52} {n:,}")
    ratios = res["exec_only_est_ratio"]
    if len(ratios) >= MIN_ROWS:
        print(
            "    exec-only-small rows, `matches` estimate / realized total: "
            + "  ".join(f"p{p}={pct(ratios, p):.2f}" for p in PERCENTILES)
        )
    picked = max(res["ss_picked"], 1)
    print(f"  PICKED                        {res['ss_picked']:,}  ({res['ss_picked'] / offered:.1%} of offered)")
    print(
        f"  PICKED and model small-total  {res['ss_picked_model_small']:,}  ({res['ss_picked_model_small'] / picked:.1%} of picks)"
    )
    print(f"  PICKED and exec small-total   {res['ss_picked_exec_small']:,}  ({res['ss_picked_exec_small'] / picked:.1%} of picks)")

    print("\n-- COUNTER GRADING  card_pass_calls / residual_card_pass " + "-" * 39)
    print(f"  {'population':<26} {'n':>7} " + " ".join(f"{'p' + str(p):>7}" for p in PERCENTILES))
    for name, key in (("executor small-total", "small"), ("executor walk/other", "walk"), ("all StreamedSelect", "all")):
        vals = res["ratios"][key]
        if len(vals) < MIN_ROWS:
            print(f"  {name:<26} {len(vals):>7}  (too few rows)")
            continue
        print(f"  {name:<26} {len(vals):>7} " + " ".join(f"{pct(vals, p):>7.3f}" for p in PERCENTILES))

    for source, vals in sorted(res["ratios_by_source"].items(), key=lambda kv: -len(kv[1])):
        if len(vals) < MIN_ROWS:
            continue
        print(f"  by acquire: {source:<14} {len(vals):>7} " + " ".join(f"{pct(vals, p):>7.3f}" for p in PERCENTILES))
    print("\n  exact counter values (n rows):  " + "  ".join(f"{k}={v}" for k, v in sorted(res["exact"].items())))

    print("\n-- HOW FAR THE BRANCH IS FROM THE ARGMIN " + "-" * 55)
    ratios = res["small_margin_ratios"]
    print("  model-small rows where StreamedSelect is admitted, costed and LOSES: " + f"{len(ratios):,}")
    if len(ratios) >= MIN_ROWS:
        print(
            "  defect size / margin to the winner: "
            + "  ".join(f"p{p}={pct(ratios, p):.4f}" for p in PERCENTILES)
            + f"  max={max(ratios):.4f}"
        )
    print(f"  rows where the whole defect would have been enough to flip the pick: {res['small_margin_reachable']:,}")

    for name in VARIANTS:
        report_variant(name, res["variants"][name], res["mirror_ok"])


def report_variant(name: str, acc: dict, graded: int) -> None:
    """One cost variant's re-run argmin: flips, the picked-time sums, and the per-flip pricing."""
    print(f"\n-- {name}: ARGMIN + FLIP PRICING " + "-" * (64 - len(name)))
    before, after = acc["before_ns"], acc["after_ns"]
    print(f"  plan flips                    {len(acc['flips']):,} of {graded:,} graded rows")
    print(f"  picked-plan time BEFORE       {before / 1e6:.3f} ms  (over {acc['rows']:,} rows)")
    print(f"  picked-plan time AFTER        {after / 1e6:.3f} ms")
    if before > 0:
        print(f"  change                        {(after - before) / before:+.4%}  ({(after - before) / 1000.0:+.1f} us total)")
    priced = [f for f in acc["flips"] if f["delta_us"] is not None]
    if not priced:
        print("  no priced flips")
        return
    wins = [f for f in priced if f["delta_us"] < -NOISE_FLOOR_US]
    losses = [f for f in priced if f["delta_us"] > NOISE_FLOOR_US]
    net = sum(f["delta_us"] for f in priced)
    med = statistics.median(f["delta_us"] for f in priced)
    print(f"  priced flips                  {len(priced):,}   net {net:+.1f} us   median {med:+.2f} us")
    print(f"  wins (>{NOISE_FLOOR_US} us faster)          {len(wins):,}   totalling {sum(f['delta_us'] for f in wins):+.1f} us")
    print(f"  losses (>{NOISE_FLOOR_US} us slower)        {len(losses):,}   totalling {sum(f['delta_us'] for f in losses):+.1f} us")
    print(f"  within +/-{NOISE_FLOOR_US} us               {len(priced) - len(wins) - len(losses):,}")
    worst = max(priced, key=lambda f: f["delta_us"])
    best = min(priced, key=lambda f: f["delta_us"])
    print(
        f"  worst regression              {worst['delta_us']:+.1f} us  {worst['old']} -> {worst['new']}  {worst['q']!r} {worst['kw']}"
    )
    print(f"  best improvement              {best['delta_us']:+.1f} us  {best['old']} -> {best['new']}  {best['q']!r} {best['kw']}")
    pairs: dict[tuple[str, str], list[float]] = {}
    for f in priced:
        pairs.setdefault((f["old"], f["new"]), []).append(f["delta_us"])
    print("  flip directions:")
    for (old, new), deltas in sorted(pairs.items(), key=lambda kv: -len(kv[1])):
        print(f"    {old:<18} -> {new:<18} {len(deltas):>5}   net {sum(deltas):+9.1f} us")
    print(f"  flips whose executor took the small-total branch: {sum(1 for f in priced if f['exec_small']):,} of {len(priced):,}")


def main() -> None:
    """Sample, grade, re-run the argmin under the counter oracle, and price the flips."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--n-queries", type=int, required=True, help="queries to draw; never --seconds, so two runs grade the same rows"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--shm-path", type=pathlib.Path, required=True)
    parser.add_argument("--json-out", type=pathlib.Path, default=None, help="dump the analysis for an A/A diff")
    args = parser.parse_args()
    if args.n_queries <= 0:
        parser.error("--n-queries must be positive")

    engine = load_engine(args.corpus, args.shm_path)
    sampler = QuerySampler(args.corpus, args.mode)
    rows = collect(engine, sampler, random.Random(args.seed), args.n_queries)
    res = analyse(rows)
    report(res, f"mode={args.mode} seed={args.seed} n_queries={args.n_queries} warmups={NUM_WARMUPS} trials={NUM_TRIALS}")
    if args.json_out:
        dump = {k: v for k, v in res.items() if k not in ("ratios", "ratios_by_source")}
        dump["ratio_medians"] = {k: (statistics.median(v) if v else math.nan) for k, v in res["ratios"].items()}
        dump["ratio_medians_by_source"] = {k: statistics.median(v) for k, v in res["ratios_by_source"].items() if v}
        args.json_out.write_text(json.dumps(dump, indent=1))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
