# ruff: noqa: INP001, ANN201, ANN204, ANN401, D101, D102, D105, D107, S101
"""Generational cache sketch — N equal-sized generations with SIEVE-style eviction.

## Design

Entries live in one of N pages arranged as a ring buffer. One page is active
(mutable); the rest are sealed (read-only). When the active page fills, it is
sealed and the oldest sealed page is retired — its visited entries are
re-inserted into a fresh active page; unvisited entries are dropped.

A single cuckoo filter spans all generations. Fingerprints are never deleted;
retired entries age out naturally as new inserts displace them via cuckoo kicks.

## Locking model (annotated in comments, not enforced in Python)

  - active page:  spinlock required for reads and writes
  - sealed pages: lock-free reads; visited bit is an atomic byte store in Rust
  - filter:       spinlock for inserts (under the same lock as active writes);
                  lock-free reads (lookup only reads, never writes)

Rotation is split into two phases so the lock is held only briefly:
  1. Lock-free: snapshot the retiring page ref, scan it for survivors, build
     the replacement page — sealed pages are immutable so no lock needed.
  2. Locked (O(1)): seal the active page, swap in the replacement, bump counter.

## Rust implementation path: per-page files

Rather than cramming all pages into one mmap region, each page maps to its own
file (e.g. sylvan.cache.0, sylvan.cache.1, ...). A small coordination file
holds the spinlock, the shared cuckoo filter, and the ring buffer counter.

Advantages over a single-file layout:
  - Rotation is file replacement: write the new page into a fresh file, then
    atomically update the coordination header. No in-place region zeroing.
  - Sealed pages can be mmap'd PROT_READ — the OS enforces immutability; an
    accidental write crashes loudly instead of silently corrupting shared state.
  - Per-page arenas are sized exactly for gen_maxsize entries and can never
    overflow during normal operation (rotation fires before they fill). The
    current design's arena-overflow full-flush goes away entirely.
  - Page files are structureless: just [slot_table][arena], no header. All
    bookkeeping lives in the coordination file.
  - Readers hold a reference to the sealed file and finish reading even after
    the coordination header is updated to point active at the next slot.

## Filter sizing

Doubled relative to the single-gen formula so that retired fingerprints have
room to age out via displacement before the filter fills:
  slot_count    = next_power_of_two(maxsize * 4)   # 2x the single-gen formula
  bucket_count  = max(slot_count // 4, 16)
  filter_slots  = bucket_count * 4  ~  4 x maxsize

At steady state (N live gens = maxsize entries): ~25% load.
Peak (+ one retired gen's worth of lingering fingerprints): ~37% load.
Well below the ~70% threshold where cuckoo displacement slows significantly.
"""

import threading
from dataclasses import dataclass
from typing import Any

import xxhash


def _next_power_of_two(n: int) -> int:
    return 1 << (n - 1).bit_length()


# ── Cuckoo filter ─────────────────────────────────────────────────────────────


