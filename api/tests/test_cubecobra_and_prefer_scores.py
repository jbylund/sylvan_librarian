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


class TestRepresentativePin:
    """Scryfall's own representative choice pins prefer_score — and yields to art_style.

    The label is its `oracle_cards` dump: one card object per oracle_id, and that object IS the
    printing Scryfall shows. See the `representative` component in backfill_prefer_scores.sql.
    """

    @staticmethod
    def _label(api: APIResource, scryfall_id: str) -> None:
        with api.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO magic.scryfall_representatives (scryfall_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (scryfall_id,),
            )

    @staticmethod
    def _score(api: APIResource, scryfall_id: str) -> float:
        with api.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT prefer_score FROM magic.cards WHERE scryfall_id = %s", (scryfall_id,))
            return cursor.fetchone()["prefer_score"]

    def test_a_labelled_printing_is_pinned(self, api_resource: APIResource) -> None:
        cid = str(uuid.uuid4())
        _insert_card(api_resource, make_raw_card(card_id=cid, name=f"Pinned {uuid.uuid4()}"))
        api_resource.admin.backfill_prefer_scores()
        before = self._score(api_resource, cid)

        self._label(api_resource, cid)
        api_resource.admin.backfill_prefer_scores()
        after = self._score(api_resource, cid)

        # The pin dominates every real component sum rather than nudging it.
        assert after - before > 900, f"labelled printing not pinned ({before} -> {after})"

    def test_an_unlabelled_printing_is_unchanged(self, api_resource: APIResource) -> None:
        """Empty/partial label table must score exactly as before — the dump is an OPTIONAL input."""
        cid = str(uuid.uuid4())
        _insert_card(api_resource, make_raw_card(card_id=cid, name=f"Unpinned {uuid.uuid4()}"))
        api_resource.admin.backfill_prefer_scores()
        first = self._score(api_resource, cid)
        api_resource.admin.backfill_prefer_scores()
        assert self._score(api_resource, cid) == first
        assert first < 900, "an unlabelled printing must not carry the pin"

    def test_the_pin_yields_to_art_style(self, api_resource: APIResource) -> None:
        """An off-style label loses the pin when an on-style printing of the same card exists.

        This is the whole reason the component is not simply "defer to Scryfall": its dump favours
        the most recent recognizable printing, which on 213 cards is licensed-crossover art that
        art_style demotes on purpose (177 of 177 labelled comparisons).
        """
        name = f"Crossover {uuid.uuid4()}"
        on_id, off_id = str(uuid.uuid4()), str(uuid.uuid4())
        on_raw = make_raw_card(card_id=on_id, name=name)
        off_raw = make_raw_card(card_id=off_id, name=name)
        off_raw["oracle_id"] = on_raw["oracle_id"]  # same card, two printings
        _insert_card(api_resource, on_raw)
        _insert_card(api_resource, off_raw)

        # Mark the labelled printing off-style the way the tagger would.
        with api_resource.app_context.writer_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE magic.cards SET card_art_tags = %s::jsonb WHERE scryfall_id = %s",
                ('{"external-ip": true}', off_id),
            )
        self._label(api_resource, off_id)
        api_resource.admin.backfill_prefer_scores()

        assert self._score(api_resource, off_id) < 900, "an off-style label must not be pinned"
        assert self._score(api_resource, on_id) < 900, "the on-style sibling is not labelled either"
