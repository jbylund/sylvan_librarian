//! Kernel benchmark for #849: bitmap materialization at the remaining narrowing call sites.
//!
//! #845 adopted `sorted_ids` for the range arms and for the two card-space arms
//! (`numeric_candidates`, `arith_tuple_narrow`). What was left was every arm that unions
//! several *posting rows* — sorted runs, no globally sorted output, because row order is not
//! id order. This measures the two that were left, against the real corpus, and they split:
//!
//! **`expand_csr` (adopted).** The oracle-text, artist and flavor CSR tables. `k` is 349–77,049
//! on ordinary needles, which is 90:1 down to 1.3:1 against the domain — the whole realistic
//! range sits deep inside the bitmap's half of `MATERIALIZE_BITMAP_RATIO`, and the measured win
//! is 2.0–4.5x on the materialization. The old code concatenated the rows into one vec and
//! sorted it; the new one scatters straight from the rows, so the concatenation disappears
//! too — worth another 2x in card space (`concat+bitmap` vs `direct` below).
//!
//! **`rarity_candidates` (declined).** Same posting-union shape, and by ratio it looks like the
//! best candidate of all — `rarity<=rare` is 29,712 cards in a domain of 31,508, or 1.1:1. It
//! still loses, because its fold is not the fold the crossover was measured against: it unions
//! at most 6 rows, each already sorted and each thousands of ids long, so `union_sorted` runs
//! a handful of linear merges rather than the `log k` heap that lost in
//! `bench_candidate_materialize`. The bitmap ties it at best and loses 20x at `rarity=common`,
//! where a single bucket needs no merge at all. Left alone; the numbers are below so the next
//! reader does not have to re-derive them.
//!
//! The same reasoning already governs `sparse_text_ids` (the oracle-word union), measured in
//! `bench_narrow_alloc.rs` section B — few rows, so the fold wins. Row COUNT is what decides
//! between a fold and a materialization; the domain:count ratio only decides between a sort and
//! a bitmap once you are materializing.
//!
//!     cargo test --release bench_expand_materialize -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store (see bench_verify_cost.rs's module doc for the
//! one-time build command).

use std::hint::black_box;
use std::time::Instant;

use rkyv::Archived;

use super::{
    archive_header, archive_payload, bitmap_card_ids, expand_csr, num_cmp, scatter_bits, trigram_candidates, union_sorted, AOffsets,
    ArtistIndex, CardData, CmpOp, FilterExpr, FlavorIndex, Mmap, OracleTextIndex, TextSearchField, ARCHIVE_HEADER_LEN,
    MATERIALIZE_BITMAP_RATIO,
};

/// Best-of rounds per contender, matching the other kernel benches.
const ITERS: usize = 200;
const STORE_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../benchmarks/verify-order/real.store");

fn time_us(mut kernel: impl FnMut() -> usize) -> f64 {
    let mut best = u128::MAX;
    for _ in 0..ITERS {
        let t0 = Instant::now();
        black_box(kernel());
        best = best.min(t0.elapsed().as_nanos());
    }
    best as f64 / 1000.0
}

/// `expand_csr` as it stood before this change: concatenate every row into one vec, sort it.
/// Kept verbatim rather than described, so the comparison is against code and not a memory of it.
fn expand_csr_by_sort(offsets: &AOffsets, payload: &AOffsets, rows: impl IntoIterator<Item = usize>) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::new();
    for row in rows {
        let start = u32::from(offsets[row]) as usize;
        let end = u32::from(offsets[row + 1]) as usize;
        out.extend(payload[start..end].iter().map(|x| u32::from(*x)));
    }
    out.sort_unstable();
    out
}

/// The middle option, to show why the shipped one does not concatenate first: build the same
/// concatenated vec, then scatter from it instead of sorting it.
fn expand_csr_by_concat_bitmap(offsets: &AOffsets, payload: &AOffsets, rows: impl IntoIterator<Item = usize>, domain: usize) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::new();
    for row in rows {
        let start = u32::from(offsets[row]) as usize;
        let end = u32::from(offsets[row + 1]) as usize;
        out.extend(payload[start..end].iter().map(|x| u32::from(*x)));
    }
    bitmap_card_ids(&scatter_bits(out, domain))
}

