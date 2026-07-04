use super::{
    build_numeric_index, build_oracle_text_index, build_tag_index, build_trigram_index,
    build_type_index, cards_of_printings, count_common_keywords, count_common_types,
    build_artist_index, build_range_index, narrow_candidates, run_query, trigram_candidates,
    ArtistIndex, CardData, CardIndexes, Candidates,
    CollField, CmpOp, FilterExpr, InlineStr, Interner, ManaCost, OracleCard, Printing, TagIndex,
    Tri, TrigramIndex, VocabInterner, ARTIST_NONE, NONE_STR, TYPE_ARTIFACT, TYPE_CREATURE, TYPE_INSTANT,
    TYPE_LEGENDARY, TYPE_PLANESWALKER,
};
use rkyv::{rancor::Error, Archived};
use std::collections::HashMap;

/// String-sorted permutation of the vocab ids, as reload_commit builds it.
fn sorted_vocab_ids(vocab: &[String]) -> Vec<u16> {
    let mut ids: Vec<u16> = (0..vocab.len() as u16).collect();
    ids.sort_unstable_by(|&a, &b| vocab[a as usize].cmp(&vocab[b as usize]));
    ids
}

/// Intern a list of collection elements as sorted, deduped vocab ids (the load-time
/// shape of the set-like collections).
fn vocab_ids(vocab: &mut VocabInterner, items: &[&str]) -> Vec<u16> {
    let mut ids: Vec<u16> = items.iter().map(|s| vocab.intern(s.to_string()).unwrap()).collect();
    ids.sort_unstable();
    ids.dedup();
    ids
}

/// Build a TrigramIndex mapping each word's trigrams to the given card ids.
fn index_of(words: &[(&str, &[u32])]) -> TrigramIndex {
    let mut idx: TrigramIndex = HashMap::new();
    for (word, cards) in words {
        for w in word.as_bytes().windows(3) {
            let entry = idx.entry([w[0], w[1], w[2]]).or_default();
            for &c in *cards {
                if !entry.contains(&c) { entry.push(c); }
            }
            entry.sort_unstable();
        }
    }
    idx
}

