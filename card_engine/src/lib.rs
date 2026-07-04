use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDate, PyDateAccess, PyDict, PyList, PyTuple};
use rkyv::{Archive, Archived, Deserialize, Serialize};
use memmap2::Mmap;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::Write as IoWrite;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::os::unix::io::AsRawFd;
use std::os::unix::fs::MetadataExt;

// Raised for malformed query input (bad filter JSON, unbuildable filter expression). Subclasses
// ValueError so existing `except ValueError` call sites keep working; new call sites can catch
// this specifically to distinguish "the query was bad" from unrelated ValueErrors.
create_exception!(card_engine, QueryError, PyValueError, "Raised when a query cannot be parsed or built.");

// Subclass of QueryError (not a sibling) so `except QueryError` already catches it; callers that
// need to distinguish "requested a field that doesn't exist" from other query errors can catch
// this specifically instead.
create_exception!(card_engine, UnknownFieldError, QueryError, "Raised when `fields` names an unknown field.");

// ─── Feature-gated counting allocator (memory measurement only) ──────────────
// Counts live bytes / live allocations of this extension's Rust heap and records
// a breakdown of reload(): see docs/issues/engine-store-size-reduction.md step 0.

#[cfg(feature = "alloc-counter")]
mod alloc_stats;

// ─── Inline string (no heap allocation) ──────────────────────────────────────

mod inline_str;
use inline_str::InlineStr;

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

pub(crate) fn card_type_str_to_bit(s: &str) -> u16 {
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

pub(crate) fn color_list_to_mask(colors: &[&str]) -> u8 {
    colors.iter().fold(0u8, |acc, c| acc | color_to_bit(c))
}

// ─── Mana cost helpers ───────────────────────────────────────────────────────

/// Symbols that contribute to devotion, matching calculate_devotion() in SQL
/// (which counts only WUBRGC characters of the mana cost string).
pub(crate) fn is_devotion_sym(s: &str) -> bool {
    s.len() == 1 && "WUBRGC".contains(s)
}

pub(crate) fn mana_pip_counts(s: &str) -> HashMap<String, u8> {
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

pub(crate) fn mana_cmc(s: &str) -> f32 {
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

// ─── Card / printing structs ─────────────────────────────────────────────────
// The store is two-level: ~31.5k OracleCards, each owning a contiguous range of
// the ~97k Printings (CardData.offsets is the CSR boundary table). Fields that
// are constant across all printings of an oracle id live on OracleCard, stored
// once; per-printing fields live on Printing. Verified against the tagged DB
// (2026-07-03): every hoisted field is printing-constant except card_legalities
// (genuinely per-printing for non-tournament sets like 30A / Collectors'
// Edition / gold-border — see the legality_divergent flag) and 3 oracle ids
// with layout-dependent face-name assembly (first printing's value wins).
// Design: docs/issues/engine-card-printing-split.md / issue #603.

#[derive(Archive, Serialize, Deserialize, Clone)]
struct ManaCost {
    pips: HashMap<String, u8>,              // faithful to mana_cost_jsonb; used for mana= queries
    devotion: Option<HashMap<String, u8>>,  // Some only when hybrids are present; used for devotion queries
    cmc: f32,
}

#[derive(Archive, Serialize, Deserialize)]
struct OracleCard {
    // Hot fields first — fits in the first cache lines for fast filter short-circuiting.
    card_name_lower: InlineStr<61>, // 61 bytes covers every card name in the Scryfall dataset
    card_colors: u8,
    card_color_identity: u8,
    produced_mana: u8,
    card_types: u16,
    // True for the ~556 oracle ids whose printings carry different legality
    // words (non-tournament printings: 30A, Collectors' Edition, gold border).
    // When set, legality filters defer to each Printing's card_legalities; when
    // clear (~98.2% of cards), the card-level word below is exact.
    legality_divergent: bool,

    // 0 = null; see parse_uuid_or_hash().
    oracle_id: u128,

    // Interned string ids into CardData.strings (NONE_STR = absent). Identical
    // values share one table entry; resolve with str_at()/the strings slice.
    card_name_id: u32,
    oracle_text_id: u32,
    oracle_text_lower_id: u32,
    card_layout_id: u32,
    mana_cost_text_id: u32,
    type_line_id: u32,

    cmc: Option<u8>,                  // always an integer; max ~16 in practice
    creature_power: Option<i8>,       // can be negative (e.g. Char-Rumbler)
    creature_toughness: Option<i8>,
    planeswalker_loyalty: Option<u8>, // always 1-12
    edhrec_rank: Option<u32>,         // up to ~30k unique cards
    cubecobra_score: Option<f32>,

    // Collection elements interned as u16 ids into CardData.coll_vocab (see
    // VocabInterner). card_subtypes preserves the printed order; the set-like
    // collections are sorted by id and deduped at load.
    card_subtypes: Vec<u16>,
    card_keywords: Vec<u16>,
    card_oracle_tags: Vec<u16>,
    // 2 bits per format, positions from the FORMAT_SHIFTS registry. The word
    // shared by this card's printings; exact unless legality_divergent.
    card_legalities: u64,

    mana_cost: ManaCost,

    creature_power_text_id: u32,
    creature_toughness_text_id: u32,
}

#[derive(Archive, Serialize, Deserialize)]
struct Printing {
    // UUIDs packed as u128, 0 = null. Real UUIDs keep their exact bit value (so
    // future lookup-by-id can match Scryfall's); non-UUID strings from hand-built
    // test dicts are hashed deterministically — see parse_uuid_or_hash().
    scryfall_id: u128,
    illustration_id: u128,

    flavor_text_id: u32,
    flavor_text_lower_id: u32,
    // Interned id into CardData.artist_vocab (~2.2k distinct lowercase artist
    // names); ARTIST_NONE = absent. Artist predicates resolve their match set
    // against the vocab once per query (FilterExpr::ArtistMatch), so no artist
    // strings live on the printing.
    card_artist_vid: u16,
    card_set_code: InlineStr<8>,
    card_border_id: u32,
    card_watermark_id: u32,
    collector_number_id: u32,
    set_name_id: u32,
    released_at_int: Option<u32>,      // yyyymmdd, parsed once at load; date/year filters and prefer use this

    card_rarity_int: Option<u8>,       // 0-5
    collector_number_int: Option<u16>, // some sets exceed i8::MAX
    price_usd: Option<f32>,
    price_eur: Option<f32>,
    price_tix: Option<f32>,
    prefer_score: Option<f32>,

    // This printing's exact legality word; only consulted when the owning
    // card's legality_divergent flag is set.
    card_legalities: u64,

    card_art_tags: Vec<u16>,
    card_is_tags: Vec<u16>,
    card_frame_data: Vec<u16>,
}

/// Parse-time row: one DB row (= one printing) with every field, before the
/// commit pass groups rows by oracle_id and splits them into OracleCard +
/// Printing. Never archived.
struct CardRow {
    card_name_lower: InlineStr<61>,
    card_colors: u8,
    card_color_identity: u8,
    produced_mana: u8,
    card_types: u16,

    scryfall_id: u128,
    oracle_id: u128,
    illustration_id: u128,

    card_name_id: u32,
    oracle_text_id: u32,
    oracle_text_lower_id: u32,
    flavor_text_id: u32,
    flavor_text_lower_id: u32,
    card_artist_vid: u16,
    card_set_code: InlineStr<8>,
    card_layout_id: u32,
    card_border_id: u32,
    card_watermark_id: u32,
    collector_number_id: u32,
    mana_cost_text_id: u32,
    type_line_id: u32,
    set_name_id: u32,
    released_at_int: Option<u32>,

    cmc: Option<u8>,
    creature_power: Option<i8>,
    creature_toughness: Option<i8>,
    planeswalker_loyalty: Option<u8>,
    card_rarity_int: Option<u8>,
    collector_number_int: Option<u16>,
    edhrec_rank: Option<u32>,
    price_usd: Option<f32>,
    price_eur: Option<f32>,
    price_tix: Option<f32>,
    prefer_score: Option<f32>,
    cubecobra_score: Option<f32>,

    card_subtypes: Vec<u16>,
    card_keywords: Vec<u16>,
    card_legalities: u64,
    card_oracle_tags: Vec<u16>,
    card_art_tags: Vec<u16>,
    card_is_tags: Vec<u16>,
    card_frame_data: Vec<u16>,

    mana_cost: ManaCost,

    creature_power_text_id: u32,
    creature_toughness_text_id: u32,
}

// Type aliases for the archived (mmap-backed) store types
pub(crate) type AOracleCard = Archived<OracleCard>;
pub(crate) type APrinting = Archived<Printing>;
// Archived string table (CardData.strings)
pub(crate) type AStrings = Archived<Vec<String>>;
// Archived CSR boundary table (CardData.offsets)
pub(crate) type AOffsets = Archived<Vec<u32>>;

/// Sentinel id for absent optional strings (a card never has 4 billion distinct strings).
const NONE_STR: u32 = u32::MAX;

/// Sentinel for a printing with no artist (see Printing.card_artist_vid).
pub(crate) const ARTIST_NONE: u16 = u16::MAX;

/// Resolve an interned id against the archived string table; None for absent.
pub(crate) fn str_at(strings: &AStrings, id: u32) -> Option<&str> {
    if id == NONE_STR { None } else { Some(strings[id as usize].as_str()) }
}

/// Build-time hash-consing interner; `strings` becomes CardData.strings.
struct Interner {
    map: HashMap<String, u32>,
    strings: Vec<String>,
}

impl Interner {
    fn new() -> Self {
        // Pre-intern "" as id 0: plain (non-optional) fields default to it when missing.
        let mut it = Interner { map: HashMap::new(), strings: Vec::new() };
        it.intern(String::new());
        it
    }

    fn intern(&mut self, s: String) -> u32 {
        if let Some(&id) = self.map.get(&s) {
            return id;
        }
        let id = self.strings.len() as u32;
        self.strings.push(s.clone());
        self.map.insert(s, id);
        id
    }

    fn intern_opt(&mut self, s: Option<String>) -> u32 {
        match s {
            Some(v) => self.intern(v),
            None => NONE_STR,
        }
    }
}

/// Build-time interner for collection elements (subtypes, keywords, tags, frame
/// data); `strings` becomes CardData.coll_vocab. Ids are u16 — the combined
/// vocabulary is ~16k distinct values, so 65,536 leaves ~4× headroom; interning
/// fails loudly rather than silently truncating if that is ever exceeded.
struct VocabInterner {
    map: HashMap<String, u16>,
    strings: Vec<String>,
}

impl VocabInterner {
    fn new() -> Self {
        VocabInterner { map: HashMap::new(), strings: Vec::new() }
    }

    fn intern(&mut self, s: String) -> PyResult<u16> {
        if let Some(&id) = self.map.get(&s) {
            return Ok(id);
        }
        let id = u16::try_from(self.strings.len()).map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "collection vocabulary exceeded u16::MAX distinct values; widen Card's collection ids to u32",
            )
        })?;
        self.strings.push(s.clone());
        self.map.insert(s, id);
        Ok(id)
    }
}

