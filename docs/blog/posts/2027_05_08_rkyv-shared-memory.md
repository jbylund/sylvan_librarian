---
title: "Zero-Copy Deserialization with rkyv and Shared Memory"
date: 2027-05-08
publishDate: 2027-05-08
tags: ["arcane-tutor", "rust", "performance", "memory", "rkyv"]
summary: "Collapsing ~800MB–1GB of per-worker RSS into one shared copy using rkyv serialization and mmap. repr(C) structs, mmap safety, and streaming reload that cut peak memory from ~1.3GB to ~350MB."
---

## The per-worker memory problem


## rkyv: zero-copy deserialization


## Serializing to a file


## mmap across workers


## repr(C) and alignment requirements


## Streaming reload


## Memory before and after

