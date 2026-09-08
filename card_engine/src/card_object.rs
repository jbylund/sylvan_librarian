//! Scryfall card objects, built in the engine rather than by the caller.
//!
//! LOCAL ADDITION (Cloudflare port), destined for upstream — the twin of `to_scryfall_card` in
//! `api/scryfall_compat/objects.py` and `toScryfallCard` in the port's
//! `src/routes/scryfall-compat/objects.ts`. Both of those build the object OUTSIDE the engine, per
//! card, from an engine row: Python builds ~60 dict entries per card and the port's Durable Object
//! builds the same in JS. A 175-card page pays that 175 times, on top of parsing the engine's rows
//! back out of JSON and re-encoding the result.
//!
//! Measured against the live deployment, that whole round trip is what `/cards/search` spends its
//! Durable Object CPU on: the DO's cost is very nearly a pure function of payload bytes (~15us/KB),
//! while the row construction underneath is ~16us per CARD. Building the object here removes the
//! parse and the re-encode entirely — the bytes written by this module are the bytes on the wire.
//!
//! WRITTEN, NOT BUILT. This emits JSON directly rather than assembling a `serde_json::Value`,
//! for two reasons:
//!
//!   - `serde_json` here has no `preserve_order` feature, so `Map` is a `BTreeMap` and a `Value`
//!     would come out ALPHABETICAL. Both existing implementations emit insertion order, and while
//!     Scryfall's own order matches neither of them (its `arena_id` is 4th, `legalities` 27th),
//!     changing our own output order for every card is a gratuitous break for clients and tests.
//!   - It is faster, which is the point: no intermediate tree, and no freshly allocated `String`
//!     key per field per card.
//!
//! Key order follows UPSTREAM's dict literal. The port's `toScryfallCard` agreed with it
//! everywhere except `security_stamp`, which sat 6th in the optional tail there and 14th upstream —
//! cosmetic, but the two should not disagree, and upstream is the reference for a port, so the port
//! moved to match this rather than the other way round.

use serde_json::{Map, Value};

/// Scryfall's shared card back, the same id on every card object.
const CARD_BACK_ID: &str = "0aeebaf5-8c7d-4636-9e82-8c27447861f7";

/// Image size -> file extension, in Scryfall's own order.
const IMAGE_EXTENSIONS: [(&str, &str); 6] = [
    ("small", "jpg"),
    ("normal", "jpg"),
    ("large", "jpg"),
    ("png", "png"),
    ("art_crop", "jpg"),
    ("border_crop", "jpg"),
];

// ─── row accessors, mirroring the port's str/num/bool/list ───────────────────
//
// Absent, wrong-typed and empty-string all read the same: the key was not answered. That is the
// rule both existing implementations follow, and it is why a card without a watermark omits the
// key rather than sending null.

fn str_of<'a>(row: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    match row.get(key) {
        Some(Value::String(s)) if !s.is_empty() => Some(s),
        _ => None,
    }
}

fn num_of<'a>(row: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    match row.get(key) {
        Some(v @ Value::Number(_)) => Some(v),
        _ => None,
    }
}

fn u64_of(row: &Map<String, Value>, key: &str) -> Option<u64> {
    row.get(key).and_then(Value::as_u64).filter(|n| *n != 0)
}

fn bool_of(row: &Map<String, Value>, key: &str) -> bool {
    row.get(key) == Some(&Value::Bool(true))
}

fn list_of<'a>(row: &'a Map<String, Value>, key: &str) -> Option<&'a Vec<Value>> {
    match row.get(key) {
        Some(Value::Array(a)) => Some(a),
        _ => None,
    }
}

// ─── JSON writing primitives ─────────────────────────────────────────────────

fn write_json_str(out: &mut Vec<u8>, s: &str) {
    // serde_json's own string encoder, so escaping matches everything else this crate emits.
    serde_json::to_writer(&mut *out, s).expect("writing a str to a Vec cannot fail");
}

fn write_key(out: &mut Vec<u8>, first: &mut bool, key: &str) {
    if *first {
        *first = false;
    } else {
        out.push(b',');
    }
    write_json_str(out, key);
    out.push(b':');
}

fn write_value(out: &mut Vec<u8>, first: &mut bool, key: &str, value: &Value) {
    write_key(out, first, key);
    serde_json::to_writer(&mut *out, value).expect("writing a Value to a Vec cannot fail");
}

