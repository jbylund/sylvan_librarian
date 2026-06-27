---
title: "Oracle ID Dedup: 23% Faster by Changing the Key, 9% Faster by Removing Dead Work"
date: 2026-12-05
publishDate: 2026-12-05
tags: ["postgres", "sql", "performance", "benchmarking"]
summary: "Two SQL hypotheses about DISTINCT ON key choice: UUID vs text, and whether DISTINCT ON the primary key does any real work. One hypothesis failed; two wins shipped."
---

The search endpoint had three distinct modes — `unique=card`, `unique=artwork`, `unique=printing` — and one SQL shape for all of them. The shape used `DISTINCT ON` in every case. For `unique=card`, the key was `card_name`. For `unique=artwork`, the key was `illustration_id`, a UUID already stored in the table. For `unique=printing`, the key was `scryfall_id`, the primary key — every row is already unique on it. The `DISTINCT ON` for printing was a no-op semantically, but the planner did not know that.

That observation, and a second question about key type, produced three hypotheses worth testing:

1. `DISTINCT ON` for `unique=card` keyed on `card_name`, a variable-length text column. The table also has `oracle_id`, a UUID — fixed-width, 16 bytes. Does the key type affect sort cost?
2. Would a `GROUP BY oracle_id` hashagg beat `DISTINCT ON (oracle_id)` by avoiding the sort entirely?
3. `DISTINCT ON (scryfall_id)` for `unique=printing` is logically unnecessary. Is it physically free?

## Building a Benchmark That Could Tell Them Apart

The easy mistake is to build a benchmark weighted toward narrow queries. A search for `name:lightning bolt` returns a handful of rows; deduplication is essentially free regardless of what key you use. The cost only shows up when the match set is large.

