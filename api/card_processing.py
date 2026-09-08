"""Card processing functions."""

from __future__ import annotations

import copy
import functools
import re
from typing import TYPE_CHECKING, Any

from api.parsing.card_query_nodes import calculate_devotion, fold_accents, mana_cost_str_to_dict
from api.parsing.db_info import FACE_TEXT_SEPARATOR as _FACE_TEXT_SEPARATOR

if TYPE_CHECKING:
    from collections.abc import Callable


# Card types that can exist as a permanent on the battlefield. Devotion (MTG
# comprehensive rules) is defined only over permanents' mana costs, confirmed
# against the real Scryfall API (devotion: never matches a pure Instant/Sorcery,
# e.g. the real Lightning Bolt), so calculate_devotion()'s result is discarded
# for any card with no type in this set. Title-cased to match parse_type_line().
PERMANENT_CARD_TYPES = {"Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker"}


def parse_type_line(type_line: str) -> tuple[list[str], list[str]]:
    """Parse the type line of a card."""
    card_types, _, card_subtypes = (x.strip().split() for x in type_line.title().partition("\u2014"))
    return card_types, card_subtypes or []


def maybeify(func: Callable) -> Callable:
    """Convert value to int (via float first), returning None if conversion fails."""

    @functools.wraps(func)
    def wrapper(val: str | int | float | None) -> int | None:
        if val is None:
            return None
        try:
            return func(val)
        except (ValueError, TypeError):
            return None

    return wrapper


@maybeify
def maybe_float(val: str | int | float | None) -> float | None:
    """Convert value to float, returning None if conversion fails."""
    return float(val)


@maybeify
def maybe_int(val: str | int | float | None) -> int | None:
    """Convert value to int (via float first), returning None if conversion fails."""
    return int(float(val))


def rarity_text_to_int(rarity_text: str) -> int:
    """Convert rarity text to int."""
    rarity_map = {
        "common": 0,
        "uncommon": 1,
        "rare": 2,
        "mythic": 3,
        "special": 4,
        "bonus": 5,
    }
    return rarity_map.get(rarity_text.lower(), -1)


def extract_collector_number_int(collector_number: str | int | float | None) -> int | None:
    """Extract the integer part of a collector number."""
    if collector_number is None:
        return None
    # Implement magic.extract_collector_number_int in Python
    # Extract numeric characters using regex, similar to the database function
    numeric_part = re.sub(r"[^0-9]", "", str(collector_number))
    if numeric_part:
        try:
            int_val = int(numeric_part)
            # PostgreSQL integer range is -2^31 to 2^31-1
            if -(2**31) <= int_val <= 2**31 - 1:
                return int_val
        except (ValueError, OverflowError):
            pass
    return None  # Field will be null by default


def extract_frame_data_from_raw_card(raw_card: dict) -> dict[str, bool]:
    """Extract frame data from a raw card dictionary.

    Combines frame version and frame effects into a single JSONB object,
    following the same pattern as _preprocess_card method.

    Args:
        raw_card: Raw card dictionary from Scryfall API.

    Returns:
        Dictionary mapping frame data keys to True.
    """
    frame_data = {}

    # Add frame version if present (titlecased for consistency)
    frame_version = raw_card.get("frame")
    if frame_version:
        frame_data[frame_version.title()] = True

    # Add frame effects if present (titlecased for consistency)
    frame_effects = raw_card.get("frame_effects", [])
    for effect in frame_effects:
        frame_data[effect.title()] = True

    return frame_data


