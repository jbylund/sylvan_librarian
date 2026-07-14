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

1. Always run the two binary searches (cost: ~free) to get k.
2. Include the child when `k < best` (it becomes the new driver; every unit
   of k below best is a saved driver verification), or when k is under a
   small floor (sorted-vec cost is noise).
3. Skip when k is broad AND `best <= AND_SKIP_THRESHOLD` (current behavior).
4. Rank-2 complements keep their existing rule (sole-source only).

This subsumes today's behavior for broad children at the cost of two binary
searches, and captures the measured ~2x for selective ones. The win is
largest when the And carries an expensive residual (unmemoized text
predicates), since each avoided verification then costs more than a numeric
compare.

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
