UPDATE magic.cards SET flavor_text = '' WHERE flavor_text IS NULL;
ALTER TABLE magic.cards ALTER COLUMN flavor_text SET NOT NULL;
ALTER TABLE magic.cards ALTER COLUMN flavor_text SET DEFAULT '';
