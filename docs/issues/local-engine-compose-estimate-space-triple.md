# Engine: `compose_printing_estimate` Should Carry Card/Printing/Artwork Space Natively

Not yet filed as a GitHub issue. Surfaced while closing out
[#1066](https://github.com/jbylund/sylvan_librarian/pull/1066) (card-invariant broadcast leaves in
`PrintingCompose`, itself step 2 of [#731](00731-engine-compose-universal-evaluator.md)) — several of
that PR's commits (7-9) are narrow, ad-hoc fixes for symptoms of the gap this doc describes. This is
the general fix; #1066 ships without it and documents its remaining gaps honestly instead of waiting
for it.

## The bug, traced to one query

`r<=uncommon tou>=2 tou<=2 devotion:w` was #1066's single worst regression (0.22x). Tracing a
different but structurally identical case, `usd>=0.22 tou>3 border:white`, all the way through:

- On `main`: `eval_domain: 267` — the exact `tou>3 ∩ border:white` intersection (`main`'s `narrow_rec`
  drops `usd` as a non-narrowing residual; the true 3-way joint is 147, but 267 is what it actually
  computes and it's enough to pick the right plan).
- On the branch: `eval_domain: 2756`. This number has **no relationship to `tou>3` or `usd>=0.22` at
  all**. `border:white`'s own leaf estimate is exact (`border_leaf_bits`+`popcount` against
  `indexes.border_printing`, giving `5131` — the exact count of white-bordered *printings*). Nothing
  tightens the `And` below that single value, so `result == candidate == 5131`, which is exactly the
  condition `acquire_plan_features` uses to decide "treat this as a bare leaf": it takes `domain_cards
  = est_cards` directly, skipping `domain_hint` entirely. `est_cards` then falls back to
  `calibrated_balls_into_bins(5131, 31724)` ≈ `2756` — a *statistical* "how many distinct cards does a
  random sample of 5131 printings touch" conversion, applied to border's own printing count, with zero
  input from the other two leaves.

Two different exact numbers exist for `border:white` (2,059 cards and 5,131 printings — both free, see
below) and the estimate uses neither. It re-derives a *third*, worse number by guessing card-space
from the printing-space one through an unrelated model.

## What's already available, for free, verified in `card_engine/src/lib.rs`

| leaves | card | printing | artwork | source |
|---|---|---|---|---|
| `border`/`layout`/`frame_data`/`subtypes`/`keywords`/`oracle_tags`/`art_tags`/`is_tags`/`legality`/`colors`/`color_identity`/`produced_mana` | exact | exact | exact | `ValueTotals` (`SpaceTotals{printings,cards,artworks}` per value, `HashMap` lookup, no scan) |
| `usd`/`eur`/`tix`/`cn`/`date` (one-sided ranges) | exact | exact | exact | `RangeCardCounts` (`below`/`at_or_above`/`at` prefix arrays, `O(log n)`) via `range_card_counts_for` |
| `cmc`/`power`/`toughness` | exact | — | — | `NumericIndex`/`#743 ArithTupleIndex`, `O(log n)`/`O(564 keys)` |
| `devotion` | exact (`eval_planes`) | — | — | card-space `BitPlanes` only, no per-value table (synthesized from mana cost, not a raw dimension) |

`exact_result_total` already reads every column of this table. `compose_printing_estimate` — the
function that actually drives `PrintingCompose`'s own pricing and, through `est.candidate`/`est.result`,
`GatheredScan`/`StreamedSelect`'s `domain_cards` too — reads almost none of it:

- The bare-range leaf arm (`lib.rs`, `FilterExpr::NumericCmp{..} | DateCmp{..} | YearCmp{..} if
  bare_range_bounds(...).is_some()`) computes `idx.range(lo, hi)` — printing count only — and never
  calls `range_card_counts_for`, even though the exact card (and artwork) count is a lookup on the same
  table sitting right next to it.
- The `TextExact{Border}` leaf arm calls `border_leaf_bits`+`popcount` — strictly more expensive than a
  `ValueTotals` lookup — and, like the range arm, only produces the printing number.

## Why this is architectural, not a missing case

`ComposeEstimate` (the struct these arms populate) has exactly one count field per role
(`result`/`candidate`), always printing-space — card-space leaves scale their exact count *up* via
`card_count * n_printings / n_cards` to fit that one slot. When nothing downstream needs the card
number back, this is harmless. When something does (every consumer in `acquire_plan_features` that
prices a per-CARD walk — `domain_cards`, `est_cards`, `scan_all` — regardless of the query's actual
`unique=`), the exact card count that existed before the scale-up has to be re-guessed from the
printing-space proxy via `calibrated_balls_into_bins`, a *second*, unrelated statistical model. Two
lossy conversions (`card → printing` then `printing → card`) stand between an exact number and the
place that needs it, and #1066's commits 7-9 are three different ad-hoc ways of routing around that —
not fixes to the underlying representation.

## Proposed shape

Give `ComposeEstimate` (and the `And`/`Or` fold over it) a real slot for each space instead of one
printing-space number with an implicit scale-up convention:

```rust
struct SpaceEstimate {
    printing: usize,           // always known — this is what compose actually builds
    card: Option<usize>,       // Some(exact) whenever a leaf/join has one for free; None otherwise
    artwork: Option<usize>,    // same
}
```

- Every leaf arm reports whichever spaces it actually has exactly, per the table above, instead of
  picking one and discarding the rest (`ValueTotals`/`RangeCardCounts` leaves start reporting all
  three immediately; `cmc`/`power`/`toughness`/`devotion` report `card` only, `artwork: None`).
- `And`'s fold takes the componentwise `min` per space it has an answer for on both sides, the same
  way `result`/`domain_hint` already fold today — no new combinatorics, just not throwing a space away
  before the fold.
- `acquire_plan_features` prefers `.card` directly whenever `Some`, the same way `exact_cards` already
  gets first refusal over `calibrated_balls_into_bins` for the AND-level 2-child `PairTotals` case —
  this makes that preference the *default* instead of a special case, and retires
  `calibrated_balls_into_bins` to genuinely unknown-card-space leaves only (`devotion` mixed with
  something that isn't in the table above, text residuals, etc.).

This should subsume #1066's commits 7 (whole-And plane-AND for anti-correlated leaves), 8 (the
existential generalization), and 9 (the arith-tuple ID-probe merge) as special cases of "fold the
`card` space instead of only `printing`" rather than three separate mechanisms — and, per the traced
example above, would give `usd>=0.22 tou>3 border:white` its exact numbers (2,059 / 5,131 / artwork)
directly from `ValueTotals`, no `eval_planes`, no ID-probing, no plane construction of any kind for
that leaf at all.

## Scope note

One coherent representational change — touches every leaf arm in `compose_printing_estimate` and the
`acquire_plan_features` consumers that read `est.result`/`est.candidate`/`domain_hint` — but it is one
idea (fold in the space that already has an exact answer), not several independent ones, so it stays
one doc despite the number of call sites.

## Sequencing

Land after #1066 merges, not before — #1066 is complete and independently measured on its own terms;
its remaining gaps (documented in its own PR body) are exactly the input to this doc, not a reason to
block it. Worth checking whether the other card-space-compose work in flight touches
`ComposeEstimate`/`compose_printing_estimate` directly before scheduling this — if it does, doing this
refactor first (or interleaved) avoids two stacks fighting over the same match arms; if it's disjoint,
order doesn't matter.

## Related

- [#1066](https://github.com/jbylund/sylvan_librarian/pull/1066) — the PR whose commits 7-9 are the
  symptom-level fixes this doc generalizes; its "Known remaining gaps" section is the direct source of
  the traced example above.
- [00731-engine-compose-universal-evaluator.md](00731-engine-compose-universal-evaluator.md) — step 2
  of this doc's build order is what #1066 ships; its leaf-source table is the same inventory this doc
  draws the card/printing/artwork columns from.
- [01020-engine-estimate-cardinality-unsound-bounds.md](01020-engine-estimate-cardinality-unsound-bounds.md)
  — a different estimator (`estimator::estimate_cardinality`, not `compose_printing_estimate`) with its
  own soundness bug; related in theme (cardinality-estimate correctness) but not in code path.
- [01025-engine-compose-walk-cost-miscalibrated.md](01025-engine-compose-walk-cost-miscalibrated.md) —
  also about `PrintingCompose`'s cost accuracy, but the *build-cost model*, not the cardinality
  estimate; orthogonal fix, same neighborhood.
