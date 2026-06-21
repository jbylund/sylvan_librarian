-- Minimal test data for integration tests

-- Insert some test cards
INSERT INTO magic.cards (
    scryfall_id, oracle_id, card_name, cmc, mana_cost_text, mana_cost_jsonb, raw_card_blob,
    card_types, card_subtypes, card_colors, card_color_identity, card_keywords,
    oracle_text, creature_power, creature_toughness, card_oracle_tags, collector_number, collector_number_int,
    released_at
) VALUES
(
    '00000000-0000-0000-0000-000000000001',
    '52b91fa6-7562-501c-a1b7-5d41f0c00020',
    'Lightning Bolt',
    1,
    '{R}',
    '{"R": 1}',
    '{"name": "Lightning Bolt", "type": "Instant", "collector_number": "123"}',
    '["Instant"]',
    '[]',
    '{"R": true}',
    '{"R": true}',
    '{}',
    'Lightning Bolt deals 3 damage to any target.',
    NULL,
    NULL,
    '{"burn": true}',
    '123',
    123,
    '2024-02-23'
),
(
    '00000000-0000-0000-0000-000000000002',
    'f2b54404-af3c-529a-b50b-ac1e1214fddd',
    'Serra Angel',
    5,
    '{3}{W}{W}',
    '{"3": 3, "W": 2}',
    '{"name": "Serra Angel", "type": "Creature", "collector_number": "45a"}',
    '["Creature"]',
    '["Angel"]',
    '{"W": true}',
    '{"W": true}',
    '{"Flying": true, "Vigilance": true}',
    'Flying, vigilance',
    4,
    4,
    '{"flying": true, "vigilance": true}',
    '45a',
    45,
    '2024-02-23'
),
(
    '00000000-0000-0000-0000-000000000003',
    '288bc0d8-1ee9-5af5-b226-38aec9cc1c5d',
    'Black Lotus',
    0,
    '{0}',
    '{}',
    '{"name": "Black Lotus", "type": "Artifact", "collector_number": "1"}',
    '["Artifact"]',
    '[]',
    '{}',
    '{}',
    '{}',
    '{T}, Sacrifice Black Lotus: Add three mana of any one color.',
    NULL,
    NULL,
    '{"mana-acceleration": true}',
    '1',
    1,
    '2024-02-23'
) ON CONFLICT (scryfall_id) DO NOTHING;

-- Insert test tags
INSERT INTO magic.oracle_tags (tag) VALUES
('flying'),
('vigilance'),
('burn'),
('mana-acceleration')
ON CONFLICT (tag) DO NOTHING;
