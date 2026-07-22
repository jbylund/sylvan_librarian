//! Per-plan cost model (#702 step 3b).
//!
//! Parametric cost formulas — one per `PhysicalPlan` — whose constants are FIT
//! to the `plan_cost_calibration` bench (src/tests.rs) measured on the real
//! corpus archive (`benchmarks/verify-order/real.store`: 31508 cards, 97206
//! printings). The routing decision this feeds is `argmin_plan plan_cost`; the
//! objective the constants were fit against is that `argmin` reproduces the
//! empirically-fastest ("gold") plan per query × mode × page depth.
//!
//! `run_query_routed` calls `plan_cost` on every query (it IS the plan selector).
//! It is also validated by the `plan_cost_model_matches_gold` test (src/tests.rs),
//! which computes real `PlanFeatures` (via `prepare_candidates` + `verify_cost_tier`)
//! and checks the model's argmin against re-measured gold.
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
//!
//! ## Calibration scope: operating-space via `scan_units` (card + printing)
//!
//! The P3/P4 per-card work was originally fit on CARD mode alone, where the loop
//! breaks at the first matching printing, and it under-predicted printing/artwork
//! P3/P4 by ~`n_printings/n_cards` (≈3.09) because those modes scan EVERY printing
//! of every candidate. The fix is `PlanFeatures::scan_units` (not a `mode` branch):
//! the per-card `card_pass` term is driven by `eval_domain` (candidate cards) and
//! the per-row residual scan + its verify `tier` by `scan_units` (rows scanned in
//! the plan's operating space — `≈ eval_domain` for card, printings-under-candidates
//! for printing/artwork). One mode-agnostic formula; the caller fills `scan_units`
//! per mode via `scan_units()`. The `_CARD_PASS`/`_SCAN` split of the old lumped
//! `VISIT` constants was fit to hold card unchanged while correcting printing (see
//! each constant's doc). Artwork rides the printing path (same all-printings scan);
//! its confirming validation is still pending a bench run.
//!
//! A 1200-query designed refit (`plan_cost_refit`, weighted LSQ, 70/30 train/test)
//! VALIDATED rather than beat these: P1's fitted STEP=4.14 ≈ 4.5 (test 1.38× ≈
//! train); P3/P4 could NOT be fit — `SCAN` goes negative because `scan_units` and
//! `matches` both scale with printing count in the workload, a STRUCTURAL
//! collinearity no corpus size fixes (P2 stays data-starved: pure-plane queries are
//! rare). The `_CARD_PASS`/`_SCAN`/`PUSH` split is a physical prior resolving what
//! data alone cannot. Model sits at ~1.4× absolute (slow bucket), ordering-correct
//! (argmin==gold 87/88) — the identifiable ceiling for this workload.

use super::*;

/// Cheap, per-query features the cost model consumes, built once per query by
/// `run_query_routed`'s `acquire` step. All counts are exact or cheap-exact (plane
/// popcount / range `k` / candidate count), never estimated.
pub(crate) struct PlanFeatures {
    /// Distinct cards in the corpus (card-space universe).
    pub n_cards: u32,
    /// Distinct printings in the corpus (printing-space universe).
    pub n_printings: u32,
    /// Result cardinality in the plan's operating space (card total for card
    /// mode, printing total for printing/artwork mode). Use measured truth here.
    pub matches: u32,
    /// Candidate CARDS the loop iterates (one `card_pass` each): the narrowed
    /// candidate count when `prepare_candidates` produced a list, else `n_cards`.
    pub eval_domain: u32,
    /// Rows the per-row residual scan touches, in the plan's operating space — the
    /// dominant P3/P4 driver. Card mode breaks at the first matching printing
    /// (≈ one row per candidate), so `scan_units ≈ eval_domain`; printing/artwork
    /// scan every printing of every candidate, so it is the printing count under
    /// those cards (`≈ eval_domain · n_printings/n_cards`). This is the field that
    /// makes the formula operating-space-correct without a `mode` branch (see the
    /// module "Calibration scope" note); the caller fills it via `scan_units()`.
    pub scan_units: u32,
    /// Per-card verify cost of the residual, ns×100 (`verify_cost_tier`); `0`
    /// when `all_match_known` (the walk skips `card_pass` entirely).
    pub residual_tier_ns100: u32,
    /// Page size (`limit`).
    pub limit: u32,
    /// Page offset.
    pub offset: u32,
    /// Printings scattered to **build the printing-space bitmap** — the first synthesis pass: the range
    /// slice (`CardRangePopcount` fuses this straight into card bits; `PrintingCompose` scatters it into
    /// a printing bitmap) and the legality broadcast-down. Border/rarity are precomputed planes → `0`.
    /// Costed at `SCATTER_PER_PRINTING_NS`.
    pub synth_printings: u32,
    /// Printings scattered in the **projection pass** — printing-bitmap → card/artwork existence — that
    /// `PrintingCompose` runs *on top of* `synth_printings` (a second O(k) pass). `CardRangePopcount`
    /// leaves this `0` because it fuses build+project into one pass; setting it in acquire (both plans
    /// do) is what lets the shared feats cost compose's extra pass honestly, so a bare range doesn't
    /// mis-route to compose. Costed at `SCATTER_PER_PRINTING_NS`; `0` for printing mode (no projection).
    pub project_printings: u32,
    /// 64-bit words of the **result-space** bitmap the total popcount + skip-scan touches — the field
    /// that keeps the popcount term honest across distinct-ons: `n_printings/64` (printing),
    /// `n_cards/64` (card), `n_artworks/64` (artwork). Set by `PrintingCompose`; `0` elsewhere.
    pub popcount_words: u32,
}

