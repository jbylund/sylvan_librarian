# Engine: transposed bitplanes for low-selectivity card dimensions

Status: phase 1 (colors + types) merged 2026-07-08 in PR #633 — targeted
geomean 1.89×, `c:g` 0.257 → 0.089 ms, controls 0.99×; produced mana
deferred. Phases 2 (legality) and 3 (rarity/keyword promotion) open.
GitHub: #630. Originally filed 2026-07-07 as successor to #619; revised
same day after checking the fields table against the code (rarity and
legality corrected, tasks phased).

## Problem

`c:g` already evaluates as a one-cycle u8 mask test, but delivering it costs
0.25 ms: 31.5k driver iterations, filter-tree dispatch, and a ~250 B cache
line per card to read one byte. Post-streaming, this eval floor *is* the
cost of broad low-cardinality queries.

## Design

One bitset per (dimension, value) in card space — 4 kB per plane at 31.5k
cards, plain u64 words + hardware popcount. Bind extracts plane-expressible
subtrees; word-wise AND/OR/NOT over cache-resident planes yields the match
bitmap in ~1–5 µs, feeding #619's order phase. Two consumption modes:

- **Exact**: plane truth = card-level truth. Colors/types qualify —
  ColorCmp/TypeCmp tri() is two-valued (never Null/PrintingDep), so Not is
  sound. Fully-consumed filters skip card_pass; all_match holds;
  offsets/group-counts supply printing/artwork totals.
- **Narrowing**: existence projection (bit = some printing matches); node
  stays in the filter, bitmap only narrows candidates. Rarity is this —
  `card_rarity_int` is printing-level, so its planes are never consumable,
  though plane-OR still retires the #618 union materialization.

Composition: And-children partition into consumed vs residual (bulk
card_pass); an Or is all-or-nothing (mask ∨ residual doesn't narrow). Mixed
filters intersect the bitmap with narrow_candidates() postings and iterate
set bits — no gather-and-sort, so #609 economics don't apply. Evaluate
word-at-a-time (`eval_word(expr, i) -> u64`), no per-node temporaries.

## Fields

- ✓ exact: colors / identity / produced (15 planes), card types (14).
- ✓ exact with carve-out: legality `legal` (~20) — `legality_divergent`
  cards (30A, CE, gold border) defer to per-printing words, so plane =
  "card-level legal AND not divergent" + tiny divergent-any set run through
  normal card_pass. Supersedes the postings plan for `legal`;
  banned/restricted stay postings.
- ✓ narrowing only: rarity (6) — printing-level, existence projection.
- Top-k subtypes/keywords, Ge/contains only (Eq/Ne/Le involve collection
  cardinality): the #628 threshold, inverted, is the promotion rule.
- ✗ numeric ranges (range indexes never lose); printing-space dims (12 kB
  planes, card-major match phase) noted as an extension.

Total ≈ 60–80 planes ≈ 240–320 kB.

## Phasing

1. Colors + types: transposition, extraction, word-loop evaluator + #619
   integration, candidate-mask path, full-corpus differential parity test,
   `c:g` benchmarks. Parity fixture: Fallaji Wayfarer — the one card (of
   97,206 printings, checked 2026-07-07) where colors ⊄ identity (WUBRG
   colors via "is all colors" CDA, identity [G]); guards any scheme
   assuming colors ⊆ identity.
2. Legality with the divergent carve-out; `f:modern` benchmarks.
3. Narrowing planes: rarity, density-threshold keyword promotion.

## Expected

`c:g` 0.25 → ~0.1 ms; broad conjunctions (`c:g t:creature f:modern`)
collapse to a few AND-loops plus streaming. Targets the 0.18–0.53 ms eval
floors from the emission probe.

## Related

- [00634-engine-permuted-bitmap-order-phase.md](00634-engine-permuted-bitmap-order-phase.md) —
  #634, the phase-1.5 successor: fuses the plane bitmap into the order phase
- [00619-engine-bitmap-streaming-select.md](00619-engine-bitmap-streaming-select.md) — #619
- [local-engine-legality-postings.md](local-engine-legality-postings.md) — superseded for `legal`
- #618/#628 — the threshold, reused as the promotion rule
