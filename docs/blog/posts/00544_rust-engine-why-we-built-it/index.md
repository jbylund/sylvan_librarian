---
title: "76× Faster Card Search by Moving Filtering Out of PostgreSQL"
date: 2027-02-13
publishDate: 2027-02-13
tags: ["rust", "python", "performance", "pyo3", "postgresql"]
summary: "Card search moved from PostgreSQL into a Rust extension via PyO3. The 76× speedup came not from clever SQL tuning but from eliminating the database round-trip entirely — and from hitting a ceiling that Python itself could not clear."
---

The PostgreSQL query for `t:creature` was taking 52 ms.
With indexes on every column, shared memory, connection pooling, and a well-tuned planner, it was still 52 ms.
We had hit a wall, and the wall was not PostgreSQL.

## What the Profiles Showed

A card search query follows a fixed shape: parse the query string, emit a SQL fragment, send it to PostgreSQL, stream back up to 100 rows, serialize to JSON.
At sub-100 ms latency the parse step and the HTTP overhead are negligible.
The bottleneck was always PostgreSQL.

But when we profiled what PostgreSQL was actually doing on `t:creature` — a query that matches about 50,000 of the 96,000 cards in the database — the answer was not slow execution.
It was slow *planning* for wide-result queries.
The query plan for a non-selective predicate triggered a full scan of an index that was not helpful for that result size, followed by a sort.
The planner had to work. Every. Time.

More than that: even with query result caching at the Python layer, a cache miss hit the database for a round-trip that included a network call (even on localhost), a query parse, a plan, and a result stream.
The minimum observable latency with a cold cache was bounded below by those fixed costs.

The [ILIKE post](../00384_ilike-trap-postgres-planner/index.md) had already shown that planning overhead can exceed execution time.
Numeric-range queries like `cmc>3` (68× faster in the engine) and legality queries like `format:legacy` (99× faster) carried the same disease: the data was not the bottleneck, the infrastructure around the data was.

## The Python Dead End

The natural first thought was: load the cards into Python memory, filter in Python, skip the database round-trip entirely.
Python is fast enough for 96,000 integer comparisons.

It is not fast enough for 96,000 Python function calls.

A Python in-memory filter loop that calls even the most trivial per-card check — reading an attribute, comparing it to a constant — hits a ceiling around 500,000 card evaluations per second.
At 96,000 cards that is 0.19 seconds per query, which is *slower* than PostgreSQL.
This is not a profiling surprise — CPython's per-function-call overhead (frame allocation, reference count updates, bytecode interpretation) runs at roughly 50–100 ns per call, which `python -m timeit 'f()'` on a trivial function confirms.
Multiplied across 96,000 cards, 10 fields, and several operators, the math does not work out.

We needed to do the filtering in a language where a tight loop over a struct field costs about 1 ns, not 50 ns.

## What a Rust Extension Gives You

