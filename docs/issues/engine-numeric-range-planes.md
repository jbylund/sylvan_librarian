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

## Design (final — one-hot interior planes + two cumulative boundary planes)

This went through three passes before landing here; see below for what
didn't work and why.

**One-hot planes for the well-populated interior** (roughly 1 through
9-10, hundreds-to-thousands of cards each): `field=K` for the interior
compiles directly to a plane read; ranges OR the relevant interior planes
together, same as colors/types.

**Two *cumulative* boundary planes per dimension, not one-hot per boundary
value:** `field<=L` (e.g. `power<=0`) and `field>=H` (e.g. `power>=10`),
built with the exact same plane machinery as everything else — a card's
bit is set if its value satisfies the threshold, full stop. This is the
key simplification:

- The low boundary plane (`<=0`) automatically absorbs whatever sparse
  negative values exist (power's 2 cards at -1, toughness's 1 card at -1)
  purely because that's what "value <= 0" already means — no separate
  handling of the negative tail, no side table, no live re-query needed.
- The high boundary plane (`>=10`, or wherever the interior stops)
  automatically absorbs the sparse high tail (cmc's 13-16, power/
  toughness's 13+) the same way.
- Any query of the shape `field<=K` or `field>=K` for **any** K — interior
  or landing on/beyond a boundary — compiles exactly as an OR of {relevant
  interior one-hot planes, the relevant boundary plane}. Zero new data
  structures, zero index queries at compile time — just more entries in
  the same `PlaneExpr::Or`, evaluated by the existing `eval_planes`.

**What still declines, and why that's fine:** queries that need to
distinguish *inside* a boundary bucket — `power=-1` specifically, or
`power<-1` (strictly tighter than what the `<=0` plane alone resolves) —
`compile_plane` simply returns `None` for these, exactly its existing
contract for anything outside a plane's scope, falling back to today's
`numeric_candidates` path unchanged. Not a compromise: these are
inherently selective queries (2 cards, 4 cards), and #630's original
verdict on numeric ranges only broke down for *broad* queries, which the
boundary planes now fully cover. No side-channel out of `compile_plane`,
no new `PlaneExpr` variant, no live re-query mechanism needed anywhere —
the exactness-boundary logic per operator (`Le`/`Lt`/`Ge`/`Gt`/`Eq`/`Ne`
against interior-range-plus-two-boundaries) mirrors `compile_devotion`'s
existing boundary tracking almost exactly, just with two saturation edges
instead of one.

### Earlier passes (superseded, kept for context on why they lost)

1. **Saturating "M+" bucket only, decline on the low tail as an open
   question.** Original sketch — left power/toughness's negative values
   unresolved ("check what -1 means before committing to a bidirectional
   scheme") and declined `field=15`-style queries entirely.
2. **A precomputed `value -> card_ids` side table for sparse values,
   resolved once per query at compile time.** Better — exact, no decline
   case — but needed a new side-channel out of `compile_plane` (it
   currently returns just `Option<PlaneExpr>`) to carry "a few extra ids
   to OR in" back to the caller, plus a new small build-time structure to
   maintain.
3. **Skip the side table, re-query the existing `numeric_candidates`
   index live at compile time** for whatever falls outside the one-hot
   range. No new storage, but still needed the same `compile_plane`
   side-channel, just fed by a live lookup instead of a precomputed table.

The final design above needs neither: making the boundaries themselves
*planes* (cumulative, not one-hot) means the sparse tails are absorbed
automatically by a mechanism that already exists, and the only "new"
behavior is `compile_plane` declining for the rare within-bucket case —
which is just its existing contract, unchanged.

## Expected

Should compound with #634: a plane-compiled `cmc<=6` becomes exact by
construction, feeding directly into #634's all_match promotion and
popcount-skip order phase — no separate exactness-classification work
needed for this dimension once both land. Because every value (common or
sparse) resolves exactly, there's no residual/fallback path to test for
these three fields at all.

## Results (implemented, 97,206-printing corpus, min-of-N ms)

Broad interior ranges go from an index-scan-and-resort to a pure plane OR:
`cmc<=4` 0.445 → 0.062 ms (**7.1×**), `cmc<=6` 0.405 → 0.067 ms (**6.1×**),
`toughness<=3` 0.131 → 0.060 ms (**2.2×**). The tail-crossing cases that
motivated the cumulative-boundary design also resolve exactly and fast:
`power<=2` 0.113 → 0.058 ms (**1.9×**), `toughness<=2` 0.106 → 0.057 ms
(**1.9×**). The flagged anomaly compounds with #634 as expected: `c:g
t:creature cmc<=4` 0.225 → 0.063 ms at offset 0 (**3.6×**), and 0.175 → 0.012
ms at offset 5000 (**15.0×**) once the fully-consumed filter unlocks the
popcount-skip order phase. Declines stay exactly flat, proving the
correctness tripwires hold: `cmc=15` (within-bucket, always declines)
0.0047 → 0.0049 ms; `-power>3` (Not over a nullable numeric field, must
always decline) 0.451 → 0.435 ms. No regressions across the exact/deep/
advisory/control groups already tracked for #634.

## Related

- #630 — parent issue; phase 1 (colors/types, PR #633) explicitly considered
  and rejected numeric ranges with the reasoning this doc revisits
- [engine-permuted-bitmap-order-phase.md](engine-permuted-bitmap-order-phase.md)
  — #634, where this was surfaced; not bundled into that work
- [engine-legality-bitplanes.md](engine-legality-bitplanes.md) — the
  divergent-carve-out pattern that does *not* apply here, and why
