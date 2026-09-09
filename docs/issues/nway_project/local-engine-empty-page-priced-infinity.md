# The router prices `EmptyPage` at INFINITY and never picks the plan that would win

`cost.rs` prices `ComposePaging::Decline` at `f64::INFINITY`, so a plan predicted to refuse is kept out of the argmin entirely — "routing to a plan that returns `None` pays the detour and then runs something else anyway". That is right when the plan really refuses. **It is wrong when the executor's actual exit is `EmptyPage`**, which returns the correct answer faster than anything else.

The two are decided by different numbers: `compose_paging` predicts from the acquire's **estimate** of the total, while the fastpath branches on the **realized** one.

## What it costs

Measured with `scripts/bench_pick_quality.py`, uniform sampler, 8,000 queries, 5,366 with two or more plans timed:

| slice | n | hit% | time-weighted hit% | share of all routing loss |
|---|---|---|---|---|
| normal | 4,447 | 95.8% | 89.7% | 79.0% |
| **empty result** | 632 (11.8%) | 91.5% | **68.8%** | **9.3%** |
| **offset past the end** | 296 (5.5%) | **66.9%** | **34.0%** | 11.8% |

A worked case, verified by forcing all three plans (`set:tmp frame:1993`, printing/cubecobra/off=100): compose 7 trials, **0 declines**, `paging_taken = EmptyPage`, **1,417 ns**; GatheredScan 42.5 us; StreamedSelect 38.5 us. `result_total = 0` for all three. The router picked GatheredScan because compose's predicted cost was `inf`. Every one of the ten worst mis-picks BY RATIO has this exact signature.

## The two sub-cases are not equally real, and only one is worth scheduling

- **Empty result** (632 rows, 11.8%) — the query genuinely matches nothing. Representative of real traffic; a user typing a filter that matches no card is ordinary.
- **Offset past the end** (296 rows, 5.5%) — the query matches rows but the requested page starts beyond them. `watermark:mps` returns **5** artworks and the harness asks for offset 100. **This cell is inflated by the sampler**: `costbench.OFFSETS` draws `offset=100` on a quarter of queries regardless of result size, whereas real traffic reaches offset 100 only by paging there, having seen results. Its 66.9% hit rate is the worst number in the table and the least trustworthy.

So the schedulable claim is the first: **~12% of queries, 9.3% of routing loss, at a 68.8% time-weighted hit rate.** Re-running the sampler with a realistic offset distribution would say how much of the second survives outside the harness, and should precede any work aimed at it.

## Same gate, the other direction

Round 80's completeness audit found the mirror failure: **74 queries where compose WAS picked and then refused after paying the entire build** — composing `pbits`, projecting `card_bits`, popcounting — before `return None`. `declined_ns` p50 **17.2 us**, p90 44.3 us, **summing to 11.66 ms = 3.59% of all picked-plan measured time** **[CORRECTED 2026-09-09: this 3.59% is a units error — a nine-execution `declined_ns` sum over a single-execution denominator. Re-run at the same n=12,000 reproduces 1,907 rows / 75 picked / 11.81 ms exactly, and the consistent ratio is 0.54% uniform, 0.16% realistic. See measurements/2026-09-09-compose-paging-confusion.txt.]**, thrown away before the fallback starts.

Both directions are the same root cause. One wastes a build; the other excludes the fastest plan. Any fix should address both or explain why not.

## What a fix looks like — and what it is not

**It is not a cost-model change.** `plan_cost` cannot see that the page is empty: `matches` is an estimate and the branch is on the realized total. No rate or feature reaches this.

Two candidate shapes, neither costed:

1. **Stop returning INFINITY where the real exit would be `EmptyPage`.** The gate would need to distinguish "will refuse" from "will return an empty page fast", which today's `ComposePaging` collapses into one `Decline`.
2. **A zero-result / past-the-end test before costing.** `offset >= total`, decided before dispatch so that nothing runs — which fixes both directions at once, since no plan means neither a wasted build nor an excluded fastest plan.

## Option 2, worked out (2026-09-05)

