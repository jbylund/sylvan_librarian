use std::fs::OpenOptions;
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicU32, AtomicU8, Ordering, fence};
use std::time::{Duration, Instant};
use memmap2::MmapMut;

pub const MAGIC: u32 = 0x4743_4143 + 1; // bump to force re-init when slot layout changes
pub const VERSION: u32 = 1;
pub const SLOT_SIZE: usize = 64;
pub const PAGE_HEADER_SIZE: usize = 64;
pub const COORD_HEADER_SIZE: usize = 64;
pub const ARENA_ALIGN: usize = 16;
pub const EMPTY: u64 = 0;
pub const TOMBSTONE: u64 = u64::MAX;
const LOCK_TIMEOUT: Duration = Duration::from_millis(1);

/// First 64 bytes of the mmap — coordination header shared across all pages.
#[repr(C)]
pub struct CoordHeader {
    pub lock: u32,                  //  0: spinlock
    _pad0: u32,                     //  4
    pub magic: u32,                 //  8
    pub version: u32,               // 12
    pub n_pages: u32,               // 16
    pub maxsize: u32,               // 20
    pub gen_maxsize: u32,           // 24
    pub counter: u32,               // 28: active_idx = counter % n_pages
    pub slot_count_per_page: u32,   // 32
    pub arena_per_page: u32,        // 36
    pub filter_bucket_count: u32,   // 40
    _pad1: u32,                     // 44
    _pad2: u64,                     // 48
    _pad3: u64,                     // 56
}
const _: () = assert!(std::mem::size_of::<CoordHeader>() == 64);

/// 64-byte header at the start of each page region.
#[repr(C)]
pub struct PageHeader {
    pub arena_head: u32,    //  0: bump pointer (bytes from arena base within this page)
    pub entry_count: u32,   //  4
    pub is_sealed: u32,     //  8: 0=active, 1=sealed
    pub generation: u32,    // 12: bumped before reuse; readers detect stale data
    _pad: [u8; 48],         // 16..64
}
const _: () = assert!(std::mem::size_of::<PageHeader>() == 64);

// ── Layout arithmetic ─────────────────────────────────────────────────────────

pub fn filter_offset() -> usize { COORD_HEADER_SIZE }

pub fn filter_bytes(bucket_count: usize) -> usize {
    bucket_count * crate::cuckoo::BUCKET_BYTES
}

pub fn page_region_start(bucket_count: usize) -> usize {
    filter_offset() + filter_bytes(bucket_count)
}

pub fn page_size(slot_count_per_page: usize, arena_per_page: usize) -> usize {
    PAGE_HEADER_SIZE + slot_count_per_page * SLOT_SIZE + arena_per_page
}

pub fn arena_start_in_page(slot_count_per_page: usize) -> usize {
    PAGE_HEADER_SIZE + slot_count_per_page * SLOT_SIZE
}

pub fn total_file_size(
    bucket_count: usize,
    n_pages: usize,
    slot_count_per_page: usize,
    arena_per_page: usize,
) -> usize {
    page_region_start(bucket_count) + n_pages * page_size(slot_count_per_page, arena_per_page)
}

pub fn compute_slot_count(gen_maxsize: usize) -> usize {
    (gen_maxsize * 2).next_power_of_two()
}

pub fn compute_filter_bucket_count(maxsize: usize) -> usize {
    ((maxsize * 4).next_power_of_two() / 4).max(16)
}

// ── Atomic helpers ─────────────────────────────────────────────────────────────

/// Atomic read of the page's generation counter (Acquire).
pub fn read_page_generation(base: *const u8) -> u32 {
    // generation is at offset 12 within PageHeader
    let ptr = unsafe { base.add(12) as *const AtomicU32 };
    fence(Ordering::Acquire);
    unsafe { (*ptr).load(Ordering::Relaxed) }
}

/// Increment the page generation counter (Release).
pub fn bump_page_generation(base: *mut u8) {
    let ptr = unsafe { base.add(12) as *const AtomicU32 };
    unsafe { (*ptr).fetch_add(1, Ordering::Release) };
}

/// Set visited=1 on a slot (offset 40 within RawSlot). Relaxed — advisory only.
pub fn set_visited(slot: *const u8) {
    let ptr = unsafe { slot.add(40) as *const AtomicU8 };
    unsafe { (*ptr).store(1, Ordering::Relaxed) };
}

/// Set visited=0 on a slot. Used when sealing a page for fresh trial.
pub fn clear_visited(slot: *const u8) {
    let ptr = unsafe { slot.add(40) as *const AtomicU8 };
    unsafe { (*ptr).store(0, Ordering::Relaxed) };
}

// ── Spinlock ──────────────────────────────────────────────────────────────────

pub fn try_lock(mmap: &MmapMut) -> bool {
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

pub fn normalize_hash(h: u64) -> u64 {
    match h { 0 => 1, u64::MAX => u64::MAX - 1, h => h }
}

// ── File open ─────────────────────────────────────────────────────────────────

pub fn open_mmap(path: &str, file_size: usize) -> std::io::Result<MmapMut> {
    let file = OpenOptions::new().read(true).write(true).create(true).open(path)?;
    let fd = file.as_raw_fd();
    unsafe { libc::flock(fd, libc::LOCK_EX) };
    if file.metadata()?.len() < file_size as u64 {
        file.set_len(file_size as u64)?;
    }
    let mmap = unsafe { MmapMut::map_mut(&file)? };
    unsafe { libc::flock(fd, libc::LOCK_UN) };
    Ok(mmap)
}
