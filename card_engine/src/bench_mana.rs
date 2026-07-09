//! Micro-benchmark for mana-comparison representations (issue #650).
//!
//! Times the comparison kernels directly — no query machinery, no
//! subprocesses — over the real distribution of mana costs (one row per
//! oracle card, exported from the corpus by
//! `python - <<… benchmarks/mana/mana_costs.tsv`). Three contenders, all
//! built from the same cost strings in one process:
//!
//!   hashmap — the shipped representation this branch replaces:
//!             HashMap<String, u8> pips + Option<HashMap> devotion, with the
//!             evaluation loops lifted verbatim from the old filter arms
//!   sortvec — sorted (interned symbol id, count) pairs, merge-walk compares
//!   packed  — 8-bit lanes in a u64 (WUBRGC/S/X) + hybrid overflow vec,
//!             SWAR compares (what ManaCost now ships)
//!
//! Every contender is asserted result-identical on every (card, query) pair
//! before anything is timed, so the run doubles as a parity suite over the
//! full real-data distribution.
//!
//!     cargo test --release bench_mana -- --ignored --nocapture

use std::collections::HashMap;
use std::hint::black_box;
use std::time::Instant;

use super::{lane_add, lanes_ge, mana_lane, CmpOp, LANES6_HI, LANES8_HI};

const ITERS: usize = 50;

fn is_devotion_sym(s: &str) -> bool {
    s.len() == 1 && "WUBRGC".contains(s)
}

/// Card-side pip parse, mirroring api's mana_cost_str_to_dict: braced symbols
/// keep everything non-numeric (X and S included), unbraced color characters
/// count too.
fn card_pip_counts(cost: &str) -> HashMap<String, u8> {
    let upper = cost.to_uppercase();
    let mut pips: HashMap<String, u8> = HashMap::new();
    let mut rest = String::new();
    let mut chars = upper.chars();
    while let Some(c) = chars.next() {
        if c == '{' {
            let sym: String = chars.by_ref().take_while(|&c| c != '}').collect();
            if sym.parse::<u32>().is_err() {
                *pips.entry(sym).or_insert(0) += 1;
            }
        } else {
            rest.push(c);
        }
    }
    for c in rest.chars() {
        if "WUBRGC".contains(c) {
            *pips.entry(c.to_string()).or_insert(0) += 1;
        }
    }
    pips
}

// ─── Contender: hashmap (the old ManaCost, eval loops verbatim) ──────────────

struct OldCost {
    pips: HashMap<String, u8>,
    devotion: Option<HashMap<String, u8>>,
    cmc: f32,
}

fn old_cost(pips: HashMap<String, u8>, cmc: f32) -> OldCost {
    let devotion = if pips.keys().any(|s| s.contains('/')) {
        let mut d: HashMap<String, u8> = HashMap::new();
        for (sym, &n) in &pips {
            if sym.contains('/') {
                for part in sym.split('/') {
                    if is_devotion_sym(part) {
                        *d.entry(part.to_string()).or_insert(0) += n;
                    }
                }
            } else {
                *d.entry(sym.clone()).or_insert(0) += n;
            }
        }
        Some(d)
    } else {
        None
    };
    OldCost { pips, devotion, cmc }
}

fn old_mana_cmp(card: &OldCost, op: CmpOp, pips: &HashMap<String, u8>, cmc: f32) -> bool {
    let card_cmc = card.cmc;
    let card_pips = &card.pips;
    let contains = || pips.iter().all(|(sym, &n)| card_pips.get(sym.as_str()).copied().unwrap_or(0) >= n) && card_cmc >= cmc;
    let subset = || card_pips.iter().all(|(sym, n)| pips.get(sym.as_str()).copied().unwrap_or(0) >= *n) && card_cmc <= cmc;
    let exact = || {
        card_cmc == cmc
            && card_pips.len() == pips.len()
            && pips.iter().all(|(sym, &n)| card_pips.get(sym.as_str()).copied().unwrap_or(0) == n)
    };
    match op {
        CmpOp::Ge => contains(),
        CmpOp::Le => subset(),
        CmpOp::Eq => exact(),
        CmpOp::Gt => contains() && !exact(),
        CmpOp::Lt => subset() && !exact(),
        CmpOp::Ne => !exact(),
    }
}

