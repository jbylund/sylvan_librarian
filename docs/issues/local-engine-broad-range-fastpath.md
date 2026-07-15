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

## Idea 1: walk the order-by permutation, verify inline, stop at `limit` — REJECTED

`range_narrowed`'s two `partition_point` binary searches already compute `k = e - s` for free
(`lib.rs:1990-1992`) before any narrowing decision is made. The original idea: instead of
materializing or discarding a candidate set, walk the existing per-orderby permutation table
(built for #634) and test each candidate for set membership inline, stopping once `limit` matches
are found.

**Rejected — its own premise doesn't hold.** The whole appeal was "cheap when the predicate is
broad *and aligned with the order-by column*: for `usd<50` at 83%, a 20-row page costs ~24
candidate checks." Trying to actually prove this (Plan: "prove the mechanism on `unique=printing`
for a tight field, `released_at` or `collector_number`") surfaced that the aligned case doesn't
exist for any of the three target fields, in the current system:

- `orderby_to_col` (`lib.rs:3311-3322`) and the API's `CardOrdering` enum
  (`api/enums.py:25-35`) don't recognize `released_at`/`collector_number` as order-by targets at
  all — anything unrecognized silently falls back to `edhrec_rank`. There is no way to even
  request `order by released_at` today.
- `price_usd` *is* a valid order-by target, but has no permutation table:
  `ArchivedSortPermutations::get`/`get_inv` (`lib.rs:1755-1780`) explicitly return `None` for
  `SortCol::PriceUsd`, because a card's price-sort position depends on which printing `prefer`
  selects — not fixed at index-build time. This isn't an oversight: #634's own design doc
  (`docs/issues/done/00634-engine-permuted-bitmap-order-phase.md`) scoped its permutation tables
  to "**card-level** sort column[s]" and states outright "rarity/usd orderbys ... unchanged."

So every real query against these fields hits idea 1's *unbounded worst case* (blind walk against
an unrelated order-by column, most commonly the default `edhrec_rank`) — the cheap, aligned case
this idea was chosen for is unreachable in practice. Building it would mean adding new order-by
targets and permutation mechanisms as a real prerequisite, a materially bigger and separate
project from what's scoped here.

**Considered and rejected a scoped-down rescue** for `unique=printing` specifically: since each
`PrintingRangeIndex` (`price_usd`, `collector_number`, `released_at`) is already sorted by
`(value, printing_idx)`, walking it directly — no new permutation needed — gives an exact,
offset-independent page for the aligned case, for that one mode. Real for `collector_number`/
`released_at` (no existing orderby semantics to preserve, so "index order = orderby order" is a
free, valid choice), but not free for `price_usd`: `order by usd`'s real, already-shipped tiebreak
is `(price, edhrec_rank asc, prefer_score desc)` (`sort_key_bits`, `lib.rs:3337-3355`), not
printing store order, so walking the raw index directly gets ties wrong whenever multiple
printings share a price — common at the cheap end this project cares about most. A bucket
approach (price values are already contiguous in the index; accumulate whole buckets until the
page is covered, re-sort only the boundary bucket by the true tiebreak) fixes this correctly, but
adds real edge-case cost (a pathologically large boundary bucket — e.g. many printings at the
store's price floor — degrades back toward O(k) anyway) for a win that **only ever applies to
`unique=printing`**. `unique=card`/`artwork` remain permanently blocked regardless: the card-level
sort key depends on which printing `prefer` selects, not fixed independent of the query, the same
reason #634 excluded rarity/usd from its own permutation tables — that's a property of the sort
key itself, not of how the walk order is produced. Given idea 2 already handles all three
`unique` modes and all three fields uniformly with no new orderby surface or bucket-size edge
cases, the narrower, edge-case-prone idea 1 rescue isn't worth the complexity. Decided not to
pursue idea 1 further, in any form; no crossover to calibrate against idea 2 as a result — idea 2
is simply the approach.

**`unique=card`/`artwork` total note kept for idea 2's benefit** (idea 1's own motivation for this
is now moot, but idea 2 needs the same fact): an exact `total` under `unique=card`/`artwork` means
deduplicating every matched printing by `oracle_id`. `cards_of_printings`
(`lib.rs:2392-2407`, see [local-engine-direct-projection-arrays.md](local-engine-direct-projection-arrays.md)
for its now-shipped direct-array projection) already does exactly this, size-adaptively, for
`Candidates::Printings` — the same dedup idea 2's own bitmap-build step needs, not new design.

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
printing-varying fields**, but they compile through entirely separate `FilterExpr` variants
(`DateCmp`/`YearCmp`, not `NumericCmp`/`NumField` — `NumField` has no `ReleasedAt` member at all),
so extending `PrintingRangeBits` to them is real, separate follow-up work with its own dedicated
`compile_plane` arms, not something this slice's `NumericCmp` wiring covers for free.

