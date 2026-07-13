//! Transposed card-space bitplanes for low-cardinality dimensions (issue #630).
//!
//! One bitset per (dimension, value): bit i of a plane says card i has that
//! value. At ~31.5k cards a plane is ~4 kB of plain u64 words, so a filter
//! subtree rewritten into word-wise AND/OR/NOT over cache-resident planes
//! evaluates in microseconds — versus the ~31.5k filter-tree dispatches (and a
//! cache line per card) the driver loop pays to prove the same bits one card
//! at a time.
//!
//! The exactly-consumable dimensions: colors, color identity, the card type
//! bits, and (bit-sliced, two saturating bits per color) devotion counts. All
//! are card-level and two-valued (their tri() never returns Null or
//! PrintingDep), so plane algebra — including complement for Not —
//! reproduces the filter's card-level truth exactly.
//! Legality (docs/issues/engine-legality-divergent-carveout.md, generalized
//! to banned/restricted by docs/issues/engine-legality-banned-restricted-
//! planes.md, #678) is exact via two planes per (format, status) -- an
//! `_EXISTS` plane ("some printing has this status") and an `_ABSENT`/
//! `_ILLEGAL` plane ("some printing doesn't"), both computed directly from
//! printings so they're correct for every card including ones whose
//! printings disagree, unlike a single card-level bit. Rarity (the 4 most
//! common values; docs/issues/engine-rarity-planes.md) is narrowing-only, for
//! the PrintingDep reason legality used to have -- see PLANE_RARITY.

use rkyv::{Archive, Deserialize, Serialize};

use super::filter::{CmpOp, ColorField, FilterExpr, NumExpr, NumField, TextSearchField};
use super::legality::{LEGALITY_BANNED, LEGALITY_LEGAL, LEGALITY_RESTRICTED, MAX_FORMATS};
use super::{flip_op, lane_get, oracle_word_eligible, scan_oracle_words, OracleCard, OracleWordIndex, Printing};

/// Plane layout, plane-major in BitPlanes.words: six color planes per color
/// field (W U B R G C, bit order matching color_to_bit — C is always zero for
/// colors/identity but keeps the mask algebra total over whatever mask the
/// parser emits), then one plane per card type bit.
const COLOR_PLANES: usize = 6;
const TYPE_PLANES: usize = 14;
const PLANE_COLORS: usize = 0;
const PLANE_IDENTITY: usize = COLOR_PLANES;
/// Produced mana (docs/issues/engine-produces-planes.md): a plain per-color
/// bitmask on OracleCard, built with the same jsonb_color_to_bits helper and
/// evaluated through the same ColorCmp code path as Colors/ColorIdentity —
/// structurally identical in every way that matters for plane-exactness
/// (card-level, never Null/PrintingDep). Was deliberately left unplaned in
/// #630 phase 1; this closes that gap the same way, no new machinery needed.
const PLANE_PRODUCED_MANA: usize = PLANE_IDENTITY + COLOR_PLANES;
const PLANE_TYPES: usize = PLANE_PRODUCED_MANA + COLOR_PLANES;
/// Devotion is bit-sliced: two saturating bits per color (count clamped to
/// 0..=3), so `devotion:uu` is one plane read and `devotion:uuu` is exactly
/// the saturated bucket. Counts come from the same hybrid-expanded map the
/// evaluator uses; the ~0.5% of cards at 3+ per color are the verification
/// set for deeper queries (see the saturated-superset arm in narrow_rec).
const PLANE_DEVOTION: usize = PLANE_TYPES + TYPE_PLANES;
const DEVOTION_BITS: usize = 2;
/// Two planes per format (docs/issues/engine-legality-divergent-carveout.md),
/// fixed-width at MAX_FORMATS each regardless of how many formats loaded data
/// actually uses -- unused slots are permanently-zero planes, matching the
/// existing `shift: None` "format absent" semantics. Both are computed
/// directly from printings (`build_bit_planes`), not the card-level canonical
/// word, so both are exact -- including divergent cards, which is what makes
/// `compile_plane` able to consume `Legality` at all: `∃p: legal(p)` and
/// `∃p: ¬legal(p)` are genuinely different facts for a divergent card (both
/// can be true at once), so `-format:X` needs its own plane rather than a
/// bit-complement of the first (which would compute `∀p: ¬legal(p)`, wrong).
pub(crate) const PLANE_LEGAL_EXISTS: usize = PLANE_DEVOTION + DEVOTION_BITS * COLOR_PLANES;
pub(crate) const PLANE_LEGAL_ILLEGAL: usize = PLANE_LEGAL_EXISTS + MAX_FORMATS;
/// Same escape hatch, generalized to `banned`/`restricted`
/// (docs/issues/engine-legality-banned-restricted-planes.md, #678): the query
/// space (`expected` against a fixed format list) is exactly as finite and
/// build-time-precomputable for these two values as it was for `LEGAL`, so
/// the identical two-exact-planes construction applies, just with more
/// blocks. `restricted` genuinely diverges per printing for `oldschool`
/// (30th Anniversary Edition / Vintage Championship promo prints, the same
/// divergent-printing pattern `LEGAL` already has) -- `build_bit_planes`
/// reads printings directly here too, so it's exact regardless. `banned`
/// never diverges in the real corpus, but gets the same treatment for
/// uniformity rather than a third, special-cased mechanism.
pub(crate) const PLANE_BANNED_EXISTS: usize = PLANE_LEGAL_ILLEGAL + MAX_FORMATS;
pub(crate) const PLANE_BANNED_ABSENT: usize = PLANE_BANNED_EXISTS + MAX_FORMATS;
pub(crate) const PLANE_RESTRICTED_EXISTS: usize = PLANE_BANNED_ABSENT + MAX_FORMATS;
pub(crate) const PLANE_RESTRICTED_ABSENT: usize = PLANE_RESTRICTED_EXISTS + MAX_FORMATS;
/// One-hot planes for cmc/power/toughness (#655), covering the interior
/// range [0,12] — hundreds-to-thousands of cards per value — plus a shared
/// "13+" high-tail bucket per field (all three have a genuine spread of rare
/// high values, e.g. toughness up to 30) and a shared "<0" low-tail bucket
/// for power/toughness only (`cmc: Option<u8>` is type-guaranteed
/// non-negative, so it never needs one). Buckets are cumulative planes built
/// with the exact same machinery as the interior values — "power<=0" is
/// just another plane, no different from "power==5" — which is what lets a
/// sparse tail (power has 2 cards at -1) get absorbed automatically instead
/// of needing a side table or a live re-query. See
/// docs/issues/engine-numeric-range-planes.md for the design history.
const NUM_INTERIOR_LO: i32 = 0;
const NUM_INTERIOR_HI: i32 = 12;
const NUM_INTERIOR_WIDTH: usize = (NUM_INTERIOR_HI - NUM_INTERIOR_LO + 1) as usize;
pub(crate) const PLANE_CMC: usize = PLANE_RESTRICTED_ABSENT + MAX_FORMATS;
pub(crate) const PLANE_CMC_HI: usize = PLANE_CMC + NUM_INTERIOR_WIDTH;
pub(crate) const PLANE_POWER_LO: usize = PLANE_CMC_HI + 1;
pub(crate) const PLANE_POWER: usize = PLANE_POWER_LO + 1;
pub(crate) const PLANE_POWER_HI: usize = PLANE_POWER + NUM_INTERIOR_WIDTH;
pub(crate) const PLANE_TOUGHNESS_LO: usize = PLANE_POWER_HI + 1;
pub(crate) const PLANE_TOUGHNESS: usize = PLANE_TOUGHNESS_LO + 1;
pub(crate) const PLANE_TOUGHNESS_HI: usize = PLANE_TOUGHNESS + NUM_INTERIOR_WIDTH;
/// One-hot planes for rarity (docs/issues/engine-rarity-planes.md), covering only the
/// 4 most common values -- common=0, uncommon=1, rare=2, mythic=3, matching
/// `magic.rarity_text_to_int`'s numbering directly (no offset needed). special=4/
/// bonus=5 are far too sparse in production (1.17%/0.00% of cards) to be worth a
/// fixed-cost plane each; they stay on the existing `RarityIndex` postings path
/// (`lib.rs`'s `rarity_candidates`). Unlike cmc/power/toughness, there's no interior/
/// overflow-bucket split: the planed range is the entire domain of interest, not a
/// window into an unbounded numeric range, so no `BucketBounds` tracking is needed
/// either -- a plain one-hot block, same shape as `PLANE_TYPES`.
pub(crate) const RARITY_PLANES: usize = 4;
pub(crate) const PLANE_RARITY: usize = PLANE_TOUGHNESS_HI + 1;
pub(crate) const PLANE_COUNT: usize = PLANE_RARITY + RARITY_PLANES;

