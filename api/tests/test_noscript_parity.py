"""Parity tests for the server-side (no-JS) card renderer against the shared fixture.

The fixture is the contract both renderers must satisfy: this test checks
create_card_html(), and the Jest suite in api/static/app.test.js checks
createCardHTML() against the same file. Regenerate it with
scripts/generate_card_html_fixture.py after an intentional rendering change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api.noscript_helpers import CARD_IMAGE_SIZES, CARD_IMAGES_SPEC, create_card_html

_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
CARD_HTML_CASES = json.loads((_STATIC_DIR / "fixtures" / "card_html_cases.json").read_text(encoding="utf-8"))


def normalize_card_html(html: str) -> str:
    """Reduce card HTML to a render-equivalent form for parity comparison.

    Keep in sync with normalizeCardHtml in app.test.js. Strips the loading-hint
    attributes (fetchpriority/loading logic intentionally differs between the JS and
    no-JS paths) and inter-tag whitespace (template indentation differs).
    """
    html = html.replace(' fetchpriority="high"', "").replace(' loading="lazy"', "")
    return re.sub(r">\s+<", "><", html).strip()


@pytest.mark.parametrize(
    argnames=["card", "index", "expected_html"],
    argvalues=[(case["card"], case["index"], case["html"]) for case in CARD_HTML_CASES],
    ids=[case["id"] for case in CARD_HTML_CASES],
)
def test_create_card_html_matches_shared_fixture(card: dict, index: int, expected_html: str) -> None:
    """The Python renderer must match the shared JS/Python card HTML contract."""
    assert normalize_card_html(create_card_html(card, index)) == expected_html


def test_sizes_layout_matches_css_grid() -> None:
    """The layout table in card_images.json must mirror the grid in styles.css.

    Each sizes max-width must sit one below a grid min-width threshold (< vs <=), and
    each slot-width formula's leading vw term must match that range's column count.
    """
    css = (_STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    grid_media = re.findall(
        r"@media \(min-width: (\d+)px\) \{\s*\.results-container \{\s*grid-template-columns: repeat\((\d+), 1fr\);",
        css,
    )
    # (upper-bound threshold, columns in the range below it); base range is 1 column,
    # the widest range has no upper bound (None) and the last media block's columns.
    thresholds = [int(px) for px, _ in grid_media]
    columns = [1] + [int(cols) for _, cols in grid_media]
    expected_ranges = list(zip([*thresholds, None], columns, strict=True))

    layout = CARD_IMAGES_SPEC["layout"]
    assert len(layout) == len(expected_ranges)
    for (condition, formula), (threshold, cols) in zip(layout, expected_ranges, strict=True):
        if threshold is None:
            assert condition is None
        else:
            assert condition == f"(max-width: {threshold - 1}px)"
        vw_share = float(formula.split("vw")[0])
        assert vw_share == round(100 / cols, 2)


def test_sizes_clause_structure() -> None:
    """The generated sizes string is the full density x layout cross product, densest first."""
    spec = CARD_IMAGES_SPEC
    clauses = CARD_IMAGE_SIZES.split(", ")
    assert len(clauses) == len(spec["density"]) * len(spec["layout"])
    assert clauses[0] == "(min-resolution: 2.9dppx) and (max-width: 409px) calc((100vw - 3.6em) * 0.5)"
    # last clause is the bare default — no media condition, no budget multiplier
    assert clauses[-1] == "calc(20vw - 2em - 12px)"
    # first-match-wins: density conditions must be ordered densest to sparsest
    resolutions = [float(cond.split(" ")[1].removesuffix("dppx)")) for cond, _ in spec["density"] if cond]
    assert resolutions == sorted(resolutions, reverse=True)


def test_image_ladder_structure() -> None:
    """The ladder is ascending, topped by the full-resolution width, with a valid grid default."""
    ladder = CARD_IMAGES_SPEC["ladder"]
    assert all(isinstance(width, int) for width in ladder)
    assert ladder == sorted(set(ladder))
    assert ladder[-1] == CARD_IMAGES_SPEC["full"]["width"]
    assert CARD_IMAGES_SPEC["grid_src_default"] in ladder


def test_css_card_dimensions_match_spec() -> None:
    """styles.css can't read JSON, so its card dimension literals are enforced here.

    Guards the aspect-ratio and the modal max-height/max-width formulas against
    drifting from the real image dimensions (this test caught a stale 1041).
    """
    css = (_STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    width = CARD_IMAGES_SPEC["full"]["width"]
    height = CARD_IMAGES_SPEC["full"]["height"]
    assert f"aspect-ratio: {width} / {height};" in css
    assert f"max-height: min(100vh, {height}px);" in css
    assert f"max-width: min(100%, {width}px, calc(min(100vh, {height}px) * {width} / {height}));" in css
