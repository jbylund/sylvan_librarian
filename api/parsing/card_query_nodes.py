"""Card-specific AST nodes and query processing."""

from __future__ import annotations

import re
import unicodedata

from titlecase import titlecase

from api.parsing.colors import COLOR_ALIAS_TO_CODES, COLOR_CODE_TO_NAME
from api.parsing.db_info import (
    ALIAS_TO_FIELD_INFOS,
    CARD_SUPERTYPES,
    CARD_TYPES,
    FORMAT_CODE_TO_NAME,
    FieldType,
    ParserClass,
)
from api.parsing.nodes import (
    AndNode,
    AttributeNode,
    BinaryOperatorNode,
    ManaValueNode,
    NotNode,
    NumericValueNode,
    OrNode,
    Query,
    QueryContext,
    QueryNode,
    RegexValueNode,
    StringValueNode,
    ValueNode,
    _node_to_json,
)
from api.utils.db_utils import IntArray

"""

# equality is the one where order not mattering is nice
# because otherwise it's all of a in b and all of b in a
color = query
color = query # as object
color ?& query and query ?& color # as array

color >= query
color @> query # as object
color ?& query # as array

color <= query
color <@ query # as object
query ?& color # as array

color > query
color @> query AND color <> query # as object
color ?& query AND not(query ?& color) # as array

color < query
color @> query AND color <> query # as object
query ?& color AND not(color ?& query) # as array
"""


# Rarity ordering for comparison operations
RARITY_TO_NUMBER = {
    "common": 0,
    "c": 0,
    "uncommon": 1,
    "u": 1,
    "rare": 2,
    "r": 2,
    "special": 3,
    "s": 3,
    "mythic": 4,
    "m": 4,
    "bonus": 5,
    "b": 5,
}


def get_rarity_number(rarity: str) -> int:
    """Convert rarity string to numeric value for comparison.

    Args:
        rarity: The rarity string (case-insensitive).

    Returns:
        Numeric value for the rarity.

    Raises:
        ValueError: If the rarity is not recognized.
    """
    rarity_lower = rarity.lower().strip()
    int_val = RARITY_TO_NUMBER.get(rarity_lower)
    if int_val is None:
        valid_rarities = str(tuple(RARITY_TO_NUMBER.keys()))
        msg = f"Unknown rarity: {rarity}. Valid rarities are: {valid_rarities}"
        raise ValueError(msg)
    return int_val


class CardAttributeNode(AttributeNode):
    """Card-specific attribute node with field mapping."""

    def __init__(self, attribute_name: str, matched_parser_class: ParserClass) -> None:
        """Initialize a card attribute node.

        Args:
            attribute_name: The search attribute name to map to database column.
            matched_parser_class: The parser class to use for this attribute.
        """
        # Preserve original attribute name BEFORE mapping for specialized handling
        self.original_attribute = attribute_name.lower()
        self.matched_parser_class = matched_parser_class

        # Look up field infos by alias and parser class
        # This handles cases where multiple columns share the same alias (e.g., collector_number and collector_number_int)
        alias_field_infos = ALIAS_TO_FIELD_INFOS.get(attribute_name.lower(), [])
        self.field_infos = [f for f in alias_field_infos if f.parser_class == matched_parser_class]

        (field_info,) = self.field_infos
        db_column_name = field_info.db_column_name

        super().__init__(db_column_name)

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"attribute_name": self.attribute_name, "original_attribute": self.original_attribute}

    def to_sql(self, context: QueryContext) -> str:
        """Generate SQL for card attribute node.

        Args:
            context: SQL parameter context.

        Returns:
            SQL string for the attribute reference.
        """
        del context
        # attribute_name is already set to the correct db_column_name in __init__
        return f"card.{self.attribute_name}"

    def to_human_explanation(self) -> str:
        """Convert to human-readable explanation."""
        # Map database column names to readable names
        name_map = {
            "cmc": "mana value",
            "creature_power": "power",
            "creature_toughness": "toughness",
            "card_color_identity": "color identity",
            "card_colors": "color",
            "card_name": "name",
            "oracle_text": "oracle text",
            "card_types": "type",
            "card_subtypes": "subtype",
            "card_rarity_int": "rarity",
            "card_legalities": "format",
            "card_artist": "artist",
            "card_set_code": "set",
            "mana_cost_jsonb": "mana cost",
            "planeswalker_loyalty": "loyalty",
            "type_line": "type line",
            "flavor_text": "flavor text",
            "card_keywords": "keyword",
            "card_layout": "layout",
            "card_border": "border",
            "card_watermark": "watermark",
            "released_at": "release date",
            "collector_number": "collector number",
            "price_usd": "price (USD)",
            "price_eur": "price (EUR)",
            "price_tix": "price (TIX)",
            "edhrec_rank": "EDHREC rank",
        }
        return name_map.get(self.attribute_name, self.attribute_name.replace("_", " "))

    def __repr__(self) -> str:
        """Return a string representation of the card attribute node."""
        return (
            f"{self.__class__.__name__}("
            f"attribute_name={self.attribute_name}, "
            f"matched_parser_class={self.matched_parser_class}, "
            f"field_infos={self.field_infos}"
            ")"
        )


_COLOR_BITS: dict[str, int] = {"W": 16, "U": 8, "B": 4, "R": 2, "G": 1}

# Canonical WUBRG(C) ordering for rendering a set of color codes back to a human, e.g. so
# "brgb" explains as "Black/Red/Green" rather than echoing input order.
_CANONICAL_COLOR_ORDER = "wubrgc"


def _color_codes_to_explanation(color_codes: str, *, name: str | None = None) -> str:
    """Format color letter codes with {W}-style tokens for the search feedback UI."""
    tokens = "".join(f"{{{c.upper()}}}" for c in color_codes)
    if name is None:
        name = "/".join(COLOR_CODE_TO_NAME[c].capitalize() for c in color_codes)
    return f"{name} ({tokens})"


def _color_dict_to_mask(color_dict: dict[str, bool]) -> int:
    return sum(bit for color, bit in _COLOR_BITS.items() if color_dict.get(color))


def _subset_masks(query_mask: int) -> list[int]:
    return [v for v in range(32) if (v & ~query_mask) == 0]  # 5 colors => 2^5 possible bitmask values


def _proper_subset_masks(query_mask: int) -> list[int]:
    return [v for v in range(32) if (v & ~query_mask) == 0 and v != query_mask]  # 5 colors => 2^5 possible bitmask values