fn write_str_or_null(out: &mut Vec<u8>, first: &mut bool, key: &str, value: Option<&str>) {
    write_key(out, first, key);
    match value {
        Some(s) => write_json_str(out, s),
        None => out.extend_from_slice(b"null"),
    }
}

fn write_bool(out: &mut Vec<u8>, first: &mut bool, key: &str, value: bool) {
    write_key(out, first, key);
    out.extend_from_slice(if value { b"true" } else { b"false" });
}

/// An array value, or `[]` when the row carries nothing.
fn write_list(out: &mut Vec<u8>, first: &mut bool, key: &str, value: Option<&Vec<Value>>) {
    write_key(out, first, key);
    match value {
        Some(a) => serde_json::to_writer(&mut *out, a).expect("writing an array to a Vec cannot fail"),
        None => out.extend_from_slice(b"[]"),
    }
}

// ─── derived values ──────────────────────────────────────────────────────────

/// Scryfall's URL slug: lowercase, every non-alphanumeric collapsed to one hyphen, trimmed.
///
/// Alphanumeric in the UNICODE sense, matching Python's `str.isalnum()` and the port's
/// `\p{L}\p{N}` — "Æther" and "Jötun" must slug the same way in all three.
fn slug(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    let mut pending_hyphen = false;
    for ch in name.chars() {
        if ch.is_alphanumeric() {
            if pending_hyphen && !out.is_empty() {
                out.push('-');
            }
            pending_hyphen = false;
            out.extend(ch.to_lowercase());
        } else {
            // Collapsed rather than emitted: a run of non-alphanumerics is ONE hyphen, and a run
            // at either end is none at all.
            pending_hyphen = true;
        }
    }
    out
}

/// Python's `urllib.parse.quote_plus`: space to `+`, everything outside the unreserved set
/// percent-encoded uppercase.
///
/// Spelled out rather than reached for from a crate because the safe set is the thing that has to
/// match: `~` stays literal (Python leaves it, and so must we), while `!`, `*`, `'`, `(` and `)`
/// are escaped — which is exactly where a naive `encodeURIComponent` twin drifts.
fn quote_plus(value: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut out = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(*byte as char),
            b' ' => out.push('+'),
            _ => {
                out.push('%');
                out.push(HEX[(byte >> 4) as usize] as char);
                out.push(HEX[(byte & 0xf) as usize] as char);
            }
        }
    }
    out
}

/// The CDN URLs for one face. Scryfall's paths are a pure function of the id, so nothing is stored.
fn write_image_uris(out: &mut Vec<u8>, scryfall_id: &str, updated_at: Option<u64>, face: &str) {
    let mut bytes = scryfall_id.bytes();
    let (Some(first), Some(second)) = (bytes.next(), bytes.next()) else {
        out.extend_from_slice(b"{}"); // no id, no paths -- same as both twins
        return;
    };
    let (first, second) = (first as char, second as char);
    let suffix = updated_at.map_or(String::new(), |t| format!("?{t}"));
    out.push(b'{');
    let mut first_key = true;
    for (size, ext) in IMAGE_EXTENSIONS {
        write_key(out, &mut first_key, size);
        write_json_str(
            out,
            &format!("https://cards.scryfall.io/{size}/{face}/{first}/{second}/{scryfall_id}.{ext}{suffix}"),
        );
    }
    out.push(b'}');
}

/// `prices`: the three price columns plus the three residue variants, each `"0.00"` or null.
fn write_prices(out: &mut Vec<u8>, row: &Map<String, Value>) {
    out.push(b'{');
    let mut first = true;
    for (key, column) in [
        ("usd", "price_usd"),
        ("usd_foil", "price_usd_foil"),
        ("usd_etched", "price_usd_etched"),
        ("eur", "price_eur"),
        ("eur_foil", "price_eur_foil"),
        ("tix", "price_tix"),
    ] {
        write_key(out, &mut first, key);
        match num_of(row, column).and_then(Value::as_f64) {
            // Two decimals, matching Python's `f"{float(v):.2f}"` and the port's `toFixed(2)`.
            Some(v) => write_json_str(out, &format!("{v:.2}")),
            None => out.extend_from_slice(b"null"),
        }
    }
    out.push(b'}');
}

