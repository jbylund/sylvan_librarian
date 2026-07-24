# Engine: PrintingCompose Orderby-Range-Index Walk

**Status: implemented**, PR pending, filed as
[#744](https://github.com/jbylund/sylvan_librarian/issues/744). Found via `scripts/survey_queries.py`
while checking the survey's remaining slowest queries after #739–#741 landed. Measured results in
"Results" below; branch `engine-compose-orderby-range-walk`.

## Measured problem

`format:commander`, `unique=printing`, `orderby=usd`: **0.578ms**, `total=96,898` (of 97,206
printings — Commander legality is a near-total match, only 308 printings excluded, 0.32%).
`type:goblin or format:legacy`/printing/usd (0.583ms) and `format:legacy`/printing/rarity
(0.534ms) are the same shape. These are now the #3–#6 slowest entries in the broad survey
(`benchmarks/survey/branch-c6484a3.csv`), just below the `Not(Or(...))` pair at #1/#2 (out of
scope here — see "Related").

Root cause has two independent parts, both avoidable:

1. **The build is expensive because it broadcasts from the majority side.** `legality_leaf_bits`
   (via `compose_printing_bits`) builds the printing-space bitmap by broadcasting the *legal*
   card-plane down to printings (`broadcast_card_bits_to_printings` touches every *set* card — i.e.
   every legal one). For Commander that's ~99.68% of all cards: the build is effectively a full
   `O(n_cards + n_printings)` pass (`LINEAR_PASS_PER_PRINTING_NS = 1.50` in `cost.rs`, ~146μs over
   97,206 printings) — for a predicate whose *true* information content is "308 printings are
   excluded," not "96,898 are included."
2. **The paging is expensive because there's no permutation for `orderby=usd`.** Per
   `done/00740-engine-compose-permutation-fallback.md`, `usd`/`rarity` have no card-space sort
   permutation, so `printing_compose_fastpath` either walks via `gather_composed_page` (visit every
   candidate card, compute a sort key, quickselect — `O(n_cards)` regardless of selectivity) or
   declines composing outright via `COMPOSE_GATHER_MAX_CARD_FRACTION` when the predicate is this
   broad (as it is here: 99.68% > 85% → declines). Either way, the query falls back to plain
   `GatheredScan` — visit everything, sort/quickselect, ~0.578ms.

Neither cost is inherent to the predicate. Both are avoidable because `orderby=usd` has its own
pre-sorted `PrintingRangeIndex` (`indexes.price_usd`) sitting unused, and Commander's *exclusion*
set is what's actually sparse.

## Proposed approach: two independent fixes, same feature

### 1. Build from whichever side is sparser

