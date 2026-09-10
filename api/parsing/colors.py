"""Colour vocabulary: the letter codes and every name Scryfall's search accepts for them."""

COLOR_CODE_TO_NAME = {
    "b": "black",
    "c": "colorless",
    "g": "green",
    "r": "red",
    "u": "blue",
    "w": "white",
}

# Every colour NAME Scryfall's search accepts, as the letter set that name spells.
#
# The guild / shard / wedge vocabulary is what players actually type -- `c:azorius` is a normal
# thing to write and this parser answered it with a parse error -- so the whole table was measured
# rather than guessed, one request each against api.scryfall.com (`c:<value> e:khm`, 2026-08-16),
# and every accepted name then checked against its letter spelling over the WHOLE corpus. Kaldheim
# holds exactly one card of three colours or more, so a set-scoped check would have agreed with
# almost any mapping; the corpus-wide pairs are the ones that pin it: `c:bant` = `c:gwu` = 153,
# `c:esper` = `c:wub` = 146, `c:yore-tiller` = `c:wubr` = 62, `c:witch-maw` = `c:gwub` = 63,
# `c:rainbow` = `c:wubrg` = 60, `c:brown` = `c:c` = 4,300, and so on for all 24 pairs.
#
# It is a BOUNDARY rather than a superset. `yore`, `glint`, `dune`, `ink` and `witch` on their own
# come back "Unknown color ..." -- the un-hyphenated four-colour nicknames are NOT in Scryfall's
# table, only the hyphenated forms and the five one-word synonyms are -- and so do `five`, `mono`,
# `guild`, `shard`, `wedge`, `nephilim` and `chromatic`.
#
# `all` spells `wubrgc` where `rainbow` spells `wubrg`, and the difference is measured rather than
# cosmetic: for card_colors and card_color_identity the `c` drops out, so `c:all` = `c:wubrg` = 60,
# while for produced_mana it does not, so `produces:all` matches nothing (no card produces all six)
# where `produces:rainbow` = `produces:wubrg` = 13. One table serves all three columns because
# `get_colors_comparison_object` already draws exactly that line. It also fully replaces the old
# name -> single-letter-code table (`white` -> `w`, etc.), so there is only one lookup path.
COLOR_ALIAS_TO_CODES = {
    # the five colours, colourless, and the British and slang spellings of the latter
    "white": "w",
    "blue": "u",
    "black": "b",
    "red": "r",
    "green": "g",
    "colorless": "c",
    "colourless": "c",
    "brown": "c",
    # the ten Ravnica guilds
    "azorius": "wu",
    "dimir": "ub",
    "rakdos": "br",
    "gruul": "rg",
    "selesnya": "gw",
    "orzhov": "wb",
    "izzet": "ur",
    "golgari": "bg",
    "boros": "rw",
    "simic": "gu",
    # the five Strixhaven colleges -- verified live the same way, corpus-wide: c:lorehold =
    # c:rw = 682, c:prismari = c:ur = 668, c:quandrix = c:gu = 638, c:silverquill = c:wb = 614,
    # c:witherbloom = c:bg = 606.
    "lorehold": "rw",
    "prismari": "ur",
    "quandrix": "gu",
    "silverquill": "wb",
    "witherbloom": "bg",
    # the five Alara shards
    "bant": "gwu",
    "esper": "wub",
    "grixis": "ubr",
    "jund": "brg",
    "naya": "rgw",
    # the five Khans wedges
    "abzan": "wbg",
    "jeskai": "urw",
    "sultai": "bgu",
    "mardu": "rwb",
    "temur": "gur",
    # the five four-colour names, hyphenated (the Nephilim) and as one word
    "yore-tiller": "wubr",
    "glint-eye": "ubrg",
    "dune-brood": "brgw",
    "ink-treader": "rgwu",
    "witch-maw": "gwub",
    "artifice": "wubr",
    "chaos": "ubrg",
    "aggression": "brgw",
    "altruism": "rgwu",
    "growth": "gwub",
    # all five colours, and all six values
    "rainbow": "wubrg",
    "all": "wubrgc",
}


_COLOR_LETTERS = frozenset("wubrgc")


def is_valid_color_value(value: str) -> bool:
    """True for a colour name Scryfall accepts or a non-empty string of colour letters (`wubrgc`).

    The one vocabulary check both parsers apply to a colour value, quoted or bare, so `c:"xyz"` is a
    parse error rather than an engine failure at serialisation time (get_colors_comparison_object
    raises the same way, but from inside the query engine, after the parser said yes).
    """
    value = value.strip().lower()
    return value in COLOR_ALIAS_TO_CODES or (bool(value) and all(c in _COLOR_LETTERS for c in value))
