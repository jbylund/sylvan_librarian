"""A comma right after a card-name word is dropped, as Scryfall does.

Typing a card name into a search box often leaves a comma behind ("Jace, Reality Sculptor" -> "jace, re"). The hand
parser had no token for `,` and failed to lex the whole query. Scryfall ignores such a comma (verified live 2026-10-04:
`name:jace, re` and `name:jace re` both return the same 6 cards, `jace, re` too), while a quoted comma stays literal
(`name:"jace," re` returns 2).
"""

import pytest

from api.parsing import generate_sql_query, parse_scryfall_query


def _sql(query: str) -> tuple:
    return generate_sql_query(parse_scryfall_query(query))


@pytest.mark.parametrize(
    ("with_comma", "without"),
    [
        ("name:jace, re", "name:jace re"),
        ("jace, re", "jace re"),
        ("(name:jace, re)", "(name:jace re)"),
        ("name:jace,", "name:jace"),
    ],
)
def test_a_comma_after_a_name_word_is_ignored(with_comma: str, without: str) -> None:
    assert _sql(with_comma) == _sql(without)


def test_a_quoted_comma_stays_part_of_the_name() -> None:
    assert _sql('name:"jace," re') != _sql("name:jace re")
