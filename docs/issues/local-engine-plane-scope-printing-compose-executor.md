# Giving PrintingCompose a Real Executor Under Plane Acquire — Measured, Not Worth It

## Context

[local-engine-plane-acquire-compose-costing.md](local-engine-plane-acquire-compose-costing.md) (Round
12) found that `PrintingCompose` is structurally excluded from ever winning the router's argmin under a
`Prep::Plane` acquire — `PlanScope::Plane::admits` is `CandidatePlan::of(plan).is_some() ||
matches!(plan, PhysicalPlan::PlanePopcountOrder)`, and `CandidatePlan::of(PrintingCompose)` is `None`.
That exclusion exists because of [00829-engine-plane-acquire-plan-mismatch.md](done/00829-engine-plane-acquire-plan-mismatch.md),
a real production panic: before `PlanScope` existed, the argmin could return a plan the `Prep::Plane`
dispatch arm had no executor for, and `exec_from_candidates` met it with `unreachable!()`.
[done/local-engine-plan-misselection.md](done/local-engine-plan-misselection.md) is the precedent for the
general shape of fix — a non-materializing plan reconsidered on a fallback path, gated on the model so
it can never be worse than what it replaces — and
[00852-engine-compose-acquire-p3-p4-ranking.md](00852-engine-compose-acquire-p3-p4-ranking.md) is the
sibling investigation whose re-verification is what surfaced this gap in the first place.

This doc was commissioned to design the fix Round 12 left open. **It recommends against building it.**
Section "The opportunity, quantified" below re-measures the population Round 12 characterized and finds
the win rate against the plan actually holding the argmin today, `PlanePopcountOrder`, is 0% across
3,209 freshly measured rows in four independent sampling regimes. The rest of the doc still designs the
executor path and argmin/dispatch mechanics in full, per the brief, in case a narrower slice this sweep
missed turns up real value later — but the headline finding is: don't build this.

## The opportunity, quantified

Round 12 measured real `PrintingCompose` against real `GatheredScan` and `StreamedSelect` under `Plane`
acquire (10.7% / 6.7% win rates) and separately showed `PrintingCompose` is never *predicted* cheaper
than `PlanePopcountOrder` there (0/2,145, from the cost model). **What it never did was measure real
`PrintingCompose` against real `PlanePopcountOrder`** — the plan that is actually admitted and actually
wins the argmin under `Plane` acquire today. That is the comparison that decides whether this whole
effort has a payoff, since `PlanePopcountOrder` is always an applicable, always-admitted competitor
whenever `Prep::Plane` is reached at all (it is the condition that puts a query there — see
`plane_popcount_order_applicable`, `card_engine/src/lib.rs:9393`).

Re-measured directly with `engine.explain_analyze` (spike script, not shipped — see "Spike" below),
sampling via `client.query_sampler.QuerySampler` against the 97,812-printing `benchmarks/bitplanes`
corpus, filtering to `count_source == "plane"` rows where `PrintingCompose`'s own fastpath didn't
decline (so it actually produced a measured page, not just a structural "applicable" mark):

| sampling regime | n (plane+compose measured) | PrintingCompose beats PlanePopcountOrder |
| --- | --: | --: |
| realistic, `prefer=default` | 1,123 | **0 (0.0%)** |
| realistic, `prefer` varied | 1,107 | **0 (0.0%)** |
| uniform | 415 | **0 (0.0%)** |
| realistic, limits swept 10→1,000,000, offsets 0→50,000 | 564 | **0 (0.0%)** |
| **total** | **3,209** | **0 (0.0%)** |

`PrintingCompose` genuinely does beat `GatheredScan` (58-74% of the time across these runs) and
`StreamedSelect` (26-56%) — Round 12's numbers were real, just answering the wrong question for this
purpose. It never beat `PlanePopcountOrder`, not once, in any regime, at any limit or offset.

The `compose_ns / plane_ns` ratio (>1 = `PlanePopcountOrder` cheaper) narrows as limit grows — the
one place a fundamentally different plan might have a chance, since `run_query_streamed_popcount`'s
popcount-skip walk and a hypothetical large-page compose gather both do more work as the page grows —
but never crosses 1.0:

| limit | n | min ratio | p50 ratio | max ratio |
| --: | --: | --: | --: | --: |
| 10 | 111 | 5.83 | 23.80 | 364.61 |
| 175 | 128 | 7.57 | 24.25 | 355.83 |
| 5,000 | 119 | 2.23 | 16.06 | 347.22 |
| 100,000 | 98 | 1.55 | 15.17 | 339.00 |
| 1,000,000 | 108 | **1.43** | 12.70 | 330.44 |

