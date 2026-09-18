"""Tests for SQL generation functionality."""

import pytest

from api import parsing
from api.parsing import QueryContext, generate_sql_query
from api.parsing.card_query_nodes import (
    _color_dict_to_mask,
    _proper_subset_masks,
    _subset_masks,
    get_colors_comparison_object,
    get_legality_comparison_object,
)


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        ("cmc=3", "(card.cmc = %(p_int_Mw)s)", {"p_int_Mw": 3}),
        ("power=3", "(card.creature_power = %(p_int_Mw)s)", {"p_int_Mw": 3}),
        ("cmc=3 power=3", "((card.cmc = %(p_int_Mw)s) AND (card.creature_power = %(p_int_Mw)s))", {"p_int_Mw": 3}),
        ("power=toughness", "(card.creature_power = card.creature_toughness)", {}),
        ("power:toughness", "(card.creature_power = card.creature_toughness)", {}),
        ("power>toughness", "(card.creature_power > card.creature_toughness)", {}),
        ("power<toughness", "(card.creature_power < card.creature_toughness)", {}),
        ("power>cmc+1", r"(card.creature_power > (card.cmc + %(p_int_MQ)s))", {"p_int_MQ": 1}),
        ("power-cmc>1", r"((card.creature_power - card.cmc) > %(p_int_MQ)s)", {"p_int_MQ": 1}),
        ("1<power-cmc", r"(%(p_int_MQ)s < (card.creature_power - card.cmc))", {"p_int_MQ": 1}),
        (
            "cmc+cmc+2<power+toughness",
            r"(((card.cmc + card.cmc) + %(p_int_Mg)s) < (card.creature_power + card.creature_toughness))",
            {"p_int_Mg": 2},
        ),
        # Test field-specific : operator behavior. A BARE name: word is COLLATED — diacritics
        # folded (#649) AND every non-alphanumeric character removed — because that is the string
        # Scryfall matches a bare word against (measured 2026-08-16: `name:ft` 1,628 against
        # `name:"ft"` 362, because "Sword of the Ages" reads as "swordoftheages").
        (
            "name:lightning",
            r"(lower(regexp_replace(card.card_name_folded, '[^[:alnum:]]', '', 'g')) LIKE %(p_str_JWxpZ2h0bmluZyU)s)",
            {"p_str_JWxpZ2h0bmluZyU": r"%lightning%"},
        ),
        # A QUOTED value is matched LITERALLY instead — against card_name, with neither fold:
        # `name:"eowyn"` answers 0 on api.scryfall.com while `name:"éowyn"` answers 3.
        (
            "name:'lightning bolt'",
            r"(lower(card.card_name) LIKE %(p_str_JWxpZ2h0bmluZyVib2x0JQ)s)",
            {"p_str_JWxpZ2h0bmluZyVib2x0JQ": r"%lightning%bolt%"},
        ),
        # An unaccented bare word still finds the accented card...
        (
            "name:eowyn",
            r"(lower(regexp_replace(card.card_name_folded, '[^[:alnum:]]', '', 'g')) LIKE %(p_str_JWVvd3luJQ)s)",
            {"p_str_JWVvd3luJQ": r"%eowyn%"},
        ),
        # ...and so does the bare accented spelling (both parsers' tokenizers accept non-ASCII
        # bare words, #649), folding to the identical SQL and parameters.
        (
            "name:éowyn",
            r"(lower(regexp_replace(card.card_name_folded, '[^[:alnum:]]', '', 'g')) LIKE %(p_str_JWVvd3luJQ)s)",
            {"p_str_JWVvd3luJQ": r"%eowyn%"},
        ),
        # Quoted, the accent is required and kept.
        (
            'name:"éowyn"',
            r"(lower(card.card_name) LIKE %(p_str_JcOpb3d5biU)s)",
            {"p_str_JcOpb3d5biU": r"%éowyn%"},
        ),
        # Exact match (!"...") is COLLATED too: !"eowyn, lady of rohan" answers "Éowyn, Lady of
        # Rohan" on api.scryfall.com, and !"limduls vault" answers Lim-Dûl's Vault.
        (
            '!"Éowyn"',
            r"(lower(regexp_replace(card.card_name_folded, '[^[:alnum:]]', '', 'g')) LIKE %(p_str_ZW93eW4)s)",
            {"p_str_ZW93eW4": "eowyn"},
        ),
        ("cmc:3", "(card.cmc = %(p_int_Mw)s)", {"p_int_Mw": 3}),  # Numeric field uses exact equality
        ("power:5", "(card.creature_power = %(p_int_NQ)s)", {"p_int_NQ": 5}),  # Numeric field uses exact equality
        # loyalty tests
        ("loyalty=3", "(card.planeswalker_loyalty = %(p_int_Mw)s)", {"p_int_Mw": 3}),
        ("loyalty>5", "(card.planeswalker_loyalty > %(p_int_NQ)s)", {"p_int_NQ": 5}),
        ("loyalty<=7", "(card.planeswalker_loyalty <= %(p_int_Nw)s)", {"p_int_Nw": 7}),
        ("loy:4", "(card.planeswalker_loyalty = %(p_int_NA)s)", {"p_int_NA": 4}),
        # color
        ("color:g", "(card.card_colors @> %(p_dict_eydHJzogVHJ1ZX0)s)", {"p_dict_eydHJzogVHJ1ZX0": {"G": True}}),  # >=
        ("color=g", "(card.card_colors = %(p_dict_eydHJzogVHJ1ZX0)s)", {"p_dict_eydHJzogVHJ1ZX0": {"G": True}}),  # =
        ("color<=g", "(card.card_colors <@ %(p_dict_eydHJzogVHJ1ZX0)s)", {"p_dict_eydHJzogVHJ1ZX0": {"G": True}}),  # <=
        ("color>=g", "(card.card_colors @> %(p_dict_eydHJzogVHJ1ZX0)s)", {"p_dict_eydHJzogVHJ1ZX0": {"G": True}}),  # >=
        (
            "color>g",
            "(card.card_colors @> %(p_dict_eydHJzogVHJ1ZX0)s AND card.card_colors <> %(p_dict_eydHJzogVHJ1ZX0)s)",
            {"p_dict_eydHJzogVHJ1ZX0": {"G": True}},
        ),  # >
        (
            "color<g",
            "(card.card_colors <@ %(p_dict_eydHJzogVHJ1ZX0)s AND card.card_colors <> %(p_dict_eydHJzogVHJ1ZX0)s)",
            {"p_dict_eydHJzogVHJ1ZX0": {"G": True}},
        ),  # <
    ],
)
def test_full_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql
    assert context == expected_parameters


