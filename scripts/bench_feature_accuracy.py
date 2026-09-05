"""Do the cost model's FEATURES match what the executor actually does? Per cell, against counters.

First link in the chain the rest of this toolkit walks:

    cardinality estimate -> cost model -> coefficients -> plan choice

Everything else here measures the far end. `bench_cost_model_agreement.py` and
`bench_cost_error_percentiles.py` compare *predicted time* to *real time*, which conflates all four
links; `fit_cost_model.py` fits coefficients and can only do so honestly once the features are right.
This tool isolates the first link by comparing each feature to the executor counter that realizes it,
so a mis-counted feature is visible as itself rather than as a rate that will not calibrate.

Why it earns a place: the compose branch handed the PRINTING count to `result_total` in artwork mode,
where it is consumed as a per-result push count. `matches_pushed` is deduped, so the feature read a
median 1.95x the truth. That survived 154k paired A/B queries, both percentile matrices and a
coefficient fit, because a 2x feature error is absorbed by whatever rate correlates with it and shows
up only as spread. Sliced here it is a single cell reading 1.95 with everything else near 1.0.

Ratio is **feature / counter**, so **>1 means the feature OVER-counts** the work done.

Read the same way as `bench_cost_error_percentiles.py`: a tight row off 1.0 is a systematic bias worth
fixing outright, a wide row means the feature is right on average but driven by something unmodelled,
and slicing by distinct-on matters because a feature can be exact in one mode and 2x off in another
while the pooled median looks fine.

    .venv/bin/python scripts/bench_feature_accuracy.py --seconds 60

For a PAIRED before/after read, bound the run by query COUNT rather than by time -- see
`--n-queries`. Two time-boxed runs of the same seed do not grade the same rows, and these cells are
small enough that the truncation alone moves them.

    PYTHONPATH=<wheel> .venv/bin/python scripts/bench_feature_accuracy.py --n-queries 40000
"""

from __future__ import annotations

import argparse
import math
import pathlib
import random
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts import costbench  # noqa: E402
from scripts.costbench import load_engine  # noqa: E402

# Deliberately below the shared (2, 7) default, and the one place in the toolkit where that is
# justified: the counters this harness reads are deterministic for a given query, so the trials exist
# only to make each plan run once. Extra rounds buy nothing and cost sample breadth.
NUM_WARMUPS = 1
NUM_TRIALS = 2
# The time box a bare invocation gets, unchanged from when `--seconds` was the only bound.
DEFAULT_SECONDS = 60.0
MIN_ROWS = costbench.MIN_ROWS
# A cell's median must land inside this to be considered calibrated, matching the agreement bar in
# bench_cost_model_agreement.py so the two tools grade on the same scale.
AGREE_LO, AGREE_HI = 0.8, 1.25
# Below this the counter is too small for a ratio to mean anything -- a feature of 3 against a counter
# of 1 is a 3x "error" that costs nanoseconds.
MIN_COUNTER = 100

