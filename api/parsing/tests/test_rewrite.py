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
    ("is:new", "frame:2015"),
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
