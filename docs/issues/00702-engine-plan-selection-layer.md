# Consolidate Plan Selection Into a Single Layer

[#702](https://github.com/jbylund/sylvan_librarian/issues/702)

The engine already makes cost-based execution decisions — it just makes them
in a growing decision tree scattered through `run_query` (lib.rs ~3976),
each branch gated on a conjunction of preconditions. This issue proposes
folding that tree into one explicit plan-selection layer: enumerate the
applicable physical plans, cost each with the constants we already measure,
pick the cheapest. Structurally a planner; deliberately **not** a
Selinger-style cost-based optimizer (see Non-goals).

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
  instead of reading an exact size — it's a plan choice masquerading as a
  filter step.
- (2) and (4) both want "a sort permutation exists" but diverge on the
  filter shape; (1) and (4) both handle broad range predicates but in
  different modes. The preconditions already brush against each other.

The trigger we're pre-empting: the first fast path whose preconditions
*overlap* another's and would pick a different (or wrong) plan, or the Nth
fast path that can't be added without re-reading the prior N−1.

## What the layer is

A thin function — call it `choose_plan` — that runs after binding /
narrowing has produced the facts the current tree already computes
(candidate set or its exact size, plane presence/popcount, residual
shape, whether a sort permutation exists, `all_match_known`) and returns a
`PhysicalPlan` enum. `run_query` becomes: gather facts → `choose_plan` →
dispatch. The existing branch bodies become the plan executors, largely
unchanged.

```
enum PhysicalPlan {
    PrintingRangeScan { .. },       // fast path 1
    PlanePopcountOrder { .. },      // fast path 2
    StreamedSelect { candidates },  // fast path 4, broad
    GatheredScan { candidates },    // fallback
}
```

Costing uses the constants we already have: `verify_cost_tier` per residual
node × the eval domain (candidate count, known exactly), plus the O(words)
vs O(popcount) distinction that (2) vs (4) already encode informally. The
7/8 guess becomes `cost(GatheredScan over candidates)` vs
`cost(StreamedSelect over full corpus)` — a comparison of two numbers we can
compute, not a magic ratio.

## Non-goals

This is explicitly **not** a classical cost-based optimizer, because the two
hardest things a CBO does don't apply:

- **No join-order search.** Single-table selection + sort + paginate. The
  plan space is shallow and enumerable by hand — a fixed handful of physical
  plans, not a search over trees.
- **No cardinality *estimation*.** Posting-list lengths and plane popcounts
  are *exact counts we read*, not statistics we estimate. The layer must
  preserve that: it costs plans from known sizes. We are not building a
  statistics/estimator subsystem, and we should resist any plan whose cost
  depends on a *guessed* selectivity.

The one genuine unknown stays unknown: **acceptance rates** of non-set
residual predicates (`o:flying`, numeric ranges) — `or_child_key` already
documents these as "unknowable statically." The layer costs by verify tier ×
domain size, exactly as the verifier ordering does today; it does not try to
predict acceptance. If we ever want that, it's a separate build-time
selectivity table (see Future), not part of this consolidation.

## Scope / sequencing

Pure refactor first, behavior-preserving: move the four decisions into
`choose_plan` with the *same* thresholds (including the literal 7/8), gated
behind a `CARD_ENGINE_PLAN_SELECT` env toggle (same LazyLock pattern as the
calibrated constants). Prove parity, then — as follow-ups — replace each
hand-tuned threshold with a computed cost comparison, one at a time, each
its own A/B. Do not change plan choices and restructure in the same commit.

Filter trees are tiny, so `choose_plan` cost is noise; it runs once per
query, not per card.

## Measurement

Parity is the whole game for the refactor step: the differential oracle
(#641 / `fuzz_row_identity_matches_reference`) must show identical row
identity and totals with the toggle on and off across the fuzz corpus. Any
divergence is a bug in the move, not an intended plan change. Once each
threshold is later replaced by a cost comparison, re-run the #647 harness on
the affected query family and confirm no regression at the geomean and no
new tail.

## Open questions

- Does `choose_plan` return a plan *before* or *after* `memoize_text_predicates`
  and `order_children_by_verify_cost`? Those currently run mid-`run_query`
  and depend on the chosen eval domain — likely the layer picks the
  candidate strategy first, then memoization/ordering run inside the chosen
  executor.
- Is the plane-∩-candidates composition (the `CardBits` direct-AND vs
  materialize-and-retain choice in fast path 3) a *plan* decision or an
  executor-internal one? Leaning executor-internal, but it's exactly the
  kind of buried cost choice this layer is meant to surface.
- Future, out of scope: a build-time per-field acceptance-rate table would
  let `or_child_key` / And ordering use real selectivity instead of cost
  tiers. Tempting, but it's the "statistics" half of a CBO we're otherwise
  declining — file separately if wild-corpus traffic ever shows verify-order
  mispredictions costing real latency.
