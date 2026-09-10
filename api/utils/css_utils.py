"""Extract the critical subset of a stylesheet for inlining into a page's <style> block.

Critical CSS is the rules needed to paint the page correctly before the external stylesheet loads.
Inlining them prevents the layout shift that would otherwise be visible on server-side rendered
result pages, where content is on screen before styles.css arrives.

Membership is an explicit allowlist rather than anything inferred: what is above the fold depends on
the markup, not on the stylesheet, so it cannot be derived from the CSS alone. Hover and focus states,
animations, and modal styles are deliberately excluded — none of them affect the initial paint, and
every rule inlined is bytes added to the HTML of every request.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import tinycss2

if TYPE_CHECKING:
    import pathlib

# Selectors inlined into the HTML <style> block. A rule is included when any comma-separated part of
# its selector appears here, so a shared rule like `.footer-legal, .footer-attribution` is pulled in
# by whichever part is listed.
_CRITICAL_SELECTORS = frozenset(
    {
        '[data-theme="light"]',
        '[data-theme="dark"]',
        "*",
        "html",
        "body",
        ".container",
        ".spacer",
        ".spacer-30",
        ".spacer-20",
        ".header",
        ".theme-toggle",
        ".header h1",
        ".header p",
        ".search-container",
        ".search-box",
        ".search-input",
        ".help-icon",
        ".order-controls",
        ".dropdown-label",
        ".order-dropdown",
        ".order-toggle",
        ".arrow-up",
        # Results grid — needed for SSR search result pages
        ".results-container",
        ".card-item",
        ".card-page-link",  # wraps .card-image; display:block/line-height:0 sizes the image box
        ".card-image",
        ".card-name-mana-row",
        ".card-name",
        ".card-mana",
        ".ms-cost",
        ".mana-symbol",
        ".card-type",
        ".card-text",
        ".card-set-power-row",
        ".card-set",
        ".card-power-toughness",
        ".results-count",
        "#statusMessage",
        # Footer — margin-top:auto positions it; missing this causes it to jump on styles load
        ".footer",
        ".footer-legal",  # also matches the comma rule .footer-legal, .footer-attribution, .footer-links
        ".footer-attribution a",
        ".footer-links a",
    }
)


def _selector_is_critical(selector: str) -> bool:
    """Return True if any part of a (possibly comma-separated) selector is critical.

    Args:
        selector: A serialized selector, which may list several comma-separated parts.

    Returns:
        Whether the rule this selector belongs to should be inlined.
    """
    return any(part.strip() in _CRITICAL_SELECTORS for part in selector.split(","))


def _minify(css: str) -> str:
    """Strip comments and collapse whitespace, since this is inlined into every page.

    Args:
        css: Serialized CSS.

    Returns:
        The same rules with comments and avoidable whitespace removed.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};:,>])\s*", r"\1", css)
    css = re.sub(r";\}", "}", css)
    return css.strip()


def build_critical_css(styles_path: pathlib.Path) -> str:
    """Extract and minify the critical rules from a stylesheet.

    `@media` blocks are descended into and rebuilt around whichever of their rules are critical, so a
    responsive override of a critical rule is kept rather than dropped with its wrapper.

    Args:
        styles_path: The stylesheet to read.

    Returns:
        Minified CSS ready to inline in a <style> block, empty if nothing matched.
    """
    rules = tinycss2.parse_stylesheet(styles_path.read_text(), skip_comments=True, skip_whitespace=True)
    parts: list[str] = []
    for rule in rules:
        if isinstance(rule, tinycss2.ast.QualifiedRule):
            selector = tinycss2.serialize(rule.prelude).strip()
            if _selector_is_critical(selector):
                parts.append(tinycss2.serialize([rule]))
        elif isinstance(rule, tinycss2.ast.AtRule) and rule.at_keyword == "media" and rule.content is not None:
            inner = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
            critical_inner = [
                r
                for r in inner
                if isinstance(r, tinycss2.ast.QualifiedRule) and _selector_is_critical(tinycss2.serialize(r.prelude).strip())
            ]
            if critical_inner:
                condition = tinycss2.serialize(rule.prelude).strip()
                inner_css = tinycss2.serialize(critical_inner)
                parts.append(f"@media {condition}{{{inner_css}}}")
    return _minify("".join(parts))
