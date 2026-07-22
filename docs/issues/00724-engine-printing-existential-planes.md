# Engine: Printing-Space Bitplanes (Legality / Border / Rarity, Bit-per-Printing)

Status: todo, filed as [#724](https://github.com/jbylund/sylvan_librarian/issues/724). **Not deferred
infrastructure** — it ships a standalone win (bare printing-mode plane queries via `popcount` + a
reused page walk, ~3×) *and* is the substrate the
[printing-space popcount-order plan](local-engine-printing-plane-popcount-order.md) needs for
compounds. See "The standalone win" below.

## What — exact, not existential

Bit-per-**printing** planes for printing-varying values — legality (per format), border, rarity.
Today these live only in **card space**, as existence *projections*: legality's `legal_x` plane
([#667](done/00667-engine-legality-divergent-carveout.md)) and border's plane
([#664](done/00664-engine-border-planes.md)) set a card's bit to mean "*some* printing satisfies,"
not *which*. That projection is why they're called "existential" and why they can't be ANDed safely
(shared witness) — but it's a **card-space artifact**, not a property of the attribute.

A **printing-space** plane is the opposite: one bit per printing, set iff *that printing* satisfies —
**exact, not existential**. That changes everything about how they compose and get used:

- **AND / OR / NOT compose exactly in printing space.** `border:black AND r:rare` is the printings
  that are *both* — the shared witness holds bit-by-bit, no projection ambiguity.
- **Two ways to use the composed result:** (a) answer a `unique=printing` query **directly** — the
  surviving bits *are* the rows; or (b) project **once** to card or artwork space (the single `∃`:
  "does this card/artwork have any surviving printing?") and finish the query there. Composing first
  and projecting once is what stays exact — unlike card-space existence-AND, which projects per-leaf
  *before* the AND and false-positives.
- **The divergent-carveout complexity disappears.** #667's dual `has_legal`/`has_notlegal` planes
  exist only because per-printing legality was projected to a card bit and could diverge; in printing
  space each printing's legality is just its own bit — exact, no carveout. So these are not only
  exact but *simpler* than the card-space planes they extend.

They also give the total as a `popcount` (O(words), density-independent) and O(1) membership tests.

## Composing a mixed query: two exact strategies (the planner picks by cost)

Take `pX pY cZ` — two printing-varying leaves (`pX`, `pY`: legality/border/rarity, or a range) and
one card-invariant leaf (`cZ`: color/type). Under `unique=card` the answer is `{card : ∃p, pX(p) ∧
pY(p) ∧ cZ(card)}`. Because these planes are **exact**, there are two exact ways to evaluate it,
differing only in cost:

- **A — everything in printing space, project once.** Broadcast `cZ` **down** to printing space
  (exact — `cZ` is identical across a card's printings), `AND` all three printing bitmaps, then
  project the survivors to the answer space (card `∃`, or artwork; nothing for `unique=printing`).
- **B — compose the printing leaves, project up, finish in card space.** `AND` `pX pY` in printing
  space first — **every surviving bit is, by construction, one printing satisfying both**, so the
  shared-witness problem is resolved *right there* (a set bit *is* the shared witness). Project that
  result **up** to card-existence (one `∃`), then `AND` with the `cZ` card plane in card space.

Both are exact. B stays exact because the *only* multi-printing-varying composition (`pX ∧ pY`)
happens in printing space **before** any projection, and ANDing the projected result with a
card-invariant plane can't reintroduce shared-witness (`cZ` is constant over a card's printings). The
load-bearing rule both obey: **two printing-varying leaves must be `AND`'d in printing space** — a
card-space existence-AND of two separately-`∃`-projected leaves false-positives (a card with a
`pX` printing and a *separate* `pY` printing).

Where they differ is pure cost: A does the `cZ` step in printing-space words (~1,519) after a
broadcast; B does it in card-space words (~492) after projecting `pX ∧ pY` up (cheap when that set is
selective). Neither dominates — so **which strategy runs is a planner cost decision**, the same
`argmin` that chooses among plans (whether these surface as distinct `PhysicalPlan`s or as one plan
that costs its projection order internally is an implementation choice). `unique=printing` skips the
up-projection entirely; `unique=artwork` projects to artwork ids. The exactness of the planes is what
makes the choice purely about cost rather than correctness.

## The standalone win: bare queries, in any unique mode

#724 is **not** inert infrastructure — it includes the **popcount + page walk** to answer a *bare*
printing-varying-plane query itself. Total = `popcount(plane)` (O(words), replaces the O(n) count
pass); page = #695's printing walk (`walk_printing_page`) with its membership test swapped from a
range slice to a plane bit. Both are small on top of *building* the planes — the popcount is a
one-liner, the walk is a reuse. That is a **standalone ~3× on bare `border:black`/`f:modern`/`r:rare`**
(the count pass is the bulk of the cost), so #724 ships value on its own rather than waiting for the
full printing-space plan — and it properly subsumes PR 5 (below), whose "precomputed count" is
exactly this `popcount`.

**Any unique mode, via a final projection.** The planes are printing-space; the answer is delivered
in the query's unique space by projecting *once* at the end — a mode-dependent cost term:

| unique mode | final projection | cost term |
|---|---|---|
| `printing` | none — the surviving bits *are* the rows | 0 |
| `card` | ↑ to card-existence via `printing_to_card` ([#690](done/00690-engine-direct-projection-arrays.md)) | O(surviving printings) |
| `artwork` | ↑ to artwork ids via `printing_to_artwork` | O(surviving printings), **+ needs PR 2b** (global artwork id) |

Two honest scope notes:

- For the **bare** case, `unique=card` is *already* fast via #667's card-space plane + #634 popcount,
  so #724's standalone bare win is really **printing mode** (and artwork, once PR 2b lands the global
  id). Bare-card via #724 is redundant with what ships today.
- Where #724 is the **only** exact option across all three modes is **compounds** (`border:black
  r:rare`, `usd<50 f:modern`): card-space existence-AND can't compose them, so they must be AND'd in
  printing space and projected once (see the two strategies above) — that's the full printing-space
  plan ([local-engine-printing-plane-popcount-order.md](local-engine-printing-plane-popcount-order.md)),
  built on these planes.

So the split is: **#724 delivers bare printing-mode queries standalone** (popcount + walk, ~3×); the
**printing-space plan** adds compound composition, the two-strategy compose-space choice, and the
compound pager (#656). #724 is no longer "sequenced last, inert" — it's a near-term win that is *also*
the substrate for the rest.

## Step-0 measurement: border/rarity are the real targets, not legality

Measured on the current build (no #724; `scripts/bench_printing_planes.py`, 97,206-printing corpus,
min ms):

| query | mode | min ms | note |
|---|---|---:|---|
| `border:black` | printing | 0.869 | the standout bare target |
| `r:rare` | printing | 0.360 | moderate bare target |
| `f:modern` | printing | 0.190 | already cheap |
| `f:commander` (97k) | printing | 0.216 | already cheap despite broad |
| `f:modern border:black` | printing | 0.741 | compound (substrate) |
| `border:black r:rare` | printing | 0.608 | compound (substrate) |
| `border:black` | card | 0.064 | already fast (#667+#634) — *not* a target |
| `border:black r:rare` | card | 0.316 | card compound (substrate) |
| `border:black` | artwork | 0.905 | slow — needs PR 2b |

The load-bearing finding: **legality is ~4× cheaper than border in printing mode at the same
breadth, because legality settles at the *card* level** (`printing_dependent(Legality) => false`,
[filter.rs](../../card_engine/src/filter.rs)) — it evaluates once per card (~31.5k) and emits all of
a matching card's printings, dropping to per-printing only for the rare divergent-legality cards.
**Border and rarity are genuinely per-printing** (`TextField::Border => StrVal::PDep`), so they
evaluate per printing (~97k) — that scan is the cost.

So #724's *bare*-query win is concentrated on **border and rarity** (per-printing, no card-level
shortcut); bare legality is already fine. **Start with border** (biggest bare win, `Eq`-only, #664's
card-space border plane to diff against). Legality's #724 value is for **compounds** (AND'd with
border/rarity in printing space) and the divergent carveout — lower priority.

## Which values get a plane

Per-value density crossover — the same one [#713](00713-is-tag-recovery.md) bucket-C uses:
**mid-band/broad → plane**, sparse → postings, saturated → complement-count. `f:modern` (76% of
printings legal) → plane; a rarely-legal format → postings. Not "a plane per value" — the crossover
applied per value, overlapping #713's printing-varying categorical work.

## Border: the first slice (concrete design)

Border is the first slice: the biggest bare win (`border:black`/printing 0.87 ms → µs), genuinely
per-printing (no card-level shortcut, unlike legality), a small closed value set, and #664's
card-space border plane is there as a correctness oracle to diff against.

**A plane, not a divergent-bit carveout.** We *could* copy legality's trick — a card-level border +
a "divergent" bit, per-printing only for divergent cards. But border is **16.3% divergent** (vs
legality's 0%, measured), so the carveout would settle only ~84% of cards at card level and still
scan the divergent 16% per-printing — a partial win, capped exactly where border is hard, and
bare-only (no per-printing bits for compounds). The printing plane gives the full win regardless of
divergence.

**Representation — density-chosen, no "other" plane.** Per-value by the storage crossover (a plane is
a fixed ~12 KB = 1 bit × n_printings; postings are 4 B/printing, so postings win below ~3,000):

| value | printings | rep |
|---|---:|---|
| `black` | 85,046 | plane |
| `borderless` | 5,701 | plane (just above the line — confirm vs query cost) |
| `white` | 5,131 | plane (ditto) |
| `gold` | 1,238 | **postings** (below crossover; and for a positive sparse query the postings list *is* the answer) |
| `yellow` | 90 | **postings** |

- **No "other" plane.** It was a *card-space existence* artifact: card-mode `-border:black` means
  "∃ a non-black printing," which `complement(∃black)` gets wrong, so #664 needed an `∃`-untracked
  term. In **printing space, negation is plain `complement`** (each printing is or isn't black) —
  exact, no "other." This is even *more* exact than #664: `-border:yellow` (which #664 declines) is
  just `complement(yellow postings)`.
- **Fixed plane set, dynamic postings, drift-assert.** The *decision* of which values get planes is
  hardcoded (`black`/`borderless`/`white`), chosen from measurement — a **dynamic** per-reload
  decision would make the archive layout data-dependent (variable plane count/indices, a runtime
  "which values are planed" map for every consumer, non-deterministic across stores) for a decision
  that essentially never changes, since the distribution is stable. The *postings* side is already
  dynamic (post whatever non-planed values exist, so a new border color lands in postings with no
  code change). A build-time assertion flags drift — a planed value shrinking below ~3k or a posted
  value growing above — so "re-measure and update the constant" surfaces as a failing check.

## Correctness surface (validated in isolation)

The existential projection: a card can satisfy *both* `∃ legal` and `∃ not-legal` at once (its
printings disagree) — the #667 divergent-carveout lesson. The printing-space plane is per-printing
exact, so it sidesteps the projection ambiguity, but building it correctly from bulk data and
composing `Not` carries its own equivalence-testing surface — kept separate from the consumer plan's
query-time correctness so a failure localizes to one side.

## Cost

Build-time computation + storage + an **archive-format bump**.

## Subsumes the count-only "PR 5" idea

The sorted-range roadmap's PR 5 ("printing-mode total for legality/rarity/border") is a precomputed
*scalar* per-value count that answers only a **bare** query's total (`border:black`/printing →
lookup a number). A printing bitplane's `popcount` **is** that count — plus composition (AND/OR),
membership, and paging. So #724 subsumes PR 5. PR 5 is worth building standalone only as a *cheaper
pre-#724 shortcut* (a count table, no per-printing bits, no archive bump) if the bare printing-mode
total turns out hot before these planes land.

## Related

- [local-engine-printing-plane-popcount-order.md](local-engine-printing-plane-popcount-order.md) —
  the consumer plan; this is its existential leaf source.
- [00667 legality](done/00667-engine-legality-divergent-carveout.md),
  [00664 border planes](done/00664-engine-border-planes.md) — the card-space versions this extends
  into printing space.
- [00713 is-tag recovery](00713-is-tag-recovery.md) — bucket-C; same per-value crossover.
- #656 — the popcount-order phase extension.