# @pytest.mark.xfail(reason="JSONB queries are not supported yet")
@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        (
            "colors:red",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZX0)s)",
            {"p_dict_eydSJzogVHJ1ZX0": {"R": True}},
        ),  # JSONB object uses containment
        (
            "colors:rg",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),  # JSONB object uses containment
        # test exact equality of colors
        (
            "colors=rg",
            r"(card.card_colors = %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),
        # test colors greater than
        (
            "colors>=rg",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),
        # test colors less than
        (
            "colors<=rg",
            r"(card.card_colors <@ %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),
        # test colors strictly greater than
        (
            "colors>rg",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s AND card.card_colors <> %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),
        # test colors strictly less than
        (
            "colors<rg",
            r"(card.card_colors <@ %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s AND card.card_colors <> %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),
        # devotion tests
        (
            "devotion:{G}",
            r"(card.devotion @> %(p_dict_eydHJzogWzFdfQ)s)",
            {"p_dict_eydHJzogWzFdfQ": {"G": [1]}},
        ),
        (
            "devotion>={G}",
            r"(card.devotion @> %(p_dict_eydHJzogWzFdfQ)s)",
            {"p_dict_eydHJzogWzFdfQ": {"G": [1]}},
        ),
        (
            "devotion>={G}{R}",
            r"(card.devotion @> %(p_dict_eydSJzogWzFdLCAnRyc6IFsxXX0)s)",
            {"p_dict_eydSJzogWzFdLCAnRyc6IFsxXX0": {"G": [1], "R": [1]}},
        ),
        # mana: tests — unlike devotion, every op ANDs in a cmc compare
        # alongside jsonb containment/equality (see
        # _handle_mana_cost_approximate_comparison), and ":" normalizes to ">=".
        #
        # Every op also leads with `card.mana_cost_text <> ''`: NO PRINTED COST IS NOT A COST OF
        # {0}. A land and Ornithopter both store `mana_cost_jsonb = '{}'`, so without that clause
        # `m:{0}` is every row in the table. Measured on api.scryfall.com 2026-08-17 at
        # unique=prints, `m:{0} t:land` is 195 and `m:{0}` is 93,355 of 105,839.
        (
            "mana:{g}",
            r"(card.mana_cost_text <> '' AND %(p_dict_eydHJzogWzFdfQ)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_MQ)s)",
            {"p_dict_eydHJzogWzFdfQ": {"G": [1]}, "p_int_MQ": 1},
        ),
        (
            "mana={g}",
            r"(card.mana_cost_text <> '' AND card.mana_cost_jsonb = %(p_dict_eydHJzogWzFdfQ)s AND card.cmc = %(p_int_MQ)s)",
            {"p_dict_eydHJzogWzFdfQ": {"G": [1]}, "p_int_MQ": 1},
        ),
        (
            "mana<={g}",
            r"(card.mana_cost_text <> '' AND card.mana_cost_jsonb <@ %(p_dict_eydHJzogWzFdfQ)s AND card.cmc <= %(p_int_MQ)s)",
            {"p_dict_eydHJzogWzFdfQ": {"G": [1]}, "p_int_MQ": 1},
        ),
        (
            "mana<{g}",
            r"(card.mana_cost_text <> '' AND card.mana_cost_jsonb <@ %(p_dict_eydHJzogWzFdfQ)s AND card.cmc <= %(p_int_MQ)s AND card.mana_cost_jsonb <> %(p_dict_eydHJzogWzFdfQ)s)",
            {"p_dict_eydHJzogWzFdfQ": {"G": [1]}, "p_int_MQ": 1},
        ),
        (
            "mana>={g}{r}",
            r"(card.mana_cost_text <> '' AND %(p_dict_eydHJzogWzFdLCAnUic6IFsxXX0)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_Mg)s)",
            {"p_dict_eydHJzogWzFdLCAnUic6IFsxXX0": {"G": [1], "R": [1]}, "p_int_Mg": 2},
        ),
        (
            "mana>{g}",
            r"(card.mana_cost_text <> '' AND %(p_dict_eydHJzogWzFdfQ)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_MQ)s AND card.mana_cost_jsonb <> %(p_dict_eydHJzogWzFdfQ)s)",
            {"p_dict_eydHJzogWzFdfQ": {"G": [1]}, "p_int_MQ": 1},
        ),
        # X is its own pip symbol, not a hybrid, and contributes 0 to cmc
        # (mana_cost_jsonb keeps the X key; cmc excludes it) — braced and bare
        # forms must produce identical SQL, since mana_cost_str_to_dict()
        # treats them the same.
        (
            "mana:{X}{R}",
            r"(card.mana_cost_text <> '' AND %(p_dict_eydYJzogWzFdLCAnUic6IFsxXX0)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_MQ)s)",
            {"p_dict_eydYJzogWzFdLCAnUic6IFsxXX0": {"X": [1], "R": [1]}, "p_int_MQ": 1},
        ),
        (
            "mana:xr",
            r"(card.mana_cost_text <> '' AND %(p_dict_eydYJzogWzFdLCAnUic6IFsxXX0)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_MQ)s)",
            {"p_dict_eydYJzogWzFdLCAnUic6IFsxXX0": {"X": [1], "R": [1]}, "p_int_MQ": 1},
        ),
        # Snow, likewise: bare 's' means the same thing as braced '{s}' (#954).
        (
            "mana:{S}",
            r"(card.mana_cost_text <> '' AND %(p_dict_eydTJzogWzFdfQ)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_MQ)s)",
            {"p_dict_eydTJzogWzFdfQ": {"S": [1]}, "p_int_MQ": 1},
        ),
        (
            "mana:s",
            r"(card.mana_cost_text <> '' AND %(p_dict_eydTJzogWzFdfQ)s <@ card.mana_cost_jsonb AND card.cmc >= %(p_int_MQ)s)",
            {"p_dict_eydTJzogWzFdfQ": {"S": [1]}, "p_int_MQ": 1},
        ),
    ],
)
def test_full_sql_translation_jsonb_colors(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    parsed = parse_query(input_query)
    observed_params = QueryContext()
    observed_sql = parsed.to_sql(observed_params)
    assert (observed_sql, observed_params) == (
        expected_sql,
        expected_parameters,
    ), f"\nExpected: {expected_sql}\t{expected_parameters}\nObserved: {observed_sql}\t{observed_params}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        (
            "color_identity:g",
            r"(magic.color_identity_mask(card.card_color_identity) = ANY(%(p_IntArray_WzAsIDFd)s::smallint[]))",
            {"p_IntArray_WzAsIDFd": [0, 1]},
        ),  # : uses bitmask subset lookup; G=1, subsets of 1 are [0,1]
        (
            "id:rg",
            r"(magic.color_identity_mask(card.card_color_identity) = ANY(%(p_IntArray_WzAsIDEsIDIsIDNd)s::smallint[]))",
            {"p_IntArray_WzAsIDEsIDIsIDNd": [0, 1, 2, 3]},
        ),  # RG mask=3, subsets=[0,1,2,3]
        (
            "identity=rg",
            r"(card.card_color_identity = %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),  # = still means JSONB equality
        (
            "coloridentity>=rg",
            r"(card.card_color_identity @> %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),  # >= uses @> against JSONB column (GIN index)
        (
            "color_identity<=rg",
            r"(magic.color_identity_mask(card.card_color_identity) = ANY(%(p_IntArray_WzAsIDEsIDIsIDNd)s::smallint[]))",
            {"p_IntArray_WzAsIDEsIDIsIDNd": [0, 1, 2, 3]},
        ),  # <= uses bitmask subset lookup same as :
        (
            "identity>g",
            r"(card.card_color_identity @> %(p_dict_eydHJzogVHJ1ZX0)s AND card.card_color_identity <> %(p_dict_eydHJzogVHJ1ZX0)s)",
            {"p_dict_eydHJzogVHJ1ZX0": {"G": True}},
        ),  # > uses @> against JSONB column (GIN index)
        (
            "id<rg",
            r"(magic.color_identity_mask(card.card_color_identity) = ANY(%(p_IntArray_WzAsIDEsIDJd)s::smallint[]))",
            {"p_IntArray_WzAsIDEsIDJd": [0, 1, 2]},
        ),  # < uses proper subsets; RG mask=3, proper subsets=[0,1,2]
        (
            "id=c",
            r"(card.card_color_identity = %(p_dict_e30)s)",
            {"p_dict_e30": {}},
        ),  # colorless identity = {} (empty), not {"C": True}
        (
            "id:c",
            r"(magic.color_identity_mask(card.card_color_identity) = ANY(%(p_IntArray_WzBd)s::smallint[]))",
            {"p_IntArray_WzBd": [0]},
        ),  # colorless mask=0, subsets=[0] — only colorless cards
        (
            "id:colorless",
            r"(magic.color_identity_mask(card.card_color_identity) = ANY(%(p_IntArray_WzBd)s::smallint[]))",
            {"p_IntArray_WzBd": [0]},
        ),  # 'colorless' name resolves to same mask=0
        (
            "id=colorless",
            r"(card.card_color_identity = %(p_dict_e30)s)",
            {"p_dict_e30": {}},
        ),  # equality with colorless name
    ],
)
def test_color_identity_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    parsed = parse_query(input_query)
    observed_params = QueryContext()
    observed_sql = parsed.to_sql(observed_params)
    assert (observed_sql, observed_params) == (
        expected_sql,
        expected_parameters,
    ), f"\nExpected: {expected_sql}\t{expected_parameters}\nObserved: {observed_sql}\t{observed_params}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        (
            "color=rg",
            r"(card.card_colors = %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),
        (
            "color:rg",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ)s)",
            {"p_dict_eydSJzogVHJ1ZSwgJ0cnOiBUcnVlfQ": {"R": True, "G": True}},
        ),
        # Regression for #668: ":"/">=" (Ge) against jsonb containment (@>) is
        # vacuously true for an empty rhs, so colorless must fall back to "="
        # instead -- card_colors/card_color_identity store colorless as {}
        # (verified against the live Scryfall API: Sol Ring's colors is []).
        (
            "color:c",
            r"(card.card_colors = %(p_dict_e30)s)",
            {"p_dict_e30": {}},
        ),
        (
            "c:c",
            r"(card.card_colors = %(p_dict_e30)s)",
            {"p_dict_e30": {}},
        ),
        (
            "color=c",
            r"(card.card_colors = %(p_dict_e30)s)",
            {"p_dict_e30": {}},
        ),
        # produced_mana is different: Scryfall's produced_mana array can
        # contain "C" as a genuine value (Sol Ring's produced_mana is ["C"],
        # not []), so "colorless" here is an ordinary containment query
        # against {"C": True}, not an empty-set special case.
        (
            "produces:c",
            r"(card.produced_mana @> %(p_dict_eydDJzogVHJ1ZX0)s)",
            {"p_dict_eydDJzogVHJ1ZX0": {"C": True}},
        ),
        (
            "produces=c",
            r"(card.produced_mana = %(p_dict_eydDJzogVHJ1ZX0)s)",
            {"p_dict_eydDJzogVHJ1ZX0": {"C": True}},
        ),
        (
            "produces:colorless",
            r"(card.produced_mana @> %(p_dict_eydDJzogVHJ1ZX0)s)",
            {"p_dict_eydDJzogVHJ1ZX0": {"C": True}},
        ),
    ],
)
def test_color_and_produces_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    parsed = parse_query(input_query)
    observed_params = QueryContext()
    observed_sql = parsed.to_sql(observed_params)
    assert (observed_sql, observed_params) == (
        expected_sql,
        expected_parameters,
    ), f"\nExpected: {expected_sql}\t{expected_parameters}\nObserved: {observed_sql}\t{observed_params}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        (
            "type:creature",
            r"(%(p_list_WydDcmVhdHVyZSdd)s <@ card.card_types)",
            {"p_list_WydDcmVhdHVyZSdd": ["Creature"]},
        ),
        (
            "t:elf t:archer",
            r"((%(p_list_WydFbGYnXQ)s <@ card.card_subtypes) AND (%(p_list_WydBcmNoZXInXQ)s <@ card.card_subtypes))",
            {"p_list_WydFbGYnXQ": ["Elf"], "p_list_WydBcmNoZXInXQ": ["Archer"]},
        ),
    ],
)
def test_full_sql_translation_jsonb_card_types(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    parsed = parse_query(input_query)
    observed_params = QueryContext()
    observed_sql = parsed.to_sql(observed_params)
    assert (observed_sql, observed_params) == (
        expected_sql,
        expected_parameters,
    ), f"\nExpected: {expected_sql}\t{expected_parameters}\nObserved: {observed_sql}\t{observed_params}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Oracle text search tests
        ("oracle:flying", "(lower(card.oracle_text) LIKE %(p_str_JWZseWluZyU)s)", {"p_str_JWZseWluZyU": "%flying%"}),
        (
            "oracle:'gain life'",
            "(lower(card.oracle_text) LIKE %(p_str_JWdhaW4lbGlmZSU)s)",
            {"p_str_JWdhaW4lbGlmZSU": "%gain%life%"},
        ),
        (
            'oracle:"gain life"',
            "(lower(card.oracle_text) LIKE %(p_str_JWdhaW4lbGlmZSU)s)",
            {"p_str_JWdhaW4lbGlmZSU": "%gain%life%"},
        ),
        ("oracle:haste", "(lower(card.oracle_text) LIKE %(p_str_JWhhc3RlJQ)s)", {"p_str_JWhhc3RlJQ": "%haste%"}),
        # Test oracle search with complex phrases
        (
            "oracle:'tap target creature'",
            "(lower(card.oracle_text) LIKE %(p_str_JXRhcCV0YXJnZXQlY3JlYXR1cmUl)s)",
            {"p_str_JXRhcCV0YXJnZXQlY3JlYXR1cmUl": "%tap%target%creature%"},
        ),
    ],
)
def test_oracle_text_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that oracle text search generates correct SQL with LIKE patterns."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql
    assert context == expected_parameters


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Flavor text search tests
        ("flavor:exile", "(lower(card.flavor_text) LIKE %(p_str_JWV4aWxlJQ)s)", {"p_str_JWV4aWxlJQ": "%exile%"}),
        (
            "flavor:'ancient power'",
            "(lower(card.flavor_text) LIKE %(p_str_JWFuY2llbnQlcG93ZXIl)s)",
            {"p_str_JWFuY2llbnQlcG93ZXIl": "%ancient%power%"},
        ),
        (
            'flavor:"ancient power"',
            "(lower(card.flavor_text) LIKE %(p_str_JWFuY2llbnQlcG93ZXIl)s)",
            {"p_str_JWFuY2llbnQlcG93ZXIl": "%ancient%power%"},
        ),
        ("flavor:magic", "(lower(card.flavor_text) LIKE %(p_str_JW1hZ2ljJQ)s)", {"p_str_JW1hZ2ljJQ": "%magic%"}),
        # Test flavor search with complex phrases
        (
            "flavor:'power of darkness'",
            "(lower(card.flavor_text) LIKE %(p_str_JXBvd2VyJW9mJWRhcmtuZXNzJQ)s)",
            {"p_str_JXBvd2VyJW9mJWRhcmtuZXNzJQ": "%power%of%darkness%"},
        ),
    ],
)
def test_flavor_text_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that flavor text search generates correct SQL with LIKE patterns."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql
    assert context == expected_parameters


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Basic keyword search
        (
            "keyword:flying",
            r"(card.card_keywords @> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        # Keyword search with colon operator (should behave like @>)
        (
            "keyword:trample",
            r"(card.card_keywords @> %(p_dict_eyd0cmFtcGxlJzogVHJ1ZX0)s)",
            {"p_dict_eyd0cmFtcGxlJzogVHJ1ZX0": {"trample": True}},
        ),
        # Keyword search (updated from alias 'k')
        (
            "keyword:haste",
            r"(card.card_keywords @> %(p_dict_eydoYXN0ZSc6IFRydWV9)s)",
            {"p_dict_eydoYXN0ZSc6IFRydWV9": {"haste": True}},
        ),
        # `keyword=` is `keyword:`, not set equality -- `kw=flying e:khm` is 28 on
        # api.scryfall.com (2026-08-16), exactly `kw:flying`'s 28, where set equality answers only
        # the cards whose whole keyword list is that one word.
        (
            "keyword=vigilance",
            r"(card.card_keywords @> %(p_dict_eyd2aWdpbGFuY2UnOiBUcnVlfQ)s)",
            {"p_dict_eyd2aWdpbGFuY2UnOiBUcnVlfQ": {"vigilance": True}},
        ),
        # Custom keyword (not in the predefined list)
        (
            "keyword:customability",
            r"(card.card_keywords @> %(p_dict_eydjdXN0b21hYmlsaXR5JzogVHJ1ZX0)s)",
            {"p_dict_eydjdXN0b21hYmlsaXR5JzogVHJ1ZX0": {"customability": True}},
        ),
        # Test different operators
        (
            "keyword>=flying",
            r"(card.card_keywords @> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        (
            "keyword<=haste",
            r"(card.card_keywords <@ %(p_dict_eydoYXN0ZSc6IFRydWV9)s)",
            {"p_dict_eydoYXN0ZSc6IFRydWV9": {"haste": True}},
        ),
        (
            "keyword>trample",
            r"(card.card_keywords @> %(p_dict_eyd0cmFtcGxlJzogVHJ1ZX0)s AND card.card_keywords <> %(p_dict_eyd0cmFtcGxlJzogVHJ1ZX0)s)",
            {"p_dict_eyd0cmFtcGxlJzogVHJ1ZX0": {"trample": True}},
        ),
        (
            "keyword<vigilance",
            r"(card.card_keywords <@ %(p_dict_eyd2aWdpbGFuY2UnOiBUcnVlfQ)s AND card.card_keywords <> %(p_dict_eyd2aWdpbGFuY2UnOiBUcnVlfQ)s)",
            {"p_dict_eyd2aWdpbGFuY2UnOiBUcnVlfQ": {"vigilance": True}},
        ),
        (
            "keyword!=flying",
            r"(card.card_keywords <> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        # kw: shorthand for keyword:
        (
            "kw:flying",
            r"(card.card_keywords @> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        (
            "kw:trample",
            r"(card.card_keywords @> %(p_dict_eyd0cmFtcGxlJzogVHJ1ZX0)s)",
            {"p_dict_eyd0cmFtcGxlJzogVHJ1ZX0": {"trample": True}},
        ),
        (
            "kw=haste",
            r"(card.card_keywords @> %(p_dict_eydoYXN0ZSc6IFRydWV9)s)",
            {"p_dict_eydoYXN0ZSc6IFRydWV9": {"haste": True}},
        ),
        # Multi-word keywords lowercase whole, whatever the caller typed -- `.title()` used to look
        # these up as `First Strike`, which is not how Scryfall spells them, so they matched nothing.
        (
            'keyword:"first strike"',
            r"(card.card_keywords @> %(p_dict_eydmaXJzdCBzdHJpa2UnOiBUcnVlfQ)s)",
            {"p_dict_eydmaXJzdCBzdHJpa2UnOiBUcnVlfQ": {"first strike": True}},
        ),
        (
            'keyword:"FIRST STRIKE"',
            r"(card.card_keywords @> %(p_dict_eydmaXJzdCBzdHJpa2UnOiBUcnVlfQ)s)",
            {"p_dict_eydmaXJzdCBzdHJpa2UnOiBUcnVlfQ": {"first strike": True}},
        ),
        # Apostrophes survive: `.title()` turned this into `Doctor'S Companion`.
        (
            'keyword:"Doctor\'s companion"',
            "(card.card_keywords @> %(p_dict_eyJkb2N0b3IncyBjb21wYW5pb24iOiBUcnVlfQ)s)",
            {"p_dict_eyJkb2N0b3IncyBjb21wYW5pb24iOiBUcnVlfQ": {"doctor's companion": True}},
        ),
    ],
)
def test_keyword_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that keyword search generates correct SQL with JSONB operators."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert context == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {context}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Basic oracle tag search (should be lowercase)
        (
            "otag:flying",
            r"(card.card_oracle_tags @> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        # Oracle tag search with hyphenated term - this currently fails but should work
        (
            "otag:dual-land",
            r"(card.card_oracle_tags @> %(p_dict_eydkdWFsLWxhbmQnOiBUcnVlfQ)s)",
            {"p_dict_eydkdWFsLWxhbmQnOiBUcnVlfQ": {"dual-land": True}},
        ),
        # Oracle tag with quoted hyphenated term should also work (and currently does)
        (
            'otag:"dual-land"',
            r"(card.card_oracle_tags @> %(p_dict_eydkdWFsLWxhbmQnOiBUcnVlfQ)s)",
            {"p_dict_eydkdWFsLWxhbmQnOiBUcnVlfQ": {"dual-land": True}},
        ),
        # Oracle tag with alias 'otag'
        (
            "otag:haste",
            r"(card.card_oracle_tags @> %(p_dict_eydoYXN0ZSc6IFRydWV9)s)",
            {"p_dict_eydoYXN0ZSc6IFRydWV9": {"haste": True}},
        ),
        # Oracle tag with numeric prefix like "40k-model" - issue #110
        (
            "otag:40k-model",
            r"(card.card_oracle_tags @> %(p_dict_eyc0MGstbW9kZWwnOiBUcnVlfQ)s)",
            {"p_dict_eyc0MGstbW9kZWwnOiBUcnVlfQ": {"40k-model": True}},
        ),
        # Oracle tag with complex hyphenated value containing digits
        (
            "otag:cycle-shm-common-hybrid-1-drop",
            r"(card.card_oracle_tags @> %(p_dict_eydjeWNsZS1zaG0tY29tbW9uLWh5YnJpZC0xLWRyb3AnOiBUcnVlfQ)s)",
            {"p_dict_eydjeWNsZS1zaG0tY29tbW9uLWh5YnJpZC0xLWRyb3AnOiBUcnVlfQ": {"cycle-shm-common-hybrid-1-drop": True}},
        ),
    ],
)
def test_oracle_tag_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that oracle tag search generates correct SQL with lowercase tags."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert context == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {context}"


