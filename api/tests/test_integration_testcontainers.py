"""Integration tests using testcontainers with real PostgreSQL database."""

from __future__ import annotations

import multiprocessing
import pathlib
import tempfile
import time
import uuid
from typing import TYPE_CHECKING

import pytest

from api.admin_resource import PRINT_COUNT_COLUMNS, AdminContext
from api.api_resource import APIResource
from api.app_context import AppContext
from api.enums import CardOrdering, ResponseShape, SortDirection, UniqueOn
from api.tests.helpers import make_raw_card, search_kwargs
from api.tests.support import override_attr
from card_engine import QueryEngine

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(scope="class")
def api_resource(postgres_container: None) -> Generator[APIResource]:
    """APIResource with schema, fixture data, and engine loaded against the session postgres."""
    schema_setup_event = multiprocessing.Event()
    api = APIResource(
        app_context=AppContext(last_import_time=multiprocessing.Value("d", time.time(), lock=True)),
        admin_context=AdminContext(schema_setup_event=schema_setup_event),
    )

    def always_true() -> bool:
        return True

    override_attr(api.app_context, "setup_complete", always_true)
    override_attr(api.admin, "_import_recent", always_true)
    api.admin.setup_schema()

    data_file = pathlib.Path(__file__).parent / "fixtures" / "test_data.sql"
    with api.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
        cursor.execute(data_file.read_text())
        conn.commit()

    api.app_context.reload_engine(force=True)
    yield api
    api.app_context.reader_pool.close()
    api.app_context.writer_pool.close()


# (query, the cards among Lightning Bolt / Serra Angel / Black Lotus BOTH lanes must answer) for
# test_count_keywords_agree_on_both_lanes, which writes the six count columns on those three.
_BOLT, _ANGEL, _LOTUS = "Lightning Bolt", "Serra Angel", "Black Lotus"
_COUNT_KEYWORD_LANE_CASES: list[tuple[str, set[str]]] = [
    ("prints=77", {_BOLT}),
    ("prints:3", {_ANGEL}),
    ("prints>=3", {_BOLT, _ANGEL}),
    ("prints<3", {_LOTUS}),
    ("prints!=1", {_BOLT, _ANGEL}),
    ("sets=46", {_BOLT}),
    ("sets=1", {_LOTUS}),
    ("paperprints=68", {_BOLT}),
    ("papersets=41", {_BOLT}),
    ("illustrations=33", {_BOLT}),
    ("illustrations>=2", {_BOLT, _ANGEL}),
    ("artists=2", {_ANGEL}),
    ("artists=1", {_BOLT}),
    # Zero is a value, not NULL: the digital-only card that credits nobody.
    ("paperprints=0", {_LOTUS}),
    ("papersets=0", {_LOTUS}),
    ("illustrations=0", {_LOTUS}),
    ("artists=0", {_LOTUS}),
    # A column on either side.
    ("prints>sets", {_BOLT}),
    ("prints=sets", {_ANGEL, _LOTUS}),
    ("prints>paperprints", {_BOLT, _ANGEL, _LOTUS}),
    ("illustrations>=prints", set()),
    ("cmc<prints", {_BOLT, _LOTUS}),
    # NULL on every card the sync has not reached: neither the comparison nor its negation.
    ("prints>=0", {_BOLT, _ANGEL, _LOTUS}),
    ("-prints>=0", set()),
    ("-artists>=0", set()),
    ("-prints=77", {_ANGEL, _LOTUS}),
]


# The set test_cheapest_agrees_on_both_lanes imports its printings into; each is named below by
# its collector number.
_CHEAPEST_SET = "zzc"

# (query, the collector numbers of _CHEAPEST_SET BOTH lanes must answer). The rows:
#   card A   1  usd 0.50 / 0.50   eur 0.40          tix 0.02
#            2  usd 2.94 / 1.59   eur 0.60 / 0.40   tix 0.05
#            3  usd  --  / 0.50   (foil-only: it enters the dollar minimum, and equals it)
#            4  unpriced
#   card B   5  usd 5.00, eur foil 3.00, in a memorabilia set -- so card B has no lowest price
#            6  unpriced
#   card C   7  priced, with the column put back to NULL: a row the sync has not reached
_CHEAPEST_LANE_CASES: list[tuple[str, set[str]]] = [
    ("cheapest:usd", {"1", "3"}),
    ("cheapest:$", {"1", "3"}),
    ("cheapest=Dollar", {"1", "3"}),
    # The negated TERM: no plain price equal to the minimum AND no foil price that is not it.
    # Printing 3 is in both lists; printing 2, priced both ways and neither the minimum, in neither.
    ("-cheapest:usd", {"3", "4", "6"}),
    # The negated GROUP: the complement -- and the NULLs (5, 7) are in neither.
    ("-(cheapest:usd)", {"2", "4", "6"}),
    ("-(-cheapest:usd)", {"1", "2"}),
    # Euros: the minimum is the plain price alone (0.40), and printing 2's foil equals it.
    ("cheapest:eur", {"1", "2"}),
    ("cheapest:€", {"1", "2"}),
    ("-cheapest:eur", {"2", "3", "4", "6"}),
    ("-(cheapest:eur)", {"3", "4", "6"}),
    ("-(-cheapest:eur)", {"1"}),
    # Tix has no foil price, so its negated term is its complement; card B has no tix at all.
    ("cheapest:tix", {"1"}),
    ("cheapest:mtgo", {"1"}),
    ("-cheapest:tix", {"2", "3", "4", "5", "6"}),
    ("-(cheapest:tix)", {"2", "3", "4", "5", "6"}),
    ("-(-cheapest:tix)", {"1"}),
    # Composed.
    ("cheapest:usd cheapest:eur", {"1"}),
    ("cheapest:usd -cheapest:usd", {"3"}),
    ("cheapest:usd or cheapest:eur", {"1", "2", "3"}),
    ("-(cheapest:usd or cheapest:tix)", {"2", "4", "6"}),
]

