---
title: "PostgreSQL COPY Loading: 10× Faster Bulk Import"
date: 2026-08-15
publishDate: 2026-08-15
tags: ["arcane-tutor", "postgres", "performance", "python"]
summary: "Switching from row-by-row inserts to PostgreSQL's COPY protocol dropped import time from ~60s to ~6.5s. Why COPY is fast, how to stream data into it from Python, and error handling tradeoffs."
---

## The problem with row-by-row inserts


## How COPY works


## Streaming from Python


## Error handling: the all-or-nothing tradeoff


## Results

