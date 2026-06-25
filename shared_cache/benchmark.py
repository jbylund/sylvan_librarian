#!/usr/bin/env python3
# ruff: noqa: T201, ANN001, ANN201, ANN202, D103
"""Latency benchmark: SharedCache vs in-process caches + key serialization shootout.

Build first:
    cd shared_cache
    PATH="$HOME/.cargo/bin:$PATH" maturin develop --release
Then:
    python benchmark.py
"""

import marshal
import pickle
import sys
import tempfile
import time
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path

import cachebox
import cbor2
import msgpack
import orjson

sys.path.insert(0, str(Path(__file__).parent))
from shared_cache import SharedCache

# ── Test fixtures ────────────────────────────────────────────────────────────

CachedResponse = namedtuple(
    "CachedResponse",
    ["status", "headers", "body", "result_count", "total_cards"],
)

_BODY_PATH = Path(__file__).parent / "elf_response.gz"
with _BODY_PATH.open("rb") as _f:
    BODY = _f.read()  # real gzip response: /search?q=elf (5025 bytes)

RESPONSE = CachedResponse(
    status="200 OK",
    headers=[
        ("content-type", "application/json"),
        ("content-encoding", "gzip"),
        ("cache-control", "public, max-age=60"),
        ("x-response-time", "12ms"),
    ],
    body=BODY,
    result_count=75,
    total_cards=75321,
)


def make_key(i: int) -> tuple:
    """Build a cache key tuple for card index i."""
    return (
        f"/search?q=card_{i}",
        (("q", f"card_{i}"),),
        (("ACCEPT-ENCODING", "gzip"),),
        "arcanetutor.com",
    )


KEYS = [make_key(i) for i in range(500)]
MISS_KEY = make_key(999_999)

# Pre-serialized bytes keys — SharedCache accepts bytes directly.
KEYS_BYTES = [orjson.dumps(k) for k in KEYS]
MISS_KEY_BYTES = orjson.dumps(MISS_KEY)


# ── Timing helper ────────────────────────────────────────────────────────────


