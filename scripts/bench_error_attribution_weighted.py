"""Where does the engine's prediction error actually come from, weighted by how often it happens?

Every other tool here reports error as a DISTRIBUTION -- a cell reads p50 1.47, a spread is 27x. That
answers "how wrong is this when it happens" and silently drops "how often, and on how much of the
work". Those rank differently, and this project has twice spent a round on a term that was badly
wrong on almost nothing: `stream_perm_steps` carries a 15.5x spread and 0.3% of its plan's predicted
cost, and StreamedSelect's `card_pass` count was exactly 2x wrong on a branch its plan is picked on
zero times.

So this ranks error sources by **error MASS** -- each source's share of the total log-error summed
across queries -- instead of by error rate. A source is worth work when its mass is large, which needs
both a big error and a population to happen on.

Three views, deliberately separate because they are different failure modes:

  ESTIMATES   `matches` against the realized `result_total`. A cardinality error the router acts on.
  COST        the picked plan's `predicted_ns` against its measured `plan_self_ns`. What actually
              becomes latency.
  PER-TERM    the money view. For each cost term whose feature has a realized counter, substitute the
              counter and recompute the arm. The drop in total log-error is that feature's error mass,
              in the model's own units. A term with no counter is reported as UNGRADED rather than as
              zero, because "we cannot see it" and "it is fine" are not the same answer. A term whose
              feature is a CONSTANT has no count to substitute; `TERM_NS_ORACLE` grades those against a
              measured phase instead, marked as a bound. And the whole vector is substituted at once
              per plan, because a single-term column cannot separate "this feature is fine" from "this
              feature's error cancels another's" -- the joint number is the ceiling on what counter
              work can remove for a plan, and what survives it is rate and model-form error.

The per-term substitution is exact rather than approximate: `fit_cost_model.design_row` returns
{term: value} and `CURRENT[plan][term]` holds the shipped coefficient, and that reconstruction is
checked against the engine's own `predicted_ns` per row -- rows where the mirror disagrees are
excluded and counted, so a drifted mirror shows up as a skipped-row count rather than as a finding.

    PYTHONPATH=<wheel> .venv/bin/python scripts/bench_error_attribution_weighted.py --n-queries 8000
"""

from __future__ import annotations

import argparse
import collections
import math
import pathlib
import random
import statistics
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts import costbench  # noqa: E402

# `PROJECT_MODES` / `compose_grades` are the two eligibility gates, IMPORTED rather than restated.
# `bench_feature_accuracy` already decides "does this arm charge this feature on the branch that ran,
# in this distinct-on" for the identical feature/counter pairs; a second copy here is exactly the
# drift `fit_cost_model`'s mirror check exists to catch, and the copy this file did NOT have is what
# produced a 10.2% finding out of nothing.
from scripts.bench_feature_accuracy import PROJECT_MODES, compose_grades  # noqa: E402
from scripts.costbench import load_engine, plan_self_ns  # noqa: E402
from scripts.fit_cost_model import CURRENT, MIRROR_TOLERANCE, design_row  # noqa: E402

NUM_WARMUPS, NUM_TRIALS = costbench.NUM_WARMUPS, costbench.NUM_TRIALS
#: Below this a measured time is dominated by timer resolution and its log-ratio is noise.
MIN_MEASURED_NS = 500.0
#: Below this a realized counter cannot support a substitution -- a counter of 3 makes any feature
#: look 100x wrong. Same floor `bench_feature_accuracy` uses and justifies.
MIN_COUNTER = 100
#: Cells thinner than this are not reported; a share over a handful of rows says nothing.
MIN_ROWS = 30