@pytest.mark.parametrize(
    argnames=["input_query", "expected_sql", "expected_parameters"],
    argvalues=[
        (
            "art:wolf",
            r"(card.card_art_tags @> %(p_dict_eyd3b2xmJzogVHJ1ZX0)s)",
            {"p_dict_eyd3b2xmJzogVHJ1ZX0": {"wolf": True}},
        ),
        (
            "art_tags:wolf",
            r"(card.card_art_tags @> %(p_dict_eyd3b2xmJzogVHJ1ZX0)s)",
            {"p_dict_eyd3b2xmJzogVHJ1ZX0": {"wolf": True}},
        ),
        (
            "art:cycle-abu-dual-land",
            r"(card.card_art_tags @> %(p_dict_eydjeWNsZS1hYnUtZHVhbC1sYW5kJzogVHJ1ZX0)s)",
            {"p_dict_eydjeWNsZS1hYnUtZHVhbC1sYW5kJzogVHJ1ZX0": {"cycle-abu-dual-land": True}},
        ),
    ],
)
def test_art_tag_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that art tag search generates correct SQL with lowercase tags."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert context == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {context}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Basic is: tag search (should be lowercase)
        (
            "is:creature",
            r"(card.card_is_tags @> %(p_dict_eydjcmVhdHVyZSc6IFRydWV9)s)",
            {"p_dict_eydjcmVhdHVyZSc6IFRydWV9": {"creature": True}},
        ),
        # is: tag search with hyphenated term
        (
            "is:modal-dfc",
            r"(card.card_is_tags @> %(p_dict_eydtb2RhbC1kZmMnOiBUcnVlfQ)s)",
            {"p_dict_eydtb2RhbC1kZmMnOiBUcnVlfQ": {"modal-dfc": True}},
        ),
        # is: tag with quoted hyphenated term
        (
            'is:"modal-dfc"',
            r"(card.card_is_tags @> %(p_dict_eydtb2RhbC1kZmMnOiBUcnVlfQ)s)",
            {"p_dict_eydtb2RhbC1kZmMnOiBUcnVlfQ": {"modal-dfc": True}},
        ),
        # Generic is: fallback for tags with no rewrite (is:spell is deferred, so still
        # lowers to a raw card_is_tags lookup). Rewritten tags (is:permanent, is:party,
        # the layout family, …) are covered by test_rewrite.py.
        (
            "is:spell",
            r"(card.card_is_tags @> %(p_dict_eydzcGVsbCc6IFRydWV9)s)",
            {"p_dict_eydzcGVsbCc6IFRydWV9": {"spell": True}},
        ),
    ],
)
def test_is_tag_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that is: tag search generates correct SQL with lowercase tags."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert context == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {context}"


