"""Caching middleware for Falcon API responses."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, NamedTuple, Protocol
from typing import cast as typecast

import orjson
from cachebox import LRUCache

from api.settings import settings
from api.utils.response_telemetry import result_count_of

if TYPE_CHECKING:
    from collections.abc import Mapping
    from multiprocessing.sharedctypes import Synchronized

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

# Only the safe, idempotent methods are cached, and the method is part of the key. Both are needed:
# a route answers only the methods it declares, so a response is method-dependent, and a POST that a
# route refuses would otherwise store a 405 that the next GET to the same URL would be served.
# Responses to POST/PUT/DELETE are not replayable in the first place.
CACHEABLE_METHODS = frozenset({"GET", "HEAD"})


def cacheable_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return the subset of headers safe to replay on a cache hit for a different request."""
    return [(k, v) for k, v in headers.items() if not k.lower().startswith(_UNCACHEABLE_HEADER_PREFIXES)]


def status_is_cacheable(status: str) -> bool:
    """Check whether a response status is safe to store and replay.

    5xx is a transient failure, not a repeatable answer. Most 4xx isn't cacheable either, on
    different grounds: the path space behind a 404 is effectively unbounded (bots, scanners,
    typos), so almost none of it repeats -- caching it buys nothing while spending LRU slots that
    would otherwise hold a reusable /search response. It also sidesteps a real hazard for free: a
    404's content can depend on the caller (see APIResource._raise_not_found, which serves
    admin-authenticated callers a different route listing), and the cache key here has no notion
    of that -- caching it would need to partition by caller to avoid leaking or masking that
    listing across callers. Not caching 4xx at all makes that a non-issue rather than something to
    get right.

    400 is the one exception: it's a deterministic function of the request shape already in the
    cache key (a query parameter that fails type coercion, e.g. ParamCoercionError), never varies
    by caller, and -- unlike a 404's effectively unbounded path space -- a client retrying the same
    malformed request genuinely repeats the same key.

    Args:
        status: A response's HTTP status line, e.g. "404 Not Found".

    Returns:
        True if the response may be cached.
    """
    if status.startswith("5"):
        return False
    if status.startswith("400"):
        return True
    return not status.startswith("4")


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

    def __init__(
        self: CachingMiddleware,
        cache: _CacheProtocol | None = None,
        cache_generation: Synchronized | None = None,
    ) -> None:
        """Initialize the caching middleware with an optional cache instance.

        Args:
            cache: Optional cache instance. If None, creates an LRUCache with maxsize 10,000.
                Any object supporting .get(), __contains__, and __setitem__ is accepted.
            cache_generation: The shared counter `AppContext.bump_cache_generation` increments after
                every write to magic.cards (see api/app_context.py). Its current value is part of
                every cache key, so an import makes every entry stored before it unreachable in
                every worker at once -- the same mechanism the in-process query caches use. None
                means the key carries a constant 0 and nothing ever invalidates it, which is only
                right for a test that builds the middleware on its own.
        """
        if cache is None:
            cache = LRUCache(maxsize=10_000)
        self.cache: _CacheProtocol = cache
        self._cache_generation = cache_generation
        logger.info("CachingMiddleware init pid=%d cache=%s", os.getpid(), type(cache).__name__)

    def invalidate(self: CachingMiddleware) -> None:
        """Clear all cached entries, delegating to the inner cache's own method.

        Not what an import calls: imports bump the shared cache generation, which is part of every
        key, so the entries written before the import are simply never looked up again and age out
        under the cache's TTL. This is for a caller that holds the instance and wants the memory back
        now rather than at expiry.
        """
        if hasattr(self.cache, "invalidate"):
            self.cache.invalidate()
        elif hasattr(self.cache, "clear"):
            self.cache.clear()

    def _generation(self: CachingMiddleware) -> int:
        """The current cache generation, or 0 when no shared counter was given."""
        if self._cache_generation is None:
            return 0
        return self._cache_generation.value

    def _cache_key(self: CachingMiddleware, req: falcon.Request) -> CacheKey:
        cached_headers = [
            "ACCEPT-ENCODING",
        ]
        host = req.headers.get("X-PROXY-HOST") or req.headers.get("HOST")
        host = host.strip().lower() if isinstance(host, str) and host else None
        return orjson.dumps(
            (
                req.method,
                req.relative_uri,
                tuple(sorted(req.params.items())),
                tuple(sorted({k: req.headers.get(k) for k in cached_headers}.items())),
                host,
                self._generation(),
            )
        )

    def process_request(self: CachingMiddleware, req: falcon.Request, resp: falcon.Response) -> None:
        """Process incoming request and check for cached response.

        Args:
            req: The incoming request.
            resp: The response object to populate if cache hit.
        """
        if not settings.enable_cache:
            return
        if req.method not in CACHEABLE_METHODS:
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
            logger.debug("Cache hit pid=%d: %s / %s", os.getpid(), req.relative_uri, resp.status)
            return
        resp.set_header("X-Cache", "miss")
        # The X-Cache header is the machine-readable hit/miss signal; nothing consumes these lines.
        logger.debug("Cache miss pid=%d: %s", os.getpid(), req.relative_uri)

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
        if req.method not in CACHEABLE_METHODS:
            return

        del resource, req_succeeded
        if req.context.get("cache_hit"):
            return
        if resp.status and not status_is_cacheable(resp.status):
            return
        if "no-store" in (resp.get_header("Cache-Control") or ""):
            return
        cache_key = req.context.get("cache_key")
        if cache_key is None:
            cache_key = self._cache_key(req)
        # __contains__ on SharedCache does a full slot probe (filter check + lock + active-page
        # probe + lock-free sealed-page probes) — no false positives. On lock timeout it returns
        # False, which causes a redundant set() that also silently drops under contention.
        if cache_key in self.cache:
            return
        media = resp.media
        is_dict_media = isinstance(media, dict)
        self.cache[cache_key] = CachedResponse(
            status=resp.status,
            headers=cacheable_headers(resp._headers),
            body=resp.render_body(),
            result_count=result_count_of(resp, media) if is_dict_media else None,
            total_cards=media.get("total_cards") if is_dict_media else None,
        )
        logger.debug("Cache updated pid=%d: %s", os.getpid(), req.relative_uri)
