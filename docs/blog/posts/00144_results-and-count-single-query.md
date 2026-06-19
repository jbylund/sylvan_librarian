---
title: "Counting Search Results Without a Second Query (and Why It Depends)"
date: 2026-08-15
publishDate: 2026-08-15
tags: ["arcane-tutor", "sql", "postgres"]
summary: "A search endpoint needs paginated results and a total count. A CTE with UNION ALL collapses them into one query. For deduplicated searches it cuts query time roughly in half by running the dedup once instead of twice."
---

A search for `format:modern` matches 73,336 printings, representing 21,994 unique cards. The client needs the top 100 by popularity and a count for the pagination UI. That is two questions, and the obvious implementation answers them with two queries.

Most searches deduplicate by `oracle_id` — one printing per unique card — because users care about cards, not every printing of each card. That deduplication is the key to understanding why a CTE with `UNION ALL` almost always beats two separate queries.

## The Naive Two-Query Approach

Both queries need to scan all matching rows and sort by `oracle_id` — the results query to deduplicate with `DISTINCT ON`, the count query to compute `COUNT(DISTINCT oracle_id)`. For `format:modern`, that is sorting 73,336 rows twice:

```sql
-- Query 1: results
SELECT card_name, card_set_code, edhrec_rank, prefer_score
FROM (
    SELECT DISTINCT ON (oracle_id)
        card_name, card_set_code, edhrec_rank, prefer_score,
        edhrec_rank AS sort_value
    FROM magic.cards AS card
    WHERE (card.card_legalities @> '{"modern": "legal"}'::jsonb)
    ORDER BY oracle_id, prefer_score DESC NULLS LAST
) t
ORDER BY sort_value ASC NULLS LAST, edhrec_rank ASC NULLS LAST LIMIT 100;

-- Query 2: count
SELECT COUNT(1) FROM (
    SELECT oracle_id FROM magic.cards AS card
    WHERE (card.card_legalities @> '{"modern": "legal"}'::jsonb)
    GROUP BY oracle_id
) t;
```

Both plans share the same expensive core — a Bitmap Heap Scan feeding a sort over 73,336 rows:

```
-- Results: 75.7ms
Sort (Sort Key: oracle_id)  Memory: 7943kB
  -> Bitmap Heap Scan  rows=73336
       -> Bitmap Index Scan on idx_cards_legalities

-- Count: 56.3ms
HashAggregate (Group Key: oracle_id)  Memory: 2073kB
  -> Bitmap Heap Scan  rows=73336
       -> Bitmap Index Scan on idx_cards_legalities
```

Total: ~132ms across two round trips. One aside: `COUNT(DISTINCT oracle_id)` produces the same result but always uses Sort + Aggregate (60ms); the `GROUP BY` form lets PostgreSQL choose HashAggregate and avoids the sort. The planner does not rewrite one to the other automatically.

## One Query for Both

A CTE with `UNION ALL` runs the deduplication once and materializes the result:

```sql
WITH distinct_cards AS (
    SELECT DISTINCT ON (oracle_id)
        card_name, card_set_code, edhrec_rank, prefer_score,
        edhrec_rank AS sort_value
    FROM magic.cards AS card
    WHERE (card.card_legalities @> '{"modern": "legal"}'::jsonb)
    ORDER BY oracle_id, prefer_score DESC NULLS LAST
)
(
    SELECT null::integer AS total_cards_count, card_name, card_set_code, edhrec_rank, prefer_score
    FROM distinct_cards
    ORDER BY sort_value ASC NULLS LAST, edhrec_rank ASC NULLS LAST, prefer_score DESC NULLS LAST
    LIMIT 100
)
UNION ALL
(
    SELECT COUNT(1) AS total_cards_count, null, null, null, null
    FROM distinct_cards
);
```

The `null::integer` cast gives both branches the same column shape so `UNION ALL` can combine them. The caller reads the last row for the count and the first 100 rows for results.

For `format:modern`, the plan:

```
CTE distinct_cards
  -> Sort (oracle_id)  Memory: 7943kB
       -> Bitmap Heap Scan  rows=73336  (actual: 21994 distinct)
          Storage: Memory  Maximum Storage: 1919kB   ← materialized once
-> LIMIT branch: top-N sort over 21994 rows, 77ms total
-> COUNT branch: 1.4ms (scan over materialized result)
Execution Time: 77.1ms
```

