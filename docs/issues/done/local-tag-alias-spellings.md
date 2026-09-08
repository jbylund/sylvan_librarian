# Tag alias spellings: `art:flames` finds nothing, `art:fire` finds 3564

## Problem

Scryfall's tagger stores alternate spellings for a tag in an `aliases` field and resolves them to
the tag before matching. `art:flames` and `art:fire` both return 3564 artworks there. Here,
`art:fire` worked and `art:flames` returned nothing, because the import read only `slug` and
dropped `aliases` on the floor.

Probing Scryfall live (`/cards/search?unique=art`) pins down the exact semantics:

```
art:fire 3564   art:flames 3564   art:flame 3564
art:loose-lips 6807   art:"open mouth" 6807   art:open-mouth 6807   art:"mouth open" 6807
art:right-facing 2633   art:"looks right" 2633   art:looks-right 2633
art:"right facing" 2633   art:"three figures" 1411
otag:removal-creature 7806   otag:"creature removal" 7806   otag:creature-removal 7806
```

Two things fall out of that.

1. **An alias behaves exactly like the slug it stands for, descendants included.** `fire` has
   18 child tags and only 2,225 direct taggings, but `art:flames` returns the same 3,564 as
   `art:fire` — so the alias resolves to the tag *before* the hierarchy expands, not after.
2. **A hyphenated slug is also accepted spelled with spaces.** `art:"right facing"` matches
   `right-facing`, and no alias is involved. That is a second, independent gap: every multi-word
   tag was reachable only in its hyphenated form.

## What the dumps actually contain

Audited against both dumps (2026-08-09):

| | art_tags | oracle_tags |
|---|---|---|
| tags | 11,517 | 4,522 |
| tags with aliases | 1,024 | 719 |
| aliases | 1,332 | 819 |
| alias equal to some tag's slug | 0 | 0 |
| alias claimed by two tags | 0 | 0 |
| same two checks after slugifying the alias | 0 / 0 | 0 / 0 |
| aliases not already slug-shaped | 690 (52%) | 229 (28%) |

Aliases occupy a namespace disjoint from the slugs, with no duplicates — so resolution is
unambiguous. Every slug in both dumps matches `[a-z0-9]+(-[a-z0-9]+)*`, which is what makes
normalizing the search term safe: it can only turn a miss into a hit.

## Approach

Aliases are resolved at **import** time, by writing them as additional keys in `card_art_tags` /
`card_oracle_tags` alongside the slug. Search-term normalization is the **query**-side half, in
the one helper both backends share.

This follows the decision the ancestor propagation already made — resolve tag semantics once at
import, keep query time a dumb exact key match — and it is the reason the fix needs no engine
change: the SQL path (`card_art_tags @> {...}`) and the Rust engine (interned `coll_vocab` ids)
both read the same column, and both take their search term from
[card_query_nodes.py](../../../api/parsing/card_query_nodes.py).

Aliases have to be stamped onto the ancestors' taggings too, not just the tag's own, since an
alias must reach descendants (point 1 above).

### Cost

Measured over the dumps, counting keys written per tagging:

- **art:** +536,636 keys on a 1,024,627-key baseline (**+52.4%**), touching 51% of 472,398 taggings
- **oracle:** +143,913 on 471,738 (**+30.5%**), touching 39.5% of 229,289 taggings

Relative growth looks steep, absolute is roughly 10MB of JSONB plus a comparable GIN increment.
Most of it comes from aliases on popular ancestors (`dominaria-origin`, with its 257 descendants)
reaching every descendant tagging. Engine `coll_vocab` grows by ~2,150 strings, from ~17k, against
a `u16` ceiling of 65,536.

**The engine figure above was originally given as ~1MB. That was wrong by 6x — it is 6.25MB.**
Corrected 2026-08-10, measured rather than derived, by building the rkyv archive twice from the
same dumps with alias stamping on and off:

| | bytes |
|---|---|
| archive, aliases stamped | 81,068,608 |
| archive, aliases not stamped | 74,815,728 |
| **alias keys** | **6,252,880** |

