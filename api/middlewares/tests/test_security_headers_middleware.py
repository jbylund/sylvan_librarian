"""Tests for security headers middleware."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from api.middlewares.security_headers import SecurityHeadersMiddleware


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
