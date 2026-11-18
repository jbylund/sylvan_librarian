-- Scryfall's `cheapest:usd` / `cheapest:eur` / `cheapest:tix` find the printings carrying their
-- card's LOWEST price in that currency. The lowest price is over the card's other printings,
-- which no row can see at query time, so -- like the count columns of 2026-10-04-01 -- the
-- answers are decided when the data is written.
--
-- One smallint per printing, three bits per currency (usd at bit 0, eur at bit 3, tix at bit 6):
--   1  `cheapest:<currency>` is true of this printing
--   2  `-cheapest:<currency>` is true of it. On Scryfall the negated TERM is an expression of its
--      own, `(price IS NULL OR price <> M) AND (foil IS NULL OR foil = M)`, and not the
--      complement of the term, so it is stored rather than derived
--   4  both are SQL NULL: the printing is priced and its card has no lowest price (every priced
--      printing of it is in a memorabilia set, or in euros is foil-only)
-- NULL means "not yet computed" and answers neither the keyword nor its negation.
--
-- Maintained by _sync_cheapest_codes in api/admin_resource.py after every import -- prices move on
-- every import -- and backfilled once below so the keyword answers as soon as this deploys rather
-- than after the next import. _build_cheapest_codes_sql's docstring carries the measured rule.
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS cheapest_codes smallint;

COMMENT ON COLUMN magic.cards.cheapest_codes IS 'Printing-level: the answers to Scryfall''s `cheapest:usd` / `cheapest:eur` / `cheapest:tix` and to their negated terms, three bits per currency (usd << 0, eur << 3, tix << 6): 1 = the term is true, 2 = the negated term is true, 4 = both are NULL (priced printing of a card with no lowest price).';

-- The backfill: the statement _build_cheapest_codes_sql() runs per chunk, here over the whole
-- table. Idempotent -- only rows whose code differs are rewritten -- and no index is added: the
-- engine answers from memory, and the SQL fallback's test of one smallint in a scan is cheap.
WITH priced AS (
    SELECT
        cards.scryfall_id,
        cards.oracle_id,
        COALESCE(cards.raw_card_blob->>'set_type', '') = 'memorabilia' AS is_memorabilia,
        CASE WHEN cards.raw_card_blob->'prices'->>'usd' ~ '^[0-9]+(\.[0-9]+)?$' THEN (cards.raw_card_blob->'prices'->>'usd')::numeric END AS usd,
        CASE WHEN cards.raw_card_blob->'prices'->>'usd_foil' ~ '^[0-9]+(\.[0-9]+)?$' THEN (cards.raw_card_blob->'prices'->>'usd_foil')::numeric END AS usd_foil,
        CASE WHEN cards.raw_card_blob->'prices'->>'eur' ~ '^[0-9]+(\.[0-9]+)?$' THEN (cards.raw_card_blob->'prices'->>'eur')::numeric END AS eur,
        CASE WHEN cards.raw_card_blob->'prices'->>'eur_foil' ~ '^[0-9]+(\.[0-9]+)?$' THEN (cards.raw_card_blob->'prices'->>'eur_foil')::numeric END AS eur_foil,
        CASE WHEN cards.raw_card_blob->'prices'->>'tix' ~ '^[0-9]+(\.[0-9]+)?$' THEN (cards.raw_card_blob->'prices'->>'tix')::numeric END AS tix,
        NULL::numeric AS tix_foil
    FROM magic.cards cards
    WHERE cards.oracle_id IS NOT NULL
), card_minimum AS (
    SELECT
        priced.oracle_id,
        min(COALESCE(priced.usd, priced.usd_foil)) FILTER (WHERE NOT priced.is_memorabilia) AS usd,
        min(priced.eur) FILTER (WHERE NOT priced.is_memorabilia) AS eur,
        min(priced.tix) FILTER (WHERE NOT priced.is_memorabilia) AS tix
    FROM priced
    GROUP BY priced.oracle_id
), proposed AS (
    SELECT
        priced.scryfall_id,
        (
        (CASE
            WHEN card_minimum.usd IS NULL THEN
                CASE WHEN priced.usd IS NOT NULL OR priced.usd_foil IS NOT NULL THEN 4 ELSE 2 END
            ELSE
                CASE WHEN priced.usd = card_minimum.usd OR priced.usd_foil = card_minimum.usd THEN 1 ELSE 0 END
                + CASE WHEN (priced.usd IS NULL OR priced.usd <> card_minimum.usd) AND (priced.usd_foil IS NULL OR priced.usd_foil = card_minimum.usd)
                       THEN 2 ELSE 0 END
        END) * 1
        + (CASE
            WHEN card_minimum.eur IS NULL THEN
                CASE WHEN priced.eur IS NOT NULL OR priced.eur_foil IS NOT NULL THEN 4 ELSE 2 END
            ELSE
                CASE WHEN priced.eur = card_minimum.eur OR priced.eur_foil = card_minimum.eur THEN 1 ELSE 0 END
                + CASE WHEN (priced.eur IS NULL OR priced.eur <> card_minimum.eur) AND (priced.eur_foil IS NULL OR priced.eur_foil = card_minimum.eur)
                       THEN 2 ELSE 0 END
        END) * 8
        + (CASE
            WHEN card_minimum.tix IS NULL THEN
                CASE WHEN priced.tix IS NOT NULL OR priced.tix_foil IS NOT NULL THEN 4 ELSE 2 END
            ELSE
                CASE WHEN priced.tix = card_minimum.tix OR priced.tix_foil = card_minimum.tix THEN 1 ELSE 0 END
                + CASE WHEN (priced.tix IS NULL OR priced.tix <> card_minimum.tix) AND (priced.tix_foil IS NULL OR priced.tix_foil = card_minimum.tix)
                       THEN 2 ELSE 0 END
        END) * 64
        )::smallint AS cheapest_codes
    FROM priced
    JOIN card_minimum ON card_minimum.oracle_id = priced.oracle_id
)
UPDATE magic.cards
SET cheapest_codes = proposed.cheapest_codes
FROM proposed
WHERE
    cards.scryfall_id = proposed.scryfall_id AND
    cards.cheapest_codes IS DISTINCT FROM proposed.cheapest_codes;
