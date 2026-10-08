-- Backing store for the Scryfall-compatible /cards/* API (api/scryfall_compat/).
--
-- Two parts, neither of which any existing route reads:
--
--   1. Lookup indexes for the identifiers Scryfall routes by that live inside raw_card_blob rather
--      than in a column of their own. Each partial index predicates on the *same* expression the
--      lookup compares, so the planner can prove the query implies the predicate.
--
--   2. magic.rulings, backing /cards/:id/rulings and its four sibling routes.
--
-- No column holds the card object the API serves: raw_card_blob is already what Scryfall sent, so
-- api/scryfall_compat/objects.py recovers the card from it by stripping the three keys the importer
-- adds. That is true of a multi-face printing only as of the merged-row work; see the deployment
-- note in docs/issues/local-scryfall-cards-api.md.


-- /cards/:code/:number and /cards/:code/:number/:lang. On lower(card_set_code) rather than the
-- column, because Scryfall's set codes are case-insensitive and the lookup folds the segment; the
-- corpus happens to store them lowercase already, but nothing constrains it to.
CREATE INDEX IF NOT EXISTS idx_cards_set_collector_lang
    ON magic.cards USING btree (lower(card_set_code), collector_number, (raw_card_blob ->> 'lang'));

-- GET /cards/named?exact=. card_name_folded is already lowercase (fold_accents() lowercases before
-- folding), so lower() here is a no-op on the value and only exists so the indexed expression is
-- the one the lookup writes. The two split_part indexes cover the face names of a "Front // Back"
-- card, which Scryfall's exact match accepts alongside the combined name: with all three indexed,
-- the lookup's OR is a BitmapOr rather than a sequential scan.
CREATE INDEX IF NOT EXISTS idx_cards_name_folded_exact
    ON magic.cards USING btree (lower(card_name_folded));
CREATE INDEX IF NOT EXISTS idx_cards_name_folded_front_face
    ON magic.cards USING btree (lower(split_part(card_name_folded, ' // ', 1)));
CREATE INDEX IF NOT EXISTS idx_cards_name_folded_back_face
    ON magic.cards USING btree (lower(split_part(card_name_folded, ' // ', 2)));

-- /cards/multiverse/:id -- multiverse_ids is an array, so containment rather than equality.
CREATE INDEX IF NOT EXISTS idx_cards_multiverse_ids
    ON magic.cards USING gin ((raw_card_blob -> 'multiverse_ids') jsonb_path_ops);

-- /cards/mtgo/:id matches either the regular or the foil MTGO id, so both are indexed.
CREATE INDEX IF NOT EXISTS idx_cards_mtgo_id
    ON magic.cards USING btree (((raw_card_blob ->> 'mtgo_id')::bigint))
    WHERE ((raw_card_blob ->> 'mtgo_id')::bigint) IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_mtgo_foil_id
    ON magic.cards USING btree (((raw_card_blob ->> 'mtgo_foil_id')::bigint))
    WHERE ((raw_card_blob ->> 'mtgo_foil_id')::bigint) IS NOT NULL;

-- /cards/arena/:id
CREATE INDEX IF NOT EXISTS idx_cards_arena_id
    ON magic.cards USING btree (((raw_card_blob ->> 'arena_id')::bigint))
    WHERE ((raw_card_blob ->> 'arena_id')::bigint) IS NOT NULL;

-- /cards/tcgplayer/:id matches either the regular or the etched TCGplayer id.
CREATE INDEX IF NOT EXISTS idx_cards_tcgplayer_id
    ON magic.cards USING btree (((raw_card_blob ->> 'tcgplayer_id')::bigint))
    WHERE ((raw_card_blob ->> 'tcgplayer_id')::bigint) IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_tcgplayer_etched_id
    ON magic.cards USING btree (((raw_card_blob ->> 'tcgplayer_etched_id')::bigint))
    WHERE ((raw_card_blob ->> 'tcgplayer_etched_id')::bigint) IS NOT NULL;

-- /cards/cardmarket/:id
CREATE INDEX IF NOT EXISTS idx_cards_cardmarket_id
    ON magic.cards USING btree (((raw_card_blob ->> 'cardmarket_id')::bigint))
    WHERE ((raw_card_blob ->> 'cardmarket_id')::bigint) IS NOT NULL;

-- The oracle_id and illustration_id identifiers POST /cards/collection accepts, and the oracle_id
-- the rulings hang off, are already served by idx_cards_oracle_id and idx_cards_illustration_id
-- from 2026-06-21-01-bulk-tag-import.sql.

CREATE TABLE IF NOT EXISTS magic.rulings (
    oracle_id uuid NOT NULL,
    source text NOT NULL,
    published_at date NOT NULL,
    comment text NOT NULL
);

COMMENT ON TABLE magic.rulings IS
    'Scryfall rulings bulk data, keyed by oracle_id. One row per ruling; a card has zero or more.';

-- The bulk file carries no ruling id, so identity is the tuple itself. md5() rather than the raw
-- comment because a btree entry is capped at ~2700 bytes and rulings run longer than that.
CREATE UNIQUE INDEX IF NOT EXISTS idx_rulings_identity
    ON magic.rulings USING btree (oracle_id, source, published_at, md5(comment));
