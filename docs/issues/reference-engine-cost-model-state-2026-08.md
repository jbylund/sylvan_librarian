# Cost Model State of the Engine — 2026-08-31 Snapshot

Round 26 of the accuracy/routing effort. This is a comprehensive diagnostic sweep, not a fix — no code
changed. Goal: find where the cost model is still wrong, ranked by real routing impact, so Round 27
targets the actual biggest remaining lever. History read before measuring anything:
[local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)
(Phase 1, Rounds 0-10), [00852](00852-engine-compose-acquire-p3-p4-ranking.md) (resolved),
[local-engine-plane-acquire-compose-costing.md](local-engine-plane-acquire-compose-costing.md) and
[local-engine-plane-scope-printing-compose-executor.md](local-engine-plane-scope-printing-compose-executor.md)
(Phase 2, "don't build this"), and
[done/local-engine-gathered-scan-undercosted-arith-existential-and.md](done/local-engine-gathered-scan-undercosted-arith-existential-and.md)
+ [local-engine-domain-cards-existential-arith-and.md](local-engine-domain-cards-existential-arith-and.md)
(Rounds 15-25, `PairTotals` shipped, joint refit failed a third time on clean data).

Measured against an isolated release build of `costcell/trunk` @ `f5aed0a0` (`maturin build --release`,
extracted wheel, `PYTHONPATH`-pinned — never `maturin develop` into the shared `.venv`), corpus
`benchmarks/bitplanes/corpus.jsonl` (97,812 printings, primary checkout, read-only).

