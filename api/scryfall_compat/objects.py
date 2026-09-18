"""Scryfall response objects: card reconstruction, envelopes, and the text rendering.

Everything here is pure — dicts in, dicts out — so the payload shape can be tested without a
database or a request. `routes.py` owns the HTTP and SQL sides.

The one subtle piece is `to_scryfall_card`. `cards.raw_card_blob` holds the card object Scryfall
sent, but not quite untouched: `preprocess_card` adds three internal keys to it and normalizes an
absent `flavor_text` to `""`. Both are exactly reversible, and reversing them is the whole of the
function.

There is no column holding a pristine copy alongside it, and there deliberately isn't one: the blob
being answerable is a property of the importer, maintained there rather than worked around here.
The one case where the blob is *not* the card — a multi-face row written before the merged-row work
— is handled as a fallback rather than as a stored duplicate, because it is a fixed window that one
import closes.
"""

from __future__ import annotations

import datetime
import re
import urllib.parse
import uuid
from typing import Any

# Keys `preprocess_card` adds to the object it snapshots into raw_card_blob. Stripping them, and
# undoing the flavor_text normalization below, inverts the snapshot. A multi-face row carries only
# `card_name`; a single-face one carries all three.
_IMPORTER_ADDED_KEYS = ("card_name", "face_name", "face_idx")

# Sizes Scryfall serves under `image_uris`, and the `version` vocabulary of the image format.
IMAGE_VERSIONS = ("small", "normal", "large", "png", "art_crop", "border_crop")
DEFAULT_IMAGE_VERSION = "large"

# Scryfall pages every card list at 175, and clients page by following `next_page` rather than by
# computing offsets, so this has to match or a client's page count silently disagrees with ours.
PAGE_SIZE = 175

# Scryfall caps a collection POST at 75 identifiers and 422s past it.
MAX_COLLECTION_IDENTIFIERS = 75

# Scryfall caps an autocomplete catalog at 20 names.
MAX_AUTOCOMPLETE_VALUES = 20


# Every field the engine must return for a card object to be assembled. Passed as `fields=` on each
# lookup, so the engine emits exactly this and nothing is fetched that is never read.
CARD_OBJECT_FIELDS = (
    "name", "scryfall_id", "oracle_id", "layout", "mana_cost", "cmc", "type_line", "oracle_text",
    "printed_name", "printed_type_line", "printed_text", "flavor_name",
    "power", "toughness", "loyalty", "colors", "color_identity", "card_keywords", "set_code", "set_name",
    "collector_number", "rarity", "flavor_text", "artist", "illustration_id", "released_at",
    "legalities", "edhrec_rank", "price_usd", "price_eur", "price_tix", "watermark",
    "card_frame_data", "card_is_tags", "border_color", "frame",
    "lang", "image_status", "set_type", "security_stamp", "set_id", "arena_id", "mtgo_id",
    "mtgo_foil_id", "tcgplayer_id", "tcgplayer_etched_id", "cardmarket_id", "penny_rank",
    "image_updated_at", "price_usd_foil", "price_usd_etched", "price_eur_foil", "multiverse_ids",
    "promo_types", "frame_effects", "games", "finishes", "booster", "digital", "foil", "nonfoil",
    "full_art", "highres_image", "oversized", "promo", "reprint", "story_spotlight", "textless",
    "variation", "card_faces", "all_parts", "produced_mana", "color_indicator",
)  # fmt: skip

# Scryfall's card back, one image for every normal card.
CARD_BACK_ID = "0aeebaf5-8c7d-4636-9e82-8c27447861f7"

# The layouts whose faces each get their OWN image -- and, with it, their own copy of every value
# the one-image layouts keep at the top level.
#
# This is the single fact the whole multi-face branch turns on, and it is a property of the LAYOUT,
# not of anything the row carries: a transform card's front and back are two photographs, so
# Scryfall puts image_uris, colors, power, illustration_id, flavor_text and the rest on the faces
# and sends NO top-level copy (and no card_back_id -- there is no shared back). A split or adventure
# card is ONE photograph of one piece of cardboard, so Scryfall sends one top-level image_uris and
# one top-level colors, and its faces carry only text.
#
# Verified exhaustively against the 2026-08-16 all_cards bulk: of 540,484 printings, every row of
# these five layouts has per-face image_uris and no top-level one, and every row of every other
# layout has the reverse -- zero exceptions in either direction.
_TWO_IMAGE_LAYOUTS = frozenset({"art_series", "double_faced_token", "modal_dfc", "reversible_card", "transform"})

# The multi-face layouts whose related_uris.edhrec link keeps the JOINED name.
#
# EDHREC files a transforming or adventuring card under its front face (cc=Delver+of+Secrets,
# cc=Brazen+Borrower, cc=Erayo%2C+Soratami+Ascendant, cc=Agadeem%27s+Awakening) and a split or
# double-backed card under both halves (cc=Fire+%2F%2F+Ice, cc=Wear+%2F%2F+Tear,
# cc=Temple+Garden+%2F%2F+Temple+Garden, cc=Punchcard+%2F%2F+Punchcard) -- all eight verified
# against api.scryfall.com. art_series sits with the front-face group, not with the other two-image
# layouts. The tcgplayer_infinite_* links in the same object keep the joined name on EVERY layout,
# split included, so this rule is deliberately scoped to edhrec alone.
_EDHREC_JOINED_LAYOUTS = frozenset({"double_faced_token", "reversible_card", "split"})

