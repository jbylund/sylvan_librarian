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

# Pre-serialized bytes keys — SharedCache accepts bytes directly.
KEYS_BYTES = [orjson.dumps(k) for k in KEYS]
MISS_KEY_BYTES = orjson.dumps(MISS_KEY)


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
        ("dict",              _dict_bench),
        ("cachebox.LRUCache", _cachebox_bench),
        ("SharedCache",       lambda: _shared_bench(cache_path)),
    ]

    for name, setup_fn in backends:
        get_hit, get_miss, set_time = setup_fn()
        row(name, set=set_time, get_hit=get_hit, get_miss=get_miss)

    print()
    print("  Note: SharedCache times use pre-serialized bytes keys (orjson ~85 ns, caller's cost).")
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


def _shared_bench(path: str):
    c = SharedCache(path=path, maxsize=len(KEYS) * 2, default_ttl=None)
    for kb in KEYS_BYTES:
        c[kb] = RESPONSE
    get_hit  = measure(lambda: c.get(KEYS_BYTES[0]), 10_000)
    get_miss = measure(lambda: c.get(MISS_KEY_BYTES), 10_000)
    set_time = measure(lambda: c.__setitem__(KEYS_BYTES[0], RESPONSE), 10_000)
    c.invalidate()
    return get_hit, get_miss, set_time


# ── Part 3: get_hit latency breakdown ────────────────────────────────────────

def bench_breakdown(cache_path: str) -> None:
    """Decompose SharedCache get_hit into its constituent phases.

    Phase A  orjson key serialization          → orjson.dumps(key)
    Phase B  lock + probe + pin + release      → _probe_only(key_bytes)
    Phase C  B + mmap→PyBytes copy             → _get_raw(key_bytes)
    Phase D  C + rkyv + Python objects         → _get_raw_decoded(key_bytes)
    Phase E  full pipeline                     → get(key) with orjson key_fn
    Phase F  E + .body + .headers access       → middleware path
    """
    print("\n=== get_hit latency breakdown (SharedCache / orjson) ===\n")

    c = SharedCache(path=cache_path, maxsize=len(KEYS) * 2, default_ttl=None)
    for kb in KEYS_BYTES:
        c[kb] = RESPONSE

    key = KEYS[0]
    key_bytes = KEYS_BYTES[0]
    miss_bytes = MISS_KEY_BYTES
    N = 20_000

    phase_a  = measure(lambda: orjson.dumps(key), N)              # caller's key serialization cost
    phase_b  = measure(lambda: c._probe_only(key_bytes), N)       # lock + probe + release only
    phase_c  = measure(lambda: c._get_raw(key_bytes), N)          # + mmap→PyBytes copy outside lock
    phase_d  = measure(lambda: c._get_raw_decoded(key_bytes), N)  # + rkyv + Python objects
    phase_e  = measure(lambda: c.get(key_bytes), N)               # full get() (bytes key)
    phase_f  = measure(lambda: (r := c.get(key_bytes), r.body, r.headers), N)  # + attribute access
    phase_miss = measure(lambda: c.get(miss_bytes), N)            # miss path (filter short-circuit)

    print(f"  {'Phase':50}  {'ns':>7}  {'%':>5}")
    print("  " + "-" * 65)
    print(f"  {'A: orjson.dumps(key)  [caller cost, not in E/F]':<50}  {phase_a:>7.1f}")
    print(f"  {'B: lock + probe + release (_probe_only)':<50}  {phase_b:>7.1f}  {phase_b/phase_f*100:>4.0f}%")
    print(f"  {'C: B + mmap→PyBytes outside lock (_get_raw)':<50}  {phase_c:>7.1f}  {phase_c/phase_f*100:>4.0f}%")
    print(f"  {'D: C + rkyv + Python objects (_get_raw_decoded)':<50}  {phase_d:>7.1f}  {phase_d/phase_f*100:>4.0f}%")
    print(f"  {'E: full get(key_bytes)':<50}  {phase_e:>7.1f}  {phase_e/phase_f*100:>4.0f}%")
    print(f"  {'F: E + .body + .headers (middleware path)':<50}  {phase_f:>7.1f}  100%")
    print(f"  {'miss: get(miss_key_bytes)  [filter short-circuit]':<50}  {phase_miss:>7.1f}")
    print()
    print(f"  Caller key serialize (A):           {phase_a:.1f} ns  (orjson; paid once per request)")
    print(f"  Lock critical section (B):          {phase_b:.1f} ns")
    print(f"  mmap→PyBytes copy (C - B):          {phase_c - phase_b:.1f} ns  ({len(BODY):,} bytes)")
    print(f"  rkyv + object construction (D - C): {phase_d - phase_c:.1f} ns")
    print(f"  .body + .headers access (F - E):    {phase_f - phase_e:.1f} ns")
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
