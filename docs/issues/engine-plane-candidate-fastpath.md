# Engine: plane/candidate fast path (skip eval_planes for small candidate sets)

Follow-on to #630/#634/#655 — surfaced during pre-merge review of those PRs, not
part of any of them.

## Problem

`run_query`'s plane/candidate composition (`lib.rs`) always calls `eval_planes`
when a plane is present, even when the co-occurring `narrow_rec` candidate set
(an ExactName lookup, a tag/keyword lookup, ...) is already known and tiny.
`eval_planes` costs a flat O(words_per_plane × tree size) regardless of how
few candidates are actually in play — for `c:g !"Lightning Bolt"`, the engine
still scans the entire green-card bitmap (~493 words) just to check one card.

## Design

`eval_plane_bit(expr, planes, cid) -> bool` answers the plane question for one
card directly, via a new `PlaneExpr::eval_bit` tree-walk that short-circuits
per candidate (`all`/`any` over children) rather than per-word: a non-green
card never touches the creature plane's word at all for `c:g AND t:creature`.
Below `PLANE_CANDIDATE_MAX` candidates, `run_query` checks each one this way
instead of materializing the whole bitmap; above it, unchanged (`eval_planes`
+ retain). `CardBits` candidates are untouched — they already get an O(words)
direct AND (#654) with no per-candidate cost either way.

**Two variants tried and dropped, in order:**

1. **Sort-and-group-by-shared-word.** Candidates within 64 ids of each other
   reuse one `eval_word` call instead of each recomputing it. Measured (real
   corpus kernel benchmark, even/random/clustered candidate distributions):
   never beat *both* the plain per-candidate check and `eval_planes` at the
   same candidate count — wherever grouping won against the plain check,
   `eval_planes` had already overtaken both. The sort overhead doesn't pay for
   itself against a genuinely scattered candidate set (the realistic case for
   most narrow_rec producers), and even where candidates do cluster, plain
   per-candidate checking already keeps up until `eval_planes` wins outright.
2. **Whole-word evaluation per candidate (`eval_word`, the initial cut).**
   Works, but wastes work on compound plane trees: computes the *entire*
   64-bit combined word (AND/OR of every referenced plane) just to read one
   bit, so a card that fails the first child still pays for every other
   child. Replaced by `eval_bit`, which short-circuits at the boolean level
   instead — measured 22-36% faster than the word-based check for `c:g
   t:creature` at realistic candidate counts (256-1024), with no change for
   single-plane expressions (nothing to short-circuit with one leaf).

## Calibration

An end-to-end Python-level sweep (`engine.query()` timing, varying candidate
count via a synthetic corpus) could not resolve the crossover at all —
downstream materialize/sort/paginate cost scales with match count regardless
of which branch wins, and swamps a sub-microsecond primitive-level effect
entirely. Switched to an isolated Rust kernel micro-benchmark instead
(`card_engine/src/bench_plane_candidate.rs`, real 31,508-card corpus,
`black_box`, best-of-200 timing — same pattern as `bench_mana.rs`/
`bench_verify_cost.rs`):

```
cargo test --release bench_plane_candidate -- --ignored --nocapture
```

Results (both plane-tree shapes, both an adversarial max-spread and a
realistic-random candidate distribution, consistent across repeated runs):
`eval_planes` costs a flat ~700-2,100 ns depending on tree size; `eval_bit`
scales per candidate and crosses that flat cost around 650-770 candidates.
`PLANE_CANDIDATE_MAX` is set to 384 — comfortably below the crossover (a
measured ~1.7-1.9x win there) with margin for noise and untested deeper
trees.

## Results

Full existing benchmark suite (`scripts/bench_permuted_order.py`, min-of-N ms,
machine quiesced — Docker Desktop's VM contaminates measurements even with no
containers running; `osascript -e 'quit app "Docker"'` fully stops it) plus
new `plane-candidate` configs targeting this path specifically: 47 queries,
**zero total mismatches** (correctness), mean delta **+0.84%** (range -1.8%
to +3.7%), indistinguishable from ordinary run-to-run noise. The new
plane-candidate rows (`c:g !"Llanowar Elves"` [1 candidate], `c:g
keyword:convoke` [26], `c:g keyword:kicker` [54], `c:g keyword:flying`
[208], `t:creature keyword:convoke` [44], plus a control at 545 candidates
— above the threshold) show 0.0% to +2.2%, indistinguishable from the
general noise floor — confirming no regression, and confirming (as expected
from the calibration) that the real effect is far too small to resolve at
end-to-end query-timing scale. The isolated kernel benchmark above is the
authoritative evidence for this change; the end-to-end suite's role here is
purely the regression/correctness check.

## Related

- #630 / #634 / #655 — the plane and candidate-narrowing machinery this
  composes
- #654 — the `Candidates::CardBits` direct-AND path, unaffected by this change
- [engine-permuted-bitmap-order-phase.md](engine-permuted-bitmap-order-phase.md)
