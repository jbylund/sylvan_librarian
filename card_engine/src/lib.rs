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
        } else if let Some(s) = start.take() {
            if i - s >= 4 {
                emit(&text[s..i]);
            }
        }
    }
    if let Some(s) = start {
        if bytes.len() - s >= 4 {
            emit(&text[s..]);
        }
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
        let mut acc = planes.swap_remove(0);
        for p in &planes {
            for (a, b) in acc.iter_mut().zip(p) {
                *a &= *b;
            }
        }
        return bitmap_card_ids(&acc);
    }
    postings.sort_by_key(Vec::len);
    let mut result = postings.swap_remove(0);
    for p in &planes {
        result.retain(|&id| (p[(id >> 6) as usize] >> (id & 63)) & 1 != 0);
    }
    for p in &postings {
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

/// build_tag_index with the printing-space selectivity threshold applied at
/// build time: values whose posting would be declined by the range guard
/// anyway (frame:2015 covers 66% of printings) are simply not stored — the
/// absent-key convention already means "no narrowing", so dropped and unknown
/// values both fall back to the scan. Third application of the threshold
/// after the range guard (#609) and rarity's union ceiling (#618).
fn build_thresholded_tag_index<T>(rows: &[T], vocab: &[String], get_ids: impl Fn(&T) -> &Vec<u16>) -> TagIndex {
    let mut idx = build_tag_index(rows, vocab, get_ids);
    let n = rows.len();
    idx.retain(|_, postings| !range_too_broad_to_narrow(postings.len(), n));
    idx
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
            if w.iter().all(|c| c.is_ascii_lowercase()) {
                if let Some(&i) = map.get(w) {
                    fp |= 1u128 << i;
                }
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
    let mut dense_of: HashMap<u32, u32> = HashMap::new();
    let mut gids: Vec<u32> = Vec::new();
    let mut counts: Vec<u32> = Vec::new();
    for p in printings {
        let gid = p.flavor_text_lower_id;
        if gid == NONE_STR {
            continue;
        }
        let d = *dense_of.entry(gid).or_insert_with(|| {
            gids.push(gid);
            counts.push(0);
            (gids.len() - 1) as u32
        });
        counts[d as usize] += 1;
    }
    let n = gids.len();
    let mut offsets = vec![0u32; n + 1];
    for i in 0..n {
        offsets[i + 1] = offsets[i] + counts[i];
    }
    let mut cursor = offsets.clone();
    let mut out = vec![0u32; offsets[n] as usize];
    for (i, p) in printings.iter().enumerate() {
        let gid = p.flavor_text_lower_id;
        if gid == NONE_STR {
            continue;
        }
        let d = dense_of[&gid] as usize;
        out[cursor[d] as usize] = i as u32;
        cursor[d] += 1;
    }
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
fn expand_flavor_ids(idx: &Archived<FlavorIndex>, dense_ids: &[u32]) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::new();
    for &d in dense_ids {
        let start = u32::from(idx.offsets[d as usize]) as usize;
        let end = u32::from(idx.offsets[d as usize + 1]) as usize;
        out.extend(idx.printings[start..end].iter().map(|x| u32::from(*x)));
    }
    out.sort_unstable();
    out
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

fn build_sort_permutations(cards: &[OracleCard], printings: &[Printing], offsets: &[u32]) -> SortPermutations {
    let perm = |get: &dyn Fn(&OracleCard) -> Option<f32>, descending: bool| -> Vec<u32> {
        let mut ids: Vec<u32> = (0..cards.len() as u32).collect();
        ids.sort_unstable_by_key(|&i| {
            let c = &cards[i as usize];
            let pk = get(c).map_or(u32::MAX, |v| f32_sort_bits(if descending { -v } else { v }));
            let e = c.edhrec_rank.unwrap_or(u32::MAX);
            // Canonical secondary: the first (store-preferred) printing's
            // default prefer score, matching sort_key_bits' third component
            // for the printing the default prefer chooses.
            let first = offsets[i as usize] as usize;
            let sc = printings
                .get(first)
                .and_then(|p| p.prefer_score)
                .map_or(u32::MAX, |v| f32_sort_bits(-v));
            (((pk as u128) << 64) | ((e as u128) << 32) | (sc as u128), i)
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

// ─── Printing-space range indexes (released_at, price, collector number) ─────
// Sorted (value, printing idx); binary-searched ranges answer range filters in
// printing space. Printings without the value are absent (they can never
// satisfy a comparison — SQL NULL semantics). Dates store yyyymmdd directly;
// collector numbers store the extracted int; prices store raw integer cents
// directly (see Printing::price_usd's doc comment) — no f32_sort_bits
// encoding needed, cents are already a natural, monotonic u32.

type PrintingRangeIndex = Vec<(u32, u32)>;

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

fn build_range_index(printings: &[Printing], get: impl Fn(&Printing) -> Option<u32>) -> PrintingRangeIndex {
    let mut idx: PrintingRangeIndex = printings
        .iter()
        .enumerate()
        .filter_map(|(i, p)| get(p).map(|v| (v, i as u32)))
        .collect();
    idx.sort_unstable();
    idx
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
fn range_candidates(idx: &Archived<PrintingRangeIndex>, lo: u32, hi: u32) -> Option<Vec<u32>> {
    let s = idx.partition_point(|p| u32::from(p.0) < lo);
    let e = idx.partition_point(|p| u32::from(p.0) < hi);
    if range_too_broad_to_narrow(e - s, idx.len()) {
        return None;
    }
    let mut result: Vec<u32> = idx[s..e].iter().map(|p| u32::from(p.1)).collect();
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
fn range_narrowed(idx: &Archived<PrintingRangeIndex>, lo: u32, hi: u32, n_printings: usize, broad_ok: bool, exact: bool) -> Option<Narrowed> {
    let s = idx.partition_point(|p| u32::from(p.0) < lo);
    let e = idx.partition_point(|p| u32::from(p.0) < hi);
    let k = e - s;
    if !range_too_broad_to_narrow(k, idx.len()) {
        let mut result: Vec<u32> = idx[s..e].iter().map(|p| u32::from(p.1)).collect();
        result.sort_unstable();
        return Some(Narrowed { set: Candidates::Printings(result), tight: exact });
    }
    if !broad_ok {
        return None; // nothing downstream would consume the bitmap — pre-#636 behavior
    }
    if k <= idx.len() - k {
        let bits = scatter_bits(idx[s..e].iter().map(|p| u32::from(p.1)), n_printings);
        return Some(Narrowed { set: Candidates::PrintingBits(bits), tight: exact });
    }
    let mut bits = scatter_bits(
        idx[..s].iter().chain(idx[e..].iter()).map(|p| u32::from(p.1)),
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
            if let Some(r) = p.card_rarity_int {
                if (r as usize) < idx.len() {
                    mask |= 1 << r;
                }
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
    let keep = |r: f64| match op {
        CmpOp::Eq => r == val,
        CmpOp::Lt => r < val,
        CmpOp::Le => r <= val,
        CmpOp::Gt => r > val,
        CmpOp::Ge => r >= val,
        CmpOp::Ne => false,
    };
    let buckets: Vec<usize> = (0..idx.len()).filter(|&r| keep(r as f64)).collect();
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
    let keep = |r: f64| match op {
        CmpOp::Eq => r == val,
        CmpOp::Lt => r < val,
        CmpOp::Le => r <= val,
        CmpOp::Gt => r > val,
        CmpOp::Ge => r >= val,
        CmpOp::Ne => r != val,
    };
    let wpp = words_per_plane(n_cards);
    let mut bits = vec![0u64; wpp];
    for b in 0..RARITY_INTERIOR {
        if keep(b as f64) {
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
    frame_data:     TagIndex,        // printing space (selectivity-thresholded)
    artists:        ArtistIndex,     // printing space (CSR by artist vocab id)
    flavor:         FlavorIndex,     // printing space (CSR by dense flavor text id)
    set_codes:      TagIndex,        // printing space
    released_at:    PrintingRangeIndex,       // printing space
    price_usd:      PrintingRangeIndex,       // printing space (integer cents, already order-preserving)
    collector_number: PrintingRangeIndex,     // printing space (extracted int)
    sort_perms:     SortPermutations,          // card space (streamed selection)
    artwork_groups: Vec<u16>,                  // card space: distinct illustration groups
    // printing space: printing_id -> card_id, direct lookup. Replaces a
    // partition_point search on `offsets` in cards_of_printings' hot paths —
    // see docs/issues/00690-engine-direct-projection-arrays.md.
    printing_to_card: Vec<u32>,
    planes:         BitPlanes,                 // card space: transposed low-cardinality dims (#630)
    border_printing: BorderPrintingPlanes,     // printing space: exact bit-per-printing border (#724)
    rarity_printing: RarityPrintingPlanes,     // printing space: exact bit-per-printing rarity (#724)
    name_bigrams:   NameBigramIndex,           // card space: exact 2-byte name containment (#639)
    legal_divergent: Vec<u16>,                // card space: ids with divergent legality (#630 phase 2), postings not a plane — see build_divergent_ids
}

impl Default for CardIndexes {
    fn default() -> Self {
        CardIndexes {
            name_trigram:   SortedTrigramIndex::default(),
            oracle_trigram: OracleTextIndex::default(),
            cmc:            Vec::new(),
            power:          Vec::new(),
            toughness:      Vec::new(),
            rarity:         Default::default(),
            subtypes:       HashMap::new(),
            keywords:       HashMap::new(),
            oracle_tags:    HashMap::new(),
            art_tags:       HashMap::new(),
            is_tags:        HashMap::new(),
            frame_data:     HashMap::new(),
            artists:        ArtistIndex::default(),
            flavor:         FlavorIndex::default(),
            set_codes:      HashMap::new(),
            released_at:    Vec::new(),
            price_usd:      Vec::new(),
            collector_number: Vec::new(),
            sort_perms:     SortPermutations::default(),
            artwork_groups: Vec::new(),
            printing_to_card: Vec::new(),
            planes:         BitPlanes::default(),
            border_printing: BorderPrintingPlanes::default(),
            rarity_printing: RarityPrintingPlanes::default(),
            name_bigrams:   NameBigramIndex::default(),
            legal_divergent: Vec::new(),
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

    /// Approximate member count (exact for both representations).
    fn len(&self) -> usize {
        match self {
            Candidates::Cards(v) | Candidates::Printings(v) => v.len(),
            Candidates::CardBits(b) | Candidates::PrintingBits(b) => b.iter().map(|w| w.count_ones() as usize).sum(),
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
        Some(Narrowed { set, tight: true })
    }

    fn loose(set: Candidates) -> Option<Narrowed> {
        Some(Narrowed { set, tight: false })
    }

    /// Project into card space. Printing→card projection is an existence
    /// projection ("some printing matches"), which loses tightness.
    fn into_card_space(self, offsets: &AOffsets, printing_to_card: &AOffsets) -> Narrowed {
        let n_cards = offsets.len().saturating_sub(1);
        match self.set {
            Candidates::Cards(_) | Candidates::CardBits(_) => self,
            Candidates::Printings(v) => {
                Narrowed { set: Candidates::Cards(cards_of_printings(offsets, printing_to_card, &v)), tight: false }
            }
            Candidates::PrintingBits(b) => {
                Narrowed { set: Candidates::CardBits(printing_bits_to_card_bits(&b, offsets, n_cards)), tight: false }
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
    let vec_result = (!vecs.is_empty()).then(|| {
        vecs.sort_unstable_by_key(Vec::len);
        let mut result = vecs.swap_remove(0);
        for v in vecs {
            if result.is_empty() {
                break;
            }
            result = intersect_sorted(&result, &v);
        }
        result
    });
    let bits_result = bit_sets.split_first().map(|(first, rest)| {
        let mut acc = first.clone();
        for b in rest {
            and_bits_into(&mut acc, b);
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
    Some(Narrowed { set, tight })
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
    Some(Narrowed { set, tight })
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
        // 2-byte name needles resolve exactly through the bigram index.
        FilterExpr::TextContains { field: TextSearchField::NameLower, word } if word.len() == 2 => Some(false),
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
        FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => Some(true),
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
) -> (Option<Candidates>, bool) {
    let n_cards = offsets.len().saturating_sub(1);
    let n_printings = if n_cards == 0 { 0 } else { u32::from(offsets[n_cards]) as usize };
    match narrow_rec(filter, indexes, offsets, cards, false) {
        None => (None, false),
        Some(n) => {
            let printing_space = n.set.is_printing_space();
            let domain = if printing_space { n_printings } else { n_cards };
            if n.set.len() <= domain - domain / 4 {
                (Some(n.set), n.tight && !printing_space)
            } else {
                (None, false)
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

/// Evaluation-cost rank for And children: cheap sources first (postings,
/// planes, card numerics, trigram lookups), printing-space ranges second
/// (their broad form pays an O(k) scatter), complements last (broad by
/// construction, useful only when nothing else narrowed).
fn and_child_rank(f: &FilterExpr) -> u8 {
    match f {
        FilterExpr::Not(_) => 2,
        FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => 1,
        FilterExpr::NumericCmp { lhs, rhs, .. } => {
            let field = |e: &NumExpr| matches!(e, NumExpr::Field(NumField::PriceUsd | NumField::CollectorNumberInt));
            if field(lhs) || field(rhs) { 1 } else { 0 }
        }
        _ => 0,
    }
}

/// `broad_ok` says whether a broad printing-range child may materialize its
/// bitmap: true under Or (the union consumes it) and Not (the complement
/// trick needs it), false where nothing would — a lone broad set at the root
/// or in a candidate-less And is discarded anyway, so the scatter would be
/// pure waste (the 10x And regressions of the first benchmark round).
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
    {
        if let Some(pe) = compile_plane(filter, &indexes.planes, &indexes.oracle_trigram.words) {
            let mut bits: Vec<u64> = Vec::new();
            eval_planes(&pe, &indexes.planes, &mut bits);
            // Legality's planes are existence projections, not true-for-
            // every-printing facts (docs/issues/engine-legality-divergent-
            // carveout.md) -- `tight`'s contract needs the latter (see
            // `Narrowed`'s doc and the dedicated `Legality` arms below), so a
            // compiled expression touching them can only narrow loosely here,
            // same as if it had fallen through to those arms directly.
            return if plane_expr_is_existential(&pe) {
                Narrowed::loose(Candidates::CardBits(bits))
            } else {
                Narrowed::tight(Candidates::CardBits(bits))
            };
        }
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
                    Narrowed::tight(Candidates::Cards(expand_text_ids(&indexes.oracle_trigram, &text_ids)))
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
                    for cid in expand_text_ids(&indexes.oracle_trigram, &sparse_text_ids(sparse)) {
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
            // Trigram candidates are supersets (false positives until the walk
            // verifies), so these sets are loose.
            match field {
                TextSearchField::NameLower => trigram_candidates(&indexes.name_trigram, word)
                    .and_then(|v| Narrowed::loose(Candidates::Cards(v))),
                // Oracle postings are in dense text-id space (see OracleTextIndex);
                // intersect there, then expand the survivors to card indices
                // through the CSR table.
                _ => trigram_candidates(&indexes.oracle_trigram.trigrams, word)
                    .and_then(|text_ids| Narrowed::loose(Candidates::Cards(expand_text_ids(&indexes.oracle_trigram, &text_ids)))),
            }
        }

        FilterExpr::NumericCmp { lhs, op, rhs } => {
            // Card-space numeric postings are tight: every posted card
            // satisfies the comparison at card level. Rarity postings are
            // loose in the sense that matters for Not: a posted card can have
            // other printings that do NOT satisfy the comparison, so the
            // complement would wrongly exclude cards `-rarity:x` matches.
            let numeric = |idx, op, v: &f64| numeric_candidates(idx, op, *v).and_then(|c| Narrowed::tight(Candidates::Cards(c)));
            let rarity = |op, v: &f64| narrow_rarity(indexes, n_cards, op, *v);
            // Same shape as `cn` below now that price is integer cents, not lossy f32 dollars --
            // the only price-specific step is snapping the *PRICE_CENTS_PER_DOLLAR conversion
            // against its own floating-point noise before delegating to int_range_bounds.
            let price = |op, v: &f64| match int_range_bounds(op, snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))? {
                None => Narrowed::tight(Candidates::Printings(Vec::new())),
                Some((lo, hi)) => range_narrowed(&indexes.price_usd, lo, hi, n_printings, broad_ok, true),
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
                (NumExpr::Field(NumField::PriceUsd), NumExpr::Const(v)) => price(*op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::PriceUsd)) => price(flip_op(*op), v),
                (NumExpr::Field(NumField::CollectorNumberInt), NumExpr::Const(v)) => cn(*op, v),
                (NumExpr::Const(v), NumExpr::Field(NumField::CollectorNumberInt)) => cn(flip_op(*op), v),
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
        FilterExpr::Not(inner)
            if matches!(inner.as_ref(), FilterExpr::Legality { shift: Some(_), expected } if status_plane_bases(*expected).is_some()) =>
        {
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
        FilterExpr::Not(inner)
            if matches!(
                inner.as_ref(),
                FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), rhs: NumExpr::Const(_), .. }
                    | FilterExpr::NumericCmp { lhs: NumExpr::Const(_), rhs: NumExpr::Field(NumField::RarityInt), .. }
            ) =>
        {
            let FilterExpr::NumericCmp { lhs, op, rhs } = inner.as_ref() else { unreachable!() };
            match (lhs, rhs) {
                (NumExpr::Field(NumField::RarityInt), NumExpr::Const(v)) => narrow_rarity(indexes, n_cards, negate_op(*op), *v),
                (NumExpr::Const(v), NumExpr::Field(NumField::RarityInt)) => narrow_rarity(indexes, n_cards, negate_op(flip_op(*op)), *v),
                _ => unreachable!(),
            }
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
            // `complete` marks indexes that post every occurrence of every
            // value — all of them except frame_data, whose dense values are
            // deliberately dropped at build (#628), so absence proves nothing
            // there.
            let (idx, card_space, complete) = match field {
                CollField::Subtypes   => (&indexes.subtypes,    true,  true),
                CollField::Keywords   => (&indexes.keywords,    true,  true),
                CollField::OracleTags => (&indexes.oracle_tags, true,  true),
                CollField::ArtTags    => (&indexes.art_tags,    false, true),
                CollField::IsTags     => (&indexes.is_tags,     false, true),
                CollField::FrameData  => (&indexes.frame_data,  false, false),
            };
            // Ge's postings are exact for Ge itself; for Eq/Gt they're only a
            // loose superset (they prove `contains(value)` but not the length
            // condition), so the residual `matches()` check the driver always
            // runs is load-bearing for those two ops.
            let mk = |set| if matches!(op, CmpOp::Ge) { Narrowed::tight(set) } else { Narrowed::loose(set) };
            match idx.get(value.as_str()) {
                // A value with no postings in a complete index proves
                // `contains(value)` false for every row, which makes Ge, Eq,
                // and Gt all provably empty alike — no row can satisfy any of
                // them without first satisfying containment. Exact for all
                // three ops, not just Ge: `is:permanent` spent 0.6 ms
                // full-scanning to return zero results.
                None if complete => {
                    Narrowed::tight(if card_space { Candidates::Cards(Vec::new()) } else { Candidates::Printings(Vec::new()) })
                }
                None => None,
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
            Narrowed::tight(Candidates::Printings(expand_artist_ids(&indexes.artists, ids)))
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
            Narrowed::tight(Candidates::Printings(expand_flavor_ids(flavor, dense_ids)))
        }

        FilterExpr::TextExact { field: TextField::SetCode, op: CmpOp::Eq, value } => {
            // A set code absent from the index appears on no printing: narrowing
            // to the empty set is exact, matching the tag-index convention would
            // be None, but unlike tags the index covers every non-empty code.
            Narrowed::tight(Candidates::Printings(
                indexes.set_codes.get(value.as_str()).map_or_else(Vec::new, |v| v.iter().map(|x| u32::from(*x)).collect()),
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
            let mut ranked: Vec<(u8, &FilterExpr)> = children.iter().map(|c| (and_child_rank(c), c)).collect();
            ranked.sort_by_key(|(r, _)| *r);
            let mut card_sets: Vec<Narrowed> = Vec::new();
            let mut printing_sets: Vec<Narrowed> = Vec::new();
            // Tightness of the And requires every child to be represented in
            // the intersection: a member of a partial intersection need not
            // satisfy the skipped children, and a complement taken over a
            // falsely-tight set would drop real matches of the negation.
            let mut every_child_included = true;
            for (rank, c) in ranked {
                let best = card_sets.iter().chain(printing_sets.iter()).map(|n| n.set.len()).min();
                if rank > 0 && best.is_some_and(|b| b <= *AND_SKIP_THRESHOLD) {
                    every_child_included = false;
                    break;
                }
                if rank == 2 && !(card_sets.is_empty() && printing_sets.is_empty()) {
                    every_child_included = false;
                    continue; // complements are broad; they only pay as the sole source
                }
                let child_broad_ok = match rank {
                    1 => !printing_sets.is_empty(),
                    _ => broad_ok,
                };
                if let Some(n) = narrow_rec(c, indexes, offsets, cards, child_broad_ok) {
                    // A child covering most of its domain barely narrows the
                    // intersection; skipping it is advisory-sound and avoids
                    // paying its projection/materialization for ~nothing.
                    let domain = if n.set.is_printing_space() { n_printings } else { n_cards };
                    if n.set.len() > domain - domain / 4 {
                        every_child_included = false;
                        continue;
                    }
                    if n.set.is_printing_space() { printing_sets.push(n) } else { card_sets.push(n) }
                } else {
                    every_child_included = false;
                }
            }
            let cards = and_all(card_sets);
            let printings = and_all(printing_sets);
            let seal = |mut n: Narrowed| {
                n.tight &= every_child_included;
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
                        Candidates::PrintingBits(b) if p.set.len() > n_printings / 4 => {
                            let _ = b;
                            None
                        }
                        _ => Some(seal(p)),
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
        SortCol::Cmc        => card.cmc.as_ref().map(|v| u8::from(*v) as f32),
        SortCol::Power      => card.creature_power.as_ref().map(|v| i8::from(*v) as f32),
        SortCol::Toughness  => card.creature_toughness.as_ref().map(|v| i8::from(*v) as f32),
        SortCol::Rarity     => p.card_rarity_int.as_ref().map(|v| u8::from(*v) as f32),
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
    a.0.cmp(&b.0).then_with(|| a.2.cmp(&b.2))
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

/// Matches this card contributes: 0/1 for Card mode (existence, short-circuit),
/// passing printings for Printing mode, distinct illustrations with a passing
/// printing for Artwork mode. `seen_words` is a reused scratch buffer: a
/// fixed-size bitmask indexed by each printing's dense `artwork_group_id`
/// (#629), one bit per group, `word = gid / 64` -- zeroed in full every card
/// (cheap: ARTWORK_GROUP_WORDS is tiny), never resized.
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
#[inline(always)]
fn card_match_count(
    card: &AOracleCard,
    cid: u32,
    printings: &[APrinting],
    start: usize,
    end: usize,
    all_match: bool,
    residual: &[&FilterExpr],
    residual_is_or: bool,
    mode: Mode,
    strings: &AStrings,
    existential_plane: Option<(&PlaneExpr, &Archived<BitPlanes>)>,
    seen_words: &mut [u64; ARTWORK_GROUP_WORDS],
) -> u32 {
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
                    return u32::from(start < end);
                }
                for pid in start..end {
                    if FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or) {
                        return 1;
                    }
                }
                0
            }
            Mode::Printing => {
                if all_match {
                    return (end - start) as u32;
                }
                let mut n = 0u32;
                for pid in start..end {
                    if FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or) {
                        n += 1;
                    }
                }
                n
            }
            Mode::Artwork => {
                seen_words.fill(0);
                for pid in start..end {
                    if !all_match && !FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or) {
                        continue;
                    }
                    let gid = u16::from(printings[pid].artwork_group_id) as usize;
                    seen_words[gid / 64] |= 1u64 << (gid % 64);
                }
                seen_words.iter().map(|w| w.count_ones()).sum()
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
                    return 1;
                }
            }
            0
        }
        Mode::Printing => {
            let mut n = 0u32;
            for pid in start..end {
                if satisfies(pid) {
                    n += 1;
                }
            }
            n
        }
        Mode::Artwork => {
            seen_words.fill(0);
            for pid in start..end {
                if !satisfies(pid) {
                    continue;
                }
                let gid = u16::from(printings[pid].artwork_group_id) as usize;
                seen_words[gid / 64] |= 1u64 << (gid % 64);
            }
            seen_words.iter().map(|w| w.count_ones()).sum()
        }
    }
}

/// Emit this card's matches as (sort key, cid, pid) tuples — the per-card body
/// of the gathered path, shared by the streamed path for page cards.
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
#[inline(always)]
fn push_card_matches(
    card: &AOracleCard,
    cid: u32,
    printings: &[APrinting],
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
    group_best: &mut Vec<Option<(u32, f64)>>,
    touched: &mut Vec<u16>,
) {
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
            let chosen: Option<u32> = if let Some((pe, planes)) = existential_plane {
                let satisfies = |pid: usize| {
                    eval_plane_expr_for_printing(pe, planes, cid, &printings[pid], strings)
                        && (all_match || FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or))
                };
                if matches!(prefer, Prefer::Default) {
                    (start..end).find(|&pid| satisfies(pid)).map(|pid| pid as u32)
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
                    chosen.map(|(pid, _)| pid)
                }
            } else if matches!(prefer, Prefer::Default) {
                let mut found: Option<u32> = None;
                for pid in start..end {
                    if all_match || FilterExpr::residual_matches(card, &printings[pid], strings, residual, residual_is_or) {
                        found = Some(pid as u32);
                        break;
                    }
                }
                found
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
                chosen.map(|(pid, _)| pid)
            };
            if let Some(pid) = chosen {
                out.push((sort_key_bits(card, &printings[pid as usize], sort_col, descending), cid, pid));
            }
        }
        Mode::Printing => {
            for pid in start..end {
                let p = &printings[pid];
                if !all_match && !FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) { continue; }
                out.push((sort_key_bits(card, p, sort_col, descending), cid, pid as u32));
            }
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
            for pid in start..end {
                let p = &printings[pid];
                if !all_match && !FilterExpr::residual_matches(card, p, strings, residual, residual_is_or) { continue; }
                let gid = u16::from(p.artwork_group_id) as usize;
                if group_best.len() <= gid {
                    group_best.resize(gid + 1, None);
                }
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
            for &gid in touched.iter() {
                let (bp, _) = group_best[gid as usize].take().unwrap();
                out.push((sort_key_bits(card, &printings[bp as usize], sort_col, descending), cid, bp));
            }
        }
    }
}

/// Below this many matches the gathered path is already microseconds and
/// byte-identical to the pre-streaming behavior; above it, walking the
/// permutation (a fixed ~n bit-tests over the counts array) plus per-page-card
/// emission wins. Same measured-constant philosophy as MAX_NARROW_FRACTION.
/// Calibrated (scripts/bench_cost_guards.py, `cmc<K` with exactly dialable
/// card counts): the crossover wanders 0.6k-1.1k across reps and corpus
/// sizes with branch differences under the ~5% noise floor throughout that
/// band; 1,024 sits at the spread's upper (gather/simple) edge, and past it
/// streaming's win grows fast (~1.8× by 8k), so the trigger stays put.
static STREAM_MIN_MATCHES: LazyLock<usize> = LazyLock::new(|| guard_env("CARD_ENGINE_STREAM_MIN_MATCHES", 1_024));

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
fn bare_range_bounds<'i>(
    filter: &FilterExpr,
    indexes: &'i Archived<CardIndexes>,
) -> Option<(&'i Archived<PrintingRangeIndex>, u32, u32)> {
    match filter {
        FilterExpr::NumericCmp { lhs, op, rhs } => {
            // Only price/collector-number are printing-range-indexed; cmc/power/toughness/rarity are
            // card-space and belong to other paths. Value snapping matches narrow_rec's `price` closure.
            let (idx, op, value) = match (lhs, rhs) {
                (NumExpr::Field(NumField::PriceUsd), NumExpr::Const(v)) => (&indexes.price_usd, *op, snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR)),
                (NumExpr::Const(v), NumExpr::Field(NumField::PriceUsd)) => (&indexes.price_usd, flip_op(*op), snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR)),
                (NumExpr::Field(NumField::CollectorNumberInt), NumExpr::Const(v)) => (&indexes.collector_number, *op, *v),
                (NumExpr::Const(v), NumExpr::Field(NumField::CollectorNumberInt)) => (&indexes.collector_number, flip_op(*op), *v),
                _ => return None,
            };
            match int_range_bounds(op, value)? {
                None => Some((idx, 0, 0)),
                Some((lo, hi)) => Some((idx, lo, hi)),
            }
        }
        FilterExpr::DateCmp { op, value } => {
            let (lo, hi) = date_range_bounds(*op, *value)?;
            Some((&indexes.released_at, lo, hi))
        }
        FilterExpr::YearCmp { op, year } => {
            let (lo, hi) = year_range_bounds(*op, *year)?;
            Some((&indexes.released_at, lo, hi))
        }
        _ => None,
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
    idx: &Archived<PrintingRangeIndex>,
    lo: u32,
    hi: u32,
    indexes: &Archived<CardIndexes>,
    n_cards: usize,
    n_printings: usize,
) -> (Vec<u64>, Vec<u64>) {
    let ptc = &indexes.printing_to_card;
    let s = idx.partition_point(|p| u32::from(p.0) < lo);
    let e = idx.partition_point(|p| u32::from(p.0) < hi);
    let mut range_pbits = vec![0u64; n_printings.div_ceil(64)];
    let mut card_bits = vec![0u64; n_cards.div_ceil(64)];
    for p in idx[s..e].iter() {
        let pid = u32::from(p.1) as usize;
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
#[allow(clippy::too_many_arguments)]
fn walk_printing_page<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    leaf: &FilterExpr,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    perm: &Archived<Vec<u32>>,
) -> Vec<(&'a AOracleCard, &'a APrinting)> {
    let residual: [&FilterExpr; 1] = [leaf];
    let cmp = |a: &Match, b: &Match| a.0.cmp(&b.0).then_with(|| a.2.cmp(&b.2));
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
        scratch.sort_unstable_by(cmp);
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

/// Page for a `unique=printing` price query ordered by `usd` (the aligned case). The price index's
/// `[s, e)` slice is already value-sorted, so the page's value-buckets are found by count without
/// touching most of the slice; only the bucket(s) the page overlaps are canonically re-sorted
/// (their within-value order in the index is by pid, but the sort key ties on
/// `(edhrec, prefer_score, pid)`, and price ties are large). The tiebreak does not flip with
/// direction, so `descending` only reverses which buckets are walked first; the same
/// `sort_key_bits`/`select_page` comparator orders the collected set, so the result is identical to
/// the gathered path it replaces.
#[allow(clippy::too_many_arguments)]
fn aligned_page<'a>(
    idx: &Archived<PrintingRangeIndex>,
    s: usize,
    e: usize,
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    printing_to_card: &AOffsets,
    descending: bool,
    page_offset: usize,
    limit: usize,
) -> Vec<(&'a AOracleCard, &'a APrinting)> {
    // Walk value-buckets lazily *from the page's starting end* — `s` forward for ascending, `e`
    // backward for descending — forming only the buckets the page touches and stopping once
    // offset+limit items are covered. Whole buckets before the page are skipped by count without
    // collecting them; the untouched remainder of the slice is never scanned at all (so the cost
    // is O(touched buckets), not O(distinct values in the slice)).
    let want = page_offset + limit;
    let mut collected: Vec<u32> = Vec::new();
    let mut cum = 0usize;
    let mut first_touched_cum = 0usize;
    let mut started = false;
    let (mut lo, mut hi) = (s, e);
    while lo < hi {
        // The next value-bucket in page order (a maximal run of equal value).
        let (bs, be) = if descending {
            let v = u32::from(idx[hi - 1].0);
            let mut b = hi - 1;
            while b > lo && u32::from(idx[b - 1].0) == v {
                b -= 1;
            }
            (b, hi)
        } else {
            let v = u32::from(idx[lo].0);
            let mut b = lo + 1;
            while b < hi && u32::from(idx[b].0) == v {
                b += 1;
            }
            (lo, b)
        };
        if descending { hi = bs } else { lo = be }
        let sz = be - bs;
        if !started && cum + sz <= page_offset {
            cum += sz;
            continue;
        }
        if !started {
            started = true;
            first_touched_cum = cum;
        }
        collected.extend((bs..be).map(|t| u32::from(idx[t].1)));
        cum += sz;
        if cum >= want {
            break;
        }
    }
    // Canonically sort the collected touched-bucket printings (small: ~one or two buckets) and
    // window the page — same comparator select_page uses, so ordering matches the gathered path.
    let mut matches: Vec<Match> = collected
        .iter()
        .map(|&pid| {
            let cid = u32::from(printing_to_card[pid as usize]);
            (sort_key_bits(&cards[cid as usize], &printings[pid as usize], SortCol::PriceUsd, descending), cid, pid)
        })
        .collect();
    matches.sort_unstable_by(|a, b| a.0.cmp(&b.0).then_with(|| a.2.cmp(&b.2)));
    let start = page_offset - first_touched_cum;
    let end = (start + limit).min(matches.len());
    matches[start..end].iter().map(|m| (&cards[m.1 as usize], &printings[m.2 as usize])).collect()
}

/// Fast path for a *bare, broad* range predicate under `unique=printing`
/// (docs/issues/local-engine-sorted-range-fastpath.md, PR 1). `total` is `k` from the range
/// index's binary search — no full per-printing count pass — and the page is produced in order
/// without materializing all `k` matches. Returns None (fall through to the general path) for
/// anything it doesn't own: non-printing modes, a plane component, a non-bare/non-range filter, a
/// selective range (the existing narrowing already wins, and restricting the walk to dense
/// predicates keeps its worst case bounded), or an order-by without a card permutation (e.g. the
/// range field itself — deferred).
#[allow(clippy::too_many_arguments)]
fn printing_range_fastpath<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    let (idx, lo, hi) = bare_range_bounds(filter, indexes)?;
    let s = idx.partition_point(|p| u32::from(p.0) < lo);
    let e = idx.partition_point(|p| u32::from(p.0) < hi);
    let k = e - s;
    if !range_too_broad_to_narrow(k, idx.len()) {
        return None; // selective: the existing narrowing path narrows tightly and wins
    }
    // total = matching printings = k (each priced printing in [lo, hi) is one row; NULL-valued
    // printings are absent from the index and don't match). Same value the count pass would sum.
    if k == 0 || page_offset >= k {
        return Some((k, Vec::new()));
    }
    // Aligned: order by the range field itself. `usd` is the only range field that is also a sort
    // column, and only a price predicate makes `idx` the value-sorted permutation for that sort;
    // a non-price predicate ordered by usd has no aligned mapping (and no permutation) — bail.
    if matches!(sort_col, SortCol::PriceUsd) {
        return is_price_leaf(filter)
            .then(|| (k, aligned_page(idx, s, e, cards, printings, &indexes.printing_to_card, descending, page_offset, limit)));
    }
    // The walk reproduces run_query_streamed's *stream* emission (per-card-contiguous), which the
    // general path only uses above STREAM_MIN_MATCHES; at or below it, run_query_streamed gathers
    // and sorts globally, ordering full-sort-key ties across cards by pid instead. Bail there so
    // the fastpath never claims a range the general path would gather. The band is narrow
    // (NARROW_FLOOR < k <= STREAM_MIN_MATCHES, i.e. 1000 < k <= 1024) and only reachable on a tiny
    // index (broad needs k > index_len/4, so index_len < ~4096) -- never in production, where broad
    // means tens of thousands. aligned_page above matches the gathered path directly, so it's exempt.
    if k <= *STREAM_MIN_MATCHES {
        return None;
    }
    let perm = indexes.sort_perms.get(sort_col, descending)?;
    if perm.len() != cards.len() {
        return None;
    }
    Some((k, walk_printing_page(cards, printings, offsets, strings, filter, sort_col, descending, limit, page_offset, perm)))
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
fn is_printing_composable(filter: &FilterExpr, indexes: &Archived<CardIndexes>) -> bool {
    match filter {
        FilterExpr::True => true,
        FilterExpr::And(v) | FilterExpr::Or(v) => v.iter().all(|c| is_printing_composable(c, indexes)),
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, .. } => true,
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(_) }
        | FilterExpr::NumericCmp { lhs: NumExpr::Const(_), op: CmpOp::Eq, rhs: NumExpr::Field(NumField::RarityInt) } => true,
        // Legality via #667's card `_EXISTS` plane + a divergent repair (see `legality_leaf_bits`).
        // Only a plane-backed status (legal/banned/restricted) with a present format; an absent format
        // (`shift: None`) matches nothing and stays on the general path.
        FilterExpr::Legality { shift: Some(_), expected } => status_plane_bases(*expected).is_some(),
        // #731: usd/cn/date range leaves — the in-range index slice scatters into an exact printing
        // bitmap (`range_leaf_bits`). `bare_range_bounds` recognizes the printing-range-indexed shape
        // (and the `[lo,hi)` bounds); a non-range op (`Eq`/`Ne`), a negation, or a card-space field
        // (cmc/power/rarity) yields `None` → stays on the general path. This is what lets a range
        // compose with border/rarity/legality — and range∧range — exactly, in any distinct-on.
        FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. } => {
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

/// #731: the exact printing bitmap for a usd/cn/date range leaf — scatter the value-sorted index
/// slice `[lo, hi)` (the same slice `build_card_range_bits` walks). Index-absent printings (no
/// price/collector-number/date) aren't in the index at all, so they're excluded by construction —
/// this is an intersection with the in-range set, never a complement, so the trivalent-NULL trap that
/// keeps `Not` off the compose path doesn't apply here.
fn range_leaf_bits(idx: &Archived<PrintingRangeIndex>, lo: u32, hi: u32, n_printings: usize) -> Vec<u64> {
    let s = idx.partition_point(|p| u32::from(p.0) < lo);
    let e = idx.partition_point(|p| u32::from(p.0) < hi);
    let mut bits = vec![0u64; n_printings.div_ceil(64)];
    for p in idx[s..e].iter() {
        let pid = u32::from(p.1) as usize;
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

/// The exact printing-space bitmap for a bare legality leaf (`f:modern` etc.), built the #724 way from
/// #667's card-space `_EXISTS` plane plus a divergent repair, rather than a full per-printing legality
/// plane — legality is only ~1.8% divergent (`legal_divergent`), so the card plane broadcast is exact
/// for the other 98.2% and only the divergent cards need per-printing fix-up. `∃-legal` over-sets a
/// divergent card (it sets every printing, but only some are legal), so the repair is **authoritative**:
/// it *overwrites* each divergent printing's bit to its true value (set and clear, re-derived from the
/// printing's own `card_legalities` word — no stored postings), which is why the broadcast needs no
/// pre-masking. Empty if the planes aren't built for this store.
fn legality_leaf_bits(
    shift: u8,
    expected: u64,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    printings: &[APrinting],
    n_printings: usize,
) -> Vec<u64> {
    let n_cards = offsets.len() - 1;
    let Some(card_bits) = legality_candidate_bits(indexes, n_cards, shift, expected, false) else {
        return vec![0u64; words_per_plane(n_printings)];
    };
    let mut pbits = broadcast_card_bits_to_printings(&card_bits, offsets, n_printings);
    // Repair the divergent cards' printings authoritatively — overwrite each bit with the per-printing
    // truth (`(word >> shift) & 0b11 == expected`, the same test filter.rs:1253 applies). Overwriting
    // both directions is what lets the broadcast above over-set them without a pre-clear pass.
    for cid in indexes.legal_divergent.iter() {
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
    pbits
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
            // empty And is vacuously true; start all-ones (tail masked) and intersect each child.
            let mut acc = all_printing_bits(n_printings);
            for child in v.iter() {
                and_bits_into(&mut acc, &compose_printing_bits(child, indexes, offsets, printings, n_printings));
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
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(c) }
        | FilterExpr::NumericCmp { lhs: NumExpr::Const(c), op: CmpOp::Eq, rhs: NumExpr::Field(NumField::RarityInt) } => {
            rarity_leaf_bits(*c, &indexes.rarity_printing, n_printings)
        }
        FilterExpr::Legality { shift: Some(shift), expected } => {
            legality_leaf_bits(*shift, *expected, indexes, offsets, printings, n_printings)
        }
        FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. }
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
fn compose_printing_estimate(
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    offsets: &AOffsets,
    n_printings: usize,
) -> (usize, usize, usize) {
    let popcount = |bits: &[u64]| bits.iter().map(|w| w.count_ones() as usize).sum::<usize>();
    match filter {
        FilterExpr::True => (n_printings, 0, 0),
        FilterExpr::And(v) => v
            .iter()
            .map(|c| compose_printing_estimate(c, indexes, offsets, n_printings))
            .fold((n_printings, 0, 0), |(m, bc, sc), (cm, cbc, csc)| (m.min(cm), bc + cbc, sc + csc)),
        FilterExpr::Or(v) => {
            let (m, bc, sc) = v
                .iter()
                .map(|c| compose_printing_estimate(c, indexes, offsets, n_printings))
                .fold((0usize, 0usize, 0usize), |(m, bc, sc), (cm, cbc, csc)| (m + cm, bc + cbc, sc + csc));
            (m.min(n_printings), bc, sc)
        }
        // Precomputed planes: exact cheap popcount, nothing synthesized.
        FilterExpr::TextExact { field: TextField::Border, op: CmpOp::Eq, value } => {
            (popcount(&border_leaf_bits(value.as_str(), &indexes.border_printing, n_printings)), 0, 0)
        }
        FilterExpr::NumericCmp { lhs: NumExpr::Field(NumField::RarityInt), op: CmpOp::Eq, rhs: NumExpr::Const(c) }
        | FilterExpr::NumericCmp { lhs: NumExpr::Const(c), op: CmpOp::Eq, rhs: NumExpr::Field(NumField::RarityInt) } => {
            (popcount(&rarity_leaf_bits(*c, &indexes.rarity_printing, n_printings)), 0, 0)
        }
        // Legality: estimate from the cheap card ∃-plane popcount scaled to printings; that count is the
        // broadcast-down the fast path pays (a linear pass) → rides `broadcast`.
        FilterExpr::Legality { shift: Some(shift), expected } => {
            let n_cards = offsets.len() - 1;
            let card_exists = legality_candidate_bits(indexes, n_cards, *shift, *expected, false).map_or(0, |b| popcount(&b));
            let est = if n_cards == 0 { 0 } else { card_exists * n_printings / n_cards };
            (est, est, 0)
        }
        // Range: `k` in-range printings from the index partition points (O(log n), no scatter here);
        // matches ≈ k, and k rides `scatter` — the cheap range-slice scatter into the printing bitmap.
        FilterExpr::NumericCmp { .. } | FilterExpr::DateCmp { .. } | FilterExpr::YearCmp { .. }
            if bare_range_bounds(filter, indexes).is_some() =>
        {
            let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("guarded by bare_range_bounds");
            let k = idx.partition_point(|p| u32::from(p.0) < hi) - idx.partition_point(|p| u32::from(p.0) < lo);
            (k, 0, k)
        }
        _ => unreachable!("compose_printing_estimate on a non-composable filter — gated by is_printing_composable"),
    }
}

/// `unique=printing` fast path for a composable printing-space expression (bare `border:`/`r:` or an
/// `AND`/`OR`/`NOT` of them, #724) — the plane analogue of `printing_range_fastpath`: `total` is the
/// composed bitmap's `popcount` (no count pass), and the page is the same `walk_printing_page` (its
/// residual test evaluates the full composed filter, and early-stops). Returns `None` (declines) for
/// a non-composable filter, or a total at/below the stream threshold — where the general path gathers
/// and globally sorts, ordering ties differently (same guard as `printing_range_fastpath`; a sparse
/// value like `border:yellow` falls through here).
#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_arguments)]
fn printing_compose_fastpath<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    filter: &FilterExpr,
    indexes: &Archived<CardIndexes>,
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    if !is_printing_composable(filter, indexes) || !printing_compose_indexes_built(indexes) {
        return None;
    }
    // Compose once, here (never in acquire) — that single build is the only synthesis, and it is paid
    // only because this plan won. The total is the popcount in the query's result space; the page is
    // the one grouped walk at the mode's granularity, using the composed bits as exact membership.
    let pbits = compose_printing_bits(filter, indexes, offsets, printings, printings.len());
    let total: usize = match mode {
        Mode::Printing => pbits.iter().map(|w| w.count_ones() as usize).sum(),
        Mode::Card => printing_bits_to_card_bits(&pbits, offsets, cards.len()).iter().map(|w| w.count_ones() as usize).sum(),
        Mode::Artwork => {
            let base = build_artwork_base(&indexes.artwork_groups);
            let n_artworks = *base.last().expect("artwork_base has n_cards+1 entries") as usize;
            printing_bits_to_artwork_bits(&pbits, printings, &indexes.printing_to_card, &base, n_artworks)
                .iter()
                .map(|w| w.count_ones() as usize)
                .sum()
        }
    };
    if total == 0 || page_offset >= total {
        return Some((total, Vec::new()));
    }
    if total <= *STREAM_MIN_MATCHES {
        return None; // sparse: the general path gathers + globally sorts, ordering ties differently
    }
    let perm = indexes.sort_perms.get(sort_col, descending)?;
    if perm.len() != cards.len() {
        return None;
    }
    Some((total, walk_grouped_page(mode, cards, printings, offsets, &pbits, prefer, sort_col, descending, limit, page_offset, perm)))
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
    #[allow(clippy::too_many_arguments)]
    fn applicable(
        self,
        filter: &FilterExpr,
        mode: Mode,
        cards: &[AOracleCard],
        plane: Option<&PlaneExpr>,
        sort_col: SortCol,
        descending: bool,
        indexes: &Archived<CardIndexes>,
    ) -> bool {
        match self {
            PhysicalPlan::PrintingRangeScan => {
                printing_range_scan_applicable(mode, plane, cards) && bare_range_bounds(filter, indexes).is_some()
            }
            PhysicalPlan::PrintingCompose => {
                printing_compose_applicable(filter, mode, cards, plane, sort_col, descending, indexes)
            }
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

    /// Whether the plan runs off the shared materialized prep (plane bitmap /
    /// candidate list) — true for all but the printing-space fast paths
    /// (`PrintingRangeScan`/`PrintingCompose`), whose whole benefit is answering from a cheap estimate
    /// and composing/walking only if they win. The router costs non-materializing plans from a cheap
    /// estimate first (phase 1) and only materializes (phase 2) when a materializing plan wins or the
    /// non-materializing one declines.
    fn materializing(self) -> bool {
        !matches!(
            self,
            PhysicalPlan::PrintingRangeScan | PhysicalPlan::PrintingCompose | PhysicalPlan::CardRangePopcount
        )
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
}

/// How `run_query_routed` obtained a query's cost features, and the artifact (if
/// any) the chosen executor reuses. One of three "count sources", picked by query
/// structure — this is the engine's whole materialization story in one enum.
enum Prep {
    /// Bare printing range: features come from the range index's exact `k` (a
    /// binary search, no scan). Nothing is materialized — `PrintingRangeScan`
    /// walks; a materializing winner materializes for itself in dispatch. `Plane`
    /// carries no bitmap here because `run_query_routed` owns it locally
    /// (`plane_bits: Vec<u64>`), passed by reference into dispatch.
    Range,
    /// True-residual plane (card): the exact match bitmap, owned by
    /// `run_query_routed`'s local `plane_bits` and passed by reference.
    /// `PlanePopcountOrder` reads it directly; P3/P4 read it as a candidate list.
    Plane,
    /// The general residual path: a materialized candidate list.
    Candidates(PreparedCandidates),
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
        (Mode::Card, Some(pe)) if plane_expr_is_existential(pe) => Some((pe, &indexes.planes)),
        _ => None,
    }
}

// ─── Applicability predicates ───────────────────────────────────────────────
// Each captures a plan's *correctness* preconditions (not its perf gate). These
// are the future `choose_plan` eligibility gates — real, named, reusable.

/// `GatheredScan` can execute any query.
#[allow(dead_code)] // trivially true; referenced through the force entry point
fn gathered_scan_applicable() -> bool {
    true
}

/// `StreamedSelect` needs a precomputed sort permutation for `(sort_col,
/// descending)` whose length matches the card count, over a non-empty store.
/// `maybe_broad` is deliberately excluded — that is a routing/perf choice, not a
/// correctness constraint; StreamedSelect returns correct rows at any breadth.
fn streamed_select_applicable(
    cards: &[AOracleCard],
    sort_col: SortCol,
    descending: bool,
    indexes: &Archived<CardIndexes>,
) -> bool {
    !cards.is_empty() && indexes.sort_perms.get(sort_col, descending).is_some_and(|p| p.len() == cards.len())
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
        && indexes.sort_perms.get(sort_col, descending).is_some_and(|p| p.len() == cards.len())
        && indexes.sort_perms.get_inv(sort_col, descending).is_some()
}

/// `PrintingRangeScan` structural eligibility only — whether it actually runs is
/// decided by `printing_range_fastpath` returning `Some` (it declines with
/// `None` for anything it doesn't own).
fn printing_range_scan_applicable(mode: Mode, plane: Option<&PlaneExpr>, cards: &[AOracleCard]) -> bool {
    *PRINTING_RANGE_FASTPATH != 0 && matches!(mode, Mode::Printing) && plane.is_none() && !cards.is_empty()
}

/// `PrintingCompose` applicability (#724), all three distinct-ons: a composable printing-space `filter`
/// (border/rarity/legality, `AND`/`OR`), the planes built, no folded plane, the forward sort permutation
/// present, flag on. `plane.is_none()` is load-bearing: under `unique=card` a *bare* border/rarity is
/// `compile_plane`-consumed into an existential plane (`plane.is_some()`) → the faster #634 card-plane
/// path handles it, so this plan declines there; under `unique=printing`/`artwork` nothing folds to a
/// plane, so it picks up bare leaves too. Only the forward permutation is needed — the unified grouped
/// walk never uses the inverse (that was the popcount-skip walk's, deferred to [#730]).
fn printing_compose_applicable(
    filter: &FilterExpr,
    _mode: Mode,
    cards: &[AOracleCard],
    plane: Option<&PlaneExpr>,
    sort_col: SortCol,
    descending: bool,
    indexes: &Archived<CardIndexes>,
) -> bool {
    // Mode-agnostic: the cost model arbitrates overlap with the specialized range plans. All three
    // range/compose plans are non-materializing (estimate-in-acquire), so nothing is eagerly built —
    // a losing plan costs only a binary search, never a wasted scatter.
    *PRINTING_COMPOSE != 0
        && !cards.is_empty()
        && plane.is_none()
        && is_printing_composable(filter, indexes)
        && printing_compose_indexes_built(indexes)
        && indexes.sort_perms.get(sort_col, descending).is_some_and(|p| p.len() == cards.len())
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
        && indexes.sort_perms.get(sort_col, descending).is_some_and(|p| p.len() == cards.len())
        && indexes.sort_perms.get_inv(sort_col, descending).is_some()
}

// ─── Shared P3/P4 candidate preparation ─────────────────────────────────────

/// The candidate materialization + filter rewriting shared by `StreamedSelect`
/// and `GatheredScan`, extracted verbatim from `run_query`. Mutates `filter` via
/// `memoize_text_predicates` + `order_children_by_verify_cost` under the same
/// `!all_match_known` / `*VERIFY_ORDER` guards and in the same order as before.
fn prepare_candidates(
    cards: &[AOracleCard],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &mut FilterExpr,
    plane: Option<&PlaneExpr>,
    mode: Mode,
    indexes: &Archived<CardIndexes>,
) -> PreparedCandidates {
    // Candidates in either space project to card ids for the walk; the walk's
    // per-printing verification restores exactness for printing-space losses.
    // A list covering nearly the whole corpus narrows nothing — the walk would
    // visit almost every card anyway, and the list costs its materialization.
    // Broad-range bitmaps (#636) can produce such lists; treating them as
    // unnarrowed also keeps the #635 memoization trigger firing for these
    // queries exactly as before. Left un-materialized (Candidates, not
    // Vec<u32>) here so the plane branch below can AND two card-space bitmaps
    // directly instead of paying to materialize one of them first.
    let (raw_candidates, residual_exact): (Option<Candidates>, bool) =
        narrow_candidates_exact(filter, indexes, offsets, cards);
    // A present plane is always exact (that's what compile_plane guarantees),
    // so the whole original query is exact iff the residual is too — either
    // because split_planes consumed all of it (bare True) or narrow_rec
    // proved the remainder tight and complete with its membership in hand
    // (see narrow_candidates_exact). #634 Step 1: when this holds, every
    // candidate is already known to match, so the per-candidate card_pass
    // calls below and in run_query_streamed become redundant re-verification
    // of what the narrowing already established.
    //
    // A plane-driven True residual needs one more check first: legality's
    // planes (docs/issues/00667-engine-legality-divergent-carveout.md) are
    // existence projections ("*some* printing matches"), unlike every other
    // plane (card-invariant fields, true or false alike for every printing
    // of a card). For unique=card that's exactly the semantics wanted --
    // Mode::Card only needs *a* matching printing to exist, same as Step 2
    // above. But Mode::Printing/Artwork enumerate individual printings, and
    // "the card has some legal printing" does not mean "this printing is
    // legal" -- card_pass must still run per printing there whenever the
    // plane touched legality, so this only trusts a True residual for those
    // modes when the plane is existential-free (plane_expr_is_existential).
    let plane_true_for_mode =
        plane.is_none_or(|expr| matches!(mode, Mode::Card) || !plane_expr_is_existential(expr));
    let all_match_known = (matches!(filter, FilterExpr::True) && plane_true_for_mode) || residual_exact;

    // The plane bitmap is the exact card-level truth of the plane-consumed
    // subexpression (split_planes), so it composes with the residual's
    // narrowed candidates by intersection — and with no candidates it IS the
    // candidate list. Either way every surviving card still runs the residual
    // through card_pass, which is what keeps printing-space losses and Null
    // semantics with the residual, not the planes. The bitmap buffer is
    // reused across queries (thread-local), same as the streamed counts
    // buffer.
    let candidate_cards: Option<Vec<u32>> = match plane {
        None => raw_candidates
            .map(|c| c.into_cards(offsets, &indexes.printing_to_card))
            .filter(|v| v.len() < cards.len() - cards.len() / 8),
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

    // Resolve indexable text predicates through their indexes once (#624)
    // when the per-card evaluation they'd replace outweighs the bind cost —
    // the gate is per-node and cost-based (see memoize_pays): each predicate
    // compares its own bind bound against the evaluation domain, so a broad
    // candidate set with a selective needle memoizes while a narrow one
    // leaves the scan alone. Skipped entirely when all_match_known: card_pass
    // never runs, so there is nothing left for the rewrite to speed up.
    if !all_match_known {
        let eval_domain = candidate_cards.as_ref().map_or(cards.len(), Vec::len);
        filter.memoize_text_predicates(cards, strings, &indexes.name_trigram, &indexes.name_bigrams, &indexes.oracle_trigram, eval_domain);
        // Sort And/Or children cheapest-verification-first so the walk's
        // short-circuit spares the expensive text predicates (semantics-preserving;
        // see order_children_by_verify_cost). After memoization, which flips
        // TextContains nodes from the scan tier to the set tier.
        if *VERIFY_ORDER != 0 {
            filter.order_children_by_verify_cost();
        }
    }

    PreparedCandidates { candidate_cards, all_match_known }
}

// ─── Plan executors ─────────────────────────────────────────────────────────

/// P2 executor: evaluate the plane into the popcount thread-local and run the
/// #634 Step 2 popcount-skip order phase. Caller guarantees applicability
/// (`plane_popcount_order_applicable`).
#[allow(clippy::too_many_arguments)]
fn exec_plane_popcount_order<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    plane_expr: &PlaneExpr,
    indexes: &Archived<CardIndexes>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    thread_local! {
        static PLANE_BITMAP_POPCOUNT: std::cell::RefCell<Vec<u64>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    PLANE_BITMAP_POPCOUNT.with(|cell| {
        let mut bitmap = cell.borrow_mut();
        eval_planes(plane_expr, &indexes.planes, &mut bitmap);
        exec_plane_popcount_order_with_bitmap(
            cards, printings, offsets, strings, prefer, sort_col, descending, limit, page_offset, plane_expr, indexes, &bitmap,
        )
    })
}

/// The popcount-skip order phase of P2 with the plane bitmap *already evaluated*
/// by the caller — the eval-owning split of `exec_plane_popcount_order`. The
/// #702-step-5 routed path (`run_query_routed`) evaluates the plane once
/// and reuses the same `&[u64]` here, so the plane is never evaluated twice on a
/// routed query. Caller guarantees `plane_popcount_order_applicable` (a
/// length-matched forward permutation and an inverse permutation both exist).
#[allow(clippy::too_many_arguments)]
fn exec_plane_popcount_order_with_bitmap<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    plane_expr: &PlaneExpr,
    indexes: &Archived<CardIndexes>,
    bitmap: &[u64],
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let perm = indexes.sort_perms.get(sort_col, descending).expect("PlanePopcountOrder applicability guarantees a permutation");
    let inv_perm =
        indexes.sort_perms.get_inv(sort_col, descending).expect("PlanePopcountOrder applicability guarantees an inverse permutation");
    run_query_streamed_popcount(
        cards, printings, offsets, prefer, limit, page_offset, perm, inv_perm, bitmap, Some(plane_expr), &indexes.planes, strings, None,
    )
}

/// `CardRangePopcount` executor: the same popcount-skip order phase as P2, but its match bitmap is a
/// range's card-existence projection (built in `acquire`) rather than a plane, and it threads the
/// range's printing-space membership set so emission shows an in-range printing. Caller guarantees
/// `card_range_popcount_applicable` (permutations exist; the shown-printing plane, if any, is
/// non-existential so it needs no per-printing re-check here).
#[allow(clippy::too_many_arguments)]
fn exec_card_range_popcount<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    indexes: &Archived<CardIndexes>,
    card_bits: &[u64],
    range_pbits: &[u64],
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let perm = indexes.sort_perms.get(sort_col, descending).expect("CardRangePopcount applicability guarantees a permutation");
    let inv_perm =
        indexes.sort_perms.get_inv(sort_col, descending).expect("CardRangePopcount applicability guarantees an inverse permutation");
    run_query_streamed_popcount(
        cards, printings, offsets, prefer, limit, page_offset, perm, inv_perm, card_bits, None, &indexes.planes, strings, Some(range_pbits),
    )
}

/// Prefix-sum the per-card distinct-artwork counts (`artwork_groups`) into artwork-space offsets: a
/// card `c`'s distinct artworks are the contiguous global ids `[artwork_base[c], artwork_base[c+1])`,
/// so global artwork id = `artwork_base[c] + artwork_group_id`. `artwork_base.last()` is `n_artworks`.
/// Derived at query time from data already in the store — no stored global-id array (#724 PR 2b).
fn build_artwork_base(artwork_groups: &Archived<Vec<u16>>) -> Vec<u32> {
    let mut base = Vec::with_capacity(artwork_groups.len() + 1);
    let mut acc = 0u32;
    base.push(0);
    for c in artwork_groups.iter() {
        acc += u32::from(u16::from(*c));
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
    artwork_base: &[u32],
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
            let aid = artwork_base[card] as usize + gid;
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
#[allow(clippy::too_many_arguments)]
fn walk_grouped_page<'a>(
    mode: Mode,
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    pbits: &[u64],
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    perm: &Archived<Vec<u32>>,
) -> Vec<(&'a AOracleCard, &'a APrinting)> {
    let is_set = |pid: usize| pbits[pid >> 6] & (1u64 << (pid & 63)) != 0;
    let cmp = |a: &Match, b: &Match| a.0.cmp(&b.0).then_with(|| a.2.cmp(&b.2));
    let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
    let mut group_best: Vec<Option<(u32, f64)>> = Vec::new(); // per group key: (best matching pid, its prefer score)
    let mut touched: Vec<u16> = Vec::new();
    let mut scratch: Vec<Match> = Vec::new();
    let mut skip = page_offset;
    for cid in perm.iter().map(|x| u32::from(*x)) {
        let card = &cards[cid as usize];
        let start = u32::from(offsets[cid as usize]) as usize;
        let end = u32::from(offsets[cid as usize + 1]) as usize;
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
                    if group_best.len() <= gid {
                        group_best.resize(gid + 1, None);
                    }
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
        if scratch.is_empty() {
            continue;
        }
        if skip >= scratch.len() {
            skip -= scratch.len();
            continue;
        }
        scratch.sort_unstable_by(cmp);
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

/// P3 executor: streamed selection over the sort permutation. Caller guarantees
/// applicability (`streamed_select_applicable`) and has run `prepare_candidates`.
#[allow(clippy::too_many_arguments)]
fn exec_streamed_select<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &FilterExpr,
    prep: &PreparedCandidates,
    plane: Option<&PlaneExpr>,
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    indexes: &Archived<CardIndexes>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let perm = indexes.sort_perms.get(sort_col, descending).expect("StreamedSelect applicability guarantees a permutation");
    let existential_plane = existential_plane_for(mode, plane, indexes);
    let card_ids: Box<dyn Iterator<Item = u32>> = match &prep.candidate_cards {
        Some(v) => Box::new(v.iter().copied()),
        None    => Box::new(0..cards.len() as u32),
    };
    run_query_streamed(
        cards, printings, offsets, strings, filter, prep.all_match_known, mode, prefer, sort_col, descending, limit,
        page_offset, perm, card_ids, &indexes.artwork_groups, existential_plane,
    )
}

/// P4 executor: the universal gathered per-card loop + `select_page`. Runs any
/// query (printing-keyed orderbys, stores without permutations, or anything the
/// other plans decline). Caller has run `prepare_candidates`.
#[allow(clippy::too_many_arguments)]
fn exec_gathered_scan<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &FilterExpr,
    prep: &PreparedCandidates,
    plane: Option<&PlaneExpr>,
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    indexes: &Archived<CardIndexes>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let all_match_known = prep.all_match_known;
    let existential_plane = existential_plane_for(mode, plane, indexes);
    let card_ids: Box<dyn Iterator<Item = u32>> = match &prep.candidate_cards {
        Some(v) => Box::new(v.iter().copied()),
        None    => Box::new(0..cards.len() as u32),
    };

    // Gathered path (printing-keyed orderbys, or stores without permutations): push
    // each card's matches directly into the selector, which keeps its buffer bounded
    // at ~k via prune + running cutoff (see GatherSelect) rather than gathering every
    // match. `total` counts every match; the page is the k smallest.
    let mut sel = GatherSelect::new(page_offset, limit);
    // artwork-mode scratch, reused per card (#629: group-id-indexed, not illustration_id-keyed)
    let mut group_best: Vec<Option<(u32, f64)>> = Vec::new();
    let mut touched: Vec<u16> = Vec::new();
    // card_pass residual: the top-level children still printing-dependent for
    // the current card (reused buffer; see FilterExpr::card_pass).
    let mut residual: Vec<&FilterExpr> = Vec::new();
    let mut residual_is_or = false;
    for cid in card_ids {
        let card = &cards[cid as usize];
        // #634 Step 1: all_match_known means the narrowing already proved
        // every candidate matches — card_pass would just re-derive Tri::True
        // at real per-node evaluation cost for nothing.
        let all_match = all_match_known
            || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or) {
                Tri::False | Tri::Null => continue,
                Tri::True => true,          // every printing matches: skip per-printing checks
                Tri::PrintingDep => false,  // verify each printing against the residual below
            };
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;
        let before = sel.buf().len();
        push_card_matches(
            card, cid, printings, start, end, all_match, &residual, residual_is_or, mode, prefer,
            sort_col, descending, strings, existential_plane, sel.buf(), &mut group_best, &mut touched,
        );
        sel.absorb(before);
    }

    let (total, page_ids) = sel.finish(page_offset, limit);
    let page = page_ids
        .into_iter()
        .map(|(cid, pid)| (&cards[cid as usize], &printings[pid as usize]))
        .collect();
    (total, page)
}

fn run_query<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &mut FilterExpr,
    plane: Option<&PlaneExpr>,
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

    let mode = match unique {
        "artwork"  => Mode::Artwork,
        "printing" => Mode::Printing,
        _          => Mode::Card,
    };

    // #702: plan selection is one cost-based routing layer (`run_query_routed`,
    // `argmin cost::plan_cost` over the applicable plans), not a hand-tuned decision
    // tree. `run_query` is the thin string→enum adapter; the core takes enums.
    run_query_routed(cards, printings, offsets, strings, filter, plane, mode, prefer, sort_col, descending, limit, page_offset, indexes)
}

/// `cost::PlanFeatures::scan_units` for a query: the rows the per-row residual
/// scan touches, in the plan's operating space. `Mode::Card` breaks at the first
/// matching printing (≈ one row per candidate), so it is `eval_domain`;
/// printing/artwork scan every printing of every candidate, so it is the printing
/// count under those cards (`n_printings` when unnarrowed). The `Some` branch sums
/// `offsets` ranges over the candidate cards — O(candidates), a routing-time cost
/// only paid when a candidate list exists.
#[allow(dead_code)] // consumed by the cost benches (tests.rs) and future all-mode routing
fn scan_units(mode: Mode, candidate_cards: Option<&[u32]>, offsets: &AOffsets, n_printings: u32, eval_domain: u32) -> u32 {
    match mode {
        Mode::Card => eval_domain,
        Mode::Printing | Mode::Artwork => match candidate_cards {
            None => n_printings,
            Some(v) => v
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
#[allow(clippy::too_many_arguments)]
fn exec_from_candidates<'a>(
    plan: PhysicalPlan,
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &FilterExpr,
    prep: &PreparedCandidates,
    plane: Option<&PlaneExpr>,
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    indexes: &Archived<CardIndexes>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    match plan {
        PhysicalPlan::StreamedSelect => exec_streamed_select(
            cards, printings, offsets, strings, filter, prep, plane, mode, prefer, sort_col, descending, limit, page_offset, indexes,
        ),
        PhysicalPlan::GatheredScan => exec_gathered_scan(
            cards, printings, offsets, strings, filter, prep, plane, mode, prefer, sort_col, descending, limit, page_offset, indexes,
        ),
        other => unreachable!("exec_from_candidates only runs P3/P4, got {other:?}"),
    }
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
/// 2. **choose** — `argmin cost::plan_cost` over `ALL.filter(applicable)`. No
///    hand-written plan list; applicability encodes prep availability, so the right
///    candidates fall out per acquire branch.
/// 3. **dispatch** — run the winner, reusing the acquired artifact.
///
/// Plan choice is a pure performance decision — every plan returns identical rows
/// (guaranteed by `force_plan_differential_agreement`). Adding a plan is declaring
/// its `applicable`/`cost`/`materializing`/executor arms; only a genuinely new
/// count source (a new `Prep`) touches acquire/dispatch. The one subtlety is
/// `Prep::Range`: it costs `PrintingRangeScan` (non-materializing) from a cheap
/// estimate, so if a *materializing* plan wins there — or `PrintingRangeScan` wins
/// but its fastpath declines — dispatch materializes lazily and re-chooses on exact
/// features. That deferral is the "don't pay to materialize a plan you won't run".
#[allow(clippy::too_many_arguments)]
fn run_query_routed<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &mut FilterExpr,
    plane: Option<&PlaneExpr>,
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    indexes: &Archived<CardIndexes>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let n_cards = cards.len() as u32;
    let n_printings = printings.len() as u32;

    // Generic argmin: the cheapest applicable plan. `filter` is passed per call (not
    // captured) so it stays free for `prepare_candidates`'s `&mut`. `materializing`
    // restricts to plans runnable off a materialized prep (the lazy-materialize path
    // below). GatheredScan is always applicable and materializing → min never empty.
    let choose = |filter: &FilterExpr, feats: &cost::PlanFeatures, materializing_only: bool| -> PhysicalPlan {
        PhysicalPlan::ALL
            .into_iter()
            .filter(|p| {
                p.applicable(filter, mode, cards, plane, sort_col, descending, indexes) && (!materializing_only || p.materializing())
            })
            .min_by(|a, b| cost::plan_cost(*a, feats).partial_cmp(&cost::plan_cost(*b, feats)).expect("plan_cost is finite"))
            .expect("GatheredScan is always applicable and materializing")
    };

    // Cost features: the query-invariant fields filled once; the four that vary by
    // count source passed in. Collapses each acquire branch's 8-field literal to one call.
    let mk_feats = |matches: u32, eval_domain: u32, scan_units: u32, residual_tier_ns100: u32| cost::PlanFeatures {
        n_cards,
        n_printings,
        matches,
        eval_domain,
        scan_units,
        residual_tier_ns100,
        limit: limit as u32,
        offset: page_offset as u32,
        broadcast_printings: 0, // PrintingCompose's legality broadcast-down (0 for ranges / precomputed planes)
        scatter_printings: 0,  // range-slice k — set by both range-plan acquire branches (costed per-plan)
        project_printings: 0,  // PrintingCompose's card/artwork projection pass; CardRangePopcount sets it too (for costing compose)
        popcount_words: 0,     // PrintingCompose overrides this (result-space bitmap words)
    };
    // Features for the general candidate path (shared by the acquire branch and the
    // lazy-materialize dispatch). `matches`/`eval_domain` = candidate CARD count, the
    // broad/narrow proxy the P3/P4 crossover keys on. `scan_units` sums printing counts
    // over the candidate cards (O(candidates) for narrowed printing/artwork — the ~1-2%
    // overhead in plan_routing_ab), kept EXACT on purpose: an O(1) estimate would trade
    // the model's honesty for a couple percent on already-fast queries — do not swap it
    // for `eval_domain·n_printings/n_cards` without re-justifying.
    let candidate_feats = |prep: &PreparedCandidates, filter: &FilterExpr| -> cost::PlanFeatures {
        let count = prep.candidate_cards.as_ref().map_or(n_cards, |v| v.len() as u32);
        let scan = scan_units(mode, prep.candidate_cards.as_deref(), offsets, n_printings, count);
        mk_feats(count, count, scan, if prep.all_match_known { 0 } else { verify_cost_tier(filter) })
    };

    // Scratch for the plane bitmap (`Prep::Plane` only). A fresh `Vec` allocates
    // just once, on the plane branch's `eval_planes`; non-plane queries leave it
    // empty (no alloc). Owned locally so the router body stays flat statements —
    // no thread-local / `.with` closure wrapping the whole function.
    let mut plane_bits: Vec<u64> = Vec::new();

    // ── acquire: pick the count source, build features, materialize its artifact ──
    let (feats, prep) = if PhysicalPlan::PlanePopcountOrder.applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
        // The ONE plane eval; its popcount IS the exact count. scan_units == count
        // (Mode::Card breaks at first match); True residual ⇒ tier 0.
        eval_planes(plane.expect("PlanePopcountOrder ⇒ plane"), &indexes.planes, &mut plane_bits);
        let count: u32 = plane_bits.iter().map(|w| w.count_ones()).sum();
        (mk_feats(count, count, count, 0), Prep::Plane)
    } else if PhysicalPlan::CardRangePopcount.applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
        // Exact in-range printing count `k` from the index partition points (two binary searches, no
        // scan, no scatter). The O(k) card-bitmap build is deferred to dispatch and paid only if this
        // plan wins — so a competing winner never eats a wasted build (re-deriving the bounds there is
        // another ~free binary search). `k` rides `synth_printings` (the deferred scatter); `matches`
        // uses the card-count proxy `min(k, n_cards)` (card total ≤ both). The materializing
        // alternatives are costed with the range's verify tier (a `0` would under-cost them).
        let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("applicable ⇒ bare range");
        let k = (idx.partition_point(|p| u32::from(p.0) < hi) - idx.partition_point(|p| u32::from(p.0) < lo)) as u32;
        let card_est = k.min(n_cards);
        let mut feats = mk_feats(card_est, card_est, card_est, verify_cost_tier(filter));
        // `k` rides `scatter_printings`: this plan's arm charges it as its FUSED one-pass build
        // (`CARD_RANGE_BUILD_PER_PRINTING_NS`), while a competing PrintingCompose costed off these shared
        // feats charges the same `k` as its cheaper scatter (`RANGE_SCATTER_…`) plus a separate
        // `project` pass — so the fused op wins the argmin and a bare range doesn't mis-route to compose.
        feats.scatter_printings = k;
        feats.project_printings = k;
        (feats, Prep::Range)
    } else if PhysicalPlan::PrintingRangeScan.applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
        // Bare range: exact k from the index (no scan). P3/P4 estimated unnarrowed
        // (their broad regime — a narrow range makes P1 lose, and dispatch materializes).
        let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("applicable ⇒ bare range");
        let k = (idx.partition_point(|p| u32::from(p.0) < hi) - idx.partition_point(|p| u32::from(p.0) < lo)) as u32;
        let mut feats = mk_feats(k, n_cards, n_printings, verify_cost_tier(filter));
        feats.scatter_printings = k; // for costing a competing PrintingCompose (which would scatter k); P1 itself walks, so its own cost ignores this
        (feats, Prep::Range)
    } else if PhysicalPlan::PrintingCompose.applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
        // Composable printing-space expr, any distinct-on. Estimate the counts cheaply — the fast path
        // composes once, only if this plan wins (never in acquire; a legality broadcast paid here and
        // then discarded would be pure waste). `synth_printings` = broadcast down (legality) + projection
        // up (card/artwork; 0 for printing). `popcount_words` = the result-space bitmap the total scans.
        let (printing_matches, broadcast, scatter) = compose_printing_estimate(filter, indexes, offsets, n_printings as usize);
        // Two build kinds, charged at different rates: `broadcast` = legality broadcast-down (linear
        // pass), `scatter` = range-slice scatter (cheap). `project` = the second pass (printing→
        // card/artwork), 0 for printing mode. Keeping all three separate is what lets a bare range's
        // CardRangePopcount acquire (which sets `scatter`/`project` too) cost this plan's passes
        // honestly against the fused build. `eval_domain`/`scan_units` cost the *materializing
        // alternatives* should compose lose: a composable filter narrows via its indices, so they see
        // ~`matches` candidates (card mode also breaks at the first match ⇒ scan_units = eval_domain) —
        // the unnarrowed universe would over-cost them and mis-route (measured: `format:X format:Y`/card).
        let (result_total, project, popcount_words, eval_domain, scan_units) = match mode {
            Mode::Printing => (printing_matches, 0, (n_printings as usize).div_ceil(64), n_cards as usize, n_printings as usize),
            Mode::Card => {
                let rt = printing_matches.min(n_cards as usize);
                (rt, printing_matches, (n_cards as usize).div_ceil(64), rt, rt)
            }
            Mode::Artwork => {
                let rt = printing_matches.min(n_cards as usize);
                (printing_matches, printing_matches, (n_printings as usize).div_ceil(64), rt, printing_matches)
            }
        };
        let mut feats = mk_feats(result_total as u32, eval_domain as u32, scan_units as u32, verify_cost_tier(filter));
        feats.broadcast_printings = broadcast as u32;
        feats.scatter_printings = scatter as u32;
        feats.project_printings = project as u32;
        feats.popcount_words = popcount_words as u32;
        (feats, Prep::Range)
    } else {
        let prep = prepare_candidates(cards, offsets, strings, filter, plane, mode, indexes);
        (candidate_feats(&prep, filter), Prep::Candidates(prep))
    };

    // ── choose: cheapest applicable plan ──
    let plan = choose(filter, &feats, false);

    // ── dispatch: run the winner, reusing the acquired artifact ──
    match (plan, &prep) {
        (PhysicalPlan::PlanePopcountOrder, Prep::Plane) => exec_plane_popcount_order_with_bitmap(
            cards, printings, offsets, strings, prefer, sort_col, descending, limit, page_offset,
            plane.expect("Prep::Plane ⇒ plane"), indexes, &plane_bits,
        ),
        // P3/P4 reuse the plane bitmap as their candidate list — identical to what
        // prepare_candidates yields for a True-residual plane query.
        (p, Prep::Plane) => exec_from_candidates(
            p, cards, printings, offsets, strings, filter,
            &PreparedCandidates { candidate_cards: Some(bitmap_card_ids(&plane_bits)), all_match_known: true },
            plane, mode, prefer, sort_col, descending, limit, page_offset, indexes,
        ),
        (p, Prep::Candidates(prep)) => exec_from_candidates(
            p, cards, printings, offsets, strings, filter, prep, plane, mode, prefer, sort_col, descending, limit, page_offset, indexes,
        ),
        // `Prep::Range` = "cheap estimate acquired, nothing materialized" — shared by CardRangePopcount
        // (#725), PrintingRangeScan (#695), and PrintingCompose (#724). Each winner does its own O(k)
        // work here, so no plan eats a build for a competing winner:
        //   - CardRangePopcount builds its card bitmap from the (re-derived, ~free) range bounds now.
        //   - the printing-space fast paths walk (or, if they decline — sparse total — materialize).
        //   - a materializing plan (StreamedSelect/GatheredScan) that beat them narrows + runs.
        (PhysicalPlan::CardRangePopcount, Prep::Range) => {
            let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("applicable ⇒ bare range");
            let (card_bits, range_pbits) = build_card_range_bits(idx, lo, hi, indexes, cards.len(), printings.len());
            exec_card_range_popcount(
                cards, printings, offsets, strings, prefer, sort_col, descending, limit, page_offset, indexes, &card_bits, &range_pbits,
            )
        }
        (plan, Prep::Range) => {
            let fast_page = match plan {
                PhysicalPlan::PrintingRangeScan => {
                    printing_range_fastpath(cards, printings, offsets, strings, filter, indexes, sort_col, descending, limit, page_offset)
                }
                PhysicalPlan::PrintingCompose => {
                    printing_compose_fastpath(cards, printings, offsets, filter, indexes, mode, prefer, sort_col, descending, limit, page_offset)
                }
                _ => None, // a materializing plan won the estimate — materialize + run it below
            };
            match fast_page {
                Some(page) => page,
                None => {
                    let prep = prepare_candidates(cards, offsets, strings, filter, plane, mode, indexes);
                    let feats = candidate_feats(&prep, filter);
                    let plan = choose(filter, &feats, true);
                    exec_from_candidates(plan, cards, printings, offsets, strings, filter, &prep, plane, mode, prefer, sort_col, descending, limit, page_offset, indexes)
                }
            }
        }
    }
}

/// In-process force/dispatch entry point (#702 step 2): run `plan` for this
/// query if it is applicable, else return `None`. This is the prerequisite for
/// the calibration harness — it makes each physical plan individually
/// executable without changing `run_query`'s default routing. Returns `None`
/// when `plan` fails its applicability predicate (or, for `PrintingRangeScan`,
/// when `printing_range_fastpath` structurally declines with `None`); `Some`
/// with the result when it ran. `GatheredScan` is always `Some`.
#[allow(clippy::too_many_arguments)]
#[allow(dead_code)] // force entry point; exercised by force_plan_differential_agreement
fn run_query_with_plan<'a>(
    plan: PhysicalPlan,
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &mut FilterExpr,
    plane: Option<&PlaneExpr>,
    unique: &str,
    prefer: &str,
    orderby: &str,
    direction: &str,
    limit: usize,
    page_offset: usize,
    indexes: &Archived<CardIndexes>,
) -> Option<(usize, Vec<(&'a AOracleCard, &'a APrinting)>)> {
    let sort_col   = orderby_to_col(orderby);
    let descending = direction == "desc";
    let prefer     = prefer_from_str(prefer);
    let mode = match unique {
        "artwork"  => Mode::Artwork,
        "printing" => Mode::Printing,
        _          => Mode::Card,
    };

    match plan {
        PhysicalPlan::PrintingRangeScan => {
            if !printing_range_scan_applicable(mode, plane, cards) {
                return None;
            }
            // Structural eligibility passed; the fastpath itself decides (None = declined).
            printing_range_fastpath(cards, printings, offsets, strings, filter, indexes, sort_col, descending, limit, page_offset)
        }
        PhysicalPlan::PrintingCompose => {
            if !printing_compose_applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
                return None;
            }
            // The fastpath composes, projects per mode, and walks — or declines (None) on a sparse
            // total, exactly as under the router.
            printing_compose_fastpath(cards, printings, offsets, filter, indexes, mode, prefer, sort_col, descending, limit, page_offset)
        }
        PhysicalPlan::PlanePopcountOrder => {
            if !plane_popcount_order_applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
                return None;
            }
            let plane_expr = plane.expect("applicability guarantees a plane");
            Some(exec_plane_popcount_order(
                cards, printings, offsets, strings, prefer, sort_col, descending, limit, page_offset, plane_expr, indexes,
            ))
        }
        PhysicalPlan::CardRangePopcount => {
            if !card_range_popcount_applicable(filter, mode, cards, plane, sort_col, descending, indexes) {
                return None;
            }
            let (idx, lo, hi) = bare_range_bounds(filter, indexes).expect("applicability guarantees a bare range");
            let (card_bits, range_pbits) = build_card_range_bits(idx, lo, hi, indexes, cards.len(), printings.len());
            Some(exec_card_range_popcount(
                cards, printings, offsets, strings, prefer, sort_col, descending, limit, page_offset, indexes, &card_bits, &range_pbits,
            ))
        }
        PhysicalPlan::StreamedSelect => {
            if !streamed_select_applicable(cards, sort_col, descending, indexes) {
                return None;
            }
            let prep = prepare_candidates(cards, offsets, strings, filter, plane, mode, indexes);
            Some(exec_streamed_select(
                cards, printings, offsets, strings, filter, &prep, plane, mode, prefer, sort_col, descending, limit, page_offset, indexes,
            ))
        }
        PhysicalPlan::GatheredScan => {
            debug_assert!(gathered_scan_applicable());
            let prep = prepare_candidates(cards, offsets, strings, filter, plane, mode, indexes);
            Some(exec_gathered_scan(
                cards, printings, offsets, strings, filter, &prep, plane, mode, prefer, sort_col, descending, limit, page_offset, indexes,
            ))
        }
    }
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
#[allow(clippy::too_many_arguments)]
fn run_query_streamed_popcount<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    prefer: Prefer,
    limit: usize,
    page_offset: usize,
    perm: &Archived<Vec<u32>>,
    inv_perm: &Archived<Vec<u32>>,
    bitmap: &[u64],
    plane: Option<&PlaneExpr>,
    planes: &Archived<BitPlanes>,
    strings: &AStrings,
    range_bits: Option<&[u64]>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
    let n_cards = cards.len();
    let total: usize = bitmap.iter().map(|w| w.count_ones() as usize).sum();
    if total == 0 || page_offset >= total {
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
        let existential = plane.is_some_and(plane_expr_is_existential);
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
        (total, page)
    })
}

/// Streamed selection: match phase records per-card match counts (total is
/// their sum), then either gathers (small totals — byte-identical to the
/// gathered path) or walks the orderby permutation emitting only page cards.
#[allow(clippy::too_many_arguments)]
fn run_query_streamed<'a>(
    cards: &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets: &AOffsets,
    strings: &AStrings,
    filter: &FilterExpr,
    all_match_known: bool,
    mode: Mode,
    prefer: Prefer,
    sort_col: SortCol,
    descending: bool,
    limit: usize,
    page_offset: usize,
    perm: &Archived<Vec<u32>>,
    card_ids: Box<dyn Iterator<Item = u32> + '_>,
    artwork_groups: &Archived<Vec<u16>>,
    existential_plane: Option<(&PlaneExpr, &Archived<BitPlanes>)>,
) -> (usize, Vec<(&'a AOracleCard, &'a APrinting)>) {
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
    for cid in card_ids {
        let card = &cards[cid as usize];
        // #634 Step 1: skip the redundant card_pass re-derivation of Tri::True
        // when the narrowing already proved every candidate matches. Gated
        // off for Mode::Artwork specifically: measured a ~45% regression for
        // `t:creature` unique=artwork with this applied unconditionally here
        // (0.13ms -> 0.19ms typical, isolated by bisecting call sites) despite
        // being a no-op change in card_pass's own return value (True either
        // way) — an unexplained codegen/scheduling effect in this loop for
        // that mode specifically, not a logical cost. Card/Printing modes
        // showed no such effect and do benefit (this loop visits every
        // candidate, not just the ~limit emitted).
        let all_match = (all_match_known && !matches!(mode, Mode::Artwork))
            || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or) {
                Tri::False | Tri::Null => continue,
                Tri::True => true,
                Tri::PrintingDep => false,
            };
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;
        // Every printing matches: card/printing counts are O(1) inside the
        // helper, and the artwork group count is a build-time constant.
        let c = if all_match && matches!(mode, Mode::Artwork) && have_group_counts {
            u32::from(u16::from(artwork_groups[cid as usize]))
        } else {
            card_match_count(
                card, cid, printings, start, end, all_match, &residual, residual_is_or, mode, strings, existential_plane,
                &mut seen_words,
            )
        };
        counts[cid as usize] = c;
        total += c as usize;
    }
    if total == 0 || page_offset >= total {
        return (total, Vec::new());
    }

    // artwork-mode emission scratch (#629), reused across cards below
    let mut group_best: Vec<Option<(u32, f64)>> = Vec::new();
    let mut touched: Vec<u16> = Vec::new();
    let cmp = |a: &Match, b: &Match| a.0.cmp(&b.0).then_with(|| a.2.cmp(&b.2));

    // Small totals: gather and quickselect — same result as the gathered path.
    if total <= *STREAM_MIN_MATCHES {
        let mut best: Vec<Match> = Vec::with_capacity(total);
        for cid in 0..cards.len() as u32 {
            if counts[cid as usize] == 0 {
                continue;
            }
            let card = &cards[cid as usize];
            let all_match = all_match_known
                || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or) {
                    Tri::True => true,
                    Tri::PrintingDep => false,
                    _ => continue,
                };
            let start = u32::from(offsets[cid as usize]) as usize;
            let end   = u32::from(offsets[cid as usize + 1]) as usize;
            push_card_matches(
                card, cid, printings, start, end, all_match, &residual, residual_is_or, mode, prefer,
                sort_col, descending, strings, existential_plane, &mut best, &mut group_best, &mut touched,
            );
        }
        let page = select_page(best, page_offset, limit)
            .into_iter()
            .map(|(cid, pid)| (&cards[cid as usize], &printings[pid as usize]))
            .collect();
        return (total, page);
    }

    // Stream: walk the permutation, consume page_offset from the counts, emit
    // page cards only. Within a card, items order by (sort key, pid) — the
    // same comparator select_page uses; across cards the permutation supplies
    // the order.
    let mut skip = page_offset;
    let mut page: Vec<(&AOracleCard, &APrinting)> = Vec::with_capacity(limit);
    let mut scratch: Vec<Match> = Vec::new();
    'walk: for cid in perm.iter().map(|x| u32::from(*x)) {
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
            || match filter.card_pass(card, strings, &mut residual, &mut residual_is_or) {
                Tri::True => true,
                Tri::PrintingDep => false,
                _ => continue,
            };
        let start = u32::from(offsets[cid as usize]) as usize;
        let end   = u32::from(offsets[cid as usize + 1]) as usize;
        scratch.clear();
        push_card_matches(
            card, cid, printings, start, end, all_match, &residual, residual_is_or, mode, prefer,
            sort_col, descending, strings, existential_plane, &mut scratch, &mut group_best, &mut touched,
        );
        scratch.sort_unstable_by(cmp);
        for m in scratch.iter().skip(skip) {
            page.push((&cards[m.1 as usize], &printings[m.2 as usize]));
            if page.len() == limit {
                break 'walk;
            }
        }
        skip = 0;
    }
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
/// catch (e.g. reordering same-size fields, changing an index type) — and on
/// any FLAVOR_FP_FEATURES change: archived fingerprints are built with that
/// table, so a new table reading old fingerprints breaks the superset test.
const ARCHIVE_FORMAT_VERSION: u32 = 20260722;
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
                staging.rows.push(card_from_pydict(&d, &mut staging.interner, &mut staging.vocab, &mut staging.artists, &mut staging.mana)?);
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
            frame_data:     build_thresholded_tag_index(&printings, &coll_vocab, |p| &p.card_frame_data),
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
            released_at:    build_range_index(&printings, |p| p.released_at_int),
            price_usd:      build_range_index(&printings, |p| p.price_usd),
            collector_number: build_range_index(&printings, |p| p.collector_number_int.map(u32::from)),
            sort_perms:     build_sort_permutations(&cards, &printings, &offsets),
            artwork_groups: artwork_group_counts,
            printing_to_card: build_printing_to_card(&offsets),
            planes:         build_bit_planes(&cards, &printings, &offsets, &strings),
            border_printing: build_border_printing_planes(&printings, &strings),
            rarity_printing: build_rarity_printing_planes(&printings),
            name_bigrams:   build_name_bigram_index(&cards),
            legal_divergent: build_divergent_ids(&cards),
        };

        #[cfg(feature = "alloc-counter")]
        let stats_after_indexes = (alloc_stats::live(), alloc_stats::allocs());

        // Snapshot the registry card_from_pydict just populated so reader
        // processes can adopt the same format→shift assignments.
        let format_shifts_snapshot = format_shifts().read().map(|m| m.clone()).unwrap_or_default();
        let card_data = CardData { cards, printings, offsets, strings, coll_vocab, coll_vocab_sorted, artist_vocab, mana_vocab, indexes, format_shifts: format_shifts_snapshot };

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
        filter_expr.bind(&data.coll_vocab, &data.coll_vocab_sorted, &data.artist_vocab, &data.mana_vocab, &data.indexes.flavor, &data.strings);

        // Consume the plane-expressible part of the filter (colors/identity/
        // types) into a bitmap expression; run_query evaluates it in a few
        // hundred word ops instead of per-card dispatch. Guarded on the archive
        // carrying planes for this card count — the format-version bump already
        // rejects pre-plane archives, this is defense in depth.
        let (plane_expr, mut filter_expr) =
            if u32::from(data.indexes.planes.n_cards) as usize == data.cards.len() && !data.cards.is_empty() {
                // Matches run_query's own unique -> Mode mapping exactly
                // (anything other than "artwork"/"printing" is Mode::Card,
                // not just the literal string "card") -- see split_planes's
                // unique_is_card doc.
                split_planes(filter_expr, &data.indexes.planes, &data.indexes.oracle_trigram.words, !matches!(unique, "artwork" | "printing"))
            } else {
                (None, filter_expr)
            };

        let (total, page) = run_query(
            &data.cards, &data.printings, &data.offsets, &data.strings, &mut filter_expr, plane_expr.as_ref(),
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
mod bench_word_dict_scan;
#[cfg(test)]
mod bench_card_dedup;
