---
title: "Cache Invalidation Across 10 Processes: A Generation Counter"
date: 2027-01-30
publishDate: 2027-01-30
tags: ["python", "multiprocessing", "caching"]
summary: "Ten worker processes share a port. A write that clears the cache on one worker leaves the other nine serving stale results. Fixed with a shared multiprocessing.Value generation counter checked on every request."
---

A bulk card import finished successfully.
The new data was in the database.
The old cards were gone.
And then a search request landed on worker 3 and returned the old results anyway — because worker 3's cache had not been touched.

That is the bug.
Arcane Tutor runs ten Bjoern worker processes sharing a port via `SO_REUSEPORT` (covered in [the Falcon + Bjoern post](../00064_falcon-bjoern-web-framework/)).
Each is an independent OS process with its own heap.
When worker 0 handled the import and called `_clear_caches()`, it cleared its own `LRUCache`.
The other nine still held stale results.

## Why the Old Approach Looked Correct

Before the fix, `_clear_caches` was three lines:

```python
def _clear_caches(self) -> None:
    self._query_cache.clear()
    getattr(self._search, "cache", {}).clear()
    getattr(self._get_all_preferred_cards, "cache", {}).clear()
```

In a single-process server — the only thing you run locally during development — this works perfectly.
One process, one cache, one clear.
The bug is invisible until you run ten workers in production, because that is the first time you have nine separate heaps that never received the `clear()` call.

Unit tests do not catch it either.
A test that imports cards and then searches has a single `APIResource` instance.
The clear propagates because there is nothing to propagate across.

## Shared Memory via `multiprocessing.Value`

Python's `multiprocessing.Value` allocates a typed scalar in a shared memory region that all child processes can read and write.
The parent creates it before forking:

```python
# api/entrypoint.py
cache_generation = multiprocessing.Value("i", 0, lock=True)
```

The `"i"` is a C `int`.
`lock=True` wraps it in a `multiprocessing.RLock` so concurrent increments are safe.
Every `ApiWorker` receives this object; because it lives in shared memory, all ten workers see the same value.

`_clear_caches` is now two lines:

```python
def _clear_caches(self) -> None:
    with self._cache_generation.get_lock():
        self._cache_generation.value += 1
```

The workers that did not handle the import notice nothing immediately.
They notice on the next request.

## What the Workers Check on Every Request

A bare `multiprocessing.Value` counter is not a cache.
The mechanism that connects the counter to the actual cached data is [`GenerationCache`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/utils/generation_cache.py):

```python
class GenerationCache:
    def __init__(self, factory, generation):
        self._factory = factory
        self._generation = generation
        self._map = LRUCache(maxsize=1)  # generation → inner cache

    def _current(self):
        gen = self._generation.value
        try:
            return self._map[gen]
        except KeyError:
            cache = self._factory()
            self._map[gen] = cache
            return cache
```

`_map` is an LRU with maxsize=1 — it holds exactly one inner cache at a time, keyed by generation number.
On every `__getitem__`, `__setitem__`, or `__contains__`, it reads the shared counter and looks up that key.
If the key is missing (the generation advanced), it calls `factory()` to construct a fresh inner cache and installs it.
The LRU immediately evicts the previous generation's cache, freeing its memory.

The full implementation is [68 lines including docstrings and type annotations](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/utils/generation_cache.py).

The `_query_cache` in `APIResource` — previously a plain `LRUCache(maxsize=1000)` — became:

```python
self._query_cache = GenerationCache(
    factory=lambda: LRUCache(maxsize=1_000),
    generation=self._cache_generation,
)
```

The callers did not change.
`_query_cache.get(key)`, `_query_cache[key] = value`, `key in _query_cache` — the proxy is drop-in.

The `_search` method used a `@cached(cache=TTLCache(maxsize=1000, ttl=60))` decorator.
Because the cache object is created when the module loads — once per process — each worker had its own independent cache, and clearing one cleared nothing elsewhere.
The replacement is inline:

```python
gen = self._cache_generation.value
try:
    search_cache = self._search_gen_cache[gen]
except KeyError:
    search_cache = TTLCache(maxsize=1000, global_ttl=60)
    self._search_gen_cache[gen] = search_cache
if cache_key in search_cache:
    return search_cache[cache_key]
```

`_search_gen_cache` is another `LRUCache(maxsize=1)` keyed by generation.
A generation mismatch produces a fresh `TTLCache`.
This pattern is not wrapped in `GenerationCache` because `_search` needs a TTL inner cache rather than a plain LRU — `GenerationCache` is factory-parameterized and could in principle produce a `TTLCache`, but the inline version was clearer during the refactor.
The decorator was convenient, but it tied the cache lifetime to the module's import, not to a counter that any worker can advance.

## The Cost: One Integer Read per Request

Every request pays for one `self._cache_generation.value` read — a load from a shared memory region followed by an integer comparison.
Measured with `timeit.timeit` over 100,000 iterations (single-process, uncontended, Python 3.13, M5 Max): `value.value` reads take roughly 30–50 ns each.
On a cache hit that takes several milliseconds for a database round-trip, this is noise.

Reads do not need to acquire the lock.
The `lock=True` argument to `multiprocessing.Value` creates a lock for writers — the increment in `_clear_caches` holds it to prevent torn writes.
Readers never contend.
Python's `multiprocessing` module uses `mmap`-backed shared memory, and reading a word-aligned C `int` is atomic on both x86 and ARM64 (where the M5 Max runs).
A worker can never observe a half-incremented counter.

The approach does not give you exact "cache cleared at T" atomicity.
If a worker has already started processing a request when the generation advances, it finishes with the old cache entry.
The next request on that worker sees the new generation and rebuilds.
For a search cache, one stale response per worker after an import is acceptable.
The alternative — leaving nine workers' caches hot indefinitely — was not.

## One Subtle Consequence for Tests

The old decorator-based caches were module-level objects, which meant they persisted across test cases if you reused the `APIResource` instance.
Tests that asserted "after a cache clear, the result is fresh" were verifying the clear-and-check behavior within a single process, and that always worked.

The new code required tests that exercise the cross-process behavior in isolation — creating a `multiprocessing.Value`, confirming that advancing it drops the right cache without touching anything else.
The [unit tests for `GenerationCache`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/utils/tests/test_generation_cache.py) cover generation advance, factory-called-once-per-generation, and LRU eviction of the old inner cache — all verifiable without spawning subprocesses, because the counter is just an integer.

The fix is in [PR #483](https://github.com/jbylund/arcane_tutor/pull/483).
The staleness bug existed from the day the second worker was added.
Every test run used one worker, so nothing surfaced it.

Worker 3 now reads the generation counter on its next request, finds it higher than the one its inner cache was keyed on, allocates a fresh `LRUCache`, and resumes serving results — no coordinator, no message passing, no shared state beyond a single integer that every worker can read in under 50 ns.
