use std::collections::HashMap;
use regex::Regex;
use serde_json::Value;
use super::{ACard, AStrings, str_at, is_devotion_sym, mana_pip_counts, mana_cmc, color_list_to_mask, card_type_str_to_bit};
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

impl NumField {
    pub(crate) fn is_card_level(self) -> bool {
        matches!(self, NumField::Cmc | NumField::Power | NumField::Toughness | NumField::Loyalty | NumField::EdhrEc)
    }
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

fn card_num(card: &ACard, f: NumField) -> Option<f32> {
    match f {
        NumField::Cmc                => card.cmc.as_ref().map(|v| u8::from(*v) as f32),
        NumField::Power              => card.creature_power.as_ref().map(|v| i8::from(*v) as f32),
        NumField::Toughness          => card.creature_toughness.as_ref().map(|v| i8::from(*v) as f32),
        NumField::Loyalty            => card.planeswalker_loyalty.as_ref().map(|v| u8::from(*v) as f32),
        NumField::RarityInt          => card.card_rarity_int.as_ref().map(|v| u8::from(*v) as f32),
        NumField::CollectorNumberInt => card.collector_number_int.as_ref().map(|v| u16::from(*v) as f32),
        NumField::EdhrEc             => card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32),
        NumField::PriceUsd           => card.price_usd.as_ref().map(|v| f32::from(*v)),
        NumField::PriceEur           => card.price_eur.as_ref().map(|v| f32::from(*v)),
        NumField::PriceTix           => card.price_tix.as_ref().map(|v| f32::from(*v)),
        NumField::PreferScore        => card.prefer_score.as_ref().map(|v| f32::from(*v)),
    }
}

pub(crate) enum NumExpr {
    Const(f64),
    Field(NumField),
    Arith(Box<NumExpr>, ArithOp, Box<NumExpr>),
}

impl NumExpr {
    pub(crate) fn eval(&self, card: &ACard) -> Option<f64> {
        match self {
            NumExpr::Const(v) => Some(*v),
            NumExpr::Field(f) => card_num(card, *f).map(|v| v as f64),
            NumExpr::Arith(lhs, op, rhs) => {
                let l = lhs.eval(card)?;
                let r = rhs.eval(card)?;
                Some(match op {
                    ArithOp::Add => l + r,
                    ArithOp::Sub => l - r,
                    ArithOp::Mul => l * r,
                    ArithOp::Div => {
                        if r == 0.0 { return None; }
                        l / r
                    }
                })
            }
        }
    }

