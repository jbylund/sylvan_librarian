# Replace DISTINCT ON with HashAggregate + top-N for unique=card/artwork queries

## Status: closed — benchmarked in PR [#480](https://github.com/jbylund/sylvan_librarian/pull/480) (5da09ce), won't pursue

The hashagg shape was benchmarked head-to-head against `DISTINCT ON` on a 200-query corpus
as part of #480. With the dedup key switched from `card_name` to `oracle_id`, hashagg was
essentially identical to `DISTINCT ON` — the avoid-the-sort hypothesis did not hold at this
data scale. What shipped instead: `DISTINCT ON` keyed on `oracle_id` (~23% faster than the
`card_name` key) and dropping the no-op `DISTINCT ON` for `unique=printing` (~9% faster).
Not pursuing further: SQL-path query-shape work is deprioritized in favor of the Rust engine
path ([done/00490-rust-filter-extension.md](00490-rust-filter-extension.md)).

## Problem

`unique=card` and `unique=artwork` queries use `DISTINCT ON` to collapse ~95k printings to one
result per card or artwork. PostgreSQL implements `DISTINCT ON` exclusively via sort + unique —
there is no hash-based path in the planner. For large result sets this is expensive:

| Query | Rows sorted | Sort memory | Execution |
|-------|-------------|-------------|-----------|
| `format:modern orderby=toughness unique=card` | 72,945 | 5,952 kB | ~191 ms |

The sort dominates even when the filter itself is fast. `format:modern` matches ~73k of ~95k
printings, so there is no index that avoids the large sort.

`unique=printing` does not need this treatment — printing is the primary key of `magic.cards`, so
no deduplication is required and the current query already returns one row per matching printing
without any `DISTINCT ON`.

## Proposed approach

Replace the `DISTINCT ON` CTE with a `GROUP BY` (HashAggregate) + top-N join for `unique=card`
and `unique=artwork`. The group key changes per mode (`oracle_id` for cards, `illustration_id` for artworks); the
structure is otherwise identical.

**`unique=card`** (group by `oracle_id` — more correct than `card_name` since oracle_id uniquely
identifies a card face across all printings, handling edge cases where names are reused):

```sql
WITH matching_cards AS (
    SELECT
        oracle_id                      AS group_key,
        MAX(prefer_score)              AS best_score,
        MIN(scryfall_id::text)::uuid   AS tiebreak,
        ANY_VALUE(creature_toughness)  AS sort_value
    FROM magic.cards
    WHERE <where_clause>
    GROUP BY oracle_id
),
top_n AS (
    SELECT group_key, best_score, tiebreak
    FROM matching_cards
    ORDER BY sort_value DESC NULLS LAST
    LIMIT %(limit)s
)
(
    SELECT null::integer AS total_cards_count, c.*
    FROM top_n
    JOIN magic.cards c ON
        c.oracle_id    = top_n.group_key  AND
        c.prefer_score = top_n.best_score AND
        c.scryfall_id  = top_n.tiebreak
)
UNION ALL
(
    SELECT COUNT(1) AS total_cards_count, null
    FROM matching_cards
);
```

**`unique=artwork`** (group by `illustration_id`):

```sql
WITH matching_artworks AS (
    SELECT
        illustration_id                AS group_key,
        MAX(prefer_score)              AS best_score,
        MIN(scryfall_id::text)::uuid   AS tiebreak,
        ANY_VALUE(creature_toughness)  AS sort_value
    FROM magic.cards
    WHERE <where_clause>
    GROUP BY illustration_id
),
top_n AS (
    SELECT group_key, best_score, tiebreak
    FROM matching_artworks
    ORDER BY sort_value DESC NULLS LAST
    LIMIT %(limit)s
)
(
    SELECT null::integer AS total_cards_count, c.*
    FROM top_n
    JOIN magic.cards c ON
        c.illustration_id = top_n.group_key  AND
        c.prefer_score    = top_n.best_score AND
        c.scryfall_id     = top_n.tiebreak
)
UNION ALL
(
    SELECT COUNT(1) AS total_cards_count, null
    FROM matching_artworks
);
```

