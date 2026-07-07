use std::collections::HashMap;
use regex::Regex;
use serde_json::Value;
use super::{AOracleCard, APrinting, AStrings, str_at, is_devotion_sym, mana_pip_counts, mana_cmc, color_list_to_mask, card_type_str_to_bit, ARTIST_NONE, NONE_STR, FlavorIndex, flavor_fingerprint, flavor_match_sets};
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
    match f {
        NumField::Cmc                => known(card.cmc.as_ref().map(|v| u8::from(*v) as f32)),
        NumField::Power              => known(card.creature_power.as_ref().map(|v| i8::from(*v) as f32)),
        NumField::Toughness          => known(card.creature_toughness.as_ref().map(|v| i8::from(*v) as f32)),
        NumField::Loyalty            => known(card.planeswalker_loyalty.as_ref().map(|v| u8::from(*v) as f32)),
        NumField::EdhrEc             => known(card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32)),
        NumField::RarityInt          => printing.map_or(NumVal::PDep, |p| known(p.card_rarity_int.as_ref().map(|v| u8::from(*v) as f32))),
        NumField::CollectorNumberInt => printing.map_or(NumVal::PDep, |p| known(p.collector_number_int.as_ref().map(|v| u16::from(*v) as f32))),
        NumField::PriceUsd           => printing.map_or(NumVal::PDep, |p| known(p.price_usd.as_ref().map(|v| f32::from(*v)))),
        NumField::PriceEur           => printing.map_or(NumVal::PDep, |p| known(p.price_eur.as_ref().map(|v| f32::from(*v)))),
        NumField::PriceTix           => printing.map_or(NumVal::PDep, |p| known(p.price_tix.as_ref().map(|v| f32::from(*v)))),
        NumField::PreferScore        => printing.map_or(NumVal::PDep, |p| known(p.prefer_score.as_ref().map(|v| f32::from(*v)))),
    }
}

pub(crate) enum NumExpr {
    Const(f64),
    Field(NumField),
    Arith(Box<NumExpr>, ArithOp, Box<NumExpr>),
}

impl NumExpr {
    fn eval(&self, card: &AOracleCard, printing: Option<&APrinting>) -> NumVal {
        match self {
            NumExpr::Const(v) => NumVal::Known(*v),
            NumExpr::Field(f) => field_num(card, printing, *f),
            NumExpr::Arith(lhs, op, rhs) => {
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
        pips: HashMap<String, u8>,
        cmc: f32,
    },

    Devotion {
        op: CmpOp,
        pips: HashMap<String, u8>,
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
    pub(crate) fn bind(
        &mut self,
        vocab: &AStrings,
        sorted_ids: &rkyv::Archived<Vec<u16>>,
        artist_vocab: &AStrings,
        flavor: &rkyv::Archived<FlavorIndex>,
        strings: &AStrings,
    ) {
        match self {
            FilterExpr::And(children) | FilterExpr::Or(children) => {
                for c in children {
                    c.bind(vocab, sorted_ids, artist_vocab, flavor, strings);
                }
            }
            FilterExpr::Not(inner) => inner.bind(vocab, sorted_ids, artist_vocab, flavor, strings),
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
        if residual_is_or {
            residual.iter().any(|c| c.tri(card, Some(printing), strings) == Tri::True)
        } else {
            residual.iter().all(|c| c.tri(card, Some(printing), strings) == Tri::True)
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

            FilterExpr::ExactName(lower) => tri_bool(card.card_name_lower.as_str() == lower.as_str()),

            FilterExpr::NumericCmp { lhs, op, rhs } => {
                match (lhs.eval(card, printing), rhs.eval(card, printing)) {
                    (NumVal::Null, _) | (_, NumVal::Null) => Tri::Null, // missing field: SQL NULL
                    (NumVal::PDep, _) | (_, NumVal::PDep) => Tri::PrintingDep,
                    (NumVal::Known(a), NumVal::Known(b)) => tri_bool(cmp(*op, a, b)),
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

            FilterExpr::FlavorMatch { gids, .. } => {
                let Some(p) = printing else { return Tri::PrintingDep };
                let gid = u32::from(p.flavor_text_lower_id);
                if gid == NONE_STR {
                    Tri::Null // no flavor text: SQL NULL, matching the pre-bind semantics
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
                    CmpOp::Ge => bits & mask == *mask,
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

            FilterExpr::ManaCostCmp { op, pips, cmc } => {
                let card_cmc = f32::from(card.mana_cost.cmc);
                let card_pips = &card.mana_cost.pips;
                tri_bool(match op {
                    CmpOp::Ge => {
                        pips.iter().all(|(sym, &n)| {
                            card_pips.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) >= n
                        }) && card_cmc >= *cmc
                    }
                    CmpOp::Le => {
                        card_pips.iter().all(|(sym, n)| {
                            pips.get(sym.as_str()).copied().unwrap_or(0) >= u8::from(*n)
                        }) && card_cmc <= *cmc
                    }
                    CmpOp::Eq => {
                        card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| {
                                card_pips.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) == n
                            })
                    }
                    CmpOp::Gt => {
                        let contains = pips.iter().all(|(sym, &n)| {
                            card_pips.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) >= n
                        }) && card_cmc >= *cmc;
                        let exact = card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| {
                                card_pips.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) == n
                            });
                        contains && !exact
                    }
                    CmpOp::Lt => {
                        let subset = card_pips.iter().all(|(sym, n)| {
                            pips.get(sym.as_str()).copied().unwrap_or(0) >= u8::from(*n)
                        }) && card_cmc <= *cmc;
                        let exact = card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| {
                                card_pips.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) == n
                            });
                        subset && !exact
                    }
                    CmpOp::Ne => {
                        !(card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| {
                                card_pips.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) == n
                            }))
                    }
                })
            }

