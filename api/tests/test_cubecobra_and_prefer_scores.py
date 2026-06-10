"""Tests for _fetch_cubecobra_data, _insert_cubecobra_data, and backfill_prefer_scores."""

from __future__ import annotations

import multiprocessing
import os
import time
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from testcontainers.postgres import PostgresContainer

from api.api_resource import APIResource
from api.card_processing import preprocess_card

if TYPE_CHECKING:
    from collections.abc import Generator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_card(card_id: str | None = None, name: str = "Test Card") -> dict:
    cid = card_id or str(uuid.uuid4())
    jpg = f"{cid[0]}/{cid[1]}/{cid}.jpg"
    return {
        "id": cid,
        "oracle_id": str(uuid.uuid4()),
        "name": name,
        "released_at": "2020-01-01",
        "legalities": {"vintage": "legal"},
        "games": ["paper"],
        "type_line": "Instant",
        "colors": [],
        "color_identity": [],
        "keywords": [],
        "prices": {"usd": "0.10"},
        "set": "tst",
        "rarity": "common",
        "collector_number": "1",
        "image_uris": {
            "small": f"https://cards.scryfall.io/small/front/{jpg}",
            "normal": f"https://cards.scryfall.io/normal/front/{jpg}",
            "large": f"https://cards.scryfall.io/large/front/{jpg}",
            "png": f"https://cards.scryfall.io/png/front/{jpg}",
            "art_crop": f"https://cards.scryfall.io/art_crop/front/{jpg}",
            "border_crop": f"https://cards.scryfall.io/border_crop/front/{jpg}",
        },
    }


def _insert_card(api: APIResource, raw: dict) -> str:
    """Insert a raw card and return its oracle_id."""
    api._load_cards_with_staging([raw])
    (processed,) = preprocess_card(raw)
    return processed["oracle_id"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def postgres_container() -> Generator[PostgresContainer]:
    container = PostgresContainer(
        image="postgres:18",
        username="testuser",
        password="testpass",  # noqa: S106
        dbname="testdb",
    )
    with container as pg:
        yield pg


@pytest.fixture(scope="class")
def api_resource(postgres_container: PostgresContainer) -> Generator[APIResource]:
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    original = {k: os.environ.get(k) for k in ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"]}
    os.environ.update({"PGHOST": host, "PGPORT": str(port), "PGDATABASE": "testdb", "PGUSER": "testuser", "PGPASSWORD": "testpass"})
    try:
        api = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
            schema_setup_event=multiprocessing.Event(),
        )
        api._setup_complete = lambda: True
        api._import_recent = lambda: True
        api.setup_schema()
        yield api
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if hasattr(api, "_conn_pool"):
            api._conn_pool.close()


# ---------------------------------------------------------------------------
# _insert_cubecobra_data
# ---------------------------------------------------------------------------


class TestInsertCubecobraData:
    def test_updates_matching_oracle_id(self, api_resource: APIResource) -> None:
        oracle_id = _insert_card(api_resource, _raw_card(name="Cubecobra Insert Test"))

        cubecobra_data = {oracle_id: {"elo": 1200.5, "cube_count": 42, "pick_count": 100}}
        rows_updated = api_resource._insert_cubecobra_data(cubecobra_data)

        assert rows_updated >= 1

        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT cubecobra_elo, cubecobra_cube_count, cubecobra_pick_count FROM magic.cards WHERE oracle_id = %s LIMIT 1",
                (oracle_id,),
            )
            row = cursor.fetchone()

        assert row is not None
        assert abs(row["cubecobra_elo"] - 1200.5) < 0.01
        assert row["cubecobra_cube_count"] == 42
        assert row["cubecobra_pick_count"] == 100

    def test_unknown_oracle_id_updates_zero_rows(self, api_resource: APIResource) -> None:
        unknown = str(uuid.uuid4())
        rows_updated = api_resource._insert_cubecobra_data({unknown: {"elo": 999.0, "cube_count": 1, "pick_count": 1}})
        assert rows_updated == 0

    def test_empty_dict_updates_zero_rows(self, api_resource: APIResource) -> None:
        rows_updated = api_resource._insert_cubecobra_data({})
        assert rows_updated == 0

    def test_multiple_cards_updated_in_one_call(self, api_resource: APIResource) -> None:
        oid1 = _insert_card(api_resource, _raw_card(name=f"Multi CubeCobra A {uuid.uuid4()}"))
        oid2 = _insert_card(api_resource, _raw_card(name=f"Multi CubeCobra B {uuid.uuid4()}"))

        cubecobra_data = {
            oid1: {"elo": 1100.0, "cube_count": 10, "pick_count": 20},
            oid2: {"elo": 900.0, "cube_count": 5, "pick_count": 8},
        }
        rows_updated = api_resource._insert_cubecobra_data(cubecobra_data)
        assert rows_updated == 2


