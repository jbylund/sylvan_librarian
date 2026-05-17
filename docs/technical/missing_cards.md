# Missing Cards Detection and Import

This document describes the new functionality for detecting and importing missing cards by comparing Scryfall with the local database.

## Overview

The system provides two main components:

1. **Script**: `scripts/find_missing_cards.py` - Automatically detects missing cards
1. **API Endpoint**: `import_card_by_name` - Imports individual cards by name

## Script Usage

### Running the Script

```bash
# Run the missing cards detection script
python scripts/find_missing_cards.py
```

### Configuration

Edit the script to configure:

```python
# Configuration
scryfall_base = "https://api.scryfall.com"
local_api_base = "http://localhost:8080"  # or "http://crestcourt.scryfall.com"
```

### What the Script Does

1. Generates 100 search queries covering:
   - All 5 colors (w,u,b,r,g)
   - CMC values 0-9
   - Creature vs non-creature cards
   - Required filters: `-is:dfc -is:adventure -is:split game:paper (f:m or f:l or f:c or f:v)`

1. For each query:
   - Searches Scryfall API
   - Searches local database
   - Identifies missing cards
   - Automatically imports missing cards

### Example Queries Generated

```
color:w cmc=0 t:creature -is:dfc -is:adventure -is:split game:paper (f:m or f:l or f:c or f:v)
color:w cmc=0 -t:creature -is:dfc -is:adventure -is:split game:paper (f:m or f:l or f:c or f:v)
color:u cmc=1 t:creature -is:dfc -is:adventure -is:split game:paper (f:m or f:l or f:c or f:v)
...
```

## API Endpoint Usage

### Import Single Card

```bash
# Import a specific card by name
curl "http://localhost:8080/import_card_by_name?card_name=Lightning%20Bolt"
```

### Response Examples

**Success:**

```json
{
  "card_name": "Lightning Bolt",
  "status": "success",
  "message": "Card 'Lightning Bolt' successfully imported"
}
```

**Already Exists:**

```json
{
  "card_name": "Lightning Bolt",
  "status": "already_exists",
  "message": "Card 'Lightning Bolt' already exists in database"
}
```

**Not Found:**

```json
{
  "card_name": "Nonexistent Card",
  "status": "not_found",
  "message": "Card 'Nonexistent Card' not found in Scryfall API"
}
```

**Filtered Out:**

```json
{
  "card_name": "Some Card",
  "status": "filtered_out",
  "message": "Card 'Some Card' was filtered out during preprocessing (not legal in supported formats or not paper)"
}
```

## Rate Limiting

- Script includes 100ms delays between Scryfall API calls (10 requests/second max)
- Respects Scryfall's rate limiting guidelines
- Includes error handling for API failures

## Supported Filters

The system automatically filters cards to only include:

- Paper Magic cards (`game:paper`)
- Cards legal in at least one major format (`f:m or f:l or f:c or f:v`)
- Excludes double-faced cards (`-is:dfc`)
- Excludes adventure cards (`-is:adventure`)
- Excludes split cards (`-is:split`)

## Error Handling

The system handles various error conditions:

- Network failures
- API rate limiting
- Invalid card names
- Database connection issues
- Cards filtered during preprocessing

All errors are logged with appropriate detail for debugging.

## Testing

Run the test suite to verify functionality:

```bash
# Run all tests including new import functionality
python -m pytest api/tests/test_import_card_by_name.py -v

# Run full test suite
python -m pytest -v
```
