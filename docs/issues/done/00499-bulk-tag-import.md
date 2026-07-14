# Migrate oracle and art tag imports to Scryfall bulk data

## Problem

The current oracle tag import works by:

1. Discovering tag slugs by scraping `scryfall.com/docs/tagger-tags` or paginating the
   tagger GraphQL `SearchTags` query.
2. For each tag slug, calling the Scryfall search API (`oracletag:<slug>`) to get the
   matching card names.
3. UPDATEing `magic.cards.card_oracle_tags` matched on `card_name`.

The hierarchy is populated separately via `_get_tag_relationships()`, which calls the
tagger GraphQL `FetchTag` query — one request per tag — to get ancestry and children.

Problems:

- **Undocumented API**: the tagger GraphQL endpoint is not part of Scryfall's public API.
  It requires session auth (CSRF token) and can break silently.
- **Slow**: ~0.5 s enforced between requests × ~4,500 oracle tags = over an hour per import.
  The hierarchy pass is an additional ~4,500 calls on top.

Art tags (illustration-scoped) are currently excluded entirely (`artwork` and `print`
namespaces are filtered out of GraphQL results), so there is no import path for them at all.

## New data available

Scryfall now publishes official bulk downloads for both tag types, listed at
`https://api.scryfall.com/bulk-data`:

- **`oracle_tags`** — 4,490 objects, one per oracle tag
- **`art_tags`** — 11,305 objects, one per illustration tag

Each object includes the complete tag metadata, the full set of card/artwork associations
(as UUIDs), and the parent/child hierarchy — all in one download.

### Oracle tag object shape

```json
{
  "object": "tag",
  "id": "<uuid>",
  "slug": "flying",
  "label": "flying",
  "type": "oracle",
  "description": "...",
  "parent_ids": ["<uuid>"],
  "child_ids": [],
  "taggings": [
    { "oracle_id": "<uuid>", "weight": "median" }
  ]
}
```

`oracle_id` maps directly to `magic.cards.oracle_id`.

### Art tag object shape

```json
{
  "object": "tag",
  "id": "<uuid>",
  "slug": "dragon",
  "type": "illustration",
  "parent_ids": ["<uuid>"],
  "child_ids": [],
  "taggings": [
    { "illustration_id": "<uuid>", "weight": "very_strong" }
  ]
}
```

`illustration_id` maps directly to `magic.cards.illustration_id`.

## Schema changes

### New tables (separate namespaces for oracle vs. art)

Rename the existing tag tables from the generic names to oracle-specific ones:

| Old name                | New name                       |
|-------------------------|--------------------------------|
| `magic.tags`            | `magic.oracle_tags`            |
| `magic.tag_relationships` | `magic.oracle_tag_relationships` |

Add art tag tables with the same structure:

```sql
CREATE TABLE magic.art_tags (
    tag text NOT NULL PRIMARY KEY
);

CREATE TABLE magic.art_tag_relationships (
    child_tag text NOT NULL,
    parent_tag text NOT NULL,
    PRIMARY KEY (child_tag, parent_tag),
    CONSTRAINT no_self_reference CHECK (child_tag <> parent_tag)
);
-- same circular-reference trigger as oracle_tag_relationships
```

### New column on magic.cards

```sql
ALTER TABLE magic.cards
    ADD COLUMN card_art_tags jsonb DEFAULT '{}'::jsonb NOT NULL;

-- GIN index to support art: filter queries
CREATE INDEX idx_cards_art_tags_gin ON magic.cards USING gin (card_art_tags);
```

### New indexes on join columns

```sql
-- needed for the batch UPDATE WHERE oracle_id = ANY(...)
CREATE INDEX idx_cards_oracle_id ON magic.cards USING btree (oracle_id);

-- needed for the batch UPDATE WHERE illustration_id = ANY(...)
CREATE INDEX idx_cards_illustration_id ON magic.cards USING btree (illustration_id);
```

