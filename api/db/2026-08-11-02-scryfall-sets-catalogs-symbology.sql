-- Backing store for the reference half of the Scryfall API: /sets, /catalog/* and /symbology.
--
-- All three are mirrored from api.scryfall.com rather than derived from magic.cards, and the reason
-- is the same in each case: what the corpus can prove is a strict subset of what these endpoints
-- have to say.
--
--   * A Set object carries eight fields no card carries -- tcgplayer_id, mtgo_code, arena_code,
--     icon_svg_uri, block, block_code, parent_set_code, printed_size. /sets/tcgplayer/:id cannot be
--     answered at all without the first of them.
--   * card_count is Scryfall's count for the whole set, not the count this instance imported. The
--     corpus is a filtered subset (no tokens, funny sets or digital-only printings), so deriving it
--     would report a number no other Scryfall client agrees with.
--   * A card symbol's svg_uri and gatherer_alternates exist nowhere in the card data.
--
-- Each table therefore stores the upstream object whole, in jsonb, and lifts out only what a lookup
-- keys on. That keeps the served bytes identical to Scryfall's without a column-by-column mapping
-- that would silently drop a field Scryfall adds later.


-- One row per set, the object exactly as Scryfall sent it.
--
-- `position` preserves the order /sets returns rather than recomputing one. The list is sorted by
-- released_at descending, but sets sharing a release date come back in an order that is neither
-- alphabetical by code nor by name (2026-11-13 yields trk, trc, ttrk, sds), and nothing in the
-- object reproduces it. Storing the index is exact and costs four bytes.
CREATE TABLE IF NOT EXISTS magic.sets (
    id uuid PRIMARY KEY,
    code text NOT NULL,
    tcgplayer_id bigint,
    position integer NOT NULL,
    set_object jsonb NOT NULL
);

COMMENT ON TABLE magic.sets IS
    'Scryfall Set objects, mirrored from api.scryfall.com/sets. One row per set; set_object is the upstream object verbatim.';
COMMENT ON COLUMN magic.sets.position IS
    'Index in Scryfall''s own /sets ordering, which is not reproducible from the object.';

-- /sets/:code. Scryfall matches a set code case-insensitively, so the index is on the folded value
-- and the lookup folds its segment to match. Unique because a code identifies one set.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sets_code ON magic.sets USING btree (lower(code));

-- /sets/tcgplayer/:id. Partial because most sets have no TCGplayer id, and a null there is not an
-- identifier anyone can look up.
CREATE INDEX IF NOT EXISTS idx_sets_tcgplayer_id
    ON magic.sets USING btree (tcgplayer_id)
    WHERE tcgplayer_id IS NOT NULL;

-- GET /sets returns every row in one List object, so the listing is an index-only ordering scan.
CREATE INDEX IF NOT EXISTS idx_sets_position ON magic.sets USING btree (position);


-- The twenty /catalog/* endpoints, one row each.
--
-- A table of (name, entries) rather than twenty tables or twenty columns: the endpoints differ only
-- in which list of strings they return, the set of them is fixed by Scryfall rather than by this
-- schema, and a catalog is only ever read or replaced whole. `entries` is a jsonb array of strings,
-- which is what the Catalog object's `data` is -- named `entries` rather than the obvious `values`
-- because VALUES is a reserved word and the column would need quoting at every use site.
CREATE TABLE IF NOT EXISTS magic.catalogs (
    name text PRIMARY KEY,
    entries jsonb NOT NULL
);

COMMENT ON TABLE magic.catalogs IS
    'Scryfall Catalog payloads, one row per /catalog/:name endpoint, entries being the data array verbatim.';


-- /symbology. Eighty-odd rows, each the upstream card_symbol object.
--
-- `position` for the same reason magic.sets has one: Scryfall returns the symbols in a fixed order
-- that no field on the object reproduces, and /symbology is served as that list.
CREATE TABLE IF NOT EXISTS magic.card_symbols (
    symbol text PRIMARY KEY,
    position integer NOT NULL,
    symbol_object jsonb NOT NULL
);

COMMENT ON TABLE magic.card_symbols IS
    'Scryfall CardSymbol objects, mirrored from api.scryfall.com/symbology, in the order that endpoint returns them.';

CREATE INDEX IF NOT EXISTS idx_card_symbols_position ON magic.card_symbols USING btree (position);
