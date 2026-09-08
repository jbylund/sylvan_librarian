use memchr::memmem;
use rkyv::Archived;
use fancy_regex::{Error as FancyError, Regex};
use serde_json::Value;
use super::{AOracleCard, APrinting, AStrings, ManaCost, str_at, mana_lane, lane_add, lane_get, lanes_ge, LANES8_HI, mana_pip_counts, mana_cmc, mana_bare_generic, color_list_to_mask, card_type_str_to_bit, trigram_candidates, trigram_min_posting, ARTIST_NONE, NONE_STR, FlavorIndex, NameBigramIndex, PrintedNameIndex, OracleTextIndex, SortedTrigramIndex, flavor_fingerprint, flavor_match_sets};
use super::legality::{LEGALITY_LEGAL, LEGALITY_BANNED, LEGALITY_RESTRICTED, format_shift};

/// Public search TextRegex backtrack cap — calibrated in docs/issues/security-regex-execution-budget.md.
pub(crate) const REGEX_BACKTRACK_LIMIT: usize = 8192;

/// Prefix on `build_filter` errors that must surface as `UnsupportedRegexError`, not `RetryableQueryError`.
pub(crate) const REGEX_COMPILE_ERR_PREFIX: &str = "regex_compile:";

/// Prefix on runtime regex match failures (`is_match` backtrack exhaustion, etc.).
pub(crate) const REGEX_MATCH_ERR_PREFIX: &str = "regex_match:";

pub(crate) fn compile_search_regex(pattern: &str) -> Result<Regex, String> {
    fancy_regex::RegexBuilder::new(&format!("(?i){pattern}"))
        .backtrack_limit(REGEX_BACKTRACK_LIMIT)
        .build()
        .map_err(|e| format!("{REGEX_COMPILE_ERR_PREFIX}{e}"))
}

use std::cell::Cell;

thread_local! {
    static REGEX_MATCH_FAILED: Cell<bool> = const { Cell::new(false) };
}

/// Reset before bind/evaluate so a prior query on this thread cannot poison the next.
pub(crate) fn clear_regex_match_failed() {
    REGEX_MATCH_FAILED.with(|c| c.set(false));
}

/// Take and clear the failure flag; `Some(message)` when a match aborted at runtime.
pub(crate) fn take_regex_match_failed() -> Option<String> {
    REGEX_MATCH_FAILED.with(|c| {
        if c.get() {
            c.set(false);
            Some(format!("{REGEX_MATCH_ERR_PREFIX}regex execution limit exceeded"))
        } else {
            None
        }
    })
}

fn regex_is_match(re: &Regex, hay: &str) -> bool {
    if REGEX_MATCH_FAILED.with(|c| c.get()) {
        return false;
    }
    match re.is_match(hay) {
        Ok(m) => m,
        Err(FancyError::RuntimeError(_)) => {
            REGEX_MATCH_FAILED.with(|c| c.set(true));
            false
        }
        Err(_) => false,
    }
}

#[cfg(test)]
pub(crate) fn regex_is_match_for_test(re: &Regex, hay: &str) -> bool {
    regex_is_match(re, hay)
}

#[cfg(test)]
pub(crate) fn compile_search_regex_for_test(pattern: &str) -> Regex {
    compile_search_regex(pattern).expect("test regex should compile")
}

// ─── Comparison / arithmetic operators ───────────────────────────────────────

#[derive(Clone, Copy, PartialEq, Debug)]
pub(crate) enum CmpOp {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
}

#[derive(Clone, Copy)]
pub(crate) enum ArithOp {
    Add,
    Sub,
    Mul,
    Div,
}

// ─── Four-valued evaluation result ────────────────────────────────────────────

/// Evaluation result of a filter node. True/False/Null follow SQL ternary logic
/// (Null = a compared attribute is missing); PrintingDep is produced only during
/// the card-level pass (printing = None) when a predicate depends on
/// printing-level fields, and tells the query driver to re-evaluate per printing.
/// With a printing supplied, PrintingDep can never occur.
#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Tri {
    True,
    False,
    Null,
    PrintingDep,
}

fn tri_bool(b: bool) -> Tri {
    if b { Tri::True } else { Tri::False }
}

// ─── Numeric expressions ──────────────────────────────────────────────────────

// PartialEq so a caller can ask "is this leaf about the field I care about" — `sort_col_bound` matches
// the sort column against the field a NumericCmp constrains.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum NumField {
    Cmc,
    Power,
    Toughness,
    Loyalty,
    RarityInt,
    CollectorNumberInt,
    EdhrEc,
    PriceUsd,
    PriceEur,
    PriceTix,
    PreferScore,
}

fn attr_to_num_field(attr: &str) -> Option<NumField> {
    match attr {
        "cmc"                  => Some(NumField::Cmc),
        "creature_power"       => Some(NumField::Power),
        "creature_toughness"   => Some(NumField::Toughness),
        "planeswalker_loyalty" => Some(NumField::Loyalty),
        "card_rarity_int"      => Some(NumField::RarityInt),
        "collector_number_int" => Some(NumField::CollectorNumberInt),
        "edhrec_rank"          => Some(NumField::EdhrEc),
        "price_usd"            => Some(NumField::PriceUsd),
        "price_eur"            => Some(NumField::PriceEur),
        "price_tix"            => Some(NumField::PriceTix),
        "prefer_score"         => Some(NumField::PreferScore),
        _ => None,
    }
}

/// Numeric operand during evaluation. PDep occurs only in the card-level pass
/// (printing = None) for printing-level fields.
#[derive(Clone, Copy)]
pub(crate) enum NumVal {
    Known(f64),
    Null,
    PDep,
}

fn field_num(card: &AOracleCard, printing: Option<&APrinting>, f: NumField) -> NumVal {
    fn known(v: Option<f32>) -> NumVal {
        v.map_or(NumVal::Null, |x| NumVal::Known(x as f64))
    }
    // Cents -> dollars via exact f64 division, not through f32 -- 722.0 / 100.0 and a
    // directly-parsed query constant "7.22" round to the identical nearest f64 (both are
    // single, non-lossy roundings of the same rational number), so this and NumExpr::Const
    // (untouched) always agree exactly. Unconditional, regardless of comparison shape
    // (Arith, Field-vs-Field, bare Field-vs-Const, ...): a bind-time fast path that
    // special-cased only the bare shape shipped two silent correctness bugs
    // (`usd+1<power`, `usd<cmc` -- see docs/issues/local-engine-broad-range-fastpath.md)
    // for a ~2-3% win on the one shape it covered, not worth the ongoing risk of a
    // representation that's easy to bypass by rephrasing a logically identical query.
    fn known_cents(v: Option<u32>) -> NumVal {
        v.map_or(NumVal::Null, |cents| NumVal::Known(f64::from(cents) / 100.0))
    }
    match f {
        NumField::Cmc                => known(card.cmc.as_ref().map(|v| f32::from(*v))),
        NumField::Power              => known(card.creature_power.as_ref().map(|v| f32::from(*v))),
        NumField::Toughness          => known(card.creature_toughness.as_ref().map(|v| f32::from(*v))),
        NumField::Loyalty            => known(card.planeswalker_loyalty.as_ref().map(|v| f32::from(*v))),
        NumField::EdhrEc             => known(card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32)),
        NumField::RarityInt          => printing.map_or(NumVal::PDep, |p| known(p.card_rarity_int.as_ref().map(|v| f32::from(*v)))),
        NumField::CollectorNumberInt => printing.map_or(NumVal::PDep, |p| known(p.collector_number_int.as_ref().map(|v| u16::from(*v) as f32))),
        // The COALESCED search key, not the raw column: `usd` falls back to the foil and then the
        // etched price on api.scryfall.com, which is 121 cards on `usd>=500` alone. See
        // `crate::search_price_usd_cents` — the range index the planner narrows with is built from
        // the same function, and they have to agree or a correct row is narrowed away.
        NumField::PriceUsd           => printing.map_or(NumVal::PDep, |p| known_cents(super::search_price_usd_cents(p))),
        NumField::PriceEur           => printing.map_or(NumVal::PDep, |p| known_cents(super::search_price_eur_cents(p))),
        NumField::PriceTix           => printing.map_or(NumVal::PDep, |p| known_cents(p.price_tix.as_ref().map(|v| u32::from(*v)))),
        NumField::PreferScore        => printing.map_or(NumVal::PDep, |p| known(p.prefer_score.as_ref().map(|v| f32::from(*v)))),
    }
}

#[derive(Clone)]
pub(crate) enum NumExpr {
    Const(f64),
    Field(NumField),
    Arith(Box<NumExpr>, ArithOp, Box<NumExpr>),
}

impl NumExpr {
    // #[inline(always)] alone doesn't reach the goal here: LLVM's
    // always-inliner refuses to inline ANY self-recursive function at ANY
    // call site, not just the recursive edge -- confirmed in the release
    // disassembly, where a first attempt at just adding the attribute to a
    // still-self-recursive eval() left both `bl NumExpr::eval` calls in
    // FilterExpr::tri's NumericCmp arm untouched. Splitting the Arith case
    // into its own (separately named, not force-inlined) function makes
    // eval_with() itself non-recursive, so the attribute now actually applies:
    // for the common Const/Field leaf case (e.g. `usd<50`, no arithmetic),
    // eval_with()'s whole body -- including the fetch closure (field_num,
    // already small enough to inline on its own) -- folds directly into tri(),
    // eliminating both calls' prologue/epilogue/jump-table tax. Arith
    // (`cmc+1<power`) is colder and still recurses through eval_arith_with,
    // unaffected either way.
    //
    // Generic over the field-fetch (#743) so the real per-card path (fetch =
    // field_num) and the tuple-scan path (fetch = one tuple key's four fields)
    // share this one evaluator — no second copy of the recursion /
    // NULL-propagation / div-by-zero logic to drift (the
    // and_child_rank/narrow_rec single-source-of-truth lesson from #741). The
    // per-card call site (tri's NumericCmp arm) passes a concrete closure, so
    // monomorphization + #[inline(always)] reproduce the pre-#743 hand-written
    // match exactly.
    #[inline(always)]
    fn eval_with<F: Fn(NumField) -> NumVal>(&self, fetch: &F) -> NumVal {
        match self {
            NumExpr::Const(v) => NumVal::Known(*v),
            NumExpr::Field(f) => fetch(*f),
            NumExpr::Arith(lhs, op, rhs) => Self::eval_arith_with(lhs, *op, rhs, fetch),
        }
    }

    fn eval_arith_with<F: Fn(NumField) -> NumVal>(lhs: &NumExpr, op: ArithOp, rhs: &NumExpr, fetch: &F) -> NumVal {
        // Null dominates PDep: Null op anything is Null for every
        // printing, so the card-level result is already exact.
        match (lhs.eval_with(fetch), rhs.eval_with(fetch)) {
            (NumVal::Null, _) | (_, NumVal::Null) => NumVal::Null,
            (NumVal::PDep, _) | (_, NumVal::PDep) => NumVal::PDep,
            (NumVal::Known(l), NumVal::Known(r)) => match op {
                ArithOp::Add => NumVal::Known(l + r),
                ArithOp::Sub => NumVal::Known(l - r),
                ArithOp::Mul => NumVal::Known(l * r),
                ArithOp::Div => {
                    if r == 0.0 { NumVal::Null } else { NumVal::Known(l / r) }
                }
            },
        }
    }
}

/// The trivalent result of a `lhs op rhs` numeric comparison, over any field-fetch.
/// Shared by `FilterExpr::tri`'s `NumericCmp` arm (fetch = `field_num`) and the #743
/// arith-tuple scan (fetch = the four card-level fields of one tuple key) so both go
/// through the exact same Null/PDep handling. `#[inline(always)]` + monomorphization
/// keeps the hot `tri` arm byte-for-byte with its former inline match.
#[inline(always)]
pub(crate) fn numeric_cmp_tri<F: Fn(NumField) -> NumVal>(lhs: &NumExpr, op: CmpOp, rhs: &NumExpr, fetch: &F) -> Tri {
    match (lhs.eval_with(fetch), rhs.eval_with(fetch)) {
        (NumVal::Null, _) | (_, NumVal::Null) => Tri::Null, // missing field: SQL NULL
        (NumVal::PDep, _) | (_, NumVal::PDep) => Tri::PrintingDep,
        (NumVal::Known(a), NumVal::Known(b)) => tri_bool(num_cmp(op, a, b)),
    }
}

// ─── per-face numeric values ────────────────────────────────────────
//
// Scryfall matches a `//` card if ANY FACE satisfies the predicate, and the three stat columns
// are INDEPENDENT of one another when it does. Both halves are measured against
// api.scryfall.com, 2026-08-16, with `!"Full // Name"` scoping so the answer is 1 or 404:
//
//   pow>=3                on Delver of Secrets (1/1 // 3/2)          -> 1   (back only)
//   pow=1 tou=2           on Delver                                   -> 1   (no face is 1/2)
//   pow>=3 pow<=1         on Delver                                   -> 1   (one column, two faces)
//   pow>tou               on Huntmaster of the Fells (2/2 // 4/4)     -> 1   (no face has p>t)
//   pow=tou               on Thing in the Ice (0/4 // 7/8)            -> 404 (no pair is equal)
//
// The last two are the pair that settles the shape: a per-face ROW model answers 404 to
// `pow>tou` on Huntmaster, and a "max power vs max toughness" model answers 1 to `pow=tou` on
// Thing in the Ice. A card carrying a SET of values per column, with the comparison existential
// over the cross product, is the only one of the three that answers both as measured — so that
// is what `face_num_values` builds and what `build_arith_tuple_index` interns.
//
// NEGATION is deliberately not part of this: Scryfall IGNORES a negated numeric term outright
// (`-pow=1` answers with `Invalid expression "-pow=1" was ignored`, and `is:dfc -pow>=3` is 2,895
// = the unfiltered `is:dfc`), so it offers no oracle for what NOT should mean over a value set.
// This port keeps its existing NOT-of-the-existential, which is the deviation already ledgered.

/// How many distinct values one card can hold for one face-scoped column. Two faces is the whole
/// corpus (`reversible_card` and `meld` are single-faced rows), and the front's own value is one
/// of them; 4 leaves room for a future three-face layout without a heap allocation in `tri`.
const MAX_FACE_VALUES: usize = 4;

/// A fixed-capacity, allocation-free value set. Local rather than a new crate dependency: the
/// whole need is "up to four f64s on the stack, deduped", and the wasm engine pays for every
/// dependency it links.
#[derive(Default)]
struct FaceValues {
    vals: [f64; MAX_FACE_VALUES],
    len: usize,
}

impl FaceValues {
    fn push(&mut self, v: f64) {
        if self.vals[..self.len].contains(&v) || self.len == MAX_FACE_VALUES {
            return;
        }
        self.vals[self.len] = v;
        self.len += 1;
    }
    fn get(&self, i: usize) -> Option<f64> {
        if i < self.len { Some(self.vals[i]) } else { None }
    }
    /// One "don't care" slot when the card has 0 or 1 value, so a column the card has nothing
    /// for still evaluates once and reaches `field_num`'s NULL exactly as before.
    fn slots(&self) -> usize {
        self.len.max(1)
    }
}

/// True for the columns whose values a face can differ on. `Cmc` is deliberately NOT here:
/// measured, mana value stays card-level on every layout — `mv=0` on Delver (back has no cost),
/// `mv=2` on Fire // Ice (each half) and `mv=2` on Bonecrusher Giant // Stomp (the adventure's
/// cost) are all 404, while the front's 1 / the joined 4 / the creature's 3 all answer 1.
fn num_field_is_face_scoped(f: NumField) -> bool {
    matches!(f, NumField::Power | NumField::Toughness | NumField::Loyalty)
}

fn num_expr_touches_face_field(e: &NumExpr) -> bool {
    match e {
        NumExpr::Const(_) => false,
        NumExpr::Field(f) => num_field_is_face_scoped(*f),
        NumExpr::Arith(l, _, r) => num_expr_touches_face_field(l) || num_expr_touches_face_field(r),
    }
}

/// The distinct values this card holds for one face-scoped column, card value first.
///
/// The card value is always one of the faces' (the merge copies a whole `_FACE_STAT_GROUPS`
/// group from one face), so listing it first costs nothing and makes the single-face path —
/// `faces` empty, one value, identical to the pre-gen-28 behaviour — fall out rather than be
/// special-cased. A face with no value for the column contributes nothing, which is why
/// `pow=4` matches Bonecrusher Giant // Stomp and the costless adventure half adds no NULL.
fn face_num_values(card: &AOracleCard, f: NumField) -> FaceValues {
    let mut out = FaceValues::default();
    let mut push = |v: f64| out.push(v);
    match f {
        NumField::Power => {
            if let Some(v) = card.creature_power.as_ref() {
                push(f64::from(*v));
            }
            for face in card.faces.iter() {
                if let Some(v) = face.creature_power.as_ref() {
                    push(f64::from(*v));
                }
            }
        }
        NumField::Toughness => {
            if let Some(v) = card.creature_toughness.as_ref() {
                push(f64::from(*v));
            }
            for face in card.faces.iter() {
                if let Some(v) = face.creature_toughness.as_ref() {
                    push(f64::from(*v));
                }
            }
        }
        NumField::Loyalty => {
            if let Some(v) = card.planeswalker_loyalty.as_ref() {
                push(f64::from(*v));
            }
            for face in card.faces.iter() {
                if let Some(v) = face.planeswalker_loyalty.as_ref() {
                    push(f64::from(*v));
                }
            }
        }
        _ => {}
    }
    out
}

/// Existential re-evaluation of a `NumericCmp` over a multi-face card's value sets.
///
/// Only reached from `tri` when the card HAS faces and the card-level answer was not already
/// `True`, so the 82% single-face majority and every already-matching row pay one branch. The
/// three columns are enumerated independently (the cross product, per the measurements above);
/// `Cmc` and every printing-level field keep coming from `field_num`, unchanged.
///
/// Three-valued aggregation matches `tri`'s own: any `True` wins, else any `False`, else `Null`.
fn face_numeric_cmp_tri(
    card: &AOracleCard,
    printing: Option<&APrinting>,
    lhs: &NumExpr,
    op: CmpOp,
    rhs: &NumExpr,
    base: Tri,
) -> Tri {
    if !num_expr_touches_face_field(lhs) && !num_expr_touches_face_field(rhs) {
        return base;
    }
    let powers = face_num_values(card, NumField::Power);
    let toughnesses = face_num_values(card, NumField::Toughness);
    let loyalties = face_num_values(card, NumField::Loyalty);
    let mut acc = base;
    for pi in 0..powers.slots() {
        for ti in 0..toughnesses.slots() {
            for li in 0..loyalties.slots() {
                let fetch = |f: NumField| -> NumVal {
                    let pick = |vs: &FaceValues, i: usize| vs.get(i).map_or(NumVal::Null, NumVal::Known);
                    match f {
                        NumField::Power => pick(&powers, pi),
                        NumField::Toughness => pick(&toughnesses, ti),
                        NumField::Loyalty => pick(&loyalties, li),
                        other => field_num(card, printing, other),
                    }
                };
                match numeric_cmp_tri(lhs, op, rhs, &fetch) {
                    Tri::True => return Tri::True,
                    Tri::PrintingDep => return Tri::PrintingDep,
                    Tri::False => acc = Tri::False,
                    Tri::Null => {}
                }
            }
        }
    }
    acc
}

/// Card-level numeric fields the #743 joint-tuple index covers: all card-scoped (not
/// printing-dependent), all small bounded integer domains, confirmed low joint
/// cardinality (531-564 distinct combinations — see docs/issues/00743). A numeric
/// expression is tuple-evaluable iff every `NumField` it references (recursively
/// through `Arith`) is one of these; any other field (edhrec, price*, rarity, cn,
/// prefer) disqualifies the whole expression.
pub(crate) fn num_field_in_arith_tuple_scope(f: NumField) -> bool {
    matches!(f, NumField::Cmc | NumField::Power | NumField::Toughness | NumField::Loyalty)
}

fn num_expr_all_in_tuple_scope(e: &NumExpr) -> bool {
    match e {
        NumExpr::Const(_) => true,
        NumExpr::Field(f) => num_field_in_arith_tuple_scope(*f),
        NumExpr::Arith(l, _, r) => num_expr_all_in_tuple_scope(l) && num_expr_all_in_tuple_scope(r),
    }
}

