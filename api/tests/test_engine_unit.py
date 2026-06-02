"""Unit tests for the Rust QueryEngine — filters, dedup, prefer, and sort.

Fixture: api/tests/fixtures/engine_cards.json
  71 real card printings across 10 oracle IDs and 32 illustration IDs.
  Chosen to exercise shared artworks, null prices, multi-color, and
  varied CMC / type / rarity distributions.

Card summary (name → printings, illustrations):
  Black Lotus       5p  1i   colorless artifact    cmc=0
  Counterspell      6p  4i   blue instant          cmc=2
  Dark Ritual       5p  1i   black instant         cmc=1
  Jace, the Mind Sculptor 10p 5i  blue planeswalker cmc=4
  Lightning Bolt   10p  6i   red instant           cmc=1
  Nicol Bolas, Planeswalker 7p 5i  UBR planeswalker cmc=8
  Serra Angel       7p  4i   white creature 4/4    cmc=5
  Shivan Dragon     5p  1i   red creature 5/5      cmc=6
  Sol Ring          5p  1i   colorless artifact    cmc=1
  Tarmogoyf        11p  4i   green creature */*+1  cmc=2
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.parsing import parse_scryfall_query
from card_engine import QueryEngine

_FIXTURE = Path(__file__).parent / "fixtures" / "engine_cards.json"


@pytest.fixture(scope="module")
def engine() -> QueryEngine:
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
        assert total == 71
        assert len(cards) == 71

    def test_name_exact(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, 'name="Lightning Bolt"')
        assert total == 10
        assert all(c["name"] == "Lightning Bolt" for c in cards)

    def test_name_contains(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, "name:bolt")
        assert total == 10

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
        # Lightning Bolt (10) + Shivan Dragon (5) + Nicol Bolas (7)
        total, _ = _run(engine, "c:r")
        assert total == 22

    def test_color_white(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "c:w")
        assert total == 7
        assert all(c["name"] == "Serra Angel" for c in cards)

    def test_color_blue(self, engine: QueryEngine) -> None:
        # Counterspell (6) + Jace (10) + Nicol Bolas (7)
        total, _ = _run(engine, "c:u")
        assert total == 23

    def test_color_black(self, engine: QueryEngine) -> None:
        # Dark Ritual (5) + Nicol Bolas (7)
        total, _ = _run(engine, "c:b")
        assert total == 12

    def test_color_green(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "c:g")
        assert total == 11
        assert all(c["name"] == "Tarmogoyf" for c in cards)

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
        # Jace (cmc=4, 10) + Serra Angel (cmc=5, 7) + Shivan Dragon (cmc=6, 5) + Nicol Bolas (cmc=8, 7)
        total, _ = _run(engine, "cmc>=4")
        assert total == 29

    def test_type_instant(self, engine: QueryEngine) -> None:
        # Lightning Bolt (10) + Counterspell (6) + Dark Ritual (5)
        total, _ = _run(engine, "t:instant")
        assert total == 21

    def test_type_creature(self, engine: QueryEngine) -> None:
        # Serra Angel (7) + Tarmogoyf (11) + Shivan Dragon (5)
        total, _ = _run(engine, "t:creature")
        assert total == 23

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
        # Serra Angel (7) + Shivan Dragon (5)
        total, _ = _run(engine, "o:flying")
        assert total == 12

    def test_set_filter(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, "s:lea")
        assert total == 7
        assert all(c["set_code"] == "lea" for c in cards)

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
        assert total == 16  # Tarmogoyf (11) + Shivan Dragon (5)
        assert all(c["name"] != "Serra Angel" for c in cards)


class TestUnique:
    def test_unique_printing_returns_all(self, engine: QueryEngine) -> None:
        total, _ = _run(engine, unique="printing")
        assert total == 71

    def test_unique_card_deduplicates_by_oracle_id(self, engine: QueryEngine) -> None:
        total, cards = _run(engine, unique="card")
        assert total == 10
        assert len({c["name"] for c in cards}) == 10

    def test_unique_artwork_deduplicates_by_illustration(self, engine: QueryEngine) -> None:
        # 32 distinct illustration_ids across the 71 fixture printings
        total, _ = _run(engine, unique="artwork")
        assert total == 32

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
        assert total == 10
        assert len({c["name"] for c in cards}) == 10


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
        assert total == 71   # total reflects full match count
        assert len(cards) == 5

    def test_sort_direction_desc_reverses_order(self, engine: QueryEngine) -> None:
        _, asc_cards = _run(engine, 'name="Lightning Bolt"', orderby="edhrec", direction="asc")
        _, desc_cards = _run(engine, 'name="Lightning Bolt"', orderby="edhrec", direction="desc")
        # Reversed direction should produce reversed order (10 distinct printings)
        assert _names(asc_cards) == list(reversed(_names(desc_cards)))
