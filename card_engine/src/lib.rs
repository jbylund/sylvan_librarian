use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use regex::Regex;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};

// ─── Color bits (W=1 U=2 B=4 R=8 G=16 C=32) ────────────────────────────────

fn color_to_bit(c: &str) -> u8 {
    match c {
        "W" => 1,
        "U" => 2,
        "B" => 4,
        "R" => 8,
        "G" => 16,
        "C" => 32,
        _ => 0,
    }
}

fn color_list_to_mask(colors: &[&str]) -> u8 {
    colors.iter().fold(0u8, |acc, c| acc | color_to_bit(c))
}

// ─── Mana cost helpers ───────────────────────────────────────────────────────

/// Count colored pip occurrences from a mana cost string like "{R}{R}{G}" or "rr".
fn mana_pip_counts(s: &str) -> HashMap<String, u8> {
    let mut pips: HashMap<String, u8> = HashMap::new();
    let upper = s.to_uppercase();
    let mut in_brace = false;
    let mut sym = String::new();
    for c in upper.chars() {
        match c {
            '{' => {
                in_brace = true;
                sym.clear();
            }
            '}' => {
                if in_brace && sym.parse::<u32>().is_err() && sym != "X" {
                    *pips.entry(sym.clone()).or_insert(0) += 1;
                }
                in_brace = false;
            }
            _ if in_brace => sym.push(c),
            _ if "WUBRGC".contains(c) => {
                *pips.entry(c.to_string()).or_insert(0) += 1;
            }
            _ => {}
        }
    }
    pips
}

fn mana_cmc(s: &str) -> f32 {
    let upper = s.to_uppercase();
    let mut cmc = 0.0f32;
    let mut in_brace = false;
    let mut sym = String::new();
    for c in upper.chars() {
        match c {
            '{' => {
                in_brace = true;
                sym.clear();
            }
            '}' => {
                if in_brace {
                    if let Ok(n) = sym.parse::<f32>() {
                        cmc += n;
                    } else if sym != "X" {
                        cmc += 1.0;
                    }
                }
                in_brace = false;
            }
            _ if in_brace => sym.push(c),
            _ if "WUBRGC".contains(c) => cmc += 1.0,
            _ => {}
        }
    }
    cmc
}

// ─── Card struct ─────────────────────────────────────────────────────────────

struct ManaCost {
    pips: HashMap<String, u8>,
    cmc: f32,
}

struct Card {
    scryfall_id: String,
    oracle_id: Option<String>,
    illustration_id: Option<String>,

    card_name: String,
    card_name_lower: String,
    oracle_text: String,
    oracle_text_lower: String,
    flavor_text: String,
    flavor_text_lower: String,
    card_artist: Option<String>,
    card_artist_lower: Option<String>,
    card_set_code: String,
    card_layout: String,
    card_border: String,
    card_watermark: Option<String>,
    collector_number: String,
    mana_cost_text: Option<String>,
    type_line: String,
    set_name: String,
    released_at: String,

    // color bitfields: W=1 U=2 B=4 R=8 G=16 C=32
    card_colors: u8,
    card_color_identity: u8,
    produced_mana: u8,

    cmc: Option<f32>,
    creature_power: Option<f32>,
    creature_toughness: Option<f32>,
    planeswalker_loyalty: Option<f32>,
    card_rarity_int: Option<f32>,
    collector_number_int: Option<f32>,
    edhrec_rank: Option<f32>,
    price_usd: Option<f32>,
    price_eur: Option<f32>,
    price_tix: Option<f32>,
    prefer_score: Option<f32>,
    cubecobra_score: Option<f32>,

    card_types: Vec<String>,
    card_subtypes: Vec<String>,
    card_keywords: Vec<String>,
    card_legalities: HashMap<String, String>,
    card_oracle_tags: HashSet<String>,
    card_is_tags: HashSet<String>,
    card_frame_data: HashSet<String>,

    mana_cost: ManaCost,

    creature_power_text: Option<String>,
    creature_toughness_text: Option<String>,
}

// ─── Loading cards from Python dicts ─────────────────────────────────────────

fn opt_str(d: &Bound<PyDict>, key: &str) -> Option<String> {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| v.extract::<String>().ok())
}

fn opt_f32(d: &Bound<PyDict>, key: &str) -> Option<f32> {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| {
            v.extract::<f64>().ok().map(|n| n as f32)
                .or_else(|| v.extract::<i64>().ok().map(|n| n as f32))
        })
}

fn str_list(d: &Bound<PyDict>, key: &str) -> Vec<String> {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| v.extract::<Vec<String>>().ok())
        .unwrap_or_default()
}

