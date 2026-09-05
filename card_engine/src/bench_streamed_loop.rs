//! Micro-benchmark that decomposes `StreamedSelect`'s match loop, the companion to
//! `bench_gather_loop`.
//!
//! **Cache state fixed, and the rates are corpus-size dependent (2026-08-03).** Chunk ROTATION (each
//! iteration walks a different slice) plus per-cell STAGGER (cells sharing a group walk different slices
//! in the same iteration) means no walk inherits another's cache lines, and the reported rate is the
//! MEDIAN over rotated chunks rather than the luckiest minimum. Both were needed: rotation alone still
//! had cell A at 10.50 ns/card against cell B's 6.11 on identical cards, because every cell on a group
//! walked the same chunk and the first paid all the misses. `scripts/upscale_corpus.py` supplies stores
//! big enough to rotate (the real corpus gives the wide group ONE chunk at 4,500 cards), selected with
//! `BENCH_LOOP_STORE`.
//!
//! Swept over 31,508 / 126,032 / 409,604 oracle cards:
//!
//!     P4  LOOP  ns/card         6.27   11.37   15.04     2.4x
//!     P4  SCAN  ns/printing     2.27    3.03    2.24     flat
//!     P4  PUSH  ns/match        1.51    4.94    6.98     4.6x
//!     P3  all_match ns/card     2.58    2.54    2.55     FLAT
//!     P3  residual  ns/card     5.08   11.33   18.15     3.6x
//!     P3  SCAN  ns/printing     3.30    5.57   10.85     3.3x
//!
//! P3's all_match arm being flat across a 13x corpus is the check that the method works: that path reads
//! only the card record and does offset arithmetic, so it has no misses to gain, while everything that
//! walks printings grows 3x+. A rate is therefore not a property of the code alone -- it is a property of
//! the code AND how much of the archive fits in cache, and `cost.rs` has no term for the second.
//!
//! At the production corpus size the shipped P4 constants are confirmed: LOOP 6.27 against 6.88 (9%
//! low), SCAN 2.27 against 2.06 (10% high), PUSH 1.51 against 2.24. That is the retraction closed from
//! the other side -- warm measurement said 2.98 and was wrong; cold measurement says 6.27 and agrees
//! with what ships.
//!
//! P3 does NOT agree: 2.58 against a shipped 5.05 per card, 5.08 against 11.63 with a residual, 3.30
//! against 5.97 per printing -- about 2x over-costed. Both plans were measured identically, so this is
//! not a cache artifact, but it is measured on `ns_loop` ONLY, and P3's arm may be absorbing setup or
//! finish cost that its loop never pays. That has to be ruled out before the gap is called an error.
//!
//! The routing consequence is the durable one: the plans' rates scale DIFFERENTLY with corpus size, so
//! the P3/P4 balance drifts as the corpus grows even with every constant left alone. Any refit is
//! calibrated to the corpus it was measured on.
//!
//! **RETRACTION (2026-08-03): every rate this harness reports is a WARM-CACHE rate, and the shipped
//! constants are not too high.** `ITERS` walks one card list repeatedly and keeps the minimum, so by the
//! second pass every card, printing and string it touches is resident. Production walks a candidate set
//! once. Measuring the first pass against the warm minimum on cells that run after the mmap has faulted
//! in gives 1.6-2.2x (A' 1.91x/1.74x, I 1.83x/1.59x, H 2.17x/2.14x; the 100x+ figures on the first cells
//! are first-touch page faults on the 68 MB store, not cache effects).
//!
//! That 1.6-2x is the whole discrepancy. Warm 2.98 ns/card against 6.34 fitted on traffic is 2.1x; P3's
//! warm 2.41 against a shipped 5.05 is 2.1x; the warm push 1.06 against 2.00 fitted is 1.9x. The
//! counter/feature ratios all read 1.00, so the features were never the gap -- the TIME was, and the
//! shipped constants include the miss cost this harness removes.
//!
//! So the five refits below did not fail because routing is a delicate joint surface. They failed
//! because they lowered rates toward a cache state production never reaches. Read every ns figure in
//! this file as "warm", useful for the SHAPE of the loop -- which terms exist, which are degenerate, how
//! artwork differs -- and not as a candidate constant. Those shape findings stand; the levels do not.
//!
//! That harness established that P4 cannot be fixed alone: three successively better descriptions of
//! its loop each REGRESSED routing (+43%, +8.8%, +40%), because `plan_cost` is only ever used
//! comparatively and P4's inflated arm is absorbing an over-estimate on P3's side. So P3's loop gets
//! the same built-design treatment, and the two arms move together.
//!
//! P3's loop is a different shape from P4's, and the difference decides the design:
//!
//!   - It has NO per-match cost. The loop writes `counts[cid] = c` and accumulates `total`; nothing is
//!     pushed. `STREAM_EMIT_PER_MATCH_NS` belongs to `ns_finish`, not here.
//!   - `scan_units` is GATED on a residual. Under `all_match` (and outside artwork mode)
//!     `card_match_count` answers from offset arithmetic without touching a printing, so the loop is
//!     O(cards) regardless of how many printings those cards have.
//!
//! Two rates instead of three is what makes this tractable where P4 was not. P4 needed three rates and
//! no single mode could identify them, because card mode pushes once per card (`matches == cards`) and
//! printing mode pushes every printing (`matches == printings`), so a column duplicated another in
//! every mode. Two rates against three printings-per-card levels is identifiable INSIDE one mode, so
//! P3 needs neither pooling nor the reparameterisation P4 required.
//!
//! The cells:
//!
//!     all_match, per mode      cards visited only; printings never touched. Isolates the per-card
//!                              overhead with `card_pass` short-circuited and counting O(1).
//!     residual, per mode       `card_pass` runs, returns PrintingDep, and `card_match_count` walks.
//!                              Gives (CARD_PASS + tier floor) per card and SCAN per printing.
//!
//! The residual predicate is `DateCmp` against a date every printing satisfies. Release date is a
//! PRINTING field, so `card_pass` cannot resolve it and must hand back a residual — which is the whole
//! point, since an always-true CARD predicate would answer `Tri::True` and never walk. Every printing
//! matching keeps the counters clean: `printings_examined` is the full span in printing mode, and in
//! card mode it is 1 per card because `card_match_count` returns at the first qualifying printing.
//! `DateCmp` is a `MASK_COMPARE_NS100` tier, the cheapest real residual, so the fitted per-card figure
//! is a floor on `STREAM_RESIDUAL_FLOOR_NS` rather than a typical value.
//!
//! What both harnesses together establish, which is the point of having them (2026-08-03).
//!
//! P3's loop is over-costed like P4's, and by more: 5.05 shipped against 2.41 measured per card on the
//! all_match path, 11.63 (5.05 + the 6.58 floor) against 4.32 with a residual, 5.97 against 2.92 per
//! printing. So BOTH scan arms are inflated ~2-3x as rates.
//!
//! Five refits were then taken through the regret gate, interleaved A/B/A/B:
//!
//!     P4 mode-blind triple                      +43%
//!     P4 pooled + artwork arm                  +8.8%
//!     P4 reparameterised                        +40%
//!     P3 and P4 both refit                      +30%
//!     ... plus symmetric residual floors        +90%
//!     P3+P4 refit AGAIN after fixing scan_units +54% (mean regret 0.50 -> 0.56 us/query)
//!
//! **The sixth attempt (2026-08-23) is not a repeat of the first four.** It came after fixing a real
//! feature bug (`scan_units`'s card-mode overcharge, `local-engine-card-residual-pass-rate.md`'s
//! companion) and refitting P3+P4 jointly on the corrected data -- the exact prerequisite this file's
//! own conclusion asked for. It still regressed, but through a DIFFERENT mechanism than the first five:
//! not P3-vs-P4 compensating error (fixing the feature and refitting both arms together handles that),
//! but P3/P4-vs-`PrintingCompose` distortion. `PrintingCompose` was deliberately left unrefit (its own
//! `Perm` arm is missing a `cards_visited` feature -- see `local-engine-compose-build-rates.md`), so
//! making P3/P4 cheaper without touching it made them win against `PrintingCompose` in cases where
//! `PrintingCompose` was actually correct: `PrintingCompose -> StreamedSelect` went from a minor cell to
//! 309 queries at 99% miss rate and 32% of all lost time, and the `printing_compose` acquire branch's
//! total share jumped to 72%. Reverted. The lesson generalizes past this one pair: **a joint refit is
//! only as joint as every plan that competes in the same argmin, not just the two you fixed a feature
//! for.** `PrintingCompose` needs its own feature fix (the `cards_visited` estimator) before ANY of its
//! rates -- or any rate for a plan that regularly competes against it -- can be safely moved again.
//! Full mechanism and revert data:
//! [local-engine-p3-p4-joint-refit-vs-compose.md](../../docs/issues/local-engine-p3-p4-joint-refit-vs-compose.md).
//!
//! The fitted values that attempt computed, for whoever builds the `PrintingCompose` feature fix and
//! retries this (`fit_cost_model.py` on ~111k/~83k rows post `scan_units` fix, `mirror_matches_engine`
//! 99.8%; `x` is fitted/shipped):
//!
//!     GatheredScan                          shipped  fitted     x
//!       GATHER_LOOP_PER_CARD_NS                3.88    4.52  1.17
//!       GATHER_SCAN_PER_ROW_NS                 2.06    2.71  1.32
//!       CARD_PASS+FLOOR (call held at 3.00)   21.89   24.07  1.10   -> floor 21.07
//!       GATHER_PUSH_PER_MATCH_NS               2.24    2.05  0.92
//!       GATHER_SELECT_PER_PAGE_SLOT_NS         3.51    3.32  0.94
//!       GATHER_COLLECT_PER_PAGE_ROW_NS         9.79    8.41  0.86
//!       GATHER_ARTWORK_PER_PRINTING_NS         0.50    0.51  1.02   (kernel-measured; left alone)
//!       GATHER_FIXED_COST_NS                 169.60   93.59  0.55   (curvature-confounded; NOT applied)
//!
//!     StreamedSelect                         shipped  fitted     x
//!       STREAM_LOOP_PER_CARD_NS                2.58    3.20  1.24
//!       STREAM_SCAN_PER_ROW_NS                 5.97    5.90  0.99
//!       CARD_PASS+FLOOR (call held at 2.47)    9.05   10.42  1.15   -> floor 7.95
//!       STREAM_EMIT_PER_MATCH_NS               0.12    0.11  0.95
//!       STREAM_PERM_STEP_NS                    1.00    1.24  1.24
//!       STREAM_ARTWORK_SEEN_PER_CARD_NS        1.21    1.10  0.91
//!       STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS   1.02    0.98  0.96   (SUPERSEDED -- Round 81 split this
//!                                                                    term in two and the sweep half now
//!                                                                    ships at 0.30. The 0.98 here and
//!                                                                    the "already confirmed" note both
//!                                                                    read a per-MATCH cost as per-card;
//!                                                                    see the constant's own doc and the
//!                                                                    gather-row caveat printed below.)
//!       STREAM_CORPUS_PASS_PER_CARD_NS         0.02    0.01  0.67   (2 sig figs on an unseparable
//!                                                                    constant; left alone)
//!       STREAM_FIXED_COST_NS                 217.00  192.69  0.89   (curvature-confounded; NOT applied)
//!
//! `plan_cost_model_matches_gold` on these: 97.7% -> 98.9%. Total routing regret on a uniform sample:
//! 0.50 -> 0.56 us/query mean, i.e. the gold-test metric improved while the thing that actually matters
//! got worse -- the reason to gate on the full regret-matrix breakdown, not a single summary number.
//!
//! Every one regressed, and the best was the one that moved LEAST from the shipped values. Two of the
//! attempts were diagnosed and corrected mid-flight -- artwork needed its own arm, and leaving P4's
//! residual floor at 18.89 while P3's went to 1.91 was an asymmetry that sent
//! `StreamedSelect -> GatheredScan` from 407 queries at 38% of lost time to 653 at 44%. Fixing that
//! asymmetry then made things worse still, because 18.89 was load-bearing despite being unmeasured.
//!
//! The conclusion is not about any constant. Every rate here is now measured against realized counters
//! on a built design, and the rates are demonstrably wrong while the PRODUCTS they form are roughly
//! right -- which is the signature of compensating error in the FEATURES, not the rates. `plan_cost`
//! multiplies a rate by an estimate; a rate 2x high against an estimate 2x low predicts correctly and
//! routes correctly, and correcting only the rate breaks it. The shipped constants are a jointly-tuned
//! routing surface, not a set of independently valid ns figures, and that is why they have survived.
//!
//! So the next work is on the estimators (`eval_domain`, `scan_units`, `matches`) graded against the
//! realized counters these harnesses publish -- not another refit. Correcting a feature and its rate
//! TOGETHER is the only move that can hold the products fixed while making both halves true.
//!
//! Calls the real `exec_streamed_select` and reads `ns_setup`/`ns_loop`/`ns_finish` and the counters
//! off `PhaseStats` — same fenceposts and counters production publishes, nothing reimplemented.
//!
//!     cargo test --release bench_streamed_loop -- --ignored --nocapture
//!
//! Needs benchmarks/verify-order/real.store, shared with `bench_verify_cost` and `bench_gather_loop`;
//! rebuild it the same way (see `bench_verify_cost`'s module docs).

