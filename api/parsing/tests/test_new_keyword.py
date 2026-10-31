"""Tests for Scryfall's `new:rarity`.

Measured on api.scryfall.com 2026-10-04: `new:rarity` (`unique=prints`, extras in) is 38,943
printings, the first of their card at their rarity, and `-new:rarity` the 79,532 that are not. Scryfall honours sixteen `new:` words; `rarity` is the only one measured to
be exactly a list of printings, so it is the only one answered here. The answer is decided at import
(`magic.cards.new_rarity`); the parser's part is the vocabulary, and the negation is the ordinary
complement, so no folding is needed as it is for `cheapest:`.
"""

from __future__ import annotations

from functools import partial

import pytest

from api.parsing import NotNode, generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.card_query_nodes import NewNode
from api.parsing.db_info import ALIAS_TO_FIELD_INFOS, NEW_KEYWORD_COLUMNS, ParserClass
from api.parsing.nodes import AndNode, OrNode
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)
BOTH_PARSERS = pytest.mark.parametrize("parse", [parse_scryfall_query, parse_with_pyparsing], ids=["hand", "pyparsing"])

# The other values Scryfall honours (and the ones it does not), none of them answered here.
REFUSED_VALUES = [
    "language",
    "art",
    "artist",
    "flavor",
    "frame",
    "card",
    "foil",
    "nonfoil",
    "paper",
    "game",
    "illustration",
    "ft",
    "flavortext",
    "lang",
    "mtgo",
    "arena",
    "nonsense",
    "rarities",
    "rarity_",
    "set",
    "print",
    "1",
]


class TestVocabulary:
    """One word, in any case, after `:` or `=`."""

    @BOTH_PARSERS
    @pytest.mark.parametrize("operator", [":", "="])
    def test_rarity_is_a_new_node(self, parse, operator: str) -> None:
        assert parse(f"new{operator}rarity").root == NewNode("rarity")

    def test_the_vocabulary_is_the_one_measured(self) -> None:
        assert NEW_KEYWORD_COLUMNS == {"rarity": "new_rarity"}
        assert {fi.parser_class for fi in ALIAS_TO_FIELD_INFOS["new"]} == {ParserClass.NEW}

    @BOTH_PARSERS
    @pytest.mark.parametrize("query", ["NEW:RARITY", "New:Rarity", "new=RARITY", 'new:"rarity"', "new:'rarity'", 'NEW="Rarity"'])
    def test_case_and_quotes_do_not_matter(self, parse, query: str) -> None:
        assert parse(query).root == NewNode("rarity")

    @BOTH_PARSERS
    @pytest.mark.parametrize("value", REFUSED_VALUES)
    def test_any_other_value_is_refused(self, parse, value: str) -> None:
        """`new:language` was measured and is not exact; the rest were not measured. None returns cards."""
        with pytest.raises(ValueError, match="arse"):
            parse(f"new:{value}")

    @BOTH_PARSERS
    @pytest.mark.parametrize("query", ['new:""', "new:", "new:/rarity/", "new:rarity:rarity"])
    def test_a_malformed_value_is_refused(self, parse, query: str) -> None:
        with pytest.raises(ValueError, match="arse"):
            parse(query)

    @BOTH_PARSERS
    @pytest.mark.parametrize("operator", [">", ">=", "<", "<=", "!="])
    def test_any_other_operator_is_refused(self, parse, operator: str) -> None:
        with pytest.raises(ValueError, match="Failed to parse query"):
            parse(f"new{operator}rarity")

    @BOTH_PARSERS
    def test_the_bare_word_is_still_a_name(self, parse) -> None:
        sql, _ = generate_sql_query(parse("new"))

        assert sql.startswith("(lower(card.card_name_folded) LIKE ")

    @BOTH_PARSERS
    def test_the_is_value_of_the_same_name_is_untouched(self, parse) -> None:
        """`is:new` is the frame class (`frame:2003 or ...`) and has nothing to do with `new:`."""
        assert "new_rarity" not in generate_sql_query(parse("is:new"))[0]


