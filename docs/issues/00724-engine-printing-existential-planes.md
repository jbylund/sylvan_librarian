# Engine: Printing-Space Bitplanes (Legality / Border / Rarity, Bit-per-Printing)

Status: todo, filed as [#724](https://github.com/jbylund/sylvan_librarian/issues/724). Sequenced
**last**, behind the [printing-space popcount-order plan](local-engine-printing-plane-popcount-order.md)
that consumes them (see that doc's ship-order step 7).

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

## Why — and why there is no benefit yet

These are **prerequisite infrastructure** for the printing-space popcount-order plan, which is where
the win is realized (it ANDs printing bitmaps, `popcount`s the total, bit-walks the page). Measured
targets there (all `unique=printing`): `f:modern border:black` 0.83 ms, bare `border:black` 0.99 ms
→ microseconds.

**On their own these planes provide no query speedup.** The current narrow-and-verify path does not
`popcount` — it counts by iterating — so a printing plane with no consumer is just a bigger archive.
The benefit appears only once the popcount-order plan exists and is wired to consume them. Hence the
sequencing: that plan is built and **validated on the range leaf source first** (`cn<100 usd<50`,
zero existential planes); these planes plug in afterward as another leaf bitmap.

## Which values get a plane

Per-value density crossover — the same one [#713](00713-is-tag-recovery.md) bucket-C uses:
**mid-band/broad → plane**, sparse → postings, saturated → complement-count. `f:modern` (76% of
printings legal) → plane; a rarely-legal format → postings. Not "a plane per value" — the crossover
applied per value, overlapping #713's printing-varying categorical work.

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
