---
title: "We Replaced Our Query Planner's Decision Tree with a Cost Model, and It Tied"
date: 2027-03-02
publishDate: 2027-03-02
tags: ["rust", "query-engine", "performance", "planner"]
summary: "We deleted the decision tree that picked execution plans and replaced it with argmin over per-plan cost estimates. On a 520-query survey it came out 0.996x — a hair slower. We shipped it anyway, because one cost rule is more extensible than four hand-disjoint preconditions, and a cost model can express an offset-by-selectivity crossover no fixed threshold can."
---

Our Rust card-search engine chose how to execute a query with a decision tree over four execution plans in `run_query`, each branch guarded by a hand-maintained conjunction of preconditions and tried in order.
[We deleted it](https://github.com/jbylund/sylvan_librarian/pull/712) and replaced plan selection with a cost model — estimate what each plan would cost, run the cheapest.
On a 520-query survey it came out **0.996×** — a hair *slower* — while picking the empirically-fastest plan on 87 of 88 calibration queries.
We shipped the rewrite that made nothing faster anyway.

## The Tree That Worked

There are four physical plans, each a familiar database access pattern with its own cost profile and applicability:

- **`PrintingRangeScan`** walks a sorted printing range and stops once the page is full — an ordered index scan feeding a `LIMIT`, terminating early instead of sorting the whole result.
- **`PlanePopcountOrder`** treats a precomputed bit-plane as the match set, like a Postgres [bitmap index scan](https://en.wikipedia.org/wiki/Bitmap_index); its cardinality is a [popcount](https://en.wikipedia.org/wiki/Hamming_weight) and its order comes from scattering the set bits through an inverse permutation.
- **`StreamedSelect`** walks a precomputed sort permutation and emits only the page's worth of matches — a presorted scan feeding a `LIMIT`, with no separate sort node.
- **`GatheredScan`** gathers every match and [quickselects](https://en.wikipedia.org/wiki/Quickselect) the page — the bounded-heap top-N sort Postgres runs for `ORDER BY … LIMIT`, here as the universal fallback.

The old code chose between them with staged `if` returns, roughly in this shape:

```rust
// unique=printing, a bare broad range, non-empty store, no plane → PrintingRangeScan
// filter == True and a sort permutation exists           → PlanePopcountOrder
// candidates narrowed to < 7/8 of the store              → StreamedSelect
// otherwise                                              → GatheredScan
```

Each precondition set was pairwise-disjoint *by construction*, not by any checked invariant.
The tell was the `len < cards.len() - cards.len() / 8` in the third branch — a hardcoded 7/8 selectivity guess, a plan choice wearing the costume of a filter step.
Two of the branches both wanted "a sort permutation exists" but diverged on the filter shape; two both handled broad range predicates but in different modes.
Nothing was broken.
The problem was that the *next* plan you wanted to add would have to reason about its interaction with the previous three, and the one after that with four.

## Routing on Estimates, Not Materialized Counts

The reason the tree was staged is that its branches keyed off the *materialized* candidate count — you had to build the set before you could measure it.
But the cardinality of most leaf predicates is available far more cheaply than the set itself: a postings list knows its `len()`, a range predicate's two `partition_point` searches already compute `k = end − start`, a plane is a popcount.
If routing keys off a cheap estimate instead of a materialized count, every plan decision moves ahead of materialization, and the staging constraint that forced the tree dissolves.

So the [`PhysicalPlan`](https://github.com/jbylund/sylvan_librarian/blob/bf6f788e212072f7e5d64e397213d7f32c0a344d/card_engine/src/lib.rs#L4082) enum owns its own knowledge — whether it's applicable, whether it materializes, and its cost — and [selection](https://github.com/jbylund/sylvan_librarian/blob/bf6f788e212072f7e5d64e397213d7f32c0a344d/card_engine/src/lib.rs#L4642) is a generic argmin, not a tree:

```rust
PhysicalPlan::ALL
    .filter(|p| p.applicable(filter, mode, /* … */))
    .min_by(|a, b| cost::plan_cost(a, feats).total_cmp(&cost::plan_cost(b, feats)))
```

The whole [router](https://github.com/jbylund/sylvan_librarian/blob/bf6f788e212072f7e5d64e397213d7f32c0a344d/card_engine/src/lib.rs#L4620) is three linear steps: **acquire** the query's cheap count source (a plane's popcount, a range's index-`k`, or a narrowed candidate list), **choose** the argmin, **dispatch** the winner.
Adding a plan is declaring four things about it — applicable, materializing, cost, executor — not editing a decision tree.
This is deliberately *not* a Selinger-style optimizer: the plan space is a fixed handful, enumerable by hand, with no join-order search.

The rewrite did not purge every hand-tuned constant.
The 7/8 guess is still there — but it moved into candidate preparation, where it now decides whether a narrowed list is small enough to keep instead of scanning everything.
It went from a plan choice wearing the costume of a filter step to an actual filter step; the plan choice it used to make is the argmin's now.

## It Tied

The [cost model](https://github.com/jbylund/sylvan_librarian/blob/bf6f788e212072f7e5d64e397213d7f32c0a344d/card_engine/src/cost.rs#L202) is a per-plan formula whose constants are fit to a calibration bench on the real 97,206-printing corpus.
The objective those constants were fit against is not absolute accuracy — it's that `argmin plan_cost` reproduces the empirically-fastest ("gold") plan per query.
The calibration set is 88 hand-built queries: each physical plan, in card and printing mode, at shallow and deep page offsets (artwork mode exercises the same `StreamedSelect`/`GatheredScan` crossover as card mode, so it is not separately probed).
The argmin reproduces the gold plan on 87 of them; the one miss is a sub-microsecond tie between two plans that run within noise of each other.

End to end, against the tree it replaced (survey of 400 generated + 120 wild queries, seed 42, `unique` across card/printing/artwork, on an M-series MacBook), speedup as tree ÷ cost model — above 1.0 is faster:

| pctile | tree | cost model | speedup |
|---|---|---|---|
| p50 | 54 µs | 54 µs | 1.00× |
| p90 | 177 µs | 177 µs | 1.00× |
| p95 | 248 µs | 249 µs | 0.99× |
| p99 | 708 µs | 617 µs | 1.15× |

The geomean across all 520 queries is **0.996×**: taken as a whole, the cost-based router is a hair *slower* than the tree.
The p99 improvement is real but comes from a separate change in the same PR — a bounded top-k in the gather executor — not from routing.
Routing itself is, if anything, a small tax: the argmin adds about 1% to the default-sorted (`edhrec`) queries that dominate the survey — the cost of computing estimates the tree never bothered with, visible in the p95 and geomean — and it buys nothing back, because on today's plans it makes the same choices the tree did.
Replacing the decision tree was not a speed play, and the survey says so plainly: it made nothing faster, and by a rounding error it made the typical query about 1% slower.

## Why Ship a Tie

The obvious objection: a cost model has its own calibration burden.
A table of fit constants that must be re-measured when a plan changes is not obviously simpler than a tree of thresholds — you have arguably moved the maintenance, not removed it.

Two things answer this.
First, the tree's disjointness was an unchecked invariant; the cost model has one decision rule and a *checkable* objective — a calibration bench (run against a locally-built copy of the real corpus, not yet a CI gate) verifies that `argmin` still picks the empirically-fastest plan, so a drifted constant or a regressed plan surfaces as a failing check.
"The preconditions are still disjoint" was never a check you could run at all.
Second, and this is the actual payoff: a cost model can express a decision a threshold tree structurally cannot.

## The Decision a Threshold Can't Make

Deep-paged broad range queries under `unique=printing` have two viable plans.
`PrintingRangeScan` (from the four plans above) reads the permutation from the top and stops when the page fills. Its whole `plan_cost` is `(page_span / match_rate) × step`, so the sparser the matches, the further it walks:

```rust
PhysicalPlan::PrintingRangeScan =>
    (page_span / match_rate) * RANGE_WALK_STEP_NS + RANGE_FIXED_COST_NS
```

The alternative — a printing-space `PlanePopcountOrder`, the same bitmap-popcount plan in printing space, which we haven't built — would read the page off a printing-existence bitmap: **flat in offset**, scaling with page size rather than depth.

A [probe](https://github.com/jbylund/sylvan_librarian/blob/bf6f788e212072f7e5d64e397213d7f32c0a344d/docs/issues/00702-engine-plan-selection-layer.md) timed the real `PrintingRangeScan` walk against a cost-representative kernel of that bitmap plan — the bitmap build and popcount measured on their own, without wiring up the full plan (which would need a printing-space sort permutation we haven't landed).
The winner flips on *two* variables at once.
At 30% match rate the bitmap plan wins past offset ~500–2000; at high density, not until ~10–20k; and the ratio reaches 35× at offset 20,000.
A fixed threshold cannot express a crossover that depends on both offset and selectivity — an argmin over two cost curves is exactly the shape of that decision.
The tree could never pick the bitmap plan — not because the threshold was wrong, but because it was a plan the staged structure had no slot for.

That win is still latent: the printing-space bitmap plan is deferred, and the survey confirms deep-paged broad-range printing queries are a cold corner of real traffic.
What shipped is the *capacity* to make the decision, at parity on everything else.

## The Model Sits at a Ceiling

The tempting next step was to make the cost model accurate.
The gathered plan's cost is a sum of per-feature terms:

```rust
PhysicalPlan::GatheredScan =>
    eval_domain * GATHER_CARD_PASS_NS
        + scan_units * (GATHER_SCAN_PER_ROW_NS + tier_ns)   // the per-row "SCAN" coefficient
        + matches   * GATHER_PUSH_PER_MATCH_NS
        + page_span * GATHER_SELECT_PER_PAGE_SLOT_NS
        + GATHER_FIXED_COST_NS
```

We built a 1,200-query fuzzer corpus and refit those constants with weighted least squares on a train/test split.
It didn't work: `scan_units`, `matches`, and `page_span` co-vary across realistic queries — a broad query scans many rows, matches many of them, and spans a deep page all at once — so the regression trades one coefficient against another and drives `GATHER_SCAN_PER_ROW_NS` negative to compensate.
An overfit, not a better model.
The constants sit at roughly 1.4× absolute error and cannot be tightened by more data; the collinearity is structural.

This would sink a real optimizer, and it is fine here.
`argmin` cares about the *ratios* between plans, not absolute costs, and the ratios are stable — which is why a model that is 40% wrong in absolute terms still reproduces the gold plan 87 times out of 88.
The calibration burden the objection worried about turns out to be bounded by exactly one thing: whether the cheapest-looking plan is the fastest plan, a property we can test, and not by how close the predicted microseconds are to the real ones, a property we can't improve.

The rewrite made nothing faster, and it is the change that will let the next plan make nothing slower.
