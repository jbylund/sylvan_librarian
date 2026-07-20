use super::{
    assign_name_ranks,
    build_numeric_index, build_oracle_text_index, build_tag_index, build_trigram_index,
    build_rarity_index, build_flavor_index, build_thresholded_tag_index, build_sort_permutations,
    assign_artwork_groups, build_bit_planes, build_divergent_ids, build_name_bigram_index, build_printing_to_card, flavor_fingerprint, flavor_match_sets,
    cards_of_printings, count_common_keywords, count_common_types,
    build_artist_index, build_range_index, range_candidates, narrow_candidates, rarity_candidates,
    range_too_broad_to_narrow, run_query, run_query_with_plan, PhysicalPlan, trigram_candidates, finalize_trigram_index, PrintingRangeIndex, NARROW_FLOOR,
    walk_printing_page, aligned_page, bare_range_bounds, printing_range_fastpath, sort_key_bits, orderby_to_col, SortCol, STREAM_MIN_MATCHES,
    archive_header, archive_payload, ARCHIVE_HEADER_LEN, Mmap,
    bitmap_contains, bitmap_card_ids, compile_plane, eval_planes, split_planes,
    ArithOp, ArtistIndex, CardData, CardIndexes, Candidates, ColorField, NumExpr, NumField, RarityIndex,
    CollField, CmpOp, FilterExpr, InlineStr, Interner, ManaCost, OracleCard, Printing, TagIndex,
    TextField, TextSearchField, Tri, SortedTrigramIndex, VocabInterner, ARTIST_NONE, NONE_STR, TYPE_ARTIFACT, TYPE_CREATURE,
    TYPE_ENCHANTMENT, TYPE_INSTANT, TYPE_LAND, TYPE_LEGENDARY, TYPE_PLANESWALKER, TYPE_SNOW, TYPE_SORCERY,
};
use rkyv::{rancor::Error, Archived};
use std::collections::HashMap;
use std::sync::OnceLock;
// Trait bringing random_range/random_bool/random into scope for the #677 fuzzer
// helpers below (SmallRng's inherent methods live on this extension trait).
use rand::RngExt;

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
        artwork_group_id: 0, // placeholder; store_of overwrites via assign_artwork_groups
    }
}

/// A `usd <op> dollars` comparison. `field_num` divides cents to dollars
/// unconditionally regardless of shape, so this is just a plain `Const` in
/// dollars -- no `bind()` step or unit conversion required.
fn usd_cmp(op: CmpOp, dollars: f64) -> FilterExpr {
    FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::PriceUsd), op, rhs: NumExpr::Const(dollars) }
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
    // Every printing above got a distinct illustration_id (next_id strictly
    // increases), so this assigns sequential, all-distinct artwork_group_ids by
    // default -- fixtures that want shared artwork overwrite illustration_id
    // after store_of returns and must recompute via assign_artwork_groups
    // themselves (see artwork_group_counts_dedup_illustrations).
    let artwork_groups = assign_artwork_groups(&mut printings, &offsets);
    // Real planes and bigrams so narrowing tests see the same store shape
    // reload_commit builds (type narrowing goes through the planes since #637).
    let indexes = CardIndexes {
        // No printing sets card_border_id away from NONE_STR at this point (any
        // border values a fixture wants get set after store_of returns, same as
        // border_planes_fixture_store already does), so an empty string table is
        // safe here -- the border-scatter loop skips every printing regardless.
        planes: build_bit_planes(&cards, &printings, &offsets, &[]),
        name_bigrams: build_name_bigram_index(&cards),
        legal_divergent: build_divergent_ids(&cards),
        sort_perms: build_sort_permutations(&cards, &printings, &offsets),
        artwork_groups,
        printing_to_card: build_printing_to_card(&offsets),
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

    // offsets for 2 cards with 2 printings each: printings 0-1 → card 0, 2-3 → card 1
    let raw_offsets = vec![0u32, 2, 4];
    let printing_to_card = build_printing_to_card(&raw_offsets);
    let indexes = CardIndexes { art_tags, keywords, printing_to_card, ..Default::default() };
    let bytes = rkyv::to_bytes::<Error>(&indexes).expect("serialize");
    let archived = rkyv::access::<Archived<CardIndexes>, Error>(&bytes).expect("access");
    let offsets_bytes = rkyv::to_bytes::<Error>(&raw_offsets).expect("serialize offsets");
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

/// Card i's printing rarities (docs/issues/00670-engine-rarity-planes.md): one value
/// per printing, None for no-rarity. Covers all 6 buckets, cards spanning two
/// buckets at once (mixed within the planed range, and mixed across the
/// plane/postings-tail boundary), and a card with no rarity anywhere.
const RARITY_PLANE_FIXTURE: &[&[Option<u8>]] = &[
    &[Some(0)],         // card 0: common only
    &[Some(1)],         // card 1: uncommon only
    &[Some(2)],         // card 2: rare only
    &[Some(3)],         // card 3: mythic only
    &[Some(4)],         // card 4: special only
    &[Some(5)],         // card 5: bonus only
    &[Some(0), Some(3)], // card 6: common + mythic (mixed, both planed)
    &[Some(2), Some(4)], // card 7: rare + special (mixed, spans plane/tail)
    &[None],             // card 8: no rarity anywhere
];

fn rarity_plane_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let cards: Vec<OracleCard> = (0..RARITY_PLANE_FIXTURE.len())
        .map(|i| stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab))
        .collect();
    let counts: Vec<usize> = RARITY_PLANE_FIXTURE.iter().map(|r| r.len()).collect();
    let mut data = store_of(cards, &counts, vocab);
    let mut p_idx = 0;
    for rarities in RARITY_PLANE_FIXTURE {
        for &r in *rarities {
            data.printings[p_idx].card_rarity_int = r;
            p_idx += 1;
        }
    }
    data.indexes.rarity = build_rarity_index(&data.printings, &data.offsets);
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data
}

/// True for the (op, val) pairs where the shared "above mythic" plane can't
/// resolve the comparison exactly -- val is 4 (special) or 5 (bonus)
/// specifically, and the op needs to tell them apart (docs/issues/
/// 00680-engine-existential-plane-generalization.md, #680's "K exact + 1 shared
/// hi bucket" design, following the same tail-bucket shape as
/// cmc/power/toughness's PLANE_*_HI, just fixed at [4,5] instead of
/// live-observed). Every other (op, val) combination is unambiguous because
/// 4 and 5 always agree on which side of any *other* threshold they fall.
fn rarity_hi_ambiguous(op: CmpOp, val: f64) -> bool {
    matches!((val, op), (4.0, CmpOp::Eq | CmpOp::Ne | CmpOp::Le | CmpOp::Gt) | (5.0, CmpOp::Eq | CmpOp::Ne | CmpOp::Lt | CmpOp::Ge))
}

/// The plane-aware narrowing function must reproduce the true existence
/// projection ("does any printing of this card satisfy op(rarity, val)") for
/// every op, across the full 0-5 domain and an impossible threshold, in
/// every case it doesn't legitimately decline (`rarity_hi_ambiguous`) — the
/// reference here is brute force over RARITY_PLANE_FIXTURE directly, not
/// rarity_candidates (which declines for Ne and over-ceiling unions, so it
/// can't serve as the reference for every op the way the plane path must
/// still answer).
#[test]
fn rarity_plane_candidates_matches_existence_projection() {
    let data = rarity_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let n_cards = RARITY_PLANE_FIXTURE.len();

    let keep = |op: CmpOp, r: f64, val: f64| match op {
        CmpOp::Eq => r == val,
        CmpOp::Lt => r < val,
        CmpOp::Le => r <= val,
        CmpOp::Gt => r > val,
        CmpOp::Ge => r >= val,
        CmpOp::Ne => r != val,
    };

    let ops = [
        ("Eq", CmpOp::Eq), ("Ne", CmpOp::Ne), ("Lt", CmpOp::Lt),
        ("Le", CmpOp::Le), ("Gt", CmpOp::Gt), ("Ge", CmpOp::Ge),
    ];
    for (name, op) in ops {
        for val in [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 2.5, -1.0, 6.0] {
            match super::rarity_plane_candidates(&archived.indexes, n_cards, op, val) {
                None => assert!(rarity_hi_ambiguous(op, val), "op {name} val {val}: plane path declined unexpectedly"),
                Some(bits) => {
                    assert!(!rarity_hi_ambiguous(op, val), "op {name} val {val}: plane path should have declined but didn't");
                    let want: Vec<u32> = RARITY_PLANE_FIXTURE
                        .iter()
                        .enumerate()
                        .filter(|(_, rarities)| rarities.iter().any(|r| r.is_some_and(|r| keep(op, f64::from(r), val))))
                        .map(|(cid, _)| cid as u32)
                        .collect();
                    for cid in 0..n_cards as u32 {
                        assert_eq!(
                            bitmap_contains(&bits, cid),
                            want.contains(&cid),
                            "op {name} val {val}: card {cid} membership mismatch"
                        );
                    }
                }
            }
        }
    }
}

/// Card 8 (RARITY_PLANE_FIXTURE) has no rarity on any printing -- it must not
/// appear under any comparison, matching build_rarity_index's own None-skips
/// silently behavior (this repo's history of Null-semantics bugs elsewhere
/// makes this worth its own explicit case, not just incidental coverage in
/// the parity test above).
#[test]
fn rarity_plane_null_rarity_card_matches_nothing() {
    let data = rarity_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let n_cards = RARITY_PLANE_FIXTURE.len();
    let null_card = 8u32;

    let ops = [
        ("Eq", CmpOp::Eq), ("Ne", CmpOp::Ne), ("Lt", CmpOp::Lt),
        ("Le", CmpOp::Le), ("Gt", CmpOp::Gt), ("Ge", CmpOp::Ge),
    ];
    for (name, op) in ops {
        for val in [0.0, 1.0, 2.0, 3.0, 4.0, 5.0] {
            let Some(bits) = super::rarity_plane_candidates(&archived.indexes, n_cards, op, val) else {
                assert!(rarity_hi_ambiguous(op, val), "op {name} val {val}: plane path declined unexpectedly");
                continue;
            };
            assert!(!bitmap_contains(&bits, null_card), "op {name} val {val}: null-rarity card must never match");
        }
    }
}

/// Rarity is now an existential-plane field (docs/issues/engine-existential-
/// plane-generalization.md, #680), promoted the same way legality is
/// (docs/issues/00667-engine-legality-divergent-carveout.md): the 4 tracked values
/// exact-consume via compile_plane, `!=val` on a tracked value is exact too
/// (Or of the other tracked planes plus the shared hi plane), but a query
/// needing to distinguish special from bonus specifically still declines to
/// compile — the plane can't tell them apart (see `PLANE_RARITY_HI`'s doc) --
/// and correctly falls back to the (unaffected) narrow_rec/RarityIndex path,
/// not a silently wrong all_match.
#[test]
fn rarity_tracked_values_exact_consumed_hi_bucket_declines() {
    let data = plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let rarity_eq = |v: f64| FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(v) };

    // Tracked values (mythic=3) compile exactly.
    assert!(compile_plane(&rarity_eq(3.0), bounds, words).is_some(), "r:mythic must compile to a plane expression");
    // Special/bonus specifically can't be told apart by the shared hi plane.
    assert!(compile_plane(&rarity_eq(4.0), bounds, words).is_none(), "r:special must decline (hi bucket is ambiguous)");
    assert!(compile_plane(&rarity_eq(5.0), bounds, words).is_none(), "r:bonus must decline (hi bucket is ambiguous)");

    // Mixed with an otherwise fully plane-expressible sibling: an ambiguous
    // rarity child must stay in the residual, not get silently dropped or promoted.
    let creature = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let (pe, residual) = split_planes(FilterExpr::And(vec![creature, rarity_eq(4.0)]), bounds, words, true);
    assert!(pe.is_some(), "the creature child must still plane-consume");
    assert!(
        matches!(&residual, FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), .. }),
        "r:special must remain in the residual, not be consumed to True"
    );

    // A tracked value's negation is exact too.
    let not_mythic = FilterExpr::Not(Box::new(rarity_eq(3.0)));
    assert!(compile_plane(&not_mythic, bounds, words).is_some(), "-r:mythic must compile to a plane expression");
}

/// Y=2 shared-witness decline: two distinct rarity plane indices under one
/// And (whether from the same field, like a bounded range, or a different
/// field entirely, like legality) can't be answered from independent
/// existence planes -- the same argument as two distinct legality facts
/// (docs/issues/00680-engine-existential-plane-generalization.md point 1's hand
/// verification), now exercised with rarity's own indices in scope.
#[test]
fn rarity_shared_witness_declines_same_field_and_cross_field() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let rarity_ge = |v: f64| FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Ge, rhs: NumExpr::Const(v) };
    let rarity_le = |v: f64| FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Le, rhs: NumExpr::Const(v) };

    // rarity>=rare AND rarity<=mythic: two distinct tracked plane indices
    // (rare, mythic) shared between the two Or-trees -- must decline.
    let bounded_range = FilterExpr::And(vec![rarity_ge(2.0), rarity_le(3.0)]);
    assert!(compile_plane(&bounded_range, bounds, words).is_none(), "same-field rarity range must decline (shared witness)");

    // format:A AND r:mythic: two distinct fields, still the identical
    // shared-witness problem -- collect_existential_indices doesn't care
    // which family a plane index came from.
    let format_a = FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    let cross_field = FilterExpr::And(vec![format_a, FilterExpr::NumericCmp {
        lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(3.0),
    }]);
    assert!(compile_plane(&cross_field, bounds, words).is_none(), "legality+rarity compound must decline (shared witness)");

    // The fallback must still produce the correct (not just declined)
    // result: no printing in this fixture is both format-A-legal and
    // mythic, so it must be zero, not a false positive from independently
    // narrowing each fact.
    let mut residual_check = cross_field;
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual_check, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 0, "no card in this fixture is both format-A-legal and mythic");
}

/// -rarity:x / rarity:x negation, exercised through narrow_rec end-to-end
/// (not just narrow_rarity directly), through the dedicated Not arm --
/// recomputes with negate_op(op) rather than complementing the existing
/// candidate set. Covers both operand orders (Field/Const and Const/Field,
/// the latter needing negate_op(flip_op(op))) and confirms the negated form
/// agrees with the same brute-force existence projection used for the
/// non-negated ops above.
#[test]
fn rarity_not_arm_matches_existence_projection() {
    let data = rarity_plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let n_cards = RARITY_PLANE_FIXTURE.len();

    let keep = |op: CmpOp, r: f64, val: f64| match op {
        CmpOp::Eq => r == val,
        CmpOp::Lt => r < val,
        CmpOp::Le => r <= val,
        CmpOp::Gt => r > val,
        CmpOp::Ge => r >= val,
        CmpOp::Ne => r != val,
    };

    let ops = [
        ("Eq", CmpOp::Eq), ("Ne", CmpOp::Ne), ("Lt", CmpOp::Lt),
        ("Le", CmpOp::Le), ("Gt", CmpOp::Gt), ("Ge", CmpOp::Ge),
    ];
    for (name, op) in ops {
        for val in [0.0, 1.0, 2.0, 3.0, 4.0, 5.0] {
            // A tracked value narrows exactly via the plane. Special/bonus
            // specifically (val 4 or 5, hi-bucket-ambiguous op) may decline
            // the plane and fall to RarityIndex postings instead -- which can
            // itself decline for its own, pre-existing, unrelated reason (the
            // MAX_UNION_FRACTION broadness guard, when the negated op selects
            // most of the corpus). Either decline is a correct answer (a full
            // residual scan is always correct, just unnarrowed), so this test
            // only checks correctness when narrowing *does* happen -- the
            // plane's own decline boundary is pinned down precisely by
            // rarity_hi_ambiguous elsewhere, which doesn't depend on corpus
            // shape the way the postings guard does.
            let filter = FilterExpr::Not(Box::new(FilterExpr::NumericCmp {
                lhs: NumExpr::Field(NumField::RarityInt),
                op,
                rhs: NumExpr::Const(val),
            }));
            let Some(n) = super::narrow_rec(&filter, &archived.indexes, &archived.offsets, &archived.cards, true) else {
                continue;
            };
            let cand = n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
            let want: Vec<u32> = RARITY_PLANE_FIXTURE
                .iter()
                .enumerate()
                .filter(|(_, rarities)| rarities.iter().any(|r| r.is_some_and(|r| !keep(op, f64::from(r), val))))
                .map(|(cid, _)| cid as u32)
                .collect();
            for cid in 0..n_cards as u32 {
                assert_eq!(
                    cand.contains(&cid),
                    want.contains(&cid),
                    "Not(field {name} {val}): card {cid} membership mismatch"
                );
            }

            // Not(val flip_op(op) field) -- the flipped operand order
            // expressing the SAME predicate as `field op val` (e.g. `field <
            // val` is `val > field`, not `val < field` -- same op on both
            // sides would be a different comparison entirely).
            let filter_flipped = FilterExpr::Not(Box::new(FilterExpr::NumericCmp {
                lhs: NumExpr::Const(val),
                op: super::flip_op(op),
                rhs: NumExpr::Field(NumField::RarityInt),
            }));
            let n2 = super::narrow_rec(&filter_flipped, &archived.indexes, &archived.offsets, &archived.cards, true)
                .unwrap_or_else(|| panic!("Not({val} {name} field) must narrow given Not(field {name} {val}) did"));
            let cand2 = n2.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
            assert_eq!(cand, cand2, "operand order must not change the result: {name} {val}");
        }
    }
}

#[test]
fn cards_of_printings_maps_and_dedups() {
    let raw_offsets = vec![0u32, 3, 4, 7];
    let offsets_bytes = rkyv::to_bytes::<Error>(&raw_offsets).expect("serialize");
    let offsets = rkyv::access::<Archived<Vec<u32>>, Error>(&offsets_bytes).expect("access");
    let printing_to_card_bytes = rkyv::to_bytes::<Error>(&build_printing_to_card(&raw_offsets)).expect("serialize");
    let printing_to_card = rkyv::access::<Archived<Vec<u32>>, Error>(&printing_to_card_bytes).expect("access");
    // printings 0-2 → card 0, 3 → card 1, 4-6 → card 2
    assert_eq!(cards_of_printings(offsets, printing_to_card, &[0, 1, 2, 3, 5, 6]), vec![0, 1, 2]);
    assert_eq!(cards_of_printings(offsets, printing_to_card, &[1]), vec![0]);
    assert_eq!(cards_of_printings(offsets, printing_to_card, &[]), Vec::<u32>::new());
}

/// Differential test for the direct-array projection
/// (docs/issues/00690-engine-direct-projection-arrays.md): `cards_of_printings`
/// must agree with an independent reference oracle (a `partition_point` search on
/// `offsets`, applied uniformly regardless of size -- the mechanism the small-k
/// path itself used before this change) at every k, spanning both the small-k
/// (<=1024) and broad-k (>1024) branches, so the branch split is provably a pure
/// performance choice, not a behavior change.
#[test]
fn cards_of_printings_matches_naive_projection_across_sizes() {
    use rand::SeedableRng;
    const NUM_SEEDS: u64 = 40;
    const N_CARDS: usize = 200;

    for seed in 0..NUM_SEEDS {
        let mut rng = rand::rngs::SmallRng::seed_from_u64(seed);
        let mut raw_offsets = Vec::with_capacity(N_CARDS + 1);
        raw_offsets.push(0u32);
        let mut total = 0u32;
        for _ in 0..N_CARDS {
            total += rng.random_range(1..=20);
            raw_offsets.push(total);
        }
        let n_printings = total as usize;

        let offsets_bytes = rkyv::to_bytes::<Error>(&raw_offsets).expect("serialize");
        let offsets = rkyv::access::<Archived<Vec<u32>>, Error>(&offsets_bytes).expect("access");
        let printing_to_card_vec = build_printing_to_card(&raw_offsets);
        let printing_to_card_bytes = rkyv::to_bytes::<Error>(&printing_to_card_vec).expect("serialize");
        let printing_to_card = rkyv::access::<Archived<Vec<u32>>, Error>(&printing_to_card_bytes).expect("access");

        // k values straddling the 1024 small/broad split; clamp to n_printings.
        for &k in &[0usize, 1, 5, 50, 999, 1024, 1025, 2000, 5000] {
            let k = k.min(n_printings);
            let mut printing_ids: Vec<u32> = {
                let mut set = std::collections::HashSet::with_capacity(k);
                while set.len() < k {
                    set.insert(rng.random_range(0..n_printings as u32));
                }
                set.into_iter().collect()
            };
            printing_ids.sort_unstable();

            let got = cards_of_printings(offsets, printing_to_card, &printing_ids);

            // Reference oracle: partition_point on offsets directly, shares no
            // code with build_printing_to_card or cards_of_printings' branches.
            let mut want: Vec<u32> = printing_ids
                .iter()
                .map(|&p| raw_offsets.partition_point(|&o| o <= p) as u32 - 1)
                .collect();
            want.dedup();

            assert_eq!(got, want, "seed={seed}, k={k}, n_printings={n_printings}");
        }
    }
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

// ─── Legality bitplanes (#630 phase 2 / #667 dual-exact-plane redesign) ───────

/// card0: legal in format A (shift 0) only, single printing. card1: legal in
/// format B (shift 2) only, single printing. card2: genuinely divergent for
/// format A — two printings, one legal and one not — so both
/// `legal_exists(A)` and `illegal_exists(A)` are true for it at once
/// (docs/issues/00667-engine-legality-divergent-carveout.md). All three also carry
/// a format C (shift 4) banned/restricted status, generalized by #678 (see
/// docs/issues/00678-engine-legality-banned-restricted-planes.md): card0 banned in
/// C (single printing), card1 restricted in C (single printing), card2
/// divergent on *banned(C)* too — one printing banned, the other merely
/// legal (not banned) — mirroring the real `restricted:oldschool` shape
/// (a 30th-Anniversary-style promo printing disagreeing with the rest).
fn legal_plane_fixture() -> CardData {
    let mut vocab = VocabInterner::new();
    let mut card0 = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card0.card_legalities = 0b01; // legal at shift 0
    let mut card1 = stub_card(2, TYPE_CREATURE, &[], &mut vocab);
    card1.card_legalities = 0b01 << 2; // legal at shift 2
    let mut card2 = stub_card(3, TYPE_CREATURE, &[], &mut vocab);
    card2.legality_divergent = true;
    let mut data = store_of(vec![card0, card1, card2], &[1, 1, 2], vocab);
    // build_bit_planes reads printing-level card_legalities, not the
    // OracleCard-level field set above -- store_of's stub printings all
    // default to 0, so every printing needs its own legality set here before
    // the planes are rebuilt below. card2's two printings deliberately
    // disagree on format A: one legal, one not.
    data.printings[0].card_legalities = 0b01 | (0b11 << 4); // card0: legal in A, banned in C
    data.printings[1].card_legalities = (0b01 << 2) | (0b10 << 4); // card1: legal in B, restricted in C
    data.printings[2].card_legalities = 0b01 | (0b11 << 4); // card2 printing 1: legal in A, banned in C
    data.printings[3].card_legalities = 0b01 << 4; // card2 printing 2: not legal in A, legal (not banned) in C
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data.indexes.legal_divergent = build_divergent_ids(&data.cards);
    data
}

#[test]
fn legal_plane_narrows_positive_includes_divergent() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    // Format A (shift 0): card0 (truly legal) + card2 (divergent, has a legal
    // printing) narrow in; card1 (legal only in format B) must not.
    let f = FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    match narrow_candidates(&f, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(bitmap_contains(&bits, 0), "truly legal card must narrow in");
            assert!(!bitmap_contains(&bits, 1), "card legal only in a different format must not narrow in");
            assert!(bitmap_contains(&bits, 2), "divergent card with a legal printing must narrow in");
        }
        _ => panic!("expected a card bitmap"),
    }

    // Format B (shift 2): card1 narrows in, card0 does not, card2 does not
    // (its printings only vary on format A; shift math must independently
    // address each format's plane).
    let g = FilterExpr::Legality { shift: Some(2), expected: 0b01 };
    match narrow_candidates(&g, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(!bitmap_contains(&bits, 0));
            assert!(bitmap_contains(&bits, 1));
            assert!(!bitmap_contains(&bits, 2));
        }
        _ => panic!("expected a card bitmap"),
    }
}

