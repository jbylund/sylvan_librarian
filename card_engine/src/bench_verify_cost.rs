//! Micro-benchmark for the verifier cost model (docs/issues/done/
//! 00648-engine-verifier-cost-ordering.md, follow-up to #651).
//!
//! `verify_cost_tier()`/`regex_tier()` group FilterExpr variants into cost
//! clusters used to sort And/Or children cheapest-first before the tri walk.
//! Only Devotion/ManaCostCmp have ever been measured (bench_mana.rs, #651);
//! every other cluster was assigned by judgment. This times the real
//! `FilterExpr::matches()` path — no reimplemented kernels, nothing to
//! compare — against the real corpus archive, so every cluster in the model
//! ends up backed by a number.
//!
//! Nodes are built the same way the query driver builds them: hand-written
//! pre-bind FilterExpr literals, then run through the crate's own bind() /
//! memoize_text_predicates() against the loaded archive, so id-set sizes
//! (ArtistMatch/FlavorMatch/NameMatch/OracleMatch) are real production sizes.
//!
//!     cargo test --release bench_verify_cost -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store (rebuild after any AOracleCard/
//! APrinting layout change — the header's size-of check will reject a stale
//! file):
//!
//!     rm -f benchmarks/verify-order/real.store
//!     .venv/bin/python -c "
//!     import pathlib, sys; sys.path.insert(0, '.')
//!     from scripts.bench_bitplanes import load_engine
//!     load_engine(pathlib.Path('benchmarks/bitplanes/corpus.jsonl'),
//!                 pathlib.Path('benchmarks/verify-order/real.store'))
//!     "

use std::hint::black_box;
use std::time::Instant;

use memchr::memmem;
use regex::Regex;
use rkyv::Archived;

use super::{
    archive_header, archive_payload, expand_text_ids, str_at, trigram_candidates, CardData, CmpOp, ColorField, CollField, FilterExpr,
    Mmap, NumExpr, NumField, TextField, TextSearchField, TYPE_CREATURE, ARCHIVE_HEADER_LEN,
};

const ITERS: usize = 50;
const STORE_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../benchmarks/verify-order/real.store");

fn time_kernel(name: &str, n: usize, mut kernel: impl FnMut() -> u32) -> f64 {
    let mut best = u128::MAX;
    let mut matches = 0;
    for _ in 0..ITERS {
        let t0 = Instant::now();
        matches = black_box(kernel());
        best = best.min(t0.elapsed().as_nanos());
    }
    let ns_per = best as f64 / n as f64;
    println!("  {name:<28} {ns_per:>8.3} ns/card  ({matches} matches)");
    ns_per
}

