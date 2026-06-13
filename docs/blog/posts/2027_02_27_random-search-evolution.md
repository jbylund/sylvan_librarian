---
title: "The Evolution of /random_search: From ORDER BY RANDOM() to In-Memory Sampling"
date: 2027-02-27
publishDate: 2027-02-27
tags: ["arcane-tutor", "postgres", "python", "performance", "caching"]
summary: "The random card endpoint went from two expensive queries per request (full scan + ORDER BY RANDOM()) to a TTL-cached in-memory sample. Why ORDER BY RANDOM() is so slow and how TTL caching changes the profile."
---

## The original implementation


## Why ORDER BY RANDOM() is O(N)


## The fix: TTL-cached preferred card list


## In-memory sampling


## Freshness vs cost tradeoff

