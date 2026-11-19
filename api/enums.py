"""Enums for the API."""

import enum


class UniqueOn(enum.StrEnum):
    """Enum for the distinct on column for the search."""

    CARD = enum.auto()
    PRINTING = enum.auto()
    ARTWORK = enum.auto()


class PreferOrder(enum.StrEnum):
    """Enum for the prefer order column for the search."""

    DEFAULT = enum.auto()
    OLDEST = enum.auto()
    NEWEST = enum.auto()
    USD_LOW = enum.auto()
    USD_HIGH = enum.auto()
    PROMO = enum.auto()


class CardOrdering(enum.StrEnum):
    """Enum for the ordering of the cards.

    Every member must have a `SortCol` arm in card_engine/src/lib.rs and a `sql_orderby` entry in
    api_resource.py. `orderby_to_col` falls through to edhrec on an unknown name, so a member added
    here and nowhere else makes the engine and SQL paths sort the same query differently.

    `cubecobra` is this project's own; the rest are Scryfall's `order=` vocabulary. Scryfall's
    `penny` and `review` are deliberately absent -- see docs/issues/local-engine-order-vocabulary.md.
    """

    ARTIST = enum.auto()
    CMC = enum.auto()
    COLOR = enum.auto()
    CUBECOBRA = enum.auto()
    EDHREC = enum.auto()
    EUR = enum.auto()
    NAME = enum.auto()
    POWER = enum.auto()
    RARITY = enum.auto()
    RELEASED = enum.auto()
    SET = enum.auto()
    TIX = enum.auto()
    TOUGHNESS = enum.auto()
    USD = enum.auto()


class ResponseShape(enum.StrEnum):
    """Enum for the shape of the cards list in search responses."""

    ROWS = enum.auto()  # list of card objects (default)
    COLUMNAR = enum.auto()  # object mapping each field to a list of per-card values


class SortDirection(enum.StrEnum):
    """Enum for the direction of the sort.

    AUTO is resolved to ASC or DESC per ordering before any search path sees it (see
    AUTO_DIRECTIONS and `resolve_direction`), so neither the engine nor the SQL builder ever
    receives it.
    """

    ASC = enum.auto()
    DESC = enum.auto()
    AUTO = enum.auto()


# What `dir=auto` means for each ordering, measured against api.scryfall.com on 2026-08-09 by
# comparing the `auto` page against the `asc` and `desc` pages of the same query. Only these five
# invert; every other ordering, edhrec included, resolves ascending -- for edhrec that is the
# direction putting rank 1 first, so "most popular first" and "ascending rank" are the same thing.
AUTO_DESCENDING_ORDERINGS: frozenset[CardOrdering] = frozenset(
    {
        CardOrdering.RELEASED,
        CardOrdering.RARITY,
        CardOrdering.USD,
        CardOrdering.TIX,
        CardOrdering.EUR,
    },
)


def resolve_direction(direction: SortDirection, orderby: CardOrdering) -> SortDirection:
    """Resolve AUTO against an ordering, leaving an explicit direction alone.

    Args:
        direction: The requested direction, possibly AUTO.
        orderby: The ordering AUTO is being resolved against.

    Returns:
        ASC or DESC, never AUTO.
    """
    if direction is not SortDirection.AUTO:
        return direction
    return SortDirection.DESC if orderby in AUTO_DESCENDING_ORDERINGS else SortDirection.ASC
