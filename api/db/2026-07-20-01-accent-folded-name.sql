-- Precomputed accent-folded lowercase card name, so fuzzy `name:` search matches
-- both accented and unaccented spellings (e.g. "eowyn" finds "Éowyn") without
-- folding diacritics per query. The authoritative fold logic is fold_accents() in
-- api/parsing/card_query_nodes.py (NFKD + strip combining marks); every import
-- recomputes this column from that function. See #649 and
-- docs/issues/00649-accent-insensitive-name-search.md.
--
-- Exact-match search (name=, !"...") intentionally keeps comparing against
-- card_name/card_name_lower and is untouched by this column.

ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS card_name_folded text;

-- One-time backfill for rows already in the table, ahead of the next bulk import.
-- translate() is a bootstrap approximation of fold_accents() covering the
-- diacritics observed in the Scryfall corpus; the next import overwrites every
-- row with the real Python computation regardless.
UPDATE magic.cards
SET card_name_folded = lower(translate(
    card_name,
    'ÁÀÂÄÃÅáàâäãåÉÈÊËéèêëÍÌÎÏíìîïÓÒÔÖÕóòôöõÚÙÛÜúùûüÑñÇçŌō',
    'AAAAAAaaaaaaEEEEeeeeIIIIiiiiOOOOOoooooUUUUuuuuNnCcOo'
))
WHERE card_name_folded IS NULL;

ALTER TABLE magic.cards ALTER COLUMN card_name_folded SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_cardname_folded_lower_trgm
    ON magic.cards USING gin (lower(card_name_folded) magic.gin_trgm_ops);
