# Faster `unique=artwork` gather: skip repped groups, columnar gid, pre-sized scratch

Three changes to the `unique=artwork` grouping loop, which picks one best-`prefer_score`
representative per artwork group. Profiling `border:black -(name:…)` / artwork / usd (a broad
`GatheredScan`, since `usd` has no sort permutation) showed the dominant cost was **per-printing
residual verification** of the printing-varying `border` predicate — reading the wide `APrinting`
struct on every printing to find a black-bordered rep.

**Skip already-repped groups.** Printings are stored prefer-desc within a card, so for the default
prefer the *first* residual-qualifying printing of a group is its rep — every later printing of that
group is dead weight. The loop now reads the group id first and skips any printing whose group is
already repped, *before* the residual. Repped groups (the majority; ~2.4 printings/group) never
re-pay the residual, and the rep needs no score comparison. Custom prefer keeps the full max-score
scan (iteration order ≠ prefer order).

**Columnar `artwork_group_id`.** A pid-indexed `artwork_group_col: Vec<u16>` the gather reads for the
group id instead of the wide struct, so repped-group printings never touch the struct at all. Built
once in `reload_commit` beside `assign_artwork_groups` and archived, so it is never recomputed
post-load and cannot drift. Helps `all_match=true` artwork scans, where the gid read is the only
per-printing work.

**Pre-sized `group_best`.** The rep scratch is indexed only by `artwork_group_id` (Card mode collapses
to index 0), so its max index is a fixed store property — the largest distinct-artwork count of any
card (385 in the corpus; p50 1, p99 6). Stored as `CardIndexes.max_artwork_groups` and used to
pre-size the scratch once per query, dropping the per-printing `len() <= gid` bounds/resize check from
all three artwork grouping loops.

Measured on the 97,206-printing corpus (`limit=100`, min of a timed window), totals byte-identical:

| query | unique | orderby | before (μs) | after (μs) | speedup |
|---|---|---|---:|---:|---:|
| `border:black` | artwork | usd | 1166 | 845 | 1.38× |
| `t:creature` | artwork | usd | 320 | 244 | 1.31× |
| `c:r` | artwork | usd | 156 | 122 | 1.28× |
| `border:black -(name:storm or name:dragon)` | artwork | usd | 1543 | 1245 | 1.24× |

A 520-query branch-vs-main survey (`survey_queries.py --count 400 --wild 120 --seed 42`): **0
total-count mismatches, no regressions** (only sub-10 μs exact-name lookups vary, within noise); p100
1089 → 933 μs (−14%). Card/printing modes and the residual-dominated `-(name:…)` query are unaffected
where the loop isn't the bottleneck. Archive grows +190 KB (+0.27%): the columnar id array
(97,206 × 2 bytes). `ARCHIVE_FORMAT_VERSION` bumped for the layout change.

Design notes: [`docs/issues/local-engine-artwork-skip-repped.md`](../issues/local-engine-artwork-skip-repped.md)
and the [`APrinting` layout investigation](../issues/local-engine-aprinting-layout.md) it resolves —
which established (after a corrected profile) that the lever is the residual, not the struct footprint.
