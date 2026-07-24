# Engine: repairing plane-promoted printing-varying fields (general pattern)

Status: reference only — not an active issue, nothing here is scheduled. Developed while designing
#667 (legality divergent-card carve-out), then **superseded for legality itself** at the *card* level
by a cleaner dual-exact-representation design (see
[done/00667-engine-legality-divergent-carveout.md](done/00667-engine-legality-divergent-carveout.md))
that needs no card-level repair. Captured here because the reasoning generalizes to a *future*
printing-varying field that might not have legality's escape hatch available.

**Update (2026-07):** part of this toolkit did resurface — the #724/#744 printing-space compose build
materializes legality's *printing* bitmap with exactly this doc's **Part 1** shape (a best-effort card
plane broadcast down + a fixed divergent correction set), shipped as `repair_divergent_printings`
(`card_engine/src/lib.rs`). What remains unbuilt is the doc's original subject — repairing a
printing-varying field promoted into a *card-level* plane — and the Part 3 joint per-printing
evaluator. See § What shipped, and what didn't.

## The general problem

A card-level plane (`compile_plane`, `all_match` promotion, the #634 popcount-skip walk) requires a
field to be a genuine two-valued fact *per card* — true for every printing or false for every
printing, no in-between. Some fields aren't: they vary printing-to-printing within a single card
(legality, hypothetically `set:`, `frame:`, `border:`, or anything else printing-native), so a naive
card-level plane bit has to pick *some* answer for a card whose printings disagree, and that answer
is wrong for whichever direction it didn't pick.

## The escape hatch: when a field doesn't need any of this

If the field's possible query shapes come from a **small, enumerable, known-at-build-time set**
(legality: ~22 formats × a LEGAL check — nothing else), both existence projections —
`∃p: satisfies(p)` and `∃p: ¬satisfies(p)` — can be **precomputed exactly at build time**,
independently, one per format. These are two genuinely different facts (a card can satisfy both at
once — that's what "the printings disagree" means), so storing both, each in whichever
representation (plane or postings) is cheap given its own measured density, answers `format:X` and
`-format:X` **exactly, with zero runtime cost**, for the overwhelming common case: a single such
field ANDed with any number of card-invariant siblings, since invariant siblings factor straight out
of the existence quantifier. `Not` wrapping something more complex than a bare leaf (`-(format:X AND
t:creature)`) needs De Morgan pushed down at compile time so the `Not` lands directly on the leaf
(swapping to the "other side" plane) rather than bit-complementing a compound — a one-time, tiny-tree,
bind-time cost, not a per-candidate one.

This escape hatch stops working the moment the field's query space **isn't enumerable ahead of
time** — a parameterized/unbounded predicate (an arbitrary numeric threshold, an arbitrary string
match) can't have "both existence projections for every possible value" precomputed, since there are
infinitely many possible values. That's the actual dividing line: legality qualifies because
`expected == LEGALITY_LEGAL` against a fixed format list is the *entire* query space for this field;
a hypothetical printing-varying numeric field would not qualify, because `> 3.7` and `> 3.71` are
different, un-precomputable existence projections.

## The repair toolkit, for when the escape hatch doesn't apply

If a future field can't use two precomputed exact representations, the following pieces (designed
and prototyped for legality, unused there once the escape hatch was found, but real and tested code
at the time) are the shape a repair-based promotion needs:

**1. Best-effort plane + fixed correction set.** Build the plane from *some* choice (e.g. a
canonical/majority printing's value), and maintain a small, fixed postings list of the cards where
that choice is wrong for at least one printing (legality's `legal_divergent`, ~556 cards, already
existed for other reasons and is reusable as-is for a future field with a similarly small
disagreement rate). Cost is bounded by the *disagreement rate*, not corpus size — only worth it if
that rate stays small.

**2. Extraction-gate principle.** Promoting the field into the plane only pays for itself when the
*containing filter* fully resolves to `True` and reaches `all_match`/popcount-skip — a partial
extraction (some sibling doesn't compile, leaving a residual) gets 100% of the repair's cost and 0%
of its benefit, since the downstream path was going to do per-candidate verification regardless.
Concretely: in `split_planes`'s partial-extraction loop, defer any child touching the repair-needing
field back into the residual *unconditionally* if reached at all — reaching that loop already means
the whole filter didn't compile as one unit (the prior whole-tree `compile_plane` check would have
returned otherwise), so there's no scenario where deferring is wrong.

**3. Shared-witness invariant.** Composing *two or more* printing-varying leaves with `And` requires
one printing to satisfy all of them **at once** — `∃p: f(p) ∧ g(p)`, not `(∃p: f(p)) ∧ (∃p: g(p))`,
which could be satisfied by two different printings and is a false positive the moment the two
differ. Card-invariant fields never have this problem (any witness works for all of them
simultaneously), which is also why the extraction gate only needs to worry about the
repair-needing field specifically, not every plane-eligible child. The correct implementation
evaluates the *whole* remaining expression against one printing at a time and only
existence-quantifies at the end (`eval_plane_expr_for_divergent_card`'s design, in the legality
doc) — never each leaf's own projection independently combined afterward. Multiple leaves of the
*same* repair-needing field in one `And` (rare in practice) either need this joint per-printing
check or can simply be declined from full compile-time promotion, falling back to the field's
existing (pre-repair) narrowing arm.

**4. Cardinality/selectivity guard.** Even when full resolution *is* achieved, the repair's cost is
roughly fixed (proportional to the disagreement-set size and leaf count, not to the query's
selectivity) while the alternative (old narrowing + `card_pass`) cost shrinks with selectivity. For
a narrow enough compound, paying the fixed repair cost can lose to just not promoting at all, even
though promotion is *correct* and *usually* faster. This needs actual selectivity information
(precomputed per-plane popcounts, at minimum) and a calibrated threshold — the same kind of
benchmark-sweep process this codebase's other guards (`MAX_UNION_FRACTION`, `STREAM_MIN_MATCHES`,
`AND_SKIP_THRESHOLD`) were derived from, not something to guess analytically.

## What shipped, and what didn't

At the **card** level, legality took the escape hatch — a fixed, small, fully-enumerable query space
(format × LEGAL) — so two exact, independently-density-thresholded `_EXISTS`/`_ABSENT` representations
per format answer `format:X`/`-format:X` at build time with no runtime tax and no cardinality guard.
That is why no *card-level* promotion repair was ever needed, and it is still true.

But the pattern above was not wasted — it came back one space over, in the **printing**-space compose
build (#724/#744):

- **Part 1 (best-effort plane + fixed correction set) — shipped.** `legality_leaf_bits_from_exists` /
  `_from_absent` broadcast the card `_EXISTS`/`_ABSENT` plane **down to printings** (best-effort), then
  `repair_divergent_printings` (`card_engine/src/lib.rs`) overwrites the divergent cards' printings
  from the fixed `legal_divergent` list (~556 cards, `build_divergent_ids` in `planes.rs`). Cost rides
  the disagreement set, and the repair pass is skipped entirely for a format with no divergence
  (`commander`) — exactly this doc's "only worth it if the disagreement rate stays small." The one
  difference from the framing above: it repairs *printing* bits (where per-printing truth matters), not
  a *card*-level plane bit.
- **Part 3 (shared-witness) — the hazard is handled, this doc's mechanism is not.** Two production
  paths cover it without `eval_plane_expr_for_divergent_card`: the card-plane path **declines** on 2+
  existence facts (`and_of_checked_for_shared_witness` in `planes.rs`, which names this doc as the
  joint-eval design if it is ever needed), and printing-space compose **solves** it structurally by
  ANDing printing bitmaps before the ∃-projection (#724/#731). The joint per-printing evaluator itself
  remains unbuilt.
- **Part 4 (cardinality/selectivity guard) — subsumed** by the #702 cost router's general
  selectivity-aware plan choice, rather than a field-specific calibrated threshold.
- **Part 2 (extraction gate) — not shipped, not needed:** nothing promotes a printing-varying field
  into a card-level plane, so the `split_planes` deferral never had to exist.

So the doc's actual subject — repair-based promotion into a **card-level** plane — remains unbuilt and
unneeded, as predicted. Revisit this doc (don't reinvent it) the next time a printing-varying field's
query space turns out *not* to be enumerable ahead of time; `repair_divergent_printings` is now a
working reference implementation of the Part 1 mechanism to crib from.

## Related

- [done/00667-engine-legality-divergent-carveout.md](done/00667-engine-legality-divergent-carveout.md) —
  where this was developed, and the card-level dual-representation design that replaced it
- [done/00724-engine-printing-existential-planes.md](done/00724-engine-printing-existential-planes.md) —
  the printing-space compose build where Part 1's repair (`repair_divergent_printings`) resurfaced
- #667, #634, #654, #656 — same as that doc's Related section
