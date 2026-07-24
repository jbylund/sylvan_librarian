# Engine: Exact-Postings Fields (`set:`/`watermark:`) as Compose Leaves

**Status: implemented**, filed as [#746](https://github.com/jbylund/sylvan_librarian/issues/746).
Filed investigating why `-set:dmu year:2023`
(0.451ms, printing/edhrec) costs noticeably more than `year:2023` alone (0.268ms) despite excluding
only 2 of its 9,234 matches. A step 4 for
[#731](00731-engine-compose-universal-evaluator.md)'s leaf-source table, alongside step 1 (range
leaves, shipped) and the sibling work this session added:
[done/00740-engine-compose-permutation-fallback.md](done/00740-engine-compose-permutation-fallback.md)
and [done/00741-engine-negated-range-narrowing.md](done/00741-engine-negated-range-narrowing.md).

## The finding

`year:2023` alone qualifies for `PrintingRangeScan` (`bare_range_bounds(filter, indexes).is_some()`
for the *whole* filter) — a non-materializing binary-search plan, effectively free. `-set:dmu
year:2023` is an `And`, so `bare_range_bounds` returns `None` for the compound as a whole regardless
of how cheap one side is; `PrintingRangeScan` is inapplicable and the query falls to a materializing
plan. There, `narrow_rec`'s `And` arm ([lib.rs:3338](../../card_engine/src/lib.rs#L3338)) narrows via
`year:2023` (rank 1) to 9,234 candidates, then correctly skips `-set:dmu`'s narrowing contribution
(rank 2, "complements only pay as sole source") — `-set:dmu` becomes a per-candidate residual check
instead. Per the engine's own measured cost model (`MASK_COMPARE_NS100` ≈ 4ns/candidate,
filter.rs:427-445), that residual check alone should cost ~37μs for 9,234 candidates — the observed
gap is ~190μs, so most of it is the materializing path's general overhead (allocation, sort-key
computation, accumulator bookkeeping), not the predicate itself.

## The idea

