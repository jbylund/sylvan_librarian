use std::fs::OpenOptions;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicU32, AtomicU64, AtomicU8, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use memmap2::MmapMut;

pub const MAGIC: u32 = 0x4743_4143 + 3; // bump to force re-init when slot layout changes
pub const VERSION: u32 = 1;
pub const SLOT_SIZE: usize = 64;
pub const PAGE_HEADER_SIZE: usize = 64;
pub const COORD_HEADER_SIZE: usize = 64;
pub const ARENA_ALIGN: usize = 16;
pub const EMPTY: u64 = 0;
pub const TOMBSTONE: u64 = u64::MAX;
const GENERATION_OFFSET: usize = 12; // byte offset of `generation` within PageHeader
const VISITED_OFFSET: usize = 48;   // byte offset of `visited` within RawSlot
const VALUE_SEQ_OFFSET: usize = 52; // byte offset of `value_seq` within RawSlot
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
    let ptr = unsafe { base.add(GENERATION_OFFSET) as *const AtomicU32 };
    unsafe { (*ptr).load(Ordering::Acquire) }
}

/// Increment the page generation counter (AcqRel).
pub fn bump_page_generation(base: *mut u8) {
    let ptr = unsafe { base.add(GENERATION_OFFSET) as *const AtomicU32 };
    unsafe { (*ptr).fetch_add(1, Ordering::AcqRel) };
}

/// Store a page's generation outright (Release). Used when re-initialising after a lock steal:
/// the parity a dead writer left behind is unknown, and a bump from odd would land on even while
/// the page is still being zeroed.
pub fn set_page_generation(base: *mut u8, value: u32) {
    let ptr = unsafe { base.add(GENERATION_OFFSET) as *const AtomicU32 };
    unsafe { (*ptr).store(value, Ordering::Release) };
}

/// Atomic Relaxed read of a slot's visited field. Pairs with set_visited()'s Relaxed store.
pub fn read_visited(slot: *const u8) -> u8 {
    let ptr = unsafe { slot.add(VISITED_OFFSET) as *const AtomicU8 };
    unsafe { (*ptr).load(Ordering::Relaxed) }
}

/// Set visited=1 on a slot. Relaxed — advisory only.
pub fn set_visited(slot: *const u8) {
    let ptr = unsafe { slot.add(VISITED_OFFSET) as *const AtomicU8 };
    unsafe { (*ptr).store(1, Ordering::Relaxed) };
}

/// Set visited=0 on a slot. Used when sealing a page for fresh trial.
pub fn clear_visited(slot: *const u8) {
    let ptr = unsafe { slot.add(VISITED_OFFSET) as *const AtomicU8 };
    unsafe { (*ptr).store(0, Ordering::Relaxed) };
}

/// Increment value_seq by 1. Called twice per in-place arena update: once before writing
/// (leaving seq odd = in progress) and once after (leaving seq even = stable).
pub fn inc_value_seq(slot: *mut u8) {
    let ptr = unsafe { slot.add(VALUE_SEQ_OFFSET) as *const AtomicU32 };
    unsafe { (*ptr).fetch_add(1, Ordering::AcqRel) };
}

/// Atomic Acquire read of a slot's key_hash field (offset 0 in RawSlot).
/// Used in lock-free paths that race with pop() writing TOMBSTONE under the spinlock.
pub fn read_key_hash(slot: *const u8) -> u64 {
    let ptr = slot as *const AtomicU64;
    unsafe { (*ptr).load(Ordering::Acquire) }
}

/// Atomic Release write of a slot's key_hash field (offset 0 in RawSlot).
/// Pairs with read_key_hash()'s Acquire load in lock-free read paths.
pub fn write_key_hash(slot: *mut u8, hash: u64) {
    let ptr = slot as *mut AtomicU64;
    unsafe { (*ptr).store(hash, Ordering::Release) };
}

/// Atomic read of value_seq for the post-f seqlock check in get_with.
pub fn read_value_seq(slot: *const u8) -> u32 {
    let ptr = unsafe { slot.add(VALUE_SEQ_OFFSET) as *const AtomicU32 };
    unsafe { (*ptr).load(Ordering::Acquire) }
}

