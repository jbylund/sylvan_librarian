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

Self-hosting has real costs, though modest ones in this case. The hardware cost was marginal — I already run a home server for Plex and Pi-hole, so one more container was nothing. Card data imports automatically on container startup, and since I push code updates frequently, the data stays current without extra effort. The site is fully responsive, which is actually how I access it most often. The one genuine dependency on Scryfall remains: card data still comes from Scryfall's bulk data dumps. Arcane Tutor owns the query layer but not the cards themselves.

## Arithmetic Across Card Attributes

To keep queries mostly portable between the two tools, Arcane Tutor extends Scryfall's syntax rather than replacing it.
The extension is arithmetic expressions over numeric card attributes on either side of a comparison:

```
power+toughness>cmc+cmc    # combined stats beat twice the mana cost
cmc+1<power                # power exceeds mana value plus one
toughness-power>=2         # significantly more durable than threatening
```

Any numeric field — `power`, `toughness`, `cmc`, `loyalty` — can appear anywhere in an expression.
Both sides are evaluated as full arithmetic expressions, not just field references.

`power+toughness>cmc+cmc` returns creatures like Gigantosaurus (5 mana, 10/10 — 20 total stats against a threshold of 10) and Yargle, Glutton of Urborg (5 mana, 9/3 — 12 against 10).
These are exactly the cards that are difficult to evaluate by eye: the comparison is between two sums, neither of which appears on the card directly.
Scryfall can filter on `power` and `toughness` individually, but the arithmetic relationship between them and `cmc` requires something Scryfall does not expose.

The query language also supports the most commonly used Scryfall filters — type, color identity, format legality, oracle text, mana cost — so Arcane Tutor and Scryfall are interchangeable for standard queries.

## Fast Enough to Search on Every Keystroke

Reactive search — results updating as you type rather than on submit — requires latency low enough that the response arrives before the next keystroke.
The project started with direct PostgreSQL queries — similar latency to Scryfall, workable for one-off lookups but not for per-keystroke updates.
Replacing that hot path with an in-process Rust engine brought query times down to tens of milliseconds. Both columns are browser network-tab measurements using the same instrument. Arcane Tutor is served as arcane-tutor.com, so both sides include public internet routing — the difference is Scryfall's CDN versus a home server, not LAN versus internet. Hardware: MacBook Pro M5 Max (18 cores, 128 GB). One measurement per query: at speedups of 30×–93×, a single sample is sufficient to establish two orders of magnitude.

| Query | Scryfall | Arcane Tutor | Speedup |
|-------|----------|--------------|---------|
| `power>toughness` | 1030ms | 15ms | 69× |
| `t:creature` | 1100ms | 12ms | 92× |
| `id:g` | 538ms | 17ms | 32× |
| `format:modern` | 1850ms | 20ms | 93× |

At 15ms, results reach the browser before the next keystroke.
The frontend sends a request on each input event; the user sees live results without a submit button.

## Ranking by Relevance, Not Alphabet

Two separate problems fall under result ranking.

The first is which card to rank first.
Scryfall's default sort is alphabetical — `format:modern` returns cards starting with "A," not the most-played cards, not the cards most useful to know about.
Arcane Tutor integrates popularity signals from CubeCobra and EDHREC to rank by play rate, so the most-played cards appear first.

The second is which printing of a card to show.
A card with 30 printings in Scryfall might surface a showcase variant, a black-and-white secret lair, or a foreign-language copy before a clean standard-frame original.
Arcane Tutor encodes printing preferences as a numeric score: standard frame, black border, original artwork, non-foil unless foil-only.
Each criterion contributes a weight; the weights sum to a `prefer_score`; the highest-scoring printing for each unique card ranks first.

Printing preference is resolved inside a CTE using `DISTINCT ON` with its own `ORDER BY`; the outer query then ranks the deduplicated cards by play rate. Two ordering steps, but a single SQL statement — no application-level post-processing.

`power+toughness>cmc+cmc` returns fast enough for live search, with results ranked by play rate rather than name.

![Results for power+toughness>cmc+cmc](power-toughness-query-results.png)
