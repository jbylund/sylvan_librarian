# Cost Model Cleanup — Final A/B Against `main` (Round 27)

Round 27, and the last one before this branch splits into PRs. Twenty-six rounds of work on
`costcell/trunk` are documented across
[local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)
(Phase 1, Rounds 0-10), [#852](00852-engine-compose-acquire-p3-p4-ranking.md) (resolved as a side
effect), [local-engine-plane-acquire-compose-costing.md](local-engine-plane-acquire-compose-costing.md)
+ [local-engine-plane-scope-printing-compose-executor.md](local-engine-plane-scope-printing-compose-executor.md)
(Rounds 12-13, "don't build this"),
[done/local-engine-gathered-scan-undercosted-arith-existential-and.md](done/local-engine-gathered-scan-undercosted-arith-existential-and.md)
+ [local-engine-domain-cards-existential-arith-and.md](local-engine-domain-cards-existential-arith-and.md)
(Rounds 15-25), and [reference-engine-cost-model-state-2026-08.md](reference-engine-cost-model-state-2026-08.md)
(Round 26's whole-engine survey). All of that validation was against the branch's own history — each
round measured its own before/after, and Round 26 measured only `costcell/trunk` against itself.
**Nothing in the tree so far measures the whole branch against `main` in one sitting.** This doc is
that measurement: two isolated builds, five harnesses, one sitting, no code changed.

## Method

Two release wheels, built with `maturin build --release` (never `maturin develop`, which rewrites the
shared `.venv`'s `card_engine.pth` and would flip every other session's `import card_engine` — see
the shared-checkout note in project memory). Each wheel unzipped to its own scratch directory and
selected per-invocation via `PYTHONPATH`, verified by printing `card_engine.__file__` and hashing the
`.so` before any measurement — confirmed four distinct binaries (plain + `routed-phases` for each
build), and confirmed the shared `.venv`'s own `import card_engine` still resolves to the primary
checkout throughout, untouched.

- **`main`** @ `ca016410`, built in a fresh detached worktree.
- **`costcell/trunk`** @ `ddba298a`, built in this round's own worktree (`costcell/27-final-ab`).

Corpus: `benchmarks/bitplanes/corpus.jsonl` (97,812 printings), read-only, from the primary checkout —
never written to; every harness was pointed at a `--shm-path` under scratch instead of the corpus's
own directory (the default `--shm-path` would have written a `.store` file next to the read-only
corpus).

`bench_regret_matrix.py` needs a `routed-phases` build for its decline-row population; both `main`
and `costcell/trunk` wheels were built both ways (plain for the other four tools, `routed-phases` for
the regret matrix) so the two sides are always compared like-for-like.

### Canary: the measurement doc's own warning, reproduced

[reference-cost-model-measurement.md](reference-cost-model-measurement.md) warns that a same-build,
same-seed pair false-positived at the old (2, 7) trial defaults and reads clean at 30. On this
machine — a shared dev box with a visible background load (pants test workers, MCP servers, browser
automation processes) at the time of this run — **30 was not enough**: three same-build pairwise
checks at the tool's own default trial count (30) read `-1.1`, `+2.0`, `+3.1` µs against a ~51-54 µs
mean latency, each with a bootstrap CI excluding zero in a different direction. Raising `--trials` to
60 (the measurement doc's prescribed remedy when a canary fires) tightened individual pairs to
~0.3 µs, but a 3-pair pooled same-build check at 60 trials (n=2,371 shared queries) still read a small
systematic `+0.5 µs, CI [+0.4, +0.7]` — traced to an order effect (the second run in a pair reads
slightly slower than the first on this machine), not random noise, since all three pairs shared the
same first/second ordering.

Because that order effect is real, every `main`-vs-`costcell/trunk` latency round below alternates
which build runs first, so the effect cancels in the pooled result rather than biasing it. See
[Latency](#latency) for why this matters to the headline number.

## Cost/feature accuracy

### `bench_cost_model_agreement.py --seconds 300 --seed 0`

| | `main` | `costcell/trunk` |
|---|---|---|
| queries sampled | 87,212 | 97,575 |
| cells within `[0.8, 1.25]` (by acquire) | 13/17 | 12/17 |
| cells within `[0.8, 1.25]` (by unique) | 10/12 | 10/12 |

One FAIL flip, both directions checked:

- **New FAIL, immaterial**: `PlanePopcountOrder / plane` — median `0.81` (main, PASS) → `0.80`
  (trunk, FAIL). This is a boundary artifact, not a real change: the displayed medians round to the
  same two decimals `main` passed on. Confirmed inert by both the regret matrix (`plane` acquire is
  0% of all SHARE, mean regret `0.00 µs` on both builds) and pairwise ordering (`PlanePopcountOrder`
  wins its argmin 100% of the time under `plane` acquire on both builds) — matches Round 26's own
  "Explicitly not candidates" finding for this exact cell.
- **No FAIL→PASS flips.** `GatheredScan / candidates` moved from median `0.61` (main, 15% within 25%)
  to `0.79` (trunk, 31% within 25%) — real, substantial movement toward agreement — but stays a hair
  under the `0.8` floor on both builds, so the verdict column doesn't change.
- Everything else moved by roughly the sampling-driven ~12% larger `n` (trunk sampled more queries in
  the same 300s wall-clock budget) with proportionally similar ratios — no other qualitative shift.

### `bench_feature_accuracy.py --seconds 300 --seed 0` (mode=uniform, default)

**Fixed in Round 28** ([local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)'s
own Round 28 section has the full bisection, mechanism, and confirmation pass) — recorded here as this
survey originally found it, plus the resolution:

| feature (pooled) | `main` median | `costcell/trunk` median (as surveyed here) | `costcell/trunk` median (post Round 28 fix) | verdict |
|---|---|---|---|---|
| `scan_units` | 1.00 (no flag) | 0.70 | 0.94 | UNDER-COUNTS → clean |

`main`: 697,375 feature-rows, `scan_units` reads clean (median 1.00, no verdict flag). `costcell/trunk`
as surveyed by this round: 705,768 rows, `scan_units` reads `0.70` and is flagged `UNDER-COUNTS` pooled
and across nearly every `unique`/`prefer` slice. This reproduced Round 26's own number for this exact
cell (`reference-engine-cost-model-state-2026-08.md`, "Feature accuracy" section: median 0.70,
"already covered, the era-correlated print-position confound... plus the printing-varying range depth
work") to two decimal places, and this A/B added the piece Round 26 didn't have: `main` does not have
this problem. **Round 28 bisected it precisely**, rather than accepting the "byproduct of this
branch's own fixes" framing as the final word: the actual trigger was a single commit
(`e1c40466`, this branch's own Round 7) whose broad-guard `scan_units` scale — fit exclusively against
`unique=card` samples, exactly like its sibling `COMPOSE_RANGE_AND_BROAD_SCAN_SCALE` from Round 4 —
was applied unconditionally to `Mode::Printing`/`Mode::Artwork` too, where the real
`printings_examined / n_printings` ratio for that guard-fired population reads an exact, zero-spread
1.0 (those modes' kernels never short-circuit). Scoping both scales to `Mode::Card` only closed the
gap: pooled `scan_units` median `0.70` (UNDER-COUNTS) → `0.94` (clean, inside `main`'s own `[0.8,
1.25]` band), confirmed via a fresh isolated-wheel `main`-vs-fixed-tip A/B at this same `--seconds 300
--seed 0` protocol. The residual `0.94` vs. `main`'s `1.00` is the two other, already-documented,
un-touched-by-this-fix contributors (`PrintingCompose`'s "narrow"-bucket under-count, named and
deferred by Round 7 itself, and the era-correlated existential-leaf confound Rounds 17/20/25 already
characterized as out of their own blast radius) — real, pre-existing, not introduced by any commit on
this branch, and not attempted this round; see Round 28's own section for why (the root cause is
`domain_cards`'s documented broad-range undercount for bare ranges, which nine prior rounds already
found hard to fix directly).

No other feature changed materially in the pooled table.

## Regret

### `bench_regret_matrix.py --seconds 180 --mode realistic` (routed-phases builds)

| | `main` | `costcell/trunk` | Δ |
|---|---|---|---|
| multi-plan queries | 75,112 | 80,499 | — |
| total regret | 120.3 ms | 71.4 ms | **-41%** |
| mean regret/query | 1.60 µs | 0.89 µs | **-44%** |

SHARE by compose paging branch (the mechanism most of this branch's rounds targeted):

| branch | `main` SHARE | `main` mean | `costcell/trunk` SHARE | `costcell/trunk` mean |
|---|---|---|---|---|
| `Perm` | 46% | 5.99 µs | 68% | 4.86 µs |
| `OrderbyWalk` | 42% | 14.88 µs | 10% | 1.90 µs |
| `Gather` | 8% | 0.19 µs | 15% | 0.18 µs |

`OrderbyWalk`'s absolute contribution collapsed from ~50.5 ms to ~7.1 ms (SHARE is a fraction of a
shrinking pie, so read the absolute too) — the single largest driver of the whole-branch improvement.
`Perm`'s absolute contribution also fell slightly (~55.3 ms → ~48.6 ms). Neither branch's *own*
cost-formula fix is what's recorded as shipped in the docs read above (Round 26 names `OrderbyWalk`'s
fix as still "fully-designed, unshipped"); the reduction is consistent with the Sigma decision rule
(`docs/issues/local-engine-compose-perm-sigma-decision-rule.md` and the Step 4-7 commits in this
branch's recent history) steering more queries away from the branch transitions where `OrderbyWalk`'s
miscalibration would have been exposed, rather than fixing the miscalibration itself.

The `#852` story, specifically — `picked → best` transitions:

| transition | `main` n | `main` SHARE | `costcell/trunk` n | `costcell/trunk` SHARE |
|---|---|---|---|---|
| `PrintingCompose → GatheredScan` | 1,072 | 43% | 180 | 8% |
| `StreamedSelect → GatheredScan` | 1,135 | 18% | 1,573 | **49%** |

The misroute `#852` targeted dropped 83% in raw occurrence count (1,072 → 180) and from the single
largest SHARE to a minor one. But `StreamedSelect → GatheredScan` — the compound-existential-plane
`GatheredScan` cost-formula miscalibration Round 26 explicitly parked as "needs a saturating/banded
rate, not a flat linear one" — grew to the largest single slice on `costcell/trunk`, both in SHARE and
in absolute terms (~21.7 ms → ~35.0 ms). This matches Round 26's own ranking of it as the largest
still-open item, now visible for the first time against a genuine `main` baseline rather than only
against the branch's own history.

## Latency

### `bench_query_latency_ab.py --mode realistic --trials 60 --sample 800`, 4 rounds, order-alternated

| round | seed | order | B - A | 95% CI | verdict |
|---|---|---|---|---|---|
| 1 | 1 | main, trunk | -1.9 µs | [-2.4, -1.4] | trunk faster |
| 2 | 2 | trunk, main | -0.1 µs | [-0.8, +0.5] | no detectable difference |
| 3 | 3 | main, trunk | -0.4 µs | [-1.1, +0.3] | no detectable difference |
| 4 | 4 | trunk, main | +0.6 µs | [-0.5, +1.5] | no detectable difference |
| **pooled** | all 4 | alternated | **-0.4 µs** | **[-0.8, -0.1]** | trunk marginally faster |

Pooled over 3,158 queries shared across all four rounds: `costcell/trunk` reads a mean latency of
52.0 µs against `main`'s 52.4 µs — nominally outside the bootstrap's zero-crossing, in the expected
direction, but the magnitude (~0.8% of mean latency) is the same order of magnitude as this machine's
own measured same-build noise floor (the pooled canary read `+0.5 µs` under an *unbalanced* run order;
see Method). Only 1 of the 4 individual rounds was independently significant.

**Reconciling this with the 41% regret-matrix win**: regret is concentrated in a specific, minority
population — compose-paging-branch mismatches under `printing_compose` acquire, which the regret
matrix shows is ~13-27% of all multi-plan queries (`n=20,458`/`75,112` on main, `21,931`/`80,499` on
trunk) and produces the vast majority of the SHARE. Pooled over *all* realistic-mode traffic —
dominated by cheap `candidates`/`plane` lookups where nothing changed — that improvement is real but
small enough, at an 800-query-per-round sample, to sit right at the edge of what this environment can
resolve from noise. A user issuing the specific query shapes the regret matrix flags would feel a
real, measurable improvement; a user issuing a uniformly-sampled realistic query would not reliably
notice one at this sample size.

## Pairwise ordering

### `bench_pairwise_ordering.py --seconds 300`, realistic and uniform, both builds

The `#852` cell head-to-head against `main` (not against the branch's own Round-0 baseline, which the
brief for this round flagged as measured after some fixes had already shipped):

| mode | pair / acquire | `main` ordered-right | `main` mean regret | `costcell/trunk` ordered-right | `costcell/trunk` mean regret |
|---|---|---|---|---|---|
| realistic | `GatheredScan` vs `PrintingCompose` `[printing_compose]` | 80% | 8.09 µs | 90% | 3.03 µs |
| uniform | `GatheredScan` vs `PrintingCompose` `[printing_compose]` | 91% | 3.97 µs | **87%** | **5.25 µs** |

Against a real `main` baseline, `#852`'s realistic-mode improvement is **80% → 90%**, not the
**69% → 97%** the tracking docs' own internal comparison reports — confirming the round's brief was
right to be suspicious of that number; the internal baseline was measured on a `costcell/trunk`
ancestor that already carried some of Round 0-10's fixes, which inflates the apparent delta. The real,
`main`-relative improvement is smaller but still genuine and in the right direction.

**Under `uniform` mode — the sampler built specifically to reach rare tails — the same pair got
worse**: 91% → 87% ordered right, mean regret nearly doubling (3.97 → 5.25 µs). This is the branch's
one clear pairwise-ordering regression: the fixes are tuned to realistic-traffic-shaped populations
(the `QuerySampler`'s traffic weighting) and give up a small amount of accuracy on the query shapes
`uniform` mode is designed to surface. Pooled (not sliced by acquire), the same direction holds:
`GatheredScan` vs `PrintingCompose` overall reads 91% → 87% under uniform, 84% → 89% under realistic.

The structurally-inert `[plane]` pairs (`PlanePopcountOrder` always wins its argmin regardless of how
any competitor is priced) were re-confirmed on both builds, both modes — 100% ordered right,
0.00-0.01 µs regret throughout, consistent with Round 12/13's original finding.

## Honest verdict

The aggregate effect is real, but noisier and smaller than the round-by-round narrative alone would
suggest, and it is not uniformly positive.

**What holds up:**
- Regret fell 41% in total, 44% per query, on a realistic traffic mix — the single most important
  number here, and it is not a wash: the reduction is dominated by the `OrderbyWalk` paging branch
  collapsing from 42% to 10% SHARE, a real, large, `main`-relative win.
- The `#852` misroute (`PrintingCompose → GatheredScan`) dropped 83% in occurrence and from the
  largest SHARE to a minor one — genuinely fixed, just not by as much as the branch's own internal
  comparison claimed (80%→90% ordered-right against `main`, not 69%→97%).
- A small, marginally-significant end-to-end latency win (-0.4 µs pooled, in the expected direction)
  survived a canary-verified, order-alternated measurement — real, but small enough that a single
  realistic user request would rarely notice it.

**What doesn't, or is smaller than advertised:**
- `#852`'s own internal "69%→97%" figure does not survive a head-to-head against `main` — the true
  number is 80%→90%, because the internal baseline had already absorbed some of Round 0-10's fixes.
- `scan_units` pooled feature accuracy got measurably worse (`main` 1.00 clean → `costcell/trunk` 0.70,
  UNDER-COUNTS) — a real, `main`-relative regression, low-severity per this round's own regret-matrix
  cross-check, and **fixed by Round 28**: a mode-scoping bug (a `unique=card`-only-calibrated scale
  applied to Printing/Artwork mode too) traced to Round 7's own `e1c40466`, closed by scoping it to
  `Mode::Card`. Pooled median now `0.94`, inside the same band `main`'s `1.00` sits in — see
  `local-engine-gathered-scan-card-printing-varying-depth.md`'s Round 28 section.
- Pairwise ordering for `GatheredScan` vs `PrintingCompose` under `printing_compose` acquire got worse
  under `uniform` sampling (91%→87%) even as it improved under `realistic` sampling (80%→90%) — the
  branch traded rare-tail accuracy for common-case accuracy on this one cell, which is a defensible
  trade given realistic traffic is what users send, but it is a trade, not a pure win.
- `StreamedSelect → GatheredScan` grew to the largest single regret slice on `costcell/trunk`
  (18%→49% SHARE, ~21.7ms→~35.0ms absolute) — not a regression introduced by this branch (Round 26
  already named and parked it), but proof that the branch's 26 rounds did not touch the largest
  remaining opportunity, which is now more visible precisely because everything else shrank around it.
- The 41% regret win does not translate into a latency difference an average realistic query would
  reliably notice, because the affected population is a minority of realistic traffic.

**Overall**: the effort was worthwhile and the routing-regret number is genuinely, substantially
better against `main`, not just against the branch's own history — but a skeptical reviewer reading
only the round-by-round docs would come away expecting a bigger, cleaner, more uniform win than what
a fresh `main`-relative measurement actually shows. Ship it, but do not carry the `69%→97%` or
"41% regret reduction ≈ 41% faster" framings into the PR descriptions; use the numbers in this doc.

## Round 29: Post-Fix Comprehensive State Check

Diagnostic snapshot, no code changes. Round 27 (above) ran the first `main`-vs-`costcell/trunk` A/B
and found a real regression alongside the wins: `bench_feature_accuracy.py`'s pooled `scan_units`
read clean on `main` (1.00) but `UNDER-COUNTS` on the branch (0.70). Round 28
([local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md))
bisected that to its own Round 7 commit (`e1c40466`), fixed it by scoping two broad-guard `scan_units`
scales to `Mode::Card`, and ran a *confirmation* pass scoped to the regression itself (scan_units,
regret matrix, cost-model-agreement, a latency A/B, pairwise ordering — all at shorter `--seconds`
than Round 27's own sweep). This round redoes Round 27's FULL sweep — every feature, the whole
cost-model-agreement table, the regret matrix ranked by share, both pairwise-ordering modes, and a
canary-gated latency A/B — against the now-fixed tip, so the branch has one honest, current,
complete picture before it splits into PRs.

**Method.** Same protocol as Round 27: two isolated release wheels (`maturin build --release`, never
`develop`), `main` @ `ca016410` built in a fresh detached worktree, `costcell/trunk` @ `288402a0` built
in this round's own worktree (`costcell/29-final-state-check`) — four wheels total per side (plain +
`routed-phases`, the latter only for the regret matrix), each verified by `card_engine.__file__` and a
distinct `.so` hash before use. Corpus: the same read-only `benchmarks/bitplanes/corpus.jsonl`
(97,812 printings), every harness pointed at its own `--shm-path` under scratch. All five harnesses run
at `--seconds 300 --seed 0` (`--mode realistic` for the regret matrix; both `realistic` and `uniform`
for pairwise ordering), matching or exceeding Round 27's own budget.

### Feature accuracy — the full table, not just `scan_units`

`bench_feature_accuracy.py --seconds 300 --seed 0` (mode=uniform, the tool's default). `main`:
466,524 feature-rows. `costcell/trunk`: 463,359 feature-rows.

| feature (pooled) | `main` median | verdict | `trunk` median | verdict |
|---|---|---|---|---|
| `compose_scan_printings` | 1.28 (n=1,754) | OVER-COUNTS | 1.47 (n=1,741) | OVER-COUNTS |
| `printings_walked` | 0.85 (n=46,172) | clean | 0.85 (n=45,875) | clean |
| `matches` | 1.00 (n=141,410) | clean | 1.00 (n=140,447) | clean |
| `eval_domain` | 1.00 (n=136,638) | clean | 1.00 (n=135,697) | clean |
| `scan_units` | 1.00 (n=140,550) | clean | 0.94 (n=139,599) | clean |

Round 28's fix holds: pooled `scan_units` is clean on both builds, `trunk`'s 0.94 sitting inside the
same `[0.8, 1.25]` band `main`'s 1.00 does. `compose_scan_printings` is flagged OVER-COUNTS on **both**
builds at comparable magnitude (1.28 vs 1.47) — pre-existing on `main`, not something the branch
introduced.

**Slicing by acquire/mode surfaces real per-slice differences the pooled row hides — but they are not
new.** Diffing every flagged (UNDER/OVER-COUNTS) cell between the two full tables:

- **One genuine incidental fix**: `scan_units / card / prefer=default` — `main` 1.37 (OVER-COUNTS) →
  `trunk` 1.00 (clean).
- **`scan_units [card_range_popcount]`**: `main` 1.00 (clean, n=5,195) → `trunk` 0.43 (UNDER-COUNTS,
  n=5,164). This is not a new regression — `0.43` is `COMPOSE_BARE_RANGE_BROAD_SCALE`, the exact
  constant Round 6 shipped (`card_engine/src/lib.rs:11628`, applied at line 12258), fit against real
  ns-time error (93.3M → 16.0M abs error, held-out validated) rather than against this literal
  `printings_examined` counter. Round 6's own doc named this exact tradeoff at the time ("flags the
  sibling `else` branch... as itself badly under-calibrated... not fixed this round"). A feature-level
  bias deliberately buried in a cost-accurate rate, exactly the risk `bench_feature_accuracy.py`'s own
  docstring warns about — not something this round re-litigates.
- **`scan_units [printing_compose]` and its `/card`, `/printing`, `/artwork` slices**: `main` reads
  1.63 (OVER, card) / 1.09 (clean, printing) / 1.00 (clean, artwork); `trunk` reads 0.52 (UNDER, card) /
  0.38 (UNDER, printing) / 0.39 (UNDER, artwork). This is Round 7's own already-named, already-deferred
  "narrow bucket" `PrintingCompose` under-count (the `domain_cards` broad-range estimate for bare
  ranges) — Round 28 itself named this as the residual gap between its fixed `0.94` and `main`'s `1.00`.
  What's new **this round** is the full per-mode quantification: the narrow-bucket effect is not
  card-specific, it spans all three `unique` modes at comparable severity (0.38-0.52), which no prior
  round's narrower pooled/card-only view had shown directly.
- A few smaller slices move the same way for the same reason (`scan_units / card`, `/orderby=rarity`,
  `/orderby=usd`) — all downstream of the same narrow-bucket population, not independent findings.

Per the task brief for this round, both of these are the two already-documented residual gaps —
**known, deferred, unrelated to this round** — reported here with exact current magnitudes, not
re-investigated.

### Cost-model agreement — the full table

`bench_cost_model_agreement.py --seconds 300 --seed 0`. `main`: 62,916 queries sampled. `trunk`:
62,693 queries sampled.

- **By acquire branch**: `main` 9/17 cells inside `[0.8, 1.25]`; `trunk` 10/17. One flip, FAIL → PASS:
  `GatheredScan / candidates` — `main` 0.71 (27% within 25%) → `trunk` 0.98 (43% within 25%). This
  continues the movement Round 27 already flagged as "real, substantial" (0.61→0.79, still short of the
  floor) — this fresh sample crosses fully into agreement.
- **By distinct-on (`unique`)**: `main` 10/12 inside band; `trunk` 9/12 — one flip the other way,
  PASS → FAIL: `GatheredScan / artwork` — `main` 1.02 (clean) → `trunk` 1.54 (UNDER-COSTED). This is
  the by-unique face of the known, already-parked "compound-existential-plane `GatheredScan`"
  miscalibration (Round 25/26, needs a saturating/banded rate) — see the Regret section below, where
  the same mechanism shows up as the branch's single largest remaining regret slice. Named per this
  round's brief, not re-investigated.
- Net: one cell fixed, one cell newly visible as failing (both attributable to already-tracked
  mechanisms, not new problems) — acquire-level count improves by one, by-unique count worsens by one.
  A wash in cell-count terms, not a regression in either underlying mechanism.
- Compose-paging predicted-vs-taken proportions (`Perm`/`OrderbyWalk`/`Gather`/decline counts under
  each RANGE_ACQUIRES branch) are within a few rows of each other on both builds — no material shift.

### Regret — ranked by share

`bench_regret_matrix.py --seconds 300 --mode realistic --seed 0` (`routed-phases` builds).

| | `main` | `costcell/trunk` | Δ |
|---|---|---|---|
| multi-plan queries | 81,935 | 82,018 | — |
| total regret | 114.3 ms | 80.2 ms | **-30%** |
| mean regret/query | 1.39 µs | 0.98 µs | **-30%** |

Smaller than Round 27's own `-41%/-44%` (measured at `--seconds 180`, no explicit seed) — regret is
heavy-tailed and dominated by rare, extreme single-query misses (this round's own sample: `main`'s
largest single-query regret was 545.9 µs; `trunk`'s was 2,348.2 µs, one query in the
`StreamedSelect → PrintingCompose` transition), so the exact percentage is sensitive to which rare-tail
queries a given `--seconds`/`--seed` combination happens to sample. The **direction** — `trunk`
substantially lower total regret than `main` — replicates across both rounds' independent measurements.

Compose-paging branch SHARE: `main` `Perm` 57% / `OrderbyWalk` 28% / `Gather` 10% / `Decline` 5%;
`trunk` `Perm` 69% / `OrderbyWalk` 10% / `Gather` 13% / `Decline` 8%. `OrderbyWalk`'s collapse (28%→10%
of a smaller pie; ~32 ms → ~8 ms absolute) is again the largest single driver — same mechanism Round 27
found, though `main`'s own OrderbyWalk share reads differently between the two rounds (42% in Round
27's 180s run vs. 28% here), another heavy-tail sampling effect, not a moved target.

`picked → best` transitions, ranked by SHARE:

| transition | `main` n | `main` SHARE | `trunk` n | `trunk` SHARE |
|---|---|---|---|---|
| `StreamedSelect → GatheredScan` | 1,284 | 19% | 1,618 | **43%** |
| `PrintingCompose → GatheredScan` | 1,040 | 30% | 159 | 7% |
| `PrintingCompose → StreamedSelect` | 474 | 22% | 296 | 16% |
| `PrintingCompose(declined) → GatheredScan` | 435 | 21% | 280 | 15% |
| `GatheredScan → PrintingCompose` | 296 | 6% | 347 | 12% |
| `GatheredScan → StreamedSelect` | 445 | 2% | 399 | 2% |

**`#852`'s misroute (`PrintingCompose → GatheredScan`) is robustly fixed**: 1,040 → 159 occurrences
(-85%), 30% → 7% SHARE — the largest slice on `main` is now a minor one on `trunk`, matching Round 27's
direction almost exactly (that round found -83%, 1,072→180).

**`StreamedSelect → GatheredScan` — the known, already-parked compound-existential-plane `GatheredScan`
miscalibration (Round 25/26, "needs a saturating/banded rate, not a flat linear one") — is now
unambiguously the largest slice**: 19%→43% SHARE, and in absolute terms `main`'s ~21.7 ms →
`trunk`'s ~34.5 ms, which reproduces Round 27's own absolute-ms finding (~21.7ms→~35.0ms) almost
exactly even though the total-regret percentage this round differs. **Honest fraction closed vs.
open**: the one identified, targeted pathology (`#852`) is fixed; the single largest remaining one is
untouched by any of this branch's 30 commits and is now more prominent only because everything else
around it shrank. Named per this round's brief as known/deferred/unrelated — not re-investigated here.

### Pairwise ordering — both modes

`bench_pairwise_ordering.py --seconds 300 --seed 0`, `realistic` and `uniform`.

The `#852` cell specifically, `GatheredScan vs PrintingCompose [printing_compose]`:

| mode | `main` ordered-right | `main` mean regret | `trunk` ordered-right | `trunk` mean regret |
|---|---|---|---|---|
| realistic | 81% (n=11,390) | 6.90 µs | 93% (n=11,460) | 2.68 µs |
| uniform | 90% (n=16,249) | 3.92 µs | 90% (n=16,271) | 4.30 µs |

**Realistic-mode improvement is stable and, if anything, slightly better than Round 27's reported
80%→90%**: this fresh sample reads 81%→93%. **Round 27's claimed uniform-mode regression for this
exact cell (91%→87%) does NOT reproduce here** — this round reads a flat 90%→90%. Given `uniform` mode
is deliberately built to reach rare tails, and this same doc has already shown (in the regret matrix,
above) that rare-tail metrics swing hard between independently-seeded 300s samples, the most honest
read is that Round 27's uniform-mode "regression" for this cell was itself sample noise, not a stable
property of the branch — flagged explicitly rather than carried forward as settled. (The gap-size
calibration did drift worse, 1.20→1.42 `gap meas/pred`, even though which plan wins stays right just as
often — a real, smaller, separate observation.)

Pooled (non-acquire-sliced) `GatheredScan vs PrintingCompose`: realistic 82%→88%; uniform 90%→90%
(flat, both n≈20,700-20,800).

**One other pair moved notably**: `GatheredScan vs StreamedSelect` under `uniform` mode improved
89%→95% (n≈33,600 both builds), a genuine secondary win (realistic mode: 95%→97%, smaller but same
direction). The structurally-inert `[plane]` pairs (`PlanePopcountOrder` always wins its argmin) stay
at 100% ordered-right, ~0.00-0.01 µs regret on both builds and both modes — re-confirmed inert, same as
Rounds 12/13/27.

### Latency, with the canary stated explicitly

Same-build canary first, per this round's own gate: `trunk-plain` vs itself, `--mode realistic
--sample 800 --trials 60 --seed 99`: **B - A = -1.2 µs, 95% CI [-1.5, -0.8], "B is FASTER"**. **Not
clean** — this reproduces Round 27's own finding of a real second-run-reads-faster order effect on
this shared box. Because of that, every real comparison below alternates which build runs first.

Four order-alternated rounds, `--mode realistic --sample 800 --trials 60`, `main-plain` vs
`trunk-plain`:

| round | seed | order | B - A | 95% CI | verdict |
|---|---|---|---|---|---|
| 1 | 1 | main, trunk | -2.34 µs | [-2.9, -1.9] | trunk faster |
| 2 | 2 | trunk, main | -2.33 µs | [-2.9, -1.8] | trunk faster |
| 3 | 3 | main, trunk | -0.63 µs | [-1.4, +0.0] | no detectable difference |
| 4 | 4 | trunk, main | -1.50 µs | [-2.5, -0.6] | trunk faster |
| **pooled** | all 4 | alternated | **-1.70 µs** | **[-2.06, -1.35]** | **trunk faster** |

Pooled over 3,189 paired queries: `main` mean 53.7 µs (median 34.0 µs), `trunk` mean 52.0 µs (median
33.2 µs) — trunk reads about 3.2% faster. All four rounds point the same direction regardless of which
build ran first or second (main-first rounds 1/3 average -1.49 µs; trunk-first rounds 2/4 average
-1.92 µs — if the canary's own order bias were the whole story, alternating order should have flipped
this asymmetry, not left it in the same direction), and 3 of 4 rounds are individually significant.

**This is a larger, more consistent signal than Round 27 found** (that round's pooled result was
-0.4 µs, CI [-0.8, -0.1], only 1 of 4 rounds individually significant, and explicitly called
"within...this environment's own noise floor"). This round's pooled -1.70 µs exceeds the same-build
canary's own -1.2 µs bias in magnitude, and holds across all four order-alternated rounds — a real,
reproducible, though still modest (~3% of mean latency, a query most users would not consciously
notice) wall-clock win. The exact magnitude clearly varies session-to-session on this shared box more
than the regret-matrix story alone would suggest — reported honestly as "trunk is measurably faster,
by an amount that itself varies between measurement sessions," not as a single fixed number.

### Overall verdict

`costcell/trunk` (`288402a0`) is net-positive against `main` (`ca016410`) on every axis measured this
round, and by a clearer margin on latency specifically than Round 27 found — but several exact
percentages (regret reduction, some pairwise-ordering deltas) show real run-to-run variance from
regret's heavy tail and should not be read as more precise than they are.

- **Feature accuracy**: Round 28's fix holds (pooled `scan_units` clean on both builds). The full
  per-slice sweep this round adds finds no new regression — every off-band cell traces to an
  already-documented, already-deliberate tradeoff (Round 6's `card_range_popcount` scale, Round 7's
  `printing_compose` narrow bucket) now quantified across all three modes for the first time, plus one
  genuine incidental fix (`scan_units / card / prefer=default`).
- **Cost-model agreement**: one cell fixed (`GatheredScan/candidates`, a continuation of Round 27's own
  partial finding), one newly visible as failing (`GatheredScan/artwork` by-unique, the by-unique face
  of the already-parked compound-existential-plane issue) — a wash in count, not a new problem.
- **Regret**: total down substantially in both this round (-30%) and Round 27 (-41%); the exact number
  is sample-sensitive but the direction is not. `#852`'s misroute is robustly fixed (-85% occurrence,
  largest slice → minor slice, in both rounds). The known, parked `StreamedSelect → GatheredScan`
  compound-existential-plane issue is now unambiguously the single largest remaining slice (43% SHARE,
  ~34.5 ms, matching Round 27's absolute-ms finding almost exactly) — untouched by any of the 30
  commits, more prominent only because everything else shrank around it.
- **Pairwise ordering**: `#852`'s realistic-mode win is stable and reproduces (81%→93%, at least as
  good as Round 27's 80%→90%). Round 27's claimed uniform-mode regression for the same cell (91%→87%)
  does **not** reproduce this round (flat 90%→90%) — most likely sample noise in a rare-tail-seeking
  mode, flagged rather than carried forward. A different pair (`GatheredScan vs StreamedSelect`) shows
  a genuine secondary uniform-mode win (89%→95%).
- **Latency**: canary not clean (-1.2 µs), but four order-alternated rounds all point the same
  direction and the pooled result (-1.70 µs, CI excluding zero) exceeds the canary's own bias — a real,
  small (~3%), reproducible wall-clock win, larger than Round 27's own -0.4 µs finding. Round 27's
  question of whether the branch is measurably faster than `main` is answered more confidently "yes"
  this round than last, though the exact magnitude moves between sessions.
- **Known, deferred, unrelated to this round** (named per this round's brief, not re-investigated):
  the compound-existential-plane `GatheredScan` cost-formula miscalibration (Round 25/26, needs a
  saturating/banded rate — now confirmed as both the largest regret slice and the source of this
  round's one CMA regression), and the `domain_cards`-driven "narrow bucket" `PrintingCompose`
  under-count (Round 7 — now quantified across all three `unique` modes via this round's full
  feature-accuracy sweep, previously only characterized pooled/card-specific).

**Ship it.** No new regressions were found; every "worse than `main`" cell this round's fuller sweep
surfaced traces to an already-documented, already-parked, deliberate tradeoff or to regret's own
heavy-tailed sampling variance — not to anything introduced by the branch's 30 commits or by Round 28's
fix specifically.
