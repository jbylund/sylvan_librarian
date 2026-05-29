use pyo3::prelude::*;
use pyo3::types::{PyDate, PyDateAccess, PyDict, PyList, PyTuple};
use regex::Regex;
use rkyv::{Archive, Archived, Deserialize, Serialize};
use memmap2::Mmap;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::Write as IoWrite;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::os::unix::io::AsRawFd;
use std::os::unix::fs::MetadataExt;

// ─── Inline string (no heap allocation) ──────────────────────────────────────

#[derive(Clone, Copy)]
struct InlineStr<const N: usize> {
    bytes: [u8; N],
    len: u8,
}

// Safety: InlineStr<N> is plain bytes (Copy), has no padding that could be
// uninitialized, and carries no internal references — it is safe to treat as
// a flat, relocatable value in an rkyv archive.
unsafe impl<const N: usize> rkyv::Portable for InlineStr<N> {}

impl<const N: usize> Archive for InlineStr<N> {
    type Archived = InlineStr<N>;
    type Resolver = ();
    fn resolve(&self, _: (), out: rkyv::Place<InlineStr<N>>) {
        // Safety: InlineStr<N> is Copy and Portable; writing it verbatim is correct.
        unsafe { out.ptr().write(*self); }
    }
}

impl<const N: usize, S: rkyv::rancor::Fallible + ?Sized> Serialize<S> for InlineStr<N> {
    fn serialize(&self, _serializer: &mut S) -> Result<(), S::Error> { Ok(()) }
}

impl<const N: usize, D: rkyv::rancor::Fallible + ?Sized> Deserialize<InlineStr<N>, D> for InlineStr<N> {
    fn deserialize(&self, _: &mut D) -> Result<InlineStr<N>, D::Error> { Ok(*self) }
}

// Safety: InlineStr<N> contains only a [u8; N] and a u8 len -- both are always
// valid regardless of their byte contents. No pointers, no enums, no invariants
// that could be violated by arbitrary bytes. This impl simply trusts the data.
unsafe impl<const N: usize, C: rkyv::rancor::Fallible + ?Sized> rkyv::bytecheck::CheckBytes<C> for InlineStr<N> {
    unsafe fn check_bytes(
        _value: *const Self,
        _context: &mut C,
    ) -> Result<(), C::Error> {
        Ok(())
    }
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

#[derive(Archive, Serialize, Deserialize)]
struct ManaCost {
    pips: HashMap<String, u8>,              // faithful to mana_cost_jsonb; used for mana= queries
    devotion: Option<HashMap<String, u8>>,  // Some only when hybrids are present; used for devotion queries
    cmc: f32,
}

#[derive(Archive, Serialize, Deserialize)]
struct Card {
    // Hot fields first — fits in the first two cache lines for fast filter short-circuiting.
    card_name_lower: InlineStr<61>, // 61 bytes covers every card name in the Scryfall dataset
    card_colors: u8,
    card_color_identity: u8,
    produced_mana: u8,
    card_types: u16,

    #[allow(dead_code)] // primary key; kept for future result payloads (printing dedup now keys on pointer)
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
    released_at_int: Option<u32>,      // yyyymmdd, parsed once at load for prefer=oldest/newest

    // Dense group ids assigned at reload after the (oracle_id, illustration_id) sort;
    // adjacency-equal to the string keys, so dedup compares integers, not UUIDs.
    oracle_group: u32,
    artwork_group: u32,

    cmc: Option<u8>,                   // always an integer; max ~16 in practice
    creature_power: Option<i8>,        // can be negative (e.g. Char-Rumbler)
    creature_toughness: Option<i8>,
    planeswalker_loyalty: Option<u8>,  // always 1-12
    card_rarity_int: Option<u8>,       // 0-5
    collector_number_int: Option<u16>, // some sets exceed i8::MAX
    edhrec_rank: Option<u32>,          // up to ~30k unique cards
    price_usd: Option<f32>,
    price_eur: Option<f32>,
    price_tix: Option<f32>,
    prefer_score: Option<f32>,
    cubecobra_score: Option<f32>,

    card_subtypes: Vec<String>,
    card_keywords: HashSet<String>,
    card_legalities: u64, // 2 bits per format, positions from the FORMAT_SHIFTS registry
    card_oracle_tags: HashSet<String>,
    card_is_tags: HashSet<String>,
    card_frame_data: HashSet<String>,

    mana_cost: ManaCost,

