-- Migration: Add CubeCobra columns
-- Raw per-oracle-id data from the CubeCobra top-cards API; NULL means card not found in CubeCobra.
-- cubecobra_score is a pre-computed combined score (0=best, 100=worst; lower is better, conceptually similar to edhrec_rank).
-- Populated by POST /ingest_cubecobra and POST /backfill_cubecobra_scores.

ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS cubecobra_elo        real    DEFAULT NULL;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS cubecobra_cube_count integer DEFAULT NULL;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS cubecobra_pick_count integer DEFAULT NULL;
ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS cubecobra_score      real    DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_cards_cubecobra_elo_btree
    ON magic.cards (cubecobra_elo DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_cards_cubecobra_cube_count_btree
    ON magic.cards (cubecobra_cube_count DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_cards_cubecobra_score_btree
    ON magic.cards (cubecobra_score ASC NULLS LAST);