# Face-merge policy for multi-face cards (#400, #873). Scryfall AND's search predicates at the
# CARD level, each satisfiable by any face — measured against api.scryfall.com 2026-08-08:
# `t:sorcery t:land` returns the MDFC lands (no single face is both), o: conjunctions match
# across faces (Ral, Monsoon Mage), and `c:b` matches Westvale Abbey's back-face-only color.
# One row per printing carrying any-face unions reproduces those semantics directly; one row
# per face would instead break every cross-face conjunction (no face-row satisfies both terms)
# on top of colliding on the scryfall_id primary key, which is how the back face silently won
# until now. Front-face scalars (cmc, mana cost, illustration, image, prices) match Scryfall's
# own top-level fields, verified on its card objects.
# `illustration_ids` is here and not among the front-face scalars because a printing SHOWS every
# face's art, and the art tags attached to it are the union over all of them (api/tag_import.py).
# `illustration_id` stays the front's, matching Scryfall's own top-level field.
_FACE_LIST_UNIONS = ("card_types", "card_subtypes", "illustration_ids")
_FACE_FLAG_UNIONS = ("card_colors", "card_keywords", "produced_mana")
# `type_line` and `mana_cost_text` join with " // " and the other two with _FACE_TEXT_SEPARATOR --
# see _JOINED_WITH_SLASHES, which is also the line between the columns search may take apart and
# the ones it may not (FACE_JOINED_TEXT_COLUMNS).
_FACE_JOINED_TEXTS = ("oracle_text", "flavor_text", "type_line", "mana_cost_text")
# The two columns whose separator is SCRYFALL's own rather than one this branch invented, so
# matching must leave them whole.
#
# `type_line` is Scryfall's top-level field for a split card ("Instant // Instant") and `t:/\/\//`
# answers 930 there. `mana_cost_text` is the same story and took a measurement to establish,
# because Scryfall's CARD OBJECT is not the evidence: it carries a top-level `mana_cost` on the
# one-image layouts only (split/adventure/prepare/flip -- 949 of 949 in the 2026-08-28
# default_cards bulk) and NONE on the two-image ones, while its SEARCH index carries the join for
# both. Probed on api.scryfall.com 2026-08-28, each as the card's own `!"..."` ANDed with the
# pattern so the corpus filters cannot confound it:
#
#   !"Extus, Oriq Overlord // Awaken the Blood Avatar"  a modal DFC, with NO top-level mana_cost
#     mana:/\/\//           1   the seam is in the haystack -- the decisive row
#     mana:/{b}{b} \/\/ /   1   ...and a pattern spans it, so it is ONE string, not a set
#   !"Fire // Ice" mana:/^{u}$/           0   the back half alone is not a value of its own
#   !"Delver of Secrets // Insectile Aberration"
#     mana:/^{u}$/          1   an EMPTY back face contributes nothing, not even a separator
#     mana:/^{u} /          0
#   !"Westvale Abbey // Ormendahl, Profane Prince"
#     mana:/^$/             1   ...so an all-costless card is EMPTY, never " // "
#     mana:/\/\//          0
#
# Corpus-wide the same day: `mana:/\/\// is:mdfc` is 40 of 100, and `mana:/^$/ is:artseries` is
# 2,243 of 2,243.
_JOINED_WITH_SLASHES = ("type_line", "mana_cost_text")
# Copied per GROUP from the first face that has any of the group, so the numeric columns and
# their _text twins always describe the same face (the schema's check constraints couple them).
_FACE_STAT_GROUPS = (
    ("creature_power", "creature_toughness", "creature_power_text", "creature_toughness_text"),
    ("planeswalker_loyalty", "planeswalker_loyalty_text"),
)
# Joins face texts, and is defined in api/parsing/db_info.py because SEARCH has to know it too:
# "in practice" was not good enough. The newline stops `.` crossing a face boundary and nothing
# else -- `o:/\ndraw/` matched the separator's own newline and answered 389 where Scryfall answers
# 381, and `o://` matched the "//" and answered 849 where Scryfall answers 1. Matching now splits
# the value back on this constant, per face, on both the engine and SQL paths.


