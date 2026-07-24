# Negated-range narrowing (`-usd<c`, `-cn<c`, `-date`/`-year`)

`NOT(x < c) == x >= c` is exact under this engine's null semantics — a NULL-valued printing fails a
direct comparison and its negation both, the same trivalent convention border/price/legality already
follow. `bare_range_bounds`, the single shared funnel every consumer of "is this a bare printing-range
comparison, what are its bounds" already goes through, now recognizes `Not` wrapping one of the four
ordered comparisons (`Lt/Le/Gt/Ge`) by flipping the op via the already-existing `negate_op` and
falling through to the same bounds computation — `is_printing_composable`, `compose_printing_estimate`,
`compose_printing_bits`, and `narrow_rec` each needed one new guard clause, no logic duplicated.

Two bugs found via the differential test for this, not by guessing — both cost regressions, not
correctness bugs, since residual verification caught what each one over-included or skipped:
`and_child_rank` blanket-classified every `Not(_)` as the cheapest-last "complement" rank, silently
dropping a negated range's narrowing contribution whenever `And`'s early-stop had already narrowed
via a sibling; and `tight_narrow_space` unconditionally claimed `DateCmp`/`YearCmp` were safe to
bit-complement despite `released_at` being nullable — the same trap `price` was already excluded
from, just never extended to dates, meaning `-year:1993`-shaped queries built and fully re-verified
an over-inclusive (NULL-dated-printing-including) candidate set via the pre-existing generic
complement path, rather than ever returning a wrong answer. Both fixed.

A third issue surfaced via benchmarking: a broad negated range (`-cn<100`, ~64% of printings) declined
to a full scan (regressing 0.545ms → 0.661ms) because the new arm passed the caller's own `broad_ok`
through, unlike the pre-existing generic Not-arm, which always forces `broad_ok: true` for its inner
check. Matching that choice fixed it.

A fourth issue found via code review, another cost-only mismatch: bug 1's fix for `and_child_rank`
reused the same overly-broad shape check (`Not(inner)` where `inner` merely *looks like* a
`NumericCmp`/`DateCmp`/`YearCmp`, regardless of field or op) rather than the actual dispatch condition
(`PriceUsd`/`CollectorNumberInt` with the four ordered ops, or `RarityInt` at any op) —
`and_child_rank` has no `indexes` to call `bare_range_bounds` directly and double-check the way the
other three consumers do. `-cmc:3` and negated-equality forms (`-usd:5`, `Ne` isn't a representable
range) were getting ranked as the cheap re-narrow tier when they actually run the generic
bit-complement or decline outright — never a wrong answer, just eager evaluation in the wrong order.
Fixed with a small `indexes`-free classifier mirroring the real dispatch, verified not to regress the
pre-existing `-r:x` ranking, plus a direct unit test asserting the rank values themselves (this class
of bug can't be caught any other way — it's invisible to correctness checks). A follow-up question
caught one more gap in the same fix: `-f:x`/`-banned:x`/`-restricted:x` (negated `Legality`, a tracked
format) has its own dedicated plane-read arm too — a third "not a complement" shape alongside `-r:x`
and the range arm, still missing from the classifier and still falling to the generic tier (predates
this PR entirely; bug 1 never reached it). Added the same way, plus two more unit test assertions.

Two rounds of the same class of bug in one PR was a signal, not a coincidence: the classifier and
`narrow_rec`'s own dedicated arms were independent implementations of "which shapes are cheap," kept
in sync only by a comment. Refactored to close the drift risk structurally instead: extracted
`is_rarity_negation_shape`/`is_legality_negation_shape` as the *single* implementation each shape
check has — `narrow_rec`'s own `-r:x`/`-f:x` arms now gate on these functions directly (replacing
their inline `matches!` guards), and `and_child_rank` calls the same two. The range case can't go
fully index-free the same way (`resolve_numeric_range_leaf` genuinely needs `indexes`), so
`and_child_rank` now takes `indexes: &Archived<CardIndexes>` and calls `bare_range_bounds` directly —
the same function `narrow_rec`'s own `Not` handling and the three other compose-path consumers
already call, rather than keeping a second reimplementation around to drift again. (Must be called on
the `Not` node itself, not the unwrapped inner — `bare_range_bounds`'s `Not` arm is what applies
`negate_op` before checking representability.) Caught one more latent issue while unifying the rarity
predicate: the inline check being replaced matched any `rhs` for a `RarityInt` `lhs` (should require
`Const`, matching the real arm) — never exercised in practice, tightened while merging the two copies
into one. A fourth dedicated `Not` arm added to `narrow_rec` in the future can no longer drift from
`and_child_rank` silently.

Measured (97,206-printing corpus): `-usd<0.25 usd<5` (the survey's #1 slowest query before this fix)
0.901ms → 0.165ms (edhrec, 5.5×), 0.933ms → 0.353ms (rarity, 2.6×); `-usd<50` 0.896ms → 0.080ms
(11.2×); `-cn<100` back to baseline after the `broad_ok` fix. `total` parity held everywhere; broad
survey shows the motivating query no longer in the top 10 slowest, no new regressions.

New Rust tests `negated_range_narrowing` and `and_child_rank_matches_narrow_rec_dispatch`. `cargo
test` (debug + release) 131/131; `test_engine_property.py` + `test_engine_unit.py` 158/158; clippy
unchanged from baseline.

Design doc: `docs/issues/local-engine-negated-range-narrowing.md`.