The closest `PrintingCompose` ever got, across every row in every regime, was 1.43x slower, at the
largest limit tested. It was never faster.

**`limit=1,000,000` is not an arbitrary large probe — it's the real ceiling, and the sweep already covers
it exactly.** `api/api_resource.py`'s `limit=None` path substitutes this exact literal
(`limit=limit if limit is not None else 1_000_000`) for an uncapped `GET /?q=...`, and the engine clamps
any limit to the true cardinality regardless (`offset.saturating_add(limit).min(v.len())`,
`card_engine/src/lib.rs:5690` at investigation time) — printings top out at 97,206 in this corpus, cards
at 31,508. So every limit above ~100k, including the one tested, degenerates to the identical "return
everything" case; there is no larger, more extreme regime to test, because the API's own no-cap
sentinel already sits ~10x past the true ceiling. The sweep's largest data point isn't a sample of the
realistic range, it's the realistic range's actual maximum, reached exactly.

**Why the margin is structural, not incidental.** Reaching `Prep::Plane` at all requires `filter ==
FilterExpr::True` after `bind_and_split_filter` — the whole predicate folded into the plane — and
`plane_popcount_order_applicable` additionally requires a card-length sort permutation to exist for the
requested orderby. `PlanePopcountOrder`'s "build" is the single `eval_planes` call `acquire_plan_features`
already paid to determine the query belongs in `Prep::Plane` in the first place (`card_engine/src/lib.rs:
11899-11901`) — dispatch reuses that bitmap for free (`exec_plane_popcount_order_with_bitmap`, line 9732).
`PrintingCompose`, if it ran here, would pay an entirely separate, unrelated build
(`compose_printing_bits`, line 7298) from scratch, in PRINTING space, then derive a card bitmap back out
of it (`printing_bits_to_card_bits`) to answer the same card-mode question `plane_bits` already answers —
see "Design" below for why this can't be fixed by sharing inputs. `PlanePopcountOrder` isn't winning
because it's better-calibrated; it's winning because its one input was already free and `PrintingCompose`'s
isn't.

### Spike (not shipped)

Wrote and ran (then discarded, per the brief) a script sampling `Plane`-acquire queries via
`QuerySampler`, calling `engine.explain_analyze` per query and comparing `min(trials_ns)` across all four
plans. Four runs: realistic/default-prefer, realistic/prefer-varied, uniform, and a limit/offset sweep
(10 through 1,000,000, offsets 0 through 50,000) built by monkeypatching `costbench.LIMITS`/`OFFSETS`.
Total 3,209 usable rows, 0 `PrintingCompose` wins against `PlanePopcountOrder` in any of them. The script
lived at `scratchpad/spike_plane_compose.py` and `scratchpad/spike_plane_compose_biglimit.py` outside the
worktree; nothing from it is committed.

One loose end from the spike, noted rather than chased further given the finding above already settles
the question this doc was commissioned to answer: my measured "applicable and didn't decline at runtime"
rate was 18.3% of plane rows (1,123/6,120), well under Round 12's 71% "applicable" figure
(2,145/3,023). The two numbers likely aren't measuring the same thing — Round 12's came from `explain()`'s
structural `applicable` bit on the `PlanEstimate` list, mine required the fastpath to actually produce a
page in `explain_analyze` (i.e., not decline on a sparse total or similar). Left open; it doesn't change
the 0/3,209 result, since a fastpath that declines can't win regardless of how it's counted.

## The historical constraint

