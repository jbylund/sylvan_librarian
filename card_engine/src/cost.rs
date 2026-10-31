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
//! the per-row residual scan + its verify `tier` by `scan_units` (printings under the
//! candidate cards). One mode-agnostic formula, and `scan_units()` no longer branches
//! on mode at all. The `_CARD_PASS`/`_SCAN` split of the old lumped
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
//!
//! ## What the span counter could and could not show (2026-08-02)
//!
//! The paragraph above used to justify the mode-agnostic `scan_units` by asserting that "the
//! `printings_scanned` counter shows the scan plans walk the full printing span of their candidates in
//! CARD mode too, not one row each". That inference does not hold, and the sentence is now deleted
//! rather than merely qualified: the counter (since renamed `printing_span`) was computed by the
//! CALLER as `end - start` per surviving card, before the match kernel ran, so it reported the span
//! whatever the kernel then did. It could not distinguish the two cases it was being cited for.
//!
//! The kernels do stop early in card mode. `card_match_count` returns from `all_match` without reading
//! a printing at all, and both it and `push_card_matches` return at the FIRST qualifying printing
//! under `Prefer::Default`. `printings_examined`, reported by the kernels themselves, is the quantity
//! `scan_units` predicts, and grading against it is what `bench_feature_accuracy.py` now does.
//!
//! The mode-agnostic span estimate nonetheless survives that correction, for a reason the original
//! note did not give: under an INEXACT narrowing most candidate cards do not match, and a
//! non-matching card is ruled out only after its whole span is walked. So the span is a good proxy in
//! card mode generally (`scan_units [candidates] / card` grades p50 1.00 against `printings_examined`)
//! and wrong specifically where the narrowing is EXACT — the plane acquire, where `all_match` holds
//! and `prefer` then decides everything. That case is handled in `acquire_plan_features`.

use super::*;

/// Cheap, per-query features the cost model consumes, built once per query by
/// `run_query_routed`'s `acquire` step. All counts are exact or cheap-exact (plane
/// popcount / range `k` / candidate count), never estimated.
#[derive(Clone)]
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
    /// Printings under the candidate cards — the dominant P3/P4 driver, and the same
    /// quantity in all three distinct-ons.
    ///
    /// Card mode DOES break at the first matching printing, so the `0.25-0.33` this note used to cite
    /// against `printings_scanned` was the counter's artifact, not the feature's error — see the module
    /// header. Graded against `printings_examined` the span estimate reads p50 1.00 in card mode
    /// anyway, because an inexact narrowing makes non-matching candidates walk their whole span.
    ///
    /// `prefer` is the one thing this cannot see: it decides whether the kernels may break early at
    /// all, and `PlanFeatures` does not carry it. That is invisible under an inexact narrowing (the
    /// non-matching cards dominate either way) and decisive under an exact one, which is why the plane
    /// branch of `acquire_plan_features` sets this field itself rather than taking the span estimate.
    pub scan_units: u32,
    /// `scan_units` for `StreamedSelect` specifically, where that plan examines a DIFFERENT number of
    /// printings from `GatheredScan` on the same query. One per-query field cannot serve both: P4's
    /// `push_card_matches` must walk a card's span to push every match, while P3's `card_match_count`
    /// answers from span arithmetic for every card `card_pass` resolves outright.
    ///
    /// Measured on the compose acquire, `scan_units` against the realized `printings_examined`:
    ///
    ///     f:modern / artwork      GatheredScan 101,716 / 73,783 = 1.38    StreamedSelect 101,716 / 7,770 = 13.09
    ///     f:gladiator / artwork    88,026 / 54,213 = 1.62                  88,026 /  5,876 = 14.98
    ///
    /// Right for P4 to within 1.4-1.6x, wrong for P3 by 13-15x, and `scan_units * STREAM_SCAN_PER_ROW_NS`
    /// is then 525 us of P3's 704 us prediction against a 91 us measured loop. That is a FEATURE error, the
    /// one class no rate can absorb — unlike the residual floor, whose kernel-vs-traffic gap turned out to
    /// be a cache artifact.
    ///
    /// Set by the acquire branch that knows the difference; `mk_plan_feats` defaults it to `scan_units`, so
    /// a branch that has not been taught reads exactly as before.
    pub stream_scan_units: u32,
    /// Diagnostic: the residual compares only CARD-level fields, so `card_pass` answers `True`/`False`
    /// per card and never `PrintingDep`. A matching candidate then contributes its whole printing span and
    /// a non-matching one none of it, which is a different estimator shape from one where printings under a
    /// single card disagree — and the latter is what `RESIDUAL_PASS_RATE_PRINTING`/`_ARTWORK` was fitted on.
    /// Exposed so `matches`'s error can be split by population before any rate is touched. **Nothing in
    /// `plan_cost` reads this.**
    pub residual_card_invariant: bool,
    /// Per-card verify cost of the residual, ns×100 (`verify_cost_tier`); `0`
    /// when `all_match_known` (the walk skips `card_pass` entirely).
    pub residual_tier_ns100: u32,
    /// Cards `run_query_streamed` visits in ARTWORK mode, i.e. `eval_domain` there and `0` in card and
    /// printing mode. Charged at `STREAM_ARTWORK_SEEN_PER_CARD_NS`.
    pub artwork_seen_cards: u32,
    /// Printings `exec_gathered_scan` visits in ARTWORK mode, i.e. `scan_units` there and `0` in card
    /// and printing mode. `push_card_matches`'s `Mode::Artwork` branch does a `group_best`/`touched`
    /// dedupe check on every printing in a candidate's span (`Mode::Printing` does not), so unlike
    /// `artwork_seen_cards` this rides `scan_units` (a printing count) rather than `eval_domain` (a
    /// card count) -- the two plans' artwork overhead differ in SHAPE, not just rate, because
    /// `run_query_streamed` dedupes with a fixed per-card bitmask while this loop dedupes per
    /// printing. Charged at `GATHER_ARTWORK_PER_PRINTING_NS`.
    pub artwork_seen_printings: u32,
    /// Printings compose's **Gather** paging branch bit-tests, which is NOT `scan_units`.
    ///
    /// `scan_units` is every printing under a candidate card — right for GatheredScan and
    /// StreamedSelect, which must test each one. Compose walks the set bits of the composed bitmap
    /// instead, so it touches `printing_matches`. Measured against `printing_span`, compose reads
    /// 1.00 on `matches` in printing mode and 1.00-1.01 on `project_printings` in artwork (the same
    /// value), while `scan_units` reads 2.0-2.8 for it.
    ///
    /// Sharing one feature between the two forced a compromise that was ~2x wrong for whichever arm
    /// lost: with the value GatheredScan needs, compose over-counts 2x; with compose's, the scan plans
    /// under-count 3.3x.
    pub compose_scan_printings: u32,
    /// Page size (`limit`).
    pub limit: u32,
    /// Page offset.
    pub offset: u32,
    /// Printings the legality **broadcast-down** synthesizes (card ∃-plane → printing bitmap) in
    /// `PrintingCompose`. `0` for border/rarity (precomputed planes) and for bare ranges (no broadcast).
    /// Costed at `LINEAR_PASS_PER_PRINTING_NS`.
    pub broadcast_printings: u32,
    /// The range index's in-range slice `k` — the printings a range leaf contributes. Charged at a
    /// DIFFERENT rate per plan (same `k`, different physical op): `PrintingCompose` scatters it into a
    /// printing bitmap (`RANGE_SCATTER_PER_PRINTING_NS`, cheap, then a separate `project_printings` pass),
    /// while `CardRangePopcount` fuses scatter+project in one pass (`CARD_RANGE_BUILD_PER_PRINTING_NS`).
    /// Set by both range-plan acquire branches so the shared feats cost either winner honestly — the
    /// fused op being cheaper than compose's two passes is why a bare range routes to CardRangePopcount.
    pub scatter_printings: u32,
    /// Printings scattered in `PrintingCompose`'s **projection pass** — printing bitmap → card/artwork
    /// existence, a second O(set) pass on top of the build. `0` for printing mode (no projection) and
    /// for non-compose plans. Costed at `LINEAR_PASS_PER_PRINTING_NS`.
    pub project_printings: u32,
    /// 64-bit words of the **result-space** bitmap the total popcount + skip-scan touches — the field
    /// that keeps the popcount term honest across distinct-ons: `n_printings/64` (printing),
    /// `n_cards/64` (card), `n_artworks/64` (artwork). Set by `PrintingCompose`; `0` elsewhere.
    pub popcount_words: u32,
    /// Set printings `gather_composed_page`'s GROUPING arm processes, or `0` when that arm is not the
    /// one that runs.
    ///
    /// The Gather arm charged three things: a per-candidate-card pass, a per-printing bit test at
    /// 0.38ns ("a cheap bit test, not a real residual scan"), and a per-match push. That describes
    /// the printing-mode arm, which really does just test a bit and push. It does not describe the
    /// `Mode::Card | Mode::Artwork` arm, which for every SET printing also reads the artwork group id,
    /// computes `prefer_score`, and compares against `group_best` — real work, none of it a bit test.
    ///
    /// The unit is the load-bearing part. `matches` in artwork mode is the DEDUPED artwork count, so
    /// `COMPOSE_GATHER_PUSH_PER_MATCH_NS` charges once per surviving group, while the grouping work
    /// scales with the PRE-dedup printing matches feeding it. A card with twelve printings across two
    /// artworks pays twelve groupings and two pushes. That is the same class of error
    /// `bench_feature_accuracy` was written for, mirrored: there a printing count drove a per-result
    /// term, here a deduped count drives a term whose work is pre-dedup.
    ///
    /// Measured consequence before this term existed: over 129 queries where compose was picked and
    /// lost to GatheredScan, compose's real/predicted was **3.07** — 89% of the lost time in artwork,
    /// 94% on the Gather branch, and every one of the five worst a single bare `f:` legality leaf at
    /// ~300us. `PrintingCompose -> GatheredScan` is 99% miss and 11% of ALL routing regret.
    ///
    /// `0` for printing mode (the push term already covers its per-set-printing work, since `matches`
    /// there IS the printing count) and for card mode under `Prefer::Default`, which takes the
    /// early-break arm instead and never groups.
    pub gather_group_printings: u32,
    /// Which of `PrintingCompose`'s three paging strategies will actually run (see `ComposePaging`),
    /// decided the same way `printing_compose_fastpath` decides. The three have different cost shapes
    /// — the permutation walk and the #744 orderby-index walk are both offset-dependent (fill the page
    /// in ~`page_span/selectivity` steps), while the permutation-free gather visits every match — so
    /// the formula branches on this rather than assuming one. Ignored by every other plan.
    pub compose_paging: super::ComposePaging,
    /// Printings a CARD-SPACE collection leaf's build broadcasts (`ids_of` +
    /// `broadcast_card_ids_to_printings`). See `ComposeEstimate::collection_broadcast`'s doc for why
    /// this is not just folded into `scatter_printings`. `0` for everything except `PrintingCompose`
    /// on a card-space `subtypes`/`keywords`/`oracle_tags` leaf.
    pub collection_broadcast_printings: u32,
}

