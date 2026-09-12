-- Two values Scryfall sends that no column held, both of them needed by the card object rather
-- than by any query:
--
--   card_keywords_printed -- the SAME keywords as card_keywords, in Scryfall's own casing and
--                            Scryfall's own order. The existing column is search-folded and its
--                            jsonb object loses order, and neither loss is recoverable: only 455
--                            of the 885 distinct keywords in the 2026-08-16 all_cards bulk come
--                            back from capitalizing the folded form ("Battle Cry", "AV Bead",
--                            "Bio-plasmic Barrage" do not), and the printed order is neither the
--                            folded dict's nor alphabetical (Brazen Borrower serves
--                            ["Flying","Flash"]). An ARRAY, because the order is the point.
--                            `keyword:` keeps binding card_keywords; this column is emit-only.
--   color_indicator       -- Scryfall's TOP-LEVEL color_indicator, the printed colour dot a card
--                            whose mana cost cannot state its colours carries (a meld result, a
--                            coloured back). 546 printings in the bulk carry one and the card
--                            object emitted it on none of them. The per-face indicator already
--                            rides the card_faces records; this is the card's own.
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS card_keywords_printed jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS color_indicator jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE magic.cards
    DROP CONSTRAINT IF EXISTS card_keywords_printed_must_be_array,
    ADD CONSTRAINT card_keywords_printed_must_be_array CHECK (jsonb_typeof(card_keywords_printed) = 'array'::text);
ALTER TABLE magic.cards
    DROP CONSTRAINT IF EXISTS color_indicator_must_be_object,
    ADD CONSTRAINT color_indicator_must_be_object CHECK (jsonb_typeof(color_indicator) = 'object'::text);

-- No backfill for either. card_keywords_printed CANNOT be reconstructed from card_keywords (that
-- is the whole reason it exists), and color_indicator was never stored at all; both fill on the
-- next import, and until then the card object omits them exactly as it did before — the defaults
-- above are the empty forms the emitters already treat as absent.
