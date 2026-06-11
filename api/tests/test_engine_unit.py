# ruff: noqa: ERA001, PLR0913
"""Unit tests for the Rust QueryEngine — filters, dedup, prefer, and sort.

Fixture: api/tests/fixtures/engine_cards.json
  87 real card printings across 13 oracle IDs and 32+ illustration IDs.
  Chosen to exercise shared artworks, null prices, multi-color, hybrid mana,
  and varied CMC / type / rarity distributions.

Card summary (name → printings):
  Black Lotus       5p   colorless artifact        cmc=0
  Boggart Ram-Gang  4p   RG creature {R/G}{R/G}{R/G}  cmc=3
  Counterspell      6p   blue instant              cmc=2
  Dark Ritual       5p   black instant             cmc=1
  Jace, the Mind Sculptor 10p  blue planeswalker   cmc=4
  Kitchen Finks     6p   GW creature {1}{G/W}{G/W} cmc=3
  Lightning Bolt   10p   red instant               cmc=1
  Nicol Bolas, Planeswalker 7p  UBR planeswalker   cmc=8
  Serra Angel       7p   white creature 4/4        cmc=5
  Shivan Dragon     5p   red creature 5/5          cmc=6
  Sol Ring          5p   colorless artifact        cmc=1
  Spectral Procession 6p white sorcery {2/W}{2/W}{2/W}  cmc=6
  Tarmogoyf        11p   green creature */*+1      cmc=2
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.parsing import parse_scryfall_query
from card_engine import QueryEngine

_FIXTURE = Path(__file__).parent / "fixtures" / "engine_cards.json"


@pytest.fixture(scope="module", name="engine")
def engine_fixture() -> QueryEngine:
    cards = json.loads(_FIXTURE.read_text())
    e = QueryEngine()
    e.reload(cards)
    return e


def _run(
    engine: QueryEngine,
    q: str = "",
    *,
    unique: str = "printing",
    prefer: str = "default",
    orderby: str = "edhrec",
    direction: str = "asc",
    limit: int = 200,
) -> tuple[int, list[dict]]:
    """Parse q, run engine.query(), return (total, cards). q='' matches all."""
    filters = parse_scryfall_query(q)
    total, cards = engine.query(
        filters=filters,
        unique=unique,
        prefer=prefer,
        orderby=orderby,
        direction=direction,
        limit=limit,
    )
    return total, list(cards)


def _names(cards: list[dict]) -> list[str]:
    return [c["name"] for c in cards]


class TestFilters:
    def test_match_all(self, engine: QueryEngine) -> None:
        total, cards = _run(engine)
        assert total == 87
        assert len(cards) == 87

    def test_name_exact(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, 'name="Lightning Bolt"')
        assert total == 10
        assert all(c["name"] == "Lightning Bolt" for c in cards)

    def test_name_contains(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "name:bolt")
        assert total == 10

    def test_name_with_absent_trigram_matches_nothing(self, engine: QueryEngine) -> None:
        # "bolxq" shares trigrams with "bolt" but contains trigrams no card name has;
        # the trigram narrowing must yield an empty candidate set (and zero results).
        total, cards = _run(engine, "name:bolxq")
        assert total == 0
        assert cards == []

    def test_set_code_query_is_case_insensitive(self, engine: QueryEngine) -> None:
        # Set codes are lowercased at import, so the query value is lowercased on
        # both the engine and SQL paths — set:LEA must behave like set:lea.
        t_upper, _ = _run(engine, "set:LEA")
        t_lower, _ = _run(engine, "set:lea")
        assert t_upper == t_lower == 7

    def test_collector_number_query_is_case_sensitive(self) -> None:
        # collector_number is stored raw and mixed-case (e.g. The List's "10E-105"),
        # and the SQL path compares it exactly — the engine must do the same.
        e = QueryEngine()
        e.reload(
            [
                {"card_name": "List Printing", "collector_number": "10E-105"},
                {"card_name": "Plain Printing", "collector_number": "105"},
            ]
        )
        total_exact, cards = _run(e, "cn:10E-105")
        assert total_exact == 1
        assert cards[0]["name"] == "List Printing"
        total_wrong_case, _ = _run(e, "cn:10e-105")
        assert total_wrong_case == 0

    def test_name_exact_titlecase_normalized(self, engine: QueryEngine) -> None:
        # name= should be case-insensitive (titlecase normalization applied on both paths)
        t_lower, _ = _run(engine, 'name="lightning bolt"')
        t_proper, _ = _run(engine, 'name="Lightning Bolt"')
        assert t_lower == t_proper == 10

    def test_artist_exact(self, engine: QueryEngine) -> None:
        # Christopher Rush painted Black Lotus (all 5) + Lightning Bolt (lea, leb, 2ed)
        total, _ = _run(engine, 'artist="Christopher Rush"')
        assert total == 8

    def test_artist_exact_lowercase_matches(self, engine: QueryEngine) -> None:
        # Validates titlecase normalization fix: lowercase query must match same rows
        t_lower, _ = _run(engine, 'artist="christopher rush"')
        t_proper, _ = _run(engine, 'artist="Christopher Rush"')
        assert t_lower == t_proper

    def test_color_red(self, engine: QueryEngine) -> None:
        # Lightning Bolt (10) + Shivan Dragon (5) + Nicol Bolas (7) + Boggart Ram-Gang (4)
        total, _ = _run(engine, "c:r")
        assert total == 26

    def test_color_white(self, engine: QueryEngine) -> None:
        # Serra Angel (7) + Kitchen Finks (6) + Spectral Procession (6)
        total, _ = _run(engine, "c:w")
        assert total == 19

    def test_color_blue(self, engine: QueryEngine) -> None:
        # Counterspell (6) + Jace (10) + Nicol Bolas (7)
        total, _ = _run(engine, "c:u")
        assert total == 23

    def test_color_black(self, engine: QueryEngine) -> None:
        # Dark Ritual (5) + Nicol Bolas (7)
        total, _ = _run(engine, "c:b")
        assert total == 12

    def test_color_green(self, engine: QueryEngine) -> None:
        # Tarmogoyf (11) + Boggart Ram-Gang (4) + Kitchen Finks (6)
        total, _ = _run(engine, "c:g")
        assert total == 21

    def test_colorless(self, engine: QueryEngine) -> None:
        # Black Lotus (5) + Sol Ring (5); use color= (exact) not c: (contains empty set = all cards)
        total, _ = _run(engine, "color=c")
        assert total == 10

    def test_cmc_equals_zero(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "cmc=0")
        assert total == 5
        assert all(c["name"] == "Black Lotus" for c in cards)

    def test_cmc_equals_one(self, engine: QueryEngine) -> None:
        # Lightning Bolt (10) + Dark Ritual (5) + Sol Ring (5)
        total, _ = _run(engine, "cmc=1")
        assert total == 20

    def test_cmc_gte_four(self, engine: QueryEngine) -> None:
        # Jace (cmc=4, 10) + Serra Angel (cmc=5, 7) + Shivan Dragon (cmc=6, 5)
        # + Spectral Procession (cmc=6, 6) + Nicol Bolas (cmc=8, 7)
        total, _ = _run(engine, "cmc>=4")
        assert total == 35

    def test_type_instant(self, engine: QueryEngine) -> None:
        # Lightning Bolt (10) + Counterspell (6) + Dark Ritual (5)
        total, _ = _run(engine, "t:instant")
        assert total == 21

    def test_type_creature(self, engine: QueryEngine) -> None:
        # Serra Angel (7) + Tarmogoyf (11) + Shivan Dragon (5) + Boggart Ram-Gang (4) + Kitchen Finks (6)
        total, _ = _run(engine, "t:creature")
        assert total == 33

    def test_type_planeswalker(self, engine: QueryEngine) -> None:
        # Jace (10) + Nicol Bolas (7)
        total, _ = _run(engine, "t:planeswalker")
        assert total == 17

    def test_type_artifact(self, engine: QueryEngine) -> None:
        # Black Lotus (5) + Sol Ring (5)
        total, _ = _run(engine, "t:artifact")
        assert total == 10

    def test_power_eq(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "pow=4")
        assert total == 7
        assert all(c["name"] == "Serra Angel" for c in cards)

    def test_toughness_eq(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "tou=4")
        assert total == 7
        assert all(c["name"] == "Serra Angel" for c in cards)

    def test_oracle_text_contains_flying(self, engine: QueryEngine) -> None:
        # Serra Angel (7) + Shivan Dragon (5) + Spectral Procession (6, creates flying tokens)
        total, _ = _run(engine, "o:flying")
        assert total == 18

    def test_set_filter(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "s:lea")
        assert total == 7
        assert all(c["set_code"] == "lea" for c in cards)

    def test_set_colon_operator_handled_by_engine(self, engine: QueryEngine) -> None:
        # The ":" operator for set/s must resolve inside the engine (str_op_to_cmp(":") == Eq).
        # If it ever returned Err the engine would throw and the API would fall back to SQL;
        # this test pins that it returns results without raising.
        total_colon, cards_colon = _run(engine, "set:lea")
        total_short, _ = _run(engine, "s:lea")
        assert total_colon == total_short
        assert total_colon > 0
        assert all(c["set_code"] == "lea" for c in cards_colon)

    def test_and_filter(self, engine: QueryEngine) -> None:
        # Red instants: only Lightning Bolt
        total, _ = _run(engine, "c:r t:instant")
        assert total == 10

    def test_or_filter(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, 'name="Black Lotus" OR name="Sol Ring"')
        assert total == 10

    def test_not_filter(self, engine: QueryEngine) -> None:
        # All creatures except Serra Angel
        total, cards = _run(engine, "t:creature -name:serra")
        assert total == 26  # Tarmogoyf (11) + Shivan Dragon (5) + Boggart Ram-Gang (4) + Kitchen Finks (6)
        assert all(c["name"] != "Serra Angel" for c in cards)


class TestArithmetic:
    def test_power_minus_toughness_eq_zero(self, engine: QueryEngine) -> None:
        # Serra Angel (4/4), Shivan Dragon (5/5), Boggart Ram-Gang (3/3)
        total, cards = _run(engine, "pow-tou=0")
        assert total == 16
        assert {c["name"] for c in cards} == {"Serra Angel", "Shivan Dragon", "Boggart Ram-Gang"}

    def test_power_plus_toughness_gt(self, engine: QueryEngine) -> None:
        # Only Shivan Dragon (5+5=10 > 8); Serra Angel (4+4=8) is excluded
        total, cards = _run(engine, "pow+tou>8")
        assert total == 5
        assert {c["name"] for c in cards} == {"Shivan Dragon"}

    def test_cmc_plus_constant_gt_power(self, engine: QueryEngine) -> None:
        # All 4 creature types: cmc+1 > power for all of them
        total, _ = _run(engine, "cmc+1>power")
        assert total == 22


class TestUnique:
    def test_unique_printing_returns_all(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, unique="printing")
        assert total == 87

    def test_unique_card_deduplicates_by_oracle_id(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, unique="card")
        assert total == 13
        assert len({c["name"] for c in cards}) == 13

    def test_unique_artwork_deduplicates_by_illustration(self, engine: QueryEngine) -> None:
        # 38 distinct illustration_ids across the 87 fixture printings
        total, _ = _run(engine, unique="artwork")
        assert total == 38

    def test_unique_card_single_result_per_name(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, 'name="Lightning Bolt"', unique="card")
        assert total == 1

    def test_unique_artwork_lightning_bolt(self, engine: QueryEngine) -> None:
        # Lightning Bolt has 6 distinct illustration_ids in the fixture
        total, _ = _run(engine, 'name="Lightning Bolt"', unique="artwork")
        assert total == 6

    def test_unique_printing_lightning_bolt(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, 'name="Lightning Bolt"', unique="printing")
        assert total == 10


class TestPrefer:
    """Tests use unique=card so each name maps to exactly one result."""

    def test_prefer_usd_low_picks_cheapest_priced(self, engine: QueryEngine) -> None:
        # Cheapest priced Lightning Bolt in fixture: m11 at $1.47.
        # Also validates the null-last fix: p09 and one sld print have null prices;
        # before the fix (unwrap_or(0.0)) they scored 0 which beat any real price.
        _, cards = _run(engine, 'name="Lightning Bolt"', unique="card", prefer="usd_low")
        assert cards[0]["set_code"] == "m11"

    def test_prefer_usd_high_picks_priciest(self, engine: QueryEngine) -> None:
        # Most expensive Lightning Bolt in fixture: lea at $620
        _, cards = _run(engine, 'name="Lightning Bolt"', unique="card", prefer="usd_high")
        assert cards[0]["set_code"] == "lea"

    def test_prefer_oldest_picks_oldest_printing(self, engine: QueryEngine) -> None:
        # Oldest Lightning Bolt is lea (1993-08-05)
        _, cards = _run(engine, 'name="Lightning Bolt"', unique="card", prefer="oldest")
        assert cards[0]["set_code"] == "lea"

    def test_prefer_newest_picks_newest_printing(self, engine: QueryEngine) -> None:
        # Newest Lightning Bolt is sld (2026-04-01)
        _, cards = _run(engine, 'name="Lightning Bolt"', unique="card", prefer="newest")
        assert cards[0]["set_code"] == "sld"

    def test_prefer_default_returns_one_per_oracle(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, unique="card", prefer="default")
        assert total == 13
        assert len({c["name"] for c in cards}) == 13


class TestSort:
    def test_sort_cmc_asc_first_is_zero(self, engine: QueryEngine) -> None:
        _, cards = _run(engine, orderby="cmc", direction="asc")
        assert cards[0]["name"] == "Black Lotus"  # only cmc=0 card

    def test_sort_cmc_desc_first_is_highest(self, engine: QueryEngine) -> None:
        _, cards = _run(engine, orderby="cmc", direction="desc")
        assert cards[0]["name"] == "Nicol Bolas, Planeswalker"  # only cmc=8 card

    def test_sort_cmc_asc_instants_ordered(self, engine: QueryEngine) -> None:
        # CMC-1 instants (Lightning Bolt, Dark Ritual) must appear before CMC-2 (Counterspell)
        _, cards = _run(engine, "t:instant", orderby="cmc", direction="asc")
        names = _names(cards)
        first_cmc1_idx = min(i for i, n in enumerate(names) if n in {"Lightning Bolt", "Dark Ritual"})
        last_counterspell_idx = max(i for i, n in enumerate(names) if n == "Counterspell")
        assert first_cmc1_idx < last_counterspell_idx

    def test_limit_caps_returned_cards(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, limit=5)
        assert total == 87  # total reflects full match count
        assert len(cards) == 5

    def test_sort_direction_desc_reverses_order(self, engine: QueryEngine) -> None:
        _, asc_cards = _run(engine, 'name="Lightning Bolt"', orderby="edhrec", direction="asc")
        _, desc_cards = _run(engine, 'name="Lightning Bolt"', orderby="edhrec", direction="desc")
        # Reversed direction should produce reversed order (10 distinct printings)
        assert _names(asc_cards) == list(reversed(_names(desc_cards)))


class TestDevotion:
    """Tests for devotion queries, including hybrid mana symbol splitting.

    Hybrid symbols like {R/G} must contribute to BOTH R and G devotion,
    matching calculate_devotion() in the SQL path. Previously the engine
    kept {R/G} as a single key and missed pure-color devotion queries.
    """

    def test_devotion_pure_color(self, engine: QueryEngine) -> None:
        # Lightning Bolt {R}: each printing has 1 R pip, so devotion:{R} should match all 10
        total, _ = _run(engine, 'devotion:{R} name="Lightning Bolt"')
        assert total == 10

    def test_devotion_hybrid_rg_counts_as_red(self, engine: QueryEngine) -> None:
        # Boggart Ram-Gang {R/G}{R/G}{R/G}: each {R/G} counts as 1 R pip
        # devotion:{R} should match all 4 printings
        total, _cards = _run(engine, 'devotion:{R} name="Boggart Ram-Gang"')
        assert total == 4

    def test_devotion_hybrid_rg_counts_as_green(self, engine: QueryEngine) -> None:
        # Same card: each {R/G} also counts as 1 G pip
        total, _cards = _run(engine, 'devotion:{G} name="Boggart Ram-Gang"')
        assert total == 4

    def test_devotion_hybrid_gw_counts_as_green(self, engine: QueryEngine) -> None:
        # Kitchen Finks {1}{G/W}{G/W}: 2 G pips via hybrid
        total, _cards = _run(engine, 'devotion:{G}{G} name="Kitchen Finks"')
        assert total == 6

    def test_devotion_hybrid_gw_counts_as_white(self, engine: QueryEngine) -> None:
        # Kitchen Finks: 2 W pips via hybrid
        total, _cards = _run(engine, 'devotion:{W}{W} name="Kitchen Finks"')
        assert total == 6

    def test_devotion_2w_hybrid_counts_as_white(self, engine: QueryEngine) -> None:
        # Spectral Procession {2/W}{2/W}{2/W}: the W in each {2/W} counts as 1 W pip
        # devotion:{W} (at least 1 W) should match all 6 printings
        total, _cards = _run(engine, 'devotion:{W} name="Spectral Procession"')
        assert total == 6

    def test_devotion_threshold_hybrid(self, engine: QueryEngine) -> None:
        # Boggart Ram-Gang has 3 R pips and 3 G pips from {R/G}{R/G}{R/G}
        # devotion:{R}{R}{R} (need 3 R) should match; devotion:{R}{R}{R}{R} should not
        total_3r, _ = _run(engine, 'devotion:{R}{R}{R} name="Boggart Ram-Gang"')
        total_4r, _ = _run(engine, 'devotion:{R}{R}{R}{R} name="Boggart Ram-Gang"')
        assert total_3r == 4
        assert total_4r == 0


class TestManaCost:
    """Tests for mana= / mana: queries, which use the faithful pip map (not devotion).

    Key invariant: {R/G}{R/G}{R/G} must NOT match mana:{R} (the card has no pure-R
    pips), even though devotion:{R} does match. If the pip/devotion split regresses,
    these tests will catch it.
    """

    def test_mana_contains_pure_pip(self, engine: QueryEngine) -> None:
        # Lightning Bolt has exactly {R}; mana:{R} should match all 10 printings
        total, _ = _run(engine, 'mana:{R} name="Lightning Bolt"')
        assert total == 10

    def test_mana_contains_hybrid_symbol(self, engine: QueryEngine) -> None:
        # Boggart Ram-Gang costs {R/G}{R/G}{R/G}; mana:{R/G} matches
        total, _ = _run(engine, 'mana:{R/G} name="Boggart Ram-Gang"')
        assert total == 4

    def test_mana_hybrid_does_not_match_pure_color(self, engine: QueryEngine) -> None:
        # Boggart Ram-Gang has NO pure {R} pips — only {R/G}.
        # mana:{R} (contains pure red) must NOT match, even though devotion:{R} does.
        total, _ = _run(engine, 'mana:{R} name="Boggart Ram-Gang"')
        assert total == 0

    def test_mana_exact_match(self, engine: QueryEngine) -> None:
        # Lightning Bolt mana cost is exactly {R}
        total, _ = _run(engine, 'mana="{R}" name="Lightning Bolt"')
        assert total == 10

    def test_mana_exact_excludes_superset(self, engine: QueryEngine) -> None:
        # Shivan Dragon costs {4}{R}{R} — mana="{R}" must not match
        total, _ = _run(engine, 'mana="{R}" name="Shivan Dragon"')
        assert total == 0

    def test_mana_contains_2w_hybrid(self, engine: QueryEngine) -> None:
        # Spectral Procession costs {2/W}{2/W}{2/W}; mana:{2/W} matches
        total, _ = _run(engine, 'mana:{2/W} name="Spectral Procession"')
        assert total == 6

    def test_mana_2w_hybrid_does_not_match_pure_white(self, engine: QueryEngine) -> None:
        # Spectral Procession has no pure {W} pips
        total, _ = _run(engine, 'mana:{W} name="Spectral Procession"')
        assert total == 0


class TestColorIdentity:
    def test_identity_subset_green(self, engine: QueryEngine) -> None:
        # id:g = "fits in a mono-green deck" = identity ⊆ {G}
        # Tarmogoyf (11) + colorless (Black Lotus 5 + Sol Ring 5)
        total, _ = _run(engine, "id:g")
        assert total == 21

    def test_identity_subset_blue(self, engine: QueryEngine) -> None:
        # Counterspell (6) + Jace (10) + colorless (10)
        total, _ = _run(engine, "id:u")
        assert total == 26

    def test_identity_superset_matches_all(self, engine: QueryEngine) -> None:
        # Every card fits in a 5-color deck
        total, _ = _run(engine, "id:wubrg")
        assert total == 87

    def test_identity_differs_from_color(self, engine: QueryEngine) -> None:
        # Nicol Bolas (UBR) has c:r but only cards with identity ⊆ {R} fit in a mono-red deck
        # Nicol Bolas has B+U+R identity so does NOT match id:r
        total_color, _ = _run(engine, "c:r")  # includes Nicol Bolas
        total_identity, _ = _run(engine, "id:r")  # excludes Nicol Bolas
        assert total_color > total_identity


class TestRarityAndLoyalty:
    def test_rarity_common(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "r=common")
        assert total == 15

    def test_rarity_rare(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "r=rare")
        assert total == 22

    def test_rarity_mythic(self, engine: QueryEngine) -> None:
        # Jace (10) + Nicol Bolas (7) + Tarmogoyf mythic prints (8) + Kitchen Finks rare? (1) + Lightning Bolt mythic (1)
        total, _ = _run(engine, "r=mythic")
        assert total == 27

    def test_rarity_gte_rare(self, engine: QueryEngine) -> None:
        # rare (22) + mythic (27)
        total, _ = _run(engine, "r>=rare")
        assert total == 49

    def test_loyalty_exact(self, engine: QueryEngine) -> None:
        # Jace, the Mind Sculptor starts at 3 loyalty — all 10 printings
        total, cards = _run(engine, "loy=3")
        assert total == 10
        assert all(c["name"] == "Jace, the Mind Sculptor" for c in cards)

    def test_loyalty_gte(self, engine: QueryEngine) -> None:
        # Jace (loy=3, 10) + Nicol Bolas (loy=5, 7)
        total, _ = _run(engine, "loy>=3")
        assert total == 17

    def test_loyalty_nonzero(self, engine: QueryEngine) -> None:
        # Only planeswalkers have loyalty
        total, _ = _run(engine, "loy>0")
        assert total == 17


class TestKeywordsAndSubtypes:
    def test_keyword_flying(self, engine: QueryEngine) -> None:
        # Serra Angel (7) + Shivan Dragon (5)
        total, _ = _run(engine, "keyword:flying")
        assert total == 12

    def test_keyword_vigilance(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "keyword:vigilance")
        assert total == 7
        assert all(c["name"] == "Serra Angel" for c in cards)

    def test_keyword_and(self, engine: QueryEngine) -> None:
        # Cards with BOTH flying AND vigilance: only Serra Angel
        total, _ = _run(engine, "keyword:flying keyword:vigilance")
        assert total == 7

    def test_subtype_angel(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "t:angel")
        assert total == 7
        assert all(c["name"] == "Serra Angel" for c in cards)

    def test_subtype_dragon(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "t:dragon")
        assert total == 5
        assert all(c["name"] == "Shivan Dragon" for c in cards)

    def test_subtype_goblin(self, engine: QueryEngine) -> None:
        # Boggart Ram-Gang is a Goblin Warrior
        total, cards = _run(engine, "t:goblin")
        assert total == 4
        assert all(c["name"] == "Boggart Ram-Gang" for c in cards)

    def test_subtype_no_match(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "t:elf")
        assert total == 0

    def test_subtype_le_empty_collection_matches(self, engine: QueryEngine) -> None:
        # SQL: col <@ ARRAY['Dragon'] is true for an empty array (vacuously).
        # Cards with no subtypes should match t<="Dragon" on both paths.
        total_le, _ = _run(engine, 't<="Dragon"')
        total_dragon, _ = _run(engine, "t:dragon")
        # LE includes Dragon-only cards plus all cards with no subtypes (37 in fixture)
        assert total_le == total_dragon + 37


class TestLegalityAndFormats:
    def test_legal_in_legacy(self, engine: QueryEngine) -> None:
        # Black Lotus is banned in legacy — 77 cards are legal
        total, _ = _run(engine, "f:legacy")
        assert total == 77

    def test_legal_in_pauper(self, engine: QueryEngine) -> None:
        # Only commons are legal in pauper
        total, _ = _run(engine, "f:pauper")
        assert total == 21

    def test_banned_in_commander(self, engine: QueryEngine) -> None:
        # Black Lotus (5 printings) is banned in commander
        total, cards = _run(engine, "banned:commander")
        assert total == 5
        assert all(c["name"] == "Black Lotus" for c in cards)

    def test_restricted_in_vintage(self, engine: QueryEngine) -> None:
        # Black Lotus (5) + Sol Ring (5) are restricted in vintage
        total, _ = _run(engine, "restricted:vintage")
        assert total == 10


class TestCardProperties:
    def test_border_black(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "border:black")
        assert total == 70

    def test_border_borderless(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "border:borderless")
        assert total == 10

    def test_layout_normal(self, engine: QueryEngine) -> None:
        # All fixture cards are normal layout
        total, _ = _run(engine, "layout:normal")
        assert total == 87

    def test_frame_2015(self, engine: QueryEngine) -> None:
        total_2015, _ = _run(engine, "frame:2015")
        total_1993, _ = _run(engine, "frame:1993")
        assert total_2015 > 0
        assert total_1993 > 0
        assert total_2015 + total_1993 < 87  # other frames exist too

    def test_watermark_fnm(self, engine: QueryEngine) -> None:
        # Kitchen Finks f09 has FNM watermark
        total, cards = _run(engine, "watermark:fnm")
        assert total == 1
        assert cards[0]["name"] == "Kitchen Finks"

    def test_year_1993(self, engine: QueryEngine) -> None:
        # Alpha/Beta/Unlimited/CED/CEI all released in 1993
        total, _ = _run(engine, "year=1993")
        assert total == 27

    def test_date_gte_2025(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "date>=2025-01-01")
        assert total > 0

    def test_collector_number_exact(self, engine: QueryEngine) -> None:
        # Black Lotus in Alpha is collector number 232
        total, cards = _run(engine, "number=232")
        assert total == 1
        assert cards[0]["name"] == "Black Lotus"

    def test_collector_number_lte(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "cn<=10")
        assert total == 6

    def test_flavor_text_contains(self, engine: QueryEngine) -> None:
        # Shivan Dragon flavor text mentions "dragon"
        total, cards = _run(engine, "ft:dragon")
        assert total == 5
        assert all(c["name"] == "Shivan Dragon" for c in cards)


class TestPriceFilters:
    def test_usd_lt(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "usd<2")
        assert total == 17

    def test_usd_gt(self, engine: QueryEngine) -> None:
        # High-value Alpha/Beta/special prints
        total, _ = _run(engine, "usd>100")
        assert total == 13

    def test_usd_filter_excludes_null(self, engine: QueryEngine) -> None:
        # Cards with null price should not match any usd comparison
        total_lt, _ = _run(engine, "usd<999")
        total_all, _ = _run(engine)
        assert total_lt < total_all  # some cards have null price


class TestProduces:
    def test_produces_white(self, engine: QueryEngine) -> None:
        # Only Black Lotus produces white mana in our fixture
        total, cards = _run(engine, "produces:w")
        assert total == 5
        assert all(c["name"] == "Black Lotus" for c in cards)

    def test_produces_black(self, engine: QueryEngine) -> None:
        # Black Lotus (5) + Dark Ritual (5) both produce black mana
        total, _ = _run(engine, "produces:b")
        assert total == 10


class TestTags:
    """Tests for is: (card_is_tags) and otag: (card_oracle_tags).

    Tags are not populated by the Scryfall tagger in this DB; the fixture
    was patched with representative values:
      card_is_tags: {"spell": true} for instants/sorceries, {"permanent": true} for others
      card_oracle_tags: {"burn": true} for Lightning Bolt, {"counter-spell": true} for Counterspell
    """

    def test_is_spell(self, engine: QueryEngine) -> None:
        # Instants (Lightning Bolt 10 + Counterspell 6 + Dark Ritual 5)
        # + Sorceries (Spectral Procession 6)
        total, _ = _run(engine, "is:spell")
        assert total == 27

    def test_is_permanent(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "is:permanent")
        assert total == 60

    def test_is_spell_and_permanent_disjoint(self, engine: QueryEngine) -> None:
        total_spell, _ = _run(engine, "is:spell")
        total_perm, _ = _run(engine, "is:permanent")
        assert total_spell + total_perm == 87

    def test_otag_burn(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "otag:burn")
        assert total == 10
        assert all(c["name"] == "Lightning Bolt" for c in cards)

    def test_otag_counter_spell(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "otag:counter-spell")
        assert total == 6
        assert all(c["name"] == "Counterspell" for c in cards)

    def test_otag_no_match(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "otag:ramp")
        assert total == 0
