//! Micro-benchmark for the oracle word dictionary's sparse-tier scan
//! (docs/issues/engine-oracle-word-index.md): std `str::match_indices` over
//! the concatenated `sparse_blob` vs. `memchr::memmem::Finder::find_iter`
//! over the same blob.
//!
//! Context: the naive per-word `.contains()` loop (one call per ~6,300
//! sparse dictionary words) measured as a genuine broad-survey regression —
//! `.contains()` redoes needle preprocessing on every call, so calling it
//! thousands of times per query was the actual bottleneck. Concatenating the
//! dictionary into one NUL-delimited blob and scanning it once with
//! `match_indices` fixed that (see lib.rs's `scan_oracle_words`). This asks
//! the next question: does `memchr::memmem` (SIMD first-byte scan + a
//! Sunday-style skip table) do even better than the standard library's
//! Two-Way search over that same single long blob — the opposite shape from
//! `bench_text_search.rs`'s prior finding (memmem lost there, but that
//! compared many *separate short-haystack* calls, not one long scan).
//!
//! Every contender is asserted result-identical (same word indices) before
//! anything is timed.
//!
//!     cargo test --release bench_word_dict_scan -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store (same archive bench_verify_cost
//! uses — see that module's doc comment for the one-time build command; note
//! its layout changed with the oracle word index, so it needs rebuilding on
//! this branch if it predates that change).

use std::hint::black_box;
use std::time::Instant;

use memchr::memmem;
use rkyv::Archived;

use super::{archive_header, archive_payload, CardData, Mmap, ARCHIVE_HEADER_LEN};

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

/// Word indices (deduped, ascending) that contain `needle`, via std
/// `match_indices` over the blob — the same logic as lib.rs's
/// `scan_oracle_words`, reimplemented locally so this file has no
/// dependency on that function's exact form.
fn matches_std(blob: &str, starts: &[u32], needle: &str) -> Vec<u32> {
    let mut out = Vec::new();
    for (pos, _) in blob.match_indices(needle) {
        let idx = (starts.partition_point(|&s| (s as usize) <= pos) - 1) as u32;
        if out.last() != Some(&idx) {
            out.push(idx);
        }
    }
    out
}

fn matches_memmem(finder: &memmem::Finder, blob: &[u8], starts: &[u32], needle_len: usize) -> Vec<u32> {
    let mut out = Vec::new();
    for pos in finder.find_iter(blob) {
        let _ = needle_len;
        let idx = (starts.partition_point(|&s| (s as usize) <= pos) - 1) as u32;
        if out.last() != Some(&idx) {
            out.push(idx);
        }
    }
    out
}

#[test]
#[ignore = "micro-benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_word_dict_scan_contenders() {
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
    let words = &data.indexes.oracle_trigram.words;
    let blob: &str = words.sparse_blob.as_str();
    let starts: Vec<u32> = words.sparse_word_starts.iter().map(|s| u32::from(*s)).collect();
    println!(
        "\n{} sparse dictionary words, {} byte blob, from {STORE_PATH}",
        words.sparse_words.len(),
        blob.len()
    );

    // Needles spanning the real distribution: a word right at the sparse/dense
    // crossover boundary ("sacrifice" — common but still sparse), a handful of
    // ordinary sparse words, and one absent from the dictionary entirely (the
    // decline path — no allocation should even matter here, but worth timing).
    let needles = ["sacrifice", "hexproof", "planeswalker", "counter", "zzzznotfound"];

    for needle in needles {
        let finder = memmem::Finder::new(needle.as_bytes());
        let std_r = matches_std(blob, &starts, needle);
        let memmem_r = matches_memmem(&finder, blob.as_bytes(), &starts, needle.len());
        assert_eq!(std_r, memmem_r, "mismatch on {needle:?}: std={std_r:?} memmem={memmem_r:?}");

        let n_std = time_ns(|| matches_std(blob, &starts, needle).len() as u32);
        let finder = memmem::Finder::new(needle.as_bytes());
        let n_memmem = time_ns(|| matches_memmem(&finder, blob.as_bytes(), &starts, needle.len()).len() as u32);
        let n_memmem_with_setup = time_ns(|| {
            let f = memmem::Finder::new(needle.as_bytes());
            matches_memmem(&f, blob.as_bytes(), &starts, needle.len()).len() as u32
        });

        println!(
            "{needle:<14} hits={:>4}  std={n_std:>9.0} ns  memmem(reused finder)={n_memmem:>9.0} ns ({:.2}x)  memmem(+setup)={n_memmem_with_setup:>9.0} ns ({:.2}x)",
            std_r.len(),
            n_std / n_memmem,
            n_std / n_memmem_with_setup,
        );
    }
}
