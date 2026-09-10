"""Tests for the result-count / timing readers shared by the response-side middlewares."""

from __future__ import annotations

import types

import falcon

from api.utils.response_telemetry import note_result_count, result_count_of, search_execute_ms, search_fetch_ms

SQL_TIMINGS = {
    "_meta": {"duration_ms": 30.0},
    "_children": {"execute_query": {"_meta": {"duration_ms": 20.0}}, "fetch_results": {"_meta": {"duration_ms": 10.0}}},
}
ENGINE_TIMINGS = {"parse": {"_meta": {"duration_ms": 0.5}}, "engine_query": {"_meta": {"duration_ms": 7.0}}}


def test_note_and_read_round_trip_on_a_real_response() -> None:
    resp = falcon.Response()
    note_result_count(resp, 5)
    assert result_count_of(resp, {"cards": {"name": ["a"], "cmc": [1]}}) == 5


def test_note_tolerates_a_missing_response() -> None:
    note_result_count(None, 5)  # handlers are also called internally with falcon_response=None


def test_read_falls_back_to_the_media_rows() -> None:
    assert result_count_of(types.SimpleNamespace(context=types.SimpleNamespace()), {"cards": [1, 2]}) == 2
    assert result_count_of(types.SimpleNamespace(), {}) == 0


def test_execute_ms_for_both_paths() -> None:
    assert search_execute_ms(SQL_TIMINGS) == 20.0
    assert search_execute_ms(ENGINE_TIMINGS) == 7.0
    assert search_execute_ms(None) is None
    assert search_execute_ms({}) is None


def test_fetch_ms_only_exists_on_the_sql_path() -> None:
    assert search_fetch_ms(SQL_TIMINGS) == 10.0
    assert search_fetch_ms(ENGINE_TIMINGS) is None
