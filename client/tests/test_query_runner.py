"""Tests for the client query runner."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import requests

from client.query_runner import (
    DEFAULT_API_URL,
    DEFAULT_BATCH_SIZE,
    DEFAULT_QUERY_DELAY,
    _DIM_VALUES,
    fetch_realistic_queries,
    parse_args,
    print_statistics,
    random_query,
    run_query,
)

if TYPE_CHECKING:
    import pytest



class TestRandomQuery:
    def test_returns_string(self) -> None:
        result = random_query()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fragments_are_known(self) -> None:
        all_fragments = {frag for frags in _DIM_VALUES.values() for frag in frags}
        for _ in range(50):
            query = random_query()
            for fragment in query.split():
                assert fragment in all_fragments, f"Unknown fragment {fragment!r} in query {query!r}"

    def test_fragment_count(self) -> None:
        for _ in range(50):
            query = random_query()
            count = len(query.split())
            assert 1 <= count <= 4, f"Expected 1-4 fragments, got {count} in {query!r}"

    def test_fragments_sorted(self) -> None:
        for _ in range(50):
            query = random_query()
            parts = query.split()
            assert parts == sorted(parts), f"Fragments not sorted in {query!r}"


class TestParseArgs:
    def test_module_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_URL", raising=False)
        monkeypatch.delenv("QUERY_DELAY", raising=False)
        monkeypatch.delenv("BATCH_SIZE", raising=False)
        with patch.object(sys, "argv", ["query_runner"]):
            args = parse_args()
        assert args.api_url == DEFAULT_API_URL
        assert args.query_delay == DEFAULT_QUERY_DELAY
        assert args.batch_size == DEFAULT_BATCH_SIZE
        assert args.realistic is False

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_URL", "http://env-host:9090")
        monkeypatch.setenv("QUERY_DELAY", "2.5")
        monkeypatch.setenv("BATCH_SIZE", "100")
        with patch.object(sys, "argv", ["query_runner"]):
            args = parse_args()
        assert args.api_url == "http://env-host:9090"
        assert args.query_delay == 2.5
        assert args.batch_size == 100

    def test_cli_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_URL", "http://env-host:9090")
        monkeypatch.setenv("QUERY_DELAY", "2.5")
        monkeypatch.setenv("BATCH_SIZE", "100")
        with patch.object(
            sys, "argv", ["query_runner", "--api-url", "http://cli-host:7070", "--query-delay", "0.1", "--batch-size", "10"]
        ):
            args = parse_args()
        assert args.api_url == "http://cli-host:7070"
        assert args.query_delay == 0.1
        assert args.batch_size == 10

    def test_realistic_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_URL", raising=False)
        with patch.object(sys, "argv", ["query_runner", "--realistic"]):
            args = parse_args()
        assert args.realistic is True


def _make_session(json_data: dict | None = None, raise_exc: Exception | None = None) -> MagicMock:
    session = MagicMock(spec=requests.Session)
    response = MagicMock()
    if raise_exc:
        session.get.side_effect = raise_exc
    else:
        response.json.return_value = json_data or {}
        response.raise_for_status.return_value = None
        session.get.return_value = response
    return session


class TestRunQuery:
    def test_success(self) -> None:
        session = _make_session({"cards": ["a", "b", "c"], "inner_timings": {"execute_query": 12.5}})
        result = run_query("http://host:8080", "name:bolt", session, orderby="edhrec", unique="card")
        assert result["success"] is True
        assert result["card_count"] == 3
        assert result["execute_ms"] == 12.5
        assert result["elapsed_ms"] >= 0

    def test_success_no_execute_ms(self) -> None:
        session = _make_session({"cards": ["a"]})
        result = run_query("http://host:8080", "type:creature", session, orderby="cmc", unique="card")
        assert result["success"] is True
        assert result["execute_ms"] is None

    def test_success_empty_cards(self) -> None:
        session = _make_session({"cards": []})
        result = run_query("http://host:8080", "name:zzz", session, orderby="cmc", unique="card")
        assert result["success"] is True
        assert result["card_count"] == 0

    def test_http_error(self) -> None:
        session = _make_session(raise_exc=requests.ConnectionError("refused"))
        result = run_query("http://host:8080", "name:bolt", session, orderby="edhrec", unique="card")
        assert result["success"] is False
        assert "refused" in result["error"]
        assert result["elapsed_ms"] >= 0

    def test_hits_correct_endpoint(self) -> None:
        session = _make_session({"cards": []})
        run_query("http://host:8080", "pow>4", session, orderby="cmc", unique="printing")
        session.get.assert_called_once()
        call_kwargs = session.get.call_args
        assert call_kwargs[0][0] == "http://host:8080/search"
        params = call_kwargs[1]["params"]
        assert params["q"] == "pow>4"
        assert params["orderby"] == "cmc"
        assert params["unique"] == "printing"


class TestPrintStatistics:
    def test_empty_is_noop(self) -> None:
        print_statistics([])

    def test_all_failed(self) -> None:
        results = [{"success": False, "error": "timeout", "elapsed_ms": 100.0}]
        print_statistics(results)

    def test_all_success(self) -> None:
        results = [
            {"success": True, "elapsed_ms": 100.0, "card_count": 10, "execute_ms": 50.0},
            {"success": True, "elapsed_ms": 200.0, "card_count": 20, "execute_ms": 100.0},
        ]
        print_statistics(results)

    def test_mixed(self) -> None:
        results = [
            {"success": True, "elapsed_ms": 150.0, "card_count": 5, "execute_ms": None},
            {"success": False, "error": "timeout", "elapsed_ms": 30000.0},
        ]
        print_statistics(results)


class TestFetchRealisticQueries:
    def test_raises_without_pg_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in list(k for k in __import__("os").environ if k.startswith("PG")):
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(RuntimeError, match="No PG\\* env vars"):
            fetch_realistic_queries()
