---
title: "Color Identity in One Byte: Turning a Seq Scan into Bitwise AND"
date: 2027-02-27
publishDate: 2027-02-27
tags: ["rust", "postgresql", "performance", "bitmaps"]
summary: "Why id:gruul was doing a full table scan and how encoding color identity as a 5-bit integer—twice, once in SQL and once in the Rust engine—fixed it with arithmetic that fits in a single CPU instruction."
---

Every Magic card has a color identity: the set of colors that appear in its mana cost and rules text.
Commander players use it constantly — `id:gruul` means "show me every card that fits in a red-green Commander deck."
That query was doing a full table scan of 97k rows, and we only caught it while running EXPLAIN ANALYZE to audit a different feature.

## The Index That Could Not Help

Color identity lives in a JSONB column, `card_color_identity`, as a key-presence map: `{"R": true, "G": true}` for a Gruul card.
We had a GIN index on that column, and GIN indexes on JSONB support containment operators — `@>` ("contains") and `<@` ("is contained by").
Those sound like exactly what subset search needs.

They are, for one direction.
`@>` asks "does this card's identity contain the query colors?" — that is superset search, `>=` semantics.
GIN can serve it because the query is a subset of the indexed value.
But `id:gruul` asks the opposite: "is this card's identity *contained by* the Gruul colors?"
The card's identity must be a subset of the query.
In PostgreSQL's GIN implementation, the `<@` direction — where the indexed value is smaller than the query — cannot be served by a GIN index.
Every `id:` query landed on a sequential scan of 97k rows.