// ─── P1: PrintingRangeScan ──────────────────────────────────────────────────
// A bare broad range predicate under unique=printing: total from the range
// index's binary search, page from an early-stopping permutation walk. Cost is
// dominated by how far the walk must go to fill the page, which is
// (offset+limit) matches at density `match_rate` printings.

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



/// Per-printing cost of `CardRangePopcount`'s **fused build** — `build_card_range_bits` sets the printing
/// bit AND the card bit (via `printing_to_card`) in one pass over the range slice, fusing compose's
/// scatter+project (0.4 + 1.5 = 1.9) into a single ~1.2 ns/printing pass (`card_range_build_cost_split`'s
/// C 98333ns / 80527 = 1.22). Carries `scatter_printings` in CardRangePopcount's arm — the same `k` as
/// compose's scatter but a cheaper op, which is exactly why a bare range routes here, not to compose.
///
/// Retuned 1.22 -> 0.93 from END-TO-END measurement, which disagrees with that kernel figure. The arm
/// over-costed by a near-uniform 1.20 (p10 0.99, p50 1.20, p90 1.43 — a spread of only 1.4, the
/// signature of a plain rate error), and this term is 80% of it. Its four other constants are shared
/// with PlanePopcountOrder, which is slightly UNDER-costed at 0.92, so they cannot absorb it.
///
/// The disagreement is real, not a sampling artifact, and was checked for exactly that: the implied
/// end-to-end rate is FLAT in k — 0.97 / 0.81 / 0.91 / 0.92 / 0.99 across k bins from 1.5k to 81k — so
/// no single-k distribution is doing the work. At k≈81,479, the same slice size the kernel benchmark
/// uses, end-to-end still implies 0.99 against its 1.26. The kernel times the build in isolation;
/// `plan_cost` predicts end-to-end time, so end-to-end is the figure it should carry. Re-running
/// `card_range_build_cost_split` today still reports 1.26 (101500/80527), so the kernel has not drifted
/// — the two simply measure different things.
pub(crate) const CARD_RANGE_BUILD_PER_PRINTING_NS: f64 = 0.93;

// ─── Candidate materialization ──────────────────────────────────────────────
// `plan_cost` prices only what happens AFTER the acquire step: `eval_domain` and `matches` are its
// inputs, not its outputs. The acquire step is therefore unpriced, and it is where the model's error
// lives — over 40 sampled queries the median `(measured - predicted) / acquire_ns` is 1.09, so
// adding the MEASURED acquire time roughly closes the gap.
//
// `materialize_cost` below is NOT that term, and does not close that gap. It prices one component of
// acquire — the candidate `collect` + `sort_unstable` — which measurement puts at a median 5% of
// acquire (quartiles 2%–40%, n=167 candidate-acquired queries): the rest is index walks, the
// narrowing recursion, `memoize_text_predicates` and feature building. Adding it to `predicted_ns`
// measurably makes absolute accuracy slightly WORSE (73.1% -> 74.9% mean error), because it is a
// small piece of a large omission and does not change which plans compare equal.
//
// So it is REPORTED BY `explain`, NOT ADDED TO `plan_cost`, for three reasons: it is validated as
// too small to help; it is identical for the plans that actually compete (`StreamedSelect` and
// `GatheredScan` call the same `prepare_candidates`), so it cannot change an argmin; and its real
// purpose is to price the bitmap-versus-sort question in
// docs/issues/done/local-engine-candidate-materialize.md, which is what it does measure exactly.

