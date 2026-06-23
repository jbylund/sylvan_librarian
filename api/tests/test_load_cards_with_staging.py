"""Tests for _load_cards_with_staging, _copy_batch_to_staging, and streaming import wiring."""

from __future__ import annotations

import logging
import multiprocessing
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import psycopg

from api.api_resource import APIResource
from api.card_processing import preprocess_card
from api.scryfall_bulk_data_fetcher import BulkDataKey
from api.tests.helpers import make_raw_card

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# Status-code tests
# ---------------------------------------------------------------------------


class TestLoadCardsWithStagingStatus:
    """_load_cards_with_staging returns the correct status string for each no-cards scenario."""

    def test_empty_list_returns_no_cards_before_preprocessing(self, api_resource: APIResource) -> None:
        result = api_resource._load_cards_with_staging([])
        assert result["status"] == "no_cards_before_preprocessing"
        assert result["cards_loaded"] == 0
        assert result["cards_sent"] == 0

    def test_empty_generator_returns_no_cards_before_preprocessing(self, api_resource: APIResource) -> None:
        result = api_resource._load_cards_with_staging(x for x in [])
        assert result["status"] == "no_cards_before_preprocessing"

    def test_preprocessing_filters_all_cards_returns_no_cards_after_preprocessing(self, api_resource: APIResource) -> None:
        """When preprocess_card returns [] for all inputs, status is no_cards_after_preprocessing."""
        with patch("api.api_resource.preprocess_card", return_value=[]):
            result = api_resource._load_cards_with_staging([make_raw_card()])
        assert result["status"] == "no_cards_after_preprocessing"
        assert result["cards_loaded"] == 0

    def test_unchanged_card_on_reimport_loads_zero(self, api_resource: APIResource) -> None:
        """Re-submitting an identical card produces success with zero loads (unchanged, no write)."""
        card = make_raw_card(name="Already Present Card")
        api_resource._load_cards_with_staging([card])  # first insert

        result = api_resource._load_cards_with_staging([card])  # second attempt
        assert result["status"] == "success"
        assert result["cards_loaded"] == 0

    def test_success_result_includes_cards_sent(self, api_resource: APIResource) -> None:
        result = api_resource._load_cards_with_staging([make_raw_card(name="Cards Sent Test")])
        assert result["status"] == "success"
        assert result["cards_sent"] >= 1
        assert "cards_loaded" in result


# ---------------------------------------------------------------------------
# _CardStream counting tests
# ---------------------------------------------------------------------------


class TestCardStreamCounting:
    """_CardStream tallies stage counts that drive the status string selection."""

    def test_multiple_preprocessed_but_all_unchanged_loads_zero(self, api_resource: APIResource) -> None:
        """Raw > 0 and preprocessed > 0 but all unchanged → success with cards_loaded=0."""
        cards = [make_raw_card(name=f"Count Card {i}") for i in range(3)]
        api_resource._load_cards_with_staging(cards)  # seed the DB

        result = api_resource._load_cards_with_staging(cards)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 0

    def test_preprocessing_filter_distinguished_from_empty_input(self, api_resource: APIResource) -> None:
        """no_cards_after_preprocessing is distinct from no_cards_before_preprocessing."""
        with patch("api.api_resource.preprocess_card", return_value=[]):
            filtered = api_resource._load_cards_with_staging([make_raw_card(), make_raw_card()])
        empty = api_resource._load_cards_with_staging([])

        assert filtered["status"] == "no_cards_after_preprocessing"
        assert empty["status"] == "no_cards_before_preprocessing"


# ---------------------------------------------------------------------------
# _copy_batch_to_staging tests
# ---------------------------------------------------------------------------


