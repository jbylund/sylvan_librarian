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

Measured (97,206-printing corpus): `-usd<0.25 usd<5` (the survey's #1 slowest query before this fix)
0.901ms → 0.165ms (edhrec, 5.5×), 0.933ms → 0.353ms (rarity, 2.6×); `-usd<50` 0.896ms → 0.080ms
(11.2×); `-cn<100` back to baseline after the `broad_ok` fix. `total` parity held everywhere; broad
survey shows the motivating query no longer in the top 10 slowest, no new regressions.

New Rust test `negated_range_narrowing`. `cargo test` (debug + release) 129/129;
`test_engine_property.py` + `test_engine_unit.py` 158/158; clippy unchanged from baseline.

Design doc: `docs/issues/local-engine-negated-range-narrowing.md`.
