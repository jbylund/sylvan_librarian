-- The engine serves searches; Postgres is the fallback for when the engine errors. Anything only
-- raw_card_blob carries is therefore unanswerable on the primary path -- a jsonb column is not in
-- the store, and the store is all an engine request reads. That is why /cards/* was SQL-only.
--
-- Two columns fix it, both shaped like the jsonb grab-bags this table already has (card_frame_data,
-- card_is_tags, card_legalities):
--
--   card_compat_blob -- every Scryfall key that is neither stored in a column of its own nor a pure
--                       function of one. Measured on a real card: 680 bytes against raw_card_blob's
--                       5,881, because the blob is overwhelmingly redundant with columns we already
--                       have plus URLs derivable from the card's id.
--   card_faces       -- each face's own fields, so a face is a thing the engine can read rather
--                       than something recoverable only by re-parsing a blob.
--
-- Both backfill from raw_card_blob, so this deploys without a reimport (same reasoning as
-- 2026-08-06-01-lowercase-keywords.sql: the bulk import would rewrite these anyway, but the
-- query-side change should not wait for a window where /cards/* returns nothing).
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS card_compat_blob jsonb;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS card_faces jsonb;

-- Subtractive rather than enumerated: the residue is defined as "what is left once everything we
-- store or can derive is removed", so a Scryfall key we have never seen lands here by default
-- instead of being silently dropped. Keys removed below are, in order: stored in their own column;
-- derivable from id/set/collector_number/oracle_id; the faces, which get their own column; and the
-- keys preprocess_card adds to the object before it is snapshotted.
--
-- `prices` is deliberately NOT removed even though price_usd/eur/tix are stored: usd_foil,
-- usd_etched and eur_foil are not, and keeping the object whole costs a few bytes against losing
-- three fields.
UPDATE magic.cards
SET card_compat_blob = raw_card_blob - ARRAY[
        'id', 'oracle_id', 'name', 'released_at', 'layout', 'mana_cost', 'cmc', 'type_line',
        'oracle_text', 'power', 'toughness', 'loyalty', 'colors', 'color_identity', 'keywords',
        'set', 'set_name', 'collector_number', 'rarity', 'flavor_text', 'artist',
        'illustration_id', 'border_color', 'edhrec_rank', 'legalities', 'produced_mana',
        'watermark', 'reserved', 'game_changer', 'frame',
        'object', 'uri', 'scryfall_uri', 'image_uris', 'rulings_uri', 'prints_search_uri',
        'set_uri', 'set_search_uri', 'scryfall_set_uri', 'card_back_id', 'related_uris',
        'purchase_uris', 'resource_id',
        'card_faces',
        'card_name', 'face_name', 'face_idx', 'scryfall_id'
    ],
    card_faces = CASE
        WHEN raw_card_blob ? 'card_faces' THEN (
            SELECT jsonb_agg(
                face - ARRAY['object', 'image_uris']
                ORDER BY ordinality
            )
            FROM jsonb_array_elements(raw_card_blob -> 'card_faces') WITH ORDINALITY AS t(face, ordinality)
        )
        ELSE NULL
    END
WHERE card_compat_blob IS NULL;

-- The engine reload reads every ENGINE_COLUMNS value for every row, so a NULL here would mean a
-- per-row branch in the hot path for a case that cannot legitimately occur.
ALTER TABLE magic.cards ALTER COLUMN card_compat_blob SET DEFAULT '{}'::jsonb;
UPDATE magic.cards SET card_compat_blob = '{}'::jsonb WHERE card_compat_blob IS NULL;
ALTER TABLE magic.cards ALTER COLUMN card_compat_blob SET NOT NULL;

ALTER TABLE magic.cards ADD CONSTRAINT card_compat_blob_must_be_object
    CHECK ((jsonb_typeof(card_compat_blob) = 'object'::text));
ALTER TABLE magic.cards ADD CONSTRAINT card_faces_must_be_array
    CHECK (((card_faces IS NULL) OR (jsonb_typeof(card_faces) = 'array'::text)));
