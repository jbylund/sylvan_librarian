# Engine: legality postings with a selectivity threshold

Split out of [done/00605-engine-unindexed-predicates.md](done/00605-engine-unindexed-predicates.md)
(approach 3 there — the one approach PR #605 didn't ship). Status: superseded.

**Superseded**: the `legal` dimension shipped as exact planes instead
([00667-engine-legality-divergent-carveout.md](00667-engine-legality-divergent-carveout.md), #667/#676), and
`banned`/`restricted` (this doc's remaining scope) followed the same plane design rather than
postings — see [00678-engine-legality-banned-restricted-planes.md](00678-engine-legality-banned-restricted-planes.md)
(#678) for why postings lost the tradeoff once actually measured. Kept for history; the design
below did not ship as written. The rarity postings idea below (the "second application") also
shipped as planes for the 4 common values — see
[00670-engine-rarity-planes.md](00670-engine-rarity-planes.md).

## Problem

Legality filters (`f:modern`, `banned:legacy`) have no index support: the card
pass scans all 31.5k cards checking the 2-bit (format, status) word. After
PR #605 indexed artist/set/date/price, `f:modern r:rare` (~0.75 ms) is the most
expensive common pattern, and legality accounted for 8 of the slowest 60 configs
in the post-#605 broad survey (452 sampled configs).

## Design

Post per (format, status) card-id lists **only where selective** — the
thresholding rule from the PR #600 discussion: drop postings covering more than
~25% of the store. Concretely:

- `banned`/`restricted` everywhere (tiny lists).
- `legal` for rotating formats (standard-legal ≈ 3k cards).
- Vintage/commander-`legal` stays a scan (would cover most of the store).

Advisory narrowing means the threshold is purely a size/speed dial, not a
correctness concern — eval verifies every candidate against the exact word.

**Divergent-legality cards** (the 556 with genuinely per-printing legality, see
[done/00603-engine-card-printing-split.md](done/00603-engine-card-printing-split.md)) must
appear in any posting a minority printing qualifies for, or be excluded from
postings entirely and left to the scan — decide during implementation.

## Rarity postings (same mechanism, natural companion)

The post-#605 survey also flagged rarity scans (`r:common` 0.80 ms, `r:mythic`
0.76 ms). Rarity is printing-space with six values; the same thresholded-posting
treatment applies — mythic/rare are selective, common/uncommon get dropped by
the threshold. Ship together if convenient; it's the second application of the
same rule.

## Sizing

Under a 25% threshold: dominated by rotating-format `legal` lists and small ban
lists; likely < 1 MB total on the 72.7 MB archive. Rarity adds two selective
posting lists (mythic/rare ≈ 20k printing ids ≈ 80 kB).

## Tasks

- [ ] Legality (format, status) postings behind the ~25% selectivity threshold,
      wired into `narrow_candidates` in card space
- [ ] Decide divergent-556 handling (include in qualifying postings vs exclude
      and leave to scan)
- [ ] Rarity postings (mythic/rare) in printing space
- [ ] Re-run the #605 targeted + broad-survey benchmarks; acceptance cases:
      `f:modern r:rare` and `r:mythic`

## Related

- [done/00605-engine-unindexed-predicates.md](done/00605-engine-unindexed-predicates.md) —
  parent doc; artist/set/date/price indexing shipped there (PR #605)
- [done/00603-engine-card-printing-split.md](done/00603-engine-card-printing-split.md) —
  candidate-space rules and the divergent-legality design
- Selective-index thresholding (PR #600 discussion) — this is its first concrete
  application; frame_data indexing composes with it
