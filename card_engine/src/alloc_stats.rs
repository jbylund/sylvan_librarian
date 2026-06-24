use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

pub static LIVE: AtomicUsize = AtomicUsize::new(0);
pub static PEAK: AtomicUsize = AtomicUsize::new(0);
pub static ALLOCS: AtomicUsize = AtomicUsize::new(0); // currently-live allocation count

// Snapshots recorded by the most recent reload()
pub static RELOAD_LIVE_BEFORE: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_LIVE_AFTER_CARDS: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_ALLOCS_AFTER_CARDS: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_LIVE_AFTER_INDEXES: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_ALLOCS_AFTER_INDEXES: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_CARDS_RKYV: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_INDEXES_RKYV: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_STRINGS_RKYV: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_ARCHIVE: AtomicUsize = AtomicUsize::new(0);
pub static RELOAD_PEAK: AtomicUsize = AtomicUsize::new(0);

pub fn live() -> usize { LIVE.load(Ordering::Relaxed) }
pub fn allocs() -> usize { ALLOCS.load(Ordering::Relaxed) }
pub fn peak() -> usize { PEAK.load(Ordering::Relaxed) }

pub fn reset_peak() {
    RELOAD_LIVE_BEFORE.store(live(), Ordering::Relaxed);
    PEAK.store(live(), Ordering::Relaxed);
}

pub fn record_reload(
    after_cards: (usize, usize),
    after_indexes: (usize, usize),
    component_bytes: (usize, usize, usize),
    archive: usize,
    peak: usize, // caller snapshots before diagnostics inflate the high-water mark
) {
    RELOAD_LIVE_AFTER_CARDS.store(after_cards.0, Ordering::Relaxed);
    RELOAD_ALLOCS_AFTER_CARDS.store(after_cards.1, Ordering::Relaxed);
    RELOAD_LIVE_AFTER_INDEXES.store(after_indexes.0, Ordering::Relaxed);
    RELOAD_ALLOCS_AFTER_INDEXES.store(after_indexes.1, Ordering::Relaxed);
    RELOAD_CARDS_RKYV.store(component_bytes.0, Ordering::Relaxed);
    RELOAD_INDEXES_RKYV.store(component_bytes.1, Ordering::Relaxed);
    RELOAD_STRINGS_RKYV.store(component_bytes.2, Ordering::Relaxed);
    RELOAD_ARCHIVE.store(archive, Ordering::Relaxed);
    RELOAD_PEAK.store(peak, Ordering::Relaxed);
}

pub struct CountingAlloc;

impl CountingAlloc {
    fn on_alloc(size: usize) {
        let live = LIVE.fetch_add(size, Ordering::Relaxed) + size;
        ALLOCS.fetch_add(1, Ordering::Relaxed);
        PEAK.fetch_max(live, Ordering::Relaxed);
    }
    fn on_dealloc(size: usize) {
        LIVE.fetch_sub(size, Ordering::Relaxed);
        ALLOCS.fetch_sub(1, Ordering::Relaxed);
    }
}

unsafe impl GlobalAlloc for CountingAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let p = unsafe { System.alloc(layout) };
        if !p.is_null() { Self::on_alloc(layout.size()); }
        p
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) };
        Self::on_dealloc(layout.size());
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let p = unsafe { System.realloc(ptr, layout, new_size) };
        if !p.is_null() {
            LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
            let live = LIVE.fetch_add(new_size, Ordering::Relaxed) + new_size;
            PEAK.fetch_max(live, Ordering::Relaxed);
        }
        p
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let p = unsafe { System.alloc_zeroed(layout) };
        if !p.is_null() { Self::on_alloc(layout.size()); }
        p
    }
}

#[global_allocator]
static COUNTING_ALLOC: CountingAlloc = CountingAlloc;
