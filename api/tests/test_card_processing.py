"""Tests for card processing functions."""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any

import pytest

from api.card_processing import color_order_rank, extract_frame_data_from_raw_card, preprocess_card
from api.tests.color_order_cards import COLOR_ORDER_CARDS, color_order_raw_cards

# Project root directory for accessing sample data
_PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent
_SAMPLE_DATA_DIR = _PROJECT_ROOT / "docs" / "sample_data"


def create_test_card(  # noqa: PLR0913, PLR0917
    card_id: str | None = None,
    name: str = "Test Card",
    legalities: dict | None = None,
    games: list | None = None,
    type_line: str = "Creature — Test",
    colors: list | None = None,
    color_identity: list | None = None,
    keywords: list | None = None,
    power: str | None = None,
    toughness: str | None = None,
    prices: dict | None = None,
    set_code: str = "test",
    artist: str | None = None,
    rarity: str = "common",
    collector_number: str = "1",
    edhrec_rank: int | None = None,
    **kwargs: Any,
) -> dict:
    """Create a test card with default values that can be overridden.

    Args:
        card_id: Unique identifier for the card
        name: Card name
        legalities: Card legalities dict
        games: List of games the card is legal in
        type_line: Card type line
        colors: Card colors list
        color_identity: Card color identity list
        keywords: List of keywords
        power: Creature power
        toughness: Creature toughness
        prices: Price dict
        set_code: Set code
        artist: Artist name
        rarity: Card rarity
        collector_number: Collector number
        edhrec_rank: EDHREC rank
        **kwargs: Additional fields to add to the card

    Returns:
        A test card dictionary with all required fields
    """
    if legalities is None:
        legalities = {"standard": "legal", "modern": "legal"}
    if games is None:
        games = ["paper"]
    if colors is None:
        colors = ["R"]
    if color_identity is None:
        color_identity = ["R"]
    if keywords is None:
        keywords = []
    if prices is None:
        prices = {"usd": "1.00"}
    card_id = card_id or str(uuid.uuid4())
    jpg_part = f"{card_id[0]}/{card_id[1]}/{card_id}.jpg"
    card = {
        "id": card_id,
        "name": name,
        "legalities": legalities,
        "games": games,
        "type_line": type_line,
        "colors": colors,
        "color_identity": color_identity,
        "keywords": keywords,
        "power": power,
        "toughness": toughness,
        "prices": prices,
        "set": set_code,
        "artist": artist,
        "rarity": rarity,
        "collector_number": collector_number,
        "edhrec_rank": edhrec_rank,
        "image_uris": {
            # https://cards.scryfall.io/normal/front/a/7/a7af8350-9a51-437c-a55e-19f3e07acfa9.jpg?1562934732
            "small": f"https://cards.scryfall.io/small/front/{jpg_part}",
            "normal": f"https://cards.scryfall.io/normal/front/{jpg_part}",
            "large": f"https://cards.scryfall.io/large/front/{jpg_part}",
            "png": f"https://cards.scryfall.io/png/front/{jpg_part}",
            "art_crop": f"https://cards.scryfall.io/art_crop/front/{jpg_part}",
            "border_crop": f"https://cards.scryfall.io/border_crop/front/{jpg_part}",
        },
    }

    # Add any additional fields
    card.update(kwargs)

    return card


