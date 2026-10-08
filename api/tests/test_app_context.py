"""Tests for AppContext's setup-complete cache, cache-generation bump, and engine reload."""

from __future__ import annotations

import multiprocessing
import unittest
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from api.app_context import MIN_IMPORT_CARDS, AppContext
from api.parsing.set_groups import SET_GROUPS_SQL, release_group, replace_set_groups

if TYPE_CHECKING:
    from collections.abc import Generator


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


class TestSetGroups:
    """The release-group registry (`g:<set>`) is read once per process per import, and never fails a search."""

    ROWS = (
        {"code": "ecl", "parent_set_code": None, "name": "Lorwyn Eclipsed"},
        {"code": "ecc", "parent_set_code": "ecl", "name": "Lorwyn Eclipsed Commander"},
    )

    @pytest.fixture(autouse=True)
    def _empty_registry(self) -> Generator[None]:
        replace_set_groups([])
        yield
        replace_set_groups([])

    def _make_context(self) -> tuple[AppContext, MagicMock]:
        pool = MagicMock()
        cursor = pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = list(self.ROWS)
        ctx = AppContext(
            reader_pool=pool,
            writer_pool=MagicMock(),
            engine=MagicMock(),
            last_import_time=multiprocessing.Value("d", 1.0, lock=True),
        )
        return ctx, cursor

    def test_loads_the_registry_from_the_set_table(self) -> None:
        ctx, cursor = self._make_context()
        ctx.ensure_set_groups()
        cursor.execute.assert_called_once_with(SET_GROUPS_SQL)
        assert release_group("ecc") == ("ecc", ("ecl",))

    def test_a_second_call_within_the_ttl_does_not_read_again(self) -> None:
        ctx, cursor = self._make_context()
        ctx.ensure_set_groups()
        ctx.ensure_set_groups()
        assert cursor.execute.call_count == 1

    def test_a_completed_import_reloads(self) -> None:
        ctx, cursor = self._make_context()
        ctx.ensure_set_groups()
        cursor.fetchall.return_value = [
            *self.ROWS,
            {"code": "tecc", "parent_set_code": "ecc", "name": "Lorwyn Eclipsed Commander Tokens"},
        ]
        ctx.last_import_time.value = 2.0
        ctx.ensure_set_groups()
        assert cursor.execute.call_count == 2
        assert release_group("ecc") == ("ecc", ("ecl", "tecc"))

    def test_force_reloads_within_the_ttl(self) -> None:
        ctx, cursor = self._make_context()
        ctx.ensure_set_groups()
        ctx.ensure_set_groups(force=True)
        assert cursor.execute.call_count == 2

    def test_a_database_error_is_swallowed_and_retried(self) -> None:
        ctx, cursor = self._make_context()
        cursor.execute.side_effect = RuntimeError("relation magic.sets does not exist")
        ctx.ensure_set_groups()  # must not raise: a search would fail with it
        assert release_group("ecc") is None
        cursor.execute.side_effect = None
        ctx.ensure_set_groups()
        assert release_group("ecc") == ("ecc", ("ecl",))

    def test_a_failed_reload_keeps_the_last_good_registry(self) -> None:
        ctx, cursor = self._make_context()
        ctx.ensure_set_groups()
        cursor.execute.side_effect = RuntimeError("connection lost")
        ctx.ensure_set_groups(force=True)
        assert release_group("ecc") == ("ecc", ("ecl",))


if __name__ == "__main__":
    unittest.main()
