# Engine: card-level narrowing planes for border:

Status: proposed, not started. GitHub: #664. Follows
[docs/workflows/performance-pr-workflow.md](../workflows/performance-pr-workflow.md).

## Measured problem

`border:` queries sit near the top of the slow tail in the broad survey (`scripts/survey_queries.py`,
400 generated + 120 wild, seed 42, `benchmarks/bitplanes/corpus.jsonl`, 97,206 printings): median
0.390ms, p90 0.808ms, max 1.187ms. `border:` has no index at all today.

## Where the cost is

`TextField::Border` has no narrowing arm in `narrow_rec` at all — every query touching it is a full
per-printing residual scan, regardless of how selective the value is.

## Proposed approach: three loose, card-level narrowing planes

Real corpus distribution (`benchmarks/bitplanes/corpus.jsonl`, n=97,206 printings, 31,508 distinct
cards):

| border | printings | % printings | cards w/ >=1 such printing | % cards |
|---|---|---|---|---|
| black | 85,046 | 87.49% | 31,169 | 98.92% |
| borderless | 5,701 | 5.86% | 3,381 | 10.73% |
| white | 5,131 | 5.28% | 2,059 | 6.53% |
| gold | 1,238 | 1.27% | 551 | 1.75% |
| yellow | 90 | 0.09% | 85 | 0.27% |

Three card-space bitplanes — `has_black_printing`, `has_borderless_printing`, `has_white_printing`
— each computed once at build time as an OR over a card's printing range
(`offsets[card]..offsets[card+1]`), same per-card build loop `build_bit_planes` already runs. Gold
and yellow (1,238 + 90 printings, 551 + 85 cards) get no dedicated plane — "none of the three bits
set" is not treated as "is gold/yellow," just as "the narrowing declined," so those two values fall
straight through to today's full scan, unindexed, same as now. `border:black` alone is expected to
decline too: 98.92% of cards have a black printing, past `narrow_candidates_exact`'s existing
broadness guard (keep only if the narrowed set is <=75% of the domain) — no special-casing needed,
the existing guard already does the right thing.

**These bits are `Narrowed::loose` candidates only, never `compile_plane`-consumable.** They
narrow which *cards* are worth walking the printings of at all; the actual per-printing residual
check (`card_pass`) always still runs to decide the real answer, for every `unique` mode.

### Why not tight (rejected: printing-space plane fed into `compile_plane`)

`compile_plane`'s existing exactness guarantee (the thing #634's residual-skip promotion relies on)
is safe today only because every dimension it touches — colors, color identity, type, devotion — is
card-invariant: true or false identically for every printing of a card, so there's never a
"which printing satisfies this" question. Border varies per printing, so it fails that invariant.

