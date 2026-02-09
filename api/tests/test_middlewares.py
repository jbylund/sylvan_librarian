"""Tests for security middleware components."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from api.middlewares.cors_middleware import CORSMiddleware
from api.middlewares.security_headers import SecurityHeadersMiddleware


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


class TestSecurityHeadersMiddleware:
    """Tests for security headers middleware."""

    def test_init_default_cdn_url(self) -> None:
        """Test security headers middleware initialization with default CDN URL."""
        with patch.dict(os.environ, {}, clear=True):
            middleware = SecurityHeadersMiddleware()
            csp = middleware.headers["Content-Security-Policy"]
            assert "https://d1hot9ps2xugbc.cloudfront.net" in csp

    def test_init_custom_cdn_url(self) -> None:
        """Test security headers middleware initialization with custom CDN URL."""
        custom_cdn = "https://custom-cdn.example.com"
        with patch.dict(os.environ, {"CDN_URL": custom_cdn}):
            middleware = SecurityHeadersMiddleware()
            csp = middleware.headers["Content-Security-Policy"]
            assert custom_cdn in csp
            assert "https://d1hot9ps2xugbc.cloudfront.net" not in csp

    def test_all_security_headers_present(self) -> None:
        """Test that all expected security headers are configured."""
        middleware = SecurityHeadersMiddleware()
        expected_headers = [
            "Content-Security-Policy",
            "X-Frame-Options",
            "X-Content-Type-Options",
            "X-XSS-Protection",
            "Referrer-Policy",
            "Permissions-Policy",
        ]
        for header in expected_headers:
            assert header in middleware.headers

    def test_x_frame_options_deny(self) -> None:
        """Test X-Frame-Options is set to DENY."""
        middleware = SecurityHeadersMiddleware()
        assert middleware.headers["X-Frame-Options"] == "DENY"

    def test_x_content_type_options_nosniff(self) -> None:
        """Test X-Content-Type-Options is set to nosniff."""
        middleware = SecurityHeadersMiddleware()
        assert middleware.headers["X-Content-Type-Options"] == "nosniff"

    def test_process_response_adds_all_headers(self) -> None:
        """Test that process_response adds all security headers."""
        middleware = SecurityHeadersMiddleware()
        req = MagicMock()
        resp = MagicMock()

        middleware.process_response(req, resp, None, True)

        # Verify all headers were set
        assert resp.set_header.call_count == len(middleware.headers)
        for header, value in middleware.headers.items():
            resp.set_header.assert_any_call(header, value)

    def test_csp_includes_required_directives(self) -> None:
        """Test CSP includes all required security directives."""
        middleware = SecurityHeadersMiddleware()
        csp = middleware.headers["Content-Security-Policy"]

        required_directives = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]

        for directive in required_directives:
            assert directive in csp
