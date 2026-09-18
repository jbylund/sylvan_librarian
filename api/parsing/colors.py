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

# The colour values that are a COUNT rather than a set of letters.
#
# `c:m` is not "the colour m" -- there is no such colour. It is Scryfall's word for MULTICOLOURED,
# and it compares the NUMBER of colours in the column, which is why it cannot live in
# COLOR_ALIAS_TO_CODES beside `azorius`: there are no letters to expand. `gold` and the
# `multicolor` spellings are the same value under other names; every one of the six answers the
# identical count (`c:m` = `c:gold` = `c:multicolor` = `c:multicolored` = `c:multicolour` =
# `c:multicoloured` = 44 in Kaldheim, where `c:2` = 43 and `c>=2` = 44).
#
# THE OPERATOR TABLE IS MEASURED, and it is not "substitute the number 2". Corpus-wide against
# api.scryfall.com, 2026-08-16:
#
#   c:m = c=m = c>m = c>=m = 4,607 = `c>=2`          (`c=2` is 3,811 and `c>2` is 796)
#   c<m = c!=m           = 29,049 = `c<2`            (`c!=2` is 29,836)
#   c<=m                 = 33,599 = EVERY CARD       (`c<=2` is 32,812)
#
# `>` is the surprise on the high side -- `c>m` is `c>=2`, not `c>2` -- and `!=` is the surprise on
# the low side: `c!=m` is `c<2`, the negation of "is multicoloured", NOT `c!=2`, which would also
# admit the 796 three-and-more-colour cards. `<=` is a tautology rather than `c<=2`, pinned against
# a second term so it cannot be read as "the whole corpus": `c<=m t:creature` = `t:creature`
# = `c<=5 t:creature` = 18,753 where `c<=2 t:creature` = 18,140.
#
# The identity spellings take the same table on their own column: `id:m` = `id=m` = `id>m` =
# `id>=m` = 5,831 = `id>=2`, `id<m` = `id!=m` = 27,768 = `id<2` (`id!=2` is 28,824), and
# `id<=m` = 33,599 = every card (`id<=2` is 32,543).
#
# `produces:` takes the same table, but over SIX values rather than five, and that asymmetry is
# measured rather than tidy: produced_mana is the one colour-ish column whose array can literally
# contain "C" (Sol Ring produces ["C"] while its colors and color_identity are both []). So
# `produces=6` = 106 = `produces:all` -- a count no five-key popcount can even reach -- the 481
# cards that produce colorless and nothing else answer `produces=1` rather than `produces=0`, the
# three producing exactly {C,W} land in `produces=2` and not `produces=1`, and counts 0..6 partition
# the corpus exactly (30,996 + 1,143 + 504 + 147 + 10 + 693 + 106 = 33,599). The colour columns must
# keep counting five: `c:all` = `c:wubrg` = `c=5` = 60, and `c=6` is not a valid query there at all
# ("Unknown color 6"). Both halves are pinned by tests so the asymmetry is not "fixed" later.
#
# `produces:m` = `produces=m` = `produces>m` = `produces>=m` = 1,460 = `produces>=2`
# (`produces=2` is 504), while `produces<m` = `produces!=m` = 1,143 = `produces=1` -- NOT
# `produces<2` (32,139), which sweeps in the cards that produce nothing -- and `produces<=m` =
# 2,603 = `produces>=1` rather than every card.
COLOR_COUNT_NAMES = frozenset(
    {
        "m",
        "gold",
        "multicolor",
        "multicolour",
        "multicolored",
        "multicoloured",
    }
)

# The colour values that are a count on ONE column only, as name -> the db column that accepts it.
#
# `any` is Scryfall's word for "produces some mana at all", and it is a produced_mana value and
# nothing else. It is NOT a globally valid colour name: on the colour columns Scryfall rejects it
# and IGNORES the term, which is a different answer from "match nothing" -- `c:any` on its own
# comes back "All of your terms were ignored", and `t:creature c:any` = `t:creature` = 18,753, the
# same for `id:any`. So the value parsers must resolve the name to THIS column before accepting it,
# and `c:any` / `id:any` stay the parse error they already were.
#
# THE OPERATOR TABLE IS MEASURED, corpus-wide against api.scryfall.com on 2026-08-28, and again
# against a `t:creature` second base so that no equality below can be an artifact of the corpus
# total; every one of them held on both:
#
#   produces:any = produces=any = produces>any = produces>=any = produces!=any
#                                        -> `produces>=1`   (corpus 2,603; t:creature 756)
#   produces<any                         -> `produces=0`    (corpus 30,996; t:creature 17,997)
#   produces<=any                        -> `produces<=1`   (corpus 32,139; t:creature 18,369)
#
# `!=` is the asymmetry worth stating out loud, because it does NOT read the way `m` does on this
# same column: `produces!=m` groups with `produces<m`, while `produces!=any` groups with
# `produces:any`. Both readings were measured, and they disagree; the tables below keep them apart.
#
# The three counts also fall exactly out of the 0..6 partition measured for COLOR_COUNT_NAMES above
# (30,996 + 1,143 + 504 + 147 + 10 + 693 + 106 = 33,599): `produces>=1` is 33,599 - 30,996 = 2,603
# and `produces<=1` is 30,996 + 1,143 = 32,139. The `t:creature` base closes the same way --
# 17,997 + 756 = 18,753 = `t:creature` -- so the two probe runs corroborate each other.
COUNT_NAME_TO_COLUMN = {
    "any": "produced_mana",
}


def count_name_rejected_for_column(value: str, db_column: str) -> bool:
    """Whether *value* is a count name only one column accepts, and *db_column* is not that column.

    Both front-end parsers ask this before accepting a colour word, so `produces:any` is a value and
    `c:any` / `id:any` are the parse error they were before `any` existed -- one predicate, so the
    two of them cannot come to different answers (test_parser_parity asserts they never do).
    """
    column = COUNT_NAME_TO_COLUMN.get(value.lower())
    return column is not None and column != db_column
