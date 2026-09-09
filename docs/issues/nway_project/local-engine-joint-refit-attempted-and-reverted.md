# The joint refit made the cost model more accurate and routing worse. Reverted.

All 26 fitted rate constants were refit together from `scripts/fit_cost_model.py --n-queries 20000` (`--mode uniform`, prefer pinned, 43,201 plan-rows, target = the executor alone), applied to `cost.rs`, measured, and reverted. This records why, because the accuracy numbers are genuinely good and the conclusion is not the one they suggest.

Jointly rather than one arm at a time because refitting one arm tilts every pairwise comparison it takes part in, which is what made compose's fittability a prerequisite — see [local-engine-compose-counter-flags.md](local-engine-compose-counter-flags.md).

## The accuracy gain was real

Out-of-sample on held-out feature-vector shapes, time-weighted within-25% (the share of executor TIME in rows predicted within [0.8, 1.25]):

| arm | shipped | refit |
|---|---|---|
| GatheredScan | 34% | 51-55% |
| StreamedSelect | 36% | 46-54% |
| PrintingCompose | 51.9% | 62.5% |

## The wall-clock effect was unmeasurable

Both routings priced against ONE set of `explain_analyze` timings per run, so the pick difference carries no timing noise within a run. 8,000 queries per population.

| population | runs | symmetric 0.5% trim |
|---|---|---|
| uniform (RANK) | -0.30%, -0.19%, -0.261% | **+0.002%** |
| realistic (VALUE) | -0.07%, **+0.086%** | **+0.012%** |

The realistic sign flips between runs, and the baseline total itself moved 135.41 -> 143.17 ms (5.7%) run to run. A symmetric 0.5% trim — the unbiased version of "ignore the outlier" — takes BOTH populations to +0.00%, i.e. the entire measured effect in either direction lives in ~78 of 7,900 queries. Leave-one-out on the single worst regression moves realistic from +0.086% to -0.025%: one query out of 7,982 flips the sign of the whole result.

Stratifying by cardinality-estimate accuracy (a property of the query, fixed before either routing is scored) does not rescue it: on uniform the win concentrates where estimates are BAD (-2.455% in a stratum holding 6% of time), on realistic the loss concentrates where they are GOOD (+0.122%, 21 wins against 70 losses). Opposite patterns on the two populations is what noise looks like, not a mechanism.

## The decisive test: the queries we already knew were mispriced

The eight example queries from [local-engine-stream-scan-units-tail-dispersion.md](local-engine-stream-scan-units-tail-dispersion.md), where `stream_scan_units` carries 34-63% of StreamedSelect's whole prediction. Timed with 15 trials under both builds:

| query | base pick | refit pick | base us | refit us |
|---|---|---|---|---|
| `cmc>=6 usd<7.31 eur>=0.51 eur<=3.82` | StreamedSelect | **PrintingCompose** | **112.4** | **230.8** |
| `tou>=1 tou<=2 tix>=0.22` | StreamedSelect | **PrintingCompose** | **112.6** | **205.9** |
| the other six | unchanged | unchanged | — | — |

**+9.17% total (2,309 -> 2,521 us)**, and both flips moved off a plan that was at or near oracle. These eight sit at **1.553x oracle** against 1.03x population-wide, so they are where routing is actually broken — and the refit takes them to 1.696x.

The mechanism is `STREAM_SCAN_PER_ROW_NS` 5.97 -> 7.94 (+33%). StreamedSelect's predicted/measured on the eight was already ABOVE 1.0 on six of them, so raising the rate moved six further from 1.0 and only two closer:

| query | base | refit |
|---|---|---|
| `pow<3 eur>2.55 tou<=6` | 2.01 | **2.70** |
| `id:b tou>=1 tix>0.02` | 1.78 | **2.46** |
| `eur<=2.80 pow>=1 pow<=3` | 1.39 | **1.96** |
| `usd>=0.13 usd<=0.76 cmc>=2 cmc<=5 cn<=126` | 0.26 | 0.35 |

**The tail-dispersion doc warned about exactly this and the refit was proposed without checking against it**: "feature/realized is p10 0.13 / p50 1.00 / p90 2.71 across all 2,441 rows charging the term, so there is no bias to remove and scaling the rate trades the median for the tail", and "must NOT be actioned as a coefficient refit: scaling the term down to fix 26 rows would break the other 1,740."

## The general lesson, which is bigger than this round

`fit_cost_model`'s objective is **per-row relative error, weighted by how often a feature-vector shape recurs**. The router's objective is **picking the fastest plan**, and its errors are tail-concentrated and OPPOSITE in sign to the median. So aggregate accuracy and routing quality are not merely imperfectly correlated here — on the population that matters they moved in opposite directions, measurably. Any future refit has to be gated on the tail queries, not on within-25%.

## Routing headroom, which bounds everything in this queue

Measured as `min` over the timed plans per query, on the same runs:

| population | baseline vs oracle |
|---|---|
| uniform | 1.040x |
| realistic | **1.030x** |

So perfect routing — the empirically fastest plan on every query — would save **3.0%** of routed plan time on the value population. Both figures are UPPER bounds on the headroom: `min` over six noisy measurements is biased low, which flatters the oracle. And routed plan time is only part of end-to-end latency (parse, acquire, dispatch and serialization sit outside it), so the end-to-end ceiling is smaller again.

This is the same shape as the finding already recorded for the And-arm estimator, which governs ~2% of real weighted query time. It should be read as a bound on cost-model work generally, not as a result about this refit.

## What was kept

- The two-tier `counter_check` gate, which is what made compose fittable at all (`93432965`). Independently useful and unaffected by the revert.
- `gathered_scan_zero_match_uses_the_lower_fixed_cost` now composes its expectation from the constants instead of a `175.35` literal, and four constants became `pub(crate)` for it. The literal broke on the refit, which is a false negative — that test is about WHICH terms a single-match round is charged, and every future refit would have failed it the same way.

## Two modelling findings that survive the revert

Both surfaced only because the constants were actually written out, and neither depends on the refit landing:

- **`COMPOSE_LINEAR_PASS_PER_PRINTING_NS` is one constant multiplied by two different features** (`broadcast_printings` at cost.rs:1247 and `project_printings` at :1250), while `design_row` gives them separate columns. The fit therefore reports two values for it, 1.95 and 2.43 — a 26% disagreement that is evidence the legality broadcast-down and the printing->card projection have genuinely different per-printing rates and the constant should be SPLIT. `tests.rs` sums the two features against the shared constant, so splitting touches that mirror too.
- **`STREAM_REDO_SCAN_PER_ROW_NS = STREAM_SCAN_PER_ROW_NS` is confirmed, not contradicted.** The fit reports 7.92 and 8.56 independently — within 8%, which supports the deliberate equality its doc describes.
