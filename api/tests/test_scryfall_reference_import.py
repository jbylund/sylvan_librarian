"""Tests for the sets / catalogs / symbology mirror.

Mocked connection pool, in the shape test_tagging_integration.py and test_rulings_import.py use for
import code. The upstream calls are mocked too: these importers talk to api.scryfall.com rather than
to a cached bulk file, so a test that did not stub the fetcher would hit the network on every run.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from api.scryfall_compat.reference_routes import CATALOG_NAMES
from api.scryfall_reference_import import import_catalogs, import_sets, import_symbology

if TYPE_CHECKING:
    from collections.abc import Iterator

# The module-level names `_import_reference_quietly` dispatches through.
_STEPS = ("_import_sets", "_import_catalogs", "_import_symbology")


@contextlib.contextmanager
def _all_steps_stubbed() -> Iterator[dict[str, MagicMock]]:
    """Stub every reference import step at once.

    All three together, never one at a time: `stub_api_resource` carries a real fetcher and a real
    connection pool, so any step left unstubbed performs a live import against api.scryfall.com and
    writes it into the session's shared database.

    Yields:
        The step name to its stub.
    """
    with contextlib.ExitStack() as stack:
        yield {name: stack.enter_context(patch(f"api.admin_resource.{name}")) for name in _STEPS}


SETS_PAYLOAD = {
    "object": "list",
    "has_more": False,
    "data": [
        {"object": "set", "id": "11111111-1111-4111-8111-111111111111", "code": "aaa", "tcgplayer_id": 42},
        {"object": "set", "id": "22222222-2222-4222-8222-222222222222", "code": "bbb"},
    ],
}

SYMBOLOGY_PAYLOAD = {
    "object": "list",
    "data": [
        {"object": "card_symbol", "symbol": "{T}", "svg_uri": "https://svgs.test/t.svg"},
        {"object": "card_symbol", "symbol": "{W}", "svg_uri": "https://svgs.test/w.svg"},
    ],
}


def _make_mock_conn_pool() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return a mock conn_pool, its connection and its cursor."""
    cursor = MagicMock()
    cursor.__enter__ = lambda _: cursor
    cursor.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.__enter__ = lambda _: conn
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    pool = MagicMock()
    pool.connection.return_value.__enter__ = lambda _: conn
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool, conn, cursor


def _catalog_fetcher(total: int = 3) -> MagicMock:
    """A fetcher answering every catalog request with `total` values."""
    fetcher = MagicMock()
    fetcher.fetch_api_json.side_effect = lambda path, **_: {
        "object": "catalog",
        "data": [f"{path}-{index}" for index in range(total)],
    }
    return fetcher


def _rows_of(cursor: MagicMock) -> list[dict[str, Any]]:
    """The parameter sequence handed to the last executemany()."""
    return list(cursor.executemany.call_args.args[1])


class TestImportSets:
    """GET /sets into magic.sets."""

    def test_fetches_the_sets_endpoint(self) -> None:
        pool, _, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SETS_PAYLOAD

        import_sets(pool, fetcher)

        fetcher.fetch_api_json.assert_called_once_with("sets")

    def test_the_table_is_emptied_before_the_insert(self) -> None:
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SETS_PAYLOAD

        import_sets(pool, fetcher)

        assert cursor.execute.call_args_list[0].args[0] == "DELETE FROM magic.sets"

    def test_position_preserves_the_upstream_order(self) -> None:
        """/sets is served in this order, and no field on a Set object reproduces it."""
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SETS_PAYLOAD

        import_sets(pool, fetcher)

        assert [row["position"] for row in _rows_of(cursor)] == [0, 1]
        assert [row["code"] for row in _rows_of(cursor)] == ["aaa", "bbb"]

    def test_a_set_without_a_tcgplayer_id_stores_null(self) -> None:
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SETS_PAYLOAD

        import_sets(pool, fetcher)

        assert [row["tcgplayer_id"] for row in _rows_of(cursor)] == [42, None]

    def test_entries_without_an_id_or_code_are_dropped(self) -> None:
        """Both are NOT NULL, and a set with neither is not addressable by any route."""
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = {"data": [{"object": "set"}, {"id": "x"}, {"code": "y"}]}

        result = import_sets(pool, fetcher)

        assert result["sets_imported"] == 0
        assert _rows_of(cursor) == []

    def test_reports_duration_and_counts(self) -> None:
        pool, _, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SETS_PAYLOAD

        result = import_sets(pool, fetcher)

        assert result["sets_imported"] == 2
        assert result["sets_with_tcgplayer_id"] == 1
        assert "duration_seconds" in result

    def test_the_load_is_one_transaction(self) -> None:
        pool, conn, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SETS_PAYLOAD

        import_sets(pool, fetcher)

        conn.commit.assert_called_once()


