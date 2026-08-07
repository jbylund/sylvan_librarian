use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDate, PyDateAccess, PyDict, PyList, PyTuple};
use rkyv::{Archive, Archived, Deserialize, Serialize};
use memmap2::Mmap;
use memchr::memmem;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::Write as IoWrite;
use std::path::PathBuf;
use std::sync::{Arc, LazyLock, Mutex};
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
// a breakdown of reload(): see docs/issues/00504-engine-store-size-reduction.md step 0.

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

/// Card types that can exist as a permanent on the battlefield. Devotion
/// (MTG comprehensive rules) is defined only over permanents' mana costs —
/// confirmed against the real Scryfall API (`devotion:` never matches a pure
/// Instant/Sorcery, e.g. the real Lightning Bolt) — so `mana_cost.devotion` is
/// zeroed at load for any card with no bit in this mask. `TYPE_INSTANT` and
/// `TYPE_SORCERY` are the only nonpermanent primary types; every other bit
/// (BASIC, CONSPIRACY, KINDRED, LEGENDARY, SNOW, WORLD) is a supertype that
/// always co-occurs with a permanent or nonpermanent primary type, never
/// determines it alone.
const PERMANENT_TYPES: u16 = TYPE_ARTIFACT | TYPE_BATTLE | TYPE_CREATURE | TYPE_ENCHANTMENT | TYPE_LAND | TYPE_PLANESWALKER;

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

// ─── Packed pip lanes ────────────────────────────────────────────────────────
// Pip counts pack into a u64 as eight 8-bit lanes (chosen over the jsonb-
// mirroring HashMap it replaces — that shape existed to make the Postgres
// query easy to write, not because the engine needed it). The eight
// single-symbol keys of mana_cost_jsonb (WUBRGC + snow + X; generic numbers
// are dropped by mana_cost_str_to_dict on the card side and mana_pip_counts
// on the query side) each own a lane; the ~29 hybrid '/' symbols overflow to
// a small sorted (vocab id, count) vec that is empty on ~97% of cards.
// Per-lane comparisons are three branchless ops (see lanes_ge), and pip-set
// equality is integer equality — a zero lane and an absent HashMap key are
// the same thing, which is what makes `mana=`'s distinct-key semantics fall
// out for free. Lane counts saturate at 127 so the borrow trick stays sound
// (real costs peak around 16 pips).

pub(crate) const MANA_LANE_SYMS: [&str; 8] = ["W", "U", "B", "R", "G", "C", "S", "X"];
/// High bit of each of the 8 core-pip lanes / the 6 devotion lanes.
pub(crate) const LANES8_HI: u64 = 0x8080_8080_8080_8080;
pub(crate) const LANES6_HI: u64 = 0x0000_8080_8080_8080;
const LANE_MAX: u8 = 0x7f;

pub(crate) fn mana_lane(sym: &str) -> Option<usize> {
    MANA_LANE_SYMS.iter().position(|s| *s == sym)
}

pub(crate) fn lane_get(packed: u64, lane: usize) -> u8 {
    (packed >> (8 * lane)) as u8
}

/// Add `n` to a lane, saturating at LANE_MAX so lanes can never borrow into
/// their neighbor and the SWAR compares stay per-lane exact.
pub(crate) fn lane_add(packed: u64, lane: usize, n: u8) -> u64 {
    let cur = lane_get(packed, lane);
    let new = cur.saturating_add(n).min(LANE_MAX);
    (packed & !(0xffu64 << (8 * lane))) | ((new as u64) << (8 * lane))
}

/// Per-lane a >= b across every lane of `hi` (the SWAR borrow trick): setting
/// each lane's high bit in `a` guarantees the per-lane subtraction cannot
/// borrow out of the lane, and the high bit survives exactly when that lane's
/// a >= b. Sound because lane values are saturated below 0x80.
pub(crate) fn lanes_ge(a: u64, b: u64, hi: u64) -> bool {
    ((a | hi).wrapping_sub(b)) & hi == hi
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
                // X is a real pip symbol (its own lane, see MANA_LANE_SYMS) —
                // only its cmc contribution is 0, handled separately by
                // mana_cmc. Confirmed against the real Scryfall API:
                // mana:{X} matches Fireball ({X}{R}) and excludes cards with
                // no X pip, which this exclusion broke.
                if in_brace && sym.parse::<u32>().is_err() {
                    *pips.entry(sym.clone()).or_insert(0) += 1;
                }
                in_brace = false;
            }
            _ if in_brace => sym.push(c),
            // Bare (unbraced) X is a real pip symbol too — confirmed against
            // the real Scryfall API: mana:x behaves identically to mana:{x}.
            _ if "WUBRGCX".contains(c) => { *pips.entry(c.to_string()).or_insert(0) += 1; }
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
// Design: docs/issues/00603-engine-card-printing-split.md / issue #603.

#[derive(Archive, Serialize, Deserialize, Clone)]
struct ManaCost {
    /// Single-symbol pip counts (WUBRGC/S/X) packed into 8-bit lanes — see
    /// the packed-pip-lanes section. Together with `hybrids` this is the
    /// faithful multiset of mana_cost_jsonb's keys, used for mana= queries.
    core: u64,
    /// Hybrid '/' pips as (mana_vocab id, count), sorted by id; empty on
    /// ~97% of cards. Any future non-hybrid symbol Scryfall invents lands
    /// here too — the vocab interns whatever the data contains.
    hybrids: Vec<(u8, u8)>,
    /// WUBRGC devotion counts (hybrids expanded) in the low six lanes,
    /// always materialized; used for devotion queries.
    devotion: u64,
    cmc: f32,
}

#[derive(Archive, Serialize, Deserialize)]
struct OracleCard {
    // Hot fields first — fits in the first cache lines for fast filter short-circuiting.
    card_name_lower: InlineStr<61>, // 61 bytes covers every card name in the Scryfall dataset
    // Accent-folded card_name_lower (e.g. "éowyn" -> "eowyn"), precomputed in Python via
    // fold_accents() (#649). Backs fuzzy name: search (name_trigram/name_bigrams/TextContains)
    // so "eowyn" matches "Éowyn"; exact-match paths deliberately keep using card_name_lower.
    card_name_folded: InlineStr<61>,
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
    // Dense rank of card_name_lower in byte order (equal names share a rank so
    // sort secondaries break their ties). Assigned post-load by
    // assign_name_ranks; the sort key for SortCol::Name. Ranks stay below 2^24
    // so the f32 sort-key conversion is exact.
    name_rank: u32,

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
    // Integer cents, not f32 dollars: every real price is exactly cent-precise (checked against
    // the corpus, 0 of 81,540 priced printings differ from their rounded-to-cent value by more
    // than 0.001), and storing the lossy f32 approximation instead of the exact integer caused
    // two real bugs (see docs/issues/local-engine-broad-range-fastpath.md) — a narrowing false
    // negative from price_bounds' own cents conversion, and a verification false negative from
    // comparing a widened-then-lossy field value against a full-precision query constant.
    price_usd: Option<u32>,
    price_eur: Option<u32>,
    price_tix: Option<u32>,
    prefer_score: Option<f32>,

    // This printing's exact legality word; only consulted when the owning
    // card's legality_divergent flag is set.
    card_legalities: u64,

    card_art_tags: Vec<u16>,
    card_is_tags: Vec<u16>,
    card_frame_data: Vec<u16>,

    // Dense id of this printing's illustration within its own card's printing
    // range: 0 = first-seen illustration (stored order — descending prefer_score),
    // 1 = next, shared artwork shares the id. Assigned by assign_artwork_groups;
    // #629's replacement for comparing/deduping on the full illustration_id UUID
    // in the artwork-mode match-count and emission hot paths.
    artwork_group_id: u16,
}

/// Parse-time row: one DB row (= one printing) with every field, before the
/// commit pass groups rows by oracle_id and splits them into OracleCard +
/// Printing. Never archived.
struct CardRow {
    card_name_lower: InlineStr<61>,
    card_name_folded: InlineStr<61>,
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
    price_usd: Option<u32>, // integer cents -- see Printing's field for why
    price_eur: Option<u32>,
    price_tix: Option<u32>,
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

/// Build-time interner for hybrid mana symbols; `strings` becomes
/// CardData.mana_vocab. The real-data universe is ~29 hybrid symbols, so u8
/// ids leave ample headroom; id 255 is reserved for query symbols absent
/// from the vocab (see MANA_SYM_UNKNOWN), hence the 254 cap.
struct ManaVocabInterner {
    map: HashMap<String, u8>,
    strings: Vec<String>,
}

impl ManaVocabInterner {
    fn new() -> Self {
        ManaVocabInterner { map: HashMap::new(), strings: Vec::new() }
    }

    fn intern(&mut self, s: &str) -> PyResult<u8> {
        if let Some(&id) = self.map.get(s) {
            return Ok(id);
        }
        if self.strings.len() >= 255 {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "mana symbol vocabulary exceeded 254 distinct values; widen ManaCost hybrid ids",
            ));
        }
        let id = self.strings.len() as u8;
        self.strings.push(s.to_string());
        self.map.insert(s.to_string(), id);
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

/// Parse a dollar-denominated price field into integer cents. Round rather than truncate --
/// the source value is a decimal price (from Scryfall's JSON via Python's json/psycopg, both
/// already correctly-rounded f64), so rounding to the nearest cent recovers the exact intended
/// value even if the f64 isn't bit-exact for the decimal (see Printing's price_usd doc comment).
fn opt_price_cents(d: &Bound<PyDict>, key: &str) -> Option<u32> {
    d.get_item(key).ok().flatten().and_then(|v| {
        v.extract::<f64>().ok().or_else(|| v.extract::<i64>().ok().map(|n| n as f64))
    }).map(|dollars| (dollars * 100.0).round() as u32)
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

fn mana_cost_from_pydict(d: &Bound<PyDict>, cmc_val: Option<f32>, mana_vocab: &mut ManaVocabInterner, card_types: u16) -> PyResult<ManaCost> {
    let mut core = 0u64;
    let mut devotion = 0u64;
    let mut hybrids: Vec<(u8, u8)> = Vec::new();
    if let Some(m) = d.get_item("mana_cost_jsonb").ok().flatten().and_then(|v| v.cast_into::<PyDict>().ok()) {
        for (k, v) in m.iter() {
            let Ok(sym) = k.extract::<String>() else { continue };
            let count = v.cast::<PyList>().ok().map(|l| l.len().min(127) as u8).unwrap_or(0);
            match mana_lane(&sym) {
                Some(lane) => {
                    core = lane_add(core, lane, count);
                    if lane < 6 {
                        devotion = lane_add(devotion, lane, count);
                    }
                }
                None => {
                    hybrids.push((mana_vocab.intern(&sym)?, count));
                    for part in sym.split('/') {
                        // WUBRGC: SQL's calculate_devotion counts C too ({C/W} hybrids)
                        if let Some(lane) = mana_lane(part).filter(|&l| l < 6) {
                            devotion = lane_add(devotion, lane, count);
                        }
                    }
                }
            }
        }
    }
    hybrids.sort_unstable();
    // Nonpermanents (Instant/Sorcery) never contribute devotion, regardless of
    // their mana cost — see PERMANENT_TYPES.
    if card_types & PERMANENT_TYPES == 0 {
        devotion = 0;
    }
    Ok(ManaCost { core, hybrids, devotion, cmc: cmc_val.unwrap_or(0.0) })
}

fn card_from_pydict(d: &Bound<PyDict>, it: &mut Interner, vocab: &mut VocabInterner, artists: &mut VocabInterner, mana: &mut ManaVocabInterner) -> PyResult<CardRow> {
    let released_at = opt_date_str(d, "released_at").unwrap_or_default();
    let released_at_int: Option<u32> = released_at.replace('-', "").parse().ok();
    // Raw strings from the dict; interned to ids as the struct is built below.
    let card_name = opt_str(d, "card_name").unwrap_or_default();
    let card_name_lower = InlineStr::<61>::from_str(&card_name.to_lowercase());
    // Already lowercased + accent-folded in Python (fold_accents(), #649); read as-is.
    let card_name_folded = InlineStr::<61>::from_str(&opt_str(d, "card_name_folded").unwrap_or_default());
    let oracle_text = opt_str(d, "oracle_text").unwrap_or_default();
    let oracle_text_lower_id = it.intern(oracle_text.to_lowercase());
    let flavor_text = opt_str(d, "flavor_text").unwrap_or_default();
    let flavor_text_lower_id = it.intern(flavor_text.to_lowercase());
    let card_artist_vid = match opt_str(d, "card_artist") {
        Some(a) => artists.intern(a.to_lowercase())?,
        None => ARTIST_NONE,
    };
    let card_types = card_types_list_to_bits(&str_list(d, "card_types"));

    Ok(CardRow {
        scryfall_id: opt_uuid(d, "scryfall_id"),
        oracle_id: opt_uuid(d, "oracle_id"),
        illustration_id: opt_uuid(d, "illustration_id"),

        card_name_lower,
        card_name_folded,
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
        price_usd: opt_price_cents(d, "price_usd"),
        price_eur: opt_price_cents(d, "price_eur"),
        price_tix: opt_price_cents(d, "price_tix"),
        prefer_score: opt_f32(d, "prefer_score"),
        cubecobra_score: opt_f32(d, "cubecobra_score"),

        card_types,
        card_subtypes: str_list_to_ids(d, "card_subtypes", vocab)?,
        card_keywords: jsonb_obj_to_ids(d, "card_keywords", vocab)?,
        card_legalities: jsonb_obj_to_legality_bits(d, "card_legalities"),
        card_oracle_tags: jsonb_obj_to_ids(d, "card_oracle_tags", vocab)?,
        card_art_tags: jsonb_obj_to_ids(d, "card_art_tags", vocab)?,
        card_is_tags: jsonb_obj_to_ids(d, "card_is_tags", vocab)?,
        card_frame_data: jsonb_obj_to_ids(d, "card_frame_data", vocab)?,

        mana_cost: mana_cost_from_pydict(d, opt_f32(d, "cmc"), mana, card_types)?,

        creature_power_text_id: it.intern_opt(opt_str(d, "creature_power_text")),
        creature_toughness_text_id: it.intern_opt(opt_str(d, "creature_toughness_text")),
    })
}

// ─── Filter expression & builder ─────────────────────────────────────────────

mod filter;
use filter::*;
mod planes;
use planes::*;
mod estimator;
mod cost;

// ─── Trigram index ────────────────────────────────────────────────────────────

/// Two-tier trigram → posting-list index, generic over the id domain it posts
/// (card ids for `name_trigram`, dense oracle-text ids for
/// `OracleTextIndex.trigrams`) — `domain` records which, both for the
/// dense-plane word count and as a build/read compatibility check.
///
/// Same #639 crossover this reuses everywhere else: past `words_per_plane(domain)*8`
/// bytes a plane is smaller *and* faster to probe than a posting list, so build
/// time buckets each trigram into whichever tier it's cheaper in — never both,
/// no discriminant per entry (see `NameBigramIndex` for the same split with a
/// worked rationale). Keys are sorted ascending within each tier so query time
/// binary-searches instead of hashing; this is also what makes the structure
/// zero-copy archivable with rkyv, unlike the `HashMap` it replaces.
#[derive(Archive, Serialize, Deserialize, Default)]
struct SortedTrigramIndex {
    /// Card id (name index) or dense text id (oracle index) count the
    /// postings/planes below range over.
    domain: u32,
    /// Sorted ascending; parallel to `dense_bits` (each entry is
    /// `words_per_plane(domain)` words) and `dense_counts`.
    dense_keys: Vec<[u8; 3]>,
    /// Match count per dense entry, parallel to `dense_keys` — avoids a
    /// popcount just to answer trigram_min_posting's size query.
    dense_counts: Vec<u32>,
    dense_bits: Vec<u64>,
    /// Sorted ascending; CSR row `sparse_postings[sparse_offsets[i]..sparse_offsets[i+1]]`.
    sparse_keys: Vec<[u8; 3]>,
    sparse_offsets: Vec<u32>,
    /// u16: both domains (card ids, dense text ids) fit comfortably at this
    /// corpus size — half the bytes of a u32 posting. `finalize_trigram_index`
    /// forces every entry dense if `domain` ever doesn't fit, so this never
    /// silently truncates.
    sparse_postings: Vec<u16>,
}

/// Bucket a trigram→postings map into `SortedTrigramIndex`'s two tiers.
/// `domain` is the id space the postings range over (card count for the name
/// index, distinct-text count for the oracle index) — both the crossover math
/// and the u16-fits check key off it.
fn finalize_trigram_index(map: HashMap<[u8; 3], Vec<u32>>, domain: usize) -> SortedTrigramIndex {
    let wpp = words_per_plane(domain);
    let plane_bytes = wpp * 8;
    let u16_ok = domain <= u16::MAX as usize + 1;
    let mut entries: Vec<([u8; 3], Vec<u32>)> = map.into_iter().collect();
    entries.sort_unstable_by_key(|(k, _)| *k);

    let mut idx = SortedTrigramIndex { domain: domain as u32, ..Default::default() };
    idx.sparse_offsets.push(0);
    for (key, ids) in entries {
        if u16_ok && ids.len() * 2 <= plane_bytes {
            idx.sparse_keys.push(key);
            idx.sparse_postings.extend(ids.iter().map(|&i| i as u16));
            idx.sparse_offsets.push(idx.sparse_postings.len() as u32);
        } else {
            idx.dense_keys.push(key);
            idx.dense_counts.push(ids.len() as u32);
            let base = idx.dense_bits.len();
            idx.dense_bits.resize(base + wpp, 0);
            for id in ids {
                idx.dense_bits[base + (id as usize >> 6)] |= 1u64 << (id & 63);
            }
        }
    }
    idx
}

/// Word dictionary + inverted index over distinct oracle texts, for needles
/// longer than 3 characters that are a single tokenized fragment (no
/// whitespace/punctuation) — see docs/issues/00663-engine-oracle-word-index.md.
/// Tokenization boundaries are exactly the characters absent from such a
/// needle, so any occurrence of the needle lies entirely inside one
/// tokenized word: scanning the dictionary for words containing it and
/// unioning their postings is the exact match set, no verification pass.
///
/// Needles of length <= 3 don't need an entry here at all: a 3-character
/// needle IS a trigram, and the existing trigram index's posting list is
/// already the exact answer for it (no intersection, no ambiguity) — see the
/// design doc's "3-character case is already solved" section. So this
/// dictionary only holds words longer than 3 characters.
///
/// Two tiers, split by #639's crossover (reused with domain = n_texts, the
/// same distinct-text count `SortedTrigramIndex`'s oracle instance uses):
/// - `sparse_*`: below the crossover, postings are ascending dense *text*
///   ids (like the trigram index) — expanded to cards via the shared CSR at
///   query time.
/// - `dense_*`: at/above the crossover, stored as **card-space** bitmaps,
///   already expanded through the CSR at build time. This is deliberately a
///   different domain than the sparse tier: the dense tier exists so
///   `compile_plane` can AND it directly against other card planes with zero
///   further expansion, and the query-time answer is card space either way,
///   so there's no reason to also materialize a text-id-space bitmap only to
///   immediately re-expand it.
#[derive(Archive, Serialize, Deserialize, Default)]
struct OracleWordIndex {
    /// Card count the dense tier's bitmaps are sized to — a build/read
    /// compatibility check, same convention as `NameBigramIndex.n_cards`.
    n_cards: u32,
    /// Sorted ascending (for determinism — query-time lookup goes through
    /// `sparse_blob` below, not this list directly; a word containing the
    /// needle can land anywhere lexicographically, so it isn't
    /// binary-searchable on its own).
    sparse_words: Vec<String>,
    /// CSR row boundaries into `sparse_postings`, length sparse_words.len()+1.
    sparse_offsets: Vec<u32>,
    /// Ascending dense text ids per row. u16: n_texts fits comfortably at
    /// this corpus size (build forces every word dense if it doesn't).
    sparse_postings: Vec<u16>,
    /// `sparse_words` concatenated in order, each preceded by a `\0` byte —
    /// a byte no tokenized word or eligible query needle ever contains (see
    /// `oracle_word_eligible`), so a needle match can never straddle two
    /// words. Query time scans this ONE buffer with `memchr::memmem`
    /// instead of calling `.contains()` once per dictionary word: calling
    /// `.contains()` ~6,300 times (once per sparse word, measured against
    /// the real corpus) redoes substring-search setup on every call — the
    /// actual bottleneck the naive per-word loop pays — where concatenating
    /// and scanning once amortizes that setup, and memmem's SIMD scan beats
    /// std's `match_indices` by 5-6x on this same blob for real dictionary
    /// sizes (bench_word_dict_scan.rs) — the reverse of the per-card-haystack
    /// finding in bench_text_search.rs, because this is one long contiguous
    /// scan rather than many short separate ones. `sparse_word_starts` maps
    /// a match's byte offset back to a word index by binary search.
    sparse_blob: String,
    /// Byte offset of `sparse_words[i]`'s leading `\0` in `sparse_blob`,
    /// ascending, length sparse_words.len(). A match at position p belongs
    /// to word `partition_point(|&s| s <= p) - 1`.
    sparse_word_starts: Vec<u32>,
    /// Sorted ascending, parallel to a `dense_bits` slice of
    /// `words_per_plane(n_cards)` words each. Not blobbed: at ~56 entries
    /// (per the design doc's corpus measurement) a plain loop is already far
    /// cheaper than the sparse tier's scan ever was.
    dense_words: Vec<String>,
    dense_bits: Vec<u64>,
}

/// Byte that never appears in a tokenized dictionary word or an eligible
/// query needle (see `oracle_word_eligible`'s `[a-z0-9']` charset) — safe as
/// a `sparse_blob` word separator with no escaping needed.
const WORD_BLOB_DELIM: u8 = 0;

/// True for needles the word index can answer exactly: longer than 3 bytes
/// (see `OracleWordIndex`'s doc) and composed only of tokenizer word bytes
/// (`[a-z0-9']`) — i.e. a single fragment that can't itself straddle a
/// tokenization boundary. Multi-word phrases and anything shorter falls
/// through to the trigram path unchanged.
fn oracle_word_eligible(word: &str) -> bool {
    word.len() > 3 && word.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'\'')
}

/// Which dictionary words (by index into their tier) contain `needle` as a
/// substring — the whole query-time cost of the word index.
pub(crate) struct OracleWordScan {
    pub(crate) dense: Vec<u32>,
    pub(crate) sparse: Vec<u32>,
}

pub(crate) fn scan_oracle_words(idx: &Archived<OracleWordIndex>, needle: &str) -> OracleWordScan {
    // Dense tier is tiny (~56 entries in production): a plain per-word loop
    // costs nothing next to the sparse tier's scan below.
    let dense = idx.dense_words.iter().enumerate().filter(|(_, w)| w.as_str().contains(needle)).map(|(i, _)| i as u32).collect();

    // Sparse tier: one memchr::memmem pass over the whole concatenated blob
    // instead of ~6,300 separate `.contains()` calls — see `sparse_blob`'s
    // doc. memmem measured 5-6x faster here than std `match_indices`
    // (bench_word_dict_scan.rs, real dictionary blob) — the reverse of
    // bench_text_search.rs's earlier finding, because this is one long
    // contiguous scan rather than many separate short-haystack calls (where
    // memmem's setup cost dominated instead). Matches never straddle a word
    // (the delimiter can't appear in `needle`), so each hit maps to exactly
    // one word via a binary search on its start offset; consecutive hits
    // within the same word (a needle can occur more than once in one word)
    // collapse to a single push.
    let mut sparse: Vec<u32> = Vec::new();
    let blob = idx.sparse_blob.as_str().as_bytes();
    for pos in memmem::find_iter(blob, needle.as_bytes()) {
        let word_idx = (idx.sparse_word_starts.partition_point(|s| (u32::from(*s) as usize) <= pos) - 1) as u32;
        if sparse.last() != Some(&word_idx) {
            sparse.push(word_idx);
        }
    }
    OracleWordScan { dense, sparse }
}

// ─── CSR tables ──────────────────────────────────────────────────────────────
// Three indexes store an array-of-arrays as a CSR (compressed sparse row) pair —
// oracle text → cards, artist → printings, flavor text → printings — flattened
// into `offsets` (row boundaries, length n_rows + 1) plus a payload vec so each
// archives as two contiguous zero-copy slices. Build and expand used to be
// written out once per index; the convention lives here instead.

/// The ascending union of the `payload` rows named by `rows`. Row ids are `u16` or `u32`
/// depending on the index, so callers widen at the call site — `u32` deliberately has no
/// `Into<usize>` (usize may be 16-bit) and a trait to hide three `as usize` casts costs more
/// than it saves. `domain` is the id space the payload indexes (cards for the oracle-text
/// table, printings for artist and flavor), which is NOT `payload.len()`: the flavor table
/// omits printings without flavor text.
///
/// Each row is internally sorted (placement below walks store order), but rows are not
/// ordered relative to each other (dense ids are first-seen order), so the union has to be
/// ordered somehow — required both by `intersect_sorted` when And-combining with other
/// candidate sets and by the query driver, which assumes candidates arrive in store order.
/// `sorted_ids` picks the cheaper of the two ways; the rows are walked twice, once to size
/// the answer for that choice and once to emit it, which costs one pass over `offsets`.
///
/// A row is one dense text/artist/flavor id's members, and every card or printing carries
/// exactly one such id, so the rows are disjoint and `sorted_ids`'s duplicate-free
/// precondition holds by construction.
fn expand_csr(offsets: &AOffsets, payload: &AOffsets, rows: impl IntoIterator<Item = usize> + Clone, domain: usize) -> Vec<u32> {
    let span = |row: usize| u32::from(offsets[row]) as usize..u32::from(offsets[row + 1]) as usize;
    let k: usize = rows.clone().into_iter().map(|row| span(row).len()).sum();
    sorted_ids(rows.into_iter().flat_map(|row| payload[span(row)].iter().map(|x| u32::from(*x))), k, domain)
}

/// Count → prefix-sum → place with a cursor. `row_of(i)` gives item `i`'s row, or
/// `None` to omit it from the table entirely (an absent artist, a printing with no
/// flavor text). Returns `(offsets, payload)`, the payload holding every included
/// item index grouped by row and ascending within it.
///
/// `row_of` is called twice per item, once to count and once to place, so it should be
/// a field read rather than a hash probe — every caller runs on the reload path over
/// ~50k items, and one that has to look its row up materializes the rows first.
fn build_csr(n_rows: usize, n_items: usize, row_of: impl Fn(usize) -> Option<usize>) -> (Vec<u32>, Vec<u32>) {
    let mut offsets = vec![0u32; n_rows + 1];
    for i in 0..n_items {
        if let Some(row) = row_of(i) {
            debug_assert!(row < n_rows, "build_csr: row_of({i}) = {row}, out of range for {n_rows} rows");
            offsets[row + 1] += 1;
        }
    }
    for i in 1..offsets.len() {
        offsets[i] += offsets[i - 1];
    }
    let mut cursor = offsets.clone();
    let mut payload = vec![0u32; offsets[n_rows] as usize];
    for i in 0..n_items {
        if let Some(row) = row_of(i) {
            payload[cursor[row] as usize] = i as u32;
            cursor[row] += 1;
        }
    }
    (offsets, payload)
}

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
    trigrams: SortedTrigramIndex,
    /// Dense text id → global string id (CardData.strings) of the distinct
    /// lowercase oracle text, in first-seen card order — same shape as
    /// FlavorIndex.gids. Length n_texts.
    gids: Vec<u32>,
    /// Row boundaries: cards of text id `t` live at
    /// `card_indices[offsets[t] .. offsets[t + 1]]`. Length n_texts + 1.
    offsets: Vec<u32>,
    /// All card indices, grouped by text id; every card appears exactly once
    /// (its text interned to exactly one id), so expansion can never duplicate.
    card_indices: Vec<u32>,
    /// Word dictionary + inverted index, built in the same pass as the
    /// trigrams above (docs/issues/00663-engine-oracle-word-index.md).
    words: OracleWordIndex,
}

/// Emit each maximal run of `[a-z0-9']` bytes at least 4 long in `text`.
/// Byte-indexed slicing is safe here: every boundary sits on an ASCII byte
/// (word bytes are all < 0x80, and any non-word byte — including every
/// continuation/lead byte of a multi-byte UTF-8 sequence, all >= 0x80 —
/// immediately ends the run), so slice bounds always land on char boundaries.
fn tokenize_words_ge4(text: &str, mut emit: impl FnMut(&str)) {
    let is_word_byte = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'\'';
    let bytes = text.as_bytes();
    let mut start: Option<usize> = None;
    for (i, &b) in bytes.iter().enumerate() {
        if is_word_byte(b) {
            start.get_or_insert(i);
        } else if let Some(s) = start.take()
            && i - s >= 4
        {
            emit(&text[s..i]);
        }
    }
    if let Some(s) = start
        && bytes.len() - s >= 4
    {
        emit(&text[s..]);
    }
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

    // Trigram postings and the word dictionary's postings, over distinct texts
    // only, in the same window-sliding/tokenizing pass per text (one pass
    // instead of one for each of trigrams/words). Visiting texts in ascending
    // dense-id order appends ids in ascending order for both, giving sorted
    // posting lists with no per-list sort needed.
    let mut trigrams: HashMap<[u8; 3], Vec<u32>> = HashMap::new();
    let mut words: HashMap<String, Vec<u32>> = HashMap::new();
    for (d, &global) in global_of_dense.iter().enumerate() {
        let text = strings[global as usize].as_str();
        let bytes = text.as_bytes();
        if bytes.len() >= 3 {
            for w in bytes.windows(3) {
                let list = trigrams.entry([w[0], w[1], w[2]]).or_default();
                if list.last() != Some(&(d as u32)) {
                    list.push(d as u32);
                }
            }
        }
        tokenize_words_ge4(text, |word| {
            let list = words.entry(word.to_string()).or_default();
            if list.last() != Some(&(d as u32)) {
                list.push(d as u32);
            }
        });
    }

    // CSR expansion table via counting sort: count cards per text, prefix-sum
    // the counts into row offsets, then place each card index in its row. Placement
    // walks cards in store order, so every row comes out sorted by card index.
    // Every card has a text id, so no row is ever omitted and `card_indices` ends up
    // exactly `cards.len()` long.
    let (offsets, card_indices) = build_csr(n_texts, text_id_of_card.len(), |i| Some(text_id_of_card[i] as usize));

    // Word dictionary split: #639's crossover, reused verbatim with domain =
    // n_texts (matching SortedTrigramIndex's oracle instance) to decide
    // sparse-vs-dense, but a promoted word's *stored* bitmap is expanded
    // through the CSR just built above to card space — see OracleWordIndex's
    // doc for why.
    let n_cards = cards.len();
    let wpp_cards = words_per_plane(n_cards);
    let wpp_texts = words_per_plane(n_texts);
    let text_u16_ok = n_texts <= u16::MAX as usize + 1;
    let mut word_entries: Vec<(String, Vec<u32>)> = words.into_iter().collect();
    word_entries.sort_unstable_by(|a, b| a.0.cmp(&b.0));

    let mut oracle_words = OracleWordIndex { n_cards: n_cards as u32, ..Default::default() };
    oracle_words.sparse_offsets.push(0);
    for (word, text_ids) in word_entries {
        if text_u16_ok && text_ids.len() * 2 <= wpp_texts * 8 {
            oracle_words.sparse_word_starts.push(oracle_words.sparse_blob.len() as u32);
            oracle_words.sparse_blob.push(WORD_BLOB_DELIM as char);
            oracle_words.sparse_blob.push_str(&word);
            oracle_words.sparse_words.push(word);
            oracle_words.sparse_postings.extend(text_ids.iter().map(|&t| t as u16));
            oracle_words.sparse_offsets.push(oracle_words.sparse_postings.len() as u32);
        } else {
            let base = oracle_words.dense_bits.len();
            oracle_words.dense_bits.resize(base + wpp_cards, 0);
            for t in text_ids {
                let start = offsets[t as usize] as usize;
                let end = offsets[t as usize + 1] as usize;
                for &cid in &card_indices[start..end] {
                    oracle_words.dense_bits[base + (cid as usize >> 6)] |= 1u64 << (cid & 63);
                }
            }
            oracle_words.dense_words.push(word);
        }
    }

    OracleTextIndex {
        trigrams: finalize_trigram_index(trigrams, n_texts),
        gids: global_of_dense,
        offsets,
        card_indices,
        words: oracle_words,
    }
}

/// Expand surviving dense text ids to card indices via the CSR table.
fn expand_text_ids(idx: &Archived<OracleTextIndex>, text_ids: &[u32], n_cards: usize) -> Vec<u32> {
    expand_csr(&idx.offsets, &idx.card_indices, text_ids.iter().map(|&t| t as usize), n_cards)
}

// ─── Name bigram index (#639 short-name narrowing) ──────────────────────────
// Trigram narrowing needs a 3-byte needle, so 2-character name searches (the
// typeahead shape: name:fi, name:dr) full-scanned with per-card substring
// searches. For a 2-byte needle, containment IS bigram membership, so a
// bigram index is not a prefilter but the exact answer — sets enter the
// candidate algebra tight, with no verification pass to pay.
//
// Two-tier storage, split at the derived crossover where a card bitplane
// (n_cards/8 bytes, flat) undercuts a u16 posting list (2 bytes/entry):
// ~2k entries at 31.5k cards, 6.3% density. 74 of 951 corpus bigrams sit
// above it, carrying 53% of all posting entries — promoting them saves ~22%
// of the index and hands the #636 algebra pre-built bitmaps for exactly the
// bigrams broad enough to want them. This is #630 phase 3's density-promotion
// rule with the threshold derived from a storage identity instead of tuned.

#[derive(Archive, Serialize, Deserialize, Default)]
struct NameBigramIndex {
    /// Sparse tier: bigram → ascending card ids. u16 on purpose (cards fit;
    /// see build); half the bytes of the u32 posting convention.
    postings: HashMap<[u8; 2], Vec<u16>>,
    /// Dense tier: bigram → plane index into `plane_words`.
    plane_of: HashMap<[u8; 2], u32>,
    /// plane_of.len() × words_per_plane(n_cards), flattened plane-major —
    /// the BitPlanes layout.
    plane_words: Vec<u64>,
    n_cards: u32,
}

fn build_name_bigram_index(cards: &[OracleCard]) -> NameBigramIndex {
    let mut lists: HashMap<[u8; 2], Vec<u32>> = HashMap::new();
    for (i, card) in cards.iter().enumerate() {
        // Folded (#649) — this index backs the same fuzzy name: path as name_trigram.
        let bytes = card.card_name_folded.as_str().as_bytes();
        let mut seen: Vec<[u8; 2]> = Vec::new(); // names are short; a vec beats a set
        for w in bytes.windows(2) {
            let bg = [w[0], w[1]];
            if !seen.contains(&bg) {
                seen.push(bg);
                lists.entry(bg).or_default().push(i as u32);
            }
        }
    }
    let wpp = cards.len().div_ceil(64);
    let plane_bytes = wpp * 8;
    let mut idx = NameBigramIndex { n_cards: cards.len() as u32, ..Default::default() };
    // u16 ids require the card count to fit; past that every bigram promotes
    // (a plane is valid at any count). Production is ~31.5k cards.
    let u16_ok = cards.len() <= u16::MAX as usize + 1;
    for (bg, ids) in lists {
        if u16_ok && ids.len() * 2 <= plane_bytes {
            idx.postings.insert(bg, ids.into_iter().map(|c| c as u16).collect());
        } else {
            let plane = idx.plane_of.len() as u32;
            idx.plane_of.insert(bg, plane);
            idx.plane_words.resize((plane as usize + 1) * wpp, 0);
            for c in ids {
                idx.plane_words[plane as usize * wpp + (c >> 6) as usize] |= 1u64 << (c & 63);
            }
        }
    }
    idx
}

/// Exact 1-byte name containment — the tier below `NameBigramIndex`, and the reason it exists is
/// traffic rather than symmetry.
///
/// A bare query term parses to `card_name` contains (`parse_scryfall_query("s")`), and the UI searches
/// on every keystroke behind a 50 ms debounce, so a ONE-CHARACTER name needle is the first query of
/// every search session a user makes. It was also the only text tier with no index at all: three bytes
/// narrow through `name_trigram`, two resolve exactly through `name_bigrams`, and one fell through to a
/// full residual scan over every card — 446 µs against `name:so`'s 8 µs, a 54x cliff at the one length
/// every search passes through.
///
/// Containment IS byte membership for a 1-byte needle, so the tier lookup is the complete answer and
/// the narrowing is TIGHT — the same argument `name_bigrams` makes one length up.
///
/// Two tiers, not three: the corpus has 51 distinct bytes in folded names, and the cheaper-of-two split
/// lands at 25 planes / 26 posting lists for **109 KB**. A complement tier (store the cards LACKING the
/// byte) was measured and never wins here — the most common byte is `' '` at 92%, whose 8% complement is
/// 2,520 ids = 5 KB against a 3,939 B plane. It would win on `oracle_text`, where 19 bytes exceed 75%,
/// which is one reason this is names-only for now.
#[derive(Archive, Serialize, Deserialize, Default)]
struct NameUnigramIndex {
    /// Sparse tier: byte → ascending card ids, u16 for the same reason as the bigram index.
    postings: HashMap<u8, Vec<u16>>,
    /// Dense tier: byte → plane index into `plane_words`.
    plane_of: HashMap<u8, u32>,
    /// `plane_of.len()` × `words_per_plane(n_cards)`, flattened plane-major.
    plane_words: Vec<u64>,
    n_cards: u32,
}

fn build_name_unigram_index(cards: &[OracleCard]) -> NameUnigramIndex {
    let mut lists: HashMap<u8, Vec<u32>> = HashMap::new();
    for (i, card) in cards.iter().enumerate() {
        // Folded (#649), matching what `name_trigram` / `name_bigrams` index and what the walk evaluates
        // — that agreement is what makes the tight narrowing sound.
        let mut seen = [false; 256];
        for &b in card.card_name_folded.as_str().as_bytes() {
            if !seen[b as usize] {
                seen[b as usize] = true;
                lists.entry(b).or_default().push(i as u32);
            }
        }
    }
    let wpp = cards.len().div_ceil(64);
    let plane_bytes = wpp * 8;
    let mut idx = NameUnigramIndex { n_cards: cards.len() as u32, ..Default::default() };
    // u16 ids require the card count to fit; past that every byte promotes to a plane, which is valid at
    // any count. Same rule as the bigram index.
    let u16_ok = cards.len() <= u16::MAX as usize + 1;
    for (b, ids) in lists {
        if u16_ok && ids.len() * 2 <= plane_bytes {
            idx.postings.insert(b, ids.into_iter().map(|c| c as u16).collect());
        } else {
            let plane = idx.plane_of.len() as u32;
            idx.plane_of.insert(b, plane);
            idx.plane_words.resize((plane as usize + 1) * wpp, 0);
            for c in ids {
                idx.plane_words[plane as usize * wpp + (c >> 6) as usize] |= 1u64 << (c & 63);
            }
        }
    }
    idx
}

// Named lifetime (not elided/HRTB) so get_text may return text borrowed from the
// string table rather than from the card itself.
fn build_trigram_index<'a, T>(rows: &'a [T], get_text: impl Fn(&'a T) -> &'a str) -> SortedTrigramIndex {
    let mut idx: HashMap<[u8; 3], Vec<u32>> = HashMap::new();
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
    finalize_trigram_index(idx, rows.len())
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

/// One trigram's resolved posting, either tier. trigram_min_posting answers
/// its size bound straight from the index (dense_counts / offsets), without
/// going through this at all, so there's no reason to carry a count here too.
enum TriOperand {
    Posting(Vec<u32>),
    Plane(Vec<u64>),
}

fn lookup_trigram(idx: &Archived<SortedTrigramIndex>, key: [u8; 3]) -> Option<TriOperand> {
    if let Ok(pos) = idx.dense_keys.binary_search(&key) {
        let wpp = words_per_plane(u32::from(idx.domain) as usize);
        let start = pos * wpp;
        let bits = idx.dense_bits[start..start + wpp].iter().map(|w| u64::from(*w)).collect();
        return Some(TriOperand::Plane(bits));
    }
    if let Ok(pos) = idx.sparse_keys.binary_search(&key) {
        let start = u32::from(idx.sparse_offsets[pos]) as usize;
        let end = u32::from(idx.sparse_offsets[pos + 1]) as usize;
        let ids = idx.sparse_postings[start..end].iter().map(|x| u32::from(u16::from(*x))).collect();
        return Some(TriOperand::Posting(ids));
    }
    None
}

/// Posting-vs-plane dispatch (docs/issues/00663-engine-oracle-word-index.md's
/// crossover table): posting×posting merges, posting×plane probes the
/// posting's ids into the plane directly, plane×plane bitmap-ANDs. The
/// smallest posting seeds the working set (as before this index had a dense
/// tier at all); every plane operand filters that seed before any remaining
/// posting merges, since a plane never loses to probing/merging a posting
/// against it. If every operand is dense (no posting to seed from), AND the
/// planes together first and bit-scan the result.
fn intersect_operands(ops: Vec<TriOperand>) -> Vec<u32> {
    let mut planes: Vec<Vec<u64>> = Vec::new();
    let mut postings: Vec<Vec<u32>> = Vec::new();
    for op in ops {
        match op {
            TriOperand::Plane(bits) => planes.push(bits),
            TriOperand::Posting(ids) => postings.push(ids),
        }
    }
    if postings.is_empty() {
        // No sparse operand to seed a working set from — every trigram window
        // landed in the dense tier. Two different shapes get here: a 3-byte
        // needle (a single window, ordinary whenever that one trigram is
        // common enough to be dense) and a longer multi-window needle where
        // every window happens to be a hot trigram (uncommon — a longer
        // needle usually has at least one rarer window, which is what lets
        // the sparse-seeded path below narrow well).
        let mut planes = planes.into_iter();
        let mut acc = planes.next().expect("postings empty implies at least one plane");
        for p in planes {
            for (a, b) in acc.iter_mut().zip(&p) {
                *a &= *b;
            }
        }
        return bitmap_card_ids(&acc);
    }
    postings.sort_by_key(Vec::len);
    // Ascending, and it stays ascending: `swap_remove(0)` seeded from the shortest as
    // intended but left the *longest* at slot 0, so the merge chain below ran in
    // descending-ish order — the expensive direction for an O(|a| + |b|) merge.
    let mut postings = postings.into_iter();
    let mut result = postings.next().expect("checked non-empty above");
    for p in &planes {
        result.retain(|&id| (p[(id >> 6) as usize] >> (id & 63)) & 1 != 0);
    }
    for p in postings {
        if result.is_empty() {
            break;
        }
        result = intersect_sorted(&result, p.as_slice());
    }
    result
}

fn trigram_candidates(idx: &Archived<SortedTrigramIndex>, word: &str) -> Option<Vec<u32>> {
    let bytes = word.as_bytes();
    if bytes.len() < 3 { return None; }

    let mut seen: Vec<[u8; 3]> = Vec::with_capacity(bytes.len() - 2);
    let mut ops: Vec<TriOperand> = Vec::with_capacity(bytes.len() - 2);
    for w in bytes.windows(3) {
        let key = [w[0], w[1], w[2]];
        // Repeated trigrams (e.g. "aaaa") would otherwise intersect the same
        // operand against itself for no benefit.
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        match lookup_trigram(idx, key) {
            Some(op) => ops.push(op),
            // A trigram absent from the index appears in no card: nothing can match.
            None => return Some(Vec::new()),
        }
    }
    Some(intersect_operands(ops))
}

/// Length of the needle's shortest trigram posting/plane — an upper bound on
/// trigram_candidates()' result size, available without materializing or
/// intersecting anything. None: needle under 3 bytes (no trigrams).
/// Some(0): a trigram is absent from the index, so nothing can match.
fn trigram_min_posting(idx: &Archived<SortedTrigramIndex>, word: &str) -> Option<usize> {
    let bytes = word.as_bytes();
    if bytes.len() < 3 {
        return None;
    }
    bytes
        .windows(3)
        .map(|w| {
            let key = [w[0], w[1], w[2]];
            if let Ok(pos) = idx.dense_keys.binary_search(&key) {
                u32::from(idx.dense_counts[pos]) as usize
            } else if let Ok(pos) = idx.sparse_keys.binary_search(&key) {
                (u32::from(idx.sparse_offsets[pos + 1]) - u32::from(idx.sparse_offsets[pos])) as usize
            } else {
                0
            }
        })
        .min()
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

/// Logical negation of a comparison operator (NOT(a op b) == a negate_op(op) b),
/// as opposed to flip_op's operand-order swap. Verified against filter.rs's
/// actual tri() implementation, not just boolean-logic intuition: NumericCmp's
/// NumVal::Null branch short-circuits to Tri::Null before the op-specific
/// comparison ever runs, for every op including Ne, and Not(Null) stays Null
/// (never flips to True) -- so Not(Eq(v)) and Ne(v) agree on null-valued
/// printings too, not just known ones.
fn negate_op(op: CmpOp) -> CmpOp {
    match op {
        CmpOp::Eq => CmpOp::Ne,
        CmpOp::Ne => CmpOp::Eq,
        CmpOp::Lt => CmpOp::Ge,
        CmpOp::Ge => CmpOp::Lt,
        CmpOp::Le => CmpOp::Gt,
        CmpOp::Gt => CmpOp::Le,
    }
}

/// Return sorted card indices satisfying `field op val` using the numeric index.
/// Returns None for Ne (not selective) and Some(empty) when no cards can match.
/// Card-space narrowing needs no selectivity guard (unlike MAX_NARROW_FRACTION
/// for the printing-space indexes): candidates are bounded by the ~3× smaller
/// card count, so even a slice covering the whole index measures at worst
/// break-even against the per-printing scan it would replace.
/// `n_cards` is the id DOMAIN, which is not `idx.len()`: the numeric indexes are nullable (a card with
/// no power has no entry), so ids run past the index's length and a bitmap sized from it would panic.
fn numeric_candidates(idx: &Archived<NumericIndex>, op: CmpOp, val: f64, n_cards: usize) -> Option<Vec<u32>> {
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
    // Card space, so the domain is `n_cards` -- `MATERIALIZE_BITMAP_RATIO` reads the domain rather than
    // assuming printing space, which is the whole reason it is a ratio.
    Some(sorted_ids(idx[start..end].iter().map(|p| u32::from(p.1)), end - start, n_cards))
}

// ─── Arith-expression tuple postings (#743) ──────────────────────────────────
// cmc/power/toughness/loyalty are all card-level and draw from a small bounded
// joint domain (531 distinct (power,toughness,cmc) triples, 564 with loyalty,
// across ~31.5k cards — checked against the corpus). Any numeric predicate that
// is a pure function of only these four fields (an Arith expression, a
// field-vs-field compare, or a bare loyalty compare — see is_arith_tuple_route)
// can be evaluated once per distinct combination instead of once per card, then
// resolved to cards via postings: the same dictionary-encode-then-postings shape
// set_codes/watermarks/rarity use for a single low-cardinality field, extended to
// a joint tuple.

/// One card's joint numeric key. Derived Hash/Eq handle the NULL (`None`) cases
/// natively at build-time interning — no sentinel encoding. `f64::from` on the
/// stored ints is lossless (all four domains fit exactly in f32, so also f64) and
/// matches field_num's own widening exactly (the differential test asserts this).
#[derive(Archive, Serialize, Deserialize, Clone, Copy, PartialEq, Eq, Hash)]
struct ArithTupleKey {
    cmc: Option<u8>,
    power: Option<i8>,
    toughness: Option<i8>,
    loyalty: Option<u8>,
}

/// Joint (cmc,power,toughness,loyalty) postings. `keys[t]` is combination `t`'s field
/// values (for query-time re-evaluation) and `postings[t]` its sorted card ids; the two
/// are parallel. `n_cards` gates applicability (0 = unbuilt, e.g. a test fixture store),
/// like every other index's domain check.
#[derive(Archive, Serialize, Deserialize, Default)]
struct ArithTupleIndex {
    keys: Vec<ArithTupleKey>,
    postings: Vec<Vec<u32>>,
    n_cards: u32,
}

/// Distinct-combination budget for the tuple key, as a multiple of sqrt(cards).
///
/// The tuple route only beats a per-card scan while these four fields collapse the corpus
/// hard, and nothing else in the code checks that. Distinct combinations grow sub-linearly
/// in card count — measured 74, 109, 155, 210, 270, 350, 453 and 564 at 250 up to 31,508
/// cards, so `keys / sqrt(cards)` peaks at 4.9 and falls to 3.2 as the corpus fills in the
/// design space. That makes sqrt a deliberately loose envelope: real growth saturates
/// toward the number of stat lines Magic prints while the bound keeps rising, so headroom
/// widens over time rather than narrowing. A ratio of keys to cards would be the wrong
/// shape — it is naturally ~0.30 at 250 cards and 0.018 at full corpus, so any bound
/// calibrated on production data would fire on every small fixture.
const ARITH_TUPLE_KEYS_PER_SQRT_CARD: usize = 10;

/// Additive slack for small corpora, where a few hundred cards can legitimately be nearly
/// all-distinct before the design space starts repeating itself.
const ARITH_TUPLE_KEYS_SLACK: usize = 32;

/// Below this the keys-to-cards relationship says nothing useful, so the budget is not
/// checked at all. Above it the largest test fixtures still exercise the assertion.
const ARITH_TUPLE_GUARD_MIN_CARDS: usize = 4_096;

/// The `keys` ceiling this corpus size is allowed to produce, per
/// `ARITH_TUPLE_KEYS_PER_SQRT_CARD`. Separate from the assertion so tests can assert
/// against the same arithmetic instead of restating it.
fn arith_tuple_key_budget(n_cards: usize) -> usize {
    ARITH_TUPLE_KEYS_PER_SQRT_CARD * n_cards.isqrt() + ARITH_TUPLE_KEYS_SLACK
}

/// Intern each card's (cmc,power,toughness,loyalty) into a dense combination id and
/// accumulate card postings under it. Cards are visited in ascending index order, so
/// every postings row is naturally sorted. The id space is the number of distinct
/// combinations (~564), far below any width concern — EdhrEc is excluded from the key
/// precisely because it would blow this up to ~card-count distinct values (#743), and the
/// `debug_assert` below is what makes that a test failure rather than a silent regression
/// to a per-card scan with extra indirection.
fn build_arith_tuple_index(cards: &[OracleCard]) -> ArithTupleIndex {
    let mut interner: HashMap<ArithTupleKey, usize> = HashMap::new();
    let mut keys: Vec<ArithTupleKey> = Vec::new();
    let mut postings: Vec<Vec<u32>> = Vec::new();
    for (i, c) in cards.iter().enumerate() {
        let key = ArithTupleKey {
            cmc: c.cmc,
            power: c.creature_power,
            toughness: c.creature_toughness,
            loyalty: c.planeswalker_loyalty,
        };
        let id = *interner.entry(key).or_insert_with(|| {
            keys.push(key);
            postings.push(Vec::new());
            keys.len() - 1
        });
        postings[id].push(i as u32);
    }
    debug_assert!(
        cards.len() < ARITH_TUPLE_GUARD_MIN_CARDS || keys.len() <= arith_tuple_key_budget(cards.len()),
        "arith tuple domain blew up: {} distinct combinations over {} cards, budget {} — is a \
         high-cardinality field in ArithTupleKey?",
        keys.len(),
        cards.len(),
        arith_tuple_key_budget(cards.len()),
    );
    ArithTupleIndex { keys, postings, n_cards: cards.len() as u32 }
}

/// Narrow a tuple-routed `NumericCmp` (`is_arith_tuple_route`) to a card-space candidate
/// set: evaluate the predicate against each of the ~564 distinct combinations and union
/// the postings of those whose result equals `want`. `want = Tri::True` for the positive
/// arm; `Tri::False` for the negation (`Not(cmp)`) — recomputing from scratch rather than
/// complementing sidesteps the NULL-inclusion trap (a NULL-valued combination is `Tri::Null`,
/// excluded from *both* polarities, exactly as tri()'s three-valued logic requires). Every
/// field is card-level, so a combination's verdict holds for all of a card's printings: the
/// result is exact and `tight` in both polarities. Returns None (→ existing scan) when the
/// index isn't built for this store.
fn arith_tuple_narrow(filter: &FilterExpr, idx: &Archived<ArithTupleIndex>, n_cards: usize, want: Tri) -> Option<Narrowed> {
    if u32::from(idx.n_cards) as usize != n_cards || n_cards == 0 {
        return None; // store without this index (fixture) — fall back to the general path
    }
    let FilterExpr::NumericCmp { lhs, op, rhs } = filter else { return None };
    // First pass: evaluate the predicate against each of the ~564 distinct combinations, collecting
    // the matching combination ids and the total card count they cover. This is the whole per-value
    // cost of the narrowing (arithmetic on four small ints, no indirection past the key array).
    let mut matched: Vec<usize> = Vec::new();
    let mut count: usize = 0;
    for (t, key) in idx.keys.iter().enumerate() {
        // Widen exactly as field_num does (see ArithTupleKey's doc): u8/i8 → f64 is lossless and
        // matches field_num's `_ as f32 as f64` for these domains. The archived Option<u8>/<i8>
        // store their scalars natively (no endian wrapper), so `f64::from(*v)` reads them directly.
        let cmc = key.cmc.as_ref().map(|v| f64::from(*v));
        let power = key.power.as_ref().map(|v| f64::from(*v));
        let toughness = key.toughness.as_ref().map(|v| f64::from(*v));
        let loyalty = key.loyalty.as_ref().map(|v| f64::from(*v));
        if eval_arith_tuple_tri(lhs, *op, rhs, cmc, power, toughness, loyalty) == want {
            matched.push(t);
            count += idx.postings[t].len();
        }
    }
    let post_ids = || matched.iter().flat_map(|&t| idx.postings[t].iter().map(|x| u32::from(*x)));
    // Representation split (#636 convention, BITS_PROMOTE): a broad result becomes a card bitmap via
    // an O(count) scatter — no sort, and the word-wise form And/Or actually want for a broad set —
    // while a sparse result keeps the sorted-vec merge path. Each card belongs to exactly one
    // combination, so the selected postings rows are disjoint; the vec is sorted (combination order
    // isn't card order) to restore the sorted-Cards invariant, reserving `count` up front to avoid
    // the realloc churn that made the broad gather ~3× a bare numeric slice before this split.
    if count > *BITS_PROMOTE {
        return Narrowed::tight(Candidates::CardBits(scatter_bits(post_ids(), n_cards)));
    }
    // The case that prompted docs/issues/done/local-engine-candidate-materialize.md: up to 564 posting rows
    // concatenated, each sorted, the whole never so. Only reachable below `BITS_PROMOTE`, since the arm
    // above already hands back a bitmap past it.
    Narrowed::tight(Candidates::Cards(sorted_ids(post_ids(), count, n_cards)))
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


/// A printing-space collection index that stores each value in whichever representation is CHEAPER: a
/// bitmap for dense values, postings for the sparse tail. The same plane/postings split
/// `BorderPrintingPlanes` and `RarityPrintingPlanes` already use, which `frame_data` never got.
///
/// Built ALONGSIDE `build_thresholded_tag_index` for now and selected by `FRAME_HYBRID`, so an
/// interleaved A/B can switch representations per process with both arms reading an identical archive --
/// which removes the layout shift that made the first attempt's measurement unreadable.
///
/// It is intended to replace `build_thresholded_tag_index`, which DROPS any value past
/// `range_too_broad_to_narrow` because "the absent-key convention already means no narrowing, so
/// dropped and unknown values both fall back to the scan". That reasoning expired twice over: #636
/// taught the consumer to scatter a broad list into a bitmap rather than decline, and the drop cost
/// three things where only the first was intended.
///
/// 1. `frame:2015` could not narrow at all — a full 31,508-card scan, ~600 µs.
/// 2. The index was not `complete`, so an unknown frame value could not narrow to provably-empty.
/// 3. `collection_compose_index` excluded `FrameData` outright *for that reason*, keeping every frame
///    query off `PrintingCompose` — the path that pages `border:black` in 26 µs.
///
/// Storing every value makes the index complete, which is what unlocks (2) and (3). And the guard was
/// inverted rather than merely stale: of the 29 `frame_data` values, it dropped the ONE where a bitmap
/// wins by the largest margin (`2015`, 250.5 KB of postings against 11.9 KB of bitmap) while keeping as
/// postings six more that would also be smaller as bitmaps. Per-value cheaper-of-the-two is 111 KB
/// against the 240 KB stored before, so this is smaller AND faster, not a trade.
#[derive(Archive, Serialize, Deserialize, Default)]
struct HybridTagIndex {
    /// Values above the density crossover: one printing-space bitmap each, ready to use with no scatter.
    dense: HashMap<String, Vec<u64>>,
    /// Everything below it: sorted printing ids, exactly as `TagIndex`.
    ///
    /// A sparse value can never be "broad" downstream: the crossover is 1/32 (3.1%) and
    /// `range_too_broad_to_narrow` fires at 25%, so anything landing here is far under that guard.
    sparse: TagIndex,
}

/// A stored bitmap is smaller than a posting list once `4 * k > n / 8` bytes, i.e. `k * 32 > n` —
/// density above 1/32. Integer arithmetic so the boundary is exact.
fn bitmap_beats_postings(k: usize, n_rows: usize) -> bool {
    k.saturating_mul(32) > n_rows
}

fn build_hybrid_tag_index<T>(rows: &[T], vocab: &[String], get_ids: impl Fn(&T) -> &Vec<u16>) -> HybridTagIndex {
    let n = rows.len();
    let mut out = HybridTagIndex::default();
    for (value, postings) in build_tag_index(rows, vocab, get_ids) {
        if bitmap_beats_postings(postings.len(), n) {
            out.dense.insert(value, scatter_bits(postings, n));
        } else {
            out.sparse.insert(value, postings);
        }
    }
    out
}

impl ArchivedHybridTagIndex {
    /// Whether this value is stored as a bitmap, i.e. whether `bits` costs a copy rather than a scatter.
    fn is_dense(&self, value: &str) -> bool {
        self.dense.get(value).is_some()
    }

    /// The value's printing-space bitmap, materialized from postings if that is how it is stored.
    /// `None` only when the value is absent from the store entirely — nothing is dropped, so that is a
    /// proof rather than a gap.
    fn bits(&self, value: &str, n_printings: usize) -> Option<Vec<u64>> {
        if let Some(b) = self.dense.get(value) {
            return Some(b.iter().map(|w| u64::from(*w)).collect());
        }
        self.sparse.get(value).map(|v| scatter_bits(v.iter().map(|x| u32::from(*x)), n_printings))
    }

    /// Printings carrying this value, for the estimator and for the sparse narrowing path. Counts
    /// without materializing when the value is a bitmap.
    fn len_of(&self, value: &str) -> Option<usize> {
        if let Some(b) = self.dense.get(value) {
            return Some(b.iter().map(|w| u64::from(*w).count_ones() as usize).sum());
        }
        self.sparse.get(value).map(|v| v.len())
    }
}

fn build_artist_index(printings: &[Printing], n_artists: usize) -> ArtistIndex {
    let (offsets, out) = build_csr(n_artists, printings.len(), |i| {
        let vid = printings[i].card_artist_vid;
        (vid != ARTIST_NONE).then_some(vid as usize)
    });
    ArtistIndex { offsets, printings: out }
}

/// Expand matching artist vocab ids to sorted printing ids via the CSR table.
fn expand_artist_ids(idx: &Archived<ArtistIndex>, artist_ids: &[u16], n_printings: usize) -> Vec<u32> {
    expand_csr(&idx.offsets, &idx.printings, artist_ids.iter().map(|&a| a as usize), n_printings)
}

// ─── Flavor-text index ────────────────────────────────────────────────────────
// Flavor is the last unindexed text field: predicates used to run per printing
// (52k contains over 26.3k distinct texts) and could never narrow, voiding Or
// narrowing for the whole node. Instead of a trigram index (measured ~5-9 MB),
// bind() evaluates the predicate once over the distinct texts and rewrites the
// node to FlavorMatch (the ArtistMatch pattern at 12x the vocab size); the CSR
// here expands matched texts to printing candidates for narrowing (~0.4 MB).
//
// The bind scan is prefiltered by a 128-bit learned fingerprint per distinct
// text: one bit per feature gram, and a text can contain the needle only if it
// contains every feature gram the needle contains — `(text & needle) == needle`
// in one u128 compare. Features were selected greedily over the live corpus to
// minimize residual pass rate on a corpus-vocabulary needle workload, with
// enough tail slots backfilled with the unchosen letters that every needle
// fires at least one bit (worst case degrades to the letter-mask floor, never
// to an unfiltered scan). Measured: ~2% of texts survive typical needles (held-out 500-word split).
// Regenerate with scripts/generate_flavor_fingerprint.py if selectivity
// drifts; staleness costs selectivity, never correctness.

const FLAVOR_FP_FEATURES: [&str; 128] = [
    "ed", "ri", "ra", "es", "te", "le", "p", "ng",
    "nt", "de", "al", "el", "ns", "ar", "v", "k",
    "ti", "la", "ce", "se", "ro", "ta", "ch", "ea",
    "co", "sh", "li", "rs", "ni", "di", "mi", "ol",
    "ur", "un", "si", "ts", "lo", "ne", "or", "ai",
    "ge", "st", "me", "il", "en", "ec", "ly", "b",
    "tr", "ma", "sa", "z", "ds", "ic", "ss", "pe",
    "io", "ie", "re", "ul", "na", "ho", "ee", "us",
    "fa", "rd", "oo", "ca", "x", "et", "cr", "su",
    "ia", "wa", "so", "ga", "rt", "id", "mo", "ty",
    "ls", "er", "ad", "bo", "sp", "gh", "j", "ru",
    "am", "cl", "fi", "ow", "pr", "fe", "gi", "da",
    "is", "ac", "gr", "ha", "rn", "dr", "gu", "as",
    "em", "ir", "lu", "at", "vi", "a", "c", "d",
    "e", "f", "g", "h", "i", "l", "m", "n",
    "o", "q", "r", "s", "t", "u", "w", "y",
];

static FLAVOR_FP_MAP: std::sync::OnceLock<HashMap<&'static [u8], u32>> = std::sync::OnceLock::new();

/// 128-bit feature mask of a (lowercase) string: bit i set iff the string
/// contains FLAVOR_FP_FEATURES[i]. Both distinct texts (at build) and needles
/// (at bind) are masked with this same table, which is what makes the superset
/// test sound. ASCII-alpha byte windows only, so multi-byte UTF-8 is skipped
/// harmlessly (features are all ASCII).
pub(crate) fn flavor_fingerprint(s: &str) -> u128 {
    let map = FLAVOR_FP_MAP
        .get_or_init(|| FLAVOR_FP_FEATURES.iter().enumerate().map(|(i, f)| (f.as_bytes(), i as u32)).collect());
    let b = s.as_bytes();
    let mut fp = 0u128;
    for n in 1..=3usize {
        if b.len() < n {
            break;
        }
        for w in b.windows(n) {
            if w.iter().all(|c| c.is_ascii_lowercase())
                && let Some(&i) = map.get(w)
            {
                fp |= 1u128 << i;
            }
        }
    }
    fp
}

#[derive(Archive, Serialize, Deserialize, Default)]
pub(crate) struct FlavorIndex {
    /// Dense flavor text id → global string id (CardData.strings) of the
    /// distinct lowercase flavor text, in first-seen printing order.
    gids: Vec<u32>,
    /// Parallel to gids: [lo, hi] halves of the text's u128 fingerprint.
    fingerprints: Vec<[u64; 2]>,
    /// CSR: printings carrying text `d` live at
    /// `printings[offsets[d] .. offsets[d + 1]]`. Length gids.len() + 1.
    offsets: Vec<u32>,
    printings: Vec<u32>,
}

fn build_flavor_index(printings: &[Printing], strings: &[String]) -> FlavorIndex {
    /// Row of a printing carrying no flavor text — omitted from the CSR table entirely.
    const NO_ROW: u32 = u32::MAX;

    // Assign dense ids in first-seen printing order, recording each printing's row as we
    // go. Flavor is the one caller whose row is a hash lookup rather than a field read,
    // and `build_csr` asks twice; materializing the rows here keeps this at one probe per
    // printing, against two before the CSR helpers existed.
    let mut dense_of: HashMap<u32, u32> = HashMap::new();
    let mut gids: Vec<u32> = Vec::new();
    let mut row_of_printing: Vec<u32> = Vec::with_capacity(printings.len());
    for p in printings {
        let gid = p.flavor_text_lower_id;
        if gid == NONE_STR {
            row_of_printing.push(NO_ROW);
            continue;
        }
        let dense = *dense_of.entry(gid).or_insert_with(|| {
            gids.push(gid);
            (gids.len() - 1) as u32
        });
        row_of_printing.push(dense);
    }
    let (offsets, out) = build_csr(gids.len(), printings.len(), |i| {
        let row = row_of_printing[i];
        (row != NO_ROW).then_some(row as usize)
    });
    let fingerprints = gids
        .iter()
        .map(|&g| {
            let fp = flavor_fingerprint(strings[g as usize].as_str());
            [fp as u64, (fp >> 64) as u64]
        })
        .collect();
    FlavorIndex { gids, fingerprints, offsets, printings: out }
}

/// Resolve a flavor predicate against the distinct texts: (sorted global
/// string ids for per-printing membership, dense text ids for CSR narrowing).
/// `needle_mask` skips texts that cannot contain the needle (0 = no prefilter,
/// e.g. regex or non-containment comparisons).
pub(crate) fn flavor_match_sets(
    flavor: &Archived<FlavorIndex>,
    strings: &AStrings,
    needle_mask: u128,
    pred: impl Fn(&str) -> bool,
) -> (Vec<u32>, Vec<u32>) {
    let mut gids: Vec<u32> = Vec::new();
    let mut dense: Vec<u32> = Vec::new();
    for (d, gid) in flavor.gids.iter().enumerate() {
        if needle_mask != 0 {
            let fp = &flavor.fingerprints[d];
            let mask = u64::from(fp[0]) as u128 | ((u64::from(fp[1]) as u128) << 64);
            if mask & needle_mask != needle_mask {
                continue;
            }
        }
        let g = u32::from(*gid);
        if pred(strings[g as usize].as_str()) {
            gids.push(g);
            dense.push(d as u32);
        }
    }
    // Dense ids are ascending by construction; global ids follow interner
    // order, not first-seen printing order — sort for binary-search membership.
    gids.sort_unstable();
    (gids, dense)
}

/// Expand matched dense flavor text ids to sorted printing ids via the CSR.
/// `n_printings` is the corpus, not `idx.printings.len()` — the table omits printings
/// without flavor text, and the bitmap route sizes itself from the id space, not the payload.
fn expand_flavor_ids(idx: &Archived<FlavorIndex>, dense_ids: &[u32], n_printings: usize) -> Vec<u32> {
    expand_csr(&idx.offsets, &idx.printings, dense_ids.iter().map(|&d| d as usize), n_printings)
}

// ─── Sort permutations (streamed selection) ──────────────────────────────────
// One precomputed card ordering per (card-level sort column, direction), used
// by the streamed emission path (see run_query): walk the permutation, test
// membership in the match bitmap, and only page cards are ever touched — no
// sort keys, no quickselect, no prefer walk outside the page. Keys mirror
// sort_key_bits with the card's store-preferred first printing standing in
// for the query-chosen one: exact for the dominant unique=card default-prefer
// case, and only orderable-differently inside blocks tied on both the primary
// column and edhrec rank. Two permutations per column because direction folds
// into the primary key only — secondaries keep their fixed order in both
// directions, so a reversed ascending walk would be wrong inside ties.
// 10 × ~126 kB ≈ 1.26 MB.
//
// `inv` mirrors `perm` one-for-one (inv[col][dir][card] = card's position in
// that sort order) for #634 Step 2's popcount-skip order phase: scattering a
// match bitmap through inv turns "walk the permutation, skip page_offset
// matches" into "accumulate word popcounts to the boundary word," O(words)
// instead of O(matches). Stored explicitly per direction rather than derived
// from one another (e.g. inv_desc[c] = n-1-inv_asc[c]) for the same reason
// `perm` itself isn't derived that way: ties keep fixed relative order in
// both directions (see above), so reversing one inverse gets tied groups'
// internal order backwards — verified by re-deriving the sort key construction
// before implementing, not assumed from the general "arrays can be negated"
// intuition. Same size as `perm`: another ~1.26 MB.

#[derive(Archive, Serialize, Deserialize, Default)]
struct SortPermutations {
    // [ascending, descending] per column
    edhrec:    [Vec<u32>; 2],
    cubecobra: [Vec<u32>; 2],
    cmc:       [Vec<u32>; 2],
    power:     [Vec<u32>; 2],
    toughness: [Vec<u32>; 2],
    // Keyed on name_rank, so the ascending permutation is also the sorted-name
    // lookup table: equal-name blocks are contiguous (rank is the primary key)
    // and narrow_rec's ExactName arm binary-searches it.
    name:      [Vec<u32>; 2],
    // Inverse of each column above, same [ascending, descending] layout.
    edhrec_inv:    [Vec<u32>; 2],
    cubecobra_inv: [Vec<u32>; 2],
    cmc_inv:       [Vec<u32>; 2],
    power_inv:     [Vec<u32>; 2],
    toughness_inv: [Vec<u32>; 2],
    name_inv:      [Vec<u32>; 2],
}

impl ArchivedSortPermutations {
    /// The permutation for a streamable column/direction; None for the
    /// printing-keyed columns (rarity, usd), whose sort key depends on the
    /// prefer-chosen printing and cannot be precomputed.
    fn get(&self, col: SortCol, descending: bool) -> Option<&Archived<Vec<u32>>> {
        let pair = match col {
            SortCol::EdhrecRank => &self.edhrec,
            SortCol::Cubecobra  => &self.cubecobra,
            SortCol::Cmc        => &self.cmc,
            SortCol::Power      => &self.power,
            SortCol::Toughness  => &self.toughness,
            SortCol::Name       => &self.name,
            SortCol::Rarity | SortCol::PriceUsd => return None,
        };
        Some(&pair[descending as usize])
    }

    /// The inverse permutation for a streamable column/direction (#634 Step 2).
    fn get_inv(&self, col: SortCol, descending: bool) -> Option<&Archived<Vec<u32>>> {
        let pair = match col {
            SortCol::EdhrecRank => &self.edhrec_inv,
            SortCol::Cubecobra  => &self.cubecobra_inv,
            SortCol::Cmc        => &self.cmc_inv,
            SortCol::Power      => &self.power_inv,
            SortCol::Toughness  => &self.toughness_inv,
            SortCol::Name       => &self.name_inv,
            SortCol::Rarity | SortCol::PriceUsd => return None,
        };
        Some(&pair[descending as usize])
    }

    /// Both directions of one streamable column, length-checked against the card count, or `None` if
    /// the column has no permutation. The single answer to "can a plan stream this orderby": every
    /// plan that walks a permutation also indexes its inverse, so all three applicability predicates
    /// and all three executors want the pair or neither.
    fn order(&self, col: SortCol, descending: bool, n_cards: usize) -> Option<SortOrder<'_>> {
        let perm = self.get(col, descending)?;
        let inv = self.get_inv(col, descending)?;
        (perm.len() == n_cards && inv.len() == n_cards).then_some(SortOrder { perm, inv })
    }
}

/// One streamable column's card ordering and its inverse, which every permutation-walking plan needs
/// together: the walk reads `perm`, and finding where in that order a card sits reads `inv`.
///
/// Paired in one value, obtained from one length-checked lookup (`ArchivedSortPermutations::order`),
/// because the two were previously fetched by five call sites with two `expect`s each and applicability
/// predicates that checked the forward array's length but not the inverse's — a store whose two arrays
/// disagreed would have reached an indexing panic inside an executor rather than a decline.
#[derive(Copy, Clone)]
struct SortOrder<'a> {
    perm: &'a Archived<Vec<u32>>,
    inv:  &'a Archived<Vec<u32>>,
}

/// Dense byte-order rank of card_name_lower onto each card (equal names share
/// a rank; the standard sort secondaries break their ties). Every card has a
/// name, so unlike the other sort columns the rank is never absent.
fn assign_name_ranks(cards: &mut [OracleCard]) {
    let mut ids: Vec<u32> = (0..cards.len() as u32).collect();
    ids.sort_unstable_by(|&a, &b| {
        cards[a as usize].card_name_lower.as_str().cmp(cards[b as usize].card_name_lower.as_str())
    });
    let mut rank = 0u32;
    for i in 0..ids.len() {
        if i > 0
            && cards[ids[i - 1] as usize].card_name_lower.as_str() != cards[ids[i] as usize].card_name_lower.as_str()
        {
            rank += 1;
        }
        cards[ids[i] as usize].name_rank = rank;
    }
}

/// `inv[perm[i]] == i` — the position of each card within the permutation.
fn invert_perm(perm: &[u32]) -> Vec<u32> {
    let mut inv = vec![0u32; perm.len()];
    for (pos, &card) in perm.iter().enumerate() {
        inv[card as usize] = pos as u32;
    }
    inv
}

/// An inclusive value interval on the sort column that every match must satisfy, or `UNBOUNDED` when
/// the filter says nothing about it. Extracted from the filter BEFORE `split_planes` consumes it (see
/// `sort_col_bound`) and carried on `QueryParams`, because that is the only point at which the tree is
/// still readable: `cmc>=6` compiles to mask algebra over bitplanes, and the residual left behind is
/// `FilterExpr::True`.
#[derive(Clone, Copy, Debug, PartialEq)]
struct SortBound {
    lo: Option<f64>,
    hi: Option<f64>,
}

impl SortBound {
    /// No constraint: the walk covers the whole permutation, exactly as it did before this existed.
    /// Also the safe default — every path that does not extract a bound gets this one, so a caller that
    /// forgets to loses performance and not correctness.
    const UNBOUNDED: SortBound = SortBound { lo: None, hi: None };

    fn is_unbounded(self) -> bool {
        self.lo.is_none() && self.hi.is_none()
    }

    /// Tighten with another interval — `And`'s rule, and the only way bounds combine.
    fn intersect(self, other: SortBound) -> SortBound {
        let tighter = |a: Option<f64>, b: Option<f64>, pick: fn(f64, f64) -> f64| match (a, b) {
            (Some(x), Some(y)) => Some(pick(x, y)),
            (v, None) | (None, v) => v,
        };
        SortBound { lo: tighter(self.lo, other.lo, f64::max), hi: tighter(self.hi, other.hi, f64::min) }
    }
}

/// The inclusive interval on `sort_col` that every match of `filter` must satisfy.
///
/// Deliberately conservative in every direction, because the cost of being too wide is a longer walk
/// and the cost of being too narrow is a wrong page:
///
/// - **`And` only.** An `Or` child can match outside any bound its sibling implies, and `Not` inverts
///   into a union of two intervals that this cannot express. Both yield `UNBOUNDED` for that subtree,
///   which an enclosing `And` then simply ignores.
/// - **Strict comparisons are widened to inclusive.** `cmc>6` reports `lo = 6`, so the walk may start
///   at the head of the `cmc == 6` tie block and step over it. One block of wasted steps against having
///   to reason about float adjacency.
/// - **Card-level columns only**, and only the three with numeric predicates. Everything else has no
///   `NumericCmp` that can constrain it (there is no `edhrec>` in the query language), and `Rarity` /
///   `PriceUsd` have no permutation to search.
fn sort_col_bound(filter: &FilterExpr, sort_col: SortCol) -> SortBound {
    let field = match sort_col {
        SortCol::Cmc => NumField::Cmc,
        SortCol::Power => NumField::Power,
        SortCol::Toughness => NumField::Toughness,
        _ => return SortBound::UNBOUNDED,
    };
    match filter {
        FilterExpr::And(children) => {
            children.iter().fold(SortBound::UNBOUNDED, |acc, c| acc.intersect(sort_col_bound(c, sort_col)))
        }
        // `op` reads left-to-right, so a constant on the left flips it: `6 <= cmc` bounds cmc BELOW.
        FilterExpr::NumericCmp { lhs: NumExpr::Field(f), op, rhs: NumExpr::Const(v) } if *f == field => {
            bound_from_cmp(*op, *v)
        }
        FilterExpr::NumericCmp { lhs: NumExpr::Const(v), op, rhs: NumExpr::Field(f) } if *f == field => {
            bound_from_cmp(flip_op(*op), *v)
        }
        _ => SortBound::UNBOUNDED,
    }
}

/// One comparison's inclusive interval. `Ne` constrains nothing (it excludes a point from the middle,
/// which is not an interval), and the strict operators widen to their inclusive neighbours.
fn bound_from_cmp(op: CmpOp, v: f64) -> SortBound {
    match op {
        CmpOp::Ge | CmpOp::Gt => SortBound { lo: Some(v), hi: None },
        CmpOp::Le | CmpOp::Lt => SortBound { lo: None, hi: Some(v) },
        CmpOp::Eq => SortBound { lo: Some(v), hi: Some(v) },
        CmpOp::Ne => SortBound::UNBOUNDED,
    }
}

/// The permutation's PRIMARY key: the sort column's value, direction folded in by negation, absent
/// sorting last. One function because two places must agree on it exactly — `build_sort_permutations`
/// orders by it, and `walk_bounds` binary-searches for it to find where a value interval starts and
/// ends in that order. A divergence between those two would not be a slow walk, it would be a walk
/// that starts past real matches and silently returns the wrong page.
///
/// `u32::MAX` for absent is what makes a numeric bound safe to search for: a card with no value in the
/// column sorts past every finite key in BOTH directions, and `numeric_cmp_tri` answers `Tri::Null` for
/// it, so it can never be a match of a comparison against that column either.
fn perm_primary_key(value: Option<f32>, descending: bool) -> u32 {
    value.map_or(u32::MAX, |v| f32_sort_bits(if descending { -v } else { v }))
}

/// The sort column's value for one archived card, in the same units `build_sort_permutations` sorted
/// on. Card-level columns only: `Rarity`/`PriceUsd` have no permutation (they depend on the
/// prefer-chosen printing), which is why `ArchivedSortPermutations::get` returns `None` for them.
fn sort_col_card_value(card: &AOracleCard, sort_col: SortCol) -> Option<f32> {
    match sort_col {
        SortCol::EdhrecRank => card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32),
        SortCol::Cubecobra  => card.cubecobra_score.as_ref().map(|v| f32::from(*v)),
        // Single bytes, so archived and native are the same type — no `u8::from` to unwrap.
        SortCol::Cmc        => card.cmc.as_ref().map(|v| f32::from(*v)),
        SortCol::Power      => card.creature_power.as_ref().map(|v| f32::from(*v)),
        SortCol::Toughness  => card.creature_toughness.as_ref().map(|v| f32::from(*v)),
        SortCol::Name       => Some(u32::from(card.name_rank) as f32),
        SortCol::Rarity | SortCol::PriceUsd => None,
    }
}

/// The segment of `perm` that can contain a match, from the filter's interval on the sort column.
///
/// The permutation is a sorted array on `perm_primary_key`, so an interval on the sort column's VALUE
/// is a contiguous range of POSITIONS, found by two binary searches — O(log n_cards) probes once per
/// query, against the alternative of reading `inv_perm` once per matching card. Which end of the walked
/// order each side of the interval lands on depends on the direction, because the key negates: under
/// `asc` the low bound starts the segment, under `desc` the high bound does.
///
/// Returns the whole permutation when the filter constrains nothing, which is also what every caller
/// gets that does not extract a bound.
fn walk_bounds<'p>(
    perm: &'p Archived<Vec<u32>>,
    cards: &[AOracleCard],
    sort_col: SortCol,
    descending: bool,
    bound: SortBound,
) -> &'p [Archived<u32>] {
    let all = &perm[..];
    if bound.is_unbounded() || perm.len() != cards.len() || *WALK_SORT_BOUND == 0 {
        return all;
    }
    // Values are read through the same encoder the permutation was built with, so this comparison and
    // that ordering cannot disagree.
    let key_at = |pos: usize| {
        let cid = u32::from(all[pos]) as usize;
        perm_primary_key(sort_col_card_value(&cards[cid], sort_col), descending)
    };
    // Under `desc` the key is negated, so the interval's ends swap roles: the largest VALUE has the
    // smallest key and therefore comes first.
    let (first_v, last_v) = if descending { (bound.hi, bound.lo) } else { (bound.lo, bound.hi) };
    let key_of = |v: f64| perm_primary_key(Some(v as f32), descending);
    // `partition_point` over positions: the first position whose key reaches the interval, and the
    // first position past it. Both sides are inclusive in value, so `start` excludes keys strictly
    // before the interval and `end` includes the whole tie block at its far edge.
    let start = first_v.map_or(0, |v| {
        let k = key_of(v);
        partition_point(all.len(), |pos| key_at(pos) < k)
    });
    let end = last_v.map_or(all.len(), |v| {
        let k = key_of(v);
        partition_point(all.len(), |pos| key_at(pos) <= k)
    });
    // An empty or inverted range means the interval falls between two stored values (`cmc>=6 cmc<=5`),
    // so nothing can match; the walk then steps nothing rather than being handed a reversed slice.
    if start >= end { &all[..0] } else { &all[start..end] }
}

/// `partition_point` over an index range: the first `i` in `0..len` where `pred(i)` is false, with
/// `pred` monotone. `slice::partition_point` cannot be used directly because the predicate needs the
/// POSITION (to read the card behind it), not the element.
fn partition_point(len: usize, pred: impl Fn(usize) -> bool) -> usize {
    let (mut lo, mut hi) = (0usize, len);
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if pred(mid) { lo = mid + 1 } else { hi = mid }
    }
    lo
}

fn build_sort_permutations(cards: &[OracleCard]) -> SortPermutations {
    // Purely card-space now: the printings/offsets arguments existed only to read the first stored
    // printing's prefer_score, which is no longer a sort key (see the closure below).
    let perm = |get: &dyn Fn(&OracleCard) -> Option<f32>, descending: bool| -> Vec<u32> {
        let mut ids: Vec<u32> = (0..cards.len() as u32).collect();
        ids.sort_unstable_by_key(|&i| {
            let c = &cards[i as usize];
            let pk = perm_primary_key(get(c), descending);
            let e = c.edhrec_rank.unwrap_or(u32::MAX);
            // Canonical secondary: the first (store-preferred) printing's
            // default prefer score, matching sort_key_bits' third component
            // for the printing the default prefer chooses.
            // Keys 1-2 then the card index. prefer_score is deliberately NOT a key here: it used to
            // be, taken from the first stored printing, which the gathered paths cannot reproduce
            // because they see the first MATCHING printing instead. Ordering on something only one
            // side can compute is what made row order depend on which plan ran. See `page_cmp`.
            (((pk as u128) << 64) | (e as u128), i)
        });
        ids
    };
    // Inverse built per direction, not derived from one another — ties keep
    // fixed relative order in both directions (see the struct doc above), so
    // reversing one inverse would get tied groups' internal order backwards.
    let both = |get: &dyn Fn(&OracleCard) -> Option<f32>| -> ([Vec<u32>; 2], [Vec<u32>; 2]) {
        let asc = perm(get, false);
        let desc = perm(get, true);
        let inv = [invert_perm(&asc), invert_perm(&desc)];
        ([asc, desc], inv)
    };
    let (edhrec, edhrec_inv) = both(&|c| c.edhrec_rank.map(|v| v as f32));
    let (cubecobra, cubecobra_inv) = both(&|c| c.cubecobra_score);
    let (cmc, cmc_inv) = both(&|c| c.cmc.map(|v| v as f32));
    let (power, power_inv) = both(&|c| c.creature_power.map(|v| v as f32));
    let (toughness, toughness_inv) = both(&|c| c.creature_toughness.map(|v| v as f32));
    let (name, name_inv) = both(&|c| Some(c.name_rank as f32));
    SortPermutations {
        edhrec, cubecobra, cmc, power, toughness, name,
        edhrec_inv, cubecobra_inv, cmc_inv, power_inv, toughness_inv, name_inv,
    }
}

/// Assigns each printing's `artwork_group_id` (dense, per-card: 0 = first-seen
/// illustration in stored order — descending prefer_score — 1 = next, shared
/// artwork shares the id) and returns the per-card distinct-illustration count
/// (u16: max printings per card is ~1k). Single source of truth for both derived
/// arrays so they can't drift out of sync with each other or with `illustration_id`.
///
/// The count is consumed by the streamed match phase when the card pass already
/// proved every printing matches: the artwork-mode contribution is then a
/// build-time constant and the per-printing grouping walk is skipped entirely.
/// The per-printing id is consumed by `card_match_count`/`push_card_matches`'s
/// `Mode::Artwork` arms (#629) to replace `illustration_id`-UUID bookkeeping with
/// dense-integer set operations.
fn assign_artwork_groups(printings: &mut [Printing], offsets: &[u32]) -> Vec<u16> {
    let mut counts = Vec::with_capacity(offsets.len().saturating_sub(1));
    let mut ills: Vec<u128> = Vec::new();
    for w in offsets.windows(2) {
        ills.clear();
        for p in &mut printings[w[0] as usize..w[1] as usize] {
            let gid = match ills.iter().position(|&x| x == p.illustration_id) {
                Some(pos) => pos,
                None => {
                    ills.push(p.illustration_id);
                    ills.len() - 1
                }
            };
            p.artwork_group_id = gid as u16;
        }
        // Checked once per card at load time, not once per printing per query --
        // see ARTWORK_GROUP_WORDS' doc for why this bound is expected to hold and
        // card_match_count's seen_words for the fixed-size bitmask it protects.
        assert!(
            ills.len() <= ARTWORK_GROUP_WORDS * 64,
            "card has {} distinct artwork groups, exceeds ARTWORK_GROUP_WORDS bound ({})",
            ills.len(),
            ARTWORK_GROUP_WORDS * 64
        );
        counts.push(ills.len() as u16);
    }
    counts
}

/// Direct `printing_id -> card_id` lookup, one linear pass over `offsets`.
/// Benchmarked (`bench_card_dedup.rs`) at ~2x cheaper than the
/// scatter-into-printing-bitmap-then-monotone-cursor path `cards_of_printings`
/// otherwise pays past 1024 matches, and unconditionally cheaper than the
/// small-k `partition_point` binary search too — see
/// docs/issues/00690-engine-direct-projection-arrays.md.
fn build_printing_to_card(offsets: &[u32]) -> Vec<u32> {
    let n_printings = offsets.last().copied().unwrap_or(0) as usize;
    let mut out = vec![0u32; n_printings];
    for (card, w) in offsets.windows(2).enumerate() {
        for p in w[0]..w[1] {
            out[p as usize] = card as u32;
        }
    }
    out
}

/// Exact result totals in all three `unique=` spaces for one value of a low-cardinality dimension.
///
/// 12 bytes. Every space is stored rather than derived because none derives from the others: printings
/// is not cards times a reprint rate, and artworks sits between them at a ratio that varies per value
/// (0.63-0.95 measured across `frame:` values alone, which is exactly why estimating it failed).
#[derive(Archive, Serialize, Deserialize, Clone, Copy, Default, Debug, PartialEq, Eq)]
struct SpaceTotals {
    printings: u32,
    cards: u32,
    artworks: u32,
}

impl ArchivedSpaceTotals {
    fn get(&self, mode: Mode) -> usize {
        match mode {
            Mode::Printing => u32::from(self.printings) as usize,
            Mode::Card => u32::from(self.cards) as usize,
            Mode::Artwork => u32::from(self.artworks) as usize,
        }
    }
}

/// Exact 3-space totals per value, for the dimensions whose predicate tests ONE value.
///
/// The whole table is ~2 KB on the production corpus, because every dimension here has under 30 values.
/// At that size there is no threshold to tune and no sparse tail to special-case: store all of them.
/// This is the counterpart to `RangeCardCounts`, which covers the dimensions whose predicates are
/// ranges and therefore need prefix/suffix aggregates instead of per-value ones.
///
/// What it does NOT replace: where a query already reads a card-space plane, that plane's popcount is
/// the exact card total for nothing (how legality got exact card counts before this table existed). The
/// table's value is the two spaces a card-space plane cannot give — a card bit does not say WHICH
/// printing matched, so it cannot count printings or artworks.
#[derive(Archive, Serialize, Deserialize, Default)]
struct ValueTotals {
    /// `border:` — printing-space, 5 values. Keyed by the interned border string, which is what
    /// `TextExact` compares against byte-for-byte.
    border: HashMap<String, SpaceTotals>,
    /// `layout:` and the `is:flip`/`is:split`/… family — card-space, 14 values.
    layout: HashMap<String, SpaceTotals>,
    /// `frame:` and `is:new`/`is:old` — printing-space, 29 values. Keyed by the `coll_vocab` string.
    frame_data: HashMap<String, SpaceTotals>,
    /// `f:X` / `banned:X` / `restricted:X`, keyed `(shift << 2) | status`.
    ///
    /// One entry per (format, status) pair rather than per format, because `FilterExpr::Legality`
    /// carries a single 2-bit `expected` status and tests `(word >> shift) & 0b11 == expected`. Statuses
    /// PARTITION each space — a printing has exactly one status per format — so a per-status entry is
    /// both exact on its own and safely summable if a future predicate ever accepts a set.
    ///
    /// Counted per PRINTING, not per card, so `legality_divergent` cards (30A, Collectors' Edition,
    /// gold border) contribute their printings' own words. Reading the card word for those would
    /// mis-count exactly the cards the divergence flag exists to flag.
    legality: HashMap<u16, SpaceTotals>,
}

/// The key `ValueTotals::legality` uses. `shift` is even and < 64, so this cannot collide.
fn legality_totals_key(shift: u8, expected: u64) -> u16 {
    (u16::from(shift) << 2) | (expected as u16 & 0b11)
}

/// A value is worth PAIRING only if it is broad enough that an estimate about it can change a routing
/// decision. `STREAM_MIN_MATCHES` is that line: below it the sparse floor decides the plan, not the
/// estimate's precision, and min-over-singles is already within a small factor of the truth.
///
/// Measured on the production corpus, this prunes the table 4.2x — 3,705 pairs to 879 — and it removes
/// `layout` entirely, because only `normal` clears the floor and every other layout query is already
/// selective. Counted in PRINTINGS, which is the conservative side: cards <= printings always, so a value
/// with 1,500 printings but 400 cards is kept, and that is exactly the one card-mode routing needs.
const PAIR_MIN_PRINTINGS: usize = 1_024;

/// Exact 3-space totals for PAIRS of low-cardinality values, so an `And` of two of them is answered
/// rather than bounded.
///
/// `compose_printing_estimate`'s `And` folds with `min`, an intersection upper bound, so the most
/// selective leaf wins and every other conjunct contributes nothing: every `f:X border:white` estimates
/// identically at 5,131 against true totals of 658-5,072. For a two-leaf query a stored pair is not a
/// tighter bound, it is the exact answer; for three leaves, min-over-pairs measured 2.02x against
/// min-over-singles' 7.80x on `f:modern r:rare border:white`.
///
/// **Unlike `ValueTotals`, this table may be incomplete.** Absence there is read as an exact zero and so
/// the singleton table must cover every value; absence HERE just means "no answer", and the caller falls
/// back to the min bound it already had. That is what lets the selectivity floor above prune it at all.
///
/// Same-dimension pairs are stored only for `frame_data`, the one multi-valued dimension here (1-5 values
/// per printing; `frame:2015 frame:legendary` really does match 10,321). Border, rarity and legality are
/// PARTITIONS — one value per printing — so two distinct values of one of them never co-occur, which is a
/// rule rather than data and needs no bytes.
#[derive(Archive, Serialize, Deserialize, Default)]
struct PairTotals {
    /// Each dimension's dense values → a compact id, shared across dimensions so a pair is one `u32`.
    border: HashMap<String, u16>,
    rarity: HashMap<u8, u16>,
    frame: HashMap<String, u16>,
    /// Keyed as `ValueTotals::legality` is, `(shift << 2) | status`.
    legality: HashMap<u16, u16>,
    /// `min(a,b) * n_ids + max(a,b)` → the pair's exact totals. Complete over the stored ids: a present
    /// key is exact (possibly zero), a missing one means at least one value was pruned by the floor.
    pairs: HashMap<u32, SpaceTotals>,
    n_ids: u16,
}

impl ArchivedPairTotals {
    fn key(&self, a: u16, b: u16) -> u32 {
        let (lo, hi) = if a <= b { (a, b) } else { (b, a) };
        u32::from(lo) * u32::from(u16::from(self.n_ids)) + u32::from(hi)
    }

    /// Exact total for the two values together, or `None` when the pair was pruned or the dimension is
    /// not covered.
    fn get(&self, a: u16, b: u16, mode: Mode) -> Option<usize> {
        self.pairs.get(&self.key(a, b).into()).map(|t| t.get(mode))
    }
}

/// Exact 3-space totals per value, in one pass over printings.
///
/// `keys_of` yields every value the printing belongs to — one for a scalar dimension like border,
/// several for a collection like `frame_data`, one per format for legality. Distinct cards and
/// artworks are deduped with a per-value stamp rather than a bitmap: a card's printings are contiguous
/// in pid order, so `last_card` catches card repeats, and a stamp of `cid + 1` per artwork group
/// catches artwork repeats WITHIN a card (which need not be contiguous). Both are O(values) memory.
fn build_value_totals<K: Eq + std::hash::Hash>(
    cards: &[OracleCard],
    printings: &[Printing],
    printing_to_card: &[u32],
    max_artwork_groups: usize,
    keys_of: impl Fn(&OracleCard, &Printing) -> Vec<K>,
) -> HashMap<K, SpaceTotals> {
    struct Acc {
        totals: SpaceTotals,
        last_card: u32,
        /// `stamps[group] == cid + 1` iff this card's artwork group was already counted. `cid + 1`
        /// rather than `cid` so that 0 means "never seen", and no earlier card can leave a stamp a
        /// later card reads as its own.
        stamps: Vec<u32>,
    }
    let mut acc: HashMap<K, Acc> = HashMap::new();
    for (pid, printing) in printings.iter().enumerate() {
        let cid = printing_to_card[pid];
        let card = &cards[cid as usize];
        let group = usize::from(printing.artwork_group_id);
        for key in keys_of(card, printing) {
            let e = acc.entry(key).or_insert_with(|| Acc {
                totals: SpaceTotals::default(),
                last_card: u32::MAX,
                stamps: vec![0; max_artwork_groups + 1],
            });
            e.totals.printings += 1;
            if e.last_card != cid {
                e.last_card = cid;
                e.totals.cards += 1;
            }
            if e.stamps[group] != cid + 1 {
                e.stamps[group] = cid + 1;
                e.totals.artworks += 1;
            }
        }
    }
    acc.into_iter().map(|(k, a)| (k, a.totals)).collect()
}

/// Build the pair table: one counting pass to find the dense values, one accumulating pass over their
/// co-occurrences.
///
/// The accumulator is a DENSE `n_ids x n_ids` array rather than the sparse map it is archived as,
/// because the inner loop runs once per unordered pair of ids on a printing — about 325 with legality's
/// 23 statuses in play, or ~32M over the corpus — and a hash lookup per increment would dominate the
/// store build. Only the non-zero cells are archived.
fn build_pair_totals(
    cards: &[OracleCard],
    printings: &[Printing],
    printing_to_card: &[u32],
    strings: &[String],
    coll_vocab: &[String],
    max_artwork_groups: usize,
) -> PairTotals {
    // Pass 1: per-value printing counts, to apply the selectivity floor.
    let (mut border_n, mut rarity_n, mut frame_n, mut legality_n) =
        (HashMap::new(), HashMap::new(), HashMap::new(), HashMap::new());
    let shifts: Vec<u8> = (0..MAX_FORMATS as u8).map(|i| i * 2).collect();
    for (pid, p) in printings.iter().enumerate() {
        let card = &cards[printing_to_card[pid] as usize];
        if p.card_border_id != NONE_STR {
            *border_n.entry(strings[p.card_border_id as usize].clone()).or_insert(0usize) += 1;
        }
        if let Some(r) = p.card_rarity_int {
            *rarity_n.entry(r).or_insert(0usize) += 1;
        }
        for v in &p.card_frame_data {
            *frame_n.entry(coll_vocab[*v as usize].clone()).or_insert(0usize) += 1;
        }
        let word = if card.legality_divergent { p.card_legalities } else { card.card_legalities };
        for &shift in &shifts {
            let status = (word >> shift) & 0b11;
            // `banned`/`restricted` total ~7,000 printing-rows across every format, so those queries are
            // already tiny and an estimate for them cannot cost routing time. Only legal/not_legal pair.
            if status == LEGALITY_LEGAL || status == 0 {
                *legality_n.entry(legality_totals_key(shift, status)).or_insert(0usize) += 1;
            }
        }
    }

    // Assign compact ids to the survivors, one id space across all four dimensions.
    let mut out = PairTotals::default();
    let mut next = 0u16;
    let assign = |n: usize, next: &mut u16| -> Option<u16> {
        (n >= PAIR_MIN_PRINTINGS).then(|| {
            let id = *next;
            *next += 1;
            id
        })
    };
    let mut border_sorted: Vec<_> = border_n.into_iter().collect();
    border_sorted.sort_unstable();
    for (v, n) in border_sorted {
        if let Some(id) = assign(n, &mut next) {
            out.border.insert(v, id);
        }
    }
    let mut rarity_sorted: Vec<_> = rarity_n.into_iter().collect();
    rarity_sorted.sort_unstable();
    for (v, n) in rarity_sorted {
        if let Some(id) = assign(n, &mut next) {
            out.rarity.insert(v, id);
        }
    }
    let mut frame_sorted: Vec<_> = frame_n.into_iter().collect();
    frame_sorted.sort_unstable();
    for (v, n) in frame_sorted {
        if let Some(id) = assign(n, &mut next) {
            out.frame.insert(v, id);
        }
    }
    let mut legality_sorted: Vec<_> = legality_n.into_iter().collect();
    legality_sorted.sort_unstable();
    for (v, n) in legality_sorted {
        if let Some(id) = assign(n, &mut next) {
            out.legality.insert(v, id);
        }
    }
    out.n_ids = next;
    let n = usize::from(next);
    if n == 0 {
        return out;
    }

    // Pass 2: co-occurrence. Same dedup shape as `build_value_totals` -- `last_card` catches card
    // repeats (a card's printings are contiguous), a `cid + 1` stamp per artwork group catches artwork
    // repeats within a card -- just held per PAIR instead of per value.
    let groups = max_artwork_groups + 1;
    let mut totals = vec![SpaceTotals::default(); n * n];
    let mut last_card = vec![u32::MAX; n * n];
    let mut stamps = vec![0u32; n * n * groups];
    let mut ids: Vec<u16> = Vec::with_capacity(32);
    for (pid, p) in printings.iter().enumerate() {
        let cid = printing_to_card[pid];
        let card = &cards[cid as usize];
        let group = usize::from(p.artwork_group_id);
        ids.clear();
        if p.card_border_id != NONE_STR
            && let Some(&id) = out.border.get(strings[p.card_border_id as usize].as_str())
        {
            ids.push(id);
        }
        if let Some(r) = p.card_rarity_int
            && let Some(&id) = out.rarity.get(&r)
        {
            ids.push(id);
        }
        for v in &p.card_frame_data {
            if let Some(&id) = out.frame.get(coll_vocab[*v as usize].as_str()) {
                ids.push(id);
            }
        }
        let word = if card.legality_divergent { p.card_legalities } else { card.card_legalities };
        for &shift in &shifts {
            let status = (word >> shift) & 0b11;
            if (status == LEGALITY_LEGAL || status == 0)
                && let Some(&id) = out.legality.get(&legality_totals_key(shift, status))
            {
                ids.push(id);
            }
        }
        for (i, &a) in ids.iter().enumerate() {
            for &b in &ids[i + 1..] {
                let (lo, hi) = if a <= b { (a, b) } else { (b, a) };
                let slot = usize::from(lo) * n + usize::from(hi);
                let e = &mut totals[slot];
                e.printings += 1;
                if last_card[slot] != cid {
                    last_card[slot] = cid;
                    e.cards += 1;
                }
                let stamp = &mut stamps[slot * groups + group];
                if *stamp != cid + 1 {
                    *stamp = cid + 1;
                    e.artworks += 1;
                }
            }
        }
    }
    // EVERY pair of stored ids is archived, including the zero ones, so that "both ids present" means
    // the answer is exact -- possibly exactly zero. Storing only non-zero cells made a provably-empty
    // pair indistinguishable from a pruned one, and `frame:2003 frame:1997` (no printing carries both era
    // frames, though the field is multi-valued so nothing rules it out a priori) fell back to
    // min-over-singles and read 10,769 against a true 0.
    //
    // Nearly free: the zero cells are the minority, and the floor already bounded `n_ids`.
    for lo in 0..n {
        for hi in lo + 1..n {
            out.pairs.insert((lo * n + hi) as u32, totals[lo * n + hi]);
        }
    }
    out
}

/// Build all four `ValueTotals` maps. One call site, so the four cannot be built from different
/// snapshots of the same printings.
fn build_all_value_totals(
    cards: &[OracleCard],
    printings: &[Printing],
    printing_to_card: &[u32],
    strings: &[String],
    coll_vocab: &[String],
    max_artwork_groups: usize,
) -> ValueTotals {
    // A macro rather than a closure alias: each `keys_of` is a distinct closure type returning a
    // distinct key type, so one generic-over-both helper binding cannot serve all four.
    macro_rules! totals {
        ($keys_of:expr) => {
            build_value_totals(cards, printings, printing_to_card, max_artwork_groups, $keys_of)
        };
    }
    // ALL 32 format slots, not just the ones this archive assigned. Restricting to the registry
    // snapshot leaves the table silently under-populated wherever the snapshot is empty (the fuzz store
    // is), and absence from this table is read as an exact zero — so a missing key is a WRONG total, not
    // a missing one. Covering every slot costs 32 x 4 statuses x 12 bytes = 1.5 KB and cannot be
    // under-populated. The entries for unassigned slots are correct rather than merely harmless: an
    // unassigned format reads as `not_legal` for every card, which is what those entries say.
    let shifts: Vec<u8> = (0..MAX_FORMATS as u8).map(|i| i * 2).collect();
    ValueTotals {
        border: totals!(|_card: &OracleCard, p: &Printing| match p.card_border_id {
            NONE_STR => Vec::new(),
            id => vec![strings[id as usize].clone()],
        }),
        layout: totals!(|card: &OracleCard, _p: &Printing| match card.card_layout_id {
            NONE_STR => Vec::new(),
            id => vec![strings[id as usize].clone()],
        }),
        frame_data: totals!(|_card: &OracleCard, p: &Printing| {
            p.card_frame_data.iter().map(|v| coll_vocab[*v as usize].clone()).collect()
        }),
        legality: totals!(|card: &OracleCard, p: &Printing| {
            let word = if card.legality_divergent { p.card_legalities } else { card.card_legalities };
            shifts.iter().map(|&shift| legality_totals_key(shift, (word >> shift) & 0b11)).collect()
        }),
    }
}

// ─── Printing-space value-major indexes ──────────────────────────────────────
// One layout for every printing-space ordering: released_at / price_usd /
// collector_number (range filters, plus the `usd` orderby walk) and
// rarity_printing_ordered (the `rarity` orderby walk). Printings without the
// value are absent — they can never satisfy a comparison (SQL NULL semantics)
// and they sort last, which the walk handles by declining. Dates store yyyymmdd
// directly; collector numbers store the extracted int; prices store raw integer
// cents directly (see Printing::price_usd's doc comment) — no f32_sort_bits
// encoding needed, cents are already a natural, monotonic u32; rarity stores
// the rarity int.

/// Value-major: `keys` holds each DISTINCT key once, ascending, and `pids[starts[i]..starts[i+1]]`
/// holds key `keys[i]`'s printings **in `page_cmp` tiebreak order**.
///
/// Two properties fall out of that, and both are load-bearing:
///
/// - A value range `[lo, hi)` is still ONE contiguous `pids` slice, found by two `partition_point`s
///   over `keys` — 4,133 entries for price rather than 81,542 pairs — so every filter consumer keeps
///   the shape it had over the old `Vec<(value, pid)>`, only cheaper to search and 45% smaller (the
///   value stops being repeated once per printing).
/// - An `orderby` walk emits rows *directly*. `page_cmp` orders on (primary, edhrec_rank, cid, pid)
///   and every printing in a run shares the primary, so a run pre-sorted on the rest already IS page
///   order: the walk stops the instant the page fills instead of collecting whole runs and sorting
///   them. Before this, a 60-row `orderby=rarity` ascending page collected 24,653 matches.
///
/// The tiebreak is direction-independent, which is why one index serves both directions:
/// `sort_key_bits` negates only the PRIMARY under `desc`, and `page_cmp` drops key 3
/// (`prefer_score`) entirely, so within a key the order is (edhrec_rank, cid, pid) ascending either
/// way. Descending reads `keys` backwards and each run forwards. No mirror index, no reversal at
/// query time.
#[derive(Archive, Serialize, Deserialize, Default)]
struct PrintingValueIndex {
    /// Each distinct key, ascending. One entry per VALUE, not per printing.
    keys: Vec<u32>,
    /// `keys.len() + 1` entries: key `i` owns `pids[starts[i]..starts[i + 1]]`. The trailing
    /// sentinel is `pids.len()`, which makes `starts[i]` also the offset of the first entry whose
    /// key is `>= keys[i]` — exactly what a `[lo, hi)` lookup wants, with no
    /// `get(i + 1).unwrap_or(len)` at every use site.
    starts: Vec<u32>,
    /// Key-major printing ids, tiebreak order within each key.
    pids: Vec<u32>,
}

impl ArchivedPrintingValueIndex {
    /// Indexed printings, NOT distinct keys — the denominator `range_too_broad_to_narrow` compares
    /// against, and what `Vec<(value, pid)>::len()` used to mean.
    fn len(&self) -> usize {
        self.pids.len()
    }

    /// Offset into `pids` of the first entry whose key is `>= v`. An unbuilt (`Default`) index has
    /// no sentinel at all, so the `get` also covers that case, reporting an empty index.
    fn offset_of(&self, v: u32) -> usize {
        let i = self.keys.partition_point(|k| u32::from(*k) < v);
        self.starts.get(i).map_or(self.pids.len(), |o| u32::from(*o) as usize)
    }

    /// The half-open value range `[lo, hi)` as a `pids` offset pair — the `(s, e)` every filter
    /// consumer used to get from two `partition_point`s over the pair vec.
    fn range(&self, lo: u32, hi: u32) -> (usize, usize) {
        (self.offset_of(lo), self.offset_of(hi))
    }

    /// Printing ids whose key is in `[lo, hi)`, key-major.
    fn range_pids(&self, lo: u32, hi: u32) -> impl Iterator<Item = u32> + '_ {
        let (s, e) = self.range(lo, hi);
        self.pids[s..e].iter().map(|p| u32::from(*p))
    }

    /// `pids` offsets of key index `i`'s run. Callers hold `i < keys.len()`, where the sentinel
    /// guarantees `starts[i + 1]`.
    fn run(&self, i: usize) -> std::ops::Range<usize> {
        u32::from(self.starts[i]) as usize..u32::from(self.starts[i + 1]) as usize
    }

    /// The printing id at a `pids` offset.
    fn pid_at(&self, t: usize) -> usize {
        u32::from(self.pids[t]) as usize
    }
}

/// Exact distinct-CARD counts alongside a `PrintingValueIndex`, so a range acquire can report the
/// real answer instead of estimating it.
///
/// The index is printing-space and value-sorted, but `unique=card` costing needs distinct *cards*,
/// and the two differ by the local printing:card ratio — measured 1.0 to 4.3 across the corpus, worst
/// where reprint density is highest. `CardRangePopcount`'s acquire has stood in `k.min(n_cards)` (the
/// in-range printing count clamped), which over-estimates a median 1.49x and up to 4.33x. See
/// docs/issues/done/local-engine-range-cardinality-estimate.md for the estimators that were tried and why
/// none of them work: distinct-card counts do not compose by arithmetic, because one card spans many
/// values, so nothing derived from a coarser summary is exact.
///
/// The trick that makes an exact table affordable is that these dimensions have far fewer distinct
/// VALUES than printings — 914 release dates against 97,206 printings, 4,133 usd prices. Printings
/// sharing a value are contiguous, so any threshold, present in the data or not, bisects to a value
/// boundary; there is no interpolation to do. One entry per distinct value is enough, and all three
/// counts come out of a single build pass.
///
/// None of the three derives from the others — each was measured failing:
/// - `suf[i] != total - pre[i]`: a card with printings on both sides of the cut is in both.
/// - `val[i] != pre[i+1] - pre[i]`: that counts cards whose FIRST printing is at this value, not
///   cards present at it (10 against a true 54 at `usd:2.99`).
#[derive(Archive, Serialize, Deserialize, Default)]
struct RangeCardCounts {
    /// Each distinct value in the index, ascending. Parallel to the three count vectors.
    ///
    /// Byte-for-byte `PrintingValueIndex::keys` since the index went value-major, so this is ~16 KB
    /// of duplication per dimension. Kept because `distinct_cards` is reached through
    /// `range_card_counts_for` and answering from the index's own keys would mean threading the index
    /// into it; both call sites do hold one, so this is a deliberate deferral, not an oversight. The
    /// two are built from the same pass and cannot drift.
    values: Vec<u32>,
    /// Distinct cards among printings with value < `values[i]`. Serves `<` and `<=`.
    below: Vec<u32>,
    /// Distinct cards among printings with value >= `values[i]`. Serves `>` and `>=`.
    at_or_above: Vec<u32>,
    /// Distinct cards among printings with value == `values[i]`. Serves `Eq`, which cannot be had by
    /// subtracting neighbouring `below` entries.
    at: Vec<u32>,
    /// The same three aggregates over distinct ARTWORKS.
    ///
    /// The third space, added because it was the only one estimated for every range: printings come
    /// free as `k = e - s` and cards from the triple above, while artwork totals went through
    /// `printing_bits_to_artwork_bits` and were read as 0.80-0.87 of the truth. Filled in the SAME pass
    /// as the card triple, over a parallel artwork-seen bitmap, so the two cannot drift.
    ///
    /// Costs a measured **156.6 KB** of archive across the five range dimensions (12 bytes per distinct
    /// value, ~13,400 values), or 0.22% — the honest price of the exactness, and more than the 133 KB
    /// `frame_data`'s hybrid gave back.
    below_artworks: Vec<u32>,
    at_or_above_artworks: Vec<u32>,
    at_artworks: Vec<u32>,
}

impl ArchivedRangeCardCounts {
    /// Exact distinct cards for the half-open value range `[lo, hi)`, or `None` when the shape is one
    /// this table cannot answer.
    ///
    /// Answerable: `[0, hi)` and `[lo, MAX)` — every op except `Eq` produces one of those — plus a
    /// range covering exactly one distinct value, which is what `Eq` produces. A range spanning
    /// several values (only `year:Y`, which covers a whole calendar year of release dates) returns
    /// `None`; distinct counts do not subtract, so the neighbouring entries cannot be combined.
    fn distinct_cards(&self, lo: u32, hi: u32) -> Option<u32> {
        self.lookup(lo, hi, &self.below, &self.at_or_above, &self.at)
    }

    /// Exact distinct ARTWORKS for `[lo, hi)`, on the same answerable shapes as `distinct_cards`.
    fn distinct_artworks(&self, lo: u32, hi: u32) -> Option<u32> {
        self.lookup(lo, hi, &self.below_artworks, &self.at_or_above_artworks, &self.at_artworks)
    }

    /// The shape both spaces share: which of the three aggregates a `[lo, hi)` reduces to, or `None`
    /// where distinct counts cannot be combined. Written once so the two spaces cannot answer
    /// differently-shaped questions.
    fn lookup(
        &self,
        lo: u32,
        hi: u32,
        below: &Archived<Vec<u32>>,
        at_or_above: &Archived<Vec<u32>>,
        at: &Archived<Vec<u32>>,
    ) -> Option<u32> {
        if self.values.is_empty() || hi <= lo || below.is_empty() {
            return None;
        }
        let pos = |v: u32| self.values.partition_point(|x| u32::from(*x) < v);
        let (i, j) = (pos(lo), pos(hi));
        if j <= i {
            return Some(0); // no indexed value falls in the range
        }
        let first = u32::from(self.values[0]);
        let last_covers_end = j == self.values.len();
        match (lo <= first, last_covers_end) {
            (true, true) => Some(u32::from(at_or_above[0])), // whole index
            (true, false) => Some(u32::from(below[j])),      // `<` / `<=`
            (false, true) => Some(u32::from(at_or_above[i])), // `>` / `>=`
            // Interior range: exact only when it holds a single distinct value, which is `Eq`.
            (false, false) if j == i + 1 => Some(u32::from(at[i])),
            _ => None,
        }
    }
}

/// Build the three count vectors for one range index. O(n) over the index plus one card-seen bitmap
/// per direction, so two passes; the index is value-major, so the value blocks are already delimited
/// by `starts` and no boundary scan is needed.
fn build_range_card_counts(
    idx: &PrintingValueIndex,
    printing_to_card: &[u32],
    n_cards: usize,
    // pid -> the printing's artwork group within its card, and card -> its first global artwork id.
    // Together these give a printing's GLOBAL artwork id, the same derivation
    // `printing_bits_to_artwork_bits` uses.
    printings: &[Printing],
    artwork_base: &[u32],
) -> RangeCardCounts {
    let mut out = RangeCardCounts::default();
    if idx.pids.is_empty() {
        return out;
    }
    let n_values = idx.keys.len();
    let n_artworks = artwork_base.last().copied().unwrap_or(0) as usize;
    let run = |b: usize| idx.starts[b] as usize..idx.starts[b + 1] as usize;
    out.values = idx.keys.clone();
    // (card id, global artwork id) for a printing. One closure so the two spaces are derived from the
    // same pid in the same place.
    let ids = |pid: u32| {
        let cid = printing_to_card[pid as usize] as usize;
        let aid = artwork_base[cid] as usize + printings[pid as usize].artwork_group_id as usize;
        (cid, aid)
    };

    // Two spaces, two domains, one pass over the index. A set-bit test per space per printing is
    // cheaper than walking the index twice, and it keeps the card and artwork columns in lockstep.
    let mut seen_c = vec![0u64; n_cards.div_ceil(64)];
    let mut seen_a = vec![0u64; n_artworks.div_ceil(64)];
    let (mut nc, mut na) = (0u32, 0u32);
    let bump = |seen: &mut [u64], n: &mut u32, id: usize| {
        let (w, bit) = (id >> 6, 1u64 << (id & 63));
        if seen[w] & bit == 0 {
            seen[w] |= bit;
            *n += 1;
        }
    };
    // Forward: `below[i]` is the running distinct count before this value's block begins.
    for b in 0..n_values {
        out.below.push(nc);
        out.below_artworks.push(na);
        for &pid in &idx.pids[run(b)] {
            let (cid, aid) = ids(pid);
            bump(&mut seen_c, &mut nc, cid);
            bump(&mut seen_a, &mut na, aid);
        }
    }
    // Backward for `at_or_above`, and per-block for `at` — both need their own fresh bitmap, since an
    // id counted in one block must still count in another.
    seen_c.fill(0);
    seen_a.fill(0);
    nc = 0;
    na = 0;
    out.at_or_above = vec![0; n_values];
    out.at = vec![0; n_values];
    out.at_or_above_artworks = vec![0; n_values];
    out.at_artworks = vec![0; n_values];
    // Scratch bitmaps reused across blocks, cleared by walking back over the ids this block actually
    // touched — a `fill(0)` per block would be O(domain) each, and there are as many blocks as
    // distinct values.
    let mut block_c = vec![0u64; n_cards.div_ceil(64)];
    let mut block_a = vec![0u64; n_artworks.div_ceil(64)];
    let (mut touched_c, mut touched_a): (Vec<usize>, Vec<usize>) = (Vec::new(), Vec::new());
    for b in (0..n_values).rev() {
        touched_c.clear();
        touched_a.clear();
        for &pid in &idx.pids[run(b)] {
            let (cid, aid) = ids(pid);
            bump(&mut seen_c, &mut nc, cid);
            bump(&mut seen_a, &mut na, aid);
            for (blk, touched, id) in [(&mut block_c, &mut touched_c, cid), (&mut block_a, &mut touched_a, aid)] {
                let (w, bit) = (id >> 6, 1u64 << (id & 63));
                if blk[w] & bit == 0 {
                    blk[w] |= bit;
                    touched.push(id);
                }
            }
        }
        out.at_or_above[b] = nc;
        out.at[b] = touched_c.len() as u32;
        out.at_or_above_artworks[b] = na;
        out.at_artworks[b] = touched_a.len() as u32;
        for (blk, touched) in [(&mut block_c, &touched_c), (&mut block_a, &touched_a)] {
            for &id in touched.iter() {
                blk[id >> 6] &= !(1u64 << (id & 63));
            }
        }
    }
    out
}

/// One-shot env override for the guard statics below: reads
/// `CARD_ENGINE_<NAME>` once (each static is a LazyLock), falling back to the
/// measured default when the var is unset or unparseable. Production leaves
/// the vars unset; the calibration harness (scripts/bench_cost_guards.py)
/// sets them in fresh subprocesses to force one branch of each guard.
fn guard_env<T: std::str::FromStr>(name: &str, default: T) -> T {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

// Printing-space range narrowing is a pessimization when the matched slice
// covers too much of the index: gathering + sorting the candidate ids and
// evaluating them by random access costs ~2× per element what the sequential
// full scan pays, and unlike the card-space indexes the candidate set doesn't
// shrink the eval domain. Past the fraction below the indexes decline to
// narrow and the query falls back to the scan. Narrowing is advisory (eval
// verifies every candidate), so this is purely a speed dial, not a
// correctness concern. Calibrated (scripts/bench_cost_guards.py, forced-branch
// sweeps on an exact-selectivity synthetic corpus): crossover at 0.33 ± 0.01
// of the index on the 97k-printing corpus but 0.28 ± 0.01 at half size, so
// 0.25 is the most aggressive trigger clear of the pooled spread (narrowing
// still wins ~1.06-1.15× there).
static MAX_NARROW_FRACTION: LazyLock<f64> = LazyLock::new(|| guard_env("CARD_ENGINE_MAX_NARROW_FRACTION", 0.25));

/// A different crossover from `MAX_NARROW_FRACTION` above, for a different decision: whether
/// `printing_compose_fastpath`'s permutation-free `gather_composed_page` fallback is worth its
/// build cost (see that function's doc). `MAX_NARROW_FRACTION` answers "does narrowing shrink the
/// candidate set enough to beat a full scan" — this answers "does skipping per-candidate residual
/// re-verification (compose's whole benefit here, since the fallback still visits every mode-space
/// candidate regardless of build) beat the cost of building the composed bitmap at all." Those are
/// different tradeoffs with different crossovers: calibrated directly (`usd<N` swept 15%-98% of
/// *cards* — the domain this must be checked in, not raw matching printings, see the call site —
/// card/rarity, interleaved A/B compose on/off), the gather fallback wins clearly below ~82% and
/// loses above ~93%, both noisy in between; 0.85 sits inside that band, erring toward the general
/// path (this engine's default lean whenever a crossover region is uncertain, same reasoning
/// `MAX_NARROW_FRACTION`'s own calibration used).
static COMPOSE_GATHER_MAX_CARD_FRACTION: LazyLock<f64> = LazyLock::new(|| guard_env("CARD_ENGINE_COMPOSE_GATHER_MAX_CARD_FRACTION", 0.85));

/// Below this many matched ids narrowing always wins regardless of fraction —
/// gathering a handful of ids is microseconds. Also keeps tiny stores (tests,
/// partial imports) narrowing, where any match trips the fraction. Not
/// measurable on the calibration corpus (1k ids is ~1% of the index, far
/// below the fraction crossover); it only binds on stores small enough that
/// any answer is microseconds.
static NARROW_FLOOR: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_NARROW_FLOOR", 1_000));

fn range_too_broad_to_narrow(matched: usize, index_len: usize) -> bool {
    matched > *NARROW_FLOOR && matched as f64 > index_len as f64 * *MAX_NARROW_FRACTION
}

/// Build one value-major index: every printing `get` yields a value for, grouped by distinct value
/// and ordered *within* a value by `page_cmp`'s tiebreak. `cards`/`offsets` are needed only for that
/// tiebreak, which reads a card-level field.
///
/// The tiebreak is `(edhrec_rank, cid, pid)` ascending with a missing rank last — `page_cmp`'s order
/// below the primary, minus key 3 (`prefer_score`), which `page_cmp` deliberately drops so the two
/// sides of a filter can agree. `cid` then falls out for free: printings are stored card-major, so
/// ascending `pid` already IS ascending `(cid, pid)`. That leaves a two-field sort key.
///
/// A store with no cards (a fixture that indexes bare printings) gets a uniform `u32::MAX` rank, so
/// each run degrades to pid order. That is what the old pair-vec layout produced, and only row ORDER
/// depends on it — never membership — so such a fixture still exercises every filter path.
fn build_printing_value_index(
    printings: &[Printing],
    cards: &[OracleCard],
    offsets: &[u32],
    get: impl Fn(&Printing) -> Option<u32>,
) -> PrintingValueIndex {
    let printing_to_card = build_printing_to_card(offsets);
    let tiebreak = |pid: usize| -> u32 {
        printing_to_card
            .get(pid)
            .and_then(|&cid| cards.get(cid as usize))
            .and_then(|c| c.edhrec_rank)
            .unwrap_or(u32::MAX)
    };
    // (key, tiebreak rank, pid) — one `sort_unstable` on the lexicographic tuple establishes both
    // the value-major grouping and the within-value order at once.
    let mut entries: Vec<(u32, u32, u32)> = printings
        .iter()
        .enumerate()
        .filter_map(|(i, p)| get(p).map(|v| (v, tiebreak(i), i as u32)))
        .collect();
    entries.sort_unstable();
    let mut out = PrintingValueIndex { pids: Vec::with_capacity(entries.len()), ..Default::default() };
    for (key, _, pid) in entries {
        if out.keys.last() != Some(&key) {
            out.keys.push(key);
            out.starts.push(out.pids.len() as u32);
        }
        out.pids.push(pid);
    }
    // The sentinel that makes `starts[i]` a lower bound for every `i`, including one past the end.
    out.starts.push(out.pids.len() as u32);
    out
}

/// Cents per dollar for price fields, now stored as integer cents (see Printing::price_usd's
/// doc comment) rather than lossy f32 dollars.
const PRICE_CENTS_PER_DOLLAR: f64 = 100.0;

/// `value * PRICE_CENTS_PER_DOLLAR` is itself a new floating-point operation, not a lossless
/// relabeling -- for roughly a quarter of two-decimal dollar amounts it lands just off the
/// intended integer (`0.28_f64 * 100.0 == 28.000000000000004`), which would silently shift
/// int_range_bounds' floor/ceil by a whole cent. The error is on the order of 1e-10 to 1e-15
/// (`5142.02_f64 * 100.0 == 514202.00000000006`, the real max price), so 1e-6 has enormous
/// margin over the noise while staying far below the smallest gap between a genuinely off-grid
/// threshold and its nearest real cent value (checked empirically down to 0.005 in
/// price_bounds_matches_direct_comparison_on_and_off_grid).
fn snap_to_nearest_cent(cents: f64) -> f64 {
    let rounded = cents.round();
    if (cents - rounded).abs() < 1e-6 { rounded } else { cents }
}

/// Half-open [lo, hi) bounds for indexes over plain integers (collector
/// number). Query values are f64 and may be fractional or out of range; bounds
/// are chosen so the range is exact for every op — `cn<100.5` means
/// value <= 100. Outer None = Ne (never narrows); inner None = provably empty
/// (an exact empty narrowing, not "no index").
fn int_range_bounds(op: CmpOp, value: f64) -> Option<Option<(u32, u32)>> {
    const TOP: i64 = u32::MAX as i64;
    let (lo, hi): (i64, i64) = match op {
        CmpOp::Ne => return None,
        CmpOp::Eq => {
            if value.fract() != 0.0 || value < 0.0 || value > TOP as f64 {
                return Some(None);
            }
            (value as i64, value as i64 + 1)
        }
        CmpOp::Lt => (0, value.ceil().clamp(0.0, TOP as f64) as i64),
        CmpOp::Le => (0, value.floor().clamp(-1.0, TOP as f64) as i64 + 1),
        CmpOp::Gt => (value.floor().clamp(-1.0, TOP as f64) as i64 + 1, TOP),
        CmpOp::Ge => (value.ceil().clamp(0.0, TOP as f64) as i64, TOP),
    };
    if hi <= lo {
        return Some(None);
    }
    Some(Some((lo as u32, hi as u32)))
}

/// Half-open [lo, hi) bounds over the packed released-at integer for a date
/// comparison. None = Ne (never narrows). Shared by narrow_rec's `DateCmp` arm
/// and the printing-range fastpath so the two never drift (see [`bare_range_bounds`]).
fn date_range_bounds(op: CmpOp, value: u32) -> Option<(u32, u32)> {
    Some(match op {
        CmpOp::Ne => return None,
        CmpOp::Eq => (value, value.saturating_add(1)),
        CmpOp::Lt => (0, value),
        CmpOp::Le => (0, value.saturating_add(1)),
        CmpOp::Gt => (value.saturating_add(1), u32::MAX),
        CmpOp::Ge => (value, u32::MAX),
    })
}

/// Half-open [lo, hi) bounds over the packed released-at integer for a year
/// comparison (`released_at` packs as `year*10000 + ...`). None = Ne or an
/// out-of-range year (never narrows). Shared like [`date_range_bounds`].
fn year_range_bounds(op: CmpOp, year: i32) -> Option<(u32, u32)> {
    if !(0..=9999).contains(&year) {
        return None;
    }
    let y = year as u32;
    Some(match op {
        CmpOp::Ne => return None,
        CmpOp::Eq => (y * 10_000, (y + 1) * 10_000),
        CmpOp::Lt => (0, y * 10_000),
        CmpOp::Le => (0, (y + 1) * 10_000),
        CmpOp::Gt => ((y + 1) * 10_000, u32::MAX),
        CmpOp::Ge => (y * 10_000, u32::MAX),
    })
}

/// Sorted printing ids with an indexed value in [lo, hi), or None for ranges
/// too broad to be worth narrowing (see MAX_NARROW_FRACTION). Test-only
/// reference for the sparse path range_narrowed() shares.
#[cfg(test)]
fn range_candidates(idx: &Archived<PrintingValueIndex>, lo: u32, hi: u32) -> Option<Vec<u32>> {
    let (s, e) = idx.range(lo, hi);
    if range_too_broad_to_narrow(e - s, idx.len()) {
        return None;
    }
    let mut result: Vec<u32> = idx.range_pids(lo, hi).collect();
    result.sort_unstable();
    Some(result)
}

/// Range narrowing that never declines (#636): sparse ranges keep the sorted-vec
/// path above; broad ranges become printing bitmaps instead of vetoing. A range
/// predicate selects a contiguous slice of the value-sorted postings, so the
/// bitmap is an O(k) scatter of whichever side is smaller — the broad slice is
/// represented as the complement of its sparse opposite without ever touching
/// its members (the gather-and-sort cost #609 measured never happens). The
/// complement over-includes unindexed printings (value NULL there), so that
/// variant is loose; direct scatters and the vec path are tight.
/// `exact` says whether [lo, hi) is the predicate's exact extent: integer
/// bounds (date/year/collector number) are; price bounds are deliberately
/// widened one position for f32/f64 rounding (see price_bounds) and therefore
/// produce supersets that must never be marked tight — a Not would complement
/// away the boundary printings, which are exactly the negation's matches.
/// Where a bitmap scatter-then-extract becomes cheaper than `collect` + `sort_unstable` for producing
/// an ascending id vector, as a DOMAIN:COUNT ratio rather than a count.
///
/// A count constant is only right at one domain: the bitmap pays `domain/64` words whatever the answer
/// size is, so its fixed cost triples between card space (493 words) and printing space (1,519). The
/// crossover collapses cleanly to a ratio instead -- `bench_candidate_materialize`'s fine sweep, 12%
/// steps with 2 confirmations:
///
///        domain     words   crossover     ratio
///        31,508       493          90     350:1
///        97,206     1,519         194     501:1
///       300,000     4,688         667     450:1
///     1,000,000    15,625       2,064     484:1
///     3,000,000    46,875       6,401     469:1
///
/// So `k * 490 > domain`, which puts printing space at k > 198. Same shape as
/// `bitmap_beats_postings`'s `k * 32 > n`, but that one is a STORAGE crossover in bytes and this is a
/// materialization-time one; the two are unrelated and their constants should not be conflated.
///
/// What it is worth where it matters: at 2,048 ids the bitmap is 3.6x, at 16,384 it is 5.8x, at 31,508
/// 6.3x. A mid-band price range materializes 13,000-24,000.
const MATERIALIZE_BITMAP_RATIO: usize = 490;

/// An ascending id vector, by whichever route is cheaper at this size and domain. `ids` may arrive in
/// any order -- `range_pids` yields key-major, which is the tiebreak order, not pid order.
///
/// **Precondition: `ids` must be duplicate-free.** This is the one way the two routes differ, and it is
/// silent: a bitmap DEDUPS and a sort does not, so a caller emitting an id twice gets a shorter vec from
/// one route than the other. Every current caller satisfies it by construction -- a `PrintingValueIndex`
/// holds one entry per printing with a value, `build_numeric_index` one per card, a card lives in
/// exactly one `arith_tuple` posting row, and `expand_csr`'s rows are one dense text/artist/flavor id's
/// members, of which every card or printing has exactly one -- and the debug build checks it on every
/// call rather than trusting that list to stay true. `rarity_candidates` is the counterexample that
/// makes the check worth having: a card printed at two rarities is in both buckets, so it must not be
/// routed here (it also measures faster as a fold -- see #849).
///
/// Ids must also be `< domain`; `scatter_bits` would panic otherwise, which is the loud failure.
///
/// Same output either way given that, so this is a pure cost choice with no consumer effect. See
/// `MATERIALIZE_BITMAP_RATIO`, and docs/issues/done/local-engine-candidate-materialize.md for the k-way merge
/// that lost to both by 3-30x.
fn sorted_ids(ids: impl Iterator<Item = u32>, k: usize, domain: usize) -> Vec<u32> {
    let bitmap = *RANGE_MATERIALIZE_BITMAP && k.saturating_mul(MATERIALIZE_BITMAP_RATIO) > domain;
    // Debug builds run BOTH and compare, so every call site the fuzz suite reaches is a live check of
    // the precondition above -- not a claim in a doc comment that drifts. Release picks one.
    #[cfg(debug_assertions)]
    {
        let collected: Vec<u32> = ids.collect();
        let by_sort = {
            let mut v = collected.clone();
            v.sort_unstable();
            v
        };
        let by_bitmap = bitmap_card_ids(&scatter_bits(collected.iter().copied(), domain));
        debug_assert_eq!(
            by_sort, by_bitmap,
            "sorted_ids routes disagree over {} ids in a domain of {domain} -- the caller emitted a \
             duplicate, which the bitmap collapses and the sort keeps",
            collected.len(),
        );
        if bitmap { by_bitmap } else { by_sort }
    }
    #[cfg(not(debug_assertions))]
    if bitmap {
        bitmap_card_ids(&scatter_bits(ids, domain))
    } else {
        let mut v: Vec<u32> = ids.collect();
        v.sort_unstable();
        v
    }
}

fn range_narrowed(idx: &Archived<PrintingValueIndex>, lo: u32, hi: u32, n_printings: usize, broad_ok: bool, exact: bool) -> Option<Narrowed> {
    let (s, e) = idx.range(lo, hi);
    let k = e - s;
    // Breadth against the WALK's domain, not the index's own length. A printing-value index omits
    // null-valued printings, so `idx.len()` is the priced subset -- 54,896 of 97,206 for tix -- and
    // measuring against it overstates breadth by exactly the null rate (1.77x for tix, 1.19x for
    // usd/eur, 1.00x for the always-present date and collector-number indexes).
    //
    // The guard asks "is this set too big a fraction of what we would otherwise scan to be worth
    // materializing", and what we would otherwise scan is the corpus. `tix>=0.03 tix<=0.03` is 16,664
    // printings: 30.4% of the tix index and 17.1% of the corpus. Judged the first way it is broad, so it
    // declines under `broad_ok: false` and narrows NOTHING -- which is why
    // `eur>0.15 tix>=0.03 tix<=0.03` runs 39 us alone but 1,405 us with any plane-consumed partner, the
    // plane having taken the only leaf that could have supplied a printing-space set.
    //
    // The collection and frame arms already use `n_printings` for the same guard; the range arms were
    // the inconsistent ones.
    let domain = if *RANGE_BREADTH_VS_CORPUS { n_printings } else { idx.len() };
    if !range_too_broad_to_narrow(k, domain) {
        // The run order is the sort-key tiebreak, not pid, and `Candidates::Printings` is contractually
        // pid-ascending -- so the ids have to be ordered somehow. `sorted_ids` picks the cheaper of the
        // two ways: at this call site `k` reaches 20,000+ on a mid-band price range, where the sort cost
        // 157 us of a 166 us query while the scatter costs ~25.
        let result = sorted_ids(idx.range_pids(lo, hi), k, n_printings);
        return Some(Narrowed { set: Candidates::Printings(result), tight: exact, proven: 0 });
    }
    if !broad_ok {
        return None; // nothing downstream would consume the bitmap — pre-#636 behavior
    }
    if k <= idx.len() - k {
        let bits = scatter_bits(idx.range_pids(lo, hi), n_printings);
        return Some(Narrowed { set: Candidates::PrintingBits(bits), tight: exact, proven: 0 });
    }
    let mut bits = scatter_bits(
        idx.pids[..s].iter().chain(idx.pids[e..].iter()).map(|p| u32::from(*p)),
        n_printings,
    );
    complement_bits(&mut bits, n_printings);
    Narrowed::loose(Candidates::PrintingBits(bits))
}

// ─── Rarity index ────────────────────────────────────────────────────────────
// rarity int (0-5) -> sorted card ids with at least one printing at that
// rarity. A card printed at several rarities appears in each of its lists
// (~34.8k entries over ~31.5k cards; 91% of cards have a single rarity).
// Card space deliberately: the per-rarity card lists shrink the evaluation
// domain, so even the broadest bucket (rare, ~35% of cards) measures ahead of
// the scan. Near-total unions still lose — see MAX_UNION_FRACTION.

type RarityIndex = [Vec<u32>; 6];

fn build_rarity_index(printings: &[Printing], offsets: &[u32]) -> RarityIndex {
    let mut idx: RarityIndex = Default::default();
    for card in 0..offsets.len().saturating_sub(1) {
        let range = offsets[card] as usize..offsets[card + 1] as usize;
        let mut mask: u8 = 0;
        for p in &printings[range] {
            if let Some(r) = p.card_rarity_int
                && (r as usize) < idx.len()
            {
                mask |= 1 << r;
            }
        }
        let mut bits = mask;
        while bits != 0 {
            let bit = bits.trailing_zeros() as usize;
            idx[bit].push(card as u32);
            bits &= bits - 1;
        }
    }
    idx // lists are sorted: cards iterated in ascending index order
}

/// Ceiling for union-based card-space narrowing, as a fraction of the index's
/// total posting entries. The card-space range indexes need no guard (their
/// slice is a free contiguous window over an always-smaller domain), but a
/// posting union pays a gather-and-merge per bucket, and at near-total
/// coverage that buys nothing: measured on the live corpus with the default
/// prefer, `rarity<=mythic` (99% of entries) ran 0.85× the scan while
/// `rarity>=uncommon` (69%) won 1.44× — break-even ≈ 90%. Non-default
/// prefers compress the win (the same 69% union wins only 1.10× under
/// prefer=usd_high, extrapolating to break-even ≈ 72–75%), so the ceiling
/// sits below the worst prefer's crossover, per the usual asymmetry argument
/// (declining early forgoes a small win, declining late pays on every
/// query). For rarity this is not restrictive: no bucket combination covers
/// between 69% and 91% of entries, so any ceiling in that band admits the
/// same unions.
const MAX_UNION_FRACTION: f64 = 0.70;

/// Union the rarity posting lists whose value satisfies `op val`. Returns None
/// for Ne (matches nearly every card, same convention as numeric_candidates)
/// and when the qualifying buckets cover more than MAX_UNION_FRACTION of the
/// index's entries (the scan costs the same without materializing the union).
/// An empty union is exact: no printing exists at a rarity satisfying the
/// comparison.
fn rarity_candidates(idx: &Archived<RarityIndex>, op: CmpOp, val: f64) -> Option<Vec<u32>> {
    if matches!(op, CmpOp::Ne) {
        return None;
    }
    let buckets: Vec<usize> = (0..idx.len()).filter(|&r| num_cmp(op, r as f64, val)).collect();
    let total: usize = idx.iter().map(|b| b.len()).sum();
    let selected: usize = buckets.iter().map(|&b| idx[b].len()).sum();
    if selected as f64 > total as f64 * MAX_UNION_FRACTION {
        return None;
    }
    let mut result: Vec<u32> = Vec::new();
    for b in buckets {
        result = union_sorted(result, idx[b].iter().map(|x| u32::from(*x)).collect());
    }
    Some(result)
}

/// Card-space candidate mask for `rarity <op> val` using the 4 tracked
/// one-hot rarity planes (common/uncommon/rare/mythic, buckets 0-3 —
/// PLANE_RARITY, docs/issues/00670-engine-rarity-planes.md) plus the shared "above
/// mythic" plane (PLANE_RARITY_HI, special/bonus combined —
/// docs/issues/00680-engine-existential-plane-generalization.md, #680), mirroring
/// `compile_rarity_cmp`'s exact-plane construction but producing a raw
/// bitmap instead of a `PlaneExpr`. `rarity_hi_verdict` decides the hi
/// plane's fate the same way it does there: `Ambiguous` (the query needs to
/// distinguish special from bonus specifically) declines the whole plane
/// path, falling through to `rarity_candidates`'s postings, which already
/// answer those two values exactly at the same cost measured before this
/// change (docs/issues/00680-engine-existential-plane-generalization.md's
/// "Measured problem" -- their cost tracks candidate count, not narrowing
/// representation, so nothing is lost by keeping them there). Loose, same as
/// rarity_candidates: rarity is PrintingDep at card level, so this only
/// narrows candidates — card_pass/printing-level residual eval still
/// verifies which printings actually match.
fn rarity_plane_candidates(indexes: &Archived<CardIndexes>, n_cards: usize, op: CmpOp, val: f64) -> Option<Vec<u64>> {
    if u32::from(indexes.planes.n_cards) as usize != n_cards || n_cards == 0 {
        return None;
    }
    let wpp = words_per_plane(n_cards);
    let mut bits = vec![0u64; wpp];
    for b in 0..RARITY_INTERIOR {
        if num_cmp(op, b as f64, val) {
            let plane = PLANE_RARITY + b;
            for (a, w) in bits.iter_mut().zip(&indexes.planes.words[plane * wpp..(plane + 1) * wpp]) {
                *a |= u64::from(*w);
            }
        }
    }
    match rarity_hi_verdict(op, val) {
        BucketVerdict::FullyIncluded => {
            for (a, w) in bits.iter_mut().zip(&indexes.planes.words[PLANE_RARITY_HI * wpp..(PLANE_RARITY_HI + 1) * wpp]) {
                *a |= u64::from(*w);
            }
        }
        BucketVerdict::FullyExcluded => {}
        BucketVerdict::Ambiguous => return None,
    }
    Some(bits)
}

/// Narrow `rarity <op> val`: plane path first, postings fallback otherwise
/// (see rarity_plane_candidates's doc). Standalone rather than a narrow_rec-
/// local closure so both the direct NumericCmp arm and -rarity:x's dedicated
/// Not arm can share it -- the latter calls this with negate_op(op), not a
/// bitmap complement (see that arm's comment for why the distinction matters).
fn narrow_rarity(indexes: &Archived<CardIndexes>, n_cards: usize, op: CmpOp, val: f64) -> Option<Narrowed> {
    if let Some(bits) = rarity_plane_candidates(indexes, n_cards, op, val) {
        return Narrowed::loose(Candidates::CardBits(bits));
    }
    rarity_candidates(&indexes.rarity, op, val).and_then(|c| Narrowed::loose(Candidates::Cards(c)))
}

// ─── Combined indexes ────────────────────────────────────────────────────────

// Postings live in two id spaces: card-level indexes post OracleCard indices
// (~31.5k), printing-level indexes post Printing indices (~97k). Candidates
// carry their space (see Candidates) and convert at combine points.
#[derive(Archive, Serialize, Deserialize)]
#[derive(Default)]
struct CardIndexes {
    name_trigram:   SortedTrigramIndex, // card space
    oracle_trigram: OracleTextIndex, // card space (via dense text ids)
    cmc:            NumericIndex,    // card space
    power:          NumericIndex,    // card space
    toughness:      NumericIndex,    // card space
    rarity:         RarityIndex,     // card space (any-printing-at-rarity)
    subtypes:       TagIndex,        // card space
    keywords:       TagIndex,        // card space
    oracle_tags:    TagIndex,        // card space
    art_tags:       TagIndex,        // printing space
    is_tags:        TagIndex,        // printing space
    frame_data:     HybridTagIndex,  // printing space (bitmap for dense values, postings for the sparse tail)
    artists:        ArtistIndex,     // printing space (CSR by artist vocab id)
    flavor:         FlavorIndex,     // printing space (CSR by dense flavor text id)
    set_codes:      TagIndex,        // printing space
    watermarks:     TagIndex,        // printing space
    released_at:    PrintingValueIndex,       // printing space
    price_usd:      PrintingValueIndex,       // printing space (integer cents, already order-preserving)
    price_eur:      PrintingValueIndex,       // printing space (integer cents, same shape as price_usd)
    price_tix:      PrintingValueIndex,       // printing space (integer cents, same shape as price_usd)
    collector_number: PrintingValueIndex,     // printing space (extracted int)
    // Exact distinct-CARD counts per distinct value of each range index above, so a card-space
    // range acquire reports the truth instead of the `k.min(n_cards)` proxy (which over-estimates a
    // median 1.49x). ~159 KB for all three; see RangeCardCounts.
    released_at_cards:      RangeCardCounts,
    price_usd_cards:        RangeCardCounts,
    price_eur_cards:        RangeCardCounts,
    price_tix_cards:        RangeCardCounts,
    collector_number_cards: RangeCardCounts,
    /// The sixth, over `rarity_printing_ordered`. Rarity is NOT a `bare_range_bounds` member -- that
    /// would make rarity queries eligible for `PrintingRangeScan`/`CardRangePopcount`, a routing change
    /// worth its own measurement -- so this serves `exact_result_total`'s dedicated rarity arm only.
    /// 6 distinct values, so it is ~144 bytes.
    ///
    /// Per-value counts would NOT have worked here: a card can be printed at several rarities, so
    /// distinct cards for `r<=rare` is not the sum of the at-rarity counts. Prefix/suffix/at is the
    /// shape the question needs, and it is the shape this struct already is.
    rarity_cards:           RangeCardCounts,
    /// Exact 3-space totals for the low-cardinality dimensions whose predicate tests one value:
    /// border, layout, frame, and (format, status). ~2 KB.
    value_totals:   ValueTotals,
    /// Exact 3-space totals for PAIRS of dense low-cardinality values (~14 KB), so a two-leaf `And` over
    /// them is answered rather than bounded by `min`.
    pair_totals:    PairTotals,
    sort_perms:     SortPermutations,          // card space (streamed selection)
    artwork_groups: Vec<u16>,                  // card space: distinct illustration groups
    // card space, n_cards+1 entries: prefix sum of artwork_groups, so card c's artworks are the
    // contiguous global ids [artwork_base[c], artwork_base[c+1]) and the last entry is n_artworks.
    // Precomputed because it is a pure function of artwork_groups and therefore fixed at load: the
    // compose fastpath rebuilt it on EVERY artwork query, an O(n_cards) pass measured at ~11-12 us.
    // 126 KB of archive to take that off the hot path.
    artwork_base: Vec<u32>,
    artwork_group_col: Vec<u16>,               // printing space: pid -> artwork_group_id (columnar; lets the gather skip read gid without touching the wide struct)
    max_artwork_groups: u16,                   // max distinct artwork groups of any single card; group_best is pre-sized to this so the hot loop needs no bounds/resize check
    // printing space: printing_id -> card_id, direct lookup. Replaces a
    // partition_point search on `offsets` in cards_of_printings' hot paths —
    // see docs/issues/00690-engine-direct-projection-arrays.md.
    printing_to_card: Vec<u32>,
    planes:         BitPlanes,                 // card space: transposed low-cardinality dims (#630)
    border_printing: BorderPrintingPlanes,     // printing space: exact bit-per-printing border (#724)
    rarity_printing: RarityPrintingPlanes,     // printing space: exact bit-per-printing rarity (#724)
    // printing space: rarity int -> tiebreak-ordered pids, the `orderby=rarity` walk's structure.
    // Dual storage with `rarity_printing` above, deliberately: the FILTER path wants a whole-bucket
    // bitmap (`rarity_cmp_leaf_bits` ANDs ~1,519 words), while the WALK wants sort order so it can
    // stop when the page fills. Neither shape serves the other, and this one is the same
    // `PrintingValueIndex` the three range dimensions use, so it costs a builder call and ~389 KB.
    rarity_printing_ordered: PrintingValueIndex,
    name_bigrams:   NameBigramIndex,           // card space: exact 2-byte name containment (#639)
    name_unigrams:  NameUnigramIndex,          // card space: exact 1-byte name containment (#858)
    legal_divergent: Vec<u16>,                // card space: ids with divergent legality (#630 phase 2), postings not a plane — see build_divergent_ids
    arith_tuple:    ArithTupleIndex,           // card space: joint (cmc,power,toughness,loyalty) postings for arith predicates (#743)
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
    // Distinct hybrid mana symbols, indexed by ManaCost.hybrids ids (~29
    // entries). ManaCostCmp binds query symbols against these (see
    // MANA_SYM_UNKNOWN for symbols no card carries).
    mana_vocab: Vec<String>,
    indexes: CardIndexes,
    // The writer's format→shift assignments. Persisted so reader processes —
    // which never run the load path that feeds FORMAT_SHIFTS — resolve
    // legality shifts identically to the worker that built the archive.
    format_shifts: HashMap<String, u8>,
}

// ─── Candidate narrowing ─────────────────────────────────────────────────────

/// A narrowed candidate set, tagged with the id space its members live in and
/// its representation (#636): sorted id vecs for sparse sets — cheap merges,
/// today's fast path — and bitmaps for broad sets, unions, and complements,
/// whose word-wise ops cost O(n/64) regardless of density. Narrowing is
/// advisory (the driver re-verifies), so converting between spaces or
/// representations can only loosen or tighten candidates, never change results.
#[derive(Debug)]
enum Candidates {
    Cards(Vec<u32>),
    Printings(Vec<u32>),
    CardBits(Vec<u64>),
    PrintingBits(Vec<u64>),
}

/// A candidate set plus the property the Not arm needs: `tight` means every
/// member satisfies the subtree in its own space (for card-space sets: for
/// every printing). Complementing a tight set yields a sound superset of the
/// negation's matches; complementing a loose (superset) set would *exclude*
/// real matches, so Not narrows only through tight children. Tightness
/// survives same-space And/Or of tight sets and is lost by space projection,
/// complement (Nulls get over-included), and any loose input.
struct Narrowed {
    set: Candidates,
    tight: bool,
    /// Which of a top-level `And`'s children this candidate set already PROVES, as a bitmask over the
    /// children in written order. Set only by the `And` arm, and only for children whose own narrowing
    /// was tight AND card-space; 0 everywhere else, including for a nested `And` (only the outermost
    /// mask is ever read).
    ///
    /// Tightness has always been one bit for the whole expression, so a single un-narrowed conjunct
    /// makes the whole set loose and the residual re-verifies EVERY conjunct — including ones membership
    /// in the set already settles. `o:this` alone narrows tightly and runs in 67 us examining zero
    /// printings; `o:this border:black` re-evaluates the oracle contains on all 19,968 candidates and
    /// costs 1,993 us. The leaf identity does not matter (`cn>200`, `frame:2015`, three leaves at once
    /// all land within 8%), because the cost is one card-level text evaluation per candidate CARD.
    ///
    /// Card-space only, for the reason `narrow_candidates_exact` spells out: a tight PRINTING-space set
    /// says "this printing matches", not "every printing of this card does", so it cannot excuse a
    /// per-printing check. Card-space tight is exactly the `all_match` promotion argument applied to one
    /// conjunct instead of the whole tree.
    proven: u64,
}

/// Ids-to-bits promotion threshold for And/Or composition. Below it the
/// sorted-vec merge paths are already microseconds and byte-identical to the
/// pre-#636 behavior; above it, scatters plus word loops avoid the
/// gather-merge allocations that made broad unions lose (#618). Same
/// measured-constant philosophy as STREAM_MIN_MATCHES / MAX_NARROW_FRACTION.
/// Calibrated (scripts/bench_cost_guards.py, `usd<x or usd>y` with two exactly
/// dialable sets): vec-merge wins ~8% below ~512 combined ids, and everything
/// from 1k to 32k sits inside the ±5% benchmark noise floor — the curves are
/// too flat there to justify moving the trigger, so it stays at 4,096.
static BITS_PROMOTE: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_BITS_PROMOTE", 4_096));

/// Set bits for each id (any order, duplicates fine) in a fresh n-bit buffer.
fn scatter_bits<I: IntoIterator<Item = u32>>(ids: I, n: usize) -> Vec<u64> {
    let mut bits = vec![0u64; n.div_ceil(64)];
    for id in ids {
        bits[(id >> 6) as usize] |= 1u64 << (id & 63);
    }
    bits
}

/// In-place complement over an n-element domain (tail bits stay clear).
fn complement_bits(bits: &mut [u64], n: usize) {
    for w in bits.iter_mut() {
        *w = !*w;
    }
    let tail = n % 64;
    if tail != 0 {
        bits[n.div_ceil(64) - 1] &= (1u64 << tail) - 1;
    }
}

fn or_bits_into(acc: &mut [u64], other: &[u64]) {
    for (a, b) in acc.iter_mut().zip(other) {
        *a |= b;
    }
}

fn and_bits_into(acc: &mut [u64], other: &[u64]) {
    for (a, b) in acc.iter_mut().zip(other) {
        *a &= b;
    }
}

/// Card-space candidate mask for one (format, status) legality check --
/// (docs/issues/00667-engine-legality-divergent-carveout.md, generalized to
/// banned/restricted by #678, see docs/issues/engine-legality-banned-
/// restricted-planes.md) exact for every card, including divergent ones:
/// reads the status's `_EXISTS` plane directly for the positive case or its
/// `_ABSENT`/`_ILLEGAL` plane for the negated case, never a bit-complement of
/// the other (that would compute `∀p: ¬status(p)`, wrong -- a divergent card
/// can satisfy both `∃p: status(p)` and `∃p: ¬status(p)` at once). Exact as a
/// *narrowing* set (no divergent-postings OR needed anymore -- `legal_divergent`
/// is unchanged and still used by `filter.rs`'s per-printing `Legality`
/// evaluation, just not here), but callers still report `Narrowed::loose`:
/// existence-for-some-printing isn't the true-for-every-printing fact `tight`
/// requires (see `narrow_rec`'s `Legality` arms and `Narrowed`'s doc).
fn legality_candidate_bits(indexes: &Archived<CardIndexes>, n_cards: usize, shift: u8, expected: u64, negate: bool) -> Option<Vec<u64>> {
    if u32::from(indexes.planes.n_cards) as usize != n_cards || n_cards == 0 {
        return None;
    }
    let (exists_base, absent_base) = status_plane_bases(expected)?;
    let wpp = words_per_plane(n_cards);
    let base = if negate { absent_base } else { exists_base };
    let legal_plane = base + shift as usize / 2;
    let words = &indexes.planes.words;
    Some(words[legal_plane * wpp..(legal_plane + 1) * wpp].iter().map(|w| u64::from(*w)).collect())
}

/// Project a printing-space bitmap up to card space. Printings of card i are
/// contiguous, and set bits come out ascending, so a single monotone cursor
/// replaces the per-posting binary search cards_of_printings pays —
/// O(set bits + cards), independent of density.
fn printing_bits_to_card_bits(pbits: &[u64], offsets: &AOffsets, n_cards: usize) -> Vec<u64> {
    let mut out = vec![0u64; n_cards.div_ceil(64)];
    let mut card: usize = 0;
    for (i, &word) in pbits.iter().enumerate() {
        let mut w = word;
        while w != 0 {
            let p = ((i as u32) << 6) | w.trailing_zeros();
            w &= w - 1;
            while u32::from(offsets[card + 1]) <= p {
                card += 1;
            }
            out[card >> 6] |= 1u64 << (card & 63);
        }
    }
    out
}

/// Map a sorted printing-id list up to its sorted card-id list via the
/// precomputed direct lookup (`CardIndexes::printing_to_card`). Printings are
/// grouped contiguously by card, so the mapped list arrives sorted with
/// adjacent duplicates — dedup is a single linear pass for small lists. Past
/// a few hundred, scattering directly into a card bitmap and extracting set
/// bits is cheaper than repeated pushes (same reasoning as `scatter_bits`
/// elsewhere). Both branches use the direct array now — benchmarked
/// unconditionally cheaper than a `partition_point` search on `offsets` at
/// every k tested, see docs/issues/00690-engine-direct-projection-arrays.md.
fn cards_of_printings(offsets: &AOffsets, printing_to_card: &AOffsets, printing_ids: &[u32]) -> Vec<u32> {
    if printing_ids.len() > 1024 {
        let n_cards = offsets.len().saturating_sub(1);
        let bits = scatter_bits(printing_ids.iter().map(|&p| u32::from(printing_to_card[p as usize])), n_cards);
        return bitmap_card_ids(&bits);
    }
    let mut out: Vec<u32> = Vec::with_capacity(printing_ids.len());
    for &p in printing_ids {
        let card = u32::from(printing_to_card[p as usize]);
        if out.last() != Some(&card) {
            out.push(card);
        }
    }
    out
}

impl Candidates {
    /// Project into card space (identity for card-space sets) and materialize
    /// as ascending card ids. Bitmap materialization needs no sort — set bits
    /// come out ascending, sidestepping the gather-and-sort cost of #609.
    fn into_cards(self, offsets: &AOffsets, printing_to_card: &AOffsets) -> Vec<u32> {
        let n_cards = offsets.len().saturating_sub(1);
        match self {
            Candidates::Cards(v) => v,
            Candidates::Printings(v) => cards_of_printings(offsets, printing_to_card, &v),
            Candidates::CardBits(b) => bitmap_card_ids(&b),
            Candidates::PrintingBits(b) => bitmap_card_ids(&printing_bits_to_card_bits(&b, offsets, n_cards)),
        }
    }

    fn is_printing_space(&self) -> bool {
        matches!(self, Candidates::Printings(_) | Candidates::PrintingBits(_))
    }

    /// Exact member count, in the set's **own** space — a printing-space set counts
    /// printings, not the cards they belong to. Bitmap variants popcount the whole
    /// bitmap, so this is O(words) rather than O(1); callers in a loop should cache it.
    fn len(&self) -> usize {
        match self {
            Candidates::Cards(v) | Candidates::Printings(v) => v.len(),
            Candidates::CardBits(b) | Candidates::PrintingBits(b) => b.iter().map(|w| w.count_ones() as usize).sum(),
        }
    }

    /// Which representation the narrowing produced, for `explain` to report. A vec-shaped
    /// result means some site built a sorted vec — a `collect` + `sort_unstable` ran. A
    /// bits-shaped one means the narrowing stayed word-wise and never sorted.
    ///
    /// Read at the *top* of the narrowing, so it is a proxy rather than a census of sort
    /// events: `or_all` can scatter a vec-shaped arm into bits, hiding a sort that did
    /// happen, and a bits-shaped arm can be extracted into a vec by `and_all`. Exact
    /// per-site accounting needs the shared materialization helper that
    /// docs/issues/done/local-engine-candidate-materialize.md proposes.
    fn repr(&self) -> NarrowedRepr {
        match self {
            Candidates::Cards(_) => NarrowedRepr::Cards,
            Candidates::Printings(_) => NarrowedRepr::Printings,
            Candidates::CardBits(_) => NarrowedRepr::CardBits,
            Candidates::PrintingBits(_) => NarrowedRepr::PrintingBits,
        }
    }

    /// The set as a bitmap over an n-element domain (scatters vec variants;
    /// space is unchanged — callers pass the domain size of the set's space).
    fn into_bits(self, n: usize) -> Vec<u64> {
        match self {
            Candidates::Cards(v) | Candidates::Printings(v) => scatter_bits(v, n),
            Candidates::CardBits(b) | Candidates::PrintingBits(b) => b,
        }
    }
}

impl Narrowed {
    fn tight(set: Candidates) -> Option<Narrowed> {
        Some(Narrowed { set, tight: true, proven: 0 })
    }

    fn loose(set: Candidates) -> Option<Narrowed> {
        Some(Narrowed { set, tight: false, proven: 0 })
    }

    /// Project into card space. Printing→card projection is an existence
    /// projection ("some printing matches"), which loses tightness.
    fn into_card_space(self, offsets: &AOffsets, printing_to_card: &AOffsets) -> Narrowed {
        let n_cards = offsets.len().saturating_sub(1);
        match self.set {
            Candidates::Cards(_) | Candidates::CardBits(_) => self,
            Candidates::Printings(v) => {
                Narrowed { set: Candidates::Cards(cards_of_printings(offsets, printing_to_card, &v)), tight: false, proven: 0 }
            }
            Candidates::PrintingBits(b) => {
                Narrowed { set: Candidates::CardBits(printing_bits_to_card_bits(&b, offsets, n_cards)), tight: false, proven: 0 }
            }
        }
    }
}

/// Intersect same-space sets. All-vec inputs keep today's sort-by-length merge
/// chain; any bitmap input (or a later promotion) runs word-wise AND. Tight
/// iff every input is tight.
fn and_all(mut sets: Vec<Narrowed>) -> Option<Narrowed> {
    if sets.is_empty() {
        return None;
    }
    if sets.len() == 1 {
        return sets.pop();
    }
    let tight = sets.iter().all(|s| s.tight);
    let card_space = !sets[0].set.is_printing_space();
    let mut vecs: Vec<Vec<u32>> = Vec::new();
    let mut bit_sets: Vec<Vec<u64>> = Vec::new();
    for s in sets {
        match s.set {
            Candidates::Cards(v) | Candidates::Printings(v) => vecs.push(v),
            Candidates::CardBits(b) | Candidates::PrintingBits(b) => bit_sets.push(b),
        }
    }
    // Intersect the vecs by ascending length (today's path), AND the bitmaps
    // word-wise, then combine by retaining the vec against the bitmap — the
    // sparse side never gets scattered, and the result stays a vec whenever
    // any input was one.
    vecs.sort_unstable_by_key(Vec::len);
    let mut vec_iter = vecs.into_iter();
    // `next()` takes the shortest and leaves the rest ascending. `swap_remove(0)` also
    // returned the shortest, but moved the *longest* into slot 0, so the merge chain then
    // ran longest-first — the expensive direction for an O(|a| + |b|) merge, and the
    // opposite of what the comment above says.
    let vec_result = vec_iter.next().map(|mut result| {
        for v in vec_iter {
            if result.is_empty() {
                break;
            }
            result = intersect_sorted(&result, &v);
        }
        result
    });
    let mut bit_iter = bit_sets.into_iter();
    let bits_result = bit_iter.next().map(|mut acc| {
        for b in bit_iter {
            and_bits_into(&mut acc, &b);
        }
        acc
    });
    let set = match (vec_result, bits_result) {
        (Some(mut v), Some(b)) => {
            v.retain(|&id| b[(id >> 6) as usize] >> (id & 63) & 1 == 1);
            if card_space { Candidates::Cards(v) } else { Candidates::Printings(v) }
        }
        (Some(v), None) => {
            if card_space { Candidates::Cards(v) } else { Candidates::Printings(v) }
        }
        (None, Some(b)) => {
            if card_space { Candidates::CardBits(b) } else { Candidates::PrintingBits(b) }
        }
        (None, None) => unreachable!("sets was non-empty"),
    };
    Some(Narrowed { set, tight, proven: 0 })
}

/// Union same-space sets. Small all-vec inputs keep today's merge; anything
/// broad or bitmap-shaped promotes to a bitmap union — O(n/64) per input with
/// no per-pair merge allocations (the #618 union-materialization cost).
fn or_all(mut sets: Vec<Narrowed>, n: usize) -> Option<Narrowed> {
    if sets.is_empty() {
        return None;
    }
    if sets.len() == 1 {
        return sets.pop();
    }
    let tight = sets.iter().all(|s| s.tight);
    let card_space = !sets[0].set.is_printing_space();
    let all_small_vecs = sets
        .iter()
        .all(|s| !matches!(s.set, Candidates::CardBits(_) | Candidates::PrintingBits(_)))
        && sets.iter().map(|s| s.set.len()).sum::<usize>() <= *BITS_PROMOTE;
    let set = if all_small_vecs {
        let mut union: Vec<u32> = Vec::new();
        for s in sets {
            match s.set {
                Candidates::Cards(v) | Candidates::Printings(v) => union = union_sorted(union, v),
                _ => unreachable!(),
            }
        }
        if card_space { Candidates::Cards(union) } else { Candidates::Printings(union) }
    } else {
        let mut iter = sets.into_iter();
        let mut acc = iter.next().unwrap().set.into_bits(n);
        for s in iter {
            or_bits_into(&mut acc, &s.set.into_bits(n));
        }
        if card_space { Candidates::CardBits(acc) } else { Candidates::PrintingBits(acc) }
    };
    Some(Narrowed { set, tight, proven: 0 })
}

/// Can `f` never evaluate to `Tri::Null` on any card? Only then is the complement of a tight narrowing
/// exact, because a Null card satisfies neither `f` nor `Not(f)` yet still lands in the complement.
///
/// Deliberately tiny, and conservative by default: a `false` answer only costs the `Not` arm its tight
/// marking, while a wrong `true` drops or invents rows. The one field that qualifies today is
/// `NameLower`, and the proof is one line in `str_val_of` -- it returns `StrVal::Known(...)`
/// UNCONDITIONALLY, where oracle goes through `opt_sv` and flavor through `map_or(StrVal::PDep, ..)`.
/// An empty name is still `Known("")`, and `"".contains(needle)` is `false` rather than Null, so the
/// empty case needs no carve-out.
///
/// Before extending this: nullable fields are a repeat source of exactly this bug --
/// `tight_narrow_space` had to drop `released_at` for being nullable, and excludes price for the same
/// class of reason. Add a field only with the `str_val_of` / accessor line that proves totality.
fn never_null(f: &FilterExpr) -> bool {
    matches!(f, FilterExpr::TextContains { field: TextSearchField::NameLower, .. })
}

/// Static answer to "could narrow_rec(f) produce a tight set, and in which
/// space?" — Some(true) = printing space, Some(false) = card space, None =
/// never tight. Conservative: loose-by-construction sources and mixed-space
/// compositions return None without computing anything. Used by the Not arm,
/// whose complement is only sound over tight sets.
fn tight_narrow_space(f: &FilterExpr) -> Option<bool> {
    match f {
        FilterExpr::ColorCmp { .. } | FilterExpr::TypeCmp { .. } => Some(false),
        // Exact names resolve exactly through the sorted name permutation.
        FilterExpr::ExactName(_) => Some(false),
        // 1- and 2-byte name needles resolve exactly through the unigram / bigram indexes.
        FilterExpr::TextContains { field: TextSearchField::NameLower, word } if word.len() <= 2 => Some(false),
        // Ge-only guard is deliberate (#700): narrow_rec's CollectionCmp arm
        // now also narrows Eq/Gt through the same containment postings, but
        // only loosely — the postings prove `contains(value)`, not the
        // length condition Eq/Gt additionally require — so they must stay
        // out of this classifier. Falling through to `None` below for them is
        // correct: Not's complement trick is only sound over tight sets, and
        // Eq/Gt never produce one.
        FilterExpr::CollectionCmp { field, op: CmpOp::Ge, .. } => {
            Some(matches!(field, CollField::ArtTags | CollField::IsTags | CollField::FrameData))
        }
        FilterExpr::NumericCmp { lhs, rhs, .. } => {
            let f = |e: &NumExpr| match e {
                NumExpr::Field(NumField::Cmc | NumField::Power | NumField::Toughness) => Some(false),
                // Price is absent deliberately, even though range_narrowed is now called with
                // exact=true for it (see the `price` closure below): this classifier gates the
                // Not arm's complement-safety check, a separate question from range_narrowed's
                // own exactness. A price-range set's complement would need to correctly exclude
                // NULL-priced printings, which are simply absent from the index rather than
                // failing a bound check -- deferred to
                // docs/issues/local-engine-broad-range-fastpath.md's fastpath work, not yet
                // reviewed for composition safety here.
                NumExpr::Field(NumField::CollectorNumberInt) => Some(true),
                NumExpr::Const(_) => None,
                _ => None,
            };
            match (f(lhs), f(rhs), matches!(lhs, NumExpr::Const(_)) || matches!(rhs, NumExpr::Const(_))) {
                (Some(space), None, true) | (None, Some(space), true) => Some(space),
                _ => None,
            }
        }
        // Absent deliberately, same reasoning as price above (found while adding negated-range
        // narrowing, docs/issues/local-engine-negated-range-narrowing.md): `released_at` is
        // nullable, and this classifier previously claimed `Some(true)` unconditionally, which the
        // generic Not-arm below would have trusted to bit-complement a tight DateCmp/YearCmp
        // set — wrongly pulling in every NULL-dated printing (absent from the index, not failing a
        // bound check) into the *candidate* set for e.g. `-year:1993`. Not a wrong final answer —
        // `narrow_candidates_exact`'s exactness check reads the complement's own (always-loose)
        // `.tight` field, not this classifier, so residual `card_pass` verification still ran and
        // dropped the NULL-dated printings before any total/page was returned — but a real,
        // avoidable cost regression (an unnecessary complement built and then fully re-verified) for
        // any negated `DateCmp`/`YearCmp` query. The four ordered ops now narrow exactly through
        // `bare_range_bounds`'s own `Not` handling instead (no complement, no NULL risk, no wasted
        // verification); `Eq`'s negation (`Ne`) isn't a representable range either way and correctly
        // declines via that path already — so nothing is lost by removing this from the "safe to
        // complement" list.
        FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => None,
        FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, .. } => Some(true),
        FilterExpr::ArtistMatch { .. } | FilterExpr::FlavorMatch { .. } => Some(true),
        FilterExpr::And(children) | FilterExpr::Or(children) => {
            let mut spaces = children.iter().map(tight_narrow_space);
            let first = spaces.next()??;
            spaces.all(|s| s == Some(first)).then_some(first)
        }
        _ => None,
    }
}

/// Like narrow_candidates, but also reports whether the returned set (when
/// Some) is card-level exact — #634 Step 1's all_match promotion needs this:
/// when the residual is provably both tight (no false positives) and
/// complete (every true match included, which `narrow_rec`'s `tight` already
/// tracks through its And/Or composition — see `and_all`/`or_all`), the whole
/// original query is exact whenever a present `plane` is too (always true —
/// that's what `compile_plane` already guarantees), and per-candidate
/// `card_pass` becomes redundant work the narrowing already did.
///
/// Critically, `tight` alone is not enough: it means every member of the set
/// truly satisfies the predicate *in the set's own space*. For a printing-
/// space result that's "this specific printing matches," not "every printing
/// of the associated card matches" — but `card_pass`'s `Tri::True` (what
/// `all_match` stands in for) specifically means the latter. A card can have
/// printings in and out of a printing-space match (e.g. `set:war` — most
/// cards have other-set printings too), so a tight-but-printing-space result
/// must never promote. Only a genuinely card-space tight result qualifies.
///
/// A discarded-for-broadness result never promotes either: "exact" alone
/// isn't enough without the actual membership in hand to skip verification
/// safely — a too-broad-to-narrow-with `cmc<=6` is still exact in principle,
/// but we don't have its membership without paying to materialize it, which
/// isn't worth doing just for this (see
/// docs/issues/00634-engine-permuted-bitmap-order-phase.md).
fn narrow_candidates_exact(
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    cards: &[AOracleCard],
) -> (Option<Candidates>, bool, u64) {
    let n_cards = offsets.len().saturating_sub(1);
    let n_printings = if n_cards == 0 { 0 } else { u32::from(offsets[n_cards]) as usize };
    match narrow_rec(filter, indexes, offsets, cards, false) {
        None => (None, false, 0),
        Some(n) => {
            let printing_space = n.set.is_printing_space();
            let domain = if printing_space { n_printings } else { n_cards };
            // Breadth is a reason to discard a LOOSE set: the walk would pay union, projection and
            // materialization and then still verify every candidate, so a near-total loose set is worse
            // than no narrowing at all. It is not a reason to discard a TIGHT card-space one, where
            // keeping it removes verification entirely -- 23,675 candidates with no card_pass beats
            // 31,508 with a full oracle-text memmem each, and not narrowly. (#860)
            let worth_keeping = n.set.len() <= domain - domain / 4 || (n.tight && !printing_space);
            if worth_keeping {
                // The mask is only meaningful alongside the set it was derived from: discarding the set
                // for broadness discards the proof with it.
                (Some(n.set), n.tight && !printing_space, n.proven)
            } else {
                (None, false, 0)
            }
        }
    }
}

// Only run_query needs the exactness bit (#634 Step 1); every other caller —
// all in tests — just wants the candidate set, same as before that change.
#[cfg(test)]
fn narrow_candidates(
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    cards: &[AOracleCard],
) -> Option<Candidates> {
    narrow_candidates_exact(filter, indexes, offsets, cards).0
}

/// Once any candidate source in an And is this selective, evaluating further
/// (costlier) children buys nothing the driver's verification doesn't already
/// do — the remaining children are skipped. Calibrated
/// (scripts/bench_cost_guards.py): the synthetic crossover where including a
/// printing-range child starts paying is wobbly (2.8k-11k driver cards) and
/// its *sign* depends on the child's selectivity — a selective child wins
/// ~2× included at 4k drivers, a broad child loses ~2× there. A wild-query
/// A/B of 2,048 vs 8,192 on a pre-name-index build regressed 8k by 3%
/// geomean with 4-8× tails (skipped `cn:` children under then-broad
/// exact-name drivers); rerun after the exact-name index landed, those
/// drivers are tiny and skip under any threshold, making the A/B a wash. So
/// 2,048 — just below the pooled synthetic spread — stands, and nothing on
/// real traffic argues for moving it.
static AND_SKIP_THRESHOLD: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_AND_SKIP_THRESHOLD", 2_048));

/// Selectivity floor below which a probed rank-1 range child is included in the
/// And narrowing even when it would NOT beat the current driver (`k >= best`).
/// The `AND_SKIP_THRESHOLD` guard classifies range children by worst-case cost
/// *class*, so it skips a highly selective range (`usd<0.02`) as readily as a
/// near-total one (`usd<50`) once any driver is selective — but `range_narrowed`'s
/// two `partition_point` searches yield the real match count `k` for free before
/// that decision (see `probe_range_k` / `narrow_rec`'s And arm). A child with
/// `k < best` becomes the new, strictly-smaller driver (fewer residual
/// verifications, never a regression). This floor additionally admits a range so
/// small that materializing its sorted vec is below the timing-noise floor even
/// when it can't lower the driver: bounded well under `AND_SKIP_THRESHOLD` so it
/// can never re-admit the broad children that guard protects, and deliberately
/// tiny (64) so it stays clear of the `!"name" set:SLD cn:N` tail — those `cn:N`
/// sets are far larger than 64 in the common (small collector-number) case, so
/// they still skip under a tiny exact-name driver, avoiding the 8,192-experiment
/// regression (docs/issues/local-engine-probe-before-and-skip.md).
static AND_PROBE_FLOOR: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_AND_PROBE_FLOOR", 64));

/// `-r:x`'s dedicated shape: a rarity comparison, any op (`narrow_rarity` isn't limited to the four
/// ordered ones the printing-range path needs — see that arm's doc). Index-free — eligibility never
/// depends on `indexes`, only the actual re-narrow work does. The single source of truth for this
/// shape: `narrow_rec`'s own `-r:x` arm gates on this function directly (not a separate inline
/// `matches!`), and so does `and_child_rank` — the two can no longer drift apart.
fn is_rarity_negation_shape(f: &FilterExpr) -> bool {
    matches!(
        f,
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), rhs: NumExpr::Const(_), .. }
            | FilterExpr::NumericCmp { lhs: NumExpr::Const(_), rhs: NumExpr::Field(NumField::RarityInt), .. }
    )
}

/// `-f:x`/`-banned:x`/`-restricted:x`'s dedicated shape: a tracked-format `Legality` leaf.
/// Index-free (`status_plane_bases` takes only `expected`). The single source of truth for this
/// shape: `narrow_rec`'s own `-f:x` arm gates on this function directly, and so does `and_child_rank`.
fn is_legality_negation_shape(f: &FilterExpr) -> bool {
    matches!(f, FilterExpr::Legality { shift: Some(_), expected } if status_plane_bases(*expected).is_some())
}

/// Evaluation-cost rank for And children: cheap sources first (postings,
/// planes, card numerics, trigram lookups), printing-space ranges second
/// (their broad form pays an O(k) scatter), complements last (broad by
/// construction, useful only when nothing else narrowed).
fn and_child_rank(f: &FilterExpr, indexes: &Archived<CardIndexes>) -> u8 {
    match f {
        // `-r:x` / `-f:x`: dedicated re-narrows, not a broad complement — rank them exactly like
        // their un-negated inner form. Guarded on the same index-free predicates `narrow_rec`'s own
        // `-r:x`/`-f:x` arms gate on (not a separate shape check), so a shape either function
        // recognizes, the other does too, by construction.
        FilterExpr::Not(inner) if is_rarity_negation_shape(inner) || is_legality_negation_shape(inner) => and_child_rank(inner, indexes),
        // `-usd<c` / `-cn<c` / `-date>c` / `-year>=c`: same "cheap re-narrow, not a complement"
        // reasoning, but delegated to `bare_range_bounds` itself (called on the `Not` node `f`, not
        // the unwrapped `inner` — its own `Not` arm is what applies `negate_op` before checking
        // representability, so calling it on `inner` directly would wrongly accept `Eq`, whose
        // negation `Ne` isn't a representable range). `and_child_rank` now takes `indexes` for
        // exactly this: unlike the index-free rarity/legality shapes, this is the one dedicated arm
        // whose real implementation (`resolve_numeric_range_leaf`'s index lookup) already needs it,
        // so there's no way to mirror it index-free without a second, driftable implementation — call
        // the real thing directly instead, the same way `is_printing_composable`/`compose_printing_bits`/
        // `compose_printing_estimate` already do.
        FilterExpr::Not(inner) if bare_range_bounds(f, indexes).is_some() => and_child_rank(inner, indexes),
        // `-(arith tuple predicate)`: a cheap exact re-narrow (#743 negated arm), not a broad
        // complement — rank it like its positive inner form, gated on the same `is_arith_tuple_route`
        // predicate `narrow_rec`'s own negated arm dispatches on (not a second shape check), so the
        // ranking can't drift from what actually executes (the #741 rank/execution-mismatch lesson).
        FilterExpr::Not(inner) if is_arith_tuple_route(inner) => and_child_rank(inner, indexes),
        // Any other `Not` (a card-space numeric like `-cmc:3`, negated equality on price/cn/date/year,
        // or a plain generic complement) falls to the broad-complement-or-decline tier — this used to
        // be the *only* arm here, silently catching every negated cheap shape above too (bug 1), then
        // (after that fix) still catching `-cmc:3`/negated-equality because the guard was too broad
        // (bug 4), then still catching `-f:x` because the guard didn't know about it at all (bug 4
        // follow-up) — each found because a shape recognized here disagreed with what `narrow_rec`
        // actually dispatched to. The three guards above are now `narrow_rec`'s own guards, not
        // reimplementations, so a future fourth dedicated arm can't drift from this one silently.
        FilterExpr::Not(_) => 2,
        // Regex trigram-narrow (#734 step 3) is second-tier: its literal factor may be broad (`flying`),
        // so pay for it only after cheap plane/posting sources — the And early-stop then skips it when a
        // selective sibling (`type:dragon`) already narrowed below the threshold.
        FilterExpr::TextRegex { .. } => 1,
        FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => 1,
        FilterExpr::NumericCmp { lhs, rhs, .. } => {
            let field = |e: &NumExpr| {
                matches!(
                    e,
                    NumExpr::Field(NumField::PriceUsd | NumField::PriceEur | NumField::PriceTix | NumField::CollectorNumberInt)
                )
            };
            if field(lhs) || field(rhs) { 1 } else { 0 }
        }
        _ => 0,
    }
}

/// The exact match count `k` of a printing-range And child (`usd`/`cn`/`date`/`year`
/// and their negations — whatever `bare_range_bounds` recognizes), computed with the
/// same two `partition_point` binary searches `range_narrowed` runs before it
/// materializes anything, so this probe is (log n, log n) and adds nothing a later
/// materialization wouldn't already pay. Returns `None` for a non-range child (a
/// `TextRegex` rank-1 sibling, say), whose selectivity can't be read this cheaply.
/// The And arm uses `k` to decide inclusion per-query instead of by cost class, and
/// to order equal-rank range children most-selective-first (removing the old
/// written-order sensitivity). An empty range yields `k = 0` (lo == hi == 0 from
/// `bare_range_bounds`), the most selective possible — exactly what a materialized
/// empty `Printings` vec would report.
fn probe_range_k(filter: &FilterExpr, indexes: &Archived<CardIndexes>) -> Option<usize> {
    let (idx, lo, hi) = bare_range_bounds(filter, indexes)?;
    let (s, e) = idx.range(lo, hi);
    Some(e - s)
}

/// The exact match count of a containment collection child, read from its index without materializing
/// anything — the collection analogue of [`probe_range_k`], and it exists for the same reason: the And
/// arm's skip decision is a COST comparison, and it can only be made for children whose size is known
/// cheaply.
///
/// `None` when there is nothing to probe: a non-containment op, or a value absent from the index. Both
/// keep the caller on its previous behaviour — an absent value in a complete index narrows to the empty
/// set, which is the best driver there is and must never be skipped.
///
/// Card-space fields count CARDS and printing-space fields count PRINTINGS, which is the same space
/// mismatch `probe_range_k` already lives with when its `k` is compared against `best`. It biases
/// toward skipping a printing-space child (printings outnumber cards ~3:1), and skipping is the safe
/// direction for a child that cannot become the driver anyway.
fn probe_collection_k(filter: &FilterExpr, indexes: &Archived<CardIndexes>) -> Option<usize> {
    let FilterExpr::CollectionCmp { field, op, value, .. } = filter else { return None };
    if !matches!(op, CmpOp::Ge | CmpOp::Eq | CmpOp::Gt) {
        return None;
    }
    // frame_data is the hybrid index, so its count is a popcount or a postings length, never a `get`.
    if matches!(field, CollField::FrameData) {
        return indexes.frame_data.len_of(value.as_str());
    }
    let idx = match field {
        CollField::Subtypes => &indexes.subtypes,
        CollField::Keywords => &indexes.keywords,
        CollField::OracleTags => &indexes.oracle_tags,
        CollField::ArtTags => &indexes.art_tags,
        CollField::IsTags => &indexes.is_tags,
        CollField::FrameData => unreachable!("handled above"),
    };
    idx.get(value.as_str()).map(|v| v.len())
}

/// One entry in the And arm's work list: a child exactly as written, or a half-open interval fused
/// from two or more same-index range children.
///
/// `usd>=0.42 usd<=0.43` is the shape that motivated this. Each half matches most of the corpus, so
/// each trips `range_too_broad_to_narrow` on its own and declines; their *intersection* is 837
/// printings. `narrow_rec` narrows children independently and intersects the results, so the
/// interval is never discovered — measured at 1,146.8 µs, against 26.7 µs for the one-sided
/// `usd>=200`, which returns *more* rows. Fusing before ranking puts the two-sided form on the same
/// sparse-vec path the one-sided form already takes.
enum AndSource<'f, 'i> {
    Child(&'f FilterExpr),
    /// `[lo, hi)` on `idx`, the intersection of two or more children's intervals, holding `k`
    /// printings — already probed, so the And arm reads it as the ranking probe instead of repeating
    /// the two binary searches. Constituents may include a `Not` (`bare_range_bounds` reduces
    /// `-usd<c` to `usd>=c`'s bounds itself); nothing downstream needs to know, because the
    /// broad-interval gate below means `k` is always sparse under `sparse_only` and `range_narrowed`
    /// therefore takes its vec path without ever consulting `broad_ok` — the one thing the negated arm
    /// cares about. The compose builders don't consult `broad_ok` at all.
    FusedRange { idx: &'i Archived<PrintingValueIndex>, lo: u32, hi: u32, k: usize },
}

/// Group an `And`'s children by which printing-range index they select on, fusing each group of two
/// or more into a single interval — `lo = max(lo_i)`, `hi = min(hi_i)`.
///
/// Children the range dispatch doesn't recognize and single-member groups pass through untouched, so
/// this cannot alter any query it doesn't fuse. Emission follows the written position of each group's
/// first member, so a caller that ranks its sources sees the tie-breaking it saw before for everything
/// unfused.
///
/// The fused interval is the exact conjunction of its constituents, which is what lets one source
/// stand in for all of them — including in `narrow_rec`'s `every_child_included` tightness accounting.
///
/// `sparse_only` is the one thing the two callers disagree about. `narrow_rec` sets it, because a broad
/// fused source reaches `range_narrowed` under a single `broad_ok` where two broad children each got
/// their own, which takes a decision away from the And's per-child skip logic for no measured gain (see
/// the gate below). The compose builders clear it: `range_leaf_bits` is an O(k) scatter at every k, so
/// one scatter of the intersection always beats two scatters and an AND, and
/// `compose_printing_estimate` reads the intersection's exact `k` where the unfused fold could only take
/// the min of the two sides (measured: 33,862 against a true 879).
fn fuse_and_range_children<'f, 'i>(
    children: &'f [FilterExpr],
    indexes: &'i Archived<CardIndexes>,
    sparse_only: bool,
) -> Vec<AndSource<'f, 'i>> {
    // At most one group per printing-range index (price / collector-number / released-at), so a
    // linear scan of the accumulator beats hashing a pointer.
    struct Group<'i> {
        first: usize,
        idx: &'i Archived<PrintingValueIndex>,
        lo: u32,
        hi: u32,
        count: usize,
    }
    let mut groups: Vec<Group<'i>> = Vec::new();
    for (pos, child) in children.iter().enumerate() {
        let Some((idx, lo, hi)) = bare_range_bounds(child, indexes) else { continue };
        match groups.iter_mut().find(|g| std::ptr::eq(g.idx, idx)) {
            Some(g) => {
                g.lo = g.lo.max(lo);
                // An unsatisfiable fusion (`usd>=1 usd<=0.5`) gives `hi < lo`, and every consumer
                // computes `k` as `partition_point(hi) - partition_point(lo)` — that subtraction
                // underflows and panics. Clamping to `[lo, lo)` yields `k = 0`, which is what an
                // empty range means and what every consumer already handles.
                g.hi = g.hi.min(hi).max(g.lo);
                g.count += 1;
            }
            None => groups.push(Group { first: pos, idx, lo, hi, count: 1 }),
        }
    }
    // Under `sparse_only`, fusion exists to DISCOVER a sparse intersection hiding behind broad halves.
    // Where the intersection is itself broad there is nothing to discover — and fusing anyway is not
    // neutral, for the `broad_ok` reason in this function's doc.
    //
    // That gate is a scope decision, not a measured win: paired traffic puts fused-vs-gated at 0.88 vs
    // 0.86 of baseline on the fusible slice, and the per-query noise floor on a slice fusion cannot
    // touch at all is ±170 µs, so the two are indistinguishable. What the gate does buy is a bound —
    // outside the sparse population where the win IS demonstrated (up to 1.3 ms/query), narrowing is a
    // provable no-op. `k` survives for the survivors so the probe isn't recomputed downstream.
    // Every printing has a card, so this CSR is corpus-length -- unlike the value indexes, which omit nulls.
    let corpus_printings = indexes.printing_to_card.len();
    let mut fused: Vec<(&Group<'i>, usize)> = Vec::new();
    for g in &groups {
        if g.count < 2 {
            continue;
        }
        let (s, e) = g.idx.range(g.lo, g.hi);
        // Same denominator argument as `range_narrowed`'s: the index omits null-valued printings, so
        // `g.idx.len()` is the priced subset and judging breadth against it overstates it by the null
        // rate. `tix>=0.03 tix<=0.03` fuses to 16,664 printings -- 30.4% of the tix index but 17.1% of
        // the corpus -- so this gate refused to fuse it, the two halves stayed separate and individually
        // broad, and BOTH declined under `broad_ok: false`. The result was a query that narrowed nothing:
        // `eur>0.15 tix>=0.03 tix<=0.03 id:bgruw` at 1,405 us against 39 us for the same query without
        // the plane-consumed partner.
        //
        // This gate, not `range_narrowed`'s, is the one that fires first -- changing only the latter
        // moved nothing, because unfused halves never reach it as an interval.
        let domain = if *RANGE_BREADTH_VS_CORPUS { corpus_printings } else { g.idx.len() };
        if !sparse_only || !range_too_broad_to_narrow(e - s, domain) {
            fused.push((g, e - s));
        }
    }
    if fused.is_empty() {
        return children.iter().map(AndSource::Child).collect();
    }
    let mut out: Vec<AndSource<'f, 'i>> = Vec::with_capacity(children.len());
    for (pos, child) in children.iter().enumerate() {
        let group = bare_range_bounds(child, indexes).and_then(|(idx, ..)| fused.iter().find(|(g, _)| std::ptr::eq(g.idx, idx)));
        match group {
            Some((g, k)) => {
                if pos == g.first {
                    out.push(AndSource::FusedRange { idx: g.idx, lo: g.lo, hi: g.hi, k: *k });
                }
            }
            None => out.push(AndSource::Child(child)),
        }
    }
    out
}

/// `broad_ok` says whether a broad printing-range child may materialize its
/// bitmap: true under Or (the union consumes it) and Not (the complement
/// trick needs it), false where nothing would — a lone broad set at the root
/// or in a candidate-less And is discarded anyway, so the scatter would be
/// pure waste (the 10x And regressions of the first benchmark round).
/// Guaranteed literal factors of a regex pattern — substrings present in **every** match, each ≥3
/// bytes (so each has at least one trigram). Used to trigram-narrow a `TextRegex` to a loose candidate
/// set that the walk then re-verifies with the real regex (#734 step 3). Extracted from the RAW pattern
/// (strip the `(?i)` we add; a case-folded HIR would be classes, not literals) and lowercased to match
/// the `*_lower` trigram index.
///
/// Pass one — concatenations of literals only. Anything a match can *skip over* ends the current run:
/// a min=0 repetition (`s?`, `.*`), a character class (`.`, `[..]`, `\d`), a zero-width look (`^`, `$`,
/// `\b`), or an alternation (`a|b`). A min≥1 repetition of a literal keeps it (`a+` still requires one
/// `a`). Conservative by construction: a factor is emitted only where every match must contain those
/// exact bytes, so narrowing on them can never drop a real match. `exile|destroy` yields no factor here
/// (deferred: union the branches' candidates); `^flying$` still yields `flying` (the looks just bound a
/// run they don't sit inside).
fn regex_required_factors(pattern: &str) -> Vec<String> {
    use regex_syntax::hir::{Hir, HirKind, Literal};
    fn flush(run: &mut Vec<u8>, out: &mut Vec<Vec<u8>>) {
        if run.len() >= 3 {
            out.push(std::mem::take(run));
        } else {
            run.clear();
        }
    }
    fn walk(hir: &Hir, run: &mut Vec<u8>, out: &mut Vec<Vec<u8>>) {
        match hir.kind() {
            HirKind::Literal(Literal(bytes)) => run.extend_from_slice(bytes),
            HirKind::Capture(c) => walk(&c.sub, run, out),
            HirKind::Concat(subs) => subs.iter().for_each(|s| walk(s, run, out)),
            // min≥1 repetition of a literal is still guaranteed (`a+` ⇒ ≥1 `a`); anything else the match can skip.
            HirKind::Repetition(r) if r.min >= 1 => match r.sub.kind() {
                HirKind::Literal(Literal(bytes)) => run.extend_from_slice(bytes),
                _ => flush(run, out),
            },
            _ => flush(run, out), // Empty | Class | Look | Alternation | Repetition{min:0}
        }
    }
    let raw = pattern.strip_prefix("(?i)").unwrap_or(pattern);
    let Ok(hir) = regex_syntax::parse(raw) else { return Vec::new() };
    let (mut run, mut out) = (Vec::new(), Vec::new());
    walk(&hir, &mut run, &mut out);
    flush(&mut run, &mut out);
    out.iter().map(|b| String::from_utf8_lossy(b).to_lowercase()).collect()
}

fn narrow_rec(
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    cards: &[AOracleCard],
    broad_ok: bool,
) -> Option<Narrowed> {
    let n_cards = offsets.len().saturating_sub(1);
    let n_printings = if n_cards == 0 { 0 } else { u32::from(offsets[n_cards]) as usize };

    // Plane-expressible subtrees (color/type comparisons under any And/Or/Not
    // combination) evaluate to an exact card bitmap in a few hundred word ops —
    // the planes are the precomputed corner of this algebra. Whole-plane
    // filters were already consumed by split_planes; this catches the ones
    // left inside mixed contexts, where they previously could not narrow at
    // all (an Or with a color child was a guaranteed full scan). True is
    // excluded: its all-ones bitmap narrows nothing. A lone oracle-word leaf
    // is excluded too: compile_plane's bonus arm for it is a strict subset of
    // the dedicated TextContains arm below (same dictionary scan, just also
    // requiring "no sparse hit" to return a PlaneExpr instead of a Narrowed),
    // so speculatively trying it here only pays for a second full dictionary
    // scan on every shape the dedicated arm below was going to handle anyway
    // — measured (scripts/bench_oracle_word_index.py) as a genuine 2x
    // regression on `o:token`-shaped queries before this exclusion.
    let lone_oracle_word_leaf = matches!(
        filter,
        FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word } if oracle_word_eligible(word)
    );
    if !lone_oracle_word_leaf
        && !matches!(filter, FilterExpr::True)
        && u32::from(indexes.planes.n_cards) as usize == n_cards
        && n_cards > 0
        && let Some(pe) = compile_plane(filter, &indexes.planes, &indexes.oracle_trigram.words)
    {
        let mut bits: Vec<u64> = Vec::new();
        eval_planes(&pe, &indexes.planes, &mut bits);
        // Legality's planes are existence projections, not true-for-
        // every-printing facts (docs/issues/engine-legality-divergent-
        // carveout.md) -- `tight`'s contract needs the latter (see
        // `Narrowed`'s doc and the dedicated `Legality` arms below), so a
        // compiled expression touching them can only narrow loosely here,
        // same as if it had fallen through to those arms directly.
        return if plane_expr_is_existential(&pe, u64::from(indexes.planes.divergent_formats)) {
            Narrowed::loose(Candidates::CardBits(bits))
        } else {
            Narrowed::tight(Candidates::CardBits(bits))
        };
    }

    match filter {
        FilterExpr::ExactName(needle) => {
            // The ascending name permutation is keyed on name_rank — i.e. on
            // card_name_lower byte order — so equal-name blocks are contiguous
            // and equality is a binary-searched range: an exact, tight card
            // set. A miss proves the empty set (names are never null).
            let perm = &indexes.sort_perms.name[0];
            if perm.len() != n_cards || cards.len() != n_cards || n_cards == 0 {
                return None; // store without name permutations
            }
            let name_of = |cid: &Archived<u32>| cards[u32::from(*cid) as usize].card_name_lower.as_str();
            let lo = perm.partition_point(|cid| name_of(cid) < needle.as_str());
            let width = perm[lo..].partition_point(|cid| name_of(cid) == needle.as_str());
            let ids: Vec<u32> = perm[lo..lo + width].iter().map(|x| u32::from(*x)).collect();
            Narrowed::tight(Candidates::Cards(ids))
        }

        FilterExpr::TextContains { field: TextSearchField::NameLower, word } if word.len() == 1 => {
            // A 1-byte needle's containment IS byte membership, so the tier lookup is the complete
            // answer — tight, exactly as the 2-byte arm below. A byte absent from the index appears in
            // no name, so the empty narrowing is exact too. (#858)
            let idx = &indexes.name_unigrams;
            if u32::from(idx.n_cards) as usize != n_cards {
                return None; // archive without unigrams for this store
            }
            let b = word.as_bytes()[0];
            if let Some(p) = idx.plane_of.get(&b) {
                let wpp = n_cards.div_ceil(64);
                let start = u32::from(*p) as usize * wpp;
                let bits: Vec<u64> = idx.plane_words[start..start + wpp].iter().map(|w| u64::from(*w)).collect();
                return Narrowed::tight(Candidates::CardBits(bits));
            }
            let ids: Vec<u32> = idx
                .postings
                .get(&b)
                .map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(u16::from(*x))).collect());
            Narrowed::tight(Candidates::Cards(ids))
        }

        FilterExpr::TextContains { field: TextSearchField::NameLower, word } if word.len() == 2 => {
            // A 2-byte needle's containment IS bigram membership, so the tier
            // lookup is the complete answer — tight, with no false positives
            // for the walk to reject. A bigram absent from the index appears
            // in no name, so the empty narrowing is exact too.
            let idx = &indexes.name_bigrams;
            if u32::from(idx.n_cards) as usize != n_cards {
                return None; // archive without bigrams for this store
            }
            let bg = [word.as_bytes()[0], word.as_bytes()[1]];
            if let Some(p) = idx.plane_of.get(&bg) {
                let wpp = n_cards.div_ceil(64);
                let start = u32::from(*p) as usize * wpp;
                let bits: Vec<u64> = idx.plane_words[start..start + wpp].iter().map(|w| u64::from(*w)).collect();
                return Narrowed::tight(Candidates::CardBits(bits));
            }
            let ids: Vec<u32> = idx
                .postings
                .get(&bg)
                .map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(u16::from(*x))).collect());
            Narrowed::tight(Candidates::Cards(ids))
        }

        FilterExpr::TextContains { field: TextSearchField::OracleTextLower, word }
            if oracle_word_eligible(word) && u32::from(indexes.oracle_trigram.words.n_cards) as usize == n_cards =>
        {
            // Exact, not a superset: every occurrence of `word` lies entirely
            // inside one tokenized dictionary word (see OracleWordIndex's
            // doc), so the union of postings for every dictionary word
            // containing it is precisely the match set — no verification.
            let words = &indexes.oracle_trigram.words;
            let scan = scan_oracle_words(words, word);
            let wpp = words_per_plane(n_cards);
            // Folds `union_sorted` once per matched dictionary word, so it is quadratic in
            // total postings — but only in the *step count*, and the needle population does
            // not reach the regime where that matters: 98.8% of eligible dictionary words
            // match at most 7 words, where the fold beats concatenate-then-sort+dedup by
            // 2-5x (bench_narrow_alloc.rs, section B). Sort+dedup only pulls ahead past
            // ~20-30 matched words, which 0.2% of needles reach — see
            // docs/issues/local-engine-sparse-union-threshold.md before rewriting this.
            let sparse_text_ids = |sparse: &[u32]| -> Vec<u32> {
                let mut ids: Vec<u32> = Vec::new();
                for &s in sparse {
                    let start = u32::from(words.sparse_offsets[s as usize]) as usize;
                    let end = u32::from(words.sparse_offsets[s as usize + 1]) as usize;
                    let row: Vec<u32> = words.sparse_postings[start..end].iter().map(|x| u32::from(u16::from(*x))).collect();
                    ids = union_sorted(ids, row);
                }
                ids
            };
            match (scan.dense.as_slice(), scan.sparse.as_slice()) {
                ([], []) => Narrowed::tight(Candidates::Cards(Vec::new())),
                ([], sparse) => {
                    let text_ids = sparse_text_ids(sparse);
                    Narrowed::tight(Candidates::Cards(expand_text_ids(&indexes.oracle_trigram, &text_ids, n_cards)))
                }
                ([d], []) => {
                    let start = *d as usize * wpp;
                    let bits: Vec<u64> = words.dense_bits[start..start + wpp].iter().map(|w| u64::from(*w)).collect();
                    Narrowed::tight(Candidates::CardBits(bits))
                }
                (dense, sparse) => {
                    let mut acc = vec![0u64; wpp];
                    for &d in dense {
                        let start = d as usize * wpp;
                        for (a, w) in acc.iter_mut().zip(&words.dense_bits[start..start + wpp]) {
                            *a |= u64::from(*w);
                        }
                    }
                    for cid in expand_text_ids(&indexes.oracle_trigram, &sparse_text_ids(sparse), n_cards) {
                        acc[(cid >> 6) as usize] |= 1u64 << (cid & 63);
                    }
                    Narrowed::tight(Candidates::CardBits(acc))
                }
            }
        }

        FilterExpr::TextContains { field, word }
            if word.len() >= 3
                && matches!(field, TextSearchField::NameLower | TextSearchField::OracleTextLower) =>
        {
            // A needle of exactly 3 bytes is exactly ONE trigram, so the posting list IS the containment
            // set — no false positives to verify away. At 4+ bytes the intersection of several trigrams
            // really is a superset ("the" AND "her" without "ther"), so those stay loose. (#859)
            let mk = if word.len() == 3 { Narrowed::tight } else { Narrowed::loose };
            match field {
                TextSearchField::NameLower => trigram_candidates(&indexes.name_trigram, word).and_then(|v| mk(Candidates::Cards(v))),
                // Oracle postings are in dense text-id space (see OracleTextIndex);
                // intersect there, then expand the survivors to card indices
                // through the CSR table.
                _ => trigram_candidates(&indexes.oracle_trigram.trigrams, word)
                    .and_then(|text_ids| mk(Candidates::Cards(expand_text_ids(&indexes.oracle_trigram, &text_ids, n_cards)))),
            }
        }

        // #734 step 3: trigram-narrow a regex by its guaranteed literal factors, then let the walk
        // re-verify with the real regex. Concatenation ⇒ every factor must appear ⇒ intersect their
        // (loose) candidate sets. No usable factor (bare alternation / class-only) ⇒ None (full scan).
        // Only name/oracle carry trigram indexes; artist/flavor regexes are already bound to `*Match`.
        FilterExpr::TextRegex { field, regex } if matches!(field, TextField::NameLower | TextField::OracleTextLower) => {
            let is_name = matches!(field, TextField::NameLower);
            // Only narrow when the trigram index is actually built for this store — fixtures (and any
            // store without it) leave it `Default`, where `trigram_candidates` returns empty rather
            // than None and would wrongly narrow to zero. Fall back to the general full-scan path.
            let built = if is_name {
                u32::from(indexes.name_trigram.domain) as usize == n_cards
            } else {
                u32::from(indexes.oracle_trigram.words.n_cards) as usize == n_cards
            };
            if !built {
                return None;
            }
            let factors = regex_required_factors(regex.as_str());
            if factors.is_empty() {
                return None;
            }
            let mut acc: Option<Vec<u32>> = None;
            for f in &factors {
                let cand = if is_name {
                    trigram_candidates(&indexes.name_trigram, f)
                } else {
                    trigram_candidates(&indexes.oracle_trigram.trigrams, f)
                };
                let Some(cand) = cand else { continue }; // factor not trigram-indexable: skip, keep the rest
                // A `match`, not `map_or`: the first factor takes ownership of its
                // candidates instead of cloning the whole vec to hand `map_or` a default.
                acc = Some(match acc {
                    None => cand,
                    Some(prev) => intersect_sorted(&prev, &cand),
                });
            }
            let acc = acc?; // no factor produced candidates ⇒ general path (full scan)
            let ids = if is_name { acc } else { expand_text_ids(&indexes.oracle_trigram, &acc, n_cards) };
            Narrowed::loose(Candidates::Cards(ids))
        }

        FilterExpr::NumericCmp { lhs, op, rhs } => {
            // Card-space numeric postings are tight: every posted card
            // satisfies the comparison at card level. Rarity postings are
            // loose in the sense that matters for Not: a posted card can have
            // other printings that do NOT satisfy the comparison, so the
            // complement would wrongly exclude cards `-rarity:x` matches.
            let numeric = |idx, op, v: &f64| numeric_candidates(idx, op, *v, n_cards).and_then(|c| Narrowed::tight(Candidates::Cards(c)));
            let rarity = |op, v: &f64| narrow_rarity(indexes, n_cards, op, *v);
            // Same shape as `cn` below now that price is integer cents, not lossy f32 dollars --
            // the only price-specific step is snapping the *PRICE_CENTS_PER_DOLLAR conversion
            // against its own floating-point noise before delegating to int_range_bounds.
            // All three price fields are integer cents with identical semantics, so one closure over
            // whichever index the field selects; the only price-specific step is snapping the
            // *PRICE_CENTS_PER_DOLLAR conversion against its own floating-point noise.
            let price = |idx, op, v: &f64| match int_range_bounds(op, snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))? {
                None => Narrowed::tight(Candidates::Printings(Vec::new())),
                Some((lo, hi)) => range_narrowed(idx, lo, hi, n_printings, broad_ok, true),
            };
            let cn = |op, v: &f64| match int_range_bounds(op, *v)? {
                None => Narrowed::tight(Candidates::Printings(Vec::new())),
                Some((lo, hi)) => range_narrowed(&indexes.collector_number, lo, hi, n_printings, broad_ok, true),
            };
            match (lhs, rhs) {
                (NumExpr::Field(NumField::Cmc), NumExpr::Const(v)) => numeric(&indexes.cmc, *op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::Cmc)) => numeric(&indexes.cmc, flip_op(*op), v),
                (NumExpr::Field(NumField::Power), NumExpr::Const(v)) => numeric(&indexes.power, *op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::Power)) => numeric(&indexes.power, flip_op(*op), v),
                (NumExpr::Field(NumField::Toughness), NumExpr::Const(v)) => numeric(&indexes.toughness, *op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::Toughness)) => numeric(&indexes.toughness, flip_op(*op), v),
                (NumExpr::Field(NumField::RarityInt), NumExpr::Const(v)) => rarity(*op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::RarityInt)) => rarity(flip_op(*op), v),
                (NumExpr::Field(NumField::PriceUsd), NumExpr::Const(v)) => price(&indexes.price_usd, *op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::PriceUsd)) => price(&indexes.price_usd, flip_op(*op), v),
                (NumExpr::Field(NumField::PriceEur), NumExpr::Const(v)) => price(&indexes.price_eur, *op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::PriceEur)) => price(&indexes.price_eur, flip_op(*op), v),
                (NumExpr::Field(NumField::PriceTix), NumExpr::Const(v)) => price(&indexes.price_tix, *op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::PriceTix)) => price(&indexes.price_tix, flip_op(*op), v),
                (NumExpr::Field(NumField::CollectorNumberInt), NumExpr::Const(v)) => cn(*op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::CollectorNumberInt)) => cn(flip_op(*op), v),
                // Everything the dedicated single-field arms above didn't consume: arith expressions
                // (`cmc+1<power`), field-vs-field (`power<toughness`), and bare loyalty compares — all
                // over only card-level fields. `is_arith_tuple_route` is the shared gate (also used by
                // the negated arm and `and_child_rank`); a mixed/out-of-scope expression (`usd+1<power`)
                // fails it and declines here, falling to the existing full scan (#743).
                _ if is_arith_tuple_route(filter) => arith_tuple_narrow(filter, &indexes.arith_tuple, n_cards, Tri::True),
                _ => None,
            }
        }

        FilterExpr::Devotion { op: CmpOp::Ge | CmpOp::Gt, pips } => {
            // The exact compiler (plane arm above) declined: some queried
            // count exceeds the 2-bit saturation. The saturated bucket is a
            // superset of every deeper match — ~0.5% of cards per color — so
            // it narrows loosely and the driver verifies the real counts.
            if u32::from(indexes.planes.n_cards) as usize != n_cards || n_cards == 0 {
                return None;
            }
            let pe = compile_devotion_superset(*pips)?;
            let mut bits: Vec<u64> = Vec::new();
            eval_planes(&pe, &indexes.planes, &mut bits);
            Narrowed::loose(Candidates::CardBits(bits))
        }

        // f:x / format:x / banned:x / restricted:x (docs/issues/
        // 00667-engine-legality-divergent-carveout.md, generalized by #678 -- see
        // docs/issues/00678-engine-legality-banned-restricted-planes.md):
        // legality_candidate_bits reads the status's `_EXISTS` plane
        // directly, so this is exact card-space narrowing -- but still
        // reported `loose`, not `tight`: `tight` means true for *every*
        // printing (see the Narrowed struct's doc), and legality genuinely
        // varies printing-to-printing, so "the card has some printing with
        // this status" doesn't satisfy that contract, same reason -r:x below
        // is loose despite rarity's plane also being exact. compile_plane
        // separately exact-consumes this shape for unique=card (see
        // planes.rs, plane_expr_is_existential); this arm still matters for
        // mixed filters compile_plane declines (the shared-witness
        // 2+-distinct-fact case) and for unique=printing/artwork, where the
        // residual card_pass verification this `loose` narrowing feeds into
        // is required for correctness. Formats absent from loaded data
        // (shift: None) fall through unindexed, unchanged.
        FilterExpr::Legality { shift: Some(shift), expected } if status_plane_bases(*expected).is_some() => {
            Narrowed::loose(Candidates::CardBits(legality_candidate_bits(indexes, n_cards, *shift, *expected, false)?))
        }

        // -f:x / -banned:x / -restricted:x — matched as its own leaf shape
        // rather than falling through to the generic Not-complement below
        // (which requires a `tight` child and wouldn't apply here
        // regardless): bit-complementing the positive plane would compute
        // `∀p: ¬status(p)` (wrong for a divergent card, which can satisfy
        // both `∃p: status(p)` and `∃p: ¬status(p)` at once) instead of
        // reading the status's `_ABSENT`/`_ILLEGAL` plane directly, which is
        // what this arm does.
        // Gated on `is_legality_negation_shape` (also `and_child_rank`'s guard for this shape) rather
        // than an inline `matches!` — one definition of "is this the -f:x shape," not two that could
        // silently disagree (see that function's doc).
        FilterExpr::Not(inner) if is_legality_negation_shape(inner) => {
            let FilterExpr::Legality { shift: Some(shift), expected } = inner.as_ref() else { unreachable!() };
            Narrowed::loose(Candidates::CardBits(legality_candidate_bits(indexes, n_cards, *shift, *expected, true)?))
        }

        // -r:x / -rarity:x — same reason as -f:x above: rarity's narrowing is
        // loose (docs/issues/00670-engine-rarity-planes.md), so the generic
        // Not-complement below would (correctly) refuse it -- a posted/planed
        // card can have other printings that don't satisfy the comparison, so
        // bit-complementing the existing candidate set would wrongly drop
        // real -r:x matches (see the comment on the NumericCmp arm above).
        // This is NOT a complement: it recomputes narrowing from scratch with
        // the logically-negated operator (Not(Eq(v)) == Ne(v), verified
        // against tri()'s actual Null handling in negate_op's doc comment),
        // which is a different and correct operation.
        // Gated on `is_rarity_negation_shape` (also `and_child_rank`'s guard for this shape) rather
        // than an inline `matches!` — same single-source-of-truth reasoning as `-f:x` above.
        FilterExpr::Not(inner) if is_rarity_negation_shape(inner) => {
            let FilterExpr::NumericCmp { lhs, op, rhs } = inner.as_ref() else { unreachable!() };
            match (lhs, rhs) {
                (NumExpr::Field(NumField::RarityInt), NumExpr::Const(v)) => narrow_rarity(indexes, n_cards, negate_op(*op), *v),
                (NumExpr::Const(v), NumExpr::Field(NumField::RarityInt)) => narrow_rarity(indexes, n_cards, negate_op(flip_op(*op)), *v),
                _ => unreachable!(),
            }
        }

        // -usd<c / -cn<c / -date>c / -year>=c: same reasoning as -r:x above (NOT(x op c) ==
        // x negate_op(op) c, exact per negate_op's doc), but for the printing-range-indexed fields
        // instead of rarity's card-space postings — `bare_range_bounds` already resolves both the
        // field dispatch and the negation (see its doc), so this arm is just "ask it, then narrow or
        // prove empty," not a second implementation of the field-matching logic above. Guarded on
        // the inner shape so cmc/power/toughness (tight card-space, correctly handled by the
        // generic Not-complement below) and -r:x (the arm just above) aren't intercepted here.
        // `broad_ok` is forced `true` regardless of the caller's own value — same choice the
        // generic Not-arm below already makes for its inner check (always narrows with
        // `broad_ok: true`): negating a predicate is exactly the shape where the flipped bounds are
        // worth computing even when broad, since there's no cheaper alternative once we're already
        // committed to this field (measured: without forcing this, a broad negated range like
        // `-cn<100` — `cn>=100`, ~64% of printings — declined to a full scan and *regressed* 0.545ms
        // → 0.661ms vs. before this arm existed at all).
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. })
                && bare_range_bounds(filter, indexes).is_some() =>
        {
            let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("guarded by bare_range_bounds");
            if lo >= hi {
                Narrowed::tight(Candidates::Printings(Vec::new()))
            } else {
                range_narrowed(idx, lo, hi, n_printings, true, true)
            }
        }

        // -(arith tuple predicate), e.g. `-power+toughness<4` / `-cmc+1<power`. Same "recompute, don't
        // complement" reasoning as -r:x/-usd<c above, but for the #743 card-space joint-tuple index:
        // re-run the tiny per-combination scan collecting `Tri::False` instead of `Tri::True`, so
        // NULL-valued combinations (Tri::Null) are excluded from the negation exactly as three-valued
        // logic requires — no complement, hence none of the NULL-inclusion trap the generic Not arm
        // (below) handles by staying loose. The four fields are card-level, so a False verdict holds for
        // every printing: exact and tight. Gated on `is_arith_tuple_route(inner)` — the same single
        // source of truth the positive arm and `and_child_rank` use, so a bare `-cmc:3` (dedicated arm,
        // routes through the generic complement) is deliberately not intercepted here.
        FilterExpr::Not(inner) if is_arith_tuple_route(inner) => {
            arith_tuple_narrow(inner, &indexes.arith_tuple, n_cards, Tri::False)
        }

        // Ge/Eq/Gt all resolve `matches()` through the same `contains(value)`
        // test on the collection, per its `coll == {value}` (Eq) and proper-
        // superset (Gt) definitions — Eq and Gt are strict *subsets* of what
        // Ge (plain containment) matches, since both additionally require a
        // length condition (`len == 1` / `len > 1`) that containment alone
        // doesn't decide. So the same postings this arm already gathers for
        // `:` are a valid — if not exact — candidate superset for `=`/`>`
        // too: every Eq/Gt match is also a Ge match, so it's guaranteed to
        // show up in these postings, just alongside cards the length check
        // will still need to reject. `narrow_rec`'s driver always re-verifies
        // with `matches()` regardless of tightness (see `Candidates`'/
        // `Narrowed`'s doc comments), so declaring the postings loose for
        // Eq/Gt is enough to stay correct — no separate index for the length
        // condition is needed. (#700; Le/Lt/Ne genuinely can't reuse this —
        // they're not expressible as `contains` plus a length check.)
        FilterExpr::CollectionCmp { field, op, value, .. } if matches!(op, CmpOp::Ge | CmpOp::Eq | CmpOp::Gt) => {
            // Ge's postings are exact for Ge itself; for Eq/Gt they're only a loose superset, so the
            // residual `matches()` check the driver always runs is load-bearing for those two ops.
            let mk = |set| if matches!(op, CmpOp::Ge) { Narrowed::tight(set) } else { Narrowed::loose(set) };
            // frame_data is the one HYBRID index: every value is stored, so absence is a PROOF and a
            // dense value hands back a ready-made bitmap with no scatter.
            //
            // `broad_ok` is honoured, unlike the first attempt at this, which bypassed it for dense
            // values reasoning that a STORED bitmap has no scatter to pay. That misread the gate --
            // it means "nothing downstream would consume this usefully", not "the scatter would be
            // wasted" -- and cost 1.3-1.8x on sparse `And`s. `3cfd441` since gave the And arm a cost
            // probe for collection children, so a broad frame child under a selective driver is now
            // skipped on COST before this is even reached.
            if matches!(field, CollField::FrameData) {
                let idx = &indexes.frame_data;
                if idx.is_dense(value.as_str()) {
                    // DENSE is the storage crossover (1/32 of printings, where a bitmap gets smaller
                    // than postings). BROAD is the narrowing guard (`MAX_NARROW_FRACTION`, 1/4, where
                    // intersecting stops paying). They are different questions eight times apart, and
                    // consulting `broad_ok` for every dense value conflated them: `frame:2003` (17% of
                    // printings), `frame:1997` (11%) and `frame:legendary` (10.6%) all sit BETWEEN the
                    // two, so each was declined as if it were broad.
                    //
                    // That is the whole of the mid-density regression this index shipped with, and it is
                    // not the `probe_collection_k` space mismatch it was first blamed on. Those three
                    // values were POSTINGS before the hybrid, which took the branch below — where
                    // `range_too_broad_to_narrow` is tested first and `broad_ok` only decides the broad
                    // case. Moving them to a bitmap silently moved them behind a stricter gate.
                    //
                    // It bites hardest with a CARD-space partner, because `narrow_candidates_exact`
                    // enters with `broad_ok: false` and rank-0 children inherit it: `o:this frame:2003`
                    // narrowed to `o:this` alone (19,968 cards, identical features to `o:this
                    // border:black`) and ran 1,809 us against 52 us for `o:this` by itself — adding a
                    // predicate that CUTS the result set 6x made the query 35x slower, because the
                    // frame test became a per-printing residual instead of a bitmap AND.
                    let k = idx.len_of(value.as_str())?;
                    // `DENSE_FRAME_BROAD_GATE=0` restores the conflated gate this fixed, so the two can
                    // be measured against each other on a byte-identical archive.
                    let broad = if *DENSE_FRAME_BROAD_GATE { range_too_broad_to_narrow(k, n_printings) } else { true };
                    if broad && !broad_ok {
                        return None;
                    }
                    return mk(Candidates::PrintingBits(idx.bits(value.as_str(), n_printings)?));
                }
                // A sparse value cannot be broad: the storage crossover is 1/32 and the broadness guard
                // fires at 1/4, so these postings go straight through.
                return match idx.sparse.get(value.as_str()) {
                    None => Narrowed::tight(Candidates::Printings(Vec::new())),
                    Some(v) => mk(Candidates::Printings(v.iter().map(|x| u32::from(*x)).collect())),
                };
            }
            // Every remaining index posts every occurrence of every value, so absence is a proof. That
            // is now true of frame_data too, handled above — it was the sole exception only because its
            // dense values were dropped at build (#628).
            let (idx, card_space) = match field {
                CollField::Subtypes   => (&indexes.subtypes,    true),
                CollField::Keywords   => (&indexes.keywords,    true),
                CollField::OracleTags => (&indexes.oracle_tags, true),
                CollField::ArtTags    => (&indexes.art_tags,    false),
                CollField::IsTags     => (&indexes.is_tags,     false),
                CollField::FrameData  => unreachable!("frame_data is the hybrid index, handled above"),
            };
            match idx.get(value.as_str()) {
                // A value with no postings in a complete index proves
                // `contains(value)` false for every row, which makes Ge, Eq,
                // and Gt all provably empty alike — no row can satisfy any of
                // them without first satisfying containment. Exact for all
                // three ops, not just Ge: `is:permanent` spent 0.6 ms
                // full-scanning to return zero results.
                None => {
                    Narrowed::tight(if card_space { Candidates::Cards(Vec::new()) } else { Candidates::Printings(Vec::new()) })
                }
                Some(v) => {
                    // Broad printing-space postings pay the same gather cost
                    // the range indexes guard against (is:spell is ~60k ids);
                    // past the fraction they scatter to a bitmap when
                    // something will consume it and decline otherwise. Every
                    // posted row carries the tag (Ge tight); Eq/Gt still need
                    // the length check downstream (loose either way).
                    // Card-space lists need no guard — same argument as
                    // numeric_candidates.
                    if !card_space && range_too_broad_to_narrow(v.len(), n_printings) {
                        if !broad_ok {
                            return None;
                        }
                        let bits = scatter_bits(v.iter().map(|x| u32::from(*x)), n_printings);
                        return mk(Candidates::PrintingBits(bits));
                    }
                    let ids: Vec<u32> = v.iter().map(|x| u32::from(*x)).collect();
                    mk(if card_space { Candidates::Cards(ids) } else { Candidates::Printings(ids) })
                }
            }
        }

        FilterExpr::ArtistMatch { ids } => {
            // ids resolved at bind time; empty means no artist satisfies the
            // predicate, which proves the empty candidate set. Every expanded
            // printing carries a matching artist — tight.
            Narrowed::tight(Candidates::Printings(expand_artist_ids(&indexes.artists, ids, n_printings)))
        }

        FilterExpr::FlavorMatch { dense_ids, .. } => {
            // Resolved at bind; empty proves the empty candidate set (printings
            // without flavor evaluate to Null and can never match). Printing-
            // space candidates, so near-total match sets (e.g. `ft!=x`) fall
            // under the same broad-range guard as the price index — size the
            // expansion from the CSR offsets before materializing it.
            let flavor = &indexes.flavor;
            let total: usize = dense_ids
                .iter()
                .map(|&d| (u32::from(flavor.offsets[d as usize + 1]) - u32::from(flavor.offsets[d as usize])) as usize)
                .sum();
            if range_too_broad_to_narrow(total, flavor.printings.len()) {
                return None;
            }
            Narrowed::tight(Candidates::Printings(expand_flavor_ids(flavor, dense_ids, n_printings)))
        }

        FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value } => {
            // A set code absent from the index appears on no printing: narrowing
            // to the empty set is exact, matching the tag-index convention would
            // be None, but unlike tags the index covers every non-empty code.
            Narrowed::tight(Candidates::Printings(
                indexes.set_codes.get(value.as_str()).map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(*x)).collect()),
            ))
        }

        // watermark: (93% null, 67 distinct values, largest ~0.8% of printings —
        // sparse enough that postings is the only representation that makes
        // sense; see docs/issues/done/local-engine-watermark-postings.md). A value
        // absent from the index appears on no printing, same empty-is-exact
        // reasoning as SetCode above.
        FilterExpr::TextExact { field: TextField::Watermark, op: CmpOp::Eq, value } => {
            Narrowed::tight(Candidates::Printings(
                indexes.watermarks.get(value.as_str()).map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(*x)).collect()),
            ))
        }

        // border: (#664, promoted to an existential field reaching
        // compile_plane/all_match for tracked values by #680 -- see
        // PLANE_BORDER's doc) — loose, card-level narrowing here regardless
        // of tracked/untracked: a tracked value (black/borderless/white/gold)
        // reads its own one-hot plane; any other Known value (currently just
        // yellow) reads the shared `other` plane -- narrower than a full
        // scan even though the residual walk still has to confirm which
        // specific value it is. Only Eq is handled (compile_plane's arm
        // handles the exact tracked-value/all_match path; Not is handled by
        // narrow_rec's generic complement machinery declining, same as ever,
        // since this is loose).
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value } => {
            if u32::from(indexes.planes.n_cards) as usize != n_cards {
                return None; // archive without these planes for this store
            }
            let plane = BORDER_TRACKED_VALUES.iter().position(|&v| v == value.as_str()).map_or(PLANE_BORDER_OTHER, |idx| PLANE_BORDER + idx);
            let wpp = words_per_plane(n_cards);
            let start = plane * wpp;
            let bits: Vec<u64> = indexes.planes.words[start..start + wpp].iter().map(|w| u64::from(*w)).collect();
            Narrowed::loose(Candidates::CardBits(bits))
        }

        FilterExpr::DateCmp { op, value } => {
            let (lo, hi) = date_range_bounds(*op, *value)?;
            range_narrowed(&indexes.released_at, lo, hi, n_printings, broad_ok, true)
        }

        FilterExpr::YearCmp { op, year } => {
            let (lo, hi) = year_range_bounds(*op, *year)?;
            range_narrowed(&indexes.released_at, lo, hi, n_printings, broad_ok, true)
        }

        FilterExpr::And(children) => {
            // Combine within each id space first (card lists are ~3x shorter),
            // then cross the boundary once by projecting the printing product up.
            // Projection loses which printings matched — the driver's per-printing
            // verification restores exactness — and therefore loses tightness.
            // Cheap sources first, printing ranges second, complements last —
            // and stop entirely once any source is selective enough that the
            // driver's verification makes further narrowing pointless. Broad
            // range bitmaps only materialize when a printing-space partner
            // exists to intersect them with; complements only when nothing
            // else narrowed at all.
            // Rank children by evaluation cost, and within a rank order the
            // probeable range children most-selective-first (smallest k). The
            // probe is two binary searches (probe_range_k), nearly free, and it
            // both shrinks the driver as fast as possible and removes the old
            // sensitivity to the order same-rank children happened to be written
            // in. Non-range children (no probe) sort after the ranges in-rank.
            // Same-index range children fuse into one interval first (`fuse_and_range_children`),
            // because two individually-broad halves can intersect to something sparse and this arm
            // only ever intersects narrowing *results*, never the bounds.
            // `sort_k` orders within a rank; `size_k` is the cost-comparison input for the skip below.
            // They are separate because collection children only just gained a probe: feeding it into
            // the sort as well would reorder rank-0 children in the same change that starts skipping
            // them, and the two effects could not then be attributed.
            let mut ranked: Vec<(u8, Option<usize>, Option<usize>, AndSource)> = fuse_and_range_children(children, indexes, true)
                .into_iter()
                .map(|src| match src {
                    AndSource::Child(c) => {
                        let rank = and_child_rank(c, indexes);
                        let sort_k = if rank == 1 { probe_range_k(c, indexes) } else { None };
                        // A rank-0 child is assumed CHEAP to materialize, and for a plane or a short
                        // posting list it is. A containment collection can be enormous (`is:spell` is
                        // ~60k printing ids), and `frame_data`'s dense values more so, so probe it and
                        // let the same cost rule apply.
                        let size_k = sort_k.or_else(|| probe_collection_k(c, indexes));
                        (rank, sort_k, size_k, AndSource::Child(c))
                    }
                    // A fused interval is a printing range like any other — rank 1, and its probe is
                    // the `k` its own broad-check already computed.
                    AndSource::FusedRange { k, .. } => (1, Some(k), Some(k), src),
                })
                .collect();
            ranked.sort_by_key(|(r, sort_k, _, _)| (*r, sort_k.unwrap_or(usize::MAX)));
            let mut card_sets: Vec<Narrowed> = Vec::new();
            let mut printing_sets: Vec<Narrowed> = Vec::new();
            // Tightness of the And requires every child to be represented in
            // the intersection: a member of a partial intersection need not
            // satisfy the skipped children, and a complement taken over a
            // falsely-tight set would drop real matches of the negation.
            let mut every_child_included = true;
            // Smallest set pushed so far, maintained as they are pushed. Recomputing it
            // per child meant popcounting every accumulated bitmap again on every
            // iteration — O(children² × words) for a value that only ever shrinks.
            let mut best: Option<usize> = None;
            // Which children the returned set will PROVE, by their index in `children` as written. A
            // child qualifies when its own narrowing came back tight AND card-space, and it actually got
            // pushed: `and_all` intersects, and an intersection is a subset of each of its inputs, so
            // every card in the result satisfies each such child. See `Narrowed::proven`.
            //
            // Indices are recovered by pointer identity rather than by tracking position through
            // `fuse_and_range_children`'s regrouping and the sort below — those reorder freely, and a
            // positional guess that silently slipped would drop a real predicate from the residual.
            // Children past 64 are simply never marked, which costs a re-verification and cannot be wrong.
            let mut proven: u64 = 0;
            for (rank, _sort_k, size_k, src) in ranked {
                let child_index = match &src {
                    AndSource::Child(c) => children.iter().position(|w| std::ptr::eq(w, *c)),
                    // A fused interval stands for two or more children at once, and is printing-space
                    // besides, so it can never be proven.
                    AndSource::FusedRange { .. } => None,
                };
                // A driver this selective already bounds the candidate set the
                // residual re-verifies, so a costlier (rank>0) child usually
                // narrows nothing the driver's verification doesn't already do
                // — skip it. The exception the probe buys: a range child whose
                // *actual* match count k (already computed, free) beats the
                // driver (k < best ⇒ it becomes the new, strictly smaller
                // driver — never a regression) or is under AND_PROBE_FLOOR
                // (materializing its sorted vec is timing noise). Such a child
                // is always sparse under a selective driver, so it only ever
                // takes range_narrowed's cheap vec path. `continue`, not
                // `break`: a later, smaller-k range child may still qualify.
                //
                // The `rank > 0` this used to carry has become "we know what the child costs": a
                // probed child is judged on its real `k` at ANY rank, because the decision is a cost
                // comparison and rank is only a proxy for cost. A rank-0 child whose `k` exceeds the
                // driver would be materialized and intersected — O(k) — purely to filter a set already
                // smaller than itself, which is the shape `broad_ok` was introduced to prevent and
                // which rank-0 children were never checked against. An UNPROBED rank-0 child keeps its
                // benefit of the doubt, so planes and short postings behave exactly as before.
                let unprobed_cheap = rank == 0 && size_k.is_none();
                if let Some(b) = best.filter(|&b| b <= *AND_SKIP_THRESHOLD)
                    && !unprobed_cheap
                    && !size_k.is_some_and(|k| k < b || k <= *AND_PROBE_FLOOR)
                {
                    every_child_included = false;
                    continue;
                }
                if rank == 2 && !(card_sets.is_empty() && printing_sets.is_empty()) {
                    every_child_included = false;
                    continue; // complements are broad; they only pay as the sole source
                }
                let child_broad_ok = match rank {
                    1 => !printing_sets.is_empty(),
                    _ => broad_ok,
                };
                let narrowed = match src {
                    AndSource::Child(c) => narrow_rec(c, indexes, offsets, cards, child_broad_ok),
                    // `range_narrowed` is what every unfused range child reaches too, with the same
                    // `exact: true` (the bounds come from the same `int_range_bounds`/
                    // `date_range_bounds`/`year_range_bounds` derivations).
                    AndSource::FusedRange { idx, lo, hi, .. } => range_narrowed(idx, lo, hi, n_printings, child_broad_ok, true),
                };
                if let Some(n) = narrowed {
                    // A child covering most of its domain barely narrows the
                    // intersection; skipping it is advisory-sound and avoids
                    // paying its projection/materialization for ~nothing.
                    let domain = if n.set.is_printing_space() { n_printings } else { n_cards };
                    let len = n.set.len();
                    if len > domain - domain / 4 {
                        every_child_included = false;
                        continue;
                    }
                    best = Some(best.map_or(len, |b| b.min(len)));
                    if let Some(i) = child_index.filter(|&i| i < 64)
                        && n.tight
                        && !n.set.is_printing_space()
                    {
                        proven |= 1 << i;
                    }
                    if n.set.is_printing_space() { printing_sets.push(n) } else { card_sets.push(n) }
                } else {
                    every_child_included = false;
                }
            }
            let cards = and_all(card_sets);
            let printings = and_all(printing_sets);
            // `proven` rides only on results that CONTAIN the card side. Every branch below that returns
            // the card intersection (alone, or intersected with a projected printing side) is a subset of
            // each proven child's set; the lone-printing branch is not, and gets 0.
            let seal = |mut n: Narrowed| {
                n.tight &= every_child_included;
                n.proven = proven;
                n
            };
            match (cards, printings) {
                (None, None) => None,
                (Some(c), None) => Some(seal(c)),
                (None, Some(p)) => {
                    // A lone broad printing-space bitmap is not worth crossing
                    // the space boundary for: the projection walks every set
                    // bit and the projected set barely shrinks the card walk —
                    // measured as a wash at best against the scan it replaces.
                    // Sparse results (vecs, and bitmaps under a quarter of the
                    // space) project as before.
                    match &p.set {
                        Candidates::PrintingBits(_) if p.set.len() > n_printings / 4 => None,
                        // No card side, so nothing here proves a card-space child -- and there cannot be
                        // one, since a pushed card set would have produced `cards`. `proven` is 0 either
                        // way; sealed through the same closure so the two branches cannot drift.
                        _ => Some(Narrowed { proven: 0, ..seal(p) }),
                    }
                }
                (Some(c), Some(p)) => {
                    // With a card-side result in hand, a broad printing-side
                    // bitmap adds little and costs its projection — keep the
                    // card side alone. Sparse printing results still intersect.
                    match &p.set {
                        Candidates::PrintingBits(_) if p.set.len() > n_printings / 4 => {
                            // The dropped printing side's children are now
                            // unrepresented — the card result cannot stay tight.
                            Some(Narrowed { tight: false, ..seal(c) })
                        }
                        _ => {
                            let pc = p.into_card_space(offsets, &indexes.printing_to_card);
                            and_all(vec![c, pc]).map(seal)
                        }
                    }
                }
            }
        }

        FilterExpr::Or(children) => {
            // Every child must narrow or the union is unbounded — with one big
            // change from the vec-only days: broad children (guard-declined
            // ranges, color/type planes) now produce bitmaps instead of None,
            // so an individually-broad child no longer vetoes its selective
            // siblings. Mixed spaces union in card space (projection up is
            // loosening-only, and the driver re-verifies).
            let mut sets: Vec<Narrowed> = Vec::with_capacity(children.len());
            for child in children {
                let n = narrow_rec(child, indexes, offsets, cards, true)?;
                // One near-total child makes the union near-total: the
                // \"candidates\" would visit almost every card while paying
                // union, projection, and materialization on the way.
                let domain = if n.set.is_printing_space() { n_printings } else { n_cards };
                if n.set.len() > domain - domain / 4 {
                    return None;
                }
                sets.push(n);
            }
            if sets.iter().all(|s| s.set.is_printing_space()) {
                or_all(sets, n_printings)
            } else {
                // Projection amplifies density ~3x (multiple printings per
                // card), so a broad printing bitmap would blanket card space:
                // the union cannot narrow, and the projection walk would be
                // paid on the way to the near-total drop.
                if sets
                    .iter()
                    .any(|s| matches!(s.set, Candidates::PrintingBits(_)) && s.set.len() > n_printings / 4)
                {
                    return None;
                }
                let sets = sets.into_iter().map(|s| s.into_card_space(offsets, &indexes.printing_to_card)).collect();
                or_all(sets, n_cards)
            }
        }

        FilterExpr::Not(inner) => {
            // Complement is only sound through a tight child: every member of a
            // tight set satisfies the inner predicate, so the complement
            // contains every element the negation can match. Complementing a
            // loose superset would exclude real matches. The result is loose —
            // elements where the inner predicate is Null (which the negation
            // also does not match) are over-included, and the driver verifies.
            // Cheap static pre-reject: only compute the child's set when its
            // shape could possibly be tight. Loose-by-construction sources
            // (trigram supersets, rarity existence, nested complements) and
            // mixed-space compositions (projection always loosens) would only
            // be computed to be discarded — sometimes at real cost (a
            // mixed-space Or pays vec sorts and a projection).
            tight_narrow_space(inner)?;
            let n = narrow_rec(inner, indexes, offsets, cards, true)?;
            if !n.tight {
                return None;
            }
            let (printing_space, domain) = (n.set.is_printing_space(), if n.set.is_printing_space() { n_printings } else { n_cards });
            let mut bits = n.set.into_bits(domain);
            complement_bits(&mut bits, domain);
            // The complement of a tight set is EXACT whenever the inner predicate can never evaluate
            // `Tri::Null` — the one thing the loose marking was protecting against, since a Null card
            // satisfies neither the predicate nor its negation but lands in the complement anyway.
            //
            // That matters most where the inner set is SPARSE: its complement is then ~the whole corpus,
            // which the breadth guard discards for a loose set, so `-name:q` fell back to a full scan
            // with `card_pass` on every card (463 us) while `-name:e` -- complement of a DENSE set, so
            // only 16% of cards -- stayed under the guard and cost 76 us. Marking the exact case tight
            // keeps the broad complement (via #860's tight exemption) and skips verification entirely.
            //
            // Printing space is excluded: `into_card_space` drops tightness on projection, so a tight
            // printing-space complement would not survive to the walk as one anyway.
            if !printing_space && never_null(inner) {
                return Narrowed::tight(Candidates::CardBits(bits));
            }
            Narrowed::loose(if printing_space { Candidates::PrintingBits(bits) } else { Candidates::CardBits(bits) })
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

/// The `unique` string → `Mode` mapping, in one place. Anything other than the
/// literal `"artwork"`/`"printing"` is `Mode::Card` (not just the literal
/// `"card"`) — see `split_planes`'s `unique_is_card` doc. Shared by `run_query`,
/// `run_query_with_plan`, `explain_analyze`, and the PyO3 `explain` method so
/// they can never drift.
fn mode_from_unique(unique: &str) -> Mode {
    match unique {
        "artwork"  => Mode::Artwork,
        "printing" => Mode::Printing,
        _          => Mode::Card,
    }
}

/// Prefer score for one printing of a card; higher wins, and selection uses a
/// strict > so the first-in-store-order printing wins ties (matching the tie
/// behavior of the dedup paths this replaced).
fn prefer_score(card: &AOracleCard, p: &APrinting, prefer: Prefer) -> f64 {
    match prefer {
        Prefer::Oldest  => -(p.released_at_int.as_ref().map(|v| u32::from(*v)).unwrap_or(99_999_999) as f64),
        Prefer::Newest  => p.released_at_int.as_ref().map(|v| u32::from(*v)).unwrap_or(0) as f64,
        Prefer::UsdLow  => -p.price_usd.as_ref().map(|v| f64::from(u32::from(*v)) / 100.0).unwrap_or(f64::INFINITY),
        Prefer::UsdHigh => p.price_usd.as_ref().map(|v| f64::from(u32::from(*v)) / 100.0).unwrap_or(0.0),
        // Card-level (edhrec is oracle-scoped): every printing ties, so the
        // first printing in store order is chosen — same as before the split.
        Prefer::Promo   => -(card.edhrec_rank.as_ref().map(|r| u32::from(*r) as f64).unwrap_or(f64::INFINITY)),
        Prefer::Default => p.prefer_score.as_ref().map(|v| f32::from(*v)).unwrap_or(0.0) as f64,
    }
}

#[derive(Clone, Copy)]
enum SortCol { Cmc, Power, Toughness, Rarity, PriceUsd, Cubecobra, EdhrecRank, Name }

fn orderby_to_col(orderby: &str) -> SortCol {
    match orderby {
        "cmc"       => SortCol::Cmc,
        "power"     => SortCol::Power,
        "rarity"    => SortCol::Rarity,
        "toughness" => SortCol::Toughness,
        "usd"       => SortCol::PriceUsd,
        "cubecobra" => SortCol::Cubecobra,
        "name"      => SortCol::Name,
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
        SortCol::Cmc        => card.cmc.as_ref().map(|v| f32::from(*v)),
        SortCol::Power      => card.creature_power.as_ref().map(|v| f32::from(*v)),
        SortCol::Toughness  => card.creature_toughness.as_ref().map(|v| f32::from(*v)),
        SortCol::Rarity     => p.card_rarity_int.as_ref().map(|v| f32::from(*v)),
        // Raw cents, not dollars -- order-preserving either way (this is a sort key, not an
        // exposed value), and cents fit exactly in f32 (max real price 514,202 cents, f32
        // represents any integer up to 2^24 exactly), so skip the /100.0 dollars conversion.
        SortCol::PriceUsd   => p.price_usd.as_ref().map(|v| u32::from(*v) as f32),
        SortCol::Cubecobra  => card.cubecobra_score.as_ref().map(|v| f32::from(*v)),
        SortCol::EdhrecRank => card.edhrec_rank.as_ref().map(|v| u32::from(*v) as f32),
        SortCol::Name       => Some(u32::from(card.name_rank) as f32),
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

/// The page comparator (`select_page`'s order): sort key, then pid. pid is unique,
/// so this is a total order over `Match`.
fn page_cmp(a: &Match, b: &Match) -> std::cmp::Ordering {
    // Keys 1-2 only (`>> 32` drops key 3, prefer_score), then CARD, then printing.
    //
    // Key 3 must not decide across cards, because the two sides cannot agree on it. The prebuilt
    // permutation bakes in `printings[offsets[i]]`'s prefer_score -- the first STORED printing, chosen
    // without knowledge of any filter -- while this path uses the first MATCHING printing. Whenever a
    // card's preferred printing fails the filter the two disagree, and the row order flips depending
    // on which plan ran. Same query, different page, different plan: a page boundary could then repeat
    // or skip a row.
    //
    // `cid` is filter-independent and total, so both sides can reach it. Within a card this changes
    // nothing: printings are stored prefer-desc, so ascending pid already IS descending prefer_score.
    (a.0 >> 32).cmp(&(b.0 >> 32)).then_with(|| a.1.cmp(&b.1)).then_with(|| a.2.cmp(&b.2))
}

/// Number of matches the gather buffer may grow *past* the page (`offset+limit`)
/// before it is pruned back down (see `exec_gathered_scan`). Bounds the buffer at
/// ~`page + CHUNK` ≈ 128KB of `Match` (32 B each — `u128`'s 16-byte alignment pads
/// the trailing two `u32`s) — L2-resident — on whole-corpus results, while leaving
/// any gather that never reaches this size byte-for-byte the un-pruned path.
/// Amortized O(1)/match (a prune every CHUNK matches costs O(page+CHUNK)).
const GATHER_PRUNE_CHUNK: usize = 4096;

/// Discard all but the `k` smallest matches (by `page_cmp`) in place, and return a
/// **cutoff**: the `(k+1)`-th smallest, i.e. a value every kept match is `<`. Those
/// dropped are `>=` the cutoff, so they can never enter a page of size `<= k` —
/// pruning preserves the true k-smallest of everything seen so far. The cutoff lets
/// the caller reject later matches `>= cutoff` before they bloat the buffer; it only
/// tightens across prunes (the k-th smallest is monotone non-increasing). Returns
/// `None` when there was nothing to prune (`len <= k`), i.e. no cutoff yet exists.
fn prune_to_smallest(v: &mut Vec<Match>, k: usize) -> Option<Match> {
    if v.len() > k {
        v.select_nth_unstable_by(k, page_cmp);
        let cutoff = v[k];
        v.truncate(k);
        Some(cutoff)
    } else {
        None
    }
}

/// Quickselect the page `[offset, offset+limit)` into position and sort only that
/// segment. The first select bounds the page from above (everything past it stays
/// unsorted); the second bounds it from below and is skipped in the common
/// offset == 0 case. O(n + limit·log limit) instead of O(n·log n).
fn select_page(mut v: Vec<Match>, offset: usize, limit: usize) -> Vec<(u32, u32)> {
    let end = offset.saturating_add(limit).min(v.len());
    if offset >= end {
        return Vec::new();
    }
    // `page_cmp` itself, not a copy of it. This was an inlined duplicate, which is exactly the
    // drift `page_cmp`'s doc warns about -- one of the two got the cid tiebreak and the other did not.
    if end < v.len() {
        v.select_nth_unstable_by(end, page_cmp);
    }
    if offset > 0 {
        v[..end].select_nth_unstable_by(offset, page_cmp);
    }
    v[offset..end].sort_unstable_by(page_cmp);
    v.truncate(end);
    v.drain(..offset);
    v.into_iter().map(|(_, c, p)| (c, p)).collect()
}

/// Streaming, memory-bounded page selector for the gather path. A producer appends a
/// card's matches (in any order) into `buf()`, then calls `absorb()`: it counts the
/// batch, drops matches that can't reach the page (`>= cutoff`), and once the buffer
/// grows a `GATHER_PRUNE_CHUNK` past the page size `k` prunes it back to the `k`
/// smallest, tightening `cutoff` (the k-th smallest, monotone non-increasing) so
/// later out-of-page matches are rejected as produced. `finish()` returns the exact
/// total and the page.
///
/// Equivalent — identical total, identical page — to pushing every match and calling
/// `select_page` once, but the buffer stays ~`k` instead of O(matches). A gather that
/// never reaches `prune_at` is byte-for-byte that un-pruned path. Verified against the
/// naive reference in `gather_select_matches_reference` (adversarial orderings, forced
/// multi-prune) and end-to-end by `fuzz_row_identity_matches_reference`.
struct GatherSelect {
    /// The `k` smallest matches seen so far (or every match, before the first prune).
    best: Vec<Match>,
    /// Exact count of every match absorbed (pre-drop) — the page-independent total.
    total: usize,
    /// The k-th smallest match seen so far; every kept match is `< cutoff`. `None`
    /// until the first prune.
    cutoff: Option<Match>,
    /// Page size (`offset + limit`): the count of smallest matches worth keeping.
    k: usize,
    /// Buffer length that triggers a prune (`k + GATHER_PRUNE_CHUNK`).
    prune_at: usize,
}

impl GatherSelect {
    fn new(offset: usize, limit: usize) -> Self {
        let k = offset.saturating_add(limit);
        Self { best: Vec::new(), total: 0, cutoff: None, k, prune_at: k.saturating_add(GATHER_PRUNE_CHUNK) }
    }

    /// The buffer a producer appends a card's matches into directly (no scratch).
    fn buf(&mut self) -> &mut Vec<Match> {
        &mut self.best
    }

    /// Absorb the batch appended to `buf()` since it had length `before`: count it,
    /// drop the tail's matches `>= cutoff` (compacting in place), then prune to `k`
    /// and tighten `cutoff` if the buffer has grown past `prune_at`.
    fn absorb(&mut self, before: usize) {
        self.total += self.best.len() - before;
        if let Some(c) = self.cutoff {
            let mut w = before;
            for r in before..self.best.len() {
                if page_cmp(&self.best[r], &c) == std::cmp::Ordering::Less {
                    self.best[w] = self.best[r];
                    w += 1;
                }
            }
            self.best.truncate(w);
        }
        if self.best.len() >= self.prune_at {
            self.cutoff = prune_to_smallest(&mut self.best, self.k);
        }
    }

    /// The exact total absorbed and the page `[offset, offset+limit)`.
    fn finish(self, offset: usize, limit: usize) -> (usize, Vec<(u32, u32)>) {
        (self.total, select_page(self.best, offset, limit))
    }
}

// ─── Query context & parameters (#757) ───────────────────────────────────────
// Every function from here down used to thread the same two arg clusters
// individually: the five store/index slices off the archive, and the six scalars
// describing the request. Neither varies within a call chain, and between them
// they outnumbered the args that actually differ per call better than 2:1. These
// two structs group them, so a signature's remaining positional args ARE its
// interesting inputs. Purely a grouping — `QueryCtx` is shared refs under one
// lifetime, `QueryParams` is six `Copy` scalars; both compile to the same
// argument passing as before.

/// What came off the archive: the mmap'd store slices and index bundle, borrowed
/// for the query's duration. Built once per PyO3 entry point right after
/// `access_unchecked` (`QueryCtx::from(data)`), then threaded as one arg.
///
/// The `'a` lifetime is what the executors' return-borrow relationships hang off
/// (`Vec<(&'a AOracleCard, &'a APrinting)>` — a page borrows the store it came
/// from), so a single lifetime across all five fields is load-bearing, not
/// incidental tidiness.
#[derive(Clone, Copy)]
struct QueryCtx<'a> {
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &'a AOffsets,
    strings: &'a AStrings,
    indexes: &'a Archived<CardIndexes>,
}

impl<'a> From<&'a Archived<CardData>> for QueryCtx<'a> {
    fn from(data: &'a Archived<CardData>) -> Self {
        QueryCtx {
            cards: &data.cards,
            printings: &data.printings,
            offsets: &data.offsets,
            strings: &data.strings,
            indexes: &data.indexes,
        }
    }
}

impl QueryCtx<'_> {
    /// `cards.len()`/`printings.len()` as the `u32`s the cost model and the
    /// bitmap/permutation code want; spelled out at enough call sites to be worth
    /// naming.
    fn n_cards(&self) -> u32 {
        self.cards.len() as u32
    }

    fn n_printings(&self) -> u32 {
        self.printings.len() as u32
    }
}

/// What the request asked for: the query parameters that are fixed for one
/// query but vary between queries. Deliberately separate from [`QueryCtx`] —
/// "came off the archive" and "the caller asked for it" are different kinds of
/// thing, and only the former is tied to the archive's lifetime.
///
/// Not every callee uses every field (`acquire_plan_features`/`explain` never
/// need `prefer`; `prepare_candidates` uses only `mode`). Passing the whole
/// struct and ignoring the rest is the deliberate trade: one uniform param type
/// beats five bespoke sub-structs for a layer whose whole problem was too many
/// distinct shapes.
///
/// `filter` is NOT a field here even though it is equally per-query:
/// `prepare_candidates` needs `&mut FilterExpr`, and `explain_analyze` clones a
/// fresh filter per (plan, round) off a pristine snapshot for timing fairness
/// (#752). Both want it as its own arg with its own mutability.
#[derive(Clone, Copy)]
struct QueryParams {
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    /// What the filter says about the SORT COLUMN, which `StreamedSelect` turns into the segment of the
    /// permutation its walk has to cover. Not a user parameter — it is derived from the filter — but it
    /// travels here because it is only meaningful next to `sort_col`/`descending`, and because the point
    /// where it can be extracted (before `split_planes`) is nowhere near the executor that uses it.
    ///
    /// Defaults to `UNBOUNDED` in `from_strs`, and every constructor that does not opt in gets that: a
    /// missing bound costs a longer walk, never a wrong page. `with_sort_bound` is the opt-in.
    sort_bound: SortBound,
}

impl QueryParams {
    /// The string→enum adapter, in one place. The PyO3 surface takes `unique`/
    /// `prefer`/`orderby`/`direction` as strings (Scryfall's query-param
    /// spelling); this is the single boundary where they become enums, so
    /// `run_query`, `run_query_with_plan`, `explain_analyze`, and the `explain`
    /// method can't drift in how they interpret them — the four-line
    /// `orderby_to_col`/`== "desc"`/`prefer_from_str`/`mode_from_unique` block
    /// each used to repeat.
    fn from_strs(unique: &str, prefer: &str, orderby: &str, direction: &str, limit: usize, page_offset: usize) -> Self {
        QueryParams {
            mode: mode_from_unique(unique),
            prefer: prefer_from_str(prefer),
            sort_col: orderby_to_col(orderby),
            descending: direction == "desc",
            limit,
            page_offset,
            sort_bound: SortBound::UNBOUNDED,
        }
    }

    /// Attach the filter's interval on the sort column. Called at the PyO3 boundary, next to
    /// `bind_and_split_filter`, since that is the last place the unsplit filter exists.
    fn with_sort_bound(mut self, sort_bound: SortBound) -> Self {
        self.sort_bound = sort_bound;
        self
    }
}

// ─── Query driver ─────────────────────────────────────────────────────────────
// One structural walk replaces the pre-split linear/hashmap dedup paths and the
// preferred-printing fast path: grouping is the store's shape, not something to
// reconstruct per query. Per candidate card the filter is evaluated once at card
// level; only when it depends on printing-level fields (Tri::PrintingDep) are the
// card's printings evaluated individually.
//
// Selection runs in one of two shapes:
//
// - Gathered (the pre-#619 path): every match gets a sort key pushed into a
//   Vec and select_page quickselects the page. Kept for the printing-keyed
//   orderbys (rarity, usd) and for small match counts, where it is exact and
//   already microseconds.
// - Streamed: a match phase records per-card match counts (total = their sum,
//   exact), then the orderby's precomputed permutation is walked, skipping
//   counts until page_offset is consumed and emitting only page cards. No
//   sort keys, no quickselect, and the prefer walk runs on ~limit cards
//   instead of every match — the emission cost measured at 47-65% of broad
//   non-default-prefer/artwork queries disappears. The match phase stays
//   sequential (the #609-measured ~2x random-access penalty is why evaluation
//   never happens in permutation order).

#[derive(Clone, Copy)]
enum Mode { Card, Artwork, Printing }

/// Upper bound on distinct artwork groups for any one card, sized for
/// `seen_words`'s fixed-size bitmask (below). Checked against the real corpus:
/// the max is 385 (Mountain; Island/Plains/Forest/Swamp all in the 365-375
/// range), so 512 bits (8 u64 words) gives ~33% headroom over today's actual
/// worst case while staying tiny (64 bytes) -- a stack array, not a heap
/// allocation, and small enough that a full `fill(0)` every card is cheaper
/// than the growable Vec's per-printing resize-check it replaces. Revisit if
/// a future card's reprint count actually approaches this -- checked once
/// per card in `assign_artwork_groups` (load time, not the per-query hot
/// path this bound protects) via a real `assert!`, not `debug_assert!`: the
/// check is free either way (once per card at load, not once per printing
/// per query), so there's no reason to let a release build skip it and
/// silently under-count in production instead of failing loudly on reload.
const ARTWORK_GROUP_WORDS: usize = 8;

/// Matches this card contributes, and how many printings it had to look at to know:
/// `(matches, examined)`. 0/1 matches for Card mode (existence, short-circuit),
/// passing printings for Printing mode, distinct illustrations with a passing
/// printing for Artwork mode. `seen_words` is a reused scratch buffer: a
/// fixed-size bitmask indexed by each printing's dense `artwork_group_id`
/// (#629), one bit per group, `word = gid / 64` -- zeroed in full every card
/// (cheap: ARTWORK_GROUP_WORDS is tiny), never resized.
///
/// `examined` is the number of times the per-printing loop body ran, which is the quantity
/// `PlanFeatures::scan_units` claims to predict. It is NOT `end - start`: every Card-mode path here
/// short-circuits, and the two `all_match` arms return without touching a printing at all. The
/// `printing_span` counter used to be incremented as the full span by the CALLER, before this
/// function ran, so it reported what a full scan would have cost rather than what happened -- and
/// `cost.rs`'s "the `printing_span` counter shows the scan plans walk the full printing span of
/// their candidates in CARD mode too, not one row each" was inferred from exactly that. Returning the
/// truth from the only place that knows it is what makes the claim checkable.
// #676 review: a legality leaf promoted into `plane` alongside a genuinely
// printing-dependent residual (DateCmp, ArtistMatch, ...) needs *both*
// checked against the *same* printing -- `all_match`/`residual_matches` alone
// only proves the residual holds for some printing, `existential_plane` alone
// only proves the plane's existential leaf holds for some (possibly
// different) printing. Neither implies a single printing satisfies both, so
// `format:A AND date>X` (unique=card) must not count/match unless some
// printing is *both* legal-in-A and past the cutoff. `existential_plane` is
// only ever `Some` for `Mode::Card` (see its computation in `run_query`), so
// `Mode::Printing`/`Artwork` below are unaffected -- their planes, if any,
// were never folded to begin with when existential (`unique_is_card`).
#[allow(clippy::too_many_arguments)]
// needless_range_loop: `pid` is the printing's absolute id, used for `artwork_group_id`
// lookups alongside the residual test — a slice iterator would lose the id.
#[allow(clippy::needless_range_loop)]
#[inline(always)]
fn card_match_count(
    card: &AOracleCard,
    cid: u32,
    printings: &[APrinting],
    artwork_group_col: &Archived<Vec<u16>>,
    start: usize,
    end: usize,
    all_match: bool,
    residual: &[&FilterExpr],
    residual_is_or: bool,
    mode: Mode,
    strings: &AStrings,
    existential_plane: Option<(&PlaneExpr, &Archived<BitPlanes>)>,
    seen_words: &mut [u64; ARTWORK_GROUP_WORDS],
) -> (u32, u32) {
    // No existential plane: identical code shape to before #676's
    // existential_plane parameter existed at all -- no closure, no extra
    // branch inside the hot loop. This is the overwhelmingly common case
    // (every query without a promoted legality leaf), and it's called once
    // per *candidate*, not once per emitted row, so its cost is on the
    // critical path for every non-Step-2 query. A prior version of this
    // function routed both cases through one closure-based `satisfies`
    // helper regardless of `existential_plane`; measured as a real (~15%)
    // regression on `banned:modern`/`restricted:vintage` (full-candidate-set
    // scans, unaffected by `existential_plane` in outcome but paying its
    // indirection anyway) via the broad survey, not the targeted benchmark --
    // isolating the fast path here restores it.
    let Some((pe, planes)) = existential_plane else {
        return match mode {
            Mode::Card => {
                if all_match {
                    // Existence is settled by the span being non-empty; no printing is read.
                    return (u32::from(start < end), 0);
                }
                for (i, p) in printings[start..end].iter().enumerate() {
                    if FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) {
                        return (1, i as u32 + 1); // stopped here: i+1 printings looked at
                    }
                }
                (0, (end - start) as u32) // no match: the whole span was ruled out
            }
            Mode::Printing => {
                if all_match {
                    // The count is the span itself, arithmetic only -- no printing is read.
                    return ((end - start) as u32, 0);
                }
                let mut n = 0u32;
                for p in &printings[start..end] {
                    if FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) {
                        n += 1;
                    }
                }
                (n, (end - start) as u32)
            }
            Mode::Artwork => {
                // #737's skip-repped shortcut, which landed in the gather loop but not here. Read the
                // group id FIRST -- from the columnar array, so an already-counted group never touches
                // the wide APrinting struct -- and skip before the residual. This loop only COUNTS
                // distinct groups, so once a bit is set no later printing of that group can change the
                // answer and testing the residual again is pure waste. Strictly safer than the
                // gather's version, which had to assume prefer-desc ordering to pick a representative;
                // counting needs no ordering assumption and so has no custom-prefer carve-out.
                seen_words.fill(0);
                for pid in start..end {
                    let gid = u16::from(artwork_group_col[pid]) as usize;
                    let (word, bit) = (gid / 64, 1u64 << (gid % 64));
                    if seen_words[word] & bit != 0 {
                        continue;
                    }
                    if !all_match
                        && !FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or)
                    {
                        continue;
                    }
                    seen_words[word] |= bit;
                }
                // Artwork never short-circuits: a later printing can always rep an unseen group.
                (seen_words.iter().map(|w| w.count_ones()).sum(), (end - start) as u32)
            }
        };
    };

    // Existential plane present (Mode::Card only -- see this function's
    // doc): the blind all_match shortcut never applies, and both the
    // residual and the plane must hold for the same printing.
    let satisfies =
        |pid: usize| eval_plane_expr_for_printing(pe, planes, cid, &printings[pid], strings)
            && (all_match || FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or));
    match mode {
        Mode::Card => {
            for pid in start..end {
                if satisfies(pid) {
                    return (1, (pid - start) as u32 + 1);
                }
            }
            (0, (end - start) as u32)
        }
        Mode::Printing => {
            let mut n = 0u32;
            for pid in start..end {
                if satisfies(pid) {
                    n += 1;
                }
            }
            (n, (end - start) as u32)
        }
        Mode::Artwork => {
            // Same skip-repped shortcut as the no-plane arm above, and it matters more here:
            // `satisfies` evaluates a plane expression per printing on top of the residual.
            seen_words.fill(0);
            for pid in start..end {
                let gid = u16::from(artwork_group_col[pid]) as usize;
                let (word, bit) = (gid / 64, 1u64 << (gid % 64));
                if seen_words[word] & bit != 0 || !satisfies(pid) {
                    continue;
                }
                seen_words[word] |= bit;
            }
            (seen_words.iter().map(|w| w.count_ones()).sum(), (end - start) as u32)
        }
    }
}

/// Emit this card's matches as (sort key, cid, pid) tuples — the per-card body
/// of the gathered path, shared by the streamed path for page cards.
///
/// Returns how many printings the loop body ran on, the same `examined` quantity
/// `card_match_count` reports and for the same reason: `scan_units` claims to predict it, and the
/// span the caller used to add in its place is not it. Card mode with the default prefer stops at the
/// first qualifying printing (one, not the span); a custom prefer must score every printing, so there
/// the span IS the truth. That difference is invisible to a caller-side `end - start`, and it is
/// exactly the difference the cost model needs to see.
///
/// `existential_plane`: `Some((plane, planes))` iff `mode` is `Card` and the
/// plane driving `all_match` touched a legality leaf
/// (docs/issues/00667-engine-legality-divergent-carveout.md "Row selection for
/// unique=card") — `all_match`/`residual` there only prove *some* printing
/// satisfies the residual, not that it's the same printing the plane's
/// existential leaf is true for, so the chosen printing must satisfy *both*
/// checked against each other, not either one alone (a legality leaf ANDed
/// with a genuinely printing-dependent residual like `DateCmp` needs one
/// printing past the cutoff *and* legal at once — checking only the plane
/// missed this, caught in #676's review). `None` (the overwhelmingly common
/// case) keeps today's behavior exactly: `Mode`s other than `Card` never hit
/// this (their planes are never folded this way, see `unique_is_card`), and a
/// card-invariant `all_match` needs no check (every printing already agrees).
#[allow(clippy::too_many_arguments)]
// needless_range_loop: `pid` is the printing's identity, not a cursor — it is pushed into
// the emitted match tuple (`pid as u32`), so the loop needs the absolute index.
#[allow(clippy::needless_range_loop)]
#[inline(always)]
fn push_card_matches(
    card: &AOracleCard,
    cid: u32,
    printings: &[APrinting],
    artwork_group_col: &Archived<Vec<u16>>,
    start: usize,
    end: usize,
    all_match: bool,
    residual: &[&FilterExpr],
    residual_is_or: bool,
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    strings: &AStrings,
    existential_plane: Option<(&PlaneExpr, &Archived<BitPlanes>)>,
    out: &mut Vec<Match>,
    group_best: &mut [Option<(u32, f64)>],
    touched: &mut Vec<u16>,
) -> u32 {
    match mode {
        Mode::Card => {
            // Printings are stored in descending default-prefer order, so
            // for the default prefer the first matching printing IS the
            // chosen one — O(1) when the card pass already said True.
            //
            // #676 review: when `existential_plane` is `Some`, the residual
            // check is still required, not replaced -- a legality leaf folded
            // into `plane` alongside a genuinely printing-dependent residual
            // (DateCmp, ArtistMatch, ...) needs a printing satisfying *both*
            // at once (docs/issues/00667-engine-legality-divergent-carveout.md "Row
            // selection for unique=card"); checking only the plane could pick
            // a printing that's legal but fails the residual, or vice versa.
            // Kept as two separate closures (not one closure branching on
            // `existential_plane` every call) for the same reason
            // `card_match_count` is split this way — see its doc.
            // `examined` rides alongside `chosen` rather than being counted in a separate local: on
            // the two default-prefer paths the chosen printing IS the one the loop stopped at, so its
            // offset from `start` is the count and no per-iteration bookkeeping is needed. The custom-
            // prefer paths score every printing and so examine the whole span, unconditionally.
            let span = (end - start) as u32;
            let (chosen, examined): (Option<u32>, u32) = if let Some((pe, planes)) = existential_plane {
                let satisfies = |pid: usize| {
                    eval_plane_expr_for_printing(pe, planes, cid, &printings[pid], strings)
                        && (all_match || FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or))
                };
                if matches!(prefer, Prefer::Default) {
                    let found = (start..end).find(|&pid| satisfies(pid)).map(|pid| pid as u32);
                    (found, found.map_or(span, |pid| pid - start as u32 + 1))
                } else {
                    let mut chosen: Option<(u32, f64)> = None;
                    for pid in start..end {
                        if !satisfies(pid) {
                            continue;
                        }
                        let score = prefer_score(card, &printings[pid], prefer);
                        if chosen.is_none_or(|(_, s)| score > s) {
                            chosen = Some((pid as u32, score));
                        }
                    }
                    (chosen.map(|(pid, _)| pid), span)
                }
            } else if matches!(prefer, Prefer::Default) {
                let mut found: Option<u32> = None;
                for pid in start..end {
                    if all_match || FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or) {
                        found = Some(pid as u32);
                        break;
                    }
                }
                (found, found.map_or(span, |pid| pid - start as u32 + 1))
            } else {
                let mut chosen: Option<(u32, f64)> = None;
                for pid in start..end {
                    let p = &printings[pid];
                    if !all_match && !FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) {
                        continue;
                    }
                    let score = prefer_score(card, p, prefer);
                    if chosen.is_none_or(|(_, s)| score > s) {
                        chosen = Some((pid as u32, score));
                    }
                }
                (chosen.map(|(pid, _)| pid), span)
            };
            if let Some(pid) = chosen {
                out.push((sort_key_bits(card, &printings[pid as usize], sort_col, descending), cid, pid));
            }
            examined
        }
        Mode::Printing => {
            for pid in start..end {
                let p = &printings[pid];
                if !all_match && !FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) { continue; }
                out.push((sort_key_bits(card, p, sort_col, descending), cid, pid as u32));
            }
            // Every printing is a candidate row: no ordering lets this stop early.
            (end - start) as u32
        }
        Mode::Artwork => {
            // Within-range order is prefer-score-desc (not illustration), so
            // group by artwork_group_id (#629) with an array-indexed scratch:
            // `group_best` is reused/grown across cards and never bulk-cleared
            // (indices below a card's own group count are the only ones ever
            // touched); `touched` tracks which indices this card set, so only
            // those get reset via `.take()` after emission. Ranges are tiny
            // (median 2 printings) so this is mostly a formality for the
            // common case, but it keeps the rare high-group-count card (basic
            // lands, up to ~385 distinct illustrations) at O(printings) instead
            // of the O(printings²) a linear per-printing scan would cost.
            touched.clear();
            if matches!(prefer, Prefer::Default) {
                // Printings are stored prefer-desc, so the first residual-qualifying printing of
                // each group is its best-prefer rep. Read `gid` first and skip any printing whose
                // group is already repped: repped groups never pay the residual verification (a
                // struct read) again, and no score comparison is needed (first qualifying wins).
                for pid in start..end {
                    // Read gid from the columnar side array, not the wide struct: repped-group
                    // printings (the majority) then never touch the struct at all. `group_best` is
                    // pre-sized by the caller to max_artwork_groups, so no per-printing resize check.
                    let gid = u16::from(artwork_group_col[pid]) as usize;
                    debug_assert!(gid < group_best.len(), "group_best must be pre-sized to max_artwork_groups");
                    if group_best[gid].is_some() {
                        continue;
                    }
                    let p = &printings[pid];
                    if !all_match && !FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) { continue; }
                    group_best[gid] = Some((pid as u32, 0.0));
                    touched.push(gid as u16);
                }
            } else {
                // Custom prefer: iteration order != prefer order, so every printing must be
                // considered and the max-prefer rep kept per group.
                for pid in start..end {
                    let p = &printings[pid];
                    if !all_match && !FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) { continue; }
                    let gid = u16::from(p.artwork_group_id) as usize;
                    debug_assert!(gid < group_best.len(), "group_best must be pre-sized to max_artwork_groups");
                    let score = prefer_score(card, p, prefer);
                    match &group_best[gid] {
                        None => {
                            group_best[gid] = Some((pid as u32, score));
                            touched.push(gid as u16);
                        }
                        Some((_, best_score)) if score > *best_score => {
                            group_best[gid] = Some((pid as u32, score));
                        }
                        _ => {}
                    }
                }
            }
            for &gid in touched.iter() {
                let (bp, _) = group_best[gid as usize].take().unwrap();
                out.push((sort_key_bits(card, &printings[bp as usize], sort_col, descending), cid, bp));
            }
            // Both grouping loops run to `end`: a later printing can always rep a group not yet seen.
            (end - start) as u32
        }
    }
}

/// Fraction of the printings under a candidate card that survive a residual, used to discount the
/// operating-space result total when the narrowing is NOT tight. With `all_match_known` the count is
/// exact (measured `matches_pushed / matches` = 1.00); without it, measured 0.38-0.42 for printing and
/// 0.50-0.56 for artwork, stable across both materializing plans. Artwork sits higher because its
/// groups collapse several printings into one, so a group survives if ANY of its printings does.
static RESIDUAL_PASS_RATE_PRINTING: LazyLock<f64> = LazyLock::new(|| guard_env("CARD_ENGINE_RESIDUAL_PASS_RATE_PRINTING", 0.40));
/// See `RESIDUAL_PASS_RATE_PRINTING`.
static RESIDUAL_PASS_RATE_ARTWORK: LazyLock<f64> = LazyLock::new(|| guard_env("CARD_ENGINE_RESIDUAL_PASS_RATE_ARTWORK", 0.53));

/// Below this many matches the gathered path is already microseconds and
/// byte-identical to the pre-streaming behavior; above it, walking the
/// permutation (a fixed ~n bit-tests over the counts array) plus per-page-card
/// emission wins. Same measured-constant philosophy as MAX_NARROW_FRACTION.
/// Calibrated (scripts/bench_cost_guards.py, `cmc<K` with exactly dialable
/// card counts): the crossover wanders 0.6k-1.1k across reps and corpus
/// sizes with branch differences under the ~5% noise floor throughout that
/// band; 1,024 sits at the spread's upper (gather/simple) edge, and past it
/// streaming's win grows fast (~1.8× by 8k), so the trigger stays put.
/// Whether `exact_result_total` may answer from the per-value `ValueTotals` table and the rarity range
/// table, and whether printing mode's `result_total` may take the exact value instead of
/// `compose_printing_estimate`'s. On by default; 0 falls every one of those back to the estimator.
///
/// Kept as a permanent handle, not scaffolding: these arms change ROUTING (a total feeds the argmin),
/// so the only honest way to price them is an interleaved A/B in which both arms read a byte-identical
/// archive. The table is archived either way, so flipping this cannot move a field offset.
/// Whether a dense `frame_data` value is gated on being genuinely BROAD (1/4) rather than merely dense
/// (1/32). 0 restores the conflated gate, for the A/B that priced the distinction.
static DENSE_FRAME_BROAD_GATE: LazyLock<bool> = LazyLock::new(|| guard_env("CARD_ENGINE_DENSE_FRAME_BROAD_GATE", 1u8) != 0);

/// Whether `sorted_ids` may take the bitmap route. 0 forces `collect` + `sort_unstable`, for the A/B.
static RANGE_MATERIALIZE_BITMAP: LazyLock<bool> = LazyLock::new(|| guard_env("CARD_ENGINE_RANGE_MATERIALIZE_BITMAP", 1u8) != 0);

/// Whether range breadth is judged against the corpus (`n_printings`) or the index's own length. The
/// index omits nulls, so the latter overstates breadth by the null rate. 0 restores it, for the A/B.
static RANGE_BREADTH_VS_CORPUS: LazyLock<bool> = LazyLock::new(|| guard_env("CARD_ENGINE_RANGE_BREADTH_VS_CORPUS", 1u8) != 0);

/// Whether the PAIR table answers a two-leaf `And` exactly and tightens the `And` fold's bound. 0 falls
/// both back to `min` over single leaves.
///
/// 21 of 21 measured cells exact where `min` read 2.8-10.1x over, and the three-leaf bound goes 7.80x to
/// 2.02x. Wiring it in regressed the disjoint cases 24x until `ComposeEstimate` split the result from the
/// candidate bound: an exact 0 was collapsing `eval_domain`/`scan_units` for the MATERIALIZING
/// alternatives, pricing `GatheredScan` at 0.2 us against a measured 199.3 us, because a plan still has
/// to scan to discover a set is empty. With the split it is neutral in aggregate and it is what lets
/// `LEGALITY_SCAN_SCOPE` be on -- docs/issues/done/local-engine-pair-totals.md.
static PAIR_TOTALS: LazyLock<bool> = LazyLock::new(|| guard_env("CARD_ENGINE_PAIR_TOTALS", 1u8) != 0);

/// Whether the legality divergent-share correction to `stream_scan_units` is scoped to filters whose
/// residual can actually settle at card level.
///
/// On since the pair table made it safe. Scoped, the correction is 5-7x more accurate on this
/// population and wins 2.0x in printing mode and 1.6x in artwork; unscoped it also moved CARD mode onto
/// `PrintingCompose`, which then declined at dispatch (`DeclineSparseExact`) and fell back having already
/// paid the build. `PairTotals` gives card mode the exact total, so `compose_paging` now predicts that
/// decline instead of walking into it. Aggregate is neutral (0.995 target / 0.997 whole mix over 12
/// interleaved rounds); the win is per query and the point is a feature that is no longer wrong.
static LEGALITY_SCAN_SCOPE: LazyLock<bool> = LazyLock::new(|| guard_env("CARD_ENGINE_LEGALITY_SCAN_SCOPE", 1u8) != 0);

/// Whether a conjunct the candidate set already proves is skipped by `card_pass` instead of
/// re-verified. 0 restores the re-verification, for the A/B that priced it.
static PROVEN_CONJUNCTS: LazyLock<bool> = LazyLock::new(|| guard_env("CARD_ENGINE_PROVEN_CONJUNCTS", 1u8) != 0);

static EXACT_VALUE_TOTALS: LazyLock<bool> = LazyLock::new(|| guard_env("CARD_ENGINE_EXACT_VALUE_TOTALS", 1u8) != 0);

static STREAM_MIN_MATCHES: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_STREAM_MIN_MATCHES", 1_024));

/// Kill-switch for narrowing the streamed walk to the filter's bound on the sort column
/// (`walk_bounds`). Default on; 0 walks the whole permutation as the executor did before the bound
/// existed. A binary switch, not a calibrated threshold — the bound costs O(log n_cards) probes once
/// per query and nothing per card, so there is no crossover to find, and its predecessor (a per-card
/// `inv_perm` min/max, gated on candidate count) was replaced precisely because it did have one.
///
/// Exists because `scripts/bench_walk_span.py` needs both behaviours in ONE binary: the effect is
/// smaller than the run-to-run spread of `ns_loop` across builds, so a cross-build A/B of it reports
/// the wrong sign.
static WALK_SORT_BOUND: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_WALK_SORT_BOUND", 1));

/// Whether run_query reorders And/Or children cheapest-verification-first
/// before the evaluation walk (see FilterExpr::order_children_by_verify_cost).
/// Unlike the guards above this is a binary A/B switch, not a threshold:
/// cost-only ordering never adds work (when nothing short-circuits, every
/// child ran anyway), so there is no crossover to calibrate — the off
/// position exists for benchmarking written-order sensitivity.
static VERIFY_ORDER: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_VERIFY_ORDER", 1));

/// Kill-switch for the printing-mode bare-range fastpath (`printing_range_fastpath`). Default on;
/// set to 0 to force every such query back onto the general path (used to A/B correctness and
/// timing). A binary switch, not a calibrated threshold.
static PRINTING_RANGE_FASTPATH: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_PRINTING_RANGE_FASTPATH", 1));

/// Card-mode range→card-existence popcount fast path (PR 2a / card-space idea 2). Same kind of
/// binary A/B kill-switch as `PRINTING_RANGE_FASTPATH`, not a calibrated threshold: `0` routes these
/// queries exactly as before (the general candidate path), `1` (default) enables `CardRangePopcount`.
static RANGE_BITS_CARD: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_RANGE_BITS_CARD", 1));

/// #724 unified printing-space compose plan (`PrintingCompose`). Binary A/B kill-switch, same kind as
/// `PRINTING_RANGE_FASTPATH`: `0` routes every composable printing-space query (border/rarity/legality,
/// `AND`/`OR`, any distinct-on) as before (general path), `1` (default) composes in printing space,
/// projects to the result space, and pages with the grouped walk.
static PRINTING_COMPOSE: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_PRINTING_COMPOSE", 1));

/// The range index and half-open `[lo, hi)` a *bare* range-predicate leaf selects, or None for
/// anything else (compound, `Ne`, a non-range numeric field). Provably-empty predicates return a
/// zero-width `[v, v)` so the fastpath's `k` resolves to 0. Reuses the exact bound derivations the
/// narrowing path uses (`int_range_bounds`, [`date_range_bounds`], [`year_range_bounds`]) so the
/// fastpath and `narrow_rec` can never disagree on which printings a predicate covers.
/// Which printing-range index a bare `NumericCmp` targets, plus its op normalized to `field op
/// const` order (`flip_op` undoes a `const op field` parse) — shared by `bare_range_bounds`'s direct
/// and negated (`Not`) cases so the field/operand-order dispatch is written once. Only
/// price/collector-number are printing-range-indexed; cmc/power/toughness/rarity are card-space and
/// belong to other paths.
fn resolve_numeric_range_leaf<'i>(
    lhs: &NumExpr,
    op: CmpOp,
    rhs: &NumExpr,
    indexes: &'i Archived<CardIndexes>,
) -> Option<(&'i Archived<PrintingValueIndex>, CmpOp, f64)> {
    match (lhs, rhs) {
        (NumExpr::Field(NumField::PriceUsd), NumExpr::Const(v)) => Some((&indexes.price_usd, op, snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))),
        (NumExpr::Const(v), NumExpr::Field(NumField::PriceUsd)) => Some((&indexes.price_usd, flip_op(op), snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))),
        (NumExpr::Field(NumField::PriceEur), NumExpr::Const(v)) => Some((&indexes.price_eur, op, snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))),
        (NumExpr::Const(v), NumExpr::Field(NumField::PriceEur)) => Some((&indexes.price_eur, flip_op(op), snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))),
        (NumExpr::Field(NumField::PriceTix), NumExpr::Const(v)) => Some((&indexes.price_tix, op, snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))),
        (NumExpr::Const(v), NumExpr::Field(NumField::PriceTix)) => Some((&indexes.price_tix, flip_op(op), snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))),
        (NumExpr::Field(NumField::CollectorNumberInt), NumExpr::Const(v)) => Some((&indexes.collector_number, op, *v)),
        (NumExpr::Const(v), NumExpr::Field(NumField::CollectorNumberInt)) => Some((&indexes.collector_number, flip_op(op), *v)),
        _ => None,
    }
}

/// The exact card-count table paired with a range index, or `None` if that index has none.
///
/// Matched by pointer identity because `bare_range_bounds` hands back one of the three index
/// references itself, not a discriminant — and all three live in the same archived struct, so the
/// comparison is exact rather than heuristic. Threading a dimension tag through
/// `resolve_numeric_range_leaf` instead would touch every caller for no more safety.
fn range_card_counts_for<'i>(
    indexes: &'i Archived<CardIndexes>,
    idx: &Archived<PrintingValueIndex>,
) -> Option<&'i ArchivedRangeCardCounts> {
    if std::ptr::eq(idx, &indexes.released_at) {
        Some(&indexes.released_at_cards)
    } else if std::ptr::eq(idx, &indexes.price_usd) {
        Some(&indexes.price_usd_cards)
    } else if std::ptr::eq(idx, &indexes.price_eur) {
        Some(&indexes.price_eur_cards)
    } else if std::ptr::eq(idx, &indexes.price_tix) {
        Some(&indexes.price_tix_cards)
    } else if std::ptr::eq(idx, &indexes.collector_number) {
        Some(&indexes.collector_number_cards)
    } else {
        None
    }
}

/// Bare bounds for a printing-range-indexed comparison — `usd`/`cn`/`date`/`year` — and, since
/// `NOT(x op c) == x negate_op(op) c` is exact under this engine's null semantics (`negate_op`'s own
/// doc: verified against `tri()`'s actual `NumVal::Null` short-circuit, the same guarantee
/// `narrow_rec`'s `-r:x` arm already relies on), a `Not` wrapping one of these four shapes too —
/// `-usd<50` becomes `usd>=50`'s bounds directly, no complement, no residual. `Eq`/`Ne` don't reduce
/// this way (`Ne` isn't a single half-open range), but nothing needs to special-case that:
/// `int_range_bounds`/`date_range_bounds`/`year_range_bounds` already return `None` for `Ne`
/// (`negate_op(Eq) == Ne`), so a negated equality falls out on its own. Every caller of this function
/// — `narrow_rec`'s own range narrowing, `is_printing_composable`, `compose_printing_estimate`,
/// `compose_printing_bits` — gets the negated shape for free; none of them special-case `Not`
/// themselves (docs/issues/local-engine-negated-range-narrowing.md).
/// The `[lo, hi)` rarity-int window a bare rarity comparison covers, or `None` when the filter is not
/// one. Deliberately narrow: `NumericCmp` on `RarityInt` against a constant, optionally negated, which
/// is every spelling of `r:`/`rarity:` the parser produces.
///
/// Separate from `bare_range_bounds` on purpose -- see `exact_result_total`'s rarity arm.
fn bare_rarity_bounds(filter: &FilterExpr) -> Option<(u32, u32)> {
    fn leaf(filter: &FilterExpr, map_op: impl Fn(CmpOp) -> CmpOp) -> Option<(u32, u32)> {
        let (op, value) = match filter {
            FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op, rhs: NumExpr::Const(v) } => (map_op(*op), *v),
            FilterExpr::NumericCmp { lhs: NumExpr::Const(v), op, rhs: NumExpr::Field(NumField::RarityInt) } => {
                (flip_op(map_op(*op)), *v)
            }
            _ => return None,
        };
        // `Some(None)` is an empty window, which is an exact total of zero rather than no answer.
        match int_range_bounds(op, value)? {
            None => Some((0, 0)),
            Some((lo, hi)) => Some((lo, hi)),
        }
    }
    match filter {
        FilterExpr::Not(inner) => leaf(inner.as_ref(), negate_op),
        _ => leaf(filter, |op| op),
    }
}

fn bare_range_bounds<'i>(
    filter: &FilterExpr,
    indexes: &'i Archived<CardIndexes>,
) -> Option<(&'i Archived<PrintingValueIndex>, u32, u32)> {
    // The direct and `Not` arms are the same three-way leaf dispatch, differing only in
    // whether the leaf's op is taken as written or negated. `map_op` is that difference,
    // so the grammar is written once and the two arms cannot drift apart.
    fn leaf<'i>(
        filter: &FilterExpr,
        indexes: &'i Archived<CardIndexes>,
        map_op: impl Fn(CmpOp) -> CmpOp,
    ) -> Option<(&'i Archived<PrintingValueIndex>, u32, u32)> {
        match filter {
            FilterExpr::NumericCmp { lhs, op, rhs } => {
                let (idx, op, value) = resolve_numeric_range_leaf(lhs, map_op(*op), rhs, indexes)?;
                match int_range_bounds(op, value)? {
                    None => Some((idx, 0, 0)),
                    Some((lo, hi)) => Some((idx, lo, hi)),
                }
            }
            FilterExpr::DateCmp { op, value } => {
                let (lo, hi) = date_range_bounds(map_op(*op), *value)?;
                Some((&indexes.released_at, lo, hi))
            }
            FilterExpr::YearCmp { op, year } => {
                let (lo, hi) = year_range_bounds(map_op(*op), *year)?;
                Some((&indexes.released_at, lo, hi))
            }
            _ => None,
        }
    }

    match filter {
        FilterExpr::Not(inner) => leaf(inner.as_ref(), indexes, negate_op),
        _ => leaf(filter, indexes, |op| op),
    }
}

/// Build `CardRangePopcount`'s two bitmaps from an exact range slice — `bare_range_bounds` supplies
/// the index + half-open `[lo, hi)` for whichever range family the leaf is (usd/cn/date) — in a
/// single pass: the tight printing-space membership set (`range_pbits`, set directly from the
/// value-sorted slice — never the loose complement `range_narrowed` would pick for a broad range,
/// which over-includes index-absent printings) and its card-existence projection (`card_bits`, each
/// printing's card via the `printing_to_card` direct array, #690). The card bitmap's popcount is the
/// exact `unique=card` total; the printing bitmap is the per-printing residual emission re-checks so
/// the representative printing it shows genuinely satisfies the range. All three indexes are exact
/// (price is integer cents #688; cn/date are natively integer), so the slice is tight.
///
/// One fused pass rather than scatter-then-`printing_bits_to_card_bits`: the projection over the
/// scattered bitmap is the expensive half (a `trailing_zeros` extraction per set bit plus a cursor
/// branch across every word), and folding it into the scatter loop via a direct `printing_to_card`
/// lookup measured ~40% cheaper on `usd<50` (~174µs → ~104µs; see `card_range_build_cost_split`),
/// even though the value-ordered slice makes those lookups random. Bare range only
/// (`card_range_popcount_applicable` requires no plane), so there is nothing to AND.
fn build_card_range_bits(
    idx: &Archived<PrintingValueIndex>,
    lo: u32,
    hi: u32,
    indexes: &Archived<CardIndexes>,
    n_cards: usize,
    n_printings: usize,
) -> (Vec<u64>, Vec<u64>) {
    let ptc = &indexes.printing_to_card;
    let mut range_pbits = vec![0u64; n_printings.div_ceil(64)];
    let mut card_bits = vec![0u64; n_cards.div_ceil(64)];
    for pid in idx.range_pids(lo, hi) {
        let pid = pid as usize;
        range_pbits[pid >> 6] |= 1u64 << (pid & 63);
        let cid = u32::from(ptc[pid]) as usize;
        card_bits[cid >> 6] |= 1u64 << (cid & 63);
    }
    (card_bits, range_pbits)
}

/// Page for a `unique=printing` bare-range query ordered by a *card-level* key: walk that key's
/// precomputed card permutation, emit each card's matching printings, stop once the page is full.
/// A card-level sort key is shared by all of a card's printings, so card order is printing order;
/// within a card, printings order by `sort_key_bits` then pid — byte-identical to the streamed
/// path's emission (`run_query_streamed`), just without the O(n) count pass, since the caller
/// already has the exact `total` from the index.
// needless_range_loop: `pid` is pushed into the match tuple, so the absolute index is the point.
#[allow(clippy::needless_range_loop)]
fn walk_printing_page<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    leaf: &FilterExpr,
    perm: &Archived<Vec<u32>>,
) -> Vec<(&'a AOracleCard, &'a APrinting)> {
    let QueryCtx { cards, printings, offsets, strings, .. } = *ctx;
    let QueryParams { sort_col, descending, limit, page_offset, .. } = *params;
    let residual: [&FilterExpr; 1] = [leaf];
    let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
    let mut scratch: Vec<Match> = Vec::new();
    let mut skip = page_offset;
    for cid in perm.iter().map(|x| u32::from(*x)) {
        let card = &cards[cid as usize];
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;
        scratch.clear();
        for pid in start..end {
            let p = &printings[pid];
            if FilterExpr::residual_matches(card, p, strings, &residual, false) {
                scratch.push((sort_key_bits(card, p, sort_col, descending), cid, pid as u32));
            }
        }
        if scratch.is_empty() {
            continue;
        }
        if skip >= scratch.len() {
            skip -= scratch.len();
            continue;
        }
        scratch.sort_unstable_by(page_cmp);
        for m in scratch.iter().skip(skip) {
            page.push((&cards[m.1 as usize], &printings[m.2 as usize]));
            if page.len() == limit {
                return page;
            }
        }
        skip = 0;
    }
    page
}

/// Whether `filter` is a bare price comparison (either operand order) — the only range field that
/// is also a sort column, so the only one an `order by usd` page can be served aligned.
fn is_price_leaf(filter: &FilterExpr) -> bool {
    matches!(
        filter,
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::PriceUsd), .. }
            | FilterExpr::NumericCmp { rhs: NumExpr::Field(NumField::PriceUsd), .. }
    )
}

/// Page for a `unique=printing` price query ordered by `usd` (the aligned case) — every printing in
/// `[lo, hi)` matches, so the page is a pure slice of the index in sort order.
///
/// A key's `pids` run is already in `page_cmp`'s tiebreak order and every printing in it shares the
/// primary key, so the runs from the page's starting end, concatenated, ARE the rows `select_page`
/// would produce. This walks key indices — forward from `lo`'s ascending, backward from `hi`'s
/// descending, since the tiebreak does not flip with direction — skips whole runs by length, and
/// emits `limit` rows. No `sort_key_bits`, no `Match` buffer, no `select_page`: O(runs skipped +
/// limit) rather than O(the runs the page overlaps), which for a price bucket is thousands of
/// printings for a 60-row page.
#[allow(clippy::too_many_arguments)]
fn aligned_page<'a>(
    idx: &Archived<PrintingValueIndex>,
    lo: u32,
    hi: u32,
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    printing_to_card: &AOffsets,
    descending: bool,
    page_offset: usize,
    limit: usize,
) -> Vec<(&'a AOracleCard, &'a APrinting)> {
    let ks = idx.keys.partition_point(|k| u32::from(*k) < lo);
    let ke = idx.keys.partition_point(|k| u32::from(*k) < hi);
    let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
    let mut skip = page_offset;
    for step in 0..ke.saturating_sub(ks) {
        let run = idx.run(if descending { ke - 1 - step } else { ks + step });
        if skip >= run.len() {
            skip -= run.len();
            continue;
        }
        for t in run.start + skip..run.end {
            let pid = idx.pid_at(t);
            let cid = u32::from(printing_to_card[pid]) as usize;
            page.push((&cards[cid], &printings[pid]));
            if page.len() == limit {
                return page;
            }
        }
        skip = 0;
    }
    page
}

/// Fast path for a *bare, broad* range predicate under `unique=printing`
/// (docs/issues/local-engine-sorted-range-fastpath.md, PR 1). `total` is `k` from the range
/// index's binary search — no full per-printing count pass — and the page is produced in order
/// without materializing all `k` matches. Returns None (fall through to the general path) for
/// anything it doesn't own: non-printing modes, a plane component, a non-bare/non-range filter, a
/// selective range (the existing narrowing already wins, and restricting the walk to dense
/// predicates keeps its worst case bounded), or an order-by without a card permutation (e.g. the
/// range field itself — deferred).
///
/// Every exit records itself in `PAGING_TAKEN` (the `Range*` variants of [`PagingTaken`]), so a
/// `None` from here reaches `explain_analyze` naming the gate rather than as a bare cost with no
/// cause. Nothing predicts these the way `compose_paging` predicts the compose branch — the point
/// is to size the declines, and to make `RangeNotBare`/`RangePermutationStale` visible if they ever
/// fire, since neither should.
fn printing_range_fastpath<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    // Timed as one span rather than split into setup/loop/finish. The three-phase split exists where
    // an executor HAS three phases to attribute cost between; this one has a dozen structurally
    // different exits (three of them producing a page) and no common shape across them. What the
    // harnesses need is that the phase sum equals the executor's own time, and one span satisfies
    // that exactly -- `ns_loop` is the honest bucket for "the work", with the other two zero.
    //
    // Only a run that PRODUCED a page publishes. A decline returns `None`, records no trial, and must
    // leave the slot as `take_phase_stats` cleared it.
    let t = std::time::Instant::now();
    let out = printing_range_fastpath_inner(ctx, params, filter);
    if out.is_some() {
        PHASE_STATS.with(|c| c.set(PhaseStats { ns_loop: t.elapsed().as_nanos() as u64, ..PhaseStats::default() }));
    }
    out
}

fn printing_range_fastpath_inner<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    let QueryCtx { cards, printings, indexes, .. } = *ctx;
    let QueryParams { sort_col, descending, limit, page_offset, .. } = *params;
    let Some((idx, lo, hi)) = bare_range_bounds(filter, indexes) else {
        note_paging_taken(PagingTaken::RangeNotBare);
        return None;
    };
    let (s, e) = idx.range(lo, hi);
    let k = e - s;
    if !range_too_broad_to_narrow(k, idx.len()) {
        note_paging_taken(PagingTaken::RangeSelective);
        return None; // selective: the existing narrowing path narrows tightly and wins
    }
    // total = matching printings = k (each priced printing in [lo, hi) is one row; NULL-valued
    // printings are absent from the index and don't match). Same value the count pass would sum.
    if k == 0 || page_offset >= k {
        note_paging_taken(PagingTaken::EmptyPage);
        return Some((k, Vec::new()));
    }
    // Aligned: order by the range field itself. `usd` is the only range field that is also a sort
    // column, and only a price predicate makes `idx` the value-sorted permutation for that sort;
    // a non-price predicate ordered by usd has no aligned mapping (and no permutation) — bail.
    if matches!(sort_col, SortCol::PriceUsd) {
        if !is_price_leaf(filter) {
            note_paging_taken(PagingTaken::RangeUnalignedPrice);
            return None;
        }
        note_paging_taken(PagingTaken::RangeAligned);
        let page = aligned_page(idx, lo, hi, cards, printings, &indexes.printing_to_card, descending, page_offset, limit);
        return Some((k, page));
    }
    // The walk reproduces run_query_streamed's *stream* emission (per-card-contiguous), which the
    // general path only uses above STREAM_MIN_MATCHES; at or below it, run_query_streamed gathers
    // and sorts globally, ordering full-sort-key ties across cards by pid instead. Bail there so
    // the fastpath never claims a range the general path would gather. The band is narrow
    // (NARROW_FLOOR < k <= STREAM_MIN_MATCHES, i.e. 1000 < k <= 1024) and only reachable on a tiny
    // index (broad needs k > index_len/4, so index_len < ~4096) -- never in production, where broad
    // means tens of thousands. aligned_page above matches the gathered path directly, so it's exempt.
    if k <= *STREAM_MIN_MATCHES {
        note_paging_taken(PagingTaken::RangeSparse);
        return None;
    }
    let Some(perm) = indexes.sort_perms.get(sort_col, descending) else {
        note_paging_taken(PagingTaken::RangeNoPermutation);
        return None;
    };
    if perm.len() != cards.len() {
        note_paging_taken(PagingTaken::RangePermutationStale);
        return None;
    }
    note_paging_taken(PagingTaken::RangeWalk);
    Some((k, walk_printing_page(ctx, params, filter, perm)))
}

/// The exact `unique=printing` total for a bare `border:VALUE` leaf, from the #724 printing planes:
/// `popcount` of the value's plane (black/borderless/white), or its postings length (gold/yellow/
/// untracked). `None` for anything that isn't a bare `border ==` leaf, or an unknown border value.
/// This replaces the O(n) count pass — the whole cost of `border:black`/printing today.
/// #724: structural check — is `filter` composable **entirely** from printing-space planes/postings?
/// Cheap (no materialization); this is what plan applicability gates on. Composable leaves: a bare
/// `border ==` value ([`BorderPrintingPlanes`]) and a bare rarity `== const` ([`RarityPrintingPlanes`],
/// equality only — ordinal `r>=rare` still takes the general path); composable interior: `And`/`Or`/
/// `True`. `Not` is deliberately **excluded**: over a nullable field, negation is not the plane's
/// `complement` (a null-border printing satisfies neither `border:black` nor `-border:black` under
/// three-valued logic, but `complement` would count it), so `-border:black` stays on the general path
/// where the residual applies the correct trivalent semantics. Anything else (a text search, a range,
/// an arithmetic compare) is likewise non-composable. A composable expression's bits are **exact** (a
/// set bit *is* a matching printing), so no per-printing re-check is needed.
/// Whether `filter` constrains legality anywhere. Used to scope the `stream_scan_units` correction to the
/// case the sweep measured wrong: legality is the one printing-varying attribute `card_pass` can resolve at
/// CARD level for most cards (only the divergent ones defer), so it is the only one where P3 and P4 examine
/// wildly different printing counts. Border, rarity and watermark all measured at parity.
fn filter_touches_legality(filter: &FilterExpr) -> bool {
    match filter {
        FilterExpr::Legality { .. } => true,
        FilterExpr::And(cs) | FilterExpr::Or(cs) => cs.iter().any(filter_touches_legality),
        FilterExpr::Not(inner) => filter_touches_legality(inner),
        _ => false,
    }
}

fn is_printing_composable(filter: &FilterExpr, indexes: &Archived<CardIndexes>) -> bool {
    match filter {
        FilterExpr::True => true,
        FilterExpr::And(v) | FilterExpr::Or(v) => v.iter().all(|c| is_printing_composable(c, indexes)),
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, .. } => true,
        // #746: `set:`/`watermark:` postings leaves. `scatter_bits(indexes.set_codes[value])` /
        // `indexes.watermarks[value]` is the exact printing bitmap (`tag_postings_leaf_bits`) — a
        // value-membership intersection, so it's exact for both the non-null `set_code` and the
        // nullable `watermark` alike (the positive form never complements). An unknown value scatters
        // nothing (all-zero, exactly "no printing").
        FilterExpr::TextExact { field: TextField::SetCode | TextField::Watermark, op: CmpOp::Eq, .. } => true,
        // #746: `-set:VALUE` — all-ones minus the value's postings (`set_code_negated_leaf_bits`),
        // exact because `set_code` is non-nullable. Guarded on the inner shape (not a bare `Not(_)`)
        // and deliberately NOT extended to `-watermark:` (nullable — its complement would need a
        // "has any watermark" known-mask this leaf doesn't build; stays on the general path).
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, .. }) =>
        {
            true
        }
        // Card-space collection containment leaves (`type:`/`kw:`/`otag:`) and their already-printing-
        // space siblings (`art:`/`is:`). `Ge` only — the postings are tight for containment but a loose
        // superset for `Eq`/`Gt` (they prove `contains(value)`, not the collection-length condition,
        // ~lib.rs:3441), and the compose path has no residual re-check, so `Eq`/`Gt` stay on the general
        // path. `FrameData` is excluded (`collection_compose_index` → `None`): it is the one non-
        // `complete` collection index, so absence there proves nothing and it can't be an exact leaf.
        FilterExpr::CollectionCmp { field, op: CmpOp::Ge, .. } => collection_compose_index(indexes, *field).is_some(),
        // `-type:`/`-kw:`/`-otag:`/`-art:`/`-is:` — the exact complement of the positive leaf (a
        // collection is never NULL, so no trivalent-NULL trap; see `collection_negated_leaf_bits`).
        // Guarded on the inner shape (not a bare `Not(_)`) and on `Ge` so a loose `Eq`/`Gt` inner can't
        // reach it.
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::CollectionCmp { field, op: CmpOp::Ge, .. } if collection_compose_index(indexes, *field).is_some()) =>
        {
            true
        }
        // Any rarity comparison, not only `== c`: the domain is closed and every present value has exact
        // bits, so an inequality is the `Or` of the qualifying ones. See `rarity_cmp_leaf_bits`.
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), rhs: NumExpr::Const(_), .. }
        | FilterExpr::NumericCmp { lhs: NumExpr::Const(_), rhs: NumExpr::Field(NumField::RarityInt), .. } => true,
        // Legality via #667's card `_EXISTS` plane + a divergent repair (see `legality_leaf_bits`).
        // Only a plane-backed status (legal/banned/restricted) with a present format; an absent format
        // (`shift: None`) matches nothing and stays on the general path.
        FilterExpr::Legality { shift: Some(_), expected } => status_plane_bases(*expected).is_some(),
        // #731: usd/cn/date range leaves — the in-range index slice scatters into an exact printing
        // bitmap (`range_leaf_bits`). `bare_range_bounds` recognizes the printing-range-indexed shape
        // and returns its `[lo,hi)` bounds: the ordered ops, and `Eq` too (a narrow `[v, v+1)`). Only
        // `Ne` or a card-space field (cmc/power/rarity) yields `None` → stays on the general path.
        // This is what lets a range compose with border/rarity/legality — and range∧range — exactly,
        // in any distinct-on.
        FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => {
            bare_range_bounds(filter, indexes).is_some()
        }
        // `-usd<50` etc.: `bare_range_bounds` reduces this to the flipped comparison's bounds
        // directly (see its doc) — composable exactly like the bare case above. Guarded on the
        // inner shape (not a bare `Not(_)` catch-all) so this doesn't also try to claim
        // `-border:`/`-r:`/`-f:`, which have their own dedicated (non-range) Not handling elsewhere
        // and stay non-composable here, same as before this arm existed.
        FilterExpr::Not(inner) if matches!(inner.as_ref(), FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. }) => {
            bare_range_bounds(filter, indexes).is_some()
        }
        _ => false,
    }
}

/// Whether the printing-space plane indexes are actually built for this store (production always
/// builds them; some unit-test fixture stores don't). A built index reports its store's printing count
/// in `n_printings`; a `Default` (unbuilt) index reports 0. Gating applicability on this lets the plan
/// decline cleanly (→ general path) rather than index into an empty word array.
fn printing_compose_indexes_built(indexes: &Archived<CardIndexes>) -> bool {
    u32::from(indexes.border_printing.n_printings) > 0 && u32::from(indexes.rarity_printing.n_printings) > 0
}

/// All-ones printing-space bitmap over the `n_printings` domain (tail bits masked to 0). Built by
/// complementing a zero vec, so the tail-clear contract is `complement_bits`'s single source of truth.
fn all_printing_bits(n_printings: usize) -> Vec<u64> {
    let mut bits = vec![0u64; words_per_plane(n_printings)];
    complement_bits(&mut bits, n_printings);
    bits
}

/// The exact printing-space bitmap for a bare `border == value` leaf: a copy of the value's plane
/// slice, its scattered postings, or all-zero for an unknown value (exactly "no printing").
fn border_leaf_bits(value: &str, bp: &Archived<BorderPrintingPlanes>, n_printings: usize) -> Vec<u64> {
    let wpp = words_per_plane(n_printings);
    if let Some(k) = BORDER_PRINTING_PLANE_VALUES.iter().position(|&v| v == value) {
        return bp.words[k * wpp..(k + 1) * wpp].iter().map(|w| u64::from(*w)).collect();
    }
    match bp.postings.iter().find(|e| e.0.as_str() == value) {
        Some(e) => scatter_bits(e.1.iter().map(|p| u32::from(*p)), n_printings),
        None => vec![0u64; wpp],
    }
}

/// The exact printing-space bitmap for a bare rarity `== c` leaf. `c` is the rarity int as a float;
/// a non-integer or out-of-range value matches nothing (all-zero). Interior ints (common..mythic)
/// read their plane slice; the sparse tail (special/bonus) scatters its postings.
fn rarity_leaf_bits(c: f64, rp: &Archived<RarityPrintingPlanes>, n_printings: usize) -> Vec<u64> {
    let wpp = words_per_plane(n_printings);
    // Only an exact non-negative integer can equal a stored rarity int; anything else matches nothing.
    if c < 0.0 || c.fract() != 0.0 || c > f64::from(u8::MAX) {
        return vec![0u64; wpp];
    }
    let int = c as u8;
    if let Some(k) = RARITY_PRINTING_PLANE_INTS.iter().position(|&v| v == int) {
        return rp.words[k * wpp..(k + 1) * wpp].iter().map(|w| u64::from(*w)).collect();
    }
    match rp.postings.iter().find(|e| e.0 == int) {
        Some(e) => scatter_bits(e.1.iter().map(|p| u32::from(*p)), n_printings),
        None => vec![0u64; wpp],
    }
}

/// The exact printing-space bitmap for **any** rarity comparison, not just `== c`.
///
/// Rarity's domain is closed and small: the four interior ints have laid-out planes and the sparse tail
/// (special/bonus) has postings, so every value present in the store can be enumerated and its exact bits
/// read. An inequality is then the `Or` of the values that satisfy it — the same enumeration
/// `walk_rarity_orderby_page` already walks for its bucket order.
///
/// Two consequences worth stating. **NULL rarity is excluded for free**: a printing with no rarity appears in
/// no plane and no posting, so it matches nothing here, which is the trivalent answer for every op including
/// `Ne` (`-r:rare` must not match a rarity-less printing). And this is **strictly more capable than
/// `compile_plane`'s** `compile_rarity_cmp`, which shares one "above mythic" plane and so declines
/// (`BucketVerdict::Ambiguous`) whenever special must be told from bonus — `r:special`, `r>=bonus` and friends.
/// Compose reads the two apart from their own postings, so it has no ambiguous case at all.
///
/// Was `Eq`-only, and the cost of that was not subtle: `r>=rare`/artwork fell off the compose path entirely
/// and took **487.7 µs** through a full candidate scan, against **59.2 µs** for `r:rare` on the same corpus —
/// 8× for a predicate that is just `rare ∨ mythic ∨ special ∨ bonus`.
fn rarity_cmp_leaf_bits(op: CmpOp, threshold: f64, rp: &Archived<RarityPrintingPlanes>, n_printings: usize) -> Vec<u64> {
    let wpp = words_per_plane(n_printings);
    let mut out = vec![0u64; wpp];
    for int in rarity_ints_present(rp) {
        if !planes::matches_op(op, f64::from(int), threshold) {
            continue;
        }
        for (dst, src) in out.iter_mut().zip(rarity_leaf_bits(f64::from(int), rp, n_printings)) {
            *dst |= src;
        }
    }
    out
}

/// Every rarity int the store actually holds: the four interior values (planes, always laid out) plus
/// whatever the sparse tail carries. One definition, shared by the compose leaf above and
/// `walk_rarity_orderby_page`'s bucket walk, so the two cannot disagree about the domain.
fn rarity_ints_present(rp: &Archived<RarityPrintingPlanes>) -> Vec<u8> {
    let mut values: Vec<u8> = RARITY_PRINTING_PLANE_INTS.to_vec();
    values.extend(rp.postings.iter().map(|e| e.0));
    values.sort_unstable();
    values.dedup();
    values
}

/// #746: the exact printing-space bitmap for a bare tag-postings leaf (`set:VALUE`/`watermark:VALUE`)
/// — scatter the value's sorted postings (`indexes.set_codes`/`indexes.watermarks`) into a fresh
/// bitmap, or all-zero for a value absent from the index (exactly "no printing", matching
/// `narrow_rec`'s empty-is-exact treatment of an unknown code). This is an intersection with the
/// postings set, never a complement, so it is exact for the non-nullable `set_code` field and the
/// nullable `watermark` field alike — the trivalent-NULL trap that keeps negation off the compose
/// path for a nullable field (see `set_code_negated_leaf_bits`) does not apply to a positive leaf.
fn tag_postings_leaf_bits(index: &Archived<TagIndex>, value: &str, n_printings: usize) -> Vec<u64> {
    match index.get(value) {
        Some(v) => scatter_bits(v.iter().map(|p| u32::from(*p)), n_printings),
        None => vec![0u64; words_per_plane(n_printings)],
    }
}

/// #746: the exact printing-space bitmap for a negated set leaf (`-set:VALUE`) — all-ones with the
/// value's postings cleared. Exact **only because `set_code` is non-nullable**: every printing
/// belongs to exactly one set, so "every printing except those in VALUE" is precisely the printings
/// matching `-set:VALUE`, with no null-valued printing to wrongly include. Cost rides the *positive*
/// postings size (the bits cleared), never the complement, which is why this is a strictly cheaper
/// shape than the generic `Not` complement. Deliberately **not** reused for `watermark:` (nullable):
/// a no-watermark printing satisfies neither `watermark:x` nor `-watermark:x`, so all-ones-minus-
/// postings would wrongly count it as a `-watermark:x` match — the same trivalent trap dates hit in
/// docs/issues/done/00741-engine-negated-range-narrowing.md; negating a nullable field would need an
/// explicit "has any watermark" known-mask, which this leaf does not build.
fn set_code_negated_leaf_bits(index: &Archived<TagIndex>, value: &str, n_printings: usize) -> Vec<u64> {
    let mut bits = all_printing_bits(n_printings);
    if let Some(v) = index.get(value) {
        for p in v.iter() {
            let pid = u32::from(*p) as usize;
            bits[pid >> 6] &= !(1u64 << (pid & 63));
        }
    }
    bits
}

/// #731: the exact printing bitmap for a usd/cn/date range leaf — scatter the value-sorted index
/// slice `[lo, hi)` (the same slice `build_card_range_bits` walks). Index-absent printings (no
/// price/collector-number/date) aren't in the index at all, so they're excluded by construction —
/// this is an intersection with the in-range set, never a complement, so the trivalent-NULL trap that
/// keeps `Not` off the compose path doesn't apply here.
fn range_leaf_bits(idx: &Archived<PrintingValueIndex>, lo: u32, hi: u32, n_printings: usize) -> Vec<u64> {
    let mut bits = vec![0u64; n_printings.div_ceil(64)];
    for pid in idx.range_pids(lo, hi) {
        let pid = pid as usize;
        bits[pid >> 6] |= 1u64 << (pid & 63);
    }
    bits
}

/// Broadcast a card-space bitmap **down** to printing space: set every printing of each set card. The
/// inverse of `printing_bits_to_card_bits`, used to lift a card-settled fact (a legality that doesn't
/// diverge across the card's printings) into the printing domain for composition. Iterates set cards
/// only, so it is O(set cards + their printings), not O(n_cards).
fn broadcast_card_bits_to_printings(card_bits: &[u64], offsets: &AOffsets, n_printings: usize) -> Vec<u64> {
    let mut pbits = vec![0u64; words_per_plane(n_printings)];
    for (i, &word) in card_bits.iter().enumerate() {
        let mut w = word;
        while w != 0 {
            let c = (((i as u32) << 6) | w.trailing_zeros()) as usize;
            w &= w - 1;
            for p in u32::from(offsets[c]) as usize..u32::from(offsets[c + 1]) as usize {
                pbits[p >> 6] |= 1u64 << (p & 63);
            }
        }
    }
    pbits
}

/// Scatter each card id's whole printing range (`offsets[c]..offsets[c+1]`) into a fresh printing
/// bitmap — the card-**id-list** analogue of `broadcast_card_bits_to_printings` (which takes a card
/// *bitmap*). Used to lift a card-space collection posting list (`subtypes`/`keywords`/`oracle_tags`,
/// whose postings are card ids) up into the printing domain for composition. O(card ids + their
/// printings), independent of `n_cards`.
fn broadcast_card_ids_to_printings(card_ids: impl Iterator<Item = u32>, offsets: &AOffsets, n_printings: usize) -> Vec<u64> {
    let mut pbits = vec![0u64; words_per_plane(n_printings)];
    for c in card_ids {
        let c = c as usize;
        for p in u32::from(offsets[c]) as usize..u32::from(offsets[c + 1]) as usize {
            pbits[p >> 6] |= 1u64 << (p & 63);
        }
    }
    pbits
}

/// Maps a `CollectionCmp` field to its compose backing: the postings index and whether it is
/// **card-space** (postings are card ids → project each card's printing range up with
/// `broadcast_card_ids_to_printings`) or **printing-space** (postings are printing ids → scatter
/// directly, like `set:`/`watermark:`). Returns `None` for `FrameData` — the only non-`complete`
/// collection index (its dense values are dropped at build, #628, so absence proves nothing there and
/// it cannot be an exact compose leaf); it stays on the general path. This is the single source of
/// truth `is_printing_composable`/`compose_printing_bits`/`compose_printing_estimate` share so their
/// field tables can't drift apart.
/// Which structure backs a `CollectionCmp` field on the compose path.
enum CollComposeSource<'i> {
    /// Postings. `true` = card-space ids (project each card's printing range up with
    /// `broadcast_card_ids_to_printings`), `false` = printing ids (scatter directly).
    Postings(&'i Archived<TagIndex>, bool),
    /// The hybrid printing-space index: a stored bitmap for dense values, postings for the tail.
    Hybrid(&'i Archived<HybridTagIndex>),
}

fn collection_compose_index(indexes: &Archived<CardIndexes>, field: CollField) -> Option<CollComposeSource<'_>> {
    Some(match field {
        CollField::Subtypes   => CollComposeSource::Postings(&indexes.subtypes,    true),
        CollField::Keywords   => CollComposeSource::Postings(&indexes.keywords,    true),
        CollField::OracleTags => CollComposeSource::Postings(&indexes.oracle_tags, true),
        CollField::ArtTags    => CollComposeSource::Postings(&indexes.art_tags,    false),
        CollField::IsTags     => CollComposeSource::Postings(&indexes.is_tags,     false),
        // `frame_data` used to return `None` here, and that exclusion was never about frames as such:
        // it was the one index that was not `complete`, because its dense values were dropped at build,
        // so absence could not prove emptiness and it could not be an exact compose leaf. Storing every
        // value makes it complete, which makes it composable — and that chain is most of this change's
        // win: `frame:2015` under `unique=printing` went 1,375 -> 41 us.
        CollField::FrameData => CollComposeSource::Hybrid(&indexes.frame_data),
    })
}

/// The exact printing-space bitmap for a bare containment collection leaf (`type:`/`kw:`/`otag:`/
/// `art:`/`is:`, i.e. `CollectionCmp { op: Ge }`) over a `complete` index. For a card-space field the
/// value's card-id postings are projected up (every printing of a matching card — subtype/keyword/
/// oracle-tag are pure **card** properties, so this projection is exact, no per-printing divergence
/// like legality); for a printing-space field the printing-id postings scatter directly. A value
/// absent from a complete index matches no row (all-zero) — the same empty-is-exact treatment
/// `narrow_rec` gives an unknown value. **Ge/containment only** (the caller gates it): the postings
/// are tight for `Ge`, but only a loose superset for `Eq`/`Gt` (they prove `contains(value)`, not the
/// collection-length condition, ~lib.rs:3441), and the compose path has no residual re-check, so
/// `Eq`/`Gt` stay on the general path.
fn collection_leaf_bits(src: &CollComposeSource, value: &str, offsets: &AOffsets, n_printings: usize) -> Vec<u64> {
    match src {
        // A dense value is already a printing bitmap, so this is a copy rather than a scatter.
        CollComposeSource::Hybrid(idx) => {
            idx.bits(value, n_printings).unwrap_or_else(|| vec![0u64; words_per_plane(n_printings)])
        }
        CollComposeSource::Postings(idx, card_space) => match idx.get(value) {
            None => vec![0u64; words_per_plane(n_printings)],
            Some(v) if *card_space => broadcast_card_ids_to_printings(v.iter().map(|x| u32::from(*x)), offsets, n_printings),
            Some(v) => scatter_bits(v.iter().map(|x| u32::from(*x)), n_printings),
        },
    }
}

/// The number of printings a containment collection leaf matches (its exact `unique=printing` total),
/// for the cost-model estimate. Card-space: sum each matching card's printing-range length; printing-
/// space: the postings length (one posting = one printing). Exact (a valid upper bound and then some),
/// so the estimate for a bare collection leaf is exact, matching set/watermark. O(card postings) — the
/// same order as the eventual scatter, cheap for the sparse subtype/keyword/oracle-tag posting lists.
fn collection_leaf_printing_count(src: &CollComposeSource, value: &str, offsets: &AOffsets) -> usize {
    match src {
        // A popcount when the value is a bitmap; a postings length otherwise.
        CollComposeSource::Hybrid(idx) => idx.len_of(value).unwrap_or(0),
        CollComposeSource::Postings(idx, card_space) => match idx.get(value) {
            None => 0,
            Some(v) if *card_space => v
                .iter()
                .map(|c| {
                    let c = u32::from(*c) as usize;
                    (u32::from(offsets[c + 1]) - u32::from(offsets[c])) as usize
                })
                .sum(),
            Some(v) => v.len(),
        },
    }
}

/// The exact printing-space bitmap for a **negated** containment collection leaf (`-type:`/`-kw:`/
/// `-otag:`/`-art:`/`-is:`) over a `complete` index — the positive leaf's bits, complemented. Exact
/// for all these fields (unlike a nullable scalar like `watermark`): a collection is never NULL — a
/// card/printing that lacks the value has a definite `contains(value) == false`, so the complement of
/// the (exact, `Ge`) positive set is precisely the negated match set, with no trivalent-NULL printing
/// wrongly swept in. Ge only, same reason as the positive leaf (a loose positive set would give a
/// loose complement).
fn collection_negated_leaf_bits(src: &CollComposeSource, value: &str, offsets: &AOffsets, n_printings: usize) -> Vec<u64> {
    let mut bits = collection_leaf_bits(src, value, offsets, n_printings);
    complement_bits(&mut bits, n_printings);
    bits
}

/// Repair the divergent cards' printings authoritatively — overwrite each bit with the per-printing
/// truth (`(word >> shift) & 0b11 == expected`, the same test filter.rs:1253 applies). Authoritative
/// (set AND clear) so either build direction can over-set/over-clear a divergent card without a
/// pre-mask pass. Iterates the global `legal_divergent` list (a superset of the per-format divergent
/// set); a card divergent in another format but not this one has all its printings agree here, so its
/// repair is a no-op. Callers gate this on there being any per-format divergence at all (#744).
fn repair_divergent_printings(
    pbits: &mut [u64],
    shift: u8,
    expected: u64,
    legal_divergent: &Archived<Vec<u16>>,
    offsets: &AOffsets,
    printings: &[APrinting],
) {
    for cid in legal_divergent.iter() {
        let c = u16::from(*cid) as usize;
        for p in u32::from(offsets[c]) as usize..u32::from(offsets[c + 1]) as usize {
            let legal = (u64::from(printings[p].card_legalities) >> shift) & 0b11 == expected;
            if legal {
                pbits[p >> 6] |= 1u64 << (p & 63);
            } else {
                pbits[p >> 6] &= !(1u64 << (p & 63));
            }
        }
    }
}

/// #724 build: broadcast the *legal* (`_EXISTS`) card plane down and repair. Cheapest when the legal
/// set is the *minority* (e.g. `oldschool`, ~3% legal) — the broadcast touches only set (legal) cards.
fn legality_leaf_bits_from_exists(
    shift: u8,
    expected: u64,
    exists: &[u64],
    legal_divergent: &Archived<Vec<u16>>,
    offsets: &AOffsets,
    printings: &[APrinting],
    n_printings: usize,
) -> Vec<u64> {
    let mut pbits = broadcast_card_bits_to_printings(exists, offsets, n_printings);
    repair_divergent_printings(&mut pbits, shift, expected, legal_divergent, offsets, printings);
    pbits
}

/// #744 build: start printing-space all-ones and clear each *illegal* (`_ABSENT`) card's printing
/// range. Cheapest when the legal set is the *majority* (a near-universal format like `commander`,
/// ~99.7% legal): the clear touches only the tiny illegal set + its printings, not the ~99.7% of cards
/// a legal-side broadcast would. Skips the repair pass entirely when this format has zero divergent
/// cards (`exists ∧ absent` empty — true for `commander` in the real corpus). Produces bit-for-bit the
/// same bitmap as `legality_leaf_bits_from_exists` — the authoritative repair overwrites every
/// divergent printing to its per-printing truth regardless of the starting side.
#[allow(clippy::too_many_arguments)]
fn legality_leaf_bits_from_absent(
    shift: u8,
    expected: u64,
    exists: &[u64],
    absent: &[u64],
    legal_divergent: &Archived<Vec<u16>>,
    offsets: &AOffsets,
    printings: &[APrinting],
    n_printings: usize,
) -> Vec<u64> {
    let mut pbits = all_printing_bits(n_printings);
    for cid in bitmap_card_ids(absent) {
        let c = cid as usize;
        for p in u32::from(offsets[c]) as usize..u32::from(offsets[c + 1]) as usize {
            pbits[p >> 6] &= !(1u64 << (p & 63));
        }
    }
    // Divergent-in-this-format cards (∃ legal ∧ ∃ illegal printing) are exactly `exists ∧ absent`;
    // repair only if there are any (skip the whole pass for a format with none, e.g. commander).
    let divergent = exists.iter().zip(absent.iter()).map(|(&a, &b)| (a & b).count_ones()).sum::<u32>();
    if divergent > 0 {
        repair_divergent_printings(&mut pbits, shift, expected, legal_divergent, offsets, printings);
    }
    pbits
}

/// The exact printing-space bitmap for a bare legality leaf (`f:modern` etc.), built the #724 way from
/// #667's card-space `_EXISTS`/`_ABSENT` planes plus a divergent repair, rather than a full per-printing
/// legality plane. **Builds from whichever side is sparser** (#744): the legal-card popcount decides —
/// majority-legal (`commander`) clears the tiny illegal set from an all-ones start; minority-legal
/// (`oldschool`) broadcasts the small legal set. Same "pick the cheaper side" shape `range_narrowed`
/// uses (`if k <= idx.len() - k`), and both directions yield the identical bitmap (the repair is
/// authoritative). Empty if the planes aren't built for this store.
fn legality_leaf_bits(
    shift: u8,
    expected: u64,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    printings: &[APrinting],
    n_printings: usize,
) -> Vec<u64> {
    let n_cards = offsets.len() - 1;
    let Some(exists) = legality_candidate_bits(indexes, n_cards, shift, expected, false) else {
        return vec![0u64; words_per_plane(n_printings)];
    };
    let legal_cards = exists.iter().map(|w| w.count_ones() as usize).sum::<usize>();
    if legal_cards * 2 > n_cards {
        // Majority legal: the *illegal* set is the sparse side — build from `_ABSENT`.
        let absent =
            legality_candidate_bits(indexes, n_cards, shift, expected, true).expect("exists resolved ⇒ absent resolves");
        legality_leaf_bits_from_absent(shift, expected, &exists, &absent, &indexes.legal_divergent, offsets, printings, n_printings)
    } else {
        legality_leaf_bits_from_exists(shift, expected, &exists, &indexes.legal_divergent, offsets, printings, n_printings)
    }
}

/// #724: materialize `filter`'s **exact** printing-space membership bitmap (`n_printings` bits, tail
/// masked to 0), composing planes/postings with `AND`/`OR`. Assumes `is_printing_composable` (the
/// caller gates it) — `unreachable!()` on any other shape. The surviving bits *are* the matching
/// printings: for `unique=printing` `popcount` is the total; for `unique=card` project up with
/// `printing_bits_to_card_bits`. This is the substrate the printing-space popcount-order plan consumes.
fn compose_printing_bits(
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    printings: &[APrinting],
    n_printings: usize,
) -> Vec<u64> {
    let wpp = words_per_plane(n_printings);
    match filter {
        FilterExpr::True => all_printing_bits(n_printings),
        FilterExpr::And(v) => {
            // empty And is vacuously true; start all-ones (tail masked) and intersect each source.
            // Same-index range children fuse into one interval first, so `usd>=0.42 usd<=0.43` scatters
            // the 879-printing intersection once instead of scattering 33,862 and 48,559 and ANDing
            // them. Unconditional (`sparse_only: false`): `range_leaf_bits` is an O(k) scatter at every
            // k, so one scatter of a subset can never lose to two of its supersets.
            let mut acc = all_printing_bits(n_printings);
            for src in fuse_and_range_children(v, indexes, false) {
                let bits = match src {
                    AndSource::Child(c) => compose_printing_bits(c, indexes, offsets, printings, n_printings),
                    AndSource::FusedRange { idx, lo, hi, .. } => range_leaf_bits(idx, lo, hi, n_printings),
                };
                and_bits_into(&mut acc, &bits);
            }
            acc
        }
        FilterExpr::Or(v) => {
            let mut acc = vec![0u64; wpp];
            for child in v.iter() {
                or_bits_into(&mut acc, &compose_printing_bits(child, indexes, offsets, printings, n_printings));
            }
            acc
        }
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value } => {
            border_leaf_bits(value.as_str(), &indexes.border_printing, n_printings)
        }
        // #746: `set:VALUE`/`watermark:VALUE` postings leaves — scatter the value's printing ids.
        FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value } => {
            tag_postings_leaf_bits(&indexes.set_codes, value.as_str(), n_printings)
        }
        FilterExpr::TextExact { field: TextField::Watermark, op: CmpOp::Eq, value } => {
            tag_postings_leaf_bits(&indexes.watermarks, value.as_str(), n_printings)
        }
        // #746: `-set:VALUE` — all-ones minus the value's postings (exact; `set_code` is non-null).
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, .. }) =>
        {
            let FilterExpr::TextExact { value, .. } = inner.as_ref() else {
                unreachable!("guarded by the matches! above")
            };
            set_code_negated_leaf_bits(&indexes.set_codes, value.as_str(), n_printings)
        }
        // Collection containment leaf (`type:`/`kw:`/`otag:`/`art:`/`is:`, `Ge`) — card-space postings
        // projected up / printing-space postings scattered (see `collection_leaf_bits`).
        FilterExpr::CollectionCmp { field, op: CmpOp::Ge, value, .. }
            if collection_compose_index(indexes, *field).is_some() =>
        {
            let src = collection_compose_index(indexes, *field).expect("guarded by the if");
            collection_leaf_bits(&src, value.as_str(), offsets, n_printings)
        }
        // Negated collection leaf — the exact complement of the positive leaf.
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::CollectionCmp { field, op: CmpOp::Ge, .. } if collection_compose_index(indexes, *field).is_some()) =>
        {
            let FilterExpr::CollectionCmp { field, value, .. } = inner.as_ref() else {
                unreachable!("guarded by the matches! above")
            };
            let src = collection_compose_index(indexes, *field).expect("guarded by the matches!");
            collection_negated_leaf_bits(&src, value.as_str(), offsets, n_printings)
        }
        // `flip_op` on the const-first form so `2<=rarity` and `rarity>=2` build the same bitmap.
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op, rhs: NumExpr::Const(c) } => {
            rarity_cmp_leaf_bits(*op, *c, &indexes.rarity_printing, n_printings)
        }
        FilterExpr::NumericCmp { lhs: NumExpr::Const(c), op, rhs: NumExpr::Field(NumField::RarityInt) } => {
            rarity_cmp_leaf_bits(flip_op(*op), *c, &indexes.rarity_printing, n_printings)
        }
        FilterExpr::Legality { shift: Some(shift), expected } => {
            legality_leaf_bits(*shift, *expected, indexes, offsets, printings, n_printings)
        }
        FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } | FilterExpr::Not(_)
            if bare_range_bounds(filter, indexes).is_some() =>
        {
            let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("guarded by bare_range_bounds");
            range_leaf_bits(idx, lo, hi, n_printings)
        }
        _ => unreachable!("compose_printing_bits on a non-composable filter — gated by is_printing_composable"),
    }
}

/// Cheap cost-model estimate for a composable filter: `(matches, broadcast_printings, scatter_printings)`
/// **without** paying legality's broadcast. The two synthesis kinds are returned separately because they
/// cost different rates (`LINEAR_PASS_PER_PRINTING_NS` vs `RANGE_SCATTER_PER_PRINTING_NS`): a legality
/// leaf is *broadcast* at query time (card `_EXISTS` popcount scaled to printings → `broadcast`), while a
/// range leaf *scatters* its index slice `k` (→ `scatter`). Border/rarity are precomputed planes — a
/// cheap `popcount` slice read, synthesizing nothing (both `0`). The fast path pays the broadcast/scatter
/// **only if this plan wins** (why acquire estimates rather than composing — it avoids a throwaway pass).
/// `AND` takes the min matches (intersection upper bound) and sums each build kind; `OR` the capped sum.
/// Used only for plan choice — the fast path recomputes the exact total.
/// What `compose_printing_estimate` returns: the composed set's size, twice.
///
/// `result` is the best available estimate — exact where the pair table or a single-leaf table answers.
/// `candidate` is the plain `min`-over-single-leaves bound, which is what the MATERIALIZING alternatives
/// actually walk: `narrow_rec` declines broad children (`border:black` at 87% under `broad_ok: false`),
/// so their candidate set is the surviving leaf's, not the intersection.
///
/// Keeping them apart is the whole point. Feeding an exact intersection into `eval_domain`/`scan_units`
/// prices `GatheredScan` on `border:white border:black` at 0.2 us against a measured 199.3 us, because a
/// plan still has to scan to DISCOVER a set is empty. A result total is not a scan domain — the same
/// distinction `exact_cards` vs `exact_total` draws one level down.
#[derive(Clone, Copy)]
struct ComposeEstimate {
    result: usize,
    candidate: usize,
    broadcast: usize,
    scatter: usize,
}

impl ComposeEstimate {
    /// A leaf: nothing to tighten, so both figures are the same count.
    fn leaf(k: usize, broadcast: usize, scatter: usize) -> Self {
        Self { result: k, candidate: k, broadcast, scatter }
    }
}

fn compose_printing_estimate(
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    n_printings: usize,
) -> ComposeEstimate {
    let popcount = |bits: &[u64]| bits.iter().map(|w| w.count_ones() as usize).sum::<usize>();
    match filter {
        FilterExpr::True => ComposeEstimate::leaf(n_printings, 0, 0),
        // The min-of-children fold is an intersection UPPER BOUND, and on a two-sided range it is a bad
        // one: `usd>=0.42 usd<=0.43` folded to min(33,862, 48,559) against a true 879, and the summed
        // scatter to 82,421 for an 879-row answer. Fusing same-index children first replaces both with
        // the interval's exact `k` — the same two `partition_point` calls the one-sided arm below
        // already makes, which is why a one-sided range estimates at 1.0x and this did not.
        FilterExpr::And(v) => {
            let folded = fuse_and_range_children(v, indexes, false)
                .into_iter()
                .map(|src| match src {
                    AndSource::Child(c) => compose_printing_estimate(c, indexes, offsets, n_printings),
                    AndSource::FusedRange { k, .. } => ComposeEstimate::leaf(k, 0, k),
                })
                .fold(ComposeEstimate::leaf(n_printings, 0, 0), |a, c| ComposeEstimate {
                    result: a.result.min(c.result),
                    candidate: a.candidate.min(c.candidate),
                    broadcast: a.broadcast + c.broadcast,
                    scatter: a.scatter + c.scatter,
                });
            // Tighten the `min` bound with every PAIR of children the table stores. `min` over singles
            // lets the most selective leaf decide alone, which is why `f:modern r:rare border:white`
            // estimated 5,131 -- `border:white`'s own count -- against a true 658. The pair
            // `r:rare border:white` is stored exactly at 1,330, taking the bound from 7.80x to 2.02x.
            //
            // `n choose 2` over an `And`'s children, bounded in practice by how many predicates a person
            // types; the two-leaf case is answered exactly one level up in `exact_result_total` and never
            // needs this.
            // Only `result` is tightened. `candidate` keeps the untightened `min`, because that is what
            // narrowing leaves the alternatives to walk once its broad children decline.
            ComposeEstimate { result: pair_bounded_min(v, indexes, folded.result), ..folded }
        }
        FilterExpr::Or(v) => {
            let summed = v
                .iter()
                .map(|c| compose_printing_estimate(c, indexes, offsets, n_printings))
                .fold(ComposeEstimate::leaf(0, 0, 0), |a, c| ComposeEstimate {
                    result: a.result + c.result,
                    candidate: a.candidate + c.candidate,
                    broadcast: a.broadcast + c.broadcast,
                    scatter: a.scatter + c.scatter,
                });
            ComposeEstimate {
                result: summed.result.min(n_printings),
                candidate: summed.candidate.min(n_printings),
                ..summed
            }
        }
        // Precomputed planes: exact cheap popcount, nothing synthesized.
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value } => {
            ComposeEstimate::leaf(popcount(&border_leaf_bits(value.as_str(), &indexes.border_printing, n_printings)), 0, 0)
        }
        // #746: `set:`/`watermark:` postings — matches = the value's postings length `k` (each
        // posting is one distinct printing), synthesized by scattering `k` ids → rides `scatter`
        // (the same cheap range-slice scatter rate). O(1) here: the length, no bitmap built.
        FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value } => {
            let k = indexes.set_codes.get(value.as_str()).map_or(0, |v| v.len());
            ComposeEstimate::leaf(k, 0, k)
        }
        FilterExpr::TextExact { field: TextField::Watermark, op: CmpOp::Eq, value } => {
            let k = indexes.watermarks.get(value.as_str()).map_or(0, |v| v.len());
            ComposeEstimate::leaf(k, 0, k)
        }
        // #746: `-set:VALUE` — matches = all printings minus the value's postings; the scatter cost
        // rides the (small) positive postings size cleared, not the (large) complement it produces.
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, .. }) =>
        {
            let FilterExpr::TextExact { value, .. } = inner.as_ref() else {
                unreachable!("guarded by the matches! above")
            };
            let k = indexes.set_codes.get(value.as_str()).map_or(0, |v| v.len());
            ComposeEstimate::leaf(n_printings.saturating_sub(k), 0, k)
        }
        // Collection containment leaf (`type:`/`kw:`/`otag:`/`art:`/`is:`, `Ge`): `k` = the exact
        // printing count the leaf matches (card-space sums the matching cards' printing ranges,
        // printing-space is the postings length). The build scatters `k` printings → rides `scatter`,
        // the cheap range-slice rate.
        FilterExpr::CollectionCmp { field, op: CmpOp::Ge, value, .. }
            if collection_compose_index(indexes, *field).is_some() =>
        {
            let src = collection_compose_index(indexes, *field).expect("guarded by the if");
            let k = collection_leaf_printing_count(&src, value.as_str(), offsets);
            ComposeEstimate::leaf(k, 0, k)
        }
        // Negated collection leaf: all printings minus the positive `k`; the scatter cost rides the
        // (small) positive `k` cleared, not the (large) complement it produces — same shape as `-set:`.
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::CollectionCmp { field, op: CmpOp::Ge, .. } if collection_compose_index(indexes, *field).is_some()) =>
        {
            let FilterExpr::CollectionCmp { field, value, .. } = inner.as_ref() else {
                unreachable!("guarded by the matches! above")
            };
            let src = collection_compose_index(indexes, *field).expect("guarded by the matches!");
            let k = collection_leaf_printing_count(&src, value.as_str(), offsets);
            ComposeEstimate::leaf(n_printings.saturating_sub(k), 0, k)
        }
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op, rhs: NumExpr::Const(c) } => {
            ComposeEstimate::leaf(popcount(&rarity_cmp_leaf_bits(*op, *c, &indexes.rarity_printing, n_printings)), 0, 0)
        }
        FilterExpr::NumericCmp { lhs: NumExpr::Const(c), op, rhs: NumExpr::Field(NumField::RarityInt) } => {
            ComposeEstimate::leaf(popcount(&rarity_cmp_leaf_bits(flip_op(*op), *c, &indexes.rarity_printing, n_printings)), 0, 0)
        }
        // Legality: matches ≈ the legal-∃ cards' printings (existence-scaled from the cheap card
        // ∃-plane popcount). The build cost rides the *sparser* side (#744): a majority-legal format
        // clears its tiny illegal set instead of broadcasting the ~all legal one, so `broadcast` is
        // scaled from `min(legal, illegal)` cards, not `legal` — otherwise the model would keep costing
        // the pre-#744 full broadcast a near-universal format no longer pays.
        FilterExpr::Legality { shift: Some(shift), expected } => {
            let n_cards = offsets.len() - 1;
            let legal = legality_candidate_bits(indexes, n_cards, *shift, *expected, false).map_or(0, |b| popcount(&b));
            let illegal = legality_candidate_bits(indexes, n_cards, *shift, *expected, true).map_or(0, |b| popcount(&b));
            let scale = |c: usize| (c * n_printings).checked_div(n_cards).unwrap_or(0);
            ComposeEstimate::leaf(scale(legal), scale(legal.min(illegal)), 0)
        }
        // Range (bare or negated — `-usd<50` etc., see `bare_range_bounds`'s doc): `k` in-range
        // printings from the index partition points (O(log n), no scatter here); matches ≈ k, and k
        // rides `scatter` — the cheap range-slice scatter into the printing bitmap.
        FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } | FilterExpr::Not(_)
            if bare_range_bounds(filter, indexes).is_some() =>
        {
            let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("guarded by bare_range_bounds");
            let (s, e) = idx.range(lo, hi);
            ComposeEstimate::leaf(e - s, 0, e - s)
        }
        _ => unreachable!("compose_printing_estimate on a non-composable filter — gated by is_printing_composable"),
    }
}

/// The two `orderby` values with no card-space sort permutation (`SortCol::PriceUsd`/`Rarity` — see
/// `ArchivedSortPermutations::get`, which returns `None` for both because the representative printing's
/// key can't be precomputed). Each nonetheless has a printing-space `PrintingValueIndex` — `price_usd`
/// and `rarity_printing_ordered` — that a `unique=printing` page can be walked directly. #744's walk
/// uses one to emit a page in sort order without visiting every candidate, the opposite shape from
/// `gather_composed_page`. Any other `orderby` either has a permutation (`walk_grouped_page`) or falls
/// to the gather fallback.
///
/// The two used to differ in *structure* — a pair vec against planes/postings — and so had a walk
/// each. They now share one layout and one walk (`walk_value_orderby_page`), which is what makes this
/// predicate mean exactly "there is a value index to walk".
fn orderby_walk_available(sort_col: SortCol) -> bool {
    matches!(sort_col, SortCol::PriceUsd | SortCol::Rarity)
}

/// The routed path's time, split into DISJOINT phases that cover all of `run_query_routed`.
///
/// Everything else here measures one participant at a time and cannot be added up. `acquire_ns` is
/// timed in its own `explain_analyze` round — a standalone `acquire_plan_features` call with its own
/// cache state — while `routed_ns` times a SEPARATE execution that does its own acquire inside. They
/// are two independent measurements of overlapping work, not a part and a whole, which is why
/// `acquire_ns / routed_ns` measured a nonsensical 104% at the median on candidate-acquired queries.
///
/// These three are read from ONE execution with four contiguous clock reads, so
/// `ns_acquire + ns_choose + ns_dispatch` accounts for the whole call bar the reads themselves. The
/// same shape `PhaseStats` uses for setup/loop/finish, one level up — and those three subdivide
/// `ns_dispatch`, together with `ns_prepare`, so the two nest rather than overlap.
///
/// Why it is worth four clock reads on the production path: `plan_cost` prices only what happens
/// AFTER acquire, so acquire is unpriced for every plan, and cost.rs records the model's median error
/// as 1.09x `acquire_ns`. Acquire is the largest unmodelled component in the engine and nothing has
/// ever measured it as a fraction of the query it belongs to.
/// Behind the `routed-phases` cargo feature, and the reason is measured rather than cautious: the
/// four clock reads cost +2.7us and +2.3us on a ~40us median query in a paired interleaved A/B, both
/// intervals excluding zero. That is ~1.6% of every request to answer a question a diagnostic build
/// can answer instead — the same trade `alloc-counter` already makes.
///
/// With the feature off, `RoutedPhaseTimer` is a zero-sized struct whose methods are empty, so the
/// reads and the publish compile away entirely.
#[derive(Default, Clone, Copy)]
struct RoutedPhases {
    /// `acquire_plan_features`: count source, feature build, and whatever artifact it materializes.
    ns_acquire: u64,
    /// The `argmin cost::plan_cost` over applicable plans. Expected to be negligible; measured so
    /// that "expected" is not doing the work — it comes out at 41ns, 0.0% of the query.
    ns_choose: u64,
    /// Running the winner, including a lazy re-materialize and re-choose when a fastpath declines.
    ns_dispatch: u64,
}

/// Marks the three phase boundaries in `run_query_routed`, or nothing at all when the
/// `routed-phases` feature is off. See `RoutedPhases`.
#[cfg(feature = "routed-phases")]
struct RoutedPhaseTimer {
    entry: std::time::Instant,
    acquired: Option<std::time::Instant>,
    chosen: Option<std::time::Instant>,
}

#[cfg(feature = "routed-phases")]
impl RoutedPhaseTimer {
    fn start() -> Self {
        Self { entry: std::time::Instant::now(), acquired: None, chosen: None }
    }
    fn acquired(&mut self) {
        self.acquired = Some(std::time::Instant::now());
    }
    fn chosen(&mut self) {
        self.chosen = Some(std::time::Instant::now());
    }
    /// Publishes the three disjoint spans. A boundary that was never marked collapses to the entry
    /// instant, which cannot happen on the one path that exists but keeps this total.
    fn finish(self) {
        let done = std::time::Instant::now();
        let acquired = self.acquired.unwrap_or(self.entry);
        let chosen = self.chosen.unwrap_or(acquired);
        ROUTED_PHASES.with(|c| {
            c.set(RoutedPhases {
                ns_acquire: (acquired - self.entry).as_nanos() as u64,
                ns_choose: (chosen - acquired).as_nanos() as u64,
                ns_dispatch: (done - chosen).as_nanos() as u64,
            });
        });
    }
}

#[cfg(not(feature = "routed-phases"))]
struct RoutedPhaseTimer;

#[cfg(not(feature = "routed-phases"))]
impl RoutedPhaseTimer {
    #[inline(always)]
    fn start() -> Self {
        Self
    }
    #[inline(always)]
    fn acquired(&mut self) {}
    #[inline(always)]
    fn chosen(&mut self) {}
    #[inline(always)]
    fn finish(self) {}
}

/// The last routed execution's phase split, cleared. All zeros without the `routed-phases` feature,
/// which is what `explain_analyze` then reports — a consumer sees three empty-looking spans rather
/// than a missing key, so the schema does not change with the feature.
fn take_routed_phases() -> RoutedPhases {
    #[cfg(feature = "routed-phases")]
    {
        ROUTED_PHASES.with(|c| c.replace(RoutedPhases::default()))
    }
    #[cfg(not(feature = "routed-phases"))]
    {
        RoutedPhases::default()
    }
}

/// What a compose paging branch actually did, against the three quantities its cost arm charges.
///
/// `PrintingCompose` published no executor counters at all until this existed, which made it the one
/// plan `bench_feature_accuracy.py` could say nothing about -- every cell labelled with a compose
/// ACQUIRE was really measuring how well compose's shared feature vector described GatheredScan and
/// StreamedSelect, the plans that do report. Its own arm terms (`compose_scan_printings`,
/// `printings_walked`) were graded against nothing, and its paging table was structurally empty at
/// any sample size. That matters more than the gap in coverage suggests: compose carries ~75% of all
/// routing regret (docs/issues/reference-cost-model-measurement.md).
///
/// The three branches do different work and each fills these differently -- see the doc on each.
#[derive(Default, Clone, Copy)]
struct ComposePageWork {
    /// Cards the branch iterated: candidate cards for the gather, permutation entries consumed for
    /// the forward walk, `0` for the orderby walk (it steps a value structure, not cards).
    cards_visited: u64,
    /// Printings the branch touched: `pbits` membership tests, for all three branches. The orderby
    /// walk's are index entries rather than candidates' printings, but they are the same operation
    /// and the same unit — one bit test per printing considered.
    printings_examined: u64,
    /// Rows the branch pushed. For the gather this is every match (it visits every candidate); for
    /// the two walks it is bounded by the page, which is the whole point of their cost shape.
    matches_pushed: u64,
    /// The whole fastpath's wall time, merged into `ns_loop` by `take_phase_stats`.
    ///
    /// One span, not three. Compose's arm is not decomposed into setup/loop/finish -- it is a build
    /// plus one of three structurally different paging branches -- so there is nothing to attribute
    /// between phases. What the harnesses need is that the phase sum equals the executor's own time,
    /// and one span gives that. Carried here rather than written to `PHASE_STATS` separately so the
    /// production compose path still pays ONE store; see this slot's doc for what that cost.
    ns_total: u64,
}

/// The `prefer`-best MATCHING printing of the group `pid` belongs to.
///
/// Byte-identical selection to `walk_grouped_page`'s grouping arm, and that is the whole requirement:
/// strict `>` on `prefer_score` so ties fall to the lowest pid (store order), one group per
/// `artwork_group_id` under `Mode::Artwork`, one group per card under `Mode::Card`. `Mode::Printing`
/// does no grouping, so every printing represents itself.
///
/// `pid` must itself be set in `pbits`. That makes the answer well-defined -- the group has at least
/// one matching printing, so it has a best one -- and it is what lets a caller use `rep == pid` as the
/// test for "this encounter is the group's row".
///
/// Cost is the card's own printing span, ~3.08 printings on the production corpus. The gather already
/// demonstrates the same resolution at 1.01 `pbits` tests per card under `Prefer::Default` (printings
/// are stored prefer-desc, so the first match wins outright); a non-default prefer must score the whole
/// span to find the max, which is the 3.11-per-card figure its `printings_examined` reports.
#[allow(clippy::too_many_arguments)]
fn group_representative(
    cards: &[AOracleCard],
    printings: &[APrinting],
    offsets: &AOffsets,
    pbits: &[u64],
    mode: Mode,
    prefer: Prefer,
    cid: usize,
    pid: usize,
    probes: &mut u64,
) -> usize {
    if matches!(mode, Mode::Printing) {
        return pid;
    }
    let card = &cards[cid];
    // Card mode collapses the whole card into one group; artwork keys on the printing's own group.
    let want_gid = matches!(mode, Mode::Artwork).then(|| u16::from(printings[pid].artwork_group_id));
    let start = u32::from(offsets[cid]) as usize;
    let end = u32::from(offsets[cid + 1]) as usize;
    // Printings are stored DESCENDING by default prefer score within a card, so under
    // `Prefer::Default` the first qualifying printing already holds the maximum and nothing later can
    // beat it -- stop there. That is the same early break `gather_composed_page` takes, measured at
    // 1.01 `pbits` tests per card against 3.11 for a scoring prefer, and it is what keeps this walk's
    // per-encounter cost off the card's full span: heavily reprinted cards sort early under
    // `edhrec_rank` (popular cards are reprinted most), so without it the walk pays ~42 probes per
    // resolution on exactly the queries it is meant to make fast.
    let first_match_wins = matches!(prefer, Prefer::Default);
    let mut best: Option<(usize, f64)> = None;
    for q in start..end {
        *probes += 1;
        if pbits[q >> 6] & (1u64 << (q & 63)) == 0 {
            continue;
        }
        if want_gid.is_some_and(|g| u16::from(printings[q].artwork_group_id) != g) {
            continue;
        }
        if first_match_wins {
            return q;
        }
        let score = prefer_score(card, &printings[q], prefer);
        match best {
            // `score <= b` keeps the incumbent, so equal scores leave the LOWEST pid in place -- the
            // same tie resolution `walk_grouped_page`'s strict `score > *best` produces.
            Some((_, b)) if score <= b => {}
            _ => best = Some((q, score)),
        }
    }
    debug_assert!(best.is_some(), "pid is set in pbits, so its own group must have a representative");
    best.map_or(pid, |(q, _)| q)
}

/// #744 orderby walk, over the one `PrintingValueIndex` layout both walkable sort columns have
/// (`usd` -> `price_usd`, `rarity` -> `rarity_printing_ordered`), for **all three distinct-ons**. Steps
/// key runs in page order -- `keys` forward ascending, backward descending -- bit-tests each entry
/// against `pbits` (the index covers *every* printing with the value, not just this filter's matches),
/// and emits rows straight into the page.
///
/// It emits rather than collects because the layout makes the order already right: `page_cmp` orders on
/// (primary, edhrec_rank, cid, pid), a key run shares the primary and is stored in the rest of that
/// order, and the tiebreak does not flip with direction (`sort_key_bits` negates only the primary). So
/// the concatenation of runs from the page's starting end IS the row order
/// `select_page`/`gather_composed_page` produce, and the walk can stop the instant the page fills.
///
/// # Card and artwork mode: emit only at the representative
///
/// A card- or artwork-mode row is a GROUP, and a group's sort key is its `prefer`-chosen
/// REPRESENTATIVE's key -- not the key of whichever printing the walk met first. Measured rather than
/// assumed: `unique=card orderby=usd desc` returns Timetwister's $8.15 printing while that card's
/// dearest is $51.42, and the page is ordered by the returned price. So emitting on first encounter
/// would silently impose a min-over-group ordering that no other plan produces.
///
/// Each encounter therefore resolves `group_representative` and emits **only when it is the
/// representative itself**. That gives each group exactly one row, at its true position:
///
/// - representative's value BELOW the current run: it was encountered in an earlier run (it matches, so
///   it is in the index) and the group was emitted there.
/// - ABOVE: this encounter is skipped, and the walk reaches the representative later.
/// - EQUAL: the representative is in this same run, so emitting at its position gives the correct
///   tiebreak -- which is why the test is `rep == pid` rather than "first unseen group".
///
/// No dedup set and no buffer: the test is local to each encounter.
///
/// Returns `None` when the runs are exhausted before `page_offset + limit` rows were seen AND fewer
/// than `total` were found. In printing mode that means the remainder are *null-value* matches (in
/// `pbits`, absent from the index, sorting last since a missing primary maps to `u32::MAX`); in the
/// grouped modes it additionally covers a group whose REPRESENTATIVE has no value while some other
/// printing of the group does -- that group sorts last and this walk cannot place it. Either way the
/// caller falls back to `gather_composed_page`. When every row lives in the index (`seen` reaches
/// `total`), a short last page is returned normally.
fn walk_value_orderby_page<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    idx: &Archived<PrintingValueIndex>,
    pbits: &[u64],
    total: usize,
) -> Option<(Vec<(&'a AOracleCard, &'a APrinting)>, ComposePageWork)> {
    let QueryCtx { cards, printings, offsets, indexes, .. } = *ctx;
    let QueryParams { mode, prefer, descending, limit, page_offset, .. } = *params;
    let printing_to_card = &indexes.printing_to_card;
    let want = page_offset + limit;
    let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
    let mut seen = 0usize; // group rows passed: skipped for the offset, then emitted
    // The walk's real work: one `pbits` test per printing considered -- index entries and
    // representative-resolution probes alike. Not `seen`: the entries that miss are the cost
    // `printings_walked` has to predict, and on a clumped filter almost every entry misses.
    let mut examined = 0u64;
    let mut resolutions = 0u64;
    let grouped = !matches!(mode, Mode::Printing);
    let n_keys = idx.keys.len();
    'walk: for step in 0..n_keys {
        for t in idx.run(if descending { n_keys - 1 - step } else { step }) {
            examined += 1;
            let pid = idx.pid_at(t);
            if pbits[pid >> 6] & (1u64 << (pid & 63)) == 0 {
                continue;
            }
            let cid = u32::from(printing_to_card[pid]) as usize;
            if grouped {
                resolutions += 1;
                if group_representative(cards, printings, offsets, pbits, mode, prefer, cid, pid, &mut examined) != pid {
                    continue;
                }
            }
            if seen >= page_offset {
                page.push((&cards[cid], &printings[pid]));
            }
            seen += 1;
            if seen == want {
                break 'walk;
            }
        }
    }
    if seen < want && seen < total {
        return None;
    }
    let pushed = page.len() as u64;
    Some((
        page,
        ComposePageWork {
            // One per representative resolution, i.e. per matching index entry in a grouped mode. Zero
            // in printing mode, which steps the value index and never looks at a card.
            cards_visited: resolutions,
            printings_examined: examined,
            // Page-bounded, which is what this counter's doc always claimed.
            matches_pushed: pushed,
            // Filled by `printing_compose_fastpath`, which owns the span; this only reports work.
            ns_total: 0,
        },
    ))
}

/// Exact distinct CARDS for a composed filter, where the shape gives it away for free -- `None` when it
/// would cost real work, leaving the caller on `calibrated_balls_into_bins`.
///
/// Two shapes qualify, and both matter because an exact card total is what lets the router's branch
/// prediction agree with the executor's (see `GROUPED_WALK_MIN_FRACTION`):
///
/// - **A one-sided range.** `RangeCardCounts` stores per-value distinct-card counts, so `<`/`<=`/`>`/
///   `>=`/`==` bisect to an exact answer. A genuinely interior range (`usd>=a usd<=b`) declines --
///   distinct counts do not subtract.
/// - **A bare legality leaf.** `legality_candidate_bits` reads the status's card-space `_EXISTS` plane,
///   and existence-for-some-printing IS the fact `unique=card` counts, so its popcount is the exact
///   total. That count was ALREADY being computed by `compose_printing_estimate` for the build terms and
///   then discarded: the arm returned it scaled into printing space, and the caller ran
///   balls-into-bins over that to recover an ESTIMATE of the number it had started with. Measured on
///   `f:penny`, exact 15,060 cards became an estimated 17,747 -- 1.18x -- and up to 1.53x on sparser
///   formats, purely from the round trip. Legality is ~8% of realistic traffic and was the population
///   whose mispriced branch produced 1.8-2.6x regressions when the grouped walk was gated on the
///   estimate.
///
/// No new stored table: both counts are already on the query path. A per-format count table would work
/// too (there are at most 32 formats x 4 statuses, so ~1 KB) but it would store what a popcount of a
/// plane the query already reads gives for nothing.
/// `min` over every stored pair of an `And`'s children, floored into the caller's existing single-leaf
/// bound. Returns the tighter of the two, and 0 when any two children are provably disjoint.
///
/// Both are still UPPER BOUNDS for three or more children -- the intersection of three sets is at most
/// the smallest pairwise intersection -- but a pairwise bound is much tighter than a single-leaf one
/// whenever the leaves are individually broad, which is exactly when the estimate matters.
fn pair_bounded_min(children: &[FilterExpr], indexes: &Archived<CardIndexes>, single_min: usize) -> usize {
    if children.len() < 2 || !*PAIR_TOTALS {
        return single_min;
    }
    let pt = &indexes.pair_totals;
    let ids: Vec<Option<u16>> = children.iter().map(|c| pair_leaf_id(c, pt)).collect();
    let mut best = single_min;
    for (i, a) in children.iter().enumerate() {
        for (j, b) in children.iter().enumerate().skip(i + 1) {
            if leaves_are_disjoint(a, b) {
                return 0;
            }
            if let (Some(x), Some(y)) = (ids[i], ids[j])
                && let Some(k) = pt.get(x, y, Mode::Printing)
            {
                best = best.min(k);
            }
        }
    }
    best
}

/// The pair-table id for a leaf, or `None` when the leaf's dimension is not covered or its value was
/// pruned by the selectivity floor.
///
/// Deliberately the same four shapes `exact_result_total`'s singleton arms accept, and for the same
/// reasons: `Eq` only on the interned strings (the ordering ops are not a per-value question), `Ge` only
/// on the collection (`Eq`/`Gt` add a length condition containment does not prove), and rarity only at
/// `Eq` (any other op is a range over several values, which no per-value entry answers).
fn pair_leaf_id(filter: &FilterExpr, pt: &ArchivedPairTotals) -> Option<u16> {
    let id = match filter {
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value } => pt.border.get(value.as_str()),
        FilterExpr::CollectionCmp { field: CollField::FrameData, op: CmpOp::Ge, value, .. } => pt.frame.get(value.as_str()),
        FilterExpr::Legality { shift: Some(shift), expected } => pt.legality.get(&legality_totals_key(*shift, *expected).into()),
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(v) }
        | FilterExpr::NumericCmp { lhs: NumExpr::Const(v), op: CmpOp::Eq, rhs: NumExpr::Field(NumField::RarityInt) } => {
            if v.fract() != 0.0 || *v < 0.0 || *v > f64::from(u8::MAX) {
                return None;
            }
            pt.rarity.get(&(*v as u8))
        }
        _ => None,
    }?;
    Some(u16::from(*id))
}

/// Whether two leaves are provably disjoint: distinct values of a dimension that holds exactly ONE value
/// per printing, so no printing can satisfy both.
///
/// `frame_data` is excluded because it is multi-valued -- `frame:2015 frame:legendary` matches 10,321
/// printings. A rule rather than stored data, which is why the pair table need not carry same-dimension
/// entries for the partitions.
fn leaves_are_disjoint(a: &FilterExpr, b: &FilterExpr) -> bool {
    match (a, b) {
        (
            FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: x },
            FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value: y },
        ) => x != y,
        (FilterExpr::Legality { shift: Some(sa), expected: ea }, FilterExpr::Legality { shift: Some(sb), expected: eb }) => {
            sa == sb && ea != eb
        }
        (
            FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(x) },
            FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(y) },
        ) => x != y,
        _ => false,
    }
}

fn exact_result_total(composed: &FilterExpr, indexes: &Archived<CardIndexes>, mode: Mode) -> Option<usize> {
    if let Some((idx, lo, hi)) = bare_range_bounds(composed, indexes) {
        // Printings come free from the index's own partition points; the other two spaces come from the
        // prefix/suffix tables, which now carry an artwork column as well as a card one.
        let (s, e) = idx.range(lo, hi);
        return match mode {
            Mode::Printing => Some(e - s),
            Mode::Card => range_card_counts_for(indexes, idx).and_then(|c| c.distinct_cards(lo, hi)).map(|n| n as usize),
            Mode::Artwork => range_card_counts_for(indexes, idx).and_then(|c| c.distinct_artworks(lo, hi)).map(|n| n as usize),
        };
    }
    // Rarity is the same question one index over, and it gets its own arm rather than joining
    // `bare_range_bounds`: that predicate gates `PrintingRangeScan`/`CardRangePopcount` applicability in
    // a dozen places, so admitting rarity there is a ROUTING change (plausibly a good one -- `r:rare` is
    // 55 us today) and belongs in its own measured commit, not in a counting one.
    if let Some((lo, hi)) = bare_rarity_bounds(composed).filter(|_| *EXACT_VALUE_TOTALS) {
        if hi <= lo {
            return Some(0); // an empty window is an exact zero in every space, not a declined shape
        }
        let idx = &indexes.rarity_printing_ordered;
        let (s, e) = idx.range(lo, hi);
        return match mode {
            Mode::Printing => Some(e - s),
            Mode::Card => indexes.rarity_cards.distinct_cards(lo, hi).map(|n| n as usize),
            Mode::Artwork => indexes.rarity_cards.distinct_artworks(lo, hi).map(|n| n as usize),
        };
    }
    // Two dense low-cardinality leaves: the PAIR table answers them exactly, where the singleton arms
    // below could only answer one of them and the `And` fold would take the `min` bound. This is what
    // makes `f:modern border:white` read 978 cards instead of 2,755 -- the difference between predicting
    // a plan that runs and one that declines against the 1,024 sparse floor.
    if let FilterExpr::And(children) = composed
        && children.len() == 2
        && *PAIR_TOTALS
    {
        if leaves_are_disjoint(&children[0], &children[1]) {
            return Some(0);
        }
        if let (Some(a), Some(b)) = (pair_leaf_id(&children[0], &indexes.pair_totals), pair_leaf_id(&children[1], &indexes.pair_totals))
            && let Some(total) = indexes.pair_totals.get(a, b, mode)
        {
            return Some(total);
        }
    }
    // The per-value table, which covers the dimensions whose predicate tests ONE value, in all three
    // spaces. Absence from a COMPLETE table is an exact zero, not a declined shape -- every one of
    // these maps holds every value present in the corpus, so a miss means nothing matches.
    let vt = &indexes.value_totals;
    match composed {
        // `Eq` only. The ordering ops on an interned string compare lexicographically, which is not a
        // per-value question and has no entry here.
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value } => {
            return Some(vt.border.get(value.as_str()).map_or(0, |t| t.get(mode)));
        }
        FilterExpr::TextExact { field: TextField::Layout, op: CmpOp::Eq, value } => {
            return Some(vt.layout.get(value.as_str()).map_or(0, |t| t.get(mode)));
        }
        // `Ge` on a collection is containment. `Eq`/`Gt` add a collection-LENGTH condition these
        // postings do not prove, so their count is an upper bound rather than a total -- the same
        // distinction the card-space arm below draws.
        FilterExpr::CollectionCmp { field: CollField::FrameData, op: CmpOp::Ge, value, .. } => {
            return Some(vt.frame_data.get(value.as_str()).map_or(0, |t| t.get(mode)));
        }
        FilterExpr::Legality { shift: Some(shift), expected } => {
            return Some(vt.legality.get(&legality_totals_key(*shift, *expected).into()).map_or(0, |t| t.get(mode)));
        }
        // A format absent from all loaded data matches nothing, in every space.
        FilterExpr::Legality { shift: None, .. } => return Some(0),
        _ => {}
    }
    // The remaining shapes are exact in CARD space only, so any other mode falls back to the estimator.
    if !matches!(mode, Mode::Card) {
        return None;
    }
    // A CARD-space containment leaf (`t:`/`keyword:`/`otag:`) posts card ids, so its postings length IS
    // the exact distinct-card count -- the same "the answer was already computed and then discarded"
    // shape the legality arm above fixes. The acquire was projecting the leaf's PRINTING count through
    // balls-into-bins instead, which reads 1.27x on `t:human` (5,411 estimated against 4,249 exact) and
    // up to 2.24x on `t:angel`.
    //
    // `Ge` only. `Eq`/`Gt` share these postings as a loose superset -- they prove containment but not the
    // collection-length condition -- so their count is an upper bound, not a total.
    if let FilterExpr::CollectionCmp { field, op: CmpOp::Ge, value, .. } = composed {
        let card_space_idx = match field {
            CollField::Subtypes => Some(&indexes.subtypes),
            CollField::Keywords => Some(&indexes.keywords),
            CollField::OracleTags => Some(&indexes.oracle_tags),
            // Printing-space: postings are printing ids, so their length is not a card count. These want
            // an import-time count table (the artwork side needs one for every family, including the
            // ranges and formats that are already exact in card space).
            CollField::ArtTags | CollField::IsTags | CollField::FrameData => None,
        };
        // Absent from a complete index is an exact ZERO, not "no answer".
        return card_space_idx.map(|idx| idx.get(value.as_str()).map_or(0, |v| v.len()));
    }
    None
}


/// Whether composing `filter` needs a card-space plane BROADCAST down into printing space -- today,
/// exactly a legality leaf (`repair_divergent_printings` / `broadcast_card_bits_to_printings`). Purely
/// structural, so the router and the executor cannot disagree about it.
fn compose_needs_broadcast(filter: &FilterExpr) -> bool {
    match filter {
        FilterExpr::Legality { .. } => true,
        FilterExpr::And(v) | FilterExpr::Or(v) => v.iter().any(compose_needs_broadcast),
        FilterExpr::Not(inner) => compose_needs_broadcast(inner),
        _ => false,
    }
}

/// Whether the #744 walk beats `gather_composed_page` for this query. The one test both the fastpath
/// and `compose_paging_with_total` apply, so the branch the router PRICES is the branch the executor
/// RUNS.
///
/// Printing mode: always. The walk does no grouping there and terminates at the page, while the gather
/// visits every match, so no density favours the gather.
///
/// Card and artwork: whenever the compose build needs no broadcast, and above the sparse floor. That is
/// not the shape this started with -- two breadth thresholds were tried first and both were wrong,
/// because **breadth does not predict the winner.** Swept over 14 card-mode filters x {usd, rarity} with
/// the branch forced each way:
///
///     breadth  query             walk/gather
///        100%  r<=mythic               0.28x   walk
///        100%  f:duel                  1.25x   gather
///         99%  border:black            0.41x   walk
///         75%  cn>=74 cn<=413          0.20x   walk
///         71%  f:modern                1.08x   gather
///         48%  f:penny                 1.85x   gather
///         45%  cn>200                  0.53x   walk
///         35%  r:rare                  0.57x   walk
///         34%  f:pauper                1.75x   gather
///          9%  r>=mythic               0.54x   walk
///
/// Every legality filter loses and every range/plane filter wins, at every breadth from 9% to 100% --
/// 28 of 28 cells separated by that one bit. The mechanism: a legality leaf's build BROADCASTS a
/// card-space plane across every printing of every matching card, which dominates the query (`f:duel`
/// measures ~180 us on both branches, so paging is noise), and legality is card-invariant, so the
/// gather's early break lands at 1.01 printings per card -- its best possible case. A range or plane
/// leaf composes with a cheap slice or scatter, leaving paging to dominate, which is where an O(page)
/// walk beats an O(matches) gather.
///
/// The earlier `GROUPED_WALK_MIN_FRACTION = 0.75` is gone with the thresholds. It excluded
/// `cn>=74 cn<=413` by 0.4 percentage points on a query where the walk is 5x faster, and it could only
/// ever be fitted, because the quantity it tested is not the one that decides.
///
/// The sparse floor stays and is the one place the two sides see different numbers -- the fastpath's
/// exact `total` against the router's estimate. Harmless for the same reason the `Perm` branch documents
/// for its identical bail: at ~1,000 rows every branch is microseconds, so a query on the boundary is
/// cheap whichever way it is classified.
fn orderby_walk_beats_gather(mode: Mode, filter: &FilterExpr, total: usize) -> bool {
    matches!(mode, Mode::Printing) || (!compose_needs_broadcast(filter) && total > *STREAM_MIN_MATCHES)
}

/// The composed set's size in the query's RESULT space: matching printings, distinct cards, or distinct
/// artworks.
///
/// Extracted because two callers must agree on it exactly. The fastpath reports it as the query's
/// `total`, and `walk_value_orderby_page` compares its own row count against it to decide whether the
/// remainder is an unorderable null-value tail. Give the walk a printing popcount in a grouped mode and
/// it thinks rows are missing that never existed, and declines every page.
///
/// `card_bits` is threaded in rather than recomputed because card mode's caller already has it -- the
/// projection is the expensive half.
fn compose_total_for_mode(
    pbits: &[u64],
    mode: Mode,
    indexes: &Archived<CardIndexes>,
    printings: &[APrinting],
    card_bits: Option<&[u64]>,
) -> usize {
    let popcount = |bits: &[u64]| bits.iter().map(|w| w.count_ones() as usize).sum::<usize>();
    match mode {
        Mode::Printing => popcount(pbits),
        Mode::Card => popcount(card_bits.expect("card mode must supply its card-space projection")),
        Mode::Artwork => {
            // Read straight off the archive. This used to prefix-sum `artwork_groups` on every artwork
            // query -- an O(n_cards) pass for a value that cannot change after load.
            let base = &indexes.artwork_base;
            let n_artworks = u32::from(*base.last().expect("artwork_base has n_cards+1 entries")) as usize;
            popcount(&printing_bits_to_artwork_bits(pbits, printings, &indexes.printing_to_card, base, n_artworks))
        }
    }
}

/// `unique=printing` fast path for a composable printing-space expression (bare `border:`/`r:` or an
/// `AND`/`OR`/`NOT` of them, #724) — the plane analogue of `printing_range_fastpath`: `total` is the
/// composed bitmap's `popcount` (no count pass), and the page is the same `walk_printing_page` (its
/// residual test evaluates the full composed filter, and early-stops). Returns `None` (declines) for
/// a non-composable filter, or a total at/below the stream threshold — where the general path gathers
/// and globally sorts, ordering ties differently (same guard as `printing_range_fastpath`; a sparse
/// value like `border:yellow` falls through here).
fn printing_compose_fastpath<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    // Ends at whichever exit produces a page; see `ComposePageWork::ns_total`. Declines return before
    // any publish and so leave the slot as `take_phase_stats` cleared it.
    let t_start = std::time::Instant::now();
    let QueryCtx { cards, printings, offsets, indexes, .. } = *ctx;
    let QueryParams { mode, sort_col, descending, page_offset, .. } = *params;
    if !is_printing_composable(filter, indexes) || !printing_compose_indexes_built(indexes) {
        note_paging_taken(PagingTaken::NotComposable);
        return None;
    }
    let perm = indexes.sort_perms.get(sort_col, descending).filter(|p| p.len() == cards.len());
    // #744: a printing-mode page ordered by a permutation-less orderby with a printing-space
    // value structure (`usd`/`rarity`) is walked directly (below), terminating at page_offset+limit
    // — cost O((offset+limit)/selectivity), the *opposite* shape from `gather_composed_page`. The
    // `COMPOSE_GATHER_MAX_CARD_FRACTION` gate must NOT apply to that branch: its premise ("broad ⇒ not
    // worth composing") is backwards here, where a broad predicate is the walk's *best* case.
    // The permutation-free gather's two decline gates, asked through the shared helper so that
    // `acquire_plan_features` can ask the SAME question when it costs this plan. See
    // `compose_gather_declines`.
    let gather_declines =
        perm.is_none().then(|| compose_gather_declines(filter, indexes, offsets, printings, cards, mode)).flatten();
    // No longer printing-mode only. Card and artwork rows are GROUPS, which the walk serves by emitting
    // at each group's representative (see `walk_value_orderby_page`), and card mode is where that
    // matters most: `unique` is 75% card against 5% artwork in realistic traffic, and card mode on a
    // walkless orderby measured 451 us against printing mode's 25 us on the same query.
    //
    // Two stages, because the decision needs the exact total and the decline gate comes before the
    // compose that produces it. This one is PERMISSIVE: skip the gather's gates whenever a walk might
    // run at all, since their premise ("broad => not worth composing") is backwards for the walk. The
    // real choice is `walk_col` below, and the gather's gates are honoured again if it says gather.
    let walk_possible = perm.is_none() && orderby_walk_available(sort_col);
    if !walk_possible && let Some(reason) = gather_declines {
        note_paging_taken(reason);
        return None;
    }
    // Compose once, here (never in acquire) — that single build is the only synthesis, and it is paid
    // only because this plan won. The total is the popcount in the query's result space; the page is
    // the one grouped walk at the mode's granularity, using the composed bits as exact membership.
    let pbits = compose_printing_bits(filter, indexes, offsets, printings, printings.len());
    // Card mode's total *is* the popcount of the card-space projection, and the permutation-free
    // `gather_composed_page` below derives its candidate cards from that same projection over
    // those same bits. Built once here and threaded through, instead of twice.
    let card_bits = matches!(mode, Mode::Card).then(|| printing_bits_to_card_bits(&pbits, offsets, cards.len()));
    let total = compose_total_for_mode(&pbits, mode, indexes, printings, card_bits.as_deref());
    // Now the exact total exists, so make the real walk-vs-gather call on it. See
    // `orderby_walk_beats_gather` for why this is the total rather than the gather's own broad verdict.
    let walk_col = walk_possible && orderby_walk_beats_gather(mode, filter, total);
    if total == 0 || page_offset >= total {
        note_paging_taken(PagingTaken::EmptyPage);
        publish_compose_work(ComposePageWork { ns_total: t_start.elapsed().as_nanos() as u64, ..Default::default() });
        return Some((total, Vec::new()));
    }
    let (page, mut work) = match perm {
        Some(perm) => {
            if total <= *STREAM_MIN_MATCHES {
                // Sparse: hand the query back to the general path rather than paging it here.
                //
                // NOT for the reason this comment used to give ("the general path gathers + globally
                // sorts, ordering ties differently"). That died with #815, which made row order total
                // and filter-independent on (key1, key2, cid, pid), replacing the key-3 `prefer_score`
                // that genuinely differed between the permutation (first STORED printing's score) and
                // the gathered paths (first MATCHING one). Measured 2026-08-04 by toggling this decline
                // off: 576 real invocations of the walk over 1,512 tie-heavy cells, 0 row differences,
                // plus 14 inside `force_plan_differential_agreement`, which asserts full row order
                // against GatheredScan. Either executor is correct here now.
                //
                // What keeps the decline is cost, not correctness. `gather_composed_page` is genuinely
                // 1.3-2.7x faster than the plan that otherwise wins on this population, but `plan_cost`
                // reads 0.27-0.53 of its real time, and the resulting mispicks cancel the wins exactly:
                // regret 1.30 -> 1.51 us, compose miss% 7% -> 18%, compose-acquire wall time 1.00 over
                // 2,341 paired queries. Neutral in time and worse in routing is not a trade worth the
                // complexity. docs/issues/local-engine-sparse-compose-gather.md carries the four
                // acceptance criteria this has to clear.
                //
                // `Exact` to distinguish it from `DeclineSparseEstimate` above: same intent, but that
                // one fires pre-compose off the estimator's upper bound, this one post-compose off
                // the real total. A harness reading one label for both cannot tell which fired.
                note_paging_taken(PagingTaken::DeclineSparseExact);
                return None;
            }
            note_paging_taken(PagingTaken::Perm);
            walk_grouped_page(ctx, params, &pbits, perm)
        }
        // No permutation. #744: if the orderby has a printing-space value structure (usd/rarity,
        // printing mode), walk it directly — terminating at page_offset+limit rather than visiting
        // every candidate. Falls back to the gather below when the walk declines (the null-value tail,
        // or a page past the value structure). Otherwise (card/artwork, or any other orderby), the
        // gather fallback pages via the bounded GatherSelect, whose tie-break matches the general path
        // exactly (same GatherSelect, same comparator) — so no separate small-total decline is needed.
        None => {
            // One walk over one layout; the sort column only picks which value index it reads.
            let walked = match sort_col {
                SortCol::PriceUsd if walk_col => Some(&indexes.price_usd),
                SortCol::Rarity if walk_col => Some(&indexes.rarity_printing_ordered),
                _ => None,
            }
            .and_then(|idx| walk_value_orderby_page(ctx, params, idx, &pbits, total));
            match walked {
                Some(rows_and_work) => {
                    note_paging_taken(PagingTaken::OrderbyWalk);
                    rows_and_work
                }
                None => {
                    // Two different situations reach this gather, and `compose_paging` predicts
                    // them differently, so they cannot share a label. `walk_col` false means no
                    // walk was ever available and `Gather` was predicted — agreement. `walk_col`
                    // true means a walk was available, was attempted, and declined (null-value
                    // tail, or a page past the value structure), so `OrderbyWalk` was predicted
                    // and this gather is the documented fallback — NOT a mispredicted branch.
                    // The breadth gate was skipped above BECAUSE a walk was available, so a walk that
                    // then declines must not silently land on the gather that gate guards -- for a broad
                    // card-mode filter that gather is the branch measured to LOSE to `GatheredScan`,
                    // which is exactly what the gate exists to say. Honour it now instead. Already
                    // computed above, so this costs nothing.
                    if let Some(reason) = gather_declines {
                        note_paging_taken(reason);
                        return None;
                    }
                    note_paging_taken(if walk_col { PagingTaken::GatherWalkDeclined } else { PagingTaken::Gather });
                    gather_composed_page(ctx, params, &pbits, card_bits.as_deref())
                }
            }
        }
    };
    work.ns_total = t_start.elapsed().as_nanos() as u64;
    publish_compose_work(work);
    Some((total, page))
}

/// The physical plans the cost router (`run_query_routed`) chooses among. Each
/// carries three declared properties — `applicable` (its correctness precondition),
/// `cost::plan_cost` (its predicted runtime), and an executor — so the router is a
/// generic argmin over `ALL.filter(applicable)`, not a hand-written decision tree.
/// Adding a plan is: a variant here, an `applicable` arm, a `plan_cost` arm, and an
/// executor arm in the router's dispatch. `run_query_with_plan` also makes each
/// individually forceable (the differential/calibration test seam).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PhysicalPlan {
    /// #695 bare-broad-range fast path under `unique=printing` (executor:
    /// `printing_range_fastpath`). Non-materializing.
    PrintingRangeScan,
    /// #724 unified compose plan: a composable printing-space expr (border/rarity/legality, AND/OR)
    /// under **any** distinct-on. Composes once in printing space, projects to the query's result space
    /// (printing = none, card / artwork = existence bitmap), and pages with the one grouped walk
    /// (`printing_compose_fastpath` → `walk_grouped_page`). Non-materializing (composes in the fast
    /// path, only if it wins). Deep-offset popcount-skip is deferred ([#730]).
    PrintingCompose,
    /// #634 Step 2 plane-bitmap popcount-skip order phase (`run_query_streamed_popcount`).
    PlanePopcountOrder,
    /// PR 2a card-space idea 2: a bare `usd` range under `unique=card`, answered as the same
    /// popcount-skip order phase over the range's card-existence bitmap (`exec_card_range_popcount`).
    CardRangePopcount,
    /// Streamed selection over the sort permutation (`run_query_streamed`).
    StreamedSelect,
    /// The universal fallback: the gathered per-card loop + `select_page`.
    GatheredScan,
}

/// Which paging strategy `PrintingCompose` (`printing_compose_fastpath`) runs for a query — a 3-way
/// distinction the cost model (`cost::plan_cost`'s `PrintingCompose` arm) needs because the three have
/// different cost shapes. `run_query_routed` picks the same variant the fastpath will.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum ComposePaging {
    /// A card-space sort permutation exists → the forward grouped walk (`walk_grouped_page`).
    /// Offset-dependent: fills the page in ~`page_span/selectivity` steps.
    Perm,
    /// No permutation, but the orderby has a printing-space value structure and mode is Printing
    /// (`usd`/`rarity`, #744) → the direct orderby walk (`walk_range_orderby_page`/
    /// `walk_rarity_orderby_page`). Offset-dependent, same shape as `Perm`; the COMPOSE_GATHER breadth
    /// gate is bypassed for it (broad is its best case).
    OrderbyWalk,
    /// No permutation and no orderby walk → the permutation-free bounded gather
    /// (`gather_composed_page`), which visits every match (offset-independent cost).
    Gather,
    /// The fastpath will refuse this query — `compose_gather_declines`, or the `Perm` branch's
    /// small-total bail. Costs infinity, which keeps the plan out of the argmin entirely: routing to
    /// a plan that returns `None` pays the detour and then runs something else anyway.
    Decline,
}

impl ComposePaging {
    /// The string `explain` reports under `"compose_paging"`. Byte-identical to what `Debug`
    /// produced before this existed, so the Python surface did not move.
    ///
    /// Spelled out rather than left as `format!("{:?}", ..)` for the same reason `PagingTaken` and
    /// `CountSource` are, and with a sharper edge than either: a consumer compares this label
    /// against `PagingTaken`'s. `scripts/bench_cost_model_agreement.py` counts agreement as
    /// `cells[(predicted, taken)]` with `predicted` from here and `taken` from
    /// `PagingTaken::label()`, so the two must SPELL the three strategies the same way. While this
    /// side was `Debug`-derived, renaming a variant here would have silently emptied that diagonal —
    /// every agreed run reclassified as a disagreement — and `compose_paging_prediction_matches_the_branch_taken`
    /// would not have noticed, because it compares enums rather than strings.
    ///
    /// `compose_paging_and_paging_taken_agree_on_strategy_names` is the test that pins the pairing.
    fn label(self) -> &'static str {
        match self {
            ComposePaging::Perm => "Perm",
            ComposePaging::OrderbyWalk => "OrderbyWalk",
            ComposePaging::Gather => "Gather",
            // No single `PagingTaken` counterpart on purpose: the executor distinguishes WHY it
            // refused (`DeclineBroad` / `DeclineSparseEstimate` / `DeclineSparseExact`) while the
            // model only predicts THAT it will. A comparison scoring these against each other has to
            // treat any `Decline*` as matching this one.
            ComposePaging::Decline => "Decline",
        }
    }
}

impl PhysicalPlan {
    /// All plans, argmin-ordered so ties resolve deterministically toward the
    /// cheaper-fixed-cost plan. The router filters this by `applicable`.
    const ALL: [PhysicalPlan; 6] = [
        PhysicalPlan::PrintingRangeScan,
        PhysicalPlan::PrintingCompose,
        PhysicalPlan::PlanePopcountOrder,
        PhysicalPlan::CardRangePopcount,
        PhysicalPlan::StreamedSelect,
        PhysicalPlan::GatheredScan,
    ];

    /// Whether this plan can *correctly* answer the query — its precondition, not a
    /// perf judgment (that is `cost::plan_cost`). These predicates also encode prep
    /// availability: `PlanePopcountOrder` is applicable exactly when the plane-bitmap
    /// prep is available, `PrintingRangeScan` exactly when the range-estimate prep is,
    /// so filtering `ALL` by `applicable` inside each prep branch yields the right
    /// candidate set with no per-branch plan list.
    fn applicable(
        self,
        ctx: &QueryCtx,
        params: &QueryParams,
        filter: &FilterExpr,
        unsplit: Option<&FilterExpr>,
        plane: Option<&PlaneExpr>,
    ) -> bool {
        let QueryCtx { cards, indexes, .. } = *ctx;
        let QueryParams { mode, sort_col, descending, .. } = *params;
        match self {
            PhysicalPlan::PrintingRangeScan => {
                printing_range_scan_applicable(mode, plane, cards) && bare_range_bounds(filter, indexes).is_some()
            }
            PhysicalPlan::PrintingCompose => printing_compose_applicable(filter, unsplit, cards, plane, indexes),
            PhysicalPlan::PlanePopcountOrder => {
                plane_popcount_order_applicable(filter, mode, cards, plane, sort_col, descending, indexes)
            }
            PhysicalPlan::CardRangePopcount => {
                card_range_popcount_applicable(filter, mode, cards, plane, sort_col, descending, indexes)
            }
            PhysicalPlan::StreamedSelect => streamed_select_applicable(cards, sort_col, descending, indexes),
            PhysicalPlan::GatheredScan => gathered_scan_applicable(),
        }
    }

}

/// The two plans that run off a materialized candidate list (P3/P4) — `exec_from_candidates`'s
/// whole repertoire, as a type.
///
/// Narrower than `PhysicalPlan` on purpose. The executor's `match` used to be over all six plans
/// ending in `unreachable!`, which made "the router only ever hands me one of these two" an
/// undocumented invariant of `run_query_routed`'s argmin rather than something either side
/// enforced — and the argmin did not enforce it (see `PlanScope`). Over this type the executor's
/// match is exhaustive, so adding a plan cannot silently re-open that hole: `of` is the one place
/// that decides whether a candidate list can run it, and it is exhaustive over `PhysicalPlan`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CandidatePlan {
    StreamedSelect,
    GatheredScan,
}

impl CandidatePlan {
    /// The candidate-list executor for `plan`, if it has one. Exhaustive over `PhysicalPlan` rather
    /// than a `_` arm: a new plan variant must state here whether a candidate list can run it.
    fn of(plan: PhysicalPlan) -> Option<Self> {
        match plan {
            PhysicalPlan::StreamedSelect => Some(CandidatePlan::StreamedSelect),
            PhysicalPlan::GatheredScan => Some(CandidatePlan::GatheredScan),
            PhysicalPlan::PrintingRangeScan
            | PhysicalPlan::PrintingCompose
            | PhysicalPlan::PlanePopcountOrder
            | PhysicalPlan::CardRangePopcount => None,
        }
    }

    /// `of`, with `GatheredScan` — which answers every query correctly — where `of` says the plan
    /// has no candidate-list executor.
    ///
    /// `PlanScope` is what makes the fallback unreachable: every caller restricts its argmin to a
    /// scope this conversion is total over. It exists anyway, in place of the `unreachable!` that was
    /// here, because the right failure mode for a router/executor disagreement is "runs a correct
    /// plan, possibly not the cheapest" and not "panics".
    ///
    /// Not because a panic would escape — the same change taught `_search` to catch `BaseException`,
    /// so one here is caught and the request degrades to SQL. That is what made the `unreachable!`
    /// user-visible before (a `PanicException` derives from `BaseException`, so the fallback missed it
    /// and the request 500ed), and it is closed on the Python side independently of this.
    ///
    /// The reason not to panic is that the fallback is a last resort rather than a licence: reaching
    /// it throws away a correct engine plan and pays a Postgres round trip because the router
    /// disagreed with its own executor — a latency cliff, where running the plan that was sitting
    /// right there costs nothing. The `debug_assert` keeps that from being a silent papering-over: a
    /// debug build (which is what CI's rust-test job runs) still fails loudly.
    fn of_or_gathered(plan: PhysicalPlan) -> Self {
        CandidatePlan::of(plan).unwrap_or_else(|| {
            debug_assert!(false, "no candidate-list executor for {plan:?} — the argmin's PlanScope did not restrict it");
            CandidatePlan::GatheredScan
        })
    }
}

/// The shared P3/P4 preparation product (see `prepare_candidates`): the
/// materialized candidate card list (or `None` = scan the whole store) and the
/// #634-Step-1 exactness bit. `existential_plane` is *not* bundled — it is
/// recomputed cheaply per executor from `(mode, plane, indexes)` via
/// `existential_plane_for`, keeping this struct free of borrows.
struct PreparedCandidates {
    candidate_cards: Option<Vec<u32>>,
    all_match_known: bool,
    /// `Candidates::repr` of what the narrowing returned, before this struct flattened
    /// it to `candidate_cards`. Diagnostic only (`explain`) — nothing in execution reads it,
    /// and it exists because a candidate *count* in some band does not imply the query paid
    /// a sort to get there: a plane AND'd with a range reaches the same count word-wise.
    narrowed_repr: NarrowedRepr,
    /// Top-level `And` children `card_pass` may skip because the candidate set proves them — see
    /// `Narrowed::proven`. Already validated against `candidate_cards` and permuted to match the
    /// post-reorder child positions, so consumers can use it directly.
    proven_conjuncts: u64,
}

impl PreparedCandidates {
    /// The card ids to visit: the narrowed list if one was materialized, else
    /// every card. Boxed because the two arms are different iterator types and
    /// both P3 and P4 want the same either-or — previously spelled out
    /// identically at the head of each.
    ///
    /// `ExactSizeIterator` rather than `Iterator`: both arms know their length (a `Vec` and a `Range`),
    /// so an executor that wants the candidate count before its loop can ask for it instead of being
    /// handed the count alongside the iterator and trusting the two to agree.
    fn card_ids<'s>(&'s self, ctx: &QueryCtx) -> Box<dyn ExactSizeIterator<Item = u32> + 's> {
        match &self.candidate_cards {
            Some(v) => Box::new(v.iter().copied()),
            None => Box::new(0..ctx.n_cards()),
        }
    }
}

/// How `run_query_routed` obtained a query's cost features, and the artifact (if
/// any) the chosen executor reuses. One of three "count sources", picked by query
/// structure — this is the engine's whole materialization story in one enum.
enum Prep {
    /// "Cheap estimate acquired, nothing materialized." Despite the name this is **not**
    /// range-index-specific: it is shared by `CardRangePopcount` (#725), `PrintingRangeScan`
    /// (#695) and `PrintingCompose` (#724), and a plane-composable printing-space query like
    /// `type:merfolk` at `unique=printing` lands here via compose with no range in sight. The
    /// payload names which acquire actually ran, so `explain` can report it instead of the
    /// variant name — reporting "range" for a compose sent one investigation down a wrong path.
    ///
    /// Nothing is materialized — the fast paths walk; a materializing winner materializes for
    /// itself in dispatch. `Plane` carries no bitmap here because `run_query_routed` owns it
    /// locally (`plane_bits: Vec<u64>`), passed by reference into dispatch.
    Range(CountSource),
    /// True-residual plane (card): the exact match bitmap, owned by
    /// `run_query_routed`'s local `plane_bits` and passed by reference.
    /// `PlanePopcountOrder` reads it directly; P3/P4 read it as a candidate list.
    Plane,
    /// The general residual path: a materialized candidate list.
    Candidates(PreparedCandidates),
}

/// Which plans `run_query_routed`'s argmin may return — the set the caller's dispatch arm actually
/// has an executor for.
///
/// The router is "argmin over `ALL.filter(applicable)`, then dispatch on `(plan, &prep)`", and
/// `applicable` is a *correctness* predicate about the query, not a statement about which artifact
/// the acquire step materialized. Those are different questions, and only `Prep::Range` answers
/// both the same way (its arm can run every plan: the fast paths walk, a materializing winner
/// materializes lazily). The other two acquires hold exactly one artifact and can run exactly the
/// plans that read it — so without this the argmin can hand them a plan their arm has no executor
/// for, which `exec_from_candidates` met with `unreachable!`.
///
/// What kept that from firing was a coincidence in somebody else's predicate, not anything the
/// router did: all three printing-space fast paths require `plane.is_none()` and
/// `PlanePopcountOrder` requires `plane.is_some()`, so a plane acquire's applicable set happens to
/// land inside its scope. Those guards are about how a predicate is REPRESENTED — a bare border
/// under `unique=card` folds into a plane, so compose declines it — and not about what a dispatch
/// arm can execute. Work in flight to let compose cost the unsplit filter alongside a plane removes
/// the coincidence while leaving the router's reasoning exactly as it was, which is the case for
/// stating the constraint where it belongs instead of relying on the overlap.
///
/// Restricting the argmin rather than teaching the arms to run more plans is also the right
/// performance answer: the acquire already paid for its artifact, and a plan outside the scope
/// would throw that work away and redo it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PlanScope {
    /// Every applicable plan — `Prep::Range`, whose dispatch arm covers all six.
    All,
    /// The candidate-list executors only (`CandidatePlan`): `Prep::Candidates`, and `Prep::Range`'s
    /// lazy-materialize fallback once it has a candidate list in hand.
    ///
    /// Not `PlanePopcountOrder`, even though that plan reads a materialized artifact too — the
    /// artifact it reads (the plane bitmap) is exactly what these two paths did *not* build. This
    /// is the distinction the old `materializing()` flag, which grouped it with P3/P4, did not draw.
    Candidates,
    /// `Candidates` plus `PlanePopcountOrder`: `Prep::Plane`, which holds the plane bitmap that
    /// plan walks, and which P3/P4 can equally read as their candidate list.
    Plane,
}

impl PlanScope {
    /// Whether the argmin may return `plan` in this scope.
    fn admits(self, plan: PhysicalPlan) -> bool {
        match self {
            PlanScope::All => true,
            PlanScope::Candidates => CandidatePlan::of(plan).is_some(),
            PlanScope::Plane => CandidatePlan::of(plan).is_some() || matches!(plan, PhysicalPlan::PlanePopcountOrder),
        }
    }
}

impl Prep {
    /// The plans this acquire's dispatch arm can execute. Paired with the `match (plan, &prep)` in
    /// `run_query_routed`: every arm there is reachable only for plans this admits.
    fn scope(&self) -> PlanScope {
        match self {
            Prep::Range(_) => PlanScope::All,
            Prep::Plane => PlanScope::Plane,
            Prep::Candidates(_) => PlanScope::Candidates,
        }
    }
}

/// Row selection (docs/issues/00667-engine-legality-divergent-carveout.md "Row
/// selection for unique=card"): only Mode::Card can have folded a legality leaf
/// into `plane` at all (unique_is_card declines the fold otherwise), and only
/// then does all_match's "the card matches" stop implying "any printing will
/// do" for picking which one to show. Cheap to recompute, so the executors do
/// so rather than threading it through `PreparedCandidates`.
fn existential_plane_for<'a>(
    mode: Mode,
    plane: Option<&'a PlaneExpr>,
    indexes: &'a Archived<CardIndexes>,
) -> Option<(&'a PlaneExpr, &'a Archived<BitPlanes>)> {
    match (mode, plane) {
        (Mode::Card, Some(pe)) if plane_expr_is_existential(pe, u64::from(indexes.planes.divergent_formats)) => {
            Some((pe, &indexes.planes))
        }
        _ => None,
    }
}

// ─── Applicability predicates ───────────────────────────────────────────────
// Each captures a plan's *correctness* preconditions (not its perf gate). These
// are the future `choose_plan` eligibility gates — real, named, reusable.

/// `GatheredScan` can execute any query. Trivially true, and called like every other
/// predicate here — from `PhysicalPlan::applicable`.
fn gathered_scan_applicable() -> bool {
    true
}

/// `StreamedSelect` needs a precomputed sort permutation for `(sort_col,
/// descending)` whose length matches the card count, over a non-empty store.
/// `maybe_broad` is deliberately excluded — that is a routing/perf choice, not a
/// correctness constraint; StreamedSelect returns correct rows at any breadth.
///
/// The INVERSE is required on the same terms, because the emission walk bounds itself to the match
/// span through it — hence `order`, which yields the pair or nothing.
fn streamed_select_applicable(
    cards: &[AOracleCard],
    sort_col: SortCol,
    descending: bool,
    indexes: &Archived<CardIndexes>,
) -> bool {
    !cards.is_empty() && indexes.sort_perms.order(sort_col, descending, cards.len()).is_some()
}

/// `PlanePopcountOrder` needs the filter fully consumed to `True`, `Mode::Card`,
/// a plane component, and both the forward (length-matched) and inverse sort
/// permutations. Mirrors the #634 Step 2 branch's guard exactly.
fn plane_popcount_order_applicable(
    filter: &FilterExpr,
    mode: Mode,
    cards: &[AOracleCard],
    plane: Option<&PlaneExpr>,
    sort_col: SortCol,
    descending: bool,
    indexes: &Archived<CardIndexes>,
) -> bool {
    matches!(filter, FilterExpr::True)
        && matches!(mode, Mode::Card)
        && !cards.is_empty()
        && plane.is_some()
        && indexes.sort_perms.order(sort_col, descending, cards.len()).is_some()
}

/// `PrintingRangeScan` structural eligibility only — whether it actually runs is
/// decided by `printing_range_fastpath` returning `Some` (it declines with
/// `None` for anything it doesn't own).
fn printing_range_scan_applicable(mode: Mode, plane: Option<&PlaneExpr>, cards: &[AOracleCard]) -> bool {
    *PRINTING_RANGE_FASTPATH != 0 && matches!(mode, Mode::Printing) && plane.is_none() && !cards.is_empty()
}

/// `PrintingCompose` applicability (#724), all three distinct-ons: a composable printing-space `filter`
/// (border/rarity/legality, `AND`/`OR`), the planes built, no folded plane, flag on. `plane.is_none()`
/// is load-bearing: under `unique=card` a *bare* border/rarity is `compile_plane`-consumed into an
/// existential plane (`plane.is_some()`) → the faster #634 card-plane path handles it, so this plan
/// declines there; under `unique=printing`/`artwork` nothing folds to a plane, so it picks up bare
/// leaves too. **No longer requires a sort permutation** (dropped `sort_col`/`descending` params to
/// match): `printing_compose_fastpath` pages via the forward grouped walk (`walk_grouped_page`) when
/// one exists for the query's `sort_col`, or via a permutation-free bounded-gather fallback
/// (`gather_composed_page`) when it doesn't (`rarity`/`usd` —
/// docs/issues/local-engine-compose-permutation-fallback.md) — either way the total (a popcount over
/// the already-exact composed bits) and the page are both correct; only *which* paging strategy runs
/// depends on the permutation's presence, decided inside the fastpath itself.
///
/// **`plane.is_none()` is about REPRESENTATION, not about routing**, and `unsplit` is what lifts it.
/// Compose builds its printing bitmap from the filter TREE; a plane sitting alongside holds predicate
/// this function cannot see, so composing the residual alone would silently drop it — wrong rows, not
/// slow rows. That is why the guard exists. But `split_planes` runs at bind time, before any plan is
/// costed, so consuming a leaf into a plane ELIMINATED compose before the argmin could weigh it: measured
/// 1.83 us for compose against StreamedSelect's 99.38 on `f:commander`/printing, a plan the router never
/// got to consider. `unsplit` carries the filter as bound, so compose can be costed on the whole
/// predicate whether or not a plane took part of it, and `cost::plan_cost` decides as #702 intends.
fn printing_compose_applicable(
    filter: &FilterExpr,
    unsplit: Option<&FilterExpr>,
    cards: &[AOracleCard],
    plane: Option<&PlaneExpr>,
    indexes: &Archived<CardIndexes>,
) -> bool {
    // Mode-agnostic: the cost model arbitrates overlap with the specialized range plans. All three
    // range/compose plans are non-materializing (estimate-in-acquire), so nothing is eagerly built —
    // a losing plan costs only a binary search, never a wasted scatter.
    //
    // With a plane present, the whole predicate is `unsplit` and compose must compose that; without one,
    // the residual IS the whole predicate. A caller that supplies no `unsplit` keeps the old behaviour.
    let whole = match (plane, unsplit) {
        (None, _) => Some(filter),
        (Some(_), Some(u)) => Some(u),
        (Some(_), None) => None,
    };
    *PRINTING_COMPOSE != 0
        && !cards.is_empty()
        && whole.is_some_and(|f| is_printing_composable(f, indexes))
        && printing_compose_indexes_built(indexes)
}

/// The predicate `PrintingCompose` composes for this query: the whole thing, wherever it lives. See
/// `printing_compose_applicable` for why the two representations both have to be available.
fn compose_source<'f>(filter: &'f FilterExpr, unsplit: Option<&'f FilterExpr>, plane: Option<&PlaneExpr>) -> &'f FilterExpr {
    match (plane, unsplit) {
        (Some(_), Some(u)) => u,
        _ => filter,
    }
}

/// `CardRangePopcount` needs `Mode::Card`, a **bare** range leaf as the whole filter — usd/cn/date,
/// whatever `bare_range_bounds` recognizes (no plane) — and both sort permutations. `plane.is_none()`
/// is deliberate and load-bearing, on both correctness and perf grounds:
/// - *correctness:* an existential legality plane (`usd<50 f:modern`) would make the card-existence
///   AND exact only when the attribute never diverges across a card's printings — a data coincidence
///   the engine refuses to bank on (docs/issues/00667-engine-legality-divergent-carveout.md); those
///   printing-varying compounds are the printing-space plane's job.
/// - *perf:* a card-invariant plane (`usd<50 c:g`) already narrows the query hard, so the existing
///   narrowed-verify path is fast, whereas this plan pays an O(k) build over the whole range slice
///   regardless — measured a net loss. So a narrowing plane means don't bother; only the bare range,
///   where the alternative is a full scan of a ~99%-broad set, is worth the build.
///
/// The bare-leaf shape also excludes range+range (`usd<50 cn<100` is an `And`, not a bare leaf):
/// composing two printing-varying ranges is a shared-witness case (`∃p: usd(p) ∧ cn(p)`) that must
/// AND in *printing* space and project once — the printing-space plane's structure, not this one's.
fn card_range_popcount_applicable(
    filter: &FilterExpr,
    mode: Mode,
    cards: &[AOracleCard],
    plane: Option<&PlaneExpr>,
    sort_col: SortCol,
    descending: bool,
    indexes: &Archived<CardIndexes>,
) -> bool {
    *RANGE_BITS_CARD != 0
        && matches!(mode, Mode::Card)
        && !cards.is_empty()
        && plane.is_none()
        && bare_range_bounds(filter, indexes).is_some()
        && indexes.sort_perms.order(sort_col, descending, cards.len()).is_some()
}

// ─── Shared P3/P4 candidate preparation ─────────────────────────────────────

/// Whether a plane-consumed predicate leaves the match kernels **nothing to verify per card** — i.e.
/// `card_pass` is redundant and both kernels take their `all_match` arm.
///
/// Legality planes (docs/issues/00667-engine-legality-divergent-carveout.md) are existence
/// projections ("*some* printing matches"), unlike every other plane (card-invariant fields, true or
/// false alike for every printing of a card). For `unique=card` that is exactly the semantics wanted.
/// But `Mode::Printing`/`Artwork` enumerate individual printings, and "the card has some legal
/// printing" does not mean "this printing is legal" — `card_pass` must still run per printing there
/// whenever the plane touched a *divergent* format, which is what `plane_expr_is_existential` tests
/// against the data-derived `divergent_formats` mask.
///
/// Extracted so the ROUTER can ask the same question the EXECUTOR does. `prepare_candidates` had the
/// only copy, and the compose acquire branch — which never calls it — therefore charged
/// `verify_cost_tier` on every legality-composed query alike. On a card-invariant format that put P3
/// at meas/pred 0.16-0.44 while `PrintingCompose` read 1.02-1.17, and the argmin lost those cells to a
/// plan measuring ~2.5x slower. A boolean, not an estimated share: the two attempts to price this as a
/// divergent *fraction* of the corpus under-charged `f:oldschool`, whose candidates largely ARE the
/// divergent cards, and traded 408 mispicks for 118 worse ones.
fn plane_leaves_nothing_to_verify(
    filter: &FilterExpr,
    mode: Mode,
    plane: Option<&PlaneExpr>,
    indexes: &Archived<CardIndexes>,
) -> bool {
    matches!(filter, FilterExpr::True)
        && plane.is_none_or(|expr| {
            matches!(mode, Mode::Card) || !plane_expr_is_existential(expr, u64::from(indexes.planes.divergent_formats))
        })
}

/// The candidate materialization + filter rewriting shared by `StreamedSelect`
/// and `GatheredScan`, extracted verbatim from `run_query`. Mutates `filter` via
/// `memoize_text_predicates` + `order_children_by_verify_cost` under the same
/// `!all_match_known` / `*VERIFY_ORDER` guards and in the same order as before.
fn prepare_candidates(ctx: &QueryCtx, params: &QueryParams, filter: &mut FilterExpr, plane: Option<&PlaneExpr>) -> PreparedCandidates {
    let QueryCtx { cards, offsets, strings, indexes, .. } = *ctx;
    let mode = params.mode;
    // Candidates in either space project to card ids for the walk; the walk's
    // per-printing verification restores exactness for printing-space losses.
    // A list covering nearly the whole corpus narrows nothing — the walk would
    // visit almost every card anyway, and the list costs its materialization.
    // Broad-range bitmaps (#636) can produce such lists; treating them as
    // unnarrowed also keeps the #635 memoization trigger firing for these
    // queries exactly as before. Left un-materialized (Candidates, not
    // Vec<u32>) here so the plane branch below can AND two card-space bitmaps
    // directly instead of paying to materialize one of them first.
    let (raw_candidates, residual_exact, proven_conjuncts): (Option<Candidates>, bool, u64) =
        narrow_candidates_exact(filter, indexes, offsets, cards);
    // Captured before the flattening below consumes it — see PreparedCandidates::narrowed_repr.
    let narrowed_repr = raw_candidates.as_ref().map_or(NarrowedRepr::None, Candidates::repr);
    // A present plane is always exact (that's what compile_plane guarantees),
    // so the whole original query is exact iff the residual is too — either
    // because split_planes consumed all of it (bare True) or narrow_rec
    // proved the remainder tight and complete with its membership in hand
    // (see narrow_candidates_exact). #634 Step 1: when this holds, every
    // candidate is already known to match, so the per-candidate card_pass
    // calls below and in run_query_streamed become redundant re-verification
    // of what the narrowing already established.
    //
    // Computed AFTER `candidate_cards`, because the `residual_exact` half is only sound while the set
    // it was derived from is the set the walk visits — see the `candidate_cards.is_some()` guard there.

    // The plane bitmap is the exact card-level truth of the plane-consumed
    // subexpression (split_planes), so it composes with the residual's
    // narrowed candidates by intersection — and with no candidates it IS the
    // candidate list. Either way every surviving card still runs the residual
    // through card_pass, which is what keeps printing-space losses and Null
    // semantics with the residual, not the planes. The bitmap buffer is
    // reused across queries (thread-local), same as the streamed counts
    // buffer.
    let candidate_cards: Option<Vec<u32>> = match plane {
        // The 7/8 breadth filter has the same inverted reasoning as `narrow_candidates_exact`'s 3/4 guard
        // (#860): a near-total LOOSE list costs materialization and still verifies every card, but a
        // near-total TIGHT one removes verification altogether, which is the larger term. `-name:q` is the
        // case -- its complement is 30,918 of 31,508 cards, so it was dropped here, `all_match_known` was
        // then correctly cleared, and the query fell back to a full scan with `card_pass` on every card.
        None => raw_candidates
            .map(|c| c.into_cards(offsets, &indexes.printing_to_card))
            .filter(|v| residual_exact || v.len() < cards.len() - cards.len() / 8),
        Some(expr) => {
            thread_local! {
                static PLANE_BITMAP: std::cell::RefCell<Vec<u64>> = const { std::cell::RefCell::new(Vec::new()) };
            }
            PLANE_BITMAP.with(|cell| {
                let mut bitmap = cell.borrow_mut();
                eval_planes(expr, &indexes.planes, &mut bitmap);
                match raw_candidates {
                    // Both sides already card-space bitmaps (e.g. #630 phase
                    // 2's legal-format masks, or the devotion superset arm):
                    // AND them directly, O(words) regardless of either side's
                    // popcount. Materializing the residual's ids first and
                    // retaining against the plane — the general path below —
                    // costs O(residual popcount), which is a poor trade when
                    // the residual is a broad mask (a legal-format narrowing
                    // is often 50-99% of the store) and the plane is tight.
                    Some(Candidates::CardBits(mut b)) => {
                        and_bits_into(&mut b, &bitmap);
                        Some(bitmap_card_ids(&b))
                    }
                    Some(c) => {
                        let mut v = c.into_cards(offsets, &indexes.printing_to_card);
                        v.retain(|&cid| bitmap_contains(&bitmap, cid));
                        Some(v)
                    }
                    None => Some(bitmap_card_ids(&bitmap)),
                }
            })
        }
    };

    // `residual_exact` says "every card in the narrowed set matches". That licenses skipping `card_pass`
    // only while the walk actually visits that set. `candidate_cards` can still come back None above —
    // the 7/8 breadth filter drops a set that is nearly the whole store — and then the walk falls back
    // to `0..n_cards` and would emit cards the narrowing never covered. Exactly the hazard the
    // `proven_conjuncts` block below already guards against, one flag over, and with a wider blast
    // radius: `proven_conjuncts` skips SOME conjuncts, this skips verification entirely.
    //
    // Latent rather than live before #860: `narrow_candidates_exact`'s own 3/4 guard is stricter than
    // the 7/8 filter at every corpus size, so nothing that survived the first could trip the second.
    // Relaxing the first for tight sets makes it reachable, and `fuzz_row_identity_matches_reference`
    // catches it on AND(cmc<8, colors!=0b00011) at seed 19 — 15 rows returned against 14 real matches.
    //
    // The `plane_leaves_nothing_to_verify` half needs no such guard: with a plane present
    // `candidate_cards` is always Some, and with no plane it can only hold for a filter that matches
    // everything, where scanning everything is the right answer.
    let all_match_known =
        plane_leaves_nothing_to_verify(filter, mode, plane, ctx.indexes) || (residual_exact && candidate_cards.is_some());

    // Resolve indexable text predicates through their indexes once (#624)
    // when the per-card evaluation they'd replace outweighs the bind cost —
    // the gate is per-node and cost-based (see memoize_pays): each predicate
    // compares its own bind bound against the evaluation domain, so a broad
    // candidate set with a selective needle memoizes while a narrow one
    // leaves the scan alone. Skipped entirely when all_match_known: card_pass
    // never runs, so there is nothing left for the rewrite to speed up.
    // The mask indexes `filter`'s top-level children, so it is only valid while BOTH still describe the
    // same query. Two things here can break that, and each clears it:
    //
    //   * `candidate_cards` being None. The narrowing was discarded (too broad, or absent), so the walk
    //     visits cards the proof never covered. Keeping the mask would skip a real predicate.
    //   * `order_children_by_verify_cost` permuting the children. The mask is carried THROUGH that
    //     permutation rather than cleared, because the reorder is exactly what makes the residual cheap
    //     and dropping the mask here would forfeit the win on every query that reaches it.
    let mut proven_conjuncts = if candidate_cards.is_some() && *PROVEN_CONJUNCTS { proven_conjuncts } else { 0 };
    if !all_match_known {
        let eval_domain = candidate_cards.as_ref().map_or(cards.len(), Vec::len);
        filter.memoize_text_predicates(cards, strings, &indexes.name_trigram, &indexes.name_bigrams, &indexes.oracle_trigram, eval_domain);
        // Sort And/Or children cheapest-verification-first so the walk's
        // short-circuit spares the expensive text predicates (semantics-preserving;
        // see order_children_by_verify_cost). After memoization, which flips
        // TextContains nodes from the scan tier to the set tier.
        if *VERIFY_ORDER != 0 {
            filter.order_children_by_verify_cost(&mut proven_conjuncts);
        }
    } else {
        // `card_pass` never runs, so there is nothing to skip and the mask has no consumer.
        proven_conjuncts = 0;
    }

    PreparedCandidates { candidate_cards, all_match_known, narrowed_repr, proven_conjuncts }
}

// ─── Plan executors ─────────────────────────────────────────────────────────

/// P2 executor: evaluate the plane into the popcount thread-local and run the
/// #634 Step 2 popcount-skip order phase. Caller guarantees applicability
/// (`plane_popcount_order_applicable`).
fn exec_plane_popcount_order<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    plane_expr: &PlaneExpr,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    thread_local! {
        static PLANE_BITMAP_POPCOUNT: std::cell::RefCell<Vec<u64>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    PLANE_BITMAP_POPCOUNT.with(|cell| {
        let mut bitmap = cell.borrow_mut();
        // Timed and published as `ns_prepare`, for the same reason `prepare_candidates` is: this is a
        // SHARED artifact that the router builds once during acquire and hands to whichever plan wins
        // (`Prep::Plane`), so a forced run that rebuilds it is paying something the routed path does
        // not pay at dispatch. Netting it is what makes the two comparable — see
        // `costbench.plan_self_ns`.
        //
        // Without this the plane rows read NEGATIVE regret: routed dispatch reuses `plane_bits` while
        // the forced trial re-evaluates the plane, so the router appeared to beat the best plan by a
        // mean of 4.21us and held -13% of share. That is the `prepare_candidates` asymmetry again, one
        // artifact along.
        let t = std::time::Instant::now();
        eval_planes(plane_expr, &ctx.indexes.planes, &mut bitmap);
        note_pending_prepare_ns(t.elapsed().as_nanos() as u64);
        exec_plane_popcount_order_with_bitmap(ctx, params, plane_expr, &bitmap)
    })
}

/// The popcount-skip order phase of P2 with the plane bitmap *already evaluated*
/// by the caller — the eval-owning split of `exec_plane_popcount_order`. The
/// #702-step-5 routed path (`run_query_routed`) evaluates the plane once
/// and reuses the same `&[u64]` here, so the plane is never evaluated twice on a
/// routed query. Caller guarantees `plane_popcount_order_applicable` (a
/// length-matched forward permutation and an inverse permutation both exist).
fn exec_plane_popcount_order_with_bitmap<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    plane_expr: &PlaneExpr,
    bitmap: &[u64],
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let indexes = ctx.indexes;
    let QueryParams { sort_col, descending, .. } = *params;
    let order = indexes
        .sort_perms
        .order(sort_col, descending, ctx.cards.len())
        .expect("PlanePopcountOrder applicability guarantees a sort order");
    // `run_query_streamed_popcount` publishes the phases and consumes the pending plane build; see
    // `publish_popcount_phases`. Counters stay zero: this plan popcounts a bitmap and visits no
    // cards, so it has nothing to report there.
    run_query_streamed_popcount(ctx, params, order, bitmap, Some(plane_expr), None)
}

/// `CardRangePopcount` executor: the same popcount-skip order phase as P2, but its match bitmap is a
/// range's card-existence projection (built in `acquire`) rather than a plane, and it threads the
/// range's printing-space membership set so emission shows an in-range printing. Caller guarantees
/// `card_range_popcount_applicable` (permutations exist; the shown-printing plane, if any, is
/// non-existential so it needs no per-printing re-check here).
fn exec_card_range_popcount<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    card_bits: &[u64],
    range_pbits: &[u64],
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let indexes = ctx.indexes;
    let QueryParams { sort_col, descending, .. } = *params;
    let order = indexes
        .sort_perms
        .order(sort_col, descending, ctx.cards.len())
        .expect("CardRangePopcount applicability guarantees a sort order");
    run_query_streamed_popcount(ctx, params, order, card_bits, None, Some(range_pbits))
}

/// Prefix-sum the per-card distinct-artwork counts (`artwork_groups`) into artwork-space offsets: a
/// card `c`'s distinct artworks are the contiguous global ids `[artwork_base[c], artwork_base[c+1])`,
/// so global artwork id = `artwork_base[c] + artwork_group_id`. `artwork_base.last()` is `n_artworks`.
/// Built once at load and archived as `CardIndexes::artwork_base`; `build_artwork_base_from` is the
/// load-time form over the counts before they are archived.
fn build_artwork_base_from(artwork_groups: &[u16]) -> Vec<u32> {
    let mut base = Vec::with_capacity(artwork_groups.len() + 1);
    let mut acc = 0u32;
    base.push(0);
    for c in artwork_groups {
        acc += u32::from(*c);
        base.push(acc);
    }
    base
}

/// Project composed printing bits up to **artwork** space: set the global artwork id
/// (`artwork_base[card] + artwork_group_id`) of every matching printing. `popcount` of the result is
/// the `unique=artwork` total — the distinct matching illustrations — replacing the O(candidates ×
/// printings) count pass the general path pays.
fn printing_bits_to_artwork_bits(
    pbits: &[u64],
    printings: &[APrinting],
    printing_to_card: &AOffsets,
    // Archived, not a slice: this is `CardIndexes::artwork_base` read directly off the store rather
    // than a Vec rebuilt per query.
    artwork_base: &AOffsets,
    n_artworks: usize,
) -> Vec<u64> {
    let mut abits = vec![0u64; n_artworks.div_ceil(64)];
    for (i, &word) in pbits.iter().enumerate() {
        let mut w = word;
        while w != 0 {
            let pid = (((i as u32) << 6) | w.trailing_zeros()) as usize;
            w &= w - 1;
            let card = u32::from(printing_to_card[pid]) as usize;
            let gid = u16::from(printings[pid].artwork_group_id) as usize;
            let aid = u32::from(artwork_base[card]) as usize + gid;
            abits[aid >> 6] |= 1u64 << (aid & 63);
        }
    }
    abits
}

/// The #724 unified compose page walk (all three distinct-ons): walk the sort permutation from the
/// front and, per card, collapse the matching (set) printings to the mode's **granularity** —
/// `Printing` emits every set printing, `Card` one best-`prefer_score` representative for the card,
/// `Artwork` one best-`prefer_score` representative per `artwork_group_id` (exactly the general path's
/// Artwork semantics). Membership is the exact composed `pbits`, so there is no residual re-evaluation.
/// The total is a separate `popcount` over the mode's result bitmap; this only builds the requested
/// page. Forward walk (no popcount-skip) — deep-offset skip is the deferred [#730] optimization.
// needless_range_loop: same shape as `gather_composed_page` — `pid` is both the membership
// probe and the emitted row identity.
#[allow(clippy::needless_range_loop)]
fn walk_grouped_page<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    pbits: &[u64],
    perm: &Archived<Vec<u32>>,
) -> (Vec<(&'a AOracleCard, &'a APrinting)>, ComposePageWork) {
    let QueryCtx { cards, printings, offsets, indexes, .. } = *ctx;
    let QueryParams { mode, prefer, sort_col, descending, limit, page_offset, .. } = *params;
    let max_artwork_groups = u16::from(indexes.max_artwork_groups);
    let is_set = |pid: usize| pbits[pid >> 6] & (1u64 << (pid & 63)) != 0;
    let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
    // per group key: (best matching pid, its prefer score). Pre-sized so the grouping loop needs no
    // per-printing resize check: Artwork needs one slot per group, Card collapses to a single group
    // (gid 0), Printing does no grouping. Card's fixed len 1 also keeps the loop safe if a
    // degenerate store leaves max_artwork_groups at 0.
    let n = match mode {
        Mode::Artwork => usize::from(max_artwork_groups),
        Mode::Card => 1,
        Mode::Printing => 0,
    };
    let mut group_best: Vec<Option<(u32, f64)>> = vec![None; n];
    let mut touched: Vec<u16> = Vec::new();
    let mut scratch: Vec<Match> = Vec::new();
    let mut skip = page_offset;
    // This walk pays per PERMUTATION ENTRY, not per match: it steps cards in sort order and bit-tests
    // each one's whole printing span, stopping only when the page fills. `cost::printings_walked`
    // models that as `page_span / match_rate`, which nothing checked before these counters.
    let mut work = ComposePageWork::default();
    for cid in perm.iter().map(|x| u32::from(*x)) {
        let card = &cards[cid as usize];
        let start = u32::from(offsets[cid as usize]) as usize;
        let end = u32::from(offsets[cid as usize + 1]) as usize;
        work.cards_visited += 1;
        work.printings_examined += (end - start) as u64;
        scratch.clear();
        match mode {
            // Printing: every set printing is its own row (no grouping).
            Mode::Printing => {
                for pid in start..end {
                    if is_set(pid) {
                        scratch.push((sort_key_bits(card, &printings[pid], sort_col, descending), cid, pid as u32));
                    }
                }
            }
            // Card / Artwork: one best-prefer representative per group — a single group for Card, one
            // per `artwork_group_id` for Artwork.
            Mode::Card | Mode::Artwork => {
                touched.clear();
                for pid in start..end {
                    if !is_set(pid) {
                        continue;
                    }
                    let gid = match mode {
                        Mode::Artwork => u16::from(printings[pid].artwork_group_id) as usize,
                        _ => 0, // Card: everything collapses into one group
                    };
                    debug_assert!(gid < group_best.len(), "group_best must be pre-sized to max_artwork_groups");
                    let score = prefer_score(card, &printings[pid], prefer);
                    match &group_best[gid] {
                        None => {
                            group_best[gid] = Some((pid as u32, score));
                            touched.push(gid as u16);
                        }
                        Some((_, best)) if score > *best => group_best[gid] = Some((pid as u32, score)),
                        _ => {}
                    }
                }
                for &gid in &touched {
                    let (bp, _) = group_best[gid as usize].take().unwrap(); // take: resets group_best for the next card
                    scratch.push((sort_key_bits(card, &printings[bp as usize], sort_col, descending), cid, bp));
                }
            }
        }
        work.matches_pushed += scratch.len() as u64;
        if scratch.is_empty() {
            continue;
        }
        if skip >= scratch.len() {
            skip -= scratch.len();
            continue;
        }
        scratch.sort_unstable_by(page_cmp);
        for m in scratch.iter().skip(skip) {
            page.push((&cards[m.1 as usize], &printings[m.2 as usize]));
            if page.len() == limit {
                return (page, work);
            }
        }
        skip = 0;
    }
    (page, work)
}

/// Permutation-free counterpart to `walk_grouped_page`, for when `orderby` has no card-space
/// permutation (`rarity`/`usd` — the representative printing depends on `prefer` and can't be
/// precomputed, see `ArchivedSortPermutations::get`'s doc). Same per-card grouping logic (`pbits`
/// membership, best-`prefer_score` representative per group) and same exactness (`pbits` already
/// consumed the whole filter — no residual, no `card_pass`), but instead of walking a permutation
/// front-to-back with an early stop, it visits only the candidate cards (`bitmap_card_ids` over the
/// card-projected `pbits` — cards with zero surviving printings are skipped entirely, an
/// optimization the rank-order permutation walk can't make) and pushes into the same bounded
/// `GatherSelect` accumulator `GatheredScan` uses for its own permutation-less case — so tie-break
/// order matches the general path exactly (same comparator, same struct), unlike the permutation
/// walk (which is why that one still declines below `STREAM_MIN_MATCHES`; this one doesn't need to).
#[allow(clippy::needless_range_loop)] // `pid` also drives `is_set`/`printings[pid]` together, same shape as `walk_grouped_page`
fn gather_composed_page<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    pbits: &[u64],
    card_bits: Option<&[u64]>,
) -> (Vec<(&'a AOracleCard, &'a APrinting)>, ComposePageWork) {
    let QueryCtx { cards, printings, offsets, indexes, .. } = *ctx;
    let QueryParams { mode, prefer, sort_col, descending, limit, page_offset, .. } = *params;
    let max_artwork_groups = u16::from(indexes.max_artwork_groups);
    let is_set = |pid: usize| pbits[pid >> 6] & (1u64 << (pid & 63)) != 0;
    // `card_bits` is `pbits` already projected into card space. `Mode::Card` computes that
    // projection for its `total` and hands it over rather than have this function repeat it
    // over the same bits; printing/artwork totals need no card projection, so those pass
    // `None` and it is built here as before.
    let projected: Vec<u64>;
    let card_bits = match card_bits {
        Some(bits) => bits,
        None => {
            projected = printing_bits_to_card_bits(pbits, offsets, cards.len());
            &projected
        }
    };
    let candidate_cards = bitmap_card_ids(card_bits);
    let n = match mode {
        Mode::Artwork => usize::from(max_artwork_groups),
        Mode::Card => 1,
        Mode::Printing => 0,
    };
    let mut group_best: Vec<Option<(u32, f64)>> = vec![None; n];
    let mut touched: Vec<u16> = Vec::new();
    let mut sel = GatherSelect::new(page_offset, limit);
    // `compose_scan_printings` is set to the composed bitmap's POPCOUNT, on the stated grounds that
    // compose "walks the set bits". This loop does not: except in the card/default-prefer arm it
    // iterates `start..end` of every candidate card and bit-tests each printing, so the real count is
    // the SPAN of the candidate cards.
    //
    // Only the span is accumulated. `cards_visited` is `candidate_cards.len()`, known before the loop
    // starts, and `matches_pushed` is the total `GatherSelect` already computes and this function
    // currently discards -- neither needs a per-iteration add.
    let mut work = ComposePageWork { cards_visited: candidate_cards.len() as u64, ..Default::default() };
    for cid in candidate_cards {
        let card = &cards[cid as usize];
        let start = u32::from(offsets[cid as usize]) as usize;
        let end = u32::from(offsets[cid as usize + 1]) as usize;
        let before = sel.buf().len();
        match mode {
            // Printing: every set printing is its own row (no grouping).
            Mode::Printing => {
                work.printings_examined += (end - start) as u64;
                for pid in start..end {
                    if is_set(pid) {
                        sel.buf().push((sort_key_bits(card, &printings[pid], sort_col, descending), cid, pid as u32));
                    }
                }
            }
            // Card, default prefer: printings are stored prefer-desc within a card (same invariant
            // `push_card_matches` relies on), so the first *set* printing in range is already the
            // chosen one — no score to compute, no `touched`/`group_best` bookkeeping, an O(1) early
            // break instead of scanning the rest of the card's printings. This matters here (unlike
            // `walk_grouped_page`, which pays the same unconditional score-every-candidate cost) since
            // this loop isn't bounded by page size — it visits every candidate card, so a per-printing
            // cost that scales with total matches rather than `limit` is the dominant term for a broad
            // composed set.
            Mode::Card if matches!(prefer, Prefer::Default) => {
                // The one arm that does stop early: `find` breaks at the first set printing, and a
                // candidate card has at least one by construction, so this tests `first_set - start + 1`
                // rather than the span.
                if let Some(pid) = (start..end).find(|&pid| is_set(pid)) {
                    work.printings_examined += (pid - start + 1) as u64;
                    sel.buf().push((sort_key_bits(card, &printings[pid], sort_col, descending), cid, pid as u32));
                } else {
                    work.printings_examined += (end - start) as u64;
                }
            }
            // Card (non-default prefer) / Artwork: one best-prefer representative per group.
            Mode::Card | Mode::Artwork => {
                work.printings_examined += (end - start) as u64;
                touched.clear();
                for pid in start..end {
                    if !is_set(pid) {
                        continue;
                    }
                    let gid = match mode {
                        Mode::Artwork => u16::from(printings[pid].artwork_group_id) as usize,
                        _ => 0, // Card: everything collapses into one group
                    };
                    debug_assert!(gid < group_best.len(), "group_best must be pre-sized to max_artwork_groups");
                    let score = prefer_score(card, &printings[pid], prefer);
                    match &group_best[gid] {
                        None => {
                            group_best[gid] = Some((pid as u32, score));
                            touched.push(gid as u16);
                        }
                        Some((_, best)) if score > *best => group_best[gid] = Some((pid as u32, score)),
                        _ => {}
                    }
                }
                for &gid in &touched {
                    let (bp, _) = group_best[gid as usize].take().unwrap(); // take: resets group_best for the next card
                    sel.buf().push((sort_key_bits(card, &printings[bp as usize], sort_col, descending), cid, bp));
                }
            }
        }
        sel.absorb(before);
    }
    // `total` is every match this branch pushed, which is what `matches_pushed` wants and what
    // `GatherSelect` has been computing and this function discarding. The popcount already gave the
    // caller the result total, hence the old `_total`.
    let (total, page_ids) = sel.finish(page_offset, limit);
    work.matches_pushed = total as u64;
    (page_ids.into_iter().map(|(cid, pid)| (&cards[cid as usize], &printings[pid as usize])).collect(), work)
}

/// P3 executor: streamed selection over the sort permutation. Caller guarantees
/// applicability (`streamed_select_applicable`) and has run `prepare_candidates`.
fn exec_streamed_select<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
    prep: &PreparedCandidates,
    plane: Option<&PlaneExpr>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let indexes = ctx.indexes;
    let perm = indexes
        .sort_perms
        .get(params.sort_col, params.descending)
        .expect("StreamedSelect applicability guarantees a permutation");
    // The walk's segment, decided here rather than inside the executor because it is a property of the
    // QUERY (its filter's interval on the sort column) and not of the emission loop. Two binary searches
    // when the filter constrains the sort column, the whole permutation otherwise.
    let walk = walk_bounds(perm, ctx.cards, params.sort_col, params.descending, params.sort_bound);
    let existential_plane = existential_plane_for(params.mode, plane, indexes);
    run_query_streamed(ctx, params, filter, prep.all_match_known, prep.proven_conjuncts, walk, prep.card_ids(ctx), existential_plane)
}

/// Per-query execution counters and coarse phase timings, for checking the cost model against what
/// the executors actually do rather than against a fitted curve.
///
/// Two distinct questions, and the model can fail either:
/// - are the FEATURE COUNTS right? `cards_visited` / `printing_span` / `matches_pushed` are the
///   real counts behind `eval_domain` / `scan_units` / `matches`. If a feature disagrees with its
///   counter, no rate constant can rescue the term.
/// - is the WORK WHERE THE MODEL SAYS? `ns_setup` + `ns_loop` + `ns_finish` should account for the
///   whole executor. Whatever is left over is work no term describes, and re-fitting cannot find it.
///
/// Counters are plain locals in the hot loop and only published here at the end, so the loop pays
/// nothing. The three phases are contiguous, so each boundary instant ends one and starts the next:
/// four `Instant::now()` calls bound three phases, which is the same four clock reads the two
/// phases cost before `ns_setup` was split out. Per query, not per card.
#[derive(Default, Clone, Copy)]
pub(crate) struct PhaseStats {
    pub(crate) cards_visited: u64,
    /// The printings under the candidate cards — what a full scan of them WOULD cost. A span,
    /// computed as `end - start` per surviving card, kept because it is the right comparison for
    /// `scan_units` in printing and artwork mode, where the loops really do traverse the span.
    ///
    /// It is the WRONG comparison in card mode, and reading it as though it were the work done is how
    /// `cost.rs` came to assert that "the scan plans walk the full printing span of their candidates
    /// in CARD mode too, not one row each". They do not: see `card_match_count` and
    /// `push_card_matches`, both of which stop at the first qualifying printing under the default
    /// prefer. Compare against `printings_examined` for what actually ran.
    pub(crate) printing_span: u64,
    /// The printings the per-card body actually ran on — the quantity `scan_units` claims to predict.
    /// Reported by the two match kernels, which are the only code that knows where they stopped.
    /// Legitimately 0 for a whole query: both `all_match` arms of `card_match_count` answer from the
    /// span arithmetic alone, and the stored artwork group count answers without a printing either.
    pub(crate) printings_examined: u64,
    pub(crate) matches_pushed: u64,
    /// Permutation entries `run_query_streamed`'s walk stepped over. Zero for every other executor and
    /// for P3's other two exits (the empty/past-the-end return and the small-total gather).
    ///
    /// The walk steps the permutation until the page fills, so its length is
    /// `page x n_cards / matches` -- inversely proportional to selectivity, and the one quantity in P3's
    /// finish phase that no existing feature is proportional to. Measured against a fixed 1,500 matches
    /// it runs 1,333 / 3,791 / 10,458 ns at 31.5k / 126k / 410k cards while the arm charged
    /// `matches x EMIT + FIXED` ~ 397 ns throughout: under by 3.4x at the production corpus and 26x at
    /// 410k. Published so the estimate can be GRADED rather than assumed, like the other three counters.
    pub(crate) perm_steps: u64,
    /// Per-query scratch setup, before the match loop starts. Split out because it is neither
    /// prepare nor match and it is NOT negligible: `run_query_streamed` zeroes an `n_cards`-long
    /// counts buffer here (~126 kB on the real corpus) no matter how few candidates it is about to
    /// visit, so on a selective query this can be most of the run. It used to fall outside every
    /// timer and land silently in the unaccounted remainder.
    ///
    /// It is also the one phase here with an obvious cost term waiting for it: the dominant part
    /// scales with `n_cards`, which `PlanFeatures` already carries.
    pub(crate) ns_setup: u64,
    pub(crate) ns_loop: u64,
    pub(crate) ns_finish: u64,
    /// Wall time of the whole `run_query_with_plan` round these phases came from. Recorded so the
    /// accounting compares like with like: `trials_ns` reports the MINIMUM across rounds, and
    /// dividing phases from one round by the minimum of another silently reads as unmodelled work.
    pub(crate) ns_round_total: u64,
    /// Wall time of `prepare_candidates` for the two materializing plans, measured here rather than
    /// inferred. No cost term describes it, and on range-acquired queries it is where a third of the
    /// runtime was landing unaccounted.
    pub(crate) ns_prepare: u64,
    /// The result total this run returned. `explain_analyze` had no ground truth at all before: a
    /// harness wanting the true cardinality made a SECOND `query()` call, which is a different
    /// execution, so agreement with the analyzed run was assumed rather than observed.
    pub(crate) result_total: u64,
    /// Which exit the printing-space fastpath that ran ACTUALLY took. For
    /// `printing_compose_fastpath` that is a paging branch, checkable against the `compose_paging`
    /// the cost model predicted; for `printing_range_fastpath` it is one of the `Range*` gates,
    /// which predict nothing and exist to size the declines. The rest of this note is about the
    /// compose half, which is the half with a prediction to falsify.
    ///
    /// `acquire_plan_features`' `PrintingCompose` branch sets `feats.compose_paging` by
    /// reimplementing a decision the fastpath makes independently -- it recomputes the permutation
    /// lookup and the `orderby_walk_available` test on its own -- and nothing checked that the two
    /// agree. That is the same shape as the Python cost mirror which silently drifted from `cost.rs`
    /// for two revisions. Reporting the real branch turns the assumption into something a harness
    /// can assert, which `compose_paging_prediction_matches_the_branch_taken` now does (and
    /// `scripts/bench_cost_model_agreement.py` observes over the real corpus).
    ///
    /// The two *availability* tests are identical by construction (`walk_col` against the
    /// prediction's `matches!(mode, Mode::Printing) && orderby_walk_available(sort_col)`), so they
    /// cannot disagree. What the prediction cannot see is one step further on: acquire never
    /// composes, so it cannot know whether the walk it predicts will SUCCEED. The walk returns an
    /// `Option` and declines at run time on the null-value tail or a page past the value structure,
    /// falling into the gather. `compose_paging` is therefore an **upper bound** on `OrderbyWalk`,
    /// exact in every other cell -- one-directional by construction, not drift between two
    /// implementations. `GatherWalkDeclined` exists so that case is self-identifying rather than
    /// arriving as a bare `Gather` indistinguishable from a genuine misprediction.
    ///
    /// The prediction is 3-way and this is 10-way, so they are not compared blindly — see
    /// `PagingTaken` for what each variant licenses.
    ///
    /// Reported here but NOT stored here: the live value lives in the `PAGING_TAKEN` thread-local
    /// and `take_phase_stats` merges it in. It is the one field written by something other than an
    /// executor, so sharing a slot with the counters meant every publisher had to remember not to
    /// clobber it — see `PAGING_TAKEN`.
    pub(crate) paging_taken: PagingTaken,
}

/// Which exit a printing-space fastpath took: `printing_compose_fastpath` against the 3-way
/// `ComposePaging` the cost model predicted, and `printing_range_fastpath` against nothing (the
/// model predicts no strategy for it — see the `Range*` block). Every exit of both records itself,
/// which is what lets `NotEntered` mean exactly one thing.
///
/// The name is compose-era and now undersells the type: only `Perm`/`OrderbyWalk`/`Gather` are
/// about paging at all, and the rest name gates. Left alone deliberately — `paging_taken` is the
/// wire key `scripts/bench_cost_model_agreement.py` and any later harness read, and renaming it
/// would churn the compose narrative throughout this file for no diagnostic gain.
///
/// An enum rather than a `&'static str` because the valid set is load-bearing in four places — the
/// fastpaths' record sites, `compose_paging_prediction_matches_the_branch_taken`'s legal-label
/// tables, `scripts/bench_cost_model_agreement.py`'s strategy constants, and this doc. As strings
/// those four drift silently; as variants the compiler settles three of them and a typo stops
/// being expressible.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub(crate) enum PagingTaken {
    /// Neither fastpath was entered at all — every other plan, and a compose or range plan that
    /// `run_query_with_plan` rejected before calling it. A fastpath that DID enter and declined
    /// always names the gate instead, so this never means "declined".
    ///
    /// `#[default]` so a cleared `PAGING_TAKEN` reads as "nothing recorded", which is what
    /// `take_phase_stats` leaves behind and what an uninstrumented plan reports.
    #[default]
    NotEntered,
    /// A strategy ran, and it must be the predicted one. These three are the only variants
    /// comparable against `compose_paging` as an equality.
    Perm,
    OrderbyWalk,
    Gather,
    /// A walk WAS available and was attempted, declined, and fell back to the gather. Legal under a
    /// predicted `OrderbyWalk` and only under one; a bare `Gather` in that cell would mean the
    /// availability tests really had drifted.
    GatherWalkDeclined,
    /// The total was 0 or the offset was past it, so the fastpath returned an empty page before any
    /// strategy ran. Shared by both fastpaths — a compose that composed to nothing and a range whose
    /// `k` the offset overran are the same observation, and neither exercises a prediction.
    EmptyPage,
    /// The structural check failed (not a composable expr, or the compose indexes are not built). A
    /// TRIPWIRE, not an expected outcome, for the same reason `RangeNotBare` is:
    /// `PhysicalPlan::PrintingCompose.applicable` IS `printing_compose_applicable`, which already
    /// requires both halves of this test, and every caller of `printing_compose_fastpath` gates on
    /// it first. So a plan `explain` ranked can only reach this if those two structural tests have
    /// drifted apart.
    NotComposable,
    /// The `COMPOSE_GATHER_MAX_CARD_FRACTION` breadth gate.
    DeclineBroad,
    /// The two sparse declines: pre-compose off the estimator's upper bound, and post-compose off
    /// the real total, respectively.
    DeclineSparseEstimate,
    DeclineSparseExact,

    // ── printing_range_fastpath ─────────────────────────────────────────────
    // Every exit of the range fastpath, for the same reason the compose ones exist: before these,
    // a declining `PrintingRangeScan` reached `explain_analyze` with a non-empty `declined_ns` and
    // a `NotEntered` label, i.e. "it cost something and we cannot say what for". Unlike compose
    // there is nothing to check them AGAINST -- `PlanFeatures` carries no range-strategy prediction
    // -- so these size the declines rather than falsify a prediction.
    /// Ordered by the range field itself (a price predicate under a `usd` orderby), so the index IS
    /// the sort permutation and the page is windowed straight out of it. One of the fastpath's two
    /// success exits.
    RangeAligned,
    /// The general case: walked the card sort permutation, emitting per-card-contiguous runs. The
    /// other success exit.
    RangeWalk,
    /// No bare range bounds — not a range predicate, or no index for its field. A TRIPWIRE, not an
    /// expected outcome: `PhysicalPlan::PrintingRangeScan.applicable` already requires
    /// `bare_range_bounds(..).is_some()`, so a plan `explain` ranked can only reach this if those
    /// two structural tests have drifted apart.
    RangeNotBare,
    /// `range_too_broad_to_narrow` said no: the range is selective enough that the ordinary
    /// narrowing beats the walk. The expected decline in production, and the cheapest — two binary
    /// searches and a ratio.
    RangeSelective,
    /// A `usd` orderby whose predicate is not a price leaf, so the index is not the sort
    /// permutation and there is no aligned mapping to window. Distinct from `RangeNoPermutation`
    /// because the orderby DOES have an aligned representation in general — this query just cannot
    /// use it — and a harness reading one label for both could not tell a missing permutation from
    /// a mismatched one.
    RangeUnalignedPrice,
    /// `k <= STREAM_MIN_MATCHES`. The range analogue of `DeclineSparseExact`, and exact for the
    /// same reason: `k` is the index's own count, not an estimate. Cheap either way — this gates
    /// before the walk, where compose's namesake gates after a full compose.
    RangeSparse,
    /// No sort permutation for this (column, direction) pair, so there is nothing to walk.
    RangeNoPermutation,
    /// A permutation exists but its length disagrees with the corpus. Its own label rather than
    /// folded into `RangeNoPermutation` because the two mean different things: that one is a query
    /// the fastpath does not serve, this one is an index built against a different store. It should
    /// never fire, and a non-zero count in the decline table is the finding.
    RangePermutationStale,
}

/// Which acquire branch produced a query's cost features — the `count_source` `explain` reports.
/// An enum for the same reason as `PagingTaken`: the three `Prep::Range` payloads are not
/// interchangeable (reporting "range" for a compose once sent an investigation down a wrong path),
/// and as strings nothing stopped a fourth from being invented at a call site.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum CountSource {
    CardRangePopcount,
    PrintingRangeScan,
    PrintingCompose,
    Plane,
    Candidates,
}

impl CountSource {
    /// Byte-identical to the strings this reported before it became an enum. Snake case, unlike
    /// `PagingTaken`'s PascalCase — which is exactly why these are spelled out rather than derived
    /// from `Debug`: the two label sets have different conventions and both are a wire format.
    fn label(self) -> &'static str {
        match self {
            CountSource::CardRangePopcount => "card_range_popcount",
            CountSource::PrintingRangeScan => "printing_range_scan",
            CountSource::PrintingCompose => "printing_compose",
            CountSource::Plane => "plane",
            CountSource::Candidates => "candidates",
        }
    }
}

/// What the residual narrowing produced, before `PreparedCandidates` flattened it to
/// `candidate_cards`. `Cards`/`Printings` mean a sorted vec was built (some site ran a `collect` +
/// `sort_unstable`); the `Bits` pair mean it stayed word-wise and never sorted; `None` means the
/// narrowing produced nothing at all.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum NarrowedRepr {
    None,
    Cards,
    Printings,
    CardBits,
    PrintingBits,
}

impl NarrowedRepr {
    /// Byte-identical to the strings this reported before it became an enum.
    fn label(self) -> &'static str {
        match self {
            NarrowedRepr::None => "none",
            NarrowedRepr::Cards => "cards",
            NarrowedRepr::Printings => "printings",
            NarrowedRepr::CardBits => "card_bits",
            NarrowedRepr::PrintingBits => "printing_bits",
        }
    }
}

impl PagingTaken {
    /// The string `explain_analyze` reports under `"paging_taken"`. Byte-identical to the labels
    /// this reported before it became an enum, so the Python surface did not move — `NotEntered`
    /// is still the empty string a consumer tests for.
    fn label(self) -> &'static str {
        match self {
            PagingTaken::NotEntered => "",
            PagingTaken::Perm => "Perm",
            PagingTaken::OrderbyWalk => "OrderbyWalk",
            PagingTaken::Gather => "Gather",
            PagingTaken::GatherWalkDeclined => "GatherWalkDeclined",
            PagingTaken::EmptyPage => "EmptyPage",
            PagingTaken::NotComposable => "NotComposable",
            PagingTaken::DeclineBroad => "DeclineBroad",
            PagingTaken::DeclineSparseEstimate => "DeclineSparseEstimate",
            PagingTaken::DeclineSparseExact => "DeclineSparseExact",
            PagingTaken::RangeAligned => "RangeAligned",
            PagingTaken::RangeWalk => "RangeWalk",
            PagingTaken::RangeNotBare => "RangeNotBare",
            PagingTaken::RangeSelective => "RangeSelective",
            PagingTaken::RangeUnalignedPrice => "RangeUnalignedPrice",
            PagingTaken::RangeSparse => "RangeSparse",
            PagingTaken::RangeNoPermutation => "RangeNoPermutation",
            PagingTaken::RangePermutationStale => "RangePermutationStale",
        }
    }
}

thread_local! {
    /// Scratch slot the instrumented executors publish into. **Only meaningful between a
    /// `take_phase_stats()` and the next publish** — that window is the whole contract, and
    /// `explain_analyze` is the only caller that establishes it.
    ///
    /// Nothing clears this on the production path, deliberately: a `Cell` write per query to reset
    /// state no production reader consults would be cost for nobody. So a reader added outside
    /// `explain_analyze` gets an arbitrary earlier query's numbers, not zeros and not the current
    /// query's. Take first, then run, then read.
    ///
    /// Every publisher writes this slot WHOLE. That is what keeps the staleness above bounded to
    /// "the last query on this thread" instead of compounding: an earlier draft had the publishers
    /// write 8 of 10 fields and inherit the rest through `..c.get()`, which silently widened "this
    /// run" to "this thread, ever" and was measured leaking a compose-only `paging_taken` into
    /// `GatheredScan` on 49 of 600 queries. The two fields that made `..c.get()` necessary are now
    /// owned elsewhere: `paging_taken` by `PAGING_TAKEN` below, `ns_round_total`/`result_total` by
    /// `explain_analyze`, which fills them after the take.
    static PHASE_STATS: std::cell::Cell<PhaseStats> = const { std::cell::Cell::new(PhaseStats {
        cards_visited: 0, printing_span: 0, printings_examined: 0, matches_pushed: 0, perm_steps: 0, ns_setup: 0, ns_loop: 0,
        ns_finish: 0, ns_round_total: 0, ns_prepare: 0, result_total: 0, paging_taken: PagingTaken::NotEntered,
    }) };

    /// The compose fastpath's branch label, stored apart from `PHASE_STATS` because it is the one
    /// field written by something that is not an executor, and on a path that runs in production.
    ///
    /// Two things fall out of the split, both of which the shared slot got wrong:
    /// - a materializing publish can no longer wipe it, so the routed path's "compose declines,
    ///   records its gate, falls through to a materializing plan" sequence is preserved by
    ///   construction rather than by every publisher remembering to write `..c.get()`;
    /// - `note_paging_taken` is a one-byte store instead of a read-modify-write of the whole
    ///   ~80-byte `PhaseStats`, which matters because it is on the production compose path.
    ///
    /// `take_phase_stats` reassembles the two into one `PhaseStats`, so consumers see no seam.
    static PAGING_TAKEN: std::cell::Cell<PagingTaken> = const { std::cell::Cell::new(PagingTaken::NotEntered) };

    /// The compose fastpath's three counters, stored apart from `PHASE_STATS` for exactly the reason
    /// `PAGING_TAKEN` is: this is the production compose path, and a whole-struct publish there is a
    /// read-modify-write of ~80 bytes to report 24.
    ///
    /// Measured, because the first version did write the whole struct: a paired A/B against the same
    /// build without it read `+1.4 µs` and `+3.1 µs` on a 129 µs mean (slower on 619 and 863 queries
    /// of ~2,000), and neutralising every cost-model change left the regression in place — the
    /// publish WAS the regression. The note on `PAGING_TAKEN` above had already said so.
    ///
    /// Safe to leave unwritten by the other executors for the same reason `PHASE_STATS` is safe:
    /// `take_phase_stats` replaces this with the default, and `explain_analyze` takes before every
    /// participant runs, so a plan that does not publish here reads zeros rather than the last
    /// compose's numbers. Outside that window nothing reads it at all.
    static COMPOSE_WORK: std::cell::Cell<ComposePageWork> = const { std::cell::Cell::new(ComposePageWork {
        cards_visited: 0, printings_examined: 0, matches_pushed: 0, ns_total: 0,
    }) };

    /// The routed path's disjoint phase split — see `RoutedPhases`. Its own slot for the same reason
    /// the two above have theirs: `run_query_routed` is the production entry point, and one 24-byte
    /// store there is affordable where a read-modify-write of `PhaseStats` measurably was not.
    ///
    /// Written by `run_query_routed` only, so it never collides with the executors' slot; the routed
    /// path's dispatch phase CONTAINS an executor publish into `PHASE_STATS`, and the two nest.
    #[cfg(feature = "routed-phases")]
    static ROUTED_PHASES: std::cell::Cell<RoutedPhases> = const { std::cell::Cell::new(RoutedPhases {
        ns_acquire: 0, ns_choose: 0, ns_dispatch: 0,
    }) };

    /// `prepare_candidates`' time for the run in progress, handed to the executor that follows it —
    /// the two are separate calls in `run_query_with_plan`, and only the executor publishes stats.
    ///
    /// Unlike the two slots above this one is never cleared by `take_phase_stats`, because it is
    /// not a stat: it is a value in flight between two calls, and the receiving executor consumes
    /// it with `replace(0)`. That handoff is what keeps it from leaking, and it is an invariant
    /// spanning three functions rather than anything the type system enforces — every
    /// `timed_prepare_candidates` must be followed by an executor that publishes. The
    /// `debug_assert` below pins it, because the failure is silent: an unconsumed value is read by
    /// the NEXT participant and reported as its `ns_prepare`, which looks entirely plausible.
    static PENDING_PREPARE_NS: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

/// `prepare_candidates`, timed. Only `run_query_with_plan`'s materializing arms use this; the routed
/// path calls `prepare_candidates` directly, since its cost already shows up in `acquire_ns`.
fn timed_prepare_candidates(
    ctx: &QueryCtx,
    params: &QueryParams,
    filter: &mut FilterExpr,
    plane: Option<&PlaneExpr>,
) -> PreparedCandidates {
    // Nothing may be in flight here: the previous timed prepare's executor must already have
    // consumed it. See PENDING_PREPARE_NS for why an unconsumed value is worse than a missing one.
    debug_assert_eq!(
        PENDING_PREPARE_NS.with(std::cell::Cell::get),
        0,
        "a previous timed_prepare_candidates was not consumed by an executor; its time would be reported as this run's",
    );
    let t = std::time::Instant::now();
    let prep = prepare_candidates(ctx, params, filter, plane);
    PENDING_PREPARE_NS.with(|c| c.set(t.elapsed().as_nanos() as u64));
    prep
}

/// Record which paging branch the compose fastpath took. Reporting only -- see
/// `PhaseStats::paging_taken` for why the predicted branch is not enough.
fn note_paging_taken(which: PagingTaken) {
    PAGING_TAKEN.with(|c| c.set(which));
}

/// Hand a shared-artifact build time to the executor that follows, the same way
/// `timed_prepare_candidates` does. Used by the plane path, whose artifact the router builds in
/// acquire and a forced run has to rebuild; see `exec_plane_popcount_order`.
fn note_pending_prepare_ns(ns: u64) {
    debug_assert_eq!(
        PENDING_PREPARE_NS.with(std::cell::Cell::get),
        0,
        "a previous shared-artifact build was not consumed by an executor; its time would be reported as this run's",
    );
    PENDING_PREPARE_NS.with(|c| c.set(ns));
}

/// Publish what a compose paging branch did, so `PrintingCompose` reports the same three quantities
/// the two materializing plans do.
///
/// A 24-byte store into `COMPOSE_WORK`, not a write of the whole `PhaseStats` — see that slot's doc
/// for the measurement that forced the split. `take_phase_stats` merges the two, so consumers see no
/// seam, and the phase timings stay zero: compose's arm is not decomposed into setup/loop/finish, so
/// there is nothing to check them against, and publishing an unvalidatable number is how
/// `printing_span` became load-bearing in the first place.
fn publish_compose_work(work: ComposePageWork) {
    COMPOSE_WORK.with(|c| c.set(work));
}

/// Last executor run's phase stats, and clear them. `explain_analyze` reads this immediately after a
/// timed run, having cleared beforehand — see `PHASE_STATS` for why that order is the contract and
/// why an unpaired read does NOT see zeros.
///
/// Both slots are taken together, so a caller cannot clear one and leave the other to leak into the
/// next participant — which is the whole failure `plan_stats_never_leak_between_participants` pins.
fn take_phase_stats() -> PhaseStats {
    let mut stats = PHASE_STATS.with(|c| c.replace(PhaseStats::default()));
    stats.paging_taken = PAGING_TAKEN.with(|c| c.replace(PagingTaken::NotEntered));
    // Compose's counters live in their own slot; fold them in so a consumer cannot tell which
    // executor wrote them. Only ONE of the two publishes per run -- a materializing executor writes
    // `PHASE_STATS` and never `COMPOSE_WORK`, compose the reverse -- so this cannot double-count, and
    // taking both together is what stops either leaking into the next participant.
    let compose = COMPOSE_WORK.with(|c| c.replace(ComposePageWork::default()));
    if compose.cards_visited | compose.printings_examined | compose.matches_pushed | compose.ns_total != 0 {
        stats.cards_visited = compose.cards_visited;
        stats.printings_examined = compose.printings_examined;
        stats.matches_pushed = compose.matches_pushed;
        // Compose reports one undivided span; `ns_loop` is where it belongs so the phase sum equals
        // the executor's time. See `ComposePageWork::ns_total`.
        stats.ns_loop = compose.ns_total;
    }
    stats
}

/// P4 executor: the universal gathered per-card loop + `select_page`. Runs any
/// query (printing-keyed orderbys, stores without permutations, or anything the
/// other plans decline). Caller has run `prepare_candidates`.
fn exec_gathered_scan<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
    prep: &PreparedCandidates,
    plane: Option<&PlaneExpr>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    // First of the three phase boundaries — everything down to the match loop is `ns_setup`.
    let t_start = std::time::Instant::now();
    let QueryCtx { cards, printings, offsets, strings, indexes } = *ctx;
    let QueryParams { mode, prefer, sort_col, descending, limit, page_offset, .. } = *params;
    let all_match_known = prep.all_match_known;
    let existential_plane = existential_plane_for(mode, plane, indexes);
    let card_ids = prep.card_ids(ctx);

    // Gathered path (printing-keyed orderbys, or stores without permutations): push
    // each card's matches directly into the selector, which keeps its buffer bounded
    // at ~k via prune + running cutoff (see GatherSelect) rather than gathering every
    // match. `total` counts every match; the page is the k smallest.
    let mut sel = GatherSelect::new(page_offset, limit);
    // artwork-mode scratch, reused per card (#629: group-id-indexed, not illustration_id-keyed).
    // Pre-sized to max_artwork_groups so the grouping loop needs no per-printing resize check.
    let mut group_best: Vec<Option<(u32, f64)>> =
        if matches!(mode, Mode::Artwork) { vec![None; usize::from(u16::from(indexes.max_artwork_groups))] } else { Vec::new() };
    let mut touched: Vec<u16> = Vec::new();
    // card_pass residual: the top-level children still printing-dependent for
    // the current card (reused buffer; see FilterExpr::card_pass).
    let mut residual: Vec<&FilterExpr> = Vec::new();
    let mut residual_is_or = false;
    // Counters for the three features this plan's cost arm keys on, so they can be checked against
    // what the loop really does. Plain locals — no atomics, no branch — published once at the end.
    let (mut n_cards_visited, mut n_printing_span, mut n_matches_pushed) = (0u64, 0u64, 0u64);
    // The printings the per-card body actually ran on, which is NOT `n_printing_span` (the span).
    // See `push_card_matches`; the two are published side by side so the gap is readable rather than
    // inferred.
    let mut n_printings_examined = 0u64;
    // Ends `ns_setup` and starts `ns_loop` — one read, two phases.
    let t_loop = std::time::Instant::now();
    for cid in card_ids {
        n_cards_visited += 1;
        let card = &cards[cid as usize];
        // #634 Step 1: all_match_known means the narrowing already proved
        // every candidate matches — card_pass would just re-derive Tri::True
        // at real per-node evaluation cost for nothing.
        let all_match = all_match_known
            || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or, prep.proven_conjuncts) {
                Tri::False | Tri::Null => continue,
                Tri::True => true,          // every printing matches: skip per-printing checks
                Tri::PrintingDep => false,  // verify each printing against the residual below
            };
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;
        let before = sel.buf().len();
        n_printing_span += (end - start) as u64;
        n_printings_examined += u64::from(push_card_matches(
            card, cid, printings, &indexes.artwork_group_col, start, end, all_match, &residual, residual_is_or, mode, prefer,
            sort_col, descending, strings, existential_plane, sel.buf(), &mut group_best, &mut touched,
        ));
        n_matches_pushed += (sel.buf().len() - before) as u64;
        sel.absorb(before);
    }
    // Ends `ns_loop` and starts `ns_finish`.
    let t_finish = std::time::Instant::now();
    let (total, page_ids) = sel.finish(page_offset, limit);
    let page = page_ids
        .into_iter()
        .map(|(cid, pid)| (&cards[cid as usize], &printings[pid as usize]))
        .collect();
    let t_end = std::time::Instant::now();
    let prep_ns = PENDING_PREPARE_NS.with(|c| c.replace(0));
    PHASE_STATS.with(|c| {
        // A WHOLE-struct set, deliberately: nothing here is inherited, so this executor cannot
        // report a field an earlier query wrote. The compose fastpath's `paging_taken` survives
        // regardless — it lives in `PAGING_TAKEN`, which this does not touch.
        c.set(PhaseStats {
            cards_visited: n_cards_visited,
            printing_span: n_printing_span,
            printings_examined: n_printings_examined,
            matches_pushed: n_matches_pushed,
            perm_steps: 0, // GatheredScan never walks the permutation
            ns_setup: (t_loop - t_start).as_nanos() as u64,
            ns_loop: (t_finish - t_loop).as_nanos() as u64,
            ns_finish: (t_end - t_finish).as_nanos() as u64,
            ns_round_total: 0, // filled by explain_analyze, which owns the round timer
            ns_prepare: prep_ns,
            result_total: 0,   // likewise: explain_analyze fills it from the value actually returned
            paging_taken: PagingTaken::NotEntered, // owned by PAGING_TAKEN; take_phase_stats merges it in
        });
    });
    (total, page)
}

/// The six-string convenience form of `run_query_routed`, for tests that state a query the way the
/// HTTP surface does. `QueryParams::from_strs` is still the single string→enum interpretation — the
/// PyO3 methods call it directly, because they also have a `SortBound` to attach and this wrapper has
/// no filter tree left to extract one from (see `bind_and_split_filter`). A query routed through here
/// therefore walks the whole permutation, which is the correct-but-slower default.
#[cfg(test)]
#[allow(clippy::too_many_arguments)] // the string surface, adapted in one place; the core takes two structs
fn run_query<'a>(
    ctx: &QueryCtx<'a>,
    filter: &mut FilterExpr,
    plane: Option<&PlaneExpr>,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
    page_offset: usize,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    // #702: plan selection is one cost-based routing layer (`run_query_routed`,
    // `argmin cost::plan_cost` over the applicable plans), not a hand-tuned decision tree.
    let params = QueryParams::from_strs(unique, prefer, orderby, direction, limit, page_offset);
    // `None`: this wrapper receives an already-split filter and has no unsplit form to offer, so compose
    // is costed exactly as it was before the split became non-destructive. See `bind_and_split_filter`.
    run_query_routed(ctx, &params, filter, None, plane)
}

/// `cost::PlanFeatures::scan_units` for a query: the rows the per-row residual
/// scan touches, in the plan's operating space. `Mode::Card` breaks at the first
/// matching printing (≈ one row per candidate), so it is `eval_domain`;
/// printing/artwork scan every printing of every candidate, so it is the printing
/// count under those cards (`n_printings` when unnarrowed). The `Some` branch sums
/// `offsets` ranges over the candidate cards — O(candidates), a routing-time cost
/// only paid when a candidate list exists. Called from `candidate_feats`.
fn scan_units(mode: Mode, candidate_cards: Option<&[u32]>, offsets: &AOffsets, n_printings: u32, n_cards: u32) -> u32 {
    // Card mode used to short-circuit to `eval_domain`, on the theory that the loop breaks at the
    // first matching printing of each card. The `printing_span` counter says otherwise: across
    // every acquire branch, GatheredScan and StreamedSelect scan the FULL printing span of their
    // candidates in card mode too, and the old feature read 0.25-0.33 against it. So the QUANTITY is
    // the same in all three modes -- printings under the candidates -- and there is no mode branch in
    // what this means.
    //
    // There is a mode branch in how it is PAID FOR. The exact sum is O(candidates), which
    // printing/artwork have always paid; card mode had not, and simply removing the short-circuit
    // added ~30-47 us of acquire to every broad card query (`border:black` 47 us at 31,169
    // candidates) -- a 40-70 us end-to-end regression on exactly those queries, for a feature nothing
    // needed to be exact. The O(1) projection is what the counter was validated against in the first
    // place: `eval_domain * n_printings/n_cards` reads 0.90-1.02 for both plans in all three modes,
    // inside the [0.8, 1.25] bar.
    //
    // Printing/artwork keep the exact sum: they are already paying for it, and it reads 1.00 with a
    // spread of 1.0. Card mode takes the projection and pays nothing.
    match candidate_cards {
        None => n_printings,
        Some(v) => match mode {
            Mode::Card => {
                let per_card = f64::from(n_printings) / f64::from(n_cards.max(1));
                (f64::from(v.len() as u32) * per_card) as u32
            }
            Mode::Printing | Mode::Artwork => v
                .iter()
                .map(|&cid| u32::from(offsets[cid as usize + 1]) - u32::from(offsets[cid as usize]))
                .sum(),
        },
    }
}

/// Dispatch the two candidate-list executors (P3/P4) on a shared `prep`. Both
/// phase-2 prep branches funnel their P3/P4 winner through here, so the executor
/// call site exists once. `PlanePopcountOrder` is handled by its own bitmap
/// executor and `PrintingRangeScan` never reaches here (non-materializing).
///
/// Takes `CandidatePlan`, not `PhysicalPlan`: the match below is then exhaustive, and the
/// "only P3/P4 reach here" precondition is the caller's to satisfy in the type rather than a
/// comment backed by an `unreachable!`.
fn exec_from_candidates<'a>(
    plan: CandidatePlan,
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
    prep: &PreparedCandidates,
    plane: Option<&PlaneExpr>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    match plan {
        CandidatePlan::StreamedSelect => exec_streamed_select(ctx, params, filter, prep, plane),
        CandidatePlan::GatheredScan => exec_gathered_scan(ctx, params, filter, prep, plane),
    }
}

/// When a `Prep::Range` fast path declines at runtime, try its non-materializing sibling before
/// paying `prepare_candidates`.
///
/// `run_query_routed`'s fallback re-chooses in `PlanScope::Candidates`, which excludes every fast
/// path — so the sibling was unreachable even
/// when it was applicable, would not decline, and was an order of magnitude cheaper. Measured on
/// `usd>20` at `unique=printing`: `PrintingRangeScan` declines, the materializing fallback runs in
/// ~105 µs, and `PrintingCompose` answers the same query in 2.3 µs.
///
/// Gated on the (now-fixed) cost model rather than tried unconditionally: only run the sibling when
/// it prices below the cheapest materializing plan, so this can never make a query slower than the
/// fallback it replaces on the model's own terms. Both paths are correct for any query either is
/// applicable to — `force_plan_differential_agreement` holds every plan to identical results — so
/// this changes only which correct plan runs.
// Eight parameters: the declined plan, the three shared query artifacts (`ctx`, `params`, `filter`),
// the two split-filter pieces a sibling needs to re-derive its own applicability (`unsplit`, `plane`),
// and the router's `choose`. Bundling them would mean a struct whose only purpose is this call, and
// whose fields the two call sites would immediately destructure again.
#[allow(clippy::too_many_arguments, reason = "shared query artifacts plus the router's choose; a wrapper struct would only be unpacked again")]
fn declined_sibling_fastpath<'a>(
    declined: PhysicalPlan,
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
    unsplit: Option<&FilterExpr>,
    plane: Option<&PlaneExpr>,
    feats: &cost::PlanFeatures,
    choose: &impl Fn(&FilterExpr, &cost::PlanFeatures, PlanScope) -> PhysicalPlan,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    let sibling = match declined {
        PhysicalPlan::PrintingRangeScan => PhysicalPlan::PrintingCompose,
        PhysicalPlan::PrintingCompose => PhysicalPlan::PrintingRangeScan,
        _ => return None, // a materializing plan won the estimate; it has no fast-path sibling
    };
    if !sibling.applicable(ctx, params, filter, unsplit, plane) {
        return None;
    }
    if cost::plan_cost(sibling, feats) >= cost::plan_cost(choose(filter, feats, PlanScope::Candidates), feats) {
        return None;
    }
    match sibling {
        PhysicalPlan::PrintingRangeScan => printing_range_fastpath(ctx, params, filter),
        PhysicalPlan::PrintingCompose => printing_compose_fastpath(ctx, params, compose_source(filter, unsplit, plane)),
        _ => unreachable!("sibling is one of the two printing-space fast paths"),
    }
}

/// Which paging strategy `printing_compose_fastpath` would run for this query, decided the same
/// way the fastpath itself decides.
///
/// Every acquire branch that costs `PrintingCompose` as a competitor needs this, not just compose's
/// own: `mk_plan_feats` defaults it to `Gather`, and `Gather` is the one branch of compose's cost
/// whose page term is `O(eval_domain)`. A branch that leaves the default while also setting
/// `eval_domain` to the unnarrowed universe charges a walk-shaped compose for a full-corpus gather
/// — measured at 33x over-costed from the `PrintingRangeScan` acquire (~125 µs predicted against
/// ~2.4 µs measured on `usd>20`/printing), which is enough to rank compose last and leave a 46x
/// faster plan unused. See docs/issues/done/local-engine-plan-misselection.md.
/// The permutation-free gather branch's two decline gates: `Some(reason)` if `printing_compose_fastpath`
/// will refuse this query, `None` if it will run it.
///
/// Extracted so the COST MODEL can ask the same question. These conditions used to live inline in the
/// fastpath, which meant `acquire_plan_features` had no way to know the plan it was costing would
/// return `None` — so it priced a real gather, compose could win the argmin on that price, and
/// dispatch then paid the detour and ran something else anyway. `compose_paging_with_total` now
/// consults this and predicts `ComposePaging::Decline`, which costs infinity and keeps the plan out
/// of the argmin entirely.
///
/// One function, two callers, so the prediction cannot drift from the behaviour by construction —
/// and `paging_taken` (#797) measures whether it has.
///
/// No permutation (rarity/usd) AND no orderby walk (card/artwork mode): before paying for the real
/// build, check whether the gather is worth it at all using the same estimate
/// `compose_printing_estimate` already computes cheaply in acquire (O(log n) partition points /
/// plane popcounts, no scatter) — see `COMPOSE_GATHER_MAX_CARD_FRACTION`'s doc for why this needs
/// its own threshold, not `MAX_NARROW_FRACTION`. The check must be in **mode space** (cards for
/// `Card`, artworks for `Artwork`), not raw matching printings: a bare range's printing-space match
/// count is a poor proxy for how many *candidates* `gather_composed_page` will actually visit (a
/// card can have several qualifying printings). A plain `.min(domain)` cap is *not* enough to
/// project it down, though — it saturates to exactly `domain` for every query whose printing-space
/// count exceeds it (`cn<100`: 35,021 printings vs 31,508 cards, and `usd<50`: 80,527 vs 31,508
/// both saturate to the *identical* 31,508, even though their true card counts are 17,616 vs 31,217
/// — miles apart), which loses exactly the signal this check needs. A balls-into-bins estimate —
/// `k` matching printings landing on `domain` cards, expected distinct cards touched
/// `≈ domain·(1 − e^(−k/domain))` — doesn't saturate the same way and tracks the true count much
/// better (checked against real totals: `cn<100` estimate 21,140 vs true 17,616; `usd<50` estimate
/// 29,062 vs true 31,217; clearly separated, unlike the capped estimate's identical 31,508 for both).
/// Distinct cards `k` matching printings are expected to touch, as balls into bins:
/// `domain * (1 - e^(-k/domain))`. Unlike `k.min(domain)` this does not saturate -- `cn<100` (35,021
/// printings) and `usd<50` (80,527) both cap to exactly 31,508 against true counts of 17,616 and
/// 31,217, losing the entire signal between them.
/// How much `balls_into_bins` over-states compose's distinct-card count, measured against
/// `cards_visited` once `PrintingCompose` began reporting it.
///
/// The model assumes each matching printing lands in an independently chosen card. Compose's
/// predicates are CLUSTERED instead -- a legality leaf broadcast down sets every printing of a
/// matching card at once -- so the same `k` printings touch far fewer cards than independence
/// predicts. Over 1,036 measured gather rows the raw estimator reads a median **1.73x** the truth.
///
/// A calibration constant and not a better model, deliberately: a size-biased estimator over the
/// corpus's span histogram (`P(hit) = 1-(1-p)^s` per span-`s` card, which is the principled fix for
/// treating every card as one equally likely bin) was tried and scored NO better -- log error 0.512
/// against 0.513 -- because the failing assumption is independence, not uniform bins, and a
/// size-biased model is still an independence model. Per-slice constants keyed on
/// broadcast/range were also tried and added nothing once the exact rows below were separated out
/// (test log error 0.439 against 0.438).
///
/// Fit on 1,036 rows, scored on a held-out 1,047: test log error 0.678 -> 0.438, median 1.73 -> 0.98,
/// spread unchanged at 3.6. Spread is what a constant cannot fix, and it is what remains.
///
/// NOT applied where `range_card_counts_for` answers exactly -- those rows measure 1.000 with log
/// error 0.056 and dividing them by anything would break them. That mixture is why the bias looked
/// like 1.35 before the two populations were separated.
const COMPOSE_CARD_ESTIMATE_BIAS: f64 = 1.78;

/// Printings the gather BIT-TESTS per matching printing.
///
/// `compose_scan_printings` was the composed bitmap's popcount, on the stated grounds that compose
/// "walks the set bits". `gather_composed_page` does not: except in its card/default-prefer arm it
/// iterates `start..end` of every candidate card and tests each printing, so the real count is the
/// SPAN of the candidate cards. Measured against `printings_examined`, the popcount reads a median
/// 0.68 of the truth -- the gather tests 1.47 printings for every one that is set.
///
/// A single constant, unlike the card estimate's: per-slice constants keyed on broadcast/range were
/// tried and made the held-out SPREAD worse (14.8 against 9.2) for a log-error gain of 0.005, which
/// is overfitting four numbers to a mixture. Test log error 0.845 -> 0.700, median 0.66 -> 0.97.
///
/// The alternative is to make the feature true by changing the executor -- walking set bits, as the
/// old comment claimed. Measured ceiling for that: bit tests on non-set printings are 11% of the
/// gather's modelled page cost (`card_pass` is 60%, `push` 26%), so it is a constant-factor
/// optimisation of one branch, not a model change. Left as a separate question.
const COMPOSE_GATHER_SPAN_PER_MATCH: f64 = 1.47;

/// How much bigger the candidate cards on a compose acquire are than an average card.
///
/// `scan_all` projects a card count into printings with the corpus mean (`printings_per_card`,
/// 3.09). That is right when the candidates are a fair sample of the corpus and wrong here: a
/// composable predicate selects cards by having a matching PRINTING, so heavily-reprinted cards are
/// over-represented — the same size bias that makes compose's own gather test 13.2 printings per
/// candidate card against a corpus mean of 3.09.
///
/// This multiplier is on `scan_units`, which costs the MATERIALIZING alternatives should compose
/// lose, so it is a routing input even though compose never reads it. Calibrating the card estimate
/// exposed it: `scan_units [printing_compose]` had been reading 0.75 with the two errors partly
/// cancelling, and dividing `est_cards` by 1.78 moved it to 0.47.
const COMPOSE_CANDIDATE_SPAN_BIAS: f64 = 2.1;

/// `balls_into_bins` with its measured clustering bias divided out of the BALL COUNT. See
/// `COMPOSE_CARD_ESTIMATE_BIAS` for the 1.78 and why clustering causes it.
///
/// The bias used to divide the estimator's OUTPUT, which broke its ceiling. `balls_into_bins` saturates
/// toward `domain` — that is the whole point of using it over `k.min(domain)` — so scaling what it returns
/// caps the calibrated estimate at `domain / 1.78`, i.e. **17,701 of 31,508 cards no matter how many
/// printings match**. `border:black` matches 85,046 of 97,206 printings and visits every card in the corpus;
/// the estimate read 16,511.
///
/// Clustering does not mean "fewer cards than the estimator says". It means the `k` matching printings are
/// not `k` independent draws — a legality leaf broadcast down sets a whole card's printings at once — so the
/// EFFECTIVE ball count is lower. That is an input. Dividing `k` instead is the same correction in the
/// selective regime the 1.78 was fitted on (`balls_into_bins(k, d) ≈ k` for `k ≪ d`, so scaling either side
/// agrees) and keeps the saturation the output form destroyed. Graded against P4's realized `cards_visited`
/// over 3,490 estimator rows, estimate/realized:
///
///     breadth (k / n_printings)      bias on output        bias on input
///     selective  < 0.1               p50 1.02              p50 1.02      <- fitted here, unchanged
///     mid        0.1-0.5             p50 0.91              p50 1.18
///     broad      > 0.5               p50 0.52              p50 0.78
///     all                            p50 0.87  spread 3.3  p50 0.90  spread 2.7
///
/// The mid band overshoots because 1.78 was fitted against the output form; it is not re-fitted here, so
/// this is the shape change alone. Re-fitting it is a fixed-point iteration on a constant whose own value
/// depends on where it is applied — the same caveat `fit_cost_model` records for the residual floor.
///
/// Why this matters for routing and not just accuracy: `eval_domain` feeds a per-card term in both scan
/// arms, and P4's rate is 25.77 ns/card against P3's 11.63, so under-counting candidates discounts P4 by
/// 2.2× as much. On the broad-residual class that inverted the pair — P3 measured 819.7 µs against P4's
/// 1,308.4 with the model pricing them within 5 µs of each other.
fn calibrated_balls_into_bins(k: usize, domain: usize) -> usize {
    balls_into_bins_effective(k as f64 / COMPOSE_CARD_ESTIMATE_BIAS, domain).max(usize::from(k > 0))
}

fn balls_into_bins(k: usize, domain: usize) -> usize {
    balls_into_bins_effective(k as f64, domain).max(usize::from(k > 0))
}

/// `domain * (1 - e^(-k/domain))` for a possibly fractional ball count, clamped to the domain.
fn balls_into_bins_effective(k: f64, domain: usize) -> usize {
    if domain == 0 {
        return 0;
    }
    let est = domain as f64 * (-k / domain as f64).exp().mul_add(-1.0, 1.0);
    (est.round() as usize).min(domain)
}

/// Distinct **artworks** an estimated printing set touches, projected in two stages.
///
/// A single `balls_into_bins` over the whole artwork corpus reads a median 1.38x against the truth,
/// because it treats every artwork as an equally likely bin and ignores that matching printings
/// cluster inside the candidate CARDS. Projecting into the candidate card set's own artwork capacity
/// first reads 1.09x over 400 compose-acquired artwork queries, halving the log error (0.379 against
/// 0.762 for the raw printing count this replaces).
///
/// Corrects BIAS only. The p90/p10 spread stays ~3.3 either way: how a card's matching printings fall
/// across its artwork groups is not something a two-moment projection can see.
fn artwork_estimate(printing_matches: usize, est_cards: usize, n_cards: usize, n_artworks: usize) -> usize {
    if n_cards == 0 || n_artworks == 0 {
        return 0;
    }
    let capacity = ((est_cards as f64) * (n_artworks as f64 / n_cards as f64)).round() as usize;
    balls_into_bins(printing_matches, capacity.min(n_artworks))
}

fn compose_gather_declines(
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    offsets: &Archived<Vec<u32>>,
    printings: &[APrinting],
    cards: &[AOracleCard],
    mode: Mode,
) -> Option<PagingTaken> {
    // The gather's own decline is about the composed set it would page over, so it reads `result`.
    let printing_matches = compose_printing_estimate(filter, indexes, offsets, printings.len()).result;
    // Artwork's domain is n_artworks, not n_cards. That used to be approximated by `cards.len()`
    // because the exact figure meant prefix-summing `artwork_groups` here -- real O(n_cards) work
    // paid just to maybe decline. It is a stored index now, so read the truth: the stand-in is
    // always <= the real domain, which inflates the matched FRACTION and made this gate decline
    // more often than its calibration intends.
    let domain = match mode {
        Mode::Printing => printings.len(),
        Mode::Card => cards.len(),
        Mode::Artwork => u32::from(*indexes.artwork_base.last().expect("artwork_base has n_cards+1 entries")) as usize,
    };
    let est = if matches!(mode, Mode::Printing) {
        printing_matches as f64 // exact in printing space already, no projection needed
    } else {
        domain as f64 * (-(printing_matches as f64) / domain as f64).exp().mul_add(-1.0, 1.0)
    };
    if est > domain as f64 * *COMPOSE_GATHER_MAX_CARD_FRACTION {
        return Some(PagingTaken::DeclineBroad);
    }
    // Small-total decline (mirrors the `Perm` branch's `total <= STREAM_MIN_MATCHES`): in the
    // permutation-free gather regime PrintingCompose has no paging edge over GatheredScan — it just
    // composes the full printing bitmap and projects it back down, two O(n_cards) passes on top of the
    // same gather. For a filter that narrows to few candidate cards the residual-eval saving is tiny
    // while that build/projection dominates, so the candidate/narrowing path wins; decline to it. The
    // SOUND card-space cardinality upper bound decides — exact for a collection leaf, unlike the
    // balls-into-bins `est` above which overestimates a clustered predicate's distinct-card count
    // (goblin: 501 cards but ~1471 `est`) and so cannot make this call. This is what keeps a sparse
    // `type:angel`/`type:goblin` card/usd (newly composable) from regressing onto this path: the
    // collection compose leaves are a printing-mode orderby-walk win, not a card-mode gather win.
    if (estimator::estimate_cardinality(filter, indexes, offsets).hi as usize) <= *STREAM_MIN_MATCHES {
        return Some(PagingTaken::DeclineSparseEstimate);
    }
    None
}

/// `compose_paging_with_total` without a result total, for the range branches: they cost a COMPETING
/// compose and have no total for it, so they get the plain 3-way answer and cannot predict a decline.
fn compose_paging_for(
    indexes: &Archived<CardIndexes>,
    n_cards: usize,
    filter: &FilterExpr,
    mode: Mode,
    sort_col: SortCol,
    descending: bool,
) -> ComposePaging {
    compose_paging_with_total(indexes, n_cards, filter, mode, sort_col, descending, None, None)
}

/// Which paging branch `printing_compose_fastpath` will take — including whether it will refuse the
/// query outright, which the caller that knows the estimated total can now predict.
// Eight arguments because it must see exactly what the fastpath sees -- any parameter dropped here is
// a way for the predicted branch to diverge from the branch taken, which
// compose_paging_prediction_matches_the_branch_taken then fails on.
#[allow(clippy::too_many_arguments)]
fn compose_paging_with_total(
    indexes: &Archived<CardIndexes>,
    n_cards: usize,
    // The expression that will actually be COMPOSED -- `compose_source`'s output on the acquire side and
    // the fastpath's own argument on the other. `compose_needs_broadcast` reads it, and a plane-consumed
    // residual would hide the legality leaf that decides the branch.
    filter: &FilterExpr,
    mode: Mode,
    sort_col: SortCol,
    descending: bool,
    result_total: Option<usize>,
    gather_declines: Option<PagingTaken>,
) -> ComposePaging {
    if indexes.sort_perms.get(sort_col, descending).is_some_and(|p| p.len() == n_cards) {
        // Mirrors the fastpath's `Some(perm) => if total <= STREAM_MIN_MATCHES` bail. `result_total`
        // is the acquire-time ESTIMATE where the fastpath has the exact count, so a query sitting
        // right on the threshold can be classified either way — cheap by construction, since compose
        // and its alternative are within noise of each other exactly there.
        //
        // A total of ZERO is different and must not be predicted as a decline. The fastpath returns at
        // `total == 0 || page_offset >= total` BEFORE reaching the bail, and that return is
        // `Some((total, vec![]))` — it SUCCEEDS, cheaply. Predicting Decline there makes `plan_cost`
        // INFINITY and removes compose from the argmin for a query it would have answered immediately.
        if result_total.is_some_and(|t| t > 0 && t <= *STREAM_MIN_MATCHES) {
            return ComposePaging::Decline;
        }
        ComposePaging::Perm
    } else if orderby_walk_available(sort_col)
        && result_total.is_some_and(|t| orderby_walk_beats_gather(mode, filter, t))
    {
        // The same `orderby_walk_beats_gather` the fastpath applies, on this side's ESTIMATE of the
        // total where the fastpath has the exact one. Any drift shows up as a
        // `compose_paging_prediction_matches_the_branch_taken` failure.
        //
        // `result_total: None` (the `compose_paging_for` entry point, which has no estimate) therefore
        // predicts `Gather` even for printing mode. That is the conservative direction: those callers
        // cost compose as a COMPETITOR from another acquire's features, and over-pricing it there loses
        // a plan choice while under-pricing it wins queries compose then serves slowly -- the exact
        // failure this test caught.
        ComposePaging::OrderbyWalk
    } else if gather_declines.is_some() {
        ComposePaging::Decline
    } else {
        ComposePaging::Gather
    }
}

/// Cost features: the query-invariant fields filled once; the four that vary by
/// count source passed in. Collapses each acquire branch's 8-field literal to one call.
fn mk_plan_feats(
    ctx: &QueryCtx,
    params: &QueryParams,
    matches: u32,
    eval_domain: u32,
    scan_units: u32,
    residual_tier_ns100: u32,
) -> cost::PlanFeatures {
    cost::PlanFeatures {
        n_cards: ctx.n_cards(),
        n_printings: ctx.n_printings(),
        matches,
        eval_domain,
        scan_units,
        residual_card_invariant: false, // diagnostic; only the candidates acquire sets it
        // Defaults to `scan_units`: only an acquire that knows P3 examines fewer printings overrides it.
        stream_scan_units: scan_units,
        residual_tier_ns100,
        limit: params.limit as u32,
        offset: params.page_offset as u32,
        broadcast_printings: 0, // PrintingCompose's legality broadcast-down (0 for ranges / precomputed planes)
        scatter_printings: 0,  // range-slice k — set by both range-plan acquire branches (costed per-plan)
        project_printings: 0,  // PrintingCompose's card/artwork projection pass; CardRangePopcount sets it too (for costing compose)
        popcount_words: 0,     // PrintingCompose overrides this (result-space bitmap words)
        compose_paging: ComposePaging::Gather, // PrintingCompose overrides this (which paging strategy it'll actually use)
        // `run_query_streamed`'s per-card artwork overhead applies to every candidate it visits, in
        // artwork mode only — so it rides `eval_domain` there and vanishes elsewhere. See
        // STREAM_ARTWORK_SEEN_PER_CARD_NS for the mechanism and the measurement.
        artwork_seen_cards: if matches!(params.mode, Mode::Artwork) { eval_domain } else { 0 },
        compose_scan_printings: 0, // set by every branch that costs a PrintingCompose (its own, or as a competitor)
        gather_group_printings: 0, // only the compose branch, and only when its grouping arm runs
    }
}

/// Features for the general candidate path (shared by `acquire_plan_features`'s
/// fallback branch and `run_query_routed`'s lazy-materialize dispatch fallback).
/// `matches`/`eval_domain` = candidate CARD count, the broad/narrow proxy the P3/P4
/// crossover keys on. `scan_units` sums printing counts over the candidate cards
/// (O(candidates) for narrowed printing/artwork — the ~1-2% overhead in
/// plan_routing_ab), kept EXACT on purpose: an O(1) estimate would trade the
/// model's honesty for a couple percent on already-fast queries — do not swap it
/// for `eval_domain·n_printings/n_cards` without re-justifying.
fn candidate_feats(ctx: &QueryCtx, params: &QueryParams, prep: &PreparedCandidates, filter: &FilterExpr) -> cost::PlanFeatures {
    let count = prep.candidate_cards.as_ref().map_or(ctx.n_cards(), |v| v.len() as u32);
    let scan = scan_units(params.mode, prep.candidate_cards.as_deref(), ctx.offsets, ctx.n_printings(), ctx.n_cards());
    // Result cardinality is in the plan's OPERATING space, so it is NOT the candidate card count in
    // every mode. Passing `count` regardless under-counted printing mode by the printings-per-card
    // ratio: measured `matches_pushed / matches` of 2.41 for printing and 1.15 for artwork against
    // 1.00 for card. Both are exactly summable over the candidate list -- `scan` is the printing count
    // (offset deltas, just above) and `artwork_groups` holds each card's distinct-artwork count.
    //
    // Exact only when the narrowing is tight: measured 1.00 on `all_match_known` rows against 0.38-0.56
    // where a residual survives, because roughly half the printings under a candidate then fail it.
    // Discount by the measured pass rate there; undiscounted it swapped a 2.4x under-count for a 2.6x
    // over-count. Result after both: 1.00 tight, 0.91-1.00 with a residual.
    // Two exact-count routes were measured here and BOTH are worse than this span estimate, for one
    // shared reason: the quantity wanted is `|candidates AND residual|`, and neither route provides an
    // intersection.
    //
    // - `estimator::estimate_cardinality` (the engine already ships it, index-backed leaves, independence
    //   over `And`): worse in every mode -- card mean |log| 1.31 against 0.79, p90 33.38 against 9.91.
    // - `RangeCardCounts::distinct_cards`, which is genuinely EXACT and O(log n): available on only 9% of
    //   rows with a residual (7% of the misordered ones, 2.0 of 46.5 ms of pairwise gap), and 6x worse where
    //   it is available -- card p50 6.32 against 1.12. It counts cards matching the residual leaf GLOBALLY,
    //   not cards matching it among the candidates, so it is exact for the wrong set.
    //
    // The exact answer needs the residual evaluated over the candidates, which is the work being costed. A
    // bitmap AND of the candidate set with an indexed leaf's set would give it for that same 9%; nothing
    // cheap covers the rest. See the candidates-acquire section of
    // docs/issues/done/local-engine-loop-phase-measurement.md.
    let matches = match params.mode {
        Mode::Card => count,
        Mode::Printing | Mode::Artwork => {
            let in_space = if matches!(params.mode, Mode::Printing) {
                scan
            } else {
                prep.candidate_cards.as_ref().map_or_else(
                    // Unnarrowed the whole corpus is a candidate, and `artwork_base.last()` is exactly
                    // the corpus artwork total -- free now that it is a stored index.
                    || u32::from(*ctx.indexes.artwork_base.last().expect("artwork_base has n_cards+1 entries")),
                    |v| v.iter().map(|&cid| u32::from(u16::from(ctx.indexes.artwork_groups[cid as usize]))).sum(),
                )
            };
            if prep.all_match_known {
                in_space
            } else {
                let rate =
                    if matches!(params.mode, Mode::Printing) { *RESIDUAL_PASS_RATE_PRINTING } else { *RESIDUAL_PASS_RATE_ARTWORK };
                ((f64::from(in_space) * rate) as u32).max(count.min(in_space))
            }
        }
    };
    let mut feats =
        mk_plan_feats(ctx, params, matches, count, scan, if prep.all_match_known {
            0
        } else {
            verify_cost_tier_unproven(filter, prep.proven_conjuncts)
        });
    // Diagnostic only (`explain`), so the residual-pass-rate population can be split by traffic before any
    // rate moves. A card-invariant residual answers `True`/`False` per card and never `PrintingDep`, so a
    // matching candidate contributes its WHOLE printing span and a non-matching one none of it — the
    // all-or-nothing shape. With a printing-level field in play the printings under one card disagree, which
    // is the shape the single `RESIDUAL_PASS_RATE_*` was fitted on. Nothing reads this in routing.
    feats.residual_card_invariant = !prep.all_match_known && !touches_printing_field(filter);
    // A residual that is invariant WITHIN a card never goes printing-dependent: `card_pass` returns
    // `True`/`False` and never `Tri::PrintingDep`, so `run_query_streamed` sets its per-card `all_match` for
    // every matching card and `card_match_count` answers from span arithmetic. P3 therefore examines **no
    // printings at all** and `printings_examined` reads exactly 0 — while the arm was charging
    // `scan_units * STREAM_SCAN_PER_ROW_NS` for the whole candidate span.
    //
    // `name:s` / artwork is the measured case: `scan_units` 97,206 against a realized 0 for P3 and 60,705 for
    // P4, so the shared feature is 1.60x for the plan that does the work and infinitely wrong for the plan
    // that does not. That charged P3 580 us of a 1,507 us prediction against a 456 us measurement and handed
    // the query to `GatheredScan` at 824 us — 368 us lost on one query, repeated across every orderby.
    //
    // This is the `all_match_known` gate one step weaker. That gate needs the whole residual to be `True`;
    // this needs only that it cannot vary within a card, which `name:s`, `o:`, `t:` and `cmc` all satisfy
    // while being ordinary residuals. P4's `scan_units` is deliberately untouched — it walks each candidate's
    // span to push, so its 60,705 is real work, and this is exactly the asymmetry an argmin needs.
    if feats.residual_card_invariant {
        feats.stream_scan_units = 0;
    }
    if prep.all_match_known || feats.residual_card_invariant {
        feats.artwork_seen_cards = 0;
    }
    // Same signal, second term — **measured, and NOT applied.** `run_query_streamed` answers an artwork count
    // from a STORED per-card group count when `all_match && have_group_counts`, touching no printing, and only
    // walks the span to dedup groups when a residual survives per printing. So `artwork_seen_cards` charging
    // `eval_domain` unconditionally is the wrong SHAPE, and measurably the wrong sign in the fast-path regime.
    // Artwork-minus-printing `ns_loop` delta on identical candidate sets, against a charged +1.21 ns/card:
    //
    //     printings_examined == 0    (stored count)   median -0.46 ns/card   n=9
    //     printings_examined == span (dedup walk)     median +0.38 ns/card   n=8
    //
    // Artwork is *cheaper* than printing when the fast path fires. Gating the term on
    // `all_match_known || residual_card_invariant` duly improved absolute agreement — P3 went from p/m
    // 1.58-1.83 to 1.14-1.34 on those cells — and **regressed routing**: the artwork regret slice went mean
    // 1.59 -> 1.71 us and max 185.5 -> 732.5. So the +1.21 is compensating for something else in the P3/P4
    // artwork balance, and cannot be removed alone. That is item 6, and it needs both arms at once — the same
    // lesson as `bench_gather_loop`'s "P4 cannot be fixed alone".

    feats
}

/// The acquire step of `run_query_routed`'s three-step algorithm (see its doc
/// comment), factored out so `explain`/`explain_analyze` (#745) compute cost
/// features via this exact same code path — never a second copy that can
/// silently drift from what the real router would pick. Picks the query's count
/// source (one of three, by structure) and builds the cost features,
/// materializing the shared artifact it implies: a True-residual plane's popcount
/// (`Prep::Plane`), a bare range's index-`k` (`Prep::Range`, nothing materialized),
/// or `prepare_candidates` (`Prep::Candidates`). Returns the plane popcount bitmap
/// alongside `Prep::Plane` (empty otherwise) — `run_query_routed`'s dispatch needs
/// it to execute the winner; a caller that only wants `feats` (`explain`) drops it.
fn acquire_plan_features(
    ctx: &QueryCtx,
    params: &QueryParams,
    filter: &mut FilterExpr,
    unsplit: Option<&FilterExpr>,
    plane: Option<&PlaneExpr>,
) -> (cost::PlanFeatures, Prep, Vec<u64>) {
    let QueryCtx { cards, offsets, indexes, .. } = *ctx;
    let QueryParams { mode, prefer, sort_col, descending, .. } = *params;
    let n_cards = ctx.n_cards();
    let n_printings = ctx.n_printings();
    // Mean printings per card, the factor between "one printing of each candidate" and "all of them".
    // Both the plane branch and the compose branch's `scan_all` need it; `.max(1.0)` guards a corpus
    // with more cards than printings, which cannot happen but costs nothing to exclude.
    let printings_per_card = (f64::from(n_printings) / f64::from(n_cards)).max(1.0);

    // Scratch for the plane bitmap (`Prep::Plane` only). A fresh `Vec` allocates
    // just once, on the plane branch's `eval_planes`; non-plane queries leave it
    // empty (no alloc).
    let mut plane_bits: Vec<u64> = Vec::new();

    let (feats, prep) = if PhysicalPlan::PlanePopcountOrder.applicable(ctx, params, filter, unsplit, plane) {
        // The ONE plane eval; its popcount IS the exact count. True residual ⇒ tier 0.
        eval_planes(plane.expect("PlanePopcountOrder ⇒ plane"), &indexes.planes, &mut plane_bits);
        let count: u32 = plane_bits.iter().map(|w| w.count_ones()).sum();
        // `scan_units` costs the MATERIALIZING alternatives to this plan (PlanePopcountOrder itself
        // popcounts a bitmap and scans nothing), and here alone it depends on `prefer`.
        //
        // A True residual means `all_match`, so `push_card_matches` takes its first-match branch: under
        // `Prefer::Default` printings are stored prefer-desc, the first printing IS the pick, and the
        // loop breaks after one — `scan_units == count`. Under any other prefer the same loop must
        // score every printing of the card to find the max, so it examines the full span.
        //
        // Measured against `printings_examined` over 4,136 plane-acquire rows: the old unconditional
        // `count` graded 1.00 at p90-p100 (the default-prefer population) and 0.16-0.35 at p10-p50 (the
        // other four prefers) — one value cannot be right for both, and the 3.09x between them is
        // exactly `printings_per_card`. This is the only feature in the vector that reads `prefer`;
        // everywhere else the span-shaped estimate already covers both regimes, because an inexact
        // narrowing makes non-matching candidates burn their whole span regardless of prefer.
        //
        // The custom-prefer side takes the EXACT span rather than `count * printings_per_card`. The
        // corpus mean puts the cell's median at 1.00 but leaves a 3.09x over-count tail (measured p90
        // 3.05, p99 3.09) on plane sets whose cards are single-printing, and a mean cannot fix a
        // spread. One pass over the set bits is exact and is paid only here — `Prefer::Default`, ~85%
        // of traffic by `REALISTIC_PREFER_WEIGHTS`, returns before reaching it.
        let scan_units = if matches!(prefer, Prefer::Default) {
            count
        } else {
            let mut span = 0u64;
            for (w, word) in plane_bits.iter().enumerate() {
                let mut bits = *word;
                while bits != 0 {
                    let cid = w * 64 + bits.trailing_zeros() as usize;
                    span += u64::from(u32::from(offsets[cid + 1]) - u32::from(offsets[cid]));
                    bits &= bits - 1;
                }
            }
            span.min(u64::from(n_printings)) as u32
        };
        (mk_plan_feats(ctx, params, count, count, scan_units, 0), Prep::Plane)
    } else if PhysicalPlan::CardRangePopcount.applicable(ctx, params, filter, unsplit, plane) {
        // Exact in-range printing count `k` from the index partition points (two binary searches, no
        // scan, no scatter). The O(k) card-bitmap build is deferred to dispatch and paid only if this
        // plan wins — so a competing winner never eats a wasted build (re-deriving the bounds there is
        // another ~free binary search). `k` rides `synth_printings` (the deferred scatter); `matches`
        // uses the card-count proxy `min(k, n_cards)` (card total ≤ both). The materializing
        // alternatives are costed with the range's verify tier (a `0` would under-cost them).
        let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("applicable ⇒ bare range");
        let (s, e) = idx.range(lo, hi);
        let k = (e - s) as u32;
        // Exact distinct cards from the per-value table when it can answer this shape — every op but
        // `Eq` is one-sided, and `Eq` is a single value, so the only fallback is `year:Y`, which spans
        // a whole year of release dates. The `k.min(n_cards)` proxy it falls back to over-estimates a
        // median 1.49x (docs/issues/done/local-engine-range-cardinality-estimate.md).
        //
        // A FUSED two-sided range never arrives here at all: `bare_range_bounds` gates this branch and does
        // not match `And`, so `usd>=a usd<=b` is declined upstream by `exact_result_total` in every mode and
        // its card/artwork totals come from compose's projection instead. Both interior-interval shapes are
        // docs/issues/00853-engine-interior-range-distinct-counts.md.
        let card_est = range_card_counts_for(indexes, idx)
            .and_then(|counts| counts.distinct_cards(lo, hi))
            .unwrap_or_else(|| k.min(n_cards));
        // What the MATERIALIZING alternatives (P3/P4) scan depends on whether dispatch's narrowing
        // survives. `range_narrowed` only hands back an enumerable printing list when the slice is
        // under `MAX_NARROW_FRACTION` of the index; past that it degrades to a printing-space bitmap,
        // which cannot yield card ids, so the scan walks the whole corpus and filters. Costing those
        // plans at `card_est` regardless under-costs the degraded case by the full ratio between the
        // two: measured 31,508 cards / 97,206 printings visited against a `card_est` of 12,450, a
        // 3.2x gap no rate constant can absorb. The sibling `PrintingRangeScan` branch below assumes
        // the opposite (always unnarrowed) and its cells agree to within 1% -- this makes both exact.
        let (eval_domain, scan_units) = if range_too_broad_to_narrow(k as usize, idx.len()) {
            (n_cards, n_printings)
        } else {
            (card_est, card_est)
        };
        let mut feats = mk_plan_feats(ctx, params, card_est, eval_domain, scan_units, verify_cost_tier(filter));
        // `k` rides `scatter_printings`: this plan's arm charges it as its FUSED one-pass build
        // (`CARD_RANGE_BUILD_PER_PRINTING_NS`), while a competing PrintingCompose costed off these shared
        // feats charges the same `k` as its cheaper scatter (`RANGE_SCATTER_…`) plus a separate
        // `project` pass — so the fused op wins the argmin and a bare range doesn't mis-route to compose.
        feats.scatter_printings = k;
        feats.project_printings = k;
        feats.compose_scan_printings = k;
        feats.compose_paging = compose_paging_for(indexes, cards.len(), filter, mode, sort_col, descending);
        (feats, Prep::Range(CountSource::CardRangePopcount))
    } else if PhysicalPlan::PrintingRangeScan.applicable(ctx, params, filter, unsplit, plane) {
        // Bare range: exact k from the index (no scan).
        let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("applicable ⇒ bare range");
        let (s, e) = idx.range(lo, hi);
        let k = (e - s) as u32;
        // What the MATERIALIZING alternatives see, by the same test the sibling `CardRangePopcount`
        // branch already applies: `range_narrowed` hands back an enumerable printing list only while
        // the slice stays under `MAX_NARROW_FRACTION`, and degrades to a printing-space bitmap past
        // it, which cannot yield card ids so the scan walks the whole corpus.
        //
        // This branch used to assume the degraded case ALWAYS, on the grounds that a narrow range
        // makes P1 lose and dispatch materializes anyway. The sibling's comment went further and
        // claimed the two "agree to within 1%". They agree at the MEDIAN and nowhere else: measured
        // against `cards_visited`, `eval_domain` here reads 1.00 at p50 but 4.23 at p70 and 41.7 at
        // p90, because a third of these queries do narrow. (The p100 of 315 is the harness's
        // MIN_COUNTER floor, not the real maximum -- 31,508/100.)
        let (eval_domain, scan_units) =
            if range_too_broad_to_narrow(k as usize, idx.len()) { (n_cards, n_printings) } else { (k.min(n_cards), k) };
        let mut feats = mk_plan_feats(ctx, params, k, eval_domain, scan_units, verify_cost_tier(filter));
        feats.compose_scan_printings = k;
        feats.scatter_printings = k; // for costing a competing PrintingCompose (which would scatter k); P1 itself walks, so its own cost ignores this
        // Also for costing that competing compose: `eval_domain`/`scan_units` above are the
        // unnarrowed universe (right for P3/P4, which is what they are there for), so leaving
        // `compose_paging` at its `Gather` default charged compose a full-corpus gather it would
        // never run. Compose's page term only reads `eval_domain` in the Gather branch.
        feats.compose_paging = compose_paging_for(indexes, cards.len(), filter, mode, sort_col, descending);
        (feats, Prep::Range(CountSource::PrintingRangeScan))
    } else if PhysicalPlan::PrintingCompose.applicable(ctx, params, filter, unsplit, plane) {
        // Composable printing-space expr, any distinct-on. Estimate the counts cheaply — the fast path
        // composes once, only if this plan wins (never in acquire; a legality broadcast paid here and
        // then discarded would be pure waste). `synth_printings` = broadcast down (legality) + projection
        // up (card/artwork; 0 for printing). `popcount_words` = the result-space bitmap the total scans.
        //
        // Estimated from the SAME predicate the executor will compose (`compose_source`), not from the
        // residual. Reading the residual once a plane had consumed the filter estimated `True` — every
        // printing in the corpus — so `matches` came back as `n_printings` for every legality query alike
        // and compose was costed as if it returned everything for nothing. Applicability, estimate and
        // execution have to agree on which representation this plan is being judged on.
        let composed = compose_source(filter, unsplit, plane);
        let est = compose_printing_estimate(composed, indexes, offsets, n_printings as usize);
        let (printing_matches, broadcast, scatter) = (est.result, est.broadcast, est.scatter);
        // Two build kinds, charged at different rates: `broadcast` = legality broadcast-down (linear
        // pass), `scatter` = range-slice scatter (cheap). `project` = the second pass (printing→
        // card/artwork), 0 for printing mode. Keeping all three separate is what lets a bare range's
        // CardRangePopcount acquire (which sets `scatter`/`project` too) cost this plan's passes
        // honestly against the fused build. `eval_domain`/`scan_units` cost the *materializing
        // alternatives* should compose lose: a composable filter narrows via its indices, so they see
        // ~`matches` candidates (card mode also breaks at the first match ⇒ scan_units = eval_domain) —
        // the unnarrowed universe would over-cost them and mis-route (measured: `format:X format:Y`/card).
        // EXACT distinct cards when the composed filter is a single one-sided range: the boundary
        // table answers prefix/suffix/Eq shapes exactly, which beats any projection. It declines
        // genuinely interior ranges, so a two-sided `usd>=a usd<=b` still falls back to the
        // projection -- and those are the bulk of what reaches here, since `bare_range_bounds`
        // matches one comparison, not an And of two. One-sided ranges DO land here in quantity:
        // every artwork-mode one, plus card-mode ones whose orderby has no permutation.
        // Two quantities, deliberately separate. `exact_cards` is CARD space no matter the query's
        // mode, because `eval_domain`, `scan_all` and the artwork capacity all consume a card count.
        // `exact_total` is the answer in the query's OWN mode, and is what `result_total` wants.
        // Conflating them puts an artwork count where cards are expected in artwork mode.
        let exact_cards = exact_result_total(composed, indexes, Mode::Card);
        let exact_total = if matches!(mode, Mode::Card) {
            exact_cards
        } else {
            exact_result_total(composed, indexes, mode)
        };
        let est_cards =
            exact_cards.unwrap_or_else(|| calibrated_balls_into_bins(printing_matches, n_cards as usize));
        // The card count the MATERIALIZING alternatives walk, which stops being `est_cards` once the
        // estimate has been tightened. `est.candidate` is the untightened `min` over single leaves, and
        // that is what narrowing actually leaves them: it declines broad children (`border:black` at 87%
        // under `broad_ok: false`), so `border:white border:black` hands them `border:white`'s 5,131
        // printings, not the empty intersection. Charging the intersection priced `GatheredScan` at
        // 0.2 us against a measured 199.3 us -- a plan still has to scan to DISCOVER a set is empty.
        //
        // Identical to `est_cards` whenever nothing was tightened, which is every query that reached here
        // before the pair table existed, so no already-calibrated cell moves.
        let domain_cards = if est.candidate == est.result {
            est_cards
        } else {
            calibrated_balls_into_bins(est.candidate, n_cards as usize)
        };
        // What the MATERIALIZING alternatives scan if compose loses. Every mode narrows -- a
        // composable filter has an index for every leaf -- so all three are the NARROWED counts.
        // Printing mode took the unnarrowed universe while card/artwork took a narrowed count; only
        // one could be right, and the counters say narrowed: printing mode visited 0.08x the claimed
        // `eval_domain` and scanned 0.14x the claimed `scan_units`. Over-costing both plans inflates
        // the predicted GAP between them (P4 carries the larger per-row rates), which is what routing
        // reads -- measured as a GatheredScan-vs-StreamedSelect gap ratio of 0.32 on this acquire.
        // Clamped at `n_printings`, which is an INVARIANT and not a calibration: this estimates the
        // printings under the candidate CARDS, and those are a subset of the corpus, so a value above
        // `n_printings` is not a wrong estimate but an impossible one. `COMPOSE_CANDIDATE_SPAN_BIAS`'s 2.1
        // says candidates are more reprinted than an average card, which is true when a composable
        // predicate SELECTS by having a matching printing and false when it selects nearly everything —
        // the same saturation failure `COMPOSE_CARD_ESTIMATE_BIAS` had, one multiplier downstream.
        //
        // `border:black` / printing reached 159,325 against a corpus of 97,206 and a realized
        // `printings_examined` of exactly 97,206. The clamp makes that cell exact. It matters for routing
        // because this feature is 76% of P3's arm on the broad-residual class, where it drove P3 to
        // pred/meas 1.53 while P4 sat at 0.88 — the pair inverted, with both plans over the same feature.
        let scan_all =
            |cards: usize| (((cards as f64) * printings_per_card * COMPOSE_CANDIDATE_SPAN_BIAS) as usize).min(n_printings as usize);
        let (result_total, project, popcount_words, eval_domain, scan_units) = match mode {
            Mode::Printing => {
                // `exact_total` for the RESULT, `printing_matches` for everything else. They are not the
                // same quantity: `printing_matches` proxies the size of the bitmap compose BUILDS, which
                // for a legality leaf is every printing of every existentially-legal card -- a superset
                // that the residual then filters. The cost features are calibrated against that superset
                // (`printings_examined`, `printing_span`), so substituting the true match count there
                // would under-charge the scan on exactly the divergent-legality cards the superset exists
                // for. `f:modern` reads 68,687 against a true 73,783; `banned:modern` 160 against 399.
                let total = if *EXACT_VALUE_TOTALS { exact_total.unwrap_or(printing_matches) } else { printing_matches };
                (total, 0, (n_printings as usize).div_ceil(64), domain_cards, scan_all(domain_cards))
            }
            Mode::Card => {
                // Card mode's result total IS the distinct-card count, which is precisely what
                // `est_cards` already holds. The estimated fallback used to be the saturating
                // `printing_matches.min(n_cards)`, which reads a median 1.99x the deduped
                // `matches_pushed` counter -- p10 1.01, so it is over on nearly every query. Two
                // names for one quantity, one of them wrong.
                (est_cards, printing_matches, (n_cards as usize).div_ceil(64), domain_cards, scan_all(domain_cards))
            }
            Mode::Artwork => {
                // `result_total` is consumed as a per-RESULT count (GatheredScan's push term,
                // StreamedSelect's emit term), and `matches_pushed` is deduped: `f:modern`/artwork
                // pushes 34,285, not the 68,687 printings it scans. Handing over the printing count
                // over-charged every materializing alternative by ~2x on this acquire. The printing
                // count stays where it belongs -- `project` (the printing->artwork pass) and
                // `scan_units` both walk printings, as `printing_span` confirms.
                let n_artworks = u32::from(*indexes.artwork_base.last().expect("artwork_base has n_cards+1 entries")) as usize;
                // The UNCALIBRATED card count feeds the artwork capacity on purpose.
                // `COMPOSE_CARD_ESTIMATE_BIAS` was measured against `cards_visited` -- distinct CARDS
                // -- and `artwork_estimate` does not consume a card count as an answer, it uses it to
                // size a capacity (`est_cards * n_artworks/n_cards`) that a second balls-into-bins then
                // draws into. Feeding the calibrated value shrank both stages and moved
                // `matches <Gather> / artwork` from 0.97 to 0.84, i.e. it made an already-good cell
                // worse. Under-charging compose's push term is what over-picks it, and compose is
                // over-picked in artwork specifically: that slice carries 21% of ALL routing regret at
                // p99 205us against 40us for printing and 36us for card.
                let capacity_cards = exact_cards.unwrap_or_else(|| balls_into_bins(printing_matches, n_cards as usize));
                // The two-stage estimate is only reached when nothing exact is available. A bare
                // one-sided range now answers artwork exactly from the range table's artwork column,
                // which is the one space every such query used to estimate (0.80-0.87 measured).
                let rt = exact_total.unwrap_or_else(|| artwork_estimate(printing_matches, capacity_cards, n_cards as usize, n_artworks));
                // The bitmap `printing_bits_to_artwork_bits` popcounts is n_artworks bits wide, not
                // n_printings -- 46,112 against 97,206 here, so this was 2.1x over as well.
                (rt, printing_matches, n_artworks.div_ceil(64), domain_cards, scan_all(domain_cards))
            }
        };
        // `eval_domain` and `scan_units` describe what the MATERIALIZING alternatives walk, and when the
        // narrowing does not shrink the candidate set they walk the whole corpus — at which point these are
        // not badly estimated, they are estimating the wrong QUANTITY. `est_cards` is a count of MATCHING
        // cards; `cards_visited` counts CANDIDATES, a superset whenever the narrowing is inexact. Measured
        // against P4's `cards_visited` over 2,904 compose rows, the distribution is bimodal — 34% of rows
        // visit every card — and on those rows:
        //
        //     est_cards            p10 0.43   p50 0.65   p90 0.83   mean |log| 0.454
        //     n_cards              p10 1.00   p50 1.00   p90 1.00   mean |log| 0.000
        //
        // Exact by construction, not calibrated. Overall mean |log| 0.370 -> 0.216. Both plans visited the
        // SAME card count on 100% of rows, which is also why this feature does not want splitting per plan.
        //
        // Predicted with the predicate and the constant the sibling `PrintingRangeScan` branch already uses
        // for the identical decision — no new constant. Scored against the realized flag, `printing_matches`
        // over `MAX_NARROW_FRACTION` (0.25) of `n_printings` catches **98%** of full-scan rows at 87%
        // accuracy, beating every threshold on the two alternative signals tried. Its 26% false positives
        // over-cost both materializing plans by the same factor, which an argmin largely absorbs; the false
        // negatives are what were losing `GatheredScan vs StreamedSelect`, so recall is the side to favour.
        let (eval_domain, scan_units) = if range_too_broad_to_narrow(printing_matches, n_printings as usize) {
            (n_cards as usize, n_printings as usize)
        } else {
            (eval_domain, scan_units)
        };
        // The tier is what the MATERIALIZING alternatives pay per candidate, so it must be asked about
        // the predicate THEY see (`filter` + `plane`), not about `composed` — and gated exactly as
        // `prepare_candidates` gates it, or the router charges a `card_pass` the kernels will skip. On a
        // card-invariant legality format `card_pass` resolves at card level for every card, so
        // `printings_examined` reads 0 and both the per-card residual and the per-row scan are dead
        // terms; charging them anyway was 92-94% of P3's predicted cost on `f:modern`, `f:gladiator`,
        // `f:commander` and `f:predh`. `residual_exact` is unavailable here (this branch never narrows),
        // so this is the conservative half of the executor's disjunction: it can over-charge, never under.
        let nothing_to_verify = plane_leaves_nothing_to_verify(filter, mode, plane, indexes);
        let tier = if nothing_to_verify { 0 } else { verify_cost_tier(composed) };
        // `GatheredScan` walks every printing of every candidate card, so its scan feature is the candidate
        // SPAN. `scan_all` estimates that span as `est_cards x` the corpus-average printings-per-card `x 2.1`,
        // which is the right shape only when candidates are an average sample. With nothing to verify they
        // are not: every printing of a matching card matches, so the span IS `printing_matches` — exact,
        // and already computed above for compose's own estimate. Graded against P4's realized
        // `printings_examined` over 597 card-invariant compose queries:
        //
        //     scan_units (shipped)   p10 1.13   p50 1.76   p90 5.08
        //     printing_matches       p10 0.68   p50 0.93   p90 3.08
        //
        // A 1.76x over-charge on the dominant term of P4's arm, which is what makes P3 win where P4 is
        // better: `StreamedSelect -> GatheredScan` is the largest regret slice on this acquire. Scoped to
        // the same boolean as the tier because the grading inverts on the other population — with a real
        // residual `scan_units` is right at p50 0.97 and `printing_matches` badly under at 0.39.
        //
        // Fixes the BIAS, not the spread: both rows read p90/p10 4.5, so what remains is the candidate
        // count's own variance (`eval_domain` grades p90/p10 3.1 here) and is not a scan-feature problem.
        let scan_units = if nothing_to_verify { printing_matches } else { scan_units };
        let mut feats = mk_plan_feats(ctx, params, result_total as u32, eval_domain as u32, scan_units as u32, tier);
        // What `StreamedSelect` actually examines here, which is NOT `scan_units`. P4 walks a card's whole
        // span to push every match; P3's `card_match_count` answers from span arithmetic for every card
        // `card_pass` resolves at card level, and on a legality-composed filter that is every non-divergent
        // card -- 556 of 31,508 in production diverge, all in `oldschool`. So P3 examines printings only for
        // the divergent remainder plus the boundary cards, measured at 0.10-0.26x of P4's count where
        // `scan_units` claimed parity: 7,770 against 73,783 on `f:modern`, 5,876 against 54,213 on
        // `f:gladiator`.
        //
        // Estimated as the divergent SHARE of the candidate span rather than a fitted fraction, because the
        // engine holds the set: `legal_divergent`. For a filter with no legality leaf the share is 1.0 and
        // this reduces to `scan_units`, which the same sweep showed is right to 1.0-2.4x on `border:black`,
        // `r:mythic` and `watermark:*` -- so the correction is confined to the case that measured wrong.
        feats.stream_scan_units = if tier == 0 {
            // Nothing to verify: `card_match_count` answers every card from span arithmetic and examines
            // no printings whatsoever. Reported as 0 so `bench_feature_accuracy` grades this against the
            // realized `printings_examined` (also 0) instead of against a scan that never happens. The
            // arm multiplies the term by zero on the same signal, so this changes no cost — only whether
            // the feature can be graded honestly.
            0
        } else if filter_touches_legality(composed) && !(*LEGALITY_SCAN_SCOPE && touches_printing_field(composed)) {
            // `&& !touches_printing_field` because the argument below is about what `card_pass` can
            // SETTLE, and legality being card-level is only decisive when it is the only thing left to
            // verify. One printing-varying partner -- `border:white`, a range, a frame value -- makes
            // `card_pass` return `PrintingDep` for every card, and P3 then walks the whole span like P4.
            //
            // Scoped on legality alone, the correction charged 2,755 for the entire `f:X border:white`
            // family against a realized 5,353-19,737, up to 7.2x under, and handed every one of them to
            // StreamedSelect: `f:modern border:white` measured 100.9 us on the plan the router picked
            // against 44.3 us for the PrintingCompose it passed over. `Legality` reads false from
            // `touches_printing_field` (it ranks by the common card-level case), so this composes
            // cleanly -- a bare legality filter still takes the divergent-share arm.
            let divergent = indexes.legal_divergent.len() as f64;
            let share = (divergent / f64::from(n_cards)).min(1.0);
            // Floored at one printing per candidate: with a divergent format in play the kernel does
            // examine the boundary printing of the cards it matches, which the share alone would put at
            // zero. Only reachable now when there IS something to verify, which is the case the floor was
            // argued for -- the `tier == 0` arm above is where it used to be wrong.
            ((scan_units as f64) * share).max(eval_domain as f64) as u32
        } else {
            scan_units as u32
        };
        feats.broadcast_printings = broadcast as u32;
        feats.scatter_printings = scatter as u32;
        feats.project_printings = project as u32;
        feats.popcount_words = popcount_words as u32;
        feats.compose_scan_printings = (printing_matches as f64 * COMPOSE_GATHER_SPAN_PER_MATCH) as u32;
        // The gather's grouping arm runs for artwork always, and for card only under a non-default
        // prefer -- card/default takes the early-break arm and never groups. Printing mode gets 0
        // because its push term already rides the printing count. Driven by the PRE-dedup printing
        // matches, which is the whole point: see `PlanFeatures::gather_group_printings`.
        let groups = match mode {
            Mode::Artwork => true,
            Mode::Card => !matches!(prefer, Prefer::Default),
            Mode::Printing => false,
        };
        feats.gather_group_printings = if groups { printing_matches as u32 } else { 0 };
        // Which paging strategy the fastpath will actually use — decided the same way the fastpath
        // itself decides, through the same helpers, including whether it will decline. Only this
        // branch knows the estimated result total, so only it can predict the small-total bail. The
        // gather gates cost an estimate each, so ask them only when the gather branch is the one that
        // would run.
        let no_perm = indexes.sort_perms.get(sort_col, descending).is_none_or(|p| p.len() != cards.len());
        // Asked whenever there is no permutation. It used to be skipped when a printing-mode walk was
        // available, on the grounds that only the gather branch reads it -- but the walk is available in
        // every mode now and `compose_paging_with_total` needs the verdict to predict a decline, so the
        // narrower guard would silently predict `Gather` for queries that decline.
        let gather_declines = no_perm
            .then(|| compose_gather_declines(filter, indexes, offsets, ctx.printings, cards, mode))
            .flatten();
        feats.compose_paging =
            compose_paging_with_total(indexes, cards.len(), composed, mode, sort_col, descending, Some(result_total), gather_declines);
        (feats, Prep::Range(CountSource::PrintingCompose))
    } else {
        let prep = prepare_candidates(ctx, params, filter, plane);
        let feats = candidate_feats(ctx, params, &prep, filter);
        (feats, Prep::Candidates(prep))
    };

    (feats, prep, plane_bits)
}

/// #702: the single cost-based plan-selection layer for ALL unique modes — the
/// whole of `run_query`'s dispatch (the hand-tuned decision tree it replaced is
/// gone). Three linear steps, no early returns:
///
/// 1. **acquire** — pick the query's *count source* (one of three, by structure)
///    and build the cost features, materializing the shared artifact it implies:
///    a True-residual plane's popcount (`Prep::Plane`), a bare range's index-`k`
///    (`Prep::Range`, nothing materialized), or `prepare_candidates`
///    (`Prep::Candidates`). This 3-way is the engine's entire materialization story.
/// 2. **choose** — `argmin cost::plan_cost` over `ALL.filter(applicable)`, narrowed to the plans
///    step 3's arm for this prep can execute (`Prep::scope`). No hand-written plan list;
///    applicability encodes prep availability, so the right candidates fall out per acquire branch.
/// 3. **dispatch** — run the winner, reusing the acquired artifact.
///
/// Plan choice is a pure performance decision — every plan returns identical rows
/// (guaranteed by `force_plan_differential_agreement`). Adding a plan is declaring
/// its `applicable`/`cost`/`PlanScope`/executor arms; only a genuinely new
/// count source (a new `Prep`) touches acquire/dispatch. The one subtlety is
/// `Prep::Range`: it costs `PrintingRangeScan` (non-materializing) from a cheap
/// estimate, so if a *materializing* plan wins there — or `PrintingRangeScan` wins
/// but its fastpath declines — dispatch materializes lazily and re-chooses on exact
/// features. That deferral is the "don't pay to materialize a plan you won't run".
fn run_query_routed<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &mut FilterExpr,
    unsplit: Option<&FilterExpr>,
    plane: Option<&PlaneExpr>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    // Generic argmin: the cheapest applicable plan the caller's dispatch arm can run. `filter` is
    // passed per call (not captured) so it stays free for `prepare_candidates`'s `&mut`. `scope`
    // narrows `ALL` to the plans that arm has an executor for — see `PlanScope`, and note that
    // `applicable` alone does NOT imply runnable-here. GatheredScan is applicable to every query
    // and admitted by every scope → the min is never empty.
    let choose = |filter: &FilterExpr, feats: &cost::PlanFeatures, scope: PlanScope| -> PhysicalPlan {
        PhysicalPlan::ALL
            .into_iter()
            .filter(|p| p.applicable(ctx, params, filter, unsplit, plane) && scope.admits(*p))
            .min_by(|a, b| cost::plan_cost(*a, feats).partial_cmp(&cost::plan_cost(*b, feats)).expect("plan_cost is finite"))
            .expect("GatheredScan is always applicable and in every scope")
    };

    // Marks three disjoint phases covering the whole call — see `RoutedPhases` for why acquire needed
    // measuring from inside one execution rather than as its own `explain_analyze` participant, and
    // why it is behind a feature. Compiles to nothing without `routed-phases`.
    let mut phases = RoutedPhaseTimer::start();

    // ── acquire: pick the count source, build features, materialize its artifact ──
    let (feats, prep, plane_bits) = acquire_plan_features(ctx, params, filter, unsplit, plane);
    phases.acquired();

    // ── choose: cheapest applicable plan this acquire's dispatch arm can run ──
    let plan = choose(filter, &feats, prep.scope());
    phases.chosen();

    // ── dispatch: run the winner, reusing the acquired artifact ──
    // Bound, not returned directly, so the closing clock read happens before the result is handed
    // back and the phase covers dispatch alone. The match has no early returns, so this one exit is
    // the only place the phases can be published from.
    let out = match (plan, &prep) {
        (PhysicalPlan::PlanePopcountOrder, Prep::Plane) => {
            exec_plane_popcount_order_with_bitmap(ctx, params, plane.expect("Prep::Plane ⇒ plane"), &plane_bits)
        }
        // P3/P4 reuse the plane bitmap as their candidate list — identical to what
        // prepare_candidates yields for a True-residual plane query.
        (p, Prep::Plane) => exec_from_candidates(
            CandidatePlan::of_or_gathered(p), ctx, params, filter,
            // `None`, matching what `Prep::narrowed_repr` reports for a plane acquire: the field
            // means "what the residual NARROWING produced", and no narrowing ran here — the list
            // came from the plane bitmap. Diagnostic-only and unread on this path, but `CardBits`
            // here would be the one value that contradicts `explain`'s contract if it ever were.
            &PreparedCandidates { candidate_cards: Some(bitmap_card_ids(&plane_bits)), all_match_known: true, proven_conjuncts: 0, narrowed_repr: NarrowedRepr::None },
            plane,
        ),
        (p, Prep::Candidates(prep)) => exec_from_candidates(CandidatePlan::of_or_gathered(p), ctx, params, filter, prep, plane),
        // `Prep::Range` = "cheap estimate acquired, nothing materialized" — shared by CardRangePopcount
        // (#725), PrintingRangeScan (#695), and PrintingCompose (#724). Each winner does its own O(k)
        // work here, so no plan eats a build for a competing winner:
        //   - CardRangePopcount builds its card bitmap from the (re-derived, ~free) range bounds now.
        //   - the printing-space fast paths walk (or, if they decline — sparse total — materialize).
        //   - a materializing plan (StreamedSelect/GatheredScan) that beat them narrows + runs.
        (PhysicalPlan::CardRangePopcount, Prep::Range(_)) => {
            let (idx, lo, hi) = bare_range_bounds(filter, ctx.indexes).expect("applicable ⇒ bare range");
            // Timed and handed to the executor as `ns_prepare`: this build happens in DISPATCH on
            // both paths, so it belongs to the plan's cost, and `card_range_popcount` being a
            // RANGE_ACQUIRE is what tells `plan_self_ns` to add it rather than exclude it.
            let t_build = std::time::Instant::now();
            let (card_bits, range_pbits) =
                build_card_range_bits(idx, lo, hi, ctx.indexes, ctx.cards.len(), ctx.printings.len());
            note_pending_prepare_ns(t_build.elapsed().as_nanos() as u64);
            exec_card_range_popcount(ctx, params, &card_bits, &range_pbits)
        }
        (plan, Prep::Range(_)) => {
            let fast_page = match plan {
                PhysicalPlan::PrintingRangeScan => printing_range_fastpath(ctx, params, filter),
                PhysicalPlan::PrintingCompose => printing_compose_fastpath(ctx, params, compose_source(filter, unsplit, plane)),
                _ => None, // a materializing plan won the estimate — materialize + run it below
            };
            match fast_page.or_else(|| declined_sibling_fastpath(plan, ctx, params, filter, unsplit, plane, &feats, &choose)) {
                Some(page) => page,
                None => {
                    let prep = prepare_candidates(ctx, params, filter, plane);
                    let feats = candidate_feats(ctx, params, &prep, filter);
                    // `PlanScope::Candidates`, not the old "materializing plans", which admitted
                    // `PlanePopcountOrder` too — and this path holds a candidate list, not the
                    // plane bitmap that plan reads. Re-chosen on a filter `prepare_candidates` has
                    // just rewritten in place, so the applicable set here is not the one acquire
                    // saw and the scope is what states which of it this arm can run.
                    let plan = choose(filter, &feats, PlanScope::Candidates);
                    exec_from_candidates(CandidatePlan::of_or_gathered(plan), ctx, params, filter, &prep, plane)
                }
            }
        }
    };
    phases.finish();
    out
}

/// In-process force/dispatch entry point (#702 step 2): run `plan` for this
/// query if it is applicable, else return `None`. This is the prerequisite for
/// the calibration harness — it makes each physical plan individually
/// executable without changing `run_query`'s default routing. Returns `None`
/// when `plan` fails its applicability predicate (or, for `PrintingRangeScan`,
/// when `printing_range_fastpath` structurally declines with `None`); `Some`
/// with the result when it ran. `GatheredScan` is always `Some`. Also the
/// executor `explain_analyze` (#745) drives per plan per timing round.
fn run_query_with_plan<'a>(
    plan: PhysicalPlan,
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &mut FilterExpr,
    unsplit: Option<&FilterExpr>,
    plane: Option<&PlaneExpr>,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    let QueryCtx { cards, indexes, .. } = *ctx;
    let QueryParams { mode, sort_col, descending, .. } = *params;

    match plan {
        PhysicalPlan::PrintingRangeScan => {
            if !printing_range_scan_applicable(mode, plane, cards) {
                return None;
            }
            // Structural eligibility passed; the fastpath itself decides (None = declined).
            printing_range_fastpath(ctx, params, filter)
        }
        PhysicalPlan::PrintingCompose => {
            if !printing_compose_applicable(filter, unsplit, cards, plane, indexes) {
                return None;
            }
            // The fastpath composes, projects per mode, and walks — or declines (None) on a sparse
            // total, exactly as under the router.
            printing_compose_fastpath(ctx, params, compose_source(filter, unsplit, plane))
        }
        PhysicalPlan::PlanePopcountOrder => {
            if !plane_popcount_order_applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
                return None;
            }
            let plane_expr = plane.expect("applicability guarantees a plane");
            Some(exec_plane_popcount_order(ctx, params, plane_expr))
        }
        PhysicalPlan::CardRangePopcount => {
            if !card_range_popcount_applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
                return None;
            }
            let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("applicability guarantees a bare range");
            // Timed exactly as the routed path times it, so a forced trial and dispatch contain the
            // same work; see the routed call site.
            let t_build = std::time::Instant::now();
            let (card_bits, range_pbits) = build_card_range_bits(idx, lo, hi, indexes, cards.len(), ctx.printings.len());
            note_pending_prepare_ns(t_build.elapsed().as_nanos() as u64);
            Some(exec_card_range_popcount(ctx, params, &card_bits, &range_pbits))
        }
        PhysicalPlan::StreamedSelect => {
            if !streamed_select_applicable(cards, sort_col, descending, indexes) {
                return None;
            }
            let prep = timed_prepare_candidates(ctx, params, filter, plane);
            Some(exec_streamed_select(ctx, params, filter, &prep, plane))
        }
        PhysicalPlan::GatheredScan => {
            debug_assert!(gathered_scan_applicable());
            let prep = timed_prepare_candidates(ctx, params, filter, plane);
            Some(exec_gathered_scan(ctx, params, filter, &prep, plane))
        }
    }
}

/// One applicable plan's predicted cost, as `explain` (#745) reports it — exposing
/// the numbers `run_query_routed`'s `choose` step already computes and discards,
/// via the identical `acquire_plan_features` acquire step so this can never
/// silently drift from what the real router would pick.
pub(crate) struct PlanEstimate {
    pub(crate) plan: PhysicalPlan,
    pub(crate) predicted_ns: f64,
    /// `cost::materialize_cost` for this plan: the modelled `collect` + `sort_unstable` cost of the
    /// candidate list it consumes — the candidate-production term `plan_cost` omits. Reported but
    /// deliberately NOT added to `predicted_ns`; `0.0` for plans that build no candidate list. See
    /// `cost.rs`'s "Candidate materialization" section for why it stays out of the routing decision.
    ///
    /// Charged on `eval_domain`, which is the candidate count only under a `Candidates` acquire.
    /// Under `Prep::Range` the two materializing plans are estimated UNNARROWED
    /// (`eval_domain = n_cards`), so this figure has no referent there -- do not pool range-acquired
    /// rows with candidate-acquired ones when reading it.
    pub(crate) materialize_ns: f64,
    /// Whether this is the plan `run_query_routed` would run: the cheapest `predicted_ns` among the
    /// plans this acquire's dispatch arm can execute (`Prep::scope`). That is index 0 after the
    /// ascending sort for a `Prep::Range` acquire, whose arm runs everything, but not necessarily
    /// for the other two — the ranking below still lists every applicable plan, including ones the
    /// router could not reach. Reported explicitly so a caller never has to reconstruct the argmin —
    /// doing that over only the plans that *ran* (dropping runtime decliners), or over the full
    /// ranking without the scope, silently diverges from what the router picks.
    ///
    /// One documented exception it cannot capture: for a `Prep::Range` acquire the router may
    /// re-materialize and re-choose at dispatch, so the executed plan can still differ. See the
    /// free `explain` fn's doc.
    pub(crate) picked: bool,
}

/// What the acquire step itself did, which is per-QUERY rather than per-plan: every
/// plan in the same `explain` call shares one of these. `plan_cost` prices only what
/// happens *after* acquire — `eval_domain` and `matches` are its inputs, not its
/// outputs — so a change to how candidates get materialized moves `acquire_ns` and
/// leaves every `predicted_ns` untouched. That divergence is the point: it isolates
/// the one term the cost model does not carry.
pub(crate) struct AcquireFacts {
    /// The full feature vector `cost::plan_cost` consumed for this query. Reported so a calibration
    /// sweep can regress measured time on *exactly* the terms the model uses; fitting against
    /// re-derived proxies instead means a feature error hides as a coefficient that will not settle.
    pub(crate) feats: cost::PlanFeatures,
    /// Which of `Prep`'s three count sources this query's structure selected. Only
    /// `"candidates"` materializes a candidate list at all, so it is the first thing
    /// to check before treating a query as a test case for materialization work.
    pub(crate) count_source: CountSource,
    /// `Candidates::repr` of what the narrowing produced — `cards`/`printings` mean a
    /// sorted vec was built (some site ran a `collect` + `sort_unstable`), `card_bits`/
    /// `printing_bits` mean it stayed word-wise. `none` means the residual narrowing produced
    /// nothing: either no candidate list at all (`range`/`plane` acquire), or a candidate
    /// acquire whose list came from the plane bitmap alone. Both are "no sort happened".
    ///
    /// Needed because a candidate *count* in a given band does not imply a sort was paid to
    /// reach it: a plane AND'd with a range lands at the same count without sorting anything.
    ///
    /// A top-of-narrowing proxy, not a per-site census — see `Candidates::repr`.
    pub(crate) narrowed_repr: NarrowedRepr,
    /// Raw per-sample wall time of the acquire step — narrowing and any
    /// materialization included. Not pre-reduced, same rationale as
    /// `PlanTrial::trials_ns`. `explain` reports a single sample; `explain_analyze`
    /// reports one per round.
    ///
    /// Every sample deliberately pays the one-time `memoize_text_predicates`
    /// rewrite, because each is taken from a pristine filter clone (see
    /// `explain_analyze`'s doc for why that fairness discipline matters). So for a
    /// text-predicate query this OVERSTATES what a warm repeated query spends in
    /// acquire, by that one-time cost — it is a fair number to compare across
    /// queries, not an end-to-end latency component.
    ///
    /// Do NOT subtract this from a materializing plan's `trials_ns` to isolate that plan's
    /// execution from the narrowing. `PhaseStats::ns_prepare` does that job properly: it times
    /// `prepare_candidates` INSIDE the plan's own run, so `ns_round_total - ns_prepare` is two
    /// numbers from one execution, with no cross-run cache-state assumption to be wrong about.
    /// Subtraction here would also be measuring the wrong quantity — `acquire_plan_features` is
    /// `prepare_candidates` PLUS cost-feature construction (`mk_plan_feats`,
    /// `compose_printing_estimate`, `verify_cost_tier`), and on a range or compose acquire it skips
    /// `prepare_candidates` entirely.
    ///
    /// What it is good for is the thing nothing else measures: the router's own pre-dispatch
    /// overhead. `run_query_with_plan` forces a plan and never runs the argmin, so no `trials_ns`
    /// contains any of it.
    pub(crate) acquire_ns: Vec<u64>,
    /// Raw per-round wall time of `run_query_routed` — the whole real path: acquire, choose,
    /// dispatch, *and* the lazy re-materialize-and-re-choose a `Prep::Range` query can do at
    /// dispatch. Empty from `explain`, which runs nothing.
    ///
    /// This is the row that decides whether a ranking error is a live defect. `explain_analyze`'s
    /// per-plan `trials_ns` come from `run_query_with_plan`, which forces a plan and therefore
    /// bypasses that re-choose; so "the picked plan is slower than the best plan" does not by
    /// itself mean the engine runs the slow one. Compare against this: routed ≈ best means the
    /// re-choose rescued it, routed ≈ picked means it did not.
    ///
    /// That comparison is inherently cross-execution — there is no in-run equivalent, because a
    /// forced-plan run skips the argmin by construction — so it is the one row that genuinely needs
    /// to be measured on the same terms as `trials_ns`, and it is: same pristine clone per round,
    /// same warmup discard, and drawn from the same shuffled participant pool rather than pinned
    /// ahead of the plan loop. That last part is load-bearing rather than tidiness: this dispatches
    /// into the picked plan's executor, so a fixed position ahead of the plans would pre-warm
    /// exactly the plan whose selection the comparison is meant to question.
    pub(crate) routed_ns: Vec<u64>,
    /// `routed_ns` split into disjoint phases, one triple per round, from INSIDE the same execution —
    /// see `RoutedPhases`. `routed_acquire_ns[i] + routed_choose_ns[i] + routed_dispatch_ns[i]`
    /// accounts for `routed_ns[i]` bar the clock reads.
    ///
    /// Not the same quantity as `acquire_ns`, and the difference is the point. That one times a
    /// standalone `acquire_plan_features` in its own participant round, so it can be compared across
    /// queries but cannot be divided into `routed_ns` — measured, the ratio comes out at 104% of the
    /// median candidate-acquired query, which is how the two got read as a part and a whole.
    pub(crate) routed_acquire_ns: Vec<u64>,
    pub(crate) routed_choose_ns: Vec<u64>,
    pub(crate) routed_dispatch_ns: Vec<u64>,
}

impl Prep {
    /// The `count_source` label `AcquireFacts` reports.
    fn count_source(&self) -> CountSource {
        match self {
            Prep::Range(acquire) => *acquire,
            Prep::Plane => CountSource::Plane,
            Prep::Candidates(_) => CountSource::Candidates,
        }
    }

    /// The `narrowed_repr` label `AcquireFacts` reports. `Range` and `Plane` materialize no
    /// candidate list at all, so they have no narrowing representation to report.
    fn narrowed_repr(&self) -> NarrowedRepr {
        match self {
            Prep::Candidates(p) => p.narrowed_repr,
            Prep::Range(_) | Prep::Plane => NarrowedRepr::None,
        }
    }
}

/// #745 primitive 1: every applicable plan's predicted cost for this query, ranked
/// cheapest first (index 0 is `run_query_routed`'s actual pick). Diagnostic only —
/// this is exactly the acquire+argmin the router runs on every query, just with
/// every candidate kept instead of only the winner, so it costs nothing beyond
/// what a normal query already pays and is safe to call constantly. Note: for a
/// `Prep::Range`-acquired query, the reported cost for a materializing plan
/// (StreamedSelect/GatheredScan) is the same coarse "broad regime" estimate
/// `run_query_routed`'s top-level choose uses — not the more precise number its
/// dispatch would compute if that plan actually won and got lazily re-costed
/// against a materialized candidate list (see `acquire_plan_features`'s
/// `PrintingRangeScan`/`PrintingCompose` branches).
fn explain(
    ctx: &QueryCtx,
    params: &QueryParams,
    filter: &mut FilterExpr,
    unsplit: Option<&FilterExpr>,
    plane: Option<&PlaneExpr>,
) -> (AcquireFacts, Vec<PlanEstimate>) {
    let t0 = std::time::Instant::now();
    let (feats, prep, _plane_bits) = acquire_plan_features(ctx, params, filter, unsplit, plane);
    let acquire_ns = t0.elapsed().as_nanos() as u64;
    let facts = AcquireFacts {
        count_source: prep.count_source(),
        narrowed_repr: prep.narrowed_repr(),
        feats, // moved: `eval_domain`/`n_cards`/`matches` live here and nowhere else
        acquire_ns: vec![acquire_ns],
        routed_ns: Vec::new(), // explain runs nothing
        routed_acquire_ns: Vec::new(),
        routed_choose_ns: Vec::new(),
        routed_dispatch_ns: Vec::new(),
    };
    let mut estimates: Vec<PlanEstimate> = PhysicalPlan::ALL
        .into_iter()
        .filter(|p| p.applicable(ctx, params, filter, unsplit, plane))
        .map(|plan| PlanEstimate {
            plan,
            predicted_ns: cost::plan_cost(plan, &facts.feats),
            materialize_ns: cost::materialize_cost(plan, &facts.feats),
            picked: false, // set below, once the ranking is known
        })
        .collect();
    estimates.sort_by(|a, b| a.predicted_ns.partial_cmp(&b.predicted_ns).expect("plan_cost is finite"));
    // The router's argmin is the cheapest plan its dispatch arm for this acquire can RUN, which is
    // index 0 only when the acquire's scope admits everything applicable (`Prep::Range`). Under a
    // plane or candidate acquire the ranking can lead with a plan `run_query_routed` would never
    // reach — reporting that one as picked is how the same conflation `PlanScope` fixes in the
    // router used to show up here as a plausible-looking but wrong answer. Marked here rather than
    // left for the caller to re-derive, which is where a caller filtering out runtime decliners
    // gets it wrong. The full ranking still reports every applicable plan: a plan out of scope for
    // this acquire is still real calibration data for `explain_analyze`, which forces plans through
    // `run_query_with_plan` and does not route.
    let scope = prep.scope();
    if let Some(picked) = estimates.iter_mut().find(|e| scope.admits(e.plan)) {
        picked.picked = true;
    }
    (facts, estimates)
}

/// One applicable plan's `explain_analyze` (#745) result: the same predicted cost
/// `explain` reports for this plan, plus raw per-trial timings — deliberately not
/// pre-reduced (no median/mean here), so a caller can see whether a plan's timing
/// is bimodal, which this engine has measured happening on identical work before
/// (00648-engine-verifier-cost-ordering.md's measurement-traps section).
pub(crate) struct PlanTrial {
    pub(crate) plan: PhysicalPlan,
    pub(crate) predicted_ns: f64,
    /// Both carried through from `PlanEstimate` unchanged — see its fields. `picked` in particular
    /// is the router's choice, which is NOT necessarily the fastest `trials_ns`: that difference is
    /// the whole point of docs/issues/done/local-engine-plan-misselection.md.
    pub(crate) materialize_ns: f64,
    pub(crate) picked: bool,
    pub(crate) trials_ns: Vec<u64>,
    /// Raw per-round wall time of the rounds where this plan ENTERED and then declined, producing
    /// no page. Mutually exclusive with `trials_ns` in practice — a decline is deterministic for a
    /// given query and store — so exactly one of the two is non-empty for a plan that participated.
    ///
    /// Separate from `trials_ns` rather than folded into it because the two answer different
    /// questions and every consumer reduces `trials_ns` with `min` as "what this plan costs to run".
    /// A decline is not a run: it produced nothing, and averaging it in would make a plan that bails
    /// early look fast.
    ///
    /// Only the two printing-space fast paths can land here. `PhysicalPlan::applicable` is at least
    /// as strong as the guard `run_query_with_plan` re-checks, so a plan `explain` ranked never
    /// fails that guard — `None` from it is always a runtime decline, never a missed applicability
    /// test. The other four either always produce a page or are not in the list.
    ///
    /// This is the cost of the decline itself, and it is not free: `PagingTaken::DeclineSparseExact`
    /// fires AFTER `printing_compose_fastpath` has composed the printing bitmap, so those rounds pay
    /// a full compose and throw it away before the general path runs the query again. The three
    /// other declines gate before the compose and are cheap. `phases.paging_taken` is what tells the
    /// two apart, which is why it is recorded for a declining plan even though nothing else is.
    pub(crate) declined_ns: Vec<u64>,
    /// Execution counters and coarse phase timings from this plan's FASTEST recorded round — the
    /// same round `min(trials_ns)` names, so a phase share read against that total describes one
    /// execution rather than two. See `PhaseStats`: the counters check whether the cost arm's
    /// FEATURES match what the loop did, and the phase timings check whether its TERMS account for
    /// the whole executor.
    ///
    /// (The counters are round-invariant — the same query visits the same cards every time — so
    /// only the timings depend on which round is kept.)
    ///
    /// The three phases are contiguous and disjoint, so `ns_setup + ns_loop + ns_finish` can only
    /// be <= `ns_round_total`. The shortfall is real unmodelled work — `run_query_with_plan`'s own
    /// dispatch and applicability checks sit outside all three — so treat it as a residual to size,
    /// not as an invariant that should reach zero.
    ///
    /// Populated for the two materializing plans — `GatheredScan` (`exec_gathered_scan`) and
    /// `StreamedSelect` (`run_query_streamed`, which publishes from all three of its return paths).
    /// The four fast paths — `PrintingRangeScan`, `PrintingCompose`, `PlanePopcountOrder`,
    /// `CardRangePopcount` — are NOT instrumented and report zeros, except `paging_taken`, which
    /// the two printing-space fastpaths set on their own. `PlanePopcountOrder` and
    /// `CardRangePopcount` write nothing at all and are the only plans for which a `NotEntered`
    /// label is expected on a successful run.
    ///
    /// A consumer must not read all-zero counters as "this plan did no work": `explain_analyze`
    /// fills `ns_round_total` for every round it records, so `ns_round_total > 0 && ns_loop == 0` is
    /// the uninstrumented case, and `ns_round_total == 0` means the plan completed no round.
    ///
    /// `ns_round_total == 0` has two sub-cases, and `declined_ns` is what separates them:
    ///
    /// - `declined_ns` empty — the plan never produced a page and never entered a fastpath either.
    /// - `declined_ns` non-empty — the plan entered and declined. Everything here is zero EXCEPT
    ///   `paging_taken`, which names the gate that fired. That is the whole point of recording
    ///   stats for a plan that produced nothing: without it a declining `PrintingCompose` is
    ///   indistinguishable from one that was never tried, and the decline labels — compose's
    ///   `NotComposable`/`DeclineBroad`/`DeclineSparseEstimate`/`DeclineSparseExact` and the range
    ///   fastpath's `RangeSelective`/`RangeSparse`/`RangeUnalignedPrice`/`RangeNoPermutation` (plus
    ///   the two tripwires `RangeNotBare`/`RangePermutationStale`) — would be reachable only from
    ///   Rust. The counters and phase timings stay zero because no executor ran — a decline is a
    ///   gate, not a loop.
    pub(crate) phases: PhaseStats,
}

/// One timed unit inside an `explain_analyze` round. The acquire step and the routed path are
/// participants rather than fixed preludes so the round's shuffle covers them too — see
/// `explain_analyze`'s doc for why a pinned position biases the plans they pre-warm.
#[derive(Clone, Copy)]
enum Participant {
    /// Index into `estimates` / `trials_ns` / `phases`.
    Plan(usize),
    Acquire,
    Routed,
}

/// Fixed so a given query shuffles identically on every run. An A/B against another build has to
/// compare the same execution order or the ordering drift swamps what is being measured — the same
/// reason `bench_candidate_materialize` seeds its own generator rather than sampling entropy.
const PARTICIPANT_SHUFFLE_SEED: u64 = 745_002;

/// #745 primitive 2: actually run every applicable plan via `run_query_with_plan`,
/// `num_warmups` discarded rounds then `num_trials` recorded rounds, returning the
/// raw per-trial timings alongside each plan's predicted cost (from `explain`, so a
/// caller never has to zip two separate lists by plan to compare predicted vs
/// actual).
///
/// `filter` is a shared reference and is never mutated in place here — every
/// `run_query_with_plan` call gets a fresh `.clone()` off this same pristine
/// snapshot instead. This is the resolution to the correctness question
/// `docs/issues/00745-engine-explain-analyze.md` raises: `run_query_with_plan`'s
/// `StreamedSelect`/`GatheredScan` arms each call `prepare_candidates` themselves,
/// which mutates the filter it's given (`memoize_text_predicates`) — reusing one
/// `&mut FilterExpr` across calls would let whichever plan happens to run first pay
/// the one-time memoize cost while every later call (any plan) gets it for free, a
/// systematic bias the round-rotation below can't fix on its own. Cloning fresh
/// from the same pristine snapshot for every single call means every call pays the
/// identical cost every time — the same discipline `plan_cost_calibration`/
/// `plan_cost_model_matches_gold` already use (tests.rs), just via `Clone` instead
/// of re-deriving from a `FuzzSpec`, since a real caller only has the bound tree.
///
/// Each round runs `n + 2` participants in a freshly shuffled order: every applicable plan, plus
/// the acquire step and the routed path. Shuffled rather than cyclically rotated, because a
/// rotation only moves the cut point — the cyclic adjacency is fixed, so every participant keeps
/// the same immediate predecessor round after round, and rotation balances only which one goes
/// first.
///
/// That distinction matters here because two participants warm work the others then reuse, and each
/// warms a *subset* of the plans it is meant to be compared against:
/// - `run_query_routed` dispatches into the picked plan's executor, so pinning it ahead of the plan
///   loop pre-warms exactly the plan whose selection `PlanTrial::picked` exists to question.
/// - on a candidate acquire, `acquire_plan_features` calls the very `prepare_candidates` that
///   `StreamedSelect` and `GatheredScan` are about to call, and that the four fast paths never do.
///   (On a range or compose acquire it takes the cheap estimate branch and does not, so the bias is
///   present for one acquire class and absent for another — it does not even wash out as a constant
///   offset across a table keyed by acquire branch.)
///
/// The shuffle is seeded from a fixed constant, so ordering stays reproducible for the same query
/// while still decorrelating which participant follows which.
///
/// What the shuffle gives up, and why `num_trials` has to carry it: rotation was *position-balanced*
/// — over `n` rounds every participant occupied every slot exactly once — and an independent
/// per-round shuffle is not. At `num_trials = 3` a participant can draw the warm tail of the round
/// twice by chance, and since every consumer reduces `trials_ns` with `min`, that lower sample is
/// the one that survives. The trade is still right, because the bias it removes was directional and
/// unequal across acquire classes while this one is zero-mean and shrinks with rounds — but it
/// shrinks only with rounds. Prefer enough trials for positions to average out
/// (`scripts/bench_cost_model_agreement.py` uses 7); do not read a 2-3 trial run as a fair
/// head-to-head between plans whose times are within ~10% of each other.
///
/// `trials_ns` is a fair head-to-head *between plans*, not a reproduction of a real
/// query's wall time: each `run_query_with_plan` call re-runs its own
/// `prepare_candidates`, whereas `run_query_routed` acquires the shared artifact
/// once and reuses it. So compare `trials_ns` across plans, not against an
/// end-to-end `query()` latency for the winner.
fn explain_analyze(
    ctx: &QueryCtx,
    params: &QueryParams,
    filter: &FilterExpr,
    unsplit: Option<&FilterExpr>,
    plane: Option<&PlaneExpr>,
    num_warmups: usize,
    num_trials: usize,
) -> (AcquireFacts, Vec<PlanTrial>) {
    use rand::SeedableRng as _;
    use rand::seq::SliceRandom as _;

    // explain() needs `&mut` for its own (one-time) acquire-step mutation; clone so
    // the timing loop below starts from `filter`'s untouched, pristine state.
    let (mut facts, estimates) = explain(ctx, params, &mut filter.clone(), unsplit, plane);
    // explain()'s single sample was taken outside the loop below, so it sits in a
    // different cache/allocator state than the plan trials it would be compared
    // against. Discard it and re-sample in-loop, one per round, from the same fresh
    // clones and the same shuffled position pool the plans draw from.
    facts.acquire_ns.clear();

    let n = estimates.len();
    let mut trials_ns: Vec<Vec<u64>> = vec![Vec::with_capacity(num_trials); n];
    // Rounds where a plan entered and declined instead of producing a page. Kept apart from
    // `trials_ns` so `min(trials_ns)` never mixes "what running costs" with "what bailing costs" —
    // see `PlanTrial::declined_ns`.
    let mut declined_ns: Vec<Vec<u64>> = vec![Vec::new(); n];
    let mut phases: Vec<PhaseStats> = vec![PhaseStats::default(); n];
    let mut rng = rand::rngs::SmallRng::seed_from_u64(PARTICIPANT_SHUFFLE_SEED);
    let mut order: Vec<Participant> =
        (0..n).map(Participant::Plan).chain([Participant::Acquire, Participant::Routed]).collect();
    // Every participant clears both stat slots after itself, so this is only about the very first
    // one: with `num_warmups == 0` an uninstrumented plan would otherwise report whatever an
    // earlier query on this thread left in `PAGING_TAKEN`, which nothing on the production path
    // resets.
    take_phase_stats();
    for round in 0..(num_warmups + num_trials) {
        order.shuffle(&mut rng);
        for &participant in &order {
            let mut round_filter = filter.clone();
            // Bound rather than consumed, so the clock read below excludes every participant's own
            // deallocation — otherwise the plans, which hold their result, would be measured on
            // different terms from the two that discard it.
            let (mut ran, mut acquired, mut routed) = (None, None, None);
            let t0 = std::time::Instant::now();
            match participant {
                Participant::Plan(i) => ran = run_query_with_plan(estimates[i].plan, ctx, params, &mut round_filter, unsplit, plane),
                Participant::Acquire => acquired = Some(acquire_plan_features(ctx, params, &mut round_filter, unsplit, plane)),
                Participant::Routed => routed = Some(run_query_routed(ctx, params, &mut round_filter, unsplit, plane)),
            }
            let dt = t0.elapsed().as_nanos() as u64;
            drop((acquired, routed));
            // Uniform, and read immediately: the next participant's run overwrites the thread-local.
            // Routed and the plans dispatch into the same executors, so a participant that publishes
            // nothing would otherwise read its predecessor's counters as its own. That is what the
            // routed path needed a special-case clear for while it sat outside this loop — measured
            // then: 49 of 600 queries had GatheredScan reporting a `paging_taken` only the compose
            // fastpath sets.
            let mut stats = take_phase_stats();
            if round < num_warmups {
                continue;
            }
            match participant {
                Participant::Acquire => facts.acquire_ns.push(dt),
                Participant::Routed => {
                    facts.routed_ns.push(dt);
                    // Taken from the slot `run_query_routed` just wrote, in the same round, so the
                    // three sum to `dt`. Replaced with the default so a later participant that does
                    // not publish here cannot report this round as its own.
                    let ph = take_routed_phases();
                    facts.routed_acquire_ns.push(ph.ns_acquire);
                    facts.routed_choose_ns.push(ph.ns_choose);
                    facts.routed_dispatch_ns.push(ph.ns_dispatch);
                }
                // A structurally-applicable plan's fastpath can still decline at runtime
                // (e.g. PrintingCompose on a sparse total) — deterministic for this
                // query/data, so a decliner simply never accumulates trials.
                Participant::Plan(i) => {
                    if let Some((total, _)) = &ran {
                        stats.ns_round_total = dt;
                        stats.result_total = *total as u64;
                        // Keep the FASTEST recorded round's phases, not the last one's. Every
                        // consumer reduces `trials_ns` with `min`, so carrying the last round's
                        // split means the phase breakdown describes a different (and by
                        // construction slower, often contended) execution than the total it gets
                        // read next to. Cheaper than it looks: `ns_round_total` travels inside
                        // `stats`, so the two always agree about which round they came from.
                        if trials_ns[i].iter().all(|&prev| dt < prev) {
                            phases[i] = stats;
                        }
                        trials_ns[i].push(dt);
                    } else {
                        // Entered and declined. Nothing executed, so the counters and phase timings
                        // stay at their default zeros — but the GATE is a real observation, and it
                        // is the only place the four decline labels are visible from outside Rust.
                        // Assigned rather than merged: everything else in `stats` is already zero
                        // here, and leaving `ns_round_total` at 0 keeps "completed no round" true.
                        phases[i].paging_taken = stats.paging_taken;
                        declined_ns[i].push(dt);
                    }
                }
            }
        }
    }

    let trials =
        estimates
        .into_iter()
        .zip(trials_ns)
        .zip(declined_ns)
        .zip(phases)
        .map(|(((e, trials_ns), declined_ns), phases)| PlanTrial {
            plan: e.plan,
            predicted_ns: e.predicted_ns,
            materialize_ns: e.materialize_ns,
            picked: e.picked,
            trials_ns,
            declined_ns,
            phases,
        })
        .collect();
    (facts, trials)
}

/// #634 Step 2: popcount-skip order phase. Scoped to `unique=card` queries
/// whose filter fully consumed to `FilterExpr::True` (the plane bitmap IS the
/// exact match set, at any selectivity — colors/types/legality). Scatters the
/// match bitmap through the inverse permutation, then works in word space
/// instead of candidate space: total is a popcount, skip is a running
/// word-popcount sum to the boundary word, emit walks set bits from there
/// mapping back through the forward permutation for `limit` cards. O(words)
/// regardless of match count or page depth — unlike `run_query_streamed`'s
/// counts-buffer fill, which is O(candidates) no matter how deep the
/// requested page is. Compound exact filters that didn't fully consume to
/// True (e.g. `t:creature power>3`, residual = `power>3`) still go through
/// `run_query_streamed`'s Step-1-improved-but-not-popcount path — extending
/// this to non-True residuals is a reasonable fast-follow, not required here.
// needless_range_loop: `pid` is the chosen printing's id, returned to the caller.
#[allow(clippy::needless_range_loop)]
fn run_query_streamed_popcount<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    order: SortOrder<'_>,
    bitmap: &[u64],
    plane: Option<&PlaneExpr>,
    range_bits: Option<&[u64]>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    // First of the three phase boundaries, same convention as `run_query_streamed`: everything to the
    // skip scan is `ns_setup` (here the total popcount plus the permuted scatter), the skip scan is
    // `ns_loop`, the emit walk is `ns_finish`. Shared by BOTH popcount plans, so instrumenting here
    // covers `PlanePopcountOrder` and `CardRangePopcount` at once.
    let t_start = std::time::Instant::now();
    let QueryCtx { cards, printings, offsets, strings, indexes } = *ctx;
    let QueryParams { prefer, limit, page_offset, .. } = *params;
    let SortOrder { perm, inv: inv_perm } = order;
    let planes = &indexes.planes;
    let n_cards = cards.len();
    let total: usize = bitmap.iter().map(|w| w.count_ones() as usize).sum();
    if total == 0 || page_offset >= total {
        // Still publishes: an empty page is real work (the popcount) and a plan that ran must report
        // a cost, or a consumer reading the phases as the executor's time gets zero for a real run.
        publish_popcount_phases(t_start.elapsed().as_nanos() as u64, 0, 0);
        return (total, Vec::new());
    }

    thread_local! {
        static PERMUTED: std::cell::RefCell<Vec<u64>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    PERMUTED.with(|cell| {
        let mut permuted = cell.borrow_mut();
        let wpp = n_cards.div_ceil(64);
        permuted.clear();
        permuted.resize(wpp, 0);
        // Scatter: every set bit's position in sort order (inv_perm[cid])
        // becomes a set bit here. Tail bits never get touched — inv_perm's
        // range is exactly 0..n_cards, so no cid maps past the last word.
        for (i, &word) in bitmap.iter().enumerate() {
            let mut w = word;
            while w != 0 {
                let cid = (i as u32) << 6 | w.trailing_zeros();
                w &= w - 1;
                let pos = u32::from(inv_perm[cid as usize]) as usize;
                permuted[pos / 64] |= 1u64 << (pos % 64);
            }
        }

        // Ends `ns_setup` (popcount + scatter) and starts `ns_loop`.
        let t_loop = std::time::Instant::now();

        // Skip: accumulate word popcounts until the boundary word containing
        // page_offset — 64 cards per word read, deep pagination is a
        // ~n_cards/64-word scan regardless of match count.
        let mut skip = page_offset;
        let mut word_idx = 0;
        while word_idx < permuted.len() {
            let wc = permuted[word_idx].count_ones() as usize;
            if skip < wc {
                break;
            }
            skip -= wc;
            word_idx += 1;
        }

        // Emit: walk set bits from the boundary word onward (skipping `skip`
        // more within it), mapping position -> card id via the forward perm.
        // all_match is always true here (filter fully consumed to True), so
        // the printing choice mirrors push_card_matches' Mode::Card branch
        // under all_match: first printing for default prefer (ranges are
        // stored in descending default-prefer order), best-scored otherwise
        // -- *unless* the plane touched a legality leaf
        // (docs/issues/00667-engine-legality-divergent-carveout.md "Row selection
        // for unique=card"), in which case card-level truth only proves
        // *some* printing matches, not whichever one prefer-order would pick
        // blindly -- verify against `eval_plane_expr_for_printing` too. Cheap
        // even then: bounded by `limit` emitted cards, not the candidate set,
        // and only pays the extra check at all for legality-touching planes.
        let existential = plane.is_some_and(|e| plane_expr_is_existential(e, u64::from(planes.divergent_formats)));
        // Ends `ns_loop` (the skip scan) and starts `ns_finish` (the emit walk).
        let t_finish = std::time::Instant::now();
        let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
        'walk: while word_idx < permuted.len() {
            let mut w = permuted[word_idx];
            while w != 0 {
                let bit = w.trailing_zeros();
                w &= w - 1;
                if skip > 0 {
                    skip -= 1;
                    continue;
                }
                let pos = (word_idx as u32) << 6 | bit;
                let cid = u32::from(perm[pos as usize]);
                let card = &cards[cid as usize];
                let start = u32::from(offsets[cid as usize]) as usize;
                let end = u32::from(offsets[cid as usize + 1]) as usize;
                // Two per-printing residuals the card-existence bitmap can't encode: an existential
                // plane (legality — the card matching doesn't pin which printing is legal) and the
                // range membership (CardRangePopcount — the shown printing must actually be in range,
                // not just belong to a card that has some in-range printing). Both are cheap: bounded
                // by `limit` emitted cards, an O(1) bit test for the range, checked only when present.
                let satisfies = |pid: usize| {
                    (!existential
                        || eval_plane_expr_for_printing(plane.expect("existential ⇒ plane"), planes, cid, &printings[pid], strings))
                        && range_bits.is_none_or(|rb| bitmap_contains(rb, pid as u32))
                };
                let chosen: Option<u32> = if matches!(prefer, Prefer::Default) {
                    (start..end).find(|&pid| satisfies(pid)).map(|pid| pid as u32)
                } else {
                    // Strict > only (matches push_card_matches): ties keep the
                    // first-found printing, not the last.
                    let mut best: Option<(u32, f64)> = None;
                    for pid in start..end {
                        if !satisfies(pid) {
                            continue;
                        }
                        let score = prefer_score(card, &printings[pid], prefer);
                        if best.is_none_or(|(_, s)| score > s) {
                            best = Some((pid as u32, score));
                        }
                    }
                    best.map(|(pid, _)| pid)
                };
                if let Some(pid) = chosen {
                    page.push((card, &printings[pid as usize]));
                }
                if page.len() == limit {
                    break 'walk;
                }
            }
            word_idx += 1;
        }
        publish_popcount_phases(
            (t_loop - t_start).as_nanos() as u64,
            (t_finish - t_loop).as_nanos() as u64,
            t_finish.elapsed().as_nanos() as u64,
        );
        (total, page)
    })
}

/// Publish the popcount executor's three phases, consuming whatever build its caller handed over.
///
/// The pending value differs per plan and the `RANGE_ACQUIRES` rule is what makes both correct:
/// `PlanePopcountOrder`'s plane eval is built during ACQUIRE on the routed path (so a forced run's
/// rebuild must not count toward dispatch, and `plane` is not a range acquire), while
/// `CardRangePopcount`'s bitmap is built in DISPATCH on both paths (so it must count, and
/// `card_range_popcount` IS a range acquire). Same handoff, opposite treatment, both from one rule.
fn publish_popcount_phases(ns_setup: u64, ns_loop: u64, ns_finish: u64) {
    let prep_ns = PENDING_PREPARE_NS.with(|c| c.replace(0));
    PHASE_STATS.with(|c| c.set(PhaseStats { ns_setup, ns_loop, ns_finish, ns_prepare: prep_ns, ..PhaseStats::default() }));
}

/// Streamed selection: match phase records per-card match counts (total is
/// their sum), then either gathers (small totals — byte-identical to the
/// gathered path) or walks the orderby permutation emitting only page cards.
// Eight since `proven_conjuncts` joined `all_match_known`: the two are the same idea at two
// granularities (see `Narrowed::proven`), and both have to reach `card_pass` at the bottom of the loop.
#[allow(clippy::too_many_arguments, reason = "all_match_known and proven_conjuncts are one signal at two granularities, both needed by card_pass")]
fn run_query_streamed<'a>(
    ctx: &QueryCtx<'a>,
    params: &QueryParams,
    filter: &FilterExpr,
    all_match_known: bool,
    // Conjuncts the candidate set proves, passed straight to `card_pass` (see `Narrowed::proven`).
    // Alongside `all_match_known` because they are the same idea at two granularities: that flag says the
    // whole residual is settled, this says which parts of it are.
    proven_conjuncts: u64,
    walk: &[Archived<u32>],
    card_ids: Box<dyn ExactSizeIterator<Item = u32> + '_>,
    existential_plane: Option<(&PlaneExpr, &Archived<BitPlanes>)>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    // First of the three phase boundaries — everything down to the match loop is `ns_setup`, which
    // for this executor is dominated by the `counts` zeroing below. See `PhaseStats::ns_setup`.
    let t_start = std::time::Instant::now();
    let QueryCtx { cards, printings, offsets, strings, indexes } = *ctx;
    let QueryParams { mode, prefer, sort_col, descending, limit, page_offset, .. } = *params;
    let artwork_groups = &indexes.artwork_groups;
    let artwork_group_col = &indexes.artwork_group_col;
    let max_artwork_groups = u16::from(indexes.max_artwork_groups);
    let mut residual: Vec<&FilterExpr> = Vec::new();
    let mut residual_is_or = false;
    let mut seen_words = [0u64; ARTWORK_GROUP_WORDS]; // #629: artwork-mode match-count scratch

    // Match phase: sequential (candidate-order) evaluation into per-card
    // counts. Exact total = sum of counts, known before emission strategy.
    // The counts buffer is reused across queries (thread-local) — the
    // per-query ~126 kB allocation was measurable on selective queries.
    thread_local! {
        static COUNTS: std::cell::RefCell<Vec<u32>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    COUNTS.with(|counts_cell| {
    let mut counts = counts_cell.borrow_mut();
    counts.clear();
    counts.resize(cards.len(), 0);
    let have_group_counts = artwork_groups.len() == cards.len();
    let mut total: usize = 0;
    // Same counters GatheredScan publishes, so the two plans' arms can be checked the same way.
    // Locals in the loop; published once at the end. See PhaseStats.
    let (mut n_cards_visited, mut n_printing_span, mut n_matches_pushed) = (0u64, 0u64, 0u64);
    // See the same local in the gathered loop. This plan only COUNTS here (the page is emitted
    // below), and counting short-circuits harder than emitting does: both `all_match` arms of
    // `card_match_count` answer without reading a printing at all, so this can legitimately be 0
    // where `n_printing_span` is the full corpus span.
    let mut n_printings_examined = 0u64;
    // Ends `ns_setup` and starts `ns_loop` — one read, two phases.
    let t_loop = std::time::Instant::now();
    for cid in card_ids {
        n_cards_visited += 1;
        let card = &cards[cid as usize];
        // #634 Step 1: skip the redundant card_pass re-derivation of Tri::True when the narrowing has
        // already proved every candidate matches.
        //
        // **This was gated OFF for `Mode::Artwork`, and the gate is now removed (2026-08-04).** Its comment
        // cited a ~45% regression on `t:creature`/artwork (0.13ms -> 0.19ms) from applying the skip here,
        // called it "an unexplained codegen/scheduling effect ... not a logical cost", and said it was
        // "isolated by bisecting call sites" — i.e. across builds, the one instrument this engine has
        // repeatedly shown cannot resolve an effect that size and will hand you the wrong sign (see trap 1
        // in docs/workflows/diagnosing-a-plan-cost-error.md).
        //
        // Re-measured with a runtime toggle inside ONE binary, `unique=artwork`, limit 175:
        //
        //     query         gate on      gate off    speedup
        //     o:this        1047.7 us      47.5 us     22.1x
        //     o:target       556.6         30.1        18.5x
        //     o:creature    1010.5         56.6        17.9x
        //     t:creature      84.1         43.0         2.0x   <- the query the gate existed to protect
        //     o:flying        24.1         13.5         1.8x
        //     c:r             32.2         18.5         1.7x
        //     t:land          15.5         12.1         1.3x
        //
        // Every cell is faster and the cited regression does not reproduce in either direction. The cost was
        // worst for an expensive residual: `o:creature` ran a full oracle-text containment check over all
        // 23,155 candidates the narrowing had already proved matched, which is why the same query in printing
        // mode — identical candidate set, identical span, `card_pass` skipped — took 56.7 us against
        // artwork's 1010.5. That query was the single largest routing regret in the engine at 508.9 us, and
        // the cost model was right about it throughout: it charges `tier = 0` here, because `all_match_known`
        // is supposed to mean no `card_pass` runs.
        //
        // Row identity is what an `all_match` mistake breaks silently in this mode — totals do not move, the
        // printing that REPS each artwork group does. 1,134 cells over 21 predicates x 3 modes x 3 orderbys x
        // 3 pages x 2 prefers, hashing the returned `scryfall_id` sequence: **identical**, 378 of them
        // artwork, including the existential-plane shapes (`f:*`, `border:*`, `r>=rare`, `watermark:*`,
        // `-f:modern`) where a card-level True does not imply every printing matches. Soundness rests on
        // `all_match_known` itself, which already refuses an existential plane outside card mode via
        // `plane_leaves_nothing_to_verify`, and only trusts `residual_exact` when the narrowing was not
        // printing-space.
        let all_match = all_match_known
            || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or, proven_conjuncts) {
                Tri::False | Tri::Null => continue,
                Tri::True => true,
                Tri::PrintingDep => false,
            };
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;
        // Counted HERE, below the `card_pass` continue above, because a card rejected there never has
        // its printings touched. Counting at the top of the loop instead included them, so this plan
        // and GatheredScan reported different `printing_span` for identical work whenever the
        // narrowing was inexact -- and `scan_units` has one definition, so at most one could be the
        // valid comparison.
        n_printing_span += (end - start) as u64;
        // Every printing matches: card/printing counts are O(1) inside the
        // helper, and the artwork group count is a build-time constant.
        let c = if all_match && matches!(mode, Mode::Artwork) && have_group_counts {
            // A stored per-card group count: answered without looking at one printing.
            u32::from(u16::from(artwork_groups[cid as usize]))
        } else {
            let (c, examined) = card_match_count(
                card, cid, printings, &indexes.artwork_group_col, start, end, all_match, &residual, residual_is_or, mode, strings,
                existential_plane,
                &mut seen_words,
            );
            n_printings_examined += u64::from(examined);
            c
        };
        counts[cid as usize] = c;
        total += c as usize;
        n_matches_pushed += c as u64;
    }
    // Ends `ns_loop` and starts `ns_finish`.
    let t_finish = std::time::Instant::now();
    // Publishing helper: the walk below has several early returns, and every one of them must leave
    // the stats behind or the accounting silently attributes this plan's work to nothing. Each takes
    // the closing instant itself, so the emit phase is bounded without a second start marker.
    let publish = |end: std::time::Instant, perm_steps: u64| {
        let prep_ns = PENDING_PREPARE_NS.with(|c| c.replace(0));
        PHASE_STATS.with(|c| {
            c.set(PhaseStats {
                cards_visited: n_cards_visited,
                printing_span: n_printing_span,
                printings_examined: n_printings_examined,
                matches_pushed: n_matches_pushed,
                perm_steps,
                ns_setup: (t_loop - t_start).as_nanos() as u64,
                ns_loop: (t_finish - t_loop).as_nanos() as u64,
                ns_finish: (end - t_finish).as_nanos() as u64,
                ns_round_total: 0,
                ns_prepare: prep_ns,
                result_total: 0,                       // see the note at the other publisher
                paging_taken: PagingTaken::NotEntered, // ditto: owned by PAGING_TAKEN
            });
        });
    };
    if total == 0 || page_offset >= total {
        publish(std::time::Instant::now(), 0);
        return (total, Vec::new());
    }

    // artwork-mode emission scratch (#629), reused across cards below. Pre-sized to
    // max_artwork_groups so the grouping loop needs no per-printing resize check.
    let mut group_best: Vec<Option<(u32, f64)>> =
        if matches!(mode, Mode::Artwork) { vec![None; usize::from(max_artwork_groups)] } else { Vec::new() };
    let mut touched: Vec<u16> = Vec::new();

    // Small totals: gather and quickselect — same result as the gathered path.
    if total <= *STREAM_MIN_MATCHES {
        let mut best: Vec<Match> = Vec::with_capacity(total);
        for cid in 0..cards.len() as u32 {
            if counts[cid as usize] == 0 {
                continue;
            }
            let card = &cards[cid as usize];
            let all_match = all_match_known
                || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or, proven_conjuncts) {
                    Tri::True => true,
                    Tri::PrintingDep => false,
                    _ => continue,
                };
            let start = u32::from(offsets[cid as usize]) as usize;
            let end   = u32::from(offsets[cid as usize + 1]) as usize;
            push_card_matches(
                card, cid, printings, artwork_group_col, start, end, all_match, &residual, residual_is_or, mode, prefer,
                sort_col, descending, strings, existential_plane, &mut best, &mut group_best, &mut touched,
            );
        }
        let page = select_page(best, page_offset, limit)
            .into_iter()
            .map(|(cid, pid)| (&cards[cid as usize], &printings[pid as usize]))
            .collect();
        publish(std::time::Instant::now(), 0);
        return (total, page);
    }

    // Stream: walk the segment `walk_bounds` handed over, consume page_offset
    // from the counts, emit page cards only. Within a card, items order by
    // (sort key, pid) — the same comparator select_page uses; across cards the
    // permutation supplies the order.
    //
    // The segment is the whole permutation unless the filter bounded the sort column, in which case
    // every position outside it belongs to a card whose value cannot satisfy that bound and therefore
    // has `counts[cid] == 0`. The walk's only effect on a zero-count entry is to `continue`, so
    // narrowing the segment cannot change which rows come back — only how many entries are stepped to
    // find them.
    let mut skip = page_offset;
    let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
    let mut scratch: Vec<Match> = Vec::new();
    // Counted for every entry the walk touches, including the ones skipped on a zero count -- that
    // skip IS the walk's cost, and it is what grows as matches thin out in a larger corpus. Entries
    // outside the segment are NOT counted: they are never stepped, and the counter's job is to grade
    // the cost model against work actually done. A plain local, published once, like the others.
    let mut n_perm_steps = 0u64;
    'walk: for cid in walk.iter().map(|x| u32::from(*x)) {
        n_perm_steps += 1;
        let c = counts[cid as usize] as usize;
        if c == 0 {
            continue;
        }
        if skip >= c {
            skip -= c;
            continue;
        }
        let card = &cards[cid as usize];
        let all_match = all_match_known
            || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or, proven_conjuncts) {
                Tri::True => true,
                Tri::PrintingDep => false,
                _ => continue,
            };
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;
        scratch.clear();
        push_card_matches(
            card, cid, printings, artwork_group_col, start, end, all_match, &residual, residual_is_or, mode, prefer,
            sort_col, descending, strings, existential_plane, &mut scratch, &mut group_best, &mut touched,
        );
        scratch.sort_unstable_by(page_cmp);
        for m in scratch.iter().skip(skip) {
            page.push((&cards[m.1 as usize], &printings[m.2 as usize]));
            if page.len() == limit {
                break 'walk;
            }
        }
        skip = 0;
    }
    publish(std::time::Instant::now(), n_perm_steps);
    (total, page)
    }) // COUNTS.with
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
    // Exact f64 dollars from the stored integer cents, not the old lossy f32 -- API consumers
    // now see the true price (e.g. 1.47, not the nearest f32 to 1.47) instead of an
    // approximation.
    ("price_usd", |py, _c, p, _s, _v| Ok(p.price_usd.as_ref().map(|v| f64::from(u32::from(*v)) / 100.0).into_pyobject(py)?.into_any())),
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
    // Card-data fields for downstream filtering, in Scryfall JSON shapes (names and value
    // shapes match RESULT_FIELD_COLUMNS in api/api_resource.py, which reshapes the SQL
    // path's raw columns to agree with these).
    ("layout", |py, c, _p, s, _v| Ok(str_at(s, u32::from(c.card_layout_id)).into_pyobject(py)?.into_any())),
    ("cmc", |py, c, _p, _s, _v| Ok(c.cmc.as_ref().map(|v| u8::from(*v)).into_pyobject(py)?.into_any())),
    ("rarity", |py, _c, p, _s, _v| {
        Ok(p.card_rarity_int.as_ref().and_then(|v| rarity_int_to_text(u8::from(*v))).into_pyobject(py)?.into_any())
    }),
    ("color_identity", |py, c, _p, _s, _v| Ok(identity_letters(u8::from(c.card_color_identity)).into_pyobject(py)?.into_any())),
    ("legalities", |py, c, p, _s, _v| {
        // Printing-level word only for the ~556 divergence cards, same rule the filters use.
        let bits = if c.legality_divergent { u64::from(p.card_legalities) } else { u64::from(c.card_legalities) };
        Ok(legality_bits_to_pydict(py, bits)?.into_any())
    }),
];

/// Mirror of magic.rarity_int_to_text -- the import stores 0-5, Scryfall speaks words.
fn rarity_int_to_text(value: u8) -> Option<&'static str> {
    match value {
        0 => Some("common"),
        1 => Some("uncommon"),
        2 => Some("rare"),
        3 => Some("mythic"),
        4 => Some("special"),
        5 => Some("bonus"),
        _ => None,
    }
}

/// Decode an identity bitmap into Scryfall's WUBRG-ordered letter list.
fn identity_letters(mask: u8) -> Vec<&'static str> {
    [("W", 1u8), ("U", 2), ("B", 4), ("R", 8), ("G", 16), ("C", 32)]
        .iter()
        .filter(|(_, bit)| mask & bit != 0)
        .map(|(letter, _)| *letter)
        .collect()
}

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
/// catch (e.g. reordering same-size fields, changing an index type) — and on
/// any FLAVOR_FP_FEATURES change: archived fingerprints are built with that
/// table, so a new table reading old fingerprints breaks the superset test.
// `NameUnigramIndex` (#858) is a new archived type, so a store built before it must fail the header
// check and be rebuilt rather than be read as garbage. Dated 2026-08-06, patch 01; the check is
// EQUALITY, so the invariant is only that a value is never reused for a different layout.
const ARCHIVE_FORMAT_VERSION: u32 = 2026080601;
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
    mana: ManaVocabInterner,
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

/// Shared filter resolution for `query`/`explain`/`explain_analyze` (#745): parse
/// `filters`' `to_json()`, bind against `data`'s vocabs, and split off the
/// plane-expressible part (colors/identity/types) into a bitmap expression. The
/// single source of truth for these steps — `query`'s hot path calls this too, so
/// the diagnostics can never route a query differently than a real search would.
///
/// The plane split is guarded on the archive carrying planes for this card count;
/// the format-version bump already rejects pre-plane archives, so this is defense
/// in depth. `unique_is_card` follows `mode_from_unique` exactly (anything but
/// `"artwork"`/`"printing"` is card mode) — see `split_planes`'s doc.
///
/// No `#[inline]`: this shares a crate with its only hot-path caller (`query`), so
/// the compiler already inlines at its discretion, and the body is dominated by
/// Python FFI (`to_json`, `orjson.dumps`) — a call boundary here is unmeasurable.
/// Binds the Python filter, extracts what it says about `sort_col`, and splits it into plane + residual.
///
/// The `SortBound` comes out of here rather than out of the executor because this is the last moment the
/// whole filter exists: `split_planes` compiles `cmc>=6` into mask algebra over bitplanes and leaves
/// `FilterExpr::True` behind, so by the time a plan runs there is nothing left to read the bound from.
/// Every caller that does not want it can ignore it and get `UNBOUNDED` behaviour — a longer walk, never
/// a different page.
fn bind_and_split_filter(
    py: Python<'_>,
    filters: &Bound<PyAny>,
    unique: &str,
    data: &Archived<CardData>,
    sort_col: SortCol,
) -> PyResult<(Option<PlaneExpr>, FilterExpr, SortBound, FilterExpr)> {
    let to_json = filters.call_method0("to_json")?;
    let json_bytes: Vec<u8> = py
        .import("orjson")?
        .call_method1("dumps", (to_json,))?
        .extract()?;
    let json_str = std::str::from_utf8(&json_bytes)
        .map_err(|e| QueryError::new_err(format!("bad UTF-8 from orjson: {e}")))?;
    let json_val: Value = serde_json::from_str(json_str)
        .map_err(|e| QueryError::new_err(format!("bad query JSON: {e}")))?;

    // Must run before build_filter so legality shifts resolve in workers that
    // never executed the load path themselves.
    sync_format_shifts(&data.format_shifts);
    let mut filter_expr = build_filter(&json_val)
        .map_err(|e| QueryError::new_err(format!("build_filter: {e}")))?;
    filter_expr.bind(&data.coll_vocab, &data.coll_vocab_sorted, &data.artist_vocab, &data.mana_vocab, &data.indexes.flavor, &data.strings);

    // Read before the split consumes the tree.
    let sort_bound = sort_col_bound(&filter_expr, sort_col);
    // Kept, and this is the point: `split_planes` is a DESTRUCTIVE rewrite run before any plan is costed,
    // and it moves predicate out of the tree that `PrintingCompose` composes from into a `PlaneExpr` it
    // cannot read. Compose then fails its `plane.is_none()` guard and never reaches the argmin -- measured
    // at 1.83 us against StreamedSelect's 99.38 on `f:commander`/printing, a plan the router was never
    // offered. Retaining the unsplit form lets each plan be costed on the representation it can consume,
    // which is what #702 says the routing layer is for. One clone of a small tree, once per query.
    let unsplit = filter_expr.clone();
    let (plane, residual) = if u32::from(data.indexes.planes.n_cards) as usize == data.cards.len() && !data.cards.is_empty() {
        split_planes(filter_expr, &data.indexes.planes, &data.indexes.oracle_trigram.words, !matches!(unique, "artwork" | "printing"))
    } else {
        (None, filter_expr)
    };
    Ok((plane, residual, sort_bound, unsplit))
}

fn plan_estimate_to_pydict<'py>(py: Python<'py>, e: &PlanEstimate) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("plan", format!("{:?}", e.plan))?;
    d.set_item("predicted_ns", e.predicted_ns)?;
    d.set_item("materialize_ns", e.materialize_ns)?;
    d.set_item("picked", e.picked)?;
    Ok(d)
}

fn plan_trial_to_pydict<'py>(py: Python<'py>, t: &PlanTrial) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("plan", format!("{:?}", t.plan))?;
    d.set_item("predicted_ns", t.predicted_ns)?;
    d.set_item("materialize_ns", t.materialize_ns)?;
    d.set_item("picked", t.picked)?;
    d.set_item("trials_ns", t.trials_ns.clone())?;
    // Non-empty only for a plan that entered a fastpath and declined; `paging_taken` below names
    // the gate. See `PlanTrial::declined_ns` — a decline is not a cheap run, and
    // `DeclineSparseExact` in particular pays a full compose first.
    d.set_item("declined_ns", t.declined_ns.clone())?;
    d.set_item("cards_visited", t.phases.cards_visited)?;
    d.set_item("printing_span", t.phases.printing_span)?;
    d.set_item("printings_examined", t.phases.printings_examined)?;
    d.set_item("matches_pushed", t.phases.matches_pushed)?;
    d.set_item("perm_steps", t.phases.perm_steps)?;
    d.set_item("ns_setup", t.phases.ns_setup)?;
    d.set_item("ns_loop", t.phases.ns_loop)?;
    d.set_item("ns_finish", t.phases.ns_finish)?;
    d.set_item("ns_round_total", t.phases.ns_round_total)?;
    d.set_item("ns_prepare", t.phases.ns_prepare)?;
    // Ground truth for this run, and which paging branch really ran. Both exist so a harness can
    // check the model against what happened rather than against a second, separate execution.
    d.set_item("result_total", t.phases.result_total)?;
    d.set_item("paging_taken", t.phases.paging_taken.label())?;
    Ok(d)
}

/// The acquire step's per-query facts, as both `explain` and `explain_analyze` report
/// them under the `"acquire"` key.
fn acquire_facts_to_pydict<'py>(py: Python<'py>, f: &AcquireFacts) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("count_source", f.count_source.label())?;
    d.set_item("narrowed_repr", f.narrowed_repr.label())?;
    d.set_item("acquire_ns", f.acquire_ns.clone())?;
    d.set_item("routed_ns", f.routed_ns.clone())?;
    d.set_item("routed_acquire_ns", f.routed_acquire_ns.clone())?;
    d.set_item("routed_choose_ns", f.routed_choose_ns.clone())?;
    d.set_item("routed_dispatch_ns", f.routed_dispatch_ns.clone())?;
    // The model's own inputs, so a calibration fit regresses on the same vector `plan_cost` reads.
    let g = &f.feats;
    for (k, v) in [
        ("eval_domain", g.eval_domain),
        ("n_cards", g.n_cards),
        ("matches", g.matches),
        ("n_printings", g.n_printings),
        ("scan_units", g.scan_units),
        // P3's own scan estimate; equals `scan_units` unless the acquire knew the two plans differ.
        ("stream_scan_units", g.stream_scan_units),
        ("residual_tier_ns100", g.residual_tier_ns100),
        ("residual_card_invariant", u32::from(g.residual_card_invariant)),
        ("limit", g.limit),
        ("offset", g.offset),
        ("broadcast_printings", g.broadcast_printings),
        ("scatter_printings", g.scatter_printings),
        ("project_printings", g.project_printings),
        ("popcount_words", g.popcount_words),
        ("artwork_seen_cards", g.artwork_seen_cards),
        ("compose_scan_printings", g.compose_scan_printings),
        ("gather_group_printings", g.gather_group_printings),
        // Derived inside plan_cost rather than stored, and exposed because the Perm/OrderbyWalk
        // paging branches are priced entirely on it and nothing else can check them.
        ("printings_walked", cost::printings_walked(g) as u32),
    ] {
        d.set_item(k, v)?;
    }
    // `label()`, not `Debug` — a consumer compares this against `PagingTaken::label()`, so the two
    // have to spell the shared strategy names identically. See `ComposePaging::label`.
    d.set_item("compose_paging", g.compose_paging.label())?;
    Ok(d)
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
        if let Some(ref c) = *guard
            && c.inode == path_inode
        {
            return Ok(Arc::clone(&c.mmap));
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
        // truncate(false) is explicit, not incidental: nothing is ever written to this file — it
        // exists only as an flock target — so opening it must never disturb whatever is already
        // there, including for a worker that already holds it open.
        let lock_file = std::fs::OpenOptions::new()
            .write(true).create(true).truncate(false).open(&lock_path)
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

        *staging = Some(Staging { rows: Vec::new(), interner: Interner::new(), vocab: VocabInterner::new(), artists: VocabInterner::new(), mana: ManaVocabInterner::new(), lock_file });
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
                staging.rows.push(card_from_pydict(d, &mut staging.interner, &mut staging.vocab, &mut staging.artists, &mut staging.mana)?);
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
        let Staging { mut rows, interner, vocab, artists, mana, lock_file } = staging;

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
                    card_name_folded: row.card_name_folded,
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
                    name_rank: 0, // assigned after grouping by assign_name_ranks

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
                artwork_group_id: 0, // placeholder; assign_artwork_groups fills every printing below
            });
        }
        offsets.push(printings.len() as u32);
        assign_name_ranks(&mut cards);

        #[cfg(feature = "alloc-counter")]
        let stats_after_cards = (alloc_stats::live(), alloc_stats::allocs());

        let strings = interner.strings;
        drop(interner.map);
        let coll_vocab = vocab.strings;
        drop(vocab.map);
        let artist_vocab = artists.strings;
        drop(artists.map);
        let mana_vocab = mana.strings;
        drop(mana.map);
        // String-sorted permutation of the vocab ids; VocabInterner caps the
        // vocab at u16::MAX entries so the cast can't truncate.
        let mut coll_vocab_sorted: Vec<u16> = (0..coll_vocab.len() as u16).collect();
        coll_vocab_sorted.sort_unstable_by(|&a, &b| coll_vocab[a as usize].cmp(&coll_vocab[b as usize]));
        // Assigns every printing's artwork_group_id in place; the returned counts
        // feed CardIndexes.artwork_groups below. Must run before printings is
        // borrowed by the builders in the CardIndexes literal.
        let artwork_group_counts = assign_artwork_groups(&mut printings, &offsets);
        // Before the counts are moved into the struct below.
        let artwork_base = build_artwork_base_from(&artwork_group_counts);
        // The range indexes and their exact card-count tables come out here rather than inside the
        // literal below, because the tables need `printing_to_card` — which the literal also wants,
        // so it is derived once and moved in.
        let printing_to_card = build_printing_to_card(&offsets);
        let released_at_idx = build_printing_value_index(&printings, &cards, &offsets, |p| p.released_at_int);
        let price_usd_idx = build_printing_value_index(&printings, &cards, &offsets, |p| p.price_usd);
        let price_eur_idx = build_printing_value_index(&printings, &cards, &offsets, |p| p.price_eur);
        let price_tix_idx = build_printing_value_index(&printings, &cards, &offsets, |p| p.price_tix);
        let collector_number_idx = build_printing_value_index(&printings, &cards, &offsets, |p| p.collector_number_int.map(u32::from));
        let released_at_cards = build_range_card_counts(&released_at_idx, &printing_to_card, cards.len(), &printings, &artwork_base);
        let price_usd_cards = build_range_card_counts(&price_usd_idx, &printing_to_card, cards.len(), &printings, &artwork_base);
        let price_eur_cards = build_range_card_counts(&price_eur_idx, &printing_to_card, cards.len(), &printings, &artwork_base);
        let price_tix_cards = build_range_card_counts(&price_tix_idx, &printing_to_card, cards.len(), &printings, &artwork_base);
        let collector_number_cards = build_range_card_counts(&collector_number_idx, &printing_to_card, cards.len(), &printings, &artwork_base);
        let rarity_idx = build_printing_value_index(&printings, &cards, &offsets, |p| p.card_rarity_int.map(u32::from));
        let rarity_cards = build_range_card_counts(&rarity_idx, &printing_to_card, cards.len(), &printings, &artwork_base);
        // Out here for the same reason as the count tables: it reads `printing_to_card`, which the
        // struct literal below moves.
        let pair_totals = build_pair_totals(
            &cards,
            &printings,
            &printing_to_card,
            &strings,
            &coll_vocab,
            usize::from(artwork_group_counts.iter().copied().max().unwrap_or(0)),
        );
        let value_totals = build_all_value_totals(
            &cards,
            &printings,
            &printing_to_card,
            &strings,
            &coll_vocab,
            usize::from(artwork_group_counts.iter().copied().max().unwrap_or(0)),
        );
        let indexes = CardIndexes {
            name_trigram:   build_trigram_index(&cards, |c| c.card_name_folded.as_str()),
            oracle_trigram: build_oracle_text_index(&cards, &strings),
            cmc:            build_numeric_index(&cards, |c| c.cmc.map(|v| v as i16)),
            power:          build_numeric_index(&cards, |c| c.creature_power.map(|v| v as i16)),
            toughness:      build_numeric_index(&cards, |c| c.creature_toughness.map(|v| v as i16)),
            rarity:         build_rarity_index(&printings, &offsets),
            subtypes:       build_tag_index(&cards, &coll_vocab, |c| &c.card_subtypes),
            keywords:       build_tag_index(&cards, &coll_vocab, |c| &c.card_keywords),
            oracle_tags:    build_tag_index(&cards, &coll_vocab, |c| &c.card_oracle_tags),
            art_tags:       build_tag_index(&printings, &coll_vocab, |p| &p.card_art_tags),
            is_tags:        build_tag_index(&printings, &coll_vocab, |p| &p.card_is_tags),
            frame_data:     build_hybrid_tag_index(&printings, &coll_vocab, |p| &p.card_frame_data),
            artists:        build_artist_index(&printings, artist_vocab.len()),
            flavor:         build_flavor_index(&printings, &strings),
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
            watermarks:     {
                let mut idx: TagIndex = HashMap::new();
                for (i, p) in printings.iter().enumerate() {
                    if p.card_watermark_id != NONE_STR {
                        let wm = strings[p.card_watermark_id as usize].as_str();
                        idx.entry(wm.to_string()).or_default().push(i as u32);
                    }
                }
                idx
            },
            released_at:    released_at_idx,
            price_usd:      price_usd_idx,
            price_eur:      price_eur_idx,
            price_tix:      price_tix_idx,
            collector_number: collector_number_idx,
            released_at_cards,
            price_usd_cards,
            price_eur_cards,
            price_tix_cards,
            collector_number_cards,
            sort_perms:     build_sort_permutations(&cards),
            max_artwork_groups: artwork_group_counts.iter().copied().max().unwrap_or(0),
            artwork_groups: artwork_group_counts,
            // Columnar copy of each printing's assigned artwork_group_id, derived here — the single
            // production spot where assign_artwork_groups (above) has just filled it. Archived with
            // the store, so it is never recomputed post-load and cannot drift from the struct field.
            artwork_group_col: printings.iter().map(|p| p.artwork_group_id).collect(),
            // Same reasoning as artwork_group_col: derived here, archived, never recomputed
            // post-load, so it cannot drift from the counts it sums.
            artwork_base,
            printing_to_card,
            planes:         build_bit_planes(&cards, &printings, &offsets, &strings),
            border_printing: build_border_printing_planes(&printings, &strings),
            rarity_printing: build_rarity_printing_planes(&printings),
            rarity_printing_ordered: rarity_idx,
            rarity_cards,
            value_totals,
            pair_totals,
            name_bigrams:   build_name_bigram_index(&cards),
            name_unigrams:  build_name_unigram_index(&cards),
            legal_divergent: build_divergent_ids(&cards),
            arith_tuple:    build_arith_tuple_index(&cards),
        };

        #[cfg(feature = "alloc-counter")]
        let stats_after_indexes = (alloc_stats::live(), alloc_stats::allocs());

        // Snapshot the registry card_from_pydict just populated so reader
        // processes can adopt the same format→shift assignments.
        let format_shifts_snapshot = format_shifts().read().map(|m| m.clone()).unwrap_or_default();

        let card_data = CardData {
            cards,
            printings,
            offsets,
            strings,
            coll_vocab,
            coll_vocab_sorted,
            artist_vocab,
            mana_vocab,
            indexes,
            format_shifts: format_shifts_snapshot,
        };

        // Write atomically: stream into a per-PID .tmp, then rename over shm_path.
        // Per-PID avoids the race where two workers write to the same .tmp and
        // one's rename consumes the file before the other can rename it.
        // Streaming the serialization straight into the file means the archive
        // bytes exist only as file pages — there is no second copy of the
        // archive as a heap buffer, and no realloc-doubling spike while it
        // grows (see docs/issues/local-engine-reload-publish-transient.md).
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

    #[allow(clippy::too_many_arguments)] // the PyO3 keyword surface; `run_query` behind it takes 9
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

        // Parse the query, bind it against this archive's vocabs, and consume the
        // plane-expressible part (colors/identity/types) into a bitmap expression
        // run_query evaluates in a few hundred word ops instead of per-card
        // dispatch. Shared verbatim with explain()/explain_analyze() — see
        // bind_and_split_filter.
        let params = QueryParams::from_strs(unique, prefer, orderby, direction, limit, offset);
        let (plane_expr, mut filter_expr, sort_bound, unsplit) = bind_and_split_filter(py, filters, unique, data, params.sort_col)?;

        let ctx = QueryCtx::from(data);
        // `run_query`'s string→enum adaptation, done here so the filter's sort-column bound can ride on
        // the params: `from_strs` is still the single interpretation of the four strings.
        let (total, page) =
            run_query_routed(&ctx, &params.with_sort_bound(sort_bound), &mut filter_expr, Some(&unsplit), plane_expr.as_ref());

        let matches: Vec<Bound<PyDict>> = page
            .iter()
            .map(|(c, p)| card_to_pydict(py, c, p, &data.strings, &data.coll_vocab, &resolved_fields))
            .collect::<PyResult<Vec<_>>>()?;
        let matches_list = PyList::new(py, matches)?;
        PyTuple::new(py, [total.into_pyobject(py)?.into_any(), matches_list.into_any()])
    }

    /// #745 primitive 1: every applicable plan's predicted cost for this query,
    /// ranked cheapest first — the numbers the router already computes on every
    /// query, just exposed instead of thrown away. Diagnostic only; safe to call
    /// constantly.
    ///
    /// `result[0]` is what `query()` runs in the common case, with one exception:
    /// for a bare-range query (`Prep::Range` acquire), a materializing plan's cost
    /// here is the router's coarse pre-materialize estimate, and the router may
    /// lazily re-materialize and re-choose on exact features at dispatch — so the
    /// executed plan can differ from `result[0]`. See the free `explain` fn's doc.
        #[allow(clippy::too_many_arguments)] // the PyO3 keyword surface; the free `explain` fn it calls takes 4
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (*, filters, unique="card", prefer="default", orderby="edhrec", direction="asc", limit=100, offset=0))]
    fn explain<'py>(
        &self,
        py: Python<'py>,
        filters: &Bound<PyAny>,
        unique: &str,
        prefer: &str,
        orderby: &str,
        direction: &str,
        limit: usize,
        offset: usize,
    ) -> PyResult<Bound<'py, PyDict>> {
        let mmap = self.get_mmap()?;
        // Safety: see query()'s access_unchecked justification.
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
        let params = QueryParams::from_strs(unique, prefer, orderby, direction, limit, offset);
        let (plane_expr, mut filter_expr, sort_bound, unsplit) = bind_and_split_filter(py, filters, unique, data, params.sort_col)?;

        // `prefer` is accepted and passed through, but note what it does NOT yet change:
        // `cost::plan_cost`/`PlanFeatures` still don't read it, so the argmin and every
        // `predicted_ns` remain prefer-blind. That is a real gap, now measurable rather
        // than merely asserted — `prefer` genuinely changes the work done, since it picks
        // each card's representative printing and `Prefer::Default` alone lets the match
        // kernels early-break on the first qualifying printing instead of scoring every
        // printing of the card. Measured over the plane acquire (exact narrowing, so the
        // early break is the whole per-card cost), `scan_units` grades 1.00 against
        // `printings_examined` under `default` and 0.32 under each of the other four — one
        // feature value cannot be right for both. Taking the parameter is what lets a
        // prefer-aware feature be graded here at all; until one exists, an `explain` for a
        // non-default `prefer` still predicts the default's numbers.
        let (facts, estimates) =
            explain(&QueryCtx::from(data), &params.with_sort_bound(sort_bound), &mut filter_expr, Some(&unsplit), plane_expr.as_ref());

        let rows: Vec<Bound<PyDict>> = estimates.iter().map(|e| plan_estimate_to_pydict(py, e)).collect::<PyResult<Vec<_>>>()?;
        let out = PyDict::new(py);
        out.set_item("acquire", acquire_facts_to_pydict(py, &facts)?)?;
        out.set_item("plans", PyList::new(py, rows)?)?;
        Ok(out)
    }

    /// #745 primitive 2: run every applicable plan `num_warmups + num_trials`
    /// times each (raw per-trial nanoseconds, not pre-reduced — see `explain_analyze`'s
    /// doc comment for why), alongside the predicted cost `explain` would report
    /// for the same plan. Not on the default query path: this multiplies work by
    /// the number of applicable plans, so it's for ad hoc/interactive diagnosis,
    /// not every request.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (*, filters, unique="card", prefer="default", orderby="edhrec", direction="asc", limit=100, offset=0, num_warmups=3, num_trials=10))]
    fn explain_analyze<'py>(
        &self,
        py: Python<'py>,
        filters: &Bound<PyAny>,
        unique: &str,
        prefer: &str,
        orderby: &str,
        direction: &str,
        limit: usize,
        offset: usize,
        num_warmups: usize,
        num_trials: usize,
    ) -> PyResult<Bound<'py, PyDict>> {
        let mmap = self.get_mmap()?;
        // Safety: see query()'s access_unchecked justification.
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
        let params = QueryParams::from_strs(unique, prefer, orderby, direction, limit, offset);
        let (plane_expr, filter_expr, sort_bound, unsplit) = bind_and_split_filter(py, filters, unique, data, params.sort_col)?;

        // Release the GIL for the timing loop: it's pure Rust (no Python calls) and
        // runs plans × (warmups + trials) executions, so a long explain_analyze
        // shouldn't block other Python threads. The bind above and the PyDict
        // conversion below stay on the GIL.
        let ctx = QueryCtx::from(data);
        let params = params.with_sort_bound(sort_bound);
        let (facts, trials) = py.detach(|| explain_analyze(&ctx, &params, &filter_expr, Some(&unsplit), plane_expr.as_ref(), num_warmups, num_trials));

        let rows: Vec<Bound<PyDict>> = trials.iter().map(|t| plan_trial_to_pydict(py, t)).collect::<PyResult<Vec<_>>>()?;
        let out = PyDict::new(py);
        out.set_item("acquire", acquire_facts_to_pydict(py, &facts)?)?;
        out.set_item("plans", PyList::new(py, rows)?)?;
        Ok(out)
    }

    /// The formats whose printings actually disagree, as `{format: shift}` — the data behind
    /// `CardData::divergent_formats`, so a harness can check the claim that only `oldschool` diverges
    /// against a real store rather than by re-reading the corpus JSONL. Empty when the archive is
    /// missing or from another build; `{}` also legitimately means "no format diverges".
    fn divergent_formats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        let Ok(mmap) = self.get_mmap() else { return Ok(d) };
        // Safety: see query()'s access_unchecked justification.
        let data = unsafe { rkyv::access_unchecked::<Archived<CardData>>(archive_payload(&mmap)) };
        let mask = u64::from(data.indexes.planes.divergent_formats);
        for (format, shift) in data.format_shifts.iter() {
            // Two bits per format, so a format is divergent if either of its bits ever differed.
            if mask >> *shift & 0b11 != 0 {
                d.set_item(format.as_str(), *shift)?;
            }
        }
        Ok(d)
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
#[cfg(test)]
mod bench_mana;
#[cfg(test)]
mod bench_verify_cost;
#[cfg(test)]
mod bench_text_search;
#[cfg(test)]
mod bench_iter_dispatch;
#[cfg(test)]
mod bench_posting_intersect;
#[cfg(test)]
mod bench_intersect_order;
#[cfg(test)]
mod bench_and_best;
#[cfg(test)]
mod bench_narrow_alloc;
#[cfg(test)]
mod bench_word_dict_scan;
#[cfg(test)]
mod bench_card_dedup;
#[cfg(test)]
mod bench_compose_paging;
#[cfg(test)]
mod bench_compose_card_projection;
#[cfg(test)]
mod bench_candidate_materialize;
#[cfg(test)]
mod bench_loop_design;
#[cfg(test)]
mod bench_gather_loop;
#[cfg(test)]
mod bench_streamed_loop;
#[cfg(test)]
mod bench_membership_check;
#[cfg(test)]
mod bench_expand_materialize;
