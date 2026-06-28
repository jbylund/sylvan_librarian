---
title: "The /random Index Pays Off Twice: 1.87x Faster on Common Card Searches"
date: 2027-07-28
publishDate: 2027-07-28
tags: ["rust", "performance", "query", "indexing"]
summary: "The preferred-printing index built for /random already stored one entry per unique card. Reusing it for card-level search queries eliminates both the full-printings scan and the dedup step — a 1.87x geometric mean speedup on the most common access patterns."
---

## A Pre-Built Index Looking for a Second Job

The [previous post](../00896_random-card-sampling/) described the preferred-printing index: a `Vec<u32>` stored in the shared mmap at reload time, holding one store index per unique oracle ID — whichever printing has the highest `prefer_score`.
It was built to power the `/random` endpoint without scanning all 97k printings on every request.

Once it existed, the same index had an obvious second use.

The Rust engine stores one row per printing — all 97k of them.
For a `unique=card` query, the engine walks every printing, applies the filter, and deduplicates to one result per oracle ID via a linear scan.
Two costs: scanning ~97k entries and resolving dedup groups.

For a large class of queries neither cost is necessary.

## The Insight: Card-Level Attributes Are Constant Across Printings

A card's name, oracle text, color identity, type line, power, toughness, CMC, format legality, and keywords are the same on every printing of that card.
If a filter only touches those attributes, then the preferred printing for each oracle ID passes the filter if and only if every other printing would too.
Scanning just the preferred printings — one per oracle ID, ~31k entries — produces exactly the same result as scanning all 97k printings and deduplicating.

The dedup step disappears: one entry per oracle ID means every match is already unique.

## Classifying Filters at Query Time

The implementation adds `is_card_level()` to `FilterExpr` and its leaf field types.
It returns `true` when every predicate in the tree touches only card-level attributes:

```rust
fn is_card_level(&self) -> bool {
    match self {
        FilterExpr::True | FilterExpr::ExactName(_) => true,
        FilterExpr::And(c) | FilterExpr::Or(c) => c.iter().all(|x| x.is_card_level()),
        FilterExpr::Not(inner) => inner.is_card_level(),
        FilterExpr::NumericCmp { lhs, rhs, .. } => lhs.is_card_level() && rhs.is_card_level(),
        FilterExpr::ColorCmp { .. } | FilterExpr::TypeCmp { .. } => true,
        FilterExpr::CollectionCmp { field, .. } => field.is_card_level(),
        FilterExpr::Legality { .. } | FilterExpr::ManaCostCmp { .. } | FilterExpr::Devotion { .. } => true,
        FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => false,
        // ... text/regex fields dispatch to their own is_card_level()
    }
}
```

Printing-level attributes — rarity, collector number, set code, price, artist, flavor text, release date, art tags, frame data — return `false`, correctly routing those queries through the original path.

## Composing with Existing Index Narrowing

`run_query` already calls `narrow_candidates` before scanning: trigram posting lists, type bit arrays, and sorted CMC/power/toughness arrays can reduce the candidate set from 97k to a few hundred before any card is evaluated.
The preferred-printing index composes with this rather than replacing it.

When `narrow_candidates` returns a candidate set, the fast path intersects it with the preferred index.
When it returns nothing (no index applies), the preferred index becomes the full candidate set.

```rust
if unique == "card"
    && !matches!(prefer, "oldest" | "newest" | "usd_low" | "usd_high" | "promo")
    && filter.is_card_level()
{
    return match candidates {
        Some(existing) => {
            let fast = intersect_sorted(&existing, preferred_indices);
            run_query_no_dedup(fast.into_iter().map(|i| &store[i as usize]), ...)
        }
        None => run_query_no_dedup(
            preferred_indices.iter().map(|&i| &store[u32::from(i) as usize]),
            ...
        ),
    };
}
```

`intersect_sorted` is the same merge-based routine used for trigram posting list intersection — it expects both inputs sorted, which they are: the store is sorted by `(oracle_id, illustration_id)` at build time, and the preferred index is built in a single sorted pass over that store.

The `prefer` mode guard excludes `oldest`, `newest`, `usd_low`, `usd_high`, and `promo` because those modes select a specific printing by a criterion other than `prefer_score`.
The preferred index holds the highest-`prefer_score` printing per oracle ID; returning it for a `prefer=oldest` query would give the wrong printing.
The default mode is `prefer_score`, which is precisely the criterion the index was built on — so the fast path is correct by construction for that mode.

## Benchmark Results

Measured on an Apple M3 Pro, single-threaded, `cargo build --release` (no LTO), store fully resident in L3 after the 30-call warmup.
Comparing `query()` (new path) vs `query_linear()` (pre-optimization path with identical `narrow_candidates` narrowing), `unique=card`, `prefer=default`, 5s timed window.
Numbers are means over all calls in the timed window; σ/mean was under 2% for all rows except `t:merfolk o:draw` (8%), where the ~20-card candidate set produces high relative variance.

| query | new (µs) | old (µs) | speedup | notes |
|---|---|---|---|---|
| `format:legacy` | 330 | 1252 | 3.8x | no index hit — preferred 31k vs full 97k |
| `format:modern` | 388 | 1101 | 2.8x | same |
| `c:r` | 240 | 511 | 2.1x | no index hit |
| `t:creature` | 348 | 674 | 1.9x | type_bits narrows; preferred intersect reduces further |
| `o:flying` | 361 | 510 | 1.4x | oracle trigram + preferred intersection |
| `cmc>5` | 174 | 232 | 1.3x | cmc sorted array + preferred intersection |
| `t:merfolk o:draw` | 148 | 141 | 0.95x | narrow_candidates already returns ~20 cards |

Geometric mean across the seven card-level queries: **1.87x**.
Printing-level queries (`a:terese`, `year>2023`) are unaffected — the fast path does not fire and timings are within noise.

## Where It Does Not Help

The `t:merfolk o:draw` row shows a slight regression.
When `narrow_candidates` already returns a tiny set — here the type_bits and oracle trigram intersection produces roughly 20 cards — calling `intersect_sorted` against all 31k preferred entries costs more than eliminating the dedup of 20 items saves.
The mechanism: `intersect_sorted` is a merge of two sorted lists, so it must scan through O(31k) preferred entries to find 20 matches, whereas the dedup it replaces was O(20).
The crossover is at small candidate counts where dedup overhead was negligible to begin with.

This could be addressed with a minimum-candidate-count guard: skip the preferred intersection when the narrowed set is already below some threshold.
Empirically, the crossover is around 50–80 candidates — below that, the O(31k) merge costs more than the dedup it replaces.
The regression is small enough (5%) and the query type rare enough in practice that the added code complexity has not been worth addressing yet.

## Largest Impact Where It Matters Most

Format and color queries — `format:modern`, `format:legacy`, `c:r` — have no applicable index, so they previously scanned all 97k printings every time.
These are also some of the most common queries: format filtering is the first refinement most users apply.
They see the largest speedup (2–4x) from this change, and they were already among the slowest queries in the engine.
The optimization is most effective exactly where it is most needed.

## Related

The preferred-printing index itself is described in [Four Algorithms for a Random Card Endpoint](../00896_random-card-sampling/).
The index structures that `narrow_candidates` uses — trigrams, sorted arrays, hash maps — are covered in [In-Process Card Search Without a Query Planner](../00704_rust-engine-index-data-structures/).
The linear-scan dedup strategy that this optimization replaces (for qualifying queries) is in [Adaptive Dedup: Linear Scan Wins Small Sets, Hash Wins Large](../00832_linear-hash-scan-distinct/).
