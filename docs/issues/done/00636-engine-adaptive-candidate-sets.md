# Engine: adaptive candidate sets (ids ⇄ bitmaps, Or/Not composition)

Status: implemented in PR #637 (2026-07-08), awaiting review/merge. Filed
2026-07-08 from the 400-query survey (clusters 3+4). GitHub: #636.

## Outcome

400-query survey vs main (efd84a5): p50 0.101→0.090, p75 0.243→0.191,
p90 0.862→0.733, p99 1.454→1.380 ms; geomean 1.14×; 81 queries >1.15×
faster vs 7 slower (worst +33 µs); totals identical on all 400. Tail wins
up to 18× (Or-composition, ranked-And shapes).

## What shipped (beyond the filed design)

- Plane feed-in arm in narrow_rec (exact CardBits for color/type subtrees).
- range_narrowed: sparse vec / direct scatter / complement-of-sparse; the
  non-null-mask variant was unnecessary — complements are just loose.
- **Cost model carried half the work** (first round regressed selective
  Ands 10×): broad_ok gating (broad bits only where consumed), ranked
  early-exiting And (AND_SKIP_THRESHOLD 2048), near-total drops before any
  projection, no broad printing bitmap crosses to card space (~3× density
  amplification), cards_of_printings scatter-walk past 1024 ids.
- Two soundness bugs caught pre-commit: partial-And false tightness
  (every_child_included seal) and price-range false tightness (price bounds
  are widened supersets — exact flag threaded, price off the static check).

## What it deliberately doesn't fix

Broad-true-match Ors (`t:goblin or usd<5`, 29k matches — floor is
verification+emission), unindexable Or children (devotion, 2-char names),
double negation. Remaining tail per the survey: 2-char names (~10 of the
slowest 30), tix/eur unindexed (~6), genuinely broad (~4).

## Related

- [00630-engine-card-bitplanes.md](00630-engine-card-bitplanes.md) — #630; planes are
  the precomputed corner of this algebra
- [00634-engine-permuted-bitmap-order-phase.md](00634-engine-permuted-bitmap-order-phase.md)
  — #634; consumes the exactness/tightness idea downstream
- [00624-engine-bind-memoized-text-predicates.md](00624-engine-bind-memoized-text-predicates.md)
  — #635; covers the unindexable-children gap