#: term -> (acquire feature, realized counter). The substitution this tool performs, and the reason
#: only these terms get a mass rather than an UNGRADED marker. `SCAN_PER_ROW` is a different feature
#: per plan -- `stream_scan_units` for StreamedSelect, `scan_units` for GatheredScan -- so the key is
#: (plan, term), the same lesson `bench_term_contributions.MEASURED_SPREAD` records.
#:
#: A pair listed here is NOT automatically substitutable on every row -- see `substitutable`. Three of
#: compose's four entries are charged only on some branches or in some distinct-ons, and substituting a
#: live counter into a column the arm deliberately zeroes injects work the executor never did. That is
#: not a hypothetical: ungated, `PROJECT_PER_PRINTING` read **-372** here (10.2% of the whole model's
#: error mass, ranked as the single largest cancelling term) entirely off 401 `unique=printing` rows
#: where compose runs no projection at all, `project_printings` is a correct 0, and `set_printings` is
#: a live diagnostic. Gated, the same term reads **+12** -- a small, ordinary error source, positive in
#: both projecting modes and on both walk branches.
TERM_ORACLE: dict[tuple[str, str], tuple[str, str]] = {
    ("GatheredScan", "LOOP_PER_CARD"): ("eval_domain", "cards_visited"),
    ("GatheredScan", "SCAN_PER_ROW"): ("scan_units", "printings_examined"),
    ("GatheredScan", "CARD_PASS+FLOOR"): ("residual_card_pass", "card_pass_calls"),
    ("GatheredScan", "PUSH_PER_MATCH"): ("matches", "matches_pushed"),
    ("StreamedSelect", "LOOP_PER_CARD"): ("eval_domain", "cards_visited"),
    ("StreamedSelect", "SCAN_PER_ROW"): ("stream_scan_units", "printings_examined"),
    ("StreamedSelect", "CARD_PASS+FLOOR"): ("stream_residual_card_pass", "card_pass_calls"),
    ("StreamedSelect", "EMIT_PER_MATCH"): ("matches", "matches_pushed"),
    ("StreamedSelect", "PERM_STEP"): ("stream_perm_steps", "perm_steps"),
    # Round 81. `redo_examined` was a published counter no term consumed; it now prices the small-total
    # redo pass's `push_card_matches` walk. Graded here from the term's first day, so the joint
    # substitution below covers it instead of reporting it as UNGRADED.
    ("StreamedSelect", "REDO_SCAN_PER_ROW"): ("stream_redo_printings", "redo_examined"),
    ("PrintingCompose", "WALK_STEP"): ("printings_walked", "printings_examined"),
    ("PrintingCompose", "GATHER_BITTEST_PER_PRINTING"): ("compose_scan_printings", "printings_examined"),
    ("PrintingCompose", "BROADCAST_PER_PRINTING"): ("broadcast_printings", "broadcast_printings"),
    ("PrintingCompose", "PROJECT_PER_PRINTING"): ("project_printings", "set_printings"),
    # GatheredScan's finish phase and its artwork dedupe -- three of the four terms Round 77 reported
    # as UNGRADED while the plan carried 78.7% of all cost error mass.
    #
    # `ARTWORK_PER_PRINTING`'s feature is not a new quantity: `mk_plan_feats` sets
    # `artwork_seen_printings = scan_units` in artwork mode and 0 elsewhere, so its FEATURE error is
    # `SCAN_PER_ROW`'s error, byte for byte, charged a second time at a second rate (0.50 vs 2.06).
    # Graded against the same counter for the same reason -- `push_card_matches`'s artwork arms both
    # return `end - start`, so the dedupe check really does run on every printing `printings_examined`
    # counts. Its mass is therefore not an INDEPENDENT source; it is the same source's second charge.
    ("GatheredScan", "SELECT_PER_PAGE_SLOT"): ("gather_page_span", "select_input_len"),
    ("GatheredScan", "COLLECT_PER_PAGE_ROW"): ("gather_page_rows", "page_rows_collected"),
    ("GatheredScan", "ARTWORK_PER_PRINTING"): ("artwork_seen_printings", "printings_examined"),
}

