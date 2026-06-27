---
title: "How Sort Order Makes HashMap Dedup Unnecessary"
date: 2027-07-03
publishDate: 2027-07-03
tags: ["rust", "performance", "query"]
summary: "To deduplicate 97k printings down to one result per oracle ID, the engine uses a linear key-change scan instead of a HashMap — because the card store is sorted by oracle_id at build time, making equal keys always adjacent. No threshold needed; the invariant guarantees correctness."
---

Before writing this post, the plan called for a crossover chart: at what result-set size does a hash map beat a linear scan for `unique=card` dedup? The chart never got made, because the research showed there is no crossover. The sort order baked into the card store at build time makes the linear scan correct on all inputs and eliminates the allocation cost that would otherwise make the hash map competitive at large scales.

The Rust engine stores one row per printing — roughly 97,000 of them. A `unique=card` query needs to return one result per unique card (the preferred printing for each oracle ID), reducing those 97k candidates to ~31k. Deduplication has to happen somewhere, and the choice of how to do it is the subject of this post.

## A Sort Order That Makes the Problem Easier

At build time, `reload_commit` sorts the entire card store:

```rust
// https://github.com/jbylund/arcane_tutor/blob/f3e11f8/card_engine/src/lib.rs#L1469
cards.sort_unstable_by_key(|c| (c.oracle_id, c.illustration_id));
```

This one line is what makes linear dedup possible. After the sort, all printings of the same oracle ID are contiguous. A group change is detectable with a single `u128` comparison against the previous key. No hash function, no bucket lookup, no resize.

The linear scan walks the sorted store and tracks the group boundary:

```rust
// Simplified — full implementation at:
// https://github.com/jbylund/arcane_tutor/blob/f3e11f8/card_engine/src/lib.rs#L1061-L1102

let mut prev_key: Option<u128> = None;
let mut group_best: Option<(&ACard, f64)> = None;

for card in cards {
    if !filter.matches(card, strings) { continue; }
    let key = key_fn(card); // oracle_id as u128
    if prev_key != Some(key) {
        if let Some((c, _)) = group_best.take() {
            best.push((sort_key_bits(c, ...), c));
        }
        prev_key = Some(key);
        group_best = Some((card, prefer_score(card, prefer)));
    } else {
        let score = prefer_score(card, prefer);
        if score > group_best.as_ref().map_or(f64::NEG_INFINITY, |g| g.1) {
            group_best = Some((card, score));
        }
    }
}
if let Some((c, _)) = group_best { best.push(...); }
```

The hash map approach — `run_query_hashmap` — is also in the codebase. It takes a `HashMap<u128, (&ACard, f64)>`, inserts each matching printing under its oracle ID, and keeps the best prefer score per entry. It works regardless of order and is the fallback for any `unique` value the engine does not recognize. For the three modes that Scryfall users actually invoke (`card`, `artwork`, `printing`), it is never on the hot path.

The store holds roughly 97,000 cards. `sort_unstable_by_key` on 97k elements with a `(u128, u128)` key completes in single-digit milliseconds on a modern machine — fast enough that it contributes nothing notable to `reload_commit` time, which is dominated by rkyv serialization and the mmap rename. The sort runs once at startup and once per bulk import; queries never pay it.

The key-comparison type evolved once: when the engine first shipped in [PR #490](https://github.com/jbylund/arcane_tutor/pull/490), the linear scan used a `u32` dense group ID (`oracle_group`) computed in a single post-sort pass, with `u32::MAX` as a sentinel. When [PR #502](https://github.com/jbylund/arcane_tutor/pull/502) switched to rkyv and a shared mmap, the dense IDs were dropped in favor of the `oracle_id: u128` that was already in the struct. The current code comments: "u128 equality, same cost as the dense u32 group ids this replaced." The sort invariant did not change.

## Why the Failure Mode Matters

If the sort invariant were violated — say, a future caller passed an unsorted iterator to `run_query_linear` — the scan would produce silently wrong output. A printing from oracle group A could appear as the tail of oracle group B, causing it to be emitted as a separate result instead of being merged with its group. No panic, no error, just incorrect results.

The invariant is enforced structurally: `reload_commit` is the only path that writes the card store, and the sort is unconditional there. The one place where the sort order could be disrupted is the oracle text trigram index: `expand_text_ids` walks a CSR table in text-ID order rather than store order, so its output is not sorted. The function calls `out.sort_unstable()` before returning, restoring the invariant before the linear scan ever sees it.

## Why This Also Works for Artwork Dedup

`unique=artwork` uses the same linear scan with `illustration_id` as the key. Scryfall assigns each `illustration_id` to exactly one `oracle_id`. Because the store is sorted by `(oracle_id, illustration_id)`, all printings sharing an illustration cluster within their oracle group. Equal illustration IDs are always contiguous. The linear scan is correct here for the same reason, and relies on the same external guarantee: that Scryfall's data schema holds.

## What the Numbers Show (and What Was Not Measured)

The `query_hashmap()` method exists on `QueryEngine` specifically to force the hash map path for benchmarking, but no direct linear-vs-hash comparison has been run and stored in the repo. What is available from [PR #540](https://github.com/jbylund/arcane_tutor/pull/540), which benchmarks the linear path directly via `query_linear()`, is the cost of scanning 97k printings with linear dedup: `format:legacy` takes 1,252 µs, `format:modern` takes 1,101 µs (30-call warmup, 5s timed window, M-series chip). The hash map path would add HashMap allocation and probe overhead on top of the same scan — the cost depends on the load factor and hash function, but allocation for ~31k entries is not free.

The preferred-printing index (PR #540) cuts these numbers to 330 µs and 388 µs respectively by eliminating dedup entirely for card-level filters with default prefer. For those queries, the linear-vs-hash comparison is no longer relevant.

## Related

All three dedup paths produce the same output shape — a `Vec<(u128 sort_key, &ACard)>` — so the two-pivot quickselect in [Paginating 30k Cards Without Sorting All of Them](00800_two-pivot-pagination.md) operates on sort keys without knowing which path produced them.

The same deduplication problem at the SQL layer — `DISTINCT ON` key choice, hashagg vs. sort, and a no-op primary-key dedup — is covered in [Oracle ID Dedup: 23% Faster by Changing the Key](00416_oracle-id-deduplication.md). The preferred-printing index that eliminates dedup for the most common query pattern is in [The /random Index Pays Off Twice](00928_preferred-index-card-search.md).
