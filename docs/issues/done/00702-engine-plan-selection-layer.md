# Consolidate Plan Selection Into a Single Layer

[#702](https://github.com/jbylund/sylvan_librarian/issues/702)

The engine already makes cost-based execution decisions — it just makes them
in a growing decision tree scattered through `run_query` (lib.rs ~3976),
each branch gated on a conjunction of preconditions. This issue proposes
folding that tree into one explicit plan-selection layer driven by **cheap
cardinality estimates**: estimate how many rows each candidate plan would
touch, cost each, pick the cheapest. Structurally a planner; deliberately
**not** a Selinger-style optimizer (see Non-goals).

## Why now

Not because anything is broken — because the branch count is at the edge
where the *next* fast path starts having to reason about interactions with
the previous ones. Today's fast paths (in `run_query` evaluation order):

1. **`printing_range_fastpath`** — `unique=printing` ∧ `plane.is_none()` ∧
   non-empty store ∧ a recognized bare broad range predicate.
   (local-engine-sorted-range-fastpath.md, #695)
2. **Plane-bitmap popcount-skip** (`run_query_streamed_popcount`) —
   `filter == True` ∧ `Mode::Card` ∧ a precomputed sort permutation exists ∧
   `perm.len() == cards.len()`. (#634 Step 2)
3. **Narrowed candidates** vs full scan — `narrow_candidates_exact`, then the
   "broad list narrows nothing" cutoff (`len < cards.len() - cards.len()/8`,
   a hardcoded 7/8 selectivity guess), plus the plane-∩-candidates
   composition (direct `CardBits` AND vs materialize-and-retain).
4. **Streamed selection** (`run_query_streamed`) vs gathered path —
   `maybe_broad` (`candidates > STREAM_MIN_MATCHES`) ∧ sort permutation
   exists ∧ `perm.len() == cards.len()`.

Each precondition set is hand-maintained and pairwise-disjoint *by
construction*, not by any checked invariant. Two concrete smells:

- The 7/8 cutoff in (3) is the one place we *guess* a selectivity threshold
  instead of reading a size — it's a plan choice masquerading as a filter
  step.
- (2) and (4) both want "a sort permutation exists" but diverge on the
  filter shape; (1) and (4) both handle broad range predicates but in
  different modes. The preconditions already brush against each other.

The trigger we're pre-empting: the first fast path whose preconditions
*overlap* another's and would pick a different (or wrong) plan, or the Nth
fast path that can't be added without re-reading the prior N−1.

## The key realization: routing on cheap estimates, not materialized counts

> **What landed (see Status below):** this section is the original design aspiration.
> The estimator shipped **unwired** — routing uses *exact* counts (a plane's popcount, a
> range's `k`) where they are cheap and *materializes* (`prepare_candidates`) otherwise. An
> O(1) estimate was deliberately not swapped in, to keep the cost model's inputs exact. The
> "route on cheap estimates" vision here is a deferred step, not the mechanism that shipped.

The reason today's decisions are staged (fast path 2 must return *before*
candidate materialization — the general path handles that query correctly
but pays an O(candidates) counts-buffer fill it skips, lib.rs ~4013) is that
they key off the **materialized** candidate count. But the *cardinality* of
most leaf predicates is available far more cheaply than the set itself:

- postings list → `len()`, O(1)
- range predicate → the two `partition_point` binary searches already
  compute `k = e − s`, O(log n) — `range_narrowed` discards this today
- negation → `N − c(P)`, exact given the child
- plane → popcount, O(words)
- **un-indexable text `contains`/regex → genuinely unknown, `[0, N]`** — the
  one gap, and exactly the predicate class that forces a scan anyway

If routing keys off a cheap *estimate* rather than the materialized count,
every plan decision moves ahead of materialization, materialization becomes
a *consequence* of the chosen plan, and the staging constraint that
prevented a single `choose_plan` dissolves. This is safe because **plan
choice is a pure performance decision** — every plan returns identical rows —
so a wrong estimate only ever costs latency, never correctness, and every
wrong guess is bounded by O(N), the query's own cost.

## Cardinality estimation (the routing input)

Compound cardinality from cheap leaf counts, over a universe of size `N`:

```
AND(c₁..cₖ):  lo = max(0, Σcᵢ − (k−1)·N)   hi = min(cᵢ)   est = N·Π(cᵢ/N)
OR(c₁..cₖ):   lo = max(cᵢ)                 hi = min(N,Σcᵢ) est = N·(1−Π(1−cᵢ/N))
NOT(c):       c' = N − c   (bounds invert)
```

- `lo`/`hi` are **sound bounds** (materialized truth always lands inside);
  `est` is the independence point estimate. The point estimate optimizes the
  common case; the bounds are the safety rail for decisions where a wrong
  guess is *expensive* rather than merely suboptimal (most sharply, the
  idea-1-vs-idea-2 crossover in local-engine-broad-range-fastpath.md: gate
  idea-1's near-unbounded permutation walk on the *lower bound* of the match
  count, fall to idea-2's bitmap when the bound can't rule the pathology out).
- **Independence is the known lie** (`t:creature pow>3`, `c:g` vs `id:g`).
  The bounds stay valid regardless of correlation, so they cover us; a small
  table of known structural correlations is a later refinement.

### Two spaces

Postings/ranges are printing-space; planes and the `unique=card` answer are
card-space. Composition is valid in any *single* universe, but **projection
does not distribute over AND/OR** — `distinct_cards(A∩B)` is not a function
of the card counts of `A` and `B`, so you cannot maintain both spaces through
the recursion. The natural fit for the existing plane/residual split
(`split_planes` already pulls planes into `plane: Option<&PlaneExpr>`):
compose the **residual** in printing-space (exact, cheap), project once via
`printing_to_card` (#690), then combine with the plane's exact card-space
popcount in card-space at the top.

Card-space projection is `COUNT DISTINCT` (O(k) in general), so it's an
**adaptive three-tier** thing, sized by `k`:

0. **free** — global-ratio scale `d̂ ≈ k·N_cards/N_printings` (correct on
   average, wrong under fan-out correlation)
1. **sampled** — map `s` sampled `[lo,hi)` entries via `printing_to_card`;
   note linear extrapolation `d_sample/f` is **biased high** (multi-printing
   cards over-counted), tolerable because plan choice is perf-only
2. **exact** — below a crossover `K`, project all `k`; `printing_to_card` is
   monotone, so on sorted ids this is a hash-free transition count

`K` (exact/sample crossover) and `s` (sample size) are calibration knobs,
same shape as the existing guards, deferred to measurement.

Whether routing ultimately consumes card-space, printing-space, or both is
still open (see Open questions) — the estimator produces both; which one
drives each decision is decided when we wire it.

## Non-goals

Still **not** a classical cost-based optimizer:

- **No join-order search.** Single-table selection + sort + paginate. The
  plan space is shallow and enumerable by hand — a fixed handful of physical
  plans, not a search over trees.
- **No statistics/sampling subsystem for *acceptance rates*.** Leaf
  *cardinalities* we estimate cheaply (above); but the acceptance rate of a
  non-set residual predicate (`o:flying`) mid-walk stays unknown, as
  `or_child_key` documents. Verifier child ordering still costs by tier ×
  domain, not by predicted acceptance. A build-time per-field selectivity
  table is a separate future item, not part of this.

## Cost model & gold-standard validation

The routing decision is `plan = argmin_plan cost(plan | cardinality)`, not a
pile of hand-tuned thresholds — the estimator supplies the cardinality, a
**cost model** turns it into a predicted cost, and the cheapest plan wins.
This reframes "is the estimate good enough" as a *cost* question, which is
the right one: a misroute matters only in proportion to the cost gap between
the plan chosen and the best plan, so a loose estimate is harmless wherever
the cost curves are flat and only bites where they diverge steeply.

- **Cost model** — parametric per-plan formulas whose **constants are fit by
  regression to the measured data** (below), not hand-set. In measured
  per-card units, reusing the `verify_cost_tier` ns/card *buckets* directly
  (don't model per-predicate cost — reuse the one calibrated tiering in
  `bench_verify_cost.rs`, so recalibration updates verifier ordering and
  planner together). Rough shapes:
  `GatheredScan ≈ C·verify_tier(residual)`; `StreamedSelect ≈ C·match_cost`;
  `PlanePopcountOrder ≈ (N/64)·word_cost` (flat in match count *and* page
  depth); `PrintingRangeScan`/idea-1 `≈ (limit/match_rate)·walk_cost` (the one
  with a bad tail). Each formula consumes the cardinality in its own operating
  space — which is where the "which space" question (below) gets pinned down.
  Predicate-cost bucketing is cheap here because the term is largely
  *common-mode* across the plans being compared (gather/stream both pay
  per-candidate verify ∝ their C; popcount has an empty residual, no verify
  term), so it mostly cancels in the argmin — cardinality and plan structure
  do the deciding. `verify_tier(residual)` (already max-over-tree, ignoring
  short-circuit/acceptance) is the starting per-card term; refine only if
  regret demands.
- **Gold standard** — `plan_gold = argmin cost(plan | TRUE count)`, the best
  achievable with perfect cardinality info. **Only legitimate once the model
  is calibrated against measured runtime** (a cardinality sweep, per the
  benchmark-artifacts protocol) — otherwise argmin-over-the-model just picks
  what the model *believes*, and "gold" is circular. This calibration is the
  load-bearing work.
- **Estimate regret** — `regret = cost(argmin cost(·|est) | true) −
  cost(plan_gold | true)`. The regret distribution is the single figure of
  merit: near-zero → the estimate is good enough to route on as-is; a fat tail
  → tighten the specific leaf/plan that drives it (the projection tiers).
  Estimate tightness is thus a *derived* requirement, not a goal — the global
  "est == truth %" is the wrong target.

### Is the cost model correct? Card mode yes, printing/artwork no (2026-07-20)

A routing cost model needs correct *ordering* between plans, not accurate
absolute predictions. The model achieves that **only in card mode** — where its
constants were fit and where `plan_cost_model_matches_gold` validated the argmin.
`printing_range_route_probe` exposed that in **printing mode it under-predicts
P3/P4 by ~3×** (model/measured ≈ 0.3–0.5; P1 fidelity swings 0.1×–2.2×). The
under-prediction factor is `n_printings/n_cards` (≈3.09): `eval_domain` counts
CARDS, but a printing-mode P3/P4 visits every card *and scans all its printings*
(~`n_printings` units) and emits printing rows, neither of which the card-fit
formula prices. It survived validation only because P1's dominance on the broad
printing ranges kept the misestimate from flipping the argmin — until the probe's
deep pages, where it did (2 rows routed off gold-P1).

**Design conclusion — enrich features, don't branch on mode.** The fix is not a
`mode` argument to `plan_cost` (that hard-codes the mode→work mapping and every
new plan/mode must be threaded through it). Mode is a *proxy* for two work
quantities the features should state directly: units *scanned* and units *emitted*,
each in the plan's operating space. The caller — which knows the mode — populates
`eval_domain`/emit in that space, and one mode-agnostic formula then prices card,
printing, and artwork alike (artwork's illustration-groups are just another count).
An explicit `mode`/executor argument is warranted only if a genuinely different
code path has a different *per-unit constant*, and even then a work-term is
cleaner than a categorical switch. This is prerequisite work for a unified
all-mode router — which, since card and printing both TIE the legacy tree, is a
structural-unification goal, not a speed one.

## Scope / sequencing

Everything validated against truth before anything reroutes. The force-plan
seam (step 2) comes *before* calibration because you can't measure a plan's
cost without being able to execute that specific plan — making plans
individually addressable is the prerequisite for the run-all-plans harness.

The target these steps converge on — the single router, in pseudocode — is
[local-engine-unified-router-target.md](./local-engine-unified-router-target.md).

**Status (2026-07-20): LANDED.** Steps 1–5 done and, past the A/B, step 6's
structural half too: `run_query` is now a 4-line string→enum adapter delegating to
the one cost-based router `run_query_routed` (all modes); the legacy decision tree,
the `CARD_ENGINE_PLAN_SELECT` toggle, and the `maybe_broad` routing threshold are
deleted (377 LOC net). What step 6 did NOT retire, because they aren't tree-routing
thresholds: `STREAM_MIN_MATCHES` (now the cost model's P3 small-total floor) and the
7/8 narrowing cutoff + memoize gate (inside `prepare_candidates`, shared by the
router). Correctness rests on the tree-independent durable tests
(`force_plan_differential_agreement`, `fuzz_row_identity_matches_reference`). The
value delivered is structural (one principled layer, extensible) at performance
parity — not a speed win (see Results below); the one real speed win, idea-1/idea-2,
remains deferred.

1. **Estimator** — standalone, sound bounds, fuzz-validated, *unwired*.
   (Shipped: #704.)
2. **Force-plan seam** — extract the four plan bodies into individually
   callable executors, each with a **applicability predicate** (which plans
   can correctly run this query; these predicates *are* the future
   `choose_plan` eligibility gates — not throwaway). Add an in-process
   force/dispatch entry point. Default routing behavior unchanged; add a
   differential test that every *applicable* plan returns rows identical to
   `GatheredScan` (the universal fallback / reference). Branches off `main`,
   independent of the estimator.
3. **Cost model + calibration harness** — run each applicable plan n× (min-of-n,
   quiesced machine — benchmark-artifacts protocol) via the force hook across a
   cardinality sweep, record `(true counts/features → measured per-plan cost)`,
   **fit the formula constants** by regression, and establish empirical gold =
   the plan actually fastest. Uses TRUE counts, so independent of the estimator.
4. **Estimate-regret report** — feed the estimator into the calibrated model;
   report the regret distribution (§Cost model). Depends on 1 + 3.
5. **Route on `argmin cost(·|est)`** — `choose_plan` wires the force/dispatch
   to the model; toggle-gated A/B via `CARD_ENGINE_PLAN_SELECT` (temporary
   legacy `run_query` duplicate, deleted once parity holds). The toggle's
   measured runtime confirms the model in production, closing the loop.
6. **Retire thresholds** — the 7/8 cutoff, `STREAM_MIN_MATCHES`, the memoize
   gate fall out of the cost comparison, one measured A/B at a time. Do not
   change plan choices and restructure in the same commit.

Filter trees are tiny, so `choose_plan`, the estimator, and the cost model are
all per-query noise; they run once per query, not per card.

## Results so far — and where the win actually is (2026-07-20)

Steps 1–4 shipped (#704/#708/#709/#710). The router (`run_query_routed`, all modes)
materializes candidates once (single plane eval / one `prepare_candidates`, no
double-eval) — or, for printing bare-ranges, takes the exact range-`k` without
materializing — and routes `argmin plan_cost` on the *actual* count. It is now the
default and only path (the legacy tree, toggle, and the tree-vs-router A/B benches
were deleted with the landing above); the findings below were measured during the
toggle-gated A/B and hold.

**Card-mode routing is a tie, not a win.** A/B on the real corpus
(then-`plan_routing_ab`, min-of-n): geomean routed/legacy **1.010×**, 41 tie / 3
marginally-slower / 0 faster. Cost-routing *reproduces* the hand-tuned tree's
plan choices — the tree's thresholds already sit at the cost crossovers — so
there is no card-mode speed to win. The residual ~1% is the argmin's own f64
evals on sub-µs queries. An earlier estimate-*before*-materialize prototype was
~15% slower (double plane eval + lo-cliff pessimism) and was abandoned for the
materialize-then-route shape above.

**This intermediate state is a deliberate structural *downgrade*.** The toggle,
the routed path, and the legacy tree now coexist — strictly *more* branching
than before. That is only justified as time-boxed scaffolding for the A/B, and
is debt to be repaid per steps 5–6: the routed path is the seed of the single
`route()`; the tree and thresholds get *deleted*, not accumulated. Landing
card-mode routing on its own buys parity-for-principle only, so it should ship
bundled with — or just before — the first frontier win below, never as "we
replaced the tree with an equal-speed cost model."

**The printing-mode P1 win was hypothesized — and measurement falsified it.**
The hypothesis: the tree's P1 gate (`range_too_broad_to_narrow`, a fixed
`k/index_len > 0.25` ratio) ignores page depth and sort alignment, while P1's
misaligned walk costs `(offset+limit)/match_rate`, so a *moderately*-broad range
at a *deep* page should misroute onto P1's blind walk where the ratio can't see
it. The `printing_range_route_probe` bench (src/tests.rs) tested exactly that —
11 bare price/year ranges across the broad/narrow margin × offsets to 20000,
printing mode, misaligned edhrec sort — comparing the tree's pick and the cost
model's pick to empirical gold:

> **54 scored rows: tree geomean regret 1.000× (gold on every row); model
> 1.015×, strictly faster on 0.** P1 wins broad ranges at *every* depth tested,
> including offset 20000 — because P3/P4 in printing mode both pay the full
> O(n_cards) match phase, which dominates P1's blind-but-early-stopping walk. So
> `range_too_broad_to_narrow` already sits at the P1/P4 crossover; depth doesn't
> move it. The naive cost model was slightly *worse* — 2 deep rows (offset 20000)
> where its `(offset+limit)/match_rate` walk term over-penalizes P1 and it routes
> away at 1.32×/1.70×.

So printing ranges tie too (the tree marginally ahead). Combined with the
card-mode tie, **cost-based routing has now matched-or-slightly-lost to the
hand-tuned tree everywhere measured** — the thresholds are genuinely well-placed.
This is the load-bearing finding: #702 as a *speed* play has no evidence behind
it on today's plans. What remains is (a) the purely structural argument (one cost
rule vs scattered thresholds — but the cost model carries its own calibration
burden, so this is not obviously a net simplification), and (b) whether a
genuinely *unexpressed* decision exists that the tree cannot make at all — the
place to look before spending more, not another mode of the same P1/P3/P4 menu.

**Artwork not separately probed** — only P3/P4 apply (the same crossover the tree
sits on in card mode), so absent a new signal it is expected to tie as well.

**The one real win found: idea-1 vs idea-2 at depth (a genuinely *unexpressed*
decision).** `idea1_vs_idea2_probe` (src/tests.rs) measured idea-1 (P1, built) against
a cost-representative idea-2 kernel (range → printing existence bitmap → popcount-skip
→ emit; the deferred #656 mechanism, timed without building it) on `unique=printing`
broad ranges × page depth, edhrec (unrelated) sort — the one cell the sorted-range doc
says the two genuinely compete. idea-2 is **flat in offset** (~19–54µs, scaling with `k`
not depth); idea-1 grows ~`(offset+limit)/match_rate`. So there is a real crossover, and
its depth scales inversely with match-rate:

> `usd<0.25` (30%) & `usd>=1` (24%): idea-2 wins from offset **~500–2000**; mid-rate
> (~50–65%) ~5000; high-rate (73–90%) ~10–20k. Ratios reach 35× at offset 20000.

This is the first evidence *for* the cost-routing thesis: the idea-1/idea-2 winner is an
offset×match_rate crossover — exactly what a cost comparison expresses and a fixed
threshold cannot — and the tree cannot pick idea-2 at all because idea-2 does not exist.
Caveats keeping this honest: (1) the kernel is an **optimistic lower bound** — a correct
idea-2 needs bits in sort order (#656's printing permutation), adding scatter cost, so
real crossovers land somewhat deeper; (2) absolute savings at *realistic* depth (offset
500–2000) are ~50–170µs on already-sub-ms queries — the 35× ratios are all at offset
20000, which nobody pages to; (3) deep-paged broad-range printing queries are a rare
corner (though #656 flags it as a known ~1.07ms gap). **Decision pending:** build idea-2
(#656 printing-space pager + permutation, with the NULL over-inclusion care that reverted
#689) and cost-route idea-1/idea-2 — real work for a rare-but-known-gap win — vs leave it
deferred. This is a prioritization call, not a technical unknown.

## Keeping costs/plans current as the engine changes

The constants are fit to a point-in-time measurement, so the design has to say
how they stay honest. This reuses the discipline the engine already applies to
`verify_cost_tier` and the calibrated guards (#647) — not a new burden:

- **What's robust vs fragile.** Constants are in consistent units (ns/card,
  ns/word), and `argmin` cares about *ratios* between plans, not absolutes — so
  a uniform hardware speed change mostly preserves the choice. Recalibration is
  needed for **non-uniform** changes: a plan reimplemented, a new index shifting
  a predicate class's cost, a new plan added. Those are code changes, and the
  process attaches to them.
- **Adding/changing a plan** is compile-time-forced to be complete, like
  `FilterExpr` variants force a `verify_cost_tier` arm: a new plan needs an
  executor + applicability predicate + cost formula + inclusion in the harness.
  The differential test (all applicable plans agree) then guards correctness
  automatically.
- **Recalibration is deliberate and off-CI.** CI machines are too noisy for
  timing (violates the benchmark-artifacts protocol), so CI runs *correctness*
  (differential agreement) and at most a coarse **drift tripwire** — assert the
  model's predicted-cheapest matches empirical-cheapest on a small committed set
  within tolerance, failing when constants drift enough to *flip* a decision.
  Re-fitting itself is a manual `--ignored` bench run on a quiesced machine that
  commits new constants **with provenance** (corpus size, date, machine — the
  style `verify_cost_tier`'s doc-comments already use).
- **Regret is the health metric.** After any plan/cost change, re-run the
  regret report; a risen tail means the constants need refitting or the formula
  *shape* is wrong (structured residuals), not just the coefficients.

## Ordering parity across plans

Swapping which plan runs a query must not change the *rows* — and it doesn't
change the *order* over the keys the product actually defines. SQL's `ORDER BY`
is three terms — `[sort column] → edhrec_rank → prefer_score` — then arbitrary
(no unique final key), so there is nothing to be parity-with past key 3. The
contract we enforce is therefore **2-key parity**: all plans agree on keys 1–2
(primary sort column, then `edhrec_rank`), both of which are card-level values
read identically by every plan (the top 64 bits of `sort_key_bits`).

- **Key 3 (`prefer_score`) is deliberately *not* enforced across plans.** The
  precomputed sort permutation bakes in the *default* representative's
  prefer_score; under a non-default prefer the perm-based plans can order a
  key-3 tie differently from the gathered path (and from SQL). Fixing it would
  cost the streaming fast-path on non-default-prefer broad queries for a tie
  that is vanishingly rare (`edhrec_rank` is ~unique per card), so it is a
  known, accepted, pre-existing divergence — not fixed here.
- **Enforced by** `force_plan_differential_agreement`: every applicable plan's
  2-key value sequence + scryfall_id multiset must match `GatheredScan`, across
  modes and a default *and* non-default prefer. (This is Rust plan-vs-plan; a
  Rust-vs-SQL order check is separate and rides the existing engine/SQL parity
  suite.)

## Measurement

Hook the estimator into the `fuzz_row_identity_matches_reference` harness and
record `(lo, hi, est, materialized_truth)` per query:

- **Bounds soundness is a hard invariant** — `truth ∈ [lo, hi]` must always
  hold; a violation is a bug in the algebra, fail the test. (Estimate
  *accuracy* is a reported distribution, not pass/fail — a bad estimate is
  only slow.)
- **Plan-type coverage assertion** — every `PhysicalPlan` variant must be
  exercised by the corpus; if not, that's a corpus gap to fill (#698/#699
  just expanded coverage — this makes it *checked*).

For the seam PR, the differential oracle must show identical row identity and
totals with the toggle on and off; any divergence is either a move bug or a
*measured, intended* estimate-vs-materialized-count divergence, not a
surprise. For the threshold PRs, re-run the #647 harness on the affected
family: no geomean regression, no new tail.

## Open questions

- **Which space drives routing** — now largely determined by the cost model:
  each plan's cost formula consumes the cardinality in its own operating space
  (card-space for card-mode gather/popcount, printing-space for a printing
  walk), so "which space" is answered per-plan rather than globally. Remaining
  question is only how tightly the estimator must project between them, which
  regret (above) decides.
- **Estimator return shape** — recursion composes a single-space
  `Cardinality { lo, est, hi }`; a both-spaces container (card + printing
  triples) is assembled only at the *root* for the decision site, never
  threaded through composition (projection doesn't distribute — above).
- Is the plane-∩-candidates composition (the `CardBits` direct-AND vs
  materialize-and-retain choice in fast path 3) a *plan* decision or
  executor-internal? Leaning executor-internal.
- Future, out of scope: a build-time per-field acceptance-rate table for
  verifier ordering — the "statistics" half of a CBO we're otherwise
  declining. File separately if wild-corpus traffic shows verify-order
  mispredictions costing real latency.
