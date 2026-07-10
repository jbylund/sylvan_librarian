//! Micro-benchmark for `run_query`'s match-phase loop shape: `card_ids` is
//! typed `Box<dyn Iterator<Item = u32>>` (to unify the `Some(candidates)` vs
//! `None` (full-range) cases into one type), meaning every `.next()` call in
//! the hottest loop in the query goes through a vtable — surfaced while
//! investigating `oracle:token`'s cost, where the match-phase loop measured
//! ~2x what raw string-search alone should cost.
//!
//! Two comparisons, both over the real corpus's card count:
//! - `bare`: trivial per-iteration work (a black_box sum) — isolates the
//!   dispatch overhead alone, nothing else.
//! - `realistic`: the actual TextContains predicate evaluated per card (the
//!   real `oracle:token` per-candidate cost) — shows what fraction of the
//!   *real* workload the dispatch overhead actually is.
//!
//! Both compare a `Box<dyn Iterator<Item = u32>>` (today's shape) against a
//! concrete, monomorphizable iterator type over the identical id sequence.
//!
//!     cargo test --release bench_iter_dispatch -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store (same archive bench_verify_cost
//! uses — see that module's doc comment for the one-time build command).

use std::hint::black_box;
use std::time::Instant;

use rkyv::Archived;

use super::{archive_header, archive_payload, CardData, FilterExpr, Mmap, Tri, ARCHIVE_HEADER_LEN};

const ITERS: usize = 200;
const STORE_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../benchmarks/verify-order/real.store");

fn time_ns(mut kernel: impl FnMut() -> u32) -> f64 {
    let mut best = u128::MAX;
    let mut out = 0;
    for _ in 0..ITERS {
        let t0 = Instant::now();
        out = black_box(kernel());
        best = best.min(t0.elapsed().as_nanos());
    }
    black_box(out);
    best as f64
}

#[test]
#[ignore = "micro-benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_iter_dispatch_cost() {
    let Ok(file) = std::fs::File::open(STORE_PATH) else {
        eprintln!("SKIP: {STORE_PATH} not found (see module docs)");
        return;
    };
    // Safety: same contract as get_mmap() in lib.rs — re-validated below.
    let mmap = unsafe { Mmap::map(&file) }.expect("mmap real.store");
    if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
        eprintln!("SKIP: {STORE_PATH} header mismatch (stale archive — rebuild it, see module docs)");
        return;
    }
    let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
    let n = data.cards.len();
    println!("\n{n} oracle cards from {STORE_PATH}");

    // A realistic candidate list: every 8th card id, ~n/8 entries — comparable
    // in size to oracle:token's 3,993-of-31,508 narrowed candidate set.
    let ids: Vec<u32> = (0..n as u32).step_by(8).collect();
    println!("candidate set size: {}", ids.len());

    // ─── bare: trivial per-iteration work, isolates dispatch alone ───
    let boxed_bare = time_ns(|| {
        let it: Box<dyn Iterator<Item = u32>> = Box::new(ids.iter().copied());
        let mut acc = 0u32;
        for cid in it {
            acc = acc.wrapping_add(black_box(cid));
        }
        acc
    });
    let concrete_bare = time_ns(|| {
        let it = ids.iter().copied();
        let mut acc = 0u32;
        for cid in it {
            acc = acc.wrapping_add(black_box(cid));
        }
        acc
    });
    println!(
        "\nbare (trivial body):       boxed={boxed_bare:>10.0} ns   concrete={concrete_bare:>10.0} ns   ratio={:.2}x",
        boxed_bare / concrete_bare
    );

    // ─── realistic: the real oracle:token per-card predicate, via the exact
    // card_pass() call the real match-phase loop makes (same residual/
    // residual_is_or plumbing) ───
    let filter = FilterExpr::TextContains { field: super::TextSearchField::OracleTextLower, word: "token".to_string() };
    let strings = &data.strings;

    let boxed_real = time_ns(|| {
        let it: Box<dyn Iterator<Item = u32>> = Box::new(ids.iter().copied());
        let mut residual: Vec<&FilterExpr> = Vec::new();
        let mut residual_is_or = false;
        let mut n_match = 0u32;
        for cid in it {
            let card = &data.cards[black_box(cid) as usize];
            if matches!(filter.card_pass(card, strings, &mut residual, &mut residual_is_or), Tri::True) {
                n_match += 1;
            }
        }
        n_match
    });
    let concrete_real = time_ns(|| {
        let it = ids.iter().copied();
        let mut residual: Vec<&FilterExpr> = Vec::new();
        let mut residual_is_or = false;
        let mut n_match = 0u32;
        for cid in it {
            let card = &data.cards[black_box(cid) as usize];
            if matches!(filter.card_pass(card, strings, &mut residual, &mut residual_is_or), Tri::True) {
                n_match += 1;
            }
        }
        n_match
    });
    println!(
        "realistic (oracle:token):  boxed={boxed_real:>10.0} ns   concrete={concrete_real:>10.0} ns   ratio={:.2}x   (dispatch overhead ~{:.0} ns/iter)",
        boxed_real / concrete_real,
        (boxed_real - concrete_real) / ids.len() as f64,
    );
}