#[test]
fn legal_plane_narrows_negated_includes_divergent() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    // -f:A: card0 is truly legal in A (no illegal printing), so it must NOT
    // narrow in; card1 (not legal in A) and card2 (divergent, has a
    // not-legal printing too) must.
    let not_a = FilterExpr::Not(Box::new(FilterExpr::Legality { shift: Some(0), expected: 0b01 }));
    match narrow_candidates(&not_a, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(!bitmap_contains(&bits, 0), "truly legal card must not narrow into the negation");
            assert!(bitmap_contains(&bits, 1), "truly not-legal card must narrow into the negation");
            assert!(bitmap_contains(&bits, 2), "divergent card with a not-legal printing must narrow into the negation");
        }
        _ => panic!("expected a card bitmap"),
    }
}

/// #678: banned:/restricted: now plane-narrow exactly like format:, including
/// through a divergent card (card2, banned in C via one printing, merely
/// legal via the other — see `legal_plane_fixture`'s doc). Only a format
/// absent from all loaded data (shift: None) still declines.
#[test]
fn banned_restricted_plane_narrows_positive_includes_divergent() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let banned_c = FilterExpr::Legality { shift: Some(4), expected: 0b11 };
    match narrow_candidates(&banned_c, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(bitmap_contains(&bits, 0), "card0 truly banned in C must narrow in");
            assert!(!bitmap_contains(&bits, 1), "card1 is restricted, not banned, in C");
            assert!(bitmap_contains(&bits, 2), "divergent card2 has a banned-in-C printing");
        }
        _ => panic!("expected a card bitmap for banned:C"),
    }

    let restricted_c = FilterExpr::Legality { shift: Some(4), expected: 0b10 };
    match narrow_candidates(&restricted_c, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(!bitmap_contains(&bits, 0), "card0 is banned, not restricted, in C");
            assert!(bitmap_contains(&bits, 1), "card1 truly restricted in C must narrow in");
            assert!(!bitmap_contains(&bits, 2), "card2 has no restricted-in-C printing");
        }
        _ => panic!("expected a card bitmap for restricted:C"),
    }

    // A format absent from all loaded data (shift: None) matches nothing at
    // eval and isn't plane-narrowed either — falls to the (cheap, correct)
    // full scan, for every indexed status.
    let absent_banned = FilterExpr::Legality { shift: None, expected: 0b11 };
    assert!(narrow_candidates(&absent_banned, &archived.indexes, &archived.offsets, &archived.cards).is_none());
    let absent_restricted = FilterExpr::Legality { shift: None, expected: 0b10 };
    assert!(narrow_candidates(&absent_restricted, &archived.indexes, &archived.offsets, &archived.cards).is_none());
}

/// -banned:C: card0 (truly banned) must not narrow in; card1 (restricted, so
/// not banned) and card2 (divergent, has a not-banned printing) must.
#[test]
fn banned_restricted_plane_narrows_negated_includes_divergent() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let not_banned_c = FilterExpr::Not(Box::new(FilterExpr::Legality { shift: Some(4), expected: 0b11 }));
    match narrow_candidates(&not_banned_c, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::CardBits(bits)) => {
            assert!(!bitmap_contains(&bits, 0), "truly banned-in-C card must not narrow into the negation");
            assert!(bitmap_contains(&bits, 1), "restricted (not banned) card must narrow into the negation");
            assert!(bitmap_contains(&bits, 2), "divergent card with a not-banned-in-C printing must narrow in");
        }
        _ => panic!("expected a card bitmap"),
    }
}

/// banned:C AND restricted:C: two distinct existence facts about the *same*
/// format — the same shared-witness exposure as two distinct formats or two
/// polarities of one format, now reachable because `collect_legality_formats`
/// dedupes by raw plane index (#678) rather than a `(format, polarity)` tuple
/// that had no way to express "same format, different status."
#[test]
fn legality_same_format_cross_status_declines_shared_witness() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let banned_c = || FilterExpr::Legality { shift: Some(4), expected: 0b11 };
    let restricted_c = || FilterExpr::Legality { shift: Some(4), expected: 0b10 };

    assert!(
        compile_plane(&FilterExpr::And(vec![banned_c(), restricted_c()]), bounds, words).is_none(),
        "banned:C AND restricted:C (same format, distinct statuses) must decline (shared-witness)"
    );
    assert!(
        compile_plane(&FilterExpr::Or(vec![banned_c(), restricted_c()]), bounds, words).is_some(),
        "OR has no shared-witness problem and must compile"
    );
}

/// The shared-witness decline's fallback must still be *correct*: a card
/// banned in C via one printing and restricted in C via a different printing
/// (no single printing is both) is the trap in action — banned_exists(C) AND
/// restricted_exists(C) would wrongly say true if naively ANDed.
#[test]
fn legality_cross_status_shared_witness_falls_back_to_correct_result() {
    let mut vocab = VocabInterner::new();
    let card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0b11 << 4; // banned in C only
    data.printings[1].card_legalities = 0b10 << 4; // restricted in C only
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let and_both = FilterExpr::And(vec![
        FilterExpr::Legality { shift: Some(4), expected: 0b11 },
        FilterExpr::Legality { shift: Some(4), expected: 0b10 },
    ]);
    let (plane, mut residual) = split_planes(and_both, bounds, words, true);
    assert!(plane.is_none(), "shared-witness AND must not partially promote either leaf into the plane");
    assert!(matches!(residual, FilterExpr::And(_)), "both legality children must remain in the residual");

    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual, plane.as_ref(), "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 0, "no single printing is both banned and restricted in C at once");
}

/// Row-selection correctness (docs/issues/00667-engine-legality-divergent-carveout.md
/// "Row selection for unique=card") through the real `run_query` pipeline,
/// mirroring `legal_plane_narrowing_preserves_divergent_printing_correctness`
/// but for `banned:` — the preferred printing deliberately says NOT banned,
/// the non-preferred one says banned, to stress that row emission for
/// `unique=printing` picks the printing that actually satisfies the query,
/// not just the card's usual preferred one.
#[test]
fn banned_plane_row_selection_preserves_divergent_printing_correctness() {
    let mut vocab = VocabInterner::new();
    let mut card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card.legality_divergent = true;
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0b01 << 4; // preferred: legal (not banned) in C
    data.printings[1].card_legalities = 0b11 << 4; // non-preferred: banned in C
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data.indexes.legal_divergent = build_divergent_ids(&data.cards);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let run = |filter: &mut FilterExpr, unique: &str| {
        run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            filter, None, unique, "default", "edhrec", "asc", 100, 0, &archived.indexes,
        )
    };

    // banned:C, unique=printing: exactly the one banned printing.
    let mut f = FilterExpr::Legality { shift: Some(4), expected: 0b11 };
    let (total, page) = run(&mut f, "printing");
    assert_eq!(total, 1);
    assert_eq!(u128::from(page[0].1.scryfall_id), 2); // the non-preferred, banned printing

    // banned:C, unique=card: the card matches (at least one printing is
    // banned), read straight from the exact banned_exists(C) plane bit.
    let mut f2 = FilterExpr::Legality { shift: Some(4), expected: 0b11 };
    let (total, _) = run(&mut f2, "card");
    assert_eq!(total, 1);

    // -banned:C, unique=printing: exactly the one not-banned printing.
    let mut not_f = FilterExpr::Not(Box::new(FilterExpr::Legality { shift: Some(4), expected: 0b11 }));
    let (total, page) = run(&mut not_f, "printing");
    assert_eq!(total, 1);
    assert_eq!(u128::from(page[0].1.scryfall_id), 1); // the preferred, not-banned printing
}

/// The correctness property the whole design hinges on: a divergent card must
/// never be silently dropped by the narrowing, in either polarity, even when
/// its *preferred* printing's status disagrees with a non-preferred printing
/// that the query should actually match. This exercises the full path
/// (narrow_rec's plane arms feeding run_query's unmodified card_pass/residual
/// walk), not just the bitmap in isolation.
#[test]
fn legal_plane_narrowing_preserves_divergent_printing_correctness() {
    let mut vocab = VocabInterner::new();
    // Preferred printing (higher prefer_score, sorts first) says NOT legal;
    // the second, non-preferred printing says legal — the opposite of the
    // "usual" case, deliberately, to stress that narrowing is built from
    // per-printing truth, not just the preferred printing's status.
    let mut card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card.legality_divergent = true;
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0; // preferred: not legal
    data.printings[1].card_legalities = 0b01; // non-preferred: legal
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data.indexes.legal_divergent = build_divergent_ids(&data.cards);
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
    // read straight from the exact legal_exists(A) plane bit.
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
    let cards: Vec<OracleCard> = (0..n).map(|i| stub_card(1 + i as u128, TYPE_CREATURE, &[], &mut vocab)).collect();
    let printing_counts = vec![1usize; n];
    let mut data = store_of(cards, &printing_counts, vocab);
    // Legal (at shift 2) on even ids only, spanning past the 64-bit word
    // boundary; printing_counts is all 1s so printing i belongs to card i.
    for i in 0..n {
        data.printings[i].card_legalities = if i % 2 == 0 { 0b01 << 2 } else { 0 };
    }
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
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

/// `plane_expr_is_existential` is the whole mode-aware-all_match fix's load-
/// bearing check: it must flag any compiled expression touching a legality
/// plane (docs/issues/00667-engine-legality-divergent-carveout.md) and only those
/// -- card-invariant fields (types here) must never be flagged, and an And
/// mixing the two must still be flagged (any existential leaf taints the
/// whole composed expression).
#[test]
fn plane_expr_is_existential_identifies_legality_only() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let legality_pe = compile_plane(&FilterExpr::Legality { shift: Some(0), expected: 0b01 }, bounds, words).unwrap();
    assert!(super::plane_expr_is_existential(&legality_pe));

    let creature_pe = compile_plane(&FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge }, bounds, words).unwrap();
    assert!(!super::plane_expr_is_existential(&creature_pe));

    let mixed = compile_plane(
        &FilterExpr::And(vec![
            FilterExpr::Legality { shift: Some(0), expected: 0b01 },
            FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge },
        ]),
        bounds,
        words,
    )
    .unwrap();
    assert!(super::plane_expr_is_existential(&mixed), "one existential leaf must taint the whole And");
}

/// Regression for the mode-aware all_match bug found while building this
/// design: `format:A` run through the *real* pipeline (split_planes, not a
/// direct narrow_candidates/run_query call with plane=None) must not let
/// unique=printing see every printing of a matching card just because the
/// card-level existence fact is true. A bare Legality leaf fully consumed to
/// `FilterExpr::True` regardless of mode would discard the only thing that
/// could re-derive *which* printing matches, so split_planes now declines the
/// fold itself for unique=printing/artwork (`unique_is_card=false`), leaving
/// the original Legality node in the residual for the normal per-printing
/// card_pass walk. unique=card is unaffected: existence is exactly what it
/// needs, so it still takes the fully-consumed fast path.
#[test]
fn legality_plane_promotion_respects_mode_through_split_planes() {
    let mut vocab = VocabInterner::new();
    // Preferred printing not legal, non-preferred printing legal -- same
    // deliberately-inverted shape as the narrow_rec-level correctness test
    // above, but exercised through split_planes this time.
    let mut card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card.legality_divergent = true;
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0; // preferred: not legal
    data.printings[1].card_legalities = 0b01; // non-preferred: legal
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data.indexes.legal_divergent = build_divergent_ids(&data.cards);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let run_mode = |unique: &str| {
        let f = FilterExpr::Legality { shift: Some(0), expected: 0b01 };
        let unique_is_card = unique != "printing" && unique != "artwork";
        let (plane, mut residual) = split_planes(f, bounds, words, unique_is_card);
        if unique_is_card {
            assert!(plane.is_some(), "a bare Legality leaf must still fully plane-consume for unique=card");
            assert!(matches!(residual, FilterExpr::True), "split_planes must leave a bare True residual for unique=card");
        } else {
            assert!(plane.is_none(), "unique=printing/artwork must decline the fold, not just patch around it");
            assert!(matches!(residual, FilterExpr::Legality { .. }), "the original Legality node must survive for per-printing verification");
        }
        run_query(
            &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
            &mut residual, plane.as_ref(), unique, "default", "edhrec", "asc", 100, 0, &archived.indexes,
        )
    };

    let (total, page) = run_mode("printing");
    assert_eq!(total, 1, "only the one legal printing may match, even though the plane says the card matches");
    assert_eq!(u128::from(page[0].1.scryfall_id), 2);

    // unique=card: the count is right via existence alone, but the *returned
    // printing* must still be the legal one, not just the card's normal
    // default-preferred printing (docs/issues/engine-legality-divergent-
    // carveout.md "Row selection for unique=card") -- this is exactly the
    // case a prior version of this test missed, by discarding `page` here.
    let (total, page) = run_mode("card");
    assert_eq!(total, 1, "unique=card only needs the existence fact the plane already proves");
    assert_eq!(
        u128::from(page[0].1.scryfall_id),
        2,
        "unique=card must return the legal printing, not the preferred-but-not-legal one"
    );
}

/// The same row-selection bug, but reached via a compound filter (the shape
/// this issue's own motivating query has: `format:X` ANDed with a
/// card-invariant sibling) rather than a bare Legality leaf -- exercises
/// `existential_plane` detection on a composed `PlaneExpr::And`, not just a
/// single `PlaneExpr::Plane`.
#[test]
fn legality_compound_and_respects_row_selection_for_unique_card() {
    let mut vocab = VocabInterner::new();
    let mut card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card.legality_divergent = true;
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0; // preferred: not legal in A
    data.printings[1].card_legalities = 0b01; // non-preferred: legal in A
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data.indexes.legal_divergent = build_divergent_ids(&data.cards);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let filter = FilterExpr::And(vec![
        FilterExpr::Legality { shift: Some(0), expected: 0b01 },
        FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge },
    ]);
    let (plane, mut residual) = split_planes(filter, bounds, words, true);
    assert!(plane.is_some(), "format:A AND t:creature (one format, no shared-witness issue) must compile whole");
    assert!(matches!(residual, FilterExpr::True));

    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual, plane.as_ref(), "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 1);
    assert_eq!(
        u128::from(page[0].1.scryfall_id),
        2,
        "the compound's card-invariant sibling must not mask the legality leaf's row-selection requirement"
    );
}

/// Found in #676's review: a legality leaf ANDed with a genuinely
/// printing-dependent, non-plane-compilable residual (`DateCmp` here --
/// never appears in `planes.rs`, so it always stays in the residual after
/// `split_planes`'s partial extraction) needs *both* checked against the
/// *same* printing. printing 0 is legal in A but released before the cutoff;
/// printing 1 is released after the cutoff but not legal in A -- no single
/// printing satisfies both, so `format:A AND date>cutoff` (unique=card) must
/// match 0 times, not pick either printing on the strength of just one half
/// of the conjunction.
#[test]
fn legality_and_date_residual_must_be_checked_together() {
    let mut vocab = VocabInterner::new();
    let card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0b01; // legal in A, but...
    data.printings[0].released_at_int = Some(20100101); // ...released before the cutoff
    data.printings[1].card_legalities = 0; // not legal in A, but...
    data.printings[1].released_at_int = Some(20220101); // ...released after the cutoff
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let filter = FilterExpr::And(vec![
        FilterExpr::Legality { shift: Some(0), expected: 0b01 },
        FilterExpr::DateCmp { op: CmpOp::Gt, value: 20200101 },
    ]);
    let (plane, mut residual) = split_planes(filter, bounds, words, true);
    assert!(plane.is_some(), "the legality leaf alone must still promote into the plane");
    assert!(
        matches!(residual, FilterExpr::DateCmp { .. }),
        "date> isn't plane-compilable, so it must remain a real residual, not collapse to True"
    );

    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual, plane.as_ref(), "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 0, "no single printing is both legal in A and released after the cutoff");
}

// ─── #677: differential row-identity fuzzer ──────────────────────────────────
//
// Every other legality/plane parity test above checks a `total` or a candidate
// id *set*. #676 was a bug where `total` was right but the emitted
// `(card, printing)` row for `unique=card`/`printing`/`artwork` did not itself
// satisfy the filter — a divergent card's plane `all_match` promotion picked a
// printing that matched the card-level existence projection but not this
// specific printing. A count-only or set-only assertion cannot see that; the
// wrong *printing* of a matching *card* is invisible unless you check row
// identity.
//
// This fuzzer generates random small stores and random filters (legality,
// rarity, border — the printing-varying fields — mixed with card-invariant
// colors/types/cmc under And/Or/Not), runs each through the real `run_query`
// pipeline (both the unplaned path and the `split_planes` + plane path, for all
// three `unique` modes), and asserts that *every returned row* is accepted by
// the trusted per-printing evaluator `FilterExpr::matches` (linear scan,
// `tri()`-only, no planes / no all_match shortcut). It also cross-checks the
// `total` against a brute-force count as a secondary guard, but the row-identity
// assertion is the one that would have caught #676. Deterministic per-seed so a
// failure reproduces exactly.

#[derive(Clone)]
enum FuzzLeaf {
    Color { op: CmpOp, mask: u8 },      // card-invariant
    Type { op: CmpOp, mask: u16 },      // card-invariant
    Cmc { op: CmpOp, val: f64 },        // card-invariant
    Power { op: CmpOp, val: f64 },      // card-invariant, nullable (non-creatures)
    Toughness { op: CmpOp, val: f64 },  // card-invariant, nullable
    Loyalty { op: CmpOp, val: f64 },    // card-invariant, nullable (planeswalkers only)
    Rarity { op: CmpOp, val: f64 },     // printing-varying
    CollectorNumber { op: CmpOp, val: f64 }, // printing-varying, nullable
    Price { op: CmpOp, val: f64 },      // printing-varying, nullable (dollars)
    Date { op: CmpOp, value: u32 },     // printing-varying (released_at, DateCmp)
    Year { op: CmpOp, year: i32 },      // printing-varying (released_at, YearCmp)
    Border { value: String },           // printing-varying
    Legality { shift: Option<u8>, expected: u64 }, // printing-dependent for divergent cards
    // Arithmetic (NumExpr::Arith), mixing a card-level and a printing-level field — exercises
    // field_num on both operands and PrintingDep propagation through arithmetic. `shape` selects
    // one of the fixed operand pairs in `fuzz_arith_pair` (kept as an id, not a stored NumExpr,
    // so FuzzLeaf stays Clone without NumExpr needing to be).
    Arith { shape: u8, op: CmpOp },
    // Set-containment against a single value (`CollectionCmp`) — subtypes/keywords/tags. `Ge` (`:`)
    // narrows via the tag index; other ops are residual. `value` resolves to a vocab id via bind.
    Collection { field: CollField, op: CmpOp, value: String },
    // Artist predicate: `TextContains(ArtistLower)` that bind() rewrites to `ArtistMatch` (printing-
    // space, CSR-indexed via the artist vocab). `word` is a lowercased artist name.
    Artist { word: String },
    // Text contains (`:`) over name/oracle/flavor. `needle` is a real corpus token so it matches
    // something. Name/oracle drive the trigram + name-bigram indexes and the full-scan memoization
    // (NameMatch/OracleMatch); flavor is bind-rewritten to FlavorMatch (fingerprint-prefiltered,
    // printing-space).
    TextContains { field: TextSearchField, needle: String },
    // Whole-name comparison (`!name` and ordered variants) — the ExactName path via TextExact.
    NameExact { op: CmpOp, value: String },
    // Mana cost comparison (`mana <op> <cost>`): query core pips packed into lanes + hybrid symbols
    // (bind() resolves to mana-vocab ids) + cmc. Card-invariant; SWAR lane arithmetic, no index.
    ManaCost { op: CmpOp, core: u64, hybrids: Vec<(String, u8)>, cmc: f32 },
    // Devotion (`devotion <op> <pips>`): queried WUBRGC counts in the low six lanes. Card-invariant;
    // exercises the devotion planes (built from card.mana_cost.devotion).
    Devotion { op: CmpOp, pips: u64 },
}

/// The `(lhs, rhs)` operand pair for an `Arith` leaf's `shape`, built fresh each call.
fn fuzz_arith_pair(shape: u8) -> (NumExpr, NumExpr) {
    let f = |field| NumExpr::Field(field);
    let arith = |l, o, r| NumExpr::Arith(Box::new(l), o, Box::new(r));
    match shape {
        0 => (arith(f(NumField::Cmc), ArithOp::Add, NumExpr::Const(1.0)), f(NumField::Power)),
        1 => (arith(f(NumField::PriceUsd), ArithOp::Add, NumExpr::Const(1.0)), f(NumField::Cmc)),
        2 => (arith(f(NumField::Power), ArithOp::Add, f(NumField::Toughness)), f(NumField::Cmc)),
        _ => (arith(f(NumField::Cmc), ArithOp::Mul, NumExpr::Const(2.0)), f(NumField::CollectorNumberInt)),
    }
}

