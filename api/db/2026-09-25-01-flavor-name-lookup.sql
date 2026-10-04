-- /cards/named reads a printing's FLAVOR NAME (the Godzilla series, the Secret Lair and Universes
-- Beyond crossovers), and the SQL fallback reads it off `flavor_name_folded`. Measured on
-- api.scryfall.com 2026-09-25:
--
--   exact=Godzilla, King of the Monsters     -> Zilortha, Strength Incarnate iko/275
--   fuzzy=mothra supersonic queen            -> Luminous Broodmoth prm/80915 (the printing carrying it)
--   fuzzy=supersonic                         -> the same printing, by containment
--   fuzzy=titanoth champion                  -> Titanoth Rex prm/80925 (oracle + flavor words pooled)
--   fuzzy=assaultron, cordyceps, bloodbender -> ambiguous (a flavor name competes as an equal)
--
-- THE KEY OF A PRINTING WHOSE FACES CARRY THE FLAVOR NAMES is the faces' names joined " // ", in
-- face order, over the faces that carry one. Measured on api.scryfall.com 2026-09-25:
-- `exact=Megatron // Megatron` answers Blightsteel Colossus sld/1079 (both faces "Megatron") while
-- `exact=Megatron` is a 404, and `exact=Chucky` answers Kardur, Doomscourge sld/1807, whose front
-- face alone carries one. The import now writes that key (`_flavor_name_folded` in
-- api/card_processing.py); this backfills it from `card_faces` so the key exists before the next
-- import. `lower()` stands in for fold_accents, as in 2026-07-20-01-accent-folded-name.sql: every
-- face-level flavor name in the corpus is ASCII, and the next import rewrites the column with the
-- real fold regardless.
UPDATE magic.cards AS card
SET flavor_name_folded = lower(faces.joined)
FROM (
    SELECT scryfall_id, string_agg(face ->> 'flavor_name', ' // ' ORDER BY ordinality) AS joined
    FROM magic.cards, jsonb_array_elements(card_faces) WITH ORDINALITY AS t(face, ordinality)
    WHERE flavor_name IS NULL
        AND jsonb_typeof(card_faces) = 'array'
        AND coalesce(face ->> 'flavor_name', '') <> ''
    GROUP BY scryfall_id
) AS faces
WHERE card.scryfall_id = faces.scryfall_id
    AND card.flavor_name IS NULL
    AND card.flavor_name_folded IS NULL;

-- 2026-08-16-03-flavor-name.sql added the column unindexed, on the grounds that only the engine
-- read it. The SQL fallback's exact, whole-name and containment stages now read it too, so it gets
-- the same index its two siblings have: a trigram GIN over the name with every non-alphanumeric
-- character removed (2026-08-16-01-unseparated-name-search.sql), the EXACT expression
-- api/scryfall_compat/routes.py's `_UNSEPARATED` spells, since an expression index only serves a
-- query that repeats it character for character. It serves the containment stage's `LIKE '%word%'`
-- and the exact and whole-name stages' `=` alike.
--
-- PARTIAL, because the column is sparse: ~670 of ~540k printings carry a flavor name. The route's
-- predicates each carry `flavor_name_folded IS NOT NULL`, which is what lets the planner use it.
CREATE INDEX IF NOT EXISTS idx_cards_flavorname_unseparated_trgm
    ON magic.cards USING gin (
        (regexp_replace(lower(coalesce(flavor_name_folded, '')), '[^[:alnum:]]', '', 'g')) magic.gin_trgm_ops
    )
    WHERE flavor_name_folded IS NOT NULL;
