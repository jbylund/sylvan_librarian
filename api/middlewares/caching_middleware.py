"""Caching middleware for Falcon API responses."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple
from typing import cast as typecast

from cachebox import LRUCache

from api.settings import settings

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

    import falcon

logger = logging.getLogger(__name__)

CacheKey = tuple[str, tuple[tuple, ...], tuple[tuple, ...], str | None]

# Headers that depend on the request rather than the cached payload: CORSMiddleware varies
# Access-Control-* on the request's Origin and re-sets them on every response, including hits;
# X-Cache describes this request's cache outcome, so a stored "miss" must never be replayed.
_UNCACHEABLE_HEADER_PREFIXES: tuple[str, ...] = ("access-control-", "x-cache")


def cacheable_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return the subset of headers safe to replay on a cache hit for a different request."""
    return {k: v for k, v in headers.items() if not k.lower().startswith(_UNCACHEABLE_HEADER_PREFIXES)}


class CachedResponse(NamedTuple):
    """Fully rendered response, detached from the falcon.Response that produced it.

    The body is captured after the compression middleware has run, so it holds the final
    (possibly compressed) bytes for the Accept-Encoding in the cache key. result_count and
    total_cards exist solely so QueryLogMiddleware can log them on cache hits, where the
    media dict is no longer available.
    """

    status: str
    headers: dict[str, str]
    body: bytes | None
    result_count: int | None
    total_cards: int | None


class CachingMiddleware:
    """Middleware to cache the request and response."""

    def __init__(self: CachingMiddleware, cache: MutableMapping | None = None) -> None:
        """Initialize the caching middleware with an optional cache instance.

        Args:
            cache: Optional cache instance. If None, creates an LRUCache with maxsize 10,000.
        """
        if cache is None:
            cache = LRUCache(maxsize=10_000)
        self.cache: MutableMapping[CacheKey, CachedResponse] = cache

    def _cache_key(self: CachingMiddleware, req: falcon.Request) -> CacheKey:
        cached_headers = [
            "ACCEPT-ENCODING",
        ]
        host = req.headers.get("X-PROXY-HOST") or req.headers.get("HOST")
        return (
            req.relative_uri,
            tuple(sorted(req.params.items())),
            tuple(sorted({k: req.headers.get(k) for k in cached_headers}.items())),
            host,
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
            logger.info("Cache hit: %s / %s response_id: %d", req.relative_uri, resp.status, id(resp))
            return
        resp.set_header("X-Cache", "miss")
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
        if req.context.get("cache_hit"):
            return
        if resp.status and resp.status.startswith("5"):
            return
        cache_key = self._cache_key(req)
        if self.cache.get(cache_key) is not None:
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
        logger.info("Cache updated: %s / %s", req.relative_uri, cache_key)