/// A bare single-field `{cmc,power,toughness} op const` comparison — already handled
/// exactly by `narrow_rec`'s dedicated single-field numeric-index arms. The tuple route
/// must decline these (the direct index is at least as good, and re-routing their
/// negation would desync `and_child_rank`'s ranking from `narrow_rec`'s dispatch — the
/// exact mismatch class #741 fought). Loyalty is intentionally excluded: it has no
/// dedicated numeric index, so the tuple route is its only narrowing.
fn is_bare_dedicated_numeric(f: &FilterExpr) -> bool {
    matches!(
        f,
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::Cmc | NumField::Power | NumField::Toughness), rhs: NumExpr::Const(_), .. }
            | FilterExpr::NumericCmp { lhs: NumExpr::Const(_), rhs: NumExpr::Field(NumField::Cmc | NumField::Power | NumField::Toughness), .. }
    )
}

/// Single source of truth (#741 precedent) for "does this filter take the #743 arith-tuple
/// route": a `NumericCmp` whose every referenced field is in `num_field_in_arith_tuple_scope`,
/// and which is *not* one of the bare single-field shapes the dedicated numeric-index arms
/// already own. Both `narrow_rec` (positive fallback and the negated arm) and `and_child_rank`
/// gate on this one function, so the narrowing dispatch and its cost ranking cannot drift.
/// A mixed expression (e.g. `usd+1<power`, an in-scope field with a printing-level one) fails
/// the scope check and declines here entirely — it is never partially narrowed.
pub(crate) fn is_arith_tuple_route(f: &FilterExpr) -> bool {
    match f {
        FilterExpr::NumericCmp { lhs, rhs, .. } => {
            num_expr_all_in_tuple_scope(lhs) && num_expr_all_in_tuple_scope(rhs) && !is_bare_dedicated_numeric(f)
        }
        _ => false,
    }
}

/// Evaluate a tuple-routed `NumericCmp` against one joint tuple key's four field values
/// (each `None` = the card has no value for that field, i.e. SQL NULL). Reuses
/// `numeric_cmp_tri`/`NumExpr::eval_with` — the identical evaluator the per-card path
/// uses — so the differential test's exact-agreement claim holds by construction, not by
/// a parallel reimplementation. Returns `Tri::True`/`Tri::False`/`Tri::Null`; `PrintingDep`
/// cannot occur (all four fields are card-level), and any out-of-scope field would be an
/// `is_arith_tuple_route` bug, caught by the debug_assert (CI runs debug).
pub(crate) fn eval_arith_tuple_tri(
    lhs: &NumExpr,
    op: CmpOp,
    rhs: &NumExpr,
    cmc: Option<f64>,
    power: Option<f64>,
    toughness: Option<f64>,
    loyalty: Option<f64>,
) -> Tri {
    let fetch = |f: NumField| -> NumVal {
        let v = match f {
            NumField::Cmc => cmc,
            NumField::Power => power,
            NumField::Toughness => toughness,
            NumField::Loyalty => loyalty,
            _ => {
                debug_assert!(false, "out-of-scope field reached arith-tuple eval; is_arith_tuple_route is wrong");
                None
            }
        };
        v.map_or(NumVal::Null, NumVal::Known)
    };
    numeric_cmp_tri(lhs, op, rhs, &fetch)
}

/// The one numeric comparator dispatch. `pub(crate)` because the rarity candidate
/// builders in `lib.rs` need exactly this and used to each carry their own copy.
pub(crate) fn num_cmp(op: CmpOp, a: f64, b: f64) -> bool {
    match op {
        CmpOp::Eq => a == b,
        CmpOp::Ne => a != b,
        CmpOp::Lt => a < b,
        CmpOp::Le => a <= b,
        CmpOp::Gt => a > b,
        CmpOp::Ge => a >= b,
    }
}

// ─── Color / collection / text field enums ───────────────────────────────────

#[derive(Clone, Copy)]
pub(crate) enum ColorField {
    Colors,
    ColorIdentity,
    ProducedMana,
}

fn card_colors(card: &AOracleCard, f: ColorField) -> u8 {
    match f {
        ColorField::Colors        => card.card_colors,
        ColorField::ColorIdentity => card.card_color_identity,
        ColorField::ProducedMana  => card.produced_mana,
    }
}

// ─── per-face colours ────────────────────────────────────────────────────────
//
// `colors` is the one colour column a face has of its own, and Scryfall compares the query against
// EVERY face's mask, existentially — the same shape the stat columns take, and for the same
// measured reason. Each row below is a live probe against api.scryfall.com on 2026-08-16, scoped
// with `!"Full // Name"` so the answer is 1 or 404:
//
//   c=b     on Valki, God of Lies // Tibalt (B // BR)             -> 1    the FRONT's mask alone
//   c:c     on Kabira Takedown // Kabira Plateau (W // [])        -> 1    the land back is colourless
//   c=wb    on Extus // Awaken the Blood Avatar (WB // BR)        -> 1    one face exactly
//   c=br    on Extus                                              -> 1    the other face exactly
//   c:brw   on Extus                                              -> 404  NO face is {W,B,R}
//   c=3     on Extus                                              -> 404  no face has three
//   c=2     on Extus                                              -> 1    both faces have two
//   c<=b    on Valki // Tibalt                                    -> 1    B ⊆ B
//   c:c     on Fire // Ice (split, faces declare NO colours)      -> 404  the faces are the card's
//
// The last row is the one that constrains the SHAPE rather than the semantics: a split or flip
// face carries no `colors` key at all, so reading its absence as the mask 0 would answer 1 there.
// The middle rows are why the card's own union is NOT a member of the set — `c:brw` and `c=3` are
// satisfied by {W,B,R} and by nothing else, and {W,B,R} is a value no face of Extus has.
//
// `color_identity` and `produced_mana` are card-level and stay that way, measured the same way and
// agreeing on both sides already: `id=wbr` on Extus is 1 while `id=wb` and `id=2` are 404 (the
// identity really is the card's three colours, not either face's two), and Scryfall's face objects
// carry neither key. Mana VALUE is card-level for the identical reason — see
// `num_field_is_face_scoped`.

/// One card's colour comparison against one mask. The single definition the two structures that
/// decide a colour leaf share: `tri`'s ColorCmp arm below, and `planes::compile_plane`, which
/// evaluates it at COMPILE time against every possible mask to pick the planes to OR. Stating the
/// operator once is what makes the plane expression and `tri` unable to disagree about it.
pub(crate) fn color_cmp(bits: u8, op: CmpOp, mask: u8) -> bool {
    match op {
        // mask == 0 means the query was literally "c"/"colorless" (see
        // get_colors_comparison_object on the Python side), not "at
        // least zero colors" -- bits & 0 == 0 is vacuously true for
        // every card, so Ge must fall back to exact equality here.
        CmpOp::Ge => if mask == 0 { bits == 0 } else { bits & mask == mask },
        CmpOp::Eq => bits == mask,
        CmpOp::Le => bits & !mask == 0,
        CmpOp::Lt => bits & !mask == 0 && bits != mask,
        CmpOp::Gt => bits & mask == mask && bits != mask,
        CmpOp::Ne => bits != mask,
    }
}

/// The `ColorCmp` predicate against one card's (or, for an exact-total lookup, one stored
/// combination's) bits, in the argument order `exact_result_total`'s color arm reads: the same
/// single definition as `color_cmp`, so the totals table and the residual verify path cannot
/// drift apart.
pub(crate) fn color_cmp_matches(op: CmpOp, mask: u8, bits: u8) -> bool {
    color_cmp(bits, op, mask)
}

/// The distinct colour masks this card holds — the query-time twin of `lib::face_color_masks`,
/// which enumerates the identical set at build time for the planes. Read that one's doc for why
/// the card's union is excluded and why an absent face `colors` inherits it.
///
/// Returns `None` for the two card-level columns and for the ~82% of cards with no faces, which is
/// the caller's signal to keep using the single card-level mask it already read.
fn face_color_masks(card: &AOracleCard, f: ColorField) -> Option<impl Iterator<Item = u8> + '_> {
    if card.faces.is_empty() || !matches!(f, ColorField::Colors) {
        return None;
    }
    let card_mask = card.card_colors;
    Some(card.faces.iter().map(move |face| face.card_colors.as_ref().map_or(card_mask, |v| *v)))
}

// ─── the front face, and `is:vanilla` ────────────────────────────────────────
//
// `is:vanilla` is the third face-scoped shape after the numeric columns above and the colour masks
// beside them, and the only one that is not existential. It is a predicate rather than the
// `t:creature -o:/./` expansion the parser used to give it because that rewrite reads the MERGED
// row, whose oracle text is every face's joined: a card whose FRONT face prints nothing loses to
// the half that does. 352 on both sides against Scryfall's own 363.
//
// THREE RULES, each measured against api.scryfall.com on 2026-08-17, and only the first is the
// question the diagnosis started from:
//
//   1. THE FRONT FACE ANSWERS — not the merged row, and NOT any face. `is:vanilla o:/./` is 12
//      there and all 12 are adventures whose creature front is blank behind an Instant/Sorcery half
//      that prints (`Beluna's Gatekeeper // Entry Denied`). The back is NOT enough: all four of
//      `Kaslem's Stonetree`, `Ecstatic Awakener`, `Chosen of Markov` and `Skin Invasion` have a
//      blank creature BACK behind a front that prints, and `is:vanilla` on the four is 0. The token
//      rows settle it in the other direction — `is:vanilla is:dfc` is 18 there, and it holds
//      `Servo // Thopter` and `Goblin // Blood` (blank front, printing back) while leaving out
//      `Elemental // Centaur` and `Fish // Kraken` (printing front, blank back).
//
//   2. THE CREATURE TEST IS THE CARD'S, not the front face's. `City's Blessing // Elemental` and
//      `Copy // Horror` are both in that 18, and neither FRONT is a creature — the back is. So the
//      card must be a creature somewhere and its front must be silent, which is exactly the pair
//      `card_types` and `faces[0]` already hold.
//
//   3. A LAND IS NEVER VANILLA. `t:creature -o:/./ -is:vanilla` is exactly 1 there and it is
//      `Dryad Arbor`, whose land types grant `{T}: Add {G}` with nothing printed to say so.
//      `is:vanilla t:land` is 0 there with and without `include_extras`, while
//      `t:creature t:land -o:/./` is 2 — Dryad Arbor and the `Forest Dryad` token. Both candidates,
//      neither vanilla, and over the whole 540,484-row import those 2 are the only rows the clause
//      removes: a creature with no printed text produces mana only through a land type.
//
// And the text read is the printed text WITHOUT its reminder text: `Icehide Golem` ("({S} can be
// paid with one mana from a snow source.)") and `Infinity Elemental` ("(This creature has INFINITE
// POWER.)") are both vanilla there and neither prints an empty string.
//
// 352 + 12 − 1 = 363, which is Scryfall's own count — and card for card, not merely the same size:
// the full 363 was fetched in three pages and diffed against this engine's own by `oracle_id`, and
// both set differences are empty. Every field this reads is already in the archive, so nothing is
// stored for it and no format moves.

/// Whether a printed oracle text leaves nothing behind once its reminder text is removed.
///
/// A depth walk rather than a strip-and-compare: the question is only whether ANY character
/// survives outside the parentheses, so nothing needs to be built. Reminder text nests in practice
/// (`Dryad Arbor`'s parenthetical quotes an ability), which is why the depth is counted rather than
/// the first `)` taken.
///
/// This is the blankness half of the rule `o:` will want in full when the reminder-text work lands;
/// at that point this collapses onto whatever that introduces, rather than staying a second copy.
fn text_blank_after_reminders(text: &str) -> bool {
    let mut depth = 0usize;
    for c in text.chars() {
        match c {
            '(' => depth += 1,
            ')' => depth = depth.saturating_sub(1),
            _ if depth == 0 && !c.is_whitespace() => return false,
            _ => {}
        }
    }
    true
}

/// `is:vanilla` / `has:vanilla`: a creature whose FRONT face prints no rules text.
///
/// The type half is two mask bits the build already parsed off the whole type line, so it needs no
/// face walk; the text half is one face read, or the card's own text for the ~82% with no faces —
/// where the card IS its one face.
fn card_is_vanilla(card: &AOracleCard, strings: &AStrings) -> bool {
    let bits = u16::from(card.card_types);
    if bits & super::TYPE_CREATURE == 0 || bits & super::TYPE_LAND != 0 {
        return false;
    }
    let text_id = match card.faces.first() {
        None => card.oracle_text_id,
        Some(front) => front.oracle_text_id,
    };
    str_at(strings, u32::from(text_id)).is_none_or(text_blank_after_reminders)
}

#[derive(Clone, Copy)]
pub(crate) enum CollField {
    Subtypes,
    Keywords,
    OracleTags,
    ArtTags,
    IsTags,
    FrameData,
}

/// Collections are interned vocab ids (see VocabInterner). Card-level
/// collections come from the OracleCard; printing-level ones (art/is tags,
/// frame data) come from the printing — None during the card pass.
fn collection<'a>(
    card: &'a AOracleCard,
    printing: Option<&'a APrinting>,
    f: CollField,
) -> Option<&'a rkyv::vec::ArchivedVec<rkyv::rend::u16_le>> {
    match f {
        CollField::Subtypes   => Some(&card.card_subtypes),
        CollField::Keywords   => Some(&card.card_keywords),
        CollField::OracleTags => Some(&card.card_oracle_tags),
        CollField::ArtTags    => printing.map(|p| &p.card_art_tags),
        CollField::IsTags     => printing.map(|p| &p.card_is_tags),
        CollField::FrameData  => printing.map(|p| &p.card_frame_data),
    }
}

// enum_variant_names: the `Lower` suffix is load-bearing, not noise — these name the
// case/accent-folded store columns (`card_name_lower`, `oracle_text_lower`, …) that search
// actually reads, as distinct from the display columns of the same fields.
#[allow(clippy::enum_variant_names)]
#[derive(Clone, Copy, PartialEq)]
pub(crate) enum TextSearchField {
    /// `name:"…"` — the LITERAL name match, `card_name_lower` and nothing folded past its case.
    /// Measured on api.scryfall.com 2026-08-16: `name:"eowyn"` answers 0 while `name:"éowyn"`
    /// answers 3, and `name:"lim-dul"` answers 0 while `name:"lim-dûl"` answers 8 — a quoted
    /// value reaches only the spelling the searcher typed.
    NameLower,
    /// `name:word` — the BARE-word match, against `card_name_collated`: diacritics folded and
    /// every non-alphanumeric character removed. The overwhelmingly common form (a bare word in
    /// a query IS this predicate), and the one that makes `ft` answer 1,628 rather than 362 by
    /// reaching "Sword **of the** Ages" through the vanished space.
    NameCollated,
    OracleTextLower,
    FlavorTextLower,
    /// `a:"quoted"` — the value as written, still UNfolded by the parser.
    ///
    /// Kept as its own variant only because the parser distinguishes the two node shapes; both
    /// arms bind through `artist_contains_ids`, because Scryfall draws no quoted/bare line for
    /// artists the way it does for `name:` — `a:"rebeccaguay"` answers `a:rebecca-guay`'s 399.
    ArtistLower,
    /// `a:word` — the COLLATED artist (diacritics folded, every non-alphanumeric character gone),
    /// which is what Scryfall compares EVERY artist value against, bare or quoted, `:` or `=`.
    ArtistCollated,
}

/// Text operand during evaluation; PDep only in the card-level pass.
enum StrVal<'a> {
    Known(&'a str),
    Null,
    PDep,
}

fn opt_sv(v: Option<&str>) -> StrVal<'_> {
    v.map_or(StrVal::Null, StrVal::Known)
}

fn text_search_field_value<'a>(
    card: &'a AOracleCard,
    printing: Option<&'a APrinting>,
    strings: &'a AStrings,
    field: TextSearchField,
) -> StrVal<'a> {
    match field {
        // LITERAL (`name:"…"`, and a plain-literal regex lowered to one): the stored lowercase
        // name, with neither fold. The query value keeps its diacritics in Python for the same
        // reason.
        TextSearchField::NameLower       => StrVal::Known(card.card_name_lower.as_str()),
        // COLLATED (`name:word`): accent-folded (#649) AND separator-folded, the query word
        // through `collate_name(fold_accents(...))` in Python, so this must match.
        TextSearchField::NameCollated    => StrVal::Known(crate::collated_name(card, strings)),
        TextSearchField::OracleTextLower => opt_sv(str_at(strings, u32::from(card.oracle_text_lower_id))),
        TextSearchField::FlavorTextLower => printing.map_or(StrVal::PDep, |p| opt_sv(str_at(strings, u32::from(p.flavor_text_lower_id)))),
        // Rewritten to ArtistMatch by bind(); printings carry no artist strings.
        TextSearchField::ArtistLower | TextSearchField::ArtistCollated => StrVal::Null,
    }
}

/// Enum that replaces fn-pointer fields in TextExact / TextRegex.
/// Function pointers cannot be parameterized over &Card vs &ACard, so enum
/// dispatch is used instead.
#[derive(Clone, Copy)]
pub(crate) enum TextField {
    NameLower,
    OracleTextLower,
    FlavorTextLower,
    ArtistLower,
    SetCode,
    Layout,
    Border,
    Watermark,
    CollectorNumber,
}

fn text_field_value<'a>(
    card: &'a AOracleCard,
    printing: Option<&'a APrinting>,
    strings: &'a AStrings,
    field: TextField,
) -> StrVal<'a> {
    match field {
        TextField::NameLower       => StrVal::Known(card.card_name_lower.as_str()),
        TextField::OracleTextLower => opt_sv(str_at(strings, u32::from(card.oracle_text_lower_id))),
        TextField::Layout          => opt_sv(str_at(strings, u32::from(card.card_layout_id))),
        TextField::FlavorTextLower => printing.map_or(StrVal::PDep, |p| opt_sv(str_at(strings, u32::from(p.flavor_text_lower_id)))),
        // Rewritten to ArtistMatch by bind(); printings carry no artist strings.
        TextField::ArtistLower     => StrVal::Null,
        TextField::SetCode         => printing.map_or(StrVal::PDep, |p| StrVal::Known(p.card_set_code.as_str())),
        TextField::Border          => printing.map_or(StrVal::PDep, |p| opt_sv(str_at(strings, u32::from(p.card_border_id)))),
        TextField::Watermark       => printing.map_or(StrVal::PDep, |p| opt_sv(str_at(strings, u32::from(p.card_watermark_id)))),
        TextField::CollectorNumber => printing.map_or(StrVal::PDep, |p| opt_sv(str_at(strings, u32::from(p.collector_number_id)))),
    }
}

// ─── FilterExpr ───────────────────────────────────────────────────────────────

/// verify_cost_tier() and printing_dependent() match on this enum
/// exhaustively (no `_` arm), so adding a variant is a compile error until
/// it's classified in both — deliberately, since a silent default there
/// would misorder the verifier walk without failing any test.
///
/// `Clone` (#745): `explain_analyze` needs a fresh, unmutated tree for every
/// `run_query_with_plan` call — its own `prepare_candidates` mutates via
/// `memoize_text_predicates` — so it clones from a pristine snapshot per call
/// rather than reusing one filter across plans/rounds. Every field here is
/// cheaply `Clone` already (small `Vec`s, `String`, `regex::Regex` is
/// internally `Arc`-based); this is a plain derive, not a deep-copy concern.
#[derive(Clone)]
pub(crate) enum FilterExpr {
    True,
    And(Vec<FilterExpr>),
    Or(Vec<FilterExpr>),
    Not(Box<FilterExpr>),
    ExactName(String),

    NumericCmp {
        lhs: NumExpr,
        op: CmpOp,
        rhs: NumExpr,
    },