@pytest.mark.parametrize(
    argnames="tag_value",
    argvalues=["creature", "modal-dfc", "spell"],
)
def test_has_is_alias(parse_query, tag_value: str) -> None:
    """Test that has: is a full synonym for is:.

    Scryfall treats has: as a full synonym for is:, for every tag value -- not just the
    handful of "does this card have X" examples its docs happen to show (has:indicator,
    has:watermark). Confirmed live against api.scryfall.com: is:X and has:X return identical
    card counts for every value tried.
    """
    is_context = QueryContext()
    has_context = QueryContext()
    assert parse_query(f"is:{tag_value}").to_sql(is_context) == parse_query(f"has:{tag_value}").to_sql(has_context)
    assert is_context == has_context


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Case-insensitive oracle tag search
        (
            "Otag:flying",
            r"(card.card_oracle_tags @> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        (
            "OTAG:flying",
            r"(card.card_oracle_tags @> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        (
            "oTaG:flying",
            r"(card.card_oracle_tags @> %(p_dict_eydmbHlpbmcnOiBUcnVlfQ)s)",
            {"p_dict_eydmbHlpbmcnOiBUcnVlfQ": {"flying": True}},
        ),
        # Case-insensitive color attribute search
        (
            "Color:red",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZX0)s)",
            {"p_dict_eydSJzogVHJ1ZX0": {"R": True}},
        ),
        (
            "COLOR:red",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZX0)s)",
            {"p_dict_eydSJzogVHJ1ZX0": {"R": True}},
        ),
        # Case-insensitive single-letter alias
        (
            "C:red",
            r"(card.card_colors @> %(p_dict_eydSJzogVHJ1ZX0)s)",
            {"p_dict_eydSJzogVHJ1ZX0": {"R": True}},
        ),
        # Case-insensitive type attribute search
        (
            "Type:creature",
            r"(%(p_list_WydDcmVhdHVyZSdd)s <@ card.card_types)",
            {"p_list_WydDcmVhdHVyZSdd": ["Creature"]},
        ),
        (
            "TYPE:creature",
            r"(%(p_list_WydDcmVhdHVyZSdd)s <@ card.card_types)",
            {"p_list_WydDcmVhdHVyZSdd": ["Creature"]},
        ),
        # Case-insensitive alias 't'
        (
            "T:creature",
            r"(%(p_list_WydDcmVhdHVyZSdd)s <@ card.card_types)",
            {"p_list_WydDcmVhdHVyZSdd": ["Creature"]},
        ),
    ],
)
def test_case_insensitive_attributes(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that attribute names are case-insensitive."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert context == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {context}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Basic set search with full 'set:' syntax
        (
            "set:iko",
            r"(card.card_set_code = %(p_str_aWtv)s)",
            {"p_str_aWtv": "iko"},
        ),
        # Set search with 's:' shorthand
        (
            "s:iko",
            r"(card.card_set_code = %(p_str_aWtv)s)",
            {"p_str_aWtv": "iko"},
        ),
        # Set search with 'e:' shorthand (Scryfall-compatible)
        (
            "e:iko",
            r"(card.card_set_code = %(p_str_aWtv)s)",
            {"p_str_aWtv": "iko"},
        ),
        # Case-insensitive set attribute search
        (
            "SET:iko",
            r"(card.card_set_code = %(p_str_aWtv)s)",
            {"p_str_aWtv": "iko"},
        ),
        # Set search with different set codes
        (
            "set:thb",
            r"(card.card_set_code = %(p_str_dGhi)s)",
            {"p_str_dGhi": "thb"},
        ),
        # Set search with multiple characters
        (
            "s:m21",
            r"(card.card_set_code = %(p_str_bTIx)s)",
            {"p_str_bTIx": "m21"},
        ),
        # test capitalization handling
        (
            "set=BLB",
            r"(card.card_set_code = %(p_str_Ymxi)s)",
            {"p_str_Ymxi": "blb"},
        ),
        (
            "s=BLB",
            r"(card.card_set_code = %(p_str_Ymxi)s)",
            {"p_str_Ymxi": "blb"},
        ),
    ],
)
def test_set_search_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that set searches generate correct SQL."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert context == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {context}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Basic rarity equality searches
        (
            "rarity:common",
            "(card.card_rarity_int = %(p_int_MA)s)",
            {"p_int_MA": 0},
        ),
        (
            "rarity:uncommon",
            "(card.card_rarity_int = %(p_int_MQ)s)",
            {"p_int_MQ": 1},
        ),
        (
            "rarity:rare",
            "(card.card_rarity_int = %(p_int_Mg)s)",
            {"p_int_Mg": 2},
        ),
        (
            "rarity:mythic",
            "(card.card_rarity_int = %(p_int_NA)s)",
            {"p_int_NA": 4},
        ),
        (
            "rarity:special",
            "(card.card_rarity_int = %(p_int_Mw)s)",
            {"p_int_Mw": 3},
        ),
        (
            "rarity:bonus",
            "(card.card_rarity_int = %(p_int_NQ)s)",
            {"p_int_NQ": 5},
        ),
        # Short alias tests (r:common, r:mythic use full names)
        (
            "r:common",
            "(card.card_rarity_int = %(p_int_MA)s)",
            {"p_int_MA": 0},
        ),
        (
            "r:mythic",
            "(card.card_rarity_int = %(p_int_NA)s)",
            {"p_int_NA": 4},
        ),
        # Short form rarity values (single-letter abbreviations)
        (
            "r:c",
            "(card.card_rarity_int = %(p_int_MA)s)",
            {"p_int_MA": 0},
        ),
        (
            "rarity:c",
            "(card.card_rarity_int = %(p_int_MA)s)",
            {"p_int_MA": 0},
        ),
        (
            "r:u",
            "(card.card_rarity_int = %(p_int_MQ)s)",
            {"p_int_MQ": 1},
        ),
        (
            "rarity:u",
            "(card.card_rarity_int = %(p_int_MQ)s)",
            {"p_int_MQ": 1},
        ),
        (
            "r:r",
            "(card.card_rarity_int = %(p_int_Mg)s)",
            {"p_int_Mg": 2},
        ),
        (
            "rarity:r",
            "(card.card_rarity_int = %(p_int_Mg)s)",
            {"p_int_Mg": 2},
        ),
        (
            "r:m",
            "(card.card_rarity_int = %(p_int_NA)s)",
            {"p_int_NA": 4},
        ),
        (
            "rarity:m",
            "(card.card_rarity_int = %(p_int_NA)s)",
            {"p_int_NA": 4},
        ),
        (
            "r:s",
            "(card.card_rarity_int = %(p_int_Mw)s)",
            {"p_int_Mw": 3},
        ),
        (
            "rarity:s",
            "(card.card_rarity_int = %(p_int_Mw)s)",
            {"p_int_Mw": 3},
        ),
        (
            "r:b",
            "(card.card_rarity_int = %(p_int_NQ)s)",
            {"p_int_NQ": 5},
        ),
        (
            "rarity:b",
            "(card.card_rarity_int = %(p_int_NQ)s)",
            {"p_int_NQ": 5},
        ),
        # Comparison operators - greater than
        (
            "rarity>common",
            "(card.card_rarity_int > %(p_int_MA)s)",
            {"p_int_MA": 0},
        ),
        (
            "rarity>uncommon",
            "(card.card_rarity_int > %(p_int_MQ)s)",
            {"p_int_MQ": 1},
        ),
        # Comparison operators - greater than or equal
        (
            "rarity>=rare",
            "(card.card_rarity_int >= %(p_int_Mg)s)",
            {"p_int_Mg": 2},
        ),
        # Comparison operators - less than
        (
            "rarity<rare",
            "(card.card_rarity_int < %(p_int_Mg)s)",
            {"p_int_Mg": 2},
        ),
        # Comparison operators - less than or equal
        (
            "rarity<=uncommon",
            "(card.card_rarity_int <= %(p_int_MQ)s)",
            {"p_int_MQ": 1},
        ),
        # Comparison operators - not equal
        (
            "rarity!=common",
            "(card.card_rarity_int != %(p_int_MA)s)",
            {"p_int_MA": 0},
        ),
        # Short alias with comparison
        (
            "r>common",
            "(card.card_rarity_int > %(p_int_MA)s)",
            {"p_int_MA": 0},
        ),
    ],
)
def test_rarity_search_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that rarity search generates correct SQL with proper ordering."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert context == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {context}"


