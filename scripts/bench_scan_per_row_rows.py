"""Per-query evidence for `scan-per-row`: every plan's predicted vs true cost, and the term's share.

The queue item for `scan-per-row` rests on aggregates -- 20.1% of tail cost, 1.68x over on the tail,
1.00 in aggregate. Aggregates say the term is worth fixing; they do not say what a fix must look
like. This prints the rows themselves, because three things only separate per query:

1. **Is the whole prediction wrong, or just this term's share of it?** A term can be 1.68x over and
   still not decide a pick if it carries 5% of the plan's cost. `SCAN_PER_ROW %` is that share.
2. **Feature or rate?** The term is `stream_scan_units * residual_on * STREAM_SCAN_PER_ROW_NS`
   (5.97 ns). Swapping the estimated feature for its realized counter (`printings_examined`) and
   re-costing says whether the ESTIMATE is wrong or the RATE is. Only the second is a refit.
3. **Does correcting it flip the pick?** A term can be badly wrong on a row the router still gets
   right, which costs nothing.

Rows are chosen by LOSS -- picked minus best measured -- so the table is the population that actually
costs time, not the population where the ratio is worst. Both plans in each comparison are timed
inside one `explain_analyze` call, so their measurements are common-mode.

    PYTHONPATH=<enginedir> .venv/bin/python scripts/bench_scan_per_row_rows.py --top 20
"""

from __future__ import annotations

import argparse
import pathlib
import random
import statistics
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts import costbench  # noqa: E402
from scripts.bench_error_attribution_weighted import TERM_ORACLE, substitutable  # noqa: E402
from scripts.costbench import Budget, iter_samples, load_engine, plan_self_ns  # noqa: E402
from scripts.fit_cost_model import CURRENT, MIRROR_TOLERANCE, design_row  # noqa: E402

#: The term under study, and the plan that charges it at 5.97 ns/row against GatheredScan's 2.06.
PLAN = "StreamedSelect"
TERM = "SCAN_PER_ROW"
#: Below this a plan's measured time is timer resolution (~41.67 ns ticks, 24 MHz timebase).
MIN_MEASURED_NS = 500.0
DEFAULT_TOP = 20
DEFAULT_N_QUERIES = 8000


def decompose(row: dict, plan: str, *, oracle: bool) -> tuple[float, float] | None:
    """That plan's predicted ns and this term's share of it, optionally with the term set to truth."""
    built = design_row(plan, row["acq"], row["limit"], row["offset"])
    if built is None:
        return None
    terms, excess = built
    coeffs = CURRENT[plan]
    counters = row["counters"].get(plan)
    total, term_ns = excess, 0.0
    for name, value in terms.items():
        swapped = value
        pair = TERM_ORACLE.get((plan, name))
        if oracle and pair and counters is not None:
            probe = {"plan": plan, "counters": counters, "acq": row["acq"], "unique": row["unique"], "paging": row["paging"]}
            if substitutable(probe, terms, plan, name, pair):
                swapped = float(counters[pair[1]])
        contribution = coeffs[name] * swapped
        total += contribution
        if name == TERM:
            term_ns = contribution
    return total, term_ns