    TextContains {
        field: TextSearchField,
        word: String,
    },
    /// An artist predicate (contains/exact/regex) after bind() resolved it
    /// against the ~2.2k-entry artist vocab: sorted vocab ids whose artist
    /// string satisfies the original predicate. Matching is an integer binary
    /// search per printing instead of a string comparison.
    ArtistMatch {
        ids: Vec<u16>,
    },
    /// A flavor-text predicate (contains/exact/regex) after bind() resolved it
    /// against the ~26.3k distinct flavor texts (fingerprint-prefiltered scan):
    /// sorted global string ids whose text satisfies the predicate — matching
    /// is an integer binary search per printing — plus the dense text ids for
    /// CSR narrowing in printing space.
    FlavorMatch {
        gids: Vec<u32>,
        dense_ids: Vec<u32>,
    },
    /// A name contains-predicate after memoize_text_predicates() resolved it
    /// through the name trigram index in a full-scan query: sorted
    /// card_name_id values of the cards whose lowercase name contains the
    /// needle. Names are always Known (missing names intern as ""), so
    /// matching is a plain two-valued binary search. The ids are specific to
    /// the store the rewrite ran against — a memoized filter must not outlive
    /// that store or that query.
    NameMatch {
        ids: Vec<u32>,
    },
    /// An oracle-text contains-predicate after memoize_text_predicates()
    /// resolved it through the oracle trigram index in a full-scan query:
    /// sorted oracle_text_lower_id values whose text contains the needle.
    /// Textless cards intern "" at load (never NONE_STR), so like TextContains
    /// they evaluate False, not Null; the Null arm in tri() only mirrors the
    /// str_at() contract defensively. Store-bound, same as NameMatch.
    OracleMatch {
        gids: Vec<u32>,
    },
    TextExact {
        field: TextField,
        op: CmpOp,
        value: String,
    },
    TextRegex {
        field: TextField,
        regex: Regex,
    },

    ColorCmp {
        field: ColorField,
        op: CmpOp,
        mask: u8,
    },

    TypeCmp {
        mask: u16,
        op: CmpOp,
    },

    CollectionCmp {
        field: CollField,
        op: CmpOp,
        value: String,
        /// `value` resolved to its vocab id by bind_collection_ids(), which the
        /// query entry points call once per query before matching; None means
        /// absent from the vocab (matches no element). Matching compares ids
        /// only — never strings — so an unbound filter behaves as if the value
        /// were unknown.
        value_id: Option<u16>,
    },

    /// `lang:xx` — a printing's language equals `value` (`card_lang`, stored as
    /// `CompatFields.lang_id`). `lang:any` matches every printing: its whole effect is the one
    /// every LangMatch leaf has, widening the query to the foreign annex (the presence of this
    /// variant in a bound filter is one of the two widening triggers; `include_multilingual` is
    /// the other). Detected here, in the engine, so the flag and the operator cannot drift.
    LangMatch {
        value: String,
        /// `value` resolved to its coll_vocab id by bind(), the CollectionCmp shape exactly:
        /// None means no loaded printing carries the language, which matches nothing.
        vid: Option<u16>,
        any: bool,
    },

    /// `st:<type>` — a printing's SET TYPE equals `value` (Scryfall's `set_type`, stored as
    /// `CompatFields.set_type_id`). `LangMatch`'s shape exactly, minus the widening: both live in
    /// the compat blob rather than in a column of their own, both intern into `coll_vocab`, and
    /// both resolve to an id in `bind()` so `tri()` is one integer equality.
    ///
    /// It is the predicate five `is:` values turn out to BE — `is:masterpiece` is `st:masterpiece`
    /// exactly (measured against api.scryfall.com, both set differences empty), and `is:alchemy`
    /// and `is:funny` are their set types — so it retires a family of stored tags rather than
    /// adding one.
    SetTypeMatch {
        value: String,
        /// `value` resolved to its coll_vocab id by bind(), the LangMatch shape exactly: None
        /// means no loaded printing carries the set type, which matches nothing.
        vid: Option<u16>,
    },

    /// `is:localizedname`, and `has:printedname`, its other spelling — this printing carries a
    /// PRINTED name. One field compare (`printed_name_folded_id != NONE_STR`), because the importer
    /// already folds the printed FULL name of every face into that id and leaves it NONE_STR when
    /// no face has one; nothing is stored for this predicate and nothing is bound for it.
    ///
    /// Presence, not difference, and not "non-English" — measured against api.scryfall.com on
    /// 2026-08-16 over the whole 540,484-row bulk. 182 of the printings it matches are ENGLISH
    /// (om1/66 prints "Rhilex the Accursed" over Agent Venom); 4,468 of the foreign ones print a
    /// name IDENTICAL to the English one and still count; and it reads per-FACE, so every Japanese
    /// transform printing matches on face names with no top-level `printed_name` at all.
    /// `is:localizedname e:dsk` is 1,917 printings there against the same 1,917 in the bulk.
    ///
    /// Its presence WIDENS the query — see `widens_to_annex`, and the count that proves it.
    PrintedNamePresent,

    /// `is:flavorname` — and `has:flavorname` — a PRINTING that carries a flavor name, Scryfall's
    /// alternate SOLD-AS name (the Godzilla series, the Secret Lair crossovers). The presence twin
    /// of `FlavorNameIn`: that leaf asks whether the flavor name satisfies a `name:` needle, this
    /// one only whether there is one. Nothing is stored for it and nothing is bound.
    ///
    /// EITHER PLACE Scryfall puts the key counts. Measured against api.scryfall.com on
    /// 2026-09-01: `is:flavorname` is 476 cards / 661 printings, 646 of them carrying the
    /// top-level key and the other 15 carrying it on their FACES alone (`transform` and
    /// `reversible_card` — vow/341 is "Dracula the Voyager" // "Casket of Native Earth", sld/1807
    /// "Chucky" on the front face only), and none carrying neither. A printing-level read alone
    /// would have answered 646 and called it the whole set.
    ///
    /// Its presence WIDENS the query, exactly as `PrintedNamePresent`'s does: 6 of the 661 are
    /// Japanese rows (iko/387 ja prints "Mechagodzilla, the Weapon" over 結晶の巨人) and
    /// api.scryfall.com returns them with no `lang:` term written.
    FlavorNamePresent,

    /// A PRINTING whose `flavor_name` satisfies the `name:` predicate that produced this leaf.
    ///
    /// `name:` reaches a printing's alternate SOLD-AS name, not just its oracle name. Measured on
    /// api.scryfall.com 2026-08-16: `name:croft` answers 2 — Lara Croft, and **Command Tower**,
    /// which is 2 of 112 printings there because two of them are sold as "Croft Manor";
    /// `name:godzilla` is 8 cards / 14 printings; `!"croft manor"` is 1. It is printing-scoped in
    /// both directions: `unique=prints` returns only the printings that carry the name.
    ///
    /// `ids` are interned `CardData.strings` ids of `Printing.flavor_name_folded_id`, sorted — a
    /// printing matches iff its own id is in the set. Compiled by `bind_flavor_names` from the
    /// ~546-record `flavor_names` index, and ONLY when the needle actually hits one of those
    /// records: a `name:` query that matches no flavor name never grows this arm at all, so the
    /// hottest predicate in the language pays a bounded scan of a table two orders of magnitude
    /// smaller than the corpus and nothing else.
    FlavorNameIn {
        ids: Vec<u32>,
    },

    /// `is:unique` — the owning CARD has been printed in exactly one SET. Card-level and total, off
    /// `OracleCard.single_set`, which the build computes over the canonical printings AND the annex
    /// (`assign_single_set_flags`); nothing here to bind and nothing per printing to consult.
    ///
    /// A SET count, not a printing count: Scryfall's syntax page defines it as "cards that have
    /// only been in a single set", and the two differ on 2,847 of its own 16,318 — `!"Forest"`
    /// alone is two printings of one set. Spanning the annex is not optional either: 130 cards have
    /// exactly one English set and a second set that exists only in another language (Salvat,
    /// ps11, pmei), and api.scryfall.com calls none of the 130 unique. Reading canonical printings
    /// alone would have called all 130 unique and been wrong 130 times.
    SingleSet,

    /// `is:vanilla` — and `has:vanilla` — a creature whose FRONT face prints no rules text.
    /// Card-level and total, off fields the archive already holds; nothing to bind and nothing per
    /// printing.
    ///
    /// A PREDICATE rather than the `t:creature -o:/./` expansion it replaces, because the merged
    /// row cannot answer it: the join hides a blank front behind the half that prints. See
    /// `card_is_vanilla` for the three measured rules — the front face, the card-level creature
    /// test, and the land face that is never vanilla.
    VanillaFace,

    /// `oracleid:<uuid>` — the oracle card whose `oracle_id` equals `id` (`parse_uuid_or_hash`'s
    /// u128, 0 for an unparseable value, which no stored id ever equals). Card-level and total,
    /// with nothing for `bind()` to resolve — bind() sees the vocab tables, not `CardIndexes` —
    /// so `tri()` compares the raw u128 and `narrow_rec` seeds the same id through
    /// `oracle_by_oracle_id` for the O(log n) answer.
    OracleIdMatch {
        id: u128,
    },

    Legality {
        shift: Option<u8>, // None: format absent from all loaded data — matches nothing
        expected: u64,
    },

    ManaCostCmp {
        op: CmpOp,
        /// Single-symbol pip counts of the query cost, packed into the same
        /// 8-bit lanes as ManaCost.core (see the packed-pip-lanes section).
        core: u64,
        /// The query's hybrid '/' symbols as (symbol, count), sorted; kept as
        /// strings so bind() can resolve them against the store's mana vocab.
        hybrids: Vec<(String, u8)>,
        /// `hybrids` resolved to sorted (mana_vocab id, count) by bind().
        /// Symbols absent from the vocab — which no card can carry — merge
        /// into the reserved MANA_SYM_UNKNOWN id, preserving exact match
        /// semantics. Built all-unknown, so an unbound filter behaves as if
        /// every hybrid symbol were unknown (mirroring CollectionCmp).
        hybrid_ids: Vec<(u8, u8)>,
        /// Each mana-vocab id's CMC CONTRIBUTION, indexed by id, resolved by bind() — 2 for a
        /// TWOBRID (`{2/W}`), 1 for every other hybrid.
        ///
        /// Generic is `cmc - (what the pips account for)`, and a twobrid accounts for TWO. Without
        /// this the subtraction credits it with one, and the shortfall becomes generic the card
        /// does not have. Measured on api.scryfall.com 2026-08-17 — Beseech the Queen,
        /// `{2/B}{2/B}{2/B}`, cmc 6, three pips: the true generic is 0, this read 6 - 3 = 3, and
        /// `!"Beseech the Queen" m>={3}` answered the card where Scryfall answers nothing.
        /// Corpus-wide, `m:{2/w} m:{2}` was 16 against Scryfall's 0.
        ///
        /// Empty until bind(), which is the same all-unknown posture `hybrid_ids` takes; a missing
        /// id falls back to 1, the weight of every non-twobrid symbol.
        hybrid_cmc: Vec<u8>,
        cmc: f32,
    },

    Devotion {
        op: CmpOp,
        /// Queried WUBRGC devotion counts in the low six 8-bit lanes,
        /// hybrid query pips expanded at build — same layout as
        /// ManaCost.devotion, so every comparison is lane arithmetic.
        pips: u64,
        /// Each mana-vocab id's DEVOTION COLOUR MASK, indexed by id, resolved by bind(): the
        /// lanes a pip of that symbol counts toward. `R/G` sets red and green; `2/W` and `W/P`
        /// set white alone, since neither `2` nor `P` is a colour.
        ///
        /// Devotion is a count of PIPS, and `ManaCost.devotion` stores per-colour lanes with
        /// hybrids expanded — so summing the queried lanes counts a `{R/G}` pip once for red and
        /// again for green when both are queried. This table is what lets the sum be corrected
        /// back to distinct pips. Empty until bind(); a missing id contributes no colours, which
        /// costs a correction rather than inventing one.
        hybrid_colors: Vec<u8>,
    },

    DateCmp {
        op: CmpOp,
        value: u32, // yyyymmdd, partial dates zero-padded (e.g. "2026-07" → 20260700)
    },

    YearCmp {
        op: CmpOp,
        year: i32,
    },
}

/// Verifier per-candidate cost estimates, in hundredths of a nanosecond
/// (ns * 100 — e.g. 1.83 ns -> 183) so sub-nanosecond gaps between measured
/// ops stay representable as plain integers, and adding or recalibrating one
/// op is a one-line constant edit instead of a renumbering of its neighbors
/// (#651 forced exactly that churn on the previous 0..4 ordinal scheme).
///
/// Measured on the real corpus (`bench_verify_cost.rs`, `cargo test --release
/// bench_verify_cost -- --ignored --nocapture`, 31,508 oracle cards, min-of-50
/// per kernel, 3 repeated runs — see that file for the per-op numbers):
///
/// - field loads and integer/float/mask compares (TypeCmp, ColorCmp,
///   NumericCmp, ExactName, TextExact, Legality, DateCmp, YearCmp): 2.0-3.8 ns
///   measured (2026-07 re-run: Legality 2.05, YearCmp/DateCmp 2.3-2.6,
///   ColorCmp 2.20, ExactName/TextExact 2.57, NumericCmp 3.16-3.83). NumericCmp
///   is the priciest member (NumExpr::eval() indirection on both sides costs
///   more than a direct field load), so the constant sits just above it.
///   Was 600 — pinned to a stale 5.6 ns NumericCmp measurement; the recalibrated
///   ceiling (~3.8) fixes StreamedSelect over-pricing a mask-compare residual
///   ~1.6x (e.g. `f:legacy or year:2020` mis-routing to compose, #731).
pub(crate) const MASK_COMPARE_NS100: u32 = 400;
/// - bounded lookups: a binary search over a bind/memoize-resolved id set
///   (ArtistMatch/FlavorMatch/NameMatch/OracleMatch), a card collection
///   (CollectionCmp), and anchored-literal regexes (a memcmp at a known
///   position — see regex_tier): 1.8-8.1 ns measured. Devotion/ManaCostCmp
///   (#651, bench_mana.rs) measure below this range (0.65-2 ns) but share the
///   constant deliberately — see their arm below.
pub(crate) const SET_LOOKUP_NS100: u32 = 900;
/// - per-candidate text scans: unmemoized TextContains: 21.6-22.7 ns measured.
pub(crate) const TEXT_SCAN_NS100: u32 = 2_300;
/// - regex without a usable anchor: bare literal and general machinery
///   measured statistically identical (~44-49 ns) once compared on equal
///   footing (both carrying the (?i) every query regex has) — the regex
///   crate's literal-prefix optimization doesn't meaningfully beat a full
///   scan for an *unanchored* pattern. This corrects the previous assumption
///   that bare-literal costs the same as TextContains (it measures ~2x more).
///   An anchored non-literal pattern (e.g. `^[aeiou]`) measured far cheaper
///   (~17.7 ns, anchoring bounds the scan regardless of what's being tested)
///   but regex_tier() doesn't distinguish that case from general machinery —
///   left as a known conservative overestimate, not fixed here (would need a
///   regex_tier() classification change, not just a constant recalibration).
pub(crate) const REGEX_MACHINERY_NS100: u32 = 5_000;
/// - fancy-regex lookarounds / backrefs / conditionals: measured 89–430×
///   REGEX_MACHINERY on the text corpus for PR-907-shaped patterns
///   (`bench_regex_backtrack_tier`, 2026-08; e.g. `draw (?!two)` ~2.1 µs/card vs
///   `draw .* cards?` ~24 ns/card). Deliberately dwarfs machinery so And reordering
///   runs cheap predicates first; `(?=.*…)` shapes can cost more still — the tier
///   prices ordering, not worst-case plan latency.
pub(crate) const REGEX_BACKTRACK_NS100: u32 = 380_000;

/// Per-candidate verification cost of a node in the tri walk. Composites take
/// the max of their children: their short-circuit may have to evaluate every
/// child, so the most expensive child bounds the cost.
/// `verify_cost_tier` over an `And` whose `proven` children are skipped, matching what `card_pass` will
/// actually evaluate (see `Narrowed::proven`).
///
/// Without this the model keeps charging the tier of a conjunct nobody verifies: `o:this border:black`
/// read `TEXT_SCAN` (23 ns/card) when the surviving residual is a `TextExact` at `MASK_COMPARE` (4 ns),
/// and both plans under-predicted by 2.0-2.6x. A cost model that does not see a change cannot route on it.
pub(crate) fn verify_cost_tier_unproven(f: &FilterExpr, proven: u64) -> u32 {
    match f {
        FilterExpr::And(children) if proven != 0 => children
            .iter()
            .enumerate()
            .filter(|(i, _)| *i >= 64 || proven & (1 << i) == 0)
            .map(|(_, c)| verify_cost_tier(c))
            .max()
            .unwrap_or(0),
        _ => verify_cost_tier(f),
    }
}

pub(crate) fn verify_cost_tier(f: &FilterExpr) -> u32 {
    match f {
        FilterExpr::TextRegex { regex, .. } => regex_tier(regex.as_str()),
        FilterExpr::TextContains { .. } => TEXT_SCAN_NS100,
        // Two mask bits reject all but the creatures, and the survivors walk one string: the FRONT
        // face's printed text, or the card's own. That walk is a scan, so it is ranked as one — the
        // model must not under-charge a predicate on the strength of the branch it usually takes.
        FilterExpr::VanillaFace => TEXT_SCAN_NS100,
        FilterExpr::Devotion { .. } | FilterExpr::ManaCostCmp { .. } => SET_LOOKUP_NS100,
        FilterExpr::ArtistMatch { .. }
        | FilterExpr::FlavorMatch { .. }
        | FilterExpr::NameMatch { .. }
        // A binary search over the compiled flavor-name ids, against a u32 already on the printing.
        | FilterExpr::FlavorNameIn { .. }
        | FilterExpr::OracleMatch { .. }
        | FilterExpr::CollectionCmp { .. } => SET_LOOKUP_NS100,
        FilterExpr::And(children) | FilterExpr::Or(children) => {
            children.iter().map(verify_cost_tier).max().unwrap_or(0)
        }
        FilterExpr::Not(inner) => verify_cost_tier(inner),
        // Exhaustive, not `_ => MASK_COMPARE_NS100`: a new variant must get a
        // considered cost here rather than silently inheriting the cheapest.
        FilterExpr::True
        | FilterExpr::ExactName(_)
        | FilterExpr::NumericCmp { .. }
        | FilterExpr::TextExact { .. }
        | FilterExpr::ColorCmp { .. }
        | FilterExpr::TypeCmp { .. }
        | FilterExpr::Legality { .. }
        // A LangMatch is one integer equality against a resolved vocab id.
        | FilterExpr::LangMatch { .. }
        // ...and a SetTypeMatch is the same equality against a different id in the same vocab.
        | FilterExpr::SetTypeMatch { .. }
        // A PrintedNamePresent is one u32 compare against a field already on the printing, a
        // FlavorNamePresent the same compare plus a walk of the printing's few faces, and a
        // SingleSet one bool read off a field already on the card.
        | FilterExpr::PrintedNamePresent
        | FilterExpr::FlavorNamePresent
        | FilterExpr::SingleSet
        // An OracleIdMatch is one 128-bit integer equality against a field already in the card.
        | FilterExpr::OracleIdMatch { .. }
        | FilterExpr::DateCmp { .. }
        | FilterExpr::YearCmp { .. } => MASK_COMPARE_NS100,
    }
}

/// Classify a regex pattern's per-candidate cost by shape. The regex crate
/// compiles literal-only patterns to memcmp-style matchers (with case
/// folding for the (?i) every query regex carries), and anchors bound the
/// scan to one position — measured on the real corpus, `^flying$` costs
/// ~half a substring scan while an unanchored literal costs about the same
/// as one. Ranking them as general regexes inverted real costs and made
/// `o:/^flying$/ oracle:sacrifice` 2.4× slower, so:
///
///   SET_LOOKUP_NS100    — literal with a ^ or $ anchor (starts_with/
///                         ends_with/equality; memcmp at a known position)
///   REGEX_MACHINERY_NS100 — everything else: bare literal (measured the
///                         same cost as live metacharacters, not the same as
///                         TextContains — see REGEX_MACHINERY_NS100's doc)
///   REGEX_BACKTRACK_NS100 — lookarounds and other fancy-regex backtracking
///                         features (see bench_regex_backtrack_tier)
pub(crate) fn regex_tier(pattern: &str) -> u32 {
    let p = pattern.strip_prefix("(?i)").unwrap_or(pattern);
    if pattern_requires_backtrack(p) {
        return REGEX_BACKTRACK_NS100;
    }
    let mut p = p;
    let anchored_start = p.starts_with('^');
    if anchored_start {
        p = &p[1..];
    }
    let bytes = p.as_bytes();
    let mut anchored_end = false;
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            // An escape of punctuation (\{ \. \$) is a literal character; an
            // alphanumeric escape (\d \w \b \p…) is a class — real machinery.
            b'\\' => match bytes.get(i + 1) {
                Some(c) if !c.is_ascii_alphanumeric() => i += 2,
                _ => return REGEX_MACHINERY_NS100,
            },
            b'$' if i == bytes.len() - 1 => {
                anchored_end = true;
                i += 1;
            }
            b'.' | b'*' | b'+' | b'?' | b'(' | b')' | b'[' | b']' | b'{' | b'}' | b'|' | b'^' | b'$' => return REGEX_MACHINERY_NS100,
            _ => i += 1,
        }
    }
    if anchored_start || anchored_end { SET_LOOKUP_NS100 } else { REGEX_MACHINERY_NS100 }
}

