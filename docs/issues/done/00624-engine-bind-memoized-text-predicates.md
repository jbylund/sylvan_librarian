# Engine: memoize indexable text predicates at bind in unnarrowable queries

Status: implemented in PR #635 (2026-07-08), awaiting review/merge. Filed
2026-07-07 from the post-#622 slow-tail review. GitHub: #624. Priority was
raised from low after the 400-query survey showed Or shapes as the #1 tail
cluster (or2 median 0.996 ms). Results: targeted geomean 1.49× (1.66× over
accepted rewrites), `oracle:deathtouch or usd<5` 1.67 → 0.87 ms, controls
1.00×. Trigger generalized beyond the original design: fires when candidates
are None OR cover more than half the corpus (broad plane bitmaps included).
Broad needles decline free via trigram_min_posting (shortest-posting bound).

## Problem

`o:draw or cn:100` (1.96 ms) tops the post-#622 survey: an unindexable Or
child voids narrowing for the whole node, so the oracle-text contains runs
against all 31.5k cards despite its trigram index bounding it to ~4.1k.

## Idea

When `narrow_candidates(root)` returns None (the query will full-scan),
rewrite indexable text predicates into resolved match sets at bind — the
third instance of the ArtistMatch (#605) / FlavorMatch (#622) pattern:

1. Gather trigram candidates, verify with real contains once at bind
   (~0.1–0.2 ms — work eval would have done anyway).
2. Rewrite to `OracleMatch { card_ids }` (exact, sorted); eval is binary
   search with SQL NULL for text-less cards, exactly like FlavorMatch.
3. Trigger is a single pre-pass; narrowable queries are left untouched
   (the driver only evaluates candidates — memoizing buys nothing there).

Verifying at bind, rather than testing candidate membership at eval, avoids
threading card indices through the eval signature stack — the complexity
this formulation dodges.

## Expected / sequencing

`o:draw or cn:100`: 1.96 → ~0.8–0.9 ms. A `cn:` range index (price-index
shape) takes this specific query to ~0.1 ms and should land first; memoization
uniquely pays for Or-combos with *genuinely unindexable* siblings —
`o:draw or frame:showcase`, arithmetic/devotion children — measured 0.8–1.4 ms,
which no index can rescue. No archive format change.

## Tasks

- [ ] OracleMatch (NameMatch?) resolved variants with NULL semantics
- [ ] Root-unnarrowable pre-pass trigger in the query entry point
- [ ] Bind gather + verify for contains (regex/exact excluded: no needle trigrams)
- [ ] Benchmark: `o:draw or cn:100`, `o:draw or frame:showcase`; bare `o:draw` unchanged
- [ ] Query-log check: Or-query frequency to validate the priority

## Related

- [00620-engine-flavor-text-narrowing.md](00620-engine-flavor-text-narrowing.md) — the
  bind-rewrite pattern this generalizes (PR #622)
- [00619-engine-bitmap-streaming-select.md](00619-engine-bitmap-streaming-select.md) —
  #619; orthogonal (emission vs eval domain)
