---
title: "22 Indexes on One Table: How Card Search Uses Every Index Type PostgreSQL Has"
date: 2026-09-26
publishDate: 2026-09-26
tags: ["postgres", "sql", "indexing", "performance"]
summary: "The magic.cards table carries indexes of every type PostgreSQL offers. A tour of trigram GIN for substring search, GIN for JSONB containment, B-tree for numerics, hash for exact-match fields, and an expression index that unlocks a color identity query that otherwise requires a full table scan."
---

`o:flying` searches 30,000+ cards for "flying" in oracle text.
`power>=5` scans for creatures with power at least 5.
`id<=ug` finds cards whose color identity is a subset of blue-green.
All three queries resolve in milliseconds.
They each hit a different index type, and one of them required rewriting both the query shape and the schema before any index could help.

## What Is on the Table

The `magic.cards` table has these columns, at a high level:

- **Numerics:** `cmc`, `creature_power`, `creature_toughness`, `planeswalker_loyalty`, `edhrec_rank`, `price_usd`, `price_eur`, `price_tix`, `card_rarity_int`
- **JSONB objects:** `card_color_identity`, `card_colors`, `card_legalities`, `card_keywords`, `card_frame_data`, `mana_cost_jsonb`, `devotion`
- **JSONB arrays:** `card_types`, `card_subtypes`
- **Text:** `card_name`, `oracle_text`, `flavor_text`, `card_artist`, `card_set_code`, `card_layout`, `card_border`

Each column class lands in a different index type.
Trigram GIN for text substring, GIN for JSONB containment, B-tree for range queries on numerics, hash for exact-match-only text fields, and functional indexes for query shapes that no structural index can serve.

