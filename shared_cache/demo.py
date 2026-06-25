#!/usr/bin/env python3
"""Runnable demo of SharedCache showing the same API used by CachingMiddleware.

Build first:
    cd shared_cache
    PATH="$HOME/.cargo/bin:$PATH" maturin develop
Then:
    python demo.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from collections import namedtuple

from shared_cache import SharedCache

CachedResponse = namedtuple(
    "CachedResponse",
    ["status", "headers", "body", "result_count", "total_cards"],
)

RESPONSE = CachedResponse(
    status="200 OK",
    headers=[("content-type", "application/json"), ("content-encoding", "gzip")],
    body=b'{"cards": [{"name": "Lightning Bolt"}], "total_cards": 75321}',
    result_count=1,
    total_cards=75321,
)

KEY = (
    "/search?q=lightning+bolt",
    (("q", "lightning bolt"),),
    (("ACCEPT-ENCODING", "gzip"),),
    "arcanetutor.com",
)

OTHER_KEY = (
    "/search?q=counterspell",
    (("q", "counterspell"),),
    (("ACCEPT-ENCODING", "gzip"),),
    "arcanetutor.com",
)


def main() -> None:
    with tempfile.NamedTemporaryFile(suffix=".cache") as temp_file:
        path = temp_file.name
        cache = SharedCache(path=path, maxsize=1_000, default_ttl=300.0)
        print(f"Opened cache at {path}  size={len(cache)}")

        # --- set via __setitem__ ---
        cache[KEY] = RESPONSE
        print(f"After set: size={len(cache)}")

        # --- get hit ---
        cached = cache.get(KEY)
        assert cached is not None, "expected a hit"
        print(f"Hit:  status={cached.status!r}  result_count={cached.result_count}  body={cached.body[:30]!r}…")

        # --- miss ---
        missed = cache.get(OTHER_KEY)
        print(f"Miss: {missed}")

        # --- __getitem__ raises KeyError on miss ---
        try:
            cache[OTHER_KEY]
        except KeyError:
            print("KeyError raised on __getitem__ miss ✓")

        # --- per-entry TTL override (use a fresh key not already in the cache) ---
        import time
        ttl_key = ("/search?q=ttl_test", (), (), None)
        cache.set(ttl_key, RESPONSE, ttl=0.001)  # 1 ms TTL
        time.sleep(0.01)
        expired = cache.get(ttl_key)
        print(f"After TTL expiry: {expired}")  # None expected

        # --- invalidate flushes everything ---
        cache[KEY] = RESPONSE
        print(f"Before invalidate: size={len(cache)}")
        cache.invalidate()
        print(f"After  invalidate: size={len(cache)}")


if __name__ == "__main__":
    main()
