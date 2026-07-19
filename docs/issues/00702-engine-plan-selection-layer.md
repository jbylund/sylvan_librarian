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

## Scope / sequencing

Estimator first, so the accuracy is measured before anything reroutes:

1. **Estimator as a standalone, validated component** — leaf counts + bounds
   algebra + adaptive card projection, *not yet wired into routing*. Pure
   addition, zero behavior change.
2. **The `choose_plan` seam** — now genuinely single-layer (routing keys off
   the estimate, so it precedes materialization). Behavior-preserving where
   it can be, toggle-gated A/B via `CARD_ENGINE_PLAN_SELECT` (same LazyLock
   pattern; temporary legacy duplicate of `run_query`, deleted in a
   follow-up once parity holds).
3. **Threshold → cost-comparison, one at a time** — the 7/8 cutoff,
   `STREAM_MIN_MATCHES`, the memoize gate — each its own measured A/B. Do not
   change plan choices and restructure in the same commit.

Filter trees are tiny, so `choose_plan` and the estimator cost is noise;
they run once per query, not per card.

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

- **Which space drives routing** — card, printing, or both per-decision. The
  estimator produces both; late projection keeps ranges exact longer but
  complicates the algebra. Deferred to the wiring PR.
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