use super::bench_loop_design::{store_path, CARD_COUNTS, ITERS, LIMIT, WIDE_MIN_PRINTINGS};
use std::hint::black_box;

use rkyv::Archived;

use super::{
    archive_header, archive_payload, exec_streamed_select, take_phase_stats, CardData, CmpOp, FilterExpr, Mmap, NarrowedRepr,
    NumExpr, NumField, PreparedCandidates, QueryCtx, QueryParams, ARCHIVE_HEADER_LEN,
};

/// A release date no printing in the corpus reaches, so the residual predicate is always true. Keeps
/// every printing matching, so `printings_examined` and `matches` are exactly the span (printing mode)
/// or 1 per card (card mode, which returns at the first match) instead of a corpus-dependent fraction.
const DATE_AFTER_EVERYTHING: u32 = 99_999_999;



/// One measured design point.
struct Cell {
    label: &'static str,
    n_cards_req: usize,
    mode: &'static str,
    /// True for the cells whose `card_pass` runs and leaves a printing-dependent residual behind. The
    /// two groups are fitted apart: with no residual the loop never walks a printing, so pooling them
    /// would regress a real printing column against a phase that did not pay for it.
    residual: bool,
    cards: f64,
    printings: f64,
    matches: f64,
    ns_setup: f64,
    ns_loop: f64,
    ns_finish: f64,
}

