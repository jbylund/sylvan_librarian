# Engine: permuted match bitmaps + exact-candidate promotion

Status: done — Steps 1/2 merged and shipped. GitHub: #634 (closed). Remaining "Step 1.5"
scope split out to #656 and #657, successor to #630 phase 1 (PR #633).

## Problem

Planes made the filter free; the bitmap's *delivery* is the new ~5 ns/match
floor (candidate vec materialization, card_pass(True) dispatch, 126 kB counts
fill, 31.5k-entry perm walk with random counts reads). This is why
`t:creature` didn't move in #633 — identical downstream work either way:
t:instant 3.6k → 0.072 ms, c:g 6.4k → 0.089, t:creature 17.3k → 0.149.

## Design

**A. Permuted bitmap.** Inverse permutation per card-level sort column
(~630 kB total; desc = n-1-pos). Scatter set bits through inv_perm into a
second 4 kB thread-local buffer. unique=card: total = popcount, skip =
word-popcount accumulation (64 cards/word — deep pagination ≈ 492 words),
emit = bit-select + map back via forward perm, per-card work for ~limit
cards only. printing/artwork under all_match: weights are O(1)
(offsets diff / artwork_groups) — trailing_zeros walk over set bits only.
Mixed filters scatter after residual eval; counts read at set bits only.
rarity/usd orderbys and STREAM_MIN_MATCHES gate unchanged.

**B. Exactness flag.** narrow_candidates sources classified exact
(planes, card-space numeric ranges, TypeCmp Ge postings) vs advisory
(trigrams, printing-space projections). And-intersection exact iff every
child consumed exactly; exact whole-filter → all_match promotion → no
residual eval, counts from offsets/groups, path A applies.
`f:modern t:creature power>3` (post phase 2) → zero per-card predicate work.

## Expected

t:creature 0.149 → ~0.03–0.06 ms; c:g 0.089 → ~0.03–0.05; deep pagination
O(words); advisory residuals (o:draw, regex, arithmetic) keep the
set-bit-iteration floor by design.

## Related

- [00630-engine-card-bitplanes.md](00630-engine-card-bitplanes.md) — #630; phase 2
  legality planes widen the exact-composed class
- [00619-engine-bitmap-streaming-select.md](00619-engine-bitmap-streaming-select.md) —
  #632 forward perms + counts buffer this partially retires
- [local-engine-legality-bitplanes.md](local-engine-legality-bitplanes.md) — #630 phase 2
  (PR #654), base branch for this work; flagged on #634 as a source shape
  ("exact + shared advisory carve-out") the exactness classification below
  doesn't yet have a case for — Step 3 candidate, not in scope here

## Grounded implementation plan (2026-07-09, branched off PR #654)

### Baseline (`scripts/bench_permuted_order.py`, new — adds an `offset` axis
`bench_bitplanes.py` never exercised, 0.5s/config)

| group | query | unique | offset | total | avg ms |
|---|---|---|---|---|---|
| exact-single | `t:creature` | card | 0 | 17,317 | 0.139 |
| exact-single | `cmc<=6` | card | 0 | 30,164 | 0.436 |
| exact-single | `!"Lightning Bolt"` | card | 0 | 1 | 0.003 |
| exact-compound | `t:creature power>3` | card | 0 | 4,239 | 0.109 |
| exact-compound | `c:g t:creature cmc<=4` | card | 0 | 2,931 | **0.237** |
| exact-compound | `t:creature` | printing | 0 | 45,976 | 0.131 |
| exact-compound | `t:creature` | artwork | 0 | 22,510 | 0.127 |
| deep-offset | `t:creature` | card | 5,000 | 17,317 | 0.146 |
| deep-offset | `t:creature` | card | 15,000 | 17,317 | 0.159 |
| deep-offset | `t:creature power>3` | card | 5,000 | 4,239 | 0.056 |
| deep-offset | `cmc<=6` | card | 500 | 30,164 | 0.449 |
| advisory-single | `f:modern` | card | 0 | 22,264 | 0.188 |
| advisory-mixed | `f:modern t:creature power>3` | card | 0 | 3,046 | 0.113 |

`exact-*`/`deep-*` rows are the queries Part A/B target. `advisory-*` rows are
the correctness tripwire — must never get promoted, must not regress.

### Key finding: Part B's classification mostly already exists

