"""Derived-predicate rewrite (api/parsing/rewrite.py).

A synonym must parse to exactly the same AST as its canonical expansion — verified
against BOTH parsers via the `parse_query` fixture, since the rewrite runs at the shared
post-parse seam. Mappings are validated against Scryfall's live API in
docs/issues/00713-is-tag-recovery.md.
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query

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
    # composes under negation and inside compounds
    ("-frame:old", "-(frame:1993 or frame:1997)"),
    ("t:goblin frame:modern", "t:goblin frame:2003"),
    ("t:goblin is:party", "t:goblin t:creature (t:cleric or t:rogue or t:warrior or t:wizard or kw:changeling)"),
]


@pytest.mark.parametrize(("synonym", "expansion"), EQUIVALENCES, ids=[s for s, _ in EQUIVALENCES])
def test_synonym_expands_to_canonical(parse_query, synonym: str, expansion: str) -> None:
    """Each synonym parses to the same AST as its hand-written expansion (both parsers)."""
    assert parse_query(synonym) == parse_query(expansion)


@pytest.mark.parametrize(("synonym", "expansion"), EQUIVALENCES, ids=[s for s, _ in EQUIVALENCES])
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
