"""Tests for TaggerClient retry helpers."""

from __future__ import annotations

import datetime
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
import tenacity

from api.tagger_client import TaggerClient


def _make_retry_state(exc: Exception, attempt_number: int = 1) -> tenacity.RetryCallState:
    """Build a RetryCallState with the given exception and attempt number."""
    state = tenacity.RetryCallState(retry_object=MagicMock(), fn=None, args=(), kwargs={})
    state.attempt_number = attempt_number
    try:
        raise exc
    except Exception:  # noqa: BLE001
        state.set_exception(sys.exc_info())
    return state


def _make_http_error(status_code: int, headers: dict | None = None) -> requests.HTTPError:
    """Build an HTTPError with a mock response."""
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.headers = headers or {}
    return requests.HTTPError(response=response)


class TestTaggerClient:
    """Tests for TaggerClient class methods."""

    # ------------------------------------------------------------------
    # _wait
    # ------------------------------------------------------------------

    def test_wait_returns_retry_after_when_present(self) -> None:
        exc = _make_http_error(429, {"Retry-After": "15"})
        state = _make_retry_state(exc)
        assert TaggerClient._wait(state) == 15.0

    def test_wait_falls_back_to_exponential_when_header_absent(self) -> None:
        exc = _make_http_error(429, {})
        state = _make_retry_state(exc, attempt_number=1)
        result = TaggerClient._wait(state)
        # exponential(multiplier=0.1, min=0.1, max=10) on attempt 1 → 0.1
        assert result == pytest.approx(0.1)

    def test_wait_falls_back_to_exponential_when_header_non_numeric(self) -> None:
        exc = _make_http_error(429, {"Retry-After": "soon"})
        state = _make_retry_state(exc, attempt_number=1)
        result = TaggerClient._wait(state)
        assert result == pytest.approx(0.1)

    def test_wait_falls_back_to_exponential_for_non_http_exception(self) -> None:
        state = _make_retry_state(ConnectionError("timeout"), attempt_number=1)
        result = TaggerClient._wait(state)
        assert result == pytest.approx(0.1)

    def test_wait_exponential_grows_with_attempt_number(self) -> None:
        exc = _make_http_error(503, {})
        result_1 = TaggerClient._wait(_make_retry_state(exc, attempt_number=1))
        result_2 = TaggerClient._wait(_make_retry_state(exc, attempt_number=2))
        assert result_2 > result_1

    def test_wait_parses_http_date_in_future(self) -> None:
        future = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=30)
        header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        exc = _make_http_error(429, {"Retry-After": header})
        state = _make_retry_state(exc)
        result = TaggerClient._wait(state)
        assert 28.0 <= result <= 32.0

    def test_wait_returns_zero_for_http_date_in_past(self) -> None:
        past = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(seconds=10)
        header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        exc = _make_http_error(429, {"Retry-After": header})
        state = _make_retry_state(exc)
        assert TaggerClient._wait(state) == 0.0

    def test_wait_falls_back_to_exponential_for_malformed_date(self) -> None:
        exc = _make_http_error(429, {"Retry-After": "not-a-date-or-number"})
        state = _make_retry_state(exc, attempt_number=1)
        assert TaggerClient._wait(state) == pytest.approx(0.1)

    # ------------------------------------------------------------------
    # _before_sleep
    # ------------------------------------------------------------------

    def test_before_sleep_logs_status_and_retry_after_for_http_error(self) -> None:
        exc = _make_http_error(429, {"Retry-After": "15"})
        state = _make_retry_state(exc, attempt_number=2)
        with patch("api.tagger_client.logger") as mock_logger:
            TaggerClient._before_sleep(state)
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert 429 in call_args.args or 429 in str(call_args)
        assert 2 in call_args.args or "2" in str(call_args)
        assert "15" in str(call_args)

    def test_before_sleep_logs_none_retry_after_when_header_absent(self) -> None:
        exc = _make_http_error(429, {})
        state = _make_retry_state(exc, attempt_number=1)
        with patch("api.tagger_client.logger") as mock_logger:
            TaggerClient._before_sleep(state)
        mock_logger.warning.assert_called_once()
        assert "None" in str(mock_logger.warning.call_args)

    def test_before_sleep_logs_exception_for_non_http_error(self) -> None:
        exc = ConnectionError("timeout")
        state = _make_retry_state(exc, attempt_number=1)
        with patch("api.tagger_client.logger") as mock_logger:
            TaggerClient._before_sleep(state)
        mock_logger.warning.assert_called_once()
        assert "timeout" in str(mock_logger.warning.call_args)