/// True when *pattern* needs fancy-regex's backtracking VM (lookarounds, etc.).
pub(crate) fn pattern_requires_backtrack(pattern: &str) -> bool {
    const LOOKAROUNDS: &[&str] = &["(?=", "(?!", "(?<=", "(?<!"];
    let bytes = pattern.as_bytes();
    let mut in_class = false;
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'[' => in_class = true,
            b']' if in_class => in_class = false,
            b'(' if !in_class && i + 1 < bytes.len() && bytes[i + 1] == b'?' => {
                let rest = &pattern[i..];
                if LOOKAROUNDS.iter().any(|tok| rest.starts_with(tok)) {
                    return true;
                }
                if rest.starts_with("(?>") || rest.starts_with("(?(") {
                    return true;
                }
            }
            b'\\' if !in_class && i + 1 < bytes.len() => {
                let nxt = bytes[i + 1];
                if (b'1'..=b'9').contains(&nxt) || matches!(nxt, b'g' | b'G' | b'k' | b'K') {
                    return true;
                }
                i += 1;
            }
            _ if !in_class && pattern[i..].starts_with("(?P=") => return true,
            _ => {}
        }
        i += 1;
    }
    false
}

/// Whether a node can NEVER settle the card-level pass — it compares only
/// printing-level fields, so at card level it always returns PrintingDep and
/// its evaluation there is pure deferral. Ordering such children after the
/// card-level ones is a free win in both And and Or: they cannot reject an
/// And or accept an Or at card level, so a card-level sibling that settles
/// first skips their eval entirely, and nothing is lost when it doesn't.
///
/// Composites settle at card level when ANY child can (a card-level False
/// settles an And, a card-level True settles an Or), so a composite is
/// printing-dependent only when ALL its children are.
fn printing_dependent(f: &FilterExpr) -> bool {
    match f {
        FilterExpr::And(children) | FilterExpr::Or(children) => children.iter().all(printing_dependent),
        FilterExpr::Not(inner) => printing_dependent(inner),
        leaf => leaf_compares_printing_field(leaf),
    }
}

/// Whether `f` compares a printing-level field **anywhere** — the `any` composition of the same leaf
/// table `printing_dependent` reads with `all`. The two questions are different and both are wanted:
///
/// - `printing_dependent` asks "can this node never settle at card level", for verify ORDERING. A
///   composite settles when ANY child can, so it is printing-dependent only when ALL children are.
/// - this asks "could any part of this need a per-printing answer", for the result-total ESTIMATE. A
///   card-invariant residual returns `True`/`False` per card and never `PrintingDep`, so each candidate
///   contributes either its whole printing span or none of it — a different estimator shape from one where
///   printings under a single card disagree.
///
/// `name:s AND usd>10` separates them: not `printing_dependent` (the name settles it), but it does touch a
/// printing field, so the span of a matching card is not all-or-nothing.
pub(crate) fn touches_printing_field(f: &FilterExpr) -> bool {
    match f {
        FilterExpr::And(children) | FilterExpr::Or(children) => children.iter().any(touches_printing_field),
        FilterExpr::Not(inner) => touches_printing_field(inner),
        leaf => leaf_compares_printing_field(leaf),
    }
}

/// The per-leaf half of both questions above: does this NON-composite node compare a printing-level
/// field. Composition is the callers' business, which is the whole reason this is factored out — the two
/// callers disagree on it and must not disagree on the table.
fn leaf_compares_printing_field(f: &FilterExpr) -> bool {
    fn num_pdep(e: &NumExpr) -> bool {
        match e {
            NumExpr::Const(_) => false,
            // Exhaustive over NumField, not `matches!` with a hidden `_ =>
            // false`: a new field must get a considered answer here rather
            // than silently inheriting "card-level".
            NumExpr::Field(field) => match field {
                NumField::RarityInt
                | NumField::CollectorNumberInt
                | NumField::PriceUsd
                | NumField::PriceEur
                | NumField::PriceTix
                | NumField::PreferScore => true,
                NumField::Cmc | NumField::Power | NumField::Toughness | NumField::Loyalty | NumField::EdhrEc => false,
            },
            NumExpr::Arith(lhs, _, rhs) => num_pdep(lhs) || num_pdep(rhs),
        }
    }
    match f {
        FilterExpr::NumericCmp { lhs, rhs, .. } => num_pdep(lhs) || num_pdep(rhs),
        FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => true,
        FilterExpr::ArtistMatch { .. } | FilterExpr::FlavorMatch { .. } => true,
        // Exhaustive over TextSearchField (no `matches!`), same reason as num_pdep.
        FilterExpr::TextContains { field, .. } => match field {
            TextSearchField::FlavorTextLower => true,
            TextSearchField::NameLower
            | TextSearchField::NameCollated
            | TextSearchField::OracleTextLower
            | TextSearchField::ArtistLower
            | TextSearchField::ArtistCollated => false,
        },
        // Exhaustive over TextField (no `matches!`), same reason as num_pdep.
        FilterExpr::TextExact { field, .. } | FilterExpr::TextRegex { field, .. } => match field {
            TextField::FlavorTextLower | TextField::SetCode | TextField::Border | TextField::Watermark | TextField::CollectorNumber => {
                true
            }
            TextField::NameLower | TextField::OracleTextLower | TextField::ArtistLower | TextField::Layout => false,
        },
        // Exhaustive over CollField (no `matches!`), same reason as num_pdep.
        FilterExpr::CollectionCmp { field, .. } => match field {
            CollField::ArtTags | CollField::IsTags | CollField::FrameData => true,
            CollField::Subtypes | CollField::Keywords | CollField::OracleTags => false,
        },
        // Divergent-legality cards defer to the printing, but they are a rare
        // exception (non-tournament reprints); rank by the common card-level case.
        FilterExpr::Legality { .. } => false,
        // The language is a per-printing fact (CompatFields.lang_id).
        FilterExpr::LangMatch { .. } => true,
        // The set type is the PRINTING's set, so it can only settle once one is in hand.
        FilterExpr::SetTypeMatch { .. } => true,
        // The printed name is a per-printing fact (Printing.printed_name_folded_id) — an English
        // row and its Japanese sibling answer differently.
        FilterExpr::PrintedNamePresent => true,
        // A flavor name is printed on the PRINTING; two printings of one card differ on it, which
        // is the whole reason `name:croft` returns 2 of Command Tower's 112 — and the same reason
        // `is:flavorname` matches Command Tower's sld/1864 row and none of its other 111.
        FilterExpr::FlavorNameIn { .. } | FilterExpr::FlavorNamePresent => true,
        // Composites are composed by the two callers, which differ on `all` vs `any`; reaching here with
        // one is a bug in whichever caller forgot to handle it, not a case to answer silently.
        FilterExpr::And(_) | FilterExpr::Or(_) | FilterExpr::Not(_) => {
            unreachable!("composites are composed by printing_dependent / touches_printing_field")
        }
        // Exhaustive, not `_ => false`: a new variant must get a considered
        // answer here rather than silently inheriting "can settle at card level".
        FilterExpr::True
        | FilterExpr::ExactName(_)
        | FilterExpr::NameMatch { .. }
        | FilterExpr::OracleMatch { .. }
        // The oracle id is the card's own identity — every printing of it shares one.
        | FilterExpr::OracleIdMatch { .. }
        // How many SETS the card has been printed in is the card's fact too, decided at build over
        // every printing of it; no printing can change the answer.
        | FilterExpr::SingleSet
        // Faces and their texts are oracle data — every printing of the card prints the same ones.
        | FilterExpr::VanillaFace
        | FilterExpr::ColorCmp { .. }
        | FilterExpr::TypeCmp { .. }
        | FilterExpr::ManaCostCmp { .. }
        | FilterExpr::Devotion { .. } => false,
    }
}

/// Or-child sort key. An Or short-circuits on acceptance, and acceptance
/// rates — unlike costs — are unknowable statically, so ordering an Or by
/// fine-grained cost backfires when a cheap child rarely accepts (measured
/// twice: `oracle:vigilance or devotion:bbb` lost 1.2× to devotion-first,
/// and a memoized name set jumping a contains lost 1.1×). The key therefore
/// only separates classes with a decisive gap:
///
///   bucket 0 — card-level tier-0 checks: cheap enough (a few ns) that
///              leading with them is near-free even when they rarely accept
///   bucket 1 — everything else below regex machinery (set lookups, pip
///              maps, text scans) in written order: costs within ~3× of
///              each other, where acceptance dominates
///   bucket 2 — regex machinery, always last
///
/// Within a bucket, printing-dependent children order last: they can never
/// settle the Or at card level (see printing_dependent), so leading with
/// them is pure deferral cost.
fn or_child_key(f: &FilterExpr) -> (u8, bool) {
    let tier = verify_cost_tier(f);
    let pdep = printing_dependent(f);
    let bucket = if tier >= REGEX_MACHINERY_NS100 {
        2
    } else if tier == MASK_COMPARE_NS100 && !pdep {
        0
    } else {
        1
    };
    (bucket, pdep)
}

/// Within-tier refinement for And children: memoized sets know their own
/// size, and under an And a smaller set is more selective — it rejects more
/// candidates per (identical) binary-search cost, so it should run first.
/// Nodes without a known set size sort after sized ones in their tier and
/// keep written order among themselves (the sort is stable).
fn and_child_set_len(f: &FilterExpr) -> usize {
    match f {
        FilterExpr::ArtistMatch { ids } => ids.len(),
        FilterExpr::NameMatch { ids } => ids.len(),
        FilterExpr::FlavorMatch { gids, .. } | FilterExpr::OracleMatch { gids } => gids.len(),
        _ => usize::MAX,
    }
}

/// Reserved ManaCost hybrid id for query symbols absent from the store's
/// mana vocab: no card carries it, so containment fails and exactness fails
/// against any card, exactly like a HashMap key nothing else holds. Distinct
/// unknown symbols merge into one entry — safe for the same reason.
pub(crate) const MANA_SYM_UNKNOWN: u8 = u8::MAX;

type AHybrids = rkyv::Archived<Vec<(u8, u8)>>;

/// Resolve the query's hybrid symbols to sorted (mana_vocab id, count).
fn bind_mana_hybrids(hybrids: &[(String, u8)], mana_vocab: &AStrings) -> Vec<(u8, u8)> {
    let mut out = Vec::with_capacity(hybrids.len());
    let mut unknown = 0u8;
    for (sym, n) in hybrids {
        // Linear scan: the vocab is ~29 entries and queries carry 0-2 hybrids.
        match mana_vocab.iter().position(|v| v.as_str() == sym.as_str()) {
            Some(i) => out.push((i as u8, *n)),
            None => unknown = unknown.saturating_add(*n),
        }
    }
    out.sort_unstable();
    if unknown > 0 {
        out.push((MANA_SYM_UNKNOWN, unknown)); // sorts last: real ids are < 255
    }
    out
}

fn hybrid_count(card: &AHybrids, id: u8) -> u8 {
    card.iter().find(|e| e.0 == id).map_or(0, |e| e.1)
}

/// Every query hybrid is contained in the card's (query ⊆ card).
fn hybrids_ge(card: &AHybrids, query: &[(u8, u8)]) -> bool {
    query.iter().all(|&(id, n)| hybrid_count(card, id) >= n)
}

/// Every card hybrid is contained in the query's (card ⊆ query).
fn hybrids_le(card: &AHybrids, query: &[(u8, u8)]) -> bool {
    card.iter().all(|e| query.iter().find(|q| q.0 == e.0).map_or(0, |q| q.1) >= e.1)
}

/// Same hybrid multiset — both sides sorted, so pairwise equality suffices.
fn hybrids_eq(card: &AHybrids, query: &[(u8, u8)]) -> bool {
    card.len() == query.len() && card.iter().zip(query).all(|(c, q)| c.0 == q.0 && c.1 == q.1)
}

/// GENERIC mana — the `{2}` in `{2}{R}` — as a COUNTED quantity, recovered from a cost's cmc and
/// its pips rather than stored.
///
/// It is not in `core`: `mana_pip_counts` drops numeric symbols on purpose (they are not pips and
/// have no lane), so a cost's generic used to survive only inside `cmc`, and comparing THAT is a
/// measurably different question. `cmc - (what the pips account for)` is the generic exactly, for a
/// query cost and a card cost alike — which is why the query side needs no new field and the store
/// needs no new column.
///
/// WHAT A PIP ACCOUNTS FOR IS NOT ALWAYS 1. Lane 7 is X (see `MANA_LANE_SYMS`) and contributes 0,
/// so it is excluded from the subtraction rather than credited; every other single symbol
/// contributes 1; and a TWOBRID contributes 2, which is why `hybrid_cmc` exists and why hybrids
/// arrive here as (vocab id, count) rather than as a bare count. Beseech the Queen is the case
/// that named it: `{2/B}{2/B}{2/B}` at cmc 6 read as 6 - 3 = 3 generic when every pip weighed 1,
/// and `m>={3}` answered a card whose generic is 0. Saturating and clamped at 0, so a weight this
/// table cannot supply degrades to 0 instead of wrapping.
fn generic_of(core: u64, hybrids: impl Iterator<Item = (u8, u8)>, cmc: f32, hybrid_cmc: &[u8]) -> u8 {
    // Lanes 0..6 only: lane 7 is X, which is a real pip and contributes 0 to cmc, so subtracting
    // it would invent generic. Every other single symbol contributes exactly 1.
    let core_pips: u32 = (0..7).map(|l| u32::from(lane_get(core, l))).sum();
    // A hybrid contributes its OWN weight, which is 2 for a twobrid and 1 for the rest — see
    // `hybrid_cmc`. Unknown ids weigh 1, the common case and the safe one.
    let hybrid_pips: u32 = hybrids
        .map(|(id, n)| u32::from(hybrid_cmc.get(id as usize).copied().unwrap_or(1)) * u32::from(n))
        .sum();
    let cmc = if cmc > 0.0 { cmc as u32 } else { 0 };
    u8::try_from(cmc.saturating_sub(core_pips + hybrid_pips)).unwrap_or(u8::MAX)
}

/// A mana symbol's contribution to converted mana cost: 2 for a TWOBRID (`2/W`), 1 otherwise.
///
/// Scryfall's cmc counts a twobrid as two — `{2/B}{2/B}{2/B}` is cmc 6, not 3 — while every other
/// hybrid (`W/U`, `W/P`, `B/G/P`) counts one. The vocab interns the symbol without its braces, so
/// the leading component is what decides it.
fn hybrid_cmc_weight(sym: &str) -> u8 {
    sym.split('/').next().and_then(|head| head.parse::<u8>().ok()).unwrap_or(1)
}

/// The devotion lanes a pip of `sym` counts toward, as a bitmask over WUBRGC.
///
/// `R/G` sets red and green — a hybrid pip is devotion to BOTH its colours. `2/W` and `W/P` set
/// white alone: neither `2` nor `P` is a colour, and Phyrexian mana is devotion to its one colour.
fn devotion_color_mask(sym: &str) -> u8 {
    sym.split('/')
        .filter_map(mana_lane)
        .filter(|&lane| lane < 6)
        .fold(0u8, |mask, lane| mask | (1u8 << lane))
}

/// Interned name ids (ascending, deduplicated) of the `flavor_names` records satisfying `pred`.
///
/// `pred` sees the record's COLLATED name, which is the form both `name:` predicates compare in.
fn flavor_name_ids(
    idx: &rkyv::Archived<PrintedNameIndex>,
    collated: &AStrings,
    pred: impl Fn(&str) -> bool,
) -> Vec<u32> {
    let mut ids: Vec<u32> = collated
        .iter()
        .enumerate()
        .filter(|(_, s)| pred(s.as_str()))
        .map(|(rec, _)| u32::from(idx.name_ids[rec]))
        .collect();
    ids.sort_unstable();
    ids.dedup();
    ids
}

/// Vocab ids (ascending) whose artist string satisfies `pred`.
fn artist_match_ids(artist_vocab: &AStrings, pred: impl Fn(&str) -> bool) -> Vec<u16> {
    artist_vocab
        .iter()
        .enumerate()
        .filter(|(_, s)| pred(s.as_str()))
        .map(|(i, _)| i as u16)
        .collect()
}

/// Vocab ids (ascending) whose artist CONTAINS `needle`, collated on both sides.
///
/// THE ONE COMPARISON EVERY ARTIST PREDICATE MAKES. On api.scryfall.com there is no `a:` / `a=`
/// distinction and no quoted / bare distinction — measured 2026-08-16, every pair answers the same
/// number:
///
///   a:"rebecca guay" 399   a="rebecca guay" 399   a:rebecca-guay 399   a=rebecca-guay 399
///   a:gaweł           23   a=gaweł           23   a:gawel         23   a="gawel"       23
///
/// and it is a CONTAINS rather than an equality: `a="rebecca"` answers 405 exactly as `a:rebecca`
/// does, and `a="guay"` answers 462 exactly as `a:guay` does. This port had `a=` as a full-string
/// compare against the unfolded vocab, so `a="greg hildebrandt"` answered 0 where Scryfall answers
/// 6, and a quoted `a:"…"` stayed literal, so `a:"rebeccaguay"` answered 0 against Scryfall's 399.
///
/// The needle arrives accent-folded ONLY when the parser built a CollatedNameValueNode (a bare
/// `a:` word); the quoted and `=` forms keep their spelling. Rather than teach the engine a second
/// copy of `fold_accents`, a NON-ASCII needle is compared against the unfolded vocab collated on
/// the fly as well as the stored folded one — the union is exactly Scryfall's behaviour, which
/// answers 23 for `gaweł` and `gawel` alike. An ASCII needle skips that pass entirely and cannot
/// need it: folding only ever maps non-ASCII to ASCII, so the stored folded vocab is already the
/// more permissive target. That keeps the common path allocation-free, as it was.
fn artist_contains_ids(artist_vocab: &AStrings, artist_vocab_collated: &AStrings, needle: &str) -> Vec<u16> {
    let collated = crate::collate_name(needle);
    // memmem::Finder built once, reused across the vocab scan — its SIMD prefilter beats
    // rebuilding str::contains's searcher per entry (~1.3x, bench_substring_finders). #734.
    let finder = memmem::Finder::new(collated.as_bytes());
    let also_unfolded = !collated.is_ascii();
    artist_vocab_collated
        .iter()
        .enumerate()
        .filter(|(vid, folded)| {
            finder.find(folded.as_str().as_bytes()).is_some()
                || (also_unfolded
                    && finder.find(crate::collate_name(artist_vocab[*vid].as_str()).as_bytes()).is_some())
        })
        .map(|(vid, _)| vid as u16)
        .collect()
}

