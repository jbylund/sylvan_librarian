---
title: "The ILIKE Trap: 40ms of Planning for 3ms of Work"
date: 2026-12-05
publishDate: 2026-12-05
tags: ["postgres", "sql", "performance"]
summary: "ILIKE on a trigram-indexed column was spending ~40ms in the query planner for every ~3ms of execution. The fix was two lines of SQL and one line of Python — but finding it required understanding what the planner actually does with ILIKE."
---

A query for `oracle:counter oracle:flying oracle:sacrifice tou>=5` should be fast.
The three oracle text terms each hit a GIN trigram index; the toughness condition hits a B-tree.
Execution time: ~3ms.
But the server was reporting 110ms for this query, and the extra 107ms never appeared in the execution plan.

The time was in the planner, not the executor.

## What EXPLAIN ANALYZE Shows

`EXPLAIN (ANALYZE, BUFFERS)` splits a query into two phases.
The execution plan covers what the executor did — index scans, sort steps, buffer hits.
Planning time is reported separately at the top level and is not part of any plan node.
For most queries, planning is a rounding error.

For this one it was not. The bottom two lines of the plan:

```
Planning Time: 109.4 ms
Execution Time: 3.1 ms
```

The executor was fast.
The 109ms was spent before the first row was read.

Adding a second oracle text term made it worse:

| ILIKE conditions | Planning time | Execution time |
|-----------------|---------------|----------------|
| 0               | ~0.3 ms       | ~25 ms         |
| 1               | ~40 ms        | ~8 ms          |
| 3               | ~110 ms       | ~3 ms          |

(PostgreSQL 17, ~30k rows in `magic.cards`, M1 MacBook Pro, warm cache. Each number is the median of five runs from `EXPLAIN ANALYZE`.)

The scaling is roughly linear in the number of ILIKE conditions.
Zero text searches, normal planning time.
Add one `oracle:` term and 40ms appear from nowhere.

## Why ILIKE Is Expensive to Plan

PostgreSQL's selectivity estimators for `LIKE` and `ILIKE` work differently.
Both live in `selfuncs.c`, the file that implements all the planner's cardinality and selectivity estimation logic.

For `LIKE '%counter%'`, the estimator extracts trigrams from the pattern, estimates how many rows contain those trigrams, and uses that to decide whether a trigram GIN index is worth using.
It queries `pg_statistic` for the column's trigram frequency data.

`ILIKE '%counter%'` calls the same path, but case-insensitively.
The estimator must account for all case variations of each trigram: `cou`, `Cou`, `cOu`, `COu`, and so on.
For a three-character trigram with all-letter characters, that is up to 8 variants.
For each trigram in the pattern, the estimator enumerates the case-folded variants and looks them up in `pg_statistic`.
The more trigrams the pattern generates — longer patterns produce more — the more work the estimator does.

With three ILIKE conditions, the planner runs three of these enumeration passes before it can even start deciding which indexes to use.
The actual execution touches the GIN indexes efficiently; the planning cost is paid in full on every query regardless of whether the result is cached afterward.

The original code in `card_query_nodes.py` always emitted `ILIKE`:

```python
return f"({lhs_sql} ILIKE %({_param_name})s)"
```

The indexes — `idx_cards_oracle_text_trgm`, `idx_cards_cardname_trgm` — were correct.
Execution was fast.
The problem was that each query paid a fixed per-condition planning cost before the fast execution path ever ran.

## My First Wrong Hypothesis

My first guess was that the GIN indexes were being ignored and a sequential scan was running.
That would explain slow queries on text columns.
I ran `EXPLAIN (ANALYZE, BUFFERS)` expecting to see `Seq Scan on cards`.

The plan showed bitmap index scans on the trigram indexes, exactly as intended.
Execution was 3ms.
I added the full `EXPLAIN ANALYZE` output to the server logs and watched: fast execution, slow planning, consistently.

The planning time was not flaky.
It reproduced on every run and scaled with the number of ILIKE conditions.
This ruled out lock contention or catalog bloat.
The pattern pointed at the selectivity estimator itself.

## The Fix: Push lower() Into the Index

The fix has two parts, and they work together.

**Part 1: New GIN indexes on `lower(column)`.**

