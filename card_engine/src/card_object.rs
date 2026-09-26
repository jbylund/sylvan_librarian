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
//!     would come out ALPHABETICAL — which is also why every nested object the engine hands over
//!     (a face, a related card, `legalities`) arrives alphabetical and is re-ordered here.
//!   - It is faster, which is the point: no intermediate tree, and no freshly allocated `String`
//!     key per field per card.
//!
//! Key order is API.SCRYFALL.COM'S, at every level. It used to be this module's own dict literal,
//! on the reading that Scryfall's order was a gratuitous change for clients; but `/cards/*` is held
//! to compatibility with Scryfall, and every object this wrote differed from Scryfall's in key
//! order — the top level, every face, every related card, `legalities` — on 63 of 63 printings of
//! a differential against api.scryfall.com (2026-09-26). `objects.py` builds the same sequence, and
//! `fixtures/card_object_parity.json` holds the two to the same BYTES, Scryfall's own objects
//! among its cases.

use serde_json::{Map, Value};

/// Scryfall's shared card back — the `card_back_id` of every one-image printing but the few thousand
/// that print another (Collectors' Edition, the oversized and memorabilia sets, planes, schemes,
/// vanguards, attractions). The row names those in its own `card_back_id`; this is the default.
pub(crate) const CARD_BACK_ID: &str = "0aeebaf5-8c7d-4636-9e82-8c27447861f7";

