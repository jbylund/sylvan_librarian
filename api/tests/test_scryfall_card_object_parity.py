"""Every route that renders a card renders the SAME card object, on the engine path and on SQL.

There is one card-object builder (`objects.to_scryfall_card`) and two ways to feed it: an engine
row, and a `magic.cards` row reshaped by `sql_row_to_engine_row`. A route takes whichever lane it
takes -- `/cards/:id` asks the engine first, `/cards/:set/:number`, `/cards/multiverse/:id` and
`/cards/random` go to SQL on every request -- so a field the SQL row does not carry is a field that
one address of a card has and another address of the same card does not. It was: every price was
null through the SQL lane, while `/cards/:id` answered the card's prices.

The assertion is therefore whole-object and route-by-route: for each card below, the object every
route renders, with the engine serving and again with it gated off, must be byte-for-byte the object
`/cards/:id` renders from the engine. A field that differs is named in the failure, so a new column
the builder learns to read but the SQL lane does not select fails here the day it lands.

The cards are chosen for the fields a SQL row can miss: all six prices, a printed loyalty, a
transform card with a planeswalker back face, an edhrec and penny rank, each of Scryfall's frame
versions but one, a colour indicator and produced mana, and a foreign-only printing carrying its
printed name, type line and text. They live in a set of their own ("sfy"), under names no other
test uses, because the database is shared across the session.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import falcon
import falcon.testing
import orjson
import pytest

from api.card_processing import rarity_text_to_int
from api.scryfall_compat.objects import _RARITY_BY_INT, CARD_OBJECT_FIELDS
from api.settings import settings
from api.tests.helpers import make_raw_card

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from api.api_resource import APIResource

SET_CODE = "sfy"

CINDER_ID = "e7e7e7e7-0001-4e7e-8e7e-e7e7e7e7e7e7"
WALKER_ID = "e7e7e7e7-0002-4e7e-8e7e-e7e7e7e7e7e7"
SEER_ID = "e7e7e7e7-0003-4e7e-8e7e-e7e7e7e7e7e7"
ARBOR_ID = "e7e7e7e7-0004-4e7e-8e7e-e7e7e7e7e7e7"
LANTERN_ID = "e7e7e7e7-0005-4e7e-8e7e-e7e7e7e7e7e7"


def _printing(card_id: str, number: str, name: str, **fields: Any) -> dict:
    """A printing carrying every compat-residue key a real one does, plus `fields`."""
    card = make_raw_card(card_id=card_id, name=name)
    index = int(number)
    card |= {
        "object": "card",
        "oracle_id": f"e8e8e8e8-000{index}-4e8e-8e8e-e8e8e8e8e8e8",
        "set": SET_CODE,
        "set_name": "Scryfall Compat Parity",
        "set_id": "e9e9e9e9-0000-4e9e-8e9e-e9e9e9e9e9e9",
        "set_type": "expansion",
        "collector_number": number,
        "lang": "en",
        "layout": "normal",
        "released_at": "2024-02-09",
        "artist": "Parity Artist",
        "illustration_id": f"eaeaeaea-000{index}-4eae-8eae-eaeaeaeaeaea",
        "border_color": "black",
        "games": ["paper", "mtgo", "arena"],
        "finishes": ["nonfoil", "foil"],
        "image_status": "highres_scan",
        "highres_image": True,
        "image_updated_at": "2026-07-13T00:36:48Z",
        "booster": True,
        "reprint": False,
        "legalities": {"vintage": "legal", "commander": "legal"},
    }
    card |= fields
    return card


def _cinder() -> dict:
    """An instant with every one of Scryfall's six prices, both ranks and every marketplace id."""
    return _printing(
        CINDER_ID,
        "1",
        "Vorthane Cinder",
        mana_cost="{R}",
        cmc=1.0,
        type_line="Instant",
        oracle_text="Vorthane Cinder deals 3 damage to any target.",
        flavor_text="Ash remembers what fire forgets.",
        flavor_name="Ember of the Tidewyrm",
        colors=["R"],
        color_identity=["R"],
        rarity="common",
        frame="1993",
        watermark="mirran",
        security_stamp="oval",
        finishes=["nonfoil", "foil", "etched"],
        prices={"usd": "0.25", "usd_foil": "1.50", "usd_etched": "3.00", "eur": "0.20", "eur_foil": "1.10", "tix": "0.03"},
        edhrec_rank=1234,
        penny_rank=567,
        multiverse_ids=[970001],
        mtgo_id=970002,
        mtgo_foil_id=970003,
        arena_id=970004,
        tcgplayer_id=970005,
        tcgplayer_etched_id=970006,
        cardmarket_id=970007,
    )