/// The observed [min,max] of whatever cards landed in one bucket plane,
/// recomputed on every build/reload (never hardcoded from a one-time data
/// snapshot — a future card outside today's observed range must still
/// compile exactly, not silently misclassify). `min > max` is the empty-
/// bucket sentinel: no card has ever been observed there, so the bucket
/// contributes nothing to any comparison, in either direction — see
/// `bucket_verdict`.
#[derive(Archive, Serialize, Deserialize, Clone, Copy)]
pub(crate) struct BucketBounds {
    pub(crate) min: i16,
    pub(crate) max: i16,
}

impl Default for BucketBounds {
    fn default() -> Self {
        BucketBounds { min: i16::MAX, max: i16::MIN }
    }
}

impl BucketBounds {
    fn observe(&mut self, v: i16) {
        self.min = self.min.min(v);
        self.max = self.max.max(v);
    }
}

#[derive(Archive, Serialize, Deserialize, Default)]
pub(crate) struct BitPlanes {
    pub(crate) n_cards: u32,
    /// PLANE_COUNT × words_per_plane, flattened plane-major; bit i of plane p
    /// is words[p * wpp + i/64] >> (i%64) & 1.
    pub(crate) words: Vec<u64>,
    // #655: live bounds for the five numeric-range bucket planes.
    pub(crate) cmc_hi: BucketBounds,
    pub(crate) power_lo: BucketBounds,
    pub(crate) power_hi: BucketBounds,
    pub(crate) toughness_lo: BucketBounds,
    pub(crate) toughness_hi: BucketBounds,
}

pub(crate) fn words_per_plane(n_cards: usize) -> usize {
    n_cards.div_ceil(64)
}

/// Single source of truth for every indexed legality status: `(expected,
/// exists_base, absent_base)`. Every other legality-plane helper
/// (`status_plane_bases`, `legality_plane_shift`, `is_legality_plane`,
/// `build_bit_planes`'s scatter loop) derives from this one table instead of
/// each maintaining its own parallel copy -- adding a 4th indexed status is a
/// one-line change here, nowhere else
/// (docs/issues/engine-legality-banned-restricted-planes.md, #678).
/// `LEGALITY_NOT_LEGAL` has no row: the parser never emits a bare `Legality`
/// leaf with that `expected` (`-format:X` is
/// `Not(Legality{expected: LEGALITY_LEGAL})`, not a literal `NOT_LEGAL` leaf).
const LEGALITY_STATUS_TABLE: [(u64, usize, usize); 3] = [
    (LEGALITY_LEGAL, PLANE_LEGAL_EXISTS, PLANE_LEGAL_ILLEGAL),
    (LEGALITY_BANNED, PLANE_BANNED_EXISTS, PLANE_BANNED_ABSENT),
    (LEGALITY_RESTRICTED, PLANE_RESTRICTED_EXISTS, PLANE_RESTRICTED_ABSENT),
];

/// The (exists_base, absent_base) plane block pair for one `Legality::expected`
/// value, or `None` for a value with no indexed plane. Used by every place
/// that used to hardcode `expected == LEGALITY_LEGAL`.
pub(crate) fn status_plane_bases(expected: u64) -> Option<(usize, usize)> {
    LEGALITY_STATUS_TABLE.iter().find(|&&(e, ..)| e == expected).map(|&(_, exists_base, absent_base)| (exists_base, absent_base))
}

/// A card's effective devotion count for one color lane, saturated to 0..=3
/// — exactly the packed lanes FilterExpr::Devotion evaluates.
fn devotion_count(card: &OracleCard, lane: usize) -> u8 {
    lane_get(card.mana_cost.devotion, lane).min(3)
}

/// One card's cmc/power/toughness value against one field's plane layout:
/// set the interior one-hot plane for values in [0,12], or a bucket plane
/// (tracking its live [min,max]) for values outside it. `lo_bucket` is
/// `None` for cmc (`Option<u8>` is type-guaranteed non-negative, so no card
/// can ever land below the interior).
fn set_numeric_plane(
    set: &mut impl FnMut(usize),
    v: Option<i32>,
    interior_base: usize,
    lo_bucket: Option<(usize, &mut BucketBounds)>,
    hi_bucket: (usize, &mut BucketBounds),
) {
    let Some(v) = v else { return }; // missing value: no bit set anywhere, correctly excluded from any comparison
    if v < NUM_INTERIOR_LO {
        let (plane, bounds) = lo_bucket.expect("value below the interior range with no low bucket configured");
        bounds.observe(v as i16);
        set(plane);
    } else if v <= NUM_INTERIOR_HI {
        set(interior_base + (v - NUM_INTERIOR_LO) as usize);
    } else {
        let (plane, bounds) = hi_bucket;
        bounds.observe(v as i16);
        set(plane);
    }
}

