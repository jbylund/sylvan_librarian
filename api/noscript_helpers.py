"""Helper functions for server-side rendering of search results (no-JS support)."""

from __future__ import annotations

import re

MAX_ORACLE_TEXT_LENGTH = 200
MAX_ALT_TEXT_LENGTH = 300  # oracle text truncation length in image alt/title text
_EAGER_LOAD_COUNT = 4  # covers a full first row at the widest common grid (4 columns)


def escape_html(text: str) -> str:
    """Escape HTML special characters.

    Args:
    ----
        text: Text to escape

    Returns:
    -------
        HTML-escaped text
    """
    # Matches the JS escapeHtml character set. Single quotes don't need escaping:
    # all attributes use double quotes and single quotes are safe in HTML text content.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def convert_mana_symbols(text: str, is_modal: bool = False) -> str:
    """Convert mana cost symbols to HTML with CSS classes.

    Args:
    ----
        text: Text containing mana symbols like {W}, {U}, etc.
        is_modal: Whether this is for a modal display

    Returns:
    -------
        HTML with mana symbol spans
    """
    if not text:
        return ""

    # Mana symbol mapping - matches JavaScript manaMap and hybridMap
    mana_map = {
        # Basic colors
        "{R}": "ms ms-r ms-cost",
        "{G}": "ms ms-g ms-cost",
        "{W}": "ms ms-w ms-cost",
        "{U}": "ms ms-u ms-cost",
        "{B}": "ms ms-b ms-cost",
        "{C}": "ms ms-c ms-cost",
        # Numbers
        "{0}": "ms ms-0 ms-cost",
        "{1}": "ms ms-1 ms-cost",
        "{2}": "ms ms-2 ms-cost",
        "{3}": "ms ms-3 ms-cost",
        "{4}": "ms ms-4 ms-cost",
        "{5}": "ms ms-5 ms-cost",
        "{6}": "ms ms-6 ms-cost",
        "{7}": "ms ms-7 ms-cost",
        "{8}": "ms ms-8 ms-cost",
        "{9}": "ms ms-9 ms-cost",
        "{10}": "ms ms-10 ms-cost",
        "{11}": "ms ms-11 ms-cost",
        "{12}": "ms ms-12 ms-cost",
        "{13}": "ms ms-13 ms-cost",
        "{14}": "ms ms-14 ms-cost",
        "{15}": "ms ms-15 ms-cost",
        "{16}": "ms ms-16 ms-cost",
        # Variables
        "{X}": "ms ms-x ms-cost",
        "{Y}": "ms ms-y ms-cost",
        "{Z}": "ms ms-z ms-cost",
        # Special
        "{T}": "ms ms-tap",
        "{Q}": "ms ms-untap",
        "{E}": "ms ms-energy",
        "{P}": "ms ms-p ms-cost",
        "{S}": "ms ms-s ms-cost",
        "{CHAOS}": "ms ms-chaos",
        "{PW}": "ms ms-pw",
        "{∞}": "ms ms-infinity",
        # Hybrid mana
        "{W/U}": "ms ms-wu ms-cost",
        "{U/B}": "ms ms-ub ms-cost",
        "{B/R}": "ms ms-br ms-cost",
        "{R/G}": "ms ms-rg ms-cost",
        "{G/W}": "ms ms-gw ms-cost",
        "{W/B}": "ms ms-wb ms-cost",
        "{U/R}": "ms ms-ur ms-cost",
        "{B/G}": "ms ms-bg ms-cost",
        "{R/W}": "ms ms-rw ms-cost",
        "{G/U}": "ms ms-gu ms-cost",
        # Hybrid with generic
        "{2/W}": "ms ms-2w ms-cost",
        "{2/U}": "ms ms-2u ms-cost",
        "{2/B}": "ms ms-2b ms-cost",
        "{2/R}": "ms ms-2r ms-cost",
        "{2/G}": "ms ms-2g ms-cost",
        # Phyrexian
        "{W/P}": "ms ms-wp ms-cost",
        "{U/P}": "ms ms-up ms-cost",
        "{B/P}": "ms ms-bp ms-cost",
        "{R/P}": "ms ms-rp ms-cost",
        "{G/P}": "ms ms-gp ms-cost",
        # Phyrexian hybrid
        "{W/U/P}": "ms ms-wup ms-cost",
        "{W/B/P}": "ms ms-wbp ms-cost",
        "{U/B/P}": "ms ms-ubp ms-cost",
        "{U/R/P}": "ms ms-urp ms-cost",
        "{B/R/P}": "ms ms-brp ms-cost",
        "{B/G/P}": "ms ms-bgp ms-cost",
        "{R/W/P}": "ms ms-rwp ms-cost",
        "{R/G/P}": "ms ms-rgp ms-cost",
        "{G/W/P}": "ms ms-gwp ms-cost",
        "{G/U/P}": "ms ms-gup ms-cost",
    }

    symbol_class = "modal-mana-symbol" if is_modal else "mana-symbol"

    # Use regex to find and replace mana symbols in a single pass
    def replace_symbol(match: re.Match) -> str:
        symbol = match.group(0)
        css_classes = mana_map.get(symbol)
        if css_classes:
            return f'<span class="{symbol_class} {css_classes}"></span>'
        return symbol  # Return unchanged if not in map

    return re.sub(r"\{[^}]{1,5}\}", replace_symbol, text)


