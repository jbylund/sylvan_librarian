# GatheredScan Under-Costed for a cmc-Range AND a Card-Invariant Existential Leaf

Found while looking for the highest-latency real queries in a 211k-query uniform sample
(`docs/issues/local-engine-gathered-scan-card-printing-varying-depth.md`'s benchmark corpus/protocol) and
checking whether the router picked the best plan on each. Not yet fixed — this is the starting point for
whoever picks it up.

## The miss

`cmc>=1 cmc<=5 border:black`, `unique=card`, `orderby=rarity`, `direction=desc`, `limit=175`, `offset=0`:
one of the 25 highest-latency real queries in the sample, and one of only two in that top-25 where routing
missed the best plan.

```
routed:  GatheredScan     1,155,375 ns  (measured)
best:    PrintingCompose    581,708 ns  (measured)
regret:    573,667 ns  (~2x)
```

## Reproducing

```python
from scripts import costbench
from api.parsing import parse_scryfall_query
engine = costbench.load_engine(pathlib.Path("benchmarks/bitplanes/corpus.jsonl"), pathlib.Path("<scratch>/store"))
kw = dict(filters=parse_scryfall_query("cmc>=1 cmc<=5 border:black"), unique="card", orderby="rarity",
          direction="desc", limit=175, offset=0, prefer="default")
acquire = engine.explain(**kw)["acquire"]
res = engine.explain_analyze(num_warmups=3, num_trials=15, **kw)
```

## Diagnosis: `PrintingCompose`'s estimate is fine here — `GatheredScan`'s is the one that's wrong

Reconstructed both plans' `predicted_ns` term-by-term from the real `acquire` feature dump and `cost.rs`'s
constants, and both formulas reproduce the reported `predicted_ns` almost exactly — so the feature values
below are trustworthy, not an artifact of a different bug in the reconstruction:

**`PrintingCompose`**: `broadcast_printings=181,706`, `project_printings=83,894`, `popcount_words=496`,
`compose_paging=OrderbyWalk` (`printings_walked=1,011`).
```
build = 181,706*1.93 + 83,894*1.93 + 496*1.07 = 513,139
page  = 1,011*0.58 + 175*2.19                = 970
total = 513,139 + 970 + 163.56               = 514,272   (reported: 514,272.07 — exact match)
```
Real measured trials: 542,166 – 636,958 ns. **Ratio ~1.13-1.2x — reasonably well-calibrated.** `broadcast_printings`
alone is 68% of this total, and it's driven entirely by the bare `cmc` range: re-querying `cmc>=1 cmc<=5`
alone (no `border`) reproduces the identical `broadcast_printings=181,706`, while `border:black` alone gives
`broadcast_printings=0` — confirming `border` reads a precomputed plane (cheap) and `cmc`'s own card-invariant
broadcast is the real, correctly-priced cost driver here, not a bug in `PrintingCompose`'s own arm.

**`GatheredScan`**: `eval_domain=24,734`, `scan_units=83,894`, `matches=24,543`, `residual_tier_ns100=0`
(i.e. "nothing to verify" — the `tier_ns > 0.0` gate in `cost.rs`'s `GatheredScan` arm never fires).
```
loop    = 24,734*3.88   = 95,968
scan    = 83,894*2.06   = 172,822
push    = 24,543*2.24   = 54,976
collect =    175*9.79   =  1,713
total   = 95,968+172,822+54,976+1,713+169.6 = 325,649   (reported: 326,262.98 — matches within rounding)
```
Real measured trials: 1,015,209 – 1,290,166 ns. **Ratio ~3.1-4.0x — this is the actual bug.**

If `residual_tier_ns100` were nonzero instead of 0 (charging `GATHER_CARD_PASS_NS + GATHER_RESIDUAL_FLOOR_NS`
per candidate, the formula's own floor for "there is something to verify"): `24,734 * (3.00 + 18.89) = 541,427`
additional ns → a would-be total of **867,076**, closing most (not all) of the gap to the measured range. This
doesn't prove the mechanism, but it's the single largest lever in the formula and the most likely place to
look first.

## Where to look

- `card_engine/src/lib.rs`, the `PrintingCompose`-acquire branch of `acquire_plan_features` (search for where
  `tier`/`residual_tier_ns100` gets decided — `verify_cost_tier_unproven`, `nothing_to_verify`,
  `compose_leaf_nothing_to_verify`, `card_invariant_domain_exact` are the names that came up investigating
  nearby rounds this session; none were traced against this specific shape). The question: for an `And` of an
  arith-tuple range (`cmc`) and a card-invariant existential leaf (`border`), does whatever proves "nothing
  left to verify" actually hold for `GatheredScan`'s own per-candidate pass, or is it borrowing a proof that's
  only valid for a different plan/mechanism?
- `card_engine/src/cost.rs`: `GATHER_CARD_PASS_NS` (3.00), `GATHER_RESIDUAL_FLOOR_NS` (18.89), and the
  `tier_ns > 0.0` gate in the `GatheredScan` arm of `plan_cost`.
- Cross-check against the exact-tightening machinery already built in `compose_printing_estimate` for
  arith+existential combinations (`compose_printing_estimate`'s `And` arm, `best_other`, `arith_tuple_count`,
  the ID-probe merge) — this may be a downstream consequence of one of those mechanisms correctly proving an
  exact CARD COUNT while something else incorrectly reads that as "no residual work at all" for `GatheredScan`
  specifically.

## Open questions (not resolved here)

- **Does the mis-route need the `AND` with `border:black`, or does bare `cmc>=1 cmc<=5` alone already
  mis-route?** Only the *feature* values were isolated (both give `broadcast_printings=181,706`), not full
  routing — worth checking before assuming the `And` combination itself is load-bearing.
- **Is this the same root cause as the other `printing_compose`-acquire miss in the same top-25**
  (`f:commander year>2003`, unique=artwork, a much smaller ~43,300 ns/~5% miss in the other direction —
  `GatheredScan` picked when `PrintingCompose` was actually 43µs better)? Not checked — could be the same
  `tier` classification issue manifesting in both directions, or two unrelated mechanisms.
- **Real-traffic size of this population.** Not measured — a natural next check is
  `bench_pairwise_ordering.py` sliced to this AST shape (arith-tuple range AND card-invariant existential
  leaf, `printing_compose` acquire) to see whether this is a rare edge case or a real regret contributor
  worth its own round.

## Related

- [local-engine-gathered-scan-card-printing-varying-depth.md](local-engine-gathered-scan-card-printing-varying-depth.md) —
  the session-long effort this was found during; its Rounds 1-9 fixed `domain_cards`/`eval_domain` accuracy
  for *printing-varying* range leaves, not `cmc` (card-invariant) — a different population from this one.
- [00852-engine-compose-acquire-p3-p4-ranking.md](00852-engine-compose-acquire-p3-p4-ranking.md) — the
  `GatheredScan`/`StreamedSelect` pair, resolved; this doc is the `GatheredScan`/`PrintingCompose` pair,
  still open.

## Follow-up round: root cause found, fixed (correction to this doc's own title)

Picked this doc up and answered the three open questions with real data before touching code, per this
round's brief. Correction up front: **the title's "card-invariant existential leaf" is a contradiction in
terms, and `border` is not one** — see Q2 below. `border` (and `rarity`) are printing-VARYING, which is
*why* they are existential. The bug is exactly that the router's tier logic treated them as if they were
card-invariant like `cmc`/`color`/`type`/`devotion`.

### Q1 — does the mis-route need the `And` with `border:black`?

Yes. Ran `explain_analyze` on bare `cmc>=1 cmc<=5` alone (same `unique=card`, several `orderby`/`limit`/
`offset` combos) against the real corpus:

```
cmc>=1 cmc<=5            orderby=rarity desc limit=175  -> GatheredScan picked, median 184,208 ns
                                                            PrintingCompose median 542,292 ns (NOT picked)
```

`GatheredScan` really is ~3x faster here and the router picks it correctly. Adding `border:black` blows
`GatheredScan`'s REAL time up ~6x (184,208 -> 1,155,375-1,290,166 ns) while its *predicted* cost barely
moves (327,291 -> 326,263 — `PlanFeatures` even reads slightly cheaper). The `And` is load-bearing: this
is not a bare-range problem.

### Q2 — where does `residual_tier_ns100` actually get set to 0, and is the classification wrong or is the bug elsewhere?

Traced it exactly. `cmc>=1 cmc<=5 border:black` under `unique=card`: **both** `cmc>=1`/`cmc<=5`
(`compile_numeric_cmp`) and `border:black` (`compile_border_cmp`) compile into `PlaneExpr`s
(`planes.rs::compile_plane`), and `split_planes`'s whole-filter shortcut folds the entire `And` into ONE
plane, leaving the residual `filter == FilterExpr::True`.

`acquire_plan_features`'s `PrintingCompose`-acquire branch then asks `plane_leaves_nothing_to_verify`:

```rust
fn plane_leaves_nothing_to_verify(filter, mode, plane, indexes) -> bool {
    matches!(filter, FilterExpr::True)
        && plane.is_none_or(|expr| {
            matches!(mode, Mode::Card) || !plane_expr_is_existential(expr, divergent_formats)
        })
}
```

For `Mode::Card` this returns `true` **unconditionally**, regardless of what the plane actually contains.
Its own doc justifies this only for *legality*: "the card has some legal printing" is exactly what
`unique=card` wants, so the #667 carveout lets `Mode::Card` skip re-verifying a divergent legality format
per printing. But `plane_expr_is_existential` is not legality-specific — `planes.rs::
needs_printing_verification` says plainly: **"for rarity and border that is every leaf"** (unconditionally
existential, unlike legality's per-format `divergent_formats` gate). The blanket `matches!(mode, Mode::Card)
||` bypass does not distinguish "legality, existential only for one divergent format" from "border/rarity,
always existential" — it grants the SAME free pass to both.

**So: the classification is a real bug, not a borrowed proof.** It is not "correct classification, bug
elsewhere" — `plane_leaves_nothing_to_verify`'s own Mode::Card carveout is unsound whenever the plane
touches rarity or border, and that unsoundness is exactly what leaks into `tier`/`residual_tier_ns100`.

A second, independent instance of the same conceptual bug turned up while verifying the fix: even after
correcting the plane-side check, `t:swamp tou=5 border:black`/card still read `residual_tier_ns100 == 0`,
because the OTHER disjunct, `compose_leaf_nothing_to_verify(filter)`, fires whenever the *residual* is a
bare safe collection leaf (`t:swamp`) — correct on its own terms (subtypes really are card-invariant) but
blind to what the `plane` alongside it contains. The original code OR'd two whole-query claims
(`plane_leaves_nothing_to_verify(filter, mode, plane, ..) || compose_leaf_nothing_to_verify(filter)`) when
what was needed was an AND of two HALF claims (filter side AND plane side each independently have nothing
left to verify).

### Q3 — what does the real executor do, and is the per-printing work genuinely necessary?

Traced `exec_gathered_scan`/`push_card_matches` directly (with temporary `eprintln!` instrumentation,
since reverted). `prepare_candidates`'s `all_match_known` is (harmlessly) `true` in both the border and
no-border cases — but a SEPARATE mechanism, `existential_plane_for` (gated on `plane_expr_is_existential`
with NO Mode::Card carveout at all), independently returns `Some` whenever the plane touches an existential
leaf, and forces `push_card_matches` into a per-printing loop that calls `eval_plane_expr_for_printing` on
each printing until one satisfies the FULL plane (both the constant `cmc` bit and the per-printing `border`
bit) — because `unique=card` still must return an ACTUAL border:black printing as the result row, not
merely prove one exists somewhere in the card's span. Confirmed against the real counters: `printings_examined`
(27,142) exceeds `cards_visited` (26,905) by 237 — cards whose first-checked printing didn't happen to be
black and needed a second look — proving the per-printing walk is real, not a costing artifact.

This work is genuinely necessary for correctness (unlike legality's carveout, which is a deliberate,
documented product decision that `unique=card` need not re-verify format legality per printing) — border
really can vary printing to printing for the same card, and the row returned has to actually match. The
cost model must charge for it; it cannot be modeled away.

### Q4 — how big is this population in real traffic?

Sampled `client.query_sampler.QuerySampler` in `realistic` mode: in 60s (31,111 queries, 7,928
`printing_compose`-acquire), 2,424 had `residual_tier_ns100 == 0` with `GatheredScan` picked, of which **61
(2.5%) touched `border`/`rarity`** — mostly common shapes like `r:rare t:plains`, `c:w r:mythic`,
`border:black t:shapeshifter`. This is not rare in the sense of "never happens" — rarity/border combined
with a type/color/keyword leaf is a completely ordinary real query.

But most of those 61 are SMALL-domain queries (a type/subtype leaf narrows hard), where the added tier
charge is a few µs against a query that was already single-digit-µs to begin with — spot-checked six of
them directly and `GatheredScan` remained correctly picked (and still fastest measured) both before and
after the fix. The specific sub-case that produces a *large*, `2x`-latency-class regret — a BROAD
card-invariant range (`cmc`/`power`/`toughness`) that leaves a large candidate domain, ANDed with
`border`/`rarity` — is a narrower slice of that population. This matches the flat aggregate
`bench_pairwise_ordering.py`/`bench_regret_matrix.py` numbers below: real, worth fixing (it's free and
correctness-preserving), but not a population large enough to move whole-corpus aggregates on its own.

### The fix

`card_engine/src/lib.rs`: added `plane_touches_rarity_or_border` (walks a compiled `PlaneExpr`, true iff
any leaf's plane index is `>= PLANE_RARITY` — rarity and border are the last two plane families,
contiguous through `PLANE_COUNT`, so an index compare identifies them exactly with no new table to keep in
sync with `planes.rs`'s private `PLANE_BLOCKS`) and `cost_plane_nothing_to_verify` (the plane-only half of
the check, with the `Mode::Card` bypass scoped to exclude a plane touching rarity/border). The `tier`
computation now ANDs the filter half and the plane half independently:

```rust
let filter_nothing_to_verify = matches!(filter, FilterExpr::True) || compose_leaf_nothing_to_verify(filter);
let nothing_to_verify = filter_nothing_to_verify && cost_plane_nothing_to_verify(mode, plane, indexes);
let tier = if nothing_to_verify { 0 } else { verify_cost_tier(composed) };
```

Deliberately scoped to ONLY this call site (the `PrintingCompose`-acquire branch's `tier` decision, the
one term the tracking doc's diagnosis identified as the actual bug). `plane_leaves_nothing_to_verify`
itself — used by the EXECUTOR's `all_match_known` in `prepare_candidates` — is untouched: granting
Mode::Card's bypass to rarity/border there is harmless (it only skips a redundant, already-cheap
`card_pass` call; the real per-printing correctness work runs through the wholly separate
`existential_plane_for` mechanism regardless of what `all_match_known` says). The other call site of
`plane_leaves_nothing_to_verify` (the `eval_domain`/`scan_units` broad-reset guard a few lines up) is also
left alone — a different concern (domain-size estimation, not verify cost) that this round's diagnosis did
not implicate.

**Pre-computation check**: the fix adds one small, bounded-size `PlaneExpr` tree walk (typically 1-5
nodes) once per acquire — no new per-candidate, per-match, or per-printing work, and no new index probe.
Cost is independent of corpus size, match count, or candidate count.

### Before / after (real corpus, original reproducer)

```
cmc>=1 cmc<=5 border:black, unique=card, orderby=rarity, direction=desc, limit=175, offset=0

BEFORE: GatheredScan     predicted 326,263 ns  picked=True   median 1,155,375-1,290,166 ns
        PrintingCompose  predicted 514,272 ns  picked=False  median   542,166-  636,958 ns

AFTER:  PrintingCompose  predicted 514,272 ns  picked=True   median   478,333-  511,166 ns
        GatheredScan     predicted 728,028 ns  picked=False  median 1,098,541-1,288,333 ns
```

Router now picks the actually-faster plan — a measured ~2.3-2.4x real latency win on this exact query.
`GatheredScan`'s revised `predicted_ns` (728,028) still under-charges the real 1.0-1.3ms (the tier's flat
`GATHER_RESIDUAL_FLOOR_NS`-based charge isn't calibrated for a per-printing existential walk specifically),
but the ARGMIN decision is what matters and it's now correct — no attempt was made to tighten the
absolute number further, since the primary metric for this round is ordering, not agreement.

### Correctness gate

`cargo test --manifest-path card_engine/Cargo.toml --release`: **168/168 passed** (167 pre-existing + one
new regression test, `compose_tier_charges_border_existential_and_arith_range`, added to `tests.rs` — a
minimal fixture reproducing the exact AST shape, asserting `residual_tier_ns100 > 0` for the `cmc`+`border`
`And` and `== 0` for the bare `cmc` range control). `cargo clippy --all-targets -- -D warnings`: clean.

### Confirmation pass

`bench_pairwise_ordering.py --seconds 60` (`GatheredScan` vs `PrintingCompose`, `printing_compose` acquire),
baseline vs fix, both uniform and realistic mode:

```
uniform:    baseline 86% ordered right, 5.47µs mean regret  ->  fix 86%, 5.51µs   (flat, within noise)
realistic:  baseline 90% ordered right, 3.32µs mean regret  ->  fix 90%, 3.24µs   (flat, within noise)
```

No aggregate movement either direction, consistent with Q4's population-size finding — the fix corrects a
real, narrow sub-population that doesn't dominate this pairwise slice's total regret. No regression.

`bench_cost_model_agreement.py --seconds 60`, `GatheredScan`/`card`: baseline median 0.81 (25% within 25%)
-> fix median 0.81 (25% within 25%) — unchanged, still PASS.

`bench_regret_matrix.py --seconds 120 --mode realistic`: baseline total regret 47.2ms over 52,384 queries
-> fix 45.2ms over 54,435 queries (~4% lower, more queries fit the same wall-clock budget because fewer
ran the now-corrected expensive misroute) — no regression, mild improvement, within this benchmark's
sample-to-sample noise band.

`bench_query_latency_ab.py --sample 400 --mode realistic --seed 7`, plus a same-build canary at the same
seed:

```
canary (baseline vs baseline):  B - A = -0.6µs  95% CI [-0.9, -0.4]   (noise floor)
baseline vs fix:                B - A = +0.1µs  95% CI [-0.2, +0.3]   NO DETECTABLE DIFFERENCE
```

Both indistinguishable from the noise floor — expected at n=400 given the affected shape's rarity (Q4).
No regression on general realistic-mode latency.

### Outcome

**Fixed.** Real bug (not a rare/skip-it case, not a borrowed-proof-elsewhere case), cheap to fix (no
hot-path cost added), shipped with a passing correctness gate and no detected regression on any
confirmation metric. The Phase A Q4 population size (rarity/border combined with something else is a
common query shape) argues this was worth fixing on principle even though it's invisible in whole-corpus
aggregates; the specific large-regret sub-case (broad card-invariant range AND rarity/border) is real and
now routes correctly.

## Round 16: the fix above was field-specific, and reproduced its own bug for a different field

The round before this one shipped `plane_touches_rarity_or_border` -- a plane-INDEX-RANGE check
(`(*p as usize) >= PLANE_RARITY`) -- to scope the `Mode::Card` bypass off rarity/border. A review of that
fix raised the architectural objection this round exists to answer: the conditional should key off
BEHAVIOR (does a plane's existential semantics force per-printing re-verification even under
`Mode::Card`?), not off which specific FIELD is touched. The number of behavioral categories grows far
slower than the number of query attributes, and hardcoding a field-identity check (a plane-index range)
where a behavioral one already existed in `planes.rs` was the same shape of mistake the original bug was
built from, just one level up.

### Is the gap real? Yes -- confirmed by reproduction, not just by reading the code

`cost_plane_nothing_to_verify`'s shipped shape was `(Mode::Card && !plane_touches_rarity_or_border(expr))
|| !plane_expr_is_existential(expr, divergent_formats)`. For a plane that is existential ONLY via a
DIVERGENT legality leaf (no rarity, no border anywhere in it), `plane_touches_rarity_or_border` returns
`false` -- so the first disjunct is `Mode::Card && true`, which is `true` under `Mode::Card` regardless of
what the second disjunct would say, short-circuiting the OR. This reproduces the exact bug shape Round 15
fixed, scoped to legality instead of rarity/border.

Reproduced on the real corpus (`oldschool` is the production corpus's one divergent format):

```
f:oldschool, unique=card, orderby=rarity desc, limit=175:
  residual_tier_ns100 == 0 (bug fires)
  GatheredScan predicted 10,358ns   measured median 29,083ns   ratio 0.36  (2.8x under-charge)
  printings_examined=6,037 vs cards_visited=961 (6.3 printings/card -- the per-printing walk is real)

f:oldschool cmc>=1 cmc<=5, unique=card, orderby=rarity desc, limit=175 (same AND shape as the original
cmc+border finding, legality instead of border):
  residual_tier_ns100 == 0 (bug fires)
  GatheredScan predicted 18,519ns   measured median 58,625ns   ratio 0.32  (3.16x under-charge)
```

Control (`otag:triggered-ability` alone, no plane at all): predicted 124,633ns vs measured 101,667ns,
`cards_visited == printings_examined` (no existential walk) -- confirms the gap is specific to the
existential-plane case, not a general costing artifact.

### Mechanism: why legality's card-mode bypass is sound for a NON-divergent format, and why it was never sound for a divergent one

Traced `existential_plane_for` (lib.rs) and `push_card_matches`'s `existential_plane` branch directly,
rather than trusting the doc comments that motivated Round 15's carveout. The answer turned out simpler
than "legality is special": **`existential_plane_for` grants NO per-family carveout at all.** It is:

```rust
fn existential_plane_for(mode, plane, indexes) -> Option<...> {
    match (mode, plane) {
        (Mode::Card, Some(pe)) if plane_expr_is_existential(pe, divergent_formats) => Some((pe, planes)),
        _ => None,
    }
}
```

Whenever this returns `Some`, `push_card_matches` walks printings one by one
(`eval_plane_expr_for_printing`) to find an ACTUAL witnessing printing for row selection (#667: "the card
has some legal printing" is enough for the COUNT, but `unique=card` still must return a printing that
really satisfies the query) -- for rarity, border, OR a divergent legality format, identically. There is
no separate, cheaper mechanism for legality. Confirmed empirically: `f:oldschool` alone shows
`printings_examined` (6,037) far exceeding `cards_visited` (961) -- the walk is real, not a costing
artifact, for legality just as it was for border in Round 15.

So "needs per-printing re-verification under Mode::Card" and "is this a printing-level property" are NOT
two concepts to reconcile -- they are the same fact, traced end to end. A property is printing-level
exactly when two printings of one card can disagree on it, and that is exactly when
`existential_plane_for` forces the row-selection walk. Rarity and border are STATICALLY printing-level
(the field structurally allows disagreement, unconditionally). Legality is the one property that is
DYNAMICALLY either bucket, resolved per format by `divergent_formats` (data-derived per store): card-level
for a format every printing happens to agree on (31 of 32 formats in the production corpus), printing-level
for one where they don't (`oldschool`). `needs_printing_verification`/`plane_expr_is_existential`
(`planes.rs`) already compute exactly this, per plane index, via the family-keyed `PLANE_BLOCKS` table --
this was never a fact `cost_plane_nothing_to_verify` needed a NEW field-specific check to derive; it needed
to stop re-deriving a narrower, wrong version of a fact `planes.rs` already had exactly right.

One more invariant closes the loop: `split_planes` (planes.rs) only ever folds an existential leaf into
`plane` under `unique_is_card` (its whole-filter and `And`-child guards are both `unique_is_card ||
!plane_expr_is_existential`) -- so `plane_expr_is_existential(plane)` being true already implies `mode ==
Mode::Card`. There is no live case where `mode` needs to appear in `cost_plane_nothing_to_verify` at all;
the `Mode::Card` term in both the buggy Round 15 shape and a naive "just drop the field check" fix would
be vestigial, not a correctness need.

### The fix: delete the field-specific check, use the general one directly

`card_engine/src/lib.rs`: deleted `plane_touches_rarity_or_border` entirely. `cost_plane_nothing_to_verify`
is now:

```rust
fn cost_plane_nothing_to_verify(plane: Option<&PlaneExpr>, indexes: &Archived<CardIndexes>) -> bool {
    plane.is_none_or(|expr| !plane_expr_is_existential(expr, u64::from(indexes.planes.divergent_formats)))
}
```

No `mode` parameter, no per-family branch, no new table: `plane_expr_is_existential` (planes.rs) already
is the general, field-agnostic predicate, keyed by family through `PLANE_BLOCKS`/`ExistentialLeaf`/
`needs_printing_verification`, not by ad hoc field identity. A future printing-varying field is handled
correctly automatically by which `PLANE_BLOCKS` entry it lands in -- no new arm needed in this file at all.
`card_engine/src/planes.rs` gained doc-comment-only clarifications on `ExistentialLeaf` and
`needs_printing_verification` making this "same fact, not two concepts" framing explicit (no logic
changes there).

### Checking for a duplicate, table-driven property classifier already existing elsewhere: not needed, but three OTHER card/printing classifiers already exist for different purposes

Before extending `planes.rs`'s table, checked whether the crate already had a canonical card-vs-printing
classifier being duplicated. It does not need a NEW one -- `plane_expr_is_existential` already was one --
but three OTHER classifiers exist, each scoped to a different purpose, each already documenting its own
deliberate disagreement with the canonical table on legality specifically:

- **`estimator.rs::has_printing_varying_leaf`** (ANY-composition, for cardinality estimation): treats
  `FilterExpr::Legality` as ALWAYS printing-varying, ignoring `divergent_formats` entirely. Its own doc
  comment already flags this: "conservative... even though `printing_dependent` ranks it invariant for
  its own (common-case) reason." Conservative in the estimator's own safe direction (overestimates
  variance), not a silent-zero-cost bug.
- **`filter.rs::printing_dependent`/`leaf_compares_printing_field`** (verify-ORDER heuristic, ALL-
  composition): treats `FilterExpr::Legality` as ALWAYS card-level, the OPPOSITE bias, also already
  documented: "Divergent-legality cards defer to the printing, but they are a rare exception... rank by
  the common card-level case." This only affects which child a verifier checks first, never correctness
  -- a suboptimal order, not a wrong answer.
- **`lib.rs::is_broadcast_leaf_shape`/`is_broadcast_composable`**: NOT a duplicate -- `is_broadcast_composable`
  and `broadcast_composable_card_bits` call `plane_expr_is_existential` directly as their own gate. Already
  unified with the canonical table by construction.

Neither of the first two is the same shape of bug as this round's finding: both are DOCUMENTED, ONE-
DIRECTION approximations for a heuristic or an estimate, not a place where real per-printing work gets
silently priced as free. Flagging as a candidate for a future doc (a single canonical property table the
first two could read from instead of carrying their own copy of the leaf list) -- not attempted here;
out of scope for this round, which is the router's cost-tier fix only.

### Combinations verified (real corpus, `unique=card`, `orderby=rarity desc`, `limit=175`)

| combination | predicted (before → after) | measured median | ratio (after) |
|---|---|---|---|
| rarity alone (`r:mythic`) | 24,087 (unchanged) | 20,834–21,083 | 1.14–1.16 (unaffected, already correct) |
| border alone (`border:black`) | 166,380 (unchanged) | 137,375–146,000 | 1.14–1.21 (unaffected, already correct) |
| non-divergent legality alone (`f:modern`) | tier stays 0 (unchanged) | n/a | correct both before/after |
| **divergent legality alone (`f:oldschool`)** | 10,358 → 31,394 | 25,917–29,083 | **1.08–1.21 (fixed, was 0.36)** |
| rarity + divergent legality (`f:oldschool r:mythic`) | 49,021 (unchanged) | 6,375–6,875 | unaffected (Round 15 already covered this: the plane touches rarity) |
| border + divergent legality (`f:oldschool border:black`) | 50,121 (unchanged) | 48,291–51,041 | 0.98–1.04 (unaffected, Round 15 already covered this) |
| **divergent legality + card-invariant range (`f:oldschool cmc>=1 cmc<=5`)** | 18,519 → 51,276 | 56,959–58,625 | **0.90–0.91 (fixed, was 0.32)** |

The two combinations Round 15 already handled correctly (anything touching rarity/border, alone or
combined with legality) are unchanged by this round's fix -- `plane_expr_is_existential` agrees with
`plane_touches_rarity_or_border` whenever rarity/border is present; it only disagrees (correctly) when
the ONLY existential leaf is a divergent legality format.

### Correctness gate

`cargo test --manifest-path card_engine/Cargo.toml --release`: **172/172 passed** (168 pre-existing +
3 new property tests in `planes.rs` asserting the `PLANE_BLOCKS` family invariant holds for every plane
index -- rarity/border unconditionally existential, legality existential iff its own format's bits are
set in `divergent_formats`, everything else never existential -- for a matrix of representative
`divergent_formats` masks (0, `u64::MAX`, one arbitrary bit), plus 1 new concrete regression test,
`compose_tier_charges_divergent_legality_existential_and_arith_range`, mirroring Round 15's
`compose_tier_charges_border_existential_and_arith_range` fixture shape with legality instead of border
and asserting both directions: `cmc` range + DIVERGENT-format legality must charge a nonzero tier, `cmc`
range + NON-divergent-format legality must stay free). Round 15's own regression test still passes
unchanged. `cargo clippy --all-targets -- -D warnings`: clean.

Pre-computation check: the fix is a net REDUCTION in per-acquire work versus Round 15's shape -- one
`PlaneExpr` tree walk (`plane_expr_is_existential`) instead of two (`plane_touches_rarity_or_border` plus
the `plane_expr_is_existential` fallback the OR could still reach). No new per-candidate, per-match, or
per-printing work; cost is still independent of corpus size, match count, or candidate count.

### Confirmation pass

`bench_pairwise_ordering.py --seconds 300`, baseline vs fix, both modes (printing_compose acquire slice,
the one this fix touches):

```
realistic:  baseline 90% ordered right, 3.06µs mean regret  ->  fix 90%, 3.01µs   (flat, within noise)
uniform:    baseline 86% ordered right, 4.94µs mean regret  ->  fix 86%, 5.06µs   (flat, within noise)
```

`bench_cost_model_agreement.py --seconds 300 --seed 0`, `GatheredScan`/`card`: baseline median 0.79 (25%
within 25%) -> fix median 0.79 (25% within 25%) -- unchanged.

`bench_regret_matrix.py --seconds 120 --mode realistic --seed 0`: baseline total regret 41.4ms over
55,944 queries -> fix 41.6ms over 55,963 queries -- flat (+0.5%), within sample-to-sample noise.

`bench_query_latency_ab.py --sample 800 --mode realistic --seed 7`, interleaved A/B/A, plus a same-build
canary at the same seed:

```
canary (baseline vs baseline):  B - A = -0.8µs  95% CI [-0.9, -0.6]
baseline vs fix:                B - A = -0.7µs  95% CI [-0.9, -0.5]   INDISTINGUISHABLE FROM CANARY
```

No detectable difference from the fix on general realistic-mode latency, as expected given the affected
shape's rarity in the overall query mix (one divergent format in the production corpus).

### Outcome

**Fixed, and generalized.** The divergent-legality gap was real (2.8–3.2x under-charge, confirmed by
reproduction before touching code) and is now closed by deleting the field-specific check Round 15 added
and routing through the general, already-existing `plane_expr_is_existential` predicate instead -- which
also removes a `mode` parameter that turned out to be redundant by construction. Blast radius: `lib.rs`
(the fix), `planes.rs` (doc clarifications + 3 new property tests), `tests.rs` (+1 concrete regression
test). No hot-path cost added (net cheaper than Round 15's shape). No regression on any confirmation
metric. The three other card/printing classifiers found while checking for duplication (`estimator.rs`,
`filter.rs`) are flagged as a candidate for a future unification doc, not attempted here.

## Round 17: the flat per-candidate charge is real, but a depth term doesn't fix it -- negative result

Round 16 fixed the classification bug (`tier` correctly reads nonzero for an existential plane) but left
a note that the reproducer's `GatheredScan` prediction (728,028ns) still undershot the measured range
(1,015,209-1,290,166ns), and hypothesized the mechanism: `push_card_matches`'s `Mode::Card`/
`Prefer::Default` arm early-exits (`(start..end).find(|&pid| satisfies(pid))`), so the number of
printings actually visited per candidate depends on the existential leaf's own selectivity, not a flat
per-candidate constant -- an "expected walk depth" problem, the same SHAPE Round 1 of the sibling
`local-engine-gathered-scan-card-printing-varying-depth.md` effort solved for printing-varying RANGE
leaves (price/date/collector_number). This round picked that up, built a real fix, and then discarded it
after the calibration data itself said no. Recorded here in full because the diagnosis along the way is
the useful part.

### Re-confirming the reproducer, fresh

Rebuilt an isolated release wheel from `costcell/trunk` (Round 16's state) and re-ran the exact
reproducer:

```
cmc>=1 cmc<=5 border:black, unique=card, orderby=rarity desc, limit=175, offset=0:
GatheredScan predicted 728,028ns   measured 1,031,292-1,163,417ns (median 1,136,250)   ratio ~1.4-1.6x
real counters: cards_visited=26,905  printings_examined=27,142  matches_pushed=26,905
real depth (printings_examined / cards_visited) = 1.0088
```

**The early-exit walk essentially never proceeds past the first printing for this exact query** --
average depth 1.009, i.e. almost every candidate's very first checked printing already satisfies
`border:black`. This on its own already says a depth-scaled correction cannot explain this specific
query's gap: at depth ≈ 1 any sound depth model can only multiply the existing charge by ≈1, and the
gap is 1.4-1.6x.

### Quantifying across a broader population: depth is real, but it's not what's wrong here

Sampled 19 hand-picked existential-plane/`Mode::Card`/`Prefer::Default` queries first (varying which
border/rarity value, with and without an ANDed `cmc`/`pow`/`tou` range), then a much larger, non-cherry-
picked sample via `client.query_sampler.QuerySampler` (2,564 rows, `uniform` mode, seed 0; 4,431 rows,
`realistic` mode, seed 1; both filtered client-side to `unique=card` queries whose text touches
`border:`/`r[<>]=?`/`f:oldschool`), recording `GatheredScan`'s `predicted_ns`/`plan_self_ns` (measured)
and the real depth from `printings_examined`/`cards_visited`.

Two clean, well-supported findings came out of the broad sample:

**1. Real depth genuinely predicts under/over-costing, in aggregate:**

```
uniform (n=2,564):                          realistic (n=4,431):
  depth [1.00,1.05)  n=657   median ratio 0.95    depth [1.00,1.05)  n=2,220  median ratio 0.53
  depth [1.05,1.50)  n=201   median ratio 0.75    depth [1.05,1.50)  n=816    median ratio 0.54
  depth [1.50,2.50)  n=188   median ratio 1.01    depth [1.50,2.50)  n=448    median ratio 0.70
  depth [2.50,4.00)  n=181   median ratio 1.38    depth [2.50,4.00)  n=289    median ratio 1.07
  depth [4.00, ∞)    n=1,327 median ratio 2.81    depth [4.00, ∞)    n=583    median ratio 2.17
corr(log depth, log ratio) = 0.50 (uniform), 0.39 (realistic)
```

Not noise: monotonic in both modes, over thousands of rows, and the direction matches the hypothesis
(higher real depth ⇒ more under-costed).

**2. But depth is a property of the LEAF VALUE, not of what's ANDed alongside it -- and for the flagship
reproducer's shape (a common existential value), that intrinsic depth is ≈1, so the "AND" isn't where
the gap comes from.** Confirmed directly: `cmc>=1 cmc<=5 r:mythic` and bare `r:mythic` (no `cmc` at all)
measure the SAME real depth (2.538 vs 2.528) -- the arith range restricts WHICH cards are candidates,
but does not change WHERE in a candidate's own print history the existential value tends to sit. Same
for `cmc>=1 cmc<=5 border:black` (depth 1.009) vs bare `border:black` (depth 1.009). So whatever is
wrong with the flagship reproducer's costing is NOT "the AND makes the walk deeper" -- it is something
else, present regardless of depth.

Isolating the executor's own per-candidate loop cost (`ns_loop / cards_visited`, from `explain_analyze`'s
per-plan phase breakdown) against the model's flat per-candidate charge (`GATHER_LOOP_PER_CARD_NS +
GATHER_CARD_PASS_NS + tier.max(GATHER_RESIDUAL_FLOOR_NS)` = 25.77ns, constant for every row below since
`residual_tier_ns100` reads the same 400 for all of them) shows what that "something else" is:

```
query                              real depth   real ns_loop/candidate   model's flat charge
border:black (bare)                   1.01              9.46                  25.77   (over-charged)
cmc>=1..5 border:black                1.01             36.07                  25.77   (under-charged)
cmc>=2..3 border:black                1.01             28.65                  25.77   (~even)
pow>=1..3 border:black                1.01             30.66                  25.77   (~even)
r:mythic (bare)                       2.53             15.24                  25.77   (over-charged)
cmc>=1..5 r:mythic                    2.54             73.22                  25.77   (under-charged)
```

At the SAME real depth (~1.0-1.01), evaluating the COMPOUND existential plane (the `cmc` bound AND the
`border`/`rarity` equality, both tested per printing by `eval_plane_expr_for_printing`) costs 3-4x more
per candidate than evaluating the BARE existential leaf alone. That is a real, distinct gap -- the
`GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS` constants were fitted against a "one `filter.card_pass`
call" cost shape (see their own docs in `cost.rs`), not against "evaluate a multi-leaf `PlaneExpr`
conjunction per printing" -- but it is a plane-EVALUATION-cost gap, not a depth gap, and it is out of
scope for "design an expected-depth estimate" (this round's brief item 2). Flagging it here rather than
chasing it, since the brief's escape hatch is specifically for exactly this outcome.

### A depth fix was still built and tested, for the population where depth genuinely is the mechanism

Even though depth doesn't explain the flagship reproducer, the broad sample's bucketed table above says
depth-driven under-costing is real SOMEWHERE in this population (the `depth ≥ 4` bucket reads median
ratio 2.2-2.8x). So a real attempt was made: added `PlanFeatures::existential_extra_units` (`cost.rs`),
set only in the `PrintingCompose`-acquire branch's tier decision (`lib.rs`) exactly when `tier != 0`
comes ENTIRELY from the plane (`filter_nothing_to_verify && !nothing_to_verify`, `Mode::Card`,
`Prefer::Default`) -- the precise condition under which `push_card_matches`/`card_match_count` run the
early-exit walk with no separate residual call. Charged in `cost.rs`'s `GatheredScan`/`StreamedSelect`
arms as `existential_extra_units * GATHER_EXISTENTIAL_DEPTH_NS`, additive on top of the existing flat
charge (which already assumes depth 1; `existential_extra_units` is only the printings EXPECTED beyond
that one).

**Deliberately NOT read off the existing `scan_units` feature**, after finding it already carries an
unrelated, pre-existing gap for exactly this population: `scan_units` floors to `domain_cards` (depth 1
assumed) whenever `card_invariant_domain_exact` holds, which reads `composed_card_invariant` from
`filter.rs::touches_printing_field` -- and that function's `Legality { .. } => false` arm (documented,
and already flagged as an accepted one-directional gap in Round 16's own "three other classifiers"
section above) treats EVERY legality leaf as card-invariant, including a DIVERGENT format. Confirmed
live: bare `f:oldschool` measured `scan_units == eval_domain` (961 == 961, depth 1 assumed) against a
REAL depth of 6.28 (`printings_examined`/`cards_visited` = 6,037/961). So `existential_extra_units` was
computed fresh, from the same already-in-scope scalars `scan_all` itself uses (`printing_matches`,
`domain_cards`, `printings_per_card`), independent of `card_invariant_domain_exact` -- no new per-query
scan, same pre-computation shape as Round 1's own feature.

### Why it doesn't hold up: Round 1's order-statistics model itself underestimates depth for this leaf family

Even computed fresh and confound-free, the numbers don't support shipping this. Two problems, found by
looking directly at which rows the fresh feature actually produces a nonzero value for:

**The order-statistics model (uniform-random position among a card's printings) is itself wrong for
existential categorical leaves whose position correlates with print era.** `border:borderless` (bare,
sampled 24 times): `existential_extra_units` = 192 (implying `expected_depth` ≈ 1.51) against a REAL
depth of 6.09 (`printings_examined`/`cards_visited` = 21,185/3,478) -- a 4x underestimate, even with the
`card_invariant_domain_exact` confound removed. The likely reason: `border:borderless`-style values
correlate with a specific print era, and printings are stored in a fixed prefer-desc order, so a card's
few matching printings cluster at one END of its print history rather than landing at a uniformly random
position -- exactly the assumption Round 1's model makes and exactly where a continuous, less era-
correlated field like `price_usd` would not violate it as badly. This is a wrong SHAPE, not a wrong rate:
no single multiplicative constant on top of `expected_depth` can fix an estimate whose underlying
distributional assumption is violated in a data-dependent way.

**The sample has almost no distinct queries to calibrate against.** Of 6,318 broadly-sampled rows (fresh
build, same two-mode sampling as above), only 148 read `existential_extra_units > 0` at all, and of
those, one query (`border:borderless`, repeated by the sampler) accounts for 24 rows and a second
(`r>=special`) for another 6 -- there are not enough DISTINCT queries in reasonable sampling time to fit
or validate a new constant responsibly, even setting the shape problem aside.

**Held-out calibration, run anyway, confirms both problems combined into a fit that shouldn't ship.**
Split by a hash of the query string (even/odd), calibration half n=77, held-out half n=71:

```
calibration half:  fitted rate = 0.036  (statistically indistinguishable from 0, n=77 dominated by ~2
                                          distinct repeated queries)
held-out half:      total abs error, NO fix:        1,597,558
                    total abs error, fitted rate:    1,620,196   (WORSE, not better)
                    median ratio, NO fix:  0.593   median ratio, fitted rate: 0.595  (no change)
```

Applying the fitted correction to the held-out half made total absolute error slightly WORSE, not
better -- a clean, unambiguous "this doesn't hold up" signal, not a marginal call.

### Outcome: discarded, reverted

**Negative result, code reverted.** `cost.rs`/`lib.rs`/`tests.rs` are back to `costcell/trunk` (Round
16's state) -- `git diff --stat costcell/trunk` reads empty. `cargo test --release`: 172/172 passed
(unchanged from Round 16). `cargo clippy --all-targets -- -D warnings`: clean (unchanged, no code to
lint). No bench re-runs against a reverted build -- there is nothing to confirm.

What this round DID establish, worth keeping for whoever picks this up next:

- The flagship reproducer's gap is NOT a depth problem (real depth ≈1.009) -- ruling out this round's
  hypothesized mechanism for that SPECIFIC query, correcting the framing this doc opened with. Its actual
  driver is `eval_plane_expr_for_printing` costing more per call for a COMPOUND plane (arith range AND
  existential leaf) than the `GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS` constants (fitted on a
  single-`card_pass`-call shape) assume -- a plane-evaluation-cost gap, unfixed, a candidate for a future
  round scoped to THAT mechanism specifically (not depth).
- Depth genuinely does drive under/over-costing elsewhere in the existential-plane population (broad
  sample, thousands of rows, monotonic, corr ~0.4-0.5) -- real, but Round 1's uniform-random-position
  `expected_depth` formula underestimates it badly for existential leaves whose matching position
  correlates with print era (`border:borderless` real depth 6.09 against the model's 1.51). A real fix
  needs a different distributional assumption for this leaf family, not a coefficient on the existing
  one -- plausibly a per-(field, value) "typical position within a card's print history" statistic
  computed once at store-build time (alongside `BorderPrintingPlanes`/`RarityPrintingPlanes`), not
  per-query. Not attempted here; flagged as the concrete next step.
- A separate, smaller, already-partially-known gap resurfaced concretely: `filter.rs::touches_printing_
  field`'s documented `Legality { .. } => false` (card-invariant, unconditionally) silently zeroes the
  `card_invariant_domain_exact` depth-1 shortcut's honesty for a DIVERGENT format specifically (bare
  `f:oldschool` reads `scan_units == eval_domain` against a real depth of 6.28). Round 16's doc already
  flagged `filter.rs`'s classifier as a documented, accepted one-directional approximation for VERIFY
  ORDERING; this round found a second, concrete consumer (`card_invariant_domain_exact`'s "no depth-1
  fast path is needed" test) where the same approximation leaks into a materially wrong SCAN_UNITS
  estimate, not just a suboptimal ordering. Not fixed here (out of this round's narrow scope), but worth
  its own line item if `local-engine-cost-model-cleanup-remaining.md` or a similar tracking doc gets
  revisited.

## Round 19: the compound-plane mechanism confirmed twice over, an additive fix built, and discarded --
## it regresses the metric that actually matters

Round 17 flagged, but did not chase, the real driver of the flagship reproducer's remaining gap:
`eval_plane_expr_for_printing` costs more per call for a COMPOUND plane (a `cmc`/`power`/`toughness`
range ANDed with an existential leaf) than the flat `GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS`
constants assume, because they were fit against a single-bare-leaf-check shape. This round quantified
that mechanism precisely with two independent measurements, built a feature and a fix, and discarded it
after a held-out check on the metric that actually drives routing said no -- a second negative result in
this doc, and a useful one: the mechanism is real, but a simple additive linear term makes absolute
routing accuracy worse, not better.

### The compiled tree shape is not what this doc's own brief assumed

Before measuring anything, checked the ACTUAL compiled `PlaneExpr` for `cmc>=1 cmc<=5 border:black`
(`planes.rs::compile_plane`/`compile_numeric_cmp`) rather than trusting this doc's own prior paraphrase
("compiles to `Or([Plane(p1)...Plane(p5)])`, up to 5 lookups"). It does not: `cmc>=1` and `cmc<=5` are
two SEPARATE `NumericCmp` leaves, each compiling to its OWN `Or` over `numeric_layout`'s 13 one-hot
interior planes (plus the shared "hi" bucket), and nothing in `compile_plane`/`compile_plane_children`
intersects the two `Or`s into one narrower one. The real shape is:

```
And([Or(≤14 planes, from cmc>=1's own bound),  Or(≤14 planes, from cmc<=5's own bound),  Plane(border)])
```

20 total `Plane` leaves for the exact flagship reproducer (verified by a node-counting walk over the
real compiled tree, `tests.rs::plane_expr_shape`), 16-28 depending on range width (verified across four
widths: `cmc=3` alone → 16, `cmc∈[2,4]` → 18, `cmc∈[1,5]` → 20, `cmc∈[0,12]` (the full interior range) →
28). This matters for anyone reusing this doc's mechanism description later: the leaf count scales with
BOTH bounds' own `Or` width, not with the intersected range's width.

### Measurement 1: a confound-free kernel micro-benchmark confirms the mechanism directly

Added `tests.rs::plane_eval_compound_leaf_cost` (`#[ignore]`d, real corpus via a `benchmarks` symlink
into the primary checkout -- read-only, nothing under `benchmarks/` touched): compiles a handful of real
`PlaneExpr` trees against the real corpus's `BitPlanes`/`OracleWordIndex`, finds one real witnessing
`(cid, printing)` pair for each, then calls `eval_plane_expr_for_printing` on that SAME fixed pair
directly in a tight best-of-80 loop (200,000 calls/round) -- no candidate walk, no page selection, no
`explain_analyze` overhead, so the number is purely the function's own per-call cost:

```
border alone:            4.12 ns/call        rarity alone:            2.17 ns/call
cmc[1,5] AND border:    30.30 ns/call        cmc[1,5] AND rarity:    33.26 ns/call

width sweep (cmc range AND border:black, fresh witness per width):
  cmc[3,3]   (16 leaves, 15 extra):  22.30 ns/call   delta +18.18 ns   (1.21 ns/extra-leaf)
  cmc[2,4]   (18 leaves, 17 extra):  30.31 ns/call   delta +26.19 ns   (1.54 ns/extra-leaf)
  cmc[1,5]   (20 leaves, 19 extra):  32.70 ns/call   delta +28.58 ns   (1.50 ns/extra-leaf)
  cmc[0,12]  (28 leaves, 27 extra):  33.10 ns/call   delta +28.98 ns   (1.07 ns/extra-leaf)
```

Confirms, directly and mechanistically: a bare existential leaf costs 2-4ns/call; ANDing a card-invariant
range partner costs 7-8x more (30-33ns/call), scaling with the range's own leaf count at roughly
1.0-1.5ns per extra `Plane` leaf (not perfectly linear -- `Or`'s `.any()` short-circuits at the first
`true` child, so the REAL evaluated count for a specific witness depends on where its bit falls, which a
static per-query feature can't know -- but clearly monotonic and the right order of magnitude).

### Measurement 2: a matched-eval_domain paired-diff on the real corpus, independently, agrees

Sampled 77 real queries (`border`/`rarity` bare and ANDed with `cmc`/`power`/`toughness` ranges of width
1, 3, 5, and 13, plus `f:oldschool`) via `explain_analyze` against a freshly-built store (the checked-in
`real.store` predates this round's `PlanFeatures` field and reads header-mismatch; rebuilt via
`costbench.load_engine` against `benchmarks/bitplanes/corpus.jsonl` instead). Found a real, genuine
confound while doing this, unrelated to this round's own mechanism: `eval_domain` reads IDENTICAL across
every range width for several MINORITY existential leaf values (`border:white/gold/borderless`, any bare
`rarity` value paired with a `power`/`toughness` range) -- an already-existing gap in the
`card_invariant_domain_exact`/estimated-domain fallback, not something this round introduced or fixes.

Controlling for it directly: comparing two rows for the SAME base leaf with the IDENTICAL `eval_domain`
isolates the leaf-count effect with zero contribution from that confound. Restricted to `eval_domain >=
4,000` (excluding the smallest populations, where single-query/single-trial-median noise swings the
per-pair rate by 10-60 ns/leaf) leaves 79 matched pairs across `border:black`/`r:common`/`r:uncommon`/
`r:rare`/`r:mythic`/`f:oldschool`. Split by a hash of `"{leaf}|{plan}|{lo_leaves}|{hi_leaves}"`:

```
calibration half (n=42): median rate 2.92 ns/leaf
held-out half    (n=37): median rate 2.64 ns/leaf   -- within 10% of the calibration half
held-out mean abs error on the DELTA prediction: rate=0 baseline 178,670 ns  ->  fitted rate 101,370 ns
                                                  (43% lower)
```

Two independent measurements (a confound-free kernel micro-benchmark and a confound-controlled real-
corpus paired-diff) agree the mechanism is real and land in the same order of magnitude (1.0-1.5 vs
2.6-2.9 ns/leaf -- the real numbers read higher, plausibly because the whole-query walk touches a
DIFFERENT `printing` struct and different bitmap words per distinct candidate card, unlike the
micro-benchmark's artificially-hot repeated-same-leaf loop).

### The fix built, and why it fails the metric that actually matters

Built the fix per the brief's design: `planes.rs::count_plane_leaves` (a new, small, structural tree
walk), `PlanFeatures::plane_extra_eval_leaves` (`count_plane_leaves(plane) - 1`, computed once per query
in the `PrintingCompose`-acquire branch exactly when `cost_plane_nothing_to_verify` says the plane is
existential -- `0` otherwise, so the already-calibrated bare-existential-leaf population is untouched),
and `GATHER_PLANE_LEAF_NS`/wiring into both `GatheredScan` and `StreamedSelect`'s arms (the same
`eval_plane_expr_for_printing` call backs both kernels). Regression tests added and passing (172 existing
+ `compose_prices_compound_plane_leaf_count_above_bare_existential`, asserting the compound reproducer
gets a nonzero charge and both bare-existential-leaf and bare-card-invariant-range controls stay at
exactly `0`). `cargo test --release`: 173/173. `cargo clippy --all-targets -- -D warnings`: clean.

Then ran the MANDATORY held-out check on this doc's own stated primary metric -- held-out
predicted-vs-measured, not the paired-delta above -- and it fails:

```
GatheredScan, eval_domain >= 4,000, plane_extra_eval_leaves > 0, hash-split by query string:
  rate=0.00 (baseline):  held-out median |log(predicted/measured)| = 0.417
  rate=2.80 (the calibrated mechanistic rate): held-out median |log ratio| = 1.145   -- WORSE, not better
```

The flagship reproducer itself shows why: BEFORE this round, `cmc>=1 cmc<=5 border:black`/card predicted
728,028ns against measured ~1.10-1.54M ns (under-charged, ratio ~0.5-0.65x, the gap this doc opened
with). Adding the calibrated 2.8ns/leaf term moves it to 2,043,877ns against the SAME measured range --
now OVER-charged by ~1.5x. The additive fix does not close the gap, it overshoots past it.

Root cause, isolated by computing the "rate `pred0` would need" per absolute row rather than per matched
pair, within the SAME leaf family (`border:black`, widths 15/17/19/27 extra leaves, at their own native
`eval_domain`): the implied rate is **negative** (-1.17, i.e. already OVER-predicted) at 15 extra leaves,
crosses to positive around 17-19, and only reaches +1.2 at 27. The existing `GATHER_CARD_PASS_NS`/
`GATHER_RESIDUAL_FLOOR_NS` floor (18.89ns, fit against a "one bare check" shape per its own doc) is
measurably NOT a clean bare-leaf baseline in practice -- it already reads as generous at low compound-leaf
counts and stingy at high ones, most likely because whatever traffic sample calibrated it in an earlier
round already contained a mix of compound-AND shapes, baking an uneven, population-dependent AVERAGE
leaf-count contribution into one flat constant. Layering a mechanistically-correct marginal rate on top
of that uneven baseline overshoots exactly where the baseline was already over-generous, and only
partly helps where it was under-generous -- a single linear additive term cannot fix a floor that is
itself already leaf-count-dependent in an uncontrolled way. A real fix would need to jointly recalibrate
the floor and the new term together over a much larger, controlled sample -- a bigger blast radius than
this round's scope (Round 15-17's `GATHER_RESIDUAL_FLOOR_NS`/`GATHER_CARD_PASS_NS` are validated,
shipped constants; re-deriving them here risks a regression across the WHOLE existing residual-tier
population, not just the compound-plane slice this round targets).

### Outcome: discarded, reverted

**Negative result, code reverted.** `cost.rs`/`lib.rs`/`planes.rs`/`tests.rs` are back to `costcell/trunk`
(Round 17's state) -- `git diff --stat costcell/trunk` reads empty. `cargo test --release`: 172/172
passed (unchanged). `cargo clippy --all-targets -- -D warnings`: clean (unchanged, no code to lint). No
bench re-runs against a reverted build -- there is nothing to confirm.

What this round DID establish, worth keeping for whoever picks this up next:

- The compound-plane mechanism is real, confirmed by two independent measurements (a confound-free
  kernel micro-benchmark and a confound-controlled real-corpus matched-domain paired-diff), landing in
  the same 1-3 ns/extra-leaf order of magnitude. This is no longer a hypothesis.
- The compiled tree shape for a two-sided numeric range ANDed with an existential leaf is
  `And([Or(≤14), Or(≤14), existential leaf])` -- NOT the narrower `Or(width)` this doc's own earlier
  round paraphrased. Anyone reusing this mechanism description should use THIS round's section above as
  the reference shape (traced directly against `planes.rs::compile_numeric_cmp`/`compile_plane_children`,
  and confirmed by a real node-count over the compiled tree), not the earlier paraphrase.
- A simple additive `rate * eval_domain * plane_extra_eval_leaves` term, layered on top of the existing
  `GATHER_RESIDUAL_FLOOR_NS`/`GATHER_CARD_PASS_NS` floor, does NOT survive held-out validation on
  predicted-vs-measured (the metric routing actually uses) even though the SAME rate is well-supported by
  a held-out check on the marginal/paired-delta metric. The floor itself appears to already absorb an
  uneven, population-dependent share of the compound-leaf effect. A future attempt should jointly refit
  the floor and a leaf-count term together (a proper weighted regression over a much larger real-traffic
  sample of existential-plane `Mode::Card` queries, not an additive patch on the existing constant), or
  investigate whether the floor's ORIGINAL calibration sample already contained enough compound-AND
  queries to explain the unevenness directly.
- A separate, small, already out-of-scope confound was found (not fixed): `eval_domain` reads identical
  across every range width for several minority existential leaf values (rare `border`/`rarity` values
  paired with a `power`/`toughness` range) -- a gap in the `card_invariant_domain_exact`/estimated-domain
  fallback, distinct from (and a further complication on top of) the `filter.rs::touches_printing_field`
  gap Round 17 already flagged. Worth its own line item in a future domain-estimation cleanup pass.

## Round 20: a joint refit was attempted, and blocked by the SAME confound Round 19 flagged in
## passing -- now shown to be structurally inseparable from the population this fix needs, not just a
## small aside

Round 19's own brief for a future attempt was explicit: jointly refit `GATHER_RESIDUAL_FLOOR_NS`,
`GATHER_CARD_PASS_NS` and a new leaf-count rate TOGETHER, against a sample broad enough to cover every
population that shares those two floor constants, rather than anchoring the floor at its old value and
fitting only the new term (Round 19's shape, which passed a marginal/paired-delta check but failed the
metric that actually drives routing). This round built that joint fit. It failed too, for a reason Round
19's own "Outcome" section already named as a loose end but did not chase: **the `eval_domain` confound
it flagged in passing ("reads identical across every range width for several minority existential leaf
values") is not a small aside on the side of this population -- once measured broadly, it dominates the
error for every existential leaf value except the one the flagship reproducer happens to use, and it
turns out to be structurally coupled to the exact acquire branch that produces the leaf-count feature.**
That coupling, not just the floor's own uneven fit, is why a broad sample cannot validate this fix with
today's architecture.

### What's different this time

Rounds 17 and 19 each built a plausible mechanism, fit it, and found the FIT didn't survive a held-out
check on the right metric. This round got as far as building the fit Round 19 asked for (feature
plumbing reintroduced verbatim, a scoped 3-constant joint-fit script, ~260 systematically varied rows
across all three named populations) -- and found the DATA itself is not trustworthy enough to validate
any fit against, for a reason specific to how this feature is computed. That is a different failure mode:
not "the mechanism doesn't hold up" but "the population needed to test the mechanism is dominated by an
orthogonal bug living in the same code path," discovered by trying to build the broad sample the brief
demanded rather than by reasoning about it in the abstract.

### The feature and the fit, rebuilt

Reintroduced Round 19's exact plumbing (its diagnosis was sound; only the fit around it was the problem):
`planes.rs::count_plane_leaves` (a plain node-counting walk over a compiled `PlaneExpr`, mirroring
`plane_expr_is_existential`'s own recursion shape), `PlanFeatures::plane_extra_eval_leaves` (`lib.rs`,
`count_plane_leaves(plane) - 1`, set only in the `PrintingCompose`-acquire branch's tier decision exactly
when `filter_nothing_to_verify && !cost_plane_nothing_to_verify` -- the plane, not a real filter residual,
is the reason `tier != 0`), and both `GatheredScan`/`StreamedSelect` arms in `cost.rs` reading
`GATHER_CARD_PASS_NS + tier_ns.max(GATHER_RESIDUAL_FLOOR_NS) + plane_extra_eval_leaves * GATHER_PLANE_LEAF_NS`
(`STREAM_*` counterparts identical in shape). `cargo test --release`: 173/173 (172 pre-existing + Round
19's own regression test format, re-added). `cargo clippy --all-targets -- -D warnings`: clean.

`scripts/fit_cost_model.py` was read first, per the brief's instruction, and NOT used as-is: it refits
every coefficient in an arm at once (all 7-8 of `GatheredScan`'s), which is the general tool this
session's discipline exists to avoid reaching for on a single-mechanism round -- fitting it here would
have moved `GATHER_LOOP_PER_CARD_NS`, `GATHER_SCAN_PER_ROW_NS`, `GATHER_PUSH_PER_MATCH_NS`, and every
other already-validated rate in the arm as a side effect of trying to fit three constants. Built a
standalone script instead (`fit_round20.py`, this session's scratchpad, not `scripts/`): it holds every
OTHER coefficient in the `GatheredScan`/`StreamedSelect` arms at its CURRENT shipped value, computes
`other_terms = measured - (those coefficients * their features)`, and fits only
`[GATHER_CARD_PASS_NS, GATHER_RESIDUAL_FLOOR_NS, GATHER_PLANE_LEAF_NS]` (and the `STREAM_*` triple)
against what's left, using the identical non-negative log-ratio IRLS `fit_cost_model.py` itself uses
(copied, not imported, so this script's narrower scope can't accidentally widen if `fit_cost_model.py`
changes later).

Sample: 3 numeric fields (`cmc`/`power`/`toughness`) x 7 range widths (0-12, giving `plane_extra_eval_
leaves` from 0 to 48) x 11 existential leaf values (`border:{black,white,borderless,gold,silver}`,
`r:{common,uncommon,rare,mythic,special}`, `f:oldschool`) plus 12 triple-AND rows for population A
(compound); the same 11 bare leaves alone for population B; 26 hand-picked real-residual queries
(`name:`/`artist:`/`flavor:`/`watermark:`/anchored and unanchored `o:` regexes) spanning `MASK_COMPARE`/
`SET_LOOKUP`/`TEXT_SCAN`/`REGEX_MACHINERY` tiers for population C -- 259 rows total, `unique=card,
orderby=rarity direction=desc limit=175 offset=0 prefer=default` (the reproducer's own paging shape, to
avoid contaminating the isolated term with the page/perm-walk terms' own separately-validated noise).
Confirmed first that natural sampling cannot substitute for this hand-built grid: 20,000 `QuerySampler`
draws in `realistic` mode and 40,000 in `uniform` mode produced **zero** rows with `plane_extra_eval_
leaves > 0` -- this population is real (the flagship reproducer is a top-25 real query) but too rare for
either sampler mode to hit in tens of thousands of tries, matching this doc's own Q4 finding.

Split calibration/held-out by `hashlib`-stable hash of the query string (Python's built-in `hash()` is
per-process randomized for strings and was caught giving a DIFFERENT split, and different fitted
coefficients, on two consecutive runs of the identical script before this was noticed and fixed --
recorded here so whoever reuses `fit_round20.py`'s shape does not repeat it).

### The confound: `eval_domain` badly overestimates the true candidate count for every leaf value except the corpus-dominant one, and it is the SAME branch that sets `plane_extra_eval_leaves`

Checking each row's realized `cards_visited` (from `explain_analyze`) against the `eval_domain` feature
`plan_cost` actually multiplies -- the same `counter_check` discipline `fit_cost_model.py` itself insists
on before trusting a fit ("a feature that mis-counts by 2.5x cannot be repaired by any rate, and the fit
will happily bury the error in whichever coefficient correlates with it") -- only **13 of 223** population
A rows land within 15% of `cards_visited`. The other 210 are not scattered noise; they are one-sided and
huge, and they sort cleanly by which existential leaf value is in the query, independent of the numeric
range's width (i.e. independent of leaf count, the very thing this round's term is about):

| leaf (ANDed with `cmc>=1 cmc<=N`, `N` swept 1-13) | `cards_visited / eval_domain`, across widths |
|---|---|
| `border:black` (near-universal -- most cards have a black-border printing) | 0.68 - 1.22 (clean) |
| `border:white` | 0.20 - 0.71 |
| `border:borderless` | 0.17 - 0.85 |
| `border:gold` | 0.07 - 0.69 |
| `r:common` / `r:uncommon` | 0.26 - 0.85 |
| `r:rare` | 0.17 - 0.68 |
| `r:mythic` | 0.02 - 0.55 |
| `f:oldschool` | 0.08 - 0.55 |

`eval_domain` for `cmc>=1 cmc<=5 r:mythic` reads 4,710 -- IDENTICAL to `matches` (also 4,710, the exact
result total) -- while the REAL `GatheredScan` loop only ever visits 1,842 cards (`cards_visited`). The
model believes the candidate list is the full exact match count; the real loop, going through a narrower
candidate set some other mechanism built, visits well under half of that. This is not new -- Round 19's
own "Outcome" section already named it in one sentence ("`eval_domain` reads identical across every range
width for several minority existential leaf values... a gap in the `card_invariant_domain_exact`/
estimated-domain fallback") -- but Round 19 measured it on a handful of rows found while chasing a
different mechanism. Measured broadly and systematically here (all 11 leaf values x 7 widths x 3 fields),
it is not a minority-case aside: **`border:black` is the only leaf value tested where `eval_domain` is
trustworthy**, and `border:black` is exactly the value this doc's own flagship reproducer and every
earlier round's hand-picked verification queries happened to use -- which is why nobody had measured this
gap's true size before this round went looking for a genuinely diverse sample.

**The coupling that blocks a fix, not just a caveat on the sample:** `plane_extra_eval_leaves` is only
ever nonzero on rows that reach the `PrintingCompose`-acquire branch's tier decision -- and that is
*exactly* the same branch whose `domain_cards`/`card_invariant_domain_exact` machinery produces the
broken `eval_domain`. Checked directly: the identical queries under `orderby=name` (which routes through
a completely different acquire path, `Prep::Candidates`, not `PrintingCompose`) show `eval_domain` within
15% of `cards_visited` on 206 of 223 rows -- clean -- but `plane_extra_eval_leaves` reads exactly **0 on
all 259 rows**, because that acquire path never sets it (this round's plumbing, like Round 19's, is
deliberately scoped to the one branch where the mechanism applies). So there is no substitute sample: the
one acquire path that reports leaf count is the one with the broken domain estimate, and the one with a
correct domain estimate never reports leaf count. A joint fit of `GATHER_CARD_PASS_NS`/
`GATHER_RESIDUAL_FLOOR_NS`/`GATHER_PLANE_LEAF_NS` against population A's absolute predicted-vs-measured is
therefore fitting mostly to `eval_domain` noise, not to the per-printing evaluation cost this round is
about -- restricting to the 13 clean rows leaves only `border:black` (plus two other near-universal
values that happen to floor `tier` to 0) and 5 distinct leaf-count values, nowhere near the "varying which
existential leaf" diversity the brief's own step 2 requires.

Population B has an independent version of the same problem, previously flagged by Round 17
(`scan_units`'s uniform-random-position assumption is wrong for a leaf value whose matching printing
clusters by print era): **0 of 10** bare-existential-leaf rows land within 15% on the `scan_units`/
`printings_examined` counter check. Population C (real residual-filter queries -- `name:`/`artist:`/
`flavor:`/`watermark:`/regex, none of which touch a plane) is comparatively clean: 24 of 26 rows pass the
`eval_domain` check.

### The fit itself, run anyway, for the record

Run on the full (unfiltered) sample, since the brief asks for the numbers even where the outcome is
negative:

```
GatheredScan (116 calibration / 143 held-out rows, hash-of-query split):
  CARD_PASS       current 3.00   fitted 2.90
  FLOOR           current 18.89  fitted 14.85
  PLANE_LEAF_NS   current 0.00   fitted 0.92

held-out, by population (median predicted/measured, within-25%):
  A (compound)   before 0.789 (20%)  ->  after 1.141 (18%)   -- flips under- to over-predicted, no gain
  B (bare leaf)  before 1.960 (17%)  ->  after 1.688 (17%)   -- moves toward 1.0 but within-25% unchanged
  C (residual)   before 1.268 (50%)  ->  after 1.140 (57%)   -- the one population that actually improves
  pooled         before 0.874 (22%)  ->  after 1.146 (22%)   -- pooled within-25% unchanged
```

`StreamedSelect` could not be fit at all: it never enters contention under `orderby=rarity`/`usd` for
any of these queries (`explain_analyze` reports only `PrintingCompose`/`GatheredScan`), so the calibration
set had zero rows. It DOES enter contention under `orderby=name` -- but that is exactly the acquire path
where `plane_extra_eval_leaves` is always 0 (see above), so even a `StreamedSelect`-only sample could not
inform `STREAM_PLANE_LEAF_NS`. `STREAM_CARD_PASS_NS`/`STREAM_RESIDUAL_FLOOR_NS` were left untouched
entirely -- not even a same-value no-op refit was attempted, since there was no leaf-count-varying data to
jointly fit them against.

Population A's within-25% agreement does not improve (20% -> 18% held-out) and population B's does not
move (17% -> 17%) -- consistent with the diagnosis above: most of both populations' error is the
`eval_domain`/`scan_units` confounds, not the CARD_PASS/FLOOR/leaf-rate terms this fit can move. Only
population C, which does not touch a plane at all and is not subject to either confound, shows a genuine
improvement (50% -> 57% within-25%) -- but C alone has no leaf-count variation (`plane_extra_eval_leaves`
is 0 for every C row by construction) and so cannot inform `GATHER_PLANE_LEAF_NS` either. No population
in this sample can jointly validate all three constants at once with today's feature set.

### Re-verifying the flagship reproducer

Unchanged from `costcell/trunk` (Round 16's state), since nothing shipped: `cmc>=1 cmc<=5 border:black`,
`unique=card`, `orderby=rarity desc`, `limit=175`, `offset=0` -- `GatheredScan` predicted 728,028ns against
a freshly re-measured 1,161,583ns (single representative trial; the broader range across repeated runs
this session was 1,077,625-1,452,625ns), ratio ~0.5-0.68, same under-prediction this doc has reported
since Round 16. Neither Round 17's depth term, Round 19's additive leaf term, nor this round's jointly-
refit version closes this gap for a reason that generalizes across all three attempts: `border:black` is
the one leaf value where `eval_domain` is NOT the dominant source of error, so the reproducer's own
remaining gap really is the per-printing-evaluation-cost mechanism Rounds 17/19/20 all correctly
identified -- but fitting a rate against a BROADER sample (as this round's brief required, precisely to
avoid overfitting to this one reproducer) immediately runs into the `eval_domain` confound on every OTHER
leaf value, which a fit cannot tell apart from the mechanism it's trying to measure.

### Outcome: discarded, reverted

**Negative result, code reverted.** `cost.rs`/`lib.rs`/`planes.rs`/`tests.rs` are back to `costcell/trunk`
(Round 16's state) -- `git diff --stat costcell/trunk` reads empty. `cargo test --release`: 173/173 passed
(unchanged). `cargo clippy --all-targets -- -D warnings`: clean (unchanged, no code to lint). No bench
re-runs against a reverted build -- there is nothing to confirm.

What this round DID establish, worth keeping for whoever picks this up next:

- Round 19's own hypothesis (jointly refit the floor and the leaf rate, rather than anchoring the floor)
  was the right next experiment to run, and it still doesn't ship -- but not for the reason Round 19
  anticipated ("the floor already unevenly absorbs part of the compound-leaf effect"). The blocking
  problem is upstream of the floor entirely: `eval_domain`, the feature every candidate-count term in
  `GatheredScan`'s/`StreamedSelect`'s arms multiplies by, is itself wrong by up to 14x (0.02-1.22 measured
  across the sample) for the `PrintingCompose`-acquire branch's arith-tuple-range-AND-existential-leaf
  shape, for every leaf value except the corpus-dominant one this doc's own reproducer happens to use.
- That confound is not merely correlated with this round's population by coincidence -- it is produced by
  the SAME acquire branch that computes `plane_extra_eval_leaves`, and the one alternative acquire path
  that has a trustworthy `eval_domain` (`orderby=name`, `Prep::Candidates`) never computes leaf count at
  all. No sample built from the current architecture can jointly offer both a clean `eval_domain` and
  leaf-count variation, which is a stronger and more specific claim than Round 19's one-line flag.
- **The real next step is fixing `eval_domain`/`domain_cards` for this shape FIRST** -- an arith-tuple
  numeric range ANDed with a non-card-invariant existential leaf under `Mode::Card` -- in whichever of
  `est.result.card`/`arith_tuple_count`/`compose_printing_estimate`/`card_invariant_domain_exact` actually
  computes it (not traced to a single line here; out of this round's `cost.rs`/`lib.rs`-tier-decision-only
  blast radius, and a large enough independent question -- domain estimation, not per-candidate cost model
  rates -- to deserve its own doc rather than a fourth attempt bolted onto this one). Once `eval_domain`
  is trustworthy across leaf values, a joint refit of `GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS`/a
  leaf-count rate becomes testable for real, using the same sample construction and fitting script this
  round built (`fit_round20.py`, this session's scratchpad -- not checked in, but the design/queries are
  fully specified above for whoever rebuilds it).
- Population B's `scan_units` confound (Round 17, era-correlated print position) is confirmed independently
  here at a larger scale (0 of 10 bare-leaf queries pass a 15% counter check) -- still not fixed, still
  flagged as needing a per-(field, value) store-build-time statistic rather than a per-query estimate.
- Population C (real residual-filter queries, no plane involved) is the one population where the existing
  floor is reasonably close (held-out within-25% 50%, before any refit) and where refitting helps (57%
  after) -- consistent with this being closer to the population the floor's ORIGINAL calibration (see
  `GATHER_RESIDUAL_FLOOR_NS`'s own doc, `MASK_COMPARE`/`SET_LOOKUP`/`TEXT_SCAN` tiers) actually targeted.

## Round 25: the Blocker Is Cleared, the Refit Was Rebuilt and Retested on Clean Data -- a Third,
## More Precise Negative Result, Discarded

Rounds 22/24 (`docs/issues/local-engine-domain-cards-existential-arith-and.md`) fixed the `eval_domain`
confound Round 20 found blocking this refit. This round rebuilt Round 19/20's plumbing verbatim
(`planes.rs::count_plane_leaves`, `PlanFeatures::plane_extra_eval_leaves`, additive
`GATHER_PLANE_LEAF_NS`/`STREAM_PLANE_LEAF_NS` terms in `cost.rs`), re-ran the joint fit against fresh
data measured on the now-fixed engine, and the fit still does not survive held-out validation --
**demonstrated for the first time on genuinely clean `eval_domain` data**, closing the open question
both Round 20 and Round 22 left explicit ("clearing the `eval_domain` blocker does not by itself imply
the rate refit will now succeed"). It does not.

### Step 1: plumbing rebuilt, verified as a pure no-op

`count_plane_leaves` (`planes.rs`) is a plain node-counting walk over the compiled `PlaneExpr` tree
(`Plane`/`Bits` leaves count 1, `Const` counts 0, mirroring `plane_expr_is_existential`'s own
recursion). `PlanFeatures::plane_extra_eval_leaves` (`count_plane_leaves(plane) - 1`) is set in
`acquire_plan_features`'s `PrintingCompose`-acquire tier decision exactly when the PLANE half (not the
filter) is the reason `tier != 0` -- the same condition Round 19/20 used. Both `GatheredScan`'s and
`StreamedSelect`'s arms in `cost.rs` gained `+ plane_extra_eval_leaves * GATHER_PLANE_LEAF_NS` (resp.
`STREAM_PLANE_LEAF_NS`) inside the existing `tier_ns > 0.0` bracket, both new constants shipped at
`0.0` (a pure no-op) pending the fit. A new regression test,
`compose_prices_compound_plane_leaf_count_above_bare_existential`, asserts the feature is nonzero for
the compound reproducer, equals `count_plane_leaves(plane) - 1` exactly, and stays `0` for both a bare
existential leaf alone and a bare card-invariant range alone. `cargo test --release`: 177/177 (176
pre-existing + 1 new). `cargo test` (debug): 178/178. `cargo clippy --all-targets -- -D warnings`:
clean. Verified the compiled-shape claim directly too: `cmc>=1 cmc<=5 border:black` reads
`plane_extra_eval_leaves == 19` (20 total `Plane` leaves, matching Round 19's own node-count exactly).

**Also fixed a real gap the rebuild surfaced**: `acquire_facts_to_pydict` (`lib.rs`, the Python-visible
feature dump `explain()` returns) never exposed `plane_extra_eval_leaves` at all -- Round 19/20 must
have added this in their own now-reverted work, since `round20_samples.jsonl` (a leftover artifact in
the scratchpad) already carried the key, but the field is genuinely absent from `costcell/trunk` today.
Added one line exposing it, alongside the other `PlanFeatures` fields already dumped there.

### Step 2: `eval_domain` re-verified clean for population A, `scan_units` confirmed a SEPARATE, still-real confound for population B

Measured fresh rather than trusting Round 22/24's own numbers unchecked (243 population-A rows, 3
arith fields x 9 widths x 9 "clean" leaf values, `explain_analyze` against a freshly-built store):

```
population A:  cards_visited / eval_domain           within 15% = 98.4%   median = 1.000
population B:  printings_examined / scan_units       within 15% =  0.0%   median = 3.950
population C:  printings_examined / scan_units       within 15% = 29.6%   median = 0.766
```

`eval_domain` is confirmed clean for population A -- Rounds 22/24's fix holds broadly, not just on
their own 429-row sweep. **`scan_units` (a DIFFERENT feature, the printing-SPAN estimate
`GATHER_SCAN_PER_ROW_NS` multiplies, computed by `scan_all`/`card_invariant_domain_exact` -- untouched
by Rounds 15-24's `And`-arm work) is a real, severe, SEPARATE confound**, confirmed at a fresh, larger
sample: population B (bare existential leaf, no arith partner) under-predicts real printings scanned by
a median ~4x, exactly the era-correlated-print-position mechanism Round 17 first flagged and Round 20
measured on a 10-row sample. Population C shows a milder version of the same gap (median 0.766).

**Accounted for explicitly rather than left to pollute the fit**: the refit script
(`fit_round25.py`, this session's scratchpad) computes each calibration row's fit TARGET using the
REALIZED `printings_examined` counter in place of the `scan_units` feature -- the same counter-check
substitution `fit_cost_model.py` itself uses, so `scan_units`'s own (unrelated, unfixed) error cannot
be silently absorbed by the CARD_PASS/FLOOR/LEAF coefficients this round is fitting. HELD-OUT
agreement, reported separately below, still uses the REAL deployed formula (raw `scan_units`, exactly
what `cost.rs::plan_cost` computes) -- the substitution is for fitting honestly, not for reporting an
artificially rosier accuracy number. No population was excluded from calibration on this basis; the
substitution made exclusion unnecessary.

### Step 3-5: the joint fit, run against clean data, per-population held-out validation

Sample: 297 rows total (population A 243, B 27, C 27 -- same 9 "clean" leaf values Round 22/24
validated, same `unique=card orderby=rarity direction=desc limit=175 offset=0 prefer=default` paging
shape as the flagship reproducer, population B varied over 3 page sizes instead for hash-split
diversity, population C the same real name:/artist:/flavor:/watermark:/o: shape as Round 20's).
Calibration/held-out split by a STABLE hash of the query string (`hashlib.sha256`, not Python's
built-in `hash()` -- Round 20's own script used the un-fixed builtin despite flagging the exact bug
in its own comment; this round's script fixes it for real, confirmed by re-running the fit twice and
getting IDENTICAL splits and coefficients both times, unlike the builtin-hash version, which gave
visibly different fitted coefficients across two runs before the fix).

`StreamedSelect`: **0 distinct design rows from 0 calibration rows** -- confirms Round 20's own
finding again: `StreamedSelect` never enters contention on this acquire's own paging shape, so
`STREAM_PLANE_LEAF_NS` cannot be informed by this sample at all, same as before.

`GatheredScan`, fitted coefficients (IRLS on the log ratio, ridge-anchored to shipped values, same
method as Round 20's script):

```
CARD_PASS       current=3.000   fitted=2.708
FLOOR           current=18.890  fitted=7.310
PLANE_LEAF_NS   current=0.000   fitted=4.346   (order of magnitude agrees with Round 19's own two
                                                 independent measurements: 1.0-1.5 ns/leaf kernel
                                                 micro-benchmark, 2.6-2.9 ns/leaf real-corpus paired-diff)
```

HELD-OUT agreement (real deployed formula, raw `scan_units`), per population:

```
             median pred/meas   within-25%   median |log ratio|
A  before         0.333            17%             1.099
A  after           0.983            15%             0.838
B  before         1.745            17%             0.606
B  after           1.081             6%             0.626
C  before         1.464             9%             0.395
C  after          1.037            36%             0.391
pooled before     0.600            16%             0.824
pooled after      1.006            15%             0.736
```

The MEDIAN and log-ratio numbers read like a real improvement across the board -- median moves to
within 2% of 1.0 for both A and pooled. **This is misleading on its own**, and checking why is what
makes this round's negative result more precise than Round 19's:

### Why it still fails: the fit improves small-`eval_domain` leaves and badly overshoots large ones, and the flagship reproducer is exactly the latter

Breaking population A's held-out rows down by which existential leaf value they use (the same
breakdown Round 19 used to first spot this shape):

```
leaf                improved / total (held-out, GatheredScan, by |log ratio| before vs after)
border:black          0 / 12
r:common              0 / 14
r:rare                0 / 17
r:uncommon            0 / 13
border:borderless    15 / 17
border:gold          13 / 15
border:white          7 / 10
f:oldschool          11 / 12
r:mythic             12 / 13
```

A clean split: the four leaves with the LARGEST bare selectivity in this corpus (`border:black` 98.9%,
`r:common`/`r:uncommon`/`r:rare` 32-35%) get WORSE on every single held-out row: their `eval_domain` is
tens of thousands, and the fitted `PLANE_LEAF_NS` rate, multiplied by both `eval_domain` and the
range's own leaf count, overshoots the true cost by 2-3x for exactly these rows. The five
minority-selectivity leaves (a few hundred to a few thousand `eval_domain`) improve substantially,
because the SAME flat rate is proportionally much smaller against their much smaller `eval_domain`.
Population A's TOTAL absolute ns error is **2.67x WORSE** after the fit (12,676,427ns -> 33,865,296ns
summed over 123 held-out rows), dominated by the large-`eval_domain` leaves' now-larger absolute
errors outweighing the small-`eval_domain` leaves' improvement.

**The flagship reproducer is exactly the worst-case leaf** (`border:black`, the corpus's single most
selective existential value): re-measured directly (`eval_domain=24,734`, `plane_extra_eval_leaves=19`,
matching Round 19's own node-count exactly for this query's compiled shape):

```
cmc>=1 cmc<=5 border:black, unique=card, orderby=rarity desc, limit=175, offset=0:
  measured range:        1,054,584 - 1,096,417 ns
  before (shipped):        728,547 ns   (under by ~1.4-1.5x -- the gap this whole doc opened with)
  after (fitted):        2,477,459 ns   (OVER by ~2.3x -- the fit makes the flagship query's own
                                          prediction WORSE, not better)
```

This is the same shape of failure Round 19 found ("the additive fix does not close the gap, it
overshoots past it"), but the mechanism is now precise where Round 19's was not: Round 19 attributed
it to "the floor already unevenly absorbs part of the compound-leaf effect" from an unknown-shaped
population-dependent average baked into `GATHER_RESIDUAL_FLOOR_NS`. This round's clean data shows the
unevenness correlates cleanly and monotonically with `eval_domain`'s own SIZE (a 50-100x range across
this population's leaf values) -- not a vague "population mix" but a specific, checkable quantity. A
flat `ns/leaf` rate multiplied by `eval_domain` is structurally the wrong shape for a population whose
`eval_domain` spans two orders of magnitude: it can be right at one scale or the other, never both.

Population B (bare leaf, `scan_units` confound already accounted for via the counter-check
substitution) shows a smaller but real absolute improvement (abs error ratio 0.480, roughly halved) --
consistent with `plane_extra_eval_leaves` being `0` for every population-B row by construction (a
single leaf), so `PLANE_LEAF_NS` contributes nothing there and the improvement is coming entirely from
`FLOOR` moving down (18.89 -> 7.31), which happens to fit population B's own residual-tier cost better.
Population C shows abs error ratio 1.167 (essentially flat, mildly worse) -- smaller sample (11
held-out rows) and no `plane_extra_eval_leaves` signal either, so this population mostly just absorbs
whatever `FLOOR`/`CARD_PASS` move does, which is a wash here.

### Why `GATHER_CARD_PASS_NS`/`GATHER_RESIDUAL_FLOOR_NS` themselves cannot be safely moved by this fit alone, either

Even setting `PLANE_LEAF_NS` aside, the fit also moves `FLOOR` from 18.89 to 7.31 and `CARD_PASS` from
3.00 to 2.71 -- outside this round's sample's own information: these two constants are shared across
the WHOLE residual-tier population (every `GatheredScan`/`StreamedSelect` query with a nonzero verify
tier, not just existential-plane queries), and Round 19 already flagged moving them from a narrow
3-population sample as a risk to the broader population that originally calibrated them. This round's
sample cannot see that broader population at all, so shipping the fitted `FLOOR`/`CARD_PASS` values
alongside a `0.0` `PLANE_LEAF_NS` (i.e., taking only "half" the fit) was considered and rejected for
the same reason -- it is not a smaller, safer version of this round's finding, it is an untested
change to constants this round has no evidence about outside this narrow slice.

### Outcome: discarded, reverted

**Negative result, code reverted.** `cost.rs`/`lib.rs`/`planes.rs`/`tests.rs` are back to
`costcell/trunk` -- `git diff --stat costcell/trunk` reads empty. `cargo test --release`: 176/176
passed (unchanged). `cargo test` (debug): 177/177 (unchanged). `cargo clippy --all-targets -- -D
warnings`: clean (unchanged, no code to lint). No bench re-runs against a reverted build -- there is
nothing to confirm; the plumbing was validated as a pure no-op (176/177 before the rebuild, 177/178
with it, identical predicted_ns everywhere with the new constants at `0.0`) before this round decided
not to ship the nonzero rate.

What this round DID establish, worth keeping for whoever picks this up next:

- **The `eval_domain` blocker Rounds 22/24 fixed is real and confirmed cleared** (98.4% within 15% on a
  fresh, independent 243-row check) -- clearing it was necessary but, as Round 22 itself already
  hedged, not sufficient. This round is the first to actually test that gap and confirm the refit still
  fails on clean data, closing the open question.
- **A NEW, more precise characterization of why a flat additive leaf-rate term fails**: it is not (only)
  "the floor absorbs an uneven population mix" (Round 19's framing) -- it is that `eval_domain` itself
  spans 50-100x across this population's leaf values, and a rate multiplied by `eval_domain` cannot be
  right at both ends. The four highest-selectivity leaves (`border:black`, `r:common/uncommon/rare` --
  not coincidentally, the corpus's most COMMON real-traffic shapes) get WORSE on 100% of held-out rows;
  the five lower-selectivity leaves improve substantially. The flagship reproducer uses the single
  worst leaf for this mechanism (`border:black`), which is why its own prediction moves from
  under-charged (0.65x) to badly over-charged (2.3x) rather than landing closer to 1.0.
- **A genuinely NEW confound was found and handled cleanly**: `scan_units` (not `eval_domain`) is a
  real, severe, still-unfixed error source for the bare-existential-leaf population specifically (median
  4x under-prediction) -- confirmed at a fresh, larger sample than Round 17/20's. This round's fit
  script accounted for it via a counter-check substitution (fit against realized `printings_examined`,
  report held-out against the real deployed `scan_units` feature) rather than letting it silently
  corrupt the CARD_PASS/FLOOR/LEAF fit the way `eval_domain`'s confound corrupted Round 20's attempt --
  a concrete methodological answer to this round's own brief, reusable by whoever revisits this next.
- **A real next step, concretely scoped**: a SATURATING or bucketed leaf-rate term (e.g., a rate that
  caps its total per-candidate contribution, or a small number of `eval_domain`-selectivity BANDS each
  with their own calibrated rate) rather than a flat linear one, would not have this specific failure
  mode -- large-`eval_domain` leaves would stop scaling the per-leaf charge past whatever cap or band
  applies to them. Not attempted here (a materially different, larger mechanism than "rebuild Round
  19/20's plumbing and refit," and this round's blast radius/brief scoped the attempt to that
  plumbing specifically) -- flagged as the concrete next step for whoever picks this arc up again.
- `scripts/fit_cost_model.py` was read again as reference only, not used, for the same reason Round 20
  gave (it refits every coefficient in an arm at once, too wide a blast radius for a 3-constant scoped
  fit). `fit_round25.py` (this session's scratchpad, not checked in) rebuilds Round 20's design with the
  `hashlib` split fix and the `scan_units` counter-check substitution described above.
