# Watermark postings index

`watermark:` had no narrowing support anywhere in the engine — no postings, no plane, no
`narrow_rec` arm. Found investigating the single slowest query (1.068ms) in a 1,000-query
realistic-traffic survey: `watermark:set or artist:thomas` — `artist:thomas` alone narrows
tightly, but `Or` requires every child to narrow or the whole union bails, so one unindexed
sibling forced a full scan for both sides.

Real data (97,290 printings) shows watermark is sparse and long-tailed: 93.1% null, 67 distinct
values, largest (`wotc`) only 0.82% of all printings. Every value is far below this codebase's
plane-vs-postings crossover (~3,000 printings/value), so a postings index — the same
`HashMap<String, Vec<u32>>` shape as `set_codes` — is the only representation that fits. Added the
index plus the matching `narrow_rec` arm, mirroring `SetCode`'s exactly (`card_watermark_id` is
interned, so the build resolves through the string table instead of checking for an empty inline
string, the only structural difference).

`ARCHIVE_FORMAT_VERSION` bumped 20260723 → 20260724 (a second same-day bump — #737 already used
today's date for its own archive change).

Measured (97,206-printing corpus, `unique=card`, interleaved same build):

| query | before | after |
|---|---:|---:|
| `watermark:set or artist:thomas` | 1.068 ms | **0.116 ms** (9.2×) |
| `watermark:notarealvalue` (absent value) | — | **0.003 ms** |

Totals unchanged (correctness preserved). New Rust test `watermark_narrowing`; `cargo test`
(debug + release) 129/129; `test_engine_property.py` and `test_engine_unit.py` pass.

Design doc: `docs/issues/done/00739-engine-watermark-postings.md`.
