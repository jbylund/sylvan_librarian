---
title: "22 Indexes on One Table: How Card Search Uses Every Index Type PostgreSQL Has"
date: 2026-09-12
publishDate: 2026-09-12
tags: ["postgres", "sql", "indexing", "performance"]
summary: "The magic.cards table has 22 specialized indexes. A tour of trigram GIN, JSONB GIN, B-tree, and functional indexes — when each wins and what query shapes each serves."
---

## The schema at a glance


## Trigram GIN for substring search


## GIN for JSONB arrays

<!-- moved from 00128; needs expansion on the index side -->

Card types, keywords, and subtypes are stored as JSONB arrays. The `:` (contains) operator checks whether the query value is a subset of the card's array ([card_query_nodes.py](https://github.com/jbylund/arcane_tutor/blob/f23eff4ef5cdeb9fb0527193e1664ec3232fc549/api/parsing/card_query_nodes.py#L1038-L1068)):

```python
if self.operator in (">=", ":"):
    return f"({query} <@ {col})"    # query array ⊆ card's array
if self.operator == "<=":
    return f"({col} <@ {query})"    # card's array ⊆ query array
if self.operator == "=":
    return f"({col} <@ {query}) AND ({query} <@ {col})"  # mutual containment
```

`t:creature` asks whether `["Creature"]` is a subset of the card's type array. `t<=creature` inverts the check — the card's type array must be a subset of `["Creature"]`, so only pure creatures match (not artifact creatures or enchantment creatures).

Color identity uses a bitmask instead of JSONB operators. Five colors map to a 5-bit integer (W=16, U=8, B=4, R=2, G=1), and subset queries become integer array membership:

```python
subsets = IntArray(_subset_masks(_color_dict_to_mask(rhs)))
return f"(magic.color_identity_mask({lhs_sql}) = ANY(%({pmask})s::smallint[]))"
```

`c<=ug` precomputes all bitmasks that are subsets of `{U=8, G=1}` — that is, `{0, 1, 8, 9}` — passes them as a smallint array, and lets PostgreSQL's `= ANY(...)` do the lookup in one index scan.

## B-tree for numerics


## How to choose

<!-- Functional indexes on lower(column) are covered in [The ILIKE Trap](../00384_ilike-trap-postgres-planner/index.md) — they were introduced specifically to fix case-insensitive search planner overhead, so the story belongs there. -->