// ─── P1: PrintingRangeScan ──────────────────────────────────────────────────
// A bare broad range predicate under unique=printing: total from the range
// index's binary search, page from an early-stopping permutation walk. Cost is
// dominated by how far the walk must go to fill the page, which is
// (offset+limit) matches at density `match_rate` printings.

/// ns per permutation step walked. Fit from usd<5 printing shallow/deep
/// (match_rate=0.734): (88708−666)ns over (13706−82) steps ≈ 6.5; year>=2020
/// printing gave ≈3.5. The per-step cost is noisy (printing clustering along the
/// sort order), so this is a representative middle value.
///
/// CAUTION: `printing_range_route_probe` measures P1 fidelity swinging 0.10×–2.24×
/// against this single constant — a walk step in printing mode scans a whole card's
/// printings, whose count varies by query. The earlier claim that exactness "matters
/// little because P1 wins by 1-2 orders of magnitude" is FALSE at depth: at offset
/// 20000 P1 leads P3 by only ~2×, and that is exactly where this loose constant
/// flips the argmin (the model routes off gold-P1). Sharpening P1 needs a per-step
/// term keyed on printings-scanned, not a scalar — deferred with the printing model.
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

/// Per-printing cost of scattering one printing into a bitmap at query time — the single physical op
/// (write a bit into a word) behind every `synth_printings`: `CardRangePopcount`'s range-slice build,
/// and `PrintingCompose`'s legality broadcast-down + card/artwork projection-up. Measured ~1.3 ns/printing
/// (`card_range_build_cost_split`, the ~80.5k-printing `usd<50` slice) and ~1.5 ns/printing
/// (`legality_compose_kernel_costs`, the broad formats) — the same kernel from two probes, so one
/// constant at the midpoint. Load-bearing: without it the model under-predicts these plans and mis-routes.
pub(crate) const SCATTER_PER_PRINTING_NS: f64 = 1.4;

// ─── P3: StreamedSelect ─────────────────────────────────────────────────────
// Match phase walks eval_domain cards computing per-card counts, then either
// walks the sort permutation to the page (broad) OR — when total <=
// STREAM_MIN_MATCHES — gathers via a `for cid in 0..n_cards` scan and
// quickselects (run_query_streamed, lib.rs). That small-total gather is the
// O(n_cards) FLOOR that makes P3 lose badly on narrow queries: a 5-row query
// forced onto P3 measured ~52µs = n_cards × ~1.65ns.

/// P3 match phase, split into a per-CANDIDATE-CARD term (`card_pass`, driven by
/// `eval_domain`) and a per-SCANNED-ROW term (`scan_units`, below). The old lumped
/// `STREAM_MATCH_PHASE_PER_CARD_NS = 3.0` was fit on CARD mode, where the loop
/// early-stops at the first matching printing (`scan_units ≈ eval_domain`) so the
/// two terms are indistinguishable; the sum stays 3.0 there. Printing/artwork scan
/// EVERY printing of each candidate (`scan_units ≈ eval_domain · n_printings/n_cards`),
/// which the lumped constant under-priced ~2× (fidelity 0.5, the eval_domain-counts-
/// cards bug). Split fit: card sum pins `CARD_PASS + SCAN = 3.0`; printing's ~2×
/// under-prediction at ratio ~3.09 pins the split (`CARD_PASS + 3.09·SCAN ≈ 6.0`).
const STREAM_CARD_PASS_NS: f64 = 1.56;
/// ns per printing scanned in the match phase (residual test per row). See
/// STREAM_CARD_PASS_NS for the split derivation. The residual verify `tier` rides
/// this term (it is paid per scanned row), common-mode with P4's scan term.
const STREAM_SCAN_PER_ROW_NS: f64 = 1.44;
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

