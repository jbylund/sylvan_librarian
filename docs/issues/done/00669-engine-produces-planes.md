# Engine: bitplanes for produces: (produced mana)

Status: proposed, not started until now. Follows
[docs/workflows/performance-pr-workflow.md](../workflows/performance-pr-workflow.md).
No GitHub issue filed yet — surfaced while investigating the broad-survey slow tail after #666.

## Measured problem

`produces:` is one of the top dimensions in the broad survey's slow tail (`scripts/survey_queries.py`,
seed 42, `benchmarks/bitplanes/corpus.jsonl`): p90 1.536ms, appearing repeatedly in the slowest-30
list (`oracle:token or produces:b`, `set:khm or produces:r`, `(set:snc color:rg) or (produces:c usd>1)`)
— an unindexed sibling forcing an otherwise-cheap `Or` to fully residual-scan, the same architectural
shape #624/#663 solved for `oracle:` specifically.

## Where the cost is

`ColorField::ProducedMana` is explicitly excluded from `compile_plane` (`planes.rs`, prior to this
change): `ColorField::ProducedMana => return None`, commented "Deliberately unplaned for now (#630):
produces: stays residual." Every query touching `produces:` — alone or nested — pays a full per-card
scan, regardless of how selective the value is.

## Proposed approach: reuse Colors/ColorIdentity's plane machinery unchanged

`produced_mana: u8` (`OracleCard`) is structurally identical to `card_colors`/`card_color_identity` in
every way that matters for `compile_plane`'s exactness argument:

- Built with the same `jsonb_color_to_bits` helper, same WUBRGC bit layout.
- Card-level (not printing-level, unlike `border:` — #666's card-vs-printing distinction doesn't
  apply here at all).
- Evaluated through the exact same `card_colors()` function and `ColorCmp` code path as Colors/
  ColorIdentity (`filter.rs:158-164`) — always known, never `Null`/`PrintingDep`.