**Why this is faster:**

- `GROUP BY` uses `HashAggregate` — O(n) time, O(distinct group keys) space, no sort.
- `ORDER BY ... LIMIT` over ~22k deduplicated rows uses a **top-N heapsort** (37 kB) rather than
  a full sort (5,952 kB) over 73k raw rows.
- The final join is 100 PK lookups via `idx_cards_scryfall_id` — essentially free.
- `Inner Unique: true` on the nested loop confirms the join produces exactly one row per group.

**Tiebreaker:** `MIN(scryfall_id::text)::uuid` resolves ties in `prefer_score` deterministically.
`MIN` is not defined for the `uuid` type directly; casting through `text` works and casting back to
`uuid` preserves index use on `scryfall_id` in the join.

**Sort key aggregation:** The sort key must be carried through the HashAggregate so the top-N step
can order without another join. How to aggregate it depends on the attribute type:

- **Card-level attributes** (`creature_toughness`, `cmc`, `edhrec_rank`, etc.) are identical
  across all printings of a card — use `ANY_VALUE(col)`.
- **Printing-level attributes** (`price_usd`, `price_eur`, `price_tix`) vary per printing. The
  sort value should reflect the printing that will be displayed (the tiebreak winner), so use the
  aggregate that selects the same printing: the value corresponding to `MAX(prefer_score)` /
  `MIN(scryfall_id::text)` tiebreak. In practice, `MIN(price_usd)` (cheapest available) is a
  reasonable approximation and simpler to implement; exact matching would require a correlated
  subquery and is likely not worth it.

`ANY_VALUE` requires PostgreSQL 16+. The codebase targets PG18 so this is safe.

**COUNT semantics:** The `UNION ALL` count uses `matching_cards`, which has one row per distinct
group (card or artwork). This is consistent — `total_cards_count` reflects the number of unique
entities in the result, matching the grouping dimension.

## Observed benchmark

`format:modern orderby=toughness unique=card`:

| Metric | DISTINCT ON | HashAggregate |
|--------|-------------|---------------|
| Dedup node | Sort + Unique (5,952 kB) | HashAggregate (5,137 kB) |
| Sort node | full quicksort over 22k rows | top-N heapsort, 37 kB |
| Final fetch | included in dedup scan | 100 × PK index lookups |
| Execution time | ~191 ms | ~116 ms |
| Planning time | ~3 ms | ~1 ms |

~38% faster on this query. The remaining cost is the heap scan (18,900 blocks), which is
irreducible without a normalized cards/printings schema — see
[normalized-cards-printings-schema.md](../normalized-cards-printings-schema.md) (not yet written).

## Benchmark suite

See [local-query-benchmark-suite.md](../local-query-benchmark-suite.md). Run it against both the current branch
and the hashagg branch before merging to confirm the improvement holds across query types and
that highly selective queries show no regression.

## Implementation tasks

- [ ] **Carry all sort keys** through the HashAggregate. The full outer ORDER BY is
  `sort_value [DIR] NULLS LAST, edhrec_rank ASC NULLS LAST, prefer_score DESC NULLS LAST`.
  Add `ANY_VALUE(edhrec_rank)` to the aggregate alongside the primary sort key.
- [ ] **Generate the new query shape** in `api_resource.py` for `unique=card` and
  `unique=artwork`. The `unique=printing` path needs no changes — no deduplication step exists
  there today and none is needed.
- [ ] **Classify sort columns** in `db_info.py` as card-level (`ANY_VALUE`) vs. printing-level
  (`MIN`/`MAX`) so the correct aggregate is emitted per `orderby=` value.
- [ ] **Benchmark across query types** — the improvement is clear for large result sets like
  `format:modern`, but may be neutral or negative for highly selective queries where the sort cost
  was already trivial. Use `magic.query_log` to identify representative slow queries and compare
  before/after.
- [ ] **Update tests** in `api/tests/` to cover the new query shape.