/// `related_uris`, pointing at the destinations directly rather than through Scryfall's affiliate
/// wrapper — emitting the wrapper from this host would route another service's revenue to them.
fn write_related_uris(out: &mut Vec<u8>, name: &str) {
    let quoted = quote_plus(name);
    out.push(b'{');
    let mut first = true;
    for (key, url) in [
        (
            "tcgplayer_infinite_articles",
            format!("https://www.tcgplayer.com/search/articles?productLineName=magic&q={quoted}"),
        ),
        (
            "tcgplayer_infinite_decks",
            format!("https://www.tcgplayer.com/search/decks?productLineName=magic&q={quoted}"),
        ),
        ("edhrec", format!("https://edhrec.com/route/?cc={quoted}")),
    ] {
        write_key(out, &mut first, key);
        write_json_str(out, &url);
    }
    out.push(b'}');
}

/// `purchase_uris`, rebuilt from the marketplace ids. Only the ids the card actually has.
fn write_purchase_uris(out: &mut Vec<u8>, row: &Map<String, Value>) {
    out.push(b'{');
    let mut first = true;
    if let Some(id) = u64_of(row, "tcgplayer_id") {
        write_key(out, &mut first, "tcgplayer");
        write_json_str(out, &format!("https://www.tcgplayer.com/product/{id}?page=1"));
    }
    if let Some(id) = u64_of(row, "cardmarket_id") {
        write_key(out, &mut first, "cardmarket");
        write_json_str(out, &format!("https://www.cardmarket.com/en/Magic/Products?idProduct={id}"));
    }
    if let Some(id) = u64_of(row, "mtgo_id") {
        write_key(out, &mut first, "cardhoarder");
        write_json_str(out, &format!("https://www.cardhoarder.com/cards/{id}"));
    }
    out.push(b'}');
}

/// The card's faces, with the two keys the engine deliberately does not store re-added: `object`
/// is the constant, and a face's `image_uris` is the card's own CDN function with the face swapped.
fn write_faces(out: &mut Vec<u8>, faces: &[Value], scryfall_id: &str, updated_at: Option<u64>) {
    out.push(b'[');
    for (index, face) in faces.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        out.push(b'{');
        let mut first = true;
        write_key(out, &mut first, "object");
        write_json_str(out, "card_face");
        if let Value::Object(map) = face {
            for (key, value) in map {
                // Absent stays absent: null, "" and [] all mean Scryfall did not send this face
                // that key, and emitting them would differ from Scryfall on most faces.
                let empty = match value {
                    Value::Null => true,
                    Value::String(s) => s.is_empty(),
                    Value::Array(a) => a.is_empty(),
                    _ => false,
                };
                if !empty {
                    write_value(out, &mut first, key, value);
                }
            }
        }
        if faces.len() > 1 {
            write_key(out, &mut first, "image_uris");
            write_image_uris(out, scryfall_id, updated_at, if index == 0 { "front" } else { "back" });
        }
        out.push(b'}');
    }
    out.push(b']');
}

// ─── the card object ─────────────────────────────────────────────────────────