/// Archive the index and query it, matching how the engine reads the shared snapshot.
fn candidates(idx: &TrigramIndex, word: &str) -> Option<Vec<u32>> {
    let bytes = rkyv::to_bytes::<Error>(idx).expect("serialize trigram index");
    let archived = rkyv::access::<Archived<TrigramIndex>, Error>(&bytes).expect("access trigram index");
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
    let abc = archived.get(&[b'a', b'b', b'c']).expect("abc must be present");
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

// ─── Two-level store fixtures ─────────────────────────────────────────────────

/// Minimal oracle card; interned-string IDs are NONE_STR.
fn stub_card(oracle_id: u128, card_types: u16, subtypes: &[&str], vocab: &mut VocabInterner) -> OracleCard {
    OracleCard {
        card_name_lower: InlineStr::from_str(""),
        card_colors: 0,
        card_color_identity: 0,
        produced_mana: 0,
        card_types,
        legality_divergent: false,
        oracle_id,
        card_name_id: NONE_STR,
        oracle_text_id: NONE_STR,
        oracle_text_lower_id: NONE_STR,
        card_layout_id: NONE_STR,
        mana_cost_text_id: NONE_STR,
        type_line_id: NONE_STR,
        cmc: None,
        creature_power: None,
        creature_toughness: None,
        planeswalker_loyalty: None,
        edhrec_rank: None,
        cubecobra_score: None,
        card_subtypes: subtypes.iter().map(|s| vocab.intern(s.to_string()).unwrap()).collect(),
        card_keywords: Vec::new(),
        card_oracle_tags: Vec::new(),
        card_legalities: 0,
        mana_cost: ManaCost { pips: HashMap::new(), devotion: None, cmc: 0.0 },
        creature_power_text_id: NONE_STR,
        creature_toughness_text_id: NONE_STR,
    }
}

/// Minimal printing.
fn stub_printing(scryfall_id: u128, illustration_id: u128, prefer_score: Option<f32>) -> Printing {
    Printing {
        scryfall_id,
        illustration_id,
        flavor_text_id: NONE_STR,
        flavor_text_lower_id: NONE_STR,
        card_artist_vid: ARTIST_NONE,
        card_set_code: InlineStr::from_str(""),
        card_border_id: NONE_STR,
        card_watermark_id: NONE_STR,
        collector_number_id: NONE_STR,
        set_name_id: NONE_STR,
        image_placeholder_id: NONE_STR,
        released_at_int: None,
        card_rarity_int: None,
        collector_number_int: None,
        price_usd: None,
        price_eur: None,
        price_tix: None,
        prefer_score,
        card_legalities: 0,
        card_art_tags: Vec::new(),
        card_is_tags: Vec::new(),
        card_frame_data: Vec::new(),
    }
}

/// Assemble a CardData where card i owns `printing_counts[i]` printings, in the
/// store's within-bucket invariant order: descending default prefer_score (the
/// first printing of each range is the default-preferred one). Printings get
/// sequential scryfall/illustration ids starting at 1, and released_at values
/// that make the LAST printing of each range the oldest.
fn store_of(cards: Vec<OracleCard>, printing_counts: &[usize], vocab: VocabInterner) -> CardData {
    assert_eq!(cards.len(), printing_counts.len());
    let mut printings = Vec::new();
    let mut offsets = vec![0u32];
    let mut next_id = 1u128;
    for &n in printing_counts {
        for k in 0..n {
            let mut p = stub_printing(next_id, next_id, Some((n - k) as f32));
            p.released_at_int = Some(20200101 - (k as u32) * 10_000);
            printings.push(p);
            next_id += 1;
        }
        offsets.push(printings.len() as u32);
    }
    // Real type index so TypeCmp narrowing sees the cards (an empty Default
    // index would narrow every type query to zero candidates).
    let indexes = CardIndexes { type_bits: build_type_index(&cards), ..Default::default() };
    CardData {
        cards,
        printings,
        offsets,
        strings: vec![],
        coll_vocab_sorted: sorted_vocab_ids(&vocab.strings),
        coll_vocab: vocab.strings,
        artist_vocab: vec![],
        indexes,
        format_shifts: HashMap::new(),
    }
}

// Verify that narrow_candidates returns printing-space candidates for art tags
// and card-space candidates for keywords, and None (no narrowing) for absent tags.
#[test]
fn narrow_candidates_spaces() {
    let mut art_tags: TagIndex = HashMap::new();
    art_tags.insert("wolf".to_string(), vec![0, 2]);
    let mut keywords: TagIndex = HashMap::new();
    keywords.insert("Flying".to_string(), vec![1]);

    let indexes = CardIndexes { art_tags, keywords, ..Default::default() };
    let bytes = rkyv::to_bytes::<Error>(&indexes).expect("serialize");
    let archived = rkyv::access::<Archived<CardIndexes>, Error>(&bytes).expect("access");
    // offsets for 2 cards with 2 printings each: printings 0-1 → card 0, 2-3 → card 1
    let offsets_bytes = rkyv::to_bytes::<Error>(&vec![0u32, 2, 4]).expect("serialize offsets");
    let offsets = rkyv::access::<Archived<Vec<u32>>, Error>(&offsets_bytes).expect("access offsets");

    let coll = |field, value: &str| FilterExpr::CollectionCmp {
        field,
        op: CmpOp::Ge,
        value: value.to_string(),
        value_id: None,
    };

    match narrow_candidates(&coll(CollField::ArtTags, "wolf"), archived, offsets) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0, 2]),
        _ => panic!("art tag must narrow in printing space"),
    }
    match narrow_candidates(&coll(CollField::Keywords, "Flying"), archived, offsets) {
        Some(Candidates::Cards(v)) => assert_eq!(v, vec![1]),
        _ => panic!("keyword must narrow in card space"),
    }
    // A tag not in the index cannot narrow; the eval step handles correctness.
    assert!(narrow_candidates(&coll(CollField::ArtTags, "zombie"), archived, offsets).is_none());

    // And of mixed spaces projects the printing product up and intersects in
    // card space: art printings {0,2} → cards {0,1}, ∩ keyword cards {1} = {1}.
    let and = FilterExpr::And(vec![coll(CollField::ArtTags, "wolf"), coll(CollField::Keywords, "Flying")]);
    match narrow_candidates(&and, archived, offsets) {
        Some(Candidates::Cards(v)) => assert_eq!(v, vec![1]),
        _ => panic!("mixed And must produce card-space candidates"),
    }
}

