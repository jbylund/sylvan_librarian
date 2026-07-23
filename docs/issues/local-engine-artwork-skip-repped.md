# Artwork gather: skip already-repped groups (default prefer)

**Status:** implemented, measured, byte-identical. Branch `engine-artwork-skip-repped`. Three changes:
the skip-repped reorder, a columnar `artwork_group_id`, and pre-sizing `group_best` (all below).

Cumulative vs `main` (97,206-printing corpus, artwork/usd, byte-identical output):

| query | main | branch | speedup |
|---|---:|---:|---:|
| `border:black -(name:storm or name:dragon)` | ~1543 µs | ~1245 µs | **1.24×** |
| `border:black` | ~1166 µs | ~845 µs | **1.38×** |
| `t:creature` | ~320 µs | ~244 µs | **1.31×** |
| `c:r` | ~156 µs | ~122 µs | **1.28×** |

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

## Also landed: columnar `artwork_group_id` (V2)

A pid-indexed `artwork_group_col: Vec<u16>` so the gather reads the group id from a compact contiguous
side array instead of the wide struct — repped-group printings (the majority) then never touch the
struct at all. **Flat on the target `border` query** (the residual, not the gid read, is the remaining
cost there), but a real win on `all_match=true` artwork scans, where the gid read *is* the only
per-printing work:

| query [artwork/usd] | skip-repped only | + columnar |
|---|---:|---:|
| `t:creature` | ~306 µs | ~281 µs (**1.14× vs main**) |
| `c:r` | ~149 µs | ~124 µs (**1.26× vs main**) |

**No drift hazard.** An earlier prototype panicked in four test fixtures and I mistakenly concluded the
column was unsafe on production reload. It isn't: `assign_artwork_groups` runs *exactly once* in
production (`reload_commit`, before archiving), so `artwork_group_id` — and the column derived from it
right there — is stored in the archive and never recomputed post-load. The drift was test-fixture-only:
four fixtures mutate `illustration_id` after `store_of` and re-derive grouping by hand. They now go
through one helper (`reassign_artwork_grouping`) that rebuilds *both* the per-card counts and the
column together, so no site can update one and forget the other. Costs one archive-format bump
(`ARCHIVE_FORMAT_VERSION`) and ~190 KB. Byte-identical output; all 128 tests pass.

## Also: pre-size `group_best`, drop the per-printing resize check

`group_best` (the per-group best-rep scratch) is indexed only by `artwork_group_id` — Card mode
collapses to index 0 — so its maximum index is a fixed property of the store: the largest
distinct-artwork count of any single card (**385** in the corpus; p50 is 1, p99 is 6). Stored as
`CardIndexes.max_artwork_groups` and used to pre-size the scratch once per query, so the three artwork
grouping loops (`push_card_matches` ×2, `walk_grouped_page`) no longer run a `len() <= gid`
bounds/resize check on every printing (the check only ever fired during warmup, then never again). A
`debug_assert` guards the invariant. Biggest effect where the loop iterates the most printings
(`t:creature` ~281→244 µs); flat where the residual dominates. Byte-identical.

## Remaining cost / possible next levers (not pursued)

The surfacing query's residual ~1256 µs still splits across the per-card name check (`card_pass`, ~430
µs), the ~40k first-per-group `border` residual reads, and the ~40k emit `price_usd` reads (usd sort
key). Further levers, if this tail ever matters: the `border_printing` bitplane as the per-printing
membership test (turn the remaining border checks into bit-tests, `#731` step 3), or a columnar
`price_usd` for the emit sort key.
