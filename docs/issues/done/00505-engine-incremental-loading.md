# Incremental loading of the Rust engine card store

## Status: done — shipped as PR [#505](https://github.com/jbylund/sylvan_librarian/pull/505) (e4a3c16)

Streaming reload (`reload_begin` / `add_batch` / `reload_commit`) is implemented and measured:
building-worker peak 1308 → 429 MB (346 MB with streamed serialization). The two remaining
rollout tasks (flip `ENABLE_ENGINE`, retire the compose `shm_size`) moved to
[local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md).

## Problem

`_reload_engine()` ([api_resource.py](../../../api/api_resource.py)) loads the card store with the
most memory-expensive shape possible: a client-side `fetchall()` of every card row, a second full
materialization via `[dict(row) for row in rows]`, and only then a single `engine.reload(dicts)`
call. The entire corpus exists as Python objects — twice — at the same moment the Rust store is
being built.

This OOM-killed the apiservice on atlas (15 GiB host, 1875m container limit) ~28 s after startup:
one worker's reload transient plus the other workers' baselines exceeded the cgroup limit. With
the shared-memory engine now on main (#490 + #502), only one worker pays this cost per reload
cycle — a cross-worker lock plus a cross-process flock serialize writers, and the resulting rkyv
archive is shared by all workers via mmap of `/dev/shm/sylvan_librarian_cards` — but the *building*
worker's transient alone approaches the container limit.

**Status (2026-06-12): this issue is the gate-flipper.** The engine is feature-gated off in
production (`ENABLE_ENGINE`, default false), which makes `_reload_engine` a no-op — so the OOM
cannot recur today, at the cost of the engine doing nothing. The plan is: land this work on
main, re-measure, then enable the engine. (The Scryfall→DB import path got the same treatment
in #497, which is the precedent for the streaming shape below; note it is a *different* code
path — this issue covers DB→engine.)

## Measurements (2026-06-12, merged main, blue DB)

Measured on main after #490 + #502 merged, against the blue DB (96,139 cards), single process,
engine columns only (`ENGINE_COLUMNS`). Protocol: `alloc-counter` feature (counting global
allocator in `card_engine`, exposed via `QueryEngine.mem_stats()`) + `ps` RSS +
`ru_maxrss` peak. Host macOS arm64.

Process RSS by stage:

| Stage | RSS (MB) | What it is |
| --- | --- | --- |
| baseline | 57 | interpreter + imports |
| Python rows ready | 894 | psycopg buffer + all rows as dicts ×2 (~837 MB transient) |
| after `engine.reload()` | 1011 (process peak **1308**) | Rust build + serialize while Python rows alive |
| after freeing transients | 875 | allocator-retained pages; archive itself is file-backed mmap |

Rust-side breakdown of `reload()` (exact, from the counting allocator):

| Checkpoint | Live bytes |
| --- | ---: |
| `Vec<Card>` + interner built | 151.9 MB |
| + `CardIndexes` built | 162.1 MB |
| peak during reload (incl. rkyv serialize) | 304.7 MB |
| steady state after reload | 8.4 MB (31 allocations) |
| **archive written** (cards 49.5 + indexes 23.1 + strings 16.0) | **88.6 MB** |

The Python-side ~837 MB transient is the dominant cost — exactly what this issue removes. The
per-item history of the Rust-side numbers is in
[00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md#measured-progress); these
figures match its post-items-1–3 column within a few percent (the small growth is data drift in
the blue DB, not a regression).

> Operational corollary for Docker: the archive lives in `/dev/shm`, and Docker's default
> `shm_size` is **64 MB** — too small for the archive plus its 2× moment during the rename
> window (2 × 88.6 MB). The apiservice compose currently sets `shm_size: 3000M` (sized before
> the store-size reductions); ~256m would now suffice, and those tmpfs pages count against the
> container's cgroup memory limit, so shrinking it is worthwhile when the gate flips on.

## Proposed approach

Stream rows into the engine in batches instead of materializing the corpus in Python. The loop
replaces the `fetchall()` + `[dict(row) ...]` body of `_reload_engine` — everything around it
(the `ENABLE_ENGINE` gate, the `force=` semantics, the cross-worker reload guard, and the
`PoolClosed` handling) stays as it is on main:

```python
# inside _reload_engine, replacing the fetchall + dict-conversion + reload(...) body
cols_sql = ", ".join(f"card.{col}" for col in _ENGINE_COLUMNS_FROM_MODULE)
with self._conn_pool.connection() as conn:
    # named cursor => server-side; psycopg buffers one batch, not the full result
    with conn.cursor(name="engine_reload") as cursor:
        cursor.itersize = BATCH_SIZE  # ~2_000: see batch-size note under Estimated savings
        cursor.execute(f"SELECT {cols_sql} FROM magic.cards AS card")
        self._engine.reload_begin()
        while batch := cursor.fetchmany(BATCH_SIZE):
            self._engine.add_batch([dict(row) for row in batch])
        self._engine.reload_commit()  # sort/index/serialize/rename; old archive served until then
```

Rust side (`card_engine/src/lib.rs`): add `reload_begin()` / `add_batch(list[dict])` /
`reload_commit()` accumulating the staging `Vec<Card>` (and its `Interner`) across batches;
`reload_commit()` runs the existing sort/index/serialize/rename pipeline, so queries keep serving
the old archive until the new one is renamed into place (this swap already exists, including the
16-byte header write and the flock that `reload()` takes today — `reload_begin` should take the
flock and `reload_commit` release it, so two workers can't interleave staging). Keep
`reload(list)` as a thin wrapper over the three for the tests.

One new failure mode to handle: a worker that calls `reload_begin` and dies before
`reload_commit` must not leave the in-process staging buffer or the flock held — tie both to a
guard object (the flock already releases on fd drop; the staging Vec should be reset by
`reload_begin`).

## Estimated savings (from the 2026-06-12 main measurements)

Incremental loading removes the Python-side transient only: one batch of row dicts instead of
the whole corpus (~837 MB ≈ 8.7 MB per 1k rows). The Rust-side transient is untouched —
`Vec<Card>` + interner (~152 MB), indexes, and the rkyv serialize (~305 MB peak) still all
coexist at commit, because the archive is serialized in one shot.

**Batch size**: the memory floor is the Rust-side staging/serialize, not the batch — so there
is no reason to hold a large batch. ~2k rows (~18 MB) makes the Python share noise; below that
you're optimizing rounding error while adding round trips (48 fetchmany round trips at 2k over
a local socket are irrelevant for a rare, seconds-long background reload).

| Scenario | One-shot (measured) | Incremental (measured) | Saving |
| --- | --- | --- | --- |
| Reload peak (building worker) | 1308 MB | **429 MB** | 879 MB (67%) |
| RSS while batches stream | 894 MB (all rows) | 186 MB (staging grows in Rust) | |
| Steady state (shared archive) | 88.6 MB | 88.6 MB | none |

Measured 2026-06-12 with the implemented streaming path (`BATCH_SIZE = 2_000`, blue DB,
96,139 cards): identical archive (88.6 MB), identical Rust reload peak (304.7 MB), identical
query results. The 429 MB peak lands within ~13% of the back-of-envelope prediction
(57 baseline + ~18 batch + ~305 Rust ≈ 380 MB); the difference is Python allocator retention
across batches.

Operationally, at the current 1875m container limit: the one-shot building worker's 1.31 GB
transient plus its siblings' baselines (~125 MB each) OOMs the container at default worker
counts; with incremental loading the building worker peaks at 429 MB, which fits alongside
the idle workers plus the ~89 MB tmpfs archive with ample headroom.

What this does **not** fix: the Rust-side build transient and the archive's 2× moment in
tmpfs during the rename — see
[local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md) (streaming
serialization, since implemented: build peak 304.7 → 171.6 MB, worker peak 429 → 346 MB;
disk-backed archive still open) and further store-size work
([00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md), items 4–5).

## Implementation tasks

- [x] Re-measure the baseline on merged main (`alloc-counter` + `mem_stats()` + RSS stages) —
      done 2026-06-12, tables above
- [x] Rust: staging buffer + `reload_begin` / `add_batch` / `reload_commit` / `reload_abort`
      (flock held begin→commit/abort; staging reset on `reload_begin` so a crashed cycle
      can't leak; `reload(list)` kept as a wrapper)
- [x] Python: server-side cursor + `fetchmany` loop in `_reload_engine()`, inside the existing
      gate/guard/force structure, with `reload_abort` on failure
- [x] Verify queries during a reload still serve the old store — publish is still rename-only
      at commit; `test_abort_discards_staging` pins that the old store serves during staging
- [x] Re-run the memory measurement: **429 MB peak measured** (vs 1308 MB one-shot), table above
- [x] ~~Size atlas's container/worker count and flip `ENABLE_ENGINE`~~ — moved to
      [local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md)
- [x] ~~Shrink compose `shm_size` from 3000M to ~256m~~ — superseded by the disk-backed
      archive switch in [local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md)
