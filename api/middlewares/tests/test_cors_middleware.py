"""Tests for CORS middleware."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from api.middlewares.cors_middleware import CORSMiddleware


class TestCORSMiddleware:
    """Tests for CORS middleware."""

    def test_init_production_environment(self) -> None:
        """Test CORS middleware initialization in production environment."""
        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            middleware = CORSMiddleware()
            assert "https://arcane-tutor.com" in middleware.allowed_origins
            assert "https://www.arcane-tutor.com" in middleware.allowed_origins
            assert "http://localhost:8080" not in middleware.allowed_origins

    def test_init_development_environment(self) -> None:
        """Test CORS middleware initialization in development environment."""
        with patch.dict(os.environ, {"ENVIRONMENT": "dev"}):
            middleware = CORSMiddleware()
            assert "http://localhost:8080" in middleware.allowed_origins
            assert "http://localhost:28080" in middleware.allowed_origins
            assert "http://127.0.0.1:8080" in middleware.allowed_origins
            assert "https://arcane-tutor.com" not in middleware.allowed_origins

    def test_init_with_extra_origins(self) -> None:
        """Test CORS middleware with additional origins from environment."""
        with patch.dict(os.environ, {"ENVIRONMENT": "dev", "CORS_ALLOWED_ORIGINS": "https://example.com, https://test.com"}):
            middleware = CORSMiddleware()
            assert "https://example.com" in middleware.allowed_origins
            assert "https://test.com" in middleware.allowed_origins

    def test_init_with_empty_extra_origins(self) -> None:
        """Test CORS middleware with empty CORS_ALLOWED_ORIGINS."""
        with patch.dict(os.environ, {"ENVIRONMENT": "dev", "CORS_ALLOWED_ORIGINS": ""}):
            middleware = CORSMiddleware()
            assert "http://localhost:8080" in middleware.allowed_origins

    def test_process_response_allowed_origin(self) -> None:
        """Test CORS headers are added for allowed origin."""
        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            middleware = CORSMiddleware()

        req = MagicMock()
        req.get_header.return_value = "https://arcane-tutor.com"
        resp = MagicMock()

        middleware.process_response(req, resp, None, True)

        resp.set_header.assert_any_call("Access-Control-Allow-Origin", "https://arcane-tutor.com")
        resp.set_header.assert_any_call("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        resp.set_header.assert_any_call("Access-Control-Allow-Headers", "Content-Type")
        resp.set_header.assert_any_call("Access-Control-Max-Age", "86400")

    def test_process_response_disallowed_origin(self) -> None:
        """Test CORS headers are not added for disallowed origin."""
        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            middleware = CORSMiddleware()

        req = MagicMock()
        req.get_header.return_value = "https://malicious-site.com"
        resp = MagicMock()

        middleware.process_response(req, resp, None, True)

        # Verify no CORS headers were set
        resp.set_header.assert_not_called()

    def test_process_response_no_origin_header(self) -> None:
        """Test no CORS headers are added when Origin header is missing."""
        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            middleware = CORSMiddleware()

        req = MagicMock()
        req.get_header.return_value = None
        resp = MagicMock()

        middleware.process_response(req, resp, None, True)

        resp.set_header.assert_not_called()
