# Engine: Drop the Redundant Broadness Veto in `printing_range_fastpath`

Status: proposed, not yet implemented or measured. Filed while closing out the "Idea-1 crossover
guard" question in
[local-engine-sorted-range-fastpath.md](done/local-engine-sorted-range-fastpath.md).

## The finding

`printing_range_fastpath` ([lib.rs:4228](../../card_engine/src/lib.rs#L4228)) has two independent
bail conditions before it walks:

1. **`range_too_broad_to_narrow(k, idx.len())`** ([lib.rs:4244](../../card_engine/src/lib.rs#L4244))
   — a broad/narrow *performance* gate: bail if the range is selective enough that the existing
   narrowing path already wins.
2. **`k <= STREAM_MIN_MATCHES`** ([lib.rs:4266](../../card_engine/src/lib.rs#L4266)) — a
   *correctness* gate: on a tiny index, `run_query_streamed` gathers and sorts globally instead of
   streaming, with a different tie-break. Unrelated to cost.

Gate 1 duplicates work the #702 cost-based router already does. `PhysicalPlan::PrintingRangeScan`'s
`applicable()` ([lib.rs:4778](../../card_engine/src/lib.rs#L4778)) has **no** broadness check, so
today the veto is the *only* thing standing between "any bare range, any `k`" and an attempted walk
— but `plan_cost(PrintingRangeScan, …)` ([cost.rs:254](../../card_engine/src/cost.rs#L254)) already
prices the same tradeoff:

```
match_rate = (matches / n_printings).max(MATCH_RATE_FLOOR)   // MATCH_RATE_FLOOR = 1e-6
printings_walked = page_span / match_rate
cost = printings_walked * RANGE_WALK_STEP_NS + RANGE_FIXED_COST_NS
```

As a range narrows, `matches` shrinks, `match_rate` shrinks, `printings_walked` blows up, and the
walk's cost rises with it — the argmin in `choose()` ([lib.rs:5417](../../card_engine/src/lib.rs#L5417))
should already route away from `PrintingRangeScan` for narrow ranges without gate 1's help. This is
exactly the hypothesis [local-engine-sorted-range-fastpath.md](done/local-engine-sorted-range-fastpath.md)'s
"Idea-1 crossover guard" question resolved: the #702 doc's `printing_range_route_probe`
([done/00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md), lines
289-310) swept the fixed veto against a pure cost-model route across offsets to 20,000 and found
them equivalent (model 1.015× vs. tree gold, no regret). If the model already agrees with the veto
everywhere it was tested, the veto isn't adding a decision the model doesn't already make — it's a
second, hardcoded copy of it.

## Proposed change

Delete the `range_too_broad_to_narrow` call from `printing_range_fastpath`; keep the
`STREAM_MIN_MATCHES` bail as-is (it's a correctness constraint, not a cost one). Let `choose()`'s
argmin be the sole arbiter of whether `PrintingRangeScan` is cheaper than the materializing
alternatives.

## Why this isn't a trivial delete

`match_rate`'s floor (`MATCH_RATE_FLOOR = 1e-6`) exists to avoid a near-zero divide, which means the
cost formula's precision degrades for very selective ranges (small `k`) — a regime the #702 probe's
sweep targeted the *moderate/broad* band (the doc's ~1.8k–20k crossover question), not necessarily
near-empty ranges. Before deleting the veto, sweep small `k` specifically (down toward `k` = a
handful of matches) and confirm the model still routes away from `PrintingRangeScan` there, the same
way [#647](done/00647-engine-cost-guard-calibration.md) calibrated guards from measurement rather
than intuition.

## Related

- [local-engine-sorted-range-fastpath.md](done/local-engine-sorted-range-fastpath.md) — where this was
  found, while closing out the Idea-1 crossover guard question.
- [done/00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md) — the
  cost-based router and the probe that validated the fixed veto against the cost model.
- [done/00647-engine-cost-guard-calibration.md](done/00647-engine-cost-guard-calibration.md) —
  precedent for calibrating a guard from measurement instead of a fixed constant.
