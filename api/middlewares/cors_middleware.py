"""CORS middleware for controlling cross-origin resource sharing."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import falcon

logger = logging.getLogger(__name__)


class CORSMiddleware:
    """Middleware to handle Cross-Origin Resource Sharing (CORS) headers.

    This middleware adds CORS headers to responses, with configurable
    allowed origins based on the environment.
    """

    def __init__(self) -> None:
        """Initialize the CORS middleware with environment-specific settings."""
        # Get environment from environment variable
        environment = os.environ.get("ENVIRONMENT", "dev")

        # Configure allowed origins based on environment
        if environment == "prod":
            # Production: Restrict to specific domains
            self.allowed_origins = [
                "https://arcane-tutor.com",
                "https://www.arcane-tutor.com",
            ]
        else:
            # Development: Allow localhost for testing
            self.allowed_origins = [
                "http://localhost:8080",
                "http://localhost:28080",
                "http://localhost:18080",
                "http://127.0.0.1:8080",
                "http://127.0.0.1:28080",
                "http://127.0.0.1:18080",
            ]

        # Allow additional origins from environment variable (comma-separated)
        extra_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "")
        if extra_origins:
            self.allowed_origins.extend(origin.strip() for origin in extra_origins.split(","))

        logger.info("CORS middleware initialized with allowed origins: %s", self.allowed_origins)

    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,  # noqa: ARG002
        req_succeeded: bool,  # noqa: ARG002
    ) -> None:
        """Add CORS headers to the response if origin is allowed.

        Args:
            req: The request object
            resp: The response object
            resource: The resource handling the request
            req_succeeded: Whether the request succeeded
        """
        origin = req.get_header("Origin")

        # If no origin header, this is not a cross-origin request
        if origin is None:
            return

        # Check if origin is allowed
        if origin in self.allowed_origins:
            resp.set_header("Access-Control-Allow-Origin", origin)
            resp.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            resp.set_header("Access-Control-Allow-Headers", "Content-Type")
            resp.set_header("Access-Control-Max-Age", "86400")  # 24 hours
        else:
            logger.warning("CORS request rejected - origin not in allowed list: %s", origin)
