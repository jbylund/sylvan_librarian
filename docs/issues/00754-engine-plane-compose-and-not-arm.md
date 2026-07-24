# Engine: Plane-Subtree Compose Leaf + General `Not(composable)` Arm

**Status: proposed**, filed as [#754](https://github.com/jbylund/sylvan_librarian/issues/754). Found while auditing the broad-survey slow
tail after the survey-driven compose chain landed (#746 set/watermark, #749 probe, #750 arith-tuple,
#751 orderby-walk, #753 collection leaves). This is the natural next step in
[#731](done/00731-engine-compose-universal-evaluator.md)'s leaf-source table: the two things that keep
the current #1–#2 slowest survey queries (both `Not(Or(...))`) off the fast path. It is also a
**consolidation** step — see "Consolidation" below — folding the per-leaf negation and null logic those
earlier PRs each added locally into one general arm, rather than a sixth special case.

## Measured problem

Broad survey (`scripts/survey_queries.py --count 400 --wild 120 --seed 42`, 97,206-printing corpus,
branch = composite of the five PRs above + main). The two slowest queries in the whole survey are now
`Not(Or(...))` shapes, essentially unchanged from `main`:

| query | unique/orderby | min ms | total |
|---|---|---:|---:|
| `border:black -(name:ancient or pow=5)` | artwork/rarity | 0.935 | 18,824 |
| `id:gw -(color:gw or set:mom)` | printing/edhrec | 0.677 | 38,411 |

The second is fully addressable here. The first is **not** (its inner `Or` contains `name:ancient`,
text — see Scope). A broader family of `color`/`type` boolean combinations sits behind the same two
gaps.

## Where the cost is — two independent gaps

1. **No general `Not(composable)` compose arm.** `is_printing_composable`
   ([lib.rs:4636](../../card_engine/src/lib.rs#L4636)) has only *leaf-specific* `Not` arms — `-set:`
   (:4651), `-type:`/`-kw:`/… (:4667), `-range` (:4692). A `Not` wrapping an `Or`/`And` falls through
   to `_ => false` (:4695). So `-(color:gw or set:mom)` can't compose even though each concept is
   individually expressible. `narrow_rec`'s `Not` arm is no help either: it requires a *tight* child
   (`tight_narrow_space`), and an `Or` of mixed leaves isn't tight → it bails → full `GatheredScan`.

2. **Color/identity/type aren't compose leaves at all** — even though they're already card-space
   `BitPlanes` (#630, `indexes.planes`) with a compiler (`compile_plane`) and evaluator
   (`eval_planes`) that `estimator.rs:257-259` already uses to turn a plane-expressible subtree into a
   card-space bitmap. `set:mom` composes (#746); `color:gw` does not.

## Proposed approach

### 1. General null-guarded `Not(inner)` compose arm

```
Not(inner) if is_printing_composable(inner) && null_safe(inner)  =>  complement(compose(inner))
```

De Morgan in bitmap space: `Not(Or(a,b))` = `complement(bits(a) ∪ bits(b))`. The complement is a
bitwise-NOT over `n_printings/64` words (~1,519 words, ~1.5µs) — cheap.

### 2. Plane-subtree compose leaf

For any plane-expressible subtree (`compile_plane(leaf, &indexes.planes, …)` returns `Some`):
`eval_planes` → card-space bitmap → **`broadcast_card_bits_to_printings`**
([lib.rs:4802](../../card_engine/src/lib.rs#L4802), the bitmap variant). One arm covers `color`,
`id`/`identity`, `type:` card-types, devotion, and rarity **combinations** in a single broadcast — not
one leaf at a time. Broadcast the **plane**, not postings: color has no `TagIndex` to begin with
(u8 mask → transposed plane), so an id-list broadcast would first have to materialize a list from the
plane bitmap — pointless. (Postings-backed fields — subtypes/keywords/set, from #753/#746 — keep their
id-list broadcast; the primitive follows the source representation, neither is universally better.)

### 3. `null_safe(inner)` predicate — the load-bearing correctness gate, and one source of truth

Complement is exact **only** through a child that is both *tight* and *null-safe*. Recurses through
`And`/`Or`; every leaf must be non-nullable (or known-mask-complemented):

- **Safe (non-nullable):** `set_code`, `color`/`identity`/`type` (colorless/typeless = empty mask, a
  real value — never NULL), collection containment (a collection is never NULL), `border`, `rarity`,
  plane-backed `legality`.
- **Not safe as bare complement:** `watermark` (nullable — already excluded from `-set`'s sibling arm
  at :4649 for exactly this reason), and a range over a nullable date/year (needs the known-mask, per
  [done/00741-engine-negated-range-narrowing.md](done/00741-engine-negated-range-narrowing.md)).

`null_safe` must be the **single source of truth** for this decision. Today the "is this complement
sound?" judgment is scattered across the three leaf-specific `Not` arms as ad-hoc local guards
(#748's `-watermark` exclusion, #741's date known-mask, #753's "collections are never NULL"). This PR
centralizes all of it into `null_safe`, and the existing guards are re-expressed *through* it, not
duplicated beside it — the same single-source-of-truth discipline `bare_range_bounds` /
`is_arith_tuple_route` / `collection_compose_index` already follow.

This is a **design-time** correctness check: a wrong `null_safe`/tightness claim returns wrong *rows*,
not just slow ones, and only a differential test with a null-valued fixture catches it — no benchmark
will. Mark any loose leaf loose and let it stay off the complement path; never complement a loose set
(it would *exclude* real matches).

## Consolidation — delete, don't layer

This is not purely additive. #754 **supersedes** three per-leaf `Not` arms shipped incrementally:

- #741 (merged): `-range` (`Not(NumericCmp/DateCmp/YearCmp)`, lib.rs:4692)
- #748: `-set:` (`set_code_negated_leaf_bits`, lib.rs:4651)
- #753: `-type:`/`-kw:`/`-collection` (lib.rs:4667)

Each is `Not(exact leaf) → complement` hand-written per leaf — exactly the special cases the general
arm in §1 generalizes. **The three arms must be deleted and replaced by the single general arm, not
left beside it.** Layering the general arm on top would leave redundant, drifting dispatch. A
differential test must confirm the negated forms those PRs measured (`-set:dmu`, `-type:goblin`,
`-usd<c`) still return identical rows through the general path.

Forward note (not required here, but the reason to resist a sixth ad-hoc case): the compose-leaf
concept is now threaded through three parallel functions — `is_printing_composable` /
`compose_printing_bits` / `compose_printing_estimate` — for every leaf kind (border, rarity, legality,
range, set/watermark, collection, and now plane-subtree). #753 already introduced
`collection_compose_index` as a shared table to stop those three from drifting. With a 7th kind, the
trajectory points at [#731](done/00731-engine-compose-universal-evaluator.md)'s "universal evaluator":
one leaf-source dispatch (a leaf → `(bitmap, exactness, null-mask)` contract) that the three functions
become thin projections over. Do **not** add a 7th ad-hoc triple here if that small abstraction is in
reach; if it isn't, at minimum route plane-subtree and `null_safe` through shared helpers so nothing is
written twice.

## Expected cost (sizing, not a promise)

`id:gw -(color:gw or set:mom)`: `color:gw` and `set:mom` are both sparse. Build ≈ `eval_planes`
(~sub-µs over ~492 card words) + `set:mom` scatter (tiny) + `broadcast_card_bits_to_printings` of
`id:gw` (`id=gw` ≈ 1,534 printings × 1.5ns ≈ **2–3µs**, measured cardinality from
`benchmarks/bitplanes/corpus.jsonl`) + complement (~1.5µs). The union is sparse, so its complement is
near-total → **the best case** for #751's orderby-range-index walk. Projected: **~677µs → low tens of
µs** (~20–30×). No new paging needed — the A-vs-B paging question was already resolved in #753 (the
orderby walk wins for near-total; a linear-sweep alternative is redundant with `GatheredScan`).

## Scope / non-goals

- **`border:black -(name:ancient or pow=5)` (the #1 slowest) is out of scope.** `name:ancient` is text
  → `compile_plane` returns `None` on the whole `Or` → it stays declined. Text composability is the
  separate, harder text-search track (#734/#735/#736 already landed the regex/memmem half). `pow=5`
  alone *could* become a broadcast leaf (card-space numeric via the #750 arith-tuple index +
  `broadcast_card_ids_to_printings`, ~5µs for its 3,300 printings), but the `name:` leaf blocks this
  query regardless, so defer it as a possible follow-on, not part of this work.
- **Any mixed `Or` with a non-plane, non-postings (i.e. text) leaf** declines — `compile_plane`/
  `is_printing_composable` return `None`/false. Expected; `is:permanent or oracle:destroy or …`
  (survey #3) stays blocked on `oracle:`.
- **`plane_expr_is_existential`** (estimator.rs:251) — verify it isn't a trap for the broadcast path
  (planes are card-space, so it should be inert here) with a differential test, don't assume.

## Acceptance

- `id:gw -(color:gw or set:mom)` printing/edhrec: ~677µs → single-digit-to-low-tens-of-µs, `total`
  parity (38,411) exact.
- A `color`/`type`-`Or` family (e.g. `color:gw or type:goblin`, `-(type:instant or type:sorcery)`):
  measurable improvement, `total` parity.
- **Differential test** mirroring `collection_compose_leaves` / `orderby_walk_matches_gather_composed`:
  the composed `Not(Or(...))` / plane-subtree path returns row-for-row identical results to the
  `GatheredScan` reference, **including a fixture with a null-valued field** (empty color identity, a
  no-watermark printing) to exercise the `null_safe` gate — both polarities.
- **Regression guard for the consolidation:** the negated forms #741/#748/#753 measured (`-usd<c`,
  `-set:dmu`, `-type:goblin`) must still return identical rows and stay fast through the new general
  arm — proof the deleted per-leaf arms lost nothing.
- **Controls that must stay declined and flat:** any `Or` containing a text leaf (falls to the general
  path); `-watermark:` (stays non-composable); every already-fast query.
- Broad survey re-run: 0 parity failures, the #2 query drops out of the tail, nothing regresses.

## Related

- [done/00731-engine-compose-universal-evaluator.md](done/00731-engine-compose-universal-evaluator.md)
  — the parent leaf-source table this widens, and the consolidation target.
- [00753-engine-collection-compose-leaves.md](00753-engine-collection-compose-leaves.md) and #746 — the
  postings-broadcast primitive and the `-set`/`-collection` `Not`-arm precedents this generalizes and
  supersedes.
- [done/00741-engine-negated-range-narrowing.md](done/00741-engine-negated-range-narrowing.md) — the
  nullable-`Not` trap the `null_safe` gate must respect, and the third `Not` arm being folded in.
- #630 (`BitPlanes`), `compile_plane`/`eval_planes` — the plane machinery being broadcast.
- `docs/workflows/performance-pr-workflow.md` — the process this doc will follow.
