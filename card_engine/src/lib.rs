use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use regex::Regex;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};

// ─── Inline string (no heap allocation) ──────────────────────────────────────

#[derive(Clone)]
struct InlineStr<const N: usize> {
    bytes: [u8; N],
    len: u8,
}

impl<const N: usize> InlineStr<N> {
    fn from_str(s: &str) -> Self {
        let max = s.len().min(N);
        // Walk back from max to ensure we don't split a multi-byte char.
        let len = (0..=max).rev().find(|&i| s.is_char_boundary(i)).unwrap_or(0);
        let mut bytes = [0u8; N];
        bytes[..len].copy_from_slice(&s.as_bytes()[..len]);
        InlineStr { bytes, len: len as u8 }
    }

    #[inline]
    fn as_str(&self) -> &str {
        unsafe { std::str::from_utf8_unchecked(&self.bytes[..self.len as usize]) }
    }
}

// ─── Card type bits (u16) ─────────────────────────────────────────────────────

const TYPE_ARTIFACT:     u16 = 1 << 0;
const TYPE_BASIC:        u16 = 1 << 1;
const TYPE_BATTLE:       u16 = 1 << 2;
const TYPE_CONSPIRACY:   u16 = 1 << 3;
const TYPE_CREATURE:     u16 = 1 << 4;
const TYPE_ENCHANTMENT:  u16 = 1 << 5;
const TYPE_INSTANT:      u16 = 1 << 6;
const TYPE_KINDRED:      u16 = 1 << 7;
const TYPE_LAND:         u16 = 1 << 8;
const TYPE_LEGENDARY:    u16 = 1 << 9;
const TYPE_PLANESWALKER: u16 = 1 << 10;
const TYPE_SNOW:         u16 = 1 << 11;
const TYPE_SORCERY:      u16 = 1 << 12;
const TYPE_WORLD:        u16 = 1 << 13;

fn card_type_str_to_bit(s: &str) -> u16 {
    match s {
        "Artifact"     => TYPE_ARTIFACT,
        "Basic"        => TYPE_BASIC,
        "Battle"       => TYPE_BATTLE,
        "Conspiracy"   => TYPE_CONSPIRACY,
        "Creature"     => TYPE_CREATURE,
        "Enchantment"  => TYPE_ENCHANTMENT,
        "Instant"      => TYPE_INSTANT,
        "Kindred"      => TYPE_KINDRED,
        "Tribal"       => TYPE_KINDRED,
        "Land"         => TYPE_LAND,
        "Legendary"    => TYPE_LEGENDARY,
        "Planeswalker" => TYPE_PLANESWALKER,
        "Snow"         => TYPE_SNOW,
        "Sorcery"      => TYPE_SORCERY,
        "World"        => TYPE_WORLD,
        _              => 0,
    }
}

fn card_types_list_to_bits(types: &[String]) -> u16 {
    types.iter().fold(0u16, |acc, t| acc | card_type_str_to_bit(t))
}

// ─── Color bits (W=1 U=2 B=4 R=8 G=16 C=32) ─────────────────────────────────

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