// ─── Loading helpers ─────────────────────────────────────────────────────────

fn opt_str(d: &Bound<PyDict>, key: &str) -> Option<String> {
    d.get_item(key).ok().flatten().and_then(|v| v.extract::<String>().ok())
}

/// UUID string → u128. Hyphenated/plain 32-hex-digit UUIDs map to their exact bit
/// value (so future lookup-by-id matches Scryfall's ids); any other non-empty
/// string (hand-built test dicts use ids like "o1") is FNV-1a-hashed, preserving
/// equality semantics. 0 is reserved for null/missing; real values never map to it.
fn parse_uuid_or_hash(s: &str) -> u128 {
    if s.is_empty() {
        return 0;
    }
    let mut val: u128 = 0;
    let mut digits = 0u32;
    let mut is_uuid = true;
    for b in s.bytes() {
        if b == b'-' {
            continue;
        }
        match (b as char).to_digit(16) {
            Some(dv) if digits < 32 => {
                val = (val << 4) | dv as u128;
                digits += 1;
            }
            _ => {
                is_uuid = false;
                break;
            }
        }
    }
    if is_uuid && digits == 32 {
        return if val == 0 { 1 } else { val }; // all-zero UUID must not collide with null
    }
    // FNV-1a (128-bit) fallback for non-UUID strings
    const FNV_OFFSET: u128 = 0x6c62272e07bb014262b821756295c58d;
    const FNV_PRIME: u128 = 0x0000000001000000000000000000013b;
    let mut h = FNV_OFFSET;
    for b in s.bytes() {
        h ^= b as u128;
        h = h.wrapping_mul(FNV_PRIME);
    }
    if h == 0 { 1 } else { h }
}

fn opt_uuid(d: &Bound<PyDict>, key: &str) -> u128 {
    let Some(v) = d.get_item(key).ok().flatten() else { return 0 };
    // psycopg returns uuid.UUID objects natively; try that first.
    if let Ok(u) = v.extract::<uuid::Uuid>() {
        let bits = u.as_u128();
        // 0 is reserved as the null sentinel; the all-zeros UUID is remapped to 1
        // (matching parse_uuid_or_hash's behaviour for genuine UUIDs).
        return if bits == 0 { 1 } else { bits };
    }
    // Fall back to string for hand-built test dicts and any other string form.
    if let Ok(s) = v.extract::<String>() {
        return parse_uuid_or_hash(&s);
    }
    0
}

