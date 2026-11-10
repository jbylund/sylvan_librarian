"""Tests for Scryfall's `new:` keyword.

Measured on api.scryfall.com: `new:rarity` (`unique=prints`, extras in) is 38,943 printings, the
first of their card at their rarity, and `-new:rarity` the 79,532 that are not (2026-10-04);
`new:card` 35,158, `new:frame` 45,061, `new:foil` 29,671, `new:nonfoil` 35,018 and `new:art` 52,064,
each read whole and each exactly the rule the import writes (2026-10-09). Scryfall honours
twenty-nine `new:` words, fourteen lists; those six are the ones this table can answer, under the
ten spellings Scryfall has for them, and every other word is refused. The answers are decided at
import (`magic.cards.new_rarity`, `magic.cards.new_flags`); the parser's part is the vocabulary, and
the negation is the ordinary complement, so no folding is needed as it is for `cheapest:`.
"""

from __future__ import annotations

from functools import partial

import pytest

from api.parsing import NotNode, generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.card_query_nodes import NewNode
from api.parsing.db_info import (
    ALIAS_TO_FIELD_INFOS,
    NEW_FLAG_BITS,
    NEW_KEYWORD_ALIASES,
    NEW_KEYWORD_COLUMNS,
    NEW_KEYWORD_EXPLANATIONS,
    NEW_KEYWORD_VALUES,
    ParserClass,
)
from api.parsing.nodes import AndNode, OrNode
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)
BOTH_PARSERS = pytest.mark.parametrize("parse", [parse_scryfall_query, parse_with_pyparsing], ids=["hand", "pyparsing"])

# The other words Scryfall honours -- none of them answerable from the rows this table holds, see
# NEW_FLAG_BITS -- and some it does not.
REFUSED_VALUES = [
    "language",
    "lang",
    "artist",
    "illustrator",
    "flavor",
    "ft",
    "flavortext",
    "game",
    "games",
    "mtgo",
    "modo",
    "magiconline",
    "arena",
    "mtga",
    "astral",
    "microprose",
    "micro",
    "sega",
    "dreamcast",
    "nonsense",
    "rarities",
    "rarity_",
    "cards",
    "arts",
    "etched",
    "set",
    "print",
    "1",
]

# Every spelling answered, and the value it is.
SPELLINGS = [(value, value) for value in NEW_KEYWORD_VALUES] + list(NEW_KEYWORD_ALIASES.items())


