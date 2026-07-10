"""Integration tests using testcontainers with real PostgreSQL database."""

from __future__ import annotations

import multiprocessing
import os
import pathlib
import tempfile
import time
import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest
from testcontainers.postgres import PostgresContainer

from api.api_resource import APIResource
from api.enums import CardOrdering, ResponseShape, SortDirection
from api.tests.helpers import search_kwargs
from card_engine import QueryEngine

if TYPE_CHECKING:
    from collections.abc import Generator


class TestContainerIntegration:
    """Integration tests using testcontainers with real PostgreSQL."""

    @pytest.fixture(scope="class")
    def postgres_container(self: TestContainerIntegration) -> Generator[PostgresContainer]:
        """Create and manage PostgreSQL test container."""
        container = PostgresContainer(
            image="postgres:18",
            username="testuser",
            password="testpass",  # noqa: S106
            dbname="testdb",
        ).with_bind_ports(
            5432,
            5433,
        )  # Bind internal 5432 to host 5433

        with container as postgres:
            # Wait for database to be ready with proper health check
            self._wait_for_database_ready(postgres)
            yield postgres

    def _wait_for_database_ready(self: TestContainerIntegration, postgres_container: PostgresContainer, timeout: int = 30) -> None:
        """Wait for the database to be ready by running a simple query."""
        host = postgres_container.get_container_host_ip()
        port = postgres_container.get_exposed_port(5432)  # This should return 5433 due to bind_ports

        connection_params = {
            "host": host,
            "port": port,
            "dbname": "testdb",
            "user": "testuser",
            "password": "testpass",
        }

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with psycopg.connect(**connection_params) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        cursor.fetchone()
                    return  # Database is ready
            except (psycopg.Error, OSError):  # Catch specific database and connection errors
                time.sleep(0.5)  # Wait before retrying
                continue

        msg = f"Database not ready within {timeout} seconds"
        raise RuntimeError(msg)

    @pytest.fixture(scope="class")
    def test_db_environment(self: TestContainerIntegration, postgres_container: PostgresContainer) -> Generator[None]:
        """Set up and restore environment variables for test database connection."""
        # Store original environment variables
        original_env = {key: os.environ.get(key) for key in ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"]}

        try:
            # Set environment variables for test database
            host = postgres_container.get_container_host_ip()
            port = postgres_container.get_exposed_port(5432)

            os.environ.update(
                {
                    "PGHOST": host,
                    "PGPORT": str(port),
                    "PGDATABASE": "testdb",
                    "PGUSER": "testuser",
                    "PGPASSWORD": "testpass",
                },
            )

            yield  # Test runs here with environment configured

        finally:
            # Restore original environment variables
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @pytest.fixture(scope="class")
    def api_resource(self: TestContainerIntegration, test_db_environment: None) -> Generator[APIResource]:  # noqa: ARG002
        """Create APIResource instance, set up database schema and test data, then yield the configured instance."""
        # Create APIResource instance
        schema_setup_event = multiprocessing.Event()
        api = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
            schema_setup_event=schema_setup_event,
        )

        def always_true() -> bool:
            return True

        api._setup_complete = always_true
        api._import_recent = always_true

        # Set up the schema using real migrations
        api.setup_schema()

        # Load test data
        test_dir = pathlib.Path(__file__).parent
        data_file = test_dir / "fixtures" / "test_data.sql"

        with api._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(data_file.read_text())
            conn.commit()

        api._reload_engine(force=True)

        # Yield the fully configured APIResource for tests to use
        yield api

        # Clean up connection pool
        if hasattr(api, "_conn_pool"):
            api._conn_pool.close()

    def test_query_parsing_with_database(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test query parsing and execution against real database."""
        # Test a simple search query
        result = api_resource._search_sql(**search_kwargs("type:creature", limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        # Should find every creature in test data (Serra Angel, Boggart
        # Ram-Gang, and the Artifact Creature Cathedral Membrane)
        cards = result["cards"]
        assert {c["name"] for c in cards} == {"Serra Angel", "Boggart Ram-Gang", "Cathedral Membrane"}

    def test_card_search_by_name(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test searching for cards by name."""
        result = api_resource._search_sql(**search_kwargs('name:"Lightning Bolt"', limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        cards = result["cards"]
        assert len(cards) == 1

        card = cards[0]
        assert card["name"] == "Lightning Bolt"

    def test_color_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """Test searching for cards by color."""
        result = api_resource._search_sql(**search_kwargs("c:red", limit=10))

        assert isinstance(result, dict)
        assert "cards" in result

        # Should find every red card (Lightning Bolt, and Boggart Ram-Gang
        # which is R/G)
        cards = result["cards"]
        assert {c["name"] for c in cards} == {"Lightning Bolt", "Boggart Ram-Gang"}

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
        # Lightning Bolt has a pure {R} pip; Boggart Ram-Gang only has {R/G} —
        # an opaque hybrid key that mana:{R} must not match.
        assert {c["name"] for c in result["cards"]} == {"Lightning Bolt"}

    def test_mana_cost_exact_match(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """mana= requires the exact same distinct symbols, counts, and cmc."""
        result = api_resource._search_sql(**search_kwargs('mana="{R}"', limit=10))
        assert {c["name"] for c in result["cards"]} == {"Lightning Bolt"}

    def test_devotion_search(self: TestContainerIntegration, api_resource: APIResource) -> None:
        """devotion: counts a permanent's colored pips, hybrids (incl. Phyrexian) split."""
        result = api_resource._search_sql(**search_kwargs("devotion:{W}", limit=10))
        # Serra Angel {W}{W} and Cathedral Membrane {1}{W/P} (Phyrexian W
        # counts toward W devotion, same as a plain hybrid would)
        assert {c["name"] for c in result["cards"]} == {"Serra Angel", "Cathedral Membrane"}

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
        assert len(cards) == 5
        card_names = {card["name"] for card in cards}
        expected_names = {"Lightning Bolt", "Serra Angel", "Black Lotus", "Boggart Ram-Gang", "Cathedral Membrane"}
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
        import_result = api_resource.import_card_by_name(card_name=card_name)

        # Check that the import was successful
        assert import_result["status"] == "success"
        assert import_result["cards_loaded"] >= 35

        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
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
        import_result = api_resource.import_card_by_name(card_name=card_name)

        # Check that the import was successful (or already exists, which is also fine for this test)
        assert import_result["status"] in ["success", "already_exists"], f"Import failed: {import_result}"

        if import_result["status"] == "success":
            assert import_result["cards_loaded"] == 3, f"Expected 3 cards loaded, got {import_result['cards_loaded']}"

        # Verify the card exists in database and has set information
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
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
        import_result = api_resource.import_card_by_name(card_name="Brainstorm")

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
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
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
        saved_engine = api_resource._engine
        api_resource._engine = QueryEngine(shm_path=str(shm_path))
        try:
            # Reload the engine so it picks up the direct DB update
            api_resource._reload_engine(force=True)

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
            api_resource._engine = saved_engine
            shm_path.unlink(missing_ok=True)
            shm_path.with_suffix(".lock").unlink(missing_ok=True)