## Proposed import flow

### Step 1 — download

Resolve the download URIs from `https://api.scryfall.com/bulk-data` and stream both JSON
files. The files are similar in size to `default-cards` so downloading into memory is fine.

### Step 2 — build a UUID → slug map

Both files use tag UUIDs in `parent_ids` / `child_ids`. Build a `{id: slug}` dict from all
tag objects before doing anything else.

### Step 3 — populate hierarchy tables

For oracle tags:

1. Bulk `INSERT INTO magic.oracle_tags (tag) VALUES … ON CONFLICT DO NOTHING` for all slugs.
2. Resolve `parent_ids` to parent slugs using the UUID map and insert into
   `magic.oracle_tag_relationships`.

Repeat with `magic.art_tags` / `magic.art_tag_relationships` for art tags.

### Step 4 — update card columns

**Invert first, then batch.** Rather than iterating tag-by-tag, build a Python dict of
`oracle_id → frozenset(slugs)` by iterating all 4,490 tag objects and their taggings.
35,549 distinct oracle IDs appear in the file; at batch size 500 that is ~71 UPDATE
statements:

```sql
UPDATE magic.cards AS c
SET card_oracle_tags = t.tags
FROM (VALUES %s) AS t(oracle_id uuid, tags jsonb)
WHERE c.oracle_id = t.oracle_id
  AND c.card_oracle_tags IS DISTINCT FROM t.tags
```

Cards that previously had tags but no longer appear in the bulk file need to be cleared.
After building the `oracle_id → tags` dict, query the DB for the (small) set of oracle_ids
that currently have non-empty tags, subtract the ones present in the bulk file in Python,
and UPDATE only the remainder:

```python
rows = cursor.execute(
    "SELECT oracle_id FROM magic.cards WHERE card_oracle_tags != '{}' AND oracle_id IS NOT NULL"
)
tagged_in_db = {r["oracle_id"] for r in rows}
to_clear = tagged_in_db - set(oracle_id_to_tags)   # oracle_ids that lost all their tags
if to_clear:
    cursor.execute(
        "UPDATE magic.cards SET card_oracle_tags = '{}' WHERE oracle_id = ANY(%s)",
        [list(to_clear)],
    )
```

`to_clear` is typically empty or tiny (only cards removed from Tagger since the last import),
so the UPDATE touches essentially nothing on routine runs.

Repeat the same inversion + batch UPDATE for art tags using `illustration_id` /
`card_art_tags`.

> **Why not group by tag-set?** The data is highly sparse: of 31,696 distinct tag-set
> combinations, 30,359 have exactly one card. Grouping by combination yields ~31k UPDATEs,
> no better than tag-by-tag. The card-centric batch gives ~71 UPDATEs instead.

## What can be deleted

- `api/tagger_client.py` — entire file
- `discover_tags_from_scryfall()`, `discover_tags_from_graphql()` in `api_resource.py`
- `_get_tag_relationships()`, `_populate_tag_hierarchy()` in `api_resource.py`
- `update_tagged_cards()` — replace with new bulk endpoint
- `api/tests/test_tagger_client.py`

## Planned PRs

**PR 1 (base)** — DB schema + import only:
- Rename `magic.tags` / `magic.tag_relationships` → `oracle_tags` / `oracle_tag_relationships`
- Update `get_tag_ancestors()` / `get_tag_descendants()` DB functions to reference the renamed table
- Add `magic.art_tags`, `magic.art_tag_relationships`, `magic.cards.card_art_tags`
- Add indexes on `oracle_id`, `illustration_id`
- Rewrite import using bulk data; delete `TaggerClient` and old helpers

**PR 2 (on top of PR 1)** — parser / query support:
- Add `art:<slug>` filter syntax (maps to `card_art_tags @> '{"<slug>": true}'`)
- Update `oracle_tags:` filter if any join table lookups change with the rename
- Update `docs/technical/card_tagging.md`