#[test]
fn cards_of_printings_maps_and_dedups() {
    let offsets_bytes = rkyv::to_bytes::<Error>(&vec![0u32, 3, 4, 7]).expect("serialize");
    let offsets = rkyv::access::<Archived<Vec<u32>>, Error>(&offsets_bytes).expect("access");
    // printings 0-2 → card 0, 3 → card 1, 4-6 → card 2
    assert_eq!(cards_of_printings(offsets, &[0, 1, 2, 3, 5, 6]), vec![0, 1, 2]);
    assert_eq!(cards_of_printings(offsets, &[1]), vec![0]);
    assert_eq!(cards_of_printings(offsets, &[]), Vec::<u32>::new());
}

#[test]
fn count_common_types_counts_every_card_once() {
    // card 0: Legendary Planeswalker, subtype "Jace"
    // card 1: Instant, no subtypes
    // card 2: Artifact + Creature, subtype "Merfolk"
    // card 3: Creature, subtypes ["Warrior", "Merfolk"]
    let mut vocab = VocabInterner::new();
    let cards = vec![
        stub_card(1, TYPE_LEGENDARY | TYPE_PLANESWALKER, &["Jace"], &mut vocab),
        stub_card(2, TYPE_INSTANT,                        &[], &mut vocab),
        stub_card(3, TYPE_ARTIFACT | TYPE_CREATURE,       &["Merfolk"], &mut vocab),
        stub_card(4, TYPE_CREATURE,                       &["Warrior", "Merfolk"], &mut vocab),
    ];
    // Multiple printings per card must not inflate the counts.
    let data = store_of(cards, &[3, 1, 2, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let counts = count_common_types(archived);

    assert_eq!(counts.get("Legendary"),    Some(&1));
    assert_eq!(counts.get("Planeswalker"), Some(&1));
    assert_eq!(counts.get("Artifact"),     Some(&1));
    assert_eq!(counts.get("Creature"),     Some(&2)); // cards 2 and 3
    assert_eq!(counts.get("Instant"),      Some(&1));
    assert_eq!(counts.get("Merfolk"),  Some(&2));
    assert_eq!(counts.get("Warrior"),  Some(&1));
    assert_eq!(counts.get("Jace"),     Some(&1));
    assert_eq!(counts.get("Land"),   None);
    assert_eq!(counts.get("Sorcery"), None);
}

#[test]
fn count_common_keywords_counts_every_card_once() {
    let mut vocab = VocabInterner::new();
    let mut card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card0.card_keywords = vocab_ids(&mut vocab, &["Flying", "Haste"]);
    let mut card1 = stub_card(2, TYPE_INSTANT, &[], &mut vocab);
    card1.card_keywords = vocab_ids(&mut vocab, &["Trample"]);
    let mut card2 = stub_card(3, TYPE_CREATURE, &[], &mut vocab);
    card2.card_keywords = vocab_ids(&mut vocab, &["Flying"]);

    let data = store_of(vec![card0, card1, card2], &[2, 1, 4], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let counts = count_common_keywords(archived);
    assert_eq!(counts.get("Flying"),  Some(&2));
    assert_eq!(counts.get("Haste"),   Some(&1));
    assert_eq!(counts.get("Trample"), Some(&1));
}

#[test]
fn collection_cmp_binds_vocab_ids_and_matches() {
    // First-seen intern order ("Trample" before "Flying") differs from the
    // alphabetical order the sorted permutation provides, so this exercises
    // the binary-search resolution rather than a trivial identity mapping.
    let mut vocab = VocabInterner::new();
    let mut card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card0.card_keywords = vocab_ids(&mut vocab, &["Trample", "Flying"]);
    let mut card1 = stub_card(2, TYPE_CREATURE, &[], &mut vocab);
    card1.card_keywords = vocab_ids(&mut vocab, &["Haste"]);
    let card2 = stub_card(3, TYPE_CREATURE, &[], &mut vocab); // no keywords

    let data = store_of(vec![card0, card1, card2], &[1, 1, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    // Keywords are card-level, so the card pass alone must fully decide.
    let run = |value: &str, op: CmpOp| -> Vec<bool> {
        let mut f = FilterExpr::CollectionCmp {
            field: CollField::Keywords,
            op,
            value: value.to_string(),
            value_id: None,
        };
        f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab);
        archived.cards.iter().map(|c| f.eval_card(c, &archived.strings) == Tri::True).collect()
    };

    assert_eq!(run("Flying", CmpOp::Ge),  vec![true, false, false]);
    assert_eq!(run("Haste", CmpOp::Ge),   vec![false, true, false]);
    assert_eq!(run("Haste", CmpOp::Eq),   vec![false, true, false]); // exactly {Haste}
    assert_eq!(run("Flying", CmpOp::Eq),  vec![false, false, false]); // card0 has two keywords
    assert_eq!(run("Flying", CmpOp::Ne),  vec![true, true, true]);
    // A value absent from the vocab matches no element: Ge nothing, Le only empty.
    assert_eq!(run("Deathtouch", CmpOp::Ge), vec![false, false, false]);
    assert_eq!(run("Deathtouch", CmpOp::Le), vec![false, false, true]);
}

#[test]
fn printing_level_predicates_are_printing_dep_in_card_pass() {
    let mut vocab = VocabInterner::new();
    let card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    let wolf_ids = vocab_ids(&mut vocab, &["wolf"]);
    let mut data = store_of(vec![card0], &[2], vocab);
    data.printings[1].card_art_tags = wolf_ids;
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut f = FilterExpr::CollectionCmp {
        field: CollField::ArtTags,
        op: CmpOp::Ge,
        value: "wolf".to_string(),
        value_id: None,
    };
    f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab);

    let card = &archived.cards[0];
    // Card pass can't decide an art-tag predicate...
    assert!(f.eval_card(card, &archived.strings) == Tri::PrintingDep);
    // ...but per-printing evaluation is exact.
    assert!(!f.matches(card, &archived.printings[0], &archived.strings));
    assert!(f.matches(card, &archived.printings[1], &archived.strings));

    // Negation keeps printing-dependence (NOT PrintingDep = PrintingDep).
    let g = FilterExpr::Not(Box::new(f));
    assert!(g.eval_card(card, &archived.strings) == Tri::PrintingDep);
    assert!(g.matches(card, &archived.printings[0], &archived.strings));
    assert!(!g.matches(card, &archived.printings[1], &archived.strings));
}

#[test]
fn divergent_legality_defers_to_printings() {
    let mut vocab = VocabInterner::new();
    let mut card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card0.card_legalities = 0b01; // legal at shift 0
    let mut card1 = stub_card(2, TYPE_CREATURE, &[], &mut vocab);
    card1.card_legalities = 0b01;
    card1.legality_divergent = true;
    let mut data = store_of(vec![card0, card1], &[1, 2], vocab);
    data.printings[1].card_legalities = 0b01; // tournament printing: legal
    data.printings[2].card_legalities = 0b00; // 30A-style printing: not legal
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let f = FilterExpr::Legality { shift: Some(0), expected: 0b01 };

    // Non-divergent card: exact at card level.
    assert!(f.eval_card(&archived.cards[0], &archived.strings) == Tri::True);
    // Divergent card: card pass defers, printings decide individually.
    assert!(f.eval_card(&archived.cards[1], &archived.strings) == Tri::PrintingDep);
    assert!(f.matches(&archived.cards[1], &archived.printings[1], &archived.strings));
    assert!(!f.matches(&archived.cards[1], &archived.printings[2], &archived.strings));
}

#[test]
fn run_query_walk_dedups_and_prefers() {
    // card 0: Creature with 3 printings, card 1: Instant with 1, card 2: Creature with 2.
    // store_of orders each bucket by descending prefer score, so the first
    // printing of each range is the default-preferred one and the last is the
    // oldest by released_at.
    let mut vocab = VocabInterner::new();
    let cards = vec![
        stub_card(1, TYPE_CREATURE, &[], &mut vocab),
        stub_card(2, TYPE_INSTANT, &[], &mut vocab),
        stub_card(3, TYPE_CREATURE, &[], &mut vocab),
    ];
    let data = store_of(cards, &[3, 1, 2], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let creatures = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let run = |unique: &str, prefer: &str| {
        run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &creatures, unique, prefer, "edhrec", "asc", 100, 0, &archived.indexes,
        )
    };

    // unique=card, default prefer: one result per matching card; the walk's
    // early exit takes the first printing of each range (ids 1 and 5).
    let (total, page) = run("card", "default");
    assert_eq!(total, 2);
    let chosen: Vec<u128> = page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect();
    assert!(chosen.contains(&1) && chosen.contains(&5));

    // unique=printing: every matching printing (3 + 2 creatures).
    let (total, _) = run("printing", "default");
    assert_eq!(total, 5);

    // unique=artwork: every printing has a distinct illustration here, so all 5 groups.
    let (total, _) = run("artwork", "default");
    assert_eq!(total, 5);

    // prefer=oldest scans each range and picks the smallest released_at —
    // the LAST printing of each range in store_of's construction (ids 3 and 6).
    let (total, page) = run("card", "oldest");
    assert_eq!(total, 2);
    let chosen: Vec<u128> = page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect();
    assert!(chosen.contains(&3) && chosen.contains(&6));
}

#[test]
fn run_query_artwork_groups_shared_illustrations() {
    // One card, 4 printings; printings 0 and 2 share an illustration.
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[4], vocab);
    // ids from store_of: scryfall/illustration 1,2,3,4. Make printing 2 share
    // printing 0's illustration (non-contiguous under prefer-desc order).
    data.printings[2].illustration_id = 1;
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let all = FilterExpr::True;
    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &all, "artwork", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 3); // illustrations {1, 2, 4}
    // Group {printings 0, 2}: printing 0 has the higher prefer score (desc order)
    // and must be the group's representative.
    let chosen: Vec<u128> = page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect();
    assert!(chosen.contains(&1) && !chosen.contains(&3));
}

/// Measures checked rkyv::access vs access_unchecked on a production-scale
/// archive. This is the evidence behind the access_unchecked safety comments
/// on query()/size(): checked access re-validates the entire archive graph
/// on every call, which is milliseconds per call at ~100k printings — orders
/// of magnitude over total query time. Run with:
///   cargo test --release -- --ignored bench_checked_vs_unchecked --nocapture
#[test]
#[ignore]
fn bench_checked_vs_unchecked_access() {
    const N_CARDS: usize = 31_500;
    const PRINTINGS_PER_CARD: usize = 3;
    let words = ["draw", "card", "creature", "destroy", "target", "flying", "counter", "spell", "token", "exile"];
    let mut interner = Interner::new();
    let mut vocab = VocabInterner::new();
    let mut cards: Vec<OracleCard> = Vec::with_capacity(N_CARDS);
    let mut printings: Vec<Printing> = Vec::with_capacity(N_CARDS * PRINTINGS_PER_CARD);
    let mut offsets: Vec<u32> = Vec::with_capacity(N_CARDS + 1);
    for i in 0..N_CARDS {
        let name = format!("Benchmark Card Number {i}");
        let oracle = format!(
            "{}: {} a {} {}, then {} {} cards. This text is representative filler standing in for \
             real oracle text so string validation cost is realistic for card group {i}.",
            words[i % 10], words[(i + 1) % 10], words[(i + 2) % 10],
            words[(i + 3) % 10], words[(i + 4) % 10], words[(i + 5) % 10],
        );
        let mut card = stub_card((i + 1) as u128, TYPE_CREATURE, &["Benchmark", words[i % 10]], &mut vocab);
        card.card_name_lower = InlineStr::from_str(&name.to_lowercase());
        card.card_name_id = interner.intern(name.clone());
        card.oracle_text_id = interner.intern(oracle.clone());
        card.oracle_text_lower_id = interner.intern(oracle.to_lowercase());
        card.card_keywords = vocab_ids(&mut vocab, &[words[i % 10]]);
        card.cmc = Some((i % 8) as u8);
        offsets.push(printings.len() as u32);
        for k in 0..PRINTINGS_PER_CARD {
            let flavor = format!("Flavor text for printing {i}-{k}, roughly the length of a real flavor quote.");
            let pid = (i * PRINTINGS_PER_CARD + k + 1) as u128;
            let mut p = stub_printing(pid, pid, Some((PRINTINGS_PER_CARD - k) as f32));
            p.flavor_text_id = interner.intern(flavor.clone());
            p.flavor_text_lower_id = interner.intern(flavor.to_lowercase());
            p.set_name_id = interner.intern(format!("Benchmark Set {}", i % 300));
            printings.push(p);
        }
        cards.push(card);
    }
    offsets.push(printings.len() as u32);
    let strings = interner.strings;

    let indexes = CardIndexes {
        name_trigram:   build_trigram_index(&cards, |c| c.card_name_lower.as_str()),
        oracle_trigram: build_oracle_text_index(&cards, &strings),
        cmc:            build_numeric_index(&cards, |c| c.cmc.map(|v| v as i16)),
        power:          build_numeric_index(&cards, |c| c.creature_power.map(|v| v as i16)),
        toughness:      build_numeric_index(&cards, |c| c.creature_toughness.map(|v| v as i16)),
        type_bits:      build_type_index(&cards),
        subtypes:       build_tag_index(&cards, &vocab.strings, |c| &c.card_subtypes),
        keywords:       build_tag_index(&cards, &vocab.strings, |c| &c.card_keywords),
        oracle_tags:    build_tag_index(&cards, &vocab.strings, |c| &c.card_oracle_tags),
        art_tags:       build_tag_index(&printings, &vocab.strings, |p| &p.card_art_tags),
        is_tags:        build_tag_index(&printings, &vocab.strings, |p| &p.card_is_tags),
        artists:        ArtistIndex::default(),
        set_codes:      HashMap::new(),
        released_at:    Vec::new(),
        price_usd:      Vec::new(),
    };
    let data = CardData {
        cards,
        printings,
        offsets,
        strings,
        coll_vocab_sorted: sorted_vocab_ids(&vocab.strings),
        coll_vocab: vocab.strings,
        artist_vocab: vec![],
        indexes,
        format_shifts: HashMap::new(),
    };
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    println!("archive size: {:.1} MB", bytes.len() as f64 / 1e6);

    const ITERS: u32 = 10;
    let t = std::time::Instant::now();
    for _ in 0..ITERS {
        let a = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("checked access");
        assert_eq!(a.printings.len(), N_CARDS * PRINTINGS_PER_CARD);
    }
    let checked = t.elapsed() / ITERS;

    let t = std::time::Instant::now();
    for _ in 0..ITERS {
        let a = unsafe { rkyv::access_unchecked::<Archived<CardData>>(&bytes) };
        assert_eq!(a.printings.len(), N_CARDS * PRINTINGS_PER_CARD);
    }
    let unchecked = t.elapsed() / ITERS;

    println!("checked rkyv::access:   {checked:?} per call");
    println!("access_unchecked:       {unchecked:?} per call");
}

#[test]
fn card_pass_extracts_residual_and_matches() {
    // card 0 is a Creature whose second printing is art:wolf; card 1 is an Instant.
    let mut vocab = VocabInterner::new();
    let cards = vec![
        stub_card(1, TYPE_CREATURE, &[], &mut vocab),
        stub_card(2, TYPE_INSTANT, &[], &mut vocab),
    ];
    let wolf_ids = vocab_ids(&mut vocab, &["wolf"]);
    let mut data = store_of(cards, &[2, 1], vocab);
    data.printings[1].card_art_tags = wolf_ids;
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut wolf = FilterExpr::CollectionCmp {
        field: CollField::ArtTags,
        op: CmpOp::Ge,
        value: "wolf".to_string(),
        value_id: None,
    };
    wolf.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab);
    let creature = || FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };

    // And[t:creature, art:wolf]: the type check is proven at card level and
    // hoisted out; only the art check stays in the residual.
    let and = FilterExpr::And(vec![creature(), wolf]);
    let mut residual: Vec<&FilterExpr> = Vec::new();
    let mut is_or = false;

    // Creature card: PrintingDep with a one-element residual (the art term).
    let t = and.card_pass(&archived.cards[0], &archived.strings, &mut residual, &mut is_or);
    assert!(t == Tri::PrintingDep && residual.len() == 1 && !is_or);
    assert!(!FilterExpr::residual_matches(&archived.cards[0], &archived.printings[0], &archived.strings, &residual, is_or));
    assert!(FilterExpr::residual_matches(&archived.cards[0], &archived.printings[1], &archived.strings, &residual, is_or));

    // Instant card: the type child is False at card level — whole And settles
    // to False without touching printings.
    let t = and.card_pass(&archived.cards[1], &archived.strings, &mut residual, &mut is_or);
    assert!(t == Tri::False);

    // Or[t:creature, art:wolf]: True for the creature card at card level (no
    // residual needed); PrintingDep with an Or-residual for the instant.
    let mut wolf2 = FilterExpr::CollectionCmp {
        field: CollField::ArtTags,
        op: CmpOp::Ge,
        value: "wolf".to_string(),
        value_id: None,
    };
    wolf2.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab);
    let or = FilterExpr::Or(vec![creature(), wolf2]);
    let t = or.card_pass(&archived.cards[0], &archived.strings, &mut residual, &mut is_or);
    assert!(t == Tri::True && residual.is_empty());
    let t = or.card_pass(&archived.cards[1], &archived.strings, &mut residual, &mut is_or);
    assert!(t == Tri::PrintingDep && residual.len() == 1 && is_or);
    assert!(!FilterExpr::residual_matches(&archived.cards[1], &archived.printings[2], &archived.strings, &residual, is_or));
}