**A surprise not in the required reading**: while cross-referencing, a second, independently-run
effort turned up covering `PrintingCompose`'s own walk/build cost model (`Perm`/`OrderbyWalk`,
"Sigma" work) — 6+ open/reference docs, 5 commits in the current `git log` head (#1009, #1060-#1065).
It is not in this doc's mandated reading list and this round did not audit it in depth, but the regret
matrix below shows it covers by far the largest share of real routing regret in the engine right now,
so it is called out explicitly rather than silently ranked against.

## Full agreement table

`bench_cost_model_agreement.py --seconds 300 --seed 0`, 107,516 queries sampled.

```
plan                acquire                   n   median     p10     p90  within 25%
CardRangePopcount   card_range_popcount    1700     0.81    0.70    0.89        52%
GatheredScan        printing_compose      59128     1.15    0.48    5.24        24%
GatheredScan        candidates            38867     0.76    0.47    1.98        30%  FAIL
GatheredScan        plane                  5540     0.70    0.60    2.60        18%  FAIL
GatheredScan        printing_range_scan    2271     1.08    0.82    1.88        62%
GatheredScan        card_range_popcount    1700     0.97    0.59    1.44        51%
PlanePopcountOrder  plane                  5540     0.77    0.54    0.97        36%  FAIL
PrintingCompose     printing_compose      28237     0.82    0.54    1.25        46%
PrintingCompose     plane                  4155     0.72    0.36    2.52        17%  FAIL
PrintingCompose     printing_range_scan    2090     0.70    0.03    1.02        27%  FAIL
PrintingCompose     card_range_popcount    1483     1.21    0.89    1.67        53%
PrintingRangeScan   printing_range_scan    1042     0.92    0.52    2.55        39%
StreamedSelect      printing_compose      42429     1.05    0.14    2.76        37%
StreamedSelect      candidates            29017     0.82    0.54    1.11        48%
StreamedSelect      plane                  5540     0.92    0.83    3.13        72%
StreamedSelect      card_range_popcount    1700     1.10    0.60    1.76        40%
StreamedSelect      printing_range_scan    1682     1.10    0.76    2.08        53%
12/17 cells inside [0.8, 1.25]
```

By-unique table: 9/12 cells inside band; `GatheredScan/card` reads 0.80 (right at the boundary — the
`[0.8,1.25]` gate reads it FAIL, consistent with this cell hovering at exactly this line since Round 9).

Annotation:

| cell | status | disposition |
| --- | --- | --- |
| `GatheredScan`/`printing_compose` (1.15, 24%) | unchanged since Round 9 | **already covered** — Round 25's parked "residual ~1.5-2x compound-existential-plane" miscalibration, needs a saturating/banded rate, explicitly out of scope this round |
| `GatheredScan`/`card` (0.80, 25%, FAIL at boundary) | unchanged since Round 9 | **already covered** — same population as above, pooled by unique instead of acquire |
| `GatheredScan`/`candidates` (0.76, 30%, FAIL) | unchanged since Round 9's partial fix | **already covered, still open** — Round 9 fixed the zero-match fixed-cost mechanism; Round 8's mechanism 2 (card-mode residual-selectivity discount) was never shipped. See "Ranked candidate list" below — this is the one already-diagnosed item worth a fresh look |
| `GatheredScan`/`plane`, `PlanePopcountOrder`/`plane` (0.70/0.77, FAIL) | new cells, not previously called out per-plan | **checked, routing-inert** — pairwise ordering confirms `PlanePopcountOrder` wins the `Plane`-acquire argmin essentially always at 0.00µs regret regardless of these absolute-cost errors (see Pairwise section). Extends the already-known "absolute costing under `Plane` acquire doesn't matter" finding from `PrintingCompose` (Round 12/13) to the other two plans in that branch |
| `PrintingCompose`/`plane` (0.72, 17%, FAIL) | unchanged | **already covered** — Round 12/13, structurally excluded from the `Plane`-acquire argmin, fixing the costing changes nothing |
| `PrintingCompose`/`printing_range_scan` (0.70, 27%, FAIL) | new cell | **checked, low impact** — pairwise ordering shows this pair at 98-99% ordered right, 0.13-0.24µs mean regret, n≈678, negligible real share |

## Regret matrix, ranked by SHARE

`bench_regret_matrix.py --seconds 180 --mode realistic --seed 0`, 81,119 multi-plan queries, **total
regret 61.8ms, mean 0.76µs**.

```
acquire                n  miss%   mean    SHARE
printing_compose   21944     8%   2.40      85%
candidates         43154     1%   0.21      15%
printing_range_scan  315     3%   0.38       0%
plane              14338     0%   0.00       0%
card_range_popcount 1368     0%   0.00       0%

compose paging branch (every row where a compose cost was priced)
Perm        9832  12%  3.94   63%
Gather     57707   1%  0.18   17%
OrderbyWalk 3682   8%  1.97   12%
Decline     9898   2%  0.55    9%

picked -> best (only when they differ)
StreamedSelect -> GatheredScan   1597  66%  21.88µs   57%
PrintingCompose -> StreamedSelect 269  98%  31.22µs   14%
GatheredScan -> PrintingCompose   368  85%  19.35µs   12%
PrintingCompose -> GatheredScan   183  99%  33.47µs   10%
StreamedSelect -> PrintingCompose 196  54%  19.34µs    6%
```

Ranking:

1. **`printing_compose` acquire, 85% of all regret (~52.5ms).** Essentially every real routing loss in
   the engine funnels here. Decomposes into the paging-branch table: `Perm` 63% share, `OrderbyWalk`
   12%, the rest low-severity default/decline buckets.
   - `Perm`'s share (~38.9ms) is **already covered** by the separate, currently-active "Sigma" effort
     (see Context above) — not re-examined in depth this round; recommend Round 27 not duplicate it.
   - `OrderbyWalk`'s share (~7.4ms) is **already covered** by an existing, fully-designed-but-unshipped
     fix — [local-engine-compose-paging-cost-based.md](local-engine-compose-paging-cost-based.md)
     ("let compose choose OrderbyWalk vs Gather on cost, not on shape"). This round's feature-accuracy
     pass (below) supplies a fresh, previously-missing quantification of part of why: `printings_walked`
     is badly wrong specifically for card/artwork mode on this branch.
   - `picked -> best` mismatches (`StreamedSelect <-> GatheredScan`, `GatheredScan <-> PrintingCompose`)
     are the SAME phenomenon Rounds 1-9 repeatedly confirmed as "unchanged" in confirmation runs — never
     the subject of their own investigation, always used as a regression check. **Already covered** in
     substance (this is the Round 25-parked compound-existential-plane miscalibration showing up as real
     regret); this round's contribution is the precise sizing (57% of ALL regret share alone,
     `StreamedSelect -> GatheredScan`, ~34.9ms/mean 21.88µs/miss 66%) confirming it as by far the largest
     single item, just one this effort has already tried and shelved four times.
