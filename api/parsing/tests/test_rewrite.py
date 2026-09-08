"""Derived-predicate rewrite (api/parsing/rewrite.py).

A synonym must parse to exactly the same AST as its canonical expansion — verified
against BOTH parsers via the `parse_query` fixture, since the rewrite runs at the shared
post-parse seam. Mappings are validated against Scryfall's live API in
docs/issues/00713-is-tag-recovery.md.
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.nodes import RegexValueNode
from api.parsing.rewrite import _regex_plain_literal

# (synonym query, canonical expansion) — the two must produce identical ASTs.
EQUIVALENCES = [
    ("frame:modern", "frame:2003"),
    ("frame:old", "frame:1993 or frame:1997"),
    ("frame:new", "frame:2003 or frame:2015 or frame:future"),
    ("is:old", "frame:1993 or frame:1997"),
    ("is:new", "frame:2003 or frame:2015 or frame:future"),
    # type / subtype based
    ("is:historic", "t:legendary or t:artifact or t:saga"),
    ("is:permanent", "t:creature or t:artifact or t:enchantment or t:land or t:planeswalker or t:battle"),
    ("is:party", "t:creature (t:cleric or t:rogue or t:warrior or t:wizard or kw:changeling)"),
    ("is:outlaw", "t:assassin or t:mercenary or t:pirate or t:rogue or t:warlock or kw:changeling"),
    ("is:vanilla", 't:creature o=""'),
    ("is:bear", "t:creature pow=2 tou=2 cmc=2"),
    # layout family
    ("is:split", "layout:split"),
    ("is:flip", "layout:flip"),
    ("is:transform", "layout:transform"),
    ("is:mdfc", "layout:modal_dfc"),
    ("is:meld", "layout:meld"),
    ("is:leveler", "layout:leveler"),
    ("is:dfc", "layout:transform or layout:modal_dfc or layout:meld"),
    ("is:colorshifted", "frame:colorshifted"),
    ("is:manland", "t:land o:become o:creature o:/still a.* land/"),
    ("is:creatureland", "t:land o:become o:creature o:/still a.* land/"),
    (
        "is:commander",
        '((t:legendary (toughness>=0 or t:background)) or o:"can be your commander") -banned:commander',
    ),
    ("is:fetchland", "otag:cycle-fetchland"),
    ("is:checkland", "otag:cycle-checkland"),
    ("is:painland", "otag:cycle-painland"),
    ("is:slowland", "otag:cycle-slowland"),
    ("is:bondland", "otag:cycle-bondland"),
    ("is:battleland", "otag:cycle-tangoland"),
    ("is:tangoland", "otag:cycle-tangoland"),
    ("is:shockland", "otag:shockland"),
    ("is:dual", "otag:cycle-abu-dual-land"),
    ("is:canopyland", "otag:cycle-horizon-land"),
    ("is:scryland", "otag:cycle-block-ths-scry-land"),
    ("is:fastland", "otag:cycle-fastland"),
    ("is:triland", "otag:cycle-ala-shardland or otag:cycle-ktk-wedgeland"),
    ("is:triome", "otag:cycle-iko-triome or otag:cycle-snc-triland"),
    ("is:companion", "kw:companion"),
    ("is:class", "t:class"),
    ("is:adventure", "layout:adventure"),
    ("is:bounceland", "otag:bounceland"),
    ("is:filterland", "otag:cycle-hybrid-filterland or otag:cycle-ody-filterland"),
    ("is:storageland", "otag:cycle-fem-storage-land or otag:cycle-mmq-storage-land or otag:cycle-tsp-storage-land"),
    ("is:gainland", "otag:gainland"),
    ("is:frenchvanilla", "otag:french-vanilla"),
    ("is:shadowland", "t:land o:/reveal an? (Plains|Island|Swamp|Mountain|Forest)/"),
    ("is:snarl", "t:land o:/reveal an? (Plains|Island|Swamp|Mountain|Forest)/"),
    ("is:modal", "otag:modal"),
    ("is:bikeland", "otag:cycle-dual-cycling-land"),
    ("is:surveilland", "otag:cycle-dual-surveil-land"),
    ("is:tricycleland", "otag:tricycle-land"),
    ("is:pathway", "otag:cycle-pathway"),
    # composes under negation and inside compounds
    ("-frame:old", "-(frame:1993 or frame:1997)"),
    ("t:goblin frame:modern", "t:goblin frame:2003"),
    ("t:goblin is:party", "t:goblin t:creature (t:cleric or t:rogue or t:warrior or t:wizard or kw:changeling)"),
]


@pytest.mark.parametrize(
    argnames=["synonym", "expansion"],
    argvalues=EQUIVALENCES,
    ids=[s for s, _ in EQUIVALENCES],
)
def test_synonym_expands_to_canonical(parse_query, synonym: str, expansion: str) -> None:
    """Each synonym parses to the same AST as its hand-written expansion (both parsers)."""
    assert parse_query(synonym) == parse_query(expansion)


@pytest.mark.parametrize(
    argnames=["synonym", "expansion"],
    argvalues=EQUIVALENCES,
    ids=[s for s, _ in EQUIVALENCES],
)
def test_synonym_generates_same_sql(synonym: str, expansion: str) -> None:
    """The rewrite is real end-to-end: synonym and expansion emit identical SQL + params."""
    assert generate_sql_query(parse_scryfall_query(synonym)) == generate_sql_query(parse_scryfall_query(expansion))


def test_unimplemented_is_tag_passes_through(parse_query) -> None:
    """A not-yet-implemented `is:` value (bucket C) is left untouched, not mangled."""
    root = parse_query("is:promo").root
    assert root.operator == ":"
    assert root.lhs.original_attribute == "is"
    assert root.rhs.value == "promo"


def test_real_frame_value_not_rewritten(parse_query) -> None:
    """A genuine frame edition (`frame:2003`) is a plain leaf, not re-expanded."""
    root = parse_query("frame:2003").root
    assert root.operator == ":"
    assert root.lhs.original_attribute == "frame"
    assert root.rhs.value == "2003"


# ── #982: not: is the same as -is: ────────────────────────────────────────────
# (not: query, equivalent -is: query) -- the two must produce identical ASTs, including
# on values with their own is:-expansion (vanilla, new, ...): not:vanilla negates the
# same subtree is:vanilla expands to, not a raw card_is_tags lookup for a key nothing
# ever stores.
NOT_EQUIVALENCES = [
    ("not:creature", "-is:creature"),
    ("not:vanilla", "-is:vanilla"),
    ("not:new", "-is:new"),
    ("not:reprint", "-is:reprint"),
]


@pytest.mark.parametrize(
    argnames=["not_query", "expansion"],
    argvalues=NOT_EQUIVALENCES,
    ids=[s for s, _ in NOT_EQUIVALENCES],
)
def test_not_expands_to_negated_is(parse_query, not_query: str, expansion: str) -> None:
    """Each not: query parses to the same AST as the equivalent -is: query (both parsers)."""
    assert parse_query(not_query) == parse_query(expansion)


@pytest.mark.parametrize(
    argnames=["not_query", "expansion"],
    argvalues=NOT_EQUIVALENCES,
    ids=[s for s, _ in NOT_EQUIVALENCES],
)
def test_not_generates_same_sql_as_negated_is(not_query: str, expansion: str) -> None:
    """The rewrite is real end-to-end: not: and -is: emit identical SQL + params."""
    assert generate_sql_query(parse_scryfall_query(not_query)) == generate_sql_query(parse_scryfall_query(expansion))


# ── #734: plain-literal regex -> substring lowering ──────────────────────────
# A metacharacter-free, unanchored regex is a substring search, so it must parse to exactly the same
# AST as its quoted-substring form (which is index-backed, where an arbitrary regex is a full scan).
LOWERED_EQUIVALENCES = [
    ("o:/sacrifice a/", 'o:"sacrifice a"'),
    ("name:/lightning bolt/", 'name:"lightning bolt"'),
    (r"o:/foo\.bar/", 'o:"foo.bar"'),  # escaped punctuation unescapes to its literal
    (r"o:/\{t\}/", 'o:"{t}"'),  # escaped braces
    ("ft:/dragon/", "ft:dragon"),
    ("a:/guay/", "a:guay"),  # artist field
]


@pytest.mark.parametrize(
    argnames=["regex_query", "substring_query"],
    argvalues=LOWERED_EQUIVALENCES,
    ids=[r for r, _ in LOWERED_EQUIVALENCES],
)
def test_plain_literal_regex_lowers_to_substring(parse_query, regex_query: str, substring_query: str) -> None:
    """A plain-literal regex parses to the same AST as the equivalent substring query (both parsers)."""
    assert parse_query(regex_query) == parse_query(substring_query)


@pytest.mark.parametrize(
    argnames=["regex_query", "substring_query"],
    argvalues=LOWERED_EQUIVALENCES,
    ids=[r for r, _ in LOWERED_EQUIVALENCES],
)
def test_lowered_regex_generates_same_sql(regex_query: str, substring_query: str) -> None:
    """The lowering is real end-to-end: the regex and the substring form emit identical SQL + params."""
    assert generate_sql_query(parse_scryfall_query(regex_query)) == generate_sql_query(parse_scryfall_query(substring_query))


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        ("o:/^flying$/",),  # anchors
        ("o:/^flying/",),
        ("o:/flying$/",),
        ("o:/draw .* cards/",),  # live metacharacters
        ("o:/[aeiou]/",),  # character class
        (r"o:/\d+/",),  # class escape
        ("o:/a|b/",),  # alternation
    ],
    ids=["anchored-both", "anchored-start", "anchored-end", "metachar", "char-class", "class-escape", "alternation"],
)
def test_nonliteral_regex_stays_regex(parse_query, query: str) -> None:
    """Anchors, metacharacters, and character classes are NOT substrings — keep them as a regex leaf."""
    assert isinstance(parse_query(query).root.rhs, RegexValueNode)


# A NON-ASCII literal is exactly where the engine's byte-walking pattern classifier used to
# panic, and this table is the split that decided which queries reached it. A pattern that is
# nothing but literals is lowered here and never becomes a regex leaf at all; one that mixes a
# metacharacter with a multi-byte character stays a regex and goes to the engine. Counts on
# api.scryfall.com 2026-08-28: `o:/x—/` 7 (lowered), `o:/.—/` 3,461 and `o:/[a-z]—/` 245 (not).
NON_ASCII_LOWERED = [("o:/x—/", 'o:"x—"'), ("o:/é/", 'o:"é"')]
NON_ASCII_STAY_REGEX = ["o:/.—/", "o:/[a-z]—/", r"o:/\w—/", "o:/[a-z]é/", "o:/—[^{]*$/"]


@pytest.mark.parametrize(
    argnames=["regex_query", "substring_query"],
    argvalues=NON_ASCII_LOWERED,
    ids=[r for r, _ in NON_ASCII_LOWERED],
)
def test_bare_non_ascii_literal_still_lowers(parse_query, regex_query: str, substring_query: str) -> None:
    """A pattern of nothing but a non-ASCII literal is a substring, and never reaches the engine."""
    assert parse_query(regex_query) == parse_query(substring_query)


@pytest.mark.parametrize(argnames=["query"], argvalues=[(q,) for q in NON_ASCII_STAY_REGEX], ids=NON_ASCII_STAY_REGEX)
def test_metacharacter_beside_non_ascii_stays_regex(parse_query, query: str) -> None:
    """Mixed shapes stay regex leaves — the engine has to classify them without panicking."""
    assert isinstance(parse_query(query).root.rhs, RegexValueNode)


# THE LOWERING IS TEXT-COLUMNS-ONLY, because "the substring this pattern spells" is only a legal
# value where a bare value IS a substring test. `mana:` is the one non-text column that can carry
# a pattern, and there the two readings are different queries: `mana:/p/ mv=1` is 9 on
# api.scryfall.com (2026-08-28), every one Phyrexian, while the lowered `mana:p` is `Invalid
# expression "mana:p" was ignored. Unknown mana symbols "P".` and answers the unfiltered 3,244.
@pytest.mark.parametrize(argnames=["query"], argvalues=[("mana:/p/",), ("m:/rr/",), ("mana:/2/",)], ids=["mana-p", "m-rr", "mana-2"])
def test_plain_literal_mana_regex_does_not_lower(parse_query, query: str) -> None:
    """A plain-literal pattern on the mana column stays a regex — lowering it discards the filter."""
    assert isinstance(parse_query(query).root.rhs, RegexValueNode)


# `~` IS A METACHARACTER in Scryfall's dialect — an automatic alias for the card's own
# self-reference, which the engine expands into a word-bounded alternation of the card's names and
# a fixed "this <noun>" phrase family. Reading it as the literal tilde turns `o:/~/` into the
# substring search `o:~`, which no oracle text on earth satisfies: 404 against 19,228 on
# api.scryfall.com (2026-08-28). The escaped form is not protected either — `o:/\~/` answers the
# same 19,228 there.
@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[("o:/~/",), (r"o:/\~/",), ("o:/~ deals 3 damage/",), ("ft:/~/",), ("name:/~/",), ("t:/~/",)],
    ids=["oracle", "escaped", "with-literal", "flavor", "name", "type"],
)
def test_tilde_is_not_a_plain_literal(parse_query, query: str) -> None:
    """A `~` pattern stays a regex on EVERY column, including the ones that do not expand it.

    Lowering is a parser-side decision and the alias is an engine-side one, so the parser cannot
    take the column into account here — and it does not need to: on `ft:`/`name:` the engine
    compiles the tilde as the literal it looks like, which is exactly what `ft:/~/`'s 2 cards
    (Blighted Agent and Urabrask the Hidden, whose Phyrexian-script flavor text contains one) and
    `name:/~/`'s 404 say it should be.
    """
    assert isinstance(parse_query(query).root.rhs, RegexValueNode)


def test_tilde_costs_one_token_in_the_regex_budget() -> None:
    r"""`~` is one piece of user-written structure, the same call the `\\s…` shorthands get.

    Its expansion is a 17-way alternation, so measuring that instead would put `o:/~~/` past
    MAX_ALTERNATIONS_PER_PATTERN — and the expansion is a fixed constant this codebase chose, not
    something the searcher typed. Python's `re` already reads `~` as an ordinary literal, so this
    needs no rewrite; the test is here so a later one is a deliberate change.
    """
    parse_scryfall_query("o:/~~~~~~~~/")


_PLAIN_LITERAL_CASES = {
    "bare_literal": {"pattern": "sacrifice a", "expected": "sacrifice a"},
    "escaped_dot": {"pattern": r"foo\.bar", "expected": "foo.bar"},
    "escaped_braces": {"pattern": r"\{t\}: add", "expected": "{t}: add"},
    "start_anchor": {"pattern": "^flying", "expected": None},
    "end_anchor": {"pattern": "flying$", "expected": None},
    "star": {"pattern": "a*b", "expected": None},
    "alternation": {"pattern": "a|b", "expected": None},
    "char_class": {"pattern": "[aeiou]", "expected": None},
    "digit_class": {"pattern": r"\d+", "expected": None},
    "word_boundary": {"pattern": r"\bfoo", "expected": None},
    "dangling_backslash": {"pattern": "foo\\", "expected": None},
    "empty": {"pattern": "", "expected": None},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_PLAIN_LITERAL_CASES.values()))),
    argvalues=[[v for _, v in sorted(_PLAIN_LITERAL_CASES[name].items())] for name in sorted(_PLAIN_LITERAL_CASES)],
    ids=sorted(_PLAIN_LITERAL_CASES),
)
def test_regex_plain_literal(expected: str | None, pattern: str) -> None:
    """`_regex_plain_literal` extracts the literal for metachar-free patterns, else None."""
    assert _regex_plain_literal(pattern) == expected
