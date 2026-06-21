---
title: "The ILIKE Trap: When the Query Planner Beats Execution Time"
date: 2026-11-21
publishDate: 2026-11-21
tags: ["postgres", "sql", "performance"]
summary: "ILIKE on a trigram-indexed column was spending ~40ms in the query planner for a ~3ms execution. Functional indexes on lower(column) fixed it."
---

## Noticing the anomaly


## EXPLAIN ANALYZE: planning vs execution


## Why ILIKE defeats trigram indexes


## The fix: functional GIN on lower(column)


## Emitting lower(col) LIKE at query-build time


## Results

