-- produced_mana_mask(jsonb) encodes produced mana as a 6-bit integer (W=32, U=16, B=8, R=4, G=2, C=1).
--
-- SIX bits, where magic.color_identity_mask has five, and the extra one is not symmetry-breaking
-- for its own sake: `produced_mana` is the one colour-ish column whose array can literally contain
-- "C". Sol Ring's produced_mana is ["C"] where its colors and color_identity are both []. So a
-- COUNT over this column counts colorless as a value, and Scryfall agrees — measured against
-- api.scryfall.com 2026-08-16:
--
--   produces=6      = 106 = produces:all = produces:wubrgc   -- unreachable if C did not count
--   produces=1 (C-only producers)             = 481          -- zero under a five-key count
--   the three cards producing exactly {C,W} land in produces=2, and produces=1 has none of them
--   counts 0..6 partition the corpus exactly: 30996+1143+504+147+10+693+106 = 33,599
--
-- The colour columns must keep counting FIVE and are deliberately NOT switched to this function:
-- `c:all` = `c:wubrg` = `c=5` = 60 there, and `c=6` is not even a valid query ("Unknown color 6").
--
-- The expression index mirrors the color_identity one, so produces= / produces>= count queries are
-- = ANY(mask_array) point lookups rather than a seq scan.

CREATE OR REPLACE FUNCTION magic.produced_mana_mask(jsonb)
RETURNS smallint LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT (
        CASE WHEN $1 ? 'W' THEN 32 ELSE 0 END +
        CASE WHEN $1 ? 'U' THEN 16 ELSE 0 END +
        CASE WHEN $1 ? 'B' THEN  8 ELSE 0 END +
        CASE WHEN $1 ? 'R' THEN  4 ELSE 0 END +
        CASE WHEN $1 ? 'G' THEN  2 ELSE 0 END +
        CASE WHEN $1 ? 'C' THEN  1 ELSE 0 END
    )::smallint
$$;

CREATE INDEX IF NOT EXISTS idx_cards_produced_mana_mask
    ON magic.cards (magic.produced_mana_mask(produced_mana));