/// `Vec::with_capacity` plus the run walk, before any comparison work
/// (`bench_candidate_materialize`, axis A).
pub(crate) const MATERIALIZE_SORT_FIXED_NS: f64 = 143.0;
/// pdqsort on `u32`, per candidate — **linear**, not `c·log2 c`. `sort_unstable` is a full pdqsort
/// so it is asymptotically `n log n`, but measured per-element cost is flat across the sizes this
/// engine sees (4.39 ns at 1,024 rising only to 5.09 at 31,508, where an `n log n` fit predicts
/// 4.39 → 6.57). Fit on the rows bracketing the crossover. Re-fit rather than extrapolating past
/// ~3M cards, where the log factor does start to show.
pub(crate) const MATERIALIZE_SORT_PER_CAND_NS: f64 = 4.95;

/// Modelled cost of producing the candidate list a materializing plan consumes, in ns. `0.0` for
/// plans that never build one — those walk or read a plane bitmap directly, and charging them this
/// would invert exactly the comparison the term is meant to inform.
///
/// Prices today's behavior: a `collect` + `sort_unstable`. It does **not** model the bitmap
/// alternative that doc proposes; if that ships, this needs the domain term as well.
///
/// The match below is deliberately NOT `PhysicalPlan::materializing()`, which means something
/// else — "runnable off a materialized prep", and so includes `PlanePopcountOrder`, which reads
/// the plane bitmap directly and builds no candidate list. Charging it here would invert exactly
/// the plane-against-materializing comparison this term exists to inform.
pub(crate) fn materialize_cost(plan: PhysicalPlan, f: &PlanFeatures) -> f64 {
    match plan {
        PhysicalPlan::StreamedSelect | PhysicalPlan::GatheredScan => {
            MATERIALIZE_SORT_FIXED_NS + MATERIALIZE_SORT_PER_CAND_NS * f64::from(f.eval_domain)
        }
        PhysicalPlan::PrintingRangeScan
        | PhysicalPlan::PrintingCompose
        | PhysicalPlan::PlanePopcountOrder
        | PhysicalPlan::CardRangePopcount => 0.0,
    }
}

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
/// Refit 2026-07-30 by `scripts/fit_cost_model.py` — non-negative Gauss-Newton on the LOG ratio
/// (symmetric in over/under, unlike a relative-error fit, which shrinks every rate toward zero),
/// ridge-anchored to the previous values because several columns barely vary on this corpus and
/// are collinear with the intercept. Fitted on ~10k distinct feature vectors, stable to <3% across
/// independent seeds. Median measured/predicted moved 1.78 -> 1.00 (P4) and 1.69 -> 1.06 (P3).
///
/// Refit again 2026-08-02, and this is the first fit of P3 that was allowed to happen: every earlier
/// run of `fit_cost_model.py` REFUSED to fit this plan, because its `counter_check` graded
/// `scan_units` against the printing SPAN, and P3's `all_match` rows disagree with the span by ~3x
/// over a term this arm multiplies by zero (`if tier_ns > 0.0`). With `printings_examined` reported
/// by the match kernels and the zero-weight rows excluded, all six counter checks read 1.00 and the
/// fit ran: median predicted/measured 0.63 -> 0.92 and within-25% 19% -> 58% over 29,084 rows /
/// 9,867 distinct shapes. P3 had been under-costed ~1.6x, which biases the router toward picking it.
/// P4 and compose were re-fit in the same run and NOT changed: P4's fitted values reproduce these
/// within 8% with no agreement gain, and compose's fit makes its median worse (0.95 -> 0.86).
/// **Split 2026-08-03**, and the level below is now the CALL only. `bench_streamed_loop` measures P3's
/// loop at 2.58 ns/card on the `all_match` path and 5.08 with a residual, on the same cells at the same
/// corpus size — so ~2.5 of the shipped 5.05 was the `card_pass` call, charged to every candidate whether
/// the call happened or not. The sum is preserved (2.58 + 2.47 = 5.05), so a residual-bearing query is
/// costed exactly as before and only the `all_match` path gets cheaper.
const STREAM_CARD_PASS_NS: f64 = 2.47;
/// P3's loop body per candidate card, paid whether or not a residual exists: the `counts` write, the
/// offsets arithmetic, and the match-count call that answers from the span alone under `all_match`.
/// 2.58 ns/card measured by `bench_streamed_loop`, and the one rate in either loop that is FLAT across a
/// 13× corpus (2.58 / 2.54 / 2.55) — it reads the card record and nothing else, so it has no misses to
/// gain. That flatness is why it is the half worth stating as a constant.
const STREAM_LOOP_PER_CARD_NS: f64 = 2.58;

/// P3's per-scanned-row cost, charged ONLY when a residual is present.
///
/// Unlike P4, P3 merely COUNTS matches, and `card_match_count` is O(1) offset arithmetic whenever
/// `all_match` holds (the artwork-group count is a build-time constant). So with no residual it does
/// no per-printing work at all — but with one it must walk the printings testing each. Regressing
/// per-card match-loop time on printings-scanned-per-card, split by tier, shows exactly that split:
///
/// | residual | slope (ns per printing/card) |
/// |----------|-----------------------------:|
/// | none (all_match) | **0.02** |
/// | MASK_COMPARE | 2.83 |
/// | SET_LOOKUP | 2.19 |
/// | TEXT_SCAN | 1.90 |
///
/// An earlier revision removed this term outright, on the O(1) argument above. That argument is
/// right for the `all_match` half and wrong for the other, and an ungated fit drove the rate to 0
/// because the tier-0 rows (the majority) dominated it. Gating on the residual separates them.
///
/// This one is NOT common-mode with P4 — P4 pays `GATHER_SCAN_PER_ROW_NS` unconditionally — so
/// unlike the verify tier it can move the argmin between the two.
const STREAM_SCAN_PER_ROW_NS: f64 = 5.97;


