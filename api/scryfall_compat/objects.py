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
import string
import urllib.parse
import uuid
from typing import Any

# Keys `preprocess_card` adds to the object it snapshots into raw_card_blob. Stripping them, and
# undoing the flavor_text normalization below, inverts the snapshot. A multi-face row carries only
# `card_name`; a single-face one carries all three.
_IMPORTER_ADDED_KEYS = ("card_name", "face_name", "face_idx")

# The `version` vocabulary of the image format -- SIX names, not the eleven `image_uris` carries.
#
# The two lists used to be the same list, and are not any more: Scryfall's `image_uris` gained five
# webp sizes (see _IMAGE_EXTENSIONS) that `version=` does not accept. Measured against
# api.scryfall.com on 2026-08-16 -- `?format=image&version=thumb` redirects to the LARGE jpg, byte
# for byte the same fallback `version=bogus` gets, and the same for grid/display/art/crop. So these
# five are emitted as URLs and refused as parameters, and widening this tuple to match the other
# would silently change five 302 targets.
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
    "power", "toughness", "loyalty", "colors", "color_identity", "card_keywords", "set_code", "set_name",
    "collector_number", "rarity", "flavor_text", "artist", "illustration_id", "released_at",
    "legalities", "edhrec_rank", "price_usd", "price_eur", "price_tix", "watermark",
    "card_frame_data", "card_is_tags", "border_color", "frame",
    "lang", "image_status", "set_type", "security_stamp", "set_id", "arena_id", "mtgo_id",
    "mtgo_foil_id", "tcgplayer_id", "tcgplayer_etched_id", "cardmarket_id", "penny_rank",
    "image_updated_at", "price_usd_foil", "price_usd_etched", "price_eur_foil", "multiverse_ids",
    "promo_types", "frame_effects", "games", "finishes", "booster", "digital", "foil", "nonfoil",
    "full_art", "highres_image", "oversized", "promo", "reprint", "story_spotlight", "textless",
    "variation", "card_faces", "all_parts",
)  # fmt: skip

# Scryfall's card back, one image for every normal card.
CARD_BACK_ID = "0aeebaf5-8c7d-4636-9e82-8c27447861f7"

# The file extension each `image_uris` size is served as, in Scryfall's own key order.
#
# ELEVEN, not the six this module shipped with. Scryfall added five webp sizes -- `thumb`, `grid`,
# `display`, `art`, `crop` -- and every card object it serves carries all eleven; a six-key
# `image_uris` differed from Scryfall on every card object emitted.
#
# Unconditional, and measured that way: across all 540,484 printings in the 2026-08-16 all_cards
# bulk, `image_uris` is either wholly ABSENT (8,444 cards, 7,641 faces -- the layouts whose picture
# lives on the other level) or carries exactly these eleven keys in exactly this order. No card,
# face, layout or `image_status` carries a partial set, so there is no per-key conditionality to
# round-trip the way `printed_*` has.
#
# Derived, not stored: the same scan confirms all eleven URLs are the same pure function of the id
# and the face on every one of the 548,604 objects that has them -- `art_crop` and `art` are
# different sizes of one path, not a stored pair. These five cost zero storage, which is why they
# are a table and not a column.
_IMAGE_EXTENSIONS = {
    "small": "jpg",
    "normal": "jpg",
    "large": "jpg",
    "png": "png",
    "art_crop": "jpg",
    "border_crop": "jpg",
    "thumb": "webp",
    "grid": "webp",
    "display": "webp",
    "art": "webp",
    "crop": "webp",
}

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


# Characters Scryfall DELETES from a slug rather than hyphenating. Live-derived: "Erayo's Essence"
# slugs to `erayos-essence` (not `erayo-s-essence`), "S.H.I.E.L.D." to `shield`, `Henzie "Toolbox"
# Torre` to `henzie-toolbox-torre`, and the zhs printings of Kongming/Pang Tong pin the curly
# quotes. U+201E ("bottom quote") is NOT deleted -- `Henzie ,,Der Beschaffer" Torre` (de) keeps it.
_SLUG_DELETED = frozenset("'\",./\u201c\u201d")

# Slug bytes served literally; every other byte is UTF-8 percent-encoded, uppercase hex. The literal
# set is exactly what appears un-encoded across the bulk corpus; `?` is the one ASCII special
# observed encoded. Unobserved characters encode, which can never break a URL.
_SLUG_LITERAL = frozenset(string.ascii_letters + string.digits + "!&()+-:;=_")