### Shipped: `PlaneExpr::PrintingRangeBits` for `price_usd`, `unique=card` only

Actual shape landed, differs from the original sketch in two ways found while implementing:

```rust
PlaneExpr::PrintingRangeBits { id: u64, card_bits: Vec<u64>, field: NumField, op: CmpOp, threshold: f64 }
```

- **No `printing_bits` bitmap.** The original sketch proposed a second precomputed bitmap for the
  per-printing check, reasoning it would avoid floating-point comparison at eval time. That
  reasoning no longer applies: post the integer-cents migration, price/collector_number/
  released_at are all plain integers on `Printing`, so `eval_plane_expr_for_printing`'s new arm
  just re-runs `filter.rs`'s own `cmp()` directly against `(field, op, threshold)` — one fewer
  independent encoding of "does this printing satisfy X" to keep in sync (exactly Bug B's shape).
- **`id: u64` for shared-witness tracking**, not reusing plane indices. `collect_existential_indices`
  (the `And`-composition safety check — `format:A AND r:mythic` can't compose via independent
  card-bitmap intersection, and neither can `usd<50 AND usd>10`: a card can have one printing
  under $50 and a different one over $10, satisfying both independently without any single
  printing satisfying both) needs a distinct identity per compiled leaf, including two range
  conditions on the *same* field at different thresholds. A monotonic counter starting above
  `PLANE_COUNT` gives each compiled leaf a guaranteed-unique id with no risk of colliding with a
  real plane index (a hash-based id was considered and rejected — collision risk, however small,
  is the wrong tradeoff for a correctness-critical check). Same-field range merging (folding
  `usd<50 AND usd>10` into one combined-bounds leaf, letting them safely share an id) is a real,
  deliberately deferred optimization, not required for correctness — declining to compose falls
  back to the always-correct residual `card_pass` path.
- **`-usd<X` needed its own dedicated negation arm** in `compile_plane_neg`, mirroring
  rarity/Legality exactly: `∃p: ¬(p.usd<X)` (recompute with the negated op), not `¬∃p: p.usd<X`
  (bit-complementing `card_bits`) — a card can satisfy both `usd<X` and `-usd<X` at once via
  different printings, so the two are not logically equivalent. Missing this was caught by the
  new tests before being caught in production.
- **Row-selection reachable only for `unique=card`; narrowing reachable for all three modes.**
  `split_planes`'s `unique_is_card` gate (the same one the original Legality carve-out
  established) declines to fold *any* existential leaf to a bare `True` residual for
  `unique=printing`/`artwork`, since `PrintingRangeBits` correctly returns `true` from
  `plane_expr_is_existential`. It now still returns the compiled plane for candidate narrowing in
  that case (see "`unique=printing`/`artwork`" section below), while `run_query`'s own
  `existential_plane` computation (feeding `eval_plane_expr_for_printing`'s row-selection check
  into `card_match_count`) stays gated to `Mode::Card` only, deliberately — the residual already
  provides correct per-printing verification for the other two modes, so consulting the plane
  there too would be redundant, not a speed win.