def _walker() -> dict:
    """A planeswalker: a printed loyalty, the 2015 frame and a frame effect."""
    return _printing(
        WALKER_ID,
        "2",
        "Kelsaro, Tide Oracle",
        mana_cost="{2}{U}{U}",
        cmc=4.0,
        type_line="Legendary Planeswalker — Kelsaro",
        oracle_text="+1: Draw a card.\n-3: Return target creature to its owner's hand.",
        loyalty="3",
        colors=["U"],
        color_identity=["U"],
        rarity="mythic",
        frame="2015",
        frame_effects=["legendary"],
        promo_types=["boosterfun"],
        prices={"usd": "12.34", "eur": "10.01", "tix": "1.50"},
        all_parts=[
            {
                "object": "related_card",
                "id": WALKER_ID,
                "component": "combo_piece",
                "name": "Kelsaro, Tide Oracle",
                "type_line": "Legendary Planeswalker — Kelsaro",
                "uri": f"https://api.scryfall.com/cards/{WALKER_ID}",
            },
            {
                "object": "related_card",
                "id": "e7e7e7e7-00e2-4e7e-8e7e-e7e7e7e7e7e7",
                "component": "token",
                "name": "Kelsaro, Tide Oracle Emblem",
                "type_line": "Emblem — Kelsaro",
                "uri": "https://api.scryfall.com/cards/e7e7e7e7-00e2-4e7e-8e7e-e7e7e7e7e7e7",
            },
        ],
        edhrec_rank=88,
        multiverse_ids=[970011],
        mtgo_id=970012,
        arena_id=970014,
        tcgplayer_id=970015,
        cardmarket_id=970017,
    )


def _seer() -> dict:
    """A transform card whose back face is a planeswalker with a colour indicator."""
    card = _printing(
        SEER_ID,
        "3",
        "Thrennic Seer // Thrennic, Unbound",
        layout="transform",
        mana_cost="",
        cmc=2.0,
        type_line="Creature — Human Wizard // Legendary Planeswalker — Thrennic",
        colors=["U"],
        color_identity=["U"],
        rarity="mythic",
        frame="2015",
        frame_effects=["originpwdfc"],
        prices={"usd": "3.00", "usd_foil": "9.99", "eur": "2.50", "tix": "0.40"},
        edhrec_rank=42,
        multiverse_ids=[970021],
        mtgo_id=970022,
        arena_id=970024,
        tcgplayer_id=970025,
        cardmarket_id=970027,
        card_faces=[
            {
                "object": "card_face",
                "name": "Thrennic Seer",
                "flavor_name": "Seer of the Drowned Choir",
                "mana_cost": "{1}{U}",
                "type_line": "Creature — Human Wizard",
                "oracle_text": "When Thrennic Seer dies, return it transformed.",
                "power": "1",
                "toughness": "2",
                "colors": ["U"],
                "artist": "Parity Artist",
                "illustration_id": "eaeaeaea-0031-4eae-8eae-eaeaeaeaeaea",
            },
            {
                "object": "card_face",
                "name": "Thrennic, Unbound",
                "mana_cost": "",
                "type_line": "Legendary Planeswalker — Thrennic",
                "oracle_text": "+1: Scry 2.",
                "loyalty": "4",
                "colors": ["U"],
                "color_indicator": ["U"],
                "artist": "Parity Artist",
                "illustration_id": "eaeaeaea-0032-4eae-8eae-eaeaeaeaeaea",
            },
        ],
    )
    # A two-image layout carries its art on the faces, as Scryfall sends it.
    card.pop("image_uris", None)
    card.pop("illustration_id", None)
    return card


def _arbor() -> dict:
    """A land creature: a colour indicator, produced mana, and the future frame."""
    return _printing(
        ARBOR_ID,
        "4",
        "Ombrevale Arbor",
        mana_cost="",
        cmc=0.0,
        type_line="Land Creature — Forest Dryad",
        oracle_text="(Ombrevale Arbor isn't a spell, it's affected by summoning sickness, and it has \"{T}: Add {G}.\")",
        power="1",
        toughness="1",
        colors=["G"],
        color_identity=["G"],
        color_indicator=["G"],
        produced_mana=["G"],
        rarity="rare",
        frame="future",
        prices={"usd": "0.75", "usd_foil": "2.25", "eur": "0.60"},
        edhrec_rank=777,
        multiverse_ids=[970041],
        mtgo_id=970042,
        tcgplayer_id=970045,
        cardmarket_id=970047,
    )


