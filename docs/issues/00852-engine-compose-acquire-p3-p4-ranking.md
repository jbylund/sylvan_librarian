# Rank `GatheredScan` vs `StreamedSelect` on the Compose Acquire

Status: **this pair is resolved** — see "Re-verified" below. Filed as
[#852](https://github.com/jbylund/sylvan_librarian/issues/852). Successor to
[the loop-phase measurement record](done/local-engine-loop-phase-measurement.md), whose calibration work
shipped in #833 / #834 / #836.

## Re-verified after the printing-varying-depth loop (`local-engine-gathered-scan-card-printing-varying-depth.md`)

That doc's Rounds 1/3/4/6/7 targeted exactly this doc's #1 remaining item — `eval_domain`/`domain_cards`
accuracy for an `And` of printing-varying range leaves in the narrowed regime — without realizing the
connection at the time. Re-measured with `bench_pairwise_ordering.py` against the accumulated result
(`costcell/trunk`, 10 rounds landed):

| | historical (this doc) | re-verified |
| --- | --: | --: |
| pair ordered right (realistic) | 87% | **97%** |
| pair mean regret (realistic) | 4.29 µs | **0.82 µs** |
| pair gap meas/pred (realistic) | 0.98 | **0.99** |
| pair ordered right (uniform) | — | 95% |
| pair mean regret (uniform) | — | 1.74 µs |

**This pair is closed as a priority** — 0.82 µs mean regret is now smaller than several pairs that were
never a concern. Item 1 on this doc's "Remaining, in order" list (the narrowed-regime `eval_domain`
error) is substantially resolved; items 2 and 3 are no longer worth pursuing on their own now that this
pair isn't the routing error the pursuit was justified by.

**The re-verification surfaced a bigger, different problem**, not previously examined by this doc: both
`GatheredScan vs PrintingCompose` and `PrintingCompose vs StreamedSelect` are now the worst pairs in the
engine, concentrated in the `plane`-acquire branch specifically:

| pair / acquire | ordered right | mean regret | gap meas/pred |
| --- | --: | --: | --: |
| `GatheredScan vs PrintingCompose` [plane], realistic | 87% | 19.09 µs | 0.85 |
| `GatheredScan vs PrintingCompose` [plane], uniform | 83% | 27.21 µs | 0.94 |
| `PrintingCompose vs StreamedSelect` [plane], realistic | 92% | 11.42 µs | 0.75 |
| `PrintingCompose vs StreamedSelect` [plane], uniform | 86% | 15.72 µs | 0.83 |

Root cause (traced, not yet fixed): `acquire_plan_features`'s `Plane` branch returns
`mk_plan_feats(ctx, params, count, count, scan_units, 0)` directly with no further field overrides —
unlike every other acquire branch, which sets `PrintingCompose`-specific build-cost fields
(`broadcast_printings`, `scatter_printings`, `project_printings`, `popcount_words`, `compose_paging`)
after the shared call. Those fields' defaults in `mk_plan_feats` (0 / `ComposePaging::Gather`) are
correct for the plans that don't read them, but `PrintingCompose` — a genuine alternative plan whenever
the plane-covered predicate is also printing-composable — gets costed off inputs that describe nothing
real about what it would actually do if it won. Same class of bug as the historical
`compose_paging`-left-at-`Gather`-default issue this doc's "Both fixed" section already closed for a
different acquire branch, not yet applied to this one. Tracked onward in
[local-engine-plane-acquire-compose-costing.md](local-engine-plane-acquire-compose-costing.md).

## Where it stands (historical, before re-verification above)

The pair went **69% → 87% ordered right** and mean regret **35.96 µs → 4.29 µs** across that stack. What
moved it last was `eval_domain`, and the mechanism is worth stating because it was misdiagnosed twice:

`eval_domain` was `est_cards` — a count of **matching** cards — graded against `cards_visited`, which counts
**candidates**, a superset whenever the narrowing is inexact. The distribution is bimodal: **34% of compose
rows visit every card in the corpus**, and on those the right value is not a better estimate but `n_cards`.

| on the 986 full-scan rows | p10 | p50 | p90 | mean \|log\| |
| --- | --: | --: | --: | --: |
| `est_cards` (was) | 0.43 | 0.65 | 0.83 | 0.454 |
| `n_cards` (now) | 1.00 | 1.00 | 1.00 | **0.000** |