/// Write one engine row as a Scryfall card object.
///
/// `base_url` is the host self-referencing URIs should address — the deployment's own, not
/// Scryfall's, so a client following `uri` or `prints_search_uri` stays on this API.
pub fn write_scryfall_card(out: &mut Vec<u8>, row: &Map<String, Value>, base_url: &str) {
    let scryfall_id = str_of(row, "scryfall_id").unwrap_or("");
    let oracle_id = str_of(row, "oracle_id").unwrap_or("");
    let name = str_of(row, "name").unwrap_or("");
    let set_code = str_of(row, "set_code").unwrap_or("");
    let number = str_of(row, "collector_number").unwrap_or("");
    let set_id = str_of(row, "set_id");
    let image_updated_at = u64_of(row, "image_updated_at");
    let faces = list_of(row, "card_faces").filter(|f| !f.is_empty());

    out.push(b'{');
    let mut first = true;

    write_key(out, &mut first, "object");
    write_json_str(out, "card");
    write_key(out, &mut first, "id");
    write_json_str(out, scryfall_id);
    write_key(out, &mut first, "oracle_id");
    write_json_str(out, oracle_id);
    write_list(out, &mut first, "multiverse_ids", list_of(row, "multiverse_ids"));
    write_key(out, &mut first, "name");
    write_json_str(out, name);
    write_key(out, &mut first, "lang");
    write_json_str(out, str_of(row, "lang").unwrap_or("en"));
    write_str_or_null(out, &mut first, "released_at", str_of(row, "released_at"));
    write_key(out, &mut first, "uri");
    write_json_str(out, &format!("{base_url}/cards/{scryfall_id}"));
    write_key(out, &mut first, "scryfall_uri");
    write_json_str(
        out,
        &format!("https://scryfall.com/card/{set_code}/{number}/{}?utm_source=api", slug(name)),
    );
    write_str_or_null(out, &mut first, "layout", str_of(row, "layout"));
    write_bool(out, &mut first, "highres_image", bool_of(row, "highres_image"));
    write_str_or_null(out, &mut first, "image_status", str_of(row, "image_status"));
    write_key(out, &mut first, "cmc");
    // As a DECIMAL, which is what api.scryfall.com answers with: `"cmc":1.0`, not `"cmc":1` (see
    // https://api.scryfall.com/cards/named?exact=Lightning+Bolt). Writing the stored number
    // directly emits `1`, because `magic.cards.cmc` is an integer column -- and that would also
    // put the engine at odds with `to_scryfall_card`, which now carries the same value as a float.
    // The two must agree byte for byte: the engine answers a card when it can and the SQL path
    // answers it when the engine cannot, and a client must not be able to tell which one did.
    match num_of(row, "cmc").and_then(serde_json::Value::as_f64) {
        Some(v) => serde_json::to_writer(&mut *out, &v).expect("number"),
        None => out.extend_from_slice(b"null"),
    }
    write_str_or_null(out, &mut first, "type_line", str_of(row, "type_line"));
    write_list(out, &mut first, "colors", list_of(row, "colors"));
    write_list(out, &mut first, "color_identity", list_of(row, "color_identity"));
    write_list(out, &mut first, "keywords", list_of(row, "card_keywords"));
    write_list(out, &mut first, "games", list_of(row, "games"));
    // `reserved` is a tag rather than a column: the reserved list is a property of the card, and
    // the engine stores it in the same is-tag set everything else uses.
    let reserved = list_of(row, "card_is_tags")
        .is_some_and(|tags| tags.iter().any(|t| t.as_str() == Some("reserved")));
    write_bool(out, &mut first, "reserved", reserved);
    write_list(out, &mut first, "finishes", list_of(row, "finishes"));
    write_bool(out, &mut first, "oversized", bool_of(row, "oversized"));
    write_bool(out, &mut first, "promo", bool_of(row, "promo"));
    write_bool(out, &mut first, "reprint", bool_of(row, "reprint"));
    write_bool(out, &mut first, "variation", bool_of(row, "variation"));
    write_str_or_null(out, &mut first, "set_id", set_id);
    write_key(out, &mut first, "set");
    write_json_str(out, set_code);
    write_str_or_null(out, &mut first, "set_name", str_of(row, "set_name"));
    write_str_or_null(out, &mut first, "set_type", str_of(row, "set_type"));
    write_key(out, &mut first, "set_uri");
    match set_id {
        Some(id) => write_json_str(out, &format!("{base_url}/sets/{id}")),
        None => out.extend_from_slice(b"null"),
    }
    write_key(out, &mut first, "set_search_uri");
    write_json_str(out, &format!("{base_url}/cards/search?order=set&q=e%3A{set_code}&unique=prints"));
    write_key(out, &mut first, "scryfall_set_uri");
    write_json_str(out, &format!("https://scryfall.com/sets/{set_code}?utm_source=api"));
    write_key(out, &mut first, "rulings_uri");
    write_json_str(out, &format!("{base_url}/cards/{scryfall_id}/rulings"));
    write_key(out, &mut first, "prints_search_uri");
    write_json_str(
        out,
        &format!("{base_url}/cards/search?order=released&q=oracleid%3A{oracle_id}&unique=prints"),
    );
    write_key(out, &mut first, "collector_number");
    write_json_str(out, number);
    write_bool(out, &mut first, "digital", bool_of(row, "digital"));
    write_str_or_null(out, &mut first, "rarity", str_of(row, "rarity"));
    write_key(out, &mut first, "card_back_id");
    write_json_str(out, CARD_BACK_ID);
    write_str_or_null(out, &mut first, "artist", str_of(row, "artist"));
    write_str_or_null(out, &mut first, "illustration_id", str_of(row, "illustration_id"));
    write_str_or_null(out, &mut first, "border_color", str_of(row, "border_color"));
    write_bool(out, &mut first, "full_art", bool_of(row, "full_art"));
    write_bool(out, &mut first, "textless", bool_of(row, "textless"));
    write_bool(out, &mut first, "booster", bool_of(row, "booster"));
    write_bool(out, &mut first, "story_spotlight", bool_of(row, "story_spotlight"));
    write_key(out, &mut first, "prices");
    write_prices(out, row);
    write_key(out, &mut first, "related_uris");
    write_related_uris(out, name);
    write_key(out, &mut first, "purchase_uris");
    write_purchase_uris(out, row);

    // A multi-face card carries its faces and NOT the top-level text they replace; a single-faced
    // one carries the text and no `card_faces`. Which keys sit at top level varies by LAYOUT,
    // which is why this is a branch rather than a fixed key set.
    if let Some(faces) = faces {
        write_key(out, &mut first, "card_faces");
        write_faces(out, faces, scryfall_id, image_updated_at);
    } else {
        write_str_or_null(out, &mut first, "mana_cost", str_of(row, "mana_cost"));
        write_str_or_null(out, &mut first, "oracle_text", str_of(row, "oracle_text"));
        write_key(out, &mut first, "image_uris");
        write_image_uris(out, scryfall_id, image_updated_at, "front");
    }

    // Keys Scryfall sends only when the card HAS them. Emitting null instead would differ from
    // Scryfall on every card that lacks them, which for most of these is most cards.
    for (key, value) in [
        ("power", str_of(row, "power")),
        ("toughness", str_of(row, "toughness")),
        // Beside the creature stats it is the planeswalker analogue of, as the PRINTED string --
        // the `planeswalker_loyalty` the planner filters on is a u8 and loses "X" and "1+*".
        ("loyalty", str_of(row, "loyalty")),
        ("flavor_text", str_of(row, "flavor_text")),
        ("watermark", str_of(row, "watermark")),
        ("frame", str_of(row, "frame")),
    ] {
        if let Some(v) = value {
            write_key(out, &mut first, key);
            write_json_str(out, v);
        }
    }
    for key in [
        "edhrec_rank",
        "penny_rank",
        "arena_id",
        "mtgo_id",
        "mtgo_foil_id",
        "tcgplayer_id",
        "tcgplayer_etched_id",
        "cardmarket_id",
    ] {
        if let Some(v) = num_of(row, key) {
            write_value(out, &mut first, key, v);
        }
    }
    // After the ids, matching upstream's dict literal. The port's TypeScript had it up with the
    // other strings; upstream is the reference for a port, so the port moved rather than this.
    if let Some(v) = str_of(row, "security_stamp") {
        write_key(out, &mut first, "security_stamp");
        write_json_str(out, v);
    }
    for key in ["promo_types", "frame_effects", "all_parts"] {
        if let Some(a) = list_of(row, key).filter(|a| !a.is_empty()) {
            write_value(out, &mut first, key, &Value::Array(a.clone()));
        }
    }
    if let Some(v) = row.get("legalities").filter(|v| !v.is_null()) {
        write_value(out, &mut first, "legalities", v);
    }

    out.push(b'}');
}

