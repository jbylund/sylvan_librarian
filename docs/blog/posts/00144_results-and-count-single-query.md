---
title: "One Query, Two Answers: How NOT MATERIALIZED Lets Each Branch Pick Its Own Index"
date: 2026-08-15
publishDate: 2026-08-15
tags: ["arcane-tutor", "sql", "postgres"]
summary: "A search endpoint needs paginated results and a total count. A single CTE with UNION ALL answers both — and for deduplicated searches it runs in roughly half the time. For non-deduplicated searches it matches two separate queries while using one round trip."
---

A search for `format:modern` needs two things from the database: the top 100 cards by popularity and a count for the pagination UI. The obvious implementation sends two queries. A single CTE with `UNION ALL` answers both — and for the deduplicated searches (`unique=card`, one printing per unique card) that make up most traffic, it runs in roughly half the time. For non-deduplicated searches it matches two separate queries while using one round trip instead of two.

## The Naive Two-Query Approach

Both queries scan all 73,336 matching rows. The results query sorts them by `oracle_id` to deduplicate with `DISTINCT ON`. For the count, the natural form is `COUNT(DISTINCT oracle_id)`, but PostgreSQL always implements that with Sort + Aggregate — the same 7943kB sort, just to count. `GROUP BY oracle_id` expresses the same intent and lets the planner choose HashAggregate instead, which uses 2073kB and avoids the sort entirely.

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

The plans:

```
-- Results: 75.7ms
Limit  (actual time=75.413..75.421 rows=100)
  Buffers: shared hit=19243
  ->  Sort  (actual time=75.412..75.415 rows=100)
        Sort Key: t.sort_value, t.edhrec_rank, t.prefer_score DESC NULLS LAST
        Sort Method: top-N heapsort  Memory: 36kB
        ->  Subquery Scan on t  (actual time=67.243..74.060 rows=21994)
              ->  Unique  (actual time=67.241..72.955 rows=21994)
                    ->  Sort  (actual time=67.240..69.431 rows=73336)
                          Sort Key: card.oracle_id, card.prefer_score DESC NULLS LAST
                          Sort Method: quicksort  Memory: 7943kB
                          ->  Bitmap Heap Scan on cards  (actual time=8.233..49.713 rows=73336)
                                Recheck Cond: (card_legalities @> '{"modern": "legal"}')
                                Rows Removed by Index Recheck: 23197
                                ->  Bitmap Index Scan on idx_cards_legalities
Execution Time: 75.727 ms

-- Count: 56.3ms
Aggregate  (actual time=55.914..55.915 rows=1)
  ->  HashAggregate  (actual time=54.214..55.353 rows=21994)
        Group Key: card.oracle_id
        Batches: 1  Memory Usage: 2073kB
        ->  Bitmap Heap Scan on cards  (actual time=8.131..46.302 rows=73336)
              Recheck Cond: (card_legalities @> '{"modern": "legal"}')
              Rows Removed by Index Recheck: 23197
              ->  Bitmap Index Scan on idx_cards_legalities
Execution Time: 56.326 ms
```

Total: ~132ms across two round trips.

## Deduplicating Once Instead of Twice

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
Append  (actual time=75.325..76.784 rows=101)
  CTE distinct_cards
    ->  Unique  (actual time=65.785..72.121 rows=21994)
          ->  Sort  (actual time=65.783..68.420 rows=73336)
                Sort Key: card.oracle_id, card.prefer_score DESC NULLS LAST
                Sort Method: quicksort  Memory: 7943kB
                ->  Bitmap Heap Scan on cards  (actual time=8.016..48.242 rows=73336)
                      Recheck Cond: (card_legalities @> '{"modern": "legal"}')
                      Rows Removed by Index Recheck: 23197
                      ->  Bitmap Index Scan on idx_cards_legalities
  ->  Limit  (actual time=75.323..75.331 rows=100)
        ->  Sort  (actual time=75.322..75.325 rows=100)
              Sort Key: distinct_cards.sort_value, ...
              Sort Method: top-N heapsort  Memory: 36kB
              ->  CTE Scan on distinct_cards  (actual time=65.788..73.889 rows=21994)
                    Storage: Memory  Maximum Storage: 1919kB
  ->  Aggregate  (actual time=1.438..1.438 rows=1)
        ->  CTE Scan on distinct_cards  (actual time=0.001..0.930 rows=21994)
              Storage: Memory  Maximum Storage: 1919kB
