"""Tests for the rulings bulk import.

Mocked connection pool rather than the container, matching test_tagging_integration.py. The reason
here is stronger than convention: `import_rulings` opens with an unconditional
`DELETE FROM magic.rulings`, and the postgres container is shared across the whole session, so a
database-backed test would wipe the row test_scryfall_cards_routes.py inserts for its own rulings
assertions and leave that module dependent on file ordering. What the mocks cannot reach -- the
`::date` cast, `ON CONFLICT DO NOTHING` against idx_rulings_identity -- is covered by running the
importer against the live bulk file, which is not something the suite should do on every run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from api import rulings_import
from api.rulings_import import _valid_rulings, import_rulings
from api.scryfall_bulk_data_fetcher import BulkDataKey

if TYPE_CHECKING:
    from collections.abc import Iterator

ORACLE_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"

RULINGS_FIXTURE = [
    {"oracle_id": ORACLE_ID, "source": "wotc", "published_at": "2004-10-04", "comment": "First ruling."},
    {"oracle_id": ORACLE_ID, "source": "scryfall", "published_at": "2019-08-23", "comment": "Second ruling."},
]


def _make_mock_conn_pool(rowcount: int = 0) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return a mock conn_pool, its connection and its cursor."""
    cursor = MagicMock()
    cursor.__enter__ = lambda _: cursor
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.rowcount = rowcount

    conn = MagicMock()
    conn.__enter__ = lambda _: conn
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    pool = MagicMock()
    pool.connection.return_value.__enter__ = lambda _: conn
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool, conn, cursor


def _make_fetcher(entries) -> MagicMock:
    """A fetcher whose stream yields the given bulk entries."""
    fetcher = MagicMock()
    fetcher.stream_data_for_key.return_value = iter(entries)
    return fetcher


def _executed_sql(cursor: MagicMock) -> list[str]:
    """The SQL of every execute() call, in order."""
    return [call.args[0] for call in cursor.execute.call_args_list]


class TestValidRulings:
    """Which bulk entries survive into the table."""

    def test_a_complete_entry_is_kept(self) -> None:
        assert list(_valid_rulings(RULINGS_FIXTURE)) == RULINGS_FIXTURE

    @pytest.mark.parametrize("missing", ["oracle_id", "source", "published_at", "comment"])
    def test_an_entry_missing_a_required_field_is_dropped(self, missing: str) -> None:
        entry = dict(RULINGS_FIXTURE[0])
        del entry[missing]
        assert list(_valid_rulings([entry])) == []

    @pytest.mark.parametrize("blank", ["oracle_id", "source", "published_at", "comment"])
    def test_an_empty_value_counts_as_missing(self, blank: str) -> None:
        """Every column is NOT NULL and an empty comment is not a ruling, so "" is not a value."""
        entry = dict(RULINGS_FIXTURE[0]) | {blank: ""}
        assert list(_valid_rulings([entry])) == []

    def test_a_timestamp_published_at_is_truncated_to_a_date(self) -> None:
        """A future timestamp form must not reach the ::date cast as a timestamp."""
        entry = dict(RULINGS_FIXTURE[0]) | {"published_at": "2004-10-04T00:00:00.000+00:00"}
        assert next(iter(_valid_rulings([entry])))["published_at"] == "2004-10-04"

    def test_only_the_four_columns_are_carried_through(self) -> None:
        """The bulk file's extra keys are dropped rather than travelling in the jsonb parameter."""
        entry = dict(RULINGS_FIXTURE[0]) | {"object": "ruling", "oracle_uri": "https://api.scryfall.com/..."}
        assert set(next(iter(_valid_rulings([entry])))) == {"oracle_id", "source", "published_at", "comment"}