# The set test_new_rarity_agrees_on_both_lanes imports into, and what both lanes must answer.
# One card (oracle id A) and a second (B), every printing named by its collector number:
#   card A   1  rare      2001-01-01   the first rare                              -> new
#            2  rare      2005-01-01   a reprint                                   -> not
#            3  uncommon  2002-01-01   the first uncommon                          -> new
#            4  rare      2001-01-01   same day as 1, a variation (variation last) -> not
#            5  rare      2000-01-01   a promo-set printing, outside the rule      -> not
#   card B   6  mythic    2001-01-01   its only printing                           -> new
#            7  rare      2003-01-01   its masterpiece printing (not wot)          -> not
#   card C   8  common    2001-01-01   the sync's column put back to NULL           -> in neither
_NEW_RARITY_SET = "zzn"
_NEW_RARITY_LANE_CASES: list[tuple[str, set[str]]] = [
    ("new:rarity", {"1", "3", "6"}),
    ("NEW=Rarity", {"1", "3", "6"}),
    ('new:"rarity"', {"1", "3", "6"}),
    ("-new:rarity", {"2", "4", "5", "7"}),
    ("-(new:rarity)", {"2", "4", "5", "7"}),
    ("-(-new:rarity)", {"1", "3", "6"}),
    ("new:rarity or -new:rarity", {"1", "2", "3", "4", "5", "6", "7"}),
    ("new:rarity r:rare", {"1"}),
    ("new:rarity -r:rare", {"3", "6"}),
    ("-new:rarity r:rare", {"2", "4", "5", "7"}),
    ("new:rarity or r:rare", {"1", "2", "3", "4", "5", "6", "7"}),
]

_NEW_FLAGS_SET = "zzf"
_NEW_FLAGS_LANE_CASES: list[tuple[str, set[str]]] = [
    ("new:card", {"1", "6"}),
    ("NEW=Paper", {"1", "6"}),
    ('new:"cardboard"', {"1", "6"}),
    ("new:printed", {"1", "6"}),
    ("-new:card", {"2", "3", "4", "5", "7"}),
    ("new:frame", {"1", "2", "4", "6"}),
    ("-new:frame", {"3", "5", "7"}),
    ("new:foil", {"3", "6"}),
    ("-new:foil", {"1", "2", "4", "5", "7"}),
    ("new:nonfoil", {"1", "7"}),
    ("-new:nonfoil", {"2", "3", "4", "5", "6"}),
    ("new:art", {"1", "3"}),
    ("new:illustration", {"1", "3"}),
    ("-new:art", {"2", "4", "5", "6", "7"}),
    ("-(-new:art)", {"1", "3"}),
    ("new:art or -new:art", {"1", "2", "3", "4", "5", "6", "7"}),
    ("new:card new:foil", {"6"}),
    ("new:card -new:foil", {"1"}),
    ("new:frame -new:card", {"2", "4"}),
    ("new:art new:rarity", {"1"}),
    ("new:foil or new:nonfoil", {"1", "3", "6", "7"}),
]


