"""Tests for Scryfall's external-id keywords, `usdfoil` and `stamp:`.

Measured on api.scryfall.com 2026-10-04 (khm/1 Axgard Braggart: mtgo 87321, arena 75036,
tcgplayer 230675, multiverse 503605; usg/306 Phyrexian Processor: mtgo 12345, foil 12346):
`mtgoid:87321`, `arenaid:75036`, `tcgplayerid:230675` and `multiverseid:503605` are each one card,
`mtgoid:12346` finds a printing by its FOIL id, `usdfoil>=1 e:khm` is 68 and `stamp:oval e:khm` 94.
"""

from __future__ import annotations

from functools import partial

import pytest

from api.parsing import generate_sql_query, parse_query, parse_scryfall_query
from api.parsing.card_query_nodes import CardAttributeNode, CardBinaryOperatorNode, leading_external_id
from api.parsing.nodes import NotNode, QueryContext
from api.parsing.pyparsing_based import parse_str_to_query as pyparsing_parse_str_to_query

parse_with_pyparsing = partial(parse_query, parser_fn=pyparsing_parse_str_to_query)
BOTH_PARSERS = pytest.mark.parametrize("parse", [parse_scryfall_query, parse_with_pyparsing], ids=["hand", "pyparsing"])

MTGO = "((card.raw_card_blob ->> 'mtgo_id')::bigint)"
MTGO_FOIL = "((card.raw_card_blob ->> 'mtgo_foil_id')::bigint)"
ARENA = "((card.raw_card_blob ->> 'arena_id')::bigint)"
TCGPLAYER = "((card.raw_card_blob ->> 'tcgplayer_id')::bigint)"
TCGPLAYER_ETCHED = "((card.raw_card_blob ->> 'tcgplayer_etched_id')::bigint)"
MULTIVERSE = "(card.raw_card_blob -> 'multiverse_ids')"
USD_FOIL = "((card.raw_card_blob -> 'prices' ->> 'usd_foil')::real)"
STAMP = "(card.raw_card_blob ->> 'security_stamp')"

# (keyword spelling, the attribute it resolves to)
SPELLINGS = [
    ("mtgoid", "mtgo_id"),
    ("mtgo_id", "mtgo_id"),
    ("mtgo", "mtgo_id"),
    ("arenaid", "arena_id"),
    ("arena_id", "arena_id"),
    ("arena", "arena_id"),
    ("tcgplayerid", "tcgplayer_id"),
    ("tcgplayer_id", "tcgplayer_id"),
    ("tcgplayer", "tcgplayer_id"),
    ("multiverseid", "multiverse_id"),
    ("multiverse_id", "multiverse_id"),
    ("multiverse", "multiverse_id"),
    ("stamp", "security_stamp"),
    ("usdfoil", "price_usd_foil"),
]


class TestParsing:
    """Every spelling parses to its attribute, and the engine JSON names it."""

    @BOTH_PARSERS
    @pytest.mark.parametrize(("spelling", "attribute"), SPELLINGS, ids=[s for s, _ in SPELLINGS])
    @pytest.mark.parametrize("operator", [":", "="])
    def test_each_spelling_resolves_to_its_attribute(self, parse, spelling: str, attribute: str, operator: str) -> None:
        node = parse(f"{spelling}{operator}5").root

        assert isinstance(node, CardBinaryOperatorNode)
        assert isinstance(node.lhs, CardAttributeNode)
        assert node.lhs.attribute_name == attribute
        assert node.operator == operator

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        "spelling", ["mtgofoilid", "tcg", "mvid", "cardmarketid", "eurfoil", "usdetched", "usd_foil", "tixfoil"]
    )
    def test_spellings_scryfall_does_not_have_stay_unknown(self, parse, spelling: str) -> None:
        with pytest.raises(ValueError, match="Failed to parse query"):
            parse(f"{spelling}:5")

    @BOTH_PARSERS
    def test_an_id_value_is_kept_as_text_for_the_engine(self, parse) -> None:
        """The engine reads the leading digits itself, so the value must arrive as written."""
        kwargs = parse("mtgoid:87321a").root.to_json()["kwargs"]

        assert kwargs["lhs"]["kwargs"] == {"attribute_name": "mtgo_id", "original_attribute": "mtgoid"}
        assert kwargs["op"] == ":"
        assert kwargs["rhs"] == {"node_type": "StringValueNode", "kwargs": {"value": "87321a"}}

    @BOTH_PARSERS
    def test_usdfoil_is_a_numeric_column(self, parse) -> None:
        kwargs = parse("usdfoil>=1").root.to_json()["kwargs"]

        assert kwargs["lhs"]["kwargs"] == {"attribute_name": "price_usd_foil", "original_attribute": "usdfoil"}
        assert kwargs["rhs"] == {"node_type": "NumericValueNode", "kwargs": {"value": 1}}

    @BOTH_PARSERS
    def test_negation_wraps_the_term(self, parse) -> None:
        root = parse("-stamp:oval").root

        assert isinstance(root, NotNode)
        assert root.operand.lhs.attribute_name == "security_stamp"