#[test]
fn artist_predicates_bind_to_vocab_ids_and_narrow() {
    // Two artists; printings 0,2 by "rebecca guay", printing 1 by "john avon",
    // printing 3 has no artist.
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab), stub_card(2, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[2, 2], vocab);
    let mut artists = VocabInterner::new();
    let rebecca = artists.intern("rebecca guay".to_string()).unwrap();
    let avon = artists.intern("john avon".to_string()).unwrap();
    data.printings[0].card_artist_vid = rebecca;
    data.printings[1].card_artist_vid = avon;
    data.printings[2].card_artist_vid = rebecca;
    data.artist_vocab = artists.strings;
    data.indexes.artists = build_artist_index(&data.printings, data.artist_vocab.len());
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut f = FilterExpr::TextContains {
        field: super::TextSearchField::ArtistLower,
        word: "rebecca".to_string(),
    };
    f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab);
    // bind rewrites the contains into an id-set match
    let FilterExpr::ArtistMatch { ref ids } = f else { panic!("expected ArtistMatch after bind") };
    assert_eq!(ids, &vec![rebecca]);

    // per-printing evaluation: integer membership; missing artist is Null (no match)
    let card = &archived.cards[0];
    assert!(f.matches(card, &archived.printings[0], &archived.strings));
    assert!(!f.matches(card, &archived.printings[1], &archived.strings));
    assert!(!f.matches(card, &archived.printings[3], &archived.strings));
    assert!(f.eval_card(card, &archived.strings) == Tri::PrintingDep);

    // narrowing expands the CSR rows to sorted printing ids
    match narrow_candidates(&f, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0, 2]),
        _ => panic!("artist predicate must narrow in printing space"),
    }

    // an artist matching nothing narrows to the exact empty set
    let mut g = FilterExpr::TextContains {
        field: super::TextSearchField::ArtistLower,
        word: "zzz".to_string(),
    };
    g.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab);
    match narrow_candidates(&g, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => assert!(v.is_empty()),
        _ => panic!("empty artist match must narrow to the empty set"),
    }
}

