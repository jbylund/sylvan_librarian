---
title: "Linear Scan vs. Hash Scan for Distinct Queries"
date: 2027-06-19
publishDate: 2027-06-19
tags: ["arcane-tutor", "rust", "performance", "query"]
summary: "Deduplicating results on a dimension (e.g., one printing per oracle ID): choosing between a linear scan and a hash scan based on result set size, and how distinct queries compose with scoring."
---

## The distinct problem


## Linear scan: when it wins


## Hash scan: when it wins


## The threshold heuristic


## Composing with scoring


## Related

The same deduplication problem was tackled earlier at the SQL layer — `DISTINCT ON` key choice,
hashagg vs. sort, and dropping a no-op primary-key dedup. See
[Oracle ID Deduplication: What We Tried, What Worked, What Didn't](00416_oracle-id-deduplication.md)
for the PostgreSQL side of the same story.

