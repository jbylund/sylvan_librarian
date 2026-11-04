-- `in:` is a CARD-level fact -- "cards that have EVER been printed in" a set code, set type, game,
-- language, rarity, frame year, foil/nonfoil, or booster -- answered with ALL of the card's
-- printings: `in:khm` is 5,318 printings under unique=prints on api.scryfall.com (2026-09-03)
-- where `e:khm` is 425. A per-printing row cannot answer that at query time (the row never sees
-- its siblings), so the per-oracle_id union is computed at import (_sync_in_tags in
-- api/admin_resource.py) and written onto EVERY row of the card, as a jsonb object of
-- {word: true} exactly like card_is_tags, so the parser's existing JSONB_OBJECT `@>` path and the
-- engine's jsonb_obj_to_ids loader both read it unchanged.
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS card_in_tags jsonb DEFAULT '{}'::jsonb NOT NULL;

-- Postgres has no ADD CONSTRAINT IF NOT EXISTS; mirror card_is_tags_must_be_object idempotently.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'card_in_tags_must_be_object'
          AND conrelid = 'magic.cards'::regclass
    ) THEN
        ALTER TABLE magic.cards
            ADD CONSTRAINT card_in_tags_must_be_object CHECK ((jsonb_typeof(card_in_tags) = 'object'::text));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_cards_in_tags_gin ON magic.cards USING gin (card_in_tags);
