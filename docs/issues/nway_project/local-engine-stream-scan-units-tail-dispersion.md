# `stream_scan_units` has no bias and enormous variance — and the variance is what costs

`scan-per-row` is the top-ranked item in [the queue](local-engine-nway-followup-queue.md): 20.1% of tail cost, 1.68× over on the tail, 1.00 in aggregate. Those aggregates say the term is worth fixing. They do not say what a fix looks like, and the obvious reading of them — "the model over-charges StreamedSelect, scale the rate down" — is wrong in a way that would make things worse.

The term is `stream_scan_units * residual_on * STREAM_SCAN_PER_ROW_NS`, with `STREAM_SCAN_PER_ROW_NS = 5.97` against GatheredScan's `GATHER_SCAN_PER_ROW_NS = 2.06`.

## CORRECTED 2026-09-09: the pooled p50 of 1.00 is two populations cancelling

The section below says "the rate is already right, the p50 is 1.00, do not refit". That is correct about the POOLED number and it concealed the real structure. A systematic correlation of the residual against every exposed acquire field found the first covariates to clear Round 69's 0.12 ceiling — `prepare_plane_word_ops` at **r = 0.371** and `broadcast_printings` at **0.333** — and splitting on them separates the medians for the first time:

| bucket | n | p10 | p50 | p90 |
|---|---|---|---|---|
| no plane | 1,128 | 0.13 | **0.81** | 1.91 |
| plane split off + legality broadcast | 579 | 0.55 | **1.39** | **8.67** |

**Opposite median bias — 0.81 under against 1.39 over.** The pooled 1.00 is their average, not a property of either, which is why six earlier splits (orderby, page depth, route, mode, prefer, card-invariance) all read 1.00 in every cell. **Two corrections are available in opposite directions, not none.** Full numbers in [measurements/2026-09-09-scan-per-row-plane-split.txt](measurements/2026-09-09-scan-per-row-plane-split.txt).

The mechanism matches each sign: a plane captures part of the filter, so the residual left for `card_pass` is thinner and P3 settles more cards at card level while `scan_units` still spans the candidates — over-estimating. Without a plane the whole predicate is residual and P3 examines more than the candidate span predicts — under-estimating.

**What it does not explain:** the spread WITHIN each bucket is still 14-16×, and **that part is not reachable.** Re-running the all-fields correlation inside each bucket — where the cancelling bias can no longer mask anything, and with derived ratios added — finds nothing worth acting on. The no-plane bucket's leaders (`stream_scan_units` 0.252, `eval_domain` 0.250, `match_rate` 0.245, `scan_units` 0.243) are mutually collinear size proxies: one weak signal at r ≈ 0.25, not six. The plane bucket has nothing above 0.19.

**So the item's ceiling is now known.** The bias is fixable and the dispersion is not:

- a plane-aware `stream_scan_units` can move each population's median to 1.00, from 0.81 and 1.39
- the ~15× within-bucket spread survives, because no available feature predicts it

**BUILT, MEASURED, AND REVERTED 2026-09-09.** Applying `1/1.39` and `1/0.81` did exactly what it was designed to do — no-plane medians went to 1.00/1.00/1.10 and the plane bucket's p90 improved from 9.33/8.00/8.04 to 6.71/5.67/5.79 — **and it regressed routing.** 32 picked-plan flips, of which 9 faster, **16 slower**, 7 inside noise; net **+0.35 ms**, total routing loss 6.64 -> 7.36 ms.

The mechanism is the item's real obstacle: **StreamedSelect's PLAN prediction is already under at the median (p50 0.85-0.92), so the feature's over-charge was COMPENSATING an under-charge elsewhere in the same arm.** Correcting the feature alone removes the compensation and exposes the under-charge. That is the queue's "the two arms' errors have opposite signs on the tail" one level down — inside a single arm, and the cancellation is load bearing.

So the bias is real, measured, fixable, and **cannot ship alone**. It has to land with whatever removes the compensating under-charge, and that under-charge has not been located. The constants are recorded in [measurements/2026-09-09-scan-per-row-plane-split.txt](measurements/2026-09-09-scan-per-row-plane-split.txt) so the work is not lost — but re-applying them without finding the under-charge reproduces the +0.35 ms.

**Three fixes have now been built for this item** — the card-invariance gate, the redo first-match break, and the plane bias — all correct in isolation. The first two were inert; the third regressed. Nobody should expect the 20× to close. Seven covariate splits and two systematic correlation searches stand behind that. `prepare_plane_word_ops` is already a `PlanFeatures` field, so the correction needs no new plumbing.

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