2. **`candidates` acquire, 15% of all regret (~9.3ms).** **Already covered, still open** — Round 8's
   mechanism 2 (card-mode residual-selectivity discount never shipped). See ranked candidates below.
3. Everything else (`printing_range_scan`, `plane`, `card_range_popcount`) — 0% share, confirmed
   negligible.

## Pairwise ordering

`bench_pairwise_ordering.py --seconds 300 --seed 0`, realistic and uniform, by acquire branch.

```
pair / acquire                                     ordered right   mean regret
GatheredScan vs PrintingCompose  [plane]        realistic 85%   19.12µs   uniform 82%  28.35µs
PrintingCompose vs StreamedSelect  [plane]      realistic 92%   11.26µs   uniform 86%  16.55µs
GatheredScan vs StreamedSelect  [printing_compose] realistic 92% 2.67µs   uniform 92%   2.58µs
GatheredScan vs PrintingCompose  [printing_compose] realistic 90% 3.03µs  uniform 87%   5.12µs
PrintingCompose vs StreamedSelect [printing_compose] realistic 95% 1.84µs uniform 96%   1.76µs
GatheredScan vs PlanePopcountOrder [plane]       realistic/uniform 100%  0.00-0.01µs
PlanePopcountOrder vs StreamedSelect [plane]     realistic/uniform 100%  0.00µs
GatheredScan vs PrintingCompose [printing_range_scan]  99%  0.14-0.24µs   n≈678-2077
```

- **`[plane]` pairs are the worst-ordered in the whole engine (82-92%) but confirmed structurally
  inert** — same reachability check Round 12/13 already did for `PrintingCompose`: `PlanePopcountOrder`
  wins the real argmin under `Plane` acquire 100% of the time against both `GatheredScan` and
  `StreamedSelect`, at 0.00-0.01µs regret, in both modes. **Already covered.**
- **`[printing_compose]` pairs are the real, reachable regret** — 87-96% ordered right, 1.76-5.12µs mean
  regret, tens of thousands of rows. This is the reachable form of item 1 in the regret ranking above.
  **Already covered** (the parked compound-existential-plane fit).
- **`[printing_range_scan]`** — checked for reachability per the task's own instruction (don't repeat
  Round 12's mistake): tiny population (n≈678-2077), 98-99% ordered right, negligible mean regret.
  **Not a candidate.**

## Feature accuracy

`bench_feature_accuracy.py --seconds 180 --mode uniform --seed 0`, 450,772 feature-rows. First use of
this tool this session. Ratio is feature/counter; >1 over-counts.

```
feature (pooled)              n        p10   p50   p90  p90/p10
compose_scan_printings      1694       0.17  1.47  6.46    37.0  OVER-COUNTS
scan_units                135821       0.09  0.70  1.23    13.3  UNDER-COUNTS
printings_walked           44580       0.09  0.85  2.47    28.0
matches                   136656       0.99  1.00  2.36     2.4
eval_domain               132021       1.00  1.00  2.16     2.2

printings_walked <compose OrderbyWalk> / card       3399   p50=0.23   UNDER-COUNT ~4.3x
printings_walked <compose OrderbyWalk> / artwork    3387   p50=0.22   UNDER-COUNT ~4.5x
printings_walked <compose Perm>       / card       10324   p50=0.96
printings_walked <compose Perm>       / artwork    10441   p50=1.03
printings_walked <compose OrderbyWalk> / printing   5796   p50=1.05
```