pub(crate) fn build_bit_planes(cards: &[OracleCard], printings: &[Printing], offsets: &[u32]) -> BitPlanes {
    let wpp = words_per_plane(cards.len());
    let mut words = vec![0u64; PLANE_COUNT * wpp];
    let mut cmc_hi = BucketBounds::default();
    let mut power_lo = BucketBounds::default();
    let mut power_hi = BucketBounds::default();
    let mut toughness_lo = BucketBounds::default();
    let mut toughness_hi = BucketBounds::default();
    for (i, card) in cards.iter().enumerate() {
        let mut set = |plane: usize| words[plane * wpp + i / 64] |= 1u64 << (i % 64);
        // Rarity (docs/issues/engine-rarity-planes.md): "any printing at this
        // rarity" existence projection, same aggregation build_rarity_index
        // does over the same range, just OR'd into planes instead of postings
        // for the 4 most common values. Missing rarity (None) contributes no
        // bit, same as build_rarity_index -- a card whose printings are all
        // null-rarity correctly sets nothing here.
        let range = offsets[i] as usize..offsets[i + 1] as usize;
        let mut rarity_mask: u8 = 0;
        for p in &printings[range.clone()] {
            if let Some(r) = p.card_rarity_int {
                rarity_mask |= 1 << r;
            }
        }
        for b in 0..RARITY_PLANES {
            if rarity_mask & (1 << b) != 0 {
                set(PLANE_RARITY + b);
            }
        }
        for b in 0..COLOR_PLANES {
            if card.card_colors & (1 << b) != 0 {
                set(PLANE_COLORS + b);
            }
            if card.card_color_identity & (1 << b) != 0 {
                set(PLANE_IDENTITY + b);
            }
            if card.produced_mana & (1 << b) != 0 {
                set(PLANE_PRODUCED_MANA + b);
            }
        }
        let mut bits = card.card_types;
        while bits != 0 {
            set(PLANE_TYPES + bits.trailing_zeros() as usize);
            bits &= bits - 1;
        }
        for b in 0..COLOR_PLANES {
            let count = devotion_count(card, b);
            if count & 1 != 0 {
                set(PLANE_DEVOTION + DEVOTION_BITS * b);
            }
            if count & 2 != 0 {
                set(PLANE_DEVOTION + DEVOTION_BITS * b + 1);
            }
            // Data-integrity tripwire (verified corpus-wide 2026-07-08, zero
            // violations): cost symbols feed color identity by rule, so
            // colored devotion without the identity bit means a loading bug.
            // C is exempt — {C} pips never join identity.
            debug_assert!(
                count == 0 || b == 5 || card.card_color_identity & (1 << b) != 0,
                "devotion without identity: card {i} color lane {b}"
            );
        }
        // Legality (docs/issues/engine-legality-divergent-carveout.md,
        // generalized to banned/restricted by #678 -- see
        // docs/issues/engine-legality-banned-restricted-planes.md): two
        // existence projections per (format, status), computed directly from
        // this card's own printings (the `range` rarity already sliced
        // above) -- exists = some printing has this status, absent = some
        // printing doesn't. Both exact for every card, including ones whose
        // printings disagree, since neither depends on a single canonical
        // card-level word or a divergence flag. MAX_FORMATS (32) fits a u64
        // mask with room to spare.
        // Indexed by position in LEGALITY_STATUS_TABLE -- the same single
        // source of truth status_plane_bases/legality_plane_shift read, so
        // this loop can never drift out of sync with which plane block a
        // status writes into.
        let mut exists_masks = [0u64; LEGALITY_STATUS_TABLE.len()];
        let mut absent_masks = [0u64; LEGALITY_STATUS_TABLE.len()];
        for p in &printings[range] {
            for f in 0..MAX_FORMATS {
                let shift = (f * 2) as u32;
                let status = (p.card_legalities >> shift) & 0b11;
                for (i, &(want, ..)) in LEGALITY_STATUS_TABLE.iter().enumerate() {
                    if status == want {
                        exists_masks[i] |= 1 << f;
                    } else {
                        absent_masks[i] |= 1 << f;
                    }
                }
            }
        }
        for (i, &(_, exists_base, absent_base)) in LEGALITY_STATUS_TABLE.iter().enumerate() {
            for f in 0..MAX_FORMATS {
                if exists_masks[i] & (1 << f) != 0 {
                    set(exists_base + f);
                }
                if absent_masks[i] & (1 << f) != 0 {
                    set(absent_base + f);
                }
            }
        }
        // #655: cmc is Option<u8>, type-guaranteed non-negative, so it has no
        // low bucket. Power/toughness are Option<i8> and do (Char-Rumbler and
        // similar).
        set_numeric_plane(&mut set, card.cmc.map(i32::from), PLANE_CMC, None, (PLANE_CMC_HI, &mut cmc_hi));
        set_numeric_plane(
            &mut set,
            card.creature_power.map(i32::from),
            PLANE_POWER,
            Some((PLANE_POWER_LO, &mut power_lo)),
            (PLANE_POWER_HI, &mut power_hi),
        );
        set_numeric_plane(
            &mut set,
            card.creature_toughness.map(i32::from),
            PLANE_TOUGHNESS,
            Some((PLANE_TOUGHNESS_LO, &mut toughness_lo)),
            (PLANE_TOUGHNESS_HI, &mut toughness_hi),
        );
    }
    BitPlanes { n_cards: cards.len() as u32, words, cmc_hi, power_lo, power_hi, toughness_lo, toughness_hi }
}

/// Ascending card ids with divergent legality (~556 of 31,508 in production —
/// well under the postings/plane byte crossover the bigram index established
/// (PR #639): postings cost 2 bytes/entry, a plane costs words_per_plane*8
/// bytes flat regardless of density, so a fixed, tiny, shared set like this
/// one is cheaper as a list than as a 33rd plane. `u16` on purpose, same
/// assumption the name-bigram index's sparse tier makes: card ids fit
/// (production is ~31.5k, comfortably under u16::MAX). No longer scattered
/// into any candidate mask -- `legality_candidate_bits` (lib.rs) narrows via
/// the exact `_EXISTS`/`_ABSENT` planes directly (docs/issues/engine-
/// legality-divergent-carveout.md's "free upgrade"), so this list is now only
/// consulted by `filter.rs`'s per-printing `Legality` `tri()` residual check.
pub(crate) fn build_divergent_ids(cards: &[OracleCard]) -> Vec<u16> {
    debug_assert!(cards.len() <= u16::MAX as usize + 1, "card count exceeds u16 range for divergent postings");
    cards
        .iter()
        .enumerate()
        .filter_map(|(i, card)| card.legality_divergent.then_some(i as u16))
        .collect()
}

// ─── Plane expressions ────────────────────────────────────────────────────────

/// A filter subtree compiled to mask algebra over planes. Evaluation is
/// word-at-a-time (eval_word), one pass over the words with no per-node
/// temporaries.
pub(crate) enum PlaneExpr {
    Plane(u16),
    /// An externally-precomputed card bitmap, cloned in whole at compile
    /// time (docs/issues/engine-oracle-word-index.md's dense word dictionary — see
    /// compile_plane's TextContains arm). Not part of BitPlanes' fixed
    /// layout: which words promote to a bitmap is data-dependent, unlike the
    /// compile-time-known dimensions the other variants index into. A clone
    /// (a few KB) is paid once per query, never per row.
    Bits(Vec<u64>),
    And(Vec<PlaneExpr>),
    Or(Vec<PlaneExpr>),
    Not(Box<PlaneExpr>),
    Const(bool),
}

/// And over children, collapsing the empty (vacuously true) and singleton cases.
fn and_of(mut children: Vec<PlaneExpr>) -> PlaneExpr {
    match children.len() {
        0 => PlaneExpr::Const(true),
        1 => children.pop().unwrap(),
        _ => PlaneExpr::And(children),
    }
}

/// Or over children, collapsing the empty (vacuously false) and singleton cases.
fn or_of(mut children: Vec<PlaneExpr>) -> PlaneExpr {
    match children.len() {
        0 => PlaneExpr::Const(false),
        1 => children.pop().unwrap(),
        _ => PlaneExpr::Or(children),
    }
}

/// Split a plane range [base, base+width) by a value mask: planes for the mask's
/// set bits, and planes for its clear bits.
fn in_out_planes(base: usize, width: usize, mask: u16) -> (Vec<PlaneExpr>, Vec<PlaneExpr>) {
    let mut inp = Vec::new();
    let mut outp = Vec::new();
    for b in 0..width {
        let plane = PlaneExpr::Plane((base + b) as u16);
        if mask & (1 << b) != 0 { inp.push(plane) } else { outp.push(plane) }
    }
    (inp, outp)
}

/// bits == mask over the planes of one field: every in-mask plane set, every
/// out-of-mask plane clear.
fn eq_expr(base: usize, width: usize, mask: u16) -> PlaneExpr {
    let (inp, outp) = in_out_planes(base, width, mask);
    and_of(inp.into_iter().chain(outp.into_iter().map(|p| PlaneExpr::Not(Box::new(p)))).collect())
}