/// Weighted pick from a small `(value, weight)` table — lets `fuzz_store` sample fields on rough
/// real-card distributions (measured from the corpus) instead of uniformly.
fn fuzz_weighted<T: Copy>(rng: &mut rand::rngs::SmallRng, table: &[(T, u32)]) -> T {
    let total: u32 = table.iter().map(|(_, w)| w).sum();
    let mut r = rng.random_range(0..total);
    for &(v, w) in table {
        if r < w {
            return v;
        }
        r -= w;
    }
    table[table.len() - 1].0
}

#[derive(Clone)]
enum FuzzSpec {
    Leaf(FuzzLeaf),
    And(Vec<FuzzSpec>),
    Or(Vec<FuzzSpec>),
    Not(Box<FuzzSpec>),
}

const FUZZ_OPS: [CmpOp; 6] = [CmpOp::Eq, CmpOp::Ne, CmpOp::Lt, CmpOp::Le, CmpOp::Gt, CmpOp::Ge];
// Three formats packed at even shifts, 2 bits each — the fixture layout from
// `legal_plane_fixture`. Expected status is one of legal(0b01)/restricted(0b10)/
// banned(0b11); NOT_LEGAL(0b00) is never a query leaf (the parser negates instead).
const FUZZ_SHIFTS: [u8; 3] = [0, 2, 4];
const FUZZ_STATUSES: [u64; 3] = [0b01, 0b10, 0b11];

fn fuzz_op(rng: &mut rand::rngs::SmallRng) -> CmpOp {
    FUZZ_OPS[rng.random_range(0..FUZZ_OPS.len())]
}

fn fuzz_leaf_color(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Color { op: fuzz_op(rng), mask: rng.random_range(0..32u8) })
}
fn fuzz_leaf_type(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Type { op: fuzz_op(rng), mask: fuzz_type_bits(rng) })
}
fn fuzz_leaf_cmc(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Cmc { op: fuzz_op(rng), val: rng.random_range(0..=8u8) as f64 })
}
fn fuzz_leaf_rarity(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Rarity { op: fuzz_op(rng), val: rng.random_range(0..=4u8) as f64 })
}
fn fuzz_leaf_border(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    // "yellow" is deliberately untracked by the border planes (#680 tracks
    // black/white/borderless/gold, folding anything else into the shared
    // `other` plane) — a decline-to-compile-exactly case that must still be
    // evaluated correctly via the residual path.
    const BORDERS: [&str; 5] = ["black", "white", "borderless", "gold", "yellow"];
    FuzzSpec::Leaf(FuzzLeaf::Border { value: BORDERS[rng.random_range(0..BORDERS.len())].to_string() })
}
fn fuzz_leaf_legality(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    // 10% of the time an absent format (shift: None), which matches nothing.
    let shift = if rng.random_bool(0.1) { None } else { Some(FUZZ_SHIFTS[rng.random_range(0..FUZZ_SHIFTS.len())]) };
    FuzzSpec::Leaf(FuzzLeaf::Legality { shift, expected: FUZZ_STATUSES[rng.random_range(0..FUZZ_STATUSES.len())] })
}

fn fuzz_leaf_power(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Power { op: fuzz_op(rng), val: rng.random_range(0..=7u8) as f64 })
}
fn fuzz_leaf_toughness(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Toughness { op: fuzz_op(rng), val: rng.random_range(0..=7u8) as f64 })
}
fn fuzz_leaf_loyalty(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Loyalty { op: fuzz_op(rng), val: rng.random_range(2..=7u8) as f64 })
}
fn fuzz_leaf_collector_number(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::CollectorNumber { op: fuzz_op(rng), val: fuzz_weighted(rng, &[(10.0, 2), (50.0, 3), (100.0, 3), (250.0, 2), (500.0, 1)]) })
}
fn fuzz_leaf_price(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    // Dollars; thresholds straddle the corpus's skew (median ~$0.33, 99th ~$60).
    FuzzSpec::Leaf(FuzzLeaf::Price { op: fuzz_op(rng), val: fuzz_weighted(rng, &[(0.1, 2), (0.5, 4), (1.0, 4), (2.0, 3), (5.0, 2), (20.0, 1), (60.0, 1)]) })
}
/// A release year on the corpus's rough distribution (skewed recent), + a small jitter.
fn fuzz_year_value(rng: &mut rand::rngs::SmallRng) -> u32 {
    fuzz_weighted(rng, &[(1995, 7), (2000, 6), (2005, 7), (2010, 8), (2015, 15), (2020, 40), (2025, 11)]) + rng.random_range(0..5)
}
fn fuzz_leaf_date(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    // Mid-year threshold (YYYYMMDD packed int), straddling the sampled release dates.
    FuzzSpec::Leaf(FuzzLeaf::Date { op: fuzz_op(rng), value: fuzz_year_value(rng) * 10_000 + 700 })
}
fn fuzz_leaf_year(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Year { op: fuzz_op(rng), year: fuzz_year_value(rng) as i32 })
}
fn fuzz_leaf_arith(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::Arith { shape: rng.random_range(0..4u8), op: fuzz_op(rng) })
}

// Collection vocab tables with corpus-like frequency bias (measured from the blue store).
// Lowercased for a consistent fuzz vocab. Weights drive how often each value is *populated*;
// queries sample uniformly so rare values still exercise the low-selectivity index postings.

// Creature subtypes (human very common, monkey rare). Card-space, populated only on creatures
// (which also get the creature type bit set, so `t:creature` and `type:human` correlate).
const FUZZ_SUBTYPES: [(&str, u32); 14] = [
    ("human", 30), ("goblin", 12), ("elf", 10), ("soldier", 8), ("wizard", 7), ("zombie", 6),
    ("beast", 6), ("spirit", 5), ("warrior", 5), ("dragon", 4), ("angel", 3), ("cat", 3),
    ("monkey", 1), ("octopus", 1),
];
// Keywords span every card type (Flying dominant; Fateseal a real rare tail). Card-space.
const FUZZ_KEYWORDS: [(&str, u32); 11] = [
    ("flying", 30), ("enchant", 11), ("trample", 9), ("vigilance", 6), ("haste", 6),
    ("equip", 5), ("flash", 5), ("mill", 5), ("scry", 4), ("cycling", 4), ("fateseal", 1),
];
// Oracle tags: the corpus's densest collection (avg ~12/card, ~4k distinct). Card-space.
const FUZZ_ORACLE_TAGS: [(&str, u32); 11] = [
    ("triggered-ability", 40), ("activated-ability", 34), ("cycle", 31), ("card-names", 27),
    ("removal", 18), ("card-advantage", 17), ("removal-creature", 15), ("evasion", 13),
    ("spot-removal", 13), ("draw", 12), ("typal-snail", 1),
];
// Art tags: densest of all (~10k distinct); bortuk-bonerattle is a real rare tail. Printing-space.
const FUZZ_ART_TAGS: [(&str, u32); 11] = [
    ("plane", 40), ("planar-origin", 20), ("location", 15), ("pose", 15), ("signature", 13),
    ("artist-signature", 13), ("animal", 12), ("human", 11), ("character", 11), ("weapon", 10),
    ("bortuk-bonerattle", 1),
];
// Is-tags are empty in the current corpus, so a synthetic but realistic Scryfall `is:` vocab keeps
// the leaf exercised rather than always-empty. Printing-space.
const FUZZ_IS_TAGS: [(&str, u32); 8] = [
    ("reprint", 30), ("promo", 12), ("firstprint", 10), ("fullart", 6), ("foil", 6),
    ("textless", 3), ("oversized", 2), ("funny", 1),
];
// Frame data: tiny vocab (~29 distinct); "2015" is dominant and dropped by the thresholded index
// (exercising that drop path). Printing-space.
const FUZZ_FRAME_DATA: [(&str, u32); 10] = [
    ("2015", 50), ("2003", 13), ("1997", 9), ("legendary", 8), ("inverted", 6),
    ("1993", 5), ("extendedart", 4), ("showcase", 3), ("enchantment", 2), ("etched", 1),
];
// Artists: own vocab (~2.2k distinct, no NULLs in corpus). Printing-space, matched via ArtistMatch.
const FUZZ_ARTISTS: [(&str, u32); 11] = [
    ("john avon", 13), ("kev walker", 11), ("svetlin velinov", 8), ("greg staples", 7),
    ("daarken", 7), ("dan frazier", 7), ("mark tedin", 7), ("adam paquette", 7),
    ("chris rahn", 6), ("rebecca guay", 4), ("yan li", 1),
];

// Fraction of creatures that are vanilla (empty oracle text). In the corpus, empty oracle text is
// almost exclusively a creature trait: 2.2% of creatures, ~0% of non-creatures. Conditioning on the
// creature bit reproduces that so the text predicates see a realistic vanilla population.
const VANILLA_CREATURE_FRAC: f64 = 0.022;

/// Sample `count` weighted picks from a collection table (duplicates allowed; `vocab_ids` sorts and
/// dedups them into the id-sorted set the set-like collections expect at load).
fn fuzz_collection_picks<'a>(rng: &mut rand::rngs::SmallRng, table: &[(&'a str, u32)], count: usize) -> Vec<&'a str> {
    (0..count).map(|_| fuzz_weighted(rng, table)).collect()
}

/// A `CollectionCmp` leaf over `field`, its value sampled from `table`. Biases toward Ge (`:`, the
/// indexed containment path) with the residual ops (=, >, !=) mixed in.
fn fuzz_leaf_collection(rng: &mut rand::rngs::SmallRng, field: CollField, table: &[(&str, u32)]) -> FuzzSpec {
    const OPS: [CmpOp; 6] = [CmpOp::Ge, CmpOp::Ge, CmpOp::Ge, CmpOp::Eq, CmpOp::Gt, CmpOp::Ne];
    let op = OPS[rng.random_range(0..OPS.len())];
    let value = table[rng.random_range(0..table.len())].0.to_string();
    FuzzSpec::Leaf(FuzzLeaf::Collection { field, op, value })
}

fn fuzz_leaf_subtype(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    fuzz_leaf_collection(rng, CollField::Subtypes, &FUZZ_SUBTYPES)
}

/// `artist:<name>` — a full lowercased name (the common `a:` substring-contains shape), which
/// bind() resolves against the artist vocab and rewrites to `ArtistMatch`.
fn fuzz_leaf_artist(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    let word = FUZZ_ARTISTS[rng.random_range(0..FUZZ_ARTISTS.len())].0.to_string();
    FuzzSpec::Leaf(FuzzLeaf::Artist { word })
}

/// Frozen (name, oracle, flavor) triples sampled once from the blue store, lowercased. Record
/// separator \x1e, field separator \x1f — neither appears in card text. Real strings give the
/// name/oracle trigram, name-bigram, and flavor indexes realistic selectivity (and exercise the
/// full-scan memoization crossover) that hand-written vocab cannot. Sampled into the store by the
/// seeded RNG; see docs/issues/00699.
static TEXT_CORPUS_RAW: &str = include_str!("../testdata/text_corpus.txt");
fn text_corpus() -> &'static [(&'static str, &'static str, &'static str)] {
    static CORPUS: OnceLock<Vec<(&str, &str, &str)>> = OnceLock::new();
    CORPUS.get_or_init(|| {
        TEXT_CORPUS_RAW
            .split('\x1e')
            .filter_map(|rec| {
                let mut f = rec.split('\x1f');
                Some((f.next()?, f.next()?, f.next()?))
            })
            .collect()
    })
}

/// A real search token from the corpus for `field`: a whole word, occasionally trimmed to a 2-3
/// char fragment to hit the short-needle / name-bigram path (#639). Retries past empty fields (a
/// card with no flavor); falls back to a common token if the field keeps coming up empty.
fn fuzz_text_needle(rng: &mut rand::rngs::SmallRng, field: TextSearchField) -> String {
    let corpus = text_corpus();
    for _ in 0..8 {
        let t = corpus[rng.random_range(0..corpus.len())];
        let text = match field {
            TextSearchField::NameLower => t.0,
            TextSearchField::OracleTextLower => t.1,
            TextSearchField::FlavorTextLower => t.2,
            TextSearchField::ArtistLower => t.0, // artist has its own leaf; not reached here
        };
        let words: Vec<&str> = text
            .split_whitespace()
            .map(|w| w.trim_matches(|c: char| !c.is_alphanumeric()))
            .filter(|w| w.chars().count() >= 2)
            .collect();
        if words.is_empty() {
            continue;
        }
        let w = words[rng.random_range(0..words.len())];
        // ~20% of the time a short 2-3 char fragment (a prefix of a real word is still a substring
        // of the text), exercising the bigram / short-needle path.
        return if rng.random_bool(0.2) {
            w.chars().take(rng.random_range(2..=3)).collect()
        } else {
            w.to_string()
        };
    }
    "the".to_string()
}

fn fuzz_leaf_text_contains(rng: &mut rand::rngs::SmallRng, field: TextSearchField) -> FuzzSpec {
    FuzzSpec::Leaf(FuzzLeaf::TextContains { field, needle: fuzz_text_needle(rng, field) })
}

/// Whole-name comparison (`!name` exact plus ordered variants) — the ExactName path via TextExact.
fn fuzz_leaf_name_exact(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    const OPS: [CmpOp; 4] = [CmpOp::Eq, CmpOp::Eq, CmpOp::Ne, CmpOp::Ge];
    let value = text_corpus()[rng.random_range(0..text_corpus().len())].0.to_string();
    FuzzSpec::Leaf(FuzzLeaf::NameExact { op: OPS[rng.random_range(0..OPS.len())], value })
}

// Hybrid mana symbols (uppercase, matching MANA_LANE_SYMS / mana_lane). Cards and queries draw from
// the same set so query hybrids resolve against the store's mana vocab instead of always-unknown.
const FUZZ_HYBRIDS: [&str; 7] = ["W/U", "U/B", "B/R", "R/G", "G/W", "R/W", "2/W"];

/// Sample a small mana cost as (uppercase symbol, count) pips. `colors_mask` limits colored pips to
/// the card's colors (cost colors ⊆ card colors, the real relationship); a query passes all colors.
/// Adds occasional {C}/{X} and (rarely) one hybrid. Generic mana isn't a pip — it lives only in cmc.
fn fuzz_mana_pips(rng: &mut rand::rngs::SmallRng, colors_mask: u8) -> Vec<(&'static str, u8)> {
    let mut pips: Vec<(&'static str, u8)> = Vec::new();
    for lane in 0..5usize {
        if colors_mask & (1 << lane) != 0 {
            // Mostly 1 pip, sometimes 2-3, rarely 5 — the 5 exercises devotion's >3 plane saturation.
            let n = fuzz_weighted(rng, &[(1u8, 70), (2, 22), (3, 6), (5, 2)]);
            pips.push((super::MANA_LANE_SYMS[lane], n));
        }
    }
    if rng.random_bool(0.08) {
        pips.push(("C", rng.random_range(1..=2)));
    }
    if rng.random_bool(0.05) {
        pips.push(("X", 1));
    }
    // Hybrids are on ~3% of real cards; slightly over-represented here for coverage of the bind path.
    if rng.random_bool(0.06) {
        pips.push((FUZZ_HYBRIDS[rng.random_range(0..FUZZ_HYBRIDS.len())], 1));
    }
    pips
}

/// `mana <op> <cost>`: core from lane symbols, hybrids kept as strings (bind resolves), cmc = pip
/// sum (X contributes 0) plus a little generic. Query cost spans all colors, not card-limited.
fn fuzz_leaf_mana_cost(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    let pips = fuzz_mana_pips(rng, 0b1_1111);
    let mut core = 0u64;
    let mut hybrids: Vec<(String, u8)> = Vec::new();
    let mut cmc = 0.0f32;
    for &(sym, n) in &pips {
        match super::mana_lane(sym) {
            Some(lane) => {
                core = super::lane_add(core, lane, n);
                if sym != "X" {
                    cmc += f32::from(n);
                }
            }
            None => {
                hybrids.push((sym.to_string(), n));
                cmc += f32::from(n);
            }
        }
    }
    cmc += f32::from(rng.random_range(0..=2u8)); // generic mana: cmc only, no pip
    hybrids.sort();
    let op = FUZZ_OPS[rng.random_range(0..FUZZ_OPS.len())];
    FuzzSpec::Leaf(FuzzLeaf::ManaCost { op, core, hybrids, cmc })
}

/// `devotion <op> <pips>`: 1-2 colors (occasionally colorless C), each 1-3 pips, in the low six lanes.
fn fuzz_leaf_devotion(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    let mut pips = 0u64;
    for _ in 0..rng.random_range(1..=2) {
        let lane = rng.random_range(0..6usize); // WUBRGC
        let n = fuzz_weighted(rng, &[(1u8, 50), (2, 35), (3, 15)]);
        pips = super::lane_add(pips, lane, n);
    }
    let op = FUZZ_OPS[rng.random_range(0..FUZZ_OPS.len())];
    FuzzSpec::Leaf(FuzzLeaf::Devotion { op, pips })
}

fn fuzz_leaf(rng: &mut rand::rngs::SmallRng) -> FuzzSpec {
    match rng.random_range(0..27u8) {
        0 => fuzz_leaf_color(rng),
        1 => fuzz_leaf_type(rng),
        2 => fuzz_leaf_cmc(rng),
        3 => fuzz_leaf_power(rng),
        4 => fuzz_leaf_toughness(rng),
        5 => fuzz_leaf_loyalty(rng),
        6 => fuzz_leaf_rarity(rng),
        7 => fuzz_leaf_collector_number(rng),
        8 => fuzz_leaf_price(rng),
        9 => fuzz_leaf_date(rng),
        10 => fuzz_leaf_year(rng),
        11 => fuzz_leaf_border(rng),
        12 => fuzz_leaf_legality(rng),
        13 => fuzz_leaf_subtype(rng),
        14 => fuzz_leaf_collection(rng, CollField::Keywords, &FUZZ_KEYWORDS),
        15 => fuzz_leaf_collection(rng, CollField::OracleTags, &FUZZ_ORACLE_TAGS),
        16 => fuzz_leaf_collection(rng, CollField::ArtTags, &FUZZ_ART_TAGS),
        17 => fuzz_leaf_collection(rng, CollField::IsTags, &FUZZ_IS_TAGS),
        18 => fuzz_leaf_collection(rng, CollField::FrameData, &FUZZ_FRAME_DATA),
        19 => fuzz_leaf_artist(rng),
        20 => fuzz_leaf_text_contains(rng, TextSearchField::NameLower),
        21 => fuzz_leaf_text_contains(rng, TextSearchField::OracleTextLower),
        22 => fuzz_leaf_text_contains(rng, TextSearchField::FlavorTextLower),
        23 => fuzz_leaf_name_exact(rng),
        24 => fuzz_leaf_mana_cost(rng),
        25 => fuzz_leaf_devotion(rng),
        _ => fuzz_leaf_arith(rng),
    }
}

fn fuzz_gen(rng: &mut rand::rngs::SmallRng, depth: u8) -> FuzzSpec {
    if depth == 0 || rng.random_bool(0.45) {
        return fuzz_leaf(rng);
    }
    match rng.random_range(0..3u8) {
        0 => FuzzSpec::And((0..rng.random_range(2..=4)).map(|_| fuzz_gen(rng, depth - 1)).collect()),
        1 => FuzzSpec::Or((0..rng.random_range(2..=4)).map(|_| fuzz_gen(rng, depth - 1)).collect()),
        _ => {
            let inner = fuzz_gen(rng, depth - 1);
            // ~30% double negation, exercising the Not/Not identity path.
            if rng.random_bool(0.3) {
                FuzzSpec::Not(Box::new(FuzzSpec::Not(Box::new(inner))))
            } else {
                FuzzSpec::Not(Box::new(inner))
            }
        }
    }
}

/// Compounds that deliberately mix a printing-varying leaf with a card-invariant
/// one — the exact `format:A AND date>X` shape from #677's description, with
/// whichever fields the generator supports substituted in.
fn fuzz_targeted(rng: &mut rand::rngs::SmallRng) -> Vec<FuzzSpec> {
    let rar = fuzz_leaf_rarity(rng);
    let bor = fuzz_leaf_border(rng);
    let col = fuzz_leaf_color(rng);
    let typ = fuzz_leaf_type(rng);
    let cmc = fuzz_leaf_cmc(rng);
    let leg1 = fuzz_leaf_legality(rng);
    let leg2 = fuzz_leaf_legality(rng);
    // The newer fields, so their multi-predicate shapes are guaranteed each run rather than left to
    // fuzz_gen's random draws across 14 leaf kinds.
    let pow = fuzz_leaf_power(rng);
    let tou = fuzz_leaf_toughness(rng);
    let price = fuzz_leaf_price(rng);
    let date = fuzz_leaf_date(rng);
    let cn = fuzz_leaf_collector_number(rng);
    let arith = fuzz_leaf_arith(rng);
    vec![
        FuzzSpec::And(vec![leg1.clone(), cmc.clone()]),
        FuzzSpec::And(vec![leg1.clone(), col.clone()]),
        FuzzSpec::And(vec![rar.clone(), typ.clone()]),
        FuzzSpec::And(vec![bor.clone(), cmc.clone()]),
        FuzzSpec::Or(vec![leg2, rar.clone()]),
        FuzzSpec::Not(Box::new(FuzzSpec::And(vec![leg1.clone(), bor.clone()]))),
        FuzzSpec::And(vec![leg1, rar.clone(), col.clone()]),
        FuzzSpec::Or(vec![FuzzSpec::And(vec![rar.clone(), cmc.clone()]), FuzzSpec::And(vec![bor, typ.clone()])]),
        // Two card-level numerics (e.g. cmc=2 AND power=3); correlated card-level pair.
        FuzzSpec::And(vec![cmc.clone(), pow.clone()]),
        FuzzSpec::And(vec![pow, tou]),
        // Printing-varying range field AND a card-invariant one -- the #677 wrong-printing shape.
        FuzzSpec::And(vec![price.clone(), typ]),
        FuzzSpec::And(vec![date, col.clone()]),
        FuzzSpec::And(vec![cn, rar]),
        FuzzSpec::Or(vec![price, cmc]),
        // Arithmetic leaf inside a compound (its own operands already mix card + printing fields).
        FuzzSpec::And(vec![arith, col]),
        // Number of printing-varying predicates in one And drives distinct Y-predicate paths:
        //   0 -> card-invariant, exact card-level answer (all_match promotes);
        //   1 -> a single existence projection + witness-printing selection;
        //   2+ -> the shared-witness path -- ∃p:A(p)∧B(p) != (∃p:A(p))∧(∃p:B(p)), so compile_plane
        //         must decline to compose and fall back to per-printing residual verification.
        // fuzz_gen hits all three probabilistically; guarantee 0 and 2+ here across field mixes.
        FuzzSpec::And(vec![fuzz_leaf_cmc(rng), fuzz_leaf_color(rng)]),                       // 0 printing-varying
        FuzzSpec::And(vec![fuzz_leaf_legality(rng), fuzz_leaf_border(rng)]),                 // 2 existential (legality+border)
        FuzzSpec::And(vec![fuzz_leaf_rarity(rng), fuzz_leaf_border(rng)]),                   // 2 existential (rarity+border)
        FuzzSpec::And(vec![fuzz_leaf_price(rng), fuzz_leaf_date(rng)]),                      // 2 printing-range (price+date)
        FuzzSpec::And(vec![fuzz_leaf_collector_number(rng), fuzz_leaf_price(rng)]),          // 2 printing-range (cn+price)
        FuzzSpec::And(vec![fuzz_leaf_rarity(rng), fuzz_leaf_price(rng)]),                    // existential + range
        FuzzSpec::And(vec![fuzz_leaf_legality(rng), fuzz_leaf_rarity(rng), fuzz_leaf_border(rng)]), // 3 existential
    ]
}

fn fuzz_build_filter(spec: &FuzzSpec) -> FilterExpr {
    match spec {
        FuzzSpec::Leaf(FuzzLeaf::Color { op, mask }) => FilterExpr::ColorCmp { field: ColorField::Colors, op: *op, mask: *mask },
        FuzzSpec::Leaf(FuzzLeaf::Type { op, mask }) => FilterExpr::TypeCmp { mask: *mask, op: *op },
        FuzzSpec::Leaf(FuzzLeaf::Cmc { op, val }) => FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: *op, rhs: NumExpr::Const(*val) },
        FuzzSpec::Leaf(FuzzLeaf::Power { op, val }) => FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Power), op: *op, rhs: NumExpr::Const(*val) },
        FuzzSpec::Leaf(FuzzLeaf::Toughness { op, val }) => FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Toughness), op: *op, rhs: NumExpr::Const(*val) },
        FuzzSpec::Leaf(FuzzLeaf::Loyalty { op, val }) => FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Loyalty), op: *op, rhs: NumExpr::Const(*val) },
        FuzzSpec::Leaf(FuzzLeaf::Rarity { op, val }) => FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: *op, rhs: NumExpr::Const(*val) },
        FuzzSpec::Leaf(FuzzLeaf::CollectorNumber { op, val }) => FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::CollectorNumberInt), op: *op, rhs: NumExpr::Const(*val) },
        FuzzSpec::Leaf(FuzzLeaf::Price { op, val }) => FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::PriceUsd), op: *op, rhs: NumExpr::Const(*val) },
        FuzzSpec::Leaf(FuzzLeaf::Date { op, value }) => FilterExpr::DateCmp { op: *op, value: *value },
        FuzzSpec::Leaf(FuzzLeaf::Year { op, year }) => FilterExpr::YearCmp { op: *op, year: *year },
        FuzzSpec::Leaf(FuzzLeaf::Arith { shape, op }) => {
            let (lhs, rhs) = fuzz_arith_pair(*shape);
            FilterExpr::NumericCmp { lhs, op: *op, rhs }
        }
        FuzzSpec::Leaf(FuzzLeaf::Collection { field, op, value }) => {
            FilterExpr::CollectionCmp { field: *field, op: *op, value: value.clone(), value_id: None }
        }
        FuzzSpec::Leaf(FuzzLeaf::Artist { word }) => {
            FilterExpr::TextContains { field: TextSearchField::ArtistLower, word: word.clone() }
        }
        FuzzSpec::Leaf(FuzzLeaf::TextContains { field, needle }) => {
            FilterExpr::TextContains { field: *field, word: needle.clone() }
        }
        FuzzSpec::Leaf(FuzzLeaf::NameExact { op, value }) => {
            FilterExpr::TextExact { field: TextField::NameLower, op: *op, value: value.clone() }
        }
        FuzzSpec::Leaf(FuzzLeaf::ManaCost { op, core, hybrids, cmc }) => FilterExpr::ManaCostCmp {
            op: *op,
            core: *core,
            hybrids: hybrids.clone(),
            hybrid_ids: Vec::new(), // bind() fills this from `hybrids` when non-empty
            cmc: *cmc,
        },
        FuzzSpec::Leaf(FuzzLeaf::Devotion { op, pips }) => FilterExpr::Devotion { op: *op, pips: *pips },
        FuzzSpec::Leaf(FuzzLeaf::Border { value }) => FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: value.clone() },
        FuzzSpec::Leaf(FuzzLeaf::Legality { shift, expected }) => FilterExpr::Legality { shift: *shift, expected: *expected },
        FuzzSpec::And(v) => FilterExpr::And(v.iter().map(fuzz_build_filter).collect()),
        FuzzSpec::Or(v) => FilterExpr::Or(v.iter().map(fuzz_build_filter).collect()),
        FuzzSpec::Not(b) => FilterExpr::Not(Box::new(fuzz_build_filter(b))),
    }
}