def test_rarity_invalid_values(parse_query) -> None:
    """Test that invalid rarity values raise appropriate errors."""
    # This should parse successfully but fail during SQL generation

    parsed = parse_query("rarity>invalid")

    # Should raise ValueError when generating SQL due to invalid rarity
    with pytest.raises(ValueError, match="Invalid rarity in comparison"):
        generate_sql_query(parsed)

    # Test with another invalid rarity
    parsed2 = parse_query("r<unknown")

    with pytest.raises(ValueError, match="Invalid rarity in comparison"):
        generate_sql_query(parsed2)


def test_rarity_case_insensitive(parse_query) -> None:
    """Test that rarity values are case-insensitive."""
    # Test different cases for equality
    queries = ["rarity:Common", "rarity:RARE", "r:Mythic", "rarity:UnComMoN"]

    for query_str in queries:
        parsed = parse_query(query_str)
        sql, params = generate_sql_query(parsed)

        # Should not raise errors and should generate valid SQL
        assert sql.startswith("(card.card_rarity_int")
        assert len(params) == 1

    # Test different cases for comparisons
    parsed_comparison = parse_query("rarity>Common")
    sql, params = generate_sql_query(parsed_comparison)

    # Should contain simple numeric comparison and not raise errors
    assert "card.card_rarity_int >" in sql
    assert params[next(iter(params.keys()))] == 0  # common = 0


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        ("artist:moeller", r"(lower(card.card_artist) LIKE %(p_str_JW1vZWxsZXIl)s)", {"p_str_JW1vZWxsZXIl": r"%moeller%"}),
        ("a:moeller", r"(lower(card.card_artist) LIKE %(p_str_JW1vZWxsZXIl)s)", {"p_str_JW1vZWxsZXIl": r"%moeller%"}),
        (
            'artist:"Christopher Moeller"',
            r"(lower(card.card_artist) LIKE %(p_str_JWNocmlzdG9waGVyJW1vZWxsZXIl)s)",
            {"p_str_JWNocmlzdG9waGVyJW1vZWxsZXIl": r"%christopher%moeller%"},
        ),
        ("artist:nielsen", r"(lower(card.card_artist) LIKE %(p_str_JW5pZWxzZW4l)s)", {"p_str_JW5pZWxzZW4l": r"%nielsen%"}),
        ("ARTIST:moeller", r"(lower(card.card_artist) LIKE %(p_str_JW1vZWxsZXIl)s)", {"p_str_JW1vZWxsZXIl": r"%moeller%"}),
        # `a=` is `a:`, not an equality against the credit line. Measured on api.scryfall.com
        # 2026-08-16: `a="todd lockwood"` is 87, exactly `a:"todd lockwood"`'s 87, and a strict
        # fragment answers in the hundreds under `=` just as it does under `:`. A whole-string
        # equality answered these queries with the cards whose ENTIRE credit is that name, which
        # is not a distinction Scryfall draws and which loses every joint credit outright.
        (
            'artist="todd lockwood"',
            r"(lower(card.card_artist) LIKE %(p_str_JXRvZGQlbG9ja3dvb2Ql)s)",
            {"p_str_JXRvZGQlbG9ja3dvb2Ql": r"%todd%lockwood%"},
        ),
        (
            'artist="TODD LOCKWOOD"',
            r"(lower(card.card_artist) LIKE %(p_str_JXRvZGQlbG9ja3dvb2Ql)s)",
            {"p_str_JXRvZGQlbG9ja3dvb2Ql": r"%todd%lockwood%"},
        ),
        (
            'a="TODD LOCKWOOD"',
            r"(lower(card.card_artist) LIKE %(p_str_JXRvZGQlbG9ja3dvb2Ql)s)",
            {"p_str_JXRvZGQlbG9ja3dvb2Ql": r"%todd%lockwood%"},
        ),
    ],
)
def test_artist_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test SQL generation for artist search queries."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert observed_sql == expected_sql
    assert context == expected_parameters


