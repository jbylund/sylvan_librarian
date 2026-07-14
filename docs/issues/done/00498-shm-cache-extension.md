# Shared-memory cache extension (Rust)

## Status: parked (2026-06-11) — measurement said no

The step-1 go/no-go measurement ran against 14 days of production `magic.query_log` data
(7,525 requests, 94.3% hit rate, 428 misses). Of 257 "duplicate" misses a shared cache could
have recovered, 236 were a single uptime-monitor query (`baloth`) re-warming each worker's
cache after import flushes; organic cross-worker duplicate misses totaled **~21 in two weeks**,
mostly autocomplete prefixes missing twice. Overall traffic is low, so per-worker LRUs sit at a
few hundred entries against a 10k maxsize: there is no meaningful hit-rate uplift, memory
duplication, or compute to recover. The prep PR
([#503](https://github.com/jbylund/sylvan_librarian/pull/503), rendered-bytes cache entries) and the
engine ([#490](https://github.com/jbylund/sylvan_librarian/pull/490)) already capture the wins that
mattered.

Revisit if organic traffic grows by orders of magnitude or query overlap between users becomes
significant. The design below is kept for that day.

## Problem

The API runs ~10 independent Bjoern workers. Caches are per-process: the
[CachingMiddleware](../../../api/middlewares/caching_middleware.py) holds an `LRUCache(maxsize=10_000)`
of `falcon.Response` objects per worker, and `_search` holds a generation-keyed `TTLCache` per
worker. Consequences:

- A popular query is computed and cached up to 10× (once per worker) — `SO_REUSEPORT` spreads
  requests randomly, so effective hit rate is far below what one shared cache would give.
- The same hot responses are duplicated in RSS across all workers.

Adding Redis would fix both but adds a service to operate and a network round trip
(~100–250 µs/op) to every request.

Per-worker caches also force invalidation to be *propagated*: the
[cross-process generation counter](../../changelog/2026-05-27-cross-process-cache-invalidation.md)
(`multiprocessing.Value` wired from `entrypoint.py` through `ApiWorker`, `GenerationCache`, and
the generation-keyed cache-of-caches in `_search`) exists only so that a mutation on one worker
eventually flushes the other nine — each stale worker still serves old data until its next
request checks the generation.

## Proposal

A small Rust extension (PyO3 + maturin, same toolchain as the card engine) exposing a
cross-process cache in a fixed-size shared-memory segment (`/dev/shm` on Linux, file-backed
`/tmp` mmap on macOS, mirroring the [shared card store](00502-shared-card-store-mmap.md)):

```python
cache = ShmCache(path, capacity_bytes)   # attach-or-create, idempotent across workers
cache.get(key: bytes) -> bytes | None    # lock-free read, ~1–5 µs
cache.set(key: bytes, value: bytes, ttl_seconds: float | None)
cache.clear()                            # bump generation counter in the header
cache.stats() -> dict                    # hits/misses/evictions/bytes used
```

All operations release the GIL.

### Honest framing vs the original idea

- **rkyv is not the key enabler here.** Cached values originate in Python, so they must be
  serialized to bytes regardless — the cache is fundamentally `bytes → bytes`. rkyv earned its
  keep in the card store because Rust owns the data model and reads are zero-copy against
  `Archived<Card>`. Here the hard part is a *mutable concurrent* structure in shm — exactly what
  the read-only rename-replace card store deliberately avoided.
- **The Rust filter engine ([#490](https://github.com/jbylund/sylvan_librarian/pull/490)) changes
  the miss-path math — against Redis, in shm's favor.** With SQL at ~10.6 ms (geomean) a
  200 µs Redis hit was negligible; with the engine at ~0.49 ms geomean and 10–20 µs for the
  fastest queries, a Redis round trip is *slower than recomputing* for fast queries and only a
  small win at the median. Only a µs-scale hit stays clearly net-positive across the board. The
  shm cache's value concentrates in the slow tail (`format:legacy` ~16 ms) and in everything
  around the filter: query parsing, FFI dict conversion, JSON rendering, and response
  compression, which a middleware-level hit skips entirely.
- **No new service, shared capacity, simpler invalidation** are the remaining structural
  arguments: ~10× effective cache capacity with zero duplication, and hits cheap enough (µs) to
  consult unconditionally on every request.

### Design: dodge the hard concurrency problems

A general-purpose shm LRU needs a cross-process allocator, robust mutexes, and crash-safe
linked-list updates on every *read*. Instead, use a **set-associative index + ring-buffer value
arena** (bitcask / Varnish-transient style):

- **Header**: magic, version, atomic generation counter (replaces the `multiprocessing.Value`
  invalidation channel), stats counters.
- **Index**: N buckets × K ways. Each entry: key hash, key/value offset+len into the ring,
  expiry timestamp, sequence counter.
- **Ring arena**: values appended; when the write head wraps, overwritten entries are implicitly
  evicted (FIFO ≈ LRU for a query cache). No allocator, no fragmentation, no compaction.
- **Concurrency**: reads are lock-free — seqlock per entry (read seq, copy, verify seq unchanged
  and key matches). Writes (cache misses only, so rare) take a single cross-process writer lock;
  a `kill -9` mid-write at worst orphans one entry version, never corrupts readers. If the
  segment is ever detectably broken: unlink and rebuild — it's a cache.
- **TTL** checked on read; **LRU upgrade path** (only if FIFO proves insufficient): a CLOCK
  second-chance bit, still no linked list.
- **Cost-based admission**: with the engine making most misses cheap, only cache responses
  whose miss cost exceeded a threshold (the request is already timed end to end). Fast queries
  re-evaluate every time; ring capacity is reserved for the slow tail, where the cache actually
  pays. This also bounds the damage of any staleness bug to expensive-but-rare queries.

### Where it plugs in

**Response cache first.** The middleware is the sweet spot: store
`(status, headers, body bytes)` — a hit is a memcpy with zero Python deserialization. The
`_search` dict cache benefits much less (every hit pays orjson/pickle decode); revisit it as a
shm L2 behind the existing in-process L1 only if measurements justify it.

Prerequisite: the middleware currently caches whole `falcon.Response` objects including `media`
dicts. Normalizing to `(status, headers, rendered body)` is a standalone prep PR that also
shrinks the current per-worker caches.

## Plan

1. **Measure** (no code): duplicate-miss rate across workers from existing cache-miss logs;
   benchmark baselines — in-process cachebox hit, Redis-in-compose hit, `diskcache` on tmpfs hit
   — and the **end-to-end miss cost with the engine live** (parse + filter + FFI + render +
   compress, not just filter time). This is the go/no-go gate: with the whole miss path at
   low-single-digit ms, "no response cache at all" is a legitimate outcome and should be ruled
   out by data (expected savings: tail queries, compression CPU, headroom under load).
2. **Prep PR**: middleware caches `(status, headers, body bytes)` instead of `Response` objects.
3. **Rust crate** `shm_cache` next to the card engine; reuse `make engine` / maturin wiring.
   Crates: `memmap2`, `rustix` (flock), no rkyv needed for v1.
4. **Integrate** behind a settings flag in `CachingMiddleware`. Invalidation simplifies: for
   shm-backed caches `clear()` is one atomic generation bump, immediately consistent across all
   workers — no propagation. The `multiprocessing.Value` plumbing, `GenerationCache`, and the
   generation-keyed cache-of-caches in `_search` get deleted; any remaining in-process L1 reads
   the generation from the shm header instead.
5. **Stress + crash tests**: multiprocess hammer test, `kill -9` during `set`, reattach after
   corruption. Benchmark with `client/query_runner.py`.

## Prior art

- Cloudflare [mmap-sync](https://github.com/cloudflare/mmap-sync) — rkyv + mmap, wait-free
  single-writer; validates the approach but is snapshot-oriented, not a keyed cache.
- `cachebox` is already Rust — the delta of this work is purely the shared-memory part.
- `diskcache` (SQLite on tmpfs, ~10–30 µs/op) is the no-Rust fallback if step 1 shows µs-level
  hits don't matter.
