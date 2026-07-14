# Detect Anchoring Independent of Literal-ness in regex_tier

Follow-up from measuring the verifier cost tiers
(docs/changelog/2026-07-09-measured-verify-cost-tiers.md).

## The gap

`regex_tier()` (filter.rs) only treats a pattern as cheap when it's a pure
literal with a `^`/`$` anchor — any pattern with live regex metacharacters
falls to `REGEX_MACHINERY_NS100`, regardless of whether it's also anchored.

`bench_verify_cost.rs` measured an anchored non-literal pattern (`^[aeiou]`,
a character class right after `^`, no exploitable literal prefix) at ~17.7
ns/candidate — cheaper than both bare-literal and general machinery (~45 ns
each), and even cheaper than a plain `TextContains` scan (~22 ns). Anchoring
bounds the regex engine to one starting position regardless of what's being
matched there; it doesn't require the anchored content to be a literal.

## Why not fixed alongside the recalibration

The cost-tier recalibration replaced ordinal ranks with measured constants —
a numbers-only change. Teaching `regex_tier()` to detect "anchored, whatever
follows" is a classification-logic change: it needs its own measurement
matrix (anchored class vs. anchored alternation vs. anchored `.` vs. multiple
anchors) rather than the one shape this issue happened to measure, and its
own correctness check (an anchor followed by something CAN still scan more
than one byte, e.g. `^a.*b` — cheap only if the engine's own anchor-bounded
matcher stays fast for that case; not obviously true from one data point).

## Where to hook

`regex_tier()`'s existing `anchored_start`/`anchored_end` detection already
exists — the change is what those flags do when the loop *also* hits a
metacharacter (currently: return `REGEX_MACHINERY_NS100` unconditionally).
Bench candidates: `^[aeiou]`, `^(a|b)`, `^a.*b`, `.*z$` (end-anchored,
non-literal) against the same real corpus and `bench_verify_cost.rs` harness.

## Expected effect

Bounded: only patterns that are both anchored *and* contain metacharacters
benefit, and regex conjuncts are already the wild-corpus tail (per the
original verifier-cost-ordering issue's honest scoping). Insurance more than
a geomean move — same shape as the original anchored-literal discovery in
#648, just for the non-literal case.
