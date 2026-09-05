# GatheredScan/card: Printing-Varying Leaf Scan Depth

Extracted from item 1 of
[local-engine-cost-model-cleanup-remaining.md](local-engine-cost-model-cleanup-remaining.md) once it
became an ongoing iteration ledger rather than a single-pass fix. Base branch for all work here is
`engine-cost-model-cleanup`, never `main`.

This is the round-by-round history. For what's left and in what order, see
[local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md).

## Problem

**Population**: `GatheredScan`/`unique=card` is the worst-agreement, highest-frequency cell in the
whole cost model — by frequency alone it dominates routing regret more than any other cell.

**What we already know**: the shape-level breakdown (bucketing `printing_compose`/card-mode queries by
AST shape) found `and-2` and `and-3` — specifically pairs/triples over printing-varying fields
(`price_eur`, `price_usd`, `price_tix`, `collector_number_int`, `released_at`, and mixed pairs like
`card_color_identity + price_tix`) — carrying the bulk of the remaining magnitude-weighted error
(tens of millions of units each, at investigation time). These don't qualify for the prior session's
card-invariant depth-1 fix: a printing-varying field has no "first printing settles it" guarantee (a
card matching `price_usd<5` can have OTHER printings that don't), so they still fall through the flat
`domain_cards * printings_per_card * COMPOSE_CANDIDATE_SPAN_BIAS` fallback in the `scan_all` closure
(`card_engine/src/lib.rs:11616`; constant at `:11041`, currently 2.1) — and that formula prices every
card as if it needed its *average* reprint history walked, regardless of how selective the predicate
actually is at the printing level.

Two starting-point ideas, either or both may end up in the ledger below:

1. **Match-density depth proxy.** The query's own printing-level match density (`matches /
   domain_cards` — average number of matching printings per matching card) is a much better proxy for
   expected scan depth than the corpus-wide `printings_per_card` average. A per-card-first-match
   expectation, using order statistics on the position of the first match among a card's printings, is
   the natural model: `expected_depth ≈ (avg_printings_per_card + 1) / (avg_matches_per_matching_card +
   1)`, capped at the card's own span. `COMPOSE_CANDIDATE_SPAN_BIAS` was fit against the OLD flat-average
   shape and should be re-derived (likely much closer to `1.0`) once the depth term itself carries real
   selectivity information, not stacked on top of the new term unchanged.
2. **Per-leaf independence-product combination.** A generalization of (1) to multiple printing-varying
   leaves at once: combine each leaf's own printing-level selectivity via an independence product
   (with a fudge factor) rather than a single aggregate depth term — see Constraints below for why this
   needs an explicit correlation guard before it can be trusted.

## Constraints

- **Pre-computation over hot-path computation, hard requirement.** This repo has a specific, measured
  precedent for what goes wrong otherwise: relaxing `compose_printing_estimate`'s `best_other`
  intersection threshold from `>=2` to `==1` closed a logical gap but caused a **23.6x acquire-time
  regression** (875ns → 20,646ns median) on the newly-admitted population, because it added an
  unconditional `eval_planes`/`popcount_with_bits` pass paid by every query in that population
  regardless of whether the tightening ever changed the routing decision. Reverted; see
  [local-engine-cost-model-cleanup-remaining.md](local-engine-cost-model-cleanup-remaining.md)'s
  "Explicitly considered and rejected" section for the full account — link, don't restate it. Any new
  idea here must trace every new number to an existing precomputed index/table/constant, not a new
  per-query scan whose cost grows with match/printing/candidate count.
- **Price-triple correlation risk.** `price_usd`, `price_eur`, and `price_tix` are near-identical market
  values expressed in different currencies/units — they are NOT independent. An independence-product
  combination across this triple (or any pair of them) will badly underestimate the true joint count.
  Any independence-style idea must be explicitly tested against this triple before being trusted, not
  just against `collector_number_int`/`released_at`-shaped queries. (Power/toughness correlation is
  already handled exactly elsewhere via `arith_tuple_count` — not a risk in this population, no need to
  re-verify it here.)
- **Out of scope, hard**: `card_engine/src/estimator.rs` (its `estimate_cardinality` is live at
  `lib.rs:11146` behind the `STREAM_MIN_MATCHES` gate — editing it can move a shipped routing decision,
  and its `compose_and` independence estimator is unwired PR1 of #702, validated for soundness only).
  Items 2-4 of the parent punch-list doc. `Mode::Printing`/`Mode::Artwork`. Anything outside `lib.rs`,
  `cost.rs`, `tests.rs`, and this doc.

## Current best

As of Round 0 (baseline, `engine-cost-model-cleanup` @ `97dc30c8`), nothing from this doc has shipped
yet — the fix is still the flat fallback described above. Baseline measured against an isolated release
build (`maturin build --release`, extracted wheel, `PYTHONPATH`-pinned — never `maturin develop` into
the shared `.venv`, which silently redirects every other session's `import card_engine`):

```
GatheredScan   card   n=35,074   median 0.67   p10 0.25   p90 2.75   16% within 25%   FAIL
```
(`.venv/bin/python scripts/bench_cost_model_agreement.py --seconds 300 --seed 0`, run from a
`costcell/00-baseline` worktree branched off `engine-cost-model-cleanup`.)

As of Round 1 (match-density depth proxy, `costcell/01-depth-proxy`), the flat fallback is replaced by
`domain_cards * expected_depth * COMPOSE_CANDIDATE_SPAN_BIAS` where `expected_depth = (printings_per_card
+ 1) / (density + 1)`, `density = printing_matches / domain_cards`, and `COMPOSE_CANDIDATE_SPAN_BIAS` is
refit to `0.7`. Same protocol:

```
GatheredScan   card   n=33,944   median 0.72   p10 0.25   p90 3.20   17% within 25%   FAIL
```

Still FAIL by the [0.8, 1.25] median bar — see Round 1 below for why the whole-cell number barely
moves despite a real, controlled improvement in the feature itself.

**As of Round 9 (`costcell/trunk` @ `58eebfdc`), the cell has crossed from FAIL to PASS** — the first
time since this doc opened. Independently re-measured (fresh isolated build, same protocol, not just
the shipping round's own self-reported numbers):

```
GatheredScan   card   n=35,132   median 0.81   p10 0.44   p90 2.99   26% within 25%   PASS
```

Nine rounds landed six kept, held-out-validated fixes (Rounds 1, 3, 4, 6, 7, 9) and one clean
mathematical rejection (Round 2), spanning two previously-separate root causes: the printing-varying
range-leaf family (`compose_printing_estimate`/`scan_all`'s feature estimation, `lib.rs`) and, as of
Round 9, `GatheredScan`'s own cost FORMULA (`cost.rs`) under-charging zero-match `candidates`-acquire
queries by ~4x. The 26%-within-25% figure is still well short of the 90%-within-10% aspiration this
doc opened with — Round 8's diagnostic identified two more concrete, unaddressed mechanisms
(card-mode's unconditional `matches = count` ignoring residual selectivity, and an `Or`/negation
population invisible to this benchmark's flat-conjunction sampling) as the next candidates.

As of Round 3 (`COMPOSE_RANGE_AND_CLUSTER_BIAS`, `costcell/03-cluster-bias`), the `est_cards` fallback
for an `And` of 2+ different-index printing-varying range leaves (the ~37% subset Round 1 identified
as ceiling-capped, and Round 2 proved no independence-product combination can fix) uses its own
clustering-bias constant, `1.1`, instead of `COMPOSE_CARD_ESTIMATE_BIAS`'s `1.78`. Held-out paired-diff
(1,500 and2/and3 RANGE_FAMILIES queries, `unique=card`, hash-of-query split): 433 improved / 117
regressed / 192 tied, total absolute `scan_units` error 8.60M → 8.02M on the held-out half. Same
`GatheredScan`/`card` FAIL as before on the single-run agreement gate — see Round 3 below for why that
is expected and not a sign the fix did nothing.

As of Round 4 (`COMPOSE_RANGE_AND_BROAD_SCAN_SCALE`, `costcell/04-broad-guard`), the LATER
`range_too_broad_to_narrow` guard's full-corpus reset (see Round 3's mid-investigation finding above)
scales `scan_units` alone down to `0.7 * n_printings` whenever `is_cross_index_range_and` holds --
`eval_domain` is left at the full `n_cards`, confirmed exact (0 total absolute error against the real
`cards_visited` counter) for this population, not merely assumed. Held-out paired-diff (372/1,500
guard-fired rows, hash-of-query split): 166 improved / 15 regressed / 0 tied, total absolute
`scan_units` error 5.40M → 1.90M on the held-out half. Same `GatheredScan`/`card` agreement-gate FAIL
as before (16%, unchanged) — this guard-fired subset is a small slice of that pooled cell, same
reasoning as Round 3.

As of Round 6 (`COMPOSE_BARE_RANGE_BROAD_SCALE`, `costcell/06-bare-range`), the `CardRangePopcount`
arm's OWN `range_too_broad_to_narrow` reset -- a bare single range leaf under `unique=card`, a
completely separate acquire branch from Rounds 3/4's `PrintingCompose` target, and Round 5's
diagnostic finding of the single largest bucket in the whole pooled cell (53.7% of error) -- scales
`scan_units` alone down to `0.43 * n_printings`. `eval_domain` is left untouched: 96.6% of rows read
exactly 1.0 (mean 0.975; the small tail is real, driven by price-field null-exclusion, not chased).
Held-out paired-diff (3,500 guard-fired rows, hash-of-query split): 1,704 improved / 31 regressed,
total absolute `scan_units` error 93.3M → 16.0M on the held-out half. Same `GatheredScan`/`card`
agreement-gate result as Rounds 3/4 (15-17%, essentially unchanged) -- this is the largest single
lever fixed so far by pooled-error share, and it still barely moves the headline number, confirming
that gate's grain is simply too coarse to see any single arm's fix, not that this fix is inert.

As of Round 7 (`COMPOSE_SAME_RANGE_BROAD_SCAN_SCALE`, `costcell/07-candidates-range`), `PrintingCompose`'s
OWN `range_too_broad_to_narrow` reset -- the rest of Round 5/6's "single:range" bucket that Round 6's
`CardRangePopcount` fix never reaches, because a bare range fails `card_range_popcount_applicable`
whenever no sort permutation exists for the query's orderby/direction, and a fused two-sided bound
(`eur>=0.23 eur<=0.45`) never reaches `CardRangePopcount` at all -- scales `scan_units` alone down to
`0.52 * n_printings`, gated on a new `is_same_index_range_only` (bare leaf or same-field `And`, as
opposed to `is_cross_index_range_and`'s different-index `And`). `eval_domain` is left untouched:
exact at 1.000 mean/median. Held-out paired-diff (13,053 guard-fired rows, hash-of-query split): 6,422
improved / 33 regressed, total absolute `scan_units` error 304.8M → 57.6M on the held-out half. Same
`GatheredScan`/`card` agreement-gate result as every prior round (17-18%, essentially unchanged) --
this population turned out to be even larger by row count than Round 6's, and still barely moves the
headline number, same grain argument as Rounds 3/4/6.

As of Round 9 (`GATHER_FIXED_COST_ZERO_MATCH_NS`, `costcell/09-zero-match`), `PhysicalPlan::GatheredScan`'s
cost arm (`cost.rs`, not `lib.rs` — the first fix in this doc that lives in the cost FORMULA rather
than feature estimation) charges `42.0` instead of `GATHER_FIXED_COST_NS` (`169.6`) whenever `matches
== 0`, gated the same way the arm's own `tier_ns > 0.0` neighbor already is. Targets Round 8's
mechanism 1: a `Prep::Candidates`-acquired zero-match round collapses every OTHER term in the arm to
zero, so the whole prediction used to read as `GATHER_FIXED_COST_NS` alone — confirmed independently
(fresh 31,030-row sample, 9,890 zero-match, every non-fixed term exactly 0). Held-out paired-diff
(hash-of-query split, 9,890 zero-match rows): calibration half (n=4,944) sets the constant to its
median measured `plan_self_ns`, 42.0; held-out half (n=4,946) reads 4,577 improved / 369 regressed / 0
tied, total absolute ns error 530,256 → 103,110 (5.1x), median ratio 0.248 → 1.000, within-25% 0.1% →
57.7%. `GatheredScan`/`card` agreement-gate cell moves from 11% to 30% within [0.8, 1.25] (median 0.57
→ 0.77) — still FAIL by the median bar (0.77 < 0.8) but the largest single-round movement of this
number since Round 0, unlike every range-family round's "same 15-18%, unchanged" result; the by-unique
`GatheredScan`/`card` cell flips FAIL → PASS (0.69 → 0.80). See Round 9 below for the residual-risk
caveat this round found and verified as immaterial (a shared-`PlanFeatures` edge case in the
RANGE_ACQUIRES-forced-competitor population, checked against `bench_regret_matrix.py` and found to move
total regret by 0.0 ms).

## Iteration ledger

| # | Idea | Outcome | GS/card within-25% | Other cells | Notes |
|---|------|---------|--------------------|-------------|-------|
| 0 | (baseline, `engine-cost-model-cleanup` @ `97dc30c8`) | — | 16% | — | n=35,074, median 0.67, p10 0.25, p90 2.75 |
| 1 | match-density depth proxy | kept | 16% → 17% (noisy, uncontrolled) | none, within run-to-run noise | paired-diff (controlled): 946 impr / 544 regr, 29.6M → 9.86M abs `scan_units` error; `BIAS` refit 2.1 → 0.7 |
| 2 | independence-product `domain_cards` for 2+ different-index range leaves | rejected at self-check | n/a (no code shipped) | n/a | printing-space variant: 38 impr / 496 regr, 17.3M → 18.1M abs error (worse); card-space variant: 0/1500 changed (mathematically incapable of firing) — see Round 2 below |
| 3 | second clustering-bias constant (`COMPOSE_RANGE_AND_CLUSTER_BIAS`) for the same shape | kept | n/a (see Round 3 below — noisy at this cell's grain) | none, within run-to-run noise | held-out paired-diff (controlled): 433 impr / 117 regr, 8.60M → 8.02M abs `scan_units` error; new bias 1.1 against `COMPOSE_CARD_ESTIMATE_BIAS`'s 1.78 |
| 4 | downward `scan_units` scale (`COMPOSE_RANGE_AND_BROAD_SCAN_SCALE`) for the `range_too_broad_to_narrow`-fired subset of the same shape | kept | n/a (see Round 4 below — noisy at this cell's grain) | none, within run-to-run noise | held-out paired-diff (controlled): 166 impr / 15 regr / 0 tied, 5.40M → 1.90M abs `scan_units` error; new scale 0.7; `eval_domain` left untouched (measured exact, 0 error) |
| 5 | diagnostic: re-bucket remaining error by AST shape | diagnostic | n/a (no code shipped) | n/a | see Round 5 below — fresh magnitude-weighted bucketing (n=30,892) finds 74.9% of all pooled `scan_units` error sits in the `range_too_broad_to_narrow` broad-guard reset FIRING OUTSIDE `is_cross_index_range_and` (a population Round 3/4's own comment already flagged as unscaled on purpose); Rounds 1-4's target shape drops to 2.3% of pooled error, median ratio 1.00 — confirming the shipped fixes worked, just on a small slice of the cell |
| 6 | downward `scan_units` scale (`COMPOSE_BARE_RANGE_BROAD_SCALE`) for the `CardRangePopcount` arm's own `range_too_broad_to_narrow` reset (single bare range leaf, `unique=card`) | kept | 15-17% both builds, unchanged (noisy at this cell's grain, same as Rounds 3/4); the finer `GatheredScan/card_range_popcount` sub-row moved 47%→52% within [0.8,1.25], median 0.94→1.05 | none, within run-to-run noise; regret matrix unchanged (96% `printing_compose` share both builds) | held-out paired-diff (controlled): 1,704 impr / 31 regr, 93.3M → 16.0M abs `scan_units` error; new scale 0.43; `eval_domain` left untouched (96.6% of rows exactly 1.0, mean 0.975 — a real but small tail from price-field null-exclusion, not chased); flags the sibling `else` branch's `scan_units = card_est` as itself badly under-calibrated (median ratio ~0.25-0.37 by field) — not fixed this round, out of scope, noted for a future round |
| 7 | downward `scan_units` scale (`COMPOSE_SAME_RANGE_BROAD_SCAN_SCALE`) for `PrintingCompose`'s OWN `range_too_broad_to_narrow` reset, gated on a NEW `is_same_index_range_only` (bare single range leaf, or a fused same-field two-sided bound) — the rest of Round 5/6's "single:range" bucket that `CardRangePopcount` never reaches | kept | 17-18% both builds, unchanged (noisy at this cell's grain, same as every prior round); the pooled `GatheredScan/printing_compose` row (all `unique` modes) unchanged at 24% both builds — expected, small slice of a much larger diverse pool | none, within run-to-run noise; regret matrix unchanged (95% `printing_compose` share both builds) | held-out paired-diff (controlled): 6,422 impr / 33 regr, 304.8M → 57.6M abs `scan_units` error; new scale 0.52; `eval_domain` left untouched (measured exact, median/mean 1.000); population confirmed to be a SEPARATE, independently-broken slice of "single:range" from Round 6's, reached via a different acquire branch (`printing_compose`, not `card_range_popcount`) for two independent reasons — see Round 7 below |
| 8 | diagnostic: bucket candidates-acquire `GatheredScan`/`card` error by shape | diagnostic | 13% (n=22,190, median 0.60), unchanged from checkpoint — expected, no code shipped | n/a | see Round 8 below — pivots off the printing-range-index family entirely (Rounds 1-7's whole target) onto `Prep::Candidates`, the OTHER acquire branch feeding this same pooled cell. Finds `eval_domain` exact (median 1.00 against `cards_visited`) and `scan_units` also near-exact-to-UNDER-predicting (median 1.00, several high-magnitude buckets 1.2-1.8x, i.e. real work exceeds the estimate) — the OPPOSITE direction from the pooled ns-space over-cost (median 0.49-0.60), so neither size feature is the culprit; the bug is in how `GATHER_*` rate/fixed constants convert those (correct) features into ns for the `candidates` (and sibling `plane`) acquire branch specifically. Two concrete mechanisms found: (a) `GATHER_FIXED_COST_NS` (169.6ns) is ~4x too high for the 32% of the sample with zero matches (median measured 42ns); (b) card-mode's `feats.matches = count` (unconditional, `candidate_feats`, lib.rs~11776) ignores real residual selectivity — `is:vanilla`-shaped high-selectivity residuals push 2-3% of the predicted match count, and the whole per-candidate verify-tier charge (`GATHER_CARD_PASS_NS + max(tier_ns, GATHER_RESIDUAL_FLOOR_NS)` × `eval_domain`) doesn't discount for short-circuit-driven cheap-average-case cost the way real `card_pass` behaves at low match rates. A THIRD population invisible to `bench_cost_model_agreement.py`'s own flat-conjunction sampler — Or/negation/nested-paren structures via `structured_query()` — shows the opposite tail shape (median near 1.0, p90 1.25-3.48x UNDER-cost) and needs its own round. |
| 9 | lower fixed cost (`GATHER_FIXED_COST_ZERO_MATCH_NS`) for `PhysicalPlan::GatheredScan`'s zero-match rounds, gated on `matches == 0` the same way the arm's `tier_ns > 0.0` neighbor is gated — the first fix in this doc inside `cost.rs`'s cost FORMULA rather than `lib.rs` feature estimation | kept | 11% → 30% (n=38,435→38,889, median 0.57→0.77) — largest single-round movement since Round 0; by-unique `GatheredScan`/`card` cell flips FAIL (0.69) → PASS (0.80) | `GatheredScan/printing_compose` unchanged (median 1.15→1.14, 24%→24%); `GatheredScan/printing_range_scan` and `/card_range_popcount` unchanged; `bench_regret_matrix.py` total regret unchanged (27.6ms both builds); `bench_query_latency_ab.py` same-build canary swings by a comparable magnitude to the real A/B diff (-0.2µs vs -0.3µs) — no real latency effect claimed | held-out paired-diff (hash-of-query split, 9,890 zero-match rows): calibration half (n=4,944) median measured `plan_self_ns` sets constant to 42.0; held-out half (n=4,946) 4,577 impr / 369 regr / 0 tied, 530,256 → 103,110 abs ns error (5.1x), median ratio 0.248 → 1.000, within-25% 0.1% → 57.7%. Confirmed a real risk this round could not fully close within its `cost.rs`-only blast radius: `plan_cost` costs EVERY candidate plan from ONE shared `PlanFeatures` per acquire (`lib.rs:12917`), so `matches == 0` also fires for `GatheredScan` costed as a competitor/picked plan under `printing_compose`/`card_range_popcount`/`printing_range_scan` (RANGE_ACQUIRES) acquire, where `eval_domain == 0` is an unset accounting default rather than a real empty candidate list, and dispatch pays a real (sometimes large, e.g. 4,959ns median for one `printing_compose` slice) `prepare_candidates` rebuild this arm has no term for at all — pre-existing (already 29x under-predicted before this round) and NOT introduced by this fix, but made numerically worse in isolation (29x → 118x under on that slice). Checked for real routing impact directly (a same-build wheel diff on two flip cases, `date<1993-08-05`/`tix<0.01` under `printing_range_scan`) and via `bench_regret_matrix.py` (total regret 27.6ms unchanged) and `bench_cost_model_agreement.py` (no other cell moved) — no measurable regression found, but the gate is a correlated proxy, not the exact phenomenon, for this sliver of RANGE_ACQUIRES rows; flagged for a future round that can touch `lib.rs` to add an acquire-branch-aware feature |
| 28 | scope `COMPOSE_RANGE_AND_BROAD_SCAN_SCALE` (Round 4) and `COMPOSE_SAME_RANGE_BROAD_SCAN_SCALE` (Round 7) to `Mode::Card` only, leaving `Mode::Printing`/`Mode::Artwork` at the pre-existing unscaled `n_printings` ceiling | kept | not this doc's own metric (see below) | pooled `scan_units` feature accuracy (`bench_feature_accuracy.py`), the metric a fresh `main`-vs-`costcell/trunk` A/B (Round 27) found regressed: median 0.70 (UNDER-COUNTS) → 0.94 (clean), against `main`'s own 1.00 | see "Round 28" narrative below — both scales were fit exclusively on `unique=card` samples (each round's own doc says so) but applied unconditionally to all three modes; `Mode::Printing`/`Mode::Artwork`'s real `printings_examined / n_printings` reads EXACTLY 1.000 (zero spread) for this guard-fired population, so the card-only-derived scale was silently manufacturing an under-count for two modes it was never calibrated against |
| 30 | `STREAM_SMALL_TOTAL_REDO_BIAS`, a `stream_scan_units` correction for `printing_compose`'s bare `else` arm (`Mode::Card`, no legality partner) — Round 1's `scan_units` revision was inherited verbatim by `StreamedSelect`'s own feature, which structurally under-prices a SECOND, unmodeled `push_card_matches` pass `run_query_streamed`'s small-total branch pays and `GatheredScan` never does | kept, partial | n/a (this doc's own agreement-gate metric untouched; see the flip/regret numbers below instead) | `#852` ordering 88%→88% clean; Round 28's pooled `scan_units` median 1.00→1.00 clean | see "Round 30" narrative below — of 114 reproduced f3f4a017 flip queries, 50 (44%) now correctly re-route to `GatheredScan`; `StreamedSelect -> GatheredScan` regret matrix slice -7% share of traffic / -12% regret-ms; residual traced to the acquire-time `result_total` ESTIMATE itself being unreliable near `STREAM_MIN_MATCHES` for cross-index-range Ands (this doc's own Round 1 "separate, uninvestigated `domain_cards` bug" flag) — not a `cost.rs` rate problem, so chunk 2 (rate refit) is unlikely to close the rest on its own |
| 32 | new `PlanFeatures::perm_walk_span` feature (`cost.rs`/`lib.rs`) for `StreamedSelect`'s OTHER branch (`walks_permutation`, `total > STREAM_MIN_MATCHES` — different from Rounds 30/31's small-total gather): `perm_steps`'s estimate multiplied by `n_cards` unconditionally, when the real executor already bounds its walk to the filter's own interval on the sort column | kept | n/a (not this doc's metric; see below) | `#852` 88%→89% clean; Round 30/31 territory (`StreamedSelect -> GatheredScan` regret slice) flat; Round 28's `scan_units` unreachable by this change | see "Round 32" narrative below — held-out mean \|log ratio\| 1.033→1.001 pooled (both halves improve independently); `StreamedSelect/candidates` cost-model-agreement cell unchanged (median 0.59 both builds) because the targeted correlation (filter bounds the same field the query orders by) is rare under uniform traffic; shipped as a strict-generalization correctness fix (collapses to the old formula when unbounded), not for measured impact on this specific cell |
| 33 | `set_collector_ranges` (`lib.rs`, load-time precomputed per-set `collector_number_int` min/max/count), a new `compose_printing_estimate` `And`-arm tightening for the 2-source `set:X` + `cn`-range shape: `density = count / (max-min+1)` scaled by the query's own overlap, replacing the plain min-fold this shape had no other tightening for | kept | n/a (not this doc's own metric; see Round 33 narrative) | pooled cost-model-agreement cells move within noise (an untouched acquire branch, `PrintingCompose/plane`, shows the largest swing, 0.88→0.76, confirming it's sampling noise not this fix); `#852` 89%→89% clean; Round 28's `scan_units` 1.00→1.00 clean; Rounds 30/31/32's flip-query population 51/95 fixed on BOTH builds, 0 regressed | see "Round 33" narrative below — held-out validation across 550 real sets / 3,300 queries / both shapes: density estimator pooled median \|log ratio\| 0.000 (88.8% within 25%) against the fold's 0.788 (18.0% within 25%); regret matrix moved 37.4ms→33.4ms (-11%, improving); one honest documented exception -- a non-contiguous set (SLD) can now undershoot where the fold used to overshoot, still a net improvement (2.5x under vs 24x over) but a new failure direction |
| 34 | `SubtypePairIndexes` (`lib.rs`, load-time top-256-per-dimension `set`/`c`/`id` x subtype `SpaceTotals` tables plus `rest_max`), a new `exact_result_total` arm (exact in any `unique=` mode on a table hit) and `compose_printing_estimate` `And`-arm tightening (exact on a hit, capped independence-product on a miss) for `set:X`/`c:X`/`id:X` And'd with a subtype leaf (`t:elf`), a shape with no tightening at all before this round (`t:` has no `compile_plane` arm and isn't in any pair table) | kept | n/a (not this doc's own metric; see Round 34 narrative) | `#852` 89%→89%/97%→97% clean; Round 28's `scan_units` 1.00→1.00 clean; Round 33's own `set:sld cn<=100` unchanged (25/25); Rounds 30/31/32's `StreamedSelect->GatheredScan` regret slice flat within noise | see "Round 34" narrative below — held-out validation (550 sets + 28 colors/28 identity raw masks, ~3,635 queries): pooled median \|log ratio\| printing 3.369→0.693, card 3.091→0.693; `set`/`colors` improve cleanly in both modes, `identity` improves in card mode but has a small, explained printing-mode within-25% trade-off (30.0%→22.1%); two mid-round corrections both caught by checking real behavior rather than trusting the design as briefed -- a flat card-space table (invisible to `unique=card`/`artwork`) rebuilt as `SpaceTotals` cells mirroring `PairTotals`, and `id:`'s real bare-colon default discovered to be `Le` (subset) not `Ge` like `c:`; identity's `rest_max` (377) verified higher than the ~100-150 routing-fragile zone the brief expected, still below the confirmed 900-2000 reversal zone, reported honestly rather than assumed safe |
| 35 | `leaves_are_disjoint` (`lib.rs`), one new arm: `set:X`/`set:Y` (x != y) added alongside the existing border/legality/rarity "exactly one value per printing" arms, feeding both `pair_bounded_min` and `exact_result_total`'s 2-child `And` shortcut | kept | n/a (not this doc's own metric) | `#852` 89%→89% clean (same-seed A/B, within noise); Round 28's `scan_units` unreachable by this change; Rounds 30-34's own populations untouched (disjoint blast radius: one new match arm) | see "Round 35" narrative below — real per-printing residual walk (`card_match_count`) verified to require the SAME printing to satisfy an And's whole residual in BOTH `Mode::Card` and `Mode::Artwork`, so the fix is exact in all three spaces despite `set` (unlike border/rarity) commonly varying across a card's own reprints and even across one illustration's own printings (16,838/46,523 real `illustration_id`s span >1 set); 40 random real set pairs x 3 modes (120 checks): baseline 0/120 agree (real always 0, estimate always nonzero, up to 135) -> fixed 120/120 exact |
| 37 | `and_trace` (`lib.rs`): structured per-query provenance for the `And` arm's own evaluation, as a tree of `leaf`/`joint_lookup`/`independence` nodes plus a `considered` list of every 2-or-3-child combination the arm's fixed sequence attempted (hit or miss) — replaces the throwaway env-gated `eprintln!` instrumentation Rounds 33-36 each rebuilt from scratch. `scripts/nway_estimate_truth_survey.py`: a checked-in, deterministic, curated-leaf-shape estimate-vs-truth survey harness (replaces one-off scratchpad diagnostics), primary metric plan-choice agreement (not raw ratio) | kept | n/a (tooling; no estimator value changed) | n/a | see "Round 37" narrative below — two real bugs found and fixed before trusting any output: `and_trace_for` missing an `is_printing_composable` guard (crashed `explain()` on any `is:`/`keyword:` tag query, breaking its "safe to call constantly" contract) and an inverted worst-first ranking in the harness's own report tables (reused a ratio-shaped rank formula on an already-distance-shaped metric, sorting a perfect median to the top of "worst"). First full sweep (53,778 rows, 88 curated shapes, all 3 spaces) found `color:X`/`id:X`/`cmc<op>N` paired with a price comparison at 0% mechanism coverage and the worst median error in the whole survey — the input Round 38 acted on |
| 38 | `min(fold, independence)` for `compose_printing_estimate`'s `And` arm: `color:X`/`id:X`/`cmc<op>N` paired with exactly one price comparison (`usd`/`eur`/`tix`, any op) — the first real use of the `"independence"` op Round 37's `and_trace` tree schema reserved for it | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 53,766 shared rows: 454 plan-choice flips total, 452 inside the three target shapes (all toward `GatheredScan`), 2 incidental elsewhere (an eligible pair embedded inside a larger conjunction); `root=leaf`/`root=or`: 0 changes, confirming the fix stayed scoped to the `And` arm | see "Round 38" narrative below — calibrated against 610 real rows: median `\|log ratio\|` 0.88→0.07 (94.8% improved, 4.4% regressed, concentrated in `cmc+usd`'s own undershoot tail); a grid search over a multiplicative bias (`fudge × independence`, 1.0–2.0) found `fudge = 1.0` (no bias at all) strictly optimal on both median AND mean error for every shape — contradicting the initial "bias it slightly high to be safe" intuition. Independently re-verified end to end with a fresh before/after sweep (not just the implementing agent's own report): all three `unique=` modes improve (printing most tightly — it's the only space `result` directly tightens; card/artwork improve via the same downstream scaling every other estimate-only shape already goes through, since `exact_domain_cards`/`exact_domain_artworks` are populated only by genuinely exact mechanisms, never by this one) |
| 39 | `and_estimate_ns` (`AcquireFacts`): single-shot wall time of the real, production, acquire-time `compose_printing_estimate` call inside `acquire_plan_features`'s `PrintingCompose` branch — a permanent per-query cost baseline for grading the general partition-search estimator's own "tax" once it exists, not another accuracy fix | kept | n/a (tooling; no estimate value changed) | n/a | see "Round 39" narrative below — real distribution (53,778-row sweep): median 750ns, p90 4.4µs, p99 11.6µs; populated on exactly the 59.3% of rows whose acquire took the `PrintingCompose` branch (`None` elsewhere, never "0ns"). Paired-wheel latency A/B for this specific addition (required by `.claude/rules/benchmark-methodology-review.md` for any change to a documented hot path): the real effect was not distinguishable from the same-build canary's own run-order drift — reported honestly as "no measurable overhead detected," not claimed as a proven-safe number |
| 40 | Generalizes Round 38's one hard-coded independence pair into a small registry (`IndepClass`/`independence_safe_pair`), scanned pairwise over every RESIDUAL `And`-arm leaf (not covered by an existing exact mechanism), plus a class-priority fix for winner attribution | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 53,775 shared rows: 395 plan-choice flips, concentrated in the newly-covered shapes (all toward `GatheredScan`); `root=leaf`/`root=or`: 0 changes; `unsafe:legality+set`/`+released`: 0/900 both builds, confirming the deliberately-excluded pair stayed untouched | see "Round 40" narrative below — registry re-validated against real data, not copied from the design doc's own self-contradictory list: adds `legality×{cn,price}`, `type×{released,usd}`, plus `id×set`/`pow×set`, which empirically REVERSE the doc's "unsafe" claim (independently re-confirmed on a fresh seed, not just the implementing agent's own sample: `id×set` median 1.34→0.14, 273/283 improved; `pow×set` 1.36→0.17, 146/147). `legality×{set,date,year}` deliberately excluded (format legality is date-DEFINED, not merely correlated — reserved for a future exact per-(set,format) mechanism, not independence). Same-currency price crosses spot-checked and found genuinely mixed, not shipped. **A real regression caught by pre-merge independent verification, not the implementing agent's own report**: `safe:cmc+usd` (already covered by Round 38) got WORSE (median 0.158→0.219, 218/246 regressed) — a same-field arith consolidation (`cmc>=1 cmc<=1`) was being marked `covered` unconditionally, silently blocking Round 38's own price×cmc pairing before the new registry scan ever saw it. Fixed (gate `mark_covered` on `single_arith_field(...).is_none()` — only a genuine cross-DIMENSION join covers its leaves); re-verified byte-identical to the pre-Round-40 baseline on the exact repro and every already-covered shape before merging |
| 41 | Card/artwork space in `compose_printing_estimate`'s `And` arm now floors on each UNCOVERED leaf's own exact card/artwork count (`children_estimates`), gated by the SAME `range_too_broad_to_narrow` breadth guard `narrow_rec` already uses — closing an asymmetry printing space never had (its own baseline, `folded.result.printing`, already floors on every leaf unconditionally) | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 8,097 shared rows (independent re-sweep, fresh seed): 23 plan-choice flips (0.3%), all one-directional (`PrintingCompose → StreamedSelect`/`GatheredScan`, never the reverse); ratio diagnostic unchanged (mean 0.184 both builds, "no detectable difference"); zero-true-count hit rate unchanged (77.6% both); border-leaf reproducers (`cmc=1 border:black`, `cmc>=1 cmc<=5 border:black`, etc.) byte-identical before/after, confirming the breadth guard correctly declines on a genuinely broad leaf | see "Round 41" narrative below — found while scoping the general N-way partition search (`local-engine-nway-compose-independence-search.md`): `color:G format:pioneer t:elf` predicted card=1179/true=246 (`t:elf`'s own exact card count, 660, was never considered — a strict subset of what compile_plane's partial color+legality joint reported, itself looser than a bare min-fold would be). This is a previously-scoped, deliberately-deferred fix — `local-engine-domain-cards-existential-arith-and.md`'s own "Ingredient 3" — shipped now with the breadth guard that round asked for, and scoped so the new floor affects ONLY `result_space` (what `explain()` reports), never `exact_domain` (what `scan_units`'s real execution-cost pricing reads), so the two fields — accidentally identical for card/artwork before this round, unlike printing where they already legitimately diverge — now behave the way printing's split always has. Motivating case: `eval_domain` 1179→660 (still not exact — true is 246 — this tightens a bound, it does not solve the underlying "no true 3-leaf joint mechanism exists yet" problem, tracked as a separate Round 42 candidate). `and_estimate_ns` canary: no consistent, reproducible tax detected, not distinguishable from the same-build canary's own noise floor |
| 42 | Generalizes `SubtypePairIndexes`/`SubtypePairEstimate` past its `v.len() == 2` gate to scan the residual for every `(dim, subtype)` pair present in an `And` of any size — no reordering relative to `compile_plane`, no new placement/priority rule; the existing `.min()`-chain across mechanisms already composes correctly regardless of order | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 8,346 shared rows (independent re-sweep, fresh seed): 23 plan-choice flips (0.3%), concentrated in the newly-covered `triple:color+type+cmc`/`triple:color+legality+type` shapes; ratio diagnostic improved (mean 0.177→0.173, "B is MORE accurate"); border-leaf reproducers byte-identical before/after; the pre-existing 2-leaf `set:eld t:knight` case unchanged | see "Round 42" narrative below — directly closes the doc's own `color:G format:pioneer t:elf` worked example: `eval_domain` 660→560 (Round 41's own floor), now a genuine `SubtypePairIndexes` table HIT (card 560/printing 1917/artwork 790 — still not exact, true is 246, but a real, confirmed tightening). **A first implementation pass shipped a real gap, caught by independent verification, not the implementing agent's own report**: it correctly generalized the gate, but ALSO skipped `covered` leaves on input for both the exact-hit AND estimate branches (mirroring the independence registry's own precedent) — which meant `color` (already covered by `compile_plane`'s joint with `legality`) never reached the new scan at all, so the motivating example got ZERO benefit on the first pass. Root cause: skipping `covered` leaves is necessary for the ESTIMATE-class fallback (an independence-shaped estimate isn't a guaranteed bound and could undershoot below an already-exact value — Round 40's own class-priority reasoning) but NOT for the EXACT-hit branch, where any true sub-conjunction's count is always a valid bound regardless of what else covered a leaf, exactly like `compile_plane`/`pair_bounded_min`/the arith-tuple merge already behave. Fixed by splitting the two branches: the exact-hit scan ignores `covered` entirely; the estimate fallback still respects it, recomputed fresh after the hit loop runs |
| 43 | Triple-level independence safety investigation — measurement only, no engine code changed | diagnostic | n/a (no code shipped) | n/a | see "Round 43" narrative below — the literal "does joint 3-way independence hold" question isn't reachable (no triangle in the registry's adjacency graph); the real, reachable, CONFIRMED-bad scenario is a "star" (two of a hub's registered partners present simultaneously, both independence estimates fired and `.min()`-folded, a composition neither pair's own 2-leaf calibration ever measured): `star:color+cmc+usd`/`star:identity+cmc+usd` substantially worse than either component's baseline across three independent seeds; an unplanned second finding (three star candidates swept by an exact `PlanePopcount` mechanism instead) are ALSO worse than baseline. Not fixed this round — a follow-up is recommended (decline both estimates when a hub + 2 different partners co-occur), scoped but not built |
| 44 | New exact `(colors\|identity) x cmc` table (`ColorCmcTable`/`ColorCmcIndexes`) — 32 raw per-mask buckets (no `Ge`/`Le` lattice pre-summing, unlike `ColorSubtypeTable`), each mask's cmc dimension prefix-summed (mirroring `RangeCardCounts` exactly); wired into the `And` arm's residual scan and `exact_result_total`'s 2-leaf shortcut | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 65,541 shared rows (independent re-sweep, fresh seed): only 3 shapes show ANY plan-choice change (`star:identity+cmc+usd` 13.4%, `star:color+cmc+usd` 12.4%, `triple:color+type+cmc` 1.3%), zero elsewhere; ratio diagnostic improved (mean 0.287→0.279, "B is MORE accurate") | see "Round 44" narrative below — directly fixes Round 43's own confirmed-bad star: `star:color+cmc+usd` median abs-log-ratio 0.80→0.58, `star:identity+cmc+usd` 0.71→0.57 (fresh-seed independent re-check: 0.52/0.55) — real, substantial, not just moved sideways. The pure 2-leaf case is now EXACT in all three spaces (verified directly via `and_trace`: `color:G cmc<=3` card 3468/3468, printing 10268/10268; `id:UG cmc>=1 cmc<=5`, a two-sided bound, also exact at 10421/30050 against true). **A real regression found and fixed by the implementing agent itself, before I ever saw it** — my own instructions said to `mark_covered` on a hit, matching every other exact mechanism's convention; measuring against the real corpus showed this was actively harmful (median moved 0.80→1.08, WORSE) because it starves BOTH `ColorId`×`Price` and `Cmc`×`Price` (neither has a partner left once both leaves are claimed), and the new table's own bound — which ignores price entirely — is often looser than what those two (price-aware, if only via independence) estimates gave. Removing `mark_covered` let all three compete via `min()` and fixed it (0.80→0.58). Safe to leave uncovered: unlike two ESTIMATE-class mechanisms compounding on the IDENTICAL two leaves (Round 40's own concern), Independence's two candidates here each share only ONE leaf with this mechanism's pair, never both — genuinely different sub-conjunctions, not competing answers to the same question. Confirmed unrelated to the "swept trio" from Round 43 (`legality`/`color`/`identity`×`price`) — untouched by this round, as expected |
| 45 | A bare `set:X` leaf's own solo `ComposeEstimate` now carries real `Some(card)`/`Some(artwork)` (from `set_totals`'s own `.cards`/`.artworks`, already computed for `SubtypePairEstimate` and previously discarded) instead of `None` — lets Round 41's card/artwork floor use `set:X`'s own true count instead of silently skipping it | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 44,511 shared rows (independent re-sweep, fresh seed): only 2 shapes show ANY plan-choice change, both `set:`-shaped (`same_family:set+set` 2.3%, `unsafe:set+type` 1.0%), zero elsewhere; ratio diagnostic unchanged (expected — floored at `true_total>=100`, excluding the small-count population this fix targets) | see "Round 45" narrative below — fixes a catastrophic case found by direct inspection, not a synthetic benchmark: `set:mh2 usd<10 cmc<5 power>1 color:g` predicted card=4762/artwork=6680 against printing=492, an impossible ordering (`card`/`artwork` can never exceed `printing` for a real population) — root cause: a bare `set:X` leaf's own estimate had `card: None, artwork: None` (confirmed via `ComposeEstimate::leaf`'s own doc: "no cheap exact card/artwork source" is the *default* for most leaf types, not a `SetCode`-specific bug), so Round 41's floor could never use `set:mh2`'s own true 309/391 as a ceiling. Fixed: card 4762→309, artwork 6680→391 (both now correctly ≤ printing's 492) — independently re-verified directly via `and_trace` on both wheels, not just the implementing agent's own report. **A second, separate, still-open bug found in the same investigation**: the `card<=printing`/`artwork<=printing` invariant is violated elsewhere in the curated catalog too (e.g. `c:w t:plains`, card=40/artwork=511 both exceeding printing=24, true_total=0 for all three) — confirmed byte-identical on the pre-Round-45 wheel, so this is NOT introduced by this round. Root cause: Round 41's own floor takes a leaf's solo card/artwork as a candidate without a final `.min()` clamp against the query's own `result.printing` — a real, pre-existing gap in Round 41 itself, not fixed here (out of scope for this round, flagged as the natural next fix) |
| 46 | Structural refactor: one `Candidate` enum + `fold_candidate` entry point (replaces ~10 hand-copied fold sites), one shared `scan_two_bucket_exact` helper (three callers: `SubtypePairIndexes`, `ColorCmcTable`, `SubtypeArithBox`), plus a `debug_assert!(cards<=artworks<=printings)` census — no mechanism logic changed | kept | n/a (not this doc's own metric) | Byte-identical bar, independently re-verified: isolated-release `nway_estimate_truth_survey.py --compare`, 65,478 shared rows — only 3 rows (one query, `t:warrior set:shm`, all 3 modes) differ, and re-running the UNMODIFIED before-wheel against itself 3 times reproduces the identical flip with zero code change, confirming it's the pre-existing table nondeterminism below, not a regression; every other row byte-identical, `picked_plan` unchanged everywhere. `cargo test`: 226 passed release / 229 debug. `cargo clippy --all-targets -- -D warnings`: clean (a `--release`-only dead-code warning on an unrelated `#[cfg(debug_assertions)]` test constant confirmed pre-existing on `costcell/trunk` too) | see "Round 46" narrative below — the census found ZERO `debug_assert` violations from any of the six EXACT mechanisms (every one already produces internally self-consistent triples); it found **10,269 root-level violations across 3,421 distinct queries** (`artworks > printings`, never `cards > artworks`) — independently reconfirmed at smaller scale (32% of `root=and` rows in a fresh spot sweep) — all attributable to Round 41's own already-known unclamped floor, confirmed far wider in scope than the single `c:w t:plains` example on record. **A real, independently-converged discovery, found separately by both the implementing agent and me during verification, root-caused precisely**: `build_subtype_pair_tables`'s top-256 cutoff (`items.sort_unstable_by_key(Reverse(cards))`, `lib.rs:1917-1922`) has no deterministic tie-break, and Rust's default `HashMap` hasher is randomly seeded per process — so a pair tied at the exact boundary value can land inside or outside the table on one build/run and not another, with real, different predicted numbers each time (reproduced directly: the identical wheel, re-run 4 times, gave `t:monk usd>0.19 c:u` card=58 via a table HIT on one run and card=48 via the MISS estimate on the other three). Confirmed unrelated to this round (reproduces on plain, unmodified `costcell/trunk`) but flagged as high-priority: it can make a FUTURE byte-identical refactor's own verification look like it found a regression when it's really this. A related manifestation also showed up in the harness's own query generation (the identical `--seed 0` run, same corpus, produced 9 different queries between two separate engine loads) — same underlying class of bug, not chased down further, noted for whoever fixes the root cause |
| 47 | `top_n_and_rest_max` (`SubtypePairIndexes`'s shared top-256-per-dimension cutoff) now extends past `n` to include every pair tied with the boundary card count, instead of a plain `truncate(n)` with no tiebreak — fixes Round 46's own discovered nondeterminism at its root | kept | n/a (not this doc's own metric) | Independently re-verified: isolated release wheel, 5 fresh index builds each, byte-identical every time for all 4 previously-flipping queries across both affected dimensions (`t:monk usd>0.19 c:u`, `t:warrior set:shm`, `c:b t:advisor`, `c:bw usd>=0.35 t:cleric`) — zero variance, where before this fix at least 3 of these flipped between builds. The now-stable table hits are also independently confirmed EXACT against ground truth (`c:u t:monk`/`c:b t:advisor`/`c:bw t:cleric` all read exactly 38/38/38 real cards, matching `explain_analyze`'s own true totals in all 3 spaces). Sweep (fresh seed, 43,365 shared rows): 43,239 unchanged, 125 improved-or-equal, 1 single-row sub-unit artifact (`t:angel c:b` printing: error 10→13 against true=127) — reproduces the agent's own identical finding exactly (same query, same true_total), explained as the capped-estimate MISS fallback's non-monotonic response to a tighter `rest_max` input, not a shape-level regression. `cargo test`: 230 passed release / 233 debug. `cargo clippy --all-targets -- -D warnings`: clean (debug; the same pre-existing release-only dead-code warning from Round 46 confirmed unrelated again) | see "Round 47" narrative below — the chosen fix (extend to include every tie) over a plain deterministic tiebreak, and why; real boundary/tie numbers for all three dimensions, with an honest note on a real discrepancy in my own independent verification attempt |
| 48 | `SubtypeArithBox`'s gate generalized from `arith_children.len() + 1 == v.len()` (whole query must be exactly "one subtype leaf + N arith leaves") to just `!arith_children.is_empty()` — scans the residual for every subtype leaf present, mirroring `SubtypePairIndexes`'s own Round 42 generalization; `mark_covered_on_hit` now scoped to only the leaves a given hit actually explains, not all of `v` | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 66,378 shared rows (fresh seed, new curated shape `subtype_cube:type+cmc+usd` added): 74 plan-choice flips (0.1%), concentrated in `subtype_cube:type+cmc+usd` (39/900, 4.3%) and `star:cmc+type+usd` (31/900, 3.4%); base "no residual" shapes (`subtype_cube:type+cmc`, `subtype_cube:type+pow+tou+cmc`) show zero rows changed, confirming strict superset behavior for the case that already worked. Ratio diagnostic: mean abs-log-ratio +0.001 overall — **"B is LESS accurate" in aggregate**, not more (`subtype_cube:type+cmc+usd` 0.402→0.456, 45 improved/64 worsened; `star:cmc+type+usd` 0.354→0.361, 38/42) | see "Round 48" narrative below — the motivating case (`t:elf cmc>=5 usd<10`) improves dramatically (printing 1665→241, true 177), but the aggregate sweep regression is real and independently reproduced: root-caused via `and_trace` to `covered`'s pre-existing leaf-occupancy conservatism (queue item #3) blocking `Independence` from trying `(subtype, price)` once the box covers `(subtype, cmc)` — a live, measured instance of the "loosen covered" gap, not a defect in this round's own logic. A related, validated idea surfaced during review (not built this round, logged in the followup queue): the box's own exact joint, combined via independence with the residual price leaf's solo rate, gives 241×0.779≈188 against true 177 — tighter than the box's price-blind 241 alone |
| 49 | Loosens the independence registry's `covered` gate from leaf-occupancy to subset-identity: `CoveredState { flags, subsets: Vec<u64> }` replaces the bare `covered: Vec<bool>` — `flags` keeps the unchanged Round-40 leaf-level bookkeeping, `subsets` records one bitmask per genuine joint hit (via `mark_covered`/`pair_bounded_min`). The independence registry no longer skips a leaf merely because SOME other mechanism touched it (`is_covered` deleted); it declines a candidate pairing only when that pairing's own combined leaf-mask exactly equals an already-recorded subset | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 66,366 shared rows (fresh seed): 378 plan-choice flips (0.6%), 100% confined to `root=and`'s `*+usd` star/cube shapes (`star:legality+identity+usd` 13.9%, `star:legality+color+usd` 12.8%, `star:legality+cmc+usd` 4.6%, `star:color+identity+usd` 3.4%, `star:cmc+type+usd` 2.2%, `subtype_cube:type+cmc+usd` 2.2%, `star:identity+type+usd` 1.6%, `star:color+type+usd` 1.3%); `root=leaf`/`root=or` show zero changes. Ratio diagnostic: mean abs-log-ratio **−0.034** (95% CI [−0.036, −0.032], excludes 0) — **"B is MORE accurate"**, reversing Round 48's own "B is LESS accurate" finding | see "Round 49" narrative below — independently reproduced end to end: the regression case (`t:elf usd<0.20 cmc>=2`) recovers exactly to printing=425 (matching the pre-Round-48 answer), Round 48's own motivating case (`t:elf cmc>=5 usd<10`) stays unchanged at 241, both traced via `and_trace` on isolated release wheels built myself, not just the implementing agent's report |
| 50 | "Anchored independence" for `SubtypeArithBox`: on a box hit, scans for a SINGLE residual leaf classifying as `IndepClass::Price` (declines entirely if 2+, the price-triple guard) and multiplies the box's own exact joint by that leaf's solo rate, folding the product as a new `Estimate`-class candidate (`"SubtypeArithAnchoredIndependence"`) via `.min()`. Deliberately narrow — only `SubtypeArithBox`, only `Price` — mirroring Round 38/42's own "one mechanism, one class, validate, then expand" discipline | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 66,363 shared rows (fresh seed): 14 plan-choice flips (0.0%), 100% confined to `subtype_cube:type+cmc+usd` (9/900) and `star:cmc+type+usd` (5/900); `root=leaf`/`root=or` zero changes. Ratio diagnostic: mean abs-log-ratio −0.002 (95% CI [−0.002, −0.001], excludes 0) — "B is MORE accurate" | see "Round 50" narrative below — independently reproduced: `t:elf cmc>=5 usd<10` (true 177) tightens 241→188 (1.36x→1.06x); `t:elf usd<0.20 cmc>=2` (true 366), not required by the plan but checked anyway, ALSO improves 425→370 (1.16x→1.01x) — a genuine bonus, not assumed |
| 51 | `ArithTupleIndex` gains `totals: Vec<SpaceTotals>`, one exact (printing,card,artwork) triple per distinct (cmc,power,toughness,loyalty) combination, summed once at build time from that key's own postings (`offsets`/`artwork_base`, already in scope at the one call site). `arith_tuple_count`→`arith_tuple_totals` now returns the exact triple instead of a card count; all 3 call sites updated — the main And-arm joint now folds `Candidate::Exact` (closing Round 46's census blind spot), the independence registry's Cmc/Pow multi-unit gains real `artwork: Some(...)`, the single-leaf fallback gains real card/artwork. `ARCHIVE_FORMAT_VERSION` bumped | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 66,372 shared rows (fresh seed): 144 plan-choice flips (0.2%), 100% confined to cmc/pow/tou-involving shapes; `root=leaf`/`root=or` zero changes. Ratio diagnostic: mean abs-log-ratio −0.003 (95% CI excludes 0) — "B is MORE accurate"; a targeted slice on rows this mechanism won (1,383 rows): `unique=printing` median abs-log-ratio 0.168→0.000 | see "Round 51" narrative below — validated against the real corpus BEFORE scoping (a direct `oracle_id`-grouped check of `corpus.jsonl`, no engine build needed) and independently re-reproduced after merging: `cmc>=8 power<=2` printing 30→21 (true 21, exact); `cmc<=1 power>=1 tou>=1` printing 3225→2786 (true 2786, exact). A real, honestly-flagged pre-existing gap found (not fixed, out of scope): `unique=artwork`'s own acquire path routes through a separate `artwork_estimate` function, not this mechanism's `exact_domain_artworks` — artwork FINAL improves but doesn't fully close (15 vs true 13, was 22) |
| 52 | Wires `est.result.card`/`.artwork` (`compose_printing_estimate`'s own And-arm fold, already computed) into `acquire_plan_features`'s `unique=card`/`unique=artwork` acquire path, closing Round 51's own artwork gap — folded in as an ADDITIONAL `.min()` tightening on top of the pre-existing calibrated-estimate baseline, never a replacement for it. `exact_result_total` (the existing exact source for these modes) is untouched | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 66,378 shared rows (fresh seed): 184 plan-choice flips (0.3%), `root=leaf` 0.0%/`root=or` 0.0%. Ratio diagnostic: mean abs-log-ratio −0.017 (95% CI excludes 0) — "B is MORE accurate". Independently re-verified row-by-row (not just the aggregate): **zero rows regressed**, 3,038 improved | see "Round 52" narrative below — a real regression in the round's OWN first attempt was caught by the corpus sweep before shipping (a plain outright-replacement merge let a partial-subset exact mechanism's own valid-but-loose bound override a much-better calibrated estimate, `id:ruw usd:0.50 cmc>=2` artwork mode: 123→21,048 against true 123, a 170x regression) — the shipped fix instead layers `est.result.card`/`.artwork` as a tightening-only `.min()`, independently reproduced: both motivating queries now exact (`cmc>=8 power<=2` artwork 15→13, `cmc<=1 power>=1 tou>=1` artwork 1993→1400), and the regression scenario stays correct (123) on both wheels |
| 53 | `PriceJointTable`: a quantile-bucketed 2D `(usd, eur)` joint (64 buckets/axis, tie-safe construction — never splits a repeated price value, never a degenerate bucket), sparse `HashMap<u32, SpaceTotals>` over only the cell pairs that actually occur, linear-scanned at query time ("any overlap counts fully", no boundary interpolation). Two call sites: a standalone whole-And fold (usd+eur alone) and a new `by_class` special case feeding one combined unit into the existing independence pairing loop (usd+eur + something else) — both `Candidate::Estimate`, never `Exact`. `tix` deliberately untouched (r=0.336, weak) | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 66,378 shared rows (fresh seed): 200 plan-choice flips (0.3%), 100% confined to `unsafe:usd+eur` (690/900, 76.7%); `unsafe:usd+tix`/`unsafe:eur+tix` and every other shape show zero flips; `root=leaf`/`root=or` both 0.0%. Ratio diagnostic: mean abs-log-ratio −0.010 (95% CI excludes 0) — "B is MORE accurate" | see "Round 53" narrative below — validated BEFORE scoping via a real Pearson-correlation check (usd↔eur r=0.877, usd↔tix r=0.336) and a Python 2D-histogram simulation; independently re-verified after merging: all five worst-tail queries land at 1.01-1.24x (was 83-186x). A real, measured inefficiency found by the implementing agent (both call sites firing redundantly for a bare 2-leaf query with nothing to pair against) was fixed before merge, not deferred — see that section for the numbers |
| 54 | Generalizes `PriceJointTable` (Round 53) past its `usd`×`eur`-only hardcoding to all three currency pairs: `price_joint_usd_eur`/`_usd_tix`/`_eur_tix`, built by the same closure-parameterized `build_price_joint_table`, dispatched via one shared `price_joint_table_for`/`resolve_price_joint_pair` helper replacing two hand-rolled `match` arms. `PRICE_JOINT_BUCKETS` (64) reused unchanged for all three — re-checked directly against the real corpus, not assumed | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 66,378 shared rows (fresh seed): 227 plan-choice flips (0.3%), 100% confined to `unsafe:usd+tix` (108/900, 12.0%) and `unsafe:eur+tix` (119/900, 13.2%); `unsafe:usd+eur` and every other shape show zero flips; `root=leaf`/`root=or` both 0.0%. Ratio diagnostic: mean abs-log-ratio −0.022 (95% CI excludes 0) — "B is MORE accurate" | see "Round 54" narrative below — surfaced by a fresh full-corpus survey run specifically to check what emerged once Round 53 stopped dominating it; validated the same way (Pearson r + a real joint-histogram simulation) BEFORE scoping, closing a real gap the design doc's own historical calibration work had found beneficial but never actually shipped. Independently re-verified: `usd`×`tix`/`eur`×`tix` land at 1.00-1.92x (was 42-87x); one real discrepancy between my own preliminary Python simulation and the shipped Rust result was investigated and resolved as a bug in MY OWN script, not the implementation — see that section for the full account |
| 55 | `(subtype, subtype)` exact top-N table (`SubtypePairTable`), the first mechanism in this arm to pair a subtype against ANOTHER subtype rather than against `set:X`/`c:X`/`id:X`. Two departures from `SetSubtypeTable`'s own shape, each validated against the real corpus before scoping: top-N membership is the UNION of the tie-inclusive top-256 in each of the three spaces independently (not card-count-ranked alone), and `rest_max` is a real per-space TRIPLE consumed natively rather than one card-space scalar scaled by a global reprint ratio. Query time: a residual exact-hit scan over every unordered pair of subtype leaves (`Candidate::Exact`), plus a single-pair-only capped-independence fallback (`Candidate::Estimate`, printing-space-native) | kept | n/a (not this doc's own metric) | `nway_estimate_truth_survey.py --compare`, 55,833 shared rows (fresh seed 7, independently re-run by me): 219 plan-choice flips (0.4%; 0.5% of `root=and`), 100% confined to `same_family:type+type_realistic` (116/750, 15.5%) and `same_family:type+type_disjoint` (103/750, 13.7%); every other shape and both `root=leaf`/`root=or` show zero flips. Mechanism coverage for both `same_family:type` shapes 0% → 100%. Ratio diagnostic: mean abs-log-ratio 0.191 → 0.183 (B−A −0.008, 95% CI [−0.009, −0.007]) — "B is MORE accurate". Zero-true-count hit rate 74.0% → 79.5% | see "Round 55" narrative below — the design (union cutoff, triple `rest_max`, keep `min(indep, rest_max)`, N=256) was settled against real corpus data BEFORE scoping, including a direct check that N is NOT sized by the `StreamedSelect`/`GatheredScan` transition. The implementing agent hit a real regression the existing suite caught and fixed it by MOVING the estimate-class fallback later in the arm — an ordering constraint worth reading, not a detail |
| 56 | Anchored independence for `ColorCmcTable`, the second anchor mechanism after Round 50's: that table's own EXACT `(color\|identity, cmc)` joint times a single residual `IndepClass::Price` leaf's own solo rate, folded as `Candidate::Estimate`. Round 50's inline closures hoisted into one shared `anchored_price_residual`/`anchored_leaves_for` both anchor sites call. NO fudge factor (swept and rejected), and deliberately NO `mark_covered`, inheriting `ColorCmcTable`'s own measured reason | kept | n/a (not this doc's own metric) | The routing-relevant metric this round exists for — `root=and` rows straddling the 1,024 `STREAM_MIN_MATCHES` boundary with >=200 absolute AND >=10% relative error, independently re-run by me at seed 7 over 39,411 shared and-rows: **1,016 -> 880 (-13.4%)**, over-side 845 -> 704 (-141), under-side 171 -> 176 (+5). 141 fixed, only 5 newly routing-relevant (all moderate under-shoots, pred 845-915 against truths 1,090-1,239). 146 plan flips, **every one in the intended direction** (145 `StreamedSelect -> GatheredScan`, 1 `PrintingCompose -> StreamedSelect`, zero the other way). Entirely confined to `star:identity+cmc+usd` (95 -> 21) and `star:color+cmc+usd` (78 -> 16); every other shape zero delta. Monotonicity confirmed empirically: 1,229 predictions changed, **0 increased** | see "Round 56" narrative below — validated BEFORE scoping (median 1.97x -> 1.01x on a 70-query random sample of the full shape population, deliberately not the straddling tail), including a factor sweep that **rejected** the fudge factor on real data. A pre-existing test's expected number legitimately changed; a zero-prediction case was checked directly against the empty-conjunction short-circuit risk |
| 57 | `LegalityDateTotals`: an EXACT per-`(format, status)` prefix sum over the `released_at` value axis (924 distinct dates), answering `f:X` × any date range in printing space by one subtraction. Keyed by the existing `legality_totals_key`; pruned by a 1,024-printing selectivity floor (`PairTotals`' own principle) AND by a second rule the plan didn't anticipate — drop any key covering the whole date index, which removed 9 phantom keys | kept | n/a (not this doc's own metric) | Routing-relevant straddles (>1,024 boundary, >=200 abs, >=10% rel), agent's sweep at seed 20260903 over 47,118 `root=and` rows: printing 463 → 452 (**−11**, all over-side), card 309 → 306 (−3), artwork 305 → 297 (−8); ALL 1,077 → 1,055. **Every one of the −22 is in `unsafe:legality+released`** (50 → 28); no other shape moved a single straddle. Target shape ratio, printing: median 1.02x → **1.00x**, p90 3.64x → **1.00x**, max 16.81x → **1.00x**. Coverage 0/900 → 813/900. 46 plan flips (0.1%), all in the target shape, and among those rows the count sitting on the wrong side of 1,024 went **31 → 8** | see "Round 57" narrative below — the investigation retracted its own premise twice before landing, an unplanned 9-phantom-key finding, and one honest cost: 173 card/artwork ratio regressions whose mechanism I verified and whose fix is the next round |
| 58 | Splits every space into two independent channels — `SpaceMeasure { guaranteed, estimate }` — so a PROVEN bound and a BEST GUESS stop competing for one `.min()` slot. `Candidate::Exact` feeds `guaranteed`, `Candidate::Estimate` feeds `estimate`; consumers needing soundness read `guaranteed`, consumers needing accuracy read `estimate.min(guaranteed)`. `exact_domain` retained as its own `ExactDomain` type (a stronger cross-space same-set claim, not a synonym). **Phase 2 (the planned `COMPOSE_CARD_ESTIMATE_BIAS` skip) was measured to FAIL and deliberately NOT taken** | phase 1 kept, phase 2 rejected | n/a (not this doc's own metric) | **Byte-identical, verified at three independent seeds**: the agent at 64,581 (seed 20260958) and 64,563 (20260903) rows, and me at 54,336 shared rows (seed 4242) — zero differences on `predicted_matches`, `picked_plan`, `and_mechanism`, and additionally `count_source`/`true_total`/`n_plans_ran` in my run. Row sets matched exactly. So every straddle count and plan choice is unchanged by construction. `cargo test` 290 debug / 287 release (+6 tests). Timing: a flat **+25 to +110 ns** per `compose_printing_estimate` call (~1% on queries with real work, +8.9% bare leaves, +13.7% `Or`), measured interleaved A/B/A/B rather than by canary | see "Round 58" narrative below — the phasing caught a real `Or`-arm fold bug that would have been invisible in a blended round, and phase 2's rejection corrected a false premise **in my own plan** |
| 59 | Makes `guaranteed` honest, enforcing "a source may claim a bound only if its number is a real count of a real set". **Three** leaf arms demoted to estimate-only (`FilterExpr::Legality`, the broadcast/devotion arm, and — found by auditing every arm rather than the two the plan named — the bare cmc/pow/tou `bare_numeric_field_count` branch), all three reporting `card_count * n_printings / n_cards`. Two mechanisms promoted via a new `Candidate::PrintingBound` variant: `LegalityDateTotals` (exact prefix-sum subtraction) and `PriceJointTable` (any overlap counted in full, hence a structural over-count). Plus the `And` arm's seed fixed — see below, without it the round does nothing above a single leaf. Plus a standing soundness check and the long-standing release-clippy error | kept | n/a (not this doc's own metric) | **Byte-identical**, independently verified by me at seed 777 over 54,321 shared rows: zero differences on `predicted_matches`/`picked_plan`/`and_mechanism`/`count_source`/`n_plans_ran`/`true_total`/`ratio`/`abs_log_ratio` **and on the entire `and_trace` dict**; agent's own run agrees at 64,605 keys. So the straddle table is empty in both directions and all three `unique` values. `cargo test` 299 debug / 296 release (+9). **`clippy --all-targets -- -D warnings` clean in BOTH profiles for the first time in this arc.** New `scripts/check_bound_class_soundness.py`: 5,553 bound-class candidates over 12 mechanisms in my run, none below truth | see "Round 59" narrative below — **two of this round's plan's claims were wrong and are corrected there**, plus the `best()`-laundering rule that generalizes beyond this round |
| 60 | `and_trace` reports BOTH channels. `AndTraceLeaf`/`AndTraceNode::{Leaf,Op}` carry a bare `SpaceEstimate`, `AndTraceGroup` an `Option<SpaceEstimate>` (`Some` exactly when `hit`, so `hit == spaces.is_some()` holds by construction). Channels derived once at the fold via a new `Candidate::spaces()` matched arm-for-arm against `fold_candidate`, not hand-written at ~17 trace sites. Python boundary flattened and strictly additive: `card`/`printing`/`artwork` keep today's `best()` values, `{space}_guaranteed`/`{space}_estimate` added beside them, absence always `None` and never `0` | kept | n/a (not this doc's own metric) | **Behaviour-neutral, verified by me at seed 6060**: 15 semantic scalar fields over 54,279 shared rows, **0 differing**; `and_trace` with the six added keys stripped, **0 differing** (the whole-dict comparison Rounds 58/59 used now differs by design, since keys were added — not a regression). Fidelity across the whole run: **673,776 space-slots, 0 violations** of `{space} == min(guaranteed, estimate)`. `cargo test` 302 debug / 299 release, clippy clean both profiles. `check_bound_class_soundness.py` reports 5,480 bound-class candidates none below truth, and reports the **identical** count whether reading the new channels or falling back to its name map — the cross-check passing | see "Round 60" narrative below — one deviation (Independence's card/artwork estimates), a real diagnostic-path cost with the probe that isolated it, and the first direct look at what the channels actually contain |
| 61 | The `Legality` leaf stops guessing: `compose_printing_estimate`'s bare `FilterExpr::Legality` arm reads `ValueTotals::legality`'s exact `.printings` instead of `legal_cards * n_printings / n_cards`. Both columns come off ONE `HashMap` row via a new `legality_space_totals` helper shared with `exact_result_total`'s own `Legality` arm, rather than two walks of that function's shape-dispatch prelude. All four statuses are stored (unlike `PairTotals`, which keeps only legal/not_legal), so `banned:`/`restricted:` become exact too. `broadcast` deliberately keeps the scaled figure — a cost bucket, not a cardinality | kept | n/a (not this doc's own metric) | **The acceptance criterion, met in full**: at the checked-in seed 0, `unsafe:legality+released` rows where `LegalityDateTotals` was exactly right and lost the `.min()` fold go **14 → 0** (14/14 recovered, 0 newly outvoted); the shape's exactly-right rows 299 → 314. At seed 61: 13 → 0. Straddles (>1,024, >=200 abs, >=10% rel) are a near-wash and seed-dependent: seed 0 **1,189 → 1,186** (6 fixed / 3 broken), seed 61 **1,253 → 1,260** (4 fixed / 11 broken). 28 plan flips at seed 0 (0.04%), all in legality shapes. Ratio diagnostic +0.000 (seed 0) / +0.001 (seed 61) — nominally "less accurate", and the per-shape split says why (see narrative). `cargo test` 303 debug / 300 release (+1 test), clippy clean both profiles, `check_bound_class_soundness.py` green (6,655 candidates, none below truth). Timing **−5.7%**: `and_estimate_ns` p50 on 8,247 legality-bearing queries 3,625 → 3,417 ns, against a 31,104-query no-legality control subset flat at 1,917 ns in both builds | see "Round 61" narrative below — the leaf-level error table for all 23 formats, the measurement trap that nearly hid a +9.3% regression, and the shapes that got worse for a structural reason worth queueing |
| 62 | Replaces three presence/equality proxies in `acquire_plan_features` with explicit structural signals, in two separable commits. **Part 1**: the two card-trust gates (`card_invariant_domain_exact`, and the `is_and` narrowing exemption) read `est.result.card.guaranteed` instead of `.best()` — "came from a trusted exact source" is `guaranteed`'s post-Round-59 definition, while `best()` is the ACCURACY read and answers a different question. **Part 2**: a new `ComposeEstimate::printing_tightened` bool, set where a fold actually lowers `result.printing` off its per-leaf seed and propagated through both the `And` and `Or` folds, replaces `est.candidate.printing() == est.result.printing()` at its two consumers. Derived as one `!=` of the SAME field against its own seed, both channels — not threaded as a `&mut bool` through `fold_candidate`'s ~20 call sites, since `SpaceMeasure`'s only mutators are monotone | kept | n/a (not this doc's own metric) | **Part 1 is a provable zero-delta**, not merely a measured one: nothing writes `result.card`'s estimate channel anywhere in `compose_printing_estimate`, so `card.best()` and `card.guaranteed` are the same `Option<usize>` at every node; the survey diff confirms zero rows moved. **Part 2 is small and one-directional**: the new flag disagrees with the retired test on **123/39,461 (0.3%)** curated and **201/44,396 (0.45%)** random rows, **every one `old=False → new=True`** — i.e. only ever finding a tightening the number comparison was blind to, never losing one. 6 of 252 replayed rows move, on 3 queries, with **zero plan flips anywhere**; `bench_pairwise_ordering` unchanged, `bench_feature_accuracy` 0 cells changed verdict. `cargo test` 303 debug / 300 release, clippy clean both profiles, `check_bound_class_soundness.py` green. Timing flat against both a same-build canary and a no-`And` control subset | see "Round 62" narrative below — the 6 moved rows are all REGRESSIONS and are itemized there, the obvious repair was measured and is catastrophic, and **the round's stated justification (in my own plan) was wrong** in a way that changes what item #3 costs |
| 63 | Two exact numbers that existed and were being discarded, in two commits. **Part 1**: the bare cmc/power/toughness leaf arm stops scaling a card count by the corpus-average reprint ratio. New `NumericSpanTotals` — a per-distinct-value exclusive prefix sum of `SpaceTotals` over each field's existing sorted `NumericIndex` — answers any bare comparison with two `partition_point`s and one subtraction, exact in all three spaces, both channels. ~570 bytes per field (these fields have ~20-40 distinct values; a per-entry prefix would have been ~250 KB). Retires the last of the three reprint-ratio arms Round 59 demoted. **Part 2**: `pair_bounded_min` returns a `PairBound` triple instead of one printing `usize`, so `PairTotals`' exact card and artwork columns reach `result.card`/`.artwork` (`guaranteed` only, mirroring `Candidate::Exact`) instead of being fetched by the trace's `get_all` and thrown away. ARCHIVE_FORMAT_VERSION → 2026090401 | kept | n/a (not this doc's own metric) | **Part 1's accuracy, against ground truth**: `cmc=0` 3,699 → **11,948** (true 11,948), `cmc<=1` 13,159 → **21,584** (true 21,584), `pow=6` 1,930 → **1,976** (true 1,976). **Part 2 retires Round 62's three regressed rows structurally and lands them tighter than Round 61 had them** — `eval_domain` now EQUALS the realized `cards_visited`: 216/480/625 against 1,200/1,200/626, plus `cmc=5 frame:1997` 3,788 → **643** (realized 643). Survey at seed 63, 9,777 shared rows: **20 plan flips (0.2%)**, all `root=and` (0.3%), concentrated in the cmc-bearing shapes (`star:legality+cmc+usd` 9.2%, `safe:cmc+usd` 5.8%); `root=leaf` and `root=or` **0**. Ratio diagnostic mean **0.144 → 0.140** (B−A −0.004, 95% CI [−0.005, −0.003], "B is MORE accurate"); zero-true-count hit rate 79.4% → 79.6%. `check_bound_class_soundness.py` green on both runs (879 bound-class candidates, none below truth). `bench_feature_accuracy` flagged cells **62 → 60**, and the two that cleared are exactly `eval_domain <PrintingCompose> / card` and `eval_domain <compose Gather> / card` with no new cells flagged. `bench_pairwise_ordering` percentages hold exactly (87/95/95/99/99/100%). `cargo test` 303 debug / 300 release, clippy clean both profiles. Timing flat-to-faster against a control subset | see "Round 63" narrative below — the obvious implementation of Part 1 was measured at +186% and REJECTED, and Part 2's first version shipped a plan-flip regression that 303 passing tests did not catch |
| 64 | Backports Round 55's two ideas to the three older dimension tables (`set`/`colors`/`identity`), retiring the older convention. The union-of-3-spaces cutoff replaces ranking by `.cards` alone, and `rest_max` becomes a per-space `SpaceTotals` over the actual excluded set instead of one card scalar. `SubtypePairEstimate` then works NATIVELY in printing space (`dim_printings * sub_printings / n_printings`, capped by `rest_max.printings`) instead of a card-space product scaled by the corpus-average reprint ratio — the same idiom Rounds 61 and 63 deleted from two leaf arms. Needs no new index: both printing marginals are already exact and available. Deletes `top_n_and_rest_max`, which had no callers left; its Round 47 "include every tie" rationale migrates into the surviving helper and its four guard tests are retargeted rather than deleted. ARCHIVE_FORMAT_VERSION → 2026090402 | kept | n/a (not this doc's own metric) | **Measured over 750 real table-MISS pairs against ground truth** — the population this round exists for: ALL median **1.309 → 1.000**, p90 **8.25 → 6.00**, routing-relevant **3 → 1**; `set` median 0.900 → 0.586 with p90 6.00 → **2.00** and max 18 → **6**; `colors` median 1.085 → 1.000; `identity` median 1.466 → 1.284, max 628 → 613. **The standard survey shows ZERO plan flips and no detectable ratio change** over 9,777 rows — its shape catalog barely generates (dim, subtype) pairs that MISS the table, so the win is real on a population the survey does not sample. What the survey does show is the union cutoff working: `SubtypePairIndexes` exact hits **38 → 41** candidates (32 → 35 rows), `SubtypePairEstimate` 67 → 61. Soundness green both runs (879 → 882 bound-class candidates, none below truth). `bench_feature_accuracy` flagged cells 59 → 57, none added. `cargo test` 303 debug / 300 release, clippy clean both profiles. Engine build+load unchanged (interleaved 5-rep min ratio 1.0042, median 1.0015) | see "Round 64" narrative below — why the survey could not see the win, why `set`'s median MISLEADS (91 of 168 rows land closer to truth and false zeros drop 72 -> 49, while the median falls purely because fewer rows are pinned at 0), and a drift artifact that first read as a +4.6% build regression |
| 65 | An inclusion FLOOR on every top-N pair table: any pair whose printing count is at or above `STREAM_MIN_MATCHES / PAIR_INCLUSION_FLOOR_DIVISOR` (divisor 2, so half the boundary) is kept regardless of rank, on top of the existing rank cutoff rather than instead of it. Turns "the fallback estimate cannot flip a routing decision" from an observation about one corpus into a **proven invariant**: `SubtypePairEstimate` reports `min(indep, rest_max.printings)`, so forcing every big pair INTO the table forces `rest_max.printings` below the floor — and since `printings >= cards` and `printings >= artworks` for any pair, that bounds card and artwork space for free. Derived from the live knob, not hardcoded, and asserted at the point of establishment via `debug_assert`. ARCHIVE_FORMAT_VERSION → 2026090403 | kept | n/a (not this doc's own metric) | **The bug it fixes**: `identity`'s `rest_max.printings` was **1,060** against a boundary of **1,024** — the CAP ITSELF on the wrong side — and all **9** of that dimension's routing-relevant misses read the cap exactly (`id:ubr t:Elf` estimated 1,060 against a true 164). After: cap **509** (2.01x margin), exact hits 5.1% → **10.6%**, **routing-relevant misses across all three dimensions 1 → 0**. `set` and `colors` are **byte-identical** — no set pair reaches the floor (its largest is 503 printings) and colors' 48 qualifying pairs were already kept. Cost **+325 pairs** (identity 286 → 611) = **+8,152 bytes of archive (+0.011%)**; build+load unchanged (interleaved 4-rep min ratio 0.9931). Survey at seed 63 over 9,777 rows: zero plan flips, no detectable ratio change, zero-true-count rate unchanged. Soundness green (883 candidates, none below truth). `cargo test` 305 debug / 302 release (+2), clippy clean both profiles | see "Round 65" narrative below — a space-mismatched safety claim that had sat in the code since Round 34, and why the cheaper zero-cost variant was declined |
| 66 | **First COST-FEATURE round, not an estimator round.** `gather_composed_page` takes one of three per-card arms and only two walk the candidate's `start..end` span: printing mode pushes every set printing, and the grouping arm (artwork always, card under a non-default prefer) must score every printing per group. The card/**default**-prefer arm breaks at the first set printing (`(start..end).find(is_set)`), because printings are stored prefer-descending so the first set one IS the representative. `compose_scan_printings` charged `printing_matches * COMPOSE_GATHER_SPAN_PER_MATCH` (1.47) in **all three**. Now charges `eval_domain` — the candidate-card count — in the early-break arm only. Deliberately NOT the sibling `groups` predicate, which agrees on card mode but would wrongly strip the multiplier from printing mode. Also wires `--n-queries` into `bench_feature_accuracy.py` (`Budget(sample=N)`, already supported and never exposed) | kept | n/a (not this doc's own metric) | **The direct property, identical population both sides (n=93 `unique=card`/`prefer=default` compose-Gather rows graded against realized `printings_examined`)**: p50 **5.040 → 1.000**, p10 0.79 → 0.16, p90 10.84 → 3.11, mean 7.38 → 1.48. Single rows exact — `f:gladiator`/card charged **80,654** against a realized 15,131, now **15,131** (verified independently). **Controls byte-identical**: card/`prefer=newest` (n=105) and printing/default (n=8) match field-for-field. `bench_feature_accuracy` at matched populations (**112,129 rows both sides, exactly equal**): 146 cells / 59 flagged before AND after, **zero new, zero newly flagged, zero unflagged**; exactly 7 cells move, all `compose_scan_printings`, each at equal n. Ordering holds (87.0→87.1, 95.8→95.7, …). Flips: **3 of 37,771 (0.008%)** on a gather-reaching population, all `GatheredScan → PrintingCompose`, dispatch-priced **2 faster / 1 slower, net +48.67 µs**. Soundness green (6,695 candidates). `cargo test` 305 debug / 302 release, clippy clean both profiles (verified by me — the agent omitted them). Timing flat: same-build controls exceed either cross-build read and the cross-build sign flips | see "Round 66" narrative below — a pinned median that needed slicing rather than smoothing, and the refit this round deliberately did NOT do |
| 68 | **First EXECUTOR round in this arc — removes real work rather than improving a prediction.** `walk_grouped_page` stepped the permutation and, per card, bit-tested the card's WHOLE printing span and called `prefer_score` on every set printing. But printings are stored prefer-DESCENDING within a card (`from_rows`' load-time sort, ties by illustration_id then scryfall_id), so under `Mode::Card` + `Prefer::Default` the FIRST set printing already IS the chosen representative — every later `prefer_score`, the `touched`/`group_best` bookkeeping and the post-loop group emit were waste. Now takes the same `(start..end).find(is_set)` early break `gather_composed_page` and `push_card_matches` already used. `printings_examined` moved off its unconditional pre-match `(end - start)` to the EXIT POSITION, per arm, with no per-iteration add (the project's hot-path instrumentation rule). Scoped to the only LIVE walk: `walk_card_page_via_popcount_skip` sits behind `COMPOSE_SIGMA_ENABLED` (defaults 0) and the printing/artwork popcount-skip walks have zero production call sites | kept | n/a (not this doc's own metric) | **Row identity is the gate and it passed twice independently.** Agent: 7,776 cells / 374,712 rows byte-identical (3 distinct-ons x 4 prefers x 3 sort cols x both directions x 6 page points x 6 densities), debug AND release, plus 9,000 cells / 235,692 rows matching by sha256 from routed dumps. Me, separately: **21,912 compose cells including 5,920 `Perm`-paging, 750,580 rows, identical sha256** over printing identity — my first attempt used `orderby=rarity` and hit **0 Perm cells**, so it proved nothing until the orderby was varied. **Realized time**: `PrintingCompose` `ns_loop` p50 **0.707** (3,896 → 1,979 ns), interleaved over 6 block pairs, with GatheredScan/StreamedSelect/PlanePopcountOrder/CardRangePopcount controls all reading p50 **1.000**. Plan choice unaffected: **0 changes over 66,414** survey observations, and 0 `paging_taken`/`picked`/`result_total` flips over 595 exact-population paired compose cells. `cargo test` 305 debug / 302 release, clippy clean both profiles (verified by me) | see "Round 68" narrative below — why the end-to-end number is much smaller than the loop number, a density regime the router never reaches, and a cost-feature consequence that is now a queue item |
| 69 | **Measurement only.** Grades StreamedSelect's two never-graded cost drivers (`perm_walk_span` via the walk term, `stream_scan_units`) against realized counters, answers whether the permutation-less sort columns need their own cost branch, and re-measures the compose walk's per-orderby clump after Round 68 invalidated it | n/a | n/a | **No instrumentation round was needed — item 1's stated blocker was wrong.** Both realized counters (`perm_steps`, `printings_examined`) already exist and are already published. Walk term: pooled median **1.023**, spread 9.6x, split by sort column into **1.9x** (`name`) to **38.8x** (`cmc`) at flat medians (0.918-1.183) — so a per-orderby scalar cannot help in CARD space, and no existing feature predicts the residual (max \|r\| 0.12). `stream_scan_units` is **bimodal**: p25/p50 exactly 1.000, p90 **11.8** (printing 16.7x, artwork 14.0x). Both cost GATES are correct — all 83 walk-gate disagreements (2.79%) are the estimate crossing `STREAM_MIN_MATCHES`, and 720 of 778 scan-gate ones (92.5%) are the plan returning before any loop. `rarity`/`usd` need **no** cost branch: they have no permutation and `streamed_select_applicable` drops the plan from the argmin (offered 0/12 vs 12/12 for `name`/`cmc`). Compose's `Perm`/`OrderbyWalk` shared arm is likewise **correct** (residual medians 1.277 vs 1.449) — but its per-column medians span **0.925-3.579** against one shipped `WALK_LENGTH_BIAS` of 1.45, which CONFIRMS item 5 | see "Round 69" narrative below — a blocker that was already unblocked, and a stale table that validated its own replacement |

### Round 85 (measurement only) — the mis-pick tail is ONE bug, and it is the empty page

Round 84's miss margin has a p99 of 19.1x and a max of 29.7x. `bench_pick_quality.py --worst N
--worst-by ratio` dumps those with each plan's PREDICTION beside its measurement, and **every single
one of the top ten has the same signature**:

```
  lost  41.0 us   measured 27.61x   predicted 0.00x   headroom 23.98x
    set:tmp frame:1993
    printing/cubecobra/off=100/prefer=newest  [printing_compose]  matches=350 eval_domain=196
      PICKED GatheredScan     predicted    8.0 us   measured   42.5 us   p/m 0.19
      BEST   PrintingCompose  predicted    inf us   measured    1.5 us   p/m  inf
```

**`PrintingCompose predicted = inf` on all ten.** That is `ComposePaging::Decline => INFINITY`, which
exists to keep a plan that will refuse out of the argmin. But the executor does not refuse — it takes
**`EmptyPage`** and returns in ~1.4 us, while the two materializing plans grind the whole candidate
set producing nothing (42.5 us and 38.5 us on that query, `result_total = 0` for all three).

Verified directly rather than inferred: forcing all three plans on `set:tmp frame:1993` gives compose
7 trials, **0 declines**, `paging_taken = EmptyPage`, min 1,417 ns. So this is a genuine mis-pick, not
a decline being mistimed as a run — the first thing to rule out, since a refusal is cheap for the
wrong reason.

**Sized:**

| slice | n | hit% | time-wtd hit% | share of lost time |
|---|---|---|---|---|
| ordinary queries | 4,449 | **95.9%** | 89.7% | 79.4% |
| **empty result or page past the end** | **917 (17.1%)** | **83.8%** | **55.8%** | **20.6%** |

So one query shape — 17% of the sample — carries **a fifth of all routing loss**, and its
time-weighted hit rate is **55.8%** against 89.7% everywhere else. Excluding it, the router's hit rate
is 95.9% under uniform.

**This is the same gate Round 80's audit lane found from the other side**, and its significance was
understated there as a footnote. That lane measured `EmptyPage` running in 9.1 us median against a
**64.1 us** median prediction with **1,261 of 1,551 rows priced INFINITY**, and separately found 74
queries where compose WAS picked and then refused after paying the whole build (3.59% of measured
time). Both are the same defect: **`compose_paging` predicts `Decline` from the acquire's ESTIMATE of
the total while the executor branches on the real one.** One direction costs a wasted build; the other
excludes the fastest plan from the argmin entirely.

**Why the ratio tail and the absolute tail are disjoint, and why both are worth having.** The
ratio-worst rows lose 15-41 us each — real but small. The absolute-worst rows lose 120-490 us and have
a completely different signature: **predicted ratios of 0.70-0.98**, i.e. the model saw a near-tie and
lost the coin flip, with the truth being ~2x. Those are `card` mode, `off=100`, `[printing_compose]`,
and they are mis-calibration rather than a gate bug. `--worst-by loss` ranks what to fix for total
time; `--worst-by ratio` ranks what is structurally wrong. Neither list finds the other's entries.

**What would fix the empty-page class** is not a cost-model change: `plan_cost` cannot see that the
page is empty, because `matches` is an estimate and the branch is on the realized total. Either the
decline gate stops returning INFINITY when the fastpath's actual exit would be `EmptyPage`, or the
router gets an exact zero-result test before costing. Both are estimator/dispatch work, not costing —
recorded here rather than scheduled.

### Round 84 (measurement only) — the router is right 94-96% of the time, and its misses are ties

Twenty rounds of this ledger measure how wrong the cost MODEL is. None of them asked the question in
front of that: **does the router pick the right plan, and when it does not, did the miss cost
anything?** `bench_pick_quality.py` answers it. Both are needed — a model can be badly calibrated and
still route correctly, which turns out to be roughly what is happening.

Every plan is timed inside ONE `explain_analyze` call, so the picked plan and the plan that should
have won are measured common-mode; that is what makes microsecond differences readable against a
~9% cross-run noise floor.

| | uniform | realistic |
|---|---|---|
| picked the fastest plan | **93.7%** | **95.9%** |
| picked within 1.09x of fastest (the noise band) | 95.0% | 96.9% |
| time-weighted hit rate | 88.5% | 91.6% |
| **total time lost to mis-picks** | **3.5%** (6.0 of 172.4 ms) | **2.8%** (3.2 of 113.6 ms) |

**And the misses are concentrated exactly where they are cheap**, which is the property worth having.
Comparing HEADROOM — the runner-up's time over the winner's, i.e. how much was on the table at all:

| | uniform | realistic |
|---|---|---|
| headroom on **MISSED** queries (p50) | **1.380** | **1.278** |
| headroom on **HIT** queries (p50) | **3.509** | **3.740** |

The router hits when the stakes are high and misses when the plans are close — a **2.5-2.9x**
difference in what was available to win. Miss margin itself is p50 **1.425** (uniform) / 1.288
(realistic): the median mis-pick costs ~30-40%, of a query that was going to be cheap anyway.

**The tail is real but small and concentrated.** Miss margin p99 is 19.1x (uniform) / 10.7x
(realistic), max 29.7x — and **the worst 1% of misses carry 13.6% of all lost time** in both modes.
So the remaining routing loss is a few dozen queries, not a systematic bias.

**Where the misses are:**

| slice | hit% (uniform) | share of lost time |
|---|---|---|
| `printing_compose` acquire | 92.5% | **95.5%** |
| `candidates` acquire | 94.9% | 4.5% |
| `plane` / range acquires | 99.7-100% | **0.0%** |
| picked `StreamedSelect` | **89.1%** | 32.5% |
| picked `GatheredScan` | 93.4% | 37.9% |
| picked `PrintingCompose` | 95.4% | 29.6% |
| `unique=card` | 92.1% | **53.6%** |

The three non-materializing/range routes are essentially perfect and carry **zero** lost time — every
routing problem this arc has is inside the `printing_compose` acquire, and `unique=card` carries over
half of it on a third of the rows. The largest single transition is
`GatheredScan -> PrintingCompose` (116 misses, 34.1% of lost time), with only 6.9% of those inside the
tie band — so that one is a genuine mis-pick rather than a coin-flip, and it is the same cell Round 83
found the build charge would have fixed at the cost of breaking more elsewhere.

**How to read this against the rest of the ledger.** Rounds 77-83 measure cost-model error mass in the
thousands of log-units and find terms 2x and 3x wrong. This says the routing consequence of all of it
is **3.5% of dispatch time**. Both are true: the model is inaccurate in ways that mostly do not change
an argmin, because the argmin only needs an ORDERING and the plans are usually far apart. That is why
several rounds here shipped correct fixes worth zero flips, and why the honest bar for future cost
work is this table rather than a feature cell.

### Round 83 — step 3 does not ship, and it would not ship at ANY accuracy

The end of Round 80's refit sequence, and the answer is no. An implementing lane and an adversarial
lane ran in parallel; the adversarial one settled it.

**Even a PERFECT build term loses.** Router simulation over one set of measured times, with the Python
arm mirror reproducing the engine's own `picked` on **100.00% of 12,000 rows** in every run (positive
is worse):

| router | uniform x3 | realistic x2 |
|---|---|---|
| refit-executor, no build charge | +0.08 / +0.09 / +0.22% | -0.08 / +0.24% |
| refit + modelled build (the design) | **+1.15 / +1.12 / +1.03%** | **+0.66 / +0.78%** |
| refit + **ORACLE realized `ns_prepare`** | +0.35 / +0.32 / +0.49% | +0.38 / +0.19% |
| shipped arms + modelled build (Round 80's variant) | +0.75% | +1.49% |
| **shipped + build, GatheredScan ONLY (Round 81's redirect)** | **+1.18%** | **+2.40%** |

So improving `materialize_cost` from |ln| 0.78 to **0.00** removes only ~2/3 of the damage from a
change that is negative to begin with. **This is not a "wait for a better build model" situation**, and
the counter-plus-acquire-time-estimator work Round 82 deferred would not change the verdict.

**Round 81's "charge it to GatheredScan only" is the WORST variant measured**, which follows from the
build being plan-independent: `prepare_candidates` takes no plan argument, its result is documented as
the shared P3/P4 product, both executors consume it read-only, and realized `ns_prepare` for
StreamedSelect / GatheredScan **on the same query** is p50 **1.000** on every route in every run.
Charging an identical build to one of two plans is a deliberate mis-model.

**The feared off-target blast radius is ~zero, for a structural reason nobody spotted.** On a
`candidates` acquire `PlanScope::Candidates` admits only StreamedSelect and GatheredScan, and
`materialize_cost` is identical for both — so **it cancels exactly in the argmin**. Per-acquire delta:
`candidates` +0.09%, `plane` +0.03%, `card_range_popcount` +0.00%. The harm is **entirely on the target
route** (+1.43% uniform), by exactly the mechanism Round 81 named — the charge pushes rows off the
materializing plans onto PrintingCompose (1,698 -> 1,919 rows) and those moves lose.

**Three premises in the task briefing were wrong, and the corrections matter beyond this round:**

1. **Picked-row ratios are a SELECTION statistic and were used as if they were the fit population.**
   Over every row a plan ran on, `GatheredScan / printing_compose` reads **0.989** against its own
   executor — dead calibrated — against the 1.149 the picked-row table shows. The refit therefore moves
   coefficients **UP**, not down. And the two modes disagree in **sign** (realistic GS 1.358 / SS 1.156,
   the opposite ordering). Over-charge is route-, mode- and population-dependent.
2. **The absorption is inverted.** Measured as the gap between fitting against `plan_self` and against
   the executor, over realized prep: **GatheredScan 0.969, StreamedSelect 0.579.** GatheredScan is the
   absorber, almost exactly — the briefing said StreamedSelect. Confirmed independently by the
   implementing lane as coefficient collapse when the build leaves the target (GatheredScan `FIXED`
   **-82%**, StreamedSelect `FIXED` -12%).
3. **A coefficient quoted without its fit target is meaningless here.** The refit numbers quoted in the
   briefing (`FIXED` 435) are the `plan_self` target; the executor target gives **73.20**. The two
   denominators differ by **5.5x** on that term. Both lanes independently flagged this.

**Salvaged and shipped from the two lanes, all of it independent of the dead design:**

- **`fit_cost_model` was fitting every arm against `plan_self_ns`**, which adds `ns_prepare` on a
  `RANGE_ACQUIRES` route — so the two materializing arms were being *taught to absorb* a build they
  have no term for. New `fit_target_ns` returns the executor sum for those two and keeps
  `plan_self_ns` for everything else. **That exception is load-bearing**: `CardRangePopcount`'s arm IS
  its dispatch build, reading 1.13 against `plan_self_ns` and **3.48** against the executor, so a
  blanket switch would have manufactured a 3x defect.
- **`fit_cost_model`'s own sampling loop had the `prefer` bug** this project has hit four times: it
  called `explain()` with no `prefer` and `explain_analyze(prefer="default")`, agreeing only by
  coincidence of pyo3 defaults over a parameter the acquire reads. It now drives
  `costbench.iter_samples`. It also gains `--n-queries` (replacing the `--seconds` bound, so two runs
  draw an identical stream — the gap Round 71 recorded and did not close), `--vary-prefer` and
  `--mode`.
- **`bench_plan_execution_ab` gains `report_flip_pricing`**, pricing each flip within one run and
  printing both runs' columns so their gap IS the drift. Exercised: A/A gives 0 flips; a deliberately
  perturbed build gives 17 flips priced +0.03 ms with the columns agreeing to 0.01 ms.
- **`bench_picked_ratios_by_route`'s three columns do not compose, and the table read as if they did.**
  A 12.6% prep share cannot reconcile two ratios 1.76x apart — they are independent medians over
  differently-ordered rows. Both the docstring and the printed footer now say so; an agent nearly
  derived a coefficient by dividing one column by another.

**Recorded, not fixed** (each would need its own round): `plan_cost` carries no acquire, so any
`materialize_cost` added there is charged on `plane` acquires where dispatch never calls
`prepare_candidates` at all — and **the obvious gate is wrong**, since `choose` receives a `PlanScope`
but `declined_sibling_fastpath` compares against `PlanScope::Candidates` *before*
`prepare_candidates` has run. The gate would have to ride the `Prep`.
`STREAM_REDO_SCAN_PER_ROW_NS` is an alias of `STREAM_SCAN_PER_ROW_NS` in the engine but two free
columns in `design_row`; the fit returns 7.27 and 8.29, unreachable while the alias holds.
`PrintingCompose`'s fit is vetoed on this corpus on feature accuracy, pre-existing and unrelated.
And a `cost.rs` docstring citing a historical fit of 21.59/8.19 against today's 39.52/13.11 was
attributed to prefer variation in a confident causal story; measuring it showed prefer moves those
terms **2-6%** and the gap is Rounds 79-82 moving the model underneath. The docstring now carries the
measurement and the correction.

**Where the time actually is.** ~50% of measured time under uniform sits in
`PrintingCompose / printing_compose`, already the best-calibrated large cell at p/self 1.038. No
cost-arm work will move it — that is an executor question, not a costing one.

### Round 82 — `materialize_cost` gets the right shape: it was missing two phases entirely

Step 1 of Round 80's refit sequence, and the prerequisite for step 3. `materialize_cost` graded
median |ln| **1.710** against realized `ns_prepare` (within-25% 8.8%) because
`143 + 4.95 * eval_domain` prices a collect+sort while `prepare_candidates` does **three** things.

**What it actually does**, measured behind a new `prepare-phases` cargo feature (off by default; the
timer compiles to a ZST with empty methods when off, the same precedent and rationale as the existing
`routed-phases`). Median per-row share:

| acquire | med `ns_prepare` | narrow | project | memo |
|---|---|---|---|---|
| `candidates` (n=1,890) | 3,375 ns | **0.85** | 0.04 | 0.01 |
| `printing_compose` (n=2,707) | 3,167 ns | 0.12 | **0.80** | 0.00 |
| `plane` (n=324) | 5,000 ns | 0.00 | **0.99** | 0.00 |
| range acquires | ~1,000 ns | 0.50 | 0.25 | 0.00 |

- **narrow** scales with **index probes, not candidates** — a bare `ExactName` yields 4 candidates and
  costs ~2.4 us (two `partition_point` searches, ~15 cache-missing string compares each). The old
  shape had **no term for this at all**, and it is 85% of the cost on `candidates`, the acquire where
  a lazy materialize actually happens.
- **project** scales with **plane word-ops**: `eval_planes` calls `eval_word` once per word and
  `eval_word` walks the whole expression, so the unit is `PlaneExpr::node_count() * n_cards/64`. A
  bare word count cannot express it — a 1-node `f:modern` and a 5-node `c:bru id:bw` differ 5x.

New shape: `121 + 942 * prepare_nodes + 1.543 * prepare_plane_word_ops + 1.641 * prepare_cands`, fit on
a **calibration half split by blake2b hash of the query string** and graded held-out on a *different
seed*, so no evaluated query has ever been in any calibration half:

| mode | n | old \|ln\| / w25 | **new \|ln\| / w25** |
|---|---|---|---|
| uniform | 5,098 | 1.710 / 8.8% | **0.784 / 15.6%** |
| realistic | 5,496 | 1.994 / 6.4% | **0.730 / 15.0%** |

Per cell (uniform, held out): `candidates` 2.113 -> **0.557**, `printing_compose` 1.440 -> **0.930**,
`plane` 1.765 -> **0.884**, `printing_range_scan` 0.633 -> **0.421**.

**The two modes disagree on the constants, and the choice is recorded rather than buried.** Realistic's
own calibration half wants `PER_NODE` 1650 (against 942) and `PER_CAND` 0.836 (against 1.641), which
would take realistic held-out to 0.536 / 22.9%. The **uniform** fit ships — consistent with every other
`cost.rs` constant and with RANK-by-uniform — and step 3 has the realistic alternative if it wants to
re-decide.

**The pinned-`eval_domain` trap, handled.** `prepare_cands` is `eval_domain` normally, `matches` on the
two acquires that pin `eval_domain` at `n_cards`, and **0 where `narrow_candidates_exact`'s own 3/4
breadth guard will discard the narrowing** (mirrored as `cost::NARROW_BREADTH_DISCARD_DIVISOR`), with a
plane exemption because `prepare_candidates`' `Some(expr)` arm returns `Some(..)` on all three paths.
Necessary because those cells are **bimodal**: `printing_range_scan` splits into a discarded half
(median 188 ns) and a materialized half (median 23.7 us) — **126x**. Charging `matches` to both graded
`card_range_popcount` at 3.80; with the guard, **1.33**. All five variants were compared in one process
over an identical 2,862-row population.

**A briefing error of mine, corrected by the lane.** I listed `materialize_cost` returning 0 for
`CardRangePopcount` as "a second gap in the same function". **It is correct, not a gap** — that plan's
dispatch build IS `build_card_range_bits`, and `plan_cost`'s own arm already charges it as
`scatter_printings * CARD_RANGE_BUILD_PER_PRINTING_NS`. A term here would double-charge the one plan
whose dispatch build is already modelled. Now documented on `materialize_cost` itself.

**No counter added, and the reasoning is the durable part.** A sibling had recommended a narrowing-work
counter. This lane declined: **a counter is only available AFTER the walk, so it can grade the
narrowing term but cannot improve it**, because `plan_cost` reads acquire-time features. Adding one
would have bought instrumentation on a hot path for no accuracy gain. Regressed alone the narrowing
phase reaches median |ln| **1.03** against **1.29** for a bare constant — `prepare_nodes` is a
tree-shape proxy for a per-probe cost whose probes differ by an order of magnitude in kind (2-byte
bigram lookup vs `ExactName` binary search vs range-slice collect), and nothing separates them. **That
phase is the accuracy floor on this term**, and closing it needs a counter PLUS an acquire-time
estimator for that counter — a separate piece of work.

**Routing-neutral, verified rather than assumed**: `materialize_ns` differs on 100% of rows and
`predicted_ns` on **0**, with **0 plan flips** over 3,600 prefer-varied rows here and 4,000 per mode in
the lane. The three new `PlanFeatures` fields are unconditional additions to the acquire path, so they
got their own paired A/B (`bench_query_latency_ab` at its converged 6/30, builds interleaved
A/B/A/B): both cross-build intervals sit **inside** the same-build canary's own drift on a 62 us mean.
The phase timers are deliberately not in that build — with the feature on they inflate `ns_prepare`
itself ~10%, which is why they are gated. `cargo test` 308 debug / 305 release **and 308 with the
feature**; clippy clean in debug, release, and with the feature.

### Round 81 — StreamedSelect's small-total floor, and step 3's precondition is NOT what we thought

Step 2 of the refit sequence Round 80 identified. It found the over-charge, fixed it, and then
established that **fixing it does not unblock step 3** — which is the more important half.

**The 77% is entirely the finish phase, and 96% of that is one flat constant.**
`SMALL_TOTAL_FLOOR_PER_CARD` charges `n_cards * 1.02` = **32.4 us** on the small-total branch; measured
`ns_finish` on 1,085 rows where the executor really took that exit is p10 8.3 / **p50 11.0** / p90
27.5 us.

**Why 1.02 was wrong, and why two prior confirmations agreed with it.** The redo loop does two things —
a `counts[cid] == 0` read on every card, and `card_pass` + `push_card_matches` on the few dozen with a
nonzero count. Charging both on `n_cards` makes the per-card rate a function of the cell's MATCH count,
which a per-card cost cannot express. Re-running `bench_streamed_loop` at `n_cards = 31,724` reads
**0.305-0.332 at its 100-match cells and 0.369-0.478 at its 400-match cells**; the constant's doc cites
1.075-1.250, which are its **600-match** cells. And `fit_cost_model`'s 0.98 is the same conflation from
traffic: that column is literally `n_cards` or 0, **collinear with the intercept, with no second column
to separate against**. So the fit could not identify it either.

The fix drops it to **0.30** (the sweep alone) and adds `STREAM_REDO_SCAN_PER_ROW_NS` over a new
`stream_redo_printings = min(matches, eval_domain) * n_printings / n_cards` — **`redo_examined`'s first
consumer**, and the second column that makes the term identifiable at all. Scored as whole formulas
against measured `ns_finish`: shipped p50 **2.94** / agg 2.16 -> new p50 **1.03** / agg **0.97**, with
the *oracle* `redo_examined` at p50 1.04 — i.e. within noise of what a perfect counter could do.

**The finding that redirects step 3: the over-charge lives entirely OFF the argmin path.** On rows
where StreamedSelect is PICKED the arm was already calibrated *before* this change. Measured
independently here on picked rows, `--mode uniform`, 6,000 queries:

| plan / acquire | n | p / `plan_self_ns` | p / executor | prep share |
|---|---|---|---|---|
| GatheredScan / `printing_compose` | 1,072 | **0.648** | 1.149 | 11.7% |
| StreamedSelect / `printing_compose` | 498 | **0.973** | 1.314 | 26.4% |
| PrintingCompose / `printing_compose` | 860 | 1.106 | 1.106 | 0.0% |
| GatheredScan / `candidates` | 833 | 1.241 | 1.241 | 0.0% |
| StreamedSelect / `candidates` | 213 | 1.197 | 1.197 | 0.0% |

On the 6,398 small-gather rows StreamedSelect loses by p50 **33 us**; removing 22 us leaves it losing
by 10.3 us, still **0-for-6,398**. So this term never routes, and after the change the picked-row
ratios move 0.973 -> 0.989 — i.e. barely at all, exactly as predicted.

**What actually causes Round 80's `StreamedSelect -> PrintingCompose` regression is a denominator, not
a miscalibration.** `plan_self_ns` adds `ns_prepare` on a `RANGE_ACQUIRES` route, so on
`printing_compose` StreamedSelect's shipped rates are ALREADY scored against a measured time that
includes the candidate build — and they read 0.973, correct. Charging the build symmetrically adds its
26.4% prep share on top, pushing it to ~1.24, past `PrintingCompose`'s 1.106. GatheredScan, whose prep
share is only 11.7% and which reads 0.648, moves to ~0.77 and stays under. **That is the +5.58 ms,
mechanism and magnitude.**

**So step 3's plan changes: charge the build term to GatheredScan only, or scale it per plan.**
StreamedSelect's rates already absorb the build on range routes, and no amount of work on its finish
phase changes that. This is what step 2 was for — establishing whether its precondition was met. It is
not, and the sequence needed to know before step 3 spent a round on it.

**Shipped anyway, on the Round 76 precedent**: a strict accuracy improvement (p50 2.94 -> 1.03) with an
unmeasurable routing effect, and the refit argument is stronger here than it was there — the term is
currently *unidentifiable* by `fit_cost_model` (one column, collinear with the intercept), and this
change gives it a second column. **0 plan flips** over 3,600 prefer-varied rows verified here; the lane
saw 0/2/0/2 over four 20,000-query sweeps, all 4 losses, net **+0.099 ms over 35,160 timed queries**
(+0.0028 us/query) against a same-build A/A drift of **+0.48%** on 146 ms — 25-50x under the floor, so
unmeasurable rather than measurably negative. Recorded plainly because 4/4 is a 0% win rate, on n=4.
`fit_cost_model`'s mirror stays **100.0% over 41,551 rows**; joint substitution unchanged at -0.4%, so
no error moved into a feature. `cargo test` 308 debug / 305 release, clippy clean both.

**A briefing error of mine that this lane caught.** I gave it "StreamedSelect over-charges its own
executor by 77%, target 1.767 -> 1.0", quoting Round 80's **pred/executor** column while the lane
measured **pred/`plan_self_ns`** (1.319). Both were in Round 80's table; I quoted one and described it
as the other. `scripts/bench_picked_ratios_by_route.py` is added so the two denominators are always
reported side by side — the gap between them IS the unpriced build, so a ratio quoted without its
denominator makes a plan look calibrated or broken depending on a choice nobody stated.

### Round 80 — three routes at GatheredScan's 78.7%, and they agree: it is not counting

Three agents attacked the same target by three deliberately different routes — add COUNTERS for the
four ungraded terms, interrogate the arm's SHAPE using the phase timings that already exist, and
regress the residual EMPIRICALLY and let the data choose. **All three converged on the same answer
from different directions, and none of them is "the four ungraded terms are wrong".**

**The answer: `GatheredScan`'s error is one term the arm does not have.** On a `Prep::Range` acquire
(`printing_compose`) the router only ESTIMATED, so a materializing winner calls `prepare_candidates`
in DISPATCH. `costbench.plan_self_ns` counts that as real latency via `RANGE_ACQUIRES` — correctly,
`run_query_routed`'s `(plan, Prep::Range(_))` arm really does pay it while the fastpath plans buy
nothing — and `cost::plan_cost` charges **zero** for it. `cost::materialize_cost` exists, is computed,
is published per plan as `materialize_ns`, and is deliberately excluded from ranking.

Three independent measurements of the same thing:

- **Phase oracle** (shape route): making `ns_setup`, `ns_loop` AND `ns_finish` all exact makes the
  model **WORSE** — -3.7% / -6.2% of mass over two seeds, and -19.9% / -22.7% on the compose route
  alone. Making the prepare term exact removes **+50.1% / +50.7%** (compose route +57.9% / +58.6%).
  A perfect model of everything the arm describes is worse than what ships.
- **Zero-parameter ladder** (empirical route), 25,757 GatheredScan rows: shipped mass 22,925 ->
  charging the build at its realized price 11,362, i.e. **50.4% removed**; adding realized counters on
  top reaches 73.7%. That is **59% of all engine cost-error mass** located.
- **Joint substitution** (counter route): swapping GatheredScan's ENTIRE oracle-backed feature vector
  for truth at once removes only **7.5%** of that plan's mass. So ~90% is rate or model form, not
  counting — reached without the build hypothesis at all.

**And it explains the 2.3x acquire split exactly.** Against the executor ALONE the two routes are
identical (pred/exec 1.310 compose vs 1.254 candidates); scored against `plan_self_ns` they read 0.493
vs 1.254. The split was never a mechanism difference. Cleanest proof: ~500 zero-match rows where every
other term is provably zero — executor measured **83 ns**, prediction 42 ns (correct), `plan_self_ns`
**4,104-4,417 ns**, 1.9% of rows carrying 8.4% of the plan's mass at a median 81x under.

**`cost.rs`'s claim that this term cannot change an argmin is false.** It was reasoned about
StreamedSelect vs GatheredScan, but on a `Prep::Range` acquire the competing set also holds the
non-materializing plans — the `PrintingCompose -> GatheredScan` cell the module header already records
at 99% miss and 11% of all routing regret.

**Not shipped, and both diagnosis routes said so independently.**

- Implemented as `routing_cost = plan_cost + materialize_cost` at both ranking sites, it measures
  **net +1.49 ms SLOWER** end to end: `GatheredScan -> PrintingCompose` 100 flips at -4.09 ms, but
  `StreamedSelect -> PrintingCompose` 194 flips at **+5.58 ms**. Cause named: on that route
  StreamedSelect already over-charges its own executor by **77%** (pred/exec 1.767 against
  GatheredScan's 0.981), so adding a real cost makes it lose picks it should win.
- Today's constants are the wrong SHAPE, not just the wrong scale. Graded against realized
  `ns_prepare`, `143 + 4.95 * eval_domain` reads median |log| **1.728** (5.6x, within-25% 5.5%); fitted
  it is ~2,800 + 1.0 (uniform) / ~2,450 + 0.55 (realistic) — fixed part ~18x too small, per-candidate
  rate ~5-9x too large, because the shipped shape prices a collect+sort while dispatch also pays the
  **narrowing walk** that produces the input.
- The populations disagree sharply. Uniform under-predicts (median signed residual +0.299), realistic
  **over**-predicts (-0.246); compose-route prep is 42.8% of measured dispatch under uniform and only
  **12.5%** under realistic. The term removes 1.5% of realistic held-out mass, and wiring today's
  constants unchanged makes realistic **1.5% worse**.

**Correct sequence, which no single lane produced:** refit `MATERIALIZE_SORT_*` on the right shape ->
recalibrate StreamedSelect's executor arm on the compose route -> then charge the term.

**What the counter route settled, which was its real job.** Its own four terms explain **~2.2%** of
total mass between them — the honest number, and it bounds that route by its own measurement:

- **`FIXED` is over-charging by ~128 ns, and correcting it alone is a step the WRONG way.** 5 flips in
  8,000 (0.06%), net -40 ns — 128 ns cannot move an argmin whose operands are microseconds — and
  lowering it makes GatheredScan cheaper, the same direction as the missing build term's error.
  Substituting measured `ns_setup` for it makes the arm **4.4-4.8% worse**, which independently
  reproduces the phase-oracle result from a route that never used the build hypothesis. Correct it in
  the same change as the build term, never on its own.
- **The sibling routes' "`ns_setup` is flat at 41-42 ns" is a QUANTIZATION ARTIFACT and this route
  caught it.** Every value is a multiple of ~41.67 ns — Apple Silicon's 24 MHz mach timebase — so
  42/83/125/167/209 are 1/2/3/4/5 ticks. Setup is <= 1 tick below ~500 candidates and rises to 2-3
  ticks (83-125 ns) at the top decile: weakly **linear in `eval_domain`**, not fixed. By route,
  `printing_compose` 83 ns against 42 ns everywhere else.
- **`FIXED_ZERO_MATCH = 42.0` is exactly right** — p50 1.000 against measured `ns_setup`.
- **`COLLECT_PER_PAGE_ROW` is healthy, exactly.** New counter `page_rows_collected` equals
  `clamp(result_total - offset, 0, limit)` on **8,000 of 8,000** rows, so the arm's clamp is the right
  FORM and not merely fed a right number. p10-p90 all 1.000, **spread 1.0 — the tightest feature in the
  toolkit**. Its only error is the upstream cardinality estimate propagating in.
- **`ARTWORK_PER_PRINTING` is healthy and needed no counter** — `mk_plan_feats` sets
  `artwork_seen_printings = scan_units` verbatim and both artwork arms return `end - start`, so the
  realized quantity is the existing `printings_examined`. p50 0.996. Its 1.5% of mass is **not an
  independent source**: it is `SCAN_PER_ROW`'s error charged a second time at 0.50 instead of 2.06.
- **`SELECT_PER_PAGE_SLOT` is the worst-calibrated feature in the whole toolkit** — n=8,637, p50
  **0.22**, spread **26.8x**, flagged UNDER-COUNTS in every acquire route, every distinct-on and every
  orderby, above `stream_perm_steps` and `printings_walked`. The mechanism is exact and new:
  `GatherSelect` prunes to `k = offset + limit` only after the buffer grows `GATHER_PRUNE_CHUNK`
  (4,096) past `k`, so below `k + 4,096` matches **no prune has ever run and `select_page` quickselects
  the whole match set** while the arm charges one page — `select_input_len == result_total` exactly on
  4,206 of 6,244 picked rows. At 3.51 ns/slot it is worth **-0.9%**, so the verdict is *wrong and
  worthless*, not healthy. Recorded because a future refit will otherwise fit that rate against a
  feature 4.5x under at the median, and because it is collinear with `matches` on the never-pruned
  population — `GATHER_PUSH_PER_MATCH_NS = 2.24` is probably already absorbing the quickselect.

**Shipped from this round**: two counters (`select_input_len`, `page_rows_collected`), both single
reads at the `ns_finish` phase boundary with nothing inside a loop body, so the hot-path rule's A/B is
not triggered by construction — A/B'd anyway (ABAB x4, exact row parity) with same-build controls
equalling or exceeding every cross-build read and the sign flipping. `cost::gather_page_span` /
`gather_page_rows` are exposed and `fit_cost_model.design_row` now reads them instead of holding its
own copies of both clamps — the duplication shape that has drifted twice in this file's history.

**Two more defects in Round 77's tool, found by this route** (the third and fourth, after Round 78's):
`substitute` must gate on the term's indicator being LIVE, since `FIXED` and `FIXED_ZERO_MATCH` are
mutually exclusive columns both present in every GatheredScan vector; and `ARTWORK_PER_PRINTING` needs
a mode gate or the swap pushes a live counter into a term the arm correctly charges 0 for outside
artwork mode — that one alone inflated its share from 1.9% to 5.3%. Merged with Round 78's branch and
`PROJECT_MODES` gates into one `substitutable`, now carrying four gates and the measured consequence of
omitting each.

### Round 79 — Round 74's fix had a second axis: it is a PREFER discount, not just a MODE one

Three agents were sent at GatheredScan's 78.7% of cost-error mass by three routes, plus one at the
`scan_units` residual. This entry is that fourth lane, which shipped; the diagnosis lanes are in
Round 80.

**Round 74 gated `scan_all`'s first-match depth discount to `Mode::Card`. That was half the
condition.** `push_card_matches`' early-breaking arms are guarded
`matches!(prefer, Prefer::Default)` — printings are stored prefer-descending, so the first set
printing is the representative ONLY under the default prefer. Under a scoring prefer the kernel must
examine every printing, and the discount was still being applied. Three independent sites each spelled
the condition `matches!(mode, Mode::Card)` and were each wrong the same way: the depth term, the
`card_invariant_domain_exact` fast path, and the `nothing_to_verify` override — whose own comment
argues for a prefer split and then ships without it. Now named once as `card_first_match_break`.

**The constant transfers rather than needing a refit**, which was checked rather than assumed.
Realized `printings_examined / cards_visited` on picked GatheredScan rows, by the cell each walk
belongs to: `card`+custom **4.60** (the newcomer), `printing`+custom 4.41, `artwork`+custom 4.33,
`card`+default **1.87** (keeps the discount). The three full-span cells agree within 6% because they
run the same loop; a separate constant would have fitted the same quantity twice.

**It must NOT move StreamedSelect, and does not.** `card_match_count` takes no `prefer` and
early-breaks regardless — realized `examined/span` under card+custom is 1.000 for GatheredScan against
0.51 for StreamedSelect. So `scan_all` takes the break as a parameter and `stream_scan_base` asks the
other question. Verified independently: `stream_scan_units` changed on **0 of 3,600** survey rows and
all 15 of its cells are byte-identical, while `scan_units` moved on 701. This is the mirror image of
Round 74's constraint — there, one shared helper had to move both plans; here the kernels genuinely
differ, and the same discipline says move only one.

| cell | n | p50 | spread |
|---|---|---|---|
| `scan_units <GatheredScan> / card` | 791 | 0.700 -> **1.000** | 10.2 -> **6.7** |
| `scan_units [printing_compose]` | 1,505 | 0.390 -> **1.000** | 10.9 -> 9.2 |
| `<GatheredScan> / printing` | 811 | 1.000 -> 1.000 | 6.8 -> **4.7** |
| `<GatheredScan> / artwork` | 809 | 1.000 -> 1.000 | 6.6 -> **3.5** |
| `card / prefer=default` (control) | 144 | 1.000 -> 1.000 | 17.8 -> 17.8 |
| all 15 `stream_scan_units` cells | 42-88 | **byte-identical** | **byte-identical** |

Paired at 15,609 feature-rows both sides with 0 cell-n mismatches; 12 flagged cells cleared, 0 newly
flagged (70 -> 58). One wart, recorded rather than hidden: `card/prefer=oldest` spread 7.8 -> 9.0 while
its median clears.

**Error mass, re-measured with Round 77's tool and verified independently here:**
`GatheredScan / SCAN_PER_ROW` **13.8% -> 11.5%** — 2.43 points, **20.3% of the target**, with no
archive change at all. Flips 28 of 3,600 (0.78%), all away from GatheredScan, dispatch-priced
common-mode at **net -676 us** (`-> PrintingCompose` 15 rows at -686.8 us, median -33.58;
`-> StreamedSelect` 13 rows at +10.6 us, median +0.08). Worst single row +103.2 us; best -229.6 us.
`cargo test` 307 debug / 304 release, clippy clean both profiles.

**The `SpaceTotals.span` column Round 74 specified is SIZED and DECLINED.** Substituting a realized
span for every row it would reach:

| what gets an exact span | rows | mass removed | share of total |
|---|---|---|---|
| `SpaceTotals.span` (`border:`/`frame:`/`art:`/`is:`) | 136 | 71.07 | **1.95%** |
| + the mirrored `RangeCardCounts` triple | 340 | 138.04 | 3.78% |
| every fallback row answered exactly (ceiling) | 715 | 356.69 | 9.78% |

It reaches 136 of 715 fallback rows and 21% of their mass — and **the prefer gate above removed 2.43
points for none of its cost**, more than the column would. Attributing the fallback mass to the
dimension that caused it says why: **`set:` alone is 3.14% and has no table at all**, `watermark:`
1.25%, `r:` 0.36% — all `TextExact` printing-space dimensions of exactly `ValueTotals`' shape
(`HashMap<String, SpaceTotals>`) and simply absent from it, against `border:` 1.69% + `frame:` 0.71% +
`is:` 0.07% for the whole proposed column. **The archive bump is worth taking when `set:` and
`watermark:` join `ValueTotals` and get a `span` alongside everyone else** — ~6.3% reachable in one
format version instead of 1.95%. Recorded on the spec comment so it is not re-derived.

**What the residual 11.5% is.** Two counters decompose it cleanly: `eval_domain / cards_visited` is
**1.000 in every branch** (the candidate count is exact; 100% of the error is depth), and realized
`printings_examined / printing_span` is **1.000 in every cell but `card | default prefer`**
(0.35-0.75). No acquire-side predictor of depth exists — the best of nine is `log domain_cards` at
r = -0.37. The mass sits in `scan_all`'s full-span fallback (715 rows, 9.4% of total), which prices
candidates at the corpus-mean depth **3.08** while they actually average **10.1**: narrow
printing-space predicates select promo and special-set cards. That is a population effect, not a
mis-fit constant, so a blanket premium refit would be a trade — it would over-charge the broad cell
Round 74 fitted against and push traffic toward already-over-picked compose. Per-value `span` is the
right shape, after `set:` and `watermark:` join `ValueTotals`.

### Round 78 — the -10.2% "cancelling pair" was a defect in Round 77's own tool

Round 77 reported `PrintingCompose / PROJECT_PER_PRINTING` substituting to **-372.43 (-10.2%)** — the
single largest entry in its table — and concluded that compose's arm must contain a compensating
error, with a standing instruction not to touch its build terms until the partner was identified. **A
lane sent to find that partner found there is none.** The number was manufactured by the tool.

**The defect.** `bench_error_attribution_weighted`'s oracle substituted `set_printings` for
`project_printings` on every row. But `compose_total_for_mode` is `Mode::Printing => popcount(pbits)`:
**no projection pass exists in printing mode**, `card_bits` is built only under `Mode::Card`, artwork
projects via `printing_bits_to_artwork_bits`, and the arm's `project_printings = 0` there is exactly
right. `set_printings` is nonetheless nonzero on those rows — `printing_compose_fastpath` computes it
unconditionally as a diagnostic, as its own comment says. So the swap charged `popcount(pbits) x 1.93
ns` for a pass that never runs. On `border:black`/`printing`/`usd_high`: 85,411 set printings,
predicted 1.83 us, measured 0.79 us, **substituted 166.67 us**. All 401 of the negative rows are
`unique=printing`; artwork (+11.05) and card (+1.07) were positive all along.

**`bench_feature_accuracy` already had the gate, with a comment predicting this exact failure** —
`PROJECT_MODES`, "grading printing mode would divide a correct 0 by a live counter and report every
such query as a 100% under-count of a pass that does not exist." Round 77's tool was the only ungated
consumer of the pair in the tree; `bench_term_contributions` copies the gated numbers. The fix imports
`PROJECT_MODES` and `compose_grades` rather than restating them, so there is one definition of each.

**A second instance the same gate catches.** `GATHER_BITTEST_PER_PRINTING` was substituting
`printings_examined` into a column the arm zeroes on **969 of its 982 "eligible" rows** — only 13
actually took `Gather` — manufacturing a phantom **+30.72 (0.8%)**. Gated, it correctly reports 13 rows
and is marked UNGRADED. So the defect inflated a positive as well as a negative; a phantom is not
signed.

**What survives.** `GatheredScan / SCAN_PER_ROW` is unchanged at **13.8%** — the headline finding and
the whole GatheredScan attribution never touched compose's gates. `WALK_STEP` 1.1% -> 1.2%.
`PROJECT_PER_PRINTING` goes -372.43 (rank 13/13) -> **+12.38 (rank 5/12)**, and is stable positive
across `unique`, `paging_taken`, `prefer` and both seeds.

**Ruled out explicitly, so it is not re-proposed.** Pairwise substitution over compose's terms shows
**no** pair with the joint-removes/each-alone-adds signature; once gated every compose term is positive
alone, leaving nothing to cancel. Rate sweeps put `BROADCAST_PER_PRINTING` and `SCATTER_PER_PRINTING`
at exactly their shipped values on both seeds. The named suspect — that Gather-only
`COMPOSE_BUILD_PER_PRINTING_NS` leaves the walks with a free build — sweeps to **0.02 / 0.00 ns per
printing** against 0.0835, under one point of mass. And Round 75's plane-acquire free build cannot
appear in a picked-rows attribution at all: compose was picked on **0** plane rows.

**Four constraints recorded for anyone touching compose's arm**, which is the durable output of the
lane:

1. **`project_printings = 0` in `Mode::Printing` is correct** — do not "fix" it toward
   `popcount(pbits)`. That charges up to ~190 us for work that does not exist, on the plan carrying
   ~75% of routing regret. `set_printings` is the truth for this feature in card/artwork **only**.
2. **Build terms and page terms live on different populations.** `BROADCAST`/`PROJECT` are charged on
   every exit, `WALK_STEP` only on the walks, `GATHER_*` only on the gather. Never grade or refit a
   page term on rows whose `paging_taken` is a different branch.
3. **`BROADCAST_PER_PRINTING` and `PROJECT_PER_PRINTING` are ONE constant in `cost.rs`**
   (`COMPOSE_LINEAR_PASS_PER_PRINTING_NS = 1.93`) split into two mirror columns. A fit reporting two
   different values is reporting a number the engine cannot hold: move both or neither.
4. **`POPCOUNT_PER_WORD x popcount_words` is a per-mode constant on this corpus**
   (`n_{printings,cards,artworks}/64`), collinear with `FIXED`. A fitted move in it is not a rate
   finding.

**The general lesson, which is why this is a round rather than a footnote.** An oracle substitution
only measures a feature where the arm actually multiplies that feature by a rate **on the execution
that happened**. Everywhere else it adds cost for work that never ran, and the row's error grows for a
reason unrelated to the feature under test — which reads, in a mass table, as a large negative
indistinguishable from a real cancellation. The tool now says so in `substitutable`'s own doc, and
names UNGRADED terms rather than silently dropping them.

### Round 77 (measurement only) — error ranked by MASS, and it reorders everything

Adds `scripts/bench_error_attribution_weighted.py`. Every other tool here reports error as a
distribution — a cell reads p50 1.47, a spread is 27x — which answers "how wrong when it happens" and
drops "how often, and on how much of the work". This ranks sources by **error mass**: each source's
share of the total log-error summed across queries. That is the ranking this arc should have had from
the start; it has twice spent a round on a term badly wrong on almost nothing.

Uniform sampler, 8,000 queries, 5,911 picked rows, `prefer` varied.

**1. Cost prediction is ~5.7x the problem cardinality estimation is.** Total cost log-error mass
**3,808** against total estimate mass **663**. And the estimates are **exact at the median on every
acquire route** (median |log| **0.000** for `printing_compose`, `candidates`, `plane`,
`printing_range_scan`, `card_range_popcount`) — their error is pure tail, 79.1% of it inside
`printing_compose`. The estimator work this arc began with is done; what remains is cost.

**2. One cell is two-thirds of all cost error.**

| slice | rows | row% | **err mass%** | median \|log\| | mass/row |
|---|---|---|---|---|---|
| **`GatheredScan [printing_compose]`** | 2,236 | 37.8% | **67.7%** | **0.764** | 1.153 |
| `GatheredScan [candidates]` | 1,097 | 18.6% | 10.5% | 0.328 | 0.365 |
| `PrintingCompose [printing_compose]` | 1,074 | 18.2% | 8.7% | 0.213 | 0.309 |
| `StreamedSelect [printing_compose]` | 641 | 10.8% | 5.7% | 0.256 | 0.340 |
| `PlanePopcountOrder [plane]` | 354 | 6.0% | 3.0% | 0.200 | 0.326 |

GatheredScan is **78.7% of all cost error mass** on 56.7% of rows, and a median |log| of 0.547 means
the typical picked GatheredScan row is **1.73x** off; on a compose acquire it is 0.764, i.e. **2.15x**
off at the MEDIAN, not in the tail. Compose and StreamedSelect sit at 0.21-0.26 (~1.24x). Distinct-on
splits almost perfectly evenly (33.5 / 33.3 / 33.2%), so this is not a mode problem.

**3. Per-term oracle — substitute each feature's realized counter and see what error disappears.**
The reconstruction is checked against the engine's own `predicted_ns` per row (0 of 5,381 excluded on
mirror drift, so Round 71's 100% mirror holds).

**Two of this table's rows were wrong when first published, and Round 78 corrects them** — the tool
substituted counters into columns the arm zeroes, which injects work the executor never ran. The
corrected numbers are below; see Round 78 for the defect. `GatheredScan / SCAN_PER_ROW`, the headline,
is unaffected.

| plan / term | rows | mass removed | share |
|---|---|---|---|
| **`GatheredScan / SCAN_PER_ROW`** | 2,164 | **510.61** | **13.8%** |
| `PrintingCompose / WALK_STEP` | 969 | 45.40 | 1.2% |
| `GatheredScan / CARD_PASS+FLOOR` | 600 | 18.83 | 0.5% |
| `StreamedSelect / PERM_STEP` | 594 | 15.81 | 0.4% |
| `PrintingCompose / PROJECT_PER_PRINTING` | 682 | 12.38 | 0.3% |
| `StreamedSelect / SCAN_PER_ROW` | 152 | 10.98 | 0.3% |
| `GatheredScan / LOOP_PER_CARD` | 1,176 | 9.93 | 0.3% |
| `PrintingCompose / BROADCAST_PER_PRINTING` | 341 | 4.78 | 0.1% |
| `StreamedSelect / CARD_PASS+FLOOR` | 159 | **-2.44** | -0.1% |
| `StreamedSelect / LOOP_PER_CARD` | 898 | -10.03 | -0.3% |
| `PrintingCompose / GATHER_BITTEST_PER_PRINTING` | 13 | — | UNGRADED |

Three things follow.

- **`GatheredScan / SCAN_PER_ROW` is the single largest fixable source at 13.8%**, an order of
  magnitude above anything else — and this is AFTER Round 74 moved its cell from p50 0.400 to 1.000.
  Round 74 fixed the median; the mass lives in the remaining ~6.6x spread, which is exactly the "bias fixed,
  variance remains" state every term in this model is now in.
- **There is no cancelling pair in compose's arm.** The first version of this entry read
  `PROJECT_PER_PRINTING` at **-10.2%** and concluded its error must be cancelling another term's, with
  a standing instruction not to touch compose's build terms until the partner was found. That was an
  artefact of the tool, not a property of the model: gated correctly the term reads **+12.38 (0.3%)**,
  an ordinary small error source, positive in both projecting modes and on both walk branches across
  two seeds. Every compose term is positive alone once gated, so there is nothing for a partner to
  cancel — confirmed directly by pairwise substitution (no pair shows the joint-removes/each-alone-adds
  signature) and by rate sweeps (`BROADCAST` and `SCATTER` minimise at exactly their shipped values).
- **Round 76 is independently corroborated from a direction it did not use.**
  `StreamedSelect / CARD_PASS+FLOOR` substitutes to **-2.44**, i.e. the truth is very slightly worse —
  matching the ~0.8% arm regression that round measured and shipped anyway on refit-prerequisite
  grounds.

**4. Over half of GatheredScan's error is in terms nothing can grade.** Its graded terms remove
13.8 + 0.6 + 0.3 − 0.1 ≈ 14.6% of total mass while the plan carries 78.7%, so the remainder is in
`SELECT_PER_PAGE_SLOT`, `COLLECT_PER_PAGE_ROW`, `ARTWORK_PER_PRINTING` and `FIXED` (each live on
1,171-3,290 rows and backed by no counter), or in the model form itself. Round 75 added counters for
the four largest UNGRADED terms; these are the next four, and they are now the frontier.

### Round 76 — StreamedSelect's double pass: a correct count worth zero routing

Three agents worked this on separate lanes — THE FIX, a COMPLETENESS AUDIT, and an adversarial VALUE
lane — and all three converged on the same answer: **the defect is real, the fix is correct, and it
changes nothing.** It ships as a correctness prerequisite, not as an improvement, and the case for
that is stated below rather than assumed.

**The double pass, from source.** `run_query_streamed` has one counting pass and three exits. The
counting pass must run over every candidate, because the API returns `total` for any page and cannot
know which rows land on page N. The **small-total gather** exit then calls `filter.card_pass` a second
time for every card with a nonzero count — unavoidably, because `card_pass` returns TWO things, a
`Tri` verdict (one cacheable bit) and the per-card residual conjunct list into a reused scratch
`Vec<&FilterExpr>` that `push_card_matches` needs. Caching the verdict would not give you the list.
Hence the exact 2x. The **permutation walk** exit re-derives too, but only for entries it emits from,
so it reads 0.997. Under `all_match_known` every site is skipped and the count is 0.

**The fix.** `stream_residual_card_pass` = `residual_card_pass` + the redo pass's cards, the latter
predicted as **`min(matches, eval_domain)`** — bounded by the candidates the counting pass visited (a
card the narrowing never offered has no count) and independently by the total (each such card
contributes at least one match). Both bounds bind, which is why neither alone is used: against
realized `card_pass_calls - cards_visited` over 6,638 rows, `eval_domain` alone reads p75 1.667 /
p90 6.197 and `matches` alone p50 1.231, while the `min` reads **p50 1.000 in all three distinct-ons**.
The split mirrors `scan_units`/`stream_scan_units` exactly — one shared vector, a per-plan quantity
rather than a compromise 2x wrong for whichever arm loses.

| branch | n | p50 before | p50 after | in-band before | after |
|---|---|---|---|---|---|
| GatheredScan single pass | 9,934 | 1.000 | 1.000 | 74.4% | 74.4% (bit-identical) |
| SS permutation walk | 4,991 | 0.997 | 0.997 | 76.3% | 75.9% |
| **SS small-total redo** | 3,207 | **0.500** | **1.000** | 8.3% | **58.8%** |
| SS no-emit exit | 172 | 1.000 | **2.000** | 72.7% | **19.8%** |

`StreamedSelect [candidates]` goes 0.500 -> 1.000, band share 35.8% -> 73.3%. The honest cost is those
**172 rows (0.9%) regressing to 2.000** — the estimate says small-gather and the executor returned
early, the same estimate-vs-reality disagreement the shipped `SMALL_TOTAL_FLOOR` term already has on
those rows, and the whole added charge there is p50 0 ns / p90 161 ns against a 32.4 us floor already
firing. The walk half deliberately gets **no** term: its second pass is 0.1% of that plan's measured
run time at p50, and the only plan-time predictor available (`min(page_rows, limit)`) over-counts
emitting cards 2.24x in printing mode, so a term would trade a graded 0.997 for a worse feature.

**Worth zero to routing, and structurally so rather than by measurement.** `0 plan flips` — verified
three times independently: the fix lane over 59,498 keys, the value lane over 20,000 rows x 4
replicates x both populations (bit-identical every time), and separately here over 3,600 prefer-varied
keys with only StreamedSelect's cost moving (484 rows) and every feature unchanged. The reason is an
identity: **StreamedSelect is picked on the model-small-total branch 0 times** — 0 of 6,398 (uniform),
0 of 6,652 (realistic) — because `stream_runs_small_gather` also gates `n_cards * 1.02` = **32.4 us
flat**, larger than the entire mean picked-plan time (~23-28 us/query). Making a plan more expensive
where it is picked zero times cannot move an argmin. Defect size / margin-to-winner is p50 **0.0024**;
only 2 of 6,398 rows where it even reaches the margin, and those need the opposite sign.

The oracle ceiling for the whole term is **-0.06 to -0.09%** (uniform) / -0.02 to -0.02% (realistic) of
picked time, and **98% of it sits on rows a router cannot see** — where the executor took small-total
while the model predicted the walk. Gated on what the router CAN see it is a regression (+0.007% /
+0.019%). The value lane's argmin mirror was validated against the engine's own `picked` flag on
**20,000/20,000 rows every run**, and its `stream_runs_small_gather` copy pinned against the engine's
derived `stream_perm_steps`.

**And it makes the arm's total prediction ~0.8% WORSE**, which the audit lane predicted and the fix
lane then confirmed on a common-mode comparison (one set of measured times, two predictions):
small-total measured/predicted 0.390 -> 0.387, over-predicting rows 87.0% -> 87.7%. The branch is
priced by a flat `n_cards * 1.02` floor that over-charges ~2.5x; adding a p50 51 ns charge to an
already-over-charged prediction cannot help. Regressing that arm's residual on candidate drivers over
3,310 rows settles where the error actually lives:

| driver | R² | has a term? |
|---|---|---|
| `printings_examined` | **0.537** | graded, but gated OFF on 39% of rows |
| `redo_examined` | **0.536** | **none** (one special case reaches 379 of 3,310) |
| `result_total` | 0.258 | yes |
| `card_pass_calls` | **0.040** | **this round's term** |
| `eval_domain` | 0.007 | yes |

**So why ship it at all.** `fit_cost_model`'s `CARD_PASS+FLOOR` column IS this quantity, and the
small-total branch is **11,048 of 29,779** StreamedSelect rows — 37% of the fit population. A refit run
today solves for STREAM coefficients against a column 2x low on more than a third of its rows, and
least squares pushes that error into whichever column correlates: exactly the failure this doc's own
header warns about and `bench_feature_accuracy` exists to prevent. A correct count is a prerequisite
for the floor work being honest, not an alternative to it. The fix lane recommended holding the diff
until the floor round; that was overridden because routing risk is proven zero three ways, the
0.8% is on a branch worth 0% of picked cost, and an uncommitted diff in a temp worktree is how
prerequisites get lost.

**A methodology trap the fix lane caught, worth more than the fix.** Scoring each build against *its
own* run's measured times reported the walk branch's mean absolute residual moving 89.8 -> 104.3 us —
on rows whose predicted delta is **exactly zero on every row**. Pure machine drift masquerading as a
1-2% effect. Any before/after on an arm must score both predictions against ONE set of times. The
value lane's A/A control says the same from the other side: 0 flip difference but **+3.6%** apparent
aggregate time drift (uniform), **+1.9%** (realistic), so only the paired within-response deltas and
the deterministic flip counts clear the floor.

**Where the value actually is, per the value lane.** On rows where the executor took small-total but
the model did not, the **`matches` estimate over-states the realized total by p50 5.97x** (uniform) /
4.18x (realistic), p90 36.5x. That is the branch's real lever, and it is cardinality estimation, not
this term.

**Findings from the audit lane, none of which is a `count x rate` defect:**

- **A 3.59% wall-clock leak, the largest number in this arc.** 1,907 of 12,000 queries hit
  `DeclineSparseExact`; **74 had compose PICKED**, so they paid the entire build — compose `pbits`,
  project `card_bits`, popcount — and then `return None`. `declined_ns` p50 **17.2 us**, p90 44.3 us,
  **sum 11.66 ms = 3.59% of all picked-plan measured time**, thrown away before the fallback starts.
  `ComposePaging::Decline => INFINITY` exists to prevent exactly this, but predicts off the acquire's
  ESTIMATE of the total while the refusal runs off the real one. **Not a missing cost term** — no
  addition to `cost.rs` can reach a mispredicted gate. It is an estimator or guard problem and needs
  its own round.
- **`EmptyPage` runs in 9.1 us median against a 64.1 us median prediction**, 1,261 of 1,551 rows priced
  INFINITY — the largest single over-charge found.
- **Two clean negatives on named suspicions**: `GatherWalkDeclined` is 6 rows of 12,000 with 0 picked;
  GatheredScan is genuinely single-pass (p50 1.000, its tails being `eval_domain`'s estimate error,
  and its artwork dedupe and page phases are all already priced).
- **773 uniform rows read `card_pass_calls / residual_card_pass` = exactly 0.000** — the acquire says
  `residual_tier_ns100 > 0` while the executor finds `all_match_known`. An over-charge in the opposite
  direction, concentrated on `printing_compose` acquires, and the coarse broad-regime caveat in
  `explain`'s own doc surfacing as a graded number.

`cargo test` **307 debug / 304 release** (+1 each, the new gate test), clippy clean both profiles,
ruff clean, and `fit_cost_model`'s mirror stays at **100.0%** over 60,974 rows — the `design_row`
update is exactly consistent with the arm.

### Round 75 — the four largest terms in the model get counters

Round 73 found that the biggest terms in the cost model were graded by nothing at all. This adds the
counters and grades them, which changes what is known about **58% + 28% + 37% + 23%** of three plans'
predicted cost. Two counters are new, one already existed under another name, and one is deliberately
left ungraded.

| term | realized quantity | counter |
|---|---|---|
| `CARD_PASS+FLOOR` (both scan plans) | `filter.card_pass` calls | **new** `card_pass_calls` |
| `BROADCAST_PER_PRINTING` | printings the card->printing pass wrote or cleared | **new** `broadcast_printings` |
| `PROJECT_PER_PRINTING` | `popcount(pbits)` — both projection passes iterate the composed bitmap's set bits and nothing else | **already existed** as `set_printings` |
| `SCATTER_PER_PRINTING` | the range slice actually scattered | **none, deliberately** |

**`SCATTER_PER_PRINTING` gets no counter and that is the finding, not an omission.** The acquire
estimate and the build read the SAME source: the estimate takes `idx.range(lo, hi)` and charges
`e - s`, while `range_leaf_bits` scatters `idx.range_pids(lo, hi)`, which is `pids[s..e]` off that
identical call — and both fuse same-index `And` children through
`fuse_and_range_children(v, indexes, false)`, same third argument. A counter could only ever read
1.000 by identity, which is worse than no counter because it looks like a measurement. Any error
there is in the 0.48 ns rate.

**The verdicts, and only one of the four is wrong:**

1. **GatheredScan's `CARD_PASS+FLOOR` — the largest term in the model — is SOUND.** p10 0.883 /
   p50 **1.000** / p90 2.168 over 9,978 rows, with 64% reading exactly 1.000. It is exact by
   construction: `residual_tier_ns100` is set from the very `prep.all_match_known` the executor reads,
   and the call is one per visited candidate. Its residual spread is `eval_domain`'s cardinality
   error, already graded and separately owned.
2. **StreamedSelect's `CARD_PASS+FLOOR` is WRONG by exactly 2x on the small-total gather branch.**
   The term prices ONE pass; `run_query_streamed` re-derives `card_pass` for every matching card in
   its redo loop, and again per emitting entry of its permutation walk. So the feature is
   `cards_visited` against a realized `cards_visited + matching_cards`, and the cell shows it as a
   clean point mass: p10/p25/p50 = **0.50 / 0.50 / 0.50** on the `candidates` route, 0.500 at p10 in
   all three distinct-ons. **Invisible to the pooled median (0.988) and to the [0.8, 1.25] band** —
   which is exactly why per-branch slicing exists. `cost.rs`'s own comment ("the loop calls
   `filter.card_pass` once per `cid`") is false for this plan.
3. **`BROADCAST_PER_PRINTING` is right where it fires, and is the tightest cell in the toolkit** —
   p50 1.000 with p90/p10 **1.2** on the compose route, aggregate 0.991. Its 0.0% pooled median is
   correct: most compose queries have no broadcast leaf. The real error is a per-format tail from
   `scale(min(legal, illegal))` spreading at corpus-average depth: `f:oldschool` reads 2,962 against a
   realized 11,725 (**0.25x**), `f:modern` 28,593 against 23,507 (**1.22x**).
4. **`PROJECT_PER_PRINTING` is right at the median and over-counts in the tail** — p50 1.000,
   aggregate 1.065, but p99 7.98 and max 50.33, worst on the Gather branch (p75 3.45, p90 8.22). p10
   is 1.000, so it is exact or over, never under. The shape is right; the error is
   `est.result.printing()` over-stating the composed bitmap.

**A latent hazard the grading exposed, worth more than three of the four verdicts.** On a `plane`
acquire both compose build features read **0.00 at every percentile** (656 and 738 rows, 100% of that
route) while the executor really does compose and project. `mk_plan_feats` seeds
`broadcast_printings`/`project_printings`/`popcount_words` to 0 and only the `printing_compose`
acquire branch fills them — so compose is costed with a **free build** on exactly the card-space plane
leaves it must broadcast down (colour, legality, devotion). Median **99.1 us** of unpriced work per
row at 1.93 ns/printing, p90 233 us, p99 427 us. The comment at `lib.rs:16927` claims "0 for …
precomputed planes", which is true only of PRINTING-space planes. **Compose was picked on 0 of those
767 rows**, so it costs no routing regret today — a hazard, not a defect, and deliberately kept out of
`MEASURED_SPREAD`'s risk column for that reason, since a risk number for a term compose is charged for
only when it can win would overstate what routing actually pays.

**Verification.** The `tier_ns > 0.0` gate is now one function (`residual_verified`) called by all
three arm sites instead of three copies, and `residual_card_pass` is exposed from `cost.rs` following
the `stream_perm_steps`/`printings_walked` precedent so the harness does not hold its own copy of the
gate. That refactor is **bit-identical**: `predicted_ns`, `picked` and every feature match across
builds on all 3,600 prefer-varied survey rows.

Instrumentation was A/B'd per the project rule, since only one addition lands inside a loop body
(`touched += end - start` per set card in the two broadcast passes; the other counters are computed at
the publish, Round 68's shape). End-to-end `bench_query_latency_ab --sample 800 --mode realistic`,
16 runs ABBA x4 with the first block discarded as cold: **B - A = +0.02 us on a 59.93 us mean**,
per-query B/A p10 0.982 / p50 0.997 / p90 1.018, against same-build controls spanning -0.85 to
+1.38 us. Targeted at the instrumented pass (48 broadcast-heavy cells, compose's `ns_build`, 16 runs
ABBA at 6/30): B/A p50 **1.006** against a same-build control of 0.995, on cells whose broadcast
counts run to 88,477 printings per query. `cargo test` 306 debug / 303 release, clippy clean both,
ruff clean.

### Round 74 — `scan_all`'s depth discount was card-mode-only, applied to every mode

The largest term in the model with a graded feature behind it (`SCAN_PER_ROW`, 44.1% of
StreamedSelect's predicted cost) was under-charging by **3x at the median**. Three agents worked
separate lanes — the QUANTITY, the FUNCTIONAL FORM, and the DISPERSION — and two of them converged
independently on the same root cause, which is the strongest evidence this arc has produced for a
single fix.

**The bug.** `scan_all`'s fall-through computes
`expected_depth = (printings_per_card + 1) / (density + 1)` — an order statistic on the position of
the FIRST MATCHING printing. That is exactly where `card_match_count`'s `Mode::Card` arm returns
(`return (1, i + 1)`). The `Mode::Printing` and `Mode::Artwork` arms of the same function return
`(end - start)` **unconditionally**, so what gets realized there is the candidate's whole span:
`printing_span == printings_examined` on **99.5% / 99.4%** of measured rows in those modes, against a
card-mode realized depth of 0.54 of the span. And `(ppc + 1) / (density + 1) <= ppc` by construction,
so applying the discount to a full-span walk **can only ever under-charge**. On top of that,
`COMPOSE_CANDIDATE_SPAN_BIAS` multiplies it, and its own doc records that it was fit on `unique=card`
samples exclusively. Round 72 hit the same shape one estimator over (a span multiplier pooled across
two regimes, pure over-charge on one).

**The fix** is a mode gate with an early return, so the non-card branch inherits no card-fitted
constant, plus one new constant named for the population it is fit on. Card mode is untouched.

**The constant was chosen by a head-to-head neither lane ran.** The two lanes proposed different
multipliers on `cards * printings_per_card` — 1.0 (an early return bypassing the bias) and 1.4
(a premium of 2.0 times the shipped 0.7 bias) — and reported them from different harnesses on
different populations, so their numbers were not comparable. Graded together on one population of 955
compose printing/artwork rows:

| multiplier | median | within 25% | \|mean log\| | spread |
|---|---|---|---|---|
| shipped | 0.335 | 30% | 1.2301 | 27.8x |
| 1.0 | 0.914 | **36%** | 0.4679 | 19.0x |
| **1.2 (shipped)** | **1.000** | 31% | 0.3193 | 18.8x |
| 1.4 | **1.000** | 29% | 0.1939 | 18.1x |
| 1.9 | 1.076 | 30% | **0.0540** | 16.8x |

The three metrics genuinely disagree: `within-25%` peaks at 1.0, median-unbiasedness starts at 1.2,
and |mean log| minimises near 1.9 because the realized distribution is right-skewed (p90 8.26) and a
mean chases that tail. **1.2 is the smallest value landing the median at exactly 1.000** — the
criterion `bench_feature_accuracy` actually flags on — while staying near the within-25% peak, and the
realized multiplier's own median is **1.095**. The direction is load-bearing rather than aesthetic:
under-charging over-picks P3, and over-charging pushes traffic to compose, which is already
over-picked in artwork (21% of all routing regret), so the mean-log optimum at 1.9 is rejected for
over-charging at the median, not for fit.

**Paired at an identical population (60,122 feature-rows both sides, 0 cell-n mismatches, 173 of 217
cells unchanged):**

| cell | n | p50 | spread |
|---|---|---|---|
| `stream_scan_units` pooled | 5,038 | 0.850 -> **1.000** | 25.6 -> **12.6** |
| `stream_scan_units [printing_compose]` | 3,434 | 0.420 -> **1.000** | 36.4 -> **18.4** |
| `stream_scan_units <StreamedSelect> / printing` | 1,832 | 0.930 -> **1.000** | 18.5 -> 10.7 |
| `stream_scan_units <StreamedSelect> / artwork` | 1,757 | 0.930 -> **1.000** | 18.3 -> 9.5 |
| `scan_units` pooled | 11,987 | 0.550 -> **0.930** | 10.7 -> **6.6** |
| `scan_units <GatheredScan> / printing` | 4,013 | 0.400 -> **1.000** | 11.1 -> 6.7 |
| `scan_units <GatheredScan> / artwork` | 4,007 | 0.390 -> **1.000** | 10.7 -> 6.6 |

**It had to move BOTH plans, and that is why the effect is larger than any single lane measured.**
GatheredScan's `scan_units` has the identical defect (p50 0.40) because both plans read the same
`scan_all`. Fixing `stream_scan_units` alone would have manufactured a false P3/P4 asymmetry — the
exact hazard `lib.rs` warns about — so the shared helper is the right place and every per-`orderby`
cell moves to ~1.000 with its spread roughly halved.

**Flips, dispatch-priced.** 54 of 3,600 prefer-varied survey rows (1.50%), **0 in card mode**, all
away from StreamedSelect. Priced by measuring both the old and new pick inside the SAME
`explain_analyze` response (common-mode, so the two share whatever drift the machine has — a
cross-build comparison could not resolve microseconds against the documented ~9% noise floor):

| transition | n | net | median |
|---|---|---|---|
| `StreamedSelect -> PrintingCompose` | 16 | **-2,410.7 us** | -56.67 us |
| `StreamedSelect -> GatheredScan` | 29 | -65.0 us | -0.37 us |
| `GatheredScan -> PrintingCompose` | 2 | **+151.4 us** | +75.71 us |

Net **-2,324.3 us** over 47 priced rows, 21 faster / 3 slower / 23 inside a 1 us floor. The flips
toward compose were the flagged risk and turned out to be the entire win; the only real regression is
a 2-row `GatheredScan -> PrintingCompose` pair, worst single row +180.1 us
(`id:u pow>=1 pow<=4 year>=1995`/printing/rarity/offset 300/newest). `cargo test` 306 debug / 303
release, clippy clean both profiles.

**Two proposals measured and REJECTED, recorded so they are not retried:**

- **The `residual_card_invariant -> stream_scan_units = 0` override is genuinely unsound** —
  `touches_printing_field` reports `Legality => false` by an explicit ranking choice made for
  `printing_dependent`'s verify order (`filter.rs:898`), so a legality residual zeroes a term the
  executor really pays (`f:oldschool t:creature`/printing zeroed against a realized 2,587). Replacing
  it with the exact divergent-candidate span makes the cell exact (32 rows, 0.000 -> 1.000) and still
  should not ship: **0 plan flips of 19,011**, and it costs **+7.4 us of acquire** on a
  12,716-candidate query (~0.55 ns per candidate) against 66.7 us saved across 46 rows. It needs an
  O(1) divergence estimate, not an O(candidates) pass. Simply dropping the zero is far worse (p50
  **20.0x**, p90 **204x**) — for broad formats divergence really is the rare corpus case.
- **A per-mode split of the cost ARM itself.** `PlanFeatures` deliberately carries no `mode`,
  `prefer` or `all_match`, so `plan_cost` structurally cannot branch on them, and every prior
  "split the arm" round (66, 68, 70, 72) was implemented acquire-side instead. The form
  `if tier_ns > 0 { count x rate }` is **exactly correct where the count is honest**: restricted to
  the `candidates` acquire's full-span-walk arms it reads **100.0% exact, spread 1.0**. The 27x pooled
  spread was one acquire branch's number, never a missing term.

**Also recorded, not acted on.** `card_match_count` does not take `prefer` at all — P3's residual
scan is prefer-independent, and all the prefer sensitivity lives in the separate `redo_examined`
counter. The `tier_ns > 0` gate has one provable condition error (under `Mode::Card` with an
existential plane the kernel walks printings even when `all_match_known`, so `tier == 0` is the wrong
proxy: 32 of 1,927 rows, realized `examined/cards_visited` p50 1.17) but it simulates at 0-1 flips per
4,000 queries and would need a new `PlanFeatures` field. And 153 of 199 rows where the feature is
live-zeroed are compose queries whose `scan_units` is 0 from an estimated EMPTY intersection while the
executor still scans — a plan must scan to discover a set is empty; this fix incidentally cuts that
population to 24.

**The card-mode grading is itself partly wrong, which bounds what any of this proves.**
`stream_scan_units` deliberately includes a redo-bias term (Round 31), but `printings_examined` is
captured only from the counting pass and structurally cannot see the redo — that is the separate
`redo_examined` counter. On the 568 card rows where redo fires, `ssu/examined` reads p50 0.892 while
`ssu/(examined + redo)` reads 0.370. So card mode's cell is measured against a counter missing a
component the feature intends, and Round 73's 44.1%/27.1x figures inherit that. Card mode is gated out
of this fix for that reason among others; repairing the grading is a prerequisite to touching it.

### Round 73 (measurement only, no code change) — which terms actually carry the cost

Adds `scripts/bench_term_contributions.py` and answers the question that should have been asked
before Rounds 69, 70 and 72: of the terms whose features have a 15-35x spread, which ones carry
enough of the predicted cost for that spread to matter? Exact rather than estimated, because Round
71's name-keyed `design_row` plus `CURRENT[plan][term]` decomposes `predicted_ns` into its own terms,
and Round 71's 100% mirror agreement is what makes it the shipped model's decomposition.

**It reorders the queue, and it retires two of my own conclusions.**

| plan | term | aggregate share | picked-only | feature spread | risk |
|---|---|---|---|---|---|
| StreamedSelect | `SCAN_PER_ROW` (`stream_scan_units`) | **44.1%** | **29.4%** | 27.1x | **11.96** |
| GatheredScan | `CARD_PASS+FLOOR` | **58.2%** | 35.1% | ungraded | — |
| PrintingCompose | `BROADCAST_PER_PRINTING` | 37.1% | 24.5% | ungraded | — |
| StreamedSelect | `CARD_PASS+FLOOR` | 27.8% | 21.4% | ungraded | — |
| PrintingCompose | `PROJECT_PER_PRINTING` | 23.1% | **47.6%** | ungraded | — |
| GatheredScan | `SCAN_PER_ROW` (`scan_units`) | 15.8% | 19.8% | 11.1x | 1.76 |
| PrintingCompose | `WALK_STEP` (`printings_walked`) | 6.8% | **1.9%** | 21.8x | 1.48 |
| StreamedSelect | `PERM_STEP` (`stream_perm_steps`) | **0.3%** | 2.2% | 15.5x | **0.04** |
| PrintingCompose | `GATHER_BITTEST` (`compose_scan_printings`) | **0.3%** | 0.1% | 34.2x | **0.10** |

- **`stream_perm_steps` is worth 0.3% of StreamedSelect's predicted cost.** Rounds 69 and 70 graded
  it, extracted `cost::stream_perm_steps` for it, and I called its 15.5x spread "the shape error a
  coefficient refit would bury" and the thing to fix. It carries essentially nothing. The grading and
  the extraction were still worth having — they closed a mirror bug and a stale-formula bug — but the
  spread is not a reason to build anything.
- **Round 72's fix was correct and nearly worthless in cost terms**: `compose_scan_printings` is 0.3%
  of compose's cost, which is exactly why it produced 0 plan flips. That result now reads as expected
  rather than as reassuring.
- **`printings_walked` is 1.9% of compose's PICKED cost**, against the 57% + 21% of regret the matrix
  attributes to the `Perm`/`OrderbyWalk` paging branches. Those are not the same quantity — regret by
  branch is a POPULATION, not a term attribution — and this says the cost error on that population
  cannot be mostly the walk term. Items 4 and 5 need re-justifying on that basis before either is
  built. It also means Round 72's unfixable `OrderbyWalk` 4x under-count matters less than its size
  suggests.
- **The real target is `SCAN_PER_ROW` on StreamedSelect**: 44.1% of predicted cost with a 27.1x
  spread, a risk of 11.96 against 1.76 for the next graded term. It is the only term that is both
  large and badly dispersed.
- **The largest terms in the model are ungraded by anything.** `CARD_PASS+FLOOR` is 58.2% of
  GatheredScan and 27.8% of StreamedSelect; compose's three build terms
  (`BROADCAST`/`PROJECT`/`SCATTER`) are ~94% of its picked cost. No harness grades any of them,
  because `bench_feature_accuracy` grades features against executor counters and these terms are
  priced on `eval_domain` gated by residual presence, or on printing counts with no counter of their
  own. That is a bigger gap than any of the spreads.

Read the median column next to the aggregate: `BROADCAST_PER_PRINTING` is 37.1% aggregate at a **0.0%
median**, i.e. it is a pure tail (the legality broadcast), while compose's `PROJECT_PER_PRINTING` is
47.6% picked at a 36.2% median and so is broad. A term that is all tail and one that is everywhere
want different treatment.

### Round 72

Compose's feature pass, which is queue items 4-6's territory. At n=178,731 feature-rows the pooled
`printings_walked` 1.24 and `matches` 0.80 that `fit_cost_model` flags turn out to be **three
separable defects**, only one of which is fixable, and the biggest one is not.

**Shipped: `compose_scan_printings` loses its multiplier on the grouping arm.** `gather_composed_page`
charges `printings_examined += (end - start)` — the candidate span — in the grouping arm (artwork
always, card under a scoring prefer). Graded against that, the shipped feature read a median of
**exactly 1.47**, the constant `COMPOSE_GATHER_SPAN_PER_MATCH` itself, on every slice: 622
`bench_feature_accuracy` rows (artwork 305, card 301, both orderby slices, p70 also 1.47) and 87
prefer-matched rows standalone. A median sitting on the constant means bare `printing_matches` reads
**1.000** — arithmetic, not a second fit. This is Round 66's own finding one arm over: that round
diagnosed the constant as pooling two regimes and being pure over-charge on one, fixed the
early-break regime, and left the multiplier on the grouping regime where it is equally unearned.

Paired at an identical population (**178,731 rows and 622 compose cells on both sides**): pooled p50
**1.47 -> 1.00**, artwork 1.47 -> 1.00, card 1.47 -> 1.00, and the tail improved too (artwork p90
5.76 -> 3.92, card 4.66 -> 3.58) because the multiplier was inflating it. **0 plan flips over 3,600
prefer-varied survey rows**; `predicted_ns` moved on 50 rows, all `PrintingCompose`, no other plan
touched; every other feature byte-identical. `cargo test` 306 debug / 303 release, clippy clean both.
`Mode::Printing` keeps the multiplier — it walks the same span and the argument probably applies, but
it produced no gradeable rows (below `MIN_ROWS` in every slice), so it stays unmeasured and unchanged.

Two candidates that were better on paper and measured **worse**, both rejected: `scan_units` (p50
0.499) despite being the engine's own candidate-span estimate and grading 1.00 on `GatheredScan`,
which walks the identical quantity; and `eval_domain` (p50 0.364).

**NOT fixed, and the attempt is the useful part: compose's `OrderbyWalk` under-counts ~4x in grouped
modes and a second term does not close it.** The cells are unambiguous — `/artwork` p50 **0.24**
(n=1,305), `/card` **0.27** (n=1,274), against `/printing` **1.14** (n=2,150) and `Perm` **1.10** in
all three modes. The mechanism is visible in the source: `walk_value_orderby_page` accumulates
`printings_examined` from two places, `examined += 1` per index entry stepped (what
`printings_walked` models, correctly) plus `group_representative(.., &mut examined)` per MATCHING
entry in grouped modes only, which scans the card's span to pick the group representative. So the
model omits a whole term that exists exactly where the feature fails.

Adding it does not work. `cards_visited` on this walk IS the resolution count, so the residue is
measurable per row rather than inferred, and it is not stable: `residue / resolutions` has median
19.2 with p10 **-1.9** and p90 91.1, and **160 of 1,050 rows have a negative residue** — the shipped
term already exceeds realized there. Sweeping a per-resolution depth from 1.0 to 6.0 moves the
two-term model's median only 0.314 -> 0.478 against a target of 1.0 and `within 25%` from 10% to 14%,
against a shipped baseline of 0.268 / 9%. The resolution COUNT is not predictable either
(`resolutions / (page_span * pmatches / matches)` p10 0.65 -> p90 2.84, and `orderby=rarity` 1.59
against `usd` 0.85). **So this is the same dispersion problem, not a missing term** — recorded so the
obvious fix is not attempted a second time.

Left for items 4-6 with numbers attached: `eval_domain <compose Gather> / card` **1.29** (n=246,
artwork 1.09), and the `OrderbyWalk` grouped bias above.

**One methodology trap, which cost a wrong reading before it was caught.** `costbench`'s comment said
`PlanFeatures` does not carry `prefer`, "so the features and every `predicted_ns` come back identical
whatever is passed". That stopped being true in Round 66, when `compose_scan_printings` gained a
`Mode::Card if Prefer::Default` arm — the acquire reads `prefer` even though the struct does not store
it. A first pass at the comparison above passed `prefer` only to `explain_analyze`, graded a
default-prefer feature vector against a scoring-prefer execution, and read the shipped feature as
**0.508** where a prefer-matched run reads **1.470**. A 2.9x error that looks exactly like a finding.
The comment is corrected, and `costbench`'s `perm_steps` doc (which still cited the pre-Round-32
`n_cards` walk formula) with it.

### Round 69 (measurement only, no code change)

Grades StreamedSelect's two never-graded cost drivers, `perm_walk_span` and `stream_scan_units`, which
is queue item 1 as re-scoped. Raw outputs in [measurements/](measurements/)
(`2026-09-04-streamedselect-feature-grading-uniform.txt` and the gate attribution beside it).

**The stated blocker was wrong, and that is the round's first result.** Item 1 said both features "need
a realized counter to grade against." Both counters already exist and are already published by
`explain_analyze`: `perm_walk_span` enters cost only through the walk term, whose realized counterpart
is `perm_steps`, and `stream_scan_units` grades against `printings_examined`. `cost.rs:526` says so in
as many words. So the gap was in the harness, not the engine — no instrumentation round, and no
paired-A/B obligation on a hot path. Cost: one throwaway script.

**The walk term's median is already right and its variance is the whole error.** Realized `perm_steps`
over the shipped estimate `min(page_span * perm_walk_span / matches, perm_walk_span)`, 560 usable rows
of 2,974 StreamedSelect plan-rows that ran (`--mode uniform`, 1-3 predicates x 3 `unique` x 6 `orderby`
x offsets {0, 60, 300}): pooled p10 0.446, median **1.023**, p90 4.302, spread **9.6x**. Sliced by sort
column the medians barely move (0.918-1.183) while the dispersion ranges from **1.9x** (`name`) to
**38.8x** (`cmc`), with `edhrec` 11.8x and `power` 8.6x. `rarity` and `usd` produce zero walk rows, so
the term is untested for those sorts.

That pattern is the finding. A per-orderby **scalar** — the shape item 5 was scoped around — cannot help,
because there is no per-column offset to correct. What varies is how well the uniform-density assumption
holds: `name` order is uncorrelated with any filter, so matches really are spread evenly and the formula
is nearly exact; `cmc` and `power` correlate with the predicates queries actually use (`cmc` with
color/type, `power` with `t:creature`), so matches clump and the waiting-time formula breaks down. And
**no feature the router already holds predicts the residual** — log-log Pearson r is **+0.058** against
`match_rate` (the control, correctly ~0), -0.105 against `page_frac`, -0.122 against the estimate
itself. A clumping correction has to be a new statistic (filter-vs-sort-column correlation), which makes
it a build rather than a calibration.

**Do the permutation-less sort columns need their own cost branch?** `rarity` and `usd` produce zero
walk rows, which prompted the question. For StreamedSelect the answer is **no, and for a better reason
than `cost.rs` gives.** `SortPermutations::get` returns `None` for `SortCol::Rarity` and
`SortCol::PriceUsd` — `build_sort_permutations` builds permutations only for
edhrec/cubecobra/cmc/power/toughness/name — and `streamed_select_applicable` requires
`sort_perms.order(..).is_some()`, so the plan is dropped from the argmin **before `plan_cost` is ever
called**. Verified empirically: across 6 broad queries x 2 `unique` x those two columns, StreamedSelect
was offered on **0 of 12** and ran on 0, while being offered on 12 of 12 for `name`/`cmc`. So
`perm_walk_span`'s `map_or(ctx.n_cards(), ..)` fallback is unreachable for the walk term, and
`cost.rs:909`'s claim that the span "already collapses to `n_cards` … when no permutation exists at
all" is true but beside the point — the applicability gate, not the collapse, is what makes it safe.
`perm_walk_span` is read at exactly one site (`cost.rs:927`), so nothing else inherits the fallback.

Incidentally this is also why `perm_walk_span == n_cards` so often: the sort-column bound only narrows
the span when the filter constrains **the same column being sorted**. In the same 48-row check the only
row where it bit was `cmc>=2` ordered by `cmc` (27,456 against `n_cards` 31,724).

**Where the question DOES land: compose already has the branch in the executor and not in the cost
model.** `orderby_walk_available(sort_col)` is literally `matches!(sort_col, PriceUsd | Rarity)` — so
compose's two walk arms partition on exactly this property, `Perm` walking the card permutation and
`OrderbyWalk` stepping a `PrintingValueIndex`. Two different traversals in two different spaces, and
regret scores them separately (57% / 21%). But `cost.rs` prices them with **one shared arm**
(`ComposePaging::Perm | ComposePaging::OrderbyWalk => printings_walked * COMPOSE_WALK_STEP_NS + …`).

Measured, that merge is **fine, and the split that matters is a different one.** Realized
`printings_examined` / `sigma_bound::uniform_mean` over 971 gradeable compose walk rows — note the
denominator excludes `WALK_LENGTH_BIAS`, so the shipped estimate is this divided by **1.45**:

| slice | n | p10 | median | p90 | spread |
|---|---|---|---|---|---|
| POOLED | 971 | 0.537 | 1.312 | 12.766 | 23.8x |
| paging `Perm` | 520 | 0.588 | **1.277** | 13.077 | 22.2x |
| paging `OrderbyWalk` | 451 | 0.493 | **1.449** | 12.596 | 25.6x |
| `orderby=name` | 154 | 0.656 | **0.925** | 1.423 | **2.2x** |
| `orderby=usd` | 213 | 0.397 | 1.049 | 5.928 | 14.9x |
| `orderby=power` | 121 | 0.488 | 1.243 | 3.094 | 6.3x |
| `orderby=edhrec` | 116 | 0.537 | 2.802 | 11.453 | 21.3x |
| `orderby=rarity` | 238 | 0.632 | 2.967 | 17.041 | 27.0x |
| `orderby=cmc` | 129 | 0.438 | **3.579** | 37.327 | **85.3x** |

The two paging arms are statistically indistinguishable (1.277 vs 1.449), so merging them is justified
and the shared-arm comment is right. **The sort COLUMN is what the pooled bias hides** — and unlike
card space, here the MEDIANS move, 0.925 to 3.579, a **3.9x range against a single shipped
`WALK_LENGTH_BIAS = 1.45`.** Per column the constant should be roughly 0.64x its current value for
`name` and 2.5x for `cmc`. It also cuts straight across the paging boundary: `rarity` (2.967) and `usd`
(1.049) are both `OrderbyWalk` and differ by 2.8x from each other, so no paging-level split can
capture it.

**So item 5's original scoping — a per-orderby bias for `printings_walked` — is correct, and this is
the first direct evidence for it.** It also means the two walks are genuinely different problems rather
than one: printing space has a per-column BIAS a scalar can fix, card space has per-column VARIANCE it
cannot. Within a column nothing existing predicts the residual in either space (|r| <= 0.21 against
`match_rate`, `page_frac`, and the estimate itself).

**`stream_scan_units` is bimodal, not biased.** Realized `printings_examined` / estimate over 1,011
charged rows: p25 **1.000**, median **1.000**, p90 **11.839**. `printing` (n=354) and `artwork` (n=366)
are exact through p50 and then reach p90 16.7x and 14.0x; `card` (n=291) is the only slice that
under-runs at the bottom (p10 0.308). Either exactly right or a large under-count, with little in
between — the second error shape a coefficient refit cannot represent, and the reason the queue's
"fix features before refitting" warning applies here too.

**Both cost gates are correct.** `walks_permutation` disagreed with whether the walk ran on 83 of 2,974
rows (2.79%), and **all 83** are the ESTIMATE landing on the wrong side of `STREAM_MIN_MATCHES` while
the executor branches on the realized `total` — the gate logic mirrors the executor exactly. The
residual-scan gate disagreed on 778 rows, of which **720 (92.5%)** are the plan returning before any
loop (no work done, so nothing mis-estimated), leaving a 58-row residue (1.95%). Attributing these
mattered: taken at face value the scan gate looked wrong on a quarter of all rows.

**And the clump table it replaces was stale.** Item 5's per-orderby table was recorded at 11:54; Round
68 landed at 12:44 and redefined `printings_examined`. Re-running at identical seeds (same 971 rows,
same per-column n) moved the pooled median 1.395 -> **1.312** and `cmc` 4.931 -> **3.579** (-27%),
narrowing the range of medians from 5.2x to 3.9x. The re-run validates itself: `usd` and `rarity` — the
only two columns that take `OrderbyWalk` rather than the `walk_grouped_page` arm Round 68 changed — are
**unchanged to three decimals** (1.049, 2.967) while every `Perm` column moved. A change landing on
exactly the columns that use the changed arm, and on none of the others, is what a real effect looks
like. This is the queue's "measure after item 2, not before" rule collecting its first scalp.

**One incidental estimator finding, on a population items 3-4 do not cover.** The boundary straddles are
one-directional gross OVER-estimates on `is:` and rarity-range leaves rather than on conjunctions —
`is:vanilla` estimated 17,437 against a realized 429, `r:rare is:vanilla` 5,653 against 15, and
`r>=mythic year>=1994 year<=2005` **9,466 against 0**. Every routing-boundary error the walk gate
suffers comes from there, not from the `And` arm this arc has spent thirty rounds on.

### Round 68

**The first executor round in this arc.** Everything before it made a prediction better; this removed
work. The framing came from the user, and it is the better one: rather than predicting a
high-variance quantity more accurately, reduce the executor's own contribution to that variance.

**What was wasted, and why it was safe to stop.** `from_rows` sorts printings prefer-DESCENDING within
a card at load time (ties by illustration_id then scryfall_id). So under `Mode::Card` +
`Prefer::Default` the first SET printing is already the chosen representative — every subsequent
`prefer_score`, the `touched`/`group_best` bookkeeping and the post-loop emit were pure waste. The
correctness-critical detail is the tie rule: the general loop kept a candidate only on strict `>`, so
ties went to the lowest pid, which is exactly where `find` stops. The representative is provably
identical, not merely usually identical. `gather_composed_page` and `push_card_matches` already took
this break; the walk did not, and the walk is the branch carrying 78% of routing regret.

**Row identity was the gate, and my own first attempt at it was worthless.** I hashed returned
printing identity over 8,008 compose cells and got matching digests — from **0 `Perm`-paging cells**,
because `orderby=rarity` routes to `OrderbyWalk`/`Gather` and never reaches the arm that changed.
Varying the orderby produced 21,912 cells including **5,920 `Perm`**, 750,580 rows, identical sha256.
The lesson generalises: a differential that does not report how many rows hit the CHANGED path is not
evidence, however large its row count.

**The loop got 30% faster and the end-to-end number is much smaller, which is the honest read.**
`ns_loop` p50 **0.707**, interleaved, with four control plans all at p50 1.000. But `ns_loop` is only
1.9 us of a 4.8 us compose round — the other 2.8 us is `ns_setup`, which this cannot touch — so it is
~4.4% of the round at p50, and the router picks compose for 76 of 1,200 queries. The implementing
agent explicitly declined to claim the `ns_round_total` p50 of 0.960 as this change's effect, which
was the right call.

**Why the win is larger than "matching cards only" predicts.** I expected little, because the walk
steps the whole permutation and most stepped cards should not match. Measured, the live `Perm`
population's composed-bitmap density is p0 **0.013**, p50 **0.205**, p90 **0.701** — compose DECLINES
on sparse totals, so the traffic that survives to `Perm` is dense and most stepped cards do match. My
prior was wrong because it ignored the decline gate upstream.

**A regression the router cannot reach, left in place deliberately.** At composed density 0.001 the
new arm reads 1.01-1.19 against the old one, reproducibly across three runs and under BOTH a `find`
and an explicit-loop-with-break implementation — so it is neither noise nor an artifact of the
construct, probably per-card branch structure from the added third arm. It sits below the live
population's p0 of 0.013 and the router never sends `Perm` there. Recorded rather than fixed.

**And a cost-feature consequence, which is the interesting part.** Making the executor faster made the
cost model WORSE, exactly as the user anticipated: `printings_walked` is unchanged while the realized
counter shrank (2,092,874 → 1,828,715 on real `Perm` traffic, a definition change as much as a
speedup), so `<compose Perm> / card` feature/counter rises p50 **1.01 → 1.09**. Nothing else moved.
That is not a defect in this round — it is the model needing to follow the executor, and it is now a
queue item. It also sharpens the sequencing already recorded: fix the executor, THEN the features,
THEN refit, because each step moves the target of the next.

### Round 67 (measurement only, no code change)

A uniform-sampler pass over the COST model rather than the estimator, to decide what to work on next
rather than to change anything. Three harnesses at seed 66, ~300k plan-rows; raw outputs preserved in
[measurements/](measurements/). They agree, and they reordered the queue.

**Where routing loss is.** `printing_compose` carries **97%** of all lost time (n=40,484, 7% miss,
max 957 us) against `candidates`' 3% and everything else ~0%. Broken down by compose paging branch:
`Perm` **57%**, `OrderbyWalk` **21%** (worst miss rate, 11%), `Decline` 15%, `Gather` **7%** (best
miss rate, 1%).

**The failure is one-directional: compose is UNDER-picked.** `GatheredScan -> PrintingCompose` is 35%
of regret and `StreamedSelect -> PrintingCompose` 32% — **67% is compose losing when it should have
won**, against 14% for compose winning when it should not. And compose's median cost is the BEST of
the three big plans (1.11, spread 2.8); the damage is a tail (p99 43.7, p100 203, `/printing` p99
132.6). It loses precisely where it should win.

**Features are not the lever for absolute accuracy, and that needed measuring rather than assuming.**
Substituting realized executor counters for every estimated feature removes only **+0.021 to +0.099**
of log error, against a `model form` floor of 0.235-0.862. So no feature fix will move the
absolute-accuracy numbers. That does not make feature work pointless — regret is about ORDERING, and
every known feature bias pushes the same way (against compose) — but it does retire the idea that the
cost model is mainly starved of good estimates. It is mainly the wrong shape.

**The finding that matters most for this arc's stated goal.** The shipped coefficients are already
absorbing feature bias: `scan_units` fitted/shipped is **4.98/1.72** (GatheredScan) and **9.59/2.13**
(StreamedSelect) — a 2.9-4.5x gap that matches that feature's own measured ~3x under-count. The whole
arc existed to unblock a joint refit; this says a refit run NOW would bury the remaining feature error
in that rate, exactly as `bench_cost_error_attribution.py`'s own doc warns ("a fit will quietly bury
the error in whichever term correlates with it"). **Fix the features first, then refit** — which is the
opposite of the order the arc's original framing implied.

**Why this reordered the queue, and how it corrected me.** Both items I had queued at the front touch
the `Gather` paging branch and `scan_units`. `Gather` is the best-behaved branch in the whole matrix —
1% miss, 7% share. The concentration is `Perm` + `OrderbyWalk` (78%), priced by `printings_walked`,
which is badly off exactly there: `<compose OrderbyWalk> / card` p50 **0.26** (spread 40.0), `/artwork`
p50 **0.25** (spread 38.1), while `<compose Perm> / printing` over-counts in the tail (p99 11.3, p100
91.3). Pooled `printings_walked` reads p50 0.90 — the pooled view hides both directions, which is the
same trap Round 66 documented one feature over.

**One methodological note worth keeping.** `bench_cost_error_percentiles.py` and
`bench_regret_matrix.py` disagreed, and the regret view won. By percentiles, `StreamedSelect` looks
like the problem (p50 1.57, over-costed 57% at every single slice) and compose looks fine (1.11). By
regret, compose is 97% of the loss and StreamedSelect's over-cost mostly does not change a decision.
The harnesses' own docs predict this: "an estimate can be off by 100x on a plan that never wins
anyway, and correct to 5% on one where the margin decides every query." Rank by regret; use
percentiles to diagnose the mechanism once regret has chosen the target.

### Round 66

**The first round in this arc that fixed the COST MODEL rather than an estimate**, and it needed no
fit, no held-out split and no new dimension — only for the feature to agree with the executor. The
framing came from the user: this is the cost model asserting the executor does something it
demonstrably does not.

**The evidence was sitting in the code, in three places, for as long as the defect.**
`COMPOSE_GATHER_SPAN_PER_MATCH`'s own doc names the carve-out ("except in its card/default-prefer arm
it iterates `start..end`"). The sibling feature five lines below gates on exactly that predicate
(`Mode::Artwork => true, Mode::Card => !matches!(prefer, Prefer::Default), Mode::Printing => false`).
And `gather_composed_page`'s early-break arm charges `pid - start + 1`, not the span. Nothing was
hidden; the call site simply never honoured what its own constant's doc said.

**A pinned median that had to be sliced rather than smoothed.** The target cell
`compose_scan_printings <compose Gather> / card` reads p50 **1.47 before AND after**, which looks like
the change did nothing. It isn't: `bench_feature_accuracy` runs `--mode uniform` and draws `prefer`
FLAT, so ~80% of that cell is the untouched non-default arm and the median never moves. Sliced to
`prefer=default` (n=32) the same cell reads p50 **4.369 → 1.000**, mean 9.163 → 1.823, and stops being
flagged. The tail moves even pooled (p99 22.97 → 15.89, p100 91.30 → 21.46). **Read a per-arm fix on a
prefer-blended population and you will conclude it did nothing** — the implementing agent surfaced
this rather than reporting the flat median, and independently replicated it before the harness patch.

**Plan flips were the expected direction, and had to be dispatch-priced to know.** The `nway` survey
reports 0 flips of 66,378 — which is structural, not evidence: that survey pins `orderby="name"`, so a
permutation always exists, compose pages by `Perm`, and `cost.rs` reads `compose_scan_printings` ONLY
in the `Gather` arm. On a gather-reaching population there are **3 flips of 37,771 (0.008%)**, all
`GatheredScan → PrintingCompose` — the intended direction, since a smaller honest feature should let
compose win where it deserves to. Priced individually: **2 faster (+57.21 µs, +1.17 µs), 1 slower
(−9.71 µs)**, net +48.67 µs. A flip count alone would not have distinguished a win from a regression
here.

**The 2.2% throughput gap in the first (unpaired) run was not the change.** `bench_feature_accuracy`
is time-boxed, so the two runs walked the same seeded sequence and stopped at different points —
88,663 against 86,723 queries. That is a **population-parity** violation of this repo's own review
rule, and it mattered because the target cell is small (n=207). The capability to fix it already
existed: `costbench.Budget` takes `sample: int` as an alternative bound and its docstring says
outright that this is the better choice "for a PAIRED comparison"; the script had never exposed it.
Wired as `--n-queries`, the re-run reports **112,129 rows on both sides, exactly equal**. On the
throughput reading itself: `explain_analyze` runs EVERY applicable plan each round, so its per-query
cost is pick-independent apart from the single `Routed` participant — with flips at 0.008% the
maximum attributable share is ~0.001%, three orders of magnitude short.

**What this round deliberately did NOT do.** Carving out the default arm leaves
`COMPOSE_GATHER_SPAN_PER_MATCH` calibrated on a population blending both regimes. Measured on the
card/non-default arm alone (n=105, `prefer=newest`), the feature/`printings_examined` ratio reads a
median of **exactly 1.47** — meaning bare `printing_matches` is already ~exact there and the
multiplier is pure over-charge. That is a refit, with its own population and its own held-out
question, and folding it in would have made this round's own evidence unattributable. Documented on
the constant and queued.

### Round 65

**A safety claim that was true in the wrong space, for 31 rounds.** Since Round 34 the build has
asserted that "each dimension's `rest_max` at this N lands far below the count where a wrong estimate
starts actually flipping a routing decision, so the fallback's whole operating range stays nowhere
near that risk zone." Measured: `set` 36 and `colors` 107 against a 1,024 boundary — comfortably true.
`identity` **1,060** — false, and by the worst possible margin, 3.5% over. The claim was verified in
CARD space, where identity's `rest_max` is 377 and genuinely far below; the boundary it invokes
applies to whichever space the estimate is compared in. This is the same defect shape Rounds 61-63
kept finding one level down — a number that is correct for one question consumed by something asking a
different one — except here it was in the comment asserting the argument held.

**The accuracy of the fallback turned out to be nearly irrelevant; the CAP is what mattered.** All 9
routing-relevant identity misses read `rest_max` exactly, not the independence product. Doubling the
identity table to N=512 was measured as an alternative and eliminated all 9 too — while leaving the
ratio distribution flat (median 1.281 → 1.340, p90 11.0 → 11.4, max 371 unchanged). That is the
round's real lesson: for a capped estimate, *bounding the cap below the decision boundary is worth
more than any improvement to the estimate itself.* It also reframes Round 64 honestly — that round's
ratio gains on `set` and `colors` were real but, on this evidence, could never have moved routing,
because those dimensions' caps were already 28x and 9.6x below the boundary.

**Why the free variant was declined.** A floor-plus-minimum-128 rule reaches the identical guarantee
at **zero net index size** (175 + 142 + 611 = 928 pairs, exactly today's total) by reallocating
capacity from `set` and `colors` — whose pairs top out at 503 printings — to `identity`, the only
dimension with pairs big enough to matter. Tempting, and rejected: it would shrink `set` 347 → 175 and
`colors` 295 → 142, and an exact table hit does more than avoid a bad estimate — it populates
`exact_domain`, which feeds `scan_units`. Those ~325 pairs would lose an exact answer in all three
spaces, and that cost is NOT bounded by the sparse-floor argument the rest of this round rests on. So
the two rules are additive, at +8 KB. Recorded because the zero-cost option is genuinely available if
index size ever becomes a constraint.

**The test that matters is not the obvious one.** A floor test whose fixture puts ONE big pair against
a small `n` proves nothing: the pair enters through the rank cutoff anyway, since being at the floor
makes it the largest thing present. The floor only adds something when MORE pairs sit at or above it
than the rank cutoff admits — which is exactly the real shape (`identity` has 611 against `TOP_N` =
256). The shipped test uses five floor-height pairs against `n = 2`, with zero cards and artworks so
they cannot enter through those spaces either. The first version of it also failed for an unrelated
reason worth remembering: 300 pairs all tied at the boundary are ALL kept by Round 47's tie rule, so
the fixture excluded nothing and `rest_max` was 0.

### Round 64

**The win is real and the standard survey cannot see it.** Over 9,777 survey rows this round is a
perfect no-op on plan choice and on the ratio diagnostic. Over 750 deliberately-constructed
`(set|c|id) × subtype` pairs that miss the exact table, it moves the median from 1.309x to **exactly
1.000x** and the p90 from 8.25x to 6.00x. Both are true: `nway_estimate_truth_survey.py`'s shape
catalog generates these pairs almost exclusively in configurations that HIT the table, so the miss
population — the only one this mechanism serves — is nearly absent from it. That is a gap in the
survey, not evidence about the change, and it is worth remembering before treating a flat survey as
proof a mechanism-level fix did nothing.

The survey does carry one direct signal: `SubtypePairIndexes` exact-hit candidates went **38 → 41**
and `SubtypePairEstimate` rows **67 → 61**, so six rows moved from estimate to exact. That is the
union cutoff keeping pairs the card-only ranking had been dropping, visible without any targeted
population.

**The `set` dimension's median is a misleading summary, and this section originally said so wrongly.**
The first write-up of this round called `set` "an honest trade, not a clean win" because its median
ratio moved further below truth (0.900 → 0.586) even as its tail improved (p90 6.00 → 2.00, max 18x →
6x). Re-measured per-query against identical truths, that framing is wrong:

- **91 of 168 rows land CLOSER to truth, 22 further** — 4:1 favourable.
- **Predicting 0 against a nonzero truth went 72 → 49.** Only 3 rows newly predict zero (truths of 3,
  3 and 1); 26 stopped doing so.

The median falls *because* fewer rows are pinned at zero, not despite it. The distribution is
zero-inflated — 49 of 168 rows predict exactly 0 — so the median sits just past the zero block: before,
ranks 1-72 were zeros and rank 84 landed at 0.900; after, only 49 are zeros, so rank 84 lands deeper
into the small-ratio rows at 0.586. **A median is not a meaningful summary of a zero-inflated ratio
distribution**; the closer/further count and the false-zero count are.

What actually changes is the shape: large over-predictions collapse toward truth (`set:c21 t:Aura`
36 → 13 against a true 6; `set:scd t:Wizard` 36 → 11 against 12), while some near-exact rows move into
moderate under-prediction (`set:dmu t:Warrior` 30 → 13 against 27). The under-count rising 84 → 102 is
real but is mostly rows moving off zero into small nonzero values, plus over-predictors crossing 1.0.
Everything in this population sits far below the 1,024 routing boundary, which is why the survey shows
zero plan flips.

The estimate BINDS on all 168 of these rows (it equals the row's final `matches`), so this mechanism
alone determines their predictions — there is no min-fold cushion here of the kind that made
`Independence`'s loose claims harmless.

**A drift artifact that first read as a +4.6% build regression.** The union cutoff does three sorts
per table instead of one, so build cost was a real question. Measured as sequential blocks — all
"before" reps, then all "after" — load time read 2.61s against 2.73s, +4.6%. Interleaved 5 reps each
instead, the same comparison reads **min ratio 1.0042, median 1.0015**. This project's own benchmark
protocol already records sequential-block measurement as the artifact that produces exactly this, and
it produced it again on the first attempt.

**One idea deliberately left out of scope.** `rest_max.printings` is now a real printing-space value,
which makes it a PROVEN upper bound on any excluded pair's count — this arm only fires when no exact
subtype-pair hit covered the leaves, so the pair was excluded, and a pair absent from the build map
has count 0. Today it is `min()`-ed with the independence product into a single estimate-only
candidate, which discards that bound (Round 59's admission rule). With the two channels that now
exist it could be both: `guaranteed = rest_max.printings`, `estimate = indep`, leaving `best()`
unchanged while populating a channel that is currently empty. Not bundled here — it changes mechanism
attribution and needs `check_bound_class_soundness.py`'s mechanism map updated, and the queue's own
discipline is to validate one idea at a time.

### Round 63

**The obvious implementation of Part 1 was measured and rejected.** `arith_tuple_totals` already
returned the exact triple for a single bound, so the cheap change was to make it primary — two lines,
no new index. It produces the identical exact numbers. It also cost **+186% on `and_estimate_ns` p50**
for queries carrying a numeric leaf, against **+7%** on a control subset of queries that cannot reach
the arm at all — about **+3.8 μs** on what the queue calls the highest-traffic of the three
reprint-ratio arms, because it is an O(distinct tuples) scan (~564 keys, four `f64` conversions and an
`eval_arith_tuple_tri` each) where the old path was O(log n). Preserved unmerged on
`r63p1-arith-tuple-reuse-measured-slow`.

The replacement is *faster* than what it replaces, not merely affordable: searching ~30 distinct
values beats searching 31,724 index entries, so the touched-query p50 came in at −19% against −12%
on the control. That is the second time this arc has found the accurate version to also be the cheap
one (Round 61's shared `legality_space_totals` lookup was −5.7%), and both times only because the cost
was measured rather than assumed.

**Part 2's first version was wrong in a way the test suite could not see.** A disjointness proof
really does mean zero cards and zero artworks, so proving `card: Some(0)` on that branch looks
strictly more honest. It is a true statement about the ANSWER and the wrong one for `result.card`,
which `acquire_plan_features` consumes as the **domain the materializing alternatives walk** — and the
two part company exactly when narrowing declines a child. Measured: it drove
`border:white border:black`'s `eval_domain` and `scan_units` to **0** and flipped it
`PrintingCompose → GatheredScan`, against a realized `cards_visited` of **2,059**. That is the
0.2 μs-against-199.3 μs mispricing `est.candidate` exists to prevent, and it is recorded in
`domain_cards`' own doc. **All 303 debug and 300 release tests passed with that bug in place**; only
checking `eval_domain` against realized execution found it. With card/artwork left absent on that
branch the query is byte-identical to before, and every win is retained.

A genuine `PairTotals` hit is different, and the difference is the round's real lesson: its count is
what narrowing on both values actually reaches, verified against execution (`cmc=5 frame:1997`
`eval_domain` 643 against a realized 643). "Exact" is not one property — a number can be an exact
answer and still be the wrong domain.

**Two harness traps worth recording.** First, the survey script's failure was reported to me as
success, because piping it through `tail` returns *tail's* exit status: two runs died on a missing
corpus (`benchmarks/` is gitignored, so it is absent from a fresh worktree) while the notification
said exit 0, and three stale JSONL files from a previous round sat at the expected paths ready to be
compared as if they were this round's. Verify the run's own output and the file's mtime, never the
piped exit code. Second, this round was implemented in a dedicated worktree because another session
moved the shared checkout to `main` mid-round — `pair_bounded_min`'s signature changed under a read.

**One diagnostic worth carrying to item #2.** `Independence`'s under-truth row count went 172 → 180.
Same interaction Round 61 found with legality: an input becoming exact removes an error that had been
cancelling `Independence`'s own over-prediction on correlated pairs. Soundness is unaffected
(estimate-class), but it is more evidence for the anchor item.

### Round 62

**Part 1 does not do what my plan said it does.** I justified it as stopping the queued
domain-seeding round (#3) from making both card gates unconditionally true. That is wrong.
Domain-seeding seeds every space `{guaranteed: Some(n_cards), estimate: Some(n_cards)}`, so
`card.guaranteed.is_some()` becomes just as vacuous as `card.best().is_some()`. Part 1 is a
readability-and-honesty improvement for today — it makes the gates say the property they actually
depend on — but it does **not** unblock #3. Only Part 2's explicit flag survives domain-seeding.

The consequence for the queue: **#3 additionally needs sites 2/3 converted to something not
presence-based at all** — an explicit "exact card source" flag parallel to `printing_tightened`,
set where a trusted card count is written rather than inferred from `Option`-ness afterwards. That
is new scope on #3 that was not in its estimate.

**The old test was mis-classifying far less than I claimed.** My plan read Round 60's "the estimate
is tighter than the bound on 17,628 of 32,745 roots" as implying the numeric test was wrong on
something like that scale. It isn't: a bound-only tightening is only *hidden* when the bound lands
above the estimate, so actual disagreement is **123/39,461 (0.3%)**. The gap is real and worth
closing on its own terms — the test cannot see a class of tightening that Round 59 made routine —
but it is a correctness fix at the margins, not a broad repair.

**The 6 rows that move all get worse, and here is exactly how.** In every case `eval_domain` was
*exactly* the realized `cards_visited` before and is not after, while `matches` and the picked plan
are unchanged:

| query | `eval_domain` | `scan_units` | realized `cards_visited` |
|---|---|---|---|
| `cmc=0 f:premodern` | 216 → **1,200** | 3,699 → 1,200 | 216 |
| `f:penny cmc=0` | 480 → **1,200** | 3,699 → 1,200 | 480 |
| `f:oathbreaker pow=6` | 625 → **626** | 1,930 → 626 | 625 |

**The exactness being lost was preserved by accident** — a bound-only tightening left the retired
numeric test `false`, which happened to route these to `est_cards`. Landing anyway is the deliberate
call: relying on that accident is precisely the coupling this arc exists to remove, and the measured
consequence today is nil (zero plan flips, `bench_pairwise_ordering` unchanged,
`bench_feature_accuracy` with 0 cells changing verdict).

**Correction (2026-09-04), on the mechanism this section originally gave.** It said `est.result.card`
is `None` on these shapes, so the tightened branch had no exact card count to fall back on. That is
wrong, and the real cause is more specific. Read directly off `explain`'s own trace for
`cmc=0 f:premodern`: `considered` holds a `PairTotals` hit carrying `card_guaranteed: 216` — the true
value — while the root reports `card: 1200`. `est.result.card` is `Some(1200)`, not `None`; 1,200 is
`cmc=0`'s own solo card count arriving via `narrow_floor`. The exact 216 never reaches `result`
because **`pair_bounded_min` folds printing space only** (`pt.get(x, y, Mode::Printing)`, one `usize`;
the arm then builds `SpaceEstimate { printing, card: UNKNOWN, artwork: UNKNOWN }` at `lib.rs:10792`).
`PairTotals::get_all` returns the exact triple from the same hashmap lookup, but its only caller is
Round 60's trace instrumentation. So the exact card and artwork counts are computed, printed in
`explain`, and thrown away. Round 60 reporting both channels in the trace is what made this visible at
all. Requeued as item #3 with that framing, and it fixes these rows structurally rather than by
restoring the accident.

**The obvious repair is catastrophic, and was measured rather than assumed.** Gating the tightened
branch on `&& exact_cards.is_none()` moves **894 rows and flips 877 plans**, reintroducing the
`border:white border:black` mispricing that `candidate` exists to prevent. The implementing agent
tried it, measured it, and declined to ship it — the right call, and the reason the 6 rows are being
accepted rather than papered over.

### Round 61

**The fix is one line, and the whole round is the measurement around it.** `ValueTotals::legality`
already held the exact per-`(format, status)` printing count, and the arm was already reading that
same row for its artwork column. Nothing had to be built.

**The error, all 23 formats.** The old scale spread a format's legal cards at the corpus-wide reprint
depth (97,812 / 31,724 = **3.083**), so its error is exactly `3.083 / that format's own depth`:

| | format | cards | true printings | scaled | scaled/true | format depth |
|---|---|---|---|---|---|---|
| worst under | `oldschool` | 961 | 4,579 | 2,962 | **0.647** | 4.765 |
| | `premodern` | 5,375 | 24,343 | 16,572 | 0.681 | 4.529 |
| | `modern` | 22,450 | 74,305 | 69,218 | 0.932 | 3.310 |
| ~exact | `vintage` | 31,646 | 97,106 | 97,571 | 1.005 | 3.069 |
| worst over | `pauper` | 10,767 | 31,920 | 33,197 | **1.040** | 2.965 |

16 of 23 under truth. The formats that looked correct (`vintage`, `commander`, `legacy`, `duel`,
`oathbreaker`) are the ones covering nearly the whole corpus — their population's depth simply IS the
corpus depth, so the scale was right by tautology, not by working. `banned:`/`restricted:` were worse
than any `f:` (`banned:modern` 160 against 403, `restricted:vintage` 160 against 657) for the same
reason inverted: tiny curated sets sitting on the most-reprinted cards.

**A bare `f:X` query does NOT show this, and that nearly wasted the round.** The obvious spot check —
`explain('f:oldschool')['acquire']['matches']` — reads **exact on both builds**, because a bare
legality leaf routes through `Prep::Plane`, whose count comes straight from `exact_result_total` and
never touches the arm being changed. The arm is only reached from inside an `And`. The check that
works is to put the leaf in a two-leaf `And` with an all-matching partner and read the LEGALITY LEAF'S
OWN node out of `explain`'s `and_trace` tree — the arm's output before any fold. Every future round
touching a leaf arm should reach for that, not for `acquire.matches`.

**A +9.3% regression the canary could not see, and the control subset that caught it.** The patch as
first written reached printings and artworks through two `exact_result_total` calls. A same-binary
canary (two runs of the baseline wheel) read 1.000, and a run of the changed wheel read +11% — but
that +11% appeared **equally on the 31,055 queries with no legality leaf at all**, which the change
cannot touch. That control subset is what separated ~10% of run-to-run drift from the real effect.
Re-measured with the control flat at exactly 1.000, the two-call form cost **+9.3%** of
`and_estimate_ns` p50 on legality-bearing queries (3,584 → 3,916 ns); collapsing it to the single
shared `legality_space_totals` lookup turned that into **−5.7%** against trunk (3,625 → 3,417 ns),
because it also skips the prelude the surviving `exact_result_total(.., Mode::Artwork)` call used to
walk. The two forms are byte-identical in behaviour — 64,428 shared rows, zero differences on
`predicted_matches`/`picked_plan`/`count_source`/`and_mechanism` **and the entire `and_trace` dict** —
so every correctness number above holds for both.

**The shapes that got worse, and why it is structural rather than a defect.** The ratio diagnostic
reads "B is LESS accurate" by +0.000/+0.001, which is a wash overall but not uniform:

| shape | seed 0 Δ | seed 61 Δ | mechanism |
|---|---|---|---|
| `safe:legality+cn` | **−0.074** | +0.002 | min-fold / `Independence` |
| `safe:legality+usd` | **−0.016** | **−0.014** | `Independence` |
| `star:legality+cmc+usd` | +0.018 | +0.023 | `Independence` |
| `OR:legality+usd` / `+cn` / `+set` / `+released` | +0.008…+0.015 | +0.010…+0.015 | `Or` arm's `add` |
| `star:legality+identity+usd` | +0.009 | +0.009 | `Independence` |

Both regressing families are the undershoot having been load-bearing, in two different arms.
`Independence` computes `round(a * b / n)`, a product that already over-predicts on correlated pairs;
a too-small `a` was cancelling part of that, and every newly-broken straddle is one of these rows
crossing 1,024 upward (`usd<=0.28 f:oldschool` [printing]: 1,018 → 1,574 against a true 607 — both
builds badly over, one happened to land on the benign side). The `Or` arm sums children, so a larger —
and now genuinely bounded — legality child makes a union over-count larger. Neither is fixable by
making the leaf wrong again; both are the same finding as Round 56's, one level up: **an estimate-class
combiner whose inputs just got exact needs its own anchor.** Queued rather than patched here, because
a fudge factor on `Independence` is exactly what Round 56 measured and rejected.

### Round 60

Additive observability, and the first time this arc can *look* at what it spent five rounds reasoning
about. Every "which channel won" question so far — the 173 card/artwork regressions, Round 57's 25
outvoted rows, Round 55's undershoot, Round 59's seed laundering — was reverse-engineered from source
and hand-built measurements. Now it is readable off `explain`.

**Reuse over invention.** `SpaceEstimate` is already exactly `{ printing, card, artwork }` of
`SpaceMeasure` and is `Copy`, so the trace carries that rather than a parallel encoding. A per-group
"class" label was considered and rejected for a concrete reason: a mechanism's contribution is not one
number in one channel. `Candidate::PrintingBound` lowers BOTH channels of printing;
`Candidate::Exact` sets `guaranteed` in all three spaces and leaves `estimate` alone. A string cannot
say either, and keeping the estimator's own shape makes a group's contribution directly comparable to
the tree node it folded into — which is the comparison the blocked investigations actually need.

**One deviation, and it is the right call.** `Candidate::Estimate` gained `card`/`artwork` fields that
are REPORTED but never FOLDED. The `"Independence"` trace group has always carried
`card_indep`/`artwork_indep` while `fold_candidate` discards them; deriving channels purely from a
one-field `Estimate` would have turned those existing keys from `Some(n)` to `None` — a byte-identity
violation on the round whose entire bar is byte-identity, and it breaks two pre-existing tests. So the
plan's assertion "every `Estimate` populates `printing.estimate` only" holds for all six estimate
mechanisms EXCEPT `Independence`'s card/artwork **estimate** channels. No `Estimate` ever touches
`guaranteed`, which is the property that matters. Pinned by a dedicated test.

**A real cost, honestly isolated.** The trace is built only under `want_trace: true`
(`explain`/`explain_analyze`), never on the production path, so production is untouched. But the
diagnostic path is not free, and the agent measured it properly rather than assuming — on the 300
widest real `And`s (11-34 `considered` groups), `explain()` p50:

| build | widest | sample |
|---|---|---|
| baseline `a297d101` | 41.1 us | 14.8 us |
| probe: all struct changes, OLD 3-key serialization | 41.6 us | 15.0 us |
| shipped | 46.2 us | 17.5 us |
| rejected: `format!`-built key names | 53.4 us | 19.1 us |

The probe is what makes this a finding rather than a number: the struct and tree changes are free, and
the entire delta is the six extra `PyDict::set_item` calls per dict — this round's actual payload.
Using `&'static str` key literals instead of `format!` recovered ~12 of an initial 30 points, which is
worth remembering for any future addition at that boundary.

**What the channels actually contain, now that they can be seen.** Round 59's non-invariant is not an
edge case: **41,838 of 147,660** tree nodes have `printing_guaranteed` absent while `printing` is
present, and on **17,628 of 32,745** roots the guess is TIGHTER than the proven bound. That is the
normal state, not an anomaly — which is precisely why "absence must serialize as `None`, never `0`"
was a hard constraint, and why the queued domain-seeding round is attractive.

**The `narrow_floor` candidate set, framed as a candidate set.** 4,317 root nodes carry a
`card_guaranteed` tighter than any child's `card_guaranteed` — the readable signature of the
`best()`-into-`guaranteed` laundering. But the agent was careful to say it is not a verdict: the same
set contains legitimate `Candidate::Exact` joints (e.g. `tou>=6 tou<=11` at 1,189 against a tightest
child of 1,221), where a real multi-leaf mechanism genuinely proved something tighter than any single
leaf. Round A still has to separate the two. That distinction is exactly what was invisible before.

**Sequencing this establishes.** C (this round, observable) -> B (replace the three presence/equality
proxies with explicit structural signals) -> domain-seeding (delete every `Option`, make `printing()`
infallible by construction) -> A (`narrow_floor`, near-trivial once bounds are always present and
honest). B grew from one instance to three: `est.candidate.printing() == est.result.printing()`,
`est.result.card.best().is_some()`, and `est.result.card.best() == Some(domain_cards)` all read
presence or equality as a stand-in for structure, and domain-seeding makes all three vacuous — so they
must be fixed together, before it.

### Round 59

The round did what it set out to do structurally, and **two things its own plan asserted were wrong**.
Both corrections are worth more than the round's mechanics.

**Correction 1: it does not change behaviour.** The plan said "This round DOES change behaviour, so
report routing-relevant straddles." It is byte-identical — zero prediction changes across 54,321 shared
rows at my seed and 64,605 keys at the agent's, including the full `and_trace` dict. Reassigning which
channel a number lives in cannot move an answer while `best()` still reads `min(guaranteed, estimate)`.

**Correction 2: the 29 outvoted rows do NOT recover, and channel reassignment structurally cannot
recover them.** The plan claimed demoting the `Legality` leaf out of `guaranteed` would stop it winning
folds it should lose. It does not: `best()` is the min across BOTH channels, so moving a small number
between them leaves it just as small and just as winning. Measured 0 of 25 (25 rather than 29 only
because the seed differs; the set is identical before and after). The trace makes it plain:

```
considered  LegalityDateTotals  hit=True  printing=15090      <- exact, and now in `guaranteed`
tree: op min_fold printing=11361
  leaf printing=89264  YearCmp { op: Ge, year: 1999 }
  leaf printing=11361  Legality { shift: 18, expected: 1 }    <- 0.72x undershoot, still wins
```

Recovering them needs the leaf to stop undershooting, not to be reclassified. And the real fix is now
precisely characterised, which is the round's most useful output: the `Legality` arm already calls
`exact_result_total(filter, indexes, Mode::Artwork)`, and the same function answers `Mode::Printing`
exactly from `value_totals.legality`. Measured across all 16 formats in the survey catalog,
leaf-scaled against that exact number: **0.647-0.955, median 0.850, 16 of 16 under truth** — a 4.5-35.3%
undershoot, materially worse than the "5-13%" this doc previously recorded. Deliberately not taken here,
since it is a real behaviour change owed its own validated round.

**The catch that saved the round from being inert.** The `And` arm seeded its printing accumulator as
`SpaceMeasure::known(pair_bounded_min(v, indexes, folded.result.printing(), ...))` — and
`SpaceEstimate::printing()` IS `best()`. So the seed collapsed the per-leaf channel split into one
`usize` and re-asserted it as proven, laundering a demoted leaf's approximation straight back into
`guaranteed` one level up. That collapse is exactly what made Round 58 byte-identical, and it means the
leaf demotions accomplish nothing above a single leaf without fixing it. Measured on an `f:A cmc<=1`
fixture (both leaves demoted, true printing count 100): pre-fix the arm's `guaranteed` reads **36**,
0.36x of truth, *in the channel the queued cross-space clamp is meant to clamp card and artwork down
to*. Post-fix it is the domain. `pair_bounded_min` is now handed `guaranteed` rather than `best()`,
which is sound because its own contributions are real counts (a stored `PairTotals` entry, or a
disjointness proof's exact 0) — bound in, bound out. `best()` is unchanged either way, which is why
byte-identity survives.

**The rule that generalizes: a number derived from `best()` may never be written to `guaranteed`,**
because `best()` can resolve from the estimate channel. Now recorded on `SpaceMeasure` itself. This is
the general form of the seed bug and the thing most likely to be reintroduced.

**A new non-invariant, also documented:** `printing.guaranteed.is_some()` is NOT invariant —
`SpaceMeasure::add` needs both sides proven, so an `Or` of two demoted leaves leaves it absent.
`printing.best().is_some()` still is, since every constructor fills the estimate channel.

**The audit found three leaf arms, not two.** Applying the rule to every arm rather than the two the
plan named turned up the bare cmc/power/toughness `bare_numeric_field_count` branch doing the identical
reprint-ratio scaling — the highest-traffic of the three. Its `arith_tuple_totals` fallback is a real
triple and keeps both channels.

**Two corrections to the plan's smaller claims**, from the agent: only ONE member of
`is_estimate_class_mechanism` now also contributes a bound (`LegalityDateTotals`), not two —
`PriceJointTable` was never in that list, so it already out-ranked three-space mechanisms for trace
attribution, which this round makes accidentally correct. And that predicate has exactly one call site
(`and_trace_build_tree`'s winner selection); the old doc credited it for the `And` arm's `covered`
bookkeeping, which is hand-written per call site. The name was kept because every accurate rename would
imply moving `LegalityDateTotals` out of the list, changing `explain` output — i.e. retiring R40's
class-priority rule, which is out of scope.

**`SetCollectorRange` stayed out**, per the earlier decision, and is now pinned by a test on a
non-contiguous `gap` set. The `Legality` leaf's own new test additionally *documents* a live
`artwork(100) > printing(36)` cross-space violation and why the clamp had to wait for this round.

**Two standing artifacts.** `scripts/check_bound_class_soundness.py` asserts no bound-class mechanism's
printing-space candidate falls below truth, checking each candidate's OWN `and_trace.considered` number
(losers included) rather than `predicted_matches` — stricter than the row-level view, which is where the
card/artwork derivation confound lives. 5,553 candidates over 12 mechanisms in my run, none below truth,
including 630 `PriceJointTable` and 242 `LegalityDateTotals`, corroborating both promotions' structural
arguments. And the `ARITH_TUPLE_BLOWUP_CARDS` release-clippy error — reported as "pre-existing, left
alone" by five consecutive rounds — is fixed by gating the const to `#[cfg(debug_assertions)]`, matching
its only consumer; `cfg` rather than `#[allow(dead_code)]` because the const genuinely should not exist
in a release build. **Both clippy profiles are clean for the first time in this arc**, so "release clippy
is clean" is now a real signal rather than a skimmed caveat.

Timing: nothing measurable, as expected for pure channel reassignment — 790 ns → 770 ns with 0 of 500
queries outside a +/-100 ns band, against a same-build control showing a 10 ns shift with 3 of 500
outside it. Worth noting the agent's bootstrap CI called even the identical-build control "significant",
which is a caution about trusting that CI at this magnitude.

### Round 58

Two channels per space, and a rejected behavioural change. The structural half shipped; the half it was
designed to enable turned out to rest on a premise of mine that measurement contradicted.

**Why the split.** `SpaceEstimate` carried one number per space, so a proven bound and a best guess had
to compete for the same `.min()` slot, and `.min()` picks the smaller regardless of which is
trustworthy. Five prior rounds each worked around that one conflation: R40 needed a class-priority rule
purely to stop the two competing by magnitude; R52 bolted `est.result.card` on as an extra `.min()`
after replacing it outright caused a 170x regression; R55 found an undershooting estimate permanently
sinks `result` with no later exact candidate able to raise it, forcing a positional ordering constraint;
R56 had to omit `mark_covered` so an anchored estimate and an exact bound could coexist; R57 left 29
rows where the mechanism computes the exactly-correct value and still loses the fold. All five are "a
sound number and a good number, one slot."

**The phasing earned its keep, concretely.** Phase 1's first survey found 3 differing rows on one query
(`(pow=8 t:minotaur) or (id:b set:gtc)`, printing 61 → 249). Cause: `min` distributes over a per-channel
fold, `+` does not. `min(g₁,e₁) + min(g₂,e₂)` can take `g` from one side and `e` from the other, which
`min(g₁+g₂, e₁+e₂)` cannot reproduce — so an `Or`'s union number silently ROSE whenever its children's
tightest answers came from different channels. Fixed by summing `guaranteed`s in one channel and
`best()`s in the other, giving `best(a.add(b)) == a.best() + b.best()` exactly. In a blended round this
would have been attributed to the behavioural half and chased in the wrong place.

**Phase 2 was rejected on its own acceptance criterion, and the reason is a correction to this doc's own
reasoning.** The plan asserted that `COMPOSE_CARD_ESTIMATE_BIAS = 1.78` corrects for `k` being
*inexact*, so an exact `k` should skip it. That is wrong. The constant corrects the printing→card
**clustering** the occupancy model's independence assumption ignores — which is independent of whether
`k` is exact. Skipping it asserts the answer set's reprint depth is 1.0. Bucketing the moved card rows
by the answer set's true depth (`true_printings / true_cards`, both measured) is monotone:

| depth | n | better | worse |
|---|---|---|---|
| 1.0-1.2 | 9 | **9** | 0 |
| 1.2-2 | 50 | 6 | 44 |
| 2-5 | 203 | 11 | **192** |
| 5-10 | 8 | 0 | 8 |
| >=10 | 5 | 0 | **5** |

`date:2019-11-07 f:gladiator` — the query the whole round was motivated on — has depth **1.000**, because
one release date is one set so every printing is a distinct card. **It was the single most favourable row
in the population, and I generalized the round from it.** At the other end `f:alchemy year<2011` is depth
17.5 (2,432 printings over 139 cards): trunk's 1,337 was already 9.6x over and the skip makes it 2,341,
16.8x over. Net on R57's 173 regressed rows: 22 recovered against **163 newly regressed**, target-shape
card median abs-log-ratio 0.133 → 0.473, total absolute error 393,406 → 1,333,602. A narrowed variant
(fire only on a single-date window) also failed — 7 recovered for 7 newly regressed, absolute error
slightly worse — because one date is not one printing per card: `date:2024-03-08 f:vintage` is ~1,050
legal printings over 334 cards (depth 3.1), since showcase/extended-art/borderless variants share a
release date. Preserved unmerged on `r58-phase2-measured-bad` (`e1d4fba7`, and `e5f75f45` for the
narrowed variant) with the measurement, so the negative result is recoverable rather than folklore.

The lead that replaces it, unvalidated: a **card-space independence product**
(`date_cards × legal_cards / n_cards`, using `RangeCardCounts`' exact distinct-CARD count for the
window). Hand-checked at both depth extremes it points the right way where occupancy structurally
cannot — `f:alchemy year<2011` needs ~139 from 11,250 window-cards and `date:2019-11-07 f:gladiator`
needs ~840 from ~927, a legal-card-fraction factor of ~0.012 vs ~0.9 that independence supplies and
occupancy cannot. But R57 rejected independence for this pair in PRINTING space on 250x per-format
density skew, so card space needs its own validated round. Separately, artwork mode's 62 regressions were
never reachable by this fix at all: `artwork_estimate`'s `capacity_cards` uses the UNCALIBRATED
`balls_into_bins`, so there is no divisor there to skip.

**A sharper note for `COMPOSE_CARD_ESTIMATE_BIAS`'s own doc than the one this round planned to leave**:
an exact `k` still needs that divisor. What it corrects is clustering, not `k`'s accuracy, and the two
are independent.

**The honesty gap this round created and did not close.** `SpaceMeasure::known(v)` sets BOTH channels to
`v`, and two leaf arms (`FilterExpr::Legality`, the `is_broadcast_leaf_shape`/devotion arm) report
`card_count * n_printings / n_cards` — an average-case approximation measured to UNDERSHOOT by 5-13%. So
`guaranteed` currently holds values BELOW truth in the channel documented as a proven upper bound. It is
there only because phase 1 is byte-identical by construction, and it is documented as a known gap on
`SpaceMeasure` itself. Closing it is the next round, and it must come before the cross-space clamp:
clamping `guaranteed.artwork` down to a `guaranteed.printing` that is itself an undershooting
approximation would propagate the undershoot into artwork space.

### Round 57

Target: `unsafe:legality+released` — the highest median absolute error of any shape (2,464) at 0%
mechanism coverage, where the engine's prediction was simply the smaller marginal.

**The investigation retracted its own premise twice, which is the most reusable part of this round.**

1. The design doc justified excluding `legality × released` from the independence registry because
   "format legality is *defined* by a release-date cutoff." **Measured false in printing space**:
   legality is card-level, `released_at` is printing-level, and reprints scatter a format's legal cards
   across the whole axis — every format except `oldschool` has legal printings back to 1993-08-05. The
   cutoff governs SET legality, not the printing population an estimate sees.
2. So independence looked like the fix, and it is a large win — 460 measured (format, date-predicate)
   pairs, wrong-side-of-1,024 rows 17.2% → 7.6%. But **"unsafe" turned out to be a per-format
   property**: skew spreads run 1.0x for `legacy`/`commander`/`vintage`/`oathbreaker`/`duel` (min-fold
   already returns 1.002-1.010x there — the blanket exclusion was discarding formats where the fix is
   free) to 250x for `oldschool`, which supplies essentially the entire tail in both directions.
3. Then the error turned out to be **an identity, not a correlation**: independence substitutes a
   format's global legal density for its local density, so its error is exactly
   `global_density / window_density`. `premodern`'s 3.69 skew in its own era predicts a 1/3.69 = 0.27x
   undershoot — precisely the 0.26-0.27x measured. That reframed the fix from "calibrate an estimator"
   to "store the temporal structure."
4. Which settled granularity empirically, and the knees are mid-year exactly as feared: Modern's is
   **2003-07-28** (8th Edition, 16.8% → 100.0%), Pioneer's **2012-10-05**. Worse, `standard` has no knee
   at all — it alternates date-to-date (2024: 15.8%, 61.0%, 17.3%, 8.6%, 4.7%, 61.4%, …) because main
   sets interleave with supplemental products. Per-year leaves a 2.7x max; per-quarter zeroes the
   routing metric; **per-date is exact**, because release dates are the atoms of the axis so every range
   aligns to bucket boundaries with nothing to pro-rate. Run-length compression was tested and rejected
   (~40% at best: for 17 of 23 formats the high-frequency alternation IS the signal — `vintage`
   compresses to 3 runs, `standard` needs 580 of 924).

Also measured and rejected along the way: a **fudge factor** does not apply here (that was Round 56's
finding, reconfirmed in spirit), and **card/artwork exactness** was left out because printing counts are
additive (one date per printing) while card counts are not — a card with printings on both sides of a
cut lands in both halves, the same constraint `RangeCardCounts` documents when it declines `year:Y`.
That asymmetry is why this round is printing-space only.

**An unplanned finding worth keeping: 9 phantom keys.** The archive grew 185,784 bytes rather than the
predicted ~148 KB. Cause: like `ValueTotals::legality`, the build walks all 32 format *slots*, and an
unassigned slot reads `not_legal` for every printing — so 9 slots cleared the 1,024 floor at 97,812
printings each, costing 33 KB for keys no query can name (`shift` comes from `format_shift(name)`). The
fix is general rather than slot-specific: drop any key covering the entire date index, since its
`prefix[j] - prefix[i]` just reproduces the date leaf's own count and can never be tighter. Verified
behaviour-neutral across 64,563 rows. Final size **+148.8 KB (+0.205%)**, matching the prediction once
the phantoms are gone. Related: a query can only ever ask for `legal`/`banned`/`restricted`
(`filter.rs`'s own match), so `not_legal` keys are unreachable by construction — worth remembering
before adding any other `legality × X` table.

**The one real cost, verified rather than accepted on report: 173 card/artwork ratio regressions**, all
on the target shape, all card (111) or artwork (62), **zero in printing space**. Traced it directly:
`date:2019-11-07 f:gladiator` goes 1,675 → **840 = exactly true** in printing, but card goes 927 → 468
against a true 840. The true card count IS 840 (one date is one set, so every printing is a distinct
card); the downstream derivation takes the now-exact P and scales it by the global card/printing ratio.
Before, it scaled the WRONG printing estimate and landed closer **by accident**. So this round exposed a
pre-existing bias rather than introducing one — the same `* n_cards / n_printings` under-bias measured
at median 0.474x. Confirmed the next round fixes it: calibrated occupancy on the same exact P gives
**813-829 against true 840 (0.97-0.99x)**, and is insensitive to the calibration constant across
k=0.40-1.00. Net routing effect is still an improvement in all three spaces, so nothing routes worse.

**Independent re-verification** (my own wheels, `__file__` asserted; before = `costcell/trunk`
`fcb9a708`): all four motivating queries exact — `f:gladiator year<=2003` 14,928 → **2,673**,
`f:standard date<=1999-06-07` 9,404 → **652**, `f:premodern year:2024` 11,117 → **1,165**,
`f:oldschool year>=2005` 2,962 → **44**, every one 1.00x against truth and tracing a
`LegalityDateTotals` hit. `cargo test` 284 debug / 281 release; `clippy --all-targets -- -D warnings`
clean in debug (the release-only `ARITH_TUPLE_BLOWUP_CARDS` error is Round 51's, confirmed pre-existing
again). Timing: a flat **+83 ns** wherever the mechanism fires (8-11% on the cheapest 2-3-leaf date
queries, ~1% on anything with real work), with both no-legality and no-date canaries unchanged within
noise — the guard works.

Two things this round could not fix and did not pretend to. Of the 300 printing rows on the target
shape, 29 get the exactly-correct value but **lose the min-fold anyway**, because the pre-existing
`Legality` leaf's own `legal_cards * n_printings / n_cards` estimate is 5-13% SMALLER than the truth and
`min()` only lowers — the same undershooting idiom the occupancy round targets. And `banned:`/
`restricted:` queries are untouched: those keys sit below the floor (7,066 rows across all formats,
`banned:modern` is 403), and separately `client/query_sampler.py` only ever generates `f:`, so **no
survey in this arc has ever exercised them** — the pruning argument there rests on population size, not
on measured routing impact.

### Round 56

Target: `star:identity+cmc+usd`, reached by re-reading the post-Round-55 survey through the lens that
actually governs routing — **absolute** error big enough to cross a decision boundary, not ratio error.
That reframing changed the target completely, and is worth keeping as the standing lens: of 40,371
`root=and` rows only **2.5%** can flip a plan choice at all (straddle 1,024 with >=200 absolute and
>=10% relative error), and **83% of those are over-estimates**. Shapes with spectacular ratio error but
tiny absolute error contribute **zero** rows — `star:identity+pow+set` reads as the survey's single
worst shape by median abs-log-ratio (1.08, with individual queries 17-34x over) yet its absolute errors
are 30-100, structurally incapable of crossing any threshold. It was correctly left alone; chasing it
would have been ratio vanity.

**The diagnosis was exact**, traced directly rather than inferred: `ColorCmcTable` already returns the
EXACT `(identity, cmc)` joint, wins the arm's `.min()`-fold outright, and the price leaf then
contributes *nothing at all* because the joint is blind to it.

```
'cmc=4 id:b usd<0.21'  pred=3669  true=801
  HIT ColorCmcTable  printing=3669  [id:b, cmc=4]     <- exact, and it wins the min-fold
  HIT Independence   printing=5173  [id:b, usd<0.21]
  HIT Independence   printing=3966  [cmc=4, usd<0.21]
marginal usd<0.21 = 21014/97812 = 21.5%  ->  3669 * 0.215 = 788  vs true 801  (0.98x)
```

That is precisely Round 50's "anchored independence" one anchor over, and the followup queue's item
already named `ColorCmcTable` as a candidate with "no validated example yet." This round supplied the
example and shipped only that one anchor.

**The fudge factor was measured and rejected, not skipped.** The intuition (bias slightly upward so
errors land in the safe direction) is reasonable and was tested properly — swept at
1.05/1.10/1.15/1.25/1.50 over 70 queries sampled at random from the full 245-query printing-mode
population of the shape, deliberately NOT the straddling tail, since calibrating a constant on the rows
selected for being wrong is fitting the tail:

| factor | median | p90 | under-est | worst under | wrong side of 1,024 |
|---|---|---|---|---|---|
| current | 1.97x | 3.59x | 3/70 | 0.76x | 5 |
| **1.00** | **1.01x** | **1.28x** | 31/70 | 0.62x | **0** |
| 1.15 | 1.16x | 1.48x | 8/70 | 0.69x | 1 |
| 1.50 | 1.53x | 1.98x | 2/70 | 0.94x | 2 |

Every non-trivial factor made *routing* worse. The mechanism is obvious in hindsight and worth stating
because it generalizes: when the population being fixed is already 83% over-estimates, biasing upward
pushes genuinely-small queries back over the very line the round exists to get them under. A factor also
fails at the job it was proposed for — the worst under-estimate only moves 0.62x -> 0.69x at 1.15,
reaching 0.94x only at 1.50 where everything else is wrecked — because the under-estimates come from
real positive price/color-cmc correlation, not a uniform downward bias, so a uniform multiplier cannot
target them. Consistent with Round 38's own grid search finding `fudge = 1.0` strictly optimal for the
independence registry. **Do not re-litigate this on intuition.**

**Two implementation details that carried real measured weight.** First, the rate must come from the
`AndSource` (`children_estimates[i].result.printing`), never re-derived from the literal bounds:
`fuse_and_range_children` intersects same-index bounds into one `FusedRange`, and a naive product of two
one-sided marginals measured 1.98x/2.58x on two-sided-range queries where the fused rate gives
1.22x/1.23x. Verified end-to-end after merge — `usd>=0.23 usd<=0.31 cmc>=5 id:rw` lands at exactly
**1,025** (1.22x), the fused number, not 1,667. Second, this site deliberately does **not**
`mark_covered`, diverging from Round 50: `ColorCmcTable`'s own scan already refuses to cover its leaves
because covering starves BOTH `Independence` candidates at once and measured WORSE (median
`abs_log_ratio` 0.71 -> 1.12 on this very shape). Covering here would reintroduce that, and it is
unnecessary anyway — the anchored candidate is the strict minimum of every other candidate, so it wins
on merit. A test asserts that strict-minimum property, so it cannot silently start depending on
starvation.

**Independent re-verification** (my own wheels, `__file__` asserted; before = `costcell/trunk`
`c87781d6`, whose Rust is identical to Round 55's `47c5fbbf`): `cmc=4 id:b usd<0.21` 3,669 -> **788**
against true 801 (4.58x -> 0.98x) and `id:w usd<0.19 cmc>=5` 3,994 -> **744** against true 580 (6.89x ->
1.28x) — both matching the pre-scoping predictions *exactly*, and both flipping `StreamedSelect` ->
`GatheredScan`. `id:b cmc=4` with no price leaf is unchanged at an exact 3,669. `cargo test` 274 debug /
271 release, `clippy --all-targets -- -D warnings` clean in debug (CI's invocation); the release-only
`ARITH_TUPLE_BLOWUP_CARDS` dead-code error is the same pre-existing Round 51 artifact confirmed in Round
55.

**Three things worth recording honestly rather than burying.**

- **A pre-existing test's expected number legitimately changed** (`color_cmc_table_star_shape_wins_the_
  min_against_independence`, final `result.printing` 10 -> 5). That fixture is this mechanism's exact
  synthetic worst case: prices are index-monotone and green ∩ `cmc<=3` is *by construction* the ten
  cheapest printings, so price is perfectly redundant given the joint and independence undershoots by
  the full 2x. Every structural claim the test was written to make still holds and is still asserted.
  Real-corpus behavior runs the opposite direction (median 1.97x over -> 1.01x).
- **Some queries now predict exactly 0 against a non-empty truth** (`cmc=7 c:r usd:0.95`: 0 vs true 1) —
  a small joint times a small rate rounding to zero. Checked directly against the obvious risk (this
  repo has an open empty-conjunction-short-circuit issue): it does **not** short-circuit execution.
  Every plan that ran returned the correct rows and all plans agreed. The zero is purely a cost input.
- **The plan mispredicted one shape.** `star:legality+cmc+usd` was expected to improve and did not — it
  has no color/identity leaf, so `ColorCmcTable` never fires on it. Correct behavior, wrong expectation.

Timing: +245 ns on a ~1.05 us `and_estimate_ns` baseline, confined to queries that actually form the
candidate (paired A/B, same-build canary). Round 50's path measured flat under the hoist, which was the
specific risk of sharing the helper. The implementing agent also found and fixed, before merge, a real
+21% tax this was levying on `(color, cmc)` queries with no price leaf at all — a provably equivalent
precheck (with no `Price`-classified source anywhere, the residual bucket is empty regardless of what is
explained), verified behavior-neutral row-for-row across a full 64,581-row survey.

### Round 55

Target: `same_family:type+type_realistic`, found at **100% "(none)" mechanism coverage** in the fresh
post-Round-54 survey — `t:cleric t:spirit` fell to a plain per-leaf min-fold (628 against a true 19,
33x over) because `SubtypePairIndexes` only ever pairs a subtype against a DIMENSION (`set:X`/`c:X`/
`id:X`); nothing in this arm answered a bare (subtype, subtype) pair at all. Confirmed directly on the
before-wheel: all three spaces uncovered (`mechs=` empty), 1576/628/797 against true 64/19/26.

**Two departures from `SetSubtypeTable`'s existing shape, each validated on the real corpus before the
plan was written** (the full numbers live in
[local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md)'s own completed entry):

- **Union-of-three-spaces top-N**, not card-count-ranked alone. `Island`×`Swamp` (card=10,
  printing=107) sits outside the top-64-by-card cutoff but deep inside top-64-by-printing — a
  card-only sort key silently drops what's big in a dimension it never looks at. At N=256 the union
  adds 37 printing-only + 21 artwork-only pairs to 258 card-ranked ones (258 → 302, +17%). Same
  "no arbitrary exclusion" philosophy as Round 47's include-all-ties fix, extended from one axis to
  three.
- **A real per-space `rest_max` triple**, consumed natively, replacing card-space-then-scale. Measured
  on the same N=256 excluded population: printing-space-native independence+cap beat
  card-space-×-global-ratio at every percentile (median 0.42x vs 0.64x, p90 3.27x vs 4.45x, max 21x vs
  24.67x). The ratio-scaling step assumes a uniform reprint rate across all subtype pairs, which is
  false. Also checked (and rejected) four local per-leaf printings-per-card scaling variants — the best
  of them (`min(ratio_a, ratio_b)`) only tied on the tail while losing on median/p90.

Two things were deliberately kept rather than "simplified," each because the data said so.
`min(indep, rest_max)` stays a genuine two-part formula: pure `rest_max` overshoots the deep tail
flatly (true=1 pairs read 31x over at N=64), while pure independence overshoots badly for
anti-correlated common subtypes (`Human`×`Dragon` indep=53 vs true=1; `Human`×`Spirit` indep=91 vs
true=3, with 15/2157 excluded pairs at N=64 exceeding `rest_max` that way) — the cap is catching a
distinct failure mode, not adding redundant margin. And N stayed at 256 for **estimate accuracy only**:
checked explicitly against `STREAM_MIN_MATCHES`/`PAIR_MIN_PRINTINGS` (1,024 printings, the threshold
`PairTotals` already prunes by on the identical "below it the sparse floor decides the plan, not the
estimate's precision" principle), only **2 of 2,221** distinct subtype pairs clear it in any space
(`Human`×`Wizard` 1,527p, `Human`×`Soldier` 1,428p; #3 is already down to 880p, and zero pairs clear it
in card or artwork space). Since `And` is monotonically non-increasing, an excluded pair's contribution
to a larger query is bounded by its own small joint count regardless of N — so this mechanism
structurally cannot push an estimate across that routing boundary, and N is not sized by it.

**A real ordering constraint the implementing agent found, and the fix.** The plan placed both new
blocks together, right after `SubtypePairIndexes`/`SubtypePairEstimate`. That broke two PRE-EXISTING
tests (`subtype_arith_box_multiple_subtype_leaves_fold_via_min`,
`subtype_arith_anchored_independence_multi_subtype_leaves_use_their_own_box_hit`) — caught by the
existing suite, not by the corpus sweep. Root cause: `fold_candidate`'s min-fold is commutative in
principle, but an ESTIMATE that undershoots permanently pulls `result` below the truth the moment it
folds, and no later EXACT candidate can raise it back (the `Exact` arm only tightens via `.min()` too).
A two-bare-subtype-leaf query with an arith bound is simultaneously this mechanism's "exactly 2
uncovered" shape AND `SubtypeArithBox`'s "multiple subtype leaves" shape, so running the fallback first
let an undershot independence guess win outright over an available, tighter-but-larger exact box hit.
The fix: the ESTIMATE-class fallback moved to run AFTER `SubtypeArithBox`/
`SubtypeArithAnchoredIndependence`, so their `mark_covered` calls are reflected in `covered` before its
fresh scan — it then correctly declines on any leaf a real exact mechanism already explained. The
exact-hit scan stayed at the planned position: an exact hit is a valid bound on the whole `And`
regardless of order, so it cannot corrupt `result` the way an estimate can. **The general lesson,
already implicit in Round 40's class-priority finding but now demonstrated as a concrete ordering
constraint: estimate-class mechanisms must be positioned after every exact mechanism whose leaves they
could compete for, because `covered` only reflects what ran before them.**

**Independent re-verification** (my own wheels, `__file__` asserted against the isolated extraction
path per this session's own stale-binary lesson; before = `costcell/trunk` `be9fae2e`, after =
`47c5fbbf`): `t:cleric t:spirit` now **exact in all three spaces** — 1576→64, 628→19, 797→26 against
true 64/19/26, via `SubtypeSubtypeExact`. `t:human t:wizard`/`t:human t:soldier`/`t:warrior t:zombie`/
`t:aura t:curse` all exact too. The residual scan holds: `t:cleric t:spirit cmc<3` still hits
`SubtypeSubtypeExact` with the third leaf present (19 vs true 9, 2.11x — was `SubtypeArithBox`'s 155,
17.2x). The anti-correlated pairs land on the capped fallback as designed: `t:human t:dragon` 394→12
card / 1513→**21** printing against true 1, and `t:human t:spirit` 679→12 card against true 3 (226x→4x).
That printing value is worth naming — **21 is exactly the `rest_max_printing` I had computed
independently in Python at N=256 before the round was scoped**, an unplanned but clean cross-validation
that the union cutoff and triple `rest_max` are built the way the analysis assumed.

Correctness gates re-run by me: `cargo test` 269 passed debug / 266 release (the 3-test gap is
`#[cfg(debug_assertions)]`-gated, expected), `cargo clippy --all-targets -- -D warnings` clean in
debug (CI's exact invocation). The one release-only dead-code warning (`ARITH_TUPLE_BLOWUP_CARDS`) was
confirmed pre-existing rather than assumed so: `git log -S` traces it to Round 51's own `f051eaf7`, and
it is present on `costcell/trunk` itself.

Timing: the agent's paired canary measurement found the touched shapes not distinguishable from noise
on `type_realistic` (+56ns, CI [−8,121]) and a small but real +74ns (CI [63,85]) on `type_disjoint`,
against that shape's own ~700-2000ns `and_estimate_ns` baseline. My own paired check from the sweep
rows agrees on magnitude (+83ns on `type_realistic` against a 1584ns baseline, +0ns overall) but cannot
resolve the effect — my two sweeps ran concurrently, and untouched shapes wobbled ±83ns in both
directions, the same size as the signal. The agent's canaried numbers are the real measurement; mine
only rules out an order-of-magnitude blowup.

### Round 54

Target: `usd`×`tix`/`eur`×`tix`, surfaced by re-running the full-corpus survey specifically to check
what emerged once Round 53 stopped dominating it. Result confirmed Round 53 worked (`unsafe:usd+eur`
dropped from the single worst shape to near the bottom of the "worst-first" list, median 0.55+ → 0.08)
— but the next-worst shapes with real sample size were `unsafe:usd+tix` (median 0.55) and
`unsafe:eur+tix` (median 0.44). Checked directly: zero mechanism fires for either, the exact same
"2+ occurrences of `Price`, no combining table, dropped" fallback `usd×eur` had before Round 53.

**A real finding, chased down before scoping this round, not assumed.** This design doc's own earlier
calibration work had found `usd×tix`/`eur×tix` "net better" under plain independence — but that
apparently never got wired up (confirmed: 0% mechanism coverage, same as usd×eur pre-Round-53). Checked
what plain independence would give directly: `tix>0.12 usd<=0.13` — current min-fold predicts 10,438
against true 120 (87×); plain independence gives 1,313 (11×) — real, but far from tight. Then simulated
a full 2D quantile-bucketed joint histogram (the same approach validated for usd×eur in Round 53),
expecting the weak Pearson correlation (r=0.336) to cap how much it could help — instead it landed at
1.70× on the same query, dramatically better than plain independence's 11×. Two more `eur×tix` examples
confirmed the pattern. **The methodological lesson**: Pearson r only measures *linear* correlation — a
low r doesn't mean "no exploitable relationship," only "not a linear one." usd/tix and eur/tix apparently
have a real, non-linear, exploitable relationship a joint histogram captures and a correlation
coefficient alone does not.

**The fix**: generalized Round 53's `PriceJointTable`/`build_price_joint_table` past their `usd`×`eur`
hardcoding — `a_edges`/`b_edges` instead of `usd_edges`/`eur_edges`, two field-accessor closures instead
of hardcoded `p.price_usd`/`p.price_eur` (mirroring `build_numeric_index`'s own established
closure-accessor precedent). Three tables now exist (`price_joint_usd_eur` — the Round 53 rename —
plus new `price_joint_usd_tix`/`price_joint_eur_tix`), all via the same builder. One new shared
dispatch, `price_joint_table_for`/`resolve_price_joint_pair`, replaces the two hand-rolled
usd/eur-only `match` arms (the standalone whole-`And` fold and the `by_class` multi-arm) with a single
resolver covering all three order-independent pairs. The `and_sources.len() > 2` guard (Round 53's own
post-merge fix for the redundant-computation inefficiency) needed no changes at all — confirmed
directly it was never pair-specific, it only checks "is there anything left in the query to pair with."

**A real, re-checked-not-assumed calibration finding**: `PRICE_JOINT_BUCKETS` (64, validated for
usd×eur) needed no adjustment for the two new pairs, but the real corpus profile differs a lot —
`tix`'s own price values cluster far more heavily than usd/eur's (MTGO tickets trade in a narrower,
more discretized range), collapsing both new tables to only 22 real buckets on the `tix` axis (against
usd's 53 / eur's 50 on their own other axis) — but that same clustering makes the tables denser, not
sparser: `usd`×`tix` populates 1,072 of 1,166 possible cells (92%), `eur`×`tix` 993 of 1,100 (90%), both
above `usd`×`eur`'s own 64%.

**Verification, independently reproduced, including one discrepancy chased down to its real root
cause.** `cargo test`: 263 passed debug / 260 passed release (exact baseline+4 — 6 new tests, 2 old
"still declines" tests removed since that behavior is intentionally reversed by this round). `cargo
clippy --all-targets -- -D warnings`: clean on debug; release shows only the same pre-existing
`ARITH_TUPLE_BLOWUP_CARDS` dead-code warning confirmed unrelated in every prior round.

Rebuilt both isolated release wheels myself (before = fresh clone at `1de2a4b0`, after = the agent's
commit), `__file__` explicitly asserted both times, and reproduced all three motivating queries
directly via `explain()`/`and_trace`: `tix>0.12 usd<=0.13` 10,438→120 (true 120, exact); `eur<0.10
tix>0.11` 12,989→188 (true 186, 1.01×); `eur>=0.25 eur<=0.28 tix>1.36` 3,866→179 (true 93, 1.92×) — all
matching the agent's own reported numbers exactly. The third number is meaningfully looser than this
round's own pre-scoping simulation suggested (0.87×) — investigated directly rather than left
unresolved: my own Python simulation script had a real bug, approximating the two-sided `eur` range via
subtraction of two separately-computed one-sided cumulative queries, which introduces a boundary error
a direct rectangle-overlap scan doesn't have. A corrected direct-scan version of the same simulation
gives 1.22×, closer but still not identical to the Rust implementation's own 1.92× — the residual gap is
expected, ordinary divergence between two independently-implemented quantile-bucketing schemes (Python
approximation vs. the Rust implementation's own tie-safe construction, verified correct against a
brute-force scan by its own dedicated tests), not a defect in the shipped mechanism. Re-ran
`nway_estimate_truth_survey.py --compare` myself (fresh seed, 66,378 shared rows): 227 plan-choice flips
(0.3%), 100% confined to `unsafe:usd+tix` (108/900, 12.0%) and `unsafe:eur+tix` (119/900, 13.2%);
`unsafe:usd+eur` and every other shape at exactly 0.0%, `root=leaf`/`root=or` both 0.0%. Ratio
diagnostic mean abs-log-ratio −0.022 (95% CI excludes 0) — "B is MORE accurate".

### Round 53

Target: fix the worst-performing shape in the whole estimator, found via a fresh, full-corpus
accuracy survey (not a targeted follow-up from a prior round's own verification, for the first time in
this arc) — `unsafe:usd+eur`. `usd>0.75 eur<0.16` predicted 25,444 against a true 137 (185.7× over);
several other real tail queries in the same shape showed 75-180× error. Root cause: `IndepClass::Price`
bundles usd/eur/tix into one class, so once 2+ price leaves are present the independence registry's own
`by_class` bucketing hits its `_ => {}` catch-all ("2+ occurrences of a class with no combining table,
dropped") and neither leaf becomes a unit at all — the query falls back to a bare per-leaf min-fold, not
even a naive independence product.

**Validated directly against the real corpus before committing to a design.** Checked the actual
correlation rather than assuming: `usd`↔`eur` Pearson r=0.877 (strongly correlated — eur tracks a
related but genuinely separate secondary market, not just an FX rate), but the eur/usd ratio itself
spans p10=0.346 to p90=1.357 — too wide for a simple "translate the bound" rule to be tight. `usd`↔`tix`
r=0.336 (weak — MTGO tickets are a mostly-separate economy), consistent with this design doc's own
earlier finding that `usd`×`tix`/`eur`×`tix` measured "net better" under plain independence — confirming
this round should target `usd`×`eur` only, leaving `tix` alone. A Python 2D-histogram simulation,
quantile-bucketed (64 per axis, equal-*count* not equal-width — equal-log-width buckets were checked
and rejected: they concentrate 38.6% of all mass in 5 of 64 buckets, since MTG prices are heavily
skewed cheap), gave 1.03-1.52× on the five worst real tail queries — versus 75-180× under the fallback.
A real degenerate-bucket risk was also checked directly: the top 10 distinct `usd` price points (all in
the $0.15-$0.30 "bulk common" tier) each carry 1,500-1,700 occurrences on their own, comfortably more
than a single bucket's ~1,282-item target share at 64 buckets — naive quantile construction would
either split a repeated price across two buckets or produce empty ones. Fixed with tie-safe
construction: only cut at an actual value boundary, never inside a run of identical values.

**The architectural simplification that kept this well-scoped**: the `(usd, eur)` joint doesn't need to
model correlation with anything else — subtype, color, cmc, legality, etc. are all reasonably assumed
independent of the joint, exactly the same assumption already underlying every other independence
pairing in this codebase. So the fix didn't need a new end-to-end mechanism handling arbitrary N-leaf
queries — it only needed to answer `(usd, eur)` correctly in isolation, then plug into the *existing*
independence-registry pattern as one more unit, exactly mirroring how `IndepClass::Cmc | IndepClass::Pow`'s
own "multi" bucket already combines several same-field bounds into one unit before pairing outward via
the existing `independence_safe_pair`/product loop — no new combination math needed there.

**The implementation**, done by a background agent to a pre-approved plan: `PriceJointTable` — each
axis's own tie-safe quantile bucket edges (raw integer cents, not floats — this codebase already stores
prices that way everywhere else), plus a sparse `HashMap<u32, SpaceTotals>` over only the
`(usd_bucket, eur_bucket)` cell pairs that actually occur. No dense 2D array or prefix-sum table: a
plain linear scan over the sparse map (mirroring `ColorCmp`'s own `value_totals` precedent) at query
time, "any overlap counts fully" (no boundary interpolation — deliberately out of scope, a plausible
future refinement). Two call sites: a standalone whole-`And` fold (usd+eur alone, mirroring
`SetCollectorRange`'s own narrow 2-source gate) and a new special case in the `by_class` multi-arm
(usd+eur plus something else), both folding `Candidate::Estimate`, never `Exact`.

**A real, measured inefficiency found by the implementing agent and fixed before merging, not
deferred.** The agent's own report flagged that for a bare 2-leaf `usd+eur` query, *both* call sites
fire — the standalone fold, and the `by_class` arm (since `Price` has exactly 2 occurrences) — but the
`by_class` arm's own unit can never pair with anything in that shape (no third source exists to pair
against), so its own table lookup is pure waste. Measured directly: the bare-2-leaf case cost ~3.6-3.9µs
median against a 3-leaf case's ~2.7µs (a single scan). Rather than ship known waste and open a separate
follow-up round for what's a small, well-understood, safe fix, I applied it directly in the agent's own
worktree before merging: an `and_sources.len() > 2` guard on the `by_class` arm (zero behavior change —
`_ => {}` already declines to push a unit for any shape it doesn't recognize, and a query with nothing
else present was never reaching the pairing loop regardless), plus a new assertion in the existing
standalone-fold test confirming no `Independence` group appears for this shape (the arm is never even
attempted, not just outcompeted). Independently re-measured after the fix: median dropped to ~1.875µs,
almost exactly halved as predicted.

**Also flagged by the agent, correcting an assumption in the plan itself**: the real corpus populates
1,834 of 2,860 possible cells (55×52 buckets after tie-collapse, not the assumed 64×64) — 64% density,
not "far fewer" as the `ColorCmp` precedent's own doc implied at a much smaller (a few dozen entries)
scale. The linear scan is still fast in absolute terms (microseconds), but this is a real, non-trivial
cost documented honestly in the code rather than left as the original "cheap" assumption.

**Verification, independently reproduced.** `cargo test`: 259 passed debug / 256 passed release (exact
baseline+8, the new tests) — confirmed unchanged after applying the guard fix on top. `cargo clippy
--all-targets -- -D warnings`: clean on debug; release shows only the same pre-existing
`ARITH_TUPLE_BLOWUP_CARDS` dead-code warning confirmed unrelated in every prior round.

Rebuilt both isolated release wheels myself (before = fresh clone at `d45476e9`, after = the agent's
commit plus my own guard-fix commit), `__file__` explicitly asserted both times, and reproduced all
five motivating queries directly via `explain()`: `usd>0.75 eur<0.16` 25,444→139 (true 137, 1.01x);
`usd>0.52 eur<=0.18` 30,213→297 (true 286, 1.04x); `usd>1.75 eur<=0.38` 19,351→240 (true 193, 1.24x);
`usd>4.13 eur<0.86` 12,097→173 (true 140, 1.24x); `usd>0.42 eur<0.16` 25,444→327 (true 307, 1.07x) — all
matching the agent's own reported numbers exactly. Re-ran `nway_estimate_truth_survey.py --compare`
myself (fresh seed, 66,378 shared rows): 200 plan-choice flips (0.3%), 100% confined to
`unsafe:usd+eur` (690/900, 76.7% — up from a much lower pre-round coverage), `unsafe:usd+tix`/
`unsafe:eur+tix` and every other shape at exactly 0.0%, `root=leaf`/`root=or` both 0.0%. Ratio
diagnostic mean abs-log-ratio −0.010 (95% CI excludes 0) — "B is MORE accurate".

### Round 52

Target: close the `unique=artwork` acquire-path gap Round 51's own verification surfaced — queue item
#2 in [local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md). Traced precisely (with
one honest methodology stumble along the way, see below): `acquire_plan_features`'s `unique=printing`
gets its number from `printing_matches = est.result.printing` — `compose_printing_estimate`'s own
tightest fold, correct since Round 51. But `unique=card`/`unique=artwork` read from a completely
different, narrower function instead — `exact_result_total`, a hand-maintained mirror recognizing only
specific shapes (bare ranges, bare rarity, a 2-leaf `Eq`-only pair, `SetSubtypeTable`'s own arm, a
subtype+arith whole-query shape, a single bare arith leaf for Card mode only) — with no arm at all for a
pure 2+-leaf arith-tuple `And` (no subtype leaf involved). It declines, and artwork mode falls through to
`artwork_estimate`, a statistical projection that never looks at `compose_printing_estimate`'s own
result at all — even though `ComposeEstimate.result.card`/`.artwork` (the SAME `est` already computed a
few lines earlier) already carries a structurally-exact figure whenever any And-arm mechanism fires,
computed and thrown away.

**A real methodology stumble, caught and corrected before it produced a wrong finding.** My own first
attempt at re-verifying the gap used the stale, in-repo `.so` at `card_engine/card_engine/` — an
editable build from August 26, predating Rounds 48-51 entirely — via a bare `import card_engine`, which
gave misleadingly "already exact" results and nearly derailed the whole investigation. Caught by
checking `card_engine.card_engine.__file__`'s own path; every finding in this round, from both me and
the implementing agent, is from a freshly-built isolated wheel with that path explicitly asserted.

**A real regression in this round's own FIRST fix attempt, caught by the corpus sweep before
shipping.** The plan called for a plain `.min()`/`.or()` merge of `exact_result_total` with
`est.result.card`/`.artwork`, reasoning that both are "exact." That reasoning had a real gap:
`exact_result_total`'s own arms are each gated to require an EXACT shape-match of the WHOLE composed
filter (confirmed directly: every arm's own guard — `children.len() == 2`, `[a, b] =
children.as_slice()`, `arith_children.len() + 1 == children.len()` — can only fire when it explains
every child, never a subset), so replacing the calibrated fallback outright with it was always safe.
But `est.result.card`/`.artwork` can legitimately come from a mechanism covering only a SUBSET of the
And's children — exactly the "residual scan" architecture Rounds 42/48/50 spent this whole arc
building, on purpose, for the printing-space `result` field. Found directly on a real corpus query
while validating: `id:ruw usd:0.50 cmc>=2`, artwork mode — `ColorCmcTable`'s own exact `(identity, cmc)`
joint is a real, valid upper bound (21,048) but blind to the highly-restrictive `usd:0.50` residual
(true answer: 123). Applying it as an outright replacement regressed this query by two orders of
magnitude (123 → 21,048) — caught by `nway_estimate_truth_survey.py --compare`'s own ratio diagnostic
flipping to "B is LESS accurate" before the fix was ever committed as final.

**The shipped fix**: `est.result.card`/`.artwork` folded in as an ADDITIONAL `.min()` TIGHTENING layered
on top of the pre-existing calibrated-estimate baseline (always computed first, never skipped) — never
a replacement for it, mirroring how the pre-existing `domain_cards` tightening a few lines below already
uses the same field safely. Since `est.result.card`/`.artwork` is always a genuine upper bound on the
true count when it fires (it can only be too loose, never too small — `Candidate::Estimate` structurally
cannot touch card/artwork space at all, so no estimate-class candidate can ever contaminate it), this
merge is a mathematically strict tightening: it can only pull the reported value DOWN toward the truth,
never push it UP past a reasonable calibrated guess, regardless of whether the And-arm's own value
happens to be tight or loose for the FULL query. `exact_cards`/`exact_total` (from `exact_result_total`
alone) are untouched — already safe to adopt outright, as they always were.

**Verification, independently reproduced.** `cargo test`: 251 passed debug / 248 passed release (exact
baseline+3). `cargo clippy --all-targets -- -D warnings`: clean on debug; release shows only the same
pre-existing `ARITH_TUPLE_BLOWUP_CARDS` dead-code warning confirmed unrelated in every prior round.

Rebuilt both isolated release wheels myself (before = fresh clone at `d5192fb0`, after = the agent's
commit), `__file__` explicitly asserted both times, and reproduced all three key numbers directly via
`engine.explain()`: `cmc>=8 power<=2` artwork FINAL 15→13 (true 13, now exact); `cmc<=1 power>=1
tou>=1` artwork FINAL 1993→1400 (true 1400, now exact); and — the critical regression check —
`id:ruw usd:0.50 cmc>=2` artwork FINAL stays 123 (true 123) on BOTH wheels, confirming the shipped
`.min()`-as-tightening design does not reintroduce the 170x regression the first attempt caused.
Re-ran `nway_estimate_truth_survey.py --compare` myself (fresh seed, 66,378 shared rows): 184
plan-choice flips (0.3%), `root=leaf`/`root=or` both exactly 0.0%. Ratio diagnostic mean abs-log-ratio
−0.017 (95% CI excludes 0) — "B is MORE accurate". Went a step further than the aggregate stat:
computed a direct row-by-row comparison myself (not just the survey's own summary) — confirmed
**zero rows regressed**, 3,038 improved.

Blast radius: `card_engine/src/lib.rs` (+38/-2 lines, two `.min()`-tightening sites plus doc comments),
`card_engine/src/tests.rs` (+307 lines, 3 new tests: the real motivating shape driven end to end through
`acquire_plan_features` with an explicit "seed choice must not already undershoot" sanity check, a
direct unit test of the `.min()`-merge's own safety property, and a hand-built reconstruction of the
exact `id:ruw`-shaped regression proving the shipped code declines to let a partial-subset value leak
through).

### Round 51

Target: close the one gap Round 46's own census left standing — queue item #2 in
[local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md). `arith_tuple_count` gave an
exact CARD count for 2+ cmc/power/toughness/loyalty bounds ANDed together (via a scan over the ~564-key
`ArithTupleIndex`), but every one of its 3 call sites needed a PRINTING number too and got there by
scaling the exact card count by the corpus-average reprint ratio (`card_count * n_printings / n_cards`)
— an estimate, not exact, and two of the three call sites gave up on artwork entirely (`None`). This was
the one mechanism invisible to Round 46's `debug_assert!(cards<=artworks<=printings)` census, since it
folded as `Candidate::Estimate`.

**Validated against the real corpus BEFORE scoping this round** — a direct `oracle_id`-grouped check of
`corpus.jsonl` (no engine build needed), confirming this was a real, both-directions accuracy gap, not a
theoretical one: `cmc>=8 power<=2` (10 cards) — real 21 printings vs the scaling's 31 (48% too high);
`cmc<=1 power>=1 tou>=1` (1,046 cards, the largest population checked) — real 2,786 vs 3,225 (16% too
high); `power>=6 tou>=6` (840 cards) — real 2,874 vs 2,590 (11% too LOW, the opposite direction). Cheap
efficient creatures over-reprint relative to the corpus average; expensive/weak or toughness-light
expensive creatures under-reprint.

**The fix, worked out in discussion before the plan**: `SpaceTotals { printings, cards, artworks }` is
the exact pattern this codebase already uses everywhere else for this problem (`SetSubtypeTable`,
`SubtypePairIndexes`'s tables, `ColorCmcTable`, `SubtypeArithBox`'s own box) — its own doc comment states
why directly: "printings is not cards times a reprint rate, and artworks sits between them at a ratio
that varies per value." `ArithTupleIndex` already visits every one of a key's matching cards once at
BUILD time to collect `postings: Vec<Vec<u32>>` — summing each key's own real printing/artwork spans at
that SAME visit, once, costs nothing extra query time ever has to pay. Rejected in discussion: an
alternative that materializes IDs and sums spans at QUERY time (using the already-existing
`arith_tuple_ids` sibling function) — strictly worse, since it would pay an allocation on every query
this common 2+-arith-leaf shape reaches, versus this round's approach paying nothing extra at all.

**The implementation**, done by a background agent to a pre-approved plan and independently
re-verified end to end: `ArithTupleIndex` gained `totals: Vec<SpaceTotals>`, parallel to `keys`/
`postings`, computed in `build_arith_tuple_index` (now taking `offsets`/`artwork_base`, both already in
scope at its one call site). `arith_tuple_count` was renamed to `arith_tuple_totals` and now returns
`Option<(usize, usize, usize)>` (matching `subtype_arith_exact`/`color_cmc_exact`'s own established
shape) instead of a card count — replaced at all 3 of its call sites, not left alongside a now-redundant
card-only variant. The main `And`-arm joint (the one flagged by the census) now folds
`Candidate::Exact`; the independence registry's `Cmc`/`Pow` multi-unit gains real `artwork: Some(...)`
(previously always `None`, so `artwork_indep` could never be computed for any pairing involving it); the
single bare-leaf fallback gains real card/artwork instead of scaling/`None`. The Round 40
`single_arith_field`-gated `mark_covered` conditional — a different concern (single-field consolidation
vs. genuine cross-dimension joint) — was correctly left untouched. `ARCHIVE_FORMAT_VERSION` bumped
`2026082501` → `2026090201`, a real archive-layout change (new field on `ArithTupleIndex`), per this
repo's own established convention.

**Verification, independently reproduced.** `cargo test`: 248 passed debug / 245 passed release (exact
baseline+4). `cargo clippy --all-targets -- -D warnings`: clean on debug; release shows only the same
pre-existing `ARITH_TUPLE_BLOWUP_CARDS` dead-code warning confirmed unrelated in every prior round.

Rebuilt both isolated release wheels myself (before = fresh clone at `f56caa1f`, after = the agent's
commit) and reproduced both corpus-validated populations directly via `and_trace`, all 3 unique modes:
`cmc>=8 power<=2` printing FINAL 30→21 (true 21 via `explain_analyze`, now exact); `cmc<=1 power>=1
tou>=1` printing FINAL 3225→2786 (true 2786, now exact). `card` mode was already exact both before and
after (unaffected, correctly). Re-ran `nway_estimate_truth_survey.py --compare` myself (fresh seed,
consistent with the agent's own 66,372-row sweep): `root=leaf`/`root=or` both exactly 0.0% changed,
ratio diagnostic mean abs-log-ratio −0.003 (95% CI excludes 0) — "B is MORE accurate", matching the
direction and rough magnitude of the agent's own targeted-slice numbers (0.168→0.000 median on the
1,383 rows this mechanism won). Spot-confirmed `cards<=artworks<=printings` holds in both reproduced
examples (10≤13≤21, 1046≤1400≤2786) — consistent with the agent's own dedicated debug-build census
re-run (23,310 measurements, zero `debug_assert!` violations).

**A real, honestly-flagged pre-existing gap found during this round, not fixed (out of scope)**:
`unique=artwork`'s own top-level acquire path routes through a SEPARATE `artwork_estimate` function, not
through `compose_printing_estimate`'s `exact_domain_artworks` — so `unique=artwork` FINAL improves
(`cmc>=8 power<=2`: 22→15 against true 13) but doesn't fully close, even though the mechanism's own
`and_trace` entry correctly reports the exact artwork total (13) internally. Confirmed present, in a
worse form, on the unmodified before-wheel too — a pre-existing, unrelated gap, not introduced by this
round, and a plausible candidate for a future round.

### Round 50

Target: build the "anchored independence" candidate validated during Round 48's own review — queue
item #1 in [local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md). Even after Round
49's fix, `SubtypeArithBox`'s exact joint for `(subtype, arith-children)` (e.g. `t:elf ∩ cmc>=5` = 241)
stays price-blind, and the raw-marginal `Independence` estimates for the same query are BOTH looser than
the box's own bound (`Independence(cmc,price)=17056`, `Independence(Elf,price)=1665`, both worse than
241) — Elf's and cmc's own MARGINAL solo counts are much broader than their ACTUAL joint, so the box's
exact joint is a far better anchor than either marginal alone. Multiplying that exact joint by the
residual `Price` leaf's own solo rate gives a materially tighter estimate: `241 × (76189/97812 ≈ 0.779)
≈ 188` against true 177 (1.36x → 1.06x).

**Deliberately narrow scope**, mirroring Round 38's and Round 42's own "one mechanism, one class, prove
it, then expand" discipline: anchors ONLY `SubtypeArithBox`'s own hit (not `SubtypePairIndexes`/
`ColorCmcTable`, which would plausibly benefit the same way but have no validated example yet), and
combines the residual rate for ONLY `IndepClass::Price` (the one class with a validated real-data
example). Any other residual class, or an unclassified residual leaf, is simply ignored — dropped from
the product, same as the independence registry already does for classes it doesn't recognize; this is
safe, not a correctness gap, since ignoring a residual constraint only makes the resulting estimate a
bound on a LARGER population than the true query, and folding it in via `.min()` (the same accepted
`Estimate`-class convention `Independence` already uses) never introduces new risk, only sometimes
leaves accuracy on the table for a later round. Also deliberately conservative about the price-triple
correlation risk already documented in this doc: if 2+ residual leaves classify as `Price` (e.g. `usd`
and `eur` both bounded, unfused), the candidate declines entirely rather than multiply both rates in —
implemented as a single Rust slice-pattern match (`by_class[Price].as_slice()` against `[i]`), so any
count other than exactly one falls through to no candidate at all, mirroring the independence registry's
own "2+ occurrences of a class with no combining table, dropped" convention.

**The implementation**, done by a background agent to a pre-approved plan and independently re-verified
end to end: a new block right after `SubtypeArithBox`'s existing `scan_two_bucket_exact` call,
per-subtype-position, re-derives the box's own exact hit via a second (deliberately not
helper-threaded, keeping this fully decoupled from a helper shared with two other mechanisms)
`subtype_arith_exact` call, computes the hit's own explained leaf-position mask, scans `and_sources` for
every entry FULLY disjoint from that mask (not just non-overlapping at one leaf), buckets the residuals
by `IndepClass` (mirroring the independence registry's own `by_class` pattern), and — only when exactly
one residual classifies as `Price` — folds `Candidate::Estimate { printing: round(box_printing ×
rate) }` under a new mechanism name, `"SubtypeArithAnchoredIndependence"` (added to
`is_estimate_class_mechanism` for correct trace-tree attribution), and marks its own leaves (box's
explained set plus the contributing Price leaf) defensively covered, mirroring `SubtypePairEstimate`'s
own established convention.

**Verification, independently reproduced.** `cargo test`: 244 passed debug / 241 passed release (exact
baseline+5, matching the 5 new tests: the validated real-shape analog, the two-Price-residual decline
guard, an ignored non-Price class, a Price-plus-unclassified-residual combo, and a multi-subtype-leaf
fixture confirming each gets its own independent candidate). `cargo clippy --all-targets -- -D
warnings`: clean on debug; release shows only the same pre-existing `ARITH_TUPLE_BLOWUP_CARDS`
dead-code warning confirmed unrelated in every prior round.

Rebuilt both isolated release wheels myself (before = fresh clone at `59d2f5cb`, after = the agent's
commit) and reproduced the real numbers directly via `and_trace`: `t:elf cmc>=5 usd<10` (true 177)
tightens exactly from 241 to 188 — `SubtypeArithBox` still fires the bare 241, the new mechanism fires
at 188 and wins. `t:elf usd<0.20 cmc>=2` (true 366) — checked per the plan's own instruction to report
the real number either way, not assumed — ALSO improves, from 425 (the post-Round-49 winner) to 370
(1.16x → 1.01x), a genuine bonus the plan only asked to verify, not one it predicted. Re-ran
`nway_estimate_truth_survey.py --compare` myself (fresh seed, 66,363 shared rows): 14 plan-choice flips
(0.0%), 100% confined to `subtype_cube:type+cmc+usd` (9/900) and `star:cmc+type+usd` (5/900) — confirmed
directly in the raw compare output, `root=leaf`/`root=or` both exactly 0.0% changed. Ratio diagnostic
mean abs-log-ratio −0.002 (95% CI [−0.002, −0.001], excludes 0) — "B is MORE accurate".

**Timing, checked directly by the agent, not assumed free.** A second `subtype_arith_exact` lookup plus
an `and_sources` scan is a real, correctly-gated addition: queries where the mechanism actually fires
(subtype + arith + a single Price residual) showed a genuine, non-noise increase (~1.33µs→~1.63-1.67µs,
+22-25%, same-build canary confirmed this exceeds noise); a query with subtype+arith but no Price
residual (mechanism attempts the scan, then declines) showed a smaller real increase (~1.21µs→~1.33µs,
+10%); a query with no arith children at all (block never entered) was flat, within noise. The
price-triple guard itself was never exercised by any real corpus query in this sweep (checked directly:
zero rows combine a box hit with two distinct price fields — the only same-field-twice cases are
two-sided ranges, which fuse into one `AndSource` and correctly count as a single Price unit) — only the
dedicated unit test exercises it.

### Round 49

Target: fix the regression Round 48's own review surfaced — a live, measured cost of `covered`'s
pre-existing leaf-occupancy conservatism, tracked as queue item #1 in
[local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md). `covered: Vec<bool>` made a
leaf permanently unavailable to the independence registry the moment ANY mechanism touched it for ANY
partner — too conservative, since the only real danger (Round 40's own class-priority rule) is an
ESTIMATE-class candidate re-answering the IDENTICAL leaf subset something already answered, not a leaf
merely appearing in some unrelated covered pairing.

**The fix**, implemented by a background agent exactly to a pre-approved plan and independently
re-verified end to end: `covered` becomes `CoveredState { flags: Vec<bool>, subsets: Vec<u64> }`. `flags`
keeps the unchanged Round-40 semantics (still read by `SubtypePairEstimate`'s own narrow-leaf fallback,
deliberately untouched — same class of over-conservatism, a candidate for an identical future fix, but
out of scope here). `subsets` gets one bitmask pushed per genuine joint hit, from every one of
`mark_covered`'s 8 call sites and `pair_bounded_min`'s own inline `PairTotals`-hit writes (a real gap on
its own — without this, an exact `PairTotals` hit would still have blocked independence from ever using
either leaf again, since it doesn't go through `mark_covered` at all). The independence registry's
`is_covered` closure — used at exactly one call site, confirmed via a full `\bcovered\b` grep before
writing the plan — is deleted outright: every residual `and_source` that classifies into an `IndepClass`
now becomes an eligible unit regardless of leaf-occupancy status, and the narrower, still-real check
moves to pairing time: `let combined = a.mask | b.mask; if covered.subsets.contains(&combined) {
continue; }`. Only an EXACT combined-subset match is declined; sharing a single leaf with a covered
entry is fair game. Deliberately preserved: independence's own multiple pairs sharing a leaf (the design
doc's "star" cases) still don't self-block each other, since `subsets` is populated only by mechanisms
OUTSIDE the registry's own loop, run before it.

**What this round does NOT do, worth being precise about since it came up directly during scoping**: it
builds no new candidate value. `(Elf, price)` and `(cmc, price)` were already-existing independence
estimates the registry already knew how to compute (they fired pre-Round-48) — this round only removes
an artificial block on reaching them once `SubtypeArithBox` covers `{Elf, cmc}`. This is different from
the separately-queued "anchored independence" idea (multiplying an exact joint itself by a residual
leaf's own solo rate, a genuinely new fourth candidate) — not attempted here.

**Verification, independently reproduced.** `cargo test`: 239 passed debug / 236 passed release (exact
baseline+3). `cargo clippy --all-targets -- -D warnings`: clean on debug; release shows only the same
pre-existing `ARITH_TUPLE_BLOWUP_CARDS` dead-code warning confirmed unrelated in prior rounds. One
pre-existing test asserted the OLD leaf-occupancy behavior directly
(`and_arm_independence_never_overrides_an_overlapping_exact_pair_total`) — confirmed by hand-computation
that it exercises the *different-subset* case (a `legality×cn` independence pair against a
`legality×cmc` `PairTotals` hit — different subsets, correctly now permitted), not the identical-subset
one, so it was renamed and re-asserted to the new correct value (2,700, not weakened or deleted) rather
than just updated to pass. New tests: a direct analog of the real regression shape (confirms both
`(Type, Price)` and `(Cmc, Price)` now fire and the tighter wins); an isolated unit test of
`CoveredState`'s own mask-equality logic (no live `independence_safe_pair` combo today actually collides
with anything `PairTotals` can also answer — every registered-safe pair includes `Price`/`SetCode`/
`ReleasedDate` on one side, none of which `pair_leaf_id` supports — so the identical-subset protection
is tested directly rather than through a real end-to-end collision, as the plan's own sanctioned
fallback); a defensive `pos >= 64` guard test (a synthetic 70-leaf `And`, confirming no panic/overflow
and graceful degradation — not exercised by any real-shaped query in the suite).

Rebuilt both isolated release wheels myself (before = fresh clone at `73b2d5cf`, after = the agent's
commit) and reproduced the real numbers directly via `and_trace`: `t:elf usd<0.20 cmc>=2` (true 366)
recovers exactly to printing=425 — `SubtypeArithBox` still fires exact 1865, `Independence` now ALSO
fires for both `(cmc, price)`=16811 and `(Elf, price)`=425, `min(1865, 16811, 425)=425`, matching the
plan's predicted numbers exactly. Round 48's own motivating case, `t:elf cmc>=5 usd<10` (true 177), stays
at 241 on both wheels — `Independence` now also fires here (17056, 1665) but both lose to the box's own
tighter 241, confirming no regression. Re-ran `nway_estimate_truth_survey.py --compare` myself (fresh
seed, 66,366 shared rows): 378 plan-choice flips (0.6%), 100% confined to `root=and`'s `*+usd` star/cube
shapes, zero elsewhere (`root=leaf`/`root=or` both 0.0%) — confirmed directly in the raw compare output,
not just the agent's summary. Ratio diagnostic mean abs-log-ratio −0.034 (95% CI [−0.036, −0.032]) —
"B is MORE accurate", reversing Round 48's own "B is LESS accurate" finding; both previously-flagged
shapes recovered past their pre-Round-48 baseline.

**Timing, reported honestly rather than smoothed over.** Two pure canaries and the two motivating
queries showed no consistent change beyond same-build noise (which itself ranged up to ~60% on p90/p99
tails at this sample size). One previously-blocked-now-permitted shape (`t:human cmc>=2 cmc<=6 usd<1`)
showed a real, non-noise increase (median ~7.2µs→~11.7µs, exceeding the same-build swing observed for
that query) — an expected, real cost of now computing genuinely more `Independence` candidates for
exactly the population this fix targets, not a general hot-path tax on unrelated queries, but not free.

Blast radius: `card_engine/src/lib.rs` (+138/-50 lines: `CoveredState` struct, `mark_covered`/
`pair_bounded_min`/`scan_two_bucket_exact` signature changes, `is_covered` deleted, `IndepUnit` gains
`mask`), `card_engine/src/tests.rs` (+258/-14 lines, 2 new tests plus 1 renamed/re-asserted, plus a small
follow-up commit fixing a stale doc comment on an unrelated pre-existing test that still passes
unchanged under the new mechanism).

### Round 48

Target: generalize `SubtypeArithBox` past its Round 46-preserved restrictive gate
(`arith_children.len() + 1 == v.len()` — the WHOLE query must be exactly "one subtype leaf + N arith
leaves, nothing else"), the same restriction Round 42 already removed for `SubtypePairIndexes`.
Confirmed live before this round: `t:elf cmc>=5 usd<10` (a subtype leaf, an arith leaf, AND an
unrelated price leaf) got zero benefit from this mechanism.

**The change**, done by a background agent and independently re-verified end to end (diff, tests, both
build profiles, real corpus, root cause of every finding): `a_positions` now collects every
`subtype_pair_leaf` position in the whole query (not one position gated on the rest of `v`'s shape);
bucket B carries the arith-eligible children's real positions instead of a dummy candidate;
`order_positions` mirrors `SubtypePairIndexes`'s own `[a_position] ++ b_positions` closure instead of
"all of `v`", so `mark_covered_on_hit` covers only the leaves a given hit actually explains — critical,
since other, unrelated leaves (the `usd` above) must stay free for other mechanisms. One assumption
in the original plan didn't hold: bucket B's positions didn't need threading through the generic `B`
type at all (which would have needed a `Copy`-bound workaround for a `Vec`) — they fit directly in the
existing `Vec<usize>` slot the tuple already carries, the same slot `SubtypePairIndexes` uses, so `B`
stays `()`.

**Verification, independently reproduced.** `cargo test`: 236 passed debug / 233 passed release (exact
baseline+3, matching the 3 new tests). `cargo clippy --all-targets -- -D warnings`: clean on debug;
release fails only on the pre-existing `ARITH_TUPLE_BLOWUP_CARDS` dead-code warning, independently
reconfirmed present on unmodified `costcell/trunk` too (built the before-side wheel from a fresh clone
at `367c0f62`, ran clippy there directly — identical failure, unrelated to this round). Rebuilt both
isolated release wheels myself (before at `367c0f62`, after at the agent's commit) and reproduced the
motivating case directly: printing 1665→241 (true 177 via `explain_analyze`), card 922→135 (true 88),
artwork 1197→174 (true 107) — an ~9-11x overestimate shrinking to ~1.4-1.6x.

**The aggregate sweep is a real, measured net-worse result on the affected curated population, not an
artifact.** 66,378 shared rows: 74 plan-choice flips (0.1%), concentrated exactly where expected
(`subtype_cube:type+cmc+usd` 4.3%, `star:cmc+type+usd` 3.4%, zero elsewhere — confirming strict
superset behavior for the shapes that already worked). But the ratio diagnostic moved the wrong way:
mean abs-log-ratio +0.001 overall, "B is LESS accurate." Traced the specific mechanism by which this
happens, independently, via `and_trace` on both wheels for `t:elf usd<0.20 cmc>=2` (true printing=366):
before, `Independence` computes an estimate directly for `(Elf, usd<0.2)` = 425 (1.16x, a good estimate
that happened to land close by chance); after, `SubtypeArithBox` fires first on `(Elf, cmc>=2)` = 1865
(exact, but blind to the price leaf, 5.1x) and marks `Elf` covered — so `Independence` never even
*attempts* `(Elf, usd<0.2)` anymore, because `covered` is leaf-occupancy-based ("has this leaf been
touched by anything"), not subset-identity-based ("has this exact pair already been exactly
answered"). This is a live, measured instance of the pre-existing "loosen covered" gap already queued
(item #3 in [local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md)) — not a defect
in this round's own logic (`SubtypeArithBox`'s own bound is still a mathematically valid upper bound;
correctness, i.e. never undershooting truth, is never violated), but direct proof the gap has real
accuracy cost, not just a theoretical soundness concern. Presented to the user as an explicit tradeoff
before merging (ship an individually-safe mechanism whose aggregate effect on one curated population is
measurably worse, with the known fix already next in the queue) — decision: merge now, do the
covered-loosening fix next, using this exact query as its own motivating/verification case.

**A second, related, validated idea surfaced during this same review, not built this round.** Given
`SubtypeArithBox` now computes an exact joint for `(Elf, cmc>=2)`, and price is plausibly independent
of subtype+cmc, combining that exact count with the residual price leaf's own solo selectivity via
independence should beat the box's price-blind bound alone. Checked directly against the motivating
case: `t:elf cmc>=5` alone (no price leaf) gives the identical 241 as the 3-leaf query, confirming the
box's count really is price-blind; total corpus is 97,812 printings, `usd<10` alone matches 76,189 (a
0.779 solo rate); 241 × 0.779 ≈ 188, against true 177 — tighter than the box's own 241 (1.36x → 1.06x).
This is a different, more targeted idea than "loosen covered": rather than letting more raw leaf-pairs
compete, treat an exact mechanism's own joint as an anchor and multiply in the independent residual
leaf(s) as a new `Estimate`-class candidate, `.min()`-folded alongside the exact bound (so it can only
ever tighten, never violate correctness, since a solo rate is always ≤1) — and it needs no change to
`covered`'s semantics at all. Logged as a new item in
[local-engine-nway-followup-queue.md](local-engine-nway-followup-queue.md), kept in the existing queue
order rather than jumped ahead of the covered-loosening fix already agreed to be next.

Blast radius: `card_engine/src/lib.rs` (+46/-16 lines, one mechanism's call site, doc comment rewritten
to describe the generalized behavior), `card_engine/src/tests.rs` (+195 lines, 3 new tests: fires
alongside an untouched unrelated leaf and doesn't cover it, multiple subtype leaves fold via `.min()`,
declines with no arith children present), `scripts/nway_estimate_truth_survey.py` (+8 lines, new curated
shape `subtype_cube:type+cmc+usd`). Timing: single-shot `and_estimate_ns` from the sweep itself showed
no consistent direction (noise-level); a dedicated 200-rep timing harness (4,200 samples, 15 real
affected-shape queries × 3 modes) with same-build canaries found canaries agreeing within ~1% and a
~44% *faster* median after this change — `SubtypeArithBox`'s direct box lookup is apparently cheaper
than the independence-product fallback this population used to fall through to, a genuine (if
unexpected) improvement, not a regression.

### Round 47

Target: fix Round 46's own discovered nondeterminism at its root, not just document it.
`top_n_and_rest_max` (the shared cutoff all three of `SubtypePairIndexes`'s dimension tables — `set`,
`colors`, `identity` — call) sorted by card count with `sort_unstable_by_key` and no secondary tie-break
key. Since Rust's default `HashMap` hasher is randomly seeded per process, a pair tied at the exact
boundary value could land inside or outside the top-256 table depending on which process built it —
already reproduced directly in Round 46 (`t:monk usd>0.19 c:u` flipping between a table HIT and the
MISS fallback across repeated loads of the identical wheel).

**Two fix shapes, one chosen deliberately.** A plain deterministic tiebreak (sort by `(Reverse(cards),
key)`, the same shape `SubtypeArithBox`'s own top-N cutoff already uses correctly, `lib.rs:2377`) makes
the outcome reproducible but still arbitrarily picks a winner between two tied pairs. Chosen instead:
extend the cutoff to include EVERY pair tied at the boundary value, so membership becomes "count >=
threshold" — a real predicate, not an arbitrary per-pair pick. This also has a genuine stability
argument beyond "cleaner": under a fixed-size cutoff with a key tiebreak, one new printing nudging a
single pair's count can reshuffle the whole tiebreak order enough to flip membership for a *different*,
unrelated pair that never changed — because "top-256-by-count-then-key" is one total order. Under
threshold-based inclusion, a pair's membership only ever changes when that pair's own count crosses the
threshold.

**Checked against the real corpus before committing to the shape, not assumed cheap**: for `set`, the
value at position 256 is 27 with 25 other pairs tied there (268 total after extension). For `colors`,
independently confirmed by both the implementing agent and a direct ground-truth check (below) — NOT
by my own first two attempts at reproducing this in Python, both of which had bugs (see the honest note
at the end of this section) — the boundary sits at 38 with at least a 3-way tie (three different
`(color, subtype)` pairs — U×Monk, B×Advisor, BW×Cleric — all read exactly 38 real cards against
`explain_analyze`'s own ground truth, both before and after this fix, strongly corroborating the
agent's own reported 9-way tie at that value). `identity` shows no tie exactly at its own boundary in
this snapshot. The fix applies uniformly to all three regardless, through the one shared helper.

**Verification, independently reproduced end to end.** Built an isolated release wheel, ran it against
the real corpus 5 times with a fresh index build each time, checking all 4 previously-flipping queries
(`t:monk usd>0.19 c:u`, `t:warrior set:shm`, `c:b t:advisor`, `c:bw usd>=0.35 t:cleric`) across all 3
unique modes: byte-identical every single time. Additionally confirmed the now-stable table hits are
not merely stable but exactly correct: `c:u t:monk`/`c:b t:advisor`/`c:bw t:cleric` (bare 2-leaf
queries) each predict exactly what `explain_analyze` measures as ground truth, in all 3 spaces. A fresh
sweep (43,365 shared rows, seed 33): 43,239 rows unchanged, 125 improved-or-equal, exactly 1 single-row
regression (`t:angel c:b` printing, absolute error 10→13 against true=127) — reproducing the
implementing agent's own identical finding down to the same query and true_total, explained as the
capped independence-product MISS fallback's non-monotonic response to a slightly tighter `rest_max`
input (which only ever gets *more* conservative under this fix, never less — the fallback formula's own
behavior in response isn't guaranteed monotonic, so a single boundary-adjacent row can move either way
even though the underlying safety margin only tightened).

**An honest note on my own verification process.** My first attempt to independently reproduce the
`colors`-dimension boundary/tie count in Python (grouping by exact raw color-tuple, ignoring the real
Ge-cumulative logic) gave a materially wrong answer (boundary=25, 5 ties) that contradicted the
implementing agent's own report (boundary=38, 9 ties) — a real discrepancy, not glossed over. Reading
the actual build order (`lib.rs`: cumulative Ge/Le summing happens *before* the top-N selection, not
after) explained why my first script was wrong in kind, not just in degree. A second, more careful
attempt at reproducing the correct cumulative logic in Python (accounting for `card_colors` being a
dict-of-present-colors in the corpus JSONL, not a list) STILL gave a different number (boundary=57, 6
ties) than the agent's. Rather than keep debugging a third Python re-implementation, switched to
checking the real engine's own behavior directly against `explain_analyze`'s ground truth — which is
what actually matters for correctness — and that check strongly corroborates the agent's number, not
mine. The exact reconciled tie-count for `colors` is not independently nailed down to full certainty in
this doc; the engine's own correctness and determinism are.

Blast radius: `card_engine/src/lib.rs` (+53/-9 lines — one function's logic, two doc comments, no
mechanism decision logic touched), `card_engine/src/tests.rs` (+73 lines, 4 new tests: 3-way tie,
fewer-than-n, no-tie regression-safety, empty input). `cargo test`: 230 passed release / 233 debug.
`cargo clippy --all-targets -- -D warnings`: clean. `and_estimate_ns`: real delta −166ns, dwarfed by the
same-build canary's own ~28,000-41,000ns spread — no detectable tax, as expected for a load-time-only
change.

### Round 46

Target: structural cleanup, not accuracy — the `And` arm's ~10 hand-written mechanisms each hand-copied
the same fold at their own call site (a 4-line `.map_or(x, |d| d.min(x))` chain for exact mechanisms, a
1-line `result.min()` for estimate ones), and two of them (`SubtypePairIndexes`, `ColorCmcTable`)
independently hand-rolled an identical "scan for two kinds of leaf, Cartesian-product them, look up an
exact table, fold, cover, trace" shape. Real duplication, confirmed by direct discussion: Round 42 and
Round 44 each independently made (and had to separately catch) a variant of the same `mark_covered`
mistake in their own copy of this pattern.

**The invariant motivating this round**: `cards <= artworks <= printings` always holds for a real
population (every printing belongs to exactly one card and has exactly one artwork, with different
cards never sharing one). Pushing self-consistency into each mechanism's own candidate, rather than
clamping the final folded answer, is mathematically sufficient to guarantee the combined result
respects the ordering too, regardless of which mechanism wins which space (proof: let `j =
argmin(artwork)`; since `card_j <= artwork_j` by that candidate's own consistency and `min(card) <=
card_j` trivially, `min(card) <= card_j <= artwork_j = min(artwork)`; same argument for `min(artwork)
<= min(printing)`). So the fix belongs at the source, not the fold step — this round adds the
enforcement (a `debug_assert!`), not a fix to any violation found.

**What shipped**: one `Candidate` enum (`Exact{printings,cards,artworks}` / `Estimate{printing}`) and
one `fold_candidate` function, now the ONLY way any mechanism records a result; one generic
`scan_two_bucket_exact` helper with three callers (`SubtypePairIndexes`, `ColorCmcTable`,
`SubtypeArithBox` — the last confirmed, by direct code reading before this round was scoped, to already
fit the identical shape with a 3-D box query standing in for `ColorCmcTable`'s single range, migrated
with its existing `arith_children.len() + 1 == v.len()` whole-query gate deliberately UNCHANGED, not
generalized to a residual scan). Independence's own N-ary scan stays bespoke (a genuinely different
shape) but now folds through the same `Candidate::Estimate` entry point.

**The census — this round's actual deliverable, alongside the refactor**: walking every `and_trace`
tree directly (not just relying on the assert to fire) found ZERO violations from any of the six EXACT
mechanisms — every one already produces an internally self-consistent triple. It found 10,269
root-level violations across 3,421 distinct queries in a full 65,478-row sweep, ALL `artworks >
printings` (never `cards > artworks`), all attributable to Round 41's own already-known unclamped
floor — confirmed far wider in scope than the single `c:w t:plains` example on record, independently
reconfirmed at smaller scale (32% of `root=and` rows in a fresh spot sweep on a different seed).
`arith_tuple_count` was confirmed (not assumed) to be structurally invisible to the assert — it folds
as `Candidate::Estimate` (exact card count, but only a SCALED, not exact, printing conversion, and no
artwork at all), so it can never trip an assert scoped to the `Exact` variant. Nothing found here was
fixed — a follow-up round will use this census to decide what to fix, per Round 45's own deferred
"decline both estimates when a hub + 2 different partners co-occur"-style discipline.

**A significant independent discovery, found separately by the implementing agent and by me during
verification, converging on the identical root cause.** The first release A/B sweep showed 9
discrepant rows. Rather than accept or dismiss them, both investigations traced them to a real,
pre-existing bug, unrelated to this round: `build_subtype_pair_tables`'s top-256-per-dimension cutoff
(`items.sort_unstable_by_key(|item| Reverse(item.1.cards))`, `lib.rs:1917-1922`) has no deterministic
secondary sort key, and Rust's default `HashMap` hasher is randomly seeded per process — so a pair
tied at the exact boundary value can land inside or outside the top-256 table on one build/run and not
another. Reproduced directly, with zero code changes: the identical release wheel, re-run four times,
gave `t:monk usd>0.19 c:u` a genuine `SubtypePairIndexes` table HIT (card=58) on one run and the
`SubtypePairEstimate` MISS fallback (card=48) on the other three — same code, same corpus, different
answer. `t:warrior set:shm` (the sweep's own 9-row discrepancy) reproduces the identical flip on the
SAME unmodified before-wheel run against itself, confirming it's this bug, not a Round 46 regression.
**Flagged as high priority independent of this round**: it can make any future byte-identical
refactor's own verification look like it found a regression when it's really this — worth fixing before
it costs someone real debugging time. A related manifestation showed up in the harness's own query
generation too (`--seed 0`, same corpus, two separate engine loads produced 9 different queries) — the
same underlying class of bug propagating into what should be deterministic query sampling. ~~Not chased
down further here~~ — **closed** (a standalone Python fix, no round number): root-caused to
`client/query_sampler.py`'s `_count_row` folding oracle/flavor words via `Counter.update(set(...))` —
CPython's per-process string hash randomization makes bare-`set` iteration order vary run to run, so two
words tied on raw frequency that always co-occur in the same rows could insert into the counter in
either relative order depending on the hash seed; `most_common()`'s tie-break for equal counts falls
back to insertion order, so which tied word won silently depended on `PYTHONHASHSEED`. Fixed with
`sorted(set(...))`; verified byte-identical `generate_queries` output across 5 fresh process runs (was
20-32 line diffs before), plus a subprocess-based regression test (two different `PYTHONHASHSEED`
values, a corpus with deliberately tied co-occurring words) confirmed to fail on the pre-fix code and
pass after.

Blast radius: `card_engine/src/lib.rs` (+397/-96 lines — the enum, the two shared functions, ~10
mechanism call-site migrations, no computation logic changed), `card_engine/src/tests.rs` (+296
lines). `cargo test`: 226 passed release / 229 debug. `cargo clippy --all-targets -- -D warnings`:
clean. `and_estimate_ns`: +108ns mean, same-build canary swings -133ns — not distinguishable from
noise.

### Round 45

Target: not found by a benchmark sweep — found by hand-tracing a real query
(`set:mh2 usd<10 cmc<5 power>1 color:g`) after Round 44 shipped, checking whether the estimate was any
good, and noticing something a ratio metric alone would never flag: `compose_printing_estimate`'s own
predicted card (4762) and artwork (6680) both *exceeded* its own predicted printing (492) — an
impossible ordering for any real population (every printing belongs to exactly one card and has
exactly one artwork, so `distinct_cards <= distinct_printings` and `distinct_artworks <=
distinct_printings` always hold), not merely a loose estimate.

**Root cause, confirmed via `and_trace` directly.** A bare `TextExact{SetCode}` leaf's own solo
estimate carried `card: null, artwork: null` — so Round 41's card/artwork floor (a `min()` over every
uncovered leaf's own solo count) could never use `set:mh2`'s own true size as a ceiling, leaving an
unrelated exact joint (`ColorCmcTable`'s `color`×`cmc` pair, Round 44) to stand unchallenged at 4762/
6680. This is a general architectural default, not a `SetCode`-specific oversight:
`ComposeEstimate::leaf`'s own doc comment says outright *"a leaf with no cheap exact card/artwork
source... there is no space beyond printing to report"* — most leaf types fall through to it. Checked
directly against the real trace: `Colors` has both card and artwork; `Cmc`/`Power` have card but not
artwork; `SetCode` and `Price` have neither.

**The fix turned out smaller than the bug**: the missing data wasn't uncomputed, it was being
discarded. `set_totals` (built for Round 34's `SubtypePairIndexes`) already aggregates a full
`SpaceTotals` — printings, cards, AND artworks — per set, but `SetSubtypeTable` only ever kept
`.cards` into `set_cards`, throwing `.artworks` away. Added a sibling `set_artworks` map from the SAME
pass (no second aggregation), and wired both into the bare `set:X` leaf's own `ComposeEstimate` via
`ComposeEstimate::leaf_spaces` instead of the card/artwork-less `ComposeEstimate::leaf`. Round 41's
floor needed zero changes — it picked up the new `Some(card)`/`Some(artwork)` automatically, exactly as
designed. One real implementation deviation, caught by the implementing agent's own test run: defaulting
a miss to `Some(0)` (as first tried) broke an existing test by falsely flooring an unrelated 2-card
intersection to zero on a fixture whose `subtype_pairs` table isn't built at all — fixed by declining
to `None` on a miss instead, matching every other exact mechanism's own "decline rather than guess"
convention (`ColorCmcTable`'s empty-`by_mask` check is the identical shape).

**Independently re-verified end to end** (rebuilt both wheels myself, re-ran the motivating query and
the pre-existing-bug repro directly via `and_trace`, ran a fresh sweep on a different seed): card
4762→309, artwork 6680→391, both now correctly `<= printing`'s 492 (still far from the true 5-leaf
16/25/20 — this fix restores a correct floor, it doesn't add a new join). Sweep (44,511 rows, seed 21):
only 2 shapes show any plan-choice change at all, both `set:`-shaped (`same_family:set+set` 2.3%,
`unsafe:set+type` 1.0%), zero elsewhere.

**A second, separate, still-open bug found by the same investigation, NOT fixed here.** The
`card<=printing`/`artwork<=printing` invariant is violated elsewhere in the curated catalog too —
`c:w t:plains` (true_total 0 in every space) predicts card=40/artwork=511 against printing=24, and
this is confirmed **byte-identical on the pre-Round-45 wheel** — genuinely pre-existing, not introduced
by this round's own fix. Root cause, traced: Round 41's own card/artwork floor takes a leaf's solo
count as a candidate without a final `.min()` clamp against the query's own `result.printing` — a
leaf's card/artwork count can itself legitimately exceed some OTHER, tighter mechanism's printing
answer (e.g. `SubtypePairEstimate`'s own estimate here), and nothing currently stops that from
surfacing as the final card/artwork answer even though it can never be true. Flagged as the natural
next fix — small, well-understood, and worth designing into whatever shared fold path a future
refactor round builds, rather than patched in ad hoc here.

Blast radius: `card_engine/src/lib.rs` (+43 lines — one new field, one extraction loop, one leaf
dispatch change), `card_engine/src/tests.rs` (+143 lines, 3 new tests). `cargo test`: 218 passed
release / 219 debug (215+3/216+3). `cargo clippy --all-targets -- -D warnings`: clean.
`and_estimate_ns`: real delta within the same-build canary's own noise floor, not distinguishable.

### Round 44

Target: fix Round 43's own confirmed-bad "star" — `color:G cmc<=3 usd<=10`-shaped queries firing two
never-jointly-calibrated independence estimates simultaneously (`ColorId`×`Price`, `Cmc`×`Price`,
sharing the `Price` hub) and landing measurably worse than either component's own baseline. Digging
into why, checked directly against the corpus: color/identity count correlates with cmc in a real,
mostly-monotonic way (mean cmc climbs from ~2.0-3.3 at 0-1 colors to ~4.5-5.6 at 3-5 colors) — the
direct consequence of needing more colored mana symbols plus WotC's own color-pie curve conventions.
The fix isn't to decline the star — it's an EXACT `(colors|identity) x cmc` table, the same way Round
34 built `SubtypePairIndexes` for `(colors|identity|set) x subtype`. `ColorId`/`ColorIdentity`×`Cmc`
were never registered against each other in the independence registry at all (only against `Price`),
so this fills a real gap rather than touching the registry.

**Feasibility, checked directly**: only 32 distinct color masks (all of WUBRG's 2⁵ combinations
appear) and 17 distinct cmc values (0-16) — a table on the order of 32×17 cells, trivially small, no
top-K-plus-fallback capping needed (unlike `SubtypePairIndexes`, which caps to the top 256 because the
`(set, subtype)` space is much bigger).

**Design, deliberately NOT `ColorSubtypeTable`'s lattice pre-summing.** `ColorSubtypeTable` bakes "sum
over every matching `Ge`/`Le` mask" into each of its 32 entries at build time — a real cost/complexity
trade worth making for a large sparse `(mask, subtype)` space, but its own doc comment documents a
real, previously-unnoticed asymmetry bug from doing exactly this (`colors` cumulates `Ge`, `identity`
cumulates `Le`). At only 544 cells, risking that bug class again wasn't worth it. Chosen instead: RAW
per-exact-mask buckets (no lattice pre-summing across masks at all — the `Ge`/`Le` distinction only
ever appears once, at query time, as an `op` parameter to `color_cmp_matches`, the SAME real per-card
matcher, not a second hand-rolled implementation), with each mask's OWN cmc dimension prefix-summed —
mirroring `RangeCardCounts` exactly, since that axis (ordered, numeric) has no directional ambiguity
the way the mask axis does. `colors` and `identity` stay two separate tables (checked directly: they're
equal for 94.1% of distinct cards, but genuinely differ for the other 5.9%, and answer different query
shapes — `Ge`/superset vs `Le`/subset — regardless), built from one shared per-card scan.

**Real results, independently re-verified end to end** (not just the implementing agent's own report —
rebuilt both wheels myself, re-ran every query below directly via `and_trace`, ran a fresh sweep on a
seed neither the agent nor Round 43 used):

- The pure 2-leaf case is now EXACT in all three spaces where it used to be a plain, sometimes-loose
  fold: `color:G cmc<=3` card 3468/3468 (was already coincidentally exact), printing 10268/10268 (was
  18721/true 10268 before — a real fix, not a coincidence); `id:UG cmc<=3` printing 25487/25487 (was
  39385/true 25487); `id:UG cmc>=1 cmc<=5` (a two-sided bound, two literal children intersected) card
  10421/10421, printing 30050/30050 — also exact, confirming the range-intersection helper.
- The 3-leaf star: `color:G cmc<=3 usd<=10` `eval_domain` 6450→3468 against true 3363 (ratio 1.92→1.03);
  `id:U cmc>=5 usd<=5` `eval_domain` 7102→1543 against true 1373 (ratio 5.17→1.12).
- Aggregate sweep (65,541 shared rows, fresh seed 11): only 3 shapes show ANY plan-choice change at all
  (`star:identity+cmc+usd` 13.4%, `star:color+cmc+usd` 12.4%, `triple:color+type+cmc` 1.3%), zero
  elsewhere; median abs-log-ratio `star:color+cmc+usd` 0.80→0.52, `star:identity+cmc+usd` 0.71→0.55 on
  this seed (agent's own seed-0 numbers: 0.80→0.58, 0.71→0.57 — consistent direction and magnitude
  across two independent seeds). The "swept trio" from Round 43 (`legality`/`color`/`identity`×`price`)
  is confirmed byte-for-byte unchanged, as expected — unrelated mechanism, untouched by this round.

**A real regression found and fixed by the implementing agent itself, before I ever saw it as a
maintainer.** My own instructions said to `mark_covered` on a table hit, matching every other exact
mechanism's convention in this arm. Measuring against the real corpus showed this was actively
harmful: median `abs_log_ratio` for `star:color+cmc+usd` moved 0.80→**1.08** (worse) with `mark_covered`
in place. Root cause: this table only bounds the `(color, cmc)` pair, ignoring price entirely, and on
the star's own query population that 2-leaf bound is routinely LOOSER than what `ColorId`×`Price`/
`Cmc`×`Price`'s independence candidates give (those at least incorporate price, if only via the
independence assumption). Marking both leaves covered starves BOTH independence candidates at once
(neither has a partner left to pair against `Price` once both are gone), leaving the final `min()`
holding only this mechanism's own looser number. Removing `mark_covered` — letting all three candidates
compute independently and `.min()`-fold together — fixed it (0.80→0.58). This is safe, not just
empirically lucky: unlike two ESTIMATE-class mechanisms compounding on the IDENTICAL two leaves (Round
40's own concern), Independence's two candidates here each share only ONE leaf with this mechanism's
pair (never both) — genuinely different sub-conjunctions, not competing answers to the same question.

Blast radius: `card_engine/src/lib.rs` (+347 lines — `ColorCmcTable`/`ColorCmcIndexes`/`MaskCmcCounts`
structs, `build_color_cmc_tables` and helpers, the `And`-arm residual-scan addition, the
`exact_result_total` 2-leaf shortcut), `card_engine/src/tests.rs` (+241 lines, 5 new tests covering
the 2-leaf hit, the star's min-composition before/after the `mark_covered` fix, `Ge`/`Le` divergence,
two-sided range intersection, and the no-op case). `cargo test`: 215 passed release / 216 debug (210 +
5/211 + 5). `cargo clippy --all-targets -- -D warnings`: clean. `and_estimate_ns`: real delta a few
hundred ns on the two target shapes, within the same-build canary's own ~6% noise floor at this sample
size — not claimed as a proven cost, consistent with every prior round's own honest reporting here.

### Round 43

Target: re-scoping "hand the general N-way partition search to an agent," the design doc's own
carried-forward blocker was "triple-level (3+-leaf) independence safety hasn't been re-checked —
pairwise-safe does not imply joint-safe," resting entirely on one inherited claim (`color`×`identity`
"invisible pairwise, real correlation as a triple"). Before scoping a fix, or even an investigation,
I re-read `independence_safe_pair`'s actual match arms (`lib.rs:8455-8477`) and built its adjacency
graph — the same "verify before building on it" discipline that corrected the "placement rule" framing
last round. **Finding: there is no triangle.** `Price` is a hub with 5 partners
(`Legality`/`ColorId`/`ColorIdentity`/`Cmc`/`Type`), none of which are registered against each other;
`SetCode`'s 2 partners (`ColorIdentity`/`Pow`) aren't registered against each other either. So the
literal "does a true 3-way joint-independence assumption hold" question can't be triggered by any query
today — nobody could hit it even trying. The doc's own motivating claim turned out to describe a
combination that was never added to the registry at all: `color`×`identity` is 100%-covered by exact
`PlanePopcount` (Round 40's own finding), never an independence candidate.

**What IS real, reachable, and unvalidated: a "star."** The registry's residual scan
(`lib.rs:9705-9739`) iterates every PAIR of present residual classes and independently `.min()`-folds
each confirmed-safe pair — so a 3-leaf query like `color:G cmc<=3 usd<=10` (residual `{ColorId, Cmc,
Price}`) fires BOTH `ColorId`×`Price` and `Cmc`×`Price` simultaneously (their common partner, `Price`,
is the hub; `ColorId`×`Cmc` itself isn't registered, correctly skipped). Each pair was calibrated on
its OWN 2-leaf-only sample (Round 38/40); nobody had checked whether folding TWO
independently-valid-but-never-jointly-tested estimates via `min()` degrades accuracy once a third leaf
is actually present.

**Measured directly** (curated `star:*` shapes added to `scripts/nway_estimate_truth_survey.py`,
following the existing `TRIPLES` pattern; three independent seeds, 0/999/7, the last run and verified
by me independently rather than trusting the investigating agent's own report alone):

- `star:color+cmc+usd` and `star:identity+cmc+usd` are substantially worse than either component's own
  2-leaf baseline — median abs-log-ratio roughly 3.5-32x worse depending on seed/sample (my own
  independent seed-7 sweep: `star:color+cmc+usd` median 0.56 vs. `safe:color+usd` 0.13/`safe:cmc+usd`
  0.16; `star:identity+cmc+usd` median 0.74 vs. `safe:identity+usd` 0.14/`safe:cmc+usd` 0.16).
  Confirmed via direct `and_trace` inspection on real queries (`color:G cmc<=3 usd<=10`: both
  `Independence` groups fire, `eval_domain`=6450 against true=3363; `id:U cmc>=5 usd<=5`: eval_domain
  =7102 against true=1373) that the "two simultaneous Independence estimates" mechanism is exactly what
  fires here, not assumed from reading the code alone.
- `star:identity+pow+set` and `star:cmc+type+usd` show the same direction of degradation, on smaller
  samples (real signal, magnitude less settled).
- `star:legality+cmc+usd` and `star:legality+type+usd` are only mildly worse, close to their
  components' own baseline noise.
- **An unplanned, independently-reproduced-on-a-third-seed second finding**: the 3 star candidates that
  get swept by an EXACT mechanism before independence ever fires at all
  (`legality+color+usd`/`legality+identity+usd`/`color+identity+usd` — `PlanePopcount` claims the two
  card-invariant leaves, leaving the price leaf plain-min-folded against that exact joint) are
  themselves worse than either component's own baseline too (my own seed-7 sweep: median 0.97-1.05,
  matching the pattern found on seeds 0/999) — a genuine 3-way `legality`/`color`/`identity`/`price`
  correlation the current `min(exact-2-leaf-joint, solo-price-leaf)` fold misses. Different mechanism
  from the double-independence question, found by the same investigation, not chased further.
- **The `color`×`identity`×subtype spot check** (`c:X id:Y t:Z`-shaped queries, directly, not via the
  harness's curated catalog): printing space is fine (abs log ratio 0.001-0.35 — two independent exact
  mechanisms, `PlanePopcount` and `SubtypePairIndexes`, happen to cross-cover it); card space is not
  (0.48-1.18, e.g. `c:r id:ru t:dragon` predicted 568 vs. true 174) — but this is `exact_result_total`'s
  own card-space `matches` computation, a SEPARATE code path from `compose_printing_estimate`'s
  `eval_domain` (which is well-behaved for this query, 244 against true 174) — a real but narrower gap
  than the doc's inherited claim suggested, not investigated further.

**Deliberately not fixed this round** (measurement only, per the round's own scope). The simplest
candidate for a follow-up: decline BOTH independence estimates (fall back to plain min-fold) when a
hub class and 2+ of its DIFFERENT registered partners are simultaneously present in the residual —
the same "ambiguous → decline" precedent the registry already uses for same-class duplicates and the
`SubtypePairEstimate` fallback, rather than assuming "pick the tighter one" is safe (Round 40's own
class-priority finding: an estimate isn't a guaranteed bound, so tighter isn't the same as more
accurate). Not designed or built here.

Blast radius: `scripts/nway_estimate_truth_survey.py` only (11 new curated `star:*` shapes,
28 lines) — no `card_engine/src/lib.rs` changes, per this round's own scope.

### Round 42

Target: found while re-scoping the general N-way partition search after Round 41 — I had framed the
remaining gap as needing a "placement rule" (which mechanism gets first claim on a leaf when multiple
candidates want it), motivated by Round 41's own finding that `SubtypePairIndexes` (Round 34) never
fires on a 3+-leaf `And` (`v.len() == 2` gate) while `compile_plane` claims `color`+`legality` together
regardless. That framing was overstated. `exact_domain_cards`/`exact_domain_artworks` already
accumulate via `.map_or(x, |d| d.min(x))` chaining across every EXACT/bound mechanism that fires,
regardless of order (the arith-ID-probe merge and `SubtypePairIndexes`'s own hit branch both already do
this) — and this composition is provably always safe for this class: any true sub-conjunction's own
exact count is a valid upper bound on the full `N`-leaf `And`, no matter what other leaves are present
or what else already fired. There is no race to adjudicate. The real gap was narrower: this mechanism
simply never COMPUTED a candidate past two total leaves — an applicability-gate limitation, not a
conflict-resolution problem.

**Generalized the gate, not the ordering.** Replaced `if v.len() == 2` with a residual scan bucketing
uncovered leaves into "dim" (`set:`/`color:`/`id:`) and "subtype" (`t:`) positions, trying
`subtype_pair_exact` on every pair in the Cartesian product. A HIT `.min()`-chains into `exact_domain_*`
exactly like every other exact mechanism, and gets its own trace group per pair (mirroring
`compile_plane`'s own existential-loop precedent of "every trial gets its own group, not just the
winner"). The capped independence-product fallback (`SubtypePairEstimate`) stays deliberately
single-pair-only: with 2+ uncovered dim or subtype leaves and no table hit, it declines entirely rather
than computing an estimate per combination and taking their min — the same "an inexact estimate can
undershoot, so combining several risks compounding it" reasoning the independence registry's own
ambiguity precedent already established.

**The first pass shipped, then a real gap was found before merging.** My own instructions to the
implementing agent said to skip `covered` leaves for the whole residual scan, copying the independence
registry's pattern uncritically. Verifying the motivating example directly (not just trusting the
sweep's aggregate numbers) showed `color:G format:pioneer t:elf` got ZERO benefit: `color` is itself
`compile_plane`-compilable and gets absorbed into its joint with `format:pioneer` (marked `covered`)
*before* this mechanism's scan ever runs, so with `covered` leaves excluded, `color` never reached the
new (dim, subtype) scan at all. This is exactly the case Round 41's own worked example was about. The
fix: only the ESTIMATE fallback needs to respect `covered` (an inexact estimate must never be folded in
for a leaf subset an exact mechanism could undershoot below — the same class-priority soundness rule
Round 40 established); the EXACT-hit scan should ignore `covered` entirely, since a real table hit's
own count is unconditionally safe to `.min()`-fold in regardless of what else covered either leaf.
Re-verified end to end after the fix (not just the implementing agent's second report): rebuilt both
wheels myself, re-ran the motivating query and every border-leaf reproducer directly via `and_trace`,
and ran a fresh independent harness sweep on a different seed — the motivating example is now a
confirmed table hit (`eval_domain` 660→560), the pre-existing 2-leaf case is unregressed, and two new
3-leaf shapes (`set:eld t:knight usd<=5`, an exact hit; `set:znr t:elf usd<=5`, the estimate fallback)
both behave correctly.

Blast radius: `card_engine/src/lib.rs` (`subtype_pair_exact`'s signature loosened from a 2-element slice
to two individual leaf refs, one extra caller in `exact_result_total` updated to match; the
`SubtypePairIndexes`/`SubtypePairEstimate` block itself, ~140 lines net), `card_engine/src/tests.rs`
(7 new tests, all Round 34 tests pass unmodified), `scripts/nway_estimate_truth_survey.py` (two new
curated 3-leaf shapes). `cargo test`: 211 passed (204 + 7). `cargo clippy --all-targets -- -D
warnings`: clean. `and_estimate_ns`: no measurable tax detected (paired delta well inside baseline's
own single-shot noise floor), consistent with every prior round's own finding for this style of
`O(leaves²)`-but-tiny-in-practice addition.

### Round 41

Target: found while scoping "hand the general N-way partition search
([local-engine-nway-compose-independence-search.md](local-engine-nway-compose-independence-search.md))
to an agent" — not a planned round. Checking the design doc's own worked example
(`color:G AND format:pioneer AND t:elf`) against the real engine (expecting to confirm a placement-
ordering story) instead found card/artwork space badly under-tightened for a reason unrelated to
placement: `t:elf` already has an exact solo count in all three spaces (the same `value_totals`
lookup every bare containment leaf uses — 660 card / 2138 printing / 913 artwork on the corpus
snapshot measured), and printing space already floors on it (`result` starts from
`pair_bounded_min(v, indexes, folded.result.printing, ...)`, where `folded.result.printing` is a
plain, unconditional min-fold over every leaf's own printing count) — but card/artwork space had no
equivalent. `exact_domain_cards`/`exact_domain_artworks` are populated ONLY when some specific
multi-leaf intersection mechanism fires (here, `compile_plane`'s `color`+`legality` joint), and were
never subsequently folded against the OTHER leaves' own already-exact counts.

**Not a fresh bug — a previously-scoped, deliberately-deferred fix.**
[local-engine-domain-cards-existential-arith-and.md](local-engine-domain-cards-existential-arith-and.md)'s
"Ingredient 3" investigated exactly this fold and explicitly did not ship it: unconditionally, it
risked resurrecting the retired `domain_hint` bug, because a BROAD leaf's own count (`border:black`
at 87% of the corpus) is a mathematically valid upper bound but a misleading one for a DIFFERENT
consumer — `acquire_plan_features`'s `scan_units`, which prices how many rows a real execution plan
will visit, and `narrow_rec` declines to use a broad leaf as its narrowing driver at all. That
round's own conclusion: "a future round could revisit it WITH an equivalent breadth guard... that is
new scope." This round builds that guard, reusing `range_too_broad_to_narrow` (`NARROW_FLOOR=1000`,
`MAX_NARROW_FRACTION=0.25`) as-is — the exact threshold `narrow_rec`'s own `broad_ok` gate already
uses, not a new one invented for this fix.

**The fold is scoped to `result_space` only, never `exact_domain`.** `result_space` and `exact_domain`
happened to be IDENTICAL for card/artwork before this round (both read the same
`exact_domain_cards`/`_artworks` variables) — unlike printing, where they already legitimately
diverge (`result` gets every tightening; `exact_domain_printing` is captured earlier, before
`pair_bounded_min`'s own pass). The new floor only ever changes `result_space.card`/`.artwork` — the
field `explain()`/`explain_analyze()` report and the harness measures against `true_total` — leaving
`exact_domain_cards`/`_artworks` (and therefore `scan_units`'s real cost-pricing input) completely
untouched. This sidesteps the `domain_hint`-conflation risk by construction rather than relying on
the breadth guard alone: even if the guard's threshold were ever wrong, this floor cannot corrupt
plan-cost pricing, because it no longer shares a variable with it.

**Verification.** New `cargo test` regression tests (`compose_and_arm_narrow_floor_diverges_result_
space_from_exact_domain`, `..._narrow_residual_leaf_tightens_card_and_artwork_floor`, `..._broad_
residual_leaf_does_not_tighten_card_or_artwork_floor`) confirm the divergence from `exact_domain`, the
floor firing on a narrow uncovered leaf, and the floor declining on a broad one. `cargo test`: 202
passed (199 + 3, one existing round-22 fixture needed a `value_totals` build it had silently been
missing, a latent gap invisible until this round's floor started reading per-leaf totals it never had
before). `cargo clippy --all-targets -- -D warnings`: clean. Independently re-verified end to end
(not just the implementing agent's own report): rebuilt both wheels myself, re-ran the motivating
query and every border-leaf reproducer directly, and ran a fresh independent harness sweep on a
different seed — all numbers matched the implementing agent's own report.

**Blast radius**: `card_engine/src/lib.rs` (~32 lines, the `And` arm's final `result_space`
construction only — `exact_domain`'s own construction is byte-identical to before), `card_engine/src/
tests.rs` (three new tests plus a fixture fix). No change to `SubtypePairIndexes`, `compile_plane`,
or the independence registry — those are a separate, still-open follow-on (see
[local-engine-nway-compose-independence-search.md](local-engine-nway-compose-independence-search.md)'s
own note on the `color+legality+subtype` 3-leaf joint, confirmed live and unaddressed by this
investigation but deliberately out of scope for this round).

### Round 40

Target: not one more hand-written branch — generalize Round 38's single hard-coded independence pair
(`color`/`id`/`cmc` × `usd`/`eur`/`tix`) into a small, re-validated registry scanned over every
residual leaf, plus fix a real ordering bug Round 38's own test surfaced. Explicitly a bounded first
slice of the design doc's full vision (not the N-choose-3/4 packing search, not cost-aware ordering,
not the `popcount_with_bits` fix) — see the plan discussion for the staging rationale.

**The design doc's own safe/unsafe list needed correcting before any registry could be built from
it**: `legality×{cn,price,set,year}` was listed safe and `legality×date/set` unsafe in the same
paragraph. Resolved by domain semantics, not an A/B test: format legality is *defined* by a
release-date cutoff, and `set:X`/a date/a year all pin the same underlying variable legality already
depends on — a second latent error beyond the contradiction (`legality×year` is unsafe for the
identical reason, not just `×set`). But "no true independence" is the norm in this domain, not the
exception — even `legality×price`, the cleanest safe pair, has a real exception (Alpha: Reserved-List
overrepresented, commands an "original printing" premium independent of playability) — so the actual
bar stayed empirical (does `min(fold, independence)` net-improve over plain fold in aggregate,
including the hard cases), not a veto by finding *a* correlation story, which nearly every pair has
somewhere. `legality×{set,date,year}` is categorically different — not a correlation-with-exceptions
but the same variable observed twice, and exactly answerable EXACTLY (`card_legalities` is already
real per-printing ground truth) — kept out of the independence registry entirely and flagged as a
separate, likely-better opportunity (a Round 34-style exact per-(set,format) table).

**Registry, re-validated against real data (not copied from the doc), printing space, n=300 draws
unless noted**: `legality×cn` 0.246→0.041 (255/14 improved+regressed of 285 scored); `legality×usd/
eur/tix` 0.188→0.011 / 0.195→0.019 / 0.401→0.077 (tix weaker but still net positive); `type×released`
0.478→0.178 (was 0% coverage, now 41.3%); `type×usd` 0.479→0.189 (now 45.7%); `color/identity/cmc×
{usd,eur,tix}` already fully covered by Round 38, eur/tix confirmed to behave identically to usd.
**Two entries reverse the design doc's own "unsafe" classification**: `id×set` 1.151→0.106 (118/3 of
122) and `pow×set` 1.114→0.154 (72/1 of 73) — independently re-confirmed with a FRESH seed (999, no
reuse of the implementing agent's own sample) before trusting this surprising a reversal: `id×set`
median 1.341→0.140 (273/283 improved), `pow×set` 1.358→0.165 (146/147 improved), printing space,
n=283/147. Declined despite looking plausible: same-currency price crosses (`usd×eur` net WORSE in
printing space, 0.159→0.394, while `usd×tix`/`eur×tix` net better — mixed, inconsistent signal) and
`set×type` (similarly mixed across spaces). `color×identity` confirmed already 100%-covered by the
pre-existing `PlanePopcount` mechanism, unaffected either way — no live gap for independence to fill.

**Class-priority fix** (a real bug Round 38's own test found: `arith_tuple_count` and
`SubtypeArithBox` both hitting the same query at the same value, attributed to whichever ran first):
"pick smallest candidate" is only sound among EXACT/upper-bound mechanisms (every one of which
guarantees `count(A∧B) ≤ min(count(A), count(B))`) — independence and `SetCollectorRange`'s density
estimate (Round 33, already documented to undershoot on a non-contiguous set) are central estimates
that can land on either side of the truth, so naively comparing them by magnitude against an exact
value is unsound, not merely risky (an undershooting estimate could silently "win" over a correct
exact answer). Fixed with a strict class priority: exact/bound candidates are tie-broken by "most
leaves covered" among themselves; an estimate-class candidate is only ever considered when no
exact/bound candidate ties the global min at all. `SetCollectorRange` verified to have no live version
of this bug today (structural: `set`/`cn` never compile to planes or appear in `PairTotals`).

**A real regression caught by independent pre-merge verification, not the implementing agent's own
report**: a fresh isolated-wheel before/after sweep found `safe:cmc+usd` — a shape ALREADY fully
covered by Round 38 — got WORSE (median 0.158→0.219, 218 of 246 comparable rows regressed, up to 13x
on the worst example: `usd>6.03 cmc>=1 cmc<=1` card mode, true 343, before 482, after 4642). Root
cause: a pre-existing (pre-Round-38) generic `arith_tuple_count` check — "2+ cmc/power/toughness
children get their true joint card count" — now marked its leaves `covered` unconditionally on a hit.
Correct for a genuine cross-dimension join (`cmc<=5 power>=3`); wrong for 2+ bounds on the SAME field
(`cmc>=1 cmc<=1`, not a cross-dimension intersection at all, just resolving one field's own effective
value) — marking it covered silently stole those leaves from the registry's own same-field
consolidation before it ever ran, dropping Round 38's own price×cmc pairing entirely rather than
merely failing to add new coverage. Fixed by gating that `mark_covered` call on
`single_arith_field(...).is_none()` (only a genuine multi-field join covers its leaves). Re-verified
independently after the fix (not just the implementing agent's re-run): the exact repro now predicts
482/864 (card/printing), matching the pre-Round-40 baseline exactly; `safe:cmc+usd` is a byte-for-byte
no-op against the pre-Round-40 baseline over all 816 `true_total>=100` rows; `unsafe:legality+set`/
`+released`/`color×identity`/`safe:color+usd`/`safe:identity+usd` each confirmed byte-identical to the
pre-Round-40 baseline.

**Final verification** (fresh isolated wheels, independently rebuilt and re-swept, not the
implementing agent's own artifacts): 395 of 53,775 shared observations flip plan choice (down from
452 before the fix — the fix also removed some spurious flips the bug had caused), concentrated in
the newly-covered shapes, all toward `GatheredScan`; `root=leaf`/`root=or`: 0 changes.
`safe:type+released` 0.400→0.231 (237/3), `safe:type+usd` 0.421→0.211 (200/0), `unsafe:identity+set`
0.973→0.244 (276/50), `unsafe:pow+set` 0.918→0.209 (208/24), `safe:legality+usd` 0.280→0.140 (547/314),
`safe:legality+cn` 0.218→0.086 (552/266) — every newly-covered shape net-improves; the regressed tails
are bounded, known-shape independence undershoots (e.g. `f:pauper usd<0.08`), not another blow-up.

Blast radius: `card_engine/src/lib.rs` (`IndepClass`/`independence_safe_pair`/`indep_class_of`, the
generalized residual scan replacing Round 38's hard-coded pair, `is_estimate_class_mechanism`, the
`covered` bookkeeping threaded through every existing mechanism call site, `and_trace_build_tree`'s
class-priority winner selection), `card_engine/src/tests.rs` (four new tests plus updates to Round
38's own fixture for the corrected tie-break). `cargo test`: 199 passed release / 200 debug (one
pre-existing unrelated debug-only test). `cargo clippy --all-targets -- -D warnings`: clean.

### Round 1

The self-check (constraint 3 in the parent doc) cleared cleanly: every new number
(`printing_matches` = `est.result.printing`, `domain_cards`, `printings_per_card`) was already
computed before `scan_all` runs, so the fix is a handful of extra float ops, not a new scan --
confirmed by the correctness/latency gates below showing no execution-time effect.

The surprising part showed up in the paired-diff, not the self-check: for **~37% of the and2/and3
RANGE_FAMILIES sample**, `domain_cards` itself (the candidate-card estimate this fix multiplies by a
depth term) is already smaller than `domain_cards * printings_per_card` -- i.e. smaller than the
*maximum possible* span for that many candidate cards -- while the REAL measured
`printings_examined` exceeds even that ceiling. No depth formula operating on top of `domain_cards`
can fix those rows; the error is upstream, in `compose_printing_estimate`'s own candidate-card count
for an And of several different-index range leaves (price/cn/released_at each have their own index,
so an And across them doesn't hit the same-index fusion or the plane-based tightening this session's
prior work added -- it falls back to `calibrated_balls_into_bins` on the min-folded printing match
count). Restricted to the other ~63% (`domain_cards` not the bottleneck), the fix moves within-25%
from 7.5% to 36.7% and the median from 2.67 to 0.92 -- a real, large improvement, just capped by a
separate, uninvestigated `domain_cards` bug for multi-range-index Ands. That bug is the natural next
target, since it bounds how far ANY depth-side fix here can go.

The primary gate (`bench_cost_model_agreement.py`, single uncontrolled 300s run each) moved the
target cell only 16% -> 17%, which looks like noise rather than signal on its own -- confirmed as
noise by checking UNRELATED cells this change cannot touch (`CardRangePopcount` 59% -> 66%,
`GatheredScan`/`candidates` 12% -> 15%, `StreamedSelect`/`printing_compose` p90 1.72 -> 2.98) moving
by comparable or larger amounts between the same two runs, purely from the two 300-second windows
sampling a different number and mix of queries. The paired, same-query-set diff (1,500 shared queries,
identical rng seed against both builds) is the only one of the two that actually isolates this
change's effect, and it shows the real number.

### Round 2

Target: fix `domain_cards` itself for the ~37% subset Round 1 identified as ceiling-capped — an
And of 2+ DIFFERENT-INDEX printing-varying range leaves (`price_usd`/`price_eur`/`price_tix`/
`collector_number_int`/`released_at`, each its own separate index), where `exact_result_total`
returns `None` (no pair-table/arith-tuple/plane-compile coverage exists for this combination) and
`est_cards` falls back to `calibrated_balls_into_bins(printing_matches, n_cards)` on the min-folded
(loosest) printing match count.

**Self-check (constraint 3):** the only new per-query work in either variant tried is one extra
`PrintingValueIndex::range` partition-point probe per distinct range field the And names (`O(log
n)`, bounded by query length, never by match/printing/candidate count), reusing the exact same
lookup `bare_range_bounds`'s other callers already pay per leaf — paid only after `exact_cards` has
already declined. Every number traces to an existing precomputed structure (`PrintingValueIndex`,
`calibrated_balls_into_bins`/`COMPOSE_CARD_ESTIMATE_BIAS`), no new per-query scan. This cleared the
self-check; the idea failed on the empirical paired-diff instead, described below.

**Attempt 1 — printing-space independence product.** Combined each leaf's own exact printing-match
selectivity (`k_i / n_printings`) via `Π(k_i / n_printings) * n_printings`, excluding any
combination touching 2+ of the price triple (checked directly: `price_usd<5 price_eur<4`-shaped
queries are a real correlation risk, confirmed on a 500-query price-only paired diff below, though
moot once the whole approach failed). Wired in as a `.min()` against the existing min-fold, feeding
both `est_cards`'s fallback and (to keep the pair internally consistent) the Round-1 depth term's
`density` numerator.

Paired diff (1,500 shared and2/and3 RANGE_FAMILIES queries, unique=card, identical seed against
baseline `costcell/trunk`@`f3f4a017`): **38 improved / 496 regressed / 966 tied, total abs
`scan_units` error 17.3M → 18.1M (worse), within-25% 11.8% → 8.3% (worse)**. Price-triple-only
subset (500 queries, `usd`/`eur`/`tix` and2/and3): 0 improved / 0 regressed — the correlation guard
worked exactly as designed (no combination in that subset reached the independence branch), but
this is moot given the whole-population result.

**Why it failed, and why no independence variant can work here:** a probability-product
combination is mathematically bounded above by its smallest single factor whenever every factor is
`<= 1` (`Π p_i <= min(p_i)`) — so ANY such combination can only ever SHRINK an estimate relative to
the tightest single-field bound, never raise it. Checking the baseline's own error DIRECTION on the
1,500-query sample confirms this is the wrong direction for the dominant failure mode here: 866/1500
rows (58%) already UNDER-estimate `scan_units`, only 450/1500 (30%) over-estimate, and Round 1's own
investigation established that `domain_cards` for this population is a FLOOR that undershoots the
true candidate span, not a ceiling that overshoots it (`printings_examined` exceeding even `domain_cards
* printings_per_card`). A transform that can only shrink an already-too-small number cannot fix it;
it just pushes the under-estimating majority further from the truth while incidentally helping the
smaller over-estimating minority, netting negative overall — exactly the 38/496 split measured.

**Attempt 2 — card-space independence product**, tried as "the closest variant" once attempt 1's
mechanism was understood to be structurally wrong-directioned: instead of combining printing-space
selectivities, combine each leaf's own CARD-space estimate (`calibrated_balls_into_bins(k_i,
n_cards)`), on the theory that "this card has SOME printing satisfying leaf i" is a weaker,
superset condition of the filter's real "one printing satisfies every leaf" semantics, so the
combination should be able to raise the estimate instead of shrinking it. Wired in as `.max()`
against today's fallback. Empirically: **0/1500 changed** — the `.max()` branch never won even
once. This confirms the same math from the opposite direction: each leaf's own card-space estimate
is itself `<= n_cards`, so the product-of-fractions form is bounded by the smallest such factor
regardless of which space it is computed in — moving to card space changes what each factor MEANS,
not the shape of the ceiling the combination is stuck under.

**Conclusion:** rejected at self-check-plus-paired-diff. Both variants respected the
pre-computation constraint (no new per-query scan class) but neither can fix a floor-too-low bug by
construction — this rules out the whole "independence product" family for this specific target, not
just a tuning miss. No code committed; both attempts were reverted (`git checkout --
card_engine/src/lib.rs` against `costcell/trunk`, confirmed clean via `git diff --stat`).

**Next steps for a future round:** the diagnosed bug (domain_cards undershoots the true candidate
span for this shape) needs a mechanism that can RAISE the estimate, which independence-style
combination cannot do. Two candidates worth checking before another attempt: (a) a flat,
shape-specific multiplicative correction on top of `calibrated_balls_into_bins`'s output — same
precedent as `COMPOSE_CANDIDATE_SPAN_BIAS`/`COMPOSE_CARD_ESTIMATE_BIAS` — but fit with a proper
calibration/held-out split (not the same 1,500-query sample used to diagnose the bug, per this
repo's benchmark-methodology rule); (b) investigating whether the undercount's true source is
within-card printing clustering (reprints of the SAME card may jointly satisfy multiple range
conditions at a materially higher rate than corpus-wide field marginals imply, since a card's own
printings are not independent draws — the same clustering `COMPOSE_CARD_ESTIMATE_BIAS`'s 1.78
divisor already corrects for at the SINGLE-field level), which would need a different precomputed
source than the ones checked here, not just a different combination formula.

### Round 3

Target: Round 2's own "next steps" item (b) — a flat, shape-specific multiplicative correction on
top of `calibrated_balls_into_bins`'s output, fit with a genuine calibration/held-out split, for the
same And-of-cross-index-range-leaves population. A second constant, `COMPOSE_RANGE_AND_CLUSTER_BIAS`,
routes `est_cards`'s fallback (`acquire_plan_features`, the `PrintingCompose` arm) to
`calibrated_balls_into_bins_with_bias(printing_matches, n_cards, COMPOSE_RANGE_AND_CLUSTER_BIAS)`
instead of `COMPOSE_CARD_ESTIMATE_BIAS`'s 1.78, whenever the new `is_cross_index_range_and` detects
an `And` with 2+ children whose `bare_range_bounds` indexes are pairwise distinct (same-index
children, e.g. a two-sided `usd>=a usd<=b`, still fuse to one index and don't count — that population
already gets an exact `k` from `fuse_and_range_children` upstream and never reaches this fallback).

**Self-check (constraint 3):** trivially clears. `is_cross_index_range_and` is O(children) —
`bare_range_bounds` per child is a pure match + float comparison, no index probe, bounded by query
length never match count — and only runs inside the `unwrap_or_else` closure, i.e. only after
`exact_cards` has already declined. No new per-query scan class; confirmed by `cargo test` and the
same-build latency canary below showing nothing distinguishable from noise.

**A structural surprise mid-investigation, not in the fix itself:** the first Python re-derivation of
`scan_units` (needed to sweep the bias without a Rust rebuild, same trick Round 1 used for
`COMPOSE_CANDIDATE_SPAN_BIAS`) matched the live build's own `scan_units` on only 1,138/1,500 rows.
The other 362 are `range_too_broad_to_narrow` — a LATER, separate guard in `acquire_plan_features`
(after `domain_cards`/`scan_units` are computed) that resets both to the full corpus whenever the
And's min-folded `printing_matches` alone exceeds `MAX_NARROW_FRACTION` (0.25) of `n_printings`,
**independently of any bias**. This is not a bug this round touches — it is a separate, deliberate
"narrowing degrades to a full scan" model, verified elsewhere — but it means a clustering-bias
constant here can only ever move the NARROW subset (562/742 of the held-out half): the broad subset's
`scan_units` is bias-invariant by construction. Modeling the guard in the Python re-derivation brought
the self-check to 1,500/1,500 exact matches before any sweep was trusted, and the real Rust build's
paired diff (below) matches the Python-simulated numbers exactly, confirming the model was right.

**Fit.** Captured, per sampled query, each leaf's own exact printing-match count (`k_i`, via an
isolated `unique=printing` sub-query per predicate — exact, no estimate) and the real
`printings_examined` GatheredScan counter, over 1,500 and2/and3 RANGE_FAMILIES queries (`unique=card`,
same population and precedent size as Rounds 1-2). Split by `hash(query) % 2` — 758 calibration / 742
held-out. Swept the bias 0.20–1.78 in steps of 0.02 on the calibration half only; the error-vs-bias
curve is smooth and convex on BOTH halves with minima 0.04 apart (1.14 calibration, ~1.06 held-out),
which is what a genuine signal looks like rather than noise fit to one split. Picked `1.1`, inside
both minima, rather than either half's precise argmin.

```
                          calibration (n=758)              held-out (n=742)
scan_units total abs      8.70M (1.78) -> 8.31M (1.1)       8.60M (1.78) -> 8.02M (1.1)
improved / regressed                 417 / 148                      433 / 117
narrow-only subset (n)                    576                            562
narrow-only total abs     3.15M (1.78) -> 2.76M (1.1)       3.19M (1.78) -> 2.61M (1.1)
price-triple subset (n)                   382                            363
price-triple total abs    3.87M (1.78) -> 3.74M (1.1)       3.73M (1.78) -> 3.47M (1.1)
```

Direction matches the assignment's hypothesis: smaller than 1.78 (less division of `k`, so a HIGHER
effective ball count and a higher resulting estimate), correcting Round 2's diagnosed floor-undercount
rather than repeating `COMPOSE_CARD_ESTIMATE_BIAS`'s saturating-overcount correction.

**Price-triple check.** The held-out price-triple subset (`usd`/`eur`/`tix`, 2+ of them) improves
proportionally in line with the whole population (213/68/82) — no sign of the correlation risk Round 2
flagged, because this is a flat multiplicative rescaling of `calibrated_balls_into_bins`'s existing
math, not a combination formula across per-leaf estimates; there is no per-leaf independence
assumption for near-identical fields to violate.

**Verified against the real build, not just simulation:** rebuilt the modified engine and re-ran the
same 1,500-query, same-seed sample through it directly (not the Python re-derivation) — the real
paired diff (baseline `costcell/trunk`@`ef78a984` vs modified) landed on the exact same numbers as the
simulation (758/742 split, 8.70M→8.31M and 8.60M→8.02M), confirming the Python model used to pick the
constant was not itself a source of error.

**Why the single-run agreement gate doesn't move.** `bench_cost_model_agreement.py`'s `GatheredScan`/
`card` cell stayed at 15% within [0.8, 1.25] on both builds (35,918 vs 35,946 rows) — expected, not a
sign the fix is inert: this cell pools every card-mode `PrintingCompose` acquire, and the affected
shape (And of 2+ different-index range leaves, narrow enough to escape `range_too_broad_to_narrow`) is
a small slice of it. The held-out paired-diff above is the controlled measurement; this cell is the
same noisy sanity check Round 1 already established is uninformative at this grain.

### Round 3 confirmation runs

- `bench_regret_matrix.py --seconds 120 --mode uniform`: same shape as Round 1's — regret still 96%
  `printing_compose` share, `StreamedSelect -> GatheredScan` / `GatheredScan -> PrintingCompose` still
  the largest picked/best mismatches, nothing resembling the 23.6x acquire-time precedent.
- `bench_query_latency_ab.py --mode realistic --sample 800 --seed 1`, baseline vs modified, interleaved
  A1/B1/A2: real diff `B - A = -0.3µs, 95% CI [-0.6, -0.1]`, "B is FASTER". Same-build canary (A1 vs
  A2, zero code difference): `-0.6µs, CI [-0.9, -0.4]`, also "B is FASTER" — a swing of comparable (here
  larger) magnitude with nothing changed, matching Round 1's own non-interleaved-run drift finding. Read
  as no detectable latency effect either way, not as a confirmed speedup.

### Round 4

Target: the "broad" ~24-25% of the same `is_cross_index_range_and` population Round 3 flagged out of
scope -- the subset where `range_too_broad_to_narrow` (a LATER, independent guard, found mid-
investigation by Round 3) resets `eval_domain`/`scan_units` to the full corpus regardless of any
bias, because the And's min-folded `printing_matches` alone is too broad a fraction of `n_printings`
to trust `domain_cards`. Round 3's own report called the ~69% "real usage" figure for this subset a
mid-investigation finding, not validated -- this round re-derives it from scratch on a fresh sample
before building anything on it.

**Re-derivation.** Sampled 1,500 and2/and3 RANGE_FAMILIES queries (`unique=card`, fresh seed) via
`query()` with `Shape(families=RANGE_FAMILIES, predicates=2 or 3)` -- family draws are distinct-
without-replacement and each of the 5 `RANGE_FAMILIES` maps 1:1 to its own printing-value index, so
every sampled query is `is_cross_index_range_and` by construction, same reasoning Round 3 used.
Detected the guard firing by its signature (`eval_domain == n_cards` and `scan_units == n_printings`,
which for this printing-varying population -- never card-invariant, never a bare collection leaf,
and Round 2 already proved `est.result.card` is never `Some` here -- means the guard fired with none
of its four exemptions applying): **372/1,500 (24.8%)**, matching the ~24-25% cited going in.

Read the real GatheredScan counters via `explain_analyze` (`num_warmups=0, num_trials=1`; counters
are round-invariant -- checked directly by rerunning 20 queries at `(0, 1)` against `(2, 5)` with
identical `cards_visited`/`printings_examined` both times). Result, on the 372 guard-fired rows:

    real cards_visited      / n_cards       mean 1.000   median 1.000   (0 rows below 1.0)
    real printings_examined / n_printings   mean 0.697   median 0.713

`eval_domain` (`n_cards`) is EXACT for every one of the 372 rows, not just close -- real card-space
narrowing gives up at the same `range_too_broad_to_narrow` threshold this guard checks (the function
is shared with the real narrowing path, not just this pricing site), so a GatheredScan the router
actually runs after this fires really does visit every card. `scan_units` is the opposite: the guard's
`n_printings` ceiling is real (never measured over 1.0) but loose, at a stable ~0.70 of it -- this
re-derives the ~69% figure cleanly, and settles the "did the guard also give up on the printing side"
question the eval_domain number could not answer.

**Fix.** Left `eval_domain` untouched (scaling an already-exact number down would reintroduce the
under-charge this guard exists to prevent -- the exact failure mode the four existing exemptions were
each added to fix, so this round does not risk it even via a downstream scale). Added
`COMPOSE_RANGE_AND_BROAD_SCAN_SCALE` (0.7), applied to `scan_units` alone, gated on
`is_cross_index_range_and(composed, indexes)` -- reused unchanged from Round 3, not reimplemented.
This is a scale on the ALREADY-DECIDED reset, not a 5th exemption: the guard's unconditional reset
still fires exactly as before; only what `scan_units` (never `eval_domain`) resets TO changes, and
only for this one shape.

**Fit.** Split the 372 guard-fired rows by `hash(query) % 2`: 191 calibration / 181 held-out. Swept
0.60-0.84 in steps of 0.01 on the calibration half only; both halves' error-vs-scale curves are
smooth, convex, and minimize at the SAME 0.71 (closer agreement than Round 3's two minima 0.04 apart).
Picked 0.7, inside the flat bottom of both and matching the sample's own mean/median realized fraction
almost exactly.

```
                          calibration (n=191)              held-out (n=181)
scan_units total abs      5.64M (1.0) -> 1.92M (0.7)        5.40M (1.0) -> 1.90M (0.7)
improved / regressed                172 / 19                        166 / 15
price-triple subset (n)                  79                              71
price-triple total abs   1.91M (1.0) -> 0.79M (0.7)        1.82M (1.0) -> 0.75M (0.7)
```

**Price-triple check.** The held-out price-triple subset (`usd`/`eur`/`tix`, 2+ of them) improves
proportionally in line with the whole population (62 improved / 9 regressed) -- same reasoning as
Round 3's own price-triple check: a flat scale on an already-computed ceiling has no per-leaf
independence assumption for the near-identical price columns to violate.

**Verified against the real build, not just simulation.** Rebuilt the modified engine and re-ran the
identical 1,500-query, same-seed sample through it directly -- `eval_domain` matched `n_cards` on all
372 guard-fired rows (0 mismatches) and `scan_units` matched `round(0.7 * n_printings)` exactly (0
mismatches), and the real paired diff landed on the exact same numbers as the Python-side
re-derivation (5.64M/5.40M -> 1.92M/1.90M, 172/19 and 166/15).

**Why the single-run agreement gate doesn't move.** `bench_cost_model_agreement.py`'s `GatheredScan`/
`card` cell stayed at 16% within [0.8, 1.25] on both builds (33,966 vs 33,806 rows) -- expected: this
cell pools every card-mode `PrintingCompose` acquire, and the guard-fired subset of
`is_cross_index_range_and` is a small slice of it, same reasoning as Round 3.

### Round 4 confirmation runs

- `cargo test --manifest-path card_engine/Cargo.toml`: 167 passed, 0 failed, 56 ignored.
- `cargo clippy --manifest-path card_engine/Cargo.toml --all-targets -- -D warnings`: clean.
- `bench_regret_matrix.py --seconds 120 --mode uniform`: same shape as Rounds 1 and 3 -- regret still
  96% `printing_compose` share, `StreamedSelect -> GatheredScan` / `GatheredScan -> PrintingCompose`
  still the largest picked/best mismatches, nothing resembling the 23.6x acquire-time precedent.
- `bench_query_latency_ab.py --mode realistic --sample 800 --seed 1`, baseline vs modified, interleaved
  A1/B1/A2: real diff `B - A = +0.4µs, 95% CI [+0.3, +0.6]`, "B is SLOWER". Same-build canary (A1 vs
  A2, zero code difference): `-0.4µs, CI [-0.5, -0.2]`, "B is FASTER" -- a swing of comparable
  magnitude with nothing changed, matching Rounds 1 and 3's own non-interleaved-run drift finding. Read
  as no detectable latency effect either way, not as a confirmed regression.

### Round 5

Diagnostic only, no code changes -- re-run the magnitude-weighted AST-shape breakdown from scratch
against the current `costcell/trunk` tip (`8ab0b4cc`), since a full-corpus checkpoint
(`bench_cost_model_agreement.py`) still shows `GatheredScan`/`card` at 15% within 25%, essentially
unchanged from Round 0's 16%, despite three landed, held-out-validated fixes (Rounds 1, 3, 4).

**Method.** Isolated release wheel (`maturin build --release`, extracted, `PYTHONPATH`-pinned).
Sampled with `QuerySampler(corpus, "uniform")`, reimplementing `query()`'s body inline (predicate
count → `_draw_families` → `predicate` per family) so each row keeps which FAMILIES were drawn --
`query()` itself doesn't return them, and every other part of the sampling loop (limits, offsets,
warmups/trials, `unique`/`orderby`/`direction` drawn independently) matches
`bench_cost_model_agreement.py` exactly. Every family maps to one of six categories: `range`
(`usd`/`eur`/`tix`/`cn`/`released` -- the printing-varying, range-indexed fields this whole doc is
about), `numeric_other` (`pow`/`tou`/`cmc`/`loyalty`), `rarity`, `text` (`name`/`oracle`/`flavor`/
`artist`), `arith` (the extended syntax), and `collection` (everything else -- type/legality/
identity/color/set/keyword/produces/tag/border/frame/watermark/devotion). `sampler.query()` only
ever emits a flat conjunction (no Or/Not/regex), so the whole GatheredScan/card population this cell
measures is single leaves and `and2`/`and3` -- there is no Or-composed or Not-wrapped subpopulation
to bucket here; that's a property of what `bench_cost_model_agreement.py` samples, not something
this round chose.

Per row: `predicted = acquire["scan_units"]`, `measured = plan["printings_examined"]` (`GatheredScan`
only, non-declined) -- the same pairing Rounds 3/4 used, per `scan_units`'s own doc comment at
`lib.rs:11213` ("the real `printings_examined` GatheredScan counter"). Bucket key = `structure`
(`single`/`and2`/`and3`) + sorted category tuple. Ranked by total absolute `scan_units` error per
bucket (magnitude-weighted), with each bucket's row count and median ratio reported alongside so a
high-count-but-tied bucket and a rare-but-catastrophic one are both visible.

300s budget (same protocol/seconds as Round 0's baseline run) → **30,892 GatheredScan/card rows**,
same order of magnitude as Round 0's 35,074 and Rounds 1-4's 1,500-query calibration samples for
their narrower held-out slices. Total pooled absolute `scan_units` error: 175,122,864. (Note: this
is a `scan_units`-space ratio, same quantity Rounds 1-4 worked in, not the ns-space
measured/predicted ratio `bench_cost_model_agreement.py`'s headline 15%/16% number reports --
the two measure different things and are not expected to match numerically.)

**Ranked bucket table** (all buckets with n ≥ 1; buckets below 0.1% share are real but tiny):

```
bucket                                 n       sum |err|   share  median ratio  within25%
single:range                        3041      93,991,483   53.7%          0.64        1%
and2:collection+range                2301      17,699,351   10.1%          1.21       15%
and2:numeric_other+range              801      14,662,649    8.4%          0.74       25%
single:collection                    6993      11,248,207    6.4%          1.00       68%
single:rarity                         614       8,168,926    4.7%          1.08       41%
and2:range+rarity                     199       5,631,260    3.2%          1.50        5%
and2:numeric_other+rarity             169       3,714,009    2.1%          0.41       21%
and2:collection+numeric_other        1628       2,553,659    1.5%          1.00       42%
and2:range+range                      398       2,442,018    1.4%          1.15       35%
single:numeric_other                 2355       2,405,375    1.4%          1.00       95%
and2:collection+rarity                466       2,234,802    1.3%          0.54       14%
and2:arith+range                      215       1,708,742    1.0%          0.58       15%
and2:collection+collection           2442       1,497,390    0.9%          0.20       20%
and3:collection+numeric_other+range   355       1,303,555    0.7%          0.44       12%
and3:collection+range+range           232         855,513    0.5%          0.80       21%
and3:numeric_other+range+rarity        30         725,010    0.4%          0.31       17%
and3:collection+collection+range      522         526,766    0.3%          0.18       10%
and2:range+text                       814         449,305    0.3%          0.86       20%
and3:numeric_other+range+range         62         434,311    0.2%          0.44       15%
and3:numeric_other+numeric_other+range 39         398,600    0.2%          0.27       10%
and3:collection+range+rarity           99         261,534    0.1%          0.77       12%
and2:arith+rarity                      35         247,425    0.1%          0.43       29%
single:text                          2454         239,195    0.1%          1.00       46%
and2:numeric_other+numeric_other      144         168,614    0.1%          1.00       82%
and3:range+range+range                 24         154,252    0.1%          1.02       17%
[remaining 38 buckets each < 0.1% share, ~0.4% combined, mostly text/arith-involving rows with n<100]
```

**Confirmation: Rounds 1-4's target shape did drop, as expected.** The `is_cross_index_range_and`-
equivalent population (an `and2`/`and3` with 2+ `range`-category families -- exactly what
`is_cross_index_range_and` requires) is **807 rows (2.6% of the sample), 4,069,972 abs error (2.3%
of the pooled total), median ratio 1.00, 28% within 25%**. Before Rounds 3/4 this shape was
"tens of millions of units each" and the single dominant contributor by every account in this doc;
now it sits at a median ratio of exactly 1.00 (as good as any bucket in the table) and would not
make a top-10 list by magnitude. The three shipped fixes worked exactly as designed on their target
population -- they just never had a chance to move the pooled cell, because that population turns
out to be a small slice of it (2.6% by row count, 2.3% by error), not the ~37%+ this doc's earlier
rounds estimated from the narrower and2/and3-RANGE_FAMILIES-only calibration sample. That estimate
was never wrong on its own terms (it was scoped to the RANGE_FAMILIES-only shape from the start);
it just wasn't representative of the whole `GatheredScan`/card population once measured against it
directly.

**What actually dominates: the SAME broad-guard reset, everywhere Round 4 didn't scale it.** Flagging
every row where `predicted == n_printings` exactly (the `range_too_broad_to_narrow` guard's
telltale signature -- both `PrintingCompose`'s and the sibling `CardRangePopcount`/
`PrintingRangeScan` arms' resets set `scan_units` to the literal, unscaled `n_printings` when they
fire) finds **2,412 rows (7.8% of the sample) carrying 131,170,530 abs error -- 74.9% of the ENTIRE
pooled total -- at median ratio 0.45** (predicted ~2.2x too high). Zero of these 2,412 rows are
`is_cross_index_range_and` -- Round 4's scale never had a chance to touch any of them, by
construction. Split by structure: `single` (n=1,851, 91.6M), `and2` (n=534, 37.3M), `and3` (n=27,
2.2M).

Reading `lib.rs` confirms this is not a new bug -- it is Round 3/4's own noted, deliberate scope
limit, finally showing up as the dominant term now that the target shape it excluded is fixed. Two
separate sites:

1. **`PrintingCompose`'s own broad-guard reset** (`lib.rs:12239-12259`) scales `scan_units` by
   `COMPOSE_RANGE_AND_BROAD_SCAN_SCALE` *only* `if is_cross_index_range_and(composed, indexes)`; the
   inline comment at `:12248-12250` says outright: "every other query reaching this branch (a single
   broad range, a broadcast legality, ...) never had this scale's calibration sample in it, so it
   keeps today's unscaled `n_printings` ceiling." That "everything else" population is exactly what
   this round measured. Per-bucket broad/narrow split confirms the broad slice is a small-count,
   huge-magnitude minority within each mixed bucket:
   ```
   and2:collection+range      broad n=210  (9% of bucket rows, 78% of bucket's error) median ratio 0.30
                               narrow n=2091 (91% of rows, 22% of error)               median ratio 1.39
   and2:numeric_other+range   broad n=188  (23% of rows, 94% of error)                 median ratio 0.21
                               narrow n=613  (77% of rows, 6% of error)                 median ratio 0.93
   and2:range+rarity          broad n=81   (41% of rows, 91% of error)                 median ratio 0.36
                               narrow n=118  (59% of rows, 9% of error)                 median ratio 3.37
   ```
   Example rows (all `predicted == n_printings == 97,812`, the full corpus):
   `tou<=5 tix>=0.02 tix<=0.04` → measured 26,834 (ratio 0.27); `tou>=2 tou<=4 year<2025` → measured
   12,491 (ratio 0.13); `r>=uncommon tix>0.02` → measured 46,256 (ratio 0.47); `r>=uncommon eur<0.49`
   → measured 49,726 (ratio 0.51).

2. **The sibling `CardRangePopcount` (`lib.rs:11801-11845`) and `PrintingRangeScan`
   (`lib.rs:11846-11872`) acquire arms** -- which serve a single BARE range leaf under `unique=card`
   (e.g. `usd>=0.24` alone, no `And` at all) -- have their own structurally identical
   `range_too_broad_to_narrow`-gated reset to `(n_cards, n_printings)`. This is a completely separate
   code path from `PrintingCompose` (confirmed live: `usd>=0.24` alone acquires via
   `count_source: card_range_popcount`, not `printing_compose`), never in scope for any of Rounds
   1-4 (whose investigation was explicitly `compose_printing_estimate`/`PrintingCompose`). This is
   the `single:range` bucket -- the single largest bucket in the whole table, 53.7% of pooled error
   on its own, median ratio 0.64. Example rows (all `predicted == 97,812`): `usd>=0.24` → measured
   40,782 (ratio 0.42); `cn>=127` → measured 45,904 (ratio 0.47); `tix<0.12` → measured 53,260
   (ratio 0.54); `year>=2023` → measured 61,411 (ratio 0.63).

**Secondary, smaller finding, opposite direction.** `and2:range+rarity`'s NARROW (non-broad) subset
reads median ratio 3.37 -- badly UNDER-costed, the opposite direction from everything else in this
round. Small in absolute terms (118 rows, ~9% of that bucket's 5.6M error, so well under 1M total)
-- not worth its own round yet, but worth a one-line flag for whoever next touches range+rarity
combinations, since it's a direction-flip rather than more of the same over-cost pattern.

**What Round 6 should target.** The `range_too_broad_to_narrow` broad-guard reset, generalized
beyond `is_cross_index_range_and`, at two sites:

- Extend (or add a sibling to) `COMPOSE_RANGE_AND_BROAD_SCAN_SCALE` inside `PrintingCompose`'s own
  reset so it also scales `scan_units` when the guard fires but `is_cross_index_range_and` is false
  (a lone broad range leaf mixed with a collection/numeric_other/rarity leaf, or a bare broadcast
  legality/range predicate). This is exactly the pre-computation-safe pattern Round 4 already used --
  a flat multiplicative scale on an already-computed ceiling, no new per-query scan -- just widened
  in scope. Needs its OWN calibration/held-out split before trusting a number: this round's median
  ratio here (0.45) reads meaningfully lower than Round 4's fitted realized fraction (~0.70-0.71) for
  the `is_cross_index_range_and` population, so reusing 0.7 unchanged is not obviously right --
  and the two mixed-leaf buckets above disagree with each other too (0.21-0.36 median), so a single
  universal constant may not fit either; check whether the guard's realized fraction varies
  systematically with the NON-range leaf's own selectivity before picking one or several constants.
- Add the analogous scale to the `CardRangePopcount`/`PrintingRangeScan` arms' own broad-guard reset
  (`lib.rs:11831`, `:11862`) for a single bare range leaf -- a different acquire branch than
  `PrintingCompose`, so it needs its own gate check and likely its own constant (median ratio here,
  0.64, differs again from both of the above), even though the underlying guard function
  (`range_too_broad_to_narrow`) is the same shared code. This is `single:range`, the single largest
  bucket by magnitude in the whole table -- the highest-leverage place to start.
- Population parity note for whoever fits this: unlike Rounds 3/4's RANGE_FAMILIES-only calibration
  sample, this population spans every family category (collection/numeric_other/rarity/text mixed
  with a range leaf, plus bare single range leaves with no other predicate at all) -- a proper
  calibration/held-out split here should draw from the SAME uniform-mode, all-category sampling this
  round used, not a re-use of the narrower RANGE_FAMILIES-only sample Rounds 1-4 built their splits
  from, since that sample structurally cannot contain the `single:range` or mixed-category rows that
  now turn out to matter most.

### Round 6

Target: Round 5's own top recommendation -- the `CardRangePopcount` arm's own
`range_too_broad_to_narrow` broad-guard reset (`lib.rs:11831` as of Round 5's tip), the single
largest bucket in the whole pooled `GatheredScan`/`card` error table (`single:range`, 53.7% of pooled
error, n=3,041 in Round 5's sample, median ratio 0.64). A bare single range leaf under `unique=card`
(e.g. `usd>=0.24` alone) -- confirmed live via `count_source: card_range_popcount`, a completely
separate acquire branch from `PrintingCompose`'s `is_cross_index_range_and` guard Rounds 3/4 fixed.

**Population re-derivation.** Sampled with `Shape(families=RANGE_FAMILIES, predicates=1,
unique={"card"})` (same shape `bench_card_range_estimate.py` already uses for this exact acquire
branch), filtered to `count_source == "card_range_popcount"`, varying `limit`/`offset`/`orderby`/
`direction` per query (matching `bench_cost_model_agreement.py`'s own protocol rather than pinning
them, so the population is not an artifact of one page shape). Of all `card_range_popcount` rows,
**52-54% have the guard fire** (two independent 3,500-row samples: 54.1% and 52.1%) -- the "broad"
population this round targets. Real `GatheredScan` counters (GatheredScan is always tried as a
forced trial in `explain_analyze` regardless of which plan the router actually picks, same trick
Rounds 3/4 used) over 3,500 guard-fired rows:

```
eval_domain realized fraction (cards_visited / n_cards):     mean 0.975  median 1.000  min 0.233
scan_units realized fraction (printings_examined / n_printings): mean 0.447  median 0.434  min 0.159  max 0.798
```

Per-field `scan_units` median: `cn` 0.41, `usd` 0.43, `eur` 0.43, `released` 0.42, `tix` 0.48 --
stable within a ~20% relative band across all five `RANGE_FAMILIES`, not one field dominating or
diverging.

**Self-check (pre-computation constraint).** The only change is a multiply-and-round on two numbers
already computed before this branch's `unwrap_or_else`-equivalent `if`/`else` runs (`n_printings` is
a corpus-wide constant read from `ctx`, `k`/`idx.len()` already drive the `range_too_broad_to_narrow`
call the branch makes regardless). No new per-query scan, no new index probe -- confirmed by
`cargo test` and the latency A/B below showing nothing distinguishable from noise once run-order
confounds are controlled for (see below).

**A structural surprise, not in the target arm itself: the sibling `else` branch is also
miscalibrated, for a different reason.** The arm's own comment claims "the sibling `PrintingRangeScan`
branch below assumes the opposite (always unnarrowed) and its cells agree to within 1% -- this makes
both exact," which reads as a claim that the NARROW-subset `(card_est, card_est)` branch is exact.
Measured directly, on the guard-NOT-fired rows from the same sample: `card_est / cards_visited`
(eval_domain check) is indeed exact at the median (1.00), but `card_est / printings_examined` (scan_units
check) reads median 0.25-0.37 depending on field -- `card_est` (a DISTINCT-CARD estimate) badly
undershoots `printings_examined` (a printing count) whenever a card has multiple reprints inside the
narrowed range, which is common for `cn`/`released`/`usd`. This is the assignment's own "verify what
that comment refers to" check: it refers to the two branches' feature vectors being internally
CONSISTENT with each other (not to either being numerically accurate), and the `else` branch's
`scan_units` side is a real, separate miscalibration -- **not fixed this round** (out of the assigned
blast radius; scoped as a follow-up in the ledger table above, not silently folded into this
constant).

**Why `eval_domain` is left untouched despite not being perfectly exact here (unlike Round 4's
population).** 96.6% of guard-fired rows read exactly 1.0; the remaining 3.4% are concentrated
entirely in `usd`/`eur`/`tix` queries at extreme thresholds (`eur>=1.05`, `tix>0.04`, ...), where most
cards have no printing with that currency at all, so the real materializing scan still narrows out
the null-complement even though the guard correctly judged the VALUE range too broad to narrow on.
Scaling the dominant 96.6%-exact regime down to chase a rare, structurally different tail would
reintroduce the under-charge the guard exists to prevent -- same call Round 4 made, but this time
independently verified rather than assumed to transfer, per the assignment's instruction.

**Fit.** Split 3,500 guard-fired rows by `hash(query) % 2`: 1,765 calibration / 1,735 held-out. Swept
0.30-0.60 in steps of 0.01 on the calibration half only; both halves' error-vs-scale curves are
smooth and convex, minimizing one step apart (0.43 calibration, 0.44 held-out).

```
                          calibration (n=1,765)            held-out (n=1,735)
scan_units total abs      96.0M (1.0) -> 15.4M (0.43)       93.3M (1.0) -> 16.0M (0.43)
improved / regressed              1,742 / 23                        1,704 / 31
```

Picked 0.43, inside the flat bottom of both curves.

**Per-field constant considered and rejected as not worth it.** A per-field scale (each field's own
median as an oracle upper bound) reaches 30.3M total abs error against the flat scale's 31.4M -- only
~3.5% further reduction, except for `tix` (275 rows, smallest subgroup) where the per-field oracle
does meaningfully better (0.80M vs 1.71M). Given the modest aggregate gain and this round's mandate to
prefer a flat constant unless the fit clearly does not hold, one flat `COMPOSE_BARE_RANGE_BROAD_SCALE`
was kept; a future round revisiting `tix` specifically could reconsider.

**Price-triple sanity (per-field, not cross-field correlation -- that check does not apply to a bare
single leaf).** `usd` (0.43), `eur` (0.43), `tix` (0.48) all sit close to the chosen 0.43; no
individual price field is a pricing outlier.

**Verified against the real build, not just the Python-side sweep.** Rebuilt with the constant and
re-ran the identical 3,500-query sample directly against it: `scan_units` matched
`round(0.43 * n_printings)` exactly on all 3,500 rows (0 mismatches), and the real paired diff landed
on the exact same total (31,400,955 combined) as the simulation.

**Routing-decision check (why this round is different from Rounds 3/4's structural risk).** Lowering
a feature this branch's shared `PlanFeatures` also prices COMPETING plans (`GatheredScan`/
`StreamedSelect`) against could in principle flip the router away from `CardRangePopcount` toward a
now-artificially-cheap competitor. Checked directly: the router picked `CardRangePopcount` on
500/500 sampled bare-range queries under BOTH the baseline and modified build (same kw), and
`bench_regret_matrix.py`'s `acquire` table shows `card_range_popcount` at 0.00 mean / 0% miss in both
builds (n=659 baseline, n=658 modified) -- no misrouting introduced.

**Why the single-run agreement gate barely moves, and why that's not evidence against the fix.**
`bench_cost_model_agreement.py`'s `GatheredScan`/`card` cell read 17% within [0.8, 1.25] on both
builds (n=33,019 baseline, n=33,251 modified) -- same story as Rounds 3/4: `card_range_popcount` is
only ~1,563-1,572 of that ~33,000-row pooled cell (~4.7%), so even fixing its single largest error
bucket cannot move a pooled median by much. The finer `GatheredScan`/`card_range_popcount` sub-row
(grouped by acquire branch, not pooled across all of `unique=card`) DID move: median 0.94 -> 1.05,
within-25% 47% -> 52% (n=1,563 / 1,572, single uncontrolled runs -- read as corroborating, not proof,
same noise caveat as every other single-run number in this doc).

### Round 6 confirmation runs

- `cargo test --manifest-path card_engine/Cargo.toml`: 167 passed, 0 failed, 56 ignored.
- `cargo clippy --manifest-path card_engine/Cargo.toml --all-targets -- -D warnings`: clean.
- `bench_regret_matrix.py --seconds 120 --mode uniform`: same shape as every prior round -- regret
  still 96% `printing_compose` share, `StreamedSelect -> GatheredScan` / `GatheredScan ->
  PrintingCompose` still the largest picked/best mismatches, `card_range_popcount`'s own regret
  unchanged (0.00 mean / 0% miss, both builds) -- nothing resembling the 23.6x acquire-time precedent.
- `bench_query_latency_ab.py --mode realistic --sample 800 --seed 1`: the FIRST paired run (baseline
  measured first, modified second) read `+4.1µs, 95% CI [+3.6, +4.5]`, "B is SLOWER" -- a magnitude
  that, unlike every prior round's canary-comparable noise, looked like a real signal at first glance.
  Investigated directly rather than accepted: (1) the specific queries showing the largest slowdowns
  were `t:legendary`, `c:g`, `set:usg` and similar -- filters that never reach `CardRangePopcount` at
  all, ruling out a routing-side effect from this change; (2) re-running with the build ORDER swapped
  (modified first, baseline second) produced `-0.1µs`, "NO DETECTABLE DIFFERENCE"; (3) two further
  same-build canaries (baseline-vs-baseline, modified-vs-modified, each a fresh pair) read `+0.8µs`
  ("B is SLOWER") and `-0.4µs` ("B is FASTER") respectively -- swings of comparable or larger magnitude
  than two of the three real A-vs-B diffs measured, with nothing changed. Read as run-order-dependent
  machine drift (exactly the failure mode the harness's own module docstring warns about), not a
  real latency effect in either direction -- consistent with the routing-decision check above finding
  zero picked-plan changes on the target population.
- `cargo build`/wheel blast radius: `git diff --stat costcell/trunk` shows only `card_engine/src/lib.rs`
  touched (58 lines: one new constant + its doc, five lines in the `CardRangePopcount` arm).

### Round 7

Target: resolve the population-size discrepancy the assignment opened with -- Round 5's "single:range"
bucket (a single family/predicate drawn from `RANGE_FAMILIES`, `unique=card`) was 3,041 rows, 53.7% of
pooled error, but Round 6's fix only touches the `CardRangePopcount` arm, which Round 6 itself measured
at ~1,660 rows (~4.7% of the pooled cell) -- smaller than the bucket. Where does the rest go?

**The `Prep::Candidates` hypothesis was checked first and refuted.** Reading `card_range_popcount_
applicable` (lib.rs:9485) confirms it requires `plane.is_none()`, a bare range (`bare_range_bounds`),
AND `indexes.sort_perms.order(sort_col, descending, cards.len()).is_some()` -- both sort-permutation
directions for the query's exact orderby/direction/card-count combination. But the NEXT branch acquire
tries when that fails is not `Prep::Candidates` -- it is `PrintingCompose`, which is mode-agnostic and
requires no sort permutation at all (`printing_compose_applicable`, lib.rs:9437, and `is_printing_
composable`'s range arm, lib.rs:6866, both gate only on `bare_range_bounds(...).is_some()`). A direct
sample confirms this empirically: of 1,184,753 bare single-range `unique=card` queries generated
(varying orderby/direction/limit/offset the way `bench_cost_model_agreement.py` does), 56.3% acquired
via `card_range_popcount` and the remaining 43.7% via `printing_compose` -- **zero** via `candidates`.

**A second mechanism, found while building that sample, matters just as much: two-sided bounds.**
`Shape(families=RANGE_FAMILIES, predicates=1, unique={"card"})` -- Round 6's own generator for this
population -- can render its one drawn predicate as a fused two-sided bound (e.g. `eur>=0.23
eur<=0.45`), because `QuerySampler`'s `bounded` parameter defaults to `None` (either shape, drawn at
random) rather than `False` (one-sided only). `bare_range_bounds`, `CardRangePopcount`'s own gate,
matches a single comparison and never an `And` (confirmed directly in `fuse_and_range_children`'s own
doc: "a FUSED two-sided range never arrives here at all"), so a two-sided bound reaches
`PrintingCompose` regardless of sort permutation. Round 5's AST-shape bucketer keyed "single" on the
SAMPLER's predicate count (one family drawn), not on `FilterExpr` structure -- so "single:range"
always included these two-sided `And`-shaped rows, they were just never told apart from true bare
leaves until this round asked.

**Conclusion: the missing population is real, independently broken, and reaches `PrintingCompose`'s
OWN broad-guard reset -- exactly Round 5's "what Round 6 should target" recommendation item 1, which
Round 6 explicitly deferred** ("Extend ... `PrintingCompose`'s own reset so it also scales `scan_units`
when the guard fires but `is_cross_index_range_and` is false ... a bare broadcast legality/range
predicate"). Sampled 23,039 `printing_compose`-acquired rows from the same shape (240s budget, fresh
seed): 13,053 (56.6%) have the guard fire (`scan_units == n_printings`), split 5,300 true bare-single /
7,753 fused two-sided. Measured against the real `printings_examined` GatheredScan counter:

```
                                  eval_domain/cards_visited   scan_units/printings_examined   printings_examined/n_printings
broad (guard fired, n=13,053)             mean 1.000                  mean 2.023 (median 1.917)        mean 0.518 (median 0.522)
narrow (guard not fired, n=9,986)         mean 0.905 (median 0.963)   mean 0.381 (median 0.382)        mean 0.122 (median 0.131)
```

`eval_domain` is exact on the broad subset (matches every prior broad-guard round). `scan_units` is
badly over-costed there (predicted ~1.9-2.0x too high), confirming Round 5's bucket-level median ratio
of 0.64 for "single:range" was a blend of this over-costed `printing_compose` slice and Round 6's
now-fixed `card_range_popcount` slice, not evidence Round 6 left its own target undone. The narrow
subset is badly UNDER-costed (median 0.38) -- a second, separate bug in `PrintingCompose`'s non-broad
branch for this same shape, structurally the same phenomenon Round 6 flagged in `CardRangePopcount`'s
sibling `else` branch (a card-count-shaped estimate undershooting a printing count) -- **not fixed this
round**, out of the assigned scope, noted below for a future round.

**Self-check (pre-computation constraint).** The new gate, `is_same_index_range_only` (lib.rs, next to
`is_cross_index_range_and`), is O(children): it calls `bare_range_bounds` per child (a pure match plus
float comparison, no index probe) and compares index pointers, the identical technique and complexity
class `is_cross_index_range_and` already uses, only run inside the same `unwrap_or_else`-adjacent
branch after `exact_cards` has already declined. No new per-query scan, no new index probe -- confirmed
by `cargo test`/`cargo clippy` and the latency A/B below.

**Fit.** Split the 13,053 broad rows by `hash(query|orderby|direction) % 2`: 6,598 calibration / 6,455
held-out. Swept 0.20-0.80 in steps of 0.02 on the calibration half only; both halves' error-vs-scale
curves are smooth and convex, minimizing at the SAME 0.52 (each sub-shape's own argmin -- 0.48 bare-
single, 0.55 fused two-sided -- brackets it tightly, so one flat constant was kept rather than two).

```
                          calibration (n=6,598)            held-out (n=6,455)
scan_units total abs     310.8M (1.0) -> 60.2M (0.52)      304.8M (1.0) -> 57.6M (0.52)
improved / regressed              6,560 / 38                       6,422 / 33
```

**Price-triple sanity (per-field, not cross-field correlation -- a flat scale on an already-computed
ceiling has no per-leaf independence assumption to violate, same reasoning as every prior broad-guard
constant).** `usd` (0.534), `eur` (0.546), `tix` (0.574) all sit in the same band as `cn`/`date`/`year`
(0.46-0.49); `tix` reads highest but not an outlier.

**Verified against the real build, not just the Python-side sweep.** Rebuilt with the constant and
replayed the identical 13,053 rows directly against it: `scan_units` matched `round(0.52 *
n_printings)` exactly on all 13,053 rows (0 mismatches, i.e. `is_same_index_range_only` correctly
recognized every one of them), and the real paired total (117,812,900) landed on the exact same number
as the calibration+held-out simulation combined (60,175,506 + 57,637,394).

### Round 7 confirmation runs

- `cargo test --manifest-path card_engine/Cargo.toml`: 167 passed, 0 failed, 56 ignored.
- `cargo clippy --manifest-path card_engine/Cargo.toml --all-targets -- -D warnings`: clean.
- `bench_regret_matrix.py --seconds 120 --mode uniform`: same shape as every prior round -- `printing_
  compose` still 95% share both builds, `StreamedSelect -> GatheredScan` / `GatheredScan ->
  PrintingCompose` still the largest picked/best mismatches, total regret comparable (49.9ms baseline
  vs 50.7ms modified) -- nothing resembling the 23.6x acquire-time precedent.
- `bench_query_latency_ab.py --mode realistic --sample 800 --seed 1`: real diff (baseline vs modified)
  `+0.7µs, 95% CI [+0.5, +0.9]`, "B is SLOWER". A same-build canary (baseline vs a second baseline run,
  identical protocol, nothing changed) read `+0.5µs, CI [+0.3, +0.7]`, also "B is SLOWER" -- same sign
  and comparable magnitude with zero code difference, matching every prior round's non-interleaved-run
  drift finding. Read as no detectable latency effect, not a confirmed regression.
- `git diff --stat costcell/trunk` shows only `card_engine/src/lib.rs` touched (101 lines: two new
  constants + their docs, one new helper function, four lines wiring it into the broad-guard branch).
- Full-table checkpoint (`bench_cost_model_agreement.py --seconds 300 --seed 0`): `GatheredScan`/`card`
  17% -> 18% within [0.8, 1.25], both within noise of each other (n=33,218 baseline, n=33,121
  modified) -- expected, same reasoning as every prior round: this cell pools every card-mode
  `PrintingCompose`/`CardRangePopcount`/`candidates`/`plane` acquire, and this round's target (a single
  range family reaching `printing_compose`'s broad guard) is a small slice of it. The pooled
  `GatheredScan`/`printing_compose` row (every `unique` mode, n=54,827/54,658) also held steady at 24%
  both builds -- same story, an even larger and more diverse pool this fix touches only a slice of.

**Next steps for a future round.** The narrow-subset (`range_too_broad_to_narrow` NOT fired)
`printing_compose` bare-range population found mid-investigation above (median `scan_units` ratio 0.38,
n=9,986 in this round's sample) is real, separately broken, and out of this round's assigned scope --
structurally the same "card-count-shaped estimate undershooting a printing count" bug Round 6 flagged
in `CardRangePopcount`'s sibling `else` branch, now confirmed to have a `PrintingCompose`-side
counterpart too.

### Round 8

Diagnostic only, no code changes. Rounds 1-7 exhausted the printing-range-index family
(`compose_printing_estimate`/`CardRangePopcount`/`PrintingCompose`, all reached via `Prep::Range`) and
the pooled `GatheredScan`/`card` cell still reads 13-16% within [0.8, 1.25], essentially unchanged from
Round 0's baseline. This round asks where the rest of the error lives, and finds it in a completely
different acquire branch: `Prep::Candidates` (`count_source == "candidates"`), reached whenever
`prepare_candidates`/`narrow_rec` cannot resolve the query to a bare range or a fully plane-compilable
expression -- text search (`name`/`o`/`ft`/`a`), the extended arithmetic syntax (`power+toughness<6`,
`cmc>=power`), `is:`-rewrite predicates, `loyalty`, and any `Or`/negated/nested-paren structure, none of
which `is_printing_composable`/`is_broadcast_leaf_shape` accept.

**Checkpoint** (`bench_cost_model_agreement.py --seconds 180 --seed 0`, isolated release wheel, same
protocol as every prior round): `GatheredScan`/`candidates` reads `n=22,190 median 0.60 p10 0.25 p90
0.92 13% within 25% FAIL` -- the single largest acquire-branch row in the whole per-plan table by row
count, well below the `[0.8, 1.25]` bar, and **over-costed** (`median < 1`), the opposite direction
from every range-leaf fix Rounds 1-7 shipped.

**Method.** Two throwaway samplers (not checked in), both pinning `unique=card` and varying
`orderby`/`direction`/`limit`/`offset` the way `bench_cost_model_agreement.py` does, against an
isolated release wheel:

- **Flat-conjunction sample** (`QuerySampler.query()`'s own body, reimplemented inline per Round 5's
  trick so each row keeps which families were drawn): 300s, uniform mode, seed 0 -- 125,680 queries
  sampled, **45,451 kept** after filtering to `count_source == "candidates"` and a non-declined
  `GatheredScan` trial. This is the same population `bench_cost_model_agreement.py` itself samples
  (same generator), just larger and carrying per-row family/shape metadata the harness doesn't keep.
- **Structured-connective sample** (`QuerySampler.structured_query()`, which draws `Or`/negated/
  parenthesized/regex shapes `query()` can never produce): 240s, uniform mode, seed 1 -- 47,058
  sampled, **29,192 kept**. `bench_cost_model_agreement.py` cannot see this population at all --
  `sampler.query()` only ever emits a flat conjunction -- so it is invisible to the checkpoint number
  above regardless of how large its error turns out to be.

Per row: `predicted_ns` = `costbench.predicted_ns` (the `GatheredScan` trial's `predicted_ns`),
`measured_ns` = `costbench.plan_self_ns` (the same netting rule the checkpoint gate uses -- `candidates`
is in neither `RANGE_ACQUIRES` nor exempt, so `plan_self_ns` is the executor alone, no `ns_prepare`
added back). Feature-level: `explain`'s own `acquire.scan_units`/`acquire.eval_domain` against the
`GatheredScan` trial's real `printings_examined`/`cards_visited` counters -- the same pairing Rounds
3-7 used for the range family, applied here to `Prep::Candidates` for the first time.

**Which feature is actually mismatched -- checked, not assumed.** Over the flat-conjunction sample:

```
eval_domain / cards_visited     n=30,794   median 1.00   p10 1.00   p90 1.00   (essentially exact)
scan_units  / printings_examined n=30,586   median 1.00   p10 0.52   p90 3.00   (noisier, but not
                                                                                  systematically over)
overall measured_ns / predicted_ns  n=45,451  median 0.49  p10 0.24  p90 0.86   within25% 8%
```

`eval_domain` is exact everywhere sampled. `scan_units` is close to exact at the pooled median and, in
several of the highest-magnitude buckets below, **under**-predicts (real `printings_examined` bigger
than the estimate) -- the opposite direction from the pooled ns-space over-cost. Neither size feature
is the mismatched one; the bug is downstream, in how `GatheredScan`'s rate/fixed constants
(`cost.rs`'s `PhysicalPlan::GatheredScan` arm) convert these already-correct features into nanoseconds
for this acquire branch specifically.

**Ranked bucket table** (flat-conjunction sample, `structure:sorted-category-tuple`, same taxonomy
style as Round 5 but rebuilt for this population -- `arith`/`text`/`collection`/`broadcast`/`range`/
`rarity`/`legality`/`loyalty` categories, since Round 5's range-family taxonomy under-describes a
population dominated by families no printing-range machinery ever sees):

```
bucket                                 n   share(abs ns err)  med_ns  med_scan_units  med_eval_domain  within25%
single:arith                        2188              25.3%    0.66            1.00             1.00        0%
and2:arith+range                     681              16.6%    0.71            1.74             1.00       30%
single:collection                    724              13.9%    0.66            1.00             1.00        4%
single:text                         8420               6.6%    0.58            1.00             1.00       14%
and2:collection+range                289               5.8%    0.61            1.70             1.00       12%
and2:range+text                     2799               4.4%    0.63            1.16             1.00       19%
and2:arith+rarity                    142               4.2%    0.59            1.72             1.00       27%
and2:arith+broadcast                 662               2.7%    0.66            1.00             1.00        1%
and2:broadcast+collection            269               2.6%    0.49            1.00             1.00        1%
and2:arith+collection                1291              2.6%    0.52            1.00             1.00        8%
```

(`arith` = extended syntax over `power`/`toughness`/`cmc` compounds, never `is_broadcast_leaf_shape`-
eligible since that gate requires a bare `NumField`, not a `NumExpr::Add`, so every arith predicate
lands in `candidates` unconditionally; `collection` = `type`/`keyword`/`tag`/`produces`/`set`/`border`/
`frame`/`watermark`/`devotion`; `broadcast` = `color`/`identity`/`cmc`/`pow`/`tou` singleton leaves that
usually escape to `Prep::Plane` but land here when paired with a non-composable partner.)

**Not shape-concentrated -- broad-based instead.** Every top-10 bucket reads `median_ns` in a tight
0.49-0.71 band regardless of which families are involved -- text-only, collection-only, and every
arith combination all cluster together. This is the opposite of Rounds 3-7's range-leaf findings,
where the fix was scoped to one precise shape; here the shape taxonomy is not the axis that
separates fixed from broken. `scan_units`'s per-bucket median tells the same story from a different
angle: it reads exactly 1.00 (agreeing with the real count) for every bucket where the query's
predicates carry high selectivity relative to the corpus, and 1.16-1.74 (UNDER-predicting) for the
`range`/`rarity`-paired buckets -- i.e. the one feature that DOES vary across buckets moves in the
wrong direction to explain a uniform over-cost.

**What actually separates fast-and-cheap from over-costed: `eval_domain` SIZE and match rate, not
shape.** Cutting the same sample by predicted `eval_domain` decile:

```
eval_domain range        n      median ns_ratio
0                     13,635          0.25   (deciles 0-2, exactly zero candidates)
(0, 2]                 4,545          0.39
(2, 9]                 4,545          0.46
(9, 23]                4,545          0.53
(23, 57]               4,545          0.60
(57, 161]              4,545          0.62
(161, 937]             4,545          0.68
(937, 31724]           4,546          0.68
```

and by verify-cost tier (`residual_tier_ns100`, from `filter.rs`'s `verify_cost_tier`):

```
tier                          n   share(abs ns err)   median ns_ratio
MASK_COMPARE (400)         7,102              49.0%              0.49
0 / all_match_known       15,590              39.3%              0.57
SET_LOOKUP (900)          16,490               9.6%              0.47
TEXT_SCAN (2,300)          5,578               1.4%              0.38
REGEX_MACHINERY (5,000)     691               0.7%              1.59
```

The two biggest tiers by magnitude (MASK_COMPARE, all_match_known) are not the two most *miscalibrated*
by ratio -- they dominate by ROW COUNT (88% of rows between them), same "volume, not tier-specific
miscalibration" pattern the doc has seen before. The real signal is the monotonic decay above: ratio
degrades steadily as `eval_domain` shrinks toward zero, which points at **two separate, compounding
mechanisms** rather than one shape-specific bug:

1. **`GATHER_FIXED_COST_NS` (169.6ns) is ~4x too high for zero-match rounds.** 14,657 of the 45,451
   sampled rows (32%) have `matches == 0` -- every multiplicative term in `PhysicalPlan::GatheredScan`'s
   `cost.rs` formula vanishes, so `predicted_ns` collapses to exactly `GATHER_FIXED_COST_NS` (median
   predicted 169.6ns, matching the constant to the decimal). Real measured cost for these rounds: median
   42.0ns -- a clean, isolated, shape-independent 4x over-charge with no other term involved. Cheap in
   absolute ns per query, but 32% of the whole `candidates` population by row count, so it alone would
   move a meaningful share of the within-25% pass rate.

2. **Card-mode's `feats.matches = count` (unconditional, `candidate_feats`, `lib.rs` ~11776) ignores real
   residual selectivity, and the per-candidate verify-tier charge doesn't discount for it either.**
   Printing/artwork mode already has a residual-pass-rate discount here (`RESIDUAL_PASS_RATE_PRINTING`/
   `_ARTWORK`); card mode has none -- `matches` is the full candidate count regardless of whether
   `all_match_known` holds. Concrete example, resampled 41 times in this run (`is:vanilla`, a static
   `tag`-family value): `eval_domain = pred_matches = 17,437` (`residual_card_invariant = true`, tier
   `MASK_COMPARE`), but `real_matches_pushed = 343` -- **2.0%** of predicted. `predicted_ns ≈ 601,657`,
   `measured_ns ≈ 95,000`, ratio **0.16** -- worse than the zero-match mechanism above, and at a LARGE
   `eval_domain`, contradicting a naive "small eval_domain only" read of the decile table. Not an
   `is:`-specific artifact: the same `eval_domain >= 2,000` + `MASK_COMPARE` slice (n=756, 684 distinct
   queries) reads median ratio 0.67, and the non-`is:` members alone (`t:creature year>2001` ratio 0.39,
   `cmc>=power year>=1997` ratio 0.34, `name:s eur<=5.06` ratio 0.48, ...) show the same direction and
   comparable magnitude. Over the whole residual-present population (`tier > 0`, n=29,861):
   `real_matches_pushed / pred_matches` reads median 1.000 (most queries genuinely do have most
   candidates match) but **p10 0.033** -- a real, fat left tail of 30x-overestimated match counts, not
   a single outlier. `GATHER_PUSH_PER_MATCH_NS` (2.24 ns/match) explains only part of the gap in the
   `is:vanilla` example (~39K ns of the ~507K ns predicted-minus-measured gap); the dominant term is
   `eval_domain * (GATHER_LOOP_PER_CARD_NS + GATHER_CARD_PASS_NS + max(tier_ns, GATHER_RESIDUAL_FLOOR_NS))`
   (~449K ns of that gap) -- i.e. the flat per-candidate verify-tier charge itself is too high whenever
   the residual is this selective, plausibly because a real `card_pass` short-circuits cheaply on most
   candidates at low match rates in a way `verify_cost_tier`'s single-node "worst child wins" model
   cannot see, and `GATHER_RESIDUAL_FLOOR_NS` (18.89, calibrated -- per its own doc comment -- against
   `bench_streamed_loop`'s always-true `DateCmp` design, a HIGH-match-rate population) may not transfer
   to a low-match-rate residual the way that comment's own precedent ("the third time this file has
   caught the same artifact") would predict. Both `residual_card_invariant = 0` (n=25,677, median ratio
   0.47) and `= 1` (n=4,184, median ratio 0.38) show the same direction, so this is not exclusive to
   card-invariant residuals either.

**Pooling check (the task's explicit ask): does the over-cost direction hold uniformly, or does it
mask an opposite error?** Within the flat-conjunction sample, YES it holds uniformly at the AST-shape
level (every top-10 bucket's median sits in 0.49-0.71, no bucket flips sign) -- but the
`scan_units`-feature check above already found the masked opposite: several buckets' `scan_units`
*feature* under-predicts (1.16-1.74x) inside the SAME rows whose *time* prediction over-costs, meaning
a naive "fix scan_units" reading of this cell would move the wrong lever. The real masking is
structural rather than per-bucket: `bench_cost_model_agreement.py`'s flat-conjunction sampler cannot
produce the population below at all, so its 13% headline is blind to it entirely, not merely diluting it.

**A third, structurally invisible population: `Or`/negation/nested-paren connectives.** Sampled via
`structured_query()` (`STRUCTURES`, never reachable through `sampler.query()`), 29,192 candidates rows:

```
structure         n   share(abs ns err)  median ns  p10   p90   within25%
regex          3,812              33.4%       0.54  0.23  1.28        7%
neg-or         3,895              18.8%       0.96  0.36  2.27       23%
or3            2,203              16.3%       0.72  0.36  1.41       27%
and-of-ors     2,650               9.7%       1.05  0.40  3.48       22%
or2            1,668               8.0%       0.68  0.34  1.28       21%
neg-and        2,519               5.9%       0.58  0.29  1.25       17%
paren-or       2,637               4.9%       1.07  0.40  2.62       21%
and-or         2,238               2.4%       0.68  0.25  1.96       19%
and2/and3/and4/single (this run)  7,570        0.5%  0.25-0.63  0.24-0.41  0.69-0.93   4-10%
```

This population's median ratios (0.54-1.07) look far closer to the `[0.8, 1.25]` bar than the flat-
conjunction population's do -- but the p90 column tells the opposite story: 1.25-3.48x, a severe
UNDER-cost tail, the OPPOSITE direction from the flat-conjunction over-cost. Pooling this in with the
flat population (which the real `bench_cost_model_agreement.py` never does, since it cannot sample
`Or`/negation at all) would report something close to "fine," masking a tail that is large enough by
row-count share (regex alone is 33.4% of THIS sample's pooled error) to plausibly drive real routing
regret -- a query whose true cost is 2-3x its prediction can lose an argmin to a plan that looks
cheaper on paper but isn't. This population needs its own round; it cannot be fixed by the same lever
as the flat-conjunction findings above (median direction is opposite), and no existing harness tracks
it at all.

**What Round 9 should target, in order:**

1. **`GATHER_FIXED_COST_NS` for zero-match `candidates`-acquired `GatheredScan` rounds** (mechanism 1
   above) -- cleanest, most isolated, no shape dependency, same "precomputed floor constant" pattern as
   every prior round's fix; likely the highest-confidence, lowest-risk first move given how cleanly it
   isolates (predicted collapses to exactly one constant, real measured is a flat ~42ns).
2. **Card-mode's `feats.matches` / the per-candidate verify-tier charge at low real match rates**
   (mechanism 2) -- larger in magnitude (dominates the top-10 bucket table) but needs a genuine
   calibration/held-out split against the real `card_pass` short-circuit behavior before trusting a
   constant, not just a flat scale reused from mechanism 1; the price-triple-style correlation check
   from Rounds 2-3 has no equivalent risk here (no independence-product combination proposed), but the
   held-out split discipline from every prior round still applies.
3. **The `Or`/negation/nested-paren population**, once 1-2 are shipped and re-measured -- needs its own
   sampler wired into whatever harness tracks it going forward, since `bench_cost_model_agreement.py`'s
   own generator structurally cannot see it.

### Round 9

Took Round 8's item 1 (`GATHER_FIXED_COST_NS` for zero-match `candidates`-acquired `GatheredScan`
rounds). First fix in this doc that lives in `cost.rs`'s cost FORMULA rather than `lib.rs` feature
estimation -- Rounds 1-8 all fixed a feature (`scan_units`/`eval_domain`) feeding an otherwise-correct
formula; here the features are already exact and the RATE/FIXED constant converting them to ns is
wrong.

**Independent re-confirmation of Round 8's diagnosis.** Fresh sample (not Round 8's own, a new
throwaway sampler, uniform mode, seed 0, 240s, isolated release wheel): 31,030 `GatheredScan`/
`candidates` rows, 9,890 (31.9%) with `matches == 0` -- matching Round 8's reported 32% closely.
Checked every non-fixed term individually rather than trusting the "collapses to one constant" claim:

```
field                     nonzero_count / n     max
eval_domain                      0 / 9,890        0
scan_units                       0 / 9,890        0
artwork_seen_printings           0 / 9,890        0
cards_visited                    0 / 9,890        0
printings_examined                0 / 9,890        0
matches_pushed                    0 / 9,890        0
```

Every term this arm multiplies by really is zero (not just small) for this population, so
`predicted_ns` reads EXACTLY `169.6` (min == max == median across all 9,890 rows) -- confirmed, not
assumed. Real measured `plan_self_ns`: median 42.0, p10 41.0, p90 84.0 (bimodal: card/printing modes
cluster at ~42, artwork at ~84 -- see below). Ratio (measured/predicted): median 0.248, 0/9,890 (0.0%)
within [0.8, 1.25] -- confirms the ~4x over-charge exactly as Round 8 reported.

**Calibration.** Hash-of-query split (`sha256(q) % 2`), same rule as every prior round. Calibration
half (n=4,944): the L1-optimal single constant is the median measured `plan_self_ns`, `42.0` -- chosen
without looking at the held-out half. Held-out half (n=4,946):

```
                         before (169.6)     after (42.0)
median ratio                   0.248             1.000
within-25%                      0.1%            57.7%
total abs ns error           530,256           103,110   (5.1x reduction)
paired diff: 4,577 improved / 369 regressed / 0 tied
```

**Per-mode split, not chased.** `PlanFeatures` carries no `unique`/mode field this arm can read, so
one pooled constant is what `cost.rs` alone can express. Held-out breakdown by mode: card (n=1,657)
83.6% within-25%, printing (n=1,625) 90.0%, artwork (n=1,664) 0.3% -- artwork's real zero-match cost
reads a flat ~2x higher (~84ns vs. card/printing's ~42ns, plausibly `exec_gathered_scan`'s
unconditional per-printing dedupe check setup), so a single pooled constant necessarily leaves
artwork's ratio at ~2.0 (previously ~0.495 -- same log-magnitude, flipped sign, and still a net win on
absolute ns error: |84-169.6|=85.6 -> |84-42|=42.0). Splitting this by mode needs a new `PlanFeatures`
field, which needs a `lib.rs` change -- out of scope for a `cost.rs`-only round, noted for later.

**A gate-precision risk found and checked, not assumed safe.** `matches == 0` is not exclusive to the
`candidates`/`plane` acquire branches Round 8 scoped its diagnosis to. `explain_analyze` costs every
CANDIDATE plan from one shared `PlanFeatures` per acquire (`plan_cost(plan, &facts.feats)`, called
once per plan in a loop at `lib.rs:12917`), so `GatheredScan`'s own `matches == 0` also fires when it
is costed under a `printing_compose`/`card_range_popcount`/`printing_range_scan` (RANGE_ACQUIRES)
acquire branch -- and there, `eval_domain == 0` is not a real empty candidate list, it is this
branch's shared `feats` never having computed one for `GatheredScan` specifically (the acquire chose a
different plan and never ran `prepare_candidates`). If `GatheredScan` is later picked or forced as a
competitor, dispatch pays a REAL `prepare_candidates` rebuild (`plan_self_ns` adds `ns_prepare` back in
for RANGE_ACQUIRES, per `costbench`'s netting rule) that no term in this arm prices at all -- sampled
directly: 375 `printing_compose`-acquired `GatheredScan`/`matches==0` rows, 358 with `eval_domain==0`,
median measured 4,959ns against a predicted 169.6ns (29x under, PRE-EXISTING, not caused by this
round). Lowering the fixed cost to 42.0 makes this already-broken slice numerically worse in isolation
(29x -> 118x under) — same direction, no new sign flip.

Checked for a REAL regression, not just reasoned about: this population is not purely diagnostic —
`GatheredScan` is the actually-`picked` plan in 93/96 (97%) of sampled zero-match `printing_compose`-
acquire rows. Directly diffed a same-build wheel and found 2 genuine routing flips in a 107-row sample
of RANGE_ACQUIRES zero-match rows where a competing plan's predicted cost sat between the old (169.6)
and new (42.0) constant (`date<1993-08-05` and `tix<0.01` under `printing_range_scan`: `PrintingRangeScan`
predicted 150.0 picked at baseline, `GatheredScan` predicted 42.0 picked after this round's change).
Ran the two tools built to catch exactly this:

- `bench_regret_matrix.py --seconds 60 --seed 0`: total regret 27.6ms baseline, 27.6ms modified (18,181
  vs 18,349 multi-plan queries — wall-clock-budget variance, not a code effect); no new row in the
  `picked -> best` mismatch table.
- `bench_cost_model_agreement.py --seconds 300 --seed 0`: `GatheredScan/printing_compose` unchanged
  (n=58,444→59,178, median 1.15→1.14, within-25% 24%→24%); `GatheredScan/printing_range_scan` unchanged
  (median 1.09→1.08, 61%→62%); `GatheredScan/card_range_popcount` unchanged (median 0.96→0.97, 51%→51%).
  12/17 acquire-branch cells inside [0.8, 1.25] both builds (unchanged); by-unique table improves 9/12
  -> 10/12 (`GatheredScan/card` flips FAIL -> PASS).

So the affected RANGE_ACQUIRES slice is real, pre-existing, and made worse in isolated ratio terms, but
too small (2 flips in 107 sampled rows; the whole slice is ~2% of its own already-passing pooled cell)
to move any reported cell or the regret total. `matches == 0` is therefore a correlated proxy, not the
exact phenomenon Round 8 scoped ("`Prep::Candidates` zero-match"), and a future round that can touch
`lib.rs` should add an acquire-branch-aware feature (or a `real_candidates_built: bool`) to gate this
cleanly rather than relying on this round's empirical "checked, found immaterial" result indefinitely.

### Round 28

Round 27 ([reference-engine-cost-model-cleanup-final-ab.md](reference-engine-cost-model-cleanup-final-ab.md))
ran the first fresh, paired `main`-vs-`costcell/trunk` A/B this whole 27-round effort had done and
found a real, previously-invisible regression: `bench_feature_accuracy.py`'s pooled `scan_units`
feature (graded against the real `printings_examined` counter, not against a rate-fit like
`bench_cost_model_agreement.py`) reads clean on `main` (median 1.00) but `UNDER-COUNTS` on
`costcell/trunk` (median 0.70). This section is the follow-up round tasked with finding and fixing it.

**Bisection.** Built an isolated release wheel at every `Engine:` commit between `main` and
`costcell/trunk`'s tip (17 candidates) and ran `bench_feature_accuracy.py`'s pooled `scan_units`
reading at each. Clean through Round 6 (`ce860337`, pooled median 0.94). The very next commit,
`e1c40466` ("A Broad-Guard Scale for PrintingCompose's Own Bare/Fused Range Reset", this doc's own
Round 7 above), drops it to 0.69 — the exact commit that tips the pooled metric from PASS to FAIL.
Every commit after that (Rounds 9, 14/15's verify-bypass work, Round 22's `best_other` gate, Round
24's `PairTotals` extension) holds steady at 0.68-0.70, confirming Round 7 is the trigger, not a later
round compounding an already-broken number.

**Mechanism.** `e1c40466`'s own fit — and Round 4's `COMPOSE_RANGE_AND_BROAD_SCAN_SCALE` fit above it
— were each calibrated exclusively against `unique=card` samples (Round 4: "Sampled 1,500 and2/and3
RANGE_FAMILIES queries (`unique=card`..."; Round 7: "the same shape" as Round 6's own
`unique={"card"}` generator). But the guard both scales live in (`acquire_plan_features`, the branch
starting `let (eval_domain, scan_units) = if ... range_too_broad_to_narrow(...)`) runs *after* the
`match mode { Mode::Printing => ..., Mode::Card => ..., Mode::Artwork => ... }` block, unconditional on
`mode` — so both scales were applied to `Mode::Printing`/`Mode::Artwork` too, shapes neither
calibration sample ever contained.

Checked directly rather than assumed: a fresh sample of this exact guard-fired population, split by
`unique`, reading `printings_examined / n_printings` (the real, unscaled ground truth):

```
('broad', 'card'):     n=303  p10=0.520  p50=0.520  p90=1.127
('broad', 'printing'): n=230  p10=0.520  p50=0.520  p90=0.520
('broad', 'artwork'):  n=956  p10=0.520  p50=0.520  p90=0.520
```

`Mode::Printing`/`Mode::Artwork` read **exactly** 0.520 at every percentile — zero spread, because
`printings_examined == n_printings` on every single row: those two modes' materializing kernels never
short-circuit, so a query broad enough to fire this guard really does walk the *entire* candidate
printing span, always. `Mode::Card`'s own kernels do short-circuit per candidate (the property both
scales were fit to exploit), which is why its own ratio has real spread (p90 1.127, not pinned to
0.520). Applying `COMPOSE_SAME_RANGE_BROAD_SCAN_SCALE`/`COMPOSE_RANGE_AND_BROAD_SCAN_SCALE` to
Printing/Artwork mode was therefore manufacturing a clean, deterministic ~0.52x/0.7x under-count out
of a population whose true ratio is 1.0 — not a mixed population, not noise, a mode-scoping bug with a
single, uniform failure mode.

This is *also* why the pooled metric moved as much as it did despite `scan_units [printing_compose]`'s
own per-acquire median barely changing (0.39 → 0.39 across the fix): the guard-fired subset is only
~4-12% of `printing_compose`'s rows per mode (dwarfed by the pre-existing, separately-tracked "narrow"
bucket — the `range_too_broad_to_narrow`-NOT-fired population Round 7 above already named and
deferred: "a card-count-shaped estimate undershooting a printing count... not fixed this round"). But
before this fix, those rows sat *above* 1.0 (the OLD unscaled `n_printings` ceiling, ~1.9-2.0x
over-counted per Round 7's own measurement) — moving them down to 0.52 didn't change the sub-bucket's
own median, but it did remove ~1,500-2,200 rows from *above* the global rank used to compute the
POOLED median, letting that rank fall into the dense, already-under-counted "narrow" bucket below it.
Round 7's fix was a real, validated improvement for the `unique=card` population it targeted — the
mode-scoping bug is what let a genuine fix for one mode quietly worsen the pooled number by removing a
compensating error for two others.

**Fix.** Gated both `is_cross_index_range_and`'s and `is_same_index_range_only`'s scale branches on
`matches!(mode, Mode::Card)`; `Mode::Printing`/`Mode::Artwork` now fall to the existing `else` branch
(the unscaled `n_printings` ceiling, already correct for every other shape reaching this guard). Zero
new computation — `mode` is already a bound local, the added check is a single enum-tag comparison —
so this carries no acquire-time cost, per this doc's own pre-computation constraint.

**Results** (isolated release wheels, `bench_feature_accuracy.py --seconds 300 --seed 0`, `main` @
`ca016410`, branch tip @ `865fb03e`):

```
                          pooled scan_units median   verdict
main                              1.00                (clean)
costcell/trunk (unfixed)         0.70                UNDER-COUNTS
costcell/trunk (fixed)           0.94                (clean)
```

The regression is closed: 0.94 sits inside the same `[0.8, 1.25]` agreement band `main`'s own 1.00
does, with no verdict flag. The residual gap between 0.94 and `main`'s 1.00 is the two *other*,
already-documented, separately-tracked contributors this round did not touch: the "narrow"-bucket
`PrintingCompose` under-count Round 7 itself named and deferred above, and the era-correlated
print-position confound for bare existential leaves
([local-engine-domain-cards-existential-arith-and.md](local-engine-domain-cards-existential-arith-and.md)'s
Round 25 section, "confirmed real, confirmed severe... out of \[that round's\] blast radius"). Both are
real, both pre-date this fix, and neither is a regression introduced by any commit on this branch —
fixing either would need touching `domain_cards`'s own broad-range estimate for bare ranges (the
`RangeCardCounts::distinct_cards` undercount this doc's own `scan_all` comments already name), which
nine prior rounds of this same effort found hard and did not attempt; left open, matching this doc's
own "Next steps for a future round" note under Round 7.

**Correctness gates.** `cargo test --manifest-path card_engine/Cargo.toml`: 177 passed, 0 failed
(debug); `--release`: 176 passed, 0 failed. `cargo clippy --manifest-path card_engine/Cargo.toml
--all-targets -- -D warnings`: clean. `git diff --stat costcell/trunk`: `card_engine/src/lib.rs` only
(19 lines).

**Confirmation pass**, before (unfixed tip) vs after (fixed), plus `main` where noted:

- `bench_regret_matrix.py --seconds 120 --mode realistic` (routed-phases builds): mean regret/query
  0.94µs (tip) → 0.95µs (fixed) → 0.95µs (`main`) — unchanged within noise; `picked -> best` SHARE
  table proportionally identical (`Perm` 69%→69%, `Gather` 15%→14%, `StreamedSelect -> GatheredScan`
  50%→47% of a shrinking pie), no anomalous transition.
- `bench_cost_model_agreement.py --seconds 300 --seed 0`: 12/17 (tip) → 13/17 (fixed) cells inside
  `[0.8, 1.25]`; every reported cell moved by less than 0.05 in ratio, one boundary flip
  (`GatheredScan/candidates` 0.78→0.81) consistent with sampling noise, not a real shift — matches
  this tool's own documented insensitivity to a feature-only fix (a rate elsewhere absorbs it).
- `bench_query_latency_ab.py --mode realistic --sample 800 --seed 1`, two order-alternated rounds plus
  a same-build canary: round 1 (tip, fixed) `+0.9µs` "B is SLOWER"; round 2 (tip, fixed) `-0.3µs` "B is
  FASTER"; canary (fixed vs fixed) `0.0µs`, CI `[-0.2, +0.3]`, no detectable difference. Opposite signs
  of comparable magnitude across the two real rounds, both inside the canary's own noise band — no
  detectable latency effect, expected for a zero-new-computation accuracy fix.
- `bench_pairwise_ordering.py --seconds 60`, realistic and uniform, `GatheredScan` vs `PrintingCompose`:
  realistic overall 89%→89% (`[printing_compose]` 91%→90%, `[plane]` 84%→86%); uniform overall 87%→87%
  (`[printing_compose]` 86%→86%). Essentially unchanged in both modes — unlike Round 7's own change,
  this fix does not touch the ordering that mattered to `#852`.

### Round 30

**Regression found by a prior diagnostic round, confirmed by bisection + literal replay (not just
correlation):** Round 1's own `scan_all` fix above (the match-density depth proxy) was legitimate and
already validated for `GatheredScan`'s `scan_units` -- but `StreamedSelect`'s own feature,
`stream_scan_units`, defaults to inheriting `scan_units` verbatim (`mk_plan_feats`'s doc: "only an
acquire that knows P3 examines fewer printings overrides it") unless the `printing_compose` acquire's
own override logic (`lib.rs`, the `feats.stream_scan_units = if tier == 0 {...} else if
filter_touches_legality(...) {...} else {...}` block) says otherwise. For a printing-varying leaf with
no legality partner (`price_usd`/`cn`/`released_at`, or an And of them), that block falls to its bare
`else { scan_units as u32 }` arm -- so Round 1's legitimate downward revision to `scan_units` rode
straight through into `stream_scan_units` too, with no acquire branch ever taught the difference. This
grew the `StreamedSelect -> GatheredScan` misroute (router picks P3 when P4 is actually faster) from
1,284 to 1,618 occurrences (mean regret 17.0us -> 21.4us) on matched-size `bench_regret_matrix.py
--mode realistic` runs -- the single largest remaining regret slice on the branch (43% share) going
into this round.

**Mechanism, confirmed directly against real dispatch counters** (not assumed from reading the code
alone): `run_query_streamed` (P3's executor) runs a first pass (`card_match_count`, over every
candidate) that is structurally identical to `GatheredScan`'s own single pass in `Mode::Card` -- both
break at the first printing satisfying the residual under `Prefer::Default`, confirmed by matching
`printings_examined` counters exactly (2,449 on both plans, `f:pioneer cn>=30 cn<=39`). What differs is
a SECOND pass this first pass's counter never sees: `run_query_streamed`'s `total <= *STREAM_MIN_MATCHES`
branch re-derives `card_pass` and re-walks the printing span for every MATCHING card a second time to
select the page (`push_card_matches`, called again, its return value discarded -- so
`printings_examined`, and therefore any `scan_units`-shaped feature, structurally cannot see this
second pass no matter how it's computed). `cost.rs`'s `StreamedSelect` arm already has a term for this
branch's OWN O(n_cards) "scan every stored count" overhead (`STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS *
n_cards`), but that floor is a per-CORPUS constant that cannot vary with a query's own `matches` count --
it was fit on a population where `matches` was small enough that the actual per-card REDO was
negligible next to the floor. On `f:pioneer cn>=30 cn<=39` (853 real matches, close to the
`STREAM_MIN_MATCHES` ceiling of 1,024) the floor alone (32.4us, `n_cards=31,724 * 1.02`) materially
undershoots the real `ns_finish` (65.3us) -- the remainder is exactly this unpriced redo, and it is why
`StreamedSelect`'s real dispatch (108.9us) is 2.3x `GatheredScan`'s (46.6us) despite identical
`printings_examined`.

**Fix** (`card_engine/src/lib.rs`, the `printing_compose` acquire's `feats.stream_scan_units` bare
`else` arm): adds a `STREAM_SMALL_TOTAL_REDO_BIAS` (`1.32`, a new lib.rs constant, NOT a `cost.rs` rate)
scaled term on top of the inherited `scan_units`, `Mode::Card` only. The redo-candidate count is the
acquire-time `result_total` estimate when it sits at or below `STREAM_MIN_MATCHES` (mirroring the same
threshold `compose_paging_with_total` already gates its own decline prediction on, a few hundred lines
up in the same function), else capped at `feats.limit` (the permutation-walk branch's own bound) rather
than dropped to zero outright -- a hard cliff at the threshold would turn the acquire estimate's own
noise into an all-or-nothing coin flip, which matters here specifically: this round's own concrete
example's acquire-time estimate (1,983) sits ABOVE the 1,024 threshold despite its REAL total (853)
landing inside the small-total branch.

**Calibration.** Bias fit against `ns_finish` minus the existing floor's own contribution (isolating
the previously-unpriced redo specifically, not re-deriving the floor), converted to `stream_scan_units`
units via the existing, untouched `STREAM_SCAN_PER_ROW_NS` (5.97), over a held-in/held-out split
(hash-of-query, 1,875/1,949 rows) of `unique=card` `printing_compose` rows where the acquire-time
estimate gates the correction AND real dispatch confirms the small-total branch actually ran
(`perm_steps == 0`, `matches_pushed > 0`). Median fitted bias 1.32 "printing units" per redone
candidate; held-out total absolute error on the unpriced remainder: 2.18e7 -> 2.06e7 (a real but
partial reduction -- this population's per-query redo cost is heavy-tailed (implied bias p10 -38.7, p90
12.7), dominated by per-query residual complexity this feature vector has no term for, not by candidate
count alone). A 4x/8x/30x sweep of the bias against the live routing-outcome metric below (not just the
ns-error metric) showed diminishing returns fast -- 5.9%->7.3% of a broader current-trunk
misroute sample fixed for a doubling of the false-positive rate on already-correct `StreamedSelect`
picks -- so the median (lowest false-positive rate, still measurably useful) was kept rather than
chasing the sweep.

**Flip-query validation.** Reproduced the ORIGINAL flip population exactly as the diagnostic round's
own `flip_finder_f3f4a017.py` does (BEFORE=`97dc30c8`, AFTER=`f3f4a017`, same seed/sample window): 114
queries found this run (consistent with the diagnostic round's own ~120, sampling noise). Replayed
against this round's FIX build (current `costcell/trunk` tip + the patch above):

```
of 114 reproduced f3f4a017 flip queries:
  now correctly pick GatheredScan (fixed):        50  (44%)
  still (wrongly) pick StreamedSelect (unchanged): 64  (56%)
  pick something else:                              0
```

**Regret matrix** (`bench_regret_matrix.py --seconds 300 --mode realistic --seed 0`, isolated release
wheels, baseline = unfixed `costcell/trunk` tip `4e101d7f` vs fix = this round's patch on top). The two
300s windows sampled different absolute query counts (121,724 vs 108,533 multi-plan queries -- system
load from other concurrent work on this box, not a code-speed effect; rates/shares below are the fair
comparison, not raw `n`):

```
StreamedSelect -> GatheredScan      n            share of traffic   mean regret   SHARE   -> ~ms
baseline (unfixed)                2,407   1.98% of 121,724 sampled    23.00us      53%    ~55.4ms
fix                                1,995   1.84% of 108,533 sampled    24.33us      56%    ~48.5ms
```

~7% fewer misroutes as a share of traffic, ~12% less absolute regret-ms attributed to this specific
transition. Total pool regret (all transitions) 104.3ms -> 86.2ms (mean/query 0.86us -> 0.79us), roughly
consistent in direction with the targeted slice, not dramatically larger -- no sign the fix disturbed
other transitions. (No dedicated same-build latency canary was run this round on top of this -- the
regret figures come from forced per-plan trial minimums, not wall-clock query timing, which is less
exposed to the sampling-count variance noted above, but a canary would still be the stronger claim; flag
this as the one gap in this round's own validation rigor.)

**Regression guards.**

- `#852` (`GatheredScan` vs `PrintingCompose` ordering, `bench_pairwise_ordering.py --seconds 300
  --mode realistic --seed 0`): overall 88% -> 88%, unchanged. By acquire: `[plane]` 83% -> 82%,
  `[printing_compose]` 91% -> 92% -- both within noise, no real shift. Clean.
- Round 28's `scan_units` feature-accuracy fix (`bench_feature_accuracy.py --seconds 120 --mode
  realistic --seed 0`): pooled `scan_units` median 1.00 -> 1.00, identical distribution shape in both
  builds -- expected, since this round's patch touches only `stream_scan_units`, never `scan_units`
  itself. Clean.

**Correctness gates.** `cargo test --release` (`card_engine`): 176/176 passed. `cargo test` (debug):
177/177 passed. `cargo clippy --all-targets -- -D warnings`: clean. Blast radius: `card_engine/src/lib.rs`
(the `printing_compose` acquire branch only) plus this doc; `cost.rs`, `estimator.rs`, `filter.rs`
untouched.

**Verdict.** Real, positive, but partial. On the population this round diagnosed and targeted directly
(the reproduced f3f4a017 flip set), 44% now route correctly again. On the broader regret matrix, the
`StreamedSelect -> GatheredScan` transition's regret is down ~7-12%, not back to `main`'s pre-regression
baseline. The residual is NOT well-explained by `cost.rs`'s rates (chunk 2's stated scope) -- it traces
to the acquire-time `result_total` estimate itself being unreliable near the `STREAM_MIN_MATCHES`
threshold for cross-index-range-leaf Ands (this round's own concrete example: real total 853, estimate
1,983, off by 2.3x), which is the SAME "separate, uninvestigated `domain_cards` bug for multi-range-index
Ands" this doc's own Round 1 section flagged as "the natural next target" and never chased. A future
round fixing that upstream cardinality estimate would likely close more of this residual than any
`cost.rs` rate refit; chunk 2 (the rate refit) still looks worth doing on its own merits but should not
be expected to finish closing this specific misroute on its own.

### Round 31

**The gap Round 30 flagged but couldn't close.** Round 30 fit `STREAM_SMALL_TOTAL_REDO_BIAS` against
`ns_finish` minus the existing floor's own contribution -- a wall-clock RESIDUAL, converted to
`stream_scan_units` units via the untouched `STREAM_SCAN_PER_ROW_NS` rate -- because no structural
counter existed for the redo pass's real work. `push_card_matches` (`lib.rs:6186`) already computes
and returns a `u32` "examined" count per call, mirroring `card_match_count`'s own `(c, examined)`
pattern -- both calls inside `run_query_streamed`'s `total <= *STREAM_MIN_MATCHES` branch's second
loop (~line 13940) simply discarded it as a bare statement.

**Step 1: the counter.** Added `PhaseStats::redo_examined: u64`, a new field zero everywhere except
this one branch (following the `set_printings`/`perm_steps` precedent: doc-declared scope, zeroed
explicitly at the other two exits of `run_query_streamed` -- the empty/past-the-end return and the
permutation walk). The small-total loop now accumulates `push_card_matches`'s return value into a
local (`n_redo_examined`) and passes it to the `publish` closure, which now takes a fourth parameter
alongside `perm_steps`. The permutation walk's OWN `push_card_matches` call (after `'walk: for cid in
walk.iter()...`) is deliberately left uninstrumented: that branch runs above `STREAM_MIN_MATCHES`,
already prices to `limit`, and its own per-step cost already flows into `ns_loop`/`ns_finish` via the
walk's wall-clock timing, the same population `perm_steps` was already calibrated against -- this
round's scope is specifically the small-total branch's previously-unpriced second pass. Surfaced to
Python exactly like `printings_examined`/`perm_steps`: a new `d.set_item("redo_examined", ...)` line in
`plan_trial_to_pydict`, and a matching entry in `scripts/costbench.py`'s `PLAN_KEYS` schema assertion.

**Free, confirmed directly, not assumed.** `push_card_matches` already computed this value on every
call in this loop; capturing it is a return-value read, not a new pass or a new computation -- no
counter, no extra field write, nothing added to what the loop already does. Confirmed both ways: (a)
by inspection -- the diff is exactly "capture the return instead of discarding it" -- and (b) directly:
temporarily reverting the capture to a bare statement (matching pre-round code) makes the new
regression test fail on its very first assertion (`redo_examined > 0`), and restoring it passes again,
with `cargo test --release` timing unaffected in either direction (the change is a single local
accumulate plus one extra `u64` in an already-stack-allocated struct).

**Regression test** (`card_engine/src/tests.rs`,
`redo_examined_counts_only_the_small_total_redo_pass`): one synthetic corpus, two disjoint match groups
(500 cards under `STREAM_MIN_MATCHES`, 2,000 over it), asserting `redo_examined > 0` and
`>= matches_pushed` on the small-total exit (`perm_steps == 0`), `== 0` on `GatheredScan` for the
identical query, and `== 0` on the walk exit (`perm_steps > 0`) for the large group. Verified to
actually catch a revert: reverting the capture to a bare statement fails the test's first assertion
with `redo_examined read 0`, exactly the Round 30 gap this round closes.

**Step 2: the refit.** Sampled `unique=card` `printing_compose` rows from `bench_regret_matrix.py
--mode realistic`'s own corpus (isolated release wheel, `--seed 13`, hash-of-query
held-in/held-out split: 2,916/3,006), gated on the same real-dispatch confirmation Round 30 used
(`perm_steps == 0`, `matches_pushed > 0`) PLUS a guard Round 30's own gate missed: `page_offset <
matches_pushed`, ruling out the OTHER `perm_steps == 0` exit (`page_offset >= total` returns before the
redo loop ever runs but still reports the counting pass's `matches_pushed`) -- without it, 816/6,747
rows silently poisoned the fit with a real redo pass that never happened.

`redo_candidates` mirrors the acquire branch's own logic exactly: the acquire-time `matches` estimate
when it's at/under `STREAM_MIN_MATCHES`, else capped at the page `limit`. Two real summary statistics
of `real_redo_examined / redo_candidates` over the calib half, and they disagree:

```
median (per-row ratio)                         1.0    p10=0.15  p90=10.2
candidate-weighted mean (sum/sum)               2.237
```

Held-out total absolute error on the real `redo_examined` counter itself (the POINTWISE metric):

```
old (1.32, Round 30's wall-clock fit)          2.467e6
median (1.0)                                   2.380e6   <- best pointwise fit
weighted mean (2.237)                          2.752e6
p75 (3.831)                                    3.336e6
```

By pointwise error alone, the median (1.0) wins -- a real, ground-truth-validated improvement over
1.32. But this population's ratio is heavily right-skewed (p10 0.15, p90 10.2: most rows sit near or
under 1.0, but a long tail runs into double digits), and the flip-query population this bias exists to
fix draws disproportionately from that tail -- a query only flips to the wrong plan when its real redo
cost was under-priced, which is exactly what the tail rows are. Checked directly rather than assumed:
replaying the same reproduced f3f4a017 flip set (below) against both candidates, the median
ACTIVELY REGRESSES queries Round 30's own 1.32 already fixed correctly, gaining nothing back. This is
the same false-positive/false-negative asymmetry Round 30's own 4x/8x/30x bias sweep found against its
noisier wall-clock-derived distribution -- resolved here against a real ground-truth counter instead of
a guessed multiplier. The ratio is flat (~1.0-1.3) across every candidate-count bucket (0-50, 50-150,
150-400, 400-1024), so the skew is in per-query residual complexity, not candidate count -- a flat
linear bias remains the right shape, matching Round 30's own conclusion.

**Fix.** `STREAM_SMALL_TOTAL_REDO_BIAS` set to **2.237** (the candidate-weighted mean), not the
pointwise-optimal 1.0 -- kept because it is the real, structurally-grounded statistic that does not
regress the live routing outcome, following the same "live outcome over pointwise ns-error" precedent
Round 30 itself set with its own bias sweep.

**Flip-query validation.** Reproduced the ORIGINAL flip population exactly as `flip_finder_f3f4a017.py`
does (BEFORE=`97dc30c8`, AFTER=`f3f4a017`, same seed/sample window), then replayed the SAME reproduced
list against three FIX builds in one script (removing the sampling-window noise a separately-run
validation would carry): Round 30's own tip (`9668dfa4`, bias 1.32), this round's pointwise-optimal
median (1.0), and this round's shipped weighted-mean (2.237).

```
of 118 reproduced f3f4a017 flip queries:
  round30 (bias=1.32):            fixed 52   still wrong 66
  round31 median (bias=1.0):      fixed 43   still wrong 75   (regresses 9 of round30's 52, gains 0)
  round31 weighted-mean (2.237):  fixed 64   still wrong 54   (regresses 0 of round30's 52, gains 12)
```

The shipped bias (2.237) regresses none of Round 30's 52 correct fixes and closes 12 more -- 64/118
(54%) now route correctly, up from Round 30's own 52/118 (44%) on this exact reproduced population (the
114/50 figure in Round 30's own doc entry came from a separate sampling run; both are the same
population modulo the classification-timing noise this whole method carries, already flagged in Round
30's own verdict).

**Regret matrix** (`bench_regret_matrix.py --seconds 300 --mode realistic --seed 0`, isolated release
wheels with `routed-phases`, before = `costcell/trunk` tip `9668dfa4` i.e. Round 30's own shipped fix,
after = this round's patch):

```
StreamedSelect -> GatheredScan      n            share of traffic   mean regret   SHARE   -> ~ms
before (Round 30's fix)           2,363   1.87% of 126,203 sampled     23.01us      49%    ~54.8ms
after (Round 31's refit)          2,129   1.73% of 123,143 sampled      9.80us      25%    ~20.7ms
```

Mean regret on this transition drops by 57% (23.01us -> 9.80us) and its SHARE of all lost time nearly
halves (49% -> 25%) -- ~54.8ms -> ~20.7ms attributed, a **62% reduction**, dwarfing Round 30's own
55.4ms -> 48.5ms (~12%). Total POOL regret (every transition) also drops, 111.9ms -> 82.7ms (mean/query
0.89us -> 0.67us) -- consistent in direction with the targeted slice, not an isolated artifact.

One nearby transition moved the other way and is worth naming rather than burying: `PrintingCompose ->
StreamedSelect` (compose picked, but StreamedSelect was really best) grew from 12% to 24% share (mean
34.28us -> 40.61us, n 396 -> 483, ~13.4ms -> ~19.8ms, +6.4ms) -- a real, expected side effect of raising
`stream_scan_units`: making StreamedSelect look pricier tips a few close compose-vs-stream calls the
other way when StreamedSelect actually was faster. Every other transition moved by less than 2 points of
SHARE in either direction. The target slice's ~34ms improvement outweighs this ~6ms give-back by 5:1,
and the total-pool number (111.9ms -> 82.7ms, -29.2ms net) confirms the net effect across the whole
matrix is a real improvement, not a wash.

**Regression guards.**

- `#852` (`GatheredScan` vs `PrintingCompose` ordering, `bench_pairwise_ordering.py --seconds 300
  --mode realistic --seed 0`): overall 88% -> 88%, unchanged. By acquire: `[plane]` 83% -> 83%,
  `[printing_compose]` 91% -> 91% -- identical in both builds, no shift at all. This round's own
  target pair, `GatheredScan` vs `StreamedSelect`, also held steady (97% -> 97% overall, 92% -> 92%
  `[printing_compose]`, 99% -> 99% `[candidates]`) -- the ordering `stream_scan_units` exists to get
  right did not regress even though its predicted GAP shrank (gap meas/pred 1.08 -> 0.55 overall):
  the model now predicts a LARGER gap than measured on this pair (conservative, not wrong-signed),
  and argmin correctness -- which side of the gap wins -- is what this guard actually checks. Clean.
- Round 28's `scan_units` feature-accuracy fix (`bench_feature_accuracy.py --seconds 120 --mode
  realistic --seed 0`): pooled `scan_units` median 1.00 -> 1.00, identical distribution in both
  builds -- expected, since this round's patch touches only `stream_scan_units`, never `scan_units`
  itself. Clean.
- Round 30's own fix: the flip-query check above IS this guard -- 0 of the 52 queries Round 30 fixed
  regressed under this round's refit.

**Correctness gates.** `cargo test --release` (`card_engine`): 178/178 passed (177 + this round's new
regression test). `cargo test` (debug): 179/179 passed. `cargo clippy --all-targets -- -D warnings`:
clean. Blast radius: `card_engine/src/lib.rs` (the new counter, its plumbing, and the
`printing_compose` acquire branch's redo-bias constant), `card_engine/src/tests.rs` (one new
regression test), `scripts/costbench.py` (the `PLAN_KEYS` schema entry for the new field), this doc.
`cost.rs` untouched, per this round's own scope.

**Verdict.** Real, significantly larger, and better-grounded than Round 30's own fix. Cumulatively
(Round 30 + Round 31 together), the `StreamedSelect -> GatheredScan` transition's attributed regret
goes 55.4ms (Round 30's own "before") -> 48.5ms (Round 30's fix, ~12% closed) -> ~20.7ms (this round,
~62% closed relative to Round 30's own before-state) -- five times the closure Round 30's wall-clock-fit
bias achieved, using the SAME feature-level lever, just fit against real structural ground truth instead
of a noisy residual. On the reproduced flip-query population this ledger entry has tracked since Round
30: 44% (52/118) -> 54% (64/118) correctly routed, with zero regression of Round 30's own fixes.

It is not fully closed. 46% of the reproduced flip population (54/118) still wrongly picks
`StreamedSelect`, one nearby transition (`PrintingCompose -> StreamedSelect`) grew by ~6.4ms as a real
side effect of raising `stream_scan_units` (a 5:1 trade against the ~34ms gained, not free), and Round
30's own diagnosed DEEPER root cause -- the acquire-time `result_total` cardinality estimate itself
being unreliable for cross-index-range-leaf `And`s near the `STREAM_MIN_MATCHES` threshold (a
`domain_cards` estimation bug, the same "natural next target" this doc's own Round 1 section flagged
and no round has yet chased) -- is completely untouched by this round. This round improved WHAT the
bias is fit against (real counter vs. wall-clock residual) and refit the constant accordingly; it did
not touch `redo_candidates`' own input (the acquire-time estimate that feeds it), which is where the
residual almost certainly still lives.

On the parent punch-list's chunk 2 (`cost.rs` rate refit, `STREAM_SCAN_PER_ROW_NS` itself): this
round's own data argues against urgency there, not for it. The real ratio read flat across every
candidate-count bucket (no saturation, no shape mismatch a rate change would fix), and a feature-level
fix alone -- with no `cost.rs` change at all -- closed 5x more of this regression than Round 30's own
attempt. A rate refit was never tested directly this round and remains formally open, but the
evidence so far suggests the acquire-time cardinality estimate (not the per-unit rate) is the more
promising next target, exactly as Round 30's own verdict already concluded.

### Round 32

**A different term than Rounds 30/31** (`walks_permutation`, the branch taken when `total >
STREAM_MIN_MATCHES`, as against Rounds 30/31's small-total gather), flagged by `cost.rs`'s own
`perm_steps` comment: the estimate (`page_span * n_cards / matches`, capped at `n_cards`) assumes
matches spread uniformly across the WHOLE corpus, but the real executor (`exec_streamed_select`)
starts and ends the walk at `walk_bounds`'s segment -- the slice the filter's own interval on the SORT
COLUMN admits, which the comment's own regrade table already showed matters (`unbounded` p90 6.43 vs
`sort-column bound` p90 5.31) without ever explaining why the bounded variant was never shipped, or
distinguishing it from the third, explicitly-rejected variant (a realized `inv_perm` span, correctly
declined for costing 0.51ns/matching card -- a real per-candidate hot-path cost this effort's
pre-computation constraint forbids).

**Why it was never shipped: not infeasible, just never circled back to.** Read `walk_bounds` and its
caller (`exec_streamed_select`, `lib.rs:10707`) and the acquire pipeline in full before assuming
either way. `walk_bounds` is already a cheap, existing function: two binary searches over the sort
permutation (O(log n_cards), nothing per candidate), early-returning the WHOLE permutation with a
single branch when the filter's bound is unbounded -- the common case, since most queries do not
filter on the same field they order by. Its input, `QueryParams::sort_bound`, is derived once per
query by `sort_col_bound` (a pure `FilterExpr` walk) at the PyO3 boundary (`bind_and_split_filter`,
`lib.rs:14360`) and attached via `with_sort_bound` BEFORE `run_query_routed` -- and therefore before
`acquire_plan_features` -- ever runs (confirmed at all three call sites: `run_query`, `explain`,
`explain_analyze`, `lib.rs:15009/15103/15152`). So the exact inputs `walk_bounds` needs
(`sort_col`, `descending`, `sort_bound`) were ALREADY sitting on `ctx`/`params` at acquire time, for
free, the whole time this effort has been running. The gap was purely that no `PlanFeatures` field
carried the segment length and no acquire branch ever called `walk_bounds` a second time to get it --
the loop-phase-measurement campaign that shipped the EXECUTOR-side bound (see
`docs/issues/done/local-engine-loop-phase-measurement.md`) used the regrade only to VALIDATE that
change, and the natural follow-up (teach the COST MODEL the same bound) was never picked up across 31
subsequent rounds. No correctness subtlety, no missing precomputed index, no rejected-and-forgotten
attempt -- just an open thread.

**Fix.** Added `cost::PlanFeatures::perm_walk_span: u32` (`cost.rs`) and a new `perm_walk_span(ctx,
params)` helper (`lib.rs`, right above `mk_plan_feats`) that calls the SAME `walk_bounds` the executor
calls, over the SAME `(sort_col, descending, sort_bound)` triple -- not a second path that could
silently disagree with what dispatch actually walks. Falls back to `n_cards` when this
`(sort_col, descending)` pair has no permutation at all (`StreamedSelect` is inapplicable there and
never reads the field, but `mk_plan_feats` sets it uniformly across all five acquire branches, since
the shared feats have to cost a competing `StreamedSelect` honestly regardless of which branch
produced them -- the same reasoning `scatter_printings`/`compose_paging` already follow). Wired into
`perm_steps`'s formula in place of `n_cards`: `(page_span * perm_walk_span / matches).min(perm_walk_span)`.
Exposed to Python via `acquire_facts_to_pydict` for grading. Self-check: the added work is one
`Option` lookup plus an early-return branch for the (dominant) unbounded case, and O(log n_cards) two
probes for the bounded case -- the same style of cheap acquire-time lookup `CardRangePopcount`'s own
range-index binary search already relies on a few branches up in the same function; no per-candidate
or per-printing cost, confirmed by the same-build canary below.

**Regression test** (`card_engine/src/tests.rs`,
`acquire_perm_walk_span_matches_the_sort_column_bound`): a small synthetic corpus (8,500 non-matching
cards sorting ahead of 1,500 matching ones under `cmc asc`, the same anti-correlated shape as the
existing dispatch-level `streamed_walk_bounds_itself_by_the_sort_column_predicate` test), asserting
`acquire_plan_features`'s returned `perm_walk_span` equals the matching segment (1,500) for both
directions under a `cmc>=5` bound, and equals the whole corpus (10,000) for the unbounded control
(ordered by `edhrec`, which the filter says nothing about). Verified to actually catch a revert:
temporarily hard-coding `perm_walk_span` back to `ctx.n_cards()` unconditionally fails the bounded
assertion with `left: 10000, right: 1500`; restoring the fix passes again.

**Held-out validation against CURRENT traffic**, not the stale comment (whose numbers predate this
whole 31-round effort). Sampled `uniform`-mode traffic through `explain_analyze` (isolated release
wheel, 180s, seed 0), keeping every `StreamedSelect` row whose realized `perm_steps` counter is
nonzero (the walking population the comment's table itself used), hash-of-query calibration/held-out
split -- nothing here is FIT, both formulas are fixed, so the split is a consistency check rather than
an overfitting guard:

```
14,217 walking StreamedSelect rows (calibration 6,657 / held-out 7,560)

                       p10     median   p90     mean |log ratio|
CALIBRATION  old (n_cards)        0.152   1.003   5.596        1.046
             new (perm_walk_span) 0.176   1.012   5.675        1.015
HELD-OUT     old (n_cards)        0.145   0.995   5.786        1.021
             new (perm_walk_span) 0.172   1.000   5.811        0.988
POOLED       old (n_cards)        0.148   0.999   5.688        1.033
             new (perm_walk_span) 0.173   1.000   5.764        1.001
```

A real, if modest, improvement that holds on BOTH halves independently (mean |log ratio| -- the
metric that treats over- and under-estimation symmetrically, which is what an argmin comparison
actually needs -- drops ~3% pooled, ~3% on calibration, ~3% on held-out). The raw percentile shape
barely moves at the tail on THIS traffic mix (p90 5.69 -> 5.76, essentially flat, not the 6.43 -> 5.31
the stale comment reported): the correlation this fix targets -- a filter that constrains the SAME
field the query orders by (`cmc>=6 order=cmc`) -- is a designed-cell phenomenon
(`scripts/bench_walk_span.py`'s own CLUSTERED-vs-BROAD framing), not a common shape under random
`uniform` sampling, so most walking rows in this population see `perm_walk_span == n_cards` (the
fallback) and are unaffected either way. The p10/mean-log movement is exactly the minority of rows
where the two formulas DO diverge, moving in the right direction.

**`StreamedSelect/candidates` cost-model-agreement, before/after** (`bench_cost_model_agreement.py`,
isolated release wheels, 180s, seed 0): **unchanged**, median 0.59 both builds (n=16,484 baseline,
n=15,440 fix -- different sampled counts from independent 180s windows, not a code-speed effect).
Split further by realized `perm_steps` within just this acquire branch (own script, same protocol,
150s):

```
                                    baseline              fix
walking (perm_steps > 0)      n=1,398  median=0.853  n=1,380  median=0.852
small-total (perm_steps == 0) n=11,227 median=0.587  n=11,104 median=0.587
```

Both sub-populations flat. The `candidates` acquire branch's own walking rows are only ~11% of its
`StreamedSelect` traffic here, and -- per the held-out result above -- most of those still see
`perm_walk_span == n_cards` under uniform sampling, so this specific pooled cell does not move
measurably even though the underlying mechanism is real (confirmed by the held-out check, which pools
across every acquire branch, not just `candidates`). Honest result: a real, validated fix with a
negligible visible effect on this specific cell under this traffic mix -- not the cell this round
closes.

**Regression guards**, isolated release wheels, `--mode realistic --seed 0`:

- `#852` (`bench_pairwise_ordering.py`, 180s): `GatheredScan vs PrintingCompose` overall 88% (n=18,431)
  -> 89% (n=20,781); `GatheredScan vs StreamedSelect` overall 97% -> 97%,
  `[candidates]` 99% -> 99%. Both within noise of independent-window sampling variance, no shift.
- Round 30/31's own territory (`bench_regret_matrix.py`, 150s): `StreamedSelect -> GatheredScan`
  n 1,060 (70% share, 20.62µs median regret, 84.2ms total) -> 1,047 (69% share, 20.83µs median,
  82.4ms total) -- flat, as expected: this round's term (`walks_permutation`) is a different branch
  from Rounds 30/31's (the small-total gather), and confirmed rather than assumed unaffected.
- Round 28's `scan_units` feature accuracy: not re-run this round -- this fix adds a wholly separate
  `PlanFeatures` field (`perm_walk_span`) consumed only by `StreamedSelect`'s `perm_steps` term, and
  touches neither `scan_units` nor `stream_scan_units`'s computation, so there is no code path by
  which it could move that cell.

**Correctness gates.** `cargo test --release` (`card_engine`): 179/179 passed (178 + this round's new
regression test). `cargo test` (debug): 180/180 passed. `cargo clippy --all-targets -- -D warnings`:
clean. Blast radius: `card_engine/src/cost.rs` (`PlanFeatures::perm_walk_span`, the `perm_steps`
formula), `card_engine/src/lib.rs` (the new `perm_walk_span` helper, wired into `mk_plan_feats`, plus
its `acquire_facts_to_pydict` exposure), `card_engine/src/tests.rs` (the six hand-built `PlanFeatures`
literals updated to compile, plus one new regression test), this doc. No other `cost.rs` rate
constants touched.

**Verdict.** Real, validated, narrow. The sort-column bound was never shipped to the cost model
because nobody had circled back to it, not because it was hard or unsafe -- every input it needs was
already free at acquire time, and the fix is a strict generalization of the existing formula (it
collapses to the old behavior whenever the filter says nothing about the sort column or no
permutation exists). Held out against current traffic, it measurably tightens the estimate on the
population it targets (mean |log ratio| improves ~3% on both calibration and held-out halves) without
moving `StreamedSelect/candidates`'s pooled cost-model-agreement cell, because that specific
correlation (filter bounds the same field the query orders by) is rare under random/uniform traffic --
a designed-cell phenomenon, not a common production shape. No regression on `#852`, on Rounds 30/31's
own territory, or on Round 28's `scan_units` cell (unreachable by this change). Shipped as a
strict-generalization correctness fix rather than for its measured routing impact, which is real but
small on this traffic mix.

### Round 33

Target: `compose_printing_estimate`'s `And` arm (`lib.rs`) falls back to a plain min-fold whenever
none of the existing tightening mechanisms apply. One common shape that falls all the way through:
`set:X` And'd with a `collector_number_int` range (`set:sld cn>=30 cn<=39`, `set:woe cn<=100`) —
`set` has no `compile_plane` arm and isn't in `ValueTotals`, and `collector_number_int` isn't
arith-tuple-eligible and has no `compile_plane` arm either, so the fold picks whichever leaf's own
CORPUS-WIDE (not set-scoped) count happens to be smaller, frequently `set:X`'s own full postings
length — discarding the `cn` bound's selectivity entirely.

**Fix.** `set_collector_ranges: HashMap<String, SetCollectorRange>` (`lib.rs`, new field on
`CardIndexes`, next to `set_codes`), holding each set's `collector_number_int` `min`/`max`/`count`
— built once at load time (`build_set_collector_ranges`, one O(n_printings) pass alongside
`set_codes`'s own existing pass, not a second scan class) and read as an O(1) `HashMap` lookup per
query, never a per-set postings scan. In `compose_printing_estimate`'s `And` arm, a new tightening
step (right after `pair_bounded_min`) detects the strict 2-source shape (after
`fuse_and_range_children`: a `set:X` leaf and a lone `collector_number_int` source, fused two-sided
or bare one-sided — nothing else in the `And`) and computes `density = count / (max - min + 1)`,
`overlap` = the query's own interval intersected with `[min, max]`, `estimate = round(density *
overlap)`, then `result = result.min(estimate)`. `fuse_and_range_children`'s `AndSource` now derives
`Copy` so the fused list can be inspected a second time without a second call. 3+ children (e.g.
`set:sld id:g cn<=100`) are out of scope this round — `and_sources.len() != 2` simply skips them,
falling back to the pre-existing fold unchanged.

**Honest limitation, not a bug.** This estimate is not a guaranteed upper bound like the mechanisms
around it (`pair_bounded_min`, `arith_tuple_count`, the `compile_plane` popcount) — for a
non-contiguous set (Secret Lair Drop, numbered per-drop rather than sequentially) it can UNDERSHOOT
the true count, a new failure mode this fallback did not have before. Accepted because it is still a
strict improvement over the alternative the fold would otherwise pick (see held-out validation
below), and it only ever narrows `result`, never touches `exact_domain_cards` (reserved for genuinely
exact answers elsewhere in this same function).

**Held-out validation, broad population (not just the 4 sets spot-checked in prior conversation).**
`validate_density_r33.py`: ground truth computed directly from the real corpus JSONL (97,811
set+cn-valued printings), independent of the engine. Every real set with >= 5 printings (550 distinct
sets), 6 sampled queries per set split across bare `Le`/`Ge` and fused two-sided shapes (3,300 total),
hash-of-query calibration/held-out split (nothing here is FIT — the formula has no free constant — the
split is a broad-population honesty check, not an overfit guard):

```
                calibration (n=1,619)          held-out (n=1,681)             pooled (n=3,300)
density  median|log ratio|  0.000              0.000                          0.000
         mean|log ratio|    0.101              0.106                          0.103
         within 25%         88.9%              88.8%                         88.8%
fold     median|log ratio|  0.788              0.793                          0.788
         within 25%         18.7%              17.3%                         18.0%
indep*   median|log ratio|  0.511              0.511                          0.511
         within 25%         30.9%              30.1%                         30.5%
```

(`indep*` = a plain independence product on the two leaves' own marginal counts — the "other obvious
idea," included to confirm the same rejection this doc's Round 2 already reached for range-vs-range
Ands also holds here: strictly worse than the density model, though still better than the fold.)

By shape (pooled): `bare_le` 90.0% within 25%, `bare_ge` 89.4%, `fused` 87.1% — both shapes the task
description called out are covered, and both land in the same range. Named spot-checks (from the
conversation-history investigation that motivated this round) reproduce exactly: `woe` (381 printings,
span [1,381], density 1.0000) → `cn<=100` estimate 100 against true 100, EXACT. `mh3` (524, span
[1,521], density 1.0058) → estimate 101 against true 100. `lea` (292, span [1,295], density 0.9898) →
estimate 99 against true 98. `sld` (2,534, span [1,9999], density 0.2534) → estimate 25 against true
104 (4.16x under) — the documented non-contiguous residual, still far better than the fold's 2,534
(24.4x over) for this exact query.

**Cost-model-agreement before/after** (`bench_cost_model_agreement.py --seconds 150 --seed 0`,
isolated release wheels, baseline = `costcell/trunk`@`4d6db48c` vs this round's fix):

```
plan / acquire                         baseline (n)         fix (n)
GatheredScan   printing_compose   median 1.19 (27,089)  1.20 (26,788)  within25% 24% both
GatheredScan   card_range_popcount median 1.07 (772)     1.09 (765)    within25% 49%/48%
PrintingCompose card_range_popcount median 1.33 (661)    1.26 (654)    within25% 39%/46%
PrintingCompose plane              median 0.88 (1,945)   0.76 (1,923)  within25% 32%/17%

plan / unique
GatheredScan   card                median 0.84 (16,479)  0.84 (16,307) within25% 26%/25%
PrintingCompose card               median 1.04 (4,953)   0.99 (4,906)  within25% 47%/43%
```

Every one of these moves by an amount consistent with two independent 150s windows sampling a
different query mix (the `PrintingCompose`/`plane` cell's 0.88→0.76 shift looks the largest, but that
acquire branch is untouched by this fix entirely — no code path connects a `set:X`+`cn` shape to
`plane` acquire, so this is sampling noise, the same pattern every prior round in this doc reports for
the pooled agreement gate: real, targeted fixes move a small held-out slice cleanly while the
pooled cell — which mixes in everything else — stays within run-to-run noise). No cell crossed a
FAIL/PASS boundary that a second baseline-vs-baseline run wouldn't also risk crossing.

**Regret matrix** (`bench_regret_matrix.py --seconds 150 --seed 0 --mode realistic`): total regret
**37.4ms (baseline) -> 33.4ms (fix)**, an 11% reduction — in the improving direction, not a
regression. `PrintingCompose -> StreamedSelect` (a nearby transition that reads the same
`compose_printing_estimate`): n 275->223, mean regret 38.10->31.10us, share 28%->21%.
`PrintingCompose -> GatheredScan`: n 148->139, mean 37.21->34.28us. `StreamedSelect ->
GatheredScan` (Rounds 30/31/32's own territory): n 1,137->1,132, mean 9.90->9.81us, share
30%->33% -- flat, confirmed unaffected below via the reproduced flip-query population directly, not
just this aggregate.

**Regression guards.**

- `#852` (`GatheredScan` vs `PrintingCompose`, `bench_pairwise_ordering.py --seconds 150 --seed 0
  --mode realistic`): 89% -> 89% (n=17,322 -> 17,038), unchanged. `GatheredScan` vs `StreamedSelect`:
  97% -> 97%, unchanged. Clean.
- Round 28's `scan_units` feature accuracy (`bench_feature_accuracy.py --seconds 150 --seed 0 --mode
  realistic`): pooled median 1.00 -> 1.00, n=134,028 -> 133,348, identical distribution shape —
  expected, this fix touches `compose_printing_estimate`'s `result`, never `scan_units` itself. Clean.
- Rounds 30/31/32's `StreamedSelect -> GatheredScan` flip-query population
  (`flip_finder_r33_validate.py`, reproducing the ORIGINAL `f3f4a017` flip set exactly as
  `flip_finder_f3f4a017.py` does, then replaying it against the `costcell/trunk` baseline and this
  round's fix): **51/95 fixed on BOTH builds, 44/95 still wrong on both, 0 regressed** — this round's
  fix is on a completely different code path (`compose_printing_estimate`'s `And` arm feature
  estimation, not `stream_scan_units`/`StreamedSelect`'s redo-bias) and confirmed, not just assumed,
  to leave that population untouched.
- Same-build latency canary (`bench_query_latency_ab.py --sample 800 --seed 1 --mode realistic`,
  isolated release wheels): real diff (baseline vs fix) `B - A = +0.6us`, 95% CI `[+0.4, +0.9]`, "B is
  SLOWER". Same-build canary (baseline vs baseline, zero code difference): `+2.3us`, CI `[+2.0,
  +2.6]`, also "B is SLOWER" — a LARGER swing with nothing changed. The real diff is not
  distinguishable from that noise floor, so read as no detectable latency effect, consistent with the
  self-check that the only added per-query work is one `HashMap` lookup gated behind a rare 2-child
  shape.

**Correctness gates.** `cargo test --release` (`card_engine`): 180/180 passed (179 + this round's new
regression test). `cargo test` (debug): 181/181 passed. `cargo clippy --all-targets -- -D warnings`
(debug, not `--release`, per this effort's established gate): clean. New regression test
(`card_engine/src/tests.rs`, `set_and_collector_number_range_density_tightening`): a synthetic
3-set corpus (`con` contiguous 1..=50, `big` contiguous 1..=100 as a corpus-wide inflator, `gap`
non-contiguous 20 printings spanning [1,999]) asserting exact expected values for the fused
two-sided shape (10), the bare one-sided shape (15), and the non-contiguous partial-improvement
shape (2) — verified to actually catch a revert (temporarily gating the tightening off with `if
false && ...` reproduces the pre-fix fold value, 20, on the first assertion; restoring passes again).
Blast radius: `card_engine/src/lib.rs` (`SetCollectorRange`, `build_set_collector_ranges`, the new
`CardIndexes` field, `AndSource`'s new `Copy` derive, the `And` arm's new tightening step),
`card_engine/src/tests.rs` (the new regression test plus one `CardIndexes` literal fixed up to
compile), this doc. `cost.rs`/`estimator.rs` untouched.

**Verdict.** Real, validated, narrow. A strict, large improvement on the specific shape it targets
(pooled median |log ratio| 0.000 against the fold's 0.788, 88.8% within 25% against 18.0%, across 550
real sets and both query shapes, not just the 4 already spot-checked) with one honest, documented
exception: a genuinely non-contiguous set (SLD) can now UNDERSHOOT where the fold used to
OVERSHOOT — still a large net improvement for that case too (2.5x under vs 24x over), but a new
failure direction this fallback did not have before. No regression found on the pooled
cost-model-agreement gate (moves within noise, as expected for a small-slice fix — the same pattern
every prior round in this doc reports), `#852`, Round 28's `scan_units` cell, or Rounds 30/31/32's
flip-query population (confirmed identical, not just unmentioned, on both builds). The regret matrix
moved in the IMPROVING direction (37.4ms -> 33.4ms, -11%) rather than staying flat, plausibly because
tightening `compose_printing_estimate`'s `result` for this shape also improves nearby
`PrintingCompose`-adjacent transitions that read the same estimate — not chased further this round
since it was not the target metric.

### Round 34

Target: the same `compose_printing_estimate` `And`-arm gap Round 33 closed for `set:X`+`cn`-range,
but for `set:X`/`c:X`/`id:X` And'd with a subtype leaf (`t:elf`, `t:human`, ...). `CollectionCmp
{Subtypes}` has no `compile_plane` arm (unlike the main card TYPES — `t:creature`, `TypeCmp` — which
already have their own whole-tree `compile_plane` fast path, untouched here) and isn't in any pair
table, so the fold picks whichever leaf's own corpus-wide count is smaller. Real example, `set:plst
t:human`: fold picks `min(set:plst's own 5,043 printings, t:human's own 10,607 printings)` = 5,043
against a true 503 — 10.0x over.

**Two corrections made mid-round, both caught by verifying against real data rather than trusting the
design as briefed — reported here because the corrected version is materially different from a
straight reading of the brief:**

1. **Card-space-only cells were the wrong shape.** A first pass built the top-256 table with a flat
   `u32` card count per cell and fed it into `compose_printing_estimate`'s `result` (printing space)
   via a `* n_printings / n_cards` conversion. That makes the estimate invisible to `unique=card`/
   `artwork`: those modes read `acquire_plan_features`'s own `est_cards`/`exact_total`, fed by
   `exact_result_total`, a function `compose_printing_estimate` never calls — so a card-mode query
   would still fall through to `calibrated_balls_into_bins`'s lossy estimate one layer down, never
   seeing the exact card count this round computed. The fix: cells are `SpaceTotals` (card/printing/
   artwork together), mirroring `PairTotals`'s own pattern for exactly this shape, and
   `exact_result_total` gets its own new 2-leaf arm reading `indexes.subtype_pairs` directly (right
   next to its existing `pair_totals` 2-leaf check) — so every `unique=` mode gets the exact answer in
   one lookup, no space conversion. `compose_printing_estimate`'s `And` arm was restructured to match:
   a table HIT now feeds `exact_domain_cards`/`_printing`/`_artworks` (mirroring `best_other`/
   `pair_range_sum`'s own pattern — `min`-ing across independently-exact intersections stays exact), and
   only a table MISS (the capped independence-product estimate, genuinely not exact) narrows `result`
   alone, the same exact/estimate line Round 33's own density model draws.
2. **`id:` is not `c:` with a different field.** The brief (and `op_to_color_cmp`, read in isolation)
   suggested every bare-colon color field defaults to `Ge` (superset). Live-query instrumentation
   during this round found otherwise: `id:g t:elf` reached the `And` arm with `op: CmpOp::Le`
   (subset), not `Ge` — confirmed by printing the actual `FilterExpr` the query produced, not by
   re-reading the parser. `c:` really is `Ge` (`c:g t:elf` reached `op: CmpOp::Ge` in the same check).
   This is a real, previously-undocumented asymmetry in this codebase's bare-colon color semantics, not
   a bug this round introduces — commander/deck-building's own "at most these colors" reading of
   `id:`, apparently implemented at the FilterExpr level though not the SQL path's own explicit
   subset-vs-superset branch this investigation also found (`card_query_nodes.py::_handle_jsonb_object`,
   a separate, legacy SQL-generation path this effort's Rust engine does not go through). `colors`
   cumulates GE (matches `color_cmp_matches(Ge, ...)`); `color_identity` cumulates LE
   (`color_cmp_matches(Le, ...)`) — two different tables, not one mirrored twice.

**Fix.** `SubtypePairIndexes` (new field on `CardIndexes`, next to `pair_totals`): `set`
(`SetSubtypeTable`), `colors`/`identity` (`ColorSubtypeTable`, one instance each). Built once at load
time (`build_subtype_pair_tables`) by reusing `build_value_totals` — the same exact card/printing/
artwork dedup logic `ValueTotals`/`PairTotals` are already built with, not a hand-rolled accumulator:
one pass crossing each printing's set against its card's subtypes (`set_subtype_totals`), one crossing
each card's raw colors/identity mask against its subtypes (`colors_raw`/`identity_raw`), plus a third
trivial pass for `set_cards` (the one marginal `set:X` needs that nothing else derives per-card;
`c:`/`id:` get theirs for free from `ComposeEstimate.result.card`). `colors_raw`/`identity_raw` are
then summed cumulatively (32 possible raw WUBRG masks, `color_cmp_matches` decides which raw cells
contribute to which query mask — GE for colors, LE for identity) into `colors_pair`/`identity_pair`.
Each of the three resulting tables keeps only the top 256 pairs by CARD count plus `rest_max` (the
largest excluded CARD count) — nested `HashMap<K, HashMap<String, SpaceTotals>>` rather than a
tuple-keyed map, so a query-time lookup is O(1) `Borrow<str>`/`u8` `.get()`s with no allocation.

Two consumers, one shared detection (`subtype_pair_dim`/`subtype_pair_leaf`/`subtype_pair_exact`):

- `exact_result_total` gets a new arm, right after its existing `pair_totals` 2-leaf check: on a table
  hit, `Some(totals.get(mode))` — exact in whichever mode the caller asked for.
- `compose_printing_estimate`'s `And` arm: on a table hit, `result = result.min(printings)` plus
  `exact_domain_cards`/`_printing`/`_artworks` all get the same triple (mirroring how `best_other`/
  `pair_range_sum` already populate those fields). On a miss, `independence_product = dim_card *
  subtype_card / n_cards` (both already in hand: `subtype_card` from the fold's own `.result.card`;
  `dim_card` the same way for `c:`/`id:`, or `subtype_pairs.set.set_cards` for `set:`), capped at
  `rest_max`, scaled `* n_printings / n_cards` (Round 33's own arith-tuple-merge/legality-arm
  conversion) — and this branch touches `result` ONLY, never `exact_domain_*`, since it is not exact.

**Verified real numbers (re-derived from `benchmarks/bitplanes/corpus.jsonl`, independent of the
engine, and cross-checked against the actual Rust build's own numbers, not just the brief's).**
`rest_max` at N=256: **set 27, colors 38, identity 377.** The first two land where the brief expected
(comfortably below the ~100-150 routing-fragile zone this investigation's diagnostic rounds
established). Identity does not: LE-cumulative has a structurally heavier tail than GE-cumulative,
because a query mask with MANY colors admits nearly every raw mask as a subset — the top ~250+
identity entries are almost entirely `(some multi-color mask, "Human")`, cards that would show up for
nearly any broad `id:` query. 377 is still well below the confirmed reversal zone (900-2000, where
over-estimating flips from risky to safe), so this is not the failure mode the brief was checking for,
but it IS a real, larger-than-expected cap for identity's independence-product fallback specifically —
reported honestly rather than silently treated as satisfying the brief's "verified safety margin"
framing. Mitigated in practice by two things this round's design already has for free: (1) the
highest-value identity pairs are exactly the ones most likely to already be table HITS (bypassing the
cap entirely), and (2) `result.min(...)` means this fallback can only narrow `result`, never widen it
past what the pre-existing fold already gave — so a large `rest_max` bounds how MUCH improvement is
possible on a miss, not a new way to regress below the fold.

**Worked examples**, `unique=printing`/`card` via `explain()`, isolated release wheel:

```
                             printing         card
set:plst t:human   true         503          486
                    fold       5,043        2,710   (10.0x / 5.6x over)
                    fix          503          486   (EXACT -- table hit)
c:g t:elf          true       1,917  (assumed exact from table hit itself)
                    fold      10,607        6,450   (5.5x / 11.5x over)
                    fix        1,917          560   (EXACT -- table hit)
id:g t:elf         true         N/A          398   (LE-cumulative, verified against raw corpus)
                    fold       2,138        7,288   (fold picks t:elf's/id:g's own count)
                    fix        1,388          398   (card EXACT -- table hit; printing not
                                                       independently re-verified beyond the engine's
                                                       own SpaceTotals internal consistency)
```

**Held-out validation** (`prepare_r34_queries.py`/`run_r34_queries.py`/`analyze_r34_results.py`, not
committed — ephemeral, matching this doc's own convention for validation scripts): every real
set with >= 5 cards (550 sets) and every real colors/identity raw mask with >= 5 cards (28 each),
crossed with up to 6 sampled subtypes each (present + absent, mirroring Round 33's "6 sampled queries
per set"), hash-of-query calibration/held-out split, ground truth computed directly from the corpus
JSONL using the SAME cumulative (GE for colors, LE for identity) relation the engine now uses — 3,635
queries total. Pooled median |log ratio| (nonzero-true rows), fold -> fix:

```
                    printing mode                    card mode
            fold        fix       within25%     fold        fix       within25%
set        3.486 -> 0.693     0.5% -> 4.6%    3.135 -> 0.693     0.4% -> 7.5%
colors     3.061 -> 1.525    11.4% -> 8.6%    3.617 -> 0.693     4.3% -> 10.0%
identity   0.658 -> 0.693    30.0% -> 22.1%   0.984 -> 0.607    13.6% -> 26.4%
pooled     3.369 -> 0.693     2.4% -> 5.7%    3.091 -> 0.693     1.2% -> 8.5%
```

`set` and `colors` improve cleanly and substantially in both modes. `identity` is the honest
exception this round's own rest_max finding predicts: its fold was already much better-calibrated
than `set`/`colors` (0.658-0.984 median vs 3.0+), so there is less room to gain, and `identity`'s
PRINTING-mode within25% actually drops (30.0% -> 22.1%) even though its median barely moves — some
small-true-count rows flip from a mild fold OVERSHOOT to a fix UNDERSHOOT of comparable or larger
|log ratio| (the same direction-flip risk Round 33's own SLD residual documents, not a new failure
mode). Identity's CARD-mode numbers improve regardless (0.984 -> 0.607 median, 13.6% -> 26.4% within
25%), and every mode stays strictly `result <= fold` by construction, so this is a real, bounded,
honestly-reported trade-off, not a silent regression.

**Regression guards.**

- `#852` (`bench_pairwise_ordering.py --seconds 90 --seed 0 --mode realistic`): `GatheredScan` vs
  `PrintingCompose` 89% -> 89% (n=10,197 -> 8,950), `GatheredScan` vs `StreamedSelect` 97% -> 97%
  (n=28,565 -> 25,177). Clean.
- Round 28's `scan_units` feature accuracy (`bench_feature_accuracy.py --seconds 90 --seed 0 --mode
  realistic`): pooled p50 1.00 -> 1.00 (n=69,908 -> 69,182), matching distribution shape (p10 0.26 ->
  0.26, p90 1.78 -> 1.76) — expected, this round touches `compose_printing_estimate`'s `result`/
  `exact_domain`, never `scan_units` itself. Clean.
- Round 33's own `set:X`+`cn`-range density check (`set:sld cn<=100`, printing mode): 25 on both
  builds, unchanged — confirmed directly, not just assumed, since both rounds are new `And`-arm
  tightenings that could in principle compose on a query hitting both shapes (rare: this round needs a
  subtype leaf, Round 33 needs a `collector_number_int` leaf, and the `And` arm's shape guards are
  each strict 2-source, so a 3-leaf query combining both falls through both unchanged, same as any
  other shape neither recognizes).
- Rounds 30/31/32's `StreamedSelect -> GatheredScan` territory (`bench_regret_matrix.py --seconds 90
  --seed 0 --mode realistic`): n 692->660, mean regret 10.26us->9.80us, share 37%->32% — flat within
  run-to-run sampling variance (n differs ~5% between the two 90s windows), not a regression; total
  regret across all transitions 19.4ms (baseline) vs 20.1ms (fix), also within this effort's own
  documented ~9% noise floor for a single non-interleaved pair of runs.
- Same-build latency canary (`bench_query_latency_ab.py --sample 800 --seed 1 --mode realistic`,
  isolated release wheels, interleaved A1/B1/A2): real diff (fix vs baseline) `B - A = 0.0us`, 95% CI
  `[-0.3, +0.3]`, "NO DETECTABLE DIFFERENCE". Same-build canary (fix vs fix, zero code difference):
  `+1.6us`, CI `[+1.3, +1.9]`, "B is SLOWER" — a LARGER swing with nothing changed, so the real diff is
  not distinguishable from noise, consistent with the only new per-query work being one or two `HashMap`
  lookups gated behind a rare, strict 2-leaf shape.

**Correctness gates.** `cargo test --release` (`card_engine`): 184/184 passed (180 + 4 new). `cargo
test` (debug): 185/185 passed. `cargo clippy --all-targets -- -D warnings` (debug, not `--release`,
per this effort's established gate): clean. Four new regression tests
(`card_engine/src/tests.rs`): `build_subtype_pair_tables_ge_le_cumulative_and_set_marginals` (the
builder's GE/LE cumulative correctness and `set_cards` marginal, hand-verified against a 4-card
fixture), `exact_result_total_answers_subtype_pairs_in_every_space` (a table hit answers exactly in
all three `Mode`s from one archived store), `subtype_pair_and_arm_tightening` (fallback beats the
fold, a hand-set exact entry is preferred over the fallback formula and populates `exact_domain_*`,
and the fallback branch does NOT populate `exact_domain_*`), `subtype_pair_and_arm_rest_max_caps_
fallback` (the cap actually binds). All four verified to actually catch a revert: temporarily gating
both new call sites off (`if false && ...`) reproduces the pre-fix fold/`None` values on the first
assertion of three of the four tests (the builder test is unaffected by construction, since it never
touches either consumer); restoring passes again. Blast radius: `card_engine/src/lib.rs`
(`SetSubtypeTable`, `ColorSubtypeTable`, `SubtypePairIndexes`, `build_subtype_pair_tables`,
`subtype_pair_dim`/`_leaf`/`_exact`, the new `CardIndexes` field, the `exact_result_total` arm, the
`And` arm's new tightening step), `card_engine/src/tests.rs` (the four new tests plus one
`CardIndexes` literal — `fuzz_store_n` — fixed up to build the real table rather than defaulting it),
this doc. `cost.rs`/`estimator.rs` untouched.

**Verdict.** Real, validated, with one honestly-reported residual: `set` and `colors` improve
cleanly and substantially (median |log ratio| from 3.0-3.6 down to 0.69-1.5, within-25% roughly
doubling to 10x-ing depending on mode); `identity` improves in card mode and is a small, bounded,
explained trade-off in printing mode, with a real rest_max (377) that this round's own
verification-over-trust discipline caught rather than assumed safe. The mid-round architecture
correction (flat card-space numbers -> `SpaceTotals` cells, mirroring `PairTotals`) and the `id:`
default-operator discovery (`Le`, not `Ge`) were both found by checking real behavior against the
brief rather than implementing the brief as given — consistent with, and validating, this whole
effort's standing "verify, don't just trust" discipline. No regression on `#852`, Round 28's
`scan_units` cell, Round 33's own density check, or Rounds 30/31/32's flip-query territory; no
detectable latency effect distinguishable from the same-build noise floor.

### Round 35

Small, targeted fix bundled with a diagnostic pass on two real user query shapes (not part of this
round's own change).

**Diagnostic: `format:modern id:g t:creature` and its `power+toughness>cmc+cmc` extension.** Real
`explain`-vs-`query` ratios on the bitplanes corpus, all three modes: both queries read exactly 1.00
in every mode -- no gap. The first is the expected case (three plane-compilable, non-existential-past-
`format:` leaves; `and_of_checked_for_shared_witness` allows it since `format:` is the only existential
leaf). The second was expected by this round's own brief to degrade -- `compile_plane`'s `NumericCmp`
arm only matches `(Field, Const)`, and the AND-tightening `is_arith_tuple_eligible` also declines a
compound `Arith` vs `Arith` comparison -- but it does NOT degrade, because `narrow_rec`'s single-leaf
NumericCmp arm dispatches through a DIFFERENT, more general gate, `is_arith_tuple_route` (`filter.rs`),
which accepts ANY `NumExpr` combination whose fields are all in `{cmc, power, toughness, loyalty}`,
compound arithmetic included. It routes to `arith_tuple_narrow`, an exact O(564)-distinct-tuple scan
via the #743 `ArithTupleIndex` (the same one `arith_tuple_count` reads), so the whole 4-leaf And
narrows to a TIGHT card set before acquire ever estimates anything. The two gates are easy to conflate
by name; they are not the same function and do not share a scope. The real caveat: `AND_SKIP_THRESHOLD`
(2,048) can skip this leaf during narrowing if an earlier child already drove the driver below that
floor, in which case the acquire-time ESTIMATE (not the final dispatched result, which the executor's
real per-printing residual always gets right regardless) would fall back to whatever the skipped
leaf's absence implies -- not exercised by either of the two real queries checked (their narrowed
domain, 2,560 cards, sits above the floor).

**Fix: `leaves_are_disjoint` gets a `set:X`/`set:Y` (x != y) arm** (`lib.rs`, alongside the existing
border/legality/rarity arms) -- a printing has exactly one `card_set_code`, so the conjunction is
unconditionally empty. The mode-scoping question this round set out to check (and a mid-task
"correction" raised again, specifically for artwork-space) turned out to have a clean answer once
checked against the actual dispatch code rather than reasoned about by analogy: `card_match_count`'s
`Mode::Card` AND `Mode::Artwork` arms (`lib.rs`) both test every child of an And residual against
ONE printing at a time (`residual_matches`/`FilterExpr::tri`) -- there is no code path where two
different printings independently satisfy two different leaves of the same And. That single-printing-
existential-over-the-whole-conjunction model is exactly what the existing `border_shared_witness_
correctness` test already exercises for `border`; `set` is structurally identical (`TextField::SetCode`
is `printing.map_or(StrVal::PDep, ...)`, the same shape as `TextField::Border`, and has no
`compile_plane` arm at all, so it always reaches this same residual path). The surface-level worry
-- illustration reuse across sets, real in this corpus (16,838 of 46,523 distinct `illustration_id`s
in `benchmarks/bitplanes/corpus.jsonl` span more than one `card_set_code`, e.g. Immaculate Magistrate's
own art across `dpa`/`lrw`/`gn3`/`cma`/`c14`/`cmr`/`ps11`) -- doesn't change the answer: the AND is
still evaluated against a single printing, so no artwork group can satisfy `set:X` via one printing and
`set:Y` via another. Confirmed directly, not just argued: `set:dpa set:lrw` (a real pair sharing that
exact illustration) reads 0 in all three modes on both builds, since the residual walk was already
correct without this function's help -- the fix only lets the acquire-time ESTIMATE say so exactly
too, instead of min-folding two individually-broad `set:` counts.

**Validation.** Regression test (`set_set_disjoint_pair_exact_in_every_space`, `tests.rs`) checks a
fixture where a card genuinely has printings in both sets (the shape a per-leaf-independent
existential would get wrong), confirmed to fail on a revert (`None` vs `Some(0)`). Held-out: 40 random
real set pairs x 3 modes (120 checks) on the bitplanes corpus -- baseline 0/120 agree (real always 0,
estimate always nonzero, up to 135x over); fixed 120/120 exact. `#852` (`bench_pairwise_ordering.py`,
30s, seed 1, `--mode realistic`): 89%/94%/97%/100%-tier ordering rates and regret magnitudes unchanged
between builds within run-to-run noise (this fix's blast radius is one new match arm gated behind the
same `PAIR_TOTALS` check the border/rarity arms already use, so it cannot touch any shape other than
two disjoint same-dimension leaves). Gates: `cargo test --release` (185/185), `cargo test` (186/186),
`cargo clippy --all-targets -- -D warnings` (clean) all pass.

### Round 36

Target: the same `compose_printing_estimate` `And`-arm gap Rounds 33-35 closed for other shapes, but
for a `t:X` CREATURE SUBTYPE leaf (`CollectionCmp{Subtypes}`) And'd with a `cmc`/`power`/`toughness`
range bound (`t:dragon power>=6`, `t:human cmc>=5 power>=5`). `t:` has no `compile_plane` arm and isn't
in any pair table (Round 34's own reasoning), and cmc/power/toughness are RANGE predicates, not the
single-value shape `SubtypePairIndexes` answers -- so this pair gets no tightening from any existing
mechanism, and the fold picks whichever leaf's own corpus-wide count is smaller. Real ratios verified
directly against the bitplanes corpus (isolated baseline build, `costcell/trunk`@`784ae9ad`): `t:dragon
power>=6` folds to 9.0x over in card mode (true 93, fold 839); `t:human cmc>=5 power>=5` folds to
28.9x over (true 106, fold 3,068) -- both larger than this round's own brief anticipated, because
common subtypes span nearly the whole stat range while flavor-coded ones (Dragon/Wurm/Giant) are
naturally concentrated.

**Correction found and fixed mid-round, before any validation was trusted:** the brief's own
population for "which cards populate a subtype's stat histogram" was `card_types` containing
`Creature` -- checked directly against `benchmarks/bitplanes/corpus.jsonl` rather than assumed, and
found wrong. 217 real cards (192 Vehicle, 25 Spacecraft, 1 Equipment -- Vehicle/Spacecraft-style
permanents that carry `creature_power`/`creature_toughness` on a plain `Artifact` type line, not a
`Creature` bit) have real stats with no `Creature` type bit at all; conversely some `Creature`-typed
cards (35 of Human's 4,265) have neither value set (likely double-faced backs or data gaps). Gating on
`card_types & TYPE_CREATURE` would have silently misranked which subtypes make the top 128 (confirmed:
the top-128 boundary shifts by exactly one subtype between the two criteria -- `Rabbit` out, `Vehicle`
in, both at 34 cards) and miscounted the occupied range for the ones it touched. Fixed before
implementation: both the ranking pass and every min/max bound use `creature_power.is_some() &&
creature_toughness.is_some()` directly, never a type-line bit.

**Design.** A dense 3-D inclusive prefix-sum cube per subtype, LOCAL to that subtype's own real
occupied `(cmc, power, toughness)` box -- not one global cube sized to the corpus's full extremes
(cmc up to 16, power up to 18, toughness up to 30, all driven by rare outlier subtypes). Verified
per-subtype: Human's own box is 9x9x10 = 810 cells (cmc/power observed 0..=8, toughness 0..=9),
Spirit's is 13x19x11 = 2,717 -- both comfortably inside the "~2,744" scale this round's brief
anticipated for the widest covered subtype (Elemental is the actual widest at ~3,300, a modest
overshoot from the brief's own estimate, reported rather than silently rounded away).

- **Table size and real-data justification.** Top 128 subtypes by real distinct-card count among
  cards with BOTH `creature_power`/`creature_toughness` present (`SUBTYPE_ARITH_TOP_N`, `lib.rs`).
  Human is largest at 4,230 real stat-bearing cards; rank 128 (`Shade`) has 34. A prior diagnostic
  pass in this session (real engine instrumentation, 720 query/mode pairs across 45 tail subtypes)
  found routing never differs between the min-fold and a hypothetical better estimate for 44/45 tail
  subtypes -- so no "tier 2" fallback was built; a miss just leaves the pre-existing fold in place,
  the same as every other shape this `And` arm doesn't recognize. The one exception, `t:forest`
  (a name collision between the rare Dryad Arbor creature and the ubiquitous basic-land subtype
  string), has only 1 real stat-bearing card in this corpus -- nowhere near the top-128 cutoff, so it
  is unaffected by this round either way. Confirmed directly on the real corpus below, not just
  argued from the ranking number.
- **Cell storage.** Each cell is a `SpaceTotals` (card/printing/artwork exact counts), mirroring
  Round 34's `SetSubtypeTable`/`ColorSubtypeTable` correction exactly: cmc/power/toughness are
  card-invariant per-card facts, but printing/artwork counts still differ from card counts due to
  reprints, so a flat card-space number would again be invisible to `unique=card`/`artwork` one layer
  down (the same class of bug Round 34's own mid-round correction caught). Built via
  `build_value_totals`, the SAME dedup logic `ValueTotals`/`PairTotals`/`SubtypePairIndexes` already
  use, crossing each top-128 subtype against its card's own `(cmc, power, toughness)` (`lib.rs`,
  `build_subtype_arith_tables`/`build_subtype_arith_box`).
- **Reserved "no value" slot.** Power/toughness axes reserve ONE extra slot at local index 0 (real
  values start at 1) for a card that carries the subtype but has no stats at all -- mostly Tribal-typed
  spells (~180 real cards across the whole top-128 population, e.g. 29 Eldrazi, 22 Elf, 18 Faerie). A
  bare `cmc` bound alone (no power/toughness constraint in the query) must still count these cards;
  excluding them from the table would make a table HIT silently WRONG (an undercount), not just
  incomplete. `cmc` gets no such slot -- verified directly that 0 of 97,812 real corpus rows have a
  null `cmc`.
- **Query-time O(1) lookup.** For each dimension: BOUND (present in the query) converts to a LOCAL
  index range; UNBOUND (absent from the query) uses the FULL local range (which, for power/toughness,
  includes the reserved slot -- an unconstrained axis places no requirement on a card's stats at all).
  The bound-to-range conversion (`arith_group_real_range`) tests every real integer in the subtype's
  own small span (at most ~30 wide) directly with `matches_op` (planes.rs's own float-comparison
  primitive, the SAME one `compile_numeric_cmp`/`numeric_candidates` use) -- deliberately NOT
  `NumericLayout`'s bucket layout, read before writing this: those buckets are sized to keep a GLOBAL,
  whole-corpus existential plane small and decline (`BucketVerdict::Ambiguous`) at their own edges by
  design, a lossy compromise a window this narrow doesn't need. The box-sum itself
  (`subtype_arith_box_sum`) is the standard 8-corner 3-D inclusion-exclusion formula over the
  prefix cube, with a `prefix_at` sentinel returning `(0,0,0)` for any negative coordinate so no edge
  needs its own special case -- genuinely O(1) at query time (8 lookups plus the bound conversion's
  bounded ~30-value scan), independent of the query's actual selectivity. Min/max short-circuit: a
  query bound with no overlap in the subtype's real range makes `arith_group_real_range` return `None`,
  which the caller (`subtype_arith_exact`) turns into an immediate exact `(0, 0, 0)` without ever
  reading `prefix`.
- **Shape guard.** Not the strict 2-source shape Round 33/34 use (`and_sources.len() == 2`): the real
  motivating example, `t:human cmc>=5 power>=5`, is a literal 3-child `And` (`t:human`, `cmc>=5`,
  `power>=5`), since nothing merges separate arith-field bounds into one `AndSource` the way
  `fuse_and_range_children` merges same-index printing-range bounds. Instead: reuses `arith_children`
  (already computed above for the pre-existing arith-tuple-count tightening) and requires EXACTLY one
  other child, which must be a subtype leaf (`arith_children.len() + 1 == v.len()`) -- covers 1-6 bound
  children across up to three fields, handling both a bare one-sided bound and a same-field fused
  two-sided bound uniformly (no `fuse_and_range_children` involvement at all, since cmc/power/toughness
  never reach that function). A genuine 2-subtype-leaf query or one mixing a subtype leaf with a Round
  34-recognized dimension (`set:plst t:human cmc>=5`) has 2+ non-arith children and correctly declines
  this shape, falling through unchanged.
- **Exact/estimate line.** A table HIT feeds `exact_domain_cards`/`_printing`/`_artworks` exactly like
  `best_other`/the arith-tuple merge/Round 34's own hit (`min`-ing across independently-exact
  intersections stays exact). There is no separate miss/estimate branch this round, per the tier-2
  finding above -- a miss leaves `result`/`exact_domain_*` exactly as every prior tightening already
  left them.
- **A second consumer, found and fixed the same way Round 34 found its own:** `compose_printing_estimate`'s
  own `exact_domain_cards` alone was NOT enough to fix card/artwork mode. Checked directly against a real
  query (`t:dragon power>=6`, `unique=card`, isolated release wheel): printing mode read exact (385/385)
  immediately, but `explain()`'s own card-mode `matches` feature still read 216 (a
  `calibrated_balls_into_bins` guess), because `PrintingCompose`'s acquire branch gets its card/artwork
  match count from `exact_result_total(composed, indexes, Mode::Card)`, a SEPARATE function
  `compose_printing_estimate` never calls -- exactly the architectural gap Round 34's own doc reports
  finding for its own shape. Fixed the same way: `exact_result_total` gets its own Round 36 arm, same
  shape guard, right after Round 34's own arm.

**Held-out validation** (isolated release wheel, real corpus, `real.store` freshly reloaded from
`benchmarks/bitplanes/corpus.jsonl`). Ground truth = `engine.query()`'s real total; the estimate =
`engine.explain()`'s `acquire.matches` feature (what `PrintingCompose`'s acquire branch, and therefore
the cost model, actually see). Independently re-derived the top-128 ranking in Python from the corpus
JSONL (same stats-presence criterion) to sample from, rather than trusting the Rust build's own
ranking: 512 random queries across all 128 covered subtypes (calibration/held-out split by
`hash(subtype) % 2`, 1-3 dimensions, mixed `>=`/`<=`/`=` operators, all three `unique=` modes):

```
                    n     exact (ratio == 1.00)
calibration       280     280 (100.0%)
held-out          232     232 (100.0%)
pooled            512     512 (100.0%)
tail (rank 100-128) 87     87 (100.0%)
```

100% exact everywhere, as expected for a genuinely exact mechanism inside a covered subtype's occupied
range (this is not an approximation being graded for closeness -- a miss would show up as a clean
disagreement, and none did). Worked examples, `unique=card`, isolated release wheel, baseline
(`costcell/trunk`@`784ae9ad`) vs this round's fix:

```
                                          printing          card          artwork
                              true    fold   fix     fold   fix     fold   fix
t:dragon power>=6             385    1513   385  9.02x  93   1.00x  6.26x 174  1.00x
                                     3.93x  1.00x
t:human cmc>=5 power>=5        244  5744   244  28.94x 106  1.00x  28.51x 142  1.00x
                                     23.54x 1.00x
t:wurm power>=7                 86   293    86  5.29x   31  1.00x  5.73x  37  1.00x
                                     3.41x  1.00x
t:giant toughness>=6            201   632   201  6.19x   57  1.00x  5.13x  89  1.00x
                                     3.14x  1.00x
t:elf power<=1                  725  2138   725  6.02x  196  1.00x  5.00x 307  1.00x
                                     2.95x  1.00x
t:human cmc=3                  3352 10607  3352  4.11x 1323  1.00x  4.23x 1724 1.00x
                                     3.16x  1.00x
t:zombie cmc>=4 power>=3 toughness>=3  448 1649  448  5.50x  166  1.00x  5.79x 205 1.00x
                                     3.68x  1.00x
t:forest power>=1                 7  1196  1196 665.00x 665 665.00x 287.33x 862 287.33x
                                     170.86x 170.86x
```

(Read each cell as fold-ratio above, fix-ratio below.) Every covered subtype goes from a real 3-29x
over-estimate to EXACT in every mode. `t:forest` is confirmed genuinely unaffected: byte-identical
fold/fix values in every mode (1,196/665/862), since it is absent from both builds' tables the same
way.

**Real routing/regret confirmation, not just isolated ratios.**

- `bench_pairwise_ordering.py --seconds 60 --seed 0 --mode realistic`: `GatheredScan` vs
  `PrintingCompose` 89% -> 89% (n=7,121 -> 6,776), `GatheredScan` vs `StreamedSelect` 97% -> 97%
  (n=20,040 -> 18,939). Unchanged within run-to-run noise, matching the pattern every prior round in
  this doc reports for a narrow, gated fix.
- `bench_regret_matrix.py --seconds 60 --seed 0 --mode realistic`: `StreamedSelect -> GatheredScan`
  (Rounds 30-32's own territory): n 438->435, mean regret 10.33us->9.58us, share 34%->33% -- flat
  within noise. `printing_compose / card` (the acquire this round's own fix touches): mean regret
  1.95us->1.84us -- moves in the improving direction, consistent with `t:dragon power>=6`/`t:human
  cmc>=5 power>=5`-style queries now costing `PrintingCompose` correctly instead of overcosting it via
  an inflated match estimate.
- Confirmed no conflict/double-counting with Round 34's own tables directly: a 3-leaf query combining
  both rounds' target shapes (`set:plst t:human cmc>=5`) matches NEITHER tightening (still folds at
  38-70x over in both modes) -- a real, verified gap for a possible future round, not silently
  mis-handled by either round's own shape guard colliding.

**Same-build latency canary** (`bench_query_latency_ab.py --sample 800 --seed 1 --mode realistic`,
isolated release wheels, interleaved A1(baseline)/B1(fix)/A2(baseline)): the first pairing read `B -
A = +1.8us`, CI `[+1.4, +2.1]`, "B is SLOWER" -- looked real at first glance. Checked rather than
trusted: a SECOND same-build canary (B1 vs a freshly rebuilt B2, the FIX build against itself, zero
code difference) read `-2.8us`, CI `[-3.1, -2.5]`, "B is FASTER" -- a LARGER swing with nothing
changed, and in the opposite direction from the first pairing. A third pairing (A2 vs B2) read `-0.9us`,
"B is FASTER", also disagreeing in sign with the first. Three pairings of the same two builds giving
three different verdicts is the noise floor this doc's own established caveat describes, not a real
per-query cost: the only code added on a non-matching query's path is a few O(1) branches and an O(children)
scan already paid for by the pre-existing arith-tuple-count tightening's own `arith_children` vector
(reused, not recomputed), and `subtype_arith_exact`'s one `HashMap<String, _>` lookup is gated behind a
shape check that most queries never satisfy. Read as no detectable latency effect, not a regression.

**Correctness gates.** `cargo test --release` (`card_engine`): 189/189 passed (185 + 4 new). `cargo
test` (debug): 190/190 passed. `cargo clippy --all-targets -- -D warnings` (clean). Four new regression
tests (`card_engine/src/tests.rs`): `subtype_arith_prefix_sum_matches_brute_force` (the 8-corner
inclusion-exclusion formula against an independent brute-force sum over hand-built cells, across 7
ranges chosen to exercise every corner term including a non-boundary asymmetric box), `subtype_arith_and_arm_tightening`
(a real `t:dragon`-shaped fixture: a 2-child hit, a bare-cmc query landing only on the reserved
"no value" slot, a 3-child `cmc>=5 power>=5` hit, and an out-of-range exact zero via the short-circuit),
`subtype_arith_and_arm_miss_leaves_fold_unchanged` (a subtype absent from the table -- the same
mechanism that keeps the real `t:forest` case safe -- leaves the pre-existing fold untouched),
`exact_result_total_answers_subtype_arith_in_every_space` (the second consumer found above, all three
`Mode`s from one archived store). All four verified to actually catch a revert: gating the new call
site off (`if false && ...`) reproduces the pre-fix value on the first assertion; restoring passes
again. Blast radius: `card_engine/src/lib.rs` (`SubtypeArithBox`, `SubtypeArithIndexes`,
`build_subtype_arith_tables`/`build_subtype_arith_box`, `subtype_arith_box_sum`,
`arith_group_real_range`, `subtype_arith_exact`, the new `CardIndexes` field, the `And` arm's new
tightening step, `exact_result_total`'s new arm), `card_engine/src/tests.rs` (the four new tests plus
two `CardIndexes` literals fixed up to build the real table), this doc. `cost.rs`/`estimator.rs`
untouched.

**Verdict.** Real, validated, exact where it applies. 100% exact (512/512, calibration and held-out
both clean) across every one of the top-128 covered subtypes, turning a real 3-29x over-estimate into
the true answer in every `unique=` mode for the "big creature" shapes this whole investigation started
from. `t:forest` confirmed genuinely unaffected (byte-identical fold/fix). Two things caught by
verifying against real data rather than trusting the design as given, both before any validation
number was produced: the ranking population (`card_types & TYPE_CREATURE` vs. real stats presence --
217 Vehicle/Spacecraft cards would have been silently mis-scoped) and the second consumer
(`exact_result_total`, the same gap Round 34 found for its own shape). No regression on `#852`,
Rounds 30-32's `StreamedSelect -> GatheredScan` territory, or Round 34's own subtype-pair tables
(confirmed no conflict on a query combining both rounds' shapes); no detectable latency effect
distinguishable from the same-build noise floor, itself checked rather than assumed clean.

## Confirmation runs

Round 1 (match-density depth proxy, kept):

- `bench_regret_matrix.py --seconds 120 --mode uniform`: no anomalous transition — regret concentrates
  where it already did (`printing_compose` 96% of share, `StreamedSelect -> GatheredScan` /
  `GatheredScan -> PrintingCompose` the largest picked/best mismatches), nothing resembling the
  historical 23.6x acquire-time blowup.
- `bench_query_latency_ab.py --mode realistic --sample 2000 --seed 1`, baseline vs modified: `+0.4us`
  mean, 95% CI `[+0.2, +0.6]`, "B is SLOWER". A same-build canary (baseline vs baseline, same
  protocol, nothing changed) produced `-0.3us`, CI `[-0.4, -0.2]`, "B is FASTER" — i.e. a swing of the
  same sign and magnitude with zero code difference, matching this script's own documented
  non-interleaved-run drift artifact. The real diff is not distinguishable from that noise floor, so
  read as no detectable latency regression, not confirmed-safe by a wide margin.

Round 9 (`GATHER_FIXED_COST_ZERO_MATCH_NS`, kept):

- `bench_regret_matrix.py --seconds 60 --seed 0`, baseline vs modified: total regret 27.6ms both
  builds (18,181 vs 18,349 multi-plan queries, wall-clock-budget variance); no new `picked -> best`
  mismatch row — including for the RANGE_ACQUIRES gate-precision risk this round found and checked
  directly (see Round 9 above).
- `bench_query_latency_ab.py --mode realistic --sample 800 --seed 1`, interleaved A1/B1/A2, baseline
  vs modified: `-0.3us` mean, 95% CI `[-0.5, -0.1]`, "B is FASTER". Same-build canary (A1 vs A2):
  `-0.2us`, CI `[-0.4, -0.1]`, also "B is FASTER" — a swing of the same sign and comparable magnitude
  with zero code difference. The real diff is not distinguishable from the canary's noise floor, so
  read as no detectable latency effect either way — expected, since this is a routing-accuracy fix
  for a rare zero-match slice, not a hot-path rate change.
- `bench_cost_model_agreement.py --seconds 300 --seed 0`, baseline vs modified: `GatheredScan/candidates`
  n=38,435→38,889, median 0.57→0.77, p10 0.25→0.47, p90 0.89→1.98, within-25% 11%→30% (still FAIL by
  the median bar, 0.77 < 0.8, but the largest single-round movement of this cell since Round 0).
  `GatheredScan/printing_compose`, `/printing_range_scan`, `/card_range_popcount` all unchanged within
  noise (see Round 9 above for the exact before/after). 12/17 acquire-branch cells inside [0.8, 1.25]
  both builds; by-unique table improves 9/12 → 10/12 (`GatheredScan/card` flips FAIL 0.69 → PASS 0.80).

Round 28 (`Mode::Card`-scope both broad-guard scan-units scales, kept): see the full "Round 28"
narrative above for the bisection, mechanism, before/after numbers (`main` 1.00, unfixed tip 0.70,
fixed 0.94), and confirmation-pass results (regret matrix, cost-model agreement, latency A/B with
canary, pairwise ordering) — all inline there rather than duplicated here.

### Round 37

Target: not another leaf-pair-shape fix — durable measurement infrastructure for the N-way `And`
composition arc, per
[local-engine-nway-compose-independence-search.md](local-engine-nway-compose-independence-search.md).
Every round from 33 through 36 had needed to answer "which mechanism actually produced this query's
estimate, and what were its inputs" and answered it with throwaway, env-gated `eprintln!`
instrumentation (`CARD_ENGINE_ROUND35_DEBUG`-style, OS-level stderr-fd capture from Python) built
fresh per round and discarded after. This round replaces that pattern with two permanent pieces:

**`and_trace`** (`lib.rs`): a new field on `explain()`/`explain_analyze()`'s acquire dict, populated
only for a top-level `And` node. Shape: `{"tree": <node>, "considered": [...]}`. `tree` is recursive —
`{"kind": "leaf", "expr": ..., "card"/"printing"/"artwork": ...}` for a direct child's own solo
estimate, or `{"kind": "op", "op": "min_fold"|"joint_lookup"|"independence", "mechanism": ..., 
"children": [...], ...}` for a combining step, with every node carrying its own three-space numbers
(the root's own numbers ARE the arm's final answer — no separate top-level "final" field to keep in
sync). `considered` lists every 2-or-3-child combination the arm's EXISTING fixed sequence of checks
actually attempted, hit or miss — a `hit: false` entry is itself the finding ("this combination was
considered and no mechanism covered it"), and a `hit: true` entry that never reaches `tree` is also
informative (today's fixed-sequence logic takes whichever check fires first, not necessarily the
tightest one available — confirmed directly: a fixture where both `arith_tuple_count` and
`SubtypeArithBox` hit the identical value shows both in `considered`, with only the first-evaluated
one winning the tree). Deliberately built as a tree, not a flat "winning mechanism" tag: a flat tag
can only describe one level, and cannot say "this pair tightened via table A, that pair via table B,
the rest min-folded" — the shape a real partition-search build needs room for. Scoped to the
OUTERMOST `And` only (no nested `And`-within-`And` recursion) — the population every curated shape
this harness generates actually needs.

**`scripts/nway_estimate_truth_survey.py`**: a checked-in, deterministic, curated-leaf-shape query
generator (every known-safe/unsafe pair and verified triple from the design doc, a same-family-twice
supplement for `set:X set:Y`/`t:X t:Y` that `QuerySampler` cannot draw on its own since one predicate
per family per query is a hard rule, an OR-rooted baseline slice, and a broad/pathological N=1..8
catch-all), measuring both `engine.explain()`'s cheap estimate and `engine.explain_analyze()`'s real
`result_total` in all three spaces per query, with a `--compare` mode for diffing two isolated builds
and a `--report` mode for a single run. **Primary metric is plan-choice agreement** (`explain()`'s own
`picked` bool, free — no extra engine call), not raw ratio: a ratio of predicted=1 against true=0
reads as "infinitely wrong" yet is completely benign, and predicted=29,000 against true=31,000 reads
as "6.9% off" and is ALSO completely benign, for the same reason — neither is near a threshold that
would change the router's pick. Ratio is graded second, floored at `true_total >= 100`
(`bench_feature_accuracy.py`'s own precedent), as a diagnostic for locating where the estimator is
loose, not the success bar.

**Two real bugs found and fixed before trusting any output, both by actually running the harness at
scale rather than trusting a hand-picked smoke test:**
1. `and_trace_for`'s only check was `matches!(filter, FilterExpr::And(_))` — any `And` wrapping a
   non-`is_printing_composable` child reached `compose_printing_estimate(..., want_trace: true)` and
   hit its `unreachable!()` guard. Found running the harness at `--n-per-shape 30`: `is:bear` parses
   to a single-child `And([CollectionCmp { field: IsTags, op: Eq, .. }])` — `Eq`, not `Ge`, so
   `is_printing_composable`'s own `CollectionCmp` arm excludes it — and crashed `explain()` outright,
   breaking its documented "safe to call constantly" contract for any tag-only query (`is:`/`keyword:`
   predicates are common real traffic, `client/query_sampler.py`'s own `tag` family). Fixed by adding
   the same `is_printing_composable` gate every other caller of `compose_printing_estimate` already
   respects; regression test constructs the exact non-composable shape directly and asserts `None`
   rather than a panic.
2. `worst_cell_tables` (the harness's own report renderer) reused `costbench.BY_MISCALIBRATION`,
   which ranks by `abs(log(median))` — correct for a RATIO centered at 1.0, wrong for `abs_log_ratio`,
   which is already a non-negative distance centered at 0. A median of exactly 0.00 (perfect) produced
   `log(~1e-9)`, a large negative number, sorting a PERFECT cell as the single worst one. Confirmed
   directly against this survey's own first full run: `unsafe:color+type` (median 0.00) sorted ahead
   of `same_family:type+type_realistic` (median 1.12, a real ~3x typical overestimate). Fixed with a
   local rank function (raw negative median, no extra log).

**First full sweep** (53,778 rows, `--n-per-shape 300 --seed 0`, all 88 curated shapes × 3 spaces):
overall zero-true-count hit rate 76.6% (the estimator correctly recognizes ~3/4 of genuinely-empty
compositions as zero), but re-sliced by the MAGNITUDE of the false positives — the metric that
actually matters, since a false positive predicting 1-30 is routing-irrelevant — a real subset
predicts hundreds to low thousands for a truly-zero answer, concentrated in `subtype_cube:*` (Round
36's own domain, median 635-654, an expected top-128 boundary effect, not a regression), the two
verified-unsafe triples (`color+legality+type`/`color+type+cmc`, median 237-347), and
`unsafe:keyword+type` (median 134). Separately, `color:X`/`id:X`/`cmc<op>N` paired with a price
comparison came back with 0% mechanism coverage across the board AND the worst median `abs_log_ratio`
of any shape in the survey (`same_family:type+type_realistic`, i.e. `t:X t:Y`, was nominally worse on
raw median, but checked against real wild-traffic frequency — see Round 38 below — and found to be
~0.02% of real queries, not worth a dedicated mechanism). This is the population Round 38 acted on.

Wild-traffic check (a real, if secondary, finding worth recording): genuinely AND'd two-subtype
queries (`t:X t:Y`, no `or`) are 3 of 14,473 real queries in `benchmarks/wild-queries/wild-corpus.jsonl`
(0.02%), confirming the shape is real but not worth a dedicated `n²/2` pairwise table — if it's ever
worth fixing, `indexes.subtypes` (already a `HybridTagIndex`: a stored bitmap per common value, a
posting list per rare one) supports an exact live AND+popcount of any two subtypes' own existing
bitmaps at zero new storage cost, cheaper than precomputing for a case that's essentially not real
traffic.

Blast radius: `card_engine/src/lib.rs` (`AndTrace`/`AndTraceLeaf`/`AndTraceGroup`/`AndTraceNode`,
`and_trace_for`, `and_trace_build_tree`, the `And` arm's existing checks each annotated to push a
`considered` entry, `compose_printing_estimate`'s new `want_trace: bool` parameter — `false` at
every production/recursive call site, `true` only from `and_trace_for`), `card_engine/src/filter.rs`
(`Debug` derives on `FilterExpr` and everything it nests, for the trace's `expr` strings — no
hand-written pretty-printer), `card_engine/src/tests.rs` (three new tests), `scripts/costbench.py`
(`ACQUIRE_KEYS` schema update), `scripts/nway_estimate_truth_survey.py` (new file). `cargo test`: 193
passed (192 + 1, across the crash-fix commit). `cargo clippy --all-targets -- -D warnings`: clean.

### Round 38

Target: the worst-accuracy shape Round 37's first sweep found — `color:X`/`id:X`/`cmc<op>N` paired
with a price comparison (`usd`/`eur`/`tix`), 0% mechanism coverage. Directly verified (not assumed):
in the example that motivated this round, `c:ruw usd:0.17` folds to 454 (the color leaf's own count)
against a true 1 — but BOTH leaves are individually EXACT on their own (`c:ruw` alone: predicted 454,
true 454; `usd:0.17` alone: predicted 1541, true 1541). A textbook "both marginals right, naive
min-fold wrong" case: the error is 100% in the fold combinator taking whichever leaf's count happens
to be smaller, not either leaf's own estimate.

**The fix**: `min(fold, independence)`, `independence = round(count(a) * count(b) / domain)`,
computed per space (`n_printings`/`n_cards`/`n_artworks`), for exactly one pair shape: one of
`color:`/`id:`/`cmc<op>N` against exactly one price-field comparison. Calibrated directly against 610
real (query, unique) rows across the three shapes (`safe:color+usd`/`safe:identity+usd`/
`safe:cmc+usd`, drawn from Round 37's own harness): replacing the fold with `min(fold, independence)`
drops the combined median `\|log(estimate/true)\|` from 0.88 (~2.4x typical error) to 0.07 (~7%), an
improvement on 578/610 rows (94.8%), a regression on 27/610 (4.4%, concentrated almost entirely in
`cmc+usd`'s own undershoot-prone tail).

**The fudge-factor question, settled with data, not intuition**: before implementing, a grid search
over a multiplicative bias (`fudge × independence`, `fudge` from 1.0 to 2.0 in steps of 0.05) was run
against the same calibration data, per-shape and combined, to test the intuition that biasing the
estimate upward would be a safe hedge against the known undershoot tail. Result: `fudge = 1.0` (no
bias at all) minimizes BOTH median and mean error for every shape individually and combined — every
increase in fudge monotonically makes things worse, even for `cmc+usd`'s own tail (the signed
distribution of `log(independence/true)` is roughly symmetric around 0 for `identity+usd`/`cmc+usd`,
and already slightly biased toward OVER-estimating for `color+usd` — a uniform upward nudge would
have made the already-good majority worse to fix a minority tail). Shipped as plain `min(fold,
independence)`, no bias term.

**Two real correctness traps checked directly, not assumed:**
- A two-sided SAME-field price range (`usd>=1 usd<=5`) reaches this check already fused into one
  exact `FusedRange` by `fuse_and_range_children` (called unconditionally, `sparse_only: false`) —
  confirmed by reading that function, not assumed; the price-family count is read post-fusion
  (`and_sources`), so this case was never at risk of silently using only one side's bound.
- `Cmc` is NOT one of the fields `fuse_and_range_children` fuses (only price/collector-number/date/
  year are printing-range-indexed) — a two-sided cmc bound (`cmc>=2 cmc<=5`) reaches this check as two
  literal, unfused children. Handled by combining them via the existing `arith_tuple_count` scan (its
  exact JOINT card count, scaled to printing the same average-case way the pre-existing arith-tuple
  tightening already does) rather than pairing on one side alone. Two-or-more literal `color:`/`id:`
  leaves of the same field have no equivalent combining table and are dropped from consideration
  entirely (no unit pushed) rather than guessed at.

**and_trace**: the first real use of the `"independence"` op value Round 37's tree schema reserved
for it (`mechanism: None` on this variant — the op name alone already says what happened, unlike
`joint_lookup`'s several named table/scan mechanisms). `scripts/nway_estimate_truth_survey.py`'s
`tree_mechanisms()` updated to bucket an `"independence"` node under the literal string
`"Independence"`, so the harness's own `and_mechanism` bucketing field picks it up.

**Verification, independently re-run end to end (not just the implementing agent's own report)**: a
fresh isolated-wheel `--n-per-shape 300 --seed 0` sweep, before vs. after, `--compare`d directly.
Plan-choice agreement: 454 flips of 53,766 shared observations, 452 inside the three target shapes
(all toward `GatheredScan` — expected, once cardinality is predicted correctly instead of wildly over),
2 incidental elsewhere (an eligible pair embedded inside a larger random conjunction); `root=leaf`/
`root=or`: 0 changes, confirming the fix stayed scoped to the `And` arm. Per-shape-per-space median
`abs_log_ratio` (before → after): `color+usd` printing 1.105→0.025, card 0.990→0.195, artwork
1.112→0.171; `identity+usd` printing 0.925→0.053, card 0.948→0.247, artwork 0.947→0.177; `cmc+usd`
printing 0.524→0.114, card 0.518→0.213, artwork 0.537→0.164 — every mode improves substantially,
though less tightly for card/artwork than printing. This was worth checking directly rather than
assuming: `result` (what this mechanism tightens) is a printing-space-only local in this part of the
function, and `exact_domain_cards`/`exact_domain_artworks` — the fields that would need to change for
card/artwork mode's OWN `matches` feature to improve — are populated only by genuinely exact
mechanisms elsewhere in the arm, never touched by this one. Card/artwork mode's real improvement
therefore comes entirely from whatever downstream scaling already converts a tightened printing
estimate into a card/artwork one for shapes with no dedicated card/artwork mechanism — the same path
every other estimate-only (non-exact) tightening in this arm already relies on, not something new
this round added.

Blast radius: `card_engine/src/lib.rs` (`numeric_cmp_field`, `is_price_num_field`, the `And` arm's new
tightening block, `AndTraceNode`'s `"independence"` op), `card_engine/src/tests.rs` (three new tests),
`scripts/nway_estimate_truth_survey.py` (`tree_mechanisms()` update). `cargo test`: 196 passed (193 +
3). `cargo clippy --all-targets -- -D warnings`: clean.

### Round 39

Target: not another accuracy fix — a permanent cost baseline, ahead of building the general
partition-search estimator the design doc describes (still entirely unbuilt; Rounds 33-38 are all
one-more-hand-written-branch, not the general machinery). Before that engine exists, we want a real
per-query nanosecond number for what today's fixed-sequence `And` arm already costs, so a future
round can answer "is the general engine's tax small" with a real before/after comparison instead of
an assumption.

**Where the timer goes matters.** `compose_printing_estimate` is called from six places. Only ONE is
the real, production, acquire-time cost every `printing_compose`-routed query already pays for
routing: `lib.rs:13833`, inside `acquire_plan_features`'s own `PrintingCompose` branch (confirmed by
reading the enclosing function, not assumed). The other five are out of scope and deliberately
untouched: `and_trace_for`'s diagnostic-only duplicate call (timing it would measure `explain()`'s
own extra overhead — it calls the estimator twice — not what production pays); three recursive
per-child calls nested inside whichever outer call started the recursion (instrumenting these too
would double/triple-count the same wall-clock time); and `compose_gather_declines`'s dispatch-time
decline check (real production cost, but a conditionally-reached narrower population, not the
acquire-time routing decision every query goes through).

**Deliberately single-shot, not multi-trial.** A lone `Instant::now()`/`elapsed()` pair is noisy for
a fast operation (`Instant::now()`'s own ~10-40ns overhead can be a real fraction of a plain
min-fold's true cost), which is normally why this codebase repeats trials and takes a min
(`explain_analyze`'s whole discipline). Not needed here: the target question is an AGGREGATE
distribution across thousands of queries (`nway_estimate_truth_survey.py` already runs at that
scale), where per-call jitter washes out in the percentile view the same way `costbench.py`'s
`percentile`/`spread` machinery already tolerates per-observation noise elsewhere. Multi-trial
repetition would answer a different question (one query's precise cost) that isn't the one being
asked, at a real cost to the "safe to call constantly" property `explain()` is documented to have.

**Real distribution** (53,778-row sweep, `--n-per-shape 300 --seed 0`): median 750ns, p90 4,416ns,
p99 11,625ns, mean 1,644ns. Populated (`Some`) on exactly 31,864/53,778 rows (59.3%) — verified this
count matches `count_source == "printing_compose"` exactly, confirming the `None`-means-"branch
didn't run" contract holds precisely (not "ran in 0ns", a real distinction a caller could otherwise
get wrong).

**The methodology this repo's own rule requires, applied to itself.**
`.claude/rules/benchmark-methodology-review.md` requires a dedicated paired A/B for any addition to
a path this doc documents as hot, not an assumption that "it's just an `Instant::now()` pair,
obviously cheap." Two isolated release wheels (`costcell/trunk` before vs. this branch after),
`bench_query_latency_ab.py --mode realistic --sample 400`, interleaved, WITH same-build canaries on
both sides. Result: the real A/B read `+1.3µs`/`+2.5µs` across two replicates, but the same-build
canaries (before-vs-itself, after-vs-itself) independently showed `+0.8µs`/`+2.0µs` of pure
run-position drift with zero code difference, and a swapped-order replicate flipped the "effect"'s
sign entirely. Honest conclusion: **not distinguishable from the canary's own noise floor** — no
measurable overhead is claimed for this addition, which is the correct thing to report when that's
what the data actually shows, not a forced "safe" verdict.

Blast radius: `card_engine/src/lib.rs` (`acquire_plan_features`'s return tuple gains a fourth
element, `AcquireFacts::and_estimate_ns`, the one new call-site timer), `card_engine/src/tests.rs`
(one new test asserting `Some` on the `PrintingCompose` branch and `None` on a control query with no
`is_printing_composable` arm at all), `scripts/costbench.py` (`ACQUIRE_KEYS` schema update),
`scripts/nway_estimate_truth_survey.py` (row schema gains `and_estimate_ns`). `cargo test`: 197
passed (196 + 1). `cargo clippy --all-targets -- -D warnings`: clean.
