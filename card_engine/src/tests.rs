use super::{
    assign_name_ranks,
    build_numeric_index, build_oracle_text_index, build_tag_index, build_trigram_index,
    build_rarity_index, build_flavor_index, build_thresholded_tag_index, build_sort_permutations,
    build_artwork_group_counts, build_bit_planes, build_divergent_ids, build_name_bigram_index, flavor_fingerprint, flavor_match_sets,
    cards_of_printings, count_common_keywords, count_common_types,
    build_artist_index, build_range_index, range_candidates, narrow_candidates, rarity_candidates,
    range_too_broad_to_narrow, run_query, trigram_candidates, finalize_trigram_index, PrintingRangeIndex, NARROW_FLOOR,
    bitmap_contains, bitmap_card_ids, compile_plane, eval_planes, split_planes,
    ArtistIndex, CardData, CardIndexes, Candidates, ColorField, NumExpr, NumField, RarityIndex,
    CollField, CmpOp, FilterExpr, InlineStr, Interner, ManaCost, OracleCard, Printing, TagIndex,
    TextField, TextSearchField, Tri, SortedTrigramIndex, VocabInterner, ARTIST_NONE, NONE_STR, TYPE_ARTIFACT, TYPE_CREATURE,
    TYPE_ENCHANTMENT, TYPE_INSTANT, TYPE_LAND, TYPE_LEGENDARY, TYPE_PLANESWALKER, TYPE_SNOW, TYPE_SORCERY,
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

/// Build a SortedTrigramIndex mapping each word's trigrams to the given card
/// ids. Domain is a large fixed constant so tiny test posting lists never
/// cross into the dense tier by accident — tests that specifically want the
/// dense/plane path use `index_of_domain` with a small domain instead.
fn index_of(words: &[(&str, &[u32])]) -> SortedTrigramIndex {
    index_of_domain(words, 100_000)
}

fn index_of_domain(words: &[(&str, &[u32])], domain: usize) -> SortedTrigramIndex {
    let mut idx: HashMap<[u8; 3], Vec<u32>> = HashMap::new();
    for (word, cards) in words {
        for w in word.as_bytes().windows(3) {
            let entry = idx.entry([w[0], w[1], w[2]]).or_default();
            for &c in *cards {
                if !entry.contains(&c) { entry.push(c); }
            }
            entry.sort_unstable();
        }
    }
    finalize_trigram_index(idx, domain)
}

/// Archive the index and query it, matching how the engine reads the shared snapshot.
fn candidates(idx: &SortedTrigramIndex, word: &str) -> Option<Vec<u32>> {
    let bytes = rkyv::to_bytes::<Error>(idx).expect("serialize trigram index");
    let archived = rkyv::access::<Archived<SortedTrigramIndex>, Error>(&bytes).expect("access trigram index");
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
        name_rank: 0,
        card_subtypes: subtypes.iter().map(|s| vocab.intern(s.to_string()).unwrap()).collect(),
        card_keywords: Vec::new(),
        card_oracle_tags: Vec::new(),
        card_legalities: 0,
        mana_cost: ManaCost { core: 0, hybrids: Vec::new(), devotion: 0, cmc: 0.0 },
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
    // Real planes and bigrams so narrowing tests see the same store shape
    // reload_commit builds (type narrowing goes through the planes since #637).
    let indexes = CardIndexes {
        planes: build_bit_planes(&cards),
        name_bigrams: build_name_bigram_index(&cards),
        legal_divergent: build_divergent_ids(&cards),
        sort_perms: build_sort_permutations(&cards, &printings, &offsets),
        ..Default::default()
    };
    CardData {
        cards,
        printings,
        offsets,
        strings: vec![],
        coll_vocab_sorted: sorted_vocab_ids(&vocab.strings),
        coll_vocab: vocab.strings,
        artist_vocab: vec![],
        mana_vocab: vec![],
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

    match narrow_candidates(&coll(CollField::ArtTags, "wolf"), archived, offsets, &[]) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0, 2]),
        _ => panic!("art tag must narrow in printing space"),
    }
    match narrow_candidates(&coll(CollField::Keywords, "Flying"), archived, offsets, &[]) {
        Some(Candidates::Cards(v)) => assert_eq!(v, vec![1]),
        _ => panic!("keyword must narrow in card space"),
    }
    // A tag with no postings in a complete index proves the empty set (an
    // unbound value_id matches nothing at eval either).
    match narrow_candidates(&coll(CollField::ArtTags, "zombie"), archived, offsets, &[]) {
        Some(Candidates::Printings(v)) => assert!(v.is_empty(), "absent tag narrows to the exact empty set"),
        _ => panic!("absent tag must narrow to empty, not decline"),
    }
    // frame_data is selectivity-thresholded (#628): dense values are absent by
    // design, so absence proves nothing and it must keep declining.
    assert!(narrow_candidates(&coll(CollField::FrameData, "zombie"), archived, offsets, &[]).is_none());

    // And of mixed spaces projects the printing product up and intersects in
    // card space: art printings {0,2} → cards {0,1}, ∩ keyword cards {1} = {1}.
    let and = FilterExpr::And(vec![coll(CollField::ArtTags, "wolf"), coll(CollField::Keywords, "Flying")]);
    match narrow_candidates(&and, archived, offsets, &[]) {
        Some(Candidates::Cards(v)) => assert_eq!(v, vec![1]),
        _ => panic!("mixed And must produce card-space candidates"),
    }
}

// build_rarity_index posts each card once per distinct printing rarity;
// rarity-less printings contribute nothing.
#[test]
fn rarity_index_posts_cards_per_distinct_rarity() {
    // card 0: printings at common(0) and mythic(3); card 1: rare(2) twice
    // (deduped by the mask); card 2: no rarity anywhere.
    let mut printings: Vec<Printing> = (1..=5).map(|i| stub_printing(i, i, None)).collect();
    printings[0].card_rarity_int = Some(0);
    printings[1].card_rarity_int = Some(3);
    printings[2].card_rarity_int = Some(2);
    printings[3].card_rarity_int = Some(2);
    let offsets = vec![0u32, 2, 4, 5];

    let idx = build_rarity_index(&printings, &offsets);
    assert_eq!(idx[0], vec![0]); // common: card 0
    assert_eq!(idx[2], vec![1]); // rare: card 1, once despite two rare printings
    assert_eq!(idx[3], vec![0]); // mythic: card 0
    assert!(idx[1].is_empty() && idx[4].is_empty() && idx[5].is_empty());
}

// rarity_candidates unions the qualifying buckets, refuses Ne and unions past
// MAX_UNION_FRACTION of posting entries, and proves empty sets for impossible
// comparisons.
#[test]
fn rarity_candidates_ops() {
    // common {0,3}, uncommon {1}, rare {2,3}, mythic {4}, special {}, bonus {}
    let idx: RarityIndex = [vec![0, 3], vec![1], vec![2, 3], vec![4], vec![], vec![]];
    let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize");
    let archived = rkyv::access::<Archived<RarityIndex>, Error>(&bytes).expect("access");

    // Eq picks one bucket; a card in several buckets appears via each.
    assert_eq!(rarity_candidates(archived, CmpOp::Eq, 2.0), Some(vec![2, 3]));
    // Ge unions rare and above; card 3 (common+rare) appears once.
    assert_eq!(rarity_candidates(archived, CmpOp::Ge, 2.0), Some(vec![2, 3, 4]));
    // Lt 2 = common|uncommon.
    assert_eq!(rarity_candidates(archived, CmpOp::Lt, 2.0), Some(vec![0, 1, 3]));
    // Impossible comparisons prove the empty set (exact, not "no narrowing").
    assert_eq!(rarity_candidates(archived, CmpOp::Eq, 2.5), Some(vec![]));
    assert_eq!(rarity_candidates(archived, CmpOp::Gt, 5.0), Some(vec![]));
    // Ne and over-ceiling unions decline to narrow.
    assert!(rarity_candidates(archived, CmpOp::Ne, 2.0).is_none());
    assert!(rarity_candidates(archived, CmpOp::Ge, 0.0).is_none());
    assert!(rarity_candidates(archived, CmpOp::Le, 5.0).is_none());
    // The ceiling is entries-based, not bucket-count: Le 3 selects only 4 of
    // 6 buckets but 100% of posting entries (special/bonus are empty), which
    // exceeds MAX_UNION_FRACTION and must decline.
    assert!(rarity_candidates(archived, CmpOp::Le, 3.0).is_none());
}

// The NumericCmp narrowing arm routes rarity through the index in card space,
// for both operand orders.
#[test]
fn narrow_candidates_rarity_card_space() {
    // All three cards also at common, so the rare/mythic unions stay under
    // MAX_UNION_FRACTION of the six entries.
    let rarity: RarityIndex = [vec![0, 1, 2], vec![], vec![0, 2], vec![1], vec![], vec![]];
    let indexes = CardIndexes { rarity, ..Default::default() };
    let bytes = rkyv::to_bytes::<Error>(&indexes).expect("serialize");
    let archived = rkyv::access::<Archived<CardIndexes>, Error>(&bytes).expect("access");
    let offsets_bytes = rkyv::to_bytes::<Error>(&vec![0u32, 2, 4, 6]).expect("serialize offsets");
    let offsets = rkyv::access::<Archived<Vec<u32>>, Error>(&offsets_bytes).expect("access offsets");

    let cmp = FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::RarityInt),
        op: CmpOp::Ge,
        rhs: NumExpr::Const(2.0),
    };
    match narrow_candidates(&cmp, archived, offsets, &[]) {
        Some(Candidates::Cards(v)) => assert_eq!(v, vec![0, 1, 2]),
        _ => panic!("rarity must narrow in card space"),
    }

    // Flipped operand order: 3 <= rarity ≡ rarity >= 3.
    let flipped = FilterExpr::NumericCmp {
        lhs: NumExpr::Const(3.0),
        op: CmpOp::Le,
        rhs: NumExpr::Field(NumField::RarityInt),
    };
    match narrow_candidates(&flipped, archived, offsets, &[]) {
        Some(Candidates::Cards(v)) => assert_eq!(v, vec![1]),
        _ => panic!("flipped rarity must narrow in card space"),
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
        f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
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
    f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);

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

// ─── Legality bitplanes (#630 phase 2) ────────────────────────────────────────

/// card0: legal in format A (shift 0) only. card1: legal in format B (shift 2)
/// only. card2: divergent, canonical (preferred-printing) word says legal in
/// neither — the OR-with-divergent-postings narrowing must still include it.
fn legal_plane_fixture() -> (Vec<OracleCard>, VocabInterner) {
    let mut vocab = VocabInterner::new();
    let mut card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card0.card_legalities = 0b01; // legal at shift 0
    let mut card1 = stub_card(2, TYPE_CREATURE, &[], &mut vocab);
    card1.card_legalities = 0b01 << 2; // legal at shift 2
    let mut card2 = stub_card(3, TYPE_CREATURE, &[], &mut vocab);
    card2.card_legalities = 0; // not legal in either, canonically
    card2.legality_divergent = true;
    (vec![card0, card1, card2], vocab)
}

#[test]
fn legal_plane_narrows_positive_includes_divergent() {
    let (cards, vocab) = legal_plane_fixture();
    let data = store_of(cards, &[1, 1, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    // Format A (shift 0): card0 (truly legal) + card2 (divergent, always
    // included) narrow in; card1 (legal only in format B) must not.
    let f = FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    match narrow_candidates(&f, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(bitmap_contains(&bits, 0), "truly legal card must narrow in");
            assert!(!bitmap_contains(&bits, 1), "card legal only in a different format must not narrow in");
            assert!(bitmap_contains(&bits, 2), "divergent card must always narrow in, regardless of canonical status");
        }
        _ => panic!("expected a card bitmap"),
    }

    // Format B (shift 2): card1 narrows in, card0 does not, card2 still does
    // (shift math must independently address each format's plane).
    let g = FilterExpr::Legality { shift: Some(2), expected: 0b01 };
    match narrow_candidates(&g, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(!bitmap_contains(&bits, 0));
            assert!(bitmap_contains(&bits, 1));
            assert!(bitmap_contains(&bits, 2));
        }
        _ => panic!("expected a card bitmap"),
    }
}

