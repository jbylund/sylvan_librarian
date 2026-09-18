"""Scryfall's "ignore what you cannot honor" query policy, for the compat surface only.

WHAT SCRYFALL DOES
------------------

Scryfall's search does not reject a query because one term in it is unusable. It DROPS that term,
records a warning naming it, and answers with whatever survives - and it 400s only when NOTHING
survives. Measured against api.scryfall.com on 2026-08-16, one request per row::

    q=f:notaformat e:khm   200, 323 rows, warnings:["Invalid expression “f:notaformat” was
                           ignored. Unknown game format “notaformat”"]
    q=f:notaformat         400 bad_request, details "All of your terms were ignored.", the same
                           warnings array
    q=subtype:elf e:war    200, 266 rows (the whole set) + "Unknown keyword “subtype”."
    q=(subtype:elf or subtype:goblin) e:war   200, 266 - a group whose every arm was dropped is
                           itself dropped
    q=()                   400 "All of your terms were ignored."

That single mechanism is the root cause of eight separate divergences this surface carried: it
400d on a dangling operator, 404d on an unknown format or language, raised on a malformed regex,
and answered a NARROWER result than Scryfall wherever this project's vocabulary is a superset of
Scryfall's (``subtype:``, ``types:``, ``oracle_tags:``, ``art_tags:``, negated numeric equality).

WHY IT LIVES ON THE COMPAT SURFACE AND NOT IN THE PARSER
--------------------------------------------------------

Because the two surfaces answer to different vocabularies, and only one of them is Scryfall's.
``subtype:``, ``types:``, ``oracle_tags:`` and ``art_tags:`` are this project's own spellings; the
native ``/search`` API and the web UI use them, and deleting them from the parser to match Scryfall
would remove working features from our own API to mirror an API that never had them. Scryfall has
the same predicates under different names (``otag:``, ``atag:``), which this parser also accepts,
so on ``/cards/search`` the Scryfall spelling works and the local-only spelling is
ignored-and-warned exactly as Scryfall does, while ``/search`` keeps the whole vocabulary. One
parser, two policies, and the policy is a route-layer concept because "what Scryfall's API accepts"
is a route-layer fact.

HOW
---

The policy runs on the RAW query text, before parsing, for the same reason Scryfall's must: a term
the parser cannot lex at all (``t:`` with no value, ``cmc>=notanumber``, ``o:/[unclosed/``) has to
be removed before the parse, not after it. The scan is quote-, regex-, brace- and paren-aware,
drops the terms the tables below name, and rebuilds the query from the spans it kept, so a query
with nothing to ignore comes back BYTE-IDENTICAL to its input (modulo the typographic-quote fold),
which is the property that keeps this off every ordinary search's conscience.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from api.parsing.db_info import ALIAS_TO_FIELD_INFOS

# The four characters Scryfall folds before lexing, and the only four.
#
# Measured by putting each candidate around a phrase and asking whether the phrase searched as one
# term (`o:Xdraw a cardX` -> 2,544 rows means X delimits a string): U+2018/U+2019 fold to the ASCII
# apostrophe and U+201C/U+201D to the ASCII double quote. Every other quotation-shaped character
# stays literal and matches nothing -- the guillemets (U+00AB/BB, U+2039/203A), the low-9 pair
# (U+201E, U+201A), the primes (U+2032, U+2033, U+2035), the fullwidth quotes (U+FF02, U+FF07),
# the CJK brackets (U+300C..U+300F), the ornate pairs (U+275B..U+275E), backtick, acute, U+02BC.
#
# The fold is a CHARACTER substitution over the whole query, not a rule about quoted regions:
# `name:"Gaea<U+2019>s Blessing"` finds Gaea's Blessing, which it could not if the curly
# apostrophe were left alone inside the double quotes, and `name:<U+2018>Gaea"s Blessing<U+2019>`
# finds nothing, which is what `name:'Gaea"s Blessing'` does. Both directions had to be measured,
# because folding all four to
# `"` fits the first observation and fails the second.
#
# Users paste curly quotes constantly -- every word processor and phone keyboard produces them --
# so this is the single highest-traffic row in the file.
_SMART_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})

# Keywords this parser accepts that Scryfall's search does not know at all.
#
# Measured one request each (`<alias>:<plausible value> e:war`, 2026-08-16): every OTHER alias in
# DB_COLUMNS came back honored, and these came back with "Unknown keyword". They are exactly the
# local-only spellings -- Scryfall reaches the same three columns as `t:`/`otag:`/`atag:`.
_NOT_SCRYFALL_KEYWORDS = frozenset({"subtype", "subtypes", "types", "color_identity", "coloridentity", "oracle_tags", "art_tags"})

# Keywords SCRYFALL knows and this project does not -- left to fail as they already do.
#
# The rule below ignores any keyword neither side knows (`nonsense:value`, which Scryfall answers
# with "Unknown keyword" and a 400 rather than a parse error). These are the exception: ignoring one
# would answer a WIDER result than Scryfall, silently, because Scryfall honors it. Pretending to
# have dropped a term Scryfall applied is worse than saying the query could not be read.
_SCRYFALL_ONLY_KEYWORDS = frozenset({"game", "in", "cube", "new", "not", "stamp", "cheapest", "include", "direct"})

# The in-query directives (#893). Listed rather than imported so this file behaves the same before
# and after that branch lands: Scryfall honors `unique:prints`, `order:cmc`, `dir:desc` and
# `prefer:oldest` inside `q`, so none of them may ever be ignored here.
_DIRECTIVE_KEYWORDS = frozenset({"unique", "order", "sort", "dir", "direction", "prefer"})

# Scryfall cannot express a NEGATED numeric EQUALITY, and says so in two different sentences.
#
# Measured (`-<kw>:<value>` alone, so the answer is the 400 that carries the whole warning):
# `-cmc:3`, `-mv:3`, `-manavalue:3` earn the value sentence; `-pow:1`, `-power:1`, `-tou:1`,
# `-toughness:1`, `-loy:3`, `-loyalty:3`, `-usd:0`, `-eur:0`, `-tix:0`, `-year:1993` earn "Unknown
# keyword" WITH THE MINUS INSIDE THE QUOTES. `-cn:1` and `-number:1` are honored -- `cn:` is the
# STRING collector-number column, and only its integer twin `cn>=` is caught by the rule below -- so
# this table is equality-and-these-columns rather than negation as such.
#
# THIS COMMENT USED TO CLAIM `-date:2021` AND `-cmc!=3` WERE HONORED TOO. Both claims were wrong,
# and re-measuring them is what produced the two tables below: `-cmc!=3 e:khm t:creature` is 151,
# the unfiltered anchor, where `cmc!=3` is 106; and `-date:2021` is 141, exactly what the UNNEGATED
# `date:2021` answers. Neither is honored; they are simply quiet about it, which is why a comment
# could carry the error.
_NEGATED_EQUALITY_UNKNOWN_KEYWORD = frozenset({"pow", "power", "tou", "toughness", "loy", "loyalty", "usd", "eur", "tix", "year"})

# The mana-value spellings, whose negated equality earns the value sentence instead.
_MANA_VALUE_KEYWORDS = frozenset({"cmc", "mv", "manavalue"})

_MANA_VALUE_REASON = "The value must be a number, or \u201ceven\u201d/\u201codd\u201d"

# A LEADING `-` ON A COMPARISON LEAF IS NOT APPLIED BY SCRYFALL. The term becomes always-true.
#
# This is the general case of the table above, and it is SILENT -- no warning, no 400, nothing in
# the response that says a term was not applied. That silence is why it went unnoticed while the
# equality half, which announces itself, has been implemented here since the policy was written.
#
# --- THE MEASUREMENT ---------------------------------------------------------
#
# Anchor `e:khm t:creature` = 151, one request per row, api.scryfall.com 2026-08-16. A row that
# answers 151 is a term that did nothing:
#
#              positive   negated                  positive   negated
#   pow>=1        146       151        year>=2022      11        151
#   pow>1         125       151        year!=2021      11        151
#   tou>=1        150       151        cn>=100        112        151
#   tou!=1        133       151        edhrec>=5000   112        151
#   pt>=3         141       151        artists>=2       0        151
#   cmc>=3        112       151        paperprints>=2  87        151
#   cmc!=3        106       151        papersets>=2    86        151
#   loy>=3          1       151        pow>=tou       106        151
#   usd>=1         28       151        cmc>=notanumber  0        151
#   eur>=1         27       151
#
# All five of `>` `>=` `<` `<=` `!=` were probed on each of pow, tou, cmc, loy, usd, eur, tix, year,
# cn, edhrec, artists, paperprints, papersets -- 65 rows, every one of them 151.
#
# --- IT IS A TAUTOLOGY, NOT A DROPPED TERM -----------------------------------
#
# The distinction decides the implementation, because the two differ under `or`:
#
#   -pow>=1                       200, 33,599 -- the WHOLE corpus, no warnings
#   -pow:1                        400 "All of your terms were ignored." + its warning
#   (-pow>=1 or t:god) e:khm      323 -- all of Kaldheim
#   (t:god) e:khm                  13 -- what a REMOVED arm would have answered
#   (-pow:1 or t:god) e:khm        13 + its warning -- the ignore machinery really does remove
#
# So this cannot be routed through the ignore machinery: the term survives as a leaf that matches
# everything. `-pow>=1 f:notaformat e:khm t:creature` is 151 warning ONLY about `f:notaformat`,
# which pins that the two mechanisms coexist without borrowing each other's sentence.
#
# --- WHERE THE RULE STOPS ----------------------------------------------------
#
# `-( ... )` is honored throughout -- `-(cmc>=3) e:khm t:creature` is 39, the complement of
# `cmc>=3`'s 112, where the bare `-cmc>=3` is 151. The fault is in how `-` binds to a comparison
# LEAF, not in negation.
#
# And the set-comparison columns negate correctly, which is what makes this a table of keywords
# rather than a rule about the operator (positive, negated, and 151 minus the positive):
#
#   r>=rare       52   99 ok     c>=2        19  132 ok     m>=2      102   49 ok
#   r!=rare      114   37 ok     c!=2       135   16 ok     m!=2      151    0 ok
#   rarity>=rare  52   99 ok     colour>=2   19  132 ok     produces>=2 5  146 ok
#                                id>=2       19  132 ok     devotion>={r}{r} 7 144 ok
#
# Every alias of those columns was probed and agrees (`color colors colour colours`,
# `id identity ci commander`, `r rarity`, `m mana`). The upstream-only spellings
# `color_identity`/`coloridentity` are deliberately NOT here: Scryfall does not know them, so on
# Scryfall they take the tautology like any other unknown keyword -- and _NOT_SCRYFALL_KEYWORDS
# drops them before this rule is reached anyway.
#
# On a TEXT column or an unknown keyword the positive comparison already matches nothing
# (`name>zzz`, `t>creature`, `nonsense>=1` are all 404 with no warning), so the negated form
# matching everything is ordinary boolean negation rather than a fault -- but the answer to
# reproduce is the same tautology, and routing those through here is what stops
# `-nonsense>=1 e:khm t:creature` emitting an unknown-keyword warning Scryfall does not (measured:
# 151, `warnings` absent). It is also why this runs BEFORE the value validators: `-lang>zz`,
# `-f>notaformat` and `-oracleid>abc` are 151 with no warning where their unnegated twins are
# ignored-and-warned.
_NEGATION_HONORING_COMPARISONS = frozenset(
    {
        "c",
        "color",
        "colors",
        "colour",
        "colours",
        "id",
        "identity",
        "ci",
        "commander",
        "r",
        "rarity",
        "m",
        "mana",
        "produces",
        "devotion",
    }
)

# `date` is the third behaviour: the `-` is DISCARDED and the term applied POSITIVELY.
#
# Not dropped (that would answer the anchor's 151) and not honored (that would answer the
# complement) -- measured on every operator, with values chosen so the three readings differ:
#
#                     positive   negated   honored would be
#   date>=2022           11        11            140
#   date<2022           141       141             11
#   date>2021            11        11            141
#   date<=2021          141       141             11
#   date!=2021           11        11            141
#   date:2021           141       141             11
#   date=2021           141       141             11
#
# `year`, the other spelling of the same underlying column, does NOT do this: `year>=2022` is 11 and
# `-year>=2022` is 151, the ordinary tautology above. Two keywords onto one column, two different
# faults -- which is why this is a keyword table and not a column one.
#
# `-(date>2021) e:khm t:creature` is 141, the honest complement of 11, so this too is the leaf
# binding rather than negation.
_DATE_KEYWORDS = frozenset({"date"})

# THE KEYWORDS SCRYFALL ACTUALLY IMPLEMENTS `>` `>=` `<` `<=` `!=` FOR. Everything else -- a text
# column this parser knows, a directive, or a keyword nobody knows -- is HONORED AND MATCHES
# NOTHING under those five operators, silently.
#
# --- THE ENUMERATION ---------------------------------------------------------------------------
#
# Not reasoned about: every alias in `DB_COLUMNS` and every directive name was probed as
# `<alias>>=0 e:khm t:creature` against api.scryfall.com, 2026-08-16, one request each. `>=0` is the
# discriminator because it is satisfiable on every numeric column, so a 404 means the comparison did
# not happen rather than that it happened and found nothing. 78 rows fell into three classes:
#
#   COMPARES (200, a real count)
#     c ci color colors colour colours commander id identity   151 (colour count)
#     cmc mv manavalue m mana                                  151
#     pow power tou toughness                                  151
#     cn number year                                           151
#     usd eur tix                                              141
#     loy loyalty                                                1
#     produces                                                 151
#
#   COMPARES, AND CHECKS ITS VALUE (200 + an ignored-term warning on a bad value)
#     r rarity        `Unknown rarity "0."`
#     date            `Invalid date or unknown set code "0"`
#     devotion        `Devotion can only match single color or hybrid mana.`
#
#   MATCHES NOTHING (404, and NO `warnings` key)
#     a art artist arttag atag banned border e s set f format legal restricted flavor fo ft
#     fulloracle function frame has is keyword kw lang language layout name o oracle oracle_id
#     oracleid oracletag otag set_type settype st t type watermark wm
#     unique sort order direction dir prefer            (the directive names take it too)
#     nonsense                                          (and so does any unknown keyword)
#
# --- WHY IT IS ONE RULE AND NOT TWO ------------------------------------------------------------
#
# The unknown-keyword case and the text-column case reach the same answer by the same route, and the
# pairs that separate them are the proof:
#
#   nonsense:1   200, 151 + `Unknown keyword "nonsense".`   nonsense>=1   404, no warning
#   t:creature   200, 151                                   t>creature    404, no warning
#   f:notaformat 200, 151 + `Unknown game format`           f>notaformat  404, no warning
#   lang:zz      200, 151 + `Unknown language `zz``         lang>zz       404, no warning
#
# Under `:`/`=` each of those runs a validator and ignores the term; under a comparison NONE of them
# does, and the term survives matching nothing. So this must run BEFORE the unknown-keyword rule and
# before every value validator -- a comparison never reaches them.
#
# `nonsense>1`, `nonsense<1`, `nonsense<=1` and `nonsense!=1` are all the same 404, so it is the
# whole comparison family and not `>=` alone.
#
# --- WHAT IS DELIBERATELY NOT IN THE SET -------------------------------------------------------
#
# `edhrec`, `artists`, `paperprints`, `papersets` and `pt` are numeric columns Scryfall compares
# (`edhrec>=5000 e:khm t:creature` = 112) and this parser has no spelling for. They are not in
# _SCRYFALL_ONLY_KEYWORDS either, so they were already being reported as unknown keywords; under
# this rule they answer the 404 an unknown keyword answers instead of the 151 the ignore machinery
# answered. Both are wrong against Scryfall's count, and putting them in the set would be worse -- a
# term kept for a keyword the parser cannot lex is a 400.
_COMPARABLE_KEYWORDS = frozenset(
    {
        # colour and colour-identity counts
        "c",
        "color",
        "colors",
        "colour",
        "colours",
        "ci",
        "id",
        "identity",
        "commander",
        "produces",
        # mana
        "m",
        "mana",
        "devotion",
        # numeric columns
        "cmc",
        "mv",
        "manavalue",
        "pow",
        "power",
        "tou",
        "toughness",
        "loy",
        "loyalty",
        "usd",
        "eur",
        "tix",
        "cn",
        "number",
        "year",
        # ordered enums / dates
        "r",
        "rarity",
        "date",
    }
)

# The five operators the table above is about; `:` and `=` are the other, older mechanism.
_COMPARISON_OPERATORS = frozenset({">", ">=", "<", "<=", "!="})

# `f:`/`format:`/`legal:`/`banned:`/`restricted:` -- Scryfall's game formats. The `legalities` key
# set of a live card object, plus the search-only spellings measured as honored. `pauperedh` and
# `frontier` are NOT among them -- both come back ignored-and-warned, which makes this a measured
# boundary rather than a guess at a superset.
_SCRYFALL_FORMATS = frozenset(
    {
        "standard",
        "future",
        "historic",
        "timeless",
        "gladiator",
        "pioneer",
        "modern",
        "legacy",
        "pauper",
        "vintage",
        "penny",
        "commander",
        "oathbreaker",
        "standardbrawl",
        "brawl",
        "competitivebrawl",
        "alchemy",
        "paupercommander",
        "duel",
        "oldschool",
        "premodern",
        "predh",
        "tlr",
        "explorer",
        "historicbrawl",
        "duelcommander",
        "edh",
    }
)

# `lang:`/`language:` -- every spelling measured as honored, plus `any`. Scryfall is generous here
# (`zh`, `jp`, `sp`, `kr`, `cn`, `tw`, `cs`, `ru-ru`, `pt-br` and the full English names all
# resolve) and still rejects `zz`, `po` and the ambiguous `chinese`.
_SCRYFALL_LANGUAGES = frozenset(
    {
        "any",
        "en",
        "es",
        "fr",
        "de",
        "it",
        "pt",
        "ja",
        "ko",
        "ru",
        "zhs",
        "zht",
        "he",
        "la",
        "grc",
        "ar",
        "sa",
        "ph",
        "qya",
        "cs",
        "zh",
        "jp",
        "sp",
        "kr",
        "cn",
        "tw",
        "ru-ru",
        "pt-br",
        "english",
        "spanish",
        "french",
        "german",
        "italian",
        "portuguese",
        "japanese",
        "korean",
        "russian",
        "phyrexian",
        "chinesesimplified",
        "chinesetraditional",
    }
)

_SCRYFALL_RARITIES = frozenset({"common", "uncommon", "rare", "special", "mythic", "bonus", "c", "u", "r", "s", "m", "b"})

# The colour VALUES Scryfall reads as a name rather than as a set of letters.
#
# Measured one request each (`c:<value> e:khm`, 2026-08-16). The accepted names are exactly the ten
# guilds, the ten shards and wedges, the five HYPHENATED four-colour names plus their five one-word
# synonyms, `rainbow`, `all`, `gold`, `brown`, and the British spellings -- while `yore`, `glint`,
# `dune`, `ink`, `witch`, `five` and `mono` are all REJECTED, so the un-hyphenated four-colour
# nicknames are not in Scryfall's table and this list is a boundary rather than a superset.
#
# The `m` family -- `m`, `gold`, `multicolor(ed)`, `multicolour(ed)` -- is listed as accepted even
# though this parser has no spelling for it: those are a colour COUNT (`c:m` = `c>=2` = 44 in
# Kaldheim, where `c:2` = 43) rather than a set, and ignoring them would answer a wider result than
# Scryfall while claiming to have dropped a term Scryfall applied. Left to fail loudly, like the
# _SCRYFALL_ONLY_KEYWORDS above.
_COLOR_NAMES = frozenset(
    {
        "white",
        "blue",
        "black",
        "red",
        "green",
        "colorless",
        "colourless",
        "multicolor",
        "multicolour",
        "multicolored",
        "multicoloured",
        "gold",
        "m",
        "brown",
        "rainbow",
        "all",
        "azorius",
        "dimir",
        "rakdos",
        "gruul",
        "selesnya",
        "orzhov",
        "izzet",
        "golgari",
        "boros",
        "simic",
        "bant",
        "esper",
        "grixis",
        "jund",
        "naya",
        "abzan",
        "jeskai",
        "sultai",
        "mardu",
        "temur",
        "yore-tiller",
        "glint-eye",
        "dune-brood",
        "ink-treader",
        "witch-maw",
        "artifice",
        "chaos",
        "aggression",
        "altruism",
        "growth",
    }
)

# `produces:` reads a NARROWER name table than the colour columns do: `produces:brown` comes back
# "Unknown color \u201cn\u201d" and `produces:colorless` "Unknown color \u201ce\u201d", where
# `c:brown` and `c:colorless` are both fine -- colorless is a producible VALUE there, spelled `c`,
# and the words for it are simply not in that table.
_PRODUCES_NAMES = _COLOR_NAMES - {"colorless", "colourless", "brown"}

_COLOR_LETTERS = "wubrgcm"
_COLORED_LETTERS = "wubrg"

_DEVOTION_KEYWORDS = frozenset({"devotion"})

# Colour letters, and the rest of the alphabet a mana symbol may be spelled from.
_DEVOTION_COLORS = "wubrg"
_MANA_SYMBOL_PARTS = frozenset("wubrgcsxyzp")

_DEVOTION_REASON = "Devotion can only match single color or hybrid mana."

_FORMAT_KEYWORDS = frozenset({"f", "format", "legal", "banned", "restricted"})
_LANGUAGE_KEYWORDS = frozenset({"lang", "language"})
_RARITY_KEYWORDS = frozenset({"r", "rarity"})
_ORACLE_ID_KEYWORDS = frozenset({"oracleid", "oracle_id"})
_COLOR_KEYWORDS = frozenset({"c", "color", "colors", "ci", "id", "identity", "produces"})

# Every keyword this file may NOT call unknown: the parser's own aliases, the in-query directives,
# and the ones the validators below have rules for.
#
# The last group is load-bearing rather than belt-and-braces: `lang:` and `oracleid:` arrive with
# #926 and this branch does not have them yet, so without it a `lang:zz` on a tree without that PR
# would be reported as an unknown KEYWORD rather than an unknown LANGUAGE — the right status with
# the wrong sentence, changing under it when an unrelated branch merged.
_KNOWN_KEYWORDS = (
    frozenset(ALIAS_TO_FIELD_INFOS)
    | _DIRECTIVE_KEYWORDS
    | _MANA_VALUE_KEYWORDS
    | _NEGATED_EQUALITY_UNKNOWN_KEYWORD
    | _NEGATION_HONORING_COMPARISONS
    | _COMPARABLE_KEYWORDS
    | _DATE_KEYWORDS
    | _FORMAT_KEYWORDS
    | _LANGUAGE_KEYWORDS
    | _RARITY_KEYWORDS
    | _ORACLE_ID_KEYWORDS
    | _COLOR_KEYWORDS
)

_UUID_V4_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")

_LEAF_RE = re.compile(r"^(-?)([A-Za-z_][A-Za-z0-9_]*)(!=|>=|<=|:|=|>|<)(.*)$", re.DOTALL)

_NUMERIC_VALUE_RE = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)$")

# The column names Scryfall accepts on the RIGHT of a numeric comparison.
_CROSS_COLUMN_VALUES = frozenset({"pow", "power", "tou", "toughness", "cmc", "mv", "manavalue", "loy", "loyalty", "x"})

# How much of a rejected expression Scryfall echoes: 20 characters INCLUDING the ellipsis.
#
# Measured by lengthening one term a character at a time -- `f:abcdefghijklmnopqr` (20 characters)
# comes back whole and `f:abcdefghijklmnopqrs` (21) comes back as `f:abcdefghijklmnopq…`, which is
# 19 characters and a U+2026. That is Rails' `String#truncate(20)`, whose omission counts against
# the budget rather than being added to it, and it also fits the other truncation seen live
# (`id:00000000-0000-00…` for a nil UUID). Only the EXPRESSION is cut; the reason sentence still
# names the full value.
_EXPRESSION_ECHO_LIMIT = 20

# A term that can never match, substituted for a numeric comparison whose value is not a number.
#
# Scryfall answers `q=cmc>=notanumber` with its ordinary 404 -- the term is HONORED and matches
# nothing, unlike the ignored terms above, which is why it cannot be dropped: dropping it would turn
# `cmc>=notanumber e:khm` into all of Kaldheim where Scryfall answers "no cards". Mana value is
# never negative, so this leaf is empty by arithmetic rather than by a special node type, and it
# composes correctly under `-` and `or` the way a dropped term would not.
_NEVER_MATCHES = "cmc<0"

# A term that always matches, substituted for a negated comparison Scryfall does not apply.
#
# The negation of `_NEVER_MATCHES` rather than a positive tautology such as `cmc>=0`, because the
# two are not the same term over a column that can be absent: `cmc>=0` asks the index for rows whose
# mana value compares, and the complement of the empty set is every row including those. It is also
# the cheaper of the two -- the engine builds the empty leaf and complements it, where `cmc>=0` is a
# full range scan.
#
# `_classify_leaf`'s output is spliced into the rebuilt query and never re-classified, so this
# spelling being itself a negated comparison costs nothing; it is idempotent regardless.
_ALWAYS_MATCHES = f"-{_NEVER_MATCHES}"

# The operators whose characters do NOT stay on the word when the value is missing. See
# _dangling_operator_term.
_BARE_WORD_OPERATORS = frozenset({":", ">", "<"})

_CONNECTORS = frozenset({"and", "or"})

# The shortest string that can be a delimited one: a pair of quotes, or a pair of slashes.
_DELIMITED_MINIMUM = 2


@dataclass
class TermPolicyResult:
    """The query as Scryfall would run it, and what it says about the terms it dropped."""

    #: The query to hand the parser: the input, minus the terms Scryfall would ignore.
    query: str
    #: Scryfall's warnings, in source order, already worded as Scryfall words them.
    warnings: list[str] = field(default_factory=list)
    #: Every term was ignored -- the caller answers 400 "All of your terms were ignored."
    all_ignored: bool = False
    #: The query's parentheses do not balance -- Scryfall's own 400, with its own sentence.
    #:
    #: Measured 2026-08-16: `e:khm (t:god`, `e:khm t:god)` and a lone `(` all answer
    #: `400 bad_request` / "Your search contains unclosed parentheses.", for a stray closer as well
    #: as a stray opener.
    unclosed_parens: bool = False


def fold_smart_quotes(query: str) -> str:
    """Fold the four typographic quotes Scryfall folds; every other character is left alone.

    Args:
        query: The raw query text as the client sent it.

    Returns:
        The query with U+2018/U+2019 as `'` and U+201C/U+201D as `"`.
    """
    return query.translate(_SMART_QUOTES)


def _dangling_operator_term(negated: bool, keyword: str, operator: str) -> str:
    """Rewrite `t:` to the bare-word name search Scryfall reads it as.

    A DANGLING OPERATOR IS NOT A TERM AT ALL: `t:` is the bare word `t`, and a bare word is a NAME
    search.

    This used to answer `q=t:` with every card, on the theory that an operator with no value
    constrains nothing. Measured (api.scryfall.com, 2026-08-16), the theory is wrong twice over --
    and so is the "this column is not null" reading it was replaced by, which fits `t:` = 22,261
    and `o:` = 22,111 and then dies on `ft:` = 1,628 where "has flavor text" is 20,877. What
    Scryfall does is simpler: the term fails to lex as a keyword expression, so the token falls
    through to an ordinary bare word -- and `t` names cards whose NAME contains "t".

    Sixteen pairs, one request each, and every one of them equal::

        t:      = t      = name:t   22,261      cmc:  = cmc         404 (no card is named "cmc")
        o:      = o                 22,111      layout: = layout    404
        name:   = name                  33      nonsense: = nonsense 404
        ft:     = ft     = name:ft   1,628      wm:   = wm           33
        in:     = in                 7,878      st:   = st        5,556
        t: e:khm  = t e:khm            215      -t: e:khm = -t e:khm  108
        t: or e:khm                 22,369      t: o: = t o      15,057

    `t: or e:khm` is the row that proves it composes as an ordinary leaf rather than as a
    special-cased whole-query fallback: 22,261 + (323 - 215) = 22,369 exactly.

    The OPERATOR decides how much of the token becomes the word. With `:`, `>` or `<` the bare word
    is the keyword alone (`t>` = `t<` = `t:` = 215 in Kaldheim); with `=`, `>=`, `<=` or `!=` the
    operator characters stay ON the word, which is why `t=` and `t>=` are 404 where `t:` is 22,261,
    and `name:"t="` is 404 to match. Both branches were checked against their `name:` twin.

    Rewriting to `name:...` rather than to a bare word keeps the substitution safe in every
    position: a keyword is `[A-Za-z_][A-Za-z0-9_]*`, so `or:` would otherwise become the connector
    `or`. Negation, grouping and `or` then compose for free, because the result is just a term.

    UNQUOTED for the bare-word branch, and quoted only for the `=`-family, because Scryfall does
    not read the two spellings alike: `name:ft` is 1,628 and `name:"ft"` is 362, and the measured
    equality is with the UNQUOTED form (`ft:` = `ft` = `name:ft` = 1,628). The `=`-family has to
    be quoted regardless -- its word carries the operator characters, and `name:"t="` is the 404
    that matched `t=`.

    Args:
        negated: Whether the term carried a leading `-`.
        keyword: The keyword text as the client wrote it.
        operator: The comparison operator the value was missing from.

    Returns:
        The term to put in the rebuilt query in place of the dangling one.
    """
    value = keyword if operator in _BARE_WORD_OPERATORS else f'"{keyword}{operator}"'
    return f"{'-' if negated else ''}name:{value}"


def _ignored_warning(term: str, reason: str) -> str:
    """Build Scryfall's `Invalid expression “…” was ignored. <reason>`, with its truncation."""
    echoed = term if len(term) <= _EXPRESSION_ECHO_LIMIT else term[: _EXPRESSION_ECHO_LIMIT - 1] + "\u2026"
    return f"Invalid expression \u201c{echoed}\u201d was ignored. {reason}"


