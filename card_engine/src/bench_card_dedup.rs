//! Micro-benchmark, split into two purposes:
//!
//! 1. Regression guard for `cards_of_printings`' direct-array projection
//!    (`printing_to_card`, shipped — see
//!    docs/issues/local-engine-direct-projection-arrays.md), benchmarked here
//!    against synthetic offsets at real corpus scale.
//! 2. Still-open exploration of `groups_of_printings`/`printing_to_global_group`
//!    for crossover axis 4 of docs/issues/local-engine-broad-range-fastpath.md
//!    — deferred, not shipped; kept here as the recorded evidence for that
//!    future decision.
//!
//! Synthetic offsets at real scale (31,508 cards / 97,206 printings, ~3.09 printings/card
//! average) rather than corpus-derived — this is about the cost *shape* at realistic scale, not
//! validating one exact query, same reasoning as bench_posting_intersect.rs.
//!
//!     cargo test --release bench_card_dedup -- --ignored --nocapture

use std::hint::black_box;
use std::time::Instant;

use rand::RngExt;
use rkyv::{rancor::Error, Archived};

use super::{build_printing_to_card, cards_of_printings, scatter_bits, AOffsets};

const N_CARDS: usize = 31_508;
const ITERS: usize = 50;

/// Prototype for the still-open question in crossover axis 4: would a
/// groups_of_printings, mirroring cards_of_printings but deduping by (card, local
/// artwork_group_id) instead of by card alone, close artwork mode's gap toward card
/// mode's? Unlike cards_of_printings' small-k path, adjacent-compare-after-sort
/// doesn't work here even for small k: printings within one card's range can visit
/// local group ids in any order (0, 1, 0, 2, ...), so equal groups from the same
/// card aren't necessarily adjacent in a printing-index-sorted walk the way card ids
/// are (cards own contiguous ranges; groups don't have that property within a
/// card's range). Always bitmap-scatter here rather than replicate that assumption
/// incorrectly.
fn groups_of_printings(offsets: &AOffsets, group_offsets: &[u32], local_group_ids: &[u16], printing_ids: &[u32], n_groups: usize) -> usize {
    let global_ids = printing_ids.iter().map(|&p| {
        let card = offsets.partition_point(|o| u32::from(*o) <= p) as u32 - 1;
        group_offsets[card as usize] + u32::from(local_group_ids[p as usize])
    });
    let bits = scatter_bits(global_ids, n_groups);
    bits.iter().map(|w| w.count_ones() as usize).sum()
}

/// Direct `printing_id -> global_group_id` lookup, precomputed once -- the same
/// mechanism as `build_printing_to_card`, but deferred (not shipped) since nothing
/// in the current codebase would consume it. See "Finding" in
/// docs/issues/local-engine-direct-projection-arrays.md for why: `unique=artwork`'s
/// real implementation dedupes per-card locally, not via a global projection.
fn printing_to_global_group(offsets: &[u32], group_offsets: &[u32], local_group_ids: &[u16]) -> Vec<u32> {
    let mut out = vec![0u32; local_group_ids.len()];
    for (card, w) in offsets.windows(2).enumerate() {
        for p in w[0]..w[1] {
            out[p as usize] = group_offsets[card] + u32::from(local_group_ids[p as usize]);
        }
    }
    out
}

fn groups_of_printings_direct(printing_to_group: &[u32], printing_ids: &[u32], n_groups: usize) -> usize {
    let bits = scatter_bits(printing_ids.iter().map(|&p| printing_to_group[p as usize]), n_groups);
    bits.iter().map(|w| w.count_ones() as usize).sum()
}

