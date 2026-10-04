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

from api.enums import CardOrdering, PreferOrder, SortDirection
from api.parsing.db_info import ALIAS_TO_FIELD_INFOS
from api.parsing.query_budget import InvalidRegexPatternError, QueryBudgetExceeded
from api.parsing.regex_budget import _enforce_pattern_limits
from api.parsing.rewrite import _regex_plain_literal

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
#
# `direct` LEFT IT for the opposite reason: Scryfall does not know it either (`direct:x e:khm t:god`
# is 12 carrying `Unknown keyword \u201cdirect\u201d.`, measured 2026-10-03), so the unknown-keyword rule is
# the right answer and this table was claiming otherwise. `include` left it too: it is a display
# option, read by `_display_option` below.
#
# EIGHTEEN MORE JOINED IT, found by probing `<keyword>:<value> e:khm t:god` (12 cards) for every
# keyword Scryfall's syntax is known to carry, 2026-10-03. Each is HONORED there -- the count
# moves, or the answer is a plain 404 with no `warnings` key -- and each was being dropped here with
# `Unknown keyword`, which is the one thing this table exists to prevent: a query answered WIDER
# than Scryfall answers it, under a sentence saying Scryfall would have ignored the term too. A
# client that validates queries here and ships them there read that warning as "safe to send".
#
#   block b edition           honored: `block:khm` and `edition:khm` are the 12
#   lore                      honored: `lore:x` is 7
#   artists                   honored: `artists:1` is the 12
#   mtgoid multiverseid arenaid tcgplayerid      404 for id 1 in that set
#   prints sets paperprints papersets illustrations edhrec usdfoil collector collectornumber
#                             404 for the probe value
#
# Under a comparison they were already honored-and-empty, by the _COMPARABLE_KEYWORDS rule, and
# still are: narrower than Scryfall's count, never wider. `cardmarketid` and `flavorname` are NOT
# here -- Scryfall calls both unknown.
#
# AND SEVEN WHOSE PARSER COLUMNS ARE ON OTHER BRANCHES: `fo` / `fulloracle`, `st` / `settype` /
# `set_type`, `pt` / `powtou`. Scryfall honors every one (the first five are rows of the
# 2026-08-16 enumeration under _COMPARABLE_KEYWORDS; `pt:6` and `powtou:6` are each 2,724, measured
# 2026-10-03), and on a tree without their columns the unknown-keyword rule was dropping them. The
# moment a branch adds the alias the parser answers the term and this entry is simply never read.
_SCRYFALL_ONLY_KEYWORDS = frozenset(
    {
        "game",
        "in",
        "cube",
        "new",
        "not",
        "stamp",
        "cheapest",
        "block",
        "b",
        "edition",
        "lore",
        "artists",
        "mtgoid",
        "multiverseid",
        "arenaid",
        "tcgplayerid",
        "prints",
        "sets",
        "paperprints",
        "papersets",
        "illustrations",
        "edhrec",
        "usdfoil",
        "collector",
        "collectornumber",
        "fo",
        "fulloracle",
        "st",
        "settype",
        "set_type",
        "pt",
        "powtou",
    }
)

# EVERY DISPLAY OPTION IS READ HERE, on the query text, and none of them is a term.
#
# `unique:`, `order:`/`sort:`, `direction:`/`dir:` and `prefer:` are this parser's directives (#893)
# and `/search` reads them through the parser. On this surface three things about them were not
# Scryfall's, all measured on api.scryfall.com 2026-10-03 (`t:goblin` = 561):
#
# 1. THE SENTENCE. An unknown value is a `warnings` entry worded by Scryfall, where this surface
#    sent `/search`'s `Unknown unique mode "nonsense" was ignored` (straight quotes, and "direction"
#    / "prefer choice" for two of the nouns):
#
#      unique:nonsense          Unknown unique mode \u201cnonsense\u201d was ignored
#      order:nonsense  sort:\u2026   Unknown order choice \u201cnonsense\u201d was ignored
#      direction:\u2026     dir:\u2026    Unknown direction choice \u201cnonsense\u201d was ignored
#      prefer:nonsense          Unknown preference mode \u201cnonsense\u201d was ignored
#      display:\u2026       as:\u2026     Unknown display mode \u201cnonsense\u201d was ignored
#
#    with the value lower-cased and cut to ten characters, dots included (`unique:abcdefghijklmnop`
#    -> \u201cabcdefg...\u201d; `prefer:abcdefghij`, exactly ten, comes back whole). A QUOTED value is unknown
#    -- `unique:"prints"` and `order:"cmc"` each warn, quotes echoed -- and a `-` changes nothing.
#
# 2. A QUERY OF NOTHING BUT OPTIONS. `unique:prints`, `order:cmc`, `prefer:oldest`, `display:grid`
#    and `unique:prints order:cmc` are each `400 All of your terms were ignored.` with `warnings:
#    null` (`unique:nonsense` alone carries its warning). This surface answered the whole corpus.
#    Removing the options from the query text is what makes that fall out: nothing is left.
#
# 3. `unique=prints` IS NOT AN OPTION. It is 561 carrying `Unknown keyword \u201cunique\u201d.`, as
#    `include=extras` and `display=grid` are; this surface answered `400 Failed to parse query`.
#    Only `:` makes a display option, which is why none of these names is in _KNOWN_KEYWORDS.
#
# `display:`/`as:` change nothing an API response shows; `grid`, `checklist`, `full`, `text` and
# `images` are accepted silently and this surface called the keyword unknown.
#
# INSIDE PARENTHESES every one of them is Scryfall's own 400, `Display options may not be specified
# inside parentheses.`, decided before the value is read (`(unique:nonsense t:goblin)` is that 400
# and not a warning). A dangling `sort:` and `sort=value` are not options and do not earn it.
#
# WHAT STAYS THIS PROJECT'S OWN: the values its tables hold that Scryfall's do not --
# `unique:artwork` / `card` / `printing`, `order:cubecobra`, the extra `prefer:` modes -- are honored
# where Scryfall warns, the same superset the `order=` parameter keeps. And `order:penny` /
# `order:review`, which Scryfall sorts by and this server cannot, keep the parameter's own sentence.
#
# The vocabularies are built from the enums `_fold_directives` builds its tables from, rather than
# imported from `api_resource`: that module needs the compiled engine, and this file is importable
# without it. `test_scryfall_cards_routes.py` asserts the two stay equal.
_DISPLAY_OPTION_VALUES: dict[str, tuple[str, frozenset[str]]] = {
    "unique": ("unique mode", frozenset({"card", "cards", "printing", "printings", "prints", "art", "artwork"})),
    "order": ("order choice", frozenset(str(member) for member in CardOrdering)),
    "sort": ("order choice", frozenset(str(member) for member in CardOrdering)),
    "direction": ("direction choice", frozenset(str(member) for member in SortDirection)),
    "dir": ("direction choice", frozenset(str(member) for member in SortDirection)),
    "prefer": ("preference mode", frozenset(str(member) for member in PreferOrder) | {"usd-low", "usd-high"}),
}

