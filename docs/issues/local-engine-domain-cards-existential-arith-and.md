# `domain_cards`/`eval_domain` Is Wrong for Arith-Range AND Existential-Leaf, and Now Has a Root Cause

## Round 25: the Blocker Is Confirmed Cleared, the Joint Refit Was Retested on Clean Data, and It Still
## Fails — a Third, More Precise Negative Result

Round 20 named the recommended next step explicitly: fix `eval_domain` first, then the
`GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS`/leaf-count-rate joint refit (Rounds 17/19 built the
mechanism, both found a naive fit didn't survive held-out validation) becomes testable for real. Rounds
22/24 did that fix. This round is the first to actually run the refit against the now-clean data — and
it still fails, for a reason more precise than either prior round could see with a corrupted
`eval_domain`: **a flat, additive per-extra-leaf rate, multiplied by `eval_domain`, cannot serve both
ends of this population's `eval_domain` range at once** — `border:black`'s `eval_domain` (24,734, near-
universal selectivity) is ~50-100x `border:gold`'s or `f:oldschool`'s (a few hundred), and any single
rate that improves the small-`eval_domain` leaves overshoots the large ones by the same multiple. See
the parent doc (`docs/issues/done/local-engine-gathered-scan-undercosted-arith-existential-and.md`)'s
Round 25 section for the full mechanism, held-out numbers, and outcome — this section records only what
belongs here: confirmation that this doc's own `eval_domain` fix is holding up broadly, cleanly
separated from that round's (negative) refit attempt.

### `eval_domain` re-verified clean for population A, broadly, not just on the 429-row sweep

Re-ran a fresh, independent check (3 arith fields x 9 widths x 9 "clean" leaf values, 243 rows,
`explain_analyze` against a freshly-built store) rather than trusting Round 22/24's own reported
figures unchecked, per this round's brief:

```
cards_visited / eval_domain, population A (n=243): within 15% = 98.4%, median = 1.000
```

Matches Round 22's own 89.3%-broad-sweep finding (the gap here is narrower, expected: this sweep drops
the two already-documented degenerate leaves, `border:silver`/`r:special`, that Round 22 itself
excluded from its "clean" figure). **Confirms, independently, that Rounds 22/24's `PairTotals`/
`pair_range_sum` combination did what the parent doc's "What this means for the queued joint-rate
refit" section claimed**: `eval_domain` is not the reason the refit fails this round. The full negative
result and its real mechanism are in the parent doc, not duplicated here — see the link above.

### Population B's `scan_units` confound: confirmed real, confirmed severe, and a SEPARATE quantity
### from `eval_domain`

This doc's own "Open questions" section never covered `scan_units`; the brief for this round asked to
check it. Measured directly (27 bare-existential-leaf rows, no arith partner, `unique=card`):

```
printings_examined / scan_units, population B (n=27): within 15% = 0.0%, median = 3.950
```

Real and severe — `scan_units` (the feature `GATHER_SCAN_PER_ROW_NS` multiplies) under-predicts the
real printings scanned by a median ~4x for this population, confirming Round 17/20's own smaller-sample
finding (era-correlated print position violates the uniform-random-depth assumption `scan_all` makes)
at a larger, fresh sample. This is orthogonal to `eval_domain`/`domain_cards` (a candidate CARD count,
already fixed) — `scan_units` is a printing-SPAN estimate, a different mechanism, still unfixed, and
out of this round's `cost.rs`/`lib.rs`-tier-decision blast radius (it lives in `acquire_plan_features`'s
`scan_all`/`card_invariant_domain_exact` machinery, untouched by Rounds 15-24's `And`-arm work). The
refit script accounted for it explicitly (substituted the realized `printings_examined` counter for the
`scan_units` feature when computing calibration TARGETS, so this confound could not leak into the
CARD_PASS/FLOOR/LEAF fit the way `eval_domain`'s confound did in Round 20) rather than letting it
silently pollute the fit — see the parent doc for why that substitution still wasn't enough to make the
fit ship.

### Outcome

No code change in this doc's own blast radius (`lib.rs`'s `PairTotals`/`compose_printing_estimate`,
untouched this round, exactly as Round 24 left it). The refit attempt and its negative result belong to
`GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS`/`plane_extra_eval_leaves` (`cost.rs`/`lib.rs`'s tier
decision) and is written up in full in the parent doc's own Round 25 section — read that for the
mechanism, the numbers, and the recommended next step (a saturating/bounded leaf term or a per-
selectivity-band calibration, not a flat linear one).

## Round 24: `PairTotals` Extended to `cmc`/`power`/`toughness` — Round 22's Tax Closed for the Common
## Widths, Shipped

Round 23 found the right shape but didn't build it: `indexes.pair_totals` (`card_engine/src/lib.rs`)
already stores an EXACT per-value-pair total for `border`/`rarity`/`frame`/`legality`, and the same
disjoint-partition argument (a card has exactly one `cmc`/`power`/`toughness`) applies to those three
fields too — summing the per-value pair-total over every value a RANGE admits reproduces the true joint
exactly, no independence assumption. This round built that extension and shipped it.

### What changed

`card_engine/src/lib.rs`:
- **`PairTotals`**: three new dimension maps (`cmc: HashMap<u8,u16>`, `power`/`toughness:
  HashMap<i8,u16>`, mirroring `rarity`'s existing shape) plus three `_seen: Vec<u8>`/`Vec<i8>` lists —
  every DISTINCT value observed at all, before `PAIR_MIN_PRINTINGS` prunes the id maps. The `_seen`
  lists exist for one reason: without them, a range sum cannot tell "no card has this value" (safe,
  contributes zero) apart from "some card has this value but it was pruned" (unsafe — silently
  treating it as zero would undercount). A new `get_all` on `ArchivedPairTotals` returns all three
  spaces (printing/card/artwork) for one pair in one hash lookup, instead of `get`'s one-space-at-a-time.
- **`build_pair_totals`**: the three new dimensions ride the SAME single accumulation pass every other
  dimension already uses — one more per-value count in pass 1, one more `ids.push` in pass 2. `cmc`/
  `power`/`toughness` are read from the CARD (`OracleCard`), not the printing — confirmed directly from
  the struct layout that these fields are stored once per card, not per printing (unlike `border`/
  `rarity`, which vary by printing).
- **`pair_leaf_id`**: three new match arms (`Cmc`/`Power`/`Toughness`, `Eq` only, either operand order),
  mirroring rarity's existing `Eq`-only restriction. Feeds the EXISTING `pair_bounded_min` call site for
  free — a bare `cmc=1`-shaped leaf paired with any other pairable leaf (existential or not) is now
  answered there without any new call site.
- **`single_arith_field`** (new): the single `NumField` every one of a set of arith children agrees on,
  or `None` if they don't (a mixed `cmc>=1 power<=2`) or the set is empty.
- **`pair_range_sum`** (new): given bound leaves on ONE arith field and one existential leaf's own
  `pair_leaf_id`, sums `pt.get_all` over every value in that field's `_seen` list that the bounds admit,
  declining (`None`) the instant any admitted value lacks an id (pruned). Bounded by the field's own
  distinct-value count (~14-21 in this corpus), not by how the query phrases the range.
- **`compose_printing_estimate`'s `And` arm**: `card_invariant`/`existential` now carry the original
  `FilterExpr` alongside each compiled `PlaneExpr` (a new `CompiledLeaf` type alias), so `pair_range_sum`
  can ask `pair_leaf_id` about the lone existential leaf's own value without re-deriving it. A new
  `pair_range_answer` is computed FIRST, before Round 22's `best_other` loop, for exactly the shape Round
  22 fixed (`card_invariant.is_empty()`, exactly one existential leaf, arith children on one field); when
  it answers, Round 22's `popcount_with_bits`/arith-ID-probe-merge machinery is skipped entirely for that
  query. When it doesn't (a card-invariant leaf present, 2+ existential leaves, 2+ distinct arith fields,
  or a pruned value), Round 22's fallback runs completely unmodified.

### Exactness: 429/429 agreement where both paths apply