fn jsonb_color_to_bits(d: &Bound<PyDict>, key: &str) -> u8 {
    let colors: Vec<String> = d
        .get_item(key)
        .ok()
        .flatten()
        .and_then(|v| {
            // Value is a Python dict like {"R": True, "G": True}
            v.cast::<PyDict>()
                .ok()
                .map(|m| m.keys().iter().filter_map(|k| k.extract::<String>().ok()).collect())
        })
        .unwrap_or_default();
    color_list_to_mask(&colors.iter().map(|s| s.as_str()).collect::<Vec<_>>())
}

fn jsonb_obj_to_hashset(d: &Bound<PyDict>, key: &str) -> HashSet<String> {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| {
            v.cast::<PyDict>()
                .ok()
                .map(|m| m.keys().iter().filter_map(|k| k.extract::<String>().ok()).collect())
        })
        .unwrap_or_default()
}

fn jsonb_obj_to_string_map(d: &Bound<PyDict>, key: &str) -> HashMap<String, String> {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| {
            v.cast::<PyDict>().ok().map(|m| {
                m.iter()
                    .filter_map(|(k, v)| {
                        let key = k.extract::<String>().ok()?;
                        let val = v.extract::<String>().ok()?;
                        Some((key, val))
                    })
                    .collect()
            })
        })
        .unwrap_or_default()
}

fn mana_cost_from_pydict(d: &Bound<PyDict>, cmc_val: Option<f32>) -> ManaCost {
    // mana_cost_jsonb is {"R": [1, 2], "G": [1]} — pip count = len of each list
    let pips: HashMap<String, u8> = d
        .get_item("mana_cost_jsonb")
        .ok()
        .flatten()
        .and_then(|v| {
            v.cast::<PyDict>().ok().map(|m| {
                m.iter()
                    .filter_map(|(k, v)| {
                        let sym = k.extract::<String>().ok()?;
                        let count = v
                            .cast::<PyList>()
                            .ok()
                            .map(|l| l.len() as u8)
                            .unwrap_or(0);
                        Some((sym, count))
                    })
                    .collect()
            })
        })
        .unwrap_or_default();
    ManaCost {
        pips,
        cmc: cmc_val.unwrap_or(0.0),
    }
}

fn card_from_pydict(d: &Bound<PyDict>) -> Card {
    let cmc = opt_f32(d, "cmc");
    let card_name = opt_str(d, "card_name").unwrap_or_default();
    let card_name_lower = card_name.to_lowercase();
    let oracle_text = opt_str(d, "oracle_text").unwrap_or_default();
    let oracle_text_lower = oracle_text.to_lowercase();
    let flavor_text = opt_str(d, "flavor_text").unwrap_or_default();
    let flavor_text_lower = flavor_text.to_lowercase();
    let card_artist = opt_str(d, "card_artist");
    let card_artist_lower = card_artist.as_ref().map(|s| s.to_lowercase());

    Card {
        scryfall_id: opt_str(d, "scryfall_id").unwrap_or_default(),
        oracle_id: opt_str(d, "oracle_id"),
        illustration_id: opt_str(d, "illustration_id"),

        card_name_lower: card_name_lower.clone(),
        card_name,
        oracle_text_lower: oracle_text_lower.clone(),
        oracle_text,
        flavor_text_lower: flavor_text_lower.clone(),
        flavor_text,
        card_artist_lower: card_artist_lower.clone(),
        card_artist,
        card_set_code: opt_str(d, "card_set_code").unwrap_or_default(),
        card_layout: opt_str(d, "card_layout").unwrap_or_default(),
        card_border: opt_str(d, "card_border").unwrap_or_default(),
        card_watermark: opt_str(d, "card_watermark"),
        collector_number: opt_str(d, "collector_number").unwrap_or_default(),
        mana_cost_text: opt_str(d, "mana_cost_text"),
        type_line: opt_str(d, "type_line").unwrap_or_default(),
        set_name: opt_str(d, "set_name").unwrap_or_default(),
        released_at: opt_str(d, "released_at").unwrap_or_default(),

        card_colors: jsonb_color_to_bits(d, "card_colors"),
        card_color_identity: jsonb_color_to_bits(d, "card_color_identity"),
        produced_mana: jsonb_color_to_bits(d, "produced_mana"),

        cmc,
        creature_power: opt_f32(d, "creature_power"),
        creature_toughness: opt_f32(d, "creature_toughness"),
        planeswalker_loyalty: opt_f32(d, "planeswalker_loyalty"),
        card_rarity_int: opt_f32(d, "card_rarity_int"),
        collector_number_int: opt_f32(d, "collector_number_int"),
        edhrec_rank: opt_f32(d, "edhrec_rank"),
        price_usd: opt_f32(d, "price_usd"),
        price_eur: opt_f32(d, "price_eur"),
        price_tix: opt_f32(d, "price_tix"),
        prefer_score: opt_f32(d, "prefer_score"),
        cubecobra_score: opt_f32(d, "cubecobra_score"),

        card_types: str_list(d, "card_types"),
        card_subtypes: str_list(d, "card_subtypes"),
        card_keywords: str_list(d, "card_keywords"),
        card_legalities: jsonb_obj_to_string_map(d, "card_legalities"),
        card_oracle_tags: jsonb_obj_to_hashset(d, "card_oracle_tags"),
        card_is_tags: jsonb_obj_to_hashset(d, "card_is_tags"),
        card_frame_data: jsonb_obj_to_hashset(d, "card_frame_data"),

        mana_cost: mana_cost_from_pydict(d, cmc),

        creature_power_text: opt_str(d, "creature_power_text"),
        creature_toughness_text: opt_str(d, "creature_toughness_text"),
    }
}

