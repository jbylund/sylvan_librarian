# Engine: Printing-Space PlanePopcountOrder (the deferred "idea 2")

Status: the printing-space plan itself is **todo** (gated on the printing-space pager/permutation,
[#656](https://github.com/jbylund/sylvan_librarian/issues/656)), but its **card-space groundwork has
shipped**: `CardRangePopcount` ([#725](https://github.com/jbylund/sylvan_librarian/pull/725),
[#726](https://github.com/jbylund/sylvan_librarian/pull/726)) proved the range→bitmap→`popcount`→bit-walk
machinery on the `unique=card` range leaf source (usd/cn/date), and the kernel bench there **measured
the build cost** this plan's whole value hinges on (see § Cost). Extracted from the #702 planner
writeup ([done/00702](done/00702-engine-plan-selection-layer.md)), where this was "the one real speed
win found" but deferred. This doc is the entry point; the mechanism detail and measurements live in
the two range-fastpath docs linked throughout — it does not duplicate them.

## What it is

A fifth physical plan: the printing-space analogue of `PlanePopcountOrder`, which today runs only
in **card** mode ([#634](done/00634-engine-permuted-bitmap-order-phase.md)). For a broad
predicate under `unique=printing`/`artwork`, build a printing-existence bitmap, take its `popcount`
as the **exact total**, then read the page directly off the bitmap in printing sort order.

The match bitmap has a few sources, and they share this one paging plan:

- a **query-time range narrowing** (`usd`/`cn`/`date`) — the original idea 2, this doc's main focus;
- a **precomputed printing bitplane** for a printing-varying value (legality, frame,
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
[broad-range-fastpath](done/local-engine-broad-range-fastpath.md) and the "three findings" in
[sorted-range-fastpath](done/local-engine-sorted-range-fastpath.md)):

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

Measured on `main` (corpus.jsonl, `limit=100`, min of a timed window). The rule is clean: a query
is slow **iff it is broad printing-varying with no narrowing leaf**.

| query | printing-varying leaves | narrowing leaf? | min | target? |
|---|---|---|---|---|
| `cn<100 usd<50` | 2 (ranges, both broad) | no | **1.18 ms** | **yes** |
| `border:black` (bare) | 1 (87%, saturated) | no | **0.99 ms** | **yes** |
| `f:modern border:black` | 2 (both broad) | no | **0.83 ms** | **yes** (shared-witness) |
| `r:rare` (bare) | 1 (38%) | no | 0.42 ms | modest |
| `f:modern r:rare` | 2 | no | 0.38 ms | modest |
| `f:modern` (bare, 76%) | 1 | no | 0.20 ms | modest |
| `f:modern c:green` | 1 | **yes** (`c:green`) | 0.082 ms | modest (all-plane) |
| `f:modern border:black c:green` | 2 | **yes** (`c:green`) | 0.224 ms | modest (all-plane, ~5–7× → ~0.04 ms) |
| `f:modern c:green r:rare` | 2 | **yes** (`c:green`) | 0.116 ms | modest (all-plane) |

**Narrowing sets the priority, but is not a hard non-target.** Whenever a card-invariant leaf narrows
(`c:green` → ~18k printings), the current path is already fast (0.08–0.12 ms) — the per-printing
verify runs over a tiny candidate set. Those queries are lower-priority. But they are still
*improvable* by the same all-plane path: plane-AND + `popcount` is O(words) regardless of the
candidate-set size, so it beats the per-printing verify (O(candidates)) even after narrowing — you
broadcast the card leaf into printing space (cheap), `AND` the printing planes, `popcount`, page.
The cost that grows in a narrowed compound is the *multi*-printing-varying verify: `f:modern c:green`
(one printing-varying leaf) is 0.082 ms, and adding a second (`border:black`) triples it to 0.224 ms,
because the per-candidate verify now checks two conditions each; the plane AND collapses that to
O(words), taking `f:modern border:black c:green` to ~0.04 ms (~5–7×).

So the target set is: **big wins** on broad printing-varying with *no* narrowing leaf
(`cn<100 usd<50` 1.18 ms, `border:black` 0.99 ms, `f:modern border:black` 0.83 ms — a minority of
traffic, since real queries usually carry a filter); **modest wins** on the common narrowed
compounds (already sub-ms, ~2–4× via the all-plane path). The gating cost either way is the same
(next section).

**Shared witness — a correctness reason, not just speed.** With 2+ printing-varying leaves
(`f:modern border:black`), a match needs *one printing* satisfying **both** (`∃p: modern(p) ∧
black(p)`) — not "some printing modern" and "some printing black." AND-ing the per-printing bitmaps
enforces that by construction (a printing bit survives only if set in every leaf's bitmap); composing
card-level existence projections would false-positive (a card with a modern printing and a *separate*
black-bordered one). So for multi-leaf printing-varying compounds this plan is the *correct*
composition, not merely the faster one.

**Both halves are cheap off the intersected plane, and the cost today is the count, not the page.**
The total is one `popcount` (O(words)); the page walks the sort order testing membership bits and
early-stops — for a broad result (~67% pass for `f:modern border:black`) that is ~150 O(1) bit-tests
to fill a 100-row page. Measured, `f:modern border:black` is **offset-flat** (0.755 / 0.746 / 0.763 ms
at offset 0 / 5k / 20k), and so is `cn<100 usd<50` (1.09 / 1.11 ms) — confirming today's cost is the
O(n) count-and-verify pass, not the paging. The `popcount` replaces the count and the bit-test walk
replaces the verify, so `f:modern border:black` should go from 0.75 ms to microseconds.

Non-target: **card mode** (served by the #667 card-space legality planes + card-space idea 2,
PR 2a/3) — this plan is a printing/artwork thing.

## Cost, and routing against today's plan

This is a **new `PhysicalPlan`**, not a replacement. For `c:green border:black f:modern` the router
has (at least) two applicable plans, and the #702 `argmin` picks the cheaper per query:

- **narrow-and-verify (today):** narrow by the card-plane leaf(s), then verify the printing-varying
  residual per candidate. Cost ≈ `candidates × (verify-tier × #printing-varying leaves)` — grows with
  both the candidate count *and* the number of printing-varying conditions (which is why
  `f:modern c:green` 0.082 ms → `f:modern border:black c:green` 0.224 ms).
- **all-plane popcount (this plan):** project each card-plane leaf into printing space, `AND` all the
  printing bitmaps, `popcount` the total, bit-walk the page. Cost ≈
  - `Σ(printings of each projected card-plane)` — **projection, the distinguishing term** (∝ how many
    card-planes must be broadcast, each O(its matching printings));
  - `#planes × words` — the ANDs;
  - `words` — the `popcount`;
  - `(offset+limit)/match_rate` — the page bit-walk (idea-1's walk term).

The point: the all-plane cost is **independent of the candidate count and of the number of
printing-varying leaves** (each is just one more O(words) AND), whereas narrow-and-verify scales with
both. So the `argmin` flips to the all-plane plan exactly as printing-varying leaves multiply or the
candidate set stays large — the crossover the measured 0.082 → 0.224 ms jump previews, expressed as a
cost comparison rather than a hand-tuned threshold. Adding the plan is declaring its `applicable`,
this `plan_cost`, and an executor arm; the #702 router does the selection.

This is one plan across the whole target set, not several: `f:modern border:black` is simply the
**zero-projection** instance (both leaves are already printing planes, nothing to broadcast), and
`c:green border:black f:modern` is the same plan with one projection. There is no separate
"existential-total" plan versus "mixed-compound" plan — just this plan at projection count 0, 1,
2, …, its projection cost term falling to zero when no card-plane leaf is present.

### Compose-space is a cost choice, not a fixed direction

"Project the card-plane *into* printing space" above is only **one** of two exact orders when a query
mixes card-invariant and printing-varying leaves. Projection runs **both ways**, and only one
direction is lossy:

- **`card→printing` (broadcast)** is *exact* — a card-invariant value (`c:green`) is identical across
  all of a card's printings, so broadcasting its bit down loses nothing.
- **`printing→card` (`∃`)** is the *existential* projection — lossy, so it must be the **last** step
  and done **once**.

So for one printing-varying leaf plus card-invariant leaves (`c:green border:black`/card), **both
orders are exact** and it's purely a cost decision:

- *compose in printing:* broadcast `c:green` down, AND with `border:black`, project up once — cost in
  printing-space words (~1,519) plus the broadcast;
- *compose in card:* project `border:black` up to card-existence (the one `∃`), AND with the
  `c:green` card plane — cost in card-space words (~492) plus that projection.

Neither dominates; it depends on the plane sizes, the answer mode (`unique=printing` needs no
up-projection at all), and which projection is cheaper. The router picks per query — a `min` over the
two orders, the same shape as its choice among plans. The bitplanes serve all of this: answer a
`unique=printing` query from the surviving bits directly, or project once to card/artwork to finish
there.

The **one hard constraint is correctness, not cost:** **2+ printing-varying leaves must compose in
printing space** (`border:black r:rare` — AND the two printing planes, *then* project up once); a
card-space existence-AND of two `∃`-projected leaves false-positives (a card with a black printing
and a *separate* rare one). With ≤1 printing-varying leaf, either order is exact and cost decides.

### The build term, now measured — and why #724 is the lever

`CardRangePopcount` (#725/#726) shipped the range leaf source of exactly this shape and **measured
the build** — the query-time scatter+project that turns a range into a bitmap, i.e. the projection
term above at projection-count-1. On `usd<50`'s ~80k-printing slice it is ~**105 µs** (a single fused
scatter+`printing_to_card` pass; ~40% cheaper than scatter-then-project — see `card_range_build_cost_split`),
and it **dominates** the query: `usd<50`/card lands at ~0.143 ms, of which the build is ~105 µs and
the popcount+walk is only ~40 µs.

That gap is the whole argument for persistent bitplanes: a **precomputed** printing bitplane has *no*
build — the #634 card-plane popcount plan runs a bare plane in ~0.06 ms. So [#724](https://github.com/jbylund/sylvan_librarian/issues/724)
(the printing bitplanes) don't just *enable* the printing-varying targets (`f:modern`,
`border:black`, which have no cheap query-time build at all) — for the range targets it **deletes the
~105 µs build term**, taking `usd`/card from ~0.143 ms toward that ~0.06 ms plane floor. The build
being the dominant, measurable cost is what makes "persist the bitplane" a quantified win rather than
a hunch.

## Parts, in ship order

Most pieces already exist or are planned in
[sorted-range-fastpath's roadmap](done/local-engine-sorted-range-fastpath.md#pr-order) — this is the
printing-space subset, in dependency order:

1. **`printing_to_card` direct array — shipped**
   ([#690](done/00690-engine-direct-projection-arrays.md)). Powers the one-shot
   projection; load-bearing for both ideas.
2. **`PrintingRangeScan` (idea 1) — shipped**
   ([#695](https://github.com/jbylund/sylvan_librarian/pull/695)). Bare broad printing ranges. Most
   of #656's pieces fall out of it plus the card-mode idea-2 core — see [#656 assembly](#656-assembly)
   below.
3. **Card-space range popcount (`CardRangePopcount`) — shipped**
   ([#725](https://github.com/jbylund/sylvan_librarian/pull/725) usd,
   [#726](https://github.com/jbylund/sylvan_librarian/pull/726) cn/date). The `unique=card` reusable
   core: a bare range's exact direct slice is, in one fused pass, scattered into a printing membership
   bitmap *and* projected to a card-existence bitmap (via `printing_to_card`); `popcount` = exact
   total; page off the #634 permutation. The "`must_be_tight`" idea landed as **building the direct
   slice ourselves** (always tight) rather than trusting `range_narrowed`'s loose broad complement —
   not a `PrintingRangeBits`/`printing_bits` struct as this doc first guessed. Bare leaf only;
   compounds and existential planes deliberately excluded (that's this printing-space plan's job).
4. **#656 — extend the popcount-order phase to compound residuals + printing/artwork.** As filed,
   #656 is a follow-on to #634 (design in [00634](done/00634-engine-permuted-bitmap-order-phase.md)):
   for a **card-level** orderby (edhrec, cmc…) the printing/artwork page is a *weighted set-bit walk*
   over #634's **existing** card permutation (card weights via `offsets`-diff / artwork groups) — no
   new permutation, no archive bump. Its compound-residual path needs each residual to expose a
   bitmap to `AND` (ranges via PR 2a/3; existential values via their planes, below). A **printing-level**
   orderby (usd, rarity — printing-varying) additionally needs a printing-space sort order — *that* is
   the one archive-bump piece, and it is arguably beyond #656 as currently scoped.
5. **The `PrintingPlanePopcountOrder` plan itself — validated on the range leaf source.** Build + AND
   + `popcount` the range bitmap(s) in printing space, page off the #656 permutation. Register as a
   new `PhysicalPlan` with its `applicable` / `materializing` / `cost::plan_cost` / executor arms —
   the #702 router makes this a *declaration*, not a tree edit. Prove the whole plan on **ranges
   only** (`cn<100 usd<50`/printing, the confirmed 1.18 ms target, **zero** printing bitplanes): a
   range leaf yields a printing bitmap with no new precomputed structure — the `partition_point`
   slice scattered directly, exactly the `range_pbits` `CardRangePopcount` already builds — and is
   **exact**, so the AND / `popcount` / bit-walk / router machinery is validated against the #702
   brute-force reference on a clean source before any printing bitplane exists.
6. **Cost-route idea 1 vs this.** The `argmin` already exists; add this plan's cost formula and let
   the offset×selectivity crossover (validated today by `idea1_vs_idea2_probe`) pick between them.
7. **Printing-space bitplanes — separate track ([#724](https://github.com/jbylund/sylvan_librarian/issues/724)).**
   The legality/border/rarity **leaf source** for the printing-varying targets (`f:modern`,
   `border:black`). **Not sequenced last / not inert** — #724 folds in the `popcount` + a reused page
   walk to answer *bare* printing-mode plane queries **itself** (a standalone ~3×, subsuming PR 5), so
   it can ship early and independently. What it defers to *this* plan is the **compound** case:
   composing multiple printing-varying leaves (and broadcasting card-planes) then projecting once —
   which is where steps 4–6 add value on top. #724 carries its own build cost (per-printing bits +
   an archive-format bump) and the divergent-legality build correctness, validated in isolation.
   Once landed, the same planes plug into this plan as leaf bitmaps for compounds — the machinery from
   step 5 is unchanged.

## #656 assembly

#656 is not a from-scratch build now that idea-1 (#695) and `CardRangePopcount` (#725/#726) have
landed — it is mostly assembly:

| #656 needs | built by | notes |
|---|---|---|
| range → **printing** membership bitmap | #725/#726 | `CardRangePopcount`'s fused build already produces exactly this (`range_pbits`, the tight direct-slice scatter) alongside the card bitmap; it's the same artifact #656 would AND. |
| **popcount total** | #725/#726 | `CardRangePopcount` popcounts the *card* bitmap; the identical step over the *printing* bitmap (`range_pbits`) is a trivial transfer. |
| **printing-space paging** | #695 | idea-1's permutation walk (expand to printings, test membership, early-stop) *is* the printing pager — swap the membership test from "in `[lo, hi)`" to "in the intersected printing bitmap." |

So **#656 ≈ `CardRangePopcount`'s `range_pbits` + #695's walk + an AND.** The only net-new work is
intersecting ≥2 printing bitmaps and routing the popcount total into the printing path; the
`Mode::Card`-only `run_query_streamed_popcount` is *not* reused (idea-1's walk is the printing-mode
pager). Landing #695 and `CardRangePopcount` first is what reduces #656 to a small follow-up, if the
range+range gap proves worth closing.

## Prerequisites & caveats

- **Printing-varying targets need precomputed *printing-space* bitplanes** — now tracked as
  [#724](https://github.com/jbylund/sylvan_librarian/issues/724) (see ship-order step 7). The
  `f:modern` / `border:black` wins all assume a bit-per-printing legality / border plane to `AND` and
  `popcount`. Neither exists: legality's #667 plane and border's #664 plane are **card-space** (they
  answer "some printing legal," not which). Building the printing-space versions is a separate track
  from the range idea-2 parts above — a build-time computation + storage + archive bump — and its own
  per-value density call (`f:modern` broad → plane; a sparse legality value → postings). The range
  parts (1–6) do not deliver the existential wins; that is a parallel piece, sequenced last because it
  is inert without this plan as its consumer.
- **#656 is a partial blocker** (the popcount-order extension; see part 4). Printing-level-orderby
  paging is the one archive-format bump.
- **NULL over-inclusion — the #689 lesson.** A range bitmap's `popcount` **over-counts** the moment
  an existential/NULL predicate is trusted directly; this is precisely what
  [PR #689 got wrong and reverted](done/local-engine-sorted-range-fastpath.md). The `must_be_tight`
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
- [local-engine-broad-range-fastpath.md](done/local-engine-broad-range-fastpath.md) — historical
  idea-1/idea-2 crossover analysis (the range-narrowing bitmap source).
- [local-engine-sorted-range-fastpath.md](done/local-engine-sorted-range-fastpath.md) — the full
  PR-ordered roadmap; idea 1 shipped as #695; the printing-mode existential-total mechanism (PR 5).
- [00680-engine-existential-plane-generalization.md](done/00680-engine-existential-plane-generalization.md),
  [00667 legality](done/00667-engine-legality-divergent-carveout.md),
  [00664 border planes](done/00664-engine-border-planes.md) — the existential-plane framework the
  *precomputed* bitmap source extends; #667's planes are **card-space**, which is why
  `f:modern`/printing still needs a printing-space one.
- [00724-engine-printing-existential-planes.md](done/00724-engine-printing-existential-planes.md)
  ([#724](https://github.com/jbylund/sylvan_librarian/issues/724)) — the printing-space bitplanes
  track: this plan's legality/border leaf source, sequenced last.
- #656 (pager/permutation), #690 (`printing_to_card`, shipped), #689 (reverted attempt / NULL
  lesson), #634 (card-mode `PlanePopcountOrder`).
