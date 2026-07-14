# In-process card filtering via `to_filter_func()`

## Status: closed — superseded by the Rust engine ([#490](https://github.com/jbylund/sylvan_librarian/pull/490))

The Python `to_filter_func()` path was prototyped on `joe/python_filter` and used as the
semantic reference for the Rust filter engine
([00490-rust-filter-extension.md](00490-rust-filter-extension.md)), which is what actually shipped.
The Python implementation was never merged; this doc is kept as the design record.

## Problem

Every search request generates parameterized SQL, round-trips to PostgreSQL, and pays for query
planning, index lookup, `DISTINCT ON` sort, and result serialization. For common queries this is
roughly 50–150 ms end-to-end.

The card corpus (~95k printings, ~22k unique oracle IDs) fits comfortably in memory at around
100–200 MB. At that scale a naive Python full scan is competitive with a DB round-trip — and
eliminates planning and network overhead entirely.

## Proposed approach

Add a `to_filter_func()` method alongside `to_sql()` on every AST node. It returns a
`Callable[[dict], bool]` that tests a single card dict. Boolean nodes compose child functions:

```python
class AndNode(NaryOperatorNode):
    def to_filter_func(self) -> Callable[[dict], bool]:
        funcs = [op.to_filter_func() for op in self.operands]
        def check(card: dict) -> bool:
            for func in funcs:
                if not func(card):
                    return False
            return True
        return check

class OrNode(NaryOperatorNode):
    def to_filter_func(self) -> Callable[[dict], bool]:
        funcs = [op.to_filter_func() for op in self.operands]
        def check(card: dict) -> bool:
            for func in funcs:
                if func(card):
                    return True
            return False
        return check

class NotNode(QueryNode):
    def to_filter_func(self) -> Callable[[dict], bool]:
        func = self.operand.to_filter_func()
        def check(card: dict) -> bool:
            return not func(card)
        return check
```

Leaf nodes close over compiled artifacts (regex, set literals) built once per query. All
implementations define a named inner `check` function and return it — no lambdas:

```python
# text contains (replaces ILIKE)
pattern = re.compile(re.escape(value), re.IGNORECASE)
def check(card: dict) -> bool:
    return bool(pattern.search(card['oracle_text']))
return check

# numeric comparison
def check(card: dict) -> bool:
    v = card['cmc']
    return v is not None and v < 3
return check

# JSONB array (colors, keywords, …)
def check(card: dict) -> bool:
    return 'R' in card['card_colors']
return check

# JSONB object (legalities, oracle_tags, is_tags)
def check(card: dict) -> bool:
    return bool(card['card_oracle_tags'].get('deathtouch'))
return check
```

Using compiled regex uniformly — for text contains, prefix, exact, and explicit regex nodes —
gives one code path instead of separate LIKE / ILIKE / regex branches. Named inner functions
show up with real names in tracebacks and profiles, unlike lambdas.

## Card store