@pytest.mark.parametrize(
    argnames=("input_query", "expected_parameters"),
    argvalues=[
        # Basic format search (format: means legal in format)
        (
            "format:standard",
            {"standard": "legal"},
        ),
        # Format alias 'f:'
        (
            "f:modern",
            {"modern": "legal"},
        ),
        # Legal search (explicit legal status)
        (
            "legal:legacy",
            {"legacy": "legal"},
        ),
        # Banned search
        (
            "banned:standard",
            {"standard": "banned"},
        ),
        # Restricted search
        (
            "restricted:vintage",
            {"vintage": "restricted"},
        ),
        # Case insensitive format names
        (
            "format:Standard",
            {"standard": "legal"},
        ),
        # Format with spaces in quotes
        (
            'format:"Historic Brawl"',
            {"historic brawl": "legal"},
        ),
        # Single letter format codes
        (
            "f:m",
            {"modern": "legal"},
        ),
        (
            "f:s",
            {"standard": "legal"},
        ),
        (
            "f:l",
            {"legacy": "legal"},
        ),
        (
            "f:p",
            {"pauper": "legal"},
        ),
        (
            "f:c",
            {"commander": "legal"},
        ),
        (
            "f:v",
            {"vintage": "legal"},
        ),
        (
            "f:h",
            {"historic": "legal"},
        ),
        # Single letter format codes with format: prefix
        (
            "format:m",
            {"modern": "legal"},
        ),
        # Single letter format codes with legal: prefix
        (
            "legal:s",
            {"standard": "legal"},
        ),
        # Single letter format codes with banned: prefix
        (
            "banned:m",
            {"modern": "banned"},
        ),
        # Case insensitive single letter format codes
        (
            "f:M",
            {"modern": "legal"},
        ),
        (
            "f:S",
            {"standard": "legal"},
        ),
    ],
)
def test_legality_search_sql_translation(parse_query, input_query: str, expected_parameters: dict) -> None:
    """Test that legality search generates correct SQL with JSONB operators."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    # Note: The parameter names will be auto-generated hashes, so we need a more flexible comparison
    assert "card.card_legalities @>" in observed_sql, f"Expected JSONB containment in SQL: {observed_sql}"
    # Check that we have exactly one parameter
    assert len(context) == 1, f"Expected exactly one parameter in context: {context}"

    # Verify the parameter value matches expected format and status
    param_value = next(iter(context.values()))
    assert param_value == expected_parameters, f"Expected parameter value: {expected_parameters}, got: {param_value}"


def test_legality_invalid_attribute() -> None:
    """Test that invalid legality attributes raise appropriate errors."""
    with pytest.raises(ValueError, match="Unknown legality attribute"):
        get_legality_comparison_object("standard", "invalid_attr")


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql_fragment", "expected_parameters"),
    argvalues=[
        (
            "number:123",
            "(card.collector_number_int = %(p_int_",
            {123},
        ),
        (
            "cn:45",
            "(card.collector_number_int = %(p_int_",
            {45},
        ),
        (
            "number:1a",
            "(card.collector_number = %(p_str_",
            {"1a"},
        ),
        (
            "cn:100b",
            "(card.collector_number = %(p_str_",
            {"100b"},
        ),
        (
            'number:"123"',
            "(card.collector_number = %(p_str_",
            {"123"},
        ),
    ],
)
def test_collector_number_sql_translation(
    parse_query, input_query: str, expected_sql_fragment: str, expected_parameters: set
) -> None:
    """Test that collector number searches generate correct SQL with exact matching for colon operator."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert expected_sql_fragment in observed_sql, f"Expected SQL fragment in: {observed_sql}"
    # Check that we have exactly one parameter
    assert len(context) == 1, f"Expected exactly one parameter in context: {context}"
    # Verify the parameter value is in expected set
    param_value = next(iter(context.values()))
    assert param_value in expected_parameters, f"Expected parameter value in {expected_parameters}, got: {param_value}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql_fragment", "expected_parameters"),
    argvalues=[
        (
            "number>50",
            "(card.collector_number_int > %(p_int_",
            {50},
        ),
        (
            "cn<100",
            "(card.collector_number_int < %(p_int_",
            {100},
        ),
        (
            "number>=25",
            "(card.collector_number_int >= %(p_int_",
            {25},
        ),
        (
            "cn<=75",
            "(card.collector_number_int <= %(p_int_",
            {75},
        ),
    ],
)
def test_collector_number_numeric_comparison_sql_translation(
    parse_query,
    input_query: str,
    expected_sql_fragment: str,
    expected_parameters: set,
) -> None:
    """Test that collector number numeric comparisons generate correct SQL using the integer column."""
    parsed = parse_query(input_query)
    context = QueryContext()
    observed_sql = parsed.to_sql(context)
    assert expected_sql_fragment in observed_sql, f"Expected SQL fragment in: {observed_sql}"
    # Check that we have exactly one parameter
    assert len(context) == 1, f"Expected exactly one parameter in context: {context}"
    # Verify the parameter value is in expected set
    param_value = next(iter(context.values()))
    assert param_value in expected_parameters, f"Expected parameter value in {expected_parameters}, got: {param_value}"