/// Image size -> file extension, in Scryfall's own order.
///
/// ELEVEN, not the six this file shipped with. Scryfall added five webp sizes — `thumb`, `grid`,
/// `display`, `art`, `crop` — and every card object it serves carries all eleven; a six-key
/// `image_uris` differed from Scryfall on every card object emitted.
///
/// Unconditional, and measured that way: across all 540,484 printings in the 2026-08-16 all_cards
/// bulk, `image_uris` is either wholly ABSENT (8,444 cards, 7,641 faces — the layouts whose picture
/// lives on the other level) or carries exactly these eleven keys in exactly this order. No card,
/// face, layout or `image_status` carries a partial set, so there is no per-key conditionality to
/// round-trip the way `printed_*` has.
///
/// Derived, not stored: the same scan confirms all eleven URLs are the same pure function of the id
/// and the face on every one of the 548,604 objects that has them — `art_crop` and `art` are
/// different sizes of one path, not a stored pair. These five cost zero archive bytes, which is why
/// they are a table and not a column.
///
/// NOT the `version=` vocabulary of `format=image`, which stays six: measured against
/// api.scryfall.com, `version=thumb` redirects to the LARGE jpg, the same fallback `version=bogus`
/// gets, and the same for grid/display/art/crop.
const IMAGE_EXTENSIONS: [(&str, &str); 11] = [
    ("small", "jpg"),
    ("normal", "jpg"),
    ("large", "jpg"),
    ("png", "png"),
    ("art_crop", "jpg"),
    ("border_crop", "jpg"),
    ("thumb", "webp"),
    ("grid", "webp"),
    ("display", "webp"),
    ("art", "webp"),
    ("crop", "webp"),
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

/// Like `str_of`, but an empty string is a VALUE rather than an absence.
///
/// Scryfall distinguishes the two and this port collapsed them: a basic land's `mana_cost` is `""`
/// on 61,908 of the 540,484 printings in the 2026-08-16 bulk, its `oracle_text` is `""` on 7,266,
/// and `artist` is `""` on 965 — and all three came out of here as `null`. The distinction is safe
/// to draw because the three keys are always PRESENT where they are emitted at all: `mana_cost` is
/// on every one of the 532,040 rows that is not a two-image layout, `oracle_text` on every one of
/// the 528,386 that is not multi-faced, and `artist` on all 540,484. A `null` from this accessor is
/// a row that carried no key at all, which only a hand-built one does.
fn present_str_of<'a>(row: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    match row.get(key) {
        Some(Value::String(s)) => Some(s),
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

/// A string key Scryfall sends only when the card HAS a value: absent (or empty) omits the key
/// rather than writing null.
fn write_opt_str(out: &mut Vec<u8>, first: &mut bool, key: &str, value: Option<&str>) {
    if let Some(s) = value {
        write_key(out, first, key);
        write_json_str(out, s);
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

/// Scryfall's URL slug for a card name.
///
/// NOT the folklore "non-alphanumerics collapse to hyphens" rule this file first shipped — that
/// rule hyphenates apostrophes (`erayo-s-essence`) and serves raw UTF-8 (`jötun-grunt`) where
/// production Scryfall deletes the apostrophe and percent-encodes the bytes. The real rule,
/// verified against the `scryfall_uri` of all 540,484 printings in the 2026-08-16 all_cards bulk
/// (zero mismatches):
///
///   1. lowercase;
///   2. DELETE `' " , . /` and the curly quotes U+201C/U+201D ("S.H.I.E.L.D." -> `shield`,
///      `Henzie "Toolbox" Torre` -> `henzie-toolbox-torre`; U+201E is NOT deleted — the de
///      printing `Henzie „Der Beschaffer" Torre` keeps it);
///   3. each run of ASCII spaces becomes one hyphen — literal hyphens pass through and may stack
///      (ru "Пламенник - военный разведчик" keeps `---`), and nothing is trimmed ("Humming-" and
///      "With Great Power . . ." both keep their trailing hyphen);
///   4. everything else survives verbatim (`:`, `!`, `&`, `、`, `・`, fullwidth punctuation,
///      U+00A0) and is then UTF-8 percent-encoded, uppercase hex, sparing exactly the bytes the
///      corpus serves literally: alphanumerics and `!&()+-:;=_`.
fn slug(name: &str) -> String {
    let mut hyphenated = String::with_capacity(name.len());
    let mut prev_space = false;
    for ch in name.chars().flat_map(char::to_lowercase) {
        if matches!(ch, '\'' | '"' | ',' | '.' | '/' | '\u{201C}' | '\u{201D}') {
            continue;
        }
        if ch == ' ' {
            if !prev_space {
                hyphenated.push('-');
            }
            prev_space = true;
        } else {
            prev_space = false;
            hyphenated.push(ch);
        }
    }
    percent_encode_path(&hyphenated)
}

/// UTF-8 percent-encoding, uppercase hex, sparing exactly the bytes `scryfall_uri` serves
/// literally across the bulk corpus: alphanumerics and `!&()+-:;=_`. Shared by the slug and the
/// collector-number segment, which Scryfall encodes the same way: oarc's `1★` is
/// `/card/oarc/1%E2%98%85/…` and arn's `2†` is `/card/arn/2%E2%80%A0/…` (both live, 2026-09-26),
/// where this writer used to serve the raw UTF-8.
fn percent_encode_path(text: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut out = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'!' | b'&' | b'(' | b')' | b'+' | b'-'
            | b':' | b';' | b'=' | b'_' => out.push(*byte as char),
            _ => {
                out.push('%');
                out.push(HEX[(byte >> 4) as usize] as char);
                out.push(HEX[(byte & 0xf) as usize] as char);
            }
        }
    }
    out
}

/// The layouts whose faces each get their OWN image — and, with it, their own copy of every value
/// the one-image layouts keep at the top level.
///
/// This is the single fact the whole multi-face branch turns on, and it is a property of the
/// LAYOUT, not of anything the row carries: a transform card's front and back are two photographs,
/// so Scryfall puts `image_uris`, `colors`, `power`, `illustration_id`, `flavor_text` and the rest
/// on the faces and sends NO top-level copy (and no `card_back_id` — there is no shared back). A
/// split or adventure card is ONE photograph of one piece of cardboard, so Scryfall sends one
/// top-level `image_uris` and one top-level `colors`, and its faces carry only text.
///
/// Verified exhaustively against the 2026-08-16 all_cards bulk: of 540,484 printings, every row of
/// these five layouts has per-face `image_uris` and no top-level one, and every row of every other
/// layout has the reverse — zero exceptions in either direction. The port used to serve per-face
/// URLs for all multi-face cards, which invented a `.../back/...` URL with no image behind it on
/// every split, flip, adventure and prepare printing.
const TWO_IMAGE_LAYOUTS: [&str; 5] =
    ["art_series", "double_faced_token", "modal_dfc", "reversible_card", "transform"];

/// The multi-face layouts a SEARCH LINK spells with the JOINED name — `related_uris.edhrec` and
/// every marketplace fallback in `purchase_uris`, which take one and the same string.
///
/// EDHREC files a transforming or adventuring card under its front face (`cc=Delver+of+Secrets`,
/// `cc=Brazen+Borrower`, `cc=Erayo%2C+Soratami+Ascendant`, `cc=Agadeem%27s+Awakening`) and a split
/// or double-backed card under both halves (`cc=Fire+%2F%2F+Ice`, `cc=Wear+%2F%2F+Tear`,
/// `cc=Temple+Garden+%2F%2F+Temple+Garden`, `cc=Punchcard+%2F%2F+Punchcard`) — all eight verified
/// against api.scryfall.com. `art_series` sits with the front-face group, not with the other
/// two-image layouts.
///
/// THE MARKETPLACES SPLIT THE SAME WAY, which is why the list is no longer edhrec's alone. Measured
/// on api.scryfall.com 2026-08-31, over `unique=prints`, on the first printing of each card whose
/// ids are MISSING so the SEARCH form is what gets emitted (a printing WITH the id gets a product
/// link and says nothing), and reading the tcgplayer term out of the `u=` parameter of Scryfall's
/// own partner redirect:
///
///   split               Bind // Liberate                      cmb2/88   cardhoarder `Bind // Liberate`
///   reversible_card     Mechtitan // Mechtitan                sld/1969  cardhoarder `Mechtitan // Mechtitan`
///   double_faced_token  Snake // Zombie                       cc2/9     tcgplayer, cardmarket AND
///                                                                       cardhoarder, all `Snake // Zombie`
///   split               Who // What // When // Where // Why   und/75    cardhoarder the whole
///                                                                       five-part name
///   adventure           Champions of Archery // Join the …    ph19/4    cardmarket, cardhoarder
///                                                                       `Champions of Archery`
///   flip                Curse of the Fire Penguin // …        unh/73    cardhoarder `Curse of the
///                                                                       Fire Penguin`
///   art_series          Aang and Katara // Aang and Katara    atle/8    tcgplayer, cardmarket,
///                                                                       cardhoarder `Aang and Katara`
///   transform           Delver of Secrets // Insectile …      sld/2367  cardhoarder `Delver of Secrets`
///
/// The `tcgplayer_infinite_*` links in `related_uris` are the exception that stays: they keep the
/// joined name on EVERY layout, split or not.
const JOINED_SEARCH_LAYOUTS: [&str; 3] = ["double_faced_token", "reversible_card", "split"];

/// The layout whose printings keep NOTHING of the card at top level — see `write_scryfall_card`.
///
/// One name rather than a set, because it is one: nothing else in the corpus omits `oracle_id`,
/// and nothing else puts `layout` on a face.
const REVERSIBLE_LAYOUT: &str = "reversible_card";

/// Top-level keys a two-image layout does not carry, because they belong to a face there.
///
/// `watermark` is deliberately NOT here — it is face-owned on EVERY faced layout, not only the
fn is_face_owned_key(key: &str) -> bool {
    matches!(
        key,
        "colors"
            | "card_back_id"
            | "illustration_id"
            | "power"
            | "toughness"
            | "loyalty"
            | "flavor_text"
            | "color_indicator"
    )
}

/// The languages Scryfall writes into the scryfall_uri path — its ten print localizations,
/// exactly. The glyph and novelty languages (ph, qya, he, la, grc, ar, sa, dw) get NO path
/// segment: a ph Elesh Norn lives at `/card/one/414/elesh-norn-mother-of-machines`, English form.
const SLUG_LANG_SEGMENTS: [&str; 10] = ["de", "es", "fr", "it", "ja", "ko", "pt", "ru", "zhs", "zht"];

/// `scryfall_uri`: `https://scryfall.com/card/{set}/{number}[/{lang}]/{slug}?utm_source=api`.
///
/// A foreign printing keeps the language segment and takes the plain English slug
/// (ody/243/zhs -> `/zhs/holistic-wisdom`, verified live). Scryfall also writes a
/// `slug(printed name)-(slug(english name))` path where it HAS a printed name (grn/212/pt is
/// `ego-%C3%A0-deriva-(unmoored-ego)`); reproducing that needs the printed name in the archive,
/// which this branch does not store yet, and the English fallback is what it serves until then.
fn scryfall_uri(name: &str, set_code: &str, number: &str, lang: &str) -> String {
    let segment = if SLUG_LANG_SEGMENTS.contains(&lang) { format!("{lang}/") } else { String::new() };
    let number = percent_encode_path(number);
    format!("https://scryfall.com/card/{set_code}/{number}/{segment}{}?utm_source=api", slug(name))
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
///
/// `gatherer` LEADS the object when the printing has multiverse ids, built from the FIRST id,
/// with `printed=true` for every translated printing and `printed=false` for English — verified
/// against the bulk corpus at 540,430 of 540,484 printings. Most of the 54 exceptions were the
/// PHYREXIAN and QUENYA printings (one-ph, ltc-qya), and they are a rule rather than an exception:
/// measured on api.scryfall.com 2026-09-26, every `lang:ph` and `lang:qya` printing that links to
/// Gatherer at all says `printed=false` (19 and 3 of them) — the glyph languages have no Gatherer
/// translation. What is left (dd2's two ja printings) lives on Scryfall's side of the wire and is
/// not derivable from the row, so it stays a known limit.
///
/// `edhrec` takes `search_name`, which is the front face's on most multi-face layouts — see
/// JOINED_SEARCH_LAYOUTS. The two tcgplayer searches take the joined name on every layout.
///
/// A printing with a `content_warning` keeps the gatherer link and NOTHING ELSE: Scryfall sends no
/// tcgplayer or edhrec entry for it (measured on leg/62, Invoke Prejudice), just as it sends no
/// `purchase_uris` — the marketplace links are what the warning withdraws.
fn write_related_uris(
    out: &mut Vec<u8>,
    name: &str,
    search_name: &str,
    multiverse_first: Option<u64>,
    lang: &str,
    content_warning: bool,
) {
    let quoted = quote_plus(name);
    out.push(b'{');
    let mut first = true;
    if let Some(id) = multiverse_first {
        let printed = if matches!(lang, "en" | "ph" | "qya") { "false" } else { "true" };
        write_key(out, &mut first, "gatherer");
        write_json_str(
            out,
            &format!("https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid={id}&printed={printed}"),
        );
    }
    if content_warning {
        out.push(b'}');
        return;
    }
    for (key, url) in [
        (
            "tcgplayer_infinite_articles",
            format!("https://www.tcgplayer.com/search/articles?productLineName=magic&q={quoted}"),
        ),
        (
            "tcgplayer_infinite_decks",
            format!("https://www.tcgplayer.com/search/decks?productLineName=magic&q={quoted}"),
        ),
        ("edhrec", format!("https://edhrec.com/route/?cc={}", quote_plus(search_name))),
    ] {
        write_key(out, &mut first, key);
        write_json_str(out, &url);
    }
    out.push(b'}');
}

/// The FRONT face's name — everything before the ` // ` a multi-faced card's name joins on.
///
/// The half of `search_name` that is not the joined name — see JOINED_SEARCH_LAYOUTS, which
/// decides which of the two a given layout gets. Scryfall searches TCGplayer for
/// `Invasion of Alara`, never for `Invasion of Alara // Awaken the Maelstrom`, because on a
/// transforming card the joined string matches no product; on a split or a double-faced token it
/// is the product, and there the joined name is what it searches for.
///
/// Applied through `search_name` at the top of `write_scryfall_card` and NOWHERE else: doing it
/// again inside `write_purchase_uris` is exactly the bug that spelled `Snake // Zombie` as `Snake`.
fn front_face_name(name: &str) -> &str {
    name.split_once(" // ").map_or(name, |(front, _)| front)
}

/// `purchase_uris`, rebuilt from the marketplace ids — or, for a key whose id this printing does
/// not have, from a NAME SEARCH on that marketplace.
///
/// All three keys are always present. Scryfall emits the search form per KEY, not per card: an
/// English printing with a TCGplayer and a Cardmarket id but no MTGO id gets two product links
/// and a cardhoarder search (verified live across khm). Every foreign printing takes the search
/// form on all three, because marketplace product ids are carried by the English printing alone —
/// they never reach an annex row, and inventing one would point at the wrong product. Emitting
/// nothing was the alternative, and it made `purchase_uris` an empty object on 426,416 printings.
///
/// `search_name` IS `write_related_uris`' — the caller decides the string, and all three
/// marketplaces split by layout exactly the way edhrec does (the measurements are on
/// JOINED_SEARCH_LAYOUTS). This took the joined `name` and cut the front face off it here, on
/// EVERY layout, which searched for `Snake // Zombie` (cc2/9) as `Snake` and
/// `Who // What // When // Where // Why` (und/75) as `Who` against a Scryfall that spells both
/// whole.
fn write_purchase_uris(out: &mut Vec<u8>, row: &Map<String, Value>, search_name: &str) {
    let quoted = quote_plus(search_name);
    out.push(b'{');
    let mut first = true;
    write_key(out, &mut first, "tcgplayer");
    write_json_str(
        out,
        &match u64_of(row, "tcgplayer_id") {
            Some(id) => format!("https://www.tcgplayer.com/product/{id}?page=1"),
            None => format!("https://www.tcgplayer.com/search/magic/product?productLineName=magic&q={quoted}&view=grid"),
        },
    );
    write_key(out, &mut first, "cardmarket");
    write_json_str(
        out,
        &match u64_of(row, "cardmarket_id") {
            Some(id) => format!("https://www.cardmarket.com/en/Magic/Products?idProduct={id}"),
            None => format!("https://www.cardmarket.com/en/Magic/Products/Search?searchString={quoted}"),
        },
    );
    write_key(out, &mut first, "cardhoarder");
    write_json_str(
        out,
        &match u64_of(row, "mtgo_id") {
            Some(id) => format!("https://www.cardhoarder.com/cards/{id}"),
            None => format!("https://www.cardhoarder.com/cards?data%5Bsearch%5D={quoted}"),
        },
    );
    out.push(b'}');
}

/// The joined top-level `mana_cost` a one-image multi-face card carries.
///
/// Scryfall's rule, checked against all 3,654 split/flip/adventure/prepare printings in the
/// 2026-08-16 bulk with zero misses: `" // "` between the faces that HAVE a cost, skipping the
/// ones that do not. Fire // Ice is `"{1}{R} // {1}{U}"`; flipped Erayo, whose back face carries
/// `"mana_cost": ""`, is `"{1}{U}"` and not `"{1}{U} // "`.
///
/// Derived rather than stored because the ingest cannot preserve it: transform_row overlays each
/// face onto the parent card, so the stored top-level cost is the FRONT face's alone.
fn joined_mana_cost(faces: &[Value]) -> String {
    faces
        .iter()
        .filter_map(|face| match face {
            Value::Object(map) => str_of(map, "mana_cost"),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join(" // ")
}

/// A face's keys in Scryfall's own order — `object` (and, on a reversible printing, the card's
/// `oracle_id`) lead, and `image_uris` closes. Merged from every face object in a 63-printing
/// api.scryfall.com sample (2026-09-26, zero ordering conflicts), with the pairs that sample never
/// shows together taken from the corpus-wide measurements already on record: `name -> flavor_name
/// -> mana_cost` (vow/338, sld/1079) and `flavor_text -> watermark -> artist` (all 1,075 face
/// watermarks in the 2026-08-16 bulk).
///
/// The engine hands a face over as a map, which serde_json (no `preserve_order`) iterates
/// ALPHABETICALLY — so this writer used to emit `artist, colors, illustration_id, mana_cost, name`,
/// the reverse of the object on every faced printing Scryfall serves. A key this list does not
/// name is written after these, in the map's order, rather than dropped.
const FACE_KEY_ORDER: [&str; 20] = [
    "layout",
    "name",
    "printed_name",
    "flavor_name",
    "mana_cost",
    "type_line",
    "printed_type_line",
    "oracle_text",
    "printed_text",
    "colors",
    "color_indicator",
    "power",
    "toughness",
    "loyalty",
    "defense",
    "flavor_text",
    "watermark",
    "artist",
    "artist_id",
    "illustration_id",
];

/// `preview`'s keys in Scryfall's order — the same on every previewed printing in the sample. The
/// engine hands the object over as a map, which iterates alphabetically, and the SQL lane's jsonb
/// stores keys shortest-first; neither is this.
const PREVIEW_KEY_ORDER: [&str; 3] = ["source", "source_uri", "previewed_at"];

/// A related card's keys in Scryfall's order. `uri` closes it and is derived — the card's own
/// `/cards/:id` on this host, exactly as the top-level `uri` is — so nothing is stored for it.
const RELATED_KEY_ORDER: [&str; 5] = ["object", "id", "component", "name", "type_line"];

/// Scryfall's order of the formats in `legalities` — its own, fixed, and the same on every card
/// object (63 of 63 in the sample). The engine decodes the legality word into a map, which
/// iterates alphabetically; a format Scryfall adds later that this list does not know yet is
/// written after these, in the map's order, rather than dropped.
const LEGALITY_ORDER: [&str; 23] = [
    "standard",
    "future",
    "historic",
    "timeless",
    "gladiator",
    "pioneer",
    "modern",
    "legacy",
    "pauper",
    "vintage",
    "penny",
    "commander",
    "oathbreaker",
    "standardbrawl",
    "brawl",
    "competitivebrawl",
    "alchemy",
    "paupercommander",
    "duel",
    "oldschool",
    "premodern",
    "predh",
    "tlr",
];

/// A map's members in a fixed key order, then any member the order does not name, in the map's
/// own order. Values are written verbatim, `null` included.
fn write_ordered_object(out: &mut Vec<u8>, map: &Map<String, Value>, order: &[&str]) {
    out.push(b'{');
    let mut first = true;
    for key in order {
        if let Some(v) = map.get(*key) {
            write_value(out, &mut first, key, v);
        }
    }
    for (key, v) in map {
        if !order.contains(&key.as_str()) {
            write_value(out, &mut first, key, v);
        }
    }
    out.push(b'}');
}

/// `all_parts`, each related card in Scryfall's key order and closed by its derived `uri`.
fn write_all_parts(out: &mut Vec<u8>, parts: &[Value], base_url: &str) {
    out.push(b'[');
    for (index, part) in parts.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        let Value::Object(map) = part else {
            serde_json::to_writer(&mut *out, part).expect("writing a Value cannot fail");
            continue;
        };
        out.push(b'{');
        let mut first = true;
        for key in RELATED_KEY_ORDER {
            if let Some(v) = map.get(key) {
                write_value(out, &mut first, key, v);
            }
        }
        if let Some(id) = str_of(map, "id") {
            write_key(out, &mut first, "uri");
            write_json_str(out, &format!("{base_url}/cards/{id}"));
        }
        for (key, v) in map {
            if !RELATED_KEY_ORDER.contains(&key.as_str()) && key != "uri" {
                write_value(out, &mut first, key, v);
            }
        }
        out.push(b'}');
    }
    out.push(b']');
}

/// Epoch seconds as Scryfall's `image_updated_at`: ISO-8601 UTC to the second, `Z`-suffixed
/// (`"2026-07-13T00:36:48Z"`). The store keeps the seconds (the image cache-buster is the same
/// number), so the string is rebuilt here — Howard Hinnant's civil-from-days, exact for any date.
fn iso8601_utc(secs: u64) -> String {
    let days = i64::try_from(secs / 86_400).unwrap_or(0);
    let rem = secs % 86_400;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z", rem / 3_600, rem % 3_600 / 60, rem % 60)
}

/// The card's faces, with the two keys the engine deliberately does not store re-added: `object`
/// is the constant, and a face's `image_uris` is the card's own CDN function with the face swapped
/// — on the two-image layouts, which are the only ones whose faces have their own picture. Keys in
/// Scryfall's order (FACE_KEY_ORDER), never the row map's alphabetical one.
fn write_faces(
    out: &mut Vec<u8>,
    faces: &[Value],
    scryfall_id: &str,
    updated_at: Option<u64>,
    two_image: bool,
    // The card's `oracle_id` and `cmc`, to be written on EVERY face -- `Some` only for a
    // reversible printing, which is the one layout whose faces carry them (and whose top-level
    // object omits them). Both faces of all 81 send the card's own values, never a second one.
    // Scryfall puts the id right after `object` and the cmc right after `mana_cost`.
    card_ids: Option<(&str, Option<&Value>)>,
) {
    // Absent stays absent: null, "" and [] mean Scryfall did not send this face that key --
    // EXCEPT for `mana_cost` and `oracle_text`, where "" is a value Scryfall does send. Every
    // face of every multi-face printing in the corpus carries both keys (8,620 of 8,620 transform
    // faces, 4,356 of them with an empty cost), so an empty string there is a costless back face,
    // never an omission. `colors` is a face key only where the faces own their own art: every
    // face of every two-image printing carries one, empty included (Agadeem, the Undercrypt is
    // colorless and still sends `"colors": []`), and no face of a split, flip, adventure or
    // prepare printing carries one at all. The engine always writes the key, so both halves of
    // that are decided here.
    let emits = |key: &str, value: &Value| -> bool {
        if key == "colors" {
            return two_image && !value.is_null();
        }
        match value {
            Value::Null => false,
            Value::String(s) => !s.is_empty() || matches!(key, "mana_cost" | "oracle_text"),
            Value::Array(a) => !a.is_empty(),
            _ => true,
        }
    };
    out.push(b'[');
    for (index, face) in faces.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        out.push(b'{');
        let mut first = true;
        write_key(out, &mut first, "object");
        write_json_str(out, "card_face");
        if let Some((oid, _)) = card_ids {
            write_key(out, &mut first, "oracle_id");
            write_json_str(out, oid);
        }
        let empty = Map::new();
        let map = match face {
            Value::Object(map) => map,
            _ => &empty,
        };
        for key in FACE_KEY_ORDER {
            if let Some(value) = map.get(key)
                && emits(key, value)
            {
                write_value(out, &mut first, key, value);
            }
            if key == "mana_cost"
                && let Some((_, cmc)) = card_ids
            {
                write_key(out, &mut first, "cmc");
                match cmc.and_then(serde_json::Value::as_f64) {
                    Some(v) => serde_json::to_writer(&mut *out, &v).expect("number"),
                    None => out.extend_from_slice(b"null"),
                }
            }
        }
        // Anything the order does not name, in the map's order. The keys this writer derives
        // itself are never copied from the row a second time: `object` and `image_uris` always,
        // and a reversible face's `oracle_id` and `cmc`, which are the card's.
        for (key, value) in map {
            if !FACE_KEY_ORDER.contains(&key.as_str())
                && !matches!(key.as_str(), "object" | "image_uris")
                && !(card_ids.is_some() && matches!(key.as_str(), "oracle_id" | "cmc"))
                && emits(key, value)
            {
                write_value(out, &mut first, key, value);
            }
        }
        if two_image {
            write_key(out, &mut first, "image_uris");
            write_image_uris(out, scryfall_id, updated_at, if index == 0 { "front" } else { "back" });
        }
        out.push(b'}');
    }
    out.push(b']');
}

// ─── the card object ─────────────────────────────────────────────────────────

/// Write one engine row as a Scryfall card object, in api.scryfall.com's own key order.
///
/// `base_url` is the host self-referencing URIs should address — the deployment's own, not
/// Scryfall's, so a client following `uri` or `prints_search_uri` stays on this API.
///
/// THE ORDER IS SCRYFALL'S (2026-09-26). This writer used to follow its own dict literal, and so
/// did `objects.py`: every card object either built differed from api.scryfall.com's in key order
/// on the top level (`arena_id` is Scryfall's 5th-8th key and was this one's ~50th), on every face,
/// on every related card and in `legalities` — 63 of 63 printings in a differential against the
/// live API. The order below is the merge of all 63 objects (zero conflicts), each conditional key
/// in the one position Scryfall gives it — the residue keys included (`resource_id`, `artist_ids`,
/// `variation_of`, `attraction_lights`, a non-shared `card_back_id`, `preview`,
/// `content_warning`), each written verbatim where the row carries it and omitted where it does
/// not.
pub fn write_scryfall_card(out: &mut Vec<u8>, row: &Map<String, Value>, base_url: &str) {
    let scryfall_id = str_of(row, "scryfall_id").unwrap_or("");
    let oracle_id = str_of(row, "oracle_id").unwrap_or("");
    let name = str_of(row, "name").unwrap_or("");
    let set_code = str_of(row, "set_code").unwrap_or("");
    let number = str_of(row, "collector_number").unwrap_or("");
    let set_id = str_of(row, "set_id");
    let lang = str_of(row, "lang").unwrap_or("en");
    let image_updated_at = u64_of(row, "image_updated_at");
    let faces = list_of(row, "card_faces").filter(|f| !f.is_empty());
    let layout = str_of(row, "layout");
    // Only ever true for a card that HAS faces: the two-image layouts are all multi-face.
    let two_image = faces.is_some() && layout.is_some_and(|l| TWO_IMAGE_LAYOUTS.contains(&l));
    // A REVERSIBLE printing keeps NOTHING of the card at top level -- not even the three keys
    // every other multi-face layout keeps. Measured across the whole 2026-08-16 all_cards bulk:
    // all 81 of them omit `oracle_id`, `cmc` and `type_line`, where a `transform` printing sends
    // all three (verified live on Delver of Secrets // Insectile Aberration). Its FACES carry
    // their own `oracle_id` and `cmc` instead -- the card's, on both faces, 0 of 81 disagreeing --
    // which is why omitting the top-level pair loses nothing.
    let reversible = layout == Some(REVERSIBLE_LAYOUT);
    // The name a SEARCH LINK spells: the joined one, except on the layouts whose searches take the
    // front face (see JOINED_SEARCH_LAYOUTS). `related_uris.edhrec` and every `purchase_uris`
    // fallback take THIS string; the two `tcgplayer_infinite_*` links take the joined `name`.
    let search_name = if faces.is_some() && !layout.is_some_and(|l| JOINED_SEARCH_LAYOUTS.contains(&l)) {
        front_face_name(name)
    } else {
        name
    };
    // Scryfall's `content_warning`, verbatim from the residue. `true` withdraws every marketplace
    // link -- see `write_related_uris`.
    let content_warning = row.get("content_warning") == Some(&Value::Bool(true));
    // A residue value, verbatim, when the row carries one (see lib.rs's PRINTING_EXTRA_KEYS): the
    // engine answers `null` for a key the printing does not carry, and that is an omission.
    let extra = |key: &str| row.get(key).filter(|v| !v.is_null());
    let is_tag = |tag: &str| {
        list_of(row, "card_is_tags").is_some_and(|tags| tags.iter().any(|t| t.as_str() == Some(tag)))
    };
    // A value that belongs to a face on a two-image layout (see `is_face_owned_key`) has no
    // top-level copy there.
    let top_level = |key: &str| !(two_image && is_face_owned_key(key));

    out.push(b'{');
    let mut first = true;

    write_key(out, &mut first, "object");
    write_json_str(out, "card");
    write_key(out, &mut first, "id");
    write_json_str(out, scryfall_id);
    if !reversible {
        write_key(out, &mut first, "oracle_id");
        write_json_str(out, oracle_id);
    }
    write_list(out, &mut first, "multiverse_ids", list_of(row, "multiverse_ids"));
    // Between the multiverse ids and the client ids, on the printings that carry one (the newest:
    // msc/806, soc/190, sos/113) — an opaque hash, verbatim.
    if let Some(v) = extra("resource_id") {
        write_value(out, &mut first, "resource_id", v);
    }
    // The marketplace and client ids, where Scryfall puts them: straight after the multiverse ids.
    for key in ["mtgo_id", "mtgo_foil_id", "arena_id", "tcgplayer_id", "tcgplayer_etched_id", "cardmarket_id"] {
        if let Some(v) = num_of(row, key) {
            write_value(out, &mut first, key, v);
        }
    }
    write_key(out, &mut first, "name");
    write_json_str(out, name);
    write_key(out, &mut first, "lang");
    write_json_str(out, lang);
    write_str_or_null(out, &mut first, "released_at", str_of(row, "released_at"));
    write_key(out, &mut first, "uri");
    write_json_str(out, &format!("{base_url}/cards/{scryfall_id}"));
    write_key(out, &mut first, "scryfall_uri");
    write_json_str(out, &scryfall_uri(name, set_code, number, lang));
    write_str_or_null(out, &mut first, "layout", str_of(row, "layout"));
    write_bool(out, &mut first, "highres_image", bool_of(row, "highres_image"));
    write_str_or_null(out, &mut first, "image_status", str_of(row, "image_status"));
    if let Some(secs) = image_updated_at {
        write_key(out, &mut first, "image_updated_at");
        write_json_str(out, &iso8601_utc(secs));
    }
    // A multi-face card carries its faces and NOT the top-level ORACLE TEXT they replace; a
    // single-faced one carries the text and no `card_faces`. `mana_cost` and `image_uris` are
    // the two the multi-face branch keeps, on the one-image layouts only: one piece of cardboard
    // has one picture and one printed cost, so Scryfall sends both at top level for
    // split/flip/adventure/prepare — and neither for transform/modal_dfc, where each face has its
    // own.
    if faces.is_none() || !two_image {
        write_key(out, &mut first, "image_uris");
        write_image_uris(out, scryfall_id, image_updated_at, "front");
    }
    match faces {
        // The faces' costs, joined — see `joined_mana_cost`.
        Some(faces) if !two_image => {
            write_key(out, &mut first, "mana_cost");
            write_json_str(out, &joined_mana_cost(faces));
        }
        Some(_) => {}
        // An empty string is a VALUE — every basic land carries `"mana_cost": ""` — so it reads
        // through `present_str_of` rather than the empty-is-absent `str_of`.
        None => write_str_or_null(out, &mut first, "mana_cost", present_str_of(row, "mana_cost")),
    }
    // `cmc` and `type_line` are the two the ordinary multi-face branch keeps and a REVERSIBLE
    // printing does not — see the note on `reversible` above.
    if !reversible {
        write_key(out, &mut first, "cmc");
        // As a DECIMAL, which is what api.scryfall.com answers with: `"cmc":1.0`, not `"cmc":1`
        // (see https://api.scryfall.com/cards/named?exact=Lightning+Bolt). Writing the stored
        // number directly emits `1`, because the engine holds cmc as an integer -- and that would
        // also put the engine at odds with `toScryfallCard`, which carries the same value as a
        // decimal. The two must agree byte for byte: tests/routes/card-object-parity.test.ts holds
        // them to it.
        //
        // The PRECISION behind the formatting is now real too: the stored value is an
        // `Option<f32>` (`opt_f32(d, "cmc")` in lib.rs, `jv_opt_f32` in core_api.rs), so Little
        // Girl's 0.5 survives the archive and arrives here as 0.5 rather than 0. The corpus still
        // excludes funny sets -- this is the capability, not a decision to import them.
        match num_of(row, "cmc").and_then(serde_json::Value::as_f64) {
            Some(v) => serde_json::to_writer(&mut *out, &v).expect("number"),
            None => out.extend_from_slice(b"null"),
        }
        write_str_or_null(out, &mut first, "type_line", str_of(row, "type_line"));
    }
    if faces.is_none() {
        // `""` is a value here too: 7,266 printings carry `"oracle_text": ""`.
        write_str_or_null(out, &mut first, "oracle_text", present_str_of(row, "oracle_text"));
    }
    // Keys Scryfall sends only when the card HAS them. Emitting null instead would differ from
    // Scryfall on every card that lacks them, which for most of these is most cards. The PRINTED
    // strings, so "X" and "1+*" survive -- the `planeswalker_loyalty` the planner filters on is a
    // u8 and loses both -- and a face's on a two-image layout.
    for key in ["power", "toughness", "loyalty"] {
        if top_level(key) {
            write_opt_str(out, &mut first, key, str_of(row, key));
        }
    }
    // `colors` is one of the values a two-image layout keeps on its faces alone (see
    // TWO_IMAGE_LAYOUTS); `color_identity` is the card's and stays at top level on every layout.
    if !two_image {
        write_list(out, &mut first, "colors", list_of(row, "colors"));
    }
    write_list(out, &mut first, "color_identity", list_of(row, "color_identity"));
    write_list(out, &mut first, "keywords", list_of(row, "card_keywords"));
    if let Some(faces) = faces {
        write_key(out, &mut first, "card_faces");
        write_faces(out, faces, scryfall_id, image_updated_at, two_image, reversible.then_some((oracle_id, num_of(row, "cmc"))));
    }
    if let Some(parts) = list_of(row, "all_parts").filter(|a| !a.is_empty()) {
        write_key(out, &mut first, "all_parts");
        write_all_parts(out, parts, base_url);
    }
    if let Some(v) = row.get("legalities").filter(|v| !v.is_null()) {
        write_key(out, &mut first, "legalities");
        match v {
            Value::Object(map) => write_ordered_object(out, map, &LEGALITY_ORDER),
            other => serde_json::to_writer(&mut *out, other).expect("writing a Value cannot fail"),
        }
    }
    write_list(out, &mut first, "games", list_of(row, "games"));
    // `reserved` and `game_changer` are tags rather than columns: both are properties of the
    // card, and the engine stores them in the same is-tag set everything else uses.
    write_bool(out, &mut first, "reserved", is_tag("reserved"));
    write_bool(out, &mut first, "game_changer", is_tag("gamechanger"));
    // Deprecated by `finishes`, and still on every object api.scryfall.com serves.
    write_bool(out, &mut first, "foil", bool_of(row, "foil"));
    write_bool(out, &mut first, "nonfoil", bool_of(row, "nonfoil"));
    write_list(out, &mut first, "finishes", list_of(row, "finishes"));
    write_bool(out, &mut first, "oversized", bool_of(row, "oversized"));
    write_bool(out, &mut first, "promo", bool_of(row, "promo"));
    write_bool(out, &mut first, "reprint", bool_of(row, "reprint"));
    write_bool(out, &mut first, "variation", bool_of(row, "variation"));
    // The printing this one is a variation OF (arn/2† names arn/2), right after the flag.
    if let Some(v) = extra("variation_of") {
        write_value(out, &mut first, "variation_of", v);
    }
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
    write_opt_str(out, &mut first, "watermark", str_of(row, "watermark"));
    if top_level("flavor_text") {
        write_opt_str(out, &mut first, "flavor_text", str_of(row, "flavor_text"));
    }
    // An attraction's lights (unf/200a `[2, 6]`), just ahead of the card back.
    if let Some(v) = extra("attraction_lights") {
        write_value(out, &mut first, "attraction_lights", v);
    }
    // No shared card back on a two-image layout, and no card-level illustration: both belong to a
    // face there, and Scryfall omits the top-level keys entirely. Elsewhere the back is the row's
    // when it names one — the store keeps only the backs that are NOT the shared one — and
    // Scryfall's shared back otherwise.
    if !two_image {
        write_key(out, &mut first, "card_back_id");
        write_json_str(out, str_of(row, "card_back_id").unwrap_or(CARD_BACK_ID));
    }
    write_str_or_null(out, &mut first, "artist", present_str_of(row, "artist"));
    // The artists' uuids, between the credit and the illustration. Present whenever the row carries
    // a list, an empty one included; absent when it carries none.
    if let Some(ids) = list_of(row, "artist_ids") {
        write_list(out, &mut first, "artist_ids", Some(ids));
    }
    if !two_image {
        write_str_or_null(out, &mut first, "illustration_id", str_of(row, "illustration_id"));
    }
    write_str_or_null(out, &mut first, "border_color", str_of(row, "border_color"));
    write_opt_str(out, &mut first, "frame", str_of(row, "frame"));
    if let Some(a) = list_of(row, "frame_effects").filter(|a| !a.is_empty()) {
        write_value(out, &mut first, "frame_effects", &Value::Array(a.clone()));
    }
    write_opt_str(out, &mut first, "security_stamp", str_of(row, "security_stamp"));
    write_bool(out, &mut first, "full_art", bool_of(row, "full_art"));
    write_bool(out, &mut first, "textless", bool_of(row, "textless"));
    write_bool(out, &mut first, "booster", bool_of(row, "booster"));
    write_bool(out, &mut first, "story_spotlight", bool_of(row, "story_spotlight"));
    if let Some(a) = list_of(row, "promo_types").filter(|a| !a.is_empty()) {
        write_value(out, &mut first, "promo_types", &Value::Array(a.clone()));
    }
    for key in ["edhrec_rank", "penny_rank"] {
        if let Some(v) = num_of(row, key) {
            write_value(out, &mut first, key, v);
        }
    }
    // The preview, in its own key order, then the content warning — the last two keys before
    // `prices`.
    if let Some(v) = extra("preview") {
        write_key(out, &mut first, "preview");
        match v {
            Value::Object(map) => write_ordered_object(out, map, &PREVIEW_KEY_ORDER),
            other => serde_json::to_writer(&mut *out, other).expect("writing a Value cannot fail"),
        }
    }
    if let Some(v) = extra("content_warning") {
        write_value(out, &mut first, "content_warning", v);
    }
    write_key(out, &mut first, "prices");
    write_prices(out, row);
    write_key(out, &mut first, "related_uris");
    let multiverse_first = list_of(row, "multiverse_ids").and_then(|ids| ids.first()).and_then(Value::as_u64);
    write_related_uris(out, name, search_name, multiverse_first, lang, content_warning);
    // A printing NO MARKETPLACE SELLS omits the key rather than carrying three dead links, and
    // the rule is the marketplaces rather than `digital` — measured 2026-08-16: prm/80925
    // (games ["mtgo"], digital true) HAS purchase_uris, ymid/59 and khm/A-198 (games ["arena"],
    // digital true) do not. tcgplayer and cardmarket sell cardboard, cardhoarder sells MTGO, and
    // nothing sells Arena. An ABSENT or empty `games` emits: the omission is a positive statement
    // ("this printing is sold nowhere"), and a row that never carried the column has made no such
    // statement. A `content_warning` printing is sold nowhere either, as far as Scryfall's links
    // go — see `write_related_uris`.
    let sold = !content_warning
        && list_of(row, "games").is_none_or(|gs| {
            gs.is_empty() || gs.iter().any(|g| matches!(g.as_str(), Some("paper") | Some("mtgo")))
        });
    if sold {
        write_key(out, &mut first, "purchase_uris");
        write_purchase_uris(out, row, search_name);
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

    /// A FACED printing emits its watermark on the faces and NOWHERE else.
    ///
    /// Measured over the whole 2026-08-16 all_cards bulk: api.scryfall.com sends a top-level
    /// `watermark` on 36,437 printings and on 0 of the 12,098 that have `card_faces`. This port
    /// sent one on all 156 faced printings that carry a face watermark, because the builder's face
    /// overlay copies face 0's value into `card_watermark` and the only gate on the key was the
    /// TWO-IMAGE one — which `split`, `flip`, `adventure` and `prepare` never trip. One piece of
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

    /// A single-faced card carries the text and image_uris; a multi-faced one carries the faces,
    /// and WHERE the picture lives is the layout's answer, not the face count's.
    #[test]
    fn faces_replace_the_top_level_text_they_stand_in_for() {
        let base = json!({
            "name": "Delver of Secrets // Insectile Aberration",
            "scryfall_id": "cd000000-0000-0000-0000-000000000002",
            "layout": "normal", "mana_cost": "{U}", "oracle_text": "top level",
        });

        let single = build(base.clone());
        assert_eq!(single["mana_cost"], "{U}");
        assert!(single.get("card_faces").is_none());
        assert!(single["image_uris"]["small"].as_str().unwrap().contains("/front/"));

        // A TWO-IMAGE layout: each face owns its picture, and the card carries neither the
        // picture nor the values that ride with it.
        let mut two = base.clone();
        two["layout"] = json!("transform");
        two["colors"] = json!(["U"]);
        two["power"] = json!("1");
        two["illustration_id"] = json!("cd000000-0000-0000-0000-0000000000ff");
        two["card_faces"] = json!([
            {"name": "Delver of Secrets", "mana_cost": "{U}", "colors": ["U"]},
            {"name": "Insectile Aberration", "mana_cost": "", "colors": [], "watermark": ""},
        ]);
        let card = build(two);
        assert!(card.get("mana_cost").is_none(), "a two-image card has no top-level mana_cost");
        assert!(card.get("image_uris").is_none(), "...and no top-level image_uris");
        for hoisted in ["colors", "power", "illustration_id", "card_back_id"] {
            assert!(card.get(hoisted).is_none(), "{hoisted} belongs to a face on a transform");
        }
        let faces = card["card_faces"].as_array().expect("faces");
        assert_eq!(faces[0]["object"], "card_face");
        assert!(faces[0]["image_uris"]["png"].as_str().unwrap().contains("/front/"));
        assert!(faces[1]["image_uris"]["png"].as_str().unwrap().contains("/back/"));
        // An empty mana cost is a VALUE on a costless back face, and an empty face colour list is
        // one too — Scryfall sends `"mana_cost": ""` and `"colors": []` on both. An empty
        // watermark is still an absence.
        assert_eq!(faces[1]["mana_cost"], "");
        assert_eq!(faces[1]["colors"], json!([]));
        assert!(faces[1].get("watermark").is_none());

        // A ONE-IMAGE multi-face layout: one picture, one joined cost, both at top level — and
        // the faces carry no picture and no colours at all.
        let mut split = base.clone();
        split["layout"] = json!("split");
        split["name"] = json!("Fire // Ice");
        split["colors"] = json!(["R", "U"]);
        split["card_faces"] = json!([
            {"name": "Fire", "mana_cost": "{1}{R}", "colors": []},
            {"name": "Ice", "mana_cost": "{1}{U}", "colors": []},
        ]);
        let card = build(split);
        assert_eq!(card["mana_cost"], "{1}{R} // {1}{U}", "the faces' costs, joined");
        assert!(card["image_uris"]["png"].as_str().unwrap().contains("/front/"));
        assert_eq!(card["colors"], json!(["R", "U"]));
        assert_eq!(card["card_back_id"], CARD_BACK_ID);
        let faces = card["card_faces"].as_array().expect("faces");
        assert!(faces[0].get("image_uris").is_none(), "a split's faces have no picture of their own");
        assert!(faces[1].get("image_uris").is_none());
        assert!(faces[0].get("colors").is_none(), "...and no colours of their own");
    }

    /// All ELEVEN sizes, in Scryfall's order, with the webp five spelled out.
    ///
    /// A key-SET test, not a URL-shape one: the six-key version of this table was wrong on every
    /// card object for as long as it shipped, and neither parity harness could see it because both
    /// reduce `image_uris` before comparing. Pinned to the bytes, so a size added in the middle of
    /// the table fails here rather than reordering every card object silently.
    #[test]
    fn image_uris_carries_scryfalls_eleven_sizes_in_scryfalls_order() {
        const EXPECTED: [(&str, &str); 11] = [
            ("small", "jpg"),
            ("normal", "jpg"),
            ("large", "jpg"),
            ("png", "png"),
            ("art_crop", "jpg"),
            ("border_crop", "jpg"),
            ("thumb", "webp"),
            ("grid", "webp"),
            ("display", "webp"),
            ("art", "webp"),
            ("crop", "webp"),
        ];
        let id = "cd000000-0000-0000-0000-000000000002";
        let expected = |face: &str, suffix: &str| {
            let body = EXPECTED
                .iter()
                .map(|(size, ext)| {
                    format!(r#""{size}":"https://cards.scryfall.io/{size}/{face}/c/d/{id}.{ext}{suffix}""#)
                })
                .collect::<Vec<_>>()
                .join(",");
            format!("{{{body}}}")
        };

        // Single-faced, with the cache-buster the row's image_updated_at supplies.
        let mut out = Vec::new();
        write_image_uris(&mut out, id, Some(1_783_903_008), "front");
        assert_eq!(String::from_utf8(out).expect("utf-8"), expected("front", "?1783903008"));

        // Per-face, back half, no cache-buster — the same eleven keys either way.
        let mut out = Vec::new();
        write_image_uris(&mut out, id, None, "back");
        assert_eq!(String::from_utf8(out).expect("utf-8"), expected("back", ""));
    }

    /// The joined top-level cost skips a face that has none, which is how a flip card reads.
    #[test]
    fn a_flipped_back_face_does_not_leave_a_dangling_separator() {
        let card = build(json!({
            "name": "Erayo, Soratami Ascendant // Erayo's Essence",
            "scryfall_id": "cd000000-0000-0000-0000-000000000003",
            "layout": "flip",
            "card_faces": [
                {"name": "Erayo, Soratami Ascendant", "mana_cost": "{1}{U}"},
                {"name": "Erayo's Essence", "mana_cost": ""},
            ],
        }));
        assert_eq!(card["mana_cost"], "{1}{U}");
        assert_eq!(card["card_faces"][1]["mana_cost"], "", "the face still reports its empty cost");
    }

    /// EDHREC files most multi-face cards under the FRONT face and split-likes under both halves.
    /// The tcgplayer searches beside it keep the joined name on every layout.
    #[test]
    fn edhrec_uses_the_front_face_name_except_on_the_split_like_layouts() {
        let front = |layout: &str| {
            let card = build(json!({
                "name": "Delver of Secrets // Insectile Aberration",
                "scryfall_id": "cd000000-0000-0000-0000-000000000004",
                "layout": layout,
                "card_faces": [{"name": "Delver of Secrets"}, {"name": "Insectile Aberration"}],
            }));
            card["related_uris"]["edhrec"].as_str().unwrap().to_owned()
        };
        for layout in ["transform", "modal_dfc", "flip", "adventure", "prepare", "art_series"] {
            assert_eq!(front(layout), "https://edhrec.com/route/?cc=Delver+of+Secrets", "{layout}");
        }
        for layout in ["split", "reversible_card", "double_faced_token"] {
            assert_eq!(
                front(layout),
                "https://edhrec.com/route/?cc=Delver+of+Secrets+%2F%2F+Insectile+Aberration",
                "{layout}"
            );
        }
        // The joined name on both tcgplayer searches, split included.
        let card = build(json!({
            "name": "Fire // Ice", "scryfall_id": "cd000000-0000-0000-0000-000000000005",
            "layout": "split", "card_faces": [{"name": "Fire"}, {"name": "Ice"}],
        }));
        assert_eq!(
            card["related_uris"]["tcgplayer_infinite_decks"],
            "https://www.tcgplayer.com/search/decks?productLineName=magic&q=Fire+%2F%2F+Ice"
        );
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

    /// The slug and quote_plus paths, which are where a reimplementation drifts. Every slug
    /// expectation here is a live production byte string (see the rule note on `slug`).
    #[test]
    fn slug_and_quote_plus_match_their_live_originals() {
        assert_eq!(slug("Lightning Bolt"), "lightning-bolt");
        assert_eq!(slug("Fire // Ice"), "fire-ice", "slashes are deleted, the space run is one hyphen");
        // Apostrophes are DELETED, not hyphenated: sok/35 serves
        // `erayo-soratami-ascendant-erayos-essence`.
        assert_eq!(
            slug("Erayo, Soratami Ascendant // Erayo's Essence"),
            "erayo-soratami-ascendant-erayos-essence"
        );
        // Non-ASCII output is UTF-8 percent-encoded: cmd/16 serves `j%C3%B6tun-grunt`.
        assert_eq!(slug("Jötun Grunt"), "j%C3%B6tun-grunt");
        assert_eq!(slug("Æther Vial"), "%C3%A6ther-vial");
        // Deleted set beyond the apostrophe: periods and straight/curly double quotes.
        assert_eq!(slug("S.H.I.E.L.D. Flying Car"), "shield-flying-car");
        assert_eq!(slug("Henzie \"Toolbox\" Torre"), "henzie-toolbox-torre");
        // Kept set: colon and bang survive (msc's Summon cards, acorn names), and literal hyphens
        // stack with space-hyphens rather than collapsing (dis/61's ru printed name keeps `---`).
        assert_eq!(slug("Summon: Choco/Mog"), "summon:-chocomog");
        assert_eq!(slug("Пламенник - военный разведчик"), "%D0%BF%D0%BB%D0%B0%D0%BC%D0%B5%D0%BD%D0%BD%D0%B8%D0%BA---%D0%B2%D0%BE%D0%B5%D0%BD%D0%BD%D1%8B%D0%B9-%D1%80%D0%B0%D0%B7%D0%B2%D0%B5%D0%B4%D1%87%D0%B8%D0%BA");
        // Nothing is trimmed: unfinity's "Humming-" ends in its hyphen on production.
        assert_eq!(slug("Humming-"), "humming-");

        assert_eq!(quote_plus("Lightning Bolt"), "Lightning+Bolt");
        assert_eq!(quote_plus("Æther Vial"), "%C3%86ther+Vial");
        assert_eq!(quote_plus("Fire // Ice"), "Fire+%2F%2F+Ice");
        // The safe set is the thing that has to match: `~` is left alone, `!*'()` are not.
        assert_eq!(quote_plus("a~b"), "a~b");
        assert_eq!(quote_plus("Yawgmoth's (Alt!)*"), "Yawgmoth%27s+%28Alt%21%29%2A");
    }

    /// A foreign printing takes the language segment in its scryfall_uri path, and only the ten
    /// print localizations get one — a `ph` printing stays on the English form. Pinned to live
    /// objects (grn/212/pt and one/414/ph, cached 2026-08-16).
    ///
    /// Scryfall also writes a `slug(printed name)-(slug(english name))` path where the printing
    /// HAS a printed name; that needs the printed name in the archive, which this branch does not
    /// store, so the English slug is the documented fallback until it does.
    #[test]
    fn a_foreign_printing_takes_the_language_path_segment() {
        let pt = build(json!({
            "name": "Unmoored Ego", "scryfall_id": "87130bc6-3a34-4855-9dd6-10607983bb29",
            "set_code": "grn", "collector_number": "212", "lang": "pt",
        }));
        assert_eq!(
            pt["scryfall_uri"],
            "https://scryfall.com/card/grn/212/pt/unmoored-ego?utm_source=api"
        );

        // A glyph language gets NO segment: ph Elesh Norn lives at the English form.
        let ph = build(json!({
            "name": "Elesh Norn, Mother of Machines", "scryfall_id": "87130bc6-3a34-4855-9dd6-10607983bb2a",
            "set_code": "one", "collector_number": "414", "lang": "ph",
        }));
        assert_eq!(
            ph["scryfall_uri"],
            "https://scryfall.com/card/one/414/elesh-norn-mother-of-machines?utm_source=api"
        );

        // ...and English itself is unsegmented.
        let en = build(json!({
            "name": "Unmoored Ego", "scryfall_id": "87130bc6-3a34-4855-9dd6-10607983bb2b",
            "set_code": "grn", "collector_number": "212", "lang": "en",
        }));
        assert_eq!(
            en["scryfall_uri"],
            "https://scryfall.com/card/grn/212/unmoored-ego?utm_source=api"
        );
    }

    #[test]
    fn gatherer_rides_the_first_multiverse_id() {
        let with_ids = build(json!({"name": "Jötun Grunt", "scryfall_id": "ab000000-0000-0000-0000-000000000007",
            "multiverse_ids": [247182, 999999]}));
        assert_eq!(
            with_ids["related_uris"]["gatherer"],
            "https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid=247182&printed=false"
        );

        let without = build(json!({"name": "x", "scryfall_id": "ab000000-0000-0000-0000-000000000007"}));
        assert!(without["related_uris"].get("gatherer").is_none());
    }

    /// `purchase_uris` always carries all three marketplaces: a product link where the printing
    /// has that id, a NAME SEARCH where it does not (verified live — the fallback is per KEY, not
    /// per card: khm English printings with tcgplayer+cardmarket ids and no mtgo id get two
    /// product links and a cardhoarder search). A zero id is not an id.
    #[test]
    fn purchase_uris_fall_back_to_a_name_search_per_missing_id() {
        let search = json!({
            "tcgplayer": "https://www.tcgplayer.com/search/magic/product?productLineName=magic&q=Jötun+Grunt&view=grid",
            "cardmarket": "https://www.cardmarket.com/en/Magic/Products/Search?searchString=Jötun+Grunt",
            "cardhoarder": "https://www.cardhoarder.com/cards?data%5Bsearch%5D=Jötun+Grunt",
        });
        // quote_plus percent-encodes the umlaut; the literals above are compared after that.
        let expect_search = json!({
            "tcgplayer": search["tcgplayer"].as_str().unwrap().replace('ö', "%C3%B6"),
            "cardmarket": search["cardmarket"].as_str().unwrap().replace('ö', "%C3%B6"),
            "cardhoarder": search["cardhoarder"].as_str().unwrap().replace('ö', "%C3%B6"),
        });
        let none = build(json!({"name": "Jötun Grunt", "scryfall_id": "01000000-0000-0000-0000-000000000004"}));
        assert_eq!(none["purchase_uris"], expect_search);

        let zero = build(json!({"name": "Jötun Grunt", "scryfall_id": "01000000-0000-0000-0000-000000000004",
            "tcgplayer_id": 0, "mtgo_id": 0, "cardmarket_id": 0}));
        assert_eq!(zero["purchase_uris"], expect_search, "a zero id is not an id");

        let some = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000004",
            "tcgplayer_id": 42, "mtgo_id": 7}));
        assert_eq!(some["purchase_uris"]["tcgplayer"], "https://www.tcgplayer.com/product/42?page=1");
        assert_eq!(some["purchase_uris"]["cardhoarder"], "https://www.cardhoarder.com/cards/7");
        assert_eq!(
            some["purchase_uris"]["cardmarket"],
            "https://www.cardmarket.com/en/Magic/Products/Search?searchString=x",
            "the one missing id takes the search form, the two present ones do not"
        );
    }

    /// A marketplace search takes the SAME string `related_uris.edhrec` does: the FRONT face on a
    /// transforming card, the JOINED name on the three JOINED_SEARCH_LAYOUTS. `related_uris`'
    /// tcgplayer_infinite_* links are the exception and keep the joined name on every layout.
    ///
    /// The front half is verified live on mom/230/es. The joined half on api.scryfall.com
    /// 2026-08-31, over `unique=prints`, on printings whose marketplace ids are missing so the
    /// SEARCH form is the one emitted: `Snake // Zombie` (cc2/9, double_faced_token) searches
    /// tcgplayer, cardmarket and cardhoarder for `Snake // Zombie`; `Bind // Liberate` (cmb2/88,
    /// split) and `Mechtitan // Mechtitan` (sld/1969, reversible_card) do the same with theirs,
    /// where `Champions of Archery // Join the Group` (ph19/4, adventure) and
    /// `Curse of the Fire Penguin // …` (unh/73, flip) search for their front faces.
    ///
    /// The fixtures carry `card_faces` AND a `layout` on purpose: `search_name` reads a row with
    /// no faces as single-faced and hands back the whole `name`, so a faceless fixture would pass
    /// the joined assertions while testing nothing.
    #[test]
    fn purchase_uris_search_splits_by_layout_the_way_edhrec_does() {
        // A FRONT-FACE layout. This is the assertion that used to hold for every layout, because
        // `write_purchase_uris` cut the front face off whatever it was handed.
        let transform = build(json!({"name": "Invasion of Alara // Awaken the Maelstrom",
            "scryfall_id": "01000000-0000-0000-0000-000000000004", "layout": "transform",
            "card_faces": [{"name": "Invasion of Alara"}, {"name": "Awaken the Maelstrom"}]}));
        assert_eq!(
            transform["purchase_uris"]["tcgplayer"],
            "https://www.tcgplayer.com/search/magic/product?productLineName=magic&q=Invasion+of+Alara&view=grid"
        );
        assert_eq!(
            transform["related_uris"]["tcgplayer_infinite_articles"],
            "https://www.tcgplayer.com/search/articles?productLineName=magic&q=Invasion+of+Alara+%2F%2F+Awaken+the+Maelstrom"
        );

        // A JOINED-SEARCH layout: all three marketplaces spell the whole name, and so does edhrec,
        // while tcgplayer_infinite_* spells it too — for the other reason, on every layout.
        let token = build(json!({"name": "Snake // Zombie",
            "scryfall_id": "01000000-0000-0000-0000-000000000005", "layout": "double_faced_token",
            "card_faces": [{"name": "Snake"}, {"name": "Zombie"}]}));
        let uris = &token["purchase_uris"];
        assert_eq!(
            uris["tcgplayer"],
            "https://www.tcgplayer.com/search/magic/product?productLineName=magic&q=Snake+%2F%2F+Zombie&view=grid"
        );
        assert_eq!(uris["cardmarket"], "https://www.cardmarket.com/en/Magic/Products/Search?searchString=Snake+%2F%2F+Zombie");
        assert_eq!(uris["cardhoarder"], "https://www.cardhoarder.com/cards?data%5Bsearch%5D=Snake+%2F%2F+Zombie");
        assert_eq!(token["related_uris"]["edhrec"], "https://edhrec.com/route/?cc=Snake+%2F%2F+Zombie");

        // The five-part split whose collection identifier this branch's name work made resolvable:
        // und/75's cardhoarder link searched for `Who`, where Scryfall searches the whole string.
        let five = build(json!({"name": "Who // What // When // Where // Why",
            "scryfall_id": "01000000-0000-0000-0000-000000000006", "layout": "split",
            "card_faces": [{"name": "Who"}, {"name": "What"}, {"name": "When"}, {"name": "Where"}, {"name": "Why"}]}));
        assert_eq!(
            five["purchase_uris"]["cardhoarder"],
            "https://www.cardhoarder.com/cards?data%5Bsearch%5D=Who+%2F%2F+What+%2F%2F+When+%2F%2F+Where+%2F%2F+Why"
        );

        // A single-faced card is unaffected either way, which is the overwhelming majority.
        let plain = build(json!({"name": "Lightning Bolt", "scryfall_id": "01000000-0000-0000-0000-000000000007"}));
        assert_eq!(
            plain["purchase_uris"]["cardhoarder"],
            "https://www.cardhoarder.com/cards?data%5Bsearch%5D=Lightning+Bolt"
        );
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

    /// Every `"key":` at nesting depth one of a JSON object's text, in order.
    fn top_level_keys(text: &str) -> Vec<String> {
        let (mut depth, mut keys, mut in_str, mut esc, mut start) = (0, Vec::new(), false, false, 0);
        for (i, ch) in text.char_indices() {
            if in_str {
                if esc {
                    esc = false;
                } else if ch == '\\' {
                    esc = true;
                } else if ch == '"' {
                    in_str = false;
                    if depth == 1 && text[i + 1..].starts_with(':') {
                        keys.push(text[start + 1..i].to_owned());
                    }
                }
                continue;
            }
            match ch {
                '"' => {
                    in_str = true;
                    start = i;
                }
                '{' | '[' => depth += 1,
                '}' | ']' => depth -= 1,
                _ => {}
            }
        }
        keys
    }

    fn write(row: serde_json::Value) -> String {
        let serde_json::Value::Object(map) = row else { panic!("row must be an object") };
        let mut out = Vec::new();
        write_scryfall_card(&mut out, &map, "https://api.scryfall.com");
        String::from_utf8(out).expect("utf-8")
    }

    /// The written bytes are key-ORDERED, which a `Value` round trip cannot show: parsing sorts
    /// them. Asserted against the encoded text, since that is what a client receives.
    ///
    /// The whole top-level sequence of Lightning Bolt msc/806 as api.scryfall.com served it on
    /// 2026-09-26, `resource_id` and `artist_ids` included. The row is shaped like the engine's --
    /// a serde map, so ALPHABETICAL -- and a key out of place anywhere fails here.
    #[test]
    fn a_single_faced_card_carries_scryfalls_keys_in_scryfalls_order() {
        let mut row = json!({
            "name": "Lightning Bolt", "scryfall_id": "7673784e-db4b-43a1-8d55-1bb9fc1e284f",
            "oracle_id": "4457ed35-7c10-48c8-9776-456485fdf070", "multiverse_ids": [],
            "mtgo_id": 1, "arena_id": 2, "tcgplayer_id": 3, "cardmarket_id": 4,
            "lang": "en", "released_at": "2026-06-26", "layout": "normal", "highres_image": true,
            "image_status": "highres_scan", "image_updated_at": 1_783_903_008, "mana_cost": "{R}",
            "cmc": 1.0, "type_line": "Instant", "oracle_text": "Lightning Bolt deals 3 damage to any target.",
            "colors": ["R"], "color_identity": ["R"], "card_keywords": [],
            "all_parts": [{"object": "related_card", "id": "7673784e-db4b-43a1-8d55-1bb9fc1e284f",
                "component": "combo_piece", "name": "Lightning Bolt", "type_line": "Instant"}],
            "legalities": {"vintage": "legal", "standard": "not_legal", "modern": "legal"},
            "games": ["paper"], "card_is_tags": ["gamechanger"], "foil": true, "nonfoil": true,
            "finishes": ["nonfoil", "foil"], "set_id": "11111111-0000-0000-0000-000000000001",
            "set_code": "msc", "set_name": "Marvel Super Heroes Commander", "set_type": "commander",
            "collector_number": "806", "rarity": "common", "flavor_text": "Zap.",
            "artist": "Milivoj Ćeran", "illustration_id": "22222222-0000-0000-0000-000000000002",
            "border_color": "black", "frame": "2015", "promo_types": ["universesbeyond"], "edhrec_rank": 157,
        });
        // Two more than `json!`'s recursion limit takes in one literal.
        row["resource_id"] = json!("A59396A4D646C69A1DD41F9906BE9A9CDECE83F18DC5C53501DD7BAE50511DBB");
        row["artist_ids"] = json!(["1eced451-4da5-42bc-b49d-70c41246581f"]);
        let text = write(row);
        assert_eq!(
            top_level_keys(&text).join(","),
            "object,id,oracle_id,multiverse_ids,resource_id,mtgo_id,arena_id,tcgplayer_id,cardmarket_id,\
             name,lang,released_at,uri,scryfall_uri,layout,highres_image,image_status,image_updated_at,\
             image_uris,mana_cost,cmc,type_line,oracle_text,colors,color_identity,keywords,all_parts,\
             legalities,games,reserved,game_changer,foil,nonfoil,finishes,oversized,promo,reprint,variation,\
             set_id,set,set_name,set_type,set_uri,set_search_uri,scryfall_set_uri,rulings_uri,\
             prints_search_uri,collector_number,digital,rarity,flavor_text,card_back_id,artist,artist_ids,\
             illustration_id,border_color,frame,full_art,textless,booster,story_spotlight,promo_types,\
             edhrec_rank,prices,related_uris,purchase_uris"
        );
        // `legalities` in Scryfall's format order, never the engine map's alphabetical one.
        assert!(text.contains(r#""legalities":{"standard":"not_legal","modern":"legal","vintage":"legal"}"#), "{text}");
    }

    /// The keys that sit between the ones above on other printings, each in the one position
    /// Scryfall gives it (msc/806 carries none of them): the rest of the ids before `name`, the
    /// creature stats after `oracle_text`, `watermark` between `rarity` and `flavor_text`, and
    /// `frame_effects`/`security_stamp` after `frame`, `penny_rank` after `edhrec_rank`.
    #[test]
    fn the_conditional_keys_take_scryfalls_positions() {
        let text = write(json!({
            "name": "Llanowar Elves", "scryfall_id": "01000000-0000-0000-0000-000000000006",
            "multiverse_ids": [1], "mtgo_id": 1, "mtgo_foil_id": 2, "arena_id": 3, "tcgplayer_id": 4,
            "tcgplayer_etched_id": 5, "cardmarket_id": 6, "oracle_text": "{T}: Add {G}.", "power": "1",
            "toughness": "1", "rarity": "common", "watermark": "set", "flavor_text": "x", "frame": "2015",
            "frame_effects": ["legendary"], "security_stamp": "oval", "edhrec_rank": 7, "penny_rank": 8,
        }));
        let keys = top_level_keys(&text);
        let at = |k: &str| keys.iter().position(|x| x == k).unwrap_or_else(|| panic!("{k} missing"));
        for pair in [
            ["multiverse_ids", "mtgo_id"],
            ["mtgo_id", "mtgo_foil_id"],
            ["mtgo_foil_id", "arena_id"],
            ["arena_id", "tcgplayer_id"],
            ["tcgplayer_id", "tcgplayer_etched_id"],
            ["tcgplayer_etched_id", "cardmarket_id"],
            ["cardmarket_id", "name"],
            ["oracle_text", "power"],
            ["power", "toughness"],
            ["toughness", "colors"],
            ["rarity", "watermark"],
            ["watermark", "flavor_text"],
            ["flavor_text", "card_back_id"],
            ["frame", "frame_effects"],
            ["frame_effects", "security_stamp"],
            ["security_stamp", "full_art"],
            ["edhrec_rank", "penny_rank"],
            ["penny_rank", "prices"],
        ] {
            assert_eq!(at(pair[1]), at(pair[0]) + 1, "{} must directly follow {}: {keys:?}", pair[1], pair[0]);
        }
    }

    /// A face's keys in Scryfall's order — never the row map's alphabetical one. The sequence is
    /// Delver of Secrets inr/60's front face as api.scryfall.com served it (2026-09-26), its
    /// `artist_id` between `artist` and `illustration_id`.
    #[test]
    fn faces_carry_scryfalls_key_order() {
        let text = write(json!({
            "name": "Delver of Secrets // Insectile Aberration",
            "scryfall_id": "6904ea20-e504-47da-95a0-08739fdde260", "layout": "transform",
            "card_faces": [
                {"artist": "Nils Hamm", "artist_id": "c540d1fc-1500-457f-93cf-d6069ee66546",
                 "colors": ["U"], "color_indicator": [],
                 "illustration_id": "1c2fee9b-89ea-4ab1-a751-451c3cd65a88", "mana_cost": "{U}",
                 "name": "Delver of Secrets", "oracle_text": "Upkeep.", "power": "1", "toughness": "1",
                 "type_line": "Creature — Human Wizard"},
                {"name": "Insectile Aberration", "mana_cost": ""},
            ],
        }));
        let face = &text[text.find(r#""card_faces":[{"#).expect("faces")..];
        assert!(
            face.starts_with(concat!(
                r#""card_faces":[{"object":"card_face","name":"Delver of Secrets","mana_cost":"{U}","#,
                r#""type_line":"Creature — Human Wizard","oracle_text":"Upkeep.","colors":["U"],"power":"1","#,
                r#""toughness":"1","artist":"Nils Hamm","artist_id":"c540d1fc-1500-457f-93cf-d6069ee66546","#,
                r#""illustration_id":"1c2fee9b-89ea-4ab1-a751-451c3cd65a88","#,
                r#""image_uris":{"#,
            )),
            "{}",
            &face[..300.min(face.len())]
        );
    }

    /// A reversible face leads with the card's `oracle_id` and carries its `cmc` right after
    /// `mana_cost` — Zndrsplt sld/379's faces on api.scryfall.com. They used to sit together
    /// straight after `object`.
    #[test]
    fn a_reversible_face_carries_cmc_after_its_mana_cost() {
        let text = write(json!({
            "name": "Temple Garden // Temple Garden", "scryfall_id": "d5dfd236-b1da-4552-b94f-ebf6bb9dafdf",
            "oracle_id": "0f7f1148-7b1c-4aeb-9a40-ab11b4ae0ad8", "layout": "reversible_card", "cmc": 0.0,
            "card_faces": [{"layout": "normal", "name": "Temple Garden", "mana_cost": "", "type_line": "Land"}],
        }));
        assert!(
            text.contains(concat!(
                r#"{"object":"card_face","oracle_id":"0f7f1148-7b1c-4aeb-9a40-ab11b4ae0ad8","layout":"normal","#,
                r#""name":"Temple Garden","mana_cost":"","cmc":0.0,"type_line":"Land","#,
            )),
            "{text}"
        );
    }

    /// Each related card in Scryfall's key order, closed by its own `uri` on this host — derived
    /// like the top-level one, and never the stored copy (which names api.scryfall.com).
    #[test]
    fn a_related_card_closes_with_its_derived_uri() {
        let card = json!({
            "name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000009",
            "all_parts": [{"type_line": "Token Creature — Goblin", "name": "Goblin", "id": "a0000000-0000-0000-0000-000000000001",
                "component": "token", "object": "related_card", "uri": "https://api.scryfall.com/cards/a0000000-0000-0000-0000-000000000001"}],
        });
        let serde_json::Value::Object(map) = card else { panic!() };
        let mut out = Vec::new();
        write_scryfall_card(&mut out, &map, "https://api.example/v1");
        let text = String::from_utf8(out).expect("utf-8");
        assert!(
            text.contains(concat!(
                r#""all_parts":[{"object":"related_card","id":"a0000000-0000-0000-0000-000000000001","component":"token","#,
                r#""name":"Goblin","type_line":"Token Creature — Goblin","uri":"https://api.example/v1/cards/a0000000-0000-0000-0000-000000000001"}]"#,
            )),
            "{text}"
        );
    }

    /// A format this writer's order does not know yet is kept, after the known ones.
    #[test]
    fn an_unknown_format_follows_scryfalls_known_ones() {
        let text = write(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000009",
            "legalities": {"aaa_new": "legal", "vintage": "legal", "standard": "legal"}}));
        assert!(text.contains(r#""legalities":{"standard":"legal","vintage":"legal","aaa_new":"legal"}"#), "{text}");
    }

    /// `game_changer` is an is-tag like `reserved`; `foil` and `nonfoil` are the stored flags.
    /// All three are on every object api.scryfall.com serves, false included.
    #[test]
    fn game_changer_foil_and_nonfoil_are_always_written() {
        let plain = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000005"}));
        assert_eq!((&plain["game_changer"], &plain["foil"], &plain["nonfoil"]), (&json!(false), &json!(false), &json!(false)));
        let set = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000005",
            "card_is_tags": ["gamechanger"], "foil": true, "nonfoil": true}));
        assert_eq!((&set["game_changer"], &set["foil"], &set["nonfoil"]), (&json!(true), &json!(true), &json!(true)));
        assert_eq!(set["reserved"], false, "one tag does not answer for the other");
    }

    /// The stored epoch renders as Scryfall's ISO-8601 string, and is absent when none is stored.
    #[test]
    fn image_updated_at_renders_iso8601_utc() {
        assert_eq!(iso8601_utc(1_783_903_008), "2026-07-13T00:36:48Z");
        assert_eq!(iso8601_utc(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(iso8601_utc(1_704_067_199), "2023-12-31T23:59:59Z");
        assert_eq!(iso8601_utc(0), "1970-01-01T00:00:00Z");
        let card = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000005",
            "image_updated_at": 1_783_903_008}));
        assert_eq!(card["image_updated_at"], "2026-07-13T00:36:48Z");
        let none = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000005"}));
        assert!(none.get("image_updated_at").is_none());
    }

    /// The collector number is percent-encoded in `scryfall_uri` like the slug: oarc/1★ and
    /// arn/2† on api.scryfall.com (2026-09-26).
    #[test]
    fn the_collector_number_is_percent_encoded() {
        let star = build(json!({"name": "All in Good Time", "scryfall_id": "17b941e9-5dcc-473e-a461-709d74e32a3c",
            "set_code": "oarc", "collector_number": "1★"}));
        assert_eq!(star["scryfall_uri"], "https://scryfall.com/card/oarc/1%E2%98%85/all-in-good-time?utm_source=api");
        let dagger = build(json!({"name": "Army of Allah", "scryfall_id": "9899352a-a4c9-47bf-b9cb-0c34060ae1c4",
            "set_code": "arn", "collector_number": "2†"}));
        assert_eq!(dagger["scryfall_uri"], "https://scryfall.com/card/arn/2%E2%80%A0/army-of-allah?utm_source=api");
        // The collector number keeps what the slug keeps literally.
        let plain = build(json!({"name": "x", "scryfall_id": "17b941e9-5dcc-473e-a461-709d74e32a3c",
            "set_code": "unf", "collector_number": "200a"}));
        assert_eq!(plain["scryfall_uri"], "https://scryfall.com/card/unf/200a/x?utm_source=api");
    }

    /// The glyph languages link Gatherer's untranslated page, as English does: every live `ph` and
    /// `qya` printing says `printed=false`.
    #[test]
    fn glyph_languages_link_gatherer_untranslated() {
        for (lang, printed) in [("ph", "false"), ("qya", "false"), ("en", "false"), ("ja", "true"), ("de", "true")] {
            let card = build(json!({"name": "x", "scryfall_id": "09705595-47c6-4f7c-9351-4004bfa39218",
                "lang": lang, "multiverse_ids": [604957]}));
            assert_eq!(
                card["related_uris"]["gatherer"],
                format!("https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid=604957&printed={printed}"),
                "{lang}"
            );
        }
    }

    /// A `content_warning` withdraws every marketplace link but gatherer — leg/62, Invoke
    /// Prejudice, live 2026-09-26: no tcgplayer or edhrec entry and no `purchase_uris`.
    #[test]
    fn a_content_warning_withdraws_the_marketplace_links() {
        let warned = build(json!({"name": "Invoke Prejudice", "scryfall_id": "903d9fde-d7da-4a0e-a337-b63023c6d74b",
            "multiverse_ids": [485302], "games": ["paper"], "content_warning": true}));
        assert_eq!(
            warned["related_uris"],
            json!({"gatherer": "https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid=485302&printed=false"})
        );
        assert!(warned.get("purchase_uris").is_none());
        let plain = build(json!({"name": "Invoke Prejudice", "scryfall_id": "903d9fde-d7da-4a0e-a337-b63023c6d74b",
            "multiverse_ids": [485302], "games": ["paper"], "content_warning": false}));
        assert!(plain["related_uris"].get("edhrec").is_some());
        assert!(plain.get("purchase_uris").is_some());
    }

    /// The residue keys, each verbatim in the one position Scryfall gives it: Balloon Stand
    /// unf/200a's `attraction_lights` just ahead of its own `card_back_id`, then `artist_ids`,
    /// `preview` (in Scryfall's key order, whatever order the row holds it in) and
    /// `content_warning` last before `prices`, `variation_of` straight after `variation`. A row
    /// carrying none of them -- `null`, as the engine answers -- writes none, and the shared back.
    #[test]
    fn the_residue_keys_take_scryfalls_positions_verbatim() {
        let text = write(json!({
            "name": "Balloon Stand", "scryfall_id": "01000000-0000-0000-0000-000000000010",
            "multiverse_ids": [1], "resource_id": "AB12", "tcgplayer_id": 2, "variation": true,
            "variation_of": "3d170015-b125-49a6-a15e-8fd116bbcb14", "rarity": "common", "flavor_text": "x",
            "attraction_lights": [2, 6], "card_back_id": "7840c131-f96b-4700-9347-2215c43156e6",
            "artist": "Someone", "artist_ids": ["ffffffff-0000-0000-0000-000000000001", "ffffffff-0000-0000-0000-000000000002"],
            "edhrec_rank": 5,
            // Alphabetical, as the engine's map hands it over; Scryfall's order is source first.
            "preview": {"previewed_at": "2022-10-04", "source": "Wizards of the Coast", "source_uri": ""},
            "content_warning": false,
        }));
        let keys = top_level_keys(&text);
        let at = |k: &str| keys.iter().position(|x| x == k).unwrap_or_else(|| panic!("{k} missing"));
        for pair in [
            ["multiverse_ids", "resource_id"],
            ["resource_id", "tcgplayer_id"],
            ["variation", "variation_of"],
            ["variation_of", "set_id"],
            ["flavor_text", "attraction_lights"],
            ["attraction_lights", "card_back_id"],
            ["artist", "artist_ids"],
            ["artist_ids", "illustration_id"],
            ["edhrec_rank", "preview"],
            ["preview", "content_warning"],
            ["content_warning", "prices"],
        ] {
            assert_eq!(at(pair[1]), at(pair[0]) + 1, "{} must directly follow {}: {keys:?}", pair[1], pair[0]);
        }
        assert!(text.contains(r#""attraction_lights":[2,6],"card_back_id":"7840c131-f96b-4700-9347-2215c43156e6""#), "{text}");
        assert!(
            text.contains(r#""preview":{"source":"Wizards of the Coast","source_uri":"","previewed_at":"2022-10-04"}"#),
            "{text}"
        );
        assert!(text.contains(r#""content_warning":false"#), "a false warning is still Scryfall's key: {text}");

        let bare = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000011",
            "resource_id": null, "variation_of": null, "attraction_lights": null, "card_back_id": null,
            "preview": null, "content_warning": null, "artist_ids": null}));
        for absent in ["resource_id", "variation_of", "attraction_lights", "preview", "content_warning", "artist_ids"] {
            assert!(bare.get(absent).is_none(), "{absent} is omitted when the row does not carry it");
        }
        assert_eq!(bare["card_back_id"], CARD_BACK_ID, "no back of its own is the shared back");
        let empty = build(json!({"name": "x", "scryfall_id": "01000000-0000-0000-0000-000000000011", "artist_ids": []}));
        assert_eq!(empty["artist_ids"], json!([]), "an empty list is a value, not an absence");
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

    /// The fixture's text with every whitespace byte outside a string dropped: the compact form
    /// both writers emit, in the FIXTURE's key order — which parsing loses here, because a
    /// `serde_json::Map` is a BTreeMap.
    fn minified(text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        let (mut in_str, mut esc) = (false, false);
        for ch in text.chars() {
            if in_str {
                out.push(ch);
                if esc {
                    esc = false;
                } else if ch == '\\' {
                    esc = true;
                } else if ch == '"' {
                    in_str = false;
                }
            } else if ch == '"' {
                in_str = true;
                out.push(ch);
            } else if !ch.is_whitespace() {
                out.push(ch);
            }
        }
        out
    }

    /// Each `"expected":` object of the minified fixture, in case order.
    fn expected_objects(min: &str) -> Vec<&str> {
        const KEY: &str = r#""expected":"#;
        let mut out = Vec::new();
        let mut rest = min;
        while let Some(at) = rest.find(KEY) {
            let body = &rest[at + KEY.len()..];
            let (mut depth, mut in_str, mut esc, mut end) = (0i32, false, false, 0);
            for (i, ch) in body.char_indices() {
                if in_str {
                    if esc {
                        esc = false;
                    } else if ch == '\\' {
                        esc = true;
                    } else if ch == '"' {
                        in_str = false;
                    }
                    continue;
                }
                match ch {
                    '"' => in_str = true,
                    '{' | '[' => depth += 1,
                    '}' | ']' => {
                        depth -= 1;
                        if depth == 0 {
                            end = i + 1;
                            break;
                        }
                    }
                    _ => {}
                }
            }
            out.push(&body[..end]);
            rest = &body[end..];
        }
        out
    }

    /// The SAME cases `api/scryfall_compat/objects.py` asserts, from the same file — to the BYTE.
    ///
    /// Two implementations build this object — this one for the engine path, `objects.py` for the
    /// SQL path — and both answer `/cards/*`, so a difference between them is one a client can
    /// see. Nothing else compares them: the Rust suite and the Python suite are separate CI jobs
    /// that never meet. A shared fixture is what makes each job fail on its own drift.
    ///
    /// Key ORDER included, at every level: the written bytes must equal the fixture's `expected`
    /// minified in the fixture's own order, which the Python side asserts too. Thirteen of the cases
    /// are api.scryfall.com's own objects (see the fixture's `_comment`), so the order both
    /// writers are held to is Scryfall's rather than either writer's.
    #[test]
    fn the_card_object_matches_the_python_builder_case_for_case() {
        const FIXTURE: &str =
            include_str!("../../api/scryfall_compat/fixtures/card_object_parity.json");
        let doc: serde_json::Value = serde_json::from_str(FIXTURE).expect("fixture must be JSON");
        let base_url = doc["base_url"].as_str().expect("base_url");
        let cases = doc["cases"].as_array().expect("cases must be an array");
        assert!(!cases.is_empty(), "fixture must carry cases");
        let min = minified(FIXTURE);
        let wires = expected_objects(&min);
        assert_eq!(wires.len(), cases.len(), "one `expected` per case");

        for (case, wire) in cases.iter().zip(wires) {
            let name = case["case"].as_str().unwrap_or("<unnamed>");
            let serde_json::Value::Object(row) = case["row"].clone() else {
                panic!("{name}: row must be an object")
            };
            let mut out = Vec::new();
            write_scryfall_card(&mut out, &row, case["base_url"].as_str().unwrap_or(base_url));
            let got: serde_json::Value =
                serde_json::from_slice(&out).expect("the writer must emit valid JSON");
            assert_eq!(got, case["expected"], "{name}: diverged from the Python builder");
            let text = String::from_utf8(out).expect("utf-8");
            if text != wire {
                let at = text.bytes().zip(wire.bytes()).take_while(|(a, b)| a == b).count();
                let window = |s: &str| s.get(at.saturating_sub(80)..(at + 80).min(s.len())).unwrap_or("").to_owned();
                panic!("{name}: same values, different bytes at {at}\n  fixture …{}…\n  written …{}…", window(wire), window(&text));
            }
        }
    }
}