#: (plan, term) -> the realized NANOSECOND measurement that bounds it, for a term whose feature is a
#: CONSTANT and so has no count to substitute. The substitution replaces the term's whole contribution
#: (`coeff * 1.0`) with the measured value, instead of replacing a feature value inside it.
#:
#: `GatheredScan / FIXED` is the fourth of Round 77's ungraded four and the one that CANNOT get a
#: counter: there is no quantity to count, only a constant. What can bound it is the phase it names.
#: The arm's two constants are per-query setup net of the per-unit work, and `exec_gathered_scan` draws
#: its first phase boundary at exactly that point -- `ns_setup` is everything from entry down to the
#: match loop, and `plan_self_ns` is `ns_setup + ns_loop + ns_finish`, so the substitution stays inside
#: one accounting.
#:
#: Read as a BOUND, not as a counter grade. A counter substitution is exact because the arm's other
#: terms are untouched and only one feature moves; here the claim "the rest of the arm covers
#: `ns_loop + ns_finish`" is what the whole tool is measuring, so what this reports is specifically
#: "how much total error disappears when the fixed term is replaced by the phase it is named for".
TERM_NS_ORACLE: dict[tuple[str, str], str] = {
    ("GatheredScan", "FIXED"): "ns_setup",
    ("GatheredScan", "FIXED_ZERO_MATCH"): "ns_setup",
}

#: (plan, term) -> per-term override of `MIN_COUNTER`, and the two finish-phase terms need one.
#:
#: `MIN_COUNTER` exists because a small counter makes a RATIO explode -- "a counter of 3 makes any
#: feature look 100x wrong". This tool does not form a ratio: it substitutes the counter into the arm
#: and re-measures in NANOSECONDS, where a counter of 3 against a feature of 10 moves the prediction by
#: 25 ns and cannot blow anything up. For these two the floor is therefore not a noise guard but a
#: population filter, and a costly one: `page_rows_collected` is bounded above by `limit`, drawn from
#: `costbench.LIMITS` (10, 100, 175), so a floor of 100 grades the term on the two large page sizes
#: only; `select_input_len` at 100 drops every narrow query. Both counters are exact integers the
#: executor reports, not estimates. Left at `MIN_COUNTER` for every pre-existing term so the numbers
#: stay comparable with the runs already on record.
TERM_MIN_COUNTER: dict[tuple[str, str], int] = {
    ("GatheredScan", "COLLECT_PER_PAGE_ROW"): 1,
    ("GatheredScan", "SELECT_PER_PAGE_SLOT"): 1,
}

#: (plan, term) -> the distinct-ons whose executor does the work the term prices. Absent means all.
#:
#: `ARTWORK_PER_PRINTING` is the artwork dedupe check and `mk_plan_feats` sets its feature to 0 outside
#: artwork mode, correctly -- no such check runs there. Without this gate the substitution pushes a
#: live `printings_examined` into a term the arm charges ZERO for and reports the manufactured
#: difference as explained error. Same trap `bench_feature_accuracy.PROJECT_MODES` guards for compose's
#: projection pass, which the arm likewise zeroes in printing mode.
TERM_MODES: dict[tuple[str, str], frozenset[str]] = {
    ("GatheredScan", "ARTWORK_PER_PRINTING"): frozenset({"artwork"}),
}


def log_err(pred: float, real: float) -> float:
    """Absolute log ratio -- symmetric in over- and under-prediction, which a percent error is not."""
    if pred <= 0 or real <= 0:
        return 0.0
    return abs(math.log(pred / real))


