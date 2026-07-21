# Engine: Printing-Space PlanePopcountOrder (the deferred "idea 2")

Status: todo, not yet a filed issue. Extracted from the #702 planner writeup
([done/00702](done/00702-engine-plan-selection-layer.md)), where this was "the one real speed win
found" but deferred. The gating prerequisite is the printing-space pager/permutation,
[#656](https://github.com/jbylund/sylvan_librarian/issues/656). This doc is the entry point; the
mechanism detail and measurements live in the two range-fastpath docs linked throughout — it does
not duplicate them.

## What it is

A fifth physical plan: the printing-space analogue of `PlanePopcountOrder`, which today runs only
in **card** mode ([#634](done/00634-engine-permuted-bitmap-order-phase.md)). For a broad
predicate under `unique=printing`/`artwork`, build a printing-existence bitmap, take its `popcount`
as the **exact total**, then read the page directly off the bitmap in printing sort order.

Contrast with the two plans that ship today for these queries:

- `PrintingRangeScan` (idea 1, shipped [#695](https://github.com/jbylund/sylvan_librarian/pull/695)):
  walks the sort permutation from the top; cost grows ~`(offset+limit)/match_rate`.
- `GatheredScan`: full scan + quickselect; pays an O(n) count pass.

The popcount plan is **flat in offset** (scales with page size, not depth) and gets the total from
a `popcount` instead of a count pass. The idea-1-vs-idea-2 winner is an **offset × selectivity
crossover** — exactly what the cost router's `argmin` can express and a fixed threshold cannot (this
is the "decision a threshold can't make" from the planner writeup).

## Why — and the honest scope

Two distinct values, only one of them strong (see
[broad-range-fastpath](local-engine-broad-range-fastpath.md) and the "three findings" in
[sorted-range-fastpath](local-engine-sorted-range-fastpath.md)):

1. **Offset-independence** for a bare broad printing range. *Weak on its own* — today's O(n) count
   pass is already offset-independent (measured flat, offset 0 vs 5000). #656 is deferred for
   frequency/effort, **not** because deep-offset is slow.
2. **A printing-space popcount total** that removes the count pass for **compound range+range
   printing/artwork** — the real target, and the slowest uncovered gap in the store.

The crossover is real and depth-scaled (measured via `idea1_vs_idea2_probe`, edhrec sort): the
popcount plan wins from offset **~500–2000** at 30% match rate, ~5000 at mid-density, ~10–20k at
high density, and the ratio reaches **35× at offset 20,000**. Those deep ratios are on
already-sub-millisecond queries (~50–170µs of absolute savings at realistic depth), which is why
value #1 alone is weak — but the winner-flips-on-two-variables shape (offset *and* selectivity) is
exactly what an `argmin` over two cost curves expresses and a fixed threshold cannot.

## Target queries

| query shape | mode | today | with this plan |
|---|---|---|---|
| `cn<100 usd<50` (range + range, both broad) | printing / artwork | **~1.07 ms** (full scan, two residuals — the uncovered gap) | ~2×, offset-independent |
| bare broad range at deep offset (`usd<50` @ offset 5000) | printing | flat already | marginal (value #1) |
| broad existential (`border:black`) | printing | ~3× the card total | ~3× via the existential-total path (PR 5) |

Explicit non-targets: **selective** values (`r:rare`, `f:modern`) already narrow and are fast
(0.18–0.36 ms) — they don't need this. Card-mode compounds are covered by the already-planned
card-space idea-2 (PR 2a/3), not this.

## Parts, in ship order

Most pieces already exist or are planned in
[sorted-range-fastpath's roadmap](local-engine-sorted-range-fastpath.md#pr-order) — this is the
printing-space subset, in dependency order:

1. **`printing_to_card` direct array — shipped**
   ([#690](done/00690-engine-direct-projection-arrays.md)). Powers the one-shot
   projection; load-bearing for both ideas.
2. **`PrintingRangeScan` (idea 1) — shipped**
   ([#695](https://github.com/jbylund/sylvan_librarian/pull/695)). Bare broad printing ranges. Most
   of #656's pieces fall out of it plus the card-mode idea-2 core — see [#656 assembly](#656-assembly)
   below.
3. **Card-space idea-2 (`PrintingRangeBits`) — planned, not printing-space** (PR 2a/3 in the
   roadmap). Lands the range-bitmap→popcount machinery + the `must_be_tight` correctness fix in
   `unique=card` first, where the bitmap composes. This is the reusable core.
4. **#656 — printing-space pager + per-sort-column printing permutation.** The gating build: a
   printing-space sort order to page off, an **archive-format bump**. Without it there is nowhere to
   read the page from in printing mode.
5. **The `PrintingPlanePopcountOrder` plan itself.** Build + AND + `popcount` the range bitmap(s) in
   printing space, page off the #656 permutation. Register as a new `PhysicalPlan` with its
   `applicable` / `materializing` / `cost::plan_cost` / executor arms — the #702 router makes this a
   *declaration*, not a tree edit.
6. **Cost-route idea 1 vs this.** The `argmin` already exists; add this plan's cost formula and let
   the offset×selectivity crossover (validated today by `idea1_vs_idea2_probe`) pick between them.

## #656 assembly

#656 is not a from-scratch build once idea-1 (#695) and the card-space idea-2 core (PR 2a) land —
it is mostly assembly:

| #656 needs | built by | notes |
|---|---|---|
| range → **printing** existence bitmap | PR 2a/3 | `PrintingRangeBits` already carries `printing_bits` (card-mode row selection tests it via `eval_plane_expr_for_printing`), so it is built regardless. |
| **popcount total** | PR 2a | PR 2a popcounts *card* bits; the identical step over printing bits is a trivial transfer. |
| **printing-space paging** | #695 | idea-1's permutation walk (expand to printings, test membership, early-stop) *is* the printing pager — swap the membership test from "in `[lo, hi)`" to "in the intersected printing bitmap." |

So **#656 ≈ PR 2a's printing bitmaps + #695's walk + an AND.** The only net-new work is
intersecting ≥2 printing bitmaps and routing the popcount total into the printing path; the
`Mode::Card`-only `run_query_streamed_popcount` is *not* reused (idea-1's walk is the printing-mode
pager). This is why landing both #695 and PR 2a first reduces #656 to a small follow-up, if the
range+range gap proves worth closing.

## Prerequisites & caveats

- **#656 is the blocker** (pager + permutation; archive-format bump).
- **NULL over-inclusion — the #689 lesson.** A range bitmap's `popcount` **over-counts** the moment
  an existential/NULL predicate is trusted directly; this is precisely what
  [PR #689 got wrong and reverted](local-engine-sorted-range-fastpath.md). The `must_be_tight`
  correctness fix is inseparable from the bitmap path and must land with it (PR 2a bundles it).
- **Two-spaces projection.** Postings/ranges are printing-space; the `unique=card` answer is
  card-space, and projection does **not** distribute over AND/OR. Compose the residual in
  printing-space (exact, cheap), project once via `printing_to_card`. See the "Two spaces" section
  of [done/00702](done/00702-engine-plan-selection-layer.md).
- **`unique=artwork` needs a global artwork id** (`printing_to_artwork`/`artwork_to_card`, PR 2b —
  another archive bump) before the plane can popcount over artwork ids.
- **Frequency, not feasibility.** Deep-paged broad-range printing is a cold corner (the #702 survey
  confirmed); the compound range+range gap is a real but rare ~1.07 ms. This is deferred on
  cost/benefit, and it is the kind of latent win the planner exists to make cheap to add later.

## Related

- [done/00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md) — the cost
  router; where this was the deferred "one real win."
- [local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) — idea-1/idea-2
  crossover analysis + the "#656 assembly from PR 1 + PR 2a" note.
- [local-engine-sorted-range-fastpath.md](local-engine-sorted-range-fastpath.md) — the full
  PR-ordered roadmap; idea 1 shipped as #695.
- #656 (pager/permutation), #690 (`printing_to_card`, shipped), #689 (reverted attempt / NULL
  lesson), #634 (card-mode `PlanePopcountOrder`).