#[test]
fn legal_plane_narrows_negated_includes_divergent() {
    let (cards, vocab) = legal_plane_fixture();
    let data = store_of(cards, &[1, 1, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    // -f:A: card0 is truly legal in A, so it must NOT narrow in; card1 (not
    // legal in A) and card2 (divergent) must.
    let not_a = FilterExpr::Not(Box::new(FilterExpr::Legality { shift: Some(0), expected: 0b01 }));
    match narrow_candidates(&not_a, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(!bitmap_contains(&bits, 0), "truly legal card must not narrow into the negation");
            assert!(bitmap_contains(&bits, 1), "truly not-legal card must narrow into the negation");
            assert!(bitmap_contains(&bits, 2), "divergent card must always narrow in, regardless of polarity");
        }
        _ => panic!("expected a card bitmap"),
    }
}

#[test]
fn legal_plane_declines_banned_restricted_and_absent_format() {
    let (cards, vocab) = legal_plane_fixture();
    let data = store_of(cards, &[1, 1, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    // banned:/restricted: never plane-narrow — unindexed and unchanged.
    let banned = FilterExpr::Legality { shift: Some(0), expected: 0b11 };
    assert!(narrow_candidates(&banned, &archived.indexes, &archived.offsets, &archived.cards).is_none());
    let restricted = FilterExpr::Legality { shift: Some(0), expected: 0b10 };
    assert!(narrow_candidates(&restricted, &archived.indexes, &archived.offsets, &archived.cards).is_none());
    // Not(banned) falls through the generic Not path, which also declines
    // (no tight source to complement) — still correct, just unnarrowed.
    let not_banned = FilterExpr::Not(Box::new(banned));
    assert!(narrow_candidates(&not_banned, &archived.indexes, &archived.offsets, &archived.cards).is_none());

    // A format absent from all loaded data (shift: None) matches nothing at
    // eval and isn't plane-narrowed either — falls to the (cheap, correct)
    // full scan.
    let absent = FilterExpr::Legality { shift: None, expected: 0b01 };
    assert!(narrow_candidates(&absent, &archived.indexes, &archived.offsets, &archived.cards).is_none());
}

/// The correctness property the whole design hinges on: a divergent card must
/// never be silently dropped by the narrowing, in either polarity, even when
/// its *preferred* printing's status disagrees with a non-preferred printing
/// that the query should actually match. This exercises the full path
/// (narrow_rec's new arms feeding run_query's unmodified card_pass/residual
/// walk), not just the bitmap in isolation.
#[test]
fn legal_plane_narrowing_preserves_divergent_printing_correctness() {
    let mut vocab = VocabInterner::new();
    // Preferred printing (higher prefer_score, sorts first) says NOT legal;
    // the second, non-preferred printing says legal — the opposite of the
    // "usual" case, deliberately, to stress the narrowing's superset property.
    let mut card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card.card_legalities = 0; // preferred printing's status: not legal
    card.legality_divergent = true;
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0; // preferred: not legal
    data.printings[1].card_legalities = 0b01; // non-preferred: legal
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let run = |filter: &mut FilterExpr, unique: &str| {
        run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            filter, None, unique, "default", "edhrec", "asc", 100, 0, &archived.indexes,
        )
    };

    // f:A, unique=printing: exactly the one legal printing, not the not-legal one.
    let mut f = FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    let (total, page) = run(&mut f, "printing");
    assert_eq!(total, 1);
    assert_eq!(u128::from(page[0].1.scryfall_id), 2); // the non-preferred, legal printing

    // f:A, unique=card: the card matches (at least one printing is legal),
    // even though the narrowing's own candidate bit for this card came only
    // from the divergent postings, not from the (canonically "not legal") legal_x bit.
    let mut f2 = FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    let (total, _) = run(&mut f2, "card");
    assert_eq!(total, 1);

    // -f:A, unique=printing: exactly the one not-legal printing.
    let mut not_f = FilterExpr::Not(Box::new(FilterExpr::Legality { shift: Some(0), expected: 0b01 }));
    let (total, page) = run(&mut not_f, "printing");
    assert_eq!(total, 1);
    assert_eq!(u128::from(page[0].1.scryfall_id), 1); // the preferred, not-legal printing
}

/// Word-boundary check: plane-index and bit-index arithmetic must stay correct
/// past the first 64-card word, for a non-zero format shift.
#[test]
fn legal_plane_narrows_correctly_across_word_boundary() {
    let mut vocab = VocabInterner::new();
    let n = 70;
    let cards: Vec<OracleCard> = (0..n)
        .map(|i| {
            let mut c = stub_card(1 + i as u128, TYPE_CREATURE, &[], &mut vocab);
            // Legal (at shift 2) on even ids only, spanning past the 64-bit word boundary.
            c.card_legalities = if i % 2 == 0 { 0b01 << 2 } else { 0 };
            c
        })
        .collect();
    let printing_counts = vec![1usize; n];
    let data = store_of(cards, &printing_counts, vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let f = FilterExpr::Legality { shift: Some(2), expected: 0b01 };
    match narrow_candidates(&f, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            for i in 0..n {
                assert_eq!(bitmap_contains(&bits, i as u32), i % 2 == 0, "card {i} narrowing mismatch");
            }
        }
        _ => panic!("expected a card bitmap"),
    }
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

    let mut creatures = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let mut run = |unique: &str, prefer: &str| {
        run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut creatures, None, unique, prefer, "edhrec", "asc", 100, 0, &archived.indexes,
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

    let mut all = FilterExpr::True;
    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut all, None, "artwork", "default", "edhrec", "asc", 100, 0, &archived.indexes,
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
        rarity:         build_rarity_index(&printings, &offsets),
        subtypes:       build_tag_index(&cards, &vocab.strings, |c| &c.card_subtypes),
        keywords:       build_tag_index(&cards, &vocab.strings, |c| &c.card_keywords),
        oracle_tags:    build_tag_index(&cards, &vocab.strings, |c| &c.card_oracle_tags),
        art_tags:       build_tag_index(&printings, &vocab.strings, |p| &p.card_art_tags),
        is_tags:        build_tag_index(&printings, &vocab.strings, |p| &p.card_is_tags),
        frame_data:     HashMap::new(),
        artists:        ArtistIndex::default(),
        flavor:         build_flavor_index(&printings, &strings),
        set_codes:      HashMap::new(),
        released_at:    Vec::new(),
        price_usd:      Vec::new(),
        collector_number: Vec::new(),
        sort_perms:     build_sort_permutations(&cards, &printings, &offsets),
        artwork_groups: build_artwork_group_counts(&printings, &offsets),
        planes:         build_bit_planes(&cards),
        name_bigrams:   build_name_bigram_index(&cards),
        legal_divergent: build_divergent_ids(&cards),
    };
    let data = CardData {
        cards,
        printings,
        offsets,
        strings,
        coll_vocab_sorted: sorted_vocab_ids(&vocab.strings),
        coll_vocab: vocab.strings,
        artist_vocab: vec![],
        mana_vocab: vec![],
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
    wolf.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
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
    wolf2.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
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
    f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
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
    match narrow_candidates(&f, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0, 2]),
        _ => panic!("artist predicate must narrow in printing space"),
    }

    // an artist matching nothing narrows to the exact empty set
    let mut g = FilterExpr::TextContains {
        field: super::TextSearchField::ArtistLower,
        word: "zzz".to_string(),
    };
    g.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
    match narrow_candidates(&g, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert!(v.is_empty()),
        _ => panic!("empty artist match must narrow to the empty set"),
    }
}

// The fingerprint is a sound necessary-condition filter: a text containing the
// needle carries every feature bit the needle carries.
#[test]
fn flavor_fingerprint_superset_property() {
    let text = flavor_fingerprint("the dream of fire never dies");
    for needle in ["dream", "fire", "never", "the dream", "re", "e"] {
        let n = flavor_fingerprint(needle);
        assert_eq!(text & n, n, "contained needle {needle:?} must be a mask subset");
    }
    // A non-contained needle with rare features is filterable.
    let z = flavor_fingerprint("zombie");
    assert_ne!(text & z, z);
    // Non-ASCII and non-alpha bytes contribute no bits on either side.
    assert_eq!(flavor_fingerprint("¡0—9!"), 0);
}

// FlavorMatch mirrors ArtistMatch at flavor scale: bind resolves the predicate
// once over the distinct texts (fingerprint-prefiltered), eval is integer
// membership with SQL NULL for flavorless printings, and narrowing expands the
// CSR in printing space.
#[test]
fn flavor_match_bind_eval_and_narrow() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab), stub_card(2, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[2, 2], vocab);
    let mut interner = Interner::new();
    let shared = interner.intern("the dream of fire".to_string());
    data.printings[0].flavor_text_lower_id = shared;
    data.printings[2].flavor_text_lower_id = shared; // same text on two printings
    data.printings[1].flavor_text_lower_id = interner.intern("a quiet forest".to_string());
    // printings[3] keeps NONE_STR: no flavor text at all
    data.strings = interner.strings;
    data.indexes.flavor = build_flavor_index(&data.printings, &data.strings);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let bound = |f: &mut FilterExpr| {
        f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
    };

    let mut f = FilterExpr::TextContains {
        field: super::TextSearchField::FlavorTextLower,
        word: "dream".to_string(),
    };
    bound(&mut f);
    let FilterExpr::FlavorMatch { ref gids, ref dense_ids } = f else { panic!("expected FlavorMatch after bind") };
    assert_eq!(gids, &vec![shared]);
    assert_eq!(dense_ids, &vec![0]); // dense ids are first-seen order

    // Per-printing evaluation: membership on the shared text, Null (no match)
    // for the flavorless printing, printing-dependent at the card level.
    let card0 = &archived.cards[0];
    let card1 = &archived.cards[1];
    assert!(f.matches(card0, &archived.printings[0], &archived.strings));
    assert!(!f.matches(card0, &archived.printings[1], &archived.strings));
    assert!(f.matches(card1, &archived.printings[2], &archived.strings));
    assert!(!f.matches(card1, &archived.printings[3], &archived.strings));
    assert!(f.eval_card(card0, &archived.strings) == Tri::PrintingDep);

    // NOT keeps NULL semantics: a flavorless printing matches neither ft:dream
    // nor its negation.
    let mut inner = FilterExpr::TextContains {
        field: super::TextSearchField::FlavorTextLower,
        word: "dream".to_string(),
    };
    bound(&mut inner);
    let neg = FilterExpr::Not(Box::new(inner));
    assert!(!neg.matches(card1, &archived.printings[3], &archived.strings));
    assert!(neg.matches(card0, &archived.printings[1], &archived.strings));

    // Narrowing expands the matched texts' CSR rows to sorted printing ids —
    // this is what makes ft: participate in Or narrowing.
    match narrow_candidates(&f, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0, 2]),
        _ => panic!("flavor predicate must narrow in printing space"),
    }

    // Exact and regex forms resolve through the same mechanism.
    let mut ex = FilterExpr::TextExact {
        field: super::TextField::FlavorTextLower,
        op: CmpOp::Eq,
        value: "a quiet forest".to_string(),
    };
    bound(&mut ex);
    match narrow_candidates(&ex, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![1]),
        _ => panic!("exact flavor must narrow"),
    }
    let mut rx = FilterExpr::TextRegex {
        field: super::TextField::FlavorTextLower,
        regex: regex::Regex::new("qu.et").unwrap(),
    };
    bound(&mut rx);
    assert!(rx.matches(card0, &archived.printings[1], &archived.strings));
    match narrow_candidates(&rx, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![1]),
        _ => panic!("regex flavor must narrow"),
    }

    // A needle matching nothing proves the empty candidate set.
    let mut none = FilterExpr::TextContains {
        field: super::TextSearchField::FlavorTextLower,
        word: "zzzqqq".to_string(),
    };
    bound(&mut none);
    match narrow_candidates(&none, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert!(v.is_empty()),
        _ => panic!("empty flavor match must narrow to the empty set"),
    }

    // The fingerprint prefilter must not change results: same sets with the
    // prefilter on (mask of the needle) and off (mask 0).
    let mask = flavor_fingerprint("dream");
    assert_ne!(mask, 0);
    let with = flavor_match_sets(&archived.indexes.flavor, &archived.strings, mask, |s| s.contains("dream"));
    let without = flavor_match_sets(&archived.indexes.flavor, &archived.strings, 0, |s| s.contains("dream"));
    assert_eq!(with, without);
}

// Collector numbers index the extracted int; fractional and out-of-range
// query values resolve to exact half-open ranges (or exact empty sets), and
// printings without a numeric part are absent (SQL NULL semantics).
#[test]
fn collector_number_narrowing() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab), stub_card(2, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[2, 2], vocab);
    data.printings[0].collector_number_int = Some(100);
    data.printings[1].collector_number_int = Some(228); // "228s" extracts to 228
    data.printings[2].collector_number_int = Some(101);
    // printings[3] has no numeric part: absent from the index
    data.indexes.collector_number =
        build_range_index(&data.printings, |p| p.collector_number_int.map(u32::from));
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let cn = |op, v| FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::CollectorNumberInt),
        op,
        rhs: NumExpr::Const(v),
    };
    let narrow = |f: &FilterExpr| match narrow_candidates(f, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => Some(v),
        // Bitmaps materialize in ascending id order, so both representations
        // compare against the same expected vectors.
        Some(Candidates::PrintingBits(b)) => Some(super::bitmap_card_ids(&b)),
        Some(_) => panic!("cn must narrow in printing space"),
        None => None,
    };

    assert_eq!(narrow(&cn(CmpOp::Eq, 100.0)), Some(vec![0]));
    assert_eq!(narrow(&cn(CmpOp::Ge, 101.0)), Some(vec![1, 2]));
    assert_eq!(narrow(&cn(CmpOp::Gt, 101.0)), Some(vec![1]));
    // Fractional bounds are exact: cn<100.5 means cn<=100; cn>100.5 means cn>=101.
    assert_eq!(narrow(&cn(CmpOp::Lt, 100.5)), Some(vec![0]));
    assert_eq!(narrow(&cn(CmpOp::Gt, 100.5)), Some(vec![1, 2]));
    // Fractional equality and out-of-range comparisons prove the empty set.
    assert_eq!(narrow(&cn(CmpOp::Eq, 100.5)), Some(vec![]));
    assert_eq!(narrow(&cn(CmpOp::Lt, -3.0)), Some(vec![]));
    // Negative lower bounds cover everything indexed (printing 3 stays absent).
    assert_eq!(narrow(&cn(CmpOp::Ge, -3.0)), Some(vec![0, 1, 2]));
    // Ne never narrows.
    assert_eq!(narrow(&cn(CmpOp::Ne, 100.0)), None);

    // Flipped operand order: 101 <= cn.
    let flipped = FilterExpr::NumericCmp {
        lhs: NumExpr::Const(101.0),
        op: CmpOp::Le,
        rhs: NumExpr::Field(NumField::CollectorNumberInt),
    };
    assert_eq!(narrow(&flipped), Some(vec![1, 2]));

    // The structural payoff: an Or with a trigram-narrowable sibling stays
    // narrowable now that cn has an index (this was the post-#622 worst query).
    let or = FilterExpr::Or(vec![cn(CmpOp::Eq, 228.0), cn(CmpOp::Eq, 100.0)]);
    assert_eq!(narrow(&or), Some(vec![0, 1]));
}

// ─── #634 Step 1: all_match promotion ─────────────────────────────────────────

/// The regression this suite exists to prevent: a printing-space predicate
/// narrowing "tight" (every posted printing genuinely matches — see
/// collector_number_narrowing above) does NOT mean "every printing of the
/// associated card matches," which is what `all_match`/`Tri::True` means at
/// card_pass's level. card 0 has two printings — one at cn=100 (matches),
/// one at cn=228 (does not) — so `cn=100` must resolve to exactly one
/// printing match, never both (which is what wrongly promoting a tight
/// printing-space result to all_match would produce).
#[test]
fn all_match_promotion_never_fires_for_printing_space_tight_results() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab), stub_card(2, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[2, 2], vocab);
    data.printings[0].collector_number_int = Some(100);
    data.printings[1].collector_number_int = Some(228);
    data.printings[2].collector_number_int = Some(101);
    data.indexes.collector_number = build_range_index(&data.printings, |p| p.collector_number_int.map(u32::from));
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut cn_eq_100 = FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::CollectorNumberInt),
        op: CmpOp::Eq,
        rhs: NumExpr::Const(100.0),
    };
    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut cn_eq_100, None, "printing", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 1, "cn=100 must match exactly one printing, not both of card 0's printings");
    assert_eq!(u128::from(page[0].1.scryfall_id), 1); // the cn=100 printing specifically

    // unique=card must also see exactly one matching card (not both, and not
    // zero), and it must be card 0.
    let mut cn_eq_100b = FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::CollectorNumberInt),
        op: CmpOp::Eq,
        rhs: NumExpr::Const(100.0),
    };
    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut cn_eq_100b, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 1);
    assert_eq!(u128::from(page[0].0.oracle_id), 1);
}

/// The positive case: a genuinely card-space exact predicate (a numeric
/// range — cmc/power/toughness are card-level fields, identical across every
/// printing) gets the same correct results with or without all_match
/// promotion. Doesn't assert card_pass was skipped (an implementation
/// detail) — just that results stay correct, across uniques, when the
/// narrowing IS safe to trust directly.
#[test]
fn all_match_promotion_correct_for_card_space_exact_predicate() {
    let mut vocab = VocabInterner::new();
    let mut card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card0.creature_power = Some(5);
    let mut card1 = stub_card(2, TYPE_CREATURE, &[], &mut vocab);
    card1.creature_power = Some(1);
    let mut data = store_of(vec![card0, card1], &[2, 3], vocab);
    data.indexes.power = build_numeric_index(&data.cards, |c| c.creature_power.map(|v| v as i16));
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut power_gt_3 = FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::Power),
        op: CmpOp::Gt,
        rhs: NumExpr::Const(3.0),
    };
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut power_gt_3, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 1); // only card0 (power 5)

    let mut power_gt_3p = FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::Power),
        op: CmpOp::Gt,
        rhs: NumExpr::Const(3.0),
    };
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut power_gt_3p, None, "printing", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 2); // card0's 2 printings, all matching (power is card-level)
}

// ─── #634 Step 2: popcount-skip order phase ───────────────────────────────────

/// Five all-Creature cards (so `t:creature` fully plane-consumes to True —
/// the whole filter, every selectivity) with three tied on cmc=3, to stress
/// exactly the property the design correction was about: descending order is
/// NOT simply the reverse of ascending, because the edhrec tie-break never
/// flips with the primary column's direction. If inverse permutations were
/// (wrongly) derived via `n-1-pos` instead of stored per-direction, the tied
/// group's internal order would come out reversed in one direction.
fn tie_break_fixture() -> (Vec<OracleCard>, VocabInterner) {
    let mut vocab = VocabInterner::new();
    let mut card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab); // cmc=3, edhrec=10
    card0.cmc = Some(3);
    card0.edhrec_rank = Some(10);
    let mut card1 = stub_card(2, TYPE_CREATURE, &[], &mut vocab); // cmc=3, edhrec=5 (lowest in the tie)
    card1.cmc = Some(3);
    card1.edhrec_rank = Some(5);
    let mut card2 = stub_card(3, TYPE_CREATURE, &[], &mut vocab); // cmc=3, edhrec=20 (highest in the tie)
    card2.cmc = Some(3);
    card2.edhrec_rank = Some(20);
    let mut card3 = stub_card(4, TYPE_CREATURE, &[], &mut vocab); // cmc=1
    card3.cmc = Some(1);
    card3.edhrec_rank = Some(1);
    let mut card4 = stub_card(5, TYPE_CREATURE, &[], &mut vocab); // cmc=5
    card4.cmc = Some(5);
    card4.edhrec_rank = Some(1);
    (vec![card0, card1, card2, card3, card4], vocab)
}

