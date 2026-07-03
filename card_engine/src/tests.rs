use super::{
    Card, CardData, CardIndexes, CmpOp, CollField, CollectionVocabs, FilterExpr, InlineStr,
    Interner, ManaCost, NONE_STR, TYPE_ARTIFACT, TYPE_CREATURE, TYPE_INSTANT, TYPE_LEGENDARY,
    TYPE_PLANESWALKER, TagIndex, TrigramIndex, build_numeric_index, build_oracle_text_index,
    build_tag_index, build_trigram_index, build_type_index, build_vocab_index,
    count_common_keywords, count_common_types, narrow_candidates, trigram_candidates,
};
use rkyv::{Archived, rancor::Error};
use std::collections::{HashMap, HashSet};

/// Build a TrigramIndex mapping each word's trigrams to the given card ids.
fn index_of(words: &[(&str, &[u32])]) -> TrigramIndex {
    let mut idx: TrigramIndex = HashMap::new();
    for (word, cards) in words {
        for w in word.as_bytes().windows(3) {
            let entry = idx.entry([w[0], w[1], w[2]]).or_default();
            for &c in *cards {
                if !entry.contains(&c) {
                    entry.push(c);
                }
            }
            entry.sort_unstable();
        }
    }
    idx
}

/// Archive the index and query it, matching how the engine reads the shared snapshot.
fn candidates(idx: &TrigramIndex, word: &str) -> Option<Vec<u32>> {
    let bytes = rkyv::to_bytes::<Error>(idx).expect("serialize trigram index");
    let archived =
        rkyv::access::<Archived<TrigramIndex>, Error>(&bytes).expect("access trigram index");
    trigram_candidates(archived, word)
}

#[test]
fn trigram_short_word_cannot_narrow() {
    let idx = index_of(&[("bolt", &[1, 2])]);
    assert_eq!(candidates(&idx, "bo"), None);
}

#[test]
fn trigram_all_present_intersects() {
    // "bol" → {1,2,3}, "olt" → {1,2}: intersection is {1,2}
    let idx = index_of(&[("bol", &[1, 2, 3]), ("olt", &[1, 2])]);
    assert_eq!(candidates(&idx, "bolt"), Some(vec![1, 2]));
}

#[test]
fn trigram_missing_means_no_candidates() {
    // "bol" is indexed but "olx" appears in no card, which proves nothing can
    // match — the result must be the empty candidate set, not an intersection
    // of whichever trigrams happen to exist.
    let idx = index_of(&[("bolt", &[1, 2])]);
    assert_eq!(candidates(&idx, "bolx"), Some(Vec::new()));
}

#[test]
fn trigram_fully_unindexed_word_is_empty_not_unnarrowed() {
    let idx = index_of(&[("bolt", &[1, 2])]);
    assert_eq!(candidates(&idx, "zzz"), Some(Vec::new()));
}

// Verify that HashMap<[u8; 3], Vec<u32>> (the trigram index key type) round-trips
// through rkyv and supports lookup via the same [u8; 3] key type.
#[test]
fn test_trigram_index_archive_and_lookup() {
    let mut idx: HashMap<[u8; 3], Vec<u32>> = HashMap::new();
    idx.insert([b'a', b'b', b'c'], vec![1, 2, 3]);
    idx.insert([b'x', b'y', b'z'], vec![4, 5]);
    idx.insert([b'f', b'o', b'o'], vec![10, 20, 30, 40]);

    let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize trigram index");
    let archived = rkyv::access::<rkyv::Archived<HashMap<[u8; 3], Vec<u32>>>, Error>(&bytes)
        .expect("access trigram index");

    // Key present -- length check
    let abc = archived
        .get(&[b'a', b'b', b'c'])
        .expect("abc must be present");
    assert_eq!(abc.len(), 3);

    // Key present -- value iteration (elements are rend::u32_le, not u32)
    let foo: Vec<u32> = archived
        .get(&[b'f', b'o', b'o'])
        .expect("foo must be present")
        .iter()
        .map(|x| u32::from(*x))
        .collect();
    assert_eq!(foo, vec![10, 20, 30, 40]);

    // Key absent
    assert!(archived.get(&[b'z', b'z', b'z']).is_none());
}