    creature_power_text: Option<String>,
    creature_toughness_text: Option<String>,
}

// Type alias for the archived (mmap-backed) card type
type ACard = Archived<Card>;

// ─── Loading helpers ─────────────────────────────────────────────────────────

fn opt_str(d: &Bound<PyDict>, key: &str) -> Option<String> {
    d.get_item(key).ok().flatten().and_then(|v| v.extract::<String>().ok())
}

// Accepts ISO strings or datetime.date (psycopg returns date columns as datetime.date).
fn opt_date_str(d: &Bound<PyDict>, key: &str) -> Option<String> {
    let v = d.get_item(key).ok().flatten()?;
    if let Ok(s) = v.extract::<String>() {
        return Some(s);
    }
    let date = v.cast::<PyDate>().ok()?;
    Some(format!("{:04}-{:02}-{:02}", date.get_year(), date.get_month(), date.get_day()))
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

// ─── Format legality bitmap ──────────────────────────────────────────────────
//
// Legalities pack into a u64: 2 bits per format, positions handed out append-only
// by a global registry the first time a format name appears in loaded data, so
// bit assignments stay stable across reloads and engine instances. A format the
// card's JSONB omits reads as not_legal. 32 formats fit; Scryfall ships 22.

const LEGALITY_NOT_LEGAL: u64 = 0;
const LEGALITY_LEGAL: u64 = 1;
const LEGALITY_RESTRICTED: u64 = 2;
const LEGALITY_BANNED: u64 = 3;
const MAX_FORMATS: usize = 32;

static FORMAT_SHIFTS: OnceLock<RwLock<HashMap<String, u8>>> = OnceLock::new();

fn format_shifts() -> &'static RwLock<HashMap<String, u8>> {
    FORMAT_SHIFTS.get_or_init(|| RwLock::new(HashMap::new()))
}

/// Bit shift for a format already seen in loaded data; None matches nothing.
fn format_shift(format: &str) -> Option<u8> {
    format_shifts().read().ok()?.get(format).copied()
}

/// Bit shift for a format, assigning the next free slot if unseen (reload path).
fn format_shift_or_assign(format: &str) -> Option<u8> {
    if let Some(shift) = format_shift(format) {
        return Some(shift);
    }
    let mut shifts = format_shifts().write().ok()?;
    if let Some(&shift) = shifts.get(format) {
        return Some(shift); // assigned while we waited for the write lock
    }
    if shifts.len() >= MAX_FORMATS {
        return None;
    }
    let shift = (shifts.len() * 2) as u8;
    shifts.insert(format.to_string(), shift);
    Some(shift)
}

fn legality_code(status: &str) -> u64 {
    match status {
        "legal"      => LEGALITY_LEGAL,
        "restricted" => LEGALITY_RESTRICTED,
        "banned"     => LEGALITY_BANNED,
        _            => LEGALITY_NOT_LEGAL,
    }
}

fn jsonb_obj_to_legality_bits(d: &Bound<PyDict>, key: &str) -> u64 {
    d.get_item(key)
        .ok()
        .flatten()
        .and_then(|v| {
            v.cast::<PyDict>().ok().map(|m| {
                m.iter()
                    .filter_map(|(k, v)| {
                        let format = k.extract::<String>().ok()?;
                        let status = v.extract::<String>().ok()?;
                        let shift = format_shift_or_assign(&format)?;
                        Some(legality_code(&status) << shift)
                    })
                    .fold(0u64, |bits, b| bits | b)
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
    let released_at = opt_date_str(d, "released_at").unwrap_or_default();
    let released_at_int: Option<u32> = released_at.replace('-', "").parse().ok();
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
        released_at,
        released_at_int,
        oracle_group: 0,  // assigned in reload() after the (oracle_id, illustration_id) sort
        artwork_group: 0,

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
        card_legalities: jsonb_obj_to_legality_bits(d, "card_legalities"),
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

enum NumExpr {
    Const(f64),
    Field(NumField),
    Arith(Box<NumExpr>, ArithOp, Box<NumExpr>),
}

impl NumExpr {
    fn eval(&self, card: &ACard) -> Option<f64> {
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

fn card_colors(card: &ACard, f: ColorField) -> u8 {
    match f {
        ColorField::Colors        => u8::from(card.card_colors),
        ColorField::ColorIdentity => u8::from(card.card_color_identity),
        ColorField::ProducedMana  => u8::from(card.produced_mana),
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

fn card_collection<'a>(card: &'a ACard, f: CollField) -> CollRef<'a> {
    match f {
        CollField::Subtypes   => CollRef::List(&card.card_subtypes),
        CollField::Keywords   => CollRef::Set(&card.card_keywords),
        CollField::OracleTags => CollRef::Set(&card.card_oracle_tags),
        CollField::IsTags     => CollRef::Set(&card.card_is_tags),
        CollField::FrameData  => CollRef::Set(&card.card_frame_data),
    }
}

enum CollRef<'a> {
    List(&'a rkyv::vec::ArchivedVec<rkyv::string::ArchivedString>),
    Set(&'a rkyv::collections::swiss_table::ArchivedHashSet<rkyv::string::ArchivedString>),
}

impl CollRef<'_> {
    fn contains(&self, v: &str) -> bool {
        match self {
            CollRef::List(l) => l.iter().any(|s| s.as_str() == v),
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
            CollRef::List(l) => l.iter().all(|s| s.as_str() == v),
            CollRef::Set(s)  => s.iter().all(|s| s.as_str() == v),
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

fn text_search_field_value<'a>(card: &'a ACard, field: TextSearchField) -> Option<&'a str> {
    match field {
        TextSearchField::NameLower       => Some(card.card_name_lower.as_str()),
        TextSearchField::OracleTextLower => Some(card.oracle_text_lower.as_str()),
        TextSearchField::FlavorTextLower => Some(card.flavor_text_lower.as_str()),
        TextSearchField::ArtistLower     => card.card_artist_lower.as_ref().map(|s| s.as_str()),
    }
}

/// Enum that replaces fn-pointer fields in TextExact / TextRegex.
/// Function pointers cannot be parameterized over &Card vs &ACard, so enum
/// dispatch is used instead.
#[derive(Clone, Copy)]
enum TextField {
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

fn text_field_value<'a>(card: &'a ACard, field: TextField) -> Option<&'a str> {
    match field {
        TextField::NameLower       => Some(card.card_name_lower.as_str()),
        TextField::OracleTextLower => Some(card.oracle_text_lower.as_str()),
        TextField::FlavorTextLower => Some(card.flavor_text_lower.as_str()),
        TextField::ArtistLower     => card.card_artist_lower.as_ref().map(|s| s.as_str()),
        TextField::SetCode         => Some(card.card_set_code.as_str()),
        TextField::Layout          => Some(card.card_layout.as_str()),
        TextField::Border          => Some(card.card_border.as_str()),
        TextField::Watermark       => card.card_watermark.as_ref().map(|s| s.as_str()),
        TextField::CollectorNumber => Some(card.collector_number.as_str()),
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
    fn matches(&self, card: &ACard) -> bool {
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
                text_search_field_value(card, *field).map_or(false, |s| s.contains(word.as_str()))
            }

            FilterExpr::TextExact { field, op, value } => {
                text_field_value(card, *field).map_or(false, |s| match op {
                    CmpOp::Eq => s == value,
                    CmpOp::Ne => s != value,
                    CmpOp::Lt => s < value.as_str(),
                    CmpOp::Le => s <= value.as_str(),
                    CmpOp::Gt => s > value.as_str(),
                    CmpOp::Ge => s >= value.as_str(),
                })
            }

            FilterExpr::TextRegex { field, regex } => {
                text_field_value(card, *field).map_or(false, |s| regex.is_match(s))
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
                let bits = u16::from(card.card_types);
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

            FilterExpr::Legality { shift, expected } => {
                shift.is_some_and(|s| (u64::from(card.card_legalities) >> s) & 0b11 == *expected)
            }

            FilterExpr::ManaCostCmp { op, pips, cmc } => {
                let card_cmc = f32::from(card.mana_cost.cmc);
                let card_pips = &card.mana_cost.pips;
                match op {
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
                }
            }

            FilterExpr::Devotion { pips } => {
                let devotion = if card.mana_cost.devotion.is_some() {
                    card.mana_cost.devotion.as_ref().unwrap()
                } else {
                    &card.mana_cost.pips
                };
                pips.iter().all(|(sym, &n)| {
                    devotion.get(sym.as_str()).map(|v| u8::from(*v)).unwrap_or(0) >= n
                })
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
                let s = card.released_at.as_str();
                let start = format!("{year:04}-01-01");
                let end   = format!("{:04}-01-01", year + 1);
                match op {
                    CmpOp::Eq => s >= start.as_str() && s < end.as_str(),
                    CmpOp::Gt => s >= end.as_str(),
                    CmpOp::Lt => s < start.as_str(),
                    CmpOp::Ge => s >= start.as_str(),
                    CmpOp::Le => s < end.as_str(),
                    CmpOp::Ne => s < start.as_str() || s >= end.as_str(),
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
        // Split hybrid symbols ({R/G} -> R:1, G:1) to match calculate_devotion() in SQL.
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
        // collector_number is stored raw and mixed-case (e.g. "10E-105"); compare exactly,
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

fn trigram_candidates(idx: &Archived<TrigramIndex>, word: &str) -> Option<Vec<u32>> {
    let bytes = word.as_bytes();
    if bytes.len() < 3 { return None; }

    let mut lists: Vec<Vec<u32>> = Vec::with_capacity(bytes.len() - 2);
    for w in bytes.windows(3) {
        match idx.get(&[w[0], w[1], w[2]]) {
            Some(list) => lists.push(list.iter().map(|x| u32::from(*x)).collect()),
            // A trigram absent from the index appears in no card: nothing can match.
            None => return Some(Vec::new()),
        }
    }
    lists.sort_unstable_by_key(|l| l.len());

    let mut result = lists.swap_remove(0);
    result.sort_unstable();
    for list in &lists {
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
// Sorted Vec<(i16, u32)> maps field value -> card index for cmc/power/toughness.
// i16 covers both u8 (cmc: 0-255) and i8 (power/toughness: -128-127) without loss.
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
fn numeric_candidates(idx: &Archived<NumericIndex>, op: CmpOp, val: f64) -> Option<Vec<u32>> {
    let (start, end) = match op {
        CmpOp::Ne => return None,
        CmpOp::Eq => {
            if val.fract() != 0.0 { return Some(Vec::new()); }
            let s = idx.partition_point(|p| (i16::from(p.0) as f64) < val);
            let e = idx.partition_point(|p| (i16::from(p.0) as f64) <= val);
            (s, e)
        }
        CmpOp::Lt => (0, idx.partition_point(|p| (i16::from(p.0) as f64) < val)),
        CmpOp::Le => (0, idx.partition_point(|p| (i16::from(p.0) as f64) <= val)),
        CmpOp::Gt => (idx.partition_point(|p| (i16::from(p.0) as f64) <= val), idx.len()),
        CmpOp::Ge => (idx.partition_point(|p| (i16::from(p.0) as f64) < val), idx.len()),
    };
    let mut result: Vec<u32> = idx[start..end].iter().map(|p| u32::from(p.1)).collect();
    result.sort_unstable();
    Some(result)
}

// ─── Tag index ───────────────────────────────────────────────────────────────
// tag name -> sorted list of card indices that have that tag.
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

#[derive(Archive, Serialize, Deserialize)]
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

#[derive(Archive, Serialize, Deserialize)]
struct CardData {
    cards:   Vec<Card>,
    indexes: CardIndexes,
    // The writer's format→shift assignments. Persisted so reader processes —
    // which never run the load path that feeds FORMAT_SHIFTS — resolve
    // legality shifts identically to the worker that built the archive.
    format_shifts: HashMap<String, u8>,
}

/// Adopt the archive's format→shift assignments into this process's registry.
/// Cheap no-op (one read lock) once the registry has caught up.
fn sync_format_shifts(archived: &Archived<HashMap<String, u8>>) {
    let behind = format_shifts().read().map(|m| m.len() < archived.len()).unwrap_or(false);
    if !behind {
        return;
    }
    if let Ok(mut shifts) = format_shifts().write() {
        for (format, shift) in archived.iter() {
            shifts.insert(format.as_str().to_string(), *shift);
        }
    }
}

// ─── Candidate narrowing ─────────────────────────────────────────────────────

fn narrow_candidates(filter: &FilterExpr, indexes: &Archived<CardIndexes>) -> Option<Vec<u32>> {
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
                    let bit_list: Vec<u32> = indexes.type_bits[bit].iter().map(|x| u32::from(*x)).collect();
                    result = union_sorted(result, bit_list);
                }
            }
            Some(result)
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::Subtypes) && matches!(op, CmpOp::Ge) =>
        {
            indexes.subtypes.get(value.as_str()).map(|v| v.iter().map(|x| u32::from(*x)).collect())
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::Keywords) && matches!(op, CmpOp::Ge) =>
        {
            indexes.keywords.get(value.as_str()).map(|v| v.iter().map(|x| u32::from(*x)).collect())
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::OracleTags) && matches!(op, CmpOp::Ge) =>
        {
            indexes.oracle_tags.get(value.as_str()).map(|v| v.iter().map(|x| u32::from(*x)).collect())
        }

        FilterExpr::CollectionCmp { field, op, value }
            if matches!(field, CollField::IsTags) && matches!(op, CmpOp::Ge) =>
        {
            indexes.is_tags.get(value.as_str()).map(|v| v.iter().map(|x| u32::from(*x)).collect())
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

#[derive(Clone, Copy)]
enum Prefer { Oldest, Newest, UsdLow, UsdHigh, Promo, Default }

fn prefer_from_str(s: &str) -> Prefer {
    match s {
        "oldest"   => Prefer::Oldest,
        "newest"   => Prefer::Newest,
        "usd_low"  => Prefer::UsdLow,
        "usd_high" => Prefer::UsdHigh,
        "promo"    => Prefer::Promo,
        _          => Prefer::Default,
    }
}

fn prefer_score(card: &ACard, prefer: Prefer) -> f64 {
    match prefer {
        Prefer::Oldest  => -(card.released_at_int.as_ref().map(|v| u32::from(*v)).unwrap_or(99_999_999) as f64),
        Prefer::Newest  => card.released_at_int.as_ref().map(|v| u32::from(*v)).unwrap_or(0) as f64,
        Prefer::UsdLow  => -(card.price_usd.as_ref().map(|v| f32::from(*v)).unwrap_or(f32::INFINITY) as f64),
        Prefer::UsdHigh => card.price_usd.as_ref().map(|v| f32::from(*v)).unwrap_or(0.0) as f64,
        Prefer::Promo   => -(card.edhrec_rank.as_ref().map(|r| u32::from(*r) as f64).unwrap_or(f64::INFINITY)),
        Prefer::Default => card.prefer_score.as_ref().map(|v| f32::from(*v)).unwrap_or(0.0) as f64,
    }
}

#[derive(Clone, Copy)]
enum SortCol { Cmc, Power, Toughness, Rarity, PriceUsd, Cubecobra, EdhrecRank }

fn orderby_to_col(orderby: &str) -> SortCol {
    match orderby {
        "cmc"       => SortCol::Cmc,
        "power"     => SortCol::Power,
        "rarity"    => SortCol::Rarity,
        "toughness" => SortCol::Toughness,
        "usd"       => SortCol::PriceUsd,
        "cubecobra" => SortCol::Cubecobra,
        _           => SortCol::EdhrecRank,
    }
}

/// Map an f32 to a u32 that orders like `f32::total_cmp` (sign-flip trick).
fn f32_sort_bits(v: f32) -> u32 {
    let b = v.to_bits();
    if b & (1 << 31) != 0 { !b } else { b | (1 << 31) }
}

/// Order-preserving integer sort key, computed once per match instead of inside the
/// comparator: primary column (direction folded in by negation, missing sorts last),
/// then edhrec rank ascending (missing last), then prefer score descending (missing
/// last). Selection then compares plain u128s; full ties fall back to store pointer
/// order in `select_page` — the same tie order the original stable sort produced.
fn sort_key_bits(card: &ACard, sort_col: SortCol, descending: bool) -> u128 {
    let primary: Option<f32> = match sort_col {
        SortCol::Cmc        => card.cmc.as_ref().map(|v| u8::from(*v) as f32),
        SortCol::Power      => card.creature_power.as_ref().map(|v| i8::from(*v) as f32),
        SortCol::Toughness  => card.creature_toughness.as_ref().map(|v| i8::from(*v) as f32),
        SortCol::Rarity     => card.card_rarity_int.as_ref().map(|v| u8::from(*v) as f32),
        SortCol::PriceUsd   => card.price_usd.as_ref().map(|v| f32::from(*v)),
        SortCol::Cubecobra  => card.cubecobra_score.as_ref().map(|v| f32::from(*v)),
        SortCol::EdhrecRank => card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32),
    };
    let p = primary.map_or(u32::MAX, |v| f32_sort_bits(if descending { -v } else { v }));
    let e = card.edhrec_rank.as_ref().map(|v| u32::from(*v)).unwrap_or(u32::MAX);
    let s = card.prefer_score.as_ref().map_or(u32::MAX, |v| f32_sort_bits(-f32::from(*v)));
    ((p as u128) << 64) | ((e as u128) << 32) | (s as u128)
}

/// Quickselect the page `[offset, offset+limit)` into position and sort only that
/// segment. The first select bounds the page from above (everything past it stays
/// unsorted); the second bounds it from below and is skipped in the common
/// offset == 0 case. O(n + limit·log limit) instead of O(n·log n).
fn select_page<'a>(mut v: Vec<(u128, &'a ACard)>, offset: usize, limit: usize) -> Vec<&'a ACard> {
    let end = offset.saturating_add(limit).min(v.len());
    if offset >= end {
        return Vec::new();
    }
    let cmp = |a: &(u128, &ACard), b: &(u128, &ACard)| {
        a.0.cmp(&b.0).then_with(|| std::ptr::from_ref(a.1).cmp(&std::ptr::from_ref(b.1)))
    };
    if end < v.len() {
        v.select_nth_unstable_by(end, cmp);
    }
    if offset > 0 {
        v[..end].select_nth_unstable_by(offset, cmp);
    }
    v[offset..end].sort_unstable_by(cmp);
    v.truncate(end);
    v.drain(..offset);
    v.into_iter().map(|(_, c)| c).collect()
}

fn run_query_hashmap<'a>(
    store: &'a [ACard],
    filter: &FilterExpr,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
    offset: usize,
) -> (usize, Vec<&'a ACard>) {
    let sort_col   = orderby_to_col(orderby);
    let descending = direction == "desc";
    let prefer     = prefer_from_str(prefer);

    enum GroupBy { Oracle, Artwork, Printing }
    let group_by = match unique {
        "artwork"  => GroupBy::Artwork,
        "printing" => GroupBy::Printing,
        _          => GroupBy::Oracle,
    };

    let mut partitions: HashMap<usize, (&ACard, f64)> = HashMap::new();
    for card in store {
        if filter.matches(card) {
            let key = match group_by {
                GroupBy::Oracle   => u32::from(card.oracle_group) as usize,
                GroupBy::Artwork  => u32::from(card.artwork_group) as usize,
                // every printing is its own partition — the pointer is a free unique key
                GroupBy::Printing => std::ptr::from_ref(card) as usize,
            };
            let score = prefer_score(card, prefer);
            let entry = partitions.entry(key).or_insert((card, f64::NEG_INFINITY));
            if score > entry.1 { *entry = (card, score); }
        }
    }

    let best: Vec<(u128, &ACard)> = partitions
        .into_values()
        .map(|(c, _)| (sort_key_bits(c, sort_col, descending), c))
        .collect();
    let total = best.len();
    (total, select_page(best, offset, limit))
}

fn run_query_linear<'a, I, F>(
    cards: I,
    filter: &FilterExpr,
    key_fn: F,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
    offset: usize,
) -> (usize, Vec<&'a ACard>)
where
    I: Iterator<Item = &'a ACard>,
    F: Fn(&ACard) -> u32,
{
    let sort_col   = orderby_to_col(orderby);
    let descending = direction == "desc";
    let prefer     = prefer_from_str(prefer);

    let mut best: Vec<(u128, &ACard)> = Vec::new();
    let mut group_best: Option<(&ACard, f64)> = None;
    let mut prev_key: u32 = u32::MAX; // group ids are dense from 0, so MAX is a safe sentinel

    for card in cards {
        if !filter.matches(card) { continue; }
        let key = key_fn(card);
        if key != prev_key {
            if let Some((c, _)) = group_best.take() { best.push((sort_key_bits(c, sort_col, descending), c)); }
            prev_key   = key;
            group_best = Some((card, prefer_score(card, prefer)));
        } else {
            let score = prefer_score(card, prefer);
            if score > group_best.as_ref().map_or(f64::NEG_INFINITY, |g| g.1) {
                group_best = Some((card, score));
            }
        }
    }
    if let Some((c, _)) = group_best { best.push((sort_key_bits(c, sort_col, descending), c)); }

    let total = best.len();
    (total, select_page(best, offset, limit))
}

fn run_query_no_dedup<'a>(
    cards: impl Iterator<Item = &'a ACard>,
    filter: &FilterExpr,
    orderby: &str,
    direction: &str,
    limit: usize,
    offset: usize,
) -> (usize, Vec<&'a ACard>) {
    let sort_col   = orderby_to_col(orderby);
    let descending = direction == "desc";
    let matched: Vec<(u128, &ACard)> = cards
        .filter(|c| filter.matches(c))
        .map(|c| (sort_key_bits(c, sort_col, descending), c))
        .collect();
    let total = matched.len();
    (total, select_page(matched, offset, limit))
}

fn run_query<'a>(
    store: &'a [ACard],
    filter: &FilterExpr,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
    offset: usize,
    indexes: &Archived<CardIndexes>,
) -> (usize, Vec<&'a ACard>) {
    let candidates = narrow_candidates(filter, indexes);

    macro_rules! cards_iter {
        () => {
            match &candidates {
                Some(idxs) => Box::new(idxs.iter().map(|&i| &store[i as usize])) as Box<dyn Iterator<Item = &ACard>>,
                None       => Box::new(store.iter()),
            }
        };
    }

    match unique {
        "card" => run_query_linear(cards_iter!(), filter, |c| u32::from(c.oracle_group), prefer, orderby, direction, limit, offset),
        // Scryfall assigns each illustration_id to exactly one oracle_id, so cards sharing an
        // illustration_id are always contiguous in the (oracle_id, illustration_id) sort order.
        // The linear dedup path is therefore correct here — no HashMap needed.
        "artwork"  => run_query_linear(cards_iter!(), filter, |c| u32::from(c.artwork_group), prefer, orderby, direction, limit, offset),
        "printing" => run_query_no_dedup(cards_iter!(), filter, orderby, direction, limit, offset),
        _          => run_query_hashmap(store, filter, unique, prefer, orderby, direction, limit, offset),
    }
}

fn card_to_pydict<'py>(py: Python<'py>, card: &ACard) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("name", card.card_name.as_str())?;
    d.set_item("set_code", card.card_set_code.as_str())?;
    d.set_item("collector_number", card.collector_number.as_str())?;
    d.set_item("power", card.creature_power_text.as_ref().map(|s| s.as_str()))?;
    d.set_item("toughness", card.creature_toughness_text.as_ref().map(|s| s.as_str()))?;
    d.set_item("mana_cost", card.mana_cost_text.as_ref().map(|s| s.as_str()))?;
    d.set_item("oracle_text", card.oracle_text.as_str())?;
    d.set_item("set_name", card.set_name.as_str())?;
    d.set_item("type_line", card.type_line.as_str())?;
    Ok(d)
}

// ─── PyO3 bindings ───────────────────────────────────────────────────────────

struct CachedMmap {
    mmap: Arc<Mmap>,
    inode: u64,
}

#[pyclass]
struct QueryEngine {
    shm_path: PathBuf,
    write_lock: Mutex<()>,
    cached_mmap: Mutex<Option<CachedMmap>>,
}

impl QueryEngine {
    // Returns the cached mmap, remapping if the on-disk inode has changed since
    // the last remap (i.e. another worker wrote a new archive via rename).
    // One stat(2) per query; remap only when the inode actually changes.
    fn get_mmap(&self) -> PyResult<Arc<Mmap>> {
        let path_inode = std::fs::metadata(&self.shm_path)
            .map(|m| m.ino())
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("stat shm: {e}")))?;

        let mut guard = self.cached_mmap.lock().unwrap();
        if let Some(ref c) = *guard {
            if c.inode == path_inode {
                return Ok(Arc::clone(&c.mmap));
            }
        }
        // Inode changed (new reload) or first call: open and map the current file.
        let file = std::fs::File::open(&self.shm_path)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("open shm: {e}")))?;
        // Safety: bytes written by rkyv::to_bytes on this platform; file is replaced
        // atomically (rename), never modified in place while mapped.
        let mmap = Arc::new(unsafe { Mmap::map(&file) }
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("mmap: {e}")))?);
        *guard = Some(CachedMmap { mmap: Arc::clone(&mmap), inode: path_inode });
        Ok(mmap)
    }
}