// ── Spinlock ──────────────────────────────────────────────────────────────────

/// This process's pid, read via a syscall once and cached: it cannot change for the life of
/// the process, and unlike Linux glibc (which caches `getpid()` itself post-2.25), macOS libc
/// makes every `std::process::id()` call a real trap into the kernel.
fn current_pid() -> u32 {
    static PID: OnceLock<u32> = OnceLock::new();
    *PID.get_or_init(std::process::id) // u32; PID 0 is unused on POSIX, safe as unlocked sentinel
}

/// Outcome of a lock attempt. `Stolen` means the previous holder was a process that no longer
/// exists: the caller now owns the lock, but whatever that process was doing under it — a
/// rotation half-way through zeroing a page, a generation left odd, an insert with the arena head
/// already moved — was cut short, and the shared state must be re-initialised before it is trusted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LockOutcome {
    Acquired,
    Stolen,
    Busy,
}

pub fn try_lock_outcome(mmap: &MmapMut) -> LockOutcome {
    let lock = unsafe { &*(mmap.as_ptr() as *const AtomicU32) };
    let my_pid = current_pid();

    // Uncontended fast path: no clock read. The overwhelming majority of calls land here, and
    // `Instant::now()` is only needed to police a timeout on the contended path below.
    if lock.compare_exchange(0, my_pid, Ordering::Acquire, Ordering::Relaxed).is_ok() {
        return LockOutcome::Acquired;
    }

    let deadline = Instant::now() + LOCK_TIMEOUT;
    loop {
        if lock.compare_exchange(0, my_pid, Ordering::Acquire, Ordering::Relaxed).is_ok() {
            return LockOutcome::Acquired;
        }
        if Instant::now() >= deadline {
            // Timed out. Check whether the owner process is still alive before giving up.
            // This prevents a crashed worker from permanently wedging the cache.
            let owner = lock.load(Ordering::Relaxed);
            if owner != 0 && owner != my_pid {
                let dead = unsafe { libc::kill(owner as libc::pid_t, 0) } == -1
                    && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH);
                if dead {
                    // Owner is gone; steal the lock. If the CAS fails another worker got
                    // here first — report busy and let the caller retry next time.
                    if lock.compare_exchange(owner, my_pid, Ordering::Acquire, Ordering::Relaxed).is_ok() {
                        return LockOutcome::Stolen;
                    }
                }
            }
            return LockOutcome::Busy;
        }
        std::hint::spin_loop();
    }
}

/// Bench-only wrapper: production callers go through `GenerationalSharedCache::lock`, which
/// re-initialises the cache when the lock had to be stolen.
#[cfg(test)]
pub fn try_lock(mmap: &MmapMut) -> bool {
    try_lock_outcome(mmap) != LockOutcome::Busy
}

pub fn unlock(mmap: &MmapMut) {
    let lock = unsafe { &*(mmap.as_ptr() as *const AtomicU32) };
    lock.store(0, Ordering::Release);
}

pub fn normalize_hash(h: u64) -> u64 {
    match h { 0 => 1, u64::MAX => u64::MAX - 1, h => h }
}

// ── File open ─────────────────────────────────────────────────────────────────

/// Open (or create) the mmap file, extend it if needed, and call `under_lock` while the
/// exclusive flock is still held. This lets the caller do a compatibility check + reinit
/// atomically so multiple workers starting simultaneously don't race each other to zero
/// and re-initialize the file.
pub fn open_mmap(
    path: &str,
    file_size: usize,
    under_lock: impl FnOnce(&mut MmapMut),
) -> std::io::Result<MmapMut> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let fd = file.as_raw_fd();
    // Restrict permissions unconditionally — .mode() only applies at creation.
    if unsafe { libc::fchmod(fd, 0o600) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    // Retry on EINTR; any other error means we cannot safely serialize workers.
    loop {
        let ret = unsafe { libc::flock(fd, libc::LOCK_EX) };
        if ret == 0 { break; }
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() != Some(libc::EINTR) {
            return Err(err);
        }
    }
    if file.metadata()?.len() < file_size as u64 {
        file.set_len(file_size as u64)?;
    }
    let mut mmap = unsafe { MmapMut::map_mut(&file)? };
    under_lock(&mut mmap);
    unsafe { libc::flock(fd, libc::LOCK_UN) };
    Ok(mmap)
}

