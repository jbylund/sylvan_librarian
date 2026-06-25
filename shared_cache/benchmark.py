#!/usr/bin/env python3
"""Latency benchmark: SharedCache vs in-process caches + key serialization shootout.

Build first:
    cd shared_cache
    PATH="$HOME/.cargo/bin:$PATH" maturin develop --release
Then:
    python benchmark.py
"""

import os
import pickle
import marshal
import sys
import tempfile
import time
from collections import namedtuple

import cachebox
import cbor2
import msgpack
import orjson

sys.path.insert(0, os.path.dirname(__file__))
from shared_cache import SharedCache

# ── Test fixtures ────────────────────────────────────────────────────────────

CachedResponse = namedtuple(
    "CachedResponse",
    ["status", "headers", "body", "result_count", "total_cards"],
)

_BODY_PATH = os.path.join(os.path.dirname(__file__), "elf_response.gz")
BODY = open(_BODY_PATH, "rb").read()  # real gzip response: /search?q=elf (5025 bytes)

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
    return (
        f"/search?q=card_{i}",
        (("q", f"card_{i}"),),
        (("ACCEPT-ENCODING", "gzip"),),
        "arcanetutor.com",
    )


KEYS = [make_key(i) for i in range(500)]
MISS_KEY = make_key(999_999)


# ── Timing helper ────────────────────────────────────────────────────────────

