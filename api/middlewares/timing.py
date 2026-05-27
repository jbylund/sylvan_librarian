"""Timing and profiling middleware for Falcon API requests."""

from __future__ import annotations

import cProfile
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import falcon

logger = logging.getLogger(__name__)


def record_span(req: falcon.Request, name: str, dur_ms: float) -> None:
    """Record a named timing span on the request context for inclusion in Server-Timing."""
    req.context.setdefault("_timing_spans", []).append((name, dur_ms))


class TimingMiddleware:
    """Middleware to log the duration, status, URL, and user agent for each request."""

    def process_request(self: TimingMiddleware, req: falcon.Request, resp: falcon.Response) -> None:
        """Record the start time for request timing.

        Args:
            req: The incoming request.
            resp: The response object (unused).
        """
        del resp
        req.context["_start_time"] = time.monotonic()

    def process_response(
        self: TimingMiddleware,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        """Log request timing and response details.

        Args:
            req: The request that generated this response.
            resp: The response object.
            resource: The resource that handled the request (unused).
            req_succeeded: Whether the request was successful (unused).
        """
        del resource, req_succeeded
        start = req.context.get("_start_time")
        if start is None:
            logger.warning("TimingMiddleware: start time not found for request %s", req.relative_uri)
            return
        duration = time.monotonic() - start
        duration_ms = duration * 1000
        logger.info(
            "[timing] %.2f ms | pid: %d | %s | %s | %s",
            duration_ms,
            os.getpid(),
            resp.status,
            req.relative_uri,
            req.get_header("User-Agent", "-"),
        )
        spans = req.context.get("_timing_spans", [])
        spans.append(("total", duration_ms))
        resp.set_header("Server-Timing", ", ".join(f"{name};dur={dur:.1f}" for name, dur in spans))


class ProfilingMiddleware:
    """Middleware to profile the request and response."""

    def __init__(self: ProfilingMiddleware) -> None:
        """Initialize the profiling middleware."""
        self.datadir = Path("/data/api/")

    def process_request(self: ProfilingMiddleware, req: falcon.Request, resp: falcon.Response) -> None:
        """Start profiling the request.

        Args:
            req: The incoming request.
            resp: The response object (unused).
        """
        del resp
        req.context["_profile"] = profile = cProfile.Profile()
        profile.enable()

    def process_response(
        self: ProfilingMiddleware,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        """Stop profiling and save profile data.

        Args:
            req: The request that generated this response.
            resp: The response object (unused).
            resource: The resource that handled the request (unused).
            req_succeeded: Whether the request was successful (unused).
        """
        del resp, resource, req_succeeded
        profile = req.context.get("_profile")
        if isinstance(profile, cProfile.Profile):
            profile.disable()
            profile_id = int(1000 * time.monotonic())
            profile.dump_stats(self.datadir / f"profile_{req.path}.{profile_id}.prof")
