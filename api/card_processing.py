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


@maybeify
def maybe_stat_int(val: str | int | float | None) -> int | None:
    """Convert a printed POWER or TOUGHNESS to int, where `*` AND `?` ARE NUMBERS AND BOTH ARE ZERO.

    `maybe_int` reads `*` as absent, and absent compares false against everything, so the whole
    `*`-statted population fell out of every power/toughness comparison: `tou<1` is 434 on
    api.scryfall.com against this engine's 273, `tou=0` 432 against 272 -- 160 cards. Scryfall's
    own `tou:*` answers the same 432 as `tou=0`; the star is not a third state there.

    The star is SUBSTITUTED, not the value replaced -- the printed arithmetic still runs. Measured
    2026-08-17, one card per form:

        Allosaurus Rider   power     1+*   matches pow=1, not pow=0
        Souls of the Lost  toughness *+1   matches tou=1
        Aysen Crusader     power     2+*   matches pow=2, and NOT pow=0

    The corpus prints six starred forms -- `*`, `1+*`, `*+1`, `2+*`, `7-*` and one `*\u00b2` --
    and this grammar covers exactly those: a term is a signed number or a star, and the terms are
    summed. A form it does not recognise raises and `maybeify` returns None, which is the
    pre-existing behaviour and the safe direction to be wrong in.

    A PRINTED `?` IS ZERO ON ITS OWN MEASUREMENT, not by analogy with the star. `Shellephant`
    (ust/121) prints `?` on both sides, and on api.scryfall.com 2026-08-17
    `!"Shellephant" tou=0` is 1, `tou>=0` is 1 and `tou>0` is 0 -- so Scryfall holds exactly 0
    for it, the same value it holds for a star. Read as ABSENT it satisfied no comparison against
    its own column at all, which is why it fell out of the range queries and not just the equality
    ones: `toughness<1` answered 433 here against Scryfall's 434, and `?` was the whole of that
    one row. The corpus prints `?` on three cards (Shellephant, `Loopy Lobster` cmb1,
    `Catch of the Day` mb2) and only Shellephant is in a set api.scryfall.com answers for at all.

    `\u221e` is deliberately NOT here: `Infinity Elemental` is `ulst`, which api.scryfall.com does
    not answer for either, so there is no measurement to follow -- the same rule that keeps
    loyalty's two starred cards on `maybe_int` below.

    Loyalty deliberately keeps `maybe_int`: the two cards printing `*` there are funny-set cards
    api.scryfall.com will not answer for at all, so there is no measurement to follow.
    """
    try:
        return int(float(val))
    except (ValueError, TypeError):
        pass
    text = str(val).strip()
    if "*" not in text and "?" not in text:
        raise ValueError(text)
    total = 0.0
    sign = 1
    for index, piece in enumerate(re.split(r"(?<=.)([+-])", text)):
        if index % 2:
            sign = -1 if piece == "-" else 1
            continue
        term = piece.strip()
        if term in {"*", "*\u00b2", "?"}:
            continue  # `*`, `*` squared and `?` are all zero
        total += sign * float(term)
    return int(total)


