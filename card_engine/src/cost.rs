//! Per-plan cost model (#702 step 3b).
//!
//! Parametric cost formulas — one per `PhysicalPlan` — whose constants are FIT
//! to the `plan_cost_calibration` bench (src/tests.rs) measured on the real
//! corpus archive (`benchmarks/verify-order/real.store`: 31508 cards, 97206
//! printings). The routing decision this feeds is `argmin_plan plan_cost`; the
//! objective the constants were fit against is that `argmin` reproduces the
//! empirically-fastest ("gold") plan per query × mode × page depth.
//!
//! **Unwired**: nothing in `run_query` calls this yet. It is validated by the
//! `plan_cost_model_matches_gold` test (src/tests.rs), which computes real
//! `PlanFeatures` (via `prepare_candidates` + `verify_cost_tier`) and checks the
//! model's argmin against re-measured gold.
//!
//! ## Units and provenance
//!
//! Constants are in nanoseconds (or ns per unit of work), fit from the
//! calibration table dated 2026-07-19 on this machine (min-of-60, warmup 5, real
//! corpus). Per the "Keeping costs/plans current" section of
//! docs/issues/00702-engine-plan-selection-layer.md: `argmin` cares about the
//! *ratios* between plans, so a uniform hardware speed change preserves the
//! choice; recalibrate on non-uniform changes (a plan reimplemented, a new index
//! shifting a predicate class, a new plan). Each constant's doc-comment names the
//! data point(s) it was fit from, mirroring `verify_cost_tier`'s provenance style.
//!
//! ## Predicate cost is common-mode
//!
//! The per-card verify tier (`residual_tier_ns100`) is added to BOTH the gather
//! and stream per-card terms, so it largely cancels in their argmin — cardinality
//! and plan structure do the deciding (see #702 "Cost model" §). Popcount (P2)
//! and range-scan (P1) run only when the residual is `True`/absent, so they carry
//! no verify term at all.

use super::*;

/// Cheap, per-query features the cost model consumes. All counts are exact
/// (from materialized truth / `prepare_candidates`), so this validates the model
/// *given* true features; estimator regret is a separate later step (#702 step 4).
#[allow(dead_code)] // consumed only through the (unwired) plan_cost entry point
pub(crate) struct PlanFeatures {
    /// Distinct cards in the corpus (card-space universe).
    pub n_cards: u32,
    /// Distinct printings in the corpus (printing-space universe).
    pub n_printings: u32,
    /// Result cardinality in the plan's operating space (card total for card
    /// mode, printing total for printing mode). Use measured truth here.
    pub matches: u32,
    /// Cards the per-card work visits: the narrowed candidate count when
    /// `prepare_candidates` produced a candidate list, else `n_cards`.
    pub eval_domain: u32,
    /// Per-card verify cost of the residual, ns×100 (`verify_cost_tier`); `0`
    /// when `all_match_known` (the walk skips `card_pass` entirely).
    pub residual_tier_ns100: u32,
    /// Page size (`limit`).
    pub limit: u32,
    /// Page offset.
    pub offset: u32,
}

// ─── P1: PrintingRangeScan ──────────────────────────────────────────────────
// A bare broad range predicate under unique=printing: total from the range
// index's binary search, page from an early-stopping permutation walk. Cost is
// dominated by how far the walk must go to fill the page, which is
// (offset+limit) matches at density `match_rate` printings.

/// ns per permutation step walked. Fit from usd<5 printing shallow/deep
/// (match_rate=0.734): (88708−666)ns over (13706−82) steps ≈ 6.5; year>=2020
/// printing gave ≈3.5. The per-step cost is noisy (printing clustering along the
/// sort order), so this is a representative middle value — exactness matters
/// little here because P1, when applicable, wins its rows by 1-2 orders of
/// magnitude over P3/P4.
const RANGE_WALK_STEP_NS: f64 = 4.5;
/// Fixed P1 setup (binary searches + walk init). Fit from usd<5 printing shallow
/// (666ns − 82 steps × RANGE_WALK_STEP_NS ≈ 150ns).
const RANGE_FIXED_COST_NS: f64 = 150.0;
/// Floor on match_rate so a (near-)empty range can't divide by ~0.
const MATCH_RATE_FLOOR: f64 = 1.0 / 1_000_000.0;