def collect(engine: object, sampler: QuerySampler, rng: random.Random, budget: costbench.Budget) -> list[dict]:
    """One row per query, carrying the picked plan's prediction, its measurement and its term vector."""
    rows: list[dict] = []
    for sample in costbench.iter_samples(engine, sampler, rng, budget, vary_prefer=True):
        acq = sample.acquire
        picked = next((p for p in sample.plans if p.get("picked")), None)
        if picked is None or not picked.get("trials_ns"):
            continue
        measured = plan_self_ns(picked, acq)
        predicted = picked.get("predicted_ns")
        if not measured or measured < MIN_MEASURED_NS or not predicted or predicted <= 0:
            continue
        built = design_row(picked["plan"], acq, sample.kw["limit"], sample.kw["offset"])
        rows.append(
            {
                "plan": picked["plan"],
                "acquire": acq["count_source"],
                "unique": sample.kw["unique"],
                # The two axes `PlanFeatures` carries no field for. `orderby` reaches the model only
                # through `perm_walk_span`, which collapses to `n_cards` on 94-98% of rows, and
                # `prefer` only through which quantity the acquire picks for a handful of features --
                # so slicing mass by them is how you find out whether that indirection is costing
                # anything. See local-engine-cost-model-mode-sort-prefer-axes.md.
                "orderby": sample.kw["orderby"],
                "prefer": sample.kw.get("prefer", "default"),
                "paging": picked.get("paging_taken") if picked["plan"] == "PrintingCompose" else None,
                "predicted": float(predicted),
                "measured": float(measured),
                "est_matches": acq["matches"],
                "true_total": picked.get("result_total") or 0,
                "acq": acq,
                "counters": picked,
                "built": built,
            }
        )
    return rows


def share_table(rows: list[dict], key: Callable[[dict], object], label: str, err: str) -> None:
    """Each slice's share of total error mass, beside its share of rows -- the two rank differently."""
    total = sum(r[err] for r in rows) or 1.0
    groups: dict[object, list[dict]] = collections.defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    print(f"\n{label}")
    print(f"  {'slice':<38} {'rows':>7} {'row%':>7} {'err mass%':>10} {'median |log|':>13} {'mass/row':>9}")
    for name, sub in sorted(groups.items(), key=lambda kv: -sum(r[err] for r in kv[1])):
        if len(sub) < MIN_ROWS:
            continue
        mass = sum(r[err] for r in sub)
        print(
            f"  {name!s:<38} {len(sub):>7,} {100 * len(sub) / len(rows):>6.1f}% {100 * mass / total:>9.1f}% "
            f"{statistics.median([r[err] for r in sub]):>13.3f} {mass / len(sub):>9.3f}"
        )


def substitutable(r: dict, terms: dict, plan: str, term: str, oracle: tuple[str, str]) -> bool:
    """Whether `oracle`'s counter-for-feature swap on this row measures the FEATURE and nothing else.

    A substitution is only a measurement of a feature where the arm actually multiplies that feature
    by a rate on the execution that happened. Where it does not, the swap adds cost for work that never
    ran, and the row's error grows for a reason that has nothing to do with the feature under test --
    which reads, in the table below, as a large mass of EITHER sign, indistinguishable from a real
    finding. Four gates; each one was added because an ungated run reported a number that was not real.

    - **Branch.** Compose charges `printings_walked` only on the walks and `compose_scan_printings`
      only on the gather. Keyed on `paging_taken` -- what RAN -- never on the acquire's predicted
      `compose_paging`, or a declined walk is graded as a gather. Ungated, `GATHER_BITTEST` graded 982
      rows of which only 13 took the gather, manufacturing +0.8%.
    - **Distinct-on**, via `TERM_MODES` plus `PROJECT_MODES`. Compose projects in card/artwork only,
      and GatheredScan's artwork dedupe runs in artwork only; the arm correctly charges 0 elsewhere
      while the counter stays live. Ungated, `PROJECT_PER_PRINTING` read **-10.2%** off printing-mode
      rows alone, and `ARTWORK_PER_PRINTING` read 5.3% instead of 1.9%.
    - **Live indicator.** `FIXED` and `FIXED_ZERO_MATCH` are mutually exclusive columns that are BOTH
      present in every GatheredScan vector, one of them zero. `term in terms` is therefore not enough
      -- the column has to be the one carrying this row.
    - **Counter floor**, per term via `TERM_MIN_COUNTER`, because a page-sized counter is legitimately
      tiny and the shared `MIN_COUNTER` would drop every narrow query.
    """
    feature, counter = oracle
    real = r["counters"].get(counter)
    if r["plan"] != plan or term not in terms or r["acq"].get(feature) is None:
        return False
    if not terms[term]:
        return False
    if real is None or real < TERM_MIN_COUNTER.get((plan, term), MIN_COUNTER):
        return False
    modes = TERM_MODES.get((plan, term))
    if modes is not None and r["unique"] not in modes:
        return False
    if feature == "project_printings" and r["unique"] not in PROJECT_MODES:
        return False
    return not (plan == "PrintingCompose" and not compose_grades(r["paging"], feature))


