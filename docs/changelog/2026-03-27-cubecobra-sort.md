# CubeCobra Sort Ordering

**Date:** 2026-03-27  
**PR:** #448

## Overview

Adds `orderby=cubecobra` (also `orderby=community` in the UI) which ranks cards by a combined
score across four community popularity dimensions: EDHREC rank, CubeCobra ELO, cube count, and
pick count.

## Database

New columns on `magic.cards` (`api/db/2026-03-26-01-cubecobra-columns.sql`):

| Column | Type | Description |
|--------|------|-------------|
| `cubecobra_elo` | real | Draft power rating from CubeCobra |
| `cubecobra_cube_count` | integer | Number of cubes the card appears in |
| `cubecobra_pick_count` | integer | Total times picked across CubeCobra drafts |
| `cubecobra_score` | real | Pre-computed combined score; lower is better (0–100) |

Data is keyed by `oracle_id` from CubeCobra and stored per-printing (same approach as
`edhrec_rank`).

## Endpoints

- **`POST /ingest_cubecobra`** — paginates the CubeCobra top-cards API, filters to `oracle_id`s
  already in the DB, bulk-updates the three raw columns via a single
  `UPDATE ... FROM jsonb_to_recordset(...)` CTE, then calls `backfill_cubecobra_scores`.
- **`POST /backfill_cubecobra_scores`** — recomputes `cubecobra_score` for all cards using the
  formula below. Also called automatically after each Scryfall bulk import.

## Score Formula (`api/sql/backfill_cubecobra_scores.sql`)

```sql
w_edhrec     * PERCENT_RANK() OVER (ORDER BY edhrec_rank          ASC  NULLS LAST)
+ w_elo        * PERCENT_RANK() OVER (ORDER BY cubecobra_elo        DESC NULLS LAST)
+ w_cube_count * PERCENT_RANK() OVER (ORDER BY cubecobra_cube_count DESC NULLS LAST)
+ w_pick_count * PERCENT_RANK() OVER (ORDER BY cubecobra_pick_count DESC NULLS LAST)
```

Equal weights (25 each) by default, passed as named SQL parameters and auto-normalised to 0–100.
One score per distinct `card_name` is propagated to all printings. Cards missing CubeCobra data
score worst (100) on those dimensions via `NULLS LAST`.

## API / Frontend

- `CardOrdering.CUBECOBRA` added to `enums.py`, mapped to `cubecobra_score ASC NULLS LAST` in
  `_search()`
- "CubeCobra" option added to the sort dropdown in `index.html`

## Notes

- `cubecobra_score` follows the lower-is-better convention of `edhrec_rank`, so no special-casing
  is needed in the sort direction logic.
- CubeCobra data must be refreshed separately from Scryfall data via `POST /ingest_cubecobra`.