Predicted with the predicate and constant the sibling `PrintingRangeScan` branch already uses for the same
decision (`range_too_broad_to_narrow`, `MAX_NARROW_FRACTION` 0.25), so no new constant. It catches **98%** of
full-scan rows at 87% accuracy. Its 26% false positives over-cost both materializing plans by the same
factor, which an argmin absorbs; the false negatives were the ones losing the pair, so **recall is the side
to favour** here.

| | before | after |
| --- | --: | --: |
| `eval_domain [printing_compose]` p50 / p10 | 0.91 / 0.45 | **1.00 / 0.68** |
| pair ordered right | 75% | **87%** |
| pair mean regret | 23.00 µs | **4.29 µs** |
| pair gap meas/pred | 0.96 | **0.98** |

That is within reach of the well-behaved `candidates` acquire (92%, 2.42 µs). **Total regret is flat** (1.52
against 1.54 µs) and `StreamedSelect -> GatheredScan` is still 967 queries, because `bench_pairwise_ordering`
scores every pair including queries where compose wins anyway, so the P3/P4 ordering never reaches the
routing outcome there. Pairwise accuracy is a leading indicator, not the result.

## The ceiling, and what it says about sequencing

An **oracle** run settles the features-vs-rates question directly: recompute both arms substituting each
plan's *realized counters* for its estimated features, keeping every shipped rate untouched, then re-run the
argmin. Over 2,778 non-tie pairs with ≥100 realized cards on both plans:

| features | ordered right | lost time (sum) |
| --- | --: | --: |
| shipped estimates | 58% | 116.2 ms |
| **oracle (realized counters)** | **83%** | **12.8 ms** |

| mode | shipped | oracle |
| --- | --: | --: |
| card | 58% | **96%** |
| printing | 62% | 80% |
| artwork | 56% | 81% |

Perfect features against *today's* rates reach 83% and cut lost time **9×**. **The rates are adequate to
83%**; the remaining 17% is what is genuinely attributable to them. Features first — do not open the rate
question until the estimates stop moving.

(58% here is not the 87% above: this run requires ≥100 realized cards on both plans, which selects the larger
queries where estimator error dominates. The valid comparison is the internal one, 58 → 83 on identical rows
with identical rates.)

## Two results that shape the work, both counter-intuitive

**Per-plan features are worth exactly zero.** Leave-one-out from the full oracle, shipped rates throughout,
2,768 pairs:

