---
title: "PostgreSQL Index Strategies for Mixed-Type Card Search"
date: 2026-10-03
publishDate: 2026-10-03
tags: ["arcane-tutor", "postgres", "sql", "indexing", "performance"]
summary: "The magic.cards table has 22 specialized indexes. A tour of trigram GIN, JSONB GIN, B-tree, and functional indexes — when each wins and what query shapes each serves."
---

## The schema at a glance


## Trigram GIN for substring search


## GIN for JSONB arrays


## B-tree for numerics


## Functional indexes: lower(column)


## How to choose

