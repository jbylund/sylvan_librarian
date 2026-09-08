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


def _slug(name: str) -> str:
    """Scryfall's URL slug for a card name: lowercase, non-alphanumerics collapsed to hyphens."""
    out = "".join(c if c.isalnum() else "-" for c in name.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def _related_uris(name: str) -> dict[str, str]:
    """Scryfall's `related_uris`, pointing at the destinations directly.

    Scryfall wraps the TCGplayer entries in `partner.tcgplayer.com/...?u=<encoded real URL>` with
    its own affiliate code. The destination is the same page, and emitting the wrapper from this
    host would route another service's affiliate revenue to Scryfall.
    """
    quoted = urllib.parse.quote_plus(name)
    return {
        "tcgplayer_infinite_articles": f"https://www.tcgplayer.com/search/articles?productLineName=magic&q={quoted}",
        "tcgplayer_infinite_decks": f"https://www.tcgplayer.com/search/decks?productLineName=magic&q={quoted}",
        "edhrec": f"https://edhrec.com/route/?cc={quoted}",
    }


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


def _faces(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The card's faces, with the two keys the engine deliberately does not store re-added.

    `object` is the constant "card_face", and a face's `image_uris` is the card's CDN function with
    front/back swapped, so neither is worth archive space.
    """
    faces = row.get("card_faces") or []
    out = []
    for index, face in enumerate(faces):
        built: dict[str, Any] = {"object": "card_face"}
        built.update({key: value for key, value in face.items() if value not in (None, "", [])})
        if len(faces) > 1:
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
    faces = _faces(row)

    card: dict[str, Any] = {
        "object": "card",
        "id": scryfall_id,
        "oracle_id": oracle_id,
        "multiverse_ids": row.get("multiverse_ids") or [],
        "name": name,
        "lang": row.get("lang") or "en",
        "released_at": row.get("released_at"),
        "uri": f"{base_url}/cards/{scryfall_id}",
        "scryfall_uri": f"https://scryfall.com/card/{set_code}/{number}/{_slug(name)}?utm_source=api",
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
        "related_uris": _related_uris(name),
        "purchase_uris": _purchase_uris(row),
    }

    # A multi-face card carries its faces and NOT the top-level text they replace; a single-faced
    # one carries the text and no `card_faces`. Which keys sit at top level varies by LAYOUT, which
    # is why this is a branch rather than a fixed key set.
    if faces:
        card["card_faces"] = faces
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


def error_object(
    *,
    code: str,
    status: int,
    details: str,
    error_type: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build Scryfall's error object.

    Args:
        code: Scryfall's machine-readable error slug, e.g. "not_found".
        status: The HTTP status the response carries.
        details: Human-readable explanation.
        error_type: Scryfall's refinement of `code`, when it sends one -- `ambiguous` on a
            `/cards/named?fuzzy=` that resolved to more than one card. Emitted between `code` and
            `status`, which is where api.scryfall.com puts it.
        warnings: Non-fatal notes about the request, when there are any.

    Returns:
        The error object, with `type` and `warnings` present only when supplied.
    """
    error: dict[str, Any] = {"object": "error", "code": code}
    if error_type is not None:
        error["type"] = error_type
    error["status"] = status
    error["details"] = details
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
