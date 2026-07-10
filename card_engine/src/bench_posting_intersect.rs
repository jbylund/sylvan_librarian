//! Micro-benchmark comparing posting-list intersection strategies around the
//! sparse/dense storage crossover — surfaced while designing
//! docs/issues/engine-oracle-word-index.md's posting/bitmap crossover
//! section. Question: for lists *below* the ~6.25% memory-parity crossover
//! (stored sparse), is there still a case for treating them as bitmaps
//! purely for intersection speed, and does the shortest-first sequential
//! filter (today's `intersect_sorted`) want merge or galloping search at
//! each step?
//!
//! Four primitives, all counting-only (no candidate materialization, to
//! isolate the comparison cost itself):
//! - `merge_intersect`: classic two-pointer sorted merge, O(a+b).
//! - `gallop_intersect`: binary-search each element of the shorter list into
//!   the longer one, O(a log b).
//! - `bitmap_and_count`: both operands already dense bitmaps, O(n/64) flat,
//!   independent of density.
//! - `probe_into_bitmap`: one operand sparse, one already a dense bitmap —
//!   O(a) with O(1) per probe, no conversion cost paid at all.
//!
//! Synthetic sorted id lists (uniform random, no clustering) over a universe
//! of N=29,088 — this codebase's real oracle-text-corpus scale (distinct
//! texts by content, matching `oracle.gids.len()`) — swept across sizes and
//! ratios that bracket the design doc's crossover (1,818 ids, 6.25%).
//! Synthetic rather than corpus-derived deliberately: this is about mapping
//! out a cost *curve* across sizes/ratios, not validating one specific query.
//!
//!     cargo test --release bench_posting_intersect -- --ignored --nocapture

use std::hint::black_box;
use std::time::Instant;

use rand::RngExt;

const N: usize = 29_088;
const ITERS: usize = 300;

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

fn random_sorted_list(rng: &mut rand::rngs::SmallRng, universe: usize, size: usize) -> Vec<u32> {
    let mut set = std::collections::HashSet::with_capacity(size);
    while set.len() < size {
        set.insert((rng.random::<u64>() as usize % universe) as u32);
    }
    let mut v: Vec<u32> = set.into_iter().collect();
    v.sort_unstable();
    v
}

fn to_bitmap(list: &[u32], universe: usize) -> Vec<u64> {
    let mut bits = vec![0u64; universe.div_ceil(64)];
    for &id in list {
        bits[id as usize / 64] |= 1u64 << (id as usize % 64);
    }
    bits
}

fn merge_intersect(a: &[u32], b: &[u32]) -> u32 {
    let (mut i, mut j) = (0usize, 0usize);
    let mut count = 0u32;
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                count += 1;
                i += 1;
                j += 1;
            }
        }
    }
    count
}

fn gallop_intersect(short: &[u32], long: &[u32]) -> u32 {
    let mut count = 0u32;
    for &v in short {
        if long.binary_search(&v).is_ok() {
            count += 1;
        }
    }
    count
}

fn bitmap_and_count(a: &[u64], b: &[u64]) -> u32 {
    a.iter().zip(b.iter()).map(|(&x, &y)| (x & y).count_ones()).sum()
}

fn probe_into_bitmap(sparse: &[u32], bitmap: &[u64]) -> u32 {
    let mut count = 0u32;
    for &id in sparse {
        let word = bitmap[id as usize / 64];
        if word & (1u64 << (id as usize % 64)) != 0 {
            count += 1;
        }
    }
    count
}

#[test]
#[ignore = "micro-benchmark; synthetic data, no external deps"]
fn bench_posting_intersect_crossover() {
    let mut rng: rand::rngs::SmallRng = rand::make_rng();

    // (size_a, size_b) pairs bracketing the ~1,818-id (6.25%) crossover:
    // close-to-crossover/close ratio, close-to-crossover/large ratio, and a
    // couple of smaller sizes for contrast.
    let configs: &[(usize, usize)] = &[
        (200, 200),
        (200, 1_000),
        (200, 5_000),
        (200, 15_000),
        (1_000, 1_000),
        (1_000, 2_000),
        (1_000, 10_000),
        (1_818, 1_818),
        (1_818, 3_600),
        (1_818, 10_000),
        (1_818, 22_000),
    ];

    println!("\nN={N}, ITERS={ITERS} (best-of), all times in ns");
    println!(
        "{:>6} {:>6}  {:>10} {:>10} {:>10} {:>10}   {:>7}",
        "a", "b", "merge", "gallop", "bitmap_and", "probe->bm", "winner"
    );

    for &(a_size, b_size) in configs {
        let a = random_sorted_list(&mut rng, N, a_size);
        let b = random_sorted_list(&mut rng, N, b_size);
        let bitmap_a = to_bitmap(&a, N);
        let bitmap_b = to_bitmap(&b, N);
        let (short, long_bitmap) = if a.len() <= b.len() { (&a, &bitmap_b) } else { (&b, &bitmap_a) };
        let (short_g, long_g) = if a.len() <= b.len() { (a.as_slice(), b.as_slice()) } else { (b.as_slice(), a.as_slice()) };

        let t_merge = time_ns(|| merge_intersect(&a, &b));
        let t_gallop = time_ns(|| gallop_intersect(short_g, long_g));
        let t_bitmap = time_ns(|| bitmap_and_count(&bitmap_a, &bitmap_b));
        let t_probe = time_ns(|| probe_into_bitmap(short, long_bitmap));

        let times = [("merge", t_merge), ("gallop", t_gallop), ("bitmap_and", t_bitmap), ("probe->bm", t_probe)];
        let winner = times.iter().min_by(|x, y| x.1.partial_cmp(&y.1).unwrap()).unwrap().0;

        println!(
            "{:>6} {:>6}  {:>10.0} {:>10.0} {:>10.0} {:>10.0}   {:>7}",
            a_size, b_size, t_merge, t_gallop, t_bitmap, t_probe, winner
        );
    }
}