# Unicode text representations of mana symbols — matches JavaScript manaTextMap
_MANA_TEXT_MAP = {
    "{W}": "☀️",
    "{U}": "💧",
    "{B}": "💀",
    "{R}": "🔥",
    "{G}": "🌳",
    "{C}": "◇",
    "{T}": "↻",
    "{Q}": "↺",
    "{E}": "⚡",
    "{P}": "Φ",
    "{S}": "❄",
    "{X}": "X",
    "{Y}": "Y",
    "{Z}": "Z",
    "{0}": "⓪",
    "{1}": "①",
    "{2}": "②",
    "{3}": "③",
    "{4}": "④",
    "{5}": "⑤",
    "{6}": "⑥",
    "{7}": "⑦",
    "{8}": "⑧",
    "{9}": "⑨",
    "{10}": "⑩",
    "{11}": "⑪",
    "{12}": "⑫",
    "{13}": "⑬",
    "{14}": "⑭",
    "{15}": "⑮",
    "{16}": "⑯",
    "{CHAOS}": "🌀",
    "{PW}": "PW",
    "{∞}": "♾︎",
    "{W/U}": "(☀️/💧)",
    "{U/B}": "(💧/💀)",
    "{B/R}": "(💀/🔥)",
    "{R/G}": "(🔥/🌳)",
    "{G/W}": "(🌳/☀️)",
    "{W/B}": "(☀️/💀)",
    "{U/R}": "(💧/🔥)",
    "{B/G}": "(💀/🌳)",
    "{R/W}": "(🔥/☀️)",
    "{G/U}": "(🌳/💧)",
    "{2/W}": "(②/☀️)",
    "{2/U}": "(②/💧)",
    "{2/B}": "(②/💀)",
    "{2/R}": "(②/🔥)",
    "{2/G}": "(②/🌳)",
    "{W/P}": "(☀️/Φ)",
    "{U/P}": "(💧/Φ)",
    "{B/P}": "(💀/Φ)",
    "{R/P}": "(🔥/Φ)",
    "{G/P}": "(🌳/Φ)",
    "{W/U/P}": "(☀️/💧/Φ)",
    "{W/B/P}": "(☀️/💀/Φ)",
    "{U/B/P}": "(💧/💀/Φ)",
    "{U/R/P}": "(💧/🔥/Φ)",
    "{B/R/P}": "(💀/🔥/Φ)",
    "{B/G/P}": "(💀/🌳/Φ)",
    "{R/W/P}": "(🔥/☀️/Φ)",
    "{R/G/P}": "(🔥/🌳/Φ)",
    "{G/W/P}": "(🌳/☀️/Φ)",
    "{G/U/P}": "(🌳/💧/Φ)",
}


def convert_mana_symbols_to_text(text: str) -> str:
    """Convert mana symbols to Unicode text (for alt text) — matches JS convertManaSymbolsToText.

    Args:
    ----
        text: Text containing mana symbols like {W}, {U}, etc.

    Returns:
    -------
        Text with mana symbols replaced by Unicode representations
    """
    if not text:
        return ""

    return re.sub(r"\{[^}]{1,5}\}", lambda match: _MANA_TEXT_MAP.get(match.group(0), match.group(0)), text)


def format_oracle_text(oracle_text: str, is_modal: bool = False) -> str:
    """Format oracle text with mana symbols and line breaks.

    Args:
    ----
        oracle_text: The oracle text to format
        is_modal: Whether this is for a modal display

    Returns:
    -------
        Formatted HTML
    """
    if not oracle_text:
        return ""

    # Convert mana symbols first
    formatted = convert_mana_symbols(oracle_text, is_modal)

    # Convert newlines to HTML line breaks
    return formatted.replace("\n", "<br>")


