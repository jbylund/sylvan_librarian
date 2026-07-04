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

from api.noscript_helpers import create_card_html

CARD_HTML_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "static" / "fixtures" / "card_html_cases.json").read_text(encoding="utf-8")
)


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
