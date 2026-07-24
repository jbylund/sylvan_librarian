# Probe Range Selectivity Before the And-Skip Decision

Follow-up to the cost-guard calibration
([engine-cost-guard-calibration](./00647-engine-cost-guard-calibration.md), PR #647).
The And-skip guard discards free selectivity information, and the calibration
sweeps measured ~2x headroom at the current operating point.

## The flaw

`narrow_rec`'s And arm (lib.rs ~2270) skips all rank>0 children once any
gathered set is <= `AND_SKIP_THRESHOLD` (2,048). Rank classifies children by
*worst-case* materialization cost, so it lumps together:

- `usd>50` — k ≈ tens of matches; materializes as a sorted vec in O(k log k)
  (microseconds); would shrink 2,048 candidates to a handful.
- `usd<50` — k ≈ the whole index; near-zero narrowing value.

Both probe identically: `range_narrowed` (lib.rs ~1474) computes k from two
`partition_point` binary searches *before* materializing anything. The skip
decision fires before that probe and never sees k.

Note the broad side is already cheap post-#636 (scatter the smaller side +
complement; or decline outright when no printing-space partner sets
`broad_ok`). The waste is only the selective-child case: nearly-free, highly
selective ranges are skipped because their cost *class* is "expensive".

## Measured headroom

From the calibration sweeps (benchmarks/cost-guards/sweeps.csv, worktree
sylvan_guard_calibration): with a selective child (`usd<0.02`, ~2% of
printings), include beats skip 1.56x at knob=2,048 and 1.95x at 4,096 —
exactly where the current threshold operates. With a broad child
(`usd<0.2`), skip wins below ~8k. The sign flips with child selectivity,
which is why calibration could not move the single constant (raising it to
8,192 lost the real-query A/B: geomean 0.97, 4-8x tail regressions).

## Expected impact: ~none in linked-URL traffic; interactive mix unmeasured

Two measurements (2026-07-08) bound the value FOR THE WILD CORPUS. The
8,192 A/B over 180 wild queries is a strict upper bound on this proposal
(it includes every child the probe rule would, and more): zero queries
improved >1.10x; best was 1.09x on a 4.6us query (noise). The corpus
(14,473 distinct queries) contains exactly one price lower bound
(`tix>15.00`, single predicate — no And); its 568 range-predicate queries
are almost all `cn:` on name+set lookups — the shape the skip rule protects.

Representativeness caveat (raised by Joe, confirmed from provenance): the
wild corpus is the Common Crawl harvest of scryfall.com/search URLs
(build_wild_corpus.py) — queries *published as links* (deck-site printing
lookups), not queries *typed* by users. Interactive collector-style
conjunctions (`t:dragon usd>100`) leave no crawlable trace and are
systematically under-sampled, so the "~none" verdict does not extend to
interactive traffic. Ground truth would be `q=` params from the production
API access logs (TimingMiddleware may already pair them with latencies) —
extract before investing here, or before trusting any traffic-weighted
geomean in this repo's benchmarks.

Note the mix uncertainty cuts toward this proposal, not away: if the
interactive mix is rich in selective-child conjunctions, a bigger constant
still loses (4-8x on linked shapes); the probe rule is per-query optimal
and robust to whatever the mix turns out to be. Until the log extraction
says otherwise, treat this as robustness/option value, not latency.

## Proposed decision rule

For rank-1 range children, probe first, then decide:

1. Always run the two binary searches (cost: ~free) to get k (`probe_range_k`).
2. Include the child when `k < best` (it becomes the new, strictly-smaller
   driver — every unit of k below best is a saved verification, and it can
   never regress), or when `k <= AND_PROBE_FLOOR`.
3. Skip when k is broad AND `best <= AND_SKIP_THRESHOLD` (current behavior).
4. Rank-2 complements keep their existing rule (sole-source only).
5. Among rank-1 range children, probe all and process smallest-k first
   (removes the written-order sensitivity where whichever same-rank child
   appeared first paid materialization).