// ─── P2: PlanePopcountOrder ─────────────────────────────────────────────────
// unique=card, filter fully consumed to True: the plane bitmap IS the exact
// match set. Scatter the match bits through the inverse permutation (O(matches)),
// scan words for the page (O(N/64)), emit the page. Flat in page depth.

/// ns per match scattered through the inverse permutation. ~0.65 observed:
/// color(bit3) card 6606 matches → 4208ns, t:creature card 17317 → 11375ns both
/// land near 0.65 ns/match with a small floor.
const PLANE_POPCOUNT_SCATTER_PER_MATCH_NS: f64 = 0.65;
/// ns per 64-card word scanned for the page boundary (N/64 = 492 words on this
/// corpus). Small — the popcount word scan is cheap next to the scatter; fit as
/// a modest floor component alongside PLANE_POPCOUNT_FIXED_COST_NS (color3
/// t:creature card ≈4250ns at 4001 matches leaves ~1600ns of floor).
const PLANE_POPCOUNT_PER_WORD_NS: f64 = 1.0;
/// ns per emitted page card. Small; folded into the floor.
const PLANE_POPCOUNT_EMIT_PER_CARD_NS: f64 = 2.0;
/// Fixed P2 setup (plane eval into the bitmap, buffers).
const PLANE_POPCOUNT_FIXED_COST_NS: f64 = 200.0;

// ─── P3: StreamedSelect ─────────────────────────────────────────────────────
// Match phase walks eval_domain cards computing per-card counts, then either
// walks the sort permutation to the page (broad) OR — when total <=
// STREAM_MIN_MATCHES — gathers via a `for cid in 0..n_cards` scan and
// quickselects (run_query_streamed, lib.rs). That small-total gather is the
// O(n_cards) FLOOR that makes P3 lose badly on narrow queries: a 5-row query
// forced onto P3 measured ~52µs = n_cards × ~1.65ns.

/// ns per card visited in the match phase (card_pass + count). Fit from broad
/// queries where eval_domain drives P3 and it is nearly independent of match
/// count across modes: t:creature card 17317 cards → 53542ns (3.09), printing
/// 17317 → 52791ns (3.05); cmc>=0 card 31508 → 101541ns (3.22), printing 31508 →
/// 97000ns (3.08). The residual verify tier adds on top (common-mode with P4).
const STREAM_MATCH_PHASE_PER_CARD_NS: f64 = 3.0;
/// ns per match, for the permutation-walk emit. Small — P3 measured nearly flat
/// in match count once eval_domain is fixed (see STREAM_MATCH_PHASE_PER_CARD_NS),
/// so this is a minor term.
const STREAM_EMIT_PER_MATCH_NS: f64 = 0.1;
/// ns per card scanned in the small-total gather (`for cid in 0..n_cards`,
/// counts[cid]==0 check). Cheaper than a match-phase visit (no filter work). Fit
/// from the narrow-query floor: cmc>=15 / o:annihilator / cmc==7 card SHALLOW all
/// ~52µs = 31508 × 1.65. Only added when `matches <= STREAM_MIN_MATCHES`, the
/// exact condition that routes P3 into that gather branch.
const STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS: f64 = 1.65;
/// Fixed P3 setup (counts buffer resize/clear, thread-local).
const STREAM_FIXED_COST_NS: f64 = 500.0;

// ─── P4: GatheredScan ───────────────────────────────────────────────────────
// The universal fallback: per-card loop pushes every match's sort key into a
// Vec (O(matches)), then select_page quickselects the page. Visits eval_domain
// cards, each paying the residual verify tier.

