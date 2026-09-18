"""Derived-predicate rewrite (api/parsing/rewrite.py).

A synonym must parse to exactly the same AST as its canonical expansion — verified
against BOTH parsers via the `parse_query` fixture, since the rewrite runs at the shared
post-parse seam. Mappings are validated against Scryfall's live API in
docs/issues/00713-is-tag-recovery.md.
"""

import re

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.db_info import BOOLEAN_IS_TAGS
from api.parsing.nodes import RegexValueNode
from api.parsing.rewrite import (
    _DERIVED_EXPANSIONS,
    ENGINE_IS_VALUES,
    SUPPORTED_HAS_VALUES,
    SUPPORTED_IS_VALUES,
    _regex_plain_literal,
)

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
    ("is:bear", "t:creature pow=2 tou=2 cmc=2"),
    # layout family
    ("is:split", "layout:split"),
    ("is:flip", "layout:flip"),
    ("is:transform", "layout:transform"),
    ("is:mdfc", "layout:modal_dfc"),
    ("is:meld", "layout:meld"),
    ("is:leveler", "layout:leveler"),
    (
        "is:dfc",
        "layout:transform or layout:modal_dfc or layout:art_series or layout:double_faced_token or layout:reversible_card",
    ),
    # The same predicate under two names, and neither of them is one layout: `is:host
    # -is:augmentation` and its converse are both empty on api.scryfall.com.
    ("is:host", "layout:host or layout:augment"),
    ("is:augmentation", "layout:host or layout:augment"),
    ("is:token", "layout:token or layout:double_faced_token or t:token"),
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
    # Mana-symbol classes. Long, and spelled out rather than trimmed to a sample: the whole point of
    # the entry is that the SET is right, and a truncated expectation would pass while the
    # vocabulary drifted.
    (
        "is:hybrid",
        "m:{W/U} or m:{W/B} or m:{U/B} or m:{U/R} or m:{B/R} or m:{B/G} or m:{R/G} or m:{R/W} or "
        "m:{G/W} or m:{G/U} or m:{W/U/P} or m:{W/B/P} or m:{U/B/P} or m:{U/R/P} or m:{B/R/P} or "
        "m:{B/G/P} or m:{R/G/P} or m:{R/W/P} or m:{G/W/P} or m:{G/U/P} or m:{2/W} or m:{2/U} or "
        "m:{2/B} or m:{2/R} or m:{2/G} or m:{C/W} or m:{C/U} or m:{C/B} or m:{C/R} or m:{C/G}",
    ),
    (
        "is:phyrexian",
        "m:{W/P} or m:{U/P} or m:{B/P} or m:{R/P} or m:{G/P} or m:{W/U/P} or m:{W/B/P} or "
        "m:{U/B/P} or m:{U/R/P} or m:{B/R/P} or m:{B/G/P} or m:{R/G/P} or m:{R/W/P} or m:{G/W/P} or "
        'm:{G/U/P} or o:"{w/p}" or o:"{u/p}" or o:"{b/p}" or o:"{r/p}" or o:"{g/p}" or o:"{c/p}" or '
        'o:"{w/u/p}" or o:"{w/b/p}" or o:"{u/b/p}" or o:"{u/r/p}" or o:"{b/r/p}" or o:"{b/g/p}" or '
        'o:"{r/g/p}" or o:"{r/w/p}" or o:"{g/w/p}" or o:"{g/u/p}"',
    ),
    # `has:printedname` is `is:localizedname` under its other spelling; the engine answers the
    # right-hand side from a field, which is why this one does not expand any further.
    ("has:printedname", "is:localizedname"),
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
    # Everything castable. A strict superset of Scryfall's face-level is:spell: +48 / 31,760 on the
    # imported corpus, 0 misses (2026-08-16).
    (
        "is:spell",
        "t:artifact or t:battle or t:creature or t:enchantment or t:instant or t:kindred or t:planeswalker or t:sorcery",
    ),
    # "first printing" is exactly "not a reprint" — the two partition the printing space on
    # Scryfall, ties included (2026-08-16). Both spellings are accepted there.
    ("is:firstprinting", "-is:reprint"),
    ("is:firstprint", "-is:reprint"),
    # Scryfall's second names for land cycles we already carry, and the ones we did not.
    ("is:karoo", "otag:bounceland"),
    ("is:canland", "otag:cycle-horizon-land"),
    ("is:cycleland", "otag:cycle-dual-cycling-land"),
    ("is:bicycleland", "otag:cycle-dual-cycling-land"),
    # Frame effects and layouts that were already expressible and simply had no entry.
    ("is:showcase", "frame:showcase"),
    ("is:extendedart", "frame:extendedart"),
    ("is:tdfc", "layout:transform"),
    ("is:planar", "layout:planar"),
    ("is:reversible", "layout:reversible_card"),
    # Spelling aliases of stored tags: the expansion is the OTHER is: value, which stays a leaf.
    ("is:full", "is:fullart"),
    ("is:promostamped", "is:stamped"),
    ("is:arenaleague", "is:arena_league"),
    ("is:intropack", "is:intro_pack"),
    ("is:judgegift", "is:judge_gift"),
    ("is:mediainsert", "is:media_insert"),
    ("is:planeswalkerdeck", "is:planeswalker_deck"),
    ("is:setpromo", "is:set_promo"),
    ("is:rainbow", "is:rainbowfoil"),
    # Columns this parser already has, under their `is:` spelling.
    ("is:borderless", "border:borderless"),
    ("is:tombstone", "frame:tombstone"),
    # Set types: `st:` is the operator these five turn out to BE.
    ("is:masterpiece", "st:masterpiece"),
    ("is:alchemy", "st:alchemy"),
    ("is:funny", "st:funny"),
    ("is:watermark", "has:watermark"),
    # Eligibility, each count-validated on its own rather than rewritten to the format filter.
    ("is:oathbreaker", "t:planeswalker f:oathbreaker"),
    ("is:brawler", '((t:legendary (toughness>=0 or t:background)) or o:"can be your commander") f:brawl'),
    ("is:duelcommander", '((t:legendary (toughness>=0 or t:background)) or o:"can be your commander") f:duel'),
    # The `has:` family: presence on a regex-capable column, or the is: tag that answers the same
    # question off the same stored value.
    ("has:watermark", "watermark:/./"),
    ("has:artist", "artist:/./"),
    ("has:flavor", "flavor:/./"),
    ("has:foil", "is:foil"),
    ("has:highres", "is:hires"),
    ("has:story", "is:spotlight"),
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


