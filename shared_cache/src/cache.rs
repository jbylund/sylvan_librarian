use std::time::{SystemTime, UNIX_EPOCH};

use memmap2::MmapMut;
use rkyv::Archived;
use xxhash_rust::xxh3::xxh3_64;

use crate::region::{
    arena_start, bump_generation, hdr_ptr, hdr_ptr_mut, open_region, read_generation,
    slot_ptr, slot_ptr_mut, try_lock, unlock, RegionHeader, RawSlot, EMPTY, HEADER_SIZE,
    SLOT_SIZE, TOMBSTONE,
};
use crate::region::normalize_hash;
use crate::types::CachedResponse;

const EVICTION_SAMPLE: u32 = 8;
// Arena allocations are padded to this alignment so rkyv's access_unchecked is safe.
const ARENA_ALIGN: u32 = 16;

pub struct SharedCache {
    mmap: MmapMut,
    arena_start: usize,
    default_ttl_secs: Option<f64>,
}

fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64
}

impl SharedCache {
    pub fn open(
        path: &str,
        maxsize: u32,
        default_ttl_secs: Option<f64>,
        arena_bytes: Option<u32>,
    ) -> std::io::Result<Self> {
        let slot_count = (maxsize * 2).next_power_of_two();
        // Default arena: 8 KiB per potential entry.
        // Real production responses (gzip-compressed JSON) run 5-6 KB per entry after rkyv
        // serialization; 8 KiB leaves headroom for larger result sets and key bytes.
        let arena_size = arena_bytes.unwrap_or(maxsize.saturating_mul(8192));
        let mmap = open_region(path, slot_count, arena_size)?;
        Ok(Self {
            arena_start: arena_start(slot_count),
            mmap,
            default_ttl_secs,
        })
    }

    // ── Header / slot accessors (all called under the lock) ──────────────────

    fn hdr(&self) -> &RegionHeader {
        unsafe { &*hdr_ptr(&self.mmap) }
    }

    fn hdr_mut(&mut self) -> &mut RegionHeader {
        unsafe { &mut *hdr_ptr_mut(&mut self.mmap) }
    }

    fn slot(&self, idx: u32) -> &RawSlot {
        unsafe { &*slot_ptr(&self.mmap, idx) }
    }

    fn slot_mut(&mut self, idx: u32) -> &mut RawSlot {
        unsafe { &mut *slot_ptr_mut(&mut self.mmap, idx) }
    }

    // ── Public API ───────────────────────────────────────────────────────────

    /// Serialize `response` to rkyv bytes and store them under `key`.
    /// No-ops silently on lock timeout or if the key is already cached.
    pub fn set(
        &mut self,
        key: &[u8],
        status: &str,
        headers: Vec<(String, String)>,
        body: Option<Vec<u8>>,
        result_count: Option<i64>,
        total_cards: Option<i64>,
        ttl_secs: Option<f64>,
    ) {
        let value_bytes = match rkyv::to_bytes::<rkyv::rancor::Error>(&CachedResponse {
            status: status.to_string(),
            headers,
            body,
            result_count,
            total_cards,
        }) {
            Ok(b) => b,
            Err(_) => return,
        };

        let hash = normalize_hash(xxh3_64(key));

        if !try_lock(&self.mmap) {
            return;
        }
        self.do_set(key, hash, &value_bytes, ttl_secs);
        unlock(&self.mmap);
    }

    /// Find `key`, release the lock, then call `f` with a slice directly into the
    /// mmap arena — no intermediate Vec.  After `f` returns, the generation counter
    /// is checked; if a concurrent `generation_reset` occurred the result is
    /// discarded and `None` is returned.
    pub fn get_with<F, R>(&mut self, key: &[u8], f: F) -> Option<R>
    where
        F: FnOnce(&[u8]) -> R,
    {
        let hash = normalize_hash(xxh3_64(key));

        if !try_lock(&self.mmap) {
            return None;
        }
        let slot = self.do_get(key, hash);
        // Snapshot generation under the lock so we see a consistent value.
        let gen_before = self.hdr().generation;
        unlock(&self.mmap);

        let (offset, len) = slot?;
        let v_start = self.arena_start + offset as usize;

        // f runs without the lock. A concurrent generation_reset can race here:
        // it may zero the slot table and begin overwriting arena bytes while f is
        // reading them. In that case f receives partially-stale data, but the
        // generation check below detects the mismatch and returns None — the
        // caller gets a false miss and re-queries on the next request.
        //
        // Preventing this with a reader-count pin would cost ~35 ns on every hit
        // (atomic writes to a MAP_SHARED page are expensive across processes), and
        // generation_reset only fires once every ~10k inserts. Not worth it.
        let result = f(&self.mmap[v_start..v_start + len as usize]);

        // read_generation issues an Acquire fence so the mmap reads above are
        // not reordered past this check.
        if read_generation(&self.mmap) != gen_before {
            return None;
        }

        Some(result)
    }