#[test]
fn set_code_and_date_narrowing() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab), stub_card(2, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[2, 1], vocab);
    data.printings[0].card_set_code = InlineStr::from_str("lea");
    data.printings[1].card_set_code = InlineStr::from_str("fdn");
    data.printings[2].card_set_code = InlineStr::from_str("lea");
    data.printings[0].released_at_int = Some(19930805);
    data.printings[1].released_at_int = Some(20241115);
    data.printings[2].released_at_int = None; // dateless printings never satisfy date filters
    // set_codes / released_at built the way reload_commit builds them
    let mut set_codes: TagIndex = HashMap::new();
    for (i, p) in data.printings.iter().enumerate() {
        set_codes.entry(p.card_set_code.as_str().to_string()).or_default().push(i as u32);
    }
    data.indexes.set_codes = set_codes;
    data.indexes.released_at = build_range_index(&data.printings, |p| p.released_at_int);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let lea = FilterExpr::TextExact {
        field: super::TextField::SetCode,
        op: CmpOp::Eq,
        value: "lea".to_string(),
    };
    match narrow_candidates(&lea, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0, 2]),
        _ => panic!("set code must narrow in printing space"),
    }
    // Unknown set code: exact empty narrowing (the index covers every code).
    let none = FilterExpr::TextExact {
        field: super::TextField::SetCode,
        op: CmpOp::Eq,
        value: "zzz".to_string(),
    };
    match narrow_candidates(&none, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => assert!(v.is_empty()),
        _ => panic!("unknown set code must narrow to the empty set"),
    }

    let year1993 = FilterExpr::YearCmp { op: CmpOp::Eq, year: 1993 };
    match narrow_candidates(&year1993, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0]),
        _ => panic!("year must narrow in printing space"),
    }
    let date_ge = FilterExpr::DateCmp { op: CmpOp::Ge, value: 20240101 };
    match narrow_candidates(&date_ge, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![1]),
        _ => panic!("date must narrow in printing space"),
    }
    // Ne is not selective and must not narrow.
    assert!(narrow_candidates(&FilterExpr::DateCmp { op: CmpOp::Ne, value: 19930805 }, &archived.indexes, &archived.offsets).is_none());
}