// ─── Filter expression ────────────────────────────────────────────────────────

#[derive(Clone, Copy, PartialEq)]
enum CmpOp {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
}

#[derive(Clone, Copy)]
enum ArithOp {
    Add,
    Sub,
    Mul,
    Div,
}

#[derive(Clone, Copy)]
enum NumField {
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
        "cmc" => Some(NumField::Cmc),
        "creature_power" => Some(NumField::Power),
        "creature_toughness" => Some(NumField::Toughness),
        "planeswalker_loyalty" => Some(NumField::Loyalty),
        "card_rarity_int" => Some(NumField::RarityInt),
        "collector_number_int" => Some(NumField::CollectorNumberInt),
        "edhrec_rank" => Some(NumField::EdhrEc),
        "price_usd" => Some(NumField::PriceUsd),
        "price_eur" => Some(NumField::PriceEur),
        "price_tix" => Some(NumField::PriceTix),
        "prefer_score" => Some(NumField::PreferScore),
        _ => None,
    }
}

fn card_num(card: &Card, f: NumField) -> Option<f32> {
    match f {
        NumField::Cmc => card.cmc,
        NumField::Power => card.creature_power,
        NumField::Toughness => card.creature_toughness,
        NumField::Loyalty => card.planeswalker_loyalty,
        NumField::RarityInt => card.card_rarity_int,
        NumField::CollectorNumberInt => card.collector_number_int,
        NumField::EdhrEc => card.edhrec_rank,
        NumField::PriceUsd => card.price_usd,
        NumField::PriceEur => card.price_eur,
        NumField::PriceTix => card.price_tix,
        NumField::PreferScore => card.prefer_score,
    }
}

enum NumExpr {
    Const(f64),
    Field(NumField),
    Arith(Box<NumExpr>, ArithOp, Box<NumExpr>),
}

impl NumExpr {
    fn eval(&self, card: &Card) -> Option<f64> {
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
                        if r == 0.0 {
                            return None;
                        }
                        l / r
                    }
                })
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

#[derive(Clone, Copy)]
enum ColorField {
    Colors,
    ColorIdentity,
    ProducedMana,
}

fn card_colors(card: &Card, f: ColorField) -> u8 {
    match f {
        ColorField::Colors => card.card_colors,
        ColorField::ColorIdentity => card.card_color_identity,
        ColorField::ProducedMana => card.produced_mana,
    }
}

#[derive(Clone, Copy)]
enum CollField {
    Types,
    Subtypes,
    Keywords,
    OracleTags,
    IsTags,
    FrameData,
}

fn card_collection<'a>(card: &'a Card, f: CollField) -> CollRef<'a> {
    match f {
        CollField::Types => CollRef::List(&card.card_types),
        CollField::Subtypes => CollRef::List(&card.card_subtypes),
        CollField::Keywords => CollRef::List(&card.card_keywords),
        CollField::OracleTags => CollRef::Set(&card.card_oracle_tags),
        CollField::IsTags => CollRef::Set(&card.card_is_tags),
        CollField::FrameData => CollRef::Set(&card.card_frame_data),
    }
}

enum CollRef<'a> {
    List(&'a Vec<String>),
    Set(&'a HashSet<String>),
}

impl CollRef<'_> {
    fn contains(&self, v: &str) -> bool {
        match self {
            CollRef::List(l) => l.iter().any(|s| s == v),
            CollRef::Set(s) => s.contains(v),
        }
    }
    fn len(&self) -> usize {
        match self {
            CollRef::List(l) => l.len(),
            CollRef::Set(s) => s.len(),
        }
    }
    fn all_equal(&self, v: &str) -> bool {
        match self {
            CollRef::List(l) => !l.is_empty() && l.iter().all(|s| s == v),
            CollRef::Set(s) => s.len() == 1 && s.contains(v),
        }
    }
}

/// Compiled, ready-to-evaluate filter.
enum FilterExpr {
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

    // `:` on text fields → substring against pre-lowercased field
    TextContains {
        lower_field: fn(&Card) -> Option<&str>,
        word: String,
    },
    // `=` or other ops on text fields → exact match (case-sensitive)
    TextExact {
        field: fn(&Card) -> Option<&str>,
        op: CmpOp,
        value: String,
    },
    // `/regex/` on text fields → case-insensitive regex against original field
    TextRegex {
        field: fn(&Card) -> Option<&str>,
        regex: Regex,
    },