**Verified**: `price_plane_path_parity_and_shared_witness` (`tests.rs`) — differential check
(`split_planes`-compiled path vs. the unplaned `card_pass` path) across `usd<50`, `-usd<50`, and
`usd<50 AND t:creature`, all three `unique` modes; plus a hand-computed independent ground truth
for `-usd<50`/`unique=card` specifically (needed because `narrow_rec`'s own internal `compile_plane`
call means the "unplaned" differential baseline isn't fully independent of `compile_plane_neg` for
negation — both sides can share the same bug and agree with each other while both being wrong;
caught this by mutation-testing the differential check itself, not just assuming it was a valid
oracle). Mutation-tested all three failure modes directly (temporarily reintroducing each bug,
confirming the relevant test fails, then reverting): the card-only row-selection mistake, skipping
shared-witness tracking, and removing the dedicated negation arms — all three caught.

**Real-corpus benchmark** (`usd<50`, 97,206 printings): `unique=card` — the mode this slice covers
— went from the ~0.4-1ms baseline that motivated this whole project down to **~0.18ms**.
`unique=printing`/`artwork` show no change (**~1.2ms**, unaffected, per the gating above).

- **Fixed cost regardless of `limit`/offset**: O(k) to build the bitmap, then O(words) to select
  any page — same offset-independence #634 Step 2 built for plane-exact filters. Wins on deep
  pagination and on reuse (AND against a plane in a compound query). Predictable worst case:
  never worse than O(k), full stop.
- **Pays O(k) even for a small first-page request** — no idea 1 to compare against anymore, so
  this is just an honest property of the approach, not a tradeoff being weighed against an
  alternative.

## `unique=printing`/`artwork`: investigated, both attempted fixes reverted

Investigated extending the same idea to `unique=printing`/`artwork`. Two fixes were implemented,
benchmarked as a real win against `usd<X`, then both reverted after broader re-benchmarking
against the engine's *other* existential fields (legality, rarity, border) surfaced a real
regression and a real correctness bug, neither visible in the price-only benchmarks — full story,
including the two fixes' design and exactly what broader testing found, moved to
[local-engine-printing-mode-existential-fastpath-attempt.md](local-engine-printing-mode-existential-fastpath-attempt.md)
(extracted to keep this doc from growing past ~500 lines once the `collector_number`/`released_at`
section below landed). Bottom line: the match-phase cost is an inherent floor for these two modes
under an existential predicate — no bitmap/walk cleverness removes it, and `#656` as scoped doesn't
cover existential facts either. Neither reverted fix's code exists on `main`.

## `collector_number`/`released_at` extension, and a second broad-testing lesson

Extended `PlaneExpr::PrintingRangeBits` from `price_usd`/`unique=card` (shipped above) to
`collector_number` (`NumericCmp`/`NumField::CollectorNumberInt`, mechanically the same shape as
price) and `released_at` (`FilterExpr::DateCmp`/`YearCmp`, separate variants with no `NumField`
involved at all — `YearCmp` compares a *derived* value, `date / 10_000`, so its per-printing
recheck needed its own formula, not just a threshold swap). The `field: NumField` tag on
`PrintingRangeBits` generalized to a new `RangePredicateField` enum (`PriceUsd`, `CollectorNumber`,
`ReleasedAtDate`, `ReleasedAtYear`) so `eval_plane_expr_for_printing`/`compile_plane`/
`compile_plane_neg` dispatch on it instead. `price_range_narrowed`/the new
`collector_number_range_narrowed`/`date_range_narrowed`/`year_range_narrowed` are `pub(crate)` in
`lib.rs`, shared between `narrow_rec`'s own per-field arms and `planes.rs`'s `compile_*_range`
functions — same Bug-B-avoidance reasoning as price's own shared-narrowing-function design.