/// A single per-card rate for one group, for the groups where that is all the design can support.
///
/// Two of the four groups are degenerate, and measurement is what showed it rather than assumption.
/// The `all_match` rows examine ZERO printings at every printings-per-card level, because
/// `card_match_count` answers from offset arithmetic there -- so the printing column is identically
/// zero and there is nothing to fit but the per-card rate. The `card` + residual rows examine exactly
/// ONE printing per card at every level, because the kernel returns at the first qualifying printing --
/// so the printing column DUPLICATES the card column and only their sum is identifiable. Reporting one
/// number for those groups is the honest form; a two-parameter fit there would be picking arbitrarily
/// from a ridge of equally good answers.
fn fit_per_card(cells: &[Cell], mode: &str, residual: bool) -> Option<f64> {
    let (mut num, mut den) = (0.0f64, 0.0f64);
    for c in cells.iter().filter(|c| c.mode == mode && c.residual == residual) {
        let w = 1.0 / c.ns_loop;
        let x = c.cards * w;
        num += x * (c.ns_loop * w);
        den += x * x;
    }
    if den <= 0.0 {
        return None;
    }
    Some(num / den)
}

/// Per-card and per-printing rates for one (mode, residual) group. Two unknowns, three ratio levels.
fn fit_pair(cells: &[Cell], mode: &str, residual: bool) -> Option<[f64; 2]> {
    let mut normal = [[0.0f64; 3]; 2];
    let mut rows = 0;
    for c in cells.iter().filter(|c| c.mode == mode && c.residual == residual) {
        // Relative least squares: each equation divided by its own measured time, so a cell running
        // 100 µs does not outweigh one running 5 µs.
        let w = 1.0 / c.ns_loop;
        let x = [c.cards * w, c.printings * w];
        for i in 0..2 {
            for j in 0..2 {
                normal[i][j] += x[i] * x[j];
            }
            normal[i][2] += x[i] * (c.ns_loop * w);
        }
        rows += 1;
    }
    let det = normal[0][0] * normal[1][1] - normal[0][1] * normal[1][0];
    if rows < 2 || det.abs() < 1e-12 {
        return None;
    }
    Some([
        (normal[0][2] * normal[1][1] - normal[0][1] * normal[1][2]) / det,
        (normal[0][0] * normal[1][2] - normal[1][0] * normal[0][2]) / det,
    ])
}

