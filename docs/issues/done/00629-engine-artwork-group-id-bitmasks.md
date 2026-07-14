# Engine: per-printing dense group-id bitmasks for artwork mode

Status: filed 2026-07-07, follow-up to the #619 streaming implementation.
GitHub: #629. In progress as of 2026-07-14 — see "Resolved design decisions" below.

## Resolved design decisions (2026-07-14, checked against real data)

Real distinct-illustration-per-card counts, `benchmarks/bitplanes/corpus.jsonl`
(31,508 cards): max 385 (basic lands), 5 cards exceed 255, 6 exceed 64.

- **u16 group id, not u8+overflow-flag.** 5 real cards exceed u8's range, so u8
  would need a genuine fallback path; u16 needs none, at a trivial extra ~97 kB
  archive cost. No per-card overflow flag.
- **No fixed-width-bitmask-plus-fallback split.** A fixed `u64` bitmask for
  match-counting with a fallback to the old `Vec<u128>` linear scan for >64-group
  cards would leave real O(k²) behavior on exactly the highest-printing-count cards
  — basic lands, some of the most heavily queried cards in real traffic. Instead:
  a growable multi-word bitmask (`Vec<u64>`, indexed by `gid / 64`) for counting, and
  a growable array-indexed "best score per group" scratch for emission — both O(1)
  per printing regardless of card size, no threshold, no second code path to test.

## Problem

Precomputed per-card group counts made artwork mode with card-level filters
cheap (`t:creature` artwork 1.5× → 2.8×), but printing-dependent filters
(`usd<50`, `rarity>=common` artwork, stuck at 1.2–1.3×) evaluate printings
individually, and each passing printing pays illustration bookkeeping on top
of the residual eval: a 16-byte UUID load plus a linear `Vec<u128>` scan.

## Design

Build-time dense group id per printing (0 = card's first-seen illustration,
shared artwork shares the id) — the dense-remap pattern from #605/#622 at
per-card scope. Distinct-group counting becomes `seen |= 1 << gid` +
`count_ones()`; emission's best-per-group selection indexes a scratch array
by gid instead of `find`-ing by u128 (helps every artwork query's page
cards, gathered path included).

u8 per printing (~97 kB) with a fallback flag for >255-illustration cards
(basic lands), or worry-free u16 (~194 kB). >64 groups → two mask words or
the Vec fallback. Archive bump.

## Expected (honest)

Only bookkeeping goes away; the residual eval floor stays. The 1.2–1.3×
artwork rows should reach ~1.4–1.6×, plus a small across-the-board artwork
emission win. Not the 2.8× of the all_match rows.

## Tasks

- [x] Dense group ids at build (u16, `assign_artwork_groups`)
- [x] Growable multi-word bitmask distinct-count in the streamed match phase
- [x] Group-id-indexed scratch in artwork emission (both paths)
- [x] Acceptance: `usd<50`/`rarity>=common` artwork improve; `t:creature`
      artwork and controls unchanged

## Results (2026-07-14)

Measured with `scripts/bench_bitplanes.py` against `benchmarks/bitplanes/corpus.jsonl`
(97,206 printings, 31,508 cards), main (`e906eb9`) vs. this branch, both release builds,
3s window/config:

| query | mode | main avg | branch avg | speedup |
|---|---|---|---|---|
| `usd<50` | artwork | 1.191 ms | 0.953 ms | 1.25× |
| `rarity>=common` | artwork | 1.223 ms | 0.906 ms | 1.35× |
| `t:creature` | artwork (all_match control) | 0.134 ms | 0.136 ms | flat (noise) |
| `usd<50` | card (control) | 0.431 ms | 0.420 ms | flat |
| `rarity>=common` | card (control) | 0.076 ms | 0.074 ms | flat |

Every `total` across all 49 targeted configs matched exactly between builds (parity
check). Below the issue's optimistic 1.4–1.6× — consistent with the "Expected
(honest)" section above: for `usd<50` the ~97k price compares still dominate, and
only the bookkeeping around them got cheaper.

**Broad screen**: `scripts/survey_queries.py --count 400 --wild 120 --seed 42` — 520
queries, 0 total mismatches, 0 regressions >15% (min_ms, excluding sub-20µs noise
floor).

**Memory**: `--features alloc-counter`, same corpus. `archive_bytes` identical byte-
for-byte between main and branch (69,027,636 both) — the new `artwork_group_id: u16`
field landed in `Printing`'s existing end-of-struct alignment padding, not a new
allocation. No memory regression to report.

## Related

- [00619-engine-bitmap-streaming-select.md](00619-engine-bitmap-streaming-select.md) — #619
- #622 / #605 — the dense-remap pattern at corpus scope