class CuckooFilter:
    """Mirrors the Rust implementation.

    xxh3_64 hash, 16-bit fingerprints from bits 32-47, 4 slots/bucket,
    same alt-bucket formula. No deletion — retired entries age out as new
    inserts displace their fingerprints via cuckoo kicks.
    """

    SLOTS = 4
    MAX_KICKS = 500

    def __init__(self, bucket_count: int) -> None:
        assert bucket_count & (bucket_count - 1) == 0, "must be power of 2"
        self.bucket_count = bucket_count
        self._b: list[list[int]] = [[0] * self.SLOTS for _ in range(bucket_count)]

    @classmethod
    def for_maxsize(cls, maxsize: int) -> "CuckooFilter":
        """Derive bucket_count from maxsize using the Rust sizing formula (doubled)."""
        slot_count = _next_power_of_two(maxsize * 4)
        bucket_count = max(slot_count // 4, 16)
        return cls(bucket_count)

    def lookup(self, key: bytes) -> bool:
        fp = self._fp(key)
        b1, b2 = self._both(key, fp)
        return fp in self._b[b1] or fp in self._b[b2]

    def insert(self, key: bytes) -> bool:
        fp = self._fp(key)
        b1, b2 = self._both(key, fp)
        for b in (b1, b2):
            if 0 in self._b[b]:
                self._b[b][self._b[b].index(0)] = fp
                return True
        b = b1
        for k in range(self.MAX_KICKS):
            s = k % self.SLOTS
            self._b[b][s], fp = fp, self._b[b][s]  # kick
            b = self._alt(b, fp)
            if 0 in self._b[b]:
                self._b[b][self._b[b].index(0)] = fp
                return True
        return False  # filter full; silently drop

    def load(self) -> float:
        used = sum(s != 0 for row in self._b for s in row)
        return used / (self.bucket_count * self.SLOTS)

    def capacity(self) -> int:
        return self.bucket_count * self.SLOTS

    def _fp(self, key: bytes) -> int:
        # bits 32-47 of xxh3_64, same as Rust; 0 is reserved empty sentinel
        return (xxhash.xxh3_64(key).intdigest() >> 32 & 0xFFFF) or 1

    def _hash(self, key: bytes) -> int:
        return xxhash.xxh3_64(key).intdigest()

    def _both(self, key: bytes, fp: int) -> tuple[int, int]:
        b1 = self._hash(key) & (self.bucket_count - 1)
        return b1, self._alt(b1, fp)

    def _alt(self, idx: int, fp: int) -> int:
        # mirrors Rust: (idx ^ (fp * 0x5bd1e995 + 0xe6546b64)) & mask
        h = (fp * 0x5BD1E995 + 0xE6546B64) & 0xFFFFFFFF
        return (idx ^ h) & (self.bucket_count - 1)


# ── Page ─────────────────────────────────────────────────────────────────────


@dataclass
class Entry:
    value: Any
    visited: bool = False


class Page:
    """One generation of the cache — either active (mutable) or sealed (read-only)."""

    def __init__(self) -> None:
        self._entries: dict[bytes, Entry] = {}
        self.is_sealed = False

    def seal(self) -> None:
        """Become read-only. Resets all visited bits so entries start a fresh trial."""
        if self.is_sealed:
            msg = "already sealed"
            raise RuntimeError(msg)
        for entry in self._entries.values():
            entry.visited = False
        self.is_sealed = True

    # ── Dispatching API ───────────────────────────────────────────────────────

    def get(self, key: bytes) -> Any | None:
        if self.is_sealed:
            return self.get_sealed(key)
        return self.get_unsealed(key)

    def get_sealed(self, key: bytes) -> Any | None:
        """Lock-free read. Page is immutable after sealing; visited is an atomic store in Rust."""
        entry = self._entries.get(key)
        if entry is not None:
            entry.visited = True
            return entry.value
        return None

    def get_unsealed(self, key: bytes) -> Any | None:
        """Caller must hold the active-gen lock."""
        entry = self._entries.get(key)
        if entry is not None:
            entry.visited = True
            return entry.value
        return None

    def insert(self, key: bytes, value: Any) -> None:
        if self.is_sealed:
            return self.insert_sealed(key, value)
        return self.insert_unsealed(key, value)

    def insert_sealed(self, _key: bytes, _value: Any) -> None:
        msg = "illegal: cannot insert into a sealed page"
        raise RuntimeError(msg)

    def insert_unsealed(self, key: bytes, value: Any) -> None:
        """Caller must hold the active-gen lock."""
        self._entries[key] = Entry(value)

    def survivors(self) -> dict[bytes, Any]:
        if self.is_sealed:
            return self.survivors_sealed()
        return self.survivors_unsealed()

    def survivors_sealed(self) -> dict[bytes, Any]:
        """Entries that received a hit since sealing — candidates for re-insertion."""
        return {k: e.value for k, e in self._entries.items() if e.visited}

    def survivors_unsealed(self) -> dict[bytes, Any]:
        msg = "illegal: survivors() on an unsealed page has no meaning"
        raise RuntimeError(msg)

    # ── Dict-like helpers for iteration / membership ──────────────────────────

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self):
        return iter(self._entries)

    def items(self):
        return self._entries.items()

    def __repr__(self) -> str:
        state = "sealed" if self.is_sealed else "unsealed"
        return f"Page({state}, {len(self._entries)} entries)"


