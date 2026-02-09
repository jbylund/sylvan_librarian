"""Middlewares package for response compression and related utilities.

Exports:
    CompressionMiddleware: Main middleware class for handling response compression.
    SecurityHeadersMiddleware: Middleware for adding security headers.
    CORSMiddleware: Middleware for handling CORS requests.
"""

from __future__ import annotations

from api.middlewares.caching_middleware import CachingMiddleware
from api.middlewares.compression import CompressionMiddleware
from api.middlewares.cors_middleware import CORSMiddleware
from api.middlewares.logging_middleware import LoggingMiddleware
from api.middlewares.security_headers import SecurityHeadersMiddleware
from api.middlewares.timing import ProfilingMiddleware, TimingMiddleware

__all__ = [
    "CORSMiddleware",
    "CachingMiddleware",
    "CompressionMiddleware",
    "LoggingMiddleware",
    "ProfilingMiddleware",
    "SecurityHeadersMiddleware",
    "TimingMiddleware",
]