def _regex_reason(pattern: str) -> str:
    """Onigmo's wording for the malformations a pasted regex actually has.

    Scryfall compiles the pattern in Ruby and reports its engine's message, so the four classes
    below were read off api.scryfall.com rather than translated: `/[unclosed/` and `/[a-/` ->
    brackets, `/(unclosed/` and `/a)/` -> parentheses, `/a{2,1}/` -> repetition, a bare leading
    quantifier -> quantifier. Anything else gets the generic sentence; the alternative is inventing
    a message per malformation, which would be a guess wearing a measurement's clothes.

    Args:
        pattern: The pattern between the slashes.

    Returns:
        The reason sentence.
    """
    unescaped = re.sub(r"\\.", "", pattern, flags=re.DOTALL)
    depth = 0
    in_class = False
    parens_balanced = True
    for char in unescaped:
        if in_class:
            if char == "]":
                in_class = False
            continue
        if char == "[":
            in_class = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                parens_balanced = False
    if in_class:
        return "Invalid regular expression: brackets [] not balanced."
    if depth != 0 or not parens_balanced:
        return "Invalid regular expression: parentheses () not balanced."
    repetition = re.search(r"\{(\d+),(\d+)\}", unescaped)
    if repetition and int(repetition.group(1)) > int(repetition.group(2)):
        return "Invalid regular expression: invalid repetition count(s)."
    if re.search(r"(^|[(|])[*+?]", unescaped):
        return "Invalid regular expression: quantifier operand invalid."
    return "Invalid regular expression: invalid pattern."