fn old_devotion_cmp(card: &OldCost, op: CmpOp, pips: &HashMap<String, u8>) -> bool {
    let devotion = card.devotion.as_ref().unwrap_or(&card.pips);
    let ge = pips.iter().all(|(sym, &n)| devotion.get(sym.as_str()).copied().unwrap_or(0) >= n);
    let le = devotion
        .iter()
        .filter(|(sym, _)| is_devotion_sym(sym.as_str()))
        .all(|(sym, n)| pips.get(sym.as_str()).copied().unwrap_or(0) >= *n);
    let eq = devotion.keys().filter(|sym| is_devotion_sym(sym.as_str())).count() == pips.len()
        && pips.iter().all(|(sym, &n)| devotion.get(sym.as_str()).copied().unwrap_or(0) == n);
    match op {
        CmpOp::Ge => ge,
        CmpOp::Eq => eq,
        CmpOp::Le => le,
        CmpOp::Gt => ge && !eq,
        CmpOp::Lt => le && !eq,
        CmpOp::Ne => !eq,
    }
}

// ─── Contender: sortvec (interned ids, merge-walk compares) ──────────────────

struct VecCost {
    pips: Vec<(u8, u8)>, // (symbol id, count), sorted by id — every symbol interned
    cmc: f32,
}

fn vec_count(pips: &[(u8, u8)], id: u8) -> u8 {
    pips.iter().find(|e| e.0 == id).map_or(0, |e| e.1)
}

fn vec_mana_cmp(card: &VecCost, op: CmpOp, pips: &[(u8, u8)], cmc: f32) -> bool {
    let contains = || pips.iter().all(|&(id, n)| vec_count(&card.pips, id) >= n) && card.cmc >= cmc;
    let subset = || card.pips.iter().all(|&(id, n)| vec_count(pips, id) >= n) && card.cmc <= cmc;
    let exact = || card.cmc == cmc && card.pips == pips;
    match op {
        CmpOp::Ge => contains(),
        CmpOp::Le => subset(),
        CmpOp::Eq => exact(),
        CmpOp::Gt => contains() && !exact(),
        CmpOp::Lt => subset() && !exact(),
        CmpOp::Ne => !exact(),
    }
}

// ─── Contender: packed (what ManaCost now ships) ─────────────────────────────

struct PackedCost {
    core: u64,
    hybrids: Vec<(u8, u8)>,
    devotion: u64,
    cmc: f32,
}

fn packed_cost(pips: &HashMap<String, u8>, cmc: f32, vocab: &mut Vec<String>) -> PackedCost {
    let mut core = 0u64;
    let mut devotion = 0u64;
    let mut hybrids: Vec<(u8, u8)> = Vec::new();
    for (sym, &n) in pips {
        match mana_lane(sym) {
            Some(lane) => {
                core = lane_add(core, lane, n);
                if lane < 6 {
                    devotion = lane_add(devotion, lane, n);
                }
            }
            None => {
                let id = vocab.iter().position(|v| v == sym).unwrap_or_else(|| {
                    vocab.push(sym.clone());
                    vocab.len() - 1
                });
                hybrids.push((id as u8, n));
                for part in sym.split('/') {
                    if let Some(lane) = mana_lane(part).filter(|&l| l < 6) {
                        devotion = lane_add(devotion, lane, n);
                    }
                }
            }
        }
    }
    hybrids.sort_unstable();
    PackedCost { core, hybrids, devotion, cmc }
}