- `scan_units` pooled under-count (median 0.70, wide spread) — **already covered**, the era-correlated
  print-position confound for bare existential leaves (Round 17/20/25), plus the printing-varying range
  depth work (Rounds 1-9). No new mechanism found here.
- `matches`/`eval_domain` pooled near-1.0 median with a fat right tail (p90 2.16-2.36, p99 10-13,
  p100 up to 122) — **already covered**, the residual card-invariant/existential-arith-AND population
  Rounds 15-25 partially fixed and Round 25 confirmed the remaining rate-fit still fails.
- **`printings_walked <compose OrderbyWalk>`, card/artwork mode: genuinely under-examined, but not
  undiscovered.** Card and artwork mode read median 0.22-0.23 (real walk length ~4.3-4.5x the
  predicted feature) while printing mode reads ~1.0-1.05. The ~4.3-4.5x gap matches the corpus's own
  `printings_per_card`/`printings_per_artwork` constant almost exactly, and this is **exactly** the gap
  [reference-engine-compose-perm-cards-visited-estimator.md](reference-engine-compose-perm-cards-visited-estimator.md)
  names in its own "Next" section as still unresolved: "when [the cards_visited rate] does go in,
  `OrderbyWalk`'s own accuracy needs re-checking in the same pass, since it shares `COMPOSE_WALK_STEP_NS`
  with `Perm` and neither the kernel nor the reconciliation regression says anything about its
  `cards_visited` shape (`resolutions`, a different quantity)." This round's number is the first
  concrete measurement of that named-but-ungraded gap: **already covered by name, freshly sized here.**
- `compose_scan_printings` OVER-counts badly (median 1.47, p99 34x) under the compose `Gather` paging
  branch specifically, small population (n≈1694, 671-831 in the finer slices). Cross-checked against
  the regret matrix's compose-paging-branch table: `Gather`'s mean regret is only 0.18µs despite a huge
  row count, i.e. low severity — the feature error is real but does not currently translate into much
  real regret. **New, minor, not worth ranking above the items below.**

## Ranked candidate list

Genuinely open, unaddressed items — ranked by real regret share where measured this round. All of
these are already named somewhere in the doc tree (this repo has 25+ rounds of history); "candidate"
here means "not yet shipped, not yet deliberately parked as unproductive," not "undiscovered."

1. **`printing_compose` acquire's `Perm`+`OrderbyWalk` paging-branch miscalibration — 63%+12% = 75% of
   ALL measured routing regret (~46ms of 61.8ms).** By far the largest number in this whole sweep. Not
   a recommendation for the domain-cards round-clock specifically: `Perm`'s share is under active work
   by a separate, mature effort (6+ docs, 5 recent commits, "Sigma" decision rule just validated on
   real-shaped traffic). `OrderbyWalk`'s share has a fully-designed, unshipped fix sitting in
   [local-engine-compose-paging-cost-based.md](local-engine-compose-paging-cost-based.md), and this
   round's feature-accuracy data adds the card/artwork-specific sizing that doc's own sibling
   ([reference-engine-compose-perm-cards-visited-estimator.md](reference-engine-compose-perm-cards-visited-estimator.md))
   flagged as still needed. **Flagging for cross-effort awareness, not claiming as a domain-cards
   finding** — if the round-clock issuing this brief has any flexibility to redirect effort, this is
   where the real money still is, by an order of magnitude over anything below.