# The languages Scryfall writes into the scryfall_uri path -- its ten print localizations, exactly.
# The glyph and novelty languages (ph, qya, he, la, grc, ar, sa, dw) get NO path segment: a ph Elesh
# Norn lives at `/card/one/414/elesh-norn-mother-of-machines`, English form.
_SLUG_LANG_SEGMENTS = frozenset({"de", "es", "fr", "it", "ja", "ko", "pt", "ru", "zhs", "zht"})


def _slug(name: str) -> str:
    """Scryfall's URL slug for a card name.

    NOT the folklore "non-alphanumerics collapse to hyphens" rule this used to carry -- that
    hyphenates apostrophes (`erayo-s-essence`) and serves raw UTF-8 (`jötun-grunt`) where production
    Scryfall deletes the apostrophe and percent-encodes the bytes. The real rule, verified against
    the `scryfall_uri` of all 540,484 printings in the 2026-08-16 all_cards bulk (zero mismatches):

      1. lowercase;
      2. DELETE `' " , . /` and the curly quotes U+201C/U+201D;
      3. each run of ASCII spaces becomes one hyphen -- literal hyphens pass through and may stack
         (ru "Пламенник - военный разведчик" keeps `---`), and nothing is trimmed ("Humming-" and
         "With Great Power . . ." both keep their trailing hyphen);
      4. everything else survives verbatim (`:`, `!`, `&`, and CJK punctuation) and is then UTF-8
         percent-encoded per _SLUG_LITERAL.
    """
    cleaned = "".join(c for c in name.lower() if c not in _SLUG_DELETED)
    hyphenated = re.sub(" +", "-", cleaned)
    return "".join(
        chr(b) if chr(b) in _SLUG_LITERAL else f"%{b:02X}" for b in hyphenated.encode("utf-8")
    )


def _scryfall_uri(name: str, set_code: str, number: str, lang: str) -> str:
    """`https://scryfall.com/card/{set}/{number}[/{lang}]/{slug}?utm_source=api`.

    A foreign printing keeps the language segment and takes the plain English slug (ody/243/zhs ->
    `/zhs/holistic-wisdom`, verified live). Scryfall also writes a
    `slug(printed name)-(slug(english name))` path where it HAS a printed name (grn/212/pt is
    `ego-%C3%A0-deriva-(unmoored-ego)`); reproducing that needs the printed name, which the card
    row does not carry yet, and the English fallback is what it serves until then.
    """
    segment = f"{lang}/" if lang in _SLUG_LANG_SEGMENTS else ""
    return f"https://scryfall.com/card/{set_code}/{number}/{segment}{_slug(name)}?utm_source=api"


# The layouts Scryfall gives TWO images to -- the ones that are two pieces of cardboard, or a
# front and a back. Everything else with `card_faces` (split, flip, adventure, prepare) is ONE
# image, and its faces must NOT get per-face image_uris: doing so invents a `.../back/...` URL with
# no image behind it. Verified exhaustively against the 2026-08-16 all_cards bulk, zero exceptions
# in either direction. These layouts also keep `colors`, `card_back_id` and `illustration_id` on
# their faces alone.
_TWO_IMAGE_LAYOUTS = frozenset({"art_series", "double_faced_token", "modal_dfc", "reversible_card", "transform"})

# A REVERSIBLE printing keeps NOTHING of the card at top level -- not even the three keys every
# other multi-face layout keeps. Measured across the whole 2026-08-16 all_cards bulk: all 81 omit
# `oracle_id`, `cmc` and `type_line`, where a `transform` printing sends all three. Its FACES carry
# the card's `oracle_id` and `cmc` instead, 0 of 81 disagreeing, so omitting them loses nothing.
_REVERSIBLE_LAYOUT = "reversible_card"

