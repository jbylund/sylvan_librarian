-- The second half of the same defect as 2026-08-17-01-fractional-power-toughness.sql, and the
-- reason it is a schema change and not just a Python one.
--
-- That migration was about a column that could not hold a printed value. This one is about a
-- column that was not allowed to hold it at all. creature_attributes_null_for_non_creatures says
-- a row may carry power/toughness only if its PARSED type line names Creature, Vehicle or
-- Spacecraft — and api/card_processing.py gates on exactly the same test, because the constraint
-- is what it was written against. The claim underneath both is that a printed stat implies a
-- modern creature type line. It does not:
--
--   $ curl -s 'https://api.scryfall.com/cards/search' \
--       --data-urlencode 'q=-t:creature -t:vehicle -t:spacecraft (pow>=0 or pow<0) include:extras' \
--       --data-urlencode 'unique=prints'
--       "total_cards": 18,
--
--   Old Fogey            Summon — Dinosaur     7/7    Atinlay Igpay      Eaturecray — Igpay  3/3
--   1996 World Champion  Summon Legend         */*    Aswan Jaguar       Summon Jaguar       2/2
--   Faerie Dragon        Summon Dragon         1/3    Flanking Licid     Summon Licid        1/1
--   Goblin Polka Band    Summon Goblin         1/1    Prismatic Dragon   Summon Dragon       2/3
--   Rainbow Knights      Summon Knights        2/1    Shichifukujin Dr.  Summon Dragon       0/0
--   Throat Wolf          Summon Wolf           3/1    Xyru Specter       Summon — Specter    2/2
--
-- (Checked against the live API on 2026-08-17. Mostly the pre-Sixth-Edition `Summon` template,
-- which Scryfall preserves verbatim on the printings that used it rather than rewriting it to
-- `Creature`; Atinlay Igpay is the pig-latin one. Counted with unique=prints, so Old Fogey's four
-- printings and the two Aswan Jaguars are each counted once per printing.)
--
-- Scryfall itself searches every one of them numerically — `pow=3` and `tou=3` both count Atinlay
-- Igpay, `tou>=3.5` counts Old Fogey — so the printed keys, not the parsed type, are what decide
-- whether a stat exists. The constraint is the thing that would reject the row, so it has to move
-- before the import can be taught to keep the value.
--
-- WHAT REPLACES IT, AND WHY NOT NOTHING. The useful half of the old rule survives: a stat on a
-- non-creature row must be a PRINTED one. The numeric columns are this project's parse of
-- creature_power_text/creature_toughness_text, so a numeric stat with no printed string beside it
-- on a row whose type line does not claim a creature is a derived value that came from somewhere
-- it should not have — which is the failure this table wants to keep catching. What the old rule
-- asserted beyond that, that only a modern creature type line may print a stat, is what the
-- eighteen printings above disprove.
--
-- NOT WIDENED: the type parse. `Summon` and `Eaturecray` remain unrecognised words, here and in
-- parse_type_line, so none of these rows begins to answer `t:creature`. Teaching the parser those
-- spellings would have satisfied the old constraint and made the numeric counts agree, at the
-- price of inventing a card type the printing does not have.
--
-- CORPUS EFFECT TODAY: none, on exactly the terms 2026-08-12-01 and 2026-08-17-01 set out. All
-- eighteen printings fail preprocess_card's legality filter — not one is legal or restricted in
-- any format — and most also fail the funny-set, playtest or paper-games rules. So no row in the
-- table changes and no query answer moves. This is a CAPABILITY change: the schema stops being
-- the thing that would drop the stat if the import filter were ever relaxed.
--
-- COST: DROP is a catalog update. The ADD validates the whole table, a sequential scan under a
-- SHARE ROW EXCLUSIVE lock; at ~31.5k oracle rows that is milliseconds, and every existing row
-- passes it vacuously — the old constraint already guaranteed that a non-creature row has all
-- four columns NULL, so the new disjunct nobody currently reaches is the one being added. Both
-- statements are in one ALTER TABLE so the table is examined once.
--
-- IF NOT EXISTS on the drop: the constraint is created inline by the CREATE TABLE in
-- 2025-09-29-great-reset.sql, which is not edited here — migrations stack on top of it — but a
-- database restored from a dump taken after this migration will not have it.
ALTER TABLE magic.cards
    DROP CONSTRAINT IF EXISTS creature_attributes_null_for_non_creatures,
    ADD CONSTRAINT creature_attributes_printed_or_null_for_non_creatures CHECK (
        (card_types ?| ARRAY['Creature'::text])
        OR (card_subtypes ?| ARRAY['Vehicle'::text, 'Spacecraft'::text])
        OR (creature_power_text IS NOT NULL)
        OR (creature_toughness_text IS NOT NULL)
        OR (
            (creature_power IS NULL)
            AND (creature_power_text IS NULL)
            AND (creature_toughness IS NULL)
            AND (creature_toughness_text IS NULL)
        )
    );