#[pymethods]
impl QueryEngine {
    #[new]
    #[pyo3(signature = (shm_path=None))]
    fn new(shm_path: Option<&str>) -> Self {
        // Use /dev/shm on Linux (shared memory), fall back to /tmp on macOS.
        let default_path = if cfg!(target_os = "linux") {
            "/dev/shm/arcane_tutor_cards"
        } else {
            "/tmp/arcane_tutor_cards"
        };
        QueryEngine {
            shm_path: PathBuf::from(shm_path.unwrap_or(default_path)),
            write_lock: Mutex::new(()),
            cached_mmap: Mutex::new(None),  // populated by first reload()
        }
    }

    fn remap(&self) -> PyResult<()> {
        // Force a remap by clearing the cached inode so get_mmap() re-opens.
        if let Some(ref mut c) = *self.cached_mmap.lock().unwrap() {
            c.inode = 0;
        }
        self.get_mmap().map(|_| ())
    }

    fn reload(&self, db_rows: &Bound<PyList>) -> PyResult<()> {
        let _guard = self.write_lock.lock().unwrap();

        // Record when we started waiting so we can detect whether another worker
        // wrote the file while we were blocked on the cross-process flock below.
        let started_at = std::time::SystemTime::now();

        // Cross-process exclusive lock: only one worker writes per reload cycle.
        // The lock file is separate so it persists across archive replacements.
        let lock_path = self.shm_path.with_extension("lock");
        let lock_file = std::fs::OpenOptions::new()
            .write(true).create(true).open(&lock_path)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("open lock: {e}")))?;
        // LOCK_EX blocks until we hold the lock; released automatically on drop.
        unsafe { libc::flock(lock_file.as_raw_fd(), libc::LOCK_EX) };

        // If another worker already wrote the file while we were waiting, skip
        // the rebuild and just remap our local handle.
        let already_written = std::fs::metadata(&self.shm_path)
            .and_then(|m| m.modified())
            .map(|mtime| mtime > started_at)
            .unwrap_or(false);
        if already_written {
            return self.get_mmap().map(|_| ());
        }

        let mut cards: Vec<Card> = db_rows
            .iter()
            .filter_map(|item| item.cast::<PyDict>().ok().map(|d| card_from_pydict(&d)))
            .collect();
        // unique=card/artwork group by oracle_id, so cards without one would all
        // collapse into a single group. The DB enforces NOT NULL; fail loudly here
        // for any other caller (e.g. hand-built test dicts).
        if let Some((idx, card)) = cards
            .iter()
            .enumerate()
            .find(|(_, c)| c.oracle_id.as_deref().unwrap_or("").is_empty())
        {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "card {idx} ({:?}) is missing oracle_id (required for unique=card dedup)",
                card.card_name
            )));
        }
        cards.sort_unstable_by(|a, b| {
            let oa = a.oracle_id.as_deref().unwrap_or("");
            let ob = b.oracle_id.as_deref().unwrap_or("");
            oa.cmp(ob).then_with(|| {
                let ia = a.illustration_id.as_deref().unwrap_or("");
                let ib = b.illustration_id.as_deref().unwrap_or("");
                ia.cmp(ib)
            })
        });

        // Dense group ids: a group is a maximal run of equal keys in the sort above,
        // exactly the adjacency the linear dedup paths relied on when they compared
        // the UUID strings directly. cards[0] keeps the 0/0 defaults.
        let mut oracle_group: u32 = 0;
        let mut artwork_group: u32 = 0;
        for i in 1..cards.len() {
            let (head, tail) = cards.split_at_mut(i);
            let (prev, cur) = (&head[i - 1], &mut tail[0]);
            if prev.oracle_id.as_deref().unwrap_or("") != cur.oracle_id.as_deref().unwrap_or("") {
                oracle_group += 1;
            }
            if prev.illustration_id.as_deref().unwrap_or("") != cur.illustration_id.as_deref().unwrap_or("") {
                artwork_group += 1;
            }
            cur.oracle_group  = oracle_group;
            cur.artwork_group = artwork_group;
        }

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

        // Snapshot the registry card_from_pydict just populated so reader
        // processes can adopt the same format→shift assignments.
        let format_shifts_snapshot = format_shifts().read().map(|m| m.clone()).unwrap_or_default();
        let card_data = CardData { cards, indexes, format_shifts: format_shifts_snapshot };
        let bytes = rkyv::to_bytes::<rkyv::rancor::Error>(&card_data)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("rkyv serialize: {e}")))?;

        // Write atomically: write to a per-PID .tmp, then rename over shm_path.
        // Per-PID avoids the race where two workers write to the same .tmp and
        // one's rename consumes the file before the other can rename it.
        let tmp_name = format!(
            "{}.{}.tmp",
            self.shm_path.file_name().unwrap_or_default().to_string_lossy(),
            std::process::id(),
        );
        let tmp_path = self.shm_path.with_file_name(tmp_name);
        {
            let mut f = std::fs::File::create(&tmp_path)
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("create tmp: {e}")))?;
            f.write_all(&bytes)
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("write tmp: {e}")))?;
        }
        std::fs::rename(&tmp_path, &self.shm_path)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("rename shm: {e}")))?;

        self.get_mmap().map(|_| ())
    }

    #[pyo3(signature = (*, filters, unique="card", prefer="default", orderby="edhrec", direction="asc", limit=100, offset=0))]
    fn query<'py>(
        &self,
        py: Python<'py>,
        filters: &Bound<PyAny>,
        unique: &str,
        prefer: &str,
        orderby: &str,
        direction: &str,
        limit: usize,
        offset: usize,
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
        // get_mmap() remaps automatically if the on-disk inode has changed since
        // the last reload, keeping workers off stale (deleted) mappings.
        let mmap = self.get_mmap()?;
        // Safety: bytes were written by rkyv::to_bytes on this platform.
        // The archive is immutable once written; reloads atomically replace the
        // file, never modifying it in place. The Arc keeps the mapping alive.
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(&mmap) };

        // Must run before build_filter so legality shifts resolve in workers
        // that never executed the load path themselves.
        sync_format_shifts(&data.format_shifts);
        let filter_expr = build_filter(&json_val)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("build_filter: {e}")))?;

        let store: &[ACard] = &data.cards;
        let (total, page) = run_query(
            store, &filter_expr, unique, prefer, orderby, direction, limit, offset, &data.indexes,
        );

        let matches: Vec<Bound<PyDict>> = page.iter().map(|c| card_to_pydict(py, c)).collect::<PyResult<Vec<_>>>()?;
        let matches_list = PyList::new(py, matches)?;
        PyTuple::new(py, [total.into_pyobject(py)?.into_any(), matches_list.into_any()])
    }

    /// Same as query() but forces the HashMap dedup path. Used for benchmarking.
    #[pyo3(signature = (*, filters, unique="card", prefer="default", orderby="edhrec", direction="asc", limit=100, offset=0))]
    fn query_hashmap<'py>(
        &self,
        py: Python<'py>,
        filters: &Bound<PyAny>,
        unique: &str,
        prefer: &str,
        orderby: &str,
        direction: &str,
        limit: usize,
        offset: usize,
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
        let mmap = self.get_mmap()?;
        // Safety: same as query().
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(&mmap) };

        // Must run before build_filter; see query().
        sync_format_shifts(&data.format_shifts);
        let filter_expr = build_filter(&json_val)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("build_filter: {e}")))?;

        let store: &[ACard] = &data.cards;
        let (total, page) = run_query_hashmap(store, &filter_expr, unique, prefer, orderby, direction, limit, offset);
        let matches: Vec<Bound<PyDict>> = page.iter().map(|c| card_to_pydict(py, c)).collect::<PyResult<Vec<_>>>()?;
        let matches_list = PyList::new(py, matches)?;
        PyTuple::new(py, [total.into_pyobject(py)?.into_any(), matches_list.into_any()])
    }

    fn size(&self) -> PyResult<usize> {
        match self.get_mmap() {
            Err(_) => Ok(0),
            // Safety: same as query().
            Ok(mmap) => Ok(unsafe { rkyv::access_unchecked::<Archived<CardData>>(&mmap) }.cards.len()),
        }
    }
}