Tracing the actual code rather than re-deriving from the design above:
`Narrowed { set: Candidates, tight: bool }` in `narrow_rec` already tracks
"exact" — set by numeric ranges, `ExactName`, complete-index `CollectionCmp::Ge`,
and the compile_plane-consumed check. `and_all`/`or_all` (the And/Or
composition already used by `narrow_rec`) already propagate it correctly:
"Tight iff every input is tight," and the And arm's `every_child_included`
guard already turns it false whenever a child got skipped for cost reasons
(`AND_SKIP_THRESHOLD`) — exactly the "don't promote unless provably complete"
rule this design calls for. Grep confirms `.tight` is read in exactly 4
places, all inside `narrow_rec`'s own recursion — it never escapes to a
caller today. So Part B isn't a new classification system from scratch; it's:

1. `narrow_candidates()` (the public wrapper `run_query` calls) needs to
   expose `tight` instead of discarding it (returns `Option<Candidates>`
   today, throwing the flag away).
2. Nothing yet combines the residual's `tight` with the `plane`'s exactness
   (always exact when present — that's what `compile_plane` already
   guarantees) into one "is the whole original filter exact" signal at the
   `query()`/`run_query` boundary.
3. Nothing uses that signal to skip `card_pass` — confirmed by reading
   `run_query`'s match loop and `run_query_streamed`'s two call sites:
   `card_pass` runs per candidate unconditionally today, even for a
   provably-tight numeric range, redoing real `tri()` evaluation for no
   reason. `card_match_count`/`push_card_matches` already accept an
   `all_match: bool` and already have the O(1) fast-path branches for it
   (built for the per-card case where `card_pass` itself said `Tri::True`) —
   Part B just needs to supply that bool structurally instead of per-card.

### Secondary finding, bundle into Step 1

`c:g t:creature cmc<=4` (0.237 ms) is anomalously slow for a 2,931-row result
(compare `t:creature power>3`, 4,239 rows, 0.109 ms). After `split_planes`
consumes `c:g`/`t:creature` into `plane` (popcount ~4,166), the residual
`cmc<=4` narrows via `numeric_candidates`, which returns a **materialized
`Vec<u32>`** — likely 20,000+ entries (`cmc<=6` alone is 30,164, 96% of the
corpus). `run_query` retains that large Vec against the small plane bitmap:
the same "materialize the broad side, retain against the tight side" shape as
the legality regression fixed in PR #654 — except numeric ranges return
`Candidates::Cards`, not `CardBits`, so that fix's direct bitmap-AND doesn't
apply here. Not fully free to fix (`numeric_candidates` has to materialize
*something* from the sorted index), but scattering the Vec into a bitmap
before intersecting — rather than retaining element-by-element — turns an
O(popcount) retain plus an O(wpp) plane read into one O(wpp) AND. Worth
bundling into Step 1 since that work already touches this exact code path.

### Phased plan

**Step 1 (Part B: exactness propagation + all_match promotion) — done**
- Expose `tight` from `narrow_candidates` (new `narrow_candidates_exact`,
  `narrow_candidates` kept as a `#[cfg(test)]`-only thin wrapper); combine
  with plane exactness at the `run_query` boundary: `all_match_known =
  matches!(filter, FilterExpr::True) || residual_exact`.
- Skip `card_pass` per candidate when `all_match_known`; feed the existing
  `all_match: bool` fast paths directly, in `run_query`'s gathered loop and
  `run_query_streamed`'s two emission loops (small-gather, main walk).
