"""Tests for card processing functions."""

from __future__ import annotations

import copy
import json
import pathlib
import uuid
from typing import Any, ClassVar

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

    def test_preprocess_card_tags_extras_instead_of_dropping_them(self) -> None:
        """Nothing is filtered out any more; the classes that were become `is:extra`.

        api.scryfall.com serves every class this function used to refuse, and hides four of them
        from a default `/cards/search` behind `include_extras=false` — a query-time gate an absent
        row cannot reproduce in either direction (`/cards/named?exact=` answers all of them, and
        `include_extras=true` has nothing to include). Probed one class at a time, 2026-08-16.
        """

        def tags(**overrides: object) -> dict[str, object]:
            rows = preprocess_card(create_test_card(**overrides))
            assert len(rows) == 1, "every row is imported now"
            return rows[0].get("card_is_tags", {})

        # ORDINARY — served by a bare `/cards/search`.
        assert "extra" not in tags(legalities={"vintage": "not_legal"}), "cn2/6 is 200 bare"
        assert "extra" not in tags(set_type="funny", set_code="ust"), "e:ust answers 249 either way"
        assert "extra" not in tags(layout="reversible_card", name="Echo // Echo"), "tdm/380 is 200 bare"
        assert "extra" not in tags(promo_types=["sldbonus", "playtest"]), "sld/SCTLR is legal and served"
        # Unstable's Hosts and Augments: `is:extra e:ust` answers 0, and both layouts were in
        # _EXTRA_LAYOUTS until 2026-08-16. Asserted as a pair so re-adding either fails here.
        assert "extra" not in tags(layout="host", set_type="funny", set_code="ust"), "is:extra e:ust is 0"
        assert "extra" not in tags(layout="augment", set_type="funny", set_code="ust"), "is:extra e:ust is 0"
        # A funny set _FUNNY_EXTRA_SETS has never heard of is SERVED, not hidden — the stale-list
        # failure mode that direction was chosen for.
        assert "extra" not in tags(set_type="funny", set_code="un99"), "an unlisted funny set defaults to served"
        assert "extra" not in tags(set_type="funny", set_code="sunf", type_line="Stickers"), "sunf's sheets are served"
        # ...and the playtest promo inside a served un-set: `und`/`unh`'s "Look at Me, I'm R&D" is a
        # real Un-card that merely depicts a playtest card, and `is:extra e:und` answers 0.
        assert "extra" not in tags(
            set_type="funny", set_code="und", promo_types=["playtest"], legalities={"vintage": "not_legal"}
        ), "is:extra e:und is 0"
        # Digital and never-legal are each ordinary alone; only the conjunction is the class.
        assert "extra" not in tags(digital=True, set_type="alchemy"), "a playable Alchemy card is served"
        assert "extra" not in tags(border_color="silver", set_type="expansion"), "567 silver printings are served"

        # EXTRA — 404 bare, 200 with include_extras=true.
        assert "extra" in tags(set_type="memorabilia"), "ced/78 appears only with extras"
        assert "extra" in tags(type_line="Card"), "tmkc/31"
        assert "extra" in tags(type_line="Token Creature — Goblin"), "thob/4"
        assert "extra" in tags(layout="planar"), "opc2/38"
        assert "extra" in tags(layout="art_series", name="Echo // Echo"), "unlike its reversible cousin"
        assert "extra" in tags(promo_types=["playtest"], legalities={"vintage": "not_legal"}), "mb2/536"
        # `content_warning`, the flag with no other signal behind it: layout `normal`, an ordinary
        # type line, legal somewhere. `is:extra e:lea` answers 1 and that one card is Crusade.
        assert "extra" in tags(content_warning=True), "lea/61 Crusade"
        # A funny ODDITY set: `is:extra e:ulst` is 62 of 62, and its rows are field-for-field
        # indistinguishable from the ust twins above — the set code is the whole signal.
        assert "extra" in tags(set_type="funny", set_code="ulst", border_color="silver"), "is:extra e:ulst is 62"
        # ...and a TOKEN in an unlisted funny set is still one: the funny rule adds and never
        # subtracts, so a stale list cannot make a future un-set's tokens vanish from search.
        assert "extra" in tags(set_type="funny", set_code="un99", layout="token"), "tokens survive a stale list"
        # A digital printing legal in NO format: `is:extra e:hbg` is 122, 104 of them this class.
        assert "extra" in tags(digital=True, set_type="alchemy", legalities={"alchemy": "not_legal", "historic": "not_legal"}), (
            "hbg's Arena-only duplicates"
        )
        # A silver-bordered promo: pal04's Arena League un-cards, j17's Rules Lawyer, pust/punh.
        assert "extra" in tags(border_color="silver", set_type="promo"), "pal04/10 Mise"
        # A Secret Lair sticker sheet (sld/335-339), whose only tell is the type line.
        assert "extra" in tags(type_line="Stickers", set_type="box"), "sld/336"

    def test_preprocess_card_keeps_non_paper_cards(self) -> None:
        """A digital-only printing is IMPORTED: Scryfall serves it with default parameters.

        Measured against api.scryfall.com 2026-08-16: `q=!"A-Tyvar Kell"` answers khm/A-198 and
        `q=is:rebalanced` answers 216 cards, both from a bare `/cards/search`. Unlike the tokens,
        funny sets and memorabilia the other filters here stand in for, nothing hides a digital
        printing behind `include_extras` — so dropping the row was the one filter in
        `preprocess_card` that made an ordinary query disagree.
        """
        digital_card = create_test_card(
            games=["mtgo"],  # Not paper
        )

        result = preprocess_card(digital_card)
        assert len(result) == 1
        assert result[0]["card_name"] == "Test Card"

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

    def test_preprocess_card_keeps_same_faced_double_side_cards(self) -> None:
        """An "X // X" printing is a reversible card Scryfall serves from a bare search.

        `q=!"Magmatic Hellkite // Magmatic Hellkite"` answers 200 (tdm/380) with no flag, as do the
        Secret Lair and Ravnica-land reversibles. Its art_series cousins ARE extras, by layout.
        """
        rows = preprocess_card(create_test_card(name="Soulflayer // Soulflayer"))
        assert len(rows) == 1
        assert "extra" not in rows[0].get("card_is_tags", {})

    def test_preprocess_card_keeps_same_faced_cards_with_extra_whitespace(self) -> None:
        """The whitespace variant is kept too — same reason as the case above."""
        assert len(preprocess_card(create_test_card(name="Aberrant  //  Aberrant"))) == 1

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

    def test_preprocess_card_keeps_all_not_legal_cards(self) -> None:
        """Never-legal is not a hiding criterion: `q=!"Hold the Perimeter"` (cn2/6) answers 200."""
        rows = preprocess_card(
            create_test_card(
                legalities=dict.fromkeys(["standard", "modern", "legacy", "vintage", "commander"], "not_legal"),
            ),
        )
        assert len(rows) == 1
        assert "extra" not in rows[0].get("card_is_tags", {})

    def test_preprocess_card_keeps_cards_only_banned(self) -> None:
        """A banned-everywhere card is served too — same axis as never-legal above."""
        only_banned_card = create_test_card(
            legalities={
                "standard": "not_legal",
                "modern": "banned",
                "legacy": "banned",
                "vintage": "banned",
                "commander": "banned",
            },
        )

        assert len(preprocess_card(only_banned_card)) == 1

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

    def test_preprocess_card_keeps_funny_sets(self) -> None:
        """A funny set is ordinary: `q=e:ust` answers 249 with and without `include_extras`."""
        rows = preprocess_card(create_test_card(set_type="funny"))
        assert len(rows) == 1
        assert "extra" not in rows[0].get("card_is_tags", {})

    def test_preprocess_card_tags_card_type_as_extra(self) -> None:
        """`q=!"The Monarch"` (tmkc/31) is 404 bare and 200 with `include_extras=true`."""
        rows = preprocess_card(create_test_card(type_line="Card"))
        assert len(rows) == 1
        assert rows[0]["card_is_tags"]["extra"] is True

    def test_preprocess_card_tags_token_type_as_extra(self) -> None:
        """`q=!"Goblin Army"` (thob/4) is 404 bare and 200 with `include_extras=true`."""
        rows = preprocess_card(create_test_card(type_line="Token Creature — Goblin"))
        assert len(rows) == 1
        assert rows[0]["card_is_tags"]["extra"] is True

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

    def test_preprocess_card_lists_its_one_illustration(self) -> None:
        """A single-faced card shows one piece of art, and a card with none shows an empty list."""
        with_art = preprocess_card(create_test_card(illustration_id="44444444-4444-4444-4444-444444444444"))[0]
        assert with_art["illustration_ids"] == ["44444444-4444-4444-4444-444444444444"]

        without_art = preprocess_card(create_test_card())[0]
        assert without_art["illustration_id"] is None
        assert without_art["illustration_ids"] == []

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
        """A printed `*` is ZERO; anything else non-numeric is still absent.

        `tou<1` is 434 on api.scryfall.com against this engine's 273 and `tou=0` 432 against 272 --
        160 cards, every one of them `*`-statted -- because absent compares false against
        everything. Scryfall's own `tou:*` answers the same 432 as `tou=0`.
        """
        card = create_test_card(
            keywords=[],
            power="*",
            toughness="X",  # Not a star and not a number: still absent.
            prices={},
        )

        result = preprocess_card(card)

        assert len(result) == 1
        result = result[0]
        assert result["creature_power"] == 0
        assert result["creature_toughness"] is None
        # The printed strings are untouched -- they are what the card object serves.
        assert result["creature_power_text"] == "*"

    @pytest.mark.parametrize(
        ("printed", "expected"),
        [
            ("*", 0),
            ("1+*", 1),  # Allosaurus Rider, and api.scryfall.com answers pow=1 for it
            ("*+1", 1),  # Souls of the Lost, tou=1
            ("2+*", 2),  # Aysen Crusader, pow=2 and NOT pow=0
            ("7-*", 7),
            ("*\u00b2", 0),
            ("3", 3),
            ("-1", -1),
            ("1.5", 1),  # int(float(...)) truncates, as maybe_int has always done
            # `?` IS ZERO TOO, measured rather than reasoned from the star: Shellephant (ust/121)
            # prints it on both sides and api.scryfall.com answers `tou=0` 1, `tou>=0` 1,
            # `tou>0` 0. Read as absent it satisfied no comparison at all -- the whole of
            # `toughness<1` answering 433 against 434.
            ("?", 0),
            ("X", None),
            # `∞` stays absent: Infinity Elemental is `ulst`, which api.scryfall.com does not
            # answer for, so there is no measurement to follow.
            ("∞", None),
        ],
    )
    def test_preprocess_card_substitutes_zero_for_a_printed_star(self, printed: str, expected: int | None) -> None:
        """Every starred form the corpus prints, `?`, and the non-numbers that stay absent."""
        card = create_test_card(keywords=[], power=printed, prices={})

        result = preprocess_card(card)[0]

        assert result["creature_power"] == expected

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
                    "defense": "7",
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

    def test_battle_face_keeps_its_defense(self) -> None:
        """Scryfall prints a battle's defense on the FACE, and no column holds it.

        Every battle so far is a transform card, so a face field list without `defense` loses the
        number outright rather than degrading it.
        """
        merged = preprocess_card(self._battle_card())[0]
        assert merged["card_faces"][0]["defense"] == "7"
        # Absent on the face that has none, because Scryfall omits rather than nulls.
        assert "defense" not in merged["card_faces"][1]

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

    def test_illustration_ids_list_every_face_front_first(self) -> None:
        """The art a printing SHOWS is all of it, which is what art tags attach to.

        `illustration_id` stays the front's for display, so the back's art exists nowhere else on
        the row -- and `arttag:snow e:khm` is 75 on Scryfall against 73 for the front-only reading.
        """
        merged = preprocess_card(self._battle_card())[0]
        assert merged["illustration_ids"] == [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]

    def test_faces_sharing_the_cards_art_collapse_to_one_illustration(self) -> None:
        """A split card (Fire // Ice) has one illustration on the CARD and none on its faces.

        Both face rows therefore inherit the same id, and the merge must dedupe rather than list
        it twice -- there is one piece of art to attach tags to.
        """
        card = create_test_card(
            name="Flame // Frost",
            layout="split",
            illustration_id="33333333-3333-3333-3333-333333333333",
            card_faces=[
                {"name": "Flame", "type_line": "Instant", "mana_cost": "{R}", "oracle_text": "Deal 2 damage."},
                {"name": "Frost", "type_line": "Instant", "mana_cost": "{U}", "oracle_text": "Tap target creature."},
            ],
        )
        merged = preprocess_card(card)[0]
        assert merged["illustration_ids"] == ["33333333-3333-3333-3333-333333333333"]

    def test_a_face_with_no_art_at_all_contributes_nothing(self) -> None:
        """A missing illustration is absent from the list, not a null entry in it."""
        card = self._battle_card()
        del card["card_faces"][1]["illustration_id"]
        merged = preprocess_card(card)[0]
        assert merged["illustration_ids"] == ["11111111-1111-1111-1111-111111111111"]

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
        # `*` is ZERO, and `1+*` is 1 -- see maybe_stat_int. The point of this test is that the
        # numeric and _text columns come from the SAME face, which they still do.
        assert merged["creature_power"] == 0
        assert merged["creature_toughness"] == 1
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

    def test_an_all_token_faced_card_is_kept_and_tagged(self) -> None:
        """No face is filtered any more — the whole card is imported and tagged `is:extra`.

        The extras class is a property of the PRINTING, decided once from the card object, so a
        multi-face token yields one row like any other multi-face card rather than none.
        """
        card = create_test_card(
            name="Test A // Test B",
            card_faces=[
                {"name": "Test A", "type_line": "Token Creature — Goblin"},
                {"name": "Test B", "type_line": "Token Creature — Elf"},
            ],
        )
        rows = preprocess_card(card)
        assert len(rows) == 1
        assert rows[0]["card_is_tags"]["extra"] is True


