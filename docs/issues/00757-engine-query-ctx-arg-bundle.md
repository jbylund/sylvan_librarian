# Engine: Bundle the Store/Index Slice Args into a `QueryCtx`

Status: proposed, not yet implemented. Tracked as #757. Filed from the #752
([00745-engine-explain-analyze.md](done/00745-engine-explain-analyze.md)) review — the `explain`/
`explain_analyze` primitives pushed the plan-selection layer's argument lists past the point where
threading them individually reads well.

## The finding

The plan-selection functions all take the same store/index slices individually, plus a
`#[allow(clippy::too_many_arguments)]` to silence the resulting lint. After #752 the counts are:

| Function | Args | Location |
| --- | --- | --- |
| `acquire_plan_features` | 13 | [lib.rs:6314](../../card_engine/src/lib.rs#L6314) |
| `explain` | 12 | [lib.rs:6637](../../card_engine/src/lib.rs#L6637) |
| `explain_analyze` | 15 | [lib.rs:6704](../../card_engine/src/lib.rs#L6704) |
| `run_query_routed` | 13 | [lib.rs:6440](../../card_engine/src/lib.rs#L6440) |
| `run_query_with_plan` | 15 | [lib.rs:6541](../../card_engine/src/lib.rs#L6541) |
| `candidate_feats` | 8 | [lib.rs:6288](../../card_engine/src/lib.rs#L6288) |

Five of the leading args are identical across all of them and never vary within a call chain — they
come straight off the mmap'd `Archived<CardData>` the caller already holds:

```rust
cards:    &[AOracleCard],
printings:&[APrinting],
offsets:  &AOffsets,
strings:  &AStrings,
indexes:  &Archived<CardIndexes>,
```

The remaining args (`filter`, `plane`, `mode`, `sort_col`, `descending`, `limit`, `page_offset`,
and the timing knobs) are the ones that actually differ per query or per call — those are the
meaningful signal, and they're currently buried among the boilerplate five.

## Proposed change

Introduce a borrow-only context struct grouping the invariant slices:

```rust
struct QueryCtx<'a> {
    cards:     &'a [AOracleCard],
    printings: &'a [APrinting],
    offsets:   &'a AOffsets,
    strings:   &'a AStrings,
    indexes:   &'a Archived<CardIndexes>,
}
```

Built once at each PyO3 entry point (`query`, `explain`, `explain_analyze`) right after
`access_unchecked`, then threaded as a single `ctx: &QueryCtx` argument. Every function in the
table above drops five args; several fall back under clippy's threshold and can shed their
`#[allow(clippy::too_many_arguments)]`.

`QueryCtx` holds only shared references with a single lifetime, so it's a zero-cost grouping — no
ownership change, no clone, and the existing `'a` return-borrow relationships (e.g.
`run_query_routed`'s `Vec<(&'a AOracleCard, &'a APrinting)>`) carry through the struct's lifetime
unchanged.

## Why this isn't purely mechanical

- **`filter` stays a separate `&mut` arg**, not a `QueryCtx` field: `prepare_candidates` needs
  `&mut FilterExpr`, and `explain_analyze` deliberately clones a fresh filter per `(plan, round)`
  off a pristine snapshot (the #752 fairness discipline). Folding it into an immutable-borrow ctx
  would fight both. Keep it out.
- **`mode`/`sort_col`/`descending` are query params, not store state** — they belong in the
  per-call arg list, not the ctx. The split is "what came off the archive" vs. "what the request
  asked for"; only the former goes in `QueryCtx`.
- **Behavior must be identical.** `force_plan_differential_agreement` and the existing
  `query()`/`explain` parity tests are the regression guard — this is a signature refactor with no
  intended behavior change, so those passing unchanged is the acceptance bar.

## Priority

Low — cosmetic/readability, no correctness or performance stake (a borrow struct compiles to the
same argument passing). Worth doing next time this layer is opened for a feature change rather than
as a standalone churn PR, since it touches the signature of every plan-selection function and would
conflict with any in-flight work there.

## Related

- [00745-engine-explain-analyze.md](done/00745-engine-explain-analyze.md) — the diagnostic work (#752)
  that pushed the arg counts up and surfaced this.
- [done/00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md) — the
  cost-based router (`run_query_routed`, `acquire_plan_features`) whose signatures this cleans up.