Re-ran Round 22/23's own 3-field × 13-width × 11-leaf-value sweep (429 rows) against two isolated
release wheels — `costcell/trunk`@`68f2cd7f` (Round 22's fix, pre-this-round) and this branch — reading
`engine.query()`'s own total (ground truth) and `explain()`'s `eval_domain` on both:

```
true_intersection: 0/429 mismatches between the two wheels (query correctness unaffected, as expected —
                    this round only touches cost ESTIMATION, never the executed result set)
eval_domain:        0/429 rows differ between before/after (both wheels answer exactly wherever either
                    can — no case where the new path disagreed with Round 22's exact fallback)
```

### Coverage: how much of the taxed population gets the new cheap path

Instrumented directly (temporary `eprintln!`, reverted before commit) to distinguish, per sweep row,
whether `pair_range_answer` fired, declined due to pruning, or the shape didn't match at all:

```
429 rows total
 33 (width=13 only): the And arm's existential logic isn't reached AT ALL for this width — a separate,
                      pre-existing fusion mechanism takes over once the range covers essentially the
                      whole corpus (same "collapses at width 13" behavior Round 22/23 already documented)
 72 (border:silver, r=special — the two ALREADY-DOCUMENTED degenerate leaves): the existential leaf
    itself never reaches `existential.len()==1` (a separate, pre-existing quirk in how these two
    specific values get classified upstream, unrelated to this round and out of its blast radius)
324 (the 9 "clean" leaf values × 3 fields × 12 widths): shape matches every time
  180 (55.6% of the 324): pair_range_answer fires — exact, cheap
  144 (44.4% of the 324): declines due to pruning, falls back to Round 22's exact (more expensive) path
```

By width (9 clean leaves × 3 fields = 27 rows/width): **widths 1-6: 100% hit (162/162). Width 7-8: 33%
hit (18/54) — only `cmc`, whose survivor set (below) extends one value further than `power`/
`toughness`'s. Widths 9-12: 0% hit (0/108), all correctly decline and fall back.** This traces exactly
to `PAIR_MIN_PRINTINGS` (1,024) pruning individual values, confirmed directly against the real corpus:

```
cmc survivors (>=1,024 printings):        0,1,2,3,4,5,6,7,8   (9 values)
power survivors:                          0,1,2,3,4,5,6        (7 values)
toughness survivors:                      1,2,3,4,5,6          (6 values)
```

A range up to width 6 stays within every field's survivor set; width 7-8 only `cmc` still clears (its
survivor set reaches 8); width 9+ exceeds all three. **This is a real, honest coverage boundary, not a
bug** — the population Round 22's fix taxed most heavily (wide ranges, per that round's own finding that
"the tax grows from +10,250ns at width 1 to +22,875ns at width 12") is exactly where this round's cheap
path covers LEAST — but the narrow-to-moderate ranges most plausible in real queries (`cmc<=3`,
`power>=1 power<=4`, ...) are exactly where it covers MOST, and that's the population this round
prioritized finishing over chasing the last few widths for diminishing returns.

### Acquire-time improvement: measured directly, same reproducers

Same sweep, `explain_analyze` acquire-time medians (20 warmups, 100 trials), before = Round 22's fix,
after = this round:

```
n=429, median delta (after-before): -771ns    mean: -11,907ns
p10/p50/p90/max: -35,208ns / -771ns / +583ns / +2,396ns   (min: -99,188ns)

by width:  1: -9,793ns   2: -12,958ns   3: -15,751ns   4: -18,584ns   5: -20,854ns   6: -21,333ns
           7:    -167ns   8:    -313ns   9:     +21ns  10:     +83ns  11:     +42ns  12:     +83ns
           (widths 7-12's near-zero median is the pruning cutoff above — most rows there fall back to
           the unchanged Round 22 path, correctly paying the SAME cost as before, not a regression)

by leaf (median): border:black -45,438ns  r=common -22,500ns  r=uncommon -20,980ns  r=rare -20,959ns
                   border:borderless -10,208ns  border:white -9,124ns  r=mythic -9,250ns
                   f:oldschool -8,896ns  border:gold -8,709ns   (border:silver/r=special: ~0, unaffected)
```

Named reproducers (before → after, `explain_analyze` median):

```
cmc=1 border:black:        50,146ns → 4,708ns   (10.6x faster)
cmc=1 border:white:        13,917ns → 4,666ns   (3.0x faster)
cmc=1 r=mythic:             14,500ns → 5,000ns   (2.9x faster)
cmc>=1 cmc<=5 border:black: 92,875ns → 5,333ns  (17.4x faster)
cmc>=1 cmc<=5 border:white: 28,146ns → 5,333ns   (5.3x faster)
cmc>=1 cmc<=5 r=mythic:     29,292ns → 5,625ns   (5.2x faster)
```

Every one of Round 22's own named reproducers is now answered by the new cheap path (width ≤5, well
inside every field's survivor set) — this closes essentially all of Round 22's OWN acquire-time tax on
its own flagship population, not just a marginal slice of it.

### Store-build-time cost: measured directly, negligible

5 reps each, full corpus reload (`benchmarks/bitplanes/corpus.jsonl`, 97,812 printings), same two wheels:

```
before median: 2.470s   after median: 2.511s   delta: +40ms (+1.6%) — inside this measurement's own
                                                 run-to-run spread (before ranged 2.380-2.540s across
                                                 its own 5 reps, a ~160ms band bigger than the delta)
archived store size:  before 72,402,040 bytes   after 72,435,480 bytes   delta: +33,440 bytes (+0.046%)
```

The transient build-time `n×n` co-occurrence array DOES grow more than the aggregate number suggests in
isolation — 22 new ids (9 cmc + 7 power + 6 toughness survivors) added to a pre-existing ~42, growing
`n_ids` to ~64 (a ~1.5x increase, ~2.3x for the `n²` array specifically) — but that transient array is a
small fraction of total reload time (JSON parsing + 22 other indices dominate), so the aggregate cost
lands at +1.6%, within noise. Accepted: real, measured, and small.

### Correctness gate

`cargo test --manifest-path card_engine/Cargo.toml`: **177/177 passed** (174 pre-existing + 3 new:
`pair_range_sum_sums_disjoint_values_and_declines_on_a_pruned_one`, `pair_leaf_id_resolves_cmc_power_
toughness_eq_and_declines_ranges`, `single_arith_field_agrees_only_when_every_child_is_the_same_field` —
all three exercise the new logic directly against a hand-built `PairTotals`, bypassing `PAIR_MIN_
PRINTINGS` entirely since a real fixture would need 1,024+ printings per value to clear it). `cargo test
--release`: **176/176 passed** (173 pre-existing + the same 3 new). Rounds 15/16/22's own regression
tests (`compose_tier_charges_border_existential_and_arith_range`, `compose_and_arm_tightens_lone_
existential_leaf_with_no_card_invariant_partner`) pass unchanged — that fixture is too small to clear
the floor, so it exercises Round 22's fallback path exactly as it did before this round, confirming the
new path declines cleanly rather than silently taking over. `cargo clippy --all-targets -- -D warnings`:
clean.

### Confirmation pass

`bench_pairwise_ordering.py --seconds 300`, both modes, before vs after — no material change:

```
realistic:  GatheredScan vs PrintingCompose   89%→89% ordered right, regret 9.29µs→9.77µs (flat)
            GatheredScan vs StreamedSelect    97%→97%, 0.80µs→0.81µs (flat)
            PrintingCompose vs StreamedSelect 94%→94%, 6.40µs→6.56µs (flat)
uniform:    GatheredScan vs PrintingCompose   87%→87%, 7.16µs→7.62µs (flat)
            GatheredScan vs StreamedSelect    95%→95%, 1.62µs→1.59µs (flat)
            PrintingCompose vs StreamedSelect 95%→95%, 4.13µs→4.41µs (flat)
```

`bench_cost_model_agreement.py --seconds 300 --seed 0`, full table:

```
by acquire:  12/17 cells inside [0.8, 1.25] → 12/17 (unchanged, no cell flips)
by unique:   10/12 cells inside [0.8, 1.25] → 10/12 (unchanged, no cell flips)
```

`bench_regret_matrix.py --seconds 120 --mode realistic --seed 0`:

```
before: 53,497 queries, total regret 42.9ms (mean 0.80µs)
after:  53,437 queries, total regret 41.1ms (mean 0.77µs)   -- improved 4.2%, same misroute categories,
                                                                no new outlier category
```

`bench_query_latency_ab.py --sample 400 --mode realistic --seed 7`, before vs after, plus a same-build
canary (before run 1 vs before run 2, interleaved: before1 → after1 → before2):

```
canary (before1 vs before2):  B - A = +1.3µs   95% CI [+0.9, +1.8]
before1 vs after1 (real):     B - A = +0.5µs   95% CI [+0.0, +1.0]
```