fn packed_mana_cmp(card: &PackedCost, op: CmpOp, core: u64, hybrid_ids: &[(u8, u8)], cmc: f32) -> bool {
    let hyb_count = |id: u8| card.hybrids.iter().find(|e| e.0 == id).map_or(0, |e| e.1);
    let contains = || {
        lanes_ge(card.core, core, LANES8_HI)
            && hybrid_ids.iter().all(|&(id, n)| hyb_count(id) >= n)
            && card.cmc >= cmc
    };
    let subset = || {
        lanes_ge(core, card.core, LANES8_HI)
            && card.hybrids.iter().all(|e| hybrid_ids.iter().find(|q| q.0 == e.0).map_or(0, |q| q.1) >= e.1)
            && card.cmc <= cmc
    };
    let exact = || card.cmc == cmc && card.core == core && card.hybrids == hybrid_ids;
    match op {
        CmpOp::Ge => contains(),
        CmpOp::Le => subset(),
        CmpOp::Eq => exact(),
        CmpOp::Gt => contains() && !exact(),
        CmpOp::Lt => subset() && !exact(),
        CmpOp::Ne => !exact(),
    }
}

fn packed_devotion_cmp(card: &PackedCost, op: CmpOp, pips: u64) -> bool {
    let ge = lanes_ge(card.devotion, pips, LANES6_HI);
    let le = lanes_ge(pips, card.devotion, LANES6_HI);
    let eq = card.devotion == pips;
    match op {
        CmpOp::Ge => ge,
        CmpOp::Eq => eq,
        CmpOp::Le => le,
        CmpOp::Gt => ge && !eq,
        CmpOp::Lt => le && !eq,
        CmpOp::Ne => !eq,
    }
}

// ─── Harness ─────────────────────────────────────────────────────────────────

fn time_kernel(name: &str, n_cards: usize, mut kernel: impl FnMut() -> u32) {
    let mut best = u128::MAX;
    let mut matches = 0;
    for _ in 0..ITERS {
        let t0 = Instant::now();
        matches = black_box(kernel());
        best = best.min(t0.elapsed().as_nanos());
    }
    println!("  {name:<52} {:>7.2} ns/card  ({matches} matches)", best as f64 / n_cards as f64);
}

