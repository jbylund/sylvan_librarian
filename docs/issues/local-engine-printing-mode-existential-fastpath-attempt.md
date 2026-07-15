# Engine: unique=printing/artwork existential fastpath — investigated, both fixes reverted

Status: investigated and reverted 2026-07-14, no GitHub issue — extracted from
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md), which shipped
`PlaneExpr::PrintingRangeBits` for `usd`/`unique=card` and then asked whether the same idea helps
`unique=printing`/`artwork` too. It doesn't, for a reason inherent to what those modes need — see
below.

## Why these modes stayed slow

`run_query`'s speed for `unique=card` doesn't come from a smaller candidate set — for `usd<50`
(83% selective) the narrowed set gets discarded by the broadness-guard regardless of mode. The
real mechanism is `run_query_streamed_popcount`: it answers `total` directly from a bitmap's
popcount and walks only `limit` set bits via a permuted-bitmap word-skip, never materializing or
iterating a candidate list. That function is hard-gated to `Mode::Card` (`lib.rs:3941`), and
extending it to `unique=printing`/`artwork` is **issue #656**, still open — quoting its own scope
note, it needs "a weighted set-bit walk... card weights are O(1) via offsets-diff/artwork_groups...
Not implemented." The original Legality carve-out (`docs/issues/done/00667-...md`) scoped this out
identically, stating outright that those two modes "need no change: they already... run the
existing (correct) per-printing `card_pass` walk."

**#656 as scoped doesn't actually cover this case.** Its weighted-walk sketch assumes "under
`all_match`" — every printing of a matching card counts, an O(1) weight lookup (`offsets`-diff or
`artwork_groups`). For an *existential* fact like price, the weight is "how many printings/groups
actually satisfy the predicate," which cannot be read from a count table — it requires visiting
that card's printings, the same O(matching printings) work `card_match_count` already does. There
is no way around this for an exact total: unlike `unique=card` (needs only existence, O(1) via the
bitmap), `unique=printing`/`artwork` inherently need to know *which* printings qualify.

## Two fixes implemented and empirically verified, instead of the full weighted-walk mechanism

1. **`split_planes` narrowing-only fold**: when an existential leaf can't safely fold to a bare
   `True` residual (wrong mode), it now still returns the compiled plane for `candidate_cards`
   narrowing (avoiding the broadness-discard-to-full-scan fallback `plane=None` takes), while
   leaving the residual unchanged so per-printing correctness still comes from the ordinary
   `card_pass` walk, not the plane. `run_query`'s own `existential_plane` (row-selection) gate
   stays `Mode::Card`-only, deliberately — using it here too would redundantly re-check the same
   predicate the residual already checks. Verified via debug instrumentation: `candidate_cards`
   for `usd<0.1`/`unique=printing` narrows correctly from 31,508 to 4,738 cards.
