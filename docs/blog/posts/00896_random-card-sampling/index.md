---
title: "Four Algorithms for a Random Card Endpoint"
date: 2027-07-14
publishDate: 2027-07-14
tags: ["rust", "performance", "memory", "algorithms"]
summary: "The /random endpoint used to keep a full copy of ~30k cards as Python dicts in every worker. Replacing it meant evaluating four algorithms in Rust, converging on a pre-built preferred-printing index stored in the shared mmap — O(n) time, near-zero per-worker memory, and within 4–6× of the original Python speed."
---

## The Memory Problem

The `/random` endpoint returned one or more randomly selected cards — one per unique oracle ID, so repeated printings of the same card do not crowd the results.
The original implementation kept this pool warm in every worker process:

```python
def _fetch_and_cache_preferred_cards(self, gen: int) -> None:
    cards = self._search(query="", limit=None)["cards"]
    self._preferred_cards_map[gen] = cards
```

`_search(query="", limit=None)` asks the engine for every card with no filter and no limit — all ~30k unique cards — and converts them all to Python dicts.
With four workers each holding a copy, that is ~120k Python dicts sitting in heap memory at all times.
The stale-while-revalidate logic around it (background threads, a generation counter, a lock) existed entirely to keep this cache from going stale after a card import.
Eliminating the cache meant eliminating all of that.

The engine stores ~97k printings in a memory-mapped file, sorted by `(oracle_id, illustration_id)`.
The random endpoint only needed `n` of the ~30k unique cards — the question was how to pick `n` without materializing all of them.

## Four Approaches

**Fisher-Yates over a full dedup pool.** Walk all printings, collect one card per oracle ID group into `Vec<&ACard>` (~30k pointers, ~240KB), then run a partial Fisher-Yates shuffle to pick `n` cards from the front.
Memory: O(unique_cards).
Time: O(total_printings) for the dedup walk + O(n) for the shuffle.

**Reservoir sampling (bounded min-heap).** Walk all printings.
At each oracle ID boundary, generate a random u64 key for that group and maintain a `BinaryHeap<Reverse<(u64, usize)>>` of size `n`.
When a new key beats the heap minimum, evict the minimum and push the newcomer.
At the end, convert the `n` heap entries to Python dicts.
Memory: O(n).
Time: O(total_printings × log n).

**Pre-selected indices with a targeted walk.** The unique card count is stored in the archive at reload time (computed once after the sort, at no extra cost).
Floyd's algorithm picks `n` distinct integers from `[0, unique_count)` in O(n) time using a HashSet of size `n`.
A single walk over the printings counts oracle ID boundaries and only tracks the preferred printing for groups whose index is in the HashSet.
Memory: O(n).
Time: O(total_printings) + O(n) for Floyd's.

**Preferred-printing index.** The archive stores a pre-built `Vec<u32>` of one index per unique card — whichever printing has the highest `prefer_score` — computed in a single pass at reload time.
(`prefer_score` is a composite float computed at import time: English-language printings score highest, followed by frame version, border color, rarity, highres scan, non-showcase finish, and artwork popularity.
A nonfoil black-bordered 2015-frame English rare is the canonical "best" printing of a card.)
A simple loop picks `n` positions from this index.
No walk over 97k printings at query time at all.
Memory: ~124 KB shared in the mmap (zero per-worker allocation).
Time: O(n).

At query time, the sampler draws `n` positions from the pool:

```rust
use rand::RngExt;
let mut rng: rand::rngs::SmallRng = rand::make_rng();
let mut chosen = HashSet::with_capacity(take);
while chosen.len() < take {
    chosen.insert(rng.random::<u64>() as usize % pool_len);
}
```

This does not guarantee distinct results — if the same index is drawn twice the loop iterates once more.
At n=60 against a pool of 31,508 cards the birthday-problem probability of any collision is about 0.06%, so in practice this never fires.

## Benchmark Results

The old hot path — `random.sample` on the prebuilt in-memory list — is the baseline to beat.
The prebuilt list holds 31,508 unique cards and occupies ~38.6 MB of heap per worker.
Measured on an Apple M3 Max, single worker process, mmap pre-warmed by one prior call (50 warmup calls, 3×3s timed windows, best-of-3):

| n | Python random.sample (µs) | Preferred index (µs) | Reservoir (µs) | Indexed (µs) |
|---|---|---|---|---|
| 1 | 0.4 | 1.2 | 566 | 482 |
| 12 | 1.5 | 7.5 | 553 | 593 |
| 30 | 3.2 | 17 | 578 | 526 |
| 60 | 5.6 | 32 | 602 | 563 |

The reservoir and indexed approaches walk all 97k printings on every call regardless of `n` — that is where the ~550µs floor comes from.
The preferred-index approach skips the walk entirely.
It is 30–400× faster than the other Rust methods and within 4–6× of the Python baseline.
The remaining gap versus Python is exactly the cost of constructing `n` new Python dicts; the Python baseline returned pre-built objects from the cache, so it paid that cost at load time rather than per call.

The memory tradeoff: 38.6 MB per worker freed.
Four workers means ~154 MB of Python dicts that previously had to be kept warm across card imports, requiring a stale-while-revalidate mechanism (generation counter, background threads, per-worker locks).
The preferred-index approach stores ~124 KB of `u32` values in the shared mmap — the same file pages all workers already have mapped.

## How the Preferred-Index Approach Works

At reload time, after the card store is sorted by `(oracle_id, illustration_id)`, a single linear pass collects one index per unique oracle_id group — whichever printing has the highest `prefer_score`:

```rust
for (i, card) in cards.iter().enumerate() {
    if prev_oracle != Some(card.oracle_id) {
        if let Some((best_idx, _)) = group_best.take() {
            preferred_indices.push(best_idx);
        }
        prev_oracle = Some(card.oracle_id);
        group_best = Some((i as u32, card.prefer_score.unwrap_or(0.0)));
    } else {
        let score = card.prefer_score.unwrap_or(0.0);
        if score > group_best.as_ref().map_or(f32::NEG_INFINITY, |g| g.1) {
            group_best = Some((i as u32, score));
        }
    }
}
```

`preferred_indices` becomes part of the rkyv archive.
Every worker reads from the same mapped file pages — there is no per-worker copy.
At query time, the loop picks `n` positions from `[0, preferred_indices.len())`, then `card_to_pydict` converts just those `n` cards.

## Decision

`sample_preferred` is the production path, shipped in [PR #535](https://github.com/jbylund/arcane_tutor/pull/535).
The reservoir and indexed implementations were removed after benchmarking — the full Rust source for all four approaches is preserved at [345b62d](https://github.com/jbylund/arcane_tutor/blob/345b62d/card_engine/src/lib.rs) for reference.

The preferred-index approach eliminates the original memory problem (38.6 MB freed per worker, stale-while-revalidate machinery deleted, `import random` removed from the API module) while recovering nearly all of the Python baseline's per-call speed.
The 4–6× per-call regression versus Python is the cost of building `n` Python dicts on the fly rather than returning pre-built ones; for an endpoint that returns at most 60 cards and is not in any hot loop, 32µs vs 5.6µs is an acceptable tradeoff.
Building the preferred-printing index at reload time adds one linear pass over ~97k cards — negligible alongside the existing sort — and the index is available to all workers the moment the reload completes.
The staleness boundary moved from between requests to between reloads, not eliminated entirely.
