"""Security headers middleware for adding HTTP security headers to all responses."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import falcon

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware:
    """Middleware to add security headers to all HTTP responses.

    This middleware adds the following security headers:
    - Content-Security-Policy: Controls which resources can be loaded
    - X-Frame-Options: Prevents clickjacking attacks
    - X-Content-Type-Options: Prevents MIME type sniffing
    - X-XSS-Protection: Enables XSS filtering (legacy browsers)
    - Referrer-Policy: Controls referrer information sent with requests
    - Permissions-Policy: Controls browser features and APIs
    """

    def __init__(self) -> None:
        """Initialize the security headers middleware."""
        # Get CDN URL from environment, default to CloudFront URL
        # Expected format: https://domain.cloudfront.net
        # Override with CDN_URL environment variable if using a different CDN
        cdn_url = os.environ.get("CDN_URL", "https://d1hot9ps2xugbc.cloudfront.net")

        self.headers = {
            # Content Security Policy - Allow self, inline styles (needed for app), and CDN
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                f"style-src 'self' 'unsafe-inline' {cdn_url}; "
                f"font-src 'self' {cdn_url}; "
                "img-src 'self' data: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
            # Prevent page from being framed (clickjacking protection)
            "X-Frame-Options": "DENY",
            # Prevent MIME type sniffing
            "X-Content-Type-Options": "nosniff",
            # Enable XSS filtering in older browsers
            "X-XSS-Protection": "1; mode=block",
            # Control referrer information
            "Referrer-Policy": "strict-origin-when-cross-origin",
            # Disable potentially dangerous browser features
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        }
        logger.info("Security headers middleware initialized")

    def process_response(
        self,
        req: falcon.Request,  # noqa: ARG002
        resp: falcon.Response,
        resource: object,  # noqa: ARG002
        req_succeeded: bool,  # noqa: ARG002
    ) -> None:
        """Add security headers to the response.

        Args:
            req: The request object
            resp: The response object
            resource: The resource handling the request
            req_succeeded: Whether the request succeeded
        """
        for header, value in self.headers.items():
            resp.set_header(header, value)