impl FilterExpr {
    /// Per-query binding against the store's vocab tables, called once before
    /// matching. Two rewrites happen here:
    ///
    /// - CollectionCmp values resolve to their vocab id (binary search over the
    ///   string-sorted permutation — ~14 string compares per term); a value
    ///   absent from the vocab resolves to None and can match no element.
    /// - Artist predicates (contains/exact/regex on ArtistLower) evaluate once
    ///   against the ~2.2k distinct artist strings and become ArtistMatch nodes
    ///   holding the sorted ids that satisfied them — per-printing matching is
    ///   then an integer membership test, and narrow_candidates can expand the
    ///   ids through the artist CSR index.
    /// - Flavor predicates get the same treatment against the ~26.3k distinct
    ///   flavor texts (FlavorMatch), with a fingerprint prefilter skipping
    ///   texts that cannot contain the needle (see FLAVOR_FP_FEATURES).
    ///
    /// Name/oracle-text contains predicates are deliberately NOT rewritten
    /// here: their rewrite is only profitable when the query full-scans, which
    /// isn't known until run_query computes candidates — see
    /// memoize_text_predicates().
    /// Grow the FLAVOR-NAME arm of a `name:` predicate — but only when a flavor name answers it.
    ///
    /// Scryfall's `name:` reads a printing's alternate SOLD-AS name as well as its oracle name:
    /// `name:croft` answers 2 there, because Command Tower is sold as "Croft Manor" on 2 of its 112
    /// printings; `name:godzilla` is 8 cards / 14 printings; `!"croft manor"` is 1.
    ///
    /// THE COST ARGUMENT IS THE DESIGN. Answering that unconditionally would make `name:` — the
    /// most common predicate in the language and the one the perf gate holds to <3% of a full scan
    /// — printing-dependent for every query, which costs the card-level settle on all of them. So
    /// the needle is put to the ~546-record `flavor_names` table FIRST, against the pre-collated
    /// strings beside it, and the arm is added only on a hit. A needle that matches nothing there
    /// leaves the tree byte-identical to what it was, which is the overwhelmingly common case; the
    /// scan itself is one `memmem::Finder` over ~9 KB of short strings, built once per predicate.
    ///
    /// Both name predicates that Scryfall reaches flavor names through are handled — the bare
    /// `name:word` (collated on both sides) and `!"…"` (collated, and matching either half of a
    /// `A // B` name, exactly as the oracle-name arm does).
    pub(crate) fn bind_flavor_names(&mut self, idx: &rkyv::Archived<PrintedNameIndex>, collated: &AStrings) {
        match self {
            FilterExpr::And(children) | FilterExpr::Or(children) => {
                for c in children {
                    c.bind_flavor_names(idx, collated);
                }
            }
            FilterExpr::Not(inner) => inner.bind_flavor_names(idx, collated),
            FilterExpr::TextContains { field: TextSearchField::NameCollated, word } => {
                let finder = memmem::Finder::new(word.as_bytes());
                let ids = flavor_name_ids(idx, collated, |s| finder.find(s.as_bytes()).is_some());
                if !ids.is_empty() {
                    let original = std::mem::replace(self, FilterExpr::True);
                    *self = FilterExpr::Or(vec![original, FilterExpr::FlavorNameIn { ids }]);
                }
            }
            FilterExpr::ExactName(needle) => {
                let needle = needle.clone();
                let ids = flavor_name_ids(idx, collated, |s| exact_name_matches(s, &needle));
                if !ids.is_empty() {
                    let original = std::mem::replace(self, FilterExpr::True);
                    *self = FilterExpr::Or(vec![original, FilterExpr::FlavorNameIn { ids }]);
                }
            }
            _ => {}
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn bind(
        &mut self,
        vocab: &AStrings,
        artist_vocab: &AStrings,
        // artist_vocab_collated: the same artists, `collate_name(fold_accents(...))` — the string
        // `a:word` matches against (see TextSearchField::ArtistCollated).
        artist_vocab_collated: &AStrings,
        mana_vocab: &AStrings,
        flavor: &rkyv::Archived<FlavorIndex>,
        strings: &AStrings,
    ) {
        match self {
            FilterExpr::And(children) | FilterExpr::Or(children) => {
                for c in children {
                    c.bind(vocab, artist_vocab, artist_vocab_collated, mana_vocab, flavor, strings);
                }
            }
            FilterExpr::Not(inner) => inner.bind(vocab, artist_vocab, artist_vocab_collated, mana_vocab, flavor, strings),
            // UNCONDITIONAL, unlike the other bind arms: the weights are read off the CARD's
            // hybrids, not the query's, so `m:{2}` against a twobrid card needs them even though
            // the query carries no hybrid symbol at all. Gating this on `!hybrids.is_empty()` —
            // which is right for `hybrid_ids` — would have left the commonest twobrid query
            // unweighted.
            FilterExpr::ManaCostCmp { hybrids, hybrid_ids, hybrid_cmc, .. } => {
                if !hybrids.is_empty() {
                    *hybrid_ids = bind_mana_hybrids(hybrids, mana_vocab);
                }
                *hybrid_cmc = mana_vocab.iter().map(|s| hybrid_cmc_weight(s.as_str())).collect();
            }
            // Like ManaCostCmp's, UNCONDITIONAL: the masks are read off the CARD's hybrids, so a
            // query naming no hybrid at all still needs them to correct its own sum.
            FilterExpr::Devotion { hybrid_colors, .. } => {
                *hybrid_colors = mana_vocab.iter().map(|s| devotion_color_mask(s.as_str())).collect();
            }
            FilterExpr::CollectionCmp { value, value_id, .. } => {
                // The vocab is renumbered into lexicographic order at load, so ids ARE the sorted
                // order and the permutation this used to search through is gone.
                let i = vocab.partition_point(|entry| entry.as_str() < value.as_str());
                *value_id = u16::try_from(i)
                    .ok()
                    .filter(|&id| vocab.get(id as usize).is_some_and(|e| e.as_str() == value.as_str()));
            }
            // The language lives in the same vocab the collection values do (CompatFields.lang_id
            // interns into coll_vocab), so this is CollectionCmp's resolution verbatim.
            FilterExpr::LangMatch { value, vid, any: false } => {
                let i = vocab.partition_point(|entry| entry.as_str() < value.as_str());
                *vid = u16::try_from(i)
                    .ok()
                    .filter(|&id| vocab.get(id as usize).is_some_and(|e| e.as_str() == value.as_str()));
            }
            // The set type interns into that same vocab (CompatFields.set_type_id), so this is
            // the resolution above verbatim.
            FilterExpr::SetTypeMatch { value, vid } => {
                let i = vocab.partition_point(|entry| entry.as_str() < value.as_str());
                *vid = u16::try_from(i)
                    .ok()
                    .filter(|&id| vocab.get(id as usize).is_some_and(|e| e.as_str() == value.as_str()));
            }
            FilterExpr::TextContains { field: TextSearchField::ArtistLower, word } => {
                // A QUOTED `a:"…"` reaches this arm, and it is collated too — Scryfall draws no
                // quoted/bare line for artists, unlike `name:`. See `artist_contains_ids`.
                let ids = artist_contains_ids(artist_vocab, artist_vocab_collated, word.as_str());
                *self = FilterExpr::ArtistMatch { ids };
            }
            FilterExpr::TextContains { field: TextSearchField::ArtistCollated, word } => {
                // A BARE `a:word`, already folded and collated by the parser. Collating an
                // already-collated needle is idempotent, so it shares the one comparison.
                let ids = artist_contains_ids(artist_vocab, artist_vocab_collated, word.as_str());
                *self = FilterExpr::ArtistMatch { ids };
            }
            FilterExpr::TextExact { field: TextField::ArtistLower, op, value } => {
                let (op, value) = (*op, std::mem::take(value));
                // `a=` IS `a:` on Scryfall — a contains, not an equality (see
                // `artist_contains_ids` for the measurements). The ordering comparisons keep the
                // full-string compare against the unfolded vocab: Scryfall answers 0 for every one
                // of them (`a>"rebecca guay"` measured 2026-08-16), so there is no behaviour there
                // to match, and `a!=` already agrees with it at 0 — changing either would be
                // inventing semantics rather than reproducing them.
                let ids = if matches!(op, CmpOp::Eq) {
                    artist_contains_ids(artist_vocab, artist_vocab_collated, &value)
                } else {
                    artist_match_ids(artist_vocab, |s| match op {
                        CmpOp::Eq => s == value,
                        CmpOp::Ne => s != value,
                        CmpOp::Lt => s < value.as_str(),
                        CmpOp::Le => s <= value.as_str(),
                        CmpOp::Gt => s > value.as_str(),
                        CmpOp::Ge => s >= value.as_str(),
                    })
                };
                *self = FilterExpr::ArtistMatch { ids };
            }
            FilterExpr::TextRegex { field: TextField::ArtistLower, regex } => {
                let ids = artist_match_ids(artist_vocab, |s| regex_is_match(regex, s));
                *self = FilterExpr::ArtistMatch { ids };
            }
            FilterExpr::TextContains { field: TextSearchField::FlavorTextLower, word } => {
                let mask = flavor_fingerprint(word.as_str());
                let finder = memmem::Finder::new(word.as_bytes()); // built once, reused (see ArtistLower)
                let (gids, dense_ids) = flavor_match_sets(flavor, strings, mask, |s| finder.find(s.as_bytes()).is_some());
                *self = FilterExpr::FlavorMatch { gids, dense_ids };
            }
            FilterExpr::TextExact { field: TextField::FlavorTextLower, op, value } => {
                let (op, value) = (*op, std::mem::take(value));
                // Equality implies containment, so Eq can use the fingerprint;
                // the other comparisons carry no containment implication.
                let mask = if op == CmpOp::Eq { flavor_fingerprint(value.as_str()) } else { 0 };
                let (gids, dense_ids) = flavor_match_sets(flavor, strings, mask, |s| match op {
                    CmpOp::Eq => s == value,
                    CmpOp::Ne => s != value,
                    CmpOp::Lt => s < value.as_str(),
                    CmpOp::Le => s <= value.as_str(),
                    CmpOp::Gt => s > value.as_str(),
                    CmpOp::Ge => s >= value.as_str(),
                });
                *self = FilterExpr::FlavorMatch { gids, dense_ids };
            }
            FilterExpr::TextRegex { field: TextField::FlavorTextLower, regex } => {
                let (gids, dense_ids) = flavor_match_sets(flavor, strings, 0, |s| regex_is_match(regex, s));
                *self = FilterExpr::FlavorMatch { gids, dense_ids };
            }
            _ => {}
        }
    }

    /// Memoize indexable text predicates in a query the driver is about to
    /// evaluate against every card (#624) — the third instance of the
    /// ArtistMatch/FlavorMatch pattern. Name/oracle contains-nodes resolve
    /// through their trigram indexes: gather candidates (bounded by the
    /// needle's rarest trigram), verify each with the real contains() once,
    /// and rewrite to a sorted match-id set whose per-card evaluation is an
    /// integer binary search instead of a substring search.
    ///
    /// Only called when the query has no candidates (no postings narrowing
    /// and no plane bitmap): with candidates, the driver evaluates only those
    /// cards and the bind-time verify would mostly be wasted work. Needles
    /// under 3 bytes have no trigrams and keep the scan; needles whose
    /// candidates exceed half the corpus stay unrewritten too — at that
    /// density a binary search costs about what contains() does, so the
    /// verify pass couldn't earn its keep.
    /// Cost-based memoization gate, measured (bench_memo_crossover.py, six
    /// needles spanning 493-11,933 candidate texts × eight candidate-domain
    /// sizes, memoize-always vs memoize-never builds): the bind cost breaks
    /// even when the evaluation domain reaches ~1.25× the needle's shortest
    /// trigram posting list. The factor here is 2 — declining early forgoes a
    /// small win, declining late pays on every query — with a floor below
    /// which the whole effect sits inside measurement noise (scaled down for
    /// tiny stores so tests and partial imports still exercise the rewrite).
    fn memoize_pays(bind_bound: usize, eval_domain: usize, n_rows: usize) -> bool {
        const MEMO_DOMAIN_FACTOR: usize = 2;
        const MEMO_DOMAIN_FLOOR: usize = 2_048;
        eval_domain >= (bind_bound * MEMO_DOMAIN_FACTOR).max(MEMO_DOMAIN_FLOOR.min(n_rows / 4))
    }

    pub(crate) fn memoize_text_predicates(
        &mut self,
        cards: &[AOracleCard],
        strings: &AStrings,
        name_trigram: &rkyv::Archived<SortedTrigramIndex>,
        name_bigrams: &rkyv::Archived<NameBigramIndex>,
        oracle: &rkyv::Archived<OracleTextIndex>,
        eval_domain: usize,
    ) {
        match self {
            FilterExpr::And(children) | FilterExpr::Or(children) => {
                for c in children.iter_mut() {
                    c.memoize_text_predicates(cards, strings, name_trigram, name_bigrams, oracle, eval_domain);
                }
            }
            FilterExpr::Not(inner) => inner.memoize_text_predicates(cards, strings, name_trigram, name_bigrams, oracle, eval_domain),
            FilterExpr::TextContains { field: field @ (TextSearchField::NameLower | TextSearchField::NameCollated), word } => {
                // BOTH name predicates narrow through the SAME collated tiers, because both
                // indexes are built over `card_name_collated`. The COLLATED predicate is answered
                // by them; the LITERAL one only gets its candidates there and re-verifies against
                // the name as written.
                //
                // Narrowing the literal predicate through a collated index is sound in the one
                // direction it is used: deleting the same character class from both sides
                // preserves containment, so a name containing `word` literally contains
                // `collate_name(word)` collated, and the collated tier can only ever be a
                // SUPERSET. `name:"of the"` narrows through `ofthe` and is then checked against
                // the space. An all-punctuation needle collates to nothing and has no tier at
                // all, so it declines to the walk rather than narrowing to everything.
                // A NON-ASCII literal needle declines to narrow at all. The index is built over
                // the ACCENT-FOLDED collated name, and folding is not a deletion — "éowyn" has no
                // window in common with the stored "eowynladyofrohan", so narrowing would drop
                // the very card `name:"éowyn"` names (measured: 3 on api.scryfall.com). The
                // separator argument below survives folding only for ASCII, which is why the two
                // guards are separate.
                let literal = *field == TextSearchField::NameLower;
                let collated = if literal { crate::collate_name(word) } else { word.clone() };
                if collated.is_empty() || (literal && !word.is_ascii()) {
                    return;
                }
                // Verifies against the string THIS predicate compares, not the one the index is
                // built from — the whole point of the split.
                let finder = memmem::Finder::new(word.as_bytes()); // built once, reused across the verify scan
                let verify = |cid: u32| -> bool {
                    let card = &cards[cid as usize];
                    let hay = if literal { card.card_name_lower.as_str() } else { crate::collated_name(card, strings) };
                    finder.find(hay.as_bytes()).is_some()
                };

                let cand: Vec<u32> = if collated.len() == 2 {
                    // 2-byte needles resolve exactly through the bigram index: containment IS
                    // bigram membership over the indexed string, so the collated predicate skips
                    // verification entirely and the ids just re-key to card_name_id for eval.
                    if u32::from(name_bigrams.n_cards) as usize != cards.len() {
                        return;
                    }
                    let bg = [collated.as_bytes()[0], collated.as_bytes()[1]];
                    let bind_bound = name_bigrams.postings.get(&bg).map_or_else(
                        || name_bigrams.plane_of.get(&bg).map_or(0, |_| cards.len() / 8),
                        |v| v.len(),
                    );
                    if !Self::memoize_pays(bind_bound, eval_domain, cards.len()) {
                        return;
                    }
                    if let Some(p) = name_bigrams.plane_of.get(&bg) {
                        let wpp = cards.len().div_ceil(64);
                        let start = u32::from(*p) as usize * wpp;
                        let mut out = Vec::new();
                        for (i, w) in name_bigrams.plane_words[start..start + wpp].iter().enumerate() {
                            let mut w = u64::from(*w);
                            while w != 0 {
                                out.push(((i as u32) << 6) | w.trailing_zeros());
                                w &= w - 1;
                            }
                        }
                        out
                    } else {
                        name_bigrams.postings.get(&bg).map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(u16::from(*x))).collect())
                    }
                } else {
                    // The intersection is bounded by the shortest posting list, so
                    // checking that bound first makes the decline path free — no
                    // gather, no intersection. Declining when only the *bound*
                    // (not the exact count) exceeds half the corpus is deliberate:
                    // it can only happen when every trigram of the needle is
                    // ultra-common, where the match set is broad anyway.
                    match trigram_min_posting(name_trigram, &collated) {
                        Some(min) if min <= cards.len() / 2 && Self::memoize_pays(min, eval_domain, cards.len()) => {}
                        _ => return,
                    }
                    let Some(cand) = trigram_candidates(name_trigram, &collated) else { return };
                    cand
                };

                let exact_tier = !literal && collated.len() == 2;
                let mut ids: Vec<u32> = cand
                    .into_iter()
                    .filter(|&cid| exact_tier || verify(cid))
                    .map(|cid| u32::from(cards[cid as usize].card_name_id))
                    .collect();
                ids.sort_unstable();
                ids.dedup();
                *self = FilterExpr::NameMatch { ids };
            }
            FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word } => {
                match trigram_min_posting(&oracle.trigrams, word) {
                    Some(min) if min <= oracle.gids.len() / 2 && Self::memoize_pays(min, eval_domain, cards.len()) => {}
                    _ => return,
                }
                let Some(dense) = trigram_candidates(&oracle.trigrams, word) else { return };
                let finder = memmem::Finder::new(word.as_bytes()); // built once, reused across the verify scan
                let mut gids: Vec<u32> = Vec::with_capacity(dense.len());
                for d in dense {
                    let gid = u32::from(oracle.gids[d as usize]);
                    if str_at(strings, gid).is_some_and(|s| finder.find(s.as_bytes()).is_some()) {
                        gids.push(gid);
                    }
                }
                gids.sort_unstable();
                *self = FilterExpr::OracleMatch { gids };
            }
            _ => {}
        }
    }

    /// Reorder And/Or children cheapest-verification-first so the tri walk's
    /// short-circuit (first False settles an And, first True settles an Or)
    /// runs the expensive text predicates on as few cards as possible. The tri
    /// accumulation is commutative — False/True dominate and the Null /
    /// PrintingDep flags just OR together — so any child order is
    /// semantics-preserving; only the cost changes. Without this, whether a
    /// broad scan pays a regex before or after the cheap mask checks depends
    /// on how the user typed the query.
    ///
    /// And children sort card-level-first (a printing-dependent child cannot
    /// reject at card level, so any card-level sibling that rejects first
    /// skips its eval — free, never negative), then on the full cost tiers,
    /// refined within the memoized-set tier to ascending set size: a smaller
    /// match set rejects more candidates per unit cost, and the size is
    /// already known (ids.len()). Or children sort on the coarser
    /// or_child_key — their short-circuit is acceptance, which no static
    /// cost model can see, so only decisive cost gaps reorder them.
    ///
    /// The sorts are stable, so equal-cost children keep written order and
    /// the result is deterministic. Must run after memoize_text_predicates():
    /// memoization flips TextContains nodes from the scan tier to the set
    /// tier. The per-printing residual pass inherits the order too, since
    /// card_pass() pushes residual children in child order.
    /// `proven` is a bitmask over THIS node's `And` children (see `Narrowed::proven`) and is permuted in
    /// step with them. Carrying it through rather than invalidating it matters: this reorder runs on every
    /// query with a residual, so a mask dropped here would never survive to its consumer. Nested nodes get
    /// no mask — only the outermost `And`'s is ever read.
    pub(crate) fn order_children_by_verify_cost(&mut self, proven: &mut u64) {
        match self {
            FilterExpr::And(children) => {
                for c in children.iter_mut() {
                    c.order_children_by_verify_cost(&mut 0);
                }
                let key = |c: &FilterExpr| (printing_dependent(c), verify_cost_tier(c), and_child_set_len(c));
                if *proven == 0 {
                    children.sort_by_key(key);
                    return;
                }
                // Sort an index permutation instead of the children, so the mask can be rebuilt against
                // the new positions. `sort_by_key` is stable, so this produces the same order as the
                // unmasked path above.
                let mut order: Vec<usize> = (0..children.len()).collect();
                order.sort_by_key(|&i| key(&children[i]));
                let mut moved: Vec<Option<FilterExpr>> = std::mem::take(children).into_iter().map(Some).collect();
                let mut remapped = 0u64;
                for (new_i, &old_i) in order.iter().enumerate() {
                    if new_i < 64 && old_i < 64 && *proven & (1 << old_i) != 0 {
                        remapped |= 1 << new_i;
                    }
                    children.push(moved[old_i].take().expect("each index appears once in a permutation"));
                }
                *proven = remapped;
            }
            FilterExpr::Or(children) => {
                for c in children.iter_mut() {
                    c.order_children_by_verify_cost(&mut 0);
                }
                children.sort_by_key(or_child_key);
            }
            FilterExpr::Not(inner) => inner.order_children_by_verify_cost(&mut 0),
            _ => {}
        }
    }

    /// True iff the filter matches this (card, printing) pair. With a printing
    /// supplied, evaluation is exact — PrintingDep cannot occur. The query
    /// driver goes through card_pass()/residual_matches() instead; this is the
    /// unfactored single-pair form, kept for tests.
    #[cfg(test)]
    pub(crate) fn matches(&self, card: &AOracleCard, printing: &APrinting, strings: &AStrings) -> bool {
        self.tri(card, Some(printing), strings) == Tri::True
    }

    /// Card-level pass: evaluate with no printing. True means every printing of
    /// the card matches; False/Null mean none can; PrintingDep means the result
    /// depends on printing-level fields. The query driver uses card_pass()
    /// (which adds residual extraction); this is the plain form, kept for tests.
    #[cfg(test)]
    pub(crate) fn eval_card(&self, card: &AOracleCard, strings: &AStrings) -> Tri {
        self.tri(card, None, strings)
    }

    /// Printing-level pass, the plain form, kept for tests: the same evaluation the residual walk
    /// performs, with a printing in hand. `eval_card`'s twin — the one-printing question a
    /// printing-scoped leaf (set code, watermark, set type) is the only way to ask directly.
    #[cfg(test)]
    pub(crate) fn eval_printing(&self, card: &AOracleCard, printing: &APrinting, strings: &AStrings) -> Tri {
        self.tri(card, Some(printing), strings)
    }

    /// Card pass with one-level residual extraction. For a top-level And/Or,
    /// children are classified individually: decided children are dropped (a
    /// False/Null child settles an And, a True child settles an Or — and at the
    /// top level only True counts as a match, so an And with a Null child can
    /// never match and collapses to False), and only the PrintingDep children
    /// go into `residual` for the per-printing walk. This is what makes
    /// broad-card × narrow-printing conjunctions cheap: `t:creature set:lea`
    /// proves the type check once per card and walks printings evaluating only
    /// the set check. `residual` is a caller-owned buffer reused across cards;
    /// `residual_is_or` says how residual_matches() must combine it.
    ///
    /// Returns True (every printing matches), False (none can), or PrintingDep
    /// (evaluate the residual per printing). Never returns Null: at the top
    /// level Null cannot become a match, so it collapses to False.
    pub(crate) fn card_pass<'f>(
        &'f self,
        card: &AOracleCard,
        strings: &AStrings,
        residual: &mut Vec<&'f FilterExpr>,
        residual_is_or: &mut bool,
        proven: u64,
    ) -> Tri {
        residual.clear();
        *residual_is_or = false;
        match self {
            FilterExpr::And(children) => {
                for (i, c) in children.iter().enumerate() {
                    // Membership in the candidate set already settles this conjunct for this card, so
                    // evaluating it can only return `True` — see `Narrowed::proven` for why that is sound
                    // and for what it costs when it is not done. Skipping it is exactly the `all_match`
                    // shortcut at conjunct granularity: not in the residual either, since the printings
                    // of a card cannot disagree about a card-space fact.
                    if i < 64 && proven & (1 << i) != 0 {
                        continue;
                    }
                    match c.tri(card, None, strings) {
                        // And(Null, x) is Null or False for every printing —
                        // never True — so the card cannot match.
                        Tri::False | Tri::Null => return Tri::False,
                        Tri::True => {}
                        Tri::PrintingDep => residual.push(c),
                    }
                }
                if residual.is_empty() { Tri::True } else { Tri::PrintingDep }
            }
            FilterExpr::Or(children) => {
                *residual_is_or = true;
                for c in children {
                    match c.tri(card, None, strings) {
                        Tri::True => {
                            residual.clear();
                            return Tri::True;
                        }
                        // Or(Null, x) is True iff x is True: Null children
                        // cannot contribute a match and drop out.
                        Tri::False | Tri::Null => {}
                        Tri::PrintingDep => residual.push(c),
                    }
                }
                if residual.is_empty() { Tri::False } else { Tri::PrintingDep }
            }
            other => match other.tri(card, None, strings) {
                Tri::PrintingDep => {
                    residual.push(self);
                    Tri::PrintingDep
                }
                Tri::True => Tri::True,
                Tri::False | Tri::Null => Tri::False,
            },
        }
    }

    /// Evaluate a card_pass() residual against one printing. Only True counts
    /// as a match at the top level, so And-residuals need every child True and
    /// Or-residuals need any child True.
    pub(crate) fn residual_matches(
        card: &AOracleCard,
        printing: &APrinting,
        strings: &AStrings,
        residual: &[&FilterExpr],
        residual_is_or: bool,
    ) -> bool {
        if REGEX_MATCH_FAILED.with(|c| c.get()) {
            return false;
        }
        if residual_is_or {
            residual.iter().any(|c| c.tri(card, Some(printing), strings) == Tri::True)
        } else {
            residual.iter().all(|c| c.tri(card, Some(printing), strings) == Tri::True)
        }
    }

    /// True iff any leaf of this (bound) filter can only be answered over the ANNEX — one of the
    /// two triggers that send a query to the widened (multilingual) driver instead of
    /// `run_query_routed`. Detected here, on the compiled tree, so the operators and the
    /// `include_multilingual` flag cannot widen differently.
    ///
    /// Three leaves qualify. `LangMatch` is the obvious one. `PrintedNamePresent` is the second,
    /// and it is not a design choice — it is Scryfall's measured behaviour: `is:localizedname`
    /// with no `lang:` term in sight answers 31,294 cards there, and `&unique=prints` shows the
    /// rows it returns are German, French, Japanese… A canonical-only reading would answer 182
    /// (the English printings that carry a printed name) and call it the whole set.
    /// `FlavorNamePresent` is the third, on the same measurement: `is:flavorname&unique=prints`
    /// is 661 rows there and 6 of them are Japanese (2026-09-01).
    pub(crate) fn widens_to_annex(&self) -> bool {
        match self {
            FilterExpr::LangMatch { .. } | FilterExpr::PrintedNamePresent | FilterExpr::FlavorNamePresent => true,
            FilterExpr::And(children) | FilterExpr::Or(children) => children.iter().any(Self::widens_to_annex),
            FilterExpr::Not(inner) => inner.widens_to_annex(),
            _ => false,
        }
    }

    /// A language every match MUST carry, when the filter pins one: a `LangMatch` that is the
    /// whole filter or a direct conjunct of a top-level `And`. Conjuncts only — under `Or` or
    /// `Not` a language constrains nothing on its own, and several conjuncts can only tighten,
    /// so answering with the FIRST is a sound (superset) narrowing either way. `lang:any`
    /// requires nothing.
    pub(crate) fn required_lang_value(&self) -> Option<&str> {
        fn leaf(f: &FilterExpr) -> Option<&str> {
            match f {
                FilterExpr::LangMatch { value, any: false, .. } => Some(value.as_str()),
                _ => None,
            }
        }
        match self {
            FilterExpr::And(children) => children.iter().find_map(leaf),
            other => leaf(other),
        }
    }

    /// This filter with every `LangMatch` leaf replaced by `True`: the query's scope, minus the
    /// language it asks for.
    ///
    /// `run_query_widened` needs it to answer "which CANONICAL rows would this query have
    /// matched?" for a card whose only matching rows are foreign — the question that decides which
    /// foreign row represents the card (see `annex_representative`). Relaxing to `True` rather
    /// than deleting the leaf keeps the tree's shape, so a `LangMatch` under `Not` or `Or`
    /// contributes exactly what a satisfied conjunct would and no arm changes arity.
    ///
    /// Not a narrowing helper and never used as one: this loosens the filter, so it may only be
    /// asked about rows already known to be in scope.
    pub(crate) fn with_lang_relaxed(&self) -> FilterExpr {
        match self {
            FilterExpr::LangMatch { .. } => FilterExpr::True,
            FilterExpr::And(children) => FilterExpr::And(children.iter().map(Self::with_lang_relaxed).collect()),
            FilterExpr::Or(children) => FilterExpr::Or(children.iter().map(Self::with_lang_relaxed).collect()),
            FilterExpr::Not(inner) => FilterExpr::Not(Box::new(inner.with_lang_relaxed())),
            other => other.clone(),
        }
    }

    /// Four-valued evaluation. True/False/Null mirror SQL ternary logic: Null is
    /// SQL's NULL ("unknown"), produced when a compared field is missing from the
    /// card, and NOT/AND/OR propagate it exactly like SQL — so -power>2 excludes
    /// powerless cards (NOT NULL = NULL) while -(power>2 and t:creature) still
    /// matches instants (NULL AND false = false, NOT false = true). Only True
    /// counts as a match.
    ///
    /// PrintingDep is the card-pass "depends on the printing" value: it behaves
    /// like an unknown that per-printing evaluation can still resolve either way,
    /// so it survives NOT and is only absorbed by a dominant exact value (AND
    /// with a False, OR with a True). Null stays senior to PrintingDep in AND/OR
    /// only via those dominance rules — when both occur the result is
    /// conservatively PrintingDep and the per-printing pass settles it.
    fn tri(&self, card: &AOracleCard, printing: Option<&APrinting>, strings: &AStrings) -> Tri {
        match self {
            FilterExpr::True => Tri::True,

            FilterExpr::And(children) => {
                let mut null = false;
                let mut pdep = false;
                for c in children {
                    match c.tri(card, printing, strings) {
                        Tri::False => return Tri::False,
                        Tri::Null => null = true,
                        Tri::PrintingDep => pdep = true,
                        Tri::True => {}
                    }
                }
                if pdep { Tri::PrintingDep } else if null { Tri::Null } else { Tri::True }
            }
            FilterExpr::Or(children) => {
                let mut null = false;
                let mut pdep = false;
                for c in children {
                    match c.tri(card, printing, strings) {
                        Tri::True => return Tri::True,
                        Tri::Null => null = true,
                        Tri::PrintingDep => pdep = true,
                        Tri::False => {}
                    }
                }
                if pdep { Tri::PrintingDep } else if null { Tri::Null } else { Tri::False }
            }
            FilterExpr::Not(inner) => match inner.tri(card, printing, strings) {
                Tri::True => Tri::False,
                Tri::False => Tri::True,
                Tri::Null => Tri::Null,
                Tri::PrintingDep => Tri::PrintingDep,
            },

            FilterExpr::ExactName(lower) => tri_bool(exact_name_matches(card.card_name_folded.as_str(), lower)),

            FilterExpr::NumericCmp { lhs, op, rhs } => {
                let base = numeric_cmp_tri(lhs, *op, rhs, &|f| field_num(card, printing, f));
                // The merged row answers for 82% of cards (no faces) and for every row that
                // already matched, so the per-face cross product is reached only by a multi-face
                // card the card-level values did not satisfy — see face_numeric_cmp_tri.
                if base == Tri::True || card.faces.is_empty() {
                    base
                } else {
                    face_numeric_cmp_tri(card, printing, lhs, *op, rhs, base)
                }
            }

            FilterExpr::TextContains { field, word } => {
                match text_search_field_value(card, printing, strings, *field) {
                    StrVal::Known(s) => tri_bool(s.contains(word.as_str())),
                    StrVal::Null => Tri::Null,
                    StrVal::PDep => Tri::PrintingDep,
                }
            }

            FilterExpr::ArtistMatch { ids } => {
                let Some(p) = printing else { return Tri::PrintingDep };
                let vid = u16::from(p.card_artist_vid);
                if vid == ARTIST_NONE {
                    Tri::Null // no artist: SQL NULL, like the missing-string case before
                } else {
                    tri_bool(ids.binary_search(&vid).is_ok())
                }
            }

            // NONE_STR is Scryfall having omitted `printed_name` on every face, which is a real
            // False (this printing has no printed name) and not an SQL NULL — unlike the interned
            // scalars above, absence here IS the answer the predicate asks about.
            FilterExpr::PrintedNamePresent => {
                let Some(p) = printing else { return Tri::PrintingDep };
                tri_bool(p.printed_name_folded_id != super::NONE_STR)
            }

            FilterExpr::FlavorNameIn { ids } => {
                let Some(p) = printing else { return Tri::PrintingDep };
                let id = u32::from(p.flavor_name_folded_id);
                tri_bool(id != super::NONE_STR && ids.binary_search(&id).is_ok())
            }

            // Presence in EITHER place Scryfall puts the key: the printing's own top-level
            // `flavor_name`, or a face's (`PrintingFace.flavor_name_id`, which the 15 transform /
            // reversible printings carry INSTEAD of the top-level one — never both). NONE_STR on
            // both is a real False, not an SQL NULL, for the same reason as PrintedNamePresent.
            FilterExpr::FlavorNamePresent => {
                let Some(p) = printing else { return Tri::PrintingDep };
                tri_bool(
                    u32::from(p.flavor_name_id) != super::NONE_STR
                        || p.faces.iter().any(|f| u32::from(f.flavor_name_id) != super::NONE_STR),
                )
            }

            FilterExpr::SingleSet => tri_bool(card.single_set),

            // Two-valued: a card either has a silent creature front or it does not, and a card with
            // no text at all interns "" rather than NONE_STR, so absence is never an SQL NULL here.
            FilterExpr::VanillaFace => tri_bool(card_is_vanilla(card, strings)),

            FilterExpr::SetTypeMatch { vid, .. } => {
                let Some(p) = printing else { return Tri::PrintingDep };
                if u16::from(p.compat.set_type_id) == super::VOCAB_NONE {
                    Tri::Null // no set type recorded: SQL NULL, like the missing-string cases above
                } else {
                    // `vid` None = the set type exists on no loaded printing; matches nothing.
                    tri_bool(vid.is_some_and(|v| u16::from(p.compat.set_type_id) == v))
                }
            }

            FilterExpr::LangMatch { vid, any, .. } => {
                // `lang:any` is True for every printing — its whole effect is the widening its
                // presence triggers, so as a predicate it must reject nothing.
                if *any {
                    return Tri::True;
                }
                let Some(p) = printing else { return Tri::PrintingDep };
                if u16::from(p.compat.lang_id) == super::VOCAB_NONE {
                    Tri::Null // no lang recorded: SQL NULL, like the missing-string cases above
                } else {
                    // `vid` None = the language exists on no loaded printing; matches nothing.
                    tri_bool(vid.is_some_and(|v| u16::from(p.compat.lang_id) == v))
                }
            }

            // Two-valued, never Null: a stored oracle_id is never 0 (build enforces it), and
            // parse_uuid_or_hash's 0 for an unparseable value therefore rejects every card —
            // the same answer the oracle_by_oracle_id path gives, which refuses id 0 outright.
            FilterExpr::OracleIdMatch { id } => tri_bool(u128::from(card.oracle_id) == *id),

            FilterExpr::FlavorMatch { gids, .. } => {
                let Some(p) = printing else { return Tri::PrintingDep };
                let gid = u32::from(p.flavor_text_lower_id);
                if gid == NONE_STR {
                    Tri::Null // no flavor text: SQL NULL, matching the pre-bind semantics
                } else {
                    tri_bool(gids.binary_search(&gid).is_ok())
                }
            }

            // Names are always present (TextContains on NameLower is always
            // Known), so membership is two-valued: an id absent from `ids`
            // means the name didn't contain the needle, exactly like
            // contains() on the inline string.
            FilterExpr::NameMatch { ids } => tri_bool(ids.binary_search(&u32::from(card.card_name_id)).is_ok()),

            FilterExpr::OracleMatch { gids } => {
                let gid = u32::from(card.oracle_text_lower_id);
                if gid == NONE_STR {
                    // Unreachable for loaded cards (missing text interns "" —
                    // contains() on it is False, and so is a binary-search
                    // miss); kept to mirror str_at()'s NONE_STR → None
                    // contract, which TextContains maps to Null via opt_sv.
                    Tri::Null
                } else {
                    tri_bool(gids.binary_search(&gid).is_ok())
                }
            }

            FilterExpr::TextExact { field, op, value } => {
                match text_field_value(card, printing, strings, *field) {
                    StrVal::Known(s) => tri_bool(match op {
                        CmpOp::Eq => s == value,
                        CmpOp::Ne => s != value,
                        CmpOp::Lt => s < value.as_str(),
                        CmpOp::Le => s <= value.as_str(),
                        CmpOp::Gt => s > value.as_str(),
                        CmpOp::Ge => s >= value.as_str(),
                    }),
                    StrVal::Null => Tri::Null,
                    StrVal::PDep => Tri::PrintingDep,
                }
            }

            FilterExpr::TextRegex { field, regex } => {
                match text_field_value(card, printing, strings, *field) {
                    StrVal::Known(s) => tri_bool(regex_is_match(regex, s)),
                    StrVal::Null => Tri::Null,
                    StrVal::PDep => Tri::PrintingDep,
                }
            }

            FilterExpr::ColorCmp { field, op, mask } => {
                // Existential over the faces' own masks — see `face_color_masks`. The card-level
                // mask answers for the 82% of cards with no faces and for every card-level column,
                // where `face_color_masks` declines and this is exactly the pre-gen-28 line.
                tri_bool(match face_color_masks(card, *field) {
                    Some(masks) => masks.into_iter().any(|bits| color_cmp(bits, *op, *mask)),
                    None => color_cmp(card_colors(card, *field), *op, *mask),
                })
            }

            FilterExpr::TypeCmp { mask, op } => {
                let bits = u16::from(card.card_types);
                tri_bool(match op {
                    CmpOp::Ge => bits & mask != 0,
                    CmpOp::Eq => bits == *mask,
                    CmpOp::Le => bits & !mask == 0,
                    CmpOp::Lt => bits & !mask == 0 && bits != *mask,
                    CmpOp::Gt => bits & mask != 0 && bits != *mask,
                    CmpOp::Ne => bits != *mask,
                })
            }

            FilterExpr::CollectionCmp { field, op, value_id, .. } => {
                // Set-containment semantics against the single-value query {value},
                // mirroring the SQL path's jsonb operators (@>, <@, =, <> and the
                // strict variants). Lt (proper subset of a one-element set) can only
                // be the empty collection; Ne is not-exactly-equal, NOT "lacks value"
                // (that's what negation is for).
                //
                // Ids only: bind_collection_ids() resolved the value up front, and
                // vocab ids are unique per string, so id equality is string equality.
                let Some(coll) = collection(card, printing, *field) else {
                    return Tri::PrintingDep; // printing-level collection during the card pass
                };
                let contains = || match (*value_id, *field) {
                    (None, _) => false,
                    // card_subtypes keeps the printed order, so it is not id-sorted.
                    (Some(id), CollField::Subtypes) => coll.iter().any(|x| u16::from(*x) == id),
                    // The set-like collections are sorted by id at load, and since the vocab is
                    // renumbered lexicographically that is also alphabetical order.
                    (Some(id), _) => coll.binary_search(&id.into()).is_ok(),
                };
                let all_equal = || match *value_id {
                    None => coll.is_empty(),
                    Some(id) => coll.iter().all(|x| u16::from(*x) == id),
                };
                tri_bool(match op {
                    CmpOp::Ge => contains(),
                    CmpOp::Eq => coll.len() == 1 && contains(),
                    CmpOp::Gt => contains() && coll.len() > 1,
                    CmpOp::Le => all_equal(),
                    CmpOp::Lt => coll.is_empty(),
                    CmpOp::Ne => !(coll.len() == 1 && contains()),
                })
            }

            FilterExpr::Legality { shift, expected } => {
                let Some(shift) = shift else { return Tri::False }; // format absent from all data
                // The card-level word is exact unless this card's printings carry
                // divergent legalities (non-tournament printings: 30A, Collectors'
                // Edition, gold border) — then defer to each printing's own word.
                let word = if card.legality_divergent {
                    match printing {
                        Some(p) => u64::from(p.card_legalities),
                        None => return Tri::PrintingDep,
                    }
                } else {
                    u64::from(card.card_legalities)
                };
                tri_bool((word >> shift) & 0b11 == *expected)
            }

            FilterExpr::ManaCostCmp { op, core, hybrid_ids, hybrid_cmc, cmc, .. } => {
                // Containment/equality over the pip multiset = the same test
                // per lane (SWAR, all eight at once) and per hybrid entry
                // (sorted-slice walks; both sides empty on ~97% of cards).
                //
                // Existential over the faces on top of that, for the same measured reason the
                // numeric columns are : `m:{R}` matches Valki // Tibalt on the BACK's
                // {5}{B}{R}, and `m={1}{R}` matches Fire // Ice on one half's cost rather than
                // the card's joined "{1}{R} // {1}{U}" (whose cmc is 4, so `eq` could never
                // hold). The card-level cost is tried first and is the whole answer for the 82%
                // of cards with no faces; a face that printed NO cost has no `mana_cost` and is
                // skipped, which is why `m=0` still does not match Delver's costless back.
                // GENERIC MANA IS A COUNTED PIP, NOT A CMC. `{2}` in a query cost means "at least
                // two GENERIC", not "cmc at least 2" — comparing cmc let every colored pip pay
                // for it, so `m:{2}` matched a card costing {R}{R}.
                //
                // Measured on api.scryfall.com 2026-08-16, `e:khm t:creature` (151) unless noted:
                //
                //   m:{2}          102   this answered 142 (= cmc >= 2)
                //   m:{1}          140   this answered 151
                //   m:{3}           60   this answered 113
                //   m:{1}{1}       102   generic SUMS across symbols — the same query as m:{2}
                //   m:{2}{r}        17   this answered 20
                //   m:{1}{1}{r}     17   again the same query
                //   m:{2} -m:{1}     0   >= and not ==, so {2} implies {1}
                //
                // and the decisive one, on the whole corpus rather than on KHM, because KHM has no
                // creature costing exactly {R}{R}: `m={r}{r} t:creature` is 24 there, and
                // `m={r}{r} m:{2} t:creature` is **0** where this answered all 24. A cost of
                // {R}{R} has cmc 2 and generic 0; only one of those two readings can be right, and
                // Scryfall's is the pip.
                //
                // The cmc comparisons are GONE rather than kept alongside: once generic and every
                // pip lane compare in the same direction, cmc's does too (cmc is their sum), so
                // keeping it would be redundant on the Ge/Le/Eq paths and would re-admit exactly
                // the cards this excludes.
                let q_generic = generic_of(*core, hybrid_ids.iter().copied(), *cmc, hybrid_cmc);
                let matches = |mc: &Archived<ManaCost>| {
                    let card_core = u64::from(mc.core);
                    let c_generic = generic_of(card_core, mc.hybrids.iter().map(|e| (e.0, e.1)), f32::from(mc.cmc), hybrid_cmc);
                    let ge = || lanes_ge(card_core, *core, LANES8_HI) && hybrids_ge(&mc.hybrids, hybrid_ids) && c_generic >= q_generic;
                    let le = || lanes_ge(*core, card_core, LANES8_HI) && hybrids_le(&mc.hybrids, hybrid_ids) && c_generic <= q_generic;
                    let eq = || c_generic == q_generic && card_core == *core && hybrids_eq(&mc.hybrids, hybrid_ids);
                    match op {
                        CmpOp::Ge => ge(),
                        CmpOp::Le => le(),
                        CmpOp::Eq => eq(),
                        CmpOp::Gt => ge() && !eq(),
                        CmpOp::Lt => le() && !eq(),
                        CmpOp::Ne => !eq(),
                    }
                };
                // NO PRINTED COST IS NOT A COST OF ZERO, and the packed form cannot tell them
                // apart: a land and Ornithopter both arrive as {core: 0, hybrids: [], cmc: 0},
                // because `{0}` parses as a number and so contributes no pip and no cmc. The
                // INTERNED STRING is where the difference survives, and it survives as EMPTY
                // rather than absent — Scryfall prints `"mana_cost": ""` on a land and `"{0}"` on
                // Ornithopter, and the card object has to keep emitting both, so the id is real
                // either way and only its contents separate them.
                //
                // Measured on api.scryfall.com 2026-08-17, unique=prints:
                //
                //   m:{0} t:land   195     this answered 12,254 — every land in the corpus
                //   m={0}          293     this answered 12,713
                //   m:{0}       93,355     this answered 105,839
                //   -m:{0}      12,442     this answered 0, because m:{0} had matched everything
                //   m:{2} layout:meld  35  this answered 59 — the same cause, not a third one
                //
                // A costless card fails the containment and exactness comparisons rather than
                // matching the zero ones, which is what makes `-m:{0}` return the lands: the
                // negation is of the leaf, and the leaf is false for them.
                //
                // `!=` IS THE EXCEPTION, and measurement is the only reason this is not a blanket
                // false: `m!={w} t:land` is 12,249 on Scryfall, so a card with no cost DOES
                // satisfy "not exactly {W}". That is consistent rather than special — `!=` asks
                // whether the costs differ, and an absent cost differs from every queried one,
                // while `:` `=` `>` `<` all ask about a cost the card does not have.
                //
                // A FACE that prints a cost makes the card costed even when the card-level string
                // is empty, which is the split-card shape: the face arm below is the one that
                // answers, and gating it on the card-level string would silence it.
                let has_cost = card.faces.iter().any(|f| f.mana_cost.is_some())
                    || str_at(strings, u32::from(card.mana_cost_text_id)).is_some_and(|s| !s.is_empty());
                tri_bool(if has_cost {
                    matches(&card.mana_cost)
                        || card.faces.iter().any(|f| f.mana_cost.as_ref().is_some_and(matches))
                } else {
                    matches!(op, CmpOp::Ne)
                })
            }

            FilterExpr::Devotion { op, pips, hybrid_colors } => {
                // DEVOTION IS A QUESTION ABOUT THE QUERIED COLORS TAKEN TOGETHER, AND A HYBRID
                // QUERIES **BOTH** OF ITS COLORS AS ONE QUANTITY.
                //
                // This read the whole six-lane vector at once — one SWAR containment, plus an
                // integer equality that demanded every UNQUERIED color be zero too. Both are
                // wrong, and the hybrid case was wrong by two orders of magnitude:
                // `devotion:{r/g}` expands to lanes r=1,g=1, and a per-lane containment then
                // means "at least one red pip AND at least one green pip" — one card in KHM,
                // where Scryfall answers 62.
                //
                // MEASURED on api.scryfall.com 2026-08-16 over `e:khm t:creature` (151). `d[c]` is
                // this card's devotion to color c; the measure is the SUM over the queried lanes.
                //
                //   devotion:{r}       27 = d[r] >= 1        devotion:{g}        36
                //   devotion:{r}{r}     7 = d[r] >= 2        devotion:{g}{g}      8
                //   devotion={r}       20 = d[r] == 1        (27 - 7, and NOT 15, which is what
                //                                             whole-vector equality answered)
                //   devotion>{r}        7 = d[r] >  1        devotion<={r}      144
                //   devotion<{r}{r}   144   devotion!={r}    131 = 151 - 20
                //   devotion:{r/g}     62 = d[r]+d[g] >= 1   (27 + 36 - 1 card carrying both)
                //   devotion:{r/g}{r/g} 16 = d[r]+d[g] >= 2
                //   devotion={r/g}     46 = d[r]+d[g] == 1   (62 - 16)
                //   devotion>{r/g}     16   devotion<={r/g}  135   devotion!={r/g} 105
                //
                // THE SUM, NOT A PER-LANE OR — that 16 is what decides it. `devotion:{r}{r}` is 7
                // and `devotion:{g}{g}` is 8, so "d[r] >= 2 OR d[g] >= 2" can be at most 15; the
                // sixteenth card is the one KHM creature carrying one red pip AND one green pip,
                // which has neither lane at 2 and a combined devotion of exactly 2. An OR answers
                // 15 to all five hybrid rows above and 62 to the first, which is why the first one
                // alone is not enough evidence to pick a model.
                //
                // The queried lanes only — a lane the query left at zero is not part of the sum
                // and not a constraint. That is the half that made `devotion={r}` 15 here:
                // pinning the unqueried colors to zero excluded every red card that is also green.
                //
                // THE MEASURE IS DISTINCT PIPS, NOT A SUM OF LANES — the sum was the last
                // approximation here and it is gone. `ManaCost.devotion` stores per-color lanes
                // with hybrids EXPANDED, so a `{R/G}` symbol sits in red and in green; summing
                // both queried lanes counts one pip twice. Two cards falsify the two obvious
                // readings in opposite directions, and only the pip count answers both:
                //
                //                                       Svella {1}{R}{G}   Burning-Tree {R/G}{R/G}
                //   Scryfall's combined devotion               2                    2
                //   sum of the queried lanes                   2  OK                4  WRONG
                //   max of the queried lanes                   1  WRONG             2  OK
                //   DISTINCT PIPS matching either              2  OK                2  OK
                //
                // Burning-Tree answers `devotion:{r/g}{r/g}` on api.scryfall.com and NOT
                // `devotion:{r/g}{r/g}{r/g}`; Svella is the sixteenth card of KHM's 16, carrying
                // one red pip and one green one. The correction below is inclusion-exclusion over
                // the card's own hybrid symbols, and it is provably ZERO for a single-color query
                // (one bit in the mask cannot match twice), so every single-color comparison —
                // and the exact devotion PLANE, which declines multi-lane queries anyway — is
                // bit-identical to before.
                //
                // The per-color lanes themselves were never the approximation and are untouched:
                // Burning-Tree answers `devotion:{r}{r}` and `devotion:{g}{g}` on Scryfall, and so
                // does this. It was only the measure ACROSS two queried lanes that was wrong.
                //
                // Verified set-scoped, where corpus vintage cannot reach: `e:rna
                // devotion:{r/g}{r/g}` 24, `e:gtc devotion:{w/u}{w/u}` 16, `e:sok
                // devotion:{b/g}{b/g}` 21, `e:rna devotion:{r/g}{r/g}{r/g}` 3, `e:khm
                // devotion!={r/g}` 252, `e:khm devotion<={r/g}` 301 — all exact.
                //
                // A SECOND, SEPARATE RESIDUAL on Scryfall's side: `=` and `!=` with a hybrid value
                // never match a card whose cost carries that hybrid pip. `devotion={r/g} m:{r/g}`
                // is 0 there across all 61, while `devotion={r/g} m:{w/u} -m:{r/g}` is 1 — that
                // pair specifically, not hybrids in general. It is not self-consistent (the same
                // cards answer `devotion={r}` and `devotion:{r/g}`, and `!=` follows the model
                // above exactly, so `=` and `!=` are not complements there), so no model fits it
                // and none is guessed here.
                let d = u64::from(card.mana_cost.devotion);
                // The card's devotion to the queried colors TOGETHER, against the number of
                // symbols the query asked for. A lane the query leaves at zero contributes
                // nothing — including the vacuous all-zero query, which lands here as measure 0
                // against want 0.
                //
                // The two sides use DIFFERENT reductions and both are deliberate. `measure` sums
                // the card's lanes and then backs out the double count below, which is the pip
                // count. `want` takes the MAX of the query's
                // lanes, because a query lane counts SYMBOLS: `{r/g}{r/g}` sets r=2 and g=2 and
                // asks for two, not four. Max is exact over the whole domain Scryfall honors,
                // since a single color has one nonzero lane and a hybrid's two lanes are equal by
                // construction. Outside that domain there is nothing to be exact about: a
                // multi-color PLAIN value is REFUSED rather than answered — `devotion:{r}{g}`
                // comes back with the whole corpus and the warning "Invalid expression … was
                // ignored. Devotion can only match single color or hybrid mana." (measured
                // 2026-08-16: `e:khm t:creature devotion:{r}{g}` is all 151 creatures, and
                // `e:khm t:instant devotion:{r}{g}` is all 36 instants).
                let mut measure: u32 = 0;
                let mut want: u8 = 0;
                let mut query_mask: u8 = 0;
                for c in 0..6 {
                    let k = lane_get(*pips, c);
                    if k > 0 {
                        measure += u32::from(lane_get(d, c));
                        want = want.max(k);
                        query_mask |= 1u8 << c;
                    }
                }
                // BACK OUT THE DOUBLE COUNT. The lanes hold hybrids expanded, so a `{R/G}` pip
                // sits in red AND green; summing both queried lanes counts one pip twice. Each
                // hybrid symbol in the card's own cost gives back (matched queried colours - 1)
                // per pip, which is inclusion-exclusion for the only overlap a mana symbol can
                // have — a pip cannot be three of the queried colours unless the symbol says so,
                // and the mask handles that case too.
                //
                // This is what makes the measure a count of DISTINCT PIPS rather than a sum of
                // lanes. Both readings agree on every single-colour pip and differ only where a
                // card carries a hybrid of the queried pair — 61 cards for {R/G}, 58 for {W/U},
                // 64 for {B/G}.
                let overcount: u32 = card
                    .mana_cost
                    .hybrids
                    .iter()
                    .map(|e| {
                        let mask = hybrid_colors.get(usize::from(e.0)).copied().unwrap_or(0);
                        let matched = (mask & query_mask).count_ones();
                        u32::from(e.1) * matched.saturating_sub(1)
                    })
                    .sum();
                let measure = measure.saturating_sub(overcount);
                let want = u32::from(want);
                tri_bool(match op {
                    CmpOp::Ge => measure >= want,
                    CmpOp::Gt => measure > want,
                    CmpOp::Eq => measure == want,
                    CmpOp::Lt => measure < want,
                    CmpOp::Le => measure <= want,
                    CmpOp::Ne => measure != want,
                })
            }

            FilterExpr::DateCmp { op, value } => {
                // value is a zero-padded yyyymmdd (see build_binary); zero-padding a
                // partial date reproduces the old lexicographic-prefix semantics exactly,
                // since any real day/month (>= 01) compares greater than 00.
                let Some(p) = printing else { return Tri::PrintingDep };
                let Some(date) = p.released_at_int.as_ref().map(|v| u32::from(*v)) else {
                    return Tri::Null; // missing date: SQL NULL
                };
                tri_bool(match op {
                    CmpOp::Eq => date == *value,
                    CmpOp::Ne => date != *value,
                    CmpOp::Lt => date < *value,
                    CmpOp::Le => date <= *value,
                    CmpOp::Gt => date > *value,
                    CmpOp::Ge => date >= *value,
                })
            }

            FilterExpr::YearCmp { op, year } => {
                let Some(p) = printing else { return Tri::PrintingDep };
                let Some(date) = p.released_at_int.as_ref().map(|v| u32::from(*v)) else {
                    return Tri::Null; // missing date: SQL NULL
                };
                let card_year = (date / 10_000) as i32;
                tri_bool(match op {
                    CmpOp::Eq => card_year == *year,
                    CmpOp::Ne => card_year != *year,
                    CmpOp::Gt => card_year > *year,
                    CmpOp::Lt => card_year < *year,
                    CmpOp::Ge => card_year >= *year,
                    CmpOp::Le => card_year <= *year,
                })
            }
        }
    }
}

