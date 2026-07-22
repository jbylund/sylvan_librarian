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

## Related

- [local-engine-printing-plane-popcount-order.md](local-engine-printing-plane-popcount-order.md) —
  the consumer plan; this is its existential leaf source.
- [00667 legality](done/00667-engine-legality-divergent-carveout.md),
  [00664 border planes](done/00664-engine-border-planes.md) — the card-space versions this extends
  into printing space.
- [00713 is-tag recovery](00713-is-tag-recovery.md) — bucket-C; same per-value crossover.
- #656 — the popcount-order phase extension.