/// A page of rows as a JSON array of card objects, written straight into `out`.
pub fn write_scryfall_cards(out: &mut Vec<u8>, rows: &[Value], base_url: &str) {
    out.push(b'[');
    for (index, row) in rows.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        match row {
            Value::Object(map) => write_scryfall_card(out, map, base_url),
            // Unreachable: the query path only ever produces objects. Emitting the row verbatim
            // rather than panicking keeps a malformed row from taking down a whole page.
            other => serde_json::to_writer(&mut *out, other).expect("writing a Value cannot fail"),
        }
    }
    out.push(b']');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn build(row: serde_json::Value) -> serde_json::Value {
        let serde_json::Value::Object(map) = row else { panic!("row must be an object") };
        let mut out = Vec::new();
        write_scryfall_card(&mut out, &map, "https://api.example/v1");
        serde_json::from_slice(&out).expect("the writer must emit valid JSON")
    }

    /// A planeswalker's printed loyalty reaches the card object, as the STRING Scryfall prints.
    ///
    /// The engine holds `planeswalker_loyalty` as a `u8` for `loy:` to filter on, which is why the
    /// text is its own field: "X" (Nissa, Steward of Elements) does not fit in the number at all,
    /// so deriving the key from it would silently drop those cards' loyalty.
    #[test]
    fn a_planeswalkers_printed_loyalty_is_the_string() {
        let card = build(json!({
            "name": "Jace Beleren",
            "scryfall_id": "ab000000-0000-0000-0000-000000000002",
            "loyalty": "3",
        }));
        assert_eq!(card["loyalty"], "3");

        let x = build(json!({
            "name": "Nissa, Steward of Elements",
            "scryfall_id": "ab000000-0000-0000-0000-000000000003",
            "loyalty": "X",
        }));
        assert_eq!(x["loyalty"], "X", "a non-numeric loyalty survives verbatim");
    }

    /// Absent stays absent. A card without a watermark omits the key; it does not send null.
    #[test]
    fn optional_keys_are_omitted_rather_than_nulled() {
        let card = build(json!({"name": "Llanowar Elves", "scryfall_id": "ab000000-0000-0000-0000-000000000001"}));
        for absent in
            ["power", "toughness", "loyalty", "flavor_text", "watermark", "frame", "security_stamp", "legalities"]
        {
            assert!(card.get(absent).is_none(), "{absent} should be omitted when the row has none");
        }
        // ... while the keys Scryfall always sends are present, even when empty.
        assert_eq!(card["object"], "card");
        assert_eq!(card["colors"], json!([]));
        assert_eq!(card["set_uri"], serde_json::Value::Null);
        assert_eq!(card["card_back_id"], CARD_BACK_ID);
    }

    /// A single-faced card carries the text and image_uris; a multi-faced one carries neither, and
    /// each face gets its own front/back CDN URLs.
    #[test]
    fn faces_replace_the_top_level_text_they_stand_in_for() {
        let base = json!({
            "name": "Delver of Secrets // Insectile Aberration",
            "scryfall_id": "cd000000-0000-0000-0000-000000000002",
            "mana_cost": "{U}", "oracle_text": "top level",
        });

        let single = build(base.clone());
        assert_eq!(single["mana_cost"], "{U}");
        assert!(single.get("card_faces").is_none());
        assert!(single["image_uris"]["small"].as_str().unwrap().contains("/front/"));

        let mut two = base.clone();
        two["card_faces"] = json!([
            {"name": "Delver of Secrets", "mana_cost": "{U}"},
            {"name": "Insectile Aberration", "mana_cost": "", "colors": []},
        ]);
        let card = build(two);
        assert!(card.get("mana_cost").is_none(), "a multi-faced card has no top-level mana_cost");
        assert!(card.get("image_uris").is_none(), "...and no top-level image_uris");
        let faces = card["card_faces"].as_array().expect("faces");
        assert_eq!(faces[0]["object"], "card_face");
        assert!(faces[0]["image_uris"]["png"].as_str().unwrap().contains("/front/"));
        assert!(faces[1]["image_uris"]["png"].as_str().unwrap().contains("/back/"));
        // Empty values inside a face are absent, not empty.
        assert!(faces[1].get("mana_cost").is_none());
        assert!(faces[1].get("colors").is_none());
    }

    /// Prices format to two decimals; a missing price is null rather than "0.00", and zero is a
    /// price like any other.
    #[test]
    fn prices_are_two_decimals_or_null() {
        let card = build(json!({"name": "x", "scryfall_id": "ef000000-0000-0000-0000-000000000003",
            "price_usd": 1, "price_eur": 0.005, "price_tix": 0}));
        assert_eq!(card["prices"]["usd"], "1.00");
        assert_eq!(card["prices"]["eur"], "0.01");
        assert_eq!(card["prices"]["tix"], "0.00");
        assert_eq!(card["prices"]["usd_foil"], serde_json::Value::Null);
    }

    /// The slug and quote_plus paths, which are where a reimplementation drifts.
    #[test]
    fn slug_and_quote_plus_match_their_python_originals() {
        assert_eq!(slug("Lightning Bolt"), "lightning-bolt");
        assert_eq!(slug("  Fire  //  Ice  "), "fire-ice");
        assert_eq!(slug("!!! ??? ---"), "");
        assert_eq!(slug("Æther Vial"), "æther-vial", "isalnum() is Unicode-aware");
        assert_eq!(slug("Jötun Grunt"), "jötun-grunt");

        assert_eq!(quote_plus("Lightning Bolt"), "Lightning+Bolt");
        assert_eq!(quote_plus("Æther Vial"), "%C3%86ther+Vial");
        assert_eq!(quote_plus("Fire // Ice"), "Fire+%2F%2F+Ice");
        // The safe set is the thing that has to match: `~` is left alone, `!*'()` are not.
        assert_eq!(quote_plus("a~b"), "a~b");
        assert_eq!(quote_plus("Yawgmoth's (Alt!)*"), "Yawgmoth%27s+%28Alt%21%29%2A");
    }

    /// `purchase_uris` carries only the marketplaces the card is actually on, and a zero id is not
    /// an id.
    #[test]
    fn purchase_uris_skip_missing_and_zero_ids() {
        let none = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000004"}));
        assert_eq!(none["purchase_uris"], json!({}));

        let zero = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000004",
            "tcgplayer_id": 0, "mtgo_id": 0, "cardmarket_id": 0}));
        assert_eq!(zero["purchase_uris"], json!({}));

        let some = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000004",
            "tcgplayer_id": 42, "mtgo_id": 7}));
        assert_eq!(some["purchase_uris"]["tcgplayer"], "https://www.tcgplayer.com/product/42?page=1");
        assert_eq!(some["purchase_uris"]["cardhoarder"], "https://www.cardhoarder.com/cards/7");
        assert!(some["purchase_uris"].get("cardmarket").is_none());
    }

    /// `reserved` is a tag, not a column — the reserved list is a property of the card and the
    /// engine stores it in the same is-tag set as everything else.
    #[test]
    fn reserved_comes_from_the_is_tag_set() {
        let plain = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000005"}));
        assert_eq!(plain["reserved"], false);
        let listed = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000005",
            "card_is_tags": ["reprint", "reserved"]}));
        assert_eq!(listed["reserved"], true);
    }

    /// The written bytes are key-ORDERED, which a `Value` round trip cannot show: parsing sorts
    /// them. Asserted against the encoded text, since that is what a client receives.
    #[test]
    fn keys_are_written_in_order_not_sorted() {
        let serde_json::Value::Object(map) = json!({
            "name": "Llanowar Elves", "scryfall_id": "01000000-0000-0000-0000-000000000006",
            "security_stamp": "oval", "cardmarket_id": 9, "watermark": "set",
        }) else {
            panic!()
        };
        let mut out = Vec::new();
        write_scryfall_card(&mut out, &map, "https://api.example/v1");
        let text = String::from_utf8(out).expect("utf-8");

        assert!(text.starts_with(r#"{"object":"card","id":"#), "object and id lead: {}", &text[..40]);
        let at = |needle: &str| text.find(needle).unwrap_or_else(|| panic!("{needle} missing"));
        // `name` before `prices` before the optional tail, and `security_stamp` AFTER the ids —
        // upstream's order, which alphabetical sorting would not produce for any of these.
        assert!(at(r#""name":"#) < at(r#""prices":"#));
        assert!(at(r#""prices":"#) < at(r#""watermark":"#));
        assert!(at(r#""watermark":"#) < at(r#""cardmarket_id":"#));
        assert!(at(r#""cardmarket_id":"#) < at(r#""security_stamp":"#));
    }

    #[test]
    fn cmc_is_written_as_a_decimal() {
        // api.scryfall.com answers `"cmc":1.0`, not `"cmc":1` -- the field is decimal because
        // fractional mana values are real (Little Girl costs {HW} and answers `"cmc":0.5`). The
        // stored value arrives as an INTEGER, because `magic.cards.cmc` is an integer column, and
        // writing it straight through is what produced `1`.
        let serde_json::Value::Object(map) = json!({
            "name": "Lightning Bolt", "scryfall_id": "01000000-0000-0000-0000-000000000007", "cmc": 1,
        }) else {
            panic!()
        };
        let mut out = Vec::new();
        write_scryfall_card(&mut out, &map, "https://api.example/v1");
        let text = String::from_utf8(out).expect("utf-8");
        assert!(text.contains(r#""cmc":1.0"#), "cmc must be decimal: {text}");

        // And a card with no mana value at all still says so.
        let serde_json::Value::Object(none) = json!({
            "name": "Ancestral Vision", "scryfall_id": "01000000-0000-0000-0000-000000000008",
        }) else {
            panic!()
        };
        let mut out = Vec::new();
        write_scryfall_card(&mut out, &none, "https://api.example/v1");
        assert!(String::from_utf8(out).expect("utf-8").contains(r#""cmc":null"#));
    }
}
