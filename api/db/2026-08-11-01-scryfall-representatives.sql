-- Scryfall's own choice of representative printing, one row per oracle_id.
--
-- Its `oracle_cards` bulk dump contains exactly one card object per oracle_id, and that object IS
-- the printing Scryfall shows for the card. That makes it ~38k external labels for the question
-- prefer_score answers -- free, refreshed daily, and far larger than any grading session can be.
--
-- A side table rather than a column on magic.cards: it is written wholesale each import (DELETE +
-- INSERT), it is one narrow row per card against 95k+ card rows, and keeping it separate means the
-- backfill can LEFT JOIN it and simply score every card as unlabelled when the table is empty --
-- which is what makes the extra dump an OPTIONAL input rather than a hard dependency.
CREATE TABLE IF NOT EXISTS magic.scryfall_representatives (
    scryfall_id uuid PRIMARY KEY
);

COMMENT ON TABLE magic.scryfall_representatives IS
    'scryfall_ids Scryfall''s oracle_cards dump names as a card''s representative printing; '
    'consumed by the prefer_score backfill. Empty is valid and means "no labels available".';
