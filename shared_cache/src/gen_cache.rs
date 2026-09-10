use std::collections::HashSet;
use std::sync::atomic::fence;
use std::sync::atomic::Ordering;
use std::time::{SystemTime, UNIX_EPOCH};

use memmap2::MmapMut;
use rkyv::util::AlignedVec;
use xxhash_rust::xxh3::xxh3_64;

use crate::cuckoo::CuckooFilter;
use crate::region::*;
use crate::types::{CachedResponse, RawSlot};

// ── Types ─────────────────────────────────────────────────────────────────────

/// Key hash + body fingerprint computed by `fast_check`, threaded into `set` so a cache write
/// doesn't hash the key and sample the body a second time for the same call.
pub struct PendingWrite {
    hash: u64,
    content_vh: u64,
    new_body_len: u32,
}

struct SurvivorEntry {
    hash: u64,
    expiry_ns: u64,
    value_hash: u64,
    body_len: u32,
    key_bytes: Vec<u8>,
    value_bytes: Vec<u8>,
}

/// Hash of first min(N,len) bytes mixed with last min(N,len) bytes.
/// For large bodies this hashes ~2×N bytes instead of the full length.
/// rotate_left(32) prevents cancellation when head and tail happen to be identical.
fn sampled_body_hash(body: Option<&[u8]>) -> u64 {
    const N: usize = 64;
    let b = match body { Some(b) if !b.is_empty() => b, _ => return 0 };
    if b.len() <= N {
        return xxh3_64(b);
    }
    xxh3_64(&b[..N]) ^ xxh3_64(&b[b.len() - N..]).rotate_left(32)
}

/// Occupancy of one page, for `SharedCache.stats()`: a page whose arena is full well below
/// `gen_maxsize` entries is the arena-bound case that used to leave the cache write-dead.
pub struct PageStats {
    pub entry_count: u32,
    pub arena_used: u32,
    pub arena_capacity: u32,
    pub sealed: bool,
}

pub struct GenerationalSharedCache {
    mmap: MmapMut,
    /// Reused per-hit copy buffer for `get_with`, so the copy-before-decode costs a memcpy but
    /// not an allocation. 16-byte aligned because rkyv's archived types are read in place.
    scratch: AlignedVec,
    n_pages: usize,
    gen_maxsize: usize,
    slot_count_per_page: usize,
    filter_bucket_count: usize,
    page_region_start: usize,
    page_size: usize,
    arena_start_in_page: usize,
    default_ttl_ns: Option<u64>,
}

// ── Init helper ───────────────────────────────────────────────────────────────

