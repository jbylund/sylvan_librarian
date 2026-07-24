# Engine: Drop the Redundant Broadness Veto in `printing_range_fastpath`

Status: proposed, not yet implemented or measured. Filed while closing out the "Idea-1 crossover
guard" question in
[local-engine-sorted-range-fastpath.md](done/local-engine-sorted-range-fastpath.md). Two possible
unblocks are laid out below: (1) sweep small `k` and prove the model routes correctly there, or
(2) the cleaner route — bias the plan's cost conservatively near its cliff and lean on the bounded
`GatheredScan` fallback, calibrated with `explain_analyze` (#745).

## The finding

`printing_range_fastpath` ([lib.rs:4587](../../card_engine/src/lib.rs#L4587)) has two independent
bail conditions before it walks:

1. **`range_too_broad_to_narrow(k, idx.len())`** ([lib.rs:4603](../../card_engine/src/lib.rs#L4603))
   — a broad/narrow *performance* gate: bail if the range is selective enough that the existing
   narrowing path already wins.
2. **`k <= STREAM_MIN_MATCHES`** ([lib.rs:4625](../../card_engine/src/lib.rs#L4625)) — a
   *correctness* gate: on a tiny index, `run_query_streamed` gathers and sorts globally instead of
   streaming, with a different tie-break. Unrelated to cost.

Gate 1 duplicates work the #702 cost-based router already does. `PhysicalPlan::PrintingRangeScan`'s
`applicable()` (`printing_range_scan_applicable`, [lib.rs:5727](../../card_engine/src/lib.rs#L5727))
has **no** broadness check, so today the veto is the *only* thing standing between "any bare range,
any `k`" and an attempted walk — but `plan_cost(PrintingRangeScan, …)`
([cost.rs:266](../../card_engine/src/cost.rs#L266)) already prices the same tradeoff:

```
match_rate = (matches / n_printings).max(MATCH_RATE_FLOOR)   // MATCH_RATE_FLOOR = 1e-6
printings_walked = page_span / match_rate
cost = printings_walked * RANGE_WALK_STEP_NS + RANGE_FIXED_COST_NS
```

As a range narrows, `matches` shrinks, `match_rate` shrinks, `printings_walked` blows up, and the
walk's cost rises with it — the argmin in `run_query_routed`'s `choose` closure
([lib.rs:6617](../../card_engine/src/lib.rs#L6617)) should already route away from
`PrintingRangeScan` for narrow ranges without gate 1's help. This is
exactly the hypothesis [local-engine-sorted-range-fastpath.md](done/local-engine-sorted-range-fastpath.md)'s
"Idea-1 crossover guard" question resolved: the #702 doc's `printing_range_route_probe`
([done/00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md), lines
289-310) swept the fixed veto against a pure cost-model route across offsets to 20,000 and found
them equivalent (model 1.015× vs. tree gold, no regret). If the model already agrees with the veto
everywhere it was tested, the veto isn't adding a decision the model doesn't already make — it's a
second, hardcoded copy of it.

## Proposed change

Delete the `range_too_broad_to_narrow` call from `printing_range_fastpath`; keep the
`STREAM_MIN_MATCHES` bail as-is (it's a correctness constraint, not a cost one). Let
`run_query_routed`'s `choose` argmin be the sole arbiter of whether `PrintingRangeScan` is cheaper
than the materializing alternatives.

## Why this isn't a trivial delete

`match_rate`'s floor (`MATCH_RATE_FLOOR = 1e-6`) exists to avoid a near-zero divide, which means the
cost formula's precision degrades for very selective ranges (small `k`) — a regime the #702 probe's
sweep targeted the *moderate/broad* band (the doc's ~1.8k–20k crossover question), not necessarily
near-empty ranges. Before deleting the veto, sweep small `k` specifically (down toward `k` = a
handful of matches) and confirm the model still routes away from `PrintingRangeScan` there, the same
way [#647](done/00647-engine-cost-guard-calibration.md) calibrated guards from measurement rather
than intuition.

## A cleaner unblock: shape the cost curve conservatively, don't just prove it

"Sweep small `k`, confirm the model is accurate enough, then delete" is one route. A second — arguably
better — one falls out of two facts about how the router actually costs plans:

- **Inputs are exact; only the output is estimated.** Since the "exact counts, not estimates"
  reframing (#722, folded into the #702 doc) the cost *features* are actual counts — the range's
  in-range `k`, plane popcounts, candidate counts — not cardinality guesses (`PlanFeatures`,
  [cost.rs:60](../../card_engine/src/cost.rs#L60): "exact or cheap-exact … never estimated"). So the
  only uncertainty near the cliff is the **count→time mapping** (`plan_cost`'s fitted coefficients),
  not "how many rows match." `MATCH_RATE_FLOOR` is a crude *shape* of that mapping at small `k`, not a
  noisy estimate.
- **The fallback is bounded.** `GatheredScan` is always applicable and O(n) — a "not awful" ceiling
  under every query.

Given both, the cost formula doesn't need to be *precise* for selective ranges — it needs to be
*conservative* there. Bias `PrintingRangeScan`'s cost to start rising a little **before** the measured
cliff, as a function of the exact known `k`, so the argmin crosses over to `GatheredScan` slightly
early. The two error directions are wildly asymmetric: under-costing near the cliff falls off it (the
super-linear walk blowup, `printings_walked = page_span / match_rate`), while over-costing bails to the
bounded scan and pays at most the fallback. Trading a little common-case efficiency to cap that
worst-case regret is the right call — **provided the bias stays local to the cliff** and doesn't erode
the regime where the walk genuinely wins.

This converts the unblock from "prove precision in an untested regime (hard, and only as good as the
sweep's coverage)" into "guarantee conservatism + lean on the bounded fallback (easy)" — and keeps the
decision *inside* the cost model (one tunable, testable arbiter) rather than duplicated as a hardcoded
veto beside it. A conservative cost bias is still cost-based routing; the veto is a second decision
system.

### Calibrating the bias with `explain_analyze`

[#745](done/00745-engine-explain-analyze.md)'s `explain_analyze` is the instrument to fit and validate
the curve. Per applicable plan it returns `predicted_ns` (the model) alongside raw, un-reduced
`trials_ns` (measured), run head-to-head with warmups and rotated order — a per-query
predicted-vs-actual panel across every plan. Use it to locate the cliff empirically (where does
`PrintingRangeScan`'s `trials_ns` shoot up as `k` shrinks?), see whether `predicted_ns` tracks or lags
that jump, and shape the bias until the predicted crossover to `GatheredScan` lands at or before the
measured cliff. Then confirm the aggregate with the #702 regret harness (no common-case regression) —
the same measurement-first discipline as [#647](done/00647-engine-cost-guard-calibration.md), applied
to the *shape* of the curve near the cliff rather than to a fixed threshold.

**Caveat:** `explain_analyze`'s `trials_ns` is a fair comparison *between plans*, not end-to-end wall
time (each trial re-runs `prepare_candidates`; the router acquires the shared artifact once and
reuses). Calibrate the crossover by comparing plans against each other and their own predictions, not
against a production `query()` latency.

## Related

- [local-engine-sorted-range-fastpath.md](done/local-engine-sorted-range-fastpath.md) — where this was
  found, while closing out the Idea-1 crossover guard question.
- [done/00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md) — the
  cost-based router, the exact-counts framing (#722), and the probe that validated the fixed veto
  against the cost model; the regret harness lives here too.
- [done/00745-engine-explain-analyze.md](done/00745-engine-explain-analyze.md) — the per-plan
  predicted-vs-actual timing primitive used to locate the cliff and calibrate the conservative bias.
- [done/00647-engine-cost-guard-calibration.md](done/00647-engine-cost-guard-calibration.md) —
  precedent for calibrating a guard from measurement instead of a fixed constant.
