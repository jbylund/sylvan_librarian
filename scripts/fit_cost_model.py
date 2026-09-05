"""Refit the cost model's rate constants against measured plan time, on the model's own features.

`bench_cost_model_agreement.py` says *which* (plan, acquire) cells disagree. This says *what the
constants should be* — it regresses measured per-plan time on exactly the feature vector
`cost::plan_cost` consumes, so a fitted coefficient drops straight into `cost.rs`.

Two things make this different from eyeballing a ratio:

- **The regression is relative, not absolute.** Plain least squares is dominated by the handful of
  slowest queries, which is how a model can fit the big cases and be 3x off across the common ones.
  Every row is scaled by its own measured time, so the objective is squared *relative* error — the
  same thing the [0.8, 1.25] median bar measures.
- **Coefficients are constrained non-negative.** These are per-unit hardware costs; a negative rate
  fits noise and then extrapolates catastrophically outside the sampled range.

Fitting only works once the FEATURES are right. A feature that mis-counts by 2.5x cannot be repaired
by any rate, and the fit will happily bury the error in whichever coefficient correlates with it —
so `--counters` first checks each realized counter against the feature that is supposed to predict
it, and refuses to report rates for a plan whose features do not track reality.

Fitting also only works once the fit TARGET is right, which is a separate question from the features:
the two materializing arms were fitted against dispatch latency, which on a range acquire includes a
candidate build their arms have no term for. See `fit_target_ns`.

    .venv/bin/python scripts/fit_cost_model.py --n-queries 20000
"""

from __future__ import annotations

import argparse
import collections
import math
import pathlib
import random
import statistics
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from client.query_sampler import MODES, QuerySampler  # noqa: E402
from scripts import costbench  # noqa: E402
from scripts.bench_cost_model_agreement import AGREE_HI, AGREE_LO  # noqa: E402
from scripts.costbench import Budget, iter_samples, load_engine  # noqa: E402

NUM_WARMUPS = 2
NUM_TRIALS = 7
# Queries to sample when `--n-queries` is used instead of `--seconds`. A COUNT is the reproducible
# bound: two runs of the same seed then draw the identical query stream, which is what makes a
# `current` column recorded against one build comparable to the same column against another. A
# seconds budget draws a different number of queries on a machine that is a little busier.
DEFAULT_N_QUERIES = 20_000
# Coordinate descent on the non-negative normal equations. The problem is convex and tiny (<=6
# coefficients), so this converges in far fewer sweeps than the cap.
MAX_FIT_SWEEPS = 500
FIT_TOLERANCE = 1e-12
# Below this a cell's fit is noise, not a rate.
MIN_ROWS_FOR_FIT = 200
# `run_query_streamed`'s small-total gather branch scans all n_cards; mirrors CARD_ENGINE_STREAM_MIN_MATCHES.
STREAM_MIN_MATCHES = 1024
# Mirrors cost.rs MATCH_RATE_FLOOR, the density floor under the page-fill walk length.
MATCH_RATE_FLOOR = 1.0 / 1_000_000.0
# Mirrors cost.rs WALK_LENGTH_BIAS: matches clump along the sort order, so a walk runs ~1.45x longer
# than uniform spacing predicts. Measured 0.69 against `printings_examined` before this existed.
WALK_LENGTH_BIAS = 1.45
# A realized counter this far from the feature meant to predict it is a FEATURE bug; refitting rates
# on top of it just relocates the error.
COUNTER_TOL = 0.15
# The mirror check's tolerance. This is a reimplementation of cost.rs in Python, so it can drift --
# and did: the arms moved to `max(tier, floor)` and gained a residual-gated per-row term while
# `design_row` still modelled the tier as a multiplier, which silently invalidated every coefficient
# reported for two revisions. The check below compares the mirror against the engine's own
# predicted_ns and refuses to report if they disagree.
MIRROR_TOLERANCE = 0.001
MIRROR_MIN_AGREEMENT = 0.99
# Gauss-Newton on the log objective; converges in a handful of steps from the current constants.
MAX_IRLS_ITERS = 40
# --by-mode: how far a per-unit rate may move between distinct-on modes before the arm is judged to be
# missing a mode-dependent term rather than to be sampling noise. Per-unit hardware costs should not
# depend on distinct-on at all -- that is the assumption the single shared arm rests on, so the bar is
# set near the noise floor rather than at a "surely that is broken" level. At 2.0 only one term in the
# engine flagged; the interesting cases sit between 1.3 and 2.0.
MODE_SPLIT_FACTOR = 1.3
# Below this (ns per unit) a rate is unidentified rather than mode-dependent, and its ratio is noise.
MODE_SPLIT_MIN_RATE = 0.05
IRLS_TOLERANCE = 1e-6
IRLS_MIN_PREDICTION_NS = 1.0
# Ridge pull toward the current constants, per row of design. Small enough that a well-identified
# term moves freely, large enough to pin the collinear ones (floor/page_span vs the intercept).
RIDGE_STRENGTH = 0.01

