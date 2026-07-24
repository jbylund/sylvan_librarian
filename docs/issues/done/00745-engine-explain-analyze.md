# Engine: `explain` / `explain_analyze` for the Plan-Selection Layer

**Status: proposed**, filed as [#745](https://github.com/jbylund/sylvan_librarian/issues/745). Builds
directly on
[00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md)
(landed): that issue gave the engine a single cost-based router
(`run_query_routed`, lib.rs) that argmins `cost::plan_cost` over
`PhysicalPlan::ALL.filter(applicable)` (6 variants: `PrintingRangeScan`,
`PrintingCompose`, `PlanePopcountOrder`, `CardRangePopcount`,
`StreamedSelect`, `GatheredScan` — lib.rs ~4843), plus a force/dispatch seam
(`run_query_with_plan`, lib.rs ~5900) that runs any one named plan directly.
This note proposes turning that machinery into a callable diagnostic
primitive instead of leaving it reachable only from `cargo test`/`cargo bench`.

## The gap

Every query already computes `cost::plan_cost` for *every* applicable plan
inside the router's `choose` closure — then throws away all but the argmin
(lib.rs ~5718). Nothing captures the discarded numbers.

On the "actual cost" side, #702 step 3 built exactly this kind of harness —
but only for a fixed, committed corpus: `plan_cost_calibration` (tests.rs
~3077) times each applicable plan min-of-n to fit the model's constants, and
`plan_cost_model_matches_gold` (tests.rs ~3185) checks argmin-vs-gold on that
same corpus. Both are validation benches, not tools — there's no way to ask
"what would every plan cost, predicted and actual, for *this* query I'm
looking at right now" without writing a new `#[bench]`.

## Two primitives

- **`explain(query) -> Vec<(PhysicalPlan, PredictedCost)>`** — enumerate
  `PhysicalPlan::ALL.filter(|p| p.applicable(...))`, call `cost::plan_cost`
  on each, return the ranked list. This is just exposing numbers the router
  already computes; it costs nothing beyond what every query pays today, so
  it's safe to call constantly (dev builds, tests, an internal endpoint).
- **`explain_analyze(query, num_warmups, num_trials) -> Map<PhysicalPlan,
  Vec<Duration>>`** — actually run each applicable plan via
  `run_query_with_plan`, `num_warmups` discarded rounds then `num_trials`
  recorded rounds, and return the **raw per-trial timings**, not a
  pre-reduced median. Loop shape:

  ```
  for round in 0..num_warmups + num_trials:
      order = rotate(applicable_plans, round)   # not the same order every round
      for plan in order:
          t = time(run_query_with_plan(plan, ...))
          if round >= num_warmups: record(plan, t)
  ```

  Rotating which plan goes first/last each round (rather than a fixed
  `a();b();c()`) avoids handing one plan a systematic advantage from
  whatever accumulates round-over-round (allocator state, cache residency).
  Returning raw timings rather than a summary lets the caller compute a
  median themselves and — as important — see whether a plan's timing is
  bimodal, which this engine has measured happening on identical work
  before ([00648-engine-verifier-cost-ordering.md](done/00648-engine-verifier-cost-ordering.md)'s
  measurement-traps section). Given plans here run sub-few-ms, 3 warmups +
  10 trials × 6 plans is a small fraction of a second per call.

## Mis-plan signal

`argmin(predicted)` vs `argmin(median(actual))` disagreeing is the thing
worth flagging — generalizing `plan_cost_model_matches_gold` from "pass/fail
against one frozen corpus" from a manual recalibration run into something
callable against any query on demand. Run over the existing survey corpus
(`scripts/survey_queries.py`) and the mismatch rate becomes a standing
health check for the cost model between the periodic recalibration benches
#702 already calls for.

## Open correctness question before trusting the timings

`run_query_with_plan` takes `filter: &mut FilterExpr` (lib.rs ~5900) —
mutable, not `&`. The verifier pipeline already has at least one step
(`memoize_text_predicates`, per the verifier-cost-ordering doc) that mutates
cost-relevant state on the filter tree as a side effect of running. Before
trusting `explain_analyze`'s relative timings under interleaving, confirm
either that each plan invocation gets a fresh/cloned `FilterExpr` or that
repeated invocation against the same mutated tree is idempotent and doesn't
advantage whichever plan happens to run first in a round. If it isn't, the
rotation above only partially compensates — the fix would be cloning
`filter` per call, at some extra cost per round.

## Where to expose

Land as an in-process Rust function first — callable from tests, benches,
and a small standalone binary — mirroring how the force/dispatch seam
itself started internal-only. Whether it later grows an HTTP surface (an
internal/dev-only Falcon route, gated off in prod) is a separate, later
decision. It should never sit on the default request path: it multiplies
work by the number of applicable plans (typically 2–4 of the 6 variants),
which is fine for an on-demand debug call and wrong for every query.

## Non-goals

- Not a routing mechanism — the router already routes on `argmin
  cost(plan)`; this sits alongside it as a diagnostic, not a replacement.
- Not a replacement for the committed calibration corpus — `plan_cost_calibration`
  and `plan_cost_model_matches_gold` stay as the CI-facing regression gate;
  `explain_analyze` is for ad hoc/interactive use and off-CI recalibration
  runs (per #702's "Keeping costs/plans current" section).
- No join-order search or new plan enumeration — same non-goal #702 already
  states; this only makes the existing fixed plan set individually
  measurable on demand.

## Related

- [00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md) —
  the cost model, `PhysicalPlan`, `run_query_with_plan`, and the calibration
  harness this builds on.
- [00648-engine-verifier-cost-ordering.md](done/00648-engine-verifier-cost-ordering.md) —
  source of the interleaved-measurement discipline and the documented
  bimodal-timing / env-var-size measurement traps.
- [docs/prs/verifier-cost-ordering.md](../prs/verifier-cost-ordering.md) —
  the three-rounds-of-manual-measurement process this would make repeatable.