/// Per-card cost of RUNNING `card_pass` at all, on top of whatever the residual's own nodes cost.
/// The tri walk has to set up, populate the reused `residual` vec, branch on the `Tri`, and drive
/// the per-printing loop; none of that is a filter node, so `verify_cost_tier` does not describe it
/// and should not be asked to.
///
/// This replaces a multiplicative `*_VERIFY_TIER_SCALE` of 2.87/2.65. The multiplier was wrong in
/// form, not just in value. `bench_verify_cost` (cargo test --release bench_verify_cost -- --ignored)
/// times the real `FilterExpr::matches()` path per node and VALIDATES the tier constants:
/// MASK_COMPARE claims 4.0 ns against 2.08-3.60 measured, SET_LOOKUP 9.0 against 2.12-8.69,
/// TEXT_SCAN 23.0 against 21.5, REGEX_MACHINERY 50 against 45.8-47.5. They are right, slightly
/// conservative. So there was no per-node calibration error for a multiplier to correct — there was
/// an unmodelled fixed per-card overhead, and scaling by 2.7x happened to approximate it for the
/// common cheap tiers while badly over-costing text (2.7 x 23 = 62 ns against a real 23 + 17).
///
/// It is a FLOOR, not an offset: `max(tier_ns, this)`. Cheap residuals are dominated by the walk,
/// expensive ones dominate it, and the two do not add. Measured by regressing per-card match-loop
/// time on printings-scanned-per-card, separately per tier class, over the real query population
/// (the earlier additive fit used single-predicate queries, whose tiny sample disagreed):
///
/// | tier | claims | P3 excess over no-residual | P4 excess |
/// |------|-------:|---------------------------:|----------:|
/// | MASK_COMPARE | 4 ns | 8.9 | 15.4 |
/// | SET_LOOKUP | 9 ns | 10.4 | 11.2 |
/// | TEXT_SCAN | 23 ns | 20.7 | 19.9 |
///
/// The excess is roughly INDEPENDENT of the tier for the cheap classes and non-monotonic across them
/// (P4's MASK excess exceeds its SET_LOOKUP excess), which additive cannot produce. `max` fits all
/// three within ~2 ns for P3 and ~4 for P4, where `tier + 11` over-costs TEXT_SCAN by 14 ns — the
/// text-heavy over-costing that showed up as GatheredScan/candidates at 0.67.
///
/// That same regression also shows the residual's cost is per CARD, not per printing scanned: the
/// SLOPE against printings-per-card is ~3.5 for every tier including none (P4) and ~2 for P3, i.e.
/// independent of what the residual is. So the tier belongs on `eval_domain`, as it now sits.
/// **Measured with a built design 2026-08-04, and NOT changed — the useful part is why.** The floor only
/// ever binds on `MASK_COMPARE` (tier 4.00); every other tier exceeds 6.58 and `max` takes the tier. So
/// `bench_streamed_loop`'s always-true `DateCmp` cells are exactly the population it governs, and they say
/// the residual's per-card cost is 2.45 ns (printing/residual 4.97 less printing/all_match 2.52) against a
/// charged `CARD_PASS + floor` of 9.05 — i.e. the `card_pass` call IS the whole cost and the mask compare
/// adds nothing measurable.
///
/// Traffic disagrees, and traffic wins on levels: the fitted `CARD_PASS+FLOOR` column reads **8.19 against
/// the shipped 9.05 (0.90)** for this arm and **21.59 against 21.89 (0.99)** for `GatheredScan`. Both
/// floors are already right to within 10% and 1%.
///
/// That is the third time this file has caught the same artifact. The design measures an always-true
/// predicate over chunk-rotated slices; production runs real residuals over a warmer archive, and the two
/// differ by 3.3x here exactly as they differed by 1.6-2.2x in the retraction at the top of
/// `bench_streamed_loop`. Shape from a built design, levels from traffic — the design's contribution is
/// the SHAPE finding that the tier adds ~0 over the call for the cheapest class, which is worth knowing
/// and is not a licence to move the level.
const STREAM_RESIDUAL_FLOOR_NS: f64 = 6.58;
/// ns per match, for the permutation-walk emit. Small — P3 measured nearly flat
/// in match count once eval_domain is fixed (see STREAM_MATCH_PHASE_PER_CARD_NS),
/// so this is a minor term.
const STREAM_EMIT_PER_MATCH_NS: f64 = 0.12;
/// ns per candidate card that ARTWORK mode pays and the other two do not.
///
/// `card_match_count`'s artwork arm keeps a per-card `seen_words` bitmask to dedupe artwork groups,
/// and that costs a fixed amount per card regardless of how many printings the card has: a
/// `seen_words.fill(0)` before the loop and a `seen_words.iter().map(count_ones).sum()` after it,
/// over `ARTWORK_GROUP_WORDS = 8` u64s. Card mode returns on the first matching printing and printing
/// mode just counts, so neither pays it. (The `all_match` + `have_group_counts` shortcut in
/// `run_query_streamed` skips the helper entirely, which is why this lands on residual-bearing
/// candidates in particular.)
///
/// Fitting the arm separately per distinct-on is what surfaced it, over three seeds:
///
///     StreamedSelect    artwork / printing        seed21      seed22      seed23
///     CARD_PASS                              5.92/3.36   5.76/3.20   5.89/3.17
///     RESIDUAL_FLOOR                        11.75/9.21  11.81/9.39  11.86/9.81
///
/// Two independently fitted per-card terms, both elevated in artwork by ~2.4 ns and never flipping
/// sign. One mechanism showing up twice, so it belongs in its own term rather than as a mode-specific
/// copy of each rate. ~16 word-ops per card is the right order for 2.4 ns.
const STREAM_ARTWORK_SEEN_PER_CARD_NS: f64 = 1.21;
/// ns per printing scanned, for `GatheredScan`'s ARTWORK-mode dedupe check
/// (`push_card_matches`'s `Mode::Artwork` branch: read `artwork_group_col[pid]`, check
/// `group_best[gid].is_some()`, on every printing in the candidate's span). Unlike
/// `STREAM_ARTWORK_SEEN_PER_CARD_NS` this could not be fit from an end-to-end query A/B: the two
/// modes' `matches_pushed` differ sharply (printing pushes every printing, artwork only distinct
/// groups), so `GATHER_PUSH_PER_MATCH_NS`'s much larger swing dominated the raw delta and even its
/// sign, and a pooled regression over natural queries hit multicollinearity between
/// cards_visited/printing_span/matches_pushed and came back negative.
///
/// Fit from a kernel test instead (`gather_artwork_kernel_costs`): `push_card_matches` called
/// directly over the same 5,000-card real-corpus slice with `all_match: true` (no residual
/// evaluation cost), `Mode::Printing` vs `Mode::Artwork`, min-of-150 rounds. Four runs (two repeats,
/// one on a different 5,000-card slice): 0.489, 0.528, 0.508, 0.489 ns/printing.
const GATHER_ARTWORK_PER_PRINTING_NS: f64 = 0.50;
/// ns per card scanned in the small-total gather (`for cid in 0..n_cards`,
/// counts[cid]==0 check). Cheaper than a match-phase visit (no filter work). Fit
/// from the narrow-query floor: cmc>=15 / o:annihilator / cmc==7 card SHALLOW all
/// ~52µs = 31508 × 1.65. Only added when `matches <= STREAM_MIN_MATCHES`, the
/// exact condition that routes P3 into that gather branch. The 1.65 above was fit on three hand
/// picked narrow queries; across the sampled space the floor measures ~31µs, not 52µs.
///
/// CONFIRMED 2026-08-03 by `bench_streamed_loop`, which needed 600-card cells to reach this branch at
/// all -- every earlier cell had more than `STREAM_MIN_MATCHES` matches and took the permutation walk
/// instead, so this constant had never been measured against the branch it prices. It now reads
/// 1.075-1.250 ns per card scanned against the 1.02 shipped, on a 33.9 us finish phase. Left alone.
const STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS: f64 = 1.02;
/// ns per permutation entry stepped in the streaming walk, the branch taken when
/// `total > STREAM_MIN_MATCHES`.
///
/// The walk covers its sort-column segment until the page fills, so it visits about
/// `page_span * n_cards / matches` entries -- inversely proportional to selectivity, and the one
/// quantity in P3's finish phase that no other feature is proportional to. Nothing charged for it
/// before: the arm had `matches * EMIT + FIXED`, which is flat in `n_cards`, so at a fixed 1,500
/// matches it predicted ~397 ns while `bench_streamed_loop` measured 1,333 / 3,791 / 10,458 ns at
/// 31.5k / 126k / 410k cards. Under by 3.4x at the production corpus and 26x at 410k, which is why
/// that phase graded mean |log| 2.06 while carrying 12% of all measured nanoseconds.
///
/// Measured 0.958-1.256 ns/entry on the cells where the walk is long (1,500 matches, ~1,260 estimated
/// steps): 1.0 is the middle of that. Cells whose walk is SHORT read 2.0-4.2 ns/entry, but those are a
/// ~167-step estimate against a few hundred ns, where a fixed cost dominates and a per-step rate
/// overstates -- the long-walk regime is the one worth fitting, being where the term is large enough to
/// change a routing decision.
///
/// The estimate is graded against the realized `perm_steps` counter rather than trusted, the same way
/// `scan_units` is graded against `printings_examined`. **The rate survived the walk being bounded to
/// its sort-column segment**, which is the useful thing that grade has said so far: traffic fit 1.17
/// before and 1.19 after, on the same seed and sample length, while realized steps at p90 fell from
/// 6.43x the estimate to 5.31x (and to 4.26x under the unshipped realized-span variant). A change that
/// deletes a fifth of the steps at the tail while moving the per-step rate by 2% is the signature of a
/// rate that is a real per-unit cost rather than a sink for the count's error.
const STREAM_PERM_STEP_NS: f64 = 1.0;
/// Per-card cost P3 pays over the WHOLE corpus regardless of how narrow the query is, charged on
/// `n_cards` rather than `eval_domain`. The thread-local counts buffer is resized and cleared to
/// `cards.len()` every query — a 126 kB memset on this corpus — and the emission walk is over the
/// corpus-sized sort permutation. Fit lands at ~9 µs total, more than a memset alone accounts for,
/// so this lumps the two; a single corpus cannot separate them (both are exactly `n_cards`). Kept
/// as a per-card RATE rather than the previous flat constant so it tracks corpus size at all.
const STREAM_CORPUS_PASS_PER_CARD_NS: f64 = 0.02;
/// Fixed P3 setup, net of the O(n_cards) work above.
const STREAM_FIXED_COST_NS: f64 = 217.0;

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
/// Refit 2026-07-30 by `scripts/fit_cost_model.py` — non-negative Gauss-Newton on the LOG ratio
/// (symmetric in over/under, unlike a relative-error fit, which shrinks every rate toward zero),
/// ridge-anchored to the previous values because several columns barely vary on this corpus and
/// are collinear with the intercept. Fitted on ~10k distinct feature vectors, stable to <3% across
/// independent seeds. Median measured/predicted moved 1.78 -> 1.00 (P4) and 1.69 -> 1.06 (P3).
/// **Split 2026-08-03**, and the level below is now the CALL only. The module header's own reading of
/// `bench_gather_loop` said this constant "BUNDLES the predicate call": ~3.2 ns/card of loop overhead
/// plus 2.94-3.00 for the `card_pass` call itself on singletons, summing to ~6.2 against the shipped
/// 6.88. It was therefore "about right for queries that make that call, and about 2x too high for the
/// #634 `all_match_known` path, which skips `card_pass` entirely and is charged for it anyway — a
/// model-shape error rather than a mis-fitted constant". This is that error, fixed where it lives.
///
/// The sum is preserved (3.88 + 3.00 = 6.88), so residual-bearing queries are costed as before.
const GATHER_CARD_PASS_NS: f64 = 3.00;
/// P4's loop body per candidate card, paid whether or not a residual exists. 3.88 = the shipped 6.88
/// less the 3.00 call above, rather than the kernel's 3.15-3.33 directly: the kernel figure is a warm
/// rate (see the retraction in `bench_gather_loop`'s header) and holding the SUM at the shipped value
/// keeps this change a pure re-gating, with no level moving on the queries that were costed correctly.
const GATHER_LOOP_PER_CARD_NS: f64 = 3.88;
/// ns per printing scanned in the gathered loop (residual test per row). The verify `tier`
/// does NOT ride this term; see GATHER_VERIFY_TIER_SCALE and STREAM_SCAN_PER_ROW_NS.
const GATHER_SCAN_PER_ROW_NS: f64 = 2.06;

