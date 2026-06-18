# Blog Post Plan

Arcane Tutor blog series — covering the full technical evolution of the project.
Target: 26 posts across several topic areas, written roughly in the order they were built.
Publishing cadence: every two weeks starting 2026-06-20.

---

## Topics

### Overview

**[O1] Why build a self-hosted Scryfall?**
Scryfall is the gold standard for Magic: The Gathering card search, but it doesn't let you extend
the query language. This post covers the motivation for Arcane Tutor: what you gain from owning
the stack (custom query operators, no API rate limits, a platform to experiment on), and the one
killer feature that motivated the whole project — arithmetic comparisons across card attributes.
Queries like `cmc+1<power` or `toughness>power` are impossible in Scryfall but naturally expressible
once you control the parser and the SQL. Walks through a few motivating examples, sketches the
overall architecture (DSL → AST → SQL → PostgreSQL), and sets up the rest of the series.

---

### Data & Import

**[D1] PostgreSQL COPY loading: 10× faster bulk import**
The initial data loading path inserted rows one by one. Switching to PostgreSQL's `COPY` protocol
dropped import time from ~60s to ~6.5s. Covers why `COPY` is so much faster (binary protocol,
no per-row parse/plan overhead, batched WAL writes), how to structure Python to stream data into
`COPY`, and the tradeoffs around error handling when a single bad row aborts the whole batch.
See [PR #33](https://github.com/jbylund/arcane_tutor/pull/33).

---

### Parser

**[P1] Building a Query DSL with pyparsing**
A Scryfall-compatible search syntax is deceptively complex: arithmetic comparisons (`cmc+1<power`),
regex literals (`o:/^{T}:/`), implicit AND, operator precedence, quoted strings with apostrophes,
color identity subset semantics. Walk through the pyparsing grammar design choices, the places where
the grammar got surprising (false arithmetic detection, `Group` wrappers for nested expressions),
and the arithmetic consolidation that cleaned up three overlapping rules.

**[P2] Hand-rolling a recursive descent parser for 49× speedup**
pyparsing's general-purpose backtracking made it the latency ceiling (~3.2k parses/sec). This post
covers: identifying the bottleneck with timing spans, planning a hand-written recursive-descent
parser while keeping pyparsing as a live parity check, the 22 edge cases the parity suite caught
(precedence bugs, AST flattening divergence, parenthesized arithmetic crash), and the final 49×
throughput improvement (158k parses/sec). See also [changelog: hand-rolled parser](changelog/2026-05-21-hand-rolled-parser.md) and
[parity gaps](changelog/2026-05-21-hand-parser-parity-gaps.md).

---

### SQL Generation & Data Modeling

**[S1] Compiling an AST to parameterized SQL**
Each AST node implements a method that returns a SQL fragment plus bound parameters.
Cover the node hierarchy (`nodes.py`, `card_query_nodes.py`), how different node types
(text match, numeric comparison, JSONB array membership, regex, arithmetic expressions) each
emit their own SQL, and how `api_resource.py` wraps them into a full `SELECT` with scoring
and `LIMIT`. Emphasize that user input never touches the query string — always parameterized.

**[S2] PostgreSQL index strategies for mixed-type search**
The `magic.cards` table has 22 specialized indices. Walk through the index types: trigram GIN for
full-text substring search, GIN for JSONB arrays (colors, keywords, legalities, mana cost),
B-tree for numerics (cmc, power, toughness), and a functional index on `lower(card_name)`.
Explain when each type wins and what query shapes each one serves.

**[S3] The ILIKE trap: when the planner beats execution**
`ILIKE` on a trigram-indexed column was spending ~40ms in the query planner for a ~3ms execution.
The fix: add functional GIN indexes on `lower(column)`, lowercase patterns at query-build time,
emit `lower(col) LIKE lower_pattern`. A concrete lesson in using `EXPLAIN (ANALYZE, BUFFERS)` to
find planning overhead as the bottleneck. See [changelog](changelog/2026-05-20-ilike-to-lower-like.md).

**[S5] Oracle ID deduplication: what we tried, what worked, what didn't**
All three `unique=` modes (card, artwork, printing) shared one SQL shape that ran `DISTINCT ON`
and sorted inside the CTE even when unnecessary. Two hypotheses to test: does the DISTINCT ON key
type matter (`card_name` text vs `oracle_id` UUID), and is `DISTINCT ON (scryfall_id)` on the
primary key doing any real work? Built a reproducible benchmark harness — a seeded corpus of 200
queries weighted toward large result sets, each run with warmup + 50 timed rounds in round-robin
order using `EXPLAIN (ANALYZE, BUFFERS)` for wall-clock times. Results: UUID key was ~23% faster
than text key for `unique=card`; hashagg was not faster than `DISTINCT ON` (failed hypothesis);
dropping the no-op `DISTINCT ON (scryfall_id)` for `unique=printing` and pushing ORDER BY into
the LIMIT branch let PostgreSQL use a top-N heapsort instead of a full sort (~9% gain).
See [PR #480](https://github.com/jbylund/arcane_tutor/pull/480).

**[S4] Two levels of ordering: printing prefer score and card relevance ranking**
Search results need two distinct scoring concerns. The *printing prefer score* (printing-level)
decides which version of a card to surface: standard frame over showcase, black border, original
artwork, non-foil unless foil-only, legendary finishes. The *sort order score* (card-level) ranks
distinct cards by relevance using CubeCobra and EDHREC signals — so `format:modern` returns
playable cards first, not an alphabetical list. Both are numeric `ORDER BY` expressions in SQL.
Covers the JSONB `?` operator for array membership, how to encode aesthetic preferences as
weighted components, and how external card-power data (CubeCobra, EDHREC) integrates as a sort
signal. (Draws from the preference-score changelogs and PR #448.)

---

### Frontend

**[F1] 40× faster card rendering: swapping DOM nodes for a regex**
`createCardHTML` called `escapeHtml` ~14 times per card. The old implementation created a new
DOM element on every call — 1,400 throwaway elements per 100-card render. A single-pass regex
replacement reduced call cost from ~1,927 ns to ~48 ns (~40×), and fixed a latent bug where the
DOM path didn't escape double quotes inside attribute values. See [PR #486](https://github.com/jbylund/arcane_tutor/pull/486).

**[F2] Responsive card images and CDN delivery**
Serving card images efficiently: CloudFront distribution, responsive `<img srcset>` for different
screen densities, lazy loading, and how the image copy/sync scripts (set-based diffing, `CardImage`
model) keep the CDN in sync with Scryfall bulk data. Covers the tradeoffs between serving
at-request vs. pre-warming a CDN.

**[F3] Autocomplete for card types (and why it's harder than it looks)**
Suggesting the most common card types as the user types a `t:` query — without a round-trip.
Covers the data source (type frequency from bulk data), how the suggestions are ranked, and the
UX decisions around when to show/hide the list.

**[F6] Fonts and mana symbols**
Magic cards use a set of symbols — `{W}`, `{U}`, `{T}`, `{2/R}` — that need to render as glyphs
rather than raw text. Covers the choice of a custom webfont (each symbol as a Unicode character)
over SVG sprites or images, how font subsetting keeps the payload small, how the font is wired
into CSS, and the tradeoff between font loading latency and per-symbol image requests. Sets up the
follow-on post where the JavaScript that replaces the symbol tokens gets a 61× speedup.

**[F5] Mana symbol rendering: regex + Map for 61× speedup**
Replacing a loop-based string replacement with a regex pattern and a JavaScript `Map` lookup.
The original code iterated over each known mana symbol and called `.replaceAll()` for each one —
O(symbols × string length) per card. A single regex `/\{[^}]{1,5}\}/g` finds all symbol tokens
in one pass; `Map.get()` does a single O(1) lookup per token. Covers how to benchmark string
transformations accurately in JS, the gotcha of regex flags and stateful `.lastIndex`, and when
this pattern generalizes. See [PR #271](https://github.com/jbylund/arcane_tutor/pull/271).

**[F4] Progressive enhancement: useful without JavaScript**
The search endpoint returns cards via JSON for JS clients, but also serves a usable HTML response
for no-JS browsers. Walk through the server-side rendering path, what gets lost without JS
(autocomplete, reactive image loading), and why this is worth maintaining.

---

### Caching & Infrastructure

**[I4] Falcon + Bjoern: choosing a Python web framework**
FastAPI + uvicorn is the current industry default for Python APIs, but Arcane Tutor uses Falcon
with Bjoern (a C WSGI server). Covers what Falcon gives up (no ORM, no templating, no
auto-generated docs) and what it gains (a small, predictable surface area for a read-heavy API
with no request bodies). Why async didn't matter here — every request hits the database (or later,
the Rust engine), so the bottleneck is never I/O concurrency in the Python layer. Bjoern's
multi-process model and how it compares to uvicorn + gunicorn in practice.

**[I1] Multi-process cache invalidation with a generation counter**
Ten Bjoern worker processes share a port. A write that clears the cache on one worker leaves the
other nine serving stale results. Fix: a `multiprocessing.Value` generation counter; workers check
it on every request and rebuild their LRU on mismatch (maxsize=1, keyed `generation → value`).
A subtle class of bug invisible in single-process dev and test. See [changelog](changelog/2026-05-27-cross-process-cache-invalidation.md).

**[I2] Cachebox: a Rust-backed drop-in for Python cachetools**
Swapping `cachetools` for `cachebox` required only a thin key-hashing compatibility wrapper.
The performance gain came essentially for free. A short post on the pattern of reaching for
Rust-native Python packages as a low-friction performance lever.

**[I3] The evolution of `/random_search`: from `ORDER BY RANDOM()` to in-memory sampling**
The original random card endpoint ran two expensive queries on every request: a full table scan
with `DISTINCT ON` to find preferred printings (~30k rows), then `ORDER BY RANDOM() LIMIT n`.
O(N) per call, uncached. The fix: a TTL-cached `_get_all_preferred_cards()` that materializes the
preferred-printing list once per 10 minutes; individual requests then do an in-memory sample.
Covers why `ORDER BY RANDOM()` is so slow, how TTL caching changes the performance profile, and
the tradeoff between freshness and cost. See [PR #453](https://github.com/jbylund/arcane_tutor/pull/453).

---

### Rust Engine

**[R1] In-process filtering**
Why we moved card search out of PostgreSQL: the Python in-memory prototype (never merged), what
the profiles showed, why Python's function call overhead was the ceiling, and how a Rust extension
via PyO3 cleared it. Covers PyO3 build/packaging basics, the 76× speedup (14.9ms → 0.20ms), and
why the SQL path is kept as a live fallback.
See [changelog](changelog/2026-06-12-rust-query-engine.md).

**[R2] Index data structures in the Rust engine**
The different index types used to accelerate filtering: trigram sets, sorted arrays, hash maps,
and how each maps to query operators (substring, exact, range, set membership). Tradeoffs between
index size, build time, and query latency.

**[R3] Bitmap fields for color identity and cache locality**
Storing color identity as a bitmap instead of a string set: bitwise subset/superset checks replace
set operations, the struct fits in a cache line, and SIMD-friendly layouts become possible. Covers
when bitmaps are a good fit and what the before/after benchmarks looked like.
See [PR #469](https://github.com/jbylund/arcane_tutor/pull/469).

**[R4] Zero-copy deserialization with rkyv and shared memory**
Multiple workers used to each maintain their own copy of ~800MB–1GB of card data. The fix:
serialize the card store to a file with `rkyv`, `mmap` the file in each worker (one OS page cache
copy, copy-on-write per process). Covers `repr(C)` structs, the `mmap`/`mprotect` safety
invariants, and the streaming reload pipeline that cut peak memory from ~1.3GB to ~350MB.
See PRs [#502](https://github.com/jbylund/arcane_tutor/pull/502) and [#505](https://github.com/jbylund/arcane_tutor/pull/505).

**[R5] String interning for compact in-memory card representations**
Card text, type lines, set codes, and artist names repeat heavily across 30k+ cards. String
interning replaces each unique string with a u32 ID, reducing per-card memory and improving
cache behavior when filtering by string fields. Covers the intern table design and how it
interacts with rkyv serialization. See [PR #41bbcc8](https://github.com/jbylund/arcane_tutor/pull/490).

**[R6] Two-pivot pagination: O(n) sort for a single page**
Instead of sorting all matching cards and paginating, two pivots identify the score boundary of
the requested page; only the cards on that page are fully sorted. O(n) scan, O(page) sort.
Covers the pivot-selection algorithm and how it interacts with tie-breaking.

**[R7] Linear scan vs. hash scan for `distinct` queries**
To deduplicate results on a dimension (e.g., one printing per oracle ID), the engine chooses
between a linear scan (cheap for high-cardinality, small result sets) and a hash scan (better for
large result sets with many duplicates). Covers the threshold heuristic and how `distinct` queries
compose with scoring.

---

## Chronological Post Order

Ordering rule: post B comes after post A only when B's change directly depends on A's.
Everything else is interleaved freely to avoid topic clustering.

Dependency notes:
- P1 → P2
- S1 → S2 → S3 (S4 independent)
- R1 → all other Rust posts; R3 + R5 must both precede R4

| # | Post | Area | Publish date |
|---|------|------|--------------|
| 1  | [O1] Why build a self-hosted Scryfall? | Overview | 2026-06-20 |
| 2  | [I4] Falcon + Bjoern: choosing a Python web framework | Infra | 2026-07-04 |
| 3  | [P1] Building a query DSL with pyparsing | Parser | 2026-07-18 |
| 4  | [S1] Compiling an AST to parameterized SQL | SQL gen | 2026-08-01 |
| 5  | [D1] PostgreSQL COPY loading: 10× faster bulk import | Data | 2026-08-15 |
| 6  | [F4] Progressive enhancement: useful without JavaScript | Frontend | 2026-08-29 |
| 7  | [S2] PostgreSQL index strategies for mixed-type search | SQL gen | 2026-09-12 |
| 8  | [F3] Autocomplete for card types | Frontend | 2026-09-26 |
| 9  | [S4] Two levels of ordering: printing prefer score and card relevance | SQL gen | 2026-10-10 |
| 10 | [F6] Fonts and mana symbols | Frontend | 2026-10-24 |
| 11 | [F5] Mana symbol rendering: regex + Map for 61× speedup | Frontend | 2026-11-07 |
| 12 | [S3] The ILIKE trap: when the planner beats execution | SQL perf | 2026-11-21 |
| 13 | [S5] Oracle ID deduplication: what we tried, what worked, what didn't | SQL perf | 2026-12-05 |
| 14 | [F2] Responsive card images and CDN delivery | Frontend | 2026-12-19 |
| 15 | [I3] The evolution of `/random_search` | Infra | 2027-01-02 |
| 16 | [I1] Multi-process cache invalidation | Infra | 2027-01-16 |
| 17 | [R1] In-process filtering | Rust | 2027-01-30 |
| 18 | [R3] Bitmap fields for color identity and cache locality | Rust | 2027-02-13 |
| 19 | [I2] Cachebox: Rust-backed drop-in for cachetools | Infra | 2027-02-27 |
| 20 | [R5] String interning for compact in-memory card data | Rust | 2027-03-13 |
| 21 | [P2] Hand-rolling a recursive descent parser for 49× speedup | Parser | 2027-03-27 |
| 22 | [R2] Index data structures in the Rust engine | Rust | 2027-04-10 |
| 23 | [F1] 40× faster card rendering: DOM nodes vs. regex | Frontend | 2027-04-24 |
| 24 | [R4] Zero-copy deserialization with rkyv and shared memory | Rust | 2027-05-08 |
| 25 | [R6] Two-pivot pagination: O(n) sort for a single page | Rust | 2027-05-22 |
| 26 | [R7] Linear scan vs. hash scan for distinct queries | Rust | 2027-06-05 |

---

## Posts to research further

These topics came up in the git log but need more investigation before committing to a post:

- **Query explanations** (`#419` — Scryfall-style human-readable query explanations): could be a
  short post on translating AST nodes back to plain English.
- **Streaming bulk import** (`#497` — avoid memory spike during Scryfall data reload): relevant
  context for the rkyv post or a standalone infra post.
- **Color identity bitmap in SQL** (`#469`) vs. the Rust bitmap — could be one post covering both.