Execution Time: 77.122 ms
```

| Query | Raw matches | Unique cards | Two queries | CTE | Winner |
|---|---|---|---|---|---|
| `format:modern` | 73,336 | 21,994 | 132ms (76ms top-n, 56ms count) | 77ms | CTE |
| `power>4` | 6,750 | 2,175 | 24ms (14ms top-n, 10ms count) | 12ms | CTE |

The deduplication sort dominates the cost. Running it once instead of twice is the source of the performance improvement.

## When NOT MATERIALIZED Closes the Gap

When searching by printing rather than unique card, there is no `DISTINCT ON`. The two branches have different optimal access paths:

- The LIMIT branch wants to walk the `edhrec_rank` B-tree index in order and stop as soon as it has 100 rows.
- The COUNT branch wants to use a condition index (`creature_power` or `card_legalities`) to count all matches directly.

A default (materialized) CTE forces the planner to choose a single access strategy for the whole CTE, then serve both branches from the result. For `format:modern`, that means materializing all 73,336 matching rows before either branch can run — 70ms, versus 46ms for two separate queries that can each pick their own index.

The non-dedup path uses `WITH matching_cards AS NOT MATERIALIZED`, which tells PostgreSQL it is free to inline the CTE definition into each branch and plan them independently:

```sql
WITH matching_cards AS NOT MATERIALIZED (
    SELECT ... FROM magic.cards WHERE ...
)
(SELECT null::integer, ... FROM matching_cards ORDER BY ... LIMIT 100)
UNION ALL
(SELECT COUNT(1), null, ... FROM matching_cards)
```

With `NOT MATERIALIZED`, the `format:modern` plan uses the `edhrec_rank` index for the LIMIT branch (examines ~235 rows, 1ms) and the GIN legalities index for the COUNT branch (73,336 rows, 48ms) — 49ms total in a single round trip:

```
Append  (actual time=1.037..48.806 rows=101)
  ->  Limit  (actual time=1.036..1.045 rows=100)
        ->  Incremental Sort  (actual time=1.035..1.038 rows=100)
              Presorted Key: card.edhrec_rank
              ->  Index Scan using idx_cards_edhrec_rank_btree  (actual time=0.605..0.992 rows=107)
                    Filter: (card_legalities @> '{"modern": "legal"}')
                    Rows Removed by Filter: 128
  ->  Aggregate  (actual time=47.745..47.746 rows=1)
        ->  Bitmap Heap Scan on cards  (actual time=7.791..45.673 rows=73336)
              Recheck Cond: (card_legalities @> '{"modern": "legal"}')
              Rows Removed by Index Recheck: 23197
              ->  Bitmap Index Scan on idx_cards_legalities
Execution Time: 48.982 ms
```

| Query | Two queries | Materialized CTE | NOT MATERIALIZED |
|---|---|---|---|
| `format:modern` | 46ms (1ms top-n, 45ms count) | 70ms | 49ms |
| `power>4` | 11ms (10ms top-n, 1ms count) | 14ms | 9ms |

The split timings surprised me at first. For `format:modern`, the planner walks the `edhrec_rank` index in sorted order and finds a modern-legal card roughly every other row — top-n exits after ~235 rows, 1ms total. But counting all 73,336 matches requires a full index scan with no early exit. For `power>4`, it is reversed: the creature_power index covers the count entirely (6,750 entries, no heap fetch, 1ms), but top-n requires walking deep into the sort index before accumulating 100 high-power creatures. Each query is fast in exactly one direction and slow in the other, and the two-query approach exploits that by letting each query pick the cheap side. `NOT MATERIALIZED` lets the planner do the same within a single statement.

## Picking the Right Form for Each Search Mode

The CTE exists for the dedup case: most searches deduplicate by `oracle_id`, and it cuts those query times roughly in half — 132ms to 77ms for a broad query, 24ms to 12ms for a selective one. For non-dedup searches, the top-n and count branches want opposite index strategies — whichever access path is optimal for one is suboptimal for the other. A single materialization decision cannot be optimal for both: materializing all matching rows is right for the count but discards the sort-index early exit for top-n; using the sort index is right for top-n but forces a deep walk for count. Planning the branches independently is the only way to let each use the index it needs, which is what `NOT MATERIALIZED` provides.