/// Expected order, by oracle_id: ascending is [card3(1), card1(3,e5),
/// card0(3,e10), card2(3,e20), card4(5)]; descending is [card4(5), card1,
/// card0, card2, card3(1)] — the tied trio (card1,card0,card2) keeps the
/// SAME internal order in both directions, only the untied cards (card3,
/// card4) swap ends.
#[test]
fn popcount_skip_tie_breaking_preserves_group_order_both_directions() {
    let (cards, vocab) = tie_break_fixture();
    let data = store_of(cards, &[1, 1, 1, 1, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let creature = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let (plane, mut residual) = split_planes(creature, &archived.indexes.planes, &archived.indexes.oracle_trigram.words);
    assert!(matches!(residual, FilterExpr::True), "t:creature must fully plane-consume");

    let mut run = |direction: &str| {
        run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut residual, plane.as_ref(), "card", "default", "cmc", direction, 100, 0, &archived.indexes,
        )
    };

    let (total, page) = run("asc");
    assert_eq!(total, 5);
    let order: Vec<u128> = page.iter().map(|(c, _)| u128::from(c.oracle_id)).collect();
    assert_eq!(order, vec![4, 2, 1, 3, 5], "ascending: tied trio in edhrec-ascending order");

    let (total, page) = run("desc");
    assert_eq!(total, 5);
    let order: Vec<u128> = page.iter().map(|(c, _)| u128::from(c.oracle_id)).collect();
    assert_eq!(
        order,
        vec![5, 2, 1, 3, 4],
        "descending: only the untied ends (card3/card4) swap — the tied trio's \
         internal order (card1,card0,card2) must stay identical to ascending, \
         not reverse"
    );
}

/// Offset landing mid-tied-group: skipping the first 2 (ascending) must land
/// exactly on the third-ranked card, not off-by-one from a word/bit
/// miscount.
#[test]
fn popcount_skip_offset_lands_inside_tied_group() {
    let (cards, vocab) = tie_break_fixture();
    let data = store_of(cards, &[1, 1, 1, 1, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let creature = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let (plane, mut residual) = split_planes(creature, &archived.indexes.planes, &archived.indexes.oracle_trigram.words);

    // Full ascending order is [card3, card1, card0, card2, card4] (oracle ids
    // [4,2,1,3,5]); offset=2 must skip card3 and card1, landing on card0.
    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual, plane.as_ref(), "card", "default", "cmc", "asc", 100, 2, &archived.indexes,
    );
    assert_eq!(total, 5);
    let order: Vec<u128> = page.iter().map(|(c, _)| u128::from(c.oracle_id)).collect();
    assert_eq!(order, vec![1, 3, 5]);

    // offset at exactly total must yield an empty page, not panic.
    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual, plane.as_ref(), "card", "default", "cmc", "asc", 100, 5, &archived.indexes,
    );
    assert_eq!(total, 5);
    assert!(page.is_empty());
}

/// The popcount-skip path must produce byte-identical results to the
/// existing (non-popcount) path for the same query — cross-checked directly
/// against run_query_streamed's counts-buffer path by disabling the plane
/// (forcing the old path) and comparing.
#[test]
fn popcount_skip_matches_non_popcount_path() {
    let (cards, vocab) = tie_break_fixture();
    let data = store_of(cards, &[1, 1, 1, 1, 1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let creature = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let (plane, mut residual_true) = split_planes(creature, &archived.indexes.planes, &archived.indexes.oracle_trigram.words);
    assert!(matches!(residual_true, FilterExpr::True));

    // Popcount path: plane present, residual True.
    let (total_pc, page_pc) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual_true, plane.as_ref(), "card", "default", "cmc", "desc", 100, 1, &archived.indexes,
    );

    // Non-popcount path: same logical filter, but passed as a real predicate
    // (not pre-consumed to True) with no plane, forcing the counts-buffer path.
    let mut creature_raw = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let (total_old, page_old) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut creature_raw, None, "card", "default", "cmc", "desc", 100, 1, &archived.indexes,
    );

    assert_eq!(total_pc, total_old);
    let ids_pc: Vec<u128> = page_pc.iter().map(|(c, _)| u128::from(c.oracle_id)).collect();
    let ids_old: Vec<u128> = page_old.iter().map(|(c, _)| u128::from(c.oracle_id)).collect();
    assert_eq!(ids_pc, ids_old);
}

// Frame postings are selectivity-thresholded at build: values covering more
// of printing space than the range guard would accept are not stored, and the
// absent-key convention makes them (and unknown values) fall back to the scan.
#[test]
fn frame_postings_thresholded_at_build() {
    let mut vocab = VocabInterner::new();
    let modern = vocab_ids(&mut vocab, &["2015"]);
    let showcase = vocab_ids(&mut vocab, &["Showcase"]);
    // 2,000 printings: all carry "2015" (dominant, must be dropped: >1,000 and
    // >25%), the first 40 also carry "Showcase" (selective, must be kept).
    let mut printings: Vec<Printing> = (1..=2000u128).map(|i| stub_printing(i, i, None)).collect();
    for (i, p) in printings.iter_mut().enumerate() {
        p.card_frame_data = modern.clone();
        if i < 40 {
            p.card_frame_data = [modern.clone(), showcase.clone()].concat();
            p.card_frame_data.sort_unstable();
        }
    }
    let idx = build_thresholded_tag_index(&printings, &vocab.strings, |p| &p.card_frame_data);
    assert!(idx.get("2015").is_none(), "dominant value must be dropped by the threshold");
    assert_eq!(idx.get("Showcase").map(|v| v.len()), Some(40));

    // Wired into narrowing: selective value narrows in printing space, the
    // dropped value declines (None, not empty — the scan still answers it).
    let indexes = CardIndexes { frame_data: idx, ..Default::default() };
    let bytes = rkyv::to_bytes::<Error>(&indexes).expect("serialize");
    let archived = rkyv::access::<Archived<CardIndexes>, Error>(&bytes).expect("access");
    let offsets_bytes = rkyv::to_bytes::<Error>(&vec![0u32, 2000]).expect("serialize offsets");
    let offsets = rkyv::access::<Archived<Vec<u32>>, Error>(&offsets_bytes).expect("access offsets");
    let coll = |value: &str| FilterExpr::CollectionCmp {
        field: CollField::FrameData,
        op: CmpOp::Ge,
        value: value.to_string(),
        value_id: None,
    };
    match narrow_candidates(&coll("Showcase"), archived, offsets, &[]) {
        Some(Candidates::Printings(v)) => assert_eq!(v.len(), 40),
        _ => panic!("selective frame value must narrow in printing space"),
    }
    assert!(narrow_candidates(&coll("2015"), archived, offsets, &[]).is_none());
    assert!(narrow_candidates(&coll("Zzz"), archived, offsets, &[]).is_none());
}

// Streamed selection must agree with the gathered path. Two stores identical
// except for the presence of sort permutations: the perm-less store takes the
// gathered path, the other streams (matches > STREAM_MIN_MATCHES). Distinct
// edhrec ranks everywhere, so no full-key tie blocks (the one place the
// canonical-secondary permutation is allowed to order differently).
#[test]
fn streamed_selection_matches_gathered() {
    const N: usize = 1_500;
    let build = |with_perms: bool| {
        let mut vocab = VocabInterner::new();
        let mut cards = Vec::with_capacity(N);
        for i in 0..N {
            let mut c = stub_card((i + 1) as u128, TYPE_CREATURE, &[], &mut vocab);
            c.cmc = Some((i % 8) as u8);
            c.edhrec_rank = Some(((i * 37) % N) as u32 + 1); // distinct, shuffled
            if i % 11 == 0 {
                c.edhrec_rank = None; // a null block, ordered by canonical secondary
            }
            cards.push(c);
        }
        let mut data = store_of(cards, &vec![3usize; N], vocab);
        // vary prices so prefer=usd_high picks different printings
        for (pid, p) in data.printings.iter_mut().enumerate() {
            p.price_usd = Some((pid % 7) as f32 + 0.5);
        }
        if with_perms {
            data.indexes.sort_perms = build_sort_permutations(&data.cards, &data.printings, &data.offsets);
            data.indexes.artwork_groups = build_artwork_group_counts(&data.printings, &data.offsets);
        }
        rkyv::to_bytes::<Error>(&data).expect("serialize")
    };
    let gathered_bytes = build(false);
    let streamed_bytes = build(true);
    let gathered = rkyv::access::<Archived<CardData>, Error>(&gathered_bytes).expect("access");
    let streamed = rkyv::access::<Archived<CardData>, Error>(&streamed_bytes).expect("access");

    // cmc >= 2 matches 6/8 of cards (1,125 > STREAM_MIN_MATCHES streams).
    let filt = || FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::Cmc),
        op: CmpOp::Ge,
        rhs: NumExpr::Const(2.0),
    };
    for unique in ["card", "printing", "artwork"] {
        for prefer in ["default", "usd_high"] {
            for orderby in ["edhrec", "cmc"] {
                for direction in ["asc", "desc"] {
                    for offset in [0usize, 7, 1120] {
                        // Fresh filter per store: run_query may memoize text
                        // predicates into store-specific string ids, so a
                        // filter must never outlive the store it ran against.
                        let (tg, pg) = run_query(
                            &gathered.cards, &gathered.printings, &gathered.offsets, &gathered.strings,
                            &mut filt(), None, unique, prefer, orderby, direction, 10, offset, &gathered.indexes,
                        );
                        let (ts, ps) = run_query(
                            &streamed.cards, &streamed.printings, &streamed.offsets, &streamed.strings,
                            &mut filt(), None, unique, prefer, orderby, direction, 10, offset, &streamed.indexes,
                        );
                        let ids = |v: &[(&super::AOracleCard, &super::APrinting)]| -> Vec<(u128, u128)> {
                            v.iter().map(|(c, p)| (u128::from(c.oracle_id), u128::from(p.scryfall_id))).collect()
                        };
                        assert_eq!(tg, ts, "total {unique}/{prefer}/{orderby}/{direction}/{offset}");
                        assert_eq!(ids(&pg), ids(&ps), "page {unique}/{prefer}/{orderby}/{direction}/{offset}");
                    }
                }
            }
        }
    }
}

// Group counts collapse duplicate illustrations within a card.
#[test]
fn artwork_group_counts_dedup_illustrations() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[4], vocab);
    data.printings[0].illustration_id = 7;
    data.printings[1].illustration_id = 7; // same art, different printing
    data.printings[2].illustration_id = 9;
    data.printings[3].illustration_id = 7;
    assert_eq!(build_artwork_group_counts(&data.printings, &data.offsets), vec![2]);
}

// Permutations put missing sort values last in both directions and reverse
// only the non-null primary order, matching sort_key_bits semantics.
#[test]
fn sort_permutations_nulls_last_both_directions() {
    let mut vocab = VocabInterner::new();
    let mut cards = vec![
        stub_card(1, TYPE_CREATURE, &[], &mut vocab),
        stub_card(2, TYPE_CREATURE, &[], &mut vocab),
        stub_card(3, TYPE_CREATURE, &[], &mut vocab),
    ];
    cards[0].cmc = Some(5);
    cards[1].cmc = None;
    cards[2].cmc = Some(1);
    let data = store_of(cards, &[1, 1, 1], vocab);
    let perms = build_sort_permutations(&data.cards, &data.printings, &data.offsets);
    assert_eq!(perms.cmc[0], vec![2, 0, 1], "asc: 1, 5, null");
    assert_eq!(perms.cmc[1], vec![0, 2, 1], "desc: 5, 1, null");
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
    match narrow_candidates(&lea, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0, 2]),
        _ => panic!("set code must narrow in printing space"),
    }
    // Unknown set code: exact empty narrowing (the index covers every code).
    let none = FilterExpr::TextExact {
        field: super::TextField::SetCode,
        op: CmpOp::Eq,
        value: "zzz".to_string(),
    };
    match narrow_candidates(&none, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert!(v.is_empty()),
        _ => panic!("unknown set code must narrow to the empty set"),
    }

    let year1993 = FilterExpr::YearCmp { op: CmpOp::Eq, year: 1993 };
    match narrow_candidates(&year1993, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![0]),
        _ => panic!("year must narrow in printing space"),
    }
    let date_ge = FilterExpr::DateCmp { op: CmpOp::Ge, value: 20240101 };
    match narrow_candidates(&date_ge, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![1]),
        _ => panic!("date must narrow in printing space"),
    }
    // Ne is not selective and must not narrow.
    assert!(narrow_candidates(&FilterExpr::DateCmp { op: CmpOp::Ne, value: 19930805 }, &archived.indexes, &archived.offsets, &archived.cards).is_none());
}

#[test]
fn broad_ranges_decline_to_narrow() {
    // Fraction rule: past MAX_NARROW_FRACTION of the index, gathering candidates
    // costs more than the scan it replaces.
    assert!(!range_too_broad_to_narrow(2_500, 10_000)); // exactly 25%: narrows
    assert!(range_too_broad_to_narrow(2_501, 10_000)); // past it: scan
    // Absolute floor: small candidate counts always narrow, even when they
    // cover the whole index (tiny stores, tests, partial imports).
    assert!(!range_too_broad_to_narrow(*NARROW_FLOOR, 10));
    assert!(range_too_broad_to_narrow(*NARROW_FLOOR + 1, 10));

    // End-to-end through the archived index: a broad slice returns None (fall
    // back to the scan), a selective slice still narrows.
    let idx: PrintingRangeIndex = (0..8_000u32).map(|v| (v, v)).collect();
    let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize");
    let archived = rkyv::access::<Archived<PrintingRangeIndex>, Error>(&bytes).expect("access");
    assert!(range_candidates(archived, 0, u32::MAX).is_none());
    assert_eq!(range_candidates(archived, 100, 200).map(|v| v.len()), Some(100));
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
    match narrow_candidates(&cheap, &archived.indexes, &archived.offsets, &archived.cards) {
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
    match narrow_candidates(&pricey, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![2]),
        _ => panic!("flipped usd comparison must narrow"),
    }
    // Ne is not selective.
    let ne = FilterExpr::NumericCmp {
        lhs: super::NumExpr::Field(super::NumField::PriceUsd),
        op: CmpOp::Ne,
        rhs: super::NumExpr::Const(1.0),
    };
    assert!(narrow_candidates(&ne, &archived.indexes, &archived.offsets, &archived.cards).is_none());
}

// ─── Bitplanes (#630) ─────────────────────────────────────────────────────────

/// A color/type-diverse store for plane parity: colorless, mono, guild pairs,
/// lands whose identity exceeds their colors, multi-type cards — and Fallaji
/// Wayfarer, the one real card (of 97,206 printings, checked 2026-07-07) whose
/// colors are NOT a subset of its color identity ("is all colors" CDA vs. a
/// {G} mana cost). Any plane scheme assuming colors ⊆ identity must fail here.
const FALLAJI_CID: usize = 5;
fn plane_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    // (card_colors, card_color_identity, card_types); color bits W=1 U=2 B=4 R=8 G=16 C=32
    let specs: &[(u8, u8, u16)] = &[
        (0, 0, TYPE_ARTIFACT),                     // colorless artifact
        (16, 16, TYPE_CREATURE),                   // mono G creature
        (1, 1, TYPE_CREATURE | TYPE_LEGENDARY),    // mono W legendary creature
        (3, 3, TYPE_INSTANT),                      // WU instant
        (0, 24, TYPE_LAND),                        // land: no colors, RG identity (Taiga)
        (31, 16, TYPE_CREATURE),                   // Fallaji Wayfarer (see FALLAJI_CID)
        (2, 3, TYPE_SORCERY),                      // U sorcery with WU identity
        (24, 31, TYPE_CREATURE | TYPE_ARTIFACT),   // RG artifact creature, WUBRG identity
        (4, 4, TYPE_ENCHANTMENT | TYPE_SNOW),      // mono B snow enchantment
        (0, 32, TYPE_LAND),                        // C-bit identity, exercising the C plane
    ];
    let cards = specs
        .iter()
        .enumerate()
        .map(|(i, &(colors, identity, types))| {
            let mut c = stub_card(i as u128 + 1, types, &[], &mut vocab);
            c.card_colors = colors;
            c.card_color_identity = identity;
            c
        })
        .collect();
    store_of(cards, &[1usize; 10], vocab)
}

// Every plane-expressible op on every color/type mask must reproduce the
// filter's card-level truth bit for bit — including Not/And/Or composition.
#[test]
fn plane_parity_color_and_type_ops() {
    let data = plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut bitmap: Vec<u64> = Vec::new();
    let mut check = |f: &FilterExpr| {
        let pe = compile_plane(f, &archived.indexes.planes, &archived.indexes.oracle_trigram.words).expect("filter must be plane-expressible");
        eval_planes(&pe, &archived.indexes.planes, &mut bitmap);
        for (cid, card) in archived.cards.iter().enumerate() {
            let want = f.eval_card(card, &archived.strings) == Tri::True;
            assert_eq!(
                bitmap_contains(&bitmap, cid as u32),
                want,
                "plane/filter mismatch at card {cid}"
            );
        }
    };

    let ops = [CmpOp::Eq, CmpOp::Ne, CmpOp::Lt, CmpOp::Le, CmpOp::Gt, CmpOp::Ge];
    let color_masks: [u8; 8] = [0, 1, 2, 16, 3, 24, 31, 32];
    let type_masks: [u16; 6] = [
        0, TYPE_CREATURE, TYPE_ARTIFACT | TYPE_CREATURE, TYPE_INSTANT,
        TYPE_LEGENDARY | TYPE_CREATURE, TYPE_SNOW,
    ];
    for op in ops {
        for mask in color_masks {
            check(&FilterExpr::ColorCmp { field: ColorField::Colors, op, mask });
            check(&FilterExpr::ColorCmp { field: ColorField::ColorIdentity, op, mask });
        }
        for mask in type_masks {
            check(&FilterExpr::TypeCmp { mask, op });
        }
    }

    let green = || FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 };
    let creature = || FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    check(&FilterExpr::And(vec![green(), creature()]));
    check(&FilterExpr::Or(vec![green(), creature()]));
    check(&FilterExpr::Not(Box::new(FilterExpr::Or(vec![green(), creature()]))));
}

// The Fallaji shape specifically: color planes and identity planes must be
// independent transpositions, not derived from one another.
#[test]
fn plane_fallaji_colors_not_subset_of_identity() {
    let data = plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut bitmap: Vec<u64> = Vec::new();
    let mut matches = |field: ColorField, op: CmpOp, mask: u8| {
        let f = FilterExpr::ColorCmp { field, op, mask };
        eval_planes(&compile_plane(&f, &archived.indexes.planes, &archived.indexes.oracle_trigram.words).unwrap(), &archived.indexes.planes, &mut bitmap);
        bitmap_contains(&bitmap, FALLAJI_CID as u32)
    };
    assert!(matches(ColorField::Colors, CmpOp::Ge, 1)); // c>=W: colors carry W
    assert!(!matches(ColorField::ColorIdentity, CmpOp::Ge, 1)); // id>=W: identity is only G
    assert!(matches(ColorField::ColorIdentity, CmpOp::Le, 16)); // id<=G holds
    assert!(!matches(ColorField::Colors, CmpOp::Le, 16)); // c<=G does not
}

// split_planes composition rules: And partitions, Or is all-or-nothing,
// produced mana and bare True stay residual.
#[test]
fn split_planes_composition_rules() {
    let data = plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let green = || FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 };
    let creature = || FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let text = || FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: "draw".to_string() };

    // And(plane, plane, text): planes consumed, the lone leftover unwraps.
    let (pe, residual) = split_planes(FilterExpr::And(vec![green(), creature(), text()]), bounds, words);
    assert!(pe.is_some());
    assert!(matches!(residual, FilterExpr::TextContains { .. }));

    // Fully plane-expressible tree is consumed whole.
    let (pe, residual) = split_planes(FilterExpr::And(vec![green(), creature()]), bounds, words);
    assert!(pe.is_some());
    assert!(matches!(residual, FilterExpr::True));
    let (pe, residual) = split_planes(FilterExpr::Or(vec![green(), creature()]), bounds, words);
    assert!(pe.is_some());
    assert!(matches!(residual, FilterExpr::True));

    // Or mixing plane and non-plane children stays entirely residual:
    // mask ∨ residual is not a narrowing mask.
    let (pe, residual) = split_planes(FilterExpr::Or(vec![green(), text()]), bounds, words);
    assert!(pe.is_none());
    assert!(matches!(residual, FilterExpr::Or(ref v) if v.len() == 2));

    // Produced mana is deliberately unplaned in phase 1.
    let produces = FilterExpr::ColorCmp { field: ColorField::ProducedMana, op: CmpOp::Ge, mask: 16 };
    let (pe, residual) = split_planes(produces, bounds, words);
    assert!(pe.is_none());
    assert!(matches!(residual, FilterExpr::ColorCmp { field: ColorField::ProducedMana, .. }));

    // Bare True keeps the range-scan path (no all-ones bitmap materialization).
    let (pe, residual) = split_planes(FilterExpr::True, bounds, words);
    assert!(pe.is_none());
    assert!(matches!(residual, FilterExpr::True));
}