Concretely: `unique=card` semantics require a *single* printing to satisfy the *whole* filter, not
each predicate independently satisfied by *some* (possibly different) printing. `border:black
border:borderless` must return zero matches (no printing is both) — but two independent per-card
"has X" bits, ANDed, would each be true for a card that has one of each, a false positive. Treating
the bits as tight would silently violate the codebase's existing "same-space tight sets stay tight
under And/Or" assumption the moment two printing-varying tight leaves are combined. There's no
local way to detect when that's safe (a solo `border:black` leaf is fine in isolation; the trap only
bites once a *second* printing-varying predicate — another `border:`, or `frame:`/`set:`/etc. —
appears anywhere in the enclosing subtree, which is a non-local fact `compile_plane`'s bottom-up
per-node composition has no mechanism to check). Consistently, this also means `-border:black`
(`Not`, which only narrows through tight children) cannot use the plane either, and falls back to
the existing full scan for negated border queries — a known, accepted limitation, not an oversight.
(Precedent: `price_usd` range narrowing is already deliberately loose for an unrelated reason —
f32/f64 rounding — and is already non-invertible via `Not` today; this isn't a novel gap.)

Measured (production corpus): `-border:black` is *not* even the complement of `has_black_printing`
in this engine's actual semantics — `unique=card`'s existential quantification applies to the whole
predicate, so `-border:black` means "has some printing that isn't black" (5,463 cards, 17.3%), not
"has zero black printings" (339 cards, 1.08%). Even a hypothetical safe-to-invert design couldn't
answer it from these three bits alone anyway, since a card whose only printings are gold/yellow
(untracked) would need to count as matching but shows false on all three — another reason this
stays out of scope rather than a narrower "just needs Not support" gap.

Also rejected: a `TagIndex` posting list per border value (can't do the "bits absent = other" trick
— absence from two posting lists can't distinguish "black" from "gold").

## Acceptance

1. Baseline on `main` (targeted script + `scripts/survey_queries.py`, same corpus/seed as above,
   reused from #663 — `ENGINE_COLUMNS` unaffected). Also capture a memory baseline
   (`--features alloc-counter`, `QueryEngine.mem_stats()`) since this adds new `CardIndexes` fields.
2. Targeted configs: `border:black` / `border:borderless` / `border:white` alone; each combined
   with a card-invariant sibling (`border:black type:creature`); **`border:black border:borderless`
   as an explicit correctness case — must return the same (empty) result as today**, proving loose
   narrowing + residual verify doesn't reintroduce the false-positive bug the tight design was
   rejected for; `border:gold` / `border:yellow` (expected to stay fully residual); a card with
   printings across multiple border colors; `-border:black` (expected to stay fully residual, not a
   regression — declines the same as today); controls unrelated to `border:`.
3. Differential test asserting `border:black border:borderless` (and similar multi-printing-varying
   combos) still returns correct (empty, or whatever brute-force says) results — the regression test
   for the specific bug this design exists to avoid reintroducing.
4. Total-row-count parity on every benchmark config throughout.
5. Re-measure and iterate (targeted + broad + memory) until no regressions remain.
6. Queries expected to improve: `border:black|borderless|white`, alone or nested, any `unique` mode
   — except `border:black` alone (declines, per the broadness guard, no change expected).
   `border:gold`/`border:yellow`/any negated `border:` predicate: expected unchanged, not a miss.

## Results

No regression-chasing needed this time — the design held up on the first implementation, which is
the payoff of catching the shared-witness trap in conversation before writing any code rather than
discovering it via a failing benchmark or (worse) a silent wrong-answer bug.

Targeted (`scripts/bench_border_planes.py`, `benchmarks/bitplanes/corpus.jsonl`, 17 configs,
`--window 8.0` — re-run at a longer window than the first pass specifically to drive down noise on
the "must not regress" categories; n ranges ~8k-250k iterations/config):

| config | before | after | change |
|---|---|---|---|
| `border:white type:creature` | 0.5626ms | 0.1014ms | 5.55x |
| `border:white` (unique=artwork) | 0.9386ms | 0.2305ms | 4.07x |
| `border:white` (unique=card) | 0.8636ms | 0.1571ms | 5.50x |
| `border:borderless` (unique=card) | 0.7615ms | 0.1991ms | 3.82x |
| `border:borderless` (unique=printing) | 0.7611ms | 0.2318ms | 3.28x |
| `border:borderless c:g` | 0.2627ms | 0.0881ms | 2.98x |
| `border:black border:borderless` (correctness canary) | 1.3411ms, total=0 | 0.3034ms, total=0 | 4.42x, parity held |
| `border:white border:black` (correctness canary, printing) | 1.0761ms, total=0 | 0.1991ms, total=0 | 5.40x, parity held |
| `border:black` (declines, per design) | 0.4134ms | 0.4031ms | 1.03x |
| `border:gold` (unindexed) | 0.8238ms | 0.8091ms | 1.02x |
| `border:yellow` (unindexed) | 0.8056ms | 0.7887ms | 1.02x |
| `-border:black` (negation) | 1.0220ms | 1.0255ms | 1.00x |
| `-border:white` (negation) | 0.4777ms | 0.4880ms | 0.98x |
| `name:soldier` (control) | 0.0312ms | 0.0322ms | 0.97x |
| `cmc>6` (control) | 0.0610ms | 0.0630ms | 0.97x |
| `oracle:creature` (control) | 0.1306ms | 0.1317ms | 0.99x |
| `t:creature c:g` (control) | 0.0635ms | 0.0626ms | 1.01x |

Geometric mean across all 17 configs: **1.98x**. The "must not regress" categories
(`border:black` alone, `gold`/`yellow`, negation, controls) tightened to a 0.97-1.03x spread at
this sample size (vs. 0.94-1.11x at a shorter first-pass window) — confirming they're genuinely
flat, not just probably-noise.

Broad survey (`scripts/survey_queries.py`, 400 generated + 120 wild, seed 42): overall geomean
**1.00x** (flat — most of the 520 queries don't touch `border:` at all, as expected), border-tagged
geomean **1.24x faster** (7 border-tagged queries in this seed's sample; the handful of >10%
deltas elsewhere in the full survey are all unrelated query shapes with sub-100µs absolute
differences, consistent with measurement noise, not a real effect).

Memory: `+11.56 KiB` total archive growth (+0.017%) — exactly `3 planes × 493 words × 8 bytes` for
31,508 cards, matching the design's own sizing with no surprises. `reload_peak` unchanged.

Total-row-count parity held on every config throughout, including the shared-witness correctness
canary (`border:black border:borderless` → 0 matches on both builds).

## Related

- #664 — the filed issue this doc mirrors and expands.
- #663 (engine-oracle-word-index) — established the benchmark protocol and the kernel-benchmark /
  memory-measurement additions to `docs/workflows/performance-pr-workflow.md` this doc follows.
