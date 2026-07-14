# Engine: bitmap match phase + per-orderby permutation streaming

Status: written up 2026-07-07 (design discussion following PR #618), not started. GitHub: #619.

## Problem

For broad queries, the *emission* machinery — computing sort keys per match,
accumulating the `best` vector, quickselect top-k, and the prefer walk over
every matching card's printings — rivals or exceeds the predicate scan it
follows. Measured by timing each query against a variant with an impossible
unindexable conjunct appended (`power+toughness>99` never narrows, so the scan
shape is preserved while emission goes to ~zero), 97,206-printing live corpus:

| config | full | eval-only | emission share |
|---|---|---|---|
| `t:creature`, card, default | 0.253 ms | 0.182 ms | 28% |
| `rarity>=common`, card, default | 0.509 ms | 0.521 ms | ~0% |
| `t:creature`, card, usd_high | 0.355 ms | 0.187 ms | 47% |
| `rarity>=common`, card, usd_high | 0.999 ms | 0.529 ms | 47% |
| `t:creature`, artwork, default | 0.396 ms | 0.185 ms | 53% |
| `rarity>=common`, artwork, default | 1.412 ms | 0.531 ms | 62% |
| `rarity>=common`, artwork, usd_high | 1.521 ms | 0.533 ms | 65% |

This is the survey's non-default-prefer (14 of slowest 60) and artwork-mode
(10 of slowest 60) tail. Emission work is wasted for every match outside the
returned page: with `limit=100`, a 31k-match query computes 31k sort keys and
prefer-scores 31k×printings to discard all but 100.

## Design

Split match from order; never let emission touch non-page matches.

1. **Permutations (build time).** For each *card-level* sort column —
   edhrec, cubecobra, cmc, power, toughness — store a `Vec<u32>` of card ids
   sorted by (key, id). ~126 kB each, ~630 kB total. Descending order is the
   same array walked backwards. (key, id) ordering makes ties deterministic
   and pagination stable. `orderby=rarity`/`usd` are excluded: their sort key
   is the *chosen printing's* value, which depends on prefer, so no card
   permutation can be precomputed — they keep the current path.
2. **Match phase (sequential, unchanged eval).** Evaluate the predicate over
   cards/candidates exactly as today, writing hits into a bitmap: 31.5k bits
   = 4 kB, cache-resident, reusable buffer. `total` = popcount — exact, free,
   no trailing count-walk. Narrowing composes untouched (candidate lists are
   card-id-sorted; only candidates get evaluated, non-candidates stay 0).
3. **Order phase.** Walk the orderby's permutation testing bits: skip until
   `page_offset` set bits pass, emit the next `limit`. Only emitted cards are
   touched: no sort keys, no `best` vector, no quickselect, and the
   prefer-ordered printing walk runs on ~100 cards instead of every match —
   non-default prefers collapse to default-prefer cost outside the page.
   Deep pagination worst case ≈ 31.5k bit tests over 4 kB — microseconds.
4. **Planner by popcount.** The exact match count is known before choosing
   the emission strategy: below a threshold, keep today's gather +
   quickselect (already microseconds on small match sets); above it, stream
   the permutation. One measured constant, same philosophy as
   MAX_NARROW_FRACTION / MAX_UNION_FRACTION (PR #609/#618).
5. **`unique=printing` / `artwork` totals.** Need more than card existence:
   per-card matching-printing counts (printing space) or distinct
   illustrations with a matching printing (artwork). Both fit in the
   sequential pass — each card's printings are a contiguous range, median 2.
   Emission per page card is unchanged from today's per-card logic.

Notably *not* required: physical reordering of the store. No card
renumbering, no index rebuild changes, no tie-semantics migration — five
arrays plus a new select path. Clustering the physical card order by edhrec
(sequential touch pattern for the default orderby's page emission) remains an
optional follow-up micro-optimization.

## Expected impact

Eliminates the emission share for any card-keyed orderby, all prefers, all
uniques: ~1.9–2.9× on broad × (non-default prefer | artwork) configs (e.g.
`rarity>=common` artwork usd_high 1.52 → ~0.55 ms), ~1.3× on broad default
card queries, no change to selective queries (planner keeps them on the
current path). Exact totals and exact pagination are preserved throughout —
this is not an approximate-counts design.

## Tasks

- [ ] Build-time permutations for the five card-level sort columns in
      CardIndexes (or CardData); (key, id) sort, nulls-last matching today's
      sort_key_bits semantics
- [ ] Reusable card bitmap in the query driver; fill during the existing
      match pass; popcount totals (+ per-card printing/artwork counts for the
      non-card uniques)
- [ ] Permutation-walk emission path (skip/take, reverse walk for desc)
- [ ] Popcount planner threshold; measure the crossover vs gather+quickselect
      (sweep match count, same deliberately-broken-query trick as the PR #609
      crossover measurement)
- [ ] Re-run the emission probe and the 452-config survey; acceptance:
      artwork-mode and non-default-prefer configs drop out of the slowest 60
- [ ] Traffic check: pull the query log for orderby/prefer/unique frequency
      to confirm coverage (5/7 orderbys stream; rarity/usd orderbys keep
      today's path)

## Related

- [00620-engine-flavor-text-narrowing.md](00620-engine-flavor-text-narrowing.md) — the
  other big tail item; independent, composes (its narrowing feeds this match
  phase)
- [done/00603-engine-card-printing-split.md](done/00603-engine-card-printing-split.md) —
  candidate spaces, contiguous printing ranges, prefer walk this reuses
- PR #609 / #618 — measured-constant-instead-of-cost-model precedent for the
  popcount planner threshold; the ~2× random-access penalty measured there is
  why the match phase stays sequential (bitmap) rather than evaluating in
  permutation order