// End-to-end: run_query through the plane path returns the same totals and
// pages as the plain filter path, across uniques and a mixed filter.
#[test]
fn run_query_plane_path_parity() {
    let data = plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let filters: Vec<Box<dyn Fn() -> FilterExpr>> = vec![
        Box::new(|| FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 }),
        Box::new(|| FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ne, mask: 0 }),
        Box::new(|| {
            FilterExpr::And(vec![
                FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 },
                FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge },
            ])
        }),
        Box::new(|| {
            // Mixed: the type check planes out, the numeric check stays residual.
            FilterExpr::And(vec![
                FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge },
                FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: CmpOp::Ne, rhs: NumExpr::Const(1.0) },
            ])
        }),
    ];
    for make in &filters {
        for unique in ["card", "printing", "artwork"] {
            let mut plain = make();
            let (t0, p0) = run_query(
                &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
                &mut plain, None, unique, "default", "edhrec", "asc", 100, 0, &archived.indexes,
            );
            let (pe, mut residual) = split_planes(make(), &archived.indexes.planes, &archived.indexes.oracle_trigram.words);
            let (t1, p1) = run_query(
                &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
                &mut residual, pe.as_ref(), unique, "default", "edhrec", "asc", 100, 0, &archived.indexes,
            );
            assert_eq!(t0, t1, "totals must agree (unique={unique})");
            let ids = |page: &[(&super::AOracleCard, &super::APrinting)]| -> Vec<u128> {
                page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect()
            };
            assert_eq!(ids(&p0), ids(&p1), "pages must agree (unique={unique})");
        }
    }
}

// ─── Numeric-range planes (#655) ───────────────────────────────────────────────

/// A cmc/power/toughness-diverse store: interior values at both extremes (0,
/// 12), the low tail (negative power/toughness — legal per the source data,
/// e.g. `*`-power cards), TWO distinct high-tail values per field (so the
/// high bucket is genuinely ambiguous for a within-bucket threshold, not
/// trivially a single-value bucket), and a noncreature card whose power/
/// toughness are absent (`Tri::Null`) entirely.
fn numeric_plane_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    // (cmc, power, toughness); card 0 is a noncreature (power/toughness absent).
    let specs: &[(u8, Option<i8>, Option<i8>)] = &[
        (0, None, None),
        (4, Some(-1), Some(-1)),
        (12, Some(12), Some(12)),
        (13, Some(15), Some(20)),
        (14, Some(16), Some(25)),
        (6, Some(3), Some(3)),
        (3, Some(0), Some(0)),
    ];
    let cards = specs
        .iter()
        .enumerate()
        .map(|(i, &(cmc, power, toughness))| {
            let mut c = stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab);
            c.cmc = Some(cmc);
            c.creature_power = power;
            c.creature_toughness = toughness;
            c
        })
        .collect();
    store_of(cards, &[1usize; 7], vocab)
}

// Every numeric plane that compiles must reproduce the filter's card-level
// truth bit for bit, across both operand orders, every operator, and
// thresholds spanning the low tail / interior / high tail. Declines (Ne
// always, and Eq/Lt/Le/Gt/Ge inside an ambiguous bucket) are skipped here —
// their contract is just "fall back," proven separately below.
#[test]
fn numeric_plane_parity_interior_and_tails() {
    let data = numeric_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut bitmap: Vec<u64> = Vec::new();
    let mut check = |f: &FilterExpr, label: &str| {
        let Some(pe) = compile_plane(f, &archived.indexes.planes, &archived.indexes.oracle_trigram.words) else { return };
        eval_planes(&pe, &archived.indexes.planes, &mut bitmap);
        for (cid, card) in archived.cards.iter().enumerate() {
            let want = f.eval_card(card, &archived.strings) == Tri::True;
            assert_eq!(bitmap_contains(&bitmap, cid as u32), want, "numeric plane mismatch at card {cid} for {label}");
        }
    };

    let field_name = |f: NumField| match f {
        NumField::Cmc => "cmc",
        NumField::Power => "power",
        NumField::Toughness => "toughness",
        _ => "other",
    };
    let op_name = |op: CmpOp| match op {
        CmpOp::Eq => "=",
        CmpOp::Ne => "!=",
        CmpOp::Lt => "<",
        CmpOp::Le => "<=",
        CmpOp::Gt => ">",
        CmpOp::Ge => ">=",
    };

    let fields = [NumField::Cmc, NumField::Power, NumField::Toughness];
    let ops = [CmpOp::Eq, CmpOp::Ne, CmpOp::Lt, CmpOp::Le, CmpOp::Gt, CmpOp::Ge];
    // Spans the low tail, every interior value's boundary, and the high tail.
    let thresholds: [f64; 8] = [-1.0, 0.0, 3.0, 6.0, 12.0, 13.0, 15.0, 20.0];
    for field in fields {
        for op in ops {
            for &t in &thresholds {
                let label = format!("{} {} {t}", field_name(field), op_name(op));
                check(&FilterExpr::NumericCmp { lhs: NumExpr::Field(field), op, rhs: NumExpr::Const(t) }, &label);
                check(
                    &FilterExpr::NumericCmp { lhs: NumExpr::Const(t), op, rhs: NumExpr::Field(field) },
                    &format!("{t} {} {}", op_name(op), field_name(field)),
                );
            }
        }
    }
}

// Boundary-crossing ranges (needing the low or high tail bucket folded in,
// not just the interior) must still compile exactly, and within-bucket
// distinguishing queries must decline rather than guess.
#[test]
fn numeric_plane_boundary_inclusion_and_decline() {
    let data = numeric_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let cmp = |field, op, v: f64| FilterExpr::NumericCmp { lhs: NumExpr::Field(field), op, rhs: NumExpr::Const(v) };

    // Crosses into the high tail, but includes BOTH high-tail values (13, 14)
    // fully — unambiguous, since every possible bucket member qualifies.
    assert!(compile_plane(&cmp(NumField::Cmc, CmpOp::Le, 14.0), bounds, words).is_some());
    // Crosses into the low tail: power>=-1 must include the power=-1 card.
    assert!(compile_plane(&cmp(NumField::Power, CmpOp::Ge, -1.0), bounds, words).is_some());
    // Entirely within the interior: no bucket involved at all.
    assert!(compile_plane(&cmp(NumField::Toughness, CmpOp::Le, 6.0), bounds, words).is_some());

    // Distinguishing inside the high tail bucket (which now holds two
    // distinct values each) can't be answered by the cumulative plane alone.
    assert!(compile_plane(&cmp(NumField::Cmc, CmpOp::Eq, 13.0), bounds, words).is_none());
    assert!(compile_plane(&cmp(NumField::Toughness, CmpOp::Eq, 20.0), bounds, words).is_none());
    assert!(compile_plane(&cmp(NumField::Toughness, CmpOp::Lt, 22.0), bounds, words).is_none());
}

// Ne is declined unconditionally, matching numeric_candidates' own choice
// ("Ne is not selective") rather than trying to express it as a plane
// complement (which would also fail the Not-safety guard for power/toughness).
#[test]
fn numeric_plane_declines_ne_unconditionally() {
    let data = numeric_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;
    for field in [NumField::Cmc, NumField::Power, NumField::Toughness] {
        let ne = FilterExpr::NumericCmp { lhs: NumExpr::Field(field), op: CmpOp::Ne, rhs: NumExpr::Const(3.0) };
        assert!(compile_plane(&ne, bounds, words).is_none());
    }
}

// Tri::Null (a noncreature's absent power/toughness) propagates through Not
// as Null, never flipped to True (filter.rs's FilterExpr::Not tri() arm) — so
// Not(NumericCmp) on cmc/power/toughness must always decline compile_plane,
// standalone or buried under And/Or, even though the un-negated comparison
// compiles fine on its own.
#[test]
fn numeric_plane_declines_not_over_numeric_cmp() {
    let data = numeric_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let power_gt3 = FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Power), op: CmpOp::Gt, rhs: NumExpr::Const(3.0) };
    assert!(compile_plane(&power_gt3, bounds, words).is_some(), "power>3 alone must compile");
    let make_negated = || FilterExpr::Not(Box::new(FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::Power),
        op: CmpOp::Gt,
        rhs: NumExpr::Const(3.0),
    }));
    assert!(compile_plane(&make_negated(), bounds, words).is_none(), "Not(power>3) must decline: Null doesn't flip to True");

    // Buried under And/Or, not just at the top.
    let buried_and = FilterExpr::And(vec![make_negated(), FilterExpr::True]);
    assert!(compile_plane(&buried_and, bounds, words).is_none());
    let buried_or = FilterExpr::Or(vec![make_negated(), FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge }]);
    assert!(compile_plane(&buried_or, bounds, words).is_none());

    // cmc is Option<u8> (never negative) but can still be unset on odd data,
    // so the guard applies uniformly across all three fields, not just the
    // two that are visibly Option in this fixture.
    let cmc_le6 = || FilterExpr::Not(Box::new(FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::Cmc),
        op: CmpOp::Le,
        rhs: NumExpr::Const(6.0),
    }));
    assert!(compile_plane(&cmc_le6(), bounds, words).is_none());

    // A Not over a non-numeric plane child must still compile fine — the
    // guard must not over-decline unrelated Not subtrees.
    let not_creature = FilterExpr::Not(Box::new(FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge }));
    assert!(compile_plane(&not_creature, bounds, words).is_some());
}

// End-to-end: run_query through the numeric-plane path (split_planes consumes
// the filter to True) must return identical totals/pages to the same filter
// run unconsumed (the pre-#655 fallback path), across uniques.
#[test]
fn run_query_numeric_plane_path_parity() {
    let data = numeric_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let filters: Vec<Box<dyn Fn() -> FilterExpr>> = vec![
        Box::new(|| FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: CmpOp::Le, rhs: NumExpr::Const(6.0) }),
        Box::new(|| FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Power), op: CmpOp::Ge, rhs: NumExpr::Const(-1.0) }),
        Box::new(|| FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Toughness), op: CmpOp::Lt, rhs: NumExpr::Const(-1.0) }),
        Box::new(|| {
            FilterExpr::And(vec![
                FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge },
                FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: CmpOp::Le, rhs: NumExpr::Const(12.0) },
            ])
        }),
    ];
    for make in &filters {
        for unique in ["card", "printing", "artwork"] {
            let mut plain = make();
            let (t0, p0) = run_query(
                &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
                &mut plain, None, unique, "default", "edhrec", "asc", 100, 0, &archived.indexes,
            );
            let (pe, mut residual) = split_planes(make(), &archived.indexes.planes, &archived.indexes.oracle_trigram.words);
            let (t1, p1) = run_query(
                &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
                &mut residual, pe.as_ref(), unique, "default", "edhrec", "asc", 100, 0, &archived.indexes,
            );
            assert_eq!(t0, t1, "totals must agree (unique={unique})");
            let ids = |page: &[(&super::AOracleCard, &super::APrinting)]| -> Vec<u128> {
                page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect()
            };
            assert_eq!(ids(&p0), ids(&p1), "pages must agree (unique={unique})");
        }
    }
}

// Step-2 eligibility (#634): a numeric range that fully consumes to True via
// split_planes must feed the same popcount-skip order-phase path that
// color/type planes already do — proven by a real all_match promotion, not
// just an equal-output check (which the parity test above already covers).
#[test]
fn numeric_plane_enables_all_match_promotion() {
    let data = numeric_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let cmc_le12 = FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: CmpOp::Le, rhs: NumExpr::Const(12.0) };
    let (pe, residual) = split_planes(cmc_le12, &archived.indexes.planes, &archived.indexes.oracle_trigram.words);
    assert!(pe.is_some(), "cmc<=12 must plane-compile");
    assert!(matches!(residual, FilterExpr::True), "cmc<=12 must fully consume: no residual card_pass needed");
}

// ─── Bind-time text-predicate memoization (#624) ─────────────────────────────

/// Store with real interned oracle texts and a name trigram index, for the
/// memoization tests. Card 3 has no oracle text — interned as "" exactly like
/// card_from_pydict does (contains on it is False, not NULL); card 4's text is
/// a trigram candidate for "abcde" that contains() must reject; four texts
/// share the marker "xyz" so a needle can exceed the half-corpus guard.
fn text_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let mut interner = Interner::new();
    let specs: &[(&str, Option<&str>)] = &[
        ("lightning bolt", Some("deal 3 damage to any target xyz")),
        ("healing angel", Some("when this enters, you gain 3 life xyz")),
        ("goblin token maker", Some("create two 1/1 red goblin creature tokens xyz")),
        ("vanilla bear", None),
        ("trigram trap", Some("abcbcde xyz")),
        ("draw engine", Some("draw a card. draw another card")),
    ];
    let cards: Vec<OracleCard> = specs
        .iter()
        .enumerate()
        .map(|(i, &(name, text))| {
            let mut c = stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab);
            c.card_name_lower = InlineStr::from_str(name);
            c.card_name_id = interner.intern(name.to_string());
            c.oracle_text_lower_id = interner.intern(text.unwrap_or_default().to_string());
            c
        })
        .collect();
    let mut data = store_of(cards, &[1usize; 6], vocab);
    data.strings = interner.strings;
    data.indexes.name_trigram = build_trigram_index(&data.cards, |c| c.card_name_lower.as_str());
    data.indexes.oracle_trigram = build_oracle_text_index(&data.cards, &data.strings);
    data
}

// Memoized nodes must reproduce TextContains truth for every card — including
// NULL on textless cards, trigram false positives rejected by the verify, and
// negation (Not(Null) stays Null, so `-o:x` still excludes textless cards).
#[test]
fn memoize_text_predicates_parity() {
    let data = text_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let memo = |mut f: FilterExpr| {
        f.memoize_text_predicates(&archived.cards, &archived.strings, &archived.indexes.name_trigram, &archived.indexes.name_bigrams, &archived.indexes.oracle_trigram, archived.cards.len());
        f
    };
    let oracle = |w: &str| FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: w.to_string() };
    let name = |w: &str| FilterExpr::TextContains { field: TextSearchField::NameLower, word: w.to_string() };

    for needle in ["damage", "draw", "goblin", "abcde", "card.", "zzz"] {
        let rewritten = memo(oracle(needle));
        assert!(matches!(rewritten, FilterExpr::OracleMatch { .. }), "oracle:{needle} must rewrite");
        let neg = memo(FilterExpr::Not(Box::new(oracle(needle))));
        let neg_orig = FilterExpr::Not(Box::new(oracle(needle)));
        for card in archived.cards.iter() {
            assert!(rewritten.eval_card(card, &archived.strings) == oracle(needle).eval_card(card, &archived.strings));
            assert!(neg.eval_card(card, &archived.strings) == neg_orig.eval_card(card, &archived.strings));
        }
    }
    for needle in ["angel", "goblin", "trap", "zzz"] {
        let rewritten = memo(name(needle));
        assert!(matches!(rewritten, FilterExpr::NameMatch { .. }), "name:{needle} must rewrite");
        for card in archived.cards.iter() {
            assert!(rewritten.eval_card(card, &archived.strings) == name(needle).eval_card(card, &archived.strings));
        }
    }
    // "abcde"'s trigrams all exist in "abcbcde" but the text does not contain
    // it: the verify must reject, leaving an empty match set.
    match memo(oracle("abcde")) {
        FilterExpr::OracleMatch { gids } => assert!(gids.is_empty(), "trigram false positive must be verified away"),
        _ => panic!("must rewrite"),
    }
}

// The rewrite must refuse: sub-trigram needles, non-card text fields, and
// needles whose candidates exceed half the corpus (binary search stops paying).
#[test]
fn memoize_text_predicates_guards() {
    let data = text_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let memo = |mut f: FilterExpr| {
        f.memoize_text_predicates(&archived.cards, &archived.strings, &archived.indexes.name_trigram, &archived.indexes.name_bigrams, &archived.indexes.oracle_trigram, archived.cards.len());
        f
    };
    let short = memo(FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: "dr".to_string() });
    assert!(matches!(short, FilterExpr::TextContains { .. }), "2-char needle has no trigrams");
    let flavor = memo(FilterExpr::TextContains { field: TextSearchField::FlavorTextLower, word: "damage".to_string() });
    assert!(matches!(flavor, FilterExpr::TextContains { .. }), "flavor is printing-level, not ours");
    // "xyz" appears in 4 of the 6 distinct texts (> half): guard keeps the scan.
    let broad = memo(FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: "xyz".to_string() });
    assert!(matches!(broad, FilterExpr::TextContains { .. }), "broad needle stays unrewritten");
}

// End-to-end trigger: a full-scan Or (unnarrowable sibling) memoizes inside
// run_query and returns the brute-force result; a narrowable query is left
// untouched.
#[test]
fn run_query_memoizes_only_full_scans() {
    let data = text_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let oracle = |w: &str| FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: w.to_string() };
    // Keywords <= "flying": no narrowing arm for Le, true for keyword-less cards.
    let broad_sibling = || FilterExpr::CollectionCmp {
        field: CollField::Keywords,
        op: CmpOp::Le,
        value: "flying".to_string(),
        value_id: None,
    };

    let mut f = FilterExpr::Or(vec![oracle("draw"), broad_sibling()]);
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut f, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 6, "every card matches the Or (empty keywords pass Le)");
    match &f {
        FilterExpr::Or(children) => assert!(
            matches!(children[0], FilterExpr::OracleMatch { .. }),
            "full-scan Or must memoize its oracle child"
        ),
        _ => panic!("shape preserved"),
    }

    // Narrowable single predicate: candidates exist, no rewrite.
    let mut g = oracle("draw");
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut g, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 1);
    assert!(matches!(g, FilterExpr::TextContains { .. }), "narrowable query stays unrewritten");
}