def build_image_url(card: dict, size: str) -> str:
    """Build the CloudFront URL for a card image.

    Args:
    ----
        card: Card dictionary with set_code, collector_number, and optionally face_idx
        size: Image size (280, 388, 538, or 745)

    Returns:
    -------
        Image URL
    """
    face = card.get("face_idx", 1)
    set_code = card["set_code"]
    collector_number = card["collector_number"]
    return f"https://d1hot9ps2xugbc.cloudfront.net/img/{set_code}/{collector_number}/{face}/{size}.webp"


# Stored placeholder value shape: "<bucket> <frameL> <frameR> <art>" with bare hex colors.
# Anchored and charset-limited so the parts can be embedded in class/style attributes directly.
_PLACEHOLDER_VALUE_RE = re.compile(r"^([a-z0-9-]+) ([0-9a-f]{6}) ([0-9a-f]{6}) ([0-9a-f]{6})$")


def placeholder_parts(card: dict) -> tuple[str, str]:
    """CSS class and inline style of the placeholder shown behind the card image while it loads.

    Measured printings carry an image_placeholder value naming a frame-template bucket
    plus three tint colors (classes/templates in static/placeholders-v1.css); the colors
    ride as CSS custom properties. Unmeasured printings fall back to a coarse ph-fb-*
    class derived from type line and mana cost, whose bucket-default colors are baked
    into the stylesheet. Matches placeholderParts in JS createCardHTML.

    Args:
    ----
        card: Card dictionary with image_placeholder, type_line, and mana_cost

    Returns:
    -------
        (class name, style attribute value or empty string)
    """
    match = _PLACEHOLDER_VALUE_RE.match(card.get("image_placeholder") or "")
    if match:
        bucket, frame_l, frame_r, art = match.groups()
        return f"ph-{bucket}", f"--frame-l:#{frame_l};--frame-r:#{frame_r};--art:#{art}"
    type_line = card.get("type_line") or ""
    if "Land" in type_line:
        return "ph-fb-land", ""
    if "Artifact" in type_line:
        return "ph-fb-artifact", ""
    colors = {symbol for symbol in card.get("mana_cost") or "" if symbol in "WUBRG"}
    if len(colors) > 1:
        return "ph-fb-gold", ""
    if len(colors) == 1:
        return f"ph-fb-{colors.pop().lower()}", ""
    return "ph-fb-artifact", ""


def _build_alt_text(card: dict) -> str:
    """Build descriptive image alt text with card name, mana cost, and oracle text.

    Matches the alt text built in JS createCardHTML.

    Args:
    ----
        card: Card dictionary with name, mana_cost, and oracle_text

    Returns:
    -------
        HTML-escaped alt text
    """
    alt_text = escape_html(card.get("name") or "Unknown Card")
    if card.get("mana_cost"):
        mana_text_representation = convert_mana_symbols_to_text(card["mana_cost"])
        alt_text += f" / {escape_html(mana_text_representation)}"
    alt_text += "\n\n"
    if card.get("oracle_text"):
        oracle_text_with_symbols = convert_mana_symbols_to_text(card["oracle_text"])
        if len(oracle_text_with_symbols) > MAX_ALT_TEXT_LENGTH:
            oracle_text_with_symbols = oracle_text_with_symbols[:MAX_ALT_TEXT_LENGTH] + "..."
        alt_text += escape_html(oracle_text_with_symbols)
    return alt_text