#[test]
#[ignore = "micro-benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_verify_cost_clusters() {
    let Ok(file) = std::fs::File::open(STORE_PATH) else {
        eprintln!("SKIP: {STORE_PATH} not found (see module docs)");
        return;
    };
    // Safety: same contract as get_mmap() in lib.rs — the file is written by
    // rkyv::to_bytes and replaced atomically; we re-validate the header below
    // before treating the payload as a trusted archive.
    let mmap = unsafe { Mmap::map(&file) }.expect("mmap real.store");
    if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
        eprintln!("SKIP: {STORE_PATH} header mismatch (stale archive — rebuild it, see module docs)");
        return;
    }
    let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };

    let n = data.cards.len();
    println!("\n{n} oracle cards from {STORE_PATH}");

    // One (card, printing) pair per oracle card: the default-preferred
    // printing, same indexing sample_preferred() uses (lib.rs).
    let pairs: Vec<(usize, usize)> = (0..n).map(|cid| (cid, u32::from(data.offsets[cid]) as usize)).collect();

    let run = |name: &str, f: &FilterExpr| -> f64 {
        time_kernel(name, n, || pairs.iter().filter(|&&(cid, pid)| f.matches(&data.cards[cid], &data.printings[pid], &data.strings)).count() as u32)
    };

    // ─── Cluster: mask / field compare (current tier 0) ──────────────────────
    println!("\n-- mask/field compare --");
    let mask_ns: Vec<f64> = vec![
        run("TypeCmp", &FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge }),
        run("ColorCmp", &FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 0b0000_0100 }),
        run(
            "NumericCmp",
            &FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: CmpOp::Lt, rhs: NumExpr::Const(3.0) },
        ),
        run("ExactName", &FilterExpr::ExactName("lightning bolt".to_string())),
        run(
            "TextExact",
            &FilterExpr::TextExact { field: TextField::NameLower, op: CmpOp::Eq, value: "island".to_string() },
        ),
        run("Legality", &FilterExpr::Legality { shift: Some(0), expected: 0b11 }),
        run("DateCmp", &FilterExpr::DateCmp { op: CmpOp::Lt, value: 20_200_101 }),
        run("YearCmp", &FilterExpr::YearCmp { op: CmpOp::Lt, year: 2020 }),
    ];

    // ─── Cluster: memoized-set binary search (current tier 1) ────────────────
    println!("\n-- memoized-set binary search --");
    let mut set_ns: Vec<f64> = Vec::new();

    let mut artist = FilterExpr::TextContains { field: TextSearchField::ArtistLower, word: "guay".to_string() };
    artist.bind(&data.coll_vocab, &data.coll_vocab_sorted, &data.artist_vocab, &data.mana_vocab, &data.indexes.flavor, &data.strings);
    assert!(matches!(artist, FilterExpr::ArtistMatch { .. }), "bind() didn't rewrite to ArtistMatch");
    set_ns.push(run("ArtistMatch", &artist));

    let mut flavor = FilterExpr::TextContains { field: TextSearchField::FlavorTextLower, word: "dragon".to_string() };
    flavor.bind(&data.coll_vocab, &data.coll_vocab_sorted, &data.artist_vocab, &data.mana_vocab, &data.indexes.flavor, &data.strings);
    assert!(matches!(flavor, FilterExpr::FlavorMatch { .. }), "bind() didn't rewrite to FlavorMatch");
    set_ns.push(run("FlavorMatch", &flavor));

    let mut name = FilterExpr::TextContains { field: TextSearchField::NameLower, word: "storm".to_string() };
    name.memoize_text_predicates(&data.cards, &data.strings, &data.indexes.name_trigram, &data.indexes.name_bigrams, &data.indexes.oracle_trigram, n);
    assert!(matches!(name, FilterExpr::NameMatch { .. }), "memoize didn't rewrite to NameMatch (needle too common/rare?)");
    set_ns.push(run("NameMatch", &name));

    let mut oracle = FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: "draw".to_string() };
    oracle.memoize_text_predicates(&data.cards, &data.strings, &data.indexes.name_trigram, &data.indexes.name_bigrams, &data.indexes.oracle_trigram, n);
    assert!(matches!(oracle, FilterExpr::OracleMatch { .. }), "memoize didn't rewrite to OracleMatch (needle too common/rare?)");
    set_ns.push(run("OracleMatch", &oracle));

    let mut coll = FilterExpr::CollectionCmp { field: CollField::Keywords, op: CmpOp::Ge, value: "Flying".to_string(), value_id: None };
    coll.bind(&data.coll_vocab, &data.coll_vocab_sorted, &data.artist_vocab, &data.mana_vocab, &data.indexes.flavor, &data.strings);
    set_ns.push(run("CollectionCmp", &coll));

    // ─── Cluster: text scan (current tier 2) ─────────────────────────────────
    println!("\n-- text scan (unmemoized TextContains) --");
    let scan_ns = run("TextContains", &FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word: "draw".to_string() });

    // ─── Cluster: regex shapes (regex_tier 1 / 2 / 3) ────────────────────────
    println!("\n-- regex shapes --");
    let anchored_ns = run(
        "TextRegex (anchored literal)",
        &FilterExpr::TextRegex { field: TextField::OracleTextLower, regex: Regex::new("(?i)^flying$").unwrap() },
    );
    let bare_ns = run(
        "TextRegex (bare literal)",
        &FilterExpr::TextRegex { field: TextField::OracleTextLower, regex: Regex::new("(?i)flying").unwrap() },
    );
    let machinery_ns = run(
        "TextRegex (machinery, literal prefix)",
        &FilterExpr::TextRegex { field: TextField::OracleTextLower, regex: Regex::new("(?i)draw .* cards?").unwrap() },
    );
    let machinery_noprefix_ns = run(
        "TextRegex (machinery, no prefix)",
        &FilterExpr::TextRegex { field: TextField::OracleTextLower, regex: Regex::new("(?i)^[aeiou]").unwrap() },
    );

    println!("\n-- cluster summary (ns/card) --");
    println!("  mask/field compare : {mask_ns:.3?}");
    println!("  set lookup         : {set_ns:.3?}");
    println!("  text scan          : {scan_ns:.3}");
    println!("  regex anchored     : {anchored_ns:.3}");
    println!("  regex bare literal : {bare_ns:.3}");
    println!("  regex machinery (literal prefix) : {machinery_ns:.3}");
    println!("  regex machinery (no prefix)      : {machinery_noprefix_ns:.3}");
}

