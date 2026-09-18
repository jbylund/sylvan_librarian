"""Tests for _fetch_cubecobra_data, _insert_cubecobra_data, and backfill_prefer_scores."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from api.card_processing import preprocess_card
from api.tests.helpers import make_raw_card

if TYPE_CHECKING:
    from api.api_resource import APIResource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_card(api: APIResource, raw: dict) -> uuid.UUID:
    """Insert a raw card and return its oracle_id as a UUID."""
    api.admin._upsert_cards([raw])
    (processed,) = preprocess_card(raw)
    return uuid.UUID(processed["oracle_id"])


# ---------------------------------------------------------------------------
# _insert_cubecobra_data
# ---------------------------------------------------------------------------


class TestInsertCubecobraData:
    def test_updates_matching_oracle_id(self, api_resource: APIResource) -> None:
        oracle_id = _insert_card(api_resource, make_raw_card(name="Cubecobra Insert Test"))

        cubecobra_data = {oracle_id: {"elo": 1200.5, "cube_count": 42, "pick_count": 100}}
        rows_updated = api_resource.admin._insert_cubecobra_data(cubecobra_data)

        assert rows_updated >= 1

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
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
        unknown = uuid.uuid4()
        rows_updated = api_resource.admin._insert_cubecobra_data({unknown: {"elo": 999.0, "cube_count": 1, "pick_count": 1}})
        assert rows_updated == 0

    def test_empty_dict_updates_zero_rows(self, api_resource: APIResource) -> None:
        rows_updated = api_resource.admin._insert_cubecobra_data({})
        assert rows_updated == 0

    def test_multiple_cards_updated_in_one_call(self, api_resource: APIResource) -> None:
        oid1 = _insert_card(api_resource, make_raw_card(name=f"Multi CubeCobra A {uuid.uuid4()}"))
        oid2 = _insert_card(api_resource, make_raw_card(name=f"Multi CubeCobra B {uuid.uuid4()}"))

        cubecobra_data = {
            oid1: {"elo": 1100.0, "cube_count": 10, "pick_count": 20},
            oid2: {"elo": 900.0, "cube_count": 5, "pick_count": 8},
        }
        rows_updated = api_resource.admin._insert_cubecobra_data(cubecobra_data)
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
        oracle_id = uuid.uuid4()
        page1 = [{"oracle_id": str(oracle_id), "elo": 1500, "cubeCount": 30, "pickCount": 60}]

        with patch.object(api_resource.admin, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [
                self._mock_response(page1),
                self._mock_response([]),  # empty page terminates
            ]
            pages = list(api_resource.admin._fetch_cubecobra_data({oracle_id}))

        assert len(pages) == 1
        assert oracle_id in pages[0]
        assert pages[0][oracle_id] == {"elo": 1500, "cube_count": 30, "pick_count": 60}

    def test_filters_out_oracle_ids_not_in_db(self, api_resource: APIResource) -> None:
        known = uuid.uuid4()
        unknown = uuid.uuid4()
        page1 = [
            {"oracle_id": str(known), "elo": 1000, "cubeCount": 5, "pickCount": 10},
            {"oracle_id": str(unknown), "elo": 800, "cubeCount": 2, "pickCount": 4},
        ]

        with patch.object(api_resource.admin, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [self._mock_response(page1), self._mock_response([])]
            pages = list(api_resource.admin._fetch_cubecobra_data({known}))

        assert known in pages[0]
        assert unknown not in pages[0]

    def test_paginates_until_empty_page(self, api_resource: APIResource) -> None:
        oids = [uuid.uuid4() for _ in range(3)]
        pages_data = [
            [{"oracle_id": str(oids[0]), "elo": 1, "cubeCount": 1, "pickCount": 1}],
            [{"oracle_id": str(oids[1]), "elo": 2, "cubeCount": 2, "pickCount": 2}],
            [{"oracle_id": str(oids[2]), "elo": 3, "cubeCount": 3, "pickCount": 3}],
            [],  # terminator
        ]

        with patch.object(api_resource.admin, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [self._mock_response(p) for p in pages_data]
            pages = list(api_resource.admin._fetch_cubecobra_data(set(oids)))

        assert len(pages) == 3

    def test_empty_db_oracle_ids_yields_empty_pages(self, api_resource: APIResource) -> None:
        page1 = [{"oracle_id": str(uuid.uuid4()), "elo": 1, "cubeCount": 1, "pickCount": 1}]

        with patch.object(api_resource.admin, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [self._mock_response(page1), self._mock_response([])]
            pages = list(api_resource.admin._fetch_cubecobra_data(set()))

        # All cards filtered out, but we still get one page dict (empty)
        assert all(len(p) == 0 for p in pages)


# ---------------------------------------------------------------------------
# ingest_cubecobra
# ---------------------------------------------------------------------------


class TestIngestCubecobra:
    def _mock_response(self, cards: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"data": cards}
        return resp

    def test_empty_first_page_does_not_raise(self, api_resource: APIResource) -> None:
        """Regression test for #965: an empty first page must not leave cards_updated unbound."""
        with patch.object(api_resource.admin, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [self._mock_response([])]
            result = api_resource.admin.ingest_cubecobra()

        assert result["status"] == "success"
        assert result["cards_updated"] == 0

    def test_sums_cards_updated_across_pages(self, api_resource: APIResource) -> None:
        """cards_updated must total every page, not just the last one fetched."""
        oid1 = _insert_card(api_resource, make_raw_card(name=f"Ingest Page A {uuid.uuid4()}"))
        oid2 = _insert_card(api_resource, make_raw_card(name=f"Ingest Page B {uuid.uuid4()}"))
        page1 = [{"oracle_id": str(oid1), "elo": 1000, "cubeCount": 5, "pickCount": 10}]
        page2 = [{"oracle_id": str(oid2), "elo": 900, "cubeCount": 3, "pickCount": 6}]

        with patch.object(api_resource.admin, "_session") as mock_session, patch("api.api_resource.time.sleep"):
            mock_session.get.side_effect = [
                self._mock_response(page1),
                self._mock_response(page2),
                self._mock_response([]),
            ]
            result = api_resource.admin.ingest_cubecobra()

        assert result["cards_updated"] == 2


# ---------------------------------------------------------------------------
# backfill_prefer_scores
# ---------------------------------------------------------------------------


class TestBackfillPreferScores:
    def test_returns_success_status(self, api_resource: APIResource) -> None:
        result = api_resource.admin.backfill_prefer_scores()
        assert result["status"] == "success"

    def test_returns_cards_updated_count(self, api_resource: APIResource) -> None:
        _insert_card(api_resource, make_raw_card(name=f"Prefer Score Card {uuid.uuid4()}"))
        result = api_resource.admin.backfill_prefer_scores()
        assert result["cards_updated"] >= 1

    def test_message_includes_count(self, api_resource: APIResource) -> None:
        result = api_resource.admin.backfill_prefer_scores()
        assert str(result["cards_updated"]) in result["message"]

    def test_prefer_score_populated_in_db(self, api_resource: APIResource) -> None:
        oracle_id = _insert_card(api_resource, make_raw_card(name=f"Prefer Score Check {uuid.uuid4()}"))
        api_resource.admin.backfill_prefer_scores()

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT prefer_score FROM magic.cards WHERE oracle_id = %s LIMIT 1", (oracle_id,))
            row = cursor.fetchone()

        assert row is not None
        assert row["prefer_score"] is not None

    def test_second_run_updates_zero_rows(self, api_resource: APIResource) -> None:
        """Re-running the backfill on already-scored cards should touch no rows."""
        _insert_card(api_resource, make_raw_card(name=f"Idempotent Score Card {uuid.uuid4()}"))
        api_resource.admin.backfill_prefer_scores()

        result = api_resource.admin.backfill_prefer_scores()

        assert result["cards_updated"] == 0

    def test_reports_duration_and_scored_count(self, api_resource: APIResource) -> None:
        """cards_scored counts the whole scored corpus, not just the rows this run moved."""
        _insert_card(api_resource, make_raw_card(name=f"Stats Card {uuid.uuid4()}"))
        api_resource.admin.backfill_prefer_scores()

        result = api_resource.admin.backfill_prefer_scores()

        assert result["duration_seconds"] >= 0
        # Nothing moved on the second run, but the corpus is still fully scored.
        assert result["cards_updated"] == 0
        assert result["cards_scored"] >= 1


# ---------------------------------------------------------------------------
# backfill_cubecobra_scores
# ---------------------------------------------------------------------------


class TestBackfillCubecobraScores:
    def test_returns_success_status(self, api_resource: APIResource) -> None:
        result = api_resource.admin.backfill_cubecobra_scores()
        assert result["status"] == "success"

    def test_reports_duration_and_counts(self, api_resource: APIResource) -> None:
        _insert_card(api_resource, make_raw_card(name=f"Cubecobra Stats Card {uuid.uuid4()}"))

        result = api_resource.admin.backfill_cubecobra_scores()

        assert result["duration_seconds"] >= 0
        assert result["cards_updated"] >= 0
        assert result["cards_with_cubecobra_data"] >= 0

    def test_counts_cards_carrying_cubecobra_data(self, api_resource: APIResource) -> None:
        """Count the cards actually carrying CubeCobra data.

        The normal import never populates cubecobra_elo, so this distinguishes a real ranking
        from one computed over an all-NULL corpus.
        """
        oracle_id = _insert_card(api_resource, make_raw_card(name=f"Cubecobra Data Card {uuid.uuid4()}"))
        before = api_resource.admin.backfill_cubecobra_scores()["cards_with_cubecobra_data"]

        api_resource.admin._insert_cubecobra_data({oracle_id: {"elo": 1500.0, "cube_count": 7, "pick_count": 9}})

        assert api_resource.admin.backfill_cubecobra_scores()["cards_with_cubecobra_data"] == before + 1


# ---------------------------------------------------------------------------
# backfill_scryfall_prefer_scores
# ---------------------------------------------------------------------------


class TestBackfillScryfallPreferScores:
    """scryfall_prefer_score reproduces Scryfall's per-card printing ordering.

    Their representative (magic.scryfall_representatives) first, then their prints-listing order:
    release date, set code, collector number, all descending. See
    backfill_scryfall_prefer_scores.sql for the probes that established the tie-breaks.
    """

    @staticmethod
    def _label(api: APIResource, scryfall_id: str) -> None:
        with api.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO magic.scryfall_representatives (scryfall_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (scryfall_id,),
            )

    @staticmethod
    def _scores(api: APIResource, scryfall_ids: list[str]) -> dict[str, float]:
        with api.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT scryfall_id, scryfall_prefer_score FROM magic.cards WHERE scryfall_id = ANY(%s)",
                (scryfall_ids,),
            )
            return {str(r["scryfall_id"]): r["scryfall_prefer_score"] for r in cursor.fetchall()}

    @staticmethod
    def _printing(name: str, oracle_id: str, released_at: str, set_code: str, collector_number: str) -> dict:
        raw = make_raw_card(name=name)
        raw["oracle_id"] = oracle_id
        raw["released_at"] = released_at
        raw["set"] = set_code
        raw["collector_number"] = collector_number
        return raw

    def test_representative_tops_the_ordering_even_when_older(self, api_resource: APIResource) -> None:
        """The labelled printing scores 0 and the rest follow the prints listing beneath it.

        The label deliberately sits on an OLDER printing than the newest -- on the real corpus
        Scryfall's representative is rarely the newest printing, so release order alone must not
        be able to outrank the pin.
        """
        name = f"Scryfall Order {uuid.uuid4()}"
        oracle_id = str(uuid.uuid4())
        rep = self._printing(name, oracle_id, "2024-01-01", "aaa", "7")
        newest_hi = self._printing(name, oracle_id, "2025-05-01", "zzz", "10")
        newest_lo = self._printing(name, oracle_id, "2025-05-01", "zzz", "3")
        oldest = self._printing(name, oracle_id, "2020-01-01", "tst", "1")
        for raw in (rep, newest_hi, newest_lo, oldest):
            _insert_card(api_resource, raw)
        self._label(api_resource, rep["id"])

        api_resource.admin.backfill_scryfall_prefer_scores()
        scores = self._scores(api_resource, [rep["id"], newest_hi["id"], newest_lo["id"], oldest["id"]])

        # rep pinned first; then released desc, and within the tied date collector number desc.
        assert scores == {
            rep["id"]: 0.0,
            newest_hi["id"]: -1.0,
            newest_lo["id"]: -2.0,
            oldest["id"]: -3.0,
        }

    def test_same_date_ties_break_by_set_code_descending(self, api_resource: APIResource) -> None:
        """Same release date, different sets: set CODE descending, as Scryfall's listing orders it."""
        name = f"Scryfall Set Tie {uuid.uuid4()}"
        oracle_id = str(uuid.uuid4())
        low_set = self._printing(name, oracle_id, "2025-05-01", "fdc", "2")
        high_set = self._printing(name, oracle_id, "2025-05-01", "pfd", "1")
        for raw in (low_set, high_set):
            _insert_card(api_resource, raw)

        api_resource.admin.backfill_scryfall_prefer_scores()
        scores = self._scores(api_resource, [low_set["id"], high_set["id"]])

        assert scores == {high_set["id"]: 0.0, low_set["id"]: -1.0}

    def test_unlabelled_card_is_still_fully_ranked(self, api_resource: APIResource) -> None:
        """No representative label at all: the card still gets the prints-listing ordering."""
        name = f"Scryfall Unlabelled {uuid.uuid4()}"
        oracle_id = str(uuid.uuid4())
        newer = self._printing(name, oracle_id, "2023-06-01", "bbb", "5")
        older = self._printing(name, oracle_id, "2021-06-01", "ccc", "9")
        for raw in (newer, older):
            _insert_card(api_resource, raw)

        api_resource.admin.backfill_scryfall_prefer_scores()
        scores = self._scores(api_resource, [newer["id"], older["id"]])

        assert scores == {newer["id"]: 0.0, older["id"]: -1.0}

    def test_leaves_prefer_score_alone(self, api_resource: APIResource) -> None:
        """The Scryfall backfill must not touch the curated default prefer_score."""
        raw = make_raw_card(name=f"Scryfall Separate {uuid.uuid4()}")
        _insert_card(api_resource, raw)
        api_resource.admin.backfill_prefer_scores()

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT prefer_score FROM magic.cards WHERE scryfall_id = %s", (raw["id"],))
            before = cursor.fetchone()["prefer_score"]

        self._label(api_resource, raw["id"])
        result = api_resource.admin.backfill_scryfall_prefer_scores()
        assert result["status"] == "success"

        with api_resource.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT prefer_score FROM magic.cards WHERE scryfall_id = %s", (raw["id"],))
            after = cursor.fetchone()["prefer_score"]

        assert after == before
