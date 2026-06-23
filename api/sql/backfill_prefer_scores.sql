-- Backfill prefer_score and prefer_score_components for all cards
-- This script recalculates the prefer score for all existing cards based on multiple attributes

WITH computed_components AS (
    SELECT
        scryfall_id,
        JSONB_BUILD_OBJECT(
            'illustration_count', (
                SELECT
                    (23 * LN(1 + COUNT(*)) / LN(40))::real
                FROM magic.cards query_target_cards
                WHERE (
                    query_target_cards.illustration_id = source.illustration_id AND
                    query_target_cards.illustration_id IS NOT NULL AND
                    query_target_cards.card_name = source.card_name
                )
            ),
            'rarity', (
                CASE
                    WHEN card_rarity_int = 0 THEN 16  -- common
                    WHEN card_rarity_int = 1 THEN 16  -- uncommon
                    WHEN card_rarity_int = 2 THEN 11  -- rare
                    WHEN card_rarity_int = 3 THEN 0   -- mythic
                    ELSE 0
                END
            ),
            'border', (
                CASE
                    WHEN card_border = 'black' THEN 14
                    WHEN card_border = 'white' THEN 0
                    WHEN card_border = 'borderless' THEN 0
                    ELSE 0
                END
            ),
            'frame', (
                CASE
                    WHEN card_frame_data ? '2015' THEN 42
                    WHEN card_frame_data ? '2003' THEN 30
                    WHEN card_frame_data ? '1997' THEN 25
                    WHEN card_frame_data ? '1993' THEN 10
                    ELSE 0
                END
            ),
            'extended_art', (
                CASE
                    WHEN card_frame_data ? 'Extendedart' THEN 12
                    ELSE 0
                END
            ),
            'highres_scan', (
                CASE
                    WHEN raw_card_blob ->> 'image_status' = 'highres_scan' THEN 16
                    ELSE 0
                END
            ),
            'has_paper', (
                CASE
                    WHEN raw_card_blob -> 'games' ? 'paper' THEN 6
                    ELSE 0
                END
            ),
            'language', (
                CASE
                    WHEN raw_card_blob ->> 'lang' = 'en' THEN 40
                    ELSE 0
                END
            ),
            'legendary_frame', (
                CASE
                    WHEN raw_card_blob -> 'frame_effects' ? 'legendary' THEN 5
                    ELSE 0
                END
            ),
            'non_showcase', (
                CASE
                    WHEN NOT (COALESCE(raw_card_blob -> 'frame_effects', '[]'::jsonb) ? 'showcase') THEN 10
                    ELSE 0
                END
            ),
            'finish', (
                CASE
                    WHEN raw_card_blob -> 'finishes' ? 'nonfoil' THEN 10
                    WHEN raw_card_blob -> 'finishes' ? 'foil' THEN 5
                    WHEN raw_card_blob -> 'finishes' ? 'etched' THEN 0
                    ELSE 0
                END
            ),
            'artwork_set', (
                CASE
                    WHEN card_set_code IS NULL OR card_set_code NOT IN ('dbl') THEN 20
                    ELSE 0
                END
            )
        ) AS new_components
    FROM magic.cards source
),
computed_scores AS (
    SELECT
        scryfall_id,
        new_components,
        (
            SELECT
                SUM(value::numeric)
            FROM
                jsonb_each(new_components)
        )::real AS new_score
    FROM computed_components
)
UPDATE magic.cards
SET
    prefer_score_components = computed_scores.new_components,
    prefer_score = computed_scores.new_score
FROM computed_scores
WHERE magic.cards.scryfall_id = computed_scores.scryfall_id
  AND magic.cards.prefer_score IS DISTINCT FROM computed_scores.new_score;
