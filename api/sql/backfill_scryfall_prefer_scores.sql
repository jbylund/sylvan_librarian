-- Backfill scryfall_prefer_score: Scryfall's own per-card printing preference, as a full ordering.
--
-- Two observable Scryfall behaviors define the order, and each is reproduced from the source that
-- carries it:
--
--   1. The TOP of the order is the printing Scryfall itself shows for the card -- its
--      `oracle_cards` dump is one card object per oracle_id and that object IS the chosen
--      representative. magic.scryfall_representatives holds those ids (synced each import).
--   2. The REST follow Scryfall's prints listing (every card object's `prints_search_uri` is
--      `order=released&unique=prints`): release date newest first, ties broken by set code, then
--      collector number, ALL descending. Verified against their live listing rather than assumed:
--      same set, same day orders 305 -> 184 -> 63 and "1638*" -> "1638" (numeric part first, full
--      text second); different sets, same day orders pfdn -> fdc, mb2 -> blc, and fbb -> 3ed --
--      that last pair is what rules out set NAME ("Foreign Black Border" < "Revised Edition") in
--      favor of set CODE ('f' > '3').
--
-- scryfall_id is the final key only to make the order total; Scryfall never shows two printings
-- with the same set and collector number.
--
-- The score is 0 for the top printing, -1, -2, ... down the card's ordering, so DESC reproduces
-- Scryfall's full ordering and the existing higher-wins prefer machinery (SQL and engine) applies
-- unchanged. A rank is assigned to every printing whether or not the card carries a representative
-- label: an unlabelled card simply orders purely by the prints listing (LEFT JOIN, so an empty
-- label table is valid input, not a failure).
--
-- prefer_score is deliberately NOT an input: this score exists so Scryfall's preference is
-- available AS ITS OWN prefer, with the curated default left alone.
WITH ranked AS (
    SELECT
        cards.scryfall_id,
        (
            1 - ROW_NUMBER() OVER (
                PARTITION BY cards.oracle_id
                ORDER BY
                    (representatives.scryfall_id IS NOT NULL) DESC,
                    cards.released_at DESC,
                    cards.card_set_code DESC,
                    cards.collector_number_int DESC NULLS LAST,
                    cards.collector_number DESC,
                    cards.scryfall_id DESC
            )
        )::real AS scryfall_prefer_score
    FROM magic.cards AS cards
    LEFT JOIN magic.scryfall_representatives AS representatives USING (scryfall_id)
)
UPDATE magic.cards
SET
    scryfall_prefer_score = ranked.scryfall_prefer_score
FROM
    ranked
WHERE
    magic.cards.scryfall_id = ranked.scryfall_id AND
    magic.cards.scryfall_prefer_score IS DISTINCT FROM ranked.scryfall_prefer_score
