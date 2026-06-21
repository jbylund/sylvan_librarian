---
title: "40× Faster Card Rendering, and a Latent XSS Bug: Swapping DOM Nodes for a Regex"
date: 2027-05-08
publishDate: 2027-05-08
tags: ["arcane-tutor", "javascript", "frontend", "performance"]
summary: "createCardHTML called escapeHtml ~14 times per card. The old DOM-element approach allocated 1,400 throwaway nodes per 100-card render. A single-pass regex fixed it and caught a latent XSS bug."
---

## The original escapeHtml


## 1,400 DOM nodes per render


## A single-pass regex replacement


## The latent double-quote bug


## Benchmarks: 1,927 ns → 48 ns per call