def measure(fn: Callable[[], object], n: int) -> float:
    """Return average time per call in nanoseconds."""
    # Warmup
    for _ in range(max(n // 10, 100)):
        fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1e9


def row(label: str, **timings: float) -> None:
    """Print one benchmark row: label followed by named timing values."""
    cols = "  ".join(f"{k}: {v:7.2f} ns" for k, v in timings.items())
    print(f"  {label:<28} {cols}")


# ── Part 1: Key serialization shootout ──────────────────────────────────────


def bench_serializers() -> None:
    """Benchmark key serialization latency across several libraries."""
    key = KEYS[0]

    serializers = [
        ("pickle proto=2", lambda k: pickle.dumps(k, 2)),
        ("pickle proto=5", lambda k: pickle.dumps(k, 5)),
        ("marshal", marshal.dumps),
        ("orjson", orjson.dumps),
        ("msgpack", msgpack.packb),
        ("cbor2", cbor2.dumps),
        ("str().encode()", lambda k: str(k).encode()),
    ]

    for _name, fn in serializers:
        fn(key)
        measure(lambda fn=fn, key=key: fn(key), 50_000)
        # Quick roundtrip sanity (not needed for correctness, just informational)


# ── Part 2: Cache backend comparison ────────────────────────────────────────


def bench_caches(cache_path: str) -> None:
    """Compare get/set latency across dict, cachebox.LRUCache, and SharedCache."""
    backends = [
        ("dict", _dict_bench),
        ("cachebox.LRUCache", _cachebox_bench),
        ("SharedCache (2 pages)", lambda: _shared_bench(cache_path, n_pages=2)),
        ("SharedCache (3 pages)", lambda: _shared_bench(cache_path, n_pages=3)),
        ("SharedCache (4 pages)", lambda: _shared_bench(cache_path, n_pages=4)),
        ("SharedCache (5 pages)", lambda: _shared_bench(cache_path, n_pages=5)),
    ]

    for name, setup_fn in backends:
        get_hit, get_miss, set_time = setup_fn()
        row(name, set=set_time, get_hit=get_hit, get_miss=get_miss)


def _dict_bench() -> tuple[float, float, float]:
    d = {}
    for k in KEYS:
        d[k] = RESPONSE
    get_hit = measure(lambda: d.get(KEYS[0]), 100_000)
    get_miss = measure(lambda: d.get(MISS_KEY), 100_000)
    d2 = {}
    set_time = measure(lambda: d2.__setitem__(KEYS[0], RESPONSE), 100_000)
    return get_hit, get_miss, set_time


def _cachebox_bench() -> tuple[float, float, float]:
    c = cachebox.LRUCache(maxsize=len(KEYS) * 2)
    for k in KEYS:
        c[k] = RESPONSE
    get_hit = measure(lambda: c.get(KEYS[0]), 100_000)
    get_miss = measure(lambda: c.get(MISS_KEY), 100_000)
    c2 = cachebox.LRUCache(maxsize=len(KEYS) * 2)
    set_time = measure(lambda: c2.__setitem__(KEYS[0], RESPONSE), 100_000)
    return get_hit, get_miss, set_time


def _shared_bench(path: str, n_pages: int = 2) -> tuple[float, float, float]:
    c = SharedCache(path=path, maxsize=len(KEYS) * 2, default_ttl=None, n_pages=n_pages)
    for kb in KEYS_BYTES:
        c[kb] = RESPONSE
    get_hit = measure(lambda: c.get(KEYS_BYTES[0]), 50_000)
    get_miss = measure(lambda: c.get(MISS_KEY_BYTES), 50_000)
    set_time = measure(lambda: c.__setitem__(KEYS_BYTES[0], RESPONSE), 50_000)
    c.invalidate()
    return get_hit, get_miss, set_time


# ── Part 3: set() fast path vs slow path ────────────────────────────────────

RESPONSE2 = CachedResponse(
    status="200 OK",
    headers=[
        ("content-type", "application/json"),
        ("content-encoding", "gzip"),
        ("cache-control", "public, max-age=60"),
        ("x-response-time", "13ms"),
    ],
    body=BODY[::-1],  # different body → different content hash
    result_count=75,
    total_cards=75321,
)


def bench_set_paths(cache_path: str, n_pages: int = 2) -> None:

    # Fast path: key already present with the same content hash → skip rkyv.
    c = SharedCache(path=cache_path, maxsize=len(KEYS) * 2, default_ttl=None, n_pages=n_pages)
    c[KEYS_BYTES[0]] = RESPONSE  # prime the key outside the timed loop
    fast = measure(lambda: c.__setitem__(KEYS_BYTES[0], RESPONSE), 50_000)
    c.invalidate()

    # Slow path: key present but content hash differs → full rkyv serialize + update.
    c2 = SharedCache(path=cache_path, maxsize=len(KEYS) * 2, default_ttl=None, n_pages=n_pages)
    c2[KEYS_BYTES[0]] = RESPONSE  # ensure the key exists first
    responses = [RESPONSE, RESPONSE2]
    i_box = [0]

    def _alternate() -> None:
        c2[KEYS_BYTES[0]] = responses[i_box[0] % 2]
        i_box[0] += 1

    slow = measure(_alternate, 50_000)
    c2.invalidate()

    row("set (fast: same content hash)", set=fast)
    row("set (slow: content hash differs)", set=slow)


# ── Part 4: get_hit latency breakdown ────────────────────────────────────────


def bench_breakdown(cache_path: str, n_pages: int = 2) -> None:
    """Decompose SharedCache get_hit into its constituent phases.

    Phase A  orjson key serialization          → orjson.dumps(key)
    Phase B  lock + probe + pin + release      → _probe_only(key_bytes)
    Phase C  B + mmap→PyBytes copy             → _get_raw(key_bytes)
    Phase D  C + rkyv + Python objects         → _get_raw_decoded(key_bytes)
    Phase E  full pipeline                     → get(key_bytes)
    Phase F  E + .body + .headers access       → middleware path
    """
    c = SharedCache(path=cache_path, maxsize=len(KEYS) * 2, default_ttl=None, n_pages=n_pages)
    for kb in KEYS_BYTES:
        c[kb] = RESPONSE

    key = KEYS[0]
    key_bytes = KEYS_BYTES[0]
    miss_bytes = MISS_KEY_BYTES
    n_iters = 100_000

    measure(lambda: orjson.dumps(key), n_iters)  # caller's key serialization cost
    measure(lambda: c._probe_only(key_bytes), n_iters)  # lock + probe + release only
    measure(lambda: c._get_raw(key_bytes), n_iters)  # + mmap→PyBytes copy outside lock
    measure(lambda: c._get_raw_decoded(key_bytes), n_iters)  # + rkyv + Python objects
    measure(lambda: c.get(key_bytes), n_iters)  # full get() (bytes key)
    measure(lambda: (r := c.get(key_bytes), r.body, r.headers), n_iters)  # + attribute access
    measure(lambda: c.get(miss_bytes), n_iters)  # miss path (filter short-circuit)

    c.invalidate()


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run all benchmark suites and print results."""
    bench_serializers()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = str(Path(tmpdir) / "bench.cache")
        bench_caches(path)
        for n in (2, 3, 4, 5):
            bench_set_paths(path, n_pages=n)
            bench_breakdown(path, n_pages=n)


if __name__ == "__main__":
    main()