The original estimate counted only the forward field — `card_oracle_tags: Vec<u16>` on `Card`,
`card_art_tags: Vec<u16>` on `Printing` — at 2 bytes per entry. But every collection entry is
stored *twice*: `CardIndexes` carries `oracle_tags: TagIndex` and `art_tags: TagIndex`, and
`type TagIndex = HashMap<String, Vec<u32>>` is an inverted index whose posting is a 4-byte id. So
an entry costs 2 bytes forward plus 4 bytes inverted, and the measured marginal cost is 6.11 bytes
over 1,024,204 alias entries — 6.40 B/entry in card space (117,499 entries), 6.07 in printing space
(906,705). Isolating the two dumps in separate builds is additive to within 8 bytes, so this is a
uniform per-entry cost and not a struct-padding artifact.

Note the denominator differs from the per-tagging counts above: those count keys written per
*tagging record* in the dump, while these count entries per stored *row*. Printings sharing an
illustration each carry their own copy, which is why the archive-side art figure (906,705) exceeds
the per-tagging one (536,636) by roughly the printings-per-illustration ratio. The same
multiplication presumably applies to the JSONB estimate, which was not re-measured.

### Rejected: a query-time alias table

The alternative was persisting `alias -> slug` (`magic.art_tag_aliases`, `magic.oracle_tag_aliases`)
and resolving on the way in: a `COALESCE` scalar subquery on the SQL path, and the map pushed into
the engine at reload so `bind` could fall back to it when a value misses `coll_vocab`. That keeps
stored data canonical — no alias keys in a `fields=card_art_tags` projection — and costs no space.

It was rejected as the wrong trade for ~10MB: it touches schema, import, SQL generation and Rust
instead of one import loop, and it introduces a second copy of the tag vocabulary whose refresh has
to be kept in step with the engine's. Worth revisiting if the JSONB growth ever bites, or if
canonical output starts to matter.

**That condition has now fired once, downstream, and the rejection still stands here.** The
Cloudflare port of this project (`daveycodez/sylvan-librarian-cloudflare`) distributes the rkyv
archive as Workers KV values under a 25MiB cap and loads them with one sequential read per chunk.
Its archive went 73.2MB to 81.1MB over the same release, of which 6.25MB is these alias keys and
0.78MB is an unrelated pair of `Printing` rank fields. That crossed a chunk boundary: chunks are
cut at 25,000,000 bytes, so the store went from 3 to 4, adding a fourth serialized read to every
cold load. Removing the alias keys alone lands it at 74,815,728 — back under the boundary, which
is what makes this section's 6.25MB the deciding figure rather than the total.

Median cold store load over that release went 337ms (n=9) to 691ms (n=10), sampled from production
logs. Read that as directional: the newer store also has a fresh key, so its chunks had not yet
warmed the per-colo caches the reads hit, and that confound decays. The extra serialized read does
not.

That is a real cost, but it is *that deployment's* cost, and the fix belongs there rather than
here. Postgres does not care about 10MB of JSONB, and this repo's parser has no seam to resolve an
alias through: `db_info.py` is static schema metadata with no DB access or cache, and the
`get_*_tags_comparison_object` helpers are pure `str -> dict`. Threading a map of 2,150 aliases —
import-derived and versioned with the dumps — into them means building that seam, which is
precisely the second tag vocabulary this section rejected. The port can resolve at parse time
against a generated map without touching any of that, because its builder and parser are both
port-local code.

So: the number was wrong and is corrected, the trade it fed into is unchanged for this repo.

## Behavior changes

- Every alias spelling now resolves: `art:flames`, `art:open-mouth`, `art:"open mouth"`.
- Multi-word tags are reachable spelled with spaces: `art:"right facing"`, `otag:"creature removal"`.
- Tag searches are now case-insensitive in the same normalization step (`art:Flames`).
- A `fields=card_art_tags` projection now lists alias keys next to the slug. Art tags are not a
  Scryfall card-JSON field and the frontend never requests them, so nothing else surfaces this.
- The query half takes effect on deploy; the data half only after the next tag import re-stamps
  `card_art_tags` / `card_oracle_tags` and the engine reloads.
- An alias that ever does collide with a slug, or that two tags claim, is dropped with a warning
  rather than guessed at — the slug wins. Neither dump contains one today.
