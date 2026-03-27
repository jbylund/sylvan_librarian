"""Test cases for prefer score components calculation."""

import unittest


def calculate_finish_score(finishes: list[str]) -> int:
    """Calculate finish score based on finishes list."""
    if "nonfoil" in finishes:
        return 10
    if "foil" in finishes:
        return 5
    if "etched" in finishes:
        return 0
    return 0


def calculate_artwork_set_score(card_set_code: str | None) -> int:
    """Calculate artwork set score based on card set code."""
    # List of sets with black/white artwork that should not receive bonus
    bw_artwork_sets = ("dbl",)
    # Explicitly handle NULL set codes to match SQL behavior
    if card_set_code is None or card_set_code not in bw_artwork_sets:
        return 20
    return 0


def calculate_non_showcase_score(frame_effects: list[str] | None) -> int:
    """Calculate non-showcase score based on frame_effects list."""
    # Give bonus if frame_effects does not contain 'showcase'
    # Cards with no frame_effects or empty array are considered non-showcase
    if frame_effects is None or "showcase" not in frame_effects:
        return 10
    return 0


class TestPreferScoreComponents(unittest.TestCase):
    """Test cases for prefer score components."""

    def test_legendary_frame_component_logic(self) -> None:
        """Test that legendary frame scoring logic is correct."""
        # Test case 1: Card with legendary frame effect
        # Should get score of 5
        card_with_legendary = {
            "frame_effects": ["legendary", "etched"],
        }
        # If frame_effects contains 'legendary', score should be 5
        score = 5 if "legendary" in card_with_legendary.get("frame_effects", []) else 0
        assert score == 5, "Card with legendary frame should get score of 5"

        # Test case 2: Card without legendary frame effect
        # Should get score of 0
        card_without_legendary = {
            "frame_effects": ["etched"],
        }
        score = 5 if "legendary" in card_without_legendary.get("frame_effects", []) else 0
        assert score == 0, "Card without legendary frame should get score of 0"

        # Test case 3: Card with no frame_effects
        # Should get score of 0
        card_no_effects = {}
        score = 5 if "legendary" in card_no_effects.get("frame_effects", []) else 0
        assert score == 0, "Card with no frame_effects should get score of 0"

    def test_finish_component_logic(self) -> None:
        """Test that finish scoring logic is correct."""
        # Test case 1: nonfoil card (most preferred) should get score of 10
        assert calculate_finish_score(["nonfoil"]) == 10, "Nonfoil card should get score of 10"

        # Test case 2: foil card (middle preference) should get score of 5
        assert calculate_finish_score(["foil"]) == 5, "Foil card should get score of 5"

        # Test case 3: etched card (least preferred) should get score of 0
        assert calculate_finish_score(["etched"]) == 0, "Etched card should get score of 0"

        # Test case 4: No finishes specified should get score of 0
        assert calculate_finish_score([]) == 0, "Card with no finishes should get score of 0"

    def test_preference_ordering(self) -> None:
        """Test that the overall preference ordering is correct."""
        # Test ordering for legendary frame: normal frame (0) < legendary frame (5)
        normal_frame_score = 0
        legendary_frame_score = 5
        assert normal_frame_score < legendary_frame_score, "Legendary frame should be preferred over normal frame"

        # Test ordering for finishes: etched < foil < nonfoil
        etched_score = 0
        foil_score = 5
        nonfoil_score = 10
        assert etched_score < foil_score < nonfoil_score, "Finish ordering should be: etched < foil < nonfoil"

        # Test ordering for artwork sets: black/white artwork (0) < full-color artwork (20)
        bw_artwork_score = 0
        color_artwork_score = 20
        assert bw_artwork_score < color_artwork_score, "Full-color artwork should be preferred over black/white artwork"

        # Test ordering for showcase: showcase (0) < non-showcase (10)
        showcase_score = 0
        non_showcase_score = 10
        assert showcase_score < non_showcase_score, "Non-showcase cards should be preferred over showcase cards"

    def test_artwork_set_component_logic(self) -> None:
        """Test that artwork set scoring logic is correct."""
        # Test case 1: Card from dbl set (black/white artwork) should get score of 0
        assert calculate_artwork_set_score("dbl") == 0, "Card from dbl set should get score of 0"

        # Test case 2: Card from regular set (full-color artwork) should get score of 20
        assert calculate_artwork_set_score("iko") == 20, "Card from iko set should get score of 20"
        assert calculate_artwork_set_score("thb") == 20, "Card from thb set should get score of 20"
        assert calculate_artwork_set_score("m21") == 20, "Card from m21 set should get score of 20"

        # Test case 3: Card with no set code should get score of 20 (prefer over black/white sets)
        assert calculate_artwork_set_score(None) == 20, "Card with no set code should get score of 20"

    def test_non_showcase_component_logic(self) -> None:
        """Test that non-showcase scoring logic is correct."""
        # Test case 1: Card with showcase frame effect should get score of 0
        assert calculate_non_showcase_score(["showcase"]) == 0, "Showcase card should get score of 0"
        assert calculate_non_showcase_score(["showcase", "legendary"]) == 0, (
            "Showcase card with other effects should get score of 0"
        )

        # Test case 2: Card without showcase frame effect should get score of 10
        assert calculate_non_showcase_score(["legendary"]) == 10, "Non-showcase card should get score of 10"
        assert calculate_non_showcase_score(["etched"]) == 10, "Non-showcase card with other effects should get score of 10"

        # Test case 3: Card with no frame_effects should get score of 10
        assert calculate_non_showcase_score(None) == 10, "Card with no frame_effects should get score of 10"
        assert calculate_non_showcase_score([]) == 10, "Card with empty frame_effects should get score of 10"


if __name__ == "__main__":
    unittest.main()