/// P4's counterpart to STREAM_RESIDUAL_FLOOR_NS — see there for the form and its derivation.
const GATHER_RESIDUAL_FLOOR_NS: f64 = 18.89;
/// ns per match pushed into the sort-key Vec + quickselected.
const GATHER_PUSH_PER_MATCH_NS: f64 = 2.24;
/// ns per page slot materialized. Fit from the deep-vs-shallow gap on broad
/// queries (cmc>=0 card: 225708−216667 ≈ 9041ns over 10000 extra offset ≈ 0.9),
/// bounded by matches: narrow deep pages (offset > matches) measured ≈ shallow
/// (select_page returns early), so the term uses min(offset+limit, matches).
const GATHER_SELECT_PER_PAGE_SLOT_NS: f64 = 3.51;
/// ns per row actually collected into the page — `page_ids.into_iter().map(..)`, two random array
/// derefs per row into `cards` and `printings`.
///
/// A SECOND driver for this phase, found by `bench_gather_loop`'s page sweep 2026-08-03. The phase was
/// charged on `page_span` alone, which the sweep falsifies directly: at identical candidates,
/// `page_span` 960 (offset 900, limit 60) costs 11,250 ns while `page_span` 600 (offset 0, limit 600)
/// costs 16,375. A bigger span costing less is impossible under one column. The quickselect scales with
/// `offset + limit`, but the collect scales with the PAGE, and the two rows separate them because one
/// pairs a large span with a small page.
///
/// Traffic cannot separate them -- span and page are correlated across the sampled query mix, which is
/// why an earlier non-negative fit put this at exactly 0.00. Four designed rows do it. Same lesson as
/// the loop's three collinear counters: shape from a built design, level from traffic.
///
/// The count is what `select_page` returns, `clamp(matches - offset, 0, limit)`, not `limit`: a page
/// past the end of the matches collects fewer rows than asked for, and charging `limit` there would
/// bill a deep page on a narrow query for rows that do not exist.
/// Level from traffic, not from the sweep. The page sweep put this near 15 ns/row, but a traffic fit
/// with the column present reads 9.79, and traffic is what the routing surface is calibrated against --
/// kernel LEVELS have not transferred in this branch (first warm cache, then an unexplained 1.6x on
/// P3's per-card rate), while kernel SHAPE has been reliable. Adding the column did not disturb
/// `GATHER_SELECT_PER_PAGE_SLOT_NS`, which refits 3.44 against a shipped 3.51 -- so the two are
/// additive in the sampled mix rather than trading off, and only this one moves.
const GATHER_COLLECT_PER_PAGE_ROW_NS: f64 = 9.79;
/// Fixed P4 setup. Fit from the narrowest query (cmc>=15 card shallow 208ns at
/// eval_domain=5: 208 − 5×(GATHER_VISIT_PER_CARD_NS+GATHER_PUSH_PER_MATCH_NS) −
/// 5×GATHER_SELECT_PER_PAGE_SLOT_NS ≈ 170).
///
/// NOT refit 2026-08-03, deliberately. A whole-arm traffic fit puts this at 85, half the shipped value,
/// on a model whose every other term sits at 0.85-1.22 -- and an intercept that far out on an otherwise
/// agreeing model is a symptom, not a measurement. `fit_cost_model.py` fits ONE equation per query
/// against total dispatch, so its intercept absorbs whatever the other columns cannot express; it read
/// 84 and then 85 while `GATHER_COLLECT_PER_PAGE_ROW_NS` moved 15.0 -> 9.79 underneath it.
///
/// Measuring the intercept directly says the same thing more sharply. `bench_gather_loop` solves it from
/// cells differing ONLY in card count, where nothing else can hide, and gets card -1,084 ns and printing
/// -845 ns. A negative fixed cost is impossible, so the linear-in-cards shape is wrong: the loop is
/// CONVEX in card count (12.40 ns/card across 400-4,500 against 6.31-7.67 over 1,500-4,500), which is the
/// same working-set effect the corpus sweep measured as rates growing 2.4x over 13x cards. A straight
/// line through a convex curve drives its intercept negative.
///
/// So 85 is compensation for curvature, not a fixed cost, and pasting it would fit today's query-size mix
/// and drift as either query sizes or the corpus change. The fix is a term for the curvature -- see the
/// corpus-size note in `bench_gather_loop` -- not a smaller constant.
const GATHER_FIXED_COST_NS: f64 = 169.6;