# feature name on the shared acquire vector -> the executor counter that realizes it.
#
# `scan_units` is graded against `printings_examined`, NOT `printing_span`. The latter is the
# printing SPAN under the candidate cards, computed by the caller before the match kernel runs, so it
# reports what a full scan would have cost rather than what happened. The two coincide in printing and
# artwork mode -- those loops really do traverse the span -- which is why grading against the span
# looked fine everywhere except card mode, where every kernel short-circuits. Reading the span as the
# work done is how `cost.rs` came to assert that the scan plans "walk the full printing span of their
# candidates in CARD mode too, not one row each"; they do not.
#
# `stream_perm_steps` is the derived walk term, not a stored field: `perm_walk_span` reaches cost ONLY
# through `min(page_span * perm_walk_span / matches, perm_walk_span)`, so grading the raw span against
# any counter is meaningless. `cost.rs` exposes the term itself for the same reason it exposes
# `printings_walked` -- one definition, read by the arm and reported to `explain`, rather than a second
# copy of the formula here. Graded against `perm_steps`, every permutation entry the walk touched
# including the zero-count skips.
#
# `residual_card_pass` is the derived residual-gated per-card term (`eval_domain` when
# `residual_tier_ns100 > 0`, else 0), exposed by `cost.rs` for the same reason `stream_perm_steps` is:
# the gate belongs to the arm, and a harness holding its own copy of it is a second definition. It is
# the largest single term in the model -- 58% of GatheredScan's predicted time and 28% of
# StreamedSelect's, because its coefficient IS the residual floor -- and nothing graded it before.
# Like `scan_units` it is a PER-PLAN feature (see `residual_feature`): `StreamedSelect` runs two
# passes, so its arm multiplies `stream_residual_card_pass` and grading it on the shared key reads an
# exact 2x under-count on the small-total branch.
#
# `broadcast_printings` and `project_printings` are compose's two `LINEAR_PASS` build terms, 37% and
# 23% of that plan's predicted time. Their counters are what the build really touched:
# `broadcast_printings` (the counter) is summed by the two card->printing passes themselves, and
# `set_printings` is `popcount(pbits)`, which IS the projection's realized length -- both
# `printing_bits_to_card_bits` and `printing_bits_to_artwork_bits` iterate the composed bitmap's set
# bits and nothing else.
#
# `scatter_printings` is deliberately absent, and that is a finding rather than an omission: the
# acquire estimate and the build read the SAME source for it. `compose_printing_estimate`'s range arm
# takes `idx.range(lo, hi)` and charges `e - s`; `range_leaf_bits` scatters `idx.range_pids(lo, hi)`,
# which is `pids[s..e]` off that identical call. Postings leaves are the same story through
# `len_of`/`bits`, and both sides fuse same-index `And` children with `fuse_and_range_children(v,
# indexes, false)` -- the same third argument. A counter here could only ever read 1.000 by identity,
# which is worse than no counter because it looks like a measurement. Any error in that term is in the
# RATE, not the feature.
#
# `gather_page_span` and `gather_page_rows` are `GatheredScan`'s finish-phase pair, added because the
# weighted error attribution found their two terms live on 3,290 picked rows with no counter behind
# either. They are DERIVED terms exposed from `cost.rs` (like `stream_perm_steps`), not stored fields,
# and they grade very differently from each other:
#
# `gather_page_span` charges `min(offset + limit, matches)` for the finish phase's quickselect. What
# `select_page` really quickselects over is `GatherSelect`'s buffer, which is pruned back to
# `k = offset + limit` only once it has grown `GATHER_PRUNE_CHUNK` (4,096) past `k` -- so below
# `k + 4,096` matches NO prune has ever run and the buffer still holds every match. That is most of
# real traffic: the realized `select_input_len` is the whole match set while the arm charges one page.
#
# `gather_page_rows` charges `clamp(matches - offset, 0, limit)` for the collect, and the realized
# `page_rows_collected` is that identical clamp on the REALIZED total. So unlike every other pair here
# the cell carries no shape error at all -- it reports the cardinality estimate propagating into the
# page phase and nothing else. Kept anyway, because "this term's only error is upstream" is a finding
# about where NOT to work, and it is the one term in this arm for which that can be said exactly.
PAIRS = (
    ("matches", "matches_pushed"),
    ("eval_domain", "cards_visited"),
    ("scan_units", "printings_examined"),
    ("stream_perm_steps", "perm_steps"),
    ("residual_card_pass", "card_pass_calls"),
    ("broadcast_printings", "broadcast_printings"),
    ("project_printings", "set_printings"),
    ("gather_page_span", "select_input_len"),
    ("gather_page_rows", "page_rows_collected"),
    # Round 81: `redo_examined` stopped being a counter with no term behind it. See
    # `cost::stream_redo_printings` -- (matching cards) x (corpus printings per card), because nothing
    # on `PlanFeatures` describes the printing span of the matching subset specifically.
    ("stream_redo_printings", "redo_examined"),
)

