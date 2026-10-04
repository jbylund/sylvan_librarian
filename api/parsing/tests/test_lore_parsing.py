"""Tests for Scryfall's `lore:` keyword.

Measured on api.scryfall.com 2026-10-04: `lore:<text>` is the value as a literal, case-insensitive
substring of any of five fields -- the name as printed, the flavor name, the flavor text, the oracle
text or the type line. `lore:jace`, `lore=jace` and `lore:JACE` are each 171 cards; `lore:godzilla`
is 8 (flavor names); `lore:god` has every Demigod; `-lore:zzzzqq e:khm` is all 305.
"""

from __future__ import annotations

from functools import partial

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.card_query_nodes import LORE_SQL_TEMPLATE, CardAttributeNode, CardBinaryOperatorNode, lore_needle
from api.parsing.nodes import NotNode, QueryContext, RegexValueNode
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)
BOTH_PARSERS = pytest.mark.parametrize("parse", [parse_scryfall_query, parse_with_pyparsing], ids=["hand", "pyparsing"])


def _sql(parse, query: str) -> tuple[str, dict]:
    context = QueryContext()
    return parse(query).root.to_sql(context), dict(context)


def _lore_sql(pattern: str) -> tuple[str, dict]:
    """The SQL and parameters a `lore:` term searching for `pattern` renders as."""
    context = QueryContext()
    placeholder = context.add(pattern)
    return f"({LORE_SQL_TEMPLATE.format(pattern=placeholder)})", dict(context)


class TestNeedle:
    """`lore_needle` is the one normalization both lanes search with."""

    @pytest.mark.parametrize(
        ("value", "needle"),
        [
            ("jace", "jace"),
            ("JACE", "jace"),
            ("Lim-Dûl", "lim-dûl"),  # accents are kept: `lore:"lim-dul"` is 0, `lore:"lim-dûl"` 35
            ("ÉOWYN", "éowyn"),
            ("æther", "aether"),  # `lore:æther` and `lore:aether` are the same cards
            ("Æther", "aether"),
            ("god  of", "god of"),  # `lore:"god  of" e:khm` is 17, the same as `lore:"god of"`
            ("god     of   the", "god of the"),
            (" of ", " of "),  # an edge space is kept: `lore:" of "` 174, `"of "` 176, `" of"` 175
            ("  of  ", " of "),
            ("", ""),
            (" // ", " // "),
        ],
    )
    def test_needle(self, value: str, needle: str) -> None:
        assert lore_needle(value) == needle

    def test_needle_is_idempotent(self) -> None:
        """The engine normalizes what it is sent again, so a second pass must change nothing."""
        for value in ("JACE", "Æther  Vial ", "Lim-Dûl", " of "):
            assert lore_needle(lore_needle(value)) == lore_needle(value)


class TestParsing:
    """`lore` parses to its own attribute under both parsers."""

    @BOTH_PARSERS
    @pytest.mark.parametrize("operator", [":", "=", "!=", "<", "<=", ">", ">="])
    def test_lore_resolves_to_its_attribute(self, parse, operator: str) -> None:
        node = parse(f"lore{operator}jace").root

        assert isinstance(node, CardBinaryOperatorNode)
        assert isinstance(node.lhs, CardAttributeNode)
        assert node.lhs.attribute_name == "lore"
        assert node.operator == operator

    @BOTH_PARSERS
    def test_keyword_is_case_insensitive(self, parse) -> None:
        assert parse("LORE:jace").root.lhs.attribute_name == "lore"
        assert parse("Lore:jace").root.lhs.attribute_name == "lore"

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "attribute"),
        [("flavor:jace", "flavor_text"), ("ft:jace", "flavor_text"), ("o:jace", "oracle_text"), ("name:jace", "card_name")],
    )
    def test_lore_shadows_no_other_text_keyword(self, parse, query: str, attribute: str) -> None:
        assert parse(query).root.lhs.attribute_name == attribute


