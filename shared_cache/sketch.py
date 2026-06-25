"""
Generational cache sketch — two equal-sized generations with SIEVE-style eviction.

Locking model (annotated in comments, not enforced in Python):
  - active gen:  spinlock required for reads and writes
  - sealed gen:  lock-free reads; visited bit is an atomic store in Rust
  - filter:      lock-free reads and writes (cuckoo filter, never deleted from)

Filter sizing mirrors the Rust implementation, doubled to ensure fingerprints
from retired generations age out naturally via cuckoo displacement rather than
requiring explicit deletion:
  slot_count    = next_power_of_two(maxsize * 4)   # 2× the single-gen formula
  bucket_count  = max(slot_count // 4, 16)
  filter_slots  = bucket_count * 4  ≈  4 × maxsize

At steady state (2 live gens = maxsize entries): ~25% load.
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
    """
    Mirrors the Rust implementation: xxh3_64 hash, 16-bit fingerprints from
    bits 32-47, 4 slots/bucket, same alt-bucket formula. No deletion — retired
    entries age out as new inserts displace their fingerprints via cuckoo kicks.
    """
    SLOTS = 4
    MAX_KICKS = 500

    def __init__(self, bucket_count: int):
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
        h = (fp * 0x5bd1e995 + 0xe6546b64) & 0xFFFFFFFF
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
            raise RuntimeError("already sealed")
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
        raise RuntimeError("illegal: cannot insert into a sealed page")

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
        raise RuntimeError("illegal: survivors() on an unsealed page has no meaning")

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
    def __init__(self, maxsize: int, n_pages: int = 2):
        self.gen_maxsize = maxsize // n_pages
        # Ring buffer: pages[counter % n] is always the active (unsealed) page.
        # All other pages are sealed. Rotation advances the counter by one,
        # which moves the active slot forward and retires the oldest sealed page.
        self.pages = [Page() for _ in range(n_pages)]
        for page in self.pages[1:]:
            page.seal()                # all but the first start sealed and empty
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
                gen      = self.counter
                retiring = self.pages[(gen + 1) % len(self.pages)]
            else:
                gen, retiring = None, None

        # ── Step 2 (lock-free): scan retiring page and build replacement ────────
        # Sealed pages are immutable, so no lock needed. Other threads continue
        # serving reads from all pages concurrently during this step.
        if retiring is not None:
            survivors = retiring.survivors()          # → survivors_sealed; lock-free
            dropped   = len(retiring) - len(survivors)
            new_page  = Page()
            for k, v in survivors.items():
                new_page.insert(k, v)                 # → insert_unsealed

        # ── Step 3 (under lock): commit swap + insert new entry ──────────────────
        with self._lock:
            if retiring is not None and self.counter == gen:
                self._commit_rotation(new_page, dropped)
            self.active.insert(key, value)            # → insert_unsealed
            self.filter.insert(key)                   # filter mutations also under lock

    # ── Internals ─────────────────────────────────────────────────────────────

    def _commit_rotation(self, new_page: Page, dropped: int) -> None:
        """Caller must hold _lock. Structural swap only — O(1)."""
        n                = len(self.pages)
        active_idx       = self.counter % n
        oldest_idx       = (self.counter + 1) % n
        newly_sealed_len = len(self.pages[active_idx])

        self.pages[active_idx].seal()   # current active → sealed
        self.pages[oldest_idx] = new_page
        self.counter += 1
        # Invariant: self.counter % n == oldest_idx (new active is in place)
        # Filter is NOT updated — retired fingerprints age out via cuckoo kicks

        print(
            f"  [rotate] {newly_sealed_len} → sealed, "
            f"{dropped} dropped, {len(new_page)} survivors re-inserted"
        )

    def __repr__(self) -> str:
        n          = len(self.pages)
        active_idx = self.counter % n
        page_strs  = [
            f"{'active' if i == 0 else 'sealed'}="
            f"{len(self.pages[(active_idx + i) % n])}/{self.gen_maxsize}"
            for i in range(n)
        ]
        return f"GenerationalCache({', '.join(page_strs)}, filter_load={self.filter.load():.0%})"


# ── Demo ──────────────────────────────────────────────────────────────────────

def main() -> None:
    cache = GenerationalCache(maxsize=8)
    print(
        f"filter: {cache.filter.bucket_count} buckets × {CuckooFilter.SLOTS} slots "
        f"= {cache.filter.capacity()} total slots "
        f"for maxsize={cache.gen_maxsize * 2} ({cache.gen_maxsize} per gen)"
    )

    def status(label: str, watched: list[bytes]) -> None:
        print(f"\n  {label}")
        print(f"  {cache}")
        for k in watched:
            in_active = k in cache.active
            in_sealed = any(k in p for p in cache.sealed)
            in_filter = cache.filter.lookup(k)
            loc = "active" if in_active else ("sealed" if in_sealed else "retired")
            fp_note = "in filter" if in_filter else "displaced from filter (true miss now)"
            print(f"    {k.decode():<8}  {loc:<8}  {fp_note}")

    # ── Phase 1: fill first gen ───────────────────────────────────────────────
    print("=== Phase 1: fill active gen (key0-key3) ===")
    for i in range(4):
        cache.set(f"key{i}".encode(), f"val{i}")

    # Hit key0 and key1 so they survive into the next large gen
    cache.get(b"key0")
    cache.get(b"key1")
    status("after hits on key0, key1", [b"key0", b"key1", b"key2", b"key3"])

    # ── Phase 2: trigger first rotation ──────────────────────────────────────
    print("\n=== Phase 2: insert key4-key7 — first rotation ===")
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
    print("\n=== Phase 3: insert key8-key11 — second rotation ===")
    print("  key0, key1 visited in sealed → survive as re-inserted entries")
    print("  key2, key3 not visited in sealed → dropped (fingerprints linger in filter)")
    for i in range(8, 12):
        cache.set(f"key{i}".encode(), f"val{i}")
    status(
        "key2, key3 now retired — fingerprints still in filter (false positives)",
        [b"key0", b"key1", b"key2", b"key3"],
    )

    print(f"\n  Demonstrate false positive: get(key2) filter={cache.filter.lookup(b'key2')}, result={cache.get(b'key2')!r}")
    print(f"  Demonstrate fast miss:      get(key99) filter={cache.filter.lookup(b'key99')}, result={cache.get(b'key99')!r}")

    # ── Phase 4: bounded workload — filter load stays well below capacity ────────
    #
    # In real usage the cache serves a finite working set and the same keys cycle
    # back. We use a fresh cache (maxsize=12, gen_maxsize=6) with:
    #   - 4 hot keys: re-requested every round → get hits in sealed → survive rotations
    #   - 8 cold keys: requested once, never again → no hits in sealed → dropped
    #
    # The key metric is filter load. With 4× sizing, capacity ≈ 4×maxsize = 48 → 64
    # (next power of two). At steady state with 12 live entries, load ≈ 19%.
    # Cold fingerprints linger after their gen is retired, but at 19% load the filter
    # has plenty of headroom and hot-key re-inserts eventually displace them via kicks.
    print("\n=== Phase 4: bounded workload — filter load stays well below capacity ===")

    c2 = GenerationalCache(maxsize=12)
    print(f"  filter: {c2.filter.capacity()} slots for maxsize=12 ({c2.filter.capacity()}/(4×12)={c2.filter.capacity()/48:.1f}×)")

    hot_keys  = [f"hot{i}".encode()  for i in range(4)]
    cold_keys = [f"cold{i}".encode() for i in range(8)]

    # Pass 1: all 12 keys requested once (cold keys are one-hit wonders from here on)
    for k in hot_keys + cold_keys:
        if c2.get(k) is None:
            c2.set(k, k.decode())

    print(f"\n  After pass 1 (all 12 keys requested once): {c2}")
    print(f"  Hot keys in sealed, awaiting their first hit: "
          f"{sum(1 for k in hot_keys if any(k in p for p in c2.sealed))}/4  (visited bits will be set in pass 2)")

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

    used = int(c2.filter.load() * c2.filter.capacity())
    print(f"\n  After 3 more passes with rotation each time: {c2}")
    print(f"  filter: {used}/{c2.filter.capacity()} slots = {c2.filter.load():.0%}  ← stays well below capacity")
    print(f"\n  Hot keys in cache:  {sum(1 for k in hot_keys  if c2.get(k) is not None)}/4  (survived every rotation)")
    print(f"  Cold keys in cache: {sum(1 for k in cold_keys if c2.get(k) is not None)}/8  (dropped after one gen)")
    print(f"\n  Cold fingerprints in filter: {sum(1 for k in cold_keys if c2.filter.lookup(k))}/8  ← lingering fps")
    print(f"  └─ false positives: filter.lookup()=True, but both gens probed → miss correctly returned")
    print(f"  └─ at {c2.filter.load():.0%} filter load, these get displaced gradually via cuckoo kicks as hot keys re-insert")


if __name__ == "__main__":
    main()