class TestContainerIntegration:
    """Integration tests using testcontainers with real PostgreSQL."""

    def test_query_parsing_with_database(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test query parsing and execution against real database."""
        # Test a simple search query
        result = api_resource._search_sql(**search_kwargs("type:creature", limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        # Should find every creature in test data (Serra Angel, Boggart
        # Ram-Gang, the Artifact Creature Cathedral Membrane, and the
        # Legendary Creature Éowyn, Fearless Knight)
        cards = result["cards"]
        assert {c["name"] for c in cards} == {
            "Serra Angel",
            "Boggart Ram-Gang",
            "Cathedral Membrane",
            "Éowyn, Fearless Knight",
        }

    def test_card_search_by_name(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test searching for cards by name."""
        result = api_resource._search_sql(**search_kwargs('name:"Lightning Bolt"', limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        cards = result["cards"]
        assert len(cards) == 1

        card = cards[0]
        assert card["name"] == "Lightning Bolt"

    def test_card_search_by_name_folds_accents(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """#649: an unaccented name: query must find the accented card, and only it."""
        result = api_resource._search_sql(**search_kwargs("name:eowyn", limit=10))

        cards = result["cards"]
        assert {c["name"] for c in cards} == {"Éowyn, Fearless Knight"}

        # Exact match stays accent-sensitive: typing the accent finds it...
        exact_accented = api_resource._search_sql(**search_kwargs('!"Éowyn, Fearless Knight"', limit=10))
        assert {c["name"] for c in exact_accented["cards"]} == {"Éowyn, Fearless Knight"}

        # ...typing without the accent does not.
        exact_unaccented = api_resource._search_sql(**search_kwargs('!"Eowyn, Fearless Knight"', limit=10))
        assert exact_unaccented["cards"] == []

    def test_color_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test searching for cards by color."""
        result = api_resource._search_sql(**search_kwargs("c:red", limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        # Should find every red card (Lightning Bolt, Boggart Ram-Gang which
        # is R/G, and Fireball which is {X}{R})
        cards = result["cards"]
        assert {c["name"] for c in cards} == {"Lightning Bolt", "Boggart Ram-Gang", "Fireball"}

    def test_cmc_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test searching for cards by converted mana cost."""
        result = api_resource._search_sql(**search_kwargs("cmc=0", limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        # Should find Black Lotus (CMC 0)
        cards = result["cards"]
        assert len(cards) == 1
        assert cards[0]["name"] == "Black Lotus"

    def test_power_toughness_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test searching for creatures by power and toughness."""
        result = api_resource._search_sql(**search_kwargs("power=4 toughness=4", limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        # Should find Serra Angel (4/4 creature)
        cards = result["cards"]
        assert len(cards) == 1
        assert cards[0]["name"] == "Serra Angel"

    def test_mana_cost_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """mana: is exact-symbol containment (plus cmc) — hybrid keys don't split."""
        result = api_resource._search_sql(**search_kwargs("mana:{R}", limit=10))
        # Lightning Bolt has a pure {R} pip, as does Fireball ({X}{R}, and X
        # doesn't block containment on R); Boggart Ram-Gang only has {R/G} —
        # an opaque hybrid key that mana:{R} must not match.
        assert {c["name"] for c in result["cards"]} == {"Lightning Bolt", "Fireball"}

    def test_mana_cost_x_symbol(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """X is its own pip symbol (not a hybrid); bare x behaves like braced {X}."""
        braced = api_resource._search_sql(**search_kwargs("mana:{X}", limit=10))
        bare = api_resource._search_sql(**search_kwargs("mana:x", limit=10))
        assert {c["name"] for c in braced["cards"]} == {c["name"] for c in bare["cards"]} == {"Fireball"}
        # X contributes 0 to cmc: mana:{X}{R}{R} implies cmc 2, but Fireball's
        # actual cmc is 1 — must not match.
        too_high = api_resource._search_sql(**search_kwargs("mana:{X}{R}{R}", limit=10))
        assert too_high["cards"] == []

    def test_mana_cost_exact_match(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """mana= requires the exact same distinct symbols, counts, and cmc."""
        result = api_resource._search_sql(**search_kwargs('mana="{R}"', limit=10))
        assert {c["name"] for c in result["cards"]} == {"Lightning Bolt"}

    def test_devotion_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """devotion: counts a permanent's colored pips, hybrids (incl. Phyrexian) split."""
        result = api_resource._search_sql(**search_kwargs("devotion:{W}", limit=10))
        # Serra Angel {W}{W}, Cathedral Membrane {1}{W/P} (Phyrexian W counts
        # toward W devotion, same as a plain hybrid would), and Éowyn,
        # Fearless Knight {1}{W}{W}
        assert {c["name"] for c in result["cards"]} == {"Serra Angel", "Cathedral Membrane", "Éowyn, Fearless Knight"}

    def test_devotion_hybrid_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """A color/color hybrid contributes to BOTH colors' devotion."""
        red = api_resource._search_sql(**search_kwargs("devotion:{R}", limit=10))
        green = api_resource._search_sql(**search_kwargs("devotion:{G}", limit=10))
        assert {c["name"] for c in red["cards"]} == {"Boggart Ram-Gang"}
        assert {c["name"] for c in green["cards"]} == {"Boggart Ram-Gang"}

    def test_devotion_permanent_only(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Devotion only counts permanents — an Instant's colored pips never count.

        Confirmed against the real Scryfall API: devotion:r never matches the
        real Lightning Bolt, despite its {R} mana cost.
        """
        result = api_resource._search_sql(**search_kwargs("devotion:{R}", limit=10))
        assert "Lightning Bolt" not in {c["name"] for c in result["cards"]}

    def test_search_sql_default_fields(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Omitting fields= keeps the historical 9-key shape."""
        result = api_resource._search_sql(**search_kwargs("name:bolt", limit=10))
        assert result["cards"][0].keys() == {
            "name",
            "set_code",
            "collector_number",
            "power",
            "toughness",
            "mana_cost",
            "oracle_text",
            "set_name",
            "type_line",
        }

    def test_search_sql_with_custom_fields(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """fields= selects exactly the requested columns, including the newly-added ones."""
        result = api_resource._search_sql(
            **search_kwargs("name:bolt", limit=10),
            fields=["name", "illustration_id", "price_usd", "prefer_score"],
        )
        cards = result["cards"]
        assert len(cards) == 1
        assert cards[0].keys() == {"name", "illustration_id", "price_usd", "prefer_score"}
        assert cards[0]["name"] == "Lightning Bolt"

    def test_database_operations_isolation(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test that database operations are properly isolated."""
        # This test verifies that we're working with the test database
        # and not affecting the main application database

        # Count cards in test database using a query that matches all cards
        result = api_resource._search_sql(**search_kwargs("cmc>=0", limit=100))

        # Should only have our test cards
        cards = result["cards"]
        assert len(cards) == 7
        card_names = {card["name"] for card in cards}
        expected_names = {
            "Lightning Bolt",
            "Serra Angel",
            "Black Lotus",
            "Boggart Ram-Gang",
            "Cathedral Membrane",
            "Fireball",
            "Éowyn, Fearless Knight",
        }
        assert card_names == expected_names

    def test_random_search_shape_matches_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test that random_search cards have the same keys as search result cards."""
        random_result = api_resource.random_search(num_cards=1)
        assert "cards" in random_result
        assert len(random_result["cards"]) >= 1
        random_card_keys = set(random_result["cards"][0].keys())

        search_result = api_resource._search_sql(**search_kwargs("cmc>=0", limit=1))
        assert len(search_result["cards"]) >= 1
        search_card_keys = set(search_result["cards"][0].keys())

        assert random_card_keys == search_card_keys, (
            f"random_search card keys {random_card_keys} != search card keys {search_card_keys}"
        )

    def test_search_columnar_shape_inverts_to_row_shape(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """shape=columnar returns per-field lists that invert back to the row-shaped cards."""
        row_result = api_resource.search(q="cmc>=0", limit=10)
        columnar_result = api_resource.search(q="cmc>=0", limit=10, shape=ResponseShape.COLUMNAR)

        rows = row_result["cards"]
        cols = columnar_result["cards"]
        assert len(rows) >= 1
        assert isinstance(cols, dict)
        assert set(cols) == set(rows[0])
        rebuilt = [dict(zip(cols, values, strict=True)) for values in zip(*cols.values(), strict=True)]
        assert rebuilt == rows
        # Envelope stays row-agnostic
        assert columnar_result["total_cards"] == row_result["total_cards"]

    def test_get_pid(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test basic API functionality with real database."""
        pid = api_resource.get_pid()
        assert isinstance(pid, int)
        assert pid > 0

    def test_import_card_by_name_integration(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test importing a card by name using the real Scryfall API and database."""
        card_name = "Beast Within"

        # Import the card using the import_card_by_name method
        import_result = api_resource.admin.import_card_by_name(card_name=card_name)

        # Check that the import was successful
        assert import_result["status"] == "success"
        assert import_result["cards_loaded"] >= 35

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM magic.cards WHERE card_name = %s", (card_name,))
            count_result = cursor.fetchone()
            card_count = count_result["count"] if count_result else 0
            assert card_count >= 1, f"Card '{card_name}' should exist in database after import (count: {card_count})"

        # Now test that we can search for it by name
        search_result = api_resource.search(q=f"name:{card_name}", limit=10)
        found_cards = search_result["cards"]

        assert len(found_cards) >= 1, f"Card '{card_name}' should be findable after import"

        # Find the exact match
        imported_card = found_cards[0]

        # Verify key properties of the imported card
        assert imported_card["name"] == card_name

        # Check that it has mana cost information (this should be present from Scryfall data)
        assert "mana_cost" in imported_card, "Card should have mana cost information"
        assert imported_card["mana_cost"] == "{2}{G}", f"Beast Within should cost {{2}}{{G}}, got: {imported_card.get('mana_cost')}"

    def test_import_card_and_search_by_set(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test importing a card from Scryfall and then searching by set code to verify set is populated."""
        # Choose a card that shouldn't already exist in the test database
        card_name = "Mox Ruby"  # A well-known card from Alpha/Beta

        # Import the card using the import_card_by_name method
        import_result = api_resource.admin.import_card_by_name(card_name=card_name)

        # Check that the import was successful (or already exists, which is also fine for this test)
        assert import_result["status"] in ["success", "already_exists"], f"Import failed: {import_result}"

        if import_result["status"] == "success":
            assert import_result["cards_loaded"] == 3, f"Expected 3 cards loaded, got {import_result['cards_loaded']}"

        # Verify the card exists in database and has set information
        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT card_name, card_set_code FROM magic.cards WHERE card_name = %s",
                (card_name,),
            )
            result = cursor.fetchone()
            assert result is not None, f"Card '{card_name}' should exist in database"

            # Check that the set code was populated
            db_set_code = result["card_set_code"]
            assert db_set_code is not None, f"Set code should be populated for '{card_name}'"
            assert len(db_set_code) >= 3, f"Set code should be at least 3 characters, got '{db_set_code}'"

            # Store the actual set code for searching
            actual_set_code = db_set_code

        # Now test that we can search for the card using set search
        set_search_result = api_resource.search(q=f"set:{actual_set_code}", limit=100)
        found_cards = set_search_result["cards"]

        assert len(found_cards) >= 1, f"Should find at least one card with set:{actual_set_code}"

        # Find the imported card in the results
        imported_card_found = False
        for card in found_cards:
            if card["name"] == card_name:
                imported_card_found = True
                break

        assert imported_card_found, f"Card '{card_name}' should be findable by set search 'set:{actual_set_code}'"

        # Also test the shorthand 's:' syntax
        shorthand_search_result = api_resource.search(q=f"s:{actual_set_code}", limit=100)
        shorthand_found_cards = shorthand_search_result["cards"]

        assert len(shorthand_found_cards) >= 1, f"Should find at least one card with s:{actual_set_code}"

        # Find the imported card in the shorthand results
        shorthand_card_found = False
        for card in shorthand_found_cards:
            if card["name"] == card_name:
                shorthand_card_found = True
                break

        assert shorthand_card_found, f"Card '{card_name}' should be findable by shorthand set search 's:{actual_set_code}'"

        # Verify both searches return the same results
        found_names = {card["name"] for card in found_cards}
        shorthand_names = {card["name"] for card in shorthand_found_cards}
        assert found_names == shorthand_names, "set: and s: searches should return identical results"

    def test_artist_search_integration(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test end-to-end artist search functionality with real database."""
        # Import Brainstorm card which has "Willian Murai" as artist
        import_result = api_resource.admin.import_card_by_name(card_name="Brainstorm")

        # Check if import was successful
        if import_result.get("status") != "success":
            pytest.skip(f"Card import failed: {import_result.get('message', 'Unknown error')}")

        # Test artist search by full name
        result = api_resource.search(q='artist:"Willian Murai"')
        cards = result["cards"]
        assert len(cards) >= 1, "Should find at least one card by Willian Murai"

        # Find Brainstorm specifically and verify artist field
        brainstorm_found = False
        for card in cards:
            if card["name"] == "Brainstorm":
                brainstorm_found = True
                break

        assert brainstorm_found, "Brainstorm should be found by artist search"

        # Test artist search by partial name (case insensitive)
        result_partial = api_resource.search(q="artist:murai")
        cards_partial = result_partial["cards"]
        assert len(cards_partial) >= 1, "Should find cards by partial artist name search"

        # Verify Brainstorm is found in partial search
        brainstorm_in_partial = any(card["name"] == "Brainstorm" for card in cards_partial)
        assert brainstorm_in_partial, "Brainstorm should be found by partial artist search"

        # Test shorthand artist search
        result_shorthand = api_resource.search(q="a:murai")
        cards_shorthand = result_shorthand["cards"]
        assert len(cards_shorthand) >= 1, "Should find cards using shorthand 'a:' for artist"

        # Verify Brainstorm is found in shorthand search
        brainstorm_in_shorthand = any(card["name"] == "Brainstorm" for card in cards_shorthand)
        assert brainstorm_in_shorthand, "Brainstorm should be found by shorthand artist search"

        # Test combined artist search with other attributes (Brainstorm has cmc=1)
        result_combined = api_resource.search(q="cmc=1 artist:murai")
        cards_combined = result_combined["cards"]
        assert len(cards_combined) >= 1, "Should find cards matching both CMC and artist criteria"

        # Verify Brainstorm is found in combined search and matches both criteria
        brainstorm_in_combined = False
        for card in cards_combined:
            if card["name"] == "Brainstorm":
                brainstorm_in_combined = True
                break

        assert brainstorm_in_combined, "Brainstorm should be found by combined search"

    @pytest.mark.usefixtures("engine_enabled")
    def test_cubecobra_ordering(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test that orderby=cubecobra sorts by cubecobra_score ascending (lower = better)."""
        # Assign distinct cubecobra_score values to three known cards
        scores = {
            "Lightning Bolt": 10.0,
            "Black Lotus": 50.0,
            "Serra Angel": 90.0,
        }
        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            for name, score in scores.items():
                cursor.execute(
                    "UPDATE magic.cards SET cubecobra_score = %s WHERE card_name = %s",
                    (score, name),
                )
            conn.commit()

        # The default archive path is shared machine-wide, so another process's
        # store would shadow this test DB's data: swap in a private store for
        # this test (the api_resource fixture is class-scoped, so restore it).
        shm_path = pathlib.Path(tempfile.gettempdir()) / f"sylvan_librarian_it_{uuid.uuid4().hex}"
        saved_engine = api_resource.app_context.engine
        api_resource.app_context.engine = QueryEngine(shm_path=str(shm_path))
        try:
            # Reload the engine so it picks up the direct DB update
            api_resource.app_context.reload_engine(force=True)

            result = api_resource._search_engine(
                **search_kwargs("cmc>=0", limit=100, orderby=CardOrdering.CUBECOBRA, direction=SortDirection.ASC)
            )
            names = [card["name"] for card in result["cards"] if card["name"] in scores]
            assert names == ["Lightning Bolt", "Black Lotus", "Serra Angel"]

            result = api_resource._search_engine(
                **search_kwargs("cmc>=0", limit=100, orderby=CardOrdering.CUBECOBRA, direction=SortDirection.DESC)
            )
            names = [card["name"] for card in result["cards"] if card["name"] in scores]
            assert names == ["Serra Angel", "Black Lotus", "Lightning Bolt"]
        finally:
            api_resource.app_context.engine = saved_engine
            shm_path.unlink(missing_ok=True)
            shm_path.with_suffix(".lock").unlink(missing_ok=True)

    @pytest.mark.usefixtures("engine_enabled")
    def test_count_keywords_agree_on_both_lanes(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """`prints`, `sets`, `paperprints`, `papersets`, `illustrations` and `artists` answer alike in SQL and the engine.

        Both lanes read the same six columns, written here as `_sync_print_counts` writes them, so
        what this checks is the plumbing a unit test cannot: that the migration added the columns,
        that ENGINE_COLUMNS selects them and the loader reads them, and that NULL (a card the sync
        has not reached -- every other row of the fixture data) behaves the same on both sides.
        """
        counts = {
            # Each tuple is prints, sets, paperprints, papersets, illustrations, artists.
            _BOLT: (77, 46, 68, 41, 33, 1),
            _ANGEL: (3, 3, 2, 2, 2, 2),
            # A digital-only card that credits no artist: zero is a value.
            _LOTUS: (1, 1, 0, 0, 0, 0),
        }
        known = set(counts)
        assignment = ", ".join(f"{column} = %s" for column in PRINT_COUNT_COLUMNS)

        def write(values: dict[str, tuple]) -> None:
            with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
                for name, numbers in values.items():
                    cursor.execute(f"UPDATE magic.cards SET {assignment} WHERE card_name = %s", (*numbers, name))
                conn.commit()

        # Private store for the same reason test_cubecobra_ordering swaps one in.
        shm_path = pathlib.Path(tempfile.gettempdir()) / f"sylvan_librarian_it_{uuid.uuid4().hex}"
        saved_engine = api_resource.app_context.engine
        write(counts)
        api_resource.app_context.engine = QueryEngine(shm_path=str(shm_path))
        try:
            api_resource.app_context.reload_engine(force=True)

            for query, expected in _COUNT_KEYWORD_LANE_CASES:
                sql = {card["name"] for card in api_resource._search_sql(**search_kwargs(query, limit=100))["cards"]}
                engine = {card["name"] for card in api_resource._search_engine(**search_kwargs(query, limit=100))["cards"]}
                assert sql == engine, query
                assert sql & known == expected, query
        finally:
            write(dict.fromkeys(counts, (None,) * len(PRINT_COUNT_COLUMNS)))
            api_resource.app_context.engine = saved_engine
            shm_path.unlink(missing_ok=True)
            shm_path.with_suffix(".lock").unlink(missing_ok=True)

    @pytest.mark.usefixtures("engine_enabled")
    def test_cheapest_agrees_on_both_lanes(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """`cheapest:usd` / `:eur` / `:tix`, their negated terms and negated groups answer alike in SQL and the engine.

        End to end: the printings go through the importer, whose sync decides `cheapest_codes`;
        the engine is loaded from the table; and both lanes are asked the same questions, per
        printing. That covers what the unit tests cannot -- that the migration added the column,
        that ENGINE_COLUMNS selects it and the loader reads it, and that both kinds of NULL (the
        unknown bit, and a column the sync has not written) come out the same on both sides.
        """
        card_a, card_b, card_c = (str(uuid.uuid4()) for _ in range(3))

        def printing(oracle_id: str, number: str, prices: dict[str, str], set_type: str = "expansion") -> dict:
            card = make_raw_card(name=f"Cheapest Lane {oracle_id[:8]}")
            return card | {
                "oracle_id": oracle_id,
                "set": _CHEAPEST_SET,
                "collector_number": number,
                "set_type": set_type,
                "prices": prices,
            }

        printings = [
            printing(card_a, "1", {"usd": "0.50", "usd_foil": "0.50", "eur": "0.40", "tix": "0.02"}),
            printing(card_a, "2", {"usd": "2.94", "usd_foil": "1.59", "eur": "0.60", "eur_foil": "0.40", "tix": "0.05"}),
            printing(card_a, "3", {"usd_foil": "0.50"}),
            printing(card_a, "4", {}),
            printing(card_b, "5", {"usd": "5.00", "eur_foil": "3.00"}, set_type="memorabilia"),
            printing(card_b, "6", {}),
            printing(card_c, "7", {"usd": "1.00", "eur": "1.00", "tix": "1.00"}),
        ]
        numbers = {card["collector_number"] for card in printings}

        def answer(search: object, query: str, unique: UniqueOn) -> list[dict]:
            return search(**(search_kwargs(query, limit=1000) | {"unique": unique}))["cards"]

        # Private store for the same reason test_cubecobra_ordering swaps one in.
        shm_path = pathlib.Path(tempfile.gettempdir()) / f"sylvan_librarian_it_{uuid.uuid4().hex}"
        saved_engine = api_resource.app_context.engine
        api_resource.app_context.engine = QueryEngine(shm_path=str(shm_path))
        try:
            api_resource.admin._upsert_cards(printings)
            with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("UPDATE magic.cards SET cheapest_codes = NULL WHERE oracle_id = %s", (card_c,))
                conn.commit()
            api_resource.app_context.reload_engine(force=True)

            for query, expected in _CHEAPEST_LANE_CASES:
                in_set = f"e:{_CHEAPEST_SET} ({query})"
                sql = {card["collector_number"] for card in answer(api_resource._search_sql, in_set, UniqueOn.PRINTING)}
                engine = {card["collector_number"] for card in answer(api_resource._search_engine, in_set, UniqueOn.PRINTING)}
                assert sql == engine, query
                assert sql == expected, query
                assert sql <= numbers
                # And on rows this test did not shape: the three fixture cards, which the sync
                # reached with whatever prices their blobs hold, grouped by card. Scoped by name
                # because the session database also holds every other test file's cards.
                fixture_cards = f'({query}) (name:"lightning bolt" or name:"serra angel" or name:"black lotus")'
                sql_cards = {card["name"] for card in answer(api_resource._search_sql, fixture_cards, UniqueOn.CARD)}
                engine_cards = {card["name"] for card in answer(api_resource._search_engine, fixture_cards, UniqueOn.CARD)}
                assert sql_cards == engine_cards, query
        finally:
            with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("DELETE FROM magic.cards WHERE card_set_code = %s", (_CHEAPEST_SET,))
                conn.commit()
            api_resource.app_context.engine = saved_engine
            shm_path.unlink(missing_ok=True)
            shm_path.with_suffix(".lock").unlink(missing_ok=True)

    @pytest.mark.usefixtures("engine_enabled")
    def test_new_rarity_agrees_on_both_lanes(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """`new:rarity` and its negation answer alike in SQL and the engine.

        End to end: the printings go through the importer, whose sync decides `new_rarity`; the
        engine is loaded from the table; and both lanes are asked the same questions, per
        printing. That covers what the unit tests cannot -- that the migration added the column,
        that ENGINE_COLUMNS selects it and the loader reads it, and that a column the sync has not
        written (NULL) comes out the same on both sides.
        """
        card_a, card_b, card_c = (str(uuid.uuid4()) for _ in range(3))

        def printing(oracle_id: str, number: str, rarity: str, released_at: str, **extra: object) -> dict:
            card = make_raw_card(name=f"New Rarity Lane {oracle_id[:8]}", rarity=rarity)
            return (
                card
                | {
                    "oracle_id": oracle_id,
                    "set": _NEW_RARITY_SET,
                    "collector_number": number,
                    "released_at": released_at,
                    "set_type": "expansion",
                }
                | extra
            )

        printings = [
            printing(card_a, "1", "rare", "2001-01-01"),
            printing(card_a, "2", "rare", "2005-01-01"),
            printing(card_a, "3", "uncommon", "2002-01-01"),
            printing(card_a, "4", "rare", "2001-01-01", variation=True),
            printing(card_a, "5", "rare", "2000-01-01", set_type="promo"),
            printing(card_b, "6", "mythic", "2001-01-01"),
            printing(card_b, "7", "rare", "2003-01-01", set_type="masterpiece"),
            printing(card_c, "8", "common", "2001-01-01"),
        ]
        numbers = {card["collector_number"] for card in printings}

        def answer(search: object, query: str) -> set[str]:
            cards = search(**(search_kwargs(query, limit=1000) | {"unique": UniqueOn.PRINTING}))["cards"]
            return {card["collector_number"] for card in cards}

        def names(search: object, query: str) -> set[str]:
            return {card["name"] for card in search(**(search_kwargs(query, limit=1000) | {"unique": UniqueOn.CARD}))["cards"]}

        # Private store for the same reason test_cubecobra_ordering swaps one in.
        shm_path = pathlib.Path(tempfile.gettempdir()) / f"sylvan_librarian_it_{uuid.uuid4().hex}"
        saved_engine = api_resource.app_context.engine
        api_resource.app_context.engine = QueryEngine(shm_path=str(shm_path))
        try:
            api_resource.admin._upsert_cards(printings)
            with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("UPDATE magic.cards SET new_rarity = NULL WHERE oracle_id = %s", (card_c,))
                conn.commit()
            api_resource.app_context.reload_engine(force=True)

            for query, expected in _NEW_RARITY_LANE_CASES:
                in_set = f"e:{_NEW_RARITY_SET} ({query})"
                sql = answer(api_resource._search_sql, in_set)
                engine = answer(api_resource._search_engine, in_set)
                assert sql == engine, query
                assert sql == expected, query
                assert sql <= numbers
                # And on rows this test did not shape: the three fixture cards, which the sync
                # reached with whatever their blobs hold, grouped by card. Scoped by name because
                # the session database also holds every other test file's cards.
                fixture_cards = f'({query}) (name:"lightning bolt" or name:"serra angel" or name:"black lotus")'
                sql_cards = names(api_resource._search_sql, fixture_cards)
                engine_cards = names(api_resource._search_engine, fixture_cards)
                assert sql_cards == engine_cards, query
        finally:
            with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("DELETE FROM magic.cards WHERE card_set_code = %s", (_NEW_RARITY_SET,))
                conn.commit()
            api_resource.app_context.engine = saved_engine
            shm_path.unlink(missing_ok=True)
            shm_path.with_suffix(".lock").unlink(missing_ok=True)

    def test_new_flags_agree_on_both_lanes(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """`new:card`, `new:frame`, `new:foil`, `new:nonfoil`, `new:art` and their negations answer alike in SQL and the engine.

        End to end: the printings go through the importer, whose sync decides `new_flags`; the
        engine is loaded from the table; and both lanes are asked the same questions, per
        printing. That covers what the unit tests cannot -- that the migration added the column,
        that ENGINE_COLUMNS selects it and the loader reads it, that the bit the Python node sends
        is the bit the sync wrote, and that a column the sync has not written (NULL) comes out the
        same on both sides.
        """
        card_a, card_b, card_c = (str(uuid.uuid4()) for _ in range(3))
        art_one, art_two = str(uuid.uuid4()), str(uuid.uuid4())

        def printing(oracle_id: str, number: str, released_at: str, art: str, **extra: object) -> dict:
            card = make_raw_card(name=f"New Flags Lane {oracle_id[:8]}")
            return (
                card
                | {
                    "oracle_id": oracle_id,
                    "set": _NEW_FLAGS_SET,
                    "collector_number": number,
                    "released_at": released_at,
                    "set_type": "expansion",
                    "frame": "1997",
                    "finishes": ["nonfoil"],
                    "illustration_id": art,
                }
                | extra
            )

        printings = [
            # card A: its first printing; a new frame; its first foil in a new artwork; a variation
            # that is the first in a frame of its own; a memorabilia printing earlier than all of
            # them.
            printing(card_a, "1", "2001-01-01", art_one),
            printing(card_a, "2", "2005-01-01", art_one, frame="2003"),
            printing(card_a, "3", "2006-01-01", art_two, frame="2003", finishes=["foil"]),
            printing(card_a, "4", "2007-01-01", art_two, frame="2015", variation=True),
            printing(card_a, "5", "2000-01-01", art_one, set_type="memorabilia"),
            # card B: a foil-only first printing in card A's artwork, then its first nonfoil.
            printing(card_b, "6", "2002-01-01", art_one, finishes=["foil"]),
            printing(card_b, "7", "2003-01-01", art_one),
            # card C: the sync's answer is blanked below, so it is in no list and no complement.
            printing(card_c, "8", "2001-01-01", str(uuid.uuid4())),
        ]
        numbers = {card["collector_number"] for card in printings}

        def answer(search: object, query: str) -> set[str]:
            cards = search(**(search_kwargs(query, limit=1000) | {"unique": UniqueOn.PRINTING}))["cards"]
            return {card["collector_number"] for card in cards}

        def names(search: object, query: str) -> set[str]:
            return {card["name"] for card in search(**(search_kwargs(query, limit=1000) | {"unique": UniqueOn.CARD}))["cards"]}

        # Private store for the same reason test_cubecobra_ordering swaps one in.
        shm_path = pathlib.Path(tempfile.gettempdir()) / f"sylvan_librarian_it_{uuid.uuid4().hex}"
        saved_engine = api_resource.app_context.engine
        api_resource.app_context.engine = QueryEngine(shm_path=str(shm_path))
        try:
            api_resource.admin._upsert_cards(printings)
            with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("UPDATE magic.cards SET new_flags = NULL WHERE oracle_id = %s", (card_c,))
                conn.commit()
            api_resource.app_context.reload_engine(force=True)

            for query, expected in _NEW_FLAGS_LANE_CASES:
                in_set = f"e:{_NEW_FLAGS_SET} ({query})"
                sql = answer(api_resource._search_sql, in_set)
                engine = answer(api_resource._search_engine, in_set)
                assert sql == engine, query
                assert sql == expected, query
                assert sql <= numbers
                # And on rows this test did not shape: the three fixture cards, which the sync
                # reached with whatever their blobs hold, grouped by card. Scoped by name because
                # the session database also holds every other test file's cards.
                fixture_cards = f'({query}) (name:"lightning bolt" or name:"serra angel" or name:"black lotus")'
                sql_cards = names(api_resource._search_sql, fixture_cards)
                engine_cards = names(api_resource._search_engine, fixture_cards)
                assert sql_cards == engine_cards, query
        finally:
            with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("DELETE FROM magic.cards WHERE card_set_code = %s", (_NEW_FLAGS_SET,))
                conn.commit()
            api_resource.app_context.engine = saved_engine
            shm_path.unlink(missing_ok=True)
            shm_path.with_suffix(".lock").unlink(missing_ok=True)