- Does **not** touch the permutation walk, counts buffer, or pagination —
  removes redundant per-candidate evaluation, keeps today's O(candidates)
  materialization. That's Step 2. The bundled "scatter-before-intersect" idea
  from the original plan was dropped: `c:g t:creature cmc<=4`'s slowness
  turned out to be `numeric_candidates`'s internal re-sort (value-order index
  slice re-sorted to id-order), paid before any plane interaction — a
  numeric-index-specific cost the plane-intersection swap wouldn't have
  touched. Left as a possible `numeric_candidates` optimization, not pursued
  here (out of scope for this issue; see confirmed-flat benchmark row below).

  **Two real bugs found and fixed during implementation** (both caught by the
  differential property test suite and/or targeted regression tests before
  reaching benchmarking):
  1. **Printing-space tight ≠ card-level all_match.** `narrow_rec`'s `tight`
     means "every member of *this* set satisfies the predicate" — for a
     printing-space result (`set:`, `artist:`, `frame:`, `year:`/`date:`,
     `number:`) that's "this specific printing," not "every printing of the
     card," which is what `card_pass`'s `Tri::True` means. A card can have
     printings in and out of a printing-space match (`s:lea` — most cards
     have other-set printings too). Fix: `narrow_candidates_exact` only
     reports exact when the result is card-space. Regression test:
     `all_match_promotion_never_fires_for_printing_space_tight_results`.
  2. **Mode::Artwork match-phase regression.** Applying `all_match_known` in
     `run_query_streamed`'s match phase (which visits every candidate, not
     just the emitted page) measured a ~45% slowdown for `t:creature`
     unique=artwork specifically (isolated by bisecting call sites) — an
     unexplained codegen/scheduling effect in that loop for that mode, not a
     logical cost (card_pass's own return value is identical either way).
     Card/Printing modes showed no such effect and do benefit. Fix: gate that
     one call site on `!matches!(mode, Mode::Artwork)`; the two emission
     loops (which only touch ~`limit` candidates) are unaffected either way.
- Risk: shared, hot, correctness-critical path. `advisory-*` benchmark rows
  are the regression tripwire — a wrongly-promoted advisory filter would
  silently return wrong results, not just run slow. Parity tests non-negotiable
  before benchmarking — both bugs above were caught this way, not by the
  benchmark.

**Step 1 results** (`scripts/bench_permuted_order.py`, 1.0s/config, machine
quiesced — Docker Desktop's VM was consuming ~32% CPU continuously and
contaminated earlier measurement attempts; min_ms shown, more robust to
system noise than avg_ms):

| query | before | after | speedup |
|---|---|---|---|
| `t:creature` (card) | 0.127 ms | 0.099 ms | **1.29×** |
| `t:creature` (printing) | 0.119 ms | 0.092 ms | **1.29×** |
| `c:g` | 0.076 ms | 0.068 ms | 1.12× |
| `power>4` | 0.080 ms | 0.072 ms | 1.11× |
| `t:creature power>3` | 0.101 ms | 0.087 ms | 1.16× |
| `t:creature` deep offset (5000) | 0.133 ms | 0.105 ms | **1.27×** |
| `t:creature` deep offset (15000) | 0.143 ms | 0.114 ms | **1.25×** |
| `t:creature power>3` deep offset (5000) | 0.053 ms | 0.037 ms | **1.42×** |
| `t:creature` (artwork) | 0.117 ms | 0.118 ms | 0.99× (mode-gated, by design) |
| `cmc<=6` (broad, discarded-for-broadness) | 0.406 ms | 0.411 ms | ~1.0× (no membership in hand, correctly declines) |
| `c:g t:creature cmc<=4` | 0.224 ms | 0.226 ms | ~1.0× (numeric_candidates sort dominates, see above) |
| `f:modern` / `o:draw` / `r:mythic` (advisory) | — | — | ~1.0×, unaffected |
| mixed exact+advisory (must never promote) | — | — | ~1.0×, unaffected |

All totals verified identical across every config. Real, consistent wins for
genuinely exact queries (single and compound predicates, card and printing
modes, and biggest at deep pagination offsets); correctly flat for broad/
advisory/mixed cases that must not promote.

**Step 2 (Part A: permuted bitmap + popcount-skip order phase) — done**
- Inverse permutations for all six `SortPermutations` columns (`cmc`, `power`,
  `toughness`, `edhrec`, `cubecobra`, `name` — no reason to defer `name`,
  same mechanism). **Correction to the original issue's design** (caught
  before writing any code, by re-deriving the sort-key construction rather
  than trusting the "desc = n-1-pos, no second table" shortcut): this
  codebase's tie-breakers (`edhrec_rank`, then `prefer_score`) never flip sign
  with the primary column's direction (`build_sort_permutations`'s `perm`
  closure negates only the primary key for `descending`), so cards tied on
  the primary value keep *identical* relative order in both the ascending and
  descending permutations. Reversing the ascending inverse (`n-1-pos`) would
  also reverse that internal tied-group order, which is wrong whenever the
  sort column has ties — the common case, not the exception (`cmc=3` alone is
  7,602 of 31,508 cards). Storing both inverse permutations explicitly, same
  as the forward tables already do, costs another ~1.26 MB (matching the
  forward tables' own size) — trivial against a >70 MB archive, and correct
  regardless of tie clustering. Archive version bump.
- New `run_query_streamed_popcount`: scatters the plane bitmap through the
  inverse permutation, then works in word space — total = popcount, skip =
  running word-popcount sum to the boundary word, emit = walk set bits from
  there mapping back through the forward permutation.
- **Scope, deliberately narrower than the original plan**: engages only when
  `unique=card` and the filter fully consumed to `FilterExpr::True` (plane
  bitmap IS the exact match set — colors/types/legality, any selectivity).
  Compound exact filters that didn't fully consume (`t:creature power>3`,
  residual = `power>3`) and `unique=printing`/`artwork` still use Step 1's
  path unchanged — extending to those is a reasonable fast-follow, not
  required to deliver the core win. Also: `cmc<=6` alone does **not** speed up
  under Step 2 either, for the same reason noted under Step 1 — `cmc` has no
  bitmap representation without the separate numeric-range-planes work, so
  there's nothing to scatter without first paying the O(match count) cost
  Step 2 exists to eliminate.