# Top-level keys a two-image layout does not carry, because they belong to a face there.
_FACE_OWNED_KEYS = frozenset(
    {
        "colors",
        "card_back_id",
        "illustration_id",
        "power",
        "toughness",
        "loyalty",
        "flavor_text",
        "watermark",
        "color_indicator",
    }
)

# The file extension each image size is served as.
_IMAGE_EXTENSIONS = {"small": "jpg", "normal": "jpg", "large": "jpg", "png": "png", "art_crop": "jpg", "border_crop": "jpg"}

# magic.cards column -> the engine's name for the same value. The columns predate the engine, and
# `to_scryfall_card` reads engine names.
_SQL_COLUMN_ALIASES = {
    "card_name": "name",
    "card_set_code": "set_code",
    "mana_cost_text": "mana_cost",
    "card_legalities": "legalities",
    "card_layout": "layout",
    "card_watermark": "watermark",
    "card_artist": "artist",
    "card_border": "border_color",
}

_RARITY_BY_INT = {0: "common", 1: "uncommon", 2: "rare", 3: "mythic", 4: "special", 5: "bonus"}


def sql_row_to_engine_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reshape a `magic.cards` row into the shape the engine emits.

    There is ONE card-object builder and both paths go through it; this is what lets the SQL
    fallback use it. Two builders would be two chances to disagree with Scryfall, and a fallback is
    where a different answer is least affordable — it runs when the engine is already in trouble.

    Building from `raw_card_blob` instead is not an option: for a multi-face row the blob is the
    FRONT FACE, not the card, so it would silently degrade exactly the cards the merge exists to fix.

    Args:
        row: A row selected with routes._CARD_COLUMNS.

    Returns:
        The same values under the engine's field names, with the compat residue flattened.
    """
    out: dict[str, Any] = {}
    for column, value in row.items():
        if column in ("card_compat_blob", "raw_card_blob", "card_colors", "card_color_identity"):
            continue
        # psycopg binds dates and uuids as objects; JSON carries neither, and the engine path
        # already emits strings. Normalizing here is what keeps ONE builder viable.
        if isinstance(value, datetime.date):
            normalized: Any = value.isoformat()
        elif isinstance(value, uuid.UUID):
            normalized = str(value)
        else:
            normalized = value
        out[_SQL_COLUMN_ALIASES.get(column, column)] = normalized

    # jsonb objects store these as {key: true} sets; the engine emits lists.
    for target, column in (("colors", "card_colors"), ("color_identity", "card_color_identity")):
        value = row.get(column)
        out[target] = sorted(value) if isinstance(value, dict) else []
    for key in ("card_keywords", "card_is_tags"):
        if isinstance(row.get(key), dict):
            out[key] = sorted(row[key])

    if row.get("card_rarity_int") is not None:
        out["rarity"] = _RARITY_BY_INT.get(row["card_rarity_int"])

    # The residue is one column here and individual fields on an engine row.
    out.update(row.get("card_compat_blob") or {})
    return out


def _image_uris(scryfall_id: str, updated_at: int | None, face: str = "front") -> dict[str, str]:
    """Build the CDN URLs for one face.

    Scryfall's paths are a pure function of the card id: its first two hex digits become directory
    levels, and `image_updated_at` rides as a cache-buster. Nothing about these is stored.
    """
    scryfall_id = str(scryfall_id or "")
    if not scryfall_id:
        return {}
    suffix = f"?{updated_at}" if updated_at else ""
    first, second = scryfall_id[0], scryfall_id[1]
    return {
        size: f"https://cards.scryfall.io/{size}/{face}/{first}/{second}/{scryfall_id}.{ext}{suffix}"
        for size, ext in _IMAGE_EXTENSIONS.items()
    }


# Characters Scryfall DELETES from a slug rather than hyphenating. Live-derived: "Erayo's
# Essence" slugs to `erayos-essence` (not `erayo-s-essence`), "S.H.I.E.L.D." to `shield`,
# `Henzie "Toolbox" Torre` to `henzie-toolbox-torre`, and the zhs printings of Kongming/Pang Tong
# pin the curly quotes. U+201E („) is NOT deleted — `Henzie „Der Beschaffer" Torre` (de) keeps it.
_SLUG_DELETED = frozenset("'\",./“”")

# Slug bytes served literally; every other byte is UTF-8 percent-encoded, uppercase hex. The
# literal set is exactly what appears un-encoded across the bulk corpus (`!&()+-:;=_`); `?` is the
# one ASCII special observed encoded. Unobserved characters encode, which can never break a URL.
# (A hand-rolled encoder rather than urllib.parse.quote: quote() can never encode `~`, and the
# byte-identical Rust and TypeScript twins need one shared safe set.)
_SLUG_LITERAL = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!&()+-:;=_")

# The languages Scryfall writes into the scryfall_uri path — its ten print localizations, exactly.
# The glyph and novelty languages (ph, qya, he, la, grc, ar, sa, dw) get NO path segment: a ph
# Elesh Norn lives at `/card/one/414/elesh-norn-mother-of-machines`, English form.
_SLUG_LANG_SEGMENTS = frozenset({"de", "es", "fr", "it", "ja", "ko", "pt", "ru", "zhs", "zht"})

# Languages whose printed name never reaches the slug even when stored: the Phyrexian and Quenya
# printings carry glyph-font printed_names ("|Ceghm.", U+E0xx runs) and production serves them the
# plain English slug. Every other non-English language — including he/grc/ar/sa, which lack the
# path segment — keeps the `printed-(english)` slug form.
_SLUG_PRINTED_IGNORED = frozenset({"en", "ph", "qya"})


def _slug(name: str) -> str:
    """Scryfall's URL slug for a card name.

    NOT the folklore "non-alphanumerics collapse to hyphens" rule this module first shipped — that
    rule hyphenates apostrophes (`erayo-s-essence`) and serves raw UTF-8 (`jötun-grunt`) where
    production Scryfall deletes the apostrophe and percent-encodes the bytes. The real rule,
    verified against the `scryfall_uri` of all 540,484 printings in the 2026-08-16 all_cards bulk
    (zero mismatches): lowercase; DELETE `' " , . /` and the curly quotes U+201C/U+201D; each run
    of ASCII spaces becomes one hyphen (literal hyphens pass through and may stack — the ru
    printed name "Пламенник - военный разведчик" keeps `---` — and nothing is trimmed: "Humming-"
    keeps its trailing hyphen); everything else survives verbatim and is then UTF-8
    percent-encoded per _SLUG_LITERAL.

    Args:
        name: The card (or printed) name.

    Returns:
        The percent-encoded slug.
    """
    cleaned = "".join(c for c in name.lower() if c not in _SLUG_DELETED)
    hyphenated = re.sub(" +", "-", cleaned)
    return "".join(ch if ch in _SLUG_LITERAL else "".join(f"%{b:02X}" for b in ch.encode()) for ch in hyphenated)


def _printed_full_name(row: dict[str, Any], lang: str) -> str | None:
    """The printing's printed full name, when the slug should use one.

    The top-level `printed_name`, or on a multi-face card the faces' `printed_name`s joined
    " // " — ONLY the faces that have one: the es printing of sos/113, whose second face has no
    printed_name, slugs as `em%C3%A9rita-del-conflicto-(emeritus-of-conflict-lightning-bolt)`
    (verified live).

    Args:
        row: The engine row.
        lang: The printing's language.

    Returns:
        The printed full name, or None for English and for the Phyrexian/Quenya glyph printings,
        whose stored printed_names production never slugs.
    """
    if lang in _SLUG_PRINTED_IGNORED:
        return None
    if row.get("printed_name"):
        return str(row["printed_name"])
    parts = [face["printed_name"] for face in row.get("card_faces") or [] if face.get("printed_name")]
    return " // ".join(parts) if parts else None


def _scryfall_uri(row: dict[str, Any], name: str, set_code: str, number: str, lang: str) -> str:
    """Build `scryfall_uri`: `https://scryfall.com/card/{set}/{number}[/{lang}]/{slug}?utm_source=api`.

    A foreign printing's slug is `slug(printed full name)-(slug(english full name))`, parentheses
    literal (grn/212/pt: `ego-%C3%A0-deriva-(unmoored-ego)`, verified live). A foreign printing
    with no printed name falls back to the plain English slug, keeping the language segment
    (ody/243/zhs -> `/zhs/holistic-wisdom`, verified live); one whose printed name slugs to
    nothing takes the same fallback (live-unpinned — no such printing exists in the corpus).

    Args:
        row: The engine row.
        name: The English full name.
        set_code: The set code.
        number: The collector number.
        lang: The printing's language.

    Returns:
        The absolute scryfall.com URL.
    """
    segment = f"{lang}/" if lang in _SLUG_LANG_SEGMENTS else ""
    english = _slug(name)
    printed_full = _printed_full_name(row, lang)
    printed = _slug(printed_full) if printed_full else ""
    path = f"{printed}-({english})" if printed else english
    return f"https://scryfall.com/card/{set_code}/{number}/{segment}{path}?utm_source=api"


def _related_uris(name: str, edhrec_name: str, multiverse_ids: list[int], lang: str) -> dict[str, str]:
    """Scryfall's `related_uris`, pointing at the destinations directly.

    Scryfall wraps the TCGplayer entries in `partner.tcgplayer.com/...?u=<encoded real URL>` with
    its own affiliate code. The destination is the same page, and emitting the wrapper from this
    host would route another service's affiliate revenue to Scryfall.

    `gatherer` LEADS the dict when the printing has multiverse ids, built from the FIRST id, with
    `printed=true` for every non-English printing and `printed=false` for English — verified
    against the bulk corpus at 540,430 of 540,484 printings. The 54 exceptions are foreign-only
    promos (dd2-ja, snc launch, one-ph, ltc-qya) whose Gatherer entries carry no translation; that
    fact lives on Scryfall's side of the wire and is not derivable from the row, so they stay a
    known limit rather than a rule.

    Args:
        name: The English card name.
        edhrec_name: The name the edhrec link searches for -- the FRONT FACE's on most multi-face
            layouts, the joined one on the split-likes (see _EDHREC_JOINED_LAYOUTS). The two
            tcgplayer searches take the joined name on every layout.
        multiverse_ids: The printing's multiverse ids, possibly empty.
        lang: The printing's language.

    Returns:
        The related_uris dict, gatherer first when present.
    """
    out: dict[str, str] = {}
    if multiverse_ids:
        printed = "false" if lang == "en" else "true"
        out["gatherer"] = f"https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid={multiverse_ids[0]}&printed={printed}"
    quoted = urllib.parse.quote_plus(name)
    out["tcgplayer_infinite_articles"] = f"https://www.tcgplayer.com/search/articles?productLineName=magic&q={quoted}"
    out["tcgplayer_infinite_decks"] = f"https://www.tcgplayer.com/search/decks?productLineName=magic&q={quoted}"
    out["edhrec"] = f"https://edhrec.com/route/?cc={urllib.parse.quote_plus(edhrec_name)}"
    return out


def _sold_somewhere(row: dict[str, Any]) -> bool:
    """Whether some marketplace sells this printing — the condition `purchase_uris` is emitted under.

    A printing no marketplace sells omits the key rather than carrying three dead links, and the
    rule is the marketplaces rather than `digital` — measured on api.scryfall.com 2026-08-16:
    prm/80925 (games ["mtgo"], digital true) HAS purchase_uris; ymid/59 and khm/A-198
    (games ["arena"], digital true) do not. tcgplayer and cardmarket sell cardboard, cardhoarder
    sells MTGO, and nothing sells Arena.

    An absent or empty `games` emits: the omission is a positive statement about the printing, and
    a row that never carried the column has made no such statement.

    Args:
        row: The engine row.

    Returns:
        True when the key should be emitted.
    """
    games = row.get("games") or []
    return not games or bool({"paper", "mtgo"} & set(games))


def _purchase_uris(row: dict[str, Any]) -> dict[str, str]:
    """Scryfall's `purchase_uris`, rebuilt from the marketplace ids. Same affiliate reasoning."""
    out: dict[str, str] = {}
    if row.get("tcgplayer_id"):
        out["tcgplayer"] = f"https://www.tcgplayer.com/product/{row['tcgplayer_id']}?page=1"
    if row.get("cardmarket_id"):
        out["cardmarket"] = f"https://www.cardmarket.com/en/Magic/Products?idProduct={row['cardmarket_id']}"
    if row.get("mtgo_id"):
        out["cardhoarder"] = f"https://www.cardhoarder.com/cards/{row['mtgo_id']}"
    return out


