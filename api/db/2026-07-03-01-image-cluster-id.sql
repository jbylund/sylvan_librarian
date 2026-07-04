-- Migration: Add image placeholder cluster id
-- Display-only id of the placeholder cluster this printing's image belongs to; NULL means not yet
-- assigned (clients fall back to a mana-cost-derived placeholder class). Ids reference the
-- codebook artifact scripts/placeholder_centroids_v1.json, whose blurred centroids ship as
-- CSS classes in api/static/placeholders-v1.css. Per printing, not per face (faces collapse to
-- one row on upsert; the image pipeline currently only handles face 1).
-- Populated by scripts/copy_images_to_s3.py at image-processing time (--assign-only to backfill).
-- Never filtered or searched on, so no index.

ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS image_cluster_id integer DEFAULT NULL;
