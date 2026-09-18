"""Tests for `GET /symbology/parse-mana`'s cost parser.

Every expectation below is a **golden captured from api.scryfall.com on 2026-08-11**, not a value
worked out by hand, because two of the behaviours being pinned are undocumented: the canonical
colour reordering (`RUW` answers `{U}{R}{W}`) and the emission order (`2XWU` answers `{X}{2}{W}{U}`).
A hand-written expectation for those would only re-assert whatever the implementation happened to do.

The colour cases are exhaustive: all 31 non-empty subsets of WUBRG, each written both forwards and
backwards, so `_canonical_colors` is pinned over its whole domain rather than at a few samples.

A second measurement session on 2026-08-28 added the HYBRID goldens -- 61 more requests, one per row,
driven by `GET /symbology`'s own inventory of 84 symbols rather than by what the parser happened to
accept. That inventory is what the parser is now written against, because the rule it used to carry
("a hybrid has exactly two halves") rejected `{W/U/P}` -- a printed Phyrexian hybrid that four live
cards put in their mana cost.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.scryfall_compat.mana import ManaCostError, parse_mana_cost

GOLDENS = [
    ("", {"cost": None, "colors": [], "cmc": 0.0, "colorless": True, "monocolored": False, "multicolored": False}),
    ("0", {"cost": "{0}", "colors": [], "cmc": 0.0, "colorless": True, "monocolored": False, "multicolored": False}),
    ("2WW", {"cost": "{2}{W}{W}", "colors": ["W"], "cmc": 4.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("XRR", {"cost": "{X}{R}{R}", "colors": ["R"], "cmc": 2.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("W2", {"cost": "{2}{W}", "colors": ["W"], "cmc": 3.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("RX", {"cost": "{X}{R}", "colors": ["R"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    (
        "GWU2",
        {
            "cost": "{2}{G}{W}{U}",
            "colors": ["W", "U", "G"],
            "cmc": 5.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "2XWU",
        {"cost": "{X}{2}{W}{U}", "colors": ["W", "U"], "cmc": 4.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "WUBRGC",
        {
            "cost": "{W}{U}{B}{R}{G}{C}",
            "colors": ["W", "U", "B", "R", "G"],
            "cmc": 6.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    ("CW", {"cost": "{W}{C}", "colors": ["W"], "cmc": 2.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{2/W}", {"cost": "{2/W}", "colors": ["W"], "cmc": 2.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{W/P}", {"cost": "{W/P}", "colors": ["W"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{HW}", {"cost": "{HW}", "colors": ["W"], "cmc": 0.5, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{C}", {"cost": "{C}", "colors": [], "cmc": 1.0, "colorless": True, "monocolored": False, "multicolored": False}),
    ("{S}", {"cost": "{S}", "colors": [], "cmc": 1.0, "colorless": True, "monocolored": False, "multicolored": False}),
    ("11R", {"cost": "{11}{R}", "colors": ["R"], "cmc": 12.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{10}", {"cost": "{10}", "colors": [], "cmc": 10.0, "colorless": True, "monocolored": False, "multicolored": False}),
    (
        "{W/U}{B/R}",
        {
            "cost": "{W/U}{B/R}",
            "colors": ["W", "U", "B", "R"],
            "cmc": 2.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{1}{W/U}{W/U}",
        {"cost": "{1}{W/U}{W/U}", "colors": ["W", "U"], "cmc": 3.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{X}{X}{R}",
        {"cost": "{X}{X}{R}", "colors": ["R"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False},
    ),
    (
        "{B/G}{B/G}",
        {"cost": "{B/G}{B/G}", "colors": ["B", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    ("{HR}{R}", {"cost": "{HR}{R}", "colors": ["R"], "cmc": 1.5, "colorless": False, "monocolored": True, "multicolored": False}),
    ("W", {"cost": "{W}", "colors": ["W"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("U", {"cost": "{U}", "colors": ["U"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("B", {"cost": "{B}", "colors": ["B"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("R", {"cost": "{R}", "colors": ["R"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("G", {"cost": "{G}", "colors": ["G"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("WU", {"cost": "{W}{U}", "colors": ["W", "U"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("UW", {"cost": "{W}{U}", "colors": ["W", "U"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("WB", {"cost": "{W}{B}", "colors": ["W", "B"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("BW", {"cost": "{W}{B}", "colors": ["W", "B"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("WR", {"cost": "{R}{W}", "colors": ["W", "R"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("RW", {"cost": "{R}{W}", "colors": ["W", "R"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("WG", {"cost": "{G}{W}", "colors": ["W", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("GW", {"cost": "{G}{W}", "colors": ["W", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("UB", {"cost": "{U}{B}", "colors": ["U", "B"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("BU", {"cost": "{U}{B}", "colors": ["U", "B"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("UR", {"cost": "{U}{R}", "colors": ["U", "R"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("RU", {"cost": "{U}{R}", "colors": ["U", "R"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("UG", {"cost": "{G}{U}", "colors": ["U", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("GU", {"cost": "{G}{U}", "colors": ["U", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("BR", {"cost": "{B}{R}", "colors": ["B", "R"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("RB", {"cost": "{B}{R}", "colors": ["B", "R"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("BG", {"cost": "{B}{G}", "colors": ["B", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("GB", {"cost": "{B}{G}", "colors": ["B", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("RG", {"cost": "{R}{G}", "colors": ["R", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("GR", {"cost": "{R}{G}", "colors": ["R", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True}),
    (
        "WUB",
        {
            "cost": "{W}{U}{B}",
            "colors": ["W", "U", "B"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "BUW",
        {
            "cost": "{W}{U}{B}",
            "colors": ["W", "U", "B"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WUR",
        {
            "cost": "{U}{R}{W}",
            "colors": ["W", "U", "R"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "RUW",
        {
            "cost": "{U}{R}{W}",
            "colors": ["W", "U", "R"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WUG",
        {
            "cost": "{G}{W}{U}",
            "colors": ["W", "U", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GUW",
        {
            "cost": "{G}{W}{U}",
            "colors": ["W", "U", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WBR",
        {
            "cost": "{R}{W}{B}",
            "colors": ["W", "B", "R"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "RBW",
        {
            "cost": "{R}{W}{B}",
            "colors": ["W", "B", "R"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WBG",
        {
            "cost": "{W}{B}{G}",
            "colors": ["W", "B", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GBW",
        {
            "cost": "{W}{B}{G}",
            "colors": ["W", "B", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WRG",
        {
            "cost": "{R}{G}{W}",
            "colors": ["W", "R", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GRW",
        {
            "cost": "{R}{G}{W}",
            "colors": ["W", "R", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "UBR",
        {
            "cost": "{U}{B}{R}",
            "colors": ["U", "B", "R"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "RBU",
        {
            "cost": "{U}{B}{R}",
            "colors": ["U", "B", "R"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "UBG",
        {
            "cost": "{B}{G}{U}",
            "colors": ["U", "B", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GBU",
        {
            "cost": "{B}{G}{U}",
            "colors": ["U", "B", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "URG",
        {
            "cost": "{G}{U}{R}",
            "colors": ["U", "R", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GRU",
        {
            "cost": "{G}{U}{R}",
            "colors": ["U", "R", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "BRG",
        {
            "cost": "{B}{R}{G}",
            "colors": ["B", "R", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GRB",
        {
            "cost": "{B}{R}{G}",
            "colors": ["B", "R", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WUBR",
        {
            "cost": "{W}{U}{B}{R}",
            "colors": ["W", "U", "B", "R"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "RBUW",
        {
            "cost": "{W}{U}{B}{R}",
            "colors": ["W", "U", "B", "R"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WUBG",
        {
            "cost": "{G}{W}{U}{B}",
            "colors": ["W", "U", "B", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GBUW",
        {
            "cost": "{G}{W}{U}{B}",
            "colors": ["W", "U", "B", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WURG",
        {
            "cost": "{R}{G}{W}{U}",
            "colors": ["W", "U", "R", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GRUW",
        {
            "cost": "{R}{G}{W}{U}",
            "colors": ["W", "U", "R", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WBRG",
        {
            "cost": "{B}{R}{G}{W}",
            "colors": ["W", "B", "R", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GRBW",
        {
            "cost": "{B}{R}{G}{W}",
            "colors": ["W", "B", "R", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "UBRG",
        {
            "cost": "{U}{B}{R}{G}",
            "colors": ["U", "B", "R", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GRBU",
        {
            "cost": "{U}{B}{R}{G}",
            "colors": ["U", "B", "R", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    # --- The hybrid inventory, measured from `GET /symbology` on 2026-08-28 -----------------------
    #
    # All ten PHYREXIAN HYBRIDS. These were 422s here until 2026-08-28: the parser required a hybrid
    # to have exactly two halves, which is right for `{W/U/B}` and wrong for every row below.
    (
        "{W/U/P}",
        {"cost": "{W/U/P}", "colors": ["W", "U"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{W/B/P}",
        {"cost": "{W/B/P}", "colors": ["W", "B"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{U/B/P}",
        {"cost": "{U/B/P}", "colors": ["U", "B"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{U/R/P}",
        {"cost": "{U/R/P}", "colors": ["U", "R"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{B/R/P}",
        {"cost": "{B/R/P}", "colors": ["B", "R"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{B/G/P}",
        {"cost": "{B/G/P}", "colors": ["B", "G"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{R/G/P}",
        {"cost": "{R/G/P}", "colors": ["R", "G"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{R/W/P}",
        {"cost": "{R/W/P}", "colors": ["W", "R"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{G/W/P}",
        {"cost": "{G/W/P}", "colors": ["W", "G"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{G/U/P}",
        {"cost": "{G/U/P}", "colors": ["U", "G"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    # The four cards that actually print one, mana cost as `/cards/search?q=is:phyrexian is:hybrid`
    # gives it (2026-08-28, 4 results, no more): Ajani, Sleeper Agent (DMU); Tamiyo, Compleated Sage
    # (NEO); Nahiri, the Unforgiving (ONE); Lukka, Bound to Ruin (ONE). Every one of these was a 422.
    (
        "{1}{G}{G/W/P}{W}",
        {
            "cost": "{1}{G/W/P}{G}{W}",
            "colors": ["W", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{2}{G}{G/U/P}{U}",
        {
            "cost": "{2}{G/U/P}{G}{U}",
            "colors": ["U", "G"],
            "cmc": 5.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{1}{R}{R/W/P}{W}",
        {
            "cost": "{1}{R/W/P}{R}{W}",
            "colors": ["W", "R"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{2}{R}{R/G/P}{G}",
        {
            "cost": "{2}{R/G/P}{R}{G}",
            "colors": ["R", "G"],
            "cmc": 5.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    # The colorless hybrids, which the two-halves rule also rejected -- not for the count, but
    # because it priced only colours, digits and `P`, and `C` is none of the three. `{C/P}` produces
    # NO colour.
    ("{C/W}", {"cost": "{C/W}", "colors": ["W"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{C/U}", {"cost": "{C/U}", "colors": ["U"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{C/B}", {"cost": "{C/B}", "colors": ["B"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{C/R}", {"cost": "{C/R}", "colors": ["R"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{C/G}", {"cost": "{C/G}", "colors": ["G"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{C/P}", {"cost": "{C/P}", "colors": [], "cmc": 1.0, "colorless": True, "monocolored": False, "multicolored": False}),
    # A TWO-part hybrid may be written either way round and comes back in the listed spelling. A
    # three-part one may not -- see UNPARSEABLE_MESSAGES, where `{U/W/P}` is a 422 though `{W/U/P}`
    # parses.
    ("{U/W}", {"cost": "{W/U}", "colors": ["W", "U"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True}),
    ("{W/2}", {"cost": "{2/W}", "colors": ["W"], "cmc": 2.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{P/W}", {"cost": "{W/P}", "colors": ["W"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{W/C}", {"cost": "{C/W}", "colors": ["W"], "cmc": 1.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{W/G}", {"cost": "{G/W}", "colors": ["W", "G"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True}),
    (
        "{W/U}{U/W}",
        {"cost": "{W/U}{W/U}", "colors": ["W", "U"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    # EMISSION ORDER. Every row here was also requested written the other way round and answered
    # the same, which is what makes it a sort rather than the writing order. The order is
    # `/symbology` catalog order -- the plain colour pips are the one exception, and keep the
    # canonical colour order the goldens above pin. `{G/W}{W/U}` and `{G/U}{W/B}` are the rows a
    # colour-rank sort cannot reach: both put the LATER colour's hybrid first.
    (
        "{G}{G/W}{W}",
        {"cost": "{G/W}{G}{W}", "colors": ["W", "G"], "cmc": 3.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    ("{W}{HW}", {"cost": "{HW}{W}", "colors": ["W"], "cmc": 1.5, "colorless": False, "monocolored": True, "multicolored": False}),
    (
        "{R}{HR}{R/W}",
        {"cost": "{R/W}{HR}{R}", "colors": ["W", "R"], "cmc": 2.5, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    ("{W}{C/P}", {"cost": "{C/P}{W}", "colors": ["W"], "cmc": 2.0, "colorless": False, "monocolored": True, "multicolored": False}),
    ("{C}{C/P}", {"cost": "{C/P}{C}", "colors": [], "cmc": 2.0, "colorless": True, "monocolored": False, "multicolored": False}),
    ("{S}{C}", {"cost": "{C}{S}", "colors": [], "cmc": 2.0, "colorless": True, "monocolored": False, "multicolored": False}),
    (
        "{HR}{HW}",
        {"cost": "{HW}{HR}", "colors": ["W", "R"], "cmc": 1.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{G/W}{W/U}",
        {
            "cost": "{W/U}{G/W}",
            "colors": ["W", "U", "G"],
            "cmc": 2.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{G/U}{W/B}",
        {
            "cost": "{W/B}{G/U}",
            "colors": ["W", "U", "B", "G"],
            "cmc": 2.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{U/B}{W/U}{B/R}",
        {
            "cost": "{W/U}{B/R}{U/B}",
            "colors": ["W", "U", "B", "R"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{C/G}{G/P}{2/G}{G/W}",
        {
            "cost": "{G/W}{C/G}{2/G}{G/P}",
            "colors": ["W", "G"],
            "cmc": 5.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{G}{G/U/P}{U}{G/W}",
        {
            "cost": "{G/W}{G/U/P}{G}{U}",
            "colors": ["W", "U", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{HW}{R}",
        {"cost": "{HW}{R}", "colors": ["W", "R"], "cmc": 1.5, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "{HR}{G/W}",
        {
            "cost": "{G/W}{HR}",
            "colors": ["W", "R", "G"],
            "cmc": 1.5,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{2/W}{G/U}",
        {
            "cost": "{G/U}{2/W}",
            "colors": ["W", "U", "G"],
            "cmc": 3.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{G/U/P}{W/U}",
        {
            "cost": "{W/U}{G/U/P}",
            "colors": ["W", "U", "G"],
            "cmc": 2.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{G/W/P}{G/U/P}",
        {
            "cost": "{G/U/P}{G/W/P}",
            "colors": ["W", "U", "G"],
            "cmc": 2.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{G/W}{G/W/P}",
        {"cost": "{G/W}{G/W/P}", "colors": ["W", "G"], "cmc": 2.0, "colorless": False, "monocolored": False, "multicolored": True},
    ),
    (
        "2{W/U/P}{G}",
        {
            "cost": "{2}{W/U/P}{G}",
            "colors": ["W", "U", "G"],
            "cmc": 4.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "{W/U/P}{W/U/P}",
        {
            "cost": "{W/U/P}{W/U/P}",
            "colors": ["W", "U"],
            "cmc": 2.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "WUBRG",
        {
            "cost": "{W}{U}{B}{R}{G}",
            "colors": ["W", "U", "B", "R", "G"],
            "cmc": 5.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
    (
        "GRBUW",
        {
            "cost": "{W}{U}{B}{R}{G}",
            "colors": ["W", "U", "B", "R", "G"],
            "cmc": 5.0,
            "colorless": False,
            "monocolored": False,
            "multicolored": True,
        },
    ),
]

# Costs api.scryfall.com rejects with a 422 rather than parsing.
UNPARSEABLE = [
    "{T}",
    "{Q}",
]

# What Scryfall SAYS about an unreadable cost, not merely that it says something. Every pair is a
# golden captured from api.scryfall.com on 2026-08-16, one request each, and together they pin the
# rule the wording follows: the reported fragment is the input with everything Scryfall could read
# struck out, adjacent bare characters merge into one run, and a braced token keeps its braces.
UNPARSEABLE_MESSAGES = [
    ("{QQQ}", "{QQQ}"),
    ("{}", "{}"),
    ("{", "{"),
    ("!!!", "!!!"),
    ("é", "É"),
    # A triple hybrid is not a Magic symbol. This one is also the reason the rule above had to be
    # worked out at all: the recognized halves come out and only the punctuation is reported.
    ("{W/U/B}", "{//}"),
    # The boundary the hybrid goldens sit against, one request per row on 2026-08-28. A slash symbol
    # parses only if `GET /symbology` lists it, so a fourth half, the same symbol spelled backwards,
    # a combination that is not printed and a generic half that is not 2 are all 422s -- even though
    # `{W/U/P}` itself parses. These fragments also pin what "everything Scryfall could read" strikes
    # out: the ten one-character symbols and nothing else. `P`, `H` and digits SURVIVE, which the
    # residue rule here used to get wrong by striking whatever the parser could price.
    ("{U/W/P}", "{//P}"),
    ("{P/W/U}", "{P//}"),
    ("{W/U/P/P}", "{//P/P}"),
    ("{C/W/P}", "{//P}"),
    ("{W/W/P}", "{//P}"),
    ("{2/W/P}", "{2//P}"),
    ("{3/W}", "{3/}"),
    ("{S/W}", "{/}"),
    ("{X/W}", "{/}"),
    ("{C/S}", "{/}"),
    # `H` is not a prefix over any colour: `GET /symbology` lists {HW} and {HR}, and no others.
    ("{H/W}", "{H/}"),
    ("{HB}", "{H}"),
    # SEVERAL fragments, which is what the "(s)" in the message is about. They concatenate in written
    # order with NO separator, and the readable symbols between them leave no trace. An earlier pass
    # here asserted a space, which nothing measured supported and `{Q}W{T}` disproves.
    ("{Q}W{T}", "{Q}{T}"),
    ("{Q}{T}", "{Q}{T}"),
    ("!W!", "!!"),
    ("{Q}WW{T}", "{Q}{T}"),
    ("!{Q}!", "!{Q}!"),
    ("{Q} {T}", "{Q}{T}"),
    # `b` is BLACK MANA and reads fine, so only `a` and `{Q}` are reported.
    ("a{Q}b", "A{Q}"),
    # The 51-CHARACTER cap, measured across nine lengths. It is characters and not bytes (51 `é` come
    # back whole at 102 bytes), it applies to the whole joined list rather than per fragment, and
    # there is no ellipsis -- the string simply stops.
    ("a" * 51, "A" * 51),
    ("a" * 52, "A" * 51),
    ("a" * 200, "A" * 51),
    ("é" * 51, "É" * 51),
    ("é" * 60, "É" * 51),
    ("{" + "a" * 50 + "}", ("{" + "A" * 50 + "}")[:51]),
    ("{QQQQQQQQ}" * 10, ("{QQQQQQQQ}" * 10)[:51]),
]


class TestParseManaGoldens:
    """Parity with Scryfall, case by case."""

    @pytest.mark.parametrize(("written", "expected"), GOLDENS)
    def test_matches_scryfall(self, written: str, expected: dict[str, Any]) -> None:
        parsed = parse_mana_cost(written)
        assert parsed["object"] == "mana_cost"
        assert {field: parsed[field] for field in expected} == expected

    @pytest.mark.parametrize("written", UNPARSEABLE)
    def test_a_non_mana_fragment_is_rejected(self, written: str) -> None:
        with pytest.raises(ManaCostError):
            parse_mana_cost(written)

    @pytest.mark.parametrize(("written", "fragment"), UNPARSEABLE_MESSAGES)
    def test_the_rejection_names_the_fragment_scryfall_names(self, written: str, fragment: str) -> None:
        """The `details` string is compared byte for byte by clients, so it is pinned that way."""
        with pytest.raises(ManaCostError) as raised:
            parse_mana_cost(written)
        expected = f"The string fragment(s) “{fragment}” could not be understood as part of mana cost."
        assert str(raised.value) == expected


class TestParseManaProperties:
    """Properties the goldens imply but do not state outright."""

    def test_an_empty_cost_is_null_but_an_explicit_zero_is_not(self) -> None:
        """The two differ upstream, so they cannot share a branch here."""
        assert parse_mana_cost("")["cost"] is None
        assert parse_mana_cost("0")["cost"] == "{0}"

    def test_case_and_whitespace_do_not_change_the_answer(self) -> None:
        assert parse_mana_cost("ruw") == parse_mana_cost("RUW")
        assert parse_mana_cost(" 2 W W ") == parse_mana_cost("2WW")

    def test_generic_pips_are_summed_into_one_symbol(self) -> None:
        assert parse_mana_cost("1{1}")["cost"] == "{2}"

    def test_consecutive_digits_are_one_number(self) -> None:
        """`11R` is eleven generic and a red pip, not two ones."""
        assert parse_mana_cost("11R")["cmc"] == 12.0

    def test_the_colors_list_is_always_wubrg_order(self) -> None:
        """Unlike `cost`, which is reordered canonically, `colors` is not."""
        assert parse_mana_cost("RUW")["cost"] == "{U}{R}{W}"
        assert parse_mana_cost("RUW")["colors"] == ["W", "U", "R"]

    def test_variable_pips_come_out_in_xyz_order_however_they_were_written(self) -> None:
        """`?cost=xyzzy` answers `{X}{Y}{Y}{Z}{Z}` on api.scryfall.com, not writing order."""
        assert parse_mana_cost("xyzzy")["cost"] == "{X}{Y}{Y}{Z}{Z}"
        assert parse_mana_cost("zyx")["cost"] == "{X}{Y}{Z}"

    def test_a_hybrid_is_the_symbols_scryfall_lists(self) -> None:
        """The rule this replaced counted halves, which gets `{W/U/B}` right and `{W/U/P}` wrong.

        Neither "exactly two" nor "two or three" is the boundary -- the inventory is.
        """
        assert parse_mana_cost("{W/U}")["cost"] == "{W/U}"
        assert parse_mana_cost("{W/U/P}")["cost"] == "{W/U/P}"
        assert parse_mana_cost("{2/W}")["cmc"] == 2.0
        assert parse_mana_cost("{W/P}")["cmc"] == 1.0
        assert parse_mana_cost("{W/U/P}")["cmc"] == 1.0
        for written in ("{W/U/B}", "{U/W/P}", "{3/W}"):
            with pytest.raises(ManaCostError):
                parse_mana_cost(written)

    def test_a_phyrexian_hybrid_contributes_both_its_colours_and_one_mana(self) -> None:
        """Ajani, Sleeper Agent -- the whole point of the fix.

        `is:phyrexian is:hybrid` finds four cards and this route used to 422 on all four
        (measured 2026-08-28).
        """
        ajani = parse_mana_cost("{1}{G}{G/W/P}{W}")
        assert ajani["cost"] == "{1}{G/W/P}{G}{W}"
        assert ajani["colors"] == ["W", "G"]
        assert ajani["cmc"] == 4.0

    def test_every_unreadable_fragment_is_named_at_once(self) -> None:
        """The message says "fragment(s)" because it can name more than one -- concatenated, not spaced.

        The exact strings live in UNPARSEABLE_MESSAGES; this states the property they encode.
        """
        with pytest.raises(ManaCostError) as raised:
            parse_mana_cost("{Q}W{T}")
        assert "“{Q}{T}”" in str(raised.value)