class TestMultiFaceRawBlob:
    """`raw_card_blob` on a merged row is the card Scryfall sent, not a face promoted to look like one.

    Every searchable field is merged onto the row's own columns, so the blob has no derivation left
    to do — and keeping it verbatim is what makes it answerable. A card object cannot be rebuilt
    from a face: `card_faces` is gone, `name` and `type_line` are the front's, and which fields a
    real card carries at top level varies by layout. These tests pin both layouts that differ.
    """

    # The importer lifts the combined name before splitting faces; it is the only key the blob
    # gains, and the only one a reader has to strip to recover Scryfall's object exactly.
    IMPORTER_ADDED: ClassVar[set[str]] = {"card_name"}

    @staticmethod
    def _transform_card() -> dict:
        """A transform card: images and text live per face, with nothing at top level."""
        card = create_test_card(
            name="Test Delver // Test Aberration",
            object="card",
            layout="transform",
            type_line="Creature — Human Wizard // Creature — Human Insect",
            colors=["U"],
            color_identity=["U"],
            card_faces=[
                {
                    "object": "card_face",
                    "name": "Test Delver",
                    "mana_cost": "{U}",
                    "type_line": "Creature — Human Wizard",
                    "oracle_text": "Look at the top card of your library.",
                    "power": "1",
                    "toughness": "1",
                    "colors": ["U"],
                    "image_uris": {"normal": "https://cards.test/front.jpg"},
                },
                {
                    "object": "card_face",
                    "name": "Test Aberration",
                    "mana_cost": "",
                    "type_line": "Creature — Human Insect",
                    "oracle_text": "Flying",
                    "power": "3",
                    "toughness": "2",
                    "colors": ["U"],
                    "image_uris": {"normal": "https://cards.test/back.jpg"},
                },
            ],
        )
        card.pop("image_uris", None)
        card.pop("mana_cost", None)
        return card

    @staticmethod
    def _split_card() -> dict:
        """A split card: one physical face, so `mana_cost` and `image_uris` stay at top level."""
        return create_test_card(
            name="Test Fire // Test Ice",
            object="card",
            layout="split",
            type_line="Instant // Instant",
            mana_cost="{1}{R} // {1}{U}",
            colors=["R", "U"],
            color_identity=["R", "U"],
            image_uris={"normal": "https://cards.test/split.jpg"},
            card_faces=[
                {
                    "object": "card_face",
                    "name": "Test Fire",
                    "mana_cost": "{1}{R}",
                    "type_line": "Instant",
                    "oracle_text": "Deals 2.",
                },
                {
                    "object": "card_face",
                    "name": "Test Ice",
                    "mana_cost": "{1}{U}",
                    "type_line": "Instant",
                    "oracle_text": "Tap it.",
                },
            ],
        )

    @pytest.mark.parametrize("builder", ["_transform_card", "_split_card"], ids=["transform", "split"])
    def test_the_blob_is_the_card_it_was_given(self, builder) -> None:
        """Whatever the layout, the blob differs from the input by the lifted name and nothing else."""
        card = getattr(self, builder)()
        original = copy.deepcopy(card)
        blob = preprocess_card(card)[0]["raw_card_blob"]

        assert set(blob) - set(original) == self.IMPORTER_ADDED
        assert set(original) - set(blob) == set()
        assert {key: value for key, value in blob.items() if key not in self.IMPORTER_ADDED} == original

    def test_a_transform_card_keeps_its_images_only_on_the_faces(self) -> None:
        """The reason every blob image read coalesces to `card_faces->0` (scripts/prefer_weights.py)."""
        blob = preprocess_card(self._transform_card())[0]["raw_card_blob"]
        assert "image_uris" not in blob
        assert blob["card_faces"][0]["image_uris"]["normal"] == "https://cards.test/front.jpg"

    def test_a_split_card_keeps_its_top_level_image(self) -> None:
        """Which is why stripping the promoted fields by rule could not have worked: layout decides."""
        blob = preprocess_card(self._split_card())[0]["raw_card_blob"]
        assert blob["image_uris"]["normal"] == "https://cards.test/split.jpg"
        assert blob["mana_cost"] == "{1}{R} // {1}{U}"

    def test_the_faces_round_trip_untouched(self) -> None:
        card = self._transform_card()
        faces = copy.deepcopy(card["card_faces"])
        assert preprocess_card(card)[0]["raw_card_blob"]["card_faces"] == faces

    def test_the_blob_is_not_a_face(self) -> None:
        blob = preprocess_card(self._transform_card())[0]["raw_card_blob"]
        assert blob["object"] == "card"
        assert blob["name"] == "Test Delver // Test Aberration"
        assert blob["type_line"] == "Creature — Human Wizard // Creature — Human Insect"

    def test_the_searchable_row_still_carries_the_merged_faces(self) -> None:
        """The blob going verbatim must not have cost the merge — that is what #400 was about."""
        row = preprocess_card(self._transform_card())[0]
        assert row["card_name"] == "Test Delver // Test Aberration"
        assert "Flying" in row["oracle_text"]
        assert "Look at the top card" in row["oracle_text"]
        assert row["creature_power"] == 1


