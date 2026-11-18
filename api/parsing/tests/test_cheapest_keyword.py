"""Tests for Scryfall's `cheapest:usd` / `cheapest:eur` / `cheapest:tix`.

Measured on api.scryfall.com 2026-10-04: `cheapest:usd e:khm` is 222 of Kaldheim's 407 printings,
`-cheapest:usd e:khm` 5, `-(cheapest:usd) e:khm` 185 and `-(-cheapest:usd) e:khm` 402 -- the
negated TERM is an expression of its own, and only the negated GROUP is the complement. The
answers are decided at import (`magic.cards.cheapest_codes`); the parser's part is the currency
vocabulary and telling `-x` from `-(x)`.
"""

from __future__ import annotations

from functools import partial

import pytest

from api.parsing import NotNode, generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.card_query_nodes import CheapestNode
from api.parsing.db_info import (
    ALIAS_TO_FIELD_INFOS,
    CHEAPEST_CURRENCY_SYMBOLS,
    CHEAPEST_CURRENCY_WORDS,
    CHEAPEST_NEGATED_TERM,
    CHEAPEST_SHIFTS,
    CHEAPEST_TERM,
    CHEAPEST_UNKNOWN,
    ParserClass,
)
from api.parsing.nodes import AndNode, OrNode
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)
BOTH_PARSERS = pytest.mark.parametrize("parse", [parse_scryfall_query, parse_with_pyparsing], ids=["hand", "pyparsing"])

CURRENCIES = ["usd", "eur", "tix"]


def _sql(currency: str, *, negated_term: bool = False) -> str:
    """The SQL a term reads: this currency's unknown bit, then its term or negated-term bit."""
    shift = CHEAPEST_SHIFTS[currency]
    answer = (CHEAPEST_NEGATED_TERM if negated_term else CHEAPEST_TERM) << shift
    return f"(CASE WHEN (card.cheapest_codes & {CHEAPEST_UNKNOWN << shift}) = 0 THEN (card.cheapest_codes & {answer}) <> 0 END)"


class TestCurrencyWords:
    """The value is one of eight words, in any case, after `:` or `=`."""

    @BOTH_PARSERS
    @pytest.mark.parametrize(("word", "currency"), list(CHEAPEST_CURRENCY_WORDS.items()), ids=list(CHEAPEST_CURRENCY_WORDS))
    @pytest.mark.parametrize("operator", [":", "="])
    def test_each_word_names_its_currency(self, parse, word: str, currency: str, operator: str) -> None:
        assert parse(f"cheapest{operator}{word}").root == CheapestNode(currency)

    def test_the_vocabulary_is_the_one_measured(self) -> None:
        assert CHEAPEST_CURRENCY_WORDS == {
            "usd": "usd",
            "$": "usd",
            "dollar": "usd",
            "eur": "eur",
            "euro": "eur",
            "€": "eur",
            "tix": "tix",
            "mtgo": "tix",
        }
        assert set(CHEAPEST_SHIFTS) == set(CHEAPEST_CURRENCY_WORDS.values())
        assert {"$", "€"} == CHEAPEST_CURRENCY_SYMBOLS
        assert {fi.parser_class for fi in ALIAS_TO_FIELD_INFOS["cheapest"]} == {ParserClass.CURRENCY}

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "currency"),
        [
            ("CHEAPEST:USD", "usd"),
            ("Cheapest:Dollar", "usd"),
            ("cheapest:EURO", "eur"),
            ("cheapest=MtGo", "tix"),
            ('cheapest:"usd"', "usd"),
            ("cheapest:'eur'", "eur"),
            ('cheapest:"$"', "usd"),
            ('cheapest="€"', "eur"),
        ],
    )
    def test_case_and_quotes_do_not_matter(self, parse, query: str, currency: str) -> None:
        assert parse(query).root == CheapestNode(currency)

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        "query",
        [
            # Each is "Unknown currency" on Scryfall.
            "cheapest:dollars",
            "cheapest:euros",
            "cheapest:ticket",
            "cheapest:tickets",
            "cheapest:usdfoil",
            "cheapest:eurfoil",
            "cheapest:tcgplayer",
            "cheapest:cardmarket",
            "cheapest:usd_foil",
            "cheapest:usd-foil",
            "cheapest:1",
            'cheapest:""',
            "cheapest:",
            "cheapest:/usd/",
        ],
    )
    def test_any_other_value_is_refused(self, parse, query: str) -> None:
        with pytest.raises(ValueError, match="arse"):
            parse(query)

    @BOTH_PARSERS
    @pytest.mark.parametrize("operator", [">", ">=", "<", "<=", "!="])
    def test_any_other_operator_is_refused(self, parse, operator: str) -> None:
        """`cheapest>usd` and `cheapest!=usd` match nothing on Scryfall; here they do not parse."""
        with pytest.raises(ValueError, match="Failed to parse query"):
            parse(f"cheapest{operator}usd")

    @BOTH_PARSERS
    @pytest.mark.parametrize("query", ["o:$", "name:€", "$", "€", "t:elf $", "usd>$", "c:$", "r:€"])
    def test_a_currency_symbol_is_a_value_nowhere_else(self, parse, query: str) -> None:
        """`$` and `€` are admitted as the value of `cheapest` only; everywhere else they still do not parse."""
        with pytest.raises(ValueError, match=r"Failed to (lex|parse) query"):
            parse(query)

    @BOTH_PARSERS
    def test_the_bare_word_is_still_a_name(self, parse) -> None:
        sql, _ = generate_sql_query(parse("cheapest"))

        assert sql.startswith("(lower(card.card_name_folded) LIKE ")


