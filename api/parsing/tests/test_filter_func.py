"""Tests for to_filter_func() — the in-process Python filter path."""

from __future__ import annotations

import pytest

from api.card_store import CardField, _LOWER_PAIRS
from api.parsing import parse_scryfall_query

# Minimal base card values keyed by field name.
_BASE_DICT: dict = {
    "scryfall_id": "aaaaaaaa-0000-0000-0000-000000000000",
    "oracle_id": "bbbbbbbb-0000-0000-0000-000000000000",
    "illustration_id": "cccccccc-0000-0000-0000-000000000000",
    "card_name": "Lightning Bolt",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "flavor_text": "The sparkmage shrieked.",
    "card_artist": "Christopher Mooney",
    "card_set_code": "m10",
    "collector_number": "146",
    "collector_number_int": 146,
    "cmc": 1.0,
    "creature_power": None,
    "creature_toughness": None,
    "planeswalker_loyalty": None,
    "card_colors": {"R": True},
    "card_color_identity": {"R": True},
    "card_keywords": {},
    "card_types": ["Instant"],
    "card_subtypes": [],
    "card_rarity_int": 0,
    "card_legalities": {"modern": "legal", "legacy": "legal", "vintage": "legal", "standard": "not_legal"},
    "card_oracle_tags": {"direct-damage": True, "burn": True},
    "card_is_tags": {"spell": True},
    "mana_cost_jsonb": {"R": [1]},
    "mana_cost_text": "{R}",
    "released_at": "1993-08-05",
    "edhrec_rank": 100,
    "price_usd": 1.99,
    "price_eur": 1.50,
    "price_tix": 0.5,
    "prefer_score": 100,
    "type_line": "Instant",
    "set_name": "Magic 2010",
    "card_layout": "normal",
    "card_border": "black",
    "card_watermark": None,
    "card_frame_data": {"2015": True},
    "devotion": {"R": [1]},
    "produced_mana": {},
    "creature_power_text": None,
    "creature_toughness_text": None,
    "cubecobra_score": None,
}

# Pre-built base card as a list, matching the CardField index layout.
_BASE: list = [None] * len(CardField)
for _name, _value in _BASE_DICT.items():
    _BASE[CardField[_name]] = _value


def card(**overrides: object) -> list:
    """Build a card list from _BASE, applying overrides and refreshing _lower fields."""
    result = list(_BASE)
    for name, value in overrides.items():
        result[CardField[name]] = value
    for src, dst in _LOWER_PAIRS:
        v = result[src]
        result[dst] = v.lower() if v is not None else None
    return result


def _run(query: str, c: list) -> bool:
    return parse_scryfall_query(query).to_filter_func()(c)


# ---------------------------------------------------------------------------
# Numeric comparisons
# ---------------------------------------------------------------------------

testcases_numeric = {
    "cmc_eq_match": {"query": "cmc=1", "c": card(cmc=1.0), "expected": True},
    "cmc_eq_no_match": {"query": "cmc=1", "c": card(cmc=2.0), "expected": False},
    "cmc_lt_match": {"query": "cmc<3", "c": card(cmc=1.0), "expected": True},
    "cmc_lt_no_match": {"query": "cmc<3", "c": card(cmc=3.0), "expected": False},
    "cmc_gte_match": {"query": "cmc>=1", "c": card(cmc=1.0), "expected": True},
    "cmc_gte_no_match": {"query": "cmc>=2", "c": card(cmc=1.0), "expected": False},
    "cmc_colon_eq": {"query": "cmc:1", "c": card(cmc=1.0), "expected": True},
    "power_eq_match": {"query": "power=3", "c": card(creature_power=3.0), "expected": True},
    "power_eq_no_match": {"query": "power=3", "c": card(creature_power=2.0), "expected": False},
    "power_null_no_match": {"query": "power=3", "c": card(creature_power=None), "expected": False},
    "toughness_gt_match": {"query": "toughness>2", "c": card(creature_toughness=3.0), "expected": True},
    "loyalty_eq_match": {"query": "loyalty=4", "c": card(planeswalker_loyalty=4), "expected": True},
    "toughness_neq_match": {"query": "toughness!=2", "c": card(creature_toughness=3.0), "expected": True},
    "toughness_neq_no_match": {"query": "toughness!=2", "c": card(creature_toughness=2.0), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_numeric.items())],
    ids=sorted(testcases_numeric),
)
def test_numeric_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Rarity
# ---------------------------------------------------------------------------