2. **`GatheredScan`/`candidates` residual-selectivity discount (Round 8's mechanism 2, never shipped) —
   15% of all regret (~9.3ms), cost-model-agreement FAIL (0.76 median, 30% within 25%, unchanged since
   Round 9).** Squarely within the domain-cards/GatheredScan effort's own turf (`cost.rs`'s
   `GatheredScan` arm, the same file Round 9 already touched for the zero-match fixed cost). Round 9
   fixed one of Round 8's two named mechanisms; this is the other one, still open, still real, and now
   has a fresh regret-share number attached. **Top recommendation for Round 27** if it stays inside the
   domain-cards effort's own scope, given item 1 is someone else's active turf and the compound-
   existential-plane fit (below) is explicitly parked.
3. **The compound-existential-plane `GatheredScan` cost-formula miscalibration** (Round 25's parked
   negative result) — sized precisely by this round's regret matrix at 57% of ALL regret share alone
   just for the `StreamedSelect -> GatheredScan` mismatch (~34.9ms, mean 21.88µs, miss 66%), plus another
   ~28% split across the other three compose-acquire mismatch directions. This is explicitly out of
   scope per this round's own brief (four discarded fit attempts; needs a saturating/banded rate, not a
   flat linear one) — listed here only so the size of the parked issue is visible against the rest of
   this ranking, not as a live recommendation.
4. **`compose_scan_printings` over-count under the compose `Gather` paging branch** — real (median 1.47,
   p99 34x) but low measured severity (Gather's mean regret is 0.18µs despite a large row count). Minor,
   not worth a dedicated round on its own; worth a one-line note for whoever next touches
   `COMPOSE_GATHER_SPAN_PER_MATCH`/`COMPOSE_GATHER_BITTEST_PER_PRINTING_NS`.

## Explicitly not candidates

Checked this round, confirmed already covered or immaterial — not to be re-discovered by a future
round:

- **`GatheredScan`/`printing_compose` and `GatheredScan`/`card` cost-agreement cells** — same population
  as candidate 3 above; the parked Round 25 negative result.
- **`GatheredScan`/`plane` and `PlanePopcountOrder`/`plane` cost-agreement FAILs** — confirmed
  routing-inert via pairwise ordering (100% ordered right, 0.00-0.01µs regret against both competitors
  under `Plane` acquire, both realistic and uniform mode). Extends Round 12/13's "`PrintingCompose`'s
  absolute cost under `Plane` acquire doesn't matter" finding to the other two plans sharing that
  branch — `PlanePopcountOrder`'s near-free popcount wins regardless of how any competitor is priced.
- **`PrintingCompose`/`plane`** — Round 12/13, structurally excluded from the `Plane`-acquire argmin.
- **`PrintingCompose`/`printing_range_scan`** — checked for reachability (per the task's explicit
  instruction not to repeat Round 12's mistake): real but tiny population (n≈678-2077), 98-99% ordered
  right, negligible mean regret. Not worth a round.
- **`scan_units` pooled under-count, `matches`/`eval_domain` fat right tail** — both already covered by
  the Rounds 1-25 history (printing-varying range depth work, era-correlated print-position confound,
  the parked existential-AND rate fit).
- **The `Or`/negation/nested-paren population** — Round 8 flagged this as invisible to
  `bench_cost_model_agreement.py`'s flat-conjunction sampler and to every tool this round used (all four
  draw from `QuerySampler`, same limitation). Still unmeasured by this round for the same reason; not
  re-discovered, not newly sized either.

## Reproducing

```bash
maturin build --release --out <scratch>/wheels   # in card_engine/, isolated wheel, never maturin develop
PYTHONPATH=<scratch>/wheels-extracted .venv/bin/python scripts/bench_cost_model_agreement.py \
  --seconds 300 --seed 0 --corpus benchmarks/bitplanes/corpus.jsonl --shm-path <scratch>/store
# same pattern for bench_regret_matrix.py --seconds 180 --mode realistic,
# bench_pairwise_ordering.py --seconds 300 --mode realistic|uniform,
# bench_feature_accuracy.py --seconds 180 --mode uniform
```