def rarity_text_to_int(rarity_text: str) -> int:
    """Convert rarity text to int."""
    rarity_map = {
        "common": 0,
        "uncommon": 1,
        "rare": 2,
        "special": 3,
        "mythic": 4,
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
# The printed keyword list is a LIST, so its face merge is an order-preserving union rather than a
# dict update.
_FACE_PRINTED_LIST_UNIONS = ("card_keywords_printed",)
_FACE_JOINED_TEXTS = ("oracle_text", "flavor_text", "type_line")
# Copied per GROUP from the first face that has any of the group, so the numeric columns and
# their _text twins always describe the same face (the schema's check constraints couple them).
_FACE_STAT_GROUPS = (
    ("creature_power", "creature_toughness", "creature_power_text", "creature_toughness_text"),
    ("planeswalker_loyalty", "planeswalker_loyalty_text"),
)
# Joins face texts. "\n" so substring/regex matches cannot span faces in practice (`.` does not
# cross newlines), "//" because that is the face separator Scryfall itself renders.
_FACE_TEXT_SEPARATOR = "\n//\n"

# What `card_faces` stores per face, in Scryfall's own key names and value shapes.
#
# The merged row above is what the query planner filters on; this is what a face IS. Keeping it
# structurally (rather than inside raw_card_blob) is what lets the ENGINE answer face-level
# questions: the store is the only thing an engine-path request reads, and a JSONB column is not
# in it. It also retires the merge's one documented residual — when several faces carry a stat
# group (Brutal Cathar's 2/2 // 3/3) the merged row keeps only the front's, while Scryfall matches
# either; per-face power/toughness/loyalty make the back searchable again.
#
# `object` is the constant "card_face" and `image_uris` is a pure function of the card's id and the
# face's position, so neither is stored; both are re-emitted on read.
_FACE_OBJECT_FIELDS = (
    "name",
    # The face's printed-language text, in Scryfall's own key positions (printed_name after name,
    # printed_type_line after type_line, printed_text after oracle_text). Presence varies per face
    # per printing — a prepare-layout Spanish printing localizes the front face's name and type
    # line and NOTHING else — and _face_records keeps absent keys absent, so the absence
    # round-trips exactly instead of becoming null or borrowed English.
    "printed_name",
    # Scryfall's face key order is name -> flavor_name -> mana_cost, verified live on vow/338
    # (transform) and sld/1079 (reversible_card) 2026-08-16. A printing carries the flavor name at
    # the CARD level or on its faces, never both.
    "flavor_name",
    "mana_cost",
    "type_line",
    "printed_type_line",
    "oracle_text",
    "printed_text",
    "power",
    "toughness",
    "loyalty",
    # Battles print their defense on the FACE (Invasion of Alara's front face is `defense: 7`) and
    # no column holds it, so leaving it out drops the number from every battle's card object.
    "defense",
    "colors",
    "color_indicator",
    "flavor_text",
    "artist",
    "artist_id",
    "illustration_id",
)


def _face_records(card_faces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Snapshot each face's own fields, front first.

    Args:
        card_faces: The card's raw `card_faces` array, as Scryfall sent it.

    Returns:
        One dict per face, carrying only the keys the face actually has. Absent keys stay absent
        rather than becoming null, because Scryfall omits them and a reconstructed face has to
        agree key-for-key.
    """
    return [{field: face[field] for field in _FACE_OBJECT_FIELDS if field in face} for face in card_faces]


def _fold_name(value: str | None) -> str | None:
    """Lowercase and accent-fold a name for the engine's name indexes, or None.

    The same fold `card_name_folded` and `printed_name_folded` use, factored out because
    `flavor_name_folded` is a third caller and the three must agree exactly.

    Args:
        value: The name as Scryfall sent it, or None when the key was absent.

    Returns:
        The folded name, or None when there was none to fold.
    """
    return fold_accents(value.lower()) if value else None


def _printed_name_folded(card: dict[str, Any], face_records: list[dict[str, Any]]) -> str | None:
    """The printed FULL name, folded exactly like card_name_folded, for the engine's printed-name index.

    Per-face printed_names joined " // " — each face falling back to its English name when Scryfall
    omitted printed_name there, so a prepare-layout printing whose second face has no printed name
    still forms the full "Front // Back" key. The card's own top-level printed_name when it has no
    faces. None when no printed name exists anywhere: an English printing contributes nothing to
    the printed-name index.

    Args:
        card: The card-level object (top-level printed_name lives here for single-faced cards).
        face_records: The `_face_records` snapshot, front first (empty for single-faced cards).

    Returns:
        The lowercased, accent-folded full printed name, or None.
    """
    if any("printed_name" in face for face in face_records):
        full = " // ".join(face.get("printed_name") or face.get("name") or "" for face in face_records)
    else:
        full = card.get("printed_name")
        if not full:
            return None
    return fold_accents(full.lower())


# Keys that do NOT go in card_compat_blob, because a column already holds them or they are a pure
# function of one. Kept subtractive, and mirrored in 2026-08-10-01-engine-card-objects.sql: the
# residue is "whatever is left", so a Scryfall key nobody has seen yet lands in the blob by default
# instead of being silently dropped the first time it appears.
#
# `prices` is deliberately absent from this set even though price_usd/eur/tix are columns --
# usd_foil, usd_etched and eur_foil are not, and keeping the object whole costs a few bytes against
# losing three fields.
_COMPAT_BLOB_EXCLUDED = frozenset(
    {
        # stored in a column of their own
        "id", "oracle_id", "name", "released_at", "layout", "mana_cost", "cmc", "type_line",
        "oracle_text", "power", "toughness", "loyalty", "colors", "color_identity", "keywords",
        "set", "set_name", "collector_number", "rarity", "flavor_text", "artist",
        "illustration_id", "border_color", "edhrec_rank", "legalities", "produced_mana",
        "watermark", "reserved", "game_changer", "frame",
        "printed_name", "printed_type_line", "printed_text",
        # pure functions of id / set / collector_number / oracle_id, re-emitted on read
        "object", "uri", "scryfall_uri", "image_uris", "rulings_uri", "prints_search_uri",
        "set_uri", "set_search_uri", "scryfall_set_uri", "card_back_id", "related_uris",
        "purchase_uris", "resource_id",
        # its own column
        "card_faces",
        # added by this module before the snapshot is taken
        "card_name", "face_name", "face_idx", "scryfall_id",
    },
)  # fmt: skip


def _compat_blob(card: dict[str, Any]) -> dict[str, Any]:
    """The Scryfall keys that no column holds and no derivation recovers.

    Args:
        card: The card object as Scryfall sent it, before this module's own keys matter.

    Returns:
        The residue, ready to store as card_compat_blob.
    """
    return {key: value for key, value in card.items() if key not in _COMPAT_BLOB_EXCLUDED}


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
        for key in _FACE_PRINTED_LIST_UNIONS:
            seen = merged[key]
            seen.extend(value for value in face[key] if value not in seen)
        for key in _FACE_JOINED_TEXTS:
            parts = [part for part in (merged.get(key), face.get(key)) if part]
            merged[key] = (" // " if key == "type_line" else _FACE_TEXT_SEPARATOR).join(parts)
        for group in _FACE_STAT_GROUPS:
            if all(merged.get(field) is None for field in group) and any(face.get(field) is not None for field in group):
                for field in group:
                    merged[field] = face.get(field)
    return merged


# The `layout` values whose printings Scryfall hides from a default `/cards/search`, plus the two
# non-layout signals that do the same job. Scryfall's own `is:extra` is 6,054 cards; this reaches
# 5,873 distinct English cards, within 3% of it — and, unlike a playability filter, it is a
# statement about the PRINTING, which is what the hiding actually tracks.
#
# `host` AND `augment` WERE HERE AND ARE WRONG. Unstable's Hosts and Augments are ORDINARY search
# results: `is:extra e:ust` answers 0 on api.scryfall.com while this set counted 32, and bare
# `e:ust` answers Unstable's full English count either way. The two layouts are unusual card FACES,
# not printings Scryfall hides — which is the distinction this set is supposed to draw. Measured
# 2026-08-16; 46 printings across ust/und/ulst stop carrying `is:extra`.
_EXTRA_LAYOUTS = frozenset({"token", "double_faced_token", "emblem", "planar", "scheme", "vanguard", "art_series", "front_card"})

# The `funny` sets Scryfall hides behind `include_extras`. Every OTHER funny set is served
# ordinarily, and both halves are total: measured on api.scryfall.com 2026-08-16, all 22 funny sets
# in the corpus answer `is:extra e:<code>` with either their whole card count or zero — never
# anything in between.
#
#   whole set extra   cmb1 121  cmb2 121  h17 4  hho 21  ph17 3  ph18 4  ph19 7  ph20 3  ph21 4
#                     ph22 5  ph23 2  phtr 3  punk 52  ulst 62  unk 512
#   whole set served  ptg 0  sunf 0  ugl 0  und 0  unf 0  unh 0  ust 0
#
# NOTHING ON THE PRINTING PREDICTS THE SPLIT, and that is a measurement rather than a shrug. The
# ulst rows (The List's Unstable reprints) were diffed field by field against their own ust twins
# over the whole 2026-08-16 bulk: of the 40-odd keys, the only values ulst holds that no
# ust/und/unh/unf/ugl row holds are `highres_image: false` and `image_status: "lowres"` — scan
# quality, and not even uniform across ulst. `set_type`, `border_color` (silver both), `layout`,
# `security_stamp`, `promo_types`, `frame_effects`, `games`, `finishes`, `booster`, `reprint`,
# `content_warning`, `legalities` (never legal both) all overlap. Widening the comparison to the
# two GROUPS — 927 printings across the 15 extra sets against 1,310 across the 7 served ones —
# found no field whose value set separates them either.
#
# The SET objects do not predict it: `foil_only` is true for both h17 (extra) and ptg (served),
# `parent_set_code` is set on both punk (extra) and sunf (served), `tcgplayer_id` is set on both
# unk (extra) and ptg (served), and `card_count`/`printed_size`/`digital`/`block` split neither
# way. So it is editorial data in Scryfall's own database, and a list is the only faithful port.
#
# SPELLED AS THE EXTRA SIDE ON PURPOSE. A funny set this list has never heard of is served
# ORDINARILY, so the failure mode of a stale list is a handful of employee-award or convention
# cards leaking into search — not a 639-card retail Un-set vanishing from it, which is what
# defaulting the other way would risk the first time Wizards prints another one.
_FUNNY_EXTRA_SETS = frozenset(
    {"cmb1", "cmb2", "h17", "hho", "ph17", "ph18", "ph19", "ph20", "ph21", "ph22", "ph23", "phtr", "punk", "ulst", "unk"}
)


# The `is:` value the extras class is stored under. Read by `api_resource`'s default
# `include_extras=false` conjunct and by the compat route's auto-enable walk; spelled once more in
# card_engine's `EXTRA_IS_TAG`, which folds `sets_with_extras` from it. Those two must agree or the
# fold silently comes back empty.
EXTRA_IS_TAG = "extra"


def _is_extra(card: dict[str, Any]) -> bool:
    """Whether Scryfall hides this printing from a default `/cards/search` — the `is:extra` class.

    See `preprocess_card` for the per-class probe that decided each half of this.

    MEASURED COVERAGE (2026-08-16, the 114,068 English printings of the all_cards bulk against
    api.scryfall.com's own `is:extra`, 10,818 printings): this class reaches 10,732 — 45 short and
    none over. The 45 are Arena-only duplicate printings with no signal on them at all (hbg 18,
    j21 16, ydmu 9, ybro 1) plus one Secret Lair poster; the same field-by-field diff that cleared
    `_FUNNY_EXTRA_SETS` finds nothing separating them from their own set-mates either, so they are
    left rather than enumerated one id at a time. Before the funny/digital/silver-promo/Stickers
    clauses it reached 10,482 with 308 misses and 2 false positives.

    Args:
        card: The bulk card object.

    Returns:
        True when the printing should carry `is:extra`.
    """
    never_legal = not set(card["legalities"].values()) & {"legal", "restricted"}
    # A FUNNY SET DECIDES FOR ITS PRINTINGS — see `_FUNNY_EXTRA_SETS` for the measurement and for
    # why no printing field can stand in for the list.
    #
    # A funny set the list names is extra outright. A funny set it does NOT name still falls
    # through to the layout, memorabilia, content-warning and "Card"/"Token" clauses below, and
    # skips only the two that measurably misfire inside the un-sets: `und`/`unh` carry a `playtest`
    # promo each ("Look at Me, I'm R&D", a real Un-card that merely DEPICTS a playtest card) and
    # `sunf` ships 48 sticker sheets, and all three sets answer `is:extra` 0 on api.scryfall.com.
    #
    # FALLING THROUGH RATHER THAN RETURNING FALSE IS THE POINT. An early False would let a future
    # funny set's tokens, planes or vanguards vanish from search the moment the list went stale --
    # the silent-vanishing failure this list's polarity was chosen to avoid, reintroduced one level
    # down. It costs nothing today: of the 57 funny printings another clause would call extra
    # (punk 52 planar, cmb1/cmb2 1 vanguard each, hho/h17 1 token each) every one is already in a
    # listed set, so the two rules agree wherever they overlap.
    funny = card.get("set_type") == "funny"
    if funny and card.get("set") in _FUNNY_EXTRA_SETS:
        return True
    # A DIGITAL PRINTING NO FORMAT ALLOWS. Arena's Alchemy duplicates and the Astral cards from the
    # 1997 MicroProse game are legal nowhere and served nowhere: 117 printings across hbg (104),
    # past (12) and prm (1), every one of them inside Scryfall's `is:extra` and not one outside it,
    # over the whole English corpus. Digital and never-legal are each ordinary on their own —
    # Alchemy's playable cards are legal in alchemy/historic, and paper's never-legal conspiracies
    # are ordinary results — so it is the conjunction that carries the class.
    if card.get("digital") is True and never_legal:
        return True
    # A SILVER-BORDERED PROMO: an Un-card handed out outside its own set (Arena League, judge
    # gifts, prerelease stamps). 10 printings across pal04/j17/p30m/punh/pust, all extras, no false
    # positive — silver alone is not the class (567 silver printings are ordinary), and neither is
    # `promo`.
    if card.get("border_color") == "silver" and card.get("set_type") == "promo":
        return True
    if card.get("layout") in _EXTRA_LAYOUTS:
        return True
    if card.get("set_type") == "memorabilia":
        return True
    # `content_warning` — the flag Scryfall sets on the printings it will not show unasked. It is an
    # EXTRAS signal and nothing else here catches it: 91 printings across the bulk (25 English), all
    # layout `normal`, all ordinary type lines, all legal somewhere. Missing it made nine sets look
    # extras-free that Scryfall auto-enables `include_extras` for — lea's only extra IS a
    # content-warning card (Crusade), and 2ed/3ed/4ed/5ed/6ed/sum/leg/arn/ddf/me1/me3/ced/cei/prm
    # are the same story. Measured 2026-08-16: `is:extra e:lea` answers 1.
    if card.get("content_warning") is True:
        return True
    # A "Card"/"Token"/"Stickers" TYPE LINE, for the printings whose layout does not already say
    # so: the checklist and substitute-card family ships as layout `normal` in some sets, and the
    # Secret Lair sticker sheets (sld/335-339) ship as an ordinary `normal` box-set printing whose
    # only tell is the type. `Stickers` is guarded on `funny` because `sunf` ships 48 sticker
    # sheets that Scryfall serves; `Card`/`Token` need no guard, and deliberately do not have one.
    type_line = card.get("type_line")
    if type_line:
        card_types, _ = parse_type_line(type_line)
        if any(t == "Card" or t == "Token" or (t == "Stickers" and not funny) for t in card_types):  # noqa: PLR1714
            return True
    # A playtest promo, EXCEPT where the printing is otherwise playable: sld/SCTLR Counterspell
    # carries `playtest`, is legal in modern, and is returned by a bare
    # `!"Counterspell"&unique=prints` — so the flag alone hides nothing.
    return "playtest" in card.get("promo_types", []) and never_legal and not funny


def preprocess_card(card: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: PLR0915,C901,PLR0912
    """Preprocess a card to remove invalid cards and add necessary fields.

    A multi-face card (transform, MDFC, split, adventure, flip) is merged into ONE row
    carrying the front face's identity and every face's searchable data — see
    `_merge_processed_faces`. Single-faced cards return a list with one dictionary.
    Returns an empty list for invalid/filtered cards.
    """
    # NOTHING IS FILTERED OUT HERE ANY MORE, and the seven clauses that used to be are the
    # `is:extra` predicate `_is_extra` answers instead.
    #
    # Every class this function refused is SERVED by api.scryfall.com, and four of them are hidden
    # from a default `/cards/search` behind `include_extras=false` — a QUERY-TIME gate. Refusing
    # the row cannot reproduce a query-time gate in either direction: `/cards/named?exact=`
    # answered 200 for every single one of them, and `include_extras=true` had nothing to include.
    # Probed one class at a time on 2026-08-16 (`q=!"<name>"` bare, then with the flag):
    #
    #   never-legal    !"Hold the Perimeter" (cn2/6)      200 bare       ORDINARY
    #   funny          !"Bamboozling Beeble" (unf/37)     200 bare       ORDINARY
    #                  !"Goblin Bowling Team" (ugl/44)    200 bare       ORDINARY (silver, never-legal)
    #   "X // X"       !"Magmatic Hellkite // …" (tdm)    200 bare       ORDINARY (reversible_card)
    #   playtest+legal sld/SCTLR Counterspell             in bare prints ORDINARY
    #   memorabilia    !"Siren's Call"&unique=prints      8 bare, 12 with extras  EXTRA
    #   type "Card"    !"The Monarch" (tmkc/31)           404 bare, 200 with      EXTRA
    #   type "Token"   !"Goblin Army" (thob/4)            404 bare, 200 with      EXTRA
    #   planar         !"Truga Jungle" (opc2/38)          404 bare, 200 with      EXTRA
    #   playtest       !"Subgoyf" (mb2/536)               404 bare, 200 with      EXTRA
    #
    # The `games`-without-paper clause went the same way one commit earlier, for the stronger
    # reason that nothing hides those at all: `q=!"A-Tyvar Kell"` answers khm/A-198 bare.
    #
    # +13,619 printings on the 2026-08-16 all_cards bulk (526,865 -> 540,484, +2.58%) and 2,986
    # new oracle cards. It also empties the annex-only drop the engine carries: the three ja-4ED
    # ante printings whose `oldschool: legal` left their oracle group with no canonical row now
    # arrive as ordinary cards.
    #
    # DIVERGENCE FROM UPSTREAM'S IMPORT POLICY, ON PURPOSE — see the PR description. Upstream's
    # corpus is the one its own SQL serves; this port's has to answer as Scryfall does.
    # `is:extra` is a COMPUTED tag: no Scryfall field says "this printing is an extra", so it
    # cannot ride BOOLEAN_IS_TAGS' one-shot sync from raw_card_blob (api_resource.py) the way
    # `reserved` and `gamechanger` do. It is set here, on the row, and the merge below preserves
    # it through the NOT NULL default.
    if _is_extra(card):
        card["card_is_tags"] = {**card.get("card_is_tags", {}), EXTRA_IS_TAG: True}

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
        # The engine's copy of the same thing. raw_card_blob is a Postgres column and the SQL path
        # is a fallback, so anything only the blob carries is unanswerable on the engine path.
        merged_row["card_faces"] = _face_records(card_faces)
        merged_row["card_compat_blob"] = _compat_blob(card)
        # The printed-language COLUMNS are the card-level triple. The per-face recursion above
        # merged each face's own printed keys into its row, so without this the front face's
        # printed_name would masquerade as the card's — the per-face halves ride card_faces.
        # ...and so is the ARTIST. A card drawn by two people carries the JOINED credit at card
        # level ("David Martin & Franz Vohwinkel" on Fire // Ice, dmr/215) and the per-face credit
        # inside card_faces, so the same face overlay put face 0's artist on the merged row and
        # `card_artist` came out "David Martin". 1,158 printings in the 2026-08-16 default_cards
        # bulk carry a two-artist credit, and three surfaces read this one column: the card
        # object's `artist`, `order=artist` (api.scryfall.com sorts Fire // Ice AFTER all six
        # plain "David Martin" dmr cards, i.e. on the joined string), and `a:` (`a:"franz
        # vohwinkel"` returns it there — artist search covers non-front faces).
        #
        # Scryfall's own string, never a join recomputed over the faces: that is what keeps the
        # 4,951 multi-faced cards whose faces share one artist on a single name rather than
        # "X & X", and the string is not re-splittable afterwards either — "Hari & Deepti" is ONE
        # artist of ten printings.
        merged_row["card_artist"] = card.get("artist")
        # ...and so is the LAYOUT, which is the one other key a FACE can genuinely carry. Scryfall
        # puts `layout` on the faces of reversible cards and on nothing else: over the whole
        # 2026-08-15 all_cards bulk exactly 81 cards have a face-level `layout`, and all 81 are
        # `reversible_card`. The overlay above put the face's value on every face row, so
        # `card_layout` came out of the FRONT face -- 77 of the 81 stored as `normal`, 3 as
        # `adventure`, 1 as `token` -- and `layout:reversible_card` answered nothing at all.
        if "layout" in card:
            merged_row["card_layout"] = card["layout"].lower()
        merged_row["printed_name"] = card.get("printed_name")
        merged_row["flavor_name"] = card.get("flavor_name")
        merged_row["printed_type_line"] = card.get("printed_type_line")
        merged_row["printed_text"] = card.get("printed_text")
        merged_row["printed_name_folded"] = _printed_name_folded(card, merged_row["card_faces"])
        merged_row["flavor_name_folded"] = _fold_name(merged_row["flavor_name"])
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
    card["card_compat_blob"] = _compat_blob(raw_card_data)
    card["scryfall_id"] = card["id"]

    card_types, card_subtypes = parse_type_line(card["type_line"])
    card["card_types"] = card_types
    card["card_subtypes"] = card_subtypes

    card["planeswalker_loyalty"] = maybe_int(card.get("loyalty"))
    if "Creature" in card_types or {"Vehicle", "Spacecraft"} & set(card_subtypes):
        # `maybe_stat_int`, not `maybe_int`: a printed `*` is ZERO on both sides of a
        # power/toughness comparison -- see its docstring for the three cards that pin the
        # arithmetic. The printed strings two lines down are untouched.
        card["creature_power"] = maybe_stat_int(card.get("power"))
        card["creature_toughness"] = maybe_stat_int(card.get("toughness"))
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
    # ...and again AS PRINTED, for the card object. The fold above is not invertible: only 455 of
    # the 885 distinct keywords in the 2026-08-16 all_cards bulk come back from capitalizing the
    # folded form ("Battle Cry", "AV Bead", "Bio-plasmic Barrage" do not), and Scryfall's order is
    # neither the folded dict's nor alphabetical ("Flying" before "Flash" on Brazen Borrower). A
    # LIST, not a {key: true} object, because it exists for its order. `keyword:` keeps binding the
    # folded keys; this one is only ever emitted.
    card["card_keywords_printed"] = list(dict.fromkeys(card.get("keywords", [])))
    card["produced_mana"] = dict.fromkeys(card.get("produced_mana", []), True)
    # Scryfall's TOP-LEVEL color_indicator -- the printed colour dot a card whose mana cost cannot
    # state its colours carries (a meld result, a coloured back). 546 printings in the bulk have
    # one and no column held it, so the card object emitted it on none of them. Not face-merged:
    # the two-image layouts keep theirs on the faces and send no top-level copy at all.
    card["color_indicator"] = dict.fromkeys(card.get("color_indicator", []), True)

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
    # The printing's language ("en", "ja", ...; Scryfall sends it lowercase already, lowered here
    # to make the invariant explicit like the columns above). `lang` itself deliberately stays in
    # card_compat_blob — the card object reads it from there — while card_lang is the search column.
    if "lang" in card:
        card["card_lang"] = card["lang"].lower()
    # The printed-language triple, verbatim. Explicit None (not absent) when Scryfall omitted the
    # key, so an upsert overrides any stale value instead of keeping it, same reasoning as the
    # creature stat columns above.
    card["printed_name"] = card.get("printed_name")
    # Scryfall's `flavor_name`: the alternate name a printing is SOLD under (the Godzilla series,
    # Stranger Things, the Secret Lair crossovers). PRINTING-level and quite separate from the
    # printed triple — a printing may carry both — and `_folded` is the name-lookup key that makes
    # `/cards/named?exact=Godzilla, Primeval Champion` resolve prm/80925, which it does on
    # api.scryfall.com and did not here. The FACE-level variant rides card_faces.
    card["flavor_name"] = card.get("flavor_name")
    card["printed_type_line"] = card.get("printed_type_line")
    card["printed_text"] = card.get("printed_text")
    card["printed_name_folded"] = _printed_name_folded(card, [])
    card["flavor_name_folded"] = _fold_name(card["flavor_name"])

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
    for key in ["produced_mana", "color_indicator", "card_oracle_tags", "card_art_tags", "card_is_tags"]:
        card.setdefault(key, {})
    card.setdefault("card_keywords_printed", [])

    return [card]
