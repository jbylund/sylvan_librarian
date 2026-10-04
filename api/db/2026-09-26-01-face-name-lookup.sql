-- /cards/named?fuzzy= reads a two-faced card's FACE names in its exact stage, as `exact=` does:
-- measured on api.scryfall.com 2026-09-26, `fuzzy=fire` and `fuzzy=ice` are Fire // Ice, `fuzzy=life`
-- is Life // Death and `fuzzy=appeal` Appeal // Authority, where a stage matching whole names only
-- left them to containment, which calls every one of them ambiguous. The SQL fallback's predicate is
-- `_ORACLE_KEY_IS_NEEDLE` in api/scryfall_compat/routes.py: the whole name through
-- idx_cards_cardname_unseparated_trgm (2026-08-16-01-unseparated-name-search.sql), and each face of
-- a name that splits in EXACTLY two through the indexes below, whose expressions and predicate it
-- repeats character for character, since an expression index only serves a query that does.
--
-- Without them the face arms are a sequential scan of every printing, ~260ms a needle on the full
-- all_cards corpus; with them the stage is under 1.5ms for every needle measured. PARTIAL, over the
-- two-part names alone (a few hundred kB each), because no other name has a face key.
CREATE INDEX IF NOT EXISTS idx_cards_front_face_collated
    ON magic.cards ((regexp_replace(lower(split_part(card_name_folded, ' // ', 1)), '[^[:alnum:]]', '', 'g')))
    WHERE split_part(card_name_folded, ' // ', 2) <> '' AND split_part(card_name_folded, ' // ', 3) = '';

CREATE INDEX IF NOT EXISTS idx_cards_back_face_collated
    ON magic.cards ((regexp_replace(lower(split_part(card_name_folded, ' // ', 2)), '[^[:alnum:]]', '', 'g')))
    WHERE split_part(card_name_folded, ' // ', 2) <> '' AND split_part(card_name_folded, ' // ', 3) = '';
