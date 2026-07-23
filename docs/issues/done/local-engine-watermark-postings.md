# Engine: Watermark Postings Index

**Status: done.** No GitHub issue filed — small, mechanical, found while investigating the
slowest query in a broad realistic-traffic survey.

## Why

`watermark:` had no narrowing support anywhere in the engine — no postings list, no plane, no
`narrow_rec` arm at all. Any query touching it fell straight to a full unnarrowed scan
(`TextExact{field: Watermark, ...}` hit `narrow_rec`'s final `_ => None`). Worse, `Or` requires
*every* child to narrow or the whole union bails (`lib.rs`'s `FilterExpr::Or` arm, `?` on each
child) — so `watermark:set or artist:thomas` forced a full scan even though `artist:thomas` alone
narrows tightly and cheaply. It was the single slowest query (1.068ms) in a 1,000-query survey
(`benchmarks/survey/survey-2026-07-23-broad.csv`), despite `watermark:` appearing in only 10 of
those 1,000 queries.

## Data shape (blue DB, 97,290 printings)

| stat | value |
|---|---:|
| non-null | 6,753 (6.9%) |
| distinct values | 67 |
| largest value (`wotc`) | 797 (0.82% of all printings) |
| 2nd largest (`set`) | 572 (0.59%) |
| tail | half the values ≤60 printings; ~15 are singletons |

Every value is far below the plane-vs-postings crossover this codebase uses elsewhere
(~3,000 printings/value, e.g. border's doc). A plane would cost a fixed ~12KB per tracked value
for a field that's absent 93% of the time; postings cost `4 bytes × 6,753 nonnull printings` ≈
27KB total, across all 67 values combined. Not a close call — postings is the only representation
that fits this shape.

## What shipped

Postings index (`watermarks: TagIndex`, same `HashMap<String, Vec<u32>>` shape as `set_codes`),
built the same way (`lib.rs` `CardIndexes` literal), plus the matching `narrow_rec` arm
(`TextField::Watermark, CmpOp::Eq` → `Narrowed::tight(Candidates::Printings(...))`), mirroring
`SetCode`'s arm exactly. `card_watermark_id` is interned (`NONE_STR` sentinel for absent), unlike
`card_set_code` (an inline `String`), so the build resolves through `strings` instead of checking
for an empty string — the only structural difference from `SetCode`'s block.

Archive-format bump: `ARCHIVE_FORMAT_VERSION` 20260723 → **20260724** (not just the next calendar
day — #737 already used 20260723 for its own archive change earlier the same day, so this needed a
distinct value to invalidate stores built under that layout).

## Measured

Interleaved, same build, 97,206-printing corpus, `unique=card`, `orderby=toughness` (matching the
survey config that surfaced this):

| query | before | after |
|---|---:|---:|
| `watermark:set or artist:thomas` | 1.068 ms | **0.116 ms** (9.2×) |
| `watermark:set` (bare) | — | 0.071 ms |
| `artist:thomas` (bare) | — | 0.084 ms |
| `watermark:wotc` (most common value) | — | 0.094 ms |
| `watermark:notarealvalue` (absent value) | — | **0.003 ms** |

Totals unchanged (`watermark:set or artist:thomas` = 744 both before and after) — same rows, just
narrowed instead of scanned. The absent-value case confirms the short-circuit: a postings miss
returns an empty *tight* candidate set, which `prepare_candidates` propagates as a known-empty
(not "unnarrowed") candidate list, so the query returns `total=0` in microseconds with no scan and
no residual verification — same as an absent set code today.

## Testing

- New Rust test `watermark_narrowing` (`tests.rs`): bare-value narrowing, absent-value empty
  narrowing, and the `Or`-composes-now regression case directly (the bug this fixes).
- `cargo test` (debug + release): 129/129 passed.
- `pytest api/tests/test_engine_property.py` (differential suite vs. reference oracle) and
  `api/tests/test_engine_unit.py`: all passed.

## Related

- Found via [`scripts/survey_queries.py`](../../scripts/survey_queries.py)'s broad realistic-traffic
  survey.
- [00664 border planes](00664-engine-border-planes.md) — the plane-vs-postings crossover this
  reasons from, for a field dense enough to go the other way.
