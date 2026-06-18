---
title: "Hand-Rolling a Recursive Descent Parser for 49× Speedup"
date: 2027-03-27
publishDate: 2027-03-27
tags: ["arcane-tutor", "parser", "python", "performance"]
summary: "pyparsing's backtracking was the latency ceiling. How we identified it, built a hand-written recursive-descent parser with pyparsing as a live parity check, and caught 22 edge cases along the way."
---

## Identifying the bottleneck


## The plan: parity-first rewrite


## Recursive descent basics


## Keeping pyparsing as a live comparator


## The 22 parity failures


## Results: 49× throughput