**Verified**: `collector_number_plane_path_parity_and_shared_witness`/
`released_at_plane_path_parity_and_shared_witness` mirror `price_plane_path_parity_and_shared_witness`
exactly (row selection, negation, same-field shared witness, all three `unique` modes), plus
`cross_field_range_predicates_decline_shared_witness` (usd+cn, usd+date, cn+date pairwise, one card
with 3 printings each satisfying exactly one condition — confirms cross-field composition declines
correctly, not just same-field) and `legality_and_date_decline_shared_witness` (legality+date,
confirming the pre-existing shared-witness machinery generalizes to the new fields without
modification). Mutation-tested both new fields' negation arms and the shared-witness id uniqueness
directly (temporarily reintroducing each bug class, confirming the relevant test fails, then
reverting) — same rigor as price's own verification.

**Real-corpus benchmark, targeted (`unique=card`)**: `cn<100` 0.75ms → 0.16ms (4.6×), `cn>=100`
0.64ms → 0.19ms (3.3×), `year>2020` 0.52ms → 0.18ms (2.9×), `date>2023-01-01` 0.54ms → 0.14ms
(3.7×). `usd<50` unchanged (0.23ms), as expected. Broad sweep (legality/rarity/border) flat.

### A second broad-testing lesson: compound queries mixing 2+ existential leaves

