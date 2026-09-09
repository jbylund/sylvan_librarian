# Why `fit_cost_model` refuses to fit PrintingCompose, and what each flag needs

`counter_check` grades each realized counter against the feature meant to predict it, and skips a plan's whole rate fit if any cell falls outside `COUNTER_TOL = 0.15`. PrintingCompose has never been fitted, because three cells flag. This doc records what each one is, measured and traced to the executor, and what fixing it would actually take — because the verdicts are clean and the fixes are not equally reachable.

Measured 2026-09-09, `--mode uniform`, n=20,000 queries / 43,199 plan-rows, on `costcell/scan-per-row-plane-bias`.

## The current cells

```
PrintingCompose  cards_visited/eval_domain                          0.83     182  <-- FEATURE
PrintingCompose  printings_examined/printings_walked[OrderbyWalk]   2.32     845  <-- FEATURE
PrintingCompose  printings_examined/printings_walked[Perm]          0.78   4,445  <-- FEATURE
PrintingCompose  matches_pushed/matches                             0.80     182  <-- estimate, not feature
```

The per-branch split of the walk cell, and the demotion of the fourth, landed in `6624d6aa`. Before that commit the walk cell read a single pooled 1.20, then a single pooled 0.89 — see that commit message for the harness bug and why the "passing" 0.89 was hiding a real defect.

## `cards_visited/eval_domain` — the arm borrows a feature built for the other two plans

