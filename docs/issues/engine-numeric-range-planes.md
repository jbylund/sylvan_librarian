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

## Design sketch

- One-hot plane per value 0..M (M≈12), plus one saturated "M+" plane (like
  devotion's top bucket): `field<=K`/`field>=K`/`field=K` compile exactly via
  mask OR whenever the comparison stays within 0..M, or crosses cleanly into
  "M+" without needing to distinguish values inside it (`field>=K` for K<=M
  is exact: OR planes K..M plus the M+ plane; `field=15` specifically is not
  — decline, fall back to today's exact index for that ~6-11-card sliver).
- No postings-repair / divergent-carve-out needed, unlike legality — these
  are pure card-level fields, never `Tri::PrintingDep`, never ambiguous. The
  tail simply declines to plane-compile and falls back to the existing exact
  numeric index, which is already cheap at that size.
- **Asymmetry to resolve before implementing:** cmc has no negative values
  (min 0), but power/toughness do (`-1` on 1-2 cards each, presumably `*`-power
  cards encoded oddly). cmc only needs saturation at the top; power/toughness
  need it at both ends, or a range like `power<=2` would silently exclude
  those 1-2 cards. Check what `-1` actually represents in the source data
  before committing to a bidirectional scheme.

## Expected

Should compound with #634: a plane-compiled `cmc<=6` becomes exact by
construction, feeding directly into #634 Part B's all_match promotion — no
separate exactness-classification work needed for this dimension once both
land.

## Related

- #630 — parent issue; phase 1 (colors/types, PR #633) explicitly considered
  and rejected numeric ranges with the reasoning this doc revisits
- [engine-permuted-bitmap-order-phase.md](engine-permuted-bitmap-order-phase.md)
  — #634, where this was surfaced; not bundled into that work
- [engine-legality-bitplanes.md](engine-legality-bitplanes.md) — the
  divergent-carve-out pattern that does *not* apply here, and why