_EQUIVALENCE_CORPUS = [
    "(this creature can't be blocked)",
    "this creature can't be blocked",
    "{T}: Add {G}.",
    "T: Add G.",
    "deal 2 damage. draw a card.",
    "+1/+1 counter",
    "11 counter",
    "a-b",
    "ab",
    "[brackets]",
    "brackets",
    "back\\slash",
]


@pytest.mark.parametrize(
    "pattern",
    [
        r"\(this creature",
        r"\{t\}",
        r"target\.",
        r"\+1/\+1",
        r"a\-b",
        r"\[brackets\]",
        r"back\\slash",
    ],
)
def test_lowering_preserves_what_the_pattern_matches(pattern: str) -> None:
    r"""The PROPERTY the rewrite has to have, not a table of what it currently answers.

    `o:/\(this creature/` reaching the engine as the substring `(this creature` reads like a
    mangled pattern and is not one: a backslash before a NON-word character IS that character, so
    the lowered literal matches exactly the strings the regex did. Stating that as an equivalence
    against `re` is what the table above cannot do -- it would pass just the same if `\(` were
    being DROPPED rather than resolved, which is how this looked to a reader who found the
    unescaped value in a wire tree and reported it as a bug.

    `re.IGNORECASE` is the flag the engine prepends to every query pattern, and the substring path
    compares case-folded text, so case-insensitivity is what both sides mean.
    """
    literal = _regex_plain_literal(pattern)
    assert literal is not None
    compiled = re.compile(pattern, re.IGNORECASE)
    for text in _EQUIVALENCE_CORPUS:
        assert (literal.casefold() in text.casefold()) == bool(compiled.search(text)), text


@pytest.mark.parametrize(
    ("operator", "pattern"),
    [
        ("o", r"\(a.b"),
        ("name", r"^\(x"),
        ("ft", r"\d\d\d"),
        ("a", r"\bguay\b"),
        ("t", r"\(a|b"),
        ("fo", r"[\]]"),
        ("e", r"kh\w"),
        ("cn", r"\d+a"),
        ("watermark", r"izz\S+"),
        ("layout", r"norm\w+"),
        ("border", r"bl\w+"),
    ],
)
def test_a_surviving_regex_keeps_its_backslashes(parse_query, operator: str, pattern: str) -> None:
    """The other half: a pattern that keeps its regex leaf is handed on byte for byte.

    That string goes straight to a regex compiler, so a backslash lost anywhere between the
    tokenizer and here changes what the query means.
    """
    rhs = parse_query(f"{operator}:/{pattern}/").root.rhs

    assert isinstance(rhs, RegexValueNode)
    assert rhs.value == pattern


