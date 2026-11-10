"""Tests for the two printing-id keywords, `scryfallid:` and `illustrationid:`.

Measured on api.scryfall.com 2026-10-03: `scryfallid:860aa0fe-0337-458c-b864-5ef5733fbae6` is one
card (Reset, me3/48) under `scryfall_id:`, `=`, upper case and quotes alike, and
`illustrationid:9e42d409-161d-4e63-8982-71e313f27b2f` is one card, two under `unique=prints`.
"""

from __future__ import annotations

import pytest

from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode
from api.parsing.nodes import AndNode, NotNode, OrNode, Query, QueryContext

SCRYFALL_ID = "1c829d83-d5b8-4be7-80f7-55b42f52b309"
ILLUSTRATION_ID = "9e42d409-161d-4e63-8982-71e313f27b2f"
# An id with an all-digit group that starts with a zero, written QUOTED: unquoted, the lexer still
# re-spells `-0337` as a number on this branch, which is its own fix.
ZERO_LED_ID = "860aa0fe-0337-458c-b864-5ef5733fbae6"


class TestPrintingIdParsing:
    """Both keywords parse to their own column, under either spelling."""

    @pytest.mark.parametrize(
        argnames=("query", "column", "operator", "value"),
        argvalues=[
            (f"scryfallid:{SCRYFALL_ID}", "scryfall_id", ":", SCRYFALL_ID),
            (f"scryfall_id:{SCRYFALL_ID}", "scryfall_id", ":", SCRYFALL_ID),
            (f"scryfallid={SCRYFALL_ID}", "scryfall_id", "=", SCRYFALL_ID),
            (f"SCRYFALLID:{SCRYFALL_ID}", "scryfall_id", ":", SCRYFALL_ID),
            (f'scryfallid:"{ZERO_LED_ID}"', "scryfall_id", ":", ZERO_LED_ID),
            (f"illustrationid:{ILLUSTRATION_ID}", "illustration_id", ":", ILLUSTRATION_ID),
            (f"illustration_id:{ILLUSTRATION_ID}", "illustration_id", ":", ILLUSTRATION_ID),
            (f"illustrationid={ILLUSTRATION_ID}", "illustration_id", "=", ILLUSTRATION_ID),
            # The value is kept as written; the comparison lowercases it.
            (f"scryfallid:{SCRYFALL_ID.upper()}", "scryfall_id", ":", SCRYFALL_ID.upper()),
            # Validating the uuid is not the parser's business: a malformed value is an ordinary
            # string that no stored id equals.
            ("scryfallid:abc", "scryfall_id", ":", "abc"),
        ],
    )
    def test_parse_printing_id_queries(self, parse_query, query: str, column: str, operator: str, value: str) -> None:
        result = parse_query(query)

        assert isinstance(result, Query)
        binary_op = result.root
        assert isinstance(binary_op, CardBinaryOperatorNode)
        assert isinstance(binary_op.lhs, CardAttributeNode)
        assert binary_op.lhs.attribute_name == column
        assert binary_op.operator == operator
        assert binary_op.rhs.value == value

    def test_parse_negated_scryfallid_beside_another_term(self, parse_query) -> None:
        result = parse_query(f"-scryfallid:{SCRYFALL_ID} name:reset")

        assert isinstance(result.root, AndNode)
        (not_node,) = [op for op in result.root.operands if isinstance(op, NotNode)]
        assert not_node.operand.lhs.attribute_name == "scryfall_id"
        assert not_node.operand.rhs.value == SCRYFALL_ID

    def test_parse_scryfallid_alternatives(self, parse_query) -> None:
        other = "b0faa7f2-b547-42c4-a810-839da50dadfe"
        result = parse_query(f"scryfallid:{SCRYFALL_ID} or scryfallid:{other}")

        assert isinstance(result.root, OrNode)
        assert {op.rhs.value for op in result.root.operands} == {SCRYFALL_ID, other}
        assert {op.lhs.attribute_name for op in result.root.operands} == {"scryfall_id"}

    @pytest.mark.parametrize("keyword", ["scryfallid", "illustrationid"])
    def test_engine_json_names_the_column(self, parse_query, keyword: str) -> None:
        """The engine builds its id predicate off `attribute_name`, so it must be the column."""
        kwargs = parse_query(f"{keyword}:{SCRYFALL_ID}").root.to_json()["kwargs"]

        assert kwargs["lhs"]["kwargs"] == {"attribute_name": keyword.replace("id", "_id"), "original_attribute": keyword}
        assert kwargs["op"] == ":"
        assert kwargs["rhs"] == {"node_type": "StringValueNode", "kwargs": {"value": SCRYFALL_ID}}


