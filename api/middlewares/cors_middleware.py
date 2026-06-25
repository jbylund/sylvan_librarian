"""CORS middleware for controlling cross-origin resource sharing."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import falcon


class CORSMiddleware:
    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,  # noqa: ARG002
        req_succeeded: bool,  # noqa: ARG002
    ) -> None:
        resp.set_header("Access-Control-Allow-Origin", "*")
