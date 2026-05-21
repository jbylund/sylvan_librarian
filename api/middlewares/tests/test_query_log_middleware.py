"""Tests for QueryLogMiddleware.process_response field population."""

from __future__ import annotations

import logging
import queue
import threading
from unittest.mock import MagicMock

from api.middlewares.query_log_middleware import QueryLogMiddleware


def _make_middleware() -> QueryLogMiddleware:
    """Create a QueryLogMiddleware without starting the background writer thread."""
    mw = QueryLogMiddleware.__new__(QueryLogMiddleware)
    mw._queue = queue.Queue(maxsize=10_000)
    mw._stop = threading.Event()
    return mw


def _make_req(path: str = "/search", params: dict | None = None, start_time: float | None = None) -> MagicMock:
    req = MagicMock()
    req.path = path
    req.params = params or {"q": "lightning bolt"}
    req.context = {"_start_time": start_time} if start_time is not None else {}
    return req


def _make_resp(media: object = None, status: str = "200 OK") -> MagicMock:
    resp = MagicMock()
    resp.media = media if media is not None else {"cards": [], "total_cards": 0}
    resp.status = status
    return resp


class TestQueryLogMiddlewareProcessResponse:
    def test_non_search_path_is_ignored(self) -> None:
        mw = _make_middleware()
        mw.process_response(_make_req(path="/healthcheck"), _make_resp(), None, True)
        assert mw._queue.empty()

    def test_trailing_slash_path_is_logged(self) -> None:
        mw = _make_middleware()
        mw.process_response(_make_req(path="/search/"), _make_resp(), None, True)
        assert not mw._queue.empty()

    def test_non_dict_media_logs_warning_and_does_not_enqueue(self, caplog: object) -> None:
        mw = _make_middleware()
        with caplog.at_level(logging.WARNING, logger="api.middlewares.query_log_middleware"):
            mw.process_response(_make_req(), _make_resp(media="not a dict"), None, True)
        assert mw._queue.empty()
        assert "unexpected response media type" in caplog.text

    def test_basic_fields_are_populated(self) -> None:
        mw = _make_middleware()
        req = _make_req(params={"q": "dragon", "orderby": "cmc", "unique": "card"})
        resp = _make_resp(media={"cards": [1, 2, 3], "total_cards": 42})
        mw.process_response(req, resp, None, True)
        entry = mw._queue.get_nowait()
        assert entry["q"] == "dragon"
        assert entry["orderby"] == "cmc"
        assert entry["unique_by"] == "card"
        assert entry["result_count"] == 3
        assert entry["total_cards"] == 42
        assert entry["had_error"] is False

    def test_cache_hit_nulls_db_timings(self) -> None:
        mw = _make_middleware()
        resp = _make_resp(media={
            "cards": [],
            "total_cards": 5,
            "cache_hit": True,
            "inner_timings": {"_children": {"execute_query": {"_meta": {"duration_ms": 99.0}}}},
        })
        mw.process_response(_make_req(), resp, None, True)
        entry = mw._queue.get_nowait()
        assert entry["cache_hit"] is True
        assert entry["execute_ms"] is None
        assert entry["fetch_ms"] is None

    def test_timing_extraction_from_nested_structure(self) -> None:
        mw = _make_middleware()
        resp = _make_resp(media={
            "cards": [],
            "total_cards": 0,
            "inner_timings": {
                "_meta": {"duration_ms": 150.0},
                "_children": {
                    "execute_query": {"_meta": {"duration_ms": 100.0}},
                    "fetch_results": {"_meta": {"duration_ms": 50.0}},
                },
            },
        })
        mw.process_response(_make_req(), resp, None, True)
        entry = mw._queue.get_nowait()
        assert entry["execute_ms"] == 100.0
        assert entry["fetch_ms"] == 50.0

    def test_had_error_when_req_succeeded_false(self) -> None:
        mw = _make_middleware()
        mw.process_response(_make_req(), _make_resp(status="200 OK"), None, False)
        assert mw._queue.get_nowait()["had_error"] is True

    def test_had_error_on_4xx_status_with_req_succeeded_true(self) -> None:
        """Handled falcon.HTTPError leaves req_succeeded=True but status is 4xx."""
        mw = _make_middleware()
        mw.process_response(_make_req(), _make_resp(status="400 Bad Request"), None, True)
        assert mw._queue.get_nowait()["had_error"] is True

    def test_had_error_on_5xx_status(self) -> None:
        mw = _make_middleware()
        mw.process_response(_make_req(), _make_resp(status="500 Internal Server Error"), None, True)
        assert mw._queue.get_nowait()["had_error"] is True

    def test_queue_full_logs_warning_and_does_not_raise(self, caplog: object) -> None:
        mw = _make_middleware()
        for _ in range(mw._queue.maxsize):
            mw._queue.put_nowait({"dummy": True})
        with caplog.at_level(logging.WARNING, logger="api.middlewares.query_log_middleware"):
            mw.process_response(_make_req(), _make_resp(), None, True)
        assert "log queue full" in caplog.text

    def test_stop_exits_drain_thread(self) -> None:
        mw = QueryLogMiddleware()
        assert mw._writer.is_alive()
        mw.stop()
        assert not mw._writer.is_alive()
