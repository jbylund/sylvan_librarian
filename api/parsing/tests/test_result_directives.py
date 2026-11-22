"""Result-shape directives (unique:/sort:/order:/direction:/prefer:) and the `ci` identity alias.

Scryfall accepts presentation directives inside the query string itself — a query
like `t:goblin sort:edhrec` is valid there and filters exactly as `t:goblin`.
Rejecting them breaks any client that forwards Scryfall-shaped query strings
verbatim. Both parsers must consume a directive and contribute nothing to the
filter tree, keeping SQL identical to the directive-free query; the extraction
pass at the rewrite seam records each (name, value) on the Query so the API layer
can apply them. Stripping is structural, matching Scryfall (measured 2026-08-07):
a directive inside an Or does not make the Or true, and a negated directive is
still just a directive.

`ci` mirrors Scryfall's alias for color identity (`ci<=bg` == `id<=bg`).
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query
from api.parsing.nodes import Query
from api.parsing.post_parse import parse_query as _parse_with_pipeline
from api.parsing.pyparsing_based import parse_str_to_query as _pyparsing_parse_str_to_query


def parse_search_query(query: str | None) -> Query:
    """Full-pipeline parse through the pyparsing front-end (the seam parse_scryfall_query uses)."""
    return _parse_with_pipeline(query, parser_fn=_pyparsing_parse_str_to_query)


# (query with directive, equivalent query without it)
DIRECTIVE_CASES = [
    ("t:goblin sort:edhrec", "t:goblin"),
    ("t:goblin order:name", "t:goblin"),
    ("t:goblin direction:asc", "t:goblin"),
    ("t:goblin dir:asc", "t:goblin"),
    ("t:goblin dir:auto", "t:goblin"),
    ("t:goblin prefer:oldest", "t:goblin"),
    ("t:goblin unique:art", "t:goblin"),
    ("t:goblin unique:prints", "t:goblin"),
    ("sort:edhrec t:goblin", "t:goblin"),
    ('t:goblin sort:"edhrec"', "t:goblin"),
    ("t:planeswalker f:commander sort:edhrec", "t:planeswalker f:commander"),
    ("(t:goblin or t:elf) sort:edhrec", "t:goblin or t:elf"),
    ("t:goblin or unique:art", "t:goblin"),
    ("-unique:art t:goblin", "t:goblin"),
]


@pytest.mark.parametrize(
    argnames=["query", "equivalent"],
    argvalues=DIRECTIVE_CASES,
    ids=[q for q, _ in DIRECTIVE_CASES],
)
def test_directive_filters_like_equivalent(query: str, equivalent: str) -> None:
    """A directive-bearing query produces the same SQL as the query without it (hand parser)."""
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_scryfall_query(equivalent))


@pytest.mark.parametrize(
    argnames=["query", "equivalent"],
    argvalues=DIRECTIVE_CASES,
    ids=[q for q, _ in DIRECTIVE_CASES],
)
def test_directive_parser_parity(query: str, equivalent: str) -> None:
    """Both parsers agree on every directive-bearing query."""
    del equivalent
    assert generate_sql_query(parse_scryfall_query(query)) == generate_sql_query(parse_search_query(query))


def test_directive_needs_a_value() -> None:
    """A dangling directive prefix is still an error, not a silent no-op."""
    with pytest.raises(ValueError, match="Failed to parse"):
        parse_scryfall_query("t:goblin sort:")
    with pytest.raises(ValueError, match="Failed to parse"):
        parse_scryfall_query("t:goblin unique:")


def test_directive_prefix_of_longer_word_is_a_name() -> None:
    """Words that merely START with a directive keep their name reading."""
    # "sorting" must not be consumed as "sort" + garbage, nor "uniquely" as "unique" + garbage.
    assert generate_sql_query(parse_scryfall_query("sorting")) == generate_sql_query(parse_search_query("sorting"))
    assert generate_sql_query(parse_scryfall_query("uniquely")) == generate_sql_query(parse_search_query("uniquely"))
    # "dir" is the riskiest of these: it is a prefix of "direction" AND of ordinary words.
    assert generate_sql_query(parse_scryfall_query("direct")) == generate_sql_query(parse_search_query("direct"))
    assert generate_sql_query(parse_scryfall_query("dire")) == generate_sql_query(parse_search_query("dire"))


def test_dir_is_recorded_as_the_short_spelling_of_direction() -> None:
    """`dir:` is Scryfall's short spelling and sets the same parameter (measured 2026-08-09).

    Recorded under its own name rather than rewritten to `direction`, so a warning about a
    repeated directive can quote back what the client actually wrote.
    """
    assert parse_scryfall_query("t:goblin dir:desc").directives == (("dir", "desc", False),)
    assert parse_search_query("t:goblin dir:desc").directives == (("dir", "desc", False),)


def test_direction_is_not_swallowed_by_the_dir_alternative() -> None:
    """The longer spelling has to win outright, or `direction:desc` parses as `dir` + garbage."""
    assert parse_scryfall_query("t:goblin direction:desc").directives == (("direction", "desc", False),)
    assert parse_search_query("t:goblin direction:desc").directives == (("direction", "desc", False),)


def test_directive_values_are_captured_in_source_order() -> None:
    """The parsed Query records every directive's (name, value, nested), preserving repeats and order."""
    parsed = parse_scryfall_query("t:goblin unique:art sort:usd unique:cards")
    assert parsed.directives == (("unique", "art", False), ("sort", "usd", False), ("unique", "cards", False))


