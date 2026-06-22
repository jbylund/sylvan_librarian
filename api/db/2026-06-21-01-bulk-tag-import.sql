-- Rename generic tag tables to oracle-specific names.
ALTER TABLE magic.tags RENAME TO oracle_tags;
ALTER TABLE magic.tag_relationships RENAME TO oracle_tag_relationships;
ALTER INDEX IF EXISTS idx_tag_relationships_child RENAME TO idx_oracle_tag_relationships_child;
ALTER INDEX IF EXISTS idx_tag_relationships_parent RENAME TO idx_oracle_tag_relationships_parent;

-- The check_circular_reference() function body still references magic.tag_relationships
-- by name (PL/pgSQL stores source text). Update it to reference the renamed table.
CREATE OR REPLACE FUNCTION magic.check_circular_reference() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF EXISTS (
        WITH RECURSIVE hierarchy AS (
            SELECT NEW.parent_tag AS tag, 1 AS depth
            UNION ALL
            SELECT tr.parent_tag, h.depth + 1
            FROM magic.oracle_tag_relationships tr
            JOIN hierarchy h ON tr.child_tag = h.tag
            WHERE h.depth < 100
        )
        SELECT 1 FROM hierarchy WHERE tag = NEW.child_tag
    ) THEN
        RAISE EXCEPTION 'Circular reference detected: % -> %', NEW.child_tag, NEW.parent_tag;
    END IF;
    RETURN NEW;
END;
$$;

-- Update recursive helper functions to reference the renamed table.
CREATE OR REPLACE FUNCTION magic.get_tag_ancestors(target_tag text) RETURNS TABLE(tag text, level integer)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE ancestors AS (
        SELECT target_tag AS tag, 0 AS level
        UNION ALL
        SELECT tr.parent_tag, a.level + 1
        FROM magic.oracle_tag_relationships tr
        JOIN ancestors a ON tr.child_tag = a.tag
        WHERE a.level < 100
    )
    SELECT a.tag, a.level
    FROM ancestors a
    WHERE a.tag != target_tag
    ORDER BY a.level;
END;
$$;

CREATE OR REPLACE FUNCTION magic.get_tag_descendants(target_tag text) RETURNS TABLE(tag text, level integer)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE descendants AS (
        SELECT target_tag AS tag, 0 AS level
        UNION ALL
        SELECT tr.child_tag, d.level + 1
        FROM magic.oracle_tag_relationships tr
        JOIN descendants d ON tr.parent_tag = d.tag
        WHERE d.level < 100
    )
    SELECT d.tag, d.level
    FROM descendants d
    WHERE d.tag != target_tag
    ORDER BY d.level;
END;
$$;

-- Art tag tables, mirroring the oracle tag structure.
CREATE TABLE magic.art_tags (
    tag text NOT NULL PRIMARY KEY
);

CREATE TABLE magic.art_tag_relationships (
    child_tag text NOT NULL,
    parent_tag text NOT NULL,
    PRIMARY KEY (child_tag, parent_tag),
    CONSTRAINT no_self_reference CHECK (child_tag <> parent_tag)
);

CREATE INDEX IF NOT EXISTS idx_art_tag_relationships_child ON magic.art_tag_relationships USING btree (child_tag);
CREATE INDEX IF NOT EXISTS idx_art_tag_relationships_parent ON magic.art_tag_relationships USING btree (parent_tag);

ALTER TABLE ONLY magic.art_tag_relationships
    ADD CONSTRAINT art_tag_relationships_child_tag_fkey FOREIGN KEY (child_tag) REFERENCES magic.art_tags(tag) ON DELETE CASCADE;

ALTER TABLE ONLY magic.art_tag_relationships
    ADD CONSTRAINT art_tag_relationships_parent_tag_fkey FOREIGN KEY (parent_tag) REFERENCES magic.art_tags(tag) ON DELETE CASCADE;

CREATE FUNCTION magic.check_art_circular_reference() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF EXISTS (
        WITH RECURSIVE hierarchy AS (
            SELECT NEW.parent_tag AS tag, 1 AS depth
            UNION ALL
            SELECT tr.parent_tag, h.depth + 1
            FROM magic.art_tag_relationships tr
            JOIN hierarchy h ON tr.child_tag = h.tag
            WHERE h.depth < 100
        )
        SELECT 1 FROM hierarchy WHERE tag = NEW.child_tag
    ) THEN
        RAISE EXCEPTION 'Circular reference detected: % -> %', NEW.child_tag, NEW.parent_tag;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER prevent_circular_references
    BEFORE INSERT OR UPDATE ON magic.art_tag_relationships
    FOR EACH ROW EXECUTE FUNCTION magic.check_art_circular_reference();

-- Art tags column on cards, mirroring card_oracle_tags.
ALTER TABLE magic.cards ADD COLUMN card_art_tags jsonb DEFAULT '{}'::jsonb NOT NULL;
ALTER TABLE magic.cards ADD CONSTRAINT card_art_tags_must_be_object CHECK (jsonb_typeof(card_art_tags) = 'object');
CREATE INDEX IF NOT EXISTS idx_cards_art_tags_gin ON magic.cards USING gin (card_art_tags);

-- Indexes needed for the batch UPDATE WHERE oracle_id/illustration_id = ANY(...).
CREATE INDEX IF NOT EXISTS idx_cards_oracle_id ON magic.cards USING btree (oracle_id);
CREATE INDEX IF NOT EXISTS idx_cards_illustration_id ON magic.cards USING btree (illustration_id);