#: Plans whose arm charges the two finish-phase page terms. Only `GatheredScan` does: no other arm has
#: a page-slot or page-row term at all, and no other executor publishes the counters -- `MIN_COUNTER`
#: would drop those rows on a zero counter anyway, but the gate keeps the reason in the code.
#: `run_query_streamed`'s small-total exit and `gather_composed_page` both drive a `GatherSelect` too,
#: and neither is priced for it; that is unmeasured work, not a grading opportunity.
GATHER_PAGE_FEATURES = frozenset({"gather_page_span", "gather_page_rows"})
GATHER_PAGE_TERM_PLANS = frozenset({"GatheredScan"})
#: The redo-pass printing walk, and the one arm that charges it. Same shape as the pair above: only
#: `run_query_streamed` has a small-total redo exit, and only its arm prices the walk. Every other
#: plan reports `redo_examined == 0`, which `MIN_COUNTER` would drop anyway -- the gate keeps the
#: reason visible instead of leaving it to a filter's side effect.
REDO_SCAN_FEATURE = "stream_redo_printings"
REDO_SCAN_TERM_PLANS = frozenset({"StreamedSelect"})

#: `gather_page_rows` is BOUNDED BY `limit`, which the sampler draws from `costbench.LIMITS`
#: (10, 100, 175) -- so the shared `MIN_COUNTER` of 100 is not a noise floor here, it is a filter that
#: silently drops every `limit=10` row and grades the term on the two large page sizes only. The floor
#: exists because a counter of 3 makes any feature look 100x wrong; a page size cannot, being an exact
#: small integer both sides. One row is enough for a ratio here.
GATHER_PAGE_ROWS_MIN_COUNTER = 1

#: Plans whose cost arm charges the residual-gated per-card term. Both materializing plans do, at
#: their own rates (`GATHER_CARD_PASS_NS` + `GATHER_RESIDUAL_FLOOR_NS`, `STREAM_CARD_PASS_NS` +
#: `STREAM_RESIDUAL_FLOOR_NS`); no other plan verifies a residual at all -- compose composes exact
#: membership and the two bitmap plans read a precomputed plane -- so their `card_pass_calls` is 0 and
#: grading them would compare a feature to work the arm never charges for.
RESIDUAL_TERM_PLANS = frozenset({"GatheredScan", "StreamedSelect"})

#: Distinct-ons where compose runs a printing->result projection pass at all. Printing mode runs none
#: (the composed bitmap already IS the answer), the arm charges `project_printings = 0` there, and
#: `set_printings` is nonzero on those rows -- so grading printing mode would divide a correct 0 by a
#: live counter and report every such query as a 100% under-count of a pass that does not exist.
PROJECT_MODES = frozenset({"card", "artwork"})

#: Plans whose cost arm charges a permutation-walk term. Only `StreamedSelect` does: `GatheredScan`
#: publishes `perm_steps: 0` explicitly ("never walks the permutation") and compose's own walks count
#: `printings_examined` instead, so grading them here would compare a feature to a counter neither the
#: arm nor the executor connects. `MIN_COUNTER` would drop those rows anyway; being explicit keeps the
#: reason in the code rather than in a filter's side effect.
WALK_TERM_PLANS = frozenset({"StreamedSelect"})
# Kept alongside so one run shows both gradings. The gap between the two columns IS the miscount, and
# reporting it as a column beats asserting it in prose.
SPAN_COUNTER = "printing_span"