# The constants currently in cost.rs, keyed by the term names `design_row` emits. The IRLS start
# point, and the baseline each fitted rate is reported against.
#
# Keyed by NAME rather than positional, because the positional form is what allowed the two mirror
# bugs this dict has carried: a term the engine charges and the design vector omitted outright
# (compose's COLLECTION_BROADCAST_PER_PRINTING), and one fixed cost standing in for an arm that
# charges two. Both were invisible as long as a reader had to count a 12-element literal against a
# 12-element prose comment -- which had itself drifted, since StreamedSelect's list omitted
# `perm_steps`. `coeffs_for` now raises on any key-set disagreement, naming the term.
CURRENT: dict[str, dict[str, float]] = {
    "GatheredScan": {
        # The UNCONDITIONAL loop rate; the card_pass CALL (3.00) rides in CARD_PASS+FLOOR on top of
        # the floor (18.89), because the arm gates the call on `tier_ns > 0` -- all_match_known skips it.
        "LOOP_PER_CARD": 3.88,
        "SCAN_PER_ROW": 2.06,
        "CARD_PASS+FLOOR": 21.89,
        "PUSH_PER_MATCH": 2.24,
        # The page phase has two drivers: the quickselect scales with offset+limit, the collect with
        # the page actually returned. A designed page sweep separates them where traffic cannot, since
        # the two are correlated in the sampled query mix.
        "SELECT_PER_PAGE_SLOT": 3.51,
        "COLLECT_PER_PAGE_ROW": 9.79,
        # `exec_gathered_scan`'s Mode::Artwork branch pays a per-printing group_best/touched dedupe
        # check that StreamedSelect's ARTWORK_SEEN_PER_CARD does not describe (that one is per-CARD).
        "ARTWORK_PER_PRINTING": 0.50,
        # The arm's TWO fixed costs, gated apart on `matches == 0` (GATHER_FIXED_COST_NS and
        # GATHER_FIXED_COST_ZERO_MATCH_NS). A single FIXED column always charged 169.6 and disagreed
        # with the engine on 20.0% of rows by a constant -127.6 ns.
        "FIXED": 169.6,
        "FIXED_ZERO_MATCH": 42.0,
    },
    # Refit once `printings_examined` existed: this plan's fit was vetoed for as long as the only
    # available counter was the printing SPAN, which its all_match rows disagree with by ~3x over a
    # term the arm multiplies by zero. Median agreement 0.63 -> 0.92, within-25% 19% -> 58%.
    "StreamedSelect": {
        # Same split as GatheredScan: 2.58 unconditional, the call (2.47) folded into the
        # residual-gated column alongside the 6.58 floor.
        "LOOP_PER_CARD": 2.58,
        "SCAN_PER_ROW": 5.97,
        "CARD_PASS+FLOOR": 9.05,
        "EMIT_PER_MATCH": 0.12,
        # The permutation walk's length -- the one quantity in P3's finish phase no other feature is
        # proportional to: the walk steps until the page fills, so it visits
        # ~page_span * perm_walk_span / matches entries, inversely proportional to selectivity.
        "PERM_STEP": 1.0,
        "ARTWORK_SEEN_PER_CARD": 1.21,
        # Round 81 split the small-total redo exit into the `counts[cid] == 0` sweep every card pays
        # and the `push_card_matches` walk the matching handful pays. The old single per-card rate of
        # 1.02 was the second folded into the first, so it charged a few dozen cards' printing walk
        # over all 31.7k cards -- 32.4 us flat against a measured p50 `ns_finish` of 11.0 us.
        "SMALL_TOTAL_FLOOR_PER_CARD": 0.30,
        "REDO_SCAN_PER_ROW": 5.97,
        "CORPUS_PASS_PER_CARD": 0.02,
        "FIXED": 217.0,
    },
    # Several of these are SHARED with other arms in cost.rs (LINEAR_PASS, RANGE_SCATTER,
    # GATHER_CARD_PASS, GATHER_PUSH_PER_MATCH, ...), so a fitted value that disagrees with the other
    # arm's is information about the shared constant, not a number to paste blindly.
    "PrintingCompose": {
        "BROADCAST_PER_PRINTING": 1.93,
        "SCATTER_PER_PRINTING": 0.48,
        "COLLECTION_BROADCAST_PER_PRINTING": 1.34,
        "PROJECT_PER_PRINTING": 1.93,
        "POPCOUNT_PER_WORD": 1.07,
        "WALK_STEP": 0.58,
        "WALK_EMIT_PER_ROW": 2.19,
        "GATHER_CARD_PASS": 13.22,
        "GATHER_BITTEST_PER_PRINTING": 0.38,
        # Added when the artwork tail was traced to the grouping arm's work being charged at the
        # bit-test rate; 1.5 is a physical guess (a struct read plus prefer_score), meant to be fitted.
        "GATHER_GROUP_PER_PRINTING": 1.5,
        "GATHER_PUSH_PER_MATCH": 3.39,
        # The full-width bitmap build, Gather-arm only: measured directly over a 10x corpus axis
        # rather than fitted here, so a pooled fit disagreeing with 0.0835 is a signal to re-examine.
        "BUILD_PER_PRINTING": 0.0835,
        "FIXED": 163.56,
    },
}


#: Plans whose `cost.rs` arm describes the EXECUTOR PHASES ALONE (`ns_setup + ns_loop + ns_finish`).
#: The `prepare_candidates` build that DISPATCH pays on a `RANGE_ACQUIRES` route is modelled by
#: `cost::materialize_cost` and charged by NOTHING -- `plan_cost` omits it deliberately (charging it
#: was measured and is a net loss; see `fit_target_ns`), so no rate below may absorb it either.
#:
#: Every other fitted arm covers its own dispatch build inside its own terms and must keep the
#: `plan_self_ns` denominator -- `CardRangePopcount` most explicitly, whose
#: `scatter_printings * CARD_RANGE_BUILD_PER_PRINTING_NS` IS its dispatch build, and which reads 1.13
#: against `plan_self_ns` against 3.48 against the executor. Getting that one backwards would look
#: like a 3x mis-calibration and is only a denominator.
EXECUTOR_ONLY_ARMS = frozenset({"StreamedSelect", "GatheredScan"})


def fit_target_ns(plan_row: dict, acq: dict, *, legacy: bool = False) -> float | None:
    """The measured quantity this plan's ARM claims to predict, in ns. Fit and grade against THIS.

    The whole point of the distinction, and the defect it fixes:

    `costbench.plan_self_ns` is dispatch latency -- the executor phases, PLUS `ns_prepare` on a
    `RANGE_ACQUIRES` route, where the router only estimated and dispatch pays the candidate build
    itself. That is the right number for "how long did this plan take", and the wrong number to fit an
    arm against **when the arm has no term for the build**. `cost::plan_cost`'s two materializing arms
    have none, so fitting them against `plan_self_ns` silently teaches their per-unit rates to absorb
    a build cost, in whatever proportion that route happens to carry.

    Measured on picked rows, uniform, 6,000 queries, both denominators side by side
    (`scripts/bench_picked_ratios_by_route.py`):

        plan / acquire                        p / plan_self_ns   p / executor   prep share
        GatheredScan / printing_compose                  0.642          1.127        12.6%
        StreamedSelect / printing_compose                0.973          1.358        27.6%
        GatheredScan / candidates                        1.232          1.232         0.0%
        StreamedSelect / candidates                      1.204          1.204         0.0%

    The two arms are calibrated against DIFFERENT denominators, and how much each has absorbed is
    measurable with `legacy` below: fit the arm both ways on ONE population and read how far each
    coefficient falls when the build comes out of the target. Uniform, 20,000 queries, prefer pinned:

        GatheredScan     FIXED 402.28 -> 73.20 (-82%)   COLLECT_PER_PAGE_ROW 12.38 -> 4.38 (-65%)
        StreamedSelect   FIXED 211.30 -> 187.06 (-12%)  CARD_PASS+FLOOR      13.11 -> 12.88 (-2%)

    **GatheredScan is the absorber, not StreamedSelect.** Independently, dividing the same gap by the
    realized `ns_prepare` reads 0.969 for GatheredScan against 0.579 for StreamedSelect -- i.e. its
    rates carry essentially the whole build and StreamedSelect's a little over half.

    The picked-row table above suggests the OPPOSITE ordering, and that is the trap: it is a different
    population. Over every row a plan RAN on, `GatheredScan / printing_compose` reads 0.989 against
    its own executor rather than 1.127, and the two distinct-on modes disagree in sign. Over-charge
    here is route-, mode- and population-dependent, not a uniform rate error waiting to be divided
    out. **Do not diagnose from picked rows and then refit on the full population.**

    **This target is a calibration fix, not a licence to add a build term.** Charging
    `cost::materialize_cost` in `plan_cost` on top of a refit arm was measured end to end and is
    WORSE: +1.03% to +1.15% (uniform) and +0.66% to +0.78% (realistic) against the refit alone, and
    still +0.19% to +0.49% when the modelled build is replaced by the ORACLE realized `ns_prepare`.
    A perfect build model loses too, so this is not a "wait for a better `materialize_cost`" case. The
    harm is entirely on the `printing_compose` route (+1.43% uniform): the charge pushes rows off the
    materializing plans onto `PrintingCompose`, and those moves lose. On a `candidates` acquire the
    charge is provably inert -- `PlanScope::Candidates` admits only the two materializing plans and
    `materialize_cost` is identical for both, so it cancels exactly in the argmin.

    `legacy=True` restores the old `plan_self_ns` target for every plan, so a constant that moves can
    be attributed to the DENOMINATOR rather than to any sampling change made alongside it.

    Returns:
        The arm's target in ns, or None when the plan produced no page or published no phase.
    """
    if legacy or plan_row["plan"] not in EXECUTOR_ONLY_ARMS:
        return costbench.plan_self_ns(plan_row, acq)
    if not plan_row["trials_ns"]:
        return None
    executor_ns = float(plan_row["ns_setup"] + plan_row["ns_loop"] + plan_row["ns_finish"])
    # Contiguous by construction, so the sum IS the executor; a zero means the plan ran and published
    # no phase, which cannot be priced and must not read as zero. Same guard `plan_self_ns` uses.
    return executor_ns if executor_ns > 0 else None


