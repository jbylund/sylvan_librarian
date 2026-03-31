"""Caching middleware for Falcon API responses."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import cast as typecast

from cachebox import LRUCache

from api.settings import settings

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    import falcon

logger = logging.getLogger(__name__)

CacheKey = tuple[str, tuple[tuple, ...], tuple[tuple, ...]]


class CachingMiddleware:
    """Middleware to cache the request and response."""

    def __init__(self: CachingMiddleware, cache: MutableMapping | None = None) -> None:
        """Initialize the caching middleware with an optional cache instance.

        Args:
            cache: Optional cache instance. If None, creates an LRUCache with maxsize 10,000.
        """
        if cache is None:
            cache = LRUCache(maxsize=10_000)
        self.cache: MutableMapping[CacheKey, falcon.Response] = cache

    def _cache_key(self: CachingMiddleware, req: falcon.Request) -> CacheKey:
        cached_headers = [
            "ACCEPT-ENCODING",
        ]
        return (
            req.relative_uri,
            tuple(sorted(req.params.items())),
            tuple(sorted({k: req.headers.get(k) for k in cached_headers}.items())),
        )

    _UNCACHED_PATHS: frozenset[str] = frozenset({x.strip("/") for x in ["/random_search"]})

    def _is_uncached(self: CachingMiddleware, req: falcon.Request) -> bool:
        return req.path.strip("/") in self._UNCACHED_PATHS

    def process_request(self: CachingMiddleware, req: falcon.Request, resp: falcon.Response) -> None:
        """Process incoming request and check for cached response.

        Args:
            req: The incoming request.
            resp: The response object to populate if cache hit.
        """
        if not settings.enable_cache:
            return
        if self._is_uncached(req):
            return

        cache_key = self._cache_key(req)
        cached_value: falcon.Response | None = self.cache.get(cache_key)
        if cached_value is not None:
            if TYPE_CHECKING:
                cached_value = typecast("falcon.Response", cached_value)
            resp.complete = True
            resp.data = cached_value.data
            if isinstance(cached_value.media, dict):
                resp.media = dict(cached_value.media)
                resp.media["cache_hit"] = True
            else:
                resp.media = cached_value.media
            resp._headers.update(cached_value._headers)
            resp.status = cached_value.status
            logger.info("Cache hit: %s / %s response_id: %d", req.relative_uri, resp.status, id(resp))
            return
        logger.info("Cache miss: %s / %s", req.relative_uri, cache_key)

    def process_response(
        self: CachingMiddleware,
        req: falcon.Request,
        resp: falcon.Response,
        resource: object,
        req_succeeded: bool,
    ) -> None:
        """Process outgoing response and cache it if not already cached.

        Args:
            req: The request that generated this response.
            resp: The response to potentially cache.
            resource: The resource that handled the request (unused).
            req_succeeded: Whether the request was successful (unused).
        """
        if not settings.enable_cache:
            return
        if self._is_uncached(req):
            return

        del resource, req_succeeded
        cache_key = self._cache_key(req)
        cached_val = self.cache.get(cache_key)
        if cached_val is None:
            resp.complete = True
            self.cache[cache_key] = resp
            logger.info("Cache updated: %s / %s", req.relative_uri, cache_key)
