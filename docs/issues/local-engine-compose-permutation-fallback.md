# Engine: `PrintingCompose` Permutation-Free Paging Fallback

**Status: done.** No GitHub issue filed. Found while investigating a broad-survey slow query
(`docs/issues/done/local-engine-watermark-postings.md`'s companion investigation).

## Measured problem

`SortCol::Rarity`/`PriceUsd` have no card-space sort permutation (`lib.rs:1762-1774`, doc: "the
sort key depends on the prefer-chosen printing and cannot be precomputed"). That's a real
correctness constraint, not an oversight — but it made `PrintingCompose`, `CardRangePopcount`, and
`StreamedSelect` all decline outright whenever `orderby=rarity` or `orderby=usd`, regardless of how
well the predicate narrows, because their applicability checks required a permutation for
*paging*, even for plans whose *total* doesn't depend on one at all.

`printing_compose_fastpath` (`lib.rs:4565`) already computed the exact composed bitmap and total
unconditionally, before any permutation check — then discarded both and declined if no permutation
existed. Border/rarity/legality bare queries don't have this problem (`split_planes` extracts them
into `plane: Option<&PlaneExpr>` upstream of plan selection, and `prepare_candidates` has its own
permutation-independent handling for that), but range leaves (`usd`/`cn`/`date`) only ever reached
exactness through the permutation-gated compose plan.

## What shipped

1. **`gather_composed_page`** (new function, mirrors `walk_grouped_page`'s per-card/per-artwork
   grouping logic exactly, minus the permutation): projects the composed `pbits` to the mode's
   candidate ids (`bitmap_card_ids`), then pushes matches into `GatherSelect` — the same bounded,
   pruned accumulator `GatheredScan` already uses for its own permutation-less case. No residual, no
   `card_pass` — `pbits` is already exact.
2. **A `Prefer::Default` fast path inside it**: printings are stored prefer-desc within a card, so
   the first *set* printing is already the chosen one — no score to compute, no group bookkeeping.
   This mattered here specifically (unlike `walk_grouped_page`, which is bounded by page size via
   early-stop) because this loop visits every candidate regardless of `limit`/`offset`.
3. **`printing_compose_applicable`** no longer requires a permutation (dropped `sort_col`/
   `descending` params to match `printing_range_scan_applicable`'s tailored-signature precedent).
4. **A dedicated broadness guard, `COMPOSE_GATHER_MAX_CARD_FRACTION`** (default 0.85): building
   `pbits` only pays for itself when narrowing actually shrinks the candidate set
   `gather_composed_page` visits. For a near-total match (e.g. `usd<50`/card at 99% of all cards),
   the build is pure overhead with no compensating benefit — measured regressing 0.41ms → 0.49ms
   before the guard existed. This is a *different* crossover from `MAX_NARROW_FRACTION` (a different
   question — "is the build worth it without a cheap post-build walk" vs. "does narrowing shrink
   the eval domain at all") and needed its own measurement, not reuse of the 0.25 constant.
5. **`compose_has_perm`** (new `PlanFeatures` field): tells `cost::plan_cost`'s `PrintingCompose`
   arm which paging strategy will actually run, since the two have different cost shapes (offset-
   dependent walk vs. visit-every-match gather).

## The estimation trap (two dead ends before landing this)

The guard needs a cheap *mode-space* estimate (candidate cards/artworks, not raw matching
printings) — `compose_printing_estimate` only gives a printing-space count, and:

- **Checking the raw printing-space fraction directly** was wrong: `cn<100` is 36% of *printings*
  (several low-collector-number printings per reprinted card) but the number that actually matters
  is much lower. This wrongly declined a query that's genuinely worth composing.
- **Projecting with a naive `.min(domain)` cap** was *also* wrong, in a way that took a second
  round to find: both `cn<100` (35,021 matching printings) and `usd<50` (80,527) exceed `n_cards`
  (31,508), so both saturate to the *identical* capped value. The cost model's only differentiating
  signal collapsed for the two cases that most needed distinguishing.
- **What actually works**: a balls-into-bins estimate — `k` matching printings landing across
  `domain` cards, expected distinct cards touched ≈ `domain·(1 − e^(−k/domain))`. One `exp()` call,
  doesn't saturate, and tracks the true count well enough to separate the cases that need
  separating (checked against real totals: `cn<100` estimate 21,140 vs. true 17,616; `usd<50`
  estimate 29,062 vs. true 31,217 — clearly distinguishable, unlike the identical capped estimate).

For `Mode::Artwork`, the true domain is `n_artworks` (≥ `n_cards`), but computing it exactly means
building `artwork_base` — real O(n_cards) work paid just to *maybe* decline. `cards.len()` is used
as a cheap, conservative stand-in instead (only ever makes the estimated fraction look more broad
than reality, erring toward declining — same conservative lean the threshold itself was calibrated
with).

## Measured (targeted, `scripts/bench_compose_permutation_fallback.py`, 97,206-printing corpus)

| query | unique | orderby | before (ms) | after (ms) | change |
|---|---|---|---:|---:|---|
| `usd<50` | card | rarity | 0.413 | 0.408 | flat |
| `usd<50` | card | usd | 0.420 | 0.411 | flat |
| `usd<50` | artwork | rarity | 0.757 | 0.752 | flat |
| `cn<100` | card | rarity | 0.643-0.708 | 0.298 | **2.2-2.4×** |
| `cn<100` | artwork | rarity | 0.857 | 0.465 | **1.84×** |
| `year>2020` | card | rarity | 0.520 | 0.339 | **1.53×** |
| `year>2020` | artwork | rarity | 0.741 | 0.557 | **1.33×** |
| `cn<100 usd<50` | printing | rarity | 1.154 | 0.412 | **2.8×** |
| `cn<100 usd<50` | card | rarity | 0.825 | 0.320 | **2.6×** |
| `border:black` (control) | card | rarity | 0.358 | 0.345 | flat |
| `t:creature` (control) | card | edhrec | 0.063 | 0.062 | flat |

Every `edhrec`-orderby control and every plane-only control (`border:black`, `r:rare`, `f:modern`)
held flat. `total` parity held across every row, every run. Full CSVs under
`benchmarks/compose-permutation-fallback/` (untracked, per the performance-PR workflow).

Broad realistic-traffic survey (`scripts/survey_queries.py`, same seed, 1000 queries): no new
regressions; `rarity`/`usd`-orderby groups' p90 improved, everything else flat within noise.

## Testing

- New/widened Rust coverage: `fuzz_row_identity_matches_reference`'s `SORTS` sweep extended to
  include `rarity` (previously only `usd` exercised the no-permutation path at all) — this is a
  differential suite against a reference oracle, across random filter structures, all three
  distinct-ons, both directions. `cargo test` (debug + release): 128/128 passed both times across
  every iteration of this change.
- `pytest api/tests/test_engine_property.py api/tests/test_engine_unit.py`: 158/158 passed.

## Related

- [done/local-engine-watermark-postings.md](done/local-engine-watermark-postings.md) — the sibling
  investigation this was found alongside.
- [done/00724-engine-printing-existential-planes.md](done/00724-engine-printing-existential-planes.md)
  — `PrintingCompose`'s substrate.
- [00731-engine-compose-universal-evaluator.md](00731-engine-compose-universal-evaluator.md) — the
  compose leaf-source generalization this builds on top of.
- `docs/workflows/performance-pr-workflow.md` — the measure-first process this followed.
