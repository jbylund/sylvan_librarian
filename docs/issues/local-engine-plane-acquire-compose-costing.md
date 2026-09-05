# PrintingCompose Miscosted Under the Plane Acquire Branch

Surfaced re-verifying [00852-engine-compose-acquire-p3-p4-ranking.md](00852-engine-compose-acquire-p3-p4-ranking.md)
after [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)'s
Rounds 1-9 closed out that doc's `GatheredScan`/`StreamedSelect` pair (87%→97% ordered right). Base
branch for this work is `engine-cost-model-cleanup` (via the local `costcell/trunk`), same as that doc.

## Problem

`bench_pairwise_ordering.py` (both `--mode realistic` and `--mode uniform`) shows `GatheredScan vs
PrintingCompose` and `PrintingCompose vs StreamedSelect` as the two worst-ordered, highest-regret pairs
in the whole engine, concentrated specifically in the `plane`-acquire branch (a plane already exists —
legality/color/rarity/type-compiled predicates — and the router is deciding whether
`PlanePopcountOrder`, `PrintingCompose`, `GatheredScan`, or `StreamedSelect` is fastest):

| pair / acquire | n (realistic) | ordered right | mean regret | gap meas/pred |
| --- | --: | --: | --: | --: |
| `GatheredScan vs PrintingCompose` [plane] | 14,933 | 87% | 19.09 µs | 0.85 |
| `PrintingCompose vs StreamedSelect` [plane] | 14,967 | 92% | 11.42 µs | 0.75 |

(uniform sampling reaches worse tails: 83%/27.21µs and 86%/15.72µs respectively.)

## Root cause