def get_colors_comparison_object(val: str, attr: str = "card_colors") -> dict[str, bool]:
    """Convert color string to comparison object for database queries.

    Args:
        val: Color string (either color codes like 'WUBRG' or a color name like 'red' or
            'azorius' — every name in COLOR_ALIAS_TO_CODES, which is the vocabulary Scryfall
            itself accepts).
        attr: The DB column this value is being compared against. Colorless means two
            different things depending on the field: for card_colors/card_color_identity
            it's the *absence* of any color (Scryfall stores both as `[]`, verified
            against the live API — e.g. Sol Ring's colors/color_identity are both `[]`),
            so 'c'/'colorless' maps to an empty dict. For produced_mana, colorless is a
            genuine producible value (Sol Ring's produced_mana is `["C"]`, not `[]`), so
            it must map to {"C": True} there instead.

    Returns:
        Dictionary mapping color codes to True for matching colors.

    Raises:
        ValueError: If the color string is invalid.
    """
    colorless_is_value = attr == "produced_mana"
    # A color NAME spells a set of letters ('azorius' -> 'wu', 'brown' -> 'c', 'colorless' -> 'c');
    # a letter string already is one. Expanding the name FIRST leaves a single code path, so
    # `c:azorius` and `c:wu` serialize to the identical rhs and cannot drift apart, and the
    # colorless-is-a-value distinction below is stated once instead of once per spelling.
    codes = COLOR_ALIAS_TO_CODES.get(val, val)
    color_code_set = set(COLOR_CODE_TO_NAME)
    if codes and set(codes) <= color_code_set:
        if colorless_is_value:
            return {c.upper(): True for c in codes}
        # Colorless-only queries use an empty dict, matching how colorless cards
        # are stored (card_color_identity = {}) rather than {"C": True}.
        return {c.upper(): True for c in codes if c != "c"}
    msg = f"Invalid color string: {val}"
    raise ValueError(msg)


def get_frame_data_comparison_object(val: str) -> dict[str, bool]:
    """Convert frame data string to comparison object for database queries.

    Handles both frame versions (e.g., "2015", "1997") and frame effects (e.g., "showcase", "legendary").
    All values are titlecased for consistency.

    Args:
        val: Frame data string to normalize.

    Returns:
        Dictionary mapping normalized frame data to True.
    """
    val = val.strip()

    # Always titlecase for consistency
    normalized_val = val.title()

    return {normalized_val: True}


def get_keywords_comparison_object(val: str) -> dict[str, bool]:
    """Convert keyword string to comparison object for database queries.

    Args:
        val: Keyword string to normalize.

    Returns:
        Dictionary mapping normalized keyword to True.
    """
    # Keywords are stored in lowercase (see card_processing.preprocess_card)
    normalized_keyword = val.strip().lower()
    return {normalized_keyword: True}


def get_oracle_tags_comparison_object(val: str) -> dict[str, bool]:
    """Convert oracle tag string to comparison object for database queries.

    Args:
        val: Oracle tag string to normalize.

    Returns:
        Dictionary mapping normalized oracle tag to True.
    """
    # Oracle tags are stored in lowercase
    normalized_tag = val.strip().lower()
    return {normalized_tag: True}


def get_art_tags_comparison_object(val: str) -> dict[str, bool]:
    """Convert art tag string to comparison object for database queries.

    Args:
        val: Art tag string to normalize.

    Returns:
        Dictionary mapping normalized art tag to True.
    """
    normalized_tag = val.strip().lower()
    return {normalized_tag: True}


def get_is_tags_comparison_object(val: str) -> dict[str, bool]:
    """Convert is: tag string to comparison object for database queries.

    Args:
        val: is: tag string to normalize.

    Returns:
        Dictionary mapping normalized is: tag to True.
    """
    # is: tags are stored in lowercase
    normalized_tag = val.strip().lower()
    return {normalized_tag: True}


def get_legality_comparison_object(val: str, attr: str) -> dict[str, str]:
    """Convert legality search to comparison object for database queries.

    Args:
        val: Format name to search for.
        attr: The search attribute name (format, legal, banned, restricted).

    Returns:
        Dictionary mapping format to legality status.
    """
    # Normalize format name to lowercase
    format_name = val.strip().lower()

    # Map single letter format codes to full format names
    format_name = FORMAT_CODE_TO_NAME.get(format_name, format_name)

    # Map search attribute to legality status
    if attr in ("format", "f", "legal"):
        status = "legal"
    elif attr == "banned":
        status = "banned"  # Scryfall uses "banned" for banned cards
    elif attr == "restricted":
        status = "restricted"
    else:
        msg = f"Unknown legality attribute: {attr}"
        raise ValueError(msg)

    return {format_name: status}


# A braced symbol anywhere in a mana cost string, e.g. the '2' and 'W' of '{2}{W}'. Shared with
# api.parsing.mana_symbols, which validates every symbol this finds.
BRACED_MANA_SYMBOL = re.compile(r"{([^}]*)}")

# Bare (unbraced) pip characters counted below: a colour, colourless, snow, or X, confirmed against
# the real Scryfall API (mana:x behaves identically to mana:{x}, and mana:s to mana:{s}). Shared with
# api.parsing.mana_symbols, which rejects any bare character outside this alphabet — see that
# module's docstring for why.
BARE_MANA_ATOMS = frozenset("WUBRGCXS")


def mana_cost_str_to_dict(mana_cost_str: str) -> dict:
    """Convert a mana cost string to a dictionary of colored symbols and their counts.

    Supports both braced format ({W}{U}), unbraced format (WU or wu), and mixed format (R{G}).
    X is a real pip symbol here (its cmc contribution of 0 is handled separately
    by calculate_cmc), not a hybrid — {X} and bare X both produce an "X" key.
    """
    colored_symbol_counts = {}
    mana_cost_upper = mana_cost_str.upper()

    # First, extract all braced symbols
    braced_symbols = BRACED_MANA_SYMBOL.findall(mana_cost_upper)
    for mana_symbol in braced_symbols:
        try:
            int(mana_symbol)
        except ValueError:
            colored_symbol_counts[mana_symbol] = colored_symbol_counts.get(mana_symbol, 0) + 1
        else:
            pass

    # Then, process unbraced characters (replace braced sections with space to prevent merging)
    # We don't care about digits here, only colored symbols
    unbraced_part = BRACED_MANA_SYMBOL.sub(" ", mana_cost_upper)
    for char in unbraced_part:
        if char in BARE_MANA_ATOMS:
            colored_symbol_counts[char] = colored_symbol_counts.get(char, 0) + 1

    as_dict = {}
    for colored_symbol, count in colored_symbol_counts.items():
        as_dict[colored_symbol] = list(range(1, count + 1))
    return as_dict


