---
title: "PostgreSQL COPY Loading: Faster Bulk Import"
date: 2026-08-29
publishDate: 2026-08-29
tags: ["postgres", "performance", "python"]
summary: "Switching from row-by-row inserts to PostgreSQL's COPY protocol meaningfully cut import time. Why COPY is fast, how to stream data into it from Python, and two approaches to expanding a JSON blob into a typed row."
---

Arcane Tutor imports card data from Scryfall's bulk export on every container startup. The initial implementation inserted rows one at a time — a loop, one `INSERT` per card, ~30k iterations ([source](https://github.com/jbylund/arcane_tutor/blob/02b969938aedbe0c436750239d20bcc5669ef131/api/api_resource.py#L462-L483)). Switching to PostgreSQL's `COPY` protocol brought the import time down significantly ([PR #33](https://github.com/jbylund/arcane_tutor/pull/33)).

## The Problem with Row-by-Row Inserts

The original code looked like this:

```python
for card in to_insert:
    card_with_json = {k: maybe_json(v) for k, v in card.items()}
    cursor.execute(
        """
        INSERT INTO magic.cards (card_name, cmc, mana_cost_text, ...)
        VALUES (%(name)s, %(cmc)s, %(mana_cost)s, ...)
        ON CONFLICT (card_name) DO NOTHING
        """,
        card_with_json | {"blob": Jsonb(card)},
    )
```

Each iteration is a full round trip: send statement, parse it, plan it, execute it, write WAL, send acknowledgment. The parsing and planning costs are small per row, but they add up across 30k rows. More significantly, each row is its own network round trip to the database — even on localhost, that latency stacks.

## How COPY Works

PostgreSQL's `COPY` command is designed for bulk data transfer. Instead of sending one parsed statement per row, you open a data channel, stream all rows, and close it. `COPY` bypasses the SQL parser entirely — this is what makes it fundamentally different from a prepared statement batch, which still parses and plans once per statement shape. PostgreSQL writes the rows in a single transaction with batched WAL. The result is effectively zero per-row overhead for the protocol layer.

The catch: `COPY` loads into a single table from a flat stream. There is no `ON CONFLICT`, no expressions, no type coercion at stream time. If you want to transform data as it lands, you need a staging table.

## The Two-Phase Approach

The implementation uses a temporary staging table with a single `jsonb` column:

```sql
CREATE TEMPORARY TABLE import_staging (card_blob jsonb) ON COMMIT DROP
```

**Phase 1:** Stream all cards as JSON blobs into the staging table via COPY — no type coercion, no constraints, just jsonb.

**Phase 2:** A single `INSERT ... SELECT` expands each blob into the columns of `magic.cards`.

The `ON COMMIT DROP` is a safety net: if an error aborts the transaction before the explicit `DROP TABLE`, PostgreSQL cleans up the staging table automatically. Without it, a failed batch would leave the staging table around and the next batch would hit a naming conflict.

## Expanding the Blob: Explicit vs. `jsonb_populate_record` ([PR #531](https://github.com/jbylund/arcane_tutor/pull/531))

The first version of Phase 2 listed every column explicitly:

```sql
INSERT INTO magic.cards (card_name, cmc, mana_cost_text, mana_cost_jsonb, ...)
SELECT
    card_blob->'name',
    (card_blob->>'cmc')::float::integer,
    card_blob->'mana_cost',
    card_blob->'mana_cost',
    ...
FROM import_staging
ON CONFLICT (card_name) DO NOTHING
```

This works, but it is brittle. Adding a column to the table means updating the INSERT list, the SELECT list, the preprocessing code that shapes the JSON, and the tests. The explicit casts like `(card_blob->>'cmc')::float::integer` are easy to get wrong — Scryfall stores cmc as a float, so the `::float` is needed before `::integer` or the cast fails on values like `3.0`.

`jsonb_populate_record` eliminates all of that:

```sql
INSERT INTO magic.cards
SELECT (jsonb_populate_record(null::magic.cards, card_blob)).*
FROM import_staging
ON CONFLICT DO NOTHING
```

`jsonb_populate_record(null::magic.cards, card_blob)` takes a null row typed as `magic.cards` (which tells it the column names and types) and populates it from the JSONB object by matching keys. The `.*` expands the composite result into individual columns. PostgreSQL applies the column type definitions for coercion — `cmc` is an `integer` column, so a JSON `3.0` becomes `3` automatically.

The practical benefits:
- Add a column to the table and it starts populating as long as the JSON key matches.
- Remove a column and the query keeps working — unmatched keys are silently ignored.
- The casts live in the schema definition, not scattered across Python and SQL.

The one constraint: JSON key names must match column names exactly. If the Scryfall blob uses `name` and the column is `card_name`, you need a preprocessing step to rename the key before the COPY.

## Streaming from Python

psycopg3 exposes COPY through a context manager on the cursor:

```python
with cursor.copy(
    "COPY import_staging (card_blob) FROM STDIN WITH (FORMAT csv, HEADER false)"
) as copy_filehandle:
    writer = csv.writer(copy_filehandle, quoting=csv.QUOTE_ALL)
    writer.writerows(
        [orjson.dumps(card, option=orjson.OPT_SORT_KEYS).decode("utf-8")]
        for card in page
    )
```

The format is CSV with `QUOTE_ALL` rather than the binary protocol. Binary COPY avoids text encoding overhead but requires constructing PostgreSQL's binary wire format manually — non-trivial for JSONB columns. CSV is straightforward: `orjson.dumps` produces a JSON string, `csv.writer` quotes it so commas and newlines inside the JSON do not confuse the parser, and psycopg handles the protocol framing.

## Error Handling: The All-or-Nothing Tradeoff

With row-by-row inserts, a single malformed card fails that one row. The loop continues and 29,999 cards import successfully. `COPY` is transactional: one bad row aborts the entire batch.

In practice this has not been an issue. The input comes from Scryfall's bulk data export, which is reliably structured. The implementation also samples ~20 random cards from the staging table before the final INSERT and logs them as a sanity check, which catches schema mismatches early.

The tradeoff is worth naming anyway: `COPY` gives you throughput at the cost of partial-success tolerance. For unreliable or user-supplied input, you would want validation before the COPY call or a separate staging pass that catches bad rows before the final INSERT.

## Results

Benchmarked with 30,000 synthetic cards on the current schema (PostgreSQL 18, 40 indexes on `magic.cards`, testcontainers cross-process networking, macOS arm64, 3 runs).

The COPY stream takes a fixed ~0.8s regardless of whether any rows actually change — it is a blind bulk transfer with no conflict awareness. The INSERT...SELECT phase does the real work. The `jsonb_populate_record` cost scales with schema width: at 40 columns and 30k rows, the expansion alone accounts for most of the 7.4s INSERT time. The explicit column INSERT avoids that by pushing individual JSON field extractions into the column list.

| Method | COPY stream | INSERT...SELECT | Total |
|---|---|---|---|
| `execute` per card (30k round trips) | — | — | 8.2s |
| `executemany` (batches of 1,000) | — | — | 2.5s |
| COPY + explicit column INSERT | ~0.8s | ~2.3s | 3.1s |
| COPY + `jsonb_populate_record` | ~0.8s | ~7.4s | 8.2s |

A later iteration ([PR #532](https://github.com/jbylund/arcane_tutor/pull/532)) dropped the staging table entirely, replacing both COPY phases with a single statement: `jsonb_array_elements` to expand the JSON array, a LEFT JOIN on the target table to make existing row values available, and per-column `CASE WHEN obj ? 'col'` expressions to compute each proposed value before the conflict clause sees it. That lets `ON CONFLICT DO UPDATE SET col = EXCLUDED.col WHERE row IS DISTINCT FROM EXCLUDED` work correctly — `EXCLUDED` already carries the right value for each column, whether the key was present in the input or not. The implementation lives in [`api/db/bulk_upsert.py`](https://github.com/jbylund/arcane_tutor/blob/0a84536fb62646cf746f03a812e7add656ba0428/api/db/bulk_upsert.py); the SQL core is at [lines 191–200](https://github.com/jbylund/arcane_tutor/blob/0a84536fb62646cf746f03a812e7add656ba0428/api/db/bulk_upsert.py#L191-L200) and the per-column CASE resolution at [lines 77–93](https://github.com/jbylund/arcane_tutor/blob/0a84536fb62646cf746f03a812e7add656ba0428/api/db/bulk_upsert.py#L77-L93).

The three scenarios below show what each approach actually does under different workloads:

- **insert**: all 30,000 rows are new
- **unchanged**: all rows already exist with identical data
- **update**: all rows exist; 95% (28,500) have `oracle_text` changed

| Method | insert | unchanged | update |
|---|---|---|---|
| `execute` per card | 8.2s | 5.3s | 5.2s † |
| `executemany` | 2.5s | 1.3s | 1.3s † |
| COPY + explicit INSERT | 3.1s | 2.2s | 2.2s † |
| COPY + `jsonb_populate_record` | 8.2s | 7.2s | 7.2s † |
| `jsonb_array_elements` + LEFT JOIN | 1.9s | 0.9s | 2.1s |

† `ON CONFLICT DO NOTHING` — conflicts are skipped without checking whether the data changed.

The unchanged scenario shows the cost floor: COPY approaches pay ~0.8s for the stream on every run regardless of whether anything changed. The `jsonb_array_elements` approach drops to 0.9s because the `WHERE` clause prevents any heap writes when nothing is distinct.

The update column is the only one where correctness matters. All the `†` rows report the same time as unchanged — not because the work is the same, but because no work was done. The `jsonb_array_elements` result (2.1s) is higher than its unchanged baseline (0.9s) because 28,500 rows are actually written. It is the only result in that column where the data in the table reflects the input.

## See Also

The tigerdata.com post [Boosting Postgres Insert Performance](https://www.tigerdata.com/blog/boosting-postgres-insert-performance) covers a similar single-statement batch approach using `unnest` with typed arrays rather than `jsonb_array_elements`. The tradeoff: `unnest` requires knowing the column types at query-construction time and cannot express "key absent → preserve existing value" without extra logic, but avoids the JSON encoding overhead and is faster for simple inserts where key-presence semantics are not needed.
