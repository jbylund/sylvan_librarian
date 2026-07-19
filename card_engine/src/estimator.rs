//! Standalone, SOUND, cheap cardinality estimator (PR1 of #702 — see
//! docs/issues/00702-engine-plan-selection-layer.md). NOT wired into query
//! routing; validated only through the `fuzz_row_identity_matches_reference`
//! harness, which asserts the mode="card" reference count always lands inside
//! `[lo, hi]` for every query the corpus throws.
//!
//! Composition happens entirely in card-space (universe N = n_cards) with a
//! printing-varying gate on the AND lower bound and NOT (see
//! `has_printing_varying_leaf` and `estimate_rec`). Printing-space leaf counts
//! (ranges, printing-space tag postings, artist/flavor CSR widths) are
//! projected to card-space per-leaf via `project`.
//!
//! SOUNDNESS is the hard invariant; tightness/cheapness are secondary. When a
//! leaf can't be bounded cheaply we return the trivially-sound "unknown"
//! `{lo:0, est:N/2, hi:N}`.

use super::*;

/// Card-space cardinality facts for plan selection. `lo`/`hi` are SOUND bounds
/// (mode="card" truth always lands inside); `est` is the point estimate.
#[derive(Clone, Copy, Debug)]
#[allow(dead_code)]
pub(crate) struct Cardinality {
    pub lo: u32,
    pub est: u32,
    pub hi: u32,
}

/// The trivially-sound "I don't know" answer over a universe of size `n`.
fn unknown(n: u32) -> Cardinality {
    Cardinality { lo: 0, est: n / 2, hi: n }
}

/// An exact card count: bounds collapse onto the point.
fn exact(c: u32) -> Cardinality {
    Cardinality { lo: c, est: c, hi: c }
}

/// Clamp a `(lo, est, hi)` triple into `[0, n]`, enforce `lo <= hi`, and pin
/// `est` into `[lo, hi]`. The final `est` clamp is a safety rail — if it ever
/// changes `est` materially that signals a composition bug.
fn finalize(lo: u32, est: u32, hi: u32, n: u32) -> Cardinality {
    let hi = hi.min(n);
    let lo = lo.min(hi);
    let est = est.clamp(lo, hi);
    Cardinality { lo, est, hi }
}

/// Project a printing-space count `k` into card-space (PR1 tier: free
/// global-ratio point estimate). Bounds for a projected varying leaf:
/// `lo = (k>0)?1:0`, `hi = min(k, N)`; `est = round(k * N / n_printings)`
/// clamped into `[1, hi]` when `k>0`, else 0.
///
/// TODO(#702): the doc's sampled (map `s` sampled ids) and exact-below-`K`
/// (transition-count on sorted ids) projection tiers are a documented
/// follow-up; PR1 deliberately ships only the free tier.
fn project(k: u32, n_cards: u32, n_printings: u32) -> Cardinality {
    if k == 0 || n_cards == 0 {
        return Cardinality { lo: 0, est: 0, hi: 0 };
    }
    let hi = k.min(n_cards);
    let est = if n_printings == 0 {
        1
    } else {
        let raw = (u64::from(k) * u64::from(n_cards) + u64::from(n_printings) / 2) / u64::from(n_printings);
        (raw as u32).clamp(1, hi)
    };
    Cardinality { lo: 1, est, hi }
}