# The layouts a SEARCH LINK spells with the JOINED name -- `related_uris.edhrec` and all three
# marketplace fallbacks in `purchase_uris`, which take one and the same string. Every other
# multi-face layout searches the FRONT face -- verified card for card against api.scryfall.com.
#
# THE MARKETPLACES SPLIT THE SAME WAY, which is why this is no longer edhrec's list alone. Measured
# on api.scryfall.com 2026-08-31 over `unique=prints`, on the first printing of each card whose ids
# are MISSING so the SEARCH form is what gets emitted, reading the tcgplayer term out of the `u=`
# parameter of Scryfall's own partner redirect:
#
#   split               Bind // Liberate                      cmb2/88   cardhoarder `Bind // Liberate`
#   reversible_card     Mechtitan // Mechtitan                sld/1969  cardhoarder `Mechtitan // Mechtitan`
#   double_faced_token  Snake // Zombie                       cc2/9     all three, `Snake // Zombie`
#   split               Who // What // When // Where // Why   und/75    cardhoarder the whole name
#   adventure           Champions of Archery // Join the …    ph19/4    `Champions of Archery`
#   flip                Curse of the Fire Penguin // …        unh/73    `Curse of the Fire Penguin`
#   art_series          Aang and Katara // Aang and Katara    atle/8    `Aang and Katara`
#   transform           Delver of Secrets // Insectile …      sld/2367  `Delver of Secrets`
#
# The two `tcgplayer_infinite_*` searches in `related_uris` are the exception that stays: they take
# the joined name on EVERY layout, so those two and this are deliberately not one string.
_JOINED_SEARCH_LAYOUTS = frozenset({"double_faced_token", "reversible_card", "split"})


def _related_uris(name: str, search_name: str, multiverse_ids: list[Any], lang: str) -> dict[str, str]:
    """Scryfall's `related_uris`, pointing at the destinations directly.

    Scryfall wraps the TCGplayer entries in `partner.tcgplayer.com/...?u=<encoded real URL>` with
    its own affiliate code. The destination is the same page, and emitting the wrapper from this
    host would route another service's affiliate revenue to Scryfall.

    `gatherer` LEADS the object when the printing has multiverse ids, built from the FIRST id, with
    `printed=true` for every non-English printing and `printed=false` for English -- verified
    against the bulk corpus at 540,430 of 540,484 printings. The 54 exceptions are foreign-only
    promos (dd2-ja, snc launch, one-ph, ltc-qya) whose Gatherer entries carry no translation; that
    fact lives on Scryfall's side of the wire and is not derivable from the row.
    """
    out: dict[str, str] = {}
    first_id = multiverse_ids[0] if multiverse_ids else None
    if isinstance(first_id, int):
        printed = "false" if lang == "en" else "true"
        out["gatherer"] = (
            f"https://gatherer.wizards.com/Pages/Card/Details.aspx?multiverseid={first_id}&printed={printed}"
        )
    quoted = urllib.parse.quote_plus(name)
    out["tcgplayer_infinite_articles"] = (
        f"https://www.tcgplayer.com/search/articles?productLineName=magic&q={quoted}"
    )
    out["tcgplayer_infinite_decks"] = f"https://www.tcgplayer.com/search/decks?productLineName=magic&q={quoted}"
    out["edhrec"] = f"https://edhrec.com/route/?cc={urllib.parse.quote_plus(search_name)}"
    return out


def _purchase_uris(row: dict[str, Any], search_name: str) -> dict[str, str]:
    """Scryfall's `purchase_uris`, product links where the ids exist and name searches where not.

    Rebuilt from the marketplace ids -- or, for a key whose id this printing does not have, from a
    NAME SEARCH on that marketplace. Same affiliate reasoning as `_related_uris`.

    All three keys are always present. The fallback is per KEY, not per card: an English printing
    with TCGplayer and Cardmarket ids but no MTGO id gets two product links and a cardhoarder
    search (verified live across khm). Every foreign printing takes the search form on all three --
    marketplace product ids belong to the English printing. Emitting nothing was the alternative,
    and it made `purchase_uris` an empty object on 426,416 printings.

    `search_name` IS `_related_uris`' -- the caller decides the string, and all three marketplaces
    split by layout exactly the way edhrec does (the measurements are on _JOINED_SEARCH_LAYOUTS).
    This took the joined name and cut the front face off it here, on EVERY layout, which searched
    for `Snake // Zombie` (cc2/9) as `Snake` and `Who // What // When // Where // Why` (und/75) as
    `Who` against a Scryfall that spells both whole. On a transforming card the front face is still
    right -- `Invasion of Alara`, not `Invasion of Alara // Awaken the Maelstrom` -- because there
    the joined string matches no product.
    """
    q = urllib.parse.quote_plus(search_name)
    tcg, cm, mtgo = row.get("tcgplayer_id"), row.get("cardmarket_id"), row.get("mtgo_id")
    return {
        "tcgplayer": (
            f"https://www.tcgplayer.com/product/{tcg}?page=1"
            if tcg
            else f"https://www.tcgplayer.com/search/magic/product?productLineName=magic&q={q}&view=grid"
        ),
        "cardmarket": (
            f"https://www.cardmarket.com/en/Magic/Products?idProduct={cm}"
            if cm
            else f"https://www.cardmarket.com/en/Magic/Products/Search?searchString={q}"
        ),
        "cardhoarder": (
            f"https://www.cardhoarder.com/cards/{mtgo}"
            if mtgo
            else f"https://www.cardhoarder.com/cards?data%5Bsearch%5D={q}"
        ),
    }


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


