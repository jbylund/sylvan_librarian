# Cost/Feature Estimation: Remaining Candidates After the Card-Invariant Scan Depth Fix

Companion to branch `engine-cost-model-cleanup` (cut from #1024). That branch has landed four commits
so far: two `compose_printing_estimate` precision bugs (unit-mismatched `domain_hint`, match-count-vs-
span confusion for printing-varying fields), the `SpaceEstimate` triple refactor
([local-engine-compose-estimate-space-triple.md](local-engine-compose-estimate-space-triple.md)'s
design), and — most recently — pricing card-invariant bare leaves (`cmc`/`power`/`toughness`/
`color_identity`/`legality`/`border`/card-space collections) at their true one-printing scan depth
instead of `domain_cards * printings_per_card * COMPOSE_CANDIDATE_SPAN_BIAS`, paired-diff verified at
897 improved / 2 regressed (91.5M → 84.7M total absolute `scan_units` error, pooled across every
`unique=card` plan/acquire).

This is the punch list of what's left, in priority order, from re-running
[`bench_cost_model_agreement.py`](../../scripts/bench_cost_model_agreement.py) against that fix. Each
item needs its own measure-then-implement pass and its own commit — see the four already-shipped
commits for the pattern (paired diff, magnitude-weighted, before touching a shared constant or gate).

## Current state of the table

By plan × `unique` (measured/predicted; >1 under-costed; `within 25%` is the agreement rate):

```
plan                unique      n      median   p10    p90   within 25%
GatheredScan        card      14120     0.68   0.25   2.73      16%   FAIL
GatheredScan        printing  14009     0.81   0.25   2.61      25%
GatheredScan        artwork   13871     0.83   0.49   2.66      22%
PlanePopcountOrder  card       2214     0.76   0.54   0.94      33%   FAIL
PrintingCompose     printing   5530     0.76   0.23   1.22      30%   FAIL
PrintingCompose     card       4256     1.00   0.55   1.80      46%
```

By plan × acquire branch, worst cell not already covered above:

```
plan             acquire branch          n     median   p10    p90   within 25%
StreamedSelect   card_range_popcount    668     0.76   0.40   1.76      13%   FAIL
```

## 1. `GatheredScan`/card's remaining `scan_units` error (biggest lever)

Extracted into its own doc — see
[local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md).
It's the biggest lever (worst median, worst agreement, highest frequency of any cell in the table) and
is now the subject of an ongoing iteration ledger, not a single-pass fix — see that doc for the current
best, the constraints (pre-computation requirement, the price-triple correlation risk), and the round-
by-round log.

## 2. `StreamedSelect`/`card_range_popcount` (worst other cell, cheap to check)

**Population**: only 668 rows, but the worst agreement rate anywhere (13% within 25%, median 0.76).
Not investigated at all this session — everything focused on `PrintingCompose`'s `GatheredScan`/
`StreamedSelect` competition, not the range-acquired path.

**Investigation plan**: `CardRangePopcount`'s own row (668, median 0.80, 51% within 25%) is
reasonably healthy, so the miscalibration is specific to how `StreamedSelect` is COSTED when a bare
range wins the acquire — i.e. `acquire_plan_features`'s `CardRangePopcount` branch's `eval_domain`/
`scan_units`/`compose_scan_printings` feed into `StreamedSelect`'s OWN cost arm (`plan_cost`), not
into `CardRangePopcount`'s. Start by reading that branch's `mk_plan_feats` call
(`card_engine/src/lib.rs`, `PhysicalPlan::CardRangePopcount.applicable` arm) alongside
`cost::plan_cost`'s `StreamedSelect` arm, and check with a live `explain()` probe on a handful of
one-sided range queries (`usd>5`, `cn>100`) whether the feature or the coefficient is the mismatch —
same first move as every fix that shipped this session.

**Risk**: low — small population, likely narrow root cause given `CardRangePopcount` itself is fine.

## 3. `PrintingCompose`/printing mode (unexamined mode)

**Population**: 5,530 rows, 30% within 25%, median 0.76 (FAIL). Every fix this session was scoped to
`Mode::Card` — printing mode's own `.result.printing`-consuming leaf arms and its `scan_all`/
`nothing_to_verify` paths have not been separately audited the way `.card` was.

**Investigation plan**: re-run the same shape-based bucketing
(`profile_scan_units_bulk.py`-style, but filtered to `unique=="printing"`) to find which AST shapes
dominate. Printing mode has no "first match wins" dedup semantics at all (`GatheredScan` under
printing mode returns every matching printing, not one per card — see `push_card_matches`'s
non-`Mode::Card` arms), so the card-invariant depth-1 fix does NOT apply here; whatever the dominant
error shape turns out to be, it needs its own mechanism, not a port of this session's fix.

**Risk**: unknown until the shape breakdown runs — treat as a fresh investigation, not an extension.

## 4. The shared `GatheredScan` p90 tail (card 2.73, printing 2.61, artwork 2.66)

**Observation**: all three modes show a very similar p90 (~2.6-2.7x under-costed at the tail), despite
having different median behavior (0.68 / 0.81 / 0.83). Similar magnitudes across otherwise-different
populations is suggestive of one shared root cause — e.g. a specific acquire branch, paging decision,
or query shape that feeds `GatheredScan` identically regardless of `unique` — rather than three
separate long tails that happen to coincide.

**Investigation plan**: before assuming this is real, check population overlap directly — pull the p90
rows from each mode's sample and see whether they cluster on the same query shapes (a `Not`, a wide
`Or`, a specific acquire branch like `plane` or `printing_range_scan`). If they do, fixing that one
shape moves all three cells at once, which is worth knowing before scoping items 1-3 as `Mode::Card`
only. If they don't overlap, this is coincidence and each mode's tail is a separate, lower-priority
problem than its own median.

