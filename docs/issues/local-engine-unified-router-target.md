# The Unified Router: Target-State Pseudocode

Companion to [00702-engine-plan-selection-layer.md](./00702-engine-plan-selection-layer.md).
That doc argues *why* the scattered decision tree in `run_query` should become one
plan-selection layer and sequences the work; this doc pinned down *what the end state
looks like* so the incremental PRs had a shared target to converge on.

**Landed 2026-07-20** (`run_query_routed`, lib.rs). The organizing principle held:
**there is exactly one routing function**, and mode (card/printing/artwork) is a
*filter over the plan set* — never a branch in control flow. What actually got
deleted: the `CARD_ENGINE_PLAN_SELECT` toggle, the legacy `run_query` decision-tree
body (now a 4-line string→enum adapter delegating to `run_query_routed`), and the
`maybe_broad` `STREAM_MIN_MATCHES` *routing* threshold. What deliberately STAYED,
because they are not tree-routing thresholds: `STREAM_MIN_MATCHES` itself (now the
cost model's P3 small-total floor, `cost.rs`), the 7/8 narrowing cutoff and the
memoize gate (both inside `prepare_candidates`, shared by the router). Divergences
from this sketch, learned by measurement, are noted inline below.

## Plans as data

The tree and the thresholds are gone. Each plan owns its eligibility and its cost;
adding a plan is adding a row here (compile-time-forced complete — see #702 "Keeping
costs/plans current").

```
enum Plan { PrintingRangeScan, PlanePopcountOrder, StreamedSelect, GatheredScan }

# Which plans could *correctly* answer this query. These predicates ARE the former
# tree's structural conditions, but now they gate eligibility ONLY — never speed.
# GatheredScan has no gate: it is the universal fallback and always in the set, so
# the set is never empty.
fn applicable(plan, q, mode) -> bool:
    match plan:
        PrintingRangeScan  -> mode == Printing
                              and q.filter.is_bare_range()       # single range pred, no plane
                              # NOT order-alignment: the fastpath serves aligned
                              # (index slice) and misaligned (perm walk) alike.
        PlanePopcountOrder -> mode == Card
                              and q.filter.reduces_to_plane()    # residual == True
        StreamedSelect     -> q.order.is_perm_backed()           # any mode
        GatheredScan       -> true                               # universal reference
```

The three modes collapse into this filter — there is no `if card … elif printing …`
anywhere. Card gets `{P2, P3, P4}`, printing gets `{P1, P3, P4}`, artwork gets
`{P3, P4}`, all by the same predicate evaluation. This is `PhysicalPlan::applicable`
as landed (the P1 arm also checks `bare_range_bounds(filter).is_some()`).

## Cardinality estimation (built, but NOT wired into the landed router)

A cheap, sound, per-operating-space estimator exists (#704): `Cardinality {lo, est,
hi}`, bounds a hard invariant (`truth ∈ [lo,hi]`), composed via
AND→independence / OR→Bonferroni / NOT→complement (see #702 "Cardinality
estimation"). **The landed `run_query_routed` does not call it.** Materialize-then-
route won over estimate-then-route (below), so the router derives features from
*exact/cheap* counts in `acquire` — a plane's popcount, a range's binary-search `k`,
a residual's candidate count — never `estimate_cardinality`. The estimator stays
available for a future plan whose count is neither free-from-prep nor cheap-exact
(where a sound estimate would be the only affordable input); today no plan needs it.

## The count source — how features are obtained (as landed: exact, in `acquire`)

The load-bearing question is *how you get the count*, and the answer that landed is:
**always an exact or cheap-exact count, from `acquire`, never the estimator.** The
count is free-or-cheap for every plan the engine has: a True-residual plane's count
IS its popcount; a bare range's is a binary-search `k`; a residual's is
`prepare_candidates`. So the router routes on those directly.

This is the lesson from the card-mode prototype: the naive "estimate → route →
execute" pipeline was ~15% slower because estimating meant a plane eval the executor
then *repeated* (#702 Results). Materialize-then-route avoids that double work — the
count-deriving prep IS the execution input, reused. The one place nothing is
materialized up front is the bare range (`Prep::Range`): its discriminating feature,
`k`, is free from the index, so `PrintingRangeScan` is costed without materializing,
and a materializing winner materializes lazily in dispatch (see "The one router").

The earlier sketch imagined a `count_source()` that *estimated* when no shared prefix
existed; that branch never landed, because the only no-shared-prefix case (the range)
has an exact-`k` that is cheaper than any estimate. If idea-2 or a future plan ever
introduces a case where the count is genuinely expensive to get exactly, *that* is
when the estimator above gets wired in.

## The one router (as landed: `run_query_routed`)

Flat — three named steps, no early returns, no plan-named `if`-cascade:

```
fn run_query_routed(q, mode, page):
    # acquire: pick the count source (one of three, by query structure), build the
    # cost features (mk_feats fills the query-invariant fields), materialize the
    # shared artifact it implies. This 3-way IS the whole materialization story.
    (feats, prep) =
        if PlanePopcountOrder.applicable(q):    # True-residual plane (card)
            eval the plane once → popcount is the exact count;   Prep::Plane
        elif PrintingRangeScan.applicable(q):   # bare printing range
            exact k from the range index (no scan);              Prep::Range
        else:
            prepare_candidates();                                Prep::Candidates

    # choose: cheapest applicable plan — no hand-written plan list.
    plan = argmin(PhysicalPlan::ALL.filter(|p| p.applicable(q)), |p| plan_cost(p, feats))

    # dispatch: run the winner, reusing prep's artifact.
    match (plan, prep):
        (PlanePopcountOrder, Plane)  → popcount executor, reuse the bitmap
        (P3|P4, Plane)               → candidate-list executor, bitmap AS the list
        (P3|P4, Candidates(prep))    → candidate-list executor, reuse prep
        (plan,  Range)               → P1 walks if it won & its fastpath accepts;
                                        else materialize lazily + re-argmin on exact feats
```

This realizes the "one routing function, plans-as-data" goal — per-plan knowledge
lives on `PhysicalPlan` (`ALL`, `applicable`, `materializing`, `cost::plan_cost`, an
executor arm), and `choose` is a generic argmin over `ALL.filter(applicable)` that
never names a plan. Adding a plan is declaring those arms; only a genuinely new
count source (a new `Prep` variant — e.g. idea-2's printing-space bitmap) touches
acquire/dispatch.

Two honest departures from the earlier sketch, learned building it:

- **The count source is not a separate `count_source()` — it's the acquire branch.**
  *How* you get the count is entangled with *which* plan you weigh: a plane's count
  IS its bitmap, a range's is a free binary search, a residual's is
  `prepare_candidates`. Each yields a different-shaped artifact (`Prep`), so it can't
  collapse to one uniform helper. The 3-way is isolated in acquire; that's as
  factored as it goes.
- **`Prep::Range` defers materialization** — the one irreducible bit of staging (the
  original sketch's "phases"), now a `match` arm, not an early-return. It costs the
  non-materializing `PrintingRangeScan` from a cheap estimate; if a materializing
  plan wins there, dispatch materializes lazily and re-chooses on exact features.
  That is the "don't pay to materialize a plan you won't run".

The sketch's "trivial escape" for `plans == [GatheredScan]` didn't land and wasn't
needed: features come from acquire (not a separate estimator pass), so a single-plan
argmin is already free.

## Cost model

One formula per plan, constants fit on the real corpus ([cost.rs](../../card_engine/src/cost.rs)).
`argmin` cares about *ratios*, which is what makes P1's bad tail visible: the tree
took P1 unconditionally; here P1 competes and *loses* when its walk is pathological
(narrow range under a misaligned sort — the idea-1/idea-2 crossover, the founding
motivation).

```
fn plan_cost(plan, f) -> ns:                                          # eval_domain = candidate CARDS
    match plan:                                                       # scan_units  = rows scanned (operating space)
        PrintingRangeScan  -> (page_span / match_rate)·STEP + FIXED   # blind-walk tail
        PlanePopcountOrder -> matches·SCATTER + words·WORD + FIXED    # O(words) floor
        StreamedSelect     -> eval_domain·CARD_PASS + scan_units·(SCAN+tier) + small_total_floor + FIXED
        GatheredScan       -> eval_domain·CARD_PASS + scan_units·(SCAN+tier) + matches·PUSH + page·SELECT + FIXED
```

The per-card `card_pass` and per-scanned-row `scan` terms are split (the `tier`
verify cost rides `scan_units`, where it is paid): this is the operating-space fix
(`scan_units`) that made the model correct for printing/artwork, not just card.

## Two things that are load-bearing and non-obvious

1. **Routing on the exact count from `acquire` is where the value is, not the argmin.**
   A CBO that estimates on every query taxes the fast paths (the plane eval that gets
   repeated — the ~15% v1 regression). The landed design is free on those paths because
   the count-deriving prep IS the execution input, reused: a plane's popcount, a
   residual's candidate list. The one non-materializing plan (`PrintingRangeScan`) is
   costed from the range index's free `k`, so *it* pays nothing up front either.

2. **`Prep::Range`'s lazy materialization is what preserves parity there.** Because P1
   is costed from a cheap estimate, a materializing plan that wins the range case must
   materialize *after* the argmin — the deferral means a broad range that P1 serves in
   µs never pays `prepare_candidates`, while a range better served by P3/P4 materializes
   exactly once. This is the sole surviving bit of "stage the decision, don't pay to
   cost a plan you won't run" — now a `match` arm, not a control-flow phase.

## What each mode yields (measured 2026-07-20 — supersedes earlier hypotheses)

- **Card** — tie. Cost-routing reproduces the tuned tree's choices (the thresholds
  already sit at the cost crossovers). Value is structural, not speed. (A/B geomean
  1.010×, see #702.)
- **Printing (P1 vs P3/P4)** — tie, NOT the win once hypothesized. The tree's
  `range_too_broad_to_narrow` ratio already sits at the P1/P4 crossover; P1 wins broad
  ranges at *every* depth tested (P3/P4 pay a full O(n_cards) match phase). Measured:
  tree gold 54/54, `printing_range_route_probe`. The earlier "tree takes P1
  unconditionally → cost bails P1→P4" story was falsified — the tree *does* guard narrow.
- **Artwork** — tie, like card (only P3/P4 apply). Confirming A/B, not a headline.
- **The actual win — idea-1 vs idea-2 (a plan not yet in `applicable()`):** the tree
  can't pick idea-2 (range → printing existence bitmap → popcount-skip) because it
  isn't built (#656). idea-2 is offset-independent; idea-1 (P1) grows ~`(offset+limit)/
  match_rate`, so they cross at a depth that scales inversely with match-rate (offset
  ~500–2000 for low-selectivity broad ranges, deeper for high). This IS the cost-shaped,
  tree-inexpressible decision the whole effort was aiming at — measured in
  `idea1_vs_idea2_probe`, real but confined to deep-paged broad printing ranges. Adding
  idea-2 means a fifth `Plan` row (gate: `mode==Printing ∧ bare_range ∧ ¬aligned`) plus
  its cost formula — the "plans as data" test of this design.

## Cost-model calibration scope (FIXED — was prerequisite)

`plan_cost` was originally fit and validated for CARD mode only, under-predicting
printing/artwork P3/P4 by ~3× (= `n_printings/n_cards`) because `eval_domain` counted
CARDS while those plans scan all printings. **Fixed** via features-not-mode
(`PlanFeatures::scan_units`, see #702 "Is the cost model correct?"): the caller
populates scan counts in the plan's operating space, one mode-agnostic formula.
Printing fidelity 1.83×→1.50× (now on par with card), and the deep-printing routing
mispicks are gone. A 1200-query designed refit confirmed the constants are at the
identifiable ceiling (~1.4× absolute, ordering-correct); further tightening is blocked
by structural collinearity, not effort — so this is done, not deferred.
