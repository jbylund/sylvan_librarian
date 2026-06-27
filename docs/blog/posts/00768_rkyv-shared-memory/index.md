---
title: "Shared Memory Collapsed Peak RSS from 1.3 GB to 350 MB Across Ten Workers"
date: 2027-06-05
publishDate: 2027-06-05
tags: ["rust", "performance", "memory", "rkyv"]
summary: "Ten Bjoern worker processes each held ~150–200 MB of identical card data. The fix: serialize with rkyv into a flat archive, write it to /dev/shm, and mmap the file in every worker. One OS page-cache copy, shared by all."
---

The OOM kill landed at 28 seconds past startup.

One Bjoern worker was rebuilding the Rust card store from 96,139 database rows.
The other nine workers were sitting idle at their baselines.
Together they exceeded the container's 1875m cgroup limit, and the kernel killed the whole apiservice.
The problem was not the database query or the Rust engine itself — it was that every worker held its own private copy of the same ~150–200 MB card dataset.
At ten workers, that is up to ~2 GB for data that was logically identical across all of them.

Before the fix, a reload looked like this: one worker fetched all 96,139 rows into Python, duplicated them into a list of dicts, called `engine.reload(dicts)`, and the Rust side built its `Vec<Card>`, index structures, and serialized the whole thing into a heap `AlignedVec` before writing it out.
The other workers never saw the result — each held its own `QueryEngine` in its own address space, populated independently.