def fit_log_ratio(design: list[list[float]], targets: list[float], start: list[float], weights: list[float]) -> list[float]:
    """Fit c >= 0 minimising squared LOG ratio, sum (log(Xc) - log(y))^2, by Gauss-Newton IRLS.

    Fitting `sum (Xc/y - 1)^2` instead looks like the same thing and is not: over-prediction is
    unbounded there while under-prediction saturates at 1, so the minimiser buys cheap error
    reduction by driving every per-unit rate to zero and leaving only the fixed term. Measured — it
    produced all-zero rates and *worse* agreement than the constants it was replacing.

    Log space is symmetric in over/under, which is what a "median ratio near 1.0" bar actually asks
    for. It is not linear in the coefficients, so each iteration reweights by the current prediction
    (the Gauss-Newton step for the log objective) and re-solves.
    """
    coeffs = list(start)
    for _ in range(MAX_IRLS_ITERS):
        scaled_rows, scaled_targets = [], []
        for row, y, w in zip(design, targets, weights, strict=True):
            pred = max(sum(c * v for c, v in zip(coeffs, row, strict=True)), IRLS_MIN_PREDICTION_NS)
            # sqrt(count): squared residuals then sum as if the shape appeared `count` times, which
            # is what a frequency-weighted median-ratio bar actually measures. Deduplicating to one
            # row per shape instead fits a DIFFERENT distribution -- it gives a rare expensive shape
            # the same say as a common cheap one, and measured 0.99 on shapes while the sampled
            # distribution sat at 0.62-0.85.
            scale = math.sqrt(w) / pred
            scaled_rows.append([v * scale for v in row])
            scaled_targets.append(y * scale)
        # Ridge toward the current constants, in RELATIVE units so one strength suits every term.
        # Several columns barely vary across this corpus — the StreamedSelect floor is literally
        # `n_cards` or 0, and page_span is usually just `limit` — leaving them collinear with the
        # intercept. Unregularised, the fit trades freely between them and lands on absurdities like
        # a 42 µs fixed cost. The prior pins those directions and lets the identified ones move.
        penalty = math.sqrt(RIDGE_STRENGTH * sum(weights))
        for j, prior in enumerate(start):
            if prior <= 0:
                continue
            row = [0.0] * len(start)
            row[j] = penalty / prior
            scaled_rows.append(row)
            scaled_targets.append(penalty)
        updated = nnls(scaled_rows, scaled_targets)
        shift = max(abs(a - b) / max(b, IRLS_MIN_PREDICTION_NS) for a, b in zip(updated, coeffs, strict=True))
        coeffs = updated
        if shift < IRLS_TOLERANCE:
            break
    return coeffs


def nnls(rows: list[list[float]], targets: list[float]) -> list[float]:
    """Minimise ||Xc - y|| subject to c >= 0, by coordinate descent on the normal equations."""
    n = len(rows[0])
    gram = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    xty = [sum(r[i] * t for r, t in zip(rows, targets, strict=True)) for i in range(n)]
    coeffs = [0.0] * n
    for _ in range(MAX_FIT_SWEEPS):
        delta = 0.0
        for i in range(n):
            if gram[i][i] <= 0:
                continue
            # Exact coordinate-wise minimum, clamped at the non-negativity boundary.
            residual = xty[i] - sum(gram[i][j] * coeffs[j] for j in range(n) if j != i)
            new = max(0.0, residual / gram[i][i])
            delta = max(delta, abs(new - coeffs[i]))
            coeffs[i] = new
        if delta < FIT_TOLERANCE:
            break
    return coeffs


# The residual floors the shipped arms use inside `max(tier_ns, floor)`.
#
# These MUST equal the `*_RESIDUAL_FLOOR_NS` constants in cost.rs, and equal the third entry of the
# matching `CURRENT` vector. All three are the same number: `design_row` makes the floor the
# coefficient of the `eval_domain * residual_on` column AND uses it to compute the `excess` offset for
# rows where the tier beats it. Change one without the others and `mirror_matches_engine` drops below
# its 99% bar -- which is exactly how the 2026-08-02 refit was caught pasting a fitted floor of 6.58
# into cost.rs while the offset here still assumed 8.18 (7.4% of rows disagreed).
#
# A consequence worth stating: the fitted floor is not directly pasteable, because the offset it was
# fitted against assumed the OLD floor. Applying it and re-fitting is a fixed-point iteration, and
# each run is only self-consistent with whatever is shipped at the time.
# The residual-gated column now prices the `card_pass` call as well as the floor, but the OFFSET
# below is still about the floor alone: it captures `eval_domain * max(tier_ns - FLOOR, 0)`, the
# excess where an expensive residual beats the floor, and the call is not part of that maximum.
SHIPPED_RESIDUAL_FLOOR = {"GatheredScan": 18.89, "StreamedSelect": 6.58}


def residual_excess(cards: float, tier_ns: float, floor: float) -> float:
    """The part of `cards * max(tier_ns, FLOOR)` that no fitted coefficient scales.

    One definition, because the two arms no longer multiply the same card count: `StreamedSelect`
    charges its residual over `stream_residual_card_pass` (two passes) where `GatheredScan` charges
    `eval_domain` (one), and the offset has to ride the SAME quantity as the column or the mirror
    check drops below its bar for exactly the rows where the redo pass fires.
    """
    return cards * max(tier_ns - floor, 0.0) if tier_ns > 0.0 else 0.0