For `power>4` (6,750 raw matches, 2,175 distinct):

```
CTE distinct_cards
  -> Sort (oracle_id)  Memory: 657kB
       -> Bitmap Heap Scan  rows=6750  (actual: 2175 distinct)
          Storage: Memory  Maximum Storage: 199kB
-> LIMIT branch + COUNT: Execution Time: 11.6ms
```

| Query | Raw matches | Unique cards | Two queries | CTE | Winner |
|---|---|---|---|---|---|
| `format:modern` | 73,336 | 21,994 | 132ms (76ms top-n, 56ms count) | 77ms | CTE |
| `power>4` | 6,750 | 2,175 | 24ms (14ms top-n, 10ms count) | 12ms | CTE |

The deduplication sort dominates the cost. Running it once instead of twice is the source of the performance improvement.

## What Changes for Non-Deduplicated Searches

When searching by printing rather than unique card, there is no `DISTINCT ON`. The two branches have different optimal access paths:

- The LIMIT branch wants to walk the `edhrec_rank` B-tree index in order and stop as soon as it has 100 rows.
- The COUNT branch wants to use a condition index (`creature_power` or `card_legalities`) to count all matches directly.

A default (materialized) CTE forces the planner to choose a single access strategy for the whole CTE, then serve both branches from the result. For `format:modern`, that means materializing all 73,336 matching rows before either branch can run — 70ms, versus 49ms for two separate queries that can each pick their own index.

The non-dedup path uses `WITH matching_cards AS NOT MATERIALIZED`, which tells PostgreSQL it is free to inline the CTE definition into each branch and plan them independently:

```sql
WITH matching_cards AS NOT MATERIALIZED (
    SELECT ... FROM magic.cards WHERE ...
)
(SELECT null::integer, ... FROM matching_cards ORDER BY ... LIMIT 100)
UNION ALL
(SELECT COUNT(1), null, ... FROM matching_cards)
```

With `NOT MATERIALIZED`, the `format:modern` plan uses the `edhrec_rank` index for the LIMIT branch (examines ~235 rows, 1ms) and the GIN legalities index for the COUNT branch (73,336 rows, 44ms) — 46ms total in a single round trip:

```
-> LIMIT branch: Index Scan using idx_cards_edhrec_rank_btree
     Filter: (card_legalities @> '{"modern": "legal"}')
     Rows Removed by Filter: 128
     Execution: ~1ms
-> COUNT branch: Bitmap Heap Scan on idx_cards_legalities
     rows=73336, Execution: ~44ms
Total Execution Time: 45.6ms
```

| Query | Two queries | Materialized CTE | NOT MATERIALIZED |
|---|---|---|---|
| `format:modern` | 46ms (1ms top-n, 45ms count) | 70ms | 46ms |
| `power>4` | 11ms (10ms top-n, 1ms count) | 14ms | 9ms |

The split timings reveal an asymmetry worth noting. `format:modern` top-n takes 1ms because 76% of cards are modern-legal — walking the `edhrec_rank` index in order, the planner finds 100 matches after examining only ~235 rows and exits early. `power>4` count takes 1ms for the opposite reason — `idx_cards_creature_power_btree` is a covering index for that query, so the planner counts 6,750 entries without touching the heap at all.

A broad filter is cheap for top-n (high match rate means early exit) and expensive for count (still has to scan all matches). A selective filter is cheap for count (tight index scan, no heap fetch) and expensive for top-n (has to walk deep into the sort index before accumulating enough matches). The two queries in the two-query approach each exploit the cheap side of this tradeoff. `NOT MATERIALIZED` lets the planner do the same within a single statement.

## Why It Exists

The dedup speedup is the reason. Most searches deduplicate by `oracle_id`, and the CTE cuts those query times roughly in half — 132ms to 77ms for a broad query, 24ms to 12ms for a selective one. The non-dedup path is essentially a wash in both directions; `NOT MATERIALIZED` keeps it competitive with two separate queries without giving up the single round trip.