/// Build the filter for `spec` and bind it against the store's vocabs -- resolves CollectionCmp
/// value ids (and ArtistMatch/ManaCostCmp when those land), a no-op for the numeric/card-invariant
/// leaves. The real query path binds before matching, and both the engine and the reference
/// evaluator (`FilterExpr::matches`) need the resolved ids, so every built filter goes through here.
fn fuzz_bound_filter(spec: &FuzzSpec, archived: &Archived<CardData>) -> FilterExpr {
    let mut f = fuzz_build_filter(spec);
    f.bind(
        &archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab,
        &archived.mana_vocab, &archived.indexes.flavor, &archived.strings,
    );
    f
}

fn fuzz_op_str(op: CmpOp) -> &'static str {
    match op {
        CmpOp::Eq => "==", CmpOp::Ne => "!=", CmpOp::Lt => "<", CmpOp::Le => "<=", CmpOp::Gt => ">", CmpOp::Ge => ">=",
    }
}
fn fuzz_num_field_str(f: NumField) -> &'static str {
    match f {
        NumField::Cmc => "cmc", NumField::Power => "power", NumField::Toughness => "toughness", NumField::Loyalty => "loyalty",
        NumField::RarityInt => "rarity", NumField::CollectorNumberInt => "cn", NumField::EdhrEc => "edhrec",
        NumField::PriceUsd => "usd", NumField::PriceEur => "eur", NumField::PriceTix => "tix", NumField::PreferScore => "prefer",
    }
}
fn fuzz_num_expr_str(e: &NumExpr) -> String {
    match e {
        NumExpr::Const(c) => format!("{c}"),
        NumExpr::Field(f) => fuzz_num_field_str(*f).to_string(),
        NumExpr::Arith(l, o, r) => {
            let os = match o { ArithOp::Add => "+", ArithOp::Sub => "-", ArithOp::Mul => "*", ArithOp::Div => "/" };
            format!("({} {os} {})", fuzz_num_expr_str(l), fuzz_num_expr_str(r))
        }
    }
}
fn fuzz_describe(spec: &FuzzSpec) -> String {
    match spec {
        FuzzSpec::Leaf(FuzzLeaf::Color { op, mask }) => format!("colors{}{:#07b}", fuzz_op_str(*op), mask),
        FuzzSpec::Leaf(FuzzLeaf::Type { op, mask }) => format!("types{}{:#018b}", fuzz_op_str(*op), mask),
        FuzzSpec::Leaf(FuzzLeaf::Cmc { op, val }) => format!("cmc{}{val}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Power { op, val }) => format!("power{}{val}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Toughness { op, val }) => format!("toughness{}{val}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Loyalty { op, val }) => format!("loyalty{}{val}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Rarity { op, val }) => format!("rarity{}{val}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::CollectorNumber { op, val }) => format!("cn{}{val}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Price { op, val }) => format!("usd{}{val}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Date { op, value }) => format!("date{}{value}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Year { op, year }) => format!("year{}{year}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Arith { shape, op }) => {
            let (lhs, rhs) = fuzz_arith_pair(*shape);
            format!("{}{}{}", fuzz_num_expr_str(&lhs), fuzz_op_str(*op), fuzz_num_expr_str(&rhs))
        }
        FuzzSpec::Leaf(FuzzLeaf::Collection { field, op, value }) => {
            let f = match field {
                CollField::Subtypes => "subtypes", CollField::Keywords => "keywords", CollField::OracleTags => "oracle_tags",
                CollField::ArtTags => "art_tags", CollField::IsTags => "is_tags", CollField::FrameData => "frame_data",
            };
            format!("{f}{}{value}", fuzz_op_str(*op))
        }
        FuzzSpec::Leaf(FuzzLeaf::Artist { word }) => format!("artist:{word}"),
        FuzzSpec::Leaf(FuzzLeaf::TextContains { field, needle }) => {
            let f = match field {
                TextSearchField::NameLower => "name",
                TextSearchField::OracleTextLower => "oracle",
                TextSearchField::FlavorTextLower => "flavor",
                TextSearchField::ArtistLower => "artist",
            };
            format!("{f}:{needle}")
        }
        FuzzSpec::Leaf(FuzzLeaf::NameExact { op, value }) => format!("name{}{value}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::ManaCost { op, core, hybrids, cmc }) => {
            format!("mana{}(core={core:#018x}, hyb={hybrids:?}, cmc={cmc})", fuzz_op_str(*op))
        }
        FuzzSpec::Leaf(FuzzLeaf::Devotion { op, pips }) => format!("devotion{}{pips:#014x}", fuzz_op_str(*op)),
        FuzzSpec::Leaf(FuzzLeaf::Border { value }) => format!("border=={value}"),
        FuzzSpec::Leaf(FuzzLeaf::Legality { shift, expected }) => format!("legality(shift={shift:?}, expected={expected:#04b})"),
        FuzzSpec::And(v) => format!("AND({})", v.iter().map(fuzz_describe).collect::<Vec<_>>().join(", ")),
        FuzzSpec::Or(v) => format!("OR({})", v.iter().map(fuzz_describe).collect::<Vec<_>>().join(", ")),
        FuzzSpec::Not(b) => format!("NOT({})", fuzz_describe(b)),
    }
}

// A random card_types bitmask; shared by fuzz_store and the type leaf so both
// draw from the same set of single- and multi-bit type shapes.
fn fuzz_type_bits(rng: &mut rand::rngs::SmallRng) -> u16 {
    const TYPES: [u16; 8] = [
        TYPE_CREATURE, TYPE_INSTANT, TYPE_SORCERY, TYPE_ARTIFACT, TYPE_ENCHANTMENT, TYPE_LAND,
        TYPE_CREATURE | TYPE_ARTIFACT, TYPE_CREATURE | TYPE_LEGENDARY,
    ];
    TYPES[rng.random_range(0..TYPES.len())]
}

/// A random small store: 5-15 cards, 1-3 printings each, card-invariant
/// colors/types/cmc, printing-varying rarity/border, and legality across 3
/// formats with a deliberate mix of non-divergent cards (all printings share
/// one word, matching the card-level word) and divergent cards
/// (`legality_divergent = true` plus printings that genuinely disagree on at
/// least one format).
fn fuzz_store(rng: &mut rand::rngs::SmallRng) -> CardData {
    let ncards = rng.random_range(5..=15usize);
    fuzz_store_n(rng, ncards)
}

/// `fuzz_store` with an explicit card count — the large-store variant (item 9) passes a few
/// thousand so `maybe_broad` holds and `run_query_streamed` / `run_query_streamed_popcount` fire.
fn fuzz_store_n(rng: &mut rand::rngs::SmallRng, ncards: usize) -> CardData {
    // Concrete legality word: an independent random status per format.
    fn rand_word(rng: &mut rand::rngs::SmallRng) -> u64 {
        FUZZ_SHIFTS.iter().fold(0u64, |w, &s| w | (rng.random_range(0..4u64) << s))
    }
    const BORDERS: [Option<&str>; 6] = [Some("black"), Some("white"), Some("borderless"), Some("gold"), Some("yellow"), None];

    let mut vocab = VocabInterner::new();
    // Artists live in their own vocab (not the shared collection vocab), matched via ArtistMatch.
    let mut artist_vocab = VocabInterner::new();
    // Mana hybrid vocab (id = index), shared across cards; bind() resolves query hybrids against it.
    let mut mana_vocab: Vec<String> = Vec::new();
    // String table for oracle/flavor text (interned here, in the card/printing loops) and borders
    // (interned in the application loop). Assigned to data.strings after store_of returns.
    let mut interner = Interner::new();
    let corpus = text_corpus();
    let mut cards: Vec<OracleCard> = Vec::with_capacity(ncards);
    let mut counts: Vec<usize> = Vec::with_capacity(ncards);
    // Per-printing (rarity, border, legality word, collector number, price cents, released_at),
    // flat in card/printing order, applied after store_of lays out the printings.
    type PMeta = (Option<u8>, Option<&'static str>, u64, Option<u16>, Option<u32>, u32);
    let mut pmeta: Vec<PMeta> = Vec::new();
    // Printing-space collections + artist vid + flavor text id, same flat card/printing order as
    // pmeta. Interned here (while the vocabs/interner are in scope) but only applied to the printings
    // after store_of returns.
    let mut art_meta: Vec<Vec<u16>> = Vec::new();
    let mut is_meta: Vec<Vec<u16>> = Vec::new();
    let mut frame_meta: Vec<Vec<u16>> = Vec::new();
    let mut artist_meta: Vec<u16> = Vec::new();
    let mut flavor_meta: Vec<u32> = Vec::new();

    for i in 0..ncards {
        let mut card = stub_card(i as u128 + 1, fuzz_type_bits(rng), &[], &mut vocab);
        card.card_colors = rng.random_range(0..32u8);
        // cmc on the corpus distribution (peaks at 2-3). Power/toughness/loyalty are *derived* from
        // it so bigger stats cost more and P~T track each other, and are present only on the
        // matching card kind: creatures have power/toughness, planeswalkers have loyalty.
        let cmc = fuzz_weighted(rng, &[(0u8, 1), (1, 3), (2, 7), (3, 8), (4, 6), (5, 4), (6, 2), (7, 1), (8, 1)]);
        card.cmc = Some(cmc);
        card.edhrec_rank = Some(rng.random_range(1..=30_000u32)); // distinct-ish, gives the sort keys something to order
        // creature 55% / planeswalker 6% / neither 39%. Planeswalkers are over-represented vs the
        // corpus's ~1% so loyalty predicates actually match something under the fuzzer's small stores.
        match fuzz_weighted(rng, &[(0u8, 55), (1, 6), (2, 39)]) {
            0 => {
                let power = (i32::from(cmc) - 1 + rng.random_range(-1..=2)).clamp(0, 12) as i8;
                let toughness = (i32::from(power) + rng.random_range(-1..=2)).clamp(1, 13) as i8;
                card.creature_power = Some(power);
                card.creature_toughness = Some(toughness);
                // Subtypes correlate with the creature type (real data: creature subtypes only
                // appear on creatures, modulo the rare kindred/tribal tail we don't model). Set the
                // bit so `t:creature` and `type:human` co-select. 1-2 subtypes, biased frequency.
                card.card_types |= TYPE_CREATURE;
                let n = rng.random_range(1..=2);
                card.card_subtypes = vocab_ids(&mut vocab, &fuzz_collection_picks(rng, &FUZZ_SUBTYPES, n));
            }
            1 => card.planeswalker_loyalty = Some((2 + i32::from(cmc) / 2 + rng.random_range(0..=2)).clamp(2, 8) as u8),
            _ => {}
        }
        // Text from a real corpus triple. Name is always present. Empty oracle (vanilla) is
        // conditioned on the creature bit at the real rate; every other card gets a non-empty oracle
        // (loop past the ~1% corpus empties). Textless cards intern "" (never NONE_STR), matching the
        // loader, so an oracle-text search evaluates False on them rather than Null.
        // Both name fields from one corpus name: card_name_lower (InlineStr) is what contains()
        // reads; card_name_id is the interned id NameMatch re-keys to after memoization, so it must
        // distinguish names (leaving it NONE_STR collapses every name to one id and over-matches).
        let name = corpus[rng.random_range(0..corpus.len())].0;
        card.card_name_lower = InlineStr::from_str(name);
        card.card_name_id = interner.intern(name.to_string());
        let vanilla = card.card_types & TYPE_CREATURE != 0 && rng.random_bool(VANILLA_CREATURE_FRAC);
        let oracle = if vanilla {
            ""
        } else {
            loop {
                let o = corpus[rng.random_range(0..corpus.len())].1;
                if !o.is_empty() {
                    break o;
                }
            }
        };
        card.oracle_text_lower_id = interner.intern(oracle.to_string());
        // Mana cost: colored pips only in the card's colors (cost colors ⊆ card colors, the real
        // relationship) so identity covers them; occasional {C}/{X}/hybrid. Devotion is gated to
        // permanents (loader rule). The devotion-plane build asserts identity covers every colored
        // devotion lane, so derive identity from the devotion lanes (C exempt). cmc mirrors card.cmc.
        let mut mc = mana_cost_of(&fuzz_mana_pips(rng, card.card_colors), &mut mana_vocab);
        if card.card_types & super::PERMANENT_TYPES == 0 {
            mc.devotion = 0;
        }
        mc.cmc = f32::from(cmc);
        let mut ident = card.card_colors;
        for lane in 0..5usize {
            if super::lane_get(mc.devotion, lane) > 0 {
                ident |= 1u8 << lane;
            }
        }
        card.card_color_identity = ident;
        card.mana_cost = mc;
        // Keywords and oracle tags apply across all card types (unlike subtypes). vocab_ids sorts
        // and dedups into the id-sorted set the engine binary-searches at match time. (Counts are
        // pulled into locals so the fn call doesn't hold `rng` while `random_range` also needs it.)
        let (nk, no) = (rng.random_range(0..=3), rng.random_range(0..=4));
        card.card_keywords = vocab_ids(&mut vocab, &fuzz_collection_picks(rng, &FUZZ_KEYWORDS, nk));
        card.card_oracle_tags = vocab_ids(&mut vocab, &fuzz_collection_picks(rng, &FUZZ_ORACLE_TAGS, no));
        let divergent = rng.random_bool(0.4);
        card.legality_divergent = divergent;
        // Printings per card is heavily skewed in the corpus (~41% have exactly one, long tail);
        // sample that shape rather than uniform 1-3. ~1% are basic-land-like with piles of printings
        // (the corpus tail runs to ~850), exercising long per-card ranges and heavy artwork dedup.
        // Divergent cards need >= 2 printings to actually disagree.
        let npr = if rng.random_bool(0.01) {
            rng.random_range(30..=200usize)
        } else {
            let n = fuzz_weighted(rng, &[(1usize, 41), (2, 22), (3, 11), (4, 9), (5, 5), (6, 3), (7, 2), (8, 1), (10, 1), (15, 1)]);
            if divergent { n.max(2) } else { n }
        };
        counts.push(npr);

        let mut words: Vec<u64> = (0..npr).map(|_| rand_word(rng)).collect();
        if divergent {
            // Force a real disagreement at one format between printings 0 and 1,
            // so the "divergent flag but printings happen to agree" degenerate
            // case is not all this branch produces.
            let ds = FUZZ_SHIFTS[rng.random_range(0..FUZZ_SHIFTS.len())];
            let s0 = rng.random_range(0..4u64);
            let s1 = { let c = rng.random_range(0..4u64); if c == s0 { (s0 + 1) & 0b11 } else { c } };
            words[0] = (words[0] & !(0b11 << ds)) | (s0 << ds);
            words[1] = (words[1] & !(0b11 << ds)) | (s1 << ds);
            // Card-level word is unused for divergent cards (eval reads per-printing).
            card.card_legalities = words[0];
        } else {
            // Non-divergent: every printing shares the card-level word (the
            // real-data invariant the exact card-level plane relies on).
            let w = words[0];
            for x in words.iter_mut() {
                *x = w;
            }
            card.card_legalities = w;
        }

        for &word in &words {
            // Rarity weighted like the corpus (rare > common > uncommon > mythic); 15% NULL for
            // Null-propagation coverage. Collector number spread 1-300 (mostly present). Price 16%
            // NULL, heavy sub-$2 tail. Release date recent-skewed.
            let rarity = if rng.random_bool(0.85) {
                Some(fuzz_weighted(rng, &[(0u8, 28), (1, 24), (2, 37), (3, 9), (4, 2)]))
            } else {
                None
            };
            let border = BORDERS[rng.random_range(0..BORDERS.len())];
            let cn = if rng.random_bool(0.95) { Some(rng.random_range(1..=300u16)) } else { None };
            let price = if rng.random_bool(0.16) {
                None
            } else {
                Some(match fuzz_weighted(rng, &[(0u8, 50), (1, 25), (2, 15), (3, 10)]) {
                    0 => rng.random_range(5..=50u32),
                    1 => rng.random_range(50..=200u32),
                    2 => rng.random_range(200..=1000u32),
                    _ => rng.random_range(1000..=6000u32),
                })
            };
            let year = fuzz_year_value(rng);
            let released = year * 10_000 + rng.random_range(1..=12u32) * 100 + rng.random_range(1..=28u32);
            pmeta.push((rarity, border, word, cn, price, released));
            // Printing-space collections (sorted+deduped by vocab_ids) + one artist per printing
            // (real data has no NULL artists). frame_data keeps "2015" dominant so the corpus-scale
            // store exercises the thresholded-index drop.
            let (na, ni, nf) = (rng.random_range(0..=4), rng.random_range(0..=2), rng.random_range(0..=2));
            art_meta.push(vocab_ids(&mut vocab, &fuzz_collection_picks(rng, &FUZZ_ART_TAGS, na)));
            is_meta.push(vocab_ids(&mut vocab, &fuzz_collection_picks(rng, &FUZZ_IS_TAGS, ni)));
            frame_meta.push(vocab_ids(&mut vocab, &fuzz_collection_picks(rng, &FUZZ_FRAME_DATA, nf)));
            artist_meta.push(artist_vocab.intern(fuzz_weighted(rng, &FUZZ_ARTISTS).to_string()).unwrap());
            // Flavor is printing-varying (a fresh corpus draw per printing, ~half empty like the
            // corpus) so the printing-space FlavorMatch path selects among differing printings.
            flavor_meta.push(interner.intern(corpus[rng.random_range(0..corpus.len())].2.to_string()));
        }
        cards.push(card);
    }

    let mut data = store_of(cards, &counts, vocab);
    data.artist_vocab = artist_vocab.strings;
    data.mana_vocab = mana_vocab;
    for (idx, (rarity, border, word, cn, price, released)) in pmeta.into_iter().enumerate() {
        data.printings[idx].card_rarity_int = rarity;
        data.printings[idx].card_border_id = border.map_or(NONE_STR, |b| interner.intern(b.to_string()));
        data.printings[idx].card_legalities = word;
        data.printings[idx].collector_number_int = cn;
        data.printings[idx].price_usd = price;
        data.printings[idx].released_at_int = Some(released);
        data.printings[idx].card_art_tags = std::mem::take(&mut art_meta[idx]);
        data.printings[idx].card_is_tags = std::mem::take(&mut is_meta[idx]);
        data.printings[idx].card_frame_data = std::mem::take(&mut frame_meta[idx]);
        data.printings[idx].card_artist_vid = artist_meta[idx];
        data.printings[idx].flavor_text_lower_id = flavor_meta[idx];
    }
    data.strings = interner.strings;
    // store_of built indexes from the placeholder printings and left the numeric/range indexes at
    // empty defaults; rebuild everything the mutated fields feed. The range indexes are load-bearing
    // for correctness, not just coverage: an empty range index narrows a matching predicate to the
    // empty (tight) set, which would make run_query wrongly return nothing.
    data.indexes.rarity = build_rarity_index(&data.printings, &data.offsets);
    // Collection narrowing indexes — load-bearing like the range indexes above: an unbuilt index
    // narrows a populated predicate to the empty set, disagreeing with the residual `matches` path.
    // frame_data is thresholded (drops the dominant "2015"), matching the engine's build.
    data.indexes.subtypes = build_tag_index(&data.cards, &data.coll_vocab, |c| &c.card_subtypes);
    data.indexes.keywords = build_tag_index(&data.cards, &data.coll_vocab, |c| &c.card_keywords);
    data.indexes.oracle_tags = build_tag_index(&data.cards, &data.coll_vocab, |c| &c.card_oracle_tags);
    data.indexes.art_tags = build_tag_index(&data.printings, &data.coll_vocab, |p| &p.card_art_tags);
    data.indexes.is_tags = build_tag_index(&data.printings, &data.coll_vocab, |p| &p.card_is_tags);
    data.indexes.frame_data = build_thresholded_tag_index(&data.printings, &data.coll_vocab, |p| &p.card_frame_data);
    data.indexes.artists = build_artist_index(&data.printings, data.artist_vocab.len());
    // Text narrowing indexes — same load-bearing property. name/oracle drive trigram + bigram
    // narrowing and the full-scan memoization; flavor is the printing-space CSR bind() resolves against.
    data.indexes.name_trigram = build_trigram_index(&data.cards, |c| c.card_name_lower.as_str());
    data.indexes.name_bigrams = build_name_bigram_index(&data.cards);
    data.indexes.oracle_trigram = build_oracle_text_index(&data.cards, &data.strings);
    data.indexes.flavor = build_flavor_index(&data.printings, &data.strings);
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data.indexes.legal_divergent = build_divergent_ids(&data.cards);
    data.indexes.sort_perms = build_sort_permutations(&data.cards, &data.printings, &data.offsets);
    data.indexes.cmc = build_numeric_index(&data.cards, |c| c.cmc.map(i16::from));
    data.indexes.power = build_numeric_index(&data.cards, |c| c.creature_power.map(i16::from));
    data.indexes.toughness = build_numeric_index(&data.cards, |c| c.creature_toughness.map(i16::from));
    data.indexes.released_at = build_range_index(&data.printings, |p| p.released_at_int);
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    data.indexes.collector_number = build_range_index(&data.printings, |p| p.collector_number_int.map(u32::from));
    data
}

/// Trusted brute-force count for the given `unique` mode: `matches` per printing,
/// no planes, no all_match. Mirrors `card_match_count`'s mode semantics exactly
/// (printing: matching printings; card: cards with >=1 matching printing;
/// artwork: per-card distinct illustration ids among matching printings, summed).
fn fuzz_reference_total(archived: &Archived<CardData>, f: &FilterExpr, mode: &str) -> usize {
    let strings = &archived.strings;
    let mut total = 0usize;
    for cid in 0..archived.cards.len() {
        let card = &archived.cards[cid];
        let start = u32::from(archived.offsets[cid]) as usize;
        let end = u32::from(archived.offsets[cid + 1]) as usize;
        match mode {
            "printing" => {
                for pid in start..end {
                    if f.matches(card, &archived.printings[pid], strings) {
                        total += 1;
                    }
                }
            }
            "artwork" => {
                let mut ills: Vec<u128> = Vec::new();
                for pid in start..end {
                    if f.matches(card, &archived.printings[pid], strings) {
                        let ill = u128::from(archived.printings[pid].illustration_id);
                        if !ills.contains(&ill) {
                            ills.push(ill);
                        }
                    }
                }
                total += ills.len();
            }
            _ => {
                if (start..end).any(|pid| f.matches(card, &archived.printings[pid], strings)) {
                    total += 1;
                }
            }
        }
    }
    total
}

/// Run one (store, filter, mode) case at the given sort column/direction through both the unplaned
/// and the plane paths, asserting: (a) total matches the brute-force reference, (b) **every emitted
/// row satisfies the filter** per the trusted evaluator, and (c) a partial page at a middle offset
/// equals the ordered full result's corresponding slice (pagination correctness). `ctx` carries the
/// seed + filter description so a failure reproduces exactly.
fn fuzz_check_case(archived: &Archived<CardData>, spec: &FuzzSpec, ctx: &str, orderby: &str, direction: &str) {
    // >= any possible total, so the offset-0 page is the complete ordered result to slice against.
    let full_limit = archived.printings.len().max(1);
    let ref_filter = fuzz_bound_filter(spec, archived);
    let strings = &archived.strings;
    let ids = |page: &[(&Archived<OracleCard>, &Archived<Printing>)]| -> Vec<u128> {
        page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect()
    };
    let all_satisfy = |page: &[(&Archived<OracleCard>, &Archived<Printing>)], label: &str, mode: &str| {
        for &(card, p) in page {
            assert!(
                ref_filter.matches(card, p, strings),
                "{label} emitted a row that does not satisfy the filter (mode={mode}, orderby={orderby}, dir={direction}, scryfall_id={}, {ctx})",
                u128::from(p.scryfall_id),
            );
        }
    };

    for mode in ["card", "printing", "artwork"] {
        let expected = fuzz_reference_total(archived, &ref_filter, mode);
        let unique_is_card = mode == "card";

        // #702 PR1: the standalone cardinality estimator's SOUND bounds must
        // bracket the mode="card" reference count for every query. `ref_filter`
        // is the full bound filter (no plane split — matches fuzz_reference_total).
        if mode == "card" {
            let c = super::estimator::estimate_cardinality(&ref_filter, &archived.indexes, &archived.offsets);
            assert!(
                (c.lo as usize) <= expected && expected <= (c.hi as usize),
                "estimator bounds unsound: mode=card expected={expected} bounds=[{},{}] est={} ({ctx})",
                c.lo, c.hi, c.est,
            );
        }

        // Full page (offset 0): total + every row satisfies. Unplaned = raw filter, plane = None;
        // plane path = split_planes + the promoted plane (unique_is_card matches the real caller).
        let mut plain = fuzz_bound_filter(spec, archived);
        let (t0, p0) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, strings,
            &mut plain, None, mode, "default", orderby, direction, full_limit, 0, &archived.indexes,
        );
        let (pe, mut residual) = split_planes(
            fuzz_bound_filter(spec, archived), &archived.indexes.planes, &archived.indexes.oracle_trigram.words, unique_is_card,
        );
        let (t1, p1) = run_query(
            &archived.cards, &archived.printings, &archived.offsets, strings,
            &mut residual, pe.as_ref(), mode, "default", orderby, direction, full_limit, 0, &archived.indexes,
        );

        assert_eq!(t0, expected, "unplaned total mismatch (mode={mode}, orderby={orderby}, dir={direction}, {ctx})");
        assert_eq!(t1, expected, "plane-path total mismatch (mode={mode}, orderby={orderby}, dir={direction}, {ctx})");
        assert_eq!(p0.len(), t0, "unplaned page truncated unexpectedly (mode={mode}, {ctx})");
        assert_eq!(p1.len(), t1, "plane-path page truncated unexpectedly (mode={mode}, {ctx})");
        all_satisfy(&p0, "unplaned", mode);
        all_satisfy(&p1, "plane path", mode);

        // Pagination: a partial page at a middle offset must be exactly the ordered full result's
        // [offset, offset+limit) slice (same query, so deterministic), and each row still satisfies.
        if expected >= 3 {
            let offset = expected / 3;
            let plimit = ((expected - offset) / 2).max(1);
            let (full0, full1) = (ids(&p0), ids(&p1));

            let mut plain_pg = fuzz_bound_filter(spec, archived);
            let (_, pg0) = run_query(
                &archived.cards, &archived.printings, &archived.offsets, strings,
                &mut plain_pg, None, mode, "default", orderby, direction, plimit, offset, &archived.indexes,
            );
            assert_eq!(
                ids(&pg0), full0[offset..offset + plimit].to_vec(),
                "unplaned pagination slice mismatch (mode={mode}, orderby={orderby}, dir={direction}, offset={offset}, {ctx})",
            );
            all_satisfy(&pg0, "unplaned paginated", mode);

            let (pe_pg, mut residual_pg) = split_planes(
                fuzz_bound_filter(spec, archived), &archived.indexes.planes, &archived.indexes.oracle_trigram.words, unique_is_card,
            );
            let (_, pg1) = run_query(
                &archived.cards, &archived.printings, &archived.offsets, strings,
                &mut residual_pg, pe_pg.as_ref(), mode, "default", orderby, direction, plimit, offset, &archived.indexes,
            );
            assert_eq!(
                ids(&pg1), full1[offset..offset + plimit].to_vec(),
                "plane pagination slice mismatch (mode={mode}, orderby={orderby}, dir={direction}, offset={offset}, {ctx})",
            );
            all_satisfy(&pg1, "plane paginated", mode);
        }
    }
}

