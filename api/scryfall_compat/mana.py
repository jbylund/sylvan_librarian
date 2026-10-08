"""Mana cost parsing for `GET /symbology/parse-mana`.

The one reference endpoint that is computed rather than mirrored: it takes a cost written any way a
human might write it (`RUW`, `2WW`, `{X}{R}{R}`) and returns Scryfall's normalized form plus the
colors, mana value and the three colour-count flags.

Two behaviours here were measured against api.scryfall.com on 2026-08-11 rather than inferred, both
because nothing documents them:

- **The normalized cost reorders colored pips into the canonical colour order**, so `RUW` comes back
  as `{U}{R}{W}` (Jeskai) and not as it was written. `_canonical_colors` is that rule.
- **The emission order is X, then generic, then every other pip in `GET /symbology` catalog
  order**, regardless of where they appeared in the input: `2XWU` normalizes to `{X}{2}{W}{U}`, and
  `CW` to `{W}{C}`. Generic pips are summed into one symbol, so `1{1}` is `{2}`. `_PIP_ORDER` is
  that rule.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

# The colour wheel. Every canonical ordering is a walk around this cycle.
_WUBRG = ("W", "U", "B", "R", "G")
_COLOR_INDEX = {color: index for index, color in enumerate(_WUBRG)}

# Symbols that are mana but not a colour: colorless, snow, and the energy-style pips.
_COLORLESS_PIPS = frozenset({"C", "S"})

# Variable pips, which contribute nothing to mana value.
_VARIABLE_PIPS = frozenset({"X", "Y", "Z"})

_BRACED = re.compile(r"\{([^}]*)\}")

# Half-mana symbols are written {HW}; the half applies to the symbol that follows the H.
# `GET /symbology` lists exactly these two (2026-08-28), and reading `H` as a prefix over any colour
# was one symbol too generous: `?cost={HB}` is a 422 on api.scryfall.com, measured the same day.
_HALF_SYMBOLS = frozenset({"HW", "HR"})
_HALF_MANA = 0.5

# Every hybrid symbol api.scryfall.com knows, in the order `GET /symbology` lists them.
#
# Fetched whole on 2026-08-28: 84 symbols, 36 of them hybrids, and this is all 36. The inventory is
# the rule -- a hybrid parses if and only if it is one of these -- because no rule stated in terms of
# the halves gets the boundary right. This module used to require exactly TWO halves, which rejects
# the ten PHYREXIAN HYBRIDS below: `{W/U/P}` and its nine siblings are printed symbols, and four live
# cards carry one in their mana cost (`is:phyrexian is:hybrid`, 2026-08-28 -- Ajani, Sleeper Agent
# `{1}{G}{G/W/P}{W}`; Tamiyo, Compleated Sage `{2}{G}{G/U/P}{U}`; Nahiri, the Unforgiving
# `{1}{R}{R/W/P}{W}`; Lukka, Bound to Ruin `{2}{R}{R/G/P}{G}`). Loosening the count to "two or three"
# would be just as wrong in the other direction: `{W/U/B}` and `{3/W}` are still 422s.
#
# The order is load-bearing twice over -- it is also the order hybrids are EMITTED in, see _PIP_ORDER.
_HYBRID_SYMBOLS = (
    # Colour pairs.
    "W/U",
    "W/B",
    "B/R",
    "B/G",
    "U/B",
    "U/R",
    "R/G",
    "R/W",
    "G/W",
    "G/U",
    # Phyrexian hybrids -- one of two colours, or 2 life. All ten colour pairs exist.
    "B/G/P",
    "B/R/P",
    "G/U/P",
    "G/W/P",
    "R/G/P",
    "R/W/P",
    "U/B/P",
    "U/R/P",
    "W/B/P",
    "W/U/P",
    # Colorless hybrids.
    "C/W",
    "C/U",
    "C/B",
    "C/R",
    "C/G",
    # Twobrid.
    "2/W",
    "2/U",
    "2/B",
    "2/R",
    "2/G",
    # Phyrexian.
    "W/P",
    "U/P",
    "B/P",
    "R/P",
    "G/P",
    "C/P",
)

# Every SPELLING that names one of those symbols, mapped to the spelling Scryfall answers with.
#
# A two-part hybrid may be written either way round and comes back canonical -- measured one request
# each on 2026-08-28, once per family: `{U/W}`->`{W/U}`, `{W/2}`->`{2/W}`, `{P/W}`->`{W/P}`,
# `{W/C}`->`{C/W}`. A three-part one may NOT: `{U/W/P}` and `{P/W/U}` are both 422s where `{W/U/P}`
# parses, so the ten Phyrexian hybrids are accepted only as spelled above.
_HYBRID_CANONICAL = {
    spelling: symbol
    for symbol in _HYBRID_SYMBOLS
    for spelling in ((symbol, "/".join(reversed(symbol.split("/")))) if symbol.count("/") == 1 else (symbol,))
}

# The order pips are EMITTED in: `GET /symbology` catalog order, for everything a cost can carry
# besides generic and variable pips (fetched 2026-08-28).
#
# Measured, one request per row on 2026-08-28, each written both ways round to prove it is a sort and
# not the writing order::
#
#     ?cost={G}{G/W}{W}    {G/W}{G}{W}     a hybrid comes out ahead of a plain pip of its colour
#     ?cost={W}{HW}        {HW}{W}         so does a half pip
#     ?cost={R}{HR}{R/W}   {R/W}{HR}{R}    and a hybrid comes out ahead of a half pip
#     ?cost={W}{C/P}       {C/P}{W}        a colourless hybrid sorts with the hybrids, not at the end
#     ?cost={S}{C}         {C}{S}          the colorless pips have an order of their own
#
# Between two hybrids it is THIS list's order and not the colour order, which is the one thing a
# colour-rank sort cannot express: `{G/W}{W/U}` answers `{W/U}{G/W}` and `{G/U}{W/B}` answers
# `{W/B}{G/U}`, both of which put the later colour first. Same for half pips: `{HR}{HW}` answers
# `{HW}{HR}` though Boros orders R before W.
#
# The five PLAIN colour pips are the exception, and the only one: `RUW` answers `{U}{R}{W}`, so they
# come out in canonical colour order rather than catalog order. They occupy five consecutive catalog
# slots, so ranking them within that block says exactly that -- see `_PLAIN_PIP_AT`.
_PIP_ORDER = (*_HYBRID_SYMBOLS, "HW", "HR", "W", "U", "B", "R", "G", "C", "S")
_PIP_INDEX = {symbol: index for index, symbol in enumerate(_PIP_ORDER)}
_PLAIN_PIP_AT = _PIP_INDEX["W"]

# How many CHARACTERS of the joined fragment list the error names before Scryfall cuts it.
#
# 51, and it is characters rather than bytes -- measured across nine lengths on 2026-08-16: 51 `a`s
# come back whole and 52 come back as 51, while 51 `é`s (102 bytes) also come back whole and 60 come
# back as 51 characters / 102 bytes. The cut applies to the WHOLE joined list rather than per
# fragment: ten separate `{QQQQQQQQ}` tokens come back as 51 characters of the concatenation. There
# is no ellipsis, unlike `/cards/collection`'s 30-character echo -- the string simply stops. 51 is an
# odd bound and nothing here explains it, which is why the measurement travels with the constant.
_FRAGMENT_ECHO_LIMIT = 51


class ManaCostError(ValueError):
    """A fragment of the cost could not be understood as mana."""


class _UnparseableSymbolError(Exception):
    """One token is not mana. Internal; never raised out of this module.

    The message Scryfall sends names **every** unparseable fragment of the cost at once ("The
    string fragment(s) ..."), so the fragments have to be collected before any error can be worded.
    Raising the finished ``ManaCostError`` from ``_symbol_value`` reported only the first, and
    reported it re-braced: ``?cost=!!!`` came back as ``“{!}”`` where api.scryfall.com says ``“!!!”``.
    """


def _canonical_colors(colors: set[str]) -> list[str]:
    """Order a colour set the way Magic writes it.

    Every canonical ordering — allied pairs, enemy pairs, shards, wedges, four-colour runs and
    WUBRG itself — is a walk around the colour wheel taking a constant number of steps: one step for
    anything contiguous (`{G}{W}` for Selesnya, `{W}{U}{B}` for Esper), two for the arrangements that
    are not (`{R}{W}` for Boros, `{U}{R}{W}` for Jeskai). Trying step 1 before step 2, and starting
    points in WUBRG order, picks the same arrangement Scryfall does for all 31 colour combinations.

    Args:
        colors: The colours present, in any order.

    Returns:
        The colours in canonical order.
    """
    if not colors:
        return []
    wanted = {_COLOR_INDEX[color] for color in colors}
    for step in (1, 2):
        for start in range(len(_WUBRG)):
            walk = [(start + offset * step) % len(_WUBRG) for offset in range(len(wanted))]
            if set(walk) == wanted:
                return [_WUBRG[index] for index in walk]
    # Unreachable for any subset of WUBRG, but a colour set that is not one must still come back
    # deterministically rather than as None.
    return [color for color in _WUBRG if color in colors]


def _symbol_value(symbol: str) -> float:
    """The mana value one braced symbol contributes.

    Args:
        symbol: The symbol's inside, without braces, uppercased.

    Returns:
        Its contribution to the total.

    Raises:
        ManaCostError: If the symbol is not mana at all.
    """
    if symbol.isdigit():
        return float(symbol)
    if symbol in _VARIABLE_PIPS:
        return 0.0
    if symbol in _HALF_SYMBOLS:
        return _HALF_MANA
    if "/" in symbol:
        # A hybrid parses if and only if it is one of the 36 symbols Scryfall lists -- see
        # _HYBRID_SYMBOLS for why the inventory rather than a rule about the halves.
        canonical = _HYBRID_CANONICAL.get(symbol)
        if canonical is None:
            raise _UnparseableSymbolError
        # A hybrid is worth its most expensive part: {2/W} is 2, {W/U}, {W/U/P} and {C/P} are 1.
        return max(float(part) if part.isdigit() else 1.0 for part in canonical.split("/"))
    if symbol in _COLORLESS_PIPS or symbol in _COLOR_INDEX:
        return 1.0
    raise _UnparseableSymbolError


def _symbol_colors(symbol: str) -> set[str]:
    """The colours one braced symbol contributes.

    Args:
        symbol: The symbol's inside, without braces, uppercased.

    Returns:
        The colours it produces; empty for generic, variable and colorless pips.
    """
    return {part for part in re.split(r"[/]", symbol.removeprefix("H")) if part in _COLOR_INDEX}


class _Token(NamedTuple):
    """One symbol, plus how it was written -- which the error wording needs and the rules do not."""

    symbol: str
    """The symbol's inside, brace-stripped and uppercased. Every parsing rule reads this."""
    spelling: str
    """How it was WRITTEN, uppercased: a braced token keeps its braces, a bare character does not."""
    braced: bool
    """True for a braced token, which is what stops `!!` and `{!}{!}` merging into one fragment."""


def _tokenize(raw: str) -> list[_Token]:
    """Split a written cost into braced-symbol contents.

    Unbraced runs are read a character at a time, except for digits, which group so `11R` is
    `{11}{R}` rather than `{1}{1}{R}`.

    Args:
        raw: The cost as written.

    Returns:
        One entry per symbol.
    """
    tokens: list[_Token] = []
    position = 0
    upper = raw.upper()
    while position < len(upper):
        braced = _BRACED.match(upper, position)
        if braced:
            tokens.append(_Token(braced.group(1).strip(), braced.group(0), braced=True))
            position = braced.end()
            continue
        char = upper[position]
        if char.isdigit():
            digits = re.match(r"\d+", upper[position:]).group(0)
            tokens.append(_Token(digits, digits, braced=False))
            position += len(digits)
            continue
        if not char.isspace():
            tokens.append(_Token(char, char, braced=False))
        position += 1
    return tokens


def _reported_fragment(token: _Token) -> str:
    """How Scryfall names a part of the cost it could not read.

    Measured one request per row against api.scryfall.com on 2026-08-16::

        ?cost=!!!       “!!!”     three bare characters, reported as ONE run
        ?cost=é         “É”       uppercased, and reported as itself rather than re-braced
        ?cost={QQQ}     “{QQQ}”   a braced token keeps its braces
        ?cost={}        “{}”      including the empty one
        ?cost={W/U/B}   “{//}”    the RECOGNIZED halves are struck out and the residue reported

    The last row is the rule the others are a degenerate case of: what comes back is the fragment
    with everything Scryfall could read removed. `{QQQ}` keeps all three Qs because none of them is
    a symbol; `{W/U/B}` keeps only its punctuation.

    What counts as "could read" is exactly the ten ONE-CHARACTER mana symbols -- the five colours,
    `{C}`, `{S}` and the three variables. `P`, `H` and digits are NOT struck, which this used to get
    wrong by inferring the set from what the parser prices rather than measuring it. Five more rows,
    one request each on 2026-08-28, all of them costs the inventory above now rejects::

        ?cost={U/W/P}   “{//P}”    a Phyrexian hybrid spelled backwards -- the P survives
        ?cost={2/W/P}   “{2//P}”   and so does the generic half
        ?cost={3/W}     “{3/}”     there is no {3/W}; only {2/X} twobrids exist
        ?cost={H/W}     “{H/}”     H survives too
        ?cost={HB}      “{H}”      there is no {HB} either; only {HW} and {HR}

    Args:
        token: The token that could not be parsed.

    Returns:
        The fragment as Scryfall would name it.
    """
    if not token.braced:
        return token.spelling
    residue = "".join(
        char for char in token.symbol if char not in _COLOR_INDEX and char not in _COLORLESS_PIPS and char not in _VARIABLE_PIPS
    )
    return f"{{{residue}}}"


def parse_mana_cost(raw: str) -> dict[str, Any]:
    """Build Scryfall's ManaCost object for a written cost.

    Args:
        raw: The cost as the client wrote it.

    Returns:
        The ManaCost object.

    Raises:
        ManaCostError: If a fragment is not mana.
    """
    tokens = _tokenize(raw or "")

    generic = 0
    variables: list[str] = []
    # One list for every pip that is neither generic nor variable -- colored, colorless and hybrid
    # alike -- because their emission order is one catalog order and not three buckets: `{W}{C/P}`
    # answers `{C/P}{W}`, so a colourless hybrid comes out AHEAD of a coloured pip.
    pips: list[str] = []
    color_set: set[str] = set()
    total = 0.0

    # EVERY unparseable fragment is collected before any error is raised, because Scryfall's message
    # names them all at once -- CONCATENATED IN ORDER WITH NO SEPARATOR, and the readable symbols
    # between them do not separate them either. Measured 2026-08-16, one request per row::
    #
    #     ?cost={Q}W{T}   “{Q}{T}”   the readable {W} between them leaves no trace
    #     ?cost=!W!       “!!”       same, for bare characters
    #     ?cost=!{Q}!     “!{Q}!”    braced and bare interleave in written order
    #     ?cost=a{Q}b     “A{Q}”     `b` is BLACK MANA and readable, so only two fragments
    #
    # An earlier pass joined the fragments with a space, which no measurement supported and which
    # `{Q}W{T}` disproves. One accumulated string is now the whole mechanism -- with an empty
    # separator there is nothing left for a per-fragment merge rule to do.
    bad = ""
    for token in tokens:
        try:
            total += _symbol_value(token.symbol)
        except _UnparseableSymbolError:
            bad += _reported_fragment(token)
            continue
        color_set |= _symbol_colors(token.symbol)
        if token.symbol.isdigit():
            generic += int(token.symbol)
        elif token.symbol in _VARIABLE_PIPS:
            variables.append(token.symbol)
        else:
            # A hybrid is emitted in the spelling Scryfall answers with, not the one it was written in.
            pips.append(_HYBRID_CANONICAL.get(token.symbol, token.symbol))

    if bad:
        msg = f"The string fragment(s) “{bad[:_FRAGMENT_ECHO_LIMIT]}” could not be understood as part of mana cost."
        raise ManaCostError(msg)

    colors = _canonical_colors(color_set)
    # An empty cost is null, but a cost that was written and happens to be free is `{0}`: Scryfall
    # answers `cost=` with null and `cost=0` with "{0}", so the two cannot share a branch.
    cost = _render_cost(variables, generic, pips, colors) if tokens else None

    return {
        "object": "mana_cost",
        "cost": cost,
        "colors": [color for color in _WUBRG if color in color_set],
        "cmc": total,
        "colorless": not color_set,
        "monocolored": len(color_set) == 1,
        "multicolored": len(color_set) > 1,
    }


def _render_cost(
    variables: list[str],
    generic: int,
    pips: list[str],
    colors: list[str],
) -> str | None:
    """Assemble the normalized cost string.

    Args:
        variables: X/Y/Z pips, in the order written.
        generic: Summed generic mana.
        pips: Every other symbol, in the order written.
        colors: The canonical colour order the five plain colour pips come out in.

    Returns:
        The normalized cost. A cost whose symbols all cancel to nothing renders as `{0}`.
    """
    rank = {color: index for index, color in enumerate(colors)}

    def sort_key(symbol: str) -> int:
        # Catalog order, with the five plain colour pips ranked inside their own block of the
        # catalog so that they alone come out in canonical colour order. Every pip that reaches here
        # is a symbol Scryfall lists, so the lookup always hits.
        if symbol in rank:
            return _PLAIN_PIP_AT + rank[symbol]
        return _PIP_INDEX.get(symbol, len(_PIP_ORDER))

    ordered = sorted(pips, key=sort_key)
    # Variables come out in X, Y, Z order regardless of how they were written, and repeats group:
    # `?cost=xyzzy` is `{X}{Y}{Y}{Z}{Z}` on api.scryfall.com (measured 2026-08-16) where writing
    # order gives `{X}{Y}{Z}{Z}{Y}`. A plain sort does both at once -- the alphabet and the pip
    # order coincide.
    parts = [f"{{{symbol}}}" for symbol in sorted(variables)]
    if generic:
        parts.append(f"{{{generic}}}")
    parts.extend(f"{{{symbol}}}" for symbol in ordered)
    return "".join(parts) or "{0}"