    /// Lock-only probe: acquire the spinlock, check whether `key` is present,
    /// update LRU seq, then release.  No arena copy.  Used to isolate the
    /// lock-critical-section cost in benchmarks.
    pub fn probe_only(&mut self, key: &[u8]) -> bool {
        let hash = normalize_hash(xxh3_64(key));
        if !try_lock(&self.mmap) {
            return false;
        }
        let found = self.do_get(key, hash).is_some();
        unlock(&self.mmap);
        found
    }

    /// Force-expire all entries by resetting the generation counter and wiping
    /// the slot table. Arena space is reclaimed at the same time.
    pub fn invalidate(&mut self) {
        if try_lock(&self.mmap) {
            self.generation_reset();
            unlock(&self.mmap);
        }
    }

    pub fn entry_count(&self) -> u32 {
        self.hdr().entry_count
    }

    // ── Internal: get ────────────────────────────────────────────────────────

    /// Probe the slot table under the lock.  On hit, updates LRU seq and returns
    /// `(arena_offset, arena_len)`.  The caller copies from the arena after
    /// releasing the lock.
    fn do_get(&mut self, key: &[u8], hash: u64) -> Option<(u32, u32)> {
        let slot_count = self.hdr().slot_count;
        let now = now_ns();
        let start = (hash % slot_count as u64) as u32;
        let mut idx = start;

        loop {
            match self.slot(idx).key_hash {
                EMPTY => return None,
                TOMBSTONE => {}
                h if h == hash => {
                    let slot = self.slot(idx);
                    let not_expired = slot.expiry_ns == u64::MAX || now < slot.expiry_ns;
                    if not_expired && self.key_matches(slot, key) {
                        let offset = slot.arena_offset;
                        let len = slot.arena_len;

                        let seq = self.hdr().seq + 1;
                        self.hdr_mut().seq = seq;
                        self.slot_mut(idx).last_used = seq;

                        return Some((offset, len));
                    }
                }
                _ => {}
            }
            idx = (idx + 1) % slot_count;
            if idx == start {
                return None;
            }
        }
    }

    fn key_matches(&self, slot: &RawSlot, key: &[u8]) -> bool {
        let k_start = self.arena_start + slot.key_offset as usize;
        &self.mmap[k_start..k_start + slot.key_len as usize] == key
    }

    // ── Internal: set ────────────────────────────────────────────────────────

    fn do_set(&mut self, key: &[u8], hash: u64, value_bytes: &[u8], ttl_secs: Option<f64>) {
        let slot_count = self.hdr().slot_count;
        let start = (hash % slot_count as u64) as u32;
        let mut idx = start;
        let mut first_available: Option<u32> = None;

        // Probe to find either the existing entry (abort) or the insert position.
        loop {
            match self.slot(idx).key_hash {
                EMPTY => {
                    first_available.get_or_insert(idx);
                    break;
                }
                TOMBSTONE => {
                    first_available.get_or_insert(idx);
                }
                h if h == hash => {
                    if self.key_matches(self.slot(idx), key) {
                        return; // already cached
                    }
                }
                _ => {}
            }
            idx = (idx + 1) % slot_count;
            if idx == start {
                break;
            }
        }

        let target = first_available.unwrap_or(idx);

        // Evict if the live entry count is at the limit.
        if self.hdr().entry_count >= self.hdr().maxsize {
            self.evict_one();
        }

        // Allocate space in the arena.
        let (val_off, key_off) = match self.alloc_pair(value_bytes.len() as u32, key.len() as u32)
        {
            Some(pair) => pair,
            None => {
                // Arena full — clear everything and start fresh.
                self.generation_reset();
                let target = (hash % self.hdr().slot_count as u64) as u32;
                if let Some(pair) =
                    self.alloc_pair(value_bytes.len() as u32, key.len() as u32)
                {
                    self.write_slot(target, hash, pair, value_bytes, key, ttl_secs);
                }
                return;
            }
        };

        let was_tombstone = self.slot(target).key_hash == TOMBSTONE;
        self.write_slot(target, hash, (val_off, key_off), value_bytes, key, ttl_secs);

        if was_tombstone {
            let tc = self.hdr().tombstone_count.saturating_sub(1);
            self.hdr_mut().tombstone_count = tc;
        }
        let ec = self.hdr().entry_count + 1;
        self.hdr_mut().entry_count = ec;
    }

