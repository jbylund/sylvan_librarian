-- Minimal test data for integration tests
--
-- mana_cost_jsonb/devotion are dict[symbol, list[int]] (array length = pip
-- count) — NOT plain integers — matching mana_cost_str_to_dict()/
-- calculate_devotion() in api/parsing/card_query_nodes.py. devotion is only
-- nonempty for permanents (Creature/Artifact/etc, never Instant/Sorcery) —
-- confirmed against the real Scryfall API, where devotion: never matches a
-- pure Instant (e.g. the real Lightning Bolt).

-- Insert some test cards
INSERT INTO magic.cards (
    scryfall_id, oracle_id, card_name, cmc, mana_cost_text, mana_cost_jsonb, devotion, raw_card_blob,
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
    '{"R": [1]}',
    '{}', -- Instant: nonpermanent, so devotion is empty despite the {R} cost
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
    '{"W": [1, 2]}', -- pure-generic braces like {3} are dropped, never a key
    '{"W": [1, 2]}', -- Creature: a permanent, so this is the positive devotion control
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
),
(
    -- Boggart Ram-Gang {R/G}{R/G}{R/G} (real card, shm #203): a hybrid
    -- permanent — mana:{R} must NOT match it (opaque hybrid key), but
    -- devotion:{R} and devotion:{G} both do (hybrid symbols split for devotion).
    '00000000-0000-0000-0000-000000000004',
    '30d2437a-87c9-4f88-8fb8-b686d6522677',
    'Boggart Ram-Gang',
    3,
    '{R/G}{R/G}{R/G}',
    '{"R/G": [1, 2, 3]}',
    '{"R": [1, 2, 3], "G": [1, 2, 3]}',
    '{"name": "Boggart Ram-Gang", "type": "Creature", "collector_number": "203"}',
    '["Creature"]',
    '["Goblin", "Warrior"]',
    '{"R": true, "G": true}',
    '{"R": true, "G": true}',
    '{}',
    'Whenever Boggart Ram-Gang attacks, you may sacrifice another creature. If you do, target creature gets +2/+0 until end of turn.',
    3,
    3,
    '{}',
    '203',
    203,
    '2008-05-02'
),
(
    -- Cathedral Membrane {1}{W/P} (real card, nph #5): a Phyrexian permanent —
    -- mana:{W} must NOT match it (opaque hybrid key, same as any other
    -- hybrid), but devotion:{W} does (P isn't a tracked devotion letter).
    '00000000-0000-0000-0000-000000000005',
    '0297eeb2-47ec-4f9a-b1ad-4e7286994f00',
    'Cathedral Membrane',
    2,
    '{1}{W/P}',
    '{"W/P": [1]}',
    '{"W": [1]}',
    '{"name": "Cathedral Membrane", "type": "Artifact Creature", "collector_number": "5"}',
    '["Artifact", "Creature"]',
    '["Phyrexian", "Wall"]',
    '{"W": true}',
    '{"W": true}',
    '{}',
    '({W/P} can be paid with either {W} or 2 life.)\nDefender',
    0,
    3,
    '{}',
    '5',
    5,
    '2011-05-13'
),
(
    -- Fireball {X}{R} (real card, clb #175): X is its own pip symbol (not a
    -- hybrid) and contributes 0 to cmc — mana:{X} and bare mana:x must both
    -- match it, and mana:{X}{R}{R} (implied cmc 2) must not (cmc is 1).
    -- Sorcery: nonpermanent, so devotion is empty despite the {R} pip.
    '00000000-0000-0000-0000-000000000006',
    'aa7714b0-2bfb-458a-8ebf-37ec2c53383e',
    'Fireball',
    1,
    '{X}{R}',
    '{"X": [1], "R": [1]}',
    '{}',
    '{"name": "Fireball", "type": "Sorcery", "collector_number": "175"}',
    '["Sorcery"]',
    '[]',
    '{"R": true}',
    '{"R": true}',
    '{}',
    'This spell costs {1} more to cast for each target it has beyond the first.\nFireball deals X damage divided as you choose among any number of targets.',
    NULL,
    NULL,
    '{}',
    '175',
    175,
    '2022-06-10'
) ON CONFLICT (scryfall_id) DO NOTHING;

-- Insert test tags
INSERT INTO magic.oracle_tags (tag) VALUES
('flying'),
('vigilance'),
('burn'),
('mana-acceleration')
ON CONFLICT (tag) DO NOTHING;
