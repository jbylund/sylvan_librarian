# Engine: repairing plane-promoted printing-varying fields (general pattern)

Status: reference only — not an active issue, nothing here is scheduled. Developed while designing
#667 (legality divergent-card carve-out), then **superseded for legality itself** by a cleaner
dual-exact-representation design (see
[docs/issues/engine-legality-divergent-carveout.md](engine-legality-divergent-carveout.md)) that
needs none of this. Captured here because the reasoning generalizes to a *future* printing-varying
field that might not have legality's escape hatch available. If nothing ever needs this, that's
fine — the design work isn't wasted, it's just not costing anything sitting in a doc either.

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

## Why none of this shipped

Legality turned out to satisfy the escape hatch's condition — a fixed, small, fully-enumerable
query space (format × LEGAL) — so building two exact, independently-density-thresholded
representations per format solves the whole problem at build time with no runtime tax and no
cardinality guard needed at all. The repair toolkit above is real, working, tested code as of this
writing (the divergent-carve-out PR's first two rounds), just not the shape that landed. Worth
revisiting this doc, not reinventing it, the next time a printing-varying field's query space turns
out not to be enumerable ahead of time.

## Related

- [engine-legality-divergent-carveout.md](engine-legality-divergent-carveout.md) — where this was
  developed, and the dual-representation design that replaced it
- #667, #634, #654, #656 — same as that doc's Related section