# `display:` / `as:` -- Scryfall's page layouts, which no API response shows.
_DISPLAY_MODE_KEYWORDS = frozenset({"display", "as"})
_DISPLAY_MODES = frozenset({"grid", "checklist", "full", "text", "images"})

# The two orders Scryfall sorts by and this server cannot. routes.py words the `order=` parameter's
# warning with the same sentence, and imports this tuple so there is one list.
SCRYFALL_ONLY_ORDERS = ("penny", "review")

# `include:` -- SCRYFALL'S IN-QUERY SPELLING OF `include_extras` AND ITS TWO SIBLINGS, a display
# option like `unique:` and `order:`, and one this surface refused outright: `name:/^reset$/
# include:extras` is 200 / 1 card on api.scryfall.com and was `400 Failed to parse query` here.
# Measured 2026-10-03, base `cmc=3` = 8,089 (8,302 with `include_extras=true`), reading the three
# flags back out of `next_page`:
#
#   include:extras    include:extra         8,302   extras=true
#   include:variations  include:variation   8,089   variations=true
#   include:multilingual                    8,090   multilingual=true
#   include:all       include:everything    8,302   extras=true variations=true multilingual=true
#   include:funny     include:digital       8,089   accepted, silently, and nothing observable moves
#   include:foo  foreign  tokens  any  none  both  prints  true  1
#                                           8,089 + `Unknown direction choice \u201cfoo\u201d was ignored`
#
# THE WARNING REALLY DOES SAY "direction choice" -- Scryfall's sentence for an unknown `direction:`
# value, reused. The value is echoed lower-cased (`include:FOO` -> \u201cfoo\u201d), quotes and all
# (`include:"extras"` is NOT `extras`: it warns about \u201c"extras"\u201d), and cut to ten characters with
# three ASCII dots (`include:extras,variations` -> \u201cextras,...\u201d), not the 20 and the `\u2026` an ignored
# expression gets.
#
# IT IS A DISPLAY OPTION, with everything that follows from that:
#
#   -include:extras t:goblin cmc=0     20     a `-` changes nothing (`-include:foo` warns the same)
#   include:extras cmc=3 &include_extras=false   8,302   the option beats the parameter
#   (include:extras t:goblin) cmc=0    400 `Display options may not be specified inside parentheses.`
#   include:extras                     400 `All of your terms were ignored.`, `warnings: null`
#   include:extras or t:goblin cmc=0   20     removed before the connectors are read
#   include:extras include:variations  both flags
#
# And only under `:`. `include=extras t:goblin` is 561 with `Unknown keyword \u201cinclude\u201d.` -- an
# ordinary unknown keyword -- and `include>extras` is the honored-and-empty comparison every
# unknown keyword is.
_INCLUDE_KEYWORD = "include"

# What one `include:` value switches on. An empty tuple is a value Scryfall accepts and ignores.
_INCLUDE_VALUES: dict[str, tuple[str, ...]] = {
    "extras": ("extras",),
    "extra": ("extras",),
    "variations": ("variations",),
    "variation": ("variations",),
    "multilingual": ("multilingual",),
    "all": ("extras", "variations", "multilingual"),
    "everything": ("extras", "variations", "multilingual"),
    "funny": (),
    "digital": (),
}

# The keywords Scryfall reads as display options under `:`.
_DISPLAY_KEYWORDS = frozenset(_DISPLAY_OPTION_VALUES) | _DISPLAY_MODE_KEYWORDS | {_INCLUDE_KEYWORD}

# How much of an unknown display-option value Scryfall echoes: ten characters, dots included.
_DISPLAY_VALUE_ECHO_LIMIT = 10

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
# (`edhrec>=5000 e:khm t:creature` = 112) and this parser has no spelling for. Under this rule they
# answer the 404 an unknown keyword answers. That is narrower than Scryfall's count, and putting
# them in the set would be worse -- a term kept for a keyword the parser cannot lex is a 400. (Under
# `:`/`=` they are in _SCRYFALL_ONLY_KEYWORDS, and left to fail to parse rather than be dropped.)
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
# The keywords whose value is a UUID: the oracle card's, the PRINTING's own id, and its artwork's.
#
# `scryfallid:` and `illustrationid:` are Scryfall keywords this surface called unknown. Measured
# on api.scryfall.com 2026-10-03, anchor `e:khm t:god` = 12:
#
#   scryfallid:860aa0fe-0337-458c-b864-5ef5733fbae6        1 card (Reset, me3/48)
#   scryfall_id:\u2026  scryfallid=\u2026  SCRYFALLID:860AA0FE-\u2026  scryfallid:"860aa0fe-\u2026"    the same 1
#   illustrationid:9e42d409-161d-4e63-8982-71e313f27b2f    1 card; 2 under unique=prints
#   scryfallid:abc e:khm t:god                             12 + `You must provide a valid v4 UUID.`
#   scryfallid:00000000-0000-0000-0000-000000000000        400, the same sentence (the nil UUID)
#   scryfallid:860aa0fe0337458cb8645ef5733fbae6            400 (no hyphens)
#   -scryfallid:abc e:khm t:god                            12, echoing \u201c-scryfallid:abc\u201d
#   scryfallid:11111111-1111-4111-8111-111111111111        404 -- well-formed, names nothing
#   scryfallid!=<id> e:khm t:god   scryfallid><id> \u2026       404 -- the comparison rule above
#
# The same v4 check `oracleid:` has, with the same sentence. A well-formed id is KEPT for the
# parser, whose `scryfall_id` / `illustration_id` columns arrive with their own PR; on a tree
# without them the term fails to parse, which is this file's answer for every keyword Scryfall
# honors and this project cannot -- never "Unknown keyword".
_UUID_KEYWORDS = frozenset({"oracleid", "oracle_id", "scryfallid", "scryfall_id", "illustrationid", "illustration_id"})
_COLOR_KEYWORDS = frozenset({"c", "color", "colors", "ci", "id", "identity", "produces"})