class TestNegatedTermAndGroup:
    """`-cheapest:usd` is its own expression; `-(cheapest:usd)` is the complement."""

    @BOTH_PARSERS
    @pytest.mark.parametrize("currency", CURRENCIES)
    def test_a_minus_on_the_term_is_the_negated_term(self, parse, currency: str) -> None:
        assert parse(f"-cheapest:{currency}").root == CheapestNode(currency, negated_term=True)

    @BOTH_PARSERS
    @pytest.mark.parametrize("currency", CURRENCIES)
    def test_a_minus_on_a_group_is_the_complement(self, parse, currency: str) -> None:
        assert parse(f"-(cheapest:{currency})").root == NotNode(CheapestNode(currency))

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            # `-(-cheapest:usd) e:khm` is 402 on Scryfall: the complement of the negated term.
            ("-(-cheapest:usd)", NotNode(CheapestNode("usd", negated_term=True))),
            ("-((cheapest:usd))", NotNode(CheapestNode("usd"))),
            ("-(cheapest:$)", NotNode(CheapestNode("usd"))),
            ("-cheapest:$", CheapestNode("usd", negated_term=True)),
            ("-cheapest=€", CheapestNode("eur", negated_term=True)),
            ('-cheapest:"mtgo"', CheapestNode("tix", negated_term=True)),
            ("- cheapest:usd", CheapestNode("usd", negated_term=True)),
            ("-CHEAPEST:EUR", CheapestNode("eur", negated_term=True)),
            ("-(cheapest:usd or cheapest:eur)", NotNode(OrNode([CheapestNode("usd"), CheapestNode("eur")]))),
            ("-(cheapest:usd -cheapest:eur)", NotNode(AndNode([CheapestNode("usd"), CheapestNode("eur", negated_term=True)]))),
            ("(cheapest:usd) -cheapest:tix", AndNode([CheapestNode("usd"), CheapestNode("tix", negated_term=True)])),
            ("cheapest:usd -(cheapest:tix)", AndNode([CheapestNode("usd"), NotNode(CheapestNode("tix"))])),
        ],
    )
    def test_the_two_negations_are_told_apart(self, parse, query: str, expected: object) -> None:
        assert parse(query).root == expected

    @BOTH_PARSERS
    def test_the_term_composes_with_other_terms(self, parse) -> None:
        sql, params = generate_sql_query(parse("e:khm -cheapest:usd t:elf"))

        set_clause, cheapest_clause, type_clause = sql.split(" AND ")
        assert set_clause.startswith("((card.card_set_code = ")
        assert cheapest_clause == _sql("usd", negated_term=True)
        assert "card.card_subtypes" in type_clause
        assert "khm" in params.values()