/// bits & !mask == 0 (nothing outside the mask): every out-of-mask plane clear.
fn le_expr(base: usize, width: usize, mask: u16) -> PlaneExpr {
    let (_, outp) = in_out_planes(base, width, mask);
    and_of(outp.into_iter().map(|p| PlaneExpr::Not(Box::new(p))).collect())
}

/// Compile one field's comparison to plane algebra. `ge_any` selects the Ge
/// shape: ColorCmp's Ge is all-of (bits & mask == mask), TypeCmp's is any-of
/// (bits & mask != 0) — see their tri() arms.
fn cmp_expr(base: usize, width: usize, mask: u16, op: CmpOp, ge_any: bool) -> PlaneExpr {
    let ge = || {
        let (inp, _) = in_out_planes(base, width, mask);
        if ge_any { or_of(inp) } else { and_of(inp) }
    };
    match op {
        // All-of Ge (ColorCmp) with mask 0 means the query was literally
        // "c"/"colorless": and_of([]) is vacuously true, but the intended
        // semantics are exact equality (bits == 0), matching the ColorCmp::Ge
        // special case in filter.rs's eval_card. Gt's own use of ge() below is
        // deliberately untouched -- Gt's "no colors" case correctly depends on
        // the vacuous-true shape to reduce to "not equal".
        CmpOp::Ge if !ge_any && mask == 0 => eq_expr(base, width, mask),
        CmpOp::Ge => ge(),
        CmpOp::Eq => eq_expr(base, width, mask),
        CmpOp::Le => le_expr(base, width, mask),
        CmpOp::Ne => PlaneExpr::Not(Box::new(eq_expr(base, width, mask))),
        CmpOp::Lt => and_of(vec![le_expr(base, width, mask), PlaneExpr::Not(Box::new(eq_expr(base, width, mask)))]),
        CmpOp::Gt => and_of(vec![ge(), PlaneExpr::Not(Box::new(eq_expr(base, width, mask)))]),
    }
}

/// One color's devotion-count comparison over its two saturating bit-slices.
/// Exactness boundaries: `>= k` is exact through k = 3 (the saturated value 3
/// MEANS >= 3), `== k` and `<= k` through k = 2 (value 3 is a bucket, not a
/// count). None past the boundary.
fn dev_ge(color: usize, k: u8) -> Option<PlaneExpr> {
    let b0 = || PlaneExpr::Plane((PLANE_DEVOTION + DEVOTION_BITS * color) as u16);
    let b1 = || PlaneExpr::Plane((PLANE_DEVOTION + DEVOTION_BITS * color + 1) as u16);
    match k {
        0 => Some(PlaneExpr::Const(true)),
        1 => Some(or_of(vec![b0(), b1()])),
        2 => Some(b1()),
        3 => Some(and_of(vec![b0(), b1()])),
        _ => None,
    }
}

fn dev_eq(color: usize, k: u8) -> Option<PlaneExpr> {
    let b0 = || PlaneExpr::Plane((PLANE_DEVOTION + DEVOTION_BITS * color) as u16);
    let b1 = || PlaneExpr::Plane((PLANE_DEVOTION + DEVOTION_BITS * color + 1) as u16);
    match k {
        0 => Some(PlaneExpr::Not(Box::new(or_of(vec![b0(), b1()])))),
        1 => Some(and_of(vec![b0(), PlaneExpr::Not(Box::new(b1()))])),
        2 => Some(and_of(vec![b1(), PlaneExpr::Not(Box::new(b0()))])),
        _ => None,
    }
}

fn dev_le(color: usize, k: u8) -> Option<PlaneExpr> {
    // count <= k  ⟺  not (count >= k + 1); k <= 2 keeps >= k+1 exact.
    if k > 2 {
        return None;
    }
    dev_ge(color, k + 1).map(|ge| PlaneExpr::Not(Box::new(ge)))
}

/// Compile a Devotion node exactly, mirroring FilterExpr::Devotion's tri():
/// Ge constrains only the queried colors (the nonzero lanes); Le/Eq
/// additionally pin every unqueried color to zero (SQL devotion-column
/// containment semantics). None whenever any needed comparison crosses the
/// saturation boundary.
fn compile_devotion(op: CmpOp, pips: u64) -> Option<PlaneExpr> {
    let query: Vec<(usize, u8)> = (0..COLOR_PLANES)
        .filter_map(|c| {
            let k = lane_get(pips, c);
            (k > 0).then_some((c, k))
        })
        .collect();
    let ge = || query.iter().map(|&(c, k)| dev_ge(c, k)).collect::<Option<Vec<_>>>().map(and_of);
    let all_colors = |f: &dyn Fn(usize, u8) -> Option<PlaneExpr>| {
        (0..COLOR_PLANES)
            .map(|c| f(c, query.iter().find(|&&(qc, _)| qc == c).map_or(0, |&(_, k)| k)))
            .collect::<Option<Vec<_>>>()
            .map(and_of)
    };
    let eq = || all_colors(&dev_eq);
    match op {
        CmpOp::Ge => ge(),
        CmpOp::Le => all_colors(&dev_le),
        CmpOp::Eq => eq(),
        CmpOp::Ne => eq().map(|e| PlaneExpr::Not(Box::new(e))),
        CmpOp::Gt => Some(and_of(vec![ge()?, PlaneExpr::Not(Box::new(eq()?))])),
        CmpOp::Lt => Some(and_of(vec![all_colors(&dev_le)?, PlaneExpr::Not(Box::new(eq()?))])),
    }
}

/// Saturated superset for devotion comparisons the exact compiler declines
/// (Ge/Gt past the boundary): clamp each queried count to 3. Every real match
/// has count >= k >= 3 per queried color, so it lands in the saturated bucket
/// — a loose candidate set for the driver to verify (~0.5% of cards/color).
pub(crate) fn compile_devotion_superset(pips: u64) -> Option<PlaneExpr> {
    (0..COLOR_PLANES)
        .filter_map(|c| {
            let k = lane_get(pips, c);
            (k > 0).then(|| dev_ge(c, k.min(3)))
        })
        .collect::<Option<Vec<_>>>()
        .map(and_of)
}

// ─── Numeric-range planes (#655) ───────────────────────────────────────────────

/// One field's plane layout: interior one-hot base (13 planes, values
/// [0,12]), an optional low bucket (power/toughness only), and a high bucket
/// (all three fields).
struct NumericLayout {
    interior_base: usize,
    lo_bucket: Option<(usize, Bucket)>,
    hi_bucket: (usize, Bucket),
}

/// A bucket's live-observed range. `min > max` means empty (no card has ever
/// landed there) — see `bucket_verdict`.
#[derive(Clone, Copy)]
struct Bucket {
    min: i32,
    max: i32,
}

fn numeric_layout(field: NumField, bounds: &rkyv::Archived<BitPlanes>) -> Option<NumericLayout> {
    let bucket = |b: &rkyv::Archived<BucketBounds>| Bucket { min: i16::from(b.min) as i32, max: i16::from(b.max) as i32 };
    match field {
        NumField::Cmc => Some(NumericLayout {
            interior_base: PLANE_CMC,
            lo_bucket: None,
            hi_bucket: (PLANE_CMC_HI, bucket(&bounds.cmc_hi)),
        }),
        NumField::Power => Some(NumericLayout {
            interior_base: PLANE_POWER,
            lo_bucket: Some((PLANE_POWER_LO, bucket(&bounds.power_lo))),
            hi_bucket: (PLANE_POWER_HI, bucket(&bounds.power_hi)),
        }),
        NumField::Toughness => Some(NumericLayout {
            interior_base: PLANE_TOUGHNESS,
            lo_bucket: Some((PLANE_TOUGHNESS_LO, bucket(&bounds.toughness_lo))),
            hi_bucket: (PLANE_TOUGHNESS_HI, bucket(&bounds.toughness_hi)),
        }),
        _ => None,
    }
}