/// ANY-composition printing-varying classifier: true if ANY leaf in the tree
/// is printing-varying. Deliberately NOT `filter::printing_dependent` (that is
/// an ALL-composition for verifier ordering). Leaf classification copies the
/// field lists from `printing_dependent`'s leaf arms but composes with `.any`.
///
/// `Legality` is treated as varying here (conservative): divergent reprints
/// genuinely vary per-printing (#667), even though `printing_dependent` ranks
/// it invariant for its own (common-case) reason.
pub(crate) fn has_printing_varying_leaf(f: &FilterExpr) -> bool {
    fn num_varying(e: &NumExpr) -> bool {
        match e {
            NumExpr::Const(_) => false,
            NumExpr::Field(field) => matches!(
                field,
                NumField::RarityInt
                    | NumField::CollectorNumberInt
                    | NumField::PriceUsd
                    | NumField::PriceEur
                    | NumField::PriceTix
                    | NumField::PreferScore
            ),
            NumExpr::Arith(lhs, _, rhs) => num_varying(lhs) || num_varying(rhs),
        }
    }
    match f {
        FilterExpr::NumericCmp { lhs, rhs, .. } => num_varying(lhs) || num_varying(rhs),
        FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => true,
        FilterExpr::ArtistMatch { .. } | FilterExpr::FlavorMatch { .. } => true,
        FilterExpr::TextContains { field, .. } => matches!(field, TextSearchField::FlavorTextLower),
        FilterExpr::TextExact { field, .. } | FilterExpr::TextRegex { field, .. } => matches!(
            field,
            TextField::FlavorTextLower | TextField::SetCode | TextField::Border | TextField::Watermark | TextField::CollectorNumber
        ),
        FilterExpr::CollectionCmp { field, .. } => {
            matches!(field, CollField::ArtTags | CollField::IsTags | CollField::FrameData)
        }
        FilterExpr::Legality { .. } => true,
        FilterExpr::And(children) | FilterExpr::Or(children) => children.iter().any(has_printing_varying_leaf),
        FilterExpr::Not(inner) => has_printing_varying_leaf(inner),
        // Exhaustive, not `_ => false`: a new variant must get a considered
        // answer here rather than silently inheriting "invariant".
        FilterExpr::True
        | FilterExpr::ExactName(_)
        | FilterExpr::NameMatch { .. }
        | FilterExpr::OracleMatch { .. }
        | FilterExpr::ColorCmp { .. }
        | FilterExpr::TypeCmp { .. }
        | FilterExpr::ManaCostCmp { .. }
        | FilterExpr::Devotion { .. } => false,
    }
}

/// Whether `f` is a TOTAL two-valued, card-invariant predicate: its card-level
/// `tri` is always True/False, never Null and never PrintingDep. Only then is
/// the complement clean (`|NOT P| = N - |P|` exactly), because a Null card
/// satisfies NEITHER `P` nor `NOT P` under three-valued logic — so a nullable
/// inner's `N - |P|` OVER-counts `NOT P` by the Null cards, and using
/// `N - inner.hi` as a NOT lower bound would be UNSOUND (found by the fuzz
/// harness on `NOT(toughness>3)`: toughness is Null for non-creatures).
///
/// Base cases are exactly the leaves whose `tri` is unconditional `tri_bool`
/// (filter.rs): `True`, `ColorCmp` (colors always present — colorless is mask
/// 0, not Null), `TypeCmp` (type line always present). And/Or/Not of total
/// children stay total (no child can introduce a Null), and all three bases are
/// card-invariant, so totality implies card-invariance.
fn is_total_two_valued(f: &FilterExpr) -> bool {
    match f {
        FilterExpr::True | FilterExpr::ColorCmp { .. } | FilterExpr::TypeCmp { .. } => true,
        FilterExpr::And(children) | FilterExpr::Or(children) => children.iter().all(is_total_two_valued),
        FilterExpr::Not(inner) => is_total_two_valued(inner),
        _ => false,
    }
}

/// Entry point. Derives `n_cards`/`n_printings` from `offsets` (mirrors
/// lib.rs's `narrow_candidates_exact` / `narrow_rec`) and recurses.
#[allow(dead_code)]
pub(crate) fn estimate_cardinality(f: &FilterExpr, indexes: &Archived<CardIndexes>, offsets: &AOffsets) -> Cardinality {
    let n_cards = offsets.len().saturating_sub(1);
    let n_printings = if n_cards == 0 { 0 } else { u32::from(offsets[n_cards]) as usize };
    estimate_rec(f, indexes, n_cards as u32, n_printings as u32)
}

fn estimate_rec(f: &FilterExpr, indexes: &Archived<CardIndexes>, n_cards: u32, n_printings: u32) -> Cardinality {
    let n = n_cards;
    match f {
        FilterExpr::And(children) => compose_and(children, indexes, n_cards, n_printings),
        FilterExpr::Or(children) => compose_or(children, indexes, n_cards, n_printings),
        FilterExpr::Not(inner) => {
            let c = estimate_rec(inner, indexes, n_cards, n_printings);
            if is_total_two_valued(inner) {
                // Total two-valued, card-invariant inner: no Null cards, so the
                // complement is clean — `|NOT P| = N - |P|` — and bounds invert.
                finalize(n.saturating_sub(c.hi), n.saturating_sub(c.est), n.saturating_sub(c.lo), n)
            } else if has_printing_varying_leaf(inner) {
                // Printing-varying inner: the NOT card-set is "some printing does
                // NOT satisfy inner", which is neither the complement of inner's
                // card-projection (a card can have one satisfying and one not) nor
                // bounded below cheaply once Nulls are possible. Only `truth <= N`
                // is sound; `lo = 0`.
                finalize(0, n.saturating_sub(c.est), n, n)
            } else {
                // Card-invariant but possibly-Null inner: `P` and `NOT P` are
                // disjoint (a card is True, False, or Null for the invariant
                // predicate), so `|NOT P| <= N - |P| <= N - inner.lo` (sound
                // upper bound). Null cards are in NEITHER set, so `N - inner.hi`
                // is NOT a sound lower bound — use `lo = 0`.
                finalize(0, n.saturating_sub(c.est), n.saturating_sub(c.lo), n)
            }
        }
        _ => estimate_leaf(f, indexes, n_cards, n_printings),
    }
}

