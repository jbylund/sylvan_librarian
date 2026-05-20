# Color Identity Query Performance

**Date:** 2026-05-19  
**PR:** #469

## Overview

Color identity subset queries (`id:wub`, `id<=rg`, etc.) were doing sequential scans. Speed
improved from ~160 ms to ~22 ms by switching from a JSONB `<@` containment check to a bitmask
`= ANY(array)` lookup backed by a B-tree expression index.

## Root Cause

The old code emitted `card_color_identity <@ %(param)s`. PostgreSQL's `jsonb_ops` GIN index
supports `@>` but not `<@`, so the `:` / `<=` / `<` color identity paths fell back to a full scan.
The `idx_cards_color_identity_gin` index had zero scans in `pg_stat_user_indexes`.

## Fix

### Database (`api/db/2026-05-19-01-color-identity-mask.sql`)

An immutable SQL function computes a 5-bit bitmask from a color identity JSONB value, and a
B-tree expression index is built on it:

```sql
CREATE FUNCTION magic.color_identity_mask(jsonb) RETURNS smallint ...
    -- W=16, U=8, B=4, R=2, G=1
CREATE INDEX idx_cards_color_identity_mask
    ON magic.cards (magic.color_identity_mask(card_color_identity));
```

### `card_query_nodes.py`

Three helpers compute the bitmask and enumerate all valid subset (or proper-subset) masks for a
query value. The emitted SQL calls the function explicitly so the planner matches the expression
index:

```python
return f"(magic.color_identity_mask(card.card_color_identity) = ANY(%({pmask})s))"
```

`=`, `>=`, and `>` operators are unchanged — they use `@>` against the JSONB column and the
existing GIN index serves them.

### `db_utils.py` — `IntArray`

psycopg serializes plain Python lists as JSONB. `IntArray` (a `list` subclass) bypasses this so
the integer subset arrays reach PostgreSQL as native integer arrays, which `= ANY(...)` requires.

## Results

| Query    | Before  | After  |
|----------|---------|--------|
| `id:w`   | ~39 ms  | ~13 ms |
| `id:wub` | ~160 ms | ~22 ms |

## Edge Cases

- `id:c` / `id:colorless` — colorless mask=0, subsets=[0]; correctly returns only colorless cards.
- `id<rg` — proper subsets of RG (mask=3) are [0, 1, 2]; does not include `id=rg` itself.