def substitute(usable: list[tuple], plan: str, term: str, oracle: tuple[str, str]) -> tuple[float, int]:
    """Total cost log-error with one term's feature replaced by its realized counter, and the n."""
    after, n_sub = 0.0, 0
    for r, terms, excess, coeffs in usable:
        real = r["counters"].get(oracle[1])
        if not substitutable(r, terms, plan, term, oracle):
            after += log_err(r["predicted"], r["measured"])
            continue
        n_sub += 1
        swapped = sum(coeffs[t] * (float(real) if t == term else v) for t, v in terms.items()) + excess
        after += log_err(swapped, r["measured"])
    return after, n_sub


def substitute_ns(usable: list[tuple], plan: str, term: str, phase: str) -> tuple[float, int]:
    """Total cost log-error with one CONSTANT term's whole contribution replaced by a measured phase.

    The counter form above swaps a feature value inside `coeff * value`; a constant term has no value
    to swap, so this drops `coeff * value` entirely and adds the measured nanoseconds in its place. See
    `TERM_NS_ORACLE` for why `ns_setup` is the right measurement for `GatheredScan`'s fixed term and
    for the caveat that makes this a bound rather than a grade.
    """
    after, n_sub = 0.0, 0
    for r, terms, excess, coeffs in usable:
        real = r["counters"].get(phase)
        # `terms[term]` is the INDICATOR, not a count: `design_row` emits FIXED and FIXED_ZERO_MATCH on
        # every row with one of them at 1.0 and the other at 0.0, so `term in terms` alone would grade
        # the branch this row did not take and add a whole setup phase the arm never charged.
        #
        # A zero phase reading is timer resolution, not a measurement of zero setup: substituting it
        # would drive the whole prediction toward 0 and manufacture an enormous log error.
        if r["plan"] != plan or not terms.get(term) or not real:
            after += log_err(r["predicted"], r["measured"])
            continue
        n_sub += 1
        swapped = sum(coeffs[t] * v for t, v in terms.items() if t != term) + float(real) + excess
        after += log_err(swapped, r["measured"])
    return after, n_sub


def substitute_all(usable: list[tuple], plan: str) -> tuple[float, int]:
    """Total cost log-error with EVERY oracle-backed feature of one plan replaced at once.

    The per-term column answers "is this feature a source"; it cannot answer "how much of this plan's
    error is feature error at all", because the terms interact -- one term's over-count cancelling
    another's under-count shows up as two small or negative single-term numbers while the pair is
    jointly large. Substituting the whole vector at once separates FEATURE error from what is left:
    the rates, the model's form, and measurement noise. That remainder is the honest ceiling on what
    any amount of counter work can remove.

    Nanosecond-bounded terms are included on the same footing as counter terms -- see `TERM_NS_ORACLE`
    for why `ns_setup` is a bound rather than a grade, which makes this a bound too.
    """
    counters = {t: fc for (p, t), fc in TERM_ORACLE.items() if p == plan}
    phases = {t: ph for (p, t), ph in TERM_NS_ORACLE.items() if p == plan}
    after, n_sub = 0.0, 0
    for r, terms, excess, coeffs in usable:
        if r["plan"] != plan:
            after += log_err(r["predicted"], r["measured"])
            continue
        n_sub += 1
        total = excess
        for t, v in terms.items():
            if t in phases and terms.get(t) and r["counters"].get(phases[t]):
                total += float(r["counters"][phases[t]])
                continue
            swap = v
            # Same eligibility as the per-term columns, so the joint number sums over the same
            # population they do rather than a laxer one.
            if t in counters and substitutable(r, terms, plan, t, counters[t]):
                swap = float(r["counters"][counters[t][1]])
            total += coeffs[t] * swap
        after += log_err(total, r["measured"])
    return after, n_sub