**Risk**: this is a triage step, not a fix — low cost, and it changes how items 1 and 3 should be
scoped if the tails turn out to share a cause.

## Explicitly considered and rejected: exact intersection for lone non-arith + arith leaves

`cmc=2 c=g`-shaped queries (exactly one plane-compilable card-invariant leaf — color/border/legality/
rarity — ANDed with one-or-more `cmc`/`power`/`toughness` leaves) have no path to an exact card count
today: `compose_printing_estimate`'s `best_other` intersection requires **2+** non-arith card-invariant
leaves before it even starts, and the arith-merge that would otherwise combine a lone non-arith leaf
with the arith side is itself gated on `best_other` already existing. Relaxing the `>= 2` threshold to
`== 1` (paired with at least one arith sibling) closes the gap logically and was implemented, but a
targeted acquire-time A/B (not just `scan_units` accuracy) found a **23.6x acquire-time regression** on
exactly the newly-admitted population (median 875ns → 20,646ns), against a flat control population
(2+ non-arith leaves, unaffected) that moved 10,625ns → 10,792ns — noise. Reverted; not committed. The
mechanism (an unconditional `eval_planes`/`popcount_with_bits` pass now paid by a much larger query
population than before) is the same class of failure as the historical 2.33x regression documented
inline at the arith-merge site (a different over-eager widening, reverted for the same reason).

A cheaper, sampled/capped version (probe the first N ids and extrapolate) was also considered and
rejected for now: it would still pay a nonzero floor (the `compile_plane`/`eval_planes` setup cost, not
just the ID-probe loop that scales with match count), it would stop being *exact* — which defeats the
purpose, since `card_invariant_domain_exact`'s whole argument depends on zero false positives — and
there is no evidence yet that this specific gap's accuracy loss actually changes any routing decision.
Worth reconsidering only after item 1 ships and the pooled agreement table is re-measured; if this
shape still shows up as a meaningful contributor, a sampled version with an explicit error bound (not
disguised as exact) would be the right design, not a resurrection of the reverted attempt.