// ─── Building FilterExpr from JSON ───────────────────────────────────────────

fn str_op_to_cmp(s: &str) -> Result<CmpOp, String> {
    match s {
        "=" | ":" => Ok(CmpOp::Eq),
        "!="      => Ok(CmpOp::Ne),
        "<"       => Ok(CmpOp::Lt),
        "<="      => Ok(CmpOp::Le),
        ">"       => Ok(CmpOp::Gt),
        ">="      => Ok(CmpOp::Ge),
        _ => Err(format!("unknown operator: {s}")),
    }
}

/// `=` IS `:` ON A COLLECTION COLUMN — set EQUALITY is not a meaning Scryfall gives it.
///
/// Measured on api.scryfall.com 2026-08-16, every collection column this feeds, `X=v` against
/// `X:v` on the same corpus — identical on every row:
///
///   kw=flying e:khm      28 = kw:flying 28        (this answered 9: cards whose ONLY keyword is
///                                                  Flying — set equality, which nothing asks for)
///   otag=ramp e:khm      35 = otag:ramp 35        (this answered 0)
///   atag=forest e:khm    17 = atag:forest 17      (this answered 0)
///   is=foil e:khm t:cre  129 = is:foil 129        (this answered 0)
///   frame=2015 …         151 = frame:2015 151     (this answered 99)
///
/// The boundary is real and lies elsewhere, not on this function: the columns where `=` DOES
/// differ from `:` are the set-valued COLOR ones, and they go through `op_to_color_cmp`, which
/// keeps `Eq` — `c=rg e:khm t:creature` is 1 against `c:rg`'s 2, `id=rg` is 1 against `id:rg`'s
/// 52, `produces=rg` is 0 against `produces:rg`'s 5. Mana keeps it too (`m={2}` 0 against
/// `m:{2}`'s 102). Probed in both directions before this changed.
///
/// The other operators are unaffected and already agree: `kw>=flying`, `kw>flying`, `kw<flying`
/// and `kw!=flying` are each 404 on Scryfall and 404 here.
///
/// TypeCmp also reads this, but cannot observe the change: `card_types` with `=` is claimed by
/// the TypeLineContains branch above for every non-empty needle, so the only `=` that reaches
/// TypeCmp carries an empty value, which no query produces.
fn op_to_collection_cmp(op: &str) -> CmpOp {
    match op {
        ":" | ">=" | "=" => CmpOp::Ge,
        ">"        => CmpOp::Gt,
        "<="       => CmpOp::Le,
        "<"        => CmpOp::Lt,
        "!="       => CmpOp::Ne,
        _          => CmpOp::Ge,
    }
}

