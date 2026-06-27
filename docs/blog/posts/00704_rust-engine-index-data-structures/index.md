---
title: "In-Process Card Search Without a Query Planner: Trigrams, Sorted Arrays, and Hash Maps"
date: 2027-04-24
publishDate: 2027-04-24
tags: ["rust", "performance", "indexing"]
summary: "The index types used to accelerate filtering in the Rust engine: trigram sets, sorted arrays, hash maps, and how each maps to query operators."
---

The Rust engine filters ~96,000 card printings in under a millisecond. It has no query planner, no cost model, and no statistics. What it has instead is four index types built at reload time, each matched to one category of query operator. The right index for a substring search is useless for a range query, and the right index for a range query would be wasteful for set membership. This post covers what each one looks like, why its shape fits its query type, and where indexes do not help at all.

## What an In-Process Index Needs to Do

A database planner has two jobs: pick an access strategy, then execute it. The Rust engine collapses those into one. Every filter expression that can use an index does so through a single `narrow_candidates` function that returns a sorted `Vec<u32>` of card store indices — or `None` if no index applies. When `None` comes back, the engine falls back to a full scan. When a candidate set comes back, the filter still runs on every candidate (the index gives a superset, not an exact match), but the scan is over a much smaller slice.

The key structural requirement is that posting lists must be sorted. Intersecting two sorted lists is O(m + n) with a two-pointer walk. Intersecting a sorted list against a `HashSet` is O(m) probe-per-element, which is faster at large sizes but requires materializing a set. For the sizes here — trigram posting lists cap at tens of thousands of entries for common three-letter sequences — a merge walk over two sorted slices is cheaper and produces a sorted result without extra work.

## Trigram Sets for Substring Search

A trigram is a three-character window of a string. The text "lightning" produces: "lig", "igh", "ght", "htn", "tni", "nin", "ing". Any card whose oracle text contains "lightning" must contain all seven of those trigrams. So a trigram index maps each trigram to the sorted list of cards (or texts) that contain it, and a substring query becomes an intersection of posting lists — one per trigram in the search term.

```rust
type TrigramIndex = HashMap<[u8; 3], Vec<u32>>;
```

The index is a `HashMap` from a three-byte key to a sorted posting list of card store indices. Building it is a single pass over the card store: for each card, slide a three-byte window over the text and append the card's index to each matching posting list, deduplicating consecutive equal entries (a card appears at most once per trigram).

Card names get a straightforward trigram index: one posting entry per (trigram, card) pair, building sorted posting lists by iterating cards in store order.

Oracle text gets a more involved variant because printings share text heavily. In a 96,000-printing store, there are roughly 28,000 distinct oracle texts — each text appears on average 3.4 times across different sets and languages. Building a naive trigram index over all printings would store each posting 3.4× over. Instead, the engine builds a **CSR (compressed sparse row)** table alongside the trigram index:

- Dense-remap the distinct oracle texts to sequential IDs (0 through n_texts − 1)
- Build the trigram index over texts, not printings (posting lists hold text IDs)
- Store the expansion: for each text ID, the list of card store indices that carry it

A substring search against oracle text intersects posting lists in the shorter text-ID space, then expands survivors through the CSR table to card indices. The expansion requires a sort (text IDs have no ordering relationship to card indices), but that sort runs only on the surviving candidates, not on all 96,000 printings.

At query time, if any trigram in the search term is absent from the index, the result is immediately empty — no intersection needed.

```rust
fn trigram_candidates(idx: &Archived<TrigramIndex>, word: &str) -> Option<Vec<u32>> {
    // ...
    for w in bytes.windows(3) {
        match idx.get(&[w[0], w[1], w[2]]) {
            Some(list) => lists.push(list),
            None => return Some(Vec::new()),  // trigram not in any card: done
        }
    }
    lists.sort_unstable_by_key(|l| l.len());  // intersect shortest first
    // ...
}
```

The function takes `Archived<TrigramIndex>` rather than `TrigramIndex` because the engine stores the indexes in an rkyv-serialized archive that is memory-mapped into each worker process. `Archived<T>` is rkyv's zero-copy view of type `T` — the hash map is accessed directly from the mapped bytes with no deserialization step. The lookup semantics are identical to a live `HashMap`, but the data lives in the file page cache rather than the Rust heap. (Zero-copy deserialization and shared memory are covered in more depth in the [rkyv and shared memory](/blog/posts/00768_rkyv-shared-memory) post.)

The lists are intersected shortest-first. For a four-word search term like `o:"whenever you draw a card"`, the rarest trigram's posting list might have 200 entries; the most common one ("you") might have 20,000. Intersecting the 200-entry list with the 20,000-entry list produces a small result; intersecting the 20,000-entry list with the 15,000-entry list first wastes work.

The trigram approach has one hard limitation: it cannot accelerate searches on words shorter than three characters. A query for `o:at` falls back to a full scan over all 96,000 cards, since no three-character window can be formed. In practice this is rare — most meaningful oracle text searches are at least four characters — but it is the correct tradeoff: an index for one- and two-character queries would need extremely long posting lists (nearly every card contains "a", "or") and the intersection gain would be negligible.

