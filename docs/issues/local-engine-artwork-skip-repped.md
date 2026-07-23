# Artwork gather: skip already-repped groups (default prefer)

**Status:** implemented, measured, byte-identical. Branch `engine-artwork-skip-repped`.

## The lever

For `unique=artwork`, the gather picks one best-`prefer_score` representative per artwork group.
The [APrinting-layout investigation](./local-engine-aprinting-layout.md) established (after a
misattributed profile) that the real gather cost on `border:black -(names)` / artwork / usd is
**per-printing residual verification of a printing-varying predicate** (`border`) — the loop calls
`residual_matches(&printings[pid], …)` on every printing to find a black-bordered rep, reading the
wide struct each time. Struct footprint was *not* the lever (both eviction and columnar
`artwork_group_id` measured flat).

The fix attacks the residual directly. Printings are stored **prefer-desc within a card**, so for the
**default prefer** the *first* residual-qualifying printing of a group is already its rep — every later
printing of that group is dead weight. So: read the group id first, and `continue` past any printing
whose group is already repped, **before** touching the residual. Repped groups (the majority — ~2.4
printings/group) never pay the residual verification again, and the rep needs no `prefer_score`
comparison (first qualifying wins, so the default branch also stops reading `prefer_score` entirely).

Custom prefer keeps the full max-score scan (iteration order ≠ prefer order, so every printing must be
considered). The change is confined to `push_card_matches`'s `Mode::Artwork` arm.

## Measured (97,206-printing corpus, min of a timed window, byte-identical output verified by
returned-row-id fingerprint)

| query [artwork/usd] | main | branch | speedup |
|---|---:|---:|---:|
| `border:black -(name:storm or name:dragon)` | ~1543 µs | ~1256 µs | **1.23×** |
| `border:black` | ~1166 µs | ~861 µs | **1.35×** |
| `t:creature` | ~320 µs | ~306 µs | 1.05× |

Zero archive-format change. All engine tests pass (128).

## Rejected: columnar `artwork_group_id` (V2)

Prototyped a pid-indexed `artwork_group_col: Vec<u16>` so repped-group printings read the group id from
a compact side array instead of the struct. **Flat on the target query** (`border` residual, not the
gid read, is the remaining cost) — it only helped `all_match=true` artwork scans (`t:creature`
320→280 µs, `c:r` 156→125 µs), where the gid read *is* the only per-printing work. And it introduced a
sync hazard: `artwork_group_id` is a placeholder in the store and **recomputed post-load** (via
`assign_artwork_groups` at 4+ sites), so a duplicated column goes stale on reload unless every recompute
site also rebuilds it. Marginal, localized gain + an archive bump + reload-sync fragility → dropped.

## Remaining cost / possible next levers (not pursued)

The surfacing query's residual ~1256 µs still splits across the per-card name check (`card_pass`, ~430
µs), the ~40k first-per-group `border` residual reads, and the ~40k emit `price_usd` reads (usd sort
key). Further levers, if this tail ever matters: the `border_printing` bitplane as the per-printing
membership test (turn the remaining border checks into bit-tests, `#731` step 3), or a columnar
`price_usd` for the emit sort key.
