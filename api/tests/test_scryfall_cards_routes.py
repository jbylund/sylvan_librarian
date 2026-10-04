"""Integration tests for the Scryfall-compatible /cards/* routes.

Everything here dispatches through `_handle`, the same path a real request takes: route resolution,
parameter binding, and the response object the client would receive. The corpus is a handful of
cards this module inserts under names no other test uses, because the postgres container is shared
across the session.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import falcon
import falcon.testing
import orjson
import pytest
from cachebox import LRUCache

from api.enums import CardOrdering, SortDirection, UniqueOn, resolve_direction
from api.parsing import AttributeNode, BinaryOperatorNode, NotNode, OrNode, StringValueNode, parse_scryfall_query
from api.parsing.query_budget import QUERY_REGEX_REJECTED_MESSAGE
from api.scryfall_compat import routes as routes_module
from api.scryfall_compat.objects import MAX_COLLECTION_IDENTIFIERS, PAGE_SIZE
from api.scryfall_compat.routes import _csv_cell, _csv_mana_cost, _csv_price
from api.settings import settings
from api.tests.helpers import make_raw_card
from api.utils.generation_cache import GenerationCache

if TYPE_CHECKING:
    from api.api_resource import APIResource

# Set code and names owned by this module alone, so assertions can be exact against a shared
# database. "sfc" is not a real Scryfall set code.
SET_CODE = "sfc"
BOLT_ID = "11111111-1111-4111-8111-111111111111"
BEAR_ID = "22222222-2222-4222-8222-222222222222"
DELVER_ID = "33333333-3333-4333-8333-333333333333"
BOLT_ORACLE_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
EXTRA_ID = "55555555-5555-4555-8555-555555555555"
# The extra's oracle id, pinned so `oracleid:` can name it. Its ONLY printing is the extra, which is
# Mechtitan's shape on api.scryfall.com: tneo/14 and sld/1969, both `is:extra`.
EXTRA_ORACLE_ID = "5a5a5a5a-5555-4555-8555-5a5a5a5a5a5a"
# A well-formed v4 id no card carries as its oracle id: `oracleid:` fires on the TERM, so this
# triggers too.
UNKNOWN_V4_ID = "fefefefe-0000-4000-8000-fefefefefefe"
# A SET OF ITS OWN, on purpose. `e:`/`s:` is the CONDITIONAL `include_extras` trigger -- a set term
# turns extras on iff that set holds one -- so putting the extra in SET_CODE would auto-enable
# every `s:sfc` query in this module and quietly change what the paging tests page over.
EXTRAS_SET_CODE = "sfe"

# The by-name key rule wants two more shapes -- a name with FIVE halves and a punctuated, accented
# one -- and they live in a set of their own so the `s:sfc` paging tests keep asserting three cards.
NAME_SET_CODE = "sfn"
WHO_ID = "77777777-7777-4777-8777-777777777777"
VAULT_ID = "66666666-6666-4666-8666-666666666666"
# 50 bytes, and deliberately short: the engine stores a card's folded name in an `InlineStr<61>`
# and TRUNCATES anything longer, so a five-part name spelled "Compat ..." five times (70 bytes) is
# unreachable through the engine while the SQL fallback still finds it. That is a pre-existing bug
# in its own right -- 36 names in the real corpus are over the limit -- and not the one under test
# here, so this name stays inside the bound rather than pinning the wrong divergence.
WHO_NAME = "Cw Who // Cw What // Cw When // Cw Where // Cw Why"
VAULT_NAME = "Compat Lim-Dûl's Vault"

# The fuzzy containment pool wants a card, its EMBLEM and a TOKEN sharing its words, in a set of
# their own so nothing else here counts them.
POOL_SET_CODE = "sfp"
POOL_EMBLEM_ID = "13131313-1313-4313-8313-131313131313"
POOL_TOKEN_ID = "14141414-1414-4414-8414-141414141414"
POOL_ART_SERIES_ID = "15151515-1515-4515-8515-151515151515"
WARDEN_NAME = "Hollowmere Warden"
# Shaped like "Minion of the Mighty // Kobold": the real card's name plus a word only the art series
# carries.
WARDEN_ART_SERIES_NAME = f"{WARDEN_NAME} // Wisp"


def _pool_card(card_id: str, number: str, name: str, layout: str, type_line: str) -> dict:
    """One card of the containment-pool trio, its class carried by `layout`.

    The emblem and the token are `is:extra` printings, so `is:extra` lists them alongside
    `_extra()`'s; what the containment pool reads is the layout.
    """
    card = make_raw_card(card_id=card_id, name=name)
    card |= {
        "object": "card",
        "set": POOL_SET_CODE,
        "set_name": "Scryfall Compat Pool",
        "collector_number": number,
        "layout": layout,
        "type_line": type_line,
        "oracle_text": "",
        "lang": "en",
    }
    return card


def _pool_art_series() -> dict:
    """The pool's ART-SERIES card, two faces like every real one."""
    card = _pool_card(POOL_ART_SERIES_ID, "4", WARDEN_ART_SERIES_NAME, "art_series", "Card // Card")
    card["card_faces"] = [
        {"object": "card_face", "name": name, "mana_cost": "", "type_line": "Card", "oracle_text": ""}
        for name in WARDEN_ART_SERIES_NAME.split(" // ")
    ]
    return card


# The containment TIERS want English names and foreign printed names sharing words, in a set of
# their own so nothing else here counts them. Modelled on the needles measured on api.scryfall.com
# 2026-09-25: `velmora` is `austere` (an English name and another card's French name carry it),
# `tveilan` is `inganno` (a WHOLE Italian name that an English name contains across a space), and
# `bound goad` is `red goad` (one word from the English name, one spanning the Portuguese name's
# separators, and no English name carrying both).
TIER_SET_CODE = "sft"
TIER_SENTINEL_ID = "16161616-1616-4616-8616-161616161616"
TIER_ANNOUNCER_ID = "17171717-1717-4717-8717-171717171717"
TIER_GUILE_IT_ID = "18181818-1818-4818-8818-181818181818"
TIER_EGO_PT_ID = "19191919-1919-4919-8919-191919191919"
TIER_EGO_ES_ID = "1a1a1a1a-1a1a-4a1a-8a1a-1a1a1a1a1a1a"
TIER_FOREIGN_IDS = (
    "1b1b1b1b-1b1b-4b1b-8b1b-1b1b1b1b1b1b",
    TIER_GUILE_IT_ID,
    TIER_EGO_PT_ID,
    TIER_EGO_ES_ID,
)


def _tier_cards() -> list[dict]:
    """Three English cards and four foreign printings of two of them.

    The foreign rows share their English card's oracle id and are made NON-canonical after the
    upsert, so the engine stores them in its annex, as an import does.
    """
    rows = [
        # (id, English name, oracle group, collector number, lang, printed name)
        (TIER_SENTINEL_ID, "Velmora Sentinel", 1, "1", "en", None),
        ("1c1c1c1c-1c1c-4c1c-8c1c-1c1c1c1c1c1c", "Grim Harbinger", 2, "2", "en", None),
        (TIER_FOREIGN_IDS[0], "Grim Harbinger", 2, "2", "fr", "Héraut de Velmora"),
        (TIER_ANNOUNCER_ID, "Glintveil Announcer", 3, "3", "en", None),
        ("1d1d1d1d-1d1d-4d1d-8d1d-1d1d1d1d1d1d", "Quiet Guile", 4, "4", "en", None),
        (TIER_GUILE_IT_ID, "Quiet Guile", 4, "4", "it", "Tveilan"),
        ("1e1e1e1e-1e1e-4e1e-8e1e-1e1e1e1e1e1e", "Moorbound Ego", 5, "5", "en", None),
        (TIER_EGO_PT_ID, "Moorbound Ego", 5, "5", "pt", "Ego à Deriva"),
        (TIER_EGO_ES_ID, "Moorbound Ego", 5, "5", "es", "Ego a la deriva"),
    ]
    cards = []
    for card_id, name, group, number, lang, printed_name in rows:
        card = make_raw_card(card_id=card_id, name=name)
        card |= {
            "object": "card",
            "oracle_id": f"2f2f2f2f-2f2f-4f2f-8f2f-00000000000{group}",
            "set": TIER_SET_CODE,
            "set_name": "Scryfall Compat Tiers",
            "collector_number": number,
            "type_line": "Creature — Human",
            "oracle_text": "",
            "lang": lang,
        }
        if printed_name:
            card["printed_name"] = printed_name
        cards.append(card)
    return cards


# FLAVOR NAMES want cards whose printings carry them, beside oracle names sharing their words, in a
# set of their own so nothing else here counts them. Modelled on the needles measured on
# api.scryfall.com 2026-09-25: `vexgrit` is `assaultron` (an oracle name and another card's flavor
# name), `spindlecap` is `cordyceps` (two cards' flavor names), `hypersonic` is `supersonic` (a
# word only a flavor name carries, answering the printing that carries it), `ballista vexgrit` is
# `ballista assaultron` (the words pooled across the flavor name and the oracle name), `cinder
# hollow` is `bag end` (a whole flavor name beating an oracle name that contains it), `nuncheon`
# is `breakfast` (a token's flavor name, which containment does not read), and `gearlord` is
# `megatron`'s faces.
FLAVOR_SET_CODE = "sfv"
FLAVOR_INVADER_ID = "21212121-2121-4121-8121-212121212121"
FLAVOR_QUEEN_ID = "22222222-2121-4121-8121-212121212121"
FLAVOR_QUEEN_DEFAULT_ID = "23232323-2121-4121-8121-212121212121"
FLAVOR_GLADE_ID = "24242424-2121-4121-8121-212121212121"
FLAVOR_NUNCHEON_ID = "25252525-2121-4121-8121-212121212121"
FLAVOR_TOKEN_ID = "26262626-2121-4121-8121-212121212121"
FLAVOR_COLOSSUS_ID = "27272727-2121-4121-8121-212121212121"
# The printings a card's DEFAULT printing outscores, so an answer carrying a flavor name is that
# printing by the flavor rule and not by score.
FLAVOR_PRINTING_IDS = (FLAVOR_INVADER_ID, FLAVOR_QUEEN_ID)


def _flavor_cards() -> list[dict]:
    """Oracle names and flavor names sharing words, one card with its flavor name on its faces."""
    rows = [
        # (id, oracle name, oracle group, collector number, layout, type line, flavor name)
        ("28282828-2121-4121-8121-212121212121", "Vexgrit Dominator", 1, "1", "normal", "Artifact Creature — Robot", None),
        ("29292929-2121-4121-8121-212121212121", "Rattle Ballista", 2, "2", "normal", "Artifact Creature — Construct", None),
        (FLAVOR_INVADER_ID, "Rattle Ballista", 2, "3", "normal", "Artifact Creature — Construct", "Vexgrit Invader"),
        (FLAVOR_QUEEN_DEFAULT_ID, "Quellmoth Beacon", 3, "4", "normal", "Creature — Insect", None),
        (FLAVOR_QUEEN_ID, "Quellmoth Beacon", 3, "5", "normal", "Creature — Insect", "Zorvath, Hypersonic Queen"),
        ("2a2a2a2a-2121-4121-8121-212121212121", "Brine Ritual", 4, "6", "normal", "Instant", "Spindlecap Excision"),
        (
            "2b2b2b2b-2121-4121-8121-212121212121",
            "Thornback Mycoloth",
            5,
            "7",
            "normal",
            "Creature — Fungus",
            "Spindlecap Rat King",
        ),
        (FLAVOR_GLADE_ID, "Horizon Glade", 6, "8", "normal", "Land", "Cinder Hollow"),
        ("2c2c2c2c-2121-4121-8121-212121212121", "Cinder Hollow Porter", 7, "9", "normal", "Creature — Hobbit", None),
        (FLAVOR_NUNCHEON_ID, "Midnight Nuncheon", 8, "10", "normal", "Sorcery", None),
        (FLAVOR_TOKEN_ID, "Pantry Food", 9, "11", "token", "Token Artifact — Food", "Nuncheon 1:00 PM"),
    ]
    cards = []
    for card_id, name, group, number, layout, type_line, flavor_name in rows:
        card = make_raw_card(card_id=card_id, name=name)
        card |= {
            "object": "card",
            "oracle_id": f"3f3f3f3f-3f3f-4f3f-8f3f-00000000000{group}",
            "set": FLAVOR_SET_CODE,
            "set_name": "Scryfall Compat Flavor",
            "collector_number": number,
            "layout": layout,
            "type_line": type_line,
            "oracle_text": "",
            "lang": "en",
        }
        if flavor_name:
            card["flavor_name"] = flavor_name
        cards.append(card)
    # Blightsteel Colossus sld/1079's shape: a reversible card whose FACES each carry the flavor name.
    colossus = make_raw_card(card_id=FLAVOR_COLOSSUS_ID, name="Ironhull Colossus // Ironhull Colossus")
    colossus |= {
        "object": "card",
        "oracle_id": "3f3f3f3f-3f3f-4f3f-8f3f-00000000000a",
        "set": FLAVOR_SET_CODE,
        "set_name": "Scryfall Compat Flavor",
        "collector_number": "12",
        "layout": "reversible_card",
        "type_line": "Artifact Creature — Golem // Artifact Creature — Golem",
        "lang": "en",
        "card_faces": [
            {
                "object": "card_face",
                "name": "Ironhull Colossus",
                "flavor_name": "Gearlord",
                "mana_cost": "{11}",
                "type_line": "Artifact Creature — Golem",
                "oracle_text": "",
            }
            for _ in range(2)
        ],
    }
    colossus.pop("image_uris", None)
    cards.append(colossus)
    return cards


# THE STAGE ORDER of `?fuzzy=` (typo before containment, which answers when no typo candidate clears
# the floor) wants names whose scores sit where the measured needles' do, on the metric both lanes
# score -- pg_trgm's similarity of the collated name. So these are the measured needles' own names,
# ROT13-encoded: a letter-for-letter substitution keeps every trigram, so each needle scores here
# exactly what it scores against the real corpus (`vadhvfvgbe free` is `inquisitor serr`, 0.611
# against "Inquisitor's Ox"), while no other module's card can come near them. In a set of their
# own so nothing else here counts them.
ORDER_SET_CODE = "sfo"
ORDER_TITANOTH_FLAVOR_ID = "31313131-3131-4131-8131-313131313131"


def _stage_order_cards() -> list[dict]:
    """Typo winners and the cards containing their needles' words, measured pairs in ROT13."""
    rows = [
        # (oracle name, flavor name), and the needle each pair answers, in the clear.
        ("Cevzriny Gvgna", None),  # primeval titanoth: the typo winner, 0.722
        ("Gvgnabgu Erk", None),  # ... over Titanoth Rex, which carries "titanoth"
        ("Gvgnabgu Erk", "Tbqmvyyn, Cevzriny Punzcvba"),  # ... and "primeval" in a flavor name
        ("Zvaq Fphycg", None),  # mind scul: the typo winner, 0.667
        ("Wnpr, gur Zvaq Fphycgbe", None),  # ... over two names containing both words
        ("Oyvtugavat", None),  # bolt lightning: the typo winner, 0.5625
        ("Yvtugavat Obyg", None),  # ... over two names containing both words
        ("Rzrevghf bs Pbasyvpg Yvtugavat Obyg", None),
        ("Nffnhyg Qebar", None),  # assaultron: 0.500, under the floor
        ("Nffnhygeba Qbzvangbe", None),  # ... so two names containing it answer, one a flavor name
        ("Jnyxvat Onyyvfgn", "Nffnhygeba Vainqre"),
        ("Vadhvfvgbe'f Bk", None),  # inquisitor serr: the typo winner, 0.611
        ("Freen Vadhvfvgbef", None),  # ... over the one card containing both words
        ("Qvfvagrtengr", None),  # hyd disintegrat: 0.474, under the floor
        ("ULQEN Qvfvagrtengbe", None),  # ... so the one card containing both words answers
        ("Neran", None),  # arenare: the typo winner, 0.625
        ("Neran Erpgbe", None),  # ... over the one card containing it
        ("Grsrev", None),  # teferi hero: 0.500, under the floor
        ("Grsrev, Ureb bs Qbzvanevn", None),  # ... so the one card containing both words answers
    ]
    groups: dict[str, int] = {}
    cards = []
    for number, (name, flavor_name) in enumerate(rows, start=1):
        group = groups.setdefault(name, len(groups) + 1)
        card_id = (
            ORDER_TITANOTH_FLAVOR_ID if flavor_name == "Tbqmvyyn, Cevzriny Punzcvba" else f"3a3a3a3a-3a3a-4a3a-8a3a-{number:012d}"
        )
        card = make_raw_card(card_id=card_id, name=name)
        card |= {
            "object": "card",
            "oracle_id": f"3b3b3b3b-3b3b-4b3b-8b3b-{group:012d}",
            "set": ORDER_SET_CODE,
            "set_name": "Scryfall Compat Stage Order",
            "collector_number": str(number),
            "type_line": "Creature — Construct",
            "oracle_text": "",
            "lang": "en",
        }
        if flavor_name:
            card["flavor_name"] = flavor_name
        cards.append(card)
    return cards


# THE TYPO STAGE'S TIEBREAK AND FLOOR, on measured needles' own names in ROT13 (see the stage order
# above), each with its real card's first-printed day. On api.scryfall.com (2026-09-26) every tie
# answers: the card first printed most recently -- `thoughts` is Thought Scour (2012) over
# Thoughtseize (2007), both 8/14, although "Gubhtugfrvmr" sorts last -- and on one first day the
# name that sorts last: `parallax` is Parallax Wave over Parallax Tide (Nemesis, both 8/14). And
# `deadeall` scores 6/11 = 0.545 against Deadfall, just under the floor, and is a 404.
TIE_SET_CODE = "sfk"


def _typo_tie_cards() -> list[dict]:
    """Two measured ties and the floor's lower bracket, with first-printed days."""
    rows = [
        ("Gubhtug Fpbhe", "2012-02-03"),  # Thought Scour, Dark Ascension
        ("Gubhtugfrvmr", "2007-10-12"),  # Thoughtseize, Lorwyn
        ("Cnenyynk Jnir", "2000-02-14"),  # Parallax Wave, Nemesis
        ("Cnenyynk Gvqr", "2000-02-14"),  # Parallax Tide, Nemesis
        ("Qrnqsnyy", "1999-06-07"),  # Deadfall, Urza's Destiny
    ]
    cards = []
    for number, (name, released_at) in enumerate(rows, start=1):
        card = make_raw_card(card_id=f"4a4a4a4a-4a4a-4a4a-8a4a-{number:012d}", name=name)
        card |= {
            "object": "card",
            "oracle_id": f"4b4b4b4b-4b4b-4b4b-8b4b-{number:012d}",
            "set": TIE_SET_CODE,
            "set_name": "Scryfall Compat Typo Ties",
            "collector_number": str(number),
            "released_at": released_at,
            "type_line": "Instant",
            "oracle_text": "",
            "lang": "en",
        }
        cards.append(card)
    return cards