def _color_reason(value: str, keyword: str) -> str | None:
    """Why Scryfall refuses a colour value, or None when it does not.

    THE ORDER OF THE THREE CHECKS IS MEASURED, not chosen: `c:witch` spells `w i t c h`, whose `i`,
    `t` and `h` are not colours, and Scryfall still answers "A card cannot be both colored and
    colorless" -- so the contradiction is decided on the letters it DID recognize, before it
    complains about the ones it did not.

    The `m` rule is decided FIRST, ahead of the contradiction: `c:monocolor` and `c:chromatic` and
    `c:spectrum` all spell a `c` alongside coloured letters AND contain an `m`, and Scryfall
    answers the `m` sentence for every one of them. Reading the contradiction first got all three
    wrong while still fitting `c:witch`, which is why the order is pinned by values that separate
    the two rules rather than by values that satisfy either.

    And the contradiction does not exist for `produces:` at all, because colorless is a genuine
    producible value there: `produces:wubrgc` is honoured (it matches nothing), and
    `produces:colorless` answers "Unknown color \u201ce\u201d" -- the unknown-letter sentence --
    where `c:colorless` is simply a name.

    The `m` rule reads the WHOLE value, not the letters it recognized, and stops at five
    characters. Both halves were needed to fit the measurements, and the first reading of this rule
    (recognized letters only, untruncated) got `c:mono` wrong in the loudest way available -- it
    answered "Unknown color \u201cn\u201d" where Scryfall answers the `m` sentence. Each value is
    `sorted(set(value) - {m, -})` cut to five: `mono`->no, `mm`->(empty), `mwu`->uw, `mzy`->yz,
    `m1`->1, `mono-red`->denor, `monocolor`->clnor, `monocolored`->cdeln, `nephilim`->ehiln (not
    "ehilnp"), `chromatic`->achio (not "achiort"), `spectrum`->ceprs, `prismatic`->acipr.

    And the letter it names is the ALPHABETICALLY FIRST unrecognized one, which took nine values to
    establish and no two of which agree on any simpler rule: `glint`->i, `yore`->e, `dune`->d,
    `null`->l, `void`->d, `spirit`->i, `land`->a, `five`->e, `qq`->q. Not the first in the string,
    not the last -- the first in sorted order.

    Args:
        value: The value after the operator, unquoted.
        keyword: The keyword the value was written against -- `produces:` has its own table.

    Returns:
        Scryfall's sentence, or None when the value is one it accepts.
    """
    lower = value.lower()
    names = _PRODUCES_NAMES if keyword == "produces" else _COLOR_NAMES
    if not lower or lower in names or lower.isdigit():
        return None
    known = {ch for ch in lower if ch in _COLOR_LETTERS}
    unknown = {ch for ch in lower if ch not in _COLOR_LETTERS}
    if "m" in lower and len(lower) > 1:
        rest = "".join(sorted(set(lower) - {"m", "-"}))[: len(_COLORED_LETTERS)]
        return f"Using \u201cm\u201d with other colors is no longer supported. Use c>{rest} instead."
    if keyword != "produces" and "c" in known and any(ch in _COLORED_LETTERS for ch in known):
        return "A card cannot be both colored and colorless."
    if unknown:
        return f"Unknown color \u201c{min(unknown)}\u201d"
    return None


