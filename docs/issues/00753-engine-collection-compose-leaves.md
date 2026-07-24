# #753 Engine: Card-Space Collection Fields (type:/kw:/otag:) as Compose Leaves

Stacks on #746 (set:/watermark: compose leaves) and #744 (orderby-range-index walk). Merge after
them, or rebase once they land.

## Measured problem

`type:goblin or format:legacy` (`unique=printing`, `orderby=usd`) cost **~575 µs** on the composite
baseline (main + #744/#746/#748-51), returning 96,445 rows. Two independent declines forced it to a
full `GatheredScan`:

- PrintingCompose declined — `CollectionCmp{field: Subtypes}` was not in `is_printing_composable`'s
  table (`_ => false`).
- Generic Or-narrowing declined — the near-total `format:legacy` child trips `narrow_rec`'s Or
  near-total guard (`len > domain − domain/4`).

So it gathered every card, computed the usd sort key per card, and quickselected. This is the exact
pathology bare `format:commander` had before #744.

## Approach

Make a card-space collection containment leaf (`type:`/`kw:`/`otag:` = `CollectionCmp{op: Ge}` over
`Subtypes`/`Keywords`/`OracleTags`) a PrintingCompose leaf by **projecting its card-id postings up to
printing space**: set every printing of each matching card (`broadcast_card_ids_to_printings`, the
card-id-list analogue of the existing `broadcast_card_bits_to_printings`). The projection is **exact**
— a subtype/keyword/oracle-tag is a pure card property, so every printing of a goblin is a goblin; no
per-printing divergence/repair like legality. Wired into all three compose functions via a shared
`collection_compose_index` field table.

The already-printing-space siblings `art:`/`is:` (`ArtTags`/`IsTags`) scatter their printing-id
postings directly (identical to `set:`/`watermark:`), no projection.

### Correctness scoping

- **`complete` fields only.** `Subtypes`/`Keywords`/`OracleTags`/`ArtTags`/`IsTags` post every
  occurrence of every value → absence proves absence, membership is exact. `FrameData` is NOT
  complete (dense values dropped at build, #628) — excluded (`collection_compose_index → None`),
  stays on the general path.
- **`Ge`/containment only.** The postings are tight for `Ge`, but only a loose superset for
  `Eq`/`Gt` (they prove `contains(value)`, not the collection-length condition). The compose path has
  **no residual re-check**, so a loose leaf would return wrong rows — `Eq`/`Gt` stay on the general
  path (where `narrow_rec`'s driver re-verifies with `matches()`). Gated, not marked-loose.
- **Negation is exact.** `-type:goblin` = the complement of the (exact, card-projected) positive set.
  A collection is never NULL — a card/printing lacking the value has a definite
  `contains == false` — so there is no trivalent-NULL trap (the reason `-watermark:` stays off the
  compose path). Added for all five complete fields.
- **No shared-witness problem.** The leaf is card-invariant (every printing of a card agrees), so
  ANDing it with a per-printing predicate (`type:goblin border:black`) is exact — unlike a predicate
  exact only in isolation.

## The paging question (A vs B) — measured, cost model audited

For a near-total composed predicate with `orderby=usd`, `mode=Printing`, two paging strategies:

- **A = `walk_range_orderby_page` (#744):** walk the pre-sorted `price_usd` index, test `pbits` per
  entry, stop at `offset+limit`. `O((offset+limit)/selectivity)`, random access into the store.
- **B = `gather_composed_page` (#740):** sweep `pbits` linearly (card-contiguous), accumulate every
  match, one final quickselect. `O(n_matches)` sweep, sequential access.

The user hypothesis was that B's linear access beats A's random walk for a near-total predicate, and
that the cost model may pick A unfairly. **Measured directly** (`bench_compose_paging.rs`, kernel
micro-bench on real.store, offset-0 page and a ~70%-deep page, both strategies asserted to return the
identical page first):

| predicate | sel% | offset | A µs | B µs | winner |
|---|---|---|---|---|---|
| type:Octopus | 0.10 | 0 | 76 | 11 | B (A declines — null-price tail) |
| type:Goblin | 1.55 | 0 | 12 | 27 | A |
| type:Goblin | 1.55 | deep | 76 | 27 | B |
| type:Human | 10.9 | 0 | 3.5 | 63 | A (18×) |
| -type:Human | 89.1 | 0 | 3.4 | 593 | **A (174×)** |
| -type:Goblin | 98.5 | 0 | 3.7 | 637 | **A (174×)** |
| -type:Octopus | 99.9 | 0 | 3.9 | 647 | **A (167×)** |
| -type:Goblin | 98.5 | deep | 124 | 1557 | **A (13×)** |

**Finding: the hypothesis is refuted.** For a near-total predicate A dominates B by 167–174× at page
1 and still 10–14× at a deep offset — because A terminates after ~`offset+limit` steps (almost every
walked entry passes `pbits`), so its "random access" touches only ~`limit` rows; B must sweep and
quickselect all ~90k matches. B wins only at low selectivity or deep offset — precisely the regime
where A's `(offset+limit)/selectivity` cost is high enough that the router **doesn't pick
PrintingCompose at all** (it routes to `GatheredScan`, which is B-shaped). The very-sparse case where
B wins is already B: A declines into its null-price tail and the fastpath falls back to
`gather_composed_page`.

**Cost-model verdict: fair, no change.** The router always picks `ComposePaging::OrderbyWalk` (A) for
this regime, which is correct — A wins wherever PrintingCompose wins. Making B a cost-selectable
PrintingCompose sub-strategy would be dead code, redundant with the existing `GatheredScan` plan.
Confirmed end-to-end: sparse `type:goblin` (1.5%) stays flat at ~58 µs (narrowing path, not the walk),
while near-total `type:goblin or format:legacy` uses the walk at ~56 µs.

## Card-mode gather-regime regression + fix

Making collection leaves composable exposed a pre-existing cost-model gap: a **sparse** composable
predicate in the **permutation-free gather regime** (card/artwork mode, `orderby=usd`/`rarity` — no
card permutation, no printing orderby walk) was newly routed to PrintingCompose, which there has no
paging edge over `GatheredScan` — it just composes the full printing bitmap and projects it back down
(two O(n_cards) passes) on top of the same gather. Measured regressions (survey): `type:angel`
card/usd 53→77 µs, `type:goblin` card/usd 55→82 µs, `type:zombie type:elf` 12→29 µs.

Fixed with a small-total decline in `printing_compose_fastpath`'s gather-regime block, mirroring the
`Perm` branch's existing `total <= STREAM_MIN_MATCHES` decline: if the SOUND card-space cardinality
upper bound (`estimator::estimate_cardinality(...).hi` — exact for a collection leaf) is
`<= STREAM_MIN_MATCHES`, decline to the candidate/narrowing path. The balls-into-bins breadth `est`
already in that block can't make this call — it overestimates a clustered predicate's distinct-card
count (goblin: 501 cards but ~1471 `est`). Post-fix all three regress-ers are back to baseline and
the printing-mode wins are unchanged. Only affects card/artwork mode (printing mode takes the orderby
walk, never this block), so the motivating query is untouched.

## Out of scope

A bare **positive near-total printing-space** leaf (`is:permanent`, 78% of printings) does not route
to the walk — its scatter estimate = the full match count, which correctly costs high, so it stays on
`GatheredScan` (~730 µs, unchanged from composite; not a regression). Legality/negations avoid this
by building from the sparse side. Bare near-total *positive* leaves are not a useful query shape
(78% of the DB); the sparse-side trick for them is a possible follow-up.

## Acceptance

`scripts/bench_collection_compose.py`; motivating group must improve, controls flat, `total` parity
identical across builds. Differential: `collection_compose_leaves` (direct compose vs residual truth)
plus `fuzz_row_identity_matches_reference` / `force_plan_differential_agreement` (now route collection
leaves through the compose path against the GatheredScan reference).
