"""A slash between terms is nothing, and one glued to a text value is a character of it.

Scryfall's rule, measured on api.scryfall.com 2026-10-04, one request per query, none of them
carrying a `warnings` key:

    fire // ice   fire / ice   fire /ice   fire/ ice   fire/ice   (fire // ice)     `fire ice` (4)
    //fire   fire //   /fire/   t:goblin /   o:fire /   e:khm /   cmc>=3 /   !fire /   the term alone
    /   //   ///                                         400 "All of your terms were ignored."
    o:1/1                                                1,432 -- the cards with "1/1" in their text
    o:fire/ice   o:fire/   t:elf/warrior   e:khm/        404: a value keeps its slashes

Every query in the first two rows was `Failed to parse query` here, and `fire // ice` is what a
user gets by pasting the name of a split or double-faced card.

Each case runs through both parsers (`parse_query` fixture) and is compared with the spelling it
must equal, so the two parsers are pinned to each other as well as to the rule.
"""

from __future__ import annotations

import json

import pytest

from api.parsing import generate_sql_query


def sql(parse_query, query: str) -> tuple:
    return generate_sql_query(parse_query(query))


@pytest.mark.parametrize(
    argnames=["query", "same_as"],
    argvalues=[
        # Between two words, whatever the spacing.
        ("fire // ice", "fire ice"),
        ("fire / ice", "fire ice"),
        ("fire /ice", "fire ice"),
        ("fire/ ice", "fire ice"),
        ("fire///ice", "fire ice"),
        # Glued to a bare word it ends the word: a name word never holds a slash.
        ("fire/ice", "fire ice"),
        ("fire//ice", "fire ice"),
        ("power/sink", "power sink"),
        ("fire/ice or t:goblin", "fire ice or t:goblin"),
        # At either end of the query or of a group.
        ("//fire", "fire"),
        ("/fire", "fire"),
        ("fire //", "fire"),
        ("fire/", "fire"),
        ("/fire/", "fire"),
        ("(fire // ice)", "(fire ice)"),
        ("( fire /)", "(fire)"),
        ("(/ fire)", "(fire)"),
        ("(fire) / t:instant", "(fire) t:instant"),
        # Around the boolean words and a negation.
        ("-fire // ice", "-fire ice"),
        ("fire // -ice", "fire -ice"),
        ("fire/-ice", "fire -ice"),
        ("pow>=2/-power", "pow>=2 -power"),
        ("fire or // ice", "fire or ice"),
        ("fire / or ice", "fire or ice"),
        ("fire and / ice", "fire and ice"),
        # Behind any kind of term.
        ("t:goblin // fire", "t:goblin fire"),
        ("t:goblin /", "t:goblin"),
        ("o:fire /", "o:fire"),
        ('o:"fire" /', 'o:"fire"'),
        ("o:/fire/ // ice", "o:/fire/ ice"),
        ("e:khm /", "e:khm"),
        ("r:rare/", "r:rare"),
        ("c:r/ fire", "c:r fire"),
        ("cmc>=3 /", "cmc>=3"),
        ("cmc>=3/", "cmc>=3"),
        ("pow>=2/ fire", "pow>=2 fire"),
        ("!fire /", "!fire"),
        ("!fire // ice", "!fire ice"),
        ('!"Fire // Ice" // bolt', '!"Fire // Ice" bolt'),
        ("fire // ice t:instant", "fire ice t:instant"),
        # A slash with a space before it is not glued to the value in front of it.
        ("name:fire / ice", "name:fire ice"),
        ("o:fire /ice", "o:fire ice"),
        ("o:1 / 2", "o:1 2"),
    ],
)
def test_a_slash_no_term_has_taken_is_nothing(parse_query, query: str, same_as: str) -> None:
    assert sql(parse_query, query) == sql(parse_query, same_as)


