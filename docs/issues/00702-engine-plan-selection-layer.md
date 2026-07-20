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

## Scope / sequencing

Everything validated against truth before anything reroutes. The force-plan
seam (step 2) comes *before* calibration because you can't measure a plan's
cost without being able to execute that specific plan — making plans
individually addressable is the prerequisite for the run-all-plans harness.

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