/// Whether `v <op> threshold` holds — the exact same float comparison
/// `numeric_candidates` (lib.rs) uses, so the plane-compiled answer and the
/// index-scan fallback can never disagree on a fractional-threshold edge
/// case (e.g. `cmc>6.5`).
fn matches_op(op: CmpOp, v: f64, threshold: f64) -> bool {
    match op {
        CmpOp::Eq => v == threshold,
        CmpOp::Ne => v != threshold,
        CmpOp::Lt => v < threshold,
        CmpOp::Le => v <= threshold,
        CmpOp::Gt => v > threshold,
        CmpOp::Ge => v >= threshold,
    }
}

enum BucketVerdict {
    /// Every possible value in the bucket satisfies the comparison.
    FullyIncluded,
    /// No possible value in the bucket satisfies it (including: bucket empty).
    FullyExcluded,
    /// Depends on which specific value a member card has — the bucket plane
    /// can't distinguish, so the caller must decline.
    Ambiguous,
}

/// Decide a bucket's fate for `field <op> threshold`, using only the
/// bucket's observed [min,max] — never the actual per-card values, which the
/// bucket plane doesn't retain. Sound because Lt/Le/Gt/Ge are monotonic in v
/// (checking the two endpoints suffices) and Eq only ever resolves when the
/// bucket is a single observed value equal to the threshold; anything less
/// certain declines rather than guesses. Ne is never called here — its
/// caller declines unconditionally, matching `numeric_candidates`'s own
/// choice ("Ne is not selective").
fn bucket_verdict(op: CmpOp, threshold: f64, bucket: Bucket) -> BucketVerdict {
    if bucket.min > bucket.max {
        return BucketVerdict::FullyExcluded; // empty: no observed member at all
    }
    let (min, max) = (bucket.min as f64, bucket.max as f64);
    match op {
        CmpOp::Ge => {
            if min >= threshold {
                BucketVerdict::FullyIncluded
            } else if max < threshold {
                BucketVerdict::FullyExcluded
            } else {
                BucketVerdict::Ambiguous
            }
        }
        CmpOp::Gt => {
            if min > threshold {
                BucketVerdict::FullyIncluded
            } else if max <= threshold {
                BucketVerdict::FullyExcluded
            } else {
                BucketVerdict::Ambiguous
            }
        }
        CmpOp::Le => {
            if max <= threshold {
                BucketVerdict::FullyIncluded
            } else if min > threshold {
                BucketVerdict::FullyExcluded
            } else {
                BucketVerdict::Ambiguous
            }
        }
        CmpOp::Lt => {
            if max < threshold {
                BucketVerdict::FullyIncluded
            } else if min >= threshold {
                BucketVerdict::FullyExcluded
            } else {
                BucketVerdict::Ambiguous
            }
        }
        CmpOp::Eq => {
            if threshold < min || threshold > max {
                BucketVerdict::FullyExcluded
            } else if bucket.min == bucket.max && min == threshold {
                BucketVerdict::FullyIncluded
            } else {
                BucketVerdict::Ambiguous
            }
        }
        CmpOp::Ne => unreachable!("Ne declines before reaching bucket_verdict"),
    }
}

/// Compile `field <op> threshold` for cmc/power/toughness. Interior values
/// are never ambiguous (a one-hot plane is a single integer point — either
/// fully in or fully out); only the bucket planes can force a decline.
/// Missing values (non-creature power/toughness, an unset cmc) set no bit
/// anywhere in the field's planes, so they're correctly excluded from any
/// `Or` here — the Null-collapses-to-false semantics `filter.rs`'s
/// `NumericCmp::tri()` already implements, reproduced by omission rather
/// than by checking for it explicitly.
fn compile_numeric_cmp(field: NumField, op: CmpOp, threshold: f64, bounds: &rkyv::Archived<BitPlanes>) -> Option<PlaneExpr> {
    if matches!(op, CmpOp::Ne) {
        return None; // matches numeric_candidates: Ne is not selective, decline
    }
    let layout = numeric_layout(field, bounds)?;
    let mut included: Vec<PlaneExpr> = Vec::new();
    for v in NUM_INTERIOR_LO..=NUM_INTERIOR_HI {
        if matches_op(op, v as f64, threshold) {
            included.push(PlaneExpr::Plane((layout.interior_base + (v - NUM_INTERIOR_LO) as usize) as u16));
        }
    }
    if let Some((plane, bucket)) = layout.lo_bucket {
        match bucket_verdict(op, threshold, bucket) {
            BucketVerdict::FullyIncluded => included.push(PlaneExpr::Plane(plane as u16)),
            BucketVerdict::FullyExcluded => {}
            BucketVerdict::Ambiguous => return None,
        }
    }
    let (hi_plane, hi_bucket) = layout.hi_bucket;
    match bucket_verdict(op, threshold, hi_bucket) {
        BucketVerdict::FullyIncluded => included.push(PlaneExpr::Plane(hi_plane as u16)),
        BucketVerdict::FullyExcluded => {}
        BucketVerdict::Ambiguous => return None,
    }
    Some(or_of(included))
}

/// True if `filter` (recursively through And/Or/Not) contains a NumericCmp
/// on a plane-backed field (cmc/power/toughness). These are NOT safe to
/// blindly complement via `PlaneExpr::Not`, unlike ColorCmp/TypeCmp/Devotion:
/// the field can be absent (`Tri::Null` — non-creature cards for power/
/// toughness, an unset cmc), and Null propagates through Not as Null
/// (`filter.rs`'s `FilterExpr::Not` arm: Kleene logic, never flipped to
/// True) — so blindly complementing the plane would wrongly match
/// missing-value cards. Other NumericCmp fields (rarity, price, ...)
/// already decline via `compile_plane`'s catch-all regardless of Not, so
/// they need no special handling here.
fn contains_unnegatable_numeric(filter: &FilterExpr) -> bool {
    let is_planeable = |e: &NumExpr| matches!(e, NumExpr::Field(NumField::Cmc | NumField::Power | NumField::Toughness));
    match filter {
        FilterExpr::NumericCmp { lhs, rhs, .. } => is_planeable(lhs) || is_planeable(rhs),
        FilterExpr::And(children) | FilterExpr::Or(children) => children.iter().any(contains_unnegatable_numeric),
        FilterExpr::Not(inner) => contains_unnegatable_numeric(inner),
        _ => false,
    }
}

/// Compile a filter subtree to a plane expression, or None if any node in it
/// is not plane-expressible. Only two-valued card-level nodes may compile:
/// complement (Not) is only sound when the node can never be Null or
/// PrintingDep, which holds for ColorCmp and TypeCmp by their tri() arms —
/// and, for NumericCmp on cmc/power/toughness, only when the subtree being
/// negated doesn't contain one at all (see `contains_unnegatable_numeric`).
/// Sort key for trying And/Or children in compile_plane: oracle-word leaves
/// (the only shape that pays a real cost — a dictionary scan — just to find
/// out whether it declines) sort last, so a cheap-to-reject sibling (e.g. a
/// NumericCmp field compile_numeric_cmp doesn't handle) fails the whole
/// `.collect::<Option<Vec<_>>>()` before the scan ever runs. Both And and Or
/// are order-independent once compiled, so reordering only for this internal
/// short-circuit check never changes the result.
fn plane_precheck_rank(f: &FilterExpr) -> u8 {
    match f {
        FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word } if oracle_word_eligible(word) => 1,
        _ => 0,
    }
}

