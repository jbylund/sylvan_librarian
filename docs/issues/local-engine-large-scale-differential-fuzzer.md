# Engine: large-scale differential fuzzer (catches scale-gated bugs the small fuzzer can't)

Status: idea only, not started. Surfaced by the NULL-over-inclusion bug in
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) — `range_narrowed`'s
complement branch (only reachable once a query's match count crosses `NARROW_FLOOR`, 1,000
entries) over-included NULL-valued printings, and neither of the two existing differential
checks could catch it:

- `fuzz_row_identity_matches_reference` (Rust, `tests.rs`) generates stores with `ncards =
  rng.random_range(5..=15)`, 1-3 printings each — at most ~45 printings total, structurally unable
  to cross `NARROW_FLOOR` no matter how many seeds or filter shapes run.
- `test_engine_property.py` (Python) *does* cross the threshold (asserts `engine.size() >= 6000`
  and a broad query's total `> 1000` specifically to exercise this) and did catch the bug once
  actually run — but it needs `pytest`, not `cargo test`, so it's a slower, separate feedback loop
  than the Rust suite most engine changes are checked against day-to-day.

## The idea

A new Rust-side fuzz test, structured like `fuzz_row_identity_matches_reference` but building one
large synthetic store (~100k printings, ~30k cards) with a distribution shaped like the real
corpus (`benchmarks/bitplanes/corpus.jsonl` is the reference) rather than uniform-random — same
NULL rates per field, similar rarity/type/color distributions — so it reliably crosses
`NARROW_FLOOR`/`MAX_NARROW_FRACTION` for every narrowable field, not just by luck.

## Why not just scale up the existing fuzzer

`fuzz_row_identity_matches_reference` rebuilds a fresh store from scratch per seed (96 seeds ×
serialize-to-rkyv + archive-access each time) and the whole `cargo test` suite currently finishes
in well under a second. Scaling every seed's store to 100k printings would turn that into a
multi-second (or worse) `cargo test` run, trading away the fast local-iteration loop the rest of
this codebase's test suite is built around. A large-scale check needs to be a separate,
`#[ignore]`-by-default test (the codebase already has 8 such tests for exactly this "slower, run
explicitly" pattern) — built once (or a handful of times, not per-seed), checked against many
filters.

## Open questions (not resolved — this is a stub, not a plan)

- Build the realistic-distribution generator once and reuse across runs (fixture file?) or
  regenerate on each explicit run?
- How many filter shapes to check per run, and does `fuzz_targeted`/`fuzz_gen` (the existing
  filter-generation helpers) need extending for shapes that specifically stress broad/complement
  narrowing (deliberately-broad thresholds per field), or does random generation already produce
  enough of them at this scale?
- Where does this run: a dedicated `cargo test -- --ignored` CI step, or folded into
  `make test-integration`?

## Related

- [local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) — the bug that
  motivated this idea, including the exact mechanism (`range_narrowed`'s complement branch) and
  the fix (`must_be_tight`, forcing a direct scatter instead of a NULL-over-including complement
  for any caller that needs an unverified existential fact).
