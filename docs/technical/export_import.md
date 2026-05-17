# Card Data Export/Import

This document describes the card data export and import functionality that allows you to backup and restore the three main database tables.

## Overview

The export/import functionality provides a way to:

- Export card data to JSON files for backup purposes
- Import previously exported data to restore database state
- Transfer data between instances of the application

## Tables Included

The following tables are included in export/import operations:

- **`magic.cards`** - All card data including JSONB fields
- **`magic.tags`** - Tag definitions
- **`magic.tag_relationships`** - Tag hierarchy relationships

## API Endpoints

### Export Data

**Endpoint:** `GET /export_card_data`

Exports all card data to timestamped JSON files in `/data/api/exports/{timestamp}/` directory.

**Example:**

```bash
curl "http://localhost:8080/export_card_data"
```

**Response:**

```json
{
  "status": "success",
  "export_directory": "/data/api/exports/20241001_143052",
  "timestamp": "20241001_143052",
  "results": {
    "cards": { "file": "/data/api/exports/20241001_143052/cards.json", "count": 25847 },
    "tags": { "file": "/data/api/exports/20241001_143052/tags.json", "count": 142 },
    "tag_relationships": { "file": "/data/api/exports/20241001_143052/tag_relationships.json", "count": 85 }
  },
  "message": "Successfully exported 25847 cards, 142 tags, and 85 tag relationships"
}
```

### Import Data

**Endpoint:** `GET /import_card_data[?timestamp=YYYYMMDD_HHMMSS]`

Imports card data from JSON files, truncating existing data first.

**Parameters:**

- `timestamp` (optional) - Specific export timestamp to import. If not provided, uses the most recent export.

**Examples:**

```bash
# Import latest export
curl "http://localhost:8080/import_card_data"

# Import specific timestamp
curl "http://localhost:8080/import_card_data?timestamp=20241001_143052"
```

**Response:**

```json
{
  "status": "success",
  "timestamp": "20241001_143052",
  "import_directory": "/data/api/exports/20241001_143052",
  "results": {
    "tags": 142,
    "tag_relationships": 85,
    "cards": 25847
  },
  "message": "Successfully imported 25847 cards, 142 tags, and 85 tag relationships"
}
```

## File Structure

Each export creates a timestamped directory containing three JSON files:

```
/data/api/exports/
├── 20241001_143052/
│   ├── cards.json
│   ├── tags.json
│   └── tag_relationships.json
├── 20241001_120000/
│   ├── cards.json
│   ├── tags.json
│   └── tag_relationships.json
└── ...
```

### File Formats

**cards.json** - Contains all card data as an array of objects:

```json
[
  {
    "card_name": "Lightning Bolt",
    "cmc": 1,
    "mana_cost_text": "{R}",
    "mana_cost_jsonb": { "R": 1 },
    "raw_card_blob": { "name": "Lightning Bolt", "type_line": "Instant" },
    "card_types": ["Instant"],
    "card_subtypes": [],
    "card_colors": { "R": true },
    "card_color_identity": { "R": true },
    "card_keywords": {},
    "oracle_text": "Deal 3 damage to any target.",
    "edhrec_rank": 1,
    "creature_power": null,
    "creature_power_text": null,
    "creature_toughness": null,
    "creature_toughness_text": null,
    "card_oracle_tags": {}
  }
]
```

**tags.json** - Contains tag definitions as an array of objects:

```json
[{ "tag": "haste" }, { "tag": "flying" }, { "tag": "trample" }]
```

**tag_relationships.json** - Contains tag hierarchy as an array of objects:

```json
[
  { "child_tag": "haste", "parent_tag": "keyword" },
  { "child_tag": "flying", "parent_tag": "keyword" },
  { "child_tag": "trample", "parent_tag": "keyword" }
]
```

## Important Notes

### Data Safety

- **Import truncates all existing data** in the three tables before importing
- Always verify you have a recent export before importing
- The import operation is transactional - it will rollback on errors

### JSON Benefits

- Native support for JSONB columns without string conversion
- Preserves data types and structure
- More readable and editable than CSV format
- Better compatibility with modern tooling

### Docker Volumes

- The `/data/api/exports/` directory is mounted as a Docker volume
- Exports persist between container restarts
- You can access exported files from the host system

### Performance

- Export processes data in batches for memory efficiency
- Import uses individual INSERT statements for precise control
- Large datasets may take several minutes to export/import

## Error Handling

Common error scenarios and responses:

**No exports directory:**

```json
{
  "status": "error",
  "message": "No exports directory found at /data/api/exports"
}
```

**Missing timestamp:**

```json
{
  "status": "error",
  "message": "Export directory for timestamp 20241001_999999 not found"
}
```

**Missing files:**

```json
{
  "status": "error",
  "message": "Missing required files: tags.json, tag_relationships.json"
}
```

## Use Cases

### Backup and Restore

```bash
# Create backup
curl "http://localhost:8080/export_card_data"

# Later, restore from backup
curl "http://localhost:8080/import_card_data?timestamp=20241001_143052"
```

### Data Migration

```bash
# On source instance
curl "http://source.example.com:8080/export_card_data"

# Copy files to destination instance's /data/api/exports/ directory

# On destination instance
curl "http://destination.example.com:8080/import_card_data"
```

### Development Reset

```bash
# Export current state
curl "http://localhost:8080/export_card_data"

# Make changes, test, then restore
curl "http://localhost:8080/import_card_data"
```