## Sorted Arrays for Range Queries

```rust
type NumericIndex = Vec<(i16, u32)>;
```

A numeric index is a vector of (value, card_index) pairs, sorted by value. Building one is filter-then-sort: extract pairs where the field is present, then sort.

```rust
fn build_numeric_index(cards: &[Card], get_val: impl Fn(&Card) -> Option<i16>) -> NumericIndex {
    let mut idx = cards.iter().enumerate()
        .filter_map(|(i, c)| get_val(c).map(|v| (v, i as u32)))
        .collect::<Vec<_>>();
    idx.sort_unstable();
    idx
}
```

The engine stores three: one for CMC, one for creature power, one for creature toughness. The i16 type covers both u8 (CMC ranges from 0 to about 16 in practice) and i8 (power and toughness can be negative — Char-Rumbler has -1/-1 in some sets) without loss.

A range query — `cmc<=3`, `pow>=5`, `tou=3` — becomes a `partition_point` binary search on the sorted slice:

```rust
CmpOp::Le => (0, idx.partition_point(|p| i16::from(p.0) as f64 <= val)),
CmpOp::Gt => (idx.partition_point(|p| i16::from(p.0) as f64 <= val), idx.len()),
CmpOp::Eq => {
    let s = idx.partition_point(|p| (i16::from(p.0) as f64) < val);
    let e = idx.partition_point(|p| (i16::from(p.0) as f64) <= val);
    (s, e)
}
```

`Le` and `Gt` each need one binary search: the split point between the matching and non-matching halves. `Eq` needs two: one to find where the equal-value run starts (everything strictly less than `val`) and one to find where it ends (everything less than or equal to `val`). A single `partition_point` gives either a lower or upper bound, not both.

The result is a slice of matching (value, card_index) pairs. Card indices within that slice are not sorted — the sort was by value, not by card position — so extracting them requires a secondary sort before they can be intersected with other candidate sets.

The `Ne` (not-equal) case returns `None` deliberately. A not-equal range like `cmc!=3` excludes a small number of cards from a large match set. There is no efficient way to represent "everything except these 800 cards" as a posting list — it would be almost the full store — so the engine falls back to full scan and evaluates the predicate per-card.

The sorted array is space-efficient relative to a hash map: for 96,000 cards, a power index with ~40,000 entries (only creatures have power) uses about 240 KB. A hash map over the same data would use roughly twice that due to load factor and bucket overhead — and it would not support range queries at all.

## Hash Maps for Set Membership

Keywords, subtypes, oracle tags, art tags, and is-tags all use the same structure:

```rust
type TagIndex = HashMap<String, Vec<u32>>;
```

Building a tag index is a single pass: for each card, for each value in the relevant set or list, append the card's store index to the posting list for that value. Cards are iterated in ascending store order, so posting lists come out sorted without an extra sort step.

```rust
fn build_tag_index(cards: &[Card], get_tags: impl Fn(&Card) -> &HashSet<String>) -> TagIndex {
    let mut idx: TagIndex = HashMap::new();
    for (i, card) in cards.iter().enumerate() {
        for tag in get_tags(card) {
            idx.entry(tag.clone()).or_default().push(i as u32);
        }
    }
    idx
}
```

At query time, a single hash map lookup gives the sorted posting list for the queried value. For `kw:flying`, the posting list is the sorted list of all card store indices with the Flying keyword — returned immediately, no scan needed.

The hash map handles only the `>=` (contains) operator efficiently. Set-comparison operators like `=` (card's collection is exactly this one value) and `<=` (card's collection is a subset of this value) cannot use the index, because they require inspecting the full collection of each candidate. The index narrows candidates for the `>=` case; the per-card filter verifies the full predicate.

## Type Bits: One Posting List per Bit

Card supertypes and card types — Artifact, Creature, Instant, Sorcery, Land, and the rest — are encoded as a u16 bitmask on each card (fourteen types, one bit each). The index maps each bit position to a sorted list of card store indices:

```rust
type TypeIndex = [Vec<u32>; 14];
```

Building it is a single pass that extracts set bits:

```rust
fn build_type_index(cards: &[Card]) -> TypeIndex {
    let mut idx: TypeIndex = Default::default();
    for (i, card) in cards.iter().enumerate() {
        let mut bits = card.card_types;
        while bits != 0 {
            let bit = bits.trailing_zeros() as usize;
            idx[bit].push(i as u32);
            bits &= bits - 1;
        }
    }
    idx
}
```

A query like `t:creature` sets bit 4 in the type mask and retrieves `idx[4]` — the sorted list of all creature cards. A query `t:artifact t:creature` (artifact creatures) triggers `narrow_candidates` on the And node, which intersects the two posting lists.

A query like `t:creature OR t:instant` unions the posting lists. The engine has a `union_sorted` function that merges two sorted slices in O(m + n), producing a deduplicated sorted result.

## What Does Not Have an Index