#[pymodule]
mod card_engine {
    #[pymodule_export]
    use super::QueryEngine;
}

#[cfg(test)]
mod tests {
    use super::*;
    use rkyv::rancor::Error;

    /// Build a TrigramIndex mapping each word's trigrams to the given card ids.
    fn index_of(words: &[(&str, &[u32])]) -> TrigramIndex {
        let mut idx: TrigramIndex = HashMap::new();
        for (word, cards) in words {
            for w in word.as_bytes().windows(3) {
                let entry = idx.entry([w[0], w[1], w[2]]).or_default();
                for &c in *cards {
                    if !entry.contains(&c) { entry.push(c); }
                }
                entry.sort_unstable();
            }
        }
        idx
    }

    /// Archive the index and query it, matching how the engine reads the shared snapshot.
    fn candidates(idx: &TrigramIndex, word: &str) -> Option<Vec<u32>> {
        let bytes = rkyv::to_bytes::<Error>(idx).expect("serialize trigram index");
        let archived = rkyv::access::<Archived<TrigramIndex>, Error>(&bytes).expect("access trigram index");
        trigram_candidates(archived, word)
    }

    #[test]
    fn trigram_short_word_cannot_narrow() {
        let idx = index_of(&[("bolt", &[1, 2])]);
        assert_eq!(candidates(&idx, "bo"), None);
    }

