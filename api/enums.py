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
    """Enum for the ordering of the cards."""

    CMC = enum.auto()
    CUBECOBRA = enum.auto()
    EDHREC = enum.auto()
    NAME = enum.auto()
    POWER = enum.auto()
    RARITY = enum.auto()
    TOUGHNESS = enum.auto()
    USD = enum.auto()


class ResponseShape(enum.StrEnum):
    """Enum for the shape of the cards list in search responses."""

    ROWS = enum.auto()  # list of card objects (default)
    COLUMNAR = enum.auto()  # object mapping each field to a list of per-card values


class SortDirection(enum.StrEnum):
    """Enum for the direction of the sort."""

    ASC = enum.auto()
    DESC = enum.auto()