class TestVocabulary:
    """One word, in any case, after `:` or `=`."""

    @BOTH_PARSERS
    @pytest.mark.parametrize("operator", [":", "="])
    def test_rarity_is_a_new_node(self, parse, operator: str) -> None:
        assert parse(f"new{operator}rarity").root == NewNode("rarity")

    def test_the_vocabulary_is_the_one_measured(self) -> None:
        assert NEW_KEYWORD_COLUMNS == {"rarity": "new_rarity"}
        assert NEW_FLAG_BITS == {"card": 1, "frame": 2, "foil": 64, "nonfoil": 128, "art": 512}
        assert NEW_KEYWORD_ALIASES == {"paper": "card", "printed": "card", "cardboard": "card", "illustration": "art"}
        assert NEW_KEYWORD_VALUES == ("rarity", "card", "frame", "foil", "nonfoil", "art")
        assert set(NEW_KEYWORD_EXPLANATIONS) == set(NEW_KEYWORD_VALUES)
        assert set(NEW_KEYWORD_ALIASES.values()) <= set(NEW_KEYWORD_VALUES)
        assert not set(NEW_KEYWORD_ALIASES) & set(NEW_KEYWORD_VALUES)
        assert {fi.parser_class for fi in ALIAS_TO_FIELD_INFOS["new"]} == {ParserClass.NEW}

    @BOTH_PARSERS
    @pytest.mark.parametrize(("word", "value"), SPELLINGS)
    @pytest.mark.parametrize("operator", [":", "="])
    def test_every_spelling_is_its_value(self, parse, operator: str, word: str, value: str) -> None:
        assert parse(f"new{operator}{word}").root == NewNode(value)
        assert parse(f'NEW{operator}"{word.upper()}"').root == NewNode(value)

    @BOTH_PARSERS
    @pytest.mark.parametrize("query", ["NEW:RARITY", "New:Rarity", "new=RARITY", 'new:"rarity"', "new:'rarity'", 'NEW="Rarity"'])
    def test_case_and_quotes_do_not_matter(self, parse, query: str) -> None:
        assert parse(query).root == NewNode("rarity")

    @BOTH_PARSERS
    @pytest.mark.parametrize("value", REFUSED_VALUES)
    def test_any_other_value_is_refused(self, parse, value: str) -> None:
        """A value this table cannot answer as Scryfall does is refused: none returns cards."""
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
        assert "new_flags" not in generate_sql_query(parse("is:new"))[0]


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
        assert parse("new:card new:paper new=printed new:cardboard").root == NewNode("card")
        assert parse("new:art -new:illustration").root == AndNode([NewNode("art"), NotNode(NewNode("art"))])
        assert parse("new:foil new:nonfoil").root == AndNode([NewNode("foil"), NewNode("nonfoil")])
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
    @pytest.mark.parametrize(("word", "value"), [pair for pair in SPELLINGS if pair[1] != "rarity"])
    def test_every_other_value_reads_its_bit_of_one_column(self, parse, word: str, value: str) -> None:
        """NULL & bit is NULL, so a row the sync has not reached is in neither polarity here too."""
        sql, params = generate_sql_query(parse(f"new:{word}"))

        assert sql == f"((card.new_flags & {NEW_FLAG_BITS[value]}) <> 0)"
        assert params == {}
        assert generate_sql_query(parse(f"-new:{word}"))[0] == f"NOT (((card.new_flags & {NEW_FLAG_BITS[value]}) <> 0))"

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
    @pytest.mark.parametrize(("word", "value"), [pair for pair in SPELLINGS if pair[1] != "rarity"])
    def test_a_flag_value_carries_its_bit(self, parse, word: str, value: str) -> None:
        """The engine holds no table of the values: the bit travels with the query."""
        assert parse(f"new:{word}").root.to_json() == {
            "node_type": "NewNode",
            "kwargs": {"value": value, "mask": NEW_FLAG_BITS[value]},
        }

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
            NewNode.from_word("flavor")
        with pytest.raises(ValueError, match="Unknown new: value"):
            NewNode("paper")

    def test_equality_and_hash_follow_the_value(self) -> None:
        assert NewNode("rarity") == NewNode.from_word(" Rarity ")
        assert hash(NewNode("rarity")) == hash(NewNode("rarity"))
        assert NewNode("rarity") != NotNode(NewNode("rarity"))
        assert len({NewNode("rarity"), NewNode("rarity")}) == 1
        assert NewNode("card") == NewNode.from_word(" Paper ") == NewNode.from_word("CARDBOARD")
        assert NewNode("card") != NewNode("art")
        assert len({NewNode(value) for value in NEW_KEYWORD_VALUES}) == len(NEW_KEYWORD_VALUES)

    @BOTH_PARSERS
    def test_explanation_says_which_polarity(self, parse) -> None:
        assert parse("new:rarity").to_human_explanation() == "the printing is the first of its card at its rarity"
        assert parse("-new:rarity").to_human_explanation() == "not (the printing is the first of its card at its rarity)"
        assert parse("new:paper").to_human_explanation() == "the printing is the first of its card on paper"
        assert parse("new:frame").to_human_explanation() == "the printing is the first of its card in its frame"
        assert parse("new:foil").to_human_explanation() == "the printing is the first of its card in foil"
        assert parse("new:nonfoil").to_human_explanation() == "the printing is the first of its card in nonfoil"
        assert parse("-new:illustration").to_human_explanation() == "not (the printing is the first with its artwork)"


if __name__ == "__main__":
    pytest.main([__file__])