#[test]
#[ignore = "micro-benchmark; needs benchmarks/mana/mana_costs.tsv"]
fn bench_mana_representations() {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../benchmarks/mana/mana_costs.tsv");
    let Ok(raw) = std::fs::read_to_string(path) else {
        eprintln!("SKIP: {path} not found (see module docs)");
        return;
    };
    let rows: Vec<(HashMap<String, u8>, f32)> = raw
        .lines()
        .map(|l| {
            let (cost, cmc) = l.split_once('\t').unwrap_or((l, "0"));
            (card_pip_counts(cost), cmc.parse().unwrap_or(0.0))
        })
        .collect();
    let n = rows.len();
    println!("\n{n} oracle-card cost rows from {path}");

    // Build all three representations, plus the sortvec interner (which also
    // covers the lane symbols — sortvec interns *every* symbol).
    let mut packed_vocab: Vec<String> = Vec::new();
    let mut vec_vocab: Vec<String> = Vec::new();
    let mut vec_intern = |sym: &str| -> u8 {
        let id = vec_vocab.iter().position(|v| v == sym).unwrap_or_else(|| {
            vec_vocab.push(sym.to_string());
            vec_vocab.len() - 1
        });
        id as u8
    };
    let old: Vec<OldCost> = rows.iter().map(|(p, c)| old_cost(p.clone(), *c)).collect();
    let packed: Vec<PackedCost> = rows.iter().map(|(p, c)| packed_cost(p, *c, &mut packed_vocab)).collect();
    let vecs: Vec<VecCost> = rows
        .iter()
        .map(|(p, c)| {
            let mut pips: Vec<(u8, u8)> = p.iter().map(|(sym, &n)| (vec_intern(sym), n)).collect();
            pips.sort_unstable();
            VecCost { pips, cmc: *c }
        })
        .collect();

    // Query specs: (label, pips, cmc) — mana= uses all ops, devotion the
    // color lanes only. Distribution-realistic shapes.
    let mana_queries: &[(&str, &[(&str, u8)], f32)] = &[
        ("mana ge {B}{B}", &[("B", 2)], 2.0),
        ("mana ge {W}", &[("W", 1)], 1.0),
        ("mana ge {R/G}", &[("R/G", 1)], 1.0),
        ("mana eq {W}{U}", &[("W", 1), ("U", 1)], 2.0),
        ("mana le {2}{W}{W}", &[("W", 2)], 4.0),
        ("mana ne {G}", &[("G", 1)], 1.0),
    ];
    let dev_queries: &[(&str, &[(&str, u8)])] = &[
        ("devotion ge B=1", &[("B", 1)]),
        ("devotion ge G=2", &[("G", 2)]),
        ("devotion ge U=3", &[("U", 3)]),
        ("devotion eq W=1", &[("W", 1)]),
        ("devotion le R=1", &[("R", 1)]),
    ];
    let op_of = |label: &str| match label.split_whitespace().nth(1).unwrap() {
        "ge" => CmpOp::Ge,
        "le" => CmpOp::Le,
        "eq" => CmpOp::Eq,
        "ne" => CmpOp::Ne,
        other => panic!("unknown op {other}"),
    };

    for &(label, qpips, qcmc) in mana_queries {
        let op = op_of(label);
        let qmap: HashMap<String, u8> = qpips.iter().map(|&(s, n)| (s.to_string(), n)).collect();
        let mut qvec: Vec<(u8, u8)> = qpips.iter().map(|&(s, n)| (vec_intern(s), n)).collect();
        qvec.sort_unstable();
        let mut qcore = 0u64;
        let mut qhyb: Vec<(u8, u8)> = Vec::new();
        for &(s, n) in qpips {
            match mana_lane(s) {
                Some(lane) => qcore = lane_add(qcore, lane, n),
                None => qhyb.push((packed_vocab.iter().position(|v| v == s).map_or(u8::MAX, |i| i as u8), n)),
            }
        }
        qhyb.sort_unstable();

        // Parity across the full distribution before timing anything.
        for i in 0..n {
            let want = old_mana_cmp(&old[i], op, &qmap, qcmc);
            assert_eq!(vec_mana_cmp(&vecs[i], op, &qvec, qcmc), want, "{label}: sortvec diverges at row {i}");
            assert_eq!(packed_mana_cmp(&packed[i], op, qcore, &qhyb, qcmc), want, "{label}: packed diverges at row {i}");
        }
        println!("{label}");
        time_kernel("hashmap", n, || old.iter().filter(|c| old_mana_cmp(c, op, &qmap, qcmc)).count() as u32);
        time_kernel("sortvec", n, || vecs.iter().filter(|c| vec_mana_cmp(c, op, &qvec, qcmc)).count() as u32);
        time_kernel("packed", n, || packed.iter().filter(|c| packed_mana_cmp(c, op, qcore, &qhyb, qcmc)).count() as u32);
    }

    for &(label, qpips) in dev_queries {
        let op = op_of(label);
        let qmap: HashMap<String, u8> = qpips.iter().map(|&(s, n)| (s.to_string(), n)).collect();
        let mut qpacked = 0u64;
        for &(s, n) in qpips {
            qpacked = lane_add(qpacked, mana_lane(s).unwrap(), n);
        }
        for i in 0..n {
            let want = old_devotion_cmp(&old[i], op, &qmap);
            assert_eq!(packed_devotion_cmp(&packed[i], op, qpacked), want, "{label}: packed diverges at row {i}");
        }
        println!("{label}");
        time_kernel("hashmap", n, || old.iter().filter(|c| old_devotion_cmp(c, op, &qmap)).count() as u32);
        time_kernel("packed", n, || packed.iter().filter(|c| packed_devotion_cmp(c, op, qpacked)).count() as u32);
    }
}
