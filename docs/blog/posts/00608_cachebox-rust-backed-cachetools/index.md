---
title: "Rust-Backed Caching Cut TTL Overhead by 6× with One Import Swap"
date: 2027-03-13
publishDate: 2027-03-13
tags: ["python", "rust", "performance", "caching"]
summary: "Swapping cachetools for cachebox (a Rust-backed Python cache) required only a thin key-hashing compatibility wrapper. TTLCache operations went from 537 ns/op to 90 ns/op — a 6× gain for a one-line dependency change."
---

The GitHub issue said "let's investigate switching" and linked to a library I had not heard of.
[Cachebox](https://github.com/awolverp/cachebox) describes itself as the fastest memoizing and caching Python library written in Rust.
Arcane Tutor was already using [cachetools](https://cachetools.readthedocs.io/) for its LRU and TTL caches — the APIs looked nearly identical.
I assumed there would be a catch.

There mostly wasn't.

## Why the Caches Matter

Arcane Tutor has three hot cache sites:

1. `get_where_clause` — parses a search query into a SQL WHERE clause. Up to 10,000 entries in an
   LRU cache; this is the most-hit function in the request path.
2. `CachingMiddleware` — full HTTP response cache, keyed by URL + headers. Holds up to 10,000
   entries as `LRUCache`.
3. `_search` — per-worker TTL cache for search results; production uses a 60-second TTL.

The underlying library for all three was `cachetools`.
It is pure Python — readable, correct, and widely deployed.
The question was whether the overhead showed up under load.

## Where Cachebox Gets Its Speed

Cachebox is a Python extension module backed by Rust.
It uses [hashbrown](https://github.com/rust-lang/hashbrown), Rust's implementation of Google's SwissTable hash map, as the underlying data structure.
SwissTable uses SIMD probing to scan multiple slots in a single instruction; Python's built-in dict uses a different collision-resolution strategy that pays a higher per-probe cost.
The Python layer is thin PyO3 bindings — the cache operations themselves happen without the interpreter loop.

The gap is widest for `TTLCache`.
Every insert into cachetools' `TTLCache` calls `time.monotonic()` in Python, then stores the timestamp in a separate Python dict alongside the value dict.
That is two Python dict operations per insert, plus the interpreter overhead for the function call.
Cachebox records the timestamp inside a Rust struct alongside the value — one write, no separate data structure.

I ran benchmarks on the deployment hardware (M5 Max, Python 3.13.7, cachebox 6.0.0,
cachetools 7.1.4, 50,000 ops, 5,000-op warmup, median of 7 rounds):

| Operation          | cachetools | cachebox | Ratio  |
|--------------------|-----------|----------|--------|
| LRUCache insert    | 240 ns/op |  57 ns/op | 4.2×  |
| LRUCache hit       | 124 ns/op |  58 ns/op | 2.1×  |
| LRUCache miss      |  58 ns/op |  26 ns/op | 2.2×  |
| TTLCache insert    | 537 ns/op |  90 ns/op | 6.0×  |
| TTLCache hit       | 528 ns/op |  62 ns/op | 8.5×  |

The TTL numbers dominate because Python's `time.monotonic()` overhead appears on every operation, not just inserts — a hit also checks whether the entry has expired.

One note on the upstream benchmarks: the [cachebox-benchmark repository](https://github.com/awolverp/cachebox-benchmark) reports a large miss-path penalty for cachebox LRUCache (4,166 ns/op vs. 120 ns/op for cachetools).
That did not reproduce on M5 Max — both libraries returned misses in under 60 ns, with cachebox faster.
The upstream benchmarks appear to have been run on a different platform or Python version where the PyO3 exception-roundtrip cost dominates.
Measure on your own hardware.

## The Compatibility Wrapper

The APIs are close but not identical.
The main surface difference:

**Key function signature.** `cachetools.cached` accepts `key=` as a callable with signature
`(*args, **kwargs) → hashable`. `cachebox.cached` accepts `key_maker=` with signature
`(args: tuple, kwds: dict) → hashable`.

Arcane Tutor has a thin wrapper around both decorators called `cached` (it adds a `settings.enable_cache` runtime flag so caching can be toggled without restarting).
The wrapper needed one line changed:

```python
# Before
key = key or cachetools.keys.hashkey
cached_func = cachetools_cached(cache, key=key)(func)

# After
key_maker = key or cachebox.make_hash_key
cached_func = cachebox_cached(cache, key_maker=key_maker)(func)
```

Every call site that passed a custom key function also needed its lambda updated — the argument names changed from `(_self, *args, **kwargs)` to `(args, kwds)`:

```python
# Before (cachetools)
@cached(cache={}, key=lambda _self, *args, **kwargs: (args, tuple(sorted(kwargs.items()))))

# After (cachebox)
@cached(cache={}, key=lambda args, kwds: (args, tuple(sorted(kwds.items()))))
```

One more edge case: cachebox requires `ttl > 0`.
The codebase had one place where a disabled cache was expressed as `TTLCache(maxsize=1, ttl=0)`.
That became `LRUCache(maxsize=1)` — a 1-slot LRU effectively evicts on every insert, which is the right behavior for "cache disabled."

The full diff is in [PR #383](https://github.com/jbylund/arcane_tutor/pull/383).
Seven files changed, five of them import updates.
The [`cached` wrapper](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/api_resource.py#L330-L350) is the only place that touches cachebox directly — every call site goes through the same decorator interface it always did.

## A Path Not Taken

Before settling on a clean API migration, I looked at whether `cachebox.make_hash_key` could serve as a drop-in for `cachetools.keys.hashkey` at each call site without touching the wrapper.
The signatures are different enough that it cannot — `hashkey` unpacks `*args, **kwargs` at the call site whereas `make_hash_key` expects `(tuple, dict)`.
A per-call-site shim would have worked, but it adds a function call per cache operation for no benefit.
Updating the wrapper once and propagating the signature change to the four custom key lambdas was the right path.

## What This Does Not Cover

Cachebox ships pre-built wheels for Linux x86_64, Linux arm64, and macOS arm64.
If your deployment target is not in that set — Alpine Linux with musl libc, for example — you need a Rust toolchain at build time.
Cachetools has no such constraint.
Before switching, verify that wheels are available for your target environment; the [PyPI page](https://pypi.org/project/cachebox/) lists what is published for each release.

The API is also younger and narrower than cachetools.
The cachebox 6.0.0 release pinned the public interface; earlier versions had breaking changes between minor releases.
If you depend on cachetools features outside `LRUCache`, `TTLCache`, and `cached` — `LFUCache`, `RRCache`, custom eviction callbacks — check the cachebox docs before switching.

Cachebox also does not document per-call thread-safety guarantees the way cachetools does.
This was not a concern for Arcane Tutor: Bjoern runs multiple worker processes, and each worker holds its own cache instance — no sharing across processes, so no concurrent-access question.
If your deployment model uses threads sharing a cache object, test the behavior rather than assuming cachetools' guarantees transfer.

The catches were bounded: two hours of migration work for a 6× gain on the highest-traffic function in the request path.