fn mana_pip_counts(s: &str) -> HashMap<String, u8> {
    let mut pips: HashMap<String, u8> = HashMap::new();
    let upper = s.to_uppercase();
    let mut in_brace = false;
    let mut sym = String::new();
    for c in upper.chars() {
        match c {
            '{' => { in_brace = true; sym.clear(); }
            '}' => {
                if in_brace && sym.parse::<u32>().is_err() && sym != "X" {
                    *pips.entry(sym.clone()).or_insert(0) += 1;
                }
                in_brace = false;
            }
            _ if in_brace => sym.push(c),
            _ if "WUBRGC".contains(c) => { *pips.entry(c.to_string()).or_insert(0) += 1; }
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
            '{' => { in_brace = true; sym.clear(); }
            '}' => {
                if in_brace {
                    if let Ok(n) = sym.parse::<f32>() { cmc += n; }
                    else if sym != "X" { cmc += 1.0; }
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
    pips: HashMap<String, u8>,              // faithful to mana_cost_jsonb; used for mana= queries
    devotion: Option<HashMap<String, u8>>,  // Some only when hybrids are present; used for devotion queries
    cmc: f32,
}

struct Card {
    // Hot fields first — fits in the first two cache lines for fast filter short-circuiting.
    card_name_lower: InlineStr<61>, // 61 bytes covers every card name in the Scryfall dataset
    card_colors: u8,
    card_color_identity: u8,
    produced_mana: u8,
    card_types: u16,

    scryfall_id: String,
    oracle_id: Option<String>,
    illustration_id: Option<String>,

    card_name: String,
    oracle_text: String,
    oracle_text_lower: String,
    flavor_text: String,
    flavor_text_lower: String,
    card_artist: Option<String>,
    card_artist_lower: Option<String>,
    card_set_code: InlineStr<8>,
    card_layout: String,
    card_border: String,
    card_watermark: Option<String>,
    collector_number: String,
    mana_cost_text: Option<String>,
    type_line: String,
    set_name: String,
    released_at: String,

    cmc: Option<u8>,                   // always an integer; max ~16 in practice
    creature_power: Option<i8>,        // can be negative (e.g. Char-Rumbler)
    creature_toughness: Option<i8>,
    planeswalker_loyalty: Option<u8>,  // always 1–12
    card_rarity_int: Option<u8>,       // 0–5
    collector_number_int: Option<u16>, // some sets exceed i8::MAX
    edhrec_rank: Option<u32>,          // up to ~30k unique cards
    price_usd: Option<f32>,
    price_eur: Option<f32>,
    price_tix: Option<f32>,
    prefer_score: Option<f32>,
    cubecobra_score: Option<f32>,

    card_subtypes: Vec<String>,
    card_keywords: HashSet<String>,
    card_legalities: HashMap<String, String>,
    card_oracle_tags: HashSet<String>,
    card_is_tags: HashSet<String>,
    card_frame_data: HashSet<String>,

    mana_cost: ManaCost,

    creature_power_text: Option<String>,
    creature_toughness_text: Option<String>,
}

// ─── Loading helpers ─────────────────────────────────────────────────────────

fn opt_str(d: &Bound<PyDict>, key: &str) -> Option<String> {
    d.get_item(key).ok().flatten().and_then(|v| v.extract::<String>().ok())
}

fn opt_f32(d: &Bound<PyDict>, key: &str) -> Option<f32> {
    d.get_item(key).ok().flatten().and_then(|v| {
        v.extract::<f64>().ok().map(|n| n as f32)
            .or_else(|| v.extract::<i64>().ok().map(|n| n as f32))
    })
}

fn opt_i8(d: &Bound<PyDict>, key: &str) -> Option<i8> {
    opt_f32(d, key).map(|v| v as i8)
}

fn opt_u8(d: &Bound<PyDict>, key: &str) -> Option<u8> {
    opt_f32(d, key).map(|v| v as u8)
}

fn opt_u16(d: &Bound<PyDict>, key: &str) -> Option<u16> {
    opt_f32(d, key).map(|v| v as u16)
}

fn opt_u32(d: &Bound<PyDict>, key: &str) -> Option<u32> {
    opt_f32(d, key).map(|v| v as u32)
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
    let pips: HashMap<String, u8> = d
        .get_item("mana_cost_jsonb")
        .ok()
        .flatten()
        .and_then(|v| {
            v.cast::<PyDict>().ok().map(|m| {
                m.iter()
                    .filter_map(|(k, v)| {
                        let sym = k.extract::<String>().ok()?;
                        let count = v.cast::<PyList>().ok().map(|l| l.len() as u8).unwrap_or(0);
                        Some((sym, count))
                    })
                    .collect()
            })
        })
        .unwrap_or_default();
    let devotion = if pips.keys().any(|s| s.contains('/')) {
        let mut d: HashMap<String, u8> = HashMap::new();
        for (sym, &n) in &pips {
            if sym.contains('/') {
                for part in sym.split('/') {
                    if part.len() == 1 && "WUBRG".contains(part) {
                        *d.entry(part.to_string()).or_insert(0) += n;
                    }
                }
            } else {
                *d.entry(sym.clone()).or_insert(0) += n;
            }
        }
        Some(d)
    } else {
        None
    };
    ManaCost { pips, devotion, cmc: cmc_val.unwrap_or(0.0) }
}

fn card_from_pydict(d: &Bound<PyDict>) -> Card {
    let card_name = opt_str(d, "card_name").unwrap_or_default();
    let card_name_lower = InlineStr::<61>::from_str(&card_name.to_lowercase());
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

        card_name_lower,
        card_name,
        oracle_text_lower,
        oracle_text,
        flavor_text_lower,
        flavor_text,
        card_artist_lower,
        card_artist,
        card_set_code: InlineStr::<8>::from_str(&opt_str(d, "card_set_code").unwrap_or_default()),
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

        cmc: opt_u8(d, "cmc"), // Un-set cards have fractional cmc, but we don't load those into the dataset
        creature_power: opt_i8(d, "creature_power"),
        creature_toughness: opt_i8(d, "creature_toughness"),
        planeswalker_loyalty: opt_u8(d, "planeswalker_loyalty"),
        card_rarity_int: opt_u8(d, "card_rarity_int"),
        collector_number_int: opt_u16(d, "collector_number_int"),
        edhrec_rank: opt_u32(d, "edhrec_rank"),
        price_usd: opt_f32(d, "price_usd"),
        price_eur: opt_f32(d, "price_eur"),
        price_tix: opt_f32(d, "price_tix"),
        prefer_score: opt_f32(d, "prefer_score"),
        cubecobra_score: opt_f32(d, "cubecobra_score"),

        card_types: card_types_list_to_bits(&str_list(d, "card_types")),
        card_subtypes: str_list(d, "card_subtypes"),
        card_keywords: jsonb_obj_to_hashset(d, "card_keywords"),
        card_legalities: jsonb_obj_to_string_map(d, "card_legalities"),
        card_oracle_tags: jsonb_obj_to_hashset(d, "card_oracle_tags"),
        card_is_tags: jsonb_obj_to_hashset(d, "card_is_tags"),
        card_frame_data: jsonb_obj_to_hashset(d, "card_frame_data"),

        mana_cost: mana_cost_from_pydict(d, opt_f32(d, "cmc")),

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

fn card_num(card: &Card, f: NumField) -> Option<f32> {
    match f {
        NumField::Cmc                => card.cmc.map(|v| v as f32),
        NumField::Power              => card.creature_power.map(|v| v as f32),
        NumField::Toughness          => card.creature_toughness.map(|v| v as f32),
        NumField::Loyalty            => card.planeswalker_loyalty.map(|v| v as f32),
        NumField::RarityInt          => card.card_rarity_int.map(|v| v as f32),
        NumField::CollectorNumberInt => card.collector_number_int.map(|v| v as f32),
        NumField::EdhrEc             => card.edhrec_rank.map(|v| v as f32),
        NumField::PriceUsd           => card.price_usd,
        NumField::PriceEur           => card.price_eur,
        NumField::PriceTix           => card.price_tix,
        NumField::PreferScore        => card.prefer_score,
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
                        if r == 0.0 { return None; }
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
        ColorField::Colors        => card.card_colors,
        ColorField::ColorIdentity => card.card_color_identity,
        ColorField::ProducedMana  => card.produced_mana,
    }
}

#[derive(Clone, Copy)]
enum CollField {
    Subtypes,
    Keywords,
    OracleTags,
    IsTags,
    FrameData,
}

fn card_collection<'a>(card: &'a Card, f: CollField) -> CollRef<'a> {
    match f {
        CollField::Subtypes   => CollRef::List(&card.card_subtypes),
        CollField::Keywords   => CollRef::Set(&card.card_keywords),
        CollField::OracleTags => CollRef::Set(&card.card_oracle_tags),
        CollField::IsTags     => CollRef::Set(&card.card_is_tags),
        CollField::FrameData  => CollRef::Set(&card.card_frame_data),
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
            CollRef::Set(s)  => s.contains(v),
        }
    }
    fn len(&self) -> usize {
        match self {
            CollRef::List(l) => l.len(),
            CollRef::Set(s)  => s.len(),
        }
    }
    fn all_equal(&self, v: &str) -> bool {
        match self {
            CollRef::List(l) => l.iter().all(|s| s == v),
            CollRef::Set(s)  => s.iter().all(|s| s == v),
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum TextSearchField {
    NameLower,
    OracleTextLower,
    FlavorTextLower,
    ArtistLower,
}

fn text_field_value<'a>(card: &'a Card, field: TextSearchField) -> Option<&'a str> {
    match field {
        TextSearchField::NameLower       => Some(card.card_name_lower.as_str()),
        TextSearchField::OracleTextLower => Some(card.oracle_text_lower.as_str()),
        TextSearchField::FlavorTextLower => Some(card.flavor_text_lower.as_str()),
        TextSearchField::ArtistLower     => card.card_artist_lower.as_deref(),
    }
}

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

    TextContains {
        field: TextSearchField,
        word: String,
    },
    TextExact {
        field: fn(&Card) -> Option<&str>,
        op: CmpOp,
        value: String,
    },
    TextRegex {
        field: fn(&Card) -> Option<&str>,
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
        format: String,
        expected: &'static str,
    },

    ManaCostCmp {
        op: CmpOp,
        pips: HashMap<String, u8>,
        cmc: f32,
    },

    Devotion {
        pips: HashMap<String, u8>,
    },

    DateCmp {
        op: CmpOp,
        value: String,
    },

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
            FilterExpr::Or(children)  => children.iter().any(|c| c.matches(card)),
            FilterExpr::Not(inner)    => !inner.matches(card),

            FilterExpr::ExactName(lower) => card.card_name_lower.as_str() == lower.as_str(),

            FilterExpr::NumericCmp { lhs, op, rhs } => {
                match (lhs.eval(card), rhs.eval(card)) {
                    (Some(a), Some(b)) => cmp(*op, a, b),
                    _ => false,
                }
            }

            FilterExpr::TextContains { field, word } => {
                text_field_value(card, *field).map_or(false, |s| s.contains(word.as_str()))
            }

            FilterExpr::TextExact { field, op, value } => {
                field(card).map_or(false, |s| match op {
                    CmpOp::Eq => s == value,
                    CmpOp::Ne => s != value,
                    CmpOp::Lt => s < value.as_str(),
                    CmpOp::Le => s <= value.as_str(),
                    CmpOp::Gt => s > value.as_str(),
                    CmpOp::Ge => s >= value.as_str(),
                })
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

            FilterExpr::TypeCmp { mask, op } => {
                let bits = card.card_types;
                match op {
                    CmpOp::Ge => bits & mask != 0,
                    CmpOp::Eq => bits == *mask,
                    CmpOp::Le => bits & !mask == 0,
                    CmpOp::Lt => bits & !mask == 0 && bits != *mask,
                    CmpOp::Gt => bits & mask != 0 && bits != *mask,
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
                    CmpOp::Lt => false,
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
                    CmpOp::Ge => {
                        pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc >= *cmc
                    }
                    CmpOp::Le => {
                        card_pips
                            .iter()
                            .all(|(sym, &n)| pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc <= *cmc
                    }
                    CmpOp::Eq => {
                        card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n)
                    }
                    CmpOp::Gt => {
                        let contains = pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc >= *cmc;
                        let exact = card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n);
                        contains && !exact
                    }
                    CmpOp::Lt => {
                        let subset = card_pips.iter().all(|(sym, &n)| pips.get(sym).copied().unwrap_or(0) >= n)
                            && card_cmc <= *cmc;
                        let exact = card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n);
                        subset && !exact
                    }
                    CmpOp::Ne => {
                        !(card_cmc == *cmc
                            && card_pips.len() == pips.len()
                            && pips.iter().all(|(sym, &n)| card_pips.get(sym).copied().unwrap_or(0) == n))
                    }
                }
            }

            FilterExpr::Devotion { pips } => {
                let devotion = card.mana_cost.devotion.as_ref().unwrap_or(&card.mana_cost.pips);
                pips.iter().all(|(sym, &n)| devotion.get(sym).copied().unwrap_or(0) >= n)
            }

            FilterExpr::DateCmp { op, value } => {
                if card.released_at.is_empty() { return false; }
                let ord = card.released_at.as_str().cmp(value.as_str());
                match op {
                    CmpOp::Eq => ord == std::cmp::Ordering::Equal,
                    CmpOp::Ne => ord != std::cmp::Ordering::Equal,
                    CmpOp::Lt => ord == std::cmp::Ordering::Less,
                    CmpOp::Le => ord != std::cmp::Ordering::Greater,
                    CmpOp::Gt => ord == std::cmp::Ordering::Greater,
                    CmpOp::Ge => ord != std::cmp::Ordering::Less,
                }
            }

            FilterExpr::YearCmp { op, year } => {
                if card.released_at.is_empty() { return false; }
                let s = &card.released_at;
                let start = format!("{year:04}-01-01");
                let end   = format!("{:04}-01-01", year + 1);
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
        return Ok(FilterExpr::DateCmp { op: cmp_op, value: val_str.to_string() });
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
        // Split hybrid symbols ({R/G} → R:1, G:1) to match calculate_devotion() in SQL.
        // mana_pip_counts is NOT used here because it keeps hybrids as single keys.
        let mut pips: HashMap<String, u8> = HashMap::new();
        for (sym, n) in mana_pip_counts(mana_str) {
            if sym.contains('/') {
                for part in sym.split('/') {
                    if part.len() == 1 && "WUBRG".contains(part) {
                        *pips.entry(part.to_string()).or_insert(0) += n;
                    }
                }
            } else {
                *pips.entry(sym).or_insert(0) += n;
            }
        }
        return Ok(FilterExpr::Devotion { pips });
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
            .unwrap_or("")
            .to_string();
        let expected: &'static str = match orig {
            "format" | "f" | "legal" => "legal",
            "banned"                 => "banned",
            "restricted"             => "restricted",
            _                        => "legal",
        };
        return Ok(FilterExpr::Legality { format, expected });
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

    if matches!(attr, "card_oracle_tags" | "card_is_tags" | "card_frame_data") {
        let coll_field = match attr {
            "card_oracle_tags" => CollField::OracleTags,
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
        let field_fn: fn(&Card) -> Option<&str> = match attr {
            "card_name"   => |c| Some(c.card_name.as_str()),
            "oracle_text" => |c| Some(c.oracle_text.as_str()),
            "flavor_text" => |c| Some(c.flavor_text.as_str()),
            "card_artist" => |c| c.card_artist.as_deref(),
            _ => return Err(format!("regex not supported on {attr}")),
        };
        return Ok(FilterExpr::TextRegex { field: field_fn, regex: re });
    }

    let raw_value = rhs["kwargs"]["value"].as_str().unwrap_or("");

    if matches!(attr, "card_set_code" | "card_layout" | "card_border" | "card_watermark" | "collector_number") {
        let value  = raw_value.to_lowercase();
        let cmp_op = str_op_to_cmp(op)?;
        let lower_fn: fn(&Card) -> Option<&str> = match attr {
            "card_set_code"      => |c| Some(c.card_set_code.as_str() as &str),
            "card_layout"        => |c| Some(c.card_layout.as_str()),
            "card_border"        => |c| Some(c.card_border.as_str()),
            "card_watermark"     => |c| c.card_watermark.as_deref(),
            "collector_number"   => |c| Some(c.collector_number.as_str()),
            _                    => unreachable!(),
        };
        return Ok(FilterExpr::TextExact { field: lower_fn, op: cmp_op, value });
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

    let field_fn: fn(&Card) -> Option<&str> = match attr {
        "card_name"   => |c| Some(c.card_name_lower.as_str()),
        "oracle_text" => |c| Some(c.oracle_text_lower.as_str()),
        "flavor_text" => |c| Some(c.flavor_text_lower.as_str()),
        "card_artist" => |c| c.card_artist_lower.as_deref(),
        _ => return Err(format!("unknown text field: {attr}")),
    };
    let cmp_op = str_op_to_cmp(op)?;
    Ok(FilterExpr::TextExact { field: field_fn, op: cmp_op, value: raw_value.to_lowercase() })
}

// ─── Trigram index ────────────────────────────────────────────────────────────

type TrigramIndex = HashMap<[u8; 3], Vec<u32>>;

fn build_trigram_index(cards: &[Card], get_text: impl Fn(&Card) -> &str) -> TrigramIndex {
    let mut idx: TrigramIndex = HashMap::new();
    for (i, card) in cards.iter().enumerate() {
        let text  = get_text(card);
        let bytes = text.as_bytes();
        if bytes.len() < 3 { continue; }
        for w in bytes.windows(3) {
            let tri  = [w[0], w[1], w[2]];
            let list = idx.entry(tri).or_default();
            if list.last() != Some(&(i as u32)) {
                list.push(i as u32);
            }
        }
    }
    idx
}

fn intersect_sorted(a: &[u32], b: &[u32]) -> Vec<u32> {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        if a[i] == b[j]      { out.push(a[i]); i += 1; j += 1; }
        else if a[i] < b[j]  { i += 1; }
        else                  { j += 1; }
    }
    out
}

fn trigram_candidates(idx: &TrigramIndex, word: &str) -> Option<Vec<u32>> {
    let bytes = word.as_bytes();
    if bytes.len() < 3 { return None; }

    let mut lists: Vec<&Vec<u32>> = bytes.windows(3)
        .map(|w| [w[0], w[1], w[2]])
        .filter_map(|tri| idx.get(&tri))
        .collect();
    if lists.is_empty() { return Some(Vec::new()); }
    lists.sort_unstable_by_key(|l| l.len());
    lists.dedup_by(|a, b| std::ptr::eq(*a, *b));

    let mut result = lists[0].clone();
    for list in &lists[1..] {
        if result.is_empty() { break; }
        result = intersect_sorted(&result, list);
    }
    Some(result)
}

fn union_sorted(a: Vec<u32>, b: Vec<u32>) -> Vec<u32> {
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less    => { out.push(a[i]); i += 1; }
            std::cmp::Ordering::Greater => { out.push(b[j]); j += 1; }
            std::cmp::Ordering::Equal   => { out.push(a[i]); i += 1; j += 1; }
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    out
}

// ─── Numeric index ────────────────────────────────────────────────────────────
// Sorted Vec<(i16, u32)> maps field value → card index for cmc/power/toughness.
// i16 covers both u8 (cmc: 0–255) and i8 (power/toughness: -128–127) without loss.
// Binary search gives the candidate slice; sort by card index for intersection.

type NumericIndex = Vec<(i16, u32)>;

fn build_numeric_index(cards: &[Card], get_val: impl Fn(&Card) -> Option<i16>) -> NumericIndex {
    let mut idx: NumericIndex = cards
        .iter()
        .enumerate()
        .filter_map(|(i, c)| get_val(c).map(|v| (v, i as u32)))
        .collect();
    idx.sort_unstable();
    idx
}

fn flip_op(op: CmpOp) -> CmpOp {
    match op {
        CmpOp::Lt => CmpOp::Gt,
        CmpOp::Le => CmpOp::Ge,
        CmpOp::Gt => CmpOp::Lt,
        CmpOp::Ge => CmpOp::Le,
        CmpOp::Eq => CmpOp::Eq,
        CmpOp::Ne => CmpOp::Ne,
    }
}

/// Return sorted card indices satisfying `field op val` using the numeric index.
/// Returns None for Ne (not selective) and Some(empty) when no cards can match.
fn numeric_candidates(idx: &NumericIndex, op: CmpOp, val: f64) -> Option<Vec<u32>> {
    let (start, end) = match op {
        CmpOp::Ne => return None,
        CmpOp::Eq => {
            if val.fract() != 0.0 { return Some(Vec::new()); }
            let s = idx.partition_point(|&(x, _)| (x as f64) < val);
            let e = idx.partition_point(|&(x, _)| (x as f64) <= val);
            (s, e)
        }
        CmpOp::Lt => (0, idx.partition_point(|&(x, _)| (x as f64) < val)),
        CmpOp::Le => (0, idx.partition_point(|&(x, _)| (x as f64) <= val)),
        CmpOp::Gt => (idx.partition_point(|&(x, _)| (x as f64) <= val), idx.len()),
        CmpOp::Ge => (idx.partition_point(|&(x, _)| (x as f64) < val), idx.len()),
    };
    let mut result: Vec<u32> = idx[start..end].iter().map(|&(_, i)| i).collect();
    result.sort_unstable();
    Some(result)
}

// ─── Tag index ───────────────────────────────────────────────────────────────
// tag name → sorted list of card indices that have that tag.
// Lists are naturally sorted because cards are iterated in index order.

type TagIndex = HashMap<String, Vec<u32>>;

fn build_tag_index(cards: &[Card], get_tags: impl Fn(&Card) -> &HashSet<String>) -> TagIndex {
    let mut idx: TagIndex = HashMap::new();
    for (i, card) in cards.iter().enumerate() {
        for tag in get_tags(card) {
            idx.entry(tag.clone()).or_default().push(i as u32);
        }
    }
    idx
}

// ─── Type bit index ───────────────────────────────────────────────────────────
// One sorted Vec<u32> per type bit (14 bits, matching the TYPE_* constants).
// Bit position N corresponds to TYPE_* = 1 << N.

type TypeIndex = [Vec<u32>; 14];

fn build_type_index(cards: &[Card]) -> TypeIndex {
    let mut idx: TypeIndex = Default::default();
    for (i, card) in cards.iter().enumerate() {
        let mut bits = card.card_types;
        while bits != 0 {
            let bit = bits.trailing_zeros() as usize;
            idx[bit].push(i as u32);
            bits &= bits - 1;
        }
    }
    idx // lists are sorted: cards iterated in ascending index order
}

fn build_list_index(cards: &[Card], get_list: impl Fn(&Card) -> &Vec<String>) -> TagIndex {
    let mut idx: TagIndex = HashMap::new();
    for (i, card) in cards.iter().enumerate() {
        for item in get_list(card) {
            idx.entry(item.clone()).or_default().push(i as u32);
        }
    }
    idx
}

// ─── Combined indexes ────────────────────────────────────────────────────────

struct CardIndexes {
    name_trigram:   TrigramIndex,
    oracle_trigram: TrigramIndex,
    cmc:            NumericIndex,
    power:          NumericIndex,
    toughness:      NumericIndex,
    type_bits:      TypeIndex,
    subtypes:       TagIndex,
    keywords:       TagIndex,
    oracle_tags:    TagIndex,
    is_tags:        TagIndex,
}

impl Default for CardIndexes {
    fn default() -> Self {
        CardIndexes {
            name_trigram:   HashMap::new(),
            oracle_trigram: HashMap::new(),
            cmc:            Vec::new(),
            power:          Vec::new(),
            toughness:      Vec::new(),
            type_bits:      Default::default(),
            subtypes:       HashMap::new(),
            keywords:       HashMap::new(),
            oracle_tags:    HashMap::new(),
            is_tags:        HashMap::new(),
        }
    }
}

struct CardData {
    cards:   Vec<Card>,
    indexes: CardIndexes,
}

// ─── Candidate narrowing ─────────────────────────────────────────────────────

fn narrow_candidates(filter: &FilterExpr, indexes: &CardIndexes) -> Option<Vec<u32>> {
    match filter {
        FilterExpr::TextContains { field, word }
            if word.len() >= 3
                && matches!(field, TextSearchField::NameLower | TextSearchField::OracleTextLower) =>
        {
            let idx = if *field == TextSearchField::NameLower { &indexes.name_trigram } else { &indexes.oracle_trigram };
            trigram_candidates(idx, word)
        }

        FilterExpr::NumericCmp { lhs, op, rhs } => match (lhs, rhs) {
            (NumExpr::Field(NumField::Cmc), NumExpr::Const(v)) =>
                numeric_candidates(&indexes.cmc, *op, *v),
            (NumExpr::Const(v), NumExpr::Field(NumField::Cmc)) =>
                numeric_candidates(&indexes.cmc, flip_op(*op), *v),
            (NumExpr::Field(NumField::Power), NumExpr::Const(v)) =>
                numeric_candidates(&indexes.power, *op, *v),
            (NumExpr::Const(v), NumExpr::Field(NumField::Power)) =>
                numeric_candidates(&indexes.power, flip_op(*op), *v),
            (NumExpr::Field(NumField::Toughness), NumExpr::Const(v)) =>
                numeric_candidates(&indexes.toughness, *op, *v),
            (NumExpr::Const(v), NumExpr::Field(NumField::Toughness)) =>
                numeric_candidates(&indexes.toughness, flip_op(*op), *v),
            _ => None,
        },

        FilterExpr::TypeCmp { mask, op } if matches!(op, CmpOp::Ge) => {
            let mut result: Vec<u32> = Vec::new();
            let mut m = *mask;
            while m != 0 {
                let bit = m.trailing_zeros() as usize;
                m &= m - 1;
                if bit < 14 {
                    result = union_sorted(result, indexes.type_bits[bit].clone());
                }
            }
            Some(result)
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::Subtypes) && matches!(op, CmpOp::Ge) =>
        {
            indexes.subtypes.get(value.as_str()).cloned()
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::Keywords) && matches!(op, CmpOp::Ge) =>
        {
            indexes.keywords.get(value.as_str()).cloned()
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::OracleTags) && matches!(op, CmpOp::Ge) =>
        {
            indexes.oracle_tags.get(value.as_str()).cloned()
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::IsTags) && matches!(op, CmpOp::Ge) =>
        {
            indexes.is_tags.get(value.as_str()).cloned()
        }

        FilterExpr::And(children) => {
            let mut sets: Vec<Vec<u32>> = children
                .iter()
                .filter_map(|c| narrow_candidates(c, indexes))
                .collect();
            if sets.is_empty() { return None; }
            sets.sort_unstable_by_key(|s| s.len());
            let mut result = sets.swap_remove(0);
            for set in sets {
                if result.is_empty() { break; }
                result = intersect_sorted(&result, &set);
            }
            Some(result)
        }

        FilterExpr::Or(children) => {
            let mut union: Vec<u32> = Vec::new();
            for child in children {
                match narrow_candidates(child, indexes) {
                    None             => return None,
                    Some(candidates) => union = union_sorted(union, candidates),
                }
            }
            Some(union)
        }

        _ => None,
    }
}

// ─── Sort / dedup / limit ─────────────────────────────────────────────────────

fn prefer_score(card: &Card, prefer: &str) -> f64 {
    match prefer {
        "oldest"   => { let d: i64 = card.released_at.replace('-', "").parse().unwrap_or(99999999); -(d as f64) }
        "newest"   => { let d: i64 = card.released_at.replace('-', "").parse().unwrap_or(0); d as f64 }
        "usd_low"  => -(card.price_usd.unwrap_or(f32::INFINITY) as f64),
        "usd_high" => card.price_usd.unwrap_or(0.0) as f64,
        "promo"    => -(card.edhrec_rank.map(|r| r as f64).unwrap_or(f64::INFINITY)),
        _          => card.prefer_score.unwrap_or(0.0) as f64,
    }
}

fn partition_key<'a>(card: &'a Card, unique: &str) -> &'a str {
    match unique {
        "artwork"  => card.illustration_id.as_deref().unwrap_or(""),
        "printing" => &card.scryfall_id,
        _          => card.oracle_id.as_deref().unwrap_or(""),
    }
}