The [PR #469](https://github.com/jbylund/arcane_tutor/pull/469) description captured the problem plainly: "`<@` in this direction (card's identity must be contained *by* the query) cannot be served by a GIN index, so every such query fell back to a full sequential scan."

EXPLAIN ANALYZE confirmed it.
Counting every card playable in a Gruul Commander deck — all 39,344 of them — took 280ms and touched every row in the table:

```
Seq Scan on cards  (actual time=0.014..278.937 rows=39344 loops=1)
  Filter: (card_color_identity <@ '{"G": true, "R": true}'::jsonb)
  Rows Removed by Filter: 57847
  Buffers: shared hit=65755
Execution Time: 280.178 ms
```

After the fix — expression index, bitmap function, `= ANY(array)` — the same count drops to 38ms:

```
Index Scan using idx_cards_color_identity_mask on cards  (actual time=0.033..37.487 rows=39344 loops=1)
  Index Cond: (color_identity_mask(card_color_identity) = ANY ('{0,1,2,3}'::smallint[]))
  Buffers: shared hit=27050
Execution Time: 38.756 ms
```

7× faster for the full-count case (280ms → 38ms), measured via `EXPLAIN (ANALYZE, BUFFERS)` on PostgreSQL 18, 97,191 cards, `shared_buffers = 2404MB`, Apple M5 Max, warm buffer cache (shared hits only).
A `LIMIT 100` search sees a shallower improvement for the same reason the seq scan looked fast in production: it can return 100 results after scanning only a fraction of the table, so the wall time is less damning.
The full-range scan is what matters, because the API runs results and count together in a single query — the count branch must walk every matching row.

## Encoding Five Colors as Five Bits

Five colors, one bit each. WUBRG fits in a byte:

```python
_COLOR_BITS: dict[str, int] = {"W": 16, "U": 8, "B": 4, "R": 2, "G": 1}
```

A Gruul card (`R`, `G`) gets mask `3` (binary `00011`).
An Esper card (`W`, `U`, `B`) gets mask `28` (binary `11100`).
Colorless is `0`.

A new SQL function encodes any card's JSONB color identity to this integer:

```sql
CREATE OR REPLACE FUNCTION magic.color_identity_mask(jsonb)
RETURNS smallint LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT (
        CASE WHEN $1 ? 'W' THEN 16 ELSE 0 END +
        CASE WHEN $1 ? 'U' THEN  8 ELSE 0 END +
        CASE WHEN $1 ? 'B' THEN  4 ELSE 0 END +
        CASE WHEN $1 ? 'R' THEN  2 ELSE 0 END +
        CASE WHEN $1 ? 'G' THEN  1 ELSE 0 END
    )::smallint
$$;
```

A B-tree expression index on `magic.color_identity_mask(card_color_identity)` then lets the planner do point lookups against the precomputed integer.
Full migration: [`api/db/2026-05-19-01-color-identity-mask.sql`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/db/2026-05-19-01-color-identity-mask.sql).

## Why Not `<= 3`

The natural SQL for "card identity is a subset of Gruul" would be `color_identity_mask(...) <= 3`.
That reads cleanly: any mask whose value is at most 3 can only have R and G bits set.

But it is wrong.
Mask `4` is Black.
Mask `5` is Black + Green.
Mask `7` is Black + Red + Green.
All three are less than `8` (Blue alone).
Integer range does not equal bit-subset containment because the bit positions are not consecutive values ordered by significance in the same way.

The correct test is bitwise: `v & ~query_mask == 0` — the card has no bits set outside the query mask.
There are at most 32 possible mask values (2^5).
At query time, Python enumerates them:

```python
def _subset_masks(query_mask: int) -> list[int]:
    return [v for v in range(32) if (v & ~query_mask) == 0]
```

For Gruul (mask = 3), this returns `[0, 1, 2, 3]` — colorless, Green-only, Red-only, and Red-Green.
The query becomes:

```sql
magic.color_identity_mask(card.card_color_identity) = ANY(%(mask_array)s::smallint[])
```

The B-tree index can serve multiple equality point lookups.
At most 32 values are in the array (the five-color case), and a B-tree scan over a smallint index covers that in microseconds.
A proper-subset check (`id<gruul`) uses the same approach but excludes the query mask itself — [`_proper_subset_masks`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/parsing/card_query_nodes.py#L205-L206).

The `=`, `>=`, and `>` operators are unchanged.
Those go in the superset direction that GIN can serve, so routing them through the bitmap path would be a regression.

## The Rust Engine Takes It Further

The PostgreSQL fix converts seq scan to index scan.
The Rust in-process engine — introduced in [PR #490](https://github.com/jbylund/arcane_tutor/pull/490) — skips the database entirely and evaluates queries against card data held in Rust memory.
There, the bitmap encoding is baked directly into the card struct.

Three fields in `Card` are each stored as a [`u8`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L151-L153):

```rust
card_colors: u8,
card_color_identity: u8,
produced_mana: u8,
```

At load time, [`jsonb_color_to_bits`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L346-L358) converts the JSONB dict to the `u8` once.
Every subsequent query reads the pre-encoded byte with no allocation and no pointer indirection.

The filter implementation is then three to five instructions per card:

```rust
FilterExpr::ColorCmp { field, op, mask } => {
    let bits = card_colors(card, *field);
    Some(match op {
        CmpOp::Le => bits & !mask == 0,           // id:gruul — subset
        CmpOp::Lt => bits & !mask == 0 && bits != *mask, // id<gruul — proper subset
        CmpOp::Ge => bits & mask == *mask,         // identity>=gruul — superset
        CmpOp::Gt => bits & mask == *mask && bits != *mask,
        CmpOp::Eq => bits == *mask,
        CmpOp::Ne => bits != *mask,
    })
}
```

No branch on the size of a set, no hash lookup, no string comparison.
`bits & !mask == 0` is one AND and one comparison.
The [full `ColorCmp` match arm](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/filter.rs#L400-L410) is nine lines.

## Cache Locality

A `u8` is one byte.
The three color fields together — `card_colors`, `card_color_identity`, `produced_mana` — are three bytes sitting immediately after [`card_name_lower` (61 bytes of inline string)](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L150) in the struct.
The 61-byte bound covers every card name in the Scryfall dataset with no heap allocation.
The struct comment makes the layout intent explicit: "Hot fields first — fits in the first two cache lines for fast filter short-circuiting."

When a query filters on both name and color identity — `id:esper t:creature o:flying`, a common Commander search — both predicates touch data that arrives in the same cache line.
A JSONB column stores a heap-allocated variable-length byte sequence behind a pointer; the actual bytes are elsewhere in memory.
Three `u8` fields after 61 bytes of name: same cache line.

With 96k cards in the store, a query that fails fast on color identity avoids loading most of the card's remaining data at all.

## Where This Does Not Help

The bitmap filter only affects the linear scan over candidates — the pass the engine runs after index narrowing.
Queries with a selective index (name trigrams, card type posting lists) arrive at the filter loop with far fewer candidates, so per-card cost matters less there.
The bitmap optimization is most visible when color identity is the only predicate and no other index narrows the set first — `id:esper` alone must walk all 96k cards.

The Rust engine handles color identity natively with the bitwise operators above.
The SQL expression index in PR #469 still matters for queries that route to the database: any query the engine cannot handle falls back to PostgreSQL, and the expression index keeps those off a sequential scan.

The same encoding is used for produced mana and card colors.
Encoding color as bits is not novel; what was worth doing twice is that each instance fixed a different ceiling: the SQL expression index eliminated the full-table-scan cost on queries that route to the database, and the Rust `u8` field eliminated the per-card evaluation overhead during in-memory filtering — a separate bottleneck the index cannot reach.
