"""CORS middleware for controlling cross-origin resource sharing."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import falcon


class CORSMiddleware:
    """Middleware to add CORS headers to all responses."""

    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        """Add CORS headers to the response."""
        del req, resource, req_succeeded
        resp.set_header("Access-Control-Allow-Origin", "*")