/// Inverse of `parse_uuid_or_hash` for genuine UUIDs: rebuilds a `Uuid` from the exact bit value
/// (converted to Python's `uuid.UUID` via pyo3's `uuid` feature). 0 is the null sentinel. Only
/// meaningful for real UUID input — non-UUID strings went through the FNV-1a fallback in
/// `parse_uuid_or_hash` and can't be recovered from their hash, which matters only for
/// hand-built test ids, never real card data.
fn uuid_from_u128(v: u128) -> Option<uuid::Uuid> {
    if v == 0 {
        None
    } else {
        Some(uuid::Uuid::from_u128(v))
    }
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

/// Interned vocab ids of a JSON list of strings, preserving element order
/// (card_subtypes keeps the printed subtype order).
fn str_list_to_ids(d: &Bound<PyDict>, key: &str, vocab: &mut VocabInterner) -> PyResult<Vec<u16>> {
    str_list(d, key).into_iter().map(|s| vocab.intern(s)).collect()
}

/// Interned vocab ids of a JSONB object's keys, sorted and deduped — the set-like
/// collections (keywords, tags, frame data) as sorted `Vec<u16>`.
fn jsonb_obj_to_ids(d: &Bound<PyDict>, key: &str, vocab: &mut VocabInterner) -> PyResult<Vec<u16>> {
    let mut ids: Vec<u16> = d
        .get_item(key)
        .ok()
        .flatten()
        .and_then(|v| {
            v.cast::<PyDict>().ok().map(|m| {
                m.keys()
                    .iter()
                    .filter_map(|k| k.extract::<String>().ok())
                    .map(|s| vocab.intern(s))
                    .collect::<PyResult<Vec<u16>>>()
            })
        })
        .transpose()?
        .unwrap_or_default();
    ids.sort_unstable();
    ids.dedup();
    Ok(ids)
}

// ─── Format legality bitmap ──────────────────────────────────────────────────

mod legality;
use legality::*;

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
                    // WUBRGC: SQL's calculate_devotion counts C too ({C/W} hybrids)
                    if is_devotion_sym(part) {
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

fn card_from_pydict(d: &Bound<PyDict>, it: &mut Interner, vocab: &mut VocabInterner, artists: &mut VocabInterner) -> PyResult<CardRow> {
    let released_at = opt_date_str(d, "released_at").unwrap_or_default();
    let released_at_int: Option<u32> = released_at.replace('-', "").parse().ok();
    // Raw strings from the dict; interned to ids as the struct is built below.
    let card_name = opt_str(d, "card_name").unwrap_or_default();
    let card_name_lower = InlineStr::<61>::from_str(&card_name.to_lowercase());
    let oracle_text = opt_str(d, "oracle_text").unwrap_or_default();
    let oracle_text_lower_id = it.intern(oracle_text.to_lowercase());
    let flavor_text = opt_str(d, "flavor_text").unwrap_or_default();
    let flavor_text_lower_id = it.intern(flavor_text.to_lowercase());
    let card_artist_vid = match opt_str(d, "card_artist") {
        Some(a) => artists.intern(a.to_lowercase())?,
        None => ARTIST_NONE,
    };

    Ok(CardRow {
        scryfall_id: opt_uuid(d, "scryfall_id"),
        oracle_id: opt_uuid(d, "oracle_id"),
        illustration_id: opt_uuid(d, "illustration_id"),

        card_name_lower,
        card_name_id: it.intern(card_name),
        oracle_text_lower_id,
        oracle_text_id: it.intern(oracle_text),
        flavor_text_lower_id,
        flavor_text_id: it.intern(flavor_text),
        card_artist_vid,
        card_set_code: InlineStr::<8>::from_str(&opt_str(d, "card_set_code").unwrap_or_default()),
        card_layout_id: it.intern(opt_str(d, "card_layout").unwrap_or_default()),
        card_border_id: it.intern(opt_str(d, "card_border").unwrap_or_default()),
        card_watermark_id: it.intern_opt(opt_str(d, "card_watermark")),
        collector_number_id: it.intern(opt_str(d, "collector_number").unwrap_or_default()),
        mana_cost_text_id: it.intern_opt(opt_str(d, "mana_cost_text")),
        type_line_id: it.intern(opt_str(d, "type_line").unwrap_or_default()),
        set_name_id: it.intern(opt_str(d, "set_name").unwrap_or_default()),
        released_at_int,

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
        card_subtypes: str_list_to_ids(d, "card_subtypes", vocab)?,
        card_keywords: jsonb_obj_to_ids(d, "card_keywords", vocab)?,
        card_legalities: jsonb_obj_to_legality_bits(d, "card_legalities"),
        card_oracle_tags: jsonb_obj_to_ids(d, "card_oracle_tags", vocab)?,
        card_art_tags: jsonb_obj_to_ids(d, "card_art_tags", vocab)?,
        card_is_tags: jsonb_obj_to_ids(d, "card_is_tags", vocab)?,
        card_frame_data: jsonb_obj_to_ids(d, "card_frame_data", vocab)?,

        mana_cost: mana_cost_from_pydict(d, opt_f32(d, "cmc")),

        creature_power_text_id: it.intern_opt(opt_str(d, "creature_power_text")),
        creature_toughness_text_id: it.intern_opt(opt_str(d, "creature_toughness_text")),
    })
}

// ─── Filter expression & builder ─────────────────────────────────────────────

mod filter;
use filter::*;

// ─── Trigram index ────────────────────────────────────────────────────────────

type TrigramIndex = HashMap<[u8; 3], Vec<u32>>;

/// Oracle-text trigram index, deduplicated by distinct text.
///
/// Distinct oracle cards still share text (~31.5k cards, ~28k distinct texts —
/// identical text under different oracle ids), so the posting lists hold *dense
/// text ids* — a private 0..n_texts numbering of the distinct
/// `oracle_text_lower_id` values — and a CSR (compressed sparse row) table
/// expands a text id back to the cards that carry it. Logically the CSR is an
/// array-of-arrays `expansion[text_id] → [card indices]`, flattened into two
/// allocations so it archives as two contiguous, zero-copy slices.
#[derive(Archive, Serialize, Deserialize, Default)]
struct OracleTextIndex {
    /// trigram → ascending list of dense text ids whose text contains it.
    trigrams: TrigramIndex,
    /// Row boundaries: cards of text id `t` live at
    /// `card_indices[offsets[t] .. offsets[t + 1]]`. Length n_texts + 1.
    offsets: Vec<u32>,
    /// All card indices, grouped by text id; every card appears exactly once
    /// (its text interned to exactly one id), so expansion can never duplicate.
    card_indices: Vec<u32>,
}

fn build_oracle_text_index(cards: &[OracleCard], strings: &[String]) -> OracleTextIndex {
    // Dense remap: the interner's ids index the *global* string table (oracle texts
    // mixed with type lines, set names, ...), so the distinct oracle texts are sparse
    // in that space. Re-number just them, first-seen order, so the CSR table below
    // has no empty rows and posting ids stay small.
    let mut dense: HashMap<u32, u32> = HashMap::new();
    let mut text_id_of_card: Vec<u32> = Vec::with_capacity(cards.len());
    for c in cards {
        let next = dense.len() as u32;
        text_id_of_card.push(*dense.entry(c.oracle_text_lower_id).or_insert(next));
    }
    let n_texts = dense.len();

    // Invert the remap (dense id → global id) so each distinct text is visited once.
    let mut global_of_dense: Vec<u32> = vec![0; n_texts];
    for (&global, &d) in &dense {
        global_of_dense[d as usize] = global;
    }

    // Trigram postings over distinct texts only — the window-sliding loop runs once
    // per text instead of once per printing. Visiting texts in ascending dense-id
    // order appends ids in ascending order, giving the sorted posting lists that
    // intersect_sorted() requires, with no per-list sort.
    let mut trigrams: TrigramIndex = HashMap::new();
    for (d, &global) in global_of_dense.iter().enumerate() {
        let bytes = strings[global as usize].as_bytes();
        if bytes.len() < 3 {
            continue;
        }
        for w in bytes.windows(3) {
            let list = trigrams.entry([w[0], w[1], w[2]]).or_default();
            if list.last() != Some(&(d as u32)) {
                list.push(d as u32);
            }
        }
    }

    // CSR expansion table via counting sort: count cards per text, prefix-sum
    // the counts into row offsets, then place each card index in its row. Placement
    // walks cards in store order, so every row comes out sorted by card index.
    let mut offsets: Vec<u32> = vec![0; n_texts + 1];
    for &t in &text_id_of_card {
        offsets[t as usize + 1] += 1;
    }
    for i in 1..offsets.len() {
        offsets[i] += offsets[i - 1];
    }
    let mut cursor: Vec<u32> = offsets.clone();
    let mut card_indices: Vec<u32> = vec![0; cards.len()];
    for (card_idx, &t) in text_id_of_card.iter().enumerate() {
        card_indices[cursor[t as usize] as usize] = card_idx as u32;
        cursor[t as usize] += 1;
    }

    OracleTextIndex { trigrams, offsets, card_indices }
}

/// Expand surviving dense text ids to card indices via the CSR table.
///
/// Each row is internally sorted (placement above walks store order), but rows are
/// not ordered relative to each other (dense ids are first-seen order), so the
/// concatenation needs one final sort — required both by intersect_sorted() when
/// And-combining with other candidate sets and by the query driver, which
/// assumes candidates arrive in store order.
fn expand_text_ids(idx: &Archived<OracleTextIndex>, text_ids: &[u32]) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::new();
    for &t in text_ids {
        let start = u32::from(idx.offsets[t as usize]) as usize;
        let end = u32::from(idx.offsets[t as usize + 1]) as usize;
        out.extend(idx.card_indices[start..end].iter().map(|x| u32::from(*x)));
    }
    out.sort_unstable();
    out
}

// Named lifetime (not elided/HRTB) so get_text may return text borrowed from the
// string table rather than from the card itself.
fn build_trigram_index<'a, T>(rows: &'a [T], get_text: impl Fn(&'a T) -> &'a str) -> TrigramIndex {
    let mut idx: TrigramIndex = HashMap::new();
    for (i, card) in rows.iter().enumerate() {
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

// Generic over the second operand's element type so it can walk archived
// posting lists (u32_le) in place, without copying them out of the mmap.
fn intersect_sorted<B: Copy>(a: &[u32], b: &[B]) -> Vec<u32>
where
    u32: From<B>,
{
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        let bj = u32::from(b[j]);
        if a[i] == bj      { out.push(a[i]); i += 1; j += 1; }
        else if a[i] < bj  { i += 1; }
        else               { j += 1; }
    }
    out
}

fn trigram_candidates(idx: &Archived<TrigramIndex>, word: &str) -> Option<Vec<u32>> {
    let bytes = word.as_bytes();
    if bytes.len() < 3 { return None; }

    let mut lists: Vec<&Archived<Vec<u32>>> = Vec::with_capacity(bytes.len() - 2);
    for w in bytes.windows(3) {
        match idx.get(&[w[0], w[1], w[2]]) {
            Some(list) => lists.push(list),
            // A trigram absent from the index appears in no card: nothing can match.
            None => return Some(Vec::new()),
        }
    }
    lists.sort_unstable_by_key(|l| l.len());
    // Repeated trigrams (e.g. "aaaa") resolve to the same archived entry — drop
    // the pointer-equal duplicates instead of intersecting them again.
    lists.dedup_by(|a, b| std::ptr::eq(*a, *b));

    // Posting lists are built sorted (build_trigram_index and the oracle CSR
    // both append ids in ascending order), so intersection runs directly over
    // the archived lists; only the shortest one is materialized as the
    // working set.
    let mut result: Vec<u32> = lists[0].iter().map(|x| u32::from(*x)).collect();
    for list in &lists[1..] {
        if result.is_empty() { break; }
        result = intersect_sorted(&result, list.as_slice());
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

fn build_numeric_index(cards: &[OracleCard], get_val: impl Fn(&OracleCard) -> Option<i16>) -> NumericIndex {
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
// tag name -> sorted list of store indices that have that tag. Card-level
// collections (subtypes/keywords/oracle_tags) post card ids; printing-level ones
// (art_tags/is_tags) post printing ids — see the space notes on CardIndexes.
// Lists are naturally sorted because rows are iterated in index order.

type TagIndex = HashMap<String, Vec<u32>>;

/// Build a tag/list index from interned collection ids. Accumulates postings by
/// vocab id in the hot loop (integer keys, no per-element string hashing), then
/// resolves each id to its owned String key once at the end.
fn build_tag_index<T>(rows: &[T], vocab: &[String], get_ids: impl Fn(&T) -> &Vec<u16>) -> TagIndex {
    let mut by_id: HashMap<u16, Vec<u32>> = HashMap::new();
    for (i, row) in rows.iter().enumerate() {
        for &id in get_ids(row) {
            by_id.entry(id).or_default().push(i as u32);
        }
    }
    by_id
        .into_iter()
        .map(|(id, postings)| (vocab[id as usize].clone(), postings))
        .collect()
}

// ─── Artist index ─────────────────────────────────────────────────────────────
// CSR from artist vocab id → printing ids (each row sorted; placement walks
// store order). Artist predicates resolve their matching vocab ids once per
// query (bind), then expand the surviving rows here to narrow in printing space.

#[derive(Archive, Serialize, Deserialize, Default)]
struct ArtistIndex {
    /// Row boundaries: printings of artist id `a` live at
    /// `printings[offsets[a] .. offsets[a + 1]]`. Length n_artists + 1.
    offsets: Vec<u32>,
    printings: Vec<u32>,
}

fn build_artist_index(printings: &[Printing], n_artists: usize) -> ArtistIndex {
    let mut offsets = vec![0u32; n_artists + 1];
    for p in printings {
        if p.card_artist_vid != ARTIST_NONE {
            offsets[p.card_artist_vid as usize + 1] += 1;
        }
    }
    for i in 1..offsets.len() {
        offsets[i] += offsets[i - 1];
    }
    let mut cursor = offsets.clone();
    let mut out = vec![0u32; offsets[n_artists] as usize];
    for (i, p) in printings.iter().enumerate() {
        if p.card_artist_vid != ARTIST_NONE {
            let a = p.card_artist_vid as usize;
            out[cursor[a] as usize] = i as u32;
            cursor[a] += 1;
        }
    }
    ArtistIndex { offsets, printings: out }
}

/// Expand matching artist vocab ids to sorted printing ids via the CSR table.
fn expand_artist_ids(idx: &Archived<ArtistIndex>, artist_ids: &[u16]) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::new();
    for &a in artist_ids {
        let start = u32::from(idx.offsets[a as usize]) as usize;
        let end = u32::from(idx.offsets[a as usize + 1]) as usize;
        out.extend(idx.printings[start..end].iter().map(|x| u32::from(*x)));
    }
    out.sort_unstable();
    out
}

// ─── Released-at index ────────────────────────────────────────────────────────
// Sorted (yyyymmdd, printing idx); binary-searched ranges answer date/year
// filters in printing space. Printings without a date are absent (they can
// never satisfy a date comparison — SQL NULL semantics).

type DateIndex = Vec<(u32, u32)>;

fn build_date_index(printings: &[Printing]) -> DateIndex {
    let mut idx: DateIndex = printings
        .iter()
        .enumerate()
        .filter_map(|(i, p)| p.released_at_int.map(|d| (d, i as u32)))
        .collect();
    idx.sort_unstable();
    idx
}

/// Sorted printing ids with released_at in [lo, hi). Callers translate ops into
/// half-open ranges; Ne is not selective and never narrows.
fn date_range_candidates(idx: &Archived<DateIndex>, lo: u32, hi: u32) -> Vec<u32> {
    let s = idx.partition_point(|p| u32::from(p.0) < lo);
    let e = idx.partition_point(|p| u32::from(p.0) < hi);
    let mut result: Vec<u32> = idx[s..e].iter().map(|p| u32::from(p.1)).collect();
    result.sort_unstable();
    result
}

// ─── Type bit index ───────────────────────────────────────────────────────────
// One sorted Vec<u32> per type bit (14 bits, matching the TYPE_* constants).
// Bit position N corresponds to TYPE_* = 1 << N.

type TypeIndex = [Vec<u32>; 14];

fn build_type_index(cards: &[OracleCard]) -> TypeIndex {
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

// ─── Combined indexes ────────────────────────────────────────────────────────

// Postings live in two id spaces: card-level indexes post OracleCard indices
// (~31.5k), printing-level indexes post Printing indices (~97k). Candidates
// carry their space (see Candidates) and convert at combine points.
#[derive(Archive, Serialize, Deserialize)]
struct CardIndexes {
    name_trigram:   TrigramIndex,    // card space
    oracle_trigram: OracleTextIndex, // card space (via dense text ids)
    cmc:            NumericIndex,    // card space
    power:          NumericIndex,    // card space
    toughness:      NumericIndex,    // card space
    type_bits:      TypeIndex,       // card space
    subtypes:       TagIndex,        // card space
    keywords:       TagIndex,        // card space
    oracle_tags:    TagIndex,        // card space
    art_tags:       TagIndex,        // printing space
    is_tags:        TagIndex,        // printing space
    artists:        ArtistIndex,     // printing space (CSR by artist vocab id)
    set_codes:      TagIndex,        // printing space
    released_at:    DateIndex,       // printing space
}

impl Default for CardIndexes {
    fn default() -> Self {
        CardIndexes {
            name_trigram:   HashMap::new(),
            oracle_trigram: OracleTextIndex::default(),
            cmc:            Vec::new(),
            power:          Vec::new(),
            toughness:      Vec::new(),
            type_bits:      Default::default(),
            subtypes:       HashMap::new(),
            keywords:       HashMap::new(),
            oracle_tags:    HashMap::new(),
            art_tags:       HashMap::new(),
            is_tags:        HashMap::new(),
            artists:        ArtistIndex::default(),
            set_codes:      HashMap::new(),
            released_at:    Vec::new(),
        }
    }
}

#[derive(Archive, Serialize, Deserialize)]
struct CardData {
    // ~31.5k oracle cards; printings of card i are
    // printings[offsets[i]..offsets[i+1]], sorted by descending default
    // prefer_score within the range (ties by illustration_id), so the
    // default-prefer walk can stop at the first matching printing.
    cards:     Vec<OracleCard>,
    printings: Vec<Printing>,
    // CSR boundary table, length cards.len() + 1.
    offsets:   Vec<u32>,
    // Hash-consed table for the interned-string fields (see Interner).
    strings: Vec<String>,
    // Vocab table for the collection fields, indexed by their u16 ids
    // (see VocabInterner). ~16k entries / ~200 KB.
    coll_vocab: Vec<String>,
    // Permutation of 0..coll_vocab.len() sorted by string, so query values
    // resolve to vocab ids by binary search (FilterExpr::bind).
    coll_vocab_sorted: Vec<u16>,
    // Distinct lowercase artist names, indexed by Printing.card_artist_vid.
    // Artist predicates (contains/exact/regex) evaluate against these ~2.2k
    // strings once per query instead of per printing.
    artist_vocab: Vec<String>,
    indexes: CardIndexes,
    // The writer's format→shift assignments. Persisted so reader processes —
    // which never run the load path that feeds FORMAT_SHIFTS — resolve
    // legality shifts identically to the worker that built the archive.
    format_shifts: HashMap<String, u8>,
}

// ─── Candidate narrowing ─────────────────────────────────────────────────────

/// A narrowed candidate set, tagged with the id space its members live in.
/// Narrowing is advisory (the driver re-verifies), so converting between spaces
/// can only loosen or tighten candidates, never change results.
enum Candidates {
    Cards(Vec<u32>),
    Printings(Vec<u32>),
}

/// Map a sorted printing-id list up to its sorted card-id list. Printings are
/// grouped contiguously by card, so the mapped list arrives sorted with adjacent
/// duplicates — dedup is a single linear pass.
fn cards_of_printings(offsets: &AOffsets, printing_ids: &[u32]) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::with_capacity(printing_ids.len());
    for &p in printing_ids {
        let card = offsets.partition_point(|o| u32::from(*o) <= p) as u32 - 1;
        if out.last() != Some(&card) {
            out.push(card);
        }
    }
    out
}

impl Candidates {
    /// Project into card space (identity for card-space sets).
    fn into_cards(self, offsets: &AOffsets) -> Vec<u32> {
        match self {
            Candidates::Cards(v) => v,
            Candidates::Printings(v) => cards_of_printings(offsets, &v),
        }
    }
}

fn narrow_candidates(filter: &FilterExpr, indexes: &Archived<CardIndexes>, offsets: &AOffsets) -> Option<Candidates> {
    match filter {
        FilterExpr::TextContains { field, word }
            if word.len() >= 3
                && matches!(field, TextSearchField::NameLower | TextSearchField::OracleTextLower) =>
        {
            match field {
                TextSearchField::NameLower => trigram_candidates(&indexes.name_trigram, word).map(Candidates::Cards),
                // Oracle postings are in dense text-id space (see OracleTextIndex);
                // intersect there, then expand the survivors to card indices
                // through the CSR table.
                _ => trigram_candidates(&indexes.oracle_trigram.trigrams, word)
                    .map(|text_ids| Candidates::Cards(expand_text_ids(&indexes.oracle_trigram, &text_ids))),
            }
        }

        FilterExpr::NumericCmp { lhs, op, rhs } => match (lhs, rhs) {
            (NumExpr::Field(NumField::Cmc), NumExpr::Const(v)) =>
                numeric_candidates(&indexes.cmc, *op, *v).map(Candidates::Cards),
            (NumExpr::Const(v), NumExpr::Field(NumField::Cmc)) =>
                numeric_candidates(&indexes.cmc, flip_op(*op), *v).map(Candidates::Cards),
            (NumExpr::Field(NumField::Power), NumExpr::Const(v)) =>
                numeric_candidates(&indexes.power, *op, *v).map(Candidates::Cards),
            (NumExpr::Const(v), NumExpr::Field(NumField::Power)) =>
                numeric_candidates(&indexes.power, flip_op(*op), *v).map(Candidates::Cards),
            (NumExpr::Field(NumField::Toughness), NumExpr::Const(v)) =>
                numeric_candidates(&indexes.toughness, *op, *v).map(Candidates::Cards),
            (NumExpr::Const(v), NumExpr::Field(NumField::Toughness)) =>
                numeric_candidates(&indexes.toughness, flip_op(*op), *v).map(Candidates::Cards),
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
            Some(Candidates::Cards(result))
        }

        FilterExpr::CollectionCmp { field, op, value, .. } if matches!(op, CmpOp::Ge) => {
            let (idx, space): (_, fn(Vec<u32>) -> Candidates) = match field {
                CollField::Subtypes   => (&indexes.subtypes,    Candidates::Cards as fn(_) -> _),
                CollField::Keywords   => (&indexes.keywords,    Candidates::Cards),
                CollField::OracleTags => (&indexes.oracle_tags, Candidates::Cards),
                CollField::ArtTags    => (&indexes.art_tags,    Candidates::Printings),
                CollField::IsTags     => (&indexes.is_tags,     Candidates::Printings),
                CollField::FrameData  => return None, // no index (low-selectivity values dominate)
            };
            idx.get(value.as_str()).map(|v| space(v.iter().map(|x| u32::from(*x)).collect()))
        }

        FilterExpr::ArtistMatch { ids } => {
            // ids resolved at bind time; empty means no artist satisfies the
            // predicate, which proves the empty candidate set.
            Some(Candidates::Printings(expand_artist_ids(&indexes.artists, ids)))
        }

        FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value } => {
            // A set code absent from the index appears on no printing: narrowing
            // to the empty set is exact, matching the tag-index convention would
            // be None, but unlike tags the index covers every non-empty code.
            Some(Candidates::Printings(
                indexes.set_codes.get(value.as_str()).map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(*x)).collect()),
            ))
        }

        FilterExpr::DateCmp { op, value } => {
            let (lo, hi) = match op {
                CmpOp::Ne => return None,
                CmpOp::Eq => (*value, value.saturating_add(1)),
                CmpOp::Lt => (0, *value),
                CmpOp::Le => (0, value.saturating_add(1)),
                CmpOp::Gt => (value.saturating_add(1), u32::MAX),
                CmpOp::Ge => (*value, u32::MAX),
            };
            Some(Candidates::Printings(date_range_candidates(&indexes.released_at, lo, hi)))
        }

        FilterExpr::YearCmp { op, year } => {
            if *year < 0 || *year > 9999 {
                return None;
            }
            let y = *year as u32;
            let (lo, hi) = match op {
                CmpOp::Ne => return None,
                CmpOp::Eq => (y * 10_000, (y + 1) * 10_000),
                CmpOp::Lt => (0, y * 10_000),
                CmpOp::Le => (0, (y + 1) * 10_000),
                CmpOp::Gt => ((y + 1) * 10_000, u32::MAX),
                CmpOp::Ge => (y * 10_000, u32::MAX),
            };
            Some(Candidates::Printings(date_range_candidates(&indexes.released_at, lo, hi)))
        }

        FilterExpr::And(children) => {
            // Combine within each id space first (card lists are ~3× shorter),
            // then cross the boundary once by projecting the printing product up.
            // Projection loses which printings matched — the driver's per-printing
            // verification restores exactness.
            let mut card_sets: Vec<Vec<u32>> = Vec::new();
            let mut printing_sets: Vec<Vec<u32>> = Vec::new();
            for c in children {
                match narrow_candidates(c, indexes, offsets) {
                    Some(Candidates::Cards(v)) => card_sets.push(v),
                    Some(Candidates::Printings(v)) => printing_sets.push(v),
                    None => {}
                }
            }
            let intersect_all = |mut sets: Vec<Vec<u32>>| -> Option<Vec<u32>> {
                if sets.is_empty() { return None; }
                sets.sort_unstable_by_key(|s| s.len());
                let mut result = sets.swap_remove(0);
                for set in sets {
                    if result.is_empty() { break; }
                    result = intersect_sorted(&result, &set);
                }
                Some(result)
            };
            let cards = intersect_all(card_sets);
            let printings = intersect_all(printing_sets);
            match (cards, printings) {
                (None, None) => None,
                (Some(c), None) => Some(Candidates::Cards(c)),
                (None, Some(p)) => Some(Candidates::Printings(p)),
                (Some(c), Some(p)) => {
                    let p_cards = cards_of_printings(offsets, &p);
                    Some(Candidates::Cards(intersect_sorted(&c, &p_cards)))
                }
            }
        }

        FilterExpr::Or(children) => {
            // Every child must narrow or the union is unbounded. Mixed spaces
            // union in card space (projection up is loosening-only, and the
            // driver re-verifies).
            let mut sets: Vec<Candidates> = Vec::with_capacity(children.len());
            for child in children {
                match narrow_candidates(child, indexes, offsets) {
                    None => return None,
                    Some(c) => sets.push(c),
                }
            }
            if sets.iter().all(|s| matches!(s, Candidates::Printings(_))) {
                let mut union: Vec<u32> = Vec::new();
                for s in sets {
                    if let Candidates::Printings(v) = s { union = union_sorted(union, v); }
                }
                Some(Candidates::Printings(union))
            } else {
                let mut union: Vec<u32> = Vec::new();
                for s in sets {
                    union = union_sorted(union, s.into_cards(offsets));
                }
                Some(Candidates::Cards(union))
            }
        }

        _ => None,
    }
}

// ─── Sort / select / limit ────────────────────────────────────────────────────

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

/// Prefer score for one printing of a card; higher wins, and selection uses a
/// strict > so the first-in-store-order printing wins ties (matching the tie
/// behavior of the dedup paths this replaced).
fn prefer_score(card: &AOracleCard, p: &APrinting, prefer: Prefer) -> f64 {
    match prefer {
        Prefer::Oldest  => -(p.released_at_int.as_ref().map(|v| u32::from(*v)).unwrap_or(99_999_999) as f64),
        Prefer::Newest  => p.released_at_int.as_ref().map(|v| u32::from(*v)).unwrap_or(0) as f64,
        Prefer::UsdLow  => -(p.price_usd.as_ref().map(|v| f32::from(*v)).unwrap_or(f32::INFINITY) as f64),
        Prefer::UsdHigh => p.price_usd.as_ref().map(|v| f32::from(*v)).unwrap_or(0.0) as f64,
        // Card-level (edhrec is oracle-scoped): every printing ties, so the
        // first printing in store order is chosen — same as before the split.
        Prefer::Promo   => -(card.edhrec_rank.as_ref().map(|r| u32::from(*r) as f64).unwrap_or(f64::INFINITY)),
        Prefer::Default => p.prefer_score.as_ref().map(|v| f32::from(*v)).unwrap_or(0.0) as f64,
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
/// last). Card-level columns read the OracleCard; printing-level columns (rarity,
/// usd) read the chosen printing, matching the pre-split semantics where the
/// group's representative printing supplied them. Full ties fall back to printing
/// store order in `select_page`.
fn sort_key_bits(card: &AOracleCard, p: &APrinting, sort_col: SortCol, descending: bool) -> u128 {
    let primary: Option<f32> = match sort_col {
        SortCol::Cmc        => card.cmc.as_ref().map(|v| u8::from(*v) as f32),
        SortCol::Power      => card.creature_power.as_ref().map(|v| i8::from(*v) as f32),
        SortCol::Toughness  => card.creature_toughness.as_ref().map(|v| i8::from(*v) as f32),
        SortCol::Rarity     => p.card_rarity_int.as_ref().map(|v| u8::from(*v) as f32),
        SortCol::PriceUsd   => p.price_usd.as_ref().map(|v| f32::from(*v)),
        SortCol::Cubecobra  => card.cubecobra_score.as_ref().map(|v| f32::from(*v)),
        SortCol::EdhrecRank => card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32),
    };
    let pk = primary.map_or(u32::MAX, |v| f32_sort_bits(if descending { -v } else { v }));
    let e = card.edhrec_rank.as_ref().map(|v| u32::from(*v)).unwrap_or(u32::MAX);
    let sc = p.prefer_score.as_ref().map_or(u32::MAX, |v| f32_sort_bits(-f32::from(*v)));
    ((pk as u128) << 64) | ((e as u128) << 32) | (sc as u128)
}

/// One query match: (sort key, card index, printing index). Ties on the sort key
/// break by printing index — printing store order, the same tie order the
/// pre-split pointer comparison produced.
type Match = (u128, u32, u32);

/// Quickselect the page `[offset, offset+limit)` into position and sort only that
/// segment. The first select bounds the page from above (everything past it stays
/// unsorted); the second bounds it from below and is skipped in the common
/// offset == 0 case. O(n + limit·log limit) instead of O(n·log n).
fn select_page(mut v: Vec<Match>, offset: usize, limit: usize) -> Vec<(u32, u32)> {
    let end = offset.saturating_add(limit).min(v.len());
    if offset >= end {
        return Vec::new();
    }
    let cmp = |a: &Match, b: &Match| a.0.cmp(&b.0).then_with(|| a.2.cmp(&b.2));
    if end < v.len() {
        v.select_nth_unstable_by(end, cmp);
    }
    if offset > 0 {
        v[..end].select_nth_unstable_by(offset, cmp);
    }
    v[offset..end].sort_unstable_by(cmp);
    v.truncate(end);
    v.drain(..offset);
    v.into_iter().map(|(_, c, p)| (c, p)).collect()
}

// ─── Query driver ─────────────────────────────────────────────────────────────
// One structural walk replaces the pre-split linear/hashmap dedup paths and the
// preferred-printing fast path: grouping is the store's shape, not something to
// reconstruct per query. Per candidate card the filter is evaluated once at card
// level; only when it depends on printing-level fields (Tri::PrintingDep) are the
// card's printings evaluated individually.

fn run_query<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &FilterExpr,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
    page_offset: usize,
    indexes: &Archived<CardIndexes>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let sort_col   = orderby_to_col(orderby);
    let descending = direction == "desc";
    let prefer     = prefer_from_str(prefer);

    enum Mode { Card, Artwork, Printing }
    let mode = match unique {
        "artwork"  => Mode::Artwork,
        "printing" => Mode::Printing,
        _          => Mode::Card,
    };

    // Candidates in either space project to card ids for the walk; the walk's
    // per-printing verification restores exactness for printing-space losses.
    let candidate_cards: Option<Vec<u32>> =
        narrow_candidates(filter, indexes, offsets).map(|c| c.into_cards(offsets));
    let card_ids: Box<dyn Iterator<Item = u32>> = match &candidate_cards {
        Some(v) => Box::new(v.iter().copied()),
        None    => Box::new(0..cards.len() as u32),
    };

    let mut best: Vec<Match> = Vec::new();
    let mut groups: Vec<(u128, u32, f64)> = Vec::new(); // artwork-mode scratch, reused per card
    // card_pass residual: the top-level children still printing-dependent for
    // the current card (reused buffer; see FilterExpr::card_pass).
    let mut residual: Vec<&FilterExpr> = Vec::new();
    let mut residual_is_or = false;
    for cid in card_ids {
        let card = &cards[cid as usize];
        let all_match = match filter.card_pass(card, strings, &mut residual, &mut residual_is_or) {
            Tri::False | Tri::Null => continue,
            Tri::True => true,          // every printing matches: skip per-printing checks
            Tri::PrintingDep => false,  // verify each printing against the residual below
        };
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;

        match mode {
            Mode::Card => {
                // Printings are stored in descending default-prefer order, so
                // for the default prefer the first matching printing IS the
                // chosen one — O(1) when the card pass already said True.
                let chosen: Option<u32> = if matches!(prefer, Prefer::Default) {
                    let mut found: Option<u32> = None;
                    for pid in start..end {
                        if all_match || FilterExpr::residual_matches(card, &printings[pid], strings, &residual, residual_is_or) {
                            found = Some(pid as u32);
                            break;
                        }
                    }
                    found
                } else {
                    let mut chosen: Option<(u32, f64)> = None;
                    for pid in start..end {
                        let p = &printings[pid];
                        if !all_match && !FilterExpr::residual_matches(card, p, strings, &residual, residual_is_or) { continue; }
                        let score = prefer_score(card, p, prefer);
                        if chosen.is_none_or(|(_, s)| score > s) {
                            chosen = Some((pid as u32, score));
                        }
                    }
                    chosen.map(|(pid, _)| pid)
                };
                if let Some(pid) = chosen {
                    best.push((sort_key_bits(card, &printings[pid as usize], sort_col, descending), cid, pid));
                }
            }
            Mode::Printing => {
                for pid in start..end {
                    let p = &printings[pid];
                    if !all_match && !FilterExpr::residual_matches(card, p, strings, &residual, residual_is_or) { continue; }
                    best.push((sort_key_bits(card, p, sort_col, descending), cid, pid as u32));
                }
            }
            Mode::Artwork => {
                // Within-range order is prefer-score-desc (not illustration),
                // so group by illustration with a small per-card scan — ranges
                // are tiny (median 2 printings). `groups` is reused across
                // cards to avoid per-card allocation.
                groups.clear();
                for pid in start..end {
                    let p = &printings[pid];
                    if !all_match && !FilterExpr::residual_matches(card, p, strings, &residual, residual_is_or) { continue; }
                    let ill = u128::from(p.illustration_id);
                    let score = prefer_score(card, p, prefer);
                    match groups.iter_mut().find(|g: &&mut (u128, u32, f64)| g.0 == ill) {
                        Some(g) => {
                            if score > g.2 {
                                g.1 = pid as u32;
                                g.2 = score;
                            }
                        }
                        None => groups.push((ill, pid as u32, score)),
                    }
                }
                for &(_, bp, _) in groups.iter() {
                    best.push((sort_key_bits(card, &printings[bp as usize], sort_col, descending), cid, bp));
                }
            }
        }
    }

    let total = best.len();
    let page = select_page(best, page_offset, limit)
        .into_iter()
        .map(|(cid, pid)| (&cards[cid as usize], &printings[pid as usize]))
        .collect();
    (total, page)
}

// ─── Result field selection ───────────────────────────────────────────────────
// The vocabulary of fields a query result row can carry. `fields=None` resolves to
// DEFAULT_FIELDS (the 9 fields every caller got before field selection existed); an explicit
// `fields` list is validated and deduped against this same table by resolve_fields(). There is
// no separate hardcoded path for "the old fields" vs. "the new fields" — everything is an entry
// in FIELD_TABLE.
type FieldExtractor =
    for<'a> fn(Python<'a>, &'a AOracleCard, &'a APrinting, &'a AStrings, &'a AStrings) -> PyResult<Bound<'a, PyAny>>;