/// Write CoordHeader + PageHeaders into a (possibly zeroed) mmap. Called under the flock
/// in open() and also by invalidate() (which uses the spinlock instead). Having this as a
/// free function lets both call sites avoid duplicating the layout arithmetic.
// too_many_arguments: nine layout parameters that the two call sites (open() under the flock,
// invalidate() under the spinlock) already hold as separate values. Bundling them into a
// `Layout` struct is the right cleanup — the same treatment #757 gave the engine's query
// layer — but it belongs in its own change, not a lint pass.
#[allow(clippy::too_many_arguments)]
fn write_init_headers(
    mmap: &mut MmapMut,
    n_pages: usize,
    maxsize: usize,
    gen_maxsize: usize,
    slot_count_per_page: usize,
    arena_per_page: usize,
    filter_bucket_count: usize,
    page_region_start: usize,
    page_size: usize,
) {
    let c = unsafe { &mut *(mmap.as_mut_ptr() as *mut CoordHeader) };
    c.magic = MAGIC;
    c.version = VERSION;
    c.n_pages = n_pages as u32;
    c.maxsize = maxsize as u32;
    c.gen_maxsize = gen_maxsize as u32;
    c.counter = 0;
    c.slot_count_per_page = slot_count_per_page as u32;
    c.arena_per_page = arena_per_page as u32;
    c.filter_bucket_count = filter_bucket_count as u32;
    for i in 0..n_pages {
        let ph_offset = page_region_start + i * page_size;
        let ph = unsafe { &mut *(mmap.as_mut_ptr().add(ph_offset) as *mut PageHeader) };
        ph.arena_head = 0;
        ph.entry_count = 0;
        ph.is_sealed = if i == 0 { 0 } else { 1 };
        ph.generation = 0;
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64
}

fn expiry_ns_for(ttl_secs: Option<f64>, default_ttl_ns: Option<u64>) -> u64 {
    let ttl_ns = ttl_secs
        .map(|s| (s * 1e9) as u64)
        .or(default_ttl_ns);
    match ttl_ns {
        Some(t) => now_ns().saturating_add(t),
        None => u64::MAX,
    }
}

pub fn access_response(bytes: &[u8]) -> &rkyv::Archived<CachedResponse> {
    unsafe { rkyv::access_unchecked::<rkyv::Archived<CachedResponse>>(bytes) }
}

// ── Impl ──────────────────────────────────────────────────────────────────────

impl GenerationalSharedCache {
    // ── Accessors (all unsafe raw pointer ops) ─────────────────────────────

    fn coord(&self) -> &CoordHeader {
        unsafe { &*(self.mmap.as_ptr() as *const CoordHeader) }
    }

    fn coord_mut(&mut self) -> &mut CoordHeader {
        unsafe { &mut *(self.mmap.as_mut_ptr() as *mut CoordHeader) }
    }

    fn active_idx(&self) -> usize {
        self.coord().counter as usize % self.n_pages
    }

    fn page_offset(&self, page_idx: usize) -> usize {
        self.page_region_start + page_idx * self.page_size
    }

    fn page_header_ptr(&self, page_idx: usize) -> *const PageHeader {
        unsafe { self.mmap.as_ptr().add(self.page_offset(page_idx)) as *const PageHeader }
    }

    fn page_header(&self, page_idx: usize) -> &PageHeader {
        unsafe { &*self.page_header_ptr(page_idx) }
    }

    fn page_header_mut(&mut self, page_idx: usize) -> &mut PageHeader {
        unsafe {
            &mut *(self.mmap.as_mut_ptr().add(self.page_offset(page_idx)) as *mut PageHeader)
        }
    }

    fn slot_ptr(&self, page_idx: usize, slot_idx: u32) -> *const RawSlot {
        unsafe {
            self.mmap
                .as_ptr()
                .add(self.page_offset(page_idx) + PAGE_HEADER_SIZE + slot_idx as usize * SLOT_SIZE)
                as *const RawSlot
        }
    }

    fn slot_ptr_mut(&mut self, page_idx: usize, slot_idx: u32) -> *mut RawSlot {
        unsafe {
            self.mmap
                .as_mut_ptr()
                .add(self.page_offset(page_idx) + PAGE_HEADER_SIZE + slot_idx as usize * SLOT_SIZE)
                as *mut RawSlot
        }
    }

    fn arena_base(&self, page_idx: usize) -> *const u8 {
        unsafe {
            self.mmap
                .as_ptr()
                .add(self.page_offset(page_idx) + self.arena_start_in_page)
        }
    }

    fn arena_base_mut(&mut self, page_idx: usize) -> *mut u8 {
        unsafe {
            self.mmap
                .as_mut_ptr()
                .add(self.page_offset(page_idx) + self.arena_start_in_page)
        }
    }

    fn filter(&self) -> CuckooFilter {
        CuckooFilter::new(
            unsafe { self.mmap.as_ptr().add(filter_offset()) as *mut u8 },
            self.filter_bucket_count as u32,
        )
    }

    fn page_generation(&self, page_idx: usize) -> u32 {
        read_page_generation(unsafe {
            self.mmap.as_ptr().add(self.page_offset(page_idx))
        })
    }

    // ── Locking ───────────────────────────────────────────────────────────

    /// Take the spinlock. If it had to be stolen from a process that died holding it, the shared
    /// state is re-initialised first: the dead holder may have left a page half-zeroed, a
    /// generation odd (lock-free readers would skip that page forever) or the ring counter
    /// un-advanced, and none of that can be told apart from a healthy cache afterwards. Losing
    /// the cache's contents once after a worker crash is the safe outcome; serving from a torn
    /// page is not.
    fn lock(&mut self) -> bool {
        match try_lock_outcome(&self.mmap) {
            LockOutcome::Acquired => true,
            LockOutcome::Stolen => {
                self.reinit_locked();
                true
            }
            LockOutcome::Busy => false,
        }
    }

    /// Zero the filter and every page and reset the headers. The caller holds the spinlock.
    fn reinit_locked(&mut self) {
        let fb = filter_bytes(self.filter_bucket_count);
        unsafe {
            std::ptr::write_bytes(self.mmap.as_mut_ptr().add(filter_offset()), 0, fb);
        }
        for i in 0..self.n_pages {
            let base = unsafe { self.mmap.as_mut_ptr().add(self.page_offset(i)) };
            // Odd while the page is zeroed (seqlock protocol), then the next even value. Set, not
            // bumped: after a steal the parity is whatever the dead writer left, and two bumps
            // from odd would leave the page permanently "mid-rotation".
            let odd = read_page_generation(base) | 1;
            set_page_generation(base, odd);
            let data_start = self.page_offset(i) + PAGE_HEADER_SIZE;
            let data_len = self.slot_count_per_page * SLOT_SIZE + (self.page_size - self.arena_start_in_page);
            unsafe {
                std::ptr::write_bytes(self.mmap.as_mut_ptr().add(data_start), 0, data_len);
            }
            let ph = self.page_header_mut(i);
            ph.arena_head = 0;
            ph.entry_count = 0;
            ph.is_sealed = if i == 0 { 0 } else { 1 };
            set_page_generation(base, odd.wrapping_add(1));
        }
        self.coord_mut().counter = 0;
    }

    // ── Arena allocation ──────────────────────────────────────────────────

    fn alloc_arena(
        &mut self,
        page_idx: usize,
        value_len: usize,
        key_len: usize,
    ) -> Option<(u32, u32)> {
        let value_padded = (value_len + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);
        let key_padded   = (key_len   + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);
        let total = value_padded + key_padded;
        let arena_capacity = self.page_size - self.arena_start_in_page;
        if self.page_header(page_idx).arena_head as usize + total > arena_capacity {
            return None;
        }
        let old_head = self.page_header(page_idx).arena_head;
        self.page_header_mut(page_idx).arena_head = old_head + total as u32;
        Some((old_head, old_head + value_padded as u32))
    }

    // ── Probe & insert ────────────────────────────────────────────────────

    fn key_matches(&self, page_idx: usize, slot: &RawSlot, key: &[u8]) -> bool {
        if slot.key_len as usize != key.len() {
            return false;
        }
        let start = slot.key_offset as usize;
        let arena = self.arena_base(page_idx);
        let stored = unsafe { std::slice::from_raw_parts(arena.add(start), slot.key_len as usize) };
        stored == key
    }

    /// Returns (arena_offset, arena_len, slot_idx) or None.
    /// Lock-free safe for sealed pages.
    fn do_probe(&self, page_idx: usize, hash: u64, key: &[u8]) -> Option<(u32, u32, u32)> {
        let slot_count = self.slot_count_per_page as u32;
        let mut idx = (hash as u32) % slot_count;
        loop {
            let slot = unsafe { &*self.slot_ptr(page_idx, idx) };
            match read_key_hash(self.slot_ptr(page_idx, idx) as *const u8) {
                EMPTY => return None,
                h if h == hash => {
                    let expires = slot.expiry_ns;
                    if expires != u64::MAX && expires <= now_ns() {
                        // expired — treat as miss but keep probing (another key could be here)
                    } else if self.key_matches(page_idx, slot, key) {
                        return Some((slot.arena_offset, slot.arena_len, idx));
                    }
                }
                _ => {}
            }
            idx = (idx + 1) % slot_count;
            if idx == (hash as u32) % slot_count {
                return None; // full loop
            }
        }
    }

    // too_many_arguments: the fields of the entry being written, pre-hashed by the caller so
    // the probe loop above and this insert share one hash computation. Bundleable (see
    // write_init_headers' note).
    #[allow(clippy::too_many_arguments)]
    fn do_insert(
        &mut self,
        page_idx: usize,
        hash: u64,
        key: &[u8],
        value_bytes: &[u8],
        expiry_ns: u64,
        value_hash: u64,
        body_len: u32,
    ) -> bool {
        let slot_count = self.slot_count_per_page as u32;
        let mut idx = (hash as u32) % slot_count;
        let start_idx = idx;
        let mut existing_idx: Option<u32> = None;
        let mut tombstone_idx: Option<u32> = None;
        loop {
            let slot = unsafe { &*self.slot_ptr(page_idx, idx) };
            match slot.key_hash {
                EMPTY => break,
                TOMBSTONE => { if tombstone_idx.is_none() { tombstone_idx = Some(idx); } }
                h if h == hash && self.key_matches(page_idx, slot, key) => {
                    existing_idx = Some(idx);
                    break;
                }
                _ => {}
            }
            idx = (idx + 1) % slot_count;
            if idx == start_idx {
                if tombstone_idx.is_none() {
                    return false; // table full
                }
                break; // table full of tombstones+occupied; reuse tombstone below
            }
        }
        let value_padded = (value_bytes.len() + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);

        // For existing slots: reuse the arena allocation if the new value fits, otherwise
        // allocate only value bytes (key is unchanged). For tombstone reuse: reuse the value
        // arena if it fits (slot is invisible to readers so no seqlock needed), allocate only
        // key bytes from the bump allocator. For new slots: allocate both.
        let mut is_inplace_reuse = false;
        let (val_off, val_capacity, new_key_off) = if let Some(eidx) = existing_idx {
            let old_offset   = unsafe { (*self.slot_ptr(page_idx, eidx)).arena_offset };
            let old_capacity = unsafe { (*self.slot_ptr(page_idx, eidx)).arena_capacity };
            if value_bytes.len() <= old_capacity as usize {
                is_inplace_reuse = true;
                (old_offset, old_capacity, None)
            } else {
                let old_head = self.page_header(page_idx).arena_head;
                if old_head as usize + value_padded > self.page_size - self.arena_start_in_page {
                    return false; // arena full
                }
                self.page_header_mut(page_idx).arena_head = old_head + value_padded as u32;
                (old_head, value_padded as u32, None)
            }
        } else if let Some(tidx) = tombstone_idx {
            let old_offset   = unsafe { (*self.slot_ptr(page_idx, tidx)).arena_offset };
            let old_capacity = unsafe { (*self.slot_ptr(page_idx, tidx)).arena_capacity };
            if value_bytes.len() <= old_capacity as usize {
                // Value fits in former tenant's allocation; only allocate key bytes.
                // No seqlock needed: slot is invisible (key_hash = TOMBSTONE) until we
                // write key_hash last, so no concurrent reader can reach this arena region.
                let key_padded = (key.len() + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);
                let old_head = self.page_header(page_idx).arena_head;
                if old_head as usize + key_padded > self.page_size - self.arena_start_in_page {
                    return false; // arena full for key bytes
                }
                self.page_header_mut(page_idx).arena_head = old_head + key_padded as u32;
                (old_offset, old_capacity, Some(old_head))
            } else {
                let Some((val_off, key_off)) = self.alloc_arena(page_idx, value_bytes.len(), key.len()) else {
                    return false; // arena full
                };
                (val_off, value_padded as u32, Some(key_off))
            }
        } else {
            let Some((val_off, key_off)) = self.alloc_arena(page_idx, value_bytes.len(), key.len()) else {
                return false; // arena full
            };
            (val_off, value_padded as u32, Some(key_off))
        };

        // Seqlock: bracket in-place overwrites so get_with can detect a concurrent update.
        if is_inplace_reuse { inc_value_seq(self.slot_ptr_mut(page_idx, idx) as *mut u8); }
        unsafe {
            let ab = self.arena_base_mut(page_idx);
            std::ptr::copy_nonoverlapping(value_bytes.as_ptr(), ab.add(val_off as usize), value_bytes.len());
            if let Some(key_off) = new_key_off {
                std::ptr::copy_nonoverlapping(key.as_ptr(), ab.add(key_off as usize), key.len());
            }
        }
        if is_inplace_reuse { inc_value_seq(self.slot_ptr_mut(page_idx, idx) as *mut u8); }
        let is_new = existing_idx.is_none();
        let slot_idx = existing_idx.or(tombstone_idx).unwrap_or(idx);
        let slot = self.slot_ptr_mut(page_idx, slot_idx);
        unsafe {
            (*slot).expiry_ns      = expiry_ns;
            (*slot).value_hash     = value_hash;
            (*slot).body_len       = body_len;
            (*slot).arena_offset   = val_off;
            (*slot).arena_len      = value_bytes.len() as u32;
            (*slot).arena_capacity = val_capacity;
            if let Some(key_off) = new_key_off {
                (*slot).key_offset = key_off;
                (*slot).key_len    = key.len() as u32;
            }
            (*slot).visited        = 0;
            // Write key_hash last — readers skip EMPTY slots, so writing hash
            // last makes the entry visible only once fully written.
            // Release store pairs with read_key_hash()'s Acquire load in lock-free paths.
            write_key_hash(slot as *mut u8, hash);
        }
        if is_new {
            self.page_header_mut(page_idx).entry_count += 1;
        }
        true
    }

    // ── Rotation ──────────────────────────────────────────────────────────

    /// Is `key` (with this hash) present and unexpired in any page other than `except`?
    /// Must be called under the spinlock: it reads the other pages' slot tables directly.
    fn live_in_other_pages(&self, except: usize, hash: u64, key: &[u8]) -> bool {
        (0..self.n_pages)
            .filter(|&p| p != except)
            .any(|p| self.do_probe(p, hash, key).is_some())
    }

    fn scan_survivors(&self, page_idx: usize) -> Vec<SurvivorEntry> {
        // Abort immediately if another worker is mid-rotation on this page (odd generation).
        if self.page_generation(page_idx) & 1 != 0 { return Vec::new(); }

        // Per-chunk seqlock: scan CHUNK slots, check generation before and after.
        // If the generation changed (pop() tombstoned a slot in this chunk), retry up to
        // MAX_RETRIES times. On retry the tombstoned slot reads as TOMBSTONE and is skipped
        // correctly. After MAX_RETRIES failures the chunk is skipped entirely — we lose at
        // most CHUNK survivors rather than the whole page.
        const CHUNK: usize = 64;
        const MAX_RETRIES: usize = 3;

        let slot_count = self.slot_count_per_page;
        let mut survivors = Vec::new();
        let arena = self.arena_base(page_idx);
        let now = now_ns();

        let mut i = 0;
        while i < slot_count {
            let chunk_end = (i + CHUNK).min(slot_count);

            for _attempt in 0..MAX_RETRIES {
                let page_gen = self.page_generation(page_idx);
                if page_gen & 1 != 0 { break; } // mid-rotation: skip chunk

                let mut chunk_survivors = Vec::new();
                for j in i..chunk_end {
                    // Acquire load pairs with write_key_hash()'s Release store in do_insert
                    // and pop().
                    let key_hash = read_key_hash(self.slot_ptr(page_idx, j as u32) as *const u8);
                    if key_hash == EMPTY || key_hash == TOMBSTONE { continue; }
                    if read_visited(self.slot_ptr(page_idx, j as u32) as *const u8) == 0 { continue; }
                    let slot = unsafe { &*self.slot_ptr(page_idx, j as u32) };
                    if slot.expiry_ns != u64::MAX && slot.expiry_ns <= now { continue; }
                    let key_bytes = unsafe {
                        std::slice::from_raw_parts(
                            arena.add(slot.key_offset as usize),
                            slot.key_len as usize,
                        )
                        .to_vec()
                    };
                    let value_bytes = unsafe {
                        std::slice::from_raw_parts(
                            arena.add(slot.arena_offset as usize),
                            slot.arena_len as usize,
                        )
                        .to_vec()
                    };
                    chunk_survivors.push(SurvivorEntry {
                        hash: key_hash,
                        expiry_ns: slot.expiry_ns,
                        value_hash: slot.value_hash,
                        body_len: slot.body_len,
                        key_bytes,
                        value_bytes,
                    });
                }

                if self.page_generation(page_idx) == page_gen {
                    // Stable read: commit this chunk's survivors.
                    survivors.extend(chunk_survivors);
                    break;
                }
                // Generation changed mid-chunk (pop() raced us); retry.
            }
            // If all retries exhausted without a stable read, chunk is silently skipped.

            i = chunk_end;
        }

        survivors
    }

    fn commit_rotation(&mut self, survivors: Vec<SurvivorEntry>) {
        let active_idx = self.active_idx();
        let retiring_idx = (active_idx + 1) % self.n_pages;

        // 1. Odd bump: signals write-in-progress to lock-free readers (seqlock protocol).
        //    Readers that see an odd generation skip this page entirely.
        bump_page_generation(unsafe {
            self.mmap.as_mut_ptr().add(self.page_offset(retiring_idx))
        });

        // 2. Seal active page; reset visited bits for a fresh trial period.
        self.page_header_mut(active_idx).is_sealed = 1;
        for i in 0..self.slot_count_per_page {
            let sp = self.slot_ptr(active_idx, i as u32) as *const u8;
            let slot = unsafe { &*(sp as *const RawSlot) };
            if slot.key_hash != EMPTY {
                clear_visited(sp);
            }
        }

        // 3a. Drop the fingerprints of the entries that die with the retiring page. The filter is
        //     shared by every page, so a fingerprint goes only if the key is neither a survivor nor
        //     live in another page (a key re-set after this page was sealed has a newer copy there,
        //     and its fingerprint must stay). Without this each rotation left gen_maxsize dead
        //     fingerprints behind, the filter saturated, and every subsequent insert's failed
        //     kick sequence evicted a random *live* fingerprint — false misses on cached keys.
        let survivor_hashes: HashSet<u64> = survivors.iter().map(|s| s.hash).collect();
        let arena = self.arena_base(retiring_idx);
        for i in 0..self.slot_count_per_page {
            let slot = unsafe { &*self.slot_ptr(retiring_idx, i as u32) };
            let hash = slot.key_hash;
            if hash == EMPTY || hash == TOMBSTONE || survivor_hashes.contains(&hash) {
                continue;
            }
            let key = unsafe {
                std::slice::from_raw_parts(arena.add(slot.key_offset as usize), slot.key_len as usize)
            };
            if !self.live_in_other_pages(retiring_idx, hash, key) {
                self.filter().delete(hash);
            }
        }

        // 3b. Zero retiring page data (slot table + arena only; preserve header ptr).
        let data_start = self.page_offset(retiring_idx) + PAGE_HEADER_SIZE;
        let data_len = self.slot_count_per_page * SLOT_SIZE
            + (self.page_size - self.arena_start_in_page);
        unsafe {
            std::ptr::write_bytes(self.mmap.as_mut_ptr().add(data_start), 0, data_len);
        }
        {
            let ph = self.page_header_mut(retiring_idx);
            ph.arena_head = 0;
            ph.entry_count = 0;
        }

        // 4. Re-insert survivors into the (now-blank) retiring page. If a survivor doesn't
        //    fit (arena full), remove it from the filter so it doesn't become a permanent
        //    false positive — unless a newer copy lives in another page.
        for s in survivors {
            if !self.do_insert(retiring_idx, s.hash, &s.key_bytes, &s.value_bytes, s.expiry_ns, s.value_hash, s.body_len)
                && !self.live_in_other_pages(retiring_idx, s.hash, &s.key_bytes)
            {
                self.filter().delete(s.hash);
            }
        }

        // 5. Even bump: page data is stable; lock-free readers may probe it again.
        bump_page_generation(unsafe {
            self.mmap.as_mut_ptr().add(self.page_offset(retiring_idx))
        });

        // 6. Unseal retiring page — it becomes the new active page.
        self.page_header_mut(retiring_idx).is_sealed = 0;

        // 7. Advance ring buffer counter.
        self.coord_mut().counter = self.coord().counter.wrapping_add(1);
    }

    // ── Public API ────────────────────────────────────────────────────────

    pub fn open(
        path: &str,
        maxsize: usize,
        n_pages: usize,
        default_ttl_secs: Option<f64>,
        arena_mb: Option<usize>,
    ) -> std::io::Result<Self> {
        let n_pages = n_pages.max(2);
        let gen_maxsize = (maxsize / n_pages).max(1);
        let slot_count_per_page = compute_slot_count(gen_maxsize);
        let filter_bucket_count = compute_filter_bucket_count(maxsize);
        let arena_per_page = arena_mb
            .map(|mb| mb * 1024 * 1024 / n_pages)
            .unwrap_or(gen_maxsize * 8192);
        let arena_per_page = (arena_per_page + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);
        let fsize = total_file_size(filter_bucket_count, n_pages, slot_count_per_page, arena_per_page);
        let prs = page_region_start(filter_bucket_count);
        let ps = page_size(slot_count_per_page, arena_per_page);
        let asip = arena_start_in_page(slot_count_per_page);

        // The compat check and reinit run inside open_mmap while the flock is still held.
        // This serializes concurrent workers at startup: the first one to acquire the lock
        // initializes the file; subsequent workers see a valid header and skip reinit.
        let mmap = open_mmap(path, fsize, |mmap| {
            let c = unsafe { &*(mmap.as_ptr() as *const CoordHeader) };
            let compatible = c.magic == MAGIC
                && c.version == VERSION
                && c.n_pages == n_pages as u32
                && c.maxsize == maxsize as u32
                && c.gen_maxsize == gen_maxsize as u32
                && c.slot_count_per_page == slot_count_per_page as u32
                && c.arena_per_page == arena_per_page as u32
                && c.filter_bucket_count == filter_bucket_count as u32;
            if !compatible {
                unsafe { std::ptr::write_bytes(mmap.as_mut_ptr(), 0, fsize); }
                write_init_headers(mmap, n_pages, maxsize, gen_maxsize, slot_count_per_page, arena_per_page, filter_bucket_count, prs, ps);
            }
        })?;

        Ok(GenerationalSharedCache {
            mmap,
            scratch: AlignedVec::new(),
            n_pages,
            gen_maxsize,
            slot_count_per_page,
            filter_bucket_count,
            page_region_start: prs,
            page_size: ps,
            arena_start_in_page: asip,
            default_ttl_ns: default_ttl_secs.map(|s| (s * 1e9) as u64),
        })
    }

    /// Call `f` with a private copy of the entry's bytes.
    ///
    /// The lock is released before the bytes are read, so another process can be writing them
    /// (active page: an in-place update bracketed by value_seq increments; sealed page: rotation
    /// zeroing bracketed by odd generation bumps). The seqlock protocol is therefore: snapshot
    /// seq/generation, copy the bytes out, re-check, and only then decode the copy. A torn copy is
    /// detected by the re-check and discarded before `f` ever sees it — the decoder
    /// (`rkyv::access_unchecked`, which trusts every relative pointer it reads) never runs over a
    /// buffer that can change underneath it. The copy goes into a reused aligned scratch buffer,
    /// so it costs a memcpy but no allocation: measured at +~70 ns on a 5 KB body (517 -> 585 ns
    /// per hit) and +~320 ns on a 24 KB body (851 -> 1175 ns), against a miss of ~57 ns.
    pub fn get_with<F, T>(&mut self, key: &[u8], f: F) -> Option<T>
    where
        F: FnOnce(&[u8]) -> T,
    {
        let hash = normalize_hash(xxh3_64(key));

        // Lock-free filter check.
        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) {
            return None;
        }

        // Probe active page under lock; snapshot (abs, len, gen) then release before calling f.
        if !self.lock() {
            return None;
        }
        let active_idx = self.active_idx();
        let active_snap = self.do_probe(active_idx, hash, key).map(|(off, len, slot_idx)| {
            let abs = self.page_offset(active_idx) + self.arena_start_in_page + off as usize;
            let page_gen = self.page_generation(active_idx);
            let seq = read_value_seq(self.slot_ptr(active_idx, slot_idx) as *const u8);
            (abs, len as usize, page_gen, slot_idx, seq)
        });
        unlock(&self.mmap);

        if let Some((abs, len, gen_before, slot_idx, seq_before)) = active_snap {
            let buf = self.copy_out(abs, len);
            // Seqlock read side: the data reads above must complete before the re-checks below.
            fence(Ordering::Acquire);
            let stable = self.page_generation(active_idx) == gen_before
                && read_value_seq(self.slot_ptr(active_idx, slot_idx) as *const u8) == seq_before;
            let result = if stable { Some(f(buf.as_slice())) } else { None };
            self.scratch = buf;
            return result;
        }

        // Probe sealed pages lock-free, newest first.
        for i in 1..self.n_pages {
            let page_idx = (active_idx + self.n_pages - i) % self.n_pages;
            let gen_before = self.page_generation(page_idx);
            // Seqlock: odd generation means commit_rotation() is actively zeroing this page.
            // Skip rather than read bytes mid-zero; treat as a miss for this page.
            if gen_before & 1 != 0 { continue; }
            if let Some((off, len, slot_idx)) = self.do_probe(page_idx, hash, key) {
                set_visited(self.slot_ptr(page_idx, slot_idx) as *const u8);
                let abs = self.page_offset(page_idx) + self.arena_start_in_page + off as usize;
                let buf = self.copy_out(abs, len as usize);
                fence(Ordering::Acquire);
                // Discard if a rotation started or completed while the bytes were being copied.
                let stable = self.page_generation(page_idx) == gen_before;
                let result = if stable { Some(f(buf.as_slice())) } else { None };
                self.scratch = buf;
                return result;
            }
        }

        None // filter false positive
    }

    /// Copy `len` bytes at `abs` into the reusable scratch buffer and hand it out; the caller puts
    /// it back in `self.scratch` when done so the allocation is kept for the next hit.
    fn copy_out(&mut self, abs: usize, len: usize) -> AlignedVec {
        let mut buf = std::mem::replace(&mut self.scratch, AlignedVec::new());
        buf.clear();
        buf.extend_from_slice(&self.mmap[abs..abs + len]);
        buf
    }

    /// Returns `None` if `key` is already cached with identical content — caller can skip `set()`.
    /// Otherwise returns the key hash and body fingerprint `fast_check` had to compute anyway,
    /// so a subsequent `set()` call for the same write doesn't hash the key and sample the body
    /// a second time.
    /// Checks filter (lock-free) → active page under lock → sealed pages lock-free.
    /// Call this from the binding layer before extracting expensive fields (headers, counts).
    pub fn fast_check(&mut self, key: &[u8], body: Option<&[u8]>) -> Option<PendingWrite> {
        let hash = normalize_hash(xxh3_64(key));
        let new_body_len = body.map_or(u32::MAX, |b| b.len() as u32);
        let content_vh = sampled_body_hash(body);
        let pending = PendingWrite { hash, content_vh, new_body_len };

        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) { return Some(pending); }

        if !self.lock() { return Some(pending); }
        let active_idx = self.active_idx();
        let active_snap = self.do_probe(active_idx, hash, key).map(|(_, _, si)| {
            let s = unsafe { &*self.slot_ptr(active_idx, si) };
            (s.body_len, s.value_hash)
        });
        unlock(&self.mmap);

        if let Some((stored_len, stored_vh)) = active_snap {
            let up_to_date = stored_len == new_body_len && content_vh == stored_vh;
            return if up_to_date { None } else { Some(pending) };
        }

        for i in 1..self.n_pages {
            let page_idx = (active_idx + self.n_pages - i) % self.n_pages;
            let gen_before = self.page_generation(page_idx);
            if gen_before & 1 != 0 { continue; } // page being zeroed by rotation — skip
            if let Some((_, _, si)) = self.do_probe(page_idx, hash, key) {
                let s = unsafe { &*self.slot_ptr(page_idx, si) };
                let matches = s.body_len == new_body_len && content_vh == s.value_hash;
                if self.page_generation(page_idx) != gen_before { return Some(pending); }
                return if matches { None } else { Some(pending) };
            }
        }
        Some(pending)
    }

    // too_many_arguments: this is the crate's public cache-write surface and mirrors the HTTP
    // response it stores (status/headers/body plus the counts and TTL). Grouping these would
    // change the public API, so it is deliberately left alone here.
    #[allow(clippy::too_many_arguments)]
    pub fn set(
        &mut self,
        key: &[u8],
        pending: PendingWrite,
        status: &str,
        headers: Vec<(String, String)>,
        body: Option<&[u8]>,
        result_count: Option<i64>,
        total_cards: Option<i64>,
        ttl_secs: Option<f64>,
    ) {
        let PendingWrite { hash, content_vh, new_body_len } = pending;

        let body_owned = body.map(|b| b.to_vec());
        let cr = CachedResponse { status: status.to_owned(), headers, body: body_owned, result_count, total_cards };
        let Ok(value_bytes) = rkyv::to_bytes::<rkyv::rancor::Error>(&cr) else { return; };
        let expiry = expiry_ns_for(ttl_secs, self.default_ttl_ns);

        // Step 1: rotate first if the active page has reached its entry budget.
        if !self.lock() { return; }
        let active_idx = self.active_idx();
        let needs_rotation = self.page_header(active_idx).entry_count >= self.gen_maxsize as u32;
        unlock(&self.mmap);
        if needs_rotation && !self.rotate() { return; }

        // Step 2: insert into the active page.
        let Some(inserted) = self.insert_active(hash, key, &value_bytes, expiry, content_vh, new_body_len) else {
            return;
        };
        if inserted || needs_rotation {
            return;
        }

        // Step 3: the page is full below its entry budget — its arena (maxsize x 8 KB by default) ran
        // out first, which happens as soon as bodies average more than the per-entry share. Nothing
        // else would ever rotate it (entry_count cannot grow), so every later set would be dropped
        // and the cache would be write-dead. Treat arena exhaustion as the rotation trigger and retry
        // once; a second failure means the survivors filled the fresh page, and that is left alone.
        // No rotation for a value that could not fit even an empty page: it would only evict.
        if !self.fits_empty_page(key.len(), value_bytes.len()) || !self.rotate() {
            return;
        }
        self.insert_active(hash, key, &value_bytes, expiry, content_vh, new_body_len);
    }

    /// One rotation: snapshot the counter under the lock, scan the retiring page's survivors
    /// lock-free, then commit under the lock unless another worker rotated in between (in which
    /// case its rotation serves). False only when the lock could not be taken.
    fn rotate(&mut self) -> bool {
        if !self.lock() { return false; }
        let active_idx = self.active_idx();
        let gen_snapshot = self.coord().counter;
        let retiring_idx = (active_idx + 1) % self.n_pages;
        unlock(&self.mmap);

        let survivors = self.scan_survivors(retiring_idx);

        if !self.lock() { return false; }
        if self.coord().counter == gen_snapshot {
            self.commit_rotation(survivors);
        }
        unlock(&self.mmap);
        true
    }

    /// Insert into whichever page is active, registering the key in the filter on success.
    /// `None` when the lock could not be taken; `Some(false)` when the page had no room.
    fn insert_active(
        &mut self,
        hash: u64,
        key: &[u8],
        value_bytes: &[u8],
        expiry_ns: u64,
        value_hash: u64,
        body_len: u32,
    ) -> Option<bool> {
        if !self.lock() { return None; }
        let active_idx = self.active_idx();
        let inserted = self.do_insert(active_idx, hash, key, value_bytes, expiry_ns, value_hash, body_len);
        if inserted {
            self.filter().insert(hash);
        }
        unlock(&self.mmap);
        Some(inserted)
    }

    /// Would a fresh (empty) page's arena hold this key + value?
    fn fits_empty_page(&self, key_len: usize, value_len: usize) -> bool {
        let value_padded = (value_len + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);
        let key_padded = (key_len + ARENA_ALIGN - 1) & !(ARENA_ALIGN - 1);
        value_padded + key_padded <= self.page_size - self.arena_start_in_page
    }

    /// Remove all copies of `key` from every page and the shared filter.
    /// Returns true if at least one copy was found and tombstoned.
    ///
    /// Every copy is tombstoned, so the fingerprint is deleted whichever page the key was found
    /// in; a tombstone keeps no hash, so a fingerprint left behind here would never be reclaimed.
    pub fn pop(&mut self, key: &[u8]) -> bool {
        let hash = normalize_hash(xxh3_64(key));
        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) {
            return false;
        }

        if !self.lock() { return false; }
        let active_idx = self.active_idx();
        let mut found = false;

        for page_idx in 0..self.n_pages {
            if let Some((_, _, slot_idx)) = self.do_probe(page_idx, hash, key) {
                let slot = self.slot_ptr_mut(page_idx, slot_idx) as *mut u8;
                if page_idx == active_idx {
                    // Invalidate any get_with() reader that snapshotted this slot before we
                    // tombstone it; without this bump the post-f value_seq check would pass
                    // and the caller would receive a value for a key that was just deleted.
                    inc_value_seq(slot);
                    self.page_header_mut(active_idx).entry_count =
                        self.page_header(active_idx).entry_count.saturating_sub(1);
                    write_key_hash(slot, TOMBSTONE);
                } else {
                    // Sealed page: bracket the TOMBSTONE write with generation bumps so
                    // scan_survivors()'s per-chunk seqlock detects the change and retries
                    // only the affected chunk rather than discarding the entire page.
                    let page_base = unsafe { self.mmap.as_mut_ptr().add(self.page_offset(page_idx)) };
                    bump_page_generation(page_base); // even → odd (write in progress)
                    write_key_hash(slot, TOMBSTONE);
                    bump_page_generation(page_base); // odd → even (stable)
                }
                found = true;
            }
        }
        if found {
            self.filter().delete(hash);
        }

        unlock(&self.mmap);
        found
    }

    pub fn invalidate(&mut self) {
        if !self.lock() { return; }
        self.reinit_locked();
        unlock(&self.mmap);
    }

    pub fn entry_count(&self) -> u32 {
        (0..self.n_pages).map(|i| self.page_header(i).entry_count).sum()
    }

    /// Number of rotations since the file was (re)initialised.
    pub fn rotation_count(&self) -> u32 {
        self.coord().counter
    }

    pub fn gen_maxsize(&self) -> usize {
        self.gen_maxsize
    }

    /// Per-page occupancy, unlocked: a snapshot for diagnostics, not a consistent view.
    pub fn page_stats(&self) -> Vec<PageStats> {
        let arena_capacity = (self.page_size - self.arena_start_in_page) as u32;
        (0..self.n_pages)
            .map(|i| {
                let ph = self.page_header(i);
                PageStats {
                    entry_count: ph.entry_count,
                    arena_used: ph.arena_head,
                    arena_capacity,
                    sealed: ph.is_sealed != 0,
                }
            })
            .collect()
    }

    pub fn contains(&mut self, key: &[u8]) -> bool {
        let hash = normalize_hash(xxh3_64(key));
        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) {
            return false;
        }
        // Full slot probe — no arena copy, no deserialization. Correctly returns false
        // for filter false positives and tombstoned entries (e.g. after pop()).
        if !self.lock() { return false; }
        let active_idx = self.active_idx();
        let in_active = self.do_probe(active_idx, hash, key).is_some();
        unlock(&self.mmap);
        if in_active { return true; }
        for i in 1..self.n_pages {
            let page_idx = (active_idx + self.n_pages - i) % self.n_pages;
            let gen_before = self.page_generation(page_idx);
            if gen_before & 1 != 0 { continue; } // page being zeroed by rotation — skip
            if self.do_probe(page_idx, hash, key).is_some() {
                if self.page_generation(page_idx) != gen_before { continue; }
                return true;
            }
        }
        false
    }

    /// Benchmarking helper: filter check + lock + active-page probe + unlock. No arena copy.
    pub fn probe_only(&mut self, key: &[u8]) -> bool {
        let hash = normalize_hash(xxh3_64(key));
        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) {
            return false;
        }
        if !self.lock() {
            return false;
        }
        let active_idx = self.active_idx();
        let found = self.do_probe(active_idx, hash, key).is_some();
        unlock(&self.mmap);
        found
    }
}

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::*;

    pub(super) fn temp_path(name: &str) -> String {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let mut p = std::env::temp_dir();
        p.push(format!("shared_cache_{name}_{}_{nanos}.cache", std::process::id()));
        let _ = std::fs::remove_file(&p);
        p.to_string_lossy().into_owned()
    }

    pub(super) fn put(cache: &mut GenerationalSharedCache, key: &[u8], body: &[u8]) {
        let pending = cache.fast_check(key, Some(body)).expect("content differs from what is cached");
        cache.set(key, pending, "200 OK", Vec::new(), Some(body), Some(1), Some(2), None);
    }

    pub(super) fn get_body(cache: &mut GenerationalSharedCache, key: &[u8]) -> Option<Vec<u8>> {
        cache
            .get_with(key, |b| access_response(b).body.as_ref().map(|v| v.as_slice().to_vec()))
            .flatten()
    }

    #[test]
    fn arena_exhaustion_rotates_instead_of_leaving_the_page_write_dead() {
        // maxsize 64 over 2 pages: gen_maxsize 32 and a 32 x 8 KB = 256 KB arena per page. Bodies of
        // three times the per-entry share fill the arena after ~10 entries, long before the page
        // reaches its 32-entry budget — the only rotation trigger before this fix.
        let path = temp_path("arena");
        let mut cache = GenerationalSharedCache::open(&path, 64, 2, None, None).unwrap();
        let body = vec![0xABu8; 3 * 8192];

        for i in 0..200u32 {
            let key = format!("key-{i}");
            put(&mut cache, key.as_bytes(), &body);
            assert_eq!(
                get_body(&mut cache, key.as_bytes()).as_deref(),
                Some(body.as_slice()),
                "key {i} was dropped: the cache stopped admitting writes"
            );
        }

        let live = cache.entry_count();
        assert!(live > 0 && live <= 64, "live entries {live} outside (0, maxsize]");
        assert!(cache.rotation_count() >= 10, "only {} rotations", cache.rotation_count());
        let stats = cache.page_stats();
        assert_eq!(stats.len(), 2);
        assert!(stats.iter().all(|p| p.arena_used <= p.arena_capacity));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn rotations_do_not_saturate_the_filter() {
        // maxsize 64 gives a 64-bucket filter: 256 fingerprint slots for at most 64 live entries.
        // Each rotation used to leave its page's dead fingerprints behind, so after a few hundred
        // sets the filter was full and every insert's failed kick evicted a live fingerprint:
        // keys that were cached went missing.
        let path = temp_path("filter");
        let mut cache = GenerationalSharedCache::open(&path, 64, 2, None, None).unwrap();
        let recent = 8usize; // well inside two rotations (gen_maxsize 32), so all still cached
        let keys: Vec<String> = (0..20_000u32).map(|i| format!("k{i}")).collect();
        for i in 0..keys.len() {
            put(&mut cache, keys[i].as_bytes(), b"v");
            for k in &keys[i.saturating_sub(recent)..=i] {
                assert!(cache.contains(k.as_bytes()), "{k} lost after set {i}: filter dropped a live fingerprint");
            }
        }
        assert!(cache.rotation_count() > 500);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn a_key_re_set_after_rotation_keeps_its_fingerprint_when_the_old_copy_dies() {
        let path = temp_path("reset");
        let mut cache = GenerationalSharedCache::open(&path, 64, 2, None, None).unwrap();
        put(&mut cache, b"hot", b"v1");
        // gen_maxsize is 32. 40 sets: one rotation, "hot" v1 now sits in the sealed page and the
        // active page holds a32..a39. Re-set "hot" (v2 lands in the active page), then 30 more
        // sets: exactly one further rotation, which zeroes the page holding v1 — a dead copy of a
        // key that is live in the other page. Its fingerprint must not be deleted with v1.
        for i in 0..40u32 { put(&mut cache, format!("a{i}").as_bytes(), b"x"); }
        assert_eq!(cache.rotation_count(), 1);
        put(&mut cache, b"hot", b"v2");
        for i in 0..30u32 { put(&mut cache, format!("b{i}").as_bytes(), b"x"); }
        assert_eq!(cache.rotation_count(), 2);
        assert!(cache.contains(b"hot"));
        assert_eq!(get_body(&mut cache, b"hot").as_deref(), Some(&b"v2"[..]));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn pop_of_a_sealed_page_key_clears_it_everywhere() {
        let path = temp_path("pop");
        let mut cache = GenerationalSharedCache::open(&path, 64, 2, None, None).unwrap();
        put(&mut cache, b"gone", b"v");
        for i in 0..40u32 { put(&mut cache, format!("a{i}").as_bytes(), b"x"); }
        assert!(cache.rotation_count() >= 1);
        assert!(cache.pop(b"gone"));
        assert!(!cache.contains(b"gone"));
        assert!(get_body(&mut cache, b"gone").is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn a_lock_stolen_from_a_dead_writer_reinitialises_the_cache() {
        use std::sync::atomic::AtomicU32;
        let path = temp_path("steal");
        let mut cache = GenerationalSharedCache::open(&path, 64, 2, None, None).unwrap();
        put(&mut cache, b"k1", b"v1");
        assert!(cache.contains(b"k1"));

        // A writer that died mid-rotation: the lock word names a pid that no longer exists, and
        // the page it was zeroing was left at an odd generation with the ring counter un-advanced.
        let word = unsafe { &*(cache.mmap.as_ptr() as *const AtomicU32) };
        word.store(i32::MAX as u32, Ordering::Relaxed);
        bump_page_generation(unsafe { cache.mmap.as_mut_ptr().add(cache.page_offset(1)) });
        assert_eq!(cache.page_generation(1) & 1, 1);

        assert!(!cache.contains(b"k1"), "a torn cache was served as if intact");
        assert_eq!(word.load(Ordering::Relaxed), 0, "lock not released after the steal");
        assert_eq!(cache.entry_count(), 0);
        assert!((0..2).all(|p| cache.page_generation(p) & 1 == 0), "a generation was left odd");
        assert_eq!(cache.rotation_count(), 0);

        put(&mut cache, b"k2", b"v2");
        assert_eq!(get_body(&mut cache, b"k2").as_deref(), Some(&b"v2"[..]));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn a_value_larger_than_the_arena_does_not_trigger_a_rotation() {
        let path = temp_path("oversize");
        let mut cache = GenerationalSharedCache::open(&path, 64, 2, None, None).unwrap();
        put(&mut cache, b"small", b"x");
        let before = cache.rotation_count();

        let huge = vec![0u8; 512 * 1024];
        put(&mut cache, b"huge", &huge);

        assert_eq!(cache.rotation_count(), before, "an unfittable value evicted a page for nothing");
        assert_eq!(get_body(&mut cache, b"small").as_deref(), Some(&b"x"[..]));
        assert!(get_body(&mut cache, b"huge").is_none());
        let _ = std::fs::remove_file(path);
    }
}

/// Micro-benchmark for the fast_check→set hashing duplication (perf-audit finding #3).
///
/// Before this fix, `SharedCache::set` (lib.rs) called `fast_check` to decide whether a write is
/// needed, then — on the "content changed" branch — called `set`, which independently
/// recomputed `normalize_hash(xxh3_64(key))` and `sampled_body_hash(body)`: the exact two values
/// `fast_check` had just computed for the same call. This times that duplicated hashing work
/// directly (no mmap, no lock, no rkyv), isolating just the arithmetic the fix removes.
///
///     cargo test --release bench_hash_reuse -- --ignored --nocapture
#[cfg(test)]
mod bench_hash_reuse {
    use std::hint::black_box;
    use std::time::Instant;

    use super::{normalize_hash, sampled_body_hash, xxh3_64};

    const ITERS: usize = 500_000;

    #[test]
    #[ignore]
    fn bench_hash_reuse() {
        let key = b"q:f:modern+is:permanent+cmc>=6&page=2&order=cmc&dir=asc";
        let body = vec![0x42u8; 24 * 1024]; // representative cached search-response body size

        // Baseline: current-before-fix behavior — hash key + sample body TWICE per write
        // (once in fast_check, once in set).
        let start = Instant::now();
        let mut sink: u64 = 0;
        for _ in 0..ITERS {
            let h1 = normalize_hash(xxh3_64(black_box(key)));
            let v1 = sampled_body_hash(Some(black_box(body.as_slice())));
            let h2 = normalize_hash(xxh3_64(black_box(key)));
            let v2 = sampled_body_hash(Some(black_box(body.as_slice())));
            sink ^= h1 ^ v1 ^ h2 ^ v2;
        }
        let dup_ns = start.elapsed().as_nanos() as f64 / ITERS as f64;
        black_box(sink);

        // Fixed: hash key + sample body ONCE, reused via PendingWrite.
        let start = Instant::now();
        let mut sink2: u64 = 0;
        for _ in 0..ITERS {
            let h1 = normalize_hash(xxh3_64(black_box(key)));
            let v1 = sampled_body_hash(Some(black_box(body.as_slice())));
            sink2 ^= h1 ^ v1;
        }
        let once_ns = start.elapsed().as_nanos() as f64 / ITERS as f64;
        black_box(sink2);

        println!("dup  (fast_check + set, pre-fix): {dup_ns:.1} ns/call");
        println!("once (hoisted via PendingWrite):  {once_ns:.1} ns/call");
        println!(
            "delta: {:.1} ns/call ({:.0}% of the pre-fix cost)",
            dup_ns - once_ns,
            100.0 * (dup_ns - once_ns) / dup_ns
        );
    }
}