fn sort_key(card: &Card, sort_col: &str, descending: bool) -> (bool, f64, bool, f64, bool, f64) {
    let primary: Option<f32> = match sort_col {
        "cmc"               => card.cmc.map(|v| v as f32),
        "creature_power"    => card.creature_power.map(|v| v as f32),
        "creature_toughness"=> card.creature_toughness.map(|v| v as f32),
        "card_rarity_int"   => card.card_rarity_int.map(|v| v as f32),
        "price_usd"         => card.price_usd,
        "cubecobra_score"   => card.cubecobra_score,
        _                   => card.edhrec_rank.map(|v| v as f32),
    };
    let primary_f   = primary.unwrap_or(0.0) as f64;
    let primary_val = if descending { -primary_f } else { primary_f };
    let edhrec      = card.edhrec_rank.unwrap_or(0) as f64;
    let pscore      = card.prefer_score.unwrap_or(0.0) as f64;
    (primary.is_none(), primary_val, card.edhrec_rank.is_none(), edhrec, card.prefer_score.is_none(), -pscore)
}

fn orderby_to_col(orderby: &str) -> &'static str {
    match orderby {
        "cmc"       => "cmc",
        "power"     => "creature_power",
        "rarity"    => "card_rarity_int",
        "toughness" => "creature_toughness",
        "usd"       => "price_usd",
        "cubecobra" => "cubecobra_score",
        _           => "edhrec_rank",
    }
}

