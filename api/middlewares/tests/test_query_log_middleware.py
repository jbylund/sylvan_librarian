"""Tests for QueryLogMiddleware.process_response field population."""

from __future__ import annotations

import logging
import queue
import threading
import types
from unittest.mock import MagicMock

from api.middlewares.query_log_middleware import QueryLogMiddleware


def _make_middleware() -> QueryLogMiddleware:
    """Create a QueryLogMiddleware without starting the background writer thread."""
    mw = QueryLogMiddleware.__new__(QueryLogMiddleware)
    mw._queue = queue.SimpleQueue()
    mw._pending_upper_bound = 0
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
        """On a hit the response body is rendered bytes — fields come from req.context, not media."""
        mw = _make_middleware()
        req = _make_req()
        req.context.update({"cache_hit": True, "cached_result_count": 7, "cached_total_cards": 5})
        resp = _make_resp()
        resp.media = None
        mw.process_response(req, resp, None, True)
        entry = mw._queue.get_nowait()
        assert entry["cache_hit"] is True
        assert entry["execute_ms"] is None
        assert entry["fetch_ms"] is None
        assert entry["result_count"] == 7
        assert entry["total_cards"] == 5

    def test_timing_extraction_from_nested_structure(self) -> None:
        mw = _make_middleware()
        resp = _make_resp(
            media={
                "cards": [],
                "total_cards": 0,
                "inner_timings": {
                    "_meta": {"duration_ms": 150.0},
                    "_children": {
                        "execute_query": {"_meta": {"duration_ms": 100.0}},
                        "fetch_results": {"_meta": {"duration_ms": 50.0}},
                    },
                },
            }
        )
        mw.process_response(_make_req(), resp, None, True)
        entry = mw._queue.get_nowait()
        assert entry["execute_ms"] == 100.0
        assert entry["fetch_ms"] == 50.0

    def test_engine_path_timing_is_read_from_the_top_level_span(self) -> None:
        """The engine path times its query as a top-level `engine_query`, not under `_children`.

        The old reader only knew the SQL shape, so execute_ms was NULL for every engine-served search.
        """
        mw = _make_middleware()
        resp = _make_resp(
            media={
                "cards": [],
                "total_cards": 0,
                "inner_timings": {
                    "parse": {"_meta": {"duration_ms": 0.4}},
                    "engine_query": {"_meta": {"duration_ms": 12.5}},
                },
            }
        )
        mw.process_response(_make_req(), resp, None, True)
        entry = mw._queue.get_nowait()
        assert entry["execute_ms"] == 12.5
        assert entry["fetch_ms"] is None

    def test_columnar_result_count_is_the_row_count_not_the_field_count(self) -> None:
        """shape=columnar makes `cards` a dict of fields; the handler's stashed row count wins."""
        mw = _make_middleware()
        resp = _make_resp(media={"cards": {"name": ["a", "b", "c"], "cmc": [1, 2, 3]}, "total_cards": 3})
        resp.context = types.SimpleNamespace(result_count=3)
        mw.process_response(_make_req(), resp, None, True)
        assert mw._queue.get_nowait()["result_count"] == 3

    def test_row_shaped_result_count_falls_back_to_len_cards(self) -> None:
        mw = _make_middleware()
        resp = _make_resp(media={"cards": [1, 2, 3, 4], "total_cards": 4})
        resp.context = types.SimpleNamespace()
        mw.process_response(_make_req(), resp, None, True)
        assert mw._queue.get_nowait()["result_count"] == 4

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
        for _ in range(mw._MAX_PENDING):
            mw._queue.put_nowait({"dummy": True})
        mw._pending_upper_bound = mw._MAX_PENDING
        with caplog.at_level(logging.WARNING, logger="api.middlewares.query_log_middleware"):
            mw.process_response(_make_req(), _make_resp(), None, True)
        assert "log queue full" in caplog.text

    def test_pending_upper_bound_only_resyncs_at_cap(self) -> None:
        """The common-case fast path never calls qsize(): the bound is just incremented."""
        mw = _make_middleware()
        for _ in range(5):
            mw.process_response(_make_req(), _make_resp(), None, True)
        assert mw._pending_upper_bound == 5
        assert mw._queue.qsize() == 5

    def test_pending_upper_bound_resyncs_after_writer_drains(self) -> None:
        """Once the optimistic bound hits the cap, it resyncs to the queue's real (smaller) size."""
        mw = _make_middleware()
        mw._pending_upper_bound = mw._MAX_PENDING
        mw._queue.put_nowait({"dummy": True})  # writer thread drained everything but this one
        mw.process_response(_make_req(), _make_resp(), None, True)
        assert mw._pending_upper_bound == 2  # resynced to qsize()==1, then incremented for this put
        assert mw._queue.qsize() == 2

    def test_stop_exits_drain_thread(self) -> None:
        mw = QueryLogMiddleware()
        assert mw._writer.is_alive()
        mw.stop()
        assert not mw._writer.is_alive()
