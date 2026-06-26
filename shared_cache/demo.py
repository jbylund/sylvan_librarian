#!/usr/bin/env python3
"""Runnable demo of SharedCache showing the same API used by CachingMiddleware.

Build first:
    cd shared_cache
    PATH="$HOME/.cargo/bin:$PATH" maturin develop
Then:
    python demo.py
"""

import contextlib
import json
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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


def make_key(parts) -> bytes:
    return json.dumps(parts, separators=(",", ":")).encode()


KEY = make_key((
    "/search?q=lightning+bolt",
    (("q", "lightning bolt"),),
    (("ACCEPT-ENCODING", "gzip"),),
    "arcanetutor.com",
))

OTHER_KEY = make_key((
    "/search?q=counterspell",
    (("q", "counterspell"),),
    (("ACCEPT-ENCODING", "gzip"),),
    "arcanetutor.com",
))


def main() -> None:
    """Exercise SharedCache: set, get hit, miss, __getitem__, TTL, len, invalidate."""
    with tempfile.NamedTemporaryFile(suffix=".cache") as temp_file:
        path = temp_file.name
        cache = SharedCache(path=path, maxsize=1_000, default_ttl=300.0)

        # --- set via __setitem__ ---
        cache[KEY] = RESPONSE

        # --- get hit ---
        cached = cache.get(KEY)
        if cached is None:
            msg = "expected a cache hit but got None"
            raise RuntimeError(msg)

        # --- miss ---
        cache.get(OTHER_KEY)

        # --- __getitem__ raises KeyError on miss ---
        with contextlib.suppress(KeyError):
            cache[OTHER_KEY]

        # --- per-entry TTL override (use a fresh key not already in the cache) ---
        ttl_key = make_key(("/search?q=ttl_test", (), (), None))
        cache.set(ttl_key, RESPONSE, ttl=0.001)  # 1 ms TTL
        time.sleep(0.01)
        cache.get(ttl_key)

        # --- invalidate flushes everything ---
        cache[KEY] = RESPONSE
        cache.invalidate()


if __name__ == "__main__":
    main()