def test_standalone_numeric_query_parses(parse_query) -> None:
    """Test that standalone numeric queries like '1' parse to NumericValueNode.

    Per issue #90, queries like '1' should parse successfully to a NumericValueNode,
    but then fail at the database level with a datatype mismatch error since
    PostgreSQL expects boolean values in WHERE clauses, not integers.
    """
    # Test integer
    parsed_query = parse_query("1")
    assert isinstance(parsed_query.root, parsing.NumericValueNode)
    assert parsed_query.root.value == 1

    # Test float
    parsed_query_float = parse_query("2.5")
    assert isinstance(parsed_query_float.root, parsing.NumericValueNode)
    assert parsed_query_float.root.value == 2.5

    # Test SQL generation - this should produce a parameterized query
    sql, context = generate_sql_query(parsed_query)

    # Should be a parameterized value
    assert sql.startswith("%(")
    assert sql.endswith(")s")
    # Context should contain the numeric value
    assert 1 in context.values()


@pytest.mark.parametrize(
    argnames="semantically_invalid_query",
    argvalues=[
        "name:bolt and 1",  # Valid parse but semantically invalid: AND between boolean and integer
        "cmc=3 and 2",  # Valid parse but semantically invalid: AND between boolean and integer
        "power>1 or 5",  # Valid parse but semantically invalid: OR between boolean and integer
    ],
)
def test_semantically_invalid_queries_parse_but_fail_at_db_level(parse_query, semantically_invalid_query: str) -> None:
    """Test that queries with standalone numeric literals parse but would fail at DB level.

    These queries are syntactically valid after issue #90 (allowing standalone numeric literals),
    but they're semantically invalid because they combine boolean expressions with bare integers.
    They should parse successfully but would fail at the database level with datatype mismatch errors.
    """
    # These should parse without errors
    parsed_query = parse_query(semantically_invalid_query)

    # Should be able to generate SQL (though it would fail at execution)
    sql, context = generate_sql_query(parsed_query)

    # SQL should be generated successfully (it's the execution that would fail)
    assert isinstance(sql, str)
    assert isinstance(context, dict)


@pytest.mark.parametrize(
    argnames=("input_query", "should_parse"),
    argvalues=[
        # Test color word patterns
        ("color:white", True),
        ("color:blue", True),
        ("color:black", True),
        ("color:red", True),
        ("color:green", True),
        ("color:colorless", True),
        # Test color letter patterns
        ("color:w", True),
        ("color:u", True),
        ("color:b", True),
        ("color:r", True),
        ("color:g", True),
        ("color:c", True),
        ("color:wubr", True),
        ("color:rg", True),
        ("color:WUBRG", True),
        # Test color identity aliases
        ("id:red", True),
        ("identity:wubr", True),
        ("coloridentity:rg", True),
        # Test mixed case color names
        ("color:White", True),
        ("color:BLUE", True),
        ("color:Red", True),
        # Test invalid color combinations should fail to parse (validation enforced by color parser)
        ("color:invalid", False),  # Invalid color name should fail to parse
        ("color:xyz", False),  # Invalid color name should fail to parse
    ],
)
def test_color_parser_patterns(parse_query, input_query: str, should_parse: bool) -> None:
    """Test that color parser patterns work correctly."""
    if should_parse:
        # Should parse without raising an exception
        parsed = parse_query(input_query)
        assert parsed is not None

        # Should be able to generate SQL
        context = QueryContext()
        sql = parsed.to_sql(context)
        assert isinstance(sql, str)
        assert context  # Should have some parameters
    else:
        # Should raise a ValueError (which wraps ParseException)
        with pytest.raises(ValueError, match="Failed to parse query"):
            parse_query(input_query)


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql_fragment"),
    argvalues=[
        # Test that negated type queries generate simple, clean SQL
        # (no NULL handling needed since database ensures non-NULL arrays)
        ("-t:elf", "NOT ((%(p_list_WydFbGYnXQ)s <@ card.card_subtypes))"),
        ("llanowar -t:elf", "NOT ((%(p_list_WydFbGYnXQ)s <@ card.card_subtypes))"),
        ("-type:creature", "NOT ((%(p_list_WydDcmVhdHVyZSdd)s <@ card.card_types))"),
        # < and != on jsonb arrays use order-insensitive set semantics
        # (proper subset / not-set-equal), mirroring the engine's CollectionCmp
        (
            "t<creature",
            "(card.card_types <@ %(p_list_WydDcmVhdHVyZSdd)s) AND NOT(%(p_list_WydDcmVhdHVyZSdd)s <@ card.card_types)",
        ),
        (
            "t!=creature",
            "NOT((card.card_types <@ %(p_list_WydDcmVhdHVyZSdd)s) AND (%(p_list_WydDcmVhdHVyZSdd)s <@ card.card_types))",
        ),
    ],
)
def test_negated_type_queries_generate_simple_sql(parse_query, input_query: str, expected_sql_fragment: str) -> None:
    """Test that negated type queries generate simple, clean SQL without NULL handling."""
    parsed = parse_query(input_query)
    observed_params = QueryContext()
    observed_sql = parsed.to_sql(observed_params)
    assert expected_sql_fragment in observed_sql, f"Expected fragment '{expected_sql_fragment}' not found in SQL: {observed_sql}"


@pytest.mark.parametrize(
    argnames=("input_query", "expected_sql", "expected_parameters"),
    argvalues=[
        # Frame version search (exact matching with JSONB object, all titlecased)
        (
            "frame:2015",
            r"(card.card_frame_data @> %(p_dict_eycyMDE1JzogVHJ1ZX0)s)",
            {"p_dict_eycyMDE1JzogVHJ1ZX0": {"2015": True}},
        ),
        (
            "frame:1997",
            r"(card.card_frame_data @> %(p_dict_eycxOTk3JzogVHJ1ZX0)s)",
            {"p_dict_eycxOTk3JzogVHJ1ZX0": {"1997": True}},
        ),
        # Frame effects search (using same frame: syntax, titlecased)
        (
            "frame:showcase",
            r"(card.card_frame_data @> %(p_dict_eydTaG93Y2FzZSc6IFRydWV9)s)",
            {"p_dict_eydTaG93Y2FzZSc6IFRydWV9": {"Showcase": True}},
        ),
        (
            "frame:legendary",
            r"(card.card_frame_data @> %(p_dict_eydMZWdlbmRhcnknOiBUcnVlfQ)s)",
            {"p_dict_eydMZWdlbmRhcnknOiBUcnVlfQ": {"Legendary": True}},
        ),
    ],
)
def test_frame_sql_translation(parse_query, input_query: str, expected_sql: str, expected_parameters: dict) -> None:
    """Test that frame search generates correct SQL with exact matching."""
    parsed = parse_query(input_query)
    observed_params = QueryContext()
    observed_sql = parsed.to_sql(observed_params)
    assert observed_sql == expected_sql, f"\nExpected: {expected_sql}\nObserved: {observed_sql}"
    assert observed_params == expected_parameters, f"\nExpected params: {expected_parameters}\nObserved params: {observed_params}"


def test_name_titlecasing(parse_query) -> None:
    """Test that name is titlecased on the exact-match path.

    Written against `name!=` because `name=` no longer reaches that path: `=` is a synonym for `:`
    on a string column, so the value is lowercased into a LIKE pattern where titlecasing is not
    observable. `!=` is the exact-match path that remains.
    """
    parsed = parse_query(""" name!="Urza's Saga" """.strip())
    observed_params = QueryContext()
    observed_sql = parsed.to_sql(observed_params)
    assert observed_params == {"p_str_VXJ6YSdzIFNhZ2E": r"Urza's Saga"}
    assert observed_sql == r"(card.card_name != %(p_str_VXJ6YSdzIFNhZ2E)s)"