The extension lives in [`card_engine/src/lib.rs`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs)
and is exposed to Python via [PyO3](https://pyo3.rs).
The `QueryEngine` Python class wraps a Rust struct that holds the full card corpus in an rkyv-serialized archive on shared memory (`/dev/shm/arcane_tutor_cards`).
[rkyv](https://rkyv.org/) encodes Rust structs in a layout that can be read directly from a memory-mapped file without any parsing or allocation — every worker maps the same bytes read-only, and queries read card fields in place.
Zero deserialization per query.

The `Card` struct stores fields at their narrowest correct width:

```rust
struct Card {
    // Hot fields first — fits in the first two cache lines for fast filter short-circuiting.
    card_name_lower: InlineStr<61>,
    card_colors: u8,
    card_color_identity: u8,
    produced_mana: u8,
    card_types: u16,
    // ...
    cmc: Option<u8>,
    creature_power: Option<i8>,
    creature_toughness: Option<i8>,
    // ...
}
```

`card_types` is a 14-bit mask — one bit per card type (Artifact, Creature, Instant, …).
A query for `t:creature` is `(card.card_types & TYPE_CREATURE) != 0`: a single `AND` instruction.
Color identity works the same way: `c:g` (cards whose color identity is a subset of green) is `(card.card_color_identity & !GREEN_BIT) == 0`.

The filtering is not just a loop over 96,000 cards.
Before the loop runs, [`narrow_candidates()`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/card_engine/src/lib.rs#L817) uses prebuilt indexes to restrict the candidate set:

| Index | Structure | Covers |
|---|---|---|
| Name trigram | `HashMap<[u8;3], Vec<u32>>` | `name:` substring queries |
| Oracle text trigram | deduped CSR (28k distinct texts, not 96k printings) | `o:` substring queries |
| CMC, power, toughness | sorted `Vec<(i16, u32)>` | numeric range queries |
| Card type bits | `[Vec<u32>; 14]` | `t:creature`, `t:instant`, etc. |
| Subtypes, keywords, tags | `HashMap<String, Vec<u32>>` | `t:merfolk`, `otag:voltron`, etc. |

AND queries intersect posting lists (merge two sorted vectors); OR queries union them.
A query like `t:merfolk and name:tide` narrows from 96,000 cards to a few dozen before the per-card filter runs.
That query runs in 0.02 ms.
SQL takes 3.6 ms — 190× slower.

## The Serialization Bridge

The Python AST has to cross the FFI boundary once per request; here is how that crossing works without pulling 96,000 cards back into Python.

The Python parser is unchanged.
Every AST node already implemented `to_sql()` to emit SQL fragments; we added a parallel [`to_json()`](https://github.com/jbylund/arcane_tutor/blob/f3e11f809493ab330a9aa67a4acb8a13dbdcf090/api/parsing/nodes.py#L42) method that serializes the node tree to a dict.
The Rust `query()` method calls `filters.to_json()` across the FFI boundary (one Python call), serializes to bytes with `orjson`, deserializes in Rust with `serde_json`, then evaluates the filter tree entirely in Rust:

```rust
fn query(&self, py: Python, filters: &Bound<PyAny>, ...) -> PyResult<...> {
    let to_json    = filters.call_method0("to_json")?;    // one Python call
    let json_bytes = py.import("orjson")?.call_method1("dumps", (to_json,))?.extract()?;
    let json_val: Value = serde_json::from_str(std::str::from_utf8(&json_bytes)?)?;
    let filter_expr = build_filter(&json_val)?;           // Rust FilterExpr tree
    // run_query() evaluates filter_expr over the mmap'd card store
}
```

The only data that crosses the FFI boundary is the JSON-encoded filter tree (a few hundred bytes) and the result dicts (at most 100 cards).
The 96,000-card corpus never moves.
The GIL is held only for the one `to_json()` call and the final dict construction, not for filtering.

## Results

Benchmarks run against the dev deployment with 96,139 cards loaded (`unique=card`, `limit=100`; engine timings are median of a 3-second timed window after 20 warmup runs; SQL timings are 12 measured runs after 3 discarded warmups, Python-layer cache cleared between each SQL call by restarting the Python process, PostgreSQL shared_buffers not flushed so the planner and buffer cache were warm; M5 Max, Python 3.13, PostgreSQL 17):

| Query | Engine | SQL | Speedup |
|---|---|---|---|
| `name:soldier` | 0.03 ms | 3.1 ms | 117× |
| `t:merfolk and name:tide` | 0.02 ms | 3.6 ms | 190× |
| `id:g` | 0.60 ms | 32.1 ms | 53× |
| `t:creature` | 0.59 ms | 52.2 ms | 88× |
| `cmc>3` | 0.70 ms | 47.3 ms | 68× |
| `cmc>6` | 0.11 ms | 8.2 ms | 74× |
| `format:legacy` | 1.01 ms | 100.0 ms | 99× |
| `(t:bird color:blue) or (t:beast color:green)` | 0.11 ms | 7.4 ms | 67× |
| `(name:forest) or (name:mountain)` | 0.07 ms | 7.1 ms | 98× |
| `power+toughness>8` | 0.95 ms | 19.0 ms | 20× |
| `power>4` | 0.15 ms | 11.4 ms | 76× |
| **geometric mean** | **0.20 ms** | **14.9 ms** | **76×** |

The weakest result — `power+toughness>8` at 20× — is the case where no index covers an arbitrary arithmetic expression across two fields.
The engine falls back to a full scan and evaluates the expression card by card.
Even without index help, 20× is the floor; every query with at least one indexable predicate is 50–190×.

## Why SQL Is Still There

The engine runs warm after the first request triggers a background reload.
A cold engine (empty store) serves from SQL while the reload populates the archive in the background.
Any exception from the engine path — a filter expression the current version cannot handle, a corrupted archive, anything — logs a warning and falls through to SQL transparently.
The `ENABLE_ENGINE` flag can disable the engine entirely per environment.

This made the rollout zero-risk: the SQL path was never modified.
The engine either answers the request or it does not; SQL always answers.

## What This Does Not Fix

The engine holds 96,000 cards in memory.
If the card count doubles, memory use doubles.
More importantly, the engine is per-worker: before the shared-memory redesign ([PR #502](https://github.com/jbylund/arcane_tutor/pull/502)), each of ten Bjoern workers held its own copy, consuming 800 MB–1 GB of RSS that PostgreSQL would have used for free.
The mmap approach in #502 collapsed this to one OS-page-cache copy shared across all workers — but that tradeoff belongs in a separate post.

The speedup also does not hold for queries that are genuinely database-bound in a way the engine cannot replicate: full-text search across very long oracle text with complex tiebreaking, for instance.
In practice those queries are rare and the fallback catches them.

The 76× geometric mean is real, but it is a property of this corpus and this workload.
A much larger corpus would narrow the index advantage for selective queries and widen the gap for full-scan queries.
We do not have data past 96,000 cards.

The result is a search path that answers in under 1 ms on a cold cache — where PostgreSQL's measured floor was 52 ms — falls back silently on any failure, and with the shared-memory redesign in [PR #502](https://github.com/jbylund/arcane_tutor/pull/502) uses no more RSS than the SQL path did.
