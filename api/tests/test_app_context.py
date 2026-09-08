"""Tests for AppContext's setup-complete cache, cache-generation bump, and engine reload."""

from __future__ import annotations

import multiprocessing
import unittest
from unittest.mock import MagicMock, patch

import pytest

from api.app_context import MIN_IMPORT_CARDS, AppContext
from api.parsing import set_dates
from api.parsing.set_dates import replace_set_release_dates, set_release_date


def _mock_pool_returning(num_cards: int) -> MagicMock:
    """A mock connection pool whose COUNT(1) query returns num_cards."""
    pool = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = [{"num_cards": num_cards}]
    pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
    return pool


class TestSetupComplete(unittest.TestCase):
    def _make_context(self, *, num_cards: int) -> AppContext:
        return AppContext(
            reader_pool=_mock_pool_returning(num_cards),
            writer_pool=MagicMock(),
            engine=MagicMock(),
            last_import_time=multiprocessing.Value("d", 1.0, lock=True),
        )

    def test_returns_true_above_threshold(self) -> None:
        ctx = self._make_context(num_cards=MIN_IMPORT_CARDS + 1)
        assert ctx.setup_complete() is True

    def test_returns_false_below_threshold(self) -> None:
        ctx = self._make_context(num_cards=MIN_IMPORT_CARDS - 1)
        assert ctx.setup_complete() is False

    def test_caches_result_within_ttl(self) -> None:
        ctx = self._make_context(num_cards=MIN_IMPORT_CARDS + 1)
        ctx.setup_complete()
        ctx.reader_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            {"num_cards": 0},
        ]
        # Second call within the TTL must not re-query -- it should still see the cached True.
        assert ctx.setup_complete() is True

    def test_changed_last_import_time_invalidates_cache(self) -> None:
        ctx = self._make_context(num_cards=MIN_IMPORT_CARDS + 1)
        assert ctx.setup_complete() is True
        ctx.last_import_time.value = 2.0
        ctx.reader_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            {"num_cards": 0},
        ]
        assert ctx.setup_complete() is False

    def test_invalidate_setup_complete_forces_recheck(self) -> None:
        ctx = self._make_context(num_cards=MIN_IMPORT_CARDS + 1)
        assert ctx.setup_complete() is True
        ctx.invalidate_setup_complete()
        ctx.reader_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            {"num_cards": 0},
        ]
        assert ctx.setup_complete() is False

    def test_returns_false_on_database_error(self) -> None:
        pool = MagicMock()
        pool.connection.side_effect = RuntimeError("connection refused")
        ctx = AppContext(reader_pool=pool, writer_pool=MagicMock(), engine=MagicMock())
        assert ctx.setup_complete() is False


def _mock_pool_returning_rows(rows: list[dict]) -> MagicMock:
    """A mock connection pool whose fetchall() returns *rows*."""
    pool = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
    return pool


