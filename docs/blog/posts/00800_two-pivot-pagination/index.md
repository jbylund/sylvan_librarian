---
title: "Paginating 30k Cards Without Sorting All of Them"
date: 2027-06-19
publishDate: 2027-06-19
tags: ["rust", "performance", "pagination"]
summary: "Instead of sorting all matching cards and paginating, two quickselects identify the score boundary of the requested page; only those cards are fully sorted. O(n) scan, O(page) sort."
---

The first draft of the Rust card engine sorted every match before returning any results.

Sorting is the natural thing to reach for.
You have a vector of matched cards, each with a numeric sort key.
You call `sort_by`, truncate to `limit`, done.
On the initial Rust engine prototype, that looked like:

```rust
// Early draft (commit 8fc753e) — sort all matches, then truncate
best.sort_by(|a, b| {
    sort_key(a, sort_col, descending)
        .partial_cmp(&sort_key(b, sort_col, descending))
        .unwrap_or(std::cmp::Ordering::Equal)
});
best.truncate(limit);
```

For 30,000 matching cards and a page of 100, that is O(30,000 × log 30,000) ≈ 450,000 comparisons to produce 100 results.
The information you actually need is the boundary score that separates your 100 results from the other 29,900.
Everything past that boundary can stay unsorted.
The final version of PR [#490](https://github.com/jbylund/sylvan_librarian/pull/490) replaced the full sort with a two-pivot selection.

## One Integer Per Card

Before getting to the selection algorithm, there is a design choice that makes the comparisons cheap: compute each card's sort key once, upfront, as a single `u128` integer.

The sort has three tiers: primary column (edhrec rank, cmc, power, rarity, price — configurable), then edhrec rank as a secondary, then prefer score as a tiebreaker.
Each tier is a 32-bit float packed into order-preserving bits, and the three 32-bit values are concatenated into 128 bits:

```rust
// Map an f32 to a u32 that orders like f32::total_cmp.
// IEEE 754 positive floats are already lexicographically ordered by their bit
// patterns, so setting the sign bit places them above all negatives in unsigned
// integer order. Negative floats are not naturally ordered that way — their
// magnitude is encoded in the low bits — so flipping all bits reverses them
// into ascending order.
fn f32_sort_bits(v: f32) -> u32 {
    let b = v.to_bits();
    if b & (1 << 31) != 0 { !b } else { b | (1 << 31) }
}

// Simplified from card_engine/src/lib.rs
fn sort_key_bits(card: &ACard, sort_col: SortCol, descending: bool) -> u128 {
    let p = primary.map_or(u32::MAX, |v| f32_sort_bits(if descending { -v } else { v }));
    let e = card.edhrec_rank.unwrap_or(u32::MAX);    // missing sorts last
    let s = card.prefer_score.map_or(u32::MAX, ...); // missing sorts last
    ((p as u128) << 64) | ((e as u128) << 32) | (s as u128)
}
```

With that in place, comparing two cards is one `u128::cmp` — a single 128-bit integer comparison that covers all three levels of precedence.

([Full implementation](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L966-L990))

## Finding the Page Boundary With Two Quickselects

With each card represented as `(u128, &Card)`, selecting a page `[offset, offset+limit)` from an unsorted vector is a job for quickselect — specifically Rust's `slice::select_nth_unstable_by`, which implements the Floyd-Rivest algorithm and runs in O(n) expected time.

The algorithm needs at most two calls:

1. **Upper pivot**: partition the vector so that everything at index `end` or beyond is at least as large as the page's maximum. Everything past `end` can be ignored.
2. **Lower pivot**: partition `v[..end]` so that everything at index `offset` or beyond belongs to the page or later. This is skipped when `offset == 0`, which is the common case — page one of a search result.

Then sort only the `limit`-size segment `v[offset..end]`:

```rust
fn select_page<'a>(mut v: Vec<(u128, &'a ACard)>, offset: usize, limit: usize) -> Vec<&'a ACard> {
    let end = offset.saturating_add(limit).min(v.len());
    if offset >= end { return Vec::new(); }

    let cmp = |a: &(u128, &ACard), b: &(u128, &ACard)| {
        a.0.cmp(&b.0).then_with(|| std::ptr::from_ref(a.1).cmp(&std::ptr::from_ref(b.1)))
    };

    if end < v.len() { v.select_nth_unstable_by(end, cmp); }   // pivot 1: upper bound
    if offset > 0    { v[..end].select_nth_unstable_by(offset, cmp); }  // pivot 2: lower bound
    v[offset..end].sort_unstable_by(cmp);
    v.drain(..offset);
    v.into_iter().map(|(_, c)| c).collect()
}
```

([Full implementation](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L996-L1014))

The total work is O(n) for the selection passes plus O(limit × log limit) for sorting the final segment — versus O(n × log n) for a full sort.
For page one of a broad query with 30,000 matches and limit 100, that is roughly 30,000 expected comparisons in the selection pass, plus 700 to sort the page, versus 450,000 for a full sort.
For page one specifically the lower pivot is skipped entirely, since there is nothing below the window to exclude.

## Tie-Breaking and Stability

There is a subtle problem with `sort_unstable_by` on a composite key: two cards with identical `u128` keys would sort in an arbitrary, potentially nondeterministic order.
The comparison function handles this with a pointer tiebreaker:

```rust
let cmp = |a: &(u128, &ACard), b: &(u128, &ACard)| {
    a.0.cmp(&b.0).then_with(|| std::ptr::from_ref(a.1).cmp(&std::ptr::from_ref(b.1)))
};
```

Cards are stored in a fixed mmap-backed array that never moves between calls.
Pointer order within that array is deterministic — it matches store order, which is sorted by `(oracle_id, illustration_id)` at load time.
The comment in the source notes this: "full ties fall back to store pointer order in `select_page` — the same tie order the original stable sort produced."
Ties resolve consistently across pages.

This is not just theoretical.
With 30,000 cards and an edhrec-rank ordering, the thousands of unranked cards (missing edhrec data) all get `u32::MAX` for the secondary key.
The pointer tiebreaker keeps them in a stable order rather than scrambling them on every request.

## What It Did Not Fix

The selection optimization applies after filtering.
For broad queries like `format:legacy` — which match roughly 31,000 cards — the engine still scans all ~97,000 printings to determine which ones pass the filter.
PR [#540](https://github.com/jbylund/sylvan_librarian/pull/540) addressed that separately, using a prebuilt preferred-printing index to cut the scan to ~31,000 entries for the common `unique=card` case.

The selection also does not help when the candidate set is already small.
A query like `t:merfolk o:draw` narrows to roughly 20 cards via index intersection before the filter loop runs; at that scale, sorting 20 elements takes the same ~1 µs as a quickselect over 20 elements.

## The Result

The Rust engine, benchmarked across eleven representative queries against 96,139 cards on a MacBook Pro (Apple M-series, dev Docker Compose stack, queries issued from Python inside the API container), ran at a geometric mean of 0.20 ms per query — versus 14.9 ms for the PostgreSQL path, a 76x speedup.
Each query used 20 warmup iterations before a 3-second timed window, `unique=card`, `limit=100`.
The sort reduction is one of three techniques layered in PR #490 alongside precomputed integer sort keys and candidate narrowing via prebuilt indexes; the benchmark does not isolate the sort contribution alone.

What the partial-sort buys over a naive full sort is proportional to how many cards match.
For a broad query like `format:legacy` that returns ~31,000 results, the upper pivot pass takes ~31,000 comparisons and the final page sort takes ~700 — about 31,700 total, versus ~450,000 for a full sort of the same set.
That is roughly 14x fewer comparisons for the sort step, before any of the other engine optimizations enter the picture.

For a search UI that will render 100 cards, sorting everything else is just paying for work no user will see.