The fix is in two parts, shipped as PRs [#502](https://github.com/jbylund/arcane_tutor/pull/502) and [#505](https://github.com/jbylund/arcane_tutor/pull/505).
The measured result, on a blue DB with 96,139 cards, macOS arm64, 2026-06-12:

| Metric | Before | After | Δ |
|---|---:|---:|---:|
| Building-worker peak RSS | 1308 MB | **346 MB** | −74% |
| RSS while rows load (Python side) | 894 MB | 186 MB | −79% |
| Rust allocator peak at commit | 304.7 MB | **171.6 MB** | −44% |
| Archive size / query results | 88.6 MB | identical | — |

Peak RSS measured via `ps` RSS on macOS arm64, sampled at the high-water mark during a reload; Rust allocator peak via a counting global allocator wired into the engine and exposed through `QueryEngine.mem_stats()`.
Both "before" and "after" rows were taken against the same blue DB with 96,139 cards.

## Why Per-Process Memory Did Not Share

The original `QueryEngine` held an `Arc<RwLock<CardData>>` containing a `Vec<Card>`, a string table, and several `HashMap`-based search indexes.
`Arc` is a reference-counted pointer into the current process's heap — it cannot cross a process boundary.
Even if two Bjoern workers share the same physical RAM pages after `fork()` (copy-on-write gives you that for free initially on Linux), the moment one worker writes to its heap — which a reload always does — the kernel copies the dirtied pages and the sharing breaks.
Each worker ends up with its own independent copy.

The only way to get true sharing across processes is to give up heap allocation entirely for the shared data and use a buffer that lives outside any process's heap: a file or `shm_open()` region that every process can `mmap` at its own virtual address, pointing to the same physical pages.

The constraint this creates is severe: a shared buffer cannot contain pointers.
A `Vec<Card>` contains a heap pointer to its backing array.
A `HashMap` contains heap pointers to its buckets.
Place either in a shared file, `mmap` it into a different process, and every pointer is now a dangling reference into another process's virtual address space.

## Zero-Copy with rkyv: Relative Offsets Instead of Pointers

[rkyv](https://rkyv.org) solves this by serializing Rust data structures into an archive where all internal references are relative byte offsets, not absolute pointers.
An archived `Vec<Card>` is a length field plus a relative offset to a contiguous block of archived `Card` structs.
An archived `String` is a length field plus a relative offset to its bytes.
Because each offset is measured from the field's own position in the buffer — not from any process-specific base address — the same archive resolves correctly when mmap'd at different virtual addresses in different processes.
ASLR is not a factor.

Reading back is the key operation.
There is no deserialization step that copies data out of the archive into owned structs.
The `Archived<Card>` type is a view directly into the archive bytes — accessing `archived_card.card_name_lower` reads from the mmap, no copy.
Every worker running a query is reading from shared physical pages, and the OS has a single copy of those pages in its page cache.

Deriving `Archive` for the card types was largely mechanical:

```rust
// Simplified — actual Card has ~40 fields
#[derive(Archive, Serialize, Deserialize)]
struct Card {
    card_name_lower: InlineStr<61>,
    card_colors: u8,
    card_color_identity: u8,
    cmc: Option<u8>,
    creature_power: Option<i8>,
    // ... numeric fields, interned string ids, bitmaps
    card_subtypes: Vec<String>,
    card_keywords: HashSet<String>,
    card_legalities: u64,
}
```

The indexes — trigram `HashMap`s, sorted `Vec<(i16, u32)>` for numeric fields, tag lists — derive `Archive` the same way and go into the same archive alongside the cards.
Everything ships in one flat file.
See the [full struct](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L147-L208) for the real layout.

## Writing and Reading the Archive Safely

The write path builds the card store, serializes it into a per-process `.tmp` file with a 16-byte header (magic bytes, format version, `size_of::<Archived<Card>>()`), and atomically renames it into place at `/dev/shm/arcane_tutor_cards`.
`rename(2)` is atomic — readers never see a partial write.
A crashed writer leaves a stale `.tmp`, not a corrupted archive at the shared path.

The read path checks the header before doing anything with the bytes.
This matters: an archive written by an older build of the Rust extension will have a different `size_of::<Archived<Card>>()` if the `Card` struct changed.
An archive that passes the header check was written by this exact build; one that fails gets treated as absent and triggers a rebuild rather than being handed to `access_unchecked`.
Handing an archive from a different build to `access_unchecked` would be undefined behavior — the memory layout the reader expects does not match what was written.

```rust
fn get_mmap(&self) -> PyResult<Arc<Mmap>> {
    let path_inode = std::fs::metadata(&self.shm_path).map(|m| m.ino())?;
    let mut guard = self.cached_mmap.lock().unwrap();
    if let Some(ref c) = *guard {
        if c.inode == path_inode {
            return Ok(Arc::clone(&c.mmap));  // inode unchanged: use cached mapping
        }
    }
    // Open and map the current file; use fstat inode (not path stat) to avoid
    // the race where the file is replaced between our stat and our open.
    let file = std::fs::File::open(&self.shm_path)?;
    let inode = file.metadata().map(|m| m.ino())?;
    let mmap = Arc::new(unsafe { Mmap::map(&file) }?);
    if mmap.len() < ARCHIVE_HEADER_LEN || mmap[..ARCHIVE_HEADER_LEN] != archive_header() {
        return Err(PyRuntimeError::new_err("archive header mismatch (stale archive; will be rebuilt)"));
    }
    *guard = Some(CachedMmap { mmap: Arc::clone(&mmap), inode });
    Ok(mmap)
}
```

Each worker caches its current mapping by inode.
One `stat(2)` per query checks whether the inode changed; remapping happens only on a change.
Since publish is rename-only, a publish always changes the inode — there is no timestamp-granularity race.
The inode after open comes from `fstat` on the already-opened file handle, not the earlier path stat, to close the window where the file could be replaced between the two calls.
The full implementation is at [`lib.rs`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L1313-L1348).

## repr(C) for Fixed-Size String Fields

The flat-archive layout also enables a query-time optimization that heap-allocated structures cannot offer.
Hot string fields — the lowercase card name used for trigram matching, the 8-character set code — default to archiving as `String`, which serializes as a relative-offset reference.
In a tight filter loop, chasing that offset for every card-name comparison costs a pointer dereference on each iteration.

The alternative is a fixed-size inline array that archives as plain bytes embedded directly in the `Card` record:

```rust
#[repr(C)]
#[derive(Clone, Copy)]
pub(crate) struct InlineStr<const N: usize> {
    bytes: [u8; N],
    len: u8,
}

unsafe impl<const N: usize> rkyv::Portable for InlineStr<N> {}
```

`repr(C)` guarantees a stable, padding-free layout.
`rkyv::Portable` marks the type as safe to treat as a flat, relocatable value — because `InlineStr<N>` contains only a fixed byte array and a length field with no interior pointers, writing it verbatim into the archive is correct.
The archived form is identical to the live form: `InlineStr<61>` in the archive is 62 bytes embedded in the `Card` record, so every card-name comparison reads from a single cache-local region rather than chasing an offset.
We did not benchmark the inline-string change in isolation — the 346 MB peak measurement captures all the layout changes together.
The cache-locality benefit is structural rather than independently quantified.
`InlineStr<61>` covers every card name in the Scryfall dataset; set codes use `InlineStr<8>`.
The full implementation is at [`card_engine/src/inline_str.rs`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/inline_str.rs).

## The Reload Spike: Two Independent Sources

Sharing the archive across workers eliminated the multi-worker duplication — idle workers now pay near-zero marginal cost for card data.
But after PR #502 shipped, the building worker still peaked at ~1.3 GB during a reload.
The spike had two independent sources, and fixing one did not fix the other.

**Python side (~840 MB):** `cursor.fetchall()` pulled all 96,139 rows into Python at once, then `[dict(row) for row in rows]` duplicated them.
The entire corpus lived as Python objects — twice — while Rust was building its `Vec<Card>` on top of that.

**Rust side (~305 MB):** After building `Vec<Card>` and the indexes (~162 MB live), `rkyv::to_bytes` built the complete archive in a single heap `AlignedVec` (~89 MB) while both the cards and the indexes were still alive.
At the last realloc, the old and new buffer capacities coexisted briefly, pushing the peak higher.
Then `write_all` copied the heap buffer into tmpfs pages with the heap buffer still alive — the archive existed twice.

PR #505 attacked both.
On the Python side, a named (server-side) cursor replaced `fetchall()`, and the body became a `fetchmany` loop feeding 2,000-row batches to a new staged reload API:

```python
with conn.cursor(name="engine_reload") as cursor:
    cursor.itersize = 2_000
    cursor.execute(f"SELECT {cols_sql} FROM magic.cards AS card")
    if not self._engine.reload_begin():
        return  # another worker just published; pick up theirs
    try:
        while batch := cursor.fetchmany(2_000):
            self._engine.add_batch(batch)
        self._engine.reload_commit()
    except BaseException:
        self._engine.reload_abort()
        raise
```

One batch of row dicts (~18 MB at 2,000 rows) is alive at a time instead of the whole corpus.
On the Rust side, `reload_commit()` replaced `to_bytes` + `write_all` with `rkyv::api::high::to_bytes_in` writing through a 1 MB `BufWriter` directly into the `.tmp` file.
The full `Vec<Card>` and indexes still have to be in memory during serialization — rkyv needs the complete object graph to compute relative offsets — but the archive bytes go directly to disk as file pages rather than staging first in a heap `AlignedVec`.
That eliminates the ~89 MB heap buffer and the realloc-doubling spike; the archive never exists as a heap copy at all.
See [`reload_commit` in `lib.rs`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L1524-L1550) and the Python streaming path at [`_reload_engine` in `api_resource.py`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/api_resource.py#L856-L913).

The staging API (`reload_begin` / `add_batch` / `reload_commit` / `reload_abort`) holds a cross-process `flock` from `begin` to `commit` or `abort`, so two workers cannot interleave staging.
A worker that calls `reload_begin` and dies releases the flock when the lock file descriptor drops — no manual cleanup needed.
If another worker publishes a new archive while a worker is waiting for the flock, `reload_begin` detects the inode change and returns `False`; the caller skips the fetch entirely and remaps to the new archive instead.

## What This Does Not Fix

PR #505 eliminated both sources of the building-worker spike, but three costs remain.

The remaining Rust allocator peak at commit (171.6 MB after PR #505) is the live staging structures themselves: the `Vec<Card>` (~152 MB) plus rkyv arena scratch.
Shrinking those requires reducing the per-card memory footprint of the `Card` struct and the indexes, not changing how they are loaded or serialized.

The two-copy window around the atomic rename — where the old archive and the new archive both exist in tmpfs simultaneously — is also still present.
Workers hold `Arc<Mmap>` handles to the old mapping and refresh lazily on the next query's inode check, so an idle worker can pin the old archive's pages indefinitely.
The `shm_size: 3000M` in the Docker Compose file exists because of this window.
Moving the archive from `/dev/shm` (tmpfs, unevictable) to a regular disk-backed path would convert the 2× from an OOM risk to a cache-pressure event — old pages become evictable once no mapping holds them.
That change is not yet shipped.

The approach also only works when all workers run in the same container and share a filesystem path.
Across hosts, a different mechanism is needed.

The gate was the OOM kill.
With the 346 MB peak fitting inside the 1875m container limit alongside the other workers' baselines, the engine moved from a feature-gated no-op to production.
