use regex::Regex;
use serde_json::Value;
use super::{AOracleCard, APrinting, AStrings, str_at, mana_lane, lane_add, lanes_ge, LANES6_HI, LANES8_HI, mana_pip_counts, mana_cmc, color_list_to_mask, card_type_str_to_bit, trigram_candidates, trigram_min_posting, ARTIST_NONE, NONE_STR, FlavorIndex, NameBigramIndex, OracleTextIndex, SortedTrigramIndex, flavor_fingerprint, flavor_match_sets};
use super::legality::{LEGALITY_LEGAL, LEGALITY_BANNED, LEGALITY_RESTRICTED, format_shift};

// ─── Comparison / arithmetic operators ───────────────────────────────────────

#[derive(Clone, Copy, PartialEq)]
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

#[derive(Clone, Copy)]
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
enum NumVal {
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
        NumField::Cmc                => known(card.cmc.as_ref().map(|v| u8::from(*v) as f32)),
        NumField::Power              => known(card.creature_power.as_ref().map(|v| i8::from(*v) as f32)),
        NumField::Toughness          => known(card.creature_toughness.as_ref().map(|v| i8::from(*v) as f32)),
        NumField::Loyalty            => known(card.planeswalker_loyalty.as_ref().map(|v| u8::from(*v) as f32)),
        NumField::EdhrEc             => known(card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32)),
        NumField::RarityInt          => printing.map_or(NumVal::PDep, |p| known(p.card_rarity_int.as_ref().map(|v| u8::from(*v) as f32))),
        NumField::CollectorNumberInt => printing.map_or(NumVal::PDep, |p| known(p.collector_number_int.as_ref().map(|v| u16::from(*v) as f32))),
        NumField::PriceUsd           => printing.map_or(NumVal::PDep, |p| known_cents(p.price_usd.as_ref().map(|v| u32::from(*v)))),
        NumField::PriceEur           => printing.map_or(NumVal::PDep, |p| known_cents(p.price_eur.as_ref().map(|v| u32::from(*v)))),
        NumField::PriceTix           => printing.map_or(NumVal::PDep, |p| known_cents(p.price_tix.as_ref().map(|v| u32::from(*v)))),
        NumField::PreferScore        => printing.map_or(NumVal::PDep, |p| known(p.prefer_score.as_ref().map(|v| f32::from(*v)))),
    }
}

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
    // eval() itself non-recursive, so the attribute now actually applies:
    // for the common Const/Field leaf case (e.g. `usd<50`, no arithmetic),
    // eval()'s whole body -- including field_num, already small enough to
    // inline on its own -- folds directly into tri(), eliminating both
    // calls' prologue/epilogue/jump-table tax. Arith (`cmc+1<power`) is
    // colder and still recurses through eval_arith, unaffected either way.
    #[inline(always)]
    fn eval(&self, card: &AOracleCard, printing: Option<&APrinting>) -> NumVal {
        match self {
            NumExpr::Const(v) => NumVal::Known(*v),
            NumExpr::Field(f) => field_num(card, printing, *f),
            NumExpr::Arith(lhs, op, rhs) => Self::eval_arith(lhs, *op, rhs, card, printing),
        }
    }

    fn eval_arith(lhs: &NumExpr, op: ArithOp, rhs: &NumExpr, card: &AOracleCard, printing: Option<&APrinting>) -> NumVal {
        // Null dominates PDep: Null op anything is Null for every
        // printing, so the card-level result is already exact.
        match (lhs.eval(card, printing), rhs.eval(card, printing)) {
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

fn cmp(op: CmpOp, a: f64, b: f64) -> bool {
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
        ColorField::Colors        => u8::from(card.card_colors),
        ColorField::ColorIdentity => u8::from(card.card_color_identity),
        ColorField::ProducedMana  => u8::from(card.produced_mana),
    }
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

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum TextSearchField {
    NameLower,
    OracleTextLower,
    FlavorTextLower,
    ArtistLower,
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
        TextSearchField::NameLower       => StrVal::Known(card.card_name_lower.as_str()),
        TextSearchField::OracleTextLower => opt_sv(str_at(strings, u32::from(card.oracle_text_lower_id))),
        TextSearchField::FlavorTextLower => printing.map_or(StrVal::PDep, |p| opt_sv(str_at(strings, u32::from(p.flavor_text_lower_id)))),
        // Rewritten to ArtistMatch by bind(); printings carry no artist strings.
        TextSearchField::ArtistLower     => StrVal::Null,
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
        cmc: f32,
    },

    Devotion {
        op: CmpOp,
        /// Queried WUBRGC devotion counts in the low six 8-bit lanes,
        /// hybrid query pips expanded at build — same layout as
        /// ManaCost.devotion, so every comparison is lane arithmetic.
        pips: u64,
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
///   NumericCmp, ExactName, TextExact, Legality, DateCmp, YearCmp): 1.8-5.6 ns
///   measured; NumericCmp is the priciest member (NumExpr::eval() indirection
///   on both sides costs more than a direct field load), so the constant sits
///   above it.
pub(crate) const MASK_COMPARE_NS100: u32 = 600;
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

/// Per-candidate verification cost of a node in the tri walk. Composites take
/// the max of their children: their short-circuit may have to evaluate every
/// child, so the most expensive child bounds the cost.
fn verify_cost_tier(f: &FilterExpr) -> u32 {
    match f {
        FilterExpr::TextRegex { regex, .. } => regex_tier(regex.as_str()),
        FilterExpr::TextContains { .. } => TEXT_SCAN_NS100,
        FilterExpr::Devotion { .. } | FilterExpr::ManaCostCmp { .. } => SET_LOOKUP_NS100,
        FilterExpr::ArtistMatch { .. }
        | FilterExpr::FlavorMatch { .. }
        | FilterExpr::NameMatch { .. }
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
pub(crate) fn regex_tier(pattern: &str) -> u32 {
    let mut p = pattern.strip_prefix("(?i)").unwrap_or(pattern);
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
            TextSearchField::NameLower | TextSearchField::OracleTextLower | TextSearchField::ArtistLower => false,
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
        FilterExpr::And(children) | FilterExpr::Or(children) => children.iter().all(printing_dependent),
        FilterExpr::Not(inner) => printing_dependent(inner),
        // Exhaustive, not `_ => false`: a new variant must get a considered
        // answer here rather than silently inheriting "can settle at card level".
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

/// Vocab ids (ascending) whose artist string satisfies `pred`.
fn artist_match_ids(artist_vocab: &AStrings, pred: impl Fn(&str) -> bool) -> Vec<u16> {
    artist_vocab
        .iter()
        .enumerate()
        .filter(|(_, s)| pred(s.as_str()))
        .map(|(i, _)| i as u16)
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
    pub(crate) fn bind(
        &mut self,
        vocab: &AStrings,
        sorted_ids: &rkyv::Archived<Vec<u16>>,
        artist_vocab: &AStrings,
        mana_vocab: &AStrings,
        flavor: &rkyv::Archived<FlavorIndex>,
        strings: &AStrings,
    ) {
        match self {
            FilterExpr::And(children) | FilterExpr::Or(children) => {
                for c in children {
                    c.bind(vocab, sorted_ids, artist_vocab, mana_vocab, flavor, strings);
                }
            }
            FilterExpr::Not(inner) => inner.bind(vocab, sorted_ids, artist_vocab, mana_vocab, flavor, strings),
            FilterExpr::ManaCostCmp { hybrids, hybrid_ids, .. } if !hybrids.is_empty() => {
                *hybrid_ids = bind_mana_hybrids(hybrids, mana_vocab);
            }
            FilterExpr::CollectionCmp { value, value_id, .. } => {
                let i = sorted_ids.partition_point(|id| vocab[u16::from(*id) as usize].as_str() < value.as_str());
                *value_id = sorted_ids
                    .get(i)
                    .map(|id| u16::from(*id))
                    .filter(|&id| vocab[id as usize].as_str() == value.as_str());
            }
            FilterExpr::TextContains { field: TextSearchField::ArtistLower, word } => {
                let ids = artist_match_ids(artist_vocab, |s| s.contains(word.as_str()));
                *self = FilterExpr::ArtistMatch { ids };
            }
            FilterExpr::TextExact { field: TextField::ArtistLower, op, value } => {
                let (op, value) = (*op, std::mem::take(value));
                let ids = artist_match_ids(artist_vocab, |s| match op {
                    CmpOp::Eq => s == value,
                    CmpOp::Ne => s != value,
                    CmpOp::Lt => s < value.as_str(),
                    CmpOp::Le => s <= value.as_str(),
                    CmpOp::Gt => s > value.as_str(),
                    CmpOp::Ge => s >= value.as_str(),
                });
                *self = FilterExpr::ArtistMatch { ids };
            }
            FilterExpr::TextRegex { field: TextField::ArtistLower, regex } => {
                let ids = artist_match_ids(artist_vocab, |s| regex.is_match(s));
                *self = FilterExpr::ArtistMatch { ids };
            }
            FilterExpr::TextContains { field: TextSearchField::FlavorTextLower, word } => {
                let mask = flavor_fingerprint(word.as_str());
                let (gids, dense_ids) = flavor_match_sets(flavor, strings, mask, |s| s.contains(word.as_str()));
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
                let (gids, dense_ids) = flavor_match_sets(flavor, strings, 0, |s| regex.is_match(s));
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
            FilterExpr::TextContains { field: TextSearchField::NameLower, word } if word.len() == 2 => {
                // 2-byte needles resolve exactly through the bigram index: the
                // member cards are the complete match set (containment IS
                // bigram membership), so no contains() verification runs at
                // all — the ids just re-key to card_name_id for eval.
                if u32::from(name_bigrams.n_cards) as usize != cards.len() {
                    return;
                }
                let bg = [word.as_bytes()[0], word.as_bytes()[1]];
                let bind_bound = name_bigrams.postings.get(&bg).map_or_else(
                    || name_bigrams.plane_of.get(&bg).map_or(0, |_| cards.len() / 8),
                    |v| v.len(),
                );
                if !Self::memoize_pays(bind_bound, eval_domain, cards.len()) {
                    return;
                }
                let mut ids: Vec<u32> = if let Some(p) = name_bigrams.plane_of.get(&bg) {
                    let wpp = cards.len().div_ceil(64);
                    let start = u32::from(*p) as usize * wpp;
                    let mut out = Vec::new();
                    for (i, w) in name_bigrams.plane_words[start..start + wpp].iter().enumerate() {
                        let mut w = u64::from(*w);
                        while w != 0 {
                            let cid = ((i as u32) << 6) | w.trailing_zeros();
                            w &= w - 1;
                            out.push(u32::from(cards[cid as usize].card_name_id));
                        }
                    }
                    out
                } else {
                    name_bigrams
                        .postings
                        .get(&bg)
                        .map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(cards[u16::from(*x) as usize].card_name_id)).collect())
                };
                ids.sort_unstable();
                ids.dedup();
                *self = FilterExpr::NameMatch { ids };
            }
            FilterExpr::TextContains { field: TextSearchField::NameLower, word } => {
                // The intersection is bounded by the shortest posting list, so
                // checking that bound first makes the decline path free — no
                // gather, no intersection. Declining when only the *bound*
                // (not the exact count) exceeds half the corpus is deliberate:
                // it can only happen when every trigram of the needle is
                // ultra-common, where the match set is broad anyway.
                match trigram_min_posting(name_trigram, word) {
                    Some(min) if min <= cards.len() / 2 && Self::memoize_pays(min, eval_domain, cards.len()) => {}
                    _ => return,
                }
                let Some(cand) = trigram_candidates(name_trigram, word) else { return };
                let mut ids: Vec<u32> = cand
                    .into_iter()
                    .filter(|&cid| cards[cid as usize].card_name_lower.as_str().contains(word.as_str()))
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
                let mut gids: Vec<u32> = Vec::with_capacity(dense.len());
                for d in dense {
                    let gid = u32::from(oracle.gids[d as usize]);
                    if str_at(strings, gid).is_some_and(|s| s.contains(word.as_str())) {
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
    pub(crate) fn order_children_by_verify_cost(&mut self) {
        match self {
            FilterExpr::And(children) => {
                for c in children.iter_mut() {
                    c.order_children_by_verify_cost();
                }
                children.sort_by_key(|c| (printing_dependent(c), verify_cost_tier(c), and_child_set_len(c)));
            }
            FilterExpr::Or(children) => {
                for c in children.iter_mut() {
                    c.order_children_by_verify_cost();
                }
                children.sort_by_key(or_child_key);
            }
            FilterExpr::Not(inner) => inner.order_children_by_verify_cost(),
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
    ) -> Tri {
        residual.clear();
        *residual_is_or = false;
        match self {
            FilterExpr::And(children) => {
                for c in children {
                    match c.tri_fast(card, None).unwrap_or_else(|| c.tri(card, None, strings)) {
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
                    match c.tri_fast(card, None).unwrap_or_else(|| c.tri(card, None, strings)) {
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
            other => match other.tri_fast(card, None).unwrap_or_else(|| other.tri(card, None, strings)) {
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
        let eval_one = |c: &FilterExpr| c.tri_fast(card, Some(printing)).unwrap_or_else(|| c.tri(card, Some(printing), strings));
        if residual_is_or {
            residual.iter().any(|c| eval_one(c) == Tri::True)
        } else {
            residual.iter().all(|c| eval_one(c) == Tri::True)
        }
    }

    /// A narrow, byte-identical fast path for the cheap leaf shapes
    /// (MASK_COMPARE_NS100 tier, see verify_cost_tier) that actually reach
    /// this code at all. That's the real filter, not raw query frequency:
    /// compile_plane (planes.rs) already intercepts ColorCmp/TypeCmp/
    /// Legality/Devotion/rarity-NumericCmp/border-TextExact into a bitmap
    /// before they ever reach tri() in the common case, so a query like
    /// `color:g` or `type:creature` alone mostly never calls tri_fast at
    /// all -- those variants are NOT here despite being high-frequency in
    /// realistic traffic (client/query_runner.py's weights), because their
    /// plane path already avoids paying tri()'s cost in the first place.
    /// NumericCmp (non-rarity fields)/DateCmp/YearCmp have no plane arm --
    /// every occurrence, however rare, pays the full residual cost, which is
    /// what justifies them here even though they're a smaller slice of
    /// real-query volume than color/type. tri() itself can't be
    /// force-inlined into these call sites without pulling in the other
    /// ~15 unrelated arms (TextContains, regex, ManaCostCmp, ...) too --
    /// measured that directly, made every mode slower (code bloat outweighing
    /// the saved call). This covers just the cheap arms that are also
    /// un-planed, small enough to actually inline, so the hot
    /// per-printing/per-card loops skip tri()'s call boundary entirely for
    /// the common case, falling back to the exact same tri() arm (not a
    /// second implementation -- no risk of the two drifting apart, unlike
    /// the reverted price-cents fast path) for anything else.
    #[inline(always)]
    fn tri_fast(&self, card: &AOracleCard, printing: Option<&APrinting>) -> Option<Tri> {
        match self {
            FilterExpr::NumericCmp { lhs, op, rhs } => Some(match (lhs.eval(card, printing), rhs.eval(card, printing)) {
                (NumVal::Null, _) | (_, NumVal::Null) => Tri::Null,
                (NumVal::PDep, _) | (_, NumVal::PDep) => Tri::PrintingDep,
                (NumVal::Known(a), NumVal::Known(b)) => tri_bool(cmp(*op, a, b)),
            }),
            // value is a zero-padded yyyymmdd (see build_binary); zero-padding a
            // partial date reproduces the old lexicographic-prefix semantics exactly,
            // since any real day/month (>= 01) compares greater than 00.
            FilterExpr::DateCmp { op, value } => {
                let Some(p) = printing else { return Some(Tri::PrintingDep) };
                let Some(date) = p.released_at_int.as_ref().map(|v| u32::from(*v)) else {
                    return Some(Tri::Null); // missing date: SQL NULL
                };
                Some(tri_bool(match op {
                    CmpOp::Eq => date == *value,
                    CmpOp::Ne => date != *value,
                    CmpOp::Lt => date < *value,
                    CmpOp::Le => date <= *value,
                    CmpOp::Gt => date > *value,
                    CmpOp::Ge => date >= *value,
                }))
            }
            FilterExpr::YearCmp { op, year } => {
                let Some(p) = printing else { return Some(Tri::PrintingDep) };
                let Some(date) = p.released_at_int.as_ref().map(|v| u32::from(*v)) else {
                    return Some(Tri::Null); // missing date: SQL NULL
                };
                let card_year = (date / 10_000) as i32;
                Some(tri_bool(match op {
                    CmpOp::Eq => card_year == *year,
                    CmpOp::Ne => card_year != *year,
                    CmpOp::Gt => card_year > *year,
                    CmpOp::Lt => card_year < *year,
                    CmpOp::Ge => card_year >= *year,
                    CmpOp::Le => card_year <= *year,
                }))
            }
            _ => None,
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

            FilterExpr::And(_) | FilterExpr::Or(_) | FilterExpr::Not(_) => self.tri_composite(card, printing, strings),

            FilterExpr::ExactName(lower) => tri_bool(card.card_name_lower.as_str() == lower.as_str()),

            FilterExpr::NumericCmp { .. } => self.tri_fast(card, printing).expect("tri_fast always handles NumericCmp"),

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
                    StrVal::Known(s) => tri_bool(regex.is_match(s)),
                    StrVal::Null => Tri::Null,
                    StrVal::PDep => Tri::PrintingDep,
                }
            }

            FilterExpr::ColorCmp { field, op, mask } => {
                let bits = card_colors(card, *field);
                tri_bool(match op {
                    // mask == 0 means the query was literally "c"/"colorless" (see
                    // get_colors_comparison_object on the Python side), not "at
                    // least zero colors" -- bits & 0 == 0 is vacuously true for
                    // every card, so Ge must fall back to exact equality here.
                    CmpOp::Ge => if *mask == 0 { bits == 0 } else { bits & mask == *mask },
                    CmpOp::Eq => bits == *mask,
                    CmpOp::Le => bits & !mask == 0,
                    CmpOp::Lt => bits & !mask == 0 && bits != *mask,
                    CmpOp::Gt => bits & mask == *mask && bits != *mask,
                    CmpOp::Ne => bits != *mask,
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
                    // The set-like collections are sorted by id at load.
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

            FilterExpr::ManaCostCmp { op, core, hybrid_ids, cmc, .. } => {
                // Containment/equality over the pip multiset = the same test
                // per lane (SWAR, all eight at once) and per hybrid entry
                // (sorted-slice walks; both sides empty on ~97% of cards).
                let mc = &card.mana_cost;
                let card_core = u64::from(mc.core);
                let card_cmc = f32::from(mc.cmc);
                let ge = || lanes_ge(card_core, *core, LANES8_HI) && hybrids_ge(&mc.hybrids, hybrid_ids) && card_cmc >= *cmc;
                let le = || lanes_ge(*core, card_core, LANES8_HI) && hybrids_le(&mc.hybrids, hybrid_ids) && card_cmc <= *cmc;
                let eq = || card_cmc == *cmc && card_core == *core && hybrids_eq(&mc.hybrids, hybrid_ids);
                tri_bool(match op {
                    CmpOp::Ge => ge(),
                    CmpOp::Le => le(),
                    CmpOp::Eq => eq(),
                    CmpOp::Gt => ge() && !eq(),
                    CmpOp::Lt => le() && !eq(),
                    CmpOp::Ne => !eq(),
                })
            }

            FilterExpr::Devotion { op, pips } => {
                // Mirrors the SQL path's JSONB containment on the devotion column
                // (devotion @> query, <@, =, and the strict/negated variants):
                // per-color positional arrays contain each other iff the counts
                // compare, so containment is per-lane count comparison — one
                // SWAR op across all six colors — and equality is integer
                // equality (a zero lane and an absent key are the same thing).
                let d = u64::from(card.mana_cost.devotion);
                let ge = lanes_ge(d, *pips, LANES6_HI);
                let le = lanes_ge(*pips, d, LANES6_HI);
                let eq = d == *pips;
                tri_bool(match op {
                    CmpOp::Ge => ge,
                    CmpOp::Eq => eq,
                    CmpOp::Le => le,
                    CmpOp::Gt => ge && !eq,
                    CmpOp::Lt => le && !eq,
                    CmpOp::Ne => !eq,
                })
            }

            FilterExpr::DateCmp { .. } => self.tri_fast(card, printing).expect("tri_fast always handles DateCmp"),

            FilterExpr::YearCmp { .. } => self.tri_fast(card, printing).expect("tri_fast always handles YearCmp"),
        }
    }

    /// The self-recursive rest of tri(): boolean composition over children.
    /// Kept out of tri() itself so tri()'s leaf dispatch can be force-inlined
    /// (see the comment on tri()) -- this one stays a real call, which is
    /// fine since a composite node's own cost is dominated by its children,
    /// not by the one extra call to get here.
    fn tri_composite(&self, card: &AOracleCard, printing: Option<&APrinting>, strings: &AStrings) -> Tri {
        match self {
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
            _ => unreachable!("tri_composite only called for And/Or/Not"),
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

fn op_to_collection_cmp(op: &str) -> CmpOp {
    match op {
        ":" | ">=" => CmpOp::Ge,
        "="        => CmpOp::Eq,
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
        let cmc = mana_cmc(mana_str);
        let cmp_op = match op { ":" => CmpOp::Ge, _ => str_op_to_cmp(op)? };
        return Ok(FilterExpr::ManaCostCmp { op: cmp_op, core, hybrids, hybrid_ids, cmc });
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
        return Ok(FilterExpr::Devotion { op: cmp_op, pips });
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
        let cmp_op = op_to_collection_cmp(op);
        return Ok(FilterExpr::CollectionCmp { field: coll_field, op: cmp_op, value, value_id: None });
    }

    build_text_filter(attr, op, rhs)
}

fn rhs_value_str<'a>(rhs: &'a Value) -> &'a str {
    rhs["kwargs"]["value"].as_str().unwrap_or("")
}

fn build_text_filter(attr: &str, op: &str, rhs: &Value) -> Result<FilterExpr, String> {
    let rhs_node_type = rhs["node_type"].as_str().unwrap_or("");

    if rhs_node_type == "RegexValueNode" {
        let pattern  = rhs["kwargs"]["value"].as_str().unwrap_or("");
        let re = Regex::new(&format!("(?i){pattern}"))
            .map_err(|e| format!("invalid regex '{pattern}': {e}"))?;
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
    if op == ":" {
        let tsf = match attr {
            "card_name"   => TextSearchField::NameLower,
            "oracle_text" => TextSearchField::OracleTextLower,
            "flavor_text" => TextSearchField::FlavorTextLower,
            "card_artist" => TextSearchField::ArtistLower,
            _ => return Err(format!("text substring not supported on {attr}")),
        };
        return Ok(FilterExpr::TextContains { field: tsf, word: lower_word });
    }

    let field = match attr {
        "card_name"   => TextField::NameLower,
        "oracle_text" => TextField::OracleTextLower,
        "flavor_text" => TextField::FlavorTextLower,
        "card_artist" => TextField::ArtistLower,
        _ => return Err(format!("unknown text field: {attr}")),
    };
    let cmp_op = str_op_to_cmp(op)?;
    Ok(FilterExpr::TextExact { field, op: cmp_op, value: raw_value.to_lowercase() })
}