/// Time all three routes over one real row set, asserting they agree before timing anything.
fn compare(label: &str, offsets: &AOffsets, payload: &AOffsets, rows: &[usize], domain: usize) {
    let want = expand_csr_by_sort(offsets, payload, rows.iter().copied());
    let got = expand_csr(offsets, payload, rows.iter().copied(), domain);
    assert_eq!(want, got, "expand_csr disagrees with the sort it replaced on {label}");
    let k = want.len();

    let sort_us = time_us(|| expand_csr_by_sort(offsets, payload, rows.iter().copied()).len());
    let concat_us = time_us(|| expand_csr_by_concat_bitmap(offsets, payload, rows.iter().copied(), domain).len());
    let direct_us = time_us(|| expand_csr(offsets, payload, rows.iter().copied(), domain).len());

    let ratio = if k == 0 { f64::INFINITY } else { domain as f64 / k as f64 };
    let verdict = if direct_us < sort_us {
        format!("bitmap {:.2}x", sort_us / direct_us)
    } else {
        format!("SORT {:.2}x", direct_us / sort_us)
    };
    println!(
        "  {label:<30} rows {:>5}  k {:>6}  dom:k {:>7.1}:1   sort {:>8.2}  concat+bmp {:>8.2}  direct {:>8.2}   {verdict}",
        rows.len(),
        k,
        ratio,
        sort_us,
        concat_us,
        direct_us,
    );
}