# THE FUZZY EXACT STAGE'S FACE NAMES, on measured cards' own names in ROT13 (see the stage order
# above), in a set of their own. On api.scryfall.com (2026-09-26) `fuzzy=fire` and `fuzzy=ice` are
# Fire // Ice, although Fireball contains "fire" and the playtest Start // Fire, which scores lower,
# carries it as a face too; `fuzzy=jump` is Jump over Encouraging Aviator // Jump, and `fuzzy=chaos`
# the front card Chaos over Order // Chaos, although both two-faced cards score higher. So a face
# name is a key, a whole name outranks a face, and between two faces the score decides.
FACE_SET_CODE = "sff"
FACE_FIRE_ICE_ID = "6c6c6c6c-6c6c-4c6c-8c6c-000000000001"
FACE_JUMP_ID = "6c6c6c6c-6c6c-4c6c-8c6c-000000000004"
FACE_CHAOS_ID = "6c6c6c6c-6c6c-4c6c-8c6c-000000000006"
FACE_SCORES = {
    FACE_FIRE_ICE_ID: 300,
    "6c6c6c6c-6c6c-4c6c-8c6c-000000000002": 200,
    "6c6c6c6c-6c6c-4c6c-8c6c-000000000003": 100,
    FACE_JUMP_ID: 100,
    "6c6c6c6c-6c6c-4c6c-8c6c-000000000005": 300,
    FACE_CHAOS_ID: 100,
    "6c6c6c6c-6c6c-4c6c-8c6c-000000000007": 300,
}


def _face_name_cards() -> list[dict]:
    """Two-faced cards whose face names are needles, with the whole-name cards they lose to."""
    rows = [
        ("Sver // Vpr", "split", "Instant // Instant"),  # Fire // Ice
        ("Fgneg // Sver", "split", "Sorcery // Instant"),  # Start // Fire, a playtest card
        ("Svernyy", "normal", "Sorcery"),  # Fireball, which contains "sver"
        ("Whzc", "normal", "Instant"),  # Jump
        ("Rapbhentvat Nivngbe // Whzc", "adventure", "Creature — Bird Wizard // Instant"),
        ("Punbf", "front_card", "Card"),  # Chaos, an extra
        ("Beqre // Punbf", "split", "Instant // Instant"),  # Order // Chaos
    ]
    cards = []
    for number, (name, layout, type_line) in enumerate(rows, start=1):
        card = make_raw_card(card_id=f"6c6c6c6c-6c6c-4c6c-8c6c-{number:012d}", name=name)
        card |= {
            "object": "card",
            "oracle_id": f"6d6d6d6d-6d6d-4d6d-8d6d-{number:012d}",
            "set": FACE_SET_CODE,
            "set_name": "Scryfall Compat Faces",
            "collector_number": str(number),
            "layout": layout,
            "type_line": type_line,
            "oracle_text": "",
            "lang": "en",
        }
        if " // " in name:
            card["card_faces"] = [
                {"object": "card_face", "name": face, "mana_cost": "{R}", "type_line": face_type, "oracle_text": ""}
                for face, face_type in zip(name.split(" // "), type_line.split(" // "), strict=True)
            ]
            card.pop("image_uris", None)
        cards.append(card)
    return cards


# The language rule wants an address NO English printing carries and an address TWO languages
# share, in a set of their own so nothing else here counts them. Modelled on The Hobbit Eternal,
# which prints five of its 158 cards only in Dwarvish: on api.scryfall.com `/cards/hoc/95` is the
# `lang: dw` Arcane Signet, because there is no English hoc/95 (measured 2026-09-11).
LANG_SET_CODE = "sfl"
SIGNET_ID = "88888888-8888-4888-8888-888888888888"
AMBER_ORACLE_ID = "cccccccc-3333-4333-8333-cccccccccccc"
AMBER_EN_ID = "99999999-9999-4999-8999-999999999999"
AMBER_JA_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _bolt() -> dict:
    card = make_raw_card(card_id=BOLT_ID, name="Compat Bolt")
    card |= {
        "object": "card",
        "oracle_id": BOLT_ORACLE_ID,
        "set": SET_CODE,
        "set_name": "Scryfall Compat",
        "collector_number": "1",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Compat Bolt deals 3 damage to any target.",
        "lang": "en",
        "multiverse_ids": [900001],
        "mtgo_id": 900002,
        "arena_id": 900003,
        "tcgplayer_id": 900004,
        "cardmarket_id": 900005,
        "illustration_id": "44444444-4444-4444-8444-444444444444",
    }
    return card


def _bear() -> dict:
    card = make_raw_card(card_id=BEAR_ID, name="Compat Bears")
    card |= {
        "object": "card",
        "set": SET_CODE,
        "set_name": "Scryfall Compat",
        "collector_number": "2",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "oracle_text": "",
        "power": "2",
        "toughness": "2",
        "colors": ["G"],
        "color_identity": ["G"],
        "lang": "en",
    }
    return card


def _delver() -> dict:
    """A transform card, so the multi-face path is exercised end to end."""
    card = make_raw_card(card_id=DELVER_ID, name="Compat Delver // Compat Aberration")
    card |= {
        "object": "card",
        "set": SET_CODE,
        "set_name": "Scryfall Compat",
        "collector_number": "3",
        "layout": "transform",
        # Present like every real bulk row's: the collection route's {set, collector_number}
        # identifier and the segment-less /cards/:code/:number both put the English printing first,
        # and a lang-less row sorts behind every language (see _card_at_address).
        "lang": "en",
        "type_line": "Creature — Human Wizard // Creature — Human Insect",
        "colors": ["U"],
        "color_identity": ["U"],
        "card_faces": [
            {
                "object": "card_face",
                "name": "Compat Delver",
                "mana_cost": "{U}",
                "type_line": "Creature — Human Wizard",
                "oracle_text": "Look at the top card of your library.",
                "power": "1",
                "toughness": "1",
                "colors": ["U"],
                "image_uris": {"large": "https://cards.test/front.jpg", "png": "https://cards.test/front.png"},
            },
            {
                "object": "card_face",
                "name": "Compat Aberration",
                "mana_cost": "",
                "type_line": "Creature — Human Insect",
                "oracle_text": "Flying",
                "power": "3",
                "toughness": "2",
                "colors": ["U"],
                "image_uris": {"large": "https://cards.test/back.jpg", "png": "https://cards.test/back.png"},
            },
        ],
    }
    card.pop("image_uris", None)
    return card


def _extra() -> dict:
    """A printing of the `is:extra` class, hidden by default and brought back by a trigger.

    A "Card" type line, which is how Scryfall ships the checklist and substitute-card family --
    `!"The Monarch"` (tmkc/31) is 404 bare and 200 with `include_extras=true`.
    """
    card = make_raw_card(card_id=EXTRA_ID, name="Compat Substitute")
    card |= {
        "object": "card",
        "oracle_id": EXTRA_ORACLE_ID,
        "set": EXTRAS_SET_CODE,
        "set_name": "Scryfall Compat Extras",
        "collector_number": "1",
        "type_line": "Card",
        "oracle_text": "",
        "lang": "en",
    }
    return card


def _who() -> dict:
    """A FIVE-part name, which has no face keys at all.

    `{"name":"Who // What // When // Where // Why"}` answers und/75 on api.scryfall.com while
    `{"name":"Who"}` and `exact=Who` are each not_found -- the whole name is the key and its parts
    are not. A rule built on `split_part(name, ' // ', 1)` reads this as a card with a front face
    named "Compat Who" and answers it.
    """
    card = make_raw_card(card_id=WHO_ID, name=WHO_NAME)
    card |= {
        "object": "card",
        "set": NAME_SET_CODE,
        "set_name": "Scryfall Compat Names",
        "collector_number": "1",
        "layout": "split",
        "type_line": " // ".join(["Sorcery"] * 5),
        "colors": ["W"],
        "color_identity": ["W"],
        "card_faces": [
            {
                "object": "card_face",
                "name": part,
                "mana_cost": "{W}",
                "type_line": "Sorcery",
                "oracle_text": f"{part} does nothing.",
            }
            for part in WHO_NAME.split(" // ")
        ],
    }
    card.pop("image_uris", None)
    return card


def _vault() -> dict:
    """A name carrying an accent AND an apostrophe AND a hyphen, so collation has something to do.

    `{"name":"limduls vault"}` and `{"name":"lim-duls vault"}` both answer Lim-Dûl's Vault on
    api.scryfall.com, and `exact=` of either does too. Every one of those spellings was a 404 here.
    """
    card = make_raw_card(card_id=VAULT_ID, name=VAULT_NAME)
    card |= {
        "object": "card",
        "set": NAME_SET_CODE,
        "set_name": "Scryfall Compat Names",
        "collector_number": "2",
        "mana_cost": "{1}{B}",
        "type_line": "Instant",
        "oracle_text": "Look at the top five cards of your library.",
        "colors": ["B"],
        "color_identity": ["B"],
        "lang": "en",
    }
    return card


def _signet() -> dict:
    """A printing that exists in ONE language, and that language is not English -- hoc/95's shape."""
    card = make_raw_card(card_id=SIGNET_ID, name="Compat Dwarvish Signet")
    card |= {
        "object": "card",
        "set": LANG_SET_CODE,
        "set_name": "Scryfall Compat Languages",
        "collector_number": "1",
        "type_line": "Artifact",
        "oracle_text": "",
        "lang": "dw",
    }
    return card


def _amber(card_id: str, lang: str, released_at: str) -> dict:
    """One of two printings sharing an address.

    The address lookup breaks ties on prefer_score and then on released_at DESC. The corpus puts
    the Japanese row FIRST on both -- the fixture pins its prefer_score above the English row's, and
    it released later -- on purpose: the English-first rule is only proven by an address where
    English would otherwise lose.
    """
    card = make_raw_card(card_id=card_id, name="Compat Shared Amber")
    card |= {
        "object": "card",
        "oracle_id": AMBER_ORACLE_ID,
        "set": LANG_SET_CODE,
        "set_name": "Scryfall Compat Languages",
        "collector_number": "2",
        "type_line": "Artifact",
        "oracle_text": "",
        "lang": lang,
        "released_at": released_at,
    }
    return card


@pytest.fixture(name="compat_corpus", scope="module")
def compat_corpus_fixture(api_resource: APIResource) -> APIResource:
    """Load this module's cards and their rulings once, then hand back the resource."""
    cards = (
        _bolt(),
        _bear(),
        _delver(),
        _extra(),
        _who(),
        _vault(),
        _signet(),
        _amber(AMBER_JA_ID, "ja", "2021-06-01"),
        _amber(AMBER_EN_ID, "en", "2020-01-01"),
        _pool_card("12121212-1212-4212-8212-121212121212", "1", WARDEN_NAME, "normal", "Creature — Human"),
        _pool_card(POOL_EMBLEM_ID, "2", f"{WARDEN_NAME} Emblem", "emblem", "Emblem — Hollowmere"),
        _pool_card(POOL_TOKEN_ID, "3", "Hollowmere Sentry", "token", "Token Creature — Spirit"),
        _pool_art_series(),
        *_tier_cards(),
        *_flavor_cards(),
        *_stage_order_cards(),
        *_typo_tie_cards(),
        *_face_name_cards(),
    )
    api_resource.admin._upsert_cards([copy.deepcopy(card) for card in cards])
    with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        # The ORDER of the pair sharing sfl/2 is the premise of the language tests, and prefer_score
        # is session state: a backfill run by any module before this one scores an English row about
        # forty higher than its foreign twin, which would put the English row first and make
        # "English is not displaced" true for the wrong reason. Pinned so the Japanese row leads
        # whatever ran before.
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = %(score)s WHERE scryfall_id = %(id)s",
            {"score": 200, "id": AMBER_JA_ID},
        )
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = %(score)s WHERE scryfall_id = %(id)s",
            {"score": 100, "id": AMBER_EN_ID},
        )
        # The containment tiers' foreign rows are annex printings, as default_cards leaves them. The
        # Spanish "Ego a la deriva" outscores the Portuguese "Ego à Deriva", so the shorter name
        # answering is the length rule and not the score.
        cursor.execute(
            "UPDATE magic.cards SET is_canonical = false WHERE scryfall_id = ANY(%(ids)s::uuid[])",
            {"ids": list(TIER_FOREIGN_IDS)},
        )
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = %(score)s WHERE scryfall_id = %(id)s",
            {"score": 300, "id": TIER_EGO_ES_ID},
        )
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = %(score)s WHERE scryfall_id = %(id)s",
            {"score": 10, "id": TIER_EGO_PT_ID},
        )
        # A flavor-named printing scores BELOW its card's default printing, as a Secret Lair or
        # promo usually does, so answering it is the flavor rule and not the score.
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = 10 WHERE scryfall_id = ANY(%(ids)s::uuid[])",
            {"ids": list(FLAVOR_PRINTING_IDS)},
        )
        cursor.execute(
            "UPDATE magic.cards SET prefer_score = 100 WHERE card_set_code = %(set)s AND NOT scryfall_id = ANY(%(ids)s::uuid[])",
            {"set": FLAVOR_SET_CODE, "ids": list(FLAVOR_PRINTING_IDS)},
        )
        # Each whole-name card scores BELOW the two-faced card carrying its name as a face, so its
        # answering is the tier and not the score.
        cursor.executemany(
            "UPDATE magic.cards SET prefer_score = %(score)s WHERE scryfall_id = %(id)s",
            [{"score": score, "id": card_id} for card_id, score in FACE_SCORES.items()],
        )
        cursor.execute("DELETE FROM magic.rulings WHERE oracle_id = %(oracle_id)s", {"oracle_id": BOLT_ORACLE_ID})
        # Three rulings across two dates, two of them same-day: a single ruling cannot tell one
        # ordering from another, which is how the ascending sort went unnoticed. Inserted oldest
        # first so the expected answer is not the insertion order either. The fourth repeats the
        # first verbatim, as the bulk file does 37 times, and Scryfall serves both copies.
        cursor.executemany(
            "INSERT INTO magic.rulings (oracle_id, source, published_at, comment) VALUES (%s, %s, %s, %s)",
            [
                (BOLT_ORACLE_ID, "wotc", "2004-10-04", "Any target means any target."),
                (BOLT_ORACLE_ID, "wotc", "2021-02-05", "Zero damage is still damage."),
                (BOLT_ORACLE_ID, "wotc", "2021-02-05", "A later clarification."),
                (BOLT_ORACLE_ID, "wotc", "2004-10-04", "Any target means any target."),
            ],
        )
        conn.commit()
    # /cards/search runs through _search, which prefers the in-process engine when its store is
    # loaded -- a store built before this insert would answer every query here with zero rows.
    api_resource.app_context.reload_engine(force=True)
    api_resource.admin._clear_caches()
    return api_resource


def dispatch(api: APIResource, path: str, query_string: str = "", *, method: str = "GET", body: dict | None = None):
    """Run one request through `_handle` and return the Falcon response."""
    environ = falcon.testing.create_environ(
        path=path,
        query_string=query_string,
        method=method,
        body=json.dumps(body) if body is not None else "",
        headers={"Content-Type": "application/json"} if body is not None else None,
    )
    req = falcon.Request(environ)
    resp = falcon.Response()
    api._handle(req, resp)
    return resp


@pytest.fixture(name="by_name_paths", params=["engine", "sql"])
def by_name_paths_fixture(request, compat_corpus: APIResource):
    """The corpus with the engine serving, and again with it gated off so SQL answers.

    The two are not peers -- every by-name route asks the engine first and reaches Postgres only
    when no store is loaded -- but they must agree, or the same request answers differently
    depending on which worker takes it. Running each case twice is what says so.
    """
    saved = settings.enable_engine
    settings.enable_engine = request.param == "engine"
    yield compat_corpus
    settings.enable_engine = saved


def payload(resp) -> dict:
    """Decode a response body regardless of whether it was set as media or as text."""
    if resp.media is not None:
        return resp.media
    return orjson.loads(resp.render_body())


def _parser_reads_oracleid() -> bool:
    """Whether this tree's parser knows the `oracleid:` keyword, which arrives with #926."""
    try:
        parse_scryfall_query(f"oracleid:{BOLT_ORACLE_ID}")
    except ValueError:
        return False
    return True


# The route-level `oracleid:` cases need the keyword in the PARSER, and that is #926's, not this
# branch's. Skipped until it lands rather than left red; `TestExtrasTriggers` pins the gate itself
# on a hand-built leaf meanwhile, so the rule is under test either way.
needs_oracleid = pytest.mark.skipif(not _parser_reads_oracleid(), reason="the `oracleid:` keyword arrives with #926")


class TestRouteRegistration:
    """Every Scryfall /cards/* path resolves, and only the ones that exist."""

    @pytest.mark.parametrize(
        "path",
        ["cards", "cards/search", "cards/named", "cards/autocomplete", "cards/random", "cards/collection"],
    )
    def test_path_is_registered(self, stub_api_resource: APIResource, path):
        assert path in stub_api_resource.routes

    def test_catch_all_absorbs_three_segments(self, stub_api_resource: APIResource):
        """/cards/:code/:number/:lang is the longest shape, so capacity must be exactly three."""
        assert stub_api_resource.routes["cards"].positional_capacity == 3

    def test_a_fourth_segment_is_not_a_route(self, stub_api_resource: APIResource):
        entry, _ = stub_api_resource._resolve_action("cards/a/b/c/d")
        assert entry is None

    def test_collection_answers_post_not_get(self, stub_api_resource: APIResource):
        spec = stub_api_resource.routes["cards/collection"].spec
        assert spec.methods == frozenset({"POST"})

    def test_the_named_subroutes_win_over_the_catch_all(self, stub_api_resource: APIResource):
        entry, args = stub_api_resource._resolve_action("cards/search")
        assert args == []
        assert entry.action.__name__ == "scryfall_cards_search"