#[cfg(test)]
mod steal_tests {
    use std::sync::atomic::{AtomicU32, Ordering};

    use memmap2::MmapMut;

    use super::{LockOutcome, try_lock_outcome, unlock};

    /// Beyond every platform's pid range (Linux pid_max tops out at 4194304, macOS at 99998), so
    /// kill(pid, 0) reliably answers ESRCH.
    const DEAD_PID: u32 = i32::MAX as u32;

    #[test]
    fn a_dead_owners_lock_is_stolen_and_reported() {
        let mmap = MmapMut::map_anon(4096).expect("anon mmap");
        let word = unsafe { &*(mmap.as_ptr() as *const AtomicU32) };
        word.store(DEAD_PID, Ordering::Relaxed);

        assert_eq!(try_lock_outcome(&mmap), LockOutcome::Stolen);
        assert_eq!(word.load(Ordering::Relaxed), std::process::id());
        unlock(&mmap);
        assert_eq!(try_lock_outcome(&mmap), LockOutcome::Acquired);
    }

    #[test]
    fn a_live_owners_lock_is_not_stolen() {
        // pid 1 always exists; kill(1, 0) answers 0 or EPERM, never ESRCH.
        let mmap = MmapMut::map_anon(4096).expect("anon mmap");
        let word = unsafe { &*(mmap.as_ptr() as *const AtomicU32) };
        word.store(1, Ordering::Relaxed);

        assert_eq!(try_lock_outcome(&mmap), LockOutcome::Busy);
        assert_eq!(word.load(Ordering::Relaxed), 1);
    }
}

/// Perf-audit finding #6: `try_lock` computed `std::process::id()` and `Instant::now()`
/// unconditionally, even on the uncontended fast path where neither is needed until the first
/// CAS attempt fails. Times the uncontended case (the common one -- no other thread ever holds
/// the lock here) before vs after deferring both.
///
///     cargo test --release bench_try_lock_fast_path -- --ignored --nocapture
#[cfg(test)]
mod bench_try_lock_fast_path {
    use std::hint::black_box;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::time::Instant;

    use super::{try_lock, unlock};
    use memmap2::MmapMut;

    const ITERS: usize = 500_000;

    /// Pre-fix try_lock: recomputes pid + deadline on every call, even when uncontended.
    #[inline(never)]
    fn try_lock_prefix(mmap: &MmapMut) -> bool {
        let lock = unsafe { &*(mmap.as_ptr() as *const AtomicU32) };
        let my_pid = black_box(std::process::id());
        let _deadline = black_box(Instant::now() + std::time::Duration::from_millis(1));
        lock.compare_exchange(0, my_pid, Ordering::Acquire, Ordering::Relaxed).is_ok()
    }

    #[test]
    #[ignore]
    fn bench_try_lock_fast_path() {
        let mmap = MmapMut::map_anon(4096).expect("anon mmap");

        let start = Instant::now();
        for _ in 0..ITERS {
            assert!(black_box(try_lock_prefix(black_box(&mmap))));
            unlock(&mmap);
        }
        let prefix_ns = start.elapsed().as_nanos() as f64 / ITERS as f64;

        let start = Instant::now();
        for _ in 0..ITERS {
            assert!(black_box(try_lock(black_box(&mmap))));
            unlock(&mmap);
        }
        let fixed_ns = start.elapsed().as_nanos() as f64 / ITERS as f64;

        println!("pre-fix (pid + clock every call): {prefix_ns:.1} ns/call");
        println!("fixed   (cached pid, no clock):   {fixed_ns:.1} ns/call");
        println!(
            "delta: {:.1} ns/call ({:.0}% reduction)",
            prefix_ns - fixed_ns,
            100.0 * (prefix_ns - fixed_ns) / prefix_ns
        );
    }
}