@pytest.mark.parametrize(
    argnames=["query", "attribute", "value"],
    argvalues=[
        ("o:1/1", "card.oracle_text", "%1/1%"),
        ("o:+1/+1", None, None),
        ("o:fire/ice", "card.oracle_text", "%fire/ice%"),
        ("o:fire/", "card.oracle_text", "%fire/%"),
        ("o:fire//ice", "card.oracle_text", "%fire//ice%"),
        ("o:a/b-c", "card.oracle_text", "%a/b-c%"),
        ("o:a-b/c", "card.oracle_text", "%a-b/c%"),
        ("name:colossus//dark", "card.card_name_folded", "%colossus//dark%"),
        ("e:khm/", "card.card_set_code", "khm/"),
    ],
)
def test_a_slash_glued_to_a_text_value_is_kept(parse_query, query: str, attribute: str | None, value: str | None) -> None:
    """`o:1/1` is 1,432 on Scryfall and `o:fire/ice` a 404: the value keeps its characters."""
    if attribute is None:
        # A sign is not a value character in either parser; quote it (`o:"+1/+1"`).
        with pytest.raises(ValueError, match="Failed to"):
            parse_query(query)
        return
    where, params = sql(parse_query, query)
    assert attribute in where
    assert list(params.values()) == [value]


def test_a_slash_glued_to_a_text_value_ends_at_the_space(parse_query) -> None:
    where, params = sql(parse_query, "o:fire/ ice")
    assert "card.oracle_text" in where
    assert sorted(params.values()) == ["%fire/%", "%ice%"]


@pytest.mark.parametrize(
    argnames=["query", "name"],
    argvalues=[
        ("!fire//ice", "fire//ice"),
        ("!lightning/bolt", "lightning/bolt"),
        ("!fire/", "fire/"),
    ],
)
def test_a_slash_glued_to_an_exact_name_is_kept(parse_query, query: str, name: str) -> None:
    assert sql(parse_query, query) == sql(parse_query, f'!"{name}"')


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        ("power/2>1",),
        ("cmc / 2 > 1",),
        ("cmc>power/2",),
        ("cmc>=3 / 2",),
        ("(power+1)/2>1",),
        ("cmc>power/2 - 1",),
    ],
)
def test_division_is_untouched(parse_query, query: str) -> None:
    """Between two numeric terms a slash still divides."""
    where, _ = sql(parse_query, query)
    assert " / " in where
    assert "card_name" not in where


@pytest.mark.parametrize(
    argnames=["query", "pattern"],
    argvalues=[
        ("o:/fire/", "fire.*x"),
        ("name:/^fire.*x$/ // ice", "^fire.*x$"),
        ("power/2>1 name:/a.*/ // ice", "a.*"),
    ],
)
def test_a_regex_still_opens_only_behind_an_operator(parse_query, query: str, pattern: str) -> None:
    tree = json.dumps(parse_query(query.replace("/fire/", "/fire.*x/")).to_json())
    assert tree.count("RegexValueNode") == 1
    assert json.dumps(pattern)[1:-1] in tree


@pytest.mark.parametrize(
    argnames=["query"],
    argvalues=[
        # Nothing but slashes: "All of your terms were ignored." on Scryfall, an error here --
        # never every card.
        ("/",),
        ("//",),
        ("///",),
        (" / / ",),
        ("(/)",),
        # Directly behind an operator a slash opens a regex, and it has to close.
        ("o:/fire",),
        ("name:/unclosed",),
        ("fire // o:/unclosed",),
        # Behind a negation or the exact-name bang it has no reading.
        ("-/fire",),
        ("!/fire",),
        # Nothing is left for the boolean word to join.
        ("fire or /",),
        ("/ or fire",),
    ],
)
def test_what_a_slash_does_not_excuse(parse_query, query: str) -> None:
    with pytest.raises(ValueError, match=r"(Failed to (parse|lex) query|Invalid query syntax|Unmatched|Parse error)"):
        parse_query(query)
