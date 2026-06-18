---
title: "Color Identity in One Byte: Bitwise Subset Checks Across 30k Cards"
date: 2027-02-13
publishDate: 2027-02-13
tags: ["arcane-tutor", "rust", "performance", "bitmaps"]
summary: "Storing color identity as a bitmap instead of a string set: bitwise subset/superset checks, cache-line-friendly structs, and before/after benchmarks."
---

## Color identity as a set problem


## From string set to bitmap


## Bitwise subset and superset checks


## Cache locality benefits


## When bitmaps are a good fit


## Benchmarks

<!-- TODO: collect and add before/after numbers here before publishing.
  Needed:
  - Struct size in bytes per card before (string set) and after (u8 bitmap)
  - Filter throughput (cards/sec or ns/card) for a color identity query, before and after
  - Cache miss rate or L1/L2 hit delta if measurable (perf stat or cachegrind)
  Bonus: total RSS delta for the full 30k-card index before and after the switch.
  The title "Color Identity in One Byte" implies we show the size; confirm the struct actually
  fits the color fields in one byte and show the layout.
-->