@pytest.mark.parametrize(
    argnames=("colon_query", "equals_query"),
    argvalues=[
        # BARE and QUOTED are two different searches, and `=` preserves the distinction rather
        # than flattening it to one side. Measured on api.scryfall.com 2026-08-16: `name=ft` is
        # 1,628 (exactly `name:ft`, NOT `name:"ft"`'s 362) and `name="ft"` is 362 (exactly
        # `name:"ft"`). Before this, `name=ft` was a whole-string equality and answered nothing.
        ("name:ft", "name=ft"),
        ('name:"ft"', 'name="ft"'),
        ('name:"Urza\'s Saga"', 'name="Urza\'s Saga"'),
        # And the rest of the text columns, where `=` carried no information of its own either:
        # `o=flying` is 4,574 = `o:flying` 4,574, `ft=aether` is 80 = `ft:aether` 80.
        ("o:flying", "o=flying"),
        ("ft:aether", "ft=aether"),
        ("a:moeller", "a=moeller"),
        # The collection columns, same rule: `kw=flying e:khm` 28 = `kw:flying` 28, `otag=ramp
        # e:khm` 35 = `otag:ramp` 35, `t=creature e:khm` 151 = `t:creature` 151.
        ("kw:flying", "kw=flying"),
        ("otag:ramp", "otag=ramp"),
        ("t:creature", "t=creature"),
        ("f:modern", "f=modern"),
        # The columns stored exact rather than searched already agreed, and still do -- `=` is
        # kept there because equality IS the meaning, not because it was rewritten.
        ("e:khm", "e=khm"),
        ("layout:normal", "layout=normal"),
    ],
)
def test_equals_is_a_synonym_for_colon(parse_query, colon_query: str, equals_query: str) -> None:
    """`=` on a text or collection column asks exactly what `:` asks."""
    colon_context = QueryContext()
    colon_sql = parse_query(colon_query).to_sql(colon_context)
    equals_context = QueryContext()
    equals_sql = parse_query(equals_query).to_sql(equals_context)

    assert equals_sql == colon_sql
    assert equals_context == colon_context


@pytest.mark.parametrize(
    argnames=("colon_query", "equals_query"),
    argvalues=[
        # THE BOUNDARY, probed in the other direction rather than assumed. `=` stays a real
        # equality on the set-valued COLOR columns and on devotion. Measured on api.scryfall.com
        # 2026-08-16 over `e:khm t:creature`: `c=rg` 1 against `c:rg`'s 2, `id=rg` 1 against
        # `id:rg`'s 52, `devotion={r}` 20 against `devotion:{r}`'s 27.
        ("c:rg", "c=rg"),
        ("id:rg", "id=rg"),
        ("produces:rg", "produces=rg"),
        ("devotion:{r}", "devotion={r}"),
    ],
)
def test_equals_is_still_an_equality_where_equality_is_the_meaning(parse_query, colon_query: str, equals_query: str) -> None:
    """The columns whose `=` differs from `:` on Scryfall keep it here."""
    colon_sql = parse_query(colon_query).to_sql(QueryContext())
    equals_sql = parse_query(equals_query).to_sql(QueryContext())

    assert equals_sql != colon_sql


@pytest.mark.parametrize(
    argnames="query",
    argvalues=["", "   ", None],
)
def test_empty_query_generates_true(parse_query, query: str | None) -> None:
    """Empty/whitespace/None queries should produce TRUE with no bound parameters."""
    sql, params = generate_sql_query(parse_query(query))
    assert sql == "TRUE"
    assert params == {}


testcases_colors_comparison_object = {
    # card_colors/card_color_identity: colorless is the absence of color.
    "colors_c": {"val": "c", "attr": "card_colors", "expected": {}},
    "colors_colorless": {"val": "colorless", "attr": "card_colors", "expected": {}},
    "identity_c": {"val": "c", "attr": "card_color_identity", "expected": {}},
    "colors_rg": {"val": "rg", "attr": "card_colors", "expected": {"R": True, "G": True}},
    # produced_mana: colorless ("C") is a genuine producible value, per Scryfall's
    # own data (Sol Ring's produced_mana is ["C"], not []).
    "produces_c": {"val": "c", "attr": "produced_mana", "expected": {"C": True}},
    "produces_colorless": {"val": "colorless", "attr": "produced_mana", "expected": {"C": True}},
    "produces_wc": {"val": "wc", "attr": "produced_mana", "expected": {"W": True, "C": True}},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases_colors_comparison_object.values()))),
    argvalues=[
        [v for k, v in sorted(testcases_colors_comparison_object[t].items())] for t in sorted(testcases_colors_comparison_object)
    ],
    ids=sorted(testcases_colors_comparison_object),
)
def test_get_colors_comparison_object(val: str, attr: str, expected: dict) -> None:
    assert get_colors_comparison_object(val, attr) == expected


testcases_color_dict_to_mask = {
    "colorless": {"color_dict": {}, "expected": 0},
    "white_only": {"color_dict": {"W": True}, "expected": 16},
    "red_green": {"color_dict": {"R": True, "G": True}, "expected": 3},
    "all_five": {"color_dict": {"W": True, "U": True, "B": True, "R": True, "G": True}, "expected": 31},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases_color_dict_to_mask.values()))),
    argvalues=[[v for k, v in sorted(testcases_color_dict_to_mask[t].items())] for t in sorted(testcases_color_dict_to_mask)],
    ids=sorted(testcases_color_dict_to_mask),
)
def test_color_dict_to_mask(color_dict: dict, expected: int) -> None:
    assert _color_dict_to_mask(color_dict) == expected


testcases_subset_masks = {
    "colorless": {"query_mask": 0, "expected": [0]},
    "white_only": {"query_mask": 16, "expected": [0, 16]},
    "red_green": {"query_mask": 3, "expected": [0, 1, 2, 3]},
    "wub": {"query_mask": 28, "expected": [0, 4, 8, 12, 16, 20, 24, 28]},
    "all_five": {"query_mask": 31, "expected": list(range(32))},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases_subset_masks.values()))),
    argvalues=[[v for k, v in sorted(testcases_subset_masks[t].items())] for t in sorted(testcases_subset_masks)],
    ids=sorted(testcases_subset_masks),
)
def test_subset_masks(query_mask: int, expected: list[int]) -> None:
    assert _subset_masks(query_mask) == expected


testcases_proper_subset_masks = {
    "colorless": {"query_mask": 0, "expected": []},
    "white_only": {"query_mask": 16, "expected": [0]},
    "red_green": {"query_mask": 3, "expected": [0, 1, 2]},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases_proper_subset_masks.values()))),
    argvalues=[[v for k, v in sorted(testcases_proper_subset_masks[t].items())] for t in sorted(testcases_proper_subset_masks)],
    ids=sorted(testcases_proper_subset_masks),
)
def test_proper_subset_masks(query_mask: int, expected: list[int]) -> None:
    assert _proper_subset_masks(query_mask) == expected


# ── #976: commander:/colour:/colours: as aliases, not a literal name search ──────────────
# (alias query, canonical query) -- alias spelling must generate byte-identical SQL to the
# canonical one, not fall through to a literal name search on card_name.
COMMANDER_COLOUR_ALIAS_EQUIVALENCES = [
    ("commander:wub", "id:wub"),
    ("commander<=wub", "id<=wub"),
    ("colour:blue", "color:blue"),
    ("colours:wu", "colors:wu"),
]


@pytest.mark.parametrize(
    argnames=["alias_query", "canonical_query"],
    argvalues=COMMANDER_COLOUR_ALIAS_EQUIVALENCES,
    ids=[q for q, _ in COMMANDER_COLOUR_ALIAS_EQUIVALENCES],
)
def test_commander_colour_alias_generates_same_sql(parse_query, alias_query: str, canonical_query: str) -> None:
    """commander:/colour:/colours: generate identical SQL to id:/color:/colors:, for both parsers."""
    alias_context = QueryContext()
    canonical_context = QueryContext()
    alias_sql = parse_query(alias_query).to_sql(alias_context)
    canonical_sql = parse_query(canonical_query).to_sql(canonical_context)
    assert alias_sql == canonical_sql
    assert alias_context == canonical_context
