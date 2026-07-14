# Native filter extension (Rust/PyO3)

## Status: done — shipped as PR [#490](https://github.com/jbylund/sylvan_librarian/pull/490)

The engine landed on main 2026-06; `_search_engine` in `api/api_resource.py` serves queries when
`ENABLE_ENGINE` is on. The card store has since evolved past the struct sketched below — see
[00502-shared-card-store-mmap.md](00502-shared-card-store-mmap.md) (#502) and
[00504-engine-store-size-reduction.md](00504-engine-store-size-reduction.md) for the current shape.

## Problem

The in-process Python filter ([`local-in-process-card-filter.md`](./local-in-process-card-filter.md)) reduces
search latency significantly versus the DB path, but the remaining cost is Python interpreter
overhead — function call frames, reference counting, and GIL acquisition on every per-card check.
Profiling shows that for queries like `format:legacy`, ~50% of filter time is a single `dict.get`
call repeated 97k times; for `t:merfolk` essentially all cost is the function call itself.

The fundamental limit: Python cannot evaluate a tight loop over 97k objects without paying ~50–100ns
of frame overhead per iteration, regardless of what's inside the loop.

## Proposed approach

Move the card store and filter evaluation entirely into a compiled Rust extension (via PyO3 +
maturin). Python owns the query parsing (which stays in Python — it's fast and complex) and the
final result presentation. Rust owns the card data and does all filtering, deduplication, and
sorting.

### Interface

```python
from typing import NamedTuple
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import nodes

class QueryResult(NamedTuple):
    total_count: int     # distinct groups after dedup, before limit
    matches: list[dict]  # top N cards, len <= limit


class QueryEngine:
    """Rust-backed card filter engine exposed via PyO3.

    Owns the card store — card data lives in Rust's heap, not Python's.
    The only Python objects created per query are the final N result dicts.
    """

    def reload(self, db_rows: list[dict]) -> None:
        """Populate or replace the card store from DB rows.

        The engine starts empty; call this (or reload_from_file) before issuing queries.
        Swaps the store atomically — in-flight queries against the old store complete normally.

        Args:
            db_rows: Rows from magic.cards — the same column set used by the in-process filter,
                     including all filterable, sortable, partition, and result columns.
        """

    def reload_from_file(self, path: str) -> None:
        """Populate or replace the card store from a Scryfall JSON snapshot on disk.

        Args:
            path: Path to a Scryfall all_cards bulk-data JSON file.
        """

    def query(
        self,
        *,
        filters: nodes.Query,
        unique: UniqueOn = UniqueOn.CARD,
        prefer: PreferOrder = PreferOrder.DEFAULT,
        orderby: CardOrdering = CardOrdering.EDHREC,
        direction: SortDirection = SortDirection.ASC,
        limit: int = 100,
    ) -> QueryResult:
        """Filter, deduplicate, sort, and return the top N matching cards.

        Args:
            filters: Parsed query object returned by the parser. query() calls
                     filters.to_json() internally — callers do not need to serialize manually.
            unique:  Deduplication key: CARD groups by oracle_id, PRINTING by scryfall_id,
                     ARTWORK by illustration_id.
            prefer:  Which printing to keep per group (oldest, newest, cheapest, etc.).
            orderby: Primary sort field.
            direction: Sort direction.
            limit:   Maximum number of results to return.

        Returns:
            QueryResult.total_count — distinct groups matched, before the limit is applied.
            QueryResult.matches     — up to `limit` result dicts, one per group.
        """
```

The existing bulk-import path calls `card_store.reload()` after writing new data; that call site
should also call `engine.reload(db_rows)` to keep the Rust store in sync.

### Card struct

```rust
struct Card {
    // partition / dedup keys (UUIDs as [u8; 16])
    scryfall_id:      [u8; 16],
    oracle_id:        [u8; 16],
    illustration_id:  Option<[u8; 16]>,

    // text fields — pre-lowercased at load time for fast substring checks
    card_name:        String,
    card_name_lower:  String,
    oracle_text:      String,
    oracle_text_lower: String,
    flavor_text:      String,   // "" when null in DB — substring search on "" is always false
    flavor_text_lower: String,
    card_artist:      Option<String>,   // nullable per DB schema; some cards have no artist
    card_artist_lower: Option<String>,
    card_set_code:    String,
    card_layout:      String,
    card_border:      String,
    card_watermark:   Option<String>,
    collector_number: String,
    mana_cost_text:   Option<String>,
    type_line:        String,
    set_name:         String,

    // colors — u8 bitfield, canonical WUBRGC ordering: W=1, U=2, B=4, R=8, G=16, C=32
    // enables multi-color containment/subset checks as a single bitmask operation
    // NOTE: _COLOR_BITS in card_query_nodes.py currently uses a different ordering and must be
    // corrected to WUBRGC (with C added) as part of this work
    card_colors:         u8,
    card_color_identity: u8,
    produced_mana:       u8,

    // integer fields — None means NULL in DB (NULLS-LAST semantics: None fails all comparisons)
    cmc:                  Option<i8>,   // max CMC in practice ~16
    creature_power:       Option<i8>,   // can be negative (e.g. Char-Rumbler); max ~30
    creature_toughness:   Option<i8>,
    planeswalker_loyalty: Option<i8>,   // always 1–12 or so
    card_rarity_int:      Option<i8>,   // 0–5
    collector_number_int: Option<i16>,  // some sets exceed 127; i16 covers up to 32k
    edhrec_rank:          Option<i32>,  // up to ~25k unique cards ranked

    // real-valued fields
    price_usd:      Option<f32>,
    price_eur:      Option<f32>,
    price_tix:      Option<f32>,
    prefer_score:   Option<f32>,
    cubecobra_score: Option<f32>,

    released_at:    String,  // "YYYY-MM-DD" — lexicographic comparison works

    // variable-length collections — Vec<String>
    // fixed-universe fields (colors) use bitfields above;
    // variable-universe fields use Vec for simplicity and correctness
    card_types:       Vec<String>,
    card_subtypes:    Vec<String>,
    card_keywords:    Vec<String>,

    // legalities — HashMap; always a single-key lookup at query time
    card_legalities: HashMap<String, String>,  // format → "legal"|"restricted"|"not_legal"

    // tag and frame sets — presence-only (all values are true); HashSet gives O(1) contains()
    card_oracle_tags: HashSet<String>,
    card_is_tags:     HashSet<String>,
    card_frame_data:  HashSet<String>,

    // mana cost — custom structure mirroring mana_cost_jsonb
    mana_cost: ManaCost,

    // output-only (not filtered on; used to construct result dicts)
    creature_power_text:    Option<String>,
    creature_toughness_text: Option<String>,
}
```

### Why bitfields for colors

Colors have a fixed 6-element universe and queries commonly check multiple colors at once
(`c:rg`, `c>=bant`). A `u8` makes all subset/superset checks a single CPU instruction:

```rust
// c:rg — card contains at least R and G
card.colors & query_mask == query_mask

// c=rg — card is exactly RG
card.colors == query_mask

// c<=rg — card's colors are a subset of RG
card.colors & !query_mask == 0
```

Types and subtypes don't qualify: hundreds of distinct values (can't fit in a u64), and queries
only ever check one value at a time — `Vec<String>` linear scan over 1–3 items is faster than a
hash lookup anyway.

### Filter semantics

Each filter type is described below with a Python snippet showing the exact check logic (so this
doc is self-contained) and a note on the natural Rust equivalent. The Python snippets assume cards
are stored as plain dicts with the field names used in the DB — the extension's `Card` struct
replaces dict access with typed field access but the logic is identical.

#### Boolean composition

AND short-circuits on the first false child; OR short-circuits on the first true child. An empty
query produces a `TrueNode` that always passes.

```python
# AND
def check(card):
    for func in funcs:
        if not func(card): return False
    return True

# OR
def check(card):
    for func in funcs:
        if func(card): return True
    return False

# NOT
def check(card): return not inner_func(card)

# TrueNode
def check(card): return True
```

Rust: `children.iter().all(|c| c.matches(card))` / `.any(...)` / `!inner.matches(card)`.

#### Text

Four fields support substring (`:`) search. All are pre-lowercased at load time so the check is
a plain case-sensitive `in`. Set-code, layout, border, watermark, and collector-number colon
searches use exact match instead (those values have a fixed, predictable case).

```python
# name:baloth  — substring, pre-lowercased field
word = "baloth"
def check(card): return card["card_name_lower"] is not None and word in card["card_name_lower"]

# name="Lightning Bolt"  — exact, case-insensitive via re
import re
pattern = re.compile(r"^Lightning Bolt$", re.IGNORECASE)
def check(card):
    v = card["card_name"]
    return v is not None and bool(pattern.search(v))

# name:"lightning bolt"  — multi-word quoted: words must appear in order
pattern = re.compile(r"lightning.*bolt")   # applied to card_name_lower
def check(card):
    v = card["card_name_lower"]
    return v is not None and bool(pattern.search(v))

# o:/^{T}:/  — user regex, applied case-insensitively to oracle_text
pattern = re.compile(r"^{T}:", re.IGNORECASE)
def check(card):
    v = card["oracle_text"]
    return v is not None and bool(pattern.search(v))

# set:m10  — exact match on fixed-case field
def check(card): return card["card_set_code"] == "m10"
```

Rust: `card.card_name_lower.contains(word)`, `regex.is_match(&card.oracle_text)`, etc.

#### Numeric

Fields: `cmc`, `creature_power`, `creature_toughness`, `planeswalker_loyalty`, `edhrec_rank`,
`price_usd`, `price_eur`, `price_tix`, `card_rarity_int`, `collector_number_int`, `prefer_score`.

`None` always fails the comparison. The colon operator (`:`) is treated as `=` for numeric fields.
Rarity text is converted to int at query-parse time: `common=0 uncommon=1 rare=2 mythic=3`.

```python
import operator as op
fn = op.gt   # for cmc>3

def check(card):
    v = card["cmc"]
    return v is not None and fn(v, 3)
```

Rust: `card.cmc.map_or(false, |v| v > 3.0)`.

#### Arithmetic expressions (Sylvan Librarian extension)

This codebase extends Scryfall syntax to allow arithmetic between fields and constants on either
side of a comparison: `power+toughness>6`, `cmc+1<power`, `power*2>=toughness`.

Each side is evaluated to an `Optional[float]`; if either side is `None` the whole expression
fails. Arithmetic operators: `+`, `-`, `*`, `/`.

```python
# power+toughness>6
def check(card):
    p = card["creature_power"]
    t = card["creature_toughness"]
    return p is not None and t is not None and (p + t) > 6

# cmc+1<power  — RHS is also a field reference
def check(card):
    c = card["cmc"]
    p = card["creature_power"]
    return c is not None and p is not None and (c + 1) < p
```

Rust: evaluate each side to `Option<f32>` using `?` to propagate `None`, then compare. Integer
fields are widened to `f32` at evaluation time: `card.cmc.map(|v| v as f32)`.

#### Colors

Stored as a `u8` bitfield (W=1 U=2 B=4 R=8 G=16 C=32). The query value is also converted to a
mask at parse time. See the [bitfield section above](#why-bitfields-for-colors) for the
operator mapping. Applies to `card_colors`, `card_color_identity`, and `produced_mana`.

The RHS in the serialized query is a **plain list of uppercase color letter strings** — e.g.
`["R", "G"]` for `c:rg`. (The current Python code uses `{"R": True, "G": True}` dicts because
PostgreSQL JSONB containment operators require that shape; the extension uses lists instead.)
Rust converts the list to a bitmask at query-deserialize time.

```python
# c:rg  (contains at least R and G — query_keys is a frozenset of color codes)
query_keys = {"R", "G"}
def check(card):
    return query_keys <= (card["card_colors"] or {}).keys()

# c=rg  (exactly RG)
def check(card):
    return (card["card_colors"] or {}).keys() == query_keys

# c<=rg  (subset of RG)
def check(card):
    return (card["card_colors"] or {}).keys() <= query_keys
```

Rust bitfield equivalents: `card.colors & mask == mask` / `== mask` / `& !mask == 0`.

Devotion (`devotion:rr`) is not a color bitfield — it counts the number of pips of a given color
in the mana cost (see mana cost section below).

#### Types and subtypes

`t:creature` checks `card_types`; `t:merfolk` checks `card_subtypes`. The parser decides which
list to search: if the title-cased value is in the known set of supertypes and types (Artifact,
Battle, Creature, Enchantment, Instant, Kindred, Land, Planeswalker, Sorcery, …) it checks
`card_types`; anything else is a subtype and checks `card_subtypes`. The full sets are in
`api/parsing/db_info.py` (`CARD_SUPERTYPES`, `CARD_TYPES`).

The RHS in the serialized query is a **single-element list** — e.g. `["Merfolk"]` (title-cased).
The search syntax can only express one type value per filter; `t:merfolk t:wizard` becomes two
child nodes under an `AndNode`, not a multi-element list.

```python
# t:merfolk  (`:` / `>=` — card subtypes contains "Merfolk")
rhs_val = "Merfolk"
def check(card): return rhs_val in (card["card_subtypes"] or [])

# t=merfolk  (exactly one subtype, and it's Merfolk)
def check(card):
    v = card["card_subtypes"] or []
    return len(v) == 1 and v[0] == rhs_val

# t>merfolk  (proper superset: contains Merfolk plus at least one other)
def check(card):
    v = card["card_subtypes"] or []
    return rhs_val in v and len(v) > 1
```

Rust: `card.card_subtypes.contains(&rhs_val)` / `len == 1 && card.card_subtypes[0] == rhs_val`.

#### Keywords

The RHS is a **single-element list** with the normalized keyword name — e.g. `["Flying"]`.
`get_keywords_comparison_object` handles alias normalization at parse time.

```python
# keyword:flying
rhs = ["Flying"]   # normalized by get_keywords_comparison_object
def check(card):
    return rhs[0] in (card["card_keywords"] or {})
```

Rust: `card.card_keywords.iter().any(|k| k == "Flying")`.

#### Legality

The alias used in the query determines the expected value in the legalities dict:

| Query alias | Expected value |
|---|---|
| `format:`, `legal:` | `"legal"` |
| `banned:` | `"banned"` |
| `restricted:` | `"restricted"` |

Format name aliases are normalised at parse time (`"mod"` → `"modern"`, `"vin"` → `"vintage"`,
etc.) — see `get_legality_comparison_object` in `api/parsing/card_query_nodes.py` for the full
map.

The RHS is a **single-element list** with the normalized format name — e.g. `["legacy"]`. Rust
derives the expected legality value from `original_attribute` on the LHS `CardAttributeNode`
(same table as above: `"format"` / `"legal"` → `"legal"`, `"banned"` → `"banned"`, etc.).

```python
# format:legacy  (single key-value check — the most common case)
rhs = ["legacy"]
expected_val = "legal"   # derived from original_attribute == "format"
def check(card):
    cd = card["card_legalities"] or {}
    return cd.get(rhs[0]) == expected_val
```

Rust: `card.card_legalities.get("legacy").map_or(false, |v| v == "legal")`.

#### Oracle tags and is-tags

These are populated from the tag system in the DB, not from Scryfall bulk data.
The RHS is a **single-element list** — e.g. `["voltron"]`.

```python
# otag:voltron
rhs = ["voltron"]
def check(card): return rhs[0] in (card["card_oracle_tags"] or {})

# is:spell
rhs = ["spell"]
def check(card): return rhs[0] in (card["card_is_tags"] or {})
```

Rust: `card.card_oracle_tags.contains("voltron")` / `card.card_is_tags.contains("spell")`.

#### Exact name

The `!"Lightning Bolt"` syntax produces an `ExactNameNode` — case-insensitive full-name match,
no wildcards.

```python
# !"Lightning Bolt"
value_lower = "lightning bolt"
def check(card): return card["card_name"].lower() == value_lower
```

Rust: `card.card_name_lower == value_lower`.

#### Mana cost

The DB stores mana cost as a dict mapping each mana symbol to a list of pip indices, e.g.
`{R}{R}{G}` → `{"R": [1, 2], "G": [3]}`. The extension's `ManaCost` struct should store pip
counts per symbol and a total CMC:

```rust
struct ManaCost {
    pips: HashMap<String, u8>,  // symbol → count, e.g. "R" → 2 for {R}{R}
    cmc: f32,
}
```

```python
# m:rg / m>=rg  — card has at least the queried pips of each symbol
query_mana = {"R": [1], "G": [1]}  # from mana_cost_str_to_dict("{R}{G}")
query_cmc  = 2

def check(card):
    cm = card["mana_cost_jsonb"] or {}
    # card has >= as many pips of each queried symbol
    return (
        all(len(cm.get(sym, [])) >= len(pips) for sym, pips in query_mana.items())
        and (card["cmc"] or 0) >= query_cmc
    )

# m=rg  — exact match
def check(card):
    return card["mana_cost_jsonb"] == query_mana and card["cmc"] == query_cmc
```

Rust exact-match semantics: no extra symbols, every queried symbol's pip count equals the card's,
and CMC matches. (Python dict equality over pip index lists is equivalent but less transparent.)

```rust
// m=rg — exact match
fn matches_exact(card: &Card, query_pips: &HashMap<String, u8>, query_cmc: f32) -> bool {
    card.mana_cost.cmc == query_cmc
        && card.mana_cost.pips.len() == query_pips.len()
        && query_pips.iter().all(|(sym, &n)| card.mana_cost.pips.get(sym) == Some(&n))
}
```

`calculate_cmc` and `mana_cost_str_to_dict` in `api/parsing/card_query_nodes.py` and
`api/card_processing.py` are the reference for parsing mana cost strings like `{2}{R}{R}`.

Devotion (`devotion:rr`) uses the same pip-count structure but checks only that the card's mana
cost contains at least the specified pips — CMC is not checked.

#### Date and year

`released_at` is stored as `"YYYY-MM-DD"` — lexicographic string comparison works directly.

```python
# date>2020-01-01
def check(card):
    v = card["released_at"]
    return v is not None and str(v) > "2020-01-01"
```

`year:2019` stays as a **single `CardBinaryOperatorNode`** in the AST — the expansion to a date
range happens at evaluation time, not at parse time. Rust detects it via `original_attribute ==
"year"` on the LHS `CardAttributeNode` and applies the operator-dependent interval:

| operator | equivalent check |
|---|---|
| `:` / `=` | `YYYY-01-01 <= released_at < YYYY+1-01-01` |
| `>` | `released_at >= YYYY+1-01-01` |
| `<` | `released_at < YYYY-01-01` |
| `>=` | `released_at >= YYYY-01-01` |
| `<=` | `released_at < YYYY+1-01-01` |

```python
# year:2019  — single node, Rust expands using table above
def check(card):
    v = card["released_at"]
    return v is not None and "2019-01-01" <= str(v) < "2020-01-01"
```

#### Frame data

The RHS is a **single-element list** with the title-cased value — e.g. `["Extendedart"]`.
`get_frame_data_comparison_object` handles title-casing at parse time.

```python
# frame:extendedart
rhs = ["Extendedart"]
def check(card): return rhs[0] in (card["card_frame_data"] or {})
```

Rust: `card.card_frame_data.contains("Extendedart")`.

### Query serialization

`to_json()` and `kwargs` do not exist yet — implementing them on `nodes.py` and
`card_query_nodes.py` is part of this work (see effort estimate). Each node implements
`to_json() -> dict`. `engine.query()` calls it internally on the `filters` argument — callers
pass the parsed query object directly:

```python
result = engine.query(filters=parsed_query, ...)
```

The implementation is a one-liner on the base class — each node exposes a `kwargs` property that
returns its fields, recursively calling `to_json()` on any child nodes. A small helper removes the
need to remember which fields are nodes vs raw values:

```python
# base class (nodes.py)
def _node_to_json(obj):
    """Serialize obj if it's a QueryNode, otherwise return it as-is."""
    return obj.to_json() if isinstance(obj, QueryNode) else obj

def to_json(self) -> dict:
    return {"node_type": self.__class__.__name__, "kwargs": self.kwargs}

# Query delegates to its root node
class Query:
    def to_json(self) -> dict:
        return self.root.to_json()

# examples of per-class kwargs properties
class AndNode:
    @property
    def kwargs(self): return {"operands": [_node_to_json(op) for op in self.operands]}

class NotNode:
    @property
    def kwargs(self): return {"operand": _node_to_json(self.operand)}

class CardBinaryOperatorNode:
    @property
    def kwargs(self): return {"lhs": _node_to_json(self.lhs), "op": self.operator, "rhs": _node_to_json(self.rhs)}

class StringValueNode:
    @property
    def kwargs(self): return {"value": self.value}

class ExactNameNode:
    @property
    def kwargs(self): return {"value": self.value.lower()}  # pre-lowercased
```

Every node emits `{"node_type": "<ClassName>", "kwargs": {...}}`:

| Node type | kwargs |
|---|---|
| `TrueNode` | `{}` |
| `AndNode` | `{"operands": [<node>, ...]}` |
| `OrNode` | `{"operands": [<node>, ...]}` |
| `NotNode` | `{"operand": <node>}` |
| `ExactNameNode` | `{"value": "lightning bolt"}` (pre-lowercased) |
| `CardAttributeNode` | `{"attribute_name": "cmc", "original_attribute": "cmc"}` |
| `StringValueNode` | `{"value": "baloth"}` |
| `NumericValueNode` | `{"value": 3.0}` |
| `ManaValueNode` | `{"value": "{2}{R}{R}"}` |
| `RegexValueNode` | `{"value": "^{T}:"}` |
| `CardBinaryOperatorNode` | `{"lhs": <node>, "op": ">=", "rhs": <node or list>}` |

For collection-type filters (colors, types, subtypes, keywords, oracle tags, is-tags, frame data,
legality) the `rhs` field is a **plain Python list**, not a node — `_node_to_json` passes it
through unchanged. Examples: `["R", "G"]` for `c:rg`, `["Merfolk"]` for `t:merfolk`, `["legacy"]`
for `format:legacy`. The Rust deserializer inspects `attribute_name` on the LHS
`CardAttributeNode` to know how to interpret the list.

`CardAttributeNode` includes both `attribute_name` (the DB column name, e.g. `"card_legalities"`)
and `original_attribute` (the query alias, e.g. `"format"` or `"banned"`) — Rust uses the
original attribute to resolve the expected legality status.

`lhs` and `rhs` in `CardBinaryOperatorNode` are themselves fully serialized nodes. Arithmetic
expressions nest `CardBinaryOperatorNode` recursively — `cmc+1<power` serializes as:

```json
{
  "node_type": "CardBinaryOperatorNode",
  "kwargs": {
    "lhs": {
      "node_type": "CardBinaryOperatorNode",
      "kwargs": {
        "lhs": {"node_type": "CardAttributeNode", "kwargs": {"attribute_name": "cmc", "original_attribute": "cmc"}},
        "op": "+",
        "rhs": {"node_type": "NumericValueNode", "kwargs": {"value": 1.0}}
      }
    },
    "op": "<",
    "rhs": {"node_type": "CardAttributeNode", "kwargs": {"attribute_name": "creature_power", "original_attribute": "power"}}
  }
}
```

The Rust deserializer must handle arbitrary nesting depth. Evaluate each side to `Option<f32>`
and use `?` to propagate `None` when either side cannot be resolved.

### Sort, dedup, and limit

`filter()` handles sort, dedup, and limit inside Rust, mirroring `_search_in_process` in
`api/api_resource.py`. Read that function as the reference implementation before coding the Rust
side. The `unique`, `prefer`, `orderby`, and `direction` parameters from the interface map
directly to its logic.

### Rollout

1. Build the extension with a subset of filter types (numeric, text contains, color, type/subtype).
2. Run the extension in shadow mode — evaluate every query against both the PostgreSQL path and the
   extension, log any result-set differences (scryfall_ids in one set but not the other), but serve
   from SQL until parity is confirmed. The SQL path is the reference: it is production-validated and
   covers all query types including arithmetic expressions.
3. Cut over and retire the SQL search path for in-process queries.

The `to_filter_func()` Python implementation exists on `joe/python_filter` as an additional
reference for filter semantics but will not be merged to main. Use the profiling scripts in
`ignored/` to measure improvement once the extension is running.

## Effort estimate

| Phase | Estimate |
|---|---|
| Card struct + loader + PyO3 bindings | 2–3 days |
| Numeric, text, color, type/subtype filters | 2–3 days |
| Legality, keyword, tag, mana cost, regex, arithmetic expressions | 3–5 days |
| Sort + dedup + limit (replicating `_search_in_process` logic) | 1–2 days |
| Query JSON serialization (Python side) | 1 day |
| Parallel validation + cutover | 2–3 days |
| **Total** | **~2–3 weeks** |

## Expected outcome

Pure filter time for queries like `name:baloth` or `t:merfolk` drops from ~2–4ms to sub-millisecond.
Legality-heavy queries like `format:legacy` (currently ~9ms filter time) drop proportionally.
The result is a filter step that barely registers in latency, making total search time dominated
by HTTP overhead and JSON serialization rather than card evaluation.