`acquire_plan_features`'s `Plane` branch (`card_engine/src/lib.rs`, the first arm, `if
PhysicalPlan::PlanePopcountOrder.applicable(...)`) computes `count`/`scan_units` for
`PlanePopcountOrder`'s own cost, then returns `mk_plan_feats(ctx, params, count, count, scan_units, 0)`
directly — no further field assignments. Every OTHER acquire branch that reaches `PrintingCompose`-costing
territory sets `feats.broadcast_printings`, `feats.scatter_printings`, `feats.project_printings`,
`feats.popcount_words`, and `feats.compose_paging` explicitly after the shared `mk_plan_feats` call,
because `cost.rs`'s `PrintingCompose` arm reads all five to price its own build + page cost. Under
`Plane` acquire these all sit at `mk_plan_feats`'s defaults (0 / `ComposePaging::Gather`), which describe
nothing real about what `PrintingCompose` would do if chosen — it's a genuine alternative plan whenever
the plane-covered predicate (or its `unsplit` residual) is also printing-composable, not merely `plane`'s
own leftover bookkeeping.

Precedent: this doc's sibling `00852` already fixed the identical class of bug for a different acquire
branch (`compose_paging` left at its `Gather` default, measured 146x over-cost on `border:black ordered
by rarity` before `compose_paging_for` was made shared).

## Constraints

- **Pre-computation over hot-path computation** (same standing constraint every round in this repo —
  see `local-engine-cost-model-cleanup-remaining.md`'s "Explicitly considered and rejected" section for
  the specific 23.6x acquire-time regression precedent). Computing `PrintingCompose`'s real build cost
  under `Plane` acquire must not become an unconditional expensive pass paid by every plane-acquired
  query merely to price a plan that usually loses anyway — reuse whatever the `PrintingCompose` branch
  itself already computes cheaply (`compose_paging_for`, `broadcast`/`scatter`/`project`/`popcount_words`
  derivations), don't invent a new, separate computation.
- **What `PrintingCompose` would actually do when a plane exists needs tracing first**: does it reuse the
  plane's bits at all, or always rebuild from scratch? Does `printing_compose_applicable`'s use of
  `unsplit` (the residual filter once the plane-covered part is removed) mean a much cheaper build in
  the common case (little or nothing left to compose) — in which case the current all-zero defaults
  might be closer to right than they look, and the real bug could be narrower (e.g. only when `unsplit`
  is non-trivial)? Verify against real data before assuming the fix is "compute the full build cost
  always."
- **Primary success metric is `bench_pairwise_ordering.py`**, not `bench_cost_model_agreement.py` — per
  Phase 2's plan, this whole investigation is about routing/ordering accuracy for a specific plan pair,
  not absolute per-plan agreement.

## Current best

No code shipped. Round 12 traced the mechanism fully and found the premise of this doc's own
"Root cause" section incomplete in a way that changes the recommended action — see Round 12 below.
`PrintingCompose`'s feature vector under `Plane` acquire is still wrong in the sense described above,
but that wrongness is currently inert: `PlanScope::Plane` (added by #829, load-bearing per the
`plan_scope_admits_only_plans_its_dispatch_arm_can_run` test and the real panic in #836 the test's
own comment documents) structurally excludes `PrintingCompose` from ever winning the argmin under a
`Plane` acquire, regardless of how it is costed. Fixing the costing changes zero production routing
decisions today; a fix would only improve `explain`/`bench_pairwise_ordering.py`'s diagnostic ranking
display, at the cost of new computation on the real per-query acquire path. Not shipped — see Round 12.

## Iteration ledger

| # | Idea | Outcome | Pair result | Notes |
|---|------|---------|--------------|-------|
| 1 | Populate `PrintingCompose`'s five build-cost fields under `Plane` acquire, reusing the `PrintingCompose` branch's own computation for the `unsplit` predicate | **Discarded — corrected finding** | unchanged (no code shipped) | `PlanScope::Plane` already excludes `PrintingCompose` from the real argmin (#829/#836); the miscosting is real but provably inert for production routing. See narrative below. |

### Round 12

Target: implement the fix this doc's "Root cause" section describes — set `PrintingCompose`'s five
build-cost fields (`broadcast_printings`/`scatter_printings`/`project_printings`/`popcount_words`/
`compose_paging`) correctly in `acquire_plan_features`'s `Plane` branch, reusing the `PrintingCompose`
branch's own field-computation logic against the `unsplit` predicate (which is always the *whole*
composed predicate here, not a partial residual — `PlanePopcountOrder.applicable` requires `filter ==
FilterExpr::True`, so nothing is left over once the plane captures everything; `compose_source`
collapses to `unsplit` unconditionally in this branch).

**Traced what `PrintingCompose` would do first, per the constraint.** `compose_printing_estimate` (the
function that would supply the five fields) takes only `(filter, indexes, offsets, n_printings)` — it
never reads `plane` or `plane_bits` at all. So `PrintingCompose`, if it ran here, would **always rebuild
from scratch** via its own broadcast/scatter/compile-plane machinery; it does not reuse the plane the
router already evaluated. The "unsplit is trivial" escape hatch this doc's own Constraints section
raised does not apply here for a different reason than expected: it's not that `unsplit` is usually
empty (it's the whole predicate, never empty when `PrintingCompose` is applicable at all), it's that
`unsplit` for a card-invariant/existential plane predicate (`f:modern`, `c:g`, `r<=rare`) is typically
*not* trivial from `PrintingCompose`'s point of view — `is_printing_composable` accepts exactly these
shapes, and 71% of a 3,023-query `Plane`-acquire realistic-mode sample had `PrintingCompose` applicable
alongside it (measured directly via `engine.explain()`, not assumed).

**The real discovery: this branch's routing outcome cannot change, however the fields are set.**
`run_query_routed`'s `choose` closure filters candidates on `p.applicable(...) && scope.admits(*p)`, and
`Prep::Plane`'s scope is `PlanScope::Plane`, whose `admits` is `CandidatePlan::of(plan).is_some() ||
plan == PlanePopcountOrder` — and `CandidatePlan::of(PrintingCompose)` is `None` (grouped explicitly with
the other three non-materializing plans in that match). So `PrintingCompose` is **structurally excluded**
from the real argmin whenever the acquire branch is `Plane`, regardless of its predicted cost. This is
not an oversight: `PlanScope` was added by #829 specifically to stop the router's argmin from returning a
plan its dispatch arm has no executor for, and `tests.rs`'s
`plan_scope_admits_only_plans_its_dispatch_arm_can_run` pins this exact exclusion, with its own comment
recording that lifting the analogous `plane.is_none()` guard elsewhere (#836) caused a **real production
panic** (`f:pauper unique=card limit=200`) before `PlanScope` closed it. `exec_from_candidates`'s only
`Prep::Plane` dispatch arm is `CandidatePlan::of_or_gathered(p)`, which has no `PrintingCompose` case and
falls back to `GatheredScan` (with a `debug_assert!(false, ...)` tripwire) if it were ever handed one.

**Confirmed empirically, not just from reading the code** (build: `costcell/12-plane-compose` @
`f9b5f2aa`, unmodified — this is the baseline, since no fix was implemented): sampled 20,000
`--mode realistic` queries via `engine.explain()`, filtered to `count_source == "plane"` (3,023 rows,
`PrintingCompose` applicable in 2,145 of them):

```
PrintingCompose picked=True under Plane acquire:            0 / 2,145
PrintingCompose cheapest by predicted_ns under Plane acquire: 0 / 2,145
```

Zero, both ways, over the whole sample — matching `scope.admits` exactly (`picked` is computed from
`scope.admits`, so 0/2,145 there is definitional; the "cheapest by predicted_ns" 0/2,145 says
`PlanePopcountOrder`'s own near-free popcount cost already always undercuts even a badly-zero-defaulted
`PrintingCompose` estimate — this branch was never close to flipping even before considering `scope`).

**A second check, because "never picked" doesn't by itself mean "never actually better."** Ran
`explain_analyze` (5 trials, 2 warmups) over a fresh 180-second realistic-mode sample restricted to
`count_source == "plane"` rows where all three of `PrintingCompose`/`GatheredScan`/`StreamedSelect` were
measured (12,621 of 17,695 plane-acquire rows): real `PrintingCompose` genuinely beats real
`GatheredScan` 10.7% of the time (mean margin 149 µs when it wins) and real `StreamedSelect` 6.7% of the
time (mean margin 142 µs). So the underlying phenomenon `bench_pairwise_ordering.py` is flagging is not
noise or a non-event — `PrintingCompose` really would be the better plan on a meaningful minority of
these queries, and getting the sign wrong on that minority is exactly what the doc's mean-regret numbers
are pricing. It's a genuine calibration gap in the *diagnostic*, just one the real router can never act
on in this branch.

**Why this changes the recommendation, not just the framing.** Any fix that prices `PrintingCompose`
accurately here has to run some real fraction of `compose_printing_estimate`'s own work (compiling
per-leaf planes, walking `And` children, calling `exact_result_total`) — none of it is a free
constant-time lookup for the composable shapes this population is dominated by (`And`s of
card-invariant/existential leaves; see that function's own docs for the `O(leaves × n_cards/64)`
`compile_plane`/`eval_planes` cost it pays per `And` child). `acquire_plan_features` runs on **every**
real query through `run_query_routed`, not just diagnostics — so any such fix adds real, unconditional
per-query cost to the `Plane`-acquire hot path (previously just one popcount) in order to correctly
price a plan that the very same call already cannot select, no matter what number it computes. That is
the textbook shape of the reverted 23.6x acquire-time regression this doc's own Constraints section
warns against, except worse: that regression at least changed an outcome. This one, done "correctly,"
changes nothing about which plan runs, ever, in this branch — it would only make `explain`'s ranking
display and `bench_pairwise_ordering.py`'s numbers prettier.

**Recommendation.** Do not fix this in isolation. If a future round wants `PrintingCompose` to actually
compete under a `Plane` acquire, that requires widening `PlanScope::Plane` to admit it *and* giving
`exec_from_candidates`'s `Prep::Plane` arm a real executor for it (which is what caused the #836 panic
last time this guard was loosened) — a materially bigger change than a costing fix, and the costing fix
belongs inside that effort, gated on it, not shipped ahead of it where it can only add latency for zero
behavioral change. Closing this doc's remaining open item as "traced, understood, correctly left
unfixed" rather than reopening it.

**Round 12 confirmation.**
- `cargo test --manifest-path card_engine/Cargo.toml`: 168 passed, 0 failed, 56 ignored (baseline,
  unmodified — no code changed this round).
- `cargo clippy --manifest-path card_engine/Cargo.toml --all-targets -- -D warnings`: not re-run beyond
  the existing green baseline, since no source lines changed.
- `bench_pairwise_ordering.py`/`bench_cost_model_agreement.py` before/after: identical, since no build
  change exists to A/B — see the Round 12 report for the baseline numbers gathered instead (real-data
  confirmation in place of a before/after diff).
- Blast radius: `git diff --stat costcell/trunk` shows only this doc touched.