def _faces(row: dict[str, Any], *, two_image: bool, reversible: bool) -> list[dict[str, Any]]:
    """The card's faces, with the keys the engine deliberately does not store re-added.

    `object` is the constant "card_face", and a face's `image_uris` is the card's CDN function with
    front/back swapped, so neither is worth archive space.

    `image_uris` is gated on the LAYOUT rather than on the face count: only a two-image layout has
    a second picture, and giving one to a split or adventure face invents a URL with nothing behind
    it. An empty `mana_cost` or `oracle_text` on a face is a VALUE, not an omission -- every face
    of every multi-face printing in the corpus carries both keys (8,620 of 8,620 transform faces,
    4,356 of them with an empty cost), so an empty string there is a costless back face.
    """
    faces = row.get("card_faces") or []
    out = []
    for index, face in enumerate(faces):
        built: dict[str, Any] = {"object": "card_face"}
        built.update(
            {
                key: value
                for key, value in face.items()
                if value is not None and (value not in ("", []) or key in ("mana_cost", "oracle_text"))
            }
        )
        if reversible:
            # Both faces of a reversible printing carry the CARD's oracle_id and cmc.
            built.setdefault("oracle_id", str(row.get("oracle_id") or ""))
            built.setdefault("cmc", _decimal(row.get("cmc")))
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
    reversible = layout == _REVERSIBLE_LAYOUT
    faces = _faces(row, two_image=two_image, reversible=reversible)
    # The name a SEARCH LINK spells: the joined one, except on the layouts whose searches take the
    # front face (see _JOINED_SEARCH_LAYOUTS). `related_uris.edhrec` and every `purchase_uris`
    # fallback take THIS string; the two `tcgplayer_infinite_*` links take the joined `name`.
    search_name = name.split(" // ", 1)[0] if faces and layout not in _JOINED_SEARCH_LAYOUTS else name

    card: dict[str, Any] = {
        "object": "card",
        "id": scryfall_id,
        "oracle_id": oracle_id,
        "multiverse_ids": row.get("multiverse_ids") or [],
        "name": name,
        "lang": lang,
        "released_at": row.get("released_at"),
        "uri": f"{base_url}/cards/{scryfall_id}",
        "scryfall_uri": _scryfall_uri(name, set_code, number, lang),
        "layout": row.get("card_layout") or row.get("layout"),
        "highres_image": bool(row.get("highres_image")),
        "image_status": row.get("image_status"),
        "cmc": _decimal(row.get("cmc")),
        "type_line": row.get("type_line"),
        "colors": row.get("colors") or [],
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
        "card_back_id": CARD_BACK_ID,
        "artist": row.get("artist"),
        "illustration_id": str(row["illustration_id"]) if row.get("illustration_id") else None,
        "border_color": row.get("border_color"),
        "full_art": bool(row.get("full_art")),
        "textless": bool(row.get("textless")),
        "booster": bool(row.get("booster")),
        "story_spotlight": bool(row.get("story_spotlight")),
        "prices": _prices(row),
        "related_uris": _related_uris(name, search_name, row.get("multiverse_ids") or [], lang),
    }
    # A printing NO MARKETPLACE SELLS omits the key rather than carrying three dead links. The
    # rule is the marketplaces, not `digital` -- measured 2026-08-16: prm/80925 (games ["mtgo"],
    # digital true) HAS purchase_uris and ymid/59 and khm/A-198 (games ["arena"], digital true) do
    # not, so it is "paper or mtgo". An ABSENT `games` list emits: the omission is a positive claim
    # about the printing rather than a gap.
    games = row.get("games")
    if games is None or not games or any(g in ("paper", "mtgo") for g in games):
        card["purchase_uris"] = _purchase_uris(row, search_name)

    # A two-image layout keeps `colors`, `card_back_id` and `illustration_id` on its FACES alone --
    # there is no shared back and no card-level illustration when the card is two pictures --
    # and Scryfall omits the top-level keys entirely rather than nulling them.
    if two_image:
        for key in ("colors", "card_back_id", "illustration_id"):
            card.pop(key, None)
    # ...and a reversible printing drops the three the other two-image layouts keep.
    if reversible:
        for key in ("oracle_id", "cmc", "type_line"):
            card.pop(key, None)

    # A multi-face card carries its faces and NOT the top-level text they replace; a single-faced
    # one carries the text and no `card_faces`. Which keys sit at top level varies by LAYOUT, which
    # is why this is a branch rather than a fixed key set.
    if faces:
        card["card_faces"] = faces
        if not two_image:
            # ONE image and one cost: a split/flip/adventure/prepare printing keeps both at top
            # level, the cost joined " // " between the faces that HAVE one, skipping the ones
            # that do not -- flipped Erayo, whose back face carries an empty cost, is `{1}{U}` and
            # not `{1}{U} // `. Checked against all 3,654 such printings with zero misses.
            card["mana_cost"] = " // ".join(
                f["mana_cost"] for f in (row.get("card_faces") or []) if f.get("mana_cost")
            )
            card["image_uris"] = _image_uris(scryfall_id, row.get("image_updated_at"))
    else:
        card["mana_cost"] = row.get("mana_cost")
        card["oracle_text"] = row.get("oracle_text")
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
        ("promo_types", row.get("promo_types") or None),
        ("frame_effects", row.get("frame_effects") or None),
        ("all_parts", row.get("all_parts") or None),
        ("legalities", row.get("legalities")),
    ):
        if value is not None:
            card[key] = value

    return card