fn compose_and(children: &[FilterExpr], indexes: &Archived<CardIndexes>, n_cards: u32, n_printings: u32) -> Cardinality {
    let n = n_cards;
    if children.is_empty() {
        return exact(n);
    }
    let cs: Vec<Cardinality> = children.iter().map(|c| estimate_rec(c, indexes, n_cards, n_printings)).collect();
    let k = children.len() as u64;

    // Upper bound: always min(child.hi).
    let hi = cs.iter().map(|c| c.hi).min().unwrap_or(n);

    // Point estimate: independence, N * Π(est_i / N).
    let est = if n == 0 {
        0
    } else {
        let mut prod = 1.0f64;
        for c in &cs {
            prod *= f64::from(c.est) / f64::from(n);
        }
        (prod * f64::from(n)).round() as u32
    };

    // Lower bound: Bonferroni ONLY when at most one child is printing-varying
    // (otherwise a card can be in each child's card-projection via DIFFERENT
    // printings while no single printing satisfies all — the AND set is then
    // NOT the intersection of the projections and Bonferroni is unsound).
    let varying = children.iter().filter(|c| has_printing_varying_leaf(c)).count();
    let lo = if varying <= 1 {
        let sum: u64 = cs.iter().map(|c| u64::from(c.lo)).sum();
        sum.saturating_sub((k - 1) * u64::from(n)).min(u64::from(n)) as u32
    } else {
        0
    };

    finalize(lo, est, hi, n)
}

fn compose_or(children: &[FilterExpr], indexes: &Archived<CardIndexes>, n_cards: u32, n_printings: u32) -> Cardinality {
    let n = n_cards;
    if children.is_empty() {
        return Cardinality { lo: 0, est: 0, hi: 0 };
    }
    let cs: Vec<Cardinality> = children.iter().map(|c| estimate_rec(c, indexes, n_cards, n_printings)).collect();

    // OR card-set = union of the children's card-projections (a card matches
    // the OR iff some printing satisfies some child), so these are sound with
    // no varying gate: lo = max(child.lo), hi = min(N, Σ child.hi).
    let lo = cs.iter().map(|c| c.lo).max().unwrap_or(0);
    let hi = cs.iter().map(|c| u64::from(c.hi)).sum::<u64>().min(u64::from(n)) as u32;

    let est = if n == 0 {
        0
    } else {
        let mut prod = 1.0f64;
        for c in &cs {
            prod *= 1.0 - f64::from(c.est) / f64::from(n);
        }
        ((1.0 - prod) * f64::from(n)).round() as u32
    };

    finalize(lo, est, hi, n)
}

/// Plane popcount for a plane-expressible leaf. Returns `(count, existential)`
/// where `existential` marks an existence-projection plane (legality / rarity /
/// border) whose popcount is a card-space superset — see
/// `planes::plane_expr_is_existential`. Guards on plane availability exactly
/// like `narrow_rec`'s plane path.
fn plane_popcount(f: &FilterExpr, indexes: &Archived<CardIndexes>, n_cards: u32) -> Option<(u32, bool)> {
    if n_cards == 0 || u32::from(indexes.planes.n_cards) != n_cards {
        return None;
    }
    let pe = compile_plane(f, &indexes.planes, &indexes.oracle_trigram.words)?;
    let mut bits: Vec<u64> = Vec::new();
    eval_planes(&pe, &indexes.planes, &mut bits);
    let c: u32 = bits.iter().map(|w| w.count_ones()).sum();
    Some((c, plane_expr_is_existential(&pe)))
}

/// A plane-backed leaf. `force_loose` forces the superset treatment
/// (`{lo:0, est:c, hi:c}`) even for a non-existential plane — used for
/// Devotion, whose plane popcount is a documented superset.
fn plane_card(f: &FilterExpr, indexes: &Archived<CardIndexes>, n_cards: u32, force_loose: bool) -> Cardinality {
    match plane_popcount(f, indexes, n_cards) {
        Some((c, existential)) => {
            if existential || force_loose {
                Cardinality { lo: 0, est: c, hi: c }
            } else {
                exact(c)
            }
        }
        None => unknown(n_cards),
    }
}

