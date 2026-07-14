# Engine card store: vocab ids for tag/keyword/subtype sets

Tracked as GitHub issue
[#598](https://github.com/jbylund/sylvan_librarian/issues/598). Item 5 of the store-size
reduction series; items 1–3 are recorded in
[00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md), item 4 split out to
[../local-engine-drop-lowercase-copies.md](../local-engine-drop-lowercase-copies.md).

## Status: done (2026-07-03)

## What changed

Per-card `HashSet<String>` / `Vec<String>` collections (keywords, subtypes, frame_data,
oracle_tags, art_tags, is_tags) became `Vec<u16>` of interned ids into a dedicated
per-store vocab table (`CardData.coll_vocab`, built by `VocabInterner`). The set-like
collections are sorted by id and deduped at load; `card_subtypes` keeps the printed order.

The vocab table is separate from the u32-id `CardData.strings` table so the ids fit in
u16: the combined collection vocabulary is ~16k distinct values against 65,536
addressable (~4× headroom), while the strings table holds >65k entries. Halving the id
width saves ~6.4 MB across the ~3.2M collection elements. If the vocabulary ever
exceeds u16::MAX, interning fails loudly with instructions to widen to u32 rather than
silently truncating.

## Re-measured collection columns (tagged DB, 97,199 cards, 2026-07-03)

The 2026-06-12 measurement predated the tagging-import rework
([00499-bulk-tag-import.md](00499-bulk-tag-import.md)) and saw empty tag columns. On the fully
tagged corpus the collections are ~25× larger than measured then:

| Column | Elements | Vocab | As-stored | Distinct bytes |
| --- | ---: | ---: | ---: | ---: |
| card_art_tags | 1,750,849 | 10,756 | 14 MB | 104 kB |
| card_oracle_tags | 1,172,238 | 4,162 | 16 MB | 71 kB |
| card_frame_data | 125,735 | 29 | 629 kB | 237 B |
| card_subtypes | 90,649 | 425 | 523 kB | 2.6 kB |
| card_keywords | 59,001 | 770 | 422 kB | 8.8 kB |
| card_is_tags | 0 | 0 | — | — |

~3.2M per-element allocations and ~31.6 MB of duplicated payload, deduplicating to
~186 kB of vocab. `card_art_tags` — omitted from the original item-5 list because it
was unmeasurable then — turned out to be the largest column.

## Measured impact (protocol of [00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md#measured-progress))

`alloc-counter` build, same 97,199-card corpus exported from the tagged local blue DB,
before/after on the same machine (the tagged corpus makes the baseline much larger than
the 2026-06-12 table's 84.5 MB / 291 MB, so it was re-baselined first):

| Metric | Baseline (main) | Vocab-interned | Δ |
| --- | ---: | ---: | ---: |
| **Archive file** | 160.7 MB | 99.2 MB | −38% |
| — archived cards | 108.6 | 46.8 | −57% |
| — strings table | 16.4 | 16.4 | par |
| — archived indexes | 35.7 | 35.7 | par |
| Rust reload peak | 347.5 MB | 174.5 MB | −50% |
| Build allocations (after cards) | 4.09 M | 0.92 M | −78% |
| Reload wall time | 2.1 s | 1.7 s | −19% |
| Measurement-script RSS peak | 607 MB | 317 MB | −48% |

Query parity and latency, 20-query mix (keyword/subtype/tag/frame plus trigram,
numeric, color, legality, mana, artist regression guards; unique=card/artwork/printing;
all collection fields in the output): **identical totals and card payloads on every
query**; geomean 1.23× faster (median-based) / 1.14× (min-based), 12 queries faster,
8 par, none slower. Full per-query tables in the PR #600 comments. Smaller cards mean
better cache density on scans (full-scan queries up to 2.3×), and collection matching
is integer-only after the follow-up below.

## Follow-up: query-time vocab-id binding (same PR)

The first cut resolved each per-card element id to its string during CollectionCmp
verification, which regressed tag-heavy fallback scans vs the old O(1) hash-set probe
(`otag:removal` 0.80×). Fixed by resolving the query value to its vocab id once per
query — binary search over `coll_vocab_sorted`, a string-sorted permutation of the
vocab archived alongside it — and comparing ids only per card (binary search on the
sorted set-like collections; linear integer scan for order-preserving subtypes). A
value absent from the vocab matches no element. `matches()` no longer needs the vocab
table at all. `otag:removal` ended 1.10× faster than main.

## Notes

- The tag/list indexes (`TagIndex`) still key by `String` so query-time lookup by the
  query's string value is unchanged; only the per-card storage and index build changed.
- `mana_cost.pips` (per-card `HashMap<String, u8>`) was left alone — it is not a
  vocabulary collection and is load-bearing for mana/devotion comparison semantics.
- The archive format version bumped to 20260703; older archives are rejected by the
  header check and rebuilt.

## Related

- [00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md) — items 1–3 and the
  measurement protocol
- [00603-engine-card-printing-split.md](00603-engine-card-printing-split.md) — item 6: restructure
  the store into cards-as-buckets-of-printings (design follow-on from this PR's review)
- [../local-engine-drop-lowercase-copies.md](../local-engine-drop-lowercase-copies.md) — item 4,
  still gated on its benchmark
- [../local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md) — the
  staging-structure size reduced here sets that ticket's live-heap floor
- [00499-bulk-tag-import.md](00499-bulk-tag-import.md) — the tagging rework that unblocked this