/// Row-identity-only check: assert every emitted page row satisfies the filter, for both the
/// unplaned and plane paths. O(page), with no O(printings) reference-total pass — cheap enough to
/// run thousands of times against a corpus-scale fixture. Catches false positives (a returned row
/// that doesn't match); the O(n) `fuzz_check_case` guards false negatives (missing matches).
#[allow(clippy::too_many_arguments)]
fn fuzz_check_row_identity(
    archived: &Archived<CardData>, spec: &FuzzSpec, ctx: &str, orderby: &str, direction: &str, mode: &str, limit: usize, offset: usize,
) {
    let f = fuzz_bound_filter(spec, archived);
    let strings = &archived.strings;
    let check = |page: &[(&Archived<OracleCard>, &Archived<Printing>)], label: &str| {
        for &(card, p) in page {
            assert!(
                f.matches(card, p, strings),
                "{label} emitted a non-matching row (mode={mode}, orderby={orderby}, dir={direction}, offset={offset}, scryfall_id={}, {ctx})",
                u128::from(p.scryfall_id),
            );
        }
    };
    let mut plain = fuzz_bound_filter(spec, archived);
    let (_, p0) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, strings,
        &mut plain, None, mode, "default", orderby, direction, limit, offset, &archived.indexes,
    );
    check(&p0, "unplaned");
    let (pe, mut residual) = split_planes(
        fuzz_bound_filter(spec, archived), &archived.indexes.planes, &archived.indexes.oracle_trigram.words, mode == "card",
    );
    let (_, p1) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, strings,
        &mut residual, pe.as_ref(), mode, "default", orderby, direction, limit, offset, &archived.indexes,
    );
    check(&p1, "plane path");
}

/// #677: differential row-identity fuzzer. Unlike the surrounding `total`-only
/// parity tests, this asserts the literal `(card, printing)` rows `run_query`
/// emits are each accepted by the trusted per-printing evaluator — the check
/// that catches a wrong-printing-of-a-matching-card bug like #676. Expected to
/// pass clean (card-invariant fields are safe by construction; border/rarity/
/// legality planes are all kept loose or existence-only); it is a regression
/// guard, not a live-bug finder.
///
/// #696 expanded its coverage: numeric/date/price/collector-number and arithmetic
/// leaves (on rough real-card distributions with the cross-field correlations —
/// power/toughness only on creatures, loyalty only on planeswalkers, stats
/// tracking cmc); every sort column and both directions; a paginated slice check
/// (the page must equal the ordered full result's window); and a large-store pass
/// so `run_query_streamed` / `run_query_streamed_popcount` fire (`maybe_broad`).
#[test]
fn fuzz_row_identity_matches_reference() {
    use rand::SeedableRng;
    const NUM_SEEDS: u64 = 96;
    const RANDOM_FILTERS_PER_STORE: usize = 10;
    const MAX_DEPTH: u8 = 3;
    // edhrec/name/cmc have sort permutations (streamed path + inverse perms); usd has none, so it
    // falls to the gathered, printing-keyed path. `desc` exercises the inverse perms / descending key.
    const SORTS: [&str; 4] = ["edhrec", "name", "cmc", "usd"];
    let pick_order = |rng: &mut rand::rngs::SmallRng| (SORTS[rng.random_range(0..SORTS.len())], if rng.random_bool(0.5) { "desc" } else { "asc" });

    // Small stores: broad predicate + structural combinatorics through the gather / small-total paths.
    for seed in 0..NUM_SEEDS {
        let mut rng = rand::rngs::SmallRng::seed_from_u64(seed);
        let data = fuzz_store(&mut rng);
        let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
        let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

        // Targeted compounds first (the exact bug shape), then purely random trees.
        let mut specs = fuzz_targeted(&mut rng);
        for _ in 0..RANDOM_FILTERS_PER_STORE {
            specs.push(fuzz_gen(&mut rng, MAX_DEPTH));
        }
        for spec in &specs {
            let (orderby, direction) = pick_order(&mut rng);
            let ctx = format!("seed={seed}, filter={}", fuzz_describe(spec));
            fuzz_check_case(archived, spec, &ctx, orderby, direction);
        }
    }

    // Corpus-like fixture on the real distributions, built ONCE and amortized across thousands of
    // queries. Sized well above the fast-path thresholds (3x STREAM_MIN_MATCHES) so random broad-ish
    // filters reliably engage the production paths, not just maximally-broad ones: `maybe_broad`
    // holds (`run_query_streamed`), a pure Color+Type card query folds to `True` and hits
    // `run_query_streamed_popcount`, and a broad range predicate under unique=printing crosses
    // k > STREAM_MIN_MATCHES into #695's fastpath.
    //
    // Not full 30k/100k scale: `run_query` computes `total` on every call, so its cost is O(cards)
    // *per query* regardless of `limit` -- thousands of queries at 30k is ~40s in a debug (CI) build.
    // 6k is the point that keeps every path exercised while thousands of varied queries stay a few
    // seconds. Split into a modest set of O(cards) full checks (total-parity + pagination -- the
    // false-negative guard) and a large set of row-identity-only checks (false-positive guard).
    const CORPUS_CARDS: usize = 6_000;
    const CORPUS_FULL_CHECKS: usize = 24;
    const CORPUS_ROWID_CHECKS: usize = 2_500;
    let mut rng = rand::rngs::SmallRng::seed_from_u64(9_999);
    let data = fuzz_store_n(&mut rng, CORPUS_CARDS);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let modes = ["card", "printing", "artwork"];

    // Full checks: deliberate fast-path shapes (popcount, #695 broad-range) plus random trees.
    let mut full_specs = vec![
        FuzzSpec::And(vec![fuzz_leaf_color(&mut rng), fuzz_leaf_type(&mut rng)]), // -> True (card-mode popcount path)
        FuzzSpec::Leaf(FuzzLeaf::Price { op: CmpOp::Lt, val: 100_000.0 }),        // matches ~all priced -> #695 fastpath
        FuzzSpec::Leaf(FuzzLeaf::Date { op: CmpOp::Gt, value: 1990_0000 }),       // broad date -> range fastpath
    ];
    while full_specs.len() < CORPUS_FULL_CHECKS {
        full_specs.push(fuzz_gen(&mut rng, MAX_DEPTH));
    }
    for spec in &full_specs {
        let (orderby, direction) = pick_order(&mut rng);
        let ctx = format!("corpus-full, filter={}", fuzz_describe(spec));
        fuzz_check_case(archived, spec, &ctx, orderby, direction);
    }

    // Thousands of cheap row-identity checks across varied query / sort / direction / offset / mode.
    for _ in 0..CORPUS_ROWID_CHECKS {
        let spec = fuzz_gen(&mut rng, MAX_DEPTH);
        let (orderby, direction) = pick_order(&mut rng);
        let mode = modes[rng.random_range(0..modes.len())];
        let offset = if rng.random_bool(0.5) { 0 } else { rng.random_range(0..800) };
        let ctx = format!("corpus-rowid, filter={}", fuzz_describe(&spec));
        fuzz_check_row_identity(archived, &spec, &ctx, orderby, direction, mode, 100, offset);
    }
}

/// #702 step 2 (force-plan seam): every physical plan that is *applicable* to a
/// query must return rows identical to `GatheredScan`, the universal fallback /
/// reference. This is the correctness guard for the extraction — it proves the
/// four individually-callable executors (`run_query_with_plan`) agree, so
/// swapping which plan runs a query is a pure performance decision.
///
/// For each fuzz query × prefer × unique mode × sort: compute the `GatheredScan`
/// result (always `Some`) as the reference, then for every other plan call
/// `run_query_with_plan`; if it returns `Some` (it was applicable), assert
/// against the reference on three axes at the full offset-0 page:
///   - `total` equality,
///   - `scryfall_id` multiset equality — same *rows*,
///   - **2-key ordering** equality — the `(primary, edhrec_rank)` value
///     sequence (top 64 bits of `sort_key_bits`).
/// The 2-key value sequence is the ordering-parity contract (#702): plans agree
/// on the two SQL-defined keys they're guaranteed to, and diverge only past
/// them (key 3, prefer_score, and below — see the `PREFERS` comment below and
/// docs/issues/00702). Comparing the 2-key *value* sequence, not the row
/// sequence, is what tolerates that key-3 slop while still catching any real
/// mis-ordering: tied rows share their (key1, key2) value, so the sequence is
/// identical even when they interleave.
///
/// Hand-built specs guarantee P1 (`PrintingRangeScan`) and P2
/// (`PlanePopcountOrder`) coverage — the random generator rarely emits a bare
/// broad range or a fully-True color+type conjunction. A coverage assertion at
/// the end fails if any of the four plans was never exercised.
#[test]
fn force_plan_differential_agreement() {
    use rand::SeedableRng;
    const CORPUS_CARDS: usize = 6_000;
    const MAX_DEPTH: u8 = 3;
    const RANDOM_QUERIES: usize = 150;
    const SORTS: [&str; 4] = ["edhrec", "name", "cmc", "usd"];

    let mut rng = rand::rngs::SmallRng::seed_from_u64(70_202);
    let data = fuzz_store_n(&mut rng, CORPUS_CARDS);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    // (spec, orderby, direction). Hand-built entries pin sorts that guarantee a
    // plan fires: color+type -> True residual + plane (PlanePopcountOrder needs a
    // perm sort in card mode); a broad date over a perm sort -> the range
    // fastpath's walk branch (k > STREAM_MIN); a broad price over usd -> the
    // fastpath's aligned branch. Random trees then broaden the coverage.
    let mut cases: Vec<(FuzzSpec, &str, &str)> = vec![
        (FuzzSpec::And(vec![fuzz_leaf_color(&mut rng), fuzz_leaf_type(&mut rng)]), "edhrec", "asc"),
        (FuzzSpec::And(vec![fuzz_leaf_color(&mut rng), fuzz_leaf_type(&mut rng)]), "name", "desc"),
        (FuzzSpec::Leaf(FuzzLeaf::Date { op: CmpOp::Gt, value: 1990_0000 }), "cmc", "asc"),
        (FuzzSpec::Leaf(FuzzLeaf::Date { op: CmpOp::Gt, value: 1990_0000 }), "edhrec", "desc"),
        (FuzzSpec::Leaf(FuzzLeaf::Price { op: CmpOp::Lt, val: 100_000.0 }), "usd", "asc"),
        (FuzzSpec::Leaf(FuzzLeaf::Price { op: CmpOp::Lt, val: 100_000.0 }), "usd", "desc"),
    ];
    for _ in 0..RANDOM_QUERIES {
        let orderby = SORTS[rng.random_range(0..SORTS.len())];
        let direction = if rng.random_bool(0.5) { "desc" } else { "asc" };
        cases.push((fuzz_gen(&mut rng, MAX_DEPTH), orderby, direction));
    }

    let modes = ["card", "printing", "artwork"];
    let all_plans = [
        PhysicalPlan::PrintingRangeScan,
        PhysicalPlan::PlanePopcountOrder,
        PhysicalPlan::StreamedSelect,
        PhysicalPlan::GatheredScan,
    ];
    let plan_idx = |p: PhysicalPlan| match p {
        PhysicalPlan::PrintingRangeScan => 0,
        PhysicalPlan::PlanePopcountOrder => 1,
        PhysicalPlan::StreamedSelect => 2,
        PhysicalPlan::GatheredScan => 3,
    };
    let mut ran = [0u32; 4];

    // >= any possible total, so the offset-0 page is the complete ordered result.
    let full_limit = archived.printings.len().max(1);
    let strings = &archived.strings;
    // Same rows regardless of tie order.
    let id_multiset = |page: &[(&Archived<OracleCard>, &Archived<Printing>)]| -> Vec<u128> {
        let mut v: Vec<u128> = page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect();
        v.sort_unstable();
        v
    };

    // Ordering-parity contract (#702): plans agree on the SQL-defined keys 1-2
    // (primary sort column, then edhrec_rank) — the top 64 bits of
    // sort_key_bits. Key 3 (prefer_score) and beyond are unspecified across
    // plans: the permutation bakes in the *default* representative's
    // prefer_score, so under a non-default prefer the perm-based plans can order
    // a key-3 tie differently from the gathered path (a known, pre-existing
    // divergence — see docs/issues/00702-engine-plan-selection-layer.md).
    // Comparing the 2-key VALUE sequence is stable across that: tied rows share
    // their (key1, key2) value, so the sequence is identical even when they
    // interleave. Exercised under a default AND a non-default prefer — 2-key
    // parity holds at both (3-key would not, at non-default prefer).
    const PREFERS: [&str; 2] = ["default", "usd_low"];

    for (spec, orderby, direction) in &cases {
        let sort_col = orderby_to_col(orderby);
        let descending = *direction == "desc";
        // (key1, key2) = the top 64 bits of sort_key_bits; drop the low 32 (key 3).
        let key2_seq = |page: &[(&Archived<OracleCard>, &Archived<Printing>)]| -> Vec<u128> {
            page.iter().map(|&(c, p)| sort_key_bits(c, p, sort_col, descending) >> 32).collect()
        };
        for &prefer in &PREFERS {
            for &mode in &modes {
                let unique_is_card = mode == "card";

                // Reference: GatheredScan is always applicable. A fresh bound + split
                // filter per plan call because prepare_candidates mutates the filter.
                let (ref_pe, mut ref_res) = split_planes(
                    fuzz_bound_filter(spec, archived), &archived.indexes.planes, &archived.indexes.oracle_trigram.words, unique_is_card,
                );
                let (ref_total, ref_page) = run_query_with_plan(
                    PhysicalPlan::GatheredScan, &archived.cards, &archived.printings, &archived.offsets, strings,
                    &mut ref_res, ref_pe.as_ref(), mode, prefer, orderby, direction, full_limit, 0, &archived.indexes,
                )
                .expect("GatheredScan is always applicable");
                ran[plan_idx(PhysicalPlan::GatheredScan)] += 1;
                let ref_ids = id_multiset(&ref_page);
                let ref_key2 = key2_seq(&ref_page);

                for &plan in &all_plans {
                    if plan == PhysicalPlan::GatheredScan {
                        continue;
                    }
                    let (pe, mut res) = split_planes(
                        fuzz_bound_filter(spec, archived), &archived.indexes.planes, &archived.indexes.oracle_trigram.words, unique_is_card,
                    );
                    let out = run_query_with_plan(
                        plan, &archived.cards, &archived.printings, &archived.offsets, strings,
                        &mut res, pe.as_ref(), mode, prefer, orderby, direction, full_limit, 0, &archived.indexes,
                    );
                    let Some((total, page)) = out else { continue };
                    ran[plan_idx(plan)] += 1;
                    assert_eq!(
                        total, ref_total,
                        "{plan:?} total disagrees with GatheredScan (mode={mode}, prefer={prefer}, orderby={orderby}, dir={direction}, filter={})",
                        fuzz_describe(spec),
                    );
                    assert_eq!(
                        id_multiset(&page), ref_ids,
                        "{plan:?} rows disagree with GatheredScan (mode={mode}, prefer={prefer}, orderby={orderby}, dir={direction}, filter={})",
                        fuzz_describe(spec),
                    );
                    assert_eq!(
                        key2_seq(&page), ref_key2,
                        "{plan:?} 2-key order disagrees with GatheredScan (mode={mode}, prefer={prefer}, orderby={orderby}, dir={direction}, filter={})",
                        fuzz_describe(spec),
                    );
                }
            }
        }
    }

    for plan in all_plans {
        assert!(
            ran[plan_idx(plan)] > 0,
            "plan {plan:?} was never exercised by the differential corpus — add coverage",
        );
    }
}