# ─── The `is:` vocabulary, and what happens outside it ────────────────────────


def test_boolean_is_tag_expressions_read_the_row_they_are_correlated_against() -> None:
    """Every BOOLEAN_IS_TAGS expression names the `cards` alias, and only columns a card row has.

    The table holds SQL expressions rather than blob keys, which is what lets one mechanism cover
    plain booleans, array membership and single-field lookups -- but an expression is only correct
    inside `_build_boolean_is_tags_sql`'s correlated subquery, where the outer row is aliased
    `cards`. One that forgot the alias would reference nothing and the tag would be silently false
    for every card: the silent-zero shape this whole table exists to end.
    """
    assert all("cards." in expr for expr in BOOLEAN_IS_TAGS.values())
    columns = {"cards.raw_card_blob", "cards.mana_cost_text"}
    for tag, expr in BOOLEAN_IS_TAGS.items():
        assert any(col in expr for col in columns), (tag, expr)


def test_every_stored_is_tag_is_a_supported_value() -> None:
    """A tag the importer writes must be one the parser reports as supported.

    The two read one dict (db_info.BOOLEAN_IS_TAGS), so this is a structural check rather than a
    duplicate-keeping-honest one — but it is the assertion that would fail if either side ever
    grew its own copy again.
    """
    assert frozenset(BOOLEAN_IS_TAGS) <= SUPPORTED_IS_VALUES


def test_engine_answered_is_values_are_supported_and_stored_nowhere() -> None:
    """The `is:` values the ENGINE answers from a field are supported, and only from there.

    Two directions, and the second is the one that bites. Supported: a predicate that works and
    still warns is worse than one that does neither. Stored nowhere: if `localizedname`, `unique`,
    `vanilla`, `flavorname`, `atypical` or `default` ever became an importer tag as well, the engine
    would keep intercepting the leaf and the stored tag would be dead weight nobody could observe.
    """
    assert ENGINE_IS_VALUES <= SUPPORTED_IS_VALUES
    assert not (ENGINE_IS_VALUES & frozenset(BOOLEAN_IS_TAGS))
    # ...and no rewrite claims them either: an expansion would silently win over the engine leaf.
    assert not (ENGINE_IS_VALUES & {value for alias, value in _DERIVED_EXPANSIONS if alias == "is"})


@pytest.mark.parametrize(
    argnames="query",
    argvalues=[
        "is:reprint",
        "is:promo",
        "is:foil",
        "is:reserved",
        "is:spell",
        "is:firstprinting",
        "is:fetchland",
        "is:nonfoil",
        "is:booster",
        "is:hires",
        "is:prerelease",
        "is:universesbeyond",
        "is:judge",
        "is:etched",
        "is:showcase",
        "is:tdfc",
        "is:hybrid",
        "is:phyrexian",
        # The 2026-09-03 vocabulary: a promo type the syntax page never lists, the concatenated
        # spelling of a stored underscored key, the short spelling of a promo type, and a meld role.
        "is:serialized",
        "is:setpromo",
        "is:rainbow",
        "is:meldpart",
        # Neither expands nor names a stored tag: the engine reads a field for each. Warning about
        # them would be the exact defect SUPPORTED_IS_VALUES exists to remove, in reverse.
        "is:localizedname",
        "is:unique",
        "is:vanilla",
        "is:flavorname",
        "is:atypical",
        "is:default",
        # ...and through the `has:` alias, which resolves to the same engine leaf.
        "has:vanilla",
        "has:flavorname",
        "has:atypical",
    ],
)
def test_supported_is_values_do_not_warn(query: str) -> None:
    """Everything the vocabulary covers — stored or derived — passes without a warning."""
    assert parse_scryfall_query(query).warnings == ()


