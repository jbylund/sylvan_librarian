# Engine: fast path for broad printing-space range queries

Status: design drafted 2026-07-14, no GitHub issue yet — file once the crossover is measured
and a direction is picked. Surfaced investigating why `usd<50` costs ~0.4–1 ms (see
[00629-engine-artwork-group-id-bitmasks.md](done/00629-engine-artwork-group-id-bitmasks.md)'s
"Expected (honest)" section: "the ~97k price compares still dominate") — but the mechanism
isn't price-specific. Every `PrintingRangeIndex`-backed field shares it.

## Prerequisite: price needs to be exact, not widened-and-deferred

**Status: done**, shipped ahead of the rest of this doc — as an integer-cents migration, not the
narrower `f32`-preserving fix originally attempted in
[PR #687](https://github.com/jbylund/sylvan_librarian/pull/687) (closed unmerged, superseded).
History kept here because both bugs, and why the fix needed to go deeper than either one, are
worth remembering.

### Bug A (found in review of #687): `price_bounds`'s own cents conversion wasn't exact

`price_bounds` shared one bound between `Lt`/`Le` and between `Gt`/`Ge`, deferring the
strict/non-strict distinction to a verify pass. The fix computed `value * 100.0` before
floor/ceil — but that multiplication is itself a new floating-point operation, not a lossless
relabeling: `0.28_f64 * 100.0 == 28.000000000000004`, `0.57_f64 * 100.0 == 56.99999999999999`.
For ~a quarter of two-decimal dollar amounts this silently shifted the bound by a whole cent,
producing a real false negative (`Ge(0.28)` against a printing priced at exactly $0.28, dropped
from narrowing entirely — not masked by verification, since a card whose only qualifying
printing gets wrongly excluded from the narrowed set is never visited at all). Patched with a
`snap_to_nearest_cent` epsilon-correction before flooring/ceiling.

### Bug B (found stress-testing beyond the review): verification has its own, independent, unrelated mismatch

Stress-testing the fix for Bug A across 20 random seeds × real generated prices turned up
something else entirely, pre-existing on `main`, untouched by either the original code or the
Bug A fix: `field_num` (`filter.rs:88-104`) reads a stored price as `f32` and widens it to `f64`
for comparison (`x as f64`), but `NumExpr::Const` never demotes the query threshold through the
same lossy step (`NumVal::Known(*v)`, full `f64` precision). These are two different-precision
representations of "the same" decimal, and they are essentially **never** bit-identical:
`7.22_f32` widened back to `f64` is `7.21999979019165`, not `7.22`. So `usd=7.22` essentially
never matches a card actually priced at $7.22, and `Ge`/`Le` are wrong at the exact boundary the
same way — independent of narrowing entirely, since this is in the verify path that always runs
regardless of what narrowing produces. Confirmed identically on a clean `main` worktree, so this
predates both the original code and #687's fix; the `price_bounds` diff never touches it.

**Why patching Bug B in place is the wrong shape of fix.** The generic path (`NumExpr::eval` →
`NumVal::Known(f64)` → `cmp(op, a, b)`) is one of *two independent* implementations of "does this
price satisfy this predicate" — the other being `price_bounds`, used for narrowing. Two
independent encodings of the same rule quietly disagreeing is exactly Bug B's shape; patching the
comparison to happen to agree with `price_bounds` today doesn't prevent a *third* independent
encoding from drifting out of sync with both, next time someone touches either one.

### Root cause of both bugs: storing price as a lossy `f32` approximation of an exact quantity

Checked against real data: every stored price is genuinely cent-precise
(`abs(price_usd - round(price_usd*100)/100.0) > 0.001` matches **0** of 81,540 priced printings),
max price is $5,142.02, and f32's ULP at that magnitude (~$0.0006) is 16× finer than a cent.
Prices are not a continuous quantity that happens to usually land on cents — they *are* integer
cents, always, and storing them as `f32` dollars introduces a lossy step (the `f32` truncation)
that doesn't need to exist. `cmc`/`power`/`toughness`/`rarity_int`/`collector_number_int` never
had either bug, because they're stored as exact small integers (`u8`/`u16`) — `as f32 as f64` is
lossless for them. Price/eur/tix are the only numeric fields where the storage type itself loses
information before comparison ever happens.

## Shipped: store price as integer cents

Changed `price_usd`/`price_eur`/`price_tix` from `Option<f32>` (dollars) to `Option<u32>`
(cents) — same 4-byte footprint, no storage penalty. This removes the lossy step both bugs
depended on, rather than patching around either one:

- **`PrintingRangeIndex` simplified**: cents *are* the sort key (a natural, monotonic `u32`) —
  `f32_sort_bits` no longer used for these fields at all, no encoding step needed
  (`build_range_index(&printings, |p| p.price_usd)`, direct).
- **`price_bounds` deleted outright**, replaced by a thin closure reusing `int_range_bounds`
  directly — the exact same shape `collector_number`'s own closure already had: `int_range_bounds(op, snap_to_nearest_cent(*v * PRICE_CENTS_PER_DOLLAR))`,
  matched on `None`/`Some((lo, hi))` identically to the `cn` closure right next to it.
  `snap_to_nearest_cent` (against `*100.0`'s own floating-point noise — the exact multiplication
  that caused Bug A) is still needed and still lives here; it's the one place a `*100.0`
  conversion of an arbitrary `f64` threshold still happens.
- **`field_num` fixes Bug B directly, with no other changes anywhere**: a new `known_cents`
  helper, `NumVal::Known(f64::from(cents) / 100.0)` instead of widening a lossy `f32`. `722.0 /
  100.0` and `float("7.22")` are bit-identical (both are single, non-lossy roundings of the same
  rational number) — so the field side and `NumExpr::Const` (untouched) now agree exactly, and
  the fully generic `cmp()` in `tri()`'s `NumericCmp` arm needed **no per-field special case at
  all**. Verification and narrowing don't share an implementation and don't need to — they're
  each independently exact once the only lossy step is gone.
- **Ingest**: new `opt_price_cents` parses the JSON price and rounds to the nearest cent once
  (`(dollars * 100.0).round() as u32`), replacing `opt_f32` for these three fields.
- **API-facing serialization returns dollars, now exactly**: `("price_usd", ...)`'s field-export
  closure divides cents back to `f64` dollars — `api/tests/test_engine_unit.py::test_price_usd_matches_prefer_ordering`
  (`price_usd == pytest.approx(1.47)`) still passes, and callers now see the *true* price (e.g.
  `7.22`) instead of the old lossy `f32` approximation (`7.21999979019165` promoted to `f64`).
- **Archive format version bumped** (`20260724` → `20260725`) — this changed the *semantic
  meaning* of on-disk bytes (dollars vs. cents), not just their size.
- Sort/prefer scoring (`Prefer::UsdLow`/`UsdHigh`, `SortCol::PriceUsd`): `Prefer` converts to
  exact dollars (`f64::from(u32::from(*v)) / 100.0`); `SortCol`'s generic `f32`-based sort-key
  path uses raw cents directly (order-preserving either way, and cents fit exactly in `f32`'s
  24-bit mantissa up to the real max price, so no dollars conversion needed there at all).

**Verified, not just argued** — three permanent regression tests in `tests.rs`, corrected once
during review (an earlier draft of this doc named two tests, ported from the design/prototype
work on the now-closed #687, that never actually made it into this branch's `tests.rs`; caught
in review of this PR, since #687's `f32_sort_bits`-based test doesn't even apply to this design —
cents are the raw sort key now, no `f32_sort_bits` encoding involved for price at all):

- `price_narrowing_bound_matches_direct_comparison_on_and_off_grid` — the actual mechanism now in
  play, `int_range_bounds(op, snap_to_nearest_cent(v * 100.0))` (the `price` closure's exact
  composition, since standalone `price_bounds` was deleted), checked against direct floating
  comparison across 13 thresholds (cent-aligned and deliberately off-grid/arithmetic-derived, incl.
  the review-caught `0.28`/`0.57` repro values) × 5 operators × ~13,900 sampled real prices, zero
  disagreements.
- `price_narrowing_and_verification_are_exact_at_the_boundary` — `Lt` excludes, `Le`/`Ge`/`Eq`
  include, at a real boundary price, both in narrowing and in end-to-end verification.
- `price_comparison_matches_exact_value_not_lossy_f32_widening` — the literal `$7.22` repro from
  the Bug B writeup.

Beyond the unit tests, re-ran the exact stress test that originally surfaced Bug B — 20 random
seeds × up to 30 real generated prices sampled as query thresholds × 5 operators, comparing the
engine against `test_engine_property.py`'s reference oracle — before the fix this failed on
essentially every case (`Eq` universally, `Ge`/`Le` at every sampled boundary); after, **0
failures out of 3,000 checks** (`unique=printing`) **and 0 failures out of 4,000** more
(`unique=card`/`artwork`). `cargo test` (debug + release): **116 passed**. `pytest` on
`test_engine_unit.py`/`test_engine_property.py` (including the 250-seeded-query differential
suite against a reference oracle sharing no code with the engine): 159 passed. `cargo clippy`:
37 warnings, diffed by file:line against `main` — identical set, just shifted by this change's
added lines.

This makes `price_usd`/`eur`/`tix` genuinely `tight` in `range_narrowed` (the `exact` param is
now `true` at the `price` closure's `int_range_bounds` call, same as `collector_number`) — same
category as `collector_number`/`released_at`. `tix`/`eur` inherit the fix automatically once
#638 indexes them (same `Option<u32>` cents type, same ingest/verify paths, already updated
here). `tight_narrow_space` still deliberately declines price — that's a separate
composition-safety question (does the `Not`-arm's complement correctly exclude NULL-priced
printings, which are simply absent from the index?) deferred to the fastpath work below, not a
side effect of this fix.

### Tried and reverted: skipping `field_num`'s division by binding price to cents

`field_num` still divides cents to dollars (`/100.0`) on every printing evaluated, since the
query-side `Const` stays in dollars. Tried removing that division by having `FilterExpr::bind`
rewrite a bare `usd`/`eur`/`tix` `Field`-vs-`Const` comparison's `Const` to cents once per query,
so `field_num` could compare raw cents directly. Measured win was small (~2-3%) and narrow (only
that one exact shape). Shipped, then reverted after code review found a real correctness bug:
the rewrite only recognized that one shape, so `usd+1<power` (price inside `NumExpr::Arith`) and
`usd<cmc` (price compared directly against another `Field`, no `Const` at all) left the
query-side operand in dollars while a modified `field_num` returned cents unconditionally —
silently off by 100x. A type-level fix was possible (a distinct `NumExpr::PriceCents` variant,
constructed only for the verified-safe shape) but made two logically identical queries
(`usd<5.00` vs. `usd+0.01<5.01`) take inconsistently optimized paths depending on phrasing — the
same shape-dependent fragility that caused the bug, and worth avoiding rather than papering over.
Not worth re-attempting without a fundamentally different approach (e.g. a real compile-time
unit-checked representation, not a bind-time rewrite keyed on syntactic shape). Full history in
[#690](https://github.com/jbylund/sylvan_librarian/pull/690).

## Problem

`usd<50` matches 80,527 of 97,206 printings (83%) — genuinely broad, same shape as the
`cmc`/`power`/`toughness` queries [00655-engine-numeric-range-planes.md](done/00655-engine-numeric-range-planes.md)
fixed with one-hot-interior + cumulative-boundary bitplanes (`cmc<=6`: 0.405 → 0.067 ms, 6.1×).
That technique doesn't transfer: it needs a small, enumerable value space (~13–17 for
`cmc`/`power`/`toughness`; price has 4,133 distinct values), *and*, more fundamentally, `cmc`/
`power`/`toughness` are card-invariant (one value per card, so a plain per-card plane bit is
exact), while price is printing-varying — `usd<50` for `unique=card` means "*some* printing is
under $50," an existential predicate over printings, the same shape legality's `∃p: satisfies(p)`
problem is ([00680-engine-existential-plane-generalization.md](done/00680-engine-existential-plane-generalization.md)).
[local-engine-printing-varying-plane-repair-pattern.md](local-engine-printing-varying-plane-repair-pattern.md)
names this as the case the plane escape hatch can't cover: an unbounded parameterized threshold
has no finite set of precomputable existence projections. The prerequisite fix above doesn't
change that — it makes the *narrowing* exact, not the *existence* projection precomputable.

**This is not just a price problem.** Every field routed through `range_narrowed` shares the same
broadness-discard/fallback cost floor (`card_engine/src/lib.rs` — grep `range_narrowed(&indexes\.`):
`price_usd`, `collector_number`, `released_at` (date/year) today, plus `tix`/`eur` once #638
lands. With the prerequisite fix, all of them are `tight`. **The broadness discard throws the
candidate set away regardless**: for a bare predicate (no plane to AND against), `run_query`
converts the candidate set to card ids and drops it if it covers ≥87.5% of all cards
(`lib.rs:3740-3743`), falling back to a raw per-card `card_pass` scan even though the discarded
set was exact.

## Idea 1: walk the order-by permutation, verify inline, stop at `limit`

`range_narrowed`'s two `partition_point` binary searches already compute `k = e - s` for free
(`lib.rs:1990-1992`) before any narrowing decision is made. Instead of materializing or
discarding a candidate set, walk the existing per-orderby permutation table (built for #634)
and test each candidate for set membership inline, stopping once `limit` matches are found.

- **Cheap when the predicate is broad**: expected candidates visited ≈ `limit / match_rate` —
  for `usd<50` at 83%, a 20-row page costs ~24 candidate checks. Close to instant.
- **Unbounded worst case.** If the order-by column is unrelated to the predicate field, there's
  no seek target, and a low-selectivity predicate with unlucky clustering could blind-walk most
  of the corpus before finding `limit` matches — worse than today's baseline. Even the
  *aligned* case (`released_at>2026-06-01 order by released_at`) is pathological if the walk
  starts at the wrong end: ascending order puts the matches at the far end, so a naive
  front-to-back walk visits almost everything before the first hit.
- **`total` for `unique=card`/`artwork` needs `oracle_id` dedup, not just `k` — and this may
  cost as much as idea 2's whole approach.** `k` alone only works directly for `unique=printing`
  (no dedup needed, `total = k` for free from the binary search — idea 1's "close to instant"
  claim is unconditionally true there). For `unique=card`/`artwork`, an *exact* `total` means
  deduplicating every matched printing by `oracle_id` — touching all `k` matches at least once,
  the same O(k) idea 1 exists to avoid. If that cost is unavoidable for `unique=card` regardless
  of page-fetch strategy, idea 1 may not actually beat idea 2 there — idea 2 pays that same O(k)
  once to build the bitmap and gets offset-independent paging as a bonus for it. **This makes
  `unique` mode (`printing` vs. `card`/`artwork`) a fourth crossover axis, not a detail** — it
  may determine which idea is even viable before selectivity/alignment/clustering matter at all.

  `cards_of_printings` (`lib.rs:2392-2407`) dedupes exactly this way, but it's a *batch* operation
  over a complete, sorted `Vec`/bitmap — reusing it directly would mean materializing the whole
  matched set upfront, exactly what idea 1 exists to avoid. Idea 1's walk discovers matches one at
  a time, in permutation (order-by) order, not printing-id order, so it needs an *incremental*
  version instead: a running card-space "seen" bitmap, and per matched printing, look up its card
  id and test-and-set a bit (new bit set → a newly-counted distinct card; already set → skip).
  That per-printing card lookup can't use `cards_of_printings`' own broad-k trick
  (`printing_bits_to_card_bits`'s monotone cursor needs ascending printing-id order, which a
  permutation walk doesn't provide) — it needs the direct-array lookup instead. **Landed** (see
  [00690-engine-direct-projection-arrays.md](00690-engine-direct-projection-arrays.md), merged via
  [#690](https://github.com/jbylund/sylvan_librarian/pull/690)), so idea 1's incremental per-match
  card lookup can use `printing_to_card` directly. Still an open, measurement-shaped question
  (kernel benchmark, not end-to-end timing) whether this incremental floor is cheap enough to make
  `unique=card` competitive with `unique=printing` — the direct array being on `main` now is what
  makes that measurement fair.

## Idea 2: scatter the exact narrowed set into a bitmap, feed the existing popcount-skip path

With the prerequisite fix, narrowing is already exact for all of these fields — there's nothing
to verify. Project/scatter the narrowed printing-space set to a card-space existence bitmap and
feed it into `run_query`'s existing plane-eligible streamed-popcount dispatch (`lib.rs:3680`),
same as a compiled plane.

Feeding the result into `run_query_streamed_popcount` still needs more than an eligibility
tweak — that function's existential row-selection (`plane_expr_is_existential` +
`eval_plane_expr_for_printing`, built for legality) is tightly coupled to `PlaneExpr`, which
none of these fields compile to today. **Decided: extend the Y-predicate/existential-plane
framework** (#680) with a new leaf rather than duplicate its row-selection logic outside it —
price genuinely is a per-printing predicate, the same shape format/rarity/border already are.

**This row-selection work is not price-specific — `collector_number` and `released_at` are also
printing-varying fields.** A card's printings have different collector numbers and release
dates, same as different prices. Under `unique=card`/`artwork`, *any* printing-varying field
needs to pick which specific matching printing to emit — that's inherent to varying by printing
at all, not to price's semantics in particular. So "prove the mechanism on a tight field first
isolates the crossover question from the row-selection work" (see Plan) is only fully true for
`unique=printing` — there, each printing is its own row, no selection needed at all. For
`unique=card`/`artwork` on *any* of these three fields, the same `PrintingRangeBits` mechanism
is needed. Treat row-selection as one piece of shared work across `price`/`collector_number`/
`released_at` together, not something deferred specifically until price's turn.
Tracing `PlaneExpr::Bits` (planes.rs:436-443, `eval_plane_expr_for_printing`,
`plane_expr_is_existential`) shows this is a contained addition, not a framework rewrite — and
it's simpler than it first looked, now that narrowing is exact:

- `Bits` already supports "compute a card bitmap once per query, clone it into the plane tree"
  (the oracle-word-index dense-dictionary precedent), but it's card-invariant by design —
  `eval_plane_expr_for_printing`'s `Bits` arm checks the card id, and `plane_expr_is_existential`
  hard-codes `Bits => false`. The needed sibling variant is just **two precomputed bitmaps**, no
  live evaluation at all:

  ```rust
  PlaneExpr::PrintingRangeBits { card_bits: Vec<u64>, printing_bits: Vec<u64> }
  ```

- `eval_planes`: reads `card_bits` exactly like `Bits` does today (the card-level existence
  answer, already exact courtesy of the prerequisite fix).
- `plane_expr_is_existential`: `true` for this variant (unlike plain `Bits`).
- `eval_plane_expr_for_printing`: a bit test against `printing_bits` for this specific printing —
  no field/op/threshold, no floating-point comparison, just membership in the already-exact
  narrowed set.
- `compile_plane`'s `NumericCmp` arm, for printing-varying fields only (`cmc`/`power`/`toughness`
  keep their existing #655 arm): compute both bitmaps at query time via `range_narrowed`, wrap
  them in this variant — same "once per query" cost model the oracle-word-index `Bits` case
  already established.

- **Fixed cost regardless of `limit`/offset**: O(k) to build the bitmaps, then O(words) to select
  any page — same offset-independence #634 Step 2 built for plane-exact filters. Wins on deep
  pagination and on reuse (AND against a plane in a compound query). Predictable worst case:
  never worse than O(k), full stop.
- **Wasteful for the common case** — pays O(k) even for a 20-row first-page request that idea 1
  would answer in ~24 checks.

## The crossover needs measurement, not a guess

Every existing adaptive guard in this engine (`AND_SKIP_THRESHOLD`, `MAX_NARROW_FRACTION`/
`NARROW_FLOOR`, `MAX_UNION_FRACTION`) was derived from a benchmark sweep, not analysis — see
[00647-engine-cost-guard-calibration.md](done/00647-engine-cost-guard-calibration.md). This
decision needs three axes:

1. **Match rate** (`k/n`).
2. **Predicate/order-by field alignment** — same field (seekable directly) vs. unrelated field
   (blind walk, no seek target).
3. **For the aligned case, direction vs. clustering** — does the naive walk start at the
   matching end, or does it need to seek first?
4. **`unique` mode** (`printing` vs. `card`/`artwork`) — not a speed difference, a viability
   question. `unique=printing` needs no dedup (`total = k` free); `unique=card`/`artwork` needs
   an exact card-level `total`, which may cost O(k) regardless of page-fetch strategy (see idea
   1's `total` note above). If so, idea 1 and idea 2 aren't competing on speed for that mode,
   they're both paying the same floor and idea 2's offset-independence is pure upside.

The adversarial cell — selective predicate, unrelated order-by, unlucky clustering — is where
idea 1's actual worst case needs to be measured against today's baseline. If it's worse there,
that bounds how unconditionally idea 1 can be used. With the prerequisite fix landed, there's no
fifth (tight/loose) axis to worry about — every field behaves identically there.

## Plan

- [x] Ship the price exactness fix standalone (see Prerequisite above) — landed as the
      integer-cents migration in [#688](https://github.com/jbylund/sylvan_librarian/pull/688).
- [x] Ship `printing_to_card` standalone first (see
      [00690-engine-direct-projection-arrays.md](00690-engine-direct-projection-arrays.md)) —
      load-bearing for idea 1's incremental per-match card check, neutral to idea 2. Landed via
      [#690](https://github.com/jbylund/sylvan_librarian/pull/690); this doc's crossover-axis-4
      baseline numbers (not yet measured) should be taken against `main` post-#690.
- [ ] Prove the fast-path mechanism on `unique=printing` for a tight field first (`released_at`
      or `collector_number`) — this is the case that genuinely isolates the crossover question
      from row-selection (no picking-a-printing problem at all when each printing is its own
      row). Does *not* by itself resolve `unique=card`/`artwork` — see the next item.
- [ ] Resolve the `unique=card`/`artwork` `total` question (crossover axis 4) before trusting
      idea 1's cost model there: measure whether an exact card-level `total` can be had for less
      than O(k), or whether `unique=card` always pays that floor regardless of page-fetch
      strategy. This may collapse the idea-1-vs-idea-2 choice for that mode entirely.
- [ ] Sketch idea 1 as a real path: `k`-from-binary-search, order-by-permutation walk with
      inline membership check, early stop at `limit`.
- [ ] Sketch idea 2: `PlaneExpr::PrintingRangeBits`, wired into `compile_plane`/`eval_planes`/
      `plane_expr_is_existential`/`eval_plane_expr_for_printing` — shared across
      `price`/`collector_number`/`released_at`, not price-specific, once `unique=card`/`artwork`
      row-selection is in scope for any of them.
- [ ] Build a sweep harness across all four crossover axes (shape of `bench_cost_guards.py` /
      `build_guard_corpus.py`), covering `price_usd`, `released_at`, and `collector_number`, plus
      at least one deliberately-adversarial synthetic case (mismatched order-by field) and both
      `unique` modes.
- [ ] Calibrate the guard from measurement (or confirm one design dominates and no guard is
      needed).
- [ ] Decide the `Not`-arm/`tight_narrow_space` composition-safety question (deferred from the
      Prerequisite section to here) — either bring it into scope (needed for `-usd>8`-shaped
      queries, a real fragment in `test_engine_property.py`'s own suite) or explicitly defer it
      again to a third, later piece of work, with a stated reason rather than silently dropping it.
- [ ] Acceptance: `usd<50`, a broad `released_at`/`collector_number` query, and the adversarial
      case all improve or stay flat vs. baseline for at least one `unique` mode; no regression on
      the existing #634/#655 exact paths; passes (and likely extends, given this exact class of
      change already produced two independent bugs in the price prerequisite work)
      `test_engine_property.py`'s differential suite against the reference oracle — a performance
      delta alone is not sufficient to call this done.

## Related

- [00690-engine-direct-projection-arrays.md](00690-engine-direct-projection-arrays.md) —
  prerequisite `printing_to_card` array, load-bearing for idea 1's per-match card check.
- [00655-engine-numeric-range-planes.md](done/00655-engine-numeric-range-planes.md) — the
  analogous fix for `cmc`/`power`/`toughness`; doesn't transfer (card-invariant, not existential).
- [00629-engine-artwork-group-id-bitmasks.md](done/00629-engine-artwork-group-id-bitmasks.md) —
  where the `usd<50` cost was first flagged as a floor, not fixed.
- [00634-engine-permuted-bitmap-order-phase.md](done/00634-engine-permuted-bitmap-order-phase.md)
  — the popcount-skip machinery idea 2 would extend.
- [00680-engine-existential-plane-generalization.md](done/00680-engine-existential-plane-generalization.md)
  — the existential-predicate framework `PrintingRangeBits` extends to numeric printing fields.
- [00647-engine-cost-guard-calibration.md](done/00647-engine-cost-guard-calibration.md) — the
  calibration-from-measurement precedent this crossover should follow.
- [local-engine-printing-varying-plane-repair-pattern.md](local-engine-printing-varying-plane-repair-pattern.md)
  — names price's exact disqualifying shape ("a hypothetical printing-varying numeric field...
  `> 3.7` and `> 3.71` are different, un-precomputable existence projections").
- [local-engine-probe-before-and-skip.md](local-engine-probe-before-and-skip.md) — the same
  "the binary search already gives you `k` for free" observation, in the AND-skip context.
- #638 — `tix`/`eur` have no range index at all yet; the same fast path (and the prerequisite
  exactness fix) should cover them once they do.