def error_object(*, code: str, status: int, details: str, warnings: list[str] | None = None) -> dict[str, Any]:
    """Build Scryfall's error object.

    Args:
        code: Scryfall's machine-readable error slug, e.g. "not_found".
        status: The HTTP status the response carries.
        details: Human-readable explanation.
        warnings: Non-fatal notes about the request, when there are any.

    Returns:
        The error object, with `warnings` present only when non-empty.
    """
    error: dict[str, Any] = {"object": "error", "code": code, "status": status, "details": details}
    if warnings:
        error["warnings"] = warnings
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


def card_list(  # noqa: PLR0913
    cards: list[dict[str, Any]],
    *,
    total_cards: int | None = None,
    has_more: bool = False,
    next_page: str | None = None,
    not_found: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build Scryfall's List object.

    Key order follows Scryfall's own so a byte-comparing client sees the same document.

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
    result: dict[str, Any] = {"object": "list"}
    if total_cards is not None:
        result["total_cards"] = total_cards
    if not_found is not None:
        result["not_found"] = not_found
    result["has_more"] = has_more
    if next_page is not None:
        result["next_page"] = next_page
    if warnings:
        result["warnings"] = warnings
    result["data"] = cards
    return result


def catalog_object(values: list[str], uri: str | None = None) -> dict[str, Any]:
    """Build Scryfall's Catalog object.

    `uri` is present IFF one is given, and the two callers genuinely differ -- measured against
    api.scryfall.com on 2026-08-12, `/catalog/battle-types` answers
    `{"object": "catalog", "uri": "...", "total_values": 1, "data": ["Siege"]}` while
    `/cards/autocomplete` answers the same object with no `uri` at all. Building one unconditionally
    would put a key on the autocomplete catalog that Scryfall does not send.

    The uri points at api.scryfall.com rather than at this host, which is the rule the card objects
    already follow: a self-referencing URI is part of the payload, not pagination.

    Args:
        values: The catalog entries.
        uri: The catalog's own URI, for the routes that carry one.

    Returns:
        The Catalog object.
    """
    catalog: dict[str, Any] = {"object": "catalog"}
    if uri is not None:
        catalog["uri"] = uri
    catalog["total_values"] = len(values)
    catalog["data"] = values
    return catalog


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
