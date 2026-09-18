-- The multilingual store: magic.cards grows from default_cards (~114k rows, English plus the
-- handful of foreign printings Scryfall itself names canonical) to all_cards (~540k rows, every
-- language). Four column groups make that searchable without changing what a default query sees:
--
--   card_lang           -- the printing's language, lowercased like card_layout/card_border; the
--                          lang: operator's column. `lang` itself stays in card_compat_blob (the
--                          card object reads it from there); this is the search column.
--   printed_name / printed_type_line / printed_text
--                       -- Scryfall's printed-language triple, verbatim, NULL when the key was
--                          absent on the printing (absence round-trips: never English-filled).
--                          The per-face halves ride the card_faces records.
--   printed_name_folded -- the full printed name ("Front // Back"), lowercased and accent-folded
--                          exactly like card_name_folded: the printed-name lookup key.
--   is_canonical        -- whether Scryfall's default_cards names this printing: id-membership,
--                          decided at import, never re-derived. Canonical rows are the engine's
--                          printing space and the SQL lanes' default result space; the rest are
--                          the engine's foreign annex, reachable only via lang: /
--                          include_multilingual / the by-id addressings.
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS card_lang text;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS printed_name text;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS printed_type_line text;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS printed_text text;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS printed_name_folded text;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS is_canonical boolean NOT NULL DEFAULT true;

-- Backfill from what the rows already carry, so this deploys without a reimport (the same
-- reasoning as 2026-08-10-01-engine-card-objects.sql). Every pre-multilingual row came from
-- default_cards, so the DEFAULT true above IS the backfill for is_canonical; the printed columns
-- stay NULL until the first all_cards import (default_cards' foreign canonicals will fill them).
UPDATE magic.cards
SET card_lang = lower(raw_card_blob ->> 'lang')
WHERE card_lang IS NULL AND raw_card_blob ? 'lang';

-- Equality-only lookups, like card_layout's.
CREATE INDEX IF NOT EXISTS idx_cards_lang ON magic.cards USING hash (card_lang) WHERE (card_lang IS NOT NULL);
ALTER TABLE magic.cards ADD CONSTRAINT check_card_lang_lowercase
    CHECK (((card_lang IS NULL) OR (card_lang = lower(card_lang))));

COMMENT ON COLUMN magic.cards.card_lang IS 'Printing language code (en, ja, ...) - stored in lowercase';
COMMENT ON COLUMN magic.cards.is_canonical IS 'Whether Scryfall default_cards names this printing (id-membership, stamped at import)';