**No exactness flag is needed, and the earlier framing above asking for one was wrong.** `guaranteed` is already the tightest PROVEN upper bound on the true count (`lib.rs:9432`), and at zero a proven bound and an exact count coincide: `0 <= true <= 0`. So the bound channel alone satisfies the API's "total_cards is always the unpaginated count" contract ([api_resource.py:605](../../../api/api_resource.py#L605)) with no extra work. Since zero is a statement about truth rather than about the bound, and the three spaces are zero-or-nonzero together, `guaranteed == 0` in ANY space proves the page empty in all three — the documented lack of cross-space consistency cannot bite, because it concerns the ordering of nonzero bounds.

That splits the work in two, by what can be ANSWERED rather than what can be proved:

- **Tier 1, `total == 0`** — provable and answerable. Return `(0, [])` with no plan run.
- **Tier 2, `offset >= total > 0`** — provable but NOT answerable: a bound proves the page empty without giving the total, and the contract demands an exact one. Still worth doing (skip the page, keep only the count), but it is a different change.

Measured with `scripts/bench_empty_page_provable.py`, uniform sampler, 8,000 queries, 1,857 (23.2%) genuinely empty:

| route | empty queries | acquire says 0 | coverage |
|---|---|---|---|
| candidates | 1,013 | 899 | 88.7% |
| plane | 9 | 9 | 100% |
| printing_compose | 777 | 78 | 10.0% |
| printing_range_scan | 58 | 1 | 1.7% |
| **all** | **1,857** | **987** | **53.2%** |

Coverage splits along the "real count of a real set" line, which is exactly the admission rule for `guaranteed`: `candidates` materializes a list and `plane` popcounts a bitmap, so on those routes the number IS the proven channel. **Tier 1 is worth 908 of 8,000 queries — 11.4% answered with zero execution**, agreeing with the 11.8% empty-result cell measured independently above.

**CORRECTED 2026-09-05 — `proven()` is not available where the zeros are, and the gate below cannot be written as stated.** The `guaranteed`/`estimate` channels live on `ComposeEstimate`, which only the `printing_compose` acquire builds — the route with 10% coverage. The `plane` and `candidates` branches pass bare `u32`s into `mk_plan_feats`, so 908 of the 908 usable zeros sit on routes with no channel to read. The proof IS in hand at each branch, just not through `SpaceMeasure`:

- **plane** — `count` is the popcount of the one plane eval and is documented exact, so `count == 0` is a proof.
- **candidates** — `matches == 0` iff `in_space == 0` (both arms of the `all_match_known` split yield 0 only then), and zero candidates means zero results whatever the residual would have done.
- **printing_compose** — `est.result.<space>.proven() == Some(0)`, i.e. the original design, for the 10% case.
- **range branches** — `k == 0` proves an empty range; a non-empty range whose residual rejects everything is invisible, which is why `printing_range_scan` measures 1.7% coverage with misses claiming a p50 of 14,876.

So the implementation is the Round-62/stage-0 pattern rather than a channel read: an explicit `provably_empty` signal recorded in each acquire branch from what that branch itself knows, consumed as one bool before dispatch. The soundness argument below is unchanged and still governs — this only changes where the answer is read from.

**The gate must read `guaranteed`, never `matches`.** The same run found 4 queries where `matches == 0` and the executor returned rows, all on `printing_compose`. `matches` is `best() = min(estimate, guaranteed)`, and the zero came from the estimate channel every time. Details, and the structural change that removes the hazard rather than documenting it, in [local-engine-structured-space-measure-consumers.md](local-engine-structured-space-measure-consumers.md) — Tier 1 should land on top of that doc's stage 1, not before it.

`printing_compose` is the coverage gap (777 empties at 10%) and is also the route that pays the wasted build above. Its misses claim a p50 of only 10, so the estimate is not wild — it simply cannot prove zero. `exact_result_total`'s `leaves_are_disjoint` arm already returns `Some(0)` cheaply and is not consulted on this path; that is the obvious follow-on.

One caveat on the headline: 23.2% is a `uniform`-sampler figure, the RANK population. The per-route coverage and the lie count are structural and carry over; the percentage should not be quoted as a traffic number.

## Evidence trail

Round 85 and Round 86 in [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md); raw output in [measurements/2026-09-05-pick-quality-uniform.txt](measurements/2026-09-05-pick-quality-uniform.txt) and the two `--worst-by` dumps beside it. Round 80's decline-side measurement is in that same ledger.