def calculate_cmc(mana_cost_str: str) -> int:
    """Calculate the converted mana cost from a mana cost string.

    Supports both braced format ({W}{U}), unbraced format (WU or wu), and mixed format (R{G} or 1{r}1).
    Consecutive digits are treated as a single multi-digit number (e.g., "11R" is {11}{R}, not {1}{1}{R}).
    """
    cmc = 0
    mana_cost_upper = mana_cost_str.upper()

    # First, process all braced symbols
    braced_symbols = BRACED_MANA_SYMBOL.findall(mana_cost_upper)
    for mana_symbol in braced_symbols:
        try:
            # Generic mana symbols add to CMC
            cmc += int(mana_symbol)
        except ValueError:
            # X costs count as 0 for CMC calculation
            if mana_symbol == "X":
                continue
            # Colored mana symbols (W, U, B, R, G, etc.) each count as 1
            # Handle hybrid symbols like {W/U} as 1
            # Handle Phyrexian symbols like {W/P} as 1
            # For simplicity, any non-numeric, non-X symbol counts as 1
            cmc += 1

    # Then, process unbraced part (after removing braced sections)
    # Replace braced sections with a space to prevent adjacent digits from merging
    unbraced_part = BRACED_MANA_SYMBOL.sub(" ", mana_cost_upper)
    # Match either: sequences of digits OR single color/colourless/snow characters
    for token in re.findall(r"\d+|[WUBRGCS]", unbraced_part):
        if token.isdigit():
            # Multi-digit generic mana (e.g., "11" in "11R")
            cmc += int(token)
        elif token in "WUBRGCS":
            # Color (or snow) character counts as 1, same as its braced form
            cmc += 1

    return cmc


def calculate_devotion(mana_cost_str: str) -> dict:
    """Calculate devotion from a mana cost string, handling split mana costs properly.

    For split mana costs like {R/G}, each color contributes 1 to its respective devotion.
    For example, {R/G} contributes 1 to both R devotion and G devotion.
    """
    devotion = {"W": [], "U": [], "B": [], "R": [], "G": [], "C": []}
    for ichar in mana_cost_str.upper().strip():
        current_devotion = devotion.get(ichar)
        if current_devotion is not None:
            current_devotion.append(len(current_devotion) + 1)
    # Remove colors with 0 devotion for cleaner storage
    return {color: color_devotion for color, color_devotion in devotion.items() if color_devotion}