    // Color bitmask comparison.
    // `:` and `>=` → contains (card_mask & query_mask == query_mask)
    // `=` → exact  (card_mask == query_mask)
    // `<=` → subset (card_mask & !query_mask == 0)
    // `<`  → proper subset
    // `>`  → proper superset
    ColorCmp {
        field: ColorField,
        op: CmpOp,
        mask: u8,
    },

    // Collection (Vec or HashSet) membership check.
    // `:` / `>=` → contains value
    // `=` → exactly one element matching
    // `>` → contains value AND more than one element (proper superset of {value})
    // `<=` → all elements match (subset of {value})
    // `<` → all elements match AND card has exactly 0 (impossible for non-empty → always false)
    CollectionCmp {
        field: CollField,
        op: CmpOp,
        value: String,
    },

    // Legality: check card_legalities[format] == expected_status.
    Legality {
        format: String,
        expected: &'static str,
    },

    // Mana cost comparison: pip containment + CMC check.
    // `:` / `>=`: card has ≥ pips of each color AND card.cmc ≥ query_cmc
    // `=`: exact pip match AND cmc match
    // `<=`: card pips ⊆ query pips AND card.cmc ≤ query_cmc
    // `<` / `>`: as above with strict inequality
    ManaCostCmp {
        op: CmpOp,
        pips: HashMap<String, u8>,
        cmc: f32,
    },

    // Devotion: pip containment only (no CMC check).
    // `:` means card.mana_cost contains ≥ the queried pips
    Devotion {
        pips: HashMap<String, u8>,
    },

    // Date: lexicographic comparison of "YYYY-MM-DD" strings.
    DateCmp {
        op: CmpOp,
        value: String,
    },

    // Year expansion: `:` / `=` → [YYYY-01-01, YYYY+1-01-01)
    // Other ops follow the table in the issue doc.
    YearCmp {
        op: CmpOp,
        year: i32,
    },
}

impl FilterExpr {
    fn matches(&self, card: &Card) -> bool {
        match self {
            FilterExpr::True => true,

            FilterExpr::And(children) => children.iter().all(|c| c.matches(card)),
            FilterExpr::Or(children) => children.iter().any(|c| c.matches(card)),
            FilterExpr::Not(inner) => !inner.matches(card),

            FilterExpr::ExactName(lower) => card.card_name_lower == *lower,

            FilterExpr::NumericCmp { lhs, op, rhs } => {
                let l = lhs.eval(card);
                let r = rhs.eval(card);
                match (l, r) {
                    (Some(a), Some(b)) => cmp(*op, a, b),
                    _ => false,
                }
            }

            FilterExpr::TextContains { lower_field, word } => {
                lower_field(card).map_or(false, |s| s.contains(word.as_str()))
            }

            FilterExpr::TextExact { field, op, value } => {
                field(card).map_or(false, |s| cmp(*op, 0.0, if s == value { 0.0 } else { 1.0 }))
            }

            FilterExpr::TextRegex { field, regex } => {
                field(card).map_or(false, |s| regex.is_match(s))
            }

            FilterExpr::ColorCmp { field, op, mask } => {
                let bits = card_colors(card, *field);
                match op {
                    CmpOp::Ge => bits & mask == *mask,
                    CmpOp::Eq => bits == *mask,
                    CmpOp::Le => bits & !mask == 0,
                    CmpOp::Lt => bits & !mask == 0 && bits != *mask,
                    CmpOp::Gt => bits & mask == *mask && bits != *mask,
                    CmpOp::Ne => bits != *mask,
                }
            }

            FilterExpr::CollectionCmp { field, op, value } => {
                let coll = card_collection(card, *field);
                match op {
                    CmpOp::Ge => coll.contains(value),
                    CmpOp::Eq => coll.len() == 1 && coll.contains(value),
                    CmpOp::Gt => coll.contains(value) && coll.len() > 1,
                    CmpOp::Le => coll.all_equal(value),
                    CmpOp::Lt => false, // proper subset of single-element set: impossible
                    CmpOp::Ne => !coll.contains(value),
                }
            }

            FilterExpr::Legality { format, expected } => {
                card.card_legalities.get(format.as_str()).map_or(false, |v| v == *expected)
            }

            FilterExpr::ManaCostCmp { op, pips, cmc } => {
                let card_cmc = card.mana_cost.cmc;
                let card_pips = &card.mana_cost.pips;
                match op {
                    // >=: card has ≥ queried pips of each color AND card cmc ≥ query cmc
                    CmpOp::Ge => {
                        pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc >= *cmc
                    }
                    // <=: card pips ⊆ query pips AND card cmc ≤ query cmc
                    CmpOp::Le => {
                        card_pips
                            .iter()
                            .all(|(sym, &n)| pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc <= *cmc
                    }
                    CmpOp::Eq => {
                        card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips
                                .iter()
                                .all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n)
                    }
                    CmpOp::Gt => {
                        // card @> query AND card != query
                        let contains = pips
                            .iter()
                            .all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc >= *cmc;
                        let exact = card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips
                                .iter()
                                .all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n);
                        contains && !exact
                    }
                    CmpOp::Lt => {
                        let subset = card_pips
                            .iter()
                            .all(|(sym, &n)| pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc <= *cmc;
                        let exact = card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips
                                .iter()
                                .all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n);
                        subset && !exact
                    }
                    CmpOp::Ne => {
                        !(card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips
                                .iter()
                                .all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n))
                    }
                }
            }