#[test]
#[ignore = "kernel benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_expand_materialize() {
    let Ok(file) = std::fs::File::open(STORE_PATH) else {
        eprintln!("SKIP: {STORE_PATH} not found (see module docs)");
        return;
    };
    // Safety: same contract as get_mmap() in lib.rs — the file is written by rkyv::to_bytes and
    // replaced atomically; the header is re-validated below before the payload is trusted.
    let mmap = unsafe { Mmap::map(&file) }.expect("mmap real.store");
    if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
        eprintln!("SKIP: {STORE_PATH} header mismatch (stale archive — rebuild it, see module docs)");
        return;
    }
    let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
    let n_cards = data.cards.len();
    let n_printings = data.printings.len();
    println!(
        "\n{n_cards} cards / {n_printings} printings — MATERIALIZE_BITMAP_RATIO puts the crossover at \
         k > {} in card space, k > {} in printing space. All times µs, best of {ITERS}.",
        n_cards / MATERIALIZE_BITMAP_RATIO,
        n_printings / MATERIALIZE_BITMAP_RATIO,
    );

    // ── expand_text_ids: oracle-text CSR, card space ─────────────────────────
    // Almost every `o:` needle lands here. `scan_oracle_words` splits the dictionary into a
    // dense tier (~56 words) answered by a bitplane and a sparse tier (~6,300) answered by
    // these postings, and the CSR is skipped only when a needle's matches are ENTIRELY dense
    // and there is exactly one of them -- `o:flying`, and not much else. `o:trample`,
    // `o:sacrifice`, `o:landwalk` and every multi-word or short needle come through here,
    // as do the literal factors of a regex. Probed against the real corpus, not assumed.
    println!("\n-- expand_text_ids: oracle text -> cards, domain {n_cards} --");
    let ot: &Archived<OracleTextIndex> = &data.indexes.oracle_trigram;
    for needle in [
        "flying",
        "destroy target creature",
        "draw a card",
        "counter target spell",
        "sacrifice",
        "token",
        "enters",
        "protection from everything",
    ] {
        let Some(text_ids) = trigram_candidates(&ot.trigrams, needle) else {
            println!("  {needle:<30} (no trigram candidates)");
            continue;
        };
        let rows: Vec<usize> = text_ids.iter().map(|&t| t as usize).collect();
        compare(needle, &ot.offsets, &ot.card_indices, &rows, n_cards);
    }

    // ── expand_artist_ids: artist CSR, printing space ────────────────────────
    println!("\n-- expand_artist_ids: artist -> printings, domain {n_printings} --");
    let ai: &Archived<ArtistIndex> = &data.indexes.artists;
    for needle in ["guay", "john", "a", "e", "rebecca", "wayne"] {
        let mut f = FilterExpr::TextContains { field: TextSearchField::ArtistLower, word: needle.to_string() };
        f.bind(&data.coll_vocab, &data.artist_vocab, &data.artist_vocab_collated, &data.mana_vocab, &data.indexes.flavor, &data.strings);
        let FilterExpr::ArtistMatch { ids } = &f else {
            println!("  {needle:<30} (bind did not produce ArtistMatch)");
            continue;
        };
        let rows: Vec<usize> = ids.iter().map(|&a| a as usize).collect();
        compare(needle, &ai.offsets, &ai.printings, &rows, n_printings);
    }

    // ── expand_flavor_ids: flavor CSR, printing space ────────────────────────
    // `the` is measured but is NOT reachable as a query: at 36,392 printings
    // `range_too_broad_to_narrow` declines the flavor arm before `expand_flavor_ids` is
    // called (probed — `ft:the` makes zero `expand_csr` calls). It is kept as the kernel's
    // wide-row data point, and as the reason the flavor arm's end-to-end win is the smallest
    // of the three: its largest sets are the ones that never get here.
    println!("\n-- expand_flavor_ids: flavor text -> printings, domain {n_printings} --");
    let fi: &Archived<FlavorIndex> = &data.indexes.flavor;
    for needle in ["dragon", "death", "the", "war", "life"] {
        let mut f = FilterExpr::TextContains { field: TextSearchField::FlavorTextLower, word: needle.to_string() };
        f.bind(&data.coll_vocab, &data.artist_vocab, &data.artist_vocab_collated, &data.mana_vocab, &data.indexes.flavor, &data.strings);
        let FilterExpr::FlavorMatch { dense_ids, .. } = &f else {
            println!("  {needle:<30} (bind did not produce FlavorMatch)");
            continue;
        };
        let rows: Vec<usize> = dense_ids.iter().map(|&d| d as usize).collect();
        compare(needle, &fi.offsets, &fi.printings, &rows, n_printings);
    }

    // ── rarity_candidates: bucket union, card space — declined, see module doc ─
    println!("\n-- rarity_candidates: rarity buckets -> cards, domain {n_cards} (NOT adopted) --");
    let ri = &data.indexes.rarity;
    println!("  {} index entries over {n_cards} cards", ri.iter().map(|b| b.len()).sum::<usize>());
    for (label, op, val) in [
        ("rarity=common", CmpOp::Eq, 0.0),
        ("rarity>=rare", CmpOp::Ge, 2.0),
        ("rarity<=uncommon", CmpOp::Le, 1.0),
        ("rarity>=uncommon", CmpOp::Ge, 1.0),
        ("rarity<=rare", CmpOp::Le, 2.0),
    ] {
        let buckets: Vec<usize> = (0..ri.len()).filter(|&r| num_cmp(op, r as f64, val)).collect();
        let ids = || buckets.iter().flat_map(|&b| ri[b].iter().map(|x| u32::from(*x)));
        let fold = || {
            let mut result: Vec<u32> = Vec::new();
            for &b in &buckets {
                result = union_sorted(result, ri[b].iter().map(|x| u32::from(*x)).collect());
            }
            result
        };
        let want = fold();
        assert_eq!(want, bitmap_card_ids(&scatter_bits(ids(), n_cards)), "rarity routes disagree on {label}");

        let fold_us = time_us(|| fold().len());
        let bitmap_us = time_us(|| bitmap_card_ids(&scatter_bits(ids(), n_cards)).len());
        println!(
            "  {label:<30} rows {:>5}  k {:>6}  dom:k {:>7.1}:1   fold {:>8.2}  bitmap {:>8.2}   {}",
            buckets.len(),
            want.len(),
            n_cards as f64 / want.len() as f64,
            fold_us,
            bitmap_us,
            if fold_us <= bitmap_us { format!("FOLD {:.2}x", bitmap_us / fold_us) } else { format!("bitmap {:.2}x", fold_us / bitmap_us) },
        );
    }
}
