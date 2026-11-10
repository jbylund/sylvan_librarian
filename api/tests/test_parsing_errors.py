"""Tests for parsing error handling and _search routing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import falcon
import pytest

import api.api_resource as api_resource_module
from api.settings import settings
from api.tests.helpers import search_kwargs
from api.tests.support import override_attr

if TYPE_CHECKING:
    from collections.abc import Generator

    from api.api_resource import APIResource


class TestParsingErrorHandling:
    """Parsing errors in _search are surfaced as HTTPBadRequest."""

    @pytest.fixture(autouse=True)
    def _api(self, stub_api_resource: APIResource) -> None:
        self.api_resource = stub_api_resource

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

    @pytest.fixture(autouse=True)
    def _api(self, stub_api_resource: APIResource) -> None:
        self.api_resource = stub_api_resource
        self.api_resource.app_context.engine = MagicMock()

    def test_routes_to_sql_when_engine_empty(self) -> None:
        self.api_resource.app_context.engine.size.return_value = 0
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
        self.api_resource.app_context.engine.size.return_value = 87
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
        self.api_resource.app_context.engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with (
            patch.object(self.api_resource, "_search_engine", side_effect=RuntimeError("engine failed")),
            patch.object(self.api_resource, "_search_sql", return_value=sentinel) as mock_sql,
        ):
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_sql.assert_called_once()
        assert result is sentinel

    def test_falls_back_to_sql_when_engine_panics(self) -> None:
        """A pyo3 panic derives from BaseException, not Exception.

        The SQL fallback exists so an engine failure degrades instead of failing the request; a
        handler catching only Exception let a panic past it and out of the WSGI handler, killing
        the worker. Stood in for by a bare BaseException subclass so the test does not need a
        loaded pyo3 extension (or a way to make it panic on demand) to pin the behaviour.
        """

        class _Panic(BaseException):
            pass

        self.api_resource.app_context.engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with (
            patch.object(self.api_resource, "_search_engine", side_effect=_Panic("engine panicked")),
            patch.object(self.api_resource, "_search_sql", return_value=sentinel) as mock_sql,
        ):
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_sql.assert_called_once()
        assert result is sentinel

    def test_engine_declining_a_query_falls_back_without_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """A query the engine cannot build is a user error, not an alertable engine failure.

        _search_engine logs a RetryableQueryError at info and lets it propagate unwrapped. That
        reaches the SQL fallback without a warning stack trace. Ill-formed regex (``o:/^[/``) is
        rejected before either backend runs — see ``test_invalid_regex_pattern_returns_400``.
        """
        from card_engine import RetryableQueryError  # noqa: PLC0415

        self.api_resource.app_context.engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "o:/draw/"}
        with (
            patch.object(
                self.api_resource,
                "_search_engine",
                side_effect=RetryableQueryError("nope"),
            ),
            patch.object(self.api_resource, "_search_sql", return_value=sentinel) as mock_sql,
            caplog.at_level("INFO"),
        ):
            result = self.api_resource._search(query="o:/draw/", limit=10)

        mock_sql.assert_called_once()
        assert result is sentinel
        assert [r.levelname for r in caplog.records if "falling back to SQL" in r.getMessage()] == ["INFO"]
        assert not [r for r in caplog.records if r.exc_info]

    def test_invalid_regex_pattern_returns_400_before_engine(self) -> None:
        """Ill-formed regex must not reach the engine or SQL fallback."""
        self.api_resource.app_context.engine.size.return_value = 87
        with (
            patch.object(self.api_resource, "_search_engine") as mock_engine,
            patch.object(self.api_resource, "_search_sql") as mock_sql,
            pytest.raises(falcon.HTTPBadRequest) as exc_info,
        ):
            self.api_resource._search(query="o:/^[/", limit=10)

        mock_engine.assert_not_called()
        mock_sql.assert_not_called()
        assert exc_info.value.title == "Invalid Search Query"
        assert "o:/^[/" in exc_info.value.description
        assert "unterminated character set" in exc_info.value.description

    def test_fatal_query_error_returns_400_without_sql_fallback(self) -> None:
        """FatalQueryError subclasses must not retry on PostgreSQL."""
        from card_engine import UnsupportedRegexError  # noqa: PLC0415

        self.api_resource.app_context.engine.size.return_value = 87
        with (
            patch.object(
                self.api_resource,
                "_search_engine",
                side_effect=UnsupportedRegexError("fancy-regex rejected pattern"),
            ),
            patch.object(self.api_resource, "_search_sql") as mock_sql,
            pytest.raises(falcon.HTTPBadRequest) as exc_info,
        ):
            self.api_resource._search(query="o:/(?=.*draw)/", limit=10)

        mock_sql.assert_not_called()
        assert exc_info.value.description == "Search query contains an unsupported regular expression."

    def test_an_unrelated_bad_request_from_the_engine_still_warns_with_a_traceback(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Only RetryableQueryError is a decline — a bare HTTPBadRequest from elsewhere is not.

        Classifying by isinstance(e, falcon.HTTPBadRequest) used to treat any HTTPBadRequest raised
        anywhere in _search_engine's call chain as a benign decline, purely because nothing else
        happened to raise that type. Classifying by isinstance(e, RetryableQueryError) instead is true by
        construction: RetryableQueryError is card_engine's own exception type, so an HTTPBadRequest raised for
        some other reason must still surface as an alertable failure.
        """
        self.api_resource.app_context.engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with (
            patch.object(
                self.api_resource,
                "_search_engine",
                side_effect=falcon.HTTPBadRequest(title="Invalid Search Query", description="nope"),
            ),
            patch.object(self.api_resource, "_search_sql", return_value=sentinel) as mock_sql,
            caplog.at_level("INFO"),
        ):
            result = self.api_resource._search(query="name:opt", limit=10)

        mock_sql.assert_called_once()
        assert result is sentinel
        warnings = [r for r in caplog.records if "falling back to SQL" in r.getMessage()]
        assert [r.levelname for r in warnings] == ["WARNING"]
        assert all(r.exc_info for r in warnings)

    def test_a_real_engine_failure_still_warns_with_a_traceback(self, caplog: pytest.LogCaptureFixture) -> None:
        """Quieting the declined-query case must not quiet an engine that actually broke."""
        self.api_resource.app_context.engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with (
            patch.object(self.api_resource, "_search_engine", side_effect=RuntimeError("engine failed")),
            patch.object(self.api_resource, "_search_sql", return_value=sentinel),
            caplog.at_level("INFO"),
        ):
            self.api_resource._search(query="name:opt", limit=10)

        warnings = [r for r in caplog.records if "falling back to SQL" in r.getMessage()]
        assert [r.levelname for r in warnings] == ["WARNING"]
        assert all(r.exc_info for r in warnings)

    @pytest.mark.parametrize(argnames=["exc"], argvalues=[(KeyboardInterrupt,), (SystemExit,)])
    def test_interpreter_shutdown_signals_still_propagate(self, exc: type[BaseException]) -> None:
        """Widening the catch to BaseException must not swallow Ctrl-C or interpreter exit."""
        self.api_resource.app_context.engine.size.return_value = 87
        with (
            patch.object(self.api_resource, "_search_engine", side_effect=exc()),
            patch.object(self.api_resource, "_search_sql") as mock_sql,
            pytest.raises(exc),
        ):
            self.api_resource._search(query="name:opt", limit=10)
        mock_sql.assert_not_called()

    def test_negative_limit_raises_bad_request(self) -> None:
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query="name:opt", limit=-1)
        assert exc_info.value.title == "Invalid Limit"

    def test_limit_above_ceiling_raises_bad_request(self) -> None:
        ceiling = api_resource_module.pagination_ceiling()
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query="name:opt", limit=ceiling + 1)
        assert exc_info.value.title == "Invalid Limit"

    def test_offset_above_ceiling_raises_bad_request(self) -> None:
        ceiling = api_resource_module.pagination_ceiling()
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query="name:opt", offset=ceiling + 1)
        assert exc_info.value.title == "Invalid Offset"

    def test_raises_service_unavailable_when_setup_incomplete(self) -> None:
        override_attr(self.api_resource.app_context, "setup_complete", lambda: False)
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

    @pytest.fixture(autouse=True)
    def _api(self, stub_api_resource: APIResource) -> None:
        self.api_resource = stub_api_resource

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

    @pytest.fixture(autouse=True)
    def _api(self, stub_api_resource: APIResource) -> None:
        self.api_resource = stub_api_resource
        self.api_resource.app_context.engine = MagicMock()

    def test_total_cards_and_cards_forwarded(self) -> None:
        mock_cards = [{"name": "Lightning Bolt"}, {"name": "Counterspell"}]
        self.api_resource.app_context.engine.query.return_value = (2, mock_cards)
        result = self.api_resource._search_engine(**search_kwargs("type:instant"))
        assert result["total_cards"] == 2
        assert result["cards"] == mock_cards

    def test_engine_called_with_limit(self) -> None:
        self.api_resource.app_context.engine.query.return_value = (0, [])
        self.api_resource._search_engine(**search_kwargs("name:opt", limit=5))
        call_kwargs = self.api_resource.app_context.engine.query.call_args.kwargs
        assert call_kwargs["limit"] == 5

    def test_query_error_propagates_unwrapped(self) -> None:
        """RetryableQueryError reaches the caller as itself, not wrapped in a dedicated exception type.

        _search_engine used to wrap it in a dedicated _EngineDeclinedQueryError; it no longer does,
        since RetryableQueryError is already engine-specific and nothing else in this call chain
        raises it — the wrapper added a type only _search's own handler understood, for no benefit.
        """
        from card_engine import RetryableQueryError  # noqa: PLC0415

        self.api_resource.app_context.engine.query.side_effect = RetryableQueryError("cannot build")
        with pytest.raises(RetryableQueryError):
            self.api_resource._search_engine(**search_kwargs("o:/draw/"))