Several predicate types fall through `narrow_candidates` to a full scan:

**Regex** (`name:/pattern/`, `o:/pattern/`): arbitrary regular expressions cannot be indexed. Cost depends on pattern complexity; a simple anchored pattern runs fast, a pathological backtracking pattern is expensive.

**Color and color identity**: stored as a bitmask byte, checked per-card with a single bitwise AND. A posting-list index over 64 possible values (6-bit field) would save roughly half the per-card work — at the cost of 64 posting lists summing to ~96,000 entries total. Whether that is worth building and maintaining is a design assumption we have not validated against production query distributions. Color is often combined with other predicates (`c:u pow>4`) where the power index already narrows candidates, making the color check a per-candidate AND rather than a primary filter.

**Legality**: stored as a 64-bit integer with 2 bits per format (roughly 30 formats, ~60 bits used). Same situation: per-card check is cheap; whether legality is selective enough as a primary filter to justify an index is not measured. Format queries like `format:modern` do return large result sets (~70,000 rows) which suggests legality often is the primary filter — this is an honest gap in the design.

**Mana cost**: stored as a `HashMap<String, u8>` per card mapping pip symbols to counts. The combination of pip counts and CMC makes this hard to index efficiently.

**Arithmetic expressions** (`cmc+1<power`): the left- and right-hand sides both involve fields; the engine evaluates the arithmetic per-card.

**Not-equal numeric**: as noted above, excluded because the result would be nearly the full store.

For a query that combines indexed and unindexed predicates — say, `o:"whenever you draw a card" c:u` — `narrow_candidates` collects candidate sets from the indexed predicates (the oracle text trigram index), intersects them, and returns a narrowed set. The unindexed color check then runs per-candidate on that narrowed slice only.

For Or, the rule is conservative: if any child of an Or cannot provide a candidate set, the whole Or returns `None`. A query like `(o:"draw a card") OR (c:u)` gets no index help — the color child has no index, so the Or must scan all 96,000 cards. This is correct because the alternative — index the oracle text child, then union with a full scan for the color child — produces a union of "some cards" and "all 96,000 cards," which is still all 96,000 cards. An unindexed Or child always contributes the full store to the union, so partial indexing gains nothing.

## Index Build Time Versus Query Time

All four index types are built during `reload_commit`, after all card batches have been loaded. The trigram indexes are the most expensive: the oracle text variant builds a per-text trigram map, then constructs the CSR table with a counting sort, which requires two passes. The sorted-array numeric indexes are a filter-and-sort; the tag and type indexes are single-pass.

Measured on an M5 Max MacBook Pro (release build, warm engine, full ~96,000-card dataset from the 2027-01 Scryfall bulk export), timing was captured with `std::time::Instant` wrapping the query call, median of 100 runs. A trigram oracle text search — `o:"whenever you draw a card"` — returns roughly 80 surviving text IDs that expand to ~270 printings; the full query including per-card filter completes in about 0.15 ms. An unindexed predicate over all 96,000 cards — `c:u` with no other filter — runs in about 0.3–0.5 ms: 96,000 bitwise operations on compact structs that fit in L2 cache. Production numbers will differ with dataset size and query mix, but the ordering is stable: indexed queries are consistently faster than full scans, and the gap widens with query selectivity.

The index build cost is paid once per data reload (~10 minutes on the default schedule, driven by the database fetch, not the index build). Query latency is what matters for interactive search. Build times for the individual index types have not been measured in isolation — the trigram CSR construction is the most structurally complex, but all indexes complete well within the time already spent fetching data from the database.

## The Core Tension

The four index types represent three distinct structural decisions:

- Trigram (HashMap of posting lists) vs. sorted array: trigrams support substring, sorted arrays support range. Using a sorted array for substring would require sorting by position within text — meaningless. Using a trigram index for range would require one posting list per possible pair of adjacent values — exponentially expensive.
- Tag hash map vs. type bit array: functionally identical (both are HashMap<key, sorted Vec<u32>>), but type bits are a closed set of 14 values encoded at build time as an array. The closed enumeration lets the index be a fixed-size array with O(1) access by bit position rather than a hash lookup.
- All posting lists vs. per-card bitmaps (color, legality): for a field with very few distinct values and cheap per-card evaluation, a full scan is competitive with an index. Color has 64 possible values; evaluating it per-card costs one bitwise AND. An index would save the AND but require a hash lookup and a posting list traversal. For highly selective queries the index wins; for queries where color is a secondary filter over an already-narrowed candidate set, the scan is fast enough that the index pays no benefit.

The CSR oracle text design makes this tension most visible: a naive posting list over all 96,000 printings is 3.4× larger than a posting list over 28,000 distinct texts. The CSR indirection adds build-time complexity and a query-time sort of the expanded survivors. For a search term like "whenever you draw a card", the text-ID intersection returns 80 texts that expand to 270 printings; sorting 270 integers costs almost nothing compared to intersecting posting lists that are 3.4× shorter at every step of the intersection. The tradeoff is not a bet — it is a calculation, and the numbers make it obvious which side wins.
