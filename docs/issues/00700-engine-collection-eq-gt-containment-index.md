# Narrow CollectionCmp Eq/Gt via the Containment Index

[#700](https://github.com/jbylund/sylvan_librarian/issues/700).

## Measured problem

`narrow_rec`'s `CollectionCmp` arm only narrows `Ge` (`:`, plain containment) through the
per-value containment index (`subtypes`/`keywords`/`oracle_tags`/`art_tags`/`is_tags`). `Eq` (`=`)
and `Gt` (`>`) fell through to a full scan of every card/printing, even though both ops still
require `contains(value)` as a precondition.

Benchmarked (`scripts/bench_collection_eq_gt.py`, `benchmarks/bitplanes/corpus.jsonl`, 97,206
printings, `main` @ `bf6f788` vs. this branch, release build, 3s timed window per config, min-of-N):

| Query | Before | After | Speedup |
|---|---|---|---|
| `subtype=Human` | 0.310 ms | 0.112 ms | 2.8x |
| `subtype>Human` | 0.417 ms | 0.078 ms | 5.3x |
| `keyword=Flying` | 0.291 ms | 0.081 ms | 3.6x |
| `keyword>Flying` | 0.396 ms | 0.084 ms | 4.7x |
| `otag=removal` | 0.109 ms | 0.023 ms | 4.8x |
| `otag>removal` | 0.443 ms | 0.126 ms | 3.5x |
| `art=human` | 0.546 ms | 0.233 ms | 2.3x |
| `art>human` | 0.863 ms | 0.254 ms | 3.4x |
| `is=spell` | 0.483 ms | 0.003 ms | 179x |

Full table in the PR. Geomean 2.6x over the 12 non-degenerate queries above (4.8x if the `is:`
pair is included — see Acceptance for why that one's an outlier, not a representative win).

## Where the cost is

`Eq`/`Gt` fell outside the arm's `matches!(op, CmpOp::Ge)` guard entirely, so `narrow_rec` returned
`None` for them and the driver ran its residual `FilterExpr::matches` linear scan over the whole
card/printing set — the same cost `:` used to pay before #628-era indexing existed for it.

## Proposed approach

`matches()`'s own definitions make the subset relationship exact, not approximate:

- `Ge` (`:`): `contains(value)`
- `Eq` (`=`): `coll == {value}` ⟺ `len == 1 ∧ contains(value)`
- `Gt` (`>`): proper superset of `{value}` ⟺ `contains(value) ∧ len > 1`

Both `Eq` and `Gt` imply `contains(value)`, so every row they can possibly match is already inside
the Ge postings for that value — the postings are a sound (if not exact) candidate superset for
all three ops. Concretely: widen the arm's guard to `Ge | Eq | Gt`, keep gathering postings exactly
as before, and pick tightness by op — `Ge` stays `Narrowed::tight`, `Eq`/`Gt` become
`Narrowed::loose`. Loose is enough: narrowing is purely advisory in this engine (see `Candidates`'/
`Narrowed`'s doc comments in `card_engine/src/lib.rs`) — the driver always re-runs `matches()` on
every candidate regardless of tightness, so the length condition the postings can't decide gets
checked there for free. The empty-postings case (`None if complete`) stays exact for all three ops
unchanged: no postings for `value` means `contains(value)` is false everywhere, which makes `Ge`,
`Eq`, and `Gt` all provably empty at once.

`frame_data` stays excluded for `Eq`/`Gt` too, same as `Ge` (#628 — its index is deliberately
incomplete, so absence proves nothing there). `tight_narrow_space`'s `Ge`-only guard already
excludes `Eq`/`Gt` from the `Not`-complement fast path — correct as-is, since their narrowing is
loose, not tight; left unchanged, just newly commented.

Rejected: a second index keyed by `(value, len)` to make `Eq`/`Gt` exact. Not worth it — the
residual check the driver already runs makes the extra index pure duplication for a case that's
already fast without it.

## Acceptance

- `fuzz_row_identity_matches_reference` (`card_engine/src/tests.rs`) already generates
  `CollectionCmp` filters across all six `CmpOp`s against biased-vocabulary stores and asserts every
  row `run_query` returns is accepted by the trusted linear-scan oracle — it exercises the new loose
  narrowing path automatically. Green in debug and `--release`.
- New unit test `narrow_candidates_eq_gt_reuse_ge_postings_loosely` (`tests.rs`) asserts
  `narrow_candidates_exact` returns identical postings for `Ge`/`Eq`/`Gt` over the same value, tight
  only for `Ge`; confirms the empty-postings case stays tight for all three; confirms `frame_data`
  still declines for `Eq`/`Gt`.
- `subtype=`/`subtype>`, `keyword=`/`keyword>`, `otag=`/`otag>`, `art=`/`art>` all improve (table
  above); `subtype:`/`keyword:`/`otag:`/`art:` (already-indexed `Ge` controls) and
  `subtype!=`/`subtype<`/`subtype<=` (genuinely un-indexable, must stay full-scan) show no
  regression. `total` is identical before/after for every query — the narrowing is advisory-only,
  so result counts can't move.
- `art=plane`/`art>plane` (a broad tag — 73,722 of 97,206 postings, over `MAX_NARROW_FRACTION`'s 25%
  threshold) show ~1.0x: the scatter-to-bitmap path pays nearly the same cost as a full scan once
  the postings are that broad, so the win here is real but small. Reported honestly rather than
  cherry-picked out of the table.
- `is=spell`/`is>spell` show a 179-181x speedup, but it's a degenerate case: this corpus's
  `card_is_tags` is empty for all 97,206 rows (a query-rewrite change, #713/#714, moved most `is:`
  values off this index), so both only exercise the exact-empty-postings branch, not the loose
  `Some(v)` path a populated index would hit. Kept in the benchmark for coverage, called out
  separately so the headline geomean isn't inflated by an index that's currently always empty.