            FilterExpr::Devotion { pips } => {
                let card_pips = &card.mana_cost.pips;
                pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) >= n)
            }

            FilterExpr::DateCmp { op, value } => {
                if card.released_at.is_empty() {
                    return false;
                }
                cmp(*op, 0.0, {
                    let ord = card.released_at.as_str().cmp(value.as_str());
                    // map Ordering to a float so we can reuse cmp()
                    match op {
                        CmpOp::Eq => if ord == std::cmp::Ordering::Equal { 0.0 } else { 1.0 },
                        CmpOp::Ne => if ord != std::cmp::Ordering::Equal { 0.0 } else { 1.0 },
                        CmpOp::Lt => if ord == std::cmp::Ordering::Less { 0.0 } else { 1.0 },
                        CmpOp::Le => if ord != std::cmp::Ordering::Greater { 0.0 } else { 1.0 },
                        CmpOp::Gt => if ord == std::cmp::Ordering::Greater { 0.0 } else { 1.0 },
                        CmpOp::Ge => if ord != std::cmp::Ordering::Less { 0.0 } else { 1.0 },
                    }
                })
            }

            FilterExpr::YearCmp { op, year } => {
                if card.released_at.is_empty() {
                    return false;
                }
                let s = &card.released_at;
                let start = format!("{year:04}-01-01");
                let end = format!("{:04}-01-01", year + 1);
                match op {
                    CmpOp::Eq => s.as_str() >= start.as_str() && s.as_str() < end.as_str(),
                    CmpOp::Gt => s.as_str() >= end.as_str(),
                    CmpOp::Lt => s.as_str() < start.as_str(),
                    CmpOp::Ge => s.as_str() >= start.as_str(),
                    CmpOp::Le => s.as_str() < end.as_str(),
                    CmpOp::Ne => s.as_str() < start.as_str() || s.as_str() >= end.as_str(),
                }
            }
        }
    }
}

// ─── TextExact helper: just compare strings directly ─────────────────────────

// (TextExact uses a closure trick with cmp() which is awkward; replace with a
//  simpler direct check in FilterExpr::matches above)
// Actually TextExact is cleaner if we just inline the comparison:

// ─── Building FilterExpr from JSON ───────────────────────────────────────────

fn str_op_to_cmp(s: &str) -> Result<CmpOp, String> {
    match s {
        "=" | ":" => Ok(CmpOp::Eq),
        "!=" => Ok(CmpOp::Ne),
        "<" => Ok(CmpOp::Lt),
        "<=" => Ok(CmpOp::Le),
        ">" => Ok(CmpOp::Gt),
        ">=" => Ok(CmpOp::Ge),
        _ => Err(format!("unknown operator: {s}")),
    }
}

fn op_to_collection_cmp(op: &str) -> CmpOp {
    match op {
        ":" | ">=" => CmpOp::Ge,
        "=" => CmpOp::Eq,
        ">" => CmpOp::Gt,
        "<=" => CmpOp::Le,
        "<" => CmpOp::Lt,
        "!=" => CmpOp::Ne,
        _ => CmpOp::Ge,
    }
}

