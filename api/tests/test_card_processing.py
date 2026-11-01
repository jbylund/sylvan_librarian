"""Tests for card processing functions."""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any

import pytest

from api.card_processing import extract_frame_data_from_raw_card, preprocess_card

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

    def test_preprocess_card_keeps_a_fractional_mana_value(self) -> None:
        """A half-mana card's cmc survives the import cast.

        Scryfall types cmc Decimal, and the {HW} symbol is what makes that matter:
        /cards/named?exact=Little+Girl answers "mana_cost":"{HW}", "cmc":0.5. The cast here
        used to be int(float(val)), which turned that into 0 — the same value a zero-cost
        card has. (Little Girl itself is still filtered out of the corpus by the funny-set
        and legality rules above; this is about the cast, not the corpus.)
        """
        card = create_test_card(name="Little Girl", mana_cost="{HW}", cmc=0.5, power="1", toughness="1")

        result = preprocess_card(card)

        assert result[0]["cmc"] == 0.5

    def test_preprocess_card_keeps_a_whole_mana_value_whole(self) -> None:
        """Widening the cast must not perturb the ~31.5k rows that are already integral."""
        card = create_test_card(name="Lightning Bolt", mana_cost="{R}", cmc=1)

        result = preprocess_card(card)

        assert result[0]["cmc"] == 1

    def test_preprocess_card_keeps_a_fractional_power_and_toughness(self) -> None:
        """A printed half survives the import cast, in the column the query compares.

        Power and toughness are STRINGS in Scryfall's schema, and eleven cards print a half
        in them — checked against the live API on 2026-08-17,
        /cards/named?exact=Little+Girl answers "power":".5", "toughness":".5". The cast here
        used to be int(float(val)), which turned .5 into 0.
        """
        card = create_test_card(name="Little Girl", power=".5", toughness=".5")

        result = preprocess_card(card)

        assert result[0]["creature_power"] == 0.5
        assert result[0]["creature_toughness"] == 0.5

    def test_preprocess_card_does_not_round_a_half_onto_its_floor(self) -> None:
        """The direction an integer column gets wrong SILENTLY.

        `power=2` finding nothing when a 2.5 card exists would be visible. `power=2`
        matching that card is not — and truncation does the second, not the first.
        """
        card = create_test_card(name="Smart Ass", power="2.5", toughness="1")

        result = preprocess_card(card)

        assert result[0]["creature_power"] == 2.5
        assert result[0]["creature_power"] != 2

    def test_preprocess_card_keeps_whole_stats_whole(self) -> None:
        """Widening the cast must not perturb the rows that are already integral."""
        card = create_test_card(name="Grizzly Bears", power="2", toughness="2")

        result = preprocess_card(card)

        assert result[0]["creature_power"] == 2
        assert result[0]["creature_toughness"] == 2

    def test_preprocess_card_keeps_a_negative_power(self) -> None:
        """Power is signed and stays so: Char-Rumbler and Spinal Parasite print -1."""
        card = create_test_card(name="Spinal Parasite", power="-1", toughness="-1")

        result = preprocess_card(card)

        assert result[0]["creature_power"] == -1
        assert result[0]["creature_toughness"] == -1

    @pytest.mark.parametrize(
        ("name", "type_line", "power", "toughness"),
        [
            ("Old Fogey", "Summon — Dinosaur", "7", "7"),
            ("Flanking Licid", "Summon Licid", "1", "1"),
            ("Atinlay Igpay", "Eaturecray — Igpay", "3", "3"),
        ],
    )
    def test_preprocess_card_keeps_a_printed_stat_behind_a_noncanonical_type_line(
        self,
        name: str,
        type_line: str,
        power: str,
        toughness: str,
    ) -> None:
        """A printed power/toughness is data even when the type line does not say Creature.

        The pre-Sixth-Edition `Summon` template is what most of these are, preserved verbatim by
        Scryfall on the printings that used it; Atinlay Igpay is the pig-latin one. Checked
        against the live API on 2026-08-17, `-t:creature -t:vehicle -t:spacecraft (pow>=0 or
        pow<0) include:extras` with unique=prints returns exactly eighteen printings, and
        api.scryfall.com searches every one of them numerically — `pow=3` and `tou=3` both count
        Atinlay Igpay, `tou>=3.5` counts Old Fogey. The keys, not the parsed type, decide whether
        a stat exists.
        """
        card = create_test_card(name=name, type_line=type_line, power=power, toughness=toughness)

        result = preprocess_card(card)[0]

        assert result["creature_power"] == float(power)
        assert result["creature_toughness"] == float(toughness)
        assert result["creature_power_text"] == power
        assert result["creature_toughness_text"] == toughness

    @pytest.mark.parametrize("type_line", ["Summon — Dinosaur", "Eaturecray — Igpay"])
    def test_preprocess_card_does_not_invent_a_creature_type_from_a_printed_stat(self, type_line: str) -> None:
        """Keeping the stat must not widen the type parse, or `t:creature` breaks instead.

        `Summon` and `Eaturecray` are unrecognised words and stay that way. Teaching
        parse_type_line to read them as `Creature` would have made the stat counts agree too,
        at the price of a card type the printing does not have.
        """
        card = create_test_card(type_line=type_line, power="3", toughness="3")

        result = preprocess_card(card)[0]

        assert "Creature" not in result["card_types"]

    def test_preprocess_card_does_not_give_a_face_the_card_level_stat(self) -> None:
        """A face's stats are the face's. The raw-key rule above is for whole cards only.

        `merged = card | face_data` lets a face inherit the card-level `power`, and Scryfall
        publishes one there for adventures: Obyra's Attendants is `3`/`4` at the top level while
        its Adventure face prints neither. For a face the parsed type line is the only
        trustworthy signal — and all eighteen noncanonical-type-line printings are single-faced,
        so nothing is lost by keeping the face path on the type gate.
        """
        sample_file = _SAMPLE_DATA_DIR / "obyras_attendants.json"
        with sample_file.open() as f:
            obyras_attendants = json.load(f)

        assert obyras_attendants["power"] == "3"
        assert "power" not in obyras_attendants["card_faces"][1]

        _front, back = preprocess_card(obyras_attendants)

        assert back["card_types"] == ["Instant"]
        assert back["creature_power"] is None
        assert back["creature_power_text"] is None
        assert back["creature_toughness"] is None
        assert back["creature_toughness_text"] is None

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