# Every keyword this file may NOT call unknown: the parser's own aliases and the ones the
# validators below have rules for. The display-option names are deliberately absent: under `:` they
# never reach the keyword rule, and under `=` Scryfall calls them unknown.
#
# The last group is load-bearing rather than belt-and-braces: `lang:` and `oracleid:` arrive with
# #926 and this branch does not have them yet, so without it a `lang:zz` on a tree without that PR
# would be reported as an unknown KEYWORD rather than an unknown LANGUAGE — the right status with
# the wrong sentence, changing under it when an unrelated branch merged.
_KNOWN_KEYWORDS = (
    (frozenset(ALIAS_TO_FIELD_INFOS) - _DISPLAY_KEYWORDS)
    | _MANA_VALUE_KEYWORDS
    | _NEGATED_EQUALITY_UNKNOWN_KEYWORD
    | _NEGATION_HONORING_COMPARISONS
    | _COMPARABLE_KEYWORDS
    | _DATE_KEYWORDS
    | _FORMAT_KEYWORDS
    | _LANGUAGE_KEYWORDS
    | _RARITY_KEYWORDS
    | _UUID_KEYWORDS
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
    #: A display option sits inside parentheses -- Scryfall's own 400, "Display options may not be
    #: specified inside parentheses.", decided before any value is read.
    nested_display_option: bool = False
    #: The display options the query carried, lifted OUT of `query`, as the `(name, value, nested)`
    #: triples `_fold_directives` takes. Only values this server's tables hold; an unknown one is a
    #: warning instead. `nested` is always False: a nested option is the 400 above.
    directives: list[tuple[str, str, bool]] = field(default_factory=list)
    #: `include:extras` / `include:all` -- switches `include_extras` on, whatever the parameter says.
    include_extras: bool = False
    #: `include:variations` / `include:all`.
    include_variations: bool = False
    #: `include:multilingual` / `include:all`.
    include_multilingual: bool = False


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


# SCRYFALL'S REGEX DIALECT IS POSTGRESQL'S, and what PostgreSQL's compiler refuses, Scryfall ignores
# with the compiler's own sentence.
#
# This file used to say Scryfall compiles in Ruby (Onigmo). The sentences it was already quoting say
# otherwise: "brackets [] not balanced", "quantifier operand invalid", "invalid repetition
# count(s)" and "invalid escape \ sequence" are PostgreSQL's regex error strings word for word
# (regerrs.h: REG_EBRACK, REG_BADRPT, REG_BADBR, REG_EESCAPE). Measured on api.scryfall.com
# 2026-10-03, anchor `t:instant` = 3,909, pattern `destroy target creature` = 152 -- a row at 3,909
# was dropped:
#
#   `Invalid regular expression: quantifier operand invalid.`
#     (?i)destroy...  destroy(?i) ...  (?-i)...  (?i:destroy)  (?s:destroy)       inline flags, anywhere
#     (?<a>destroy)  (?P<a>destroy)  (?'n'destroy)  (?>destroy)  (?|destroy)   named, atomic, reset
#     destroy++  destroy*+  destroy?+  destroy+*  destroy{1}+  destroy{1}{2}   a quantified quantifier
#     destroy???                                                               (one lazy `?` is fine)
#     ^*destroy  destroy$*  destroy\y+  ...creature\b{2}  (?=d)*destroy          a quantified constraint
#     destroy(*)  destroy |*  {2}a  a|{2}  (?#x)*a                             nothing to quantify
#
#   `Invalid regular expression: invalid escape \ sequence.`
#     \p{L}  \h  \z  \Z  \k  \g1  \Q...\E  [\p{L}]  bare \x  \xg  \u12  bare \c
#     -- every letter probed, one request each: the escapes that RUN are a b d e f m n r s t v w y
#     in either case (the pattern is lower-cased before it is compiled), `\x` + hex, `\u` + four
#     hex, `\c` + a character, and a backslash before anything that is not a letter.
#
#   RUN: (?:...) (?=...) (?!...) (?<=...) (?<!...) (?#...)   a*? a+? a?? a{1}? a{2,}?   [[:alpha:]]
#        \y \m \M \b \B   \x20 \u0020 \cA   {,2} and {r} (a `{` not followed by a digit is literal)
#
# This surface ran every row of the first two groups that the engine's `regex` / `fancy_regex`
# take, so a query using `(?i)` or a named group was validated here and silently lost its regex on
# Scryfall.
#
# THE FIRST ERROR, LEFT TO RIGHT, IS THE ONE REPORTED, as a compiler reports it: `(a++` is the
# quantifier sentence and `a)++` the parenthesis one; `[a++` is "brackets" (the class swallowed the
# rest) and `a++[` the quantifier; `(?<x` is the quantifier sentence, not "not balanced";
# `a{2,1}++` is the repetition count. One scan reproduces that order.
#
# NOT REPRODUCED: `[a-\w]` (`invalid character range`), and anything PostgreSQL rejects that is not
# listed above. Those fall through to the parser's own check below and, failing that, run.
#
# ONE RESIDUE ON THIS BRANCH: `\y`, `\m`, `\M` and `\c` are escapes this scan accepts, as Scryfall
# does, and the parser's regex budget -- which reads patterns with Python's `re` -- does not, so
# they are still ignored with `invalid pattern.` until the budget learns PostgreSQL's escapes (#907).
_QUANTIFIER_OPERAND_REASON = "Invalid regular expression: quantifier operand invalid."
_INVALID_ESCAPE_REASON = "Invalid regular expression: invalid escape \\ sequence."
_PARENS_REASON = "Invalid regular expression: parentheses () not balanced."
_BRACKETS_REASON = "Invalid regular expression: brackets [] not balanced."
_BRACES_REASON = "Invalid regular expression: braces {} not balanced."
_REPETITION_COUNT_REASON = "Invalid regular expression: invalid repetition count(s)."

# PostgreSQL's DUPMAX: `a{255,}` runs and `a{256,}` is `invalid repetition count(s)`.
_MAX_REPETITION_COUNT = 255

# The letters that may follow a backslash on their own. Lower case: Scryfall lower-cases first.
_VALID_ESCAPE_LETTERS = frozenset("abdefmnrstvwy")
# Of those, the zero-width ones -- a quantifier cannot follow them.
_CONSTRAINT_ESCAPE_LETTERS = frozenset("bmy")

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_DIGITS = frozenset("0123456789")
_UNICODE_ESCAPE_LENGTH = 6
_BOUND_RE = re.compile(r"\{([0-9]+)(?:(,)([0-9]*))?")


def _escape_length(pattern: str, i: int) -> int:
    """The length of the escape starting at `pattern[i]` (a backslash), or -1 when PostgreSQL refuses it."""
    n = len(pattern)
    if i + 1 >= n:
        return 1
    following = pattern[i + 1]
    if following not in _ASCII_LETTERS:
        return 2
    letter = following.lower()
    if letter == "x":
        end = i + 2
        while end < n and pattern[end] in _HEX_DIGITS:
            end += 1
        return end - i if end > i + 2 else -1
    if letter == "u":
        digits = pattern[i + 2 : i + _UNICODE_ESCAPE_LENGTH]
        if len(digits) == _UNICODE_ESCAPE_LENGTH - 2 and all(ch in _HEX_DIGITS for ch in digits):
            return _UNICODE_ESCAPE_LENGTH
        return -1
    if letter == "c":
        return 3 if i + 2 < n else -1
    return 2 if letter in _VALID_ESCAPE_LETTERS else -1


def _bracket_end(pattern: str, i: int) -> tuple[int, str | None]:
    """Scan the bracket expression opening at `pattern[i]`.

    Returns:
        `(index just past the closing bracket, None)`, or `(-1, reason)` for the sentence
        PostgreSQL gives a class that never closes or holds an escape it refuses.
    """
    n = len(pattern)
    j = i + 1
    if j < n and pattern[j] == "^":
        j += 1
    if j < n and pattern[j] == "]":
        j += 1
    while j < n:
        char = pattern[j]
        if char == "\\":
            length = _escape_length(pattern, j)
            if length < 0:
                return -1, _INVALID_ESCAPE_REASON
            j += length
        elif char == "[" and j + 1 < n and pattern[j + 1] in ":.=":
            close = pattern.find(f"{pattern[j + 1]}]", j + 2)
            if close == -1:
                return -1, _BRACKETS_REASON
            j = close + 2
        elif char == "]":
            return j + 1, None
        else:
            j += 1
    return -1, _BRACKETS_REASON


def _group_open(pattern: str, i: int) -> tuple[int, bool | None]:
    """Read the group opener at `pattern[i]`, a `(`.

    Returns:
        `(length of the opener, whether the group is a lookaround)`, or `(0, None)` for a `(?`
        extension PostgreSQL has no reading for -- an inline flag, a named, atomic or reset group --
        which it reports as a quantifier with nothing to apply to. A `(?#` comment is not handled
        here.
    """
    if pattern[i + 1 : i + 2] != "?":
        return 1, False
    kind = pattern[i + 2 : i + 3]
    if kind == ":":
        return 3, False
    if kind in ("=", "!"):
        return 3, True
    if kind == "<" and pattern[i + 3 : i + 4] in ("=", "!"):
        return 4, True
    return 0, None


def _bound_end(pattern: str, i: int) -> tuple[int, str | None]:
    """Read the `{m}` / `{m,}` / `{m,n}` bound at `pattern[i]`, a `{` followed by a digit.

    Returns:
        `(index just past the closing brace, None)`, or `(-1, reason)`.
    """
    bound = _BOUND_RE.match(pattern, i)
    if bound is None or pattern[bound.end() : bound.end() + 1] != "}":
        return -1, _BRACES_REASON
    low = int(bound.group(1))
    if bound.group(2) is None:
        high: int | None = low
    elif bound.group(3) == "":
        high = None
    else:
        high = int(bound.group(3))
    if low > _MAX_REPETITION_COUNT or (high is not None and (high > _MAX_REPETITION_COUNT or high < low)):
        return -1, _REPETITION_COUNT_REASON
    return bound.end() + 1, None


def _postgres_syntax_reason(pattern: str) -> str | None:  # noqa: C901, PLR0912 -- one left-to-right scan
    """The first thing PostgreSQL's compiler would refuse in `pattern`, as Scryfall words it.

    Args:
        pattern: The pattern between the slashes.

    Returns:
        The reason sentence, or None when the scan finds nothing to refuse. See the block comment
        above for the measurements.
    """
    # What a quantifier would apply to: nothing yet, an atom, a quantifier, a quantifier already
    # made lazy, or a zero-width constraint.
    kind = "none"
    # One entry per open group: whether it is a lookaround (a constraint once closed).
    groups: list[bool] = []
    n = len(pattern)
    i = 0
    while i < n:
        char = pattern[i]
        if char == "\\":
            length = _escape_length(pattern, i)
            if length < 0:
                return _INVALID_ESCAPE_REASON
            letter = pattern[i + 1 : i + 2].lower()
            kind = "constraint" if length == 2 and letter in _CONSTRAINT_ESCAPE_LETTERS else "atom"  # noqa: PLR2004
            i += length
        elif char == "[":
            i, reason = _bracket_end(pattern, i)
            if reason is not None:
                return reason
            kind = "atom"
        elif char == "(" and pattern[i + 1 : i + 3] == "?#":
            # A comment is transparent: `a(?#x)*` runs and `(?#x)*a` has nothing to quantify.
            close = pattern.find(")", i + 3)
            if close == -1:
                return _PARENS_REASON
            i = close + 1
        elif char == "(":
            length, lookaround = _group_open(pattern, i)
            if lookaround is None:
                return _QUANTIFIER_OPERAND_REASON
            groups.append(lookaround)
            kind = "none"
            i += length
        elif char == ")":
            if not groups:
                return _PARENS_REASON
            kind = "constraint" if groups.pop() else "atom"
            i += 1
        elif char == "|":
            kind = "none"
            i += 1
        elif char in "^$":
            kind = "constraint"
            i += 1
        elif char == "?" and kind == "quantifier":
            kind = "lazy"
            i += 1
        elif char in "*+?" or (char == "{" and pattern[i + 1 : i + 2] in _ASCII_DIGITS):
            # A bound, or one of the three quantifier characters. A `{` not followed by a digit is a
            # literal brace (`{,2}`, `{r}`) and falls through to the atom arm below.
            end, reason = _bound_end(pattern, i) if char == "{" else (i + 1, None)
            if reason is None and kind != "atom":
                reason = _QUANTIFIER_OPERAND_REASON
            if reason is not None:
                return reason
            kind = "quantifier"
            i = end
        else:
            kind = "atom"
            i += 1
    return _PARENS_REASON if groups else None


def _regex_reason(pattern: str) -> str:
    """The older, two-pass check, kept as the fallback for what `_postgres_syntax_reason` does not model.

    It reports four classes, read off api.scryfall.com: `/[unclosed/` and `/[a-/` -> brackets,
    `/(unclosed/` and `/a)/` -> parentheses, `/a{2,1}/` -> repetition, a bare leading quantifier ->
    quantifier. Anything else gets the generic sentence; the alternative is inventing a message per
    malformation, which would be a guess wearing a measurement's clothes.

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


# SCRYFALL REFUSES THREE THINGS ABOUT A REGEX BEFORE IT COMPILES IT, each decided on the pattern's
# CHARACTERS rather than its syntax, and each ignored with a sentence of its own. All measured on
# api.scryfall.com 2026-10-03, anchor `t:instant` = 3,909, one request per row; a row that answers
# 3,909 carrying the sentence is a regex that was dropped.
#
# --- `Regular expression too complex.` -- A SCORE, AND A LENGTH -------------------------------
#
# Lengthening one pattern a character at a time, the last that ran and the first that did not:
#
#   `.` x89 | x90                 `destroy` + `.`x89 + `creature` | the same with x90
#   `a*` x44 | x45                `a+` and `a?` the same 44 | 45
#   `|` x22 | x23                 alone, between one-letter words, between eight-letter words
#   `(a)` x29 + `.`x60 | x30      `a{2}` x14 + `.`x60 | x15      `(?=a)` x9 + `.`x60 | x10
#
# One sum fits every row -- refused at 90:
#
#   `.` 1      `(` 1      `*` `+` `?` `{` 2 each      `|` 4
#
# and the mixed rows confirm it is ONE sum and not six caps: 45 dots + 22 `a*` (89) runs and 46
# (90) does not; 20 pipes + 9 dots (89) runs and + 10 does not; `.*` x29 (87) runs and x30 does
# not. A lookaround costs 3 -- its `(` and its `?`. Everything else weighs nothing: letters and
# digits (150 of them), `)`, `[` `]` `^` `$` `}`, the class escapes, a backreference.
#
# IT COUNTS CHARACTERS AND NOT SYNTAX. An escaped or bracketed operator costs what a live one does:
# `\.` x45 + `.` x45 is refused and x44 runs; `[.]` x30 + 60 dots is refused; `\|` and `[|]` are
# refused at x23. `o:/\(this creature\)/` therefore spends 1, and a mana symbol `{r}` spends 2.
#
# THE LENGTH: 248 characters run and 249 do not, whatever they are (`a`, `1`, `A`, `,`, an accented
# letter -- characters, not bytes) and whatever the keyword. Three things are longer than they
# look, and they are exactly what Ruby's `String#inspect` escapes: a backslash counts 2 (`\.` x82
# runs, x83 is refused), a double quote counts 2, and `#{` counts 3. One is shorter: `--` counts 1.
#
# FIRST of the text rules: `(((a)))` + 90 dots and `a{60}` + 90 dots are "too complex", and so is an
# unbalanced `[` or `(` in front of 90 dots, so it precedes the compiler as well.
#
# --- `Too many nested groups.` -- PARENTHESES THREE DEEP ---------------------------------------
#
#   o:/destroy ((target|another) (nonblack|nonwhite)|that) creature/   116    depth 2 runs
#   o:/destroy ((target (nonblack|nonwhite))|that) creature/           400    alone: nothing is left
#   t:instant o:/destroy (((target))) creature/                        3,909  depth 3
#   t:instant o:/destroy (?:(?:(?:target))) creature/                  3,909  non-capturing counts
#   t:instant o:/(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)(k)/                    404    siblings do not nest
#
# A count of the two characters: a parenthesis that is escaped, or inside a bracket expression, or
# inside a `(?#` comment, opens and closes a level like any other -- `o:/\(\(\(/` and
# `o:/[(][(][(]/` are refused, and `o:/(\)(\)(a)))/`, a real depth of 3, RUNS because each `\)`
# closed one. The counter is not clamped at zero (`o:/())(((a)/` reaches -1, comes back to 2, and
# the compiler speaks instead). Before the compiler: `o:/(((a/` is "nested", not "not balanced".
#
# --- `Too much repetition.` -- THE `{...}` UPPER BOUNDS, ADDED UP, MAY NOT EXCEED 50 -------------
#
#   a{50}  a{0,50}  a{25}b{25}  x{3,4}y{46}        run
#   a{51}  a{0,51}  a{25}b{26}  x{3,4}y{47}        refused
#   (a{10}){10}                                    runs -- a SUM (20), not the product (100)
#
# The UPPER bound counts and an open one counts nothing: `a{25,26}` runs, `a{51,60}` and `a{60,51}`
# are refused, `a{51,}` runs. Characters again: `[{51}]` and `{r}{51}` are refused, `\{51\}` and
# `a{ 51}` run. LAST of the three (`(((a{60})))` is "nested") and still ahead of the compiler
# (`a{60}[` is this sentence, not "brackets [] not balanced").
#
# NOT REPRODUCED: PostgreSQL's own `Invalid regular expression: regular expression is too complex.`
# -- a different sentence, from the compiler -- which eighteen ADJACENT word boundaries earn.
_TOO_COMPLEX_REASON = "Regular expression too complex."
_REGEX_COMPLEXITY_LIMIT = 90
_REGEX_INSPECT_LENGTH_LIMIT = 248
_COMPLEXITY_WEIGHTS = {".": 1, "(": 1, "*": 2, "+": 2, "?": 2, "{": 2, "|": 4}

_NESTED_GROUPS_REASON = "Too many nested groups."
_MAX_REGEX_PAREN_DEPTH = 2

_TOO_MUCH_REPETITION_REASON = "Too much repetition."
_MAX_REGEX_REPETITION_SUM = 50
_REPETITION_BOUND_RE = re.compile(r"\{(?:[0-9]+,)?([0-9]+)\}")


def _too_complex(pattern: str) -> bool:
    """Whether the pattern is over Scryfall's complexity score or its length."""
    score = 0
    length = 0
    n = len(pattern)
    i = 0
    while i < n:
        char = pattern[i]
        following = pattern[i + 1] if i + 1 < n else ""
        score += _COMPLEXITY_WEIGHTS.get(char, 0)
        if char == "-" and following == "-":
            # The pair is one character.
            i += 1
            length += 1
        elif char in '\\"' or (char == "#" and following == "{"):
            length += 2
        else:
            length += 1
        i += 1
    return score >= _REGEX_COMPLEXITY_LIMIT or length > _REGEX_INSPECT_LENGTH_LIMIT


def _nests_too_deep(pattern: str) -> bool:
    """Whether the pattern's parentheses, counted as characters, reach a depth of three."""
    depth = 0
    for char in pattern:
        if char == "(":
            depth += 1
            if depth > _MAX_REGEX_PAREN_DEPTH:
                return True
        elif char == ")":
            depth -= 1
    return False


def _repeats_too_much(pattern: str) -> bool:
    """Whether the upper bounds of the pattern's `{...}` quantifiers add up past fifty."""
    return sum(int(bound) for bound in _REPETITION_BOUND_RE.findall(pattern)) > _MAX_REGEX_REPETITION_SUM


def scryfall_regex_text_reason(pattern: str) -> str | None:
    """Why Scryfall refuses a regex it has not compiled yet, or None.

    The checks it runs on the pattern's TEXT, in the order it runs them.

    Args:
        pattern: The pattern between the slashes.

    Returns:
        The reason sentence, or None.
    """
    if _too_complex(pattern):
        return _TOO_COMPLEX_REASON
    if _nests_too_deep(pattern):
        return _NESTED_GROUPS_REASON
    if _repeats_too_much(pattern):
        return _TOO_MUCH_REPETITION_REASON
    return None


# The character a backreference is rewritten to: one no card's text carries.
_NEVER_IN_CARD_TEXT = "\\x01"


def _neutralize_backreferences(pattern: str) -> str:
    r"""Rewrite each `\<digits>` to a character no card carries.

    A BACKREFERENCE IS ACCEPTED BY SCRYFALL AND NEVER MATCHES ANYTHING. It is not a backreference
    there at all. `t:creature name:/^(.)\1\1/` is a plain 404 on api.scryfall.com and was a refusal
    of the WHOLE query here -- the parser's budget refuses `\1` because the public engine is
    linear-time. The obvious reading, that Scryfall evaluated the pattern and no creature's name
    opens with a tripled letter, is wrong, and the rows that show it are the ones where a real
    backreference WOULD match (2026-10-03, `t:elf` = 698)::

        name:/oo/ t:elf            75      Wood Elves, and 74 more
        name:/(o)\1/ t:elf         404     the same question, asked with a backreference
        name:/(.)\1/ t:elf         404     any doubled letter at all
        o:/(e)\1/ t:elf            404     ...in rules text
        name:/o\1/ t:elf           404     NO GROUP to refer to, and no "invalid backreference"
        name:/^(.)\2/ t:elf        404     a group that does not exist: the same silence
        name:/(o)\0/  name:/(o)\10/   404
        -name:/(.)\1/ t:elf        730     every elf -- the complement of nothing (730, not 698,
                                           because a `name:` regex still switches extras on)
        name:/a\1b/ or t:elf       730     and it composes under `or` as an empty leaf

    So the term is honored and empty. Translating to an equivalent pattern is impossible (there is
    nothing to be equivalent to), and running real backreferences would compute an answer Scryfall
    does not give -- `name:/(o)\1/ t:elf` would be 75. The term is KEPT and each `\<digits>` becomes
    `\x01`: the pattern stays a regex (the `name:` extras trigger still sees one) and compiles on
    the linear engine like any other escape.

    `\\1` is an escaped backslash and a digit, and is left alone.

    Args:
        pattern: The pattern between the slashes.

    Returns:
        The pattern with every backreference replaced, or the pattern itself when it has none.
    """
    out: list[str] = []
    n = len(pattern)
    i = 0
    changed = False
    while i < n:
        char = pattern[i]
        if char != "\\" or i + 1 >= n:
            out.append(char)
            i += 1
        elif pattern[i + 1] not in _ASCII_DIGITS:
            out.append(pattern[i : i + 2])
            i += 2
        else:
            i += 1
            while i < n and pattern[i] in _ASCII_DIGITS:
                i += 1
            out.append(_NEVER_IN_CARD_TEXT)
            changed = True
    return "".join(out) if changed else pattern


def _parser_regex_reason(operator: str, pattern: str) -> str | None:
    """Why the PARSER would refuse this pattern, in Scryfall's words, or None when it would run.

    A PATTERN SCRYFALL WOULD RUN AND THIS ENGINE'S OWN BUDGET WILL NOT is dropped with Scryfall's
    too-complex sentence -- the FAILURE MODE is Scryfall's even where the threshold is not.

    The parser's static budget (`api/parsing/regex_budget.py`, #1047) is the engine's safety bound,
    and it is narrower than Scryfall's in four places: more than 4 lookarounds (Scryfall prices a
    lookaround at 3 of its 90), more than 64 constructs (Scryfall runs 89 dots), an open-ended
    `{m,}` with m over 1 (`o:/a{2,}/` runs there), and more than 256 UTF-8 BYTES (Scryfall counts
    248 CHARACTERS). Left to the parser, any of them refused the WHOLE query -- `Failed to parse
    query` on this surface, a sentence Scryfall has no counterpart for -- even with other terms
    present. Dropping the one term keeps the rest of the query answering and tells the caller, in
    the words a Scryfall client already handles, that the regex was not applied.

    THE RESIDUE, a known deviation: `t:instant o:/(?=d)(?=de)(?=des)(?=dest)(?=destr)destroy target
    creature/` is 152 on api.scryfall.com (2026-10-03) and all of `t:instant` with the warning here.
    The safe direction for a client that validates here and ships to Scryfall: this surface refuses,
    loudly, what Scryfall would have run -- never the reverse. `/search` keeps the budget as its own
    400, unchanged.

    The same call is the well-formedness check: a pattern the budget cannot PARSE is one the engine
    will not compile either, and takes `_regex_reason`'s sentence.

    A metacharacter-free pattern under `:` never reaches the budget -- the rewrite lowers it to a
    plain substring -- so it is exempt here too.

    Args:
        operator: The term's operator; only `:` is lowered.
        pattern: The pattern between the slashes, backreferences already neutralized.

    Returns:
        The reason sentence, or None.
    """
    if operator == ":" and _regex_plain_literal(pattern) is not None:
        return None
    try:
        _enforce_pattern_limits(pattern)
    except QueryBudgetExceeded:
        return _TOO_COMPLEX_REASON
    except InvalidRegexPatternError:
        return _regex_reason(pattern)
    return None


def _classify_regex(term: str, head: str, operator: str, pattern: str) -> tuple[bool, str, str | None]:
    """Decide what becomes of a term whose value is a `/.../` regex literal.

    Args:
        term: The raw text of the term.
        head: Everything before the opening slash: the `-`, the keyword and the operator.
        operator: The term's operator.
        pattern: The pattern between the slashes.

    Returns:
        The `(keep, text, reason)` triple `_classify_leaf` returns.
    """
    # The refusals Scryfall decides on the pattern's text come first -- `o:/(((a/` is "nested", not
    # "not balanced" -- then its compiler's.
    reason = scryfall_regex_text_reason(pattern) or _postgres_syntax_reason(pattern)
    if reason is not None:
        return False, term, reason
    neutralized = _neutralize_backreferences(pattern)
    reason = _parser_regex_reason(operator, neutralized)
    if reason is not None:
        return False, term, reason
    if neutralized != pattern:
        return True, f"{head}/{neutralized}/", None
    return True, term, None


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


def _is_regex_literal(raw_value: str) -> bool:
    """Whether a raw value is a `/.../` regex literal rather than an ordinary value."""
    return len(raw_value) >= _DELIMITED_MINIMUM and raw_value.startswith("/") and raw_value.endswith("/")


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

    # A numeric column asked for something that is not a number. With `:`/`=` Scryfall ignores the
    # term; with a comparison it keeps it and matches nothing (`q=cmc>=notanumber` is a 404, not a
    # 400), so those two answers are different terms rather than one rule.
    if (keyword in _MANA_VALUE_KEYWORDS or keyword in _NEGATED_EQUALITY_UNKNOWN_KEYWORD) and not _is_numeric_value(raw_value):
        if equality:
            if keyword in _MANA_VALUE_KEYWORDS:
                return False, term, _MANA_VALUE_REASON
            return False, term, f"Unknown keyword \u201c{keyword}\u201d."
        return True, _NEVER_MATCHES, None

    return _classify_value(term, keyword, operator, raw_value)


def _classify_value(term: str, keyword: str, operator: str, raw_value: str) -> tuple[bool, str, str | None]:
    """Decide what becomes of a term whose keyword and operator Scryfall accepts, by its value.

    Args:
        term: The raw text of the term, as the client wrote it.
        keyword: The lowercased keyword before the operator.
        operator: The term's operator.
        raw_value: The value exactly as written, so a regex literal keeps its slashes.

    Returns:
        The `(keep, text, reason)` triple `_classify_leaf` returns.
    """
    reason = _value_reason(keyword, _unquote(raw_value))
    if reason is not None:
        return False, term, reason
    # A regex literal Scryfall, or this parser, will not run. Decided here so the answer is
    # Scryfall's ignored term rather than a refusal of the whole query. Not on the colour columns:
    # their validator above has already read the slashes as value characters.
    if _is_regex_literal(raw_value) and keyword not in _COLOR_KEYWORDS:
        return _classify_regex(term, term[: len(term) - len(raw_value)], operator, raw_value[1:-1])
    return True, term, None


def _value_reason(keyword: str, value: str) -> str | None:
    """Why Scryfall refuses this keyword's VALUE, or None when it accepts it.

    Split out of `_classify_leaf` so each half stays readable: that one decides which RULE applies
    to a term, this one applies the per-keyword vocabularies. It no longer takes the operator: the
    comparison rule in `_classify_leaf` answers every keyword Scryfall does not compare before this
    is reached, and the keywords that DO reach it check their value under every operator alike.

    Args:
        keyword: The lowercased keyword before the operator.
        value: The value with one layer of quotes removed.

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
    if keyword in _UUID_KEYWORDS and not _UUID_V4_RE.match(value):
        return "You must provide a valid v4 UUID."
    if keyword in _COLOR_KEYWORDS:
        color_reason = _color_reason(value, keyword)
        if color_reason is not None:
            return color_reason
    return None


@dataclass
class _DisplayOption:
    """What one `<display keyword>:<value>` leaf does. It is never a term: it always leaves the query."""

    #: Scryfall's sentence for a value it does not know, or None.
    warning: str | None = None
    #: The `(name, value, nested)` triple to fold over the request parameters, or None.
    directive: tuple[str, str, bool] | None = None
    #: Which of `extras` / `variations` / `multilingual` an `include:` value switches on.
    include: tuple[str, ...] = ()


def _unknown_display_value(noun: str, raw_value: str) -> str:
    """`Unknown <noun> \u201c<value>\u201d was ignored` -- Scryfall's sentence for a display option's bad value.

    The value is lower-cased and cut to ten characters, the three ASCII dots included.
    """
    value = raw_value.lower()
    if len(value) > _DISPLAY_VALUE_ECHO_LIMIT:
        value = value[: _DISPLAY_VALUE_ECHO_LIMIT - 3] + "..."
    return f"Unknown {noun} \u201c{value}\u201d was ignored"


def _display_option(term: str) -> _DisplayOption | None:
    """Read a leaf as a display option, or return None when it is not one.

    Only `:` with a value makes one: `unique=prints` is an unknown keyword, `unique>prints` the
    honored-and-empty comparison, and a dangling `sort:` the bare word `sort`. A leading `-`
    changes nothing. See _DISPLAY_OPTION_VALUES and _INCLUDE_KEYWORD for the measurements.

    Args:
        term: The raw text of the leaf.

    Returns:
        What the option does, or None for a leaf that is an ordinary term.
    """
    match = _LEAF_RE.match(term)
    if match is None or match.group(3) != ":" or match.group(4) == "":
        return None
    keyword = match.group(2).lower()
    if keyword not in _DISPLAY_KEYWORDS:
        return None
    raw_value = match.group(4)
    value = raw_value.lower()
    if keyword == _INCLUDE_KEYWORD:
        switches = _INCLUDE_VALUES.get(value)
        if switches is None:
            # The DIRECTION sentence, reused -- measured, not a slip.
            return _DisplayOption(warning=_unknown_display_value("direction choice", raw_value))
        return _DisplayOption(include=switches)
    if keyword in _DISPLAY_MODE_KEYWORDS:
        if value in _DISPLAY_MODES:
            return _DisplayOption()
        return _DisplayOption(warning=_unknown_display_value("display mode", raw_value))
    noun, values = _DISPLAY_OPTION_VALUES[keyword]
    if value in values:
        return _DisplayOption(directive=(keyword, value, False))
    if noun == "order choice" and value in SCRYFALL_ONLY_ORDERS:
        return _DisplayOption(warning=f"This server cannot sort by {value!r} yet; sorted by name instead.")
    return _DisplayOption(warning=_unknown_display_value(noun, raw_value))


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
    directives: list[tuple[str, str, bool]] = field(default_factory=list)
    include: set[str] = field(default_factory=set)
    nested_display_option: bool = False


def _policy_level(source: str, scan: _Scan, *, nested: bool = False) -> str | None:
    """Apply the policy to one nesting level, recursing into groups.

    Args:
        source: The text of this level.
        scan: The warnings and display options collected so far.
        nested: Whether this level is inside parentheses.

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
        replacement = _apply_to_piece(piece, scan, nested=nested)
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


def _apply_to_piece(piece: _Piece, scan: _Scan, *, nested: bool) -> _Piece | None:
    """Apply the policy to one piece, or return None when it leaves the query.

    Args:
        piece: One connector, group or leaf.
        scan: The warnings and display options collected so far.
        nested: Whether the piece is inside parentheses.

    Returns:
        What the rebuilt query carries in its place, or None when nothing does. A connector is
        always returned unchanged -- `_drop_orphaned_connectors` decides whether it survives.
    """
    if piece.kind == "connector":
        return piece
    if piece.kind == "group":
        inner = _policy_level(piece.inner or "", scan, nested=True)
        if inner is None:
            return None
        return _Piece(text=f"{piece.prefix}({inner})", kind="group")
    option = _display_option(piece.text)
    if option is not None:
        # Not a term: it leaves the query whatever its value, and the connectors around it are
        # read as if it had never been there (`include:extras or t:goblin` is `t:goblin`).
        scan.nested_display_option = scan.nested_display_option or nested
        scan.include.update(option.include)
        if option.directive is not None:
            scan.directives.append(option.directive)
        if option.warning is not None:
            scan.warnings.append(option.warning)
        return None
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
    result = TermPolicyResult(
        query=folded,
        warnings=scan.warnings,
        nested_display_option=scan.nested_display_option,
        directives=scan.directives,
        include_extras="extras" in scan.include,
        include_variations="variations" in scan.include,
        include_multilingual="multilingual" in scan.include,
    )
    if query is not None and query.strip():
        result.query = query
        return result
    # Nothing survived, and the only ways that happens are a term Scryfall refused and a query of
    # nothing but display options: a dangling operator is REWRITTEN rather than dropped
    # (_dangling_operator_term), so `q=t:` no longer empties the query and no longer needs an
    # always-true leaf standing in for it.
    result.all_ignored = True
    return result
