# Engine: index the unindexed predicates (artist, set, legality, date)

Follow-on to [00603-engine-card-printing-split.md](00603-engine-card-printing-split.md) (PR #604).
Status: **done** — approaches 1, 2 and 4 (artist vocab, set-code index,
released-at index) plus the survey's price index shipped in PR #605
(2026-07-03): targeted-suite geomean 4.2× (artist ~10×, set 4–8×, year ~4×;
the compound-Or worst case 4.7×), parity exact, archive +1.6 MB. Approach 3
(legality postings) split out to
[local-engine-legality-postings.md](../local-engine-legality-postings.md), along with the
survey's rarity-postings suggestion.

## Problem

After the card/printing split, the most expensive query patterns are all predicates
with no index support, ranked by measured cost (97,199-printing corpus):

| Cost | Example | Mechanism |
| ---: | --- | --- |
| 2.57 ms | `t:creature (o:"draw a card" or a:rebecca)` | Or blocks all narrowing |
| 2.05 ms | `a:rebecca` | artist substring scan over 97k printings |
| 0.64–1.27 ms | `set:lea` variants | set-code scan over candidate printings |
| ~0.97 ms | `f:modern r:rare` | legality scan over all 31.5k cards |
| ~0.6 ms | `frame:2015`, `devotion:3`, `year=1994` | residual full scans |

The Or case compounds: `narrow_candidates` requires every Or child to narrow, so one
unindexable child (`a:rebecca`) voids the whole Or — and the sibling `o:` term then
runs its contains over 17.3k candidate cards without its trigram index.

## Proposed approaches

### 1. Artist vocab (fixes rows 1–2)

Only 2,195 distinct artists exist. Intern artists through the `VocabInterner`
pattern (u16 ids, printing-side); at query time resolve `contains("rebecca")`
against the 2.2k distinct strings *once* → a small set of matching artist ids →
per-printing integer membership instead of 97k substring scans (~1000× fewer
string compares). Optionally post an artist→printings `TagIndex` so `a:` narrows,
which also makes mixed Or nodes fully narrowable (fixing the compound case).
Same trick applies to any low-cardinality contains-searched field.

### 2. Set-code index (fixes row 3)

~1,000 distinct set codes, perfectly selective, printing space. A
`TagIndex`-shaped map (set code → sorted printing ids) turns every `set:` query
into an index hit. Highest value-per-effort on this list — set filters are common.
`set_name` could share the treatment if `set:` aliases route there.

### 3. Legality postings with a selectivity threshold (fixes row 4)

The card pass scans all 31.5k cards checking the 2-bit word. Post per
(format, status) card-id lists **only where selective** (the thresholding rule
from the PR #600 discussion: drop postings covering more than ~25% of the store):
`banned`/`restricted` everywhere, `legal` for rotating formats
(standard-legal ≈ 3k cards); vintage/commander-legal stays a scan. Advisory
narrowing means the threshold is purely a size/speed dial, not a correctness
concern. Divergent-legality cards (the 556) must appear in any posting a
minority printing qualifies for, or be excluded from postings entirely and left
to the scan — decide during implementation.

### 4. Released-at numeric index (rows 5, partial)

`year=`/`date>=` filters scan printings. The existing `NumericIndex` pattern
(sorted `(value, id)` pairs, binary-searched range → candidates) applies directly
with released_at_int in printing space. `frame_data` is already covered by the
thresholding idea in [00603-engine-card-printing-split.md](00603-engine-card-printing-split.md)'s
related notes; `devotion`/`mana` are inherently card-pass scans and already cheap
(~0.6 ms over 31.5k cards).

## Sizing notes

- Artist vocab: ~2.2k strings (~30 kB) + u16 per printing; optional TagIndex ≈
  97k postings ≈ 400 kB.
- Set index: 97k postings ≈ 400 kB.
- Legality postings under a 25% threshold: dominated by rotating-format `legal`
  lists and small ban lists; likely < 1 MB total.
- Released-at index: 97k × 8 B ≈ 780 kB.

All additive to a 72.7 MB archive; each stays useful under And-intersection with
the existing card-space indexes via the Candidates space rules.

## Tasks

- [x] Artist vocab + query-time id-set resolution + CSR artist index (PR #605)
- [x] Set-code TagIndex in printing space + narrow_candidates arm (PR #605)
- [x] Legality (format, status) postings — split out to
      [local-engine-legality-postings.md](../local-engine-legality-postings.md)
- [x] Released-at index in printing space for date/year filters (PR #605)
- [x] Re-run the #604 benchmark suites; the compound Or config (4.7×) and
      `a:rebecca` (9.2×) were the acceptance cases (PR #605)

## Broad-survey findings (2026-07-03, post-#605, 452 sampled configs)

Predicates × operators × And/Or/negation compositions × unique/prefer/orderby:
p50 0.24 ms, p90 1.09 ms, max 2.45 ms. The slowest-60 tail by mechanism
(overlapping tags): unindexed printing predicates 34, Or-with-unindexable-child
31, non-default prefer 14, flavor scans 11, frame 10, artwork mode 10,
legality 8. Slowest single predicates: `ft:` ~1.4 ms (no flavor index),
2-char name contains (`ox`) 0.89 (below trigram length), `usd>50` 0.83,
`cn:100` 0.80, `r:common` 0.80, `frame:showcase` 0.78, `r:mythic` 0.76.

Implications, in value order:

- **Price index** (`usd`): done in PR #605 (f32_sort_bits into the released_at
  range-index shape; survey p90 1.09 → 1.03 ms, `(t:goblin or usd>50)` 7.0×).
  eur/tix are three more lines each if ever wanted.
- **Rarity postings with the selectivity threshold**: six values; mythic/rare
  selective, common/uncommon dropped by the threshold. Folded into
  [local-engine-legality-postings.md](../local-engine-legality-postings.md) (same mechanism).
- **Legality postings** (already task 3): 8 of the slowest 60. Split out to
  [local-engine-legality-postings.md](../local-engine-legality-postings.md).
- **Flavor text**: split out to
  [00620-engine-flavor-text-narrowing.md](../00620-engine-flavor-text-narrowing.md) — a
  measured 9 MB trigram index was rejected in favor of a ~0.4 MB
  distinct-text-scan + CSR design.
- Bounded non-items: 2-char name contains (0.89 ms worst case, inherent trigram
  floor), artwork mode over broad matches with non-default prefers (emission
  cost of 20–45k groups, not scan cost), border/watermark/collector/layout
  (rare fields, ≤0.8 ms).

## Related

- [00603-engine-card-printing-split.md](00603-engine-card-printing-split.md) — the store
  restructure whose benchmarks surfaced this list; candidate-space rules live there
- [00598-engine-collection-vocab-interning.md](00598-engine-collection-vocab-interning.md) —
  the VocabInterner pattern approach 1 reuses
- [local-engine-legality-postings.md](../local-engine-legality-postings.md) — the remaining
  approach 3 + rarity postings, split out when this doc moved to done/
- Selective-index thresholding (PR #600 discussion) — approach 3 is its first
  concrete application; frame_data indexing composes with it
