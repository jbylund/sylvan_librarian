"""Regenerate the shared card-HTML parity fixture from the Python renderer.

The fixture (api/static/fixtures/card_html_cases.json) is the contract both renderers
must satisfy: test_noscript_parity.py checks create_card_html() against it, and the
Jest suite (app.test.js) checks createCardHTML() against it. Rerun this script after
an intentional rendering change, then confirm the Jest side still passes.

Usage: python scripts/generate_card_html_fixture.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.noscript_helpers import create_card_html

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "api" / "static" / "fixtures" / "card_html_cases.json"

# Oracle text engineered so the 200-character card-text cutoff lands inside the {W}
# symbol (exercising the back-up-before-unclosed-brace logic on both sides) and the
# converted text exceeds the 300-character alt-text cutoff.
_LONG_ORACLE_PREFIX = (
    "Flying, vigilance\n"
    "Whenever Sunlit Archivist attacks, look at the top five cards of your library. "
    "You may reveal a Plains card from among them and put it into your hand. "
    "Put the others on the bottoms"
)
_LONG_ORACLE_TEXT = (
    _LONG_ORACLE_PREFIX
    + "{W} of your library in a random order. If you revealed a card this way, you gain 3 life and scry 2 at the beginning of the next end step."
)

CASES = [
    {
        "id": "mana-cost-instant",
        "index": 0,
        "card": {
            "name": "Lightning Bolt",
            "mana_cost": "{R}",
            "type_line": "Instant",
            "oracle_text": "Lightning Bolt deals 3 damage to any target.",
            "set_name": "Magic 2011",
            "set_code": "m11",
            "collector_number": "149",
        },
    },
    {
        "id": "long-oracle-symbol-at-cutoff",
        "index": 1,
        "card": {
            "name": "Sunlit Archivist",
            "mana_cost": "{2}{W}{W}",
            "type_line": "Creature — Human Cleric",
            "oracle_text": _LONG_ORACLE_TEXT,
            "power": "2",
            "toughness": "4",
            "set_name": "Testament of Parity",
            "set_code": "top",
            "collector_number": "12",
        },
    },
    {
        "id": "vanilla-creature-power-toughness",
        "index": 2,
        "card": {
            "name": "Grizzly Bears",
            "mana_cost": "{1}{G}",
            "type_line": "Creature — Bear",
            "oracle_text": "",
            "power": "2",
            "toughness": "2",
            "set_name": "Limited Edition Alpha",
            "set_code": "lea",
            "collector_number": "197",
        },
    },
    {
        # 🌳 is a surrogate pair in JS — guards that the 300-char alt-text cutoff
        # counts code points on both sides, not UTF-16 units
        "id": "alt-truncation-emoji-code-points",
        "index": 3,
        "card": {
            "name": "Verdant Chorus",
            "mana_cost": "{G}{G}",
            "type_line": "Enchantment",
            "oracle_text": ("{G} grows. " * 40).strip(),
            "set_name": "Testament of Parity",
            "set_code": "top",
            "collector_number": "33",
        },
    },
    {
        "id": "no-oracle-no-mana-cost",
        "index": 5,
        "card": {
            "name": "Blank Slate",
            "type_line": "Artifact",
            "set_code": "top",
            "collector_number": "77",
        },
    },
    {
        # image_placeholder set -> ph-<bucket> class + tint colors as CSS custom properties
        "id": "measured-placeholder-gradient",
        "index": 4,
        "card": {
            "name": "Breeding Pool",
            "type_line": "Land — Forest Island",
            "oracle_text": "({T}: Add {G} or {U}.)\nAs Breeding Pool enters the battlefield, you may pay 2 life.",
            "set_name": "Ravnica Remastered",
            "set_code": "rvr",
            "collector_number": "275",
            "image_placeholder": "m15-land 5a7a52 5a6f8a 3d5a55",
        },
    },
    {
        # no measured value + Land type line -> ph-fb-land fallback (type checks precede mana cost)
        "id": "placeholder-fallback-land",
        "index": 8,
        "card": {
            "name": "Command Tower",
            "type_line": "Land",
            "oracle_text": "{T}: Add one mana of any color in your commander's color identity.",
            "set_name": "Commander 2011",
            "set_code": "cmd",
            "collector_number": "269",
        },
    },
    {
        # no measured value + two mana colors -> ph-fb-gold fallback
        "id": "placeholder-fallback-gold",
        "index": 9,
        "card": {
            "name": "Lightning Helix",
            "mana_cost": "{R}{W}",
            "type_line": "Instant",
            "oracle_text": "Lightning Helix deals 3 damage to any target and you gain 3 life.",
            "set_name": "Ravnica: City of Guilds",
            "set_code": "rav",
            "collector_number": "213",
        },
    },
    {
        # malformed stored value must not reach the style attribute -> falls back
        "id": "placeholder-malformed-value-falls-back",
        "index": 10,
        "card": {
            "name": "Giant Growth",
            "mana_cost": "{G}",
            "type_line": "Instant",
            "oracle_text": "Target creature gets +3/+3 until end of turn.",
            "set_name": "Limited Edition Alpha",
            "set_code": "lea",
            "collector_number": "205",
            "image_placeholder": 'm15-g "onmouseover=alert(1) x y',
        },
    },
    {
        "id": "escaping-quotes-and-hybrid-mana",
        "index": 7,
        "card": {
            "name": 'Urza\'s "Bauble" <Prototype>',
            "mana_cost": "{W/U}{W/U}",
            "type_line": "Artifact",
            "oracle_text": '{T}, Sacrifice this artifact: Look at a card & say "done".',
            "set_name": "Ice Age",
            "set_code": "ice",
            "collector_number": "6",
        },
    },
]


def normalize_card_html(html: str) -> str:
    """Reduce card HTML to a render-equivalent form for parity comparison.

    Keep in sync with normalizeCardHtml in app.test.js. Strips the loading-hint
    attributes (fetchpriority/loading logic intentionally differs between the JS and
    no-JS paths) and inter-tag whitespace (template indentation differs).
    """
    html = html.replace(' fetchpriority="high"', "").replace(' loading="lazy"', "")
    return re.sub(r">\s+<", "><", html).strip()


def main() -> None:
    """Render each case through the Python renderer and write the fixture."""
    truncated = _LONG_ORACLE_TEXT[:200]
    if truncated.count("{") <= truncated.count("}"):
        message = f"200-char cutoff must land inside a mana symbol (prefix is {len(_LONG_ORACLE_PREFIX)} chars, need 198)"
        raise ValueError(message)

    fixture = [{**case, "html": normalize_card_html(create_card_html(case["card"], case["index"]))} for case in CASES]
    FIXTURE_PATH.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(fixture)} cases to {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
