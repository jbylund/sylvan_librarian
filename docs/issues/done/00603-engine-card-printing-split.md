# Engine store restructure: cards as buckets of printings

Tracked as GitHub issue
[#603](https://github.com/jbylund/sylvan_librarian/issues/603), which mirrors this
doc's content with permalinked code pointers.

Item 6 of the store-size series, and a structural follow-on to
[00598-engine-collection-vocab-interning.md](00598-engine-collection-vocab-interning.md).
Status: **done** — merged in PR #604 (2026-07-03); archive 99.2 → 72.7 MB, geomean
query speedup 1.32× across 34 configs, exact parity modulo verified prefer-metric
ties. Within-bucket order ended up prefer-score-desc (not illustration), giving the
default-prefer walk O(1) selection; artwork grouping uses a small per-card scan
instead. The residual-evaluation follow-up flagged during benchmarking (the walk
re-evaluating card-level sub-predicates per printing on broad-card ×
narrow-printing conjunctions) also landed within #604: one-level residual
extraction in `card_pass_with_residual`, final round 27/34 configs strictly
faster, worst 0.91×.

## Problem

The store is a flat `Vec<Card>` of 97,199 printings, but ~half of each printing is
oracle-level data duplicated across its reprints (3.08 printings per oracle id): name,
oracle text, type line, types/subtypes, colors, mana cost, cmc, P/T, keywords, oracle
tags, edhrec rank. The dedup machinery (`unique=card/artwork` linear key-change dedup,
the hashmap fallback, `preferred_indices`) exists solely to reconstruct grouping the
data model already implies, and the preferred-printing fast path only fires for
`unique=card` + default prefer + card-level filters — `prefer=oldest/newest/usd_*`
scan and score all 97k printings.

## Verified against the tagged blue DB (2026-07-03, 31,508 oracle ids)

- Hoistable fields are printing-constant except: 3 oracle ids where the assembled
  face name differs by layout (omen reprints, e.g. "Marang River Regent // Coil and
  Catch" vs "… // Marang River Regent") and 556 where `card_legalities` differs.
  The legality divergence is **genuine, not (only) staleness**: minority-legality
  printings concentrate in non-tournament sets — 30a (406), ced/cei (184),
  ptc gold-border (129) — plus old core sets likely reflecting per-printing
  oldschool/premodern computation. Legality cannot be hoisted outright.
- Printings per card: median 2, 41% single-printing, p90 = 6, p99 = 18, max 851
  (basic lands); 230 cards have > 20 printings.

## Proposed layout

Do not nest (`Vec<Card { printings: Vec<Printing> }>` makes cards variable-size and
wrecks flat-scan cache behavior). Parent/child columnar instead:

- `cards: Vec<OracleCard>` (~31k) — hoisted oracle-level fields.
- `printings: Vec<Printing>` (~97k) — printing-level fields only (set, collector,
  rarity, artist, flavor, illustration, art tags, is-tags, frame, border, watermark,
  prices, released_at, prefer_score, scryfall_id). Keeps today's
  `(oracle_id, illustration_id)` sort, so each card's printings are a contiguous
  range and illustration groups stay contiguous within it (`unique=artwork` intact).
- `offsets: Vec<u32>` (~124 KB) — card *i*'s printings are
  `printings[offsets[i]..offsets[i+1]]`. Same CSR pattern as the oracle trigram
  index's `expand_text_ids`.
- Prefer mini-orderings: precompute the four non-default prefer permutations
  (oldest/newest/usd_low/usd_high) as `u16` lists only for cards above ~8 printings
  (~230 cards over 20; median card is 2 and selects on the fly for free).

Query flow for `unique=card`: evaluate card-level predicates once per card, then walk
the survivors' printings in prefer order and take the first passing the
printing-level predicates — structural dedup plus short-circuit, for every prefer.

## Expected wins

- Archive ~99 → ~75–80 MB: hoisting saves ~15 MB of archived cards (~230 B of the
  ~480 B printing is oracle-level), and card-level index postings shrink 3.08×
  (name trigram is posted per printing today; subtypes/keywords/oracle-tag/type/
  numeric postings move to card-id space).
- The preferred-printing fast path's three eligibility conditions (unique=card,
  default prefer, fully card-level filter) all disappear — the bucket walk *is* the
  only path, so performance stops being bimodal. Today `t:creature` fast-paths at
  ~31.5k evaluations but `t:creature r:rare` silently drops to 97k; after the split
  every query runs card predicates ×31.5k + printing predicates on survivors'
  printings only. It is also more complete than the fast path could be extended to
  be: the preferred printing may fail a printing-level predicate while a sibling
  passes — the prefer-ordered walk finds that sibling, a precomputed preferred
  index structurally cannot.
- Card-level scans switch from a gather (preferred_indices into the 480 B-stride
  printing array — touches most of the 46.8 MB anyway) to a sequential scan of a
  dense ~8 MB card array; the PR #600 full-scan speedups came from exactly this
  bytes-per-candidate effect.
- Non-default prefers stop scanning+scoring all 97k printings (they are ineligible
  for today's fast path); purely printing-level predicates (artist, frame, set)
  keep 97k evaluations — no comparison win there, only the smaller-row effect.

## Candidate-space rules (dual id spaces)

Card-level indexes post card ids; printing-level indexes post printing ids.
Narrowing stays advisory (eval verifies), so space conversions can only loosen or
tighten candidates, never break correctness. Rules:

- At And/Or nodes, combine within each space first, then cross the boundary once
  with the products — never convert per index (card lists are ~3× shorter; keep
  intersections there as long as possible).
- The consumer picks the final space: `unique=printing`/`artwork` expand the
  card-space product down (card id → contiguous range append, stays sorted);
  `unique=card` projects the printing-space product *up* (printing → card is one
  lookup; contiguity makes the mapped list sorted with adjacent dups, dedup free),
  then walks each candidate card's printings in prefer order.
- Bonus: a card-level-only query under `unique=printing` evaluates card predicates
  once per candidate card and emits its whole printing range unverified.

## Costs / risks
- Result assembly and `sort_key_bits` need a `(card, printing)` pair; printing-level
  orderby (usd, rarity) keys off the chosen printing, matching today's semantics.
- Legality is two-level by design: the card stores the word shared by its
  tournament printings plus a `legality_divergent` bit; every printing keeps its
  exact u64 (8 B × 97k = 778 KB). Flag clear (~98.2% of cards): the card-level
  equality check is exact. Flag set (556 cards): the card-level predicate returns
  "maybe" and the prefer-ordered walk verifies each candidate printing's own word.
  No union encoding needed (the 2-bit-per-format statuses don't OR: legal|banned
  would fabricate restricted). This is also more correct than today's fast path,
  which only ever checks the precomputed preferred printing.
- Decide canonical values for the 3 multi-name omen cards (match Scryfall's card
  object); the same "divergent" flag technique applies if name search ever cares.
- Archive format break (versioned header already handles); Python API unchanged.

## Tasks

- [x] Characterize the old-core-set slice of the 556 legality-divergent cards
      (genuine per-printing oldschool/premodern vs import staleness — affects
      how many cards carry the divergent flag, not the design)
- [x] Prototype the two-array + offsets layout behind the existing QueryEngine API
- [x] Re-run the memory protocol and the 20-query parity/latency harness from
      PR #600, plus new benchmarks for `prefer=oldest/newest/usd_low/usd_high`
      (the paths the current benchmark doesn't cover)
- [x] Decide fate of `preferred_indices` / linear / hashmap dedup paths (all
      collapsed into the structural walk; `query_linear`/`query_hashmap` removed)

## Related

- [00598-engine-collection-vocab-interning.md](00598-engine-collection-vocab-interning.md) —
  item 5; its measurement + parity harness carries over
- [local-engine-drop-lowercase-copies.md](../local-engine-drop-lowercase-copies.md) — item 4; hoisting
  name/oracle-text ids to cards changes its cost model (re-estimate after this)
- [local-engine-reload-publish-transient.md](../local-engine-reload-publish-transient.md) — smaller
  staging structures lower its live-heap floor further
- Selective-index thresholding (discussed on PR #600: drop posting lists covering
  >~25% of the store, add the missing frame_data index) — independent, composes with
  this; postings just get 3× shorter first
- [00605-engine-unindexed-predicates.md](00605-engine-unindexed-predicates.md) — the follow-on
  ticket for the expensive patterns this PR's benchmarks surfaced (artist, set,
  legality, date indexing)