class TestCardProcessing:
    """Test card processing functions."""

    def test_preprocess_card_filters_non_paper_cards(self) -> None:
        """Test preprocess_card filters out non-paper cards."""
        invalid_card = create_test_card(
            games=["mtgo"],  # Not paper
        )

        result = preprocess_card(invalid_card)
        assert result == []

    def test_preprocess_card_processes_double_faced_cards(self) -> None:
        """Test preprocess_card processes cards with card_faces (DFCs) correctly."""
        dfc_card = create_test_card(
            card_faces=[{"name": "Front", "type_line": "Creature — Human"}, {"name": "Back", "type_line": "Creature — Werewolf"}],
        )

        result = preprocess_card(dfc_card)
        # DFCs should return 2 cards (one per face)
        assert len(result) == 2
        assert result[0]["face_idx"] == 1
        assert result[0]["face_name"] == "Front"
        assert result[0]["card_name"] == "Test Card"
        assert result[1]["face_idx"] == 2
        assert result[1]["face_name"] == "Back"
        assert result[1]["card_name"] == "Test Card"

    def test_preprocess_card_filters_same_faced_double_side_cards(self) -> None:
        """Test preprocess_card filters out cards with the same name on both faces (X // X)."""
        same_faced_card = create_test_card(name="Soulflayer // Soulflayer")

        result = preprocess_card(same_faced_card)
        assert result == []

    def test_preprocess_card_filters_same_faced_cards_with_extra_whitespace(self) -> None:
        """Test preprocess_card filters out X // X cards regardless of whitespace."""
        same_faced_card = create_test_card(name="Aberrant  //  Aberrant")

        result = preprocess_card(same_faced_card)
        assert result == []

    def test_preprocess_card_allows_different_faced_double_side_cards(self) -> None:
        """Test preprocess_card does NOT filter out cards with different names on each face."""
        normal_dfc = create_test_card(
            name="Hound Tamer // Untamed Pup",
            card_faces=[
                {"name": "Hound Tamer", "type_line": "Creature — Human", "colors": ["G"], "color_identity": ["G"]},
                {"name": "Untamed Pup", "type_line": "Creature — Dog", "colors": [], "color_identity": ["G"]},
            ],
        )

        result = preprocess_card(normal_dfc)
        # Different names — should be processed normally (2 faces)
        assert len(result) == 2

    def test_preprocess_card_filters_all_not_legal_cards(self) -> None:
        """Test preprocess_card filters out cards that are not legal in any format."""
        no_legal_card = create_test_card(
            legalities=dict.fromkeys(["standard", "modern", "legacy", "vintage", "commander"], "not_legal"),
        )

        result = preprocess_card(no_legal_card)
        assert result == []

    def test_preprocess_card_filters_cards_only_banned(self) -> None:
        """Test preprocess_card filters out cards that are only banned (legal in no format)."""
        only_banned_card = create_test_card(
            legalities={
                "standard": "not_legal",
                "modern": "banned",
                "legacy": "banned",
                "vintage": "banned",
                "commander": "banned",
            },
        )

        result = preprocess_card(only_banned_card)
        assert result == []

    def test_preprocess_card_allows_restricted_cards(self) -> None:
        """Test preprocess_card keeps cards that are legal or restricted in at least one format."""
        restricted_card = create_test_card(
            legalities={
                "standard": "not_legal",
                "modern": "not_legal",
                "legacy": "banned",
                "vintage": "restricted",
                "commander": "banned",
            },
        )

        result = preprocess_card(restricted_card)
        assert len(result) == 1

    def test_preprocess_card_filters_funny_sets(self) -> None:
        """Test preprocess_card filters out funny set types."""
        invalid_card = create_test_card(
            set_type="funny",  # Funny set type
        )

        result = preprocess_card(invalid_card)
        assert result == []

    def test_preprocess_card_filters_card_type(self) -> None:
        """Test preprocess_card filters out cards with Card type."""
        invalid_card = create_test_card(
            type_line="Card",
        )

        result = preprocess_card(invalid_card)
        assert result == []

    def test_preprocess_card_filters_token_type(self) -> None:
        """Test preprocess_card filters out cards with Token type."""
        invalid_card = create_test_card(
            type_line="Token Creature — Goblin",
        )

        result = preprocess_card(invalid_card)
        assert result == []

    def test_preprocess_card_processes_valid_card(self) -> None:
        """Test preprocess_card processes valid cards correctly."""
        valid_card = create_test_card(
            card_id="00000000-0000-0000-0000-000000000006",
            name="Lightning Bolt",
            type_line="Instant",
            keywords=["haste"],
            prices={"usd": "0.25", "eur": "0.20", "tix": "0.01"},
            set_code="m15",
            artist="Christopher Rush",
            collector_number="1",
            edhrec_rank=1,
        )

        result = preprocess_card(valid_card)

        assert len(result) == 1
        result = result[0]
        assert result["card_types"] == ["Instant"]
        # card_subtypes is now always present, set to empty array when no subtypes
        assert result["card_subtypes"] == []
        assert result["card_colors"] == {"R": True}
        assert result["card_color_identity"] == {"R": True}
        assert result["card_keywords"] == {"haste": True}
        assert result["price_usd"] == 0.25
        assert result["price_eur"] == 0.20
        assert result["price_tix"] == 0.01
        assert result["card_set_code"] == "m15"

    def test_preprocess_card_processes_frame_data(self) -> None:
        """Test preprocess_card processes frame data correctly."""
        card_with_frame = create_test_card(
            frame="2015",
            frame_effects=["showcase", "legendary"],
        )

        result = preprocess_card(card_with_frame)

        assert len(result) == 1
        result = result[0]
        expected_frame_data = {"2015": True, "Showcase": True, "Legendary": True}
        assert result["card_frame_data"] == expected_frame_data

    def test_preprocess_card_handles_missing_frame_data(self) -> None:
        """Test preprocess_card handles missing frame data correctly."""
        card_without_frame = create_test_card(
            name="Regular Card",
            type_line="Creature — Human",
            colors=["W"],
            color_identity=["W"],
            keywords=[],
        )

        result = preprocess_card(card_without_frame)

        assert len(result) == 1
        result = result[0]
        assert result["card_frame_data"] == {}  # Should be empty object when no frame data present

    def test_extract_frame_data_from_raw_card_with_frame_and_effects(self) -> None:
        """Test extract_frame_data_from_raw_card with frame and frame_effects."""
        raw_card = {
            "frame": "2015",
            "frame_effects": ["showcase", "legendary"],
        }

        result = extract_frame_data_from_raw_card(raw_card)
        expected = {"2015": True, "Showcase": True, "Legendary": True}
        assert result == expected

    def test_extract_frame_data_from_raw_card_with_only_frame(self) -> None:
        """Test extract_frame_data_from_raw_card with only frame version."""
        raw_card = {"frame": "1997"}

        result = extract_frame_data_from_raw_card(raw_card)
        expected = {"1997": True}
        assert result == expected

    def test_extract_frame_data_from_raw_card_with_only_effects(self) -> None:
        """Test extract_frame_data_from_raw_card with only frame effects."""
        raw_card = {"frame_effects": ["borderless", "etched"]}

        result = extract_frame_data_from_raw_card(raw_card)
        expected = {"Borderless": True, "Etched": True}
        assert result == expected

    def test_extract_frame_data_from_raw_card_empty(self) -> None:
        """Test extract_frame_data_from_raw_card with empty raw card."""
        raw_card = {}

        result = extract_frame_data_from_raw_card(raw_card)
        expected = {}
        assert result == expected

    def test_preprocess_card_lowercases_keywords(self) -> None:
        """Keywords are stored lowercase so `keyword:` can find Scryfall's non-Title-Case spellings."""
        card = create_test_card(keywords=["First strike", "Double strike", "Doctor's companion", "Flying"])

        result = preprocess_card(card)[0]

        assert result["card_keywords"] == {
            "first strike": True,
            "double strike": True,
            "doctor's companion": True,
            "flying": True,
        }

    def test_preprocess_card_handles_missing_fields(self) -> None:
        """Test preprocess_card handles missing optional fields."""
        minimal_card = create_test_card(
            colors=[],
            color_identity=[],
            keywords=[],
            prices={},
        )

        result = preprocess_card(minimal_card)

        assert len(result) == 1
        result = result[0]
        assert result["card_colors"] == {}
        assert result["card_color_identity"] == {}
        assert result["card_keywords"] == {}
        assert result["creature_power"] is None
        assert result["creature_toughness"] is None
        assert result["price_usd"] is None
        assert result["price_eur"] is None
        assert result["price_tix"] is None

    def test_preprocess_card_defaults_missing_flavor_text_to_empty_string(self) -> None:
        """Scryfall omits flavor_text entirely when a printing has none; we normalize to ''."""
        card = create_test_card()
        assert "flavor_text" not in card

        result = preprocess_card(card)

        assert result[0]["flavor_text"] == ""

    def test_preprocess_card_defaults_null_flavor_text_to_empty_string(self) -> None:
        """An explicit null flavor_text (not just an absent key) also normalizes to ''."""
        card = create_test_card(flavor_text=None)

        result = preprocess_card(card)

        assert result[0]["flavor_text"] == ""

    def test_preprocess_card_preserves_present_flavor_text(self) -> None:
        """A real flavor_text value passes through unchanged."""
        card = create_test_card(flavor_text="A flavor line.")

        result = preprocess_card(card)

        assert result[0]["flavor_text"] == "A flavor line."

    def test_preprocess_card_handles_non_numeric_power_toughness(self) -> None:
        """Test preprocess_card handles non-numeric power/toughness values."""
        card = create_test_card(
            keywords=[],
            power="*",  # Non-numeric
            toughness="X",  # Non-numeric
            prices={},
        )

        result = preprocess_card(card)

        assert len(result) == 1
        result = result[0]
        assert result["creature_power"] is None
        assert result["creature_toughness"] is None

    def test_preprocess_hound_tamer_dfc(self) -> None:
        """Test preprocess_card processes Hound Tamer DFC sample data correctly."""
        sample_file = _SAMPLE_DATA_DIR / "hound_tamer.json"
        with sample_file.open() as f:
            hound_tamer = json.load(f)

        result = preprocess_card(hound_tamer)

        # Should return 2 faces
        assert len(result) == 2

        # Check front face
        front = result[0]
        assert front["face_idx"] == 1
        assert front["face_name"] == "Hound Tamer"
        assert front["card_name"] == "Hound Tamer // Untamed Pup"
        assert front["creature_power"] == 3
        assert front["creature_toughness"] == 3
        assert "Creature" in front["card_types"]
        assert front["cmc"] == 3

        # Check back face
        back = result[1]
        assert back["face_idx"] == 2
        assert back["face_name"] == "Untamed Pup"
        assert back["card_name"] == "Hound Tamer // Untamed Pup"
        assert back["creature_power"] == 4
        assert back["creature_toughness"] == 4
        assert "Creature" in back["card_types"]
        # CMC is inherited from the card (3), even though back face has no mana cost
        assert back["cmc"] == 3

    def test_preprocess_obyras_attendants(self) -> None:
        """Test preprocess_card processes Obyra's Attendants DFC sample data correctly."""
        sample_file = _SAMPLE_DATA_DIR / "obyras_attendants.json"
        with sample_file.open() as f:
            obyras_attendants = json.load(f)

        result = preprocess_card(obyras_attendants)

        # Should return 2 faces
        front, back = result
        assert front["creature_power"] == 3
        assert back.get("creature_power") is None
        assert front["card_types"] == ["Creature"]
        assert back["card_types"] == ["Instant"]


