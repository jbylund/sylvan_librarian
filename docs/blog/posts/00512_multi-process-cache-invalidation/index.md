---
title: "10 Workers, 9 Stale Caches: The Cache Invalidation Bug That Only Appears in Production"
date: 2027-01-16
publishDate: 2027-01-16
tags: ["python", "multiprocessing", "caching"]
summary: "Ten worker processes share a port. A write that clears the cache on one worker leaves the other nine serving stale results. Fixed with a multiprocessing.Value generation counter."
---

## The bug: invisible in single-process dev


## How ten workers share state


## The generation counter approach


## maxsize=1 LRU keyed by generation


## Testing for this class of bug