class TestLeadingExternalId:
    """The value an id keyword names is its leading decimal digits."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("87321", 87321),
            ("87321a", 87321),
            ("87321.0", 87321),
            ("040", 40),
            ("4294967295", 4294967295),
            # No digits, or more than the engine's u32 holds: the id no card has.
            ("", 0),
            ("abc", 0),
            ("a87321", 0),
            ("-1", 0),
            ("0", 0),
            ("4294967296", 0),
            ("99999999999", 0),
            # Only ASCII digits count, as in the engine.
            ("٣", 0),
        ],
    )
    def test_leading_digits(self, value: str, expected: int) -> None:
        assert leading_external_id(value) == expected


class TestSQLGeneration:
    """None of these is a column: each renders as an expression over raw_card_blob."""

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "columns", "expected_id"),
        [
            # Both ids of a pair, spelled exactly as the partial indexes over them.
            ("mtgoid:87321", (MTGO, MTGO_FOIL), 87321),
            ("mtgo_id=87321", (MTGO, MTGO_FOIL), 87321),
            ("mtgo:12346", (MTGO, MTGO_FOIL), 12346),
            ("arenaid:75036", (ARENA,), 75036),
            ("arena=75036", (ARENA,), 75036),
            ("tcgplayerid:230675", (TCGPLAYER, TCGPLAYER_ETCHED), 230675),
            ("tcgplayer_id:230675", (TCGPLAYER, TCGPLAYER_ETCHED), 230675),
            # The value is its leading digits...
            ("mtgoid:87321a", (MTGO, MTGO_FOIL), 87321),
            ('mtgoid:"87321"', (MTGO, MTGO_FOIL), 87321),
            # ...and a value with none compares against 0, the id no card has. It is NOT dropped:
            # the comparison is still three-valued, so its negation still depends on the row.
            ("mtgoid:abc", (MTGO, MTGO_FOIL), 0),
            ("arenaid:abc", (ARENA,), 0),
        ],
    )
    def test_id_equality_compares_every_column_of_the_keyword(
        self, parse, query: str, columns: tuple[str, ...], expected_id: int
    ) -> None:
        context = QueryContext()
        sql = parse(query).to_sql(context)

        (placeholder,) = context
        assert sql == "(" + " OR ".join(f"{column} = %({placeholder})s" for column in columns) + ")"
        assert context[placeholder] == expected_id

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "expected"), [("multiverseid:503605", "503605"), ("multiverse:503605a", "503605"), ("multiverse_id=abc", "0")]
    )
    def test_multiverseid_is_array_containment(self, parse, query: str, expected: str) -> None:
        context = QueryContext()
        sql = parse(query).to_sql(context)

        (placeholder,) = context
        assert sql == f"({MULTIVERSE} @> %({placeholder})s::jsonb)"
        assert context[placeholder] == expected

    @BOTH_PARSERS
    def test_negated_id_is_left_three_valued(self, parse) -> None:
        """`-mtgoid:87321 e:khm` matches nothing on Scryfall: no khm printing has a foil id.

        `NOT (id = x OR foil_id = x)` is NULL wherever either id is missing, which is exactly that,
        so the equality must NOT be wrapped in a COALESCE.
        """
        context = QueryContext()
        sql = parse("-mtgoid:87321").to_sql(context)

        (placeholder,) = context
        assert sql == f"NOT (({MTGO} = %({placeholder})s OR {MTGO_FOIL} = %({placeholder})s))"

    @BOTH_PARSERS
    @pytest.mark.parametrize("keyword", ["mtgoid", "arenaid", "tcgplayerid", "multiverseid", "stamp"])
    @pytest.mark.parametrize("operator", [">", "<", ">=", "<=", "!="])
    def test_a_comparison_is_false(self, parse, keyword: str, operator: str) -> None:
        """`mtgoid>5 e:khm` matches nothing and `-mtgoid>5 e:khm` is all 305: a plain FALSE."""
        assert generate_sql_query(parse(f"{keyword}{operator}5")) == ("FALSE", {})

    @BOTH_PARSERS
    @pytest.mark.parametrize("query", ["stamp:oval", "stamp=oval", "stamp:OVAL", 'stamp:"oval"'])
    def test_stamp_is_exact_case_insensitive_and_two_valued(self, parse, query: str) -> None:
        """`-stamp:oval e:khm` is 216 on Scryfall: a printing with no stamp is FALSE, not NULL."""
        context = QueryContext()
        sql = parse(query).to_sql(context)

        (placeholder,) = context
        assert sql == f"(COALESCE({STAMP}, '') = %({placeholder})s)"
        assert context[placeholder] == "oval"

    @BOTH_PARSERS
    @pytest.mark.parametrize(
        ("query", "expected_sql"),
        [
            ("usdfoil>=1", f"({USD_FOIL} >= %(p_int_MQ)s)"),
            ("usdfoil<1", f"({USD_FOIL} < %(p_int_MQ)s)"),
            ("usdfoil:1", f"({USD_FOIL} = %(p_int_MQ)s)"),
            ("usdfoil=0.25", f"({USD_FOIL} = %(p_float_MC4yNQ)s)"),
            ("-usdfoil>=1", f"NOT (({USD_FOIL} >= %(p_int_MQ)s))"),
            # Against another column, on either side: `usdfoil>usd e:khm` is 247, `usd>usdfoil e:khm` 57.
            ("usdfoil>usd", f"({USD_FOIL} > card.price_usd)"),
            ("usd>usdfoil", f"(card.price_usd > {USD_FOIL})"),
            ("usdfoil>eur", f"({USD_FOIL} > card.price_eur)"),
        ],
    )
    def test_usdfoil_compares_the_foil_price(self, parse, query: str, expected_sql: str) -> None:
        sql, _ = generate_sql_query(parse(query))

        assert sql == expected_sql


if __name__ == "__main__":
    pytest.main([__file__])
