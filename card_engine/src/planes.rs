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

use super::filter::{CmpOp, ColorField, FilterExpr};
use super::OracleCard;

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
pub(crate) const PLANE_COUNT: usize = PLANE_DEVOTION + DEVOTION_BITS * COLOR_PLANES;

#[derive(Archive, Serialize, Deserialize, Default)]
pub(crate) struct BitPlanes {
    pub(crate) n_cards: u32,
    /// PLANE_COUNT × words_per_plane, flattened plane-major; bit i of plane p
    /// is words[p * wpp + i/64] >> (i%64) & 1.
    pub(crate) words: Vec<u64>,
}

pub(crate) fn words_per_plane(n_cards: usize) -> usize {
    n_cards.div_ceil(64)
}

/// A card's effective per-color devotion count, saturated to 0..=3 —
/// exactly the map FilterExpr::Devotion evaluates (hybrid-expanded when
/// hybrids are present, raw pips otherwise).
fn devotion_count(card: &OracleCard, sym: &str) -> u8 {
    let map = card.mana_cost.devotion.as_ref().unwrap_or(&card.mana_cost.pips);
    map.get(sym).copied().unwrap_or(0).min(3)
}

const DEVOTION_SYMS: [&str; COLOR_PLANES] = ["W", "U", "B", "R", "G", "C"];

pub(crate) fn build_bit_planes(cards: &[OracleCard]) -> BitPlanes {
    let wpp = words_per_plane(cards.len());
    let mut words = vec![0u64; PLANE_COUNT * wpp];
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
        for (b, sym) in DEVOTION_SYMS.iter().enumerate() {
            let count = devotion_count(card, sym);
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
                "devotion without identity: card {i} color {sym}"
            );
        }
    }
    BitPlanes { n_cards: cards.len() as u32, words }
}

// ─── Plane expressions ────────────────────────────────────────────────────────

/// A filter subtree compiled to mask algebra over planes. Evaluation is
/// word-at-a-time (eval_word), one pass over the words with no per-node
/// temporaries.
pub(crate) enum PlaneExpr {
    Plane(u16),
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

fn devotion_color(sym: &str) -> Option<usize> {
    DEVOTION_SYMS.iter().position(|s| *s == sym)
}

/// Compile a Devotion node exactly, mirroring FilterExpr::Devotion's tri():
/// Ge constrains only the queried colors; Le/Eq additionally pin every
/// unqueried color to zero (SQL devotion-column containment semantics).
/// None whenever any needed comparison crosses the saturation boundary.
fn compile_devotion(op: CmpOp, pips: &std::collections::HashMap<String, u8>) -> Option<PlaneExpr> {
    let query: Vec<(usize, u8)> = pips
        .iter()
        .map(|(sym, &k)| devotion_color(sym).map(|c| (c, k)))
        .collect::<Option<Vec<_>>>()?;
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
pub(crate) fn compile_devotion_superset(pips: &std::collections::HashMap<String, u8>) -> Option<PlaneExpr> {
    pips.iter()
        .map(|(sym, &k)| devotion_color(sym).and_then(|c| dev_ge(c, k.min(3))))
        .collect::<Option<Vec<_>>>()
        .map(and_of)
}

/// Compile a filter subtree to a plane expression, or None if any node in it
/// is not plane-expressible. Only two-valued card-level nodes may compile:
/// complement (Not) is only sound when the node can never be Null or
/// PrintingDep, which holds for ColorCmp and TypeCmp by their tri() arms.
pub(crate) fn compile_plane(filter: &FilterExpr) -> Option<PlaneExpr> {
    match filter {
        FilterExpr::True => Some(PlaneExpr::Const(true)),
        FilterExpr::And(children) => children
            .iter()
            .map(compile_plane)
            .collect::<Option<Vec<_>>>()
            .map(and_of),
        FilterExpr::Or(children) => children
            .iter()
            .map(compile_plane)
            .collect::<Option<Vec<_>>>()
            .map(or_of),
        FilterExpr::Not(inner) => compile_plane(inner).map(|p| PlaneExpr::Not(Box::new(p))),
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
        FilterExpr::Devotion { op, pips } => compile_devotion(*op, pips),
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
pub(crate) fn split_planes(filter: FilterExpr) -> (Option<PlaneExpr>, FilterExpr) {
    if matches!(filter, FilterExpr::True) {
        return (None, filter);
    }
    if let Some(pe) = compile_plane(&filter) {
        return (Some(pe), FilterExpr::True);
    }
    match filter {
        FilterExpr::And(children) => {
            let mut planes: Vec<PlaneExpr> = Vec::new();
            let mut rest: Vec<FilterExpr> = Vec::new();
            for c in children {
                match compile_plane(&c) {
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
