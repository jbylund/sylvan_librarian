"""Caching middleware for Falcon API responses."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, NamedTuple, Protocol
from typing import cast as typecast

import orjson
from cachebox import LRUCache

from api.settings import settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    import falcon


class _CacheProtocol(Protocol):
    def get(self, key: bytes) -> object | None: ...
    def __contains__(self, key: object) -> bool: ...
    def __setitem__(self, key: bytes, value: object) -> None: ...


logger = logging.getLogger(__name__)

CacheKey = bytes

# Headers that depend on the request rather than the cached payload: CORSMiddleware varies
# Access-Control-* on the request's Origin and re-sets them on every response, including hits;
# X-Cache describes this request's cache outcome, so a stored "miss" must never be replayed.
_UNCACHEABLE_HEADER_PREFIXES: tuple[str, ...] = ("access-control-", "x-cache")


def cacheable_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return the subset of headers safe to replay on a cache hit for a different request."""
    return [(k, v) for k, v in headers.items() if not k.lower().startswith(_UNCACHEABLE_HEADER_PREFIXES)]


class CachedResponse(NamedTuple):
    """Fully rendered response, detached from the falcon.Response that produced it.

    The body is captured after the compression middleware has run, so it holds the final
    (possibly compressed) bytes for the Accept-Encoding in the cache key. result_count and
    total_cards exist solely so QueryLogMiddleware can log them on cache hits, where the
    media dict is no longer available.
    """

    status: str
    headers: list[tuple[str, str]]
    body: bytes | None
    result_count: int | None
    total_cards: int | None


class CachingMiddleware:
    """Middleware to cache the request and response."""

    def __init__(self: CachingMiddleware, cache: _CacheProtocol | None = None) -> None:
        """Initialize the caching middleware with an optional cache instance.

        Args:
            cache: Optional cache instance. If None, creates an LRUCache with maxsize 10,000.
                Any object supporting .get(), __contains__, and __setitem__ is accepted.
        """
        if cache is None:
            cache = LRUCache(maxsize=10_000)
        self.cache: _CacheProtocol = cache
        logger.info("CachingMiddleware init pid=%d cache=%s", os.getpid(), type(cache).__name__)

    def invalidate(self: CachingMiddleware) -> None:
        """Clear all cached entries, delegating to the inner cache's own method."""
        # Not yet wired into APIResource._clear_caches() — bulk imports do not currently
        # flush the HTTP response cache. Stale responses are served until natural eviction.
        # Wiring this up requires passing the middleware instance into APIResource at
        # construction time (or exposing it through the app). Tracked for a follow-up PR.
        if hasattr(self.cache, "invalidate"):
            self.cache.invalidate()
        elif hasattr(self.cache, "clear"):
            self.cache.clear()

    def _cache_key(self: CachingMiddleware, req: falcon.Request) -> CacheKey:
        cached_headers = [
            "ACCEPT-ENCODING",
        ]
        host = req.headers.get("X-PROXY-HOST") or req.headers.get("HOST")
        host = host.strip().lower() if isinstance(host, str) and host else None
        return orjson.dumps(
            (
                req.relative_uri,
                tuple(sorted(req.params.items())),
                tuple(sorted({k: req.headers.get(k) for k in cached_headers}.items())),
                host,
            )
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
        req.context["cache_key"] = cache_key  # reused by process_response — one serialization per request
        cached: CachedResponse | None = self.cache.get(cache_key)
        if cached is not None:
            if TYPE_CHECKING:
                cached = typecast("CachedResponse", cached)
            resp.complete = True
            resp.status = cached.status
            resp.data = cached.body
            resp._headers.update(cached.headers)
            resp.set_header("X-Cache", "hit")
            req.context["cache_hit"] = True
            req.context["cached_result_count"] = cached.result_count
            req.context["cached_total_cards"] = cached.total_cards
            logger.info("Cache hit pid=%d: %s / %s", os.getpid(), req.relative_uri, resp.status)
            return
        resp.set_header("X-Cache", "miss")
        logger.info("Cache miss pid=%d: %s", os.getpid(), req.relative_uri)

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
        if req.context.get("cache_hit"):
            return
        if resp.status and resp.status.startswith("5"):
            return
        cache_key = req.context.get("cache_key") or self._cache_key(req)
        # __contains__ on SharedCache checks only the cuckoo filter (~0.006% FPR), so a false
        # positive here causes a response to never be cached for that key. A full probe via
        # .get() would eliminate false positives at the cost of a lock + slot scan per miss.
        # The same body-hash fast-path used in set() could be applied here to cheaply confirm
        # before skipping, but the FPR is low enough that the simpler check is worth it.
        if cache_key in self.cache:
            return
        media = resp.media
        is_dict_media = isinstance(media, dict)
        self.cache[cache_key] = CachedResponse(
            status=resp.status,
            headers=cacheable_headers(resp._headers),
            body=resp.render_body(),
            result_count=len(media.get("cards") or []) if is_dict_media else None,
            total_cards=media.get("total_cards") if is_dict_media else None,
        )
        logger.info("Cache updated pid=%d: %s", os.getpid(), req.relative_uri)
