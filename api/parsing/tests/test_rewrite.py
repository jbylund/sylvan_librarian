"""Derived-predicate rewrite (api/parsing/rewrite.py).

A synonym must parse to exactly the same AST as its canonical expansion — verified
against BOTH parsers via the `parse_query` fixture, since the rewrite runs at the shared
post-parse seam. Mappings are validated against Scryfall's live API in
docs/issues/00713-is-tag-recovery.md.
"""

from collections.abc import Iterator

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.nodes import QueryNode, RegexValueNode
from api.parsing.rewrite import _expanded_template, _regex_plain_literal

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
# A metacharacter-free, unanchored, whitespace-free regex is a substring search, so it must parse to
# exactly the same AST as its substring form (which is index-backed, where an arbitrary regex is a
# full scan).
LOWERED_EQUIVALENCES = [
    ("o:/sacrifice/", "o:sacrifice"),
    ("name:/lightning/", "name:lightning"),
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
        # A plain literal with whitespace is contiguous as a regex but the substring leaf renders as
        # LIKE '%draw%a%card%' on the SQL path, so lowering it would widen the query.
        ("o:/draw a card/",),
        ("name:/lightning bolt/",),
    ],
    ids=[
        "anchored-both",
        "anchored-start",
        "anchored-end",
        "metachar",
        "char-class",
        "class-escape",
        "alternation",
        "whitespace",
        "whitespace-name",
    ],
)
def test_nonliteral_regex_stays_regex(parse_query, query: str) -> None:
    """Anchors, metacharacters, character classes, and whitespace are NOT substrings — keep the regex leaf."""
    assert isinstance(parse_query(query).root.rhs, RegexValueNode)


def test_whitespace_literal_regex_keeps_contiguous_sql() -> None:
    """The regex reaches the SQL as a contiguous `~*` match, not a gapped LIKE pattern."""
    sql, params = generate_sql_query(parse_scryfall_query("o:/draw a card/"))
    assert "~*" in sql
    assert "draw a card" in params.values()


_PLAIN_LITERAL_CASES = {
    "bare_literal": {"pattern": "sacrifice", "expected": "sacrifice"},
    "escaped_dot": {"pattern": r"foo\.bar", "expected": "foo.bar"},
    "escaped_braces": {"pattern": r"\{t\}:", "expected": "{t}:"},
    "whitespace": {"pattern": "sacrifice a", "expected": None},
    "tab": {"pattern": "sacrifice\ta", "expected": None},
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


# ── cached templates are shared, so SQL generation must not write to lhs ─────


def _leaves(node: QueryNode) -> Iterator[QueryNode]:
    if hasattr(node, "operands"):
        for operand in node.operands:
            yield from _leaves(operand)
    elif hasattr(node, "operand"):
        yield from _leaves(node.operand)
    else:
        yield node


def test_sql_generation_leaves_the_cached_expansion_template_alone() -> None:
    """Rendering `is:party` three times gives identical SQL and never touches the template's lhs.

    `_clone_expansion` shares lhs across every clone of a cached template; `_handle_jsonb_array`
    used to assign `lhs.attribute_name` while resolving type vs subtype, which rewrote the cached
    template (idempotently, as it happened -- but a write into a cache all the same).
    """
    outputs = [generate_sql_query(parse_scryfall_query("is:party")) for _ in range(3)]
    assert outputs[0] == outputs[1] == outputs[2]
    template = _expanded_template(("is", "party"))
    type_leaves = [leaf for leaf in _leaves(template) if leaf.lhs.original_attribute == "t"]
    assert len(type_leaves) == 5  # creature, cleric, rogue, warrior, wizard
    assert {leaf.lhs.attribute_name for leaf in type_leaves} == {"card_types"}


def test_type_leaf_lhs_is_not_rewritten_by_sql_generation(parse_query) -> None:
    """A subtype value routes the SQL to card_subtypes without renaming the node's column."""
    parsed = parse_query("t:cleric")
    sql, _params = generate_sql_query(parsed)
    assert "card.card_subtypes" in sql
    assert parsed.root.lhs.attribute_name == "card_types"
    assert generate_sql_query(parsed) == (sql, _params)
