# Range leaves become composable, closing the artwork-range gap (#694/#731)

A `usd`/`cn`/`date` range leaf is now an exact **printing-space bitmap source** for the unified
`PrintingCompose` plan, not just a card-space fast path. Compose scatters the range index's in-range
slice into a printing bitmap, ANDs/ORs it with other composable leaves (border/rarity/legality/other
ranges) in printing space, and projects once to the query's unique space. This closes #694's last gap
— a range under `unique=artwork` — and enables compound ranges (`usd<50 cn<100`, `usd<50 border:black`)
in every distinct-on, which the card-space `CardRangePopcount` plan explicitly declined (a two-range
`∃p: usd(p) ∧ cn(p)` is a shared-witness case that must AND in printing space before projecting).

`CardRangePopcount` also stops materializing in acquire: it now does only the two binary searches for
the exact in-range count `k` and defers the card-bitmap build to dispatch, paid only if it wins. All
three range/compose plans are estimate-in-acquire; nothing eagerly materializes a losing plan.

Measured on the 97,206-printing corpus (`limit=100`, min of a timed window, compose kill-switch off
vs on):

| query | unique | before (μs) | after (μs) | speedup |
|---|---|---:|---:|---:|
| `usd<50 border:black` | printing | 1267 | 77 | 16.5× |
| `usd<50 cn<100` | printing | 1271 | 90 | 14.1× |
| `cn<100` | artwork | 910 | 124 | 7.3× |
| `usd<50 cn<100` | card | 917 | 140 | 6.6× |
| `usd<50 border:black` | artwork | 1291 | 200 | 6.5× |
| `usd<50` | artwork | 824 | 211 | 3.9× |

Bare `usd<50` at `card`/`printing` stays flat (still routed to CardRangePopcount / PrintingRangeScan).
Totals are byte-identical off vs on across a 1,500-query branch-vs-main survey (0 count mismatches),
and the distribution is unchanged (p50 42 μs, p90 184 μs both builds).

## Cost calibration this exposed

Two cost-model constants were fit from fresh kernel measurements to route the new compositions
correctly. Both are measurement-backed, not tuned to a single query.

**Range scatter is not a projection.** The old model charged one `SCATTER_PER_PRINTING_NS = 1.4`
constant for every query-time bitmap op. Kernels (`card_range_build_cost_split`,
`range_compose_kernel_costs`, `legality_compose_kernel_costs`) show three distinct rates:

- range **scatter** (`range_leaf_bits`, contiguous read + random write): **0.36 ns/printing**
- **linear pass** (legality broadcast-down + printing→card projection, both offset-walks): **1.50 ns/printing**
- CardRangePopcount's **fused build** (scatter + project in one pass via `printing_to_card`): **1.22 ns/printing**

Split into `RANGE_SCATTER_PER_PRINTING_NS` / `LINEAR_PASS_PER_PRINTING_NS` / `CARD_RANGE_BUILD_PER_PRINTING_NS`
with a new `scatter_printings` feature. The fused build being cheaper than compose's two passes
(1.22 < 0.36 + 1.50) is exactly why a bare range routes to CardRangePopcount, and the model now says so.

**Mask-compare verify tier was stale.** `MASK_COMPARE_NS100` was `600` (6.0 ns/row), pinned to a
NumericCmp that a 2026-07 re-run of `bench_verify_cost` now measures at 3.83 ns (Legality 2.05, YearCmp
2.39). At 6.0 ns/row it over-charged `StreamedSelect`'s scan term ~1.6× and mis-routed
`format:legacy or year:2020` / card onto compose (215 → 364 μs). Recalibrated to `400` (a conservative
ceiling just above the priciest measured member); the query routes back to `StreamedSelect` at **206 μs**.