fn op_to_color_cmp(op: &str) -> CmpOp {
    match op {
        ":" | ">=" => CmpOp::Ge,
        "="        => CmpOp::Eq,
        "<="       => CmpOp::Le,
        "<"        => CmpOp::Lt,
        ">"        => CmpOp::Gt,
        "!="       => CmpOp::Ne,
        _          => CmpOp::Ge,
    }
}

fn build_num_expr(v: &Value) -> Result<NumExpr, String> {
    let node_type = v["node_type"].as_str().unwrap_or("");
    let kw = &v["kwargs"];
    match node_type {
        "NumericValueNode" => {
            let val = kw["value"].as_f64().ok_or("NumericValueNode missing value")?;
            Ok(NumExpr::Const(val))
        }
        "CardAttributeNode" => {
            let attr = kw["attribute_name"].as_str().unwrap_or("");
            attr_to_num_field(attr)
                .map(NumExpr::Field)
                .ok_or_else(|| format!("unknown numeric field: {attr}"))
        }
        "CardBinaryOperatorNode" => {
            let op_str = kw["op"].as_str().unwrap_or("");
            let arith_op = match op_str {
                "+" => ArithOp::Add,
                "-" => ArithOp::Sub,
                "*" => ArithOp::Mul,
                "/" => ArithOp::Div,
                _ => return Err(format!("expected arithmetic op, got: {op_str}")),
            };
            let lhs = build_num_expr(&kw["lhs"])?;
            let rhs = build_num_expr(&kw["rhs"])?;
            Ok(NumExpr::Arith(Box::new(lhs), arith_op, Box::new(rhs)))
        }
        _ => Err(format!("unexpected node in numeric expr: {node_type}")),
    }
}

pub(crate) fn build_filter(v: &Value) -> Result<FilterExpr, String> {
    let node_type = v["node_type"].as_str().unwrap_or("");
    let kw = &v["kwargs"];

    match node_type {
        "TrueNode" => Ok(FilterExpr::True),

        "AndNode" => {
            let operands = kw["operands"]
                .as_array()
                .ok_or("AndNode missing operands")?
                .iter()
                .map(build_filter)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(FilterExpr::And(operands))
        }

        "OrNode" => {
            let operands = kw["operands"]
                .as_array()
                .ok_or("OrNode missing operands")?
                .iter()
                .map(build_filter)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(FilterExpr::Or(operands))
        }

        "NotNode" => {
            let inner = build_filter(&kw["operand"])?;
            Ok(FilterExpr::Not(Box::new(inner)))
        }

        "ExactNameNode" => {
            let value = kw["value"].as_str().unwrap_or("").to_string();
            Ok(FilterExpr::ExactName(value))
        }

        "CardBinaryOperatorNode" => build_binary(kw),

        _ => Err(format!("unexpected top-level node type: {node_type}")),
    }
}