def _devotion_reason(value: str) -> str | None:
    """Why Scryfall refuses this devotion value, or None when it accepts it.

    `devotion:` takes ONE colour, repeated -- or one hybrid PAIR, repeated. Anything else is
    ignored-and-warned, in both polarities and under every operator, in two different sentences
    depending on whether Scryfall recognised the symbol at all. Measured against api.scryfall.com
    2026-08-16, anchor `e:khm t:creature` = 151::

        HONORED    {r} 27   {R} 27   r 27   {r}{r} 7   rr 7   {r}{r}{r} 404 (nothing that deep)
                   {r/g} 62   {g/r} 62   {r/g}{r/g} 16   {r/g}{g/r} 16

        "Devotion can only match single color or hybrid mana."
                   {w}{u}   {r}{g}   rg          two different colours
                   {r}{r/g}                      a colour and a hybrid do not mix
                   {c} {s} {x} {1}               recognized symbols that are not a colour
                   {2/r} {r/p}                   hybrids with a non-colour half
                   2                             any non-symbol value

        "Unknown mana symbols \u201c<VALUE, UPPERCASED>\u201d."
                   {p} -> "{P}"    {} -> "{}"    notmana -> "NOTMANA"

    So `{c}`, `{s}`, `{x}`, `{1}`, `{2/r}` and `{r/p}` ARE mana symbols and simply are not devotion,
    while a lone `{p}` and an empty `{}` are not symbols at all. Order-insensitivity of the hybrid
    pair is measured, not assumed: `{g/r}` and `{r/g}` answer the same 62, and mixing the two
    spellings in one value answers the same 16 as either alone.

    Args:
        value: The value with one layer of quotes removed.

    Returns:
        Scryfall's sentence, or None.
    """
    lower = value.lower()
    unknown = f"Unknown mana symbols \u201c{value.upper()}\u201d."
    if lower.startswith("{"):
        # `{a}{b}{c}` -- anything that is not a closed brace group makes the whole value unreadable.
        groups = re.findall(r"\{[^{}]*\}", lower)
        if "".join(groups) != lower:
            return unknown
        symbols = [group[1:-1] for group in groups]
    else:
        symbols = list(lower)
    if not symbols:
        return unknown
    signatures = set()
    for symbol in symbols:
        parts = symbol.split("/")
        # A symbol Scryfall does not know at all: an empty group, or a part outside the mana
        # alphabet. A LONE `p` is in that class too -- `{p}` is "Unknown mana symbols", where
        # `{r/p}` is a symbol Scryfall knows and rejects for devotion.
        if any(part not in _MANA_SYMBOL_PARTS and not part.isdigit() for part in parts):
            return unknown
        if parts == ["p"]:
            return unknown
        # Known, but devotion counts colour pips only: every half must be a colour.
        if not all(part in _DEVOTION_COLORS for part in parts):
            return _DEVOTION_REASON
        signatures.add("".join(sorted(set(parts))))
    if len(signatures) > 1:
        return _DEVOTION_REASON
    return None


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes, so a validator reads the value the lexer would."""
    if len(value) >= _DELIMITED_MINIMUM and value[0] in "\"'" and value.endswith(value[0]):
        return value[1:-1]
    return value


def _is_numeric_value(value: str) -> bool:
    """Whether a value reads as a number to the numeric columns (Scryfall also takes even/odd)."""
    plain = _unquote(value).strip().lower()
    if plain in {"even", "odd"}:
        return True
    if _NUMERIC_VALUE_RE.match(plain):
        return True
    # `pow>=tou` and friends: a column name on the right is Scryfall's cross-column comparison.
    return plain.isalpha() and plain in _CROSS_COLUMN_VALUES


@dataclass
class _Piece:
    """One top-level piece of a query: a group, a boolean connector, or a leaf term."""

    text: str
    kind: str
    inner: str | None = None
    prefix: str = ""


def _skip_delimited(source: str, pos: int) -> int:
    """Advance past a quoted string, a regex literal or a mana symbol starting at `pos`.

    The one implementation, because `_scan_pieces` and `_unbalanced_parens` must agree exactly on
    which regions of a query are text rather than syntax: a `(` inside `name:"(a"` is a character,
    and a scan that disagreed with the balance check about that would report a typo in a valid
    query, or corrupt one.

    Args:
        source: The whole level being scanned.
        pos: Index of the opening delimiter.

    Returns:
        The index just past the closing delimiter, or the end of the string when it never closes.
    """
    n = len(source)
    opener = source[pos]
    if opener == "{":
        close = source.find("}", pos + 1)
        return n if close == -1 else close + 1
    pos += 1
    while pos < n:
        char = source[pos]
        if char == "\\" and pos + 1 < n:
            pos += 2
        elif char == opener:
            return pos + 1
        else:
            pos += 1
    return n


def _scan_pieces(source: str) -> list[_Piece]:
    """Split one nesting level into pieces, respecting everything the lexer respects.

    `"…"`, `'…'`, `/…/` and `{…}` all carry spaces without ending a term, and a backslash escapes
    the next character inside a string or a pattern, because a term boundary this scan gets wrong is
    a query this policy would corrupt.

    Args:
        source: The text of one nesting level.

    Returns:
        The pieces, in source order.
    """
    pieces: list[_Piece] = []
    n = len(source)
    pos = 0
    while pos < n:
        if source[pos].isspace():
            pos += 1
            continue
        start = pos
        depth = 0
        group_start = -1
        group_end = -1
        while pos < n:
            char = source[pos]
            if char in "\"'/{":
                pos = _skip_delimited(source, pos)
                continue
            if char == "(":
                if depth == 0:
                    group_start = pos
                depth += 1
                pos += 1
                continue
            if char == ")":
                depth -= 1
                pos += 1
                if depth == 0:
                    group_end = pos
                continue
            if depth == 0 and char.isspace():
                break
            pos += 1
        text = source[start:pos]
        if group_start >= 0 and group_end == pos:
            pieces.append(
                _Piece(
                    text=text,
                    kind="group",
                    prefix=source[start:group_start],
                    inner=source[group_start + 1 : group_end - 1],
                )
            )
        elif text.lower() in _CONNECTORS:
            pieces.append(_Piece(text=text, kind="connector"))
        else:
            pieces.append(_Piece(text=text, kind="leaf"))
    return pieces


def _is_unknown_keyword(keyword: str) -> bool:
    """Whether Scryfall would call this keyword unknown.

    Two cases: a spelling this project added that Scryfall never had, and a spelling NEITHER side
    knows (`nonsense:value`, which Scryfall ignores and this surface used to answer with a parse
    error). A keyword Scryfall knows and this project does not is deliberately excluded -- see
    `_SCRYFALL_ONLY_KEYWORDS`.

    Args:
        keyword: The lowercased keyword before the operator.

    Returns:
        Whether the term carrying it should be dropped and warned about.
    """
    if keyword in _NOT_SCRYFALL_KEYWORDS:
        return True
    return keyword not in _KNOWN_KEYWORDS and keyword not in _SCRYFALL_ONLY_KEYWORDS


def _unapplied_negation(negated: bool, equality: bool, keyword: str, term: str) -> str | None:
    """What a leading `-` Scryfall does not apply leaves behind, or None when it is applied.

    Args:
        negated: Whether the term carried a leading `-`.
        equality: Whether the operator is `:` or `=`, which the table above already covers.
        keyword: The lowercased keyword.
        term: The raw text of the term, as the client wrote it.

    Returns:
        The text the rebuilt query carries in place of `term`, or None to leave the term alone.
        See _NEGATION_HONORING_COMPARISONS and _DATE_KEYWORDS for the measurements.
    """
    if not negated:
        return None
    if keyword in _DATE_KEYWORDS:
        return term[1:]
    if not equality and keyword not in _NEGATION_HONORING_COMPARISONS:
        return _ALWAYS_MATCHES
    return None


def _classify_leaf(term: str) -> tuple[bool, str, str | None]:
    """Decide what becomes of one leaf term.

    Args:
        term: The raw text of the term, as the client wrote it.

    Returns:
        `(keep, text, reason)`. When `keep` is True, `text` is what the rebuilt query carries (which
        may differ from `term` for an unsatisfiable numeric comparison). When it is False, `reason`
        is Scryfall's sentence, or None for the one removal Scryfall does not warn about.
    """
    match = _LEAF_RE.match(term)
    if match is None:
        return True, term, None
    negated = match.group(1) == "-"
    keyword = match.group(2).lower()
    operator = match.group(3)
    raw_value = match.group(4)

    # BEFORE the unknown-keyword rule, because a dangling operator never reaches Scryfall's keyword
    # table at all: `nonsense:x` is "Unknown keyword" and `nonsense:` is a 404 for a card named
    # "nonsense" -- the same 404 `q=nonsense` gives. See _dangling_operator_term.
    if raw_value == "":
        return True, _dangling_operator_term(negated, match.group(2), operator), None

    equality = operator in {":", "="}

    # BEFORE the unknown-keyword rule and before every value validator, because Scryfall applies it
    # there: `-nonsense>=1`, `-subtype>=1`, `-lang>zz`, `-f>notaformat` and `-oracleid>abc` are all
    # the anchor's 151 with an ABSENT `warnings` key, where each unnegated twin is
    # ignored-and-warned.
    unapplied_negation = _unapplied_negation(negated, equality, keyword, term)
    if unapplied_negation is not None:
        return True, unapplied_negation, None

    # BEFORE the unknown-keyword rule and before every value validator, because Scryfall's
    # comparison operators reach neither. A keyword outside _COMPARABLE_KEYWORDS -- a text column, a
    # directive name, or a keyword nobody knows -- is HONORED and matches nothing under `>` `>=` `<`
    # `<=` `!=`, with no `warnings` key at all. `nonsense>=1`, `t>creature`, `f>notaformat`,
    # `lang>zz`, `oracleid>abc` and `is>foil` are one 404 each; their `:` twins are all
    # ignored-and-warned. See _COMPARABLE_KEYWORDS for the 78-row enumeration.
    #
    # The _SCRYFALL_ONLY exemption inside _is_unknown_keyword does not apply here: it exists so a
    # keyword Scryfall honors is not silently dropped, and this rule drops nothing -- it answers
    # Scryfall's own empty result.
    if operator in _COMPARISON_OPERATORS and keyword not in _COMPARABLE_KEYWORDS:
        return True, _NEVER_MATCHES, None

    if _is_unknown_keyword(keyword):
        return False, term, f"Unknown keyword \u201c{'-' if negated else ''}{keyword}\u201d."

    if negated and equality:
        if keyword in _MANA_VALUE_KEYWORDS:
            return False, term, _MANA_VALUE_REASON
        if keyword in _NEGATED_EQUALITY_UNKNOWN_KEYWORD:
            return False, term, f"Unknown keyword \u201c-{keyword}\u201d."

    value = _unquote(raw_value)

    # A numeric column asked for something that is not a number. With `:`/`=` Scryfall ignores the
    # term; with a comparison it keeps it and matches nothing (`q=cmc>=notanumber` is a 404, not a
    # 400), so those two answers are different terms rather than one rule.
    if (keyword in _MANA_VALUE_KEYWORDS or keyword in _NEGATED_EQUALITY_UNKNOWN_KEYWORD) and not _is_numeric_value(raw_value):
        if equality:
            if keyword in _MANA_VALUE_KEYWORDS:
                return False, term, _MANA_VALUE_REASON
            return False, term, f"Unknown keyword \u201c{keyword}\u201d."
        return True, _NEVER_MATCHES, None

    reason = _value_reason(keyword, value, raw_value)
    if reason is not None:
        return False, term, reason
    return True, term, None


def _value_reason(keyword: str, value: str, raw_value: str) -> str | None:
    """Why Scryfall refuses this keyword's VALUE, or None when it accepts it.

    Split out of `_classify_leaf` so each half stays readable: that one decides which RULE applies
    to a term, this one applies the per-keyword vocabularies. It no longer takes the operator: the
    comparison rule in `_classify_leaf` answers every keyword Scryfall does not compare before this
    is reached, and the keywords that DO reach it check their value under every operator alike.

    Args:
        keyword: The lowercased keyword before the operator.
        value: The value with one layer of quotes removed.
        raw_value: The value exactly as written, so a regex literal keeps its slashes.

    Returns:
        Scryfall's sentence, or None.
    """
    if keyword in _FORMAT_KEYWORDS and value.lower() not in _SCRYFALL_FORMATS:
        return f"Unknown game format \u201c{value}\u201d"
    if keyword in _LANGUAGE_KEYWORDS and value.lower() not in _SCRYFALL_LANGUAGES:
        return f"Unknown language `{value}`"
    # EVERY operator, not only `:`/`=`. Rarity is an ordered enum, so `r>rare` is a comparison
    # Scryfall really performs -- and it checks the value under a comparison exactly as it does under
    # equality. Measured, anchor `e:khm t:creature` = 151: `r:notarare`, `r=notarare`, `r>notarare`,
    # `r>=notarare`, `r<notarare` and `r!=notarare` are all 151 carrying `Unknown rarity
    # "notarare."`, and `rarity>=0` is 151 carrying `Unknown rarity "0."`.
    if keyword in _RARITY_KEYWORDS and value.lower() not in _SCRYFALL_RARITIES:
        # The full stop INSIDE the quotes is Scryfall's, not a typo here.
        return f"Unknown rarity \u201c{value}.\u201d"
    # Devotion checks its value under every operator and in both polarities -- see _devotion_reason.
    if keyword in _DEVOTION_KEYWORDS:
        devotion_reason = _devotion_reason(value)
        if devotion_reason is not None:
            return devotion_reason
    if keyword in _ORACLE_ID_KEYWORDS and not _UUID_V4_RE.match(value):
        return "You must provide a valid v4 UUID."
    if keyword in _COLOR_KEYWORDS:
        color_reason = _color_reason(value, keyword)
        if color_reason is not None:
            return color_reason
    # A regex literal that will not compile. Validated here so the answer is Scryfall's 400 rather
    # than a 500 out of the engine.
    if len(raw_value) >= _DELIMITED_MINIMUM and raw_value.startswith("/") and raw_value.endswith("/"):
        pattern = raw_value[1:-1]
        try:
            re.compile(pattern)
        except re.error:
            return _regex_reason(pattern)
    return None


def _unbalanced_parens(source: str) -> bool:
    """Whether the query's parentheses fail to balance, ignoring strings, patterns and mana."""
    n = len(source)
    depth = 0
    pos = 0
    while pos < n:
        char = source[pos]
        if char in "\"'/{":
            pos = _skip_delimited(source, pos)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return True
        pos += 1
    return depth != 0