fn op_to_color_cmp(op: &str) -> CmpOp {
    match op {
        ":" | ">=" => CmpOp::Ge,
        "=" => CmpOp::Eq,
        "<=" => CmpOp::Le,
        "<" => CmpOp::Lt,
        ">" => CmpOp::Gt,
        "!=" => CmpOp::Ne,
        _ => CmpOp::Ge,
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

fn build_filter(v: &Value) -> Result<FilterExpr, String> {
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
    let lhs_kw = &lhs["kwargs"];

    // Arithmetic / non-CardAttributeNode lhs → numeric expression
    if lhs_type != "CardAttributeNode" {
        let lhs_expr = build_num_expr(lhs)?;
        let rhs_expr = build_num_expr(rhs)?;
        let cmp_op = str_op_to_cmp(op)?;
        return Ok(FilterExpr::NumericCmp { lhs: lhs_expr, op: cmp_op, rhs: rhs_expr });
    }

    let attr = lhs_kw["attribute_name"].as_str().unwrap_or("");
    let orig = lhs_kw["original_attribute"].as_str().unwrap_or("");

    // ── Numeric fields ────────────────────────────────────────────────────────
    if let Some(num_field) = attr_to_num_field(attr) {
        // `:` for numerics means `=`
        let cmp_op = str_op_to_cmp(op)?;
        let rhs_expr = build_num_expr(rhs)?;
        return Ok(FilterExpr::NumericCmp {
            lhs: NumExpr::Field(num_field),
            op: cmp_op,
            rhs: rhs_expr,
        });
    }

    // ── Date / Year ───────────────────────────────────────────────────────────
    if attr == "released_at" {
        let val_str = rhs_value_str(rhs);
        if orig == "year" {
            let year: i32 = val_str.parse().map_err(|_| format!("bad year: {val_str}"))?;
            // `:` for year means `=`
            let cmp_op = str_op_to_cmp(op)?;
            return Ok(FilterExpr::YearCmp { op: cmp_op, year });
        }
        let cmp_op = str_op_to_cmp(op)?;
        return Ok(FilterExpr::DateCmp { op: cmp_op, value: val_str.to_string() });
    }

    // ── Mana cost ─────────────────────────────────────────────────────────────
    if attr == "mana_cost_jsonb" {
        let mana_str = rhs_value_str(rhs);
        let pips = mana_pip_counts(mana_str);
        let cmc = mana_cmc(mana_str);
        // `:` for mana means `>=`
        let cmp_op = match op {
            ":" => CmpOp::Ge,
            _ => str_op_to_cmp(op)?,
        };
        return Ok(FilterExpr::ManaCostCmp { op: cmp_op, pips, cmc });
    }

    // ── Devotion ──────────────────────────────────────────────────────────────
    if attr == "devotion" {
        let mana_str = rhs_value_str(rhs);
        let pips = mana_pip_counts(mana_str);
        return Ok(FilterExpr::Devotion { pips });
    }

    // ── Colors ────────────────────────────────────────────────────────────────
    if matches!(attr, "card_colors" | "card_color_identity" | "produced_mana") {
        let color_field = match attr {
            "card_colors" => ColorField::Colors,
            "card_color_identity" => ColorField::ColorIdentity,
            _ => ColorField::ProducedMana,
        };
        let color_strs: Vec<&str> = rhs
            .as_array()
            .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
            .unwrap_or_default();
        let mask = color_list_to_mask(&color_strs);
        let cmp_op = op_to_color_cmp(op);
        return Ok(FilterExpr::ColorCmp { field: color_field, op: cmp_op, mask });
    }

    // ── Legality ──────────────────────────────────────────────────────────────
    if attr == "card_legalities" {
        let format = rhs
            .as_array()
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let expected: &'static str = match orig {
            "format" | "f" | "legal" => "legal",
            "banned" => "banned",
            "restricted" => "restricted",
            _ => "legal",
        };
        return Ok(FilterExpr::Legality { format, expected });
    }

    // ── Card types and subtypes ───────────────────────────────────────────────
    if matches!(attr, "card_types" | "card_subtypes") {
        let coll_field = if attr == "card_types" { CollField::Types } else { CollField::Subtypes };
        let value = rhs
            .as_array()
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let cmp_op = op_to_collection_cmp(op);
        return Ok(FilterExpr::CollectionCmp { field: coll_field, op: cmp_op, value });
    }

    // ── Keywords ──────────────────────────────────────────────────────────────
    if attr == "card_keywords" {
        let value = rhs
            .as_array()
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        // `:` for keywords means "has keyword" = Ge
        let cmp_op = op_to_collection_cmp(op);
        return Ok(FilterExpr::CollectionCmp { field: CollField::Keywords, op: cmp_op, value });
    }

    // ── Oracle tags / is-tags / frame data ───────────────────────────────────
    if matches!(attr, "card_oracle_tags" | "card_is_tags" | "card_frame_data") {
        let coll_field = match attr {
            "card_oracle_tags" => CollField::OracleTags,
            "card_is_tags" => CollField::IsTags,
            _ => CollField::FrameData,
        };
        let value = rhs
            .as_array()
            .and_then(|a| a.first())
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let cmp_op = op_to_collection_cmp(op);
        return Ok(FilterExpr::CollectionCmp { field: coll_field, op: cmp_op, value });
    }

    // ── Text fields ───────────────────────────────────────────────────────────
    build_text_filter(attr, op, rhs)
}

fn rhs_value_str<'a>(rhs: &'a Value) -> &'a str {
    // rhs is a node dict; extract the "value" from kwargs
    rhs["kwargs"]["value"].as_str().unwrap_or("")
}