class TestSetReleaseDates(unittest.TestCase):
    """The set-code -> release-date registry: loaded once per import per process, never a search failure."""

    def setUp(self) -> None:
        self._saved = dict(set_dates._SET_RELEASE_DATES)
        replace_set_release_dates({})

    def tearDown(self) -> None:
        replace_set_release_dates(self._saved)

    def _make_context(self, rows: list[dict]) -> AppContext:
        return AppContext(
            reader_pool=_mock_pool_returning_rows(rows),
            writer_pool=MagicMock(),
            engine=MagicMock(),
            last_import_time=multiprocessing.Value("d", 1.0, lock=True),
        )

    def test_loads_once_per_last_import_time(self) -> None:
        ctx = self._make_context([{"code": "hob", "released_at": "2026-08-14"}])
        ctx.ensure_set_release_dates()
        ctx.ensure_set_release_dates()
        ctx.reader_pool.connection.assert_called_once()
        assert set_release_date("hob") == "2026-08-14"
        assert set_release_date("HOB") == "2026-08-14"

    def test_changed_last_import_time_reloads(self) -> None:
        ctx = self._make_context([{"code": "hob", "released_at": "2026-08-14"}])
        ctx.ensure_set_release_dates()
        cursor = ctx.reader_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"code": "hob", "released_at": "2026-08-14"}, {"code": "zzt", "released_at": "2020-01-01"}]
        ctx.last_import_time.value = 2.0
        ctx.ensure_set_release_dates()
        assert ctx.reader_pool.connection.call_count == 2
        assert set_release_date("zzt") == "2020-01-01"

    def test_database_error_is_swallowed_and_retried(self) -> None:
        ctx = self._make_context([{"code": "hob", "released_at": "2026-08-14"}])
        ctx.reader_pool.connection.side_effect = RuntimeError("connection refused")
        ctx.ensure_set_release_dates()  # must not raise: a search never fails on this table
        assert set_release_date("hob") is None
        # Nothing was cached on failure, so the next call tries the database again.
        ctx.reader_pool.connection.side_effect = None
        ctx.ensure_set_release_dates()
        assert set_release_date("hob") == "2026-08-14"

    def test_refresh_raises_on_database_error(self) -> None:
        ctx = self._make_context([])
        ctx.reader_pool.connection.side_effect = RuntimeError("connection refused")
        with pytest.raises(RuntimeError):
            ctx.refresh_set_release_dates()


class TestBumpCacheGeneration(unittest.TestCase):
    def test_increments_the_shared_counter(self) -> None:
        ctx = AppContext(
            reader_pool=MagicMock(),
            writer_pool=MagicMock(),
            engine=MagicMock(),
            cache_generation=multiprocessing.Value("i", 0),
        )
        ctx.bump_cache_generation()
        ctx.bump_cache_generation()
        assert ctx.cache_generation.value == 2


class TestReloadEngine(unittest.TestCase):
    def _make_context(self) -> tuple[AppContext, MagicMock, MagicMock]:
        reader_pool = MagicMock()
        writer_pool = MagicMock()
        cursor = MagicMock()
        cursor.fetchmany.side_effect = [[{"scryfall_id": "x"}], []]
        writer_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
        engine = MagicMock()
        engine.size.return_value = 0
        engine.reload_begin.return_value = True
        ctx = AppContext(reader_pool=reader_pool, writer_pool=writer_pool, engine=engine)
        return ctx, reader_pool, writer_pool

    def test_skipped_when_engine_feature_disabled(self) -> None:
        ctx, _, writer_pool = self._make_context()
        with patch("api.app_context.settings") as mock_settings:
            mock_settings.enable_engine = False
            ctx.reload_engine(force=True)
        writer_pool.connection.assert_not_called()

    def test_reads_via_writer_pool_not_reader_pool(self) -> None:
        ctx, reader_pool, writer_pool = self._make_context()
        with patch("api.app_context.settings") as mock_settings:
            mock_settings.enable_engine = True
            ctx.reload_engine(force=True)
        writer_pool.connection.assert_called_once()
        reader_pool.connection.assert_not_called()
        ctx.engine.reload_commit.assert_called_once()

    def test_skips_rebuild_when_not_forced_and_already_populated(self) -> None:
        ctx, _, writer_pool = self._make_context()
        ctx.engine.size.return_value = 1
        with patch("api.app_context.settings") as mock_settings:
            mock_settings.enable_engine = True
            ctx.reload_engine(force=False)
        writer_pool.connection.assert_not_called()

    def test_releases_guard_on_failure(self) -> None:
        ctx, _, writer_pool = self._make_context()
        writer_pool.connection.side_effect = RuntimeError("boom")
        with patch("api.app_context.settings") as mock_settings:
            mock_settings.enable_engine = True
            with pytest.raises(RuntimeError):
                ctx.reload_engine(force=True)
        # A second call must not block forever on a guard the first call failed to release.
        writer_pool.connection.side_effect = None
        with patch("api.app_context.settings") as mock_settings:
            mock_settings.enable_engine = True
            ctx.reload_engine(force=True)


if __name__ == "__main__":
    unittest.main()
