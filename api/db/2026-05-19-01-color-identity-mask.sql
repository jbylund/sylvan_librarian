-- color_identity_mask(jsonb) encodes color identity as a 5-bit integer (W=16, U=8, B=4, R=2, G=1).
-- The expression index lets the planner serve id: / id<= / id< queries via = ANY(subset_array)
-- point lookups instead of a full seq scan against the unindexable <@ JSONB containment direction.

CREATE OR REPLACE FUNCTION magic.color_identity_mask(jsonb)
RETURNS smallint LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT (
        CASE WHEN $1 ? 'W' THEN 16 ELSE 0 END +
        CASE WHEN $1 ? 'U' THEN  8 ELSE 0 END +
        CASE WHEN $1 ? 'B' THEN  4 ELSE 0 END +
        CASE WHEN $1 ? 'R' THEN  2 ELSE 0 END +
        CASE WHEN $1 ? 'G' THEN  1 ELSE 0 END
    )::smallint
$$;

CREATE INDEX IF NOT EXISTS idx_cards_color_identity_mask
    ON magic.cards (magic.color_identity_mask(card_color_identity));
