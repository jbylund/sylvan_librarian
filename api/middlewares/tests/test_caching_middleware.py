"""Tests for caching middleware."""

from __future__ import annotations

import multiprocessing
import types
from unittest.mock import MagicMock, patch

import orjson
import pytest

from api.middlewares.caching_middleware import CachedResponse, CachingMiddleware


class TestCachingMiddleware:
    """Tests for caching middleware."""

    def _make_req(self, path: str = "/search", uri: str = "/search?q=lightning+bolt", method: str = "GET") -> MagicMock:
        req = MagicMock()
        req.method = method
        req.path = path
        req.relative_uri = uri
        req.params = {"q": "lightning bolt"}
        req.headers = {}
        req.context = {}
        return req

    def _make_resp(self, status: str = "200 OK") -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp._headers = {"content-type": "application/json"}
        resp.media = {"cards": [1, 2, 3], "total_cards": 42}
        resp.render_body.return_value = b"rendered body"
        return resp

    def _cache_key(self, host: str | None = None, method: str = "GET", generation: int = 0) -> bytes:
        return orjson.dumps(
            (
                method,
                "/search?q=lightning+bolt",
                (("q", "lightning bolt"),),
                (("ACCEPT-ENCODING", None),),
                host,
                generation,
            )
        )

    def test_2xx_response_is_cached_as_rendered_bytes(self) -> None:
        """Successful responses should be stored as a CachedResponse, not the Response object."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp("200 OK")

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        assert len(cache) == 1
        cached = cache[self._cache_key()]
        assert isinstance(cached, CachedResponse)
        assert cached.status == "200 OK"
        assert cached.headers == [("content-type", "application/json")]
        assert cached.body == b"rendered body"
        assert cached.result_count == 3
        assert cached.total_cards == 42

    def test_request_dependent_headers_are_not_cached(self) -> None:
        """CORS headers vary on the request's Origin and must not be replayed from cache."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp()
        resp._headers = {
            "content-type": "application/json",
            "access-control-allow-origin": "http://localhost:8080",
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "Content-Type",
            "access-control-max-age": "86400",
            "x-cache": "miss",
        }

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        cached = cache[self._cache_key()]
        assert cached.headers == [("content-type", "application/json")]

    def test_non_dict_media_cached_without_counts(self) -> None:
        """Responses without a media dict (e.g. static files) cache with None counts."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp()
        resp.media = None

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        cached = cache[self._cache_key()]
        assert cached.body == b"rendered body"
        assert cached.result_count is None
        assert cached.total_cards is None

    @pytest.mark.parametrize(
        argnames=["status"],
        argvalues=[
            ("500 Internal Server Error",),
            ("502 Bad Gateway",),
            ("503 Service Unavailable",),
            ("504 Gateway Timeout",),
        ],
    )
    def test_5xx_status_not_cached(self, status: str) -> None:
        """5xx responses should be excluded from the cache."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp(status)

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, False)

        assert len(cache) == 0

    def test_cache_hit_populates_response_from_cached_bytes(self) -> None:
        """A cache hit replays status, headers, and body, and flags the hit on req.context."""
        cached = CachedResponse(
            status="200 OK",
            headers=[("content-type", "application/json"), ("content-encoding", "br")],
            body=b"cached body",
            result_count=7,
            total_cards=99,
        )
        cache = {self._cache_key(): cached}

        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp()
        resp._headers = {}

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_request(req, resp)

        assert resp.complete is True
        assert resp.status == "200 OK"
        assert resp.data == b"cached body"
        assert resp._headers == {"content-type": "application/json", "content-encoding": "br"}
        resp.set_header.assert_called_once_with("X-Cache", "hit")
        assert req.context["cache_hit"] is True
        assert req.context["cached_result_count"] == 7
        assert req.context["cached_total_cards"] == 99

    def test_cache_miss_sets_miss_header(self) -> None:
        """A consulted-but-empty cache should mark the response with X-Cache: miss."""
        middleware = CachingMiddleware(cache={})
        req = self._make_req()
        resp = self._make_resp()

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_request(req, resp)

        resp.set_header.assert_called_once_with("X-Cache", "miss")

    def test_cache_bypass_when_disabled_sets_no_header(self) -> None:
        """When the cache is disabled entirely there is no hit/miss to report."""
        middleware = CachingMiddleware(cache={})
        req = self._make_req()
        resp = self._make_resp()

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = False
            middleware.process_request(req, resp)

        resp.set_header.assert_not_called()

    def test_no_store_response_not_cached(self) -> None:
        """Responses with Cache-Control: no-store must not be stored in the LRU cache."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp()
        resp.get_header.return_value = "no-store"

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        assert len(cache) == 0

    def test_different_hosts_have_separate_cache_entries(self) -> None:
        """Responses for the same query on different hostnames must not share a cache entry."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)

        for host in ("scryfall.crestcourt.com", "tolarian-acade.my"):
            req = self._make_req()
            req.headers = {"X-PROXY-HOST": host}
            resp = self._make_resp()
            with patch("api.middlewares.caching_middleware.settings") as mock_settings:
                mock_settings.enable_cache = True
                middleware.process_response(req, resp, None, True)

        assert len(cache) == 2
        assert cache[self._cache_key("scryfall.crestcourt.com")] is not cache[self._cache_key("tolarian-acade.my")]

    def test_direct_access_uses_host_header_as_cache_key(self) -> None:
        """When accessed directly (no X-Proxy-Host), the Host header discriminates the cache key."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)

        for host in ("localhost:9876", "staging.example.com"):
            req = self._make_req()
            req.headers = {"HOST": host}
            resp = self._make_resp()
            with patch("api.middlewares.caching_middleware.settings") as mock_settings:
                mock_settings.enable_cache = True
                middleware.process_response(req, resp, None, True)

        assert len(cache) == 2
        assert cache[self._cache_key("localhost:9876")] is not cache[self._cache_key("staging.example.com")]

    def test_hit_request_is_not_restored(self) -> None:
        """process_response after a hit must not re-render or overwrite the cached entry."""
        cached = CachedResponse(status="200 OK", headers={}, body=b"cached body", result_count=0, total_cards=0)
        cache = {self._cache_key(): cached}

        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        req.context["cache_hit"] = True
        resp = self._make_resp()

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        assert cache[self._cache_key()] is cached
        resp.render_body.assert_not_called()

    @pytest.mark.parametrize(argnames=["method"], argvalues=[("POST",), ("PUT",), ("PATCH",), ("DELETE",)])
    def test_unsafe_method_response_is_not_cached(self, method: str) -> None:
        """Responses to methods that are not safe and idempotent must never be stored."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req(method=method)
        resp = self._make_resp("200 OK")

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        assert cache == {}

    @pytest.mark.parametrize(argnames=["method"], argvalues=[("POST",), ("PUT",), ("PATCH",), ("DELETE",)])
    def test_unsafe_method_does_not_consult_the_cache(self, method: str) -> None:
        """An unsafe method must not be served a stored response, whatever is in the cache."""
        cached = CachedResponse(status="200 OK", headers={}, body=b"cached body", result_count=0, total_cards=0)
        cache = {self._cache_key(): cached, self._cache_key(method=method): cached}

        middleware = CachingMiddleware(cache=cache)
        req = self._make_req(method=method)
        resp = MagicMock()

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_request(req, resp)

        assert req.context.get("cache_hit") is None
        assert resp.complete is not True

    def test_method_is_part_of_the_cache_key(self) -> None:
        """The same URL under two methods must not share an entry.

        A route answers only the methods it declares, so its response depends on the method: a
        refused POST returns a 405 that must never be replayed to a GET of the same URL.
        """
        cache = {}
        middleware = CachingMiddleware(cache=cache)

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(self._make_req(method="GET"), self._make_resp(), None, True)
            middleware.process_response(self._make_req(method="HEAD"), self._make_resp(), None, True)

        assert sorted(cache) == sorted([self._cache_key(method="GET"), self._cache_key(method="HEAD")])

    def test_cache_generation_is_part_of_the_key(self) -> None:
        """Bumping the shared generation makes every earlier entry unreachable.

        Imports bump `AppContext.cache_generation` rather than reaching into every worker's response
        cache; the middleware honours that by keying on the current value, the same way the in-process
        query caches do. Without it a /search response stayed servable until LRU eviction -- indefinitely
        on a quiet deployment -- after the corpus underneath it had changed.
        """
        cache = {}
        generation = multiprocessing.Value("i", 0, lock=True)
        middleware = CachingMiddleware(cache=cache, cache_generation=generation)

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(self._make_req(), self._make_resp(), None, True)

            hit_req = self._make_req()
            middleware.process_request(hit_req, self._make_resp())
            assert hit_req.context.get("cache_hit") is True

            with generation.get_lock():
                generation.value += 1

            miss_req = self._make_req()
            miss_resp = self._make_resp()
            middleware.process_request(miss_req, miss_resp)

        assert miss_req.context.get("cache_hit") is None
        miss_resp.set_header.assert_called_once_with("X-Cache", "miss")
        assert set(cache) == {self._cache_key(generation=0)}

    def test_no_generation_keys_on_a_constant(self) -> None:
        """A middleware built without the shared counter still produces a stable key."""
        middleware = CachingMiddleware(cache={})
        assert middleware._cache_key(self._make_req()) == self._cache_key(generation=0)

    def test_columnar_result_count_comes_from_the_handler(self) -> None:
        """For shape=columnar, len(media["cards"]) is the field count; the stashed row count is stored."""
        cache = {}
        middleware = CachingMiddleware(cache=cache)
        req = self._make_req()
        resp = self._make_resp()
        resp.media = {"cards": {"name": ["a", "b", "c"], "cmc": [1, 2, 3]}, "total_cards": 3}
        resp.context = types.SimpleNamespace(result_count=3)

        with patch("api.middlewares.caching_middleware.settings") as mock_settings:
            mock_settings.enable_cache = True
            middleware.process_response(req, resp, None, True)

        assert cache[self._cache_key()].result_count == 3
