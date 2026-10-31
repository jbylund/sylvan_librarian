"""Database field information and mappings for Scryfall queries."""

from __future__ import annotations

from enum import StrEnum


class FieldType(StrEnum):
    """Enumeration of supported database field types."""

    JSONB_ARRAY = "jsonb_array"
    JSONB_OBJECT = "jsonb_object"
    NUMERIC = "numeric"
    TEXT = "text"
    DATE = "date"


class ParserClass(StrEnum):
    """Enumeration of parser classes for different field types."""

    NUMERIC = "numeric"  # Supports arithmetic operations (cmc, power, etc.)
    MANA = "mana"  # Mana cost fields with special mana value parsing
    RARITY = "rarity"  # Rarity fields with string-to-numeric conversion
    LEGALITY = "legality"  # Format/legal fields with JSON handling
    COLOR = "color"  # Color fields (card colors and color identity)
    TEXT = "text"  # Simple text fields (name, artist, oracle text)
    DATE = "date"  # Date fields with full date values
    YEAR = "year"  # Year fields with 4-digit year values
    CURRENCY = "currency"  # `cheapest:` -- the value is one of a closed set of currency words
    NEW = "new"  # `new:` -- the value is one of a closed set of words; see NEW_KEYWORD_VALUES


class FieldInfo:
    """Information about a database field and its search aliases."""

    def __init__(self, *, db_column_name: str, field_type: FieldType, search_aliases: list[str], parser_class: ParserClass) -> None:
        """Initialize field information.

        Args:
            db_column_name: The actual database column name.
            field_type: The type of the field.
            search_aliases: List of search aliases for this field.
            parser_class: The parser class to use for this field. If None, defaults based on field_type.
        """
        self.db_column_name = db_column_name
        self.field_type = field_type
        self.search_aliases = search_aliases
        # Default parser class based on field type if not specified
        if parser_class is None:
            parser_class = ParserClass.NUMERIC if field_type == FieldType.NUMERIC else ParserClass.TEXT
        self.parser_class = parser_class

    def __repr__(self: FieldInfo) -> str:
        """Return a string representation of the field info."""
        return (
            "FieldInfo("
            f"db_column_name={self.db_column_name}, "
            f"field_type={self.field_type}, "
            f"search_aliases={self.search_aliases}, "
            f"parser_class={self.parser_class}"
            ")"
        )