# ---------------------------------------------------------------------------
# _fetch_cubecobra_data
# ---------------------------------------------------------------------------


class TestFetchCubecobraData:
    def _mock_response(self, cards: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"data": cards}
        return resp

    def test_yields_matching_cards_and_stops_on_empty_page(self, api_resource: APIResource) -> None:
        oracle_id = str(uuid.uuid4())
        page1 = [{"oracle_id": oracle_id, "elo": 1500, "cubeCount": 30, "pickCount": 60}]

        with patch.object(api_resource, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [
                self._mock_response(page1),
                self._mock_response([]),  # empty page terminates
            ]
            pages = list(api_resource._fetch_cubecobra_data({oracle_id}))

        assert len(pages) == 1
        assert oracle_id in pages[0]
        assert pages[0][oracle_id] == {"elo": 1500, "cube_count": 30, "pick_count": 60}

    def test_filters_out_oracle_ids_not_in_db(self, api_resource: APIResource) -> None:
        known = str(uuid.uuid4())
        unknown = str(uuid.uuid4())
        page1 = [
            {"oracle_id": known, "elo": 1000, "cubeCount": 5, "pickCount": 10},
            {"oracle_id": unknown, "elo": 800, "cubeCount": 2, "pickCount": 4},
        ]

        with patch.object(api_resource, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [self._mock_response(page1), self._mock_response([])]
            pages = list(api_resource._fetch_cubecobra_data({known}))

        assert known in pages[0]
        assert unknown not in pages[0]

    def test_paginates_until_empty_page(self, api_resource: APIResource) -> None:
        oids = [str(uuid.uuid4()) for _ in range(3)]
        pages_data = [
            [{"oracle_id": oids[0], "elo": 1, "cubeCount": 1, "pickCount": 1}],
            [{"oracle_id": oids[1], "elo": 2, "cubeCount": 2, "pickCount": 2}],
            [{"oracle_id": oids[2], "elo": 3, "cubeCount": 3, "pickCount": 3}],
            [],  # terminator
        ]

        with patch.object(api_resource, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [self._mock_response(p) for p in pages_data]
            pages = list(api_resource._fetch_cubecobra_data(set(oids)))

        assert len(pages) == 3

    def test_empty_db_oracle_ids_yields_empty_pages(self, api_resource: APIResource) -> None:
        page1 = [{"oracle_id": str(uuid.uuid4()), "elo": 1, "cubeCount": 1, "pickCount": 1}]

        with patch.object(api_resource, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [self._mock_response(page1), self._mock_response([])]
            pages = list(api_resource._fetch_cubecobra_data(set()))

        # All cards filtered out, but we still get one page dict (empty)
        assert all(len(p) == 0 for p in pages)


# ---------------------------------------------------------------------------
# backfill_prefer_scores
# ---------------------------------------------------------------------------


class TestBackfillPreferScores:
    def test_returns_success_status(self, api_resource: APIResource) -> None:
        result = api_resource.backfill_prefer_scores()
        assert result["status"] == "success"

    def test_returns_cards_updated_count(self, api_resource: APIResource) -> None:
        _insert_card(api_resource, _raw_card(name=f"Prefer Score Card {uuid.uuid4()}"))
        result = api_resource.backfill_prefer_scores()
        assert result["cards_updated"] >= 1

    def test_message_includes_count(self, api_resource: APIResource) -> None:
        result = api_resource.backfill_prefer_scores()
        assert str(result["cards_updated"]) in result["message"]

    def test_prefer_score_populated_in_db(self, api_resource: APIResource) -> None:
        oracle_id = _insert_card(api_resource, _raw_card(name=f"Prefer Score Check {uuid.uuid4()}"))
        api_resource.backfill_prefer_scores()

        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT prefer_score FROM magic.cards WHERE oracle_id = %s LIMIT 1", (oracle_id,))
            row = cursor.fetchone()

        assert row is not None
        assert row["prefer_score"] is not None
