-- this index helps support the random search endpoint
CREATE INDEX IF NOT EXISTS idx_cards_name_preferscore ON magic.cards USING btree (card_name, prefer_score DESC NULLS LAST) INCLUDE (scryfall_id);