// The NONE_STR → Null defense in OracleMatch mirrors TextContains exactly.
// Loaded cards can't produce this state (missing text interns ""), but stub
// cards can, and both paths must agree there too.
#[test]
fn oracle_match_none_str_mirrors_text_contains() {
    let mut vocab = VocabInterner::new();
    let card = stub_card(1, TYPE_CREATURE, &[], &mut vocab); // oracle_text_lower_id = NONE_STR
    let data = store_of(vec![card], &[1], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let memoized = FilterExpr::OracleMatch { gids: Vec::new() };
    let plain = FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: "draw".to_string() };
    assert!(memoized.eval_card(&archived.cards[0], &archived.strings) == Tri::Null);
    assert!(plain.eval_card(&archived.cards[0], &archived.strings) == Tri::Null);
}

// ─── Oracle word index (docs/issues/engine-oracle-word-index.md) ────────────

/// 17 distinct oracle texts (n_texts=17, so words_per_plane=1 and the #639
/// crossover is "dense iff a word's own text count exceeds 4"), deliberately
/// engineered so a single `oracle:` needle can exercise every cell of the
/// query-time dispatch:
/// - "target": word "target" alone contains it (5 texts, dense) and no other
///   dictionary word does — the single-dense-hit, no-sparse-hit shape.
/// - "creature": word "creature" (9 texts, dense) plus "creaturehood" (1
///   text, sparse) both contain it — the mixed dense+sparse shape.
/// - "cast": "cast"/"recast"/"broadcast" (1 text each, all sparse) all
///   contain it — the dense-empty, multi-sparse-hit shape.
/// - "zzzzz": nothing contains it — the both-empty shape.
fn oracle_word_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let mut interner = Interner::new();
    let texts: &[&str] = &[
        "this creature has flying",
        "this creature has trample",
        "when this creature enters draw a card",
        "sacrifice a creature gain 1 life",
        "target creature gets plus one plus one",
        "destroy target creature",
        "return target creature to owner hand",
        "counter target creature spell",
        "create a token",
        "create two tokens",
        "create a token thats a copy",
        "exile target artifact or creature",
        "cast this spell only during combat",
        "you may recast spells",
        "broadcast a signal to allies",
        "vanilla bear with no text",
        "no true creaturehood exists",
    ];
    let cards: Vec<OracleCard> = texts
        .iter()
        .enumerate()
        .map(|(i, text)| {
            let mut c = stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab);
            c.oracle_text_lower_id = interner.intern(text.to_string());
            c
        })
        .collect();
    let mut data = store_of(cards, &[1usize; 17], vocab);
    data.strings = interner.strings;
    data.indexes.oracle_trigram = build_oracle_text_index(&data.cards, &data.strings);
    data
}

/// 6 distinct texts (n_texts=6, words_per_plane=1, dense iff a word's own
/// text count exceeds 4) engineered so a single needle has TWO dense hits and
/// ZERO sparse hits: "wordone" and "wordtwo" each appear in 5 of the 6 texts,
/// and both contain "word" as a substring, with no other dictionary word
/// containing it. This is the one dispatch shape oracle_word_fixture_store
/// doesn't produce — "creature" there mixes one dense hit with one sparse hit
/// ("creaturehood"), never two-or-more dense hits alone — even though both
/// land in the same catch-all match arm in narrow_rec.
fn oracle_word_multi_dense_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let mut interner = Interner::new();
    let texts: &[&str] = &[
        "wordone wordtwo alpha",
        "wordone wordtwo beta",
        "wordone wordtwo gamma",
        "wordone wordtwo delta",
        "wordone wordtwo epsilon",
        "nothing relevant here",
    ];
    let cards: Vec<OracleCard> = texts
        .iter()
        .enumerate()
        .map(|(i, text)| {
            let mut c = stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab);
            c.oracle_text_lower_id = interner.intern(text.to_string());
            c
        })
        .collect();
    let mut data = store_of(cards, &[1usize; 6], vocab);
    data.strings = interner.strings;
    data.indexes.oracle_trigram = build_oracle_text_index(&data.cards, &data.strings);
    data
}

/// Brute-force `oracle:<needle>` over every card, for differential comparison.
fn brute_force_oracle_contains(archived: &Archived<CardData>, needle: &str) -> Vec<u32> {
    (0..archived.cards.len() as u32)
        .filter(|&cid| archived.strings[u32::from(archived.cards[cid as usize].oracle_text_lower_id) as usize].as_str().contains(needle))
        .collect()
}

// Every eligible needle (len > 3, no token-boundary bytes) must narrow to the
// exact brute-force match set, tight — no verification pass, matching the
// design doc's exactness argument.
#[test]
fn oracle_word_index_exact_union_parity() {
    let data = oracle_word_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let rec = |f: &FilterExpr| super::narrow_rec(f, &archived.indexes, &archived.offsets, &archived.cards, true);
    let oracle = |w: &str| FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: w.to_string() };

    for needle in ["target", "creature", "cast", "zzzzz"] {
        let expected = brute_force_oracle_contains(archived, needle);
        let n = rec(&oracle(needle)).unwrap_or_else(|| panic!("oracle:{needle} must narrow"));
        assert!(n.tight, "oracle:{needle} must narrow tight (exact, no verification)");
        assert_eq!(n.set.into_cards(&archived.offsets), expected, "oracle:{needle} must match the brute-force set exactly");
    }
}

// Pins the specific representation each needle takes, so a future change that
// silently falls back to a superset (losing exactness) or picks the wrong
// tier fails loudly here instead of only in the parity test above.
#[test]
fn oracle_word_index_dispatch_shapes() {
    let data = oracle_word_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let rec = |f: &FilterExpr| super::narrow_rec(f, &archived.indexes, &archived.offsets, &archived.cards, true);
    let oracle = |w: &str| FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: w.to_string() };

    // Single dense hit, no sparse hit: the dense word's bitmap comes back
    // directly, no allocation-and-scatter round trip.
    match rec(&oracle("target")).expect("must narrow").set {
        Candidates::CardBits(_) => {}
        other => panic!("single dense hit must return CardBits directly, got {other:?}", other = std::mem::discriminant(&other)),
    }
    // Dense-empty, sparse hits only: a plain sorted-merge union, no bitmap
    // touched at all.
    match rec(&oracle("cast")).expect("must narrow").set {
        Candidates::Cards(_) => {}
        other => panic!("sparse-only hits must stay a Cards vec, got {other:?}", other = std::mem::discriminant(&other)),
    }
    // Both empty: the empty set is exact (no card can contain a needle no
    // dictionary word contains), not a decline.
    match rec(&oracle("zzzzz")).expect("empty is still exact narrowing").set {
        Candidates::Cards(v) => assert!(v.is_empty()),
        other => panic!("no-hit shape must be an empty Cards vec, got {other:?}", other = std::mem::discriminant(&other)),
    }
    // Mixed dense+sparse: scratch bitmap, OR the dense hit in, scatter the
    // expanded sparse hit on top.
    match rec(&oracle("creature")).expect("must narrow").set {
        Candidates::CardBits(_) => {}
        other => panic!("mixed dense+sparse must return CardBits, got {other:?}", other = std::mem::discriminant(&other)),
    }
}

// Two-or-more dense hits with zero sparse hits lands in the same catch-all
// match arm as "mixed dense+sparse" above, but oracle_word_fixture_store never
// produces that exact shape (its only multi-hit needle, "creature", always
// pairs a dense hit with a sparse one) — so this pins it with a dedicated
// fixture, both for exactness and for the CardBits representation.
#[test]
fn oracle_word_index_multi_dense_no_sparse() {
    let data = oracle_word_multi_dense_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let rec = |f: &FilterExpr| super::narrow_rec(f, &archived.indexes, &archived.offsets, &archived.cards, true);
    let oracle = |w: &str| FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: w.to_string() };

    let expected = brute_force_oracle_contains(archived, "word");
    let n = rec(&oracle("word")).expect("oracle:word must narrow");
    assert!(n.tight, "oracle:word must narrow tight (exact, no verification)");
    match &n.set {
        Candidates::CardBits(_) => {}
        other => panic!("2+ dense hits, no sparse, must return CardBits, got {other:?}", other = std::mem::discriminant(other)),
    }
    assert_eq!(n.set.into_cards(&archived.offsets), expected, "oracle:word must match the brute-force set exactly");
}

// compile_plane's bonus arm only fires for the single-dense-hit shape (needed
// for correctness — a mixed shape's dense bitmap alone would undercount) and,
// when it does, composes with an unrelated plane predicate via a plain AND —
// exercising PlaneExpr::Bits directly instead of just the standalone
// narrow_rec path above.
#[test]
fn compile_plane_word_bonus_composes_with_other_planes() {
    let data = oracle_word_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;
    let oracle = |w: &str| FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: w.to_string() };

    // Single-dense-hit needle: compile_plane must consume it directly.
    assert!(compile_plane(&oracle("target"), bounds, words).is_some(), "single dense hit must compile to a plane");
    // Mixed dense+sparse needle: compile_plane must decline (the dense bitmap
    // alone would miss the sparse "creaturehood" match) — narrow_rec's
    // general dispatch is the only correct path for this shape.
    assert!(compile_plane(&oracle("creature"), bounds, words).is_none(), "mixed dense+sparse must not compile: dense bitmap alone would undercount");

    // AND with an unrelated plane-expressible predicate (every card here is a
    // creature, so this is a true tautological AND, just exercising composition).
    let creature_type = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let both = FilterExpr::And(vec![oracle("target"), creature_type]);
    let pe = compile_plane(&both, bounds, words).expect("dense word AND a plane predicate must compile whole");
    let mut bitmap: Vec<u64> = Vec::new();
    eval_planes(&pe, bounds, &mut bitmap);
    assert_eq!(bitmap_card_ids(&bitmap), brute_force_oracle_contains(archived, "target"), "every card here is a creature, so the AND changes nothing");
}

// SortedTrigramIndex's posting-vs-plane dispatch (merge/probe/AND), forced
// through every combination by picking a domain small enough that some
// trigrams promote to the dense tier and some don't.
#[test]
fn trigram_dense_sparse_dispatch_parity() {
    // Domain 40: words_per_plane(40)=1, plane_bytes=8, dense iff count > 4.
    let domain = 40usize;
    // "aaa": 6 ids (dense). "bbb": 6 ids, same 6 ids as "aaa" (dense x dense,
    // AND path). "ccc": 2 ids, subset of aaa's (sparse x dense, probe path).
    // "ddd": 2 ids disjoint from "ccc" (sparse x sparse, merge path — already
    // covered elsewhere, included here for completeness).
    let mut idx: HashMap<[u8; 3], Vec<u32>> = HashMap::new();
    idx.insert(*b"aaa", vec![1, 2, 3, 4, 5, 6]);
    idx.insert(*b"bbb", vec![1, 2, 3, 4, 5, 6]);
    idx.insert(*b"ccc", vec![2, 4]);
    idx.insert(*b"ddd", vec![7, 8]);
    let finalized = super::finalize_trigram_index(idx, domain);
    let bytes = rkyv::to_bytes::<Error>(&finalized).expect("serialize");
    let archived = rkyv::access::<Archived<SortedTrigramIndex>, Error>(&bytes).expect("access");

    // "aaabbb": trigrams aaa, aab(absent -> empty), ... — use a needle whose
    // trigrams are exactly {aaa, bbb} minus the absent middle ones isn't
    // possible with real sliding windows, so exercise the dispatch directly
    // through needles built to hit exactly the desired trigram pairs.
    assert_eq!(trigram_candidates(archived, "aaa").unwrap(), vec![1, 2, 3, 4, 5, 6], "single dense trigram: bitmap bit-scanned back to ids");
    assert_eq!(super::trigram_min_posting(archived, "aaa"), Some(6));
    assert_eq!(super::trigram_min_posting(archived, "ccc"), Some(2));

    // ccc's sparse posting [2,4] probed against aaa's dense plane: both 2 and
    // 4 are set, so the probe keeps everything — same answer as a merge would
    // give, but taken through the posting-vs-plane path.
    let ops = vec![super::lookup_trigram(archived, *b"aaa").unwrap(), super::lookup_trigram(archived, *b"ccc").unwrap()];
    assert_eq!(super::intersect_operands(ops), vec![2, 4], "posting seed probed against a plane operand");

    // aaa AND bbb (both dense, identical bitmaps): plane x plane AND path,
    // with no posting to seed from at all.
    let ops = vec![super::lookup_trigram(archived, *b"aaa").unwrap(), super::lookup_trigram(archived, *b"bbb").unwrap()];
    assert_eq!(super::intersect_operands(ops), vec![1, 2, 3, 4, 5, 6], "plane x plane AND path, no posting seed available");
}

// ─── Adaptive candidate sets (#636) ───────────────────────────────────────────

// range_narrowed never declines: sparse ranges keep the vec path, broad ones
// become bitmaps — scattered directly when the slice is the smaller side,
// complement-scattered (loose) when it isn't. Uses a synthetic index large
// enough to clear NARROW_FLOOR, which tiny fixtures never do.
#[test]
fn range_narrowed_representations() {
    let printings: Vec<Printing> = (0..4096u32).map(|i| {
        let mut p = stub_printing(u128::from(i) + 1, u128::from(i) + 1, None);
        // values 0..1024, four printings per value; printings 0-3 get value 0, etc.
        p.price_usd = Some((i / 4) as f32);
        p
    }).collect();
    let idx = build_range_index(&printings, |p| p.price_usd.map(super::f32_sort_bits));
    let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize");
    let archived = rkyv::access::<Archived<PrintingRangeIndex>, Error>(&bytes).expect("access");
    let n = printings.len();
    let bounds = |op, v| super::price_bounds(op, v).unwrap();

    // Bounds are widened one position for f32 rounding, so `< v` includes v's
    // own printings as superset members — counts below are values 0..=v.
    // Sparse (164 entries, 4%): vec, tight.
    let (lo, hi) = bounds(CmpOp::Lt, 40.0);
    let nr = super::range_narrowed(archived, lo, hi, n, false, false).expect("sparse ranges narrow in any context");
    assert!(!nr.tight, "price bounds are widened supersets, never tight");
    match nr.set {
        Candidates::Printings(v) => assert_eq!(v.len(), 164),
        _ => panic!("sparse range must stay a vec"),
    }

    // Broad but at most half (values 0..=400 → 1604 entries, 39%): direct scatter, tight.
    let (lo, hi) = bounds(CmpOp::Lt, 400.0);
    assert!(super::range_narrowed(archived, lo, hi, n, false, true).is_none(), "broad bits need a consumer (broad_ok)");
    let nr = super::range_narrowed(archived, lo, hi, n, true, true).expect("broad_ok materializes the bitmap");
    assert!(nr.tight, "integer-exact bounds keep the direct scatter tight");
    match &nr.set {
        Candidates::PrintingBits(b) => {
            assert_eq!(b.iter().map(|w| w.count_ones()).sum::<u32>(), 1604);
            assert!(bitmap_contains(b, 0) && !bitmap_contains(b, 1700));
        }
        _ => panic!("broad range must be a bitmap"),
    }

    // Beyond half (values 0..=900 → 3604 entries, 88%): complement scatter, loose.
    let (lo, hi) = bounds(CmpOp::Lt, 900.0);
    let nr = super::range_narrowed(archived, lo, hi, n, true, true).expect("broad_ok materializes the complement");
    assert!(!nr.tight, "complement over-includes unindexed printings");
    match &nr.set {
        Candidates::PrintingBits(b) => {
            assert_eq!(b.iter().map(|w| w.count_ones()).sum::<u32>(), 3604);
            assert!(bitmap_contains(b, 0) && !bitmap_contains(b, 3700));
        }
        _ => panic!("broad range must be a bitmap"),
    }
}

/// Store for composition tests: subtypes, colors/types (planes), per-printing
/// set codes and rarities, with the real index builders the reload path uses.
fn narrow_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let specs: &[(&[&str], u8, u16)] = &[
        (&["goblin"], 8, TYPE_CREATURE),         // R goblin
        (&["goblin"], 8, TYPE_CREATURE),         // R goblin
        (&["merfolk"], 2, TYPE_CREATURE),        // U merfolk
        (&[], 16, TYPE_ENCHANTMENT),             // G enchantment
        (&[], 0, TYPE_ARTIFACT),                 // colorless artifact
        (&["wizard"], 2, TYPE_CREATURE),         // U wizard
    ];
    let cards: Vec<OracleCard> = specs.iter().enumerate().map(|(i, &(subs, colors, types))| {
        let mut c = stub_card(i as u128 + 1, types, subs, &mut vocab);
        c.card_colors = colors;
        c.card_color_identity = colors;
        c
    }).collect();
    let mut data = store_of(cards, &[2usize; 6], vocab);
    for (i, p) in data.printings.iter_mut().enumerate() {
        p.card_set_code = InlineStr::from_str(if i % 2 == 0 { "lea" } else { "m21" });
        p.card_rarity_int = Some((i % 2) as u8); // even printings common, odd uncommon
    }
    data.indexes.subtypes = build_tag_index(&data.cards, &data.coll_vocab, |c| &c.card_subtypes);
    data.indexes.rarity = build_rarity_index(&data.printings, &data.offsets);
    let mut set_codes: TagIndex = HashMap::new();
    for (i, p) in data.printings.iter().enumerate() {
        set_codes.entry(p.card_set_code.as_str().to_string()).or_default().push(i as u32);
    }
    data.indexes.set_codes = set_codes;
    data
}

