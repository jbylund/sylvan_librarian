# The cost model has no mode, sort-column or prefer axis — and pays for it repeatedly

`cost::PlanFeatures` carries **24 fields and none of them is `mode`, `prefer`, `sort_col` or
`descending`.** `plan_cost` therefore cannot branch on any of the three variables that most change what
the executors do. Every one of those conditions is instead encoded *indirectly*, as a convention about
which feature the acquire sets to zero or switches to a different quantity.

That indirection works. It has also produced the same class of defect five times in twenty rounds, and
it is the reason several terms are stuck at "median fixed, dispersion untouchable".

This doc records the observation, the evidence, and what a fix would have to be. It is not scheduled.

## How the three axes are encoded today

Entirely inside `acquire_plan_features`, by choosing feature VALUES:

| feature | what encodes an axis |
|---|---|
| `artwork_seen_cards` | `eval_domain` in artwork mode, `0` in card and printing |
| `artwork_seen_printings` | `scan_units` in artwork mode, `0` in card and printing |
| `project_printings` | `0` in printing mode (no projection pass exists) |
| `gather_group_printings` | `0` in printing mode and in card mode under `Prefer::Default` |
| `compose_scan_printings` | `eval_domain` under card + `Prefer::Default`, `printing_matches` otherwise |
| `stream_scan_units` | diverges from `scan_units` on a card-invariant residual |
| `perm_walk_span` | `walk_bounds(..)` for this `(sort_col, descending)`, else `n_cards` |

So the model *does* know about mode and prefer — it just knows through seven separate conventions
rather than three variables, and each convention has to be restated at every site that needs it.

## The evidence that this is costly

**The same condition, spelled independently at three sites, wrong the same way at all three.**
Round 74 gated `scan_all`'s first-match depth discount on `matches!(mode, Mode::Card)`. Round 79 found
that was half the condition — `push_card_matches`' early-breaking arms are guarded
`matches!(prefer, Prefer::Default)`, so under a scoring prefer the kernel examines every printing and
the discount should not apply. **Three sites each spelled it `matches!(mode, Mode::Card)`**: the depth
term, `card_invariant_domain_exact`, and `nothing_to_verify` — the last with a comment arguing for the
prefer split, shipped without it. A carried `prefer` would have made that one predicate, checked once.

**Round 74 itself was the same shape**: a card-mode order statistic applied to printing and artwork,
under-charging by **3x at the median** (`scan_units <GatheredScan> / printing` p50 0.400).

**Round 66** found `gather_composed_page` takes one of three per-card arms keyed on `(mode, prefer)`
while one feature priced all three; splitting moved a cell p50 **5.040 -> 1.000**.

**Round 72** found a span multiplier pooled across two regimes, pure over-charge on one of them.

**The sort column is the axis with no encoding at all, and it is where the dispersion lives.**
`stream_perm_steps` grades at a flat median (0.918-1.183) with dispersion running **1.9x on
`orderby=name` to 38.8x on `orderby=cmc`** — `name` order is uncorrelated with any filter so uniform
density holds, `cmc` correlates with the predicates queries actually use so matches clump.
`printings_walked`'s per-column MEDIANS span **0.925 to 3.579** against one shipped
`WALK_LENGTH_BIAS = 1.45`. Round 80 found `SELECT_PER_PAGE_SLOT` flagged UNDER-COUNTS in **every**
acquire route, **every** distinct-on and **every** orderby simultaneously.

**The executor already branches on the sort column where the model does not.**
`orderby_walk_available(sort_col)` is literally `matches!(sort_col, PriceUsd | Rarity)` — the two
columns with no permutation — and compose's two walk arms partition on exactly that. `cost.rs` prices
them with one shared arm. Measured, that merge happens to be right (residual medians 1.277 vs 1.449),
but it is right by luck: the sort column's real effect cuts *across* the paging boundary, since
`rarity` (2.967) and `usd` (1.049) are both `OrderbyWalk` and 2.8x apart.

**Mode disagreement reaches the sign of a conclusion.** Round 83's lane measured `predicted/executor`
by plan and found uniform and realistic disagree on *which plan over-charges* (uniform GS 0.989 /
SS 1.095; realistic GS 1.358 / SS 1.156). A single pooled rate cannot be right for both.