DB_COLUMNS = [
    FieldInfo(
        db_column_name="card_artist",
        field_type=FieldType.TEXT,
        search_aliases=["artist", "a"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_colors",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["color", "colors", "colour", "colours", "c"],
        parser_class=ParserClass.COLOR,
    ),
    FieldInfo(
        db_column_name="card_color_identity",
        field_type=FieldType.JSONB_OBJECT,
        # `commander:` is how players search a commander's colour identity -- a deck built
        # around it must stay within it, so a commander query is a color-identity query.
        search_aliases=["color_identity", "coloridentity", "id", "identity", "ci", "commander"],
        parser_class=ParserClass.COLOR,
    ),
    FieldInfo(
        db_column_name="card_frame_data",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["frame"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_keywords",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["keyword", "kw"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_name",
        field_type=FieldType.TEXT,
        search_aliases=["name"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_subtypes",
        field_type=FieldType.JSONB_ARRAY,
        search_aliases=["subtype", "subtypes"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_types",
        field_type=FieldType.JSONB_ARRAY,
        search_aliases=["type", "types", "t"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="cmc",
        field_type=FieldType.NUMERIC,
        search_aliases=["cmc", "mv", "manavalue"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="creature_power",
        field_type=FieldType.NUMERIC,
        search_aliases=["power", "pow"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="creature_toughness",
        field_type=FieldType.NUMERIC,
        search_aliases=["toughness", "tou"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="planeswalker_loyalty",
        field_type=FieldType.NUMERIC,
        search_aliases=["loyalty", "loy"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="edhrec_rank",
        field_type=FieldType.NUMERIC,
        search_aliases=[],
        parser_class=ParserClass.NUMERIC,
    ),
    # Scryfall's six COUNT keywords (measured on api.scryfall.com 2026-10-03: Lightning Bolt is
    # `prints=77`, `sets=46`, `paperprints=68`, `papersets=41`, `illustrations=33`). Five are
    # counts over all of the card's printings and `artists` is the printing's own; none can be
    # computed from one row at query time, so each is a column _sync_print_counts
    # (api/admin_resource.py) writes at import. Ordinary numeric columns from here on.
    FieldInfo(
        db_column_name="card_print_count",
        field_type=FieldType.NUMERIC,
        search_aliases=["prints"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="card_set_count",
        field_type=FieldType.NUMERIC,
        search_aliases=["sets"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="card_paper_print_count",
        field_type=FieldType.NUMERIC,
        search_aliases=["paperprints"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="card_paper_set_count",
        field_type=FieldType.NUMERIC,
        search_aliases=["papersets"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="card_illustration_count",
        field_type=FieldType.NUMERIC,
        search_aliases=["illustrations"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="artist_count",
        field_type=FieldType.NUMERIC,
        search_aliases=["artists"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="mana_cost_jsonb",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["mana", "m"],
        parser_class=ParserClass.MANA,
    ),
    FieldInfo(
        db_column_name="devotion",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["devotion"],
        parser_class=ParserClass.MANA,
    ),
    FieldInfo(
        db_column_name="price_usd",
        field_type=FieldType.NUMERIC,
        search_aliases=["usd"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="price_eur",
        field_type=FieldType.NUMERIC,
        search_aliases=["eur"],
        parser_class=ParserClass.NUMERIC,
    ),
    FieldInfo(
        db_column_name="price_tix",
        field_type=FieldType.NUMERIC,
        search_aliases=["tix"],
        parser_class=ParserClass.NUMERIC,
    ),
    # Scryfall's `cheapest:usd` / `cheapest:eur` / `cheapest:tix`: the printings carrying their
    # card's lowest price in that currency. The lowest price is over the card's OTHER printings,
    # which no row sees at query time, so the answers are decided at import and packed into one
    # smallint -- see CHEAPEST_TERM below and _build_cheapest_codes_sql in api/admin_resource.py.
    # A parser class of its own because the value is a closed vocabulary (CHEAPEST_CURRENCY_WORDS)
    # and because `-cheapest:usd` is NOT the complement of `cheapest:usd`; both parsers build a
    # CheapestNode for it rather than a generic comparison.
    FieldInfo(
        db_column_name="cheapest_codes",
        field_type=FieldType.NUMERIC,
        search_aliases=["cheapest"],
        parser_class=ParserClass.CURRENCY,
    ),
    # Scryfall's `new:<value>`: the printing is the first of its card with something -- at its rarity,
    # on paper, in its frame, in foil -- or the first anywhere with its artwork. "First" is over other
    # printings, which no row sees at query time, so the answers are decided at import and stored --
    # one boolean for `rarity`, one bit of `new_flags` for each other value; see NEW_KEYWORD_COLUMNS
    # and NEW_FLAG_BITS below and the two syncs in api/admin_resource.py. A parser class of its own
    # because the value is a closed vocabulary and only the measured part of Scryfall's is answered.
    # The FieldInfo names the first of the two columns; NewNode picks the one a value reads.
    FieldInfo(
        db_column_name="new_rarity",
        field_type=FieldType.NUMERIC,
        search_aliases=["new"],
        parser_class=ParserClass.NEW,
    ),
    FieldInfo(
        db_column_name="produced_mana",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["produces"],
        parser_class=ParserClass.COLOR,
    ),
    FieldInfo(
        db_column_name="raw_card_blob",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=[],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="oracle_text",
        field_type=FieldType.TEXT,
        search_aliases=["oracle", "o"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="flavor_text",
        field_type=FieldType.TEXT,
        search_aliases=["flavor", "ft"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_oracle_tags",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["oracle_tags", "otag", "oracletag", "function"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_art_tags",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["art_tags", "art", "atag", "arttag"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_is_tags",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["is", "has"],
        parser_class=ParserClass.TEXT,
    ),
    # A distinct FieldInfo from "is" above, sharing its db_column_name, so a `not:` leaf
    # generates the identical SQL/explanation as `is:` on its own -- rewrite.py's
    # negate_not_prefix distinguishes the two via original_attribute and supplies the
    # negation Scryfall's docs describe ("not: is the same as -is:").
    FieldInfo(
        db_column_name="card_is_tags",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["not"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_rarity_int",
        field_type=FieldType.NUMERIC,
        search_aliases=["rarity", "r"],
        parser_class=ParserClass.RARITY,
    ),
    FieldInfo(
        db_column_name="card_set_code",
        field_type=FieldType.TEXT,
        search_aliases=["set", "s", "e"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="collector_number",
        field_type=FieldType.TEXT,
        search_aliases=["number", "cn"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="collector_number_int",
        field_type=FieldType.NUMERIC,
        search_aliases=["number", "cn"],
        parser_class=ParserClass.NUMERIC,
    ),  # No direct aliases - will be routed
    FieldInfo(
        db_column_name="card_legalities",
        field_type=FieldType.JSONB_OBJECT,
        search_aliases=["format", "f", "legal", "banned", "restricted"],
        parser_class=ParserClass.LEGALITY,
    ),
    FieldInfo(
        db_column_name="card_layout",
        field_type=FieldType.TEXT,
        search_aliases=["layout"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_border",
        field_type=FieldType.TEXT,
        search_aliases=["border"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_watermark",
        field_type=FieldType.TEXT,
        search_aliases=["watermark", "wm"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="released_at",
        field_type=FieldType.DATE,
        search_aliases=["date"],
        parser_class=ParserClass.DATE,
    ),
    FieldInfo(
        db_column_name="released_at",
        field_type=FieldType.DATE,
        search_aliases=["year"],
        parser_class=ParserClass.YEAR,
    ),
]

KNOWN_CARD_ATTRIBUTES = set()
NUMERIC_CARD_ATTRIBUTES: set[str] = set()
SEARCH_NAME_TO_DB_NAME = {}

ALIAS_TO_FIELD_INFOS: dict[str, list[FieldInfo]] = {}
COLNAME_TO_FIELD_INFOS: dict[str, list[FieldInfo]] = {}
PARSER_CLASS_TO_FIELD_INFOS: dict[ParserClass, list[FieldInfo]] = {}

for col in DB_COLUMNS:
    for ialias in col.search_aliases:
        ALIAS_TO_FIELD_INFOS.setdefault(ialias.lower(), []).append(col)

    COLNAME_TO_FIELD_INFOS.setdefault(col.db_column_name, []).append(col)
    PARSER_CLASS_TO_FIELD_INFOS.setdefault(col.parser_class, []).append(col)

    KNOWN_CARD_ATTRIBUTES.add(col.db_column_name.lower())
    KNOWN_CARD_ATTRIBUTES.update(alias.lower() for alias in col.search_aliases)
    if col.parser_class == ParserClass.NUMERIC:
        NUMERIC_CARD_ATTRIBUTES.add(col.db_column_name.lower())
        NUMERIC_CARD_ATTRIBUTES.update(alias.lower() for alias in col.search_aliases)
    SEARCH_NAME_TO_DB_NAME[col.db_column_name.lower()] = col.db_column_name

    for ialias in col.search_aliases:
        SEARCH_NAME_TO_DB_NAME[ialias.lower()] = col.db_column_name


# The words Scryfall reads as a currency in `cheapest:<word>`, in any case (api.scryfall.com,
# 2026-10-04): `usd`, `$` and `dollar` are one currency, `eur`, `euro` and `€` another, `tix` and
# `mtgo` the third. `dollars`, `euros`, `ticket`, `tickets`, `usdfoil`, `eurfoil`, `tcgplayer`
# and `cardmarket` are each "Unknown currency" there.
CHEAPEST_CURRENCY_WORDS: dict[str, str] = {
    "usd": "usd",
    "$": "usd",
    "dollar": "usd",
    "eur": "eur",
    "euro": "eur",
    "€": "eur",
    "tix": "tix",
    "mtgo": "tix",
}

# The two of them that are not words: a bare `$` or `€` is a value only directly after `cheapest`
# and its operator. Both parsers read this, so neither accepts the symbol anywhere else.
CHEAPEST_CURRENCY_SYMBOLS: frozenset[str] = frozenset(word for word in CHEAPEST_CURRENCY_WORDS if not word.isalnum())

# `magic.cards.cheapest_codes` packs three bits per currency, at CHEAPEST_SHIFTS[currency]:
#
#   CHEAPEST_TERM          `cheapest:<currency>` is true of this printing
#   CHEAPEST_NEGATED_TERM  `-cheapest:<currency>` is true of it -- its own expression on Scryfall,
#                          not the complement, so it cannot be derived from the bit above
#   CHEAPEST_UNKNOWN       both are SQL NULL: the printing is priced and its card has no lowest
#                          price to compare with. Never set together with either bit above.
#
# The whole column is NULL until the sync has run over the card. card_engine/src/lib.rs carries
# the same three constants and shifts; test_engine_unit.py pins the two copies against each other.
CHEAPEST_TERM = 1
CHEAPEST_NEGATED_TERM = 2
CHEAPEST_UNKNOWN = 4
CHEAPEST_SHIFTS: dict[str, int] = {"usd": 0, "eur": 3, "tix": 6}


# `new:rarity` and the boolean column it reads -- the first `new:` value answered, measured
# 2026-10-04 (38,943 of 38,943 printings on api.scryfall.com) and synced by _sync_new_rarity.
NEW_KEYWORD_COLUMNS: dict[str, str] = {"rarity": "new_rarity"}

# The `new:` values answered from `magic.cards.new_flags`, and the bit of that smallint each reads.
# Each was measured to be exactly a list of printings on api.scryfall.com (2026-10-09, the whole
# list read with extras and variations in against that day's `default_cards`;
# _build_new_flags_sql in api/admin_resource.py carries the rule and the counts):
#
#   card     the card's first printing on paper                                   35,158 of 35,158
#   frame    the card's first printing in each frame (1993, 1997, 2003, 2015, future)   45,061 of 45,061
#   foil     the card's first paper printing in traditional foil                  29,671 of 29,671
#   nonfoil  the card's first paper printing in nonfoil                           35,018 of 35,018
#   art      the first printing anywhere of each artwork, across cards            52,064 of 52,064
#
# The bit numbers leave gaps on purpose. They are the positions the downstream port stores the same
# answers at, and the gaps are the values Scryfall honours that THIS table cannot answer, because
# the importer drops the printings they are decided over: bits 2-5 are `mtgo`, `arena`, `astral` and
# `sega` (a card's first printing in a game -- the digital printings are not imported), bit 8 is
# `flavor` (decided by a printing's FRONT face, and a two-faced printing is stored as one face, the
# last) and bit 10 is `language` (decided over every language's rows; only one row a printing is
# imported). A value takes its bit when the rows it needs arrive; until then it is refused like any
# unknown word, so no `new:` term returns a list that is not Scryfall's.
NEW_FLAG_BITS: dict[str, int] = {
    "card": 1 << 0,
    "frame": 1 << 1,
    "foil": 1 << 6,
    "nonfoil": 1 << 7,
    "art": 1 << 9,
}

# The other spellings Scryfall honours for the values above, each measured to be the same list as
# the value it names (2026-10-09): `new:paper` is `new:card` id for id over all 35,158 printings,
# and `printed`, `cardboard` and `illustration` return what their value does on every query tried.
NEW_KEYWORD_ALIASES: dict[str, str] = {
    "paper": "card",
    "printed": "card",
    "cardboard": "card",
    "illustration": "art",
}

# Every canonical `new:` value answered, in the order the error message lists them.
NEW_KEYWORD_VALUES: tuple[str, ...] = (*NEW_KEYWORD_COLUMNS, *NEW_FLAG_BITS)

# What each value's printing is the first of, for the human explanation of a query.
NEW_KEYWORD_EXPLANATIONS: dict[str, str] = {
    "rarity": "the printing is the first of its card at its rarity",
    "card": "the printing is the first of its card on paper",
    "frame": "the printing is the first of its card in its frame",
    "foil": "the printing is the first of its card in foil",
    "nonfoil": "the printing is the first of its card in nonfoil",
    "art": "the printing is the first with its artwork",
}


CARD_SUPERTYPES = {
    "Basic",
    "Legendary",
    "Snow",
    "World",
}

CARD_TYPES = {
    "Artifact",
    "Conspiracy",
    "Creature",
    "Enchantment",
    "Instant",
    "Kindred",  # new name for tribal
    "Land",
    "Planeswalker",
    "Sorcery",
    "Tribal",
}

FORMAT_CODE_TO_NAME = {
    "m": "modern",
    "s": "standard",
    "l": "legacy",
    "p": "pauper",
    "c": "commander",
    "v": "vintage",
    "h": "historic",
}
