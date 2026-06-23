"""Shared helpers for api tests."""

from __future__ import annotations

import uuid

from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import parse_scryfall_query
from api.utils.timer import Timer


def search_kwargs(
    query: str,
    limit: int = 10,
    orderby: CardOrdering = CardOrdering.EDHREC,
    direction: SortDirection = SortDirection.ASC,
) -> dict:
    """Build kwargs for _search_sql or _search_engine from a query string."""
    parsed = parse_scryfall_query(query)
    return {
        "parsed_query": parsed,
        "query": query,
        "unique": UniqueOn.CARD,
        "prefer": PreferOrder.DEFAULT,
        "orderby": orderby,
        "direction": direction,
        "limit": limit,
        "timer": Timer(),
    }


def make_raw_card(card_id: str | None = None, name: str = "Test Card", rarity: str = "common") -> dict:
    """Minimal raw Scryfall card dict that passes preprocess_card and satisfies NOT NULL constraints."""
    cid = card_id or str(uuid.uuid4())
    jpg = f"{cid[0]}/{cid[1]}/{cid}.jpg"
    return {
        "id": cid,
        "oracle_id": str(uuid.uuid4()),
        "name": name,
        "released_at": "2020-01-01",
        "legalities": {"vintage": "legal"},
        "games": ["paper"],
        "type_line": "Instant",
        "colors": [],
        "color_identity": [],
        "keywords": [],
        "prices": {"usd": "0.10"},
        "set": "tst",
        "rarity": rarity,
        "collector_number": "1",
        "image_uris": {
            "small": f"https://cards.scryfall.io/small/front/{jpg}",
            "normal": f"https://cards.scryfall.io/normal/front/{jpg}",
            "large": f"https://cards.scryfall.io/large/front/{jpg}",
            "png": f"https://cards.scryfall.io/png/front/{jpg}",
            "art_crop": f"https://cards.scryfall.io/art_crop/front/{jpg}",
            "border_crop": f"https://cards.scryfall.io/border_crop/front/{jpg}",
        },
    }
