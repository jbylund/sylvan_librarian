-- Every illustration a printing SHOWS, front first -- not just the front face's.
--
-- `illustration_id` is the FRONT face's once faces merge into one row (#894, matching Scryfall's
-- own top-level field), so a double-faced card's back art had no column at all. `card_art_tags` is
-- attached by joining on it (api/tag_import.py), which made a tag that exists only on the back art
-- unreachable by any query: `arttag:snow e:khm` is 75 on Scryfall and was 73 here, missing
-- Birgi // Harnfel and Esika // The Prismatic Bridge.
--
-- jsonb rather than uuid[] to match the other list columns (card_types, card_subtypes) and, more to
-- the point, because api/db/bulk_upsert.py maps a column's declared type through _PG_TYPE: jsonb is
-- extracted with `obj->`, while an ARRAY column falls through to text and would fail the insert.
ALTER TABLE magic.cards ADD COLUMN illustration_ids jsonb DEFAULT '[]'::jsonb NOT NULL;
ALTER TABLE magic.cards ADD CONSTRAINT illustration_ids_must_be_array CHECK (jsonb_typeof(illustration_ids) = 'array');

COMMENT ON COLUMN magic.cards.illustration_ids IS
    'Every illustration this printing shows, front first, deduped: illustration_id plus each face''s. '
    'The join key for card_art_tags -- a card answers for every face it shows, not just its front.';

-- Backfill from what the row already carries, so the next art-tag import finds a populated column
-- whether or not a card import has run since. A `card_faces` array in the blob is what a merged
-- multi-face row looks like; every other row backfills to the single-element list its
-- illustration_id already implies, which is exactly what preprocess_card writes for it.
WITH shown AS (
    SELECT
        c.scryfall_id,
        face.illustration_id,
        MIN(face.position) AS position
    FROM magic.cards AS c
    CROSS JOIN LATERAL (
        SELECT c.illustration_id::text AS illustration_id, 0 AS position
        UNION ALL
        SELECT element.value->>'illustration_id', element.position::int
        FROM jsonb_array_elements(COALESCE(c.raw_card_blob->'card_faces', '[]'::jsonb))
            WITH ORDINALITY AS element(value, position)
    ) AS face
    WHERE face.illustration_id IS NOT NULL
    GROUP BY c.scryfall_id, face.illustration_id
)
UPDATE magic.cards
SET illustration_ids = shown_ids.illustration_ids
FROM (
    SELECT scryfall_id, jsonb_agg(illustration_id ORDER BY position) AS illustration_ids
    FROM shown
    GROUP BY scryfall_id
) AS shown_ids
WHERE magic.cards.scryfall_id = shown_ids.scryfall_id;