## Where the defect is, after three splits

Two covariate splits came back empty. A third comparison localizes it, and it rules OUT the two obvious next moves.

**It is not the counter and it is not the loop.** `printings_examined` is a SHARED counter: GatheredScan's `SCAN_PER_ROW` maps to it too, through a different feature and rate. Each plan's feature against its own plan's counter:

| plan | feature | rate | n | p10 | p50 | p90 | p10-p90 |
|---|---|---|---|---|---|---|---|
| GatheredScan | `scan_units` | 2.06 | 6,523 | 0.36 | 1.00 | 1.77 | **4.9×** |
| StreamedSelect | `stream_scan_units` | 5.97 | 2,877 | 0.15 | 1.00 | 2.77 | **18.5×** |

Both medians are exactly 1.00 and `scan_units` is **~4× tighter**. The quantity is predictable, the counter measures it, and one formula in this codebase already predicts it well. So instrumenting the loop is not the next step — the next step is asking what `stream_scan_units` does differently.

**The field's own doc already had the diagnosis, more precisely than any of the splits above.** `PlanFeatures::stream_scan_units` (`cost.rs:113-124`) records `scan_units` against realized `printings_examined` on the compose acquire: `f:modern`/artwork reads GatheredScan **1.38** against StreamedSelect **13.09**; `f:gladiator`/artwork **1.62** against **14.98**. "Right for P4 to within 1.4-1.6x, wrong for P3 by 13-15x... a FEATURE error, the one class no rate can absorb." `stream_scan_units` exists to fix that, and `mk_plan_feats` defaults it to `scan_units` "so a branch that has not been taught reads exactly as before."

So the obvious next step is "teach the untaught branches" — and **that is wrong too.** Splitting by acquire route:

| route | taught? | n | p10 | p50 | p90 | p10-p90 |
|---|---|---|---|---|---|---|
| `printing_compose` | **YES** (legality divergent share) | 1,797 | 0.18 | 1.00 | 3.58 | **20.0×** |
| `candidates` | partly (`residual_card_invariant` -> 0) | 757 | 0.61 | 1.00 | 1.64 | **2.7×** |
| `printing_range_scan` | no | 142 | 0.06 | 1.00 | 1.00 | 16.2× |
| `card_range_popcount` | no | 114 | 0.13 | 0.70 | 1.16 | 8.9× |
| `plane` | no | 67 | 0.00 | 0.00 | 0.00 | — |

**The taught branch is the worst, and it carries 62% of the population.** `candidates`, the least taught, is the tightest at 2.7×. The genuinely untaught routes are 323 rows between them. So the work is not spreading the fix — it is that compose's fix does not cover compose.

That follows from the correction's own scope: it is the legality-divergent SHARE of the candidate span, and the field doc says "for a filter with no legality leaf the share is 1.0 and this reduces to `scan_units`". So on every compose row WITHOUT a legality leaf, `stream_scan_units` is still the value measured wrong by 13-15× for P3. **That is the target: compose rows with no legality leaf.**

Two smaller, cleaner defects sit beside it. `plane` reads feature 0.00 at every percentile while the executor examines printings — a straightforwardly missing charge on 67 rows. And `card_range_popcount` sits at p50 **0.70**, the only route with a biased median rather than a dispersed one.

**RETRACTED — the P3-override lead recorded earlier on 2026-09-09 was confounded.** It split on `stream_scan_units != scan_units` and reported the firing rows as 2.1× more dispersed (29.7× against 14.0×), with `stream_scan_units/scan_units` reaching p90 3.00. That bucket mixes compose's divergent-share correction with the deliberate `stream_scan_units = 0` writes at `lib.rs:17565` and the `tier == 0` arm at `lib.rs:18494`; the zeros drag its p10 to 0.13 and manufacture the spread. The p90 3.00 wants re-checking on the divergent-share rows alone before it is treated as an anomaly.

