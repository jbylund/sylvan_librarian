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
//! Produced mana is deliberately left out for now; `produces:` queries stay on
//! the residual path. Rarity and legality need the narrowing/divergence
//! machinery from later phases (see the issue).

use rkyv::{Archive, Deserialize, Serialize};

use super::filter::{CmpOp, ColorField, FilterExpr, NumExpr, NumField, TextSearchField};
use super::legality::{LEGALITY_LEGAL, MAX_FORMATS};
use super::{flip_op, lane_get, oracle_word_eligible, scan_oracle_words, OracleCard, OracleWordIndex, Printing, NONE_STR};

/// Plane layout, plane-major in BitPlanes.words: six color planes per color
/// field (W U B R G C, bit order matching color_to_bit — C is always zero for
/// colors/identity but keeps the mask algebra total over whatever mask the
/// parser emits), then one plane per card type bit.
const COLOR_PLANES: usize = 6;
const TYPE_PLANES: usize = 14;
const PLANE_COLORS: usize = 0;
const PLANE_IDENTITY: usize = COLOR_PLANES;
const PLANE_TYPES: usize = 2 * COLOR_PLANES;
/// Devotion is bit-sliced: two saturating bits per color (count clamped to
/// 0..=3), so `devotion:uu` is one plane read and `devotion:uuu` is exactly
/// the saturated bucket. Counts come from the same hybrid-expanded map the
/// evaluator uses; the ~0.5% of cards at 3+ per color are the verification
/// set for deeper queries (see the saturated-superset arm in narrow_rec).
const PLANE_DEVOTION: usize = PLANE_TYPES + TYPE_PLANES;
const DEVOTION_BITS: usize = 2;
/// One plane per format's "legal" bit (#630 phase 2), fixed-width at
/// MAX_FORMATS regardless of how many formats loaded data actually uses —
/// unused slots are permanently-zero planes, matching the existing `shift:
/// None` "format absent" semantics. These are narrowing-only candidate masks
/// (see narrow_rec's Legality arms in lib.rs), never exact-consumed here:
/// Legality can return Tri::PrintingDep for divergent cards, which compile_plane
/// already excludes from exact consumption (only two-valued nodes qualify).
pub(crate) const PLANE_LEGAL: usize = PLANE_DEVOTION + DEVOTION_BITS * COLOR_PLANES;
pub(crate) const PLANE_BORDER_BLACK: usize = PLANE_LEGAL + MAX_FORMATS;
pub(crate) const PLANE_BORDER_BORDERLESS: usize = PLANE_BORDER_BLACK + 1;
pub(crate) const PLANE_BORDER_WHITE: usize = PLANE_BORDER_BORDERLESS + 1;
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
pub(crate) const PLANE_CMC: usize = PLANE_BORDER_WHITE + 1;
pub(crate) const PLANE_CMC_HI: usize = PLANE_CMC + NUM_INTERIOR_WIDTH;
pub(crate) const PLANE_POWER_LO: usize = PLANE_CMC_HI + 1;
pub(crate) const PLANE_POWER: usize = PLANE_POWER_LO + 1;
pub(crate) const PLANE_POWER_HI: usize = PLANE_POWER + NUM_INTERIOR_WIDTH;
pub(crate) const PLANE_TOUGHNESS_LO: usize = PLANE_POWER_HI + 1;
pub(crate) const PLANE_TOUGHNESS: usize = PLANE_TOUGHNESS_LO + 1;
pub(crate) const PLANE_TOUGHNESS_HI: usize = PLANE_TOUGHNESS + NUM_INTERIOR_WIDTH;
pub(crate) const PLANE_COUNT: usize = PLANE_TOUGHNESS_HI + 1;

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

