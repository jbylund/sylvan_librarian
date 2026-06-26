use std::sync::atomic::{AtomicU16, Ordering};

/// Cuckoo filter embedded in the mmap arena.
///
/// Layout: `bucket_count` contiguous buckets, each `BUCKET_BYTES` wide
/// (4 × u16 fingerprints, 0 = empty). `bucket_count` must be a power of two.
///
/// 16-bit fingerprints with bucket_count = slot_count/4 gives ~50% load at
/// capacity and FPR ≈ 0.006% (1 in 16,384).
///
/// All methods take `&self` and write through the raw `*mut u8` pointer.
/// Callers must hold the spinlock before calling any mutating method
/// (insert / delete).
///
/// Slot accesses use AtomicU16 (Relaxed) so that lock-free `lookup()` callers
/// don't race with spinlock-held `insert()`/`delete()` writes. The ordering
/// contract is provided by the Acquire fence in callers (lock-free) and the
/// spinlock's own Acquire/Release (writers).
pub const SLOTS: usize = 4;          // fingerprints per bucket
pub const FP_SIZE: usize = 2;        // bytes per fingerprint (u16)
pub const BUCKET_BYTES: usize = SLOTS * FP_SIZE;

const MAX_KICKS: usize = 500;

pub struct CuckooFilter {
    data: *mut u8,
    bucket_count: u32,
}

// Safety: the pointer is into an mmap region; callers ensure exclusive write
// access via the spinlock.
unsafe impl Send for CuckooFilter {}
unsafe impl Sync for CuckooFilter {}

impl CuckooFilter {
    pub fn new(data: *mut u8, bucket_count: u32) -> Self {
        Self { data, bucket_count }
    }

    /// Returns true if the key with this hash is probably in the cache.
    /// ~0.006% FPR (1 in 16,384) at 50% load. No false negatives unless delete()
    /// has been called and two live keys share the same 16-bit fingerprint and
    /// overlapping bucket pairs — in that case the affected key self-heals on the
    /// next set(). Safe to call without the lock (lock-free read path).
    pub fn lookup(&self, hash: u64) -> bool {
        let fp = fingerprint(hash);
        let b1 = self.idx(hash);
        let b2 = self.alt(b1, fp);
        self.bucket_has(b1, fp) || self.bucket_has(b2, fp)
    }

    /// Insert the key hash. Silently drops on filter overfull (negligible at
    /// 50% load; results in false misses for that key, not incorrect data).
    /// Must be called under the spinlock.
    pub fn insert(&self, hash: u64) {
        let mut fp = fingerprint(hash);
        let mut b = self.idx(hash);

        // Try primary then alternate bucket.
        for _ in 0..2 {
            if let Some(s) = self.empty_slot(b) {
                unsafe { (*self.slot_ptr(b, s)).store(fp, Ordering::Relaxed) };
                return;
            }
            b = self.alt(b, fp);
        }

        // Both buckets full — cuckoo kick. Deterministic slot cycling avoids
        // PRNG overhead on the hot path; MAX_KICKS bounds the loop.
        b = self.idx(hash);
        for k in 0..MAX_KICKS {
            let s = k % SLOTS;
            let p = self.slot_ptr(b, s);
            let kicked = unsafe { (*p).load(Ordering::Relaxed) };
            unsafe { (*p).store(fp, Ordering::Relaxed) };
            fp = kicked;
            b = self.alt(b, fp);
            if let Some(s) = self.empty_slot(b) {
                unsafe { (*self.slot_ptr(b, s)).store(fp, Ordering::Relaxed) };
                return;
            }
        }
        // Filter overfull: silently drop. Key gets false misses until the
        // next generation_reset rebuilds the filter.
    }

    /// Remove a fingerprint for this hash. Must be called under the spinlock.
    pub fn delete(&self, hash: u64) {
        let fp = fingerprint(hash);
        let b1 = self.idx(hash);
        let b2 = self.alt(b1, fp);
        for &b in &[b1, b2] {
            for s in 0..SLOTS {
                let p = self.slot_ptr(b, s);
                if unsafe { (*p).load(Ordering::Relaxed) } == fp {
                    unsafe { (*p).store(0, Ordering::Relaxed) };
                    return;
                }
            }
        }
    }

    // ── Private helpers ───────────────────────────────────────────────────────

    fn idx(&self, hash: u64) -> u32 {
        (hash as u32) & (self.bucket_count - 1)
    }

    /// Alternate bucket index: idx XOR hash(fingerprint), stays in range.
    /// Property: alt(alt(b, fp), fp) == b, so relocations are reversible.
    fn alt(&self, idx: u32, fp: u16) -> u32 {
        let h = (fp as u32).wrapping_mul(0x5bd1e995).wrapping_add(0xe6546b64);
        (idx ^ h) & (self.bucket_count - 1)
    }

    /// Pointer to slot `slot` within `bucket`. All offsets are multiples of 2
    /// (filter base is 64-byte aligned; each bucket is 8 bytes), so the AtomicU16
    /// dereference is always correctly aligned.
    fn slot_ptr(&self, bucket: u32, slot: usize) -> *const AtomicU16 {
        unsafe {
            self.data.add(bucket as usize * BUCKET_BYTES + slot * FP_SIZE) as *const AtomicU16
        }
    }

    fn bucket_has(&self, bucket: u32, fp: u16) -> bool {
        (0..SLOTS).any(|s| unsafe { (*self.slot_ptr(bucket, s)).load(Ordering::Relaxed) == fp })
    }

    fn empty_slot(&self, bucket: u32) -> Option<usize> {
        (0..SLOTS).find(|&s| unsafe { (*self.slot_ptr(bucket, s)).load(Ordering::Relaxed) == 0 })
    }
}

/// 16-bit fingerprint derived from bits 32-47 of the hash.
/// 0 is reserved as the "empty" sentinel; map it to 1.
fn fingerprint(hash: u64) -> u16 {
    let f = (hash >> 32) as u16;
    if f == 0 { 1 } else { f }
}