class TestCopyBatchToStaging:
    """_copy_batch_to_staging COPYs one batch through a temp staging table."""

    def test_inserts_card_and_returns_row_count(self, api_resource: APIResource) -> None:
        (preprocessed,) = preprocess_card(make_raw_card(name="Staging Insert Card"))
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            batch = api_resource._copy_batch_to_staging(cursor, "test_stg_insert", (preprocessed,))
            conn.commit()
        assert batch["inserted"] == 1
        assert batch["updated"] == 0

    def test_returns_sample_cards_as_list_of_dicts(self, api_resource: APIResource) -> None:
        (preprocessed,) = preprocess_card(make_raw_card(name="Staging Sample Card"))
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            batch = api_resource._copy_batch_to_staging(cursor, "test_stg_sample", (preprocessed,))
            conn.commit()
        for sc in batch["sample"]:
            assert isinstance(sc, dict)

    def test_unchanged_card_returns_zero_rowcount(self, api_resource: APIResource) -> None:
        """Re-inserting an unchanged card returns inserted=0, updated=0."""
        (preprocessed,) = preprocess_card(make_raw_card(name="Staging Conflict Card"))
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            api_resource._copy_batch_to_staging(cursor, "test_stg_conflict_a", (preprocessed,))
            conn.commit()
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            batch = api_resource._copy_batch_to_staging(cursor, "test_stg_conflict_b", (preprocessed,))
            conn.commit()
        assert batch["inserted"] == 0
        assert batch["updated"] == 0


# ---------------------------------------------------------------------------
# Multi-batch tests
# ---------------------------------------------------------------------------


class TestMultiBatchLoad:
    """Cards spanning multiple batches are fully inserted."""

    def test_all_cards_inserted_across_batches(self, api_resource: APIResource) -> None:
        cards = [make_raw_card(name=f"Batch Card {uuid.uuid4()}") for _ in range(7)]
        result = api_resource._load_cards_with_staging(cards, page_size=3)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 7
        assert result["cards_sent"] == 7

    def test_batch_boundary_at_exact_multiple(self, api_resource: APIResource) -> None:
        """page_size=4, 8 cards → two full batches of 4."""
        cards = [make_raw_card(name=f"Exact Batch {uuid.uuid4()}") for _ in range(8)]
        result = api_resource._load_cards_with_staging(cards, page_size=4)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 8

    def test_unchanged_cards_not_loaded_across_batch_boundary(self, api_resource: APIResource) -> None:
        existing = [make_raw_card(name=f"Existing {uuid.uuid4()}") for _ in range(3)]
        api_resource._load_cards_with_staging(existing, page_size=10)

        new_cards = [make_raw_card(name=f"New {uuid.uuid4()}") for _ in range(4)]
        result = api_resource._load_cards_with_staging(existing + new_cards, page_size=3)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 4
        assert result["cards_sent"] == 7  # all cards are sent; existing ones just produce 0 loads


# ---------------------------------------------------------------------------
# Error-path cleanup tests
# ---------------------------------------------------------------------------


class TestStagingTableCleanupOnError:
    """A mid-batch failure must not leak the temp staging table or poison the pooled connection."""

    @staticmethod
    def _create_table_then_fail(cursor: psycopg.Cursor, staging_table_name: str, page: tuple) -> tuple:  # noqa: ARG004
        cursor.execute(f"CREATE TEMPORARY TABLE {staging_table_name} (card_blob jsonb) ON COMMIT DROP")
        msg = "simulated failure mid-batch"
        raise psycopg.DataError(msg)

    def test_error_mid_batch_returns_database_error_and_leaks_nothing(
        self, api_resource: APIResource, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.object(api_resource, "_copy_batch_to_staging", side_effect=self._create_table_then_fail),
            caplog.at_level(logging.ERROR, logger="api.api_resource"),
        ):
            result = api_resource._load_cards_with_staging([make_raw_card(name="Doomed Card")])

        assert result["status"] == "database_error"
        assert result["cards_loaded"] == 0

        # The failure must be interpretable: exception type in the message, full traceback in the log.
        assert result["message"] == "Error loading cards: DataError: simulated failure mid-batch"
        error_records = [r for r in caplog.records if "Error loading cards with staging table" in r.message]
        assert error_records, "the failure should be logged"
        assert all(r.exc_info for r in error_records), "the log record should carry the traceback"

        # The rollback on connection return must have removed the temp table from every session.
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT relname FROM pg_class WHERE relname LIKE 'import_staging_%'")
            leaked = [r["relname"] for r in cursor.fetchall()]
        assert leaked == []

    def test_import_succeeds_after_earlier_failure(self, api_resource: APIResource) -> None:
        """The pool is reusable after a failed import: the next import on the same pool succeeds."""
        with patch.object(api_resource, "_copy_batch_to_staging", side_effect=self._create_table_then_fail):
            failed = api_resource._load_cards_with_staging([make_raw_card(name="First Try Fails")])
        assert failed["status"] == "database_error"

        recovered = api_resource._load_cards_with_staging([make_raw_card(name="Second Try Succeeds")])
        assert recovered["status"] == "success"
        assert recovered["cards_loaded"] == 1