Benchmarking compound queries (`cn<100 AND usd<50`, `f:modern AND year>2020`) — again, a shape not
covered by the single-field targeted benchmarks above — found a real regression: up to ~1.8× on
two-range-field compounds, and (digging further after the user asked "does this affect the
already-shipped usd<50 too?") a **latent ~3× regression on `f:modern AND usd<50`, present since
price's original PR** (`cd3656e`) and never specifically benchmarked until now. Root cause in both
cases: `narrow_rec`'s top-level fastpath and `split_planes` both attempt `compile_plane` before
knowing whether `and_of_checked_for_shared_witness` will accept the result — each range field's
`compile_*_range` function does a full binary-search-and-materialize before the shared-witness
check can reject it, and 2+ existential leaves in one `And` are common enough (any legality/rarity/
border query paired with any range field, or two range fields together) that this waste shows up
directly in realistic query shapes, not just contrived ones.

**Fix**: `has_conflicting_range_families` (`planes.rs`) — a cheap, purely structural tree walk (no
narrowing, no materialization) that detects whether some `And` node combines 2+ distinct
existential-family leaves (legality/rarity/border/usd/collector_number/released_at, coarse-tagged
via `ExistentialFamily`, deliberately less precise than `collect_existential_indices`' exact dedup
— a false positive here only costs a missed optimization, never a correctness bug, since the real,
authoritative shared-witness check downstream is unchanged). Consulted as a bail-out at both call
sites (`narrow_rec`'s top-level fastpath, `split_planes`'s whole-tree attempt and per-child loop)
to skip the expensive compile attempt entirely when a conflict is structurally certain, falling
straight to the always-correct per-field/residual dispatch instead. `Or` is deliberately not a
conflict site on its own (`∃` distributes over `∨`) — an earlier cut of this fix walked `Or` the
same as `And` and caused its own ~8× regression on `r:common or r:uncommon` (caught by benchmarking,
not by any unit test; added `range_conflict_check_does_not_penalize_bare_or` to cover it directly).

**Real-corpus benchmark, compound queries, before → after this fix** (baseline is `41c9fab`, which
already includes price's shipped fastpath): `cn<100 AND usd<50` 1.03ms → 1.86ms (regressed) → 0.91ms
(fixed, better than baseline); `cn<100 AND year>2020` 1.06ms → 1.67ms → 0.80ms; `f:modern AND
usd<50` 1.03ms → (unaffected by the cn/date work directly, but shares the same latent bug) → 0.51ms
(fixing the pre-existing regression too, a bonus); `f:modern AND cn<100`/`f:modern AND year>2020`
now 0.44-0.45ms, close to or better than the pre-any-plane-extension baseline (0.65ms/0.48ms on
`4a4be10`). `r:common or r:uncommon` (the `Or`-safety regression) back to 0.070ms baseline.
Targeted single-field and broad legality/rarity/border numbers unaffected, re-confirmed after this
fix (see above).

**Lesson carried forward, again**: this is the *third* time in this doc that a targeted benchmark
looked clean and a broader sweep against compound/mixed-family queries found a real regression —
`#667`'s original "broad survey, not the targeted script" lesson, `unique=printing`/`artwork`'s
weighted-walk regression above, and now compound existential-leaf queries. Each time the fix that
looked right had a real, different cost profile once tested against realistic query composition,
not just the query shape that motivated the change.

## Plan

- [x] Ship the price exactness fix standalone (see Prerequisite above) — landed as the
      integer-cents migration in [#688](https://github.com/jbylund/sylvan_librarian/pull/688).
- [x] Ship `printing_to_card` standalone first (see
      [local-engine-direct-projection-arrays.md](local-engine-direct-projection-arrays.md)) —
      load-bearing for idea 1's incremental per-match card check, neutral to idea 2, changes this
      doc's crossover-axis-4 baseline numbers.
- [x] Prove/reject idea 1's premise — rejected, see "Idea 1 ... REJECTED" above: no order-by
      support for `released_at`/`collector_number`, no permutation table for `price_usd`, so the
      aligned/cheap case is unreachable for any target field.
- [x] Implement `PlaneExpr::PrintingRangeBits` for `price_usd`, wired into `eval_planes`,
      `plane_expr_is_existential`, `eval_plane_expr_for_printing`, `collect_existential_indices`,
      and `compile_plane`/`compile_plane_neg`'s `NumericCmp` arms — see "Shipped" above for the
      two ways the final shape differs from the original sketch.
- [x] Benchmark against today's baseline for `unique=card` — real win confirmed (~0.4-1ms → ~0.18ms
      on `usd<50`).
- [x] Investigate extending to `unique=printing`/`artwork` — see "investigated, both attempted
      fixes reverted" above. Neither closed the gap for broad queries (inherent match-phase
      floor), and broader re-benchmarking against the engine's other existential fields
      (legality/rarity/border, not just price) found a real regression in each — both reverted in
      full, confirmed byte-identical to the pre-investigation commit. Closing this gap for real
      would need a different approach than #656's own scoped mechanism, which doesn't cover
      existential facts — not attempted here.
- [x] Extend to `collector_number`/`released_at` (`DateCmp`/`YearCmp`, separate `compile_plane`
      arms from `NumericCmp`'s) — see "collector_number/released_at extension" above. Real wins
      confirmed (2.9-4.6× on targeted single-field queries), broad legality/rarity/border sweep
      flat.
- [x] Found and fixed a compound-query regression (2+ existential leaves in one `And` wastefully
      compile-then-discard before the shared-witness check rejects them) — `has_conflicting_range_
      families`, see "A second broad-testing lesson" above. Also fixed a latent instance of the
      same bug that predates this slice (`f:modern AND usd<50`, present since price's original PR).
- [ ] Decide the `Not`-arm/`tight_narrow_space` composition-safety question (deferred from the
      Prerequisite section to here) — either bring it into scope (needed for `-usd>8`-shaped
      queries, a real fragment in `test_engine_property.py`'s own suite) or explicitly defer it
      again to a third, later piece of work, with a stated reason rather than silently dropping it.
- [ ] Acceptance: `unique=printing`/`artwork` on `usd<50` (still the one gap left open, see
      "investigated, both attempted fixes reverted" above) improves or stays flat vs. baseline; no
      regression on the existing #634/#655 exact paths; passes (and likely extends, given this
      exact class of change already produced two independent bugs in the price prerequisite work,
      one in this slice's own negation handling, and one in the compound-query fix's own first cut)
      `test_engine_property.py`'s differential suite against the reference oracle — a performance
      delta alone is not sufficient to call this done.

## Related

- [local-engine-printing-mode-existential-fastpath-attempt.md](local-engine-printing-mode-existential-fastpath-attempt.md)
  — the `unique=printing`/`artwork` investigation, extracted from this doc: both attempted fixes
  and why they were reverted.
- [local-engine-direct-projection-arrays.md](local-engine-direct-projection-arrays.md) —
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