#: Which of the three PAIRS features each compose paging branch's cost arm actually multiplies by a
#: rate. `Perm` and `OrderbyWalk` are priced `printings_walked * WALK_STEP + limit * WALK_EMIT_PER_ROW`
#: -- they charge NEITHER `matches` nor `eval_domain`, and grading those against a walk that stops at
#: `page_offset + limit` produced cells reading 100-200x off numbers the model never reads. Only
#: `Gather` charges all three (`eval_domain`, `compose_scan_printings`, `matches`).
#: The `build` half of compose's arm, charged on EVERY exit rather than per branch. The bitmap is
#: composed and projected to the result space before `printing_compose_fastpath` picks a paging branch
#: at all -- it is what the fastpath times as `ns_build` -- so unlike the page terms these do not
#: depend on which branch ran. `design_row` adds them the same way: unconditionally, alongside
#: whichever page term applies.
COMPOSE_BUILD_CHARGES = frozenset({"broadcast_printings", "project_printings"})

COMPOSE_ARM_CHARGES: dict[str, frozenset[str]] = {
    "Perm": frozenset({"printings_walked"}) | COMPOSE_BUILD_CHARGES,
    "OrderbyWalk": frozenset({"printings_walked"}) | COMPOSE_BUILD_CHARGES,
    "Gather": frozenset({"eval_domain", "compose_scan_printings", "matches"}) | COMPOSE_BUILD_CHARGES,
    # The walk was available, was attempted, declined, and fell into the gather -- so the gather is
    # what ran and the gather's terms are what to grade.
    "GatherWalkDeclined": frozenset({"eval_domain", "compose_scan_printings", "matches"}) | COMPOSE_BUILD_CHARGES,
    # An empty page (or one starting past the end) returns before any paging branch, so no page term
    # describes it -- but the BUILD ran, and published its counters, which is exactly why this exit
    # needs an entry of its own instead of falling through to "grades nothing". `set_printings` is 0
    # whenever the total is, so those rows drop on `MIN_COUNTER` rather than on this gate.
    "EmptyPage": COMPOSE_BUILD_CHARGES,
}


def compose_grades(paging_taken: str, feat: str) -> bool:
    """Whether compose's arm charges `feat` on the branch it actually took.

    Keyed on `paging_taken` -- what RAN -- not on the acquire's `compose_paging`, which is the
    model's PREDICTION of the branch. Those disagree exactly where a walk was predicted and declined,
    and grading a gather's counters against a walk's terms is how the two get conflated.
    """
    charged = COMPOSE_ARM_CHARGES.get(paging_taken)
    # An exit with no cost arm of its own (EmptyPage, the declines): nothing ran that a term describes.
    return feat in charged if charged is not None else False


def scan_feature(plan: str, paging: str, tier_ns100: int) -> str | None:
    """Which feature the ARM actually charges its printing scan on, or None if it charges none.

    One shared vector costs every plan, but they do not all read the same field, and comparing a
    counter to a feature the arm never touches manufactures a defect. Compose walks the set bits of
    its composed bitmap (`compose_scan_printings`) when it pages by Gather, and stops at
    `page_offset + limit` when it pages by walking (`printings_walked`); only the materializing scan
    plans read `scan_units`.

    `StreamedSelect` reads it only WITH a residual. Its arm is
    `if tier_ns > 0.0 { stream_scan_units * STREAM_SCAN_PER_ROW_NS } else { 0.0 }` -- with `all_match`
    (tier 0) P3 walks no printings and the term is switched off entirely. Graded anyway, those rows read
    p50 2.72 / p70 3.08 against `printings_examined`, because `scan_units` there is GatheredScan's
    full-span quantity while StreamedSelect's counting kernel answers existence from the first
    matching printing. That is not a feature error -- it is a number the model never multiplies by
    anything -- and reporting it as one sent `fit_cost_model.py`'s `counter_check` into refusing to
    fit this plan at all.

    And the field it reads is `stream_scan_units`, NOT `scan_units` -- this returned the latter until
    Round 69, which graded StreamedSelect against a number its arm never touches. The two default to
    equal (`mk_plan_feats` seeds `stream_scan_units: scan_units`) and diverge wherever an acquire knows
    P3 examines fewer printings: `residual_card_invariant` zeroes it, and the legality
    divergent-share correction rescales it. `lib.rs`'s own comment at that second site says the value is
    "reported as 0 so `bench_feature_accuracy` grades this against the realized `printings_examined`" --
    an intent this function silently defeated.
    """
    if plan == "PrintingCompose":
        # `GatherWalkDeclined` IS the gather -- a walk was attempted, declined, and fell into it.
        return "compose_scan_printings" if paging in ("Gather", "GatherWalkDeclined") else "printings_walked"
    if plan == "StreamedSelect":
        return None if tier_ns100 == 0 else "stream_scan_units"
    return "scan_units"