    /// Allocate `value_aligned_len` + `key_len` bytes from the bump arena.
    /// Returns `(value_offset, key_offset)` or `None` if the arena is full.
    fn alloc_pair(&mut self, value_len: u32, key_len: u32) -> Option<(u32, u32)> {
        let value_padded = (value_len + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);
        let total = value_padded.checked_add(key_len)?;
        let head = self.hdr().arena_head;
        let new_head = head.checked_add(total)?;
        if new_head > self.hdr().arena_size {
            return None;
        }
        self.hdr_mut().arena_head = new_head;
        Some((head, head + value_padded))
    }

    fn write_slot(
        &mut self,
        idx: u32,
        hash: u64,
        (val_off, key_off): (u32, u32),
        value_bytes: &[u8],
        key: &[u8],
        ttl_secs: Option<f64>,
    ) {
        // Copy bytes into the arena.
        let v_abs = self.arena_start + val_off as usize;
        self.mmap[v_abs..v_abs + value_bytes.len()].copy_from_slice(value_bytes);
        let k_abs = self.arena_start + key_off as usize;
        self.mmap[k_abs..k_abs + key.len()].copy_from_slice(key);

        let now = now_ns();
        let ttl = ttl_secs.or(self.default_ttl_secs);
        let expiry_ns = ttl.map(|s| now + (s * 1e9) as u64).unwrap_or(u64::MAX);

        let seq = self.hdr().seq + 1;
        self.hdr_mut().seq = seq;

        // Write all fields before key_hash so readers never observe a partial slot.
        let s = self.slot_mut(idx);
        s.expiry_ns = expiry_ns;
        s.last_used = seq;
        s.arena_offset = val_off;
        s.arena_len = value_bytes.len() as u32;
        s.key_offset = key_off;
        s.key_len = key.len() as u32;
        s.key_hash = hash; // must be last
    }

    // ── Internal: eviction ───────────────────────────────────────────────────

    /// Sample `EVICTION_SAMPLE` random slots and tombstone the one with the
    /// smallest `last_used` (oldest). Stale or expired slots are immediate picks.
    fn evict_one(&mut self) {
        let slot_count = self.hdr().slot_count;
        let now = now_ns();

        let mut best_idx: Option<u32> = None;
        let mut best_seq = u64::MAX;

        for _ in 0..EVICTION_SAMPLE {
            let idx = rand::random::<u32>() % slot_count;
            let s = self.slot(idx);
            match s.key_hash {
                EMPTY | TOMBSTONE => {}
                _ => {
                    let expired = s.expiry_ns != u64::MAX && now >= s.expiry_ns;
                    if expired || s.last_used < best_seq {
                        best_seq = if expired { 0 } else { s.last_used };
                        best_idx = Some(idx);
                        if expired {
                            break; // free pick, no need to sample more
                        }
                    }
                }
            }
        }

        if let Some(idx) = best_idx {
            self.slot_mut(idx).key_hash = TOMBSTONE;
            let ec = self.hdr().entry_count.saturating_sub(1);
            self.hdr_mut().entry_count = ec;
            let tc = self.hdr().tombstone_count + 1;
            self.hdr_mut().tombstone_count = tc;
        }
    }

    // ── Internal: generation reset ───────────────────────────────────────────

    /// Wipe the slot table and reset the arena bump pointer.
    /// All existing entries become unreachable. Called when the arena is full.
    fn generation_reset(&mut self) {
        // Increment before zeroing so concurrent get_with readers detect the reset
        // via read_generation and discard their results rather than return garbage.
        bump_generation(&self.mmap);
        let slot_count = self.hdr().slot_count;
        unsafe {
            std::ptr::write_bytes(
                self.mmap.as_mut_ptr().add(HEADER_SIZE),
                0,
                slot_count as usize * SLOT_SIZE,
            );
        }
        let h = self.hdr_mut();
        h.arena_head = 0;
        h.entry_count = 0;
        h.tombstone_count = 0;
    }
}

// ── Deserialization helper ───────────────────────────────────────────────────

/// Zero-copy access to an archived CachedResponse stored in `bytes`.
///
/// # Safety
/// `bytes` must have been produced by `rkyv::to_bytes::<CachedResponse>` in the
/// same binary. Upheld because we write and read within the same shared library.
pub fn access_response(bytes: &[u8]) -> &Archived<CachedResponse> {
    unsafe { rkyv::access_unchecked::<Archived<CachedResponse>>(bytes) }
}