fn build_text_filter(attr: &str, op: &str, rhs: &Value) -> Result<FilterExpr, String> {
    let rhs_node_type = rhs["node_type"].as_str().unwrap_or("");

    // Regex
    if rhs_node_type == "RegexValueNode" {
        let pattern = rhs["kwargs"]["value"].as_str().unwrap_or("");
        let re = Regex::new(&format!("(?i){pattern}"))
            .map_err(|e| format!("invalid regex '{pattern}': {e}"))?;
        let field_fn: fn(&Card) -> Option<&str> = match attr {
            "card_name" => |c| Some(c.card_name.as_str()),
            "oracle_text" => |c| Some(c.oracle_text.as_str()),
            "flavor_text" => |c| Some(c.flavor_text.as_str()),
            "card_artist" => |c| c.card_artist.as_deref(),
            _ => return Err(format!("regex not supported on {attr}")),
        };
        return Ok(FilterExpr::TextRegex { field: field_fn, regex: re });
    }

    let raw_value = rhs["kwargs"]["value"].as_str().unwrap_or("");

    // Fixed-case exact-match fields: set code, layout, border, watermark, collector number.
    if matches!(attr, "card_set_code" | "card_layout" | "card_border" | "card_watermark" | "collector_number") {
        let value = raw_value.to_lowercase();
        // For these fields, any op (including `:`) is exact match
        let cmp_op = str_op_to_cmp(op)?;
        // We compare lowercased card value against lowercased query
        let lower_fn: fn(&Card) -> Option<&str> = match attr {
            "card_set_code" => |c| Some(c.card_set_code.as_str()),
            "card_layout" => |c| Some(c.card_layout.as_str()),
            "card_border" => |c| Some(c.card_border.as_str()),
            "card_watermark" => |c| c.card_watermark.as_deref(),
            "collector_number" => |c| Some(c.collector_number.as_str()),
            _ => unreachable!(),
        };
        // Use TextExact with pre-lowercased value (fields stored lowercase in DB)
        return Ok(FilterExpr::TextExact { field: lower_fn, op: cmp_op, value });
    }

    // Substring text fields: name, oracle_text, flavor_text, artist.
    let lower_word = raw_value.to_lowercase();
    if op == ":" {
        // Substring against pre-lowercased field
        let lower_fn: fn(&Card) -> Option<&str> = match attr {
            "card_name" => |c| Some(c.card_name_lower.as_str()),
            "oracle_text" => |c| Some(c.oracle_text_lower.as_str()),
            "flavor_text" => |c| Some(c.flavor_text_lower.as_str()),
            "card_artist" => |c| c.card_artist_lower.as_deref(),
            _ => return Err(format!("text substring not supported on {attr}")),
        };
        return Ok(FilterExpr::TextContains { lower_field: lower_fn, word: lower_word });
    }

    // Exact / comparison on text field
    let field_fn: fn(&Card) -> Option<&str> = match attr {
        "card_name" => |c| Some(c.card_name.as_str()),
        "oracle_text" => |c| Some(c.oracle_text.as_str()),
        "flavor_text" => |c| Some(c.flavor_text.as_str()),
        "card_artist" => |c| c.card_artist.as_deref(),
        _ => return Err(format!("unknown text field: {attr}")),
    };
    let cmp_op = str_op_to_cmp(op)?;
    Ok(FilterExpr::TextExact { field: field_fn, op: cmp_op, value: raw_value.to_string() })
}

// ─── Sort / dedup / limit ─────────────────────────────────────────────────────

fn prefer_score(card: &Card, prefer: &str) -> f64 {
    match prefer {
        "oldest" => {
            let d: i64 = card.released_at.replace('-', "").parse().unwrap_or(99999999);
            -(d as f64)
        }
        "newest" => {
            let d: i64 = card.released_at.replace('-', "").parse().unwrap_or(0);
            d as f64
        }
        "usd_low" => -(card.price_usd.unwrap_or(0.0) as f64),
        "usd_high" => card.price_usd.unwrap_or(0.0) as f64,
        "promo" => -(card.edhrec_rank.unwrap_or(0.0) as f64),
        _ => card.prefer_score.unwrap_or(0.0) as f64, // "default"
    }
}

fn partition_key<'a>(card: &'a Card, unique: &str) -> &'a str {
    match unique {
        "artwork" => card.illustration_id.as_deref().unwrap_or(""),
        "printing" => &card.scryfall_id,
        _ => card.oracle_id.as_deref().unwrap_or(""), // "card"
    }
}

fn sort_key(card: &Card, sort_col: &str, descending: bool) -> (bool, f64, bool, f64, bool, f64) {
    let primary: Option<f32> = match sort_col {
        "cmc" => card.cmc,
        "creature_power" => card.creature_power,
        "creature_toughness" => card.creature_toughness,
        "card_rarity_int" => card.card_rarity_int,
        "price_usd" => card.price_usd,
        "cubecobra_score" => card.cubecobra_score,
        _ => card.edhrec_rank, // "edhrec_rank" default
    };
    let primary_f = primary.unwrap_or(0.0) as f64;
    let primary_val = if descending { -primary_f } else { primary_f };
    let edhrec = card.edhrec_rank.unwrap_or(0.0) as f64;
    let pscore = card.prefer_score.unwrap_or(0.0) as f64;
    (primary.is_none(), primary_val, card.edhrec_rank.is_none(), edhrec, card.prefer_score.is_none(), -pscore)
}