class TestResultFieldSelection:
    """`fields=` validation on `_search`, independent of which backend serves the request."""

    @pytest.fixture(autouse=True)
    def _api(self, stub_api_resource: APIResource) -> None:
        self.api_resource = stub_api_resource

    def test_unknown_field_raises_bad_request(self) -> None:
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query="", fields=["not_a_real_field"])
        assert exc_info.value.title == "Invalid Fields"

    def test_empty_fields_list_raises_bad_request(self) -> None:
        with pytest.raises(falcon.HTTPBadRequest) as exc_info:
            self.api_resource._search(query="", fields=[])
        assert exc_info.value.title == "Invalid Fields"

    def test_duplicate_fields_are_deduped_in_order(self) -> None:
        resolved = self.api_resource._resolve_result_fields(["price_usd", "name", "price_usd"])
        assert resolved == ["price_usd", "name"]

    def test_none_resolves_to_default_fields(self) -> None:
        from api.api_resource import DEFAULT_RESULT_FIELDS  # noqa: PLC0415

        assert self.api_resource._resolve_result_fields(None) == list(DEFAULT_RESULT_FIELDS)

    def test_every_price_ordering_is_also_a_readable_field(self) -> None:
        """An `order=` you can rank by is an `order=` whose number you can read back.

        Asserted over CardOrdering rather than over a literal list, so a currency added to
        the ordering vocabulary without a matching result field fails here rather than
        shipping a page nobody can interpret.
        """
        from api.api_resource import RESULT_FIELD_COLUMNS  # noqa: PLC0415
        from api.enums import CardOrdering  # noqa: PLC0415

        for ordering in (CardOrdering.USD, CardOrdering.EUR, CardOrdering.TIX):
            assert f"price_{ordering}" in RESULT_FIELD_COLUMNS

    def test_price_fields_resolve_in_order(self) -> None:
        resolved = self.api_resource._resolve_result_fields(["price_usd", "price_eur", "price_tix"])
        assert resolved == ["price_usd", "price_eur", "price_tix"]


