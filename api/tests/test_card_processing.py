"""Tests for card processing functions."""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any

from api.card_processing import preprocess_card
from api.parsing.card_query_nodes import extract_frame_data_from_raw_card

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

    def test_preprocess_card_merges_double_faced_cards_into_one_row(self) -> None:
        """A multi-face card produces exactly ONE row, so faces no longer fight for the PK.

        The old per-face fan-out emitted N rows sharing one scryfall_id; the upsert's
        ON CONFLICT then kept whichever face came last — the back — which is how every
        battle, MDFC spell side, and front-face text went missing (#400, #873).
        """
        dfc_card = create_test_card(
            card_faces=[{"name": "Front", "type_line": "Creature — Human"}, {"name": "Back", "type_line": "Creature — Werewolf"}],
        )

        result = preprocess_card(dfc_card)
        assert len(result) == 1
        merged = result[0]
        assert merged["card_name"] == "Test Card"
        assert merged["scryfall_id"] == dfc_card["id"]
        assert merged["card_subtypes"] == ["Human", "Werewolf"]
        assert merged["type_line"] == "Creature — Human // Creature — Werewolf"

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
        assert len(result) == 1
        assert result[0]["card_name"] == "Hound Tamer // Untamed Pup"

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
        """A real transform card merges to one row: front stats, both faces searchable."""
        sample_file = _SAMPLE_DATA_DIR / "hound_tamer.json"
        with sample_file.open() as f:
            hound_tamer = json.load(f)

        result = preprocess_card(hound_tamer)

        assert len(result) == 1
        merged = result[0]
        assert merged["card_name"] == "Hound Tamer // Untamed Pup"
        # Front face supplies the stat group (the 3/3, not the pup's 4/4)
        assert merged["creature_power"] == 3
        assert merged["creature_toughness"] == 3
        assert merged["cmc"] == 3
        assert merged["mana_cost_text"] == "{2}{G}"
        # Both faces' searchable data is present
        assert merged["card_subtypes"] == ["Human", "Werewolf"]
        assert merged["type_line"] == "Creature — Human Werewolf // Creature — Werewolf"
        assert "Trample" in merged["oracle_text"]  # front-face text
        assert "Nightbound" in merged["oracle_text"]  # back-face text
        assert "nightbound" in merged["card_keywords"]

    def test_preprocess_obyras_attendants(self) -> None:
        """A real adventure card merges to one row with both faces' types searchable."""
        sample_file = _SAMPLE_DATA_DIR / "obyras_attendants.json"
        with sample_file.open() as f:
            obyras_attendants = json.load(f)

        result = preprocess_card(obyras_attendants)

        assert len(result) == 1
        merged = result[0]
        # Creature body's stats, both faces' types: `t:creature t:instant` both match now
        assert merged["creature_power"] == 3
        assert merged["card_types"] == ["Creature", "Instant"]
        assert merged["card_subtypes"] == ["Faerie", "Wizard", "Adventure"]


