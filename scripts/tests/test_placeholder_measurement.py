"""Tests for placeholder bucketing and color measurement."""

import io
import json

import pytest

np = pytest.importorskip("numpy")
PIL_Image = pytest.importorskip("PIL.Image")

from scripts.placeholder_measurement import (  # noqa: E402
    DEFAULT_TEMPLATES_PATH,
    PLACEHOLDER_VALUE_RE,
    PlaceholderTemplates,
    bucket_for,
)

bucket_testcases = {
    "modern_frame_red": {
        "meta": {"frame": "2003", "border_color": "black", "colors": ["R"], "type_line": "Instant"},
        "expected": "modern-r",
    },
    "m15_frame_dual_land": {
        "meta": {"frame": "2015", "border_color": "black", "colors": [], "type_line": "Land — Forest Island"},
        "expected": "m15-land",
    },
    "retro_frame_reprint_is_old": {
        # released recently but printed with the 1997 frame (The List, DMR retro)
        "meta": {"frame": "1997", "border_color": "black", "colors": ["G"], "type_line": "Sorcery"},
        "expected": "old-g",
    },
    "white_border_any_frame": {
        "meta": {"frame": "2003", "border_color": "white", "colors": ["U"], "type_line": "Instant"},
        "expected": "old-wb-u",
    },
    "borderless_is_fullart": {
        "meta": {"frame": "2015", "border_color": "borderless", "colors": ["W"], "type_line": "Enchantment"},
        "expected": "fullart-w",
    },
    "full_art_flag_is_fullart": {
        "meta": {"frame": "2015", "border_color": "black", "full_art": True, "colors": [], "type_line": "Basic Land"},
        "expected": "fullart-land",
    },
    "artifact_land_is_land": {
        "meta": {"frame": "2003", "border_color": "black", "colors": [], "type_line": "Artifact Land"},
        "expected": "modern-land",
    },
    "gold_two_colors": {
        "meta": {"frame": "2015", "border_color": "black", "colors": ["R", "W"], "type_line": "Instant"},
        "expected": "m15-gold",
    },
    "colorless_nonartifact_uses_artifact": {
        "meta": {"frame": "2015", "border_color": "black", "colors": [], "type_line": "Creature — Eldrazi"},
        "expected": "m15-artifact",
    },
    "missing_frame_defaults_to_m15": {
        "meta": {"colors": ["B"], "type_line": "Creature — Zombie"},
        "expected": "m15-b",
    },
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(bucket_testcases.values()))),
    argvalues=[[v for k, v in sorted(bucket_testcases[name].items())] for name in sorted(bucket_testcases)],
    ids=sorted(bucket_testcases),
)
def test_bucket_for(expected: str, meta: dict) -> None:
    assert bucket_for(meta) == expected


@pytest.fixture(scope="module")
def templates() -> PlaceholderTemplates:
    return PlaceholderTemplates()


def card_image(left_rgb: tuple, right_rgb: tuple | None = None, art_rgb: tuple = (40, 60, 50)) -> "PIL_Image.Image":
    """A 280x391 card: frame filled left/right (right defaults to left), art rect distinct."""
    right_rgb = right_rgb or left_rgb
    arr = np.zeros((391, 280, 3), dtype=np.uint8)
    arr[:, :140] = left_rgb
    arr[:, 140:] = right_rgb
    # modern art rect, roughly
    arr[43:213, 21:259] = art_rgb
    return PIL_Image.fromarray(arr)


class TestMeasurement:
    def test_value_shape(self, templates: PlaceholderTemplates) -> None:
        value = templates.measure_image(card_image((170, 40, 30)), "modern-r")
        assert value is not None
        assert PLACEHOLDER_VALUE_RE.match(value)
        assert value.startswith("modern-r ")

    def test_mono_frame_measures_equal_sides(self, templates: PlaceholderTemplates) -> None:
        value = templates.measure_image(card_image((170, 40, 30)), "modern-r")
        _, frame_l, frame_r, _ = value.split(" ")
        left = np.array([int(frame_l[i : i + 2], 16) for i in (0, 2, 4)])
        right = np.array([int(frame_r[i : i + 2], 16) for i in (0, 2, 4)])
        assert np.abs(left - right).max() <= 12

    def test_dual_frame_measures_different_sides(self, templates: PlaceholderTemplates) -> None:
        value = templates.measure_image(card_image((60, 130, 60), (60, 80, 160)), "modern-land")
        _, frame_l, frame_r, _ = value.split(" ")
        # left side greener, right side bluer
        assert int(frame_l[2:4], 16) > int(frame_l[4:6], 16)
        assert int(frame_r[4:6], 16) > int(frame_r[2:4], 16)

    def test_art_color_reflects_art_rect(self, templates: PlaceholderTemplates) -> None:
        value = templates.measure_image(card_image((170, 40, 30), art_rgb=(20, 40, 120)), "modern-r")
        art = value.split(" ")[3]
        assert int(art[4:6], 16) > int(art[0:2], 16)  # blue art dominates red channel

    def test_unknown_bucket_returns_none(self, templates: PlaceholderTemplates) -> None:
        assert templates.measure_image(card_image((10, 10, 10)), "does-not-exist") is None

    def test_measure_bytes_matches_measure_image(self, templates: PlaceholderTemplates) -> None:
        image = card_image((30, 90, 40))
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", lossless=True)
        meta = {"frame": "2003", "border_color": "black", "colors": ["G"], "type_line": "Sorcery"}
        assert templates.measure_bytes(buffer.getvalue(), meta) == templates.measure_image(image, "modern-g")


class TestArtifactCssConsistency:
    def test_every_bucket_has_a_css_class(self, templates: PlaceholderTemplates) -> None:
        """The committed CSS must define ph-<bucket> for every template and all fallbacks."""
        css = (DEFAULT_TEMPLATES_PATH.parents[1] / "api" / "static" / "placeholders-v1.css").read_text()
        for bucket in templates.templates:
            assert f".ph-{bucket}" in css
        for group in ("w", "u", "b", "r", "g", "gold", "artifact", "land"):
            assert f".ph-fb-{group}" in css

    def test_bucket_names_match_bucket_for_vocabulary(self, templates: PlaceholderTemplates) -> None:
        """Every committed bucket must be producible by bucket_for's gen/group vocabularies."""
        raw = json.loads(DEFAULT_TEMPLATES_PATH.read_text())
        gens = set(raw["art_rects"])
        groups = {"w", "u", "b", "r", "g", "gold", "artifact", "land"}
        for bucket in templates.templates:
            gen, group = bucket.rsplit("-", 1)
            assert gen in gens
            assert group in groups
