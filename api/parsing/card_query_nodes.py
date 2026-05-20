"""Card-specific AST nodes and query processing."""

from __future__ import annotations

import re

from titlecase import titlecase

from api.parsing.db_info import (
    ALIAS_TO_FIELD_INFOS,
    CARD_SUPERTYPES,
    CARD_TYPES,
    COLOR_CODE_TO_NAME,
    COLOR_NAME_TO_CODE,
    DB_NAME_TO_FIELD_TYPE,
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
    QueryNode,
    RegexValueNode,
    StringValueNode,
    ValueNode,
    param_name,
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


def get_field_type(attr: str) -> str:
    """Get the field type for a given attribute name.

    Args:
        attr: The attribute name to look up.

    Returns:
        The field type for the attribute, or TEXT if not found.
    """
    return DB_NAME_TO_FIELD_TYPE.get(attr, FieldType.TEXT)


# Rarity ordering for comparison operations
RARITY_TO_NUMBER = {
    "common": 0,
    "uncommon": 1,
    "rare": 2,
    "mythic": 3,
    "special": 4,
    "bonus": 5,
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

    def to_sql(self, context: dict) -> str:
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


def _color_dict_to_mask(color_dict: dict[str, bool]) -> int:
    return sum(bit for color, bit in _COLOR_BITS.items() if color_dict.get(color))


def _subset_masks(query_mask: int) -> list[int]:
    return [v for v in range(32) if (v & ~query_mask) == 0]  # 5 colors => 2^5 possible bitmask values


def _proper_subset_masks(query_mask: int) -> list[int]:
    return [v for v in range(32) if (v & ~query_mask) == 0 and v != query_mask]  # 5 colors => 2^5 possible bitmask values


def get_colors_comparison_object(val: str) -> dict[str, bool]:
    """Convert color string to comparison object for database queries.

    Args:
        val: Color string (either color codes like 'WUBRG' or color name like 'red').

    Returns:
        Dictionary mapping color codes to True for matching colors.
        Returns an empty dict for colorless ('c' or 'colorless'), since colorless
        cards are stored with an empty color identity in the database.

    Raises:
        ValueError: If the color string is invalid.
    """
    # If all chars are color codes
    color_code_set = set(COLOR_CODE_TO_NAME)
    if val and set(val) <= color_code_set:
        # Colorless-only queries use an empty dict, matching how colorless cards
        # are stored (card_color_identity = {}) rather than {"C": True}.
        return {c.upper(): True for c in val if c != "c"}
    # If it's a color name (e.g. 'red', 'blue', etc.)
    try:
        letter_code = COLOR_NAME_TO_CODE[val]
        if letter_code == "c":
            return {}
        return {letter_code.upper(): True}
    except KeyError as e:
        msg = f"Invalid color string: {val}"
        raise ValueError(msg) from e


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


def extract_frame_data_from_raw_card(raw_card: dict) -> dict[str, bool]:
    """Extract frame data from a raw card dictionary.

    Combines frame version and frame effects into a single JSONB object,
    following the same pattern as _preprocess_card method.

    Args:
        raw_card: Raw card dictionary from Scryfall API.

    Returns:
        Dictionary mapping frame data keys to True.
    """
    frame_data = {}

    # Add frame version if present (titlecased for consistency)
    frame_version = raw_card.get("frame")
    if frame_version:
        frame_data[frame_version.title()] = True

    # Add frame effects if present (titlecased for consistency)
    frame_effects = raw_card.get("frame_effects", [])
    for effect in frame_effects:
        frame_data[effect.title()] = True

    return frame_data


def get_keywords_comparison_object(val: str) -> dict[str, bool]:
    """Convert keyword string to comparison object for database queries.

    Args:
        val: Keyword string to normalize.

    Returns:
        Dictionary mapping normalized keyword to True.
    """
    # Normalize the input keyword
    normalized_keyword = val.strip().title()
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


def mana_cost_str_to_dict(mana_cost_str: str) -> dict:
    """Convert a mana cost string to a dictionary of colored symbols and their counts.

    Supports both braced format ({W}{U}), unbraced format (WU or wu), and mixed format (R{G}).
    """
    colored_symbol_counts = {}
    mana_cost_upper = mana_cost_str.upper()

    # First, extract all braced symbols
    braced_symbols = re.findall(r"{([^}]*)}", mana_cost_upper)
    for mana_symbol in braced_symbols:
        try:
            int(mana_symbol)
        except ValueError:
            colored_symbol_counts[mana_symbol] = colored_symbol_counts.get(mana_symbol, 0) + 1
        else:
            pass

    # Then, process unbraced characters (replace braced sections with space to prevent merging)
    # We don't care about digits here, only colored symbols
    unbraced_part = re.sub(r"{[^}]*}", " ", mana_cost_upper)
    for char in unbraced_part:
        # Only count color characters (W, U, B, R, G, C)
        if char in "WUBRGC":
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
    braced_symbols = re.findall(r"{([^}]*)}", mana_cost_upper)
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
    unbraced_part = re.sub(r"{[^}]*}", " ", mana_cost_upper)
    # Match either: sequences of digits OR single color characters
    for token in re.findall(r"\d+|[WUBRGC]", unbraced_part):
        if token.isdigit():
            # Multi-digit generic mana (e.g., "11" in "11R")
            cmc += int(token)
        elif token in "WUBRGC":
            # Color character counts as 1
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


class ExactNameNode(QueryNode):
    """Represents an exact card name search using the ! prefix syntax from Scryfall.

    For example, !"Lightning Bolt" finds only cards with that exact name (case-insensitive).
    """

    def __init__(self, value: str) -> None:
        """Initialize an ExactNameNode with the exact name to search for."""
        self.value = value

    def to_sql(self, context: dict) -> str:
        """Generate SQL for exact name matching (case-insensitive, no wildcards).

        LIKE wildcard characters (% and _) are escaped so the value is matched
        literally rather than as a pattern.
        """
        escaped = self.value.lower().replace("%", r"\%").replace("_", r"\_")
        _param_name = param_name(escaped)
        context[_param_name] = escaped
        return f"(lower(card.card_name) LIKE %({_param_name})s)"

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

    def to_sql(self, context: dict) -> str:
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
        # Handle empty string values
        if isinstance(self.rhs, StringValueNode) and not self.rhs.value.strip():
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

    def _format_card_attribute_explanation(self, attr_node: CardAttributeNode, operator_str: str, rhs_str: str) -> str:  # noqa: PLR0911
        """Format explanation for card attribute comparisons."""
        db_column_name = attr_node.attribute_name.lower()

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
        if db_column_name == "card_name" and self.operator in (":", "="):
            return f"the name contains {rhs_str}"
        if db_column_name == "oracle_text" and self.operator in (":", "="):
            return f"the oracle text contains {rhs_str}"
        if db_column_name == "card_types" and self.operator in (":", "="):
            return f"the type contains {rhs_str}"
        if db_column_name == "card_rarity_int":
            return f"the rarity {operator_str} {rhs_str}"
        if db_column_name == "card_artist" and self.operator in (":", "="):
            return f"the artist contains {rhs_str}"
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
                # Try to expand single-letter color codes
                if len(value) == 1 and value.lower() in COLOR_CODE_TO_NAME:
                    return COLOR_CODE_TO_NAME[value.lower()].capitalize()
                # Try to expand multi-letter color codes (e.g., "ug" -> "Blue/Green")
                max_colors = 5
                if len(value) <= max_colors and all(c.lower() in COLOR_CODE_TO_NAME for c in value):
                    color_names = [COLOR_CODE_TO_NAME[c.lower()].capitalize() for c in value.lower()]
                    return "/".join(color_names)

            # If context is a format-related attribute, try to expand format codes
            if db_column_name == "card_legalities" and value.lower() in FORMAT_CODE_TO_NAME:
                return FORMAT_CODE_TO_NAME[value.lower()].capitalize()

        return value

    def _handle_card_attribute(self, context: dict) -> str:
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

        if self.operator == ":":
            return self._handle_colon_operator(context, field_type, lhs_sql, attr)

        if field_type == FieldType.TEXT:
            return self._handle_text_comparison(context, attr)

        msg = f"Unknown field type: {field_type}"
        raise NotImplementedError(msg)

    def _handle_text_comparison(self, context: dict, attr: str) -> str:
        """Handle text comparisons."""
        # artist is titlecased
        # card name is titlecased
        # set is lowercased
        if attr in ("card_artist", "card_name"):
            self.rhs.value = titlecase(self.rhs.value)
        elif attr in ("set", "card_set_code"):
            self.rhs.value = self.rhs.value.lower()
        return super().to_sql(context)

    def _handle_rarity_comparison(self, context: dict) -> str:
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

    def _handle_numeric_comparison(self, context: dict) -> str:
        if self.operator == ":":
            self.operator = "="
        return super().to_sql(context)

    def _handle_colon_operator(self, context: dict, field_type: str, lhs_sql: str, attr: str) -> str:
        """Handle colon operator for different field types."""
        if field_type == FieldType.TEXT:
            # Handle fields that need exact matching instead of pattern matching
            if attr in ("card_set_code", "card_layout", "card_border", "card_watermark", "collector_number"):
                # For layout, border, and watermark fields, lowercase the search value for case-insensitive matching
                if attr in ("card_layout", "card_border", "card_watermark") and hasattr(self.rhs, "value"):
                    self.rhs.value = self.rhs.value.lower()

                if self.operator == ":":
                    self.operator = "="
                return super().to_sql(context)

            # Regular text field handling with pattern matching
            return self._handle_text_field_pattern_matching(context, lhs_sql)

        msg = f"Unknown field type: {field_type}"
        raise NotImplementedError(msg)

    def _handle_mana_cost_comparison(self, context: dict) -> str:
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

    def _handle_mana_cost_approximate_comparison(self, context: dict, mana_cost_str: str) -> str:
        """Handle approximate mana cost comparisons using containment and CMC."""
        # Convert the query mana cost to dict for containment checking
        query_mana_dict = mana_cost_str_to_dict(mana_cost_str)
        query_cmc = calculate_cmc(mana_cost_str)

        # Prepare parameters
        mana_param = param_name(query_mana_dict)
        cmc_param = param_name(query_cmc)
        context[mana_param] = query_mana_dict
        context[cmc_param] = query_cmc

        # SQL fragments
        mana_jsonb_sql = "card.mana_cost_jsonb"
        cmc_sql = "card.cmc"

        if self.operator == "=":
            return f"({mana_jsonb_sql} = %({mana_param})s AND {cmc_sql} = %({cmc_param})s)"

        if self.operator == "<=":
            # Card costs <= query if:
            # 1. Card doesn't have more colored pips (card mana <@ query mana)
            # 2. Card doesn't cost more total (card cmc <= query cmc)
            return f"({mana_jsonb_sql} <@ %({mana_param})s AND {cmc_sql} <= %({cmc_param})s)"

        if self.operator == "<":
            # Card costs < query if:
            # 1. Card doesn't have more colored pips (card mana <@ query mana)
            # 2. Card doesn't cost more total (card cmc <= query cmc)
            # 3. Costs are not identical
            return (
                f"({mana_jsonb_sql} <@ %({mana_param})s AND {cmc_sql} <= %({cmc_param})s AND {mana_jsonb_sql} <> %({mana_param})s)"
            )

        if self.operator == ">=":
            # Card costs >= query if:
            # 1. Card has at least the colored pips (card mana @> query mana)
            # 2. Card costs at least as much total (card cmc >= query cmc)
            return f"(%({mana_param})s <@ {mana_jsonb_sql} AND {cmc_sql} >= %({cmc_param})s)"

        if self.operator == ">":
            # Card costs > query if:
            # 1. Card has at least the colored pips (card mana @> query mana)
            # 2. Card costs at least as much total (card cmc >= query cmc)
            # 3. Costs are not identical
            return (
                f"(%({mana_param})s <@ {mana_jsonb_sql} AND {cmc_sql} >= %({cmc_param})s AND {mana_jsonb_sql} <> %({mana_param})s)"
            )

        msg = f"Unsupported mana cost operator: {self.operator}"
        raise ValueError(msg)

    def _handle_date_search(self, context: dict) -> str:
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
        pname = param_name(search_value)
        context[pname] = search_value
        return f"(card.released_at {operator} %({pname})s)"

    def _handle_year_search(self, context: dict) -> str:
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
            p_start_name = param_name(start_of_year)
            p_end_name = param_name(start_of_next_year)
            context[p_start_name] = start_of_year
            context[p_end_name] = start_of_next_year
            return f"(%({p_start_name})s <= card.released_at AND card.released_at < %({p_end_name})s)"
        if operator == ">":
            # year > 2024 means released_at >= 2025-01-01
            pname = param_name(start_of_next_year)
            context[pname] = start_of_next_year
            return f"(card.released_at >= %({pname})s)"
        if operator == "<":
            # year < 2024 means released_at < 2024-01-01
            pname = param_name(start_of_year)
            context[pname] = start_of_year
            return f"(card.released_at < %({pname})s)"
        if operator == ">=":
            # year >= 2024 means released_at >= 2024-01-01
            pname = param_name(start_of_year)
            context[pname] = start_of_year
            return f"(card.released_at >= %({pname})s)"
        if operator == "<=":
            # year <= 2024 means released_at < 2025-01-01
            pname = param_name(start_of_next_year)
            context[pname] = start_of_next_year
            return f"(card.released_at < %({pname})s)"

        msg = f"Unsupported operator for year search: {operator}"
        raise ValueError(msg)

    def _handle_text_field_pattern_matching(self, context: dict, lhs_sql: str) -> str:
        """Handle pattern matching for regular text fields."""
        # Check if RHS is a regex pattern
        if isinstance(self.rhs, RegexValueNode):
            regex_pattern = self.rhs.value
            _param_name = param_name(regex_pattern)
            context[_param_name] = regex_pattern
            # Use PostgreSQL ~* operator for case-insensitive regex matching
            return f"({lhs_sql} ~* %({_param_name})s)"

        if isinstance(self.rhs, StringValueNode | ManaValueNode):
            txt_val = self.rhs.value.strip()
        elif isinstance(self.rhs, str):
            txt_val = self.rhs.strip()
        else:
            msg = f"Unknown type: {type(self.rhs)}, {locals()}"
            raise TypeError(msg)
        words = ["", *txt_val.lower().split(), ""]
        pattern = "%".join(words)
        _param_name = param_name(pattern)
        context[_param_name] = pattern
        return f"(lower({lhs_sql}) LIKE %({_param_name})s)"

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

    def _handle_jsonb_object(self, context: dict) -> str:  # noqa: PLR0912, PLR0915, C901
        # Produce the query as a jsonb object
        lhs_sql = self.lhs.to_sql(context)
        attr = self.lhs.attribute_name
        is_color_identity = False
        if attr in ("card_colors", "card_color_identity", "produced_mana"):
            rhs = get_colors_comparison_object(self.rhs.value.strip().lower())
            is_color_identity = attr == "card_color_identity"
            if is_color_identity and self.operator in (":", "<="):
                subsets = IntArray(_subset_masks(_color_dict_to_mask(rhs)))
                pmask = param_name(subsets)
                context[pmask] = subsets
                return f"(magic.color_identity_mask({lhs_sql}) = ANY(%({pmask})s::smallint[]))"
            if is_color_identity and self.operator == "<":
                subsets = IntArray(_proper_subset_masks(_color_dict_to_mask(rhs)))
                pmask = param_name(subsets)
                context[pmask] = subsets
                return f"(magic.color_identity_mask({lhs_sql}) = ANY(%({pmask})s::smallint[]))"
            pname = param_name(rhs)
            context[pname] = rhs
        elif attr == "devotion":
            # Devotion uses mana cost syntax, so we need to convert it to color comparison
            # Extract color codes from mana cost syntax like {G}, {R}{G}, etc.
            query_devotion = calculate_devotion(self.rhs.value.strip())
            pname = param_name(query_devotion)
            context[pname] = query_devotion
        elif attr == "card_keywords":
            rhs = get_keywords_comparison_object(self.rhs.value.strip())
            pname = param_name(rhs)
            context[pname] = rhs
        elif attr == "card_frame_data":
            # Frame data handling - treat like keywords (exact string match)
            rhs = get_frame_data_comparison_object(self.rhs.value.strip())
            pname = param_name(rhs)
            context[pname] = rhs
        elif attr == "card_oracle_tags":
            # Oracle tags are stored in lowercase, unlike keywords
            rhs = get_oracle_tags_comparison_object(self.rhs.value.strip())
            pname = param_name(rhs)
            context[pname] = rhs
        elif attr == "card_is_tags":
            # is: tags are stored in lowercase, similar to oracle tags
            rhs = get_is_tags_comparison_object(self.rhs.value.strip())
            pname = param_name(rhs)
            context[pname] = rhs
        elif attr == "card_legalities":
            # Handle legality searches - need original search attribute for status mapping
            original_attr = getattr(self.lhs, "original_attribute", attr)
            rhs = get_legality_comparison_object(self.rhs.value.strip(), original_attr)
            pname = param_name(rhs)
            context[pname] = rhs
        else:
            msg = f"Unknown attribute: {attr}"
            raise ValueError(msg)

        if self.operator == "=":
            return f"({lhs_sql} = %({pname})s)"
        if self.operator in (">=", ":"):
            return f"({lhs_sql} @> %({pname})s)"
        if self.operator == "<=":
            return f"({lhs_sql} <@ %({pname})s)"
        if self.operator == ">":
            return f"({lhs_sql} @> %({pname})s AND {lhs_sql} <> %({pname})s)"
        if self.operator == "<":
            return f"({lhs_sql} <@ %({pname})s AND {lhs_sql} <> %({pname})s)"
        if self.operator in ("!=", "<>"):
            return f"({lhs_sql} <> %({pname})s)"
        msg = f"Unknown operator: {self.operator}"
        raise ValueError(msg)

    def _handle_jsonb_array(self, context: dict) -> str:
        # TODO: this should produce the query as an array, not jsonb
        rhs_val = self.rhs.value.strip().title()
        if self.lhs.attribute_name.lower() in ("card_types", "card_subtypes", "type"):
            if rhs_val in CARD_SUPERTYPES | CARD_TYPES:
                self.lhs.attribute_name = "card_types"
            else:
                self.lhs.attribute_name = "card_subtypes"
        col = self.lhs.to_sql(context)

        inners = [rhs_val]
        pname = param_name(inners)
        context[pname] = inners
        query = f"%({pname})s"
        if self.operator == "=":
            return f"({col} <@ {query}) AND ({query} <@ {col})"
        if self.operator in (">=", ":"):
            return f"({query} <@ {col})"
        if self.operator == "<=":
            return f"({col} <@ {query})"
        if self.operator == ">":
            return f"({query} <@ {col}) AND NOT({col} <@ {query})"
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