/// #702 PR1 estimator accuracy report (NOT an assertion — a reporting tool).
/// Runs a spread of fuzz queries against a corpus store, comparing the point
/// estimate and bounds to the mode="card" reference count, and prints the
/// error distribution. Run with:
///   cargo test --release estimator_accuracy -- --ignored --nocapture
#[test]
#[ignore = "estimator accuracy report; cargo test estimator_accuracy -- --ignored --nocapture"]
fn estimator_accuracy() {
    use rand::SeedableRng;
    const CORPUS_CARDS: usize = 6_000;
    const QUERIES: usize = 5_000;
    const MAX_DEPTH: u8 = 3;

    let mut rng = rand::rngs::SmallRng::seed_from_u64(4_242);
    let data = fuzz_store_n(&mut rng, CORPUS_CARDS);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let n_cards = archived.cards.len();

    let (mut exact, mut within_2x, mut worse) = (0u32, 0u32, 0u32);
    let mut tight_bounds = 0u32; // bounds narrower than half the universe
    let mut collapsed_bounds = 0u32; // lo == hi (a fully-pinned answer)
    let mut unsound = 0u32; // must stay 0
    let mut total = 0u32;

    for _ in 0..QUERIES {
        let spec = fuzz_gen(&mut rng, MAX_DEPTH);
        let f = fuzz_bound_filter(&spec, archived);
        let truth = fuzz_reference_total(archived, &f, "card");
        let c = super::estimator::estimate_cardinality(&f, &archived.indexes, &archived.offsets);
        total += 1;

        if (c.lo as usize) > truth || truth > (c.hi as usize) {
            unsound += 1;
            eprintln!("UNSOUND: truth={truth} bounds=[{},{}] filter={}", c.lo, c.hi, fuzz_describe(&spec));
        }

        // Point-estimate accuracy vs truth.
        let est = c.est as usize;
        if est == truth {
            exact += 1;
        } else {
            let (a, b) = (est.max(1), truth.max(1));
            if a <= 2 * b && b <= 2 * a {
                within_2x += 1;
            } else {
                worse += 1;
            }
        }

        // Bounds tightness.
        let width = (c.hi - c.lo) as usize;
        if width * 2 < n_cards {
            tight_bounds += 1;
        }
        if c.lo == c.hi {
            collapsed_bounds += 1;
        }
    }

    let pct = |x: u32| 100.0 * f64::from(x) / f64::from(total);
    eprintln!("\n=== #702 estimator accuracy ({total} queries, {n_cards} cards) ===");
    eprintln!("unsound bounds violations : {unsound} (MUST be 0)");
    eprintln!("point est == truth        : {exact} ({:.1}%)", pct(exact));
    eprintln!("point est within 2x       : {within_2x} ({:.1}%)", pct(within_2x));
    eprintln!("point est worse than 2x   : {worse} ({:.1}%)", pct(worse));
    eprintln!("bounds width < N/2        : {tight_bounds} ({:.1}%)", pct(tight_bounds));
    eprintln!("bounds collapsed (lo==hi) : {collapsed_bounds} ({:.1}%)", pct(collapsed_bounds));
    assert_eq!(unsound, 0, "estimator produced unsound bounds — see stderr");
}

/// #702 step 3 (cost calibration): time each *applicable* physical plan on the
/// REAL corpus archive, across a spread of query selectivities × unique modes ×
/// page depths, so the per-plan cost formulas can be fit to measured runtime and
/// the empirical gold (fastest plan) established. Reporting tool, not an
/// assertion.
///
///     cargo test --release plan_cost_calibration -- --ignored --nocapture
///
/// Needs benchmarks/verify-order/real.store (see bench_verify_cost.rs to (re)build
/// it); skips cleanly if absent or stale. Min-of-N after warmup, binding a fresh
/// filter per iteration OUTSIDE the timer (FilterExpr isn't Clone, and
/// prepare_candidates mutates it via memoize). For calibration-grade numbers run
/// on a quiesced machine (benchmark-artifacts protocol) — otherwise directional.
/// The `est` column is the estimator's card-space point estimate (meaningful vs
/// `total` in card mode; printing-mode totals count printings).
#[test]
#[ignore = "plan-cost calibration bench; needs real.store; cargo test --release plan_cost_calibration -- --ignored --nocapture"]
fn plan_cost_calibration() {
    use std::hint::black_box;
    use std::time::Instant;
    const STORE_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../benchmarks/verify-order/real.store");
    const WARMUP: usize = 3;
    const ITERS: usize = 30;

    let Ok(file) = std::fs::File::open(STORE_PATH) else {
        eprintln!("SKIP: {STORE_PATH} not found (see bench_verify_cost.rs docs to build it)");
        return;
    };
    // Safety: same contract as bench_verify_cost / get_mmap — header re-validated below.
    let mmap = unsafe { Mmap::map(&file) }.expect("mmap real.store");
    if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
        eprintln!("SKIP: {STORE_PATH} header mismatch (stale — rebuild, see bench_verify_cost.rs)");
        return;
    }
    let archived = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
    eprintln!("real.store: {} cards, {} printings\n", archived.cards.len(), archived.printings.len());

    // Colors are the low 5 bits (WUBRG); TYPE_CREATURE is 1<<4. Ge (`:`) = contains.
    let color = |op, mask| FuzzSpec::Leaf(FuzzLeaf::Color { op, mask });
    let typ   = |op, mask| FuzzSpec::Leaf(FuzzLeaf::Type { op, mask });
    let cmc   = |op, val| FuzzSpec::Leaf(FuzzLeaf::Cmc { op, val });
    let power = |op, val| FuzzSpec::Leaf(FuzzLeaf::Power { op, val });
    let price = |op, val| FuzzSpec::Leaf(FuzzLeaf::Price { op, val });
    let year  = |op, y: i32| FuzzSpec::Leaf(FuzzLeaf::Year { op, year: y });

    // A spread from near-whole-corpus down to narrow, plus shapes that make each
    // plan applicable: pure-plane → True residual (P2); bare range (P1);
    // residual predicate → P3/P4 only.
    let queries: Vec<(&str, FuzzSpec)> = vec![
        ("cmc>=0 (all)",       cmc(CmpOp::Ge, 0.0)),
        ("t:creature",         typ(CmpOp::Ge, TYPE_CREATURE)),
        ("color(bit3)",        color(CmpOp::Ge, 1 << 3)),
        ("color3 t:creature",  FuzzSpec::And(vec![color(CmpOp::Ge, 1 << 3), typ(CmpOp::Ge, TYPE_CREATURE)])),
        ("t:creature power>3", FuzzSpec::And(vec![typ(CmpOp::Ge, TYPE_CREATURE), power(CmpOp::Gt, 3.0)])),
        ("cmc<=2",             cmc(CmpOp::Le, 2.0)),
        ("usd<5",              price(CmpOp::Lt, 5.0)),
        ("year>=2020",         year(CmpOp::Ge, 2020)),
        ("cmc>=15 (narrow)",   cmc(CmpOp::Ge, 15.0)),
    ];
    let modes = ["card", "printing"];
    // (label, limit, offset): shallow first page vs a deep page (broad queries only reach it).
    let pages = [("shallow", 60usize, 0usize), ("deep", 60usize, 10_000usize)];
    let all_plans = [
        PhysicalPlan::PrintingRangeScan,
        PhysicalPlan::PlanePopcountOrder,
        PhysicalPlan::StreamedSelect,
        PhysicalPlan::GatheredScan,
    ];
    let labels = ["P1-range", "P2-popcnt", "P3-stream", "P4-gather"];

    println!(
        "{:<20} {:>7} {:>7} {:>7} {:>6}  {:>9} {:>9} {:>9} {:>9}  gold",
        "query", "mode", "page", "total", "est", labels[0], labels[1], labels[2], labels[3],
    );

    for (qlabel, spec) in &queries {
        // Estimator's card-space point estimate (full unsplit filter, as validated).
        let est = super::estimator::estimate_cardinality(
            &fuzz_bound_filter(spec, archived), &archived.indexes, &archived.offsets,
        ).est;
        for &mode in &modes {
            let unique_is_card = mode == "card";
            for &(plabel, limit, offset) in &pages {
                let mut ns = [None::<u64>; 4];
                let mut total = 0usize;
                for (pi, plan) in all_plans.iter().enumerate() {
                    let mut best = u64::MAX;
                    let mut applicable = false;
                    for it in 0..(WARMUP + ITERS) {
                        // Fresh bind+split per iteration (outside the timer): memoize
                        // mutates the residual, so reusing it would drift the cost.
                        let (pe, mut res) = split_planes(
                            fuzz_bound_filter(spec, archived), &archived.indexes.planes,
                            &archived.indexes.oracle_trigram.words, unique_is_card,
                        );
                        let t0 = Instant::now();
                        let out = black_box(run_query_with_plan(
                            *plan, &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
                            &mut res, pe.as_ref(), mode, "default", "edhrec", "asc", limit, offset, &archived.indexes,
                        ));
                        let dt = t0.elapsed().as_nanos() as u64;
                        match out {
                            Some((t, _)) => {
                                total = t;
                                applicable = true;
                                if it >= WARMUP {
                                    best = best.min(dt);
                                }
                            }
                            None => break, // inapplicable for this query/mode
                        }
                    }
                    if applicable {
                        ns[pi] = Some(best);
                    }
                }
                let gold = (0..4)
                    .filter_map(|i| ns[i].map(|v| (v, labels[i])))
                    .min_by_key(|(v, _)| *v)
                    .map_or("-", |(_, l)| l);
                let cell = |o: Option<u64>| o.map_or_else(|| "-".to_string(), |v| v.to_string());
                println!(
                    "{:<20} {:>7} {:>7} {:>7} {:>6}  {:>9} {:>9} {:>9} {:>9}  {}",
                    qlabel, mode, plabel, total, est, cell(ns[0]), cell(ns[1]), cell(ns[2]), cell(ns[3]), gold,
                );
            }
        }
    }
}

/// format:A AND format:B (two distinct formats) can't be answered by ANDing
/// independent existence-projection planes -- ∃p: legal_A(p) ∧ legal_B(p) is
/// not the same as (∃p: legal_A(p)) ∧ (∃p: legal_B(p)); a card can have
/// different witness printings for each side. compile_plane must decline this
/// shape and shared-format-both-polarities alike, while Or of two distinct
/// formats has no such problem and does compile (∃ distributes over ∨).
#[test]
fn legality_and_of_two_formats_declines_but_or_compiles() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let a = || FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    let b = || FilterExpr::Legality { shift: Some(2), expected: 0b01 };

    assert!(
        compile_plane(&FilterExpr::And(vec![a(), b()]), bounds, words).is_none(),
        "two distinct formats ANDed must decline (shared-witness)"
    );
    assert!(
        compile_plane(&FilterExpr::Or(vec![a(), b()]), bounds, words).is_some(),
        "OR of two formats has no shared-witness problem and must compile"
    );
    assert!(
        compile_plane(&FilterExpr::And(vec![a(), FilterExpr::Not(Box::new(a()))]), bounds, words).is_none(),
        "format:A AND -format:A (same format, both polarities) is the same contradiction-prone shape and must also decline"
    );
}

/// The shared-witness decline's fallback must still be *correct*, not just
/// declined: a card legal in A via one printing and legal in B via a
/// different printing (no single printing satisfies both) is the trap in
/// action -- legal_exists(A) AND legal_exists(B) would wrongly say true.
/// Routed through the real split_planes + run_query pipeline.
#[test]
fn legality_shared_witness_and_falls_back_to_correct_result() {
    let mut vocab = VocabInterner::new();
    let card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0b01; // legal in A (shift 0) only
    data.printings[1].card_legalities = 0b01 << 2; // legal in B (shift 2) only
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let and_both = FilterExpr::And(vec![
        FilterExpr::Legality { shift: Some(0), expected: 0b01 },
        FilterExpr::Legality { shift: Some(2), expected: 0b01 },
    ]);
    let (plane, mut residual) = split_planes(and_both, bounds, words, true);
    assert!(plane.is_none(), "shared-witness AND must not partially promote either leaf into the plane");
    assert!(matches!(residual, FilterExpr::And(_)), "both legality children must remain in the residual");

    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual, plane.as_ref(), "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 0, "no single printing is legal in both A and B at once");
}

/// -(format:A AND t:creature): Not wrapping a compound needs De Morgan pushed
/// to compile time so the Not lands on the Legality leaf directly (swapping
/// to illegal_exists), never as a bit-complement of the AND's compiled plane.
/// Demonstrated against a card divergent in A: the positive AND is true for
/// it (its legal-in-A printing is also a creature), so a naive complement
/// would wrongly exclude it -- but the card's *other*, not-legal-in-A
/// printing separately satisfies the negation, so the correct answer
/// includes it.
#[test]
fn legality_de_morgan_not_of_compound() {
    let mut vocab = VocabInterner::new();
    let card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    let mut data = store_of(vec![card], &[2], vocab);
    data.printings[0].card_legalities = 0b01; // legal in A
    data.printings[1].card_legalities = 0; // not legal in A
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let a = || FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    let creature = || FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };

    let and_pe = compile_plane(&FilterExpr::And(vec![a(), creature()]), bounds, words)
        .expect("format:A AND t:creature must compile (one format, no shared-witness issue)");
    let mut and_bits: Vec<u64> = Vec::new();
    eval_planes(&and_pe, &archived.indexes.planes, &mut and_bits);
    assert!(bitmap_contains(&and_bits, 0), "printing0 is legal in A and a creature, so the positive AND is true for card 0");

    let not_and = FilterExpr::Not(Box::new(FilterExpr::And(vec![a(), creature()])));
    let pe = compile_plane(&not_and, bounds, words).expect("-(format:A AND t:creature) must still compile via De Morgan");
    let mut bits: Vec<u64> = Vec::new();
    eval_planes(&pe, &archived.indexes.planes, &mut bits);
    assert!(bitmap_contains(&bits, 0), "card has a not-legal-in-A printing, so it satisfies the negation despite the positive AND being true");
}

/// Regression closing a gap this design doc's own Acceptance section left
/// open (and a second reviewer flagged): `contains_unnegatable_numeric`'s
/// pre-existing guard (declining `Not` entirely when a null-valued numeric
/// field like power/toughness is anywhere inside, since `Tri::Null` never
/// flips to `True`) must still fire correctly now that `compile_plane_neg`
/// has a Legality-aware `And`/`Or` arm alongside it -- composing a legality
/// leaf with an unnegatable numeric leaf under `Not` must decline exactly
/// like composing it with any other card-invariant leaf already did
/// (`numeric_plane_declines_not_over_numeric_cmp` covers the pre-existing,
/// Legality-free shapes; this covers the new interaction specifically).
#[test]
fn legality_not_still_declines_with_unnegatable_numeric_sibling() {
    let data = legal_plane_fixture();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let a = || FilterExpr::Legality { shift: Some(0), expected: 0b01 };
    let power_gt3 = || FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Power), op: CmpOp::Gt, rhs: NumExpr::Const(3.0) };

    // Sanity: the positive compound compiles fine on its own (one format,
    // no shared-witness issue, and the un-negated numeric comparison is
    // plane-eligible) -- isolates the failure to the Not/Null interaction.
    assert!(
        compile_plane(&FilterExpr::And(vec![a(), power_gt3()]), bounds, words).is_some(),
        "format:A AND power>3 must compile without a Not present"
    );

    // Not(And(legality, unnegatable numeric)): the And arm of compile_plane_neg
    // recurses per child (De Morgan into an Or) -- the numeric child must
    // still decline via contains_unnegatable_numeric's catch-all check,
    // collapsing the whole compile to None through the `?` propagation.
    let not_and = FilterExpr::Not(Box::new(FilterExpr::And(vec![a(), power_gt3()])));
    assert!(
        compile_plane(&not_and, bounds, words).is_none(),
        "-(format:A AND power>3) must decline: Null doesn't flip to True, even with a legality sibling"
    );

    // Not(Or(legality, unnegatable numeric)): the Or arm goes through
    // and_of_checked_for_shared_witness, a different code path than And's --
    // must decline there too, not just in the And arm.
    let not_or = FilterExpr::Not(Box::new(FilterExpr::Or(vec![a(), power_gt3()])));
    assert!(
        compile_plane(&not_or, bounds, words).is_none(),
        "-(format:A OR power>3) must decline via the Or arm of compile_plane_neg too"
    );
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
    // store_of already assigned artwork_group_id from the original (all-distinct)
    // illustration_ids, so it must be recomputed after this mutation.
    data.printings[2].illustration_id = 1;
    data.indexes.artwork_groups = assign_artwork_groups(&mut data.printings, &data.offsets);
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
    let artwork_groups = assign_artwork_groups(&mut printings, &offsets);

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
        artwork_groups,
        printing_to_card: build_printing_to_card(&offsets),
        planes:         build_bit_planes(&cards, &printings, &offsets, &strings),
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
    let (plane, mut residual) = split_planes(creature, &archived.indexes.planes, &archived.indexes.oracle_trigram.words, true);
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
    let (plane, mut residual) = split_planes(creature, &archived.indexes.planes, &archived.indexes.oracle_trigram.words, true);

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
    let (plane, mut residual_true) = split_planes(creature, &archived.indexes.planes, &archived.indexes.oracle_trigram.words, true);
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
            p.price_usd = Some((pid % 7) as u32 * 100 + 50); // $0.50, $1.50, ... $6.50
        }
        if with_perms {
            data.indexes.sort_perms = build_sort_permutations(&data.cards, &data.printings, &data.offsets);
            data.indexes.artwork_groups = assign_artwork_groups(&mut data.printings, &data.offsets);
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
    let counts = assign_artwork_groups(&mut data.printings, &data.offsets);
    assert_eq!(counts, vec![2]);
    // 0 = first-seen (printing 0's illustration 7), 1 = next distinct (printing 2's
    // illustration 9); printings 1 and 3 share illustration 7's group.
    let gids: Vec<u16> = data.printings.iter().map(|p| p.artwork_group_id).collect();
    assert_eq!(gids, vec![0, 0, 1, 0]);
}

// #629: real data has a handful of cards (basic lands) with >64 distinct
// illustrations -- this exercises the match-count bitmask and emission
// scratch's growth past a single u64 word / past 64 array slots, and (via a
// shared-illustration group) that group selection still picks the best-scored
// *matching* printing, not just the highest prefer_score overall.
#[test]
fn artwork_group_ids_handle_more_than_64_groups() {
    const N: usize = 70;
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[N], vocab);
    // Printings 0 and 1 share an illustration (one group of 2); 2..70 each
    // keep their distinct store_of-assigned illustration -- 69 distinct
    // groups total, past the 64-bit single-word threshold.
    data.printings[1].illustration_id = data.printings[0].illustration_id;
    data.indexes.artwork_groups = assign_artwork_groups(&mut data.printings, &data.offsets);
    assert_eq!(data.indexes.artwork_groups, vec![69]);

    // Printing 0 has the higher prefer_score (store_of orders descending) but
    // fails usd<50; printing 1 shares its group and passes -- the chosen
    // representative for that group must be printing 1, not printing 0.
    data.printings[0].price_usd = Some(10_000); // $100.00
    for p in data.printings[1..].iter_mut() {
        p.price_usd = Some(1_000); // $10.00
    }
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);

    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let mut filter = usd_cmp(CmpOp::Lt, 50.0);
    let (total, page) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut filter, None, "artwork", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 69); // every group has >= 1 passing printing
    let chosen: Vec<u128> = page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect();
    // store_of assigns scryfall_id = printing index + 1, so printing 0 is
    // scryfall_id 1 and printing 1 is scryfall_id 2.
    assert!(chosen.contains(&2) && !chosen.contains(&1));
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