testcases_rarity = {
    "rarity_eq_common_match": {"query": "rarity=common", "c": card(card_rarity_int=0), "expected": True},
    "rarity_eq_common_no_match": {"query": "rarity=common", "c": card(card_rarity_int=1), "expected": False},
    "rarity_gte_uncommon_match": {"query": "rarity>=uncommon", "c": card(card_rarity_int=2), "expected": True},
    "rarity_gte_uncommon_no_match": {"query": "rarity>=uncommon", "c": card(card_rarity_int=0), "expected": False},
    "rarity_lt_rare_match": {"query": "rarity<rare", "c": card(card_rarity_int=1), "expected": True},
    "rarity_lt_rare_no_match": {"query": "rarity<rare", "c": card(card_rarity_int=2), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_rarity.items())],
    ids=sorted(testcases_rarity),
)
def test_rarity_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Text / name search
# ---------------------------------------------------------------------------

testcases_text = {
    "name_contains_match": {"query": "name:lightning", "c": card(card_name="Lightning Bolt"), "expected": True},
    "name_contains_no_match": {"query": "name:lightning", "c": card(card_name="Counterspell"), "expected": False},
    "name_contains_case_insensitive": {"query": "name:LIGHTNING", "c": card(card_name="Lightning Bolt"), "expected": True},
    "name_exact_match": {"query": 'name="Lightning Bolt"', "c": card(card_name="Lightning Bolt"), "expected": True},
    "name_exact_no_match": {"query": 'name="Lightning"', "c": card(card_name="Lightning Bolt"), "expected": False},
    "exact_name_node_match": {"query": '!"Lightning Bolt"', "c": card(card_name="Lightning Bolt"), "expected": True},
    "exact_name_node_no_match": {"query": '!"Counterspell"', "c": card(card_name="Lightning Bolt"), "expected": False},
    "oracle_contains_match": {"query": "oracle:damage", "c": card(oracle_text="deals 3 damage"), "expected": True},
    "oracle_contains_no_match": {"query": "oracle:damage", "c": card(oracle_text="draw a card"), "expected": False},
    "oracle_multiword_match": {
        "query": "oracle:'3 damage'",
        "c": card(oracle_text="deals 3 damage to any target"),
        "expected": True,
    },
    "oracle_multiword_no_match": {"query": "oracle:'3 damage'", "c": card(oracle_text="deals 5 damage"), "expected": False},
    "set_match": {"query": "set:m10", "c": card(card_set_code="m10"), "expected": True},
    "set_no_match": {"query": "set:m10", "c": card(card_set_code="lea"), "expected": False},
    "artist_contains_match": {"query": "artist:mooney", "c": card(card_artist="Christopher Mooney"), "expected": True},
    "artist_contains_no_match": {"query": "artist:mooney", "c": card(card_artist="Mark Poole"), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_text.items())],
    ids=sorted(testcases_text),
)
def test_text_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

testcases_regex = {
    "oracle_regex_match": {"query": r"oracle:/\d damage/", "c": card(oracle_text="deals 3 damage"), "expected": True},
    "oracle_regex_no_match": {"query": r"oracle:/\d damage/", "c": card(oracle_text="draw a card"), "expected": False},
    "oracle_regex_case_insensitive": {"query": r"oracle:/DAMAGE/", "c": card(oracle_text="deals 3 damage"), "expected": True},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_regex.items())],
    ids=sorted(testcases_regex),
)
def test_regex_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

