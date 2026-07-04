"""Tests for the placeholder codebook assignment logic."""

import io
import json

import pytest

np = pytest.importorskip("numpy")
PIL_Image = pytest.importorskip("PIL.Image")

from scripts.placeholder_assignment import DEFAULT_CODEBOOK_PATH, PlaceholderCodebook  # noqa: E402


@pytest.fixture(scope="module")
def codebook() -> PlaceholderCodebook:
    return PlaceholderCodebook()


@pytest.fixture(scope="module")
def border_by_id() -> dict[int, str]:
    raw = json.loads(DEFAULT_CODEBOOK_PATH.read_text())
    return {cluster["id"]: cluster["border"] for cluster in raw["clusters"]}


def solid_card(rgb: tuple[int, int, int], border_rgb: tuple[int, int, int] | None = None) -> "PIL_Image.Image":
    """A 280x391 card-shaped image with an optional distinct border ring."""
    arr = np.zeros((391, 280, 3), dtype=np.uint8)
    arr[:, :] = rgb
    if border_rgb is not None:
        ring = 12  # ~1 ring pixel after the 28x39 downscale
        arr[:ring, :] = border_rgb
        arr[-ring:, :] = border_rgb
        arr[:, :ring] = border_rgb
        arr[:, -ring:] = border_rgb
    return PIL_Image.fromarray(arr)


class TestBorderClassification:
    def test_white_border(self, codebook: PlaceholderCodebook) -> None:
        image = solid_card((120, 120, 120), border_rgb=(250, 250, 250))
        assert codebook.border_class(codebook.thumb_vector(image)) == "white"

    def test_black_border(self, codebook: PlaceholderCodebook) -> None:
        image = solid_card((120, 120, 120), border_rgb=(10, 10, 10))
        assert codebook.border_class(codebook.thumb_vector(image)) == "black"

    def test_borderless_art_ring(self, codebook: PlaceholderCodebook) -> None:
        image = solid_card((120, 120, 120))
        assert codebook.border_class(codebook.thumb_vector(image)) == "art"


class TestAssignment:
    """Assignment must be gated to centroids of the image's border class."""

    @pytest.mark.parametrize(
        argnames=["border_rgb", "expected_border"],
        argvalues=[
            ((250, 250, 250), "white"),
            ((10, 10, 10), "black"),
            (None, "art"),
        ],
        ids=["white-border", "black-border", "borderless"],
    )
    def test_border_gating(
        self,
        codebook: PlaceholderCodebook,
        border_by_id: dict[int, str],
        border_rgb: tuple[int, int, int] | None,
        expected_border: str,
    ) -> None:
        image = solid_card((170, 40, 30), border_rgb=border_rgb)
        cluster_id = codebook.assign_image(image)
        assert border_by_id[cluster_id] == expected_border

    def test_assign_bytes_matches_assign_image(self, codebook: PlaceholderCodebook) -> None:
        image = solid_card((30, 90, 40), border_rgb=(10, 10, 10))
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", lossless=True)
        assert codebook.assign_bytes(buffer.getvalue()) == codebook.assign_image(image)

    def test_every_cluster_id_has_a_css_class(self, codebook: PlaceholderCodebook) -> None:
        """The committed CSS must define ph-<id> for every codebook cluster and all fallbacks."""
        css = (DEFAULT_CODEBOOK_PATH.parents[1] / "api" / "static" / "placeholders-v1.css").read_text()
        for cluster_id in codebook.cluster_ids:
            assert f".ph-{cluster_id}" in css
        for color_group in ("w", "u", "b", "r", "g", "gold", "artifact", "land"):
            assert f".ph-fb-{color_group}" in css
