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
        # `colour`/`colours` are Scryfall's British spellings and answer identically (`colour:wu e:khm`
        # = `c:wu e:khm` = 6, measured 2026-08-16). `color_identity`/`coloridentity` below are the
        # reverse case -- spellings THIS parser accepts and Scryfall does not -- and are left alone:
        # answering where Scryfall warns costs a searcher nothing, while removing them would break
        # queries that already work.
        search_aliases=["color", "colors", "colour", "colours", "c"],
        parser_class=ParserClass.COLOR,
    ),
    FieldInfo(
        db_column_name="card_color_identity",
        field_type=FieldType.JSONB_OBJECT,
        # `commander` is how a player actually searches a commander's colours, and it is plain colour
        # IDENTITY: `commander:wu e:khm` = `id:wu e:khm` = 117, and it takes the counts too
        # (`commander:m e:khm` = `commander>=2 e:khm` = 74). Scryfall's identity vocabulary is a
        # BOUNDARY -- `cid`, `commanderidentity`, `colouridentity` and `colour_identity` all come
        # back "Unknown keyword" -- so nothing else joins it.
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
        db_column_name="oracle_id",
        field_type=FieldType.TEXT,
        search_aliases=["oracleid", "oracle_id"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="oracle_text",
        field_type=FieldType.TEXT,
        # `fo:`/`fulloracle:` are Scryfall's FULL-oracle spellings and share this column: the
        # stored `oracle_text` IS the full text, reminder text included, so the SQL path answers
        # both from it and needs no second column. They are told apart downstream by
        # `original_attribute` -- which matters only to the card engine, whose searchable oracle
        # column has reminder text stripped out of it the way Scryfall's `o:` does.
        # Measured on api.scryfall.com 2026-08-16: `fo:lifelink` 713 / `o:lifelink` stripped,
        # `fo:draw e:khm` 57 / `o:draw e:khm` 39, `fo:/\(this creature/` 1,098 / `o:/\(/` 0.
        search_aliases=["oracle", "o", "fo", "fulloracle"],
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
        db_column_name="card_lang",
        field_type=FieldType.TEXT,
        search_aliases=["lang", "language"],
        parser_class=ParserClass.TEXT,
    ),
    FieldInfo(
        db_column_name="card_set_type",
        field_type=FieldType.TEXT,
        search_aliases=["set_type", "settype", "st"],
        parser_class=ParserClass.TEXT,
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

# The `is:` values derivable from a single boolean SQL expression against a card's own row,
# synced in one set-based statement after each import (see _sync_boolean_is_tags) -- no
# per-tag API sweep, unlike CUSTOM_IS_TAGS below, and no accumulation in the import loop.
# Each expression must reference the row alias `cards` (it runs inside a correlated
# subquery, not a plain WHERE) -- adding a tag here is the whole change. Most read
# `cards.raw_card_blob`; hybrid/phyrexian read `cards.mana_cost_text` instead, per
# docs/issues/done/00713-is-tag-recovery.md's own reasoning for putting them here rather
# than in the query-rewrite table: the DSL only does exact-symbol containment, so a
# rewrite would be a brittle ~15-term OR over an open, growing symbol set. Density-gated
# at ~2% of the corpus (see docs/issues/00985): reserved (1.1%) and gamechanger (0.4%)
# were the original two; the rest were added after a corpus-wide survey of every is: tag
# on Scryfall's syntax page found these sitting at or under masterpiece's 1.8%.
# foil/nonfoil/reprint/booster/hires (50-97%) are deliberately NOT here -- "higher
# cardinality, memory check first" -- and stay a candidate for a separate, more careful
# pass -- except for the three below, whose memory question is now ANSWERED rather than
# assumed: the Cloudflare port's builder ran twice over the same corpus (2026-08-16 all_cards,
# 31,724 cards / 517,746 rows) and the archives total 363.02 MiB without foil/promo/reprint and
# 364.17 MiB with them, +1.16 MiB / +0.32%, because a value carried by that share of the corpus
# stores as a bitmap plane rather than a posting list.
#
# It lives HERE rather than in admin_resource because the parser reads it too: the keys are half of
# `rewrite.SUPPORTED_IS_VALUES`, the complete list of `is:` values this parser can answer, and
# api/parsing cannot import api/admin_resource.
#
# The vocabulary was hand-kept from Scryfall's SYNTAX PAGE, which documents about half of what its
# search accepts: is:serialized (292 cards on api.scryfall.com, 2026-09-03), is:surgefoil (1,584),
# is:setpromo (1,381), is:promopack (2,599), is:galaxyfoil (283), is:textured (92),
# is:stepandcompleat (68) and the Final Fantasy family appear nowhere on it, and every one of them
# was a warned no-match here. So on 2026-09-03 it was ENUMERATED instead: all 73,480 printings that
# can carry `promo_types` were paged from api.scryfall.com (`-is:booster` and `is:boosterfun`, extras
# and variations included), giving 115 distinct members; unioned with the page's 92 `is:` values that
# made 221 candidates, each one outside SUPPORTED_IS_VALUES was probed as `is:<value>`, and 78 came
# back a 200 -- every row below that is a promo_types member of its own name is one of those. The
# candidates Scryfall itself REJECTS as `is:` values (`acorn`, `oval`, `triangle`, `arena`, `circle`,
# `snow`, `devoid`, `legendary`, `inverted`, `lesson`, `enchantment` and the DFC frame effects) are
# deliberately absent: they are frame_effects/security_stamp members that `frame:`/`stamp:` reach,
# and a row would answer where Scryfall refuses. Every new row is sparse -- the largest, promopack,
# is 2,599 cards -- so none of them reopens the density question above.
BOOLEAN_IS_TAGS: dict[str, str] = {
    # Alphabetized by key. Expressions read either a plain top-level boolean (reserved,
    # gamechanger, spotlight), promo_types/keywords/finishes array membership, or a
    # single-field lookup (set_type, preview.source).
    "arena_league": "cards.raw_card_blob->'promo_types' @> '\"arenaleague\"'",
    "beginnerbox": "cards.raw_card_blob->'promo_types' @> '\"beginnerbox\"'",
    "booster": "cards.raw_card_blob->'booster' = 'true'::jsonb",
    "boosterfun": "cards.raw_card_blob->'promo_types' @> '\"boosterfun\"'",
    "boxtopper": "cards.raw_card_blob->'promo_types' @> '\"boxtopper\"'",
    "brawldeck": "cards.raw_card_blob->'promo_types' @> '\"brawldeck\"'",
    "bringafriend": "cards.raw_card_blob->'promo_types' @> '\"bringafriend\"'",
    "bundle": "cards.raw_card_blob->'promo_types' @> '\"bundle\"'",
    "buyabox": "cards.raw_card_blob->'promo_types' @> '\"buyabox\"'",
    "chocobotrackfoil": "cards.raw_card_blob->'promo_types' @> '\"chocobotrackfoil\"'",
    "commanderparty": "cards.raw_card_blob->'promo_types' @> '\"commanderparty\"'",
    "commanderpromo": "cards.raw_card_blob->'promo_types' @> '\"commanderpromo\"'",
    "concept": "cards.raw_card_blob->'promo_types' @> '\"concept\"'",
    "confettifoil": "cards.raw_card_blob->'promo_types' @> '\"confettifoil\"'",
    "convention": "cards.raw_card_blob->'promo_types' @> '\"convention\"'",
    "cosmicfoil": "cards.raw_card_blob->'promo_types' @> '\"cosmicfoil\"'",
    "datestamped": "cards.raw_card_blob->'promo_types' @> '\"datestamped\"'",
    "dazzlefoil": "cards.raw_card_blob->'promo_types' @> '\"dazzlefoil\"'",
    "digital": "cards.raw_card_blob->'digital' = 'true'::jsonb",
    "dossier": "cards.raw_card_blob->'promo_types' @> '\"dossier\"'",
    "doubleexposure": "cards.raw_card_blob->'promo_types' @> '\"doubleexposure\"'",
    "doublerainbow": "cards.raw_card_blob->'promo_types' @> '\"doublerainbow\"'",
    "draculaseries": "cards.raw_card_blob->'promo_types' @> '\"draculaseries\"'",
    "draftweekend": "cards.raw_card_blob->'promo_types' @> '\"draftweekend\"'",
    "dragonscalefoil": "cards.raw_card_blob->'promo_types' @> '\"dragonscalefoil\"'",
    "duels": "cards.raw_card_blob->'promo_types' @> '\"duels\"'",
    "embossed": "cards.raw_card_blob->'promo_types' @> '\"embossed\"'",
    "etched": "cards.raw_card_blob->'finishes' @> '\"etched\"'",
    "event": "cards.raw_card_blob->'promo_types' @> '\"event\"'",
    "facetfoil": "cards.raw_card_blob->'promo_types' @> '\"facetfoil\"'",
    "ffi": "cards.raw_card_blob->'promo_types' @> '\"ffi\"'",
    "ffii": "cards.raw_card_blob->'promo_types' @> '\"ffii\"'",
    "ffiii": "cards.raw_card_blob->'promo_types' @> '\"ffiii\"'",
    "ffiv": "cards.raw_card_blob->'promo_types' @> '\"ffiv\"'",
    "ffix": "cards.raw_card_blob->'promo_types' @> '\"ffix\"'",
    "ffv": "cards.raw_card_blob->'promo_types' @> '\"ffv\"'",
    "ffvi": "cards.raw_card_blob->'promo_types' @> '\"ffvi\"'",
    "ffvii": "cards.raw_card_blob->'promo_types' @> '\"ffvii\"'",
    "ffviii": "cards.raw_card_blob->'promo_types' @> '\"ffviii\"'",
    # Final Fantasy X, and established the way every row below was: `is:ffx` is 120 cards / 170
    # printings on api.scryfall.com (2026-09-03), and intersecting the `promo_types` of all 170 leaves
    # `ffx` and `universesbeyond`. The second is the wider set every Universes Beyond printing
    # carries and is already a row here; `ffx` is the discriminating member.
    "ffx": "cards.raw_card_blob->'promo_types' @> '\"ffx\"'",
    "ffxi": "cards.raw_card_blob->'promo_types' @> '\"ffxi\"'",
    "ffxii": "cards.raw_card_blob->'promo_types' @> '\"ffxii\"'",
    "ffxiii": "cards.raw_card_blob->'promo_types' @> '\"ffxiii\"'",
    "ffxiv": "cards.raw_card_blob->'promo_types' @> '\"ffxiv\"'",
    "ffxv": "cards.raw_card_blob->'promo_types' @> '\"ffxv\"'",
    "ffxvi": "cards.raw_card_blob->'promo_types' @> '\"ffxvi\"'",
    "firstplacefoil": "cards.raw_card_blob->'promo_types' @> '\"firstplacefoil\"'",
    "fnm": "cards.raw_card_blob->'promo_types' @> '\"fnm\"'",
    "foil": "cards.raw_card_blob->'foil' = 'true'::jsonb",
    "fracturefoil": "cards.raw_card_blob->'promo_types' @> '\"fracturefoil\"'",
    "fullart": "cards.raw_card_blob->'full_art' = 'true'::jsonb",
    "galaxyfoil": "cards.raw_card_blob->'promo_types' @> '\"galaxyfoil\"'",
    "gamechanger": "cards.raw_card_blob->'game_changer' = 'true'::jsonb",
    "gameday": "cards.raw_card_blob->'promo_types' @> '\"gameday\"'",
    "giftbox": "cards.raw_card_blob->'promo_types' @> '\"giftbox\"'",
    "gilded": "cards.raw_card_blob->'promo_types' @> '\"gilded\"'",
    "gleaminggold": "cards.raw_card_blob->'promo_types' @> '\"gleaminggold\"'",
    "glossy": "cards.raw_card_blob->'promo_types' @> '\"glossy\"'",
    "godzillaseries": "cards.raw_card_blob->'promo_types' @> '\"godzillaseries\"'",
    "halofoil": "cards.raw_card_blob->'promo_types' @> '\"halofoil\"'",
    "headliner": "cards.raw_card_blob->'promo_types' @> '\"headliner\"'",
    "hires": "cards.raw_card_blob->'highres_image' = 'true'::jsonb",
    # Matches color/color, 2/color, colorless/color, and color/color/phyrexian.
    "hybrid": r"cards.mana_cost_text ~ '\{[2CWUBRG]/[WUBRG]'",
    "imagine": "cards.raw_card_blob->'promo_types' @> '\"imagine\"'",
    "instore": "cards.raw_card_blob->'promo_types' @> '\"instore\"'",
    "intro_pack": "cards.raw_card_blob->'promo_types' @> '\"intropack\"'",
    "invisibleink": "cards.raw_card_blob->'promo_types' @> '\"invisibleink\"'",
    "japanshowcase": "cards.raw_card_blob->'promo_types' @> '\"japanshowcase\"'",
    "jpwalker": "cards.raw_card_blob->'promo_types' @> '\"jpwalker\"'",
    "judge_gift": "cards.raw_card_blob->'promo_types' @> '\"judgegift\"'",
    "league": "cards.raw_card_blob->'promo_types' @> '\"league\"'",
    "magnified": "cards.raw_card_blob->'promo_types' @> '\"magnified\"'",
    "manafoil": "cards.raw_card_blob->'promo_types' @> '\"manafoil\"'",
    "masterpiece": "cards.raw_card_blob->>'set_type' = 'masterpiece'",
    "media_insert": "cards.raw_card_blob->'promo_types' @> '\"mediainsert\"'",
    # The meld ROLE is the `component` of this card's OWN entry in its `all_parts` array -- every
    # meld card carries all three entries (two parts, one result), so `layout:meld` says the card
    # melds and nothing about which side it is, and reading any entry but the card's own would tag
    # all three the same. 14 parts and 7 results on api.scryfall.com (2026-09-03), two parts per
    # result; both answered 0 here before this.
    "meldpart": "EXISTS (SELECT 1 FROM jsonb_array_elements(cards.raw_card_blob->'all_parts') part WHERE part->>'id' = cards.raw_card_blob->>'id' AND part->>'component' = 'meld_part')",
    "meldresult": "EXISTS (SELECT 1 FROM jsonb_array_elements(cards.raw_card_blob->'all_parts') part WHERE part->>'id' = cards.raw_card_blob->>'id' AND part->>'component' = 'meld_result')",
    "neonink": "cards.raw_card_blob->'promo_types' @> '\"neonink\"'",
    # "Partner with <name>" cards carry a plain "Partner" keyword alongside it (verified
    # against the corpus), so checking for "Partner" alone already covers both.
    "nonfoil": "cards.raw_card_blob->'nonfoil' = 'true'::jsonb",
    "oilslick": "cards.raw_card_blob->'promo_types' @> '\"oilslick\"'",
    "openhouse": "cards.raw_card_blob->'promo_types' @> '\"openhouse\"'",
    "partner": "cards.raw_card_blob->'keywords' @> '\"Partner\"'",
    # Search for `/P}` in mana costs and oracle texts.
    "phyrexian": r"(cards.mana_cost_text ~ '/P\}' OR cards.oracle_text ~ '/P\}')",
    "planeswalker_deck": "cards.raw_card_blob->'promo_types' @> '\"planeswalkerdeck\"'",
    "player_rewards": "cards.raw_card_blob->'promo_types' @> '\"playerrewards\"'",
    "playpromo": "cards.raw_card_blob->'promo_types' @> '\"playpromo\"'",
    "portrait": "cards.raw_card_blob->'promo_types' @> '\"portrait\"'",
    "poster": "cards.raw_card_blob->'promo_types' @> '\"poster\"'",
    "prerelease": "cards.raw_card_blob->'promo_types' @> '\"prerelease\"'",
    "promo": "cards.raw_card_blob->'promo' = 'true'::jsonb",
    "promopack": "cards.raw_card_blob->'promo_types' @> '\"promopack\"'",
    "rainbowfoil": "cards.raw_card_blob->'promo_types' @> '\"rainbowfoil\"'",
    "raisedfoil": "cards.raw_card_blob->'promo_types' @> '\"raisedfoil\"'",
    "ravnicacity": "cards.raw_card_blob->'promo_types' @> '\"ravnicacity\"'",
    "rebalanced": "cards.raw_card_blob->'promo_types' @> '\"rebalanced\"'",
    "release": "cards.raw_card_blob->'promo_types' @> '\"release\"'",
    "reprint": "cards.raw_card_blob->'reprint' = 'true'::jsonb",
    "resale": "cards.raw_card_blob->'promo_types' @> '\"resale\"'",
    "reserved": "cards.raw_card_blob->'reserved' = 'true'::jsonb",
    "ripplefoil": "cards.raw_card_blob->'promo_types' @> '\"ripplefoil\"'",
    "scroll": "cards.raw_card_blob->'promo_types' @> '\"scroll\"'",
    "scryfallpreview": "cards.raw_card_blob->'preview'->>'source' = 'Scryfall'",
    "serialized": "cards.raw_card_blob->'promo_types' @> '\"serialized\"'",
    "set_promo": "cards.raw_card_blob->'promo_types' @> '\"setpromo\"'",
    "silverfoil": "cards.raw_card_blob->'promo_types' @> '\"silverfoil\"'",
    "silverscroll": "cards.raw_card_blob->'promo_types' @> '\"silverscroll\"'",
    "sldbonus": "cards.raw_card_blob->'promo_types' @> '\"sldbonus\"'",
    "sourcematerial": "cards.raw_card_blob->'promo_types' @> '\"sourcematerial\"'",
    "spotlight": "cards.raw_card_blob->'story_spotlight' = 'true'::jsonb",
    "stamped": "cards.raw_card_blob->'promo_types' @> '\"stamped\"'",
    "standardshowdown": "cards.raw_card_blob->'promo_types' @> '\"standardshowdown\"'",
    "startercollection": "cards.raw_card_blob->'promo_types' @> '\"startercollection\"'",
    "starterdeck": "cards.raw_card_blob->'promo_types' @> '\"starterdeck\"'",
    "stepandcompleat": "cards.raw_card_blob->'promo_types' @> '\"stepandcompleat\"'",
    "storechampionship": "cards.raw_card_blob->'promo_types' @> '\"storechampionship\"'",
    "surgefoil": "cards.raw_card_blob->'promo_types' @> '\"surgefoil\"'",
    "textless": "cards.raw_card_blob->'textless' = 'true'::jsonb",
    "textured": "cards.raw_card_blob->'promo_types' @> '\"textured\"'",
    "thick": "cards.raw_card_blob->'promo_types' @> '\"thick\"'",
    "tourney": "cards.raw_card_blob->'promo_types' @> '\"tourney\"'",
    "universesbeyond": "cards.raw_card_blob->'promo_types' @> '\"universesbeyond\"'",
    "upsidedown": "cards.raw_card_blob->'promo_types' @> '\"upsidedown\"'",
    "variation": "cards.raw_card_blob->'variation' = 'true'::jsonb",
    "vault": "cards.raw_card_blob->'promo_types' @> '\"vault\"'",
    "wizardsplaynetwork": "cards.raw_card_blob->'promo_types' @> '\"wizardsplaynetwork\"'",
}