A module-level singleton. The loading source can be swapped — see [Data sources](#data-sources)
below.

```python
# api/card_store.py
_cards: list[dict] = []

def all_cards() -> list[dict]:
    return _cards
```

Tags (`card_oracle_tags`, `card_is_tags`) are already materialized as JSONB objects on the card
row — no precomputation needed.

## Query execution

Filter, partition, and pick-best in a single pass. `unique=printing` uses `scryfall_id` as its
partition key so all three modes share the same code path — no special-casing needed.

```python
partition_key_fn = {
    "card":     lambda c: c['oracle_id'],
    "artwork":  lambda c: c['illustration_id'],
    "printing": lambda c: c['scryfall_id'],
}[unique]

prefer_key_fn = {
    PreferOrder.OLDEST:   lambda c: -int((c['released_at'] or '9999-99-99').replace('-', '')),
    PreferOrder.NEWEST:   lambda c:  int((c['released_at'] or '0000-00-00').replace('-', '')),
    PreferOrder.USD_LOW:  lambda c: -(c['price_usd'] or 0),
    PreferOrder.USD_HIGH: lambda c:  (c['price_usd'] or 0),
    PreferOrder.DEFAULT:  lambda c:  (c['prefer_score'] or 0),
}[prefer]

# single pass: filter + partition
filter_func = query_ast.to_filter_func()
partitions: dict[Any, list] = {}
for card in card_store.all_cards():
    if filter_func(card):
        partitions.setdefault(partition_key_fn(card), []).append(card)

# pick best candidate per partition — always max, key functions invert ascending cases
best = [max(candidates, key=prefer_key_fn) for candidates in partitions.values()]

# sort + limit
best.sort(key=sort_key, reverse=sort_desc)
total = len(best)
page  = best[:limit]
```

ISO date strings sort lexicographically so `int(date.replace('-', ''))` gives a comparable integer;
negating it makes `max` behave like `min`. The `UNION ALL` count collapses to `len(best)` before
slicing.

## Data sources

The card store is decoupled from its loading source so it can be populated two ways:

**Phase 1 — load from DB.** Explicit column list — not `SELECT *` — omitting columns that are
never needed for filtering, result output, partitioning, or sorting. The notable exclusion is
`raw_card_blob` (the raw Scryfall JSON dump). The full list is the union of:

- filterable columns (all `db_column_name` values in `db_info.py`)
- result columns (`card_name`, `card_set_code`, `collector_number`, `creature_power_text`,
  `creature_toughness_text`, `mana_cost_text`, `set_name`, `type_line`, `prefer_score`)
- partition keys (`scryfall_id`, `oracle_id`, `illustration_id`)
- sort/prefer columns (`released_at`, `cubecobra_score`, plus numeric columns already in filter set)

Reload after bulk imports. Tags are already materialized on the row. `prefer_score` is already
computed.

**Phase 2 — load from Scryfall bulk data directly.** `ScryfallBulkDataFetcher` already fetches
and caches the `all_cards` snapshot. Loading from it would mean no DB write step at all: fetch
bulk data → normalize field names → populate store. Two gaps to close first:

- `prefer_score` is computed locally (see `backfill_prefer_scores`). Could be computed in-process
  from the same fields used today, without ever writing to DB.
- `card_oracle_tags` / `card_is_tags` come from the Scryfall tagger API (separate from bulk data)
  and are currently scraped and written to DB. These would need to be fetched from the tagger
  and merged in at load time — or accepted as a second loading pass.

## Expected performance

| Filter type | Mechanism | Estimated cost (95k rows) |
|---|---|---|
| Numeric / boolean | dict field access + comparison | ~3 ms |
| Text contains | compiled `re.search` | ~15–25 ms |
| JSONB array / object | `in` / `.get()` on Python dict | ~5 ms |
| Dedup (22k unique cards) | `dict.setdefault` | < 1 ms |
| Sort + limit | `list.sort` on 22k rows | < 1 ms |

Estimated end-to-end for a typical query: **10–30 ms**, vs. 50–150 ms today.

## Phase 1 — naive full scan

Implement `to_filter_func()` on all node types. Run the card store and filter path in parallel
with the existing SQL path; log both latencies for comparison. Switch to the Python path once
benchmarks confirm parity or improvement across the representative query set in
[local-query-benchmark-suite.md](../local-query-benchmark-suite.md).

## Phase 2 — in-memory indices (future)

Only worth pursuing if Phase 1 profiling shows a bottleneck. Additive on top of the Phase 1
architecture — the full-scan path stays as a fallback for unindexed fields.

**Numeric / low-cardinality fields** are the easiest win. MTG attributes have very few distinct
values (e.g. ~17 distinct CMCs, ~800 distinct mana costs, 4 rarities, small power/toughness
range). A `dict[value, list[int]]` mapping each value to a list of card indices supports O(1)
equality lookup. Range queries (`cmc<3`) union the entries for all matching values — still fast
given the tiny value space.

**Text fields** (oracle text, card name) have high cardinality. Pre-building an inverted token
index (token → card list) would help prefix/contains queries but adds significant load-time cost
and memory. Profile before committing to this.

## Implementation tasks

- [ ] Add `to_filter_func()` to `nodes.py` base classes (`AndNode`, `OrNode`, `NotNode`,
  `TrueNode`, `BinaryOperatorNode`)
- [ ] Add `to_filter_func()` to leaf node types in `card_query_nodes.py`: text, numeric,
  JSONB array, JSONB object, rarity, date, mana cost, regex, arithmetic expression
- [ ] Implement `api/card_store.py`: `load(conn)`, `all_cards()`, thread-safe reload on bulk import
- [ ] Implement the filter → dedup → sort → limit pipeline in `api_resource.py` behind a feature
  flag (e.g. `USE_IN_PROCESS_FILTER = True`)
- [ ] Run both paths in parallel during rollout; log latencies for comparison
- [ ] Benchmark across the representative query set; confirm no correctness regressions
- [ ] Remove SQL path and feature flag once Python path is validated