// --- PrintingCompose's own rates -------------------------------------------------------------
//
// This arm borrowed every constant it used from plans fitted against DIFFERENT physical operations,
// because until now nothing fitted it: `design_row` returned None for PrintingCompose, so the one arm
// carrying ~75% of measured routing regret was the one arm no tool calibrated. Fitting it (11,332
// rows, 5,996 distinct shapes) moved within-25% agreement from 39% to 55% and p10 from 0.30 to 0.52,
// the largest single gain of the exercise -- and showed the borrowed values are genuinely wrong here:
//
//     term                    borrowed from            was    fitted
//     BROADCAST / PROJECT     LINEAR_PASS             1.50      1.93
//     SCATTER                 RANGE_SCATTER           0.36      0.48
//     WALK_STEP               RANGE_WALK_STEP          4.5      0.58
//     GATHER_CARD_PASS        GATHER_CARD_PASS        6.80      9.81
//     GATHER_PUSH_PER_MATCH   GATHER_PUSH_PER_MATCH   2.81      3.39
//     FIXED                   RANGE_FIXED_COST       150.0    163.56
//
// The two gather rates are the informative ones: fitted on the SAME sample, GatheredScan wants 6.58
// and 2.54 for what the comments called "the same operation". They are not the same operation --
// compose walks a bitmap it just built, GatheredScan walks the printing array -- so the sharing was an
// assumption, not a measurement. WALK_STEP at 7.7x is the largest error; RANGE_WALK_STEP stays at 4.5
// for PrintingRangeScan, which has too few rows here to refit and should not inherit this.

/// Legality broadcast-down and the printing→card/artwork projection pass, both linear over the set.
pub(crate) const COMPOSE_LINEAR_PASS_PER_PRINTING_NS: f64 = 1.93;
/// Range-slice scatter into the printing bitmap during build.
pub(crate) const COMPOSE_SCATTER_PER_PRINTING_NS: f64 = 0.48;

/// A CARD-SPACE collection leaf's build (`ids_of` + `broadcast_card_ids_to_printings`) used to ride
/// `COMPOSE_SCATTER_PER_PRINTING_NS`, on the assumption that it was the same shape of operation as a
/// range's contiguous slice-scatter. It measurably is not: a card-cursor lookup per id (`offsets[c]`/
/// `offsets[c+1]`) plus a variable-width printing-range fill, against a range's single contiguous
/// write.
///
/// Backed out of `otag:triggered-ability`/`otag:cycle`/`otag:activated-ability` (`unique=printing`,
/// EDHREC): with `printings_walked` corrected (`WalkCheckpoints`) and every other term in
/// `PhysicalPlan::PrintingCompose`'s formula computed from measured features, the residual against real
/// wall time scaled cleanly with `collection_broadcast_printings` (not a flat offset), implying 1.41,
/// 1.30, and 1.31 ns/printing -- tight enough (2.7-2.9x `COMPOSE_SCATTER_PER_PRINTING_NS`, all three
/// within 8% of each other) to be a real rate and not sampling noise, but from 3 points on one corpus
/// size, not the corpus-scaling sweep the rates above this comment were fit with. Revisit if a wider
/// measurement disagrees.
pub(crate) const COMPOSE_COLLECTION_BROADCAST_PER_PRINTING_NS: f64 = 1.34;
/// Result-space bitmap words popcounted for the total.
const COMPOSE_POPCOUNT_PER_WORD_NS: f64 = 1.07;
/// Per printing stepped over by the Perm / OrderbyWalk page fill.
const COMPOSE_WALK_STEP_NS: f64 = 0.58;
/// Per row emitted by the Perm / OrderbyWalk page fill.
const COMPOSE_WALK_EMIT_PER_ROW_NS: f64 = 2.19;
/// Per candidate card visited by `gather_composed_page`.
///
/// Raised from 9.81 on 2026-08-03. `fit_cost_model` has wanted this higher in every run it was asked
/// (1.23x, 1.35x, 1.60x across seeds) and it is the dominant term for the queries that dominate
/// compose's regret: a bare `f:` legality leaf under `unique=artwork` puts `eval_domain` at nearly the
/// whole corpus, so 31,508 candidate cards times a 3.4ns error is ~108us on ONE query. Those queries
/// measure ~300us and `PrintingCompose -> GatheredScan` is 99% miss.
///
/// Its own constant, not shared with `GatheredScan`'s `GATHER_CARD_PASS_NS` (6.88) — the two loops do
/// different per-card work, and moving this does not disturb that plan.
const COMPOSE_GATHER_CARD_PASS_NS: f64 = 13.22;
/// Per printing bit-tested against `pbits` inside the gather.
const COMPOSE_GATHER_BITTEST_PER_PRINTING_NS: f64 = 0.38;
/// Per match pushed into the bounded GatherSelect accumulator.
const COMPOSE_GATHER_PUSH_PER_MATCH_NS: f64 = 3.39;
/// Per SET printing the grouping arm scores and compares — see `gather_group_printings` for why this
/// is not the bit-test rate and not the push rate. Cheaper than a push (no `sort_key_bits`, no
/// buffer growth) and dearer than a bit test (a struct read plus `prefer_score`); fitted below.
const COMPOSE_GATHER_GROUP_PER_PRINTING_NS: f64 = 1.5;
/// Per-query setup for the compose fastpath.
const COMPOSE_FIXED_COST_NS: f64 = 163.56;

/// The full-width printing-bitmap build, which no other term charges.
///
/// `compose_printing_bits` allocates an `n_printings`-wide bitmap and ANDs each child into it,
/// `printing_bits_to_card_bits` projects it, and `bitmap_card_ids` walks the card bitmap to extract
/// set ids. All three are O(corpus width) whatever the query matches, and `popcount_words` charges
/// only the **result-space** bitmap, so the printing-space build was free in the model.
///
/// Measured as `meas - oracle_pred` on the gather population — realized counters substituted for every
/// feature, shipped rates untouched, so what is left is work no term charges for. Nine corpus sizes
/// from 0.5x to 5x, built by replicating the corpus and sampling the fractional part by oracle CARD
/// (sampling printings would thin each card's span and change the printings-per-card distribution,
/// which is the quantity under test), 33 cells each:
///
///     residual = -107 ns + 0.0835 ns/printing     R^2 = 0.998
///
/// The intercept is zero within noise: this is a width term, not a fixed cost, and
/// `COMPOSE_FIXED_COST_NS` above stays as it is. `ns/printing` varies 1.13x over that 10x range with
/// no trend — the bitmap crosses 6 KB to 59 KB, L1 to L2, without the rate moving — so the linear
/// shape holds and this constant is not specific to the production corpus.
///
/// **Scoped to the Gather arm deliberately, though the work is physically a BUILD cost** shared by all
/// three paging branches. `Perm` and `OrderbyWalk` had their rates fitted with this cost already
/// absorbed into them, so charging it there as well double-counts: measured per branch over six sizes,
/// `predicted/measured` on Perm goes 1.193 -> 1.544 at the production corpus when this term is added.
/// Gather is the only branch whose error is a clean level (flat 0.52-0.55 across the whole 10x range),
/// which is what makes a single constant the right instrument for it — and it fixes it, to 0.88-0.99.
/// Widening this to the build section requires fixing `Perm`/`OrderbyWalk` first, and those two drift
/// in OPPOSITE directions with corpus size while sharing their rates, so it is not a refit.
/// See docs/issues/local-engine-sparse-compose-gather.md.
///
/// This DOES move production routing, and an earlier draft of this comment claimed it did not. The
/// sparse-permutation case is declined, but `compose_paging_with_total` still returns `Gather` whenever
/// there is no card-space permutation and the query is not printing-mode on usd/rarity — card and
/// artwork on a usd/rarity orderby, which is ordinary traffic (13 of 45 cells in a quick sweep). So it
/// carries a regret gate like any other arm change, not the free pass "it is behind the decline" would
/// have bought.
const COMPOSE_BUILD_PER_PRINTING_NS: f64 = 0.0835;

