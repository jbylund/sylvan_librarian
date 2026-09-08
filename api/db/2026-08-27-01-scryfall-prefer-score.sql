-- Scryfall's printing preference as its OWN score, separate from prefer_score.
--
-- The value encodes each printing's position in Scryfall's per-card ordering: 0 for the printing
-- their `oracle_cards` dump names as the card's representative, then -1, -2, ... down the rest of
-- the card's printings in their prints-listing order (release date, newest first, with their
-- tie-breaks). Higher-is-preferred matches prefer_score's convention, so the same DESC machinery
-- serves both -- but the two scores never mix: prefer=default reads prefer_score, prefer=scryfall
-- reads this column, and the backfills are independent.
--
-- Run backfill_scryfall_prefer_scores.sql to populate; NULL means "not yet backfilled" and sorts
-- last, exactly like a NULL prefer_score does.
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS scryfall_prefer_score real;

CREATE INDEX IF NOT EXISTS idx_cards_scryfall_prefer_score ON magic.cards (scryfall_prefer_score DESC NULLS LAST);