def residual_feature(plan: str) -> str | None:
    """Which per-card `card_pass` feature the ARM multiplies, or None if this plan verifies none.

    Same shape and same `None` contract as `scan_feature`, for the same reason: one shared vector,
    arms doing different amounts of work with it. `None` is compose and the two bitmap plans, which
    verify no residual at all -- their `card_pass_calls` is 0 and grading them would compare a feature
    to work no arm charges for. `exec_gathered_scan` calls `card_pass` once per visited candidate, so its
    arm charges `residual_card_pass`. `run_query_streamed` runs TWO passes -- a counting pass over the
    candidates and, on the small-total exit, a redo over every card with a nonzero count -- so its arm
    charges `stream_residual_card_pass`, which is the first plus the second.

    Grading StreamedSelect on the shared key read p50 **0.500** on the small-total branch (3,213 rows
    of a 40,000-query sample): an exact 2x under-count, invisible in the pooled `<StreamedSelect>` cell
    at 0.988 and inside the [0.8, 1.25] band. Both keys come from `cost.rs` so neither is a second copy
    of a gate.
    """
    if plan not in RESIDUAL_TERM_PLANS:
        return None
    return "stream_residual_card_pass" if plan == "StreamedSelect" else "residual_card_pass"


#: The two per-plan spellings of the residual per-card term, for the slices that select it by name.
RESIDUAL_FEATURES = frozenset({"residual_card_pass", "stream_residual_card_pass"})

percentile = costbench.percentile


def arm_charges(feat: str, plan: str, paging: str, unique: str) -> bool:
    """Whether THIS plan's arm multiplies `feat` by a rate on THIS query, per-plan spellings resolved.

    One shared feature vector costs every plan, so a feature being present says nothing about whether
    the arm being graded reads it. Grading a counter against a term no arm charges manufactures a
    defect -- which has happened here three times (StreamedSelect's `scan_units` with no residual,
    compose's `project_printings` in printing mode, the walk branches' `matches`/`eval_domain`).
    """
    if feat == "stream_perm_steps" and plan not in WALK_TERM_PLANS:
        return False  # this plan's arm has no permutation-walk term
    if feat == "project_printings" and unique not in PROJECT_MODES:
        return False  # no projection pass exists in printing mode; the arm charges 0 and is right
    if feat in COMPOSE_BUILD_CHARGES and plan != "PrintingCompose":
        # A range acquire sets `project_printings`/`scatter_printings` on the SHARED vector so a
        # competing compose is costed honestly, but no other plan's arm reads them and no other
        # executor publishes the counters. `MIN_COUNTER` would drop these rows on a zero counter;
        # saying so here keeps the reason out of a filter's side effect.
        return False
    if feat in GATHER_PAGE_FEATURES and plan not in GATHER_PAGE_TERM_PLANS:
        return False  # no other arm has a finish-phase page term to grade
    if feat == REDO_SCAN_FEATURE and plan not in REDO_SCAN_TERM_PLANS:
        return False  # no other executor has a small-total redo exit, let alone a term for it
    # This branch's arm never multiplies this feature by a rate.
    return not (plan == "PrintingCompose" and not compose_grades(paging, feat))