/// Printings a forward-permutation / orderby walk steps over to fill one page: `page_span` result
/// rows at density `match_rate`. Derived rather than stored, and exposed so a harness can check it
/// against the `printing_span` counter -- the Perm and OrderbyWalk paging branches are priced
/// entirely on this quantity and nothing else validates them.
pub(crate) fn printings_walked(f: &PlanFeatures) -> f64 {
    let page_span = f64::from((f.offset.saturating_add(f.limit)).min(f.matches));
    let match_rate = (f64::from(f.matches) / f64::from(f.n_printings.max(1))).max(MATCH_RATE_FLOOR);
    page_span / match_rate * WALK_LENGTH_BIAS
}

/// The closed form above assumes matches are spread UNIFORMLY along the walk order, so a page of
/// `page_span` rows arrives after `page_span / match_rate` printings. They are not: the permutation
/// orders cards by the sort column, and matches cluster within that order, so the walk runs longer
/// than uniform spacing predicts before the page fills.
///
/// Measured against `printings_examined` once the walk branches began reporting it, the raw form
/// reads a median 0.69 -- consistently under on all three acquires that reach a walk
/// (`printing_range_scan` 0.66, `printing_compose` 0.67, `card_range_popcount` 0.74), which is what
/// makes it a bias worth dividing out rather than three separate errors.
///
/// Bias only. The spread stays wide (p90/p10 ~10-18) because how matches clump along a sort order is
/// not something a density ratio can see, and no constant will fix that.
const WALK_LENGTH_BIAS: f64 = 1.45;