**The repo already suspected this.** `fit_cost_model --by-mode` exists precisely to fit each arm per
distinct-on and flag terms that move more than `MODE_SPLIT_FACTOR = 1.3` between them, with its own
doc saying a divergent term means "the arm is missing a mode-dependent term and no single set of
constants can serve all three".

## Why it was built this way, and what that argues

The division of labour is deliberate and defensible: the ACQUIRE knows mode and prefer and can compute
the right quantity once, while `plan_cost` stays a pure function of a flat feature vector — cheap,
mirror-able in Python (`fit_cost_model.design_row`, held at 100% agreement), and free of the
combinatorial branch explosion a 3-mode x 5-prefer x 8-sort arm would invite.

**So the fix is not "add three fields and branch on them everywhere".** That would trade seven
conventions for a 120-cell arm nobody can fit. The evidence points somewhere narrower:

1. **The sort column is the axis genuinely missing**, not merely implicit. Mode and prefer at least
   reach the model through feature values; the sort column reaches it only through `perm_walk_span`,
   which collapses to `n_cards` on 94-98% of rows. What the walk terms need is not a `sort_col` field
   but a **filter-vs-sort-column correlation statistic** — the quantity that says whether matches clump
   in this order. Nothing on `PlanFeatures` predicts it (max |r| 0.12 against every existing feature),
   so it is a build, and `SpaceTotals`' spec comment records the closest available shape.
2. **Where a condition IS shared across sites, name it once.** Round 79 did this with
   `card_first_match_break` after three sites disagreed. That is cheap, catches the recurring defect,
   and needs no new field.
3. **Grade per axis by default.** `bench_feature_accuracy` already slices by `unique`, `prefer` and
   `orderby`; the defects above were all findable there and were found late because pooled medians
   hid them. `bench_error_attribution_weighted`'s per-term table should slice the same way.
4. **A per-axis constant is only justified where the executor's WORK differs**, not where a pooled fit
   happens to be off. Round 82's `materialize_cost` is the counter-example worth remembering: the two
   materializing plans call the *same* `prepare_candidates` with no plan argument, so per-plan
   constants there would be fitting a fudge factor — and measured, that variant was the worst of six.

## Measured: at the AGGREGATE level, neither axis is a discriminator

The measurement this doc originally called for has been done — `bench_error_attribution_weighted.py`
now slices mass by `orderby` and `prefer` alongside `unique`. **Both axes come back flat.** 8,000
uniform queries, 5,911 picked rows:

| sort column | row% | err mass% | median \|log\| |
|---|---|---|---|
| `cubecobra` | 12.7% | 13.6% | 0.358 |
| `usd` | 13.1% | 13.2% | 0.304 |
| `edhrec` | 13.4% | 13.0% | 0.327 |
| `rarity` | 11.9% | 12.4% | 0.333 |
| `power` | 12.0% | 12.3% | 0.329 |
| `name` | 12.1% | 11.9% | 0.308 |
| `toughness` | 12.8% | 11.9% | 0.321 |
| `cmc` | 11.9% | 11.7% | 0.340 |

Every slice carries mass in proportion to its rows, and the median |log| spans **0.054** across all
eight columns. `prefer` is the same: mass% tracks row% within 0.8 pp on all five values, median |log|
spanning **0.037**. Distinct-on was already known flat (33.5 / 33.3 / 33.2%).

**So do not build a sort-column axis expecting to move the aggregate.** The per-term axis effects in
Rounds 69, 74 and 79 are real and were worth fixing, but they live in terms that are now small —
`WALK_STEP` is 1.1% of total mass and `PERM_STEP` 0.3%. The dominant remaining error (the unpriced
candidate build, and whatever survives in GatheredScan's arm) is axis-INDEPENDENT, and it is large
enough to make the axes look flat even where they are not.

That reframes the whole doc. The argument for carrying `mode`/`prefer`/`sort_col` is **not** accuracy
mass — it is the recurring-defect argument in the section above: the same condition spelled at three
sites and wrong at all three, five times in twenty rounds. That is a maintainability case, and it
should be argued as one rather than implying a latency win, per this directory's own scoping rule.

Two caveats on the negative. It is measured on PICKED rows' total cost error, so an axis effect that
changes WHICH plan wins without changing total error would not show here. And a flat aggregate is
consistent with axis effects in several terms cancelling; the per-term view is the one that found them
before, and it should stay the instrument of record for that question.

See [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)
for the round-by-round evidence, particularly Rounds 66, 69, 72, 74, 79 and 80.