testcases_colors = {
    "color_contains_match": {"query": "color:r", "c": card(card_colors={"R": True}), "expected": True},
    "color_contains_no_match": {"query": "color:r", "c": card(card_colors={"U": True}), "expected": False},
    "color_eq_match": {"query": "color=r", "c": card(card_colors={"R": True}), "expected": True},
    "color_eq_no_match_superset": {"query": "color=r", "c": card(card_colors={"R": True, "G": True}), "expected": False},
    "color_gte_match": {"query": "color>=rg", "c": card(card_colors={"R": True, "G": True, "U": True}), "expected": True},
    "color_gte_no_match": {"query": "color>=rg", "c": card(card_colors={"R": True}), "expected": False},
    "color_lte_match": {"query": "color<=rg", "c": card(card_colors={"R": True}), "expected": True},
    "color_lte_no_match": {"query": "color<=rg", "c": card(card_colors={"R": True, "U": True}), "expected": False},
    "color_gt_match": {"query": "color>r", "c": card(card_colors={"R": True, "G": True}), "expected": True},
    "color_gt_no_match_equal": {"query": "color>r", "c": card(card_colors={"R": True}), "expected": False},
    "color_lt_match": {"query": "color<rg", "c": card(card_colors={"R": True}), "expected": True},
    "color_lt_no_match_equal": {"query": "color<rg", "c": card(card_colors={"R": True, "G": True}), "expected": False},
    "colorless_eq_match": {"query": "color=c", "c": card(card_colors={}), "expected": True},
    "colorless_eq_no_match": {"query": "color=c", "c": card(card_colors={"R": True}), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_colors.items())],
    ids=sorted(testcases_colors),
)
def test_color_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Color identity
# ---------------------------------------------------------------------------

testcases_identity = {
    "identity_contains_match": {"query": "id:r", "c": card(card_color_identity={"R": True}), "expected": True},
    "identity_contains_no_match": {"query": "id:r", "c": card(card_color_identity={"U": True}), "expected": False},
    "identity_lte_match": {"query": "id<=rg", "c": card(card_color_identity={"R": True}), "expected": True},
    "identity_lte_no_match": {"query": "id<=rg", "c": card(card_color_identity={"R": True, "U": True}), "expected": False},
    "identity_eq_match": {"query": "id=rg", "c": card(card_color_identity={"R": True, "G": True}), "expected": True},
    "identity_colorless_match": {"query": "id=c", "c": card(card_color_identity={}), "expected": True},
    "identity_colorless_no_match": {"query": "id=c", "c": card(card_color_identity={"R": True}), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_identity.items())],
    ids=sorted(testcases_identity),
)
def test_identity_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# JSONB object: keywords, tags, legalities
# ---------------------------------------------------------------------------

testcases_jsonb_obj = {
    "keyword_contains_match": {"query": "keyword:flying", "c": card(card_keywords={"Flying": True}), "expected": True},
    "keyword_contains_no_match": {"query": "keyword:flying", "c": card(card_keywords={}), "expected": False},
    "otag_match": {"query": "otag:burn", "c": card(card_oracle_tags={"burn": True}), "expected": True},
    "otag_no_match": {"query": "otag:burn", "c": card(card_oracle_tags={"counterspell": True}), "expected": False},
    "is_tag_match": {"query": "is:spell", "c": card(card_is_tags={"spell": True}), "expected": True},
    "is_tag_no_match": {"query": "is:spell", "c": card(card_is_tags={"creature": True}), "expected": False},
    "format_legal_match": {"query": "format:modern", "c": card(card_legalities={"modern": "legal"}), "expected": True},
    "format_legal_no_match": {"query": "format:modern", "c": card(card_legalities={"modern": "banned"}), "expected": False},
    "format_banned_match": {"query": "banned:modern", "c": card(card_legalities={"modern": "banned"}), "expected": True},
    "format_banned_no_match": {"query": "banned:modern", "c": card(card_legalities={"modern": "legal"}), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_jsonb_obj.items())],
    ids=sorted(testcases_jsonb_obj),
)
def test_jsonb_object_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# JSONB array: types and subtypes
# ---------------------------------------------------------------------------

testcases_jsonb_arr = {
    "type_instant_match": {"query": "type:instant", "c": card(card_types=["Instant"]), "expected": True},
    "type_instant_no_match": {"query": "type:instant", "c": card(card_types=["Creature"]), "expected": False},
    "type_creature_match": {"query": "type:creature", "c": card(card_types=["Creature"]), "expected": True},
    "subtype_wizard_match": {"query": "type:wizard", "c": card(card_subtypes=["Wizard"]), "expected": True},
    "subtype_wizard_no_match": {"query": "type:wizard", "c": card(card_subtypes=["Dragon"]), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_jsonb_arr.items())],
    ids=sorted(testcases_jsonb_arr),
)
def test_jsonb_array_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Date / year
# ---------------------------------------------------------------------------