// Tightness and complement rules: Not narrows only through tight children.
// Rarity postings are the trap — a mixed-rarity card matches both `r:x` and
// `-r:x`, so complementing its card-space existence set would drop real
// matches; the loose tag must block the Not arm.
#[test]
fn not_narrows_only_tight_children() {
    let data = narrow_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let rec = |f: &FilterExpr| super::narrow_rec(f, &archived.indexes, &archived.offsets, &archived.cards, true);

    // value_id bound as production's bind() would — narrowing keys on the
    // string, evaluation on the id, and they must agree.
    let goblin_id = archived.coll_vocab.iter().position(|s| s.as_str() == "goblin").map(|i| i as u16);
    let goblin = || FilterExpr::CollectionCmp { field: CollField::Subtypes, op: CmpOp::Ge, value: "goblin".into(), value_id: goblin_id };
    let rarity = || FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(0.0) };
    // 1-char: below even the bigram floor, so genuinely unindexable.
    let name1 = || FilterExpr::TextContains { field: TextSearchField::NameLower, word: "q".into() };

    // Tight leaf → complement narrows, loose, and covers every ¬-match.
    let n = rec(&FilterExpr::Not(Box::new(goblin()))).expect("Not(subtype) must narrow");
    assert!(!n.tight);
    let cand = n.set.into_cards(&archived.offsets);
    for (cid, card) in archived.cards.iter().enumerate() {
        let matches_not = (u32::from(archived.offsets[cid])..u32::from(archived.offsets[cid + 1]))
            .any(|p| FilterExpr::Not(Box::new(goblin())).matches(card, &archived.printings[p as usize], &archived.strings));
        if matches_not {
            assert!(cand.contains(&(cid as u32)), "complement must not drop card {cid}");
        }
    }

    // Loose children block the Not arm entirely.
    assert!(rec(&FilterExpr::Not(Box::new(rarity()))).is_none(), "rarity existence sets are not complement-safe");
    assert!(rec(&FilterExpr::Not(Box::new(name1()))).is_none(), "sub-bigram text cannot narrow at all");
    let double_not = FilterExpr::Not(Box::new(FilterExpr::Not(Box::new(goblin()))));
    assert!(rec(&double_not).is_none(), "complements are loose, so double negation stops narrowing");

    // Printing-space complement: -set:lea covers the m21 printings' cards.
    let not_lea = FilterExpr::Not(Box::new(FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value: "lea".into() }));
    let n = rec(&not_lea).expect("Not(set) must narrow");
    assert!(matches!(n.set, Candidates::PrintingBits(_)));
}

// Or composition with previously-vetoing children: a plane-expressible color
// child and a Not child now contribute bitmaps instead of forcing None.
#[test]
fn or_composes_plane_and_complement_children() {
    let data = narrow_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let rec = |f: &FilterExpr| super::narrow_rec(f, &archived.indexes, &archived.offsets, &archived.cards, true);

    let goblin_id = archived.coll_vocab.iter().position(|s| s.as_str() == "goblin").map(|i| i as u16);
    let goblin = || FilterExpr::CollectionCmp { field: CollField::Subtypes, op: CmpOp::Ge, value: "goblin".into(), value_id: goblin_id };
    let green = || FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 };

    // ColorCmp had no narrowing arm before #636; via the plane feed-in the Or composes.
    let or = FilterExpr::Or(vec![goblin(), green()]);
    let n = rec(&or).expect("Or(subtype, color) must narrow");
    let cand = n.set.into_cards(&archived.offsets);
    assert_eq!(cand, vec![0, 1, 3], "goblins ∪ green");

    // An unindexable child still vetoes: nothing can represent it (1-char is
    // below even the bigram floor).
    let or = FilterExpr::Or(vec![goblin(), FilterExpr::TextContains { field: TextSearchField::NameLower, word: "q".into() }]);
    assert!(rec(&or).is_none());
}

// End-to-end: run_query totals equal brute-force counts for shapes that
// exercise every new composition rule. Narrowing is advisory, so an unsound
// candidate set shows up here as a missing match.
#[test]
fn adaptive_narrowing_run_query_parity() {
    let data = narrow_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let goblin_id = archived.coll_vocab.iter().position(|s| s.as_str() == "goblin").map(|i| i as u16);
    let goblin = || FilterExpr::CollectionCmp { field: CollField::Subtypes, op: CmpOp::Ge, value: "goblin".into(), value_id: goblin_id };
    let green = || FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 };
    let lea = || FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value: "lea".into() };
    let rarity = || FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(0.0) };

    let filters: Vec<FilterExpr> = vec![
        FilterExpr::Or(vec![goblin(), green()]),
        FilterExpr::Or(vec![goblin(), rarity()]),
        FilterExpr::Not(Box::new(goblin())),
        FilterExpr::Not(Box::new(lea())),
        FilterExpr::Not(Box::new(rarity())),
        FilterExpr::And(vec![FilterExpr::Not(Box::new(lea())), goblin()]),
        FilterExpr::And(vec![green(), FilterExpr::Or(vec![goblin(), lea()])]),
        FilterExpr::Or(vec![FilterExpr::Not(Box::new(goblin())), lea()]),
    ];
    for f in filters {
        let brute = archived
            .cards
            .iter()
            .enumerate()
            .filter(|(cid, card)| {
                (u32::from(archived.offsets[*cid])..u32::from(archived.offsets[cid + 1]))
                    .any(|p| f.matches(card, &archived.printings[p as usize], &archived.strings))
            })
            .count();
        let mut f2 = f;
        let (total, _) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut f2, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
        );
        assert_eq!(total, brute, "narrowing must stay advisory-sound");
    }
}

// Not over a partially-represented And: if a child contributes no candidate
// set (unindexable, skipped, or dropped), the And's intersection members need
// not satisfy that child, so the result must NOT be tight — a complement over
// it would drop cards that fail the unrepresented child (which is exactly what
// makes them match the negation). Caught by inspection in review; pinned here.
#[test]
fn not_over_partial_and_is_blocked() {
    let data = narrow_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let goblin_id = archived.coll_vocab.iter().position(|s| s.as_str() == "goblin").map(|i| i as u16);
    let goblin = || FilterExpr::CollectionCmp { field: CollField::Subtypes, op: CmpOp::Ge, value: "goblin".into(), value_id: goblin_id };
    let unindexable = || FilterExpr::TextContains { field: TextSearchField::NameLower, word: "q".into() };

    // Static check: And with an unrepresentable child can't be tight → Not
    // must refuse to narrow at all.
    let not_partial = FilterExpr::Not(Box::new(FilterExpr::And(vec![goblin(), unindexable()])));
    assert!(super::narrow_rec(&not_partial, &archived.indexes, &archived.offsets, &archived.cards, true).is_none());

    // Dynamic check via run_query: totals must equal brute force. Every card
    // fails `name:q` here, so every card matches the negation — a complement
    // of the goblins-only set would have dropped cards 0 and 1.
    let brute = archived
        .cards
        .iter()
        .enumerate()
        .filter(|(cid, card)| {
            (u32::from(archived.offsets[*cid])..u32::from(archived.offsets[cid + 1]))
                .any(|p| not_partial.matches(card, &archived.printings[p as usize], &archived.strings))
        })
        .count();
    let mut f = FilterExpr::Not(Box::new(FilterExpr::And(vec![goblin(), unindexable()])));
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut f, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, brute);
    assert_eq!(total, 6, "every card matches -(goblin and name:q)");
}


// The review's reproduction: price bounds are widened one position for
// f32/f64 rounding (superset contract), so a price-range set must never be
// tight — Not would complement away the boundary printings, which are exactly
// the negation's matches. A printing priced exactly 5.0 fails `usd>5` and
// must survive `-usd>5`.
#[test]
fn not_over_price_range_keeps_boundary_matches() {
    let mut vocab = VocabInterner::new();
    let cards: Vec<OracleCard> = (0..6).map(|i| stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab)).collect();
    let mut data = store_of(cards, &[2usize; 6], vocab);
    for (i, p) in data.printings.iter_mut().enumerate() {
        p.price_usd = Some(if i < 2 { 5.0 } else { 10.0 }); // card 0 sits on the boundary
    }
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd.map(super::f32_sort_bits));
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut f = FilterExpr::Not(Box::new(FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::PriceUsd),
        op: CmpOp::Gt,
        rhs: NumExpr::Const(5.0),
    }));
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut f, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 1, "the boundary-priced card matches -usd>5 and must not be complemented away");
}

// ─── Name bigram index (#639) ─────────────────────────────────────────────────

// Tier assignment at the derived crossover (plane bytes vs 2 bytes/entry),
// and exactness on both tiers: for a 2-byte needle, the set IS the answer.
#[test]
fn name_bigrams_tiers_and_exactness() {
    // 4,096 cards: every card contains "zz" (dense → plane tier); every 64th
    // contains "qx" (64 entries × 2 B ≤ the 512 B plane cost at this store
    // size → u16 postings tier; the crossover scales with n_cards).
    let mut vocab = VocabInterner::new();
    let cards: Vec<OracleCard> = (0..4096u32).map(|i| {
        let mut c = stub_card(u128::from(i) + 1, TYPE_CREATURE, &[], &mut vocab);
        let name = if i % 64 == 0 { format!("azz qx{i}") } else { format!("azz b{i}") };
        c.card_name_lower = InlineStr::from_str(&name);
        c
    }).collect();
    let data = store_of(cards, &vec![1usize; 4096], vocab);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let idx = &archived.indexes.name_bigrams;

    assert!(idx.plane_of.get(&[b'z', b'z']).is_some(), "4,096-name bigram must promote to a plane");
    assert!(idx.postings.get(&[b'q', b'x']).is_some(), "64-name bigram stays a posting list");

    let rec = |w: &str| {
        let f = FilterExpr::TextContains { field: TextSearchField::NameLower, word: w.to_string() };
        super::narrow_rec(&f, &archived.indexes, &archived.offsets, &archived.cards, false)
    };
    // Dense tier: exact bitmap, tight.
    let n = rec("zz").expect("dense bigram narrows");
    assert!(n.tight);
    assert!(matches!(n.set, Candidates::CardBits(_)));
    assert_eq!(n.set.len(), 4096);
    // Sparse tier: exact vec, tight, and byte-for-byte the contains() answer.
    let n = rec("qx").expect("sparse bigram narrows");
    assert!(n.tight);
    let cand = n.set.into_cards(&archived.offsets);
    let brute: Vec<u32> = archived.cards.iter().enumerate()
        .filter(|(_, c)| c.card_name_lower.as_str().contains("qx"))
        .map(|(i, _)| i as u32)
        .collect();
    assert_eq!(cand, brute, "bigram membership IS containment for 2-byte needles");
    // Absent bigram: exact empty (no name contains it), not None.
    let n = rec("vw").expect("absent bigram is an exact empty narrowing");
    assert_eq!(n.set.len(), 0);
    // 1-char stays unindexable.
    let f = FilterExpr::TextContains { field: TextSearchField::NameLower, word: "z".to_string() };
    assert!(super::narrow_rec(&f, &archived.indexes, &archived.offsets, &archived.cards, false).is_none());
}

// The motivating composition: `name:xx or name:yy` previously full-scanned
// (two per-card substring searches); both children now contribute exact sets.
// Also: Not(bigram) is a sound complement, and memoize rewrites 2-byte needles
// to NameMatch with zero contains() calls in full-scan contexts.
#[test]
fn name_bigrams_compose_and_memoize() {
    let mut vocab = VocabInterner::new();
    let mut interner = Interner::new();
    let names = ["fire drake", "field agent", "drone", "bear", "firm hand", "quiet"];
    let cards: Vec<OracleCard> = names.iter().enumerate().map(|(i, name)| {
        let mut c = stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab);
        c.card_name_lower = InlineStr::from_str(name);
        c.card_name_id = interner.intern(name.to_string());
        c
    }).collect();
    let mut data = store_of(cards, &[1usize; 6], vocab);
    data.strings = interner.strings;
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let name2 = |w: &str| FilterExpr::TextContains { field: TextSearchField::NameLower, word: w.to_string() };

    // Or of two bigram children composes: "fi" → {0,1,4}, "dr" → {0,2}.
    let or = FilterExpr::Or(vec![name2("fi"), name2("dr")]);
    let n = super::narrow_rec(&or, &archived.indexes, &archived.offsets, &archived.cards, false).expect("bigram Or composes");
    assert_eq!(n.set.into_cards(&archived.offsets), vec![0, 1, 2, 4]);

    // run_query parity across the new shapes, negation included.
    for f in [or, FilterExpr::Not(Box::new(name2("fi")))] {
        let brute = archived.cards.iter().enumerate()
            .filter(|(cid, card)| {
                (u32::from(archived.offsets[*cid])..u32::from(archived.offsets[cid + 1]))
                    .any(|p| f.matches(card, &archived.printings[p as usize], &archived.strings))
            })
            .count();
        let mut f2 = f;
        let (total, _) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut f2, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
        );
        assert_eq!(total, brute);
    }

    // Memoize path: a 2-byte needle in a full-scan context becomes NameMatch
    // through the bigram index (exact — no contains() verification).
    let mut f = name2("fi");
    f.memoize_text_predicates(&archived.cards, &archived.strings, &archived.indexes.name_trigram, &archived.indexes.name_bigrams, &archived.indexes.oracle_trigram, archived.cards.len());
    match &f {
        FilterExpr::NameMatch { ids } => assert_eq!(ids.len(), 3),
        _ => panic!("2-byte needle must memoize via bigrams"),
    }
    for (cid, card) in archived.cards.iter().enumerate() {
        let want = card.card_name_lower.as_str().contains("fi");
        assert!((f.eval_card(card, &archived.strings) == Tri::True) == want, "NameMatch parity at card {cid}");
    }
}

// Broad printing-space tag postings behave like broad ranges (#640): scatter
// to a tight bitmap when a consumer exists (broad_ok), decline otherwise —
// never gather tens of thousands of ids raw. Sparse tags keep the vec path.
#[test]
fn broad_tag_postings_scatter_or_decline() {
    let mut vocab = VocabInterner::new();
    let spell = vocab.intern("spell".to_string()).unwrap();
    let rare_tag = vocab.intern("etched".to_string()).unwrap();
    let cards: Vec<OracleCard> = (0..1200u32).map(|i| stub_card(u128::from(i) + 1, TYPE_CREATURE, &[], &mut vocab)).collect();
    let mut data = store_of(cards, &vec![4usize; 1200], vocab); // 4,800 printings
    for (i, p) in data.printings.iter_mut().enumerate() {
        // "spell" on half of all printings (2,400 = 50% > MAX_NARROW_FRACTION);
        // "etched" on 1 in 100 (48, sparse).
        if i % 2 == 0 { p.card_is_tags.push(spell); }
        if i % 100 == 0 { p.card_is_tags.push(rare_tag); }
    }
    data.indexes.is_tags = build_tag_index(&data.printings, &data.coll_vocab, |p| &p.card_is_tags);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let tag = |v: &str| FilterExpr::CollectionCmp { field: CollField::IsTags, op: CmpOp::Ge, value: v.into(), value_id: None };
    let rec = |f: &FilterExpr, broad_ok: bool| super::narrow_rec(f, &archived.indexes, &archived.offsets, &archived.cards, broad_ok);

    // Broad tag: bitmap under broad_ok, decline without.
    assert!(rec(&tag("spell"), false).is_none(), "broad tag without a consumer reverts to the scan");
    let n = rec(&tag("spell"), true).expect("broad tag scatters for a consumer");
    assert!(n.tight, "every posted printing carries the tag");
    match &n.set {
        Candidates::PrintingBits(b) => assert_eq!(b.iter().map(|w| w.count_ones()).sum::<u32>(), 2400),
        _ => panic!("broad tag must be a bitmap"),
    }
    // Sparse tag: vec either way.
    let n = rec(&tag("etched"), false).expect("sparse tag narrows in any context");
    assert!(matches!(n.set, Candidates::Printings(ref v) if v.len() == 48));
    // Absent tag: exact empty.
    let n = rec(&tag("foil"), false).expect("absent tag proves the empty set");
    assert_eq!(n.set.len(), 0);
}

// ─── Devotion bit-sliced planes ───────────────────────────────────────────────

/// Cards with controlled mana costs, hybrids included. A {R/G} pip counts
/// toward BOTH red and green devotion (the loader's hybrid expansion), which
/// the planes must reproduce.
fn devotion_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let costs: &[(&[(&str, u8)], u16)] = &[
        (&[], TYPE_CREATURE),                     // no cost
        (&[("U", 1)], TYPE_CREATURE),              // {U}
        (&[("U", 2)], TYPE_CREATURE),              // {U}{U}
        (&[("U", 3)], TYPE_CREATURE),              // {U}{U}{U}
        (&[("U", 5)], TYPE_CREATURE),              // deep: saturates
        (&[("R/G", 1)], TYPE_CREATURE),            // {R/G}: 1 red AND 1 green
        (&[("R/G", 2), ("R", 1)], TYPE_CREATURE),  // {R/G}{R/G}{R}: R=3, G=2
        (&[("W", 1), ("U", 1)], TYPE_CREATURE),    // {W}{U}
        (&[("C", 2)], TYPE_CREATURE),               // {C}{C}: colorless devotion
        (&[("R", 1)], TYPE_INSTANT),                // {R} on an Instant: never counts (see devotion_ignores_nonpermanent_pips)
    ];
    let mut mana_vocab: Vec<String> = Vec::new();
    let cards: Vec<OracleCard> = costs.iter().enumerate().map(|(i, &(pips, card_types))| {
        let mut c = stub_card(i as u128 + 1, card_types, &[], &mut vocab);
        c.mana_cost = mana_cost_of(pips, &mut mana_vocab);
        // Mirrors mana_cost_from_pydict()'s PERMANENT_TYPES gate (lib.rs):
        // devotion only exists for permanents, regardless of the raw pips.
        if card_types & super::PERMANENT_TYPES == 0 {
            c.mana_cost.devotion = 0;
        }
        // identity must cover devotion colors (the build tripwire enforces it)
        let mut ident = 0u8;
        for (lane, sym) in ["W", "U", "B", "R", "G"].iter().enumerate() {
            if super::lane_get(c.mana_cost.devotion, lane) > 0 {
                ident |= super::color_list_to_mask(&[sym]);
            }
        }
        c.card_color_identity = ident;
        c
    }).collect();
    let mut data = store_of(cards, &[1usize; 10], vocab);
    data.mana_vocab = mana_vocab;
    data
}