`is_printing_composable`/`compose_printing_bits` ([lib.rs:4393](../../card_engine/src/lib.rs#L4393),
[lib.rs:4551](../../card_engine/src/lib.rs#L4551)) already turn border, rarity, legality, and
range leaves into exact printing-space bitmaps, composed with `AND`/`OR` — #731's model. `set:`/
`watermark:` aren't in that table at all today: they're backed by a plain postings `TagIndex`
(`indexes.set_codes`/`indexes.watermarks`, a `HashMap<String, Vec<u32>>` of sorted printing ids), no
plane, no leaf-bits function, positive or negated.

Adding one is cheap and reuses an existing primitive: `scatter_bits`
([lib.rs:2406](../../card_engine/src/lib.rs#L2406)) already does exactly this for border's
non-plane postings fallback (`border_leaf_bits`, lib.rs:4443). A `set:dmu` leaf is
`scatter_bits(indexes.set_codes["dmu"], n_printings)` — the same shape, just keyed off a different
index.

**The negated form is the interesting part, and it's cheaper than the range case.** Negating a range
can flip to a large second range (`-cn<100` ⇒ `cn>=100`, ~64% of printings — why `broad_ok` mattered
in the negated-range-narrowing work). Negating a small exact-postings set doesn't have that problem:
`-set:dmu` is "start from all-ones, clear these 436 bits" — cost rides the *positive* postings size
(436) regardless of polarity, never the complement (96,770). That's a strictly cheaper shape than
anything currently in the compose table, and it's exactly why the generic `Not`-arm (which requires a
tight child and pays for a full complement) isn't the right tool here — a dedicated leaf-bits
function sidesteps it the same way `range_leaf_bits` already does for ranges.

## Estimated cost

Using the engine's own calibrated constant (`RANGE_SCATTER_PER_PRINTING_NS = 0.36ns`, cost.rs:174 —
measured for scattering an index/postings slice into a bitmap, so it's the right rate to reuse here):

| step | count | cost |
|---|---:|---:|
| scatter `year:2023` into a bitmap | 9,234 | ~3.3μs |
| clear `set:dmu`'s postings from it | 436 | ~0.16μs |
| popcount the result | ~1,519 words | low single-digit μs |

Total materialization: roughly 5-10μs, against the current path's ~190μs gap over `year:2023` alone
— potentially close to an order of magnitude on this shape. Not measured yet; this is a napkin
estimate from calibrated per-op constants, not a benchmark.

## Measured (`scripts/bench_tag_postings_compose.py`, 97,206-printing corpus, min ms)

| query | unique | orderby | total | before (μs) | after (μs) | change |
|---|---|---|---:|---:|---:|---|
| `-set:dmu year:2023` (motivating) | printing | edhrec | 9,232 | 451 | 57 | **7.9×** |
| `-set:dmu year:2023` | card | edhrec | 4,325 | 266 | 80 | **3.3×** |
| `-set:dmu year:2023` | printing | rarity | 9,232 | 471 | 126 | **3.7×** |
| `-set:dmu year:2023` | artwork | edhrec | 5,577 | 457 | 86 | **5.3×** |
| `-set:dmu` (bare — was the generic complement) | printing | edhrec | 96,770 | 1,077 | 44 | **24.5×** |
| `set:dmu year:2023` (positive, selective) | printing | edhrec | 2 | 37 | 47 | 0.79× |
| `set:dmu year:2023` | card | edhrec | 1 | 36 | 38 | flat |
| `year:2023` (range control) | printing | edhrec | 9,234 | 268 | 271 | flat |
| `set:dmu` (bare) | printing | edhrec | 436 | 86 | 82 | flat |
| `set:dmu r:mythic` | printing | edhrec | 49 | 35 | 35 | flat |
| `-set:dmu border:black` (broad) | card | edhrec | 31,055 | 629 | 595 | flat |
| `watermark:wotc` (bare) | printing | edhrec | 797 | 110 | 104 | flat |
| `watermark:wotc year:2023` | printing | edhrec | 22 | 76 | 77 | flat |
| `border:black` (control) | card | rarity | 31,169 | 398 | 346 | flat |
| `t:creature` (control) | card | edhrec | 17,317 | 66 | 62 | flat |
| `usd<50` (control) | card | rarity | 31,217 | 447 | 417 | flat |
| `f:modern` (control) | card | edhrec | 22,264 | 63 | 61 | flat |

`total` parity held on every row. Geometric mean over the five clearly-improved rows: **6.6×**.

The one regression is the positive selective intersection `set:dmu year:2023` (printing), +10μs at
the ~40μs floor: the compound now qualifies for `PrintingCompose`, so it builds the full composed
printing bitmap instead of narrowing to `set:dmu`'s 436 postings and residual-checking the year.
This is the same tradeoff step 1 (range leaves) already makes for any selective positive
intersection — the shape becoming composable is exactly the point, and the negated form it's paired
with wins 8×. The broad realistic-traffic survey (`scripts/survey_queries.py`, 520 queries, seed 42)
shows the real `set:` traffic in it (`set:stx`, `set:ltr`, `set:otj`) improving and no new slow
pattern; nothing outside this shape moves beyond the noise floor.

`-set:dmu border:black` (broad, 31k matches, card mode) stays flat rather than improving — a broad
card-space result is roughly as expensive to project up from the composed bitmap as it is to narrow,
so the planner sees no clear win either way. The gains are on shapes where a small, exact leaf can
gate a materializing path down to a compose one, not on ones already dominated by their broad side.

## Testing

- New Rust test `set_watermark_compose_leaves` (`tests.rs`): the differential check — for both `set:`
  polarities, `watermark:` positive, and mixes with a year range (`And`/`Or`), the exact
  `compose_printing_bits` bitmap must agree printing-for-printing with the general per-candidate
  residual-check path (`card_pass` + `residual_matches`), on a store whose set codes straddle card
  boundaries (so `set:`/`-set:` is genuinely printing-dependent) and whose watermark is nullable.
  Also asserts `compose_printing_estimate` is a valid match-count upper bound (exact for bare
  leaves), that `-watermark:` (nullable) is **not** composable, and that a `-watermark:` inside an
  `And` keeps the whole `And` off the compose path.
- `cargo test` (debug + release): 132/132 passed.
- `pytest api/tests/test_engine_property.py api/tests/test_engine_unit.py`: 158/158 passed.
- `cargo clippy`: no new warnings (the ones at `lib.rs:4773`/`4797`/`4798` predate this change).

## Scope shipped

`set:` (positive and negated) and `watermark:`'s **positive** form. Negated `watermark:` is
deliberately not a compose leaf: `watermark` is nullable, so "all-ones minus postings" would wrongly
count no-watermark printings as matching `-watermark:x` — it stays on the general path (its
`tight_narrow_space` classifier already declines it) until an explicit "has any watermark"
known-mask is built. See the correctness caveat below.

## Correctness caveat — must not apply uniformly to both fields

`#731`'s own caveat applies here directly: **`NOT` over a nullable field needs a "known" mask** — a
null-valued printing satisfies neither the direct predicate nor its negation (the same trivalent trap
`tight_narrow_space` had for `DateCmp`/`YearCmp`, fixed this session in
[done/00741-engine-negated-range-narrowing.md](done/00741-engine-negated-range-narrowing.md)). `set_code` has
no null case (every printing belongs to exactly one set) — "all-ones minus postings" is exact.
`watermark` **is** nullable (`card_watermark_id != NONE_STR` gates the postings build in
`reload_commit`, see [00739-engine-watermark-postings.md](done/00739-engine-watermark-postings.md)) —
"all-ones minus postings" would wrongly count no-watermark printings as matching `-watermark:x`. The
negated compose leaf should therefore cover `set:` only, or `watermark:` needs an explicit
known-mask subtraction (a third small postings-derived bitmap of "has any watermark") before its
complement is safe — the same shape `tight_narrow_space`'s bug taught, don't re-learn it here.

## Scope

No new `PhysicalPlan` variant — this slots entirely into the existing `PrintingCompose` plan. Its
applicability (`printing_compose_applicable`), cost (`plan_cost`'s `PrintingCompose` arm, already
priced off `RANGE_SCATTER_PER_PRINTING_NS`), and execution (`printing_compose_fastpath` /
`gather_composed_page` / `walk_grouped_page`) all key off `is_printing_composable` /
`compose_printing_bits` / `compose_printing_estimate` — the three functions this widens. The planner
doesn't gain a new choice to reason about; `PrintingCompose` just becomes applicable to more filter
shapes, the same way step 1 (range leaves) widened it without adding a plan.

Add `TextExact{SetCode, Eq}` (and its `Not`, per the caveat above) to those three functions, mirroring
`border`'s arms. Since `is_printing_composable` already recurses through `And`/`Or`, this generalizes
immediately to any mix with ranges/border/rarity/legality — no new per-combination logic. Measure with
a targeted
`scripts/bench_*.py` before/after per the performance PR workflow, using `-set:dmu year:2023` as the
motivating query and `set:dmu`/`year:2023` alone as controls, before touching `watermark:`'s
known-mask question.

## Related

- [#731](00731-engine-compose-universal-evaluator.md) — the parent plan; this is a step-4 leaf kind
  its table doesn't yet enumerate.
- [done/00740-engine-compose-permutation-fallback.md](done/00740-engine-compose-permutation-fallback.md) —
  sibling fix from the same broad-survey investigation.
- [done/00741-engine-negated-range-narrowing.md](done/00741-engine-negated-range-narrowing.md) — where the
  nullable-field `NOT` trap was found and fixed for dates; the same discipline applies to watermark
  here.
- [done/00739-engine-watermark-postings.md](done/00739-engine-watermark-postings.md) — the
  `set_codes`/`watermarks` `TagIndex` this reuses.
