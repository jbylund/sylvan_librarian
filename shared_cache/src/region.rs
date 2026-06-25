use std::fs::OpenOptions;
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicU32, Ordering, fence};
use std::time::{Duration, Instant};

use memmap2::MmapMut;

pub const MAGIC: u32 = 0x5343_4143; // "SCAC"
pub const VERSION: u32 = 3;
pub const HEADER_SIZE: usize = 64;
pub const SLOT_SIZE: usize = 64;

/// 0 in key_hash field: slot is empty.
pub const EMPTY: u64 = 0;
/// u64::MAX in key_hash field: slot is a tombstone (evicted, probe chains intact).
pub const TOMBSTONE: u64 = u64::MAX;

const LOCK_TIMEOUT: Duration = Duration::from_millis(1);

/// First 64 bytes of the mmap. Must be exactly 64 bytes — asserted below.
#[repr(C)]
pub struct RegionHeader {
    /// Spinlock: 0 = free, 1 = held. Cast to AtomicU32 for CAS operations.
    pub lock: u32,
    _pad0: u32,
    pub magic: u32,
    pub version: u32,
    pub slot_count: u32,
    pub arena_size: u32,
    pub arena_head: u32,   // bump pointer (bytes from arena base)
    pub entry_count: u32,  // live entries in current generation
    pub tombstone_count: u32,
    pub maxsize: u32,      // evict when entry_count reaches this
    pub generation: u32,   // incremented on every generation_reset(); readers use this to detect concurrent resets
    _pad1: u32,
    pub seq: u64,          // monotonic counter; written to last_used on every access
    _pad2: u64,
}

/// One 64-byte slot in the open-addressing hash table.
#[repr(C)]
pub struct RawSlot {
    pub key_hash: u64,     // EMPTY / TOMBSTONE / normalized xxh3 hash
    pub expiry_ns: u64,    // Unix epoch nanoseconds; u64::MAX = never expires
    pub last_used: u64,    // seq value at last access (for sampled LRU eviction)
    pub arena_offset: u32, // byte offset of rkyv value bytes within the arena
    pub arena_len: u32,    // length of rkyv value bytes
    pub key_offset: u32,   // byte offset of raw key bytes within the arena
    pub key_len: u32,      // length of raw key bytes
    _pad: [u8; 24],
}

const _: () = assert!(std::mem::size_of::<RegionHeader>() == HEADER_SIZE);
const _: () = assert!(std::mem::size_of::<RawSlot>() == SLOT_SIZE);

/// Normalize a hash so it never collides with the EMPTY or TOMBSTONE sentinels.
pub fn normalize_hash(h: u64) -> u64 {
    match h {
        0 => 1,
        u64::MAX => u64::MAX - 1,
        h => h,
    }
}

/// Number of buckets in the cuckoo filter (must be a power of two).
/// slot_count/4 buckets × 4 slots/bucket = slot_count total slots = 2×maxsize,
/// keeping load at ≤50% at capacity. FPR ≈ 0.006% (1 in 16,384) with 16-bit fps.
pub fn bucket_count(slot_count: u32) -> u32 {
    (slot_count / 4).max(16)
}

/// Byte offset of the cuckoo filter region in the mmap.
pub fn filter_offset(slot_count: u32) -> usize {
    HEADER_SIZE + slot_count as usize * SLOT_SIZE
}

/// Byte size of the cuckoo filter region.
pub fn filter_size(slot_count: u32) -> usize {
    bucket_count(slot_count) as usize * crate::cuckoo::BUCKET_BYTES
}

/// Byte offset where the arena begins (after header + slot table + cuckoo filter).
pub fn arena_start(slot_count: u32) -> usize {
    filter_offset(slot_count) + filter_size(slot_count)
}

pub fn file_size(slot_count: u32, arena_size: u32) -> usize {
    arena_start(slot_count) + arena_size as usize
}

