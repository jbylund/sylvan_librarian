# PrintingCompose permutation-free paging fallback

`orderby=rarity`/`usd` have no card-space sort permutation (the representative printing depends on
`prefer` and can't be precomputed), which made `PrintingCompose` decline outright for those orderbys
regardless of how well a query's predicate narrowed — even though it already computes the exact
composed bitmap and total *before* the permutation check, then threw both away.

Added `gather_composed_page`: a permutation-free paging strategy reusing the same `GatherSelect`
bounded accumulator `GatheredScan` already uses for its own permutation-less case, fed from the
already-exact composed bits (no residual re-verification needed). Also fixed a real inefficiency
copied from the permutation-based walk (`walk_grouped_page`): it always computed `prefer_score` for
every matching printing, even under the common `Prefer::Default` case, where the first match is
already the answer — fine when bounded by page size, not fine when visiting every candidate.

Building the composed bitmap only pays for itself when narrowing actually shrinks the candidate set;
for a near-total match it's pure overhead. Added `COMPOSE_GATHER_MAX_CARD_FRACTION` (0.85, measured
directly, a different crossover from the existing `MAX_NARROW_FRACTION`) to decline in that case —
using a balls-into-bins estimate (`domain·(1 − e^(−k/domain))`) rather than a naive `.min(domain)`
cap, since the cap saturates identically for very different queries (checked: `cn<100` and `usd<50`
both exceed `n_cards` and capped to the same value, losing the signal needed to tell them apart).

Measured (97,206-printing corpus): `cn<100`/card/rarity 0.643-0.708ms → 0.298ms (2.2-2.4×),
`cn<100 usd<50`/printing/rarity 1.154ms → 0.412ms (2.8×), `year>2020`/card/rarity 0.520ms → 0.339ms
(1.53×). Near-total-broad bare ranges (`usd<50` at 99% of cards) correctly hold flat via the guard.
Every `edhrec`-orderby and plane-only control held flat; `total` parity held everywhere.

`fuzz_row_identity_matches_reference`'s sort sweep extended to include `rarity` (previously only
`usd` exercised the no-permutation path). `cargo test` (debug + release) 128/128;
`test_engine_property.py` + `test_engine_unit.py` 158/158.

Design doc: `docs/issues/done/00740-engine-compose-permutation-fallback.md`.
