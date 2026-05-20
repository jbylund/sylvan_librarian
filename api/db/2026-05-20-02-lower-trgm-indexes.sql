-- Replace ILIKE trigram indexes with lower() functional indexes.
-- lower() LIKE has much lower planning overhead than ILIKE because PostgreSQL's
-- LIKE selectivity estimator is cheaper than the case-folding ILIKE estimator.

CREATE INDEX IF NOT EXISTS idx_cards_oracle_text_lower_trgm
    ON magic.cards USING gin (lower(oracle_text) magic.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_cards_cardname_lower_trgm
    ON magic.cards USING gin (lower(card_name) magic.gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_cards_artist_lower_trgm
    ON magic.cards USING gin (lower(card_artist) magic.gin_trgm_ops)
    WHERE card_artist IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_flavor_text_lower_trgm
    ON magic.cards USING gin (lower(flavor_text) magic.gin_trgm_ops)
    WHERE flavor_text IS NOT NULL;

DROP INDEX IF EXISTS magic.idx_cards_oracle_text_trgm;
DROP INDEX IF EXISTS magic.idx_cards_cardname_trgm;
DROP INDEX IF EXISTS magic.idx_cards_artist_trgm;
DROP INDEX IF EXISTS magic.idx_cards_flavor_text_trgm;
