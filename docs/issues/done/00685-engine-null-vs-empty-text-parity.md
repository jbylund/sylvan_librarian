# Engine vs SQL: NULL vs empty-string text fields under negation

Status: resolved 2026-07-14 in [PR #685](https://github.com/jbylund/sylvan_librarian/pull/685).
`oracle_text` was already NULL-free (Scryfall always sends it). `flavor_text` had 45,015 NULL
rows; confirmed empirically against scryfall.com's live search (missing-as-empty is Scryfall's
own behavior — no third "unknown, excluded either way" bucket) and fixed with Option 1 below:
normalized at ingest, backfilled, and the column is now `NOT NULL DEFAULT ''`. No engine change
needed — it already matched Scryfall.

## Problem

The engine and SQL paths can disagree on negated text filters for cards whose text
columns are NULL in the database.

The engine loads nullable text columns with `unwrap_or_default()`, so a card with no
oracle text holds `""` in the store. SQL keeps the NULL. The two values behave the same
for positive filters but differently under negation:

- `-o:draw` on a vanilla creature with NULL `oracle_text`:
  - **Engine**: `"".contains("draw")` is `false` → negation matches → card included.
  - **SQL**: `oracle_text LIKE '%draw%'` is NULL → `NOT (...)` is NULL → card excluded.

Affected fields are the ones stored as plain `String` in the engine but nullable in the
DB: `oracle_text`, `flavor_text` (and in principle `card_layout`, `card_border`,
`collector_number`, `set_name`, `type_line`, though those are realistically never NULL).
Fields the engine stores as `Option` (`card_artist`, `card_watermark`,
`mana_cost_text`) already produce SQL-NULL semantics via the tri-state evaluator
([negation / collection operators / devotion], PR #490 follow-up work), so they are
consistent.

## Which behavior is correct?

Unclear, and the two references disagree:

- **Scryfall** appears to include textless cards in `-o:draw` (treat missing as empty) —
  the engine's behavior.
- **The numeric-field decision** (2026-06-12) went the other way: attribute filters only
  match cards that *have* the attribute, even under negation, matching SQL NULL
  semantics — the engine implements this with tri-state evaluation.

Whether text should follow the numeric rule (missing → unknown → excluded under
negation) or the Scryfall text behavior (missing → empty string) needs an empirical
check against Scryfall before picking a side.

## How prevalent is it?

Only cards whose import wrote NULL rather than `''` are affected. Check before
investing: if the importer normalizes missing `oracle_text`/`flavor_text` to `''`
(or the columns are NOT NULL in practice), both paths already agree and this issue
collapses to documentation.

```sql
SELECT count(*) FILTER (WHERE oracle_text IS NULL) AS null_oracle,
       count(*) FILTER (WHERE flavor_text IS NULL) AS null_flavor
FROM magic.cards;
```

## Options

1. **Normalize at import** — write `''` instead of NULL for text columns (and backfill).
   Both paths then agree with the engine's current behavior, matching Scryfall.
   Cheapest; no query-time cost; loses the NULL/empty distinction nobody queries on.
2. **Tri-state the engine's text fields** — store `Option<String>`/sentinel ids for the
   nullable columns and return unknown from text filters when absent, matching SQL.
   Mirrors the numeric decision but likely diverges from Scryfall for text.
3. **COALESCE in SQL** — `coalesce(oracle_text, '')` in the generated fragments,
   matching the engine. Same outcome as (1) without touching data; small planner risk
   for the trigram/text indexes (expression no longer matches the indexed column).

## Verification

Cross-path parity suite (see [pr-490-review-todos](../prs/pr-490-review-todos.md)):
load fixture cards with NULL and `''` text into the DB and engine, run negated text
queries through both `_search_sql` and `_search_engine`, assert identical card sets.
