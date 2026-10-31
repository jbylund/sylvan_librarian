"""Tests for card processing functions."""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any

import pytest

from api.card_processing import ROLE_CLASSES_KEY, extract_frame_data_from_raw_card, preprocess_card, role_classes

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


_EVERYWHERE_LEGAL = {"commander": "legal", "brawl": "legal", "competitivebrawl": "legal", "duel": "legal", "oathbreaker": "legal"}
# Printed before Arena: playable in the paper formats, not in Brawl.
_PAPER_ONLY = _EVERYWHERE_LEGAL | {"brawl": "not_legal", "competitivebrawl": "not_legal"}


def _face(type_line: str, oracle_text: str = "", toughness: str | None = None) -> dict[str, Any]:
    """One card face, shaped as Scryfall sends it: `toughness` is absent, not null, without one."""
    face: dict[str, Any] = {"type_line": type_line, "oracle_text": oracle_text}
    if toughness is not None:
        face["toughness"] = toughness
    return face


def _role_card(  # noqa: PLR0913
    type_line: str,
    *,
    oracle_text: str = "",
    toughness: str | None = None,
    layout: str = "normal",
    legalities: dict[str, str] | None = None,
    faces: list[dict[str, Any]] | None = None,
    name: str = "Role Test",
    all_parts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The fields `role_classes` reads, on a card shaped as Scryfall sends it."""
    card = {"id": str(uuid.uuid4()), "name": name, "layout": layout, "legalities": legalities or _EVERYWHERE_LEGAL}
    card |= _face(type_line, oracle_text, toughness)
    if faces is not None:
        card["card_faces"] = faces
    if all_parts is not None:
        card["all_parts"] = all_parts
    return card


def _meld_parts(*roles_and_names: tuple[str, str], own: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """`all_parts` entries; the one named like `own` carries its id, the rest a stranger's."""
    return [
        {"component": role, "name": name, "id": own["id"] if own is not None and own["name"] == name else str(uuid.uuid4())}
        for role, name in roles_and_names
    ]


_BRISELA_PARTS = (
    ("meld_result", "Brisela, Voice of Nightmares"),
    ("meld_part", "Bruna, the Fading Light"),
    ("meld_part", "Gisela, the Broken Blade"),
)

# Each case is a real card, reduced to the fields the rules read, with api.scryfall.com's answer
# for it (2026-09-26). (id, card, the role classes it carries)
_ROLE_CASES: list[tuple[str, dict[str, Any], set[str]]] = [
    (
        "a legendary creature leads every kind of deck",
        _role_card("Legendary Creature — Bird Wizard", toughness="3"),
        {"spell", "commander", "brawler", "duelcommander"},
    ),
    ("an instant is a spell and nothing else", _role_card("Instant"), {"spell"}),
    ("a nonlegendary creature is no commander", _role_card("Creature — Human Monk", toughness="2"), {"spell"}),
    # Derevi, Empyrial Tactician: Duel Commander's "banned as commander" list is `restricted`.
    (
        "duel's restricted is banned as commander",
        _role_card("Legendary Creature — Bird Wizard", toughness="3", legalities=_EVERYWHERE_LEGAL | {"duel": "restricted"}),
        {"spell", "commander", "brawler"},
    ),
    # Griselbrand.
    (
        "a banned commander is no commander",
        _role_card("Legendary Creature — Demon", toughness="7", legalities=_PAPER_ONLY | {"commander": "banned"}),
        {"spell", "duelcommander"},
    ),
    # Arena's legends are `not_legal` in Commander, not banned, and Scryfall counts them.
    (
        "not_legal in commander still counts",
        _role_card("Legendary Creature — Human Soldier", toughness="1", legalities={"commander": "not_legal", "brawl": "legal"}),
        {"spell", "commander", "brawler"},
    ),
    # Tajic, Legion's Valor: brawl-legal, banned in competitive brawl.
    (
        "a competitive brawl ban is no brawler",
        _role_card(
            "Legendary Creature — Human Soldier",
            toughness="1",
            legalities={"commander": "not_legal", "brawl": "legal", "competitivebrawl": "banned"},
        ),
        {"spell", "commander"},
    ),
    # Heart of Kiran: a printed toughness leads Commander and Brawl decks, not Duel Commander ones.
    ("a legendary vehicle", _role_card("Legendary Artifact — Vehicle", toughness="4"), {"spell", "commander", "brawler"}),
    # Acolyte of Bahamut (never on Arena).
    ("a background", _role_card("Legendary Enchantment — Background", legalities=_PAPER_ONLY), {"spell", "commander"}),
    ("a legendary artifact with no toughness", _role_card("Legendary Artifact"), {"spell"}),
    # Teyo: Brawl takes a legendary planeswalker; Oathbreaker takes any.
    ("a legendary planeswalker", _role_card("Legendary Planeswalker — Teyo"), {"spell", "brawler", "oathbreaker"}),
    # Grist, the Hunger Tide: a creature everywhere but the battlefield.
    (
        "a planeswalker that is a creature off the battlefield",
        _role_card(
            "Legendary Planeswalker — Grist",
            oracle_text="As long as Grist isn\u2019t on the battlefield, it\u2019s a 1/1 Insect creature in addition to its other types.\n+1: Create a token.",
        ),
        {"spell", "commander", "brawler", "duelcommander", "oathbreaker"},
    ),
    (
        "a planeswalker that can be your commander",
        _role_card(
            "Legendary Planeswalker — Daretti",
            oracle_text="+2: Discard up to two cards.\nDaretti, Scrap Savant can be your commander.",
        ),
        {"spell", "commander", "brawler", "duelcommander", "oathbreaker"},
    ),
    # Budoka Pupil // Ichiga, Who Topples Oaks: the legend is the FLIPPED half.
    (
        "a flip card's flipped legend",
        _role_card(
            "Creature — Human Monk // Legendary Creature — Spirit",
            layout="flip",
            toughness="2",
            legalities=_PAPER_ONLY,
            faces=[_face("Creature — Human Monk", toughness="2"), _face("Legendary Creature — Spirit", toughness="3")],
        ),
        {"spell"},
    ),
    # Homura, Human Ascendant // Homura's Essence.
    (
        "a flip card's legendary front",
        _role_card(
            "Legendary Creature — Human Monk // Legendary Enchantment",
            layout="flip",
            toughness="4",
            legalities=_PAPER_ONLY,
            faces=[_face("Legendary Creature — Human Monk", toughness="4"), _face("Legendary Enchantment")],
        ),
        {"spell", "commander", "duelcommander"},
    ),
    # Westvale Abbey // Ormendahl, Profane Prince: a land you play, a legend you turn it into.
    (
        "a transform card's legendary back",
        _role_card(
            "Land // Legendary Creature — Demon",
            layout="transform",
            faces=[_face("Land"), _face("Legendary Creature — Demon", toughness="7")],
        ),
        set(),
    ),
    # Kytheon, Hero of Akros // Gideon, Battle-Forged: the planeswalker is a back, so no oathbreaker.
    (
        "a legendary creature with a planeswalker back",
        _role_card(
            "Legendary Creature — Human Soldier // Legendary Planeswalker — Gideon",
            layout="transform",
            faces=[_face("Legendary Creature — Human Soldier", toughness="1"), _face("Legendary Planeswalker — Gideon")],
        ),
        {"spell", "commander", "brawler", "duelcommander"},
    ),
    # Valki, God of Lies // Tibalt, Cosmic Impostor: castable as either, led by the front.
    (
        "a modal card with a planeswalker back",
        _role_card(
            "Legendary Creature — God // Legendary Planeswalker — Tibalt",
            layout="modal_dfc",
            faces=[_face("Legendary Creature — God", toughness="1"), _face("Legendary Planeswalker — Tibalt")],
        ),
        {"spell", "commander", "brawler", "duelcommander"},
    ),
    # Agadeem's Awakening // Agadeem, the Undercrypt.
    (
        "a modal spell // land",
        _role_card("Sorcery // Land", layout="modal_dfc", faces=[_face("Sorcery"), _face("Land")]),
        {"spell"},
    ),
    ("a split card", _role_card("Instant // Sorcery", layout="split", faces=[_face("Instant"), _face("Sorcery")]), {"spell"}),
    ("a land", _role_card("Land"), set()),
    # Seat of the Synod: an Artifact that is a land is not cast.
    ("an artifact land", _role_card("Artifact Land"), set()),
    # Ferris Wheel: Attractions are visited, not cast.
    ("an attraction", _role_card("Artifact — Attraction"), set()),
    ("a conspiracy", _role_card("Conspiracy"), set()),
    # Aswan Jaguar: the creature spelling before Sixth Edition is a spell on Scryfall.
    ("a pre-Sixth-Edition Summon", _role_card("Summon Jaguar", toughness="2"), {"spell"}),
    ("a token", _role_card("Token Legendary Creature — God", layout="token", toughness="5"), set()),
]


class TestRoleClasses:
    """`role_classes`: who can lead a deck, and what is cast, read from the face you cast."""

    @pytest.mark.parametrize(
        ("card", "expected"), [(card, expected) for _, card, expected in _ROLE_CASES], ids=[name for name, _, _ in _ROLE_CASES]
    )
    def test_role_classes(self, card: dict[str, Any], expected: set[str]) -> None:
        assert set(role_classes(card)) == expected

    def test_meld_result_is_a_spell_and_no_commander(self) -> None:
        """Brisela, Voice of Nightmares: a legendary creature nobody can cast."""
        card = _role_card("Legendary Creature — Eldrazi Angel", layout="meld", toughness="10", name="Brisela, Voice of Nightmares")
        card["all_parts"] = _meld_parts(*_BRISELA_PARTS, own=card)
        assert set(role_classes(card)) == {"spell"}

    def test_meld_part_is_a_commander(self) -> None:
        """Gisela, the Broken Blade: the half you cast leads a deck."""
        card = _role_card("Legendary Creature — Angel Horror", layout="meld", toughness="3", name="Gisela, the Broken Blade")
        card["all_parts"] = _meld_parts(*_BRISELA_PARTS, own=card)
        assert set(role_classes(card)) == {"spell", "commander", "brawler", "duelcommander"}

    def test_meld_result_is_found_by_name_when_all_parts_names_a_sibling_printing(self) -> None:
        """Ragnarok, Divine Deliverance fin/99b: none of its `all_parts` ids is its own."""
        result = _role_card("Legendary Creature — Beast Avatar", layout="meld", toughness="6", name="Ragnarok, Divine Deliverance")
        part = _role_card("Legendary Creature — Human Cleric", layout="meld", toughness="2", name="Vanille, Cheerful l\u2019Cie")
        parts = _meld_parts(
            ("meld_part", "Fang, Fearless l\u2019Cie"),
            ("meld_result", "Ragnarok, Divine Deliverance"),
            ("meld_part", "Vanille, Cheerful l\u2019Cie"),
        )
        result["all_parts"] = part["all_parts"] = parts
        assert set(role_classes(result)) == {"spell"}
        assert set(role_classes(part)) == {"spell", "commander", "brawler", "duelcommander"}

    def test_preprocess_card_writes_the_cards_role_classes_onto_every_face(self) -> None:
        """Both face rows share a scryfall_id and one survives the upsert; either must hold the card's answer."""
        card = create_test_card(
            name="Kytheon, Hero of Akros // Gideon, Battle-Forged",
            type_line="Legendary Creature — Human Soldier // Legendary Planeswalker — Gideon",
            legalities=_EVERYWHERE_LEGAL,
            layout="transform",
            card_faces=[
                {
                    "name": "Kytheon, Hero of Akros",
                    "type_line": "Legendary Creature — Human Soldier",
                    "power": "2",
                    "toughness": "1",
                },
                {"name": "Gideon, Battle-Forged", "type_line": "Legendary Planeswalker — Gideon", "loyalty": "3"},
            ],
        )
        front, back = preprocess_card(card)
        expected = ["spell", "commander", "brawler", "duelcommander"]
        assert front["raw_card_blob"][ROLE_CLASSES_KEY] == expected
        assert back["raw_card_blob"][ROLE_CLASSES_KEY] == expected

    def test_preprocess_card_leaves_a_card_with_no_role_class_untouched(self) -> None:
        card = create_test_card(type_line="Land", legalities=_EVERYWHERE_LEGAL)
        (row,) = preprocess_card(card)
        assert ROLE_CLASSES_KEY not in row["raw_card_blob"]
