"""Tests for CORS middleware."""

from __future__ import annotations

from unittest.mock import MagicMock

from api.middlewares.cors_middleware import CORSMiddleware


class TestCORSMiddleware:
    def test_process_response_sets_wildcard_origin(self) -> None:
        middleware = CORSMiddleware()
        req = MagicMock()
        req.get_header.return_value = "https://example.com"
        resp = MagicMock()

        middleware.process_response(req, resp, None, True)

        resp.set_header.assert_called_once_with("Access-Control-Allow-Origin", "*")

    def test_process_response_no_origin_header(self) -> None:
        middleware = CORSMiddleware()
        req = MagicMock()
        req.get_header.return_value = None
        resp = MagicMock()

        middleware.process_response(req, resp, None, True)

        resp.set_header.assert_called_once_with("Access-Control-Allow-Origin", "*")