The [benchmark harness](https://github.com/jbylund/arcane_tutor/blob/5da09ce581c2295a09927e8bb419fc495ef9bafe/client/query_runner.py) generates a seeded corpus of 200 query/orderby/unique triples, weighted 60% toward large-result queries: `format:modern`, `format:legacy`, `format:commander`, three-color `id:xxx` filters. `format:modern` alone matches 73,336 rows. Seeding the RNG makes the corpus deterministic — the same seed produces the same 200 queries every time, so results from different runs are directly comparable. The 200 triples collapse to 163 distinct query strings after deduplication (several triples share the same query text but differ in orderby or unique mode, so the per-approach tables below reflect 163 distinct queries rather than 200).

For each query, the harness runs a warmup pass through all approaches in sequence (equal warm-cache state), then 50 timed rounds in round-robin order — no approach sees systematically colder cache than another. Each timed run uses `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` to get wall-clock execution time directly from the planner rather than from Python's `time.monotonic()`, which would include driver and serialization overhead. All runs were on an M3 MacBook Pro with PostgreSQL 18 running in Docker.

## UUID vs. Text: 23% Faster

The old SQL for `unique=card`:

```sql
WITH distinct_cards AS (
    SELECT DISTINCT ON (card_name)
        card_name, oracle_id, prefer_score, edhrec_rank AS sort_value
    FROM magic.cards AS card
    WHERE ...
    ORDER BY card_name, prefer_score DESC NULLS LAST
)
...
```

The hypothesis: replace `card_name` with `oracle_id`. `card_name` is `text` — variable-length, collation-sensitive comparisons. `oracle_id` is `uuid` — fixed 16 bytes, integer-comparable. `DISTINCT ON` sorts by the key column to identify group boundaries, so the comparison type matters.

The new SQL:

```sql
WITH distinct_cards AS (
    SELECT DISTINCT ON (oracle_id)
        card_name, oracle_id, prefer_score, edhrec_rank AS sort_value
    FROM magic.cards AS card
    WHERE ...
    ORDER BY oracle_id, prefer_score DESC NULLS LAST
)
...
```

Benchmark results across 163 distinct queries × 50 rounds:

| Approach | Median | P90 | Total |
|---|---|---|---|
| `DISTINCT ON (card_name)` | 9.5ms | 74.7ms | 181,312ms |
| `DISTINCT ON (oracle_id)` | 7.3ms | 60.2ms | 139,402ms |

Total execution time across the corpus dropped 23.1%. The gain is concentrated in the large-result queries — `border:black` went from 92ms to 51ms (44% faster), `format:modern` from 87ms to 55ms (37% faster), `usd<5` from 77ms to 44ms (43% faster). For narrow queries, both are under 2ms and the difference is noise.

The [change in the source](https://github.com/jbylund/arcane_tutor/blob/5da09ce581c2295a09927e8bb419fc495ef9bafe/api/api_resource.py#L1204-L1209) is three characters: `"card_name"` → `"oracle_id"`.

One question this raises: is `DISTINCT ON (oracle_id)` logically equivalent to `DISTINCT ON (card_name)`? Oracle ID identifies the canonical card text as defined by Scryfall — all printings of the same card across all sets share one oracle ID, and a reprinted card with errata'd text gets a new oracle ID for the errata'd version. Card name is stable across printings but diverges from oracle ID in a few edge cases (the same card can appear under different names in different languages, and some reversible dual-faced cards have oracle IDs that do not align 1:1 with the card name you would search). In practice, using oracle ID as the dedup key produces the right result for every search the frontend handles — it is the same key Scryfall uses for `unique=card` deduplication — but it is a semantic change, not just a performance one.

## Hashagg vs. DISTINCT ON: No Difference

While investigating the key type, a second hypothesis appeared: could `GROUP BY oracle_id` with a `MAX(prefer_score)` aggregation (hashagg) beat `DISTINCT ON (oracle_id)` by avoiding the sort entirely? `DISTINCT ON` requires a sort on the key column; hashagg builds a hash table. For a table of ~300,000 rows with ~22,000 distinct oracle IDs, maybe the hash table approach wins.

It did not.

| Approach | Median | P90 | Total |
|---|---|---|---|
| `DISTINCT ON (oracle_id)` | 7.3ms | 60.2ms | 139,402ms |
| Hashagg (`GROUP BY oracle_id`) | 7.6ms | 60.7ms | 139,152ms |

The difference is within measurement noise. The planner chose comparable plans for both shapes at this cardinality — PostgreSQL's cost model had already found the near-optimal strategy. That ruled out the aggregation path. The remaining question was whether the `unique=printing` sort could be eliminated entirely.

## Dropping the No-Op Sort: 9% Faster

For `unique=printing`, the original SQL was:

```sql
WITH distinct_cards AS (
    SELECT DISTINCT ON (scryfall_id)
        card_name, prefer_score, edhrec_rank AS sort_value
    FROM magic.cards AS card
    WHERE ...
    ORDER BY scryfall_id, prefer_score DESC NULLS LAST
)
(
    SELECT null::integer, card_name, ...
    FROM distinct_cards
    ORDER BY sort_value ASC NULLS LAST, edhrec_rank ASC NULLS LAST, prefer_score DESC NULLS LAST
    LIMIT 100
)
UNION ALL
(SELECT COUNT(1), null, null, ... FROM distinct_cards)
```

`scryfall_id` is the primary key. `DISTINCT ON (scryfall_id)` keeps one row per `scryfall_id` — which is all rows, because the column is unique. The `DISTINCT ON` eliminates nothing. But the planner does not consult uniqueness constraints when planning `DISTINCT ON` — it sees `ORDER BY scryfall_id` and emits a Sort + Unique node regardless. It sorts every matching row on the primary key. For `format:modern`, that is 73,336 rows:

```
Unique  (actual time=62.0..69.8 rows=73336)
  ->  Sort  (actual time=61.9..65.7 rows=73336)
        Sort Key: card.scryfall_id, card.prefer_score DESC NULLS LAST
        Sort Method: quicksort  Memory: 17143kB
        ->  Bitmap Heap Scan on cards  (actual time=7.8..47.4 rows=73336)
```

The sort keeps all 73,336 rows — the Unique step removes zero — and allocates 17MB just to produce an identical set. The planner cannot infer from the primary key constraint that the dedup step is vacuous.

The fix is to remove `DISTINCT ON` from the printing path entirely and push the `ORDER BY` out of the CTE into the `LIMIT` branch:

```sql
WITH matching_cards AS NOT MATERIALIZED (
    SELECT card_name, prefer_score, edhrec_rank AS sort_value
    FROM magic.cards AS card
    WHERE ...
)
(
    SELECT null::integer, card_name, ...
    FROM matching_cards
    ORDER BY sort_value ASC NULLS LAST, edhrec_rank ASC NULLS LAST, prefer_score DESC NULLS LAST
    LIMIT 100
)
UNION ALL
(SELECT COUNT(1), null, null, ... FROM matching_cards)
```

Two things change. First, the full sort on `scryfall_id` disappears — no dedup, no 300,000-row sort just to keep all 300,000 rows. Second, the `ORDER BY` moves to the `LIMIT` branch, which tells PostgreSQL it only needs the top 100 values. The planner can satisfy that with a top-N heapsort — O(n log k) for k=100 — instead of a full quicksort — O(n log n). The CTE is also `NOT MATERIALIZED` (`NOT MATERIALIZED` was introduced in PostgreSQL 12 as an explicit hint; by default, CTEs referenced more than once are materialized) so the count branch can use its own index strategy independently, as described in [One Query, Two Answers](00144_results-and-count-single-query.md).

Benchmark results:

| Approach | Median | P90 | Total |
|---|---|---|---|
| `DISTINCT ON (scryfall_id)` | 4.3ms | 63.5ms | 36,763ms |
| No dedup, ORDER BY in LIMIT branch | 4.2ms | 59.2ms | 33,358ms |

Total execution time dropped 9.3%. The gain is smaller than for the key-type change because `unique=printing` queries are inherently faster — there is no deduplication work at all — and because top-N heapsort only beats full sort when the result set is large relative to the page size. For `format:modern`, the saving is more visible: 59.5ms → 48.8ms (18% faster).

The [two branches](https://github.com/jbylund/arcane_tutor/blob/5da09ce581c2295a09927e8bb419fc495ef9bafe/api/api_resource.py#L1258-L1330) in the source make the difference explicit: `unique=printing` uses `matching_cards AS NOT MATERIALIZED` with no `ORDER BY` in the CTE; all other modes use `distinct_cards` with `DISTINCT ON` and an `ORDER BY` keyed by the dedup column.

## What Shipped

PR [#480](https://github.com/jbylund/arcane_tutor/pull/480) shipped two of the three hypotheses:

- `DISTINCT ON (card_name)` → `DISTINCT ON (oracle_id)` for `unique=card`: **23% faster across the corpus**
- `unique=printing` path rewritten without `DISTINCT ON`, `ORDER BY` deferred to `LIMIT` branch: **9% faster across the corpus**
- Hashagg: no improvement over `DISTINCT ON (oracle_id)`, not shipped

The gains are real but bounded. On narrow queries, both approaches complete in under 2ms and the difference is noise. The 23% and 9% figures are medians across a corpus weighted toward large result sets. On a corpus of random narrow queries the numbers would be smaller. The benchmark is explicit about this: it is a stress test of the dedup code path, not a representative sample of all traffic.

One side effect: the fix required `oracle_id` to be non-null in the fixture data. `DISTINCT ON (oracle_id)` with null values collapses all nulls to a single row — three different test cards with null `oracle_id` became one result, and two integration tests broke. The migration added a `NOT NULL` constraint; the fixtures were patched with deterministic UUID v5 values.

## Related

The same deduplication problem — one preferred printing per oracle ID — reappears in the Rust engine,
where the query planner is gone and the choice is made explicitly in code. See
[Linear Scan vs. Hash Scan for Distinct Queries](00832_linear-hash-scan-distinct.md) for how the
engine picks between a linear scan and a hash scan based on result set size.