    #[test]
    fn trigram_all_present_intersects() {
        // "bol" → {1,2,3}, "olt" → {1,2}: intersection is {1,2}
        let idx = index_of(&[("bol", &[1, 2, 3]), ("olt", &[1, 2])]);
        assert_eq!(candidates(&idx, "bolt"), Some(vec![1, 2]));
    }

    #[test]
    fn trigram_missing_means_no_candidates() {
        // "bol" is indexed but "olx" appears in no card, which proves nothing can
        // match — the result must be the empty candidate set, not an intersection
        // of whichever trigrams happen to exist.
        let idx = index_of(&[("bolt", &[1, 2])]);
        assert_eq!(candidates(&idx, "bolx"), Some(Vec::new()));
    }

    #[test]
    fn trigram_fully_unindexed_word_is_empty_not_unnarrowed() {
        let idx = index_of(&[("bolt", &[1, 2])]);
        assert_eq!(candidates(&idx, "zzz"), Some(Vec::new()));
    }

    // Verify that HashMap<[u8; 3], Vec<u32>> (the trigram index key type) round-trips
    // through rkyv and supports lookup via the same [u8; 3] key type.
    #[test]
    fn test_trigram_index_archive_and_lookup() {
        let mut idx: HashMap<[u8; 3], Vec<u32>> = HashMap::new();
        idx.insert([b'a', b'b', b'c'], vec![1, 2, 3]);
        idx.insert([b'x', b'y', b'z'], vec![4, 5]);
        idx.insert([b'f', b'o', b'o'], vec![10, 20, 30, 40]);

        let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize trigram index");
        let archived = rkyv::access::<rkyv::Archived<HashMap<[u8; 3], Vec<u32>>>, Error>(&bytes)
            .expect("access trigram index");

        // Key present -- length check
        let abc = archived.get(&[b'a', b'b', b'c']).expect("abc must be present");
        assert_eq!(abc.len(), 3);

        // Key present -- value iteration (elements are rend::u32_le, not u32)
        let foo: Vec<u32> = archived
            .get(&[b'f', b'o', b'o'])
            .expect("foo must be present")
            .iter()
            .map(|x| u32::from(*x))
            .collect();
        assert_eq!(foo, vec![10, 20, 30, 40]);

        // Key absent
        assert!(archived.get(&[b'z', b'z', b'z']).is_none());
    }

