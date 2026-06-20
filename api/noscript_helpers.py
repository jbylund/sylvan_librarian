"""Helper functions for server-side rendering of search results (no-JS support)."""

from __future__ import annotations

import re

MAX_ORACLE_TEXT_LENGTH = 200


def escape_html(text: str) -> str:
    """Escape HTML special characters.

    Args:
    ----
        text: Text to escape

    Returns:
    -------
        HTML-escaped text
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


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

    oracle_text = oracle_text.strip()

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

    # Create alt text
    alt_text = escape_html(card.get("name", "Unknown Card"))

    # Build srcset and sizes for responsive images
    # sizes attribute matches the grid breakpoints:
    # - < 410px: 1 column (100vw minus padding/gap)
    # - 410-750px: 2 columns (50vw minus gap/padding)
    # - 750-1370px: 3 columns (33.33vw minus gap/padding)
    # - 1370-2500px: 4 columns (25vw minus gap/padding)
    # - >= 2500px: 5 columns (20vw minus gap/padding)
    srcset = (
        f"{escape_html(image_280)} 280w, "
        f"{escape_html(image_388)} 388w, "
        f"{escape_html(image_538)} 538w, "
        f"{escape_html(image_745)} 745w"
    )
    sizes = (
        "(max-width: 410px) calc(100vw - 60px), "
        "(max-width: 750px) calc(50vw - 30px), "
        "(max-width: 1370px) calc(33.33vw - 25px), "
        "(max-width: 2500px) calc(25vw - 20px), "
        "calc(20vw - 15px)"
    )

    # Create image HTML with srcset for responsive images
    # Use 388px as default src (good middle ground for initial load)
    priority_attr = ' fetchpriority="high"' if index == 0 else ""
    image_html = (
        f'<img class="card-image" '
        f'src="{escape_html(image_388)}" '
        f'srcset="{srcset}" '
        f'sizes="{sizes}" '
        f'alt="{alt_text}" title="{alt_text}"{priority_attr} />'
    )

    # Build card components
    name_html = f'<div class="card-name">{escape_html(card.get("name", "Unknown Card"))}</div>'

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
                 {name_html}
                 {mana_html}
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
