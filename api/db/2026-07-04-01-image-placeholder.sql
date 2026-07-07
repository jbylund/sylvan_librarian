-- Migration: Add image placeholder value
-- Display-only placeholder data for this printing's card image: "<bucket> <frameL> <frameR> <art>"
-- e.g. "modern-r 8a3b2f 6a4a3f 334455". The bucket names a shared grayscale frame template
-- (frame generation x color group, pure function of card metadata — see
-- scripts/placeholder_measurement.py); the three hex colors are measured from the printing's
-- 280px image and tint that template client-side (classes in api/static/placeholders-v1.css).
-- NULL means not yet measured; clients fall back to a mana-cost/type-derived ph-fb-* class.
-- Per printing, not per face (faces collapse to one row on upsert; the image pipeline
-- currently only handles face 1).
-- Populated by scripts/copy_images_to_s3.py at image-processing time (--assign-only to backfill).
-- Never filtered or searched on, so no index.

ALTER TABLE magic.cards ADD COLUMN IF NOT EXISTS image_placeholder text DEFAULT NULL;
