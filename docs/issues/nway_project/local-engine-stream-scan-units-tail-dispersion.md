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

| query | orderby | unique | limit/off | feature | realized | ratio | term's share |
|---|---|---|---|---|---|---|---|
| `usd>=0.13 usd<=0.76 cmc>=2 cmc<=5 cn<=126` | cubecobra/desc | card | 175/100 | 17,276 | 50,443 | **0.34×** | 34.0% |
| `pow>=1 pow<=3 r>=common` | toughness/desc | card | 10/0 | 31,196 | 12,256 | 2.55× | 56.2% |
| `cmc>=6 usd<7.31 eur>=0.51 eur<=3.82` | cmc/asc | card | 10/100 | 5,928 | 2,073 | 2.86× | 47.1% |
| `tou>=1 tou<=2 tix>=0.22` | cmc/desc | artwork | 175/100 | 19,261 | 4,713 | **4.09×** | 62.2% |
| `id:bgu frame:2015 pow>=2 pow<=4` | edhrec/asc | artwork | 100/0 | 28,126 | 14,186 | 1.98× | 49.3% |
| `pow<3 eur>2.55 tou<=6` | cmc/desc | artwork | 10/100 | 25,344 | 5,861 | **4.32×** | 62.6% |
| `eur<=2.80 pow>=1 pow<=3` | toughness/asc | card | 10/0 | 31,196 | 14,469 | 2.16× | 56.2% |
| `id:b tou>=1 tix>0.02` | cmc/desc | artwork | 175/100 | 27,345 | 9,013 | 3.03× | 62.5% |

Four things fall out that the aggregates hide.

**The term dominates the prediction it sits in** — 34% to 63% of StreamedSelect's whole predicted cost on these rows. A 4× error in a term carrying 62% moves the argmin on its own. This is why it outranks terms with worse ratios: `PERM_STEP` is 0.47× on the tail and carries under 0.7% of tail cost.

**The error runs BOTH ways, and the single costliest row is the under-estimate.** `usd>=0.13 usd<=0.76 cmc>=2 cmc<=5 cn<=126` reads 17,276 against a realized 50,443 — **0.34×** — StreamedSelect was priced at 303 µs, ran in 1,021 µs, was picked, and lost. Every other row in the table is an over-estimate. "Over-charging StreamedSelect" describes seven of eight rows and inverts the worst one.

**Correcting the FEATURE alone flips 7 of the 8 mis-picks.** Set `stream_scan_units` to its realized counter, leave every rate alone, re-run the argmin: six rows move to the plan that was actually fastest. So this is reachable by estimator work, which is the strongest thing that can be said for an item in this arc.

**Every row is `paging=Perm` and every row carries an arithmetic range** — but the obvious reading of that is REFUTED, and it was in this doc for an hour before being checked.

The hypothesis was that the segment diverges when the filter constrains a column the permutation is NOT ordered by, so `walk_bounds` can bound nothing. Two things kill it. **Row 3 constrains `cmc` and is ordered BY `cmc`** — the one row where the bound should be tight — and is still 2.86× over. And splitting the whole population by `orderby` is flat:

| orderby | n | p10 | p50 | p90 |
|---|---|---|---|---|
| toughness | 408 | 0.15 | 1.00 | 2.35 |
| cmc | 392 | 0.15 | 1.00 | 3.07 |
| power | 405 | 0.12 | 1.00 | 2.71 |
| edhrec | 440 | 0.15 | 1.00 | 2.42 |
| cubecobra | 398 | 0.14 | 1.00 | 2.79 |
| name | 401 | 0.10 | 1.00 | 2.92 |

Every permutation carries the same perfect median and the same ~20× spread. `orderby` explains none of the dispersion, so this is not the walk-bounds story and no per-orderby constant reaches it either — which also rules out the fix that `printings-walked-per-sort` proposes for its own term.

