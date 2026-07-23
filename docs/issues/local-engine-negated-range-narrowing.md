# Engine: Negated-Range Narrowing (`-usd<c`, `-cn<c`, `-date`/`-year`)

**Status: done.** No GitHub issue filed. Found investigating `-usd<0.25 usd<5`, the slowest query
in a broad realistic-traffic survey (1.131ms) after the watermark and compose-permutation-fallback
fixes landed.

## The idea

`NOT(x < c) == x >= c` — and this isn't an approximation, it's exact under this engine's null
semantics. A NULL-valued printing fails a direct comparison (`x < c`) *and* fails its negation
(`-x < c`) both — the same trivalent convention every nullable field in this engine already follows
(border, price, legality). So a negated ordered comparison isn't a case requiring the generic
bit-complement machinery (which needs a *tight* child and can't safely handle nullable fields,
docs on `tight_narrow_space`) — it's just the flipped comparison, computed directly, no complement
at all. This holds for `Lt↔Ge` and `Le↔Gt`; `Eq↔Ne` doesn't reduce further since `Ne` isn't a single
half-open range (that's fine — `int_range_bounds`/`date_range_bounds`/`year_range_bounds` already
return `None` for `Ne`, so a negated equality declines on its own, no special-casing needed).

## Where it lives: one shared function, four small call sites

`bare_range_bounds` (`lib.rs`) was already the single funnel every consumer of "is this filter a
bare printing-range comparison, and what are its bounds" goes through — `is_printing_composable`,
`compose_printing_estimate`, `compose_printing_bits`, and `narrow_rec`'s own range arms all call it
rather than each having their own field/op dispatch. Teaching *that one function* to also recognize
`Not(NumericCmp{Lt/Le/Gt/Ge})`/`Not(DateCmp)`/`Not(YearCmp)` — flip the op via the already-existing
`negate_op` (used since the `-r:x` rarity case), then fall through to the exact same bounds
computation as the direct case — means every consumer gets the negated shape for free. Each of the
four call sites needed exactly one new guard clause (`Not(inner) if matches!(inner.as_ref(), ...)`),
zero new logic duplicated. `negate_op` was already verified against `filter.rs`'s actual `tri()`
null-short-circuit (see its doc comment) — the same correctness guarantee this reuses, not a new
one.