// Verify that HashSet<String> supports contains() via &str (Borrow-based lookup).
// This is still used for card_art_tags.
#[test]
fn test_hashset_string_str_lookup() {
    let mut set: HashSet<String> = HashSet::new();
    set.insert("Flying".to_string());
    set.insert("Vigilance".to_string());
    set.insert("Trample".to_string());

    let bytes = rkyv::to_bytes::<Error>(&set).expect("serialize hashset");
    let archived =
        rkyv::access::<rkyv::Archived<HashSet<String>>, Error>(&bytes).expect("access hashset");

    assert!(archived.contains("Flying"));
    assert!(archived.contains("Trample"));
    assert!(!archived.contains("Deathtouch"));
}

// Verify that HashMap<String, Vec<u32>> (the tag index value type) supports
// get() via &str, which is needed for narrow_candidates tag lookups.
#[test]
fn test_tag_index_str_lookup() {
    let mut idx: HashMap<String, Vec<u32>> = HashMap::new();
    idx.insert("merfolk".to_string(), vec![1, 5, 9]);
    idx.insert("dragon".to_string(), vec![2, 7]);

    let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize tag index");
    let archived = rkyv::access::<rkyv::Archived<HashMap<String, Vec<u32>>>, Error>(&bytes)
        .expect("access tag index");

    let merfolk: Vec<u32> = archived
        .get("merfolk")
        .expect("merfolk must be present")
        .iter()
        .map(|x| u32::from(*x))
        .collect();
    assert_eq!(merfolk, vec![1, 5, 9]);
    assert!(archived.get("angel").is_none());
}