So the fix is genuinely mechanical: add a third 6-plane block (`PLANE_PRODUCED_MANA`, same width and
layout as `PLANE_COLORS`/`PLANE_IDENTITY`), set its bits in `build_bit_planes`'s existing per-card
loop, and replace `compile_plane`'s `return None` with `PLANE_PRODUCED_MANA`. No new narrowing arm,
no new struct, no tight/loose judgment call (unlike #666) — this reuses `cmp_expr`/`in_out_planes`'s
existing mask-decomposition logic verbatim, the same way `PLANE_IDENTITY` already reuses it for a
second independent 6-plane block alongside `PLANE_COLORS`.

Real corpus (`benchmarks/bitplanes/corpus.jsonl`, 31,508 distinct cards): 7.83% of cards produce any
mana at all; individual colors range 2.07% (C) to 3.86% (R) — comfortably selective, though
selectivity isn't even the deciding factor here the way it was for word-index dense/sparse tiers or
border's broadness guard: `compile_plane` consumes it exactly regardless of density, same as Colors/
ColorIdentity (a mono-color deck's `c:g` isn't narrow either, and it's still planed).

### Why this wasn't done in #630 phase 1

Not fully clear from the code alone — `produces:` isn't mentioned anywhere in `#630`'s design history
beyond the one exclusion line and its comment ("deliberately unplaned for now"), which reads like
scope-limiting for that phase rather than a discovered blocker. Nothing found in this investigation
suggests a real correctness reason it was excluded (see the structural-identity argument above) — if
review turns up one, this doc is wrong and should say so instead of shipping.

## Acceptance

1. Baseline on `main` (targeted script + `scripts/survey_queries.py`, same corpus/seed as above,
   reused from #663/#666). Memory baseline too (`--features alloc-counter`), since this is a new
   1.5 KiB/card-domain-worth of planes (6 more planes, same size as one existing color/identity block).
2. Targeted configs: `produces:g` / `produces:c` alone, each `CmpOp` (`Eq`/`Ne`/`Ge`/etc, mirroring the
   existing color parity test's op coverage); the exact slow-tail queries from the survey
   (`oracle:token or produces:b`, `set:khm or produces:r`); `Not(produces:x)`; combined with a
   card-invariant sibling (`produces:g type:land`) and a printing-space one (`produces:r set:war`, to
   confirm the mixed-space `And` composition the codebase already handles elsewhere still works);
   controls unrelated to `produces:`.
3. Differential test extending `plane_parity_color_and_type_ops`'s existing coverage to
   `ColorField::ProducedMana` (same ops × masks loop) plus a dedicated independence assertion (a card
   whose `produced_mana` is disjoint from both its own colors and identity — a colorless artifact that
   produces every color is the natural real-world case).
4. `split_planes_composition_rules`'s existing "produced mana stays residual" assertion must flip to
   "produced mana is now plane-expressible, fully consumed."
5. Total-row-count parity on every benchmark config.
6. Re-measure and iterate until no regressions remain.
7. Queries expected to improve: any `produces:` predicate, alone, negated, or nested in `And`/`Or`
   with anything else plane-expressible — including the specific slow-tail queries from the survey
   that motivated this. No queries are expected to regress; there's no loose/declining case to design
   around here, unlike #666.

## Results

Mechanical change, clean result — no regression-chasing loop needed (same as #666, unlike #663's
detour). Confirms the "this is structurally identical to Colors/ColorIdentity" argument held up.

Targeted (`scripts/bench_produces_planes.py`, `benchmarks/bitplanes/corpus.jsonl`, 13 configs,
`--window 5.0`):

| config | before | after | change |
|---|---|---|---|
| `set:khm or produces:r` | 0.955ms | 0.089ms | 10.73x |
| `oracle:token or produces:b` | 0.577ms | 0.086ms | 6.71x |
| `-produces:g` (negation — fully exact, unlike `border:`) | 0.263ms | 0.072ms | 3.65x |
| `produces:g` / `produces:w` / `produces:r` (solo) | 0.187-0.193ms | 0.053-0.054ms | 3.52-3.57x |
| `produces:g type:land` | 0.103ms | 0.057ms | 1.81x |
| `produces:r set:war` (tiny total=18, floor-limited) | 0.025ms | 0.025ms | ~1.00x |
| `(set:snc color:rg) or (produces:c usd>1)` | 1.647ms | 1.605ms | 1.03x, see below |
| controls | unchanged | unchanged | ~1.00x |

One config didn't move: `(set:snc color:rg) or (produces:c usd>1)`, the exact slow-tail query from
the survey that used `produces:c`. Root cause is the pre-existing parser bug noted above, not a
limitation of this design — `"c"` maps to an empty mask (matching every card) for any color-class
field, so that `Or` branch was already unnarrowable (its own union already covers the whole corpus)
regardless of whether `produces:` has a plane. Total-row-count parity confirms this isn't a
regression either (23,631 on both builds, unchanged) — genuinely flat, not slower.

Broad survey (`scripts/survey_queries.py`, seed 42): overall geomean 0.969x (flat), produced_mana-
tagged geomean **2.17x faster** across the 8 tagged queries in this sample — 7 of 8 improved
(0.10x-0.97x ratio, i.e. 1.03x-10x faster), the 8th being the same `produces:c` query above (1.06x,
flat, same explanation). The handful of >10% deltas elsewhere in the full 520-query survey are all
unrelated shapes with sub-millisecond absolute differences, consistent with noise.

Memory: `+23.11 KiB` archive growth (+0.035%) — exactly `6 planes × 493 words × 8 bytes`, double
`border:`'s footprint since this is 6 planes instead of 3, no surprises. `reload_peak` unchanged.

Total-row-count parity held on every config, every run, throughout.

## Related

- #663 (engine-oracle-word-index), #666 (engine-border-planes) — established the benchmark protocol.
- #630 (bitplanes phase 1) — where Colors/ColorIdentity/Types were originally planed and `produces:`
  was explicitly left out.
- #624 (bind-memoized text predicates) — the precedent for "an unindexed `Or` sibling forces the
  whole branch residual," the pattern that surfaced this in the broad survey.
- Found, not fixed, during this work: `produces:c`/`produces:colorless` incorrectly matches every
  card (parser bug, `api/parsing/card_query_nodes.py:223-232` — treats the literal "c"/"colorless"
  as an empty mask for *any* color-class field, correct for `color:`/`identity:` but wrong for
  `produces:`, where `{C}` is a real positive bit). Out of scope here (a parser semantics fix, not
  an engine performance change) — flagged for a separate follow-up.
