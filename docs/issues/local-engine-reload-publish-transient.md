# Engine reload: serialize/publish transient and the tmpfs 2× window

## Problem

Even after [incremental loading](done/00505-engine-incremental-loading.md) removes the ~837 MB Python-side
transient, the building worker still pays a ~305 MB Rust-side peak at `reload_commit()`
([card_engine/src/lib.rs](../../card_engine/src/lib.rs)), and the archive briefly exists twice in
tmpfs around the atomic rename. Numbers below are from the 2026-06-12 measurement on merged main
(96,139 cards, archive 88.6 MB) in
[00505-engine-incremental-loading.md](done/00505-engine-incremental-loading.md#measurements-2026-06-12-merged-main-blue-db).

At the peak moment, these coexist:

1. **Native staging structures, ~152 MB** — the `Vec<Card>` + interner strings. Shrinking these
   is [dropping the `_lower` copies](local-engine-drop-lowercase-copies.md) and
   [vocab-interning the collection fields](done/00598-engine-collection-vocab-interning.md), out of scope here.
2. **`CardIndexes`, ~10 MB** — heap-form indexes (live total 162 MB).
3. **The one-shot serialize buffer, ~89 MB + growth slop** — `rkyv::to_bytes` builds the entire
   archive in one contiguous heap `AlignedVec` while all sources are still alive, and the buffer's
   geometric growth transiently holds old + new capacity at the last realloc.
4. **A fourth copy at write time** — `write_all(&bytes)` duplicates the 89 MB heap buffer into
   tmpfs pages while the heap buffer *and* the old archive's tmpfs pages are still live.

On the tmpfs side, the 2× window is wider than "during the rename": the rename itself is
metadata-only, but the new archive's pages exist from `write_all` onward, and the *old* archive's
pages survive the rename for as long as any worker still has it mapped — an unlinked tmpfs file
keeps its pages until the last mapping drops, and workers refresh their cached `Arc<Mmap>` only
lazily, on the next query's inode check. An idle worker pins the old archive indefinitely. tmpfs
pages are unevictable and charged to the container's cgroup, which is why this window sets the
Docker `shm_size` floor.

The 2× cannot be fully eliminated while queries keep serving the old archive until the new one is
published — atomic swap means both copies exist somewhere briefly. The goal is to make the moment
cheap (fewer simultaneous copies) and reclaimable (not a hard cgroup charge).

## Proposed changes

1. **Stream-serialize directly into the `.tmp` file.** Replace `to_bytes` + `write_all` with
   rkyv's writer-based serialization (or serialize into a pre-sized file-backed mmap of the
   `.tmp`). The archive bytes then exist exactly once, as file pages — no ~89 MB heap buffer, no
   doubling-realloc spike, no heap+tmpfs double copy at write time. Fallback if the writer API
   fights us: pre-size the `AlignedVec` to last-archive-size + slack, which removes the realloc
   spike but keeps one heap copy.