fn run_query_hashmap<'a>(
    store: &'a [Card],
    filter: &FilterExpr,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
) -> (usize, Vec<&'a Card>) {
    let sort_col  = orderby_to_col(orderby);
    let descending = direction == "desc";

    let mut partitions: HashMap<&str, (&Card, f64)> = HashMap::new();
    for card in store {
        if filter.matches(card) {
            let key   = partition_key(card, unique);
            let score = prefer_score(card, prefer);
            let entry = partitions.entry(key).or_insert((card, f64::NEG_INFINITY));
            if score > entry.1 { *entry = (card, score); }
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

fn run_query_linear<'a, I, F>(
    cards: I,
    filter: &FilterExpr,
    key_fn: F,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
) -> (usize, Vec<&'a Card>)
where
    I: Iterator<Item = &'a Card>,
    F: Fn(&'a Card) -> &'a str,
{
    let sort_col  = orderby_to_col(orderby);
    let descending = direction == "desc";

    let mut best: Vec<&Card> = Vec::new();
    let mut group_best: Option<(&Card, f64)> = None;
    let mut prev_key: &str = "";

    for card in cards {
        if !filter.matches(card) { continue; }
        let key = key_fn(card);
        if key != prev_key {
            if let Some((c, _)) = group_best.take() { best.push(c); }
            prev_key   = key;
            group_best = Some((card, prefer_score(card, prefer)));
        } else {
            let score = prefer_score(card, prefer);
            if score > group_best.as_ref().map_or(f64::NEG_INFINITY, |g| g.1) {
                group_best = Some((card, score));
            }
        }
    }
    if let Some((c, _)) = group_best { best.push(c); }

    let total = best.len();
    best.sort_by(|a, b| {
        sort_key(a, sort_col, descending)
            .partial_cmp(&sort_key(b, sort_col, descending))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    best.truncate(limit);
    (total, best)
}

fn run_query_no_dedup<'a>(
    cards: impl Iterator<Item = &'a Card>,
    filter: &FilterExpr,
    orderby: &str,
    direction: &str,
    limit: usize,
) -> (usize, Vec<&'a Card>) {
    let sort_col  = orderby_to_col(orderby);
    let descending = direction == "desc";
    let mut matched: Vec<&Card> = cards.filter(|c| filter.matches(c)).collect();
    let total = matched.len();
    matched.sort_by(|a, b| {
        sort_key(a, sort_col, descending)
            .partial_cmp(&sort_key(b, sort_col, descending))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    matched.truncate(limit);
    (total, matched)
}

fn run_query<'a>(
    store: &'a [Card],
    filter: &FilterExpr,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
    indexes: &CardIndexes,
) -> (usize, Vec<&'a Card>) {
    let candidates = narrow_candidates(filter, indexes);

    macro_rules! cards_iter {
        () => {
            match &candidates {
                Some(idxs) => Box::new(idxs.iter().map(|&i| &store[i as usize])) as Box<dyn Iterator<Item = &Card>>,
                None       => Box::new(store.iter()),
            }
        };
    }

    match unique {
        "card" => run_query_linear(cards_iter!(), filter, |c| c.oracle_id.as_deref().unwrap_or(""), prefer, orderby, direction, limit),
        // Scryfall assigns each illustration_id to exactly one oracle_id, so cards sharing an
        // illustration_id are always contiguous in the (oracle_id, illustration_id) sort order.
        // The linear dedup path is therefore correct here — no HashMap needed.
        "artwork"  => run_query_linear(cards_iter!(), filter, |c| c.illustration_id.as_deref().unwrap_or(""), prefer, orderby, direction, limit),
        "printing" => run_query_no_dedup(cards_iter!(), filter, orderby, direction, limit),
        _          => run_query_hashmap(store, filter, unique, prefer, orderby, direction, limit),
    }
}

fn card_to_pydict<'py>(py: Python<'py>, card: &Card) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("name", &card.card_name)?;
    d.set_item("set_code", card.card_set_code.as_str())?;
    d.set_item("collector_number", &card.collector_number)?;
    d.set_item("power", card.creature_power_text.as_deref())?;
    d.set_item("toughness", card.creature_toughness_text.as_deref())?;
    d.set_item("mana_cost", card.mana_cost_text.as_deref())?;
    d.set_item("oracle_text", &card.oracle_text)?;
    d.set_item("set_name", &card.set_name)?;
    d.set_item("type_line", &card.type_line)?;
    Ok(d)
}

// ─── PyO3 bindings ───────────────────────────────────────────────────────────

#[pyclass]
struct QueryEngine {
    data: Arc<RwLock<CardData>>,
}

#[pymethods]
impl QueryEngine {
    #[new]
    fn new() -> Self {
        QueryEngine {
            data: Arc::new(RwLock::new(CardData {
                cards:   Vec::new(),
                indexes: CardIndexes::default(),
            })),
        }
    }

    fn reload(&self, db_rows: &Bound<PyList>) -> PyResult<()> {
        let mut cards: Vec<Card> = db_rows
            .iter()
            .filter_map(|item| item.cast::<PyDict>().ok().map(|d| card_from_pydict(&d)))
            .collect();
        cards.sort_unstable_by(|a, b| {
            let oa = a.oracle_id.as_deref().unwrap_or("");
            let ob = b.oracle_id.as_deref().unwrap_or("");
            oa.cmp(ob).then_with(|| {
                let ia = a.illustration_id.as_deref().unwrap_or("");
                let ib = b.illustration_id.as_deref().unwrap_or("");
                ia.cmp(ib)
            })
        });

        let indexes = CardIndexes {
            name_trigram:   build_trigram_index(&cards, |c| c.card_name_lower.as_str()),
            oracle_trigram: build_trigram_index(&cards, |c| c.oracle_text_lower.as_str()),
            cmc:            build_numeric_index(&cards, |c| c.cmc.map(|v| v as i16)),
            power:          build_numeric_index(&cards, |c| c.creature_power.map(|v| v as i16)),
            toughness:      build_numeric_index(&cards, |c| c.creature_toughness.map(|v| v as i16)),
            type_bits:      build_type_index(&cards),
            subtypes:       build_list_index(&cards, |c| &c.card_subtypes),
            keywords:       build_tag_index(&cards, |c| &c.card_keywords),
            oracle_tags:    build_tag_index(&cards, |c| &c.card_oracle_tags),
            is_tags:        build_tag_index(&cards, |c| &c.card_is_tags),
        };

        *self.data.write().map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("data lock: {e}")))? =
            CardData { cards, indexes };
        Ok(())
    }

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
        let to_json    = filters.call_method0("to_json")?;
        let json_bytes: Vec<u8> = py
            .import("orjson")?
            .call_method1("dumps", (to_json,))?
            .extract()?;
        let json_str = std::str::from_utf8(&json_bytes)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad UTF-8 from orjson: {e}")))?;
        let json_val: Value = serde_json::from_str(json_str)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad query JSON: {e}")))?;
        let filter_expr = build_filter(&json_val)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("build_filter: {e}")))?;

        let data = self.data.read()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("data lock: {e}")))?;
        let (total, page) = run_query(
            &data.cards, &filter_expr, unique, prefer, orderby, direction, limit, &data.indexes,
        );

        let matches: Vec<Bound<PyDict>> = page.iter().map(|c| card_to_pydict(py, c)).collect::<PyResult<Vec<_>>>()?;
        let matches_list = PyList::new(py, matches)?;
        PyTuple::new(py, [total.into_pyobject(py)?.into_any(), matches_list.into_any()])
    }

    /// Same as query() but forces the HashMap dedup path. Used for benchmarking.
    #[pyo3(signature = (*, filters, unique="card", prefer="default", orderby="edhrec", direction="asc", limit=100))]
    fn query_hashmap<'py>(
        &self,
        py: Python<'py>,
        filters: &Bound<PyAny>,
        unique: &str,
        prefer: &str,
        orderby: &str,
        direction: &str,
        limit: usize,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let to_json    = filters.call_method0("to_json")?;
        let json_bytes: Vec<u8> = py
            .import("orjson")?
            .call_method1("dumps", (to_json,))?
            .extract()?;
        let json_str = std::str::from_utf8(&json_bytes)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad UTF-8 from orjson: {e}")))?;
        let json_val: Value = serde_json::from_str(json_str)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad query JSON: {e}")))?;
        let filter_expr = build_filter(&json_val)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("build_filter: {e}")))?;

        let data = self.data.read()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("data lock: {e}")))?;
        let (total, page) = run_query_hashmap(&data.cards, &filter_expr, unique, prefer, orderby, direction, limit);
        let matches: Vec<Bound<PyDict>> = page.iter().map(|c| card_to_pydict(py, c)).collect::<PyResult<Vec<_>>>()?;
        let matches_list = PyList::new(py, matches)?;
        PyTuple::new(py, [total.into_pyobject(py)?.into_any(), matches_list.into_any()])
    }

    fn size(&self) -> PyResult<usize> {
        let data = self.data.read()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("data lock: {e}")))?;
        Ok(data.cards.len())
    }
}

#[pymodule]
mod card_engine {
    #[pymodule_export]
    use super::QueryEngine;
}
