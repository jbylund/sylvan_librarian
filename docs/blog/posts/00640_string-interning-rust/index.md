---
title: "96k Cards, One Copy of Each String: String Interning in the Rust Engine"
date: 2027-03-27
publishDate: 2027-03-27
tags: ["rust", "performance", "memory"]
summary: "The Rust card store held 96k printings, each owning its own heap-allocated strings — even though 'Forest' appears 130 times with the same set name and 'Shock' has the same oracle text across every printing. Replacing each unique string with a u32 ID cut the archived card data from 107 MB to 47 MB and the build-time heap from 205 MB to 145 MB."
---

After the first version of the card store landed ([PR #490](https://github.com/jbylund/arcane_tutor/pull/490)),
96,139 cards produced ~29 MB of actual string payload — but a 205 MB transient heap and a
107 MB archived card section.
That 3–7× gap needed an explanation.
The data had always been this repetitive; the Rust engine was just the first representation to hold all of it in memory at once.
A feature-gated counting allocator (`--features alloc-counter`, `QueryEngine.mem_stats()`) was already wired in for exactly this kind of question; the breakdown it returned pointed straight at the cause.

## Where the 107 MB Came From

The initial `Card` struct stored all string fields as owned `String` values:

```rust
struct Card {
    card_name: String,
    oracle_text: String,
    oracle_text_lower: String,   // lowercase copy, kept for case-insensitive search
    flavor_text: String,
    flavor_text_lower: String,
    card_artist: Option<String>,
    card_artist_lower: Option<String>,
    card_set_code: InlineStr<8>, // short enough to inline; not interned
    card_layout: String,
    card_border: String,
    card_watermark: Option<String>,
    type_line: String,
    set_name: String,
    // ... plus oracle_id, scryfall_id as 36-char UUID strings
}
```

Every printing owned its own heap allocations for all of those fields.
A `String` in Rust is 24 bytes of header (pointer + length + capacity) plus a heap block.
Across 96k printings, that is roughly 24 allocations per card just for strings — 2.27 million total allocations in the card vector alone.

The real problem was duplication.
Running `sum(octet_length(col))` over the Scryfall dataset shows how much each field repeats:

| Field | Total across all printings | Distinct values | Duplication factor |
|---|---:|---:|---:|
| oracle_text | 14.1 MB | 28,491 distinct texts | 3.0× |
| type_line | 1.8 MB | 3,378 distinct lines | 17.6× |
| set_name | 1.8 MB | 653 distinct names | **130×** |
| card_artist | 1.2 MB | 2,195 distinct artists | 43× |
| card_name | 1.5 MB | 30,872 distinct names | 3.0× |

"Basic Forest" has the same type line as every other Basic Forest printing.
Mark Rosewater designed 2,000+ cards but there are only 2,195 distinct artists in the dataset — some very busy.
The set name "Mirage" appears on every one of the ~300 Mirage printings.

When rkyv serializes the card store to a flat buffer, it inlines all those duplicate strings with no sharing.
Each printing writes its own copy.
That is why 29 MB of distinct string payload became 107 MB of archived cards.

## The Fix: Hash-Consing at Load Time

String interning solves this by giving each unique string a numeric ID.
The `Interner` struct does the work at load time:

```rust
/// Build-time hash-consing interner; `strings` becomes CardData.strings.
struct Interner {
    map: HashMap<String, u32>,
    strings: Vec<String>,
}

impl Interner {
    fn new() -> Self {
        // Pre-intern "" as id 0: plain (non-optional) fields default to it when missing.
        let mut it = Interner { map: HashMap::new(), strings: Vec::new() };
        it.intern(String::new());
        it
    }

    fn intern(&mut self, s: String) -> u32 {
        if let Some(&id) = self.map.get(&s) {
            return id;
        }
        let id = self.strings.len() as u32;
        self.strings.push(s.clone());
        self.map.insert(s, id);
        id
    }

    fn intern_opt(&mut self, s: Option<String>) -> u32 {
        match s {
            Some(v) => self.intern(v),
            None => NONE_STR,  // u32::MAX = sentinel for absent optional strings
        }
    }
}
```

The full implementation is at
[`card_engine/src/lib.rs`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L224-L253).

The `Card` struct becomes:

```rust
struct Card {
    // Interned string ids into CardData.strings (NONE_STR = absent).
    // Identical values share one table entry; resolve with str_at()/the strings slice.
    card_name_id: u32,
    oracle_text_id: u32,
    oracle_text_lower_id: u32,
    flavor_text_id: u32,
    flavor_text_lower_id: u32,
    card_artist_id: u32,
    card_artist_lower_id: u32,
    card_layout_id: u32,
    card_border_id: u32,
    card_watermark_id: u32,
    type_line_id: u32,
    set_name_id: u32,
    // ... card_set_code stays as InlineStr<8>: 9 bytes, no pointer, scan-hot
}
```

Sixteen string fields collapsed to sixteen `u32` IDs.
The shared string table lives once on `CardData`:

```rust
struct CardData {
    cards:   Vec<Card>,
    strings: Vec<String>,   // Hash-consed table for the interned-string fields on Card
    indexes: CardIndexes,
    // ...
}
```

Resolving a string at query time costs one bounds check and one slice index:

```rust
/// Resolve an interned id against the archived string table; None for absent.
pub(crate) fn str_at(strings: &AStrings, id: u32) -> Option<&str> {
    if id == NONE_STR { None } else { Some(strings[id as usize].as_str()) }
}
```

## Two Fields That Stayed Inline

Two string fields were deliberately excluded from interning.

`card_name_lower` stays as `InlineStr<61>` — a fixed-size array of 61 bytes plus a length byte, no heap pointer.
It sits at the very start of the `Card` struct, so exact-name and name-contains checks read it from the card's first cache line without touching a second memory location.
Interning it would save roughly 5 MB at the cost of a pointer-chase on every name filter evaluation against 96k cards.

`card_set_code` stays as `InlineStr<8>` for the same reason: an 8-byte set code fits in 9 bytes inline.
Interning it would save roughly 0.5 MB.

The rule that emerged: intern cold fields (oracle text, flavor text, type line, set name, artist, layout, border, watermark) and keep scan-hot fields inline.

## How rkyv Serializes the IDs

rkyv serializes the `Card` struct field-for-field into a flat buffer.
A `u32` is 4 bytes in the archive.
A `String` in rkyv's representation carries the string content plus a length prefix.
Before interning, each printing wrote its own copy of every string into the archive; after interning, each printing writes 4 bytes per string field, and the strings table (`Vec<String>`) is written once for the whole store.

When the archive is mmapped, filter evaluation reads directly from the archived types (`Archived<Card>`) without deserializing.
A filter checking oracle text resolves the `oracle_text_lower_id` field — a `u32` in the mmap — through the shared strings table.
The string table is in the same mmap, so both the ID and the string it resolves to stay in the shared OS page cache.
All workers read the same physical pages.
This is described in more detail in the post on zero-copy deserialization with rkyv.

## Measured Impact

The `alloc-counter` feature was used to instrument each step precisely.
Each column is measured after that step landed; "before" is the initial shared-store baseline with all-`String` fields (M3 Max, local blue DB, 96,139 cards, 2026-06-12):

| Metric | Before interning | After interning |
|---|---:|---:|
| Archived cards | 107.5 MB | **47.2 MB** |
| Strings table in archive | — | 15.3 MB |
| Build allocations | 2.27 million | **0.96 million** |
| Rust reload peak (heap) | 549 MB | **330 MB** |

The archived card section shrank 56%.
Combined with deduplicating the oracle-text trigram index (a separate change in the same PR), the full archive went from 156.6 MB to 84.5 MB — 46% smaller — and the required Docker `shm_size` for `/dev/shm` dropped proportionally.

One latency side effect that was not anticipated: most query types got faster.
Smaller cards mean each cache line covers more of the store during a linear scan.
Color scans (`id:g`) improved 20%; exact name queries (`name:soldier`) improved 21% (median of 25 runs, same data, before vs. after the interning commit).
The one regression was `set:lea` — which got 7% slower (+75 µs absolute).
`card_set_code` is stored as `InlineStr<8>` and was not interned, so the strings-table indirection is not the cause; the actual regression source is not identified.
The candidate set for `set:lea` is small (~300 Lea printings), so the regression may reflect noise or a change in how the narrowed candidate set interacts with the CPU cache after the card structs shrank — but that has not been verified.

## What Interning Cannot Do

Interning applies only to scalar string fields — the one-string-per-card columns.
The per-card collections (`card_keywords: HashSet<String>`, `card_subtypes: Vec<String>`, `card_oracle_tags`, `card_is_tags`, `card_frame_data`) are not interned.
Their measured payload is small (~1.6 MB across all printings), but each element is a separate heap allocation — roughly 270k allocations total — and the oracle/art tag collections were empty in the measured dataset (the tagger import had not yet run).
Vocabulary interning for these fields is planned once the tag import is settled.

The `_lower` fields (`oracle_text_lower_id`, `flavor_text_lower_id`, `card_artist_lower_id`) are still stored alongside their original-case twins.
After interning, the distinct lowercase strings cost about 7–8 MB of payload plus 4 bytes per card per field.
Dropping them would require case-folding during filter evaluation — cheap when trigrams have already pruned the candidate set to a few hundred cards, but patterns shorter than three characters get no trigram pruning and would fold-and-scan all 96k cards on every request.
That benchmark has not been run, so the fields stay.

The Scryfall dataset had always been this repetitive — 130 copies of every set name, 43 copies of every artist string.
PostgreSQL never made that visible: a query returns rows one at a time, so no single process ever held all 96k strings simultaneously.
The Rust engine materialized the full card store as one in-process structure for the first time, and the duplication had nowhere to hide.