2. **Weighted permuted-bitmap walk** in `run_query_streamed`'s "large totals" branch, replacing a
   raw `perm.iter()` scan of all `n_cards` entries with a bitmap-scatter-and-`trailing_zeros` walk
   (mirroring `run_query_streamed_popcount`'s mechanism, but reading `counts[cid]` as a
   *variable* per-card weight instead of a uniform 1, since whole words can't be skipped by
   popcount alone when weights vary). This is the real, generic fix for the adversarial shape
   `#634`'s own design worried about (`released_at>2026-06-01 order by released_at`-style: low
   selectivity, order-by uncorrelated with or opposed to the predicate, forcing a walk deep into
   the permutation) — verified correct via the full test suite (particularly
   `fuzz_row_identity_matches_reference`'s 96-seed property fuzzer) and via direct timing
   instrumentation.

**Why neither moved the `usd<50`-shaped benchmark numbers**: instrumented both phases directly.
For `usd<50`/`usd<0.1` under `unique=printing`, the *match* phase (visiting narrowed candidates,
checking their printings) costs 200μs–1.1ms and dominates completely; the walk/emission phase
costs low single-digit microseconds either way, old or new, because `limit`-based early
termination already made the *old* raw-permutation scan cheap for these specific queries (dense,
early matches in `edhrec_rank` order) — its O(n_cards) worst case was real but not hit here. The
weighted-walk fix's value is real but narrower than hoped: it matters for deep pagination /
adversarial ordering, not for the broad, offset-0 queries this whole project has benchmarked. The
match-phase cost is the true, inherent floor for these modes under an existential predicate, and
no bitmap/walk cleverness removes it — only narrowing the candidate set (fix 1, above) helps, and
only in proportion to selectivity (a small win at `usd<50`'s 83%, a bigger one at low selectivity).

**Real-corpus benchmark, full sweep, both fixes applied**: `unique=printing`/`artwork` unchanged
from baseline (~1.2ms) across `usd<0.1` through `usd<50`. `unique=card` unaffected by either fix
(still ~0.18ms). Both fixes passed 118/118 tests with no new clippy warnings at the time — but see
below.

## What broader testing found (both fixes reverted)

Asked "do these fixes help any other queries?" — a reasonable question, since both fixes are
field-agnostic (any existential leaf, not just price). Benchmarking existing legality/rarity/
border queries under `unique=printing`/`artwork` (`f:commander`, `f:legacy`, `f:modern`,
`r:common or r:uncommon`, `-border:black`) — none of which this investigation had tested, since
all prior benchmarking used only `usd<X` — found real problems in both fixes:

- **Fix 2 (weighted walk): a real, reproducible ~10-18% regression** on every one of those
  queries, confirmed via clean back-to-back A/B against a `git worktree` build of the pre-fix
  commit. Cause: the walk's bitmap-scatter step is O(n_cards) *unconditionally*, paid even when
  the old raw-permutation walk would have terminated early (the common case for broad predicates
  in default `edhrec_rank` order — confirmed by directly timing a simplified reproduction of the
  old walk logic, which took ~1.3μs for the exact queries this whole project benchmarked,
  nowhere near its O(n_cards) worst case). Net: pays an unconditional cost to fix a worst case
  that essentially never triggers for these query shapes.
- **Fix 1 (narrowing-only fold): its own regression on near-total matches** (`f:commander` at
  99.8%) for the same reason — `candidate_cards`'s `Some(expr)` branch has no broadness-discard
  check, unlike the `plane=None` branch, so it unconditionally pays `bitmap_card_ids` materialization
  for a match rate where there's nothing to narrow.
- **Attempting to fix *that* regression (adding a discard check to the `Some(expr)` branch)
  introduced a real correctness bug**, caught by `fuzz_row_identity_matches_reference`: for a
  compound `And` where `split_planes` partitions one child into the plane and leaves another in
  the residual (e.g. `rarity>0 AND types!=X`, with `types!=X` folded into the plane and `rarity>0`
  left as the residual), discarding the plane-derived `candidate_cards` silently drops the
  plane's constraint entirely — nothing else re-checks it, since the residual only knows about
  `rarity>0`. This is different from fix 1's own narrowing-only scenario (residual = the *same*
  predicate the plane represents, so discarding and falling back to the residual is safe) — the
  two cases reach the same code path and are not distinguishable there without deeper surgery.
  Confirmed the bug was newly introduced (not pre-existing) by reproducing it against a
  `git worktree` build of the pre-fix commit, where the fuzzer passed cleanly.

Given fix 2 provides no measured benefit for any tested query and a real regression on several,
and fix 1 provides no measured benefit for its own target case (`usd<X`) and a real regression on
near-total matches with no safe way found to fix that within this investigation's scope, both were
reverted in full — confirmed byte-identical to the pre-investigation commit
(`git diff <that-commit> -- lib.rs planes.rs tests.rs` is empty) and re-verified clean (118/118
tests, no new clippy warnings, benchmarks back to baseline for both the `usd<X` sweep and the
legality/rarity/border queries that exposed the problems).

**Lesson carried forward**: validate performance changes against the engine's existing, varied
query shapes, not only the query that motivated the change — the same lesson `#667`'s own
"performance regression caught by the broad survey, not the targeted script" section already
recorded, re-learned here at a different layer (candidate narrowing and pagination walks, not
per-candidate dispatch). Re-applied one more time, at a third layer, extending
`PrintingRangeBits` to `collector_number`/`released_at` — see
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md#collector_numberreleased_at-extension-and-a-second-broad-testing-lesson).

## Related

- [local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) — the parent
  project this was extracted from; ships the `unique=card` win this doc's investigation couldn't
  extend further.
- Closing this gap for real would need a different approach than #656's own scoped mechanism,
  which doesn't cover existential facts — not attempted here.