/// Measures checked rkyv::access vs access_unchecked on a production-scale
/// archive. This is the evidence behind the access_unchecked safety comments
/// on query()/size(): checked access re-validates the entire archive graph
/// on every call, which is milliseconds per call at ~100k cards — orders of
/// magnitude over total query time. Run with:
///   cargo test --release -- --ignored bench_checked_vs_unchecked --nocapture
#[test]
#[ignore]
fn bench_checked_vs_unchecked_access() {
    const N: usize = 96_000;
    let words = [
        "draw", "card", "creature", "destroy", "target", "flying", "counter", "spell", "token",
        "exile",
    ];
    let mut interner = Interner::new();
    let mut subtype_vocab: HashMap<String, u32> = HashMap::new();
    let mut subtype_vocab_values: Vec<String> = Vec::new();
    let mut keyword_vocab: HashMap<String, u32> = HashMap::new();
    let mut keyword_vocab_values: Vec<String> = Vec::new();
    let mut oracle_tag_vocab: HashMap<String, u32> = HashMap::new();
    let mut oracle_tag_vocab_values: Vec<String> = Vec::new();
    let intern_vocab =
        |map: &mut HashMap<String, u32>, values: &mut Vec<String>, value: String| -> u32 {
            if let Some(id) = map.get(&value) {
                *id
            } else {
                let id = values.len() as u32;
                values.push(value.clone());
                map.insert(value, id);
                id
            }
        };
    let mut cards: Vec<Card> = Vec::with_capacity(N);
    for i in 0..N {
        let name = format!("Benchmark Card Number {i}");
        // Oracle text keyed on i/3 so printings share texts ~3x, matching the
        // real dataset's duplication (and exercising the CSR oracle index).
        let group = i / 3;
        let oracle = format!(
            "{}: {} a {} {}, then {} {} cards. This text is representative filler standing in for \
             real oracle text so string validation cost is realistic for card group {group}.",
            words[group % 10],
            words[(group + 1) % 10],
            words[(group + 2) % 10],
            words[(group + 3) % 10],
            words[(group + 4) % 10],
            words[(group + 5) % 10],
        );
        let flavor = format!(
            "Flavor text for card {i}, roughly the length of a real flavor quote in the dataset."
        );
        cards.push(Card {
            card_name_lower: InlineStr::from_str(&name.to_lowercase()),
            card_colors: (i % 32) as u8,
            card_color_identity: (i % 32) as u8,
            produced_mana: 0,
            card_types: TYPE_CREATURE,
            scryfall_id: (i + 1) as u128,
            oracle_id: (group + 1) as u128,
            illustration_id: (i + 1) as u128,
            card_name_id: interner.intern(name.clone()),
            oracle_text_id: interner.intern(oracle.clone()),
            oracle_text_lower_id: interner.intern(oracle.to_lowercase()),
            flavor_text_id: interner.intern(flavor.clone()),
            flavor_text_lower_id: interner.intern(flavor.to_lowercase()),
            card_artist_id: interner.intern(format!("Artist {}", i % 1000)),
            card_artist_lower_id: interner.intern(format!("artist {}", i % 1000)),
            card_set_code: InlineStr::from_str("bench"),
            card_layout_id: interner.intern("normal".to_string()),
            card_border_id: interner.intern("black".to_string()),
            card_watermark_id: NONE_STR,
            collector_number_id: interner.intern(format!("{}", i % 500)),
            mana_cost_text_id: interner.intern("{2}{G}{G}".to_string()),
            type_line_id: interner.intern("Creature — Benchmark".to_string()),
            set_name_id: interner.intern(format!("Benchmark Set {}", i % 300)),
            released_at_int: Some(20240101),
            cmc: Some((i % 8) as u8),
            creature_power: Some((i % 10) as i8),
            creature_toughness: Some((i % 10) as i8),
            planeswalker_loyalty: None,
            card_rarity_int: Some((i % 4) as u8),
            collector_number_int: Some((i % 500) as u16),
            edhrec_rank: Some(i as u32),
            price_usd: Some(1.0),
            price_eur: Some(1.0),
            price_tix: Some(0.1),
            prefer_score: None,
            cubecobra_score: None,
            card_subtypes: vec![
                intern_vocab(
                    &mut subtype_vocab,
                    &mut subtype_vocab_values,
                    "Benchmark".to_string(),
                ),
                intern_vocab(
                    &mut subtype_vocab,
                    &mut subtype_vocab_values,
                    words[i % 10].to_string(),
                ),
            ],
            card_keywords: vec![intern_vocab(
                &mut keyword_vocab,
                &mut keyword_vocab_values,
                words[i % 10].to_string(),
            )],
            card_legalities: 0,
            card_oracle_tags: vec![intern_vocab(
                &mut oracle_tag_vocab,
                &mut oracle_tag_vocab_values,
                format!("tag-{}", i % 100),
            )],
            card_art_tags: HashSet::new(),
            card_is_tags: vec![],
            card_frame_data: vec![],
            mana_cost: ManaCost {
                pips: HashMap::from([("G".to_string(), 2u8)]),
                devotion: None,
                cmc: (i % 8) as f32,
            },
            creature_power_text_id: NONE_STR,
            creature_toughness_text_id: NONE_STR,
        });
    }
    let strings = interner.strings;
    let collection_vocabs = CollectionVocabs {
        subtypes: subtype_vocab_values,
        keywords: keyword_vocab_values,
        oracle_tags: oracle_tag_vocab_values,
        is_tags: vec![],
        frame_data: vec![],
    };

    let indexes = CardIndexes {
        name_trigram: build_trigram_index(&cards, |c| c.card_name_lower.as_str()),
        oracle_trigram: build_oracle_text_index(&cards, &strings),
        cmc: build_numeric_index(&cards, |c| c.cmc.map(|v| v as i16)),
        power: build_numeric_index(&cards, |c| c.creature_power.map(|v| v as i16)),
        toughness: build_numeric_index(&cards, |c| c.creature_toughness.map(|v| v as i16)),
        type_bits: build_type_index(&cards),
        subtypes: build_vocab_index(&cards, |c| &c.card_subtypes, &collection_vocabs.subtypes),
        keywords: build_vocab_index(&cards, |c| &c.card_keywords, &collection_vocabs.keywords),
        oracle_tags: build_vocab_index(
            &cards,
            |c| &c.card_oracle_tags,
            &collection_vocabs.oracle_tags,
        ),
        art_tags: build_tag_index(&cards, |c| &c.card_art_tags),
        is_tags: build_vocab_index(&cards, |c| &c.card_is_tags, &collection_vocabs.is_tags),
    };
    let data = CardData {
        cards,
        strings,
        collection_vocabs,
        indexes,
        format_shifts: HashMap::new(),
        preferred_indices: Vec::new(),
    };
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    println!("archive size: {:.1} MB", bytes.len() as f64 / 1e6);

    const ITERS: u32 = 10;
    let t = std::time::Instant::now();
    for _ in 0..ITERS {
        let a = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("checked access");
        assert_eq!(a.cards.len(), N);
    }
    let checked = t.elapsed() / ITERS;

    let t = std::time::Instant::now();
    for _ in 0..ITERS {
        let a = unsafe { rkyv::access_unchecked::<Archived<CardData>>(&bytes) };
        assert_eq!(a.cards.len(), N);
    }
    let unchecked = t.elapsed() / ITERS;

    println!("checked rkyv::access:   {checked:?} per call");
    println!("access_unchecked:       {unchecked:?} per call");
}