/// Three ways to answer the same metacharacter-free substring query
/// (`o:/sacrifice a/` ≡ `o:"sacrifice a"`), all scanning the per-card
/// `oracle_text_lower` strings so the ns/card numbers are directly comparable:
///
///   1. **regex** — `Regex::new("(?i)sacrifice a")` built once, `is_match` per
///      card: what `o:/sacrifice a/` compiles to today (a bare unanchored
///      literal → REGEX_MACHINERY tier, `regex_tier`).
///   2. **str::contains** — `s.contains(needle)`: what `TextContains`'s
///      unmemoized scan runs.
///   3. **memmem::Finder** — built once, `find(...).is_some()` per card: a
///      SIMD substring finder with its Two-Way/prefilter setup amortized over
///      the whole corpus (the same trick `sparse_blob` already uses).
///
/// Motivates lowering metacharacter-free `TextRegex` to `TextContains` (and
/// then to a memmem finder). `cargo test --release bench_substring_finders --
/// --ignored --nocapture`
#[test]
#[ignore = "micro-benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_substring_finders() {
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
    let n = data.cards.len();

    // Pre-resolve each card's lowercased oracle text once (outside the timed
    // loop): all three kernels scan this identical &str slice, so the numbers
    // isolate the substring test itself, not the interned-id lookup.
    let texts: Vec<&str> = (0..n)
        .map(|cid| str_at(&data.strings, u32::from(data.cards[cid].oracle_text_lower_id)).unwrap_or(""))
        .collect();
    let needle = "sacrifice a"; // already lowercase — matches oracle_text_lower

    println!("\n{n} oracle cards — substring finder shootout for {needle:?}");

    let re = Regex::new(&format!("(?i){needle}")).unwrap();
    let regex_ns = time_kernel("regex (?i) is_match", n, || texts.iter().filter(|s| re.is_match(s)).count() as u32);

    let contains_ns = time_kernel("str::contains", n, || texts.iter().filter(|s| s.contains(needle)).count() as u32);

    let finder = memmem::Finder::new(needle.as_bytes());
    let memmem_ns = time_kernel("memmem::Finder (built once)", n, || {
        texts.iter().filter(|s| finder.find(s.as_bytes()).is_some()).count() as u32
    });

    println!("\n  regex / contains  = {:.2}x", regex_ns / contains_ns);
    println!("  regex / memmem    = {:.2}x", regex_ns / memmem_ns);
    println!("  contains / memmem = {:.2}x", contains_ns / memmem_ns);

    // The bigger win is the access path, not the per-row constant. A `TextRegex` node has no
    // `narrow_rec` arm, so the plan scans all n cards; the equivalent `TextContains` narrows via the
    // trigram index to a candidate superset (then verifies). Report that superset size — the rows the
    // rewritten query actually visits — against the full corpus the regex must scan.
    let cand = trigram_candidates(&data.indexes.oracle_trigram.trigrams, needle)
        .map(|tids| expand_text_ids(&data.indexes.oracle_trigram, &tids).len())
        .unwrap_or(n);
    println!("\n  rows visited: regex {n} (full scan) vs contains {cand} trigram-candidates ({:.1}% of corpus)", 100.0 * cand as f64 / n as f64);
    println!("  end-to-end match-phase estimate:");
    println!("    regex    {n} rows x {regex_ns:.1} ns   = {:>8.0} ns", n as f64 * regex_ns);
    println!("    contains {cand} rows x {contains_ns:.1} ns = {:>8.0} ns  ({:.1}x)", cand as f64 * contains_ns, (n as f64 * regex_ns) / (cand as f64 * contains_ns).max(1.0));
    println!("    memmem   {cand} rows x {memmem_ns:.1} ns = {:>8.0} ns  ({:.1}x)", cand as f64 * memmem_ns, (n as f64 * regex_ns) / (cand as f64 * memmem_ns).max(1.0));
}

/// Pins the per-card `NumericCmp` cost for usd/collector_number/year --
/// exactly the fields whose bind-time-cents rewrite (usd) and eval/eval_arith
/// split (all three) this file's own module docs point at as the thing
/// being measured. `cmc` above already covers the generic Field<op>Const
/// leaf; this adds the specific fields the interleaved A/B benchmarks (see
/// commit history) reported real per-printing wins for, so a future
/// regression in either optimization shows up here as a real ns/card
/// number, not just a percentage in a PR description that can't be re-run.
/// usd goes through `bind()` (same as a real query) rather than a
/// hand-converted cents `Const`, so this also exercises the bind rewrite
/// itself, not just field_num's read side.
#[test]
#[ignore = "micro-benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_price_and_range_verify_cost() {
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
    let n = data.cards.len();
    println!("\n{n} oracle cards from {STORE_PATH}");

    let pairs: Vec<(usize, usize)> = (0..n).map(|cid| (cid, u32::from(data.offsets[cid]) as usize)).collect();
    let run = |name: &str, f: &FilterExpr| -> f64 {
        time_kernel(name, n, || pairs.iter().filter(|&&(cid, pid)| f.matches(&data.cards[cid], &data.printings[pid], &data.strings)).count() as u32)
    };

    println!("\n-- price / range NumericCmp (mask/field compare tier) --");
    let mut usd = FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::PriceUsd), op: CmpOp::Lt, rhs: NumExpr::Const(50.0) };
    usd.bind(&data.coll_vocab, &data.coll_vocab_sorted, &data.artist_vocab, &data.mana_vocab, &data.indexes.flavor, &data.strings);
    run("NumericCmp (usd<50)", &usd);
    run(
        "NumericCmp (cn<100)",
        &FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::CollectorNumberInt), op: CmpOp::Lt, rhs: NumExpr::Const(100.0) },
    );
    run("YearCmp (year>2020)", &FilterExpr::YearCmp { op: CmpOp::Gt, year: 2020 });
}
