# Card-mode fast path for bare `usd` range queries (#725)

A bare price range under `unique=card` (e.g. `usd<50`, the default unique mode) is now answered by a
new `CardRangePopcount` plan instead of a full scan with a per-card count pass. In one pass over the
range's exact printing slice it builds a card-existence bitmap (each printing's card via the
`printing_to_card` direct array) whose **popcount is the exact total** (no count pass), and the page
is read off the existing sort permutation — the same popcount-skip order phase the plane plan (#634)
already uses, fed a range-derived bitmap rather than a precomputed plane.

Measured on the 97,206-printing corpus (`limit=100`, min of an 8 s window, same build, kill-switch
off vs on):

| query | before | after | speedup |
|---|---:|---:|---:|
| `usd<50` / card | 0.340 ms | 0.143 ms | 2.38× |
| `usd<50` / card, offset 700 | 0.345 ms | 0.144 ms | 2.38× |
| `usd<2` / card | 0.457 ms | 0.131 ms | 3.48× |

Offset-flat, because the cost was always the count pass, not the paging. Totals are byte-identical
off vs on across the targeted set (the parity guard), and the broad 520-query survey and the 88-query
cost calibration are unchanged. The build is a single fused pass (scatter the printing bitmap and set
the card bit together) rather than scatter-then-project — a kernel bench showed the projection was
the expensive half, and fusing it is ~40% cheaper (it's most of the query cost, since there is no
precomputed structure — a persisted printing bitplane would be the next step, cf. #724).

## Scope

The plan fires only for a **bare** `usd` range in card mode — deliberately narrow:

- **Compounds with a card-invariant plane** (`usd<50 c:g`, `usd<50 t:creature`) stay on the existing
  narrowed-verify path: the plane already narrows the query hard, so that path is faster than paying to
  build the whole range bitmap.
- **Existential-legality compounds** (`usd<50 f:modern`) are excluded on correctness grounds — a
  card-existence AND is exact only when legality never diverges across a card's printings, which the
  engine refuses to assume (#667). Those belong in printing space.
- **Range+range** (`usd<50 cn<100`) is excluded — it is a shared-witness case (one printing must satisfy
  both), which a card-space existence AND cannot express.

`cn` / `date` share the machinery and are a follow-on (PR 3). Gated by `CARD_ENGINE_RANGE_BITS_CARD`
(default on) as an A/B kill-switch. Design notes: `docs/issues/done/local-engine-sorted-range-fastpath.md`.