class TestSearch:
    """GET /cards/search."""

    def test_returns_a_list_object(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22"))
        assert body["object"] == "list"
        assert body["total_cards"] == 1
        assert body["has_more"] is False
        assert body["data"][0]["name"] == "Compat Bolt"

    def test_cards_are_full_scryfall_objects(self, compat_corpus: APIResource):
        card = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22"))["data"][0]
        assert card["object"] == "card"
        assert card["id"] == BOLT_ID
        assert card["oracle_text"] == "Compat Bolt deals 3 damage to any target."
        assert "card_name" not in card

    def test_no_match_is_a_scryfall_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22No+Such+Compat+Card%22")
        body = payload(resp)
        assert resp.status == falcon.HTTP_404
        assert body["object"] == "error"
        assert body["code"] == "not_found"

    def test_missing_query_is_a_scryfall_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search")
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        # The character after `didn` is U+2018, and a `warnings: null` sits beside it: Scryfall
        # writes both, and this is the body a client sees most often after a typo. Key ORDER is
        # Scryfall's too.
        assert body["details"] == "You didn\u2018t enter anything to search for."
        assert list(body) == ["object", "code", "status", "warnings", "details"]
        assert body["warnings"] is None

    @pytest.mark.parametrize("order", ["penny", "review"])
    def test_the_two_unsupported_orders_warn_instead_of_failing(self, compat_corpus: APIResource, order):
        """Scryfall itself falls back silently; the cards still come back, with a note saying why."""
        body = payload(dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&order={order}"))
        assert body["total_cards"] == 1
        assert any(order in warning for warning in body["warnings"])

    @pytest.mark.parametrize("order", ["released", "set", "artist", "color", "eur", "tix", "rarity", "cmc"])
    def test_supported_orders_do_not_warn(self, compat_corpus: APIResource, order):
        """The vocabulary is built from CardOrdering, so this fails if an ordering stops being wired."""
        body = payload(dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&order={order}"))
        assert body["total_cards"] == 1
        assert "warnings" not in body

    def test_an_unrecognized_order_still_warns(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&order=nonsense"))
        assert any("nonsense" in warning for warning in body["warnings"])

    @pytest.mark.parametrize(
        ("order", "expected"),
        [("usd", SortDirection.DESC), ("rarity", SortDirection.DESC), ("name", SortDirection.ASC)],
        ids=["usd-desc", "rarity-desc", "name-asc"],
    )
    def test_dir_auto_resolves_per_order(self, compat_corpus: APIResource, monkeypatch, order, expected):
        """`auto` is Scryfall's default and is per-ordering; it must not flatten to ascending.

        Asserted on what reaches `_search` rather than on the returned page, because the fixture
        corpus is too small for two directions to differ on most orderings.
        """
        seen = {}
        original = compat_corpus._search

        def spy(**kwargs: object) -> dict:
            seen.update(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(compat_corpus, "_search", spy)
        dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&order={order}&dir=auto")
        assert resolve_direction(seen["direction"], seen["orderby"]) == expected

    def test_an_explicit_dir_is_not_turned_into_auto(self, compat_corpus: APIResource, monkeypatch):
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&order=usd&dir=asc")
        assert seen["direction"] == SortDirection.ASC

    def test_page_beyond_the_results_is_a_422(self, compat_corpus: APIResource):
        """Scryfall separates "matched nothing" from "matched, but not this far in".

        A query with no results is `404 not_found` at every page; a page past the end of a result
        that DID match is `422 validation_error` (measured 2026-08-16). Answering 404 to both told
        a paginating client its query had stopped matching.
        """
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&page=2")
        assert resp.status == falcon.HTTP_422
        body = payload(resp)
        assert body["code"] == "validation_error"
        assert body["details"].startswith("You have paginated beyond the end of these results")
        assert "warnings" not in body

    def test_a_query_that_matched_nothing_keeps_the_404_at_every_page(self, compat_corpus: APIResource):
        for page in ("1", "5"):
            resp = dispatch(compat_corpus, "/cards/search", f"q=%21%22No+Such+Card+At+All%22&page={page}")
            assert resp.status == falcon.HTTP_404
            assert payload(resp)["code"] == "not_found"

    @pytest.mark.parametrize("page", ["0", "-3", "abc", "", "0x2", "1e2", "-0"])
    def test_page_is_scryfalls_to_i_and_clamp_never_a_rejection(self, compat_corpus: APIResource, page):
        """Measured on api.scryfall.com 2026-08-16.

        page=0, page=-3, page=abc and page= all serve page 1, and page=2.5 / page=+2 / page=2abc
        truncate at the first non-digit. This surface answered `400 "The page parameter must be a
        positive integer."` -- a sentence Scryfall does not own and a rejection it does not make.
        """
        resp = dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&page={page}")
        assert resp.status == falcon.HTTP_200

    def test_next_page_is_absent_when_the_page_is_the_last(self, compat_corpus: APIResource):
        assert "next_page" not in payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22"))

    def test_next_page_lowercases_q_and_collapses_its_whitespace(self, compat_corpus: APIResource, monkeypatch):
        """Scryfall's own echo, measured 2026-08-16.

        `E:KHM T:Creature OR T:Land` comes back as `e:khm t:creature or t:land`,
        `a:"Rebecca Guay"` as `a:"rebecca guay"` (inside the quotes), `o:/^Whenever/` as
        `o:/^whenever/`, `name:Éowyn` as `name:éowyn`, and edge or doubled whitespace is trimmed
        and collapsed. All are the same query to this parser, so the echo changes only spelling.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=++T%3ACreature++OR+T%3ALand+"))
        assert body["has_more"] is True
        assert "q=t%3Acreature+or+t%3Aland&" in body["next_page"]

    def test_an_order_scryfall_serves_and_this_server_cannot_keeps_its_spelling(self, compat_corpus: APIResource, monkeypatch):
        """`penny` and `review` fall back to `name` here and Scryfall sorts by them.

        Scryfall's echo therefore says `order=penny`. Echoing `name` would round-trip fine (page 2
        falls back the same way) but differ from Scryfall for nothing.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=t%3Acreature&order=penny"))
        assert "order=penny" in body["next_page"]

    def test_next_page_echoes_the_resolved_order_and_unique(self, compat_corpus: APIResource, monkeypatch):
        """Scryfall echoes what it DECIDED, not what it was sent.

        Measured against api.scryfall.com 2026-08-16: `?order=cubecobra` -- an ordering it does not
        recognize -- comes back as `order=name` in `next_page`. A client follows that URL verbatim,
        so echoing the raw parameter hands it a link whose ordering the server already declined,
        and page 2 then pages a different result set than page 1.

        Page size shrunk to 1 so a second page exists over the session's corpus.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=t%3Acreature&order=nosuchorder&unique=nosuchmode"))
        assert body["has_more"] is True
        assert "order=name" in body["next_page"]
        assert "unique=cards" in body["next_page"]

    # ── include_extras ───────────────────────────────────────────────────────

    def test_the_extras_class_is_hidden_by_default(self, compat_corpus: APIResource):
        """Scryfall's default, which is a QUERY-TIME gate and not an import one.

        The printing is stored (#927 stopped dropping the class) and excluded here, which is the
        only arrangement where `include_extras=true` has anything to include.
        """
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Substitute%22")
        assert resp.status == falcon.HTTP_404

    def test_include_extras_true_returns_them(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Substitute%22&include_extras=true"))
        assert [card["id"] for card in body["data"]] == [EXTRA_ID]

    def test_a_trigger_term_overrides_an_explicit_false(self, compat_corpus: APIResource):
        """`is:extra` is one of the unconditional triggers, so it can never answer nothing.

        Scryfall FORCES the flag rather than defaulting it -- an explicit `include_extras=false`
        alongside a trigger term is overridden, in the rows and in the echo alike.
        """
        body = payload(
            dispatch(compat_corpus, "/cards/search", "q=is%3Aextra&include_extras=false"),
        )
        assert {card["id"] for card in body["data"]} == {
            EXTRA_ID,
            POOL_EMBLEM_ID,
            POOL_TOKEN_ID,
            POOL_ART_SERIES_ID,
            FLAVOR_TOKEN_ID,
            FACE_CHAOS_ID,
        }

    @pytest.mark.parametrize(
        "query",
        ['a:"Test Artist"', "wm:setsymbol", "layout:normal", "name:/^comp/", "t:token", "is:extra"],
        ids=["artist", "watermark", "layout", "name-regex", "type-token", "is-extra"],
    )
    def test_every_unconditional_trigger_forces_the_flag(self, compat_corpus: APIResource, monkeypatch, query):
        """Each fires on the TERM, whatever it matches -- see `_extras_triggers` for the probes.

        Asserted on what reaches `_search` rather than on the page, because these terms select
        different cards and only the flag is under test.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": query, "include_extras": "false"}))
        assert seen["include_extras"] is True

    @pytest.mark.parametrize(
        "query",
        ["t:creature", "o:damage", 'name:"compat"', "cn:1", "border:black", "is:convention", '!"Compat Bolt"'],
        ids=["type", "oracle", "name-literal", "collector-number", "border", "is-other", "exact-name"],
    )
    def test_a_non_trigger_term_leaves_the_flag_alone(self, compat_corpus: APIResource, monkeypatch, query):
        """A non-trigger term leaves the flag exactly as the caller sent it.

        The rule is SYNTACTIC and narrow: `t:creature` matches 1,742 extras on Scryfall and still
        echoes `include_extras=false`. A rule read off the RESULT SET would fire on every one of
        these.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": query, "include_extras": "false"}))
        assert seen["include_extras"] is False

    def test_a_set_term_triggers_only_for_a_set_that_holds_an_extra(self, compat_corpus: APIResource, monkeypatch):
        """The one CONDITIONAL trigger, and the reason the engine folds `sets_with_extras`.

        Measured over 18 sets on 2026-08-16 and the split is perfect: lea/leb/2ed/3ed/sum hold 1,
        4ed/5ed/6ed 2, leg 4, j21 16, hbg 122, unk 506 and every one enables; ust, ice, war, unf,
        por and 7ed hold none and none of them does.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        monkeypatch.setattr(compat_corpus, "_sets_with_extras", lambda: frozenset({EXTRAS_SET_CODE}))

        dispatch(compat_corpus, "/cards/search", urlencode({"q": f"e:{EXTRAS_SET_CODE}", "include_extras": "false"}))
        assert seen["include_extras"] is True
        dispatch(compat_corpus, "/cards/search", urlencode({"q": f"e:{SET_CODE}", "include_extras": "false"}))
        assert seen["include_extras"] is False

    def test_the_set_table_is_only_consulted_when_the_query_names_a_set(self, compat_corpus: APIResource, monkeypatch):
        """An ordinary page must not pay for the table -- the guard is the trigger walk, not a cache."""
        calls = []
        monkeypatch.setattr(compat_corpus, "_sets_with_extras", lambda: (calls.append(1), frozenset())[1])
        dispatch(compat_corpus, "/cards/search", "q=t%3Acreature")
        assert calls == []
        dispatch(compat_corpus, "/cards/search", urlencode({"q": f"e:{SET_CODE}"}))
        assert calls == [1]

    def test_an_engine_that_cannot_answer_leaves_the_auto_enable_off(self, compat_corpus: APIResource, monkeypatch):
        """A missing or wrong-build store is an empty table, never a 500 on the search."""

        class _Broken:
            def sets_with_extras(self) -> frozenset[str]:
                msg = "no store"
                raise RuntimeError(msg)

        broken = _Broken()
        monkeypatch.setattr(compat_corpus, "_engine_for_lookup", lambda: broken)
        assert compat_corpus._sets_with_extras() == frozenset()

    def test_next_page_echoes_the_resolved_include_extras(self, compat_corpus: APIResource, monkeypatch):
        """The echo is the value that was SERVED, not the parameter as sent.

        `q=e:lea&include_extras=false` echoes `include_extras=true` on api.scryfall.com AND
        returns the extras; echoing `false` while serving with them on gives a client a link that
        contradicts the page it came from. Measured 2026-08-16 over 57 set probes plus the
        unconditional families -- the echo agreed with what was served in every one.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "name:/^compat/", "include_extras": "false"})))
        assert body["has_more"] is True
        assert "include_extras=true" in body["next_page"]

    def test_next_page_echoes_a_false_it_actually_served(self, compat_corpus: APIResource, monkeypatch):
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", "q=t%3Acreature&include_extras=false"))
        assert "include_extras=false" in body["next_page"]

    @needs_oracleid
    @pytest.mark.parametrize("unique", ["cards", "prints"])
    def test_an_oracleid_whose_every_printing_is_an_extra_answers_it(self, by_name_paths: APIResource, unique):
        """`oracleid:` is an unconditional trigger, so a card that is ALL extras is not a 404.

        Mechtitan's shape: on api.scryfall.com `oracleid:<Mechtitan>` sent with
        `include_extras=false` answers tneo/14 and sld/1969, both `is:extra`, under `unique=prints`
        and tneo/14 under `unique=cards`. Without the trigger the gate closes and it is a 404. Both paths, since `_search` answers from
        the engine or from SQL depending on the worker.
        """
        query = urlencode({"q": f"oracleid:{EXTRA_ORACLE_ID}", "include_extras": "false", "unique": unique})
        body = payload(dispatch(by_name_paths, "/cards/search", query))
        assert [card["id"] for card in body["data"]] == [EXTRA_ID]

    @needs_oracleid
    def test_an_oracleid_on_an_ordinary_card_still_answers_it(self, by_name_paths: APIResource):
        """The gate opening adds nothing to a card with no extras -- Doubling Cube's shape."""
        query = urlencode({"q": f"oracleid:{BOLT_ORACLE_ID}", "include_extras": "false"})
        body = payload(dispatch(by_name_paths, "/cards/search", query))
        assert [card["id"] for card in body["data"]] == [BOLT_ID]

    @needs_oracleid
    @pytest.mark.parametrize(
        "query",
        [
            f"oracleid:{EXTRA_ORACLE_ID}",
            f"oracleid={EXTRA_ORACLE_ID}",
            f"oracleid:{EXTRA_ORACLE_ID.upper()}",
            f'oracleid:"{EXTRA_ORACLE_ID}"',
            f"oracle_id:{EXTRA_ORACLE_ID}",
            f"-oracleid:{EXTRA_ORACLE_ID} cmc=3",
            f"oracleid:{BOLT_ORACLE_ID}",
            f"oracleid:{UNKNOWN_V4_ID}",
        ],
        ids=["colon", "equals", "upper-case", "quoted", "underscore", "negated", "no-extras", "no-card"],
    )
    @pytest.mark.parametrize("unique", ["cards", "prints"])
    def test_every_oracleid_spelling_forces_the_flag(self, by_name_paths: APIResource, monkeypatch, query, unique):
        """Each spelling api.scryfall.com fires on, measured 2026-09-25 -- see `_oracle_id_term_triggers`.

        `<term> or t:creature`, the probe's own shape over this corpus: the flag is what is under
        test, and the echo carries it.
        """
        seen = {}
        original = by_name_paths._search
        monkeypatch.setattr(by_name_paths, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        params = urlencode({"q": f"{query} or t:creature", "include_extras": "false", "unique": unique})
        body = payload(dispatch(by_name_paths, "/cards/search", params))
        assert seen["include_extras"] is True
        assert "include_extras=true" in body["next_page"]

    @pytest.mark.parametrize(
        "query",
        [
            "oracleid:abc",
            "oracleid:5a5a5a5a",
            "oracleid:00000000-0000-0000-0000-000000000000",
            "oracleid:5a5a5a5a-5555-1555-8555-5a5a5a5a5a5a",
            "oracleid:5a5a5a5a-5555-4555-c555-5a5a5a5a5a5a",
            f"oracleid:{EXTRA_ORACLE_ID.replace('-', '')}",
            f"oracleid:/{EXTRA_ORACLE_ID}/",
            f"oracleid!={EXTRA_ORACLE_ID}",
        ],
        ids=["word", "prefix", "nil", "version-1", "bad-variant", "no-hyphens", "regex", "not-equal"],
    )
    def test_an_oracleid_scryfall_ignores_leaves_the_flag_alone(self, by_name_paths: APIResource, monkeypatch, query):
        """Every non-v4 value, the regex spelling and `!=` echo false on api.scryfall.com.

        Measured 2026-09-25 with `<term> or cmc=3`: 8,089 rows echoing `include_extras=false`, the
        bare count. The first seven are "ignored" there -- `You must provide a valid v4 UUID.` --
        and `!=` is kept but is not a trigger.
        """
        seen = {}
        original = by_name_paths._search
        monkeypatch.setattr(by_name_paths, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(by_name_paths, "/cards/search", urlencode({"q": f"{query} or t:creature", "include_extras": "false"}))
        assert seen["include_extras"] is False

    # ── in-query directives ──────────────────────────────────────────────────

    def test_a_directive_reaches_the_search(self, compat_corpus: APIResource, monkeypatch):
        """#893's fold runs inside `_search`, so this surface gets it by calling the same method."""
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": "t:creature unique:prints order:cmc dir:desc"}))
        assert seen["unique"] is UniqueOn.PRINTING
        assert seen["orderby"] is CardOrdering.CMC
        assert seen["direction"] is SortDirection.DESC

    def test_a_directive_beats_the_query_parameter(self, compat_corpus: APIResource, monkeypatch):
        """The directive wins, which is measured rather than assumed.

        api.scryfall.com 2026-08-16, in both directions: `unique:prints&unique=cards` answers 387
        and `unique:cards&unique=prints` answers 285.
        """
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": "t:creature unique:prints", "unique": "cards"}))
        assert seen["unique"] is UniqueOn.PRINTING

    def test_next_page_echoes_the_values_the_directives_decided(self, compat_corpus: APIResource, monkeypatch):
        """`q` echoes VERBATIM, directive and all, so the parameters beside it must agree with it.

        A link carrying `order=name` next to a `q` saying `order:cmc` pages a different result set
        on page 2 than the one page 1 came from.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(
            dispatch(
                compat_corpus,
                "/cards/search",
                urlencode({"q": "t:creature order:cmc unique:prints dir:desc"}),
            ),
        )
        assert body["has_more"] is True
        assert "order=cmc" in body["next_page"]
        assert "unique=prints" in body["next_page"]
        assert "dir=desc" in body["next_page"]

    def test_a_directive_warning_is_reported_once(self, compat_corpus: APIResource):
        """In Scryfall's sentence, typographic quotes and all, and by the term policy alone."""
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "t:creature unique:bogus"})))
        assert body["warnings"] == ["Unknown unique mode \u201cbogus\u201d was ignored"]

    def test_page_size_matches_scryfall(self):
        assert PAGE_SIZE == 175

    def test_pretty_emits_indented_json(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&pretty=true")
        assert resp.text.startswith('{\n  "object"')

    def test_csv_format_is_scryfalls_eighteen_column_export(self, compat_corpus: APIResource):
        """The header row's bytes are the contract, so they are asserted whole.

        This used to assert `header.startswith("object,id,oracle_id,")` -- the flattened card object
        this route invented. api.scryfall.com exports a SUMMARY instead: eighteen columns, several
        named differently from the JSON keys behind them (`scryfall_id` not `id`, `usd_price` not
        `prices.usd`, one `multiverse_id` rather than the array). Measured 2026-08-16.
        """
        resp = dispatch(compat_corpus, "/cards/search", "q=%21%22Compat+Bolt%22&format=csv")
        # NO charset, unlike every JSON response here -- Scryfall's exactly.
        assert resp.content_type == "text/csv"
        assert resp.headers["content-disposition"] == 'attachment; filename="search.csv"'
        # `has_more` has no envelope in a CSV body, so it rides a header.
        assert resp.headers["x-scryfall-has-more"] == "false"
        header, row = resp.text.splitlines()[:2]
        assert header == (
            "multiverse_id,mtgo_id,set,collector_number,lang,rarity,name,mana_cost,cmc,type_line,"
            "artist,usd_price,usd_foil_price,eur_price,tix_price,image_uri,scryfall_uri,scryfall_id"
        )
        assert row.endswith(BOLT_ID)
        assert resp.text.endswith("\n")

    def test_csv_is_case_sensitive_and_an_unknown_format_is_json(self, compat_corpus: APIResource):
        """`format=CSV` and `format=bogus` both serve JSON on api.scryfall.com -- never an error."""
        for spelling in ("CSV", "bogus", "text", "image"):
            resp = dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&format={spelling}")
            assert resp.content_type.startswith("application/json"), spelling
            assert payload(resp)["object"] == "list", spelling

    def test_csv_quotes_only_what_rfc_4180_requires(self):
        """Minimal quoting, and an EMPTY STRING is not the same cell as an absent value.

        Every basic land is the proof: Scryfall's JSON gives it `"mana_cost": ""` and the CSV row
        reads `...,"",0.0,Land,...` -- two bytes where the null price columns beside it have none.
        """
        assert _csv_cell("Lightning Bolt") == "Lightning Bolt"
        assert _csv_cell("Legendary Creature — God // Legendary Creature — Bird") == (
            "Legendary Creature — God // Legendary Creature — Bird"
        )
        assert _csv_cell("Alrund, God of the Cosmos") == '"Alrund, God of the Cosmos"'
        assert _csv_cell('Henzie "Toolbox" Torre') == '"Henzie ""Toolbox"" Torre"'
        assert _csv_cell("") == '""'
        assert _csv_cell(None) == ""

    def test_csv_prices_round_trip_through_float(self):
        """The JSON carries two decimals always; the CSV does not, and a null price is empty."""
        assert _csv_price("60.00") == "60.0"
        assert _csv_price("0.10") == "0.1"
        assert _csv_price("2.57") == "2.57"
        assert _csv_price(None) is None
        assert _csv_cell(_csv_price(None)) == ""

    def test_csv_mana_cost_drops_the_empty_half_of_a_two_image_card(self):
        """Measured: Delver of Secrets is `{U}`, not `{U} // `, and a free MDFC land is `""`."""
        assert _csv_mana_cost({"mana_cost": "{1}{R} // {1}{U}"}) == "{1}{R} // {1}{U}"
        assert _csv_mana_cost({"card_faces": [{"mana_cost": "{U}"}, {"mana_cost": ""}]}) == "{U}"
        assert _csv_mana_cost({"card_faces": [{"mana_cost": ""}, {"mana_cost": ""}]}) == ""
        assert _csv_mana_cost({}) is None

    def test_unparseable_query_is_a_scryfall_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=%28cmc%3E1")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["object"] == "error"

    def test_unbalanced_parentheses_get_scryfalls_own_sentence(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=t%3Acreature+%28t%3Aland")
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert body["details"] == "Your search contains unclosed parentheses."
        assert body["warnings"] is None

    def test_a_query_whose_every_term_was_ignored_is_a_400_carrying_the_warnings(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=subtype%3Aeldrazi")
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert list(body) == ["object", "code", "status", "warnings", "details"]
        assert body["details"] == "All of your terms were ignored."
        assert body["warnings"] == [
            "Invalid expression \u201csubtype:eldrazi\u201d was ignored. Unknown keyword \u201csubtype\u201d.",
        ]

    def test_a_surviving_term_makes_an_ignored_one_a_warning_on_a_200(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", "q=f%3Anotaformat+%21%22Compat+Bolt%22"))
        assert body["object"] == "list"
        assert body["total_cards"] == 1
        assert body["warnings"] == [
            "Invalid expression \u201cf:notaformat\u201d was ignored. Unknown game format \u201cnotaformat\u201d",
        ]

    def test_typographic_quotes_reach_the_parser_as_the_quotes_they_stand_for(self, compat_corpus: APIResource):
        """Users paste curly quotes constantly; this surface answered 400 to every one of them."""
        curly = payload(dispatch(compat_corpus, "/cards/search", "q=%21%E2%80%9CCompat+Bolt%E2%80%9D"))
        assert curly["total_cards"] == 1

    def test_a_malformed_regex_is_a_400_carrying_scryfalls_reason(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", "q=o%3A%2F%5Bunclosed%2F")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["warnings"] == [
            "Invalid expression \u201co:/[unclosed/\u201d was ignored. Invalid regular expression: brackets [] not balanced.",
        ]

    @pytest.mark.parametrize("unique", ["card", "printing", "printings", "artwork", "bogus"])
    def test_an_unrecognized_unique_mode_is_silent_as_scryfalls_is(self, compat_corpus: APIResource, unique):
        body = payload(dispatch(compat_corpus, "/cards/search", f"q=%21%22Compat+Bolt%22&unique={unique}"))
        assert "warnings" not in body


class TestDisplayOptionsInTheQuery:
    """`unique:`, `order:`, `include:` and the rest, written in `q`, are display options and not terms.

    Measured on api.scryfall.com 2026-10-03; the requests are in query_terms.py beside
    `_DISPLAY_OPTION_VALUES` and `_INCLUDE_KEYWORD`.
    """

    def test_the_policys_vocabulary_is_the_folds(self):
        """The term policy lists the values `_fold_directives` accepts, and may not drift from it."""
        from api.api_resource import _DIRECTIVE_TABLES  # noqa: PLC0415
        from api.scryfall_compat.query_terms import _DISPLAY_OPTION_VALUES  # noqa: PLC0415

        assert set(_DISPLAY_OPTION_VALUES) == set(_DIRECTIVE_TABLES)
        for name, (_noun, values) in _DISPLAY_OPTION_VALUES.items():
            assert values == set(_DIRECTIVE_TABLES[name][1]), name

    @pytest.mark.parametrize("query", ["unique:prints", "order:cmc", "prefer:oldest", "display:grid", "unique:prints order:cmc"])
    def test_a_query_of_nothing_but_options_is_all_terms_ignored(self, compat_corpus: APIResource, query):
        """This surface answered the whole corpus; Scryfall answers a 400 with `warnings: null`."""
        resp = dispatch(compat_corpus, "/cards/search", urlencode({"q": query}))
        assert resp.status == falcon.HTTP_400
        assert payload(resp) == {
            "object": "error",
            "code": "bad_request",
            "status": 400,
            "warnings": None,
            "details": "All of your terms were ignored.",
        }

    def test_an_unknown_option_alone_carries_its_warning_into_the_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", urlencode({"q": "unique:nonsense"}))
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert body["details"] == "All of your terms were ignored."
        assert body["warnings"] == ["Unknown unique mode \u201cnonsense\u201d was ignored"]

    def test_an_equals_sign_does_not_make_an_option(self, compat_corpus: APIResource):
        """`unique=prints` beside a term is that term's answer carrying the unknown-keyword sentence."""
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": '!"Compat Bolt" unique=prints'})))
        assert body["total_cards"] == 1
        assert body["warnings"] == [
            "Invalid expression \u201cunique=prints\u201d was ignored. Unknown keyword \u201cunique\u201d.",
        ]

    def test_a_display_mode_is_accepted_silently(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": '!"Compat Bolt" display:grid'})))
        assert body["total_cards"] == 1
        assert "warnings" not in body

    @pytest.mark.parametrize(
        "query",
        ['(unique:prints !"Compat Bolt")', '!"Compat Bolt" (include:extras or t:creature)', '(order:nonsense !"Compat Bolt")'],
    )
    def test_an_option_inside_parentheses_is_scryfalls_own_400(self, compat_corpus: APIResource, query):
        resp = dispatch(compat_corpus, "/cards/search", urlencode({"q": query}))
        assert resp.status == falcon.HTTP_400
        assert payload(resp) == {
            "object": "error",
            "code": "bad_request",
            "status": 400,
            "warnings": None,
            "details": "Display options may not be specified inside parentheses.",
        }

    def test_a_negated_option_and_one_beside_an_or_apply_without_comment(self, compat_corpus: APIResource, monkeypatch):
        """A `-` changes nothing, and the option is removed before the connectors are read."""
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "-unique:prints or t:creature order:cmc"})))
        assert seen["query"] == "t:creature"
        assert seen["unique"] is UniqueOn.PRINTING
        assert seen["orderby"] is CardOrdering.CMC
        assert "warnings" not in body

    # ── include: ─────────────────────────────────────────────────────────────

    def test_include_extras_opens_the_gate_the_parameter_opens(self, compat_corpus: APIResource):
        """`!"…"` fires no trigger, so without the option this is the 404 the default lane answers."""
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": '!"Compat Substitute" include:extras'})))
        assert [card["id"] for card in body["data"]] == [EXTRA_ID]
        assert "warnings" not in body

    def test_the_option_beats_a_parameter_that_says_false(self, compat_corpus: APIResource, monkeypatch):
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        query = urlencode({"q": '!"Compat Substitute" include:extras', "include_extras": "false"})
        body = payload(dispatch(compat_corpus, "/cards/search", query))
        assert [card["id"] for card in body["data"]] == [EXTRA_ID]
        assert seen["include_extras"] is True
        assert seen["include_variations"] is False
        assert seen["include_multilingual"] is False

    @pytest.mark.parametrize(
        ("value", "flags"),
        [
            ("extras", (True, False, False)),
            ("variations", (False, True, False)),
            ("multilingual", (False, False, True)),
            ("all", (True, True, True)),
            ("everything", (True, True, True)),
            ("funny", (False, False, False)),
        ],
    )
    def test_each_include_value_reaches_the_search_as_its_parameter_would(
        self, compat_corpus: APIResource, monkeypatch, value, flags
    ):
        seen = {}
        original = compat_corpus._search
        monkeypatch.setattr(compat_corpus, "_search", lambda **kw: (seen.update(kw), original(**kw))[1])
        dispatch(compat_corpus, "/cards/search", urlencode({"q": f"t:creature include:{value}"}))
        assert seen["query"] == "t:creature"
        assert (seen["include_extras"], seen["include_variations"], seen["include_multilingual"]) == flags

    def test_next_page_echoes_the_flags_the_option_set(self, compat_corpus: APIResource, monkeypatch):
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "t:creature include:all"})))
        assert body["has_more"] is True
        assert "include_extras=true" in body["next_page"]
        assert "include_variations=true" in body["next_page"]
        assert "include_multilingual=true" in body["next_page"]

    def test_an_unknown_include_value_opens_nothing_and_the_page_carries_the_warning(self, compat_corpus: APIResource):
        """The sentence really is the `direction:` one."""
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": '!"Compat Bolt" include:foo'})))
        assert body["total_cards"] == 1
        assert body["warnings"] == ["Unknown direction choice \u201cfoo\u201d was ignored"]
        resp = dispatch(compat_corpus, "/cards/search", urlencode({"q": '!"Compat Substitute" include:foo'}))
        assert resp.status == falcon.HTTP_404

    def test_include_alone_is_the_400_with_no_warnings(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/search", urlencode({"q": "include:extras"}))
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert body["details"] == "All of your terms were ignored."
        assert body["warnings"] is None

    def test_the_random_draw_reads_include_too(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/random", urlencode({"q": '!"Compat Substitute"'}))
        assert resp.status == falcon.HTTP_404
        body = payload(dispatch(compat_corpus, "/cards/random", urlencode({"q": '!"Compat Substitute" include:extras'})))
        assert body["id"] == EXTRA_ID

    def test_the_random_draw_refuses_a_nested_option(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/random", urlencode({"q": "(include:extras t:creature)"}))
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["details"] == "Display options may not be specified inside parentheses."


class TestRegexesScryfallWillNotRun:
    """A regex Scryfall ignores is ignored here, with its sentence, and the rest of the query answers."""

    @pytest.mark.parametrize(
        ("pattern", "echo", "reason"),
        [
            ("(((a)))", "o:/(((a)))/", "Too many nested groups."),
            ("." * 90, "o:/................\u2026", "Regular expression too complex."),
            ("a{51}", "o:/a{51}/", "Too much repetition."),
            ("(?i)bolt", "o:/(?i)bolt/", "Invalid regular expression: quantifier operand invalid."),
            ("\\p{L}olt", "o:/\\p{L}olt/", "Invalid regular expression: invalid escape \\ sequence."),
            # Scryfall would run this one; the parser's own budget will not, and says so the same way.
            ("(?=a)(?=b)(?=c)(?=d)(?=e)x", "o:/(?=a)(?=b)(?=c)(\u2026", "Regular expression too complex."),
        ],
    )
    def test_beside_another_term_it_is_a_warning_on_a_200(self, compat_corpus: APIResource, pattern, echo, reason):
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": f'!"Compat Bolt" o:/{pattern}/'})))
        assert body["total_cards"] == 1
        assert body["warnings"] == [f"Invalid expression \u201c{echo}\u201d was ignored. {reason}"]

    def test_alone_it_is_the_400_that_carries_the_warning(self, compat_corpus: APIResource):
        resp = dispatch(
            compat_corpus, "/cards/search", urlencode({"q": "o:/destroy ((target (nonblack|nonwhite))|that) creature/"})
        )
        assert resp.status == falcon.HTTP_400
        assert payload(resp) == {
            "object": "error",
            "code": "bad_request",
            "status": 400,
            "warnings": ["Invalid expression \u201co:/destroy ((target\u2026\u201d was ignored. Too many nested groups."],
            "details": "All of your terms were ignored.",
        }

    def test_a_backreference_matches_nothing_instead_of_refusing_the_query(self, compat_corpus: APIResource):
        r"""`t:creature name:/^(.)\1\1/` is Scryfall's plain 404, with no warnings."""
        resp = dispatch(compat_corpus, "/cards/search", urlencode({"q": "s:sfc name:/(o)\\1/"}))
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["code"] == "not_found"
        # The complement of nothing is everything the rest of the query matches.
        negated = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "s:sfc -name:/(.)\\1/"})))
        plain = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": "s:sfc"})))
        assert negated["total_cards"] == plain["total_cards"]
        assert "warnings" not in negated

    def test_search_keeps_the_budget_as_its_own_refusal(self, compat_corpus: APIResource):
        """Only this surface drops the term; `/search` answers the budget's 400 as it did."""
        with pytest.raises(falcon.HTTPBadRequest) as refused:
            compat_corpus._search(query="t:creature o:/(?=a)(?=b)(?=c)(?=d)(?=e)x/", limit=10)
        assert refused.value.description == QUERY_REGEX_REJECTED_MESSAGE


