---
title: "u32 IDs Instead of Strings: Compact Card Representations Across 30k Records"
date: 2027-03-13
publishDate: 2027-03-13
tags: ["arcane-tutor", "rust", "performance", "memory"]
summary: "Card text, type lines, set codes, and artist names repeat heavily across 30k+ cards. String interning replaces each unique string with a u32 ID, shrinking per-card memory and improving cache behavior."
---

## The repetition problem


## String interning basics


## The intern table design


## u32 IDs in card structs


## Interaction with rkyv serialization


## Memory and cache impact

<!-- TODO: collect and add before/after numbers here before publishing.
  Needed:
  - Bytes per card struct before interning (with String fields) and after (with u32 IDs)
  - Total RSS for the full 30k-card index before and after
  - Number of unique strings across each interned field (card_name, type_line, set_code,
    artist, oracle_text) — this motivates why interning pays off
  - Filter or scoring throughput before and after (to show cache locality benefit, not just size)
  The title "u32 IDs Instead of Strings: Compact Card Representations Across 30k Records"
  should ideally get a memory number (e.g. "XMB → YMB") once you have it.
-->

