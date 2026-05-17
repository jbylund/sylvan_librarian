# Prefer Score: Bonus for Non-Showcase Cards

**Date:** 2026-01-30

## Overview

Enhanced the prefer score calculation to include a preference for non-showcase (standard frame) cards over showcase cards, ensuring that standard frame printings rank higher than showcase printings when multiple versions of the same card exist.

## New Component

### Non-Showcase Preference

Cards without showcase frame effects receive a bonus in the prefer score:

- **Showcase cards**: 0 points (no bonus)
- **Non-showcase cards**: +10 points (standard frame preference)

This gives a preference to standard frame card versions while still allowing other scoring factors (frame version, language, rarity, etc.) to influence the final ordering.

## Background

Showcase cards use alternate frame treatments (e.g., special art frames) and are identified by the presence of `"showcase"` in the `frame_effects` array in the raw card data. While showcase cards can be visually interesting, collectors and players often prefer standard frame versions for their classic presentation and consistency across their collections.

## Detection Logic

A card is considered a showcase card if and only if its `frame_effects` array contains the string `"showcase"`:

- **Cards with `frame_effects` containing `"showcase"`**: Showcase card (0 points)
- **Cards with `frame_effects` not containing `"showcase"`**: Non-showcase card (10 points)
- **Cards with missing or empty `frame_effects`**: Non-showcase card (10 points)

## Examples

### Showcase vs. Non-Showcase Printings

For different printings of the same card:

- **Lightning Bolt (standard frame)**: +10 points (non_showcase)
- **Lightning Bolt (showcase frame)**: 0 points (non_showcase)

Combined with other prefer score components (frame version, language, rarity, etc.), this ensures proper ordering among different printings with the standard frame version ranking higher.

### Frame Effects Combinations

The showcase check is independent of other frame effects:

- **Card with `frame_effects: ["legendary"]`**: +10 points (non-showcase, no showcase in array)
- **Card with `frame_effects: ["showcase", "legendary"]`**: 0 points (showcase, contains showcase)
- **Card with `frame_effects: []`**: +10 points (non-showcase, empty array)
- **Card with no `frame_effects` field**: +10 points (non-showcase, missing field)

## Technical Details

### Database Schema

The new component is stored in the existing `prefer_score_components` JSONB field:

```json
{
  "non_showcase": 10,
  ...other components...
}
```

### SQL Implementation

Located in `api/sql/backfill_prefer_scores.sql`:

```sql
'non_showcase', (
    SELECT
        CASE
            WHEN NOT (COALESCE(raw_card_blob -> 'frame_effects', '[]'::jsonb) ? 'showcase') THEN 10
            ELSE 0
        END
)
```

### Data Source

The component reads from the `raw_card_blob` JSONB column in the `magic.cards` table:

- Uses the JSONB `?` operator to check if the `frame_effects` array contains `"showcase"`
- Uses `COALESCE` to treat missing `frame_effects` as an empty array `[]`
- Cards without showcase in their frame_effects receive 10 points
- Cards with showcase in their frame_effects receive 0 points

## Testing

- **New test method** `test_non_showcase_component_logic` with comprehensive assertions
- Tests verify:
  - Cards with `["showcase"]` get 0 points
  - Cards with `["showcase", "legendary"]` get 0 points
  - Cards with `["legendary"]` get 10 points
  - Cards with `["etched"]` get 10 points
  - Cards with `None` frame_effects get 10 points
  - Cards with `[]` frame_effects get 10 points
  - Overall preference ordering in `test_preference_ordering` (showcase < non-showcase)

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

| Component          | Score Range | Weight   |
| ------------------ | ----------- | -------- |
| Language (English) | 0-40        | High     |
| Frame version      | 0-42        | High     |
| Illustration count | 0-23        | Medium   |
| Artwork Set        | 0-20        | Medium   |
| Border color       | 0-14        | Medium   |
| Rarity             | 0-16        | Medium   |
| High-res scan      | 0-16        | Medium   |
| Extended art       | 0-12        | Low      |
| **Non-Showcase**   | **0-10**    | **Low**  |
| Finish             | 0-10        | Low      |
| Has paper          | 0-6         | Low      |
| Legendary frame    | 0-5         | Very Low |

## Implementation Notes

### JSONB Operator Usage

The implementation uses PostgreSQL's JSONB `?` operator to efficiently test for the presence of a specific string in an array:

- `raw_card_blob -> 'frame_effects' ? 'showcase'` returns `true` if "showcase" is in the array
- The `NOT` operator inverts this to give the bonus when showcase is absent
- `COALESCE` handles the case where `frame_effects` is missing or NULL

### Consistency with Legendary Frame

This component follows the same pattern as the existing `legendary_frame` component, which also checks the `frame_effects` array for a specific value. Both components:

- Read from `raw_card_blob -> 'frame_effects'`
- Use the JSONB `?` operator for detection
- Give a bonus for specific frame characteristics

## Future Considerations

- Similar mechanisms could be added for other frame effects (e.g., borderless, etched)
- User-configurable preferences for frame effects could be implemented
- The scoring values could be adjusted based on user feedback
- Additional special frame treatments could be detected and scored