// Price is stored as integer cents (see Printing::price_usd's doc comment) specifically to make
// both narrowing (via int_range_bounds, same as collector_number) and verification (field_num's
// exact cents/100.0 division) genuinely exact at the boundary -- not "may include as a
// candidate, verified later" like the old f32-dollars representation.
#[test]
fn price_narrowing_and_verification_are_exact_at_the_boundary() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[4], vocab);
    data.printings[0].price_usd = Some(5); // $0.05
    data.printings[1].price_usd = Some(10); // $0.10 -- sits exactly on the query boundary
    data.printings[2].price_usd = Some(6000); // $60.00
    data.printings[3].price_usd = None; // priceless printings never satisfy price filters
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let card = &archived.cards[0];

    let cmp = |op| usd_cmp(op, 0.10);

    // Lt must exclude the boundary printing exactly -- both in narrowing and in verification.
    let lt = cmp(CmpOp::Lt);
    match narrow_candidates(&lt, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => {
            assert!(v.contains(&0));
            assert!(!v.contains(&1), "Lt must exclude the exact boundary price");
            assert!(!v.contains(&2) && !v.contains(&3));
        }
        _ => panic!("usd must narrow in printing space"),
    }
    assert!(lt.matches(card, &archived.printings[0], &archived.strings));
    assert!(!lt.matches(card, &archived.printings[1], &archived.strings));

    // Le, Ge, Eq must all include the boundary printing -- the Bug B repro (a stored price
    // widened from f32 essentially never bit-matched a full-precision query constant, so Ge/Eq
    // silently excluded exact matches independent of narrowing).
    for (op, op_name, is_eq) in [(CmpOp::Le, "Le", false), (CmpOp::Ge, "Ge", false), (CmpOp::Eq, "Eq", true)] {
        let f = cmp(op);
        match narrow_candidates(&f, &archived.indexes, &archived.offsets, &archived.cards) {
            Some(Candidates::Printings(v)) => assert!(v.contains(&1), "narrowing must include the boundary price for {op_name}"),
            None if is_eq => {} // narrow_candidates_exact's broadness filter can decline Eq on a tiny store; matches() below is what matters
            _ => panic!("usd must narrow in printing space for {op_name}"),
        }
        assert!(f.matches(card, &archived.printings[1], &archived.strings), "verification must include the boundary price for {op_name}");
    }
    // Gt must exclude it.
    let gt = cmp(CmpOp::Gt);
    assert!(!gt.matches(card, &archived.printings[1], &archived.strings));

    // Flipped operand order (50.0 < usd, i.e. usd > 50.0): only the $60 printing qualifies.
    let pricey = FilterExpr::NumericCmp { lhs: NumExpr::Const(50.0), op: CmpOp::Lt, rhs: NumExpr::Field(NumField::PriceUsd) };
    match narrow_candidates(&pricey, &archived.indexes, &archived.offsets, &archived.cards) {
        Some(Candidates::Printings(v)) => assert_eq!(v, vec![2]),
        _ => panic!("flipped usd comparison must narrow"),
    }
    // Ne is not selective.
    let ne = usd_cmp(CmpOp::Ne, 1.0);
    assert!(narrow_candidates(&ne, &archived.indexes, &archived.offsets, &archived.cards).is_none());
}

// Direct repro of the review-caught bug: comparing a stored price widened from a lossy f32 to
// f64 against a full-precision query constant essentially never matched, even at the exact
// value the query was checking for. usd=7.22 must match a printing priced at exactly $7.22, and
// usd>=7.22 must not drop it -- confirmed on a clean main worktree that both failed identically
// before this fix (unrelated to narrowing, since it's in field_num/cmp's verification path).
#[test]
fn price_comparison_matches_exact_value_not_lossy_f32_widening() {
    let mut vocab = VocabInterner::new();
    let cards = vec![stub_card(1, TYPE_CREATURE, &[], &mut vocab)];
    let mut data = store_of(cards, &[1], vocab);
    data.printings[0].price_usd = Some(722); // $7.22 -- 7.22_f32 widened to f64 is 7.21999979019165
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let card = &archived.cards[0];
    let p = &archived.printings[0];

    let cmp = |op| usd_cmp(op, 7.22);
    assert!(cmp(CmpOp::Eq).matches(card, p, &archived.strings), "usd=7.22 must match a printing priced at exactly $7.22");
    assert!(cmp(CmpOp::Ge).matches(card, p, &archived.strings), "usd>=7.22 must not drop the exact match");
    assert!(cmp(CmpOp::Le).matches(card, p, &archived.strings));
    assert!(!cmp(CmpOp::Lt).matches(card, p, &archived.strings));
    assert!(!cmp(CmpOp::Gt).matches(card, p, &archived.strings));
}

// The `price` closure in narrow_rec's NumericCmp arm is int_range_bounds(op,
// snap_to_nearest_cent(v * PRICE_CENTS_PER_DOLLAR)) -- not a standalone function anymore (the
// old price_bounds was deleted), so this pins down that exact composition directly rather than
// through a name that no longer exists. Thresholds include on-grid values a user would actually
// type (paired with their exact cents value, Some(_)) and deliberately off-grid ones an
// arithmetic expression could produce (paired with None); 0.28 and 0.57 are the review-caught
// repro values (`0.28_f64 * 100.0 == 28.000000000000004`, `0.57_f64 * 100.0 ==
// 56.99999999999999`) that snap_to_nearest_cent exists to correct.
#[test]
fn price_narrowing_bound_matches_direct_comparison_on_and_off_grid() {
    let thresholds: &[(f64, Option<u32>)] = &[
        (50.00, Some(5000)), (49.99, Some(4999)), (0.01, Some(1)), (5142.02, Some(514202)),
        (100.00, Some(10000)), (0.28, Some(28)), (0.57, Some(57)), (0.0, Some(0)),
        (49.998, None), (50.003, None), (33.335, None), (0.005, None), (12.3456789, None),
    ];
    let ops = [
        (CmpOp::Lt, "Lt"),
        (CmpOp::Le, "Le"),
        (CmpOp::Gt, "Gt"),
        (CmpOp::Ge, "Ge"),
        (CmpOp::Eq, "Eq"),
    ];
    let bound = |op, v: f64| super::int_range_bounds(op, super::snap_to_nearest_cent(v * 100.0)).unwrap();

    for &(t, on_grid_cents) in thresholds {
        for &(op, op_name) in &ops {
            let range = bound(op, t);
            let in_range = |cents: u32| match range {
                None => false, // provably empty
                Some((lo, hi)) => cents >= lo && cents < hi,
            };
            if let Some(cents) = on_grid_cents {
                let price = f64::from(cents) / 100.0;
                let expected = match op {
                    CmpOp::Lt => price < t,
                    CmpOp::Le => price <= t,
                    CmpOp::Gt => price > t,
                    CmpOp::Ge => price >= t,
                    CmpOp::Eq => true,
                    CmpOp::Ne => unreachable!(),
                };
                assert_eq!(in_range(cents), expected, "{op_name}({t}) disagrees with direct comparison AT ITS OWN threshold price {price}");
            }
            for cents in (1..514_202u32).step_by(37) {
                // sampled every 37 cents across the real max-price range -- not exhaustive, but
                // int_range_bounds' own exactness over the integer domain is already trusted
                // (mirrors collector_number's identical usage); this is specifically about
                // whether *100.0 + snap survives the multiplication noise it's meant to correct.
                let price = cents as f64 / 100.0;
                let expected = match op {
                    CmpOp::Lt => price < t,
                    CmpOp::Le => price <= t,
                    CmpOp::Gt => price > t,
                    CmpOp::Ge => price >= t,
                    CmpOp::Eq => (price - t).abs() < 1e-9,
                    CmpOp::Ne => unreachable!(),
                };
                assert_eq!(in_range(cents), expected, "{op_name}({t}) disagrees with direct comparison at price {price}");
            }
        }
    }
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
    // (card_colors, card_color_identity, card_types, produced_mana); color bits W=1 U=2 B=4 R=8 G=16 C=32.
    // produced_mana is deliberately independent of colors/identity on several
    // rows (card 0: colorless artifact that produces all five + C, like a
    // mana rock; card 5: Fallaji Wayfarer produces only C, matching neither
    // its own colors (31) nor its identity (16)) to prove the plane is its
    // own independent transposition, not derived from the other two.
    let specs: &[(u8, u8, u16, u8)] = &[
        (0, 0, TYPE_ARTIFACT, 63),                   // colorless artifact, produces everything
        (16, 16, TYPE_CREATURE, 0),                  // mono G creature, produces nothing
        (1, 1, TYPE_CREATURE | TYPE_LEGENDARY, 0),   // mono W legendary creature
        (3, 3, TYPE_INSTANT, 0),                      // WU instant
        (0, 24, TYPE_LAND, 24),                       // land: no colors, RG identity (Taiga), produces RG
        (31, 16, TYPE_CREATURE, 32),                  // Fallaji Wayfarer (see FALLAJI_CID), produces only C
        (2, 3, TYPE_SORCERY, 0),                       // U sorcery with WU identity
        (24, 31, TYPE_CREATURE | TYPE_ARTIFACT, 8),   // RG artifact creature, WUBRG identity, produces only R
        (4, 4, TYPE_ENCHANTMENT | TYPE_SNOW, 0),      // mono B snow enchantment
        (0, 32, TYPE_LAND, 32),                        // C-bit identity, exercising the C plane, produces C
    ];
    let cards = specs
        .iter()
        .enumerate()
        .map(|(i, &(colors, identity, types, produced))| {
            let mut c = stub_card(i as u128 + 1, types, &[], &mut vocab);
            c.card_colors = colors;
            c.card_color_identity = identity;
            c.produced_mana = produced;
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
            check(&FilterExpr::ColorCmp { field: ColorField::ProducedMana, op, mask });
        }
        for mask in type_masks {
            check(&FilterExpr::TypeCmp { mask, op });
        }
    }

    let green = || FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 };
    let creature = || FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let produces_red = || FilterExpr::ColorCmp { field: ColorField::ProducedMana, op: CmpOp::Ge, mask: 8 };
    check(&FilterExpr::And(vec![green(), creature()]));
    check(&FilterExpr::Or(vec![green(), creature()]));
    check(&FilterExpr::Not(Box::new(FilterExpr::Or(vec![green(), creature()]))));
    check(&FilterExpr::And(vec![produces_red(), creature()]));
    check(&FilterExpr::Not(Box::new(produces_red())));
}

// Regression for #668 (color:c / produces:c matching every card): Ge with an
// empty mask is the "c"/"colorless" query, and must reduce to bits == 0, not
// the vacuously-true and_of([])/bits & 0 == 0 shape. Checked against both the
// row-scan filter and the bitplane compiler, since each has its own Ge arm.
#[test]
fn color_cmp_ge_empty_mask_is_colorless_only() {
    let data = plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let check = |field: ColorField, want_true: &[usize]| {
        let f = FilterExpr::ColorCmp { field, op: CmpOp::Ge, mask: 0 };
        for (cid, card) in archived.cards.iter().enumerate() {
            let want = want_true.contains(&cid);
            assert_eq!(f.eval_card(card, &archived.strings) == Tri::True, want, "eval_card mismatch at card {cid}");
        }
        let pe = compile_plane(&f, &archived.indexes.planes, &archived.indexes.oracle_trigram.words).expect("filter must be plane-expressible");
        let mut bitmap: Vec<u64> = Vec::new();
        eval_planes(&pe, &archived.indexes.planes, &mut bitmap);
        for cid in 0..archived.cards.len() {
            assert_eq!(bitmap_contains(&bitmap, cid as u32), want_true.contains(&cid), "plane mismatch at card {cid}");
        }
    };

    // card_colors == 0: cards 0, 4, 9 (see plane_fixture_store specs above)
    check(ColorField::Colors, &[0, 4, 9]);
    // produced_mana == 0: cards 1, 2, 3, 6, 8
    check(ColorField::ProducedMana, &[1, 2, 3, 6, 8]);
    // card_color_identity == 0: only card 0
    check(ColorField::ColorIdentity, &[0]);
}

// produced_mana must be its own independent transposition, not derived from
// colors or identity: card 0 (colorless, produces everything) and card 5
// (Fallaji Wayfarer, colors=WUBR/identity=G, produces only C) both have
// produced_mana disjoint from their own colors/identity bits.
#[test]
fn plane_produces_independent_of_colors_and_identity() {
    let data = plane_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;

    let produces_white = FilterExpr::ColorCmp { field: ColorField::ProducedMana, op: CmpOp::Ge, mask: 1 };
    let pe = compile_plane(&produces_white, bounds, words).expect("produces: must be plane-expressible");
    let mut bitmap: Vec<u64> = Vec::new();
    eval_planes(&pe, bounds, &mut bitmap);
    assert!(bitmap_contains(&bitmap, 0), "colorless artifact produces white (mask 63) despite having no colors of its own");
    assert!(!bitmap_contains(&bitmap, 5), "Fallaji Wayfarer (colors WUBR) produces only C, not white");
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
    let (pe, residual) = split_planes(FilterExpr::And(vec![green(), creature(), text()]), bounds, words, true);
    assert!(pe.is_some());
    assert!(matches!(residual, FilterExpr::TextContains { .. }));

    // Fully plane-expressible tree is consumed whole.
    let (pe, residual) = split_planes(FilterExpr::And(vec![green(), creature()]), bounds, words, true);
    assert!(pe.is_some());
    assert!(matches!(residual, FilterExpr::True));
    let (pe, residual) = split_planes(FilterExpr::Or(vec![green(), creature()]), bounds, words, true);
    assert!(pe.is_some());
    assert!(matches!(residual, FilterExpr::True));

    // Or mixing plane and non-plane children stays entirely residual:
    // mask ∨ residual is not a narrowing mask.
    let (pe, residual) = split_planes(FilterExpr::Or(vec![green(), text()]), bounds, words, true);
    assert!(pe.is_none());
    assert!(matches!(residual, FilterExpr::Or(ref v) if v.len() == 2));

    // Produced mana is plane-expressible (docs/issues/00669-engine-produces-planes.md):
    // same card-level, always-known bitmask shape as Colors/ColorIdentity.
    let produces = FilterExpr::ColorCmp { field: ColorField::ProducedMana, op: CmpOp::Ge, mask: 16 };
    let (pe, residual) = split_planes(produces, bounds, words, true);
    assert!(pe.is_some());
    assert!(matches!(residual, FilterExpr::True));

    // Bare True keeps the range-scan path (no all-ones bitmap materialization).
    let (pe, residual) = split_planes(FilterExpr::True, bounds, words, true);
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
            let (pe, mut residual) = split_planes(make(), &archived.indexes.planes, &archived.indexes.oracle_trigram.words, true);
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

/// Regression for a real bug that shipped briefly in this area: an earlier
/// version rewrote a bare price `Field` (compared directly against a
/// `Const`) into a cents-only fast path at bind time, to avoid dividing on
/// every printing evaluated. That rewrite only recognized the *exact* bare
/// shape -- it did not recurse into `NumExpr::Arith`. `usd+1<power` (price
/// inside an arithmetic expression -- `parser_class=NUMERIC` on
/// `usd`/`eur`/`tix` admits this grammatically, same as `cmc`/`power`) left
/// the `1` in dollars while the fast path made `field_num` return cents
/// unconditionally, silently computing `(cents+1)<power` instead of
/// `(dollars+1)<power` -- off by a factor of 100. A second, independent
/// instance of the same bug (`usd<cmc`, a price `Field` compared directly
/// against *another* `Field`, no `Const` anywhere) confirmed the rewrite
/// couldn't be patched to cover every shape without either false negatives
/// or a full scaling transform on both sides of the comparison. Reverted the
/// whole optimization: `NumExpr::Field(Price*)` always means dollars again
/// (matching every build before it), for a ~2-3% loss on the one shape the
/// fast path covered, in exchange for making every shape correct by
/// construction rather than by rewrite coverage. Kept as a permanent
/// regression test for exactly this class of bug, in case a similar
/// shape-specific fast path is tried again later.
#[test]
fn usd_inside_arithmetic_evaluates_in_dollars_not_cents() {
    let mut vocab = VocabInterner::new();
    let mut card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card.creature_power = Some(52);
    let mut data = store_of(vec![card], &[1], vocab);
    data.printings[0].price_usd = Some(5000); // $50.00
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let card = &archived.cards[0];
    let printing = &archived.printings[0];

    // usd+1<power: $50.00 + 1 = 51 < power(52) -- must match.
    let mut f = FilterExpr::NumericCmp {
        lhs: NumExpr::Arith(Box::new(NumExpr::Field(NumField::PriceUsd)), ArithOp::Add, Box::new(NumExpr::Const(1.0))),
        op: CmpOp::Lt,
        rhs: NumExpr::Field(NumField::Power),
    };
    f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
    assert!(f.matches(card, printing, &archived.strings), "usd+1<power must evaluate in dollars: 50+1=51 < 52");
}

/// The second independent instance referenced in the doc above:
/// `usd<cmc` -- a price `Field` compared directly against *another* `Field`,
/// no `Const` anywhere -- which no version of a `Const`-rewriting fast path
/// could have covered at all, since there's no `Const` to rewrite.
#[test]
fn usd_compared_directly_against_another_field_evaluates_in_dollars() {
    let mut vocab = VocabInterner::new();
    let mut card = stub_card(1, TYPE_CREATURE, &[], &mut vocab);
    card.cmc = Some(3);
    let mut data = store_of(vec![card], &[1], vocab);
    data.printings[0].price_usd = Some(200); // $2.00
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let card = &archived.cards[0];
    let printing = &archived.printings[0];

    // usd<cmc: $2.00 < cmc(3) -- must match.
    let mut f = FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::PriceUsd), op: CmpOp::Lt, rhs: NumExpr::Field(NumField::Cmc) };
    f.bind(&archived.coll_vocab, &archived.coll_vocab_sorted, &archived.artist_vocab, &archived.mana_vocab, &archived.indexes.flavor, &archived.strings);
    assert!(f.matches(card, printing, &archived.strings), "usd<cmc must evaluate in dollars: 2.00 < 3");
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
            let (pe, mut residual) = split_planes(make(), &archived.indexes.planes, &archived.indexes.oracle_trigram.words, true);
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
    let (pe, residual) = split_planes(cmc_le12, &archived.indexes.planes, &archived.indexes.oracle_trigram.words, true);
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

// ─── Oracle word index (docs/issues/00663-engine-oracle-word-index.md) ────────────

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
        assert_eq!(n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card), expected, "oracle:{needle} must match the brute-force set exactly");
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
    assert_eq!(n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card), expected, "oracle:word must match the brute-force set exactly");
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

// ─── Border planes (docs/issues/done/00664-engine-border-planes.md, #664; promoted
// to an existential field reaching compile_plane/all_match for the 4 tracked
// values by docs/issues/00680-engine-existential-plane-generalization.md, #680) ──

/// 9 cards with varied printing-level border colors. Card 3 independently has
/// a black printing *and* a separate borderless printing — the shared-witness
/// correctness canary subject: `border:black border:borderless` must find no
/// single printing satisfying both, even though the card "has" each color.
/// Card 5 (gold) is now a *tracked* value, unlike before #680; card 8
/// (yellow) is the genuinely-untracked subject exercising the shared `other`
/// plane -- real Scryfall data (all yellow-border cards are from Aetherdrift/
/// `dft`), not a synthetic placeholder.
fn border_planes_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let mut interner = Interner::new();
    let specs: &[&[Option<&str>]] = &[
        &[Some("black")],                                     // 0: pure black
        &[Some("black"), Some("black")],                       // 1: pure black, 2 printings
        &[Some("borderless")],                                 // 2: pure borderless
        &[Some("black"), Some("borderless")],                  // 3: shared-witness subject
        &[Some("white")],                                       // 4: pure white
        &[Some("gold")],                                        // 5: tracked (4th value)
        &[None],                                                 // 6: missing border
        &[Some("black"), Some("white"), Some("borderless")],   // 7: all tracked values at once
        &[Some("yellow")],                                       // 8: untracked -- the shared `other` plane's subject
    ];
    let cards: Vec<OracleCard> = specs.iter().enumerate().map(|(i, _)| stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab)).collect();
    let printing_counts: Vec<usize> = specs.iter().map(|s| s.len()).collect();
    let mut data = store_of(cards, &printing_counts, vocab);
    let mut idx = 0;
    for borders in specs {
        for border in borders.iter() {
            data.printings[idx].card_border_id = border.map_or(NONE_STR, |b| interner.intern(b.to_string()));
            idx += 1;
        }
    }
    data.strings = interner.strings;
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data
}

/// Brute-force `border:<value>` over every card, for differential comparison:
/// does any printing of this card carry the given border color.
fn brute_force_has_border(archived: &Archived<CardData>, border: &str) -> Vec<u32> {
    (0..archived.cards.len() as u32)
        .filter(|&cid| {
            let start = u32::from(archived.offsets[cid as usize]);
            let end = u32::from(archived.offsets[cid as usize + 1]);
            (start..end).any(|p| {
                let bid = u32::from(archived.printings[p as usize].card_border_id);
                bid != NONE_STR && archived.strings[bid as usize].as_str() == border
            })
        })
        .collect()
}

// narrow_rec's border arm must reproduce the brute-force existential exactly
// for every tracked value (a solo leaf's per-card existential really is
// exact), but never mark it tight -- narrow_rec's loose narrowing feeds the
// residual per-printing walk regardless of what compile_plane can separately
// do with the same leaf (tested below).
#[test]
fn border_planes_exact_union_parity() {
    let data = border_planes_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let rec = |f: &FilterExpr| super::narrow_rec(f, &archived.indexes, &archived.offsets, &archived.cards, true);
    let border = |v: &str| FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: v.to_string() };

    for value in ["borderless", "white", "gold"] {
        let expected = brute_force_has_border(archived, value);
        let n = rec(&border(value)).unwrap_or_else(|| panic!("border:{value} must narrow"));
        assert!(!n.tight, "border planes narrow loose through narrow_rec, tight or not is compile_plane's separate call to make");
        assert_eq!(n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card), expected, "border:{value} narrowed set must match brute force");
    }

    // Untracked (yellow): narrows loosely through the shared `other` plane --
    // strictly better than declining outright, even though it can't tell
    // yellow apart from a hypothetical second untracked value.
    let expected_yellow = brute_force_has_border(archived, "yellow");
    let n = rec(&border("yellow")).expect("untracked value must still narrow via the shared other plane");
    assert!(!n.tight);
    assert_eq!(n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card), expected_yellow, "border:yellow narrowed set must match brute force");
}