def design_row(plan: str, acq: dict, limit: int, offset: int) -> tuple[dict[str, float], float] | None:
    """The feature vector for one plan's cost arm, plus the part no coefficient scales.

    Keyed by TERM NAME, matching `CURRENT`, rather than a positional list paired with a separate
    names list. The positional form is what let two mirror bugs live here: a term the arm charges and
    this vector omitted entirely (compose's `collection_broadcast_printings`), and a term whose
    meaning had silently shifted. Neither is expressible now -- a missing term is a `KeyError` naming
    it, and nothing can be inserted at the wrong index because there are no indices. It also made
    every edit a hand-count of a 12-element literal against a 12-element comment, which had already
    drifted (`StreamedSelect`'s omitted `perm_steps`).

    Mirrors `cost.rs` exactly. The awkward term is the residual charge, which the arms express as
    `eval_domain * max(tier_ns, FLOOR)` -- not linear in the floor, so it cannot be one column. It is
    split: a column of `eval_domain` gated on residual presence, whose coefficient IS the floor, plus
    an OFFSET of `eval_domain * max(tier_ns - FLOOR, 0)` for the excess where an expensive residual
    beats the floor. The offset must be subtracted from the target before fitting, or the fit solves
    `Xc ~= y` while the model computes `Xc + offset` -- a different problem, which shows up as
    impossible (negative) improvements.
    """
    eval_domain = float(acq["eval_domain"])
    scan_units = float(acq["scan_units"])
    # P3's own scan estimate, which differs from `scan_units` on a legality-composed acquire -- see
    # `PlanFeatures::stream_scan_units`. Absent from an older recorded run, in which case it equals
    # `scan_units` and this mirrors the pre-split arm exactly.
    stream_scan_units = float(acq.get("stream_scan_units", acq["scan_units"]))
    matches = float(acq["matches"])
    n_cards = float(acq["n_cards"])
    tier_ns = acq["residual_tier_ns100"] / 100.0
    # `cost::gather_page_span` / `cost::gather_page_rows` themselves, exposed by `explain` for exactly
    # this reason -- these were two more Python copies of an arm's formula, the shape that has already
    # drifted twice in this file (`stream_perm_steps`' `n_cards`, `printings_walked`'s bias). The
    # fallbacks mirror the current arm and are only for runs recorded before the fields existed.
    page_span = float(acq.get("gather_page_span", min(offset + limit, acq["matches"])))
    # Mirrors cost.rs: `select_page` returns clamp(matches - offset, 0, limit), so a page past the end of
    # the matches collects fewer rows than requested.
    page_rows = float(acq.get("gather_page_rows", min(max(acq["matches"] - offset, 0), limit)))
    residual_on = 1.0 if tier_ns > 0.0 else 0.0
    floor = SHIPPED_RESIDUAL_FLOOR.get(plan, 0.0)
    excess = residual_excess(eval_domain, tier_ns, floor)

    if plan == "GatheredScan":
        # NOT `artwork_seen_cards`-if-nonzero: `exec_gathered_scan`'s per-printing dedupe loop runs
        # unconditionally (no `all_match_known` shortcut like StreamedSelect's stored-group-count
        # fast path), so `artwork_seen_cards` can read 0 (zeroed by all_match_known) on the same row
        # where `artwork_seen_printings` is correctly nonzero -- read the real field directly.
        artwork_seen_printings = float(acq.get("artwork_seen_printings", 0))
        # The arm charges ONE of two fixed costs -- `GATHER_FIXED_COST_ZERO_MATCH_NS` when
        # `matches == 0`, `GATHER_FIXED_COST_NS` otherwise -- so a single FIXED column cannot express
        # it and the mirror always charged the larger. That was 20.0% of GatheredScan rows disagreeing
        # with the engine at a constant residue of exactly -127.6 ns (169.6 - 42.0), and the single
        # largest cause of the mirror-drift refusal. Two mutually exclusive indicator columns instead:
        # every row contributes 1.0 to exactly one of them, so each coefficient fits its own branch.
        zero_match = 1.0 if acq["matches"] == 0 else 0.0
        return (
            {
                "LOOP_PER_CARD": eval_domain,
                "SCAN_PER_ROW": scan_units,
                "CARD_PASS+FLOOR": eval_domain * residual_on,
                "PUSH_PER_MATCH": matches,
                "SELECT_PER_PAGE_SLOT": page_span,
                "COLLECT_PER_PAGE_ROW": page_rows,
                "ARTWORK_PER_PRINTING": artwork_seen_printings,
                "FIXED": 1.0 - zero_match,
                "FIXED_ZERO_MATCH": zero_match,
            },
            excess,
        )
    if plan == "StreamedSelect":
        # Mirrors the arm's guards: an empty result or a page past the end returns before BOTH branches,
        # so neither the gather floor nor the walk is charged there.
        # `cost::stream_perm_steps` itself, exposed for exactly this reason, so the column is not a
        # second copy of the arm's walk formula. It WAS one, and it was WRONG: it read `n_cards` where
        # the arm reads `perm_walk_span` -- the Round 32 generalization from "the whole permutation" to
        # "the segment the filter's sort-column bound admits", which this mirror never picked up. So
        # the PERM_STEP coefficient was being fitted against the pre-Round-32 formula on every query
        # whose filter bounds the sort column -- measured at **4.1%** of walking rows (37 of 902), on
        # which the old column OVER-stated the walk by a median **2.44x** (p90 6.08, max 11.25). Small
        # population, large error, and precisely the queries the generalization exists for. This is the
        # failure `printings_walked`'s own doc in cost.rs describes -- two definitions of a walk formula
        # drifting apart -- caught a second time.
        # The fallback is for runs recorded before the field existed, and mirrors the CURRENT arm.
        perm_walk_span = float(acq.get("perm_walk_span", n_cards))
        walks_perm = matches > STREAM_MIN_MATCHES and offset < matches
        perm_steps = float(
            acq["stream_perm_steps"]
            if "stream_perm_steps" in acq
            else (min(page_span * perm_walk_span / matches, perm_walk_span) if walks_perm else 0.0)
        )
        # Mirrors run_query_streamed's early return: zero matches, or a page past the total, never
        # reaches the small-total gather. See STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS in cost.rs.
        runs_small_gather = 0 < matches <= STREAM_MIN_MATCHES and offset < matches
        small_total = n_cards if runs_small_gather else 0.0
        # P3's per-row term is GATED on residual presence: it only counts matches, and
        # `card_match_count` is O(1) offset arithmetic under all_match, so it walks printings only
        # when a residual must be tested. `n_cards` carries the O(corpus) work it pays regardless of
        # selectivity -- the counts buffer resized and cleared every query.
        #
        # The residual column is NOT `eval_domain`: this plan runs two passes, and its small-total
        # exit re-derives `card_pass` for every matching card on top of the counting pass. `cost.rs`
        # exposes the arm's own quantity as `stream_residual_card_pass` (already zero under
        # `all_match_known`, so no `residual_on` gate here), for the same reason `stream_perm_steps`
        # is exposed -- a second copy of a gate in this file is how the PERM_STEP column silently
        # fitted a pre-Round-32 formula. The fallback is for runs recorded before the key existed and
        # mirrors the pre-split arm exactly.
        residual_cards = float(acq.get("stream_residual_card_pass", eval_domain * residual_on))
        return (
            {
                "LOOP_PER_CARD": eval_domain,
                "SCAN_PER_ROW": stream_scan_units * residual_on,
                "CARD_PASS+FLOOR": residual_cards,
                "EMIT_PER_MATCH": matches,
                "PERM_STEP": perm_steps,
                "ARTWORK_SEEN_PER_CARD": float(acq["artwork_seen_cards"]),
                "SMALL_TOTAL_FLOOR_PER_CARD": small_total,
                # `cost::stream_redo_printings` itself, for the reason `stream_perm_steps` is read
                # rather than recomputed: a Python copy of a gate is how the PERM_STEP column silently
                # fitted a pre-Round-32 formula. It is a u32 on both sides, so there is no truncation
                # gap here of the kind `printings_walked` has. The fallback mirrors the CURRENT arm
                # and is only for runs recorded before the key existed.
                "REDO_SCAN_PER_ROW": float(
                    acq.get(
                        "stream_redo_printings",
                        round(min(matches, eval_domain) * acq["n_printings"] / n_cards) if runs_small_gather else 0.0,
                    )
                ),
                "CORPUS_PASS_PER_CARD": n_cards,
                "FIXED": 1.0,
            },
            # The offset must ride the same card count as the column above; see `residual_excess`.
            residual_excess(residual_cards, tier_ns, floor),
        )
    if plan == "PrintingCompose":
        # The arm no tool has ever fitted, while the regret matrix puts 75% of all lost time on it.
        # `build` is common to every paging branch; the page term is whichever branch will run, so a
        # row contributes to exactly one of the two page columns and zero to the other. Decline costs
        # infinity and never reaches a measurement, so those rows are absent by construction.
        paging = acq.get("compose_paging", "Gather")
        if paging == "Decline":
            return None
        gather = paging == "Gather"
        # Recomputed rather than read from the exposed u32, which is truncated for display. The
        # mirror has to match cost.rs bit for bit or its self-check fails on small walks.
        match_rate = max(matches / max(float(acq["n_printings"]), 1.0), MATCH_RATE_FLOOR)
        # Mirrors `cost::printings_walked`: the closed form times WALK_LENGTH_BIAS. The
        # `orderby_walk_scan` floor this used to take a max against is gone -- both walks now step a
        # value index entry at a time, so there is no bucket granularity to express.
        walk = page_span / match_rate * WALK_LENGTH_BIAS if not gather else 0.0
        return (
            {
                "BROADCAST_PER_PRINTING": float(acq["broadcast_printings"]),
                "SCATTER_PER_PRINTING": float(acq["scatter_printings"]),
                # The card-space collection leaf's build (`ids_of` + `broadcast_card_ids_to_printings`),
                # charged by the engine in `build` on EVERY paging branch. It was missing from this
                # vector entirely, which accounted for 53 of 53 compose mirror disagreements -- exactly
                # `collection_broadcast_printings * 1.34` on every one of them. Pricier per printing
                # than a range's contiguous scatter because it walks a card cursor per id.
                "COLLECTION_BROADCAST_PER_PRINTING": float(acq.get("collection_broadcast_printings", 0)),
                "PROJECT_PER_PRINTING": float(acq["project_printings"]),
                "POPCOUNT_PER_WORD": float(acq["popcount_words"]),
                "WALK_STEP": walk,
                "WALK_EMIT_PER_ROW": limit if not gather else 0.0,
                "GATHER_CARD_PASS": eval_domain if gather else 0.0,
                "GATHER_BITTEST_PER_PRINTING": float(acq["compose_scan_printings"]) if gather else 0.0,
                "GATHER_GROUP_PER_PRINTING": float(acq.get("gather_group_printings", 0)) if gather else 0.0,
                "GATHER_PUSH_PER_MATCH": matches if gather else 0.0,
                # The full-width printing-bitmap build, charged on the Gather arm only -- Perm and
                # OrderbyWalk had their rates fitted with it already absorbed. See
                # `COMPOSE_BUILD_PER_PRINTING_NS` in cost.rs for why it is scoped rather than shared.
                "BUILD_PER_PRINTING": float(acq["n_printings"]) if gather else 0.0,
                "FIXED": 1.0,
            },
            0.0,  # no residual-floor term in this arm, so nothing comes off the target
        )
    return None


