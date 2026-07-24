# Engine: Printing-Space Compose as the Universal Exact Evaluator

Status: **most leaf sources landed; two gaps remain.** Filed as
[#731](https://github.com/jbylund/sylvan_librarian/issues/731). The generalization of
[#724](done/00724-engine-printing-existential-planes.md)'s printing-space compose from "whole filter
composable" into the query engine's general evaluation model. #724 (the substrate) has merged.

Shipped since filing — the `is_printing_composable` leaf table (`card_engine/src/lib.rs`) now spans:
planes (border/rarity/legality, #724); **range leaves** usd/cn/date (#733, closed #694) plus their
**negations** (#741); **exact-postings** set:/watermark: and `-set:` (#748, index #739); and
**collection-containment** type:/kw:/otag:/art:/is: and their `NOT` arms (#753).

Remaining (steps 2–3 below):

- **Card-invariant broadcast for `color`/`cmc`** — the broadcast *mechanism* landed (#753 uses
  `broadcast_card_ids_to_printings` for the collection fields), but `color` has no compose index and
  `cmc` (a card-space numeric) still declines to the general path. Wiring these two is what's left of
  step 2.
- **Per-survivor text residual** (step 3) — not started: the compose path has no residual re-check, so
  text leaves (`name`/`oracle`/`flavor`) and loose `Eq`/`Gt` collection leaves still fall to the
  general path.

## The model

Every query leaf that has an index can be turned into — or tested against — an **exact printing-space
bitmap**. Compose those bitmaps with `AND`/`OR`/`NOT` in printing space, then project **once** to the
query's unique space (`printing` = the bits themselves; `card`/`artwork` = a single `∃`-projection).
That one pipeline answers arbitrary queries exactly, at any distinct-on. #724 built it for
border/rarity/legality under `AND`/`OR`; this is the generalization to *all* leaf kinds.

## Leaf sources — each yields an exact printing bitmap

| kind | leaves | how | status |
|---|---|---|---|
| plane read | border, rarity, legality | plane slice (legality: broadcast card `∃`-plane + divergent repair) | built (#724) |
| range materialize | `usd`, `cn`, `date` | scatter the range index's in-range slice into a bitmap; `-usd<c` etc. flips the bounds | **shipped** (#733; negations #741) |
| postings scatter | `set`, `watermark` | postings scattered into a bitmap (or cleared from all-ones for `-set:` — the nullable `-watermark:` stays out, needs a known-mask) | **shipped** (#748, index #739) |
| collection containment | `type`/`kw`/`otag` (card-space, broadcast up), `art`/`is` (printing-space postings) | card-id postings broadcast down via `broadcast_card_ids_to_printings`, or scatter directly; `Ge`/containment only, plus `NOT` arms | **shipped** (#753) |
| broadcast down | card-invariant `color`/`cmc` | broadcast the card plane to printing space; **no repair** (never diverges) | **todo** — `color` has no compose index; `cmc` declines to general path |
| per-survivor residual | text (`name`/`oracle`/`flavor`), or any leaf | verify on the composed survivors, not as a bitmap | **todo** — compose path has no residual re-check yet |

## Materialize vs. verify — one cost axis, both exact

An indexed printing-varying leaf can **either** be materialized as a bitmap and `AND`'d, **or** verified
per surviving printing. Both are exact; the planner picks by which side is smaller:

- composed set large, range broad → materialize the range bitmap and `AND` (O(range) build + O(words)).
- composed set already small → iterate its set bits and read each printing's field directly
  (O(survivors), no index, no full bitmap).

This is the same density crossover the border plane-vs-postings decision embodies, one level up — and it
dissolves the "compose leaf vs residual" distinction into a cost decision rather than a correctness one.

## The load-bearing rule (why printing space at all)

**Two or more printing-varying leaves must be `AND`'d in printing space _before_ the `∃`-projection.** A
card-space existence-`AND` of separately-`∃`-projected leaves false-positives on the shared witness — a
card with a black printing and a *separate* rare printing satisfies `∃black ∧ ∃rare` but has no single
black-and-rare printing. Composing in printing space and projecting once is exactly what avoids that
(see [#724](done/00724-engine-printing-existential-planes.md)). Card-invariant leaves and card-level
residuals are witness-independent, so they apply *after* the projection.

## Correctness caveats (both the trivalent-NULL issue)

- **Ranges: intersect the in-range slice, never complement the out-of-range one.** `usd<20` is the
  printings the index places in `[min, 20)`; a no-price printing is in neither slice and must be
  excluded — intersecting the in-range slice does that, complementing `≥20` would wrongly keep it.
- **`NOT` over a nullable field needs a "known" mask.** A null-border printing satisfies neither
  `border:black` nor `-border:black`; the plane `complement` isn't the trivalent negation. `NOT` is
  excluded from compose until the known-mask is added.

## Why it matters

#724 today requires the *whole* filter composable, so only ~0.5% of realistic traffic qualifies
(measured, `survey_queries.py` — most queries mix border/rarity/legality with color/type/name). Making
ranges and card-invariant leaves bitmap sources, plus deferring text to per-survivor residual, expands
the addressable slice to most real queries — `r:rare border:black usd<20`, `f:modern c=g t:creature` —
each an exact printing-space composition projected once. This is the direct answer to "why the targeted
wins didn't move the broad survey": the addressable slice was structurally tiny, not the wins small.

## Scope / sequencing

Build order (each its own PR, gated by the cost model that already prices synthesis):

1. **Range leaves** (`usd`/`cn`/`date`) as compose sources — reuses `build_card_range_bits`. **Shipped**
   ([#733](https://github.com/jbylund/sylvan_librarian/pull/733), also closed #694; negations #741).
2. **Card-invariant broadcast leaves** (`color`/`type`/`cmc`) — reuses `broadcast_card_bits_to_printings`.
   **Partly shipped:** the broadcast-down mechanism landed for collection fields `type:`/`kw:`/`otag:`
   ([#753](https://github.com/jbylund/sylvan_librarian/pull/753), via `broadcast_card_ids_to_printings`),
   and exact-postings `set:`/`watermark:` landed alongside ([#748](https://github.com/jbylund/sylvan_librarian/pull/748)).
   **Still todo:** `color` (no compose index) and `cmc` (declines as a card-space numeric).
3. **Per-survivor residual** for the non-composable remainder (text) — feed compose's exact narrowing
   into the existing residual-verify path (printing-varying residual before the projection, card-invariant
   after). **Not started** — this is also what would let loose `Eq`/`Gt` collection leaves (which #753
   kept on the general path for lack of a re-check) compose.

## Related

- [#724](done/00724-engine-printing-existential-planes.md) — printing-space compose, the substrate.
- [#730](00730-engine-popcount-skip-walk.md) — deep-pagination popcount-skip walk (orthogonal).