def create_card_html(card: dict, index: int) -> str:
    """Generate HTML for a single card (server-side rendering).

    Args:
    ----
        card: Card dictionary with name, mana_cost, type_line, oracle_text, set_name, etc.
        index: Card index for ID generation

    Returns:
    -------
        HTML string for the card
    """
    card_id = str(index)

    # Build image URLs for srcset - using 4 sizes uniformly spread between 280 and 745
    image_280 = build_image_url(card, "280")
    image_388 = build_image_url(card, "388")
    image_538 = build_image_url(card, "538")
    image_745 = build_image_url(card, "745")

    alt_text = _build_alt_text(card)

    # Build srcset and sizes for responsive images
    # sizes breakpoints are one below the CSS grid min-width thresholds (< not <=):
    # - < 410px: 1 column, < 750px: 2 columns, < 1370px: 3 columns, < 2500px: 4 columns, else 5
    # calc values derived from CSS: body padding 1em (2em total), card padding 0.8em (1.6em total), gap 15px
    srcset = (
        f"{escape_html(image_280)} 280w, "
        f"{escape_html(image_388)} 388w, "
        f"{escape_html(image_538)} 538w, "
        f"{escape_html(image_745)} 745w"
    )
    sizes = (
        "(max-width: 409px) calc(100vw - 3.6em), "
        "(max-width: 749px) calc(50vw - 2.6em - 7.5px), "
        "(max-width: 1369px) calc(33.33vw - 2.27em - 10px), "
        "(max-width: 2499px) calc(25vw - 2.1em - 11.25px), "
        "calc(20vw - 2em - 12px)"
    )

    # Create image HTML with srcset for responsive images
    # Use 388px as default src (good middle ground for initial load)
    priority_attr = ' fetchpriority="high"' if index == 0 else ""
    lazy_attr = "" if index < _EAGER_LOAD_COUNT else ' loading="lazy"'
    ph_class, ph_style = placeholder_parts(card)
    ph_style_attr = f' style="{ph_style}"' if ph_style else ""
    img_tag = (
        f'<img class="card-image {ph_class}"{ph_style_attr} '
        f'src="{escape_html(image_388)}" '
        f'srcset="{srcset}" '
        f'sizes="{sizes}" '
        f'alt="{alt_text}" title="{alt_text}"{priority_attr}{lazy_attr} />'
    )

    # Link the image to the card detail page — matches JS createCardHTML
    if card.get("set_code") and card.get("collector_number"):
        card_page_path = f"/card/{escape_html(card['set_code'])}/{escape_html(card['collector_number'])}"
        image_html = f'<a href="{card_page_path}" class="card-page-link">{img_tag}</a>'
    else:
        image_html = img_tag

    # Build card components
    name_html = f'<div class="card-name">{escape_html(card.get("name") or "Unknown Card")}</div>'

    mana_html = ""
    if card.get("mana_cost"):
        mana_converted = convert_mana_symbols(card["mana_cost"], False)
        mana_html = f'<div class="card-mana">{mana_converted}</div>'

    type_html = ""
    if card.get("type_line"):
        type_html = f'<div class="card-type">{escape_html(card["type_line"])}</div>'

    oracle_html = ""
    if card.get("oracle_text"):
        oracle_text = card["oracle_text"]
        # Truncate carefully to avoid cutting mana symbols in half
        if len(oracle_text) > MAX_ORACLE_TEXT_LENGTH:
            truncated = oracle_text[:MAX_ORACLE_TEXT_LENGTH]
            # If we're in the middle of a mana symbol (unclosed brace), back up to before it
            if truncated.count("{") > truncated.count("}"):
                truncated = truncated.rpartition("{")[0]
            formatted = format_oracle_text(truncated, False)
            oracle_html = f'<div class="card-text">{formatted}...</div>'
        else:
            formatted = format_oracle_text(oracle_text, False)
            oracle_html = f'<div class="card-text">{formatted}</div>'

    set_power_html = ""
    has_set = card.get("set_name")
    has_power_toughness = card.get("power") is not None and card.get("toughness") is not None

    if has_set or has_power_toughness:
        set_part = f'<div class="card-set">{escape_html(card["set_name"])}</div>' if has_set else '<div class="card-set"></div>'
        power_toughness_part = ""
        if has_power_toughness:
            power_toughness_part = (
                f'<div class="card-power-toughness">{escape_html(str(card["power"]))} / {escape_html(str(card["toughness"]))}</div>'
            )
        set_power_html = f'<div class="card-set-power-row">{set_part}{power_toughness_part}</div>'

    return f"""
             <div class="card-item" data-card-id="{escape_html(card_id)}">
                 {image_html}
                 <div class="card-name-mana-row">
                     {name_html}
                     {mana_html}
                 </div>
                 {type_html}
                 {oracle_html}
                 {set_power_html}
             </div>
         """


def generate_results_html(cards: list[dict]) -> str:
    """Generate HTML for all cards in search results.

    Args:
    ----
        cards: List of card dictionaries

    Returns:
    -------
        HTML string for all cards
    """
    return "".join(create_card_html(card, i) for i, card in enumerate(cards))


def generate_results_count_html(total_cards: int, query: str) -> str:
    """Generate HTML for the results count display.

    Args:
    ----
        total_cards: Total number of cards found
        query: Search query string

    Returns:
    -------
        HTML string for results count
    """
    escaped_query = escape_html(query)
    card_word = "card" if total_cards == 1 else "cards"
    return f'Found {total_cards} {card_word} matching "{escaped_query}"'