def _prices(row: dict[str, Any]) -> dict[str, Any]:
    """Scryfall's `prices` object: the three price columns plus the three residue variants."""

    def fmt(value: float | None) -> str | None:
        return None if value is None else f"{float(value):.2f}"

    return {
        "usd": fmt(row.get("price_usd")),
        "usd_foil": fmt(row.get("price_usd_foil")),
        "usd_etched": fmt(row.get("price_usd_etched")),
        "eur": fmt(row.get("price_eur")),
        "eur_foil": fmt(row.get("price_eur_foil")),
        "tix": fmt(row.get("price_tix")),
    }


def _joined_mana_cost(faces: list[dict[str, Any]]) -> str:
    """The joined top-level `mana_cost` a one-image multi-face card carries.

    Scryfall's rule, checked against all 3,654 split/flip/adventure/prepare printings in the
    2026-08-16 bulk with zero misses: `" // "` between the faces that HAVE a cost, skipping the ones
    that do not. Fire // Ice is `"{1}{R} // {1}{U}"`; flipped Erayo, whose back face carries
    `"mana_cost": ""`, is `"{1}{U}"` and not `"{1}{U} // "`.

    Derived rather than stored because the ingest cannot preserve it: card_processing overlays each
    face onto the parent card, so the stored top-level cost is the FRONT face's alone.

    Args:
        faces: The stored face objects, front first.

    Returns:
        The joined cost, or "" when no face carries one.
    """
    return " // ".join(face["mana_cost"] for face in faces if face.get("mana_cost"))