#[test]
#[ignore = "micro-benchmark; needs benchmarks/verify-order/real.store (see module docs)"]
fn bench_streamed_loop() {
    let path = store_path();
    let Ok(file) = std::fs::File::open(&path) else {
        eprintln!("SKIP: {path} not found (see module docs)");
        return;
    };
    // Safety: same contract as bench_verify_cost / get_mmap() — written by rkyv::to_bytes and replaced
    // atomically, and the header is re-validated below before the payload is trusted.
    let mmap = unsafe { Mmap::map(&file) }.expect("mmap real.store");
    if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
        eprintln!("SKIP: {path} header mismatch (stale archive — rebuild it, see module docs)");
        return;
    }
    let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
    let ctx = QueryCtx::from(data);

    let (mut singleton, mut medium, mut wide): (Vec<u32>, Vec<u32>, Vec<u32>) = (Vec::new(), Vec::new(), Vec::new());
    for cid in 0..data.cards.len() {
        let span = u32::from(data.offsets[cid + 1]) as usize - u32::from(data.offsets[cid]) as usize;
        if span == 1 {
            singleton.push(cid as u32);
        } else if span >= WIDE_MIN_PRINTINGS {
            wide.push(cid as u32);
        } else {
            medium.push(cid as u32);
        }
    }
    println!(
        "\n{} oracle cards: {} with 1 printing, {} with 2..{WIDE_MIN_PRINTINGS}, {} with >={WIDE_MIN_PRINTINGS}",
        data.cards.len(),
        singleton.len(),
        medium.len(),
        wide.len()
    );

    // `cmc >= -1` is always true and lives on the CARD, so `card_pass` resolves it to Tri::True with an
    // empty residual — the all_match-shaped cells. `DateCmp` is always true but lives on the PRINTING,
    // so `card_pass` must return PrintingDep and the loop walks; that is the residual shape.
    let no_residual = FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc), op: CmpOp::Ge, rhs: NumExpr::Const(-1.0) };
    let printing_residual = FilterExpr::DateCmp { op: CmpOp::Lt, value: DATE_AFTER_EVERYTHING };

    struct Config {
        label: &'static str,
        n_cards_req: usize,
        mode: &'static str,
        residual: bool,
        /// The whole group; each iteration walks a different chunk and each CELL is staggered onto a
        /// different chunk, so no walk inherits another's cache lines. Holding one slice and taking a
        /// minimum is what made the earlier rates warm-cache numbers, ~2x under production.
        group: Vec<u32>,
        params: QueryParams,
    }
    // (label, group, unique, residual). `all_match_known` is set only on the no-residual cells; with a
    // printing-dependent residual it must be false or the walk would be short-circuited and the cell
    // would measure the other group again.
    let designs: [(&'static str, &Vec<u32>, &str, bool); 12] = [
        ("1p   card   all_match", &singleton, "card", false),
        ("med  card   all_match", &medium, "card", false),
        ("wide card   all_match", &wide, "card", false),
        ("1p   print  all_match", &singleton, "printing", false),
        ("med  print  all_match", &medium, "printing", false),
        ("wide print  all_match", &wide, "printing", false),
        ("1p   card   residual", &singleton, "card", true),
        ("med  card   residual", &medium, "card", true),
        ("wide card   residual", &wide, "card", true),
        ("1p   print  residual", &singleton, "printing", true),
        ("med  print  residual", &medium, "printing", true),
        ("wide print  residual", &wide, "printing", true),
    ];

    let mut configs: Vec<Config> = Vec::new();
    for n_req in CARD_COUNTS {
        for (label, group, unique, residual) in &designs {
            if group.len() < n_req {
                println!("{label:<24}{n_req:>7}   SKIP (only {} cards in group)", group.len());
                continue;
            }
            configs.push(Config {
                label,
                n_cards_req: n_req,
                mode: unique,
                residual: *residual,
                group: (*group).clone(),
                params: QueryParams::from_strs(unique, "default", "name", "asc", LIMIT, 0),
            });
        }
    }

    let mut best_setup = vec![f64::INFINITY; configs.len()];
    let mut best_finish = vec![f64::INFINITY; configs.len()];
    let mut counters = vec![(0.0, 0.0, 0.0); configs.len()];
    // Median across rotated chunks is the honest rate; with rotation every pass walks unfamiliar cards.
    let mut per_card_samples: Vec<Vec<f64>> = vec![Vec::with_capacity(ITERS); configs.len()];
    for iter in 0..ITERS {
        for (i, cfg) in configs.iter().enumerate() {
            let filter = if cfg.residual { &printing_residual } else { &no_residual };
            let chunks = (cfg.group.len() / cfg.n_cards_req).max(1);
            let start = ((iter + i) % chunks) * cfg.n_cards_req;
            let end = (start + cfg.n_cards_req).min(cfg.group.len());
            let prep = PreparedCandidates {
                candidate_cards: Some(cfg.group[start..end].to_vec()),
                all_match_known: !cfg.residual,
                // The loop is driven directly here; no narrowing ran, so nothing is proven.
                proven_conjuncts: 0,
                narrowed_repr: NarrowedRepr::Cards,
            };
            black_box(exec_streamed_select(&ctx, &cfg.params, filter, &prep, None));
            let s = take_phase_stats();
            per_card_samples[i].push(s.ns_loop as f64 / (s.cards_visited.max(1)) as f64);
            best_setup[i] = best_setup[i].min(s.ns_setup as f64);
            best_finish[i] = best_finish[i].min(s.ns_finish as f64);
            counters[i] = (s.cards_visited as f64, s.printings_examined as f64, s.matches_pushed as f64);
        }
    }

    let mut cells: Vec<Cell> = Vec::new();
    println!(
        "\n{:<24}{:>7}{:>9}{:>11}{:>10}{:>10}{:>11}{:>10}{:>10}",
        "cell", "n", "cards", "printings", "matches", "ns_setup", "ns_loop", "ns/card", "ns_finish"
    );
    for (i, cfg) in configs.iter().enumerate() {
        let (cards, printings, matches) = counters[i];
        let cell = Cell {
            label: cfg.label,
            n_cards_req: cfg.n_cards_req,
            mode: cfg.mode,
            residual: cfg.residual,
            cards,
            printings,
            matches,
            ns_setup: best_setup[i],
            ns_loop: {
                let v = &mut per_card_samples[i];
                v.sort_by(f64::total_cmp);
                v[v.len() / 2] * cards
            },
            ns_finish: best_finish[i],
        };
        println!(
            "{:<24}{:>7}{:>9.0}{:>11.0}{:>10.0}{:>10.0}{:>11.0}{:>10.2}{:>10.0}",
            cell.label,
            cell.n_cards_req,
            cell.cards,
            cell.printings,
            cell.matches,
            cell.ns_setup,
            cell.ns_loop,
            cell.ns_loop / cell.cards.max(1.0),
            cell.ns_finish
        );
        cells.push(cell);
    }

    // The design's leverage, stated rather than assumed. The all_match rows are the check that P3's
    // loop really is O(cards): printings-per-card varies ~7x across them while the printing column
    // should stay near zero, because `card_match_count` answers from offset arithmetic there.
    println!("\nprintings examined per card (all_match rows should stay ~0 -- the loop never walks):");
    for c in &cells {
        println!(
            "  {:<24}{:>7}  printings/card {:>6.2}   matches/card {:>6.2}",
            c.label,
            c.n_cards_req,
            c.printings / c.cards.max(1.0),
            c.matches / c.cards.max(1.0)
        );
    }

    println!("\n{:<34}{:>12}{:>16}   note", "fitted per (mode, residual)", "ns/card", "ns/printing");
    let mut recovered: Vec<(&str, f64, f64)> = Vec::new();
    for mode in ["card", "printing"] {
        for residual in [false, true] {
            let tag = format!("{mode} / {}", if residual { "residual" } else { "all_match" });
            // Only printing-mode-with-residual has a non-degenerate printing column; everything else
            // gets the one rate it can support, with why printed alongside so the table is readable
            // without the source.
            match (mode, residual) {
                ("printing", true) => match fit_pair(&cells, mode, residual) {
                    Some(pair) => {
                        println!("{tag:<34}{:>12.2}{:>16.2}   both identifiable", pair[0], pair[1]);
                        recovered.push(("printing/residual", pair[0], pair[1]));
                    }
                    None => println!("{tag:<34}   pair not identifiable"),
                },
                _ => {
                    let why = if residual { "printings==cards, so this is their SUM" } else { "printings==0, no printing term exists" };
                    match fit_per_card(&cells, mode, residual) {
                        Some(per_card) => {
                            println!("{tag:<34}{per_card:>12.2}{:>16}   {why}", "--");
                            recovered.push((if residual { "card/residual" } else { "all_match" }, per_card, 0.0));
                        }
                        None => println!("{tag:<34}   no cells"),
                    }
                }
            }
        }
    }

    // Cross-check, the counterpart to bench_gather_loop's two independent recoveries of PUSH. Card mode
    // with a residual examines exactly one printing per card, so its single fitted rate must equal
    // printing mode's per-card PLUS per-printing. Those come from different cells and different
    // columns, so agreement is a test of whether the loop is linear in these two counters at all.
    let card_resid = recovered.iter().find(|r| r.0 == "card/residual").map(|r| r.1);
    let print_resid = recovered.iter().find(|r| r.0 == "printing/residual").copied();
    if let (Some(cr), Some((_, pc, pp))) = (card_resid, print_resid) {
        let predicted = pc + pp;
        println!("\ncross-check (independent cells, independent columns):");
        println!("  card/residual fitted directly          {cr:>7.2} ns/card");
        println!("  printing/residual per-card + per-row   {predicted:>7.2} ns/card  ({pc:.2} + {pp:.2})");
        let (lo, hi) = (cr.min(predicted), cr.max(predicted));
        println!("  spread {:>.2}x  (linearity in these two counters requires these to agree)", hi / lo.max(1e-9));
    }

    // The finish phase, per branch, against the quantity each branch is actually driven by. This is what
    // fits STREAM_PERM_STEP_NS and STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS, and it needs cells on both
    // sides of STREAM_MIN_MATCHES to do it -- hence the 600-card row.
    println!("\nfinish phase by branch (n_cards = {}):", data.cards.len());
    println!("  {:<24}{:>7}{:>10}{:>12}{:>13}{:>12}", "cell", "n", "matches", "ns_finish", "branch", "ns per unit");
    for c in &cells {
        // Mirrors run_query_streamed's guards: an empty result or a page past the end returns before
        // either branch, so those cells drive nothing and are labelled rather than fitted.
        let (branch, units) = if c.matches <= 0.0 {
            ("none", 0.0)
        } else if c.matches <= 1024.0 {
            ("gather", data.cards.len() as f64)
        } else {
            ("perm walk", (LIMIT as f64 * data.cards.len() as f64 / c.matches).min(data.cards.len() as f64))
        };
        let per_unit = if units > 0.0 { c.ns_finish / units } else { 0.0 };
        println!(
            "  {:<24}{:>7}{:>10.0}{:>12.0}{:>13}{:>12.3}",
            c.label, c.n_cards_req, c.matches, c.ns_finish, branch, per_unit
        );
    }
    println!(
        "\n  gather rows are ns per card scanned, against a shipped STREAM_SMALL_TOTAL_FLOOR_PER_CARD_NS\n  \
         of 0.30; perm-walk rows are ns per permutation entry, against STREAM_PERM_STEP_NS. Grade the\n  \
         step ESTIMATE against the realized `perm_steps` counter separately -- this table assumes it.\n\n  \
         READ THE GATHER ROWS AS A SLOPE, NOT AS A LEVEL. The divisor is the whole corpus while the redo\n  \
         loop's per-match work (card_pass + push_card_matches) rides its `matches` column, so the per-unit\n  \
         figure GROWS with the match count and only its intercept is the sweep rate this constant means.\n  \
         The 1.02 that shipped until Round 81 came from reading a 600-match cell's per-unit figure as a\n  \
         level; the 100- and 400-match cells here put the intercept near 0.30 and the slope near one\n  \
         printing's push, which is now STREAM_REDO_SCAN_PER_ROW_NS over `stream_redo_printings`."
    );

    println!(
        "\n  shipped: STREAM_CARD_PASS_NS 5.05 per card, STREAM_SCAN_PER_ROW_NS 5.97 per printing,\n  \
         STREAM_RESIDUAL_FLOOR_NS 6.58 added per card when a residual exists. So the residual rows here\n  \
         compare against 5.05 + 6.58 = 11.63 per card, and the all_match rows against 5.05 with no\n  \
         printing term at all -- which the zero printing column above confirms is the right shape."
    );
}
