---
title: "The Query Scryfall Can't Answer: `power+toughness>cmc+cmc`"
date: 2026-06-20
publishDate: 2026-06-20
tags: ["arcane-tutor", "mtg", "scryfall", "postgres", "python"]
summary: "Motivation for building Arcane Tutor: owning the query language, and the killer feature Scryfall can't do — arithmetic comparisons across card attributes."
---

I was playing Magic and wanted to find creatures where the combined power and toughness is greater than twice the mana cost —
cards that are cheap for their stats.
In Scryfall syntax, the natural query would be:

```
power+toughness>cmc+cmc
```

Scryfall has a rich query language — color, format legality, set, artist, flavor text, price — and it does allow comparing numeric fields against each other (`power>toughness` is valid), but arithmetic expressions like `power>toughness+1` or `power+toughness>cmc+cmc` are not supported.

The obvious answer is SQL.
Load the bulk data into PostgreSQL and write queries directly:

```sql
SELECT card_name
FROM cards
WHERE creature_power + creature_toughness > cmc * 2
  AND type_line LIKE '%Creature%';
```

That works — if you are at a terminal with the data already loaded.
The objection to building anything more is that this is already sufficient: one query, one answer, no infrastructure required.
But I wanted something usable from a browser or a phone during a game, without pulling up a laptop and writing SQL.
So I built [Arcane Tutor](https://github.com/jbylund/arcane_tutor): a self-hosted, Scryfall-compatible card search engine with extended arithmetic syntax.

Four motivations shaped how it was built.

## Supporting Arithmetic Queries

Scryfall's query syntax is the de facto standard.
To keep queries mostly portable between the two tools, I extended it rather than starting from scratch.
The extension I was most interested in was adding arithmetic comparisons over numeric card attributes, for example:

```
power+toughness>cmc+cmc
cmc+1<power
toughness>=power
```

Any numeric field (`power`, `toughness`, `cmc`, `loyalty`) can appear on either side of a comparison, combined with arithmetic.
The left and right sides are evaluated as expressions, not just field references.
I also added support for the most commonly used Scryfall filters to keep queries portable.

The query language was originally implemented as a custom DSL: a pyparsing grammar that produces an AST, which is compiled to parameterized SQL.
Later posts cover the grammar design and a hand-rolled rewrite that improved query parsing time by 49×.

## Results Should Update as You Type

I wanted results as I typed, not after submitting a complete query.
This fits how people use Scryfall —
they start with a broad search and narrow it with additional filters.
Reactive search makes that loop faster: the results update as you type.

The web interface is a vanilla JS frontend that sends queries on each keystroke;
the API returns results as JSON.
A later post covers the progressive enhancement story — the same endpoint serves both JS and no-JS browsers.

## Scryfall Takes Over a Second on Common Queries

Scryfall's response times are in the hundreds of milliseconds to seconds:

| Query | Scryfall |
|-------|----------|
| `power>toughness` | 1030ms |
| `t:creature` | 1100ms |
| `id:g` | 538ms |
| `format:modern` | 1850ms |

Reactive search requires low enough latency that results update without perceptible delay.
But fast responses are useful regardless — a card search tool should feel instant.

The initial implementation used PostgreSQL with specialized indexes, returning results in tens to hundreds of milliseconds.
The hot path was later replaced with an in-process Rust engine, dropping query latency to single-digit or sub-millisecond.
Later posts cover the PostgreSQL index strategy and the Rust engine in depth.

## Alphabetical Order Is Not Relevance

Result ranking breaks into two separate problems.

The first is *which card to rank first* in results.
Scryfall's default sort is alphabetical, which tells you nothing about relevance.
A search for `format:modern` returns cards starting with "A" —
not the most-played cards, not the most useful ones to know about.
Integrating signals from CubeCobra and EDHREC gives a relevance-based default sort that puts the most-played cards first.

The second is *which printing of a card to show*.
For any given card name,
Scryfall might surface a showcase variant, a black-and-white secret lair, or a foreign-language printing before a clean standard-frame copy.
I have an opinion about what the right printing looks like:
standard frame, black border, original artwork, non-foil unless foil-only.
These preferences are encoded as a numeric scoring expression so the best printing ranks first by default.

In the PostgreSQL search path, both were implemented as SQL scoring expressions.
A later post covers how both layers work together.

## How It's Built

The vanilla JS frontend sends queries to the Python API on each keystroke.
The API parses the query string into an AST, executes it against the card data, and returns results as JSON.
Originally that meant compiling the AST to a parameterized PostgreSQL query;
the hot path was later replaced with an in-process Rust engine for a 76× speedup.
`power+toughness>cmc+cmc` works, and `format:modern` returns in 17ms — over 100× faster than Scryfall.