pub(crate) fn plan_cost(plan: PhysicalPlan, f: &PlanFeatures) -> f64 {
    let n_cards = f64::from(f.n_cards);
    // `n_printings` is no longer bound here: it was only feeding the local copy of the walk-length
    // formula, which now calls `printings_walked` so there is one definition of it.
    let matches = f64::from(f.matches);
    let eval_domain = f64::from(f.eval_domain);
    let scan_units = f64::from(f.scan_units);
    let tier_ns = f64::from(f.residual_tier_ns100) / 100.0;
    let limit = f64::from(f.limit);
    let page_span = f64::from((f.offset.saturating_add(f.limit)).min(f.matches));

    // Printings walked to fill the page in a forward-permutation walk (both printing-space plans).
    // Calls the shared `printings_walked` rather than recomputing `page_span / match_rate`: this was
    // a second copy of that formula, and adding WALK_LENGTH_BIAS to the function alone changed what
    // harnesses were TOLD without changing what the router CHARGED. `fit_cost_model`'s mirror check
    // caught it as a 3.7% disagreement; there is now one definition.
    let printings_walked = self::printings_walked(f);
    match plan {
        // #695 bare range, unique=printing: total is the range index's `k` (no synth, no popcount pass),
        // page is a forward permutation walk. So just the walk + fixed setup.
        PhysicalPlan::PrintingRangeScan => {
            printings_walked * RANGE_WALK_STEP_NS  // forward-perm walk to fill the page
                + RANGE_FIXED_COST_NS              // per-query setup
        }
        // #724 unified compose, any distinct-on. One term per build operation, plus a paging term
        // that depends on which strategy `printing_compose_fastpath` will actually use (see
        // `compose_has_perm`'s doc) — the permutation walk and the permutation-free gather fallback
        // have different cost shapes, so this must not just assume the walk.
        PhysicalPlan::PrintingCompose => {
            let build = f64::from(f.broadcast_printings) * COMPOSE_LINEAR_PASS_PER_PRINTING_NS  // legality broadcast-down into the printing bitmap (border/rarity read a plane → 0)
                + f64::from(f.scatter_printings) * COMPOSE_SCATTER_PER_PRINTING_NS  // range-slice scatter into the printing bitmap (cheap: no card cursor)
                + f64::from(f.collection_broadcast_printings) * COMPOSE_COLLECTION_BROADCAST_PER_PRINTING_NS  // card-space collection leaf's build (ids_of + broadcast_card_ids_to_printings) — a card cursor per id, pricier than a range's contiguous scatter
                + f64::from(f.project_printings) * COMPOSE_LINEAR_PASS_PER_PRINTING_NS  // second pass: project printing→card/artwork (0 for printing mode) — the pass CardRangePopcount fuses away
                + f64::from(f.popcount_words) * COMPOSE_POPCOUNT_PER_WORD_NS; // popcount the result-space bitmap for the total (printing/card/artwork words)
            let page = match f.compose_paging {
                // Perm (forward grouped walk) and OrderbyWalk (#744 value-index/plane walk) share the
                // offset-dependent walk shape: fill the page in ~page_span/selectivity steps, then emit
                // one page. OrderbyWalk terminates at page_offset+limit just like the permutation walk,
                // which is exactly why the COMPOSE_GATHER breadth gate is bypassed for it — broad is its
                // best case, not its worst.
                super::ComposePaging::Perm | super::ComposePaging::OrderbyWalk => {
                    // One term for both branches, and there used to be a second: `orderby_walk_scan`
                    // floored the rarity walk at `n_printings`, because a rarity bucket was a one-hot
                    // PLANE and ANDing one covered the whole corpus however few matches survived.
                    // Both walks now step a `PrintingValueIndex` entry at a time, so there is no
                    // bucket granularity left to express and the floor was measured 146x OVER on
                    // `border:black` ordered by rarity (58.3 us charged against 0.4 us realized).
                    // Deleting the feature is the fix; `printings_walked` prices both walks.
                    printings_walked * COMPOSE_WALK_STEP_NS  // walk to fill the page
                        + limit * COMPOSE_WALK_EMIT_PER_ROW_NS  // emit one page of rows
                }
                // gather_composed_page: visits every candidate (eval_domain, same rate GatheredScan's
                // own permutation-free walk pays per card), tests `pbits` membership per printing
                // (scan_units — a cheap bit test, not a real residual scan, so the cheap
                // RANGE_SCATTER_PER_PRINTING_NS rate applies, not GATHER_SCAN_PER_ROW_NS + tier_ns),
                // and pushes each surviving match into the bounded GatherSelect accumulator (matches,
                // same per-match rate GatheredScan pays for the same operation). Offset-independent —
                // unlike the walk above, it costs the same regardless of how deep the page is.
                super::ComposePaging::Gather => {
                    eval_domain * COMPOSE_GATHER_CARD_PASS_NS
                        + f64::from(f.compose_scan_printings) * COMPOSE_GATHER_BITTEST_PER_PRINTING_NS
                        + f64::from(f.gather_group_printings) * COMPOSE_GATHER_GROUP_PER_PRINTING_NS
                        + matches * COMPOSE_GATHER_PUSH_PER_MATCH_NS
                        + f64::from(f.n_printings) * COMPOSE_BUILD_PER_PRINTING_NS
                }
                // The fastpath will refuse this query, so there is no page term to charge. Infinity
                // keeps the plan out of the argmin entirely — routing to a plan that returns `None`
                // pays the detour and then runs something else anyway.
                super::ComposePaging::Decline => return f64::INFINITY,
            };
            build + page + COMPOSE_FIXED_COST_NS // per-query setup
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
            f64::from(f.scatter_printings) * CARD_RANGE_BUILD_PER_PRINTING_NS  // fused one-pass build: scatter+project the range slice straight into card bits
                + matches * PLANE_POPCOUNT_SCATTER_PER_MATCH_NS  // scatter matches through the inverse permutation
                + (n_cards / 64.0) * PLANE_POPCOUNT_PER_WORD_NS  // popcount the card bitmap + skip-scan to the offset
                + limit * PLANE_POPCOUNT_EMIT_PER_CARD_NS  // emit one page of cards
                + PLANE_POPCOUNT_FIXED_COST_NS  // per-query setup
        }
        PhysicalPlan::StreamedSelect => {
            // The small-total gather branch (run_query_streamed) scans all
            // n_cards when total <= STREAM_MIN_MATCHES — the O(N) floor that
            // sinks P3 on narrow queries.
            // Mirrors `run_query_streamed`'s own guard: it returns at `total == 0 || page_offset >=
            // total` BEFORE reaching the small-total gather, so those queries never scan n_cards and
            // must not be charged for it. Charging them anyway over-costs by the whole floor --
            // measured est/real of 55.7 at p50 on zero-match queries, which really take 0.62 us
            // against a ~35 us estimate, and 1,265 of 33k StreamedSelect rows land there.
            let runs_small_gather = u64::from(f.matches) <= *super::STREAM_MIN_MATCHES as u64
                && f.matches > 0
                && u64::from(f.offset) < u64::from(f.matches);
            let floor = if runs_small_gather { n_cards * STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS } else { 0.0 };
            // The other branch: when the small-total gather does NOT run and there is a page to emit,
            // the walk steps the permutation until it fills. Same guards as the gather -- a query with
            // no matches, or a page past the end, returns before the walk too.
            let walks_permutation = !runs_small_gather && f.matches > 0 && u64::from(f.offset) < u64::from(f.matches);
            let perm_steps = if walks_permutation {
                // Entries visited to accumulate `page_span` matches, when matches are spread uniformly
                // through the permutation: one match per `n_cards / matches` entries. Bounded by the
                // corpus, since the walk cannot step past the end of the permutation.
                //
                // The executor now starts and ends the walk at the segment its filter's bound on the
                // SORT COLUMN admits (`walk_bounds`), which this cannot see -- no `PlanFeatures` field
                // carries the filter's shape. The uniform-spread assumption absorbs it: the expected gap
                // before the first match is one `n_cards / matches` stride, negligible against
                // `page_span` of them. What the regrade showed is that the assumption's remaining error
                // is a DIFFERENT shape. Realized/estimated over ~12.5k walking rows, same seed and
                // sample length:
                //
                //     unbounded walk                 p10 0.13   median 1.00   p90 6.43
                //     sort-column bound              p10 0.11   median 0.96   p90 5.31
                //     realized inv_perm min/max      p10 0.08   median 0.90   p90 4.26
                //
                // The third row is not shipped -- it cost 0.51 ns per matching card -- but it bounds how
                // much of the tail a start position can reach at all, and the gap between rows two and
                // three is real: a realized minimum catches clustering from ANY source, while a bound
                // catches only what the predicate names. What is left in BOTH is non-matching entries
                // INTERIOR to the walked segment, which no start position reaches by construction. That
                // is the popcount-skip mechanism's territory.
                (page_span * n_cards / f64::from(f.matches)).min(n_cards)
            } else {
                0.0
            };
            // card_pass — and the verify tier that prices it — is per candidate CARD: the loop
            // calls `filter.card_pass` once per `cid`, and only the cheaper printing-dependent
            // residual is re-checked per row inside `push_card_matches`. Charging `tier_ns` per
            // scanned ROW instead is invisible in card mode (scan_units ≈ eval_domain) and
            // overcharges printing/artwork by the whole printings-per-card ratio.
            //
            // The per-card term is TWO terms, gated apart on the same `tier_ns > 0` signal this arm
            // already uses for its scan: the loop body runs for every candidate, but the `card_pass`
            // CALL only happens when there is a residual to check. `all_match_known` skips it outright
            // (#634 step 1), and `tier_ns == 0` is exactly that condition. Charging the call anyway made
            // the arm's card-mode body read p50 1.90 over-costed.
            eval_domain
                * (STREAM_LOOP_PER_CARD_NS
                    + if tier_ns > 0.0 { STREAM_CARD_PASS_NS + tier_ns.max(STREAM_RESIDUAL_FLOOR_NS) } else { 0.0 })
                // Only with a residual does P3 walk printings; see STREAM_SCAN_PER_ROW_NS.
                + if tier_ns > 0.0 { f64::from(f.stream_scan_units) * STREAM_SCAN_PER_ROW_NS } else { 0.0 }
                + matches * STREAM_EMIT_PER_MATCH_NS
                + perm_steps * STREAM_PERM_STEP_NS
                + f64::from(f.artwork_seen_cards) * STREAM_ARTWORK_SEEN_PER_CARD_NS
                + floor
                + n_cards * STREAM_CORPUS_PASS_PER_CARD_NS
                + STREAM_FIXED_COST_NS
        }
        PhysicalPlan::GatheredScan => {
    // Rows the collect actually walks: `select_page` yields `clamp(matches - offset, 0, limit)`, so a
    // page past the end of the matches collects fewer rows than `limit` asked for.
    let page_rows = f64::from(f.matches.saturating_sub(f.offset).min(f.limit));
            // Per-CARD verify tier, for the reason spelled out in the StreamedSelect arm above.
            // Split and gated exactly as in the StreamedSelect arm above, and deliberately in the same
            // change: this lowers both plans' cost on `all_match` queries, and the one asymmetric
            // adjustment tried before (P3's residual floor moved while P4's stayed) sent
            // `StreamedSelect -> GatheredScan` from 407 lost-time queries to 653.
            eval_domain
                * (GATHER_LOOP_PER_CARD_NS
                    + if tier_ns > 0.0 { GATHER_CARD_PASS_NS + tier_ns.max(GATHER_RESIDUAL_FLOOR_NS) } else { 0.0 })
                + scan_units * GATHER_SCAN_PER_ROW_NS
                + matches * GATHER_PUSH_PER_MATCH_NS
                + page_span * GATHER_SELECT_PER_PAGE_SLOT_NS
                + page_rows * GATHER_COLLECT_PER_PAGE_ROW_NS
                + f64::from(f.artwork_seen_printings) * GATHER_ARTWORK_PER_PRINTING_NS
                + GATHER_FIXED_COST_NS
        }
    }
}
