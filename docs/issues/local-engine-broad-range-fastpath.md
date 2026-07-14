# Engine: fast path for broad printing-space range queries

Status: design drafted 2026-07-14, no GitHub issue yet — file once the crossover is measured
and a direction is picked. Surfaced investigating why `usd<50` costs ~0.4–1 ms (see
[00629-engine-artwork-group-id-bitmasks.md](done/00629-engine-artwork-group-id-bitmasks.md)'s
"Expected (honest)" section: "the ~97k price compares still dominate") — but the mechanism
isn't price-specific. Every `PrintingRangeIndex`-backed field shares it.

## Prerequisite: price needs to be exact, not widened-and-deferred

**Status: superseded, redoing from scratch — see "Revised plan: store price as integer cents"
below.** [PR #687](https://github.com/jbylund/sylvan_librarian/pull/687) shipped a first attempt
(floor/ceil-in-cents bounds, keeping `f32` storage) that turned out to fix only one of two real
bugs. History kept here because both bugs, and why the fix needed to go deeper than either one,
are worth remembering.

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

## Revised plan: store price as integer cents

Change `price_usd`/`price_eur`/`price_tix` from `Option<f32>` (dollars) to `Option<u32>` (cents)
— same 4-byte footprint, no storage penalty. This removes the lossy step both bugs depend on,
rather than patching around either one:

- **`PrintingRangeIndex` simplifies**: cents *are* the sort key already (a natural, monotonic
  `u32`) — `f32_sort_bits` disappears entirely for these fields, no encoding step needed.
- **`price_bounds` still needs floor/ceil-in-cents** (narrowing still has to convert a
  user-typed `f64` dollar threshold into the integer-cents domain to bound a slice of the
  now-cents-keyed index — that conversion is still a multiplication, still needs the
  `snap_to_nearest_cent`-style correction), but can likely become a thin wrapper around
  `int_range_bounds` directly, rather than a bespoke function — `collector_number` already
  solves exactly this "arbitrary `f64` threshold against an exact small-integer domain" problem.
- **`field_num` fixes Bug B directly, with no other changes anywhere**: `known(p.price_usd.as_ref().map(|v| u32::from(*v) as f64 / 100.0))`
  instead of widening a lossy `f32`. `722.0 / 100.0` and `float("7.22")` are already proven
  bit-identical (both are single, non-lossy roundings of the same rational number) — so the field
  side and `NumExpr::Const` (untouched) now agree exactly, and the fully generic `cmp()` in
  `tri()`'s `NumericCmp` arm needs **no per-field special case at all**. Verification and
  narrowing don't need to share an implementation to stay consistent once the only lossy step is
  gone — they're each independently exact.
- **Ingest**: parse the JSON price and round to the nearest cent once (`(v * 100.0).round() as u32`),
  not per-query.
- **API-facing serialization must still return dollars**: `api/tests/test_engine_unit.py::test_price_usd_matches_prefer_ordering`
  asserts `price_usd == pytest.approx(1.47)` — the public contract doesn't change, only the
  internal representation.
- **Archive format version bump required** — this changes the *semantic meaning* of on-disk
  bytes (dollars vs. cents), not just their size; a stale archive would silently misread as
  wildly wrong prices without it.
- Sort/prefer scoring (`Prefer::UsdLow`/`UsdHigh`, `SortCol::PriceUsd`): minor conversions:
  order-preserving either way, so sorting on raw cents directly is also an option, not just a
  dollars conversion.

### Plan

- [ ] Branch fresh from `main` (not on top of #687 — this supersedes it) and redo the whole
      thing as the integer-cents migration, informed by everything above.
- [ ] Struct/ingest/archive-version changes for `price_usd`/`price_eur`/`price_tix`.
- [ ] `price_bounds` as a thin wrapper (or direct reuse) of `int_range_bounds`-shaped logic.
- [ ] `field_num`'s price arms switched to exact cents/100.0 division.
- [ ] Every test touched by #687 re-verified against the new representation, plus new
      end-to-end tests proving `Eq`/`Ge`/`Le` now agree with narrowing at the exact boundary —
      not just that narrowing alone is exact.
- [ ] Decide #687's fate once the new branch is ready: close unmerged, since the new branch's
      diff against `main` supersedes it entirely.

**Verified, not just argued** — both checks are now permanent regression tests in `tests.rs`:
`f32_sort_bits_distinguishes_every_cent_up_to_50k` proves zero sort-bits collisions across every
adjacent cent pair from $0.01 to $50,000 (10× the real max price), and
`price_bounds_matches_direct_comparison_on_and_off_grid` proves zero disagreements with direct
floating comparison across 11 thresholds (cent-aligned and deliberately off-grid/
arithmetic-derived ones like `49.998`, `0.005`, `12.3456789`) × 5 operators × ~13,900 sampled
real prices. This is provably exact over the entire realistic range, not empirically-probably-fine.

This makes `price_usd` genuinely `tight` (`range_narrowed`'s `exact` param flipped to `true` at
its one `price_usd` call site) — same category as `collector_number`/`released_at`. Shipped as
its own tiny, purely-mechanical PR, independent of everything below. `tix`/`eur` should inherit
the identical fix once #638 indexes them (same cent-granularity argument). `tight_narrow_space`
still deliberately declines price — that's a separate composition-safety question, deferred to
the fastpath work below.

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
- `total` for `unique=card` needs `oracle_id` dedup, not just `k` — `k` alone only works
  directly for `unique=printing`.

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

The adversarial cell — selective predicate, unrelated order-by, unlucky clustering — is where
idea 1's actual worst case needs to be measured against today's baseline. If it's worse there,
that bounds how unconditionally idea 1 can be used. With the prerequisite fix landed, there's no
fourth (tight/loose) axis to worry about — every field behaves identically here.

## Plan

- [ ] Ship the `price_bounds` exactness fix standalone (see Prerequisite above) — already
      verified, just needs the Rust change + a `tests.rs` regression test.
- [ ] Prove the fast-path mechanism on a tight field first (`released_at` or
      `collector_number`, already tight today) — isolates the crossover question from the
      `PlaneExpr` row-selection work.
- [ ] Sketch idea 1 as a real path: `k`-from-binary-search, order-by-permutation walk with
      inline membership check, early stop at `limit`, dedup-aware `total` for `unique=card`.
- [ ] Sketch idea 2: `PlaneExpr::PrintingRangeBits`, wired into `compile_plane`/`eval_planes`/
      `plane_expr_is_existential`/`eval_plane_expr_for_printing`.
- [ ] Build a sweep harness across the three crossover axes (shape of `bench_cost_guards.py` /
      `build_guard_corpus.py`), covering `price_usd`, `released_at`, and `collector_number`, plus
      at least one deliberately-adversarial synthetic case (mismatched order-by field).
- [ ] Calibrate the guard from measurement (or confirm one design dominates and no guard is
      needed).
- [ ] Acceptance: `usd<50`, a broad `released_at`/`collector_number` query, and the adversarial
      case all improve or stay flat vs. baseline; no regression on the existing #634/#655 exact
      paths.

## Related

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