**Two moves this rules out:** folding into `walk-variable-check` (that item is `printings_walked`, PrintingCompose's `WALK_STEP` — a different plan and a different loop; the terms share a signature, not a mechanism), and instrumenting the scan loop (the counter and loop are fine, per the table above).

## The mechanism, and why the fix generalizes

P4 walks each candidate card's whole printing span to push every match, so `scan_units` is real work for it. P3 uses `card_match_count`, which answers from SPAN ARITHMETIC for any card `card_pass` settles at card level, and examines printings only where `card_pass` returns `PrintingDep`.

Legality is card-level for almost every card, so on a legality filter P3 settles the lot without touching a printing — except the divergent remainder, **556 of 31,508 cards, all in `oldschool`**. Hence 7,770 examined against P4's 73,783 on `f:modern`.

**The correction is not really about divergence.** It is `share = |legal_divergent| / n_cards`, a GLOBAL constant of ~1.8% with no dependence on which format the filter names — so it is applied to `f:modern` and `f:pauper` as readily as to the one format that actually diverges. And it is floored: `max(scan_base * share, eval_domain)`. At 1.8% of a ~100k span the share term is ~1,800 while `eval_domain` is usually larger, so **the floor is the operative content — one printing per candidate card — and the share is dominated by it.** It is dressed as a divergence model and behaves as a per-card floor.

The `!touches_printing_field` half of the gate is load-bearing and should not be confused with the share: one printing-varying partner makes `card_pass` return `PrintingDep` for EVERY card, so P3 walks the full span like P4. Scoped on legality alone the correction charged 2,755 for the whole `f:X border:white` family against a realized 5,353-19,737, and `f:modern border:white` measured 100.9 us on the plan it handed the query to against 44.3 us for the compose it passed over.

**The general rule already exists, on the other branch.** The principle is "P3 examines printings only where `card_pass` cannot settle at card level", and legality is one card-level field among many — `cmc`, `power`, `toughness`, type and subtypes all qualify. `candidates` implements exactly that:

```rust
if feats.residual_card_invariant { feats.stream_scan_units = 0; }
```

— "the `all_match_known` gate one step weaker... this needs only that it cannot vary within a card, which `name:s`, `o:`, `t:` and `cmc` all satisfy". **That is why `candidates` measures 2.7x and compose 20x.** And compose HAS the signal: `lib.rs:18481` sets `feats.residual_card_invariant = composed_card_invariant` and then computes `stream_scan_units` from the legality special case instead of from it.

So the fix is to make compose use its own card-invariance the way `candidates` does. **The 0-vs-`eval_domain` disagreement is now SETTLED by measurement: zero is right.** Over all 1,330 rows with `residual_card_invariant` true and StreamedSelect timed, P3 examines no printings on **1,315 (98.9%)** — p50 and p90 both 0 on both routes ([measurements/2026-09-09-card-invariant-printings-examined.txt](measurements/2026-09-09-card-invariant-printings-examined.txt)).

The 15 exceptions are the divergent format itself, and they show compose's floor is wrong in BOTH directions at once:

| row | `printings_examined` | `eval_domain` |
|---|---|---|
| `f:oldschool` / artwork | **10,991** | 961 |
| `devotion:rrr f:oldschool` / printing | 15 | 5 |
| `cmc+1<pow f:duel` / printing | 41 | 417 |

Non-divergent legality is charged `eval_domain` where the truth is **0**; `f:oldschool` is charged `eval_domain` where the truth is **~11.4× it**. A single global share — `|legal_divergent| / n_cards`, format-blind — cannot be right for both, so the arm lands between the two answers. That is the 20.0× spread.

**A secondary finding constrains the fix:** `residual_card_invariant` reads TRUE for `f:oldschool`, because `Legality` returns false from `touches_printing_field` — its own comment says it "ranks by the common card-level case". The flag is optimistic exactly where divergence lives, so a fix keyed on the flag alone inherits that. Separating the two populations needs a FORMAT-aware divergence test, not the flag.

## The divergent case is not fittable, so do not try

Checked before designing an arm around the 11.4×. Divergence is exactly one format — confirmed against the corpus, not taken from the comment: **556 of 31,724 cards (1.75%), all `oldschool`**, and no other format has a single card whose legality differs across printings.

Over 20,000 uniform queries, 42 rows name oldschool with StreamedSelect timed, 33 with a usable denominator:

```
printings_examined / eval_domain:   min 0.00    p50 6.28    max 53.55
```

**The 11.4× was one point in a 0-to-53× spread.** There is no constant to fit; an arm built on it would repeat the global-share mistake one level down. Two further numbers: `residual_card_invariant` reads true on only 15 of 42 (so the flag's optimism depends on the partner leaves), and **StreamedSelect is the best plan on just 3 of 42** — so charging 0 there would make a usually-not-best plan look free.

Real-traffic exposure is **1 query of 14,473** (0.006% by weight).

**Decision (2026-09-09): accept being wrong on oldschool to be right everywhere else.** Supported twice over — it is rare *and* unfittable.

**The recommended shape, which is a cleaner way to be wrong than charging 0:**

```rust
if composed_card_invariant && !filter_touches_divergent_format { stream_scan_units = 0 }
```

The 98.9% gets the measured-correct `0`, and oldschool falls through to the existing `scan_units` default rather than being actively charged zero for a plan that examines ~11k printings. The gate needs the FORMAT, not `residual_card_invariant` — that flag reads true for oldschool, because `Legality` returns false from `touches_printing_field`.

## Implemented, and it reaches 0.9% of queries

The gated rule was built — `filter_touches_divergent_format` over `BitPlanes::divergent_formats`, which already existed and needed no archive change. **It is correct and it is nearly a no-op**, which corrects the hypothesis above rather than confirming it. Full numbers in [measurements/2026-09-09-card-invariant-gate-result.txt](measurements/2026-09-09-card-invariant-gate-result.txt).

`stream_scan_units` changed on **69 of 7,945 queries (0.9%)**, all compose, all to 0. Of the 1,840 rows with `residual_card_invariant` true only 69 moved — the rest already had 0 from the `tier == 0` arm. Card-invariant AND `tier > 0` turns out to be rare. Effect: StreamedSelect pred/measured p50 0.91 -> 0.90 with an unchanged 3.8× spread, one picked-plan flip in 7,944, and a routing-loss delta of 2.49 -> 2.08 ms that rests on that single flip and should be read as noise.

**Two corrections follow.**

**The metric was blind.** "How much narrower can we get the spread" cannot see this fix: on a card-invariant row the realized counter is 0, so the row is excluded from any feature/realized ratio by the divide-by-zero guard. Setting the feature to 0 where truth is 0 is invisible to that metric BY CONSTRUCTION, so the unchanged spread is not evidence of failure.

**The hypothesis was wrong regardless.** The chain was "compose grades 20.0× against candidates' 2.7× because compose's fix does not cover compose". The extension reaches under 1% of queries, so missing card-invariance is not what disperses compose's rows. **The dispersed compose rows have `tier > 0` and are NOT card-invariant** — genuinely printing-varying residuals where P3 does walk spans, and where `stream_scan_units` falls through to the big `else` branch: Round 30's redo-pass calibration, fit as a wall-clock RESIDUAL because no structural counter existed for the redo's real work. That is where the 20× lives.

## Where the remaining error is: the small-total redo branch

The counters needed to go after the `else` branch already exist — Round 31 added `PhaseStats::redo_examined` and refit directly against it, and a separate `REDO_SCAN_PER_ROW` term consumes it. So Round 30's wall-clock-residual chain is already superseded.

A double-charge looked likely (the `else` branch adds `2.237 * redo_candidates` into `stream_scan_units` in CARD MODE only, while `REDO_SCAN_PER_ROW` charges the redo again at the same 5.97 rate) and is **refuted**: card mode and printing/artwork have nearly identical profiles, and small-total rows are UNDER-predicted at the median, the wrong direction for a double charge.

What the split shows instead is the sharpest localization this item has:

| bucket | n | p10 | p50 | p90 |
|---|---|---|---|---|
| printing/artwork + large | 2,317 | 0.44 | 1.10 | **1.62** |
| card mode + large | 1,070 | 0.35 | 1.11 | **1.64** |
| printing/artwork + small-total | 1,713 | 0.52 | 0.82 | **6.26** |
| card mode + small-total | 917 | 0.55 | 0.86 | **6.76** |

**All of StreamedSelect's dispersion is in the small-total redo branch.** Large-total rows predict at p90 1.62-1.64, which is fine; the small-total branch is p90 6.26-6.76.

**And that branch's own term names the missing quantity.** `stream_redo_printings`' doc reads p10 0.19 / p50 0.93 / p90 3.38 against `redo_examined`, and says "the tail is the cardinality estimate arriving through `matches` rather than this shape" — its per-card printing count is the CORPUS ratio `n_printings / n_cards`, "because nothing on `PlanFeatures` describes the printing span of the MATCHING subset specifically — `scan_units` spans the candidates, not the matches."

So the missing quantity has a name: **the printing span of the matching subset.** That is a missing FEATURE, which is exactly the signature this item has shown throughout, and no refit of either redo constant reaches it.

## The named gap, found: the redo feature ignores an executor fastpath that already shipped

Asked whether teaching the executor to skip a card's remaining printings once a match is found would help. **It already does that, where sound** — `push_card_matches`' Mode::Card arm under `Prefer::Default` is `(start..end).find(..)` and records `examined` as the offset from `start`, not the span, because printings are stored in descending default-prefer order so the first match is the chosen one (Round 68). Custom prefer must score every printing; printing/artwork mode needs every matching printing by definition.

**The cost model was never told.** `stream_redo_printings` takes the per-card printing count as the CORPUS ratio `n_printings / n_cards`. Measured against the realized `redo_examined`, card-mode small-total rows:

| bucket | n | p10 | p50 | p90 |
|---|---|---|---|---|
| `Prefer::Default` (breaks early) | 156 | 0.36 | **3.06** | 4.18 |
| custom prefer (scores all) | 588 | 0.16 | **0.82** | 2.33 |

**The over-estimate is exactly the reprint ratio**: `n_printings / n_cards` = 97,812 / 31,724 = **3.083** against a measured 3.06. The feature charges the ratio per matching card and the executor examines about one. Under custom prefer the same feature reads 0.82. So this is not calibration drift — it is one branch of a two-branch executor being priced with the other branch's formula.

**And the discount already exists here.** `scan_all(domain_cards, card_first_match_break)` applies exactly this correction to `scan_units`, and `card_first_match_break` is already computed at `lib.rs:18085` as `Mode::Card && Prefer::Default`. `stream_redo_printings` does not take the parameter.

**BUILT 2026-09-09.** `PlanFeatures::card_first_match_break`, set once in `mk_plan_feats`, and `stream_redo_printings` returns one printing per matching card on that branch. The feature goes **p50 3.06 -> 1.00, p90 4.18 -> 1.35** where the break applies, and is untouched where it does not. Zero picked-plan flips in 7,842; routing loss 6.66 -> 6.64 ms.

Two limits are recorded honestly in [measurements/2026-09-09-redo-first-match-break.txt](measurements/2026-09-09-redo-first-match-break.txt):

- **The plan number moved the wrong way** — StreamedSelect was already under-predicted on these rows, so removing a 3× over-charge pushed its median from 0.92 to 0.85. The over-charge was partly compensating other under-charges.
- **This is NOT the item's headline defect.** The example queries and the pooled distribution are unchanged, because their error is in `SCAN_PER_ROW` (`stream_scan_units`) while the fix landed on `REDO_SCAN_PER_ROW` (`stream_redo_printings`). Two different terms. **`SCAN_PER_ROW`'s 20× compose dispersion remains unexplained.**

Kept because `fit_cost_model` fits rates AGAINST these features, so a feature 3× wrong on a subpopulation corrupts any rate fitted on it. Also checked: `stream_redo_cards` does NOT share the blind spot — `CARD_PASS+FLOOR` grades p50 1.00 in both prefer buckets, since the break is per-printing within a card and the redo still visits every matching card.

## The pair failure, which caps what this item can deliver

`PrintingCompose` is under-predicted on the same rows — 0.30×, 0.63×, 0.67×, 0.77×, 0.81×, 0.87× — so these mis-picks are doubly wrong, and fixing one arm does not fully fix the comparison. Two of the six flips land on `PrintingCompose` rather than the measured best plan, because compose's own under-prediction still wins the argmin after StreamedSelect is corrected.

So `scan-per-row` is necessary and not sufficient. The remaining error belongs to compose, and the terms it would live in are the ones [the queue](local-engine-nway-followup-queue.md)'s `gather-tail` item records as over-represented on the tail and **not yet graded against any counter**.

## What a fix has to do

- **Not scale `STREAM_SCAN_PER_ROW_NS`.** The p50 is 1.00; there is no bias to remove.
- **Not search for another covariate.** `orderby` and page depth are both ruled out here by direct measurement, and Round 69 ruled out `match_rate`, page depth and the estimate itself for the sibling term at max |r| 0.12. Treat the shape as a missing term and go at the loop, which is what `walk-variable-check` proposes.
- **Be graded on the pair, not the arm.** Single-arm accuracy is what let two independent sightings read this as a rate problem. The gate is `bench_pairwise_ordering.py` plus a picked-plan diff.

## Evidence trail

`scripts/bench_scan_per_row_rows.py` produces the table above; raw output in [measurements/2026-09-09-scan-per-row-rows.txt](measurements/2026-09-09-scan-per-row-rows.txt). The tail decomposition that ranked the item is Round 84 in [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md); the second sighting is [measurements/2026-09-09-streamedselect-perm-overcharge.txt](measurements/2026-09-09-streamedselect-perm-overcharge.txt).