The real diff is SMALLER than the canary's own same-build noise — no detectable aggregate regression.
Consistent with the taxed population being a narrow slice (~0.85-1.23% of `Mode::Card` queries per
Round 21's proxy) of the realistic-mode sample this benchmark draws from.

### Scope decision: the multi-arith-field generalization (power×toughness, etc.) — not folded in

A real generalization exists and was checked against real data before deciding: `cmc`/`power`/
`toughness` are each single-valued per card, but so is their JOINT tuple — a card has exactly one
`(power, toughness)` pair, not a range of possible pairs — so the same disjoint-partition argument
extends to an `And` spanning TWO (or three) arith fields together, with or without an existential leaf.
Measured the actual cross-product size directly against `benchmarks/bitplanes/corpus.jsonl` (grouped by
printing, matching `PAIR_MIN_PRINTINGS`'s own counting unit):

```
(power, toughness):            122 distinct pairs, 13 clear the 1,024-printing floor
(cmc, power):                  141 distinct pairs, 11 clear the floor
(cmc, toughness):               144 distinct pairs, 14 clear the floor
(cmc, power, toughness):        497 distinct triples, 10 clear the floor
```

Small in every case — technically cheap, confirming the idea is sound and not a combinatorial trap.
**Not folded into this round anyway**, because it needs genuinely new plumbing beyond an extension of
what's already built: a compacted joint-value key (its own map, analogous to `legality`'s `(shift<<2)|
status` compaction, but for however many field combinations are worth covering), that key's OWN
pruning-safety `_seen` list, and a cross-product-aware version of `pair_range_sum` that enumerates BOTH
ranges' surviving values together — comparable in size to everything this round already built, for a
population narrower still than the single-field shape (a real query ranging power AND toughness
together, with exactly one existential leaf and nothing else card-invariant, is less common than the
already-narrow single-field case this round covers). **Correctness is unaffected either way**: Round
22's existing `arith_tuple_ids`-based probe-merge already answers the 2+-arith-field case EXACTLY today,
completely unchanged by this round (confirmed by reading the code path directly — `single_arith_field`
returns `None` for a mixed-field `arith_children`, so `pair_range_answer` stays `None` and the query
falls through to Round 22's unmodified fallback). This is a real, well-scoped follow-up for a future
round — a speed opportunity left on the table, not a correctness gap — and the measurements above are
that round's head start.

### What this means for the queued joint-rate refit

Round 20's blocked joint refit (`GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS`/leaf-count rate) needs
`eval_domain` to be trustworthy ground truth. Round 22 already cleared that for 89.3% of the broad sweep
(up from 5.8%). This round doesn't change that coverage number (it was already exact via Round 22's
fallback everywhere this round's new path doesn't reach) — what it changes is the ACQUIRE-TIME COST of
getting there for width ≤6-8 (the common case), not which rows are exact. **The refit's ground truth is
exactly as clean as it was after Round 22; this round makes reaching that ground truth cheaper for the
population it covers, and leaves the rest on Round 22's already-exact (just pricier) fallback.** No new
correctness caveat for whoever runs that refit next.

### Commit

One commit on `costcell/24-pair-totals-arith`. `git diff --stat costcell/trunk`: `card_engine/src/
lib.rs`, `card_engine/src/tests.rs`, this doc.

## Round 23: Is Round 22's Tax Avoidable? A Cheap Bound Investigated and Rejected, a Better Exact
## Alternative Found Instead — Not Shipped

Round 22 fixed correctness (`eval_domain` now exact for this population) at a real, measured acquire-time
cost (`popcount_with_bits` + the arith-ID-probe merge, `O(matching_ids)`, now running where it used to be
skipped). The question this round investigates: can a much cheaper `O(1)`/`O(log n)` estimate — e.g.
`min(exact_count(arith_leaf), exact_count(existential_leaf))`, each side's own already-cheap count — get
"good enough" routing without paying that tax? Investigation only; no code shipped. All numbers below are
from a real corpus (`benchmarks/bitplanes/corpus.jsonl`), two isolated release wheels (`costcell/trunk`@
`cc17e031`, pre-Round-22, and this branch's base `188a0ee4`, post-Round-22 — both built via `maturin build
--release`, no `make engine`/`maturin develop`), and a Python port of `cost.rs`'s `plan_cost` verified to
reproduce `explain()`'s own `predicted_ns` to 0.0000 relative error across 1,287 plan-rows before being
trusted for anything.

### 1. The taxed population, characterized broadly (not just the 6 reproducers)

Re-ran Round 22's own 3-field (`cmc`/`power`/`toughness`) x 13-width x 11-leaf-value sweep (429 rows,
`cmc>=1 cmc<=W` for `W` in 1..13, same 11 existential values), this time capturing real `explain_analyze`
acquire-time on BOTH wheels for every row, not just the 6 named reproducers:

```
n=429, median delta (after-before): +17,605ns   mean: +22,921ns
p10 / p90 / max: -83ns / +52,562ns / +95,230ns
median ratio (after/before): 4.61x   p90: 10.74x   max: 19.81x
```

By existential leaf (median delta, median ratio):

| leaf | median delta | median ratio |
|---|--:|--:|
| `border:black` | +69,958ns | 15.53x |
| `r=common` | +36,812ns | 8.22x |
| `r=rare` | +35,646ns | 8.05x |
| `r=uncommon` | +33,958ns | 7.68x |
| `f:oldschool` | +15,584ns | 4.49x |
| `r=mythic` | +17,500ns | 4.45x |
| `border:borderless` | +18,625ns | 4.94x |
| `border:white` | +16,229ns | 4.43x |
| `border:gold` | +15,334ns | 3.83x |
| `border:silver`, `r=special` | ~0ns | ~1.0x (degenerate — see below) |

`border:silver` never appears in this corpus at all (the real 5th border value is `yellow`; a labeling
miss carried over from Round 22's own sweep, not a new finding) and `r=special` is the separate,
already-documented `eval_domain`-flat-across-widths bug (Question 1's own doc, above) — both are
degenerate zero/near-zero-signal rows, correctly showing ~no delta. By width, the tax grows from
+10,250ns (width 1) to +22,875ns (width 12), then collapses to +500ns at width 13 (the range covers
essentially the whole corpus, so both before/after `eval_domain` saturate near `n_cards` and the two
converge). **`border:black`'s 15.53x/+69,958ns is the worst cell specifically because of its own 98.9%
selectivity**: the arith-ID-probe merge's cost scales with `O(matching_ids)`, and border:black keeps
almost every one of the arith leaf's own matching ids in play — the widest possible probe set.

Real-traffic prevalence: Round 21/22's own regex-proxy estimate (0.85-1.23% of `Mode::Card` queries) is
the best available figure for this specific "lone existential + arith, nothing else card-invariant"
population; narrowing it further would need an AST-level classifier over `QuerySampler` output, out of
scope for this round's budget.

### 2. Cheap `min()` vs exact: real gap, and the RATIO alone is misleading

The cheap estimate (`min` of each side's own bare exact count — `bare_numeric_field_count`/
`numeric_range_ids`, a genuine `O(log n)` two-`partition_point` lookup with no allocation, confirmed by
reading `numeric_range_count`'s body directly; and `value_totals.border`/`.legality` or
`rarity_cards.distinct_cards`, both O(1)/O(log n) too — so "cheap" is real, not hand-waved) was computed
for all 429 sweep rows and compared against the true joint intersection (`engine.query()`'s own `total`
for the AND filter — unambiguous ground truth, independent of any estimate):

```
zero-true-match rows excluded (39, degenerate): 390 remain
exact match (cheap == true): 0/390 (0.0%)
overestimate ratio (cheap/true): median 2.015x   p90 3.939x   max 27.579x
rows where cheap < true (would violate the upper-bound guarantee): 0/429
```

Cheap is *never* an underestimate (expected — it's a `min` of two supersets, a mathematically valid upper
bound) and *never* exact either. But **the ratio alone overstates which rows matter** — exactly the
failure mode the user flagged: a 16x error on a true value of 200 is a ~3,000-card absolute miss; a 2x
error on a true value of 5,500 is a larger, ~5,500-card absolute miss that the cost model actually feels.
By leaf (median true intersection, median cheap value, median ABSOLUTE delta, median ratio, and how many
of that leaf's 39 rows flip routing — see §4):

| leaf | med. true | med. cheap | med. abs. Δ | med. ratio | routing flips |
|---|--:|--:|--:|--:|--:|
| `r=rare` | 5,321 | 11,059 | **+5,709** | 2.078x | **23/39** |
| `r=uncommon` | 5,190 | 10,279 | **+4,886** | 1.981x | **23/39** |
| `r=common` | 5,897 | 10,694 | **+4,636** | 1.792x | **23/39** |
| `border:black` | 16,417 | 16,664 | +246 | 1.015x | 1/39 |
| `r=mythic` | 1,410 | 2,620 | +1,210 | 1.858x | 0/39 |
| `border:white` | 946 | 2,059 | +1,113 | 2.177x | 0/39 |
| `border:borderless` | 1,684 | 3,478 | +1,794 | 2.065x | 0/39 |
| `f:oldschool` | 382 | 961 | +579 | 2.516x | 0/39 |
| `border:gold` | 147 | 551 | +404 | 3.748x | 0/39 |
| `r=special` | 153 | 370 | +217 | 2.418x | 0/39 |

`border:gold`'s 3.748x median ratio is the WORST ratio among borders, and it flips *nothing* (absolute
miss ~400 cards). `r=rare/common/uncommon`'s ~2x ratios are unremarkable next to `border:gold`'s or
`r=mythic`'s, yet they cause every routing flip but one (§4) — because their bare existential-side counts
are ~10,000-11,000 cards (32-35% corpus selectivity), so even a "modest" 2x ratio is a multi-thousand-card
absolute error, and the cost model's per-candidate rate (~4-13ns/card across the competing plans) turns
that into tens of microseconds. **Absolute magnitude, not ratio, is what predicts real damage — confirmed
directly in §4, not asserted.**

**Anti-correlation check, done directly on this corpus's own fields**: the historical `id:br devotion:w`
case is a near-ZERO true intersection against two individually-large marginals (a hard contradiction —
`id:br` requires white in the identity color-wise never mind, the point is the near-zero intersection).
Nothing that extreme appears here (zero rows had `true_intersection == 0` except the degenerate
`border:silver` rows, where the leaf itself doesn't exist in the corpus). But a MILDER version of the same
effect is real and visible: `field=1 r=mythic` (a low arith value ANDed with mythic) is the worst-ratio
population (up to 27.579x, `toughness=1 r=mythic`, true=95 vs cheap=2,620) precisely because low-cmc/
power/toughness cards skew away from mythic rarity in this corpus (a plausible real card-design
correlation, not noise) — so `min()`'s independence assumption is measurably wrong here, just not
catastrophically so in absolute terms (see the table above: this shows up as a big ratio but a modest
absolute delta, and correctly causes zero routing flips).

### 3. A better alternative than `min()`: an EXACT disjoint-bucket sum, not a bound

`cmc`/`power`/`toughness` are **single-valued per card** (a card has exactly one cmc) — this corpus has
only 17/19/21 distinct integer values for the three fields respectively. That means partitioning cards by
their exact arith value is a genuine, exhaustive, non-overlapping partition of the card space: summing a
PER-VALUE exact joint count (arith value `v` -> count of cards with that `v` AND satisfying the existential
value) over every `v` in `[lo, hi]` reproduces the TRUE joint intersection exactly — no independence
assumption, no anti-correlation risk, because it's a sum over disjoint cells, not a product or a `min`.
This does NOT generalize to multi-valued fields (card types, formats/legality — a card can have several),
only to the single-valued arith-tuple family, exactly as scoped.

**Validated directly against the real corpus**, not just argued: built the (arith value -> existential
value -> distinct card count) table from `benchmarks/bitplanes/corpus.jsonl` (grouping printings by
`oracle_id`, one row per card) for all three arith fields against border/rarity/`f:oldschool`, then checked
`sum(table[field][v] for v in [lo,hi])` against every one of the 429 sweep rows' real `true_intersection`:

```
checked=429  exact_matches=429  (100.0%)
```

Every single row, exact, including the degenerate `border:silver` (0) and `r=special` rows. Table size:
138 (cmc) + 145 (power) + 152 (toughness) non-zero (value, existential-value) cells = 435 cells total,
each a 3-space count — a few KB, not a new large structure.

**An existing, already-shipped precedent for exactly this pattern was found**: `indexes.pair_totals` /
`pair_leaf_id` / `pair_bounded_min` (`card_engine/src/lib.rs`, ~2705-2900 and 8530-8574) is the SAME
disjoint-pair-sum idea, already built and already wired into the `And` arm — `pair_bounded_min(v, indexes,
folded.result.printing)` runs unconditionally at line 7736, *before* any of Round 22's existential-arith
machinery even starts. It currently recognizes exactly four dense, low-cardinality dimensions — `border`
(`TextExact` `Eq`), `frame_data` (`CollectionCmp` `Ge`), `legality` (per format/status), and `rarity`
(`NumericCmp{RarityInt}`, `Eq` only) — and **nothing for `cmc`/`power`/`toughness`**: `pair_leaf_id`'s
match falls through to `_ => None` for every `NumericCmp` on those three fields, at any operator.

This is the SAME root-cause shape Round 21 found for `value_totals.border` — an existing exact answer,
unreached for this specific AST shape. Confirmed directly: `parse_scryfall_query('cmc=1 border:white')`
produces a genuine single `=` binary-operator node for `cmc`, not an implicit two-leaf range — exactly the
shape `pair_leaf_id` already handles for rarity today. **Extending `pair_leaf_id`'s match (3 new arms:
`Cmc`/`Power`/`Toughness` `NumericCmp` `Eq`) plus `build_pair_totals`'s per-printing id-collection pass (3
more lookups per printing, reusing the same `PAIR_MIN_PRINTINGS`-gated selectivity floor already there)
would make the WIDTH-1 slice of this population (`cmc=1 border:white`-shaped, 33/429 = 7.7% of the sweep,
the cheapest-to-tax slice at +10,250ns median but still real) exact via the EXISTING `pair_bounded_min`
call site, with zero new call sites and no new top-level struct** — the smallest possible next step here.

**This does not cover the ranged case.** `pair_leaf_id` is deliberately `Eq`-only (mirroring rarity's own
restriction — "any other op is a range over several values, which no per-value entry answers"), and the
`And` arm passes `pair_bounded_min` the ORIGINAL, unfused children (`v`), not `fuse_and_range_children`'s
output — so `cmc>=1 cmc<=5` (two `Ge`/`Le` leaves) would still not match, even with the extension above.
The ranged case is the bulk of both the row count (12/13 widths) and essentially all of the absolute
acquire-time tax (width-1 rows average +10,250ns vs the +19,000-23,000ns width 4-12 average). Closing it
needs a range-capable mechanism, and there are two honest ways to build one:

- **(a) Extend `pair_totals` itself**, adding cmc/power/toughness as 3 more dense dimensions to the
  existing `O(n²)` id x id co-occurrence matrix `build_pair_totals` already builds, plus a new helper that
  sums `pt.get(x, y)` over however many arith values in `[lo, hi]` clear the pruning floor (bounded, ≤21
  lookups). Reuses the existing struct/build pass/selectivity floor entirely, but grows the co-occurrence
  matrix by ~45 more candidate ids (17+19+21, before pruning) against today's ~15-30 — a real, if
  transient (index-build-time only) memory cost, and it stores a lot of PAIR cells (cmc×power, cmc×frame,
  ...) the router never actually queries for this shape, since only arith×existential pairs matter here.
- **(b) A new, purpose-built cumulative table**, attached wherever the arith index's own per-value data
  lives, mirroring `RangeCardCounts`'s existing `below`/`at_or_above`/`at` prefix-sum design (already
  shipped, for a different set of range dimensions — price/collector-number/release-date) but keyed
  additionally by existential value. Sized like the validation table above (~57 arith values x ~11
  existential values, well under 20KB), answers a RANGE in true `O(1)` (two `partition_point` calls plus a
  prefix difference) rather than `O(bucket count)`, and doesn't touch the unrelated existing `pair_totals`
  matrix at all — strictly better cost shape, at the price of being new code (new struct, new build pass,
  new query-time helper) rather than an extension of something that already ships.

Both (a) and (b) are **index-build-time work, not free** — this is squarely the "pre-computation over
hot-path computation" tradeoff the round's brief named, just located one level earlier (build time) than
either Round 22's fix or the `min()` idea (both pure query-time mechanisms). Neither was implemented this
round; both are validated-by-construction (the 429/429 result above already IS what (b) would compute) and
ready for a dedicated follow-up round to actually wire in.

### 4. Does the cheap estimate change routing? Yes, often, and mostly for the worse

Restricted the comparison to the plans `explain()` itself reports as real candidates for this acquire
branch — confirmed directly that `StreamedSelect` is NEVER offered here (396/429 rows offer exactly
`{GatheredScan, PrintingCompose}`, 33/429 offer `{GatheredScan}` alone; scoring a plan the router would
never actually consider would be a fabricated comparison). For each row, recomputed both plans' predicted
cost under the exact `eval_domain`/`scan_units` (matches `explain()`'s own numbers, verified to 0.0000
relative error) and under the cheap `eval_domain` with `scan_units` recomputed via the SAME `scan_all`
density fallback the pre-Round-22 code path took (this shape's `composed_card_invariant` is always false,
so the `card_invariant_domain_exact`/`exact_domain_won` fast paths are unavailable either way, before or
after — confirmed from the code, not assumed):

```
argmin flips: 70/429 (16.3%)
69/70 flip GatheredScan (exact) -> PrintingCompose (cheap); 1/70 the reverse
```

Flips concentrate ENTIRELY on the three leaves with the largest absolute bare counts, not the leaves with
the worst ratios: `r=rare`/`r=common`/`r=uncommon` account for 69 of the 70 flips (23/39 each), plus one
`border:black` row. `r=mythic` (comparable ratio, ~6x smaller absolute count) flips zero times.

**Real measured regret**, not just a predicted-cost artifact: ran `explain_analyze` (15 warmups, 60
trials) on the actual post-Round-22 wheel for all 70 flipped queries, reading REAL per-plan trial medians
for both `GatheredScan` and `PrintingCompose` (both genuinely execute — this is not a simulation):

```
n=70
55/70 (79%): cheap's pick really is slower — median regret when worse: +51,625ns, summed: +3,318,023ns
15/70 (21%): cheap's pick happens to be faster — median improvement: -25,708ns, summed: -404,456ns
net summed regret over just these 70 rows: +2,913,566ns (+2.91ms)
```

The 15/70 "improvements" are not the cheap estimate doing something right — they're the EXACT model's own
`PrintingCompose` cost formula being mis-calibrated for unrelated reasons on wider `r=rare` ranges (a
pre-existing cost-model imprecision this round didn't introduce and isn't trying to fix), which the cheap
estimate's overestimate happens to route around by accident. That is not a case for shipping it: a
routing choice that's "right for the wrong reason" 21% of the time against "measurably wrong" 79% of the
time, concentrated on exactly the rarity values (common/uncommon/rare) most likely in real traffic — not
the corpus's rare border/legality corners — is a net loser.

### 5. Recommendation

**Do not ship the `min()` cheap estimate.** It disagrees with the real candidate set's argmin on 16.3% of
this sweep, the disagreement is real (not a modeling artifact — measured directly via `explain_analyze` on
both real candidate plans), 79% of those disagreements cost real wall time (median +51.6us, net +2.9ms
over just 70 rows), and the disagreements concentrate on the leaf values (common/uncommon/rare) most
plausible in real traffic, not corpus-specific corners. Round 22's exact fix should stay unconditional.

**But the right next step is not "accept the tax" either — it's the disjoint-bucket-sum family (§3),
which this round found to be EXACT (validated 429/429) at a cost this round did not fully characterize at
query time but which is bounded by construction** (≤21 lookups for the general form, `O(1)` for the
`RangeCardCounts`-style cumulative form) **and is strictly better than `min()` everywhere it applies**: no
anti-correlation risk, no accuracy/speed trade to make. Two concrete next steps, in order of size:

1. **Smallest, immediate**: extend `pair_leaf_id`/`build_pair_totals` to recognize `cmc`/`power`/
   `toughness` `Eq` values (3 new match arms, reusing 100% of existing machinery) — closes the width-1
   slice (`cmc=1 X`-shaped, 7.7% of this sweep) via the ALREADY-WIRED `pair_bounded_min` call site, for
   free at query time. A good, low-risk follow-up on its own.
2. **Complete, bigger**: build the `RangeCardCounts`-style cumulative per-(arith field, existential value)
   table sketched in §3(b) — closes the ranged case too (the bulk of both the row count and the acquire-
   time tax), at the cost of new index-build-time code, not a query-time trick. This is where a dedicated
   follow-up round should aim; this round's validation (429/429 exact against real data) is that round's
   head start, not something it needs to re-derive.

### Artifacts

Exploratory scripts (scratchpad only, not committed): `sweep_r23.py` (429-row sweep, both wheels),
`analyze_r23.py` (acquire-delta/cheap-vs-exact/routing-impact analysis, includes the verified `cost.rs`
Python port), `measure_flip_regret.py` (real per-plan trial measurement for the 70 flipped rows),
`validate_bucket_sum.py` (disjoint-bucket-sum validation against real corpus data). No engine-code edits
were made or reverted this round — every number above came from the two wheels as they already exist
(`cc17e031`, `188a0ee4`) plus new Python analysis, so `git diff --stat costcell/trunk` for this round shows
only this doc.



Round 21's sketch (below) turned into a fix. `compose_printing_estimate`'s `And` arm (`card_engine/src/lib.rs`,
~line 7845): the `else if !card_invariant.is_empty()` guard on `best_other`'s existential-leaf loop is now a
plain `else` — a lone existential leaf (no card-invariant partner at all) runs the SAME
`popcount_with_bits(Some(e))` loop a card-invariant-paired existential leaf already used, unmodified.
`popcount_with_bits` already handled an empty `card_invariant` vec correctly (it was never incapable of
answering this shape, just never *called* for it) — see Round 21's own diagnosis above, confirmed unchanged
on `costcell/trunk`@`cc17e031` before this round touched anything.

### Ingredients 2 and 3 from Round 21's sketch: investigated, not shipped

**Ingredient 2** (prefer a precomputed exact count over a fresh `eval_planes`+`popcount` for the scalar):
does not cleanly apply to the population this bug actually affects. The BITS from `popcount_with_bits` are
still needed regardless, for the arith-ID-probe merge a few lines below `best_other` — and for the exact
flagship shape (an arith-tuple leaf ANDed with the existential leaf), that merge is what produces the TRUE
joint intersection; the scalar from `popcount_with_bits` alone (before the merge) is only the existential
leaf's own bare count, not yet the answer this round needs. So the eval_planes call itself cannot be
skipped for the population that matters, and substituting the scalar alone (leaving bits untouched) would
only help a narrower, already-rare shape (zero arith children, `card_invariant` empty, one existential
leaf) that ingredient 3 would have covered for free anyway. Not shipped.

**Ingredient 3** (fold `folded.result.card` in as a `.min()` floor at the final struct construction):
investigated and explicitly NOT shipped, because the SAME idea was already tried once before, for the
retired `domain_hint` field, and reverted — see the existing code comment directly above the final struct
construction (`card_engine/src/lib.rs`, ~line 7941 as of this round): *"a single BROAD leaf's own card
count... is not a safe upper bound on the whole `And` unless `narrow_rec` would actually USE that leaf to
narrow."* The concern is not mathematical (a single leaf's own count IS always ≥ the true joint) — it is
that `.card`/`domain_cards` is consumed downstream as an estimate of what the real EXECUTION PLAN will
visit, and `narrow_rec` may decline a broad leaf entirely (the documented `broad_ok: false` precedent,
`border:black` at 87%). Folding in a broad leaf's own count without the breadth guard the retired
`domain_hint` used to carry risks resurrecting that exact, already-fixed bug. Given the primary fix
(the gate) is independently sufficient and validated (below), this round left ingredient 3 out rather than
risk it — a future round could revisit it WITH an equivalent breadth guard, but that is new scope, not a
"free" addition.

**Shipped, with the guard, in Round 41** (see
[local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)'s
own "Round 41" section) — reusing `range_too_broad_to_narrow` as-is for the guard this note asked for,
and additionally scoping the new floor to `result_space` only, never `exact_domain` (the field
`scan_units` actually reads), so the `domain_hint`-conflation risk this note describes is sidestepped
by construction rather than by the guard alone.

### Reproducers: before / after (real corpus, isolated release wheels, `costcell/trunk`@`cc17e031` vs this round)

`unique=card`, `orderby=rarity`, `direction=desc`, `limit=175`, `offset=0`, `prefer=default`:

| query | before `eval_domain` | before `cards_visited`/`eval_domain` | after `eval_domain` | after ratio |
|---|--:|--:|--:|--:|
| `cmc=1 border:black` | 4,893 | 0.624 | 3,052 | **1.000** |
| `cmc=1 border:white` | 2,756 | 0.113 | 311 | **1.000** |
| `cmc=1 r=mythic` | 4,710 | 0.021 | 101 | **1.000** |
| `cmc>=1 cmc<=5 border:white` | 2,756 | 0.641 | 1,766 | **1.000** |
| `cmc>=1 cmc<=5 r=mythic` | 4,710 | 0.391 | 1,842 | **1.000** |
| `cmc>=1 cmc<=5 border:black` | 24,734 | 1.088 | 24,734 | 1.088 (unchanged — already tightened via `arith_tuple_count`, a separate mechanism; the gate fix additionally now runs the arith-ID-probe merge here too, but border:black's near-100% selectivity means it doesn't move the number) |

Every reproducer Round 21 named — `cmc=1 border:black`, `cmc=1 border:white`, and a rarity case
(`cmc=1 r=mythic` / `cmc>=1 cmc<=5 r=mythic`) — moves to an EXACT `eval_domain` (ratio 1.000). The
two-sided flagship shape (`cmc>=1 cmc<=5 border:black`) was already in a defensible band via a different,
unaffected mechanism and stays there.

### Broader sweep: is the confound Round 20 hit now cleared, broadly?

Round 20's own blocker was precise: of 223 systematically-varied population-A rows (3 arith fields × 7
widths × 11 existential leaf values), only 13 (5.8%) landed within 15% of real `cards_visited` —
`border:black` was "the only leaf value tested where `eval_domain` is trustworthy." Re-ran the identical
sweep (3 fields × 13 widths × 11 leaf values, 393 successful rows) against this round's fixed wheel:

```
total rows: 393, within 15% of cards_visited: 351 (89.3%)   -- was 13/223 (5.8%) before this round

border:black        n=39  median 1.000  100% within 15% except width-13 outlier (82.1% — border:black's
                                                                                own near-universal count)
border:white/borderless/gold, r=common/uncommon/rare/mythic, f:oldschool:
                     n=39 each, median 1.000, 100% within 15%  -- every one of these was badly wrong
                                                                   (0.02-0.85) before this round
r=special            n=39  median 0.699  10.3% within 15%  -- STILL broken, but a DIFFERENT, already-known
                                                               bug (see below), not this round's gate
```

The gate fix clears the confound for every leaf value tested except `r=special` (0.012 bare selectivity,
325 of 31,724 cards). Traced directly: `r=special`'s `eval_domain` reads a FLAT 219 across `cmc<=1` through
`cmc<=9` (only changing once the range widens enough to include ALL 325 matching cards) — this is the
SAME, separately-documented gap Round 17/19/20 already flagged in passing ("`eval_domain` reads identical
across every range width for several minority existential leaf values... a gap in the
`card_invariant_domain_exact`/estimated-domain fallback"), not a symptom of the `best_other` gate this
round fixed. Out of this round's blast radius (a different mechanism, `acquire_plan_features`'s domain
fallback, not `compose_printing_estimate`'s `And` arm) — flagged here as still open, not chased.

### Pre-computation check: acquire-time cost, measured directly

The gate fix makes the SAME already-existing `popcount_with_bits`/arith-ID-probe-merge machinery run for
MORE query shapes than before (previously gated off whenever `card_invariant` was empty) — this is real,
measurable added cost for the newly-covered population specifically, not a new mechanism. Measured
directly (20 warmups, 200 trials, isolated release wheels, `explain_analyze`'s own `acquire_ns`):

| query | before (median) | after (median) | delta |
|---|--:|--:|--:|
| `cmc=1 border:black` | 709 ns | 42,083 ns | +41,374 ns |
| `cmc=1 border:white` | 709 ns | 6,125 ns | +5,416 ns |
| `cmc=1 r=mythic` | 1,000 ns | 6,583 ns | +5,583 ns |
| `cmc>=1 cmc<=5 border:white` | 4,833 ns | 26,500 ns | +21,667 ns |
| `cmc>=1 cmc<=5 r=mythic` | 5,125 ns | 28,166 ns | +23,041 ns |
| `cmc>=1 cmc<=5 border:black` | 4,959 ns | 92,791 ns | +87,832 ns |

This is NOT from `popcount_with_bits`'s `eval_planes` call itself (a fixed ~496-word bitmap AND, the same
small cost the mechanism has always paid when it ran) — traced to the arith-ID-probe merge a few lines
below `best_other`, now reached for this population for the first time. That merge's cost scales with the
arith-tuple leaf's own selectivity (`bare_numeric_field_ids`/`arith_tuple_ids` materializes one `Vec<u32>`
of matching card ids, then filters/sums over it) — `cmc>=1 cmc<=5` (77% of all cards) costs far more than
`cmc=1` alone (10% of all cards), matching the table above. This merge is NOT optional: it is what produces
the TRUE joint intersection (e.g. `cmc=1 ∩ border:white` = 311 cards, not border:white's own 2,059-card
bare count) — without it, the fix would only partially tighten `eval_domain`, not close it to exact.

Confirmed the added cost is SCOPED to the newly-covered population, not a general regression: four
unrelated queries that already had a card-invariant partner (so `best_other` already fired before this
round) show no measurable acquire-time change: `c:w cmc<=3` 667ns→666ns, `f:modern c:u` 7,250ns→6,833ns,
`t:elf` 500ns→542ns, `devotion:w c:u usd>5` 7,500ns→7,833ns (15 warmups/100 trials each; all within noise).

This is a real, bounded-but-non-trivial trade for a narrow, previously-mis-costed population — accepted
because (a) it reuses existing, already-designed-for-this-purpose machinery rather than adding anything
new, (b) it is invisible in whole-corpus aggregates (below), and (c) the population it fixes was previously
driving `eval_domain` off by up to 47x (`cmc=1 r=mythic`, 0.021 ratio), which is the more consequential
error for routing.

### Correctness gate

`cargo test --manifest-path card_engine/Cargo.toml --release`: **173/173 passed** (172 pre-existing + 1 new
regression test, `compose_and_arm_tightens_lone_existential_leaf_with_no_card_invariant_partner` in
`tests.rs`, reusing the existing `cmc_border_existential_fixture_store` fixture — asserts the AND's exact
card intersection (`Some(2)`) and printing span (`3`) directly via `compose_printing_estimate`, for the
specific "arith range AND one existential leaf, no other card-invariant leaf" shape. Confirmed the test
actually catches the regression by temporarily reverting the gate: fails with `left: None, right: Some(2)`
against the old `!card_invariant.is_empty()` guard, as expected). Every existing assertion unchanged.
`cargo test --manifest-path card_engine/Cargo.toml` (debug, with debug-assert tripwires): 173/173 passed.
`cargo clippy --manifest-path card_engine/Cargo.toml --all-targets -- -D warnings`: clean.

### Broader regression check (mandatory — this touches the same `And` arm Rounds 1-9 validated for a
### different population)

`bench_cost_model_agreement.py --seconds 300 --seed 0`, full table, baseline vs fix:

```
overall: 11/17 cells inside [0.8, 1.25]  ->  12/17 cells inside [0.8, 1.25]   (improved, not regressed)
GatheredScan/card:     0.84 (26% within 25%)  ->  0.86 (27% within 25%)       (moved toward 1.0)
GatheredScan/printing_compose: 1.18 (24%)     ->  1.22 (24%)                  (flat, within noise)
```

No cell flips from PASS to FAIL. One cell (`PrintingCompose`/`plane`) moves from 17% to 30% within-25%,
both still below the 80% pass bar — not a regression, a small improvement. Total sampled queries in the
same 300s window: 101,108 → 97,253 (−3.8%), consistent with the measured acquire-time cost above diluted
across the WHOLE uniform sample (most queries in the sample never touch this population at all).

`bench_pairwise_ordering.py --seconds 300`, `GatheredScan` vs `PrintingCompose`/`StreamedSelect`, both
modes, baseline vs fix:

```
realistic:  GatheredScan vs PrintingCompose      88% ordered right both, regret 11.90µs->11.89µs (flat),
                                                  gap meas/pred 0.91 -> 1.01  (moved to near-exact)
            GatheredScan vs StreamedSelect        97% both, 0.86µs->0.87µs (flat), 1.04->1.04 (unchanged)
uniform:    GatheredScan vs PrintingCompose        88% both, 7.27µs->7.03µs (flat), 1.03->1.06 (flat)
            GatheredScan vs StreamedSelect        95% both, 1.88µs->1.73µs (flat), 1.07->1.07 (unchanged)
```

`bench_regret_matrix.py --seconds 120 --mode realistic --seed 0`: baseline total regret 38.8ms over 50,549
queries (mean 0.77µs) -> fix 42.9ms over 52,944 queries (mean 0.81µs), +5.2% mean, comparable to prior
rounds' own sample-to-sample noise band (Round 15/16 reported similar ±0.5-4% swings from re-sampling
alone). One new single-query outlier (max regret 1,927.6µs in a `StreamedSelect -> PrintingCompose`
misroute category that existed in baseline too, just with a smaller max there, 95.3µs) — likely a
different rare query landed in the differently-sized random walk (same seed, but per-query timing
differences shift how many queries QuerySampler draws in the same wall-clock budget); no NEW misroute
category appeared, and the "picked -> best" breakdown's set of categories is identical before/after.

`bench_query_latency_ab.py --sample 400 --mode realistic --seed 7`, baseline vs fix, plus a same-build
canary at the same seed (sequential runs, not literally interleaved sub-second — see caveat below):

```
canary (base run 1 vs base run 2):  B - A = +0.4µs  95% CI [+0.2, +0.5]
baseline vs fix:                    B - A = +1.0µs  95% CI [+0.6, +1.7]
```

The fix's interval does not fully overlap the canary's, but the two are close (0.6µs apart) at a sample
size the script's own docs flag as noisy for cross-process comparisons at these defaults (n=400). Given
the affected population's rarity (Round 21: ~0.85-1.23% of `Mode::Card` queries by a rough regex proxy,
an over-count relative to the exact AST shape), a small, real, borderline-detectable aggregate effect this
size is consistent with the acquire-time cost measured directly above, not a red flag on its own.

### Step 5 (joint rate refit): blocker demonstrably cleared, refit itself deferred

Round 20's own recommended next step was explicit: fix `eval_domain` first, then the `GATHER_CARD_PASS_NS`/
`GATHER_RESIDUAL_FLOOR_NS`/leaf-count-rate joint refit becomes testable for real. The broad sweep above
confirms the blocker IS cleared for essentially the whole population Round 20 swept (89.3% of rows within
15% of `cards_visited`, up from 5.8%) — a real, load-bearing precondition for that future refit, not a
minor caveat.

The refit itself was NOT attempted in this round. Building it properly means reintroducing Round 19/20's
`count_plane_leaves`/`plane_extra_eval_leaves` plumbing as a clearly-separate addition, a standalone fit
script mirroring `fit_round20.py`'s design, and the SAME three-population (compound/bare/residual)
held-out validation discipline Round 20 used — each of those was itself a full round's worth of work in
Rounds 19/20, and both of THOSE rounds' negative results (the additive term overshoots the flagship
reproducer even with a correct mechanism, Round 19; the floor's original calibration already unevenly
absorbs part of the compound-leaf effect, Round 20) were about the RATE FIT itself, not about `eval_domain`
— clearing the `eval_domain` blocker does not by itself imply the rate refit will now succeed. Rebuilding
that machinery and re-running the fit deserves its own dedicated round rather than being compressed into
this one's remaining scope. Per this round's own brief: "if the correctness fix (1-4) works but the refit
(5) doesn't [get attempted], ship the correctness fix alone" — done, with the next round now unblocked to
attempt the refit directly against trustworthy `eval_domain` data.

### Commit

One commit on `costcell/22-existential-and-fix` (gate fix + regression test + fixture addition; no
`cost.rs` changes, since step 5 was not attempted). `git diff --stat costcell/trunk`: `card_engine/src/lib.rs`,
`card_engine/src/tests.rs`, this doc.



Round 20 of the `GatheredScan`-compound-plane effort
([done/local-engine-gathered-scan-undercosted-arith-existential-and.md](done/local-engine-gathered-scan-undercosted-arith-existential-and.md))
found that three rounds of rate-fitting against `cmc>=1 cmc<=5 border:black`-shaped queries all failed
for the same reason: `eval_domain`/`domain_cards` (the acquire-time card-domain estimate every
`GatheredScan`/`StreamedSelect` per-candidate term multiplies) is itself wrong by up to 14x for this
population, and every rate fit was measured against a corrupted ground truth. This doc is that round's
named next step — fix the domain estimate first — and finds the exact code responsible, not just its
symptom. It is the third appearance of the same general problem: combining two range/existential leaves
into one accurate card-domain count was also the blocker in
[#852](00852-engine-compose-acquire-p3-p4-ranking.md)'s item 1, and an independence-product family of
fixes for a related (but distinct) shape was proven a dead end in
[local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md)'s
Round 2.

No fix ships in this doc. Everything below was verified against the real corpus (`benchmarks/bitplanes/corpus.jsonl`)
via `engine.explain()`/`explain_analyze()` on an isolated release wheel built from `costcell/trunk`@`bb5798b2`
(Round 20's own commit), plus temporary `eprintln!` instrumentation in `compose_printing_estimate`/
`acquire_plan_features` (reverted before this commit — `git diff --stat costcell/trunk` shows only this file).

## Question 1: what type of queries are affected

**The population is precisely: `Mode::Card`, `PrintingCompose` acquire, an `And` whose children are some
mix of arith-tuple leaves (`cmc`/`power`/`toughness`) and exactly one printing-varying existential leaf
(`border`/`rarity`/a divergent-format `legality`), with no OTHER card-invariant leaf (color, non-divergent
legality, devotion, ...) present.** Every dimension below was swept directly.

### Fields and range width

All three arith-tuple fields (`cmc`, `power`, `toughness`) show the identical pattern — this is not
`cmc`-specific. Width matters, but not in the way "a single value is exact" would suggest: it interacts
with which side of the `And` a plain per-child `min` picks (see Question 2), not with narrowness alone.
Swept widths 1 (`cmc=V`), 3, 5, and the full interior range (13) against 11 existential leaf values; every
width shows the same qualitative failure for every leaf except `border:black`.

### Existential leaf families and values — the "near-universal" hypothesis, tested directly

Bare-leaf selectivity in this corpus (`Mode::Card`, fraction of `n_cards=31,724`):

| leaf | frac |
|---|--:|
| `border:black` | 0.989 |
| `border:borderless` | 0.110 |
| `border:white` | 0.065 |
| `border:gold` | 0.017 |
| `border:silver` | 0.000 |
| `r=rare` | 0.349 |
| `r=common` | 0.337 |
| `r=uncommon` | 0.324 |
| `r=mythic` | 0.083 |
| `r=special` | 0.012 |
| `f:oldschool` (the corpus's one divergent format) | 0.030 |

No rarity value is near-universal (max 35%), and the corpus's only *divergent* legality format
(`oldschool`, the only format where "existential" even applies — a non-divergent format like `modern` or
`commander` is card-invariant and never reaches this code path at all) is itself a minority value (3%).
**Border is the only family in this corpus with a near-universal value at all**, so the brief's "find a
near-universal value in a different family" cannot be answered with a second clean field+value pair from
this corpus's own data — but the mechanism itself (Question 2) was confirmed directly by construction
instead: adding a near-vacuous 99.8%-selective leaf from a *different* family (`f:commander`, non-divergent
legality) to a broken query flips the same code path on and fixes it, which is a stronger and more direct
test than a second natural near-universal value would have been. Selectivity of the *discarded* side, not
field identity, is confirmed to be the true driver — see Question 2.

Ratio (`GatheredScan.cards_visited / eval_domain`) across all 11 leaf values x 4 widths x 3 fields (33
rows, `cmc>=1 cmc<=5`-shaped and narrower/wider):

- `border:black`: 0.624 (width 1) to 1.268 (full range) — the only leaf that stays in a defensible band.
- Every other value (`border:white/borderless/gold`, `r=common/uncommon/rare/mythic/special`,
  `f:oldschool`): 0.02 to 0.95, monotonically worse (lower) as the leaf's own selectivity drops, and
  **always an over-estimate** (`eval_domain` too big) except for the degenerate `border:silver` case
  (0 corpus matches at all — `eval_domain=0` while `cards_visited>0`, a separate, minor edge case in the
  `range_too_broad_to_narrow`/zero-match interaction, not chased further here).

### Mode

Identical failure in `Mode::Printing` and `Mode::Artwork` — `eval_domain` for `cmc>=1 cmc<=5 border:white`
reads 2,756 in all three modes (`unique=card/printing/artwork`), and `Mode::Printing`/`Mode::Artwork` share
the exact same `domain_cards` computation `Mode::Card` reads (`acquire_plan_features`'s shared
`(eval_domain, scan_units)` tuple, keyed off `domain_cards` regardless of `mode`). Not card-mode-specific.

### Acquire branch

Confirmed acquire-branch-specific, matching Round 20's finding exactly: the same filter under
`orderby=name` (`Prep::Candidates`/`count_source=plane`, not `PrintingCompose`) reads `eval_domain` exactly
equal to `cards_visited` for all three test leaves (black/white/mythic), ratio 1.0 every time. The bug is
entirely a `compose_printing_estimate`/`acquire_plan_features`'s `PrintingCompose`-branch phenomenon, and
does not touch the `plane`/`Prep::Candidates` acquire's own (correct) domain computation.

### Real-traffic representation

A regex-based proxy over `QuerySampler` (40,000 draws each, `uniform`/`realistic`) matching queries that
mention both an existential-family leaf and an arith-tuple comparison anywhere in the text: 1.7-2.4% of all
sampled queries, ~0.85-1.23% in `Mode::Card` specifically. This over-counts the actual bug population,
because (per Question 2) adding *any other* card-invariant leaf — color, a non-divergent format, devotion —
cures it; a realistic query combining `f:modern c:w cmc<=3 border:black`-style leaves would not hit this
bug at all. Round 20's own, more rigorous measurement of the closely-related `plane_extra_eval_leaves`
population (60,000 combined draws, both modes) found **zero** naturally-sampled rows — this doc's
population is a superset of that one (it doesn't require the leaf-count feature, just the domain
corruption), but is still a narrow, hand-constructible shape rather than one `QuerySampler` reliably hits.
Confirms and refines Round 20's finding rather than contradicting it: real but rare, and rare specifically
because most real queries carry an incidental card-invariant leaf that happens to fix the bug as a side
effect, not because the AST shape itself is exotic.

## Question 2: the mechanism — confirmed root cause, not just a symptom

### The bug, precisely

`compose_printing_estimate`'s `And` arm (`card_engine/src/lib.rs:7680`) computes an exact joint card
count only through `best_other` (line 7840):

```rust
let mut best_other: Option<(usize, Vec<u64>)> = None;
if existential.is_empty() {
    if card_invariant.len() >= 2 { best_other = Some(popcount_with_bits(None)); }
} else if !card_invariant.is_empty() {
    for e in &existential {
        let candidate = popcount_with_bits(Some(e));
        if best_other.as_ref().is_none_or(|(c, _)| candidate.0 < *c) { best_other = Some(candidate); }
    }
}
```

`card_invariant`/`existential` are populated at line 7801 by filtering OUT every arith-tuple-eligible
child (`cmc`/`power`/`toughness` are excluded from both) and partitioning the rest by
`plane_expr_is_existential`. **For the flagship reproducer's own minimal shape — one or more arith leaves
plus exactly one existential leaf and nothing else — `card_invariant` is empty and `existential` has one
element, so *neither* branch of the `if`/`else if` fires: the `else if !card_invariant.is_empty()` guard
requires a card-invariant partner that a lone existential leaf does not need.** `best_other` stays `None`
for the rest of the function, so `exact_domain_cards` (and everything downstream: `est.result.card`,
`domain_cards`'s `is_and` tightening, `card_invariant_domain_exact`) never gets an exact answer — even
though `popcount_with_bits(Some(e))` (line 7818) works fine with an empty `card_invariant` vec; it is
never *called* for this shape, not incapable of answering it.

Confirmed directly with a temporary `eprintln!` at line 7852 (right after the `if`/`else if` block,
reverted before commit): for every one of `cmc=1 border:white`, `cmc=1 border:white f:commander`,
`cmc=1 border:black`, `cmc>=1 cmc<=5 border:black`, `cmc>=1 cmc<=5 border:white`, `cmc>=1 cmc<=5 r=mythic`
— `card_invariant.len()==0`, `existential.len()==1`, `best_other.is_some()==false`, in every case with no
OTHER card-invariant leaf in the query.

### Falsifiable test, run directly: does adding a card-invariant partner fix it?

Yes, cleanly, and the fix does not need the partner to be selective — a near-vacuous one works just as
well as a real one, which is exactly what the "gating bug, not an accuracy bug" diagnosis predicts:

| query | `card_invariant.len()` | `best_other` | `eval_domain` | `cards_visited` | ratio |
|---|--:|:--:|--:|--:|--:|
| `cmc=1 border:white` | 0 | false | 2,756 | 311 | 0.113 |
| `cmc=1 border:white f:commander` (99.8% selective, near-vacuous) | 1 | true | 309 | 311 | **1.006** |
| `cmc=1 border:white f:modern` (70.8% selective, a real constraint) | 1 | true | 127 | 127 | **1.000** |
| `cmc=1 border:black` | 0 | false | 4,893 | 3,052 | 0.624 |
| `cmc=1 border:black f:commander` | 1 | true | 3,044 | 3,068 | **1.008** |

The last row is the sharper point: even `border:black` — the leaf every prior round called "clean" — is
*not* actually well-estimated at width 1 (ratio 0.624) once you isolate the shape from the wider-range
case. It only reads "clean" for the `cmc>=1 cmc<=5`-shaped flagship reproducer specifically, and that
cleanliness comes from a second, *unrelated* coincidence (below), not from `best_other` firing — `best_other`
is confirmed `false` there too. Once ANY card-invariant partner is present, `best_other` fires and the
estimate becomes essentially exact for every leaf tested, `black` included.

### Why `border:black` looks clean anyway, for the specific `cmc>=1 cmc<=5` shape

`acquire_plan_features`'s `domain_cards_before_card` (line 12147) does not even read the `best_other`
path's output when it fires — it reads `est.candidate.printing`/`est.result.printing` instead:

```rust
let domain_cards_before_card = if est.candidate.printing == est.result.printing {
    est_cards
} else {
    calibrated_balls_into_bins(est.candidate.printing, n_cards as usize)
};
```

For a 2-sided range (`cmc>=1 cmc<=5`, two arith children), the *printing-space* value `result` gets
tightened by a **separate**, already-working mechanism (`arith_tuple_count`, an exact `#743` index scan
over 2+ arith children — unaffected by the `best_other` bug, since it never touches `card_invariant`/
`existential` at all) — but `candidate` never receives that tightening (`candidate` is deliberately the
untightened per-child `min`, "what narrow_rec actually leaves the alternatives to walk" per the function's
own doc). Confirmed via `eprintln!`: `cmc>=1 cmc<=5 border:black` has `est.candidate.printing=85,411` vs
`est.result.printing=83,894` (NOT equal), so `domain_cards_before_card` takes the `calibrated_balls_into_bins`
branch on the **untightened** 85,411, not the tightened 83,894. That untightened number is itself just
`min(cmc>=1's own printing count, cmc<=5's own printing count, border:black's own printing count)` — and
it happens to land close to the truth here purely because **whichever side the plain per-child `min`
discards is, for `border:black` specifically, close to 100% selective, so discarding it costs almost
nothing.** For every other leaf tested, the discarded side is a real minority constraint, and discarding
it is exactly the over-estimate measured in Question 1. This is the same "selectivity of the discarded
side" mechanism as the `best_other` gate, arrived at through a completely different code path — two
independent coincidences, not one robust mechanism, which is why `border:black` alone (width 1, no second
arith child) is *not* clean (ratio 0.624 above) even though the wider-range reproducer is.

### A third, free, already-computed ingredient the fold already carries and discards

Checked (via a second `eprintln!`, also reverted) whether the per-child fold that builds `folded` (line
7714, `children_estimates.iter().fold(...)`, `SpaceEstimate::min` at line 7417) already carries a
useful `.card` value before `best_other` ever runs. It does, for **border** specifically: `border`'s own
leaf arm in `compose_printing_estimate` calls `exact_result_total(filter, indexes, Mode::Card)` (which
hits `vt.border`, a precomputed exact 3-space per-value table — O(1)/O(log n), no bitmap) and returns it
via `ComposeEstimate::leaf_spaces`, so `folded.result.card` already holds `min` of every child's own exact
card count wherever one exists. Confirmed directly: `cmc>=1 cmc<=5 border:white` → `folded.result.card =
Some(2,059)`, exactly `border:white`'s own bare card-match count. **But this value is thrown away
regardless of whether `best_other` fires** — the final struct literal at line 7948,
`ComposeEstimate { result: result_space, exact_domain, ..folded }`, always sets `.card` from
`result_space` (built from `exact_domain_cards`, `best_other`'s output, `None` here), never falls back to
`folded.result.card` when that's `None`. This is free (no new probe, already computed today) and would be
a strict tightening (an individual child's own exact count is always ≥ the true joint intersection, so
`.min()`-ing it in can only help) — but it caps out at "the tightest single child's own marginal count,"
not the true joint intersection, so it is a partial complement to fixing `best_other`, not a substitute
for it. **Rarity does not currently have this same free ingredient**: its own leaf arm
(`compose_printing_estimate`, `NumericCmp{RarityInt}`) deliberately uses `ComposeEstimate::leaf` (not
`leaf_spaces`), leaving `.card`/`.artwork` at `None` — its own comment cites a documented, pre-existing
bug in `RangeCardCounts::distinct_cards` for **broad** comparisons (`r<=mythic` read 31,722 against a true
31,724). Whether that bug also affects a narrow `Eq` value like `r=mythic` specifically was not
re-verified here — flagged as open below, not assumed either way.

### Hypothesis, stated falsifiably, and the verdict

**Hypothesis**: `domain_cards`/`eval_domain` for this population is not "estimated," it is a plain `min`
over each individual child's own marginal count (via one of two independent code paths — `best_other`'s
gate, or `domain_cards_before_card`'s untightened `candidate` fallback), which silently discards whichever
side of the `And` the `min` doesn't pick — and the estimate reads as "accurate" if and only if the
discarded side happens to be near-100% selective (so discarding it costs little), regardless of which
family or field that side belongs to.

**Test**: constructed queries where the card-invariant partner is deliberately near-vacuous (`f:commander`,
99.8%) versus genuinely selective (`f:modern`, 70.8%) alongside a badly-broken leaf (`border:white`).
**Confirmed**: both restore `best_other` and make `eval_domain` exact (ratio 1.006 and 1.000
respectively) — the fix works whether or not the added partner narrows anything, because it is a *gating*
fix (does an exact joint popcount run at all), not an *accuracy* fix for an existing estimate. Also
confirmed the corollary: `border:black` itself is NOT reliably clean absent the second-arith-child
coincidence (ratio 0.624 at width 1, becoming 1.008 once a card-invariant partner is added) — refuting the
version of the hypothesis that would say "border:black is intrinsically well-modeled." It isn't; it's
lucky, twice, in slightly different ways depending on range width.

## What a fix would need to do (a sketch, not a design)

Two complementary ingredients, both confirmed as real by the data above, likely both wanted together:

1. **Drop the `!card_invariant.is_empty()` requirement in `best_other`'s `else if` branch** (line 7845),
   so a lone existential leaf (no card-invariant partner) still gets its own exact popcount via
   `popcount_with_bits(Some(e))` with an empty `card_invariant` vec — this is the direct fix for the
   *gating* bug, confirmed to work by the `f:commander`/`f:modern` natural experiment above (which works
   *because* it flips this exact gate, not because those queries are special).

2. **Prefer an existing precomputed exact count over a fresh `eval_planes`+`popcount`, where one exists**,
   rather than materializing a bitmap purely to get a scalar. `exact_result_total` already has `vt.border`
   (a direct O(1) 3-space lookup, confirmed used by `border`'s own leaf arm already) and `vt.legality`
   (`legality_totals_key`-keyed, same shape, confirmed to exist for legality too). Whether rarity has an
   equally safe equivalent for a narrow `Eq` value specifically (as opposed to the documented-broad-range
   bug in `RangeCardCounts::distinct_cards`) is unresolved — worth checking directly before relying on it,
   not assumed by this doc. The BITS (needed separately for the arith-ID-probe merge a few lines below
   `best_other`) may or may not need a fresh `eval_planes` call at all if a single leaf's compiled
   `PlaneExpr` already resolves to a direct slice of `indexes.planes.words[...]` — not confirmed here,
   worth a look before assuming the cheap-count path and the bits path have to be the same call.

3. **Stop discarding `folded.result.card`/`folded.candidate.card` at the final struct construction**
   (line 7948) when `exact_domain_cards` is `None` — a free, already-computed, strictly-safe `.min()`
   floor (an individual child's own exact count is always ≥ the true joint), covering border today and
   any other leaf whose own arm already populates `.card`, with zero new per-query cost. This is a partial
   complement to (1)/(2), not a substitute — it only ever tightens to "the best single child's own count,"
   never to the true joint intersection two-or-more constraints would give.

Whichever combination ships, the next round should re-run the exact `fit_round20.py` joint-refit protocol
Round 20 built (design fully specified in that doc, not checked in) — once `eval_domain` is trustworthy
across leaf values, the `GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS`/leaf-count-rate joint fit Rounds
19-20 couldn't validate becomes testable for real, on the same sample construction already built for that
purpose.

## Open questions / what's still uncertain

- **Does rarity's known `RangeCardCounts::distinct_cards` bug (documented for broad ranges) also affect a
  narrow `Eq` value?** Not re-verified here; `compose_printing_estimate`'s rarity leaf arm blanket-disables
  `.card` for the whole `NumericCmp{RarityInt}` family regardless of comparison operator, so this doc
  cannot tell whether that blanket is itself over-broad.
- **Legality's own `best_other` behavior for a divergent format was not separately re-verified past
  Round 15/16's existing fix** (`plane_expr_is_existential`) — this doc's data is entirely border/rarity;
  `f:oldschool` was swept in Question 1's ratio table but not independently traced through `best_other` the
  way border/rarity were.
- **Whether the arith-ID-probe merge's bits can come from a stored slice instead of a fresh `eval_planes`
  call** (ingredient 2's second half) is a real-cost question for whoever implements the fix, not answered
  by this investigation round.
- **The real-traffic frequency estimate (Question 1) is a rough regex proxy**, not an AST-level
  classification of "empty card_invariant" — likely an over-count relative to the exact bug population,
  for the reason stated (an incidental card-invariant leaf elsewhere in a real query cures it as a side
  effect). A precise count would need to instrument the actual gate, not query text.