**What the rows have in common is a large predicted segment against a small examined one** — feature 19k-31k on six of eight while the executor examines 4.7k-14.5k. The reading was early termination: the walk fills its page and stops while the feature predicts the whole segment. **Tested, and refuted too.**

feature/realized by page depth, `(offset + limit) / matches`:

| depth | n | p10 | p50 | p90 |
|---|---|---|---|---|
| < 0.01 (shallow) | 606 | **0.50** | 1.00 | **1.91** |
| 0.01 - 0.05 | 419 | 0.27 | 0.81 | 2.18 |
| 0.05 - 0.20 | 300 | 0.07 | 1.00 | 2.83 |
| 0.20 - 0.50 | 211 | 0.13 | 1.00 | 3.01 |
| 0.50 - 1.00 | 186 | 0.14 | 1.00 | 6.90 |
| **>= 1.00 (whole)** | 722 | **0.07** | 1.00 | **3.00** |

The hypothesis predicted the worst over-estimate at shallow depth, settling to 1.00 as the page needs the whole result. The opposite happens: shallow depth has the TIGHTEST spread, and **at depth >= 1.00, where the page needs every match and early termination is impossible, the ratio still spans 0.07-3.00**. If the feature predicted the segment and the walk traversed all of it, that cell would read ~1.00. It does not. **So the error is not about when the walk stops — it is about what the segment IS.**

**This search has precedent and it already concluded empty.** `stream_perm_steps`' own doc comment (`cost.rs:1074`) records for the sibling term: "nothing already on `PlanFeatures` predicts the residual (max |r| 0.12 against `match_rate`, page depth, and the estimate itself)". Page depth is named there explicitly. Two covariates are now ruled out for THIS term by direct measurement, and Round 69 ruled out the obvious set for the sibling — so the next person should not spend another round on covariate search.

**That promotes the remaining reading:** the feature may be measuring the wrong quantity rather than measuring the right one badly. It is exactly the question [the queue](local-engine-nway-followup-queue.md)'s `walk-variable-check` asks of `printings_walked`, and it now generalizes to `stream_scan_units`. A perfect median with 20x dispersion that no available covariate predicts is the signature of a MISSING TERM, not of a mis-scaled one.

## The pair failure, which caps what this item can deliver

`PrintingCompose` is under-predicted on the same rows — 0.30×, 0.63×, 0.67×, 0.77×, 0.81×, 0.87× — so these mis-picks are doubly wrong, and fixing one arm does not fully fix the comparison. Two of the six flips land on `PrintingCompose` rather than the measured best plan, because compose's own under-prediction still wins the argmin after StreamedSelect is corrected.

So `scan-per-row` is necessary and not sufficient. The remaining error belongs to compose, and the terms it would live in are the ones [the queue](local-engine-nway-followup-queue.md)'s `gather-tail` item records as over-represented on the tail and **not yet graded against any counter**.

## What a fix has to do

- **Not scale `STREAM_SCAN_PER_ROW_NS`.** The p50 is 1.00; there is no bias to remove.
- **Not search for another covariate.** `orderby` and page depth are both ruled out here by direct measurement, and Round 69 ruled out `match_rate`, page depth and the estimate itself for the sibling term at max |r| 0.12. Treat the shape as a missing term and go at the loop, which is what `walk-variable-check` proposes.
- **Be graded on the pair, not the arm.** Single-arm accuracy is what let two independent sightings read this as a rate problem. The gate is `bench_pairwise_ordering.py` plus a picked-plan diff.

## Evidence trail

`scripts/bench_scan_per_row_rows.py` produces the table above; raw output in [measurements/2026-09-09-scan-per-row-rows.txt](measurements/2026-09-09-scan-per-row-rows.txt). The tail decomposition that ranked the item is Round 84 in [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md); the second sighting is [measurements/2026-09-09-streamedselect-perm-overcharge.txt](measurements/2026-09-09-streamedselect-perm-overcharge.txt).