class TestManyFacedText:
    """A card with more than two faces has no searchable rules text.

    Measured on api.scryfall.com 2026-10-04, each scoped `!"Who // What // When // Where // Why"`:
    `o:target`, `o:destroy`, `o:/target/` and `o:/./` are 404 and `o:/^$/` is 1. The corpus card is
    this module's five-part name, whose every face reads "<name> does nothing."
    """

    @pytest.mark.parametrize("term", ["o:nothing", 'o:"does nothing"', "o:/nothing/", "o:/./"])
    def test_no_term_finds_it_through_a_face(self, compat_corpus: APIResource, term):
        resp = dispatch(compat_corpus, "/cards/search", urlencode({"q": f'!"{WHO_NAME}" {term}'}))
        assert resp.status == falcon.HTTP_404

    def test_the_text_is_there_and_empty(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": f'!"{WHO_NAME}" o:/^$/'})))
        assert [card["id"] for card in body["data"]] == [WHO_ID]

    def test_the_card_object_still_prints_every_face(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/search", urlencode({"q": f'!"{WHO_NAME}"'})))
        (card,) = body["data"]
        assert [face["oracle_text"] for face in card["card_faces"]] == [f"{part} does nothing." for part in WHO_NAME.split(" // ")]

    @pytest.mark.parametrize("term", ['o:"top card of your library"', "o:flying", "o:/flying/"])
    def test_a_two_faced_card_is_searched_through_either_face_as_ever(self, compat_corpus: APIResource, term):
        query = urlencode({"q": f'!"Compat Delver // Compat Aberration" {term}'})
        body = payload(dispatch(compat_corpus, "/cards/search", query))
        assert [card["id"] for card in body["data"]] == [DELVER_ID]


class TestVariationsGate:
    """`include_variations`, its auto-enable, and its independence from the extras gate."""

    def test_only_is_variation_forces_the_gate(self):
        """The whole of this gate's auto-enable rule, and it is a FORCE like the extras ones.

        `t:creature or is:variation` sent with `include_variations=false` answers 51,566 on
        api.scryfall.com and echoes true. Nothing that enables EXTRAS enables this: `a:`, `wm:`,
        `layout:`, `name:/^z/`, `t:token`, `is:extra`, `is:oversized`, `is:reserved`,
        `is:rebalanced` and a set term all echo `include_variations=false` (measured 2026-08-16,
        and `e:hho` is 21 bare against 23 with the parameter though hho auto-enables extras).
        """
        forced = routes_module._mentions_is_tag
        assert forced(parse_scryfall_query("is:variation"), "variation") is True
        assert forced(parse_scryfall_query("t:creature or is:variation"), "variation") is True
        assert forced(parse_scryfall_query("-is:variation t:land"), "variation") is True
        for query in ("a:guay", "wm:mirran", "layout:normal", "t:token", "is:extra", "is:oversized", "e:hho"):
            assert forced(parse_scryfall_query(query), "variation") is False, query

    def test_the_two_gates_are_independent(self, compat_corpus: APIResource):
        """Both conjuncts can be spliced at once, and neither subsumes the other.

        `t:creature` is 51,473 bare, 55,454 with extras alone, 51,523 with variations alone and
        55,506 with both; `is:variation` is 93 with variations on and 97 once extras are on too,
        so the classes overlap by 4 printings out of 97.
        """
        for params, expect_variation in (("", True), ("include_variations=true", False)):
            body = payload(dispatch(compat_corpus, "/cards/search", f"q=t%3Acreature&{params}"))
            echoed = body.get("next_page") or ""
            assert ("include_variations=true" in echoed) is not expect_variation or not echoed


class TestExtrasTriggers:
    """`_extras_triggers`, the syntactic rule behind `include_extras`'s auto-enable.

    Pure parse-tree tests: no corpus, because the whole point of the measurement is that the rule
    does NOT depend on what the query matches.
    """

    @pytest.mark.parametrize(
        "query",
        [
            'a:"Wesley Burt"',
            "wm:llorwyn",
            "layout:normal",
            "layout:art_series",
            "name:/^z/",
            "t:token",
            "is:extra",
        ],
    )
    def test_unconditional_triggers(self, query):
        """Each fires on the TERM, not on what it selects.

        `a:"Wesley Burt"` triggers although `a:"Wesley Burt" is:extra` is 0, `name:/zzzqq/` matches
        nothing and still triggers, and `layout:normal` -- the most ordinary value there is --
        triggers. Measured on api.scryfall.com 2026-08-16.
        """
        assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is True

    @pytest.mark.parametrize(
        "query",
        [
            "t:creature",
            "o:draw",
            "o:/^whenever/",
            "ft:death",
            "cn:100",
            "year:1993",
            "border:black",
            "frame:2003",
            "is:convention",
            'name:"lightning"',
            "name:lightning",
            '!"Lightning Bolt"',
            "",
        ],
    )
    def test_probed_non_triggers(self, query):
        """Every one of these was probed and does NOT auto-enable.

        `t:creature`, `o:draw` and `ft:death` match 1,742 / 358 / 26 extras respectively, so a rule
        read off the RESULT SET would fire on all three. This one does not, which is the whole
        finding.
        """
        assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is False

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("e:lea", ("lea",)),
            ("s:khm", ("khm",)),
            ("set:WAR", ("war",)),
            ("e:war or e:lea", ("war", "lea")),
            ("-e:lea t:land", ("lea",)),
        ],
    )
    def test_set_terms_are_collected_lowercased(self, query, expected):
        triggers = routes_module._extras_triggers(parse_scryfall_query(query))
        assert triggers.forced is False
        assert triggers.sets == expected

    def test_a_trigger_propagates_out_of_an_or_and_a_negation(self):
        """Both kinds of trigger propagate through `or` and through negation.

        `(e:lea t:creature) or t:land` enables on api.scryfall.com even though LEA's only extra is
        an enchantment that cannot be in that result set, and `-e:lea t:land` enables while
        `-e:war t:land` does not. Negation does not cancel a trigger, which is what makes the rule
        syntactic rather than semantic.
        """
        assert routes_module._extras_triggers(parse_scryfall_query("t:land or wm:x")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("-wm:x t:land")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("(e:lea t:creature) or t:land")).sets == ("lea",)

    def test_a_lowered_literal_regex_is_still_a_regex_here(self):
        """The rewrite lowers a metacharacter-free regex to a literal before this walk sees it.

        That USED to be a residual in both directions: `name:/zzzqq/` read as `name:"zzzqq"` and
        missed a trigger Scryfall fires, and `t:/token/` read as `t:token` and fired one Scryfall
        does not. `StringValueNode.regex_derived` records the spelling the rewrite erased, so both
        now answer as Scryfall does. Measured 2026-08-16::

            name:/bolt/  175 = its extras-on count    name:"bolt"  157
            t:token cmc=3  6 (extras auto-enabled)    t:/token/ cmc=3  0
            is:/extra/ cmc=3 and border:/silver/ cmc=3 both answer plain cmc=3 (22,832)

        Every pattern with a metacharacter keeps its RegexValueNode and behaved all along.
        """
        assert routes_module._extras_triggers(parse_scryfall_query("name:/zzzqq/")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("name:/^z/")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query('name:"zzzqq"')).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("t:/token/")).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("t:token")).forced is True
        assert routes_module._extras_triggers(parse_scryfall_query("t:/^token$/")).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("is:/extra/")).forced is False
        assert routes_module._extras_triggers(parse_scryfall_query("border:/silver/")).forced is False

    def test_the_is_and_border_triggers_are_value_specific(self):
        """Five `is:` values and one `border:` value force extras on; their neighbours do not.

        All 32 supported STORED `is:` values were probed for the `include_extras` echo on
        2026-08-16, and `border:gold` is the control that makes `border:silver` a trigger rather
        than a coincidence: every gold border is a World Championship card, so the whole population
        is memorabilia, and it still answers 0 bare against 1,373 with the flag.

        `glossy` is the same point from the other side. It holds NO extras, so the flag cannot move
        its count and a count-based probe calls it unfalsifiable -- and the echo says true anyway,
        because the rule is syntactic.
        """
        for value in ("extra", "oversized", "reserved", "rebalanced", "glossy"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"is:{value}")).forced is True, value
        for value in ("variation", "convention", "judge", "league", "promo", "foil"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"is:{value}")).forced is False, value
        assert routes_module._extras_triggers(parse_scryfall_query("border:silver")).forced is True
        for value in ("gold", "black", "white", "borderless"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"border:{value}")).forced is False, value
        for value in ("1993", "2015", "future"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"frame:{value}")).forced is False, value

    def test_a_derived_is_fires_on_the_term_written_not_on_its_expansion(self):
        """`is:split` is not `layout:split` for this rule, though the rewrite makes them one tree.

        Measured on api.scryfall.com 2026-08-16: `is:split` echoes `include_extras=false` and
        answers 327 where `layout:split` echoes true and answers 347. All 90 derived values were
        probed one at a time; twelve fire and 78 do not, and the split follows no structural rule --
        `is:mdfc` fires with zero extras in its population, `is:stamped` does not fire with 696.
        """
        # Derived, expands to something that DOES trigger, and does not trigger there. The first
        # six expand to `layout:`, an unconditional trigger; `is:commander` expands to a subtree
        # ending in `-banned:commander`, which is one too.
        for query in (
            "is:split",
            "is:flip",
            "is:transform",
            "is:meld",
            "is:leveler",
            "is:adventure",
            "is:commander",
        ):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is False, query
        # Derived AND measured triggers. `is:dfc` is the one that makes this a list rather than a
        # rule: it is exactly the union of transform / modal_dfc / meld, and it fires where two of
        # its three parts do not.
        for query in ("is:mdfc", "is:dfc"):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is True, query
        # The spelling the caller DID write still fires beside the one they did not.
        assert routes_module._extras_triggers(parse_scryfall_query("is:split layout:normal")).forced is True
        # And a set term inside an expansion is not a set the caller named: nothing widens by
        # accident through the conditional arm either.
        assert routes_module._extras_triggers(parse_scryfall_query("is:split")).sets == ()

    @staticmethod
    def _oracle_id_leaf(value: str, operator: str = ":", *, regex_derived: bool = False) -> BinaryOperatorNode:
        """An `oracleid:` leaf as #926's parser builds it: `oracle_id`, the operator, the value.

        Built by hand because the keyword is #926's and this branch's parser does not know it yet;
        the shape is the one that parser emits, `regex_derived` included for the lowered regex.
        """
        return BinaryOperatorNode(AttributeNode("oracle_id"), operator, StringValueNode(value, regex_derived=regex_derived))

    def test_an_oracleid_naming_a_v4_uuid_forces_extras(self):
        """`oracleid:` at a v4 UUID fires on the TERM -- see `_oracle_id_term_triggers`.

        Measured on api.scryfall.com 2026-09-25 with `<term> or cmc=3`: `:` and `=`, any case,
        negated, and for a well-formed id no card has. `oracle_id:` binds to the same attribute and
        quoting reaches the same leaf, so this covers those spellings too.
        """
        cmc = parse_scryfall_query("cmc=3").root
        for leaf in (
            self._oracle_id_leaf(EXTRA_ORACLE_ID),
            self._oracle_id_leaf(EXTRA_ORACLE_ID, "="),
            self._oracle_id_leaf(EXTRA_ORACLE_ID.upper()),
            self._oracle_id_leaf(UNKNOWN_V4_ID),
        ):
            assert routes_module._extras_triggers(leaf).forced is True, (leaf.operator, leaf.rhs.value)
            assert routes_module._extras_triggers(OrNode([leaf, cmc])).forced is True
            assert routes_module._extras_triggers(NotNode(leaf)).forced is True

    def test_an_oracleid_scryfall_ignores_does_not_force_extras(self):
        """A value that is not a v4 UUID, the regex spelling and `!=` all echo false.

        Measured 2026-09-25: the non-v4 values are "ignored" with `You must provide a valid v4
        UUID.` and the regex with an unknown-regex-keyword warning, so none of them reaches
        Scryfall's tree; `!=` does, and still is not a trigger. The route's term policy drops all
        of these but `!=` first, so the walk's own checks are what these pin.
        """
        for value in (
            "abc",
            "5a5a5a5a",
            "00000000-0000-0000-0000-000000000000",
            "5a5a5a5a-5555-1555-8555-5a5a5a5a5a5a",
            "5a5a5a5a-5555-4555-c555-5a5a5a5a5a5a",
            EXTRA_ORACLE_ID.replace("-", ""),
        ):
            assert routes_module._extras_triggers(self._oracle_id_leaf(value)).forced is False, value
        assert routes_module._extras_triggers(self._oracle_id_leaf(EXTRA_ORACLE_ID, regex_derived=True)).forced is False
        assert routes_module._extras_triggers(self._oracle_id_leaf(EXTRA_ORACLE_ID, "!=")).forced is False

    @needs_oracleid
    def test_the_parsed_oracleid_spellings_agree_with_the_hand_built_leaf(self):
        """The same verdicts through the parser, once it knows the keyword."""
        for query in (
            f"oracleid:{EXTRA_ORACLE_ID}",
            f"oracleid={EXTRA_ORACLE_ID}",
            f"oracle_id:{EXTRA_ORACLE_ID.upper()}",
            f'oracleid:"{EXTRA_ORACLE_ID}"',
            f"-oracleid:{EXTRA_ORACLE_ID} cmc=3",
            f"oracleid:{UNKNOWN_V4_ID} or cmc=3",
        ):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is True, query
        for query in (f"oracleid!={EXTRA_ORACLE_ID}", "oracleid:abc", f"oracleid:/{EXTRA_ORACLE_ID}/"):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is False, query

    def test_banned_triggers_wholesale_and_f_only_at_premodern(self):
        """Every legality alias binds to `card_legalities`, so the alias separates them.

        `banned:` fires at every value probed while `restricted:` does not, and of the 21 format
        values `premodern` is the only one that fires -- `legal:premodern` fires too, so it is the
        value rather than the alias. Measured 2026-08-16.
        """
        for value in ("legacy", "vintage", "modern", "pauper"):
            assert routes_module._extras_triggers(parse_scryfall_query(f"banned:{value}")).forced is True, value
        for query in ("f:premodern", "format:premodern", "legal:premodern", "-f:premodern t:land"):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is True, query
        for query in ("restricted:vintage", "f:pauper", "f:legacy", "f:vintage", "legal:standard", "f:oldschool"):
            assert routes_module._extras_triggers(parse_scryfall_query(query)).forced is False, query