`AND_PROBE_FLOOR` shipped at **64**, not an arbitrary "small" value: `k < best`
is the driver-replacement win and can never regress; the floor is a second,
riskier admission (a range larger than the driver that can only shrink the
intersection if correlated). Sized to genuine timing noise — a ≤64-element
sorted vec builds in well under the benchmark's ~5% floor even under a
one-card driver — so it can never reproduce the 8,192-experiment tail: the
`!"name" set:SLD cn:N` canary's `cn:N` sets are far larger than 64 in the
common (small collector-number) case and still skip under a tiny exact-name
driver. It is bounded well under `AND_SKIP_THRESHOLD`, so it can never
re-admit the broad children that guard protects.

This subsumes today's behavior for broad children at the cost of two binary
searches, and captures the measured ~2x for selective ones. The win is
largest when the And carries an expensive residual (unmemoized text
predicates), since each avoided verification then costs more than a numeric
compare.

## Measured (implementation, baseline fc222fd)

Targeted harness (`scripts/bench_cost_guards.py` and_skip family, median of 3
reps, fresh subprocess per branch) — the drift-free comparison, since it
forces include-vs-skip via `CARD_ENGINE_AND_SKIP_THRESHOLD` on one build.
Ratio is skip ÷ include (>1 ⇒ skipping cost us; ~1 ⇒ equivalent):

| corpus / child | driver K | baseline skip/incl | branch skip/incl |
| --- | --- | --- | --- |
| independent `usd<0.02` (selective) | 1024 | 1.19× | 1.19× |
| independent `usd<0.02` (selective) | **2048** | **1.79×** | **1.00×** |
| independent `usd<0.02` (selective) | **4096** | **2.08×** | **1.00×** |
| independent `usd<0.2` (broad) | 2048 | 0.68× | 0.65× |
| independent `usd<0.2` (broad) | 4096 | 0.63× | 0.62× |
| correlated `usd<0.2` (broad) | 512–4096 | 0.31–0.46× | 0.31–0.49× |

The selective child's ~1.8–2.1× skip penalty at the 2,048–4,096 operating
point is eliminated (→1.00×); the broad child is unchanged (skip still
correctly wins — no regression). At K=1024 the branch still skips the
selective child (1.19×, unchanged): there k≈1,944 > best, so including it
can't lower the driver — the probe makes the per-query-optimal call, not a
blind include. Crossover sits exactly at k, as it should.

Broad survey (`scripts/survey_queries.py`, 520 queries): no measurable
systematic effect. The aggregate min-ms shift (~0.92× geomean) is cross-run
machine drift on a co-tenanted box — untouched single-predicate queries
(`artist:avon`, `type:planeswalker`, `name:angel`), which the And arm never
touches, moved by the same ~0.82× as everything else. One genuine mover
stands out above the drift: `(tou=5 usd>50) or (name:counter set:otj)`
improved **2.99×** (118 µs → 39 µs) — the selective-range-under-selective-
driver And (`tou=5 usd>50`), exactly the shape this change targets. Matches
the design's "~none on the wild corpus, real for interactive collector-style
conjunctions" prediction. Full Rust suite (debug + release) green, incl. the
#677 differential row-identity fuzzer and a new `and_skip_probes_range_selectivity`
unit test.

## Considerations

- Tightness bookkeeping: including a probed child keeps
  `every_child_included` semantics unchanged; skipping still clears it.
- Price ranges are never tight (widened f32 bounds — lib.rs ~1470); the
  included vec is a superset, which is fine (driver verifies).
- The probe order among multiple rank-1 children should prefer smallest k
  (probe all, sort by k) — this also removes the written-order sensitivity
  where whichever same-rank child appears first pays materialization.
- Validate with the existing harness: `scripts/bench_cost_guards.py` and_skip
  family (selective + broad + correlated children) plus the wild-query A/B in
  `scripts/bench_guard_validation.py`; the `!"name" set:SLD cn:N` tail shapes
  from the 8,192 experiment are the regression canary.
