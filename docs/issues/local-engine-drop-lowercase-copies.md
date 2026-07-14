# Engine card store: drop the `_lower` copies (contingent on a benchmark)

Item 4 of the store-size reduction series. The full cost analysis and the measured record of
items 1–3 (u128 UUIDs, string interning, trigram-CSR dedup: archive 156.6 → 84.5 MB, Rust
reload peak 549 → 291 MB) live in
[done/00504-engine-store-size-reduction.md](done/00504-engine-store-size-reduction.md). Item 5 landed
([done/00598-engine-collection-vocab-interning.md](done/00598-engine-collection-vocab-interning.md)).

## Status update (2026-07-03): one of three fields done, gate risk shrank

- `card_artist_lower`: **done via PR #605's artist vocab**, which removed *both*
  artist string ids from printings (~780 KB of ids + the duplicated strings) with
  no fold-at-verify tradeoff — the 2.2k-entry vocab is lowercase-only and the
  fold happens once per query at bind.
- `flavor_text_lower` (~2.3 MB distinct): expected to fall out of
  [00620-engine-flavor-text-narrowing.md](00620-engine-flavor-text-narrowing.md) the same
  way — its bind-time distinct-text scan can fold case itself.
- `oracle_text_lower` (~4.7 MB distinct): the only remaining field that needs
  this doc's gate benchmark. The card/printing split (PR #604) improved its
  numbers: the id is per-card now, and a short-pattern fold-and-scan covers
  31.5k cards, not 96k printings — the gate risk is ~3× smaller than when
  written.

## Problem

`oracle_text_lower` is stored alongside its original-case twin. After interning it dedups to
~4.7 MB of distinct payload plus a per-card id, while the removal cost is unchanged:
case-insensitive matching must fold case during verification — cheap on trigram-pruned
candidate sets, but patterns shorter than 3 chars get no pruning and would fold-and-scan all
31.5k cards per query.

**Gate:** benchmark short-pattern (< 3 char) oracle-text queries with fold-at-verify before
deciding. ~4.7 MB is the prize; a regression on a common query class is the risk.

## Implementation tasks

- [ ] Benchmark short-pattern case-folding, then remove the `_lower` fields if it holds up
- [ ] Re-run the full memory measurement (protocol in the
      [done record](done/00504-engine-store-size-reduction.md#measured-progress)) and extend its table

## Related

- [done/00598-engine-collection-vocab-interning.md](done/00598-engine-collection-vocab-interning.md) — item 5
  of the same series (done)
- [00620-engine-flavor-text-narrowing.md](00620-engine-flavor-text-narrowing.md) — would retire
  flavor_text_lower as a side effect
- [local-engine-reload-publish-transient.md](local-engine-reload-publish-transient.md) — the other open
  engine-memory ticket; staging-structure size (this item) sets its live-heap floor