class TestNamed:
    """GET /cards/named."""

    def test_exact_match_is_case_insensitive(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "exact=compat+bolt"))
        assert body["name"] == "Compat Bolt"

    def test_exact_miss_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "exact=Compat+Nothing")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["code"] == "not_found"

    def test_exact_matches_one_face_of_a_multi_face_card(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "exact=Compat+Aberration"))
        assert body["id"] == DELVER_ID

    def test_set_filter_narrows_the_match(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", f"exact=Compat+Bolt&set={SET_CODE}"))
        assert body["set"] == SET_CODE

    def test_set_filter_that_excludes_the_card_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "exact=Compat+Bolt&set=zzz")
        assert resp.status == falcon.HTTP_404

    def test_fuzzy_resolves_a_containment_match(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "fuzzy=compat+bears"))
        assert body["name"] == "Compat Bears"

    def test_fuzzy_containment_ignores_the_names_separators(self, compat_corpus: APIResource):
        """Scryfall matches a word against the name with its separators gone (measured 2026-08-16).

        `fuzzy=redgoad` answers not_found there while `fuzzy=red goad` resolves, so this is one
        word matched against one unseparated name -- not the query being rejoined.
        """
        body = payload(dispatch(compat_corpus, "/cards/named", "fuzzy=compatbolt"))
        assert body["name"] == "Compat Bolt"

    def test_fuzzy_ambiguity_is_a_not_found_carrying_a_type(self, compat_corpus: APIResource):
        """api.scryfall.com sends code=not_found with type=ambiguous, not code=ambiguous."""
        resp = dispatch(compat_corpus, "/cards/named", "fuzzy=compat")
        assert resp.status == falcon.HTTP_404
        body = payload(resp)
        assert body["code"] == "not_found"
        assert body["type"] == "ambiguous"
        assert "Too many cards match ambiguous name" in body["details"]

    def test_fuzzy_tolerates_a_typo(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/named", "fuzzy=Compat+Bolzt"))
        assert body["name"] == "Compat Bolt"

    def test_fuzzy_miss_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "fuzzy=qqqqzzzzxxxx")
        assert resp.status == falcon.HTTP_404

    @pytest.mark.parametrize("needle", ["sver", "vpr", "SVER"])
    def test_fuzzy_a_face_name_is_an_exact_key(self, by_name_paths: APIResource, needle):
        """Either face of a two-part name answers in the exact stage, before containment is asked.

        Measured on api.scryfall.com 2026-09-26: `fuzzy=fire` and `fuzzy=ice` are Fire // Ice, and
        so are `tear` Wear // Tear, `boom` and `bust` Boom // Bust, `life` Life // Death and
        `appeal` Appeal // Authority. Matching whole names only, the stage left `sver` to
        containment, where Svernyy and Fgneg // Sver contain it too, and answered `ambiguous`.
        Fgneg // Sver carries the same face and scores lower, so Sver // Vpr answers, as the playtest
        Start // Fire loses `fuzzy=fire` there.
        """
        assert payload(dispatch(by_name_paths, "/cards/named", f"fuzzy={needle}"))["id"] == FACE_FIRE_ICE_ID

    @pytest.mark.parametrize(("needle", "expected"), [("whzc", FACE_JUMP_ID), ("punbf", FACE_CHAOS_ID)])
    def test_fuzzy_a_whole_name_outranks_a_face_that_scores_higher(self, by_name_paths: APIResource, needle, expected):
        """The whole-name tier leads the score, and an extra's whole name leads a served card's face.

        Measured on api.scryfall.com 2026-09-26: `fuzzy=jump` is Jump m10/59, not Encouraging
        Aviator // Jump, and `fuzzy=chaos` the front card Chaos fj25/46, not Order // Chaos --
        although each two-faced card has the higher prefer score, and Chaos is an extra.
        """
        assert payload(dispatch(by_name_paths, "/cards/named", f"fuzzy={needle}"))["id"] == expected

    @pytest.mark.parametrize("needle", ["sver", "vpr", "fgneg", "whzc", "punbf", "Sver // Vpr"])
    def test_fuzzy_and_exact_read_the_same_oracle_keys(self, by_name_paths: APIResource, needle):
        """`exact=fire` is Fire // Ice and `exact=chaos` Chaos on api.scryfall.com too (2026-09-26)."""
        fuzzy = payload(dispatch(by_name_paths, "/cards/named", urlencode({"fuzzy": needle})))
        exact = payload(dispatch(by_name_paths, "/cards/named", urlencode({"exact": needle})))
        assert fuzzy["id"] == exact["id"]

    def test_fuzzy_containment_leaves_out_an_emblem(self, by_name_paths: APIResource):
        """An emblem sharing a card's words neither answers containment nor makes the card ambiguous.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=lili last hope` and `teferi hero` answer
        the planeswalker although its emblem's name holds the same words.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", "fuzzy=hollowmere+ward"))
        assert body["name"] == WARDEN_NAME

    def test_fuzzy_containment_keeps_a_token(self, by_name_paths: APIResource):
        """A token stays in the pool: `mere` is carried by the card and the token alike.

        Not `hollowmere`, which the typo stage now answers before containment is asked: it scores
        10/18 against Hollowmere Warden and the token Hollowmere Sentry alike, over the floor, and
        a typo tie answers (the served card, here also the name that sorts last).
        """
        resp = dispatch(by_name_paths, "/cards/named", "fuzzy=mere")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["type"] == "ambiguous"
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=hollowmere"))["name"] == WARDEN_NAME

    @pytest.mark.parametrize("name", [WARDEN_ART_SERIES_NAME, "Hollowmere Warden Wisp", "Wisp"])
    def test_exact_never_answers_an_art_series(self, by_name_paths: APIResource, name):
        """No key of an art-series card is an `exact=` answer, its whole name included.

        Measured on api.scryfall.com 2026-09-25: `exact=Lightning Bolt // Lightning Bolt` and
        `exact=Minion of the Mighty // Kobold` are 404s.
        """
        resp = dispatch(by_name_paths, "/cards/named", urlencode({"exact": name}))
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["code"] == "not_found"

    @pytest.mark.parametrize("name", [WARDEN_ART_SERIES_NAME, "hollowmere warden wisp"])
    def test_fuzzy_spelling_an_art_series_answers_the_real_card(self, by_name_paths: APIResource, name):
        """An art series is out of the whole-name and typo stages too, so the real card answers.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=minion of the mighty kobold` answers the afr
        card Minion of the Mighty, and `fuzzy=lightning bolt // lightning bolt` Lightning Bolt.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", urlencode({"fuzzy": name})))
        assert body["name"] == WARDEN_NAME

    def test_fuzzy_words_only_an_art_series_carries_are_not_found(self, by_name_paths: APIResource):
        """`fuzzy=mighty kobold` is a 404 on api.scryfall.com (2026-09-25): not found, not ambiguous."""
        resp = dispatch(by_name_paths, "/cards/named", "fuzzy=warden+wisp")
        assert resp.status == falcon.HTTP_404
        assert "type" not in payload(resp)

    def test_an_english_name_outranks_a_printed_name_containing_the_words(self, by_name_paths: APIResource):
        """Printed names are containment's SECOND tier: `velmora` is the English card alone.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=austere` is Austere Command, not ambiguous
        with Dour Port-Mage's French name "Portmage austère".
        """
        body = payload(dispatch(by_name_paths, "/cards/named", "fuzzy=velmora"))
        assert body["id"] == TIER_SENTINEL_ID

    def test_an_english_name_containing_the_query_outranks_a_whole_printed_name(self, by_name_paths: APIResource):
        """Even a printed name that IS the query yields to an English name that contains it.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=inganno` is Wedding Announcement, not Guile,
        whose Italian printed name is "Inganno".
        """
        body = payload(dispatch(by_name_paths, "/cards/named", "fuzzy=tveilan"))
        assert body["id"] == TIER_ANNOUNCER_ID

    @pytest.mark.parametrize("needle", ["bound goad", "ego deriva"])
    def test_printed_names_answer_where_no_english_name_does(self, by_name_paths: APIResource, needle):
        """With no English name carrying the words, the printed tier answers, pooling both names.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=red goad` is the Portuguese Unmoored Ego.
        Three printed names carry `ego deriva`; the SHORTEST answers, though it scores lowest.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", urlencode({"fuzzy": needle})))
        assert body["id"] == TIER_EGO_PT_ID
        assert body["lang"] == "pt"

    @pytest.mark.parametrize("needle", ["Ego à Deriva", "ego a deriva"])
    def test_a_whole_printed_name_answers_its_foreign_printing(self, by_name_paths: APIResource, needle):
        """A printed name that IS the query answers that printing, through the engine path too.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=ego a deriva` is grn/212/pt and
        `fuzzy=blitzschlag` the German Lightning Bolt. The printing is not canonical, which the
        engine's by-id lookup does not reach, so the stage hands back the card it found.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", urlencode({"fuzzy": needle})))
        assert body["id"] == TIER_EGO_PT_ID
        assert body["lang"] == "pt"

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Zorvath, Hypersonic Queen", FLAVOR_QUEEN_ID),
            ("zorvathhypersonicqueen", FLAVOR_QUEEN_ID),
            ("Nuncheon 1:00 PM", FLAVOR_TOKEN_ID),
            ("Gearlord // Gearlord", FLAVOR_COLOSSUS_ID),
        ],
    )
    def test_exact_answers_a_flavor_name_with_its_printing(self, by_name_paths: APIResource, name, expected):
        """A flavor name is an `exact=` key, collated, extras included, and answers its printing.

        Measured on api.scryfall.com 2026-09-25: `exact=Mothra, Supersonic Queen` is Luminous
        Broodmoth prm/80915 (not its default printing), `exact=godzillaprimevalchampion` is Titanoth
        Rex prm/80925, `exact=Egg Pawn` a Myr token, and `exact=Megatron // Megatron` Blightsteel
        Colossus sld/1079, whose two faces each carry "Megatron".
        """
        body = payload(dispatch(by_name_paths, "/cards/named", urlencode({"exact": name})))
        assert body["id"] == expected

    def test_exact_flavor_name_honours_the_set(self, by_name_paths: APIResource):
        """`exact=Mothra, Supersonic Queen&set=iko` is iko/371 on api.scryfall.com, `set=2xm` a 404."""

        def exact(set_code: str) -> falcon.Response:
            return dispatch(by_name_paths, "/cards/named", urlencode({"exact": "Zorvath, Hypersonic Queen", "set": set_code}))

        assert payload(exact(FLAVOR_SET_CODE))["id"] == FLAVOR_QUEEN_ID
        assert exact(SET_CODE).status == falcon.HTTP_404

    def test_only_the_whole_flavor_name_is_an_exact_key(self, by_name_paths: APIResource):
        """The faces' names JOINED are the key, and neither one face's name nor a part of one is.

        Measured on api.scryfall.com 2026-09-25: `exact=Megatron // Megatron` is Blightsteel Colossus
        sld/1079 and `exact=Megatron` a 404.
        """
        assert payload(dispatch(by_name_paths, "/cards/named", urlencode({"exact": "Gearlord // Gearlord"})))["id"] == (
            FLAVOR_COLOSSUS_ID
        )
        for name in ("Gearlord", "Zorvath"):
            assert dispatch(by_name_paths, "/cards/named", urlencode({"exact": name})).status == falcon.HTTP_404, name

    def test_a_flavor_name_is_exacts_key_and_not_a_collection_identifiers(self, by_name_paths: APIResource):
        """`{"name":"Godzilla, King of the Monsters"}` is not_found where `exact=` answers Zilortha."""
        name = "Zorvath, Hypersonic Queen"
        assert payload(dispatch(by_name_paths, "/cards/named", urlencode({"exact": name})))["id"] == FLAVOR_QUEEN_ID
        body = payload(dispatch(by_name_paths, "/cards/collection", method="POST", body={"identifiers": [{"name": name}]}))
        assert body["data"] == []
        assert body["not_found"] == [{"name": name}]

    @pytest.mark.parametrize(
        ("needle", "expected"),
        [
            ("zorvath hypersonic queen", FLAVOR_QUEEN_ID),
            ("cinder hollow", FLAVOR_GLADE_ID),
            ("nuncheon 1:00 pm", FLAVOR_TOKEN_ID),
        ],
    )
    def test_fuzzy_whole_flavor_name_answers_its_printing(self, by_name_paths: APIResource, needle, expected):
        """A flavor name that IS the query answers first, over an oracle name containing it.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=mothra supersonic queen` is prm/80915,
        `fuzzy=egg pawn` a Myr token, and `fuzzy=bag end` Horizon Canopy ltc/366 rather than the
        oracle Bag End Porter, which only contains the words.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", urlencode({"fuzzy": needle})))
        assert body["id"] == expected

    @pytest.mark.parametrize(
        ("needle", "expected"),
        [
            ("hypersonic", FLAVOR_QUEEN_ID),
            ("ballista vexgrit", FLAVOR_INVADER_ID),
            ("invader rattle", FLAVOR_INVADER_ID),
            ("gearlord", FLAVOR_COLOSSUS_ID),
        ],
    )
    def test_fuzzy_containment_reads_flavor_names(self, by_name_paths: APIResource, needle, expected):
        """Containment's FIRST tier holds the flavor names, pooled with the card's oracle name.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=supersonic` is Luminous Broodmoth prm/80915,
        `ballista assaultron` Walking Ballista pip/352, `recyclops` a face's flavor name on sld/2169.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", urlencode({"fuzzy": needle})))
        assert body["id"] == expected

    @pytest.mark.parametrize("needle", ["vexgrit", "spindlecap"])
    def test_fuzzy_flavor_name_competes_as_an_equal(self, by_name_paths: APIResource, needle):
        """A flavor name makes containment ambiguous, with an oracle name or with another flavor name.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=assaultron` (the oracle Assaultron Dominator
        and Walking Ballista's "Assaultron Invader") and `cordyceps` (two cards' flavor names).
        """
        resp = dispatch(by_name_paths, "/cards/named", urlencode({"fuzzy": needle}))
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["type"] == "ambiguous"

    def test_an_oracle_name_carrying_the_words_keeps_its_own_printing(self, by_name_paths: APIResource):
        """Only a flavor name the words NEED picks the printing: `quellmoth` is the card's default.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=broodmoth supersonic` is prm/80915, and
        `fuzzy=luminous broodmoth mothra`, whose oracle name alone nearly spells it, iko/21.
        """
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=quellmoth+hypersonic"))["id"] == FLAVOR_QUEEN_ID
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=quellmoth"))["id"] == FLAVOR_QUEEN_DEFAULT_ID

    def test_an_extras_flavor_name_is_a_whole_name_key_but_no_containment_key(self, by_name_paths: APIResource):
        """A token's flavor name answers as a WHOLE name, and neither answers nor competes in containment.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=egg pawn` is the Myr token carrying it, while
        `fuzzy=lunch` is a 404 and `fuzzy=breakfast` is Second Breakfast, not ambiguous with the Food
        token "Breakfast 7:00 AM".
        """
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=nuncheon+1%3A00+pm"))["id"] == FLAVOR_TOKEN_ID
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=nuncheon"))["id"] == FLAVOR_NUNCHEON_ID
        assert dispatch(by_name_paths, "/cards/named", "fuzzy=1%3A00+pm").status == falcon.HTTP_404

    def test_fuzzy_a_strong_typo_winner_outranks_the_card_containing_every_word(self, by_name_paths: APIResource):
        """The typo stage runs BEFORE containment, even where one card carries every word.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=primeval titanoth` is Primeval Titan, although
        Titanoth Rex carries "titanoth" and its Godzilla printing's flavor name carries "primeval";
        and `fuzzy=arenare` is Arena, not Arena Rector. Both needles are in ROT13, as their names are.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", "fuzzy=cevzriny+gvgnabgu"))
        assert body["name"] == "Cevzriny Gvgna"
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=neraner"))["name"] == "Neran"

    def test_fuzzy_a_strong_typo_winner_outranks_several_cards_containing_every_word(
        self,
        by_name_paths: APIResource,
    ):
        """Several containing names do not make a strong typo winner `ambiguous`.

        Measured on api.scryfall.com 2026-09-25: `fuzzy=mind scul` is Mind Sculpt, although Jace, the
        Mind Sculptor contains both words too.
        """
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=zvaq+fphy"))["name"] == "Zvaq Fphycg"

    def test_fuzzy_under_the_floor_the_one_card_containing_every_word_answers(self, by_name_paths: APIResource):
        """No typo candidate over the floor, so containment answers; over it, the typo winner does.

        On api.scryfall.com (2026-09-25): `fuzzy=teferi hero` is Teferi, Hero of Dominaria, Teferi
        scoring 0.500; `fuzzy=hyd disintegrat` is HYDRA Disintegrator, Disintegrate scoring 0.474;
        `fuzzy=arenare` is Arena at 0.625, over Arena Rector; and `fuzzy=inquisitor serr` is
        Inquisitor's Ox at 0.611, over Serra Inquisitors, which contains both words. Word-split
        pg_trgm scored Serra Inquisitors highest itself, so the SQL path answered it until both
        lanes scored the collated name.
        """
        body = payload(dispatch(by_name_paths, "/cards/named", "fuzzy=grsrev+ureb"))
        assert body["name"] == "Grsrev, Ureb bs Qbzvanevn"
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=ulq+qvfvagrteng"))["name"] == "ULQEN Qvfvagrtengbe"
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=neraner"))["name"] == "Neran"
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=vadhvfvgbe+free"))["name"] == "Vadhvfvgbe'f Bk"

    def test_fuzzy_word_order_counts_and_several_containing_names_are_ambiguous(self, by_name_paths: APIResource):
        """The collated name keeps word order, on both lanes; under the floor several names tie.

        On api.scryfall.com (2026-09-25), `fuzzy=bolt lightning` is Blightning at 9/16, although
        Lightning Bolt (9/19) and "Emeritus of Conflict // Lightning Bolt" both contain the words --
        word-split pg_trgm, which the SQL path scored before, rated Lightning Bolt 1.0 -- and
        `fuzzy=assaultron` is ambiguous: Assault Drone scores 0.500, under the floor, and two names
        contain the word, one a flavor name.
        """
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=obyg+yvtugavat"))["name"] == "Oyvtugavat"
        resp = dispatch(by_name_paths, "/cards/named", "fuzzy=nffnhygeba")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["type"] == "ambiguous"

    def test_fuzzy_a_tie_answers_the_card_first_printed_last(self, by_name_paths: APIResource):
        """A typo tie is never `ambiguous`: the newer card answers, then the name that sorts last.

        Measured on api.scryfall.com 2026-09-26: `fuzzy=thoughts` is Thought Scour (first printed
        2012) over Thoughtseize (2007), and `fuzzy=parallax` is Parallax Wave over Parallax Tide,
        which share Nemesis's release day. Each pair scores 8/14 here as there.
        """
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=gubhtugf"))["name"] == "Gubhtug Fpbhe"
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=cnenyynk"))["name"] == "Cnenyynk Jnir"

    def test_fuzzy_the_floor_sits_between_two_probes(self, by_name_paths: APIResource):
        """0.55, bracketed on api.scryfall.com (2026-09-26) by a 404 and an answer.

        `fuzzy=deadeall` scores 6/11 = 0.545 against Deadfall and is a 404; `fuzzy=lightning blow`
        scores 5/9 = 0.556 against Lightning Bolt and answers it -- and does inside a set too, which
        is the SQL lane on both paths, as `fuzzy=lightning blast&set=m11` (0.526) is a 404.
        """
        assert dispatch(by_name_paths, "/cards/named", "fuzzy=qrnqrnyy").status == falcon.HTTP_404
        assert payload(dispatch(by_name_paths, "/cards/named", "fuzzy=yvtugavat+oybj"))["name"] == "Yvtugavat Obyg"
        in_set = f"fuzzy=yvtugavat+oybj&set={ORDER_SET_CODE}"
        assert payload(dispatch(by_name_paths, "/cards/named", in_set))["name"] == "Yvtugavat Obyg"
        blast = f"fuzzy=yvtugavat+oynfg&set={ORDER_SET_CODE}"
        assert dispatch(by_name_paths, "/cards/named", blast).status == falcon.HTTP_404

    def test_neither_parameter_is_a_400(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named")
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["code"] == "bad_request"

    def test_text_format_renders_plain_text(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/named", "exact=Compat+Bolt&format=text")
        assert resp.content_type.startswith("text/plain")
        assert resp.text.splitlines()[0] == "Compat Bolt {R}"

    def test_image_format_redirects_to_the_image(self, compat_corpus: APIResource):
        with pytest.raises(falcon.HTTPFound) as found:
            dispatch(compat_corpus, "/cards/named", "exact=Compat+Bolt&format=image&version=png")
        assert "/png/" in found.value.headers["location"]

    def test_image_format_honors_the_back_face(self, compat_corpus: APIResource):
        with pytest.raises(falcon.HTTPFound) as found:
            dispatch(compat_corpus, "/cards/named", "exact=Compat+Delver&format=image&face=back&version=large")
        # Derived from the card id, not read from the fixture: image URLs are a pure function of
        # the id, so the store carries none of them.
        assert (
            found.value.headers["location"] == "https://cards.scryfall.io/large/back/3/3/33333333-3333-4333-8333-333333333333.jpg"
        )


class TestNameKeyRule:
    """The keys a card answers to, on `named?exact=` and on a collection `{"name"}` identifier.

    THE TWO ARE NOT THE SAME LOOKUP. Measured against api.scryfall.com on 2026-08-31, ONE
    IDENTIFIER PER REQUEST -- a collection response's `data` is not in identifier order, and a
    batched probe attributes its answers to the wrong needles, which is how the first reading of
    this rule came out wrong:

      {"name":"Delver of Secrets"}                   -> Delver of Secrets // Insectile Aberration
      {"name":"Insectile Aberration"}                -> the same card (a BACK face names it)
      {"name":"Delver of Secrets // Insectile ..."}  -> not_found, where `exact=` is the card
      {"name":"Fire // Ice"} / {"Wear // Tear"} /
      {"name":"Bonecrusher Giant // Stomp"}          -> not_found, all three
      {"name":"Who // What // When // Where // Why"} -> und/75, a FIVE-part name IS a key
      {"name":"Who"}                                 -> not_found, and so is `exact=Who`
      {"name":"Elves"}                               -> Elves (ffdn/9), a KEY and not a substring
      {"name":"limduls vault"}                       -> Lim-Dûl's Vault, collated
      {"name":"  Lightning Bolt  "}                  -> Lightning Bolt, trimmed
      {"name":"Delver of Secrets","set":"mid"}       -> mid/47, set FILTERS the lookup

    Every case runs TWICE, through `by_name_paths`: once with the engine serving and once with it
    gated off so the SQL fallback answers. The fallback is what a worker with no store loaded uses,
    so a key rule expressed in only one of the two is a rule this API applies only sometimes.
    """

    @staticmethod
    def _collection(api: APIResource, name: str, set_code: str | None = None) -> dict:
        identifier: dict = {"name": name}
        if set_code:
            identifier["set"] = set_code
        return payload(dispatch(api, "/cards/collection", method="POST", body={"identifiers": [identifier]}))

    @staticmethod
    def _resolves(api: APIResource, name: str, set_code: str | None = None) -> str | None:
        """The id a collection identifier resolves to, or None for not_found."""
        body = TestNameKeyRule._collection(api, name, set_code)
        return body["data"][0]["id"] if body["data"] else None

    @staticmethod
    def _exact(api: APIResource, name: str) -> str | None:
        """The id `named?exact=name` resolves to, or None for a 404."""
        resp = dispatch(api, "/cards/named", urlencode({"exact": name}))
        return None if resp.status == falcon.HTTP_404 else payload(resp)["id"]

    @pytest.mark.parametrize("name", ["Compat Delver", "Compat Aberration"])
    def test_either_face_names_the_card_on_both_surfaces(self, by_name_paths: APIResource, name):
        """The BACK face is the half the predicate this replaced never looked at.

        It compared `split_part(card_name, ' // ', 1)` and the whole name, so
        `{"name":"Compat Aberration"}` missed a card api.scryfall.com resolves.
        """
        assert self._resolves(by_name_paths, name) == DELVER_ID
        assert self._exact(by_name_paths, name) == DELVER_ID

    def test_the_joined_name_is_exacts_key_and_not_a_collection_identifiers(self, by_name_paths: APIResource):
        """The one key the two surfaces disagree about."""
        joined = "Compat Delver // Compat Aberration"
        assert self._exact(by_name_paths, joined) == DELVER_ID
        assert self._resolves(by_name_paths, joined) is None

    def test_a_five_part_name_is_a_key_and_none_of_its_parts_is(self, by_name_paths: APIResource):
        """EXACTLY two halves is what makes a face key, which `split_part` cannot say on its own."""
        assert self._resolves(by_name_paths, WHO_NAME) == WHO_ID
        assert self._exact(by_name_paths, WHO_NAME) == WHO_ID
        for part in WHO_NAME.split(" // "):
            assert self._resolves(by_name_paths, part) is None, part
            assert self._exact(by_name_paths, part) is None, part

    @pytest.mark.parametrize(
        "spelling",
        [
            "Compat Lim-Dûl's Vault",
            "Compat Lim-Dul's Vault",
            "compat limduls vault",
            "compat lim-duls vault",
            "compatlimdulsvault",
            "COMPAT LIMDULS VAULT",
        ],
    )
    def test_names_compare_collated(self, by_name_paths: APIResource, spelling):
        """Accent, apostrophe, hyphen and spacing all drop out of the comparison."""
        assert self._resolves(by_name_paths, spelling) == VAULT_ID
        assert self._exact(by_name_paths, spelling) == VAULT_ID

    def test_a_collated_face_does_not_straddle_the_join(self, by_name_paths: APIResource):
        """The halves are split BEFORE either side is collated, so `" // "` is not just punctuation.

        Collating first would delete the join and let `compatdelvercompataberration` -- and worse,
        a needle spanning it -- read as a face.
        """
        assert self._resolves(by_name_paths, "compatdelver") == DELVER_ID
        assert self._resolves(by_name_paths, "delvercompataberration") is None
        assert self._resolves(by_name_paths, "compatdelvercompataberration") is None
        assert self._exact(by_name_paths, "compatdelvercompataberration") == DELVER_ID

    def test_leading_and_trailing_whitespace_is_not_part_of_the_name(self, by_name_paths: APIResource):
        """`{"name":"  Lightning Bolt  "}` resolves on api.scryfall.com; this compared it as posted."""
        assert self._resolves(by_name_paths, "  Compat Bolt  ") == BOLT_ID

    @pytest.mark.parametrize("needle", ["Compat", "Bolt", "Vault", "", "   "])
    def test_a_substring_of_a_name_is_not_a_key(self, by_name_paths: APIResource, needle):
        """A name lookup, not a search.

        `{"name":"Elves"}` answers *Elves* (ffdn/9) on api.scryfall.com -- the card actually named
        that, not one of the hundreds whose names contain the word, which is what a containment
        lookup cut to one row would have answered.
        """
        assert self._resolves(by_name_paths, needle) is None

    def test_set_filters_the_lookup(self, by_name_paths: APIResource):
        """`{"name":"Delver of Secrets","set":"mid"}` answers mid/47 -- the card, in the set asked for.

        A card with no printing in that set drops out of the lookup rather than answering another
        printing, which is the same thing `exact=&set=` does.
        """
        assert self._resolves(by_name_paths, "Compat Bolt", SET_CODE) == BOLT_ID
        assert self._resolves(by_name_paths, "Compat Bolt", NAME_SET_CODE) is None
        assert self._resolves(by_name_paths, VAULT_NAME, NAME_SET_CODE) == VAULT_ID

    def test_an_unresolvable_name_comes_back_in_not_found(self, by_name_paths: APIResource):
        """Reported, not dropped: the client has to be able to tell which identifier failed."""
        body = self._collection(by_name_paths, "Compat Delver // Compat Aberration")
        assert body["data"] == []
        assert body["not_found"] == [{"name": "Compat Delver // Compat Aberration"}]


class TestAutocomplete:
    """GET /cards/autocomplete."""

    def test_returns_a_catalog(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/autocomplete", "q=Compat+Bol"))
        assert body["object"] == "catalog"
        assert "Compat Bolt" in body["data"]
        assert body["total_values"] == len(body["data"])

    def test_short_queries_return_an_empty_catalog(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, "/cards/autocomplete", "q=C"))["data"] == []

    def test_at_most_twenty_values(self, compat_corpus: APIResource):
        assert len(payload(dispatch(compat_corpus, "/cards/autocomplete", "q=co"))["data"]) <= 20


class TestRandom:
    """GET /cards/random."""

    def test_returns_a_card(self, compat_corpus: APIResource):
        """Unrestricted, so it can draw any row in the session-shared database -- assert on id only.

        The `object` key comes from whatever the drawn row's blob holds, and other modules insert
        fixtures that never had one; `test_query_restricts_the_draw` pins the full shape.
        """
        body = payload(dispatch(compat_corpus, "/cards/random"))
        assert uuid.UUID(body["id"])

    def test_query_restricts_the_draw(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/random", "q=%21%22Compat+Bolt%22"))
        assert body["object"] == "card"
        assert body["id"] == BOLT_ID

    def test_query_matching_nothing_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/random", "q=%21%22No+Such+Compat+Card%22")
        assert resp.status == falcon.HTTP_404

    # ── the extras gate, which this route ran without ────────────────────────
    #
    # A DRAW, so the assertions are built to be deterministic rather than sampled: the query
    # matches the extras printing and nothing else, so the gate turns the whole match set empty
    # and the answer is the 404 every time. Measured on api.scryfall.com 2026-08-17 with the same
    # shape — `/cards/random?q=t:goblin cmc=0` (no trigger, all extras) is 404 there, and
    # `&include_extras=true` returns q07/T12.

    def test_the_extras_class_is_hidden_from_the_draw(self, compat_corpus: APIResource):
        """`!"…"` fires no trigger, so this is the default lane, and the class is excluded in it."""
        resp = dispatch(compat_corpus, "/cards/random", "q=%21%22Compat+Substitute%22")
        assert resp.status == falcon.HTTP_404

    def test_include_extras_true_draws_the_extra(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards/random", "q=%21%22Compat+Substitute%22&include_extras=true"))
        assert body["id"] == EXTRA_ID

    def test_a_trigger_term_draws_it_without_the_flag(self, compat_corpus: APIResource):
        """`is:extra` is an unconditional trigger, so the gate opens on the term alone.

        The same rule `/cards/search` runs, read with the same helper rather than a second copy of
        it — a query that names the class can never answer nothing.
        """
        body = payload(dispatch(compat_corpus, "/cards/random", "q=is%3Aextra&include_extras=false"))
        assert body["id"] in {EXTRA_ID, POOL_EMBLEM_ID, POOL_TOKEN_ID, POOL_ART_SERIES_ID, FLAVOR_TOKEN_ID, FACE_CHAOS_ID}

    @needs_oracleid
    def test_an_oracleid_draws_a_card_whose_every_printing_is_an_extra(self, by_name_paths: APIResource):
        """The draw shares the gate, so `oracleid:` opens it here too -- Mechtitan is not a 404."""
        query = urlencode({"q": f"oracleid:{EXTRA_ORACLE_ID}", "include_extras": "false"})
        assert payload(dispatch(by_name_paths, "/cards/random", query))["id"] == EXTRA_ID

    def test_a_set_term_on_an_extras_set_is_the_conditional_trigger(self, compat_corpus: APIResource, monkeypatch):
        """The one trigger that asks the store: a set term enables extras iff that set holds one.

        The table is stubbed for the same reason the `/cards/search` twin stubs it — it is folded
        into the archive at build, so a fixture engine is not the thing under test here.
        """
        monkeypatch.setattr(compat_corpus, "_sets_with_extras", lambda: frozenset({EXTRAS_SET_CODE}))
        body = payload(dispatch(compat_corpus, "/cards/random", f"q=e%3A{EXTRAS_SET_CODE}"))
        assert body["id"] == EXTRA_ID

    def test_repeated_draws_are_not_all_the_same_card(self, compat_corpus: APIResource, monkeypatch):
        """The draw's SQL text and parameters never vary, so a memoized result would pin one card.

        The query cache holds a single entry when caching is off, which is how the suite runs — the
        count query and the draw evict each other and the bug hides. So this installs the
        multi-entry cache production has. Twenty draws over a corpus of at least three cards
        collide on one id with probability below (1/3)**19, so one distinct id here means the
        result was replayed rather than redrawn.
        """
        monkeypatch.setattr(
            compat_corpus,
            "_query_cache",
            GenerationCache(factory=lambda: LRUCache(maxsize=1_000), generation=compat_corpus.app_context.cache_generation),
        )
        drawn = {payload(dispatch(compat_corpus, "/cards/random"))["id"] for _ in range(20)}
        assert len(drawn) > 1

    def test_the_response_is_marked_uncacheable(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards/random")
        assert "no-store" in resp.get_header("Cache-Control")


class TestByIdentifier:
    """GET /cards/:id and the external-id namespaces."""

    def test_scryfall_id(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{BOLT_ID}"))["id"] == BOLT_ID

    def test_unknown_scryfall_id_is_a_404(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, f"/cards/{uuid.uuid4()}")
        assert resp.status == falcon.HTTP_404
        assert payload(resp)["object"] == "error"

    def test_a_segment_that_is_not_a_uuid_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, "/cards/not-an-id").status == falcon.HTTP_404

    @pytest.mark.parametrize(
        ("namespace", "external_id"),
        [("multiverse", 900001), ("mtgo", 900002), ("arena", 900003), ("tcgplayer", 900004), ("cardmarket", 900005)],
    )
    def test_external_id_namespaces(self, compat_corpus: APIResource, namespace, external_id):
        assert payload(dispatch(compat_corpus, f"/cards/{namespace}/{external_id}"))["id"] == BOLT_ID

    def test_unknown_external_id_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, "/cards/mtgo/999999999").status == falcon.HTTP_404

    def test_non_numeric_external_id_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, "/cards/mtgo/abc").status == falcon.HTTP_404

    def test_set_code_and_collector_number(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{SET_CODE}/1"))["id"] == BOLT_ID

    def test_set_code_is_case_insensitive(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{SET_CODE.upper()}/1"))["id"] == BOLT_ID

    def test_language_segment_selects_english_by_default(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{SET_CODE}/1/en"))["id"] == BOLT_ID

    def test_a_language_the_printing_lacks_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, f"/cards/{SET_CODE}/1/ja").status == falcon.HTTP_404

    def test_an_address_no_english_printing_carries_falls_through_to_the_printing_that_does(self, compat_corpus: APIResource):
        """`/cards/hoc/95` is the Dwarvish Arcane Signet on api.scryfall.com, measured 2026-09-11.

        There is no English hoc/95. The segment-less form answers the printing that exists; the
        hardcoded `en` it replaced answered a 404 that `/cards/hoc/95/dw` contradicted.
        """
        card = payload(dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/1"))
        assert (card["id"], card["lang"]) == (SIGNET_ID, "dw")

    def test_english_answers_the_segment_less_form_even_when_a_foreign_row_outranks_it(self, compat_corpus: APIResource):
        """The fall-through is for an address with NO English row; it never displaces one that has.

        The premise comes first: under the lookup's own ordering the Japanese row is FIRST at this
        address, so a plain address lookup answers it and the assertion after cannot pass by an
        accident of ordering.
        """
        plain = compat_corpus._fetch_one_card(
            "lower(card_set_code) = %(set_code)s AND collector_number = %(number)s",
            {"set_code": LANG_SET_CODE, "number": "2"},
        )
        assert plain["id"] == AMBER_JA_ID
        assert payload(dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/2"))["id"] == AMBER_EN_ID

    def test_a_named_language_selects_that_row_where_two_share_the_address(self, compat_corpus: APIResource):
        assert payload(dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/2/ja"))["id"] == AMBER_JA_ID

    @pytest.mark.parametrize(
        ("number", "lang"),
        [("1", "en"), ("2", "de")],
        ids=["english-of-a-foreign-only-address", "a-language-nobody-printed"],
    )
    def test_a_named_language_no_printing_carries_is_still_a_404(self, compat_corpus: APIResource, number, lang):
        """`/cards/m15/18/de` is a 404 on api.scryfall.com: only the ABSENT segment falls through."""
        assert dispatch(compat_corpus, f"/cards/{LANG_SET_CODE}/{number}/{lang}").status == falcon.HTTP_404

    def test_text_format_on_a_path_lookup(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, f"/cards/{BOLT_ID}", "format=text")
        assert resp.text.splitlines()[0] == "Compat Bolt {R}"


class TestMultiFaceFidelity:
    """A multi-face printing comes back as the card Scryfall sent, not as one of its faces."""

    def test_the_card_object_carries_its_faces(self, compat_corpus: APIResource):
        card = payload(dispatch(compat_corpus, f"/cards/{DELVER_ID}"))
        assert card["object"] == "card"
        assert card["name"] == "Compat Delver // Compat Aberration"
        assert [face["name"] for face in card["card_faces"]] == ["Compat Delver", "Compat Aberration"]

    def test_no_importer_key_reaches_the_payload(self, compat_corpus: APIResource):
        """The blob is the card, but not byte-for-byte: three internal keys have to come back off."""
        card = payload(dispatch(compat_corpus, f"/cards/{DELVER_ID}"))
        assert not {"card_name", "face_name", "face_idx", "raw_card_blob"} & set(card)

    def test_text_format_renders_both_faces(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, f"/cards/{DELVER_ID}", "format=text")
        assert "Compat Delver {U}" in resp.text
        assert "Compat Aberration" in resp.text


class TestCollection:
    """POST /cards/collection."""

    def test_resolves_mixed_identifiers(self, compat_corpus: APIResource):
        body = payload(
            dispatch(
                compat_corpus,
                "/cards/collection",
                method="POST",
                body={"identifiers": [{"id": BOLT_ID}, {"name": "Compat Bears"}, {"set": SET_CODE, "collector_number": "3"}]},
            ),
        )
        assert body["object"] == "list"
        assert {card["id"] for card in body["data"]} == {BOLT_ID, BEAR_ID, DELVER_ID}
        assert body["not_found"] == []

    def test_unresolvable_identifiers_are_reported(self, compat_corpus: APIResource):
        body = payload(
            dispatch(compat_corpus, "/cards/collection", method="POST", body={"identifiers": [{"name": "Nothing At All Compat"}]}),
        )
        assert body["data"] == []
        assert body["not_found"] == [{"name": "Nothing At All Compat"}]

    def test_oracle_id_identifier(self, compat_corpus: APIResource):
        body = payload(
            dispatch(compat_corpus, "/cards/collection", method="POST", body={"identifiers": [{"oracle_id": BOLT_ORACLE_ID}]}),
        )
        assert body["data"][0]["id"] == BOLT_ID

    def test_set_and_number_falls_through_to_a_foreign_only_row_beside_an_english_answer(self, compat_corpus: APIResource):
        """`{"set":"hoc","collector_number":"95"}` finds the Dwarvish row with an empty not_found.

        Measured on api.scryfall.com 2026-09-11. In the same batch, an address that has an English
        printing answers the English one, whatever a foreign row sharing the number would score.
        """
        body = payload(
            dispatch(
                compat_corpus,
                "/cards/collection",
                method="POST",
                body={
                    "identifiers": [
                        {"set": LANG_SET_CODE, "collector_number": "1"},
                        {"set": LANG_SET_CODE, "collector_number": "2"},
                    ],
                },
            ),
        )
        assert {card["id"] for card in body["data"]} == {SIGNET_ID, AMBER_EN_ID}
        assert body["not_found"] == []

    def test_over_the_limit_is_scryfalls_400_not_a_422(self, compat_corpus: APIResource):
        """`400 bad_request` with Scryfall's own sentence, measured 2026-08-16.

        This asserted a `422 validation_error` with wording of its own, so a client string-matching
        Scryfall's message saw neither the status nor the text it was matching on.
        """
        resp = dispatch(
            compat_corpus,
            "/cards/collection",
            method="POST",
            body={"identifiers": [{"id": BOLT_ID}] * 76},
        )
        assert resp.status == falcon.HTTP_400
        body = payload(resp)
        assert body["code"] == "bad_request"
        assert body["details"] == "The `identifiers` list must have at least 1 and no more than 75 references."

    def test_a_body_without_identifiers_is_the_count_sentence(self, compat_corpus: APIResource):
        """An ABSENT list reads as an empty one, so it earns the count sentence rather than its own."""
        resp = dispatch(compat_corpus, "/cards/collection", method="POST", body={"cards": []})
        assert resp.status == falcon.HTTP_400
        assert payload(resp)["details"] == "The `identifiers` list must have at least 1 and no more than 75 references."

    def test_get_is_not_allowed(self, compat_corpus: APIResource):
        with pytest.raises(falcon.HTTPMethodNotAllowed):
            dispatch(compat_corpus, "/cards/collection")


class TestRulings:
    """The five rulings routes."""

    def test_rulings_by_scryfall_id(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, f"/cards/{BOLT_ID}/rulings"))
        assert body["object"] == "list"
        assert body["data"][0]["object"] == "ruling"

    def test_rulings_are_newest_first_like_scryfalls(self, compat_corpus: APIResource):
        """The order api.scryfall.com serves: `published_at` descending.

        Measured against it on 2026-08-12 -- 16 of 16 cards whose rulings span more than one date
        came back newest-first, 0 oldest-first. The ascending sort this replaced inverted every one
        of them, which is the opposite of what a compatibility surface is for.

        The order WITHIN one date is `comment` only because something has to be deterministic:
        Scryfall breaks that tie with an internal ruling id the bulk file does not carry.
        """
        body = payload(dispatch(compat_corpus, f"/cards/{BOLT_ID}/rulings"))
        assert [(row["published_at"], row["comment"]) for row in body["data"]] == [
            ("2021-02-05", "A later clarification."),
            ("2021-02-05", "Zero damage is still damage."),
            ("2004-10-04", "Any target means any target."),
            ("2004-10-04", "Any target means any target."),
        ]

    def test_a_repeated_ruling_is_served_as_often_as_scryfall_serves_it(self, compat_corpus: APIResource):
        """The bulk file's verbatim repeats are rulings, not export noise.

        Measured 2026-09-25: the file repeats 37 whole tuples across 11 cards, and api.scryfall.com
        answered all 11 with every repeat in place -- Varis, Silverymoon Ranger 21 rulings of which
        11 are distinct, Expand the Sphere 12 of 6. A unique index on the tuple dropped all 37.
        """
        body = payload(dispatch(compat_corpus, f"/cards/{BOLT_ID}/rulings"))
        comments = [row["comment"] for row in body["data"]]
        assert comments.count("Any target means any target.") == 2

    def test_rulings_by_set_and_collector_number(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, f"/cards/{SET_CODE}/1/rulings"))
        assert len(body["data"]) == 4  # three rulings, one of them served twice

    @pytest.mark.parametrize(
        ("namespace", "external_id"),
        [("multiverse", 900001), ("mtgo", 900002), ("arena", 900003)],
    )
    def test_rulings_by_external_id(self, compat_corpus: APIResource, namespace, external_id):
        body = payload(dispatch(compat_corpus, f"/cards/{namespace}/{external_id}/rulings"))
        assert len(body["data"]) == 4  # three rulings, one of them served twice

    def test_a_card_with_no_rulings_returns_an_empty_list(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, f"/cards/{BEAR_ID}/rulings"))
        assert body["data"] == []
        assert body["has_more"] is False

    @pytest.mark.parametrize(
        ("path", "details"),
        [
            # The path addresses nothing at all.
            ("/cards/not-a-uuid", "The requested object or REST method was not found."),
            ("/cards/multiverse", "The requested object or REST method was not found."),
            # A well-formed address that resolves to no card.
            ("/cards/00000000-0000-4000-8000-000000000000", "No card found with the given ID or set code and collector number."),
            ("/cards/multiverse/99999999", "No card found with the given ID or set code and collector number."),
            # `/cards/<x>/rulings` where x is not an id reads as a set code and a collector number
            # called "rulings", so Scryfall answers the CARD miss rather than the rulings one.
            ("/cards/not-a-uuid/rulings", "No card found with the given ID or set code and collector number."),
            # The rulings shapes.
            (
                "/cards/00000000-0000-4000-8000-000000000000/rulings",
                "No card found with the given ID, multiverse ID, or set code & collector number.",
            ),
            (
                "/cards/multiverse/99999999/rulings",
                "No card found with the given ID, multiverse ID, or set code & collector number.",
            ),
            ("/cards/zzz/999/rulings", "No card found with the given ID, multiverse ID, or set code & collector number."),
        ],
    )
    def test_a_miss_carries_the_body_scryfall_words_for_that_shape(
        self, compat_corpus: APIResource, path: str, details: str
    ) -> None:
        """Three bodies, not one, and none of them the string this used to send.

        Captured from api.scryfall.com on 2026-08-12. Nothing pinned `details` before -- the route
        tests asserted the status and the code and left the field a client string-matches alone,
        which is exactly how a compatibility surface drifts on it.
        """
        response = dispatch(compat_corpus, path)
        assert response.status == falcon.HTTP_404
        assert payload(response)["details"] == details

    def test_rulings_for_an_unknown_card_is_a_404(self, compat_corpus: APIResource):
        assert dispatch(compat_corpus, f"/cards/{uuid.uuid4()}/rulings").status == falcon.HTTP_404


class TestAllCards:
    """GET /cards, the unfiltered listing."""

    def test_first_page_is_a_list_object(self, compat_corpus: APIResource):
        body = payload(dispatch(compat_corpus, "/cards"))
        assert body["object"] == "list"
        assert body["total_cards"] >= 3
        assert len(body["data"]) <= PAGE_SIZE

    def test_a_page_past_the_end_is_a_422(self, compat_corpus: APIResource):
        resp = dispatch(compat_corpus, "/cards", "page=100000")
        assert resp.status == falcon.HTTP_422
        assert payload(resp)["code"] == "validation_error"

    def test_a_non_positive_page_serves_page_one(self, compat_corpus: APIResource):
        """The same never-rejecting `page` as /cards/search -- one rule, because it is one parameter."""
        resp = dispatch(compat_corpus, "/cards", "page=0")
        assert resp.status == falcon.HTTP_200
        assert payload(resp)["object"] == "list"

    def test_next_page_addresses_this_host(self, compat_corpus: APIResource, monkeypatch):
        """The listing builds its own next_page, on a path the search route's tests never reach.

        Page size shrunk to 1 so a second page exists over the session's corpus rather than
        needing 176 rows.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = payload(dispatch(compat_corpus, "/cards"))
        assert body["has_more"] is True
        assert body["next_page"] == "http://falconframework.org/cards?page=2"

    # A Japanese printing of Compat Bolt, inserted by one test and removed again.
    FOREIGN_BOLT_ID = "9f9f9f9f-1111-4111-8111-9f9f9f9f9f9f"

    @staticmethod
    def _every_page(api: APIResource) -> tuple[list[dict], dict]:
        """Walk the listing from page 1 by `has_more`; return every card and the last page."""
        listed: list[dict] = []
        page = 1
        while True:
            body = payload(dispatch(api, "/cards", f"page={page}"))
            listed.extend(body["data"])
            if not body["has_more"]:
                return listed, body
            page += 1

    def test_a_listed_card_carries_its_fields(self, compat_corpus: APIResource):
        """The cards in the listing are whole cards, not empty objects in a valid envelope.

        The listing passed `magic.cards` rows straight to `to_scryfall_card`, which reads the
        engine's field names (`name`, `set_code`, `mana_cost`), not the table's (`card_name`,
        `card_set_code`, `mana_cost_text`). Every card came out with an empty name and set, and
        without its mana cost, rarity, legalities, prices or marketplace ids. The tests above check
        only the envelope, so they passed.
        """
        listed, _ = self._every_page(compat_corpus)
        owned = [card for card in listed if card.get("id") in {BOLT_ID, BEAR_ID, DELVER_ID, WHO_ID, VAULT_ID}]
        assert len(owned) == 5
        assert all(card["name"] and card["set"] and card["collector_number"] for card in owned)
        bolt = next(card for card in listed if card.get("id") == BOLT_ID)
        assert (bolt["name"], bolt["set"], bolt["collector_number"]) == ("Compat Bolt", SET_CODE, "1")
        assert (bolt["oracle_id"], bolt["mana_cost"], bolt["multiverse_ids"]) == (BOLT_ORACLE_ID, "{R}", [900001])
        assert bolt["prices"]["usd"] == "0.10"
        assert bolt["image_uris"]["normal"].endswith(f"/{BOLT_ID}.jpg")
        delver = next(card for card in listed if card.get("id") == DELVER_ID)
        assert [face["name"] for face in delver["card_faces"]] == ["Compat Delver", "Compat Aberration"]

    def test_a_listed_card_is_the_object_its_id_answers(self, by_name_paths: APIResource, monkeypatch):
        """Each card on every page is the same object `/cards/:id` returns, on both paths.

        Comparing whole objects catches any field the listing renders differently, not only the
        few named above. A page size of 2 puts page 2 and later under the comparison, and the walk
        checks that no card is listed twice or skipped. Only this module's cards are compared by
        id: the database is shared, and another module's cards are not guaranteed to be in the
        store the engine path reads.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 2)
        listed, last = self._every_page(by_name_paths)
        listed_ids = [card.get("id") for card in listed]
        assert len(listed) > 2
        assert last["total_cards"] == len(listed) == len(set(listed_ids))
        owned = {BOLT_ID, BEAR_ID, DELVER_ID, WHO_ID, VAULT_ID}
        assert owned <= set(listed_ids)
        for card in listed:
            if card["id"] in owned:
                assert card == payload(dispatch(by_name_paths, f"/cards/{card['id']}"))

    def test_a_foreign_printing_is_not_listed(self, compat_corpus: APIResource):
        """The listing is the canonical printings, the default of every other lane on this branch.

        magic.cards holds every language since all_cards became the feed: about 540k rows, where
        default_cards is about 114k. Without `is_canonical` the listing grew by every foreign
        printing, and the engine's by-id lookup, which reads canonical printings only, does not
        find those cards.
        """
        before, _ = self._every_page(compat_corpus)
        foreign = _bolt() | {"id": self.FOREIGN_BOLT_ID, "lang": "ja", "printed_name": "コンパット・ボルト"}
        compat_corpus.admin._upsert_cards([foreign], canonical_ids=set())
        compat_corpus.admin._clear_caches()
        try:
            listed, last = self._every_page(compat_corpus)
        finally:
            with compat_corpus.app_context.writer_pool.connection() as conn:
                conn.execute("DELETE FROM magic.cards WHERE scryfall_id = %s", (self.FOREIGN_BOLT_ID,))
            compat_corpus.admin._clear_caches()
        assert self.FOREIGN_BOLT_ID not in {card.get("id") for card in listed}
        assert last["total_cards"] == len(listed) == len(before)


class TestThroughTheFullApp:
    """The same routes through a real Falcon app, which `_handle` alone does not exercise.

    `_handle` leaves `resp.media` for the framework to serialize and lets a redirect propagate as an
    exception. Only a full app resolves the media handler against the `application/json;
    charset=utf-8` content type these routes set, and only a full app turns `format=image` into an
    actual 302.
    """

    def _client(self, api: APIResource) -> falcon.testing.TestClient:
        app = falcon.App()
        app.add_sink(api._handle, prefix="/")
        return falcon.testing.TestClient(app)

    def test_a_card_serializes_over_the_wire(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get(f"/cards/{BOLT_ID}")
        assert result.status_code == 200
        assert result.headers["content-type"] == "application/json; charset=utf-8"
        assert orjson.loads(result.content)["id"] == BOLT_ID

    def test_a_search_serializes_over_the_wire(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get("/cards/search", params={"q": '!"Compat Bolt"'})
        assert result.status_code == 200
        assert orjson.loads(result.content)["object"] == "list"

    def test_an_error_carries_its_status_over_the_wire(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get("/cards/named", params={"exact": "Compat Nothing"})
        assert result.status_code == 404
        assert orjson.loads(result.content) == {
            "object": "error",
            "code": "not_found",
            "status": 404,
            "details": "No cards found matching “Compat Nothing”",
        }

    def test_image_format_is_a_302(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_get(
            "/cards/named",
            params={"exact": "Compat Delver", "format": "image", "face": "back"},
        )
        assert result.status_code == 302
        assert result.headers["location"] == "https://cards.scryfall.io/large/back/3/3/33333333-3333-4333-8333-333333333333.jpg"

    def test_head_is_accepted_wherever_get_is(self, compat_corpus: APIResource):
        assert self._client(compat_corpus).simulate_head(f"/cards/{BOLT_ID}").status_code == 200

    def test_collection_round_trips_a_posted_body(self, compat_corpus: APIResource):
        result = self._client(compat_corpus).simulate_post(
            "/cards/collection",
            json={"identifiers": [{"id": BOLT_ID}, {"name": "Nothing At All Compat"}]},
        )
        assert result.status_code == 200
        body = orjson.loads(result.content)
        assert [card["id"] for card in body["data"]] == [BOLT_ID]
        assert body["not_found"] == [{"name": "Nothing At All Compat"}]

    def test_collection_returns_one_entry_per_identifier_including_duplicates(self, compat_corpus: APIResource):
        """`data` answers the LIST that was sent, not the set of cards it names.

        Deduplicating by card id looked like a courtesy and broke the route's contract: a client
        posting a deck list with four copies of a card got one object back. Measured 2026-08-16 --
        three identical `{id}` identifiers return three card objects.
        """
        result = self._client(compat_corpus).simulate_post(
            "/cards/collection",
            json={"identifiers": [{"id": BOLT_ID}, {"id": BOLT_ID}, {"id": BOLT_ID}]},
        )
        assert result.status_code == 200
        assert [card["id"] for card in orjson.loads(result.content)["data"]] == [BOLT_ID] * 3

    @pytest.mark.parametrize(
        ("identifier", "tail"),
        [
            # `arena_id` is the case worth having: a real key on a card object, and simply not a
            # collection identifier, so a client reaching for it used to be told the card is missing.
            ({"arena_id": 67330}, ""),
            ({}, ""),
            ({"nonsense": "x"}, ""),
            ({"set": "khm"}, "set"),
            ({"set": "khm", "lang": "ja"}, "set"),
            ({"set": "khm", "zzz": 1}, "set"),
            ({"collector_number": "1"}, "collector_number"),
            ({"collector_number": "1", "lang": "en"}, "collector_number"),
        ],
    )
    def test_collection_rejects_an_identifier_whose_keys_name_no_lookup(
        self, compat_corpus: APIResource, identifier: dict, tail: str
    ):
        """Every string here is measured, the tail included: it lists the RECOGNIZED keys present."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [identifier]})
        assert result.status_code == 400
        body = orjson.loads(result.content)
        assert body["code"] == "bad_request"
        assert body["details"] == f"Invalid identifier schema: {tail}"

    @pytest.mark.parametrize("key", ["mtgo_id", "multiverse_id"])
    def test_collection_rejects_a_non_integer_id(self, compat_corpus: APIResource, key: str):
        """`A` rather than `An` -- Scryfall picks the article per field, not per sentence."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{key: "abc"}]})
        assert result.status_code == 400
        assert orjson.loads(result.content)["details"] == f"A `{key}` identifier must be an integer: abc"

    @pytest.mark.parametrize(
        "body",
        [
            {"identifiers": []},
            {},
            {"nope": True},
            {"identifiers": [None]},
            {"identifiers": ["Lightning Bolt"]},
            {"identifiers": [{"id": "x"}] * (MAX_COLLECTION_IDENTIFIERS + 1)},
        ],
    )
    def test_collection_answers_one_sentence_to_every_list_shaped_mistake(self, compat_corpus: APIResource, body: dict):
        """An empty, absent, over-long or non-object-holding list is all the same 400.

        `{"identifiers": []}` used to answer `200 {"data": []}`, telling the client its (empty)
        question had an (empty) answer, and the over-long one used to be a `422 validation_error`
        with wording of its own. The cap check also runs BEFORE identifier validation -- the 76
        entries here carry `{"id": "x"}`, which is not a valid UUID, and the count sentence still wins.
        """
        result = self._client(compat_corpus).simulate_post("/cards/collection", json=body)
        assert result.status_code == 400
        payload_body = orjson.loads(result.content)
        assert payload_body["code"] == "bad_request"
        assert payload_body["details"] == "The `identifiers` list must have at least 1 and no more than 75 references."
        assert result.headers["cache-control"] == "no-cache"

    def test_collection_distinguishes_a_missing_list_from_a_non_array_one(self, compat_corpus: APIResource):
        """An absent list reads as an empty one; a present-but-not-a-list one gets its own sentence."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": {}})
        assert result.status_code == 400
        assert orjson.loads(result.content)["details"] == "The `identifiers` list must be a JSON array."

    def test_collection_envelope_has_no_has_more(self, compat_corpus: APIResource):
        """Scryfall's collection List is `{object, not_found, data}` -- it does not paginate."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{"id": BOLT_ID}]})
        assert list(orjson.loads(result.content)) == ["object", "not_found", "data"]

    @pytest.mark.parametrize(
        ("identifier", "why"),
        [
            ("00000000-0000-0000-0000-000000000000", "nil uuid: version 0"),
            ("00000000-0000-0000-0000-000000000001", "not the zero VALUE: version 0"),
            ("3f2c8e5d-91b7-1a6e-bd12-4f5a9c7e8b01", "version 1"),
            ("3f2c8e5d-91b7-7a6e-bd12-4f5a9c7e8b01", "version 7"),
            ("3f2c8e5d-91b7-4a6e-cd12-4f5a9c7e8b01", "variant c"),
            ("3f2c8e5d-91b7-4a6e-0d12-4f5a9c7e8b01", "variant 0"),
            ("not-a-uuid", "not a uuid at all"),
            ("", "empty string"),
            ("7673784edb4b43a18d551bb9fc1e284f", "no dashes"),
        ],
    )
    def test_collection_rejects_a_non_v4_identifier(self, compat_corpus: APIResource, identifier: str, why: str):
        """Measured against api.scryfall.com 2026-08-16: a malformed identifier UUID 400s the request."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{"id": identifier}]})
        assert result.status_code == 400, why
        assert orjson.loads(result.content)["code"] == "bad_request"

    @pytest.mark.parametrize(
        ("identifier", "why"),
        [
            ("00000000-0000-4000-8000-000000000000", "the ZERO value wearing v4's nibbles"),
            ("3f2c8e5d-91b7-4a6e-9d12-4f5a9c7e8b01", "valid, unknown: variant 9"),
            ("3f2c8e5d-91b7-4a6e-bd12-4f5a9c7e8b01", "valid, unknown: variant b"),
        ],
    )
    def test_collection_reports_a_valid_unknown_uuid_as_not_found(self, compat_corpus: APIResource, identifier: str, why: str):
        """The other half of the boundary: the rule is the SHAPE, not the value and not existence."""
        result = self._client(compat_corpus).simulate_post("/cards/collection", json={"identifiers": [{"id": identifier}]})
        assert result.status_code == 200, why
        body = orjson.loads(result.content)
        assert body["not_found"] == [{"id": identifier}]
        assert body["data"] == []

    def test_collection_echoes_the_offending_key_and_value(self, compat_corpus: APIResource):
        """30 characters then U+2026, and a short value whole -- both measured."""
        client = self._client(compat_corpus)
        long_value = client.simulate_post(
            "/cards/collection", json={"identifiers": [{"id": "00000000-0000-0000-0000-000000000000"}]}
        )
        assert (
            orjson.loads(long_value.content)["details"]
            == "An `id` identifier must be a valid UUID: 00000000-0000-0000-0000-000000\u2026"
        )
        short = client.simulate_post("/cards/collection", json={"identifiers": [{"oracle_id": "not-a-uuid"}]})
        assert orjson.loads(short.content)["details"] == "An `oracle_id` identifier must be a valid UUID: not-a-uuid"

    def test_one_malformed_identifier_400s_the_whole_batch(self, compat_corpus: APIResource):
        """Wherever it sits, and the FIRST malformed one is the one reported."""
        client = self._client(compat_corpus)
        bad = {"id": "00000000-0000-0000-0000-000000000000"}
        for identifiers in ([{"id": BOLT_ID}, bad], [bad, {"id": BOLT_ID}]):
            result = client.simulate_post("/cards/collection", json={"identifiers": identifiers})
            assert result.status_code == 400
            assert "00000000-0000-0000-0000-000000" in orjson.loads(result.content)["details"]

    def test_non_uuid_identifier_kinds_are_untouched_by_the_uuid_rule(self, compat_corpus: APIResource):
        """`{set, collector_number}` nonsense is a MISS, not a bad request -- only the three UUID keys are checked."""
        result = self._client(compat_corpus).simulate_post(
            "/cards/collection",
            json={"identifiers": [{"set": SET_CODE, "collector_number": "zzz"}, {"name": "Nothing At All Compat"}]},
        )
        assert result.status_code == 200
        assert len(orjson.loads(result.content)["not_found"]) == 2

    def test_next_page_addresses_this_host(self, compat_corpus: APIResource, monkeypatch):
        """Every other URI in the payload stays Scryfall's; this one has to point back here.

        The page size is shrunk to 1 rather than inserting 176 cards: what is under test is the URL
        the handler builds when there is a further page, not the boundary arithmetic, which
        `test_page_beyond_the_results_is_a_404` already pins.
        """
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        result = self._client(compat_corpus).simulate_get("/cards/search", params={"q": f"s:{SET_CODE}"})
        body = orjson.loads(result.content)

        assert body["total_cards"] == 3
        assert len(body["data"]) == 1
        assert body["has_more"] is True
        assert body["next_page"].startswith("http://falconframework.org/cards/search?")
        assert "page=2" in body["next_page"]
        assert f"q=s%3A{SET_CODE}" in body["next_page"]

    def test_the_last_page_has_no_next_page(self, compat_corpus: APIResource, monkeypatch):
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        body = orjson.loads(
            self._client(compat_corpus).simulate_get("/cards/search", params={"q": f"s:{SET_CODE}", "page": "3"}).content,
        )
        assert body["has_more"] is False
        assert "next_page" not in body

    def test_following_next_page_walks_the_result_set(self, compat_corpus: APIResource, monkeypatch):
        """Paging by `next_page` must visit every card exactly once, which is what a client does."""
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        client = self._client(compat_corpus)
        seen, page = [], 1
        while True:
            body = orjson.loads(client.simulate_get("/cards/search", params={"q": f"s:{SET_CODE}", "page": str(page)}).content)
            seen.extend(card["id"] for card in body["data"])
            if not body["has_more"]:
                break
            page += 1
        assert sorted(seen) == sorted([BOLT_ID, BEAR_ID, DELVER_ID])


