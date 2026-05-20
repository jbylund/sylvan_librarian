"""Tests for caching middleware."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from api.middlewares.caching_middleware import CachingMiddleware


class TestCachingMiddleware:
    """Tests for caching middleware."""

    def _make_req(self, path: str = "/search", uri: str = "/search?q=lightning+bolt") -> MagicMock:
        req = MagicMock()
        req.path = path
        req.relative_uri = uri
        req.params = {"q": "lightning bolt"}
        req.headers = {}
        return req

    def _make_resp(self, status: str = "200 OK") -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp._headers = {}
        return resp

    def test_2xx_response_is_cached(self) -> None:
        """Successful responses should be stored in the cache."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp("200 OK")

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        assert len(cache) == 1

    @pytest.mark.parametrize(
        argnames="status",
        argvalues=[
            "500 Internal Server Error",
            "502 Bad Gateway",
            "503 Service Unavailable",
            "504 Gateway Timeout",
        ],
    )
    def test_5xx_status_not_cached(self, status: str) -> None:
        """5xx responses should be excluded from the cache."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp(status)

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, False)

        assert len(cache) == 0