testcases_date = {
    "date_eq_match": {"query": "date=1993-08-05", "c": card(released_at="1993-08-05"), "expected": True},
    "date_eq_no_match": {"query": "date=1993-08-05", "c": card(released_at="2024-01-01"), "expected": False},
    "date_lt_match": {"query": "date<2000-01-01", "c": card(released_at="1993-08-05"), "expected": True},
    "date_lt_no_match": {"query": "date<2000-01-01", "c": card(released_at="2024-01-01"), "expected": False},
    "year_eq_match": {"query": "year=1993", "c": card(released_at="1993-08-05"), "expected": True},
    "year_eq_no_match": {"query": "year=1993", "c": card(released_at="2024-01-01"), "expected": False},
    "year_gt_match": {"query": "year>2000", "c": card(released_at="2024-01-01"), "expected": True},
    "year_gt_no_match": {"query": "year>2000", "c": card(released_at="1993-08-05"), "expected": False},
    "year_lte_match": {"query": "year<=1993", "c": card(released_at="1993-12-31"), "expected": True},
    "year_lte_no_match": {"query": "year<=1993", "c": card(released_at="1994-01-01"), "expected": False},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_date.items())],
    ids=sorted(testcases_date),
)
def test_date_year_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Mana cost
# ---------------------------------------------------------------------------

testcases_mana = {
    "mana_eq_match": {
        "query": "mana=r",
        "c": card(mana_cost_jsonb={"R": [1]}, cmc=1.0),
        "expected": True,
    },
    "mana_eq_no_match": {
        "query": "mana=r",
        "c": card(mana_cost_jsonb={"R": [1], "G": [1]}, cmc=2.0),
        "expected": False,
    },
    "mana_gte_match": {
        "query": "mana>=r",
        "c": card(mana_cost_jsonb={"R": [1], "G": [1]}, cmc=2.0),
        "expected": True,
    },
    "mana_gte_no_match": {
        "query": "mana>=rr",
        "c": card(mana_cost_jsonb={"R": [1]}, cmc=1.0),
        "expected": False,
    },
    "mana_lte_match": {
        "query": "mana<=rg",
        "c": card(mana_cost_jsonb={"R": [1]}, cmc=1.0),
        "expected": True,
    },
    "mana_lte_no_match": {
        "query": "mana<=r",
        "c": card(mana_cost_jsonb={"R": [1], "G": [1]}, cmc=2.0),
        "expected": False,
    },
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_mana.items())],
    ids=sorted(testcases_mana),
)
def test_mana_filter(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected


# ---------------------------------------------------------------------------
# Boolean combinators: AND / OR / NOT
# ---------------------------------------------------------------------------

testcases_bool = {
    "and_both_match": {"query": "cmc=1 color:r", "c": card(cmc=1.0, card_colors={"R": True}), "expected": True},
    "and_one_fails": {"query": "cmc=1 color:r", "c": card(cmc=2.0, card_colors={"R": True}), "expected": False},
    "or_first_match": {"query": "cmc=1 OR cmc=2", "c": card(cmc=1.0), "expected": True},
    "or_second_match": {"query": "cmc=1 OR cmc=2", "c": card(cmc=2.0), "expected": True},
    "or_neither_match": {"query": "cmc=1 OR cmc=2", "c": card(cmc=3.0), "expected": False},
    "not_match": {"query": "-color:r", "c": card(card_colors={"U": True}), "expected": True},
    "not_no_match": {"query": "-color:r", "c": card(card_colors={"R": True}), "expected": False},
    "empty_query_always_true": {"query": "", "c": card(), "expected": True},
}


@pytest.mark.parametrize(
    argnames=["c", "expected", "query"],
    argvalues=[[v["c"], v["expected"], v["query"]] for k, v in sorted(testcases_bool.items())],
    ids=sorted(testcases_bool),
)
def test_boolean_combinators(query: str, c: dict, expected: bool) -> None:
    assert _run(query, c) == expected