class TestSelfUrlScheme:
    """`next_page` has to be followable, which means getting the scheme right behind a proxy."""

    def _next_page(self, api: APIResource, monkeypatch, headers: dict[str, str] | None = None) -> str:
        monkeypatch.setattr("api.scryfall_compat.routes.PAGE_SIZE", 1)
        environ = falcon.testing.create_environ(path="/cards", headers=headers)
        req, resp = falcon.Request(environ), falcon.Response()
        api._handle(req, resp)
        return payload(resp)["next_page"]

    def test_plain_http_request_stays_http(self, compat_corpus: APIResource, monkeypatch):
        assert self._next_page(compat_corpus, monkeypatch).startswith("http://falconframework.org/")

    def test_a_forwarded_proto_wins_over_the_requests_own_scheme(self, compat_corpus: APIResource, monkeypatch):
        """Behind a TLS-terminating proxy the request itself is http; only the header knows better."""
        url = self._next_page(compat_corpus, monkeypatch, {"X-Forwarded-Proto": "https"})
        assert url.startswith("https://")

    def test_the_rfc_7239_forwarded_header_is_honored_too(self, compat_corpus: APIResource, monkeypatch):
        url = self._next_page(compat_corpus, monkeypatch, {"Forwarded": "proto=https"})
        assert url.startswith("https://")

    def test_the_proxy_host_header_sets_the_host(self, compat_corpus: APIResource, monkeypatch):
        url = self._next_page(
            compat_corpus,
            monkeypatch,
            {"X-Proxy-Host": "cards.example.test", "X-Forwarded-Proto": "https"},
        )
        assert url.startswith("https://cards.example.test/cards?")
