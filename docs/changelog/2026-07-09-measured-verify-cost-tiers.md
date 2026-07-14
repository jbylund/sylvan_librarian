# Verifier cost tiers are measured, not guessed

`verify_cost_tier()`/`regex_tier()` (#648, refined in #651) ranked FilterExpr
nodes into small ordinal tiers (0-3 / 1-3) assigned by judgment — only
Devotion/ManaCostCmp had ever been measured against an absolute cost
(`bench_mana.rs`, #651). Renumbering already caused churn once: #651's
reclassification forced a tier compaction across the whole function.

Both functions now return `u32` costs in hundredths of a nanosecond
(`ns * 100`) instead of ordinal ranks, via named constants
(`MASK_COMPARE_NS100`, `SET_LOOKUP_NS100`, `TEXT_SCAN_NS100`,
`REGEX_MACHINERY_NS100`) — recalibrating or inserting one op is now a
one-line constant edit, never a renumbering of its neighbors. Every constant
is backed by a new kernel bench, `bench_verify_cost.rs`, which times the real
`FilterExpr::matches()` path (no reimplemented kernels — there's nothing to
compare, just the current cost of the current code) against the real corpus
archive (31,508 oracle cards, min-of-50 per kernel, 3 repeated runs):

| cluster | measured | constant |
|---|---|---|
| mask/field compare (TypeCmp, ColorCmp, NumericCmp, ExactName, TextExact, Legality, DateCmp, YearCmp) | 1.8-5.6 ns | `MASK_COMPARE_NS100` = 600 |
| memoized-set binary search (ArtistMatch, FlavorMatch, NameMatch, OracleMatch, CollectionCmp) + anchored-literal regex | 1.8-8.1 ns | `SET_LOOKUP_NS100` = 900 |
| unmemoized TextContains | 21.6-22.7 ns | `TEXT_SCAN_NS100` = 2300 |
| regex without a usable anchor (bare literal *and* general machinery) | 44-49 ns | `REGEX_MACHINERY_NS100` = 5000 |

One correction fell out of the measurement: bare-literal regex (`(?i)flying`)
was assumed to cost the same as `TextContains` (both "a scan"). Measured on
equal footing (both carrying the `(?i)` every query regex has), it costs the
same as full regex machinery instead — ~2x a `TextContains` scan, not the
same. `regex_tier()` now folds the bare-literal case into
`REGEX_MACHINERY_NS100`.

Devotion/ManaCostCmp (#651) measure below `SET_LOOKUP_NS100`'s range
(0.65-2 ns) but keep sharing the constant deliberately, per #651's own
reasoning: tier 1 keeps them out of the Or acceptance-gamble bucket that
tier 0 would expose them to.

Re-ran the `bench_verify_order.py` A/B suite after recalibrating: parity
holds (554/554 queries, totals identical old vs new — this is a pure speed
dial), and the enrichment-family geomean improved from 1.16x to 1.21x, with
`o:/sacrifice a creature/ mana:{b}{b}` now hitting 4.78x (up from 3.15x
originally measured in #648, before #651's pip repacking and this
recalibration).

## Known gap, not fixed here

`bench_verify_cost.rs` also measured an *anchored non-literal* pattern
(`^[aeiou]`, no exploitable literal prefix): ~17.7 ns — far cheaper than
either bare-literal or machinery (~45 ns), because anchoring bounds the scan
to one position regardless of what's being tested there, not just for pure
literals. `regex_tier()` doesn't currently detect this — any pattern with
live metacharacters after the anchor still falls to `REGEX_MACHINERY_NS100`,
a safe but measurably conservative overestimate for this shape. See
`docs/issues/local-engine-regex-anchor-detection.md`.