/// Try children cheapest-to-reject first (see plane_precheck_rank), short-
/// circuiting on the first None without disturbing the caller's requested
/// order — `and_of`/`or_of` don't care about member order, so this is free.
fn compile_plane_children(children: &[FilterExpr], bounds: &rkyv::Archived<BitPlanes>, words: &rkyv::Archived<OracleWordIndex>) -> Option<Vec<PlaneExpr>> {
    let mut order: Vec<&FilterExpr> = children.iter().collect();
    order.sort_by_key(|c| plane_precheck_rank(c));
    order.into_iter().map(|c| compile_plane(c, bounds, words)).collect()
}

/// Whether plane index `p` falls in any legality existence-projection block
/// (`LEGALITY_STATUS_TABLE`), regardless of which status/polarity it is.
fn is_legality_plane(p: usize) -> bool {
    legality_plane_shift(p).is_some()
}

/// Collect distinct legality plane indices referenced by legality-plane
/// leaves within `expr`, appending into `out` (deduplicated). Each specific
/// plane index already identifies one (format, status, polarity) existence
/// fact, so deduping on the raw index is exactly the right granularity --
/// simpler than (and a superset of) the old `(format, polarity)` tuple key,
/// which only had to consider `LEGAL`. Every distinct fact referenced inside
/// an `And` is shared-witness-prone: `format:A AND -format:A` (same format,
/// two polarities), `banned:A AND restricted:A` (same format, two statuses),
/// and `format:A AND format:B` (two formats) are all the identical problem --
/// a divergent card can satisfy two existence facts via two *different*
/// witnessing printings, even though no single printing can satisfy both at
/// once (see `and_of_checked_for_shared_witness`'s doc). A literal duplicate
/// leaf (`format:A AND format:A`) reads the same plane index and collapses to
/// one entry, fine to compose -- the same underlying fact checked twice, not
/// two facts needing a shared witness.
fn collect_legality_formats(expr: &PlaneExpr, out: &mut Vec<u16>) {
    match expr {
        PlaneExpr::Plane(p) => {
            if is_legality_plane(*p as usize) && !out.contains(p) {
                out.push(*p);
            }
        }
        PlaneExpr::Bits(_) | PlaneExpr::Const(_) => {}
        PlaneExpr::And(cs) | PlaneExpr::Or(cs) => {
            for c in cs {
                collect_legality_formats(c, out);
            }
        }
        PlaneExpr::Not(inner) => collect_legality_formats(inner, out),
    }
}

/// Whether any leaf of a compiled plane expression reads a legality plane
/// (any status/polarity in `LEGALITY_STATUS_TABLE`). These are existence projections over
/// per-printing-varying data ("*some* printing has this status") -- unlike
/// every other plane (colors, types, numeric buckets, devotion, border, all
/// card-invariant), a card-level True here does not imply every printing of
/// the card individually satisfies the query. Used to gate the #634 Step 1
/// `all_match_known` fast path to `unique=card`, where existence is exactly
/// the semantics needed (docs/issues/engine-legality-divergent-carveout.md;
/// Step 2's popcount path is already `Mode::Card`-only for unrelated reasons,
/// see `run_query`).
pub(crate) fn plane_expr_is_existential(expr: &PlaneExpr) -> bool {
    match expr {
        PlaneExpr::Plane(p) => is_legality_plane(*p as usize),
        PlaneExpr::Bits(_) | PlaneExpr::Const(_) => false,
        PlaneExpr::And(cs) | PlaneExpr::Or(cs) => cs.iter().any(plane_expr_is_existential),
        PlaneExpr::Not(inner) => plane_expr_is_existential(inner),
    }
}

/// `And` two-or-more legality-plane leaves referencing distinct existence
/// facts -- different formats, the same format under both polarities
/// (`format:A AND -format:A`), or (#678) the same format under two different
/// *statuses* (`banned:A AND restricted:A`) -- can't be answered by ANDing
/// independent existence projections: `∃p: legal_A(p) ∧ legal_B(p)` requires
/// one printing to satisfy both at once, which isn't the same as `(∃p:
/// legal_A(p)) ∧ (∃p: legal_B(p))` (a false positive the moment different
/// printings would be the witness for each) -- the same argument applies
/// verbatim whichever two distinct facts are involved, since `collect_legality_
/// formats` dedupes by raw plane index, not by format alone. `Or` never has
/// this problem (`∃` distributes over `∨`), so this is only
/// called where an `And` is about to be assembled -- both the direct case
/// and the De-Morgan'd `Not(Or(...))` case in `compile_plane_neg`. Declining
/// (falling back to `narrow_rec`'s existing, correct `Legality` narrowing) is
/// deliberately simpler than building a shared-witness-safe joint check for a
/// shape nobody realistically writes --
/// docs/issues/engine-printing-varying-plane-repair-pattern.md has the joint
/// per-printing evaluation this would need if it ever mattered enough to build.
fn and_of_checked_for_shared_witness(children: Vec<PlaneExpr>) -> Option<PlaneExpr> {
    let mut formats = Vec::new();
    for c in &children {
        collect_legality_formats(c, &mut formats);
    }
    if formats.len() > 1 {
        return None;
    }
    Some(and_of(children))
}

