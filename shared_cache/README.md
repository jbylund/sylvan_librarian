# shared_cache

A cross-process generational cache backed by a memory-mapped file. Multiple worker processes
that open the same path share a single cache: a response cached by worker 1 is a hit for
worker 2 without any IPC round-trip.

## How it works

### File layout

```
[0..64]               CoordHeader    — spinlock, magic/version, ring buffer counter, filter params
[64..64+filter_bytes] Cuckoo filter  — 16-bit fingerprints, 4 slots/bucket, shared across pages
[filter_end..]        N pages        — each page: PageHeader + slot table + arena
```

Each page contains a fixed-size open-addressing hash table and a bump-allocator arena for
rkyv-serialized value bytes and raw key bytes.

### Generational eviction (SIEVE-style)

N pages form a ring buffer. The current active page accepts writes; the remaining N-1 pages are
sealed (read-only). When the active page fills, rotation occurs:

1. **Lock-free phase**: scan the oldest sealed page for entries with their `visited` bit set
   (accessed at least once since they were sealed). Surviving entries are collected into a list.
2. **O(1) locked phase**: seal the active page, reinitialize the oldest sealed page from the
   survivor list, advance the ring buffer counter. This is the only step that requires the lock.

Entries that are never accessed while sealed are dropped on rotation — no explicit eviction needed.
Hot entries survive indefinitely by cycling through the ring.

### Locking model

- **Active page**: spinlock required for reads and writes.
- **Sealed pages**: lock-free reads. The `visited` bit is an atomic byte store. A per-page
  generation counter detects concurrent rotation; readers that observe a generation change
  treat the result as a miss rather than returning stale data.
- **Filter**: lock-free reads; spinlock for inserts.

### set() fast path

To avoid rkyv serialization when the value hasn't changed, `set()` runs a tiered check before
serializing:

1. **Filter miss** → new key, skip all checks.
2. **Length mismatch** → content changed, skip hash checks.
3. **Sampled hash mismatch** → content almost certainly changed. For bodies ≤ 64 bytes this is
   a full hash; for larger bodies it hashes the first 64 + last 64 bytes (~128 bytes total).
4. **All match** → return early, rkyv skipped.

If any step finds a mismatch the key is inserted/updated with the new value (overwriting the
existing slot with fresh arena bytes; the old bytes are reclaimed at rotation time).

### Cuckoo filter

16-bit fingerprints, 4 slots/bucket, `bucket_count = next_pow2(maxsize × 4) / 4`.
`get()` and `__contains__` check the filter lock-free before acquiring the spinlock: true misses
skip the lock entirely. At ≤ 50% load the false-positive rate is ≈ 0.006%.

## Build

```bash
source .venv/bin/activate
PATH="$HOME/.cargo/bin:$PATH" maturin develop          # debug (fast build)
PATH="$HOME/.cargo/bin:$PATH" maturin develop --release # release (fast runtime)
```

## Usage

```python
from shared_cache import SharedCache

cache = SharedCache(
    path="/tmp/arcane.cache",
    maxsize=10_000,
    default_ttl=300.0,  # seconds; None = never expire
    n_pages=2,          # number of generations (default 2)
    arena_mb=None,      # override per-page arena size
)

key_bytes = orjson.dumps(key)  # serialize once, reuse for get and set

cached = cache.get(key_bytes)  # None on miss or expiry
cache[key_bytes] = response    # value must have status/headers/body/result_count/total_cards
len(cache)                     # approximate live entry count
cache.invalidate()             # flush all entries immediately
```

## Performance

Measured on Apple M-series with a real 5,025-byte gzip-compressed response body
(`/search?q=elf`, 500 warm keys). SharedCache numbers use pre-serialized `bytes` keys
(orjson key serialization is ~85 ns, paid once per request by the caller).

### Backend comparison

| Backend | set | get_hit | get_miss |
|---|---|---|---|
| `dict` | 63 ns | 36 ns | 33 ns |
| `cachebox.LRUCache` | 69 ns | 45 ns | 38 ns |
| `SharedCache` | 103 ns | 351 ns | **31 ns** |

Miss latency is on par with in-process caches — the cuckoo filter returns false for unknown keys
without acquiring the spinlock. Hit latency is higher because SharedCache reconstructs Python
objects from rkyv-serialized shared memory on each read. The payoff is that every worker process
shares the same pool rather than maintaining its own.

### set() paths

| Path | Cost | When |
|---|---|---|
| Same value | ~109 ns | Content hash matches → skip rkyv, headers, counts entirely |
| Changed value, fits in slot | ~1637 ns | Content differs, new body ≤ old capacity → rkyv + in-place overwrite |
| Larger value or new key | ~1887 ns | New body > old capacity, or first insertion → rkyv + arena alloc + copy |

## Tradeoffs

| | dict / LRUCache | SharedCache |
|---|---|---|
| get_hit latency | ~36–51 ns | ~364 ns (middleware path) |
| Memory per process | `maxsize × value_size` | shared; 1× regardless of workers |
| Cross-process hits | ✗ | ✓ |
| Eviction | exact LRU | SIEVE-style (visited bit per slot) |
| Persistence | none | survives worker restart (file persists) |

SharedCache wins on total memory once you have more than ~2 workers.

## See also

- [sketch.py](sketch.py) — Python model of the generational cache design
- [benchmark.py](benchmark.py) — latency comparison vs in-process caches
- [demo.py](demo.py) — runnable usage example
