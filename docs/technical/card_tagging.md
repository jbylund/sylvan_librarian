# Card Tagging System

The API supports importing and managing card tags from Scryfall's tagger system.

## Endpoints

- `update_tagged_cards` — import cards for a specific tag
- `discover_and_import_all_tags` — bulk import all available tags and their card associations

## Usage

```bash
# Import cards for a specific tag
curl "http://localhost:8080/update_tagged_cards?tag=flying"

# Discover and import all tags (cards only, no hierarchy)
curl "http://localhost:8080/discover_and_import_all_tags?import_cards=true&import_hierarchy=false"

# Import tag hierarchy only (no card associations)
curl "http://localhost:8080/discover_and_import_all_tags?import_cards=false&import_hierarchy=true"

# Full import: all tags, cards, and hierarchy relationships
curl "http://localhost:8080/discover_and_import_all_tags?import_cards=true&import_hierarchy=true"
```

## Database Schema

- **`magic.cards.card_tags`** (jsonb) — tag associations per card
- **`magic.card_tags`** table — tag hierarchy with parent-child relationships (circular-reference trigger enforced)

## Rate Limiting

The bulk import includes built-in rate limiting:

- 200ms delay between individual tag imports
- 500ms delay between hierarchy relationship requests
- Progress logging every 50 tags processed
