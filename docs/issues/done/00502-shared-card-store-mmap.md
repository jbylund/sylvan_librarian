# Cross-process shared card store via mmap

## Status: done — shipped as PR [#502](https://github.com/jbylund/sylvan_librarian/pull/502)

Implemented with the rkyv + mmap layout below, with one design change: publish is a flat
archive file written to a per-PID `.tmp` and atomically renamed into place (workers remap on
an inode check), rather than the double-buffer generation-counter scheme sketched in
[Atomic swap on reload](#atomic-swap-on-reload). Follow-on work:
[00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md),
[00505-engine-incremental-loading.md](00505-engine-incremental-loading.md), and the still-open
[local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md).

## Problem

Each Bjoern worker process holds its own independent `QueryEngine` instance. At ~95k cards the
card data plus indexes is roughly 150–200 MB per worker. On a host with 4–5 workers that is
~800 MB–1 GB of RSS just for card data that is logically identical across all workers.

The `Arc<RwLock<CardData>>` inside `QueryEngine` is a userspace pointer — it lives at a
process-local virtual address and cannot be shared across `fork()` boundaries after the first
write. The [pre-fork COW approach](./local-in-process-card-filter.md) gets you shared physical pages for
free on Linux as long as reads don't dirty pages, but it breaks down on reload (the reloading
worker allocates new heap memory that other workers never see).

## Approach: flat buffer in shared memory

`Vec`, `String`, and `HashMap` contain heap pointers that are process-local virtual addresses and
cannot be placed in a shared memory region as-is. The solution is to lay out the entire card store
as a flat, pointer-free byte buffer backed by `mmap`, then map the same region into every worker
process. Workers can all read concurrently with no copies and no IPC overhead — a query is a scan
over memory that happens to be shared.

### Data layout

The [`rkyv`](https://rkyv.org) crate is the natural fit here. It serializes Rust structs into an
archive format where all internal references are relative byte offsets rather than absolute
pointers. An archived `Vec<Card>` is a contiguous block of archived `Card` structs; an archived
`String` is a length-prefixed byte slice at a known offset. The result is a single contiguous byte
buffer that can be written to a file or shared memory region, `mmap`'d in any process, and
accessed through zero-copy `Archived<Card>` views — no deserialization step.

The indexes (`HashMap<[u8;3], Vec<u32>>` for trigrams, `Vec<(i16, u32)>` for B-trees, etc.) are
serialized into the same buffer after the card array. A small fixed-size header at the start of the
region records byte offsets to each index.

### Creating and mapping the region

On Linux, `shm_open` + `ftruncate` + `mmap` creates an anonymous shared memory object accessible
by name from any process. The [`memmap2`](https://docs.rs/memmap2) crate wraps this with a safe
`MmapMut` / `Mmap` API.

```rust
// writer (reload path) — one worker or a dedicated reloader
let shm = File::options().read(true).write(true)
    .open("/dev/shm/sylvan_librarian_cards")?;
let mut mmap = unsafe { MmapMut::map_mut(&shm)? };
rkyv::to_bytes::<_, 4096>(&card_data)?.copy_to_slice(&mut mmap);
mmap.flush()?;

// reader (every worker at startup and after reload signal)
let shm = File::open("/dev/shm/sylvan_librarian_cards")?;
let mmap = unsafe { Mmap::map(&shm)? };
let archived = unsafe { rkyv::archived_root::<CardData>(&mmap) };
```

The `archived` reference is a zero-copy view into shared memory. Queries operate on
`Archived<Card>` structs directly — no allocation, no copying.

## Atomic swap on reload

Writing new card data in-place would race with in-flight queries reading the same region. The
standard solution is **double-buffering with a generation counter** — essentially userspace RCU
(Read-Copy-Update).

Two fixed-size shared memory regions exist simultaneously: the active region and the standby
region. A third tiny region holds a single atomic `u64` generation counter plus two reader counts.

**Reload sequence:**

1. Writer serializes new card data into the standby region (the one workers are not reading).
2. Writer atomically increments the generation counter. Workers now see an odd value — the
   transition marker — and any new query waits until it becomes even again.
3. Writer sets the new active index (which region is "current"), then increments the counter again
   to make it even. New queries now use the new region.
4. Writer waits for the reader count on the old generation to reach zero (all in-flight queries
   against the old region have completed). Old region becomes the new standby.

**Worker query sequence:**

1. Atomically read generation counter and increment the reader count for the current region.
2. Execute the query against the active region.
3. Decrement the reader count. The writer may now reclaim the old region if count drops to zero.

This gives true simultaneous reads from all workers against shared memory, with reloads that never
block readers and readers that never block reloads beyond the brief counter-spin.

## Trade-offs

| | This approach | Current (per-process heap) | Pre-fork COW |
|---|---|---|---|
| Memory per worker | ~0 MB (shared pages) | ~150–200 MB | ~0 MB (until reload) |
| Reload propagates to all workers | Yes, immediately | No | No |
| Implementation complexity | High | Low | Low |
| Works with Bjoern multi-process | Yes | Yes | Yes |
| Requires redesigning Card layout | Yes (rkyv `Archive` derive) | No | No |

The main cost is invasiveness: the `Card` struct and all index types need `#[derive(Archive)]`
from `rkyv`, and filter evaluation operates on `Archived<T>` types rather than owned `T`. The
filter logic itself is unchanged — `archived_card.cmc` dereferences the same way as `card.cmc`.

## Relevant crates

- [`rkyv`](https://docs.rs/rkyv) — zero-copy serialization into mmap-able archives
- [`memmap2`](https://docs.rs/memmap2) — safe `mmap` / `shm_open` wrappers
- [`shared_memory`](https://docs.rs/shared_memory) — higher-level cross-platform shared memory

## Effort estimate

| Phase | Estimate |
|---|---|
| Add `rkyv::Archive` derives to `Card` and index types | 1 day |
| Shared memory region creation, sizing, and lifecycle | 1 day |
| Double-buffer + generation counter for atomic reload | 2 days |
| PyO3 bindings update (`QueryEngine` maps instead of owns) | 1 day |
| Testing under concurrent load | 1–2 days |
| **Total** | **~6–8 days** |
