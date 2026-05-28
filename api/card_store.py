"""In-memory card store for in-process query filtering."""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection

logger = logging.getLogger(__name__)


class CardField(IntEnum):
    """Integer index of each field in a card list. Order must match _DB_COLUMNS + computed fields."""

    # partition keys
    scryfall_id = 0
    oracle_id = 1
    illustration_id = 2
    # filterable columns (from db_info.py / magic.cards)
    card_artist = 3
    card_border = 4
    card_color_identity = 5
    card_colors = 6
    card_frame_data = 7
    card_is_tags = 8
    card_keywords = 9
    card_layout = 10
    card_legalities = 11
    card_name = 12
    card_oracle_tags = 13
    card_rarity_int = 14
    card_set_code = 15
    card_subtypes = 16
    card_types = 17
    card_watermark = 18
    cmc = 19
    collector_number = 20
    collector_number_int = 21
    creature_power = 22
    creature_toughness = 23
    devotion = 24
    edhrec_rank = 25
    flavor_text = 26
    mana_cost_jsonb = 27
    oracle_text = 28
    planeswalker_loyalty = 29
    price_eur = 30
    price_tix = 31
    price_usd = 32
    produced_mana = 33
    released_at = 34
    # result output columns
    creature_power_text = 35
    creature_toughness_text = 36
    mana_cost_text = 37
    set_name = 38
    type_line = 39
    prefer_score = 40
    # sort-only columns
    cubecobra_score = 41
    # computed at load time — pre-lowercased for fast `in` checks
    card_name_lower = 42
    card_artist_lower = 43
    oracle_text_lower = 44
    flavor_text_lower = 45


# Number of fields that come from the DB SELECT (the rest are computed at load time).
_NUM_DB_FIELDS = 42

_DB_COLUMNS = [f.name for f in CardField if f.value < _NUM_DB_FIELDS]
_COLUMNS_SQL = ", ".join(f"card.{col}" for col in _DB_COLUMNS)

# (source_field, lowercase_field) pairs populated at load time.
_LOWER_PAIRS = (
    (CardField.card_name, CardField.card_name_lower),
    (CardField.card_artist, CardField.card_artist_lower),
    (CardField.oracle_text, CardField.oracle_text_lower),
    (CardField.flavor_text, CardField.flavor_text_lower),
)

_NUM_FIELDS = len(CardField)

_store: list[list[Any]] = []


def load(conn: Connection) -> None:
    """Load all cards from the database into the in-memory store."""
    logger.info("Card store loading from database (pid=%d)...", __import__("os").getpid())
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT {_COLUMNS_SQL} FROM magic.cards AS card")
        rows = cursor.fetchall()
    logger.info("Card store fetched %d rows, building lists...", len(rows))

    cards: list[list[Any]] = []
    for row in rows:
        card: list[Any] = list(row.values())  # 42 DB fields
        card.extend([None] * (_NUM_FIELDS - _NUM_DB_FIELDS))  # computed field slots
        for src, dst in _LOWER_PAIRS:
            v = card[src]
            card[dst] = v.lower() if v is not None else None
        cards.append(card)

    _store.clear()
    _store.extend(cards)
    sample_name = _store[0][CardField.card_name] if _store else None
    logger.info("Card store loaded: %d cards (sample card_name=%r)", len(_store), sample_name)


def all_cards() -> list[list[Any]]:
    """Return the current in-memory card list."""
    return _store


def size() -> int:
    """Return the number of cards currently loaded."""
    return len(_store)