/// P4 gathered loop, split per-CANDIDATE-CARD (`card_pass`, `eval_domain`) and
/// per-SCANNED-ROW (`scan_units`), same rationale as STREAM_CARD_PASS_NS. The old
/// lumped `GATHER_VISIT_PER_CARD_NS = 5.5` was fit on card mode (all-match broad,
/// eval_domain==matches, tier=0, sum ≈ 6.3-6.9 with GATHER_PUSH); card keeps
/// `CARD_PASS + SCAN = 5.5`. Printing's ~2× under-prediction at ratio ~3.09 splits
/// it (`CARD_PASS + 3.09·SCAN ≈ 11`).
const GATHER_CARD_PASS_NS: f64 = 2.87;
/// ns per printing scanned in the gathered loop (residual test per row); the verify
/// `tier` rides this term, common-mode with P3's scan term. See GATHER_CARD_PASS_NS.
const GATHER_SCAN_PER_ROW_NS: f64 = 2.63;
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
/// Only meaningful for plans that are *applicable* to the query (`run_query_routed`
/// only ever costs `PhysicalPlan::ALL.filter(applicable)`); an inapplicable plan's
/// cost is not defined.
pub(crate) fn plan_cost(plan: PhysicalPlan, f: &PlanFeatures) -> f64 {
    let n_cards = f64::from(f.n_cards);
    let n_printings = f64::from(f.n_printings);
    let matches = f64::from(f.matches);
    let eval_domain = f64::from(f.eval_domain);
    let scan_units = f64::from(f.scan_units);
    let tier_ns = f64::from(f.residual_tier_ns100) / 100.0;
    let limit = f64::from(f.limit);
    let page_span = f64::from((f.offset.saturating_add(f.limit)).min(f.matches));

    // Printings walked to fill the page in a forward-permutation walk (both printing-space plans):
    // roughly `page_span` rows at density `match_rate`.
    let match_rate = (matches / n_printings).max(MATCH_RATE_FLOOR);
    let printings_walked = page_span / match_rate;
    match plan {
        // #695 bare range, unique=printing: total is the range index's `k` (no synth, no popcount pass),
        // page is a forward permutation walk. So just the walk + fixed setup.
        PhysicalPlan::PrintingRangeScan => {
            printings_walked * RANGE_WALK_STEP_NS  // forward-perm walk to fill the page
                + RANGE_FIXED_COST_NS              // per-query setup
        }
        // #724 unified compose, any distinct-on. One term per operation it performs:
        PhysicalPlan::PrintingCompose => {
            f64::from(f.synth_printings) * SCATTER_PER_PRINTING_NS  // build the printing bitmap: legality broadcast-down / range scatter (border/rarity read a plane → 0)
                + f64::from(f.project_printings) * SCATTER_PER_PRINTING_NS  // second pass: project printing→card/artwork (0 for printing mode) — the pass CardRangePopcount fuses away
                + f64::from(f.popcount_words) * PLANE_POPCOUNT_PER_WORD_NS  // popcount the result-space bitmap for the total (printing/card/artwork words)
                + printings_walked * RANGE_WALK_STEP_NS  // forward grouped walk to fill the page
                + limit * PLANE_POPCOUNT_EMIT_PER_CARD_NS  // emit one page of rows
                + RANGE_FIXED_COST_NS  // per-query setup
        }
        // #634 plane popcount-skip order walk (precomputed bitmap ⇒ no synth):
        PhysicalPlan::PlanePopcountOrder => {
            matches * PLANE_POPCOUNT_SCATTER_PER_MATCH_NS  // scatter matches through the inverse permutation
                + (n_cards / 64.0) * PLANE_POPCOUNT_PER_WORD_NS  // popcount the card bitmap + skip-scan to the offset
                + limit * PLANE_POPCOUNT_EMIT_PER_CARD_NS  // emit one page of cards
                + PLANE_POPCOUNT_FIXED_COST_NS  // per-query setup
        }
        // #725 bare range, unique=card: PlanePopcountOrder's popcount-skip walk over a card bitmap
        // *built at query time* from the range slice — same walk terms, plus the build synth.
        PhysicalPlan::CardRangePopcount => {
            f64::from(f.synth_printings) * SCATTER_PER_PRINTING_NS  // build the card-existence bitmap from the range slice
                + matches * PLANE_POPCOUNT_SCATTER_PER_MATCH_NS  // scatter matches through the inverse permutation
                + (n_cards / 64.0) * PLANE_POPCOUNT_PER_WORD_NS  // popcount the card bitmap + skip-scan to the offset
                + limit * PLANE_POPCOUNT_EMIT_PER_CARD_NS  // emit one page of cards
                + PLANE_POPCOUNT_FIXED_COST_NS  // per-query setup
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
            // card_pass is per candidate card; the residual scan + its verify tier
            // are per scanned printing (scan_units) — the operating-space split.
            eval_domain * STREAM_CARD_PASS_NS
                + scan_units * (STREAM_SCAN_PER_ROW_NS + tier_ns)
                + matches * STREAM_EMIT_PER_MATCH_NS
                + floor
                + STREAM_FIXED_COST_NS
        }
        PhysicalPlan::GatheredScan => {
            eval_domain * GATHER_CARD_PASS_NS
                + scan_units * (GATHER_SCAN_PER_ROW_NS + tier_ns)
                + matches * GATHER_PUSH_PER_MATCH_NS
                + page_span * GATHER_SELECT_PER_PAGE_SLOT_NS
                + GATHER_FIXED_COST_NS
        }
    }
}