def main() -> None:  # noqa: PLR0915 - one table, printed row by row
    """Print the costliest rows where this term is charged, with every plan's numbers."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-queries", type=int, default=DEFAULT_N_QUERIES)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, required=True)
    args = parser.parse_args()

    engine = load_engine(args.corpus, args.shm_path)
    sampler = QuerySampler(args.corpus, args.mode)
    budget = Budget(sample=args.n_queries, warmups=costbench.NUM_WARMUPS, trials=costbench.NUM_TRIALS)

    rows = []
    for sample in iter_samples(engine, sampler, random.Random(args.seed), budget, vary_prefer=True):
        acq = sample.acquire
        timed, counters = {}, {}
        for p in sample.plans:
            self_ns = plan_self_ns(p, acq) if p.get("trials_ns") else None
            if self_ns and self_ns >= MIN_MEASURED_NS:
                timed[p["plan"]] = self_ns
                counters[p["plan"]] = p
        picked = next((p["plan"] for p in sample.plans if p.get("picked")), None)
        if PLAN not in timed or picked is None or picked not in timed or len(timed) < 2:  # noqa: PLR2004
            continue
        pc = next((p for p in sample.plans if p["plan"] == "PrintingCompose"), None)
        best = min(timed, key=lambda k: timed[k])
        rows.append({
            "q": sample.q, "unique": sample.kw["unique"], "limit": sample.kw["limit"], "offset": sample.kw["offset"],
            "acq": acq, "counters": counters, "timed": timed, "picked": picked, "best": best,
            "loss": timed[picked] - timed[best],
            "paging": (pc or {}).get("paging_taken") if picked == "PrintingCompose" else acq.get("compose_paging"),
            "preds": {p["plan"]: p.get("predicted_ns") for p in sample.plans},
        })

    tail = sorted(rows, key=lambda r: -r["loss"])[: args.top]
    print(f"\n{len(rows):,} queries with {PLAN} timed against at least one other plan, mode={args.mode}")
    print(f"showing the {len(tail)} costliest by LOSS (picked minus best measured)\n")

    flips = mirror_ok = 0
    for r in tail:
        shipped = decompose(r, PLAN, oracle=False)
        engine_pred = r["preds"].get(PLAN)
        trust = shipped and engine_pred and abs(shipped[0] / engine_pred - 1.0) < MIRROR_TOLERANCE
        print(f"  {r['q'][:60]}   unique={r['unique']} off={r['offset']} paging={r['paging']}")
        for plan in sorted(r["timed"], key=lambda k: r["timed"][k]):
            pred = r["preds"].get(plan)
            mark = " <-PICKED" if plan == r["picked"] else ("  <-best" if plan == r["best"] else "")
            ratio = f"{pred / r['timed'][plan]:>6.2f}x" if pred else "     --"
            print(f"      {plan:<17} pred {(pred or 0) / 1000:>9.2f} us   true {r['timed'][plan] / 1000:>9.2f} us   {ratio}{mark}")
        if trust:
            mirror_ok += 1
            feat = design_row(PLAN, r["acq"], r["limit"], r["offset"])[0].get(TERM)
            real = r["counters"][PLAN].get(TERM_ORACLE[(PLAN, TERM)][1])
            oracled = decompose(r, PLAN, oracle=True)
            share = 100 * shipped[1] / shipped[0] if shipped[0] else 0
            newbest = min(r["timed"], key=lambda k: (oracled[0] if k == PLAN else (r["preds"].get(k) or float("inf"))))
            flips += newbest == r["best"] and r["picked"] != r["best"]
            print(f"      {TERM}: feature {feat:>10,.0f} vs realized {real or 0:>10,} "
                  f"= {(feat / real) if real else float('nan'):>5.2f}x   term is {share:>4.1f}% of {PLAN}'s prediction")
            print(f"      with the feature set to truth: {PLAN} prices {oracled[0] / 1000:>8.2f} us "
                  f"(was {shipped[0] / 1000:.2f}) -> argmin picks {newbest}")
        else:
            print(f"      (mirror does not reproduce this row's {PLAN} prediction -- term breakdown withheld)")
        print()

    print(f"{mirror_ok} of {len(tail)} rows had a mirror-exact rebuild; correcting the FEATURE alone flips {flips} mis-pick(s).")
    ratios = []
    for r in rows:
        built = design_row(PLAN, r["acq"], r["limit"], r["offset"])
        if not built or TERM not in built[0]:
            continue
        real = r["counters"][PLAN].get(TERM_ORACLE[(PLAN, TERM)][1])
        if real:
            ratios.append(built[0][TERM] / real)
    if ratios:
        s = sorted(ratios)
        q = lambda p: s[min(len(s) - 1, int(p * len(s)))]  # noqa: E731
        print(f"\nfeature/realized across ALL {len(s):,} rows charging the term: "
              f"p10 {q(0.10):.2f}  p50 {statistics.median(s):.2f}  p90 {q(0.90):.2f}")
        print("A refit scales the RATE, which moves every one of those rows. Read the p50 before proposing one.")


if __name__ == "__main__":
    main()
