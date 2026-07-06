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

from api.noscript_helpers import CARD_GRID_SIZES_SPEC, CARD_IMAGE_SIZES, create_card_html

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
    """The layout table in card_grid_sizes.json must mirror the grid in styles.css.

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

    layout = CARD_GRID_SIZES_SPEC["layout"]
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
    spec = CARD_GRID_SIZES_SPEC
    clauses = CARD_IMAGE_SIZES.split(", ")
    assert len(clauses) == len(spec["density"]) * len(spec["layout"])
    assert clauses[0] == "(min-resolution: 2.9dppx) and (max-width: 409px) calc((100vw - 3.6em) * 0.5)"
    # last clause is the bare default — no media condition, no budget multiplier
    assert clauses[-1] == "calc(20vw - 2em - 12px)"
    # first-match-wins: density conditions must be ordered densest to sparsest
    resolutions = [float(cond.split(" ")[1].removesuffix("dppx)")) for cond, _ in spec["density"] if cond]
    assert resolutions == sorted(resolutions, reverse=True)