[#829](https://github.com/jbylund/sylvan_librarian/pull/829) is why `PrintingCompose` can't simply be
admitted today. Before the fix, `run_query_routed`'s argmin was `ALL.filter(applicable)`, with no notion
that `applicable` (a correctness predicate about the query) says nothing about which artifact the acquire
step actually materialized. A `Prep::Plane` acquire holds only the plane bitmap, and its dispatch arm
could run `PlanePopcountOrder` or the two candidate-list executors (`StreamedSelect`/`GatheredScan`) —
nothing else. When [#836](https://github.com/jbylund/sylvan_librarian/pull/836) lifted a `plane.is_none()`
guard so compose could cost the `unsplit` filter alongside a plane, the argmin started legitimately
returning `PrintingCompose` under `Prep::Plane`, and `exec_from_candidates`'s match — which only knew
`StreamedSelect`/`GatheredScan` — hit `unreachable!()`. Real panic, on `f:pauper unique=card limit=200`
and other shapes, confirmed reachable at production corpus sizes.

The fix: `PlanScope` (`card_engine/src/lib.rs:9307`) narrows the argmin to exactly what the current
acquire's dispatch arm can run, keyed off `Prep::scope()` (line 9336). `CandidatePlan` (line 9144) turns
the P3/P4 executor pair into an exhaustive type so `exec_from_candidates`'s match can't silently regain an
`unreachable!` arm; `CandidatePlan::of_or_gathered` (line 9181) is the belt-and-suspenders fallback if
`PlanScope` and dispatch ever disagree again (`debug_assert!(false, ...)`, degrades to `GatheredScan`
rather than panicking). The test that pins this invariant is
`plan_scope_admits_only_plans_its_dispatch_arm_can_run` (`card_engine/src/tests.rs:3657`) — it asserts,
for every `PhysicalPlan`, that `PlanScope::Plane.admits(plan) == (CandidatePlan::of(plan).is_some() ||
plan == PhysicalPlan::PlanePopcountOrder)`. **Any change that widens `PlanScope::Plane` must change this
assertion in the same commit**, and must not do so without also giving the `Prep::Plane` dispatch arm a
matching executor — that pairing is the entire lesson of #829, and it's what a hypothetical
implementation of this doc's design would have to get right.

## Proposed design (for the record, not recommended for implementation)

### Can PrintingCompose reuse the plane bits instead of rebuilding?

Traced, not assumed. `plane_bits` (`Prep::Plane`'s artifact) is a **card**-space bitmap: `eval_planes`
(`card_engine/src/planes.rs:1531`) compiles the query's `PlaneExpr` into `n_cards` bits, one per card,
existentially — "does this card have some printing that satisfies the predicate." `PrintingCompose`'s own
build, `compose_printing_bits` (`card_engine/src/lib.rs:7298`), produces a **printing**-space bitmap —
`n_printings` bits — by recursively composing leaf indexes (`legality_leaf_bits`, `rarity_cmp_leaf_bits`,
`border_leaf_bits`, `broadcast_card_bits_to_printings`, range scatters, …) over `compose_source(filter,
unsplit, plane)`, which under a `Plane` acquire is `unsplit` — the whole pre-split predicate, never a
residual (Round 12 already established this: `PlanePopcountOrder.applicable` requires `filter ==
FilterExpr::True`, so nothing is left over for compose to see as a "residual"). Neither `compose_printing_bits`
nor its cost-estimate counterpart `compose_printing_estimate` (line 7680) reads `plane` or `plane_bits` at
all — confirmed by Round 12 for the estimate function and confirmed again here for the actual build.
There is no code path today, estimate or execution, that starts `PrintingCompose` from an existing plane.

Could one be added? For the one sub-case where it's structurally possible — a single card-invariant
broadcast leaf (`is_broadcast_leaf_shape`, e.g. `pow<=2`), where compose's own build step
(`broadcast_composable_card_bits` → `broadcast_card_bits_to_printings`) independently re-derives a card
bitmap that `eval_planes` already computed — reusing `plane_bits` in place of `broadcast_composable_card_bits`'s
output would work and would be strictly cheaper than compose's own path. But it doesn't get you a plan
that can *beat* `PlanePopcountOrder`: for `Mode::Card` (the only mode `Prep::Plane` ever serves —
`plane_popcount_order_applicable` requires `mode == Mode::Card`), the next thing `PrintingCompose` does
with its card bitmap is find one representative printing per matching card — and with `filter == True`,
that's "any printing, picked by `prefer`," the identical rule `push_card_matches`/`exec_plane_popcount_order_with_bitmap`
already applies to `plane_bits` directly. A plane-bits-reusing `PrintingCompose` doesn't converge to
"cheaper than `PlanePopcountOrder`" — it converges to *being* `PlanePopcountOrder`, reached by a more
expensive path (still deriving `card_bits`, an exact total via `compose_total_for_mode`, and a paging
decision `PlanePopcountOrder` skips entirely). For legality leaves (existential, sometimes divergent
across a card's printings — see `docs/issues/00667-engine-legality-divergent-carveout.md`), the situation
is the same or worse: `eval_planes` already resolves the card-existential answer correctly, including the
divergent-format carveout, and there is nothing left for a printing-space rebuild to add once `filter ==
True` removes any per-printing residual to verify.

**Conclusion: there is no version of "let PrintingCompose start from the plane" that produces a plan
distinct from, and cheaper than, `PlanePopcountOrder`.** The ceiling of this idea is re-implementing
`PlanePopcountOrder` through a more roundabout path. This is the mechanistic explanation for the 0/3,209
empirical result above, not just a correlation with it.

### Argmin/dispatch mechanics (if someone pursues this anyway)

Named for a future implementer, in case a narrower slice (some predicate shape or page geometry this
sweep didn't sample) is later found to have real value:

- **`PlanScope::admits`** (`card_engine/src/lib.rs:9322-9330`) would need a third disjunct:
  `PlanScope::Plane => CandidatePlan::of(plan).is_some() || matches!(plan, PhysicalPlan::PlanePopcountOrder
  | PhysicalPlan::PrintingCompose)`.
- **Not `CandidatePlan::of`** (line 9152) — that type is deliberately exhaustive over exactly the two
  candidate-list executors (`exec_from_candidates`'s repertoire); `PrintingCompose` is a different kind of
  plan (self-composing, non-materializing-via-candidate-list) and doesn't belong in it.
- **`run_query_routed`'s `Prep::Plane` dispatch arm** (`card_engine/src/lib.rs:12618-12632`) would need a
  new arm ahead of the generic `(p, Prep::Plane) => exec_from_candidates(...)` fallthrough:
  `(PhysicalPlan::PrintingCompose, Prep::Plane) => { ... }`, calling `printing_compose_fastpath` on
  `compose_source(filter, unsplit, plane)` and falling back into the existing `exec_from_candidates(...,
  plane_bits as candidate list)` path on `None` (a decline) — mirroring how `Prep::Range`'s own
  `PrintingCompose` arm (line 12654) handles its fastpath declining.
- **`acquire_plan_features`'s `Plane` branch** (lines 11899-11937) would need to additionally price
  `PrintingCompose` whenever it's applicable alongside the plane, by calling the same field-computation
  logic the `PrintingCompose.applicable` branch further down already has (lines 12016-12530-ish) — not
  duplicating it. See "Cost-model piece" below for why this is a real cost concern once (if) the plan is
  admissible, separate from Round 12's finding that it's currently inert.
- **Test to update in the same commit**: `plan_scope_admits_only_plans_its_dispatch_arm_can_run`
  (`card_engine/src/tests.rs:3657`) — its `PlanScope::Plane` assertion must grow the `PrintingCompose`
  disjunct exactly when the dispatch arm above ships, never before or after.
- **Test to add**: something in the shape of a `plane_acquire_admits_printing_compose_and_dispatch_can_run_it`
  test — force the argmin's `PlanScope::Plane` to admit `PrintingCompose`, feed it a plane-acquire query
  where `PrintingCompose` is applicable, and assert dispatch produces a real page (not the
  `CandidatePlan::of_or_gathered` `debug_assert!(false, ...)` fallback path) — the #829-shaped regression
  this change must never reintroduce is "argmin picks a plan dispatch silently downgrades or panics on."
  `force_plan_differential_agreement` (`card_engine/src/tests.rs:3025`) already asserts every plan returns
  identical rows over a random corpus and would need `PrintingCompose` exercised under a plane-holding
  sort spec if it doesn't already.

## Cost-model piece

Round 12 found `acquire_plan_features`'s `Plane` branch leaves `PrintingCompose`'s five build-cost fields
(`broadcast_printings`/`scatter_printings`/`project_printings`/`popcount_words`/`compose_paging`) at
`mk_plan_feats`'s defaults, and concluded this is currently **inert** — `PlanScope::Plane` excludes
`PrintingCompose` from the argmin regardless of how it's costed, so fixing the costing changes zero
routing decisions today, only `explain`'s diagnostic ranking. That conclusion still holds, and this round
adds a stronger one: even if `PlanScope::Plane` were widened (making the costing live), the costing
couldn't change the outcome either, because `PrintingCompose` measures slower than `PlanePopcountOrder`
in every sampled row — an accurate cost model would (correctly) rank it last just as reliably as the
current, wrong-for-different-reasons zero-defaulted one does. If someone implements the widening anyway
(narrower slice found, or for `explain`/diagnostic accuracy on its own merits), the fields need populating
by reusing the `PrintingCompose.applicable` branch's own computation against `compose_source(filter,
unsplit, plane)` — see Round 12's trace for the specific reuse points (`compose_printing_estimate`, the
`is_broadcast_leaf_shape`/legality/range leaf arms) rather than re-deriving them.

## Risks and staging

Not applicable in the sense of "here's how to roll this out safely," because the recommendation is not to
roll it out. Recorded for whoever revisits this:

- **Blast radius if built anyway**: `PlanScope::admits` (1 line), one new `tests.rs` assertion, one new
  dispatch arm in `run_query_routed` (~10-20 lines including its decline fallback), and — the expensive
  part — folding `PrintingCompose`'s build-cost fields into `acquire_plan_features`'s `Plane` branch by
  sharing logic with its ~300-line `PrintingCompose.applicable` branch. Call it 50-80 net new lines plus
  whatever refactor is needed to share the two branches' field computation without duplicating it.
- **What could go wrong**: a second #829-shaped panic if the `PlanScope` widening and the dispatch arm
  ship in different commits (or if a future refactor moves one without the other — the test catches this
  only if it's kept in lockstep, which is a discipline, not a guarantee); an unconditional new cost
  computation on every `Plane`-acquire query (a large share of `unique=card` traffic — every legality,
  color, rarity, or type predicate that folds entirely into the plane) in exchange for zero routing
  benefit, which is a straightforwardly bad trade given this round's numbers.
- **Precedent for gating experimental engine behavior**: `COMPOSE_SIGMA_ENABLED`
  (`card_engine/src/lib.rs:6339`, `guard_env("CARD_ENGINE_COMPOSE_SIGMA_ENABLED", 0u8) != 0`, default off)
  is exactly this codebase's existing pattern for a change that's implemented but not trusted by default —
  and `PRINTING_COMPOSE` itself (line 6380, default *on*) is the same mechanism used as a kill switch for
  an already-shipped feature. If this were built despite the finding above, it should ship behind a
  same-shaped flag (default off), not unconditionally — but the honest recommendation is not to build it
  rather than to gate it.

## Explicitly out of scope / open questions

- **Why Round 12's 71% "applicable" figure and this round's 18.3% "applicable and didn't decline" figure
  disagree.** Not chased down — see "Spike" above. Doesn't affect the conclusion, since a fastpath that
  declines contributes zero wins either way.
- **Resolved: whether `PlanePopcountOrder` is ever unavailable under `Plane` acquire, leaving
  `PrintingCompose` to compete against only `GatheredScan`/`StreamedSelect` — the one scenario where
  Round 12's real 10.7%/6.7% win-rate margins (149µs/142µs) could represent a genuine, capturable
  routing gap this round's `PlanePopcountOrder`-relative measurement wouldn't see.** Checked directly:
  sampled 14,291 real `plane`-acquire queries (realistic traffic, fresh run) — `PlanePopcountOrder` was
  applicable in **100%**, 0/14,291 missing. Combined with the "Re-verified" pairwise data on `#852`
  (`GatheredScan vs PlanePopcountOrder` and `PlanePopcountOrder vs StreamedSelect`, both 100% ordered
  right, 0.00µs mean regret), there is no real subset where this degrades to a two-plan comparison —
  `PlanePopcountOrder` is essentially always present and already correctly chosen. Round 12's real
  margins were genuine but measured against plans that were never the actual incumbent; there is no
  ~100µs+ improvement on the table via routing here.
- **Whether some predicate shape entirely outside this sweep's sampler (both `QuerySampler` modes, plus a
  manual limit/offset sweep) could flip the result.** Nothing in 3,209 rows across four regimes did. The
  limit dimension specifically is closed, not just inductively suggestive: `limit=1,000,000` is the API's
  own uncapped-query sentinel and already exceeds the corpus's true printing/card cardinality, so there is
  no larger limit a real query could ever issue — the margin's narrowing-but-never-crossing trend was
  chased to its actual ceiling, not just a large sample point. What remains open is predicate SHAPE
  (leaf types, `And`/`Or` combinations) outside what the two `QuerySampler` modes generate, which is a
  real, separate axis this sweep didn't fully control for.
- **Non-default `prefer` was tested (regime 2) and didn't change the qualitative result**, but only one
  sample was run varying it; if this doc is ever revisited, that's already covered rather than a gap.
- **Whether `PrintingCompose`'s own acquire path (`Prep::Range(CountSource::PrintingCompose)`) — where it
  competes on equal footing and does win a meaningful share, per #852 — has any remaining routing
  problems of its own.** Out of scope here; that's #852's territory, and it's already closed as resolved
  per its own "Re-verified" section.