def _lantern() -> dict:
    """A card printed ONLY in Japanese, so its printed name, type line and text are the card's."""
    return _printing(
        LANTERN_ID,
        "5",
        "Sulvane Lantern",
        lang="ja",
        printed_name="スルヴェインの灯籠",
        printed_type_line="アーティファクト",
        printed_text="{T}で、好きな色1色のマナ1点を加える。",
        mana_cost="{2}",
        cmc=2.0,
        type_line="Artifact",
        oracle_text="{T}: Add one mana of any color.",
        produced_mana=["B", "G", "R", "U", "W"],
        rarity="uncommon",
        frame="2003",
        games=["paper"],
        finishes=["nonfoil"],
        prices={"usd": "0.50"},
        edhrec_rank=5,
        multiverse_ids=[970051],
    )


CARDS = {
    "cinder": _cinder,
    "walker": _walker,
    "seer": _seer,
    "arbor": _arbor,
    "lantern": _lantern,
}


@pytest.fixture(name="parity_corpus", scope="module")
def parity_corpus_fixture(api_resource: APIResource) -> APIResource:
    """Load the cards once, rebuild the engine's store over them, and hand back the resource.

    Loaded twice, the second time with every card's legalities naming every format the engine has
    registered. Scryfall lists EVERY format on every card, and the engine emits every format it has
    registered -- a process-wide set, grown by whatever other modules loaded into the shared
    database -- writing `not_legal` for one a row does not name. A row naming fewer formats than
    the registry is therefore a shape no import produces, and comparing one would test the session
    rather than the lanes.
    """
    cards = [build() for build in CARDS.values()]
    api_resource.admin._upsert_cards(copy.deepcopy(cards))
    api_resource.app_context.reload_engine(force=True)
    registered = api_resource.app_context.engine.card_by_scryfall_id(CINDER_ID, ["legalities"])["legalities"]
    for card in cards:
        card["legalities"] = {name: card["legalities"].get(name, "not_legal") for name in registered}
    api_resource.admin._upsert_cards(copy.deepcopy(cards))
    api_resource.app_context.reload_engine(force=True)
    api_resource.admin._clear_caches()
    return api_resource


@pytest.fixture(name="lane", params=["engine", "sql"])
def lane_fixture(request, parity_corpus: APIResource) -> Generator[APIResource]:
    """The corpus with the engine serving, and again with it gated off so SQL answers."""
    saved = settings.enable_engine
    settings.enable_engine = request.param == "engine"
    yield parity_corpus
    settings.enable_engine = saved


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


def payload(resp) -> dict:
    """The body as it goes over the wire: serialized by orjson, as the app's JSON handler does.

    Round-tripped rather than compared as set, because `resp.media` still holds the builder's
    Python values -- a tuple where the wire has a list, a UUID where it has a string -- and those
    are not differences a client can see.
    """
    if resp.media is not None:
        return orjson.loads(orjson.dumps(resp.media))
    return orjson.loads(resp.render_body())


def _one(api: APIResource, path: str, query: dict | None = None) -> dict:
    """The card a single-card route answers."""
    return payload(dispatch(api, path, urlencode(query or {})))


def _listed(body: dict, card_id: str) -> dict:
    """The card with `card_id` out of a List object's `data`, or the List itself when it is absent."""
    return next((card for card in body.get("data") or [] if card.get("id") == card_id), body)


def _face_name(card: dict) -> str:
    """The name a collection identifier knows a card by: a two-part name's front face, else the name.

    `{"name": "Front // Back"}` is not_found on api.scryfall.com; a face name is the key.
    """
    parts = card["name"].split(" // ")
    return parts[0] if len(parts) == 2 else card["name"]


def _illustration_id(card: dict) -> str:
    """The card's illustration id: its own, or its front face's on a layout whose faces carry art."""
    return card.get("illustration_id") or card["card_faces"][0]["illustration_id"]


def _collection(api: APIResource, identifier: dict, card_id: str) -> dict:
    """The card one collection identifier resolves to."""
    body = payload(dispatch(api, "/cards/collection", method="POST", body={"identifiers": [identifier]}))
    return _listed(body, card_id)


def _search(api: APIResource, card_id: str, number: str) -> dict:
    """The card as `/cards/search` lists it."""
    return _listed(_one(api, "/cards/search", {"q": f"e:{SET_CODE} cn:{number}", "unique": "prints"}), card_id)


