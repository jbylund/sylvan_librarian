-- Two keys join card_compat_blob: `resource_id` and a `card_back_id` that is NOT Scryfall's shared
-- back. 2026-08-10-01-engine-card-objects.sql (and api/card_processing.py's _COMPAT_BLOB_EXCLUDED)
-- dropped both as "pure functions of id / set / collector_number / oracle_id, re-emitted on read",
-- and neither is:
--
--   card_back_id -- differs from the shared back 0aeebaf5-... on ~2,600 of the ~98k default
--                   printings (Collectors' Edition's gold back, the oversized and memorabilia sets),
--                   and every card object served one back for all of them.
--   resource_id  -- an opaque hash Scryfall sends on the newest ~6,000 printings (msc/806,
--                   soc/190). Nothing stored derives it.
--
-- The shared back itself stays out: both card-object writers emit it when the row names no other,
-- so storing it would cost ~55 bytes a row for no information.
--
-- Backfilled from raw_card_blob, like the columns in 2026-08-10-01, so the SQL lane -- and the
-- engine, which reads card_compat_blob at every reload -- serve both before the next bulk import
-- rewrites the rows anyway. `||` overwrites the same two keys with the same values, so this is safe
-- to run twice.
UPDATE magic.cards
SET card_compat_blob = card_compat_blob || jsonb_strip_nulls(
        jsonb_build_object(
            'resource_id', raw_card_blob -> 'resource_id',
            'card_back_id', CASE
                WHEN raw_card_blob ->> 'card_back_id' <> '0aeebaf5-8c7d-4636-9e82-8c27447861f7'
                    THEN raw_card_blob -> 'card_back_id'
            END
        )
    )
WHERE raw_card_blob ? 'resource_id'
    OR raw_card_blob ->> 'card_back_id' <> '0aeebaf5-8c7d-4636-9e82-8c27447861f7';
