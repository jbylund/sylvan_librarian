# Engine: Printing-Space Existential Bitplanes (Legality / Border / Frame)

Status: todo, filed as [#724](https://github.com/jbylund/sylvan_librarian/issues/724). Sequenced
**last**, behind the [printing-space popcount-order plan](local-engine-printing-plane-popcount-order.md)
that consumes them (see that doc's ship-order step 7).

## What

Bit-per-**printing** planes for printing-varying categorical/existential values — legality (per
format), border, frame. Today these live only in **card space**: legality's `legal_x` plane
([#667](done/00667-engine-legality-divergent-carveout.md)) and border's plane
([#664](done/00664-engine-border-planes.md)) are existence *projections* — a card's bit means
"*some* printing satisfies," not *which* printing. That answers `unique=card` but can neither answer
per-printing membership nor feed a printing-space `popcount`.

A printing-space plane (one bit per printing) gives, for a printing-varying value: the exact
per-printing match set, its total as a `popcount` (O(words), density-independent), and O(1)
membership tests.

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
