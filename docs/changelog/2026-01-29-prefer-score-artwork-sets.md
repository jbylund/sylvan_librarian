# Prefer Score: Bonus for Excluding Black/White Artwork Sets

**Date:** 2026-01-29

## Overview

Enhanced the prefer score calculation to include a preference for full-color artwork over black/white artwork sets, ensuring that full-color card printings rank higher than their black/white counterparts when multiple versions exist.

## New Component

### Artwork Set Preference

Cards from sets with full-color artwork receive a bonus in the prefer score:

- **Black/white artwork sets (e.g., dbl)**: 0 points (no bonus)
- **Full-color artwork sets (all others)**: +20 points (strong preference)

This gives a strong preference to full-color card versions while still allowing other scoring factors (frame, language, rarity, etc.) to influence the final ordering.

## Background

The **dbl** set contains black/white artwork that differs from the standard full-color artwork found in most Magic: The Gathering sets. When multiple printings of the same card exist, collectors and players generally prefer the full-color versions for their richer visual presentation.

## Examples

### Lightning Bolt from Different Sets

For different printings of the same card across sets:

- **Lightning Bolt (M21, full-color)**: +20 points (artwork_set)
- **Lightning Bolt (dbl, black/white)**: 0 points (artwork_set)

Combined with other prefer score components (frame version, language, rarity, etc.), this ensures proper ordering among different printings with the full-color version ranking higher.

### Extensibility

The implementation uses an extensible exclusion list approach, allowing additional black/white artwork sets to be added in the future:

```sql
WHEN card_set_code NOT IN ('dbl') THEN 20
```

If future sets also use black/white artwork, they can be easily added to the exclusion list:

```sql
WHEN card_set_code IS NULL OR card_set_code NOT IN ('dbl', 'other_bw_set') THEN 20
```

## Technical Details

### Database Schema

The new component is stored in the existing `prefer_score_components` JSONB field:

```json
{
  "artwork_set": 20,
  ...other components...
}
```

### SQL Implementation

Located in `api/sql/backfill_prefer_scores.sql`:

```sql
'artwork_set', (
    SELECT
        CASE
            WHEN card_set_code IS NULL OR card_set_code NOT IN ('dbl') THEN 20
            ELSE 0
        END
)
```

### Data Source

The component reads from the `card_set_code` column in the `magic.cards` table:

- Cards with `card_set_code = 'dbl'` receive 0 points
- All other cards (including those with `NULL` set codes) receive 20 points

## Testing

- **New test method** `test_artwork_set_component_logic` with multiple assertions
- Tests verify:
  - Cards from 'dbl' set get 0 points
  - Cards from other sets (iko, thb, m21) get 20 points
  - Cards with no set code get 20 points (preferred over black/white sets)
  - Overall preference ordering in `test_preference_ordering`

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

The new component fits into the overall prefer score system:

| Component          | Score Range | Weight     |
| ------------------ | ----------- | ---------- |
| Language (English) | 0-40        | High       |
| Frame version      | 0-42        | High       |
| Illustration count | 0-23        | Medium     |
| **Artwork Set**    | **0-20**    | **Medium** |
| Border color       | 0-14        | Medium     |
| Rarity             | 0-16        | Medium     |
| High-res scan      | 0-16        | Medium     |
| Extended art       | 0-12        | Low        |
| Finish             | 0-10        | Low        |
| Has paper          | 0-6         | Low        |
| Legendary frame    | 0-5         | Very Low   |

## Future Considerations

- Additional black/white artwork sets can be easily added to the exclusion list
- User-configurable preferences for artwork style could be implemented
- Similar mechanisms could be added for other artwork variations (e.g., showcase frames, borderless art)