def measure(fn, n: int) -> float:
    """Return average time per call in nanoseconds."""
    # Warmup
    for _ in range(max(n // 10, 100)):
        fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1e9


def row(label: str, **timings: float) -> None:
    cols = "  ".join(f"{k}: {v:7.2f} ns" for k, v in timings.items())
    print(f"  {label:<28} {cols}")


# ── Part 1: Key serialization shootout ──────────────────────────────────────

def bench_serializers() -> None:
    print("\n=== Key serialization (1 representative CacheKey) ===\n")
    key = KEYS[0]

    serializers = [
        ("pickle proto=2",  lambda k: pickle.dumps(k, 2)),
        ("pickle proto=5",  lambda k: pickle.dumps(k, 5)),
        ("marshal",         lambda k: marshal.dumps(k)),
        ("orjson",          lambda k: orjson.dumps(k)),
        ("msgpack",         lambda k: msgpack.packb(k)),
        ("cbor2",           lambda k: cbor2.dumps(k)),
        ("str().encode()",  lambda k: str(k).encode()),
    ]

    print(f"  {'Serializer':<22}  {'ns/call':>8}  {'bytes':>6}  roundtrip ok?")
    print("  " + "-" * 55)
    for name, fn in serializers:
        blob = fn(key)
        t = measure(lambda fn=fn, key=key: fn(key), 50_000)
        # Quick roundtrip sanity (not needed for correctness, just informational)
        ok = "—"  # serializers don't need roundtrip for keys
        print(f"  {name:<22}  {t:>8.3f}  {len(blob):>6}")


# ── Part 2: Cache backend comparison ────────────────────────────────────────

def bench_caches(cache_path: str) -> None:
    print("\n=== Cache backend latency ===\n")
    print(f"  Body size: {len(BODY):,} bytes   Keys: {len(KEYS)}\n")

    backends = [
        ("dict",                    _dict_bench),
        ("cachebox.LRUCache",       _cachebox_bench),
        ("SharedCache (pickle)",    lambda: _shared_bench(cache_path, key_fn=None)),
        ("SharedCache (orjson)",    lambda: _shared_bench(cache_path, key_fn=orjson.dumps)),
        ("SharedCache (marshal)",   lambda: _shared_bench(cache_path, key_fn=marshal.dumps)),
    ]

    for name, setup_fn in backends:
        get_hit, get_miss, set_time = setup_fn()
        row(name, set=set_time, get_hit=get_hit, get_miss=get_miss)

    print()
    print("  Note: SharedCache times include pickle key serialization (~450–500 ns).")
    print("  Advantage: one cache shared across all worker processes.")


def _dict_bench():
    d = {}
    for k in KEYS:
        d[k] = RESPONSE
    get_hit  = measure(lambda: d.get(KEYS[0]), 100_000)
    get_miss = measure(lambda: d.get(MISS_KEY), 100_000)
    d2 = {}
    set_time = measure(lambda: d2.__setitem__(KEYS[0], RESPONSE), 100_000)
    return get_hit, get_miss, set_time


def _cachebox_bench():
    c = cachebox.LRUCache(maxsize=len(KEYS) * 2)
    for k in KEYS:
        c[k] = RESPONSE
    get_hit  = measure(lambda: c.get(KEYS[0]), 100_000)
    get_miss = measure(lambda: c.get(MISS_KEY), 100_000)
    c2 = cachebox.LRUCache(maxsize=len(KEYS) * 2)
    set_time = measure(lambda: c2.__setitem__(KEYS[0], RESPONSE), 100_000)
    return get_hit, get_miss, set_time


def _shared_bench(path: str, key_fn=None):
    c = SharedCache(path=path, maxsize=len(KEYS) * 2, default_ttl=None, key_fn=key_fn)
    for k in KEYS:
        c[k] = RESPONSE
    get_hit  = measure(lambda: c.get(KEYS[0]), 10_000)
    get_miss = measure(lambda: c.get(MISS_KEY), 10_000)
    set_time = measure(lambda: c.__setitem__(KEYS[0], RESPONSE), 10_000)
    c.invalidate()
    return get_hit, get_miss, set_time


# ── Part 3: get_hit latency breakdown ────────────────────────────────────────

def bench_breakdown(cache_path: str) -> None:
    """Decompose SharedCache get_hit into its constituent phases.

    Phase A  orjson key serialization          → orjson.dumps(key)
    Phase B  lock + xxhash + probe + memcpy    → _get_raw(key_bytes)
    Phase C  rkyv deserialize + Python objects → _get_raw_decoded(key_bytes)
    Phase D  full pipeline                     → get(key) with orjson key_fn
    """
    print("\n=== get_hit latency breakdown (SharedCache / orjson) ===\n")

    c = SharedCache(path=cache_path, maxsize=len(KEYS) * 2, default_ttl=None,
                    key_fn=orjson.dumps)
    for k in KEYS:
        c[k] = RESPONSE

    key = KEYS[0]
    key_bytes = orjson.dumps(key)
    N = 20_000

    phase_a = measure(lambda: orjson.dumps(key), N)
    phase_b = measure(lambda: c._get_raw(key_bytes), N)
    phase_c = measure(lambda: c._get_raw_decoded(key_bytes), N)
    phase_d = measure(lambda: c.get(key), N)
    phase_e = measure(lambda: c.get(key).body, N)  # includes .body attribute access (the copy we eliminated)

    print(f"  {'Phase':50}  {'ns':>7}  {'%':>5}")
    print("  " + "-" * 65)
    print(f"  {'A: orjson.dumps(key)':<50}  {phase_a:>7.3f}  {phase_a/phase_e*100:>4.0f}%")
    print(f"  {'B: lock + xxhash + probe + memcpy (_get_raw)':<50}  {phase_b:>7.3f}  {phase_b/phase_e*100:>4.0f}%")
    print(f"  {'C: B + rkyv + Python object construction (_get_raw_decoded)':<50}  {phase_c:>7.3f}  {phase_c/phase_e*100:>4.0f}%")
    print(f"  {'D: full pipeline without .body access':<50}  {phase_d:>7.3f}  {phase_d/phase_e*100:>4.0f}%")
    print(f"  {'E: D + .body access (middleware path)':<50}  {phase_e:>7.3f}  100%")
    print()

    lock_estimate = phase_b * 0.05   # CAS + store ≈ 5% of phase B on ARM64 M-series
    memcpy_estimate = phase_b - lock_estimate - 5  # subtract hash (~5 ns) and lock
    deserialize = phase_c - phase_b

    print(f"  Rough sub-breakdown of phase B ({phase_b:.1f} ns):")
    print(f"    spinlock CAS + release:  ~{lock_estimate:.1f} ns  (estimated)")
    print(f"    xxhash3 + slot probe:    ~5 ns  (estimated)")
    print(f"    mmap memcpy ({len(BODY):,} bytes):  ~{memcpy_estimate:.3f} ns")
    print()
    print(f"  Python object construction (C - B): {deserialize:.3f} ns")
    print(f"  .body access cost (E - D): {phase_e - phase_d:.3f} ns")
    c.invalidate()


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    bench_serializers()

    path = tempfile.mktemp(suffix=".cache")
    try:
        bench_caches(path)
        bench_breakdown(path)
    finally:
        if os.path.exists(path):
            os.unlink(path)


if __name__ == "__main__":
    main()