const FIELD_TABLE: &[(&str, FieldExtractor)] = &[
    ("name", |py, c, _p, s, _v| Ok(str_at(s, u32::from(c.card_name_id)).into_pyobject(py)?.into_any())),
    ("set_code", |py, _c, p, _s, _v| Ok(p.card_set_code.as_str().into_pyobject(py)?.into_any())),
    ("collector_number", |py, _c, p, s, _v| Ok(str_at(s, u32::from(p.collector_number_id)).into_pyobject(py)?.into_any())),
    ("power", |py, c, _p, s, _v| Ok(str_at(s, u32::from(c.creature_power_text_id)).into_pyobject(py)?.into_any())),
    ("toughness", |py, c, _p, s, _v| Ok(str_at(s, u32::from(c.creature_toughness_text_id)).into_pyobject(py)?.into_any())),
    ("mana_cost", |py, c, _p, s, _v| Ok(str_at(s, u32::from(c.mana_cost_text_id)).into_pyobject(py)?.into_any())),
    ("oracle_text", |py, c, _p, s, _v| Ok(str_at(s, u32::from(c.oracle_text_id)).into_pyobject(py)?.into_any())),
    ("set_name", |py, _c, p, s, _v| Ok(str_at(s, u32::from(p.set_name_id)).into_pyobject(py)?.into_any())),
    ("type_line", |py, c, _p, s, _v| Ok(str_at(s, u32::from(c.type_line_id)).into_pyobject(py)?.into_any())),
    ("illustration_id", |py, _c, p, _s, _v| Ok(uuid_from_u128(u128::from(p.illustration_id)).into_pyobject(py)?.into_any())),
    ("scryfall_id", |py, _c, p, _s, _v| Ok(uuid_from_u128(u128::from(p.scryfall_id)).into_pyobject(py)?.into_any())),
    ("price_usd", |py, _c, p, _s, _v| Ok(p.price_usd.as_ref().map(|v| f32::from(*v)).into_pyobject(py)?.into_any())),
    ("prefer_score", |py, _c, p, _s, _v| Ok(p.prefer_score.as_ref().map(|v| f32::from(*v)).into_pyobject(py)?.into_any())),
    // card_subtypes preserves the printed order; the set-like collections are stored
    // sorted by vocab id (first-seen order), so they get re-sorted lexicographically
    // for deterministic output.
    ("card_subtypes", |py, c, _p, _s, v| {
        let items: Vec<&str> = c.card_subtypes.iter().map(|id| coll_str(v, u16::from(*id))).collect();
        Ok(items.into_pyobject(py)?.into_any())
    }),
    ("card_keywords", |py, c, _p, _s, v| Ok(sorted_strs(v, &c.card_keywords).into_pyobject(py)?.into_any())),
    ("card_oracle_tags", |py, c, _p, _s, v| Ok(sorted_strs(v, &c.card_oracle_tags).into_pyobject(py)?.into_any())),
    ("card_art_tags", |py, _c, p, _s, v| Ok(sorted_strs(v, &p.card_art_tags).into_pyobject(py)?.into_any())),
    ("card_is_tags", |py, _c, p, _s, v| Ok(sorted_strs(v, &p.card_is_tags).into_pyobject(py)?.into_any())),
    ("card_frame_data", |py, _c, p, _s, v| Ok(sorted_strs(v, &p.card_frame_data).into_pyobject(py)?.into_any())),
];