`gather_composed_page` iterates `bitmap_card_ids(card_bits)`, and `card_bits` is the **composed result** bitmap projected into card space. So the loop visits *matching* cards. The arm multiplies `eval_domain`, which `acquire_plan_features` sets to `domain_cards` in all three modes — and the code says what that is: "`eval_domain` and `scan_units` describe what the MATERIALIZING alternatives walk" ([lib.rs:18290](../../../card_engine/src/lib.rs#L18290)). Two different quantities, by design.

It is not an estimate error dressed up as a feature error. Paired on the same 180 queries, GatheredScan's realized `cards_visited / eval_domain` reads **1.00**, so `eval_domain` is an accurate *candidate* count; compose's 0.83 is the composition's selectivity over that candidate set.

Split by distinct-on: **artwork 0.97** (n=86), **card 0.65** (n=87), **printing 1.75** (n=9).

The fix has direct precedent — `compose_scan_printings` exists because sharing one printing feature between compose and the scan plans forced a compromise ~2x wrong for whichever arm lost. The same move here is a `compose_result_cards` field set to `est_cards` (the card-space result the compose acquire already computes), consumed by `COMPOSE_GATHER_CARD_PASS_NS` instead of `eval_domain`, graded against `cards_visited` on Gather rows.

**But that only reaches two of the three modes.** In card mode `eval_domain` already tracks `est_cards` (their ratio reads p50 1.00 on those rows), so the 0.65 there is `est_cards` over-predicting the realized composed card count by ~1.5x — an estimator error, not a feature choice, and card mode is half the population. So this flag is part feature-choice and part cardinality estimate, and the feature fix alone will not clear the cell.

`GATHER_CARD_PASS` carries **21.4% of compose's predicted cost** (3.27% of all predicted time), so this is the flag that matters most by magnitude despite the thin row count.

## `printings_examined/printings_walked[OrderbyWalk]` — a units mismatch, not a bias

`walk_value_orderby_page` ([lib.rs:13608](../../../card_engine/src/lib.rs#L13608)) loops `for step in 0..n_keys` over the value index's distinct keys and walks each key's run, and its counter is documented as "one `pbits` test per printing considered — index entries **and representative-resolution probes** alike."

`printings_walked` models something else: `min(offset + limit, matches) / (matches / n_printings) * WALK_LENGTH_BIAS`, i.e. printings stepped at the *global* match density. Index entries plus per-group probe work is a different unit, so no constant reconciles the two, and the 2.32 is not a bias to divide out.

This confirms the suspicion already recorded in [`walk-variable-check`](local-engine-nway-followup-queue.md) — that `printings_examined` counts a cheap quantity while cost is driven by an expensive one, presenting as a MISSING TERM rather than a bias. That item's regression (`ns_loop` against each candidate variable, on the same rows) has to run before a feature can be proposed.

By distinct-on: artwork 2.80 (n=449), card 1.91 (n=396), no printing rows — printing-mode usd/rarity orderbys take Gather instead.

## `printings_examined/printings_walked[Perm]` — one constant, two axes

`walk_grouped_page` is a forward grouped walk taking the card/default-prefer early break. It shares `printings_walked`, and `WALK_LENGTH_BIAS = 1.45` was calibrated as a pooled bias across the three *acquires* that reach a walk (0.66 / 0.67 / 0.74, agreeing — which is what made it look like one bias).

The branch is not the only axis. Perm's own error varies across acquires by 2.2x:

| Perm, by acquire | ratio | implied bias |
|---|---|---|
| `card_range_popcount` (n=262) | 0.39 | 0.56 |
| `plane` (n=734) | 0.56 | 0.82 |
| `printing_range_scan` (n=263) | 0.76 | 1.10 |
| `printing_compose` (n=3,186) | 0.85 | 1.23 |
| **OrderbyWalk** (`printing_compose` only, n=845) | 2.32 | 3.37 |

One constant of 1.45 spans an implied 0.56 to 3.37. Splitting per branch fixes the branch axis and leaves the acquire axis, and `plane` at 0.56 is the same explanatory variable this branch already carries a StreamedSelect bias for — so the two should be designed together, not separately.

Two further complications: `printings_walked` is also multiplied by `PrintingRangeScan`'s arm ([cost.rs](../../../card_engine/src/cost.rs)), and that plan is **never graded at all** — `counter_check`'s `instrumented` filter requires a nonzero `cards_visited`, which `PrintingRangeScan`, `PlanePopcountOrder` and `CardRangePopcount` do not have. So changing the shared constant moves three arms no check is watching.

## What the veto costs, measured

Out-of-sample on held-out feature-vector shapes, 4 splits (2 seeds x 2 directions), scored on total executor time:

| variant | count-weighted within-25% | time-weighted within-25% |
|---|---|---|
| VETO — shipped coefficients, never fitted | 48.8% | **51.9%** |
| FIT ALL — ignore the veto | 48.4% | **62.5%** |
| OFFSET — hold the flagged terms at shipped, fit the rest | 44.0% | 59.0% |

So the veto forgoes **10.6 points of time-weighted accuracy**, and holding the flagged terms fixed is *worse* than fitting them, because pinning them forces the neighbours to compensate (`GATHER_PUSH_PER_MATCH` 3.39 -> 6.27, `FIXED` 164 -> 206).

The veto's stated premise is that "the fit will happily bury the error in whichever coefficient correlates with it". At these magnitudes it does not: fitting through the flagged features moves those rates by roughly their own measured error and leaves the rest alone.

- `GATHER_CARD_PASS` 13.22 -> **13.45** (1.02x) — the fit barely touches it.
- `WALK_STEP` 0.58 -> **0.46** (0.79x) — almost exactly Perm's measured 0.78, i.e. absorbed locally.

## Recommendation

Drop compose's veto to a warning that names the affected terms, and fit the arm, recording that `WALK_STEP` and `GATHER_CARD_PASS` carry known feature errors and their fitted rates are therefore provisional. This is the sequencing the queue already argues for — executor, then features, then refit — and it unblocks the joint three-arm refit now rather than behind two feature-design projects.

Do NOT use the offset mechanism: measured worse than fitting, above.

The three fixes then schedule separately, none of them one-liners:

- **`compose_result_cards`** — the reachable half of the first flag, precedented by `compose_scan_printings`. Will not clear the cell on its own, because card mode's half is a cardinality estimate.
- **[`walk-variable-check`](local-engine-nway-followup-queue.md)** — run the `ns_loop` regression before proposing any OrderbyWalk feature. The 2.32 is a units mismatch and no per-sort constant addresses it.
- **[`printings-walked-per-sort`](local-engine-nway-followup-queue.md)** — the branch axis, designed together with the acquire/plane axis rather than as a second pooled constant, and only after the three ungraded arms that share `printings_walked` are brought under a counter check.

One caveat on magnitude before any of this is scheduled: `WALK_STEP` is **0.71%** of all predicted time and `GATHER_CARD_PASS` **3.27%**. These are prerequisites for a clean refit, not priorities in their own right. For scale, the two largest single misprices in the whole model are `GatheredScan / CARD_PASS+FLOOR` at 27.7% of all predicted time and `StreamedSelect / SCAN_PER_ROW` at 16.6% (measured the same run); compose's whole arm is 15.3%, of which the build terms `BROADCAST_PER_PRINTING` (5.0% of all predicted time) and `PROJECT_PER_PRINTING` (3.7%) have unflagged features and are exactly what the veto is currently preventing us from fitting.