def _faces(row: dict[str, Any], two_image: bool) -> list[dict[str, Any]]:
    """The card's faces, with the two keys the engine deliberately does not store re-added.

    `object` is the constant "card_face", and a face's `image_uris` is the card's CDN function with
    front/back swapped -- on the two-image layouts, which are the only ones whose faces have their
    own picture.

    Args:
        row: The engine row.
        two_image: Whether this printing's layout gives each face its own image.

    Returns:
        The face objects, front first.
    """
    faces = row.get("card_faces") or []
    out = []
    for index, face in enumerate(faces):
        built: dict[str, Any] = {"object": "card_face"}
        for key, value in face.items():
            # `colors` is a face key only where the faces own their own art: every face of every
            # two-image printing carries one, empty included (Agadeem, the Undercrypt is colorless
            # and still sends `"colors": []`), and no face of a split, flip, adventure or prepare
            # printing carries one at all. The engine always writes the key, so both halves of that
            # are decided here.
            if key == "colors":
                if two_image:
                    built[key] = value
                continue
            # Absent stays absent -- EXCEPT for `mana_cost` and `oracle_text`, where "" is a value
            # Scryfall does send. Every face of every multi-face printing in the corpus carries both
            # keys (8,620 of 8,620 transform faces, 4,356 of them with an empty cost), so an empty
            # string there is a costless back face, never an omission.
            if value is None or value == []:
                continue
            if value == "" and key not in ("mana_cost", "oracle_text"):
                continue
            built[key] = value
        if two_image:
            built["image_uris"] = _image_uris(
                row.get("scryfall_id", ""),
                row.get("image_updated_at"),
                "front" if index == 0 else "back",
            )
        out.append(built)
    return out