#[test]
fn price_narrowing_is_a_superset_under_f32_rounding() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[4], vocab);
    data.printings[0].price_usd = Some(0.05);
    data.printings[1].price_usd = Some(0.1); // sits at the query boundary
    data.printings[2].price_usd = Some(60.0);
    data.printings[3].price_usd = None; // priceless printings never satisfy price filters
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd.map(super::f32_sort_bits));
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let cheap = FilterExpr::NumericCmp {
        lhs: super::NumExpr::Field(super::NumField::PriceUsd),
        op: CmpOp::Lt,
        rhs: super::NumExpr::Const(0.10),
    };
    // 0.10f64 is not representable as f32; the widened bound may include the
    // boundary printing as a candidate, but must never lose printing 0.
    match narrow_candidates(&cheap, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => {
            assert!(v.contains(&0));
            assert!(!v.contains(&2) && !v.contains(&3));
        }
        _ => panic!("usd must narrow in printing space"),
    }
    // ...and the walk's exact evaluation rejects the boundary printing.
    let card = &archived.cards[0];
    assert!(cheap.matches(card, &archived.printings[0], &archived.strings));
    assert!(!cheap.matches(card, &archived.printings[1], &archived.strings));

    let pricey = FilterExpr::NumericCmp {
        lhs: super::NumExpr::Const(50.0),
        op: CmpOp::Lt,
        rhs: super::NumExpr::Field(super::NumField::PriceUsd),
    };
    match narrow_candidates(&pricey, &archived.indexes, &archived.offsets) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![2]),
        _ => panic!("flipped usd comparison must narrow"),
    }
    // Ne is not selective.
    let ne = FilterExpr::NumericCmp {
        lhs: super::NumExpr::Field(super::NumField::PriceUsd),
        op: CmpOp::Ne,
        rhs: super::NumExpr::Const(1.0),
    };
    assert!(narrow_candidates(&ne, &archived.indexes, &archived.offsets).is_none());
}