// Verify that narrow_candidates returns the correct card ids for an art tag
// that is present in the index, and returns None (no narrowing) for an absent tag.
#[test]
fn narrow_candidates_art_tags() {
    let mut art_tags: TagIndex = HashMap::new();
    art_tags.insert("wolf".to_string(), vec![0, 2]);
    art_tags.insert("dragon".to_string(), vec![1]);

    let indexes = CardIndexes {
        art_tags,
        ..Default::default()
    };
    let bytes = rkyv::to_bytes::<Error>(&indexes).expect("serialize");
    let archived = rkyv::access::<Archived<CardIndexes>, Error>(&bytes).expect("access");

    let present = FilterExpr::CollectionCmp {
        field: CollField::ArtTags,
        op: CmpOp::Ge,
        value: "wolf".to_string(),
    };
    assert_eq!(narrow_candidates(&present, archived), Some(vec![0, 2]));

    // A tag not in the index cannot narrow; the eval step handles correctness.
    let absent = FilterExpr::CollectionCmp {
        field: CollField::ArtTags,
        op: CmpOp::Ge,
        value: "zombie".to_string(),
    };
    assert_eq!(narrow_candidates(&absent, archived), None);
}

/// Minimal card for tests that only care about card_types and card_subtypes ids.
/// All interned-string IDs are NONE_STR; count_common_types never reads them.
fn stub_card(card_types: u16, card_subtypes: Vec<u32>) -> Card {
    Card {
        card_name_lower: InlineStr::from_str(""),
        card_colors: 0,
        card_color_identity: 0,
        produced_mana: 0,
        card_types,
        scryfall_id: 0,
        oracle_id: 0,
        illustration_id: 0,
        card_name_id: NONE_STR,
        oracle_text_id: NONE_STR,
        oracle_text_lower_id: NONE_STR,
        flavor_text_id: NONE_STR,
        flavor_text_lower_id: NONE_STR,
        card_artist_id: NONE_STR,
        card_artist_lower_id: NONE_STR,
        card_set_code: InlineStr::from_str(""),
        card_layout_id: NONE_STR,
        card_border_id: NONE_STR,
        card_watermark_id: NONE_STR,
        collector_number_id: NONE_STR,
        mana_cost_text_id: NONE_STR,
        type_line_id: NONE_STR,
        set_name_id: NONE_STR,
        released_at_int: None,
        cmc: None,
        creature_power: None,
        creature_toughness: None,
        planeswalker_loyalty: None,
        card_rarity_int: None,
        collector_number_int: None,
        edhrec_rank: None,
        price_usd: None,
        price_eur: None,
        price_tix: None,
        prefer_score: None,
        cubecobra_score: None,
        card_subtypes,
        card_keywords: vec![],
        card_legalities: 0,
        card_oracle_tags: vec![],
        card_art_tags: HashSet::new(),
        card_is_tags: vec![],
        card_frame_data: vec![],
        mana_cost: ManaCost {
            pips: HashMap::new(),
            devotion: None,
            cmc: 0.0,
        },
        creature_power_text_id: NONE_STR,
        creature_toughness_text_id: NONE_STR,
    }
}

