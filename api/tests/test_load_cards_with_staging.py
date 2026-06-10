"""Tests for _load_cards_with_staging, _copy_batch_to_staging, and streaming import wiring."""

from __future__ import annotations

import logging
import multiprocessing
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import psycopg

from api.api_resource import APIResource

if TYPE_CHECKING:
    import pytest
from api.card_processing import preprocess_card
from api.scryfall_bulk_data_fetcher import BulkDataKey
from api.tests.helpers import make_raw_card

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

    def test_already_imported_cards_return_all_cards_already_present(self, api_resource: APIResource) -> None:
        """Cards already in the DB are skipped; if all are skipped the status is all_cards_already_present."""
        card = make_raw_card(name="Already Present Card")
        api_resource._load_cards_with_staging([card])  # first insert

        result = api_resource._load_cards_with_staging([card])  # second attempt
        assert result["status"] == "all_cards_already_present"
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

    def test_multiple_preprocessed_but_all_already_imported_gives_correct_status(self, api_resource: APIResource) -> None:
        """Raw > 0 and preprocessed > 0 but all filtered by already_imported_ids → all_cards_already_present."""
        cards = [make_raw_card(name=f"Count Card {i}") for i in range(3)]
        api_resource._load_cards_with_staging(cards)  # seed the DB

        result = api_resource._load_cards_with_staging(cards)
        # Would be no_cards_before_preprocessing if raw==0,
        # or no_cards_after_preprocessing if preprocessed==0.
        # Only reaches all_cards_already_present when both counts are > 0.
        assert result["status"] == "all_cards_already_present"

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
            rows, _ = api_resource._copy_batch_to_staging(cursor, "test_stg_insert", (preprocessed,))
            conn.commit()
        assert rows == 1

    def test_returns_sample_cards_as_list_of_dicts(self, api_resource: APIResource) -> None:
        (preprocessed,) = preprocess_card(make_raw_card(name="Staging Sample Card"))
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            _, sample_cards = api_resource._copy_batch_to_staging(cursor, "test_stg_sample", (preprocessed,))
            conn.commit()
        for sc in sample_cards:
            assert isinstance(sc, dict)

    def test_on_conflict_does_nothing_returns_zero(self, api_resource: APIResource) -> None:
        """Re-inserting the same card returns rowcount 0 (ON CONFLICT DO NOTHING)."""
        (preprocessed,) = preprocess_card(make_raw_card(name="Staging Conflict Card"))
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            api_resource._copy_batch_to_staging(cursor, "test_stg_conflict_a", (preprocessed,))
            conn.commit()
        with api_resource._conn_pool.connection() as conn, conn.cursor() as cursor:
            rows, _ = api_resource._copy_batch_to_staging(cursor, "test_stg_conflict_b", (preprocessed,))
            conn.commit()
        assert rows == 0


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

    def test_already_imported_cards_skipped_across_batch_boundary(self, api_resource: APIResource) -> None:
        existing = [make_raw_card(name=f"Existing {uuid.uuid4()}") for _ in range(3)]
        api_resource._load_cards_with_staging(existing, page_size=10)

        new_cards = [make_raw_card(name=f"New {uuid.uuid4()}") for _ in range(4)]
        result = api_resource._load_cards_with_staging(existing + new_cards, page_size=3)
        assert result["status"] == "success"
        assert result["cards_loaded"] == 4
        assert result["cards_sent"] == 4


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