@dataclass
class _Scan:
    """State threaded through the recursive walk."""

    warnings: list[str] = field(default_factory=list)


def _policy_level(source: str, scan: _Scan) -> str | None:
    """Apply the policy to one nesting level, recursing into groups.

    Args:
        source: The text of this level.
        scan: The warnings collected so far, and whether a dangling operator has been seen.

    Returns:
        The rebuilt text, or None when nothing at this level survived -- which is what makes a group
        whose every arm was dropped disappear along with its parentheses.
    """
    pieces = _scan_pieces(source)
    if not pieces:
        return None
    kept: list[_Piece] = []
    # Tracks REWRITES as well as drops, because a numeric comparison whose value is not a number is
    # replaced rather than removed: returning `source` on the strength of "nothing was dropped"
    # would silently throw that substitution away.
    changed = False
    for piece in pieces:
        replacement = _apply_to_piece(piece, scan)
        if replacement is None:
            if piece.kind != "connector":
                changed = True
            if piece.kind == "connector":
                kept.append(piece)
            continue
        if replacement.text != piece.text:
            changed = True
        kept.append(replacement)
    if not changed:
        return source
    cleaned = _drop_orphaned_connectors(kept)
    if not cleaned:
        return None
    return " ".join(piece.text for piece in cleaned)


