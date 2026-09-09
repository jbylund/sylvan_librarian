# `stream_scan_units` has no bias and enormous variance — and the variance is what costs

`scan-per-row` is the top-ranked item in [the queue](local-engine-nway-followup-queue.md): 20.1% of tail cost, 1.68× over on the tail, 1.00 in aggregate. Those aggregates say the term is worth fixing. They do not say what a fix looks like, and the obvious reading of them — "the model over-charges StreamedSelect, scale the rate down" — is wrong in a way that would make things worse.

The term is `stream_scan_units * residual_on * STREAM_SCAN_PER_ROW_NS`, with `STREAM_SCAN_PER_ROW_NS = 5.97` against GatheredScan's `GATHER_SCAN_PER_ROW_NS = 2.06`.

## The refit trap, stated first because it is the whole point

Feature over realized counter (`stream_scan_units` against `printings_examined`), across all **2,441** rows that charge the term, uniform sampler, 8,000 queries:

| p10 | p50 | p90 |
|---|---|---|
| **0.13** | **1.00** | **2.71** |

**The rate is already right.** A refit scales the rate, which moves every one of those 2,441 rows, and the median is exactly 1.00 — so any scaling makes the median worse in exchange for the tail. What the feature lacks is not a constant but a shape: a 20× spread between p10 and p90 at a perfect median. This is the same diagnosis `printings_walked` carries in the queue, and it is why the item must not be actioned from its "1.68× over" summary.

Two independent sightings have now been read as a rate problem — Round 84's tail decomposition and the 2026-09-09 report against `main` — so the misreading is the expected one, not a hypothetical.

## The rows themselves

The eight costliest by LOSS (picked minus best measured). All eight had a mirror-exact rebuild, so the term breakdown is the router's own arithmetic, not a re-derivation.

| query | unique / off | feature | realized | ratio | term's share of the prediction |
|---|---|---|---|---|---|
| `usd>=0.13 usd<=0.76 cmc>=2 cmc<=5 cn<=126` | card / 100 | 17,276 | 50,443 | **0.34×** | 34.0% |
| `pow>=1 pow<=3 r>=common` | card / 0 | 31,196 | 12,256 | 2.55× | 56.2% |
| `cmc>=6 usd<7.31 eur>=0.51 eur<=3.82` | card / 100 | 5,928 | 2,073 | 2.86× | 47.1% |
| `tou>=1 tou<=2 tix>=0.22` | artwork / 100 | 19,261 | 4,713 | **4.09×** | 62.2% |
| `id:bgu frame:2015 pow>=2 pow<=4` | artwork / 0 | 28,126 | 14,186 | 1.98× | 49.3% |
| `pow<3 eur>2.55 tou<=6` | artwork / 100 | 25,344 | 5,861 | **4.32×** | 62.6% |
| `eur<=2.80 pow>=1 pow<=3` | card / 0 | 31,196 | 14,469 | 2.16× | 56.2% |
| `frame:showcase cmc>=4` | artwork / 0 | 6,226 | 4,547 | 1.37× | 55.9% |

Four things fall out that the aggregates hide.

**The term dominates the prediction it sits in** — 34% to 63% of StreamedSelect's whole predicted cost on these rows. A 4× error in a term carrying 62% moves the argmin on its own. This is why it outranks terms with worse ratios: `PERM_STEP` is 0.47× on the tail and carries under 0.7% of tail cost.

**The error runs BOTH ways, and the single costliest row is the under-estimate.** `usd>=0.13 usd<=0.76 cmc>=2 cmc<=5 cn<=126` reads 17,276 against a realized 50,443 — **0.34×** — StreamedSelect was priced at 303 µs, ran in 1,021 µs, was picked, and lost. Every other row in the table is an over-estimate. "Over-charging StreamedSelect" describes seven of eight rows and inverts the worst one.

**Correcting the FEATURE alone flips 6 of the 8 mis-picks.** Set `stream_scan_units` to its realized counter, leave every rate alone, re-run the argmin: six rows move to the plan that was actually fastest. So this is reachable by estimator work, which is the strongest thing that can be said for an item in this arc.

**Every row is `paging=Perm` and every row carries an arithmetic range.** Seven of eight are two-sided or multi-range numeric conjunctions (`usd>=..usd<=..`, `cmc>=..cmc<=..`, `pow>=1 pow<=3`, `tou>=1 tou<=2`, `eur>=..eur<=..`), and the eighth pairs `frame:showcase` with `cmc>=4`. That is a shape hypothesis with a mechanism to look for, not a population artifact: it says the walked segment `stream_scan_units` predicts diverges from what the executor examines specifically when the filter constrains a numeric column that the permutation is not ordered by.

## The pair failure, which caps what this item can deliver

`PrintingCompose` is under-predicted on the same rows — 0.30×, 0.63×, 0.67×, 0.77×, 0.81×, 0.87× — so these mis-picks are doubly wrong, and fixing one arm does not fully fix the comparison. Two of the six flips land on `PrintingCompose` rather than the measured best plan, because compose's own under-prediction still wins the argmin after StreamedSelect is corrected.

So `scan-per-row` is necessary and not sufficient. The remaining error belongs to compose, and the terms it would live in are the ones [the queue](local-engine-nway-followup-queue.md)'s `gather-tail` item records as over-represented on the tail and **not yet graded against any counter**.

## What a fix has to do

- **Not scale `STREAM_SCAN_PER_ROW_NS`.** The p50 is 1.00; there is no bias to remove.
- **Predict the walked segment better on arith-range conjunctions**, which is where the dispersion lives. The shape hypothesis above is the place to start, and it is testable directly: regress the realized `printings_examined` against the filter's constrained columns versus the permutation's sort column.
- **Be graded on the pair, not the arm.** Single-arm accuracy is what let two independent sightings read this as a rate problem. The gate is `bench_pairwise_ordering.py` plus a picked-plan diff.

## Evidence trail

`scripts/bench_scan_per_row_rows.py` produces the table above; raw output in [measurements/2026-09-09-scan-per-row-rows.txt](measurements/2026-09-09-scan-per-row-rows.txt). The tail decomposition that ranked the item is Round 84 in [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md); the second sighting is [measurements/2026-09-09-streamedselect-perm-overcharge.txt](measurements/2026-09-09-streamedselect-perm-overcharge.txt).
