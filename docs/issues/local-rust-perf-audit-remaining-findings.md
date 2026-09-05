# Rust Perf Audit: Remaining Findings

Companion to a 2026-08-26 audit of `card_engine`/`shared_cache`. Five findings from that audit
shipped as individually-measured commits on branch `perf-audit-fixes` (shared-cache double-hashing,
the legalities-field registry cache, `PreparedCandidates::card_ids`'s vtable dispatch, `try_lock`'s
clock/pid overhead, and `Match`'s layout). Two were measured and declined:

- `plan_cost`'s unconditional `printings_walked` call — isolated cost ~0.4ns/call, five orders of
  magnitude below anything a routing-decision benchmark could resolve.
- `existential_leaf`'s `PLANE_BLOCKS` linear scan (originally item 2 below) — measured against a
  precomputed `Vec<Option<ExistentialLeaf>>` lookup over all `PLANE_COUNT` (290) plane indices,
  8 runs: the lookup table was slower or tied in 7 of 8 (deltas of -2% to -15%), with only one
  early run showing a +33% improvement that didn't reproduce. `PLANE_BLOCKS` is a 10-entry `const`
  array of small `Copy` structs, cheap enough (~0.4ns/call either way) that the compiler already
  handles the linear scan about as well as a `Vec`-indexed lookup, which additionally pays for a
  heap indirection and a bounds check. No commit; benchmark code reverted after measuring.

This doc is the punch list of what's left, in priority order. Each item needs its own
measure-then-implement pass and its own commit, following the same discipline as the shipped ones
— see that branch for the pattern (kernel micro-benchmark before AND after, run against a real
before/after diff, not a plausibility argument).

## 1. gen_cache: response body copied three times on every cache write

`shared_cache/src/gen_cache.rs`, `SharedCache::set` (lib.rs) → `GenerationalSharedCache::set`
(gen_cache.rs:672) → `do_insert` (gen_cache.rs:263).

Current path for a cache-miss/changed-value write:
1. `lib.rs`'s `set()`: `body.map(|b| b.to_vec())` — copies the Python bytes into an owned `Vec<u8>`
   (`body_owned`) so it can be moved into `CachedResponse`.
2. `gen_cache.rs`'s `set()`: `rkyv::to_bytes::<rkyv::rancor::Error>(&cr)` — serializes the whole
   `CachedResponse` (including `body_owned`) into a fresh `value_bytes` buffer. rkyv archives a
   `Vec<u8>` field by copying its bytes into the output, so this is copy #2 of the body specifically.
3. `do_insert`'s `copy_nonoverlapping(value_bytes.as_ptr(), ab.add(val_off), value_bytes.len())`
   (gen_cache.rs:349) — copies the whole serialized buffer into the mmap arena. Copy #3.

For larger cached bodies (search result pages can run tens of KB) this is 3x the necessary
bytes-moved per write.

**Why this is the riskiest item on the list, and needs care beyond a perf bench.** Removing copy
#1 means giving `CachedResponse` (or a parallel type used only for writes) a borrowed body field
(`Option<&[u8]>`) so rkyv serializes directly from the caller's slice instead of an owned copy.
That's a change to the wire format code, and both `do_insert` (this process, right after) and every
OTHER process's `get_with`/`access_response` (gen_cache.rs, reading the same mmap) must still agree
on the resulting `Archived<CachedResponse>` layout. rkyv's `Archive`/`Serialize` split allows a
borrowing source type to produce the same archived output as the owned one (the archived shape is
`ArchivedVec<u8>` either way), but this needs either a second struct with a lifetime parameter and
its own manual or derived `Serialize<S>` impl, or an rkyv-supported wrapper — worth checking what
rkyv 0.8 offers before hand-rolling one. A mistake here doesn't crash loudly; it's a data-integrity
risk (silently wrong bytes read back by a different worker process sharing the same mmap), not just
a wrong perf number. Test with round-trip correctness (write via the new path, read via the
existing `get_with`/`access_response`, byte-compare) before trusting any speed measurement.

Copy #3 (arena `copy_nonoverlapping`) is structural — the value has to land in shared memory
somehow — and not worth touching without a specific measured reason.

## 2. lib.rs: `assign_artwork_groups`'s illustration dedup is O(k²) per card

`card_engine/src/lib.rs:2468-2482`. For each printing, `ills.iter().position(|&x| x ==
p.illustration_id)` linearly scans the card's already-seen illustrations. Bounded by
`ARTWORK_GROUP_WORDS*64` = 512 (asserted), but cards with hundreds of printings/illustrations
(basic lands) pay real quadratic cost at every reload.

Fix sketch: a per-card `HashMap<u128, u16>` (illustration_id → group index) reused across cards
(cleared, not reallocated, between cards) instead of the linear `Vec::position` scan.

**Reload-time only, not query-time.** Check current reload latency budget/measurements (see
`docs/issues/local-engine-reload-publish-transient.md`) before deciding this is worth the
complexity — if reload is not latency-sensitive at current corpus size, this is a "someday" item.

## 3. planes.rs: `build_bit_planes` makes three separate passes over each card's printing range

`card_engine/src/planes.rs:331-421`. Rarity, border, and legality each re-slice and re-iterate the
same card's `printings[range]` independently, at reload time.

Fix sketch: fuse into one pass over `printings[range]` per card, computing all three facts
together, halving the iteration overhead of this build step.

**Also reload-time only.** Same caveat as #2 — check whether reload latency is actually a live
concern before investing; this is a straightforward but not urgent cleanup.

## Suggested order if picked back up

1 (still a per-write query-path cost, and the highest complexity/risk — budget real time for the
rkyv correctness work) → 2, 3 (reload-time only; do only if reload latency is shown to matter).
