DROP SCHEMA IF EXISTS magic CASCADE;
CREATE SCHEMA magic;

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA magic;


CREATE OR REPLACE FUNCTION magic.extract_collector_number_int(collector_number_text text) RETURNS integer
    LANGUAGE plpgsql IMMUTABLE
    AS $$
BEGIN
    -- Extract numeric characters and convert to integer
    -- Returns NULL if no numeric characters found
    DECLARE
        numeric_part text;
    BEGIN
        -- Use regexp_replace to remove all non-numeric characters
        numeric_part := regexp_replace(collector_number_text, '[^0-9]', '', 'g');

        -- Return NULL if no numeric characters remain or if empty
        IF numeric_part = '' OR numeric_part IS NULL THEN
            RETURN NULL;
        END IF;

        -- Cast to integer, handle potential overflow
        RETURN numeric_part::integer;
    EXCEPTION
        WHEN OTHERS THEN
            -- Return NULL for any casting errors (e.g., number too large)
            RETURN NULL;
    END;
END;
$$;


COMMENT ON FUNCTION magic.extract_collector_number_int(collector_number_text text) IS 'Extract numeric portion from collector number text for sorting and comparison purposes';


CREATE OR REPLACE FUNCTION magic.rarity_int_to_text(rarity_int integer) RETURNS text
    LANGUAGE plpgsql IMMUTABLE
    AS $$
BEGIN
    RETURN CASE rarity_int
        WHEN 0 THEN 'common'
        WHEN 1 THEN 'uncommon'
        WHEN 2 THEN 'rare'
        WHEN 3 THEN 'mythic'
        WHEN 4 THEN 'special'
        WHEN 5 THEN 'bonus'
        ELSE NULL
    END;
END;
$$;


COMMENT ON FUNCTION magic.rarity_int_to_text(rarity_int integer) IS 'Convert rarity integer back to text';


CREATE OR REPLACE FUNCTION magic.rarity_text_to_int(rarity_text text) RETURNS integer
    LANGUAGE plpgsql IMMUTABLE
    AS $$
BEGIN
    RETURN CASE LOWER(TRIM(rarity_text))
        WHEN 'common' THEN 0
        WHEN 'uncommon' THEN 1
        WHEN 'rare' THEN 2
        WHEN 'mythic' THEN 3
        WHEN 'special' THEN 4
        WHEN 'bonus' THEN 5
        ELSE -1
    END;
END;
$$;

COMMENT ON FUNCTION magic.rarity_text_to_int(rarity_text text) IS 'Convert rarity text to integer for ordering and comparison';


CREATE OR REPLACE FUNCTION magic.all_elements_initcap(arr jsonb) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE
    AS $$
BEGIN
    RETURN NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(arr) AS element
        WHERE element != initcap(element)
    );
END;
$$;


CREATE FUNCTION magic.check_circular_reference() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Check if adding this relationship would create a cycle
    IF EXISTS (
        WITH RECURSIVE hierarchy AS (
            SELECT NEW.parent_tag as tag, 1 as depth
            UNION ALL
            SELECT tr.parent_tag, h.depth + 1
            FROM magic.tag_relationships tr
            JOIN hierarchy h ON tr.child_tag = h.tag
            WHERE h.depth < 100 -- prevent infinite recursion
        )
        SELECT 1 FROM hierarchy WHERE tag = NEW.child_tag
    ) THEN
        RAISE EXCEPTION 'Circular reference detected: % -> %', NEW.child_tag, NEW.parent_tag;
    END IF;

    RETURN NEW;
END;
$$;


CREATE FUNCTION magic.get_tag_ancestors(target_tag text) RETURNS TABLE(tag text, level integer)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE ancestors AS (
        -- Base case: the tag itself
        SELECT target_tag as tag, 0 as level
        UNION ALL
        -- Recursive case: parent tags
        SELECT tr.parent_tag, a.level + 1
        FROM magic.tag_relationships tr
        JOIN ancestors a ON tr.child_tag = a.tag
        WHERE a.level < 100 -- prevent infinite recursion
    )
    SELECT a.tag, a.level
    FROM ancestors a
    WHERE a.tag != target_tag -- exclude the tag itself
    ORDER BY a.level;
END;
$$;


CREATE FUNCTION magic.get_tag_descendants(target_tag text) RETURNS TABLE(tag text, level integer)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE descendants AS (
        -- Base case: the tag itself
        SELECT target_tag as tag, 0 as level
        UNION ALL
        -- Recursive case: child tags
        SELECT tr.child_tag, d.level + 1
        FROM magic.tag_relationships tr
        JOIN descendants d ON tr.parent_tag = d.tag
        WHERE d.level < 100 -- prevent infinite recursion
    )
    SELECT d.tag, d.level
    FROM descendants d
    WHERE d.tag != target_tag -- exclude the tag itself
    ORDER BY d.level;