# ── Generational cache ────────────────────────────────────────────────────────


class GenerationalCache:
    """Ring buffer of N pages.

    pages[counter % n] is always the active (unsealed) page; all others are sealed.

    In the Rust implementation each Page corresponds to a separate mmap file.
    self.filter corresponds to a region in the coordination file, alongside the
    spinlock and the counter.
    """

    def __init__(self, maxsize: int, n_pages: int = 2) -> None:
        self.gen_maxsize = maxsize // n_pages
        self.pages = [Page() for _ in range(n_pages)]
        for page in self.pages[1:]:
            page.seal()  # all but the first start sealed and empty
        self.counter = 0
        self.filter = CuckooFilter.for_maxsize(maxsize)
        self._lock = threading.Lock()  # guards active page + filter mutations

    # ── Convenience properties (demo and debug) ───────────────────────────────

    @property
    def active(self) -> Page:
        return self.pages[self.counter % len(self.pages)]

    @property
    def sealed(self) -> list[Page]:
        n = len(self.pages)
        active_idx = self.counter % n
        return [self.pages[(active_idx - i) % n] for i in range(1, n)]

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, key: bytes) -> Any | None:
        # No lock: cuckoo filter reads are safe concurrently; lookup only reads,
        # never writes. In Rust this is a plain load with an Acquire fence.
        if not self.filter.lookup(key):
            return None

        with self._lock:
            value = self.active.get(key)  # → get_unsealed
            if value is not None:
                return value

        # No lock needed for sealed probes. _rotate() replaces pages[oldest_idx]
        # with a fresh empty page — a concurrent reader that misses the swap just
        # gets a false miss (empty page, returns None), not corrupted data. The
        # old sealed page is valid and readable right up until the reference is
        # replaced; a reader who catches the new page sees no entries → miss.
        # In Rust, each generation would be a separate mmap file: readers hold a
        # reference to the old file and can finish reading even after the active
        # file pointer is updated in the coordination header.
        for sealed_page in self.sealed:
            value = sealed_page.get(key)  # → get_sealed; newest first
            if value is not None:
                return value

        return None  # filter false positive — lingering fingerprint from retired gen

    def set(self, key: bytes, value: Any) -> None:
        # ── Step 1 (under lock): snapshot retiring page ref if rotation needed ─
        with self._lock:
            if len(self.active) >= self.gen_maxsize:
                gen = self.counter
                retiring = self.pages[(gen + 1) % len(self.pages)]
            else:
                gen, retiring = None, None

        # ── Step 2 (lock-free): scan retiring page and build replacement ────────
        # Sealed pages are immutable, so no lock needed. Other threads continue
        # serving reads from all pages concurrently during this step.
        if retiring is not None:
            survivors = retiring.survivors()  # → survivors_sealed; lock-free
            dropped = len(retiring) - len(survivors)
            new_page = Page()
            for k, v in survivors.items():
                new_page.insert(k, v)  # → insert_unsealed

        # ── Step 3 (under lock): commit swap + insert new entry ──────────────────
        with self._lock:
            if retiring is not None and self.counter == gen:
                self._commit_rotation(new_page, dropped)
            self.active.insert(key, value)  # → insert_unsealed
            self.filter.insert(key)  # filter mutations also under lock

    # ── Internals ─────────────────────────────────────────────────────────────

    def _commit_rotation(self, new_page: Page, _dropped: int) -> None:
        """Caller must hold _lock. Structural swap only — O(1).

        In the Rust/per-file design this is the only step that needs the lock:
        write the new page file path into the coordination header and bump the
        counter. The expensive survivor scan (step 2 of set()) runs lock-free
        before this is called.
        """
        n = len(self.pages)
        active_idx = self.counter % n
        oldest_idx = (self.counter + 1) % n
        len(self.pages[active_idx])

        self.pages[active_idx].seal()  # current active → sealed
        self.pages[oldest_idx] = new_page
        self.counter += 1
        # Invariant: self.counter % n == oldest_idx (new active is in place)
        # Filter is NOT updated — retired fingerprints age out via cuckoo kicks

    def __repr__(self) -> str:
        n = len(self.pages)
        active_idx = self.counter % n
        page_strs = [
            f"{'active' if i == 0 else 'sealed'}={len(self.pages[(active_idx + i) % n])}/{self.gen_maxsize}" for i in range(n)
        ]
        return f"GenerationalCache({', '.join(page_strs)}, filter_load={self.filter.load():.0%})"