/// Card count `end - start` from the numeric index's partition_point bounds,
/// WITHOUT materializing the id vec (mirrors `numeric_candidates`). None for
/// Ne (not selective).
fn numeric_count(idx: &Archived<NumericIndex>, op: CmpOp, val: f64) -> Option<u32> {
    let (start, end) = match op {
        CmpOp::Ne => return None,
        CmpOp::Eq => {
            if val.fract() != 0.0 {
                return Some(0);
            }
            let s = idx.partition_point(|p| (i16::from(p.0) as f64) < val);
            let e = idx.partition_point(|p| (i16::from(p.0) as f64) <= val);
            (s, e)
        }
        CmpOp::Lt => (0, idx.partition_point(|p| (i16::from(p.0) as f64) < val)),
        CmpOp::Le => (0, idx.partition_point(|p| (i16::from(p.0) as f64) <= val)),
        CmpOp::Gt => (idx.partition_point(|p| (i16::from(p.0) as f64) <= val), idx.len()),
        CmpOp::Ge => (idx.partition_point(|p| (i16::from(p.0) as f64) < val), idx.len()),
    };
    Some((end - start) as u32)
}

/// Printing count `e - s` for a `[lo, hi)` window over a printing range index,
/// WITHOUT materializing (mirrors `range_narrowed`'s `k = e - s`).
fn range_count(idx: &Archived<PrintingRangeIndex>, lo: u32, hi: u32) -> u32 {
    let s = idx.partition_point(|p| u32::from(p.0) < lo);
    let e = idx.partition_point(|p| u32::from(p.0) < hi);
    (e - s) as u32
}