class TestSql:
    """One pattern, five fields, two-valued."""

    def test_template_reads_the_five_fields(self) -> None:
        for field in ("card.card_name", "card.flavor_text", "card.type_line", "card.flavor_name"):
            assert f"lower({field}) LIKE {{pattern}}" in LORE_SQL_TEMPLATE
        # The oracle text is searched without its reminder text: `lore:ft e:khm` is 22 cards on
        # Scryfall, and 41 with the Sagas' "(... after your draw step ...)" left in.
        assert "lower(card.oracle_text) LIKE" not in LORE_SQL_TEMPLATE
        assert r"lower(regexp_replace(card.oracle_text, '[ \t\n\r\f]*\([^)]*(\)|$)', '', 'g')) LIKE {pattern}" in LORE_SQL_TEMPLATE
        # A face's flavor name, for the printings that carry it there rather than on the card.
        assert "jsonb_array_elements(card.card_faces)" in LORE_SQL_TEMPLATE
        assert "lower(lore_face ->> 'flavor_name') LIKE {pattern}" in LORE_SQL_TEMPLATE
        # The name as printed, not the accent-folded one `name:` searches.
        assert "card_name_folded" not in LORE_SQL_TEMPLATE
        # Two-valued: a row with no oracle text or flavor name is FALSE, not NULL.
        assert LORE_SQL_TEMPLATE.startswith("COALESCE(")
        assert LORE_SQL_TEMPLATE.endswith(", FALSE)")

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "pattern"),
        [
            ("lore:jace", "%jace%"),
            ("lore=jace", "%jace%"),
            ("lore:JACE", "%jace%"),
            ('lore:"god of"', "%god of%"),
            ('lore:"god  of"', "%god of%"),
            ('lore:" of "', "% of %"),
            ('lore:" // "', "% // %"),
            ("lore:æther", "%aether%"),
            ('lore:"lim-dûl"', "%lim-dûl%"),
            ('lore:"théoden, strength restored"', "%théoden, strength restored%"),
            ("lore:2", "%2%"),
            # A metacharacter-free regex is the text it spells.
            ("lore:/jace/", "%jace%"),
        ],
    )
    def test_lore_is_one_like_pattern_over_five_fields(self, parse, query: str, pattern: str) -> None:
        assert _sql(parse, query) == _lore_sql(pattern)

    @BOTH_PARSERS
    def test_the_value_is_a_phrase_not_words(self, parse) -> None:
        """`oracle:"god of"` lets anything sit between the words; `lore:` does not."""
        _, lore_params = _sql(parse, 'lore:"god of"')
        _, oracle_params = _sql(parse, 'oracle:"god of"')

        assert list(lore_params.values()) == ["%god of%"]
        assert list(oracle_params.values()) == ["%god%of%"]

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "pattern"),
        [
            ('lore:"100%"', r"%100\%%"),
            ('lore:"pure_gold"', r"%pure\_gold%"),
            ('lore:"a\\\\b"', "%a\\\\b%"),
        ],
    )
    def test_like_metacharacters_in_the_value_are_literal(self, parse, query: str, pattern: str) -> None:
        _, params = _sql(parse, query)

        assert list(params.values()) == [pattern]

    @BOTH_PARSERS
    def test_negation_is_a_plain_not(self, parse) -> None:
        """`-lore:zzzzqq e:khm` is all 305 on Scryfall: the complement, with no third value."""
        node = parse("-lore:jace").root
        sql, params = _sql(parse, "-lore:jace")
        inner_sql, inner_params = _lore_sql("%jace%")

        assert isinstance(node, NotNode)
        assert sql == f"NOT ({inner_sql})"
        assert params == inner_params

    @BOTH_PARSERS
    @pytest.mark.parametrize("operator", ["!=", "<", "<=", ">", ">="])
    def test_a_comparison_matches_nothing(self, parse, operator: str) -> None:
        """`lore>jace` and `lore!=jace` match nothing on Scryfall."""
        assert _sql(parse, f"lore{operator}jace") == ("FALSE", {})

    @BOTH_PARSERS
    def test_a_regular_expression_is_refused(self, parse) -> None:
        node = parse("lore:/^jace$/").root

        assert isinstance(node.rhs, RegexValueNode)
        with pytest.raises(ValueError, match="lore: takes text, not a regular expression"):
            node.to_sql(QueryContext())

    @BOTH_PARSERS
    def test_lore_composes_with_other_terms(self, parse) -> None:
        sql, params = generate_sql_query(parse("lore:jace e:khm"))
        inner_sql, inner_params = _lore_sql("%jace%")

        assert inner_sql in sql
        assert "card.card_set_code" in sql
        assert inner_params.items() <= params.items()


class TestEngineJson:
    """The engine is sent the same needle the SQL path searches for."""

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "operator", "needle"),
        [
            ("lore:jace", ":", "jace"),
            ("lore=JACE", "=", "jace"),
            ('lore:"god  of"', ":", "god of"),
            ('lore:" of "', ":", " of "),
            ("lore:æther", ":", "aether"),
            ('lore:"lim-dûl"', ":", "lim-dûl"),
        ],
    )
    def test_engine_json_carries_the_normalized_needle(self, parse, query: str, operator: str, needle: str) -> None:
        assert parse(query).root.to_json() == {
            "node_type": "CardBinaryOperatorNode",
            "kwargs": {
                "lhs": {"node_type": "CardAttributeNode", "kwargs": {"attribute_name": "lore", "original_attribute": "lore"}},
                "op": operator,
                "rhs": {"node_type": "StringValueNode", "kwargs": {"value": needle}},
            },
        }

    @BOTH_PARSERS
    def test_a_regular_expression_reaches_the_engine_as_one(self, parse) -> None:
        """So the engine can decline it, rather than searching for the pattern's text."""
        rhs = parse("lore:/^jace$/").root.to_json()["kwargs"]["rhs"]

        assert rhs == {"node_type": "RegexValueNode", "kwargs": {"value": "^jace$"}}


class TestExplanation:
    """`lore:` and `lore=` are the same search, so they read the same."""

    @BOTH_PARSERS
    @pytest.mark.parametrize("query", ["lore:jace", "lore=jace"])
    def test_explanation(self, parse, query: str) -> None:
        assert parse(query).to_human_explanation() == "the lore contains jace"