@pytest.mark.usefixtures("engine_enabled")
class TestResultFieldRouting:
    """The fields resolved by _search are threaded to whichever backend serves the request."""

    @pytest.fixture(autouse=True)
    def _api(self, stub_api_resource: APIResource) -> None:
        self.api_resource = stub_api_resource
        self.api_resource.app_context.engine = MagicMock()

    def test_fields_passed_to_sql_path(self) -> None:
        self.api_resource.app_context.engine.size.return_value = 0
        sentinel = {"cards": [], "total_cards": 0, "query": ""}
        with patch.object(self.api_resource, "_search_sql", return_value=sentinel) as mock_sql:
            self.api_resource._search(query="", fields=["name", "illustration_id"])
        assert mock_sql.call_args.kwargs["fields"] == ["name", "illustration_id"]

    def test_fields_passed_to_engine_path(self) -> None:
        self.api_resource.app_context.engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": ""}
        with patch.object(self.api_resource, "_search_engine", return_value=sentinel) as mock_engine:
            self.api_resource._search(query="", fields=["name", "price_usd"])
        assert mock_engine.call_args.kwargs["fields"] == ["name", "price_usd"]


class TestEngineFeatureGate:
    """ENABLE_ENGINE gates the engine path: off (default) means the engine is inert."""

    @pytest.fixture(autouse=True)
    def _api(self, stub_api_resource: APIResource) -> Generator[None]:
        self.api_resource = stub_api_resource
        self.api_resource.app_context.engine = MagicMock()
        saved_enable_engine = settings.enable_engine
        yield
        settings.enable_engine = saved_enable_engine

    def _mock_result(self) -> dict:
        return {
            "result": [{"name": "Opt", "total_cards_count": None}, {"total_cards_count": 1}],
            "timings": {},
        }

    def test_disabled_routes_to_sql_even_with_populated_store(self) -> None:
        settings.enable_engine = False
        self.api_resource.app_context.engine.size.return_value = 87
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
        self.api_resource.app_context.engine.size.assert_not_called()
        self.api_resource.app_context.engine.query.assert_not_called()

    def test_disabled_reload_is_a_noop(self) -> None:
        settings.enable_engine = False
        with patch.object(self.api_resource.app_context, "writer_pool") as mock_pool:
            self.api_resource.app_context.reload_engine()
        mock_pool.connection.assert_not_called()
        self.api_resource.app_context.engine.reload.assert_not_called()

    def test_enabled_routes_to_engine(self) -> None:
        settings.enable_engine = True
        self.api_resource.app_context.engine.size.return_value = 87
        sentinel = {"cards": [], "total_cards": 0, "query": "name:opt"}
        with patch.object(self.api_resource, "_search_engine", return_value=sentinel) as mock_engine:
            result = self.api_resource._search(query="name:opt", limit=10)
        mock_engine.assert_called_once()
        assert result is sentinel

    def test_enabled_reload_streams_batches(self) -> None:
        settings.enable_engine = True
        # Empty store, or the populated-store fast path skips the reload.
        self.api_resource.app_context.engine.size.return_value = 0
        self.api_resource.app_context.engine.reload_begin.return_value = True
        batch1, batch2 = [{"card_name": "A"}], [{"card_name": "B"}]
        with patch.object(self.api_resource.app_context, "writer_pool") as mock_pool:
            mock_cursor = MagicMock()
            mock_cursor.fetchmany.side_effect = [batch1, batch2, []]
            mock_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor
            self.api_resource.app_context.reload_engine()
        engine = self.api_resource.app_context.engine
        engine.reload_begin.assert_called_once()
        assert [c.args[0] for c in engine.add_batch.call_args_list] == [batch1, batch2]
        engine.reload_commit.assert_called_once()
        engine.reload_abort.assert_not_called()

    def test_enabled_reload_skips_when_another_worker_published(self) -> None:
        settings.enable_engine = True
        self.api_resource.app_context.engine.size.return_value = 0
        self.api_resource.app_context.engine.reload_begin.return_value = False
        with patch.object(self.api_resource.app_context, "writer_pool") as mock_pool:
            mock_cursor = MagicMock()
            mock_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor
            self.api_resource.app_context.reload_engine()
        self.api_resource.app_context.engine.add_batch.assert_not_called()
        self.api_resource.app_context.engine.reload_commit.assert_not_called()

    def test_enabled_reload_aborts_on_failure(self) -> None:
        settings.enable_engine = True
        self.api_resource.app_context.engine.size.return_value = 0
        self.api_resource.app_context.engine.reload_begin.return_value = True
        self.api_resource.app_context.engine.add_batch.side_effect = RuntimeError("boom")
        with patch.object(self.api_resource.app_context, "writer_pool") as mock_pool:
            mock_cursor = MagicMock()
            mock_cursor.fetchmany.side_effect = [[{"card_name": "A"}], []]
            mock_pool.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = mock_cursor
            with pytest.raises(RuntimeError, match="boom"):
                self.api_resource.app_context.reload_engine()
        self.api_resource.app_context.engine.reload_abort.assert_called_once()
        self.api_resource.app_context.engine.reload_commit.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__])