`status_plane_bases` already returns both `(exists_base, absent_base)` for every tracked format —
the "_ABSENT"/"_ILLEGAL" plane the existing `-f:x` (negated Legality) arm already reads directly
(`legality_candidate_bits(..., negated: true)`). Extend `compose_printing_bits`'s Legality build to
choose adaptively: if the format's legal-card popcount exceeds `n_cards / 2`, build from the
*absent* plane (start printing-space at all-1s, clear each illegal card's printing range down) and
skip the divergent repair pass entirely when the format has zero divergent cards (true for
Commander in the real corpus — checked, see "Real numbers" below); otherwise build from the
*exists* plane as today. This is exactly the same "pick the cheaper side" shape `range_narrowed`
already uses (`if k <= idx.len() - k`) — same pattern, different representation, not a new idea.

This is a strict improvement to `compose_printing_bits` alone, independent of the paging fix below
— it helps `walk_grouped_page` and `gather_composed_page` too, for any near-universal Legality
predicate, regardless of orderby.

### 2. Walk the orderby's own range index when there's no permutation and mode is Printing

New paging branch inside `printing_compose_fastpath`, alongside `walk_grouped_page` (has
permutation) and `gather_composed_page` (#740's fallback): when `mode == Printing` and `sort_col` is
a *permutation-less* orderby with a printing-space value-ordered structure, walk that structure's
value-buckets in page order — same shape as `aligned_page`'s bucket walk, but testing the
already-built `pbits` bit per visited entry (the structure covers *every* printing with the value,
not just this filter's matches) instead of assuming unconditional membership in a contiguous slice.
Terminates once `offset + limit` matches are collected, so cost is
`O((offset+limit-th match's bucket) / selectivity)`, not `O(n_cards)` — the *opposite* of
`gather_composed_page`'s shape, and the reason `COMPOSE_GATHER_MAX_CARD_FRACTION` must **not** gate
this branch: that guard's premise (broad ⇒ not worth it) is backwards for this walk, where broad is
the *best* case.

The permutation-less orderbys are exactly `usd` and `rarity` (`SortCol::PriceUsd`/`Rarity` — the two
`ArchivedSortPermutations::get` returns `None` for), and each has such a structure:

- **`usd`** walks the `price_usd` `PrintingRangeIndex` (value-sorted `(cents, pid)`) — many small
  value-buckets, so both directions fill a 100-row page in ~`100/selectivity` visited entries: **μs**.
- **`rarity`** walks the exact `rarity_printing` planes/postings as one bucket per rarity int (there
  is no `PrintingRangeIndex` for rarity, but the planes *are* a printing-space value-bucketed
  structure). Only six buckets, and hugely uneven, so cost is direction-dependent: **descending**
  (rarest first) touches a tiny top bucket → **μs**; **ascending** (common first) must collect the
  whole common bucket before it can order the page, `O(commons)` ≈ tens of thousands of key
  computations → **~170μs**, still a 3× win over the gather/scan baseline but not μs. This asymmetry
  is inherent (few, uneven buckets, no within-bucket order index) — quickselect (`select_page`, not a
  full sort) keeps the ascending case as cheap as it can be. Null-value matches (no price / no
  rarity) sort last and aren't in the structure; the walk declines to `gather_composed_page` when a
  page reaches into that tail (never for a broad offset-0 page).

Scoped to `mode == Printing` only: `unique=card`/`artwork`'s row sort key is the *representative*
printing's price/rarity (chosen via `prefer`), and walking value order and taking "first occurrence
per card" is only correct if `prefer` happens to correlate with the value — the same reason
`usd`/`rarity` never got a card-space permutation in the first place. Not attempting that here.

### Cost-model update

`cost.rs`'s `compose_has_perm: bool` became a 3-way `ComposePaging` enum (`Perm` walk / `OrderbyWalk`
/ `Gather` quickselect) — the same mechanism #740 introduced, one more variant. `run_query_routed`
picks the variant the same way `printing_compose_fastpath` does. The `Legality` build-cost estimate
(`compose_printing_estimate`) now scales `broadcast` from `min(legal, illegal)` cards, not `legal`,
so the model no longer charges a near-universal format the full legal-side broadcast it stopped
paying under part 1.

## Expected cost (worked from real numbers, not guessed)

Real corpus counts (`benchmarks/bitplanes/corpus.jsonl`, checked directly rather than assumed):
`n_printings=97,206`, Commander-illegal printings=308 (0.32%), **0 divergent cards for Commander**
(no per-card repair pass needed at all — every printing of a card agrees on Commander legality in
this corpus).

Using `cost.rs`'s own calibrated constants:

| step | cost model | estimate |
|---|---|---:|
| build (illegal-side broadcast, ~308 printings' cards, no repair) | `LINEAR_PASS_PER_PRINTING_NS × ~450` | ~0.7μs |
| total (popcount, `97,206/64` words) | `PLANE_POPCOUNT_PER_WORD_NS × 1,519` | ~1.5μs |
| walk (`(0+100)/0.9968 ≈ 100` steps) | `RANGE_WALK_STEP_NS × 100` | ~0.45μs |
| fixed | `RANGE_FIXED_COST_NS` | ~0.15μs |
| **engine-internal total** | | **~2.8μs** |

This lines up with the *original* `~20μs` estimate as plausible, maybe even conservative on the
engine-internal side — but the engine's own cost-model constants don't capture everything: the
broad survey's fastest measured queries span from **~2μs** (exact-name lookups, `!"Sol Ring"`-style
— a comparably cheap "look up a small thing, stop early" shape) up to **~80μs** for the aligned
range walk (`-usd<50`/card/rarity, `total=555`, itself a small-output aligned bucket walk that
*should* be cost-model-cheap too, by this same math). That gap isn't explained by anything in this
doc, and probably reflects overhead outside the Rust engine's own cost model (PyO3 call/argument
marshaling, cost-model plan-selection dispatch before the winning plan even runs) that varies by
query shape in a way not worth guessing at here. **Realistic expectation: somewhere in the
single-digit-to-low-tens-of-μs range, not a promise of an exact number** — measure once built, same
as every other doc in this thread.

## Scope / non-goals

- `unique=card`/`artwork` with `orderby=usd`/`rarity`: out of scope, see above (`prefer`-dependent
  representative selection).
- The plane-side "pick the sparser side" build optimization is scoped to Legality only for now —
  it's the one field where "mostly-true for a popular format" is a common real pattern. Rarity
  (5 discrete values, no single dominant one) and Border don't obviously have the same imbalance;
  revisit only if a similar broad-single-value case turns up.
- `type:goblin or format:legacy`/printing/usd (#3 in the survey) is **not** fixed by this alone —
  `card_subtype` (`type:goblin`) isn't in `is_printing_composable`'s recognized leaf set at all, so
  the whole `Or` declines composability regardless of this change. Separate gap, not addressed here.
- The `Not(Or(...))` pair (`border:black -(name:ancient or pow=5)`, `id:gw -(color:gw or set:mom)`)
  currently ranked #1/#2 in the survey: unrelated, different shape, not addressed here.

## Acceptance

- `format:commander`/`format:legacy` (bare), printing mode, `usd` orderby: drop from ~0.5–0.6ms to
  the single-digit-to-low-tens-of-μs range. **Met** (52μs, ~12×).
- Same queries, `rarity` orderby: **partially met** — `rarity` has no `PrintingRangeIndex`, only six
  hugely-uneven plane buckets, so the walk is direction-dependent (see approach §2): descending
  hits μs (~51μs), ascending is `O(commons)` ≈ 170μs (3× win, not μs). Inherent, documented.
- `total` parity with today's (already-correct) numbers on every affected query. **Held** (0
  mismatches across the targeted set and the 520-query survey).
- Every `unique=card`/`artwork` query, every already-fast printing-mode query (aligned range,
  permutation-orderby), and every plane-only control: hold flat. **Held** (the permutation-orderby
  printing queries actually *improved* — part 1's cheaper build is orderby-independent).
- New Rust tests (`legality_sparse_side_build_matches_broadcast`,
  `orderby_walk_matches_gather_composed`): the sparse-side build produces bit-for-bit the same bitmap
  as the broadcast-from-legal build (differential + a per-printing-truth cross-check), and the walk
  produces the identical page as `gather_composed_page` across both orderbys, both directions, and
  several offsets. `fuzz_row_identity_matches_reference` additionally exercises the walk end-to-end
  (it forces `PrintingCompose` under `usd`/`rarity` at `limit=100`).

## Results

Measured on `benchmarks/bitplanes/corpus.jsonl` (97,206 printings), `main` @ `fc222fd` vs. this
branch, `scripts/bench_compose_orderby_range_walk.py` (5s window/config, min-ms, `direction=asc`).
Sub-millisecond throughout, given in μs:

| query | unique | orderby | main (μs) | branch (μs) | change | total |
|---|---|---|---:|---:|---|---:|
| `f:commander` | printing | usd | 607 | 52 | **11.7×** | 96,898 |
| `f:legacy` | printing | usd | 613 | 52 | **11.8×** | 96,439 |
| `f:modern` | printing | usd | 687 | 89 | **7.7×** | 73,783 |
| `f:pioneer` | printing | usd | 474 | 135 | **3.5×** | 47,416 |
| `f:commander` | printing | rarity (asc) | 555 | 174 | **3.2×** | 96,898 |
| `f:legacy` | printing | rarity (asc) | 552 | 173 | **3.2×** | 96,439 |
| `f:commander` | printing | edhrec (perm) | 202 | 44 | **4.6×** (part 1 build) | 96,898 |
| `border:black` | printing | rarity (asc) | 1,097 | 159 | **6.9×** (rarity walk) | 85,046 |
| `f:commander` | card | usd | 274 | 274 | flat | 31,451 |
| `f:commander` | artwork | rarity | 567 | 533 | flat | 45,980 |
| `usd<50` (aligned) | printing | usd | 49 | 50 | flat | 80,527 |
| `f:modern` | card | usd (plane) | 232 | 239 | flat | 22,264 |
| `t:creature` | card | edhrec | 66 | 67 | flat | 17,317 |

`f:commander`/printing/rarity **descending** measured 51μs (vs 174μs ascending) — the direction
asymmetry §2 predicts. Geomean speedup over the eight moved rows above: **~5.6×**.

Broad survey (`scripts/survey_queries.py --seed 42 --count 400 --wild 120`, `main` vs branch, 520
common queries): **0 total-parity mismatches, 0 regressions** (>10% and >10μs). Six queries improved,
including the two motivating shapes present in the wild corpus (`format:commander`/printing/usd
577→52μs, `format:legacy`/printing/rarity 576→181μs) and `year:2023 border:black`/printing/rarity
(115→62μs). By-orderby p90: rarity 456→332μs, usd 216→203μs; by-unique p90: printing 221→203μs.

`cargo test` (debug + release): 133/133. `pytest api/tests/test_engine_property.py
api/tests/test_engine_unit.py`: 158/158. CSVs under `benchmarks/compose-orderby-range-walk/`
(untracked, per the performance-PR workflow).

## Related

- [done/00740-engine-compose-permutation-fallback.md](done/00740-engine-compose-permutation-fallback.md) —
  where `gather_composed_page` and `COMPOSE_GATHER_MAX_CARD_FRACTION` came from; this doc's paging
  fix is the case that guard was never meant to cover.
- [done/00741-engine-negated-range-narrowing.md](done/00741-engine-negated-range-narrowing.md) —
  sibling investigation from the same survey-driven thread; also has the `and_child_rank`/
  `narrow_rec` single-source-of-truth precedent this doc's paging branch should follow when it's
  implemented.
- [done/00667-engine-legality-divergent-carveout.md](done/00667-engine-legality-divergent-carveout.md)
  — the existing `_EXISTS`/`_ABSENT` plane pair and divergent-repair mechanism this reuses.
