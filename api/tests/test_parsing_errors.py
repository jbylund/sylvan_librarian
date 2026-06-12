"""Tests for parsing error handling and _search routing."""

from __future__ import annotations

import multiprocessing
import time
from unittest.mock import MagicMock, patch

import falcon
import pytest

from api.api_resource import APIResource
from api.settings import settings
from api.tests.helpers import search_kwargs


def _make_api() -> APIResource:
    return APIResource(last_import_time=multiprocessing.Value("d", time.time(), lock=True))


class TestParsingErrorHandling:
    """Parsing errors in _search are surfaced as HTTPBadRequest."""

    def setup_method(self) -> None:
        self.api_resource = _make_api()
        self.api_resource._setup_complete = lambda: True

    def teardown_method(self) -> None:
        if hasattr(self, "api_resource") and self.api_resource:
            self.api_resource._conn_pool.close()

    def test_incomplete_query_raises_bad_request(self) -> None:
        query = "cmc=2 and id="
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query=query)
        assert exc_info.value.title == "Invalid Search Query"
        assert exc_info.value.description == f'Failed to parse query: "{query}"'

    @pytest.mark.parametrize(
        argnames=["query"],
        argvalues=[
            ("cmc=2 and id=",),
            ("name:test and",),
            ("power>1 or",),
            ("cmc=3 and ()",),
        ],
    )
    def test_various_incomplete_queries_raise_bad_request(self, query: str) -> None:
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query=query)
        assert exc_info.value.title == "Invalid Search Query"
        assert exc_info.value.description == f'Failed to parse query: "{query}"'


@pytest.mark.usefixtures("engine_enabled")
class TestSearchRouting:
    """_search routes to _search_sql or _search_engine and validates inputs.

    Routing only happens with the engine feature gate on; the gate itself is
    covered in TestEngineFeatureGate.
    """

    def setup_method(self) -> None:
        self.api_resource = _make_api()
        self.api_resource._setup_complete = lambda: True
        self.api_resource._engine = MagicMock()

    def teardown_method(self) -> None:
        if hasattr(self, "api_resource") and self.api_resource:
            self.api_resource._conn_pool.close()

    def test_routes_to_sql_when_engine_empty(self) -> None:
        self.api_resource._engine.size.return_value = 0
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with (
            patch.object(self.api_resource, "_search_sql", return_value=sentinel) as mock_sql,
            patch.object(self.api_resource, "_search_engine") as mock_engine,
        ):
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_sql.assert_called_once()
        mock_engine.assert_not_called()
        assert result is sentinel

    def test_routes_to_engine_when_engine_has_data(self) -> None:
        self.api_resource._engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with (
            patch.object(self.api_resource, "_search_engine", return_value=sentinel) as mock_engine,
            patch.object(self.api_resource, "_search_sql") as mock_sql,
        ):
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_engine.assert_called_once()
        mock_sql.assert_not_called()
        assert result is sentinel

    def test_falls_back_to_sql_when_engine_raises(self) -> None:
        self.api_resource._engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with (
            patch.object(self.api_resource, "_search_engine", side_effect=RuntimeError("engine failed")),
            patch.object(self.api_resource, "_search_sql", return_value=sentinel) as mock_sql,
        ):
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_sql.assert_called_once()
        assert result is sentinel

    def test_negative_limit_raises_bad_request(self) -> None:
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query="name:opt", limit=-1)
        assert exc_info.value.title == "Invalid Limit"

    def test_raises_service_unavailable_when_setup_incomplete(self) -> None:
        self.api_resource._setup_complete = lambda: False
        with pytest.raises(falcon.HTTPServiceUnavailable) as exc_info:
            self.api_resource._search(query="name:opt")
        assert exc_info.value.title == "Service Unavailable"

    @pytest.mark.parametrize(
        argnames=["query"],
        argvalues=[
            ("t=",),
            ("cmc=2 and id=",),
            ("name:test and",),
            ("power>1 or",),
        ],
    )
    def test_raises_bad_request_for_unparseable_query(self, query: str) -> None:
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query=query)
        assert exc_info.value.title == "Invalid Search Query"
        assert exc_info.value.description == f'Failed to parse query: "{query}"'


class TestSearchSqlDirect:
    """_search_sql result structure and count-row extraction."""

    def setup_method(self) -> None:
        self.api_resource = _make_api()

    def teardown_method(self) -> None:
        if hasattr(self, "api_resource") and self.api_resource:
            self.api_resource._conn_pool.close()

    def _mock_run_query(self, cards: list[dict], total: int) -> dict:
        return {
            "result": [*[{**c, "total_cards_count": None} for c in cards], {"total_cards_count": total}],
            "timings": {},
        }

    def test_total_cards_extracted_from_count_row(self) -> None:
        with patch.object(self.api_resource, "_run_query", return_value=self._mock_run_query([{"name": "Opt"}], total=7)):
            result = self.api_resource._search_sql(**search_kwargs("name:opt"))
        assert result["total_cards"] == 7

    def test_cards_stripped_of_count_column(self) -> None:
        with patch.object(self.api_resource, "_run_query", return_value=self._mock_run_query([{"name": "Opt"}], total=1)):
            result = self.api_resource._search_sql(**search_kwargs("name:opt"))
        assert "total_cards_count" not in result["cards"][0]
        assert result["cards"][0]["name"] == "Opt"

    def test_empty_result_returns_zero_total(self) -> None:
        with patch.object(self.api_resource, "_run_query", return_value=self._mock_run_query([], total=0)):
            result = self.api_resource._search_sql(**search_kwargs("name:opt"))
        assert result["total_cards"] == 0
        assert result["cards"] == []