def per_term(rows: list[dict]) -> None:
    """Substitute each term's realized counter and report the total cost log-error it removes."""
    usable, mirror_bad = [], 0
    for r in rows:
        if r["built"] is None:
            continue
        terms, excess = r["built"]
        coeffs = CURRENT[r["plan"]]
        mine = sum(coeffs[t] * v for t, v in terms.items()) + excess
        if abs(mine / r["predicted"] - 1.0) >= MIRROR_TOLERANCE:
            mirror_bad += 1
            continue
        usable.append((r, terms, excess, coeffs))
    if not usable:
        print("\nno rows with a reconstructable prediction")
        return

    base = sum(log_err(r["predicted"], r["measured"]) for r, _, _, _ in usable)
    print(f"\n{'=' * 92}\nPER-TERM ERROR MASS -- substitute the realized counter, see what error disappears")
    print(f"{'=' * 92}")
    print(f"{len(usable):,} picked rows with a mirror-exact reconstruction ({mirror_bad} excluded on mirror drift)")
    print(f"total cost log-error mass: {base:.1f}\n")
    print(f"  {'plan / term':<52} {'rows':>7} {'mass removed':>13} {'share':>8}")
    results, thin = [], []
    for (plan, term), oracle in TERM_ORACLE.items():
        after, n_sub = substitute(usable, plan, term, oracle)
        (results if n_sub >= MIN_ROWS else thin).append((base - after, plan, term, n_sub, ""))
    # A phase timing bounds a term that has no countable quantity -- `FIXED` is a constant, so the
    # measured `ns_setup` it stands for is the only thing that can grade it. Marked as a bound, since
    # substituting a measurement for a prediction is not the same experiment as swapping two counts.
    for (plan, term), phase in TERM_NS_ORACLE.items():
        after, n_sub = substitute_ns(usable, plan, term, phase)
        if n_sub >= MIN_ROWS:
            results.append((base - after, plan, term, n_sub, f"  [bound, vs measured {phase}]"))
    for removed, plan, term, n_sub, note in sorted(results, reverse=True):
        print(f"  {plan + ' / ' + term:<52} {n_sub:>7,} {removed:>13.2f} {100 * removed / base:>7.1f}%{note}")
    # Named rather than dropped. A term whose eligible population collapsed is UNGRADED on this
    # sample, which is a different answer from "small mass" and must not read as one -- compose's
    # gather terms land here on any run where the router picks the walk branches.
    print(f"\n  Too few eligible rows to grade on this sample (UNGRADED, not clean; need {MIN_ROWS}):")
    for _, plan, term, n_sub, _note in sorted(thin, key=lambda x: (x[1], x[2])):
        print(f"    {plan + ' / ' + term:<50} {n_sub:>7,} rows")
    print("\n  Positive = substituting truth REMOVES error, so the feature is a real source.")
    print("  Negative = the feature's error was CANCELLING another term's; fixing it alone makes the")
    print("  arm worse, which is exactly what Round 76 measured and shipped anyway on correctness grounds.")
    print("  Before reading a negative cell as a cancellation, check `substitutable`: an ungated swap")
    print("  charges work the executor never ran, and that is the OTHER way a cell goes negative.")

    joint(usable, base)
    ungraded_report(usable)


