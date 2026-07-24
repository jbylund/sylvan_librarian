//! Micro-benchmark: PrintingCompose paging strategy **A vs B** for a `unique=printing`,
//! `orderby=usd` query whose composed predicate has no card-space sort permutation (the #744 regime).
//!
//!   A = `walk_range_orderby_page` — walk the pre-sorted `price_usd` `PrintingRangeIndex` in value
//!       order, test the composed `pbits` bit per visited entry, stop at `offset+limit`. Cost
//!       `O((offset+limit)/selectivity)`, but the row it collects is a *random* pid into the store.
//!   B = `gather_composed_page` — sweep the composed `pbits` (candidate cards in id order, each card's
//!       printings contiguous ⇒ ~sequential), accumulate every match into a bounded `GatherSelect`,
//!       one final quickselect by usd. Cost `O(n_matches)` sweep + `O(k log k)` select, offset-
//!       independent, sequential access.
//!
//! The router today ALWAYS picks A (OrderbyWalk) for this regime — B is only a runtime fallback for
//! A's null-price tail. This bench measures whether A is in fact the winner across a selectivity
//! sweep, so the cost model's fixed choice can be audited. Predicates are real composable collection
//! leaves (this PR's feature), from very sparse to near-total, so the composed bitmaps have realistic
//! pid clustering. Both strategies are asserted to return the identical offset-0 page first.
//!
//!     cargo test --release bench_compose_paging -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store (same file/rebuild contract as bench_verify_cost.rs).

use std::hint::black_box;
use std::time::Instant;

use rkyv::Archived;

use super::{
    archive_header, archive_payload, compose_printing_bits, gather_composed_page, walk_range_orderby_page, CardData, CmpOp, CollField,
    FilterExpr, Mmap, Mode, Prefer, SortCol, ARCHIVE_HEADER_LEN,
};

const ITERS: usize = 200;
const LIMIT: usize = 175; // a Scryfall page
const STORE_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../benchmarks/verify-order/real.store");

fn best_ns(mut kernel: impl FnMut() -> usize) -> (u128, usize) {
    let mut best = u128::MAX;
    let mut rows = 0;
    for _ in 0..ITERS {
        let t0 = Instant::now();
        rows = black_box(kernel());
        best = best.min(t0.elapsed().as_nanos());
    }
    (best, rows)
}

#[test]
#[ignore = "micro-benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_compose_paging() {
    let Ok(file) = std::fs::File::open(STORE_PATH) else {
        eprintln!("SKIP: {STORE_PATH} not found (see module docs)");
        return;
    };
    let mmap = unsafe { Mmap::map(&file) }.expect("mmap real.store");
    if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
        eprintln!("SKIP: {STORE_PATH} header mismatch (stale archive — rebuild it, see module docs)");
        return;
    }
    let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
    let n_printings = data.printings.len();
    let (cards, printings, offsets, p2c) = (&data.cards, &data.printings, &data.offsets, &data.indexes.printing_to_card);
    println!("\n{} printings, {} cards from {STORE_PATH}", n_printings, cards.len());

    let coll = |field, value: &str, negate: bool| -> FilterExpr {
        let leaf = FilterExpr::CollectionCmp { field, op: CmpOp::Ge, value: value.to_string(), value_id: None };
        if negate { FilterExpr::Not(Box::new(leaf)) } else { leaf }
    };

    // Selectivity sweep, sparse → near-total. All real composable collection leaves (this PR).
    // Real-corpus subtypes are title-case.
    let filters: Vec<(&str, FilterExpr)> = vec![
        ("type:Octopus (very sparse)", coll(CollField::Subtypes, "Octopus", false)),
        ("type:Goblin (sparse)", coll(CollField::Subtypes, "Goblin", false)),
        ("type:Human (mid)", coll(CollField::Subtypes, "Human", false)),
        ("-type:Human (~85%)", coll(CollField::Subtypes, "Human", true)),
        ("-type:Goblin (~98%)", coll(CollField::Subtypes, "Goblin", true)),
        ("-type:Octopus (near-total)", coll(CollField::Subtypes, "Octopus", true)),
    ];

    // Offsets: page 1 (shallow) and a deep page (~70% through the match set).
    println!("\n  {:<30} {:>7} {:>6} {:>6} {:>10} {:>10} {:>7}  winner", "predicate", "sel%", "off", "rows", "A walk ns", "B gather ns", "B/A");
    for (label, filter) in &filters {
        let pbits = compose_printing_bits(filter, &data.indexes, offsets, printings, n_printings);
        let total: usize = pbits.iter().map(|w| w.count_ones() as usize).sum();
        let sel = 100.0 * total as f64 / n_printings as f64;
        if total == 0 {
            println!("  {label:<30}  (0 matches — skipped)");
            continue;
        }
        for &offset in &[0usize, (total * 7 / 10).saturating_sub(LIMIT / 2)] {
            if offset >= total {
                continue;
            }
            let run_a = || {
                walk_range_orderby_page(&data.indexes.price_usd, &pbits, cards, printings, p2c, SortCol::PriceUsd, false, total, LIMIT, offset)
                    .map_or(0, |p| p.len())
            };
            let run_b = || {
                gather_composed_page(Mode::Printing, cards, printings, offsets, &pbits, Prefer::Default, SortCol::PriceUsd, false, LIMIT, offset, u16::from(data.indexes.max_artwork_groups))
                    .len()
            };
            // Cross-check identical page (offset 0 only — A declines into the null-price tail at deep
            // offsets, where only B is defined; that decline is itself the finding for those rows).
            let a_page = walk_range_orderby_page(&data.indexes.price_usd, &pbits, cards, printings, p2c, SortCol::PriceUsd, false, total, LIMIT, offset);
            let b_page = gather_composed_page(Mode::Printing, cards, printings, offsets, &pbits, Prefer::Default, SortCol::PriceUsd, false, LIMIT, offset, u16::from(data.indexes.max_artwork_groups));
            if let Some(a_page) = &a_page {
                let a_ids: Vec<u128> = a_page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect();
                let b_ids: Vec<u128> = b_page.iter().map(|(_, p)| u128::from(p.scryfall_id)).collect();
                assert_eq!(a_ids, b_ids, "A and B disagree on the page for {label} @ offset {offset}");
            }
            let (a_ns, a_rows) = best_ns(run_a);
            let (b_ns, b_rows) = best_ns(run_b);
            let a_declined = a_page.is_none();
            let ratio = b_ns as f64 / a_ns as f64;
            let winner = if a_declined { "B (A declined)" } else if a_ns < b_ns { "A" } else { "B" };
            println!(
                "  {label:<30} {sel:>6.2} {offset:>6} {:>6} {a_ns:>10} {b_ns:>10} {ratio:>6.2}x  {winner}",
                a_rows.max(b_rows),
            );
            let _ = a_rows;
            let _ = b_rows;
        }
    }
    println!();
}
