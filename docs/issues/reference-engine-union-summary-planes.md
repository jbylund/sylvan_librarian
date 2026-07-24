# Engine: union-summary planes for tiered plane families (design note)

Status: design recorded 2026-07-08, deliberately not implemented. No GitHub
issue — file one if a plane family ever meets the criteria below.

## The rule tiering actually follows

Tiering a plane family (planes for dense values, postings for sparse — the
NameBigramIndex shape from #639) is **never a capability split**: a postings
list is algebra-complete because complement is scatter-then-flip
(all-ones minus members, O(k + words)) — the Not arm and the broad-range
complement trick already do exactly this. Tiering is purely a constants
trade: flat planes are zero-cost at query time; the postings tier saves
`plane_bytes − 2·k` at rest and pays an O(k) scatter per touched value per
query.

The recurring cost only bites when ops read **many values at once** — the
complement side of closed-mask algebras (`t=creature` reads all 13 other
type planes; `Le`/`Ne`/negations similarly). Membership-only dimensions
(bigrams, subtypes, keywords: one value per query, no op reads the other
values' planes) tier for free, which is why #639 did.

## The union-summary plane

For a tiered family whose ops DO read the complement side: add **one
aggregate plane, `rare_any` = OR of all sparse-tier values**. Then

- `t=creature` → `Creature ∧ ¬(dense outs) ∧ ¬rare_any` — zero scatters,
  exact, whenever the query mask names no sparse value.
- **Caveat**: when the mask itself contains a sparse value, the aggregate
  cannot separate "has snow only" from "has snow and world" (it loses
  which-rare information by construction) — `¬rare_any ∨ snow` wrongly
  admits snow+world. Fall back to scattering the named sparse values'
  postings (tiny by definition of the tier), keeping everything exact.
- Same trick as the flavor absent-gram bitmap (#623): a union summary that
  exists purely to *reject* cheaply.

Costs beyond flat planes: a `PlaneExpr::Bits(Vec<u64>)` leaf (owned,
compile-time-scattered bitmaps alongside `Plane(idx)`), tier lookup in the
compile arm, the rare-in-mask fallback, tests for all three.

## Why types didn't get it (2026-07-08 numbers, 31.5k cards)

| design | at rest | recurring | code |
|---|---|---|---|
| flat 14 planes (shipped, #633) | 55 kB | zero | zero |
| two-tier | ~27 kB | 2–4 µs on Eq/Le/Ne | Bits leaf + tiering |
| two-tier + rare_any | ~31 kB | ~zero | + the carve-out |

Saving 24 kB (0.03% of a 75 MB archive) doesn't buy the machinery. The
actual type-index waste was elsewhere: the TypeIndex postings (~145 kB)
have been write-only since the #637 plane feed-in replaced their narrowing
arm — remove them instead (planned with the devotion-planes archive bump).

## When to reach for this

A plane family where (a) values are numerous enough that flat planes cost
real space (hundreds+ values, or printing-space planes at 12 kB each),
(b) the density distribution is long-tailed, and (c) ops read the
complement side (closed-mask semantics). All three at once — otherwise
flat (small closed algebras) or plain two-tier (membership-only) wins.

## Related

- [00630-engine-card-bitplanes.md](00630-engine-card-bitplanes.md) — #630/#633 flat planes
- [00636-engine-adaptive-candidate-sets.md](00636-engine-adaptive-candidate-sets.md) —
  #636/#637 scatter/complement machinery this leans on
- [00623-engine-flavor-absent-gram-bitmap.md](00623-engine-flavor-absent-gram-bitmap.md) —
  #623, the same union-summary-as-rejector idea