def test_directive_free_query_captures_nothing() -> None:
    """A query without directives records an empty tuple."""
    assert parse_scryfall_query("t:goblin").directives == ()


def test_directive_values_are_lowercased_and_unquoted() -> None:
    """Directive names and values normalize to lowercase; quoted values lose their quotes."""
    assert parse_scryfall_query('UNIQUE:"Art" t:elf').directives == (("unique", "art", False),)


def test_directive_inside_or_or_negation_is_flagged_nested() -> None:
    """A directive under an Or or a negation carries nested=True — it looks scoped but is not."""
    assert parse_scryfall_query("t:goblin or unique:art").directives == (("unique", "art", True),)
    assert parse_scryfall_query("-unique:art t:goblin").directives == (("unique", "art", True),)


def test_directive_in_a_parenthesized_and_group_is_not_nested() -> None:
    """Conjunction is flat: `(cmc=5 prefer:oldest) (cmc=4 prefer:newest)` is four top-level terms.

    Both directives capture un-nested and in order, so the API layer's override warning — not a
    scope warning — is what explains which one won.
    """
    parsed = parse_scryfall_query("(cmc=5 prefer:oldest) (cmc=4 prefer:newest)")
    assert parsed.directives == (("prefer", "oldest", False), ("prefer", "newest", False))
    equivalent = parse_scryfall_query("cmc=5 cmc=4")
    assert generate_sql_query(parsed) == generate_sql_query(equivalent)


@pytest.mark.parametrize(
    argnames="query",
    argvalues=[
        "t:goblin unique:art sort:usd",
        "-unique:art t:goblin",
        '(t:elf or unique:"prints") direction:desc',
        "unique:art",
    ],
)
def test_directive_capture_parity(query: str) -> None:
    """Both parsers record identical directives for the same query."""
    assert parse_scryfall_query(query).directives == parse_search_query(query).directives


def test_directive_only_query_filters_like_empty() -> None:
    """A query that is nothing but a directive matches everything, in both parsers."""
    assert generate_sql_query(parse_scryfall_query("unique:art")) == generate_sql_query(parse_search_query("unique:art"))
    assert parse_scryfall_query("unique:art").directives == (("unique", "art", False),)


CI_CASES = [
    ("ci<=bg", "id<=bg"),
    ("ci:wu", "id:wu"),
    ("ci>=rg", "identity>=rg"),
    ("t:land ci<=bg", "t:land id<=bg"),
]


@pytest.mark.parametrize(
    argnames=["ci_query", "id_query"],
    argvalues=CI_CASES,
    ids=[q for q, _ in CI_CASES],
)
def test_ci_is_an_identity_alias(ci_query: str, id_query: str) -> None:
    """`ci` produces identical SQL to the established identity aliases, in both parsers."""
    assert generate_sql_query(parse_scryfall_query(ci_query)) == generate_sql_query(parse_scryfall_query(id_query))
    assert generate_sql_query(parse_search_query(ci_query)) == generate_sql_query(parse_search_query(id_query))


# `-` is not a word character to the hand tokenizer, so `usd-low` lexes as WORD MINUS WORD.
# The hand parser consumed a single token after the colon and stopped at `usd`, leaving `-low`
# to fail the parse -- while pyparsing's `string_value_word` (\w[\w.-]*) took the whole thing.
# So the two parsers disagreed on every hyphenated directive value, and the hyphenated prefer
# spellings _DIRECTIVE_PREFER enumerates were unreachable from inside a query through the hand
# parser. No existing case used one, which is why parity did not see it.
HYPHENATED_DIRECTIVES = [
    ("prefer:usd-low", "prefer", "usd-low"),
    ("prefer:usd-high t:bolt", "prefer", "usd-high"),
    ("prefer:usd-low t:goblin", "prefer", "usd-low"),
]


@pytest.mark.parametrize(
    argnames=["query", "name", "value"],
    argvalues=HYPHENATED_DIRECTIVES,
    ids=[q for q, _, _ in HYPHENATED_DIRECTIVES],
)
def test_hyphenated_directive_values_survive_both_parsers(query: str, name: str, value: str) -> None:
    """A hyphenated directive value reaches the directive whole, in both parsers."""
    for parse in (parse_scryfall_query, parse_search_query):
        directives = parse(query).directives
        assert directives == ((name, value, False),), f"{parse.__name__} lost the hyphen in {query!r}"
