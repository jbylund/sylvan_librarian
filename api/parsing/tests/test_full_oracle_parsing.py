"""Tests for the full-oracle (fo:/fulloracle:) spellings of the oracle text column."""

from __future__ import annotations

import pytest

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import Query


class TestFullOracleParsing:
    r"""`fo:` and `fulloracle:` are Scryfall's FULL-oracle operator.

    They share `oracle_text`'s column deliberately. The stored `oracle_text` IS the full text,
    reminder text included, so the SQL path answers `fo:` from it with no second column and no
    migration; what tells the two apart is `original_attribute`, which the card engine reads
    because ITS searchable oracle column has reminder text stripped out of it the way Scryfall's
    `o:` does.

    Measured against api.scryfall.com on 2026-08-16:

        fo:lifelink                                       713
        fo:"damage dealt by this creature also causes"     71   (o: answers 0)
        fo:draw e:khm                                      57   (o: answers 39)
        fo:/\\(this creature/                            1,098   (o:/\\(/ is 0 corpus-wide)
    """

    @pytest.mark.parametrize("spelling", ["fo", "fulloracle"])
    def test_full_oracle_spellings_resolve_to_the_oracle_text_column(self, parse_query, spelling: str) -> None:
        result = parse_query(f"{spelling}:lifelink")

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.attribute_name == "oracle_text"
        assert binary_op.operator == ":"
        assert binary_op.rhs.value == "lifelink"

    @pytest.mark.parametrize("spelling", ["fo", "fulloracle"])
    def test_the_spelling_survives_as_original_attribute(self, parse_query, spelling: str) -> None:
        """The ONE thing that distinguishes fo: from o: downstream."""
        binary_op = parse_query(f"{spelling}:lifelink").root

        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.original_attribute == spelling
        assert binary_op.to_json()["kwargs"]["lhs"]["kwargs"]["original_attribute"] == spelling

    def test_o_keeps_its_own_original_attribute(self, parse_query) -> None:
        binary_op = parse_query("o:lifelink").root

        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.original_attribute == "o"

    @pytest.mark.parametrize("spelling", ["fo", "fulloracle"])
    def test_full_oracle_takes_a_regex(self, parse_query, spelling: str) -> None:
        r"""A regex reaches the same column with the same spelling attached.

        Regex is the shape that most needs the unstripped text: `o:/\\(/` returns zero rows on
        api.scryfall.com corpus-wide, because no parenthesis survives the strip.
        """
        binary_op = parse_query(f"{spelling}:/lifelink/").root

        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.attribute_name == "oracle_text"
        assert binary_op.lhs.original_attribute == spelling
        assert binary_op.rhs.value == "lifelink"