Instead of indexing `oracle_text`, index `lower(oracle_text)`.
PostgreSQL maintains the index automatically when the column is updated; the functional expression just changes what value gets stored in each leaf entry.

```sql
CREATE INDEX IF NOT EXISTS idx_cards_oracle_text_lower_trgm
    ON magic.cards USING gin (lower(oracle_text) magic.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_cards_cardname_lower_trgm
    ON magic.cards USING gin (lower(card_name) magic.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_cards_artist_lower_trgm
    ON magic.cards USING gin (lower(card_artist) magic.gin_trgm_ops)
    WHERE card_artist IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_flavor_text_lower_trgm
    ON magic.cards USING gin (lower(flavor_text) magic.gin_trgm_ops)
    WHERE flavor_text IS NOT NULL;
```

([`api/db/2026-05-20-02-lower-trgm-indexes.sql`](https://github.com/jbylund/arcane_tutor/blob/0b20fce016afe07ca03d28bb3233f7f496cd28ba/api/db/2026-05-20-02-lower-trgm-indexes.sql))

**Part 2: Lowercase the pattern at query-build time, emit `lower(col) LIKE pattern`.**

```python
words = ["", *(_escape_like_pattern(w) for w in txt_val.lower().split()), ""]
pattern = "%".join(words)
return f"(lower({lhs_sql}) LIKE {context.add(pattern)})"
```

([`card_query_nodes.py`](https://github.com/jbylund/arcane_tutor/blob/0b20fce016afe07ca03d28bb3233f7f496cd28ba/api/parsing/card_query_nodes.py#L931-L933))

Now `oracle:counter` generates `lower(oracle_text) LIKE '%counter%'`.
The pattern is already lowercase, so the LIKE estimator does not need to enumerate case variants.
The trigrams in the pattern match exactly what is stored in the `lower(oracle_text)` index entries.
The planner uses the cheap LIKE estimator path and the index scan runs as before.

The old ILIKE indexes were dropped.
With `lower(col) LIKE pattern` in the query, they would never be used anyway — PostgreSQL cannot use a plain-column index for a `lower(col)` expression.

## What Else Changed

Switching to `LIKE` also surfaced a latent bug: `%` and `_` in user input were live SQL wildcards under ILIKE.
`name:"50%"` generated `ILIKE '%50%%'` — the trailing `%` matched any suffix rather than the literal character, a bug that had existed since the first text search was added.
The fix required a `_escape_like_pattern` helper ([line 459](https://github.com/jbylund/arcane_tutor/blob/0b20fce016afe07ca03d28bb3233f7f496cd28ba/api/parsing/card_query_nodes.py#L459-L461)):

```python
def _escape_like_pattern(value: str) -> str:
    # Backslash must be escaped first; otherwise the \ added for % and _ would be re-escaped.
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
```

Under the new path, `name:"50%"` generates `LIKE '%50\%%'`, matching only the literal string `50%`.
This aligns with Scryfall's behavior.

## After the Fix

| lower() LIKE conditions | Planning time | Execution time |
|------------------------|---------------|----------------|
| 0                      | ~0.3 ms       | ~25 ms         |
| 1                      | ~0.5 ms       | ~8 ms          |
| 3                      | ~0.9 ms       | ~3 ms          |

Same methodology as before.
The three-condition query went from 110ms total to 3.9ms.
Planning overhead collapsed from 40ms per condition to well under 1ms per condition.
Execution was unchanged.

This fix does not help regex searches (`oracle:/counter/`).
The `~*` operator uses a different selectivity estimator that works against precomputed regex statistics, not a per-character case-variant enumeration, so it does not carry the same per-condition planning cost.
The regex path was left on `~*` and was not changed in [PR #470](https://github.com/jbylund/arcane_tutor/pull/470), which covers only the `LIKE`-path text fields.

One caveat worth naming: the `lower()` functional index only benefits queries that emit `lower(col) LIKE`.
Any query still using `ILIKE` against the old column expression gets a sequential scan — there is no index to fall back to.
That made the migration a hard cutover: once the old indexes were dropped, every text search path in the query generator had to be converted before deploying.

The tool for finding this was not a profiler or a flame graph.
It was `EXPLAIN (ANALYZE, BUFFERS)` on a query that felt slow, and reading the number at the bottom labeled "Planning Time."