The [schema](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/db/2025-09-29-great-reset.sql#L252-L279) defines 28 indexes in the base migration.
Three later migrations add ten more and drop four.
The result is a table with every index type PostgreSQL offers.
Not by design — each query class had a different problem, and no single index type could solve more than one of them.

## Trigram GIN for Substring Search

`o:flying` needs to find "flying" anywhere in `oracle_text`.
B-tree cannot help: it indexes the whole value in sorted order, so prefix lookups work but arbitrary substring scans do not.
The `pg_trgm` extension adds a GIN index type that decomposes text into overlapping three-character grams and stores them in an inverted index.
A `LIKE '%flying%'` query extracts the trigrams `fly`, `lyi`, `yin`, `ing`, finds their posting lists, intersects, and checks the heap pages — no full scan.

The current indexes are on `lower(column)` rather than the raw column:

```sql
CREATE INDEX idx_cards_oracle_text_lower_trgm
    ON magic.cards USING gin (lower(oracle_text) gin_trgm_ops);

CREATE INDEX idx_cards_cardname_lower_trgm
    ON magic.cards USING gin (lower(card_name) gin_trgm_ops);
```

The original indexes used `ILIKE` patterns against the raw column.
That was functionally correct — `ILIKE` matches case-insensitively, and `pg_trgm` supports it.
The problem was in the planner, not execution.
PostgreSQL's `ILIKE` selectivity estimator accounts for case-folding across the trigram set; the `LIKE` estimator does not.
The overhead is paid at planning time, not execution time, and it scales per condition:

Measured against a fully loaded 30,000-card dataset on PostgreSQL 17 using `EXPLAIN ANALYZE`, with the query `oracle:counter oracle:flying oracle:sacrifice tou>=5` as the three-condition case:

| ILIKE conditions | Planning time | Execution time |
|---|---|---|
| 0 | ~0.3 ms | ~25 ms |
| 1 | ~40 ms | ~8 ms |
| 3 | ~110 ms | ~3 ms |

That query was spending 110ms planning and 3ms executing.
Switching the indexes to `lower(column)` and lowercasing the search pattern at query-build time let the planner use the cheaper `LIKE` estimator while preserving case-insensitive matching.
The change is in [PR #470](https://github.com/jbylund/sylvan_librarian/pull/470) and the [migration](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/db/2026-05-20-02-lower-trgm-indexes.sql).

The query side emits ([card_query_nodes.py](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/parsing/card_query_nodes.py#L917-L933)):

```python
words = ["", *(_escape_like_pattern(w) for w in txt_val.lower().split()), ""]
pattern = "%".join(words)
return f"(lower({lhs_sql}) LIKE {context.add(pattern)})"
```

`o:flying` becomes `lower(card.oracle_text) LIKE '%flying%'`.
Multi-word terms like `o:"whenever you"` become `'%whenever%you%'` — words in order but not necessarily adjacent.

The caveat: trigram indexes are not useful for patterns shorter than three characters.
`o:pi` cannot extract any trigrams and falls back to a sequential scan.
This is a known limitation of `pg_trgm`, not specific to this implementation.

## GIN for JSONB Containment

Card types, keywords, and legalities are stored as JSONB.
The query `t:creature` asks whether the card's type array includes "Creature"; `format:modern` asks whether `card_legalities` includes `{"modern": "legal"}`.
Both use PostgreSQL's `@>` containment operator, which GIN indexes natively support.

The [indexes](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/db/2025-09-29-great-reset.sql#L257-L278):

```sql
CREATE INDEX idx_cards_legalities    ON magic.cards USING gin (card_legalities);
CREATE INDEX idx_cards_cardtypes_gin ON magic.cards USING gin (card_types);
CREATE INDEX idx_cards_colors_gin    ON magic.cards USING gin (card_colors);
```

The SQL each query emits ([card_query_nodes.py](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/parsing/card_query_nodes.py#L1006-L1018)):

```python
if self.operator in (">=", ":"):
    return f"({lhs_sql} @> {placeholder})"   # card's value is a superset of query
if self.operator == "<=":
    return f"({lhs_sql} <@ {placeholder})"   # card's value is a subset of query
```

`t:creature` maps to `card.card_types @> '["Creature"]'`.
`t<=creature` inverts the check — the card's type array must be a *subset* of `["Creature"]`, so only pure creatures qualify, not artifact creatures or enchantment creatures.

The important asymmetry: `@>` (card contains query) is indexable by GIN.
`<@` (card is contained by query) is not.
The planner can use the GIN index to find all cards where `card_types @> '["Creature"]'`.
For `card_types <@ '["Creature"]'`, it has to check every card's type array from scratch.
This asymmetry in GIN is what forced the color identity redesign described next.

## When GIN Cannot Help: The Color Identity Case

`id:ug` — find cards whose color identity is a subset of blue-green.
A Simic Guildmage (UG) qualifies.
A Forest (G only) qualifies.
A Nicol Bolas (UBRG) does not.

Color identity is stored as a JSONB object: `{"U": true, "G": true}` for Simic Guildmage.
The natural SQL for "card's identity is a subset of {U, G}" is `card_color_identity <@ '{"U": true, "G": true}'`.
But `<@` is not indexable by GIN.
Before [PR #469](https://github.com/jbylund/sylvan_librarian/pull/469), every `id:`, `id<=`, or `id<` query ran a full sequential scan — roughly 30,000 rows every time.

The fix uses a different query shape: precompute all valid bitmask values at query build time and issue an `= ANY(array)` lookup against an expression index.

Five colors map to a 5-bit integer: W=16, U=8, B=4, R=2, G=1.
Blue-green has mask `8+1=9`.
All subsets of 9 are masks `v` where `v & ~9 == 0`: the set `{0, 1, 8, 9}` — colorless, green-only, blue-only, blue-green.
A new SQL function encodes any card's color identity as that integer, and an expression index is built on it ([2026-05-19-01-color-identity-mask.sql](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/db/2026-05-19-01-color-identity-mask.sql)):

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

CREATE INDEX idx_cards_color_identity_mask
    ON magic.cards (magic.color_identity_mask(card_color_identity));
```

At query time, Python precomputes all 32 possible masks (there are only 2^5) and filters to the valid subset ([card_query_nodes.py](https://github.com/jbylund/sylvan_librarian/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/parsing/card_query_nodes.py#L963-L968)):

```python
def _subset_masks(query_mask: int) -> list[int]:
    return [v for v in range(32) if (v & ~query_mask) == 0]

subsets = IntArray(_subset_masks(_color_dict_to_mask(rhs)))
pmask = context.add(subsets)
return f"(magic.color_identity_mask({lhs_sql}) = ANY({pmask}::smallint[]))"
```

`id:ug` emits `magic.color_identity_mask(card.card_color_identity) = ANY('{0,1,8,9}'::smallint[])`.
The expression index turns that into a B-tree index scan on the precomputed integer — four point lookups instead of 30,000 row evaluations.

The `=`, `>=`, and `>` operators (superset checks) continue to use `@>` against the GIN index, since that direction is indexable.
Only `<=` and `<` needed the new path.
And the array is bounded: five colors means at most 32 candidate masks regardless of query complexity, so the precomputation cost is constant.

## B-Tree for Numerics and Ranges

Range queries — `power>=5`, `cmc<3`, `price<0.50` — use B-tree indexes.
B-tree supports `<`, `<=`, `=`, `>=`, `>` natively; the planner walks the index in sorted order and stops when the condition fails.

```sql
CREATE INDEX idx_cards_creature_power_btree
    ON magic.cards USING btree (creature_power)
    WHERE (creature_power IS NOT NULL);

CREATE INDEX idx_cards_price_usd
    ON magic.cards USING btree (price_usd)
    WHERE (price_usd IS NOT NULL);

CREATE INDEX idx_cards_edhrec_rank_btree
    ON magic.cards USING btree (edhrec_rank);
```

The partial indexes (`WHERE ... IS NOT NULL`) are deliberate.
Most cards do not have a power value — only creatures and vehicles do.
Including NULL rows in a power index would waste space and slow scans that will never return a match for non-creatures.
The partial index covers only the rows that can ever satisfy a `power` query.

Three covering indexes also exist for the most common sort patterns:

```sql
CREATE INDEX idx_cards_cmc_edhrec_btree_include
    ON magic.cards USING btree (cmc, edhrec_rank)
    INCLUDE (card_name, mana_cost_text, oracle_text, set_name, type_line, card_artist, illustration_id);
```

With these, a query like `cmc=2` sorted by `edhrec_rank` can satisfy the projection entirely from the index leaf pages — no heap fetch needed.
The `INCLUDE` columns travel with the index entry but are not part of the sort key.
The cost is index size: every included column is duplicated in the leaf page, and inserts or updates must maintain those copies.
At 30,000 cards the overhead is negligible, but it is the reason these covering indexes exist only for the three most-queried sort shapes rather than for every column combination.

## Hash for Exact-Match-Only Fields

`set:iko` needs exact equality — never a range, never a substring match.
Hash indexes are the right fit here: they store only the hash of the key rather than the key itself in a balanced tree, so they are smaller and do lookups in O(1) rather than O(log n):

```sql
CREATE INDEX idx_cards_set_code  ON magic.cards USING hash (card_set_code)
    WHERE (card_set_code IS NOT NULL);
CREATE INDEX idx_cards_layout    ON magic.cards USING hash (card_layout)
    WHERE (card_layout IS NOT NULL);
CREATE INDEX idx_cards_border    ON magic.cards USING hash (card_border)
    WHERE (card_border IS NOT NULL);
```

The tradeoff: hash indexes are useless for any operator other than `=`.
A query needing `set > "iko"` would skip the hash index and fall back to a sequential scan.
For set code, layout, border, and watermark, the query DSL exposes only exact-match semantics — the colon operator maps to `=` for these fields — so the restriction does not surface in practice.

## Routing to the Right Index at Query Build Time

The node that picks which SQL to emit is also the node that determines which index the planner can use.
`CardBinaryOperatorNode._handle_card_attribute` reads the field's type tag and routes:

```python
if field_info.parser_class == ParserClass.NUMERIC:
    return self._handle_numeric_comparison(context)  # → B-tree
if field_type == FieldType.JSONB_OBJECT:
    return self._handle_jsonb_object(context)        # → GIN or expression index
if field_type == FieldType.JSONB_ARRAY:
    return self._handle_jsonb_array(context)         # → GIN array containment
if self.operator == ":":
    return self._handle_colon_operator(...)          # → hash or trigram GIN
```

The routing is invisible to the user.
`power>=5` and `id:ug` are both valid query terms; the type tags decide which SQL operator and which index the planner can reach.

One category falls outside all of this: cross-column arithmetic comparisons like `cmc+1<power`.
PostgreSQL cannot index an expression that crosses two columns.
These queries do a filtered sequential scan, which is acceptable because the arithmetic expression itself is selective.
It is the one case where having 22 indexes does not help.

The three queries from the opening resolve like this: `o:flying` hits the trigram GIN on `lower(oracle_text)` and costs ~8ms instead of a full table scan.
`power>=5` hits the partial B-tree that excludes the roughly 20,000 non-creature cards who could never satisfy a power condition.
`id<=ug` hits the expression index on `color_identity_mask()` — four point lookups against a precomputed integer — after years of running as a full sequential scan.

The ILIKE planning overhead is a consequence of the trigram index choice and gets a full treatment in the [S3 post](../00384_ilike-trap-postgres-planner/), including EXPLAIN ANALYZE output and the before/after query plans.
The color identity bitmask reappears in the [R3 post](../00576_bitmap-fields-color-identity/), where the same five-bit encoding enables bitwise subset checks inside the Rust in-process filter engine.