fn build_binary(kw: &Value) -> Result<FilterExpr, String> {
    let op = kw["op"].as_str().unwrap_or(":");
    let lhs = &kw["lhs"];
    let rhs = &kw["rhs"];

    let lhs_type = lhs["node_type"].as_str().unwrap_or("");
    let lhs_kw   = &lhs["kwargs"];

    if lhs_type != "CardAttributeNode" {
        let lhs_expr = build_num_expr(lhs)?;
        let rhs_expr = build_num_expr(rhs)?;
        let cmp_op   = str_op_to_cmp(op)?;
        return Ok(FilterExpr::NumericCmp { lhs: lhs_expr, op: cmp_op, rhs: rhs_expr });
    }

    let attr = lhs_kw["attribute_name"].as_str().unwrap_or("");
    let orig = lhs_kw["original_attribute"].as_str().unwrap_or("");

    if let Some(num_field) = attr_to_num_field(attr) {
        let cmp_op   = str_op_to_cmp(op)?;
        let rhs_expr = build_num_expr(rhs)?;
        return Ok(FilterExpr::NumericCmp { lhs: NumExpr::Field(num_field), op: cmp_op, rhs: rhs_expr });
    }

    if attr == "released_at" {
        let val_str = rhs_value_str(rhs);
        if orig == "year" {
            let year: i32 = val_str.parse().map_err(|_| format!("bad year: {val_str}"))?;
            let cmp_op = str_op_to_cmp(op)?;
            return Ok(FilterExpr::YearCmp { op: cmp_op, year });
        }
        let cmp_op = str_op_to_cmp(op)?;
        // yyyymmdd as integer; zero-pad partial dates so ordering matches the
        // lexicographic compare on ISO strings this replaced (day 00 < any real day).
        let digits: String = val_str.chars().filter(|c| c.is_ascii_digit()).collect();
        if digits.is_empty() || digits.len() > 8 {
            return Err(format!("bad date: {val_str}"));
        }
        let value: u32 = format!("{digits:0<8}").parse().map_err(|_| format!("bad date: {val_str}"))?;
        return Ok(FilterExpr::DateCmp { op: cmp_op, value });
    }

    if attr == "mana_cost_jsonb" {
        let mana_str = rhs_value_str(rhs);
        let mut core = 0u64;
        let mut hybrids: Vec<(String, u8)> = Vec::new();
        for (sym, n) in mana_pip_counts(mana_str) {
            match mana_lane(&sym) {
                Some(lane) => core = lane_add(core, lane, n),
                None => hybrids.push((sym, n)),
            }
        }
        hybrids.sort_unstable();
        // Until bind() resolves them against the store's vocab, hybrid
        // symbols count as unknown — one merged entry no card can match.
        let hybrid_ids = if hybrids.is_empty() { Vec::new() } else { vec![(MANA_SYM_UNKNOWN, 1)] };
        // The TRUE cmc of the query cost, which is what `generic_of` subtracts the pips from.
        // `mana_cmc` reads braces and bare letters but skips loose digits, so the shorthand forms
        // the parser passes through verbatim — `m:2` as "2", `m>=2WW` as "2WW", `m:1{r}1` as
        // "1{R}1" — arrived carrying none of their generic. `m:2` is the case that shows it: an
        // empty cost with cmc 0 is a tautology, and this answered all 151 of `e:khm t:creature`
        // where Scryfall answers 102, the same 102 as `m:{2}`.
        let cmc = mana_cmc(mana_str) + mana_bare_generic(mana_str) as f32;
        let cmp_op = match op { ":" => CmpOp::Ge, _ => str_op_to_cmp(op)? };
        return Ok(FilterExpr::ManaCostCmp { op: cmp_op, core, hybrids, hybrid_ids, hybrid_cmc: Vec::new(), cmc });
    }

    if attr == "devotion" {
        let mana_str = rhs_value_str(rhs);
        // Split hybrid symbols ({R/G} -> R:1, G:1) and keep only the WUBRGC
        // lanes, matching calculate_devotion() in SQL (which counts only
        // color characters). mana_pip_counts is NOT used lane-directly
        // because it keeps hybrids as single keys.
        let mut pips = 0u64;
        for (sym, n) in mana_pip_counts(mana_str) {
            if sym.contains('/') {
                for part in sym.split('/') {
                    if let Some(lane) = mana_lane(part).filter(|&l| l < 6) {
                        pips = lane_add(pips, lane, n);
                    }
                }
            } else if let Some(lane) = mana_lane(&sym).filter(|&l| l < 6) {
                pips = lane_add(pips, lane, n);
            }
        }
        let cmp_op = match op { ":" => CmpOp::Ge, _ => str_op_to_cmp(op)? };
        return Ok(FilterExpr::Devotion { op: cmp_op, pips, hybrid_colors: Vec::new() });
    }

    if matches!(attr, "card_colors" | "card_color_identity" | "produced_mana") {
        let color_field = match attr {
            "card_colors"          => ColorField::Colors,
            "card_color_identity"  => ColorField::ColorIdentity,
            _                      => ColorField::ProducedMana,
        };
        let color_strs: Vec<&str> = rhs
            .as_array()
            .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
            .unwrap_or_default();
        let mask = color_list_to_mask(&color_strs);
        // id:/identity: means "card's identity is a subset of query colors" (Le), not superset (Ge)
        let cmp_op = if attr == "card_color_identity" && op == ":" {
            CmpOp::Le
        } else {
            op_to_color_cmp(op)
        };
        return Ok(FilterExpr::ColorCmp { field: color_field, op: cmp_op, mask });
    }

    if attr == "card_legalities" {
        let format = rhs
            .as_array()
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let expected = match orig {
            "format" | "f" | "legal" => LEGALITY_LEGAL,
            "banned"                 => LEGALITY_BANNED,
            "restricted"             => LEGALITY_RESTRICTED,
            _                        => LEGALITY_LEGAL,
        };
        return Ok(FilterExpr::Legality { shift: format_shift(format), expected });
    }

    if attr == "card_types" {
        let mask: u16 = rhs
            .as_array()
            .map(|a| a.iter().fold(0u16, |acc, v| acc | card_type_str_to_bit(v.as_str().unwrap_or(""))))
            .unwrap_or(0);
        return Ok(FilterExpr::TypeCmp { mask, op: op_to_collection_cmp(op) });
    }

    if attr == "card_lang" {
        // Equality only, plus the `any` widener — the same surface upstream's parser grants
        // `lang:` (string-order comparisons error there like on the other string columns, so a
        // non-equality op reaching here is defense in depth, not a reachable path).
        if !matches!(op, ":" | "=") {
            return Err(format!("operator {op:?} is not supported on lang"));
        }
        let value = rhs_value_str(rhs).to_lowercase();
        let any = value == "any";
        return Ok(FilterExpr::LangMatch { value, vid: None, any });
    }

    if attr == "card_set_type" {
        // Equality only, the surface upstream's parser grants `st:` — same as `lang:`, and for the
        // same reason: it is a string column, and ordered comparisons on those error in the parser.
        if !matches!(op, ":" | "=") {
            return Err(format!("operator {op:?} is not supported on set_type"));
        }
        // Scryfall spells the set types with underscores (`draft_innovation`, `duel_deck`) and
        // accepts the hyphenated form too; the stored value is Scryfall's own, lowercased.
        let value = rhs_value_str(rhs).to_lowercase().replace('-', "_");
        return Ok(FilterExpr::SetTypeMatch { value, vid: None });
    }

    if attr == "oracle_id" {
        // Equality only, the surface upstream's parser grants `oracleid:` (string-order
        // comparisons parse there like on the other string columns, so a non-equality op reaching
        // here is defense in depth, not a reachable path). parse_uuid_or_hash folds hex case, so
        // an uppercase uuid — the parser hands the value on unchanged — resolves the same id.
        if !matches!(op, ":" | "=") {
            return Err(format!("operator {op:?} is not supported on oracle_id"));
        }
        return Ok(FilterExpr::OracleIdMatch { id: super::parse_uuid_or_hash(rhs_value_str(rhs)) });
    }

    if attr == "card_subtypes" {
        let value = rhs.as_array().and_then(|a| a.first()).and_then(|v| v.as_str()).unwrap_or("").to_string();
        return Ok(FilterExpr::CollectionCmp { field: CollField::Subtypes, op: op_to_collection_cmp(op), value, value_id: None });
    }

    if attr == "card_keywords" {
        let value  = rhs.as_array().and_then(|a| a.first()).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let cmp_op = op_to_collection_cmp(op);
        return Ok(FilterExpr::CollectionCmp { field: CollField::Keywords, op: cmp_op, value, value_id: None });
    }

    if matches!(attr, "card_oracle_tags" | "card_art_tags" | "card_is_tags" | "card_frame_data") {
        let coll_field = match attr {
            "card_oracle_tags" => CollField::OracleTags,
            "card_art_tags"    => CollField::ArtTags,
            "card_is_tags"     => CollField::IsTags,
            _                  => CollField::FrameData,
        };
        let value  = rhs.as_array().and_then(|a| a.first()).and_then(|v| v.as_str()).unwrap_or("").to_string();
        // Four `is:` values the importer stores no tag for, because fields already on the row
        // answer them (see `rewrite.ENGINE_IS_VALUES`, which is what keeps the parser from
        // reporting them unsupported). They arrive as `card_is_tags` membership like every other
        // `is:` value and turn into their own leaf HERE rather than in the parser, so the tag
        // vocabulary stays what the importer writes and nothing has to be stored twice.
        if matches!(coll_field, CollField::IsTags) {
            match value.as_str() {
                "localizedname" => return Ok(FilterExpr::PrintedNamePresent),
                "flavorname" => return Ok(FilterExpr::FlavorNamePresent),
                "unique" => return Ok(FilterExpr::SingleSet),
                "vanilla" => return Ok(FilterExpr::VanillaFace),
                _ => {}
            }
        }
        let cmp_op = op_to_collection_cmp(op);
        return Ok(FilterExpr::CollectionCmp { field: coll_field, op: cmp_op, value, value_id: None });
    }

    build_text_filter(attr, op, rhs)
}

/// Does `!"needle"` name this card? `stored` is the card's FOLDED name, `needle` the query's own
/// collated spelling (`collate_name(fold_accents(value.lower()))`, done in Python).
///
/// COLLATED on both sides — diacritics folded, every non-alphanumeric character removed — which
/// is how Scryfall compares it. Measured on api.scryfall.com 2026-08-16, all four of
/// `!"Lim-Dûl's Vault"`, `!"lim-dul's vault"`, `!"limduls vault"` and `!"Lim-Dul's Vault"` answer
/// the same one card, and `!"eowyn, lady of rohan"` answers "Éowyn, Lady of Rohan". Comparing
/// `card_name_lower` — what this did before — answered only the accented, fully punctuated
/// spelling, so a searcher who typed the name off the card and skipped the circumflex got
/// nothing.
///
/// The faces are split BEFORE collating, because the `" // "` join is itself non-alphanumeric:
/// collapsing first would make every face boundary vanish and let a needle straddle it.
///
/// The whole name, or — when the name has EXACTLY TWO halves — either side of the `" // "` join.
/// A two-faced card answers to each of its face names on its own. Measured against
/// api.scryfall.com on 2026-08-16: `!"Lightning Bolt"` returns two cards, `Lightning Bolt` and
/// `Emeritus of Conflict // Lightning Bolt` (sos/113), whose *second* face carries the name;
/// `!"Fire"` returns `Fire // Ice`, `!"Stomp"` returns `Bonecrusher Giant // Stomp`,
/// `!"Insectile Aberration"` returns `Delver of Secrets // Insectile Aberration`. Comparing only
/// the joined name found the first of those and missed all the rest.
///
/// TWO, and not "any part". `split(" // ")` yields every part, so a longer name answered to each
/// of its own — measured on api.scryfall.com 2026-08-31, `include_extras=true` throughout because
/// und/75 is extras-gated: `!"Who"` answers 0 there and answered 1 here, `!"What"` answers 0 there
/// and answered 1 here, both of them `Who // What // When // Where // Why`, the one printed name
/// with more than two parts. Its whole name stays a key —
/// `!"Who // What // When // Where // Why"` answers 1 on both sides. Nothing two-halved moves:
/// `!"Stomp"` answers 1 on both, `!"Fire"` answers 2 on both (`Fire // Ice` and `Start // Fire`),
/// and the joined name of a two-half card is a key as well
/// (`!"Curse of the Fire Penguin // Curse of the Fire Penguin Creature"`, 1 on both). The
/// collation is untouched: `!"limduls vault"` still answers `Lim-Dûl's Vault`, 1 on both.
///
/// This is the `!` SEARCH operator and nothing else. `/cards/named?exact=` deliberately answers on
/// ORACLE names alone (see `core_api::folded_name_matches` and the route's own note) — the two
/// surfaces share a rule shape, not a scope, and conflating them would widen a route Scryfall keeps
/// narrow.
pub(crate) fn exact_name_matches(stored: &str, needle: &str) -> bool {
    if crate::collate_name(stored) == needle {
        return true;
    }
    // `split_once` and then a reject, rather than `split(...).any(...)`: the face keys exist only
    // for a name that is exactly two halves, and a third part means there are none.
    let Some((front, back)) = stored.split_once(" // ") else { return false };
    if back.contains(" // ") {
        return false;
    }
    crate::collate_name(front) == needle || crate::collate_name(back) == needle
}

fn rhs_value_str(rhs: &Value) -> &str {
    rhs["kwargs"]["value"].as_str().unwrap_or("")
}

fn build_text_filter(attr: &str, op: &str, rhs: &Value) -> Result<FilterExpr, String> {
    let rhs_node_type = rhs["node_type"].as_str().unwrap_or("");

    if rhs_node_type == "RegexValueNode" {
        let pattern  = rhs["kwargs"]["value"].as_str().unwrap_or("");
        let re = compile_search_regex(pattern)?;
        let field = match attr {
            "card_name"   => TextField::NameLower,
            "oracle_text" => TextField::OracleTextLower,
            "flavor_text" => TextField::FlavorTextLower,
            "card_artist" => TextField::ArtistLower,
            _ => return Err(format!("regex not supported on {attr}")),
        };
        return Ok(FilterExpr::TextRegex { field, regex: re });
    }

    let raw_value = rhs["kwargs"]["value"].as_str().unwrap_or("");

    // `name!=x`, `o!=x`, `a!=x`, `set!=x` — Scryfall answers NOTHING, for every value and every
    // string column, and that is a statement about the OPERATOR rather than about the value.
    // Measured 2026-08-16: `name!="lightning bolt"` 404 where `name="lightning bolt"` answers 2,
    // `name!=bolt` 404 where `name=bolt` answers 41; `o!="draw a card"`, `a!="rebecca guay"`,
    // `t!=creature` and `set!=khm` all 404 — while the NUMERIC `cmc!=3` answers 25,522, so this is
    // not "`!=` is unsupported". It is the empty set, and it composes as one: `name!=ft or
    // t:creature` answers exactly `t:creature`'s 18,753 and `-name!=ft` answers the whole corpus.
    // This port answered a not-equal SUPERSET (33,751 for `name!=ft`), which is the one shape a
    // client cannot recover from — a filter that silently widens rather than narrows.
    if op == "!=" {
        return Ok(FilterExpr::Not(Box::new(FilterExpr::True)));
    }

    if matches!(attr, "card_set_code" | "card_layout" | "card_border" | "card_watermark" | "collector_number") {
        // collector_number_id is stored raw and mixed-case (e.g. "10E-105"); compare exactly,
        // matching the SQL path. The other four are lowercased at import, so lowercasing
        // the query value gives case-insensitive matching with a plain equality.
        let value = if attr == "collector_number" { raw_value.to_string() } else { raw_value.to_lowercase() };
        let cmp_op = str_op_to_cmp(op)?;
        let field = match attr {
            "card_set_code"    => TextField::SetCode,
            "card_layout"      => TextField::Layout,
            "card_border"      => TextField::Border,
            "card_watermark"   => TextField::Watermark,
            "collector_number" => TextField::CollectorNumber,
            _                  => unreachable!(),
        };
        return Ok(FilterExpr::TextExact { field, op: cmp_op, value });
    }

    let lower_word = raw_value.to_lowercase();
    // `=` IS `:` ON A TEXT COLUMN — a SUBSTRING test, not an equality, and not a member of the
    // comparison family the branch above answers with the empty set.
    //
    // This is the one operator on these columns that carries no information at all. Measured on
    // api.scryfall.com 2026-08-16, `X=v` against `X:v` over the whole default corpus:
    //
    //   o=flying    4,574 = o:flying 4,574      (this answered 99 — the cards whose ORACLE TEXT
    //                                            IS the word "flying", i.e. a real equality)
    //   ft=aether      80 = ft:aether 80        (this answered 0)
    //   name=ft     1,628 = name:ft 1,628       (this answered 0)
    //   fo=lifelink   713 = fo:lifelink 713
    //   a=rebecca     170 = a:rebecca 170       (already agreed — `bind` collapses every artist
    //                                            form onto one collated contains regardless)
    //
    // and the BARE/QUOTED split survives `=` intact rather than being flattened to one side of
    // it: `name="ft"` is 362 on Scryfall, exactly `name:"ft"`, against `name=ft`'s 1,628. That
    // distinction is carried by the node shape (`CollatedNameValueNode` vs `StringValueNode`), so
    // routing `=` here preserves it for free — the parser now builds the collated node for `=`
    // as well, which is the other half of this fix.
    //
    // WHAT IS NOT IN THIS CLASS, probed in both directions rather than assumed. `!=` is the empty
    // set (the branch above, unchanged). `<`, `<=`, `>`, `>=` keep the string-order comparison
    // they had; Scryfall answers 404 to all of them on every string column, which is what the
    // query-validation layer already reproduces. And `=` stays a genuine EQUALITY on the columns
    // that are stored exact rather than searched — set code, layout, border, watermark, collector
    // number — which is why those five are claimed by the branch above this one and never reach
    // here: `e=khm` is 151 and `layout=normal` is 284, agreeing with `:` because equality IS the
    // meaning there, not because the operator was rewritten.
    if matches!(op, ":" | "=") {
        let tsf = match attr {
            // `CollatedNameValueNode` is the parser's spelling of "the user typed a BARE word
            // here"; a `StringValueNode` under `name:` means they quoted it (or wrote a
            // plain-literal regex, which lowers to the same). The two are different searches —
            // see `TextSearchField::NameLower` / `NameCollated` for the live measurements.
            "card_name" if rhs_node_type == "CollatedNameValueNode" => TextSearchField::NameCollated,
            "card_name"   => TextSearchField::NameLower,
            "oracle_text" => TextSearchField::OracleTextLower,
            "flavor_text" => TextSearchField::FlavorTextLower,
            // Same split as `card_name`: a bare word arrives as a CollatedNameValueNode and is
            // matched against the collated artist vocab, a quoted one stays literal.
            "card_artist" if rhs_node_type == "CollatedNameValueNode" => TextSearchField::ArtistCollated,
            "card_artist" => TextSearchField::ArtistLower,
            _ => return Err(format!("text substring not supported on {attr}")),
        };
        // `æ` expands, and ONLY in the text columns — see `crate::fold_ae` for the per-character
        // probe. The name and artist needles are already fully transliterated by the parser's
        // `fold_accents`, and a literal `name:"…"` deliberately keeps its spelling.
        let word = match tsf {
            TextSearchField::OracleTextLower | TextSearchField::FlavorTextLower => crate::fold_ae(&lower_word),
            _ => lower_word,
        };
        return Ok(FilterExpr::TextContains { field: tsf, word });
    }

    let field = match attr {
        "card_name"   => TextField::NameLower,
        "oracle_text" => TextField::OracleTextLower,
        "flavor_text" => TextField::FlavorTextLower,
        "card_artist" => TextField::ArtistLower,
        _ => return Err(format!("unknown text field: {attr}")),
    };
    let cmp_op = str_op_to_cmp(op)?;
    let value = match field {
        TextField::OracleTextLower | TextField::FlavorTextLower => crate::fold_ae(&raw_value.to_lowercase()),
        _ => raw_value.to_lowercase(),
    };
    Ok(FilterExpr::TextExact { field, op: cmp_op, value })
}