def _all_cards(api: APIResource, card_id: str) -> dict:
    """The card as the unfiltered `/cards` listing renders it, walking pages until it appears."""
    page = 1
    while True:
        body = _one(api, "/cards", {"page": page})
        card = _listed(body, card_id)
        if card is not body or not body.get("has_more"):
            return card
        page += 1


# Every route that renders a card object, each as a function of the raw card. A route that takes a
# field the card does not have (an Arena id on a paper-only printing) is skipped for that card, and
# so is the language-less `/cards/:set/:number` for a foreign printing: which printing that address
# answers when no English one exists is a rule of its own, not a rendering.
ROUTES: dict[str, Callable[[APIResource, dict], dict]] = {
    "id": lambda api, c: _one(api, f"/cards/{c['id']}"),
    "set-number": lambda api, c: _one(api, f"/cards/{c['set']}/{c['collector_number']}"),
    "set-number-lang": lambda api, c: _one(api, f"/cards/{c['set']}/{c['collector_number']}/{c['lang']}"),
    "multiverse": lambda api, c: _one(api, f"/cards/multiverse/{c['multiverse_ids'][0]}"),
    "mtgo": lambda api, c: _one(api, f"/cards/mtgo/{c['mtgo_id']}"),
    "mtgo-foil": lambda api, c: _one(api, f"/cards/mtgo/{c['mtgo_foil_id']}"),
    "arena": lambda api, c: _one(api, f"/cards/arena/{c['arena_id']}"),
    "tcgplayer": lambda api, c: _one(api, f"/cards/tcgplayer/{c['tcgplayer_id']}"),
    "tcgplayer-etched": lambda api, c: _one(api, f"/cards/tcgplayer/{c['tcgplayer_etched_id']}"),
    "cardmarket": lambda api, c: _one(api, f"/cards/cardmarket/{c['cardmarket_id']}"),
    "random": lambda api, c: _one(api, "/cards/random", {"q": f"e:{c['set']} cn:{c['collector_number']}"}),
    "named-exact": lambda api, c: _one(api, "/cards/named", {"exact": c["name"]}),
    "named-exact-set": lambda api, c: _one(api, "/cards/named", {"exact": c["name"], "set": c["set"]}),
    "named-fuzzy": lambda api, c: _one(api, "/cards/named", {"fuzzy": c["name"]}),
    "named-fuzzy-set": lambda api, c: _one(api, "/cards/named", {"fuzzy": c["name"], "set": c["set"]}),
    "search": lambda api, c: _search(api, c["id"], c["collector_number"]),
    "all-cards": lambda api, c: _all_cards(api, c["id"]),
    "collection-id": lambda api, c: _collection(api, {"id": c["id"]}, c["id"]),
    "collection-oracle-id": lambda api, c: _collection(api, {"oracle_id": c["oracle_id"]}, c["id"]),
    "collection-illustration-id": lambda api, c: _collection(api, {"illustration_id": _illustration_id(c)}, c["id"]),
    "collection-mtgo-id": lambda api, c: _collection(api, {"mtgo_id": c["mtgo_id"]}, c["id"]),
    "collection-multiverse-id": lambda api, c: _collection(api, {"multiverse_id": c["multiverse_ids"][0]}, c["id"]),
    "collection-set-number": lambda api, c: _collection(api, {"set": c["set"], "collector_number": c["collector_number"]}, c["id"]),
    "collection-name": lambda api, c: _collection(api, {"name": _face_name(c)}, c["id"]),
    "collection-name-set": lambda api, c: _collection(api, {"name": _face_name(c), "set": c["set"]}, c["id"]),
}

# What a card must have for each route to address it, where it may not.
_ROUTE_NEEDS: dict[str, Callable[[dict], Any]] = {
    "set-number": lambda c: c["lang"] == "en",
    "multiverse": lambda c: c.get("multiverse_ids"),
    "mtgo": lambda c: c.get("mtgo_id"),
    "mtgo-foil": lambda c: c.get("mtgo_foil_id"),
    "arena": lambda c: c.get("arena_id"),
    "tcgplayer": lambda c: c.get("tcgplayer_id"),
    "tcgplayer-etched": lambda c: c.get("tcgplayer_etched_id"),
    "cardmarket": lambda c: c.get("cardmarket_id"),
    "collection-mtgo-id": lambda c: c.get("mtgo_id"),
    "collection-multiverse-id": lambda c: c.get("multiverse_ids"),
}

CASES = [
    pytest.param(card, route, id=f"{card}-{route}")
    for card, build in CARDS.items()
    for route in ROUTES
    if _ROUTE_NEEDS.get(route, lambda _: True)(build())
]