class TestPrintingIdSQLGeneration:
    """Both keywords are an exact, case-insensitive equality on the column's text form."""

    @pytest.mark.parametrize(
        argnames=("query", "expected_value"),
        argvalues=[
            (f"scryfallid:{SCRYFALL_ID}", SCRYFALL_ID),
            (f"scryfall_id:{SCRYFALL_ID}", SCRYFALL_ID),
            (f"scryfallid={SCRYFALL_ID}", SCRYFALL_ID),
            # A uuid renders lowercase through ::text, so the search value is lowercased.
            (f"scryfallid:{SCRYFALL_ID.upper()}", SCRYFALL_ID),
            (f"scryfallid={SCRYFALL_ID.upper()}", SCRYFALL_ID),
            (f'scryfallid:"{ZERO_LED_ID}"', ZERO_LED_ID),
            ("scryfallid:not-a-uuid", "not-a-uuid"),
        ],
    )
    def test_scryfallid_generates_exact_equality_sql(self, parse_query, query: str, expected_value: str) -> None:
        context = QueryContext()
        sql = parse_query(query).to_sql(context)

        (placeholder,) = context
        # The UUID column is cast: Postgres has no `uuid = text`, and every bound parameter is text.
        assert sql == f"(card.scryfall_id::text = %({placeholder})s)"
        assert context[placeholder] == expected_value

    @pytest.mark.parametrize("operator", [":", "="])
    def test_illustrationid_is_three_valued(self, parse_query, operator: str) -> None:
        """A printing with ANOTHER artwork is NULL, and one with none is FALSE.

        `-illustrationid:<id> !"Reset"` is a 404 on Scryfall though none of Reset's printings
        carries that artwork: the negation keeps only the printings with no illustration id.
        """
        context = QueryContext()
        sql = parse_query(f"illustrationid{operator}{ILLUSTRATION_ID.upper()}").to_sql(context)

        (placeholder,) = context
        assert sql == (
            "(CASE WHEN card.illustration_id IS NULL THEN FALSE "
            f"WHEN card.illustration_id::text = %({placeholder})s THEN TRUE END)"
        )
        assert context[placeholder] == ILLUSTRATION_ID

    def test_negated_illustrationid_negates_the_three_values(self, parse_query) -> None:
        sql = parse_query(f"-illustrationid:{ILLUSTRATION_ID}").to_sql(QueryContext())

        assert sql.startswith("NOT ((CASE WHEN card.illustration_id IS NULL THEN FALSE WHEN ")

    @pytest.mark.parametrize("operator", [">", "<", ">=", "<=", "!="])
    def test_ordered_comparisons_compare_the_text_form(self, parse_query, operator: str) -> None:
        """They parse like the other string-equality columns: kept in the tree, compared as text."""
        context = QueryContext()
        sql = parse_query(f"scryfallid{operator}{SCRYFALL_ID.upper()}").to_sql(context)

        (placeholder,) = context
        assert sql == f"(card.scryfall_id::text {operator} %({placeholder})s)"
        assert context[placeholder] == SCRYFALL_ID


if __name__ == "__main__":
    pytest.main([__file__])
