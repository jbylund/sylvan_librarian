---
title: "Oracle ID Deduplication: What We Tried, What Worked, What Didn't"
date: 2026-12-05
publishDate: 2026-12-05
tags: ["arcane-tutor", "postgres", "sql", "performance", "benchmarking"]
summary: "Two SQL hypotheses about DISTINCT ON key choice: UUID vs text, and whether DISTINCT ON the primary key does any real work. One hypothesis failed; two wins shipped."
---

## The starting point


## Two hypotheses


## Building a reproducible benchmark


## card_name vs oracle_id: ~23% faster


## Hashagg vs DISTINCT ON: no difference


## The no-op DISTINCT on the primary key: ~9% faster


## What shipped


## Related

The same deduplication problem — one preferred printing per oracle ID — reappears in the Rust engine,
where the query planner is gone and the choice is made explicitly in code. See
[Linear Scan vs. Hash Scan for Distinct Queries](00832_linear-hash-scan-distinct.md) for how the
engine picks between a linear scan and a hash scan based on result set size.

