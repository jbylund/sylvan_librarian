"""Enums for the API."""

import enum


class UniqueOn(enum.StrEnum):
    """Enum for the distinct on column for the search."""

    CARD = enum.auto()
    PRINTING = enum.auto()
    ARTWORK = enum.auto()


class PreferOrder(enum.StrEnum):
    """Enum for the prefer order column for the search.

    Every `prefer:` Scryfall's syntax page lists (read 2026-09-02), under this API's underscored
    spellings; `api_resource._DIRECTIVE_PREFER` carries Scryfall's own. DEFAULT is "no
    preference" -- the engine's own printing order, which reproduces what Scryfall returns when no
    `prefer:` is written. DEFAULT_FRAME is Scryfall's `prefer:default` ("the default Magic frame"),
    which is NOT the same thing: on api.scryfall.com the two differ on exactly the cards whose
    no-prefer printing is an atypical frame (Chaos Warp's future-frame mbc/72, 1 of 450 measured),
    which `prefer:default` demotes and a bare query does not. ATYPICAL is its complement; the
    measured class lives on the engine's `PreferClassIds` (PR #912).
    """

    DEFAULT = enum.auto()
    OLDEST = enum.auto()
    NEWEST = enum.auto()
    USD_LOW = enum.auto()
    USD_HIGH = enum.auto()
    EUR_LOW = enum.auto()
    EUR_HIGH = enum.auto()
    TIX_LOW = enum.auto()
    TIX_HIGH = enum.auto()
    PROMO = enum.auto()
    DEFAULT_FRAME = enum.auto()
    ATYPICAL = enum.auto()
    UNIVERSESBEYOND = enum.auto()
    NOTUNIVERSESBEYOND = enum.auto()
    # This API's own, not Scryfall's: the best-looking printing that is still this card -- over
    # the printings with no flavor name, in-universe above Universes Beyond, and within each
    # borderless, then any other frame variant, then plain, then textless; English above every
    # other language; a flavor-named (crossover) printing is never a candidate. See the engine's
    # `Prefer`.
    BORDERLESS = enum.auto()


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