fn time_ns(mut kernel: impl FnMut() -> usize) -> f64 {
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

/// Real per-card printing-count shape is heavily right-skewed (most cards 1-4 printings, a
/// long tail to ~385 for basic lands) -- approximated here with a mostly-small-count
/// distribution plus an occasional large one, landing on the real total printing count.
fn synthetic_offsets(rng: &mut rand::rngs::SmallRng) -> (Vec<u32>, usize) {
    let mut offsets = Vec::with_capacity(N_CARDS + 1);
    offsets.push(0u32);
    let mut total = 0u32;
    for i in 0..N_CARDS {
        let n = if i % 500 == 0 { rng.random_range(50..120) } else { rng.random_range(1..=6) };
        total += n;
        offsets.push(total);
    }
    (offsets, total as usize)
}

fn archive_u32_vec(v: &[u32]) -> Vec<u8> {
    rkyv::to_bytes::<Error>(&v.to_vec()).expect("serialize").to_vec()
}

/// Real corpus shape (benchmarks/bitplanes/corpus.jsonl): 46,112 distinct illustrations over
/// 31,508 cards / 97,206 printings -- 1.46 groups/card average, i.e. n_groups sits much closer
/// to n_cards (1.46x) than to n_printings (47.4%). Approximated per-card here the same way
/// printing counts are: mostly 1-2 groups, occasional larger (a card with several distinct
/// arts), scaled to land near the real n_groups/n_printings ratio.
fn synthetic_groups(rng: &mut rand::rngs::SmallRng, offsets: &[u32]) -> (Vec<u32>, Vec<u16>, usize) {
    let mut group_offsets = Vec::with_capacity(N_CARDS + 1);
    group_offsets.push(0u32);
    let mut local_group_ids = vec![0u16; *offsets.last().unwrap() as usize];
    let mut total_groups = 0u32;
    for w in offsets.windows(2) {
        let n_printings_this_card = (w[1] - w[0]) as usize;
        let n_groups_this_card = rng.random_range(1..=n_printings_this_card.min(3)).max(1) as u16;
        for (i, slot) in local_group_ids[w[0] as usize..w[1] as usize].iter_mut().enumerate() {
            *slot = (i % n_groups_this_card as usize) as u16;
        }
        total_groups += u32::from(n_groups_this_card);
        group_offsets.push(total_groups);
    }
    (group_offsets, local_group_ids, total_groups as usize)
}

#[test]
#[ignore = "micro-benchmark; synthetic data at real corpus scale"]
fn bench_card_dedup_broad_vs_selective() {
    let mut rng: rand::rngs::SmallRng = rand::make_rng();
    let (offsets_vec, n_printings) = synthetic_offsets(&mut rng);
    let offsets_bytes = archive_u32_vec(&offsets_vec);
    let offsets: &AOffsets = rkyv::access::<Archived<Vec<u32>>, Error>(&offsets_bytes).expect("access offsets");
    let printing_to_card_vec = build_printing_to_card(&offsets_vec);
    let printing_to_card_bytes = archive_u32_vec(&printing_to_card_vec);
    let printing_to_card: &AOffsets = rkyv::access::<Archived<Vec<u32>>, Error>(&printing_to_card_bytes).expect("access printing_to_card");
    let (group_offsets, local_group_ids, n_groups) = synthetic_groups(&mut rng, &offsets_vec);
    let group_lookup = printing_to_global_group(&offsets_vec, &group_offsets, &local_group_ids);

    println!("\nN_CARDS={N_CARDS}, n_printings={n_printings}, n_groups={n_groups}, ITERS={ITERS} (best-of), all times in ns");
    println!(
        "{:>10} {:>10}  {:>10} {:>10}  {:>10} {:>10}  {:>10} {:>10}",
        "k", "match_pct", "cards (shipped)", "ns/match", "groups (bsearch)", "ns/match", "groups_dir (deferred)", "ns/match"
    );

    // Real k values from the actual corpus at each usd<X threshold in the selectivity sweep
    // (benchmarks/bitplanes/corpus.jsonl, 97,206 real printings): 0.1, 0.5, 1, 5, 10, 25, 50.
    // Scaled to this synthetic offsets' n_printings so the *fraction* matches the real sweep.
    let real_fracs = [6114.0 / 97206.0, 50614.0 / 97206.0, 57854.0 / 97206.0, 71308.0 / 97206.0, 75738.0 / 97206.0, 79158.0 / 97206.0, 80527.0 / 97206.0];
    for &frac in &real_fracs {
        let k = (n_printings as f64 * frac) as usize;
        let k = k.min(n_printings);
        let mut printing_ids: Vec<u32> = {
            let mut set = std::collections::HashSet::with_capacity(k);
            while set.len() < k {
                set.insert(rng.random_range(0..n_printings as u32));
            }
            set.into_iter().collect()
        };
        printing_ids.sort_unstable();

        let t_cards = time_ns(|| cards_of_printings(offsets, printing_to_card, &printing_ids).len());
        let t_groups = time_ns(|| groups_of_printings(offsets, &group_offsets, &local_group_ids, &printing_ids, n_groups));
        let t_groups_dir = time_ns(|| groups_of_printings_direct(&group_lookup, &printing_ids, n_groups));
        let pct = 100.0 * k as f64 / n_printings as f64;
        println!(
            "{:>10} {:>9.1}%  {:>15.0} {:>10.2}  {:>16.0} {:>10.2}  {:>21.0} {:>10.2}",
            k,
            pct,
            t_cards,
            t_cards / k as f64,
            t_groups,
            t_groups / k as f64,
            t_groups_dir,
            t_groups_dir / k as f64
        );
    }
}