def _apply_to_piece(piece: _Piece, scan: _Scan) -> _Piece | None:
    """Apply the policy to one piece, or return None when it leaves the query.

    Args:
        piece: One connector, group or leaf.
        scan: The warnings collected so far, and whether a dangling operator has been seen.

    Returns:
        What the rebuilt query carries in its place, or None when nothing does. A connector is
        always returned unchanged -- `_drop_orphaned_connectors` decides whether it survives.
    """
    if piece.kind == "connector":
        return piece
    if piece.kind == "group":
        inner = _policy_level(piece.inner or "", scan)
        if inner is None:
            return None
        return _Piece(text=f"{piece.prefix}({inner})", kind="group")
    keep, text, reason = _classify_leaf(piece.text)
    if keep:
        return _Piece(text=text, kind="leaf")
    scan.warnings.append(_ignored_warning(piece.text, reason or ""))
    return None


def _drop_orphaned_connectors(pieces: list[_Piece]) -> list[_Piece]:
    """Remove the `and`/`or` a drop left with nothing on one side.

    Scryfall tolerates `t:elf or` and so does this, by removing what the drop orphaned rather than
    handing the parser a fragment it would reject.

    Args:
        pieces: The surviving pieces, in order.

    Returns:
        The pieces with leading, doubled and trailing connectors removed.
    """
    cleaned: list[_Piece] = []
    for piece in pieces:
        if piece.kind == "connector" and (not cleaned or cleaned[-1].kind == "connector"):
            continue
        cleaned.append(piece)
    while cleaned and cleaned[-1].kind == "connector":
        cleaned.pop()
    return cleaned