/// ns per card visited in the gathered loop (card_pass + push). Fit with
/// GATHER_PUSH_PER_MATCH_NS from all-match broad card queries where
/// eval_domain==matches and tier=0, which pin the sum ≈ 6.3-6.9 (cmc>=0 31508 →
/// 216667ns = 6.87; t:creature 17317 → 109834 = 6.34; color3 6606 → 40458 =
/// 6.12). Split so the gathered loop's per-visit cost exceeds P3's
/// STREAM_MATCH_PHASE_PER_CARD_NS — the measured P4/P3 ≈ 2× ratio on broad
/// queries — with the remainder attributed to the per-match push.
const GATHER_VISIT_PER_CARD_NS: f64 = 5.5;
/// ns per match pushed into the sort-key Vec + quickselected.
const GATHER_PUSH_PER_MATCH_NS: f64 = 1.0;
/// ns per page slot materialized. Fit from the deep-vs-shallow gap on broad
/// queries (cmc>=0 card: 225708−216667 ≈ 9041ns over 10000 extra offset ≈ 0.9),
/// bounded by matches: narrow deep pages (offset > matches) measured ≈ shallow
/// (select_page returns early), so the term uses min(offset+limit, matches).
const GATHER_SELECT_PER_PAGE_SLOT_NS: f64 = 0.9;
/// Fixed P4 setup. Fit from the narrowest query (cmc>=15 card shallow 208ns at
/// eval_domain=5: 208 − 5×(GATHER_VISIT_PER_CARD_NS+GATHER_PUSH_PER_MATCH_NS) −
/// 5×GATHER_SELECT_PER_PAGE_SLOT_NS ≈ 170).
const GATHER_FIXED_COST_NS: f64 = 200.0;

/// Estimated wall-clock cost of running `plan` on a query with features `f`, in
/// nanoseconds. Lower is cheaper; the planner routes to `argmin_plan plan_cost`.
/// Only meaningful for plans that are *applicable* to the query (the caller
/// filters by the applicability predicates / `run_query_with_plan` returning
/// `Some`); an inapplicable plan's cost is not defined.
#[allow(dead_code)] // unwired: routing is a later #702 step; only the test calls this
pub(crate) fn plan_cost(plan: PhysicalPlan, f: &PlanFeatures) -> f64 {
    let n_cards = f64::from(f.n_cards);
    let n_printings = f64::from(f.n_printings);
    let matches = f64::from(f.matches);
    let eval_domain = f64::from(f.eval_domain);
    let tier_ns = f64::from(f.residual_tier_ns100) / 100.0;
    let limit = f64::from(f.limit);
    let page_span = f64::from((f.offset.saturating_add(f.limit)).min(f.matches));

    match plan {
        PhysicalPlan::PrintingRangeScan => {
            let match_rate = (matches / n_printings).max(MATCH_RATE_FLOOR);
            (page_span / match_rate) * RANGE_WALK_STEP_NS + RANGE_FIXED_COST_NS
        }
        PhysicalPlan::PlanePopcountOrder => {
            matches * PLANE_POPCOUNT_SCATTER_PER_MATCH_NS
                + (n_cards / 64.0) * PLANE_POPCOUNT_PER_WORD_NS
                + limit * PLANE_POPCOUNT_EMIT_PER_CARD_NS
                + PLANE_POPCOUNT_FIXED_COST_NS
        }
        PhysicalPlan::StreamedSelect => {
            // The small-total gather branch (run_query_streamed) scans all
            // n_cards when total <= STREAM_MIN_MATCHES — the O(N) floor that
            // sinks P3 on narrow queries.
            let floor = if u64::from(f.matches) <= *super::STREAM_MIN_MATCHES as u64 {
                n_cards * STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS
            } else {
                0.0
            };
            eval_domain * (STREAM_MATCH_PHASE_PER_CARD_NS + tier_ns) + matches * STREAM_EMIT_PER_MATCH_NS + floor + STREAM_FIXED_COST_NS
        }
        PhysicalPlan::GatheredScan => {
            eval_domain * (GATHER_VISIT_PER_CARD_NS + tier_ns)
                + matches * GATHER_PUSH_PER_MATCH_NS
                + page_span * GATHER_SELECT_PER_PAGE_SLOT_NS
                + GATHER_FIXED_COST_NS
        }
    }
}
