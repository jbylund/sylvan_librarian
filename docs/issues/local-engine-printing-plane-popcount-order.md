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

The match bitmap has a few sources, and they share this one paging plan:

- a **query-time range narrowing** (`usd`/`cn`/`date`) — the original idea 2, this doc's main focus;
- a **precomputed existential printing bitplane** for a printing-varying value (legality, frame,
  border) — e.g. a bit-per-printing "modern-legal" plane;
- **a postings list scattered into a bitmap on demand** — O(matches) to build, for any value that
  has printing-space postings;
- **a card-space bitplane broadcast into printing space on demand** — a card-invariant predicate
  (`c:green`, `t:creature`, `cmc`) is naturally a *card* plane; give each printing its card's bit via
  `printing_to_card`, O(n_printings). This is the **cheap, exact** direction — unlike the
  `printing→card` projection (§ caveats), `card→printing` broadcast needs no distinct-count, because
  the predicate does not vary by printing.

Once a bitmap exists the total is a `popcount` — **O(words) (~1,500 words here ≈ microseconds),
independent of match density** — and the page is read off the sort order. So density gates which
*representation you store* (postings for sparse, a plane for broad/mid, complement for saturated),
**not the cost of the count**. This is the key reason even a *broad* predicate is fine, and why a
broad **compound** is the real win: build each leaf's bitmap (from a range, postings, a plane, or a
broadcast card-plane), `AND` them in O(words), and `popcount` the result.

The card→printing broadcast is what makes a **mixed compound** — a card-invariant leaf AND a
printing-varying one — computable entirely in printing space: `f:modern c:green` becomes
"`c:green` card-plane broadcast to printings, AND `f:modern` printing-plane, `popcount`, page,"
versus today's per-printing legality scan over the green-narrowed set. Same for `border:black
t:creature`, `c:g usd<50`, etc. It also beats intersecting two broad postings lists (`f:modern` ∩
`usd<50` = a 76k list against an 80k list). It's also why a broad
printing-varying value like `f:modern` is a target here, not the non-target the range-only framing
would suggest (see below).

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
| mixed compound: card-invariant × printing-varying (`f:modern c:green`, `c:g usd<50`) | printing / artwork | per-printing scan over the card-narrowed set | broadcast the card-plane in, `AND` the printing bitmap, `popcount`, page |
| bare broad range at deep offset (`usd<50` @ offset 5000) | printing | flat already | marginal (value #1) |
| broad existential printing values — `f:modern` (**76% of printings legal**), `border:black` | printing / artwork | full scan + per-printing existence check; the #667 *card-space* legality plane makes `f:modern`/**card** fast, but printing mode has no such plane | a precomputed existential **printing** bitplane → `popcount` total + O(words) page |

`f:modern` is broad (76% legal), so it does *not* narrow — the earlier "selective, already fast"
framing was measuring card mode, where the #667 card-space plane does the work. Printing mode pays
a per-printing existence check over that broad set, which a printing-space legality bitplane
collapses to a `popcount`.

Explicit non-targets: genuinely **sparse** values (`r:rare`, `is:promo`) narrow cheaply to postings —
no plane earns its keep. **Card mode** broadly is served elsewhere (the #667 card-space legality
planes; card-space idea 2 for ranges, PR 2a/3). Which representation a printing-varying value gets —
plane vs positive-postings vs complement-postings — is a per-value density call (mid-band/broad →
plane, sparse → postings, saturated → complement-count); the printing-mode existential-total
mechanism is sketched under
[sorted-range-fastpath § Existential fields](local-engine-sorted-range-fastpath.md#existential-fields-a-second-cheap-total-mechanism).

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
- **Frequency — and the generalization changes the calculus.** Feasibility was never the question;
  this was deferred on cost/benefit. But that estimate was made against the *range-only* framing,
  where deep-paged broad ranges and compound range+range are a cold corner the #702 survey confirmed.
  The existential (`f:modern`/printing) and especially the **mixed-compound** (`f:modern c:green`)
  targets are *common* query shapes — `f:modern` + other filters is a heavily-used pattern in
  practice — not a rare corner. So the broadened plan is worth materially more than the deferred
  range-only idea-2 was; the re-estimate, not any new feasibility question, is the reason to revisit
  the deferral. Still gated on #656.

## Related

- [done/00702-engine-plan-selection-layer.md](done/00702-engine-plan-selection-layer.md) — the cost
  router; where this was the deferred "one real win."
- [local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) — historical
  idea-1/idea-2 crossover analysis (the range-narrowing bitmap source).
- [local-engine-sorted-range-fastpath.md](local-engine-sorted-range-fastpath.md) — the full
  PR-ordered roadmap; idea 1 shipped as #695; the printing-mode existential-total mechanism (PR 5).
- [00680-engine-existential-plane-generalization.md](done/00680-engine-existential-plane-generalization.md),
  [00667 legality](done/00667-engine-legality-divergent-carveout.md),
  [00664 border planes](done/00664-engine-border-planes.md) — the existential-plane framework the
  *precomputed* bitmap source extends; #667's planes are **card-space**, which is why
  `f:modern`/printing still needs a printing-space one.
- #656 (pager/permutation), #690 (`printing_to_card`, shipped), #689 (reverted attempt / NULL
  lesson), #634 (card-mode `PlanePopcountOrder`).