pub(crate) fn build_bit_planes(cards: &[OracleCard], printings: &[Printing], offsets: &[u32], strings: &[String]) -> BitPlanes {
    let wpp = words_per_plane(cards.len());
    let mut words = vec![0u64; PLANE_COUNT * wpp];
    let border_id =
        |needle: &str| strings.iter().position(|s| s == needle).map(|i| i as u32).filter(|&id| id != NONE_STR);
    let border_black = border_id("black");
    let border_borderless = border_id("borderless");
    let border_white = border_id("white");
    let mut cmc_hi = BucketBounds::default();
    let mut power_lo = BucketBounds::default();
    let mut power_hi = BucketBounds::default();
    let mut toughness_lo = BucketBounds::default();
    let mut toughness_hi = BucketBounds::default();
    for (i, card) in cards.iter().enumerate() {
        let mut set = |plane: usize| words[plane * wpp + i / 64] |= 1u64 << (i % 64);
        for b in 0..COLOR_PLANES {
            if card.card_colors & (1 << b) != 0 {
                set(PLANE_COLORS + b);
            }
            if card.card_color_identity & (1 << b) != 0 {
                set(PLANE_IDENTITY + b);
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
        // "legal AND not divergent", not raw status: this makes the plane a
        // pure exact predicate (no false positives, ever) rather than one that
        // merely happens to be corrected downstream. narrow_rec's OR-with-
        // divergent-postings formula (see legal_candidate_bits in lib.rs)
        // produces an identical candidate mask either way (De Morgan's), so
        // this costs nothing today — but it means `legal_x` alone is already
        // the exact card-space source #634 wants to popcount directly, with
        // the divergent set as the one small carve-out #634's promotion path
        // would still need to verify per-candidate.
        for f in 0..MAX_FORMATS {
            let shift = (f * 2) as u32;
            if !card.legality_divergent && (card.card_legalities >> shift) & 0b11 == LEGALITY_LEGAL {
                set(PLANE_LEGAL + f);
            }
        }
        let start = offsets[i] as usize;
        let end = offsets[i + 1] as usize;
        let mut has_black = false;
        let mut has_borderless = false;
        let mut has_white = false;
        for p in &printings[start..end] {
            let bid = p.card_border_id;
            has_black |= border_black == Some(bid);
            has_borderless |= border_borderless == Some(bid);
            has_white |= border_white == Some(bid);
            if has_black && has_borderless && has_white {
                break;
            }
        }
        if has_black {
            set(PLANE_BORDER_BLACK);
        }
        if has_borderless {
            set(PLANE_BORDER_BORDERLESS);
        }
        if has_white {
            set(PLANE_BORDER_WHITE);
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
/// (production is ~31.5k, comfortably under u16::MAX). Scattered directly
/// into a format's legal_x candidate mask by legal_candidate_bits (lib.rs)
/// rather than intersected via the general Candidates machinery — the set is
/// always this same small list, not query-dependent, so there's nothing to
/// look up.
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

pub(crate) fn compile_plane(filter: &FilterExpr, bounds: &rkyv::Archived<BitPlanes>, words: &rkyv::Archived<OracleWordIndex>) -> Option<PlaneExpr> {
    match filter {
        FilterExpr::True => Some(PlaneExpr::Const(true)),
        FilterExpr::And(children) => compile_plane_children(children, bounds, words).map(and_of),
        FilterExpr::Or(children) => compile_plane_children(children, bounds, words).map(or_of),
        FilterExpr::Not(inner) => {
            if contains_unnegatable_numeric(inner) {
                return None;
            }
            compile_plane(inner, bounds, words).map(|p| PlaneExpr::Not(Box::new(p)))
        }
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
                // Deliberately unplaned for now (#630): produces: stays residual.
                ColorField::ProducedMana => return None,
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
        _ => None,
    }
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
pub(crate) fn split_planes(filter: FilterExpr, bounds: &rkyv::Archived<BitPlanes>, words: &rkyv::Archived<OracleWordIndex>) -> (Option<PlaneExpr>, FilterExpr) {
    if matches!(filter, FilterExpr::True) {
        return (None, filter);
    }
    if let Some(pe) = compile_plane(&filter, bounds, words) {
        return (Some(pe), FilterExpr::True);
    }
    match filter {
        FilterExpr::And(children) => {
            let mut planes: Vec<PlaneExpr> = Vec::new();
            let mut rest: Vec<FilterExpr> = Vec::new();
            for c in children {
                match compile_plane(&c, bounds, words) {
                    Some(pe) => planes.push(pe),
                    None => rest.push(c),
                }
            }
            if planes.is_empty() {
                return (None, FilterExpr::And(rest));
            }
            // rest is nonempty here: had every child compiled, the whole-tree
            // compile above would have consumed the And.
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