def _decimal(value: float | int | None) -> float | None:
    """Carry a mana value as the DECIMAL Scryfall types it as.

    api.scryfall.com answers `"cmc":1.0`, not `"cmc":1` — check
    https://api.scryfall.com/cards/named?exact=Lightning+Bolt. The field is decimal because
    fractional mana values are real: Little Girl costs {HW} and answers `"cmc":0.5`
    (https://api.scryfall.com/cards/named?exact=Little+Girl). A whole-numbered mana value therefore
    still serializes with its decimal point, and `magic.cards.cmc` being an `integer` column is what
    made this service answer `1` instead.

    That column is also why the underlying 0.5 cannot be stored at all today; changing its type is a
    migration and belongs on its own, and nothing that half-mana exists in is imported. This keeps
    the SERIALIZATION honest in the meantime, which is what a client comparing against Scryfall
    sees.

    Args:
        value: The stored mana value, or None. Typed narrowly rather than `Any` because this is the
            one place the column's type matters: an `integer` column is exactly what produced the
            wrong output.

    Returns:
        The value as a float, or None when the card has none.
    """
    return None if value is None else float(value)


def to_scryfall_card(row: dict[str, Any], *, base_url: str = "https://api.scryfall.com") -> dict[str, Any]:
    """Build the Scryfall card object for one engine row.

    BUILDS rather than unwraps a stored copy, which is the whole reason /cards/* can be served from
    the engine: an object assembled from columns is answerable from the store, while one recovered
    from `raw_card_blob` is answerable only from Postgres — and Postgres is the fallback for when
    the engine errors.

    Three sources, and every one of Scryfall's keys comes from exactly one: 29 stored columns, 12
    derived (every *_uri and image_uris, pure functions of the id/set/collector number/oracle id),
    and the 33-key residue carried in card_compat_blob. Only `resource_id` is dropped — an
    undocumented Scryfall internal with no stable meaning.

    Args:
        row: An engine row carrying CARD_OBJECT_FIELDS, or a SQL row through sql_row_to_engine_row.
        base_url: The host self-referencing URIs should address.

    Returns:
        The card object. Keys Scryfall omits stay omitted rather than becoming null, because a
        client comparing shapes would otherwise see a difference on every row.
    """
    # str() because a SQL row binds these as UUID objects while an engine row is already a string,
    # and every derived URI slices the id.
    scryfall_id = str(row.get("scryfall_id") or "")
    oracle_id = str(row.get("oracle_id") or "")
    name = row.get("name") or ""
    set_code = row.get("set_code") or ""
    number = row.get("collector_number") or ""
    lang = row.get("lang") or "en"
    layout = row.get("card_layout") or row.get("layout")
    has_faces = bool(row.get("card_faces"))
    # Only ever true for a card that HAS faces: the two-image layouts are all multi-face.
    two_image = has_faces and layout in _TWO_IMAGE_LAYOUTS
    # The joined name everywhere except edhrec on the layouts EDHREC files by front face.
    edhrec_name = name.split(" // ")[0] if has_faces and layout not in _EDHREC_JOINED_LAYOUTS else name
    faces = _faces(row, two_image)

    card: dict[str, Any] = {
        "object": "card",
        "id": scryfall_id,
        "oracle_id": oracle_id,
        "multiverse_ids": row.get("multiverse_ids") or [],
        "name": name,
        # Between `name` and `lang`, where api.scryfall.com puts it (verified on grn/212/pt and
        # khm/1/ja) — and PRESENT only when the printing carries one, which is why these are
        # conditional splats mid-literal rather than entries in the optional tail: the tail would
        # put them after `legalities`, and key position is part of the parity contract here the
        # same way security_stamp's position was.
        **({"printed_name": row["printed_name"]} if row.get("printed_name") else {}),
        # Scryfall's `flavor_name` — the alternate name a printing is SOLD under (the Godzilla
        # series, Stranger Things, the Secret Lair crossovers), which is a different thing from a
        # printed_name and can sit beside one. Its position is "immediately before `lang`" on all
        # 669 top-level occurrences in the 2026-08-16 all_cards bulk, verified live on prm/80925
        # (no printed_name) and sld/2236/ja (one). The FACE-level variant rides card_faces.
        **({"flavor_name": row["flavor_name"]} if row.get("flavor_name") else {}),
        "lang": lang,
        "released_at": row.get("released_at"),
        "uri": f"{base_url}/cards/{scryfall_id}",
        "scryfall_uri": _scryfall_uri(row, name, set_code, number, lang),
        "layout": row.get("card_layout") or row.get("layout"),
        "highres_image": bool(row.get("highres_image")),
        "image_status": row.get("image_status"),
        "cmc": _decimal(row.get("cmc")),
        "type_line": row.get("type_line"),
        # Directly after the oracle `type_line` it translates, per the live objects.
        **({"printed_type_line": row["printed_type_line"]} if row.get("printed_type_line") else {}),
        # `colors` is one of the values a two-image layout keeps on its faces alone (see
        # _TWO_IMAGE_LAYOUTS); `color_identity` is the card's and stays at top level on every layout.
        **({} if two_image else {"colors": row.get("colors") or []}),
        "color_identity": row.get("color_identity") or [],
        "keywords": row.get("card_keywords") or [],
        "games": row.get("games") or [],
        "reserved": "reserved" in (row.get("card_is_tags") or []),
        "finishes": row.get("finishes") or [],
        "oversized": bool(row.get("oversized")),
        "promo": bool(row.get("promo")),
        "reprint": bool(row.get("reprint")),
        "variation": bool(row.get("variation")),
        "set_id": row.get("set_id"),
        "set": set_code,
        "set_name": row.get("set_name"),
        "set_type": row.get("set_type"),
        "set_uri": f"{base_url}/sets/{row['set_id']}" if row.get("set_id") else None,
        "set_search_uri": f"{base_url}/cards/search?order=set&q=e%3A{set_code}&unique=prints",
        "scryfall_set_uri": f"https://scryfall.com/sets/{set_code}?utm_source=api",
        "rulings_uri": f"{base_url}/cards/{scryfall_id}/rulings",
        "prints_search_uri": f"{base_url}/cards/search?order=released&q=oracleid%3A{oracle_id}&unique=prints",
        "collector_number": number,
        "digital": bool(row.get("digital")),
        "rarity": row.get("rarity"),
        # No shared card back on a two-image layout, and no card-level illustration: both belong to
        # a face there, and Scryfall omits the top-level keys entirely.
        **({} if two_image else {"card_back_id": CARD_BACK_ID}),
        # An empty string is a VALUE here, not an absence: `artist` is `""` on 965 of the 540,484
        # printings in the 2026-08-16 bulk (and present on all of them), the same distinction
        # `mana_cost` and `oracle_text` draw below.
        "artist": row.get("artist"),
        **({} if two_image else {"illustration_id": str(row["illustration_id"]) if row.get("illustration_id") else None}),
        "border_color": row.get("border_color"),
        "full_art": bool(row.get("full_art")),
        "textless": bool(row.get("textless")),
        "booster": bool(row.get("booster")),
        "story_spotlight": bool(row.get("story_spotlight")),
        "prices": _prices(row),
        "related_uris": _related_uris(name, edhrec_name, row.get("multiverse_ids") or [], lang),
        **({"purchase_uris": _purchase_uris(row)} if _sold_somewhere(row) else {}),
    }

    # A multi-face card carries its faces and NOT the top-level ORACLE TEXT they replace; a
    # single-faced one carries the text and no `card_faces`. Which keys sit at top level varies by
    # LAYOUT, which is why this is a branch rather than a fixed key set.
    #
    # `mana_cost` and `image_uris` are the two the multi-face branch keeps, on the one-image layouts
    # only: one piece of cardboard has one picture and one printed cost, so Scryfall sends both at
    # top level for split/flip/adventure/prepare -- and neither for transform/modal_dfc, where each
    # face has its own.
    if faces:
        card["card_faces"] = faces
        if not two_image:
            card["mana_cost"] = _joined_mana_cost(row.get("card_faces") or [])
            card["image_uris"] = _image_uris(scryfall_id, row.get("image_updated_at"))
    else:
        # `""` is a value on both, and the row carries it verbatim: every basic land serves
        # `"mana_cost": ""` (61,908 printings) and 7,266 printings serve `"oracle_text": ""`.
        card["mana_cost"] = row.get("mana_cost")
        card["oracle_text"] = row.get("oracle_text")
        # Directly after the `oracle_text` it translates — single-face only, like the text it
        # shadows; a multi-face printing's printed text rides its face objects.
        if row.get("printed_text"):
            card["printed_text"] = row["printed_text"]
        card["image_uris"] = _image_uris(scryfall_id, row.get("image_updated_at"))

    # Keys Scryfall sends only when the card has them. Emitting null instead would differ from
    # Scryfall on every card that lacks them, which for most of these is most cards.
    for key, value in (
        ("power", row.get("power")),
        ("toughness", row.get("toughness")),
        # Where Scryfall puts it, beside the creature stats it is the planeswalker analogue of. The
        # PRINTED string: `planeswalker_loyalty` is a u8 in the engine and cannot hold "X" or "1+*".
        ("loyalty", row.get("loyalty")),
        ("flavor_text", row.get("flavor_text") or None),
        ("watermark", row.get("watermark")),
        ("frame", row.get("frame")),
        ("edhrec_rank", row.get("edhrec_rank")),
        ("penny_rank", row.get("penny_rank")),
        ("arena_id", row.get("arena_id")),
        ("mtgo_id", row.get("mtgo_id")),
        ("mtgo_foil_id", row.get("mtgo_foil_id")),
        ("tcgplayer_id", row.get("tcgplayer_id")),
        ("tcgplayer_etched_id", row.get("tcgplayer_etched_id")),
        ("cardmarket_id", row.get("cardmarket_id")),
        ("security_stamp", row.get("security_stamp")),
        # `color_indicator` and `produced_mana`: the printed colour dot on a card whose mana cost
        # cannot state its colours (a meld result, a coloured back), and the mana a card can make.
        # Both were stored and neither was ever emitted, so every land and every meld result this
        # service served was missing a key Scryfall sends.
        ("color_indicator", row.get("color_indicator") or None),
        ("produced_mana", row.get("produced_mana") or None),
        ("promo_types", row.get("promo_types") or None),
        ("frame_effects", row.get("frame_effects") or None),
        ("all_parts", row.get("all_parts") or None),
        ("legalities", row.get("legalities")),
    ):
        # Five of the string keys above, and color_indicator, belong to a face on a two-image
        # layout; `frame` is the printing's and stays. See _FACE_OWNED_KEYS.
        if two_image and key in _FACE_OWNED_KEYS:
            continue
        if value is not None:
            card[key] = value

    return card


