# Format legality search: exact status vs "any legal" / "any not legal"

## Problem

Every format legality lookup today is an **exact status match**. `format:vintage` requires
`card_legalities->>'vintage' = 'legal'`, so restricted cards don't match — but on Scryfall,
`f:vintage` returns Black Lotus. There is also no way to express the two useful disjunctions:

- **"any legal"** — playable in the format: `legal` OR `restricted`
- **"any not legal"** — not playable: `banned` OR `not_legal`

Scryfall data has exactly four statuses per format: `legal`, `not_legal`, `banned`, `restricted`.

## Current behavior (both paths agree)

- **Parser** ([card_query_nodes.py](../../api/parsing/card_query_nodes.py) `legality_str_to_comparison`):
  `format:` / `f:` / `legal:` → status `legal`; `banned:` → `banned`; `restricted:` → `restricted`.
  Single-letter format aliases resolve via `FORMAT_CODE_TO_NAME` in
  [db_info.py](../../api/parsing/db_info.py) (`f:m` → `modern`).
- **SQL path**: JSONB containment against `{format: status}` — exact match.
- **Rust engine** ([lib.rs](../../card_engine/src/lib.rs)): legalities are packed 2 bits per
  format into a `u64` (`not_legal=0`, `legal=1`, `restricted=2`, `banned=3`; bit positions
  assigned by a global append-only registry at reload). The filter is an exact 2-bit compare.

## Proposed semantics

| Query keyword | Matches statuses | Note |
|---|---|---|
| `format:X` / `f:X` | `legal`, `restricted` | Scryfall parity ("playable") |
| `legal:X` | `legal` | exact |
| `restricted:X` | `restricted` | exact |
| `banned:X` | `banned` | exact |
| `not_legal:X` (new) | `not_legal` | exact; includes formats absent from the card's map |
| `unplayable:X` (new, name TBD) | `banned`, `not_legal` | complement of `format:X` |

`unplayable:X` is expressible as `-f:X` once `f:` means playable, so it's optional sugar —
decide whether it earns a keyword.

## Implementation sketch

Both paths must change together or engine and SQL results diverge.

- **SQL**: exact match stays containment; the disjunctions become
  `card_legalities->>%(fmt)s IN ('legal','restricted')` (or `NOT IN`, minding cards where the
  key is absent — `->>'x' IS NULL` should count as `not_legal`).
- **Engine**: re-pick the 2-bit codes so each disjunction is a single-bit test:
  `not_legal=00`, `legal=01`, `banned=10`, `restricted=11` — then **bit 0 = playable**
  (`legal|restricted`) and its complement is "any not legal", while exact matches remain
  2-bit compares. The filter gains a mask so one expression covers both:
  `(bits >> shift) & mask == expected` with `mask=0b01` for playability, `0b11` for exact.
- **Parser**: `legality_str_to_comparison` needs to emit status *sets* rather than a single
  status, and the AST node must carry that through `to_json()` for the engine and through
  SQL generation.

## Behavior changes to call out

- `format:vintage` grows by ~650 restricted printings (Scryfall parity — arguably a bug fix).
- Formats using `restricted` in current data: vintage (650 printings), oldschool (135),
  duel (117), timeless (57), tlr (45). Everything else is unaffected.
- Explanation strings ("it's legal in vintage") should say "playable in" for the disjunction.

## Related

- [00490-rust-filter-extension.md](done/00490-rust-filter-extension.md) — the engine this lands in.
- [local-query-benchmark-suite.md](./local-query-benchmark-suite.md) — `format:legacy` is the slowest
  engine query; the bitmap repack above is what made legality checks cheap.