/// Resolve one interned collection-element id against the archived vocab table.
/// Every id is a real entry (there is no absent sentinel for collection elements).
pub(crate) fn coll_str(vocab: &AStrings, id: u16) -> &str {
    vocab[id as usize].as_str()
}

/// Resolves interned collection ids to a lexicographically sorted `Vec<&str>` for
/// deterministic field output.
fn sorted_strs<'a>(vocab: &'a AStrings, ids: &Archived<Vec<u16>>) -> Vec<&'a str> {
    let mut v: Vec<&str> = ids.iter().map(|id| coll_str(vocab, u16::from(*id))).collect();
    v.sort_unstable();
    v
}

const DEFAULT_FIELDS: &[&str] =
    &["name", "set_code", "collector_number", "power", "toughness", "mana_cost", "oracle_text", "set_name", "type_line"];

/// Resolves a caller-requested field list into FIELD_TABLE entries, deduping repeats (a name
/// requested twice is only fetched/emitted once) and rejecting anything outside the vocabulary.
/// `None` resolves to DEFAULT_FIELDS. Called once per query, before the per-row loop, so the
/// per-row cost is a flat list of closure calls rather than a name comparison per field per card.
fn resolve_fields(fields: Option<Vec<String>>) -> PyResult<Vec<(&'static str, FieldExtractor)>> {
    let requested: Vec<&str> = match &fields {
        Some(v) => v.iter().map(String::as_str).collect(),
        None => DEFAULT_FIELDS.to_vec(),
    };
    let mut seen = HashSet::with_capacity(requested.len());
    let mut resolved = Vec::with_capacity(requested.len());
    for name in requested {
        if !seen.insert(name) {
            continue;
        }
        match FIELD_TABLE.iter().find(|(n, _)| *n == name) {
            Some(entry) => resolved.push(*entry),
            None => return Err(UnknownFieldError::new_err(format!("unknown field: {name:?}"))),
        }
    }
    Ok(resolved)
}