class _Unset:
    """Sentinel for `error_object(warnings=...)`: the key is omitted entirely.

    Three states are needed, not two. api.scryfall.com writes `"warnings": null` on a
    `bad_request` from `/cards/search` even when nothing was warned about, writes the array when
    something was, and writes NO key at all on a `not_found`, a `validation_error` or
    `/cards/named`'s missing-parameter 400 (all measured 2026-08-16). `None` therefore has to mean
    "present and null", which leaves nothing for "absent" but a sentinel.
    """


_UNSET = _Unset()


def error_object(
    *,
    code: str,
    status: int,
    details: str,
    error_type: str | None = None,
    warnings: list[str] | _Unset | None = _UNSET,
) -> dict[str, Any]:
    """Build Scryfall's error object.

    `warnings` sits BEFORE `details`, which is Scryfall's own key order -- measured on every error
    body that carries one (`/cards/search?q=f:notaformat` and `/cards/search` with no `q` at all):
    `{object, code, status, warnings, details}`. This used to append it last, so a client comparing
    bodies byte for byte saw a different document for the same answer.

    Args:
        code: Scryfall's machine-readable error slug, e.g. "not_found".
        status: The HTTP status the response carries.
        details: Human-readable explanation.
        error_type: Scryfall's refinement of `code`, when it sends one -- `ambiguous` on a
            `/cards/named?fuzzy=` that resolved to more than one card. Emitted between `code` and
            `status`, which is where api.scryfall.com puts it.
        warnings: Non-fatal notes about the request. Pass a list or None to WRITE the key (null
            when there is nothing to say); omit the argument to leave the key out.

    Returns:
        The error object, with `type` present only when supplied and `warnings` positioned before
        `details`.
    """
    error: dict[str, Any] = {"object": "error", "code": code}
    if error_type is not None:
        error["type"] = error_type
    error["status"] = status
    if not isinstance(warnings, _Unset):
        error["warnings"] = warnings or None
    error["details"] = details
    return error


