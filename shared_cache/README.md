# shared_cache

A cross-process LRU cache backed by a memory-mapped file. Multiple worker processes
that open the same path share a single cache: a response cached by worker 1 is a hit
for worker 2 without any IPC round-trip.

## How it works

The mmap file is divided into three regions:

```
[0..64]         RegionHeader  — spinlock + magic/version/counts/seq
[64..64+N×64]   Slot table    — open-addressing hash table (N = next_power_of_two(maxsize × 2))
[64+N×64..]     Arena         — bump allocator for rkyv value bytes + raw key bytes
```

Each slot stores `key_hash`, `expiry_ns`, `last_used` (LRU sequence), and offsets into the
arena. Values are serialized with [rkyv](https://rkyv.org/) — zero-copy on the read side
within the locked critical section.

A single `AtomicU32` spinlock at byte 0 gates all mutations. On a get, the lock is held
only for the slot probe and LRU seq update (~60 ns); the arena copy happens **outside**
the lock, so concurrent reads from multiple processes overlap almost entirely. A
`generation` counter (incremented on `generation_reset`) lets readers detect a concurrent
flush and discard stale data rather than return garbage. In the rare case `generation_reset`
races with an in-flight copy, the reader gets a false miss and re-queries — the cost of
preventing this with a reader-count pin (~35 ns/hit on MAP_SHARED pages) exceeds the
benefit given how rarely `generation_reset` fires.

The response body is copied directly from the mmap arena into a Python `bytes` object —
no intermediate Rust `Vec`. `.body` attribute access on the returned object is a
reference-count increment with no additional copy.

**Eviction** samples 8 random slots and tombstones the one with the smallest `last_used`
(approximate LRU, same approach as Redis). Tombstones preserve probe chain integrity for
linear probing.

**Arena reset**: when the bump allocator overflows, the slot table is zeroed and the arena
pointer reset to 0. All entries are lost — this is a full cache flush, not a compaction.
Arena size defaults to `maxsize × 8 KiB`. Arcane Tutor's gzip-compressed JSON responses
are ~5 KB each after rkyv serialization, so the default comfortably fits 10,000 entries.
Override with `arena_mb` if you measure a different typical response size.

## Build

```bash
cd shared_cache
source ../.venv/bin/activate
PATH="$HOME/.cargo/bin:$PATH" maturin develop   # debug (fast build)
PATH="$HOME/.cargo/bin:$PATH" maturin develop --release  # release (fast runtime)
```

## Usage

```python
from shared_cache import SharedCache

cache = SharedCache(
    path="/tmp/arcane.cache",
    maxsize=10_000,
    default_ttl=300.0,   # seconds; None = never expire
    arena_mb=None,       # override arena size (default: maxsize × 8 KiB)
    key_fn=orjson.dumps, # optional; defaults to pickle.dumps(key, 2)
)

# Store — value must have status/headers/body/result_count/total_cards attributes.
# Compatible with the existing CachedResponse NamedTuple.
cache[key] = response

# Retrieve — returns a CachedResponse-like object or None
cached = cache.get(key)   # None on miss or expiry
cached = cache[key]       # raises KeyError on miss

# Introspection
len(cache)         # approximate live entry count
cache.invalidate() # flush all entries immediately
```

Pass `key_fn=orjson.dumps` for best performance (see benchmarks below).

## Performance

Measured on Apple M-series with a real 5,025-byte gzip-compressed response body
(`/search?q=elf`, 500 warm keys). All SharedCache numbers use `key_fn=orjson.dumps`.

### Backend comparison

| Backend | get_hit | get_miss | set |
|---|---|---|---|
| `dict` | 35 ns | 34 ns | 61 ns |
| `cachebox.LRUCache` | 57 ns | 43 ns | 72 ns |
| `SharedCache` (orjson) | 452 ns | 153 ns | 956 ns |
| `SharedCache` (pickle) | 1,072 ns | 725 ns | 1,652 ns |

In-process caches return a reference to an existing Python object (zero copies). SharedCache
reconstructs Python objects from rkyv-serialized shared memory on each read — that's the
source of the gap. The payoff is that every worker process shares the same 10k-entry pool
rather than maintaining its own.

### Where the 490 ns goes (middleware path)

```
A  orjson key serialize           85 ns   17%   tuple → bytes (before lock)
B  lock + probe + release         59 ns   12%   spinlock CAS, slot scan, LRU update, unlock
C  mmap→PyBytes (body)            91 ns   19%   5 KB copied directly from arena to Python bytes
D  rkyv + Python object build    196 ns   40%   headers PyList + status str + PyO3 object
E  full get()                    452 ns   92%
   .body + .headers access        37 ns    8%   refcount bumps — effectively free
   ──────────────────────────────────────────────────────────
F  middleware path                489 ns  100%
```

The spinlock is held for only Phase B (~60 ns); the arena copy runs concurrently across all
worker processes. `.body` and `.headers` are pre-built Python objects in the pyclass — attribute
access is a refcount increment with no copy, so the middleware's `resp.data = cached.body` and
`resp._headers.update(cached.headers)` cost nothing extra.

## Tradeoffs

| | dict / LRUCache | SharedCache |
|---|---|---|
| get_hit latency | ~35–57 ns | ~490 ns (middleware path) |
| Memory per process | `maxsize × value_size` | shared; 1× regardless of workers |
| Cross-process hits | ✗ | ✓ |
| Process crash safety | n/a | lock timeout prevents hang |
| Persistence | none | survives worker restart (file persists) |

Break-even: SharedCache wins on total memory once you have more than ~2 workers, since each
additional worker adds zero memory cost for cached entries.

## Drop-in for CachingMiddleware

```python
# In entrypoint.py, before spawning workers:
import orjson
from shared_cache import SharedCache
from api.middlewares.caching_middleware import CachingMiddleware

shared = SharedCache(path="/tmp/arcane.cache", maxsize=10_000,
                     default_ttl=300.0, key_fn=orjson.dumps)
# Pass the same instance (or re-open the same path) in each worker process.
app = create_app(cache=shared)
```

## See also

- [demo.py](demo.py) — runnable usage example
- [benchmark.py](benchmark.py) — latency comparison vs in-process caches