/// Build a ManaCost from (symbol, count) pairs the way the loader does:
/// lane symbols pack into core, hybrids expand into devotion and intern into
/// `mana_vocab` (the store's table, shared across cards).
fn mana_cost_of(pips: &[(&str, u8)], mana_vocab: &mut Vec<String>) -> ManaCost {
    let mut core = 0u64;
    let mut devotion = 0u64;
    let mut hybrids: Vec<(u8, u8)> = Vec::new();
    for &(sym, n) in pips {
        match super::mana_lane(sym) {
            Some(lane) => {
                core = super::lane_add(core, lane, n);
                if lane < 6 {
                    devotion = super::lane_add(devotion, lane, n);
                }
            }
            None => {
                let id = mana_vocab.iter().position(|v| v == sym).unwrap_or_else(|| {
                    mana_vocab.push(sym.to_string());
                    mana_vocab.len() - 1
                });
                hybrids.push((id as u8, n));
                for part in sym.split('/') {
                    if let Some(lane) = super::mana_lane(part).filter(|&l| l < 6) {
                        devotion = super::lane_add(devotion, lane, n);
                    }
                }
            }
        }
    }
    hybrids.sort_unstable();
    ManaCost { core, hybrids, devotion, cmc: 0.0 }
}

/// Pack WUBRGC (symbol, count) pairs into devotion lanes for query pips.
fn packed_pips(pips: &[(&str, u8)]) -> u64 {
    let mut p = 0u64;
    for &(sym, n) in pips {
        p = super::lane_add(p, super::mana_lane(sym).expect("lane symbol"), n);
    }
    p
}

// Every devotion op agrees with tri() through the planes, at every saturation
// depth — and past the boundary the compiler declines rather than guesses.
#[test]
fn devotion_plane_parity_and_boundaries() {
    let data = devotion_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let dev = |op, pips: &[(&str, u8)]| FilterExpr::Devotion { op, pips: packed_pips(pips) };
    let mut bitmap: Vec<u64> = Vec::new();
    let mut check_exact = |f: &FilterExpr| {
        let pe = compile_plane(f, &archived.indexes.planes, &archived.indexes.oracle_trigram.words).expect("must compile exactly");
        eval_planes(&pe, &archived.indexes.planes, &mut bitmap);
        for (cid, card) in archived.cards.iter().enumerate() {
            let want = f.eval_card(card, &archived.strings) == Tri::True;
            assert_eq!(bitmap_contains(&bitmap, cid as u32), want, "devotion parity at card {cid}");
        }
    };
    for op in [CmpOp::Ge, CmpOp::Eq, CmpOp::Le, CmpOp::Ne, CmpOp::Gt, CmpOp::Lt] {
        for k in 1..=2u8 {
            check_exact(&dev(op, &[("U", k)]));
        }
    }
    check_exact(&dev(CmpOp::Ge, &[("U", 3)])); // saturated value 3 means >= 3: exact
    check_exact(&dev(CmpOp::Ge, &[("R", 1), ("G", 1)])); // multi-color, hybrid cards in play
    check_exact(&dev(CmpOp::Ge, &[("C", 2)])); // colorless devotion
    check_exact(&dev(CmpOp::Ge, &[("R", 3)])); // {R/G}{R/G}{R} card reaches R=3

    // Past the saturation boundary the exact compiler declines...
    assert!(compile_plane(&dev(CmpOp::Ge, &[("U", 4)]), &archived.indexes.planes, &archived.indexes.oracle_trigram.words).is_none());
    assert!(compile_plane(&dev(CmpOp::Eq, &[("U", 3)]), &archived.indexes.planes, &archived.indexes.oracle_trigram.words).is_none());
    assert!(compile_plane(&dev(CmpOp::Le, &[("U", 3)]), &archived.indexes.planes, &archived.indexes.oracle_trigram.words).is_none());
    // ...and the saturated superset covers every deep match for narrowing.
    let deep = dev(CmpOp::Ge, &[("U", 5)]);
    let n = super::narrow_rec(&deep, &archived.indexes, &archived.offsets, &archived.cards, false).expect("deep-k narrows loosely");
    assert!(!n.tight);
    let cand = n.set.into_cards(&archived.offsets);
    for (cid, card) in archived.cards.iter().enumerate() {
        if deep.eval_card(card, &archived.strings) == Tri::True {
            assert!(cand.contains(&(cid as u32)), "superset must cover card {cid}");
        }
    }
}

// The user-specified hybrid invariant, pinned: a card costing {R/G} has one
// red devotion AND one green devotion.
#[test]
fn hybrid_pip_counts_toward_both_colors() {
    let data = devotion_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let dev = |sym: &str, k: u8| FilterExpr::Devotion { op: CmpOp::Ge, pips: packed_pips(&[(sym, k)]) };
    let mut bitmap: Vec<u64> = Vec::new();
    let rg_card = 5; // {R/G}
    for (f, want) in [(dev("R", 1), true), (dev("G", 1), true), (dev("R", 2), false), (dev("U", 1), false)] {
        eval_planes(&compile_plane(&f, &archived.indexes.planes, &archived.indexes.oracle_trigram.words).unwrap(), &archived.indexes.planes, &mut bitmap);
        assert_eq!(bitmap_contains(&bitmap, rg_card), want);
    }
}

// Devotion (MTG comprehensive rules) is defined only over permanents' mana
// costs, confirmed against the real Scryfall API (devotion: never matches a
// pure Instant/Sorcery, e.g. the real Lightning Bolt). A colored pip on a
// nonpermanent must contribute zero devotion, no matter the operator.
#[test]
fn devotion_ignores_nonpermanent_pips() {
    let data = devotion_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let instant_card = &archived.cards[9]; // {R} on TYPE_INSTANT
    assert_eq!(u64::from(instant_card.mana_cost.devotion), 0, "an Instant's devotion must be zeroed at load, regardless of its pips");
    let ge_r = FilterExpr::Devotion { op: CmpOp::Ge, pips: packed_pips(&[("R", 1)]) };
    let le_r = FilterExpr::Devotion { op: CmpOp::Le, pips: packed_pips(&[("R", 1)]) };
    assert!(ge_r.eval_card(instant_card, &archived.strings) != Tri::True, "a nonpermanent's {{R}} must not satisfy devotion:{{R}}");
    assert!(le_r.eval_card(instant_card, &archived.strings) == Tri::True, "empty devotion is a subset of any query");
}

// ─── Sorted name index: order:name + exact-name narrowing ────────────────────

/// Six single-printing cards with names out of store order, one duplicated
/// ("sol ring" twice), ranks assigned and sort permutations built exactly as
/// the real load path does. Sorted name order: atog, black lotus, cancel,
/// fog, sol ring, sol ring — with the duplicate pair tied on name_rank and
/// separated by their edhrec ranks.
fn named_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let names = ["fog", "sol ring", "atog", "sol ring", "black lotus", "cancel"];
    let mut cards: Vec<OracleCard> = names
        .iter()
        .enumerate()
        .map(|(i, name)| {
            let mut c = stub_card((i + 1) as u128, TYPE_CREATURE, &[], &mut vocab);
            c.card_name_lower = InlineStr::from_str(name);
            // Distinct, deliberately store-order-scrambled edhrec ranks: the
            // second "sol ring" (card 3) outranks the first (card 1).
            c.edhrec_rank = Some([40, 60, 10, 20, 30, 50][i]);
            c
        })
        .collect();
    assign_name_ranks(&mut cards);
    let mut data = store_of(cards, &[1; 6], vocab);
    data.indexes.sort_perms = build_sort_permutations(&data.cards, &data.printings, &data.offsets);
    data
}

// Equal names share a dense rank; distinct names rank in byte order.
#[test]
fn name_ranks_dense_and_shared_across_duplicates() {
    let data = named_store();
    let ranks: Vec<u32> = data.cards.iter().map(|c| c.name_rank).collect();
    // fog=3, sol ring=4 (both copies), atog=0, black lotus=1, cancel=2
    assert_eq!(ranks, vec![3, 4, 0, 4, 1, 2]);
}

// ExactName narrows to the exact, tight card set through the ascending name
// permutation: hit (single), hit (duplicate pair), boundary names, and a miss
// proving the empty set.
#[test]
fn exact_name_narrows_tight() {
    let data = named_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let exact = |name: &str| FilterExpr::ExactName(name.to_string());
    let narrow = |name: &str| {
        super::narrow_rec(&exact(name), &archived.indexes, &archived.offsets, &archived.cards, false)
            .expect("exact name must narrow")
    };

    for (name, mut want) in [
        ("fog", vec![0u32]),
        ("sol ring", vec![1, 3]),
        ("atog", vec![2]),          // first in sorted order
        ("cancel", vec![5]),        // adjacent to the last block
        ("zzz past the end", vec![]),
        ("aaa before the start", vec![]),
        ("sol rin", vec![]),        // prefix of a real name is still a miss
    ] {
        let n = narrow(name);
        assert!(n.tight, "{name}: equality through the sorted permutation is exact");
        let mut got = n.set.into_cards(&archived.offsets);
        got.sort_unstable();
        want.sort_unstable();
        assert_eq!(got, want, "candidates for {name:?}");
    }

    // Composition: the tight set participates in the candidate algebra, and
    // run_query totals agree with a full scan on every shape.
    let mut shapes: Vec<FilterExpr> = vec![
        exact("sol ring"),
        exact("no such card"),
        FilterExpr::Or(vec![exact("sol ring"), FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge }]),
        FilterExpr::And(vec![exact("fog"), FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge }]),
        FilterExpr::Not(Box::new(exact("sol ring"))),
    ];
    for (i, f) in shapes.iter_mut().enumerate() {
        let brute = archived
            .cards
            .iter()
            .filter(|c| f.eval_card(c, &archived.strings) == Tri::True)
            .count();
        let (total, _) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            f, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
        );
        assert_eq!(total, brute, "totals parity for shape {i}");
    }
}

// order:name sorts pages by name in both directions, breaks the duplicate-name
// tie by edhrec rank in BOTH directions (direction folds into the primary key
// only), and paginates consistently.
#[test]
fn order_name_sorts_and_paginates() {
    let data = named_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let run = |direction: &str, limit: usize, offset: usize| -> Vec<String> {
        let mut all = FilterExpr::True;
        let (total, page) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut all, None, "card", "default", "name", direction, limit, offset, &archived.indexes,
        );
        assert_eq!(total, 6);
        page.iter().map(|(c, _)| c.card_name_lower.as_str().to_string()).collect()
    };

    assert_eq!(run("asc", 100, 0), ["atog", "black lotus", "cancel", "fog", "sol ring", "sol ring"]);
    assert_eq!(run("desc", 100, 0), ["sol ring", "sol ring", "fog", "cancel", "black lotus", "atog"]);
    // Pagination: the page [2, 4) of the ascending order.
    assert_eq!(run("asc", 2, 2), ["cancel", "fog"]);

    // The duplicate pair ties on name and must break by edhrec rank ascending
    // in both directions: card 3 (rank 20) before card 0 (rank 60).
    let ids = |direction: &str| -> Vec<u128> {
        let mut all = FilterExpr::True;
        let (_, page) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut all, None, "card", "default", "name", direction, 100, 0, &archived.indexes,
        );
        page.iter()
            .filter(|(c, _)| c.card_name_lower.as_str() == "sol ring")
            .map(|(c, _)| u128::from(c.oracle_id))
            .collect()
    };
    assert_eq!(ids("asc"), [4, 2], "within the tie: lower edhrec rank first");
    assert_eq!(ids("desc"), [4, 2], "secondaries keep their order under desc");
}

// ─── Verifier cost ordering ───────────────────────────────────────────────────

fn type_mask() -> FilterExpr {
    FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge }
}

fn contains_scan() -> FilterExpr {
    FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: "draw".to_string() }
}

fn machinery_regex() -> FilterExpr {
    FilterExpr::TextRegex { field: TextField::OracleTextLower, regex: regex::Regex::new("draw .* cards?").unwrap() }
}

// Pattern-shape cost classification: anchored literals are memcmp-cheap
// (SET_LOOKUP_NS100); everything else — bare literals included, measured the
// same cost as real machinery, not a text scan (bench_verify_cost.rs) —
// shares REGEX_MACHINERY_NS100.
#[test]
fn regex_tier_classifies_pattern_shapes() {
    use super::{regex_tier, REGEX_MACHINERY_NS100, SET_LOOKUP_NS100};
    assert_eq!(regex_tier("(?i)^flying$"), SET_LOOKUP_NS100);
    assert_eq!(regex_tier("dragon$"), SET_LOOKUP_NS100);
    assert_eq!(regex_tier("(?i)^\\{t\\}: add"), SET_LOOKUP_NS100, "escaped punctuation is literal");
    assert_eq!(regex_tier("^gob"), SET_LOOKUP_NS100);
    assert_eq!(regex_tier("(?i)flying"), REGEX_MACHINERY_NS100, "unanchored literal measures the same as machinery");
    assert_eq!(regex_tier("draw .* cards?"), REGEX_MACHINERY_NS100);
    assert_eq!(regex_tier("^[aeiou]"), REGEX_MACHINERY_NS100);
    assert_eq!(regex_tier("(?i)^\\d+$"), REGEX_MACHINERY_NS100, "class escapes are machinery");
    assert_eq!(regex_tier("a|b"), REGEX_MACHINERY_NS100);
    assert_eq!(regex_tier("ends with backslash\\"), REGEX_MACHINERY_NS100, "dangling escape: not literal");
}

// And children reorder cheapest-tier-first regardless of written order, and
// equal-tier children keep their written order (stable sort).
#[test]
fn verify_order_sorts_and_children_cheap_first() {
    let cmc_lt = |v: f64| FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::Cmc),
        op: CmpOp::Lt,
        rhs: NumExpr::Const(v),
    };
    let mut f = FilterExpr::And(vec![machinery_regex(), cmc_lt(3.0), contains_scan(), cmc_lt(5.0), type_mask()]);
    f.order_children_by_verify_cost();
    let FilterExpr::And(children) = &f else { panic!("still an And") };
    assert!(matches!(children[0], FilterExpr::NumericCmp { rhs: NumExpr::Const(v), .. } if v == 3.0));
    assert!(matches!(children[1], FilterExpr::NumericCmp { rhs: NumExpr::Const(v), .. } if v == 5.0));
    assert!(matches!(children[2], FilterExpr::TypeCmp { .. }));
    assert!(matches!(children[3], FilterExpr::TextContains { .. }));
    assert!(matches!(children[4], FilterExpr::TextRegex { .. }));
}

// Within the memoized-set tier, And children refine to ascending set size
// (smaller set = more rejections per identical binary-search cost); tier-1
// children without a known size (collections) come after the sized sets.
#[test]
fn verify_order_and_refines_by_set_size() {
    let coll = FilterExpr::CollectionCmp { field: CollField::Keywords, op: CmpOp::Ge, value: "flying".to_string(), value_id: None };
    let mut f = FilterExpr::And(vec![
        coll,
        FilterExpr::NameMatch { ids: vec![1, 2, 3, 4, 5] },
        FilterExpr::OracleMatch { gids: vec![7, 9] },
    ]);
    f.order_children_by_verify_cost();
    let FilterExpr::And(children) = &f else { panic!("still an And") };
    assert!(matches!(&children[0], FilterExpr::OracleMatch { gids } if gids.len() == 2));
    assert!(matches!(&children[1], FilterExpr::NameMatch { ids } if ids.len() == 5));
    assert!(matches!(children[2], FilterExpr::CollectionCmp { .. }));
}

// Or children sort by coarse buckets: acceptance short-circuits an Or, and a
// small set is the worst acceptance lead, so neither set size nor fine cost
// tiers may reorder Or children — only decisive gaps do.
#[test]
fn verify_order_or_sorts_by_bucket_only() {
    let mut f = FilterExpr::Or(vec![
        machinery_regex(),
        FilterExpr::NameMatch { ids: vec![1, 2, 3, 4, 5] },
        FilterExpr::OracleMatch { gids: vec![7, 9] },
        type_mask(),
    ]);
    f.order_children_by_verify_cost();
    let FilterExpr::Or(children) = &f else { panic!("still an Or") };
    assert!(matches!(children[0], FilterExpr::TypeCmp { .. }));
    assert!(matches!(&children[1], FilterExpr::NameMatch { ids } if ids.len() == 5), "written order kept within tier");
    assert!(matches!(&children[2], FilterExpr::OracleMatch { gids } if gids.len() == 2));
    assert!(matches!(children[3], FilterExpr::TextRegex { .. }));
}