class TestColorOrderRank:
    """`color_order` -- a card's place under Scryfall's `order=color`, decided at import."""

    @pytest.mark.parametrize(("expected", "card"), COLOR_ORDER_CARDS, ids=[card["name"] for _rank, card in COLOR_ORDER_CARDS])
    def test_a_real_card_takes_scryfalls_place(self, expected: int, card: dict[str, Any]) -> None:
        assert color_order_rank(card) == expected

    def test_the_eighteen_are_listed_in_the_order_the_rank_gives(self) -> None:
        """The pool is Scryfall's ascending answer, so rank then name must reproduce it as listed."""
        listed = [card["name"] for _rank, card in COLOR_ORDER_CARDS]
        ranks = {card["name"]: rank for rank, card in COLOR_ORDER_CARDS}
        assert sorted(listed, key=lambda name: (ranks[name], name.lower())) == listed

    def test_every_pair_and_triple_has_its_own_place_in_the_games_order(self) -> None:
        """The pairs do not tie: allied then enemy, shards then wedges, each in its printed sequence."""
        combinations = [
            *("W", "U", "B", "R", "G"),
            *("WU", "UB", "BR", "RG", "GW", "WB", "UR", "BG", "RW", "GU"),
            *("WUB", "UBR", "BRG", "RGW", "GWU", "WBG", "URW", "BGU", "RWB", "GUR"),
            *("WUBR", "UBRG", "BRGW", "RGWU", "GWUB", "WUBRG"),
        ]
        ranks = [color_order_rank({"colors": list(c), "color_identity": list(c), "type_line": "Instant"}) for c in combinations]
        assert ranks == list(range(31))
        # Whatever order Scryfall lists the letters in: its arrays are alphabetical, not WUBRG.
        assert color_order_rank({"colors": ["G", "W"], "color_identity": ["G", "W"], "type_line": "Instant"}) == combinations.index(
            "GW"
        )

    def test_a_colourless_card_and_a_land_are_placed_by_identity_after_every_colour(self) -> None:
        artifact = {"colors": [], "type_line": "Artifact"}
        land = {"colors": [], "type_line": "Land"}
        assert color_order_rank(artifact | {"color_identity": ["W"]}) == 32
        assert color_order_rank(artifact | {"color_identity": ["G", "U"]}) == 32 + 14
        assert color_order_rank(artifact | {"color_identity": []}) == 32 + 31
        assert color_order_rank(land | {"color_identity": ["W"]}) == 64
        assert color_order_rank(land | {"color_identity": []}) == 64 + 31
        # A land with a colour of its own is still a land, by its identity.
        assert color_order_rank({"colors": ["G"], "color_identity": ["G"], "type_line": "Land Creature — Forest Dryad"}) == 64 + 4

    def test_the_front_face_decides_and_the_back_face_does_not(self) -> None:
        """Brigid, Clachan's Heart: a white front and a green back is a white card, not green-white."""
        brigid = {
            "name": "Brigid, Clachan's Heart // Brigid, Doun's Mind",
            "type_line": "Legendary Creature — Kithkin Warrior // Legendary Creature — Kithkin Soldier",
            "color_identity": ["G", "W"],
            "card_faces": [
                {"name": "Brigid, Clachan's Heart", "type_line": "Legendary Creature — Kithkin Warrior", "colors": ["W"]},
                {"name": "Brigid, Doun's Mind", "type_line": "Legendary Creature — Kithkin Soldier", "colors": ["G"]},
            ],
        }
        assert color_order_rank(brigid) == 0
        turned = brigid | {"card_faces": brigid["card_faces"][::-1]}
        assert color_order_rank(turned) == 4

    def test_a_word_that_only_contains_land_is_not_a_land(self) -> None:
        assert color_order_rank({"colors": [], "color_identity": [], "type_line": "Artifact — Landmark"}) == 32 + 31
        assert color_order_rank({"colors": ["U"], "color_identity": ["U"], "type_line": "Creature — Islandwalker"}) == 1

    def test_preprocess_card_writes_it_on_every_row_of_a_card(self) -> None:
        """Every face's row carries the CARD's place, so whichever row is stored holds the front's answer."""
        expected = {card["name"]: rank for rank, card in COLOR_ORDER_CARDS}
        for raw in color_order_raw_cards():
            rows = preprocess_card(raw)
            assert rows, raw["name"]
            assert [row["color_order"] for row in rows] == [expected[raw["name"]]] * len(rows), raw["name"]