fn estimate_leaf(f: &FilterExpr, indexes: &Archived<CardIndexes>, n_cards: u32, n_printings: u32) -> Cardinality {
    let n = n_cards;
    match f {
        FilterExpr::True => exact(n),

        // ExactName's exact card count is the name-range width, which needs the
        // `cards` slice (lib.rs:2831-2833) — not in this entry point's
        // signature — so PR1 returns the sound "unknown" for it. (Not exercised
        // by the fuzz corpus, which emits TextExact{NameLower}, not ExactName.)
        FilterExpr::ExactName(_) => unknown(n),

        FilterExpr::NumericCmp { lhs, op, rhs } => {
            // Only the simple `Field(f) op Const(c)` / `Const op Field(f)`
            // shapes; Arith and Field-vs-Field bail to unknown.
            let (field, op, c) = match (lhs, rhs) {
                (NumExpr::Field(fld), NumExpr::Const(c)) => (*fld, *op, *c),
                (NumExpr::Const(c), NumExpr::Field(fld)) => (*fld, flip_op(*op), *c),
                _ => return unknown(n),
            };
            match field {
                NumField::Cmc => numeric_count(&indexes.cmc, op, c).map_or_else(|| unknown(n), exact),
                NumField::Power => numeric_count(&indexes.power, op, c).map_or_else(|| unknown(n), exact),
                NumField::Toughness => numeric_count(&indexes.toughness, op, c).map_or_else(|| unknown(n), exact),
                // Card-space "any-printing-at-rarity" count = the mode=card
                // some-match count for a single rarity leaf. narrow_rarity's
                // set is loose (for Not), but its POPCOUNT is exact here.
                NumField::RarityInt => {
                    narrow_rarity(indexes, n_cards as usize, op, c).map_or_else(|| unknown(n), |nar| exact(nar.set.len() as u32))
                }
                // Printing-space integer-cent range → project (varying).
                NumField::PriceUsd => match int_range_bounds(op, snap_to_nearest_cent(c * PRICE_CENTS_PER_DOLLAR)) {
                    None => unknown(n),
                    Some(None) => project(0, n_cards, n_printings),
                    Some(Some((lo, hi))) => project(range_count(&indexes.price_usd, lo, hi), n_cards, n_printings),
                },
                // Printing-space integer range → project (varying).
                NumField::CollectorNumberInt => match int_range_bounds(op, c) {
                    None => unknown(n),
                    Some(None) => project(0, n_cards, n_printings),
                    Some(Some((lo, hi))) => project(range_count(&indexes.collector_number, lo, hi), n_cards, n_printings),
                },
                // No index (price_eur/price_tix are not indexed) or unindexed
                // fields (loyalty/edhrec/prefer_score) → sound unknown.
                NumField::PriceEur | NumField::PriceTix | NumField::Loyalty | NumField::EdhrEc | NumField::PreferScore => unknown(n),
            }
        }

        // Card-invariant exact plane popcount.
        FilterExpr::ColorCmp { .. } | FilterExpr::TypeCmp { .. } => plane_card(f, indexes, n_cards, false),

        // Existence-projection plane → superset {0, c, c}.
        FilterExpr::Legality { .. } => plane_card(f, indexes, n_cards, true),

        // Devotion Ge/Gt: saturated-bucket plane superset {0, c, c}; other ops
        // are not plane-narrowable → unknown.
        FilterExpr::Devotion { op: CmpOp::Ge | CmpOp::Gt, .. } => plane_card(f, indexes, n_cards, true),
        FilterExpr::Devotion { .. } => unknown(n),

        FilterExpr::CollectionCmp { field, op: CmpOp::Ge, value, .. } => {
            // Mirror narrow_rec's field dispatch (lib.rs:3040-3047).
            let (idx, card_space, complete) = match field {
                CollField::Subtypes => (&indexes.subtypes, true, true),
                CollField::Keywords => (&indexes.keywords, true, true),
                CollField::OracleTags => (&indexes.oracle_tags, true, true),
                CollField::ArtTags => (&indexes.art_tags, false, true),
                CollField::IsTags => (&indexes.is_tags, false, true),
                CollField::FrameData => (&indexes.frame_data, false, false),
            };
            match idx.get(value.as_str()) {
                Some(v) => {
                    let cnt = v.len() as u32;
                    if card_space {
                        exact(cnt)
                    } else {
                        project(cnt, n_cards, n_printings)
                    }
                }
                None if complete => {
                    // Absent from a complete index ⇒ matches nothing.
                    if card_space {
                        exact(0)
                    } else {
                        project(0, n_cards, n_printings)
                    }
                }
                // frame_data drops dense values at build (#628), so absence
                // proves nothing → unknown.
                None => unknown(n),
            }
        }
        FilterExpr::CollectionCmp { .. } => unknown(n),

        // Printing-space CSR width sums → project (varying).
        FilterExpr::ArtistMatch { ids } => {
            let k: usize = ids
                .iter()
                .map(|&a| (u32::from(indexes.artists.offsets[a as usize + 1]) - u32::from(indexes.artists.offsets[a as usize])) as usize)
                .sum();
            project(k as u32, n_cards, n_printings)
        }
        FilterExpr::FlavorMatch { dense_ids, .. } => {
            let flavor = &indexes.flavor;
            let k: usize = dense_ids
                .iter()
                .map(|&d| (u32::from(flavor.offsets[d as usize + 1]) - u32::from(flavor.offsets[d as usize])) as usize)
                .sum();
            project(k as u32, n_cards, n_printings)
        }

        // Memoize-produced card-space id sets (never emitted by bind(), so not
        // seen in the fuzz corpus). NameMatch keys on card_name_id — names are
        // unique per oracle card, so ids.len() is the exact card count.
        // OracleMatch keys on oracle_text_lower_id; see the soundness caveat in
        // the module report (shared oracle-text ids could in principle make
        // gids.len() an undercount; not exercised here).
        FilterExpr::NameMatch { ids } => exact(ids.len() as u32),
        FilterExpr::OracleMatch { gids } => exact(gids.len() as u32),

        FilterExpr::DateCmp { op, value } => match date_range_bounds(*op, *value) {
            None => unknown(n),
            Some((lo, hi)) => project(range_count(&indexes.released_at, lo, hi), n_cards, n_printings),
        },
        FilterExpr::YearCmp { op, year } => match year_range_bounds(*op, *year) {
            None => unknown(n),
            Some((lo, hi)) => project(range_count(&indexes.released_at, lo, hi), n_cards, n_printings),
        },

        // set:X postings → project (varying); every other TextExact/TextRegex
        // and all TextContains/ManaCostCmp are unindexed here → unknown.
        FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value } => {
            let k = indexes.set_codes.get(value.as_str()).map_or(0, |v| v.len());
            project(k as u32, n_cards, n_printings)
        }

        FilterExpr::TextExact { .. }
        | FilterExpr::TextRegex { .. }
        | FilterExpr::TextContains { .. }
        | FilterExpr::ManaCostCmp { .. } => unknown(n),

        // Composites are handled in estimate_rec; reaching here is a bug.
        FilterExpr::And(_) | FilterExpr::Or(_) | FilterExpr::Not(_) => unknown(n),
    }
}
