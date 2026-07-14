# Engine: squeezing broad-query selection and dedup

> **Status: done.** Implemented — dense `oracle_group`/`artwork_group` ids are assigned at
> reload and the dedup/selection paths use them. Note: a follow-up proposal would replace the
> dense group fields with `u128` UUID comparisons to enable id-based lookups — see
> [00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md), item 1.

## Problem

After the partial-sort (`select_page`) and legality-bitmap changes, broad queries are the
engine's slowest: `format:legacy` matches 95,341 of 96,139 printings and runs ~2.4–3.4 ms
depending on `unique`. The filter itself is now ~0.1 ms; the remaining cost is dedup
bookkeeping and the selection comparator. Estimated breakdown for one such query:

- ~380k `sort_key` calls: quickselect does ~2n comparisons, each comparison computes
  `sort_key` for both sides, and each call re-matches the `sort_col` string and rebuilds a
  6-tuple of `(bool, f64)` pairs ([lib.rs](../../../card_engine/src/lib.rs) `sort_key`).
- ~95k UUID string compares: linear dedup detects group boundaries by comparing
  `oracle_id` / `illustration_id` strings (mostly shared prefixes, 36 chars).
- ~95k `Vec<&Card>` pushes + the O(n) quickselect passes themselves.

The hard floor is "visit every match once": `total` is the deduped group count, so no
index or permutation trick avoids the full scan. That floor is a few hundred µs.

## Proposal 1 — precompute integer sort keys (biggest, easy)

Compute each element's key **once** into a `Vec<(Key, &Card)>` before selection, instead of
recomputing inside the comparator. Pack `Key` as an order-preserving integer: the sign-flip
trick maps `f64` to a monotonically ordered `u64` (`bits ^ (sign ? !0 : 1<<63)`), missing
values map to `u64::MAX`, plus a trailing store-index for the strict-total-order tiebreak.
Selection and the final page sort then compare plain integers — branchless and
cache-friendly — and key computation drops from ~4n to n.

Expected: 2–4× on the selection phase. Transient memory ~3 MB per broad query.

## Proposal 2 — dense group IDs at reload

`reload` already sorts cards by `(oracle_id, illustration_id)`; assign dense `u32`
`oracle_group` / `artwork_group` IDs in that same pass and store them on `Card`. Linear
dedup boundary detection becomes an integer compare; the hashmap fallback path keys on
`u32` instead of hashing strings.

## Proposal 3 — resolve orderby/prefer up front; precompute release date

- `orderby` and `prefer` should be resolved to enums once per query, not string-matched per
  card (falls out of Proposal 1 for orderby).
- `prefer_score` for `oldest`/`newest` does `released_at.replace('-', "").parse()` —
  a heap allocation **per card per call**. Parse once at reload into a stored `u32`
  (`yyyymmdd`). Invisible in default-prefer benchmarks, painful for `prefer=oldest` on
  broad queries.

With 1–3, `format:legacy` should land around 0.7–1 ms single-threaded.

## Structural options (bigger wins, real caveats)

### Precomputed sort permutations (helps `unique=printing` only)

Store one sorted `Vec<u32>` per orderby column at reload (7 × 96k × 4 B ≈ 2.7 MB). For
no-dedup queries, walk cards in sort order and take the first `offset+limit` passing the
filter; get `total` from a tight count-only scan. `format:legacy|printing` would drop to
~0.2 ms.

**Caveat:** does not transfer to the dedup paths. Walking in sort order selects each
group's best-*sort-value* printing; current semantics select the best-*prefer-score*
printing and then sort those representatives. Different answers — would need a semantics
decision, not just an optimization.

### Rayon parallelism

Scan, filter, key generation, and selection are all embarrassingly parallel; ~4–6× more on
broad queries. **Caveat:** the API already runs ~10 worker processes, so per-query threads
improve p50 at low load while stealing cores from throughput at high load. Hold until
single-query latency demonstrably matters more than QPS.

## Priority

`format:legacy` at 2.4 ms is already ~40× faster than its SQL baseline and is a tail query.
Proposals 1–3 are cheap and principled; do them together (they touch the same code) and
re-run the benchmark grid. The structural options need a load-profile argument first.

## Related

- [00490-rust-filter-extension.md](./00490-rust-filter-extension.md) — engine architecture.
- [local-format-legality-search.md](../local-format-legality-search.md) — the legality bitmap that made
  the filter cheap and exposed selection as the remaining cost.
- [local-query-benchmark-suite.md](../local-query-benchmark-suite.md) — how these numbers are measured.
