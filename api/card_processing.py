"""Card processing functions."""

from __future__ import annotations

import copy
import functools
import re
from typing import TYPE_CHECKING, Any

from api.parsing.card_query_nodes import calculate_devotion, fold_accents, mana_cost_str_to_dict

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


# ── Role classes: who can lead a deck, and what is cast ──────────────────────
# `is:commander`, `is:brawler`, `is:duelcommander`, `is:oathbreaker` and `is:spell` are questions
# about ONE face -- the one you cast -- and about the printing's own legalities. They are decided
# here, from the card as Scryfall sends it, because this is the only place that still holds the
# faces: a faced card is stored as a row per face under one scryfall_id, so a query predicate sees
# whichever face the row kept and can never ask "is the FRONT a legendary creature". See
# `role_classes` for the rules; BOOLEAN_IS_TAGS reads the result back off raw_card_blob.
ROLE_CLASSES_KEY = "role_classes"

# Layouts that are not a card anyone casts or chooses to lead a deck.
_NOT_A_CARD_LAYOUTS = frozenset({"token", "double_faced_token", "emblem", "planar", "scheme", "vanguard", "art_series"})

# Layouts whose every face can be cast: both halves of a split, an adventure or a prepare card, and
# either side of a modal DFC. On every other faced layout (transform, flip, reversible) only the
# front is cast; the other face is turned to.
_EVERY_FACE_CAST_LAYOUTS = frozenset({"split", "adventure", "prepare", "modal_dfc"})

# Card types a spell can have, and the ones nothing is cast as. A face with a spell type is a spell
# even beside a never-cast word (Theros's `Hero Artifact -- Equipment`); a word in neither set
# counts as castable, which is Scryfall's reading of `Summon` and `Eaturecray` too. Title-cased to
# match parse_type_line().
_SPELL_TYPES = frozenset(
    {"Artifact", "Battle", "Creature", "Enchantment", "Instant", "Kindred", "Tribal", "Planeswalker", "Sorcery"}
)
_NEVER_CAST_TYPES = frozenset(
    {"Card", "Plane", "Phenomenon", "Scheme", "Vanguard", "Conspiracy", "Emblem", "Dungeon", "Hero", "Event", "Boss", "Stickers"}
)
# Artifact subtypes that are never cast: Attractions are visited, Contraptions assembled.
_NEVER_CAST_SUBTYPES = frozenset({"Attraction", "Contraption"})


def _front_faces(card: dict[str, Any]) -> list[dict[str, Any]]:
    """The faces that decide who can lead a deck: face 0, or every half of a split card.

    Face 0 rather than the card's own keys, because a faced card's top-level `type_line` is the
    joined one ("Creature -- Human Monk // Legendary Creature -- Human Monk") and a reversible
    card carries no top-level type line at all.
    """
    faces = card.get("card_faces") or []
    if not faces:
        return [card]
    return faces if card.get("layout") == "split" else faces[:1]


def _cast_faces(card: dict[str, Any]) -> list[dict[str, Any]]:
    """The faces that can be cast: all of them on an _EVERY_FACE_CAST_LAYOUTS card, else the front."""
    faces = card.get("card_faces") or []
    if faces and card.get("layout") in _EVERY_FACE_CAST_LAYOUTS:
        return faces
    return _front_faces(card)


def _is_meld_result(card: dict[str, Any]) -> bool:
    """Whether this card is the RESULT of a meld -- the same rule BOOLEAN_IS_TAGS' `meldresult` reads.

    The role is the `component` of the card's own `all_parts` entry, found by id, or by the card's
    own name when a reprint's `all_parts` lists a sibling printing's ids.
    """
    parts = card.get("all_parts") or []
    own = next((part for part in parts if part.get("id") == card.get("id")), None)
    if own is not None:
        return own.get("component") == "meld_result"
    return any(part.get("name") == card.get("name") and part.get("component") == "meld_result" for part in parts)


def _is_spell_face(face: dict[str, Any]) -> bool:
    """Whether one castable face is a spell: not a land or token, and not a never-cast type."""
    card_types, card_subtypes = parse_type_line(face.get("type_line") or "")
    types = set(card_types)
    if not types or types & {"Land", "Token"}:
        return False
    if not types & _SPELL_TYPES and types & _NEVER_CAST_TYPES:
        return False
    return not _NEVER_CAST_SUBTYPES & set(card_subtypes)


def _creature_outside_the_battlefield(oracle_text: str) -> bool:
    """Grist, the Hunger Tide: "As long as Grist isn't on the battlefield, it's a 1/1 Insect creature"."""
    text = oracle_text.lower().replace("\u2019", "'")
    return any("isn't on the battlefield, it's a" in sentence and "creature" in sentence for sentence in re.split(r"[.\n]", text))


