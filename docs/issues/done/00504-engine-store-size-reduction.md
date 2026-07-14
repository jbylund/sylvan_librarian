# Shrinking the Rust engine card store

## Status: done for items 1–3 (2026-06-12); remaining items split out

Items 1–3 (u128 UUIDs, string interning, trigram-CSR dedup) landed and are measured below:
archive 156.6 → 84.5 MB, Rust reload peak 549 → 291 MB. The two gated items moved to their
own tickets: item 4 (drop `_lower` copies) to
[local-engine-drop-lowercase-copies.md](../local-engine-drop-lowercase-copies.md), item 5 (vocab-intern
tag/keyword sets) to
[00598-engine-collection-vocab-interning.md](00598-engine-collection-vocab-interning.md).
This doc is kept as the cost-analysis and measurement record.

## Problem

> Branch note: this describes the shared-memory engine on the `joe/rust/*` branch (the
> [00502-shared-card-store-mmap.md](00502-shared-card-store-mmap.md) design, implemented but not yet merged).
> Upstream `main` does not have the shared store; per-worker numbers there differ.

The engine card store is a flat rkyv archive written to `/dev/shm/sylvan_librarian_cards` and mmap'd
read-only by every worker — storage is shared, not per-worker. Measured precisely (`alloc-counter` feature, 2026-06-12,
96,139 cards): the archive is **156.6 MB** — cards 107.5 MB + indexes 49.1 MB — and building it
costs the reloading worker a transient Rust peak of ~450–550 MB: a 205.6 MB `Vec<Card>` across
2,269,892 allocations, +71.5 MB of heap-form indexes, plus the ~157 MB serialization buffer.
(These are the step-0 baseline numbers; per-change actuals are tracked in
[Implementation tasks](#implementation-tasks).)

Store size matters three ways, all roughly linear in card/index size:

- the tmpfs footprint charged to the container's cgroup (×2 during the atomic-rename swap
  window — see [local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md)), which
  also sets the required Docker `shm_size`;
- the Rust build transient in the reloading worker
  ([incremental loading](00505-engine-incremental-loading.md) removes the ~910 MB Python transient
  that sits alongside it, but not this);
- query-time cache pressure: scans walk the archived cards.

This issue is about making the store — and with it the build transient — smaller.

## Where the bytes go

For reference, the `Card` struct as of 2026-06-12, after items 1–3 (UUIDs as `u128`, scalar
strings interned to `u32` ids, `released_at` string dropped); the archived form mirrors this
field-for-field via rkyv:

```rust
struct Card {
    // Hot fields first — fits in the first two cache lines for fast filter short-circuiting.
    card_name_lower: InlineStr<61>, // 61 bytes covers every card name in the Scryfall dataset
    card_colors: u8,
    card_color_identity: u8,
    produced_mana: u8,
    card_types: u16,

    // UUIDs packed as u128, 0 = null; non-UUID test ids are hashed (parse_uuid_or_hash).
    scryfall_id: u128,
    oracle_id: u128,
    illustration_id: u128,

    // Interned string ids into CardData.strings (NONE_STR = absent).
    card_name_id: u32,
    oracle_text_id: u32,
    oracle_text_lower_id: u32,
    flavor_text_id: u32,
    flavor_text_lower_id: u32,
    card_artist_id: u32,
    card_artist_lower_id: u32,
    card_set_code: InlineStr<8>,
    card_layout_id: u32,
    card_border_id: u32,
    card_watermark_id: u32,
    collector_number_id: u32,
    mana_cost_text_id: u32,
    type_line_id: u32,
    set_name_id: u32,
    released_at_int: Option<u32>,      // yyyymmdd; date/year filters and prefer use this

    cmc: Option<u8>,
    creature_power: Option<i8>,
    creature_toughness: Option<i8>,
    planeswalker_loyalty: Option<u8>,
    card_rarity_int: Option<u8>,
    collector_number_int: Option<u16>,
    edhrec_rank: Option<u32>,
    price_usd: Option<f32>,
    price_eur: Option<f32>,
    price_tix: Option<f32>,
    prefer_score: Option<f32>,
    cubecobra_score: Option<f32>,

    // Not yet interned (item 5, deferred): per-card collections of heap strings.
    card_subtypes: Vec<String>,
    card_keywords: HashSet<String>,
    card_legalities: u64, // 2 bits per format
    card_oracle_tags: HashSet<String>,
    card_is_tags: HashSet<String>,
    card_frame_data: HashSet<String>,

    mana_cost: ManaCost, // per-card pips HashMap<String, u8> — also item-5 territory

    creature_power_text_id: u32,
    creature_toughness_text_id: u32,
}
```

The cost analysis below describes the **step-0 baseline** (all scalar fields were owned
`String`s then); it is kept as the record of where the bytes went. The struct stored 96k
printings covering only ~22k unique oracle IDs, with no sharing and several denormalizations:

1. **Lowercase duplicates.** `oracle_text_lower`, `flavor_text_lower`, `card_artist_lower` — the
   largest text fields are stored twice per card.
2. **Oracle-level text duplicated per printing.** `oracle_text` (×2 with its lower copy),
   `card_name`, `type_line` are identical across all printings of a card (~4.4 printings per
   oracle ID on average) but each printing owns its own heap copy.
3. **Per-card collections of heap strings.** Four `HashSet<String>` (keywords, oracle_tags,
   is_tags, frame_data) plus `Vec<String>` subtypes — ~50 B fixed overhead per set plus one
   allocation per element, with elements drawn from vocabularies of only a few hundred to a few
   thousand distinct values.
4. **Low-cardinality fields stored as `String`.** `card_layout`, `card_border`, `set_name`,
   `card_set_code` repeats, `released_at` (kept even though `released_at_int` is already parsed).
5. **UUIDs as 36-char strings.** `scryfall_id`, `oracle_id`, `illustration_id` ≈ 60 B each as
   `String` vs 16 B as `u128`/`[u8; 16]`.
6. **Trigram indexes** over name and oracle text in `CardIndexes` — rebuilt per store, doubled
   during swap. The oracle trigram index also has per-printing redundancy: identical oracle texts
   produce identical postings.

## Proposed changes, in implementation order

The bulk of the payoff is in items 2–3 (allocation overhead, index size — string *payload* is
only ~29 MB, so deduplication per se is not the win). Item 1 went first because it settled the
`Card` struct shape and dedup key types before the bigger rework, and it independently unlocks
id-based lookups; it landed 2026-06-12 (actuals in
[Implementation tasks](#implementation-tasks)). Item 3 depends on item 2's interner. Item 4 is
*contingent*: once item 2 dedups the `_lower` fields, their residual cost is ~7–8 MB of distinct
payload — re-measure after 2–3 and weigh against the query-time case-folding cost before doing
it.

1. **All three UUIDs as `u128`; drop the dense group fields** *(done 2026-06-12)*. Storing
   `scryfall_id`/`oracle_id`/`illustration_id` as `u128` (a `0` sentinel for null —
   `Option<u128>` is 32 B due to alignment) replaces ~290k `String` allocations (~22 MB) with
   48 B/card (~4.6 MB) and keeps the ids queryable — wanted for future lookup by scryfall /
   oracle / illustration id (Scryfall supports this undocumented). That makes
   `oracle_group`/`artwork_group` (introduced by
   [done/local-engine-broad-query-selection.md](local-engine-broad-query-selection.md)) redundant: the
   linear dedup paths only need "did the key change between adjacent cards" (u128 equality —
   same cost as the u32 compares, so no perf regression), the HashMap path can key on u128, and
   the dense ids that items 2–3 need as array indices come from the interner (it assigns
   0..n_distinct at build time) rather than from per-card fields. Behavior-preserving: any total
   order on u128 keeps equal oracle ids adjacent, and illustration contiguity is per-oracle-id,
   so the lexicographic→numeric sort change is invisible to dedup. For the lookup feature:
   oracle-id lookup is a binary search on the existing sort order, illustration ids are
   contiguous within it (one oracle id per illustration), and only scryfall_id needs a
   `HashMap<u128, idx>` side index. It also simplifies `reload()`: the post-sort pass that walks
   the cards comparing neighbors to assign dense ranks is deleted outright — the sort stays, but
   no rank assignment replaces it (the interner produces its dense ids as a byproduct of
   hash-consing in item 2, not as a separate pass). (Loader bonus: psycopg yields `uuid.UUID`
   objects, whose `.int` is the u128 directly.)
2. **Intern all per-card strings.** A per-store table of distinct strings with cards holding
   `u32` ids, built through hash-consing at load so identical values (oracle_text ×3.0,
   type_line ×17.6, set_name ×130, artist ×43, …) are stored once. This pays twice: the archived
   payload dedups (~49 MB → ~8 MB, and per-printing string prefixes collapse to 4 B ids), and
   the transient heap `Vec<Card>` sheds most of its 2.27 M allocations. Subsumes what would
   otherwise be separate "intern oracle strings" / "enum the low-cardinality fields" items.
3. **Dedup oracle trigram postings by interned oracle-text id.** The index is built per printing
   despite only 28,491 distinct oracle texts (3.0× redundancy); posting by the dense text id the
   interner (item 2) assigns, with a text-id → card-indices expansion table built at load,
   shrinks the largest index ~3× (~60 MB → ~20 MB, estimated). (Identical texts are not always
   adjacent in the store — the same text can appear under different oracle ids — hence the
   explicit expansion table rather than index ranges.)
4. **Drop the `_lower` copies — contingent, re-evaluate after items 2–3.** Standalone they are
   ~20 MB of payload, but after interning they dedup to ~7–8 MB of distinct strings plus 4 B
   ids, while the removal cost is unchanged: case-insensitive matching must fold case during
   verification — cheap on trigram-pruned candidate sets, but patterns shorter than 3 chars get
   no pruning and would fold-and-scan all 96k cards per query. Decide with a benchmark once the
   interned numbers are real.
5. **Vocab ids for tag/keyword/subtype sets** *(deferred — the tagging import is being reworked
   first)*. Measured payload is small (~1.6 MB across keywords/subtypes/frame_data) — the
   motivation is the ~270k per-element allocations and the *unmeasured*
   `card_oracle_tags`/`card_is_tags`, which were empty in the local DB but will be substantial
   on a fully tagged corpus. Per-card sorted `Vec<u32>` (or bitset where the vocab fits) over a
   global vocab table.
6. **Drop `released_at` string** (already parsed to `released_at_int`); format on demand for
   payloads. Small, near-free while doing item 2.

Considered and rejected: interning `card_name_lower` (InlineStr<61>, ~6 MB — the largest
remaining card field). It is deliberately inline as the first struct field so exact-name and
name-contains checks read it from the card's first cache line; interning would trade ~5 MB for
an indirection on the most common query class. (`card_set_code`'s InlineStr<8> is likewise
kept — interning it would save ~0.5 MB.) The resulting rule: intern cold/payload fields,
keep scan-hot fields inline.

Considered and rejected: dropping `type_line` and reconstructing it from types + subtypes.
It is payload-only (all filtering uses the `card_types` bits and subtype list), but
reconstruction breaks on multi-face `//` lines (types/subtypes are face-unions), supertypes,
and the printed (not rule-derivable) subtype order — and after interning the 3,378 distinct
type lines cost ~0.5 MB total, so there is nothing meaningful left to save.

## Measured field bytes (DB, 96,139 printings, 2026-06-12)

`sum(octet_length(col))` as stored per printing vs. summed over distinct values:

| Field | Total | Distinct values | Distinct bytes | Dup factor |
| --- | ---: | ---: | ---: | ---: |
| oracle_text | 14.1 MB | 28,491 | 4.7 MB | 3.0× |
| flavor_text | 4.5 MB | 25,819 | 2.3 MB | 2.0× |
| type_line | 1.8 MB | 3,378 | 0.10 MB | 17.6× |
| set_name | 1.8 MB | 653 | 0.014 MB | 130× |
| card_name | 1.5 MB | 30,872 | 0.51 MB | 3.0× |
| card_artist | 1.2 MB | 2,195 | 0.029 MB | 43× |
| layout / border / mana_cost / collector / watermark | 2.0 MB | — | < 0.07 MB | 60–17,000× |
| keywords / subtypes / frame_data elements | 1.6 MB | 29–668 per vocab | < 0.01 MB | 55–2,700× |

(96,139 printings / 30,869 oracle IDs = 3.1 printings per oracle. `card_oracle_tags` /
`card_is_tags` were empty in the measured DB — tagger data not imported — so their cost is
unmeasured and will add on a fully tagged corpus.)

**The headline: all as-stored string payload is only ~29 MB** — ~49 MB counting the `_lower`
duplicates of oracle/flavor/artist — yet the archived cards weigh 107.5 MB and their transient
heap form 205.6 MB. Reconciling against the exact alloc-counter numbers:

- **Transient heap `Vec<Card>` (205.6 MB, 2.27 M allocations ≈ 24/card).** `String` headers
  (24 B each), malloc rounding, and per-card `HashSet`/`Vec` overhead roughly triple the ~49 MB
  payload, on top of the ~600 B/card fixed struct. This form exists only during reload, but it
  is the largest single block of the build transient.
- **Archived cards (107.5 MB).** rkyv eliminates malloc overhead but interns nothing: the full
  duplicated payload (~49 MB) plus fixed fields and per-string/per-collection length prefixes
  (~58 MB ≈ ~600 B/card) are written out per printing.
- **Archived indexes (49.1 MB; 71.5 MB in heap form during build).** Dominated by the oracle
  trigram postings, built per printing despite only 28,491 distinct texts (3.0× redundancy).

## Estimated impact

| Component | Today (measured) | After (est.) | Mechanism |
| --- | --- | --- | --- |
| archived cards | 107.5 MB | ~45 MB | intern by id, drop `_lower`, u128 UUIDs |
| archived indexes | 49.1 MB | ~22 MB | postings by interned text id |
| build transient `Vec<Card>` | 205.6 MB / 2.27 M allocs | ~80 MB / a few 100k | same changes, applied to the heap form |

Plausible end state: **archive ~65–75 MB** (~2.3× smaller) and a build-transient Rust peak of
~250 MB instead of ~550 MB.

## Measured progress

Same instrumented protocol throughout (`alloc-counter` feature, 96,139 cards, local blue DB);
each column is measured after that item landed, all on 2026-06-12. Remaining gap to the end
state: item 4 (contingent, ~7–8 MB) and item 5 (deferred).

| Metric | Step 0 | 1: u128 UUIDs | 2: interning | 3: trigram CSR |
| --- | ---: | ---: | ---: | ---: |
| **Archive file (shared store)** | 156.6 MB | 148.9 MB | 111.7 MB | **84.5 MB** |
| — archived cards | 107.5 | 99.8 | 47.2 | 47.2 |
| — strings table | — | — | 15.3 | 15.3 |
| — archived indexes | 49.1 | 49.1 | 49.1 | 22.0 |
| Rust reload peak | 549 MB | 536 MB | 330 MB | 291 MB |
| — heap `Vec<Card>` (+ interner) | 205.6 | 192.7 | 144.9 | 144.9 |
| — heap index build delta | 71.5 | 71.5 | 49.1 | 9.7 |
| Build allocations (after cards) | 2.27 M | 1.98 M | 0.96 M | 0.96 M |
| Building-worker total RSS peak | 1546 MB | 1513 MB | 1391 MB | 1305 MB |

Knock-on effects:

- Required `shm_size` roughly halves (two archives coexist during the rename window:
  ~313 MB → ~140 MB), as does the tmpfs charge against the container's memory limit.
- Combined with [incremental loading](00505-engine-incremental-loading.md), the building worker's
  total reload peak drops from ~1.5 GB to roughly ~0.4 GB.
- Query latency is neutral-to-improved (measured 2026-06-12 vs the pre-item-1 rkyv baseline,
  identical match counts on a 12-query mix): multi-trigram phrase search −57%, color scan −20%,
  exact name −21%, most others par; worst case +7% (`set:lea`, +75 µs). Smaller cards mean
  better cache density on scans, and text-id-space posting intersection beats card-id-space.

## Implementation tasks

- [x] Measure true store size precisely, broken down by cards vs trigram indexes vs allocation
  overhead — done 2026-06-12 via the `alloc-counter` cargo feature (`QueryEngine.mem_stats()`);
  numbers in [Problem](#problem) and the headline analysis
- [x] ~~Re-measure tag columns on a fully tagged DB~~ — moved to
  [00598-engine-collection-vocab-interning.md](00598-engine-collection-vocab-interning.md)
- [x] All three UUIDs as `u128` with 0-as-null sentinel; drop `oracle_group`/`artwork_group` and
  the rank-assignment pass in `reload()`; switch dedup paths to u128 keys — done 2026-06-12.
  The −288k allocations matched the 3 × 96k prediction. Non-UUID test ids are FNV-hashed
  (`parse_uuid_or_hash`); 0 is reserved for null.
- [x] Hash-consing interning for per-card strings (`u32` ids into a per-store table) — done
  2026-06-12, with the `released_at` string drop (item 6) folded in: date/year filters now
  compare `released_at_int` numerically (partial dates zero-padded to yyyymmdd, preserving the
  old lexicographic-prefix semantics).
- [x] Dedup oracle trigram postings by interned text id; expand via dense-CSR
  text-id → card-indices table — done 2026-06-12. Postings intersect in text-id space, then
  expand + sort to store order; the heap-form index build also shrank ~71 → ~10 MB.
- [x] ~~Remove `_lower` fields; vocab-intern keywords/tags/subtypes/frame_data; re-run the
  memory measurement~~ — moved to
  [local-engine-drop-lowercase-copies.md](../local-engine-drop-lowercase-copies.md) and
  [00598-engine-collection-vocab-interning.md](00598-engine-collection-vocab-interning.md)
