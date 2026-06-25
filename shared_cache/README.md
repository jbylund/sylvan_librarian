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

A single `AtomicU32` spinlock at byte 0 serializes all get/set operations. Reads copy
the rkyv bytes out of the arena before releasing the lock, then deserialize outside it.
The response body is copied directly into a Python `bytes` object in `build_response`,
so `.body` access on a cache hit is a reference-count increment — no extra copy.

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
| `dict` | 35 ns | 32 ns | 62 ns |
| `cachebox.LRUCache` | 46 ns | 44 ns | 77 ns |
| `SharedCache` (orjson) | 541 ns | 154 ns | 946 ns |
| `SharedCache` (pickle) | 1,159 ns | 731 ns | 1,671 ns |

In-process caches return a reference to an existing Python object (zero copies). SharedCache
must copy the response body out of shared memory and reconstruct Python objects on each read —
that's the source of the gap. The payoff is that every worker process shares the same 10k-entry
pool rather than maintaining its own.

### Where the 541 ns goes

```
A  orjson key serialize        91 ns   16%   tuple → bytes (before lock)
B  lock + probe + memcpy      255 ns   46%   spinlock, slot scan, copy 5 KB from mmap to Rust
C  rkyv parse + Python objs   213 ns   38%   deserialize + build CachedResponse (after lock)
   ─────────────────────────────────────────────────────────────
D  full get                   559 ns  100%

   .body attribute access      ~2 ns    0%   refcount bump on pre-built PyBytes — effectively free
```

Phase B scales with response size (it is a memcpy of the rkyv blob). Phase C is dominated
by the body copy into Python memory and string allocations for status/headers; it is roughly
linear in body size. The spinlock itself is ~5% of Phase B; key serialization and xxhash
happen before lock acquisition and do not contribute to the critical section.

## Tradeoffs

| | dict / LRUCache | SharedCache |
|---|---|---|
| get_hit latency | ~35–50 ns | ~540 ns |
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
