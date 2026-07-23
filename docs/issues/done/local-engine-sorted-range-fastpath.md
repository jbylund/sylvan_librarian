# Engine: fast path for broad sorted-range predicates, split by unique mode

**Status: done.** Plan drafted 2026-07-16. **Shipped:** PR 1 (printing-mode Idea 1, [#695]), PR 2a
(`usd`, `unique=card`, [#725]), PR 3 (`cn`/`date`, `unique=card`, [#726]) — the card-mode range fast
path (`CardRangePopcount`) now covers all three range families, and Idea 1 covers bare printing
ranges. PR 4 was dropped (see *Considered and dropped*). Everything else this doc left open has
since resolved, not by building it as originally scoped, but by later work landing the same outcome
through a different mechanism:

- **PR 2b** (artwork range support) and **#656** (the deferred Idea-2 printing-space pager for
  compounds like `cn<100 usd<50`) — both resolved by
  [#724](00724-engine-printing-existential-planes.md) (printing-space compose, all three unique
  modes, no archive bump — a query-time-derived global artwork id instead of the persisted array this
  doc proposed) and [#733](https://github.com/jbylund/sylvan_librarian/pull/733) (range leaves as
  compose sources, closing #694 and enabling `cn<100 usd<50`/printing — measured 1271→90µs, the exact
  gap this doc flagged as "the slowest compound of all... an uncovered gap").
- **PR 5** (existential printing-mode total for border/rarity) — subsumed by #724's printing
  bitplanes, exactly as this doc predicted ("Largely subsumed by #724" below): a bitplane's `popcount`
  *is* the count PR 5 would have computed.
- **The Idea-1 crossover guard** — resolved as a non-issue: the #702 cost-based router's own
  `printing_range_route_probe` ([00702](00702-engine-plan-selection-layer.md), lines 289-310)
  swept the fixed 25% veto boundary against a real cost model across offsets to 20,000 and found the
  veto already sits at the crossover (model 1.015× vs. tree gold, no regret) — this doc's prediction
  of a reclaimable moderate band didn't hold up under measurement. Closing this out surfaced a further
  simplification (the veto is now redundant with the cost model and could be deleted outright):
  [local-engine-range-veto-redundancy.md](../local-engine-range-veto-redundancy.md).

Supersedes the *planning* half of
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md), which is kept as the
historical record (the shipped price-exactness prerequisite, and the full account of what PR #689
tried, muddled, and reverted). This doc restates the plan with what we learned since.

## Problem

Broad range predicates over the `PrintingRangeIndex`-backed fields — `usd<50` (83% of printings),
`cn<100`, `year>2020`, `date>2023-01-01`, and `tix`/`eur` once [#638] indexes them — cost ~0.4–1 ms.
On `main` a bare broad range leaf has its narrowing **vetoed** (`broad_ok=false` at the root call
of `narrow_rec`, [lib.rs:2676](../../../card_engine/src/lib.rs#L2676) → `range_narrowed`'s
`!broad_ok` return, [lib.rs:2044](../../../card_engine/src/lib.rs#L2044)), so *all three unique modes*
fall into the same full scan: `run_query_streamed` walks **every card**, expands each to its
printings, and evaluates the price predicate per printing to accumulate `total`
([lib.rs:4099-4131](../../../card_engine/src/lib.rs#L4099-L4131)). That whole-corpus predicate scan is
the cost floor. It is identical across modes — the tell that we're discarding mode-specific
structure.

## The insight the previous investigation missed

These fields each have a **sorted** `PrintingRangeIndex`. Counting how many printings fall in a
half-open `[lo, hi)` is therefore two `partition_point`s — **O(log n)**, not O(matching printings).

PR #689's revert reasoned that "an existential predicate needs the exact count of satisfying
printings, an O(matching printings) cost no walk can remove," and concluded printing mode was an
inherent floor. That is **false for `usd`/`cn`/`date`**: the sorted index hands you the count in
O(log n). The PR over-generalized, then tried to serve printing mode by extending the *card-space*
popcount machinery (`run_query_streamed_popcount` is hard-gated to `Mode::Card`) instead of using
the count it already had. Wrong tool → looked hard → reverted.

It is also **only partly true for legality / rarity / border**. Those have no sorted index, but
they *do* have existential plane pairs, which support a cheaper-than-scan printing-mode total — see
[Existential fields: a second cheap-total mechanism](#existential-fields-a-second-cheap-total-mechanism)
below. The only genuinely irreducible case is *two broad constraints of different kinds* combined
(e.g. `usd<50 f:modern`/printing).

Consequence: the printing-mode `total` is cheap for **every** indexed field, just at different
constants — and the fast path **decomposes by unique mode**, with a printing-mode path the PR
never built.

## Two mechanisms (doc's original Idea 1 / Idea 2), one per mode family

**Idea 1 — `total = k`, then produce the page in order, stop at `limit`.** For `unique=printing`,
every matching printing is its own row: `total` is exactly `k` from the binary search, no dedup, no
card space *ever*. Only the page remains, and how it's produced depends on the order-by:

- **Card-level order (`edhrec`/`name`, the default).** The [#634] sort permutations are *card-space*
  (`SortCol::PriceUsd`/`Rarity` return `None`, [lib.rs:1763](../../../card_engine/src/lib.rs#L1763)) —
  there is no printing permutation. But a card-level key is shared by all of a card's printings, so
  card order *is* printing order: walk the existing card permutation in rank order, expand each card
  to its matching printings, early-stop at `offset + limit`. Cost ≈ `(offset + limit) / match_rate`.
- **Order by the range field itself (`usd<50 order by usd`).** The range index *is* the printing
  permutation for that field — its `[s, e)` slice is already value-sorted. But its within-value
  tiebreak is `pid`, while the canonical key is `(price[dir], edhrec asc, prefer_score asc, pid)`
  ([`sort_key_bits`](../../../card_engine/src/lib.rs#L3355)), and price ties are large (top buckets
  ~1,600 printings; `order by usd asc` page 1 is chosen entirely from one bucket). So the index
  serves the *value* order but not the *tie* order. Fix at query time, no new structure: walk
  value-bucket boundaries by count to find which buckets the page `[offset, offset+limit)` overlaps
  (skipping whole buckets before/after untouched); take all items from fully-covered buckets (≤
  `limit` of them); from each boundary bucket `select_nth_unstable` the needed count by tiebreak (n
  smallest at a leading cut, n largest at a trailing cut; a middle window inside one bucket needs
  two selects); then sort the combined `limit`-item page once by the full canonical key
  `(value[dir], edhrec, prefer_score, pid)` — quickselect need only produce the right *set*, the
  final sort orders it. With `limit`~100 and buckets ~1,600 that's ≤~2 O(bucket) selects + an
  O(limit log limit) sort (µs) vs. the gathered path's full 80k sort. The tiebreak doesn't flip with
  direction, so one code path serves both. Result: `total` O(log n), page O(bucket + limit),
  offset-independent, no archive change.
- **Order by a *different* printing-varying field** (`usd<50 order by cn`): no aligned permutation;
  stays on the gathered path — out of PR 1's scope.

**Idea 2 — project the narrowed set to an existence bitmap, feed the popcount-skip pager.** For
`unique=card`: binary-search → matching printing slice → for each, `printing_to_card[p]`
([the #690 direct array](00690-engine-direct-projection-arrays.md)) sets that card's bit; `total` =
**popcount**; page via `run_query_streamed_popcount` ([lib.rs:3945](../../../card_engine/src/lib.rs#L3945)).
No predicate evaluation, only a bit-scatter over the `k` matches. This is `PrintingRangeBits`, the
core of PR #689. `unique=artwork` is the same shape *once a global artwork id exists*: today the
group id is per-card-local ([lib.rs:1851](../../../card_engine/src/lib.rs#L1851)), so the uniqueness key
is the pair `(card, artwork_group_id)` and there is no single integer to scatter — which is why
[#693] needed a per-query dedup bitmask. Assigning each distinct `(card, local_group)` a stable
**global artwork id** at build time (a `printing_to_artwork` array plus its `artwork_to_card`
inverse, the direct analogue of [#690]'s `printing_to_card`) makes artwork mode structurally
identical to card mode: `printing_to_artwork[p]` scatters a bit, `total` is a popcount, and
`artwork_to_card` resolves each selected artwork back to a card for page emission. This is
artwork-specific groundwork (see PR 2b in the Plan) — printing mode never dedups and card mode
already has its array, so it blocks neither PR 1 nor PR 2a.

## Where each wins

| Case | Winner | Why |
|---|---|---|
| `unique=printing`, broad, first/shallow page | **Idea 1** | `total = k` free; page is a ~`limit/rate` walk. Idea 2's O(k) scatter is pure overhead here. |
| `unique=printing`, order-by **aligned** with the field (`usd<50 order by usd`) | **Idea 1** | The range index *is* the value-sorted permutation → direct slice; boundary bucket(s) re-sorted by the canonical tiebreak at query time. Offset-independent, both directions, no new structure. |
| **compound** `unique=printing`/`artwork`, both broad (`cn<100 usd<50`) | **Idea 2** (printing-space) | No cheap total from either range alone; build+AND+popcount both range bitmaps. Needs the pager extended to printing space ([#656]) — deferred (uncovered gap, ~1.07 ms today). |
| `unique=card` / `unique=artwork`, broad | **Idea 2** | Exact `total` needs dedup = O(k) regardless; once you pay O(k), the bitmap buys offset-independence + reuse for free. |
| **Compound** (`usd<50 AND t:creature`) | **Idea 2** | The bitmap intersects other planes in O(words); Idea 1 is a terminal page-producer, can't compose. |
| **Selective** predicate | *neither* | `main` already narrows these via the sparse-vec path; Idea 1 goes unbounded, Idea 2 is redundant. |

The only cell where they genuinely compete is `unique=printing` + broad + deep-page + unrelated
order-by. Everywhere else the mode and the compound/offset shape pick the winner unambiguously.

## Existential fields: a second cheap-total mechanism

Legality/rarity/border have no sorted index, so `total = k` doesn't apply — but their **existential
plane pairs** give a printing-mode total far cheaper than a full scan, without visiting most
printings. For a queried value `V`, classify every candidate card by two existential planes —
`has_V` ("∃ printing = V") and `has_notV` ("∃ printing ≠ V"):

```
total = Σ_{has_V && !has_notV}  pcount(c)         # pure-V card: all printings match, count from offsets, no loop
      + Σ_{has_V &&  has_notV}  count_matching(c)  # mixed card: loop its printings
      +  0  for !has_V cards
```

The plane pairs already exist on `main`: legality has `PLANE_LEGAL_EXISTS` + `PLANE_LEGAL_ILLEGAL`
directly ([planes.rs:72](../../../card_engine/src/planes.rs#L72)); border/rarity are one-hot, so
`has_notV` is an OR of the other value planes (~5 words per 64 cards). Correctness rests on
"pure-V ⇒ all printings match V," which holds by construction.

The win is gated on **low intra-card mixing**, which holds: rarity is 91% single-rarity (measured),
and border/legality mix only via special treatments / the divergent-legality carveout (minorities).
Mixing is bounded by `min(has_V, has_notV)`, so *whichever* value is queried, the mixed loop is
short. Magnitude is more modest than the sorted-range case: **O(matching cards) + O(mixed
printings)** (the pure sum still walks one bit per matching card — `pcount` varies, no popcount
shortcut), so ~3–5× on the total phase vs. the ~O(log n) sorted-range win.

Two refinements:

- **Bare single-value query** (`border:black`/printing, nothing else): total is a **precomputed
  per-value printing count** — O(1), no classification. A small build-time table (borders ~6,
  rarities ~6, legality ~45 format×state). The classification above is the *general* form that also
  composes with a card-space candidate mask (`border:black AND t:creature`), which the constant can't.
- **Does not help `usd<50 f:modern`/printing**: combining a range count and an existential count
  per card needs per-card range counts inside the mixed loop — back to scanning. Two broad
  constraints of *different kinds* is the one irreducible shape.

### The broadness discard is not the culprit

`border:black`/printing is slow (0.853 ms baseline) because its card-space candidate covers
≥87.5% of cards and is discarded to a full scan
([lib.rs:3815](../../../card_engine/src/lib.rs#L3815)) — but the discard is *correct*, not a bug to
undo. Keeping a ~90%-card candidate would iterate ~90% of cards instead of 100% while still
re-checking `border` per printing (the candidate is a loose card-existence set, so no per-printing
work is skipped), and the card-id materialization overhead roughly cancels the ~10% iteration
saved — which is exactly the tradeoff [#647] calibrated the 87.5% threshold to. So un-discarding
buys ~10% at best. The real cost is counting matching printings with no cheap total, which is what
the classification above removes (~−75%). Card mode already sidesteps the discard entirely via the
existential popcount plane (not gated there); printing mode can't (that path is `Mode::Card`-only),
which is why the slowness is printing-specific. Fix the mechanism (PR 5), not the discard.

## Groundwork (all already on `main`)

- **Exact narrowing**: price is integer cents ([#688]) — `range_narrowed(..., exact=true)` for all
  three fields; nothing to verify after narrowing.
- **`printing_to_card` direct array** + `eval` inlining ([#690]) — powers Idea 2's O(k) projection.
- **`range_narrowed` / `Narrowed.tight` / `broad_ok` / `exact`** plumbing and the binary search
  that yields `k` for free already exist ([lib.rs:2035](../../../card_engine/src/lib.rs#L2035)).

Confirmed absent from `main`: `PrintingRangeBits`, `must_be_tight`, and any range-family check —
so nothing is half-landed. PR #689's `printing_to_card` and `eval`-split commits were the ones
extracted and shipped as [#690]; the rest of that branch is unmerged.

## Correctness: the NULL over-inclusion trap (Idea 2 only)

`range_narrowed`'s broad **complement** branch ([lib.rs:2051-2056](../../../card_engine/src/lib.rs#L2051))
over-includes printings absent from the index (NULL value), so it is `loose`, not `tight`. This is
harmless on `main` (broad lone ranges are vetoed, never reaching a card-space existence answer). It
becomes a real overcount the moment Idea 2 trusts a card bitmap's popcount directly — PR #689
measured `usd<50`/card returning 31,396 instead of 31,217. **Idea 2 must gate on `tight` (thread a
`must_be_tight` flag to the one call site that discards the residual and trusts the bitmap).** Idea
1 is immune — it re-tests membership per emitted printing. This is *created by* Idea 2, not
separable from it.

## Baseline (Step 0)

`main` @ 93608a6, 97,206-printing corpus, min ms over an 8 s window (idle machine, Docker down),
`limit=100`, `orderby=edhrec` unless noted. `total` is the parity check.

| query | card | printing | artwork | total (card / prn) |
|---|---:|---:|---:|---|
| `usd<50` | 0.332 | 0.733 | 0.779 | 31,217 / 80,527 |
| `cn<100` | 0.585 | 0.838 | 0.870 | 17,616 / 35,021 |
| `year>2020` | 0.472 | 0.704 | 0.755 | 18,249 / 46,445 |
| `usd<50 order by usd` (printing) | — | **1.036** | — | 80,527 |
| `usd<50` offset 5000 | 0.345 | 0.746 | — | (vs 0.332 / 0.733 at offset 0) |
| `border:black` (printing) | — | **0.853** | — | 85,046 |
| `r:rare` / `f:modern` (printing) | — | 0.358 / 0.180 | — | 36,764 / 73,783 |
| *ref:* `f:modern` / `t:creature` / `r:rare` (card, kept narrowing) | 0.060 / 0.062 / 0.057 | — | — | — |

**Root cause is uniform:** every slow row is a *broad predicate that loses its narrowing to the
discard, then pays the full count pass*. The fast reference rows (0.06 ms) are planed or
narrowing-kept — the target the slow rows should approach. Field type doesn't change the cost;
broad + printing + no-narrowing = full scan whether the field is sorted-range or existential.

Three findings that shaped the plan:

- **The aligned case (`order by usd`) is the single *worst* config** (1.036 ms, +41% over edhrec
  order) — it falls off the card permutation to the gathered path. PR 1's boundary-bucket sort
  turns the worst case into ~the best. Argues for including it *in* PR 1.
- **Deep offset is flat** — measured at offset 0 vs 5000 for bare (`usd<50`: 0.733→0.746) *and*
  compound printing/artwork (`cn<100 usd<50`, `usd<50 f:modern`, `usd<50 t:creature`: all deltas
  ≤0.02 ms). Today's O(n) count pass is offset-independent. This defeats only the
  *offset-independence* argument for [#656] — **not** its other value (a printing-space popcount
  total, which would remove the count pass for compound printing/artwork; see below). #656 is
  deferred for frequency/effort, not because deep-offset is flat.
- **PR 5's target is *broad* existential printing** (`border:black`), not legality/rarity broadly —
  the *selective* existential values (`r:rare`, `f:modern`) already narrow and are fast (0.18–0.36
  ms). Narrows PR 5's scope and its estimate (0.853 → ~0.18 ms).

### Compounds (Step 0)

| compound | printing | artwork | card |
|---|---:|---:|---:|
| `usd<50 t:creature` (range + selective) | 0.427 | 0.455 | 0.226 |
| `usd<50 f:modern` (range + broad plane) | 0.639 | 0.678 | 0.296 |
| `cn<100 usd<50` (range + range, both broad) | **1.069** | **1.093** | 0.761 |
| `r:rare border:black` (existential + existential) | 0.587 | 0.597 | 0.296 |

**Compound wins live in card mode** (Idea 2's bitmap composes — PR 2a/3/4). For printing/artwork:
range+selective is already improved by narrowing (0.43 vs bare 0.73, but not free — a residual
per-printing check remains); range+broad-plane is the irreducible two-broad shape; range+range is
the **slowest compound of all (~1.07 ms) and an uncovered gap** — neither range narrows (both
>25%), so it full-scans with two residuals. A printing-space popcount ([#656], build+AND+popcount
both range bitmaps) could give range+range printing/artwork ~2×, offset-independent — deferred for
frequency, not because it's impossible.

So PR 1 (`total=k`) helps **bare** single range predicates only; compound range wins come from
Idea 2's composable bitmap in **card** mode (PR 2a/3).

## Plan / sequencing

### Step 0 — prep (not a PR)

Establish the interleaved-A/B baseline harness (shape of `bench_cost_guards.py`) across all three
unique modes for the target queries, and **resolve the PR-1 blocker**: did [#634] build a
per-sort-column permutation for *printing* mode that Idea 1's walk can ride? This gates the order
below — if that permutation is absent, PR 1 grows and swaps after PR 2a. Step 0 also sizes the
Idea-1 guard and confirms whether PR 5 targets a real bottleneck.

### PR order

Ordered by dependency and risk; magnitudes are from PR #689's interleaved-A/B measurements.

- [x] **PR 1 — Idea 1, `unique=printing`, small & independent.** Implemented, open for review as
  [#695](https://github.com/jbylund/sylvan_librarian/pull/695) (not yet merged). For a bare broad range leaf, derive
  `total = k` from the binary search instead of the full count pass, and produce the page without a
  full sort: for card-level orderings ride the existing card permutation (expand to printings,
  early-stop); for order-by-the-range-field, slice the range index and re-sort only the boundary
  bucket(s) by the canonical tiebreak (see [Two mechanisms](#two-mechanisms-docs-original-idea-1--idea-2-one-per-mode-family)).
  Touches only the printing-mode path; leaves `narrow_rec`/`broad_ok` for card/artwork as `main` has
  them. No new persisted structure. `CARD_ENGINE_PRINTING_RANGE_FASTPATH=0` is an A/B kill-switch.
  **Gated at the veto boundary** (`range_too_broad_to_narrow`, ~25% of the index) — the fastpath
  only claims ranges the general path was already full-scanning, so nothing below 25% changes plan;
  the card-walk additionally requires `k > STREAM_MIN_MATCHES` so it never reproduces stream
  ordering where the general path would gather (matters only on tiny indexes; see the crossover
  guard below). Widening below 25% is the deferred crossover guard.
  *Measured* (same-build off vs on, 97,206-printing corpus, min ms): `usd<50` 0.75→0.04,
  `cn<100` 0.87→0.04, `year>2020` 0.74→0.04, aligned `usd<50 order by usd` 1.06→0.05
  (−90 to −95%); card/artwork/compound/selective controls flat; broad survey unchanged.
- [x] **PR 2a — Idea 2, `usd`, `unique=card`** — shipped as the `CardRangePopcount` plan. A bare
  `usd` range projects its exact direct-slice printings → card-existence bitmap → popcount total →
  the #634 streamed-popcount page (range membership threaded into emission so the shown printing is
  in range). The `must_be_tight` idea landed as *building the direct slice ourselves* (always tight)
  rather than trusting `range_narrowed`'s loose broad complement.
  *Measured* (97,206-printing corpus, `limit=100`, min ms, kill-switch off→on): `usd<50`/card
  0.340→0.143 (2.38×), offset 700 0.345→0.144 (2.38×), `usd<2` 0.457→0.131 (3.48×). 0 total-parity
  mismatches across the targeted set and the 520-query survey; calibration 88/88 gold; no control
  regressions. The build is a single fused pass (scatter printing bit + set card bit via
  `printing_to_card` together): a kernel bench (`card_range_build_cost_split`) found the
  scatter-then-project's *projection* was the expensive half (143µs vs 30µs on `usd<50`), and fusing
  it is ~40% cheaper (174µs→104µs) — that build is most of the query cost, so a persisted printing
  bitplane (#724) would be the next lever.
  **Scope narrowed from the original plan, on measurement:** *bare* range only. Composable-plane
  compounds (`usd<50 c:g`) were dropped — the plane already narrows them, so the existing path is
  faster than building the whole range bitmap (measured a regression when forced). Existential
  (`usd<50 f:modern`) and range+range (`usd<50 cn<100`) are excluded on **correctness** grounds
  (shared-witness / legality divergence — printing-space's job, not card-space). *Depends:* —.
  *Gate:* `CARD_ENGINE_RANGE_BITS_CARD` A/B.
- [x] **PR 3 — extend `CardRangePopcount` to `collector_number` + `released_at`** — shipped. The
  gate became `bare_range_bounds` (already returns index + `[lo,hi)` for usd/cn/date), and
  `build_card_range_bits` takes the index rather than hardcoding `price_usd`; `usd_bare_range_bounds`
  removed. No new plan, no new correctness surface (cn/date are printing-varying integer ranges like
  usd). *Measured* (off→on): `cn<100`/card 0.589→0.088 (**6.66×**), offset 700 6.47×, `year>=2015`
  0.416→0.124 (3.36×), `year<2005` 0.280→0.064 (4.40×); usd unchanged; 0 parity mismatches;
  calibration 88/88 gold. *Compounds still excluded* (bare-leaf gate) — they're the printing-space
  plane's job (compose in printing space, project once).
- [x] **PR 2b — global artwork id groundwork + `unique=artwork`.** Resolved by
  [#724](00724-engine-printing-existential-planes.md) + [#733], but not as originally scoped:
  instead of a persisted `printing_to_artwork`/`artwork_to_card` pair (archive format bump), #724
  derives the global artwork id at query time (`artwork_base[card] + artwork_group_id`, a prefix-sum
  of the existing `artwork_groups` counts) — no archive change. #733 then made range leaves
  (`usd`/`cn`/`date`) compose sources under that projection. *Measured* (#733): `cn<100`/artwork
  910→124µs (7.3×), `usd<50 border:black`/artwork 1291→200µs (6.5×), `usd<50`/artwork 824→211µs
  (3.9×).
- [x] **PR 5 — printing-mode total for legality/rarity/border** — subsumed by
  [#724](00724-engine-printing-existential-planes.md) exactly as predicted below: a printing
  bitplane's `popcount` *is* this count, plus composition/membership/paging for free. No standalone
  PR was needed.

**Why this order:** PR 1 first keeps the "small, independent, separable" principle — lowest risk,
printing-only, validates the walk + harness (contingent on Step 0; else swap with 2a). `2a → 3`
is the core value block on the default `unique=card` path in dependency order (2a lays the plane, 3
reuses it). `2b` follows 3 so the artwork plane inherits all three fields at once. `5` last and
gated — real but modest, on a combo not yet confirmed hot. (PR 4 was dropped — see below.)

### Considered and dropped

- **PR 4 — compound structural pre-checks** (`has_conflicting_range_families` /
  `contains_range_family_leaf`, to skip a `compile_plane` fold that would be discarded). **Dropped as
  not worth a PR**, once we looked at what it would actually save (established while sequencing after
  PR 3 merged):
  - The `compile_plane`-then-discard it targeted is **cheap**. `compile_plane` only builds a
    `PlaneExpr` — references into *precomputed* plane indices (`cmp_expr`, `compile_border_cmp`,
    `compile_rarity_cmp`); no evaluation, no scan (except the narrow oracle-word-bonus arm, which
    copies a `Bits` slice). The discard happens in `split_planes` for an existential plane
    (legality/rarity/border) under a shared-witness `And` or a printing/artwork existential leaf, but
    what's thrown away is a small AST + a few index lookups. Skipping it saves ~nothing.
  - The range side is cheaper still: `usd`/`cn`/`date` never `compile_plane` (it returns `None` for
    them); a bare range's whole cost is two `partition_point`s, and the bare-range fast paths (PR 1 /
    `CardRangePopcount`) are already lazy (printing) or reuse-the-build-on-fallback (card) — no
    materialize-then-discard to skip.
  - The *only* O(k) waste that could exist is a **broad range in a compound** scattered into a bitmap
    (`range_narrowed`'s `broad_ok` branch, ~tens of µs on the smaller/complement side) and then
    dropped by `narrow_candidates_exact`'s >¾-domain cutoff. But that lives in `narrow_rec`, not
    `compile_plane`; it's small; and its firing conditions (`broad_ok` threaded true *and* the result
    exceeding ¾) are unconfirmed.

  So the original bullet conflated a cheap AST discard with a hypothetical O(k) narrow-discard, and
  neither justifies a PR. Revisit only if a measurement (instrument `range_narrowed` /
  `narrow_candidates_exact` to count scatter-then-discard events and their µs) turns up real waste —
  as a standalone "skip doomed `compile_plane`" it's dead.

### Deferred, now resolved

- [x] **Idea-2 printing-space pager** ([#656]) — the mechanism for **compound printing/artwork** (a
  printing-space popcount total; `cn<100 usd<50`/printing ~1.07 ms was the uncovered gap). Resolved
  by [#724](00724-engine-printing-existential-planes.md) (printing-space compose) +
  [#733](https://github.com/jbylund/sylvan_librarian/pull/733) (range leaves as compose sources) —
  see [#656 assembly](#656-assembly-from-pr-1--pr-2a) below. Not the mechanism originally sketched
  here (a dedicated printing-bitmap popcount-skip pager), but the same outcome.
- [x] **Idea-1 crossover guard** — resolved as a **non-issue**, not by widening the gate. The #702
  cost-based router's `printing_range_route_probe`
  ([00702](00702-engine-plan-selection-layer.md), lines 289-310) swept the fixed 25% veto
  boundary against a real `(k, offset)`-aware cost model across offsets to 20,000 and found the veto
  already sits at the crossover (model 1.015× vs. tree gold, no regret) — the reclaimable ~1.8k–20k
  band predicted below didn't hold up under measurement, so there was nothing to widen into. This did
  surface a further finding — the veto is now redundant with the cost model and could be deleted
  outright — split out to
  [local-engine-range-veto-redundancy.md](../local-engine-range-veto-redundancy.md). The sweep design
  below is kept as the historical record of the (superseded) plan.

### Crossover guard: sweep design (superseded — see above)

Goal: replace the walk branch's veto-boundary gate (`range_too_broad_to_narrow && k > STREAM_MIN_MATCHES`)
with a calibrated `(k, offset)` predicate that picks the cheaper of narrow-and-scan vs. fastpath-walk.
Follow [#647]'s dialable-`k` method.

Axes to sweep:

- **k (match count)** — the knob. Dial with `usd<X` swept against the corpus price CDF (or a
  synthetic uniform field for exact control), spanning ~500 → ~40k so it straddles both the
  first-page crossover (√(n·limit) ≈ 1.8k) and the 25% veto (~20k).
- **offset (page depth)** — 0, 100, 1,000, 5,000, and ~`k−limit` (last page). This is the axis that
  bends the guard: the walk carries `offset`, narrowing doesn't.
- **order-by class** — card-level unrelated (`order by edhrec`, the walk — the offset-sensitive,
  interesting case) and aligned (`order by usd`, `aligned_page` — offset-independent, a control that
  should win whenever broad). `limit` fixed at the product page size.

Method: per `(k, offset, order-by)` cell, interleaved A/B of walk vs. narrow via
`CARD_ENGINE_PRINTING_RANGE_FASTPATH` on/off (off = the narrow+scan the fastpath would replace),
min ms; the crossover per `(offset, order-by)` slice is the `k` where they cross. Expected fit:
walk wins when `(offset+limit)·n_cards / k ≲ c·k`, i.e. `k² ≳ (offset+limit)·n_cards / c` — so the
guard is roughly `k*k > (offset+limit)*n_cards*K` for a measured `K`, offset-aware by construction.
Aligned keeps its simpler broad gate (offset-independent).

Harness: extend `scripts/bench_printing_range.py` (or a `bench_cost_guards.py`-shaped script) to
sweep the `(k, offset)` grid with dialable `k` and emit per-slice crossovers. **Acceptance is the
deep-page floor**: every `(k, deep-offset)` cell must stay ≥ the narrow plan — the guard exists so
deep pages fall back to narrowing exactly where the walk would lose.

### #656 assembly (from PR 1 + PR 2a)

Originally planned to consolidate into the printing-space plan doc as **#656 ≈ PR 2a's printing
bitmaps + PR 1's walk + an AND**. Landed differently: [#724](00724-engine-printing-existential-planes.md)
built printing-space compose (AND/OR across border/rarity/legality, projected once to any unique
mode) directly, and [#733](https://github.com/jbylund/sylvan_librarian/pull/733) made range leaves a
compose source on top of it — reaching the same compound wins without a dedicated #656 PR.

### Acceptance (every PR)

- [ ] Improves or holds flat vs. baseline for its target mode; no regression on the [#634]/[#655]
  exact paths or the legality/rarity/border sweep.
- [ ] Passes (and likely extends) `test_engine_property.py`'s differential suite against the
  reference oracle — a perf delta alone is not "done" (this class already produced two independent
  bugs in the price prerequisite work).

## Open questions

- **Printing-mode page walk mechanics** — *resolved (Step 0):* #634 permutations are card-space
  (price/rarity return `None`); PR 1 rides the card permutation for card-level orderings and slices
  the range index (boundary-bucket re-sort) for order-by-the-range-field. A *different*
  printing-varying order-by stays on the gathered path.
- **Global artwork id sizing** — *moot:* #724 never persists `artwork_to_card`; it derives the global
  id at query time from the existing `artwork_groups` prefix sum, so there was no array to size.

## Related

- [local-engine-printing-plane-popcount-order.md](../local-engine-printing-plane-popcount-order.md) —
  the consolidated plan for the deferred Idea-2 printing-space popcount plan (#656): parts, ship
  order, target queries, and the #656 assembly.
- [local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) — history:
  price-exactness prerequisite (shipped) and the full PR #689 account.
- [00690-engine-direct-projection-arrays.md](00690-engine-direct-projection-arrays.md) —
  `printing_to_card`, load-bearing for Idea 2.
- [00680-engine-existential-plane-generalization.md](00680-engine-existential-plane-generalization.md)
  — the existential-plane framework Idea 2 extends.
- [00634-engine-permuted-bitmap-order-phase.md](00634-engine-permuted-bitmap-order-phase.md) —
  the order permutation Idea 1 walks and the popcount pager Idea 2 feeds.
- [local-engine-numeric-range-planes.md](local-engine-numeric-range-planes.md) — the
  card-invariant `cmc`/`power`/`toughness` analogue; does not transfer (no existential dimension).
- [00647-engine-cost-guard-calibration.md](00647-engine-cost-guard-calibration.md) — the
  guard-from-measurement precedent for Idea 1's crossover.
- [#638] `tix`/`eur` range index; [#656] printing-space popcount pager; [#693] artwork dedup bitmask.

[#638]: https://github.com/jbylund/sylvan_librarian/issues/638
[#647]: https://github.com/jbylund/sylvan_librarian/pull/647
[#656]: https://github.com/jbylund/sylvan_librarian/issues/656
[#634]: https://github.com/jbylund/sylvan_librarian/pull/634
[#655]: https://github.com/jbylund/sylvan_librarian/pull/655
[#680]: https://github.com/jbylund/sylvan_librarian/pull/680
[#688]: https://github.com/jbylund/sylvan_librarian/pull/688
[#689]: https://github.com/jbylund/sylvan_librarian/pull/689
[#690]: https://github.com/jbylund/sylvan_librarian/pull/690
[#693]: https://github.com/jbylund/sylvan_librarian/pull/693
[#695]: https://github.com/jbylund/sylvan_librarian/pull/695