class TestImportRulings:
    """The load itself."""

    def test_streams_the_rulings_bulk_key(self) -> None:
        pool, _, _ = _make_mock_conn_pool()
        fetcher = _make_fetcher(RULINGS_FIXTURE)

        import_rulings(pool, fetcher)

        fetcher.stream_data_for_key.assert_called_once_with(BulkDataKey.RULINGS)

    def test_the_table_is_emptied_before_anything_is_inserted(self) -> None:
        pool, _, cursor = _make_mock_conn_pool()

        import_rulings(pool, _make_fetcher(RULINGS_FIXTURE))

        statements = _executed_sql(cursor)
        assert statements[0] == "DELETE FROM magic.rulings"
        assert all("INSERT INTO magic.rulings" in statement for statement in statements[1:])

    def test_the_load_commits_once_and_only_at_the_end(self) -> None:
        """One transaction, so no reader can observe the table between the DELETE and the reload."""
        pool, conn, _ = _make_mock_conn_pool()

        import_rulings(pool, _make_fetcher(RULINGS_FIXTURE))

        conn.commit.assert_called_once()

    def test_rows_are_written_in_batches(self) -> None:
        pool, _, cursor = _make_mock_conn_pool()
        entries = [dict(RULINGS_FIXTURE[0], comment=f"Ruling {i}.") for i in range(7)]

        with patch.object(rulings_import, "_BATCH_SIZE", 3):
            import_rulings(pool, _make_fetcher(entries))

        inserts = [call for call in cursor.execute.call_args_list if "INSERT" in call.args[0]]
        assert len(inserts) == 3  # 3 + 3 + 1
        assert [len(call.args[1]["rows"].obj) for call in inserts] == [3, 3, 1]

    def test_the_count_is_rows_inserted_not_rows_sent(self) -> None:
        """ON CONFLICT DO NOTHING drops tuples the file repeats; those must not be counted.

        The live file carried 37 such duplicates out of 77,998 entries on 2026-08-11, so reporting
        len(batch) claimed a row total the table did not hold.
        """
        pool, _, _ = _make_mock_conn_pool(rowcount=1)

        loaded = import_rulings(pool, _make_fetcher(RULINGS_FIXTURE))

        assert loaded == 1  # one INSERT of two rows, of which the server accepted one

    def test_an_empty_bulk_file_still_clears_the_table(self) -> None:
        pool, conn, cursor = _make_mock_conn_pool()

        assert import_rulings(pool, _make_fetcher([])) == 0

        assert _executed_sql(cursor) == ["DELETE FROM magic.rulings"]
        conn.commit.assert_called_once()

    def test_a_failure_mid_stream_never_commits(self) -> None:
        """The single transaction is what makes a truncated download safe.

        `stream_data_for_key` raises BulkDataParseError when too little of a large file parses. If
        the DELETE were committed separately, that raise would leave magic.rulings empty until the
        next successful import; inside one transaction it rolls back and readers keep the previous
        rulings.
        """
        pool, conn, _ = _make_mock_conn_pool()

        def exploding_stream() -> Iterator[dict]:
            yield RULINGS_FIXTURE[0]
            msg = "not enough of the file parsed"
            raise ValueError(msg)

        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = exploding_stream()

        with patch.object(rulings_import, "_BATCH_SIZE", 1), pytest.raises(ValueError, match="not enough"):
            import_rulings(pool, fetcher)

        conn.commit.assert_not_called()


class TestImportRulingsQuietly:
    """The wrapper the import sequence calls."""

    def test_a_rulings_failure_does_not_abort_the_import(self, stub_api_resource) -> None:
        """Rulings are the one import step nothing downstream reads, so they must not be fatal."""
        with patch("api.admin_resource._import_rulings", side_effect=RuntimeError("bulk file is garbage")):
            stub_api_resource.admin._import_rulings_quietly()

    def test_a_successful_refresh_calls_the_importer(self, stub_api_resource) -> None:
        with patch("api.admin_resource._import_rulings", return_value=77_961) as importer:
            stub_api_resource.admin._import_rulings_quietly()

        importer.assert_called_once()