# ---------------------------------------------------------------------------
# _run_import_under_lock streaming wiring (mocked — tests control flow only)
# ---------------------------------------------------------------------------


class TestRunImportUnderLockStreaming:
    """_run_import_under_lock must delegate to stream_data_for_key, not _get_cards_to_insert."""

    def _make_api(self) -> APIResource:
        # Patch out setup_schema and import_data during construction: __init__ calls both, and an
        # unpatched import_data with last_import_time=0.0 performs a real full Scryfall import.
        with patch.object(APIResource, "setup_schema"), patch.object(APIResource, "import_data"):
            api = APIResource(last_import_time=multiprocessing.Value("d", 0.0, lock=True))
        api._conn_pool.close()
        api._conn_pool = MagicMock()
        return api

    def test_calls_stream_data_for_key(self) -> None:
        api = self._make_api()
        with (
            patch.object(api, "_import_recent", return_value=False),
            patch.object(api, "setup_schema"),
            patch.object(
                api,
                "_load_cards_with_staging",
                return_value={"status": "no_cards_before_preprocessing", "cards_loaded": 0, "sample_cards": [], "message": ""},
            ),
            patch.object(api._bulk_data_fetcher, "stream_data_for_key") as mock_stream,
        ):
            mock_stream.return_value = iter([])
            api._run_import_under_lock()
        mock_stream.assert_called_once_with(BulkDataKey.DEFAULT_CARDS)

    def test_stream_iterator_passed_directly_to_load_cards_with_staging(self) -> None:
        """The exact iterator returned by stream_data_for_key is forwarded to _load_cards_with_staging."""
        api = self._make_api()
        sentinel = iter([{"id": "sentinel"}])
        with (
            patch.object(api, "_import_recent", return_value=False),
            patch.object(api, "setup_schema"),
            patch.object(api._bulk_data_fetcher, "stream_data_for_key", return_value=sentinel),
            patch.object(
                api,
                "_load_cards_with_staging",
                return_value={"status": "no_cards_before_preprocessing", "cards_loaded": 0, "sample_cards": [], "message": ""},
            ) as mock_staging,
        ):
            api._run_import_under_lock()
        args, _ = mock_staging.call_args
        assert args[0] is sentinel


# ---------------------------------------------------------------------------
# Upsert behavior tests
# ---------------------------------------------------------------------------


class TestUpsertBehavior:
    """_copy_batch_to_staging correctly partitions into new, unchanged, and changed cards."""

    def test_unchanged_card_skips_write(self, api_resource: APIResource) -> None:
        """Group 2: re-submitting identical data produces zero loads."""
        card_id = str(uuid.uuid4())
        card = make_raw_card(card_id=card_id)
        api_resource._load_cards_with_staging([card])

        result = api_resource._load_cards_with_staging([card])
        assert result["cards_inserted"] == 0
        assert result["cards_updated"] == 0

    def test_changed_card_is_updated(self, api_resource: APIResource) -> None:
        """Group 3: re-submitting a card with changed data updates the stored row."""
        card_id = str(uuid.uuid4())
        api_resource._load_cards_with_staging([make_raw_card(card_id=card_id)])

        result = api_resource._load_cards_with_staging([make_raw_card(card_id=card_id, rarity="rare")])
        assert result["cards_inserted"] == 0
        assert result["cards_updated"] == 1

        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT card_rarity_text FROM magic.cards WHERE scryfall_id = %s", (card_id,))
            row = cursor.fetchone()
        assert row["card_rarity_text"] == "rare"

    def test_changed_card_preserves_backfilled_columns(self, api_resource: APIResource) -> None:
        """Group 3: updating a changed card leaves prefer_score and card_is_tags intact."""
        card_id = str(uuid.uuid4())
        api_resource._load_cards_with_staging([make_raw_card(card_id=card_id)])

        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE magic.cards SET prefer_score = 42.0, card_is_tags = '{\"is:instant\": true}'::jsonb WHERE scryfall_id = %s",
                (card_id,),
            )
            conn.commit()

        api_resource._load_cards_with_staging([make_raw_card(card_id=card_id, rarity="rare")])

        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT prefer_score, card_is_tags FROM magic.cards WHERE scryfall_id = %s", (card_id,))
            row = cursor.fetchone()
        assert row["prefer_score"] == 42.0
        assert row["card_is_tags"] == {"is:instant": True}