/// Open (or create + initialize) the shared memory file.
/// Uses an exclusive flock during initialization so concurrent openers don't race.
pub fn open_region(path: &str, slot_count: u32, arena_size: u32) -> std::io::Result<MmapMut> {
    let file = OpenOptions::new().read(true).write(true).create(true).open(path)?;
    let fd = file.as_raw_fd();

    unsafe { libc::flock(fd, libc::LOCK_EX) };

    let size = file_size(slot_count, arena_size);
    if file.metadata()?.len() < size as u64 {
        file.set_len(size as u64)?;
    }

    let mut mmap = unsafe { MmapMut::map_mut(&file)? };

    let existing_magic = unsafe { (*hdr_ptr(&mmap)).magic };
    let compatible = existing_magic == MAGIC && unsafe {
        let h = &*hdr_ptr(&mmap);
        h.version == VERSION && h.slot_count == slot_count && h.arena_size == arena_size
    };

    if !compatible {
        // Fresh init (or incompatible existing file — wipe and restart).
        unsafe {
            std::ptr::write_bytes(mmap.as_mut_ptr(), 0, size);
            let h = &mut *hdr_ptr_mut(&mut mmap);
            h.magic = MAGIC;
            h.version = VERSION;
            h.slot_count = slot_count;
            h.arena_size = arena_size;
            h.maxsize = slot_count / 2;
        }
    }

    unsafe { libc::flock(fd, libc::LOCK_UN) };

    Ok(mmap)
}

// ── Raw pointer accessors ────────────────────────────────────────────────────

pub fn hdr_ptr(mmap: &MmapMut) -> *const RegionHeader {
    mmap.as_ptr() as *const RegionHeader
}

pub fn hdr_ptr_mut(mmap: &mut MmapMut) -> *mut RegionHeader {
    mmap.as_mut_ptr() as *mut RegionHeader
}

pub fn slot_ptr(mmap: &MmapMut, idx: u32) -> *const RawSlot {
    let offset = HEADER_SIZE + idx as usize * SLOT_SIZE;
    unsafe { mmap.as_ptr().add(offset) as *const RawSlot }
}

pub fn slot_ptr_mut(mmap: &mut MmapMut, idx: u32) -> *mut RawSlot {
    let offset = HEADER_SIZE + idx as usize * SLOT_SIZE;
    unsafe { mmap.as_mut_ptr().add(offset) as *mut RawSlot }
}

// ── Generation counter ───────────────────────────────────────────────────────

/// Atomically read the generation counter.  Use Acquire ordering so that all
/// arena reads that precede this call in program order are not reordered after it.
pub fn read_generation(mmap: &MmapMut) -> u32 {
    let ptr = unsafe {
        let hdr = mmap.as_ptr() as *const RegionHeader;
        std::ptr::addr_of!((*hdr).generation) as *const AtomicU32
    };
    // Acquire fence before the load ensures prior mmap reads happen-before this.
    fence(Ordering::Acquire);
    unsafe { (*ptr).load(Ordering::Relaxed) }
}

/// Increment the generation counter (called under the spinlock inside generation_reset).
pub fn bump_generation(mmap: &MmapMut) {
    let ptr = unsafe {
        let hdr = mmap.as_ptr() as *const RegionHeader;
        std::ptr::addr_of!((*hdr).generation) as *const AtomicU32
    };
    unsafe { (*ptr).fetch_add(1, Ordering::Release) };
}

// ── Spinlock ─────────────────────────────────────────────────────────────────

/// Try to acquire the spinlock. Returns false on timeout (1 ms), preventing
/// a hung worker from permanently blocking the cache.
pub fn try_lock(mmap: &MmapMut) -> bool {
    // Safety: lock is the first u32 of RegionHeader at offset 0 of the mmap,
    // which is page-aligned. AtomicU32 has the same layout as u32.
    let lock = unsafe { &*(mmap.as_ptr() as *const AtomicU32) };
    let deadline = Instant::now() + LOCK_TIMEOUT;
    loop {
        if lock.compare_exchange(0, 1, Ordering::Acquire, Ordering::Relaxed).is_ok() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::hint::spin_loop();
    }
}

pub fn unlock(mmap: &MmapMut) {
    let lock = unsafe { &*(mmap.as_ptr() as *const AtomicU32) };
    lock.store(0, Ordering::Release);
}