def test_unsupported_is_value_warns_once_per_leaf() -> None:
    """An `is:` value with no data behind it says so instead of returning a silent zero.

    Scryfall IGNORES an unknown `is:` value and warns (measured 2026-08-16: `is:notarealtag e:khm`
    returns the whole set). This parser keeps the term, so the answer is a no-match — the warning is
    what tells the caller which of the two happened.
    """
    (warning,) = parse_scryfall_query("is:notarealtag t:creature").warnings
    assert "is:notarealtag" in warning

    # Under a negation and inside an or-group, both of which the walk descends into.
    assert len(parse_scryfall_query("-is:notarealtag").warnings) == 1
    assert len(parse_scryfall_query("is:nope or is:alsonope").warnings) == 2

    # A supported value in the same query does not add one.
    assert len(parse_scryfall_query("is:nope is:reprint").warnings) == 1


@pytest.mark.parametrize(
    argnames="query",
    argvalues=["has:watermark", "has:artist", "has:flavor", "has:foil", "has:booster", "has:etched", "has:story"],
)
def test_supported_has_values_do_not_warn(query: str) -> None:
    """The `has:` family gets the same treatment `is:` does — supported means silent."""
    assert parse_scryfall_query(query).warnings == ()


def test_unsupported_has_value_warns_like_an_is_value() -> None:
    """`has:` shares `is:`'s column, so an unmapped value would otherwise be the same silent zero.

    `has:illustration` is the case to hold onto: the column IS stored, and the value is one
    api.scryfall.com answers — what is missing is a presence predicate over an id, which no
    rewrite can express. Warning says that; returning zero does not.
    """
    (warning,) = parse_scryfall_query("has:illustration").warnings
    assert "has:illustration" in warning
    assert len(parse_scryfall_query("has:notarealfield t:creature").warnings) == 1


def test_set_type_parses_as_its_own_column() -> None:
    """`st:` is a column, not a tag: it must not land in card_is_tags with the is:/has: family."""
    node = parse_scryfall_query("st:masterpiece").root
    assert node.lhs.attribute_name == "card_set_type"
    # Every alias Scryfall accepts for it reaches the same column.
    for alias in ("set_type", "settype", "st"):
        assert parse_scryfall_query(f"{alias}:promo").root.lhs.attribute_name == "card_set_type"


def test_type_operator_is_not_an_is_value() -> None:
    """Only `is:` is checked — a subtype nobody has is a legitimate empty result, not a warning."""
    assert parse_scryfall_query("t:notarealtype").warnings == ()


def test_has_is_a_total_alias_of_is(parse_query) -> None:
    """`has:` accepts the WHOLE `is:` vocabulary, not the hand-listed `has:`-flavoured subset.

    `_HAS_EXPANSIONS` was built by probing `has:`-FLAVOURED candidates -- the presence questions,
    and the boolean tags that read like presence questions -- so every value nobody thought to
    spell against `has:` was absent and returned a silent no-match. `has:split` is the one that
    surfaced it: 126 cards on api.scryfall.com, nothing here.

    MEASURED 2026-08-17 over 22 values spanning every shape the `is:` vocabulary has -- derived
    layout predicates, computed text predicates, importer booleans, and the two set-shaped ones.
    `is:X` and `has:X` answered the SAME total_cards on all 22:

        is:permanent 26220 = has:permanent      is:frenchvanilla 1095 = has:frenchvanilla
        is:split       126 = has:split          is:indicator      369 = has:indicator
    """
    assert SUPPORTED_HAS_VALUES >= SUPPORTED_IS_VALUES
    # One per shape, expanding to the identical AST under either spelling.
    # ...including an ENGINE-answered one. `vanilla` expands to nothing at all, so the alias has to
    # reach the leaf itself rather than a rewrite of it.
    for value in ("split", "dfc", "frenchvanilla", "permanent", "promo", "etched", "commander", "vanilla"):
        assert parse_query(f"has:{value}").to_json() == parse_query(f"is:{value}").to_json(), value


def test_the_presence_half_is_not_overtaken_by_the_alias(parse_query) -> None:
    """`has:watermark` asks whether a watermark is PRESENT -- there is no `is:watermark`.

    The alias is a FALLBACK, applied only where `_HAS_EXPANSIONS` has no entry; folding it in ahead
    would turn these two into unsupported tags matching nothing.
    """
    assert parse_query("has:watermark").to_json() == parse_query("watermark:/./").to_json()
    assert parse_query("has:artist").to_json() == parse_query("artist:/./").to_json()
