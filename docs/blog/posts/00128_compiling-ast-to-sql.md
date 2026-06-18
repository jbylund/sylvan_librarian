---
title: "Compiling a Query AST to Parameterized SQL"
date: 2026-08-01
publishDate: 2026-08-01
tags: ["arcane-tutor", "sql", "postgres", "python"]
summary: "Each AST node emits a SQL fragment and bound parameters. How the node hierarchy works, how different field types generate different SQL, and why user input never touches the query string."
---

## The node hierarchy


## Text match nodes


## Numeric comparison and arithmetic nodes


## JSONB array membership


## Regex nodes


## Wrapping fragments into a full SELECT


## Parameterization as a design constraint

