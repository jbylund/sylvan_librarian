# Engine: fold provably-constant predicates at bind

Status: won't implement — closed 2026-07-11. GitHub: #631 (closed as completed/rejected: checked
real wild-corpus traffic and found zero instances of the tautology shapes this doc assumed).
Filed 2026-07-07 from the #619 benchmark review.

## Problem

`rarity>=common` is provably true *by data* (0 of 97,206 printings lack a
rarity) yet runs a full scan — and as a conjunct it makes the whole filter
printing-dependent, defeating all_match fast paths.

## Design

Per-field `(min, max, has_nulls)` stats in the archive (bytes), plus a fold
step in bind(): NumericCmp folds to True when every value in [min, max]
satisfies the op **and has_nulls == false**; to constant false when none can
**and has_nulls == false**. The null guard is load-bearing both ways: NULL
rows fail `rarity>=common` (True-folding needs no nulls) and Not(False)=True
vs Not(Null)=Null (False-folding under negation needs it too). Static
mask-algebra folds need no stats: `id<=wubrg` → True, `c>=colorless` → True.
Propagate: And drops True / collapses on False; Or dual; Not flips.

## Qualifying today (measured)

rarity (0 nulls), cmc (0), released_at (0), color masks (never null).
Not foldable: power/toughness (51k nulls — `power>=0` is a creature filter
in disguise), prices (16k), edhrec (4.5k). `has_nulls` computed from the
store, never assumed.

## Expected

Bare tautologies become match-all; the real win is conjuncts —
`t:creature rarity>=common` becomes `t:creature` (all_match restored).
Composes with [00630-engine-card-bitplanes.md](00630-engine-card-bitplanes.md): folded
children leave more filters fully plane-expressible.
