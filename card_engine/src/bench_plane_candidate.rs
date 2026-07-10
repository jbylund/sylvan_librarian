//! Micro-benchmark for the plane/candidate fast path (PLANE_CANDIDATE_MAX):
//! `eval_plane_bit` per candidate (short-circuiting per-candidate via
//! `PlaneExpr::eval_bit` — see its doc comment in planes.rs) vs.
//! `eval_planes` once + `bitmap_contains` per candidate, isolated from all
//! Python/PyO3/query-driver overhead so the crossover is measured on the two
//! primitives alone, not swamped by downstream materialize/sort/paginate
//! cost that scales with match count regardless of which one wins (an
//! end-to-end Python-level sweep couldn't resolve this cleanly for exactly
//! that reason).
//!
//! A sort-and-group-by-word variant (candidates sharing a word reuse one
//! `eval_word` call) was also tried and dropped: measured, it never beat
//! *both* `eval_plane_bit` and `eval_planes` at the same candidate count —
//! wherever grouping beat the plain per-candidate check, `eval_planes` had
//! already overtaken both. See docs/issues/engine-plane-candidate-fastpath.md.
//!
//! `even` spreads candidate ids evenly across the whole id space (the
//! adversarial case — real lookups aren't this deliberately spread); `random`
//! is a uniform random sample, the realistic stand-in for an ExactName/tag/
//! keyword lookup.
//!
//!     cargo test --release bench_plane_candidate -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store (same archive bench_verify_cost
//! uses — see that module's doc comment for the one-time build command).

use std::hint::black_box;
use std::time::Instant;

use rkyv::Archived;

use super::{archive_header, archive_payload, compile_plane, eval_plane_bit, eval_planes, bitmap_contains, CardData, ColorField, CmpOp, FilterExpr, Mmap, TYPE_CREATURE, ARCHIVE_HEADER_LEN};

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
fn bench_plane_candidate_crossover() {
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

    let green = FilterExpr::ColorCmp { field: ColorField::Colors, op: CmpOp::Ge, mask: 16 };
    let creature = FilterExpr::TypeCmp { mask: TYPE_CREATURE, op: CmpOp::Ge };
    let simple = compile_plane(&green, &data.indexes.planes).expect("c:g must compile to a plane");
    let compound = compile_plane(&FilterExpr::And(vec![green, creature]), &data.indexes.planes).expect("c:g t:creature must compile to a plane");

    type IdGen = Box<dyn Fn(usize) -> Vec<u32>>;
    let sizes = [1usize, 2, 4, 8, 16, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 2048];
    for (label, expr) in [("simple (c:g)", &simple), ("compound (c:g t:creature)", &compound)] {
        let dists: [(&str, IdGen); 2] = [
            ("even (adversarial max-spread)", Box::new(move |count: usize| (0..count).map(|i| ((i * n) / count) as u32).collect())),
            ("random (realistic tag/keyword lookup)", Box::new(move |count: usize| {
                let mut state: u64 = 0x9E3779B97F4A7C15;
                (0..count)
                    .map(|_| {
                        state ^= state << 13;
                        state ^= state >> 7;
                        state ^= state << 17;
                        (state % n as u64) as u32
                    })
                    .collect()
            })),
        ];
        for (dist_name, ids_for) in &dists {
            println!("\n-- {label}, {dist_name} --");
            println!("{:>6} {:>14} {:>14} {:>8}", "count", "eval_bit ns", "eval_planes ns", "ratio");
            for &count in &sizes {
                let ids = ids_for(count);

                let bit_ns = time_ns(|| ids.iter().filter(|&&cid| eval_plane_bit(expr, &data.indexes.planes, cid)).count() as u32);

                let mut bitmap: Vec<u64> = Vec::new();
                let planes_ns = time_ns(|| {
                    eval_planes(expr, &data.indexes.planes, &mut bitmap);
                    ids.iter().filter(|&&cid| bitmap_contains(&bitmap, cid)).count() as u32
                });

                let ratio = planes_ns / bit_ns;
                println!("{count:>6} {bit_ns:>14.1} {planes_ns:>14.1} {ratio:>8.2}");
            }
        }
    }
}