fn card_to_pydict<'py>(
    py: Python<'py>,
    card: &AOracleCard,
    printing: &APrinting,
    strings: &AStrings,
    vocab: &AStrings,
    fields: &[(&'static str, FieldExtractor)],
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (name, extractor) in fields {
        d.set_item(*name, extractor(py, card, printing, strings, vocab)?)?;
    }
    Ok(d)
}

// ─── Archive file header ─────────────────────────────────────────────────────
// A 16-byte header is prepended to the rkyv archive: magic, format version, and
// size_of::<AOracleCard> / size_of::<APrinting>. get_mmap() rejects any file whose header doesn't
// match this build, so an archive written by an older build (different archived
// layout) is treated as absent and rebuilt instead of being handed to
// access_unchecked — which would be undefined behavior. The 16-byte length also
// keeps the payload 16-aligned (the mmap base is page-aligned), satisfying
// rkyv's alignment requirement for the archived root.

const ARCHIVE_MAGIC: [u8; 8] = *b"ATCARDS\0";
/// Bump on any archived-data-model change the struct sizes below wouldn't
/// catch (e.g. reordering same-size fields, changing an index type).
const ARCHIVE_FORMAT_VERSION: u32 = 20260706;
const ARCHIVE_HEADER_LEN: usize = 16;

fn archive_header() -> [u8; ARCHIVE_HEADER_LEN] {
    let mut h = [0u8; ARCHIVE_HEADER_LEN];
    h[..8].copy_from_slice(&ARCHIVE_MAGIC);
    h[8..12].copy_from_slice(&ARCHIVE_FORMAT_VERSION.to_le_bytes());
    h[12..14].copy_from_slice(&(std::mem::size_of::<AOracleCard>() as u16).to_le_bytes());
    h[14..16].copy_from_slice(&(std::mem::size_of::<APrinting>() as u16).to_le_bytes());
    h
}

/// The rkyv payload of a mapping whose header get_mmap() already validated.
fn archive_payload(mmap: &Mmap) -> &[u8] {
    &mmap[ARCHIVE_HEADER_LEN..]
}

// ─── PyO3 bindings ───────────────────────────────────────────────────────────

struct CachedMmap {
    mmap: Arc<Mmap>,
    inode: u64,
}

/// In-progress staged reload: cards accumulated across add_batch() calls plus
/// the cross-process flock, held from reload_begin() until reload_commit() /
/// reload_abort() so no other process can interleave a write. Dropping the
/// staging (commit, abort, or a fresh reload_begin after an abandoned cycle)
/// closes the lock file, which releases the flock.
struct Staging {
    rows: Vec<CardRow>,
    interner: Interner,
    vocab: VocabInterner,
    artists: VocabInterner,
    #[allow(dead_code)] // held for its flock; released on drop
    lock_file: std::fs::File,
}

// Names ordered by bit position matching the TYPE_* constants (bit 0 = index 0, …).
const TYPE_BIT_NAMES: [&str; 14] = [
    "Artifact", "Basic", "Battle", "Conspiracy", "Creature", "Enchantment",
    "Instant", "Kindred", "Land", "Legendary", "Planeswalker", "Snow", "Sorcery", "World",
];

/// Count type and subtype occurrences across oracle cards (one per oracle id —
/// what "preferred printings" approximated before the card/printing split).
/// Accumulates by integer key in the hot loop — bit position for types, interned vocab
/// id for subtypes — then converts to owned strings once at the end.
pub(crate) fn count_common_types(data: &Archived<CardData>) -> HashMap<String, u32> {
    let mut type_counts = [0u32; 14];
    let mut subtype_counts: HashMap<u16, u32> = HashMap::new();

    for card in data.cards.iter() {
        let mut bits = u16::from(card.card_types);
        while bits != 0 {
            let pos = bits.trailing_zeros() as usize;
            type_counts[pos] += 1;
            bits &= bits - 1;
        }

        for id in card.card_subtypes.iter() {
            *subtype_counts.entry(u16::from(*id)).or_insert(0) += 1;
        }
    }

    let mut result: HashMap<String, u32> = HashMap::new();
    for (i, &count) in type_counts.iter().enumerate() {
        if count > 0 {
            result.insert(TYPE_BIT_NAMES[i].to_string(), count);
        }
    }
    for (id, count) in subtype_counts {
        result.insert(coll_str(&data.coll_vocab, id).to_string(), count);
    }
    result
}

/// Count keyword occurrences across oracle cards (one per oracle id).
pub(crate) fn count_common_keywords(data: &Archived<CardData>) -> HashMap<String, u32> {
    let mut keyword_counts: HashMap<u16, u32> = HashMap::new();

    for card in data.cards.iter() {
        for id in card.card_keywords.iter() {
            *keyword_counts.entry(u16::from(*id)).or_insert(0) += 1;
        }
    }

    keyword_counts
        .into_iter()
        .map(|(id, v)| (coll_str(&data.coll_vocab, id).to_string(), v))
        .collect()
}

#[pyclass]
struct QueryEngine {
    shm_path: PathBuf,
    staging: Mutex<Option<Staging>>,
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
        // Cache the inode from the opened handle (fstat), not the path stat above:
        // the file can be replaced between the two, and pairing the old path inode
        // with the new file's mapping would force a spurious remap on the next call.
        let inode = file.metadata()
            .map(|m| m.ino())
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("fstat shm: {e}")))?;
        // Safety: bytes written by rkyv::to_bytes on this platform; file is replaced
        // atomically (rename), never modified in place while mapped.
        let mmap = Arc::new(unsafe { Mmap::map(&file) }
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("mmap: {e}")))?);
        // Reject archives not written by this exact build (stale file from an older
        // build, or a foreign file at the shared path): handing them to
        // access_unchecked would be UB. Callers treat the error as "no archive".
        if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "archive header mismatch at {} (stale or foreign archive; will be rebuilt)",
                self.shm_path.display(),
            )));
        }
        *guard = Some(CachedMmap { mmap: Arc::clone(&mmap), inode });
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
            "/dev/shm/sylvan_librarian_cards"
        } else {
            "/tmp/sylvan_librarian_cards"
        };
        QueryEngine {
            shm_path: PathBuf::from(shm_path.unwrap_or(default_path)),
            staging: Mutex::new(None),
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

    /// Start a staged reload: acquire the cross-process write lock and reset
    /// the staging buffer. Returns false (and refreshes the local mapping) if
    /// another worker published a new archive while we waited for the lock —
    /// the caller should skip fetching entirely. Any staging abandoned by a
    /// previous failed cycle is discarded here.
    fn reload_begin(&self) -> PyResult<bool> {
        let mut staging = self.staging.lock().unwrap();
        // Drop an abandoned cycle's buffer and its flock before re-acquiring.
        *staging = None;

        // Snapshot the archive's identity before contending for the cross-process
        // lock, so we can detect whether another worker published a new archive
        // while we were blocked. Publish is rename-only, so a publish always
        // changes the inode — unlike mtime, which is subject to filesystem
        // timestamp granularity and clock steps.
        let inode_before = std::fs::metadata(&self.shm_path).ok().map(|m| m.ino());

        // Cross-process exclusive lock: only one worker writes per reload cycle.
        // The lock file is separate so it persists across archive replacements.
        // Held until reload_commit()/reload_abort() drops the Staging.
        let lock_path = self.shm_path.with_extension("lock");
        let lock_file = std::fs::OpenOptions::new()
            .write(true).create(true).open(&lock_path)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("open lock: {e}")))?;
        // LOCK_EX blocks until we hold the lock; released automatically on drop.
        loop {
            if unsafe { libc::flock(lock_file.as_raw_fd(), libc::LOCK_EX) } == 0 {
                break;
            }
            let err = std::io::Error::last_os_error();
            if err.kind() != std::io::ErrorKind::Interrupted {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(format!("flock: {err}")));
            }
        }

        // If another worker published a new archive while we were waiting (the
        // inode changed, or a file appeared), skip the rebuild and just remap
        // our local handle.
        let inode_after = std::fs::metadata(&self.shm_path).ok().map(|m| m.ino());
        if inode_after.is_some() && inode_after != inode_before {
            self.get_mmap().map(|_| ())?;
            return Ok(false);
        }

        #[cfg(feature = "alloc-counter")]
        alloc_stats::reset_peak();

        *staging = Some(Staging { rows: Vec::new(), interner: Interner::new(), vocab: VocabInterner::new(), artists: VocabInterner::new(), lock_file });
        Ok(true)
    }

    /// Append one batch of card dicts to the staging buffer.
    fn add_batch(&self, db_rows: &Bound<PyList>) -> PyResult<()> {
        let mut guard = self.staging.lock().unwrap();
        let staging = guard.as_mut().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("add_batch called without reload_begin")
        })?;
        for item in db_rows.iter() {
            if let Ok(d) = item.cast::<PyDict>() {
                staging.rows.push(card_from_pydict(&d, &mut staging.interner, &mut staging.vocab, &mut staging.artists)?);
            }
        }
        Ok(())
    }

    /// Discard an in-progress staged reload, releasing the cross-process lock.
    fn reload_abort(&self) -> PyResult<()> {
        self.staging.lock().unwrap().take();
        Ok(())
    }

    /// Sort, index, serialize, and atomically publish the staged cards, then
    /// release the cross-process lock. Queries keep serving the old archive
    /// until the rename lands.
    fn reload_commit(&self) -> PyResult<()> {
        let staging = self.staging.lock().unwrap().take().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("reload_commit called without reload_begin")
        })?;
        let Staging { mut rows, interner, vocab, artists, lock_file } = staging;

        // The store groups printings by oracle_id, so rows without one would all
        // collapse into a single card. The DB enforces NOT NULL; fail loudly here
        // for any other caller (e.g. hand-built test dicts).
        if let Some((idx, row)) = rows.iter().enumerate().find(|(_, r)| r.oracle_id == 0) {
            let name = interner.strings.get(row.card_name_id as usize).map_or("", |s| s.as_str());
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "card {idx} ({name:?}) is missing oracle_id (required for card grouping)"
            )));
        }
        // Equal oracle ids end up adjacent (making each card's printings one
        // contiguous range), and within a card printings order by descending
        // default prefer_score so the default-prefer walk takes the first
        // matching printing. Score ties fall back to illustration order, then
        // scryfall_id, making the chosen printing fully deterministic (exact
        // ties on the prefer metric are common — reprint sheets share scores —
        // and an unstable sort would otherwise pick arbitrarily among them).
        rows.sort_unstable_by(|a, b| {
            a.oracle_id
                .cmp(&b.oracle_id)
                .then_with(|| {
                    let sa = a.prefer_score.unwrap_or(0.0);
                    let sb = b.prefer_score.unwrap_or(0.0);
                    sb.total_cmp(&sa)
                })
                .then_with(|| a.illustration_id.cmp(&b.illustration_id))
                .then_with(|| a.scryfall_id.cmp(&b.scryfall_id))
        });

        // Group rows into OracleCards + Printings + CSR offsets. Card-level
        // fields come from the group's first row (verified printing-constant on
        // the real corpus; the 3 divergent-name omen cards take the first
        // printing's value). Legality is the exception: a group whose rows
        // disagree gets legality_divergent set, deferring legality filters to
        // each printing's own word.
        let mut cards: Vec<OracleCard> = Vec::new();
        let mut printings: Vec<Printing> = Vec::with_capacity(rows.len());
        let mut offsets: Vec<u32> = Vec::new();
        for mut row in rows {
            let is_new = cards.last().is_none_or(|c| c.oracle_id != row.oracle_id);
            if is_new {
                offsets.push(printings.len() as u32);
                cards.push(OracleCard {
                    card_name_lower: row.card_name_lower,
                    card_colors: row.card_colors,
                    card_color_identity: row.card_color_identity,
                    produced_mana: row.produced_mana,
                    card_types: row.card_types,
                    legality_divergent: false,
                    oracle_id: row.oracle_id,
                    card_name_id: row.card_name_id,
                    oracle_text_id: row.oracle_text_id,
                    oracle_text_lower_id: row.oracle_text_lower_id,
                    card_layout_id: row.card_layout_id,
                    mana_cost_text_id: row.mana_cost_text_id,
                    type_line_id: row.type_line_id,
                    cmc: row.cmc,
                    creature_power: row.creature_power,
                    creature_toughness: row.creature_toughness,
                    planeswalker_loyalty: row.planeswalker_loyalty,
                    edhrec_rank: row.edhrec_rank,
                    cubecobra_score: row.cubecobra_score,
                    card_subtypes: std::mem::take(&mut row.card_subtypes),
                    card_keywords: std::mem::take(&mut row.card_keywords),
                    card_oracle_tags: std::mem::take(&mut row.card_oracle_tags),
                    card_legalities: row.card_legalities,
                    mana_cost: row.mana_cost.clone(),
                    creature_power_text_id: row.creature_power_text_id,
                    creature_toughness_text_id: row.creature_toughness_text_id,
                });
            } else if row.card_legalities != cards.last().map(|c| c.card_legalities).unwrap_or(0) {
                cards.last_mut().unwrap().legality_divergent = true;
            }
            printings.push(Printing {
                scryfall_id: row.scryfall_id,
                illustration_id: row.illustration_id,
                flavor_text_id: row.flavor_text_id,
                flavor_text_lower_id: row.flavor_text_lower_id,
                card_artist_vid: row.card_artist_vid,
                card_set_code: row.card_set_code,
                card_border_id: row.card_border_id,
                card_watermark_id: row.card_watermark_id,
                collector_number_id: row.collector_number_id,
                set_name_id: row.set_name_id,
                released_at_int: row.released_at_int,
                card_rarity_int: row.card_rarity_int,
                collector_number_int: row.collector_number_int,
                price_usd: row.price_usd,
                price_eur: row.price_eur,
                price_tix: row.price_tix,
                prefer_score: row.prefer_score,
                card_legalities: row.card_legalities,
                card_art_tags: row.card_art_tags,
                card_is_tags: row.card_is_tags,
                card_frame_data: row.card_frame_data,
            });
        }
        offsets.push(printings.len() as u32);

        #[cfg(feature = "alloc-counter")]
        let stats_after_cards = (alloc_stats::live(), alloc_stats::allocs());

        let strings = interner.strings;
        drop(interner.map);
        let coll_vocab = vocab.strings;
        drop(vocab.map);
        let artist_vocab = artists.strings;
        drop(artists.map);
        // String-sorted permutation of the vocab ids; VocabInterner caps the
        // vocab at u16::MAX entries so the cast can't truncate.
        let mut coll_vocab_sorted: Vec<u16> = (0..coll_vocab.len() as u16).collect();
        coll_vocab_sorted.sort_unstable_by(|&a, &b| coll_vocab[a as usize].cmp(&coll_vocab[b as usize]));
        let indexes = CardIndexes {
            name_trigram:   build_trigram_index(&cards, |c| c.card_name_lower.as_str()),
            oracle_trigram: build_oracle_text_index(&cards, &strings),
            cmc:            build_numeric_index(&cards, |c| c.cmc.map(|v| v as i16)),
            power:          build_numeric_index(&cards, |c| c.creature_power.map(|v| v as i16)),
            toughness:      build_numeric_index(&cards, |c| c.creature_toughness.map(|v| v as i16)),
            type_bits:      build_type_index(&cards),
            subtypes:       build_tag_index(&cards, &coll_vocab, |c| &c.card_subtypes),
            keywords:       build_tag_index(&cards, &coll_vocab, |c| &c.card_keywords),
            oracle_tags:    build_tag_index(&cards, &coll_vocab, |c| &c.card_oracle_tags),
            art_tags:       build_tag_index(&printings, &coll_vocab, |p| &p.card_art_tags),
            is_tags:        build_tag_index(&printings, &coll_vocab, |p| &p.card_is_tags),
            artists:        build_artist_index(&printings, artist_vocab.len()),
            set_codes:      {
                let mut idx: TagIndex = HashMap::new();
                for (i, p) in printings.iter().enumerate() {
                    let code = p.card_set_code.as_str();
                    if !code.is_empty() {
                        idx.entry(code.to_string()).or_default().push(i as u32);
                    }
                }
                idx
            },
            released_at:    build_date_index(&printings),
        };

        #[cfg(feature = "alloc-counter")]
        let stats_after_indexes = (alloc_stats::live(), alloc_stats::allocs());

        // Snapshot the registry card_from_pydict just populated so reader
        // processes can adopt the same format→shift assignments.
        let format_shifts_snapshot = format_shifts().read().map(|m| m.clone()).unwrap_or_default();
        let card_data = CardData { cards, printings, offsets, strings, coll_vocab, coll_vocab_sorted, artist_vocab, indexes, format_shifts: format_shifts_snapshot };

        // Write atomically: stream into a per-PID .tmp, then rename over shm_path.
        // Per-PID avoids the race where two workers write to the same .tmp and
        // one's rename consumes the file before the other can rename it.
        // Streaming the serialization straight into the file means the archive
        // bytes exist only as file pages — there is no second copy of the
        // archive as a heap buffer, and no realloc-doubling spike while it
        // grows (see docs/issues/engine-reload-publish-transient.md).
        let tmp_name = format!(
            "{}.{}.tmp",
            self.shm_path.file_name().unwrap_or_default().to_string_lossy(),
            std::process::id(),
        );
        let tmp_path = self.shm_path.with_file_name(tmp_name);
        {
            let f = std::fs::File::create(&tmp_path)
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("create tmp: {e}")))?;
            let mut buf = std::io::BufWriter::with_capacity(1 << 20, f);
            buf.write_all(&archive_header())
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("write header: {e}")))?;
            rkyv::api::high::to_bytes_in::<_, rkyv::rancor::Error>(
                &card_data,
                rkyv::ser::writer::IoWriter::new(&mut buf),
            )
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("rkyv serialize: {e}")))?;
            buf.flush()
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("flush tmp: {e}")))?;
        }

        #[cfg(feature = "alloc-counter")]
        {
            // Snapshot the build peak before the component-size diagnostics
            // below re-serialize pieces into heap buffers and inflate it.
            let build_peak = alloc_stats::peak();
            let archive_len = std::fs::metadata(&tmp_path)
                .map(|m| m.len() as usize)
                .unwrap_or(0)
                .saturating_sub(ARCHIVE_HEADER_LEN);
            let component_bytes = (
                rkyv::to_bytes::<rkyv::rancor::Error>(&card_data.cards).map(|b| b.len()).unwrap_or(0)
                    + rkyv::to_bytes::<rkyv::rancor::Error>(&card_data.printings).map(|b| b.len()).unwrap_or(0),
                rkyv::to_bytes::<rkyv::rancor::Error>(&card_data.indexes).map(|b| b.len()).unwrap_or(0),
                rkyv::to_bytes::<rkyv::rancor::Error>(&card_data.strings).map(|b| b.len()).unwrap_or(0),
            );
            alloc_stats::record_reload(stats_after_cards, stats_after_indexes, component_bytes, archive_len, build_peak);
        }

        std::fs::rename(&tmp_path, &self.shm_path)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("rename shm: {e}")))?;

        // The new archive is published; release the cross-process write lock.
        drop(lock_file);

        self.get_mmap().map(|_| ())
    }

    /// One-shot reload: the staged API as a single call. Kept for tests and
    /// for callers that already hold the full corpus in memory.
    fn reload(&self, db_rows: &Bound<PyList>) -> PyResult<()> {
        if !self.reload_begin()? {
            return Ok(()); // another worker just published; we picked up theirs
        }
        if let Err(e) = self.add_batch(db_rows) {
            self.reload_abort()?;
            return Err(e);
        }
        self.reload_commit()
    }

    #[pyo3(signature = (*, filters, unique="card", prefer="default", orderby="edhrec", direction="asc", limit=100, offset=0, fields=None))]
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
        fields: Option<Vec<String>>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let resolved_fields = resolve_fields(fields)?;
        let to_json    = filters.call_method0("to_json")?;
        let json_bytes: Vec<u8> = py
            .import("orjson")?
            .call_method1("dumps", (to_json,))?
            .extract()?;
        let json_str = std::str::from_utf8(&json_bytes)
            .map_err(|e| QueryError::new_err(format!("bad UTF-8 from orjson: {e}")))?;
        let json_val: Value = serde_json::from_str(json_str)
            .map_err(|e| QueryError::new_err(format!("bad query JSON: {e}")))?;
        // get_mmap() remaps automatically if the on-disk inode has changed since
        // the last reload, keeping workers off stale (deleted) mappings.
        let mmap = self.get_mmap()?;
        // Safety: the archive is trusted by construction, so we skip validation.
        // This is the canonical justification for every access_unchecked in this
        // module (query_hashmap() and size() refer here):
        //
        // - The only writer is reload() in this module: the bytes come from
        //   rkyv::to_bytes in the same build of this crate that reads them.
        //   get_mmap() enforces this with the archive header check (magic,
        //   format version, size_of::<ACard>), so an archive left behind by an
        //   older build — e.g. /tmp on macOS dev persisting across rebuilds —
        //   is rejected and rebuilt rather than mapped.
        // - A torn or truncated archive is never observable: reload() writes to
        //   a per-PID temp file and publishes it with rename(2), which is
        //   atomic. A crashed writer leaves a stale .tmp, never a partial file
        //   at shm_path. A missing archive already failed in get_mmap().
        // - The mapping is immutable: replacement is rename-only, the file is
        //   never modified in place, and the Arc keeps the old mapping alive
        //   for in-flight readers across a swap.
        //
        // Checked rkyv::access() re-validates the entire archive graph on every
        // call: measured at ~7 ms per call on a ~120 MB / 96k-card archive
        // (bench_checked_vs_unchecked_access), vs sub-millisecond query
        // evaluation — a 10-100x slowdown per query. It would also be a false
        // guarantee: InlineStr's CheckBytes is deliberately permissive, so
        // validation cannot be the safety boundary; the trusted write path is.
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };

        // Must run before build_filter so legality shifts resolve in workers
        // that never executed the load path themselves.
        sync_format_shifts(&data.format_shifts);
        let mut filter_expr = build_filter(&json_val)
            .map_err(|e| QueryError::new_err(format!("build_filter: {e}")))?;
        filter_expr.bind(&data.coll_vocab, &data.coll_vocab_sorted, &data.artist_vocab);

        let (total, page) = run_query(
            &data.cards, &data.printings, &data.offsets, &data.strings, &filter_expr,
            unique, prefer, orderby, direction, limit, offset, &data.indexes,
        );

        let matches: Vec<Bound<PyDict>> = page
            .iter()
            .map(|(c, p)| card_to_pydict(py, c, p, &data.strings, &data.coll_vocab, &resolved_fields))
            .collect::<PyResult<Vec<_>>>()?;
        let matches_list = PyList::new(py, matches)?;
        PyTuple::new(py, [total.into_pyobject(py)?.into_any(), matches_list.into_any()])
    }

    fn size(&self) -> PyResult<usize> {
        match self.get_mmap() {
            // Missing, unopenable, or wrong-build (header mismatch) archive.
            // Returns 0 so Python treats the engine as empty and rebuilds.
            Err(_) => Ok(0),
            // Safety: see the access_unchecked justification in query(). A file
            // that mapped and passed the header check is always a complete rkyv
            // archive from this build (atomic rename publish), so checked access
            // here would only re-validate trusted bytes at ~7 ms per size() call.
            // Printing count (the pre-split row count), so the Python side's
            // size checks and log lines keep their meaning.
            Ok(mmap) => Ok(unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) }.printings.len()),
        }
    }

    /// Return `n` randomly sampled oracle cards, each shown as its
    /// default-preferred printing — the first in the card's range, since
    /// printings are stored in descending default-prefer order.
    #[pyo3(signature = (n, fields=None))]
    fn sample_preferred<'py>(&self, py: Python<'py>, n: usize, fields: Option<Vec<String>>) -> PyResult<Bound<'py, PyList>> {
        let resolved_fields = resolve_fields(fields)?;
        let mmap = self.get_mmap()?;
        // Safety: see the access_unchecked justification in query().
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };

        let pool_len = data.cards.len();
        let take = n.min(pool_len);

        use rand::RngExt;
        let mut rng: rand::rngs::SmallRng = rand::make_rng();
        let mut chosen = std::collections::HashSet::with_capacity(take);
        while chosen.len() < take {
            chosen.insert(rng.random::<u64>() as usize % pool_len);
        }

        let dicts: Vec<Bound<PyDict>> = chosen.iter()
            .map(|&cid| {
                let card = &data.cards[cid];
                let preferred = u32::from(data.offsets[cid]) as usize;
                card_to_pydict(py, card, &data.printings[preferred], &data.strings, &data.coll_vocab, &resolved_fields)
            })
            .collect::<PyResult<_>>()?;
        PyList::new(py, dicts)
    }


    /// Count type and subtype occurrences across oracle cards.
    /// Returns {type_name: count} covering both supertypes/types (decoded from
    /// the card_types bitmask) and subtypes (from card_subtypes strings).
    fn common_card_types<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let mmap = self.get_mmap()?;
        // Safety: see the access_unchecked justification in query().
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
        let counts = count_common_types(data);
        let d = PyDict::new(py);
        for (name, count) in &counts {
            d.set_item(name, count)?;
        }
        Ok(d)
    }

    /// Count keyword occurrences across oracle cards.
    /// Returns {keyword_name: count} for all keywords present on preferred cards.
    fn common_card_keywords<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let mmap = self.get_mmap()?;
        // Safety: see the access_unchecked justification in query().
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
        let counts = count_common_keywords(data);
        let d = PyDict::new(py);
        for (name, count) in &counts {
            d.set_item(name, count)?;
        }
        Ok(d)
    }

    /// Rust-heap allocator stats and reload() memory breakdown.
    /// Empty dict unless built with --features alloc-counter (measurement-only).
    fn mem_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        #[cfg(feature = "alloc-counter")]
        {
            use std::sync::atomic::Ordering::Relaxed;
            d.set_item("live_bytes", alloc_stats::LIVE.load(Relaxed))?;
            d.set_item("live_allocs", alloc_stats::ALLOCS.load(Relaxed))?;
            d.set_item("reload_live_before", alloc_stats::RELOAD_LIVE_BEFORE.load(Relaxed))?;
            d.set_item("reload_live_after_cards", alloc_stats::RELOAD_LIVE_AFTER_CARDS.load(Relaxed))?;
            d.set_item("reload_allocs_after_cards", alloc_stats::RELOAD_ALLOCS_AFTER_CARDS.load(Relaxed))?;
            d.set_item("reload_live_after_indexes", alloc_stats::RELOAD_LIVE_AFTER_INDEXES.load(Relaxed))?;
            d.set_item("reload_allocs_after_indexes", alloc_stats::RELOAD_ALLOCS_AFTER_INDEXES.load(Relaxed))?;
            d.set_item("reload_peak", alloc_stats::RELOAD_PEAK.load(Relaxed))?;
            d.set_item("cards_rkyv_bytes", alloc_stats::RELOAD_CARDS_RKYV.load(Relaxed))?;
            d.set_item("indexes_rkyv_bytes", alloc_stats::RELOAD_INDEXES_RKYV.load(Relaxed))?;
            d.set_item("strings_rkyv_bytes", alloc_stats::RELOAD_STRINGS_RKYV.load(Relaxed))?;
            d.set_item("archive_bytes", alloc_stats::RELOAD_ARCHIVE.load(Relaxed))?;
        }
        Ok(d)
    }
}

#[pymodule]
mod card_engine {
    use pyo3::prelude::*;

    #[pymodule_export]
    use super::QueryEngine;

    #[pymodule_init]
    fn init(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add("QueryError", m.py().get_type::<super::QueryError>())?;
        m.add("UnknownFieldError", m.py().get_type::<super::UnknownFieldError>())
    }
}

#[cfg(test)]
mod tests;