fn orderby_to_col(orderby: &str) -> &'static str {
    match orderby {
        "cmc" => "cmc",
        "power" => "creature_power",
        "rarity" => "card_rarity_int",
        "toughness" => "creature_toughness",
        "usd" => "price_usd",
        "cubecobra" => "cubecobra_score",
        _ => "edhrec_rank",
    }
}

/// Filter + dedup + sort + limit, returning (total_count, page of Card indices).
fn run_query<'a>(
    store: &'a [Card],
    filter: &FilterExpr,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
) -> (usize, Vec<&'a Card>) {
    let sort_col = orderby_to_col(orderby);
    let descending = direction == "desc";

    // Group matching cards by partition key, keeping the best per group.
    let mut partitions: HashMap<&str, (&Card, f64)> = HashMap::new();
    for card in store {
        if filter.matches(card) {
            let key = partition_key(card, unique);
            let score = prefer_score(card, prefer);
            let entry = partitions.entry(key).or_insert((card, f64::NEG_INFINITY));
            if score > entry.1 {
                *entry = (card, score);
            }
        }
    }

    let mut best: Vec<&Card> = partitions.into_values().map(|(c, _)| c).collect();
    let total = best.len();

    best.sort_by(|a, b| {
        sort_key(a, sort_col, descending)
            .partial_cmp(&sort_key(b, sort_col, descending))
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    best.truncate(limit);
    (total, best)
}

fn card_to_pydict<'py>(py: Python<'py>, card: &Card) -> Bound<'py, PyDict> {
    let d = PyDict::new(py);
    d.set_item("name", &card.card_name).unwrap();
    d.set_item("set_code", &card.card_set_code).unwrap();
    d.set_item("collector_number", &card.collector_number).unwrap();
    d.set_item("power", card.creature_power_text.as_deref()).unwrap();
    d.set_item("toughness", card.creature_toughness_text.as_deref()).unwrap();
    d.set_item("mana_cost", card.mana_cost_text.as_deref()).unwrap();
    d.set_item("oracle_text", &card.oracle_text).unwrap();
    d.set_item("set_name", &card.set_name).unwrap();
    d.set_item("type_line", &card.type_line).unwrap();
    d
}

// ─── PyO3 bindings ───────────────────────────────────────────────────────────

#[pyclass]
struct QueryEngine {
    store: Arc<RwLock<Vec<Card>>>,
}

#[pymethods]
impl QueryEngine {
    #[new]
    fn new() -> Self {
        QueryEngine {
            store: Arc::new(RwLock::new(Vec::new())),
        }
    }

    /// Populate the card store from a list of DB row dicts.
    fn reload(&self, db_rows: &Bound<PyList>) -> PyResult<()> {
        let cards: Vec<Card> = db_rows
            .iter()
            .filter_map(|item| item.cast::<PyDict>().ok().map(|d| card_from_pydict(&d)))
            .collect();
        let mut store = self.store.write().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("store lock poisoned: {e}"))
        })?;
        *store = cards;
        Ok(())
    }

    /// Filter, deduplicate, sort, and return (total_count, matches).
    #[pyo3(signature = (*, filters, unique="card", prefer="default", orderby="edhrec", direction="asc", limit=100))]
    fn query<'py>(
        &self,
        py: Python<'py>,
        filters: &Bound<PyAny>,
        unique: &str,
        prefer: &str,
        orderby: &str,
        direction: &str,
        limit: usize,
    ) -> PyResult<Bound<'py, PyTuple>> {
        // Serialize the query to JSON — orjson is ~7x faster than stdlib json
        let to_json = filters.call_method0("to_json")?;
        let json_bytes: Vec<u8> = py
            .import("orjson")?
            .call_method1("dumps", (to_json,))?
            .extract()?;
        let json_str = std::str::from_utf8(&json_bytes)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad UTF-8 from orjson: {e}")))?;

        let json_val: Value = serde_json::from_str(&json_str)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad query JSON: {e}")))?;

        let filter_expr = build_filter(&json_val)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("build_filter: {e}")))?;

        let store = self.store.read().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("store lock poisoned: {e}"))
        })?;

        let (total, page) = run_query(&store, &filter_expr, unique, prefer, orderby, direction, limit);

        let matches: Vec<Bound<PyDict>> = page.iter().map(|c| card_to_pydict(py, c)).collect();
        let matches_list = PyList::new(py, matches)?;

        PyTuple::new(py, [total.into_pyobject(py)?.into_any(), matches_list.into_any()])
    }

    fn size(&self) -> PyResult<usize> {
        let store = self.store.read().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("store lock poisoned: {e}"))
        })?;
        Ok(store.len())
    }
}

#[pymodule]
mod card_engine {
    #[pymodule_export]
    use super::QueryEngine;
}