2. **Move the archive off tmpfs to a disk-backed file on Linux.** An mmap of a regular file gives
   identical cross-process sharing (the workers are processes in one container; `/dev/shm` buys
   nothing across them). The difference is reclaim: after writeback, file-backed pages are clean
   page cache the kernel can evict under pressure, instead of an unevictable tmpfs charge that can
   OOM the cgroup. The 2× still happens transiently as dirty page cache, but it degrades to a
   disk re-read instead of an OOM kill, and the `shm_size` knob (and its
   [planned shrink to ~256m](done/00505-engine-incremental-loading.md#implementation-tasks)) disappears
   entirely. macOS dev already runs this model via the `/tmp` fallback. The path needs to survive
   only as long as the container (rebuilt on startup anyway), so the container filesystem or an
   anonymous volume is fine.

3. **Bound the old-mapping tail.** The building worker cannot unmap other processes' mappings, so
   after a publish, idle workers keep the old archive's pages alive until their next query. Add a
   cheap periodic remap (timer or per-N-requests inode check, reusing the existing `remap()` path)
   so the tail of the 2× window is bounded instead of open-ended.

Not pursued:

- **A/B slot ping-pong with a pointer file** — still holds both copies by design; adds
  complexity without changing the peak.
- **In-place overwrite of the live archive** — readers would see torn data mid-query.
- **Truncating the old file after rename** — SIGBUS for in-flight readers.
- **Compressing the archive** — breaks mmap-ability.

## Expected effect

At the publish moment, per the 2026-06-12 numbers (archive 88.6 MB):

| Copy | Today | After items 1–2 |
| --- | --- | --- |
| Native staging + indexes (heap) | ~162 MB | ~162 MB (see [engine-drop-lowercase-copies](local-engine-drop-lowercase-copies.md) / [engine-collection-vocab-interning](done/00598-engine-collection-vocab-interning.md)) |
| Serialize buffer (heap, incl. realloc slop) | ~89–140 MB | 0 |
| New archive (file pages) | 88.6 MB unevictable tmpfs | 88.6 MB evictable page cache |
| Old archive until last remap (file pages) | 88.6 MB unevictable tmpfs | 88.6 MB evictable page cache |

The Rust allocator peak drops from ~305 MB to roughly the 162 MB live-structures floor, and the
file-page 2× stops being an OOM risk. Item 3 turns the old-archive tail from unbounded (idle
worker) into a fixed window. Archive size itself is
[store-size reduction](local-engine-drop-lowercase-copies.md) territory and multiplies through every row
above.

## Measured result (2026-06-12, item 1 shipped in PR [#505](https://github.com/jbylund/sylvan_librarian/pull/505))

`reload_commit()` now streams via `rkyv::api::high::to_bytes_in` into a 1 MB `BufWriter` over
the `.tmp` file (header still written first; per-PID tmp + rename publish unchanged). Same
protocol as the incremental-loading measurements (blue DB, 96,139 cards, `BATCH_SIZE = 2_000`):

| Metric | Heap-buffer serialize | Streamed serialize |
| --- | ---: | ---: |
| Rust allocator peak during reload | 304.7 MB | **171.6 MB** (−44%) |
| Building-worker process peak | 429 MB | **346 MB** (−19%) |
| Archive / query results | 88.6 MB / — | identical |

The 171.6 MB lands ~10 MB above the 162 MB live-structures prediction (rkyv arena scratch).
Combined with incremental loading, the reload peak is now 1308 → 346 MB (−74%) from where
main started the day. The `alloc-counter` component diagnostics were reordered after the
publish (with the peak snapshotted first) so they no longer pollute the reported peak.

## Implementation tasks

- [x] Replace `to_bytes` + `write_all` in `reload_commit()` with streaming serialization into the
      `.tmp` file (keep the 16-byte header write and the per-PID tmp + rename publish as is)
      — shipped in [#505](https://github.com/jbylund/sylvan_librarian/pull/505) alongside
      [incremental loading](done/00505-engine-incremental-loading.md)
- [x] Re-measure the reload peak with `alloc-counter` + RSS; confirm the serialize buffer is gone
      — table above
- [ ] Switch the Linux default `shm_path` to a disk-backed location; drop `shm_size` from the
      apiservice compose (supersedes the ~256m shrink task in
      [00505-engine-incremental-loading.md](done/00505-engine-incremental-loading.md#implementation-tasks))
- [ ] Add a bounded remap for idle workers (periodic inode check via the existing `remap()`)
- [ ] Verify a reload under live queries: old archive served until rename, no SIGBUS, old pages
      released after the remap window
- [ ] Size atlas's container/worker count and flip `ENABLE_ENGINE` (rollout task moved here
      from [done/00505-engine-incremental-loading.md](done/00505-engine-incremental-loading.md); this is
      the goal the whole reload-memory series serves)