// Within an Or, nodes of text-scan-adjacent cost (pip maps, contains, bare
// literals) keep written order: their cost gap is too small to outweigh the
// unknowable acceptance rates (devotion-first cost `oracle:vigilance or
// devotion:bbb` 1.2× when measured).
#[test]
fn verify_order_or_keeps_scan_cost_band_in_written_order() {
    let devotion = || FilterExpr::Devotion { op: CmpOp::Ge, pips: packed_pips(&[("B", 3)]) };
    let mut f = FilterExpr::Or(vec![contains_scan(), devotion()]);
    f.order_children_by_verify_cost();
    let FilterExpr::Or(children) = &f else { panic!("still an Or") };
    assert!(matches!(children[0], FilterExpr::TextContains { .. }), "contains keeps its written lead");
    assert!(matches!(children[1], FilterExpr::Devotion { .. }));

    // In an And the same pair DOES reorder: rejection is what And children
    // short-circuit on, and the pip walk measures ~3× under the text scan.
    let mut g = FilterExpr::And(vec![contains_scan(), devotion()]);
    g.order_children_by_verify_cost();
    let FilterExpr::And(children) = &g else { panic!("still an And") };
    assert!(matches!(children[0], FilterExpr::Devotion { .. }), "And sorts the cheaper pip walk first");
    assert!(matches!(children[1], FilterExpr::TextContains { .. }));
}

// Within an Or, a memoized set must NOT jump ahead of a contains: the ~3×
// cost gap between a binary search and a substring scan is smaller than the
// acceptance-rate swing it gambles on (measured: `(… or color:g)
// (oracle:token or name:storm)` lost 1.1× to set-first ordering).
#[test]
fn verify_order_or_keeps_sets_and_scans_in_written_order() {
    let mut f = FilterExpr::Or(vec![contains_scan(), FilterExpr::NameMatch { ids: vec![1, 2] }]);
    f.order_children_by_verify_cost();
    let FilterExpr::Or(children) = &f else { panic!("still an Or") };
    assert!(matches!(children[0], FilterExpr::TextContains { .. }));
    assert!(matches!(children[1], FilterExpr::NameMatch { .. }));
}

// And children that can reject at card level run before printing-dependent
// ones, which never can: `usd>20 t:dragon` must check the subtype first so
// rejected cards skip the price eval entirely. A composite with any
// card-level member can still settle at card level, so it stays early.
#[test]
fn verify_order_and_defers_printing_dependent_children() {
    let usd = || FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::PriceUsd),
        op: CmpOp::Gt,
        rhs: NumExpr::Const(20.0),
    };
    let dragon = || FilterExpr::CollectionCmp { field: CollField::Subtypes, op: CmpOp::Ge, value: "dragon".to_string(), value_id: None };
    let mut f = FilterExpr::And(vec![usd(), dragon()]);
    f.order_children_by_verify_cost();
    let FilterExpr::And(children) = &f else { panic!("still an And") };
    assert!(matches!(children[0], FilterExpr::CollectionCmp { .. }), "card-level rejector first");
    assert!(matches!(children[1], FilterExpr::NumericCmp { .. }));

    // Or(usd, type) can settle at card level through its type member (a True
    // settles an Or), so it is not printing-dependent and leads the contains.
    let mut g = FilterExpr::And(vec![contains_scan(), FilterExpr::Or(vec![usd(), type_mask()])]);
    g.order_children_by_verify_cost();
    let FilterExpr::And(children) = &g else { panic!("still an And") };
    assert!(matches!(children[0], FilterExpr::Or(_)), "mixed composite can settle: stays card-level");
    assert!(matches!(children[1], FilterExpr::TextContains { .. }));
}

// Printing-dependent Or children can never settle the Or during the card
// pass, so a cheap tier never pulls them ahead of card-level children —
// `usd>20` must not jump ahead of the contains it can't short-circuit.
#[test]
fn verify_order_or_defers_printing_dependent_children() {
    let usd = || FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::PriceUsd),
        op: CmpOp::Gt,
        rhs: NumExpr::Const(20.0),
    };
    let mut f = FilterExpr::Or(vec![contains_scan(), usd()]);
    f.order_children_by_verify_cost();
    let FilterExpr::Or(children) = &f else { panic!("still an Or") };
    assert!(matches!(children[0], FilterExpr::TextContains { .. }), "card-level scan stays ahead of pdep numeric");
    assert!(matches!(children[1], FilterExpr::NumericCmp { .. }));

    // A card-level mask still moves ahead of a printing-dependent set lookup.
    let mut g = FilterExpr::Or(vec![FilterExpr::FlavorMatch { gids: vec![3], dense_ids: vec![] }, type_mask()]);
    g.order_children_by_verify_cost();
    let FilterExpr::Or(children) = &g else { panic!("still an Or") };
    assert!(matches!(children[0], FilterExpr::TypeCmp { .. }));
    assert!(matches!(children[1], FilterExpr::FlavorMatch { .. }));
}

// Composites rank as the max of their children (their evaluation may have to
// run every child), and the sort recurses through And/Or/Not nesting.
#[test]
fn verify_order_recurses_and_ranks_composites() {
    let inner_or = FilterExpr::Or(vec![machinery_regex(), type_mask()]);
    let mut f = FilterExpr::Not(Box::new(FilterExpr::And(vec![inner_or, contains_scan(), type_mask()])));
    f.order_children_by_verify_cost();
    let FilterExpr::Not(inner) = &f else { panic!("still a Not") };
    let FilterExpr::And(children) = inner.as_ref() else { panic!("still an And") };
    assert!(matches!(children[0], FilterExpr::TypeCmp { .. }));
    assert!(matches!(children[1], FilterExpr::TextContains { .. }));
    let FilterExpr::Or(or_children) = &children[2] else { panic!("Or ranks tier 3, last") };
    assert!(matches!(or_children[0], FilterExpr::TypeCmp { .. }), "nested Or sorted too");
    assert!(matches!(or_children[1], FilterExpr::TextRegex { .. }));
}

// End-to-end: the two spellings of a mixed-cost conjunction return identical
// totals and pages through run_query — ordering is a speed dial, not a
// semantics change.
#[test]
fn verify_order_spellings_agree_end_to_end() {
    let mut vocab = VocabInterner::new();
    let mut strings = Interner::new();
    let mut cards = Vec::new();
    for i in 0..12u32 {
        let types = if i % 2 == 0 { TYPE_CREATURE } else { TYPE_INSTANT };
        let mut c = stub_card((i + 1) as u128, types, &[], &mut vocab);
        let text = if i % 3 == 0 { "flying" } else { "draw a card" };
        c.oracle_text_lower_id = strings.intern(text.to_string());
        c.edhrec_rank = Some(i + 1);
        cards.push(c);
    }
    let mut data = store_of(cards, &vec![2usize; 12], vocab);
    data.strings = strings.strings;
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let run = |mut filter: FilterExpr| {
        let (total, page) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut filter, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
        );
        let ids: Vec<u128> = page.iter().map(|(c, _)| u128::from(c.oracle_id)).collect();
        (total, ids)
    };
    let expensive_first = FilterExpr::And(vec![machinery_regex(), type_mask()]);
    let cheap_first = FilterExpr::And(vec![type_mask(), machinery_regex()]);
    let (t1, p1) = run(expensive_first);
    let (t2, p2) = run(cheap_first);
    assert_eq!(t1, 4, "creatures with draw-a-card text: cards 3, 5, 9, 11");
    assert_eq!((t1, p1), (t2, p2));

    let or_a = FilterExpr::Or(vec![machinery_regex(), type_mask()]);
    let or_b = FilterExpr::Or(vec![type_mask(), machinery_regex()]);
    assert_eq!(run(or_a), run(or_b));
}

// ─── Query-string mana parsing: mana_pip_counts / mana_cmc ───────────────────

// X is its own pip symbol (its own lane, see MANA_LANE_SYMS), not a hybrid and
// not excluded — only its cmc contribution is 0, handled by mana_cmc
// separately. Confirmed against the real Scryfall API: mana:{X} matches
// Fireball ({X}{R}) and mana:x behaves identically to mana:{x}. This parser
// (used only for query-time mana:/devotion: values — see build_binary's
// attr == "mana_cost_jsonb"/"devotion" arms) once dropped X entirely, braced
// or not, silently degrading `mana:{X}{R}` into `mana:{R}` and matching cards
// with no X pip at all (e.g. Shivan Dragon, {4}{R}{R}).
#[test]
fn mana_pip_counts_treats_x_as_a_real_symbol() {
    use super::mana_pip_counts;
    let braced = mana_pip_counts("{X}{R}");
    assert_eq!(braced.get("X"), Some(&1), "braced X must not be dropped");
    assert_eq!(braced.get("R"), Some(&1));
    let bare = mana_pip_counts("XR");
    assert_eq!(bare.get("X"), Some(&1), "bare X must be recognized, same as braced");
    assert_eq!(bare.get("R"), Some(&1));
    let doubled = mana_pip_counts("{X}{X}{R}");
    assert_eq!(doubled.get("X"), Some(&2), "repeated X pips must accumulate");
}

// mana_cmc's X-exclusion was already correct (X contributes 0 whether braced
// or bare) — pinned here so a future refactor of mana_pip_counts can't
// accidentally couple the two functions' X handling back together.
#[test]
fn mana_cmc_excludes_x_braced_and_bare() {
    use super::mana_cmc;
    assert_eq!(mana_cmc("{X}{R}"), 1.0);
    assert_eq!(mana_cmc("XR"), 1.0);
    assert_eq!(mana_cmc("{X}{X}{2}{R}"), 3.0, "two generic + one R; both Xs contribute 0");
}

// ─── Packed mana pips: ManaCostCmp semantics ─────────────────────────────────

/// Store with a spread of cost shapes: plain, multi-pip, hybrid, X, snow,
/// empty — enough to exercise every ManaCostCmp op against the packed lanes,
/// the hybrid overflow vec, and the bind path.
fn mana_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let mut mana_vocab: Vec<String> = Vec::new();
    // (pips, cmc): oracle ids 1..=8 in this order
    let costs: &[(&[(&str, u8)], f32)] = &[
        (&[("W", 1)], 1.0),               // 1: {W}
        (&[("W", 2)], 2.0),               // 2: {W}{W}
        (&[("W", 1), ("U", 1)], 2.0),     // 3: {W}{U}
        (&[("R/G", 1)], 1.0),             // 4: {R/G}
        (&[("X", 1), ("R", 1)], 1.0),     // 5: {X}{R}
        (&[("S", 1), ("G", 1)], 2.0),     // 6: {S}{G}
        (&[], 0.0),                       // 7: no cost
        (&[("W/P", 1)], 1.0),             // 8: {W/P} (Phyrexian — an opaque hybrid key, same as {R/G})
    ];
    let cards: Vec<OracleCard> = costs.iter().enumerate().map(|(i, &(pips, cmc))| {
        let mut c = stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab);
        c.mana_cost = mana_cost_of(pips, &mut mana_vocab);
        c.mana_cost.cmc = cmc;
        // identity must cover devotion colors (the build tripwire enforces it)
        for (lane, sym) in ["W", "U", "B", "R", "G"].iter().enumerate() {
            if super::lane_get(c.mana_cost.devotion, lane) > 0 {
                c.card_color_identity |= super::color_list_to_mask(&[sym]);
            }
        }
        c
    }).collect();
    let mut data = store_of(cards, &[1usize; 8], vocab);
    data.mana_vocab = mana_vocab;
    data
}

/// Build a bound ManaCostCmp the way build_binary + bind would: lane symbols
/// pack into core, hybrid symbols resolve against the store's vocab (or the
/// reserved unknown id when absent).
fn mana_cmp_of(op: CmpOp, pips: &[(&str, u8)], cmc: f32, mana_vocab: &[String]) -> FilterExpr {
    let mut core = 0u64;
    let mut hybrids: Vec<(String, u8)> = Vec::new();
    let mut hybrid_ids: Vec<(u8, u8)> = Vec::new();
    let mut unknown = 0u8;
    for &(sym, n) in pips {
        match super::mana_lane(sym) {
            Some(lane) => core = super::lane_add(core, lane, n),
            None => {
                hybrids.push((sym.to_string(), n));
                match mana_vocab.iter().position(|v| v == sym) {
                    Some(i) => hybrid_ids.push((i as u8, n)),
                    None => unknown = unknown.saturating_add(n),
                }
            }
        }
    }
    hybrids.sort_unstable();
    hybrid_ids.sort_unstable();
    if unknown > 0 {
        hybrid_ids.push((super::MANA_SYM_UNKNOWN, unknown));
    }
    FilterExpr::ManaCostCmp { op, core, hybrids, hybrid_ids, cmc }
}

// Containment, exactness, and their strict/negated variants over the packed
// representation, matching the jsonb multiset semantics they replace: `=`
// needs the same distinct symbols with the same counts (a zero lane IS an
// absent key), hybrids are their own symbols (never lane-expanded for
// mana=), and X on a card blocks exactness against an X-less query.
#[test]
fn mana_cost_cmp_packed_semantics() {
    let data = mana_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let matches = |f: &FilterExpr| -> Vec<u128> {
        archived.cards.iter()
            .filter(|c| f.eval_card(c, &archived.strings) == Tri::True)
            .map(|c| u128::from(c.oracle_id))
            .collect()
    };
    let mv: Vec<String> = archived.mana_vocab.iter().map(|s| s.to_string()).collect();

    // Ge = query ⊆ card (and cmc >=): every white cost with cmc >= 1.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ge, &[("W", 1)], 1.0, &mv)), [1, 2, 3]);
    // Eq: same symbols, same counts, same cmc — {W} only, not {W}{W} or {W}{U}.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Eq, &[("W", 1)], 1.0, &mv)), [1]);
    // Le = card ⊆ query: {W}, {W}{W}, and the empty cost.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Le, &[("W", 2)], 2.0, &mv)), [1, 2, 7]);
    // Strict variants exclude the exact cost.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Gt, &[("W", 1)], 1.0, &mv)), [2, 3]);
    assert_eq!(matches(&mana_cmp_of(CmpOp::Lt, &[("W", 2)], 2.0, &mv)), [1, 7]);
    // Hybrid pips are distinct symbols: {R/G} matches only through the vocab.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ge, &[("R/G", 1)], 1.0, &mv)), [4]);
    assert_eq!(matches(&mana_cmp_of(CmpOp::Eq, &[("R/G", 1)], 1.0, &mv)), [4]);
    // ...and {R} does not contain {R/G}, nor does the {X}{R} card equal {R}.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ge, &[("R", 1)], 1.0, &mv)), [5]);
    assert_eq!(matches(&mana_cmp_of(CmpOp::Eq, &[("R", 1)], 1.0, &mv)), Vec::<u128>::new());
    // Phyrexian mana is just another opaque hybrid symbol for mana: — {W/P}
    // matches only through the vocab, and {W} does not contain it (mirrors
    // {R/G} above; the "Phyrexian still counts toward W devotion" invariant
    // is pinned in api/tests/test_engine_unit.py and test_data.sql instead,
    // since this fixture only exercises ManaCostCmp, not Devotion).
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ge, &[("W/P", 1)], 1.0, &mv)), [8]);
    assert_eq!(matches(&mana_cmp_of(CmpOp::Eq, &[("W/P", 1)], 1.0, &mv)), [8]);
    // Snow is a lane like any other.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ge, &[("S", 1)], 1.0, &mv)), [6]);
    // X is a lane like any other too, not a hybrid — only card 5 carries it.
    // (This exercises the FilterExpr evaluator's already-correct lane
    // handling; the query-string parser's X-dropping bug is pinned directly
    // in mana_pip_counts_treats_x_as_a_real_symbol above, since mana_cmp_of
    // builds the FilterExpr from typed pips and bypasses string parsing.)
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ge, &[("X", 1)], 0.0, &mv)), [5]);
    assert_eq!(matches(&mana_cmp_of(CmpOp::Eq, &[("X", 1), ("R", 1)], 1.0, &mv)), [5]);
    // A symbol no card carries: containment and exactness fail everywhere;
    // card ⊆ query still holds for the empty cost (query-only symbols never
    // constrain the subset direction — same as the HashMap semantics).
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ge, &[("Q/Z", 1)], 1.0, &mv)), Vec::<u128>::new());
    assert_eq!(matches(&mana_cmp_of(CmpOp::Eq, &[("Q/Z", 1)], 1.0, &mv)), Vec::<u128>::new());
    assert_eq!(matches(&mana_cmp_of(CmpOp::Le, &[("Q/Z", 1)], 2.0, &mv)), [7]);
    // Ne is not-exactly-equal.
    assert_eq!(matches(&mana_cmp_of(CmpOp::Ne, &[("W", 1)], 1.0, &mv)), [2, 3, 4, 5, 6, 7, 8]);
}

// The SWAR lane comparison agrees with the scalar per-lane loop across the
// value spectrum, including the saturation cap that keeps it sound.
#[test]
fn lanes_ge_matches_scalar_compare() {
    let vals = [0u8, 1, 2, 3, 5, 126, 127];
    for &a0 in &vals {
        for &b0 in &vals {
            for &a5 in &vals {
                for &b5 in &vals {
                    let a = super::lane_add(super::lane_add(0, 0, a0), 5, a5);
                    let b = super::lane_add(super::lane_add(0, 0, b0), 5, b5);
                    let want = a0 >= b0 && a5 >= b5;
                    assert_eq!(super::lanes_ge(a, b, super::LANES6_HI), want, "a0={a0} b0={b0} a5={a5} b5={b5}");
                }
            }
        }
    }
    // lane_add saturates below the lane's high bit, so borrows can't escape.
    let big = super::lane_add(super::lane_add(0, 2, 120), 2, 120);
    assert_eq!(super::lane_get(big, 2), 127);
    assert_eq!(super::lane_get(big, 1), 0);
    assert_eq!(super::lane_get(big, 3), 0);
}