def _merge_processed_faces(faces: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse fully-processed per-face rows into the card's single searchable row.

    The first face (the front) supplies the row and with it every identity and display
    scalar; later faces fold in per the policy tables above. Known residual, sized in
    the tests: when several faces carry a stat group (Brutal Cathar's 2/2 // 3/3), only
    the first face's values are searchable — Scryfall also matches the back's.

    Args:
        faces: Non-empty list of processed rows, one per surviving face, front first.

    Returns:
        The merged row (the front face's dict, mutated in place).
    """
    merged, *rest = faces
    for face in rest:
        for key in _FACE_LIST_UNIONS:
            seen = merged[key]
            seen.extend(value for value in face[key] if value not in seen)
        for key in _FACE_FLAG_UNIONS:
            merged[key].update(face[key])
        for key in _FACE_JOINED_TEXTS:
            parts = [part for part in (merged.get(key), face.get(key)) if part]
            merged[key] = (" // " if key in _JOINED_WITH_SLASHES else _FACE_TEXT_SEPARATOR).join(parts)
        for group in _FACE_STAT_GROUPS:
            if all(merged.get(field) is None for field in group) and any(face.get(field) is not None for field in group):
                for field in group:
                    merged[field] = face.get(field)
    return merged


def preprocess_card(card: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: PLR0915,C901,PLR0912
    """Preprocess a card to remove invalid cards and add necessary fields.

    A multi-face card (transform, MDFC, split, adventure, flip) is merged into ONE row
    carrying the front face's identity and every face's searchable data — see
    `_merge_processed_faces`. Single-faced cards return a list with one dictionary.
    Returns an empty list for invalid/filtered cards.
    """
    if not set(card["legalities"].values()) & {"legal", "restricted"}:
        return []
    if "playtest" in card.get("promo_types", []):
        return []
    if "paper" not in card.get("games", []):
        return []
    if card.get("set_type") == "funny":
        return []

    # Filter out unplayable cards: Cards and Tokens
    type_line = card.get("type_line")
    if type_line:
        card_types, card_subtypes = parse_type_line(type_line)
        if "Card" in card_types or "Token" in card_types:
            return []

    # Filter out "X // X" cards (same name on both faces, e.g. "Name // Name")
    card_name = card.get("name", "")
    if "//" in card_name:
        left_name, _, right_name = card_name.partition("//")
        if left_name.strip() == right_name.strip():
            return []

    if "raw_card_blob" in card:
        # Already processed, don't need to re-process
        return [card]

    # Lift the card name before processing faces, because it shouldn't be clobbered by card_faces
    if "card_name" not in card:
        # Non-recursive case: first time seeing this card
        card["card_name"] = card.get("name")
    else:
        # Recursive case: processing a face
        card["face_name"] = card.get("name")

    # Handle cards with card_faces (DFCs): process each face through the full pipeline below,
    # then collapse the per-face rows into the card's one searchable row.
    card_faces = card.get("card_faces")
    if card_faces:
        for creature_attribute in ["creature_power", "creature_toughness"]:
            card.pop(creature_attribute, None)
            card.pop(f"{creature_attribute}_text", None)
        face_rows = []
        for face_data in card_faces:
            # Merge card-level data with face-specific data
            # Precedence: face_data (name, type_line, etc.) > card (legalities, games, etc.)
            merged = copy.deepcopy(card) | face_data
            merged.pop("card_faces", None)  # Don't keep recursing
            face_rows.extend(preprocess_card(merged))
        if not face_rows:
            return []
        merged_row = _merge_processed_faces(face_rows)
        # ...and then the CARD's own type line outranks the join, because Scryfall's row is that
        # line and not always the join. `type_line` here is the top-level field read off the card
        # above, before any face was folded in.
        #
        # The join IS Scryfall's answer nearly everywhere -- measured over the 2026-08-31
        # default_cards bulk, 5,110 of 5,112 faced printings that carry a top-level `type_line`
        # carry exactly `" // ".join(face type lines)`: split (`Bind // Liberate` and `Fire // Ice`
        # are both "Instant // Instant"), adventure (`Champions of Archery // Join the Group` is
        # "Legendary Creature — Human Archer // Sorcery — Adventure"), flip, transform, MDFC. The
        # two that differ are the two printings of the only five-faced card, `Who // What // When
        # // Where // Why` (und/75 and unh/120): Scryfall says the bare "Instant" where the join
        # says it five times. Neither reaches this line -- both are `not_legal` in every format --
        # so this is the rule stated where the join is made rather than a change to today's rows.
        #
        # The FALLBACK is the live half. A reversible printing carries no top-level `type_line` at
        # all (81 of 81 in the same bulk), and three of them survive the filters above -- tdm/378,
        # tdm/379, tdm/381, the Tarkir omen dragons, whose doubled siblings the `X // X` name
        # filter drops. Taking `card["type_line"]` unconditionally would null the type line on
        # those three; the join is their only one, so it stands.
        #
        # `card_types`/`card_subtypes` are NOT recomputed from this string, deliberately: they are
        # the per-face union built in `_merge_processed_faces` (`_FACE_LIST_UNIONS`), which is the
        # only reading that survives a joined line. `parse_type_line` splits on the FIRST em dash,
        # so parsing "Legendary Creature — Human Archer // Sorcery — Adventure" back would file
        # "Sorcery" under SUBTYPES. The union is right and stays.
        if type_line:
            merged_row["type_line"] = type_line
        # The blob is the card-level object with its faces re-attached — what Scryfall sent, not a
        # face promoted to look like a card. Every searchable field is already merged onto the row
        # above, so the blob has no derivation left to do, and keeping it verbatim is what makes it
        # answerable: a card object cannot be rebuilt from a face (`card_faces` is gone, `name` and
        # `type_line` are the front's, and which fields a real card carries at top level varies by
        # layout — a split card has `mana_cost` and `image_uris` there, a transform card does not).
        #
        # The one consumer that read a *face* field off the blob is `image_uris`, which for a
        # transform card now lives only under `card_faces`; every reader coalesces to
        # `card_faces->0` (scripts/copy_images_to_s3.py, scripts/prefer_weights.py). Everything else
        # read from the blob — lang, set_type, games, finishes, frame_effects, image_status,
        # reserved, game_changer — is card-level and identical either way.
        merged_row["raw_card_blob"] = copy.deepcopy(card) | {"card_faces": card_faces}
        return [merged_row]

    # Single face case - set defaults
    card.setdefault("face_name", card.get("name"))
    card.setdefault("face_idx", 1)

    # Scryfall omits flavor_text entirely when a printing has none (unlike oracle_text, which it
    # always sends, empty string included, even for vanilla cards). Normalize to '' so negated
    # flavor-text filters treat "no flavor text" as empty, matching Scryfall's own search behavior
    # (confirmed empirically: -flavor:<impossible> includes flavorless prints on scryfall.com) and
    # the engine's existing unwrap_or_default() handling.
    card["flavor_text"] = card.get("flavor_text") or ""

    # Store the original card data before modifications for raw_card_blob
    raw_card_data = copy.deepcopy(card)
    card["raw_card_blob"] = raw_card_data
    card["scryfall_id"] = card["id"]

    card_types, card_subtypes = parse_type_line(card["type_line"])
    card["card_types"] = card_types
    card["card_subtypes"] = card_subtypes

    card["planeswalker_loyalty"] = maybe_int(card.get("loyalty"))
    if "Creature" in card_types or {"Vehicle", "Spacecraft"} & set(card_subtypes):
        card["creature_power"] = maybe_int(card.get("power"))
        card["creature_toughness"] = maybe_int(card.get("toughness"))
        card["creature_power_text"] = card.get("power")
        card["creature_toughness_text"] = card.get("toughness")
    else:
        # Explicit None (not pop) so these keys appear as JSON null in the processed blob.
        # An absent key falls through to the existing DB row's value during upsert merging;
        # an explicit null overrides it, keeping creature_power_text/creature_toughness_text
        # in sync with creature_power/creature_toughness for the check constraint.
        card["creature_power_text"] = None
        card["creature_toughness_text"] = None
        card["creature_power"] = None
        card["creature_toughness"] = None

    # objects of keys to true
    card["card_colors"] = dict.fromkeys(card["colors"], True)
    card["card_color_identity"] = dict.fromkeys(card["color_identity"], True)
    # Lowercased so the stored key matches what `keyword:` looks up -- Scryfall's own spelling is
    # inconsistently cased ("First strike", "Doctor's companion"), and lowercase is the same
    # normalization the oracle/art/is tag collections already use on both sides.
    card["card_keywords"] = dict.fromkeys((keyword.lower() for keyword in card.get("keywords", [])), True)
    card["produced_mana"] = dict.fromkeys(card.get("produced_mana", []), True)

    card["edhrec_rank"] = card.get("edhrec_rank")

    card["card_frame_data"] = extract_frame_data_from_raw_card(card)

    # Extract pricing data if available - ensure they are floats for jsonb_populate_record
    prices = card.get("prices", {})
    card["price_usd"] = maybe_float(prices.get("usd"))
    card["price_eur"] = maybe_float(prices.get("eur"))
    card["price_tix"] = maybe_float(prices.get("tix"))

    # Extract set code for dedicated column (lowercased for case-insensitive search;
    # Scryfall codes are lowercase already, this just makes the invariant explicit)
    set_code = card.get("set")
    card["card_set_code"] = set_code.lower() if isinstance(set_code, str) else set_code

    # Extract layout and border for dedicated columns (lowercased for case-insensitive search)
    if "layout" in card:
        card["card_layout"] = card["layout"].lower()
    if "border_color" in card:
        card["card_border"] = card["border_color"].lower()
    if "watermark" in card:
        card["card_watermark"] = card["watermark"].lower()

    mana_cost_text = card.get("mana_cost", "")
    card["mana_cost_jsonb"] = mana_cost_str_to_dict(mana_cost_text)
    # Nonpermanents (Instant/Sorcery) never contribute devotion, regardless of
    # their mana cost - see PERMANENT_CARD_TYPES.
    is_permanent = bool(PERMANENT_CARD_TYPES & set(card_types))
    card["devotion"] = calculate_devotion(mana_cost_text) if is_permanent else {}

    # Map field names to match database column names for jsonb_populate_record
    # Don't overwrite card_name if already set (for DFCs, it's set before processing faces)
    if "card_name" not in card:
        card["card_name"] = card.get("name")
    # Accent-folded lowercase name, precomputed once at import so fuzzy name: search can
    # match "eowyn" against "Éowyn" without folding diacritics on every query (#649).
    card["card_name_folded"] = fold_accents(card["card_name"].lower())
    card["mana_cost_text"] = card.get("mana_cost")
    card["planeswalker_loyalty_text"] = card.get("loyalty")
    card["card_artist"] = card.get("artist")

    # Handle CMC and edhrec_rank conversion using helper function
    card["cmc"] = maybe_int(card.get("cmc"))

    # Handle rarity conversion - implement in Python to avoid SQL boilerplate
    rarity_text = card.get("rarity", "").lower()
    if rarity_text:
        card["card_rarity_text"] = rarity_text
        card["card_rarity_int"] = rarity_text_to_int(rarity_text)

    # Handle collector number - implement extraction in Python to avoid SQL boilerplate
    collector_number = card.get("collector_number")
    card["collector_number"] = collector_number
    card["collector_number_int"] = extract_collector_number_int(collector_number)
    card["illustration_id"] = card.get("illustration_id")
    # Every illustration this row SHOWS, front first. One entry here, and `_FACE_LIST_UNIONS`
    # below appends the other faces' when the faces merge, so a merged row lists the front's
    # (which is also `illustration_id`) followed by each later face's. A face carrying no art of
    # its own -- split, adventure and flip cards put one `illustration_id` on the card and none on
    # the faces -- inherits the card's here and dedupes back to one on merge.
    card["illustration_ids"] = [card["illustration_id"]] if card["illustration_id"] else []

    # Handle legalities and produced_mana defaults
    card.setdefault("card_legalities", card.get("legalities", {}))

    # Ensure all NOT NULL DEFAULT fields are set to avoid constraint violations
    for key in ["produced_mana", "card_oracle_tags", "card_art_tags", "card_is_tags"]:
        card.setdefault(key, {})

    return [card]