    // Verify that HashSet<String> supports contains() via &str (Borrow-based lookup).
    // This is used for card_keywords, card_oracle_tags, card_is_tags, card_frame_data.
    #[test]
    fn test_hashset_string_str_lookup() {
        let mut set: HashSet<String> = HashSet::new();
        set.insert("Flying".to_string());
        set.insert("Vigilance".to_string());
        set.insert("Trample".to_string());

        let bytes = rkyv::to_bytes::<Error>(&set).expect("serialize hashset");
        let archived = rkyv::access::<rkyv::Archived<HashSet<String>>, Error>(&bytes)
            .expect("access hashset");

        assert!(archived.contains("Flying"));
        assert!(archived.contains("Trample"));
        assert!(!archived.contains("Deathtouch"));
    }

    // Verify that HashMap<String, Vec<u32>> (the tag index value type) supports
    // get() via &str, which is needed for narrow_candidates tag lookups.
    #[test]
    fn test_tag_index_str_lookup() {
        let mut idx: HashMap<String, Vec<u32>> = HashMap::new();
        idx.insert("merfolk".to_string(), vec![1, 5, 9]);
        idx.insert("dragon".to_string(), vec![2, 7]);

        let bytes = rkyv::to_bytes::<Error>(&idx).expect("serialize tag index");
        let archived = rkyv::access::<rkyv::Archived<HashMap<String, Vec<u32>>>, Error>(&bytes)
            .expect("access tag index");

        let merfolk: Vec<u32> = archived
            .get("merfolk")
            .expect("merfolk must be present")
            .iter()
            .map(|x| u32::from(*x))
            .collect();
        assert_eq!(merfolk, vec![1, 5, 9]);
        assert!(archived.get("angel").is_none());
    }
}
