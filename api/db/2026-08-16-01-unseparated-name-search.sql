-- The containment stage of /cards/named?fuzzy= matches a query word against the name with every
-- non-alphanumeric character removed, because that is what Scryfall matches (measured
-- 2026-08-16: `fuzzy=red goad` resolves `goad` inside "Ego à Deriva", spanning two spaces, and
-- `fuzzy=aust com` finds both halves inside "Manicomio Infausto"). See
-- api/scryfall_compat/routes.py's _UNSEPARATED, which spells the identical expression.
--
-- EXPRESSION indexes, not columns: `LIKE '%word%'` over a wrapped column cannot use the plain
-- trigram indexes on card_name_folded, so without these the stage degrades from an index probe to
-- a sequential scan of every printing -- and the multilingual store made that ~540k rows. An
-- expression index only serves a query that repeats the expression character for character, which
-- is why the route builds its predicate from one shared format string.
--
-- Both operands are IMMUTABLE (lower, coalesce, regexp_replace), which is what an index expression
-- requires. printed_name_folded is NULL on most rows; coalesce keeps those rows indexed as '' so
-- the OR arm never goes NULL.
CREATE INDEX IF NOT EXISTS idx_cards_cardname_unseparated_trgm
    ON magic.cards USING gin (
        (regexp_replace(lower(coalesce(card_name_folded, '')), '[^[:alnum:]]', '', 'g')) magic.gin_trgm_ops
    );

CREATE INDEX IF NOT EXISTS idx_cards_printedname_unseparated_trgm
    ON magic.cards USING gin (
        (regexp_replace(lower(coalesce(printed_name_folded, '')), '[^[:alnum:]]', '', 'g')) magic.gin_trgm_ops
    );
