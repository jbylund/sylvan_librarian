"""Unit tests for the Scryfall response objects: no database, no request.

`api/tests/test_scryfall_cards_routes.py` covers the routes themselves against a real corpus.
"""

from __future__ import annotations

import datetime
import json

import pytest

from api.scryfall_compat.objects import (
    build_page_url,
    card_list,
    card_to_text,
    catalog_object,
    collection_list,
    error_object,
    image_uri,
    ruling_object,
    sql_row_to_engine_row,
    to_scryfall_card,
)


def row(**overrides: object) -> dict:
    """An engine row carrying CARD_OBJECT_FIELDS, as the store emits it."""
    return {
        "scryfall_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "oracle_id": "11111111-2222-3333-4444-555555555555",
        "name": "Lightning Bolt",
        "mana_cost": "{R}",
        "cmc": 1.0,
        "type_line": "Instant",
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
        "colors": ["R"],
        "color_identity": ["R"],
        "set_code": "lea",
        "set_name": "Limited Edition Alpha",
        "set_id": "99999999-8888-7777-6666-555555555555",
        "collector_number": "161",
        "rarity": "common",
        "released_at": "1993-08-05",
        "lang": "en",
        "layout": "normal",
        "legalities": {"modern": "legal"},
        "tcgplayer_id": 697344,
        "cardmarket_id": 892161,
        "mtgo_id": 152037,
        "image_updated_at": 1783903008,
        "card_faces": [],
        "all_parts": [],
    } | overrides


