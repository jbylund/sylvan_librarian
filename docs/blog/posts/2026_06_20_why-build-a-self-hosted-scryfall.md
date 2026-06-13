---
title: "Why Build a Self-Hosted Scryfall?"
date: 2026-06-20
publishDate: 2026-06-20
tags: ["arcane-tutor", "mtg", "scryfall", "postgres", "python"]
summary: "Motivation for building Arcane Tutor: owning the query language, and the killer feature Scryfall can't do — arithmetic comparisons across card attributes."
---

## The query that started it all

I was playing Magic and wanted to find creatures where the combined power and
toughness is greater than twice the mana cost — cards that are "statistically
undercosted" by raw numbers. In Scryfall syntax you might try something like:

```
power+toughness>cmc+cmc
```

Scryfall doesn't support this. It has a rich query language — you can filter by
color, format legality, set, artist, flavor text, price — but arithmetic
comparisons across numeric fields aren't part of it. Each numeric field can be
compared against a constant (`pow>3`, `cmc<=2`) but not against each other, and
certainly not as part of an expression.

## The obvious first move

My first instinct was the developer's default: dump the Scryfall bulk data into
PostgreSQL and write the queries by hand. Scryfall publishes a daily JSON export
of every card, and `psql` can answer almost any question you can formulate. The
query above becomes:

```sql
SELECT card_name
FROM cards
WHERE creature_power + creature_toughness > cmc * 2
  AND type_line LIKE '%Creature%';
```

This works. It answers the question. But it immediately runs into a practical
problem: it requires a terminal. I wanted to look cards up while actually
playing the game — sitting across a table from someone, phone in hand, trying
to evaluate a card. Opening a terminal, SSHing somewhere, and typing a SQL
query is not that workflow. And even if I set up some kind of web interface,
hand-writing full SQL is a lot more friction than Scryfall's syntax, which is
concise and expressive enough that players already know it.

## What Scryfall gets right

Scryfall's query language is genuinely well designed. You don't need to know
SQL to use it. Filters compose naturally with spaces (implicit AND) and `or`:

```
t:creature c:red cmc<=3 o:haste
```

The syntax is terse, memorable, and discoverable. Players learn it organically
from using the site. Any replacement would need to feel at least as natural, or
nobody (including me) would actually use it.

The right move wasn't to replace Scryfall's syntax — it was to extend it.
Support everything Scryfall supports, then add the arithmetic layer on top.

## Extending the syntax

The extended syntax Arcane Tutor adds looks like this:

```
power+toughness>cmc+cmc
cmc+1<power
toughness>=power
```

Any numeric field (`power`, `toughness`, `cmc`, `loyalty`) can appear on either
side of a comparison, combined with arithmetic. The left and right sides are
evaluated as expressions, not just field references. This is the one thing the
project was built to do.

Everything else — color identity, format legality, text search, set filters,
regex search — is there because a useful card search tool needs it, and because
building on top of Scryfall's established syntax meant not having to invent a
new language from scratch.

## Then it spiraled

Once I had a working API, the next thing I added was a frontend that sent
queries as you typed and updated the results live. Reactive search sounds like
a small UI detail but it changes how you use the tool — you don't formulate a
complete query and submit it, you start typing and watch the results narrow.
It also meant the API needed to be fast enough that latency wasn't perceptible
while typing.

That led to an observation: with the right indexes, a self-hosted instance
could actually be faster than Scryfall for many queries. Scryfall is a
large public service handling traffic at a scale I'm not, but it's also a
general-purpose system. A single-purpose search engine with a carefully
tuned schema and indexes, running on a nearby server, can win on latency.
Measuring Scryfall today:

| Query | Scryfall |
|-------|----------|
| `power>toughness` | 1030ms |
| `id:g` | 538ms |
| `format:modern` | 1850ms |

Those are real response times from a fast connection. The same queries on
Arcane Tutor with a warm cache return in single-digit milliseconds. That
turned out to be achievable with careful indexing — and later, with an
in-process Rust engine, the query execution itself dropped to under a
millisecond.

From there, I made a checklist of everything Scryfall's query syntax supports
and worked through it: color identity with subset/superset semantics, format
legality, set filters, rarity, artist, flavor text, regex search, collector
number. Each one is a small engineering problem — how does this filter map to
the data model, what's the right index, how does it compose with everything
else. The arithmetic extension was the original goal; feature parity with
Scryfall became the project.

A third motivation emerged along the way: control over ordering. This turns
out to be two separate problems.

The first is *which card to rank first* in results. Scryfall's default sort is
alphabetical, which tells you nothing about relevance. A search for
`format:modern` returns cards starting with "A" — not the most-played cards,
not the most useful ones to know about. Integrating signals from CubeCobra and
EDHREC gives a relevance-based default sort that surfaces the cards people
actually care about.

The second is *which printing of a card to show*. For any given card name,
Scryfall might surface a showcase variant, a black-and-white secret lair, or a
foreign-language printing before a clean standard-frame copy. I have an
opinion about what the right printing looks like: standard frame, black border,
original artwork, non-foil unless foil-only. These preferences are encodable
as a numeric scoring expression, so the most canonical printing floats to the
top by default. A later post covers how both scoring layers work together.

## What's under the hood

The architecture is straightforward: a Python API that parses the query string
into an AST, compiles the AST to a parameterized PostgreSQL query, and returns
results as JSON. A vanilla JS frontend sends queries as the user types and
renders the results. The whole thing is containerized and designed to run on
a small VPS, accessible from anywhere — including a phone at a game table.

Performance became an increasing focus as the feature set grew. The parser
was eventually rewritten from scratch for a 49× throughput improvement. The
SQL hot path was later replaced with an in-process Rust engine for a 76×
speedup. But none of that was the original plan — it started with a single
query that Scryfall couldn't answer, and grew from there.

## This series

This is the first post in a series covering the full technical evolution of
Arcane Tutor: the query parser, SQL generation, PostgreSQL indexing, frontend
optimizations, and the Rust engine that eventually replaced the database for
search. Each post covers one piece of the system, roughly in the order it was
built.

The project is open source at [github.com/jbylund/arcane_tutor](https://github.com/jbylund/arcane_tutor).