- Unchanged: `rarity`/`usd` orderbys (no permutation, gathered path already),
  `STREAM_MIN_MATCHES` gate, emission/tiebreak semantics — verified via a
  direct equivalence test against the non-popcount path, not just inspection.

**Step 2 results** (same protocol as Step 1, `min_ms`, machine quiesced):

| query | Step 1 | Step 2 | speedup (Step 2 alone) | cumulative vs. pre-#634 |
|---|---|---|---|---|
| `t:creature` (offset 0) | 0.096 ms | 0.060 ms | **1.59×** | **2.09×** (0.126 ms baseline) |
| `t:creature` (offset 5,000) | 0.104 ms | 0.062 ms | **1.67×** | **2.16×** (0.134 ms baseline) |
| `t:creature` (offset 15,000) | 0.113 ms | 0.060 ms | **1.89×** | **2.38×** (0.143 ms baseline) |
| `c:g` (offset 0) | 0.065 ms | 0.053 ms | 1.23× | — |
| `c:g` (offset 3,000) | 0.074 ms | 0.054 ms | **1.37×** | — |
| `t:instant` | 0.056 ms | 0.049 ms | 1.16× | — |
| `t:creature power>3` (compound, out of scope) | 0.087 ms | 0.088 ms | ~1.0× (expected) | — |
| `t:creature` printing/artwork (out of scope) | 0.090/0.117 ms | 0.093/0.118 ms | ~1.0× (expected) | — |
| `cmc<=6`, advisory/mixed/control | — | — | ~1.0× (expected, no bitmap available) | — |

The headline result: at pre-#634 baseline, `t:creature` cost **grew with page
depth** (0.126 → 0.134 → 0.143 ms across offset 0/5,000/15,000 — a ~13%
increase). Under Step 2 it's **flat** (0.060 → 0.062 → 0.060 ms) — the exact
"deep pagination becomes O(words)" property Step 2 was built for, and not
plausibly noise given the specific shape (flat, not just lower). All totals
verified identical across every config throughout.

### Step 1.5 (proposed, not yet designed): per-child exact-node elision

Current Step 1 is all-or-nothing: `power>3 AND o:draw` gets zero benefit even
though `power>3` alone is exact, because the residual as a whole isn't (one
non-exact child taints it). A more general version: instead of a single
whole-residual exactness bool, have the And-composition (which already
tracks `every_child_included` per child) identify *which* children are
individually card-space-exact, and rebuild a reduced `FilterExpr` with those
children replaced/dropped — mirroring what `split_planes` already does for
`compile_plane`-able children, generalized to numeric ranges/`ExactName`/
complete-index lookups. `card_pass` then only evaluates the genuinely
advisory remainder. Real, separate tree-surgery work (narrow_rec today only
computes candidate sets, never touches the `FilterExpr` tree) — sequence
after Step 1 lands and is validated, not bundled in.

### Tasks

- [ ] Step 1: expose `tight`, combine with plane exactness, skip `card_pass`
      when `all_match` is structurally known
- [ ] Step 1 bundled: scatter-before-intersect for broad Vec residuals
- [ ] Step 1 tests: parity vs brute force for every exact/advisory shape;
      explicit test that mixed exact+advisory never promotes and still
      verifies the advisory part per candidate
- [ ] Step 1 benchmark + regression check, iterate
- [ ] Step 2: inverse permutations, archive version bump, scatter pass,
      popcount-skip walk, weighted set-bit walk for printing/artwork
- [ ] Step 2 equivalence tests vs the counts-buffer path (uniques × orderbys
      × directions × offsets, deep offsets, tie blocks)
- [ ] Step 2 benchmark + regression check, iterate
- [ ] Final report