class TestImportCatalogs:
    """The twenty /catalog/* endpoints into magic.catalogs."""

    def test_fetches_every_documented_catalog(self) -> None:
        pool, _, _ = _make_mock_conn_pool()
        fetcher = _catalog_fetcher()

        import_catalogs(pool, fetcher)

        requested = [call.args[0] for call in fetcher.fetch_api_json.call_args_list]
        assert requested == [f"catalog/{name}" for name in CATALOG_NAMES]

    def test_reports_what_it_loaded(self) -> None:
        pool, _, _ = _make_mock_conn_pool()

        result = import_catalogs(pool, _catalog_fetcher(total=3))

        assert result["catalogs_imported"] == len(CATALOG_NAMES)
        assert result["catalogs_failed"] == 0
        assert result["values_imported"] == 3 * len(CATALOG_NAMES)

    def test_one_failing_catalog_does_not_lose_the_other_nineteen(self) -> None:
        """A per-catalog failure is skipped, not fatal: nineteen fresh beats aborting the refresh."""
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()

        def answer(path: str, **_: object) -> dict[str, Any]:
            if path == "catalog/word-bank":
                msg = "upstream is down"
                raise RuntimeError(msg)
            return {"data": ["value"]}

        fetcher.fetch_api_json.side_effect = answer

        result = import_catalogs(pool, fetcher)

        assert result["catalogs_failed"] == 1
        assert result["catalogs_imported"] == len(CATALOG_NAMES) - 1
        assert "word-bank" not in {row["name"] for row in _rows_of(cursor)}

    def test_a_failed_catalog_is_not_written_as_empty(self) -> None:
        """Writing an empty row would tell a client Magic has no such vocabulary."""
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.side_effect = RuntimeError("upstream is down")

        import_catalogs(pool, fetcher)

        assert _rows_of(cursor) == []

    def test_a_malformed_payload_counts_as_a_failure(self) -> None:
        pool, _, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = {"data": "not a list"}

        result = import_catalogs(pool, fetcher)

        assert result["catalogs_failed"] == len(CATALOG_NAMES)

    def test_non_string_values_are_dropped(self) -> None:
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = {"data": ["Goblin", None, 7, "Wizard"]}

        import_catalogs(pool, fetcher)

        assert _rows_of(cursor)[0]["entries"].obj == ["Goblin", "Wizard"]


class TestImportSymbology:
    """GET /symbology into magic.card_symbols."""

    def test_fetches_the_symbology_endpoint(self) -> None:
        pool, _, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SYMBOLOGY_PAYLOAD

        import_symbology(pool, fetcher)

        fetcher.fetch_api_json.assert_called_once_with("symbology")

    def test_position_preserves_the_upstream_order(self) -> None:
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SYMBOLOGY_PAYLOAD

        import_symbology(pool, fetcher)

        assert [(row["symbol"], row["position"]) for row in _rows_of(cursor)] == [("{T}", 0), ("{W}", 1)]

    def test_the_table_is_emptied_before_the_insert(self) -> None:
        pool, _, cursor = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SYMBOLOGY_PAYLOAD

        import_symbology(pool, fetcher)

        assert cursor.execute.call_args_list[0].args[0] == "DELETE FROM magic.card_symbols"

    def test_reports_duration_and_count(self) -> None:
        pool, _, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.fetch_api_json.return_value = SYMBOLOGY_PAYLOAD

        result = import_symbology(pool, fetcher)

        assert result["symbols_imported"] == 2
        assert "duration_seconds" in result


class TestImportReferenceQuietly:
    """The wrapper the import sequence calls."""

    @pytest.mark.parametrize("failing", _STEPS)
    def test_any_failing_step_is_swallowed(self, stub_api_resource, failing: str) -> None:
        """Nothing downstream reads these tables, so a bad upstream must not cost the corpus refresh.

        All three steps are stubbed and only the named one is made to fail. Stubbing just the
        failing one would leave the other two to run for real — a live fetch of api.scryfall.com and
        a write into the shared test database — while the test still passed.
        """
        with _all_steps_stubbed() as steps:
            steps[failing].side_effect = RuntimeError("upstream is down")
            stub_api_resource.admin._import_reference_quietly()

    def test_a_failure_in_one_step_still_runs_the_others(self, stub_api_resource) -> None:
        with _all_steps_stubbed() as steps:
            steps["_import_sets"].side_effect = RuntimeError("down")
            stub_api_resource.admin._import_reference_quietly()

        steps["_import_catalogs"].assert_called_once()
        steps["_import_symbology"].assert_called_once()

    def test_all_three_run_on_the_happy_path(self, stub_api_resource) -> None:
        with _all_steps_stubbed() as steps:
            stub_api_resource.admin._import_reference_quietly()

        for step in steps.values():
            step.assert_called_once()