class TestSearchEngineDirect:
    """_search_engine forwards engine.query results verbatim."""

    def setup_method(self) -> None:
        self.api_resource = _make_api()
        self.api_resource._engine = MagicMock()

    def teardown_method(self) -> None:
        if hasattr(self, "api_resource") and self.api_resource:
            self.api_resource._conn_pool.close()

    def test_total_cards_and_cards_forwarded(self) -> None:
        mock_cards = [{"name": "Lightning Bolt"}, {"name": "Counterspell"}]
        self.api_resource._engine.query.return_value = (2, mock_cards)
        result = self.api_resource._search_engine(**search_kwargs("type:instant"))
        assert result["total_cards"] == 2
        assert result["cards"] == mock_cards

    def test_engine_called_with_limit(self) -> None:
        self.api_resource._engine.query.return_value = (0, [])
        self.api_resource._search_engine(**search_kwargs("name:opt", limit=5))
        call_kwargs = self.api_resource._engine.query.call_args.kwargs
        assert call_kwargs["limit"] == 5


class TestEngineFeatureGate:
    """ENABLE_ENGINE gates the engine path: off (default) means the engine is inert."""

    def setup_method(self) -> None:
        self.api_resource = APIResource(
            last_import_time=multiprocessing.Value("d", time.time(), lock=True),
        )
        self.api_resource._setup_complete = lambda: True
        self.api_resource._engine = MagicMock()
        self._saved_enable_engine = settings.enable_engine

    def teardown_method(self) -> None:
        settings.enable_engine = self._saved_enable_engine
        if hasattr(self, "api_resource") and self.api_resource:
            self.api_resource._conn_pool.close()

    def _mock_result(self) -> dict:
        return {
            "result": [{"name": "Opt", "total_cards_count": None}, {"total_cards_count": 1}],
            "timings": {},
        }

    def test_disabled_routes_to_sql_even_with_populated_store(self) -> None:
        settings.enable_engine = False
        self.api_resource._engine.size.return_value = 87
        with (
            patch.object(self.api_resource, "_run_query", return_value=self._mock_result()),
            patch.object(self.api_resource, "_search_engine") as mock_engine,
        ):
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_engine.assert_not_called()
        assert result["total_cards"] == 1

    def test_disabled_never_touches_the_engine(self) -> None:
        # The gate must short-circuit before any engine call (size() included),
        # so a disabled deployment has zero engine involvement.
        settings.enable_engine = False
        with patch.object(self.api_resource, "_run_query", return_value=self._mock_result()):
            self.api_resource._search(query="name:opt", limit=10)
        self.api_resource._engine.size.assert_not_called()
        self.api_resource._engine.query.assert_not_called()

    def test_disabled_reload_is_a_noop(self) -> None:
        settings.enable_engine = False
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            self.api_resource._reload_engine()
        mock_pool.connection.assert_not_called()
        self.api_resource._engine.reload.assert_not_called()

    def test_enabled_routes_to_engine(self) -> None:
        settings.enable_engine = True
        self.api_resource._engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with patch.object(self.api_resource, "_search_engine", return_value=sentinel) as mock_engine:
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_engine.assert_called_once()
        assert result is sentinel

    def test_enabled_reload_streams_batches(self) -> None:
        settings.enable_engine = True
        # Empty store, or the populated-store fast path skips the reload.
        self.api_resource._engine.size.return_value = 0
        self.api_resource._engine.reload_begin.return_value = True
        batch1, batch2 = [{"card_name": "A"}], [{"card_name": "B"}]
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_cursor = MagicMock()
            mock_cursor.fetchmany.side_effect = [batch1, batch2, []]
            mock_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor
            self.api_resource._reload_engine()
        engine = self.api_resource._engine
        engine.reload_begin.assert_called_once()
        assert [c.args[0] for c in engine.add_batch.call_args_list] == [batch1, batch2]
        engine.reload_commit.assert_called_once()
        engine.reload_abort.assert_not_called()

    def test_enabled_reload_skips_when_another_worker_published(self) -> None:
        settings.enable_engine = True
        self.api_resource._engine.size.return_value = 0
        self.api_resource._engine.reload_begin.return_value = False
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_cursor = MagicMock()
            mock_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor
            self.api_resource._reload_engine()
        self.api_resource._engine.add_batch.assert_not_called()
        self.api_resource._engine.reload_commit.assert_not_called()

    def test_enabled_reload_aborts_on_failure(self) -> None:
        settings.enable_engine = True
        self.api_resource._engine.size.return_value = 0
        self.api_resource._engine.reload_begin.return_value = True
        self.api_resource._engine.add_batch.side_effect = RuntimeError("boom")
        with patch.object(self.api_resource, "_conn_pool") as mock_pool:
            mock_cursor = MagicMock()
            mock_cursor.fetchmany.side_effect = [[{"card_name": "A"}], []]
            mock_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor
            with pytest.raises(RuntimeError, match="boom"):
                self.api_resource._reload_engine()
        self.api_resource._engine.reload_abort.assert_called_once()
        self.api_resource._engine.reload_commit.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__])
