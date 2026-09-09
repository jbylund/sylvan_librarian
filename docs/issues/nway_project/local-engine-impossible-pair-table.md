# A complete categorical co-occurrence table, so absence proves emptiness

**Filed as REFERENCE, not proposed work.** The mechanism is sound and nearly free; the query shape it answers is essentially absent from real traffic. Numbers at the bottom.

`printing_compose` proves only 43 of 733 empty queries empty, and every one of the 690 misses has `and_mechanism = (none)` — no joint mechanism fired at all. The cause is structural: compose is the only acquire route that never looks at the intersection, so it can prove zero only through a mechanism that knows the joint, and none covers these pairs. See [local-engine-empty-page-priced-infinity.md](local-engine-empty-page-priced-infinity.md) and `measurements/2026-09-05-tier1-provably-empty.txt`.

The existing pair tables cannot fill this in principle: they are **top-N**, so absence from them means "not in the top N", never "impossible". The misses are rare×rare conjunctions — estimate p50 **7** — which is exactly the population top-N excludes by construction.

## The proposal

A **complete** co-occurrence bitmap per pair of low-cardinality categorical dimensions: one bit for `(value_a, value_b)` meaning "at least one printing has both". Bit clear ⟹ the conjunction is provably empty. Complete rather than top-N is the whole point — that is what makes absence a proof.

Hook it into `leaves_are_disjoint`, which already returns `Some(0)` into `exact_result_total` and thence the `guaranteed` channel, so nothing downstream needs to change. It applies to any query with ≥2 positive categorical leaves, not only all-categorical ones: a sub-conjunction being empty proves the whole conjunction empty (Round 42's principle, the same one the pair tables already rest on).

## The claim is about the CORPUS, not about Magic

The bit means "no printing **in this corpus** has both values" — never "this pair is impossible in the game". That distinction is the whole soundness story. A statement about loaded data is exact for answering queries against that data, which is all the engine ever does; a statement about the rules is not, and reasoning from the rules is precisely what the Fallaji Wayfarer check caught being unsound (see the colour-contradiction entry in the queue). Like every other index it is derived data and must be rebuilt when the corpus changes.

So the table is a whitelist of observed co-occurrences with every unrecorded pair defaulting to empty. That default is what makes it complete rather than top-N, and it is the only reason absence can be a proof.

## It is small enough to be dense, so no compression

Measured over 97,812 printings:

| dimension | distinct values |
|---|---|
| keyword | 811 |
| set | 657 |
| t (subtypes) | 425 |
| watermark | 67 |
| frame | 29 |
| is | 23 |
| border | 5 |

| pair | full grid | observed | dense bitmap | unobserved |
|---|---|---|---|---|
| keyword × set | 532,827 | 11,172 | 65.0 KB | 97.9% |
| keyword × t | 344,675 | 7,621 | 42.1 KB | 97.8% |
| set × t | 279,225 | 20,826 | 34.1 KB | 92.5% |
| keyword × watermark | 54,337 | 887 | 6.6 KB | 98.4% |
| set × watermark | 44,019 | 654 | 5.4 KB | 98.5% |

**All 21 pairs, full dense grids summed: 1,396,205 bits = 0.2 MB.** Against a 97,812-printing archive that is noise. The grids are 87–98.5% zeros, which makes compression tempting, but it would trade away the O(1) bit test for a saving that does not register. Dense also gives exactness in both directions with no false-positive story to reason about.

## Coverage, measured

Of the 690 empty compose queries the acquire cannot prove empty:

| | queries | |
|---|---|---|
| ≥2 positive categorical leaves (eligible) | 253 | 37% |
| — names a value absent from the corpus entirely | 81 | provable by a simpler dictionary check |
| — **some pair genuinely impossible: the table fires** | **163** | **24% of all misses** |

Together, 244 of 690. Compose's empty-proof rate would go from **5.9% to ~39%**.

## Why this is NOT scheduled

Tier 1 measured the payoff for converting a proof into a skipped dispatch at **p50 +0.17 µs, total −0.7%** over 363 paired queries — indistinguishable from zero, because the work being skipped is already cheap. This proposal produces more proofs of the same kind, so the same null should be assumed until something shows otherwise.

Two arguments could still justify it, and both are unmeasured:

1. **Better cost-model input** — replacing a guess of 7 with an exact 0. Note the table only answers empty/not-empty, so it improves nothing anywhere else.
2. **The decline population** — the 74 queries that pay an entire compose build before refusing, ~0.5% of all measured time on uniform and 0.16% on realistic (the 3.59% once quoted here was a units error, corrected 2026-09-09), so that is NOT where the real time is after all. Whether the table reaches them is the question to answer FIRST; it is the only version of this with a plausible latency case.

Build cost is also unmeasured: an O(printings × pairs) scan at load, or a new archive section with the format-version bump that implies.

## Same-dimension pairs (`keyword × keyword`, `t × t`) — measured, and the shape is not there

Raised 2026-09-05. Multi-valued dimensions can collide with themselves, and the cells are the strongest in the table: `keyword × keyword` is **99.1%** unobserved (2,826 co-occurring pairs of 328,455), `t × t` **97.5%**. All four set-valued triangles cost **51 KB**. Single-valued dimensions need nothing — `set:a set:b` is already `leaves_are_disjoint`.

But the shape does not occur. In the uniform sampler: **0 of 8,000** queries have two positive leaves on one set-valued dimension, so that harness cannot evaluate this at all. In real crawled traffic, once disjunctions are excluded — `(t:island or t:mountain)` and `t:beast or t:bird` are the common form, and a co-occurrence table says nothing about an `Or` — it is **2 of 14,473 queries (0.01%)**, and both are non-empty (`(type:creature type:legendary) set:cmr`, `t:merfolk t:legend`), so the table would correctly not fire on either.

## The scope check that governs the whole item

The 24%-of-compose-misses figure above comes from the uniform sampler, which over-represents this shape by construction. Against the wild crawl corpus, conjunctive queries carrying **≥2 distinct categorical dimensions** are **17 of 14,473 (0.12%, 0.10% weighted)** — and that is before restricting to the empty ones. The most common pairs there are `is × set` (6) and `set × t` (5).

One caveat against over-reading even that: the categorical set measured here deliberately excludes rarity, format/legality and colours, which are the filters real users actually combine, because each already has its own exact mechanism (`LegalityDateTotals`, the rarity arm, `ColorCmcTable`). A version of this restricted to dimensions with no existing mechanism is what the 0.12% describes.