class TestEngineCardObjects:
    """What the ENGINE needs to answer a card-shaped question, without reading raw_card_blob.

    The engine serves searches and Postgres is the fallback for when it errors, so a field that
    lives only in the blob is a field /cards/* cannot answer on the primary path. These pin the
    two columns that close that gap.
    """

    @staticmethod
    def _two_faced(
        artist: str | None = None,
        face_artists: tuple[str, str] = ("Front Artist", "Back Artist"),
    ) -> dict:
        """A transform card whose two faces disagree on every per-face field that matters.

        `artist` is the CARD-level credit Scryfall sends beside the faces — joined with " & " when
        the two faces have different artists, and the single name when they share one.
        """
        return create_test_card(
            name="Front Test // Back Test",
            artist=artist,
            layout="transform",
            lang="en",
            set_type="expansion",
            games=["paper", "mtgo"],
            finishes=["nonfoil", "foil"],
            arena_id=12345,
            card_faces=[
                {
                    "name": "Front Test",
                    "type_line": "Creature — Human",
                    "mana_cost": "{1}{W}",
                    "colors": ["W"],
                    "power": "2",
                    "toughness": "2",
                    "oracle_text": "Front text.",
                    "flavor_text": "Front flavor.",
                    "artist": face_artists[0],
                    "illustration_id": "11111111-1111-1111-1111-111111111111",
                },
                {
                    "name": "Back Test",
                    "type_line": "Creature — Werewolf",
                    "mana_cost": "",
                    "colors": ["R"],
                    "power": "3",
                    "toughness": "3",
                    "oracle_text": "Back text.",
                    "artist": face_artists[1],
                    "illustration_id": "22222222-2222-2222-2222-222222222222",
                },
            ],
        )

    def test_faces_are_stored_structurally(self) -> None:
        """Each face keeps its own fields, front first, so the engine can read a face."""
        faces = preprocess_card(self._two_faced())[0]["card_faces"]
        assert [face["name"] for face in faces] == ["Front Test", "Back Test"]
        assert faces[0]["illustration_id"] == "11111111-1111-1111-1111-111111111111"
        assert faces[1]["illustration_id"] == "22222222-2222-2222-2222-222222222222"
        assert faces[0]["artist"] == "Front Artist"
        assert faces[1]["artist"] == "Back Artist"

    def test_a_cards_artist_is_scryfalls_and_never_a_faces(self) -> None:
        """The card's joined credit survives the face overlay, and a shared one is not doubled.

        `card_artist` is read off the merged dict, and the face overlay had already put face 0's
        `artist` there — so a card drawn by two people was credited to the front one alone. Taking
        Scryfall's own card-level string rather than joining the faces is what makes the shared
        case right for free: two faces by one artist keep the single name.
        """
        two = preprocess_card(self._two_faced(artist="Front Artist & Back Artist"))[0]
        assert two["card_artist"] == "Front Artist & Back Artist"
        assert [face["artist"] for face in two["card_faces"]] == ["Front Artist", "Back Artist"]

        shared = preprocess_card(
            self._two_faced(artist="Solo Artist", face_artists=("Solo Artist", "Solo Artist")),
        )[0]
        assert shared["card_artist"] == "Solo Artist"

    def test_back_face_stats_survive_the_merge(self) -> None:
        """Both faces' stats stay reachable, retiring the merge's documented residual.

        _merge_processed_faces keeps only the FRONT's stat group, while Scryfall matches either
        face. The per-face records carry both, so the back's 3/3 is searchable again.
        """
        row = preprocess_card(self._two_faced())[0]
        assert row["creature_power_text"] == "2"
        assert [face["power"] for face in row["card_faces"]] == ["2", "3"]
        assert [face["toughness"] for face in row["card_faces"]] == ["2", "3"]

    def test_absent_face_keys_stay_absent(self) -> None:
        """A face without flavor_text stores no flavor_text key.

        Scryfall omits it rather than sending null, and a reconstructed face has to agree
        key-for-key.
        """
        faces = preprocess_card(self._two_faced())[0]["card_faces"]
        assert faces[0]["flavor_text"] == "Front flavor."
        assert "flavor_text" not in faces[1]

    def test_single_faced_cards_have_no_face_records(self) -> None:
        """One face is not a face list; the column stays absent so the engine stores nothing."""
        assert "card_faces" not in preprocess_card(create_test_card(name="Solo Test"))[0]

    def test_compat_blob_carries_what_no_column_holds(self) -> None:
        """The fields /cards/* needs and nothing else stores."""
        blob = preprocess_card(self._two_faced())[0]["card_compat_blob"]
        assert blob["lang"] == "en"
        assert blob["set_type"] == "expansion"
        assert blob["games"] == ["paper", "mtgo"]
        assert blob["finishes"] == ["nonfoil", "foil"]
        assert blob["arena_id"] == 12345

    def test_compat_blob_excludes_what_columns_already_hold(self) -> None:
        """Redundancy is the whole cost of raw_card_blob; the residue must not repeat it."""
        blob = preprocess_card(self._two_faced())[0]["card_compat_blob"]
        for stored in ("name", "type_line", "oracle_text", "colors", "legalities", "set"):
            assert stored not in blob, f"{stored} has a column of its own"

    def test_compat_blob_excludes_derivable_uris(self) -> None:
        """Every *_uri is a pure function of the card's id, set or oracle id."""
        blob = preprocess_card(self._two_faced())[0]["card_compat_blob"]
        for derived in ("uri", "scryfall_uri", "image_uris", "rulings_uri", "prints_search_uri"):
            assert derived not in blob, f"{derived} is derivable"

    def test_compat_blob_is_much_smaller_than_the_raw_blob(self) -> None:
        """The reason this exists: the blob is overwhelmingly redundant with columns we have."""
        row = preprocess_card(self._two_faced())[0]
        assert len(json.dumps(row["card_compat_blob"])) < len(json.dumps(row["raw_card_blob"])) / 2

    def test_single_faced_cards_still_get_a_compat_blob(self) -> None:
        """The residue is card-level, so it is not a multi-face concern."""
        assert "card_compat_blob" in preprocess_card(create_test_card(name="Solo Test"))[0]