def _escape_like_pattern(value: str) -> str:
    # Backslash must be escaped first; otherwise the \ added for % and _ would be re-escaped.
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def fold_accents(value: str) -> str:
    """Strip Latin diacritics so accented and unaccented spellings compare equal.

    NFKD-decomposes each character into base letter + combining marks, then drops
    the marks (unicodedata.combining(c) != 0). This is the single source of truth
    for accent folding: it's used to precompute card_name_folded at import time
    (see preprocess_card()) and to fold the search term for fuzzy card_name:
    queries in both the SQL and Rust engine paths, so the two sides never diverge
    on what counts as "the same" name (#649).
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def collate_name(value: str) -> str:
    """Strip every non-alphanumeric character, so separators stop deciding a name match.

    The SEPARATOR half of the fold a bare ``name:`` word gets (fold_accents() is the other half).
    Scryfall compares a bare name word with diacritics folded and separators gone, which is what
    lets ``ft`` find "Sword **of the** Ages" (1,628 results against ``name:"ft"``'s 362) and
    ``limdul`` find "Lim-Dul's Vault". ``!"..."`` is compared the same way, which is why
    ``!"limduls vault"`` and ``!"Lim-Dul's Vault"`` both find the card that only the fully
    accented, fully punctuated spelling used to.

    The engine's ``collate_name`` (card_engine/src/lib.rs) is the twin that folds the STORED side.
    """
    return "".join(c for c in value if c.isalnum())


class ExactNameNode(QueryNode):
    """Represents an exact card name search using the ! prefix syntax from Scryfall.

    For example, !"Lightning Bolt" finds only cards with that exact name (case-insensitive).
    """

    def __init__(self, value: str) -> None:
        """Initialize an ExactNameNode with the exact name to search for."""
        self.value = value

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization.

        COLLATED -- lowercased, diacritics folded, every non-alphanumeric character removed --
        because that is the name Scryfall compares ``!`` against. Measured on api.scryfall.com
        2026-08-16, all four of ``!"Lim-Dul's Vault"`` (with and without the circumflex),
        ``!"lim-dul's vault"`` and ``!"limduls vault"`` answer the same single card, and
        ``!"eowyn, lady of rohan"`` answers "Eowyn, Lady of Rohan". Comparing the literal lowercase
        name -- what this emitted before -- answered only the first of those, so typing a card's
        name without its accent or its punctuation found nothing.
        """
        return {"value": collate_name(fold_accents(self.value.lower()))}

    def to_sql(self, context: QueryContext) -> str:
        """Generate SQL for exact name matching (case-insensitive, no wildcards).

        COLLATED on both sides, matching kwargs() and the engine: Scryfall answers ``!"limduls
        vault"`` with the same card as ``!"Lim-Dul's Vault"``. There is no stored column for the
        collated name, so the fold is expressed inline; see the card_name: branch of
        CardBinaryOperatorNode.to_sql for the indexing note. LIKE special characters (backslash,
        %, _) are escaped so the value is matched literally rather than as a pattern.
        """
        escaped = _escape_like_pattern(collate_name(fold_accents(self.value.lower())))
        placeholder = context.add(escaped)
        return f"(lower(regexp_replace(card.card_name_folded, '[^[:alnum:]]', '', 'g')) LIKE {placeholder})"

    def __repr__(self) -> str:
        """Return a string representation of the ExactNameNode."""
        return f"ExactNameNode({self.value!r})"

    def __eq__(self, other: object) -> bool:
        """Check equality with another ExactNameNode based on value."""
        if not isinstance(other, ExactNameNode):
            return False
        return self.value == other.value

    def __hash__(self) -> int:
        """Return a hash based on the value."""
        return hash(("ExactNameNode", self.value))

    def to_human_explanation(self) -> str:
        """Return a human-readable explanation for an exact name search."""
        return f'exact name is "{self.value}"'


class CardBinaryOperatorNode(BinaryOperatorNode):
    """Card-specific binary operator node with custom SQL generation."""

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        if not isinstance(self.lhs, CardAttributeNode):
            # Arithmetic / non-card-attribute lhs: generic serialization
            return {"lhs": _node_to_json(self.lhs), "op": self.operator, "rhs": _node_to_json(self.rhs)}

        field_infos = self.lhs.field_infos
        field_type = field_infos[0].field_type if field_infos else None

        if field_type == FieldType.JSONB_ARRAY:
            # Resolve type vs subtype without mutating self.lhs — build lhs JSON explicitly.
            rhs_val = self.rhs.value.strip().title()
            attr = self.lhs.attribute_name.lower()
            if attr in ("card_types", "card_subtypes", "type"):
                resolved_attr = "card_types" if rhs_val in CARD_SUPERTYPES | CARD_TYPES else "card_subtypes"
            else:
                resolved_attr = self.lhs.attribute_name
            lhs_json = {
                "node_type": "CardAttributeNode",
                "kwargs": {
                    "attribute_name": resolved_attr,
                    "original_attribute": self.lhs.original_attribute,
                },
            }
            return {"lhs": lhs_json, "op": self.operator, "rhs": [rhs_val]}

        return {"lhs": self.lhs.to_json(), "op": self.operator, "rhs": self._rhs_to_json()}

    def _rhs_to_json(self) -> object:  # noqa: PLR0911, PLR0912
        """Compute the JSON-serializable rhs for non-JSONB_ARRAY CardAttributeNode LHS."""
        if not self.lhs.field_infos:
            return _node_to_json(self.rhs)
        field_info = self.lhs.field_infos[0]
        field_type = field_info.field_type
        attr = self.lhs.attribute_name

        if field_type == FieldType.JSONB_OBJECT:
            # Mana cost and devotion: pass raw ManaValueNode for Rust to parse pip counts
            if field_info.parser_class == ParserClass.MANA:
                return _node_to_json(self.rhs)
            val = self.rhs.value.strip()
            if attr in ("card_colors", "card_color_identity", "produced_mana"):
                return list(get_colors_comparison_object(val.lower(), attr).keys())
            if attr == "card_keywords":
                return list(get_keywords_comparison_object(val).keys())
            if attr == "card_frame_data":
                return list(get_frame_data_comparison_object(val).keys())
            if attr == "card_oracle_tags":
                return list(get_oracle_tags_comparison_object(val).keys())
            if attr == "card_art_tags":
                return list(get_art_tags_comparison_object(val).keys())
            if attr == "card_is_tags":
                return list(get_is_tags_comparison_object(val).keys())
            if attr == "card_legalities":
                return list(get_legality_comparison_object(val, self.lhs.original_attribute).keys())

        if field_info.parser_class == ParserClass.RARITY and isinstance(self.rhs, StringValueNode):
            return NumericValueNode(get_rarity_number(self.rhs.value)).to_json()

        if attr in ("card_name", "card_artist") and isinstance(self.rhs, StringValueNode):
            value = titlecase(self.rhs.value)
            # A BARE card_name: word is COLLATED -- diacritics folded (#649) AND every
            # non-alphanumeric character removed -- because that is the string Scryfall matches a
            # bare word against. A QUOTED value (and a plain-literal regex lowered to one) is
            # matched literally instead, so it keeps neither fold; see StringValueNode. Measured on
            # api.scryfall.com 2026-08-16: `name:ft` 1,628 against `name:"ft"` 362, `name:ofthe`
            # 1,109 against `name:"ofthe"` 0, `name:limdul` 8 against `name:"limdul"` 0.
            #
            # `=` IS `:` HERE -- it is not a comparison on a string column, it carries no
            # information of its own, and the bare/quoted split survives it INTACT. Measured on
            # api.scryfall.com 2026-08-16: `name=ft` answers `name:ft`'s 1,628 (not `name:"ft"`'s
            # 362), `name="ft"` answers `name:"ft"`'s 362, and `name=limdul` answers
            # `name:limdul`'s 8. Gating on `:` alone sent `name=ft` down the literal path, where a
            # whole-string equality then answered nothing at all. `!=` is NOT in this class -- it
            # is the empty set on every string column.
            # `a:` gets the SAME split, on the same kind of evidence (api.scryfall.com, 2026-08-16):
            # `a:gawel` answers 10 exactly as `a:gaweł` does, `a:rebecca-guay` answers
            # `a:"rebecca guay"`'s 166, and `a:gu*ay` answers `a:guay`'s 197. An artist could only be
            # found under their own diacritics and punctuation before this.
            if attr in ("card_name", "card_artist") and self.operator in (":", "=") and not self.rhs.literal:
                return {"node_type": "CollatedNameValueNode", "kwargs": {"value": collate_name(fold_accents(value))}}
            return {"node_type": "StringValueNode", "kwargs": {"value": value}}

        return _node_to_json(self.rhs)

    def to_sql(self, context: QueryContext) -> str:
        """Generate SQL for card-specific binary operations.

        Args:
            context: SQL parameter context (unused).

        Returns:
            SQL string for the binary operation.
        """
        if isinstance(self.lhs, CardAttributeNode):
            return self._handle_card_attribute(context)

        # Fallback: use default logic
        return super().to_sql(context)

    def to_human_explanation(self) -> str:
        """Convert to human-readable explanation with card-specific formatting."""
        # `:` (contains / JSONB containment) against an empty value is always vacuous --
        # `LIKE '%'` matches every row, `{} <@ anything` is always true -- so it carries no
        # real constraint and explains to "". `=` against an empty value is the opposite: a
        # real, narrow constraint (the field is exactly empty, e.g. is:vanilla's `o=""`), so
        # it must NOT collapse here -- `_format_card_attribute_explanation` renders that case
        # below instead. Covers ManaValueNode too: a quoted empty mana/devotion value
        # (mana:"") parses to one of these, not a StringValueNode, since parse_mana_value
        # validates quoted values the same as bare ones (#909) — StringValueNode alone
        # stopped catching it (#950).
        if self.operator == ":" and isinstance(self.rhs, StringValueNode | ManaValueNode) and not self.rhs.value.strip():
            return ""
        # Handle plain string rhs (for empty queries)
        if isinstance(self.rhs, str) and not self.rhs.strip():
            return ""

        # Get left and right explanations
        lhs_str = self.lhs.to_human_explanation()
        rhs_str = self._explain_value(self.rhs, self.lhs) if isinstance(self.rhs, ValueNode) else self.rhs.to_human_explanation()

        # Get operator explanation
        operator_map = {
            "=": "is",
            "!=": "is not",
            ">=": "≥",
            "<=": "≤",
            ":": "contains",
            "*": "×",  # noqa: RUF001
            "/": "÷",
        }
        operator_str = operator_map.get(self.operator, self.operator)

        # Special formatting for card attributes
        if isinstance(self.lhs, CardAttributeNode):
            return self._format_card_attribute_explanation(self.lhs, operator_str, rhs_str)

        # Default format
        return f"{lhs_str} {operator_str} {rhs_str}"

    def _format_card_attribute_explanation(self, attr_node: CardAttributeNode, operator_str: str, rhs_str: str) -> str:  # noqa: PLR0911, PLR0912
        """Format explanation for card attribute comparisons."""
        db_column_name = attr_node.attribute_name.lower()

        # `=` against an empty value reaches here (see to_human_explanation) as a real
        # constraint, not the vacuous `:` case -- state it plainly rather than falling into
        # "the X contains " with nothing after it.
        if self.operator == "=" and not rhs_str:
            return f"the {attr_node.to_human_explanation()} is empty"

        # Special formatting for certain attributes
        if db_column_name == "card_color_identity" and self.operator in ("=", ":"):
            return f"the color identity is {rhs_str}"
        if db_column_name == "card_legalities" and self.operator in ("=", ":"):
            return f"it's legal in {rhs_str}"
        if db_column_name == "card_colors" and self.operator in ("=", ":"):
            return f"the color is {rhs_str}"
        if db_column_name == "creature_power":
            return f"the power {operator_str} {rhs_str}"
        if db_column_name == "creature_toughness":
            return f"the toughness {operator_str} {rhs_str}"
        if db_column_name == "cmc":
            return f"the mana value {operator_str} {rhs_str}"
        # `:` is substring containment on these; `=` is real equality against the whole
        # field (verified against the actual SQL: `name=X` -> `card_name = X`, `name:X` ->
        # `card_name_folded LIKE %X%`, same split for oracle_text/card_types/card_artist) --
        # so `=` reads as "is", not "contains", to describe what it actually checks. Any
        # other operator (e.g. `!=`) falls through to the generic default below.
        if db_column_name == "card_name" and self.operator in (":", "="):
            return f"the name is {rhs_str}" if self.operator == "=" else f"the name contains {rhs_str}"
        if db_column_name == "oracle_text" and self.operator in (":", "="):
            return f"the oracle text is {rhs_str}" if self.operator == "=" else f"the oracle text contains {rhs_str}"
        if db_column_name == "card_types" and self.operator in (":", "="):
            return f"the type is {rhs_str}" if self.operator == "=" else f"the type contains {rhs_str}"
        if db_column_name == "card_rarity_int":
            return f"the rarity {operator_str} {rhs_str}"
        if db_column_name == "card_artist" and self.operator in (":", "="):
            return f"the artist is {rhs_str}" if self.operator == "=" else f"the artist contains {rhs_str}"
        if db_column_name == "card_set_code" and self.operator in (":", "="):
            return f"the set contains {rhs_str}"

        # Default format using attribute name
        lhs_str = attr_node.to_human_explanation()
        return f"{lhs_str} {operator_str} {rhs_str}"

    def _explain_value(self, value_node: ValueNode, context_node: CardAttributeNode) -> str:
        """Explain a value node, expanding codes based on context."""
        # For non-StringValueNode types, just return the string value
        if not isinstance(value_node, StringValueNode):
            return str(value_node.value)

        value = value_node.value.strip()

        # If context is a color-related attribute, try to expand color codes
        if isinstance(context_node, CardAttributeNode):
            db_column_name = context_node.attribute_name.lower()
            if db_column_name in ("card_colors", "card_color_identity"):
                # A color NAME ('temur', 'azorius', 'blue', 'colorless') spells a letter set
                # via COLOR_ALIAS_TO_CODES. Single-letter spellings expand the same as bare
                # letter codes below ('blue' -> 'Blue ({U})'); multi-letter names show the
                # letters alongside the name as {G}{U}{R}-style bracket tokens so the reader
                # isn't left to memorize which colors a guild/shard/wedge name means (#990).
                # The frontend's showResults() runs the whole message through
                # convertManaSymbols() after escaping, which turns exactly these tokens into
                # real mana-font icons -- so this is the one spot in the string a server
                # response is allowed to steer frontend HTML, and only ever with this fixed
                # A-Z/digit token vocabulary.
                alias_codes = COLOR_ALIAS_TO_CODES.get(value.lower())
                if alias_codes is not None:
                    if len(alias_codes) == 1:
                        return _color_codes_to_explanation(alias_codes)
                    return _color_codes_to_explanation(alias_codes, name=value.capitalize())
                # Try to expand single-letter color codes
                if len(value) == 1 and value.lower() in COLOR_CODE_TO_NAME:
                    return _color_codes_to_explanation(value.lower())
                # Try to expand multi-letter color codes (e.g., "ug" -> "Blue/Green ({U}{G})"),
                # deduped and in canonical WUBRG(C) order so a repeated/scrambled letter string
                # like "brgb" reads as "Black/Red/Green ({B}{R}{G})" rather than echoing every
                # input letter in input order ("Black/Red/Green/Black").
                max_colors = 5
                if len(value) <= max_colors and all(c.lower() in COLOR_CODE_TO_NAME for c in value):
                    present = {c.lower() for c in value}
                    color_codes = "".join(c for c in _CANONICAL_COLOR_ORDER if c in present)
                    return _color_codes_to_explanation(color_codes)

            # If context is a format-related attribute, try to expand format codes
            if db_column_name == "card_legalities" and value.lower() in FORMAT_CODE_TO_NAME:
                return FORMAT_CODE_TO_NAME[value.lower()].capitalize()

        return value

    def _handle_card_attribute(self, context: QueryContext) -> str:
        """Handle card attribute-specific SQL generation."""
        attr = self.lhs.attribute_name
        field_infos = self.lhs.field_infos
        lhs_sql = self.lhs.to_sql(context)

        if not field_infos:
            msg = f"No field infos found for attribute: {attr} / {field_infos}"
            raise ValueError(msg)

        # Use the first field info for type determination
        # Multiple field infos can exist for the same alias (e.g., mana_cost_text and mana_cost_jsonb)
        # and special handling below will route to the correct one
        field_info = field_infos[0]
        field_type = field_info.field_type

        # Special handling for mana attributes with comparison operators
        if attr in ("mana_cost_text", "mana_cost_jsonb"):
            return self._handle_mana_cost_comparison(context)

        # Special handling for date/year searches
        if field_info.parser_class == ParserClass.DATE:
            return self._handle_date_search(context)
        if field_info.parser_class == ParserClass.YEAR:
            return self._handle_year_search(context)

        if field_info.parser_class == ParserClass.NUMERIC:
            return self._handle_numeric_comparison(context)

        if field_info.parser_class == ParserClass.RARITY:
            return self._handle_rarity_comparison(context)

        if field_type == FieldType.JSONB_OBJECT:
            return self._handle_jsonb_object(context)

        if field_type == FieldType.JSONB_ARRAY:
            return self._handle_jsonb_array(context)

        # `=` IS `:` ON A TEXT COLUMN -- a substring test, not an equality. It is the one operator
        # on these columns that carries no information of its own. Measured on api.scryfall.com
        # 2026-08-16 over the whole default corpus, `X=v` against `X:v`: `o=flying` 4,574 =
        # `o:flying` 4,574 (this answered the cards whose oracle text IS the word), `ft=aether` 80
        # = `ft:aether` 80, `name=ft` 1,628 = `name:ft` 1,628, `fo=lifelink` 713 = `fo:lifelink`
        # 713. The columns stored exact rather than searched -- set code, layout, border,
        # watermark, collector number -- are claimed inside `_handle_colon_operator` and keep a
        # genuine equality, because equality IS the meaning there; routing `=` through the same
        # door reaches them by the path that also lowercases the value, which `e=KHM` needed.
        #
        # `!=` and the ordered comparisons are NOT in this class and still fall through below.
        if self.operator in (":", "="):
            return self._handle_colon_operator(context, field_type, lhs_sql, attr)

        if field_type == FieldType.TEXT:
            return self._handle_text_comparison(context, attr)

        msg = f"Unknown field type: {field_type}"
        raise NotImplementedError(msg)

    def _handle_text_comparison(self, context: QueryContext, attr: str) -> str:
        """Handle text comparisons."""
        # artist is titlecased
        # card name is titlecased
        # set is lowercased
        if attr in ("card_artist", "card_name"):
            self.rhs.value = titlecase(self.rhs.value)
        elif attr in ("set", "card_set_code"):
            self.rhs.value = self.rhs.value.lower()
        return super().to_sql(context)

    def _handle_rarity_comparison(self, context: QueryContext) -> str:
        # Special handling for rarity - convert text values to numeric
        if isinstance(self.rhs, StringValueNode):
            try:
                rarity_number = get_rarity_number(self.rhs.value)
                # Replace the string value with the numeric value
                self.rhs = NumericValueNode(rarity_number)
            except ValueError as e:
                # Re-raise with more context
                msg = f"Invalid rarity in comparison: {e}"
                raise ValueError(msg) from e
        return self._handle_numeric_comparison(context)

    def _handle_numeric_comparison(self, context: QueryContext) -> str:
        if self.operator == ":":
            self.operator = "="
        return super().to_sql(context)

    def _handle_colon_operator(self, context: QueryContext, field_type: str, lhs_sql: str, attr: str) -> str:
        """Handle the containment operators -- `:` and its synonym `=` -- for different field types."""
        if field_type == FieldType.TEXT:
            # Handle fields that need exact matching instead of pattern matching
            if attr in ("card_set_code", "card_layout", "card_border", "card_watermark", "collector_number"):
                # set_code/layout/border/watermark are lowercased at import, so lowercasing the
                # search value gives case-insensitive matching with a plain equality.
                # collector_number is stored raw and mixed-case (e.g. "10E-105"): compare exactly.
                if attr in ("card_set_code", "card_layout", "card_border", "card_watermark") and hasattr(self.rhs, "value"):
                    self.rhs.value = self.rhs.value.lower()

                if self.operator == ":":
                    self.operator = "="
                return super().to_sql(context)

            # Regular text field handling with pattern matching
            return self._handle_text_field_pattern_matching(context, lhs_sql, attr)

        msg = f"Unknown field type: {field_type}"
        raise NotImplementedError(msg)

    def _handle_mana_cost_comparison(self, context: QueryContext) -> str:
        """Handle mana cost comparisons with approximate matching."""
        # TODO: need to use text or jsonb matching depending on the operator
        mana_cost_str = self.rhs.value

        # : means >=
        if self.operator == ":":
            self.operator = ">="

        # For comparison operators, we need both containment check and CMC check
        if self.operator in ("<=", "<", ">=", ">", "="):
            return self._handle_mana_cost_approximate_comparison(context, mana_cost_str)
        raise AssertionError(self)

    def _handle_mana_cost_approximate_comparison(self, context: QueryContext, mana_cost_str: str) -> str:
        """Handle approximate mana cost comparisons using containment and CMC."""
        query_mana_dict = mana_cost_str_to_dict(mana_cost_str)
        query_cmc = calculate_cmc(mana_cost_str)

        mana = context.add(query_mana_dict)
        cmc = context.add(query_cmc)

        mana_jsonb_sql = "card.mana_cost_jsonb"
        cmc_sql = "card.cmc"

        # NO PRINTED COST IS NOT A COST OF ZERO, and this lane could not tell them apart either.
        # A land and Ornithopter both store `mana_cost_jsonb = '{}'`, because `{0}` is a number and
        # so is not a pip; the difference survives only in `mana_cost_text`, which is '' on the
        # land and '{0}' on Ornithopter (card_processing copies Scryfall's `mana_cost` straight
        # across, and Scryfall emits both). Without this clause `m:{0}` is `'{}' <@ mana_cost_jsonb
        # AND cmc >= 0`, which is every card in the table.
        #
        # Measured on api.scryfall.com 2026-08-17 at unique=prints: `m:{0} t:land` is 195 — the
        # cards that print a literal {0} — against the whole land corpus, and `m:{0}` is 93,355
        # against 105,839. The engine lane gates the same way, on the interned string.
        #
        # `<> ''` also excludes NULL, which is what a row with no `mana_cost` key at all stores:
        # `NULL <> ''` is NULL, and a NULL conjunct is not TRUE, so both spellings of "no cost"
        # fall out together and neither needs its own branch.
        #
        # `!=` IS THE EXCEPTION on the engine lane — an absent cost differs from every queried
        # cost, so `m!={w} t:land` is 12,249 there — but `!=` never reaches this method: the
        # caller admits only `<= < >= > =` and asserts on anything else. There is nothing to carry
        # until this lane grows a `!=`.
        costed = "card.mana_cost_text <> ''"

        if self.operator == "=":
            return f"({costed} AND {mana_jsonb_sql} = {mana} AND {cmc_sql} = {cmc})"

        if self.operator == "<=":
            # Card costs <= query if:
            # 1. Card doesn't have more colored pips (card mana <@ query mana)
            # 2. Card doesn't cost more total (card cmc <= query cmc)
            return f"({costed} AND {mana_jsonb_sql} <@ {mana} AND {cmc_sql} <= {cmc})"

        if self.operator == "<":
            # Card costs < query if:
            # 1. Card doesn't have more colored pips (card mana <@ query mana)
            # 2. Card doesn't cost more total (card cmc <= query cmc)
            # 3. Costs are not identical
            return f"({costed} AND {mana_jsonb_sql} <@ {mana} AND {cmc_sql} <= {cmc} AND {mana_jsonb_sql} <> {mana})"

        if self.operator == ">=":
            # Card costs >= query if:
            # 1. Card has at least the colored pips (card mana @> query mana)
            # 2. Card costs at least as much total (card cmc >= query cmc)
            return f"({costed} AND {mana} <@ {mana_jsonb_sql} AND {cmc_sql} >= {cmc})"

        if self.operator == ">":
            # Card costs > query if:
            # 1. Card has at least the colored pips (card mana @> query mana)
            # 2. Card costs at least as much total (card cmc >= query cmc)
            # 3. Costs are not identical
            return f"({costed} AND {mana} <@ {mana_jsonb_sql} AND {cmc_sql} >= {cmc} AND {mana_jsonb_sql} <> {mana})"

        msg = f"Unsupported mana cost operator: {self.operator}"
        raise ValueError(msg)

    def _handle_date_search(self, context: QueryContext) -> str:
        """Handle date search queries.

        For 'date:' searches, compares against the full released_at date.
        Accepts either YYYY or YYYY-MM-DD format.

        Args:
            context: SQL parameter context.

        Returns:
            SQL string for the date comparison.
        """
        search_value = self.rhs.value if isinstance(self.rhs, StringValueNode | NumericValueNode) else str(self.rhs)

        # Normalize : operator to =
        operator = "=" if self.operator == ":" else self.operator

        # For date searches, compare against the full date
        # The value should be in YYYY-MM-DD or YYYY format
        placeholder = context.add(search_value)
        return f"(card.released_at {operator} {placeholder})"

    def _handle_year_search(self, context: QueryContext) -> str:
        """Handle year search queries.

        For 'year:' searches, converts to date range queries for better index usage.
        Only accepts 4-digit year values (YYYY).

        Args:
            context: SQL parameter context.

        Returns:
            SQL string for the year comparison using date ranges.
        """
        search_value = self.rhs.value if isinstance(self.rhs, StringValueNode | NumericValueNode) else str(self.rhs)

        # Normalize : operator to =
        operator = "=" if self.operator == ":" else self.operator

        # For year searches, convert to date range queries for better index usage
        # Only accept 4-digit year values
        year_str_length = 4
        if (isinstance(search_value, str) and len(search_value) == year_str_length and search_value.isdigit()) or isinstance(
            search_value,
            int | float,
        ):
            year_value = int(search_value)
        else:
            msg = f"Invalid year value: {search_value}. Year must be a 4-digit number."
            raise ValueError(msg)

        # Convert year comparison to date range for index usage
        # year=2024 becomes: '2024-01-01' <= released_at AND released_at < '2025-01-01'
        # year>2024 becomes: released_at >= '2025-01-01'
        # year<2024 becomes: released_at < '2024-01-01'
        # year>=2024 becomes: released_at >= '2024-01-01'
        # year<=2024 becomes: released_at < '2025-01-01'

        start_of_year = f"{year_value}-01-01"
        start_of_next_year = f"{year_value + 1}-01-01"

        if operator == "=":
            p_start = context.add(start_of_year)
            p_end = context.add(start_of_next_year)
            return f"({p_start} <= card.released_at AND card.released_at < {p_end})"
        if operator == ">":
            # year > 2024 means released_at >= 2025-01-01
            return f"(card.released_at >= {context.add(start_of_next_year)})"
        if operator == "<":
            # year < 2024 means released_at < 2024-01-01
            return f"(card.released_at < {context.add(start_of_year)})"
        if operator == ">=":
            # year >= 2024 means released_at >= 2024-01-01
            return f"(card.released_at >= {context.add(start_of_year)})"
        if operator == "<=":
            # year <= 2024 means released_at < 2025-01-01
            return f"(card.released_at < {context.add(start_of_next_year)})"

        msg = f"Unsupported operator for year search: {operator}"
        raise ValueError(msg)

    def _handle_text_field_pattern_matching(self, context: QueryContext, lhs_sql: str, attr: str) -> str:
        """Handle pattern matching for regular text fields."""
        # Check if RHS is a regex pattern
        if isinstance(self.rhs, RegexValueNode):
            # Use PostgreSQL ~* operator for case-insensitive regex matching
            return f"({lhs_sql} ~* {context.add(self.rhs.value)})"

        if isinstance(self.rhs, StringValueNode | ManaValueNode):
            txt_val = self.rhs.value.strip()
        elif isinstance(self.rhs, str):
            txt_val = self.rhs.strip()
        else:
            msg = f"Unknown type: {type(self.rhs)}, {locals()}"
            raise TypeError(msg)

        # card_name: is TWO searches, and which one this is was decided by the quotes.
        #
        # A BARE word is compared against the name with diacritics folded (#649, card_name_folded,
        # precomputed at import) AND every non-alphanumeric character removed -- the separator fold
        # Scryfall applies, which is what makes `name:ft` answer 1,628 rather than `name:"ft"`'s
        # 362 by reaching "Sword of the Ages" through the vanished space. There is no stored column
        # for that string, so the fold is expressed inline; a deployment that wants it indexed
        # wants a trigram index on the same expression (or a generated card_name_collated column),
        # which is what the Rust engine stores.
        #
        # A QUOTED value is compared against the name AS WRITTEN: `name:"eowyn"` answers 0 on
        # api.scryfall.com while `name:"eowyn"` with the accent answers 3.
        #
        # `name=` REACHES THIS, and the bare/quoted split survives it intact rather than being
        # flattened to one side: `name=ft` is 1,628 on api.scryfall.com (exactly `name:ft`, NOT
        # `name:"ft"`'s 362) and `name="ft"` is 362 (exactly `name:"ft"`), measured 2026-08-16. The
        # split is carried by `self.rhs.literal`, which the quotes set and the operator does not
        # touch, so routing `=` here preserves it for free. `name!=` is not in this class and
        # still takes the exact-match path on card_name.
        if attr == "card_name" and isinstance(self.rhs, StringValueNode) and self.rhs.literal:
            lhs_sql = "card.card_name"
        elif attr == "card_name":
            lhs_sql = "regexp_replace(card.card_name_folded, '[^[:alnum:]]', '', 'g')"
            txt_val = collate_name(fold_accents(txt_val))

        words = ["", *(_escape_like_pattern(w) for w in txt_val.lower().split()), ""]
        pattern = "%".join(words)
        return f"(lower({lhs_sql}) LIKE {context.add(pattern)})"

    """
    col = query
    col = query # as object
    col ?& query and query ?& col # as array

    col >= query
    col @> query # as object
    col ?& query # as array

    col <= query
    col <@ query # as object
    query ?& col # as array

    col > query
    col @> query AND col <> query # as object
    col ?& query AND not(query ?& col) # as array

    col < query
    col @> query AND col <> query # as object
    query ?& col AND not(col ?& query) # as array
    """

    def _handle_jsonb_object(self, context: QueryContext) -> str:  # noqa: PLR0912, C901
        # Produce the query as a jsonb object
        lhs_sql = self.lhs.to_sql(context)
        attr = self.lhs.attribute_name
        # `=` IS `:` ON A COLLECTION COLUMN -- set EQUALITY is not a meaning Scryfall gives it, and
        # a card whose keyword list is exactly ["Flying"] is not what anyone asking `kw=flying`
        # wants. Measured on api.scryfall.com 2026-08-16, `X=v` against `X:v` on the same corpus,
        # identical on every row::
        #
        #     kw=flying e:khm             28 = kw:flying 28
        #     otag=ramp e:khm             35 = otag:ramp 35
        #     is=foil e:khm t:creature   129 = is:foil 129
        #     f=modern e:khm             304 = f:modern 304
        #
        # The boundary is real and lies elsewhere: the COLOR columns in this same handler keep a
        # genuine equality, because equality is the meaning there -- `c=rg e:khm t:creature` is 1
        # against `c:rg`'s 2, and `id=rg` is 1 against `id:rg`'s 52. So does `devotion`, whose `=`
        # is a count comparison rather than a set one (`devotion={r} e:khm t:creature` is 20
        # against `devotion:{r}`'s 27). Probed in both directions before this changed.
        is_containment_collection = attr not in ("card_colors", "card_color_identity", "produced_mana", "devotion")
        is_color_identity = False
        if attr in ("card_colors", "card_color_identity", "produced_mana"):
            rhs = get_colors_comparison_object(self.rhs.value.strip().lower(), attr)
            is_color_identity = attr == "card_color_identity"
            if is_color_identity and self.operator in (":", "<="):
                subsets = IntArray(_subset_masks(_color_dict_to_mask(rhs)))
                pmask = context.add(subsets)
                return f"(magic.color_identity_mask({lhs_sql}) = ANY({pmask}::smallint[]))"
            if is_color_identity and self.operator == "<":
                subsets = IntArray(_proper_subset_masks(_color_dict_to_mask(rhs)))
                pmask = context.add(subsets)
                return f"(magic.color_identity_mask({lhs_sql}) = ANY({pmask}::smallint[]))"
            placeholder = context.add(rhs)
            # An empty rhs means the query was literally "c"/"colorless", not
            # "at least zero colors" -- jsonb containment against an empty
            # object (@>) is vacuously true for every row, so ":"/">=" must
            # fall back to exact equality when rhs is empty.
            if not rhs and self.operator in (":", ">="):
                return f"({lhs_sql} = {placeholder})"
        elif attr == "devotion":
            # Devotion uses mana cost syntax, so we need to convert it to color comparison
            # Extract color codes from mana cost syntax like {G}, {R}{G}, etc.
            query_devotion = calculate_devotion(self.rhs.value.strip())
            placeholder = context.add(query_devotion)
        elif attr == "card_keywords":
            rhs = get_keywords_comparison_object(self.rhs.value.strip())
            placeholder = context.add(rhs)
        elif attr == "card_frame_data":
            # Frame data handling - treat like keywords (exact string match)
            rhs = get_frame_data_comparison_object(self.rhs.value.strip())
            placeholder = context.add(rhs)
        elif attr == "card_oracle_tags":
            # Oracle tags are stored in lowercase, unlike keywords
            rhs = get_oracle_tags_comparison_object(self.rhs.value.strip())
            placeholder = context.add(rhs)
        elif attr == "card_art_tags":
            rhs = get_art_tags_comparison_object(self.rhs.value.strip())
            placeholder = context.add(rhs)
        elif attr == "card_is_tags":
            # is: tags are stored in lowercase, similar to oracle tags
            rhs = get_is_tags_comparison_object(self.rhs.value.strip())
            placeholder = context.add(rhs)
        elif attr == "card_legalities":
            # Handle legality searches - need original search attribute for status mapping
            original_attr = getattr(self.lhs, "original_attribute", attr)
            rhs = get_legality_comparison_object(self.rhs.value.strip(), original_attr)
            placeholder = context.add(rhs)
        else:
            msg = f"Unknown attribute: {attr}"
            raise ValueError(msg)

        if self.operator == "=" and not is_containment_collection:
            return f"({lhs_sql} = {placeholder})"
        if self.operator in (">=", ":", "="):
            return f"({lhs_sql} @> {placeholder})"
        if self.operator == "<=":
            return f"({lhs_sql} <@ {placeholder})"
        if self.operator == ">":
            return f"({lhs_sql} @> {placeholder} AND {lhs_sql} <> {placeholder})"
        if self.operator == "<":
            return f"({lhs_sql} <@ {placeholder} AND {lhs_sql} <> {placeholder})"
        if self.operator in ("!=", "<>"):
            return f"({lhs_sql} <> {placeholder})"
        msg = f"Unknown operator: {self.operator}"
        raise ValueError(msg)

    def _handle_jsonb_array(self, context: QueryContext) -> str:
        # TODO: this should produce the query as an array, not jsonb
        rhs_val = self.rhs.value.strip().title()
        if self.lhs.attribute_name.lower() in ("card_types", "card_subtypes", "type"):
            if rhs_val in CARD_SUPERTYPES | CARD_TYPES:
                self.lhs.attribute_name = "card_types"
            else:
                self.lhs.attribute_name = "card_subtypes"
        col = self.lhs.to_sql(context)

        query = context.add([rhs_val])
        # `=` IS `:` HERE TOO, for the same reason and on the same evidence: `t=creature e:khm` is
        # 151 and `t:creature e:khm` is 151, `t=legendary e:khm` is 42 and `t:legendary e:khm` is
        # 42 (api.scryfall.com, 2026-08-16). A set equality answered only the cards whose whole
        # type list is the one word, which is never what a type query means.
        if self.operator in (">=", ":", "="):
            return f"({query} <@ {col})"
        if self.operator == "<=":
            return f"({col} <@ {query})"
        if self.operator == ">":
            return f"({query} <@ {col}) AND NOT({col} <@ {query})"
        # < and != express set equality as containment BOTH WAYS, not as jsonb literal equality,
        # which is order-sensitive for arrays. `=` used to be spelled this way as well and is now
        # the containment above; these two are the only readers of the two-way form left.
        if self.operator == "<":
            return f"({col} <@ {query}) AND NOT({query} <@ {col})"
        if self.operator in ("!=", "<>"):
            return f"NOT(({col} <@ {query}) AND ({query} <@ {col}))"
        msg = f"Unknown operator: {self.operator}"
        raise ValueError(msg)


def to_card_query_ast(node: QueryNode) -> QueryNode:
    """Convert a generic query node to a card-specific AST node.

    Args:
        node: The query node to convert.

    Returns:
        The corresponding card-specific node.
    """
    # If already a card query AST node, return as-is
    if isinstance(node, CardBinaryOperatorNode):
        return node
    if isinstance(node, CardAttributeNode):
        return node

    if isinstance(node, BinaryOperatorNode):
        return CardBinaryOperatorNode(
            to_card_query_ast(node.lhs),
            node.operator,
            to_card_query_ast(node.rhs),
        )
    if isinstance(node, AttributeNode):
        return CardAttributeNode(
            attribute_name=node.attribute_name,
        )
    if isinstance(node, AndNode):
        return AndNode([to_card_query_ast(op) for op in node.operands])
    if isinstance(node, OrNode):
        return OrNode([to_card_query_ast(op) for op in node.operands])
    if isinstance(node, NotNode):
        return NotNode(to_card_query_ast(node.operand))
    if isinstance(node, Query):
        return Query(to_card_query_ast(node.root))
    return node