This was a deliberate choice over doing the equivalent rewrite in Python (`api/parsing/rewrite.py`,
the seam `#734`'s `lower_literal_regexes` uses): unlike that case, Postgres already treats
`NOT (price_usd < 25)` and `price_usd >= 25` identically (native three-valued `NULL` semantics), so
the SQL side gets nothing from receiving the rewritten form — only the Rust engine's own
composability/narrowing decisions actually need the equivalence, and `bare_range_bounds` already
being a shared funnel made a small, contained Rust change cheaper than routing through a second
parser layer for no cross-consumer benefit.

## Two bugs found and fixed along the way

Both surfaced by the differential test for this feature, not guessed at up front:

1. **`and_child_rank` blanket-classified every `Not(_)` as rank 2** ("complement, broad by
   construction, useful only as sole source"). That's right for the *generic* Not-complement, but
   wrong for a negated range/rarity comparison, which is a cheap, exact re-narrow — same cost as its
   un-negated form. The `And` arm's early-stop (`AND_SKIP_THRESHOLD`) was silently dropping the
   negated child's narrowing contribution whenever a sibling had already narrowed first. Not a
   correctness bug (residual verification still catches whatever narrowing skips), but a real,
   avoidable cost regression for any `-usd<c`/`-r:x` mixed into an `And`. Fixed by delegating to the
   inner shape's own rank instead of hardcoding 2.
2. **`tight_narrow_space` unconditionally claimed `DateCmp`/`YearCmp` were safe to bit-complement**
   (`Some(true)`, no null check) — but `released_at` is nullable, and the generic Not-arm's
   complement doesn't exclude NULL-dated printings the way `bare_range_bounds`'s direct approach
   does. This is the *same* trap `price` was already excluded from years ago (see the comment right
   above it), just never extended to dates. Like bug 1, this was **not a correctness bug** — the
   over-inclusive complement it builds is always marked `Narrowed::loose`, and
   `narrow_candidates_exact`'s exactness check (`all_match_known`) reads that concrete `.tight` field,
   not this classifier, so residual `card_pass` verification still ran and dropped every NULL-dated
   printing before any total/page was returned. The real cost was purely wasted work: `-year:1993`
   (or any negated `DateCmp`/`YearCmp` equality) built and then fully re-verified an unnecessarily
   broad candidate set, via the pre-existing generic complement path — nothing about *this* feature
   introduced it, my test's `Ne` case just exercised a corner the existing suite hadn't covered
   before. Fixed by excluding `DateCmp`/`YearCmp` from `tight_narrow_space`, mirroring `price`'s
   exclusion exactly. The four ordered ops don't lose anything (they now narrow through the new,
   correct arm instead, with no wasted verification); the previously-mislabeled `Eq`/`Ne` negation is
   the only shape affected, and it now correctly declines up front instead of paying for a doomed
   complement.

## A third issue found via benchmarking, not testing

A broad negated range (`-cn<100` ≡ `cn>=100`, ~64% of printings) initially *regressed* relative to
the pre-this-PR baseline (0.545ms → 0.661ms) even though it now "narrows." Root cause: my new
`narrow_rec` arm passed the caller's own `broad_ok` through to `range_narrowed`, so a broad flipped
range simply declined (falling to a full scan) exactly like a broad *positive* range would. But the
pre-existing generic Not-arm always forces `broad_ok: true` for its inner check — a deliberate
choice (negating a predicate is exactly the shape where the flipped bounds are worth computing even
when broad, since there's no cheaper alternative once committed to the field). Matching that same
choice for the new arm (force `true` unconditionally, not the caller's `broad_ok`) fixed it —
`-cn<100` now matches or slightly beats the original baseline instead of regressing.

## A fourth issue found via code review: `and_child_rank`'s guard was still too broad

Bug 1's fix (delegate to the inner shape's own rank instead of hardcoding 2) used the same
overly-broad shape check the rest of this PR had to tighten elsewhere: `Not(inner) if
matches!(inner.as_ref(), NumericCmp{..}|DateCmp{..}|YearCmp{..})`. That fires for *any* field and
*any* op, but the real dispatch in `narrow_rec`/`bare_range_bounds` only takes the cheap path for
`PriceUsd`/`CollectorNumberInt` with the four ordered ops (`Lt`/`Le`/`Gt`/`Ge`), or `RarityInt` at any
op (its own dedicated `-r:x` arm). Everything else — `-cmc:c`/`-power:c`/`-toughness:c` (card-space,
any op) and negated equality on price/cn/date/year (`Ne` isn't a representable range) — actually
resolves via the generic bit-complement arm, or declines outright. `and_child_rank` has no `indexes`
parameter, so unlike `is_printing_composable`/`compose_printing_bits`/`compose_printing_estimate` (all
of which call `bare_range_bounds(filter, indexes)` directly to double-check), it couldn't reuse that
function and instead re-approximated its shape check — too loosely.

Concretely: `-cmc:3` inside an `And` got ranked as if it were the cheap re-narrow path (tier 0, via
the recursive `and_child_rank(inner)` call landing on `NumericCmp`'s "not price/cn" fallback branch),
when execution actually runs the generic complement (a real but comparatively costly card-space
bit-complement) — same rank/execution mismatch as bugs 1 and 2, just a *narrower* recurrence of bug 1
that its own fix didn't fully close. Not a correctness bug (same reasoning as bugs 1 and 2: residual
verification catches whatever the mismatch skips or wastes) — the `And` arm would still evaluate
these children, just in the wrong order relative to their actual cost.

Fixed by extracting `not_child_is_cheap_renarrow` — a small, `indexes`-free field/op classifier
mirroring `bare_range_bounds`'s `Not` arm plus the pre-existing `-r:x` carve-out (verified this
doesn't regress `-r:x`'s own ranking, which the old, broader guard got right only by accident) — and
using it as the guard instead of the bare shape `matches!`. Caught in review, not by the differential
test: rank/execution mismatches are a cost question, not a correctness one, so nothing about a wrong
final answer would ever surface it. Added a direct unit test (`and_child_rank_matches_narrow_rec_dispatch`)
asserting the ranks this class of bug can't be caught any other way.

## Measured (`scripts/bench_negated_range_narrowing.py`, 97,206-printing corpus, min ms)

| query | unique | orderby | before | after | change |
|---|---|---|---:|---:|---|
| `-usd<0.25 usd<5` (motivating example) | card | edhrec | 0.901 | 0.165 | **5.5×** |
| `-usd<0.25 usd<5` | card | rarity | 0.933 | 0.353 | **2.6×** |
| `-usd<0.25 usd<5` | printing | rarity | 1.479 | 0.400 | **3.7×** |
| `-usd<0.25 usd<5` | artwork | rarity | 1.244 | 0.574 | **2.2×** |
| `usd>=0.25 usd<5` (equivalent direct form, control) | card | rarity | 0.362 | 0.354 | flat |
| `-usd<50` (bare, selective) | card | rarity | 0.896 | 0.080 | **11.2×** |
| `-cn<100` (bare, broad — the regression found and fixed) | card | rarity | 0.545 | 0.537 | flat (was 0.661 before the `broad_ok` fix) |
| `-year>2020` (bare) | card | rarity | 0.498 | 0.366 | **1.4×** |
| `usd<50` (control) | card | rarity | 0.420 | 0.406 | flat |
| `border:black` (control) | card | rarity | 0.355 | 0.341 | flat |
| `t:creature` (control) | card | edhrec | 0.063 | 0.061 | flat |

`total` parity held on every row across every run. The broad realistic-traffic survey
(`scripts/survey_queries.py`, 1000 queries): `-usd<0.25 usd<5` — the survey's #1 slowest query
before this fix — no longer appears in the top 10; no new slow patterns introduced.

## Testing

- New Rust test `negated_range_narrowing`: bare `-usd<c` narrowing (including the NULL-price
  exclusion, checked on both the direct and negated forms), the motivating `-usd<0.25 usd<5`
  compound checked end-to-end via `run_query` (not `narrow_candidates` alone — a corpus this small
  hits `AND_SKIP_THRESHOLD`'s early-stop, which is a separate, correct, pre-existing optimization,
  not something this feature needed to defeat), `is_printing_composable`/`compose_printing_bits`
  agreement, `-cn<100`, and the `-year:1993`/`-date>=c` cases (including confirming the `Ne`-shaped
  negation correctly declines rather than computing a wrong answer).
- New Rust test `and_child_rank_matches_narrow_rec_dispatch` (bug 4): directly asserts the rank
  values for `-usd<c` (matches its un-negated form), `-usd:c`/`-cmc:c` (must fall to the generic
  tier, not the cheap one), and `-r:x` (must keep its pre-existing cheap rank at any op) — the only
  way to cover a rank/execution mismatch, since it can't be observed through a correctness check.
- `cargo test` (debug + release): 131/131 passed.
- `pytest api/tests/test_engine_property.py api/tests/test_engine_unit.py`: 158/158 passed.
- `cargo clippy`: unchanged from baseline (42 warnings).

## Related

- [local-engine-compose-permutation-fallback.md](local-engine-compose-permutation-fallback.md) —
  the sibling investigation this branches from; both found while chasing the same broad-survey
  slow-query list.
- [done/local-engine-watermark-postings.md](done/local-engine-watermark-postings.md) — the first
  fix in this same investigation thread.
