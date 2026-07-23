# Engine: Popcount-Skip Walk for Deep Pagination, Generalized to All Distinct-Ons

Status: todo, filed as [#730](https://github.com/jbylund/sylvan_librarian/issues/730). Deferred
optimization split out of the #724 printing-space compose work
([00724](done/00724-engine-printing-existential-planes.md)).

## Two ways to page a match bitmap

Once a query's matches are a bitmap in the result space, the page can be produced two ways:

- **Forward grouped walk** (`walk_printing_page` / `walk_artwork_page`, and card after the #724
  unification): walk the sort permutation from the front, collapse each card's matching printings to
  the distinct-on's granularity (printing = every set printing, artwork = one representative per
  `artwork_group_id`, card = one per card), and emit until `offset + limit` rows have passed. Cost is
  O(rows visited) = O(offset + limit) worth of the sort order.
- **Popcount-skip order** (`run_query_streamed_popcount`, the #634/#725 card-space walk): word-scan the
  match bitmap + per-card counts to land directly on the O-th match, then emit `limit`. Cost is
  O(bitmap words) to skip + O(limit) to emit — **independent of how deep the offset is**.

At `offset = 0` they're equivalent (the forward walk fills the page immediately). The skip only wins at
**deep pagination** — page 50 of a broad result — where the forward walk pays for the rows before the
page and the skip does not.

## What the #724 unification changed

#724 unified printing / card / artwork compose onto the *one* forward grouped walk (one plan, one cost
formula parameterized by output space). That was the right clarity/correctness call, but it moved **card
compose off the popcount-skip walk** it previously inherited from `CardRangePopcount`. So card compose
now pays the forward-walk offset cost for deep pages. Acceptable because deep pagination is rare in
card-search UIs (page 0 dominates), but it is a regression at the tail worth recording.

## The idea

1. **Restore popcount-skip for deep offsets** — route the compose plan to a skip-order walk when
   `offset` is large enough that visiting the preceding rows dominates (a cost-model decision, or a
   fixed threshold).
2. **Generalize popcount-skip to all three distinct-ons.** Today only card space has it. The bitmaps a
   skip walk needs already exist after #724:
   - **printing** — skip over the composed printing bitmap directly.
   - **card** — the card-existence projection (already built).
   - **artwork** — the artwork-existence projection + per-card artwork counts (`artwork_groups`, already
     stored) to skip whole cards' artwork spans.

   So this is a walk-side addition, not a new index — the projection substrate is in place.

## Why deferred

Deep pagination is rare, and unifying the three modes onto one walk was the larger win. This is a
targeted optimization to build once/if deep-offset compose queries prove hot — measure the offset
distribution of real traffic before spending the complexity.

## Related

- [00724](done/00724-engine-printing-existential-planes.md) — the printing-space compose plan whose
  unification this splits off from.
- `run_query_streamed_popcount` (`card_engine/src/lib.rs`) — the existing card-space popcount-skip walk
  to generalize.