def joint(usable: list[tuple], base: float) -> None:
    """Per plan, the error mass its whole feature vector removes when substituted at once."""
    print("\n  ALL of one plan's oracle-backed features substituted AT ONCE -- feature error jointly,")
    print("  against the plan's own share of the mass. What survives is rate/form error, not counting.")
    print(f"  {'plan':<52} {'rows':>7} {'plan mass':>10} {'removed':>10} {'of plan':>8}")
    for plan in sorted({p for p, _ in TERM_ORACLE} | {p for p, _ in TERM_NS_ORACLE}):
        plan_mass = sum(log_err(r["predicted"], r["measured"]) for r, _, _, _ in usable if r["plan"] == plan)
        after, n_sub = substitute_all(usable, plan)
        if n_sub >= MIN_ROWS and plan_mass > 0:
            print(f"  {plan:<52} {n_sub:>7,} {plan_mass:>10.1f} {base - after:>10.2f} {100 * (base - after) / plan_mass:>7.1f}%")


def ungraded_report(usable: list[tuple]) -> None:
    """Terms carrying nonzero cost that neither oracle can touch -- unmeasured, which is not clean."""
    ungraded = collections.Counter()
    for r, terms, _, coeffs in usable:
        for t, v in terms.items():
            if (r["plan"], t) not in TERM_ORACLE and (r["plan"], t) not in TERM_NS_ORACLE and coeffs[t] * v > 0:
                ungraded[f"{r['plan']} / {t}"] += 1
    print("\n  UNGRADED terms that carry nonzero cost (no counter exists -- unmeasured, not clean):")
    for name, n in ungraded.most_common(8):
        print(f"    {name:<50} {n:>7,} rows")


def main() -> None:
    """Rank estimate and cost error sources by mass rather than by rate."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n-queries", type=int, default=8000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    args = parser.parse_args()

    engine = load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".errattr.store"))
    sampler = QuerySampler(args.corpus, args.mode)
    budget = costbench.Budget(sample=args.n_queries, warmups=NUM_WARMUPS, trials=NUM_TRIALS)
    rows = collect(engine, sampler, random.Random(args.seed), budget)
    for r in rows:
        r["cost_err"] = log_err(r["predicted"], r["measured"])
        r["est_err"] = log_err(float(r["est_matches"]), float(r["true_total"])) if r["true_total"] else 0.0
    print(f"\n{len(rows):,} picked rows, mode={args.mode}, bound={args.n_queries:,} queries")

    est = [r for r in rows if r["true_total"] >= MIN_COUNTER]
    print(f"\n{'=' * 92}\nESTIMATE ERROR -- `matches` against realized `result_total` ({len(est):,} rows)\n{'=' * 92}")
    print(f"total estimate log-error mass: {sum(r['est_err'] for r in est):.1f}")
    share_table(est, lambda r: r["acquire"], "by acquire route", "est_err")
    share_table(est, lambda r: r["unique"], "by distinct-on", "est_err")

    print(f"\n{'=' * 92}\nCOST ERROR -- picked plan's predicted_ns against measured\n{'=' * 92}")
    print(f"total cost log-error mass: {sum(r['cost_err'] for r in rows):.1f}")
    share_table(rows, lambda r: r["plan"], "by plan", "cost_err")
    share_table(rows, lambda r: f"{r['plan']} [{r['acquire']}]", "by plan and acquire route", "cost_err")
    share_table(rows, lambda r: r["unique"], "by distinct-on", "cost_err")
    share_table(rows, lambda r: r["orderby"], "by SORT COLUMN -- the axis with no PlanFeatures field", "cost_err")
    share_table(rows, lambda r: r["prefer"], "by PREFER -- reaches the model only via feature choice", "cost_err")
    compose = [r for r in rows if r["plan"] == "PrintingCompose"]
    if len(compose) >= MIN_ROWS:
        share_table(compose, lambda r: f"compose {r['paging']}", "compose only, by paging branch taken", "cost_err")
    per_term(rows)


if __name__ == "__main__":
    main()