class TestToScryfallCard:
    """An engine row BECOMES a card object; nothing is unwrapped from a stored copy.

    That distinction is the reason /cards/* can be served from the store at all: a blob is a
    Postgres column, and Postgres is the fallback for when the engine errors.
    """

    def test_stored_columns_carry_through(self):
        card = to_scryfall_card(row())
        assert card["object"] == "card"
        assert card["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert card["name"] == "Lightning Bolt"
        assert card["type_line"] == "Instant"
        assert card["colors"] == ["R"]
        assert card["set"] == "lea"
        assert card["rarity"] == "common"
        assert card["legalities"] == {"modern": "legal"}

    def test_cmc_is_a_decimal_even_when_the_column_holds_an_integer(self):
        """`"cmc":1.0` is what api.scryfall.com answers, and `magic.cards.cmc` is an integer column.

        The field is decimal because fractional mana values are real -- Little Girl costs {HW} and
        Scryfall answers `"cmc":0.5` for it. A whole-numbered mana value therefore still carries its
        decimal point, and a client comparing bodies against Scryfall can see the difference.
        """
        card = to_scryfall_card(row(cmc=1))
        assert isinstance(card["cmc"], float)
        assert '"cmc": 1.0' in json.dumps(card)
        assert to_scryfall_card(row(cmc=None))["cmc"] is None

    def test_uris_are_derived_not_stored(self):
        """Every *_uri is a pure function of the id, set, collector number or oracle id."""
        card = to_scryfall_card(row(), base_url="https://example.test")
        assert card["uri"] == "https://example.test/cards/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert card["rulings_uri"] == "https://example.test/cards/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/rulings"
        assert "oracleid%3A11111111-2222-3333-4444-555555555555" in card["prints_search_uri"]
        assert card["scryfall_uri"] == "https://scryfall.com/card/lea/161/lightning-bolt?utm_source=api"
        assert card["scryfall_set_uri"] == "https://scryfall.com/sets/lea?utm_source=api"

    def test_image_uris_follow_scryfalls_cdn_path(self):
        """The first two hex digits of the id are directory levels, and the cache-buster rides."""
        card = to_scryfall_card(row())
        assert card["image_uris"]["large"] == (
            "https://cards.scryfall.io/large/front/a/a/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg?1783903008"
        )
        assert card["image_uris"]["png"].endswith(".png?1783903008")

    def test_purchase_uris_drop_scryfalls_affiliate_wrapper(self):
        """Same destination, without routing another service's affiliate revenue to Scryfall."""
        card = to_scryfall_card(row())
        assert card["purchase_uris"]["tcgplayer"] == "https://www.tcgplayer.com/product/697344?page=1"
        assert card["purchase_uris"]["cardhoarder"] == "https://www.cardhoarder.com/cards/152037"
        for uri in card["purchase_uris"].values():
            assert "partner.tcgplayer.com" not in uri
            assert "affiliate_id" not in uri
            assert "scryfall" not in uri

    def test_absent_keys_stay_absent(self):
        """Scryfall OMITS a key it has no value for. Emitting null would differ on every row."""
        card = to_scryfall_card(row())
        for key in ("power", "toughness", "loyalty", "flavor_text", "watermark", "arena_id", "promo_types"):
            assert key not in card, f"{key} should be omitted, not null"

    def test_a_planeswalkers_printed_loyalty_is_the_string(self):
        """The engine holds `planeswalker_loyalty` as a u8 for `loy:`; the card object needs the text.

        Without its own field the key was emitted by nothing at all, so every planeswalker's card
        object came back with no `loyalty` -- and deriving it from the number would still lose "X"
        (Nissa, Steward of Elements) and "1+*", which do not fit in a u8.
        """
        card = to_scryfall_card(row(name="Jace Beleren", loyalty="3"))
        assert card["loyalty"] == "3"

        keys = list(card)
        assert keys.index("loyalty") > keys.index("type_line"), "loyalty sits with the printed stats"

        assert to_scryfall_card(row(loyalty="X"))["loyalty"] == "X"

    def test_border_color_and_frame_come_from_the_engine_row(self):
        """Both come from the engine row now.

        They were read from keys no engine row carried, so every engine-served card had
        `border_color: null` and no `frame` at all -- where Scryfall always sends both.
        """
        card = to_scryfall_card(row(border_color="black", frame="2015"))
        assert card["border_color"] == "black"
        assert card["frame"] == "2015"

    def test_border_color_survives_the_sql_alias(self):
        """The SQL column aliases into the engine's field name.

        The column is `card_border` and the engine field is `border_color`; one builder reads one
        name, so the SQL row is aliased into it rather than the builder reading both.
        """
        assert sql_row_to_engine_row({"card_border": "borderless"})["border_color"] == "borderless"

    def test_present_optional_keys_appear(self):
        card = to_scryfall_card(row(power="3", toughness="2", flavor_text="Kaboom.", arena_id=12345))
        assert card["power"] == "3"
        assert card["flavor_text"] == "Kaboom."
        assert card["arena_id"] == 12345

    def test_prices_use_scryfalls_string_format(self):
        card = to_scryfall_card(row(price_usd=1.5, price_usd_foil=282.0))
        assert card["prices"]["usd"] == "1.50"
        assert card["prices"]["usd_foil"] == "282.00"
        assert card["prices"]["eur"] is None, "prices keys are always present, unlike optional keys"

    def test_a_single_faced_card_has_top_level_text_and_no_faces(self):
        card = to_scryfall_card(row())
        assert "card_faces" not in card
        assert card["oracle_text"] == "Lightning Bolt deals 3 damage to any target."
        assert card["image_uris"]

    def test_a_two_image_card_moves_its_text_and_its_picture_into_the_faces(self):
        """Which keys sit at top level varies by LAYOUT, so this is a branch, not a fixed shape."""
        card = to_scryfall_card(
            row(
                name="Delver of Secrets // Insectile Aberration",
                card_layout="transform",
                card_faces=[
                    {"name": "Delver of Secrets", "oracle_text": "Front.", "mana_cost": "{U}"},
                    {"name": "Insectile Aberration", "oracle_text": "Back.", "power": "3"},
                ],
            )
        )
        assert "oracle_text" not in card
        assert "image_uris" not in card
        assert "mana_cost" not in card
        assert "card_back_id" not in card
        assert [f["name"] for f in card["card_faces"]] == ["Delver of Secrets", "Insectile Aberration"]
        assert all(f["object"] == "card_face" for f in card["card_faces"])
        # Each face gets its own side of the CDN path.
        assert "/front/" in card["card_faces"][0]["image_uris"]["large"]
        assert "/back/" in card["card_faces"][1]["image_uris"]["large"]

    def test_a_one_image_card_keeps_its_picture_and_joins_its_faces_costs(self):
        """A split card is one piece of cardboard: one picture, one cost, and text-only faces."""
        card = to_scryfall_card(
            row(
                name="Fire // Ice",
                card_layout="split",
                card_faces=[
                    {"name": "Fire", "oracle_text": "Two damage.", "mana_cost": "{1}{R}"},
                    {"name": "Ice", "oracle_text": "Tap it.", "mana_cost": "{1}{U}"},
                ],
            )
        )
        assert card["mana_cost"] == "{1}{R} // {1}{U}"
        assert "/front/" in card["image_uris"]["large"]
        assert card["card_back_id"] == "0aeebaf5-8c7d-4636-9e82-8c27447861f7"
        assert all("image_uris" not in face for face in card["card_faces"])
        # ...and edhrec files a split under both halves, unlike every other multi-face layout.
        assert card["related_uris"]["edhrec"] == "https://edhrec.com/route/?cc=Fire+%2F%2F+Ice"

    def test_related_cards_pass_through_when_present(self):
        parts = [{"object": "related_card", "id": "x", "component": "token", "name": "Goblin", "type_line": "Token"}]
        assert to_scryfall_card(row(all_parts=parts))["all_parts"] == parts

    def test_card_back_id_is_the_shared_constant(self):
        assert to_scryfall_card(row())["card_back_id"] == "0aeebaf5-8c7d-4636-9e82-8c27447861f7"


class TestEnvelopes:
    """The List, Catalog, Ruling and error objects match Scryfall's shapes."""

    def test_error_object_omits_warnings_when_there_are_none(self):
        assert error_object(code="not_found", status=404, details="nope") == {
            "object": "error",
            "code": "not_found",
            "status": 404,
            "details": "nope",
        }

    def test_error_object_carries_warnings(self):
        error = error_object(code="bad_request", status=400, details="nope", warnings=["heads up"])
        assert error["warnings"] == ["heads up"]

    def test_card_list_key_order_matches_scryfall(self):
        listing = card_list([{"object": "card"}], total_cards=200, has_more=True, next_page="https://x/?page=2")
        assert list(listing) == ["object", "total_cards", "has_more", "next_page", "data"]

    def test_card_list_omits_pagination_it_was_not_given(self):
        assert card_list([]) == {"object": "list", "has_more": False, "data": []}

    def test_collection_list_carries_not_found(self):
        listing = collection_list([], [{"name": "Nope"}])
        assert listing["not_found"] == [{"name": "Nope"}]

    def test_collection_list_has_no_has_more(self):
        """Scryfall does not paginate `/cards/collection` and does not send the key (measured 2026-08-16)."""
        assert list(collection_list([{"object": "card"}], [])) == ["object", "not_found", "data"]
        assert collection_list([], []) == {"object": "list", "not_found": [], "data": []}

    def test_the_two_envelopes_share_one_key_order(self):
        """Both are built by `_list_object`, so the keys they share cannot fall into different orders."""
        paged = [k for k in card_list([], not_found=[]) if k != "has_more"]
        assert paged == list(collection_list([], []))

    def test_catalog_object_counts_its_values(self):
        assert catalog_object(["Bolt", "Shock"]) == {"object": "catalog", "total_values": 2, "data": ["Bolt", "Shock"]}

    def test_ruling_object_renders_the_date_as_iso(self):
        row = {
            "oracle_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "source": "wotc",
            "published_at": datetime.date(2004, 10, 4),
            "comment": "It does.",
        }
        assert ruling_object(row)["published_at"] == "2004-10-04"


class TestPageUrl:
    """`next_page` is absolute, sorted, and carries the page it fetches."""

    def test_page_is_appended_and_parameters_sorted(self):
        url = build_page_url("https://example.test/cards/search", {"q": "fire", "order": "name"}, 2)
        assert url == "https://example.test/cards/search?order=name&page=2&q=fire"

    def test_query_values_are_escaped(self):
        url = build_page_url("https://example.test/cards/search", {"q": "t:creature c:r"}, 3)
        assert "q=t%3Acreature+c%3Ar" in url


class TestImageUri:
    """`format=image` resolves the size and face the client asked for."""

    def test_front_face_size_is_selected(self):
        card = {"image_uris": {"png": "https://cdn/front.png", "large": "https://cdn/front.jpg"}}
        assert image_uri(card, version="png", face="front") == "https://cdn/front.png"

    def test_back_face_uses_the_second_face(self):
        card = {
            "image_uris": {"large": "https://cdn/front.jpg"},
            "card_faces": [
                {"image_uris": {"large": "https://cdn/front.jpg"}},
                {"image_uris": {"large": "https://cdn/back.jpg"}},
            ],
        }
        assert image_uri(card, version="large", face="back") == "https://cdn/back.jpg"

    def test_back_face_on_a_single_faced_card_falls_back_to_the_front(self):
        card = {"image_uris": {"large": "https://cdn/front.jpg"}}
        assert image_uri(card, version="large", face="back") == "https://cdn/front.jpg"

    def test_missing_size_is_reported_as_absent(self):
        assert image_uri({"image_uris": {}}, version="png", face="front") is None


class TestCardToText:
    """`format=text` renders the card the way Scryfall's text format does."""

    def test_instant_renders_name_cost_type_and_text(self):
        card = {
            "name": "Lightning Bolt",
            "mana_cost": "{R}",
            "type_line": "Instant",
            "oracle_text": "Lightning Bolt deals 3 damage to any target.",
        }
        assert card_to_text(card) == ("Lightning Bolt {R}\nInstant\nLightning Bolt deals 3 damage to any target.")

    def test_creature_appends_power_and_toughness(self):
        card = {"name": "Grizzly Bears", "mana_cost": "{1}{G}", "type_line": "Creature — Bear", "power": "2", "toughness": "2"}
        assert card_to_text(card).endswith("\n2/2")

    def test_planeswalker_appends_loyalty(self):
        card = {"name": "Ajani", "mana_cost": "{2}{W}{W}", "type_line": "Legendary Planeswalker — Ajani", "loyalty": "4"}
        assert card_to_text(card).endswith("\nLoyalty: 4")

    def test_faces_are_separated_by_a_blank_line(self):
        card = {
            "name": "Delver of Secrets // Insectile Aberration",
            "card_faces": [
                {
                    "name": "Delver of Secrets",
                    "mana_cost": "{U}",
                    "type_line": "Creature — Human Wizard",
                    "power": "1",
                    "toughness": "1",
                },
                {
                    "name": "Insectile Aberration",
                    "mana_cost": "",
                    "type_line": "Creature — Human Insect",
                    "power": "3",
                    "toughness": "2",
                },
            ],
        }
        rendered = card_to_text(card)
        assert rendered.count("\n\n") == 1
        assert rendered.startswith("Delver of Secrets {U}")
        assert rendered.endswith("3/2")

    @pytest.mark.parametrize("missing", ["oracle_text", "mana_cost"])
    def test_absent_pieces_are_skipped_rather_than_rendered_empty(self, missing):
        card = {
            "name": "Vanilla",
            "mana_cost": "{G}",
            "type_line": "Creature — Bear",
            "oracle_text": "text",
            "power": "2",
            "toughness": "2",
        }
        del card[missing]
        assert "\n\n" not in card_to_text(card)