def collect(engine: object, sampler: QuerySampler, rng: random.Random, budget: costbench.Budget) -> list[dict]:
    """One row per (query, plan, feature) where the plan reported the matching counter."""
    rows: list[dict] = []
    # `prefer` is sampled, not pinned: it decides whether the card-mode kernels stop at the first
    # qualifying printing or must score every one, which is the single largest per-card work
    # difference any sampled parameter reaches -- and the cost model cannot see it, since
    # `PlanFeatures` does not carry `prefer` (see `explain`'s doc in lib.rs). A run pinned to
    # `default` measures only the short-circuiting path and reads the feature as though the
    # long path did not exist.
    for sample in costbench.iter_samples(engine, sampler, rng, budget, vary_prefer=True):
        acq = sample.acquire
        for plan in sample.plans:
            if not plan["trials_ns"]:
                continue  # declined: it ran nothing, so there is no counter to check against
            # `compose_paging` is the model's PREDICTION; `paging_taken` is what ran. Label and grade
            # compose on the latter -- they disagree exactly where a walk was predicted and declined.
            paging = plan.get("paging_taken") if plan["plan"] == "PrintingCompose" else acq["compose_paging"]
            for feat, counter in PAIRS:
                # Two features have a per-PLAN spelling, because one shared vector costs every plan
                # but they do not all read the same field; `None` means this arm charges no such
                # term for this query and there is nothing to grade.
                if feat == "scan_units":
                    feat = scan_feature(plan["plan"], paging, acq["residual_tier_ns100"])  # noqa: PLW2901 - the arm decides
                elif feat == "residual_card_pass":
                    feat = residual_feature(plan["plan"])  # noqa: PLW2901 - ditto
                if feat is None or not arm_charges(feat, plan["plan"], paging, sample.kw["unique"]):
                    continue
                got = plan.get(counter)
                floor = GATHER_PAGE_ROWS_MIN_COUNTER if feat == "gather_page_rows" else MIN_COUNTER
                if got is None or got < floor:
                    continue
                span = plan.get(SPAN_COUNTER)
                rows.append(
                    {
                        "feature": feat,
                        "plan": plan["plan"],
                        "acquire": acq["count_source"],
                        "unique": sample.kw["unique"],
                        "orderby": sample.kw["orderby"],
                        # Sampled, so this slice is the one that separates the short-circuiting
                        # card-mode kernels from the ones that must score every printing.
                        "prefer": sample.kw.get("prefer", "default"),
                        # Compose's arm reads `scan_units` ONLY in its Gather branch; Perm/OrderbyWalk
                        # stop at page_offset+limit and never scan the candidates. A ratio measured on
                        # those is comparing the feature to work the arm never charges for.
                        "paging": paging,
                        "ratio": acq[feat] / got,
                        # What the same row would have read graded against the printing SPAN -- the
                        # old comparison. `nan` where the plan publishes no span, which `percentile`
                        # tolerates and `MIN_ROWS` cells then drop.
                        "span_ratio": (acq[feat] / span) if span else float("nan"),
                    }
                )
    return rows


def verdict(sorted_vals: list[float]) -> str:
    """Flag a cell whose median feature/counter ratio sits outside the agreement band."""
    med = costbench.percentile(sorted_vals, 50)
    if AGREE_LO <= med <= AGREE_HI:
        return ""
    return "  OVER-COUNTS" if med > AGREE_HI else "  UNDER-COUNTS"