| variant | ordered right | lost time |
| --- | --: | --: |
| shipped | 59% | 116.7 ms |
| full oracle (per-plan) | **83%** | **12.4 ms** |
| oracle, `eval_domain` back to its estimate | 68% | **91.2 ms** |
| oracle, `scan` back | 79% | 22.4 ms |
| oracle, `matches` back | 83% | 13.2 ms |
| oracle, `scan` forced **SHARED** (both read P4's) | 83% | 12.4 ms |
| oracle, `eval_domain` forced **SHARED** | 83% | 12.4 ms |

`eval_domain` is ~75% of the recoverable loss (78 of 104 ms), `scan` ~10%, `matches` nothing. And forcing
either feature to be *shared* while keeping it exact costs nothing at all — so the answer is **not** to split
features per plan, it is to make the one shared number accurate. The mechanism agrees: on the broad-residual
class both plans examine the same 97,206 printings, and on the card-invariant class the verify-tier gate
already zeroes P3's scan term.

**A corollary worth checking:** `stream_scan_units` may now be redundant with that gate, since the divergence
it was built for is the population the gate already handles.

**A feature fix only pays when the two plans weight the feature differently.** Three of four corrections moved
the pair 0%, because `eval_domain` and `scan_units` feed *both* arms — a correction to either moves both
predictions the same direction and the difference barely changes. The `scan_units` clamp moved it because it
lands asymmetrically: 76% of P3's arm on this class against 28% of P4's. Same principle as `cost.rs`'s "a
term wrong for every plan cancels", applied to features rather than rates.

## Remaining, in order

1. **The residual `eval_domain` error in the *narrowed* regime** (p10 0.68). Two things bound it: the exact
   path is itself only p50 0.92 / p10 0.50 against the counter, so 1.00 is not the ceiling; and combining two
   exact range counts is not free — the boundary table answers each range independently, and an `And`'s
   distinct-card count is not derivable from the two without composing.
2. **`scan_units` on selective compose queries** — mean |log| **1.22**, p10 0.08, p90 3.62, a ~45× spread that
   no bias variant improves. Second-order for this pair (~10% of recoverable loss), so it ranks below (1).
3. **The pair-level loop harness, once the features stop moving.** `bench_streamed_loop` and `bench_gather_loop`
   share `bench_loop_design` so their cells match, but neither computes the cross-plan quantity — and
   `explain_analyze` compares predicted against measured *per plan* on sampled traffic and cannot isolate a
   per-unit rate. So the one number routing depends on, P3's per-printing rate against P4's on identical
   cells, is produced by nothing.

   **Extend, do not fork.** `bench_loop_design` exists precisely because that comparison is only valid when
   the cells match, and they had already drifted once (`CARD_COUNTS` sharing two of five sizes). Move cell
   construction into `bench_loop_design`, then add one reporting test that runs both arms over those cells and
   prints each rate, the measured ratio, and the shipped ratio beside it.

   Two prerequisites, both already flagged in `bench_streamed_loop`'s own header: the rates there are `ns_loop`
   only, and *"P3's arm may be absorbing setup or finish cost that its loop never pays — that has to be ruled
   out before the gap is called an error"* (`Cell` already carries `ns_setup` and `ns_finish`, unused). And the
   broad-residual population is already the `residual: true` group, so no new cell class is needed.

## Two traps for whoever opens the rate question

**A pooled traffic fit endorses both current rates** (StreamedSelect `SCAN_PER_ROW` 6.04, ratio 1.01;
GatheredScan 2.53, 1.23), so `fit_cost_model` cannot find this and a pooled refit will confirm the status quo.

**A built design reads warm-cache**, so it yields the *shape* — which of the two rates is misattributed — and
not the level. For when it opens: the existing harnesses report P3 at 3.30 ns/printing against P4's 2.27, a
ratio of **1.45**, where the shipped constants are 5.97 and 2.06, a ratio of **2.90**. Both were measured warm
but *together*, and a ratio between two equally-warm arms survives the cache caveat that voids their levels.
Wrong-rate concentrates by mode at artwork 29%, printing 24%, card 18%.

## Carried forward, not part of this issue

Recorded so they are not lost with the parent doc. Each is a separate idea and none blocks the above.

- **Extend the popcount-skip walk past `FilterExpr::True`.** `run_query_streamed_popcount` scatters through
  `inv_perm` and walks words at 64 cards a load, but only for `unique=card` + `True`. Printing mode — where
  P3 is actually routed — is the case it does not cover. Needs per-card counts for the skip, since a popcount
  counts cards and not matches. Tracked at [#730](00730-engine-popcount-skip-walk.md).
- **Compose `format:A AND format:B`,** now that both are usually card-invariant: the shared-witness objection
  dissolves when neither format diverges, since `∃p: A(p) ∧ B` is `(∃p: A(p)) ∧ B`. `compile_plane` still
  declines it with `u64::MAX`, and `legality_and_of_two_formats_declines_but_or_compiles` names the assertion
  to revisit. #835's divergence mask is what makes this newly answerable.
- **A curvature term for the loop rates** — cause of both the `FIXED` disagreement and corpus drift, though
  the five-size sweep demoted it: the drift saturates and production sits at the bottom of the curve.
- **Gate P4's artwork arm on its own.** Every surviving mispick is in artwork mode.
- **Make the fit loss the actual multi-way routing outcome** rather than a pairwise proxy — see the leading-
  indicator caveat above, which is the same complaint.

## Reproducing

```bash
.venv/bin/python scripts/bench_cost_model_agreement.py --seconds 300   # the bar
.venv/bin/python scripts/bench_plan_misselection.py --source distribution --out A.jsonl
.venv/bin/python scripts/bench_plan_misselection.py --compare A.jsonl B.jsonl   # the only real verdict
```

Which tool answers which question, and the measurement traps this line of work paid for one at a time:
[reference-cost-model-measurement.md](reference-cost-model-measurement.md).

## Related

- [done/local-engine-loop-phase-measurement.md](done/local-engine-loop-phase-measurement.md) — the full
  measurement record, including the retractions.
- [done/local-engine-cost-model-agreement.md](done/local-engine-cost-model-agreement.md) — the agreement
  matrix and the four feature fixes.
- [#856](00856-engine-compose-membership-bittest.md) — the larger prize
  on this same population: P3 should not re-derive membership compose already computed exactly.
- [local-engine-compose-build-rates.md](local-engine-compose-build-rates.md) — the rate measurements, and the
  `Perm` arm's missing `cards_visited` term.
- [local-engine-plan-misselection.md](done/local-engine-plan-misselection.md) — the other open calibration gap,
  `PlanePopcountOrder` under-costed a median 1.61×.
