"""In-memory card store for in-process query filtering."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection

logger = logging.getLogger(__name__)

# Explicit column list — union of filterable, result output, partition key, and sort columns.
# raw_card_blob is intentionally excluded (bulk with no query use).
_COLUMNS = [
    # partition keys
    "scryfall_id",
    "oracle_id",
    "illustration_id",
    # filterable (from db_info.py)
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
    "card_oracle_tags",
    "card_rarity_int",
    "card_set_code",
    "card_subtypes",
    "card_types",
    "card_watermark",
    "cmc",
    "collector_number",
    "collector_number_int",
    "creature_power",
    "creature_toughness",
    "devotion",
    "edhrec_rank",
    "flavor_text",
    "mana_cost_jsonb",
    "oracle_text",
    "planeswalker_loyalty",
    "price_eur",
    "price_tix",
    "price_usd",
    "produced_mana",
    "released_at",
    # result output columns
    "creature_power_text",
    "creature_toughness_text",
    "mana_cost_text",
    "set_name",
    "type_line",
    "prefer_score",
    # sort-only columns
    "cubecobra_score",
]

_COLUMNS_SQL = ", ".join(f"card.{col}" for col in _COLUMNS)

_store: list[dict[str, Any]] = []


def load(conn: Connection) -> None:
    """Load all cards from the database into the in-memory store."""
    logger.info("Card store loading from database (pid=%d)...", __import__("os").getpid())
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT {_COLUMNS_SQL} FROM magic.cards AS card")
        rows = cursor.fetchall()
    logger.info("Card store fetched %d rows, building dicts...", len(rows))
    cards = [dict(row) for row in rows]
    _store.clear()
    _store.extend(cards)
    sample_name = _store[0].get("card_name") if _store else None
    logger.info("Card store loaded: %d cards (sample card_name=%r)", len(_store), sample_name)


def all_cards() -> list[dict[str, Any]]:
    """Return the current in-memory card list."""
    return _store


def size() -> int:
    """Return the number of cards currently loaded."""
    return len(_store)
