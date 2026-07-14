# Shared Cache: Lock-Free Active-Page Reads via Page Seqlock

## Background

The shared cache has one global spinlock (`CoordHeader.lock`). Sealed pages are
already probed without it — generation counters detect a concurrent rotation and
the caller retries as a miss. The active page is the remaining contention point:
every `get_with` call acquires the spinlock, probes the slot table, snapshots the
arena offset, then releases it before calling `f`.

The lock hold time per read is short (~100-200 ns: one hash-table probe over a
few cache lines), but it serializes all workers for every cache hit.

We already use a seqlock for one narrower race: `value_seq` on each slot brackets
in-place arena overwrites so `get_with` can detect a torn read of the arena bytes
after the lock is released. That pattern can be extended one level up to cover
the slot table itself.

## Proposed Approach

Add a `write_seq: u32` to `PageHeader` (using existing pad bytes — the header is
64 bytes with 48 bytes of padding). The active-page writer increments it to odd
before any slot mutation and back to even after:

```rust
// PageHeader (region.rs)
pub write_seq: u32,  // seqlock: odd = mutation in progress, even = stable
```

`get_with` on the active page becomes:

```rust
// No spinlock needed for the slot probe:
loop {
    let seq_before = read_page_write_seq(active_idx);
    if seq_before & 1 == 1 { std::hint::spin_loop(); continue; }
    fence(Ordering::Acquire);

    let snap = self.do_probe(active_idx, hash, key).map(|(off, len, slot_idx)| {
        let seq = read_value_seq(self.slot_ptr(active_idx, slot_idx));
        (off, len, slot_idx, seq)
    });

    fence(Ordering::Acquire);
    let seq_after = read_page_write_seq(active_idx);
    if seq_after != seq_before { continue; } // mutation raced us — retry

    break snap;
};
```

The spinlock would then only be held on the write path: `do_insert` brackets all
slot mutations with `inc_page_write_seq` / `inc_page_write_seq`, and the existing
per-slot `value_seq` seqlock covers the arena bytes after the lock is released.

Result: reads of both active and sealed pages become fully lock-free. Writes
still acquire the spinlock to coordinate `arena_head` bumps, `entry_count`, filter
updates, and rotation.

## Interaction with Existing Seqlocks

There are two levels of seqlock after this change:

| Seqlock | Scope | Protects |
|---|---|---|
| `PageHeader.write_seq` | per page | slot table (key_hash, arena_offset, key_len, …) |
| `RawSlot.value_seq` | per slot | arena bytes for in-place overwrites |

`get_with` checks the page seqlock to validate the probe, then reads `value_seq`
from the found slot to validate the arena read. A writer that overwrites a slot
in-place must increment both: `write_seq` around the slot field update and
`value_seq` around the `copy_nonoverlapping` into the arena. New inserts (no
existing slot) only need `write_seq`.

## When This Matters

At the current scale (4-8 Bjoern workers, DB queries in the 10-100 ms range)
the spinlock is never the bottleneck. The lock hold per read is under 1 µs; a
cache hit saves 50-100 ms of DB round-trip. Spinlock contention only becomes
measurable at 32+ workers or query rates fast enough that many workers hit the
cache simultaneously on the same hot key.

A cheap proxy for whether it matters: add a miss counter to `try_lock` and log
it. If it stays near zero under load, the optimization is purely theoretical.

## Should We Do It?

The implementation is moderate complexity: two new `inc_page_write_seq` calls
wrapping every slot mutation in `do_insert` and `commit_rotation`, plus a
retry loop in `get_with`. The existing `value_seq` pattern is a near-exact
template.

**Verdict**: not worth it now — do it if spinlock contention shows up in
profiling or if the worker count grows significantly. The instrumentation step
(counting `try_lock` misses) is cheap and should come first.
