---
title: "String Interning for Compact In-Memory Card Representations"
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