// The regression test this design exists to guarantee: two independent
// per-card "has X border" bits, ANDed, would wrongly include card 3 (which
// has a black printing and a separate borderless printing, independently
// satisfying both) -- but no single printing is both, so the real answer
// (found by the residual per-printing walk the loose narrowing never
// bypasses) must be zero matches. Checked through both the unplaned path
// (plane: None, exercising narrow_rec + card_pass alone) and the real planed
// path (split_planes, exercising compile_plane's new shared-witness decline
// for border -- see and_of_checked_for_shared_witness's generalization).
#[test]
fn border_shared_witness_correctness() {
    let data = border_planes_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;
    let border = |v: &str| FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: v.to_string() };

    let and_both = FilterExpr::And(vec![border("black"), border("borderless")]);

    assert!(
        compile_plane(&and_both, bounds, words).is_none(),
        "border:black AND border:borderless must decline to compile exactly (shared witness)"
    );

    let mut f = and_both;
    let (total, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut f, None, "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total, 0, "unplaned: no printing is both black and borderless");

    let (pe, mut residual) = split_planes(
        FilterExpr::And(vec![border("black"), border("borderless")]), bounds, words, true,
    );
    let (total2, _) = run_query(
        &archived.cards, &archived.printings, &archived.offsets, &archived.strings,
        &mut residual, pe.as_ref(), "card", "default", "edhrec", "asc", 100, 0, &archived.indexes,
    );
    assert_eq!(total2, 0, "planed: falls back to the same correct zero result, not a false positive from independent narrowing");
}

/// Tracked values (black/borderless/white/gold) now exact-consume via
/// compile_plane, mirroring rarity/legality; an untracked value
/// (`border:yellow`) can't be told apart from any other untracked value by
/// the shared `other` plane, so it declines to compile exactly -- same shape
/// as `r:special`/`r:bonus` -- while still narrowing loosely (tested above).
/// Negation on a tracked value is exact too (Or of the other 3 tracked planes
/// plus `other`).
#[test]
fn border_tracked_values_exact_consumed_other_bucket_declines() {
    let data = border_planes_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;
    let border = |v: &str| FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: v.to_string() };

    for value in ["black", "borderless", "white", "gold"] {
        assert!(compile_plane(&border(value), bounds, words).is_some(), "border:{value} must compile to a plane expression");
        let not_value = FilterExpr::Not(Box::new(border(value)));
        assert!(compile_plane(&not_value, bounds, words).is_some(), "-border:{value} must compile to a plane expression");
    }

    assert!(compile_plane(&border("yellow"), bounds, words).is_none(), "border:yellow must decline (other bucket is ambiguous)");
    let not_yellow = FilterExpr::Not(Box::new(border("yellow")));
    assert!(compile_plane(&not_yellow, bounds, words).is_none(), "-border:yellow must decline (other bucket is ambiguous)");

    // Mixed with an otherwise fully plane-expressible sibling: the untracked
    // child must stay in the residual, not get silently dropped or promoted.
    let creature = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let (pe, residual) = split_planes(FilterExpr::And(vec![creature, border("yellow")]), bounds, words, true);
    assert!(pe.is_some(), "the creature child must still plane-consume");
    assert!(
        matches!(&residual, FilterExpr::TextExact { field: TextField::Border, .. }),
        "border:yellow must remain in the residual, not be consumed to True"
    );
}

/// The `other` bucket's exact-negation safety net, specifically: a card whose
/// *only* printing carries an untracked border must still evaluate correctly
/// (via `Or(..., other)`) for both a positive and a negated query on a
/// tracked value -- proving the shared bucket really does close the domain by
/// construction the way the design doc argues, not just for the cards this
/// fixture happens to include incidentally.
#[test]
fn border_other_bucket_closes_domain_for_tracked_negation() {
    let data = border_planes_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let bounds = &archived.indexes.planes;
    let words = &archived.indexes.oracle_trigram.words;
    let yellow_card = 8u32; // card 8: only printing is yellow (untracked)

    let not_black = FilterExpr::Not(Box::new(FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: "black".to_string() }));
    let pe = compile_plane(&not_black, bounds, words).expect("-border:black must compile");
    let mut bits = Vec::new();
    eval_planes(&pe, bounds, &mut bits);
    assert!(
        bitmap_contains(&bits, yellow_card),
        "a card whose only printing is untracked must satisfy -border:black (it has no black printing)"
    );

    let black = FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: "black".to_string() };
    let pe2 = compile_plane(&black, bounds, words).expect("border:black must compile");
    let mut bits2 = Vec::new();
    eval_planes(&pe2, bounds, &mut bits2);
    assert!(!bitmap_contains(&bits2, yellow_card), "the same card must not satisfy border:black");
}

/// 7 of 8 cards have a black printing (87.5%, past narrow_candidates_exact's
/// keep-if-<=75%-of-domain broadness guard, `domain - domain/4` with integer
/// division); the 8th has a borderless printing (12.5%).
fn border_broadness_fixture_store() -> CardData {
    let mut vocab = VocabInterner::new();
    let mut interner = Interner::new();
    let specs: &[&str] = &["black", "black", "black", "black", "black", "black", "black", "borderless"];
    let cards: Vec<OracleCard> = specs.iter().enumerate().map(|(i, _)| stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab)).collect();
    let mut data = store_of(cards, &[1usize; 8], vocab);
    for (i, border) in specs.iter().enumerate() {
        data.printings[i].card_border_id = interner.intern(border.to_string());
    }
    data.strings = interner.strings;
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
    data
}

// The dedicated arm always narrows (that's its whole job), but
// narrow_candidates_exact's existing broadness guard -- unrelated to this
// PR, already exercised elsewhere -- must still decline an overly-broad
// result at the root, exactly as it would for any other narrowing source.
// No special-casing needed for border:black: this is the guard doing its
// job, not a gap this design has to plug itself.
#[test]
fn border_black_declines_via_broadness_guard() {
    let data = border_broadness_fixture_store();
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let border = |v: &str| FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: v.to_string() };

    let n = super::narrow_rec(&border("black"), &archived.indexes, &archived.offsets, &archived.cards, true).expect("the dedicated arm itself always narrows");
    assert_eq!(n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card).len(), 7);

    assert!(
        narrow_candidates(&border("black"), &archived.indexes, &archived.offsets, &archived.cards).is_none(),
        "border:black alone must decline: 87.5% exceeds narrow_candidates_exact's 75% broadness cutoff"
    );
    assert!(narrow_candidates(&border("borderless"), &archived.indexes, &archived.offsets, &archived.cards).is_some());
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
        // values (cents) 0..1024, four printings per value; printings 0-3 get value 0, etc.
        p.price_usd = Some(i / 4);
        p
    }).collect();
    let idx = build_range_index(&printings, |p| p.price_usd);
    let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize");
    let archived = rkyv::access::<Archived<PrintingRangeIndex>, Error>(&bytes).expect("access");
    let n = printings.len();
    // int_range_bounds directly (not through the dollars->cents *100 step) -- this test is
    // about range_narrowed's own branching, exercised via the plain integer domain it always
    // operates on, same as collector_number's own tests would.
    let bounds = |op, v| super::int_range_bounds(op, v).unwrap().unwrap();

    // Bounds are exact (no widening) -- `< v` excludes v's own value, counts below are values
    // 0..v (v itself excluded).
    // Sparse (160 entries, 3.9%): vec, tight.
    let (lo, hi) = bounds(CmpOp::Lt, 40.0);
    let nr = super::range_narrowed(archived, lo, hi, n, false, false).expect("sparse ranges narrow in any context");
    assert!(!nr.tight, "exact param was passed false here to exercise range_narrowed's own branching");
    match nr.set {
        Candidates::Printings(v) => assert_eq!(v.len(), 160),
        _ => panic!("sparse range must stay a vec"),
    }

    // Broad but at most half (values 0..400 → 1600 entries, 39%): direct scatter, tight.
    let (lo, hi) = bounds(CmpOp::Lt, 400.0);
    assert!(super::range_narrowed(archived, lo, hi, n, false, true).is_none(), "broad bits need a consumer (broad_ok)");
    let nr = super::range_narrowed(archived, lo, hi, n, true, true).expect("broad_ok materializes the bitmap");
    assert!(nr.tight, "integer-exact bounds keep the direct scatter tight");
    match &nr.set {
        Candidates::PrintingBits(b) => {
            assert_eq!(b.iter().map(|w| w.count_ones()).sum::<u32>(), 1600);
            assert!(bitmap_contains(b, 0) && !bitmap_contains(b, 1700));
        }
        _ => panic!("broad range must be a bitmap"),
    }

    // Beyond half (values 0..900 → 3600 entries, 88%): complement scatter, loose.
    let (lo, hi) = bounds(CmpOp::Lt, 900.0);
    let nr = super::range_narrowed(archived, lo, hi, n, true, true).expect("broad_ok materializes the complement");
    assert!(!nr.tight, "complement over-includes unindexed printings");
    match &nr.set {
        Candidates::PrintingBits(b) => {
            assert_eq!(b.iter().map(|w| w.count_ones()).sum::<u32>(), 3600);
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
    // Rarity planes are built from the same printings, so they must be
    // rebuilt here too -- build_bit_planes already ran once inside store_of
    // before card_rarity_int was overwritten above, leaving stale (all-zero)
    // rarity plane bits otherwise.
    data.indexes.planes = build_bit_planes(&data.cards, &data.printings, &data.offsets, &data.strings);
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
    let cand = n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
    for (cid, card) in archived.cards.iter().enumerate() {
        let matches_not = (u32::from(archived.offsets[cid])..u32::from(archived.offsets[cid + 1]))
            .any(|p| FilterExpr::Not(Box::new(goblin())).matches(card, &archived.printings[p as usize], &archived.strings));
        if matches_not {
            assert!(cand.contains(&(cid as u32)), "complement must not drop card {cid}");
        }
    }

    // -r:x narrows via its own dedicated Not(NumericCmp{RarityInt}) arm --
    // recomputing with the negated op (Not(Eq) -> Ne), not complementing the
    // existing loose rarity candidate set (which would be unsafe: a posted
    // card can have other printings that don't satisfy the comparison). Same
    // superset check as the tight-child case above, just via a different arm.
    let n = rec(&FilterExpr::Not(Box::new(rarity()))).expect("-r:x must narrow via the negated-op arm");
    assert!(!n.tight);
    let cand = n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
    for (cid, card) in archived.cards.iter().enumerate() {
        let matches_not = (u32::from(archived.offsets[cid])..u32::from(archived.offsets[cid + 1]))
            .any(|p| FilterExpr::Not(Box::new(rarity())).matches(card, &archived.printings[p as usize], &archived.strings));
        if matches_not {
            assert!(cand.contains(&(cid as u32)), "-r:x narrowing must not drop card {cid}");
        }
    }

    // Genuinely loose children with no dedicated Not arm still block it.
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
    let cand = n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
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


// The review's reproduction: `tight_narrow_space` still declines price (deliberately --
// composition/Not-arm safety for a printing-varying field is a separate question from
// price_bounds/field_num's own exactness, not addressed by the integer-cents migration), so
// Not(usd>5) takes the plain per-candidate tri() path, not narrow_rec's Not-arm complement
// shortcut. A printing priced exactly 5.0 fails `usd>5` and must survive `-usd>5`.
#[test]
fn not_over_price_range_keeps_boundary_matches() {
    let mut vocab = VocabInterner::new();
    let cards: Vec<OracleCard> = (0..6).map(|i| stub_card(i as u128 + 1, TYPE_CREATURE, &[], &mut vocab)).collect();
    let mut data = store_of(cards, &[2usize; 6], vocab);
    for (i, p) in data.printings.iter_mut().enumerate() {
        p.price_usd = Some(if i < 2 { 500 } else { 1000 }); // card 0 sits on the boundary ($5.00)
    }
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");

    let mut f = FilterExpr::Not(Box::new(usd_cmp(CmpOp::Gt, 5.0)));
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
    let cand = n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
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
    assert_eq!(n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card), vec![0, 1, 2, 4]);

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
    let cand = n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
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
        let mut got = n.set.into_cards(&archived.offsets, &archived.indexes.printing_to_card);
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
    let usd = || usd_cmp(CmpOp::Gt, 20.0);
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
    let usd = || usd_cmp(CmpOp::Gt, 20.0);
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

// ─── Printing-range fastpath (PR 1, docs/issues/local-engine-sorted-range-fastpath.md) ────────
// The fastpath produces a page for a bare, broad printing-mode range predicate without the O(n)
// count pass. These differential-test its page production (the card-permutation walk and the
// aligned boundary-bucket sort) against a naive full-sort reference that shares only the ordering
// primitive (sort_key_bits) with it, not its bucket/walk selection logic.

/// `n_cards` cards with populated edhrec/cmc sort keys, 1-4 printings each, prices heavily
/// clustered onto a few hot values (so the price index has large equal-value tie buckets) plus
/// ~15% NULL, and a real price range index built over them.
fn printing_range_fixture(seed: u64, n_cards: usize) -> CardData {
    use rand::SeedableRng;
    let mut rng = rand::rngs::SmallRng::seed_from_u64(seed);
    let mut vocab = VocabInterner::new();
    // Distinct edhrec ranks (shuffled), as real data has — a rank is unique per card. This keeps
    // any two cards from sharing a full (primary, edhrec) sort key, so the streamed/walk emission
    // (per-card-contiguous) and a naive global (key, pid) sort agree; colliding ranks would let
    // them legitimately differ on cross-card full ties, which never occur in practice.
    let mut ranks: Vec<u32> = (0..n_cards as u32).collect();
    for i in (1..n_cards).rev() {
        ranks.swap(i, rng.random_range(0..=i));
    }
    let mut cards = Vec::with_capacity(n_cards);
    for i in 0..n_cards {
        let mut c = stub_card(i as u128, 0, &[], &mut vocab);
        c.edhrec_rank = Some(ranks[i]);
        c.cmc = Some(rng.random_range(0..8u8));
        cards.push(c);
    }
    let counts: Vec<usize> = (0..n_cards).map(|_| rng.random_range(1..=4)).collect();
    let mut data = store_of(cards, &counts, vocab);
    // cents; 5000 == $50.00, which usd<50 excludes (strict). Hot values 15/100 form big buckets.
    const PRICES: [u32; 6] = [15, 15, 100, 100, 100, 5000];
    for p in data.printings.iter_mut() {
        p.price_usd = (rng.random_range(0..100) >= 15).then(|| PRICES[rng.random_range(0..PRICES.len())]);
    }
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    data
}

/// Naive reference page for a printing-mode query: filter every printing by `leaf`, sort by
/// (sort_key_bits, pid) — the exact comparator select_page uses — and window [offset, offset+limit).
/// Returns the page's scryfall ids in order.
fn naive_printing_page(
    archived: &Archived<CardData>, leaf: &FilterExpr, sc: SortCol, desc: bool, offset: usize, limit: usize,
) -> Vec<u128> {
    let mut all: Vec<(u128, u32, u32)> = Vec::new();
    for cid in 0..archived.cards.len() as u32 {
        let card = &archived.cards[cid as usize];
        let start = u32::from(archived.offsets[cid as usize]) as usize;
        let end = u32::from(archived.offsets[cid as usize + 1]) as usize;
        for pid in start..end {
            let p = &archived.printings[pid];
            if FilterExpr::residual_matches(card, p, &archived.strings, &[leaf], false) {
                all.push((sort_key_bits(card, p, sc, desc), cid, pid as u32));
            }
        }
    }
    all.sort_unstable_by(|a, b| a.0.cmp(&b.0).then_with(|| a.2.cmp(&b.2)));
    let end = (offset + limit).min(all.len());
    if offset >= end {
        return Vec::new();
    }
    all[offset..end].iter().map(|&(_, _, pid)| u128::from(archived.printings[pid as usize].scryfall_id)).collect()
}

fn page_scryfall_ids(page: &[(&Archived<OracleCard>, &Archived<Printing>)]) -> Vec<u128> {
    page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect()
}

#[test]
fn bare_range_bounds_recognizes_leaves_and_rejects_rest() {
    let data = printing_range_fixture(0, 10);
    let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
    let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
    let ix = &archived.indexes;
    let bounds = |f: &FilterExpr| bare_range_bounds(f, ix).map(|(_, lo, hi)| (lo, hi));
    // usd<50 -> [0, 5000) cents; cn>=100 -> [100, MAX); year>2020 -> [2021*10000, MAX)
    assert_eq!(bounds(&usd_cmp(CmpOp::Lt, 50.0)), Some((0, 5000)));
    let cn_ge = FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::CollectorNumberInt), op: CmpOp::Ge, rhs: NumExpr::Const(100.0) };
    assert_eq!(bounds(&cn_ge), Some((100, u32::MAX)));
    assert_eq!(bounds(&FilterExpr::YearCmp { op: CmpOp::Gt, year: 2020 }), Some((2021 * 10_000, u32::MAX)));
    // rejects: Ne, compound, and a non-range numeric field (cmc is card-space)
    assert!(bounds(&usd_cmp(CmpOp::Ne, 50.0)).is_none());
    assert!(bounds(&FilterExpr::And(vec![usd_cmp(CmpOp::Lt, 50.0)])).is_none());
    let cmc_lt = FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: CmpOp::Lt, rhs: NumExpr::Const(3.0) };
    assert!(bounds(&cmc_lt).is_none());
}

#[test]
fn printing_range_walk_matches_naive_page() {
    let leaf = usd_cmp(CmpOp::Lt, 50.0);
    for seed in 0..16u64 {
        let data = printing_range_fixture(seed, 200);
        let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
        let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
        for &sc in &[SortCol::EdhrecRank, SortCol::Cmc] {
            for &desc in &[false, true] {
                let perm = archived.indexes.sort_perms.get(sc, desc).expect("perm exists");
                for &(off, lim) in &[(0usize, 10usize), (0, 100), (5, 20), (50, 50), (300, 25)] {
                    let got = walk_printing_page(
                        &archived.cards, &archived.printings, &archived.offsets, &archived.strings, &leaf, sc, desc, lim, off, perm,
                    );
                    let want = naive_printing_page(&archived, &leaf, sc, desc, off, lim);
                    assert_eq!(page_scryfall_ids(&got), want, "walk seed {seed} desc {desc} off {off} lim {lim}");
                }
            }
        }
    }
}

#[test]
fn printing_range_aligned_page_matches_naive_incl_tie_buckets() {
    let leaf = usd_cmp(CmpOp::Lt, 50.0);
    for seed in 0..16u64 {
        let data = printing_range_fixture(seed, 300);
        let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
        let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
        let idx = &archived.indexes.price_usd;
        // usd<50 -> cents [0, 5000); lo is 0 so s is the slice start.
        let s = 0usize;
        let e = idx.partition_point(|p| u32::from(p.0) < 5000);
        let k = e - s;
        assert!(k > 100, "seed {seed}: matching set must be broad with big tie buckets, got {k}");
        // total = k = the true match count (independent count over all printings). The fastpath
        // reports total = k, so pinning k == count pins the reported total.
        let naive_count = (0..archived.printings.len())
            .filter(|&pid| FilterExpr::residual_matches(&archived.cards[u32::from(archived.indexes.printing_to_card[pid]) as usize], &archived.printings[pid], &archived.strings, &[&leaf], false))
            .count();
        assert_eq!(k, naive_count, "seed {seed}: binary-search k must equal the true match count");
        for &desc in &[false, true] {
            // offsets at the start, inside each tie bucket, spanning a boundary, and near the end
            for &off in &[0, k / 5, k / 2, (3 * k) / 4, k.saturating_sub(30)] {
                for &lim in &[10usize, 40, 175] {
                    if off >= k {
                        continue;
                    }
                    let got = aligned_page(idx, s, e, &archived.cards, &archived.printings, &archived.indexes.printing_to_card, desc, off, lim);
                    let want = naive_printing_page(&archived, &leaf, SortCol::PriceUsd, desc, off, lim);
                    assert_eq!(page_scryfall_ids(&got), want, "aligned seed {seed} desc {desc} off {off} lim {lim} k {k}");
                }
            }
        }
    }
}

/// `n` cards, one printing each, all priced $1.00 — so `usd<50` matches every printing and k == n,
/// exactly dialable across the STREAM_MIN_MATCHES boundary. Distinct edhrec ranks, price index built.
fn all_priced_fixture(n: usize) -> CardData {
    let mut vocab = VocabInterner::new();
    let cards: Vec<OracleCard> = (0..n)
        .map(|i| {
            let mut c = stub_card(i as u128, 0, &[], &mut vocab);
            c.edhrec_rank = Some(i as u32);
            c
        })
        .collect();
    let mut data = store_of(cards, &vec![1usize; n], vocab);
    for p in data.printings.iter_mut() {
        p.price_usd = Some(100);
    }
    data.indexes.price_usd = build_range_index(&data.printings, |p| p.price_usd);
    data
}

#[test]
fn printing_range_fastpath_gates_card_walk_at_stream_threshold() {
    // The card-level walk reproduces run_query_streamed's per-card-contiguous *stream* emission,
    // which the general path only uses above STREAM_MIN_MATCHES; at or below it the general path
    // gathers (global sort), ordering full-key ties across cards differently. So the walk must bail
    // at/below the threshold and fire above it. Aligned (order by usd) matches the gathered path and
    // is exempt. k == n in this fixture.
    let stream_min = *STREAM_MIN_MATCHES;
    for (n, walk_fires) in [(stream_min, false), (stream_min + 16, true)] {
        let data = all_priced_fixture(n);
        let bytes = rkyv::to_bytes::<Error>(&data).expect("serialize");
        let archived = rkyv::access::<Archived<CardData>, Error>(&bytes).expect("access");
        let leaf = usd_cmp(CmpOp::Lt, 50.0);
        let ix = &archived.indexes;
        let fp = |sc| printing_range_fastpath(&archived.cards, &archived.printings, &archived.offsets, &archived.strings, &leaf, ix, sc, false, 100, 0);
        // Every printing is priced $1 < $50, so the true match count is n; when the fastpath fires
        // its reported total must equal it (total == k == match count), end to end.
        match fp(SortCol::EdhrecRank) {
            Some((total, _)) => {
                assert!(walk_fires, "card-walk fired below the gate at n={n} (STREAM_MIN={stream_min})");
                assert_eq!(total, n, "walk total must equal the true match count at n={n}");
            }
            None => assert!(!walk_fires, "card-walk should have fired at n={n} (STREAM_MIN={stream_min})"),
        }
        let (aligned_total, _) = fp(SortCol::PriceUsd).expect("aligned exempt from stream gate");
        assert_eq!(aligned_total, n, "aligned total must equal the true match count at n={n}");
    }
}