class TestSql:
    """Both polarities are a bit test on one smallint, NULL when the currency's unknown bit is set."""

    @BOTH_PARSERS
    @pytest.mark.parametrize("currency", CURRENCIES)
    def test_the_term_and_the_negated_term_read_their_own_bit(self, parse, currency: str) -> None:
        term, term_params = generate_sql_query(parse(f"cheapest:{currency}"))
        negated, negated_params = generate_sql_query(parse(f"-cheapest:{currency}"))

        assert term == _sql(currency)
        assert negated == _sql(currency, negated_term=True)
        assert term != negated
        assert term_params == {}
        assert negated_params == {}

    @BOTH_PARSERS
    def test_the_negated_group_is_sql_negation(self, parse) -> None:
        """NOT over a NULL CASE stays NULL: a printing with no answer is in neither list."""
        assert generate_sql_query(parse("-(cheapest:usd)"))[0] == f"NOT ({_sql('usd')})"
        assert generate_sql_query(parse("-(-cheapest:eur)"))[0] == f"NOT ({_sql('eur', negated_term=True)})"

    def test_the_three_currencies_do_not_share_a_bit(self) -> None:
        masks = [
            bit << shift for shift in CHEAPEST_SHIFTS.values() for bit in (CHEAPEST_TERM, CHEAPEST_NEGATED_TERM, CHEAPEST_UNKNOWN)
        ]

        assert len(set(masks)) == len(masks)
        assert sum(masks) == (1 << 9) - 1  # nine bits of a smallint


class TestEngineJson:
    """The engine receives the canonical currency and the polarity; the word table stays in Python."""

    @BOTH_PARSERS
    @pytest.mark.parametrize(("word", "currency"), list(CHEAPEST_CURRENCY_WORDS.items()), ids=list(CHEAPEST_CURRENCY_WORDS))
    def test_the_json_carries_the_canonical_currency(self, parse, word: str, currency: str) -> None:
        assert parse(f"cheapest:{word}").root.to_json() == {
            "node_type": "CheapestNode",
            "kwargs": {"currency": currency, "negated_term": False},
        }

    @BOTH_PARSERS
    def test_the_negated_term_is_a_flag_and_the_negated_group_a_not_node(self, parse) -> None:
        assert parse("-cheapest:usd").root.to_json() == {
            "node_type": "CheapestNode",
            "kwargs": {"currency": "usd", "negated_term": True},
        }
        assert parse("-(cheapest:usd)").root.to_json() == {
            "node_type": "NotNode",
            "kwargs": {"operand": {"node_type": "CheapestNode", "kwargs": {"currency": "usd", "negated_term": False}}},
        }


class TestCheapestNode:
    """The node itself: construction, equality, explanation."""

    def test_an_unknown_currency_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown currency"):
            CheapestNode("dollar")
        with pytest.raises(ValueError, match="Unknown currency"):
            CheapestNode.from_word("gbp")

    def test_equality_and_hash_follow_currency_and_polarity(self) -> None:
        assert CheapestNode("usd") == CheapestNode.from_word(" Dollar ")
        assert hash(CheapestNode("usd")) == hash(CheapestNode("usd"))
        assert CheapestNode("usd") != CheapestNode("eur")
        assert CheapestNode("usd") != CheapestNode("usd", negated_term=True)
        assert CheapestNode("usd").as_negated_term() == CheapestNode("usd", negated_term=True)
        assert len({CheapestNode("usd"), CheapestNode("usd"), CheapestNode("usd", negated_term=True)}) == 2

    @BOTH_PARSERS
    def test_a_repeated_term_is_deduplicated_but_the_two_polarities_are_not(self, parse) -> None:
        assert parse("cheapest:usd cheapest:$").root == CheapestNode("usd")
        assert parse("cheapest:usd -cheapest:usd").root == AndNode([CheapestNode("usd"), CheapestNode("usd", negated_term=True)])

    @BOTH_PARSERS
    def test_explanation_says_which_polarity(self, parse) -> None:
        assert parse("cheapest:usd").to_human_explanation() == "the printing is the card's cheapest in USD"
        assert parse("-cheapest:tix").to_human_explanation() == "the printing is not the card's cheapest in TIX"
        assert parse("-(cheapest:eur)").to_human_explanation() == "not (the printing is the card's cheapest in EUR)"


if __name__ == "__main__":
    pytest.main([__file__])