def scryfall_term_policy(raw_query: str) -> TermPolicyResult:
    """Fold the typographic quotes, then drop every term Scryfall would ignore.

    `all_ignored` is the 400 case, and it is deliberately not the same as "empty query": Scryfall
    answers an empty `q` with its own sentence (see `_EMPTY_QUERY_DETAILS` in routes.py) and a query
    whose every
    term was unusable with "All of your terms were ignored." -- two sentences for two mistakes.

    Args:
        raw_query: The `q` parameter as the client sent it.

    Returns:
        The rebuilt query, Scryfall's warnings, and which 400 (if any) the caller owes.
    """
    folded = fold_smart_quotes(raw_query)
    if _unbalanced_parens(folded):
        return TermPolicyResult(query=folded, unclosed_parens=True)
    scan = _Scan()
    query = _policy_level(folded, scan)
    if query is not None and query.strip():
        return TermPolicyResult(query=query, warnings=scan.warnings)
    # Nothing survived, and now the only way that happens is a term Scryfall refused: a dangling
    # operator is REWRITTEN rather than dropped (_dangling_operator_term), so `q=t:` no longer
    # empties the query and no longer needs an always-true leaf standing in for it.
    return TermPolicyResult(query=folded, warnings=scan.warnings, all_ignored=True)