def table(  # noqa: PLR0913 - a table renderer's arguments ARE its output format, as `percentile_table` says
    rows: list[dict],
    key: Callable[[dict], object],
    label: str,
    *,
    limit: int = 30,
    value: str = "ratio",
    rank: tuple[str, Callable[[list[float]], float]] = costbench.BY_MISCALIBRATION,
    annotate: Callable[[list[float]], str] | None = verdict,
) -> None:
    """Feature/counter percentiles for one grouping, worst-calibrated cells first by default.

    `rank` is overridable because the default ordering is median-based (`|log(p50)|`) and so is
    `verdict`: a cell whose median is 1.0 and whose p90/p10 is 38x sorts last and prints unflagged.
    That is the correct default for a bias, and the wrong one for the walk terms, whose error IS the
    spread -- so those pass `BY_COUNT` and read the percentile columns instead.
    """
    costbench.percentile_table(
        rows,
        key,
        label,
        value=value,
        rank=rank,
        limit=limit,
        min_rows=MIN_ROWS,
        annotate=annotate,
    )


def main() -> None:
    """Sample, then show which features disagree with the counters that realize them."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=None, help=f"time-boxed run (the default, {DEFAULT_SECONDS:g}s)")
    # A time-boxed run is the WRONG bound for a paired before/after read of these cells, and not
    # merely a noisy one. Both runs walk the same seeded query stream, so the slower build simply
    # stops earlier and its population is a shorter PREFIX of the other's -- 88,663 against 86,723
    # queries on one 300s pair here. The cells this tool exists to grade are small (the compose
    # `card` cell is a few hundred rows of a hundred thousand), so a couple of percent of truncation
    # moves a cell's composition by more than the feature change being measured, and the delta then
    # reads as a result. `Budget(sample=N)` fixes the query COUNT instead, which makes the two runs
    # grade the identical row set -- confirmed by comparing row counts, which must match exactly.
    parser.add_argument("--n-queries", type=int, default=None, help="fixed query count instead of a time box; use for a PAIRED A/B")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    args = parser.parse_args()

    # Neither bound given means the historical default; both given is rejected by `Budget` itself,
    # which already refuses "a budget with no bound, or two that could disagree" -- re-checking it
    # here would be a second copy of that rule, free to drift from it.
    seconds = DEFAULT_SECONDS if args.seconds is None and args.n_queries is None else args.seconds
    budget = costbench.Budget(seconds=seconds, sample=args.n_queries, warmups=NUM_WARMUPS, trials=NUM_TRIALS)

    engine = load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".featacc.store"))
    sampler = QuerySampler(args.corpus, args.mode)
    rows = collect(engine, sampler, random.Random(args.seed), budget)
    bound = f"{budget.sample:,} queries" if budget.sample is not None else f"{budget.seconds:g}s"
    print(f"\n{len(rows):,} feature-rows, mode={args.mode}, bound={bound}.  ratio = FEATURE / COUNTER, so >1 is OVER-counted.")
    table(rows, lambda r: r["feature"], "feature (pooled -- hides per-cell errors that cancel)")
    table(rows, lambda r: f"{r['feature']} [{r['acquire']}]", "feature [acquire]")
    # The slice that catches mode-dependent features. A count taken in printing space is exact in
    # printing mode and ~2x off in artwork mode; pooled across modes it reads as mild spread.
    table(rows, lambda r: f"{r['feature']} [{r['acquire']}] / {r['unique']}", "feature [acquire] / distinct-on")
    # One shared feature vector costs every plan, but the plans do different work: `scan_units` feeds
    # StreamedSelect, GatheredScan and compose's Gather branch alike. If they disagree here, no single
    # value of the feature is right for all of them and the fix is per-arm, not per-mode.
    table(rows, lambda r: f"{r['feature']} <{r['plan']}> / {r['unique']}", "feature <plan> / distinct-on")
    # `prefer` decides whether the card-mode kernels early-break, and `PlanFeatures` does not carry
    # it, so one feature value has to serve both regimes. If these two rows differ, the feature is not
    # merely miscalibrated -- it is blind to a variable that changes the work.
    # Both scan features, since `stream_scan_units` is the same quantity for a different arm and the
    # early-break question applies to it identically. Labelled by feature so the two stay separable.
    scan = [r for r in rows if r["feature"] in ("scan_units", "stream_scan_units")]
    table(scan, lambda r: f"{r['feature']} / {r['unique']} / prefer={r['prefer']}", "scan features by distinct-on and PREFER")
    # Orderby was always sampled and never sliced, so its effect has never been visible. It selects
    # the plan set (StreamedSelect needs a sort permutation; PlanePopcountOrder needs its column) and
    # therefore which arm reads the shared vector at all.
    table(rows, lambda r: f"{r['feature']} / orderby={r['orderby']}", "feature by ORDERBY", limit=40)
    # The residual-gated per-card term. `feature <plan> / distinct-on` above already separates the two
    # arms; what no table there separates is `prefer`, and this term is exactly where that matters:
    # `card_pass` is per CARD, so its count should not move with `prefer` at all, and a row here that
    # does move is the feature failing to see a real difference in how many passes run.
    residual = [r for r in rows if r["feature"] in RESIDUAL_FEATURES]
    table(
        residual,
        lambda r: f"<{r['plan']}> / {r['unique']} / prefer={r['prefer']}",
        "RESIDUAL per-card term (CARD_PASS+FLOOR) by plan, distinct-on and PREFER",
        limit=40,
    )
    # Compose's two LINEAR_PASS build terms, same slice. The build runs before paging is chosen and
    # `prefer` reaches none of it, so these rows should be flat across the four values; they are here
    # because a term at 37% and 23% of a plan's predicted time is worth showing to be insensitive
    # rather than assumed to be.
    build = [r for r in rows if r["feature"] in COMPOSE_BUILD_CHARGES]
    table(
        build,
        lambda r: f"{r['feature']} / {r['unique']} / prefer={r['prefer']}",
        "COMPOSE BUILD terms by distinct-on and PREFER",
        limit=40,
    )
    # Compose only pays `scan_units` when it pages by Gather, so judge the feature on those rows.
    compose = [r for r in rows if r["plan"] == "PrintingCompose"]
    table(compose, lambda r: f"{r['feature']} <compose {r['paging']}> / {r['unique']}", "compose only: feature by PAGING branch")
    # The walk terms get their own table ranked BY COUNT, not by miscalibration, because their error
    # is not a median. `BY_MISCALIBRATION` sorts on |log(p50)| and `verdict` flags on p50 alone, so a
    # cell sitting at 1.02 with a 38x p90/p10 ranks last and reads "calibrated" -- which is exactly
    # what Round 69 found `stream_perm_steps` to be, and exactly the error a coefficient refit cannot
    # represent. Read the p10..p90 columns here, not the flag: 1.9x on `orderby=name` (a sort order
    # uncorrelated with the filter, so uniform density holds) against 38.8x on `orderby=cmc` (where it
    # does not). `printings_walked` is included because it is the same failure in printing space, where
    # the medians DO move (0.925 to 3.579 against one shipped WALK_LENGTH_BIAS of 1.45).
    walks = [r for r in rows if r["feature"] in ("stream_perm_steps", "printings_walked")]
    table(
        walks,
        lambda r: f"{r['feature']} / orderby={r['orderby']} / {r['unique']}",
        "WALK TERMS by sort column -- read the SPREAD, not the median",
        limit=40,
        rank=costbench.BY_COUNT,
    )
    # The old grading, same rows: `scan_units` against the printing SPAN rather than the printings
    # actually examined. Printed last as the control -- if these cells sit at 1.0 where the real
    # column does not, the span is what the constants were fit against.
    spanned = [r for r in scan if math.isfinite(r["span_ratio"])]
    table(
        spanned,
        lambda r: f"scan_units / {r['unique']}",
        "CONTROL: scan_units vs printing SPAN (the old grading)",
        value="span_ratio",
    )
    print(f"\n  Cells outside [{AGREE_LO}, {AGREE_HI}] are flagged. A feature error cannot be fixed by any")
    print("  rate: fit_cost_model.py will bury it in whichever coefficient correlates with it.")


if __name__ == "__main__":
    main()