pub(crate) fn compile_plane(filter: &FilterExpr, bounds: &rkyv::Archived<BitPlanes>, words: &rkyv::Archived<OracleWordIndex>) -> Option<PlaneExpr> {
    match filter {
        FilterExpr::True => Some(PlaneExpr::Const(true)),
        FilterExpr::And(children) => and_of_checked_for_shared_witness(compile_plane_children(children, bounds, words)?),
        FilterExpr::Or(children) => compile_plane_children(children, bounds, words).map(or_of),
        // De Morgan pushdown so a Not that reaches a Legality leaf lands on
        // `illegal_exists` instead of bit-complementing `legal_exists` (wrong
        // for divergent cards -- see PLANE_LEGAL_EXISTS's doc). Handles the
        // contains_unnegatable_numeric guard itself, per-leaf, via its own
        // catch-all arm -- see compile_plane_neg's doc.
        FilterExpr::Not(inner) => compile_plane_neg(inner, bounds, words),
        // Bonus consumption (docs/issues/engine-oracle-word-index.md): only
        // when the needle matches exactly one dictionary word total (dense or
        // sparse) and that word is dense — the same "single dense hit, no
        // sparse hits" case narrow_rec's general dispatch handles, just
        // reached here first so it composes with other planes via And/Or
        // instead of forcing the whole subtree residual. Any other shape
        // (multiple hits, or a sparse hit at all) declines: unioning would be
        // needed, which this bonus arm intentionally doesn't attempt — the
        // general path in narrow_rec already covers it correctly.
        FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word }
            if oracle_word_eligible(word) && u32::from(words.n_cards) == u32::from(bounds.n_cards) =>
        {
            let scan = scan_oracle_words(words, word);
            match (scan.dense.as_slice(), scan.sparse.as_slice()) {
                ([d], []) => {
                    let wpp = words_per_plane(u32::from(words.n_cards) as usize);
                    let start = *d as usize * wpp;
                    Some(PlaneExpr::Bits(words.dense_bits[start..start + wpp].iter().map(|w| u64::from(*w)).collect()))
                }
                _ => None,
            }
        }
        FilterExpr::ColorCmp { field, op, mask } => {
            let base = match field {
                ColorField::Colors => PLANE_COLORS,
                ColorField::ColorIdentity => PLANE_IDENTITY,
                ColorField::ProducedMana => PLANE_PRODUCED_MANA,
            };
            // color_to_bit only sets bits 0..6; anything else would make the
            // plane complement unsound, so refuse rather than assume.
            if u16::from(*mask) & !((1 << COLOR_PLANES) - 1) != 0 {
                return None;
            }
            Some(cmp_expr(base, COLOR_PLANES, u16::from(*mask), *op, false))
        }
        FilterExpr::TypeCmp { mask, op } => {
            if mask & !((1 << TYPE_PLANES) - 1) != 0 {
                return None;
            }
            Some(cmp_expr(PLANE_TYPES, TYPE_PLANES, *mask, *op, true))
        }
        // Devotion is card-level and two-valued (tri_bool always), so its
        // bit-sliced planes compile exactly within the saturation boundary.
        FilterExpr::Devotion { op, pips } => compile_devotion(*op, *pips),
        FilterExpr::NumericCmp { lhs, op, rhs } => match (lhs, rhs) {
            (NumExpr::Field(f), NumExpr::Const(v)) => compile_numeric_cmp(*f, *op, *v, bounds),
            (NumExpr::Const(v), NumExpr::Field(f)) => compile_numeric_cmp(*f, flip_op(*op), *v, bounds),
            _ => None,
        },
        // f:x / format:x / banned:x / restricted:x (docs/issues/
        // engine-legality-divergent-carveout.md, generalized by #678 -- see
        // docs/issues/engine-legality-banned-restricted-planes.md): exact for
        // every card via the status's `_EXISTS` plane -- no divergent-card
        // caveat, same as `LEGAL`. Only a format absent from all loaded data
        // (shift: None) stays unindexed; `Not` is handled by
        // compile_plane_neg, not here.
        FilterExpr::Legality { shift: Some(shift), expected } => {
            status_plane_bases(*expected).map(|(exists_base, _)| PlaneExpr::Plane((exists_base + *shift as usize / 2) as u16))
        }
        _ => None,
    }
}

/// Compile `Not(filter)` directly, pushing negation down to `Legality` leaves
/// (which need `PLANE_LEGAL_ILLEGAL`, not a bit-complement of
/// `PLANE_LEGAL_EXISTS`) via De Morgan, while leaves that are safe to
/// bit-complement (colors/types/devotion/non-null-valued numerics) fall
/// through to the cheaper "compile positive, wrap in `PlaneExpr::Not`" path
/// unchanged. Mutually recursive with `compile_plane`.
fn compile_plane_neg(filter: &FilterExpr, bounds: &rkyv::Archived<BitPlanes>, words: &rkyv::Archived<OracleWordIndex>) -> Option<PlaneExpr> {
    match filter {
        FilterExpr::Legality { shift: Some(shift), expected } => {
            status_plane_bases(*expected).map(|(_, absent_base)| PlaneExpr::Plane((absent_base + *shift as usize / 2) as u16))
        }
        // Not(And(cs)) = Or(Not(c) for c in cs) -- existence distributes over
        // Or, so no shared-witness check is needed here regardless of how
        // many legality leaves end up among the children.
        FilterExpr::And(children) => compile_plane_neg_children(children, bounds, words).map(or_of),
        // Not(Or(cs)) = And(Not(c) for c in cs) -- THIS does have the
        // shared-witness exposure (see and_of_checked_for_shared_witness).
        FilterExpr::Or(children) => and_of_checked_for_shared_witness(compile_plane_neg_children(children, bounds, words)?),
        FilterExpr::Not(inner) => compile_plane(inner, bounds, words), // double negation
        FilterExpr::True => Some(PlaneExpr::Const(false)),
        // Everything else: only Legality has a divergent-card correctness
        // gap, so every other plane-eligible leaf is safe to compile
        // positive and wrap -- contains_unnegatable_numeric still guards
        // cmc/power/toughness's Null-vs-missing-value issue exactly as
        // before, just checked per-leaf here instead of once upfront (this
        // function's own And/Or recursion propagates a declined leaf's None
        // through .collect::<Option<_>>() the same way the old upfront check
        // did for the whole subtree).
        other => {
            if contains_unnegatable_numeric(other) {
                return None;
            }
            compile_plane(other, bounds, words).map(|p| PlaneExpr::Not(Box::new(p)))
        }
    }
}

fn compile_plane_neg_children(children: &[FilterExpr], bounds: &rkyv::Archived<BitPlanes>, words: &rkyv::Archived<OracleWordIndex>) -> Option<Vec<PlaneExpr>> {
    children.iter().map(|c| compile_plane_neg(c, bounds, words)).collect()
}

/// Consume the plane-expressible part of a bound filter. Returns the compiled
/// plane expression (None when nothing compiled) and the residual filter the
/// driver must still evaluate (FilterExpr::True when everything compiled).
///
/// Composition rules: a fully compilable tree is consumed whole. A top-level
/// And partitions — compilable children move into the plane expression, the
/// rest stay as the residual (the bulk analogue of card_pass's per-card
/// residual extraction). An Or is all-or-nothing: mask ∨ residual is not a
/// narrowing mask, so a partially compilable Or stays entirely residual.
/// A bare True is left alone — the full-range scan beats materializing an
/// all-ones bitmap into a candidate list.
///
/// `unique_is_card` gates legality specifically: its planes are existence
/// projections ("*some* printing matches"), so consuming a leaf touching them
/// to a bare `True` residual (docs/issues/engine-legality-divergent-
/// carveout.md) is only sound for `unique=card`, where existence is the
/// semantics wanted. For `unique=printing`/`artwork`, doing so would discard
/// the only thing that can re-derive *which* printing actually matches --
/// there's no information left in a `True` residual to recover it from, so
/// this must decline the fold at the source rather than try to patch it up
/// after (`plane_expr_is_existential` in `run_query` is kept anyway, as a
/// defense-in-depth check for any other caller of `split_planes`/`run_query`
/// that doesn't route through here). Every other plane (card-invariant
/// fields) has no such exposure and ignores this flag entirely.
pub(crate) fn split_planes(
    filter: FilterExpr,
    bounds: &rkyv::Archived<BitPlanes>,
    words: &rkyv::Archived<OracleWordIndex>,
    unique_is_card: bool,
) -> (Option<PlaneExpr>, FilterExpr) {
    if matches!(filter, FilterExpr::True) {
        return (None, filter);
    }
    if let Some(pe) = compile_plane(&filter, bounds, words)
        && (unique_is_card || !plane_expr_is_existential(&pe))
    {
        return (Some(pe), FilterExpr::True);
    }
    match filter {
        FilterExpr::And(children) => {
            let mut planes: Vec<PlaneExpr> = Vec::new();
            let mut rest: Vec<FilterExpr> = Vec::new();
            // Legality-touching children are held back until every child has
            // been tried, so the shared-witness check (see
            // and_of_checked_for_shared_witness) sees the full picture --
            // this loop calls compile_plane per child directly (not the
            // whole-And path above, which already failed), so it needs its
            // own version of that same guard. Non-legality children
            // (colors/types) have no shared-witness exposure and are always
            // safe to extract immediately, whether or not the rest resolves
            // -- unlike the first (repair-based) design for this issue,
            // there's no tax to avoid paying here anymore.
            let mut legality_children: Vec<(FilterExpr, PlaneExpr)> = Vec::new();
            for c in children {
                match compile_plane(&c, bounds, words) {
                    Some(pe) => {
                        let mut fmts = Vec::new();
                        collect_legality_formats(&pe, &mut fmts);
                        if fmts.is_empty() {
                            planes.push(pe);
                        } else {
                            legality_children.push((c, pe));
                        }
                    }
                    None => rest.push(c),
                }
            }
            let mut all_formats = Vec::new();
            for (_, pe) in &legality_children {
                collect_legality_formats(pe, &mut all_formats);
            }
            if unique_is_card && all_formats.len() <= 1 {
                for (_, pe) in legality_children {
                    planes.push(pe);
                }
            } else {
                // 2+ distinct legality existence facts (different formats,
                // different statuses of the same format, or the same format
                // under both polarities -- collect_legality_formats dedupes
                // by raw plane index, so all three shapes count the same
                // way) can't be ANDed together safely regardless of mode
                // (shared-witness); a single one is safe for the plane's own
                // exactness but still deferred for unique=printing/artwork,
                // same reasoning as the top-level shortcut above. Either way
                // it falls back to narrow_rec's existing (correct, still
                // exact as of this issue) Legality narrowing arm instead.
                for (c, _) in legality_children {
                    rest.push(c);
                }
            }
            if planes.is_empty() {
                return (None, FilterExpr::And(rest));
            }
            let residual = if rest.len() == 1 { rest.pop().unwrap() } else { FilterExpr::And(rest) };
            (Some(and_of(planes)), residual)
        }
        other => (None, other),
    }
}