# ── Demo ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run a worked example showing generational eviction."""
    cache = GenerationalCache(maxsize=8)

    def status(_label: str, watched: list[bytes]) -> None:
        for k in watched:
            any(k in p for p in cache.sealed)
            cache.filter.lookup(k)

    # ── Phase 1: fill first gen ───────────────────────────────────────────────
    for i in range(4):
        cache.set(f"key{i}".encode(), f"val{i}")

    # Hit key0 and key1 so they survive into the next large gen
    cache.get(b"key0")
    cache.get(b"key1")
    status("after hits on key0, key1", [b"key0", b"key1", b"key2", b"key3"])

    # ── Phase 2: trigger first rotation ──────────────────────────────────────
    for i in range(4, 8):
        cache.set(f"key{i}".encode(), f"val{i}")
    status(
        "key0-key3 now in sealed (visited reset); key4-key7 in active",
        [b"key0", b"key1", b"key2", b"key3"],
    )

    # Hit key0 and key1 while they're in the sealed gen — they'll survive
    cache.get(b"key0")
    cache.get(b"key1")
    # key2 and key3 get no hits in sealed — they'll be dropped next rotation

    # ── Phase 3: trigger second rotation ─────────────────────────────────────
    for i in range(8, 12):
        cache.set(f"key{i}".encode(), f"val{i}")
    status(
        "key2, key3 now retired — fingerprints still in filter (false positives)",
        [b"key0", b"key1", b"key2", b"key3"],
    )

    # ── Phase 4: bounded workload — filter load stays well below capacity ────────
    #
    # In real usage the cache serves a finite working set and the same keys cycle
    # back. We use a fresh cache (maxsize=12, gen_maxsize=6) with:
    #   - 4 hot keys: re-requested every round → get hits in sealed → survive rotations
    #   - 8 cold keys: requested once, never again → no hits in sealed → dropped
    #
    # The key metric is filter load. With 4x sizing, capacity ~4x maxsize = 48 -> 64
    # (next power of two). At steady state with 12 live entries, load ≈ 19%.
    # Cold fingerprints linger after their gen is retired, but at 19% load the filter
    # has plenty of headroom and hot-key re-inserts eventually displace them via kicks.

    c2 = GenerationalCache(maxsize=12)

    hot_keys = [f"hot{i}".encode() for i in range(4)]
    cold_keys = [f"cold{i}".encode() for i in range(8)]

    # Pass 1: all 12 keys requested once (cold keys are one-hit wonders from here on)
    for k in hot_keys + cold_keys:
        if c2.get(k) is None:
            c2.set(k, k.decode())

    # Passes 2-4: only hot keys re-requested. Each pass, the 4 hot keys get hits in
    # sealed (visited=True). A new batch of filler keys is inserted to fill the active
    # gen and trigger a rotation, retiring the sealed gen. Hot keys survive; cold drop.
    for pass_num in range(2, 5):
        # visit hot keys while they're still accessible
        for k in hot_keys:
            c2.get(k)
        # fill active gen to trigger rotation (using unique fillers each pass)
        for i in range(c2.gen_maxsize):
            c2.set(f"filler-p{pass_num}-{i}".encode(), "filler")

    int(c2.filter.load() * c2.filter.capacity())


if __name__ == "__main__":
    main()
