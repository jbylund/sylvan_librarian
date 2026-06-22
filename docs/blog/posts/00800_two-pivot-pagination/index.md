---
title: "Paginating 30k Cards Without Sorting All of Them"
date: 2027-06-05
publishDate: 2027-06-05
tags: ["rust", "performance", "pagination"]
summary: "Instead of sorting all matching cards, two pivots identify the score boundary of the requested page and only those cards are fully sorted. O(n) scan, O(page) sort."
---

## The naive approach


## The insight: you only need one page


## Finding the pivots


## O(n) scan, O(k) sort


## Tie-breaking


## Interaction with offset

