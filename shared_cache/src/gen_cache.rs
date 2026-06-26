use std::sync::atomic::fence;
use std::sync::atomic::Ordering;
use std::time::{SystemTime, UNIX_EPOCH};

use memmap2::MmapMut;
use xxhash_rust::xxh3::xxh3_64;

use crate::cuckoo::CuckooFilter;
use crate::region::*;
use crate::types::{CachedResponse, RawSlot};

// ── Types ─────────────────────────────────────────────────────────────────────

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

pub struct GenerationalSharedCache {
    mmap: MmapMut,
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
            match slot.key_hash {
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
            std::sync::atomic::compiler_fence(Ordering::Release);
            (*slot).key_hash       = hash;
        }
        if is_new {
            self.page_header_mut(page_idx).entry_count += 1;
        }
        true
    }

    // ── Rotation ──────────────────────────────────────────────────────────

    fn scan_survivors(&self, page_idx: usize) -> Vec<SurvivorEntry> {
        let slot_count = self.slot_count_per_page;
        let mut survivors = Vec::new();
        let arena = self.arena_base(page_idx);
        let now = now_ns();
        for i in 0..slot_count {
            // Acquire load: pairs with the Release compiler_fence in do_insert (key_hash
            // written last) and with pop()'s plain write under the spinlock. Using an
            // atomic load here prevents a data race with pop() writing TOMBSTONE while
            // scan_survivors runs without the lock.
            let key_hash = read_key_hash(self.slot_ptr(page_idx, i as u32) as *const u8);
            if key_hash == EMPTY || key_hash == TOMBSTONE {
                continue;
            }
            let slot = unsafe { &*self.slot_ptr(page_idx, i as u32) };
            if slot.visited == 0 {
                continue;
            }
            if slot.expiry_ns != u64::MAX && slot.expiry_ns <= now {
                continue;
            }
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
            survivors.push(SurvivorEntry {
                hash: key_hash,
                expiry_ns: slot.expiry_ns,
                value_hash: slot.value_hash,
                body_len: slot.body_len,
                key_bytes,
                value_bytes,
            });
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

        // 3. Zero retiring page data (slot table + arena only; preserve header ptr).
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
        //    false positive.
        for s in survivors {
            if !self.do_insert(retiring_idx, s.hash, &s.key_bytes, &s.value_bytes, s.expiry_ns, s.value_hash, s.body_len) {
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
                && c.slot_count_per_page == slot_count_per_page as u32
                && c.arena_per_page == arena_per_page as u32;
            if !compatible {
                unsafe { std::ptr::write_bytes(mmap.as_mut_ptr(), 0, fsize); }
                write_init_headers(mmap, n_pages, maxsize, gen_maxsize, slot_count_per_page, arena_per_page, filter_bucket_count, prs, ps);
            }
        })?;

        Ok(GenerationalSharedCache {
            mmap,
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

    /// Call `f` with a direct slice into the mmap arena — zero extra allocation.
    /// The lock is released before `f` runs; the slice is valid because:
    ///   - Active page: in-place updates are bracketed by seqlock increments on value_seq;
    ///     if value_seq changes while `f` runs, the result is discarded as a cache miss.
    ///   - Sealed pages: immutable until rotation; generation check after `f` detects a concurrent swap.
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
        if !try_lock(&self.mmap) {
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
            let result = f(&self.mmap[abs..abs + len]);
            if self.page_generation(active_idx) != gen_before {
                return None;
            }
            if read_value_seq(self.slot_ptr(active_idx, slot_idx) as *const u8) != seq_before {
                return None;
            }
            return Some(result);
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
                let result = f(&self.mmap[abs..abs + len as usize]);
                // Discard if generation changed while f ran (rotation started or completed).
                let gen_after = self.page_generation(page_idx);
                if gen_after != gen_before {
                    return None;
                }
                return Some(result);
            }
        }

        None // filter false positive
    }

    pub fn get(&mut self, key: &[u8]) -> Option<Vec<u8>> {
        self.get_with(key, |bytes| bytes.to_vec())
    }

    /// Returns `true` if `key` is already cached with identical content — caller can skip `set()`.
    /// Checks filter (lock-free) → active page under lock → sealed pages lock-free.
    /// Call this from the binding layer before extracting expensive fields (headers, counts).
    pub fn fast_check(&mut self, key: &[u8], body: Option<&[u8]>) -> bool {
        let hash = normalize_hash(xxh3_64(key));
        let new_body_len = body.map_or(0, |b| b.len() as u32);
        let content_vh = sampled_body_hash(body);

        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) { return false; }

        if !try_lock(&self.mmap) { return false; }
        let active_idx = self.active_idx();
        let active_snap = self.do_probe(active_idx, hash, key).map(|(_, _, si)| {
            let s = unsafe { &*self.slot_ptr(active_idx, si) };
            (s.body_len, s.value_hash)
        });
        unlock(&self.mmap);

        if let Some((stored_len, stored_vh)) = active_snap {
            return stored_len == new_body_len && content_vh == stored_vh;
        }

        for i in 1..self.n_pages {
            let page_idx = (active_idx + self.n_pages - i) % self.n_pages;
            if let Some((_, _, si)) = self.do_probe(page_idx, hash, key) {
                let s = unsafe { &*self.slot_ptr(page_idx, si) };
                return s.body_len == new_body_len && content_vh == s.value_hash;
            }
        }
        false
    }

    pub fn set(
        &mut self,
        key: &[u8],
        status: &str,
        headers: Vec<(String, String)>,
        body: Option<&[u8]>,
        result_count: Option<i64>,
        total_cards: Option<i64>,
        ttl_secs: Option<f64>,
    ) {
        let hash = normalize_hash(xxh3_64(key));
        let new_body_len = body.map_or(0, |b| b.len() as u32);
        let content_vh = sampled_body_hash(body);

        let body_owned = body.map(|b| b.to_vec());
        let cr = CachedResponse { status: status.to_owned(), headers, body: body_owned, result_count, total_cards };
        let Ok(value_bytes) = rkyv::to_bytes::<rkyv::rancor::Error>(&cr) else { return; };
        let expiry = expiry_ns_for(ttl_secs, self.default_ttl_ns);

        // Step 1: check if rotation needed; snapshot retiring page ref.
        if !try_lock(&self.mmap) { return; }
        let active_idx = self.active_idx();
        let needs_rotation = self.page_header(active_idx).entry_count >= self.gen_maxsize as u32;
        let (retiring_idx, gen_snapshot) = if needs_rotation {
            let g = self.coord().counter;
            let ri = (active_idx + 1) % self.n_pages;
            (ri, g)
        } else {
            (0, 0)
        };
        unlock(&self.mmap);

        // Step 2: lock-free survivor scan (if needed).
        let survivors = if needs_rotation {
            self.scan_survivors(retiring_idx)
        } else {
            Vec::new()
        };

        // Step 3: commit rotation (if still valid) + insert.
        if !try_lock(&self.mmap) { return; }
        if needs_rotation && self.coord().counter == gen_snapshot {
            self.commit_rotation(survivors);
        }
        let active_idx = self.active_idx();
        if self.do_insert(active_idx, hash, key, &value_bytes, expiry, content_vh, new_body_len) {
            self.filter().insert(hash);
        }
        unlock(&self.mmap);
    }

    /// Remove all copies of `key` from every page and the shared filter.
    /// Returns true if at least one copy was found and tombstoned.
    ///
    /// Filter delete only happens if the key is found in the active page. If the key lives only
    /// in a sealed page the filter fingerprint is left in place — future gets probe and
    /// return None correctly; the fingerprint is cleared on the next rotation.
    pub fn pop(&mut self, key: &[u8]) -> bool {
        let hash = normalize_hash(xxh3_64(key));
        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) {
            return false;
        }

        if !try_lock(&self.mmap) { return false; }
        let active_idx = self.active_idx();
        let mut found = false;

        for page_idx in 0..self.n_pages {
            if let Some((_, _, slot_idx)) = self.do_probe(page_idx, hash, key) {
                unsafe { (*self.slot_ptr_mut(page_idx, slot_idx)).key_hash = TOMBSTONE; }
                if page_idx == active_idx {
                    self.page_header_mut(active_idx).entry_count =
                        self.page_header(active_idx).entry_count.saturating_sub(1);
                    self.filter().delete(hash);
                }
                found = true;
            }
        }

        unlock(&self.mmap);
        found
    }

    pub fn invalidate(&mut self) {
        if !try_lock(&self.mmap) { return; }
        // Zero filter.
        let fb = filter_bytes(self.filter_bucket_count);
        unsafe {
            std::ptr::write_bytes(self.mmap.as_mut_ptr().add(filter_offset()), 0, fb);
        }
        // Zero each page's data and reset headers.
        for i in 0..self.n_pages {
            // Odd bump: signals write-in-progress (seqlock protocol).
            bump_page_generation(unsafe { self.mmap.as_mut_ptr().add(self.page_offset(i)) });
            let data_start = self.page_offset(i) + PAGE_HEADER_SIZE;
            let data_len = self.slot_count_per_page * SLOT_SIZE
                + (self.page_size - self.arena_start_in_page);
            unsafe {
                std::ptr::write_bytes(self.mmap.as_mut_ptr().add(data_start), 0, data_len);
            }
            let ph = self.page_header_mut(i);
            ph.arena_head = 0;
            ph.entry_count = 0;
            ph.is_sealed = if i == 0 { 0 } else { 1 };
            // Even bump: page is stable (zeroed and reset); generation always lands on even.
            bump_page_generation(unsafe { self.mmap.as_mut_ptr().add(self.page_offset(i)) });
        }
        self.coord_mut().counter = 0;
        unlock(&self.mmap);
    }

    pub fn entry_count(&self) -> u32 {
        (0..self.n_pages).map(|i| self.page_header(i).entry_count).sum()
    }

    pub fn contains(&self, key: &[u8]) -> bool {
        let hash = normalize_hash(xxh3_64(key));
        fence(Ordering::Acquire);
        if !self.filter().lookup(hash) {
            return false;
        }
        // Full slot probe — no arena copy, no deserialization. Correctly returns false
        // for filter false positives and tombstoned entries (e.g. after pop()).
        if !try_lock(&self.mmap) { return false; }
        let active_idx = self.active_idx();
        let in_active = self.do_probe(active_idx, hash, key).is_some();
        unlock(&self.mmap);
        if in_active { return true; }
        // Sealed pages lock-free. A generation change during probe causes a key mismatch
        // at worst — no arena access means no UB risk.
        for i in 1..self.n_pages {
            let page_idx = (active_idx + self.n_pages - i) % self.n_pages;
            if self.do_probe(page_idx, hash, key).is_some() {
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
        if !try_lock(&self.mmap) {
            return false;
        }
        let active_idx = self.active_idx();
        let found = self.do_probe(active_idx, hash, key).is_some();
        unlock(&self.mmap);
        found
    }
}