    pub(crate) fn is_card_level(&self) -> bool {
        match self {
            NumExpr::Const(_) => true,
            NumExpr::Field(f) => f.is_card_level(),
            NumExpr::Arith(lhs, _, rhs) => lhs.is_card_level() && rhs.is_card_level(),
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

fn card_colors(card: &ACard, f: ColorField) -> u8 {
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

impl CollField {
    pub(crate) fn is_card_level(self) -> bool {
        !matches!(self, CollField::ArtTags | CollField::FrameData | CollField::IsTags)
    }
}

/// Collections are interned vocab ids (see VocabInterner); resolving an element
/// for comparison is an index into the archived coll_vocab table.
fn card_collection<'a>(card: &'a ACard, f: CollField) -> &'a rkyv::vec::ArchivedVec<rkyv::rend::u16_le> {
    match f {
        CollField::Subtypes   => &card.card_subtypes,
        CollField::Keywords   => &card.card_keywords,
        CollField::OracleTags => &card.card_oracle_tags,
        CollField::ArtTags    => &card.card_art_tags,
        CollField::IsTags     => &card.card_is_tags,
        CollField::FrameData  => &card.card_frame_data,
    }
}

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum TextSearchField {
    NameLower,
    OracleTextLower,
    FlavorTextLower,
    ArtistLower,
}

impl TextSearchField {
    pub(crate) fn is_card_level(self) -> bool {
        matches!(self, TextSearchField::NameLower | TextSearchField::OracleTextLower)
    }
}

fn text_search_field_value<'a>(card: &'a ACard, strings: &'a AStrings, field: TextSearchField) -> Option<&'a str> {
    match field {
        TextSearchField::NameLower       => Some(card.card_name_lower.as_str()),
        TextSearchField::OracleTextLower => str_at(strings, u32::from(card.oracle_text_lower_id)),
        TextSearchField::FlavorTextLower => str_at(strings, u32::from(card.flavor_text_lower_id)),
        TextSearchField::ArtistLower     => str_at(strings, u32::from(card.card_artist_lower_id)),
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

impl TextField {
    fn is_card_level(self) -> bool {
        matches!(self, TextField::NameLower | TextField::OracleTextLower)
    }
}

fn text_field_value<'a>(card: &'a ACard, strings: &'a AStrings, field: TextField) -> Option<&'a str> {
    match field {
        TextField::NameLower       => Some(card.card_name_lower.as_str()),
        TextField::OracleTextLower => str_at(strings, u32::from(card.oracle_text_lower_id)),
        TextField::FlavorTextLower => str_at(strings, u32::from(card.flavor_text_lower_id)),
        TextField::ArtistLower     => str_at(strings, u32::from(card.card_artist_lower_id)),
        TextField::SetCode         => Some(card.card_set_code.as_str()),
        TextField::Layout          => str_at(strings, u32::from(card.card_layout_id)),
        TextField::Border          => str_at(strings, u32::from(card.card_border_id)),
        TextField::Watermark       => str_at(strings, u32::from(card.card_watermark_id)),
        TextField::CollectorNumber => str_at(strings, u32::from(card.collector_number_id)),
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

impl FilterExpr {
    pub(crate) fn matches(&self, card: &ACard, strings: &AStrings, vocab: &AStrings) -> bool {
        self.tri(card, strings, vocab) == Some(true)
    }

    /// Three-valued evaluation mirroring SQL: None is SQL's NULL ("unknown"),
    /// produced when a compared field is missing from the card. NOT/AND/OR
    /// propagate unknown exactly like SQL ternary logic, so -power>2 excludes
    /// powerless cards (NOT NULL = NULL), matching Scryfall's "attribute
    /// filters only match cards that have the attribute", while
    /// -(power>2 and t:creature) still matches instants (NULL AND false =
    /// false, NOT false = true). Only Some(true) counts as a match.
    fn tri(&self, card: &ACard, strings: &AStrings, vocab: &AStrings) -> Option<bool> {
        match self {
            FilterExpr::True => Some(true),

            FilterExpr::And(children) => {
                let mut unknown = false;
                for c in children {
                    match c.tri(card, strings, vocab) {
                        Some(false) => return Some(false),
                        None => unknown = true,
                        Some(true) => {}
                    }
                }
                if unknown { None } else { Some(true) }
            }
            FilterExpr::Or(children) => {
                let mut unknown = false;
                for c in children {
                    match c.tri(card, strings, vocab) {
                        Some(true) => return Some(true),
                        None => unknown = true,
                        Some(false) => {}
                    }
                }
                if unknown { None } else { Some(false) }
            }
            FilterExpr::Not(inner) => inner.tri(card, strings, vocab).map(|b| !b),

            FilterExpr::ExactName(lower) => Some(card.card_name_lower.as_str() == lower.as_str()),

            FilterExpr::NumericCmp { lhs, op, rhs } => {
                match (lhs.eval(card), rhs.eval(card)) {
                    (Some(a), Some(b)) => Some(cmp(*op, a, b)),
                    _ => None, // a compared field is missing: SQL NULL
                }
            }

            FilterExpr::TextContains { field, word } => {
                text_search_field_value(card, strings, *field).map(|s| s.contains(word.as_str()))
            }

            FilterExpr::TextExact { field, op, value } => {
                text_field_value(card, strings, *field).map(|s| match op {
                    CmpOp::Eq => s == value,
                    CmpOp::Ne => s != value,
                    CmpOp::Lt => s < value.as_str(),
                    CmpOp::Le => s <= value.as_str(),
                    CmpOp::Gt => s > value.as_str(),
                    CmpOp::Ge => s >= value.as_str(),
                })
            }

            FilterExpr::TextRegex { field, regex } => {
                text_field_value(card, strings, *field).map(|s| regex.is_match(s))
            }

            FilterExpr::ColorCmp { field, op, mask } => {
                let bits = card_colors(card, *field);
                Some(match op {
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
                Some(match op {
                    CmpOp::Ge => bits & mask != 0,
                    CmpOp::Eq => bits == *mask,
                    CmpOp::Le => bits & !mask == 0,
                    CmpOp::Lt => bits & !mask == 0 && bits != *mask,
                    CmpOp::Gt => bits & mask != 0 && bits != *mask,
                    CmpOp::Ne => bits != *mask,
                })
            }

            FilterExpr::CollectionCmp { field, op, value } => {
                // Set-containment semantics against the single-value query {value},
                // mirroring the SQL path's jsonb operators (@>, <@, =, <> and the
                // strict variants). Lt (proper subset of a one-element set) can only
                // be the empty collection; Ne is not-exactly-equal, NOT "lacks value"
                // (that's what negation is for).
                let coll = card_collection(card, *field);
                let elem = |id: &rkyv::rend::u16_le| vocab[u16::from(*id) as usize].as_str();
                let contains = || coll.iter().any(|id| elem(id) == value.as_str());
                Some(match op {
                    CmpOp::Ge => contains(),
                    CmpOp::Eq => coll.len() == 1 && contains(),
                    CmpOp::Gt => contains() && coll.len() > 1,
                    CmpOp::Le => coll.iter().all(|id| elem(id) == value.as_str()),
                    CmpOp::Lt => coll.len() == 0,
                    CmpOp::Ne => !(coll.len() == 1 && contains()),
                })
            }

            FilterExpr::Legality { shift, expected } => {
                Some(shift.is_some_and(|s| (u64::from(card.card_legalities) >> s) & 0b11 == *expected))
            }

            FilterExpr::ManaCostCmp { op, pips, cmc } => {
                let card_cmc = f32::from(card.mana_cost.cmc);
                let card_pips = &card.mana_cost.pips;
                Some(match op {
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
                Some(match op {
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
                let Some(date) = card.released_at_int.as_ref().map(|v| u32::from(*v)) else {
                    return None; // missing date: SQL NULL
                };
                Some(match op {
                    CmpOp::Eq => date == *value,
                    CmpOp::Ne => date != *value,
                    CmpOp::Lt => date < *value,
                    CmpOp::Le => date <= *value,
                    CmpOp::Gt => date > *value,
                    CmpOp::Ge => date >= *value,
                })
            }

            FilterExpr::YearCmp { op, year } => {
                let Some(date) = card.released_at_int.as_ref().map(|v| u32::from(*v)) else {
                    return None; // missing date: SQL NULL
                };
                let card_year = (date / 10_000) as i32;
                Some(match op {
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

    /// Returns true if every leaf predicate touches only card-level attributes
    /// (constant across all printings of the same oracle id). When true for a
    /// `unique=card, prefer=default` query, the preferred-printing index covers
    /// the full result set with no dedup needed.
    pub(crate) fn is_card_level(&self) -> bool {
        match self {
            FilterExpr::True | FilterExpr::ExactName(_) => true,
            FilterExpr::And(c) | FilterExpr::Or(c) => c.iter().all(|x| x.is_card_level()),
            FilterExpr::Not(inner) => inner.is_card_level(),
            FilterExpr::NumericCmp { lhs, rhs, .. } => lhs.is_card_level() && rhs.is_card_level(),
            FilterExpr::TextContains { field, .. } => field.is_card_level(),
            FilterExpr::TextExact   { field, .. } => field.is_card_level(),
            FilterExpr::TextRegex   { field, .. } => field.is_card_level(),
            FilterExpr::ColorCmp { .. } | FilterExpr::TypeCmp { .. } => true,
            FilterExpr::CollectionCmp { field, .. } => field.is_card_level(),
            FilterExpr::Legality { .. } | FilterExpr::ManaCostCmp { .. } | FilterExpr::Devotion { .. } => true,
            FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => false,
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
        return Ok(FilterExpr::CollectionCmp { field: CollField::Subtypes, op: op_to_collection_cmp(op), value });
    }

    if attr == "card_keywords" {
        let value  = rhs.as_array().and_then(|a| a.first()).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let cmp_op = op_to_collection_cmp(op);
        return Ok(FilterExpr::CollectionCmp { field: CollField::Keywords, op: cmp_op, value });
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
        return Ok(FilterExpr::CollectionCmp { field: coll_field, op: cmp_op, value });
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
