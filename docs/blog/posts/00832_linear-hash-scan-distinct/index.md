---
title: "Adaptive Dedup: Linear Scan Wins Small Sets, Hash Wins Large — Here Is the Threshold"
date: 2027-06-19
publishDate: 2027-06-19
tags: ["rust", "performance", "query"]
summary: "Deduplicating results on a dimension (e.g., one printing per oracle ID): choosing between a linear scan and a hash scan based on result set size, and how distinct queries compose with scoring."
---

## The distinct problem


## Linear scan: when it wins


## Hash scan: when it wins


## The threshold heuristic

<!-- TODO: state the actual threshold and add supporting benchmark before publishing.
  The title promises "Here Is the Threshold" — that number must appear in this section.
  Needed:
  - The threshold value (N results) at which hash scan overtakes linear scan
  - A small table or chart: result set size vs. time for both approaches, showing the crossover
  - How the threshold was determined (empirically? analytically?)
  - Whether it's hardcoded or adaptive (and if adaptive, what signal drives it)
-->


## Composing with scoring


## Related

The same deduplication problem was tackled earlier at the SQL layer — `DISTINCT ON` key choice,
hashagg vs. sort, and dropping a no-op primary-key dedup. See
[Oracle ID Deduplication: What We Tried, What Worked, What Didn't](00416_oracle-id-deduplication.md)
for the PostgreSQL side of the same story.