class TestFaceMerging:
    """The face-merge policy: front-face identity, any-face searchability (#400, #873).

    Scryfall AND's predicates at the card level, each satisfiable by any face (measured
    2026-08-08: `t:sorcery t:land` returns MDFC lands, `o:` conjunctions match across
    faces, `c:b` matches Westvale Abbey's back-face-only color). These tests pin the
    merged row to those semantics.
    """

    @staticmethod
    def _battle_card() -> dict:
        """A transform battle shaped like Invasion of Kamigawa // Rooftop Saboteurs."""
        return create_test_card(
            name="Invasion of Testing // Test Saboteurs",
            layout="transform",
            cmc=3,
            card_faces=[
                {
                    "name": "Invasion of Testing",
                    "type_line": "Battle — Siege",
                    "mana_cost": "{2}{U}",
                    "colors": ["U"],
                    "oracle_text": "When this Siege enters, look at the top card.",
                    "illustration_id": "11111111-1111-1111-1111-111111111111",
                },
                {
                    "name": "Test Saboteurs",
                    "type_line": "Creature — Moonfolk Ninja",
                    "mana_cost": "",
                    "colors": ["U"],
                    "power": "3",
                    "toughness": "2",
                    "oracle_text": "This creature can't be blocked.",
                    "illustration_id": "22222222-2222-2222-2222-222222222222",
                },
            ],
        )

    def test_battle_front_types_are_searchable(self) -> None:
        """`t:battle` must match transform battles — the union carries the front's types.

        The acceptance test from #400: Battle appears in zero type lines corpus-wide today
        because every battle is stored as its back face.
        """
        merged = preprocess_card(self._battle_card())[0]
        assert merged["card_types"] == ["Battle", "Creature"]
        assert merged["card_subtypes"] == ["Siege", "Moonfolk", "Ninja"]

    def test_front_face_supplies_identity_scalars(self) -> None:
        """Mana cost, illustration, and image come from the front face, as on Scryfall."""
        merged = preprocess_card(self._battle_card())[0]
        assert merged["mana_cost_text"] == "{2}{U}"
        assert merged["illustration_id"] == "11111111-1111-1111-1111-111111111111"

    def test_oracle_text_joins_faces_with_separator(self) -> None:
        """Each face's text is substring-searchable in the one joined column.

        The newline separator keeps `.`-based regexes from matching across the face boundary.
        """
        merged = preprocess_card(self._battle_card())[0]
        assert merged["oracle_text"] == ("When this Siege enters, look at the top card.\n//\nThis creature can't be blocked.")

    def test_back_face_stats_used_when_front_has_none(self) -> None:
        """A land-front / creature-back card (Westvale Abbey) keeps the back's P/T.

        The front offers none, and Scryfall's pow: matches the back there too.
        """
        card = create_test_card(
            name="Test Abbey // Test Prince",
            card_faces=[
                {"name": "Test Abbey", "type_line": "Land", "colors": [], "oracle_text": "{T}: Add {C}."},
                {
                    "name": "Test Prince",
                    "type_line": "Legendary Creature — Demon",
                    "colors": ["B"],
                    "power": "9",
                    "toughness": "7",
                    "oracle_text": "Flying, lifelink.",
                },
            ],
        )
        merged = preprocess_card(card)[0]
        assert merged["creature_power"] == 9
        assert merged["creature_toughness"] == 7
        # ...and the back-face-only color is searchable (c:b matches on Scryfall)
        assert merged["card_colors"] == {"B": True}

    def test_front_face_stats_win_when_both_faces_have_them(self) -> None:
        """Two creature faces (Brutal Cathar's 2/2 // 3/3): the front's group wins.

        Known residual vs Scryfall, which also matches the back's pow=3; documented in
        the merge policy and left for a per-face follow-up if measurement warrants.
        """
        card = create_test_card(
            name="Test Cathar // Test Brute",
            card_faces=[
                {"name": "Test Cathar", "type_line": "Creature — Human Soldier", "colors": ["W"], "power": "2", "toughness": "2"},
                {"name": "Test Brute", "type_line": "Creature — Werewolf", "colors": [], "power": "3", "toughness": "3"},
            ],
        )
        merged = preprocess_card(card)[0]
        assert merged["creature_power"] == 2
        assert merged["creature_toughness"] == 2
        assert merged["creature_power_text"] == "2"

    def test_stat_group_stays_face_consistent(self) -> None:
        """The numeric and _text stat columns always describe the same face.

        A `*`-power back face still counts as carrying the group: its text is real data.
        """
        card = create_test_card(
            name="Test Land // Test Goyf",
            card_faces=[
                {"name": "Test Land", "type_line": "Land", "colors": []},
                {"name": "Test Goyf", "type_line": "Creature — Lhurgoyf", "colors": ["G"], "power": "*", "toughness": "1+*"},
            ],
        )
        merged = preprocess_card(card)[0]
        assert merged["creature_power"] is None  # "*" is non-numeric
        assert merged["creature_power_text"] == "*"
        assert merged["creature_toughness_text"] == "1+*"

    def test_mdfc_spell_and_land_types_both_searchable(self) -> None:
        """`t:sorcery t:land` matches an MDFC (Agadeem's Awakening) — #400's acceptance."""
        card = create_test_card(
            name="Test Awakening // Test Undercrypt",
            layout="modal_dfc",
            card_faces=[
                {
                    "name": "Test Awakening",
                    "type_line": "Sorcery",
                    "mana_cost": "{X}{B}{B}{B}",
                    "colors": ["B"],
                    "oracle_text": "Return cards from your graveyard.",
                },
                {
                    "name": "Test Undercrypt",
                    "type_line": "Land",
                    "mana_cost": "",
                    "colors": [],
                    "oracle_text": "As this land enters, you may pay 3 life.",
                },
            ],
        )
        merged = preprocess_card(card)[0]
        assert merged["card_types"] == ["Sorcery", "Land"]
        assert merged["mana_cost_text"] == "{X}{B}{B}{B}"

    def test_all_faces_filtered_drops_the_card(self) -> None:
        """A card whose every face is filtered (e.g. Token type lines) yields no row."""
        card = create_test_card(
            name="Test A // Test B",
            card_faces=[
                {"name": "Test A", "type_line": "Token Creature — Goblin"},
                {"name": "Test B", "type_line": "Token Creature — Elf"},
            ],
        )
        assert preprocess_card(card) == []