#[test]
fn count_common_types_sums_preferred_only() {
    // card 0: Legendary Planeswalker, subtype "Jace"   — preferred
    // card 1: Instant, no subtypes                     — not preferred (skipped)
    // card 2: Artifact + Creature, subtype "Merfolk"   — preferred
    // card 3: Creature, subtypes ["Warrior", "Merfolk"] — preferred
    let collection_vocabs = CollectionVocabs {
        subtypes: vec![
            "Jace".to_string(),
            "Merfolk".to_string(),
            "Warrior".to_string(),
        ],
        ..Default::default()
    };
    let cards = vec![
        stub_card(TYPE_LEGENDARY | TYPE_PLANESWALKER, vec![0]),
        stub_card(TYPE_INSTANT, vec![]),
        stub_card(TYPE_ARTIFACT | TYPE_CREATURE, vec![1]),
        stub_card(TYPE_CREATURE, vec![2, 1]),
    ];
    let data = CardData {
        cards,
        strings: vec![],
        collection_vocabs,
        indexes: CardIndexes::default(),
        format_shifts: HashMap::new(),
        preferred_indices: vec![0, 2, 3], // card 1 (Instant) excluded
    };
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let counts = count_common_types(archived);

    // Type bits decoded correctly from the bitmask.
    assert_eq!(counts.get("Legendary"), Some(&1));
    assert_eq!(counts.get("Planeswalker"), Some(&1));
    assert_eq!(counts.get("Artifact"), Some(&1));
    assert_eq!(counts.get("Creature"), Some(&2)); // cards 2 and 3

    // Card 1 (Instant) is not in preferred_indices — must be absent.
    assert_eq!(counts.get("Instant"), None);

    // Subtypes borrowed from archive strings; "Merfolk" appears in two preferred cards.
    assert_eq!(counts.get("Merfolk"), Some(&2));
    assert_eq!(counts.get("Warrior"), Some(&1));
    assert_eq!(counts.get("Jace"), Some(&1));

    // Types with zero count are never emitted.
    assert_eq!(counts.get("Land"), None);
    assert_eq!(counts.get("Sorcery"), None);
}

#[test]
fn count_common_keywords_sums_preferred_only() {
    // card 0: Flying + Haste  — preferred
    // card 1: Trample         — not preferred (skipped)
    // card 2: Flying          — preferred
    // card 3: Vigilance       — preferred
    let collection_vocabs = CollectionVocabs {
        keywords: vec![
            "Flying".to_string(),
            "Haste".to_string(),
            "Trample".to_string(),
            "Vigilance".to_string(),
        ],
        ..Default::default()
    };

    let mut card0 = stub_card(TYPE_CREATURE, vec![]);
    card0.card_keywords = vec![0, 1];
    let mut card1 = stub_card(TYPE_INSTANT, vec![]);
    card1.card_keywords = vec![2];
    let mut card2 = stub_card(TYPE_CREATURE, vec![]);
    card2.card_keywords = vec![0];
    let mut card3 = stub_card(TYPE_ARTIFACT, vec![]);
    card3.card_keywords = vec![3];

    let data = CardData {
        cards: vec![card0, card1, card2, card3],
        strings: vec![],
        collection_vocabs,
        indexes: CardIndexes::default(),
        format_shifts: HashMap::new(),
        preferred_indices: vec![0, 2, 3], // card 1 (Instant) excluded
    };
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let counts = count_common_keywords(archived);

    // Flying appears on cards 0 and 2 (both preferred).
    assert_eq!(counts.get("Flying"), Some(&2));
    // Haste only on card 0 (preferred).
    assert_eq!(counts.get("Haste"), Some(&1));
    // Vigilance only on card 3 (preferred).
    assert_eq!(counts.get("Vigilance"), Some(&1));
    // Trample on card 1 — not preferred, must be absent.
    assert_eq!(counts.get("Trample"), None);
}
