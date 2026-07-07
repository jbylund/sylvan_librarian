"""Rust-backed card filter engine (PyO3 extension)."""

import enum

from .card_engine import QueryEngine as QueryEngine
from .card_engine import QueryError as QueryError
from .card_engine import UnknownFieldError as UnknownFieldError


class EngineField(enum.StrEnum):
    """Enum for the fields selectable via the `fields` parameter of QueryEngine's query methods.

    Mirrors FIELD_TABLE in src/lib.rs. Rust remains the source of truth for validation (an
    unknown name raises UnknownFieldError there); this exists for discoverability and
    typo-catching on the Python side, so it must be updated by hand whenever a field is added to
    FIELD_TABLE.
    """

    NAME = enum.auto()
    SET_CODE = enum.auto()
    COLLECTOR_NUMBER = enum.auto()
    POWER = enum.auto()
    TOUGHNESS = enum.auto()
    MANA_COST = enum.auto()
    ORACLE_TEXT = enum.auto()
    SET_NAME = enum.auto()
    TYPE_LINE = enum.auto()
    ILLUSTRATION_ID = enum.auto()
    SCRYFALL_ID = enum.auto()
    IMAGE_PLACEHOLDER = enum.auto()
    CARD_SUBTYPES = enum.auto()
    CARD_KEYWORDS = enum.auto()
    CARD_ORACLE_TAGS = enum.auto()
    CARD_ART_TAGS = enum.auto()
    CARD_IS_TAGS = enum.auto()
    CARD_FRAME_DATA = enum.auto()


# Columns fetched from magic.cards to populate the engine store.
# Must match what card_from_pydict() reads in src/lib.rs.
ENGINE_COLUMNS: list[str] = [
    "scryfall_id",
    "oracle_id",
    "illustration_id",
    "card_artist",
    "card_border",
    "card_color_identity",
    "card_colors",
    "card_frame_data",
    "card_is_tags",
    "card_keywords",
    "card_layout",
    "card_legalities",
    "card_name",
    "card_art_tags",
    "card_oracle_tags",
    "card_rarity_int",
    "card_set_code",
    "card_subtypes",
    "card_types",
    "card_watermark",
    "cmc",
    "collector_number",
    "collector_number_int",
    "image_placeholder",
    "creature_power",
    "creature_toughness",
    "edhrec_rank",
    "flavor_text",
    "mana_cost_jsonb",
    "mana_cost_text",
    "oracle_text",
    "planeswalker_loyalty",
    "price_eur",
    "price_tix",
    "price_usd",
    "produced_mana",
    "released_at",
    "creature_power_text",
    "creature_toughness_text",
    "set_name",
    "type_line",
    "prefer_score",
    "cubecobra_score",
]