class TestMultilingualIngest:
    """The printed-language columns, the language column, and their DFC discipline."""

    def test_a_japanese_printing_carries_its_printed_triple(self) -> None:
        card = create_test_card(
            name="Shock",
            lang="ja",
            printed_name="ショック",
            printed_type_line="インスタント",
            printed_text="ショックはクリーチャー1体かプレインズウォーカー1体かプレイヤー1人を対象とする。",
        )
        (row,) = preprocess_card(card)
        assert row["card_lang"] == "ja"
        assert row["printed_name"] == "ショック"
        assert row["printed_type_line"] == "インスタント"
        assert row["printed_text"].startswith("ショック")
        assert row["printed_name_folded"] == "ショック"
        # The triple has columns of its own now, so the compat residue must not double-store it.
        assert "printed_name" not in row["card_compat_blob"]
        # lang deliberately STAYS in the blob: the card object reads it from there.
        assert row["card_compat_blob"]["lang"] == "ja"

    def test_an_english_printing_has_no_printed_columns(self) -> None:
        (row,) = preprocess_card(create_test_card(lang="en"))
        assert row["card_lang"] == "en"
        # Explicit None (not absent) so an upsert overrides stale values.
        assert row["printed_name"] is None
        assert row["printed_type_line"] is None
        assert row["printed_text"] is None
        assert row["printed_name_folded"] is None

    def test_dfc_printed_columns_are_the_cards_not_a_faces(self) -> None:
        # A Spanish prepare-layout printing: the FRONT face localizes name and type line and
        # nothing else, the back face nothing at all, and the card has no top-level triple.
        card = create_test_card(
            name="Prepare // Fight",
            lang="es",
            card_faces=[
                {
                    "name": "Prepare",
                    "printed_name": "Preparación",
                    "type_line": "Instant",
                    "printed_type_line": "Instantáneo",
                    "oracle_text": "Untap target creature.",
                },
                {
                    "name": "Fight",
                    "type_line": "Sorcery",
                    "oracle_text": "Target creature you control fights target creature you don't control.",
                },
            ],
        )
        (row,) = preprocess_card(card)
        # The columns are the card-level triple — absent here — not the front face's overlay.
        assert row["printed_name"] is None
        assert row["printed_type_line"] is None
        # The per-face halves ride the face records, absence exact per face.
        front, back = row["card_faces"]
        assert front["printed_name"] == "Preparación"
        assert front["printed_type_line"] == "Instantáneo"
        assert "printed_text" not in front
        assert "printed_name" not in back
        # The folded FULL name joins printed where present and falls back to English where not.
        assert row["printed_name_folded"] == "preparacion // fight"

    def test_face_records_keep_scryfall_key_order(self) -> None:
        card = create_test_card(
            name="A // B",
            lang="es",
            card_faces=[
                {
                    "name": "A",
                    "printed_name": "A-es",
                    "mana_cost": "{R}",
                    "type_line": "Instant",
                    "printed_type_line": "Instantáneo",
                    "oracle_text": "x",
                    "printed_text": "x-es",
                },
                {"name": "B", "type_line": "Sorcery", "oracle_text": "y"},
            ],
        )
        (row,) = preprocess_card(card)
        front = row["card_faces"][0]
        keys = list(front)
        assert keys.index("printed_name") == keys.index("name") + 1
        assert keys.index("printed_type_line") == keys.index("type_line") + 1
        assert keys.index("printed_text") == keys.index("oracle_text") + 1
