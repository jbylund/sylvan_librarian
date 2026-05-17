# Prefer Score: Legendary Frame and Finishes Support

**Date:** 2025-11-15

## Overview

Enhanced the prefer score calculation to include card finishing and legendary frame effects, providing more nuanced card preference ordering that aligns with collector and player preferences.

## New Components

### Legendary Frame Effect

Cards with the legendary frame effect receive a small bonus in the prefer score:

- **Normal frame**: 0 points (baseline)
- **Legendary frame**: +5 points (weak preference)

This gives a slight preference to legendary-framed versions of cards while maintaining the influence of other scoring factors.

### Card Finishes

Card finishes now factor into prefer score with clear ordering:

- **Etched**: 0 points (least preferred)
- **Foil**: 5 points (middle preference)
- **Non-foil**: 10 points (most preferred)

This ordering reflects the general preference for non-foil cards for readability and gameplay, while still allowing foil and etched versions as alternatives.

## Examples

### Talrand, Sky Summoner (2X2 - Etched)

This etched legendary card would receive:

- Legendary frame: +5 points
- Etched finish: 0 points
- **Total from new components**: 5 points

Combined with other prefer score components (frame version, language, rarity, etc.), this ensures proper ordering among different printings.

### Lightning Bolt Variants

For different printings of the same card:

- Non-foil version: +10 points (finish)
- Foil version: +5 points (finish)
- Etched version: 0 points (finish)

## Technical Details

### Database Schema

The new components are stored in the existing `prefer_score_components` JSONB field:

```json
{
  "legendary_frame": 5,
  "finish": 10,
  ...other components...
}
```

### SQL Implementation

Located in `api/sql/backfill_prefer_scores.sql`:

```sql
'legendary_frame', (
    SELECT
        CASE
            WHEN raw_card_blob -> 'frame_effects' ? 'legendary' THEN 5
            ELSE 0
        END
),
'finish', (
    SELECT
        CASE
            WHEN raw_card_blob -> 'finishes' ? 'nonfoil' THEN 10
            WHEN raw_card_blob -> 'finishes' ? 'foil' THEN 5
            WHEN raw_card_blob -> 'finishes' ? 'etched' THEN 0
            ELSE 0
        END
)
```

### Data Source

Both components read from the Scryfall card data stored in `raw_card_blob`:

- `frame_effects`: Array field containing frame effect keywords (e.g., ["legendary", "etched"])
- `finishes`: Array field containing available finishes (e.g., ["foil"], ["nonfoil"], ["etched"])

## Testing

- **3 new unit tests** validating component scoring logic
- **746 total tests pass** with no regressions
- Tests verify:
  - Legendary frame scoring (0 vs 5 points)
  - Finish scoring (0, 5, 10 points)
  - Overall preference ordering

## Migration

The prefer score backfill script automatically recalculates scores for all cards. To update existing data:

```sql
-- Run the backfill script
\i api/sql/backfill_prefer_scores.sql
```

Or via the API endpoint:

```bash
curl -X POST http://localhost:8080/backfill_prefer_scores
```

## Scoring Context

The new components fit into the overall prefer score system:

| Component           | Score Range | Weight       |
| ------------------- | ----------- | ------------ |
| Language (English)  | 0-40        | High         |
| Frame version       | 0-42        | High         |
| Illustration count  | 0-23        | Medium       |
| Border color        | 0-14        | Medium       |
| Rarity              | 0-16        | Medium       |
| High-res scan       | 0-16        | Medium       |
| Extended art        | 0-12        | Low          |
| **Finish**          | **0-10**    | **Low**      |
| Has paper           | 0-6         | Low          |
| **Legendary frame** | **0-5**     | **Very Low** |

## Future Considerations

- Additional frame effects (showcase, borderless, etc.) could be added as separate components
- Finish preferences could be made configurable per user
- Support for multiple finishes on the same card printing (though rare in practice)