def _reference(api: APIResource, card_id: str) -> dict:
    """`/cards/:id` answered by the ENGINE: the object every other route must reproduce."""
    saved = settings.enable_engine
    settings.enable_engine = True
    try:
        return _one(api, f"/cards/{card_id}")
    finally:
        settings.enable_engine = saved


def _differing_fields(got: dict, want: dict) -> list[str]:
    """The top-level keys that differ, by value first and then by key order alone.

    Order counts because the wire is JSON text: a client diffing bodies sees a moved key. A value
    whose own keys are ordered differently is `<field> <key order>`, and the object's own keys
    ordered differently is `<key order>`.
    """
    missing = object()
    keys = sorted({*got, *want})
    fields = [key for key in keys if got.get(key, missing) != want.get(key, missing)]
    fields += [
        f"{key} <key order>" for key in keys if key not in fields and orjson.dumps(got.get(key)) != orjson.dumps(want.get(key))
    ]
    if not fields and list(got) != list(want):
        fields.append("<key order>")
    return fields


class TestTheReferenceIsTheEnginesCard:
    """The object the routes are compared against is built from an engine row and is right."""

    @pytest.mark.parametrize("card", list(CARDS))
    def test_the_engine_serves_the_card(self, parity_corpus: APIResource, card):
        """The engine holds the card, so `_reference` is not a SQL object compared with itself."""
        row = parity_corpus.app_context.engine.card_by_scryfall_id(CARDS[card]()["id"], list(CARD_OBJECT_FIELDS))
        assert row is not None
        assert row["scryfall_id"] == CARDS[card]()["id"]

    def test_the_rarity_column_reads_back_as_it_was_written(self):
        """The SQL lane's rarity table inverts the one the importer writes the column with."""
        for rarity in ("common", "uncommon", "rare", "special", "mythic", "bonus"):
            assert _RARITY_BY_INT[rarity_text_to_int(rarity)] == rarity

    def test_the_reference_carries_every_price(self, parity_corpus: APIResource):
        prices = _reference(parity_corpus, CINDER_ID)["prices"]
        assert prices == {"usd": "0.25", "usd_foil": "1.50", "usd_etched": "3.00", "eur": "0.20", "eur_foil": "1.10", "tix": "0.03"}

    @pytest.mark.parametrize(
        ("card_id", "field", "expected"),
        [
            (WALKER_ID, "loyalty", "3"),
            (WALKER_ID, "rarity", "mythic"),
            (CINDER_ID, "edhrec_rank", 1234),
            (CINDER_ID, "penny_rank", 567),
            (CINDER_ID, "frame", "1993"),
            (WALKER_ID, "frame", "2015"),
            (ARBOR_ID, "frame", "future"),
            (LANTERN_ID, "frame", "2003"),
            (CINDER_ID, "security_stamp", "oval"),
            (CINDER_ID, "watermark", "mirran"),
            (ARBOR_ID, "color_indicator", ["G"]),
            (ARBOR_ID, "produced_mana", ["G"]),
            (LANTERN_ID, "produced_mana", ["B", "G", "R", "U", "W"]),
            (CINDER_ID, "flavor_name", "Ember of the Tidewyrm"),
            (LANTERN_ID, "printed_name", "スルヴェインの灯籠"),
            (LANTERN_ID, "printed_type_line", "アーティファクト"),
            (LANTERN_ID, "printed_text", "{T}で、好きな色1色のマナ1点を加える。"),
        ],
    )
    def test_the_reference_carries_the_field(self, parity_corpus: APIResource, card_id, field, expected):
        """Each value the SQL lane once dropped is on the reference, so both lanes omitting it fails.

        A field this branch's card object does not carry at all is skipped: it is not the reference's
        to have.
        """
        if field not in CARD_OBJECT_FIELDS:
            pytest.skip(f"{field} is not a card-object field on this branch")
        assert _reference(parity_corpus, card_id)[field] == expected


@pytest.mark.parametrize(("card", "route"), CASES)
def test_every_route_renders_the_cards_id_object(lane: APIResource, card, route):
    """The route's object is the engine's `/cards/:id` object, key for key and in the same order."""
    raw = CARDS[card]()
    want = _reference(lane, raw["id"])
    got = ROUTES[route](lane, raw)
    assert got.get("object") == "card", f"{route} did not answer the card: {got}"
    assert got["id"] == raw["id"]
    differing = _differing_fields(got, want)
    assert not differing, "fields differ from /cards/:id: " + ", ".join(
        f"{field} (route {got.get(field.split()[0])!r}, /cards/:id {want.get(field.split()[0])!r})" for field in differing
    )