END;
$$;


CREATE FUNCTION magic.is_sorted_alphabetically(arr jsonb) RETURNS boolean
    LANGUAGE plpgsql IMMUTABLE
    AS $$
BEGIN
    RETURN arr = (
        SELECT jsonb_agg(value ORDER BY value)
        FROM jsonb_array_elements_text(arr)
    );
END;
$$;


CREATE FUNCTION magic.json_array_to_array(jsonbin jsonb) RETURNS text[]
    LANGUAGE sql IMMUTABLE
    AS $$
  SELECT array_agg(el) FROM jsonb_array_elements_text(jsonbin) el;
$$;


CREATE TABLE magic.cards (
    -- integer columns
    card_rarity_int integer,
    cmc integer,
    collector_number_int integer,
    creature_power integer,
    creature_toughness integer,
    planeswalker_loyalty integer,
    edhrec_rank integer,

    -- real columns
    price_usd real,
    price_eur real,
    price_tix real,

    scryfall_id UUID NOT NULL,
    set_name TEXT,
    oracle_id UUID,
    type_line text,
    illustration_id UUID,
    released_at date NOT NULL,

    -- columns
    card_name text NOT NULL,
    oracle_text text,
    raw_card_blob jsonb NOT NULL,
    mana_cost_text text,
    mana_cost_jsonb jsonb,
    devotion jsonb,
    card_types jsonb NOT NULL,
    card_subtypes jsonb DEFAULT '[]'::jsonb NOT NULL,
    card_colors jsonb NOT NULL,
    card_color_identity jsonb NOT NULL,
    card_keywords jsonb NOT NULL,
    creature_power_text text,
    creature_toughness_text text,
    planeswalker_loyalty_text text,
    card_oracle_tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    card_set_code text,
    card_artist text,
    card_rarity_text text,
    card_legalities jsonb DEFAULT '{}'::jsonb NOT NULL,
    collector_number text,
    produced_mana jsonb DEFAULT '{}'::jsonb NOT NULL,
    card_frame_data jsonb DEFAULT '{}'::jsonb NOT NULL,
    flavor_text text,
    card_is_tags jsonb DEFAULT '{}'::jsonb NOT NULL,
    card_layout text,
    card_border text,
    card_watermark text,

    -- constraints
    CONSTRAINT card_color_identity_must_be_object CHECK ((jsonb_typeof(card_color_identity) = 'object'::text)),
    CONSTRAINT card_color_identity_valid_colors CHECK ((card_color_identity <@ '{"B": true, "C": true, "G": true, "R": true, "U": true, "W": true}'::jsonb)),
    CONSTRAINT card_colors_must_be_object CHECK ((jsonb_typeof(card_colors) = 'object'::text)),
    CONSTRAINT card_colors_valid_colors CHECK ((card_colors <@ '{"B": true, "C": true, "G": true, "R": true, "U": true, "W": true}'::jsonb)),
    CONSTRAINT devotion_must_be_object CHECK ((jsonb_typeof(devotion) = 'object'::text)),
    CONSTRAINT card_frame_data_must_be_object CHECK ((jsonb_typeof(card_frame_data) = 'object'::text)),
    CONSTRAINT card_is_tags_must_be_object CHECK ((jsonb_typeof(card_is_tags) = 'object'::text)),
    CONSTRAINT card_keywords_must_be_object CHECK ((jsonb_typeof(card_keywords) = 'object'::text)),
    CONSTRAINT card_legalities_must_be_object CHECK ((jsonb_typeof(card_legalities) = 'object'::text)),
    CONSTRAINT card_oracle_tags_must_be_object CHECK ((jsonb_typeof(card_oracle_tags) = 'object'::text)),
    CONSTRAINT card_subtypes_must_be_array CHECK ((jsonb_typeof(card_subtypes) = 'array'::text)),
    CONSTRAINT card_types_must_be_array CHECK ((jsonb_typeof(card_types) = 'array'::text)),
    CONSTRAINT check_card_border_lowercase CHECK (((card_border IS NULL) OR (card_border = lower(card_border)))),
    CONSTRAINT check_card_layout_lowercase CHECK (((card_layout IS NULL) OR (card_layout = lower(card_layout)))),
    CONSTRAINT check_card_watermark_lowercase CHECK (((card_watermark IS NULL) OR (card_watermark = lower(card_watermark)))),
    CONSTRAINT creature_attributes_null_for_non_creatures CHECK (((card_types ?| ARRAY['Creature'::text]) OR (card_subtypes ?| ARRAY['Vehicle'::text, 'Spacecraft'::text]) OR ((creature_power IS NULL) AND (creature_power_text IS NULL) AND (creature_toughness IS NULL) AND (creature_toughness_text IS NULL)))),
    CONSTRAINT produced_mana_must_be_object CHECK ((jsonb_typeof(produced_mana) = 'object'::text)),
    CONSTRAINT produced_mana_valid_colors CHECK ((produced_mana <@ '{"B": true, "C": true, "G": true, "R": true, "U": true, "W": true}'::jsonb)),
    CONSTRAINT raw_card_is_object CHECK ((jsonb_typeof(raw_card_blob) = 'object'::text))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_scryfall_id ON magic.cards USING btree (scryfall_id);

COMMENT ON COLUMN magic.cards.card_artist IS 'Artist name for the card artwork - will be null for cards without artist information';
COMMENT ON COLUMN magic.cards.card_border IS 'Card border color (black, white, borderless, silver, gold) - stored in lowercase';
COMMENT ON COLUMN magic.cards.card_layout IS 'Card layout type (normal, split, flip, transform, etc.) - stored in lowercase';
COMMENT ON COLUMN magic.cards.card_legalities IS 'Card legality status in different formats stored as JSONB object, e.g. {"standard": "legal", "modern": "banned", "legacy": "restricted"}';
COMMENT ON COLUMN magic.cards.card_rarity_int IS 'Card rarity as integer for efficient ordering and comparison: common=0, uncommon=1, rare=2, mythic=3, special=4, bonus=5';
COMMENT ON COLUMN magic.cards.card_rarity_text IS 'Card rarity as text: common, uncommon, rare, mythic, special, bonus';
COMMENT ON COLUMN magic.cards.card_set_code IS 'Set code (e.g. "iko", "thb") - will be null for cards without set information';
COMMENT ON COLUMN magic.cards.card_watermark IS 'Card watermark (guild symbols, set symbols, etc.) - stored in lowercase';
COMMENT ON COLUMN magic.cards.collector_number IS 'Card collector number as text exactly as it appears on the card (can be numeric or contain letters like "123a")';
COMMENT ON COLUMN magic.cards.collector_number_int IS 'Card collector number as integer extracted from text for numeric comparisons (e.g., "123a" -> 123, NULL for non-numeric)';
COMMENT ON COLUMN magic.cards.flavor_text IS 'Card flavor text for flavor text search (flavor:)';
COMMENT ON COLUMN magic.cards.price_eur IS 'Price in Euros - will be null for cards without pricing information';
COMMENT ON COLUMN magic.cards.price_tix IS 'Price in MTGO Tickets - will be null for cards without pricing information';
COMMENT ON COLUMN magic.cards.price_usd IS 'Price in US Dollars - will be null for cards without pricing information';
COMMENT ON COLUMN magic.cards.produced_mana IS 'Mana colors that this card can produce stored as object with color codes as keys (e.g., {"G": true, "R": true})';


CREATE TABLE magic.tag_relationships (
    child_tag text NOT NULL,
    parent_tag text NOT NULL,
    PRIMARY KEY (child_tag, parent_tag),
    CONSTRAINT no_self_reference CHECK ((child_tag <> parent_tag))
);

CREATE INDEX IF NOT EXISTS idx_tag_relationships_child ON magic.tag_relationships USING btree (child_tag);
CREATE INDEX IF NOT EXISTS idx_tag_relationships_parent ON magic.tag_relationships USING btree (parent_tag);


CREATE TABLE magic.tags (
    tag text NOT NULL,
    PRIMARY KEY (tag)
);

CREATE VIEW magic.leaf_tags AS
 SELECT tag
   FROM magic.tags t
  WHERE (NOT (tag IN ( SELECT DISTINCT tag_relationships.parent_tag
           FROM magic.tag_relationships
          WHERE (tag_relationships.parent_tag IS NOT NULL))))
  ORDER BY tag;

CREATE VIEW magic.root_tags AS
 SELECT tag
   FROM magic.tags t
  WHERE (NOT (tag IN ( SELECT DISTINCT tag_relationships.child_tag
           FROM magic.tag_relationships
          WHERE (tag_relationships.child_tag IS NOT NULL))))
  ORDER BY tag;


CREATE TABLE magic.valid_rarities (
    card_rarity_int integer NOT NULL,
    card_rarity_text text NOT NULL
);

INSERT INTO magic.valid_rarities (card_rarity_int, card_rarity_text) VALUES
    (0, 'common'),
    (1, 'uncommon'),
    (2, 'rare'),
    (3, 'mythic'),
    (4, 'special'),
    (5, 'bonus');

COMMENT ON TABLE magic.valid_rarities IS 'Lookup table for valid card rarities with integer and text representations';


ALTER TABLE ONLY magic.valid_rarities
    ADD CONSTRAINT valid_rarities_card_rarity_int_card_rarity_text_key UNIQUE (card_rarity_int, card_rarity_text);


ALTER TABLE ONLY magic.valid_rarities
    ADD CONSTRAINT valid_rarities_card_rarity_text_key UNIQUE (card_rarity_text);


ALTER TABLE ONLY magic.valid_rarities
    ADD CONSTRAINT valid_rarities_pkey PRIMARY KEY (card_rarity_int);


CREATE TRIGGER prevent_circular_references BEFORE INSERT OR UPDATE ON magic.tag_relationships FOR EACH ROW EXECUTE FUNCTION magic.check_circular_reference();


ALTER TABLE ONLY magic.cards
    ADD CONSTRAINT fk_cards_rarity FOREIGN KEY (card_rarity_int, card_rarity_text) REFERENCES magic.valid_rarities(card_rarity_int, card_rarity_text);


ALTER TABLE ONLY magic.tag_relationships
    ADD CONSTRAINT tag_relationships_child_tag_fkey FOREIGN KEY (child_tag) REFERENCES magic.tags(tag) ON DELETE CASCADE;


ALTER TABLE ONLY magic.tag_relationships
    ADD CONSTRAINT tag_relationships_parent_tag_fkey FOREIGN KEY (parent_tag) REFERENCES magic.tags(tag) ON DELETE CASCADE;


/*
boilerplate indexes
*/
/* gin indexes */
CREATE INDEX IF NOT EXISTS idx_cards_artist_trgm ON magic.cards USING gin (card_artist magic.gin_trgm_ops) WHERE (card_artist IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_cardname_trgm ON magic.cards USING gin (card_name magic.gin_trgm_ops) WHERE (card_name IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_flavor_text_trgm ON magic.cards USING gin (flavor_text magic.gin_trgm_ops) WHERE (flavor_text IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_oracle_text_trgm ON magic.cards USING gin (oracle_text magic.gin_trgm_ops);

/* hash indexes */
CREATE INDEX IF NOT EXISTS idx_cards_border ON magic.cards USING hash (card_border) WHERE (card_border IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_layout ON magic.cards USING hash (card_layout) WHERE (card_layout IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_set_code ON magic.cards USING hash (card_set_code) WHERE (card_set_code IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_watermark ON magic.cards USING hash (card_watermark) WHERE (card_watermark IS NOT NULL);

/* gin indexes */
CREATE INDEX IF NOT EXISTS idx_cards_cardsubtypes_gin ON magic.cards USING gin (card_subtypes);
CREATE INDEX IF NOT EXISTS idx_cards_cardtypes_gin ON magic.cards USING gin (card_types);
CREATE INDEX IF NOT EXISTS idx_cards_devotion ON magic.cards USING gin (devotion);
CREATE INDEX IF NOT EXISTS idx_cards_frame_data_gin ON magic.cards USING gin (card_frame_data);
CREATE INDEX IF NOT EXISTS idx_cards_is_tags_gin ON magic.cards USING gin (card_is_tags);
CREATE INDEX IF NOT EXISTS idx_cards_legalities ON magic.cards USING gin (card_legalities);
CREATE INDEX IF NOT EXISTS idx_cards_produced_mana ON magic.cards USING gin (produced_mana);

/* btree */
CREATE INDEX IF NOT EXISTS idx_cards_collector_number ON magic.cards USING btree (collector_number) WHERE (collector_number IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_collector_number_int ON magic.cards USING btree (collector_number_int) WHERE (collector_number_int IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_color_identity_gin ON magic.cards USING gin (card_color_identity);
CREATE INDEX IF NOT EXISTS idx_cards_colors_gin ON magic.cards USING gin (card_colors);
CREATE INDEX IF NOT EXISTS idx_cards_creature_power_btree ON magic.cards USING btree (creature_power) WHERE (creature_power IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_creature_toughness_btree ON magic.cards USING btree (creature_toughness) WHERE (creature_toughness IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_price_eur ON magic.cards USING btree (price_eur) WHERE (price_eur IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_price_tix ON magic.cards USING btree (price_tix) WHERE (price_tix IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_price_usd ON magic.cards USING btree (price_usd) WHERE (price_usd IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cards_releasedat ON magic.cards USING btree (released_at);
