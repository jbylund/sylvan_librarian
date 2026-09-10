"""Tests for extracting the critical subset of a stylesheet."""

from __future__ import annotations

import pathlib
import re

import pytest

from api.noscript_helpers import create_card_html
from api.utils.css_utils import build_critical_css

STYLES_PATH = pathlib.Path(__file__).parent.parent / "static" / "styles.css"

selection_testcases = {
    "critical_rule_is_kept": {"css": "body{color:red}", "expected": "body{color:red}"},
    "uncritical_rule_is_dropped": {"css": ".tooltip{color:red}", "expected": ""},
    "comma_rule_kept_via_any_part": {
        "css": ".footer-legal,.tooltip{color:red}",
        "expected": ".footer-legal,.tooltip{color:red}",
    },
    "hover_state_is_dropped": {"css": ".card-item:hover{color:red}", "expected": ""},
    "id_selector_is_kept": {"css": "#statusMessage{color:red}", "expected": "#statusMessage{color:red}"},
    "attribute_selector_is_kept": {
        "css": '[data-theme="dark"]{color:red}',
        "expected": '[data-theme="dark"]{color:red}',
    },
}


class TestSelection:
    """Only allowlisted selectors survive."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(selection_testcases.values()))),
        argvalues=[[v for _, v in sorted(selection_testcases[name].items())] for name in sorted(selection_testcases)],
        ids=sorted(selection_testcases),
    )
    def test_rule_selection(self, css: str, expected: str, tmp_path: pathlib.Path) -> None:
        """Test each selector shape is kept or dropped as the allowlist dictates."""
        stylesheet = tmp_path / "styles.css"
        stylesheet.write_text(css)
        assert build_critical_css(stylesheet) == expected

    def test_empty_stylesheet_yields_empty_string(self, tmp_path: pathlib.Path) -> None:
        """Test nothing critical produces nothing, rather than stray punctuation."""
        stylesheet = tmp_path / "styles.css"
        stylesheet.write_text(".tooltip{color:red}\n.modal{color:blue}\n")
        assert build_critical_css(stylesheet) == ""


class TestMediaQueries:
    """@media blocks are descended into and rebuilt around their critical rules."""

    def test_media_block_keeps_only_its_critical_rules(self, tmp_path: pathlib.Path) -> None:
        """Test a responsive override of a critical rule survives, without its uncritical siblings.

        Dropping the whole at-rule would lose the override; keeping it whole would inline rules that
        do not affect the initial paint.
        """
        stylesheet = tmp_path / "styles.css"
        stylesheet.write_text("@media (max-width:600px){body{color:red}.tooltip{color:blue}}")
        assert build_critical_css(stylesheet) == "@media (max-width:600px){body{color:red}}"

    def test_media_block_with_nothing_critical_is_dropped_entirely(self, tmp_path: pathlib.Path) -> None:
        """Test an at-rule whose rules are all uncritical leaves no empty wrapper behind."""
        stylesheet = tmp_path / "styles.css"
        stylesheet.write_text("@media (max-width:600px){.tooltip{color:blue}}")
        assert build_critical_css(stylesheet) == ""


class TestMinification:
    """Output is inlined into every page, so it carries no avoidable bytes."""

    def test_comments_and_whitespace_are_stripped(self, tmp_path: pathlib.Path) -> None:
        """Test the result has no comments and no padding around punctuation."""
        stylesheet = tmp_path / "styles.css"
        stylesheet.write_text("/* a comment */\nbody {\n    color: red;\n    margin: 0;\n}\n")
        result = build_critical_css(stylesheet)
        assert result == "body{color:red;margin:0}"
        assert "/*" not in result

    def test_real_stylesheet_extracts_a_nonempty_subset(self) -> None:
        """Test the shipped stylesheet still yields critical CSS, and less than the whole file.

        A selector rename in styles.css that silently emptied this would reintroduce the layout
        shift inlining exists to prevent.
        """
        result = build_critical_css(STYLES_PATH)
        assert result
        assert len(result) < len(STYLES_PATH.read_text())
        assert "body{" in result


class TestServerRenderedCards:
    """Every class the no-JS card renderer emits must be styled before styles.css arrives."""

    def test_every_ssr_card_class_defined_in_styles_is_critical(self) -> None:
        """Test no class on a server-rendered card is left to the full stylesheet.

        A class that styles.css defines but the allowlist omits paints unstyled first and reflows
        when the external stylesheet lands — the layout shift inlining exists to prevent. Classes
        styles.css does not define at all (the mana-font `ms-*` icons) come from another sheet and
        are out of scope.
        """
        card = {
            "name": "Sunlit Archivist",
            "mana_cost": "{2}{W}{W}",
            "type_line": "Creature — Human Cleric",
            "oracle_text": "Flying, vigilance\nWhenever Sunlit Archivist attacks, draw a card.",
            "power": "2",
            "toughness": "4",
            "set_name": "Testament of Parity",
            "set_code": "top",
            "collector_number": "12",
        }
        html = create_card_html(card, 0)
        emitted = {cls for attr in re.findall(r'class="([^"]*)"', html) for cls in attr.split()}
        defined = set(re.findall(r"\.([A-Za-z_][\w-]*)", STYLES_PATH.read_text()))
        critical = build_critical_css(STYLES_PATH)
        missing = sorted(cls for cls in emitted & defined if not re.search(rf"\.{re.escape(cls)}(?![\w-])", critical))
        assert missing == []