// ─── Evaluation ───────────────────────────────────────────────────────────────

impl PlaneExpr {
    /// One 64-card word of the expression: recursive over the tree, so a full
    /// evaluation is a single pass over the words with no intermediate bitmaps.
    fn eval_word(&self, words: &rkyv::Archived<Vec<u64>>, wpp: usize, i: usize) -> u64 {
        match self {
            PlaneExpr::Plane(p) => u64::from(words[*p as usize * wpp + i]),
            PlaneExpr::Bits(bits) => bits[i],
            PlaneExpr::And(children) => {
                let mut acc = !0u64;
                for c in children {
                    acc &= c.eval_word(words, wpp, i);
                    if acc == 0 {
                        break;
                    }
                }
                acc
            }
            PlaneExpr::Or(children) => {
                let mut acc = 0u64;
                for c in children {
                    acc |= c.eval_word(words, wpp, i);
                    if acc == !0u64 {
                        break;
                    }
                }
                acc
            }
            PlaneExpr::Not(inner) => !inner.eval_word(words, wpp, i),
            PlaneExpr::Const(b) => {
                if *b {
                    !0u64
                } else {
                    0u64
                }
            }
        }
    }
}

/// Evaluate a plane expression into a card bitmap (`out` is a reused buffer).
/// The tail bits past n_cards are cleared — Not() would otherwise set them.
pub(crate) fn eval_planes(expr: &PlaneExpr, planes: &rkyv::Archived<BitPlanes>, out: &mut Vec<u64>) {
    let n_cards = u32::from(planes.n_cards) as usize;
    let wpp = words_per_plane(n_cards);
    out.clear();
    out.reserve(wpp);
    for i in 0..wpp {
        out.push(expr.eval_word(&planes.words, wpp, i));
    }
    let tail = n_cards % 64;
    if tail != 0 {
        out[wpp - 1] &= (1u64 << tail) - 1;
    }
}

/// If plane `p` is one of legality's existence planes, the (shift, expected,
/// is_illegal) it addresses -- `shift` matches `FilterExpr::Legality`'s own
/// field, `expected` is the status the block was built for (`LEGAL`/`BANNED`/
/// `RESTRICTED`), and `is_illegal` distinguishes an `_ABSENT`/`_ILLEGAL`
/// (negated-check) plane from its `_EXISTS` counterpart. `None` for every
/// other (card-invariant) plane. Derived from `LEGALITY_STATUS_TABLE`, the
/// same single source of truth `status_plane_bases` reads.
fn legality_plane_shift(p: usize) -> Option<(u8, u64, bool)> {
    for &(expected, exists_base, absent_base) in &LEGALITY_STATUS_TABLE {
        if (exists_base..exists_base + MAX_FORMATS).contains(&p) {
            return Some((((p - exists_base) * 2) as u8, expected, false));
        }
        if (absent_base..absent_base + MAX_FORMATS).contains(&p) {
            return Some((((p - absent_base) * 2) as u8, expected, true));
        }
    }
    None
}

/// Evaluate a compiled plane expression against one specific printing, rather
/// than a card's already-known aggregate truth -- needed wherever row
/// emission picks/verifies a printing for a card whose card-level match came
/// through a legality leaf (docs/issues/engine-legality-divergent-carveout.md
/// "Row selection for unique=card"): `legal_exists`/`illegal_exists` only
/// guarantee *some* printing satisfies the expression, not this one. Every
/// other (card-invariant) leaf reads the same card-level bit `eval_planes`
/// would have used -- identical for every printing of the card by
/// construction -- so this only actually diverges from the card-level answer
/// at a legality leaf, which consults `printing` directly instead, mirroring
/// `tri()`'s own per-printing check (`filter.rs`'s `Legality` arm). Callers
/// only need this for the bounded set of printings being emitted, never the
/// whole candidate set -- see the design doc for why that scoping is what
/// makes this cheap instead of repeating the abandoned first design's
/// performance mistake.
pub(crate) fn eval_plane_expr_for_printing(
    expr: &PlaneExpr,
    planes: &rkyv::Archived<BitPlanes>,
    cid: u32,
    printing: &rkyv::Archived<Printing>,
) -> bool {
    match expr {
        PlaneExpr::Plane(p) => {
            let p = *p as usize;
            if let Some((shift, expected, is_illegal)) = legality_plane_shift(p) {
                let status = (u64::from(printing.card_legalities) >> shift) & 0b11;
                if is_illegal { status != expected } else { status == expected }
            } else {
                let wpp = words_per_plane(u32::from(planes.n_cards) as usize);
                let word = u64::from(planes.words[p * wpp + cid as usize / 64]);
                (word >> (cid % 64)) & 1 == 1
            }
        }
        PlaneExpr::Bits(bits) => bitmap_contains(bits, cid),
        PlaneExpr::Const(b) => *b,
        PlaneExpr::And(children) => children.iter().all(|c| eval_plane_expr_for_printing(c, planes, cid, printing)),
        PlaneExpr::Or(children) => children.iter().any(|c| eval_plane_expr_for_printing(c, planes, cid, printing)),
        PlaneExpr::Not(inner) => !eval_plane_expr_for_printing(inner, planes, cid, printing),
    }
}

/// True iff card `cid`'s bit is set in the bitmap.
pub(crate) fn bitmap_contains(bitmap: &[u64], cid: u32) -> bool {
    bitmap[(cid >> 6) as usize] >> (cid & 63) & 1 != 0
}

/// Materialize a bitmap's set bits as ascending card ids.
pub(crate) fn bitmap_card_ids(bitmap: &[u64]) -> Vec<u32> {
    let count: usize = bitmap.iter().map(|w| w.count_ones() as usize).sum();
    let mut out: Vec<u32> = Vec::with_capacity(count);
    for (i, &word) in bitmap.iter().enumerate() {
        let mut w = word;
        while w != 0 {
            out.push((i as u32) << 6 | w.trailing_zeros());
            w &= w - 1;
        }
    }
    out
}