class TestNegation:
    """`-new:rarity` is the plain complement, so it is an ordinary NotNode: group or term alike."""

    @BOTH_PARSERS
    @pytest.mark.parametrize("query", ["-new:rarity", "- new:rarity", "-(new:rarity)", "-((new:rarity))"])
    def test_every_spelling_of_the_negation_is_a_not_node(self, parse, query: str) -> None:
        assert parse(query).root == NotNode(NewNode("rarity"))

    @BOTH_PARSERS
    def test_a_double_negation_is_two_nots(self, parse) -> None:
        """No special case: unlike `-(-cheapest:usd)`, nothing here folds a `-` into the node."""
        assert parse("-(-new:rarity)").root == NotNode(NotNode(NewNode("rarity")))

    @BOTH_PARSERS
    def test_it_composes_with_other_terms(self, parse) -> None:
        assert parse("new:rarity or -new:rarity").root == OrNode([NewNode("rarity"), NotNode(NewNode("rarity"))])
        assert isinstance(parse("new:rarity e:khm").root, AndNode)
        assert NewNode("rarity") in parse("new:rarity e:khm").root.operands

    @BOTH_PARSERS
    def test_a_repeated_term_is_deduplicated_but_not_its_complement(self, parse) -> None:
        assert parse("new:rarity new=rarity").root == NewNode("rarity")
        assert parse("new:rarity -new:rarity").root == AndNode([NewNode("rarity"), NotNode(NewNode("rarity"))])


class TestSql:
    """The term is the column, and the negation is SQL NOT -- NULL stays NULL, as it does on Scryfall."""

    @BOTH_PARSERS
    def test_the_term_reads_the_column(self, parse) -> None:
        sql, params = generate_sql_query(parse("new:rarity"))

        assert sql == "card.new_rarity"
        assert params == {}

    @BOTH_PARSERS
    def test_the_negation_is_sql_negation(self, parse) -> None:
        assert generate_sql_query(parse("-new:rarity"))[0] == "NOT (card.new_rarity)"

    @BOTH_PARSERS
    def test_the_term_composes_with_other_terms(self, parse) -> None:
        sql, params = generate_sql_query(parse("e:khm new:rarity t:elf"))

        set_clause, new_clause, type_clause = sql.split(" AND ")
        assert set_clause.startswith("((card.card_set_code = ")
        assert new_clause == "card.new_rarity"
        assert "card.card_subtypes" in type_clause
        assert "khm" in params.values()


class TestEngineJson:
    """The engine receives the canonical value; the polarity is an ordinary NotNode."""

    @BOTH_PARSERS
    def test_the_json_carries_the_value(self, parse) -> None:
        assert parse("NEW:Rarity").root.to_json() == {"node_type": "NewNode", "kwargs": {"value": "rarity"}}

    @BOTH_PARSERS
    def test_the_negation_is_a_not_node(self, parse) -> None:
        assert parse("-new:rarity").root.to_json() == {
            "node_type": "NotNode",
            "kwargs": {"operand": {"node_type": "NewNode", "kwargs": {"value": "rarity"}}},
        }


class TestNewNode:
    """The node itself: construction, equality, explanation."""

    def test_an_unanswered_value_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown new: value"):
            NewNode("language")
        with pytest.raises(ValueError, match="Unknown new: value"):
            NewNode.from_word("art")

    def test_equality_and_hash_follow_the_value(self) -> None:
        assert NewNode("rarity") == NewNode.from_word(" Rarity ")
        assert hash(NewNode("rarity")) == hash(NewNode("rarity"))
        assert NewNode("rarity") != NotNode(NewNode("rarity"))
        assert len({NewNode("rarity"), NewNode("rarity")}) == 1

    @BOTH_PARSERS
    def test_explanation_says_which_polarity(self, parse) -> None:
        assert parse("new:rarity").to_human_explanation() == "the printing is the first of its card at its rarity"
        assert parse("-new:rarity").to_human_explanation() == "not (the printing is the first of its card at its rarity)"


if __name__ == "__main__":
    pytest.main([__file__])