def role_classes(card: dict[str, Any]) -> list[str]:
    """The role classes (`commander`, `brawler`, `duelcommander`, `oathbreaker`, `spell`) of a raw card.

    Takes the card as Scryfall sends it, `card_faces` and all. Each rule was fitted to
    api.scryfall.com's own answer card for card (see docs/issues/00985-is-tag-remaining-coverage.md
    for the measurements):

    - `spell`: some castable face (`_cast_faces`) is a spell (`_is_spell_face`). A meld result is a
      spell there, so this does not ask about melds.
    - The four deck-leading classes read the FRONT face (`_front_faces`) and are never a meld
      result:
        - a legendary Creature, a legendary card that is a creature outside the battlefield
          (Grist), or any card whose text says it "can be your commander";
        - for `commander` and `brawler`, also a legendary card with a printed toughness (Vehicles,
          Spacecraft) or a Background -- `duelcommander` counts neither;
        - for `brawler`, also a legendary Planeswalker;
        - `oathbreaker` is its own shape: the front face is a Planeswalker.
    - Legality comes from the printing's own `legalities`: `commander` is anything not banned in
      Commander; `brawler` is legal in Brawl and not banned in `competitivebrawl`; `duelcommander`
      is LEGAL in Duel, where `restricted` is how Scryfall writes "banned as commander";
      `oathbreaker` is legal in Oathbreaker.
    """
    if card.get("layout") in _NOT_A_CARD_LAYOUTS:
        return []
    classes = []
    if any(_is_spell_face(face) for face in _cast_faces(card)):
        classes.append("spell")
    if _is_meld_result(card):
        return classes

    creature = other_permanent = legendary_walker = walker = False
    for face in _front_faces(card):
        card_types, card_subtypes = parse_type_line(face.get("type_line") or "")
        if "Token" in card_types:
            continue
        oracle_text = face.get("oracle_text") or ""
        legendary = "Legendary" in card_types
        walker |= "Planeswalker" in card_types
        legendary_walker |= legendary and "Planeswalker" in card_types
        creature |= "can be your commander" in oracle_text.lower() or (
            legendary and ("Creature" in card_types or _creature_outside_the_battlefield(oracle_text))
        )
        other_permanent |= legendary and (face.get("toughness") is not None or "Background" in card_subtypes)

    legalities = card.get("legalities") or {}
    if (creature or other_permanent) and legalities.get("commander") != "banned":
        classes.append("commander")
    if (
        (creature or other_permanent or legendary_walker)
        and legalities.get("brawl") == "legal"
        and legalities.get("competitivebrawl") != "banned"
    ):
        classes.append("brawler")
    if creature and legalities.get("duel") == "legal":
        classes.append("duelcommander")
    if walker and legalities.get("oathbreaker") == "legal":
        classes.append("oathbreaker")
    return classes


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


def preprocess_card(card: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: PLR0915,C901,PLR0912
    """Preprocess a card to remove invalid cards and add necessary fields.

    For Double-Faced Cards (DFCs), returns multiple dictionaries (one per face).
    For single-faced cards, returns a list with one dictionary.
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
        # Decided here, while the card still has all its faces, and carried onto every face's
        # raw_card_blob by the merge below -- so whichever face row survives the upsert, it holds
        # the CARD's answer. Omitted when empty, which leaves a land's blob as Scryfall sent it.
        if classes := role_classes(card):
            card[ROLE_CLASSES_KEY] = classes
    else:
        # Recursive case: processing a face
        card["face_name"] = card.get("name")

    # Handle cards with card_faces (DFCs)
    card_faces = card.get("card_faces")
    if card_faces:
        for creature_attribute in ["creature_power", "creature_toughness"]:
            card.pop(creature_attribute, None)
            card.pop(f"{creature_attribute}_text", None)
        processed_faces = []
        for face_idx, face_data in enumerate(card_faces, start=1):
            # Merge card-level data with face-specific data
            # Precedence: face_idx override > face_data (name, type_line, etc.) > card (legalities, games, etc.)
            merged = copy.deepcopy(card) | face_data | {"face_idx": face_idx}
            merged.pop("card_faces", None)  # Don't keep recursing
            processed_faces_for_face = preprocess_card(merged)
            processed_faces.extend(processed_faces_for_face)
        return processed_faces

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

    # Handle legalities and produced_mana defaults
    card.setdefault("card_legalities", card.get("legalities", {}))

    # Ensure all NOT NULL DEFAULT fields are set to avoid constraint violations
    for key in ["produced_mana", "card_oracle_tags", "card_art_tags", "card_is_tags"]:
        card.setdefault(key, {})

    return [card]
