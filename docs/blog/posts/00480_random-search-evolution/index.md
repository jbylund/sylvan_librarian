---
title: "Stop Using `ORDER BY RANDOM()`: It Scans the Whole Table"
date: 2027-01-16
publishDate: 2027-01-16
tags: ["postgres", "python", "performance", "caching"]
summary: "The /random_search endpoint ran two full-table queries on every request. A 10-minute TTL cache materialized the 30k preferred-printing list once; individual requests then did an in-memory sample. Zero DB round-trips on the hot path."
---

The `/random_search` endpoint had a `TODO` comment in the code from the day it was written:

```python
# TODO: how to keep this query in sync with the larger search query?
```

That comment was a sign the implementation was always going to scan the table.

The original endpoint ([PR #354](https://github.com/jbylund/sylvan_librarian/pull/354)) ran a full-table scan on every request.
`ORDER BY RANDOM()` is not a random sampler — it is a sort.
PostgreSQL assigns a random float to every row, then sorts the entire result set.
There is no shortcut.
The planner cannot use any index for this, because the sort key is computed fresh for each execution.
On a table with ~97k card printings, that meant reading and sorting every row, every time someone loaded the homepage.

## Why `ORDER BY RANDOM()` Cannot Be Fast

A B-tree index works because the sort key is a stable property of each row.
PostgreSQL can seek to a position in the index, read a few leaf pages, and stop.
With `RANDOM()`, the sort key is different on every execution — there is no index structure that can hold it, because the values do not exist until the query runs.
The planner's only option is a sequential scan.

Here is what the pre-PR #453 `random_search` actually ran on each request ([source, commit 7c2718a](https://github.com/jbylund/sylvan_librarian/blob/7c2718a75c445eb3b263d0cc1c21f6a14c4f7470/api/api_resource.py#L2497)):

```sql
WITH cte1 AS (
    SELECT DISTINCT ON (card_name)
        scryfall_id
    FROM
        magic.cards
    ORDER BY
        card_name,
        prefer_score DESC NULLS LAST,
        scryfall_id
),
cte2 AS (
    SELECT scryfall_id
    FROM cte1
    WHERE RANDOM() < %(card_sample_rate)s
    ORDER BY RANDOM()
    LIMIT %(num_cards)s
)
SELECT card_artist, card_name AS name, ...
FROM cte2 JOIN magic.cards ON cte2.scryfall_id = magic.cards.scryfall_id
```

This query did two full scans on every call.
The first CTE (`cte1`) read every row in `magic.cards` to build the deduplicated preferred-printing list — `DISTINCT ON` with an `ORDER BY` on a non-indexed key is a sort, not a seek.
The second CTE (`cte2`) called `RANDOM()` twice: once as a filter (`WHERE RANDOM() < 0.01`) trying to keep the sample small, and once as the sort key (`ORDER BY RANDOM()`).
The fallback loop retried at a 100% sample rate when the 1% filter came up empty.
Uncached, O(N) in the number of printings, on every request.

The initial implementation before that ([PR #354](https://github.com/jbylund/sylvan_librarian/pull/354)) was even more ad hoc — it generated a random UUID and used `scryfall_id >= random_uuid` to seek to a random position in the primary-key B-tree, hoping to find something nearby.
It worked but was biased toward lower UUIDs and did not deduplicate card names.

## The Dead End That Confirmed the Problem

A natural thought: could `TABLESAMPLE` help?
PostgreSQL has `TABLESAMPLE SYSTEM(p)` and `TABLESAMPLE BERNOULLI(p)` for sampling without a full sort.
`SYSTEM` works at the block level and is fast; `BERNOULLI` checks every row but does not sort.
Neither produces a uniform sample across the deduplicated preferred-printing view — they sample from raw printings, not from the one-per-card-name list the endpoint needed.
They would still have required materializing `cte1` first, then sampling from it.
The `ORDER BY RANDOM()` inside the CTE would still be there.

## The Fix: Cache the Materialized List

[PR #453](https://github.com/jbylund/sylvan_librarian/pull/453) replaced the per-request query with a TTL-cached method:

```python
@cached(cache=TTLCache(maxsize=1, ttl=600))
def _get_all_preferred_cards(self) -> list[dict[str, Any]]:
    """Return all preferred printings (one per card name), cached for 10 minutes."""
    search_method = getattr(self._search, "__wrapped__", self._search)
    return search_method(self, query="", limit=None)["cards"]

def random_search(self, *, num_cards: int = 1, **_: object) -> dict[str, Any]:
    num_cards = min(max(num_cards, 1), 1000)
    all_cards = self._get_all_preferred_cards()
    cards = random.sample(all_cards, min(num_cards, len(all_cards)))
    return {"cards": cards, "total_cards": len(cards)}
```

([source](https://github.com/jbylund/sylvan_librarian/blob/ae4e01046ed978c4e5cabe1c9fc439b0057c45fa/api/api_resource.py#L2486-L2508))

The cold path is the same cost as before — one full-table query.
On this hardware (~97k rows, local Docker Compose, M3 Pro) the cold call took roughly 200 ms wall-clock, measured as a single timed call wrapping the full query round-trip.

The warm path, which is every request within the 10-minute TTL window, is `random.sample()` against an in-memory list.
No DB round-trip, no sequential scan, no sort.
`random.sample()` without replacement across a list of 30k Python dicts is fast: measured with `timeit` (CPython 3.13, macOS arm64, 10k iterations, stable across three runs):

| n (cards returned) | median per call |
|---|---|
| 1 | 0.4 µs |
| 10 | 1.6 µs |
| 30 | 4.0 µs |

The warm path is roughly five orders of magnitude faster than the cold path for n=10 — the homepage default.

`_get_all_preferred_cards` calls `self._search.__wrapped__` instead of `self._search`.
The `_search` method has its own per-query TTL cache (maxsize=1000, 60-second TTL, keyed by query parameters).
Passing `query="", limit=None` through that cache would store all 30k cards under a single cache key and evict other entries — the cache is sized for short result sets, not a full-table export.
The `__wrapped__` attribute on a `@cached` function points to the underlying function before decoration.
Calling it directly bypasses the `_search` cache while still running the same SQL path that produces the correct response shape.
This is what the `TODO` comment was waiting for: the two code paths now share the same query, so they cannot diverge.

Cache invalidation was handled by adding `_get_all_preferred_cards.cache` to the `_clear_caches()` helper that already cleared `_query_cache` and `_search.cache`.
Any successful card import clears all three.

## What This Cannot Guarantee

The TTL is 10 minutes.
A card import that adds or changes the preferred printing of a card will not be reflected in `/random_search` until the next cold request after the TTL expires.
For a search engine where card data updates at most once or twice a day, this is an acceptable window.
For a system where fresh card data is latency-sensitive, it is not.

The in-memory list also lives in each worker process independently.
With four Bjoern workers, that is four copies of ~30k card dicts — none shared.
`tracemalloc` later measured ~38.6 MB of heap per worker for this cache, or ~154 MB total.
That is the cost this post trades for the performance gain: no more sequential scans, but substantial per-worker memory that has to stay coherent across card imports.

There is also a thundering-herd case worth naming: when the TTL expires under concurrent traffic, multiple workers can simultaneously miss the cache and each fire the full-table scan.
There is no lock or leader election — every worker that hits a cold cache runs the query independently.
On a lightly-trafficked endpoint where daily updates drive most reloads, this is tolerable.
On an endpoint under steady high concurrency it is not.

The follow-on post ([Four Algorithms for a Random Card Endpoint](../00896_random-card-sampling/)) covers how moving sampling into the Rust engine eliminated the per-worker copies entirely.

For a homepage that loads ten cards at a time and updates its data source once daily, amortizing a 200 ms database query across 600 requests is a better deal than running it 600 times.
