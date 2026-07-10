//! Micro-benchmark for oracle-text substring search: std `str::contains` vs
//! `memchr::memmem`, one-shot and Finder-reuse, over the real distinct
//! oracle texts (issue: accelerating `oracle:`/`name:`/`artist:`/`flavor:`
//! substring search — surfaced while investigating `oracle:token`'s cost).
//!
//! Every contender is asserted result-identical against every distinct text
//! before anything is timed, so the run doubles as a parity check over the
//! full real-data distribution.
//!
//!     cargo test --release bench_text_search -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store (same archive bench_verify_cost
//! uses — see that module's doc comment for the one-time build command).

use std::hint::black_box;
use std::time::Instant;

use memchr::memmem;
use rkyv::Archived;

use super::{archive_header, archive_payload, str_at, CardData, Mmap, ARCHIVE_HEADER_LEN};

const ITERS: usize = 50;
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
fn bench_text_search_contenders() {
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

    // Distinct oracle texts, first-seen order — same dedup shape the real
    // oracle trigram index and memoize_text_predicates operate over.
    let mut seen = std::collections::HashSet::new();
    let mut texts: Vec<&str> = Vec::new();
    for card in data.cards.iter() {
        let gid = u32::from(card.oracle_text_lower_id);
        if seen.insert(gid) {
            if let Some(s) = str_at(&data.strings, gid) {
                texts.push(s);
            }
        }
    }
    println!("\n{} distinct oracle texts from {STORE_PATH}", texts.len());

    // Needles spanning the range of trigram-selectivity behavior seen in
    // practice: "token" (declines memoization — its trigrams are nearly as
    // common as the intersection), a short common word, a longer phrase.
    let needles = ["token", "draw", "haste", "destroy", "sacrifice a creature"];

    for needle in needles {
        // Parity check first: all three contenders must agree on every text.
        let finder = memmem::Finder::new(needle.as_bytes());
        for &t in &texts {
            let std_r = t.contains(needle);
            let oneshot_r = memmem::find(t.as_bytes(), needle.as_bytes()).is_some();
            let finder_r = finder.find(t.as_bytes()).is_some();
            assert_eq!(std_r, oneshot_r, "mismatch (one-shot) on {needle:?} vs {t:?}");
            assert_eq!(std_r, finder_r, "mismatch (finder) on {needle:?} vs {t:?}");
        }

        let n_std = time_ns(|| texts.iter().filter(|t| t.contains(needle)).count() as u32);
        let n_oneshot = time_ns(|| texts.iter().filter(|t| memmem::find(t.as_bytes(), needle.as_bytes()).is_some()).count() as u32);
        let finder = memmem::Finder::new(needle.as_bytes());
        let n_finder = time_ns(|| texts.iter().filter(|t| finder.find(t.as_bytes()).is_some()).count() as u32);

        println!(
            "{needle:<24} std={n_std:>10.0} ns  memmem_oneshot={n_oneshot:>10.0} ns ({:.2}x)  memmem_finder={n_finder:>10.0} ns ({:.2}x)",
            n_std / n_oneshot,
            n_std / n_finder,
        );
    }
}
