"""Derived-predicate rewrite (api/parsing/rewrite.py).

A synonym must parse to exactly the same AST as its canonical expansion — verified
against BOTH parsers via the `parse_query` fixture, since the rewrite runs at the shared
post-parse seam. Mappings are validated against Scryfall's live API in
docs/issues/00713-is-tag-recovery.md.
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.nodes import RegexValueNode, StringValueNode
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


# ── game: is a prefixed is: tag ──────────────────────────────────────────────
# `game:paper` looks up the `game_paper` key the import writes (BOOLEAN_IS_TAGS), never a bare
# `paper`: game: and is: share card_is_tags, so without the prefix `game:promo` would answer
# is:promo's promos where Scryfall matches nothing for an unknown game.


@pytest.mark.parametrize(
    argnames=["query", "expected_value"],
    argvalues=[
        ("game:paper", "game_paper"),
        ("game:PAPER", "game_paper"),  # lowered before prefixing
        ('game:"mtgo"', "game_mtgo"),
        ("game:promo", "game_promo"),  # unknown game -> a key no row carries
    ],
    ids=["paper", "upper", "quoted", "unknown"],
)
def test_game_value_is_prefixed(parse_query, query: str, expected_value: str) -> None:
    """The rhs takes the game_ prefix; the lhs stays the `game` attribute on card_is_tags."""
    root = parse_query(query).root
    assert root.lhs.original_attribute == "game"
    assert root.lhs.attribute_name == "card_is_tags"
    assert isinstance(root.rhs, StringValueNode)
    assert root.rhs.value == expected_value


def test_game_prefix_applies_under_negation_and_in_compounds(parse_query) -> None:
    """`-game:mtgo` and `t:goblin game:paper` reach their game leaf through NotNode / AndNode."""
    negated = parse_query("-game:mtgo").root
    assert negated.operand.rhs.value == "game_mtgo"
    compound = parse_query("t:goblin game:paper").root
    game_leaves = [op for op in compound.operands if getattr(op.lhs, "original_attribute", None) == "game"]
    assert [leaf.rhs.value for leaf in game_leaves] == ["game_paper"]


def test_game_regex_becomes_plain_string(parse_query) -> None:
    """A regex rhs is prefixed as a PLAIN string: `game:/^pap/` names a tag nothing carries.

    Left as a pattern it could match `game_paper` and answer a query Scryfall rejects.
    """
    root = parse_query("game:/^pap/").root
    assert isinstance(root.rhs, StringValueNode)
    assert root.rhs.value == "game_^pap"


def test_is_leaf_with_same_value_is_not_prefixed(parse_query) -> None:
    """The pass keys on original_attribute: `is:paper` keeps its bare value."""
    root = parse_query("is:paper").root
    assert root.lhs.original_attribute == "is"
    assert root.rhs.value == "paper"
    assert parse_query("game:paper") != parse_query("is:paper")


def test_game_generates_same_sql_as_prefixed_is_tag() -> None:
    """The prefix is the whole mechanism: `game:paper` is `is:game_paper` at the SQL layer."""
    assert generate_sql_query(parse_scryfall_query("game:paper")) == generate_sql_query(parse_scryfall_query("is:game_paper"))


def test_game_explanation_names_the_game_not_the_storage_key(parse_query) -> None:
    """The explanation says what the user typed (`game` / `paper`), not `game_paper`."""
    explanation = parse_query("game:paper").to_human_explanation()
    assert "game" in explanation
    assert "paper" in explanation
    assert "game_" not in explanation


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
