-- Scryfall's `order=color` as one number per card, so both search lanes sort by the same stored
-- value: the order the game prints its colour combinations in, by the FRONT FACE.
--
--    0..30   cards with a colour that are not lands, by their colours:
--            W U B R G, WU UB BR RG GW WB UR BG RW GU, the shards, the wedges, the four-colour
--            sets, all five
--   32..63   colourless cards that are not lands, by their colour identity in that same order,
--            no identity last
--   64..95   lands, by their colour identity, whatever their colours
--
-- Written by preprocess_card (color_order_rank in api/card_processing.py) on every import, from
-- the whole Scryfall card. NULL means "not yet computed" and sorts last in both directions on
-- both lanes, like any other missing sort value.
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS color_order smallint;

COMMENT ON COLUMN magic.cards.color_order IS 'Card-level: the position of the card under Scryfall''s `order=color`. 0..30 a coloured non-land by its colours, 32..63 a colourless non-land by its colour identity, 64..95 a land by its colour identity; within each block the rank of the combination in the game''s printed order (W U B R G, the ten pairs, the ten triples, the five four-colour sets, WUBRG, none). Read off the front face of a multi-faced card.';

-- The backfill, so `order=color` answers in this order as soon as this deploys rather than after
-- the next import. It reads the stored row, which is all a migration can see, and the stored row
-- of a multi-faced card holds one face rather than the front one -- so a card whose faces differ
-- in colour or in being a land is placed by that face here, and is put right by the next import,
-- which rewrites every row whose value differs. Over the 2026-10-04 default_cards file that is
-- 185 of 32,011 cards (449 of 99,762 rows: transform 260, modal_dfc 174, adventure 15); every
-- other card is placed exactly.
-- combination_ranks spells each combination in WUBRG letter order, which is the order the key
-- expressions below build theirs in; the rank is its place in the game's order.
WITH combination_ranks (combination, rank) AS (
    VALUES
        ('W', 0), ('U', 1), ('B', 2), ('R', 3), ('G', 4),
        ('WU', 5), ('UB', 6), ('BR', 7), ('RG', 8), ('WG', 9),
        ('WB', 10), ('UR', 11), ('BG', 12), ('WR', 13), ('UG', 14),
        ('WUB', 15), ('UBR', 16), ('BRG', 17), ('WRG', 18), ('WUG', 19),
        ('WBG', 20), ('WUR', 21), ('UBG', 22), ('WBR', 23), ('URG', 24),
        ('WUBR', 25), ('UBRG', 26), ('WBRG', 27), ('WURG', 28), ('WUBG', 29),
        ('WUBRG', 30), ('', 31)
),
keyed AS (
    SELECT
        scryfall_id,
        concat(
            CASE WHEN card_colors ? 'W' THEN 'W' END,
            CASE WHEN card_colors ? 'U' THEN 'U' END,
            CASE WHEN card_colors ? 'B' THEN 'B' END,
            CASE WHEN card_colors ? 'R' THEN 'R' END,
            CASE WHEN card_colors ? 'G' THEN 'G' END
        ) AS colors,
        concat(
            CASE WHEN card_color_identity ? 'W' THEN 'W' END,
            CASE WHEN card_color_identity ? 'U' THEN 'U' END,
            CASE WHEN card_color_identity ? 'B' THEN 'B' END,
            CASE WHEN card_color_identity ? 'R' THEN 'R' END,
            CASE WHEN card_color_identity ? 'G' THEN 'G' END
        ) AS identity,
        COALESCE(card_types ? 'Land', false) AS is_land
    FROM magic.cards
    WHERE color_order IS NULL
)
UPDATE magic.cards AS card
SET color_order = CASE
        WHEN keyed.is_land THEN 64 + identity_rank.rank
        WHEN colors_rank.rank = 31 THEN 32 + identity_rank.rank
        ELSE colors_rank.rank
    END
FROM keyed
JOIN combination_ranks AS colors_rank ON colors_rank.combination = keyed.colors
JOIN combination_ranks AS identity_rank ON identity_rank.combination = keyed.identity
WHERE card.scryfall_id = keyed.scryfall_id;