def not_found_error(details: str) -> dict[str, Any]:
    """Build the 404 error object.

    Args:
        details: Human-readable explanation.

    Returns:
        The error object.
    """
    return error_object(code="not_found", status=404, details=details)


def bad_request_error(details: str, *, warnings: list[str] | None = None) -> dict[str, Any]:
    """Build the 400 error object.

    Args:
        details: Human-readable explanation.
        warnings: Non-fatal notes about the request.

    Returns:
        The error object.
    """
    return error_object(code="bad_request", status=400, details=details, warnings=warnings)


def _list_object(  # noqa: PLR0913
    cards: list[dict[str, Any]],
    *,
    total_cards: int | None = None,
    has_more: bool | None = None,
    next_page: str | None = None,
    not_found: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build a List object, the one definition of its key order.

    `has_more` is written IFF the caller supplies one. That is the only difference between the two
    List envelopes Scryfall answers with, and it is a difference in the key SET rather than in the
    value -- see `collection_list`. Every other key keeps its position here so the two cannot drift.

    Args:
        cards: The page of objects.
        total_cards: Unpaginated match count; omitted on lists that do not paginate.
        has_more: Whether a further page exists; omitted entirely when None.
        next_page: Absolute URL of the next page, when there is one.
        not_found: Identifiers a collection request could not resolve.
        warnings: Non-fatal notes about the request.

    Returns:
        The List object.
    """
    result: dict[str, Any] = {"object": "list"}
    if total_cards is not None:
        result["total_cards"] = total_cards
    if not_found is not None:
        result["not_found"] = not_found
    if has_more is not None:
        result["has_more"] = has_more
    if next_page is not None:
        result["next_page"] = next_page
    if warnings:
        result["warnings"] = warnings
    result["data"] = cards
    return result


def card_list(  # noqa: PLR0913
    cards: list[dict[str, Any]],
    *,
    total_cards: int | None = None,
    has_more: bool = False,
    next_page: str | None = None,
    not_found: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build Scryfall's paginated List object.

    Key order follows Scryfall's own so a byte-comparing client sees the same document. `has_more`
    is always present: a paginated list says whether there is more even when there is not.

    Args:
        cards: The page of objects.
        total_cards: Unpaginated match count; omitted on lists that do not paginate.
        has_more: Whether a further page exists.
        next_page: Absolute URL of the next page, when there is one.
        not_found: Identifiers a collection request could not resolve.
        warnings: Non-fatal notes about the request.

    Returns:
        The List object.
    """
    return _list_object(
        cards,
        total_cards=total_cards,
        has_more=has_more,
        next_page=next_page,
        not_found=not_found,
        warnings=warnings,
    )


def collection_list(cards: list[dict[str, Any]], not_found: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the List `POST /cards/collection` answers with: `{object, not_found, data}`.

    NO `has_more`. Measured against api.scryfall.com on 2026-08-16 -- every collection response's
    key set is exactly those three, whether or not anything was found. It is the one List Scryfall
    does not paginate: the request carries at most `MAX_COLLECTION_IDENTIFIERS` identifiers and the
    answer carries all of them, so there is no further page for a `has_more` to describe.

    A separate entry point rather than a keyword at the call site, because omitting a key is the
    kind of thing a keyword hides: `card_list(found, not_found=not_found)` read as correct and
    quietly emitted `has_more: false`. Both build the same object through `_list_object`, so the key
    ORDER still has exactly one definition.

    Args:
        cards: The cards that resolved.
        not_found: The identifiers that resolved to nothing, in request order.

    Returns:
        The List object.
    """
    return _list_object(cards, not_found=not_found)


def catalog_object(values: list[str]) -> dict[str, Any]:
    """Build Scryfall's Catalog object.

    Args:
        values: The catalog entries.

    Returns:
        The Catalog object.
    """
    return {"object": "catalog", "total_values": len(values), "data": values}


def ruling_object(row: dict[str, Any]) -> dict[str, Any]:
    """Build one Scryfall Ruling object from a `magic.rulings` row.

    Args:
        row: A row with oracle_id, source, published_at and comment.

    Returns:
        The Ruling object.
    """
    return {
        "object": "ruling",
        "oracle_id": str(row["oracle_id"]),
        "source": row["source"],
        "published_at": row["published_at"].isoformat(),
        "comment": row["comment"],
    }


def build_page_url(base_url: str, params: dict[str, Any], page: int) -> str:
    """Build the absolute `next_page` URL for a search result.

    Scryfall spells every effective parameter into `next_page` rather than echoing only what the
    client sent, and clients follow the URL verbatim, so the query string is rebuilt from the
    resolved values.

    Args:
        base_url: Scheme and host the request arrived on, plus the route path.
        params: Effective query parameters, excluding `page`.
        page: The page number the URL should fetch.

    Returns:
        The absolute URL.
    """
    query = dict(sorted(params.items()))
    query["page"] = page
    return f"{base_url}?{urllib.parse.urlencode(sorted(query.items()))}"


def _face_of(card: dict[str, Any], face: str) -> dict[str, Any]:
    """Return the requested face of a card, falling back to the card itself.

    Args:
        card: A Scryfall card object.
        face: "back" for the second face; anything else selects the card/front.

    Returns:
        The face object, or the card when it has no distinct faces.
    """
    faces = card.get("card_faces") or []
    back_face_count = 2
    if face == "back" and len(faces) >= back_face_count:
        return faces[1]
    return card


def image_uri(card: dict[str, Any], *, version: str, face: str) -> str | None:
    """Return the image URL for a card at a given size and face.

    Args:
        card: A Scryfall card object.
        version: One of IMAGE_VERSIONS.
        face: "front" or "back".

    Returns:
        The image URL, or None when the card carries no image of that size.
    """
    selected = _face_of(card, face)
    uris = selected.get("image_uris") or card.get("image_uris") or {}
    return uris.get(version)


def _render_face(face: dict[str, Any]) -> str:
    """Render one card face in Scryfall's plain-text format.

    Args:
        face: A card or card_face object.

    Returns:
        The rendered face, without a trailing newline.
    """
    heading = face.get("name", "")
    mana_cost = face.get("mana_cost")
    if mana_cost:
        heading = f"{heading} {mana_cost}"

    lines = [heading]
    if face.get("type_line"):
        lines.append(face["type_line"])
    if face.get("oracle_text"):
        lines.append(face["oracle_text"])
    if face.get("power") is not None and face.get("toughness") is not None:
        lines.append(f"{face['power']}/{face['toughness']}")
    elif face.get("loyalty") is not None:
        lines.append(f"Loyalty: {face['loyalty']}")
    elif face.get("defense") is not None:
        lines.append(f"Defense: {face['defense']}")
    return "\n".join(lines)


def card_to_text(card: dict[str, Any]) -> str:
    """Render a card in Scryfall's `format=text` layout.

    Args:
        card: A Scryfall card object.

    Returns:
        The rendered card. Multi-face cards render every face, separated by a blank line.
    """
    faces = card.get("card_faces") or []
    if faces:
        return "\n\n".join(_render_face(face) for face in faces)
    return _render_face(card)