            FilterExpr::Devotion { op, pips } => {
                // Mirrors the SQL path's JSONB containment on the devotion column
                // (devotion @> query, <@, =, and the strict/negated variants):
                // per-color positional arrays contain each other iff the counts
                // compare, so containment reduces to count comparisons here.
                // The card-side map can carry non-color keys (generic pips like "1"
                // come straight from mana_cost_jsonb) that the DB's devotion column
                // never holds, so only WUBRGC entries participate — query pips are
                // already color-only (see build_binary).
                let devotion = if card.mana_cost.devotion.is_some() {
                    card.mana_cost.devotion.as_ref().unwrap()
                } else {
                    &card.mana_cost.pips
                };
                let ge = pips.iter().all(|(sym, &n)| {
                    devotion.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) >= n
                });
                let le = devotion.iter()
                    .filter(|(sym, _)| is_devotion_sym(sym.as_str()))
                    .all(|(sym, n)| pips.get(sym.as_str()).copied().unwrap_or(0) >= u8::from(*n));
                let eq = devotion.keys().filter(|sym| is_devotion_sym(sym.as_str())).count() == pips.len()
                    && pips.iter().all(|(sym, &n)| {
                        devotion.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) == n
                    });
                tri_bool(match op {
                    CmpOp::Ge => ge,
                    CmpOp::Eq => eq,
                    CmpOp::Le => le,
                    CmpOp::Gt => ge && !eq,
                    CmpOp::Lt => le && !eq,
                    CmpOp::Ne => !eq,
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
        let pips = mana_pip_counts(mana_str);
        let cmc  = mana_cmc(mana_str);
        let cmp_op = match op { ":" => CmpOp::Ge, _ => str_op_to_cmp(op)? };
        return Ok(FilterExpr::ManaCostCmp { op: cmp_op, pips, cmc });
    }

    if attr == "devotion" {
        let mana_str = rhs_value_str(rhs);
        // Split hybrid symbols ({R/G} -> R:1, G:1) and keep only WUBRGC, matching
        // calculate_devotion() in SQL (which counts only color characters).
        // mana_pip_counts is NOT used directly because it keeps hybrids as single keys.
        let mut pips: HashMap<String, u8> = HashMap::new();
        for (sym, n) in mana_pip_counts(mana_str) {
            if sym.contains('/') {
                for part in sym.split('/') {
                    if is_devotion_sym(part) {
                        *pips.entry(part.to_string()).or_insert(0) += n;
                    }
                }
            } else if is_devotion_sym(&sym) {
                *pips.entry(sym).or_insert(0) += n;
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