def coeffs_for(plan: str, terms: list[str]) -> list[float]:
    """`CURRENT[plan]` as a vector in `terms` order, refusing any mismatch loudly.

    The whole point of naming the terms is that a disagreement between the arm mirror and the
    constants becomes an error that says which term, instead of a silently shifted column. So this
    checks the key sets both ways rather than just indexing.
    """
    have = CURRENT[plan]
    missing = [t for t in terms if t not in have]
    extra = [t for t in have if t not in terms]
    if missing or extra:
        msg = (
            f"CURRENT[{plan!r}] does not match `design_row`'s terms: "
            f"missing {missing}, unused {extra}. Add the constant from cost.rs, or drop the term."
        )
        raise KeyError(msg)
    return [have[t] for t in terms]


def perm_step_check(samples: list[dict]) -> tuple[int, float, float, float] | None:
    """Realized `perm_steps` against the estimate cost.rs derives. The ratio should be 1.00.

    Separate from `counter_check` because this term is derived rather than stored. It reads the
    engine's own `stream_perm_steps` -- exposed in Round 70 for exactly this -- rather than recomputing
    the formula, which is what it used to do and which was WRONG: it divided by `n_cards` where the arm
    uses `perm_walk_span`, so it graded the pre-Round-32 formula. That made THREE copies of this
    walk length in the tree (here, `design_row`, and the arm), two of them stale. The rate was fitted
    and cross-validated (kernel 0.958-1.256 ns/entry, traffic 1.15), but a rate can look right while
    the quantity it multiplies is wrong, so the ESTIMATE needs its own grade.

    What is being tested is the uniform-spread assumption: the walk is modelled as finding one match
    every `perm_walk_span / matches` entries, which holds if matches are scattered evenly through the
    walked segment and fails if they cluster. Clustering is not far-fetched -- the permutation is
    ordered by a sort column, and predicates correlate with sort columns (`year>=2020` under
    `order=released` is the extreme case), so a real skew here would be a genuine model defect.

    Round 69 measured it, and the skew is real but is NOT in the median: pooled p50 1.023, and sliced
    by sort column the medians stay flat (0.918-1.183) while the DISPERSION runs from 1.9x on
    `orderby=name` -- an order uncorrelated with any filter, where uniform spread genuinely holds -- to
    38.8x on `orderby=cmc`, where it does not. Nothing already on `PlanFeatures` predicts the residual
    (max |r| 0.12). So no coefficient fixes this, which is the whole point of grading it apart.

    Read the SPREAD, not just the median. The executor bounds its walk to the realized match span, so
    the ends of a cluster no longer cost anything and this ratio can only be inflated by non-matching
    entries INTERIOR to the span. Bounding those ends took p90 from 6.43 to 4.26 (p10 0.13 -> 0.08,
    median 1.00 -> 0.90) on one seed and sample length: a third of the tail was the leading prefix, and
    what is left is a different mechanism's to fix.

    Returns (rows, p10, median, p90) of realized/estimated, or None if no row walked.
    """
    ratios = []
    for s in samples:
        if s["plan"] != "StreamedSelect" or not s.get("perm_steps"):
            continue
        acq, matches = s["acq"], float(s["acq"]["matches"])
        if matches <= 0:
            continue
        # `cost::stream_perm_steps` itself. Absent from a run recorded before Round 70, in which case
        # fall back to the arm's CURRENT formula -- `perm_walk_span`, not `n_cards`.
        if "stream_perm_steps" in acq:
            estimate = float(acq["stream_perm_steps"])
        else:
            page_span = float(min(s["offset"] + s["limit"], matches))
            span = float(acq.get("perm_walk_span", acq["n_cards"]))
            estimate = min(page_span * span / matches, span)
        if estimate > 0:
            ratios.append(float(s["perm_steps"]) / estimate)
    if not ratios:
        return None
    ratios.sort()
    return (len(ratios), ratios[len(ratios) // 10], ratios[len(ratios) // 2], ratios[(9 * len(ratios)) // 10])


def counter_check(samples: list[dict]) -> dict[str, list[tuple[str, float]]]:
    """Realized counter vs the feature that should predict it, per plan. Ratios should be 1.00."""
    # `scan_units` pairs with `printings_examined`, not the `printing_span` this used to read: the span
    # is computed by the caller before the match kernel runs, so in card mode -- where every kernel
    # stops at the first qualifying printing -- it reports work that never happened.
    pairs = (("cards_visited", "eval_domain"), ("printings_examined", "scan_units"), ("matches_pushed", "matches"))

    def feature_for(plan: str, counter: str, row: dict) -> str:
        """Which field the plan's arm actually reads -- not all three plans read the same one.

        StreamedSelect's scan term is `stream_scan_units`, not `scan_units`. This returned the latter
        until Round 70, so the check graded P3 against a number its arm never touches; the two default
        to equal and diverge wherever an acquire knows P3 examines fewer printings
        (`residual_card_invariant` zeroes it, the legality divergent-share correction rescales it).
        `design_row` above has always read the right field, so this was a diagnostic-only disagreement
        with the fit it sits beside.
        """
        if counter == "printings_examined":
            if plan == "PrintingCompose":
                return "compose_scan_printings" if row["acq"].get("compose_paging") == "Gather" else "printings_walked"
            if plan == "StreamedSelect":
                return "stream_scan_units"
        return {"cards_visited": "eval_domain", "printings_examined": "scan_units", "matches_pushed": "matches"}[counter]

    out: dict[str, list[tuple[str, float]]] = {}
    by_plan: dict[str, list[dict]] = collections.defaultdict(list)
    for s in samples:
        by_plan[s["plan"]].append(s)
    for plan, rows in sorted(by_plan.items()):
        instrumented = [r for r in rows if r["ns_round_total"] and r["cards_visited"]]
        if not instrumented:
            continue  # only the two scan plans carry counters; absent is not the same as wrong
        checks = []
        for counter, _default in pairs:
            # Only grade rows whose arm actually multiplies the feature by a rate. StreamedSelect's
            # scan term is `if tier_ns > 0.0 { scan_units * ... } else { 0.0 }`, so on an all_match
            # query (tier 0) `scan_units` is a number the model never reads -- and grading it anyway
            # read 0.65 here and vetoed the whole plan's fit over a term that contributes zero.
            graded = instrumented
            if counter == "printings_examined" and plan == "StreamedSelect":
                graded = [r for r in instrumented if r["acq"]["residual_tier_ns100"] > 0]
            # Compose's three paging branches charge different features, so grading a row against a
            # feature its branch never multiplies by a rate manufactures a defect. Its Gather branch
            # charges eval_domain/compose_scan_printings/matches; Perm and OrderbyWalk charge only
            # printings_walked and stop at page_offset+limit, so their `cards_visited` is 0 (the
            # orderby walk steps a value structure, not cards) and their `matches_pushed` is a page,
            # not a total. Ungated, those read 0.02 and 0.01 and vetoed the plan's whole fit.
            if plan == "PrintingCompose":
                gather_only = counter in ("cards_visited", "matches_pushed")
                graded = [r for r in graded if (r.get("paging_taken") in ("Gather", "GatherWalkDeclined")) == gather_only]
            if not graded:
                continue
            got = [r[counter] / max(r["acq"][feature_for(plan, counter, r)], 1) for r in graded]
            feature = feature_for(plan, counter, graded[0])
            if got:
                checks.append((f"{counter}/{feature}", statistics.median(got)))
        if checks:
            out[plan] = checks
    return out


def collect(  # noqa: PLR0913 - four sampling inputs plus the two knobs that define the POPULATION being fitted
    engine: object,
    rng: random.Random,
    budget: Budget,
    sampler: QuerySampler,
    *,
    legacy_target: bool = False,
    vary_prefer: bool = False,
) -> list[dict]:
    """Sample queries until the budget runs out, keeping one row per plan that actually ran.

    Drives `costbench.iter_samples` rather than a private copy of the sampling loop, which is what
    this had. That copy called `engine.explain(**kw)` with no `prefer` and
    `explain_analyze(prefer="default", ...)`; those agree only because `explain`'s pyo3 default
    happens to be `"default"` -- an agreement by coincidence, over a parameter the ACQUIRE reads
    (Round 66: `compose_scan_printings` has a `Mode::Card if Prefer::Default` arm). `iter_samples`
    passes the same drawn `prefer` to both calls by construction, whichever way it is drawn.

    `vary_prefer` is OFF by default, matching the population every constant in `cost.rs` was fitted
    on, so a rate that moves is the model moving rather than the sampler. It is a knob rather than a
    hard-coded `False` because `prefer` decides whether a card-mode match kernel may stop at the first
    qualifying printing -- a ~3x swing in per-card work that no other sampled parameter reaches, and
    one `scan_units` does not model -- so "does this rate depend on `prefer`" is a question worth being
    able to ask directly.

    **Measured, and the answer is no**, which is worth recording because it is not the intuitive one.
    Same seed, same 20,000 queries, same target, only `prefer` varied:

        GatheredScan   CARD_PASS+FLOOR  39.52 pinned -> 42.07 varied   (+6%)
        StreamedSelect CARD_PASS+FLOOR  13.11 pinned -> 13.35 varied   (+2%)

    So the fitted rates are near-invariant to it, and a large gap between a fit run today and a number
    quoted in an old `cost.rs` doc comment is the MODEL having changed underneath (Rounds 79-82 moved
    features and constants), not the sampler. Do not read one as the other -- I did, and built a
    confident causal story out of a stale comparison before this measurement contradicted it.
    """
    samples: list[dict] = []
    for sample in iter_samples(engine, sampler, rng, budget, vary_prefer=vary_prefer):
        acq, kw = sample.acquire, sample.kw
        for p in sample.plans:
            # `predicted_ns` screens the infinite cost a declining compose reports, which no `<= 0`
            # guard catches.
            measured = fit_target_ns(p, acq, legacy=legacy_target)
            if not p["trials_ns"] or costbench.predicted_ns(p) is None or measured is None:
                continue
            samples.append(
                {
                    "plan": p["plan"],
                    "q": sample.q,
                    "unique": kw["unique"],
                    "acq": acq,
                    "limit": kw["limit"],
                    "offset": kw["offset"],
                    "measured": measured,
                    "predicted": float(p["predicted_ns"]),
                    "ns_round_total": p["ns_round_total"],
                    "cards_visited": p["cards_visited"],
                    "ns_setup": p["ns_setup"],
                    "ns_loop": p["ns_loop"],
                    "ns_finish": p["ns_finish"],
                    "printing_span": p["printing_span"],
                    "paging_taken": p.get("paging_taken"),
                    "printings_examined": p["printings_examined"],
                    "matches_pushed": p["matches_pushed"],
                    "perm_steps": p.get("perm_steps", 0),
                }
            )
    return samples


def mirror_matches_engine(samples: list[dict]) -> tuple[float, int]:
    """Fraction of rows where this file's arm mirror equals the engine's own `predicted_ns`.

    `design_row` + `CURRENT` is a Python reimplementation of `cost::plan_cost`. If it has drifted, the
    fitter is fitting coefficients for a model the engine does not run, and every number it prints is
    meaningless. Cheap to check exactly, because `explain` reports the engine's prediction.

    Note that this compares against `predicted_ns` while `fit_target_ns` may target the EXECUTOR
    alone. That is not an inconsistency: `plan_cost` today IS the executor arm and nothing else, so
    the two agree. Should a term ever be added to `plan_cost` that `design_row` does not mirror -- the
    dispatch build was proposed and rejected -- this check is what would have to subtract it, and the
    engine would have to publish which rows it was charged on rather than have Python re-derive the
    gate from `count_source`.
    """
    ok = total = 0
    for x in samples:
        built = design_row(x["plan"], x["acq"], x["limit"], x["offset"])
        if built is None or x["predicted"] <= 0:
            continue
        row, excess = built
        # Paired by NAME, so a term present in one side and not the other is an error rather than a
        # silent off-by-one down the rest of the vector.
        coeffs = CURRENT[x["plan"]]
        coeffs_for(x["plan"], list(row))  # key-set check; raises naming the offending term
        mine = sum(coeffs[k] * v for k, v in row.items()) + excess
        total += 1
        ok += abs(mine / x["predicted"] - 1.0) < MIRROR_TOLERANCE
    return (ok / total if total else 0.0), total


def fit_plan(plan: str, rows: list[dict], label: str | None = None) -> dict[str, float] | None:
    """Fit and report one plan's arm: current vs fitted coefficient, and the agreement each gives.

    Returns the fitted rates keyed by term name, so a caller partitioning by distinct-on compares
    them by name rather than by position; `None` when the plan has no fittable design.
    """
    design, names, targets = [], None, []
    for r in rows:
        built = design_row(plan, r["acq"], r["limit"], r["offset"])
        if built is None:
            # Skip the ROW, not the plan. Compose's Decline branch costs infinity and so has no arm to
            # fit, but `explain_analyze` runs every plan regardless of the model, so 440 of 2,886
            # compose rows come back Decline-and-measured. Aborting the plan on the first of them is
            # why PrintingCompose silently never got fitted.
            continue
        row, excess = built
        # The numeric fit needs a stable column order, so one is fixed from the first row and every
        # later row is projected onto it -- and asserted to carry the same terms, since a branch that
        # emitted a different term set would otherwise fit two meanings into one column.
        if names is None:
            names = list(row)
        elif list(row) != names:
            msg = f"{plan}: design_row emitted terms {list(row)} after {names}; the arm's columns must be fixed"
            raise KeyError(msg)
        design.append([row[n] for n in names])
        # Fit and score the part coefficients control: the residual EXCESS over the floor is not
        # scaled by any of them, so it comes off the target rather than riding as a column.
        targets.append(max(r["measured"] - excess, 1.0))
    # One row per DISTINCT feature vector, at its median measured time. The sampler draws from a
    # fixed predicate pool, so a few hundred distinct query shapes turn into tens of thousands of
    # rows; left duplicated, the fit optimises whichever shape recurs most and degenerates to a
    # constant (measured: a 19.8 µs StreamedSelect FIXED term and every per-unit rate at zero).
    if names is None:
        return None
    grouped: dict[tuple[float, ...], list[float]] = collections.defaultdict(list)
    for vec, y in zip(design, targets, strict=True):
        grouped[tuple(vec)].append(y)
    design = [list(k) for k in grouped]
    targets = [statistics.median(v) for v in grouped.values()]
    weights = [float(len(v)) for v in grouped.values()]
    current = coeffs_for(plan, names)
    coeffs = fit_log_ratio(design, targets, current, weights)

    print(f"\n=== {label or plan} ({len(rows):,} rows, {len(design):,} distinct shapes) ===")
    print(f"{'term':<34}{'current':>12}{'fitted':>12}{'x':>8}")
    for name, cur, c in zip(names, current, coeffs, strict=True):
        print(f"{name:<34}{cur:>12.2f}{c:>12.2f}{c / cur if cur else math.inf:>8.2f}")

    # Both scored on the same deduplicated shapes, so the comparison is like for like.
    before, after = [], []
    for d, y, w in zip(design, targets, weights, strict=True):
        for coefs, out in ((current, before), (coeffs, after)):
            pred = sum(c * v for c, v in zip(coefs, d, strict=True))
            out.extend([y / pred if pred > 0 else math.inf] * int(w))
    for tag, ratios in (("current", before), ("fitted", after)):
        finite = [x for x in ratios if math.isfinite(x)]
        near = sum(1 for x in finite if AGREE_LO <= x <= AGREE_HI) / len(finite)
        qs = statistics.quantiles(finite, n=10)
        print(
            f"  {tag:<8} median {statistics.median(finite):>6.2f}   p10 {qs[0]:>6.2f}   p90 {qs[8]:>7.2f}   within 25% {near:>5.0%}"
        )
    return dict(zip(names, coeffs, strict=True))


def fit_by_mode(plan: str, rows: list[dict]) -> None:
    """Fit one arm separately per distinct-on, and show how far the coefficients move.

    A single arm is fitted across all three modes today, on the assumption that distinct-on changes
    only the FEATURES (`scan_units`, `matches`) and not the per-unit costs. Where that assumption
    holds, the three fits land on the same rates and the split is just noise. Where they diverge
    sharply, the arm is missing a mode-dependent term and no single set of constants can serve all
    three — the fit will land on a compromise that is wrong everywhere.

    This is a diagnostic, not a source of shippable constants: each partition sees a third of the
    rows, so a term that is weakly identified overall becomes noisy here. Read the SPREAD, and treat
    a flagged term as a question about the arm's shape rather than as three numbers to hard-code.
    """
    by_mode: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_mode[r["unique"]].append(r)
    fits: dict[str, dict[str, float]] = {}
    for mode, mrows in sorted(by_mode.items()):
        if len(mrows) < MIN_ROWS_FOR_FIT:
            continue
        got = fit_plan(plan, mrows, label=f"{plan} / {mode}")
        if got is not None:
            fits[mode] = got
    if len(fits) < 2:  # noqa: PLR2004 - nothing to compare against
        return
    modes = list(fits)
    names = list(fits[modes[0]])
    print(f"\n--- {plan}: coefficient spread across distinct-on ---")
    print(f"{'term':<34}" + "".join(f"{m:>11}" for m in modes) + f"{'max/min':>10}")
    for name in names:
        vals = [fits[m][name] for m in modes]
        lo, hi = min(vals), max(vals)
        # A term at ~0 in every mode is unidentified, not mode-dependent; ignore it either way.
        ratio = hi / lo if lo > MODE_SPLIT_MIN_RATE else math.inf
        flag = "  MODE-DEPENDENT" if hi > MODE_SPLIT_MIN_RATE and ratio > MODE_SPLIT_FACTOR else ""
        shown = "   inf" if math.isinf(ratio) else f"{ratio:>10.2f}"
        print(f"{name:<34}" + "".join(f"{v:>11.2f}" for v in vals) + f"{shown}{flag}")
    print(f"  Flagged where the rate moves more than {MODE_SPLIT_FACTOR}x between modes: that is the arm")
    print("  missing a mode-dependent term, not three constants waiting to be hard-coded.")


def parse_args() -> tuple[argparse.Namespace, Budget]:
    """Command line, plus the sampling budget it implies. Rejects the values that fit nothing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    budget_arg = parser.add_mutually_exclusive_group()
    budget_arg.add_argument("--n-queries", type=int, help=f"queries to sample (reproducible; default {DEFAULT_N_QUERIES:,})")
    budget_arg.add_argument("--seconds", type=float, help="wall-clock budget instead of a query count")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="uniform", help="query sampler weighting; RANK by uniform")
    parser.add_argument("--corpus", type=pathlib.Path, default=REPO_ROOT / "benchmarks/bitplanes/corpus.jsonl")
    parser.add_argument("--shm-path", type=pathlib.Path, default=None)
    parser.add_argument(
        "--legacy-target",
        action="store_true",
        help="fit every arm against `plan_self_ns`, as before the executor/build split; see `fit_target_ns`",
    )
    parser.add_argument(
        "--vary-prefer",
        action="store_true",
        help="draw `prefer` from the sampler instead of pinning it to `default`; a DIAGNOSTIC, see `collect`",
    )
    parser.add_argument(
        "--by-mode",
        action="store_true",
        help="also fit each arm separately per distinct-on, to expose rates that are not mode-independent",
    )
    args = parser.parse_args()
    if args.n_queries is not None and args.n_queries <= 0:
        parser.error("--n-queries must be positive")
    if args.seconds is not None and args.seconds <= 0:
        parser.error("--seconds must be positive")
    budget = (
        Budget(seconds=args.seconds, warmups=NUM_WARMUPS, trials=NUM_TRIALS)
        if args.seconds is not None
        else Budget(sample=args.n_queries or DEFAULT_N_QUERIES, warmups=NUM_WARMUPS, trials=NUM_TRIALS)
    )
    return args, budget


def main() -> None:
    """Collect a sample, verify features track counters, then fit each scan plan's rates."""
    args, budget = parse_args()
    engine = load_engine(args.corpus, args.shm_path or args.corpus.with_suffix(".fit.store"))
    # Pass the sampler explicitly: `collect`'s fallback built one off a hardcoded corpus path, so
    # `--corpus` was loading the engine from one file and drawing queries from another (or, off the
    # default checkout, raising FileNotFoundError). Values must come from the corpus the engine holds.
    sampler = QuerySampler(args.corpus, args.mode)
    samples = collect(
        engine, random.Random(args.seed), budget, sampler, legacy_target=args.legacy_target, vary_prefer=args.vary_prefer
    )
    bound = f"{args.seconds:.0f}s" if args.seconds is not None else f"{budget.sample:,} queries"
    target = "plan_self_ns (legacy)" if args.legacy_target else "the executor alone for the two materializing arms"
    prefer = "varied (DIAGNOSTIC)" if args.vary_prefer else "pinned to default"
    print(f"\n{len(samples):,} plan-rows collected over {bound}, mode={args.mode}, prefer {prefer}")
    print(f"fit target: {target}")

    agree, checked = mirror_matches_engine(samples)
    print(f"arm mirror vs engine predicted_ns: {agree:.1%} exact over {checked:,} rows")
    if agree < MIRROR_MIN_AGREEMENT:
        print(
            f"  REFUSING TO FIT: the Python mirror of cost.rs disagrees with the engine on "
            f"{1 - agree:.1%} of rows. `design_row`/`CURRENT` have drifted from the shipped arms; any "
            f"coefficients fitted now would be for a model the engine does not run. Sync them first."
        )
        return

    print(f"\n{'plan':<20}{'counter / feature':<40}{'median':>9}")
    suspect: set[str] = set()
    for plan, checks in counter_check(samples).items():
        for label, ratio in checks:
            flag = "" if abs(ratio - 1.0) <= COUNTER_TOL else "  <-- FEATURE, not rate"
            if flag:
                suspect.add(plan)
            print(f"{plan:<20}{label:<40}{ratio:>9.2f}{flag}")
    print("  a ratio far from 1.00 is a miscounted feature; no rate can absorb it.")
    perm = perm_step_check(samples)
    if perm is not None:
        rows, p10, med, p90 = perm
        print(f"\nStreamedSelect perm_steps realized/estimated over {rows:,} walking rows:")
        print(f"  p10 {p10:.2f}   median {med:.2f}   p90 {p90:.2f}")
        print("  tests the uniform-spread assumption behind `page_span * perm_walk_span / matches`; skew")
        print("  would show as a median away from 1.00, and clustering as a wide p10-p90 spread.")
        print("  Round 69: the median is fine and the SPREAD is the defect -- 1.9x on orderby=name")
        print("  against 38.8x on orderby=cmc, at flat medians. No rate can represent that.")

    by_plan: dict[str, list[dict]] = collections.defaultdict(list)
    for s in samples:
        by_plan[s["plan"]].append(s)
    for plan, rows in sorted(by_plan.items()):
        fittable = any(design_row(plan, r["acq"], r["limit"], r["offset"]) is not None for r in rows)
        if len(rows) < MIN_ROWS_FOR_FIT or not fittable:
            continue
        if plan in suspect:
            print(f"\n=== {plan} — SKIPPED: fix the feature above before fitting rates to it ===")
            continue
        fit_plan(plan, rows)
        if args.by_mode:
            fit_by_mode(plan, rows)


if __name__ == "__main__":
    main()
