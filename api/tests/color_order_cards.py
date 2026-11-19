"""Eighteen real cards for `order=color`, one of every shape the order treats differently.

Each entry is the card's place under the order -- what `color_order_rank` must answer for it -- and
the fields of its Scryfall card object the rule reads (api.scryfall.com, 2026-10-08), trimmed of
everything else. A transforming or modal double-faced card has no top-level `colors`, exactly as
Scryfall serves it; a split card has them at the top level and none on its faces.

The list is in SCRYFALL'S OWN ASCENDING ORDER for these eighteen names, asked as exact names under
`order=color`; `DESCENDING_NAMES` is its answer to the same question under `dir=desc`. That is not
the ascending answer reversed: the blocks turn and the names inside them do not, so Emeria's Call
is before Swords to Plowshares and Dryad Arbor before Forest in both.

The edhrec ranks are the cards' own. They run against the name order inside the white block
(Swords to Plowshares is rank 11, Emeria's Call 2,783), so an ordering that broke a block's ties by
popularity fails on the first two rows.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from api.tests.helpers import make_raw_card

COLOR_ORDER_SET_CODE = "clr"

COLOR_ORDER_CARDS: list[tuple[int, dict[str, Any]]] = [
    (
        0,
        {
            "name": "Emeria's Call // Emeria, Shattered Skyclave",
            "type_line": "Sorcery // Land",
            "color_identity": ["W"],
            "edhrec_rank": 2783,
            "card_faces": [
                {"name": "Emeria's Call", "type_line": "Sorcery", "colors": ["W"], "mana_cost": "{4}{W}{W}{W}"},
                {"name": "Emeria, Shattered Skyclave", "type_line": "Land", "colors": [], "mana_cost": ""},
            ],
        },
    ),
    (
        0,
        {
            "name": "Swords to Plowshares",
            "type_line": "Instant",
            "colors": ["W"],
            "color_identity": ["W"],
            "mana_cost": "{W}",
            "edhrec_rank": 11,
        },
    ),
    (
        1,
        {
            "name": "Search for Azcanta // Azcanta, the Sunken Ruin",
            "type_line": "Legendary Enchantment // Legendary Land",
            "color_identity": ["U"],
            "edhrec_rank": 3983,
            "card_faces": [
                {"name": "Search for Azcanta", "type_line": "Legendary Enchantment", "colors": ["U"], "mana_cost": "{1}{U}"},
                {"name": "Azcanta, the Sunken Ruin", "type_line": "Legendary Land", "colors": [], "mana_cost": ""},
            ],
        },
    ),
    (
        2,
        {
            "name": "Valki, God of Lies // Tibalt, Cosmic Impostor",
            "type_line": "Legendary Creature — God // Legendary Planeswalker — Tibalt",
            "color_identity": ["B", "R"],
            "edhrec_rank": 6217,
            "card_faces": [
                {"name": "Valki, God of Lies", "type_line": "Legendary Creature — God", "colors": ["B"], "mana_cost": "{1}{B}"},
                {
                    "name": "Tibalt, Cosmic Impostor",
                    "type_line": "Legendary Planeswalker — Tibalt",
                    "colors": ["B", "R"],
                    "mana_cost": "{5}{B}{R}",
                },
            ],
        },
    ),
    (
        3,
        {
            "name": "Lightning Bolt",
            "type_line": "Instant",
            "colors": ["R"],
            "color_identity": ["R"],
            "mana_cost": "{R}",
            "edhrec_rank": 157,
        },
    ),
    (
        4,
        {
            "name": "Grizzly Bears",
            "type_line": "Creature — Bear",
            "colors": ["G"],
            "color_identity": ["G"],
            "mana_cost": "{1}{G}",
            "edhrec_rank": 8107,
        },
    ),
    (
        8,
        {
            "name": "Arlinn Kord // Arlinn, Embraced by the Moon",
            "type_line": "Legendary Planeswalker — Arlinn // Legendary Planeswalker — Arlinn",
            "color_identity": ["G", "R"],
            "edhrec_rank": 8413,
            "card_faces": [
                {
                    "name": "Arlinn Kord",
                    "type_line": "Legendary Planeswalker — Arlinn",
                    "colors": ["G", "R"],
                    "mana_cost": "{2}{R}{G}",
                },
                {
                    "name": "Arlinn, Embraced by the Moon",
                    "type_line": "Legendary Planeswalker — Arlinn",
                    "colors": ["G", "R"],
                    "mana_cost": "",
                },
            ],
        },
    ),
    (
        11,
        {
            "name": "Fire // Ice",
            "type_line": "Instant // Instant",
            "colors": ["R", "U"],
            "color_identity": ["R", "U"],
            "mana_cost": "{1}{R} // {1}{U}",
            "edhrec_rank": 13168,
            "card_faces": [
                {"name": "Fire", "type_line": "Instant", "mana_cost": "{1}{R}"},
                {"name": "Ice", "type_line": "Instant", "mana_cost": "{1}{U}"},
            ],
        },
    ),
    (
        13,
        {
            "name": "Boros Charm",
            "type_line": "Instant",
            "colors": ["R", "W"],
            "color_identity": ["R", "W"],
            "mana_cost": "{R}{W}",
            "edhrec_rank": 182,
        },
    ),
    (
        16,
        {
            "name": "Nicol Bolas, the Ravager // Nicol Bolas, the Arisen",
            "type_line": "Legendary Creature — Elder Dragon // Legendary Planeswalker — Bolas",
            "color_identity": ["B", "R", "U"],
            "edhrec_rank": 5294,
            "card_faces": [
                {
                    "name": "Nicol Bolas, the Ravager",
                    "type_line": "Legendary Creature — Elder Dragon",
                    "colors": ["B", "R", "U"],
                    "mana_cost": "{1}{U}{B}{R}",
                },
                {
                    "name": "Nicol Bolas, the Arisen",
                    "type_line": "Legendary Planeswalker — Bolas",
                    "colors": ["B", "R", "U"],
                    "mana_cost": "",
                },
            ],
        },
    ),
    (
        30,
        {
            "name": "Transguild Courier",
            "type_line": "Artifact Creature — Golem",
            "colors": ["B", "G", "R", "U", "W"],
            "color_identity": ["B", "G", "R", "U", "W"],
            "mana_cost": "{4}",
            "edhrec_rank": 17941,
        },
    ),
    (
        33,
        {
            "name": "Eldrazi Skyspawner",
            "type_line": "Creature — Eldrazi Drone",
            "colors": [],
            "color_identity": ["U"],
            "mana_cost": "{2}{U}",
            "edhrec_rank": 12717,
        },
    ),
    (63, {"name": "Sol Ring", "type_line": "Artifact", "colors": [], "color_identity": [], "mana_cost": "{1}", "edhrec_rank": 1}),
    (
        66,
        {
            "name": "Westvale Abbey // Ormendahl, Profane Prince",
            "type_line": "Land // Legendary Creature — Demon",
            "color_identity": ["B"],
            "edhrec_rank": 1474,
            "card_faces": [
                {"name": "Westvale Abbey", "type_line": "Land", "colors": [], "mana_cost": ""},
                {"name": "Ormendahl, Profane Prince", "type_line": "Legendary Creature — Demon", "colors": ["B"], "mana_cost": ""},
            ],
        },
    ),
    (
        68,
        {
            "name": "Dryad Arbor",
            "type_line": "Land Creature — Forest Dryad",
            "colors": ["G"],
            "color_identity": ["G"],
            "mana_cost": "",
            "edhrec_rank": 773,
        },
    ),
    (68, {"name": "Forest", "type_line": "Basic Land — Forest", "colors": [], "color_identity": ["G"], "mana_cost": ""}),
    (
        69,
        {
            "name": "Hengegate Pathway // Mistgate Pathway",
            "type_line": "Land // Land",
            "color_identity": ["U", "W"],
            "edhrec_rank": 1159,
            "card_faces": [
                {"name": "Hengegate Pathway", "type_line": "Land", "colors": [], "mana_cost": ""},
                {"name": "Mistgate Pathway", "type_line": "Land", "colors": [], "mana_cost": ""},
            ],
        },
    ),
    (95, {"name": "Wastes", "type_line": "Basic Land", "colors": [], "color_identity": [], "mana_cost": ""}),
]

ASCENDING_NAMES = [card["name"] for _rank, card in COLOR_ORDER_CARDS]
DESCENDING_NAMES = [
    "Wastes",
    "Hengegate Pathway // Mistgate Pathway",
    "Dryad Arbor",
    "Forest",
    "Westvale Abbey // Ormendahl, Profane Prince",
    "Sol Ring",
    "Eldrazi Skyspawner",
    "Transguild Courier",
    "Nicol Bolas, the Ravager // Nicol Bolas, the Arisen",
    "Boros Charm",
    "Fire // Ice",
    "Arlinn Kord // Arlinn, Embraced by the Moon",
    "Grizzly Bears",
    "Lightning Bolt",
    "Valki, God of Lies // Tibalt, Cosmic Impostor",
    "Search for Azcanta // Azcanta, the Sunken Ruin",
    "Emeria's Call // Emeria, Shattered Skyclave",
    "Swords to Plowshares",
]


def color_order_raw_cards() -> list[dict[str, Any]]:
    """The eighteen as importable raw cards, in an order that is neither answer.

    `make_raw_card` supplies what the importer requires of any card; the fields above replace its
    own, and its top-level `colors` is dropped where Scryfall serves none.
    """
    cards = []
    for index, (_rank, fields) in enumerate(reversed(COLOR_ORDER_CARDS)):
        card = make_raw_card(card_id=str(uuid.UUID(int=0xC0104 << 96 | index)), name=fields["name"])
        if "colors" not in fields:
            del card["colors"]
        # A fixed oracle id, so importing the list twice is importing the same cards twice.
        card |= copy.deepcopy(fields) | {
            "oracle_id": str(uuid.UUID(int=0xC0105 << 96 | index)),
            "set": COLOR_ORDER_SET_CODE,
            "collector_number": str(index + 1),
        }
        cards.append(card)
    return cards
