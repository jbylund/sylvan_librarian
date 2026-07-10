# Engine: numeric-range bitplanes for cmc/power/toughness

Follow-on to #630 (phases 1/2 shipped as PR #633, #654). Not in scope for
#634 (permuted bitmap / exactness promotion, `engine-permuted-bitmap-order-
phase.md`) — filed separately, surfaced while benchmarking that work. Status:
proposed, not designed in detail yet.

## Problem

#630 phase 1's original survey rejected numeric ranges ("already served by
card-space range indexes that never lose"), which is true in isolation — the
sorted-index binary search (`numeric_candidates`) is exact and cheap for
selective queries. But it materializes every qualifying id from the sorted
index, so cost scales with match count. For a **broad** range this is real:
`cmc<=6` (30,164 of 31,508 cards, 96%) costs 0.436 ms today, dominated by
materializing that Vec — not by anything downstream.

## Why this isn't devotion's bit-slicing scheme

Devotion's 2-bit-per-color scheme works because queries are always about a
small, fixed set of thresholds (pip count 0/1/2/3+) — 2 bits exactly cover
every comparison shape that can occur. CMC/power/toughness queries use
arbitrary thresholds (`cmc<=4`, `cmc<=6`, `cmc=3`, ...), so a coarse
bucket scheme only answers exactly when a query happens to land on a bucket
boundary. The mechanism that actually generalizes is closer to colors/types:
**one plane per distinct value**, OR'd together for a range comparison —
`cmc<=6` becomes 7 planes ORed (~7×wpp word ops), cost independent of match
count. This is why it helps broad ranges specifically and does nothing for
selective ones (`cmc=9`, 101 cards — already cheap).

## Data (card-level, deduplicated by oracle_id, n=31,508, 2026-07-09 blue DB)

```
cmc:        0→1190  1→3055  2→6654  3→7602  4→5952  5→3762  6→1949
            7→853  8→297  9→101  10→52  11→18  12→16  13-16→7 (total)
power:      -1→2  0→767  1→3274  2→5181  3→3718  4→2130  5→1125  6→623
            7→265  8→115  9→47  10→40  11→9  12→13  13+→11 (total)
toughness:  -1→1  0→257  1→3434  2→4284  3→3954  4→2839  5→1395  6→689
            7→272  8→122  9→45  10→42  11→13  12→13  13+→9 (total)
```

All three are heavily right-skewed: cmc<=6 alone is 96% of the corpus.
One-hot planes for 0..12 (13 planes) plus a saturated "13+" bucket cover
99.98%+ of cmc values; similar for power/toughness.

## Design (revised — the original sketch below was wrong, kept for context)

**Planes for the common range** (roughly 0 through 10-12, hundreds-to-
thousands of cards each): one-hot plane per distinct value, OR'd together
for a range comparison — `cmc<=6` = 7 planes ORed, exact, O(words)
regardless of match count.

**A compile-time-resolved correction for sparse/outlier values** — power's
2 cards at `power=-1`, toughness's 1 card at `toughness=-1`, and cmc's high
tail (13→1, 14→1, 15→4, 16→1) — instead of either a saturating bucket
(declines to compile exactly whenever a query needs to distinguish *within*
the bucket, e.g. `cmc=15`) or a dedicated plane per rare value (an entire
3,944-byte plane, plus one more word-OR on *every* query touching that
dimension forever, to encode 1-4 cards' membership — wasteful).

The key realization, initially missed: unlike legality's divergent-card
correction (#654), which has to happen **per candidate at query time**
because a divergent card's true status depends on which printing you're
looking at (unknowable until `card_pass` runs), a card's numeric value is
fixed and known at *build* time — there's no ambiguity to defer. So the
correction can happen **once per query, at compile time**: for each sparse
tail value (a tiny precomputed `value -> card_ids` side table), check
whether `value <op> threshold` holds — a single scalar comparison — and if
so, scatter that value's few known card ids directly into the result
bitmap. Not "always include and let something re-verify" (legality's
pattern) but "resolve statically, include only when actually true" —
strictly exact, O(1) per query plus O(tail size) to scatter, no
decline/fallback case anywhere. This also removes the high-tail
saturating-bucket problem: `cmc=15` becomes exactly resolvable (check
13/14/15/16 against `=15`, scatter in the 4 matching ids) instead of
falling back to the old scan.

**Implementation note:** `compile_plane` currently returns just
`Option<PlaneExpr>`. Folding in "a few extra known ids to OR into the
result" needs a small side-channel out of that function — not a new
`PlaneExpr` tree node, just a few extra ids scattered into the evaluated
bitmap once after the tree evaluates (the same way `legal_candidate_bits`
scatters legality's divergent postings today). Empty/unused for every
existing caller (colors/types/devotion), populated only for numeric-range
tail values.

### Original sketch (superseded)

The first pass proposed a saturating "M+" bucket (declining to compile
`field=15` exactly, falling back to the old scan) and treated power/
toughness's negative tail as an open question ("check what -1 means before
committing to a bidirectional scheme"). Superseded by the compile-time
resolution above, which handles both ends of both tails exactly with less
storage than either a bucket or dedicated planes, and needs no fallback path
at all.

## Expected

Should compound with #634: a plane-compiled `cmc<=6` becomes exact by
construction, feeding directly into #634's all_match promotion and
popcount-skip order phase — no separate exactness-classification work
needed for this dimension once both land. Because every value (common or
sparse) resolves exactly, there's no residual/fallback path to test for
these three fields at all.

## Related

- #630 — parent issue; phase 1 (colors/types, PR #633) explicitly considered
  and rejected numeric ranges with the reasoning this doc revisits
- [engine-permuted-bitmap-order-phase.md](engine-permuted-bitmap-order-phase.md)
  — #634, where this was surfaced; not bundled into that work
- [engine-legality-bitplanes.md](engine-legality-bitplanes.md) — the
  divergent-carve-out pattern that does *not* apply here, and why
