-- `flavor_name`: the alternate name a PRINTING is sold under — the Godzilla series, Stranger
-- Things, the Secret Lair crossovers. Two columns, exactly like the printed-name pair beside them:
--
--   flavor_name         -- Scryfall's value, verbatim, NULL when the key was absent.
--   flavor_name_folded  -- lowercased and accent-folded like card_name_folded / printed_name_folded:
--                          the name-lookup key.
--
-- It is a THIRD name space, not a variant of the printed one, and the split matters because the
-- lanes read them differently. Measured against api.scryfall.com 2026-08-16:
--
--   /cards/named?exact=Godzilla, Primeval Champion  -> 200, Titanoth Rex prm/80925
--   /cards/named?exact=Ego à Deriva                 -> 404   (a printed name; exact never reads one)
--   /cards/search?q=!"Godzilla, Primeval Champion"  -> 200, 1 card
--   /cards/autocomplete?q=godzil                    -> 0 values (autocomplete never reads one)
--
-- so `exact=` and `!"…"` match card_name ∪ flavor_name, while printed names stay out of both.
--
-- 669 of the 540,484 all_cards printings carry it (546 distinct strings, 10.4 KB), so this is a
-- sparse column: no index is added, and the name lanes that need one build it in the engine.
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS flavor_name text;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS flavor_name_folded text;

-- No backfill: the value is not derivable from anything stored, so the columns stay NULL until the
-- next import fills them. That is the same shape as the printed columns in
-- 2026-08-15-01-multilingual-store.sql, and for the same reason.
