# Engine: legality divergent-card carve-out for all_match promotion

Status: implemented, including the row-selection fix (see "Row selection for `unique=card`" below),
benchmarked, pending PR merge. GitHub: #667.
Follows [docs/workflows/performance-pr-workflow.md](../workflows/performance-pr-workflow.md).
Follow-on to #634 (Steps 1/2, shipped in #658), flagged in that issue's own comment thread.

## Measured problem

`FilterExpr::Legality` (`f:`/`format:`) never reaches `all_match` promotion or the #634 Step 2
popcount-skip order phase, however selective the rest of the filter is. `compile_plane` has no
`Legality` arm at all — it falls through the `_ => None` catch-all (`planes.rs`) — so `split_planes`
can never fully consume a filter containing a `Legality` node to `FilterExpr::True`, which is
Step 2's precondition. Concretely: `format:modern id:g t:creature` — `id:`/`t:` already compile to
exact planes, but the `format:modern` child alone keeps the whole filter off Step 2, forcing
per-candidate `card_pass` regardless of how selective `id:g`/`t:creature` already are.

## Why the plane can't just be "legal" today

`legal_x` (#654) is built as "legal AND not divergent": exact for ~98.2% of cards, but the ~556
divergent-legality cards (their printings disagree on status) are unconditionally excluded from the
bit regardless of true status, since one card-level bit can't represent per-printing disagreement.
`compile_plane`'s contract is "two-valued card-level nodes only" (`planes.rs:636-641`), and
`Legality`'s `tri()` genuinely returns `Tri::PrintingDep` for divergent cards (`filter.rs:1223-1237`)
— that's why it's excluded today.

## First design (repair-based), tried and abandoned — see the general write-up

A first pass built a repair mechanism: keep the single "legal and not divergent" plane, and for the
~556 divergent cards specifically, re-derive and overwrite the correct bit on the evaluated bitmap
before anything reads it. This worked, was fully tested, and its broad-survey regressions were
tracked down and fixed (a redundant-lookup bug, then a residual-leftover extraction-gate bug) — but
a much cleaner design was found before landing it: see the analysis below. The repair mechanism, the
extraction-gate principle, the shared-witness invariant, and the cardinality-guard problem it ran
into are real, generalizable engineering, captured separately for the next field that doesn't have
this issue's escape hatch:
[docs/issues/reference-engine-printing-varying-plane-repair-pattern.md](../reference-engine-printing-varying-plane-repair-pattern.md).

## Second design: two exact planes per format, no repair, no runtime tax

**The escape hatch legality has and a hypothetical unbounded field wouldn't**: legality's entire
query space is finite and known at build time — `expected == LEGALITY_LEGAL`, for one of ~22
formats. That means *both* existence projections can be precomputed exactly, once, at build time:

- `legal_exists(F)` = `∃p: legal_F(p)` — does this card have any printing legal in format `F`.
- `illegal_exists(F)` = `∃p: ¬legal_F(p)` — does this card have any printing *not* legal in `F`.

These are genuinely different facts for a divergent card (both can be true at once — that's what
"the printings disagree" means), so `-format:F` needs its own plane, not a bit-complement of the
first. Once both exist, `format:F` and `-format:F` are **exact for every card, divergent or not**,
with **zero runtime cost beyond the existing `eval_planes` word-sweep** — no repair, no divergent
postings lookup, no cardinality guard, because there's nothing left to correct.

**Storage, checked before assuming it's cheap**: `∃p: legal_F(p)` / `∃p: ¬legal_F(p)` cardinality
per format, real corpus (`benchmarks/bitplanes/corpus.jsonl`, 31,508 cards):

| format | legal_exists | illegal_exists |
|---|---|---|
| commander | 99.8% (31,451) | **0.2% (57)** |
| legacy | 99.7% (31,421) | 0.3% (87) |
| vintage | 99.8% (31,439) | 0.2% (69) |
| modern | 70.7% (22,264) | 29.3% (9,244) |
| pioneer | 46.4% (14,630) | 53.6% (16,878) |
| oldschool | **3.1% (961)** | 98.7% (31,083) |

The spread (0.2% to 99.8%) initially suggested a density-thresholded plane-vs-postings choice per
format/polarity, mirroring #628/#639/#671 — **checked and rejected**: `eval_planes` costs O(words),
not O(popcount), so a plane for a 57-card sparse set costs exactly the same to evaluate as one for a
31,451-card dense set. The postings-vs-plane tradeoff only pays off when storage itself is expensive
(subtypes/keywords: ~1,500 distinct values, a plane per value would be megabytes) or when a
postings union's materialization cost scales with result size (the exact problem this redesign
eliminates). Neither applies at 22 formats × 2 polarities × ~3.9KB ≈ 172KB total, trivial against a
68MB archive. **Simpler design: both planes, unconditionally, every format, no threshold logic.**

### Compiling `Legality` and `Not(Legality)`

- `Legality{shift: Some(s), expected: LEGALITY_LEGAL}` → `PlaneExpr::Plane(legal_exists_plane(s))`.
- `Not(Legality{shift: Some(s), expected: LEGALITY_LEGAL})` → `PlaneExpr::Plane(illegal_exists_plane(s))`
  directly — **not** `PlaneExpr::Not(Plane(legal_exists))`, which would compute
  `¬(∃p: legal(p))` = `∀p: ¬legal(p)` (wrong — a divergent card with both a legal and an illegal
  printing satisfies `illegal_exists` but not `∀p: ¬legal(p)`).
- `Not` wrapping something *more complex* than a bare `Legality` leaf (`-(format:X AND t:creature)`)
  needs De Morgan pushed down at **compile time** so the `Not` lands directly on each leaf: a new
  `compile_plane_neg` function, mutually recursive with `compile_plane`, implementing
  `Not(And(cs))` → `Or(compile_plane_neg(c) for c in cs)` and `Not(Or(cs))` → `And(...)` , `Not(Not(x))`
  → `compile_plane(x)`, `Not(True)` → `Const(false)`, and falling back to "compile positive, wrap in
  `PlaneExpr::Not`" for leaves that don't need special handling (colors/types/devotion — genuinely
  two-valued, safe to blindly complement, same as today). `contains_unnegatable_numeric`'s existing
  guard (declining `Not` entirely when a null-valued numeric field like power/toughness is anywhere
  inside) is unchanged and checked first — this redesign doesn't touch that, only adds Legality's
  own case. Cost: a tiny-tree, one-time-per-query bind-time rewrite, not a per-candidate one.

### The shared-witness case that still needs a decline, and why it doesn't need the repair toolkit

`format:A AND format:B` (two *distinct* formats ANDed) still can't be answered by ANDing two
independent existence-projection planes — `∃p: legal_A(p) ∧ legal_B(p)` isn't
`(∃p: legal_A(p)) ∧ (∃p: legal_B(p))` regardless of how exactly each side is computed; a card with
different witness printings for each satisfies the right side without satisfying the left. Same
issue reached via De Morgan: `¬(format:A OR format:B)` becomes `And(illegal_A, illegal_B)`, which has
the identical exposure. Even `format:A AND -format:A` (same format, both polarities) is affected — a
divergent-on-A card satisfies both `legal_exists_A` and `illegal_exists_A` independently, even
though no single printing can be both legal and not-legal in the same format at once (the true
answer is always false).

Rather than building the shared-witness-safe per-printing joint-evaluation machinery from the first
design for this narrow case, **just decline**: before assembling any `PlaneExpr::And` that would
result from either a direct `And` or a De-Morgan'd `Not(Or(...))`, count how many *distinct*
`(format, polarity)` pairs are referenced anywhere among the legality-plane leaves being combined. If
more than one, decline the whole compile (return `None`), falling back to today's
`legal_candidate_bits`-based narrowing for that shape. `Or` never has this problem (`∃` distributes
over `∨`), so no check is needed there. This is a real scope cut, not a workaround: nobody
realistically writes `format:A AND format:B`, and the fallback is exactly as correct (if not as fast)
as before this issue existed.

Polarity has to be part of the key, not folded away: a first implementation deduplicated by format
alone, on the theory that "both polarities count as touching that format" — which does correctly
decline `format:A AND format:B`, but **fails to decline `format:A AND -format:A`**, since that's only
one *format* even though it's two distinct existence facts (`legal_exists_A` and `illegal_exists_A`,
independently true for a divergent card, even though no single printing can be both legal and
not-legal in A at once — the true answer is always false). Caught by a test
(`legality_and_of_two_formats_declines_but_or_compiles`) that checked this exact shape; fixed by
keying `collect_legality_formats` on `(format, polarity)` instead of `format` alone. A literal
duplicate leaf (`format:A AND format:A`) still collapses to one entry and composes fine — same
underlying fact checked twice, not two facts needing a shared witness.

### Mode-aware `all_match`: the `unique=printing`/`artwork` correctness hole

`compile_plane`'s existing contract — and the `all_match_known` fast path it feeds (#634 Step 1) —
was built exclusively for **card-invariant** fields (colors, types, cmc, power/toughness, devotion,
border): the same value on every printing of a card, so "the card matches" trivially implies "every
printing matches," which is exactly what `all_match=true` tells `push_card_matches`/`card_match_count`
to assume for `unique=printing`/`artwork` (skip per-printing verification, return every printing in
range). Legality does not have that property — `legal_exists(F)` is `∃p`, not `∀p` — so naively giving
it the same treatment is a real correctness bug: a card divergent on modern legality would, under
`unique=printing`, incorrectly return its *illegal* printings too. Caught by a test
(`legality_plane_promotion_respects_mode_through_split_planes`) run through the actual
`split_planes`/`run_query` pipeline, not just direct `narrow_rec` calls — the direct-call tests alone
did not exercise this, since `#634`'s Step 2 popcount path is already (and remains) hard-gated to
`Mode::Card` for unrelated reasons, masking the hole from anyone benchmarking Step 2 specifically.

Rarity (#670) has the identical exposure (per-printing-varying, existence-projected) and never ran
into this because it was deliberately kept **out of** `compile_plane` entirely — narrow_rec-only,
always `loose`. Legality's escape hatch (finite query space, both projections precomputable) is what
makes reaching `all_match` possible at all here, but that only helps `unique=card`, where existence is
exactly the semantics wanted (same as Step 2). The fix has two parts:

- `plane_expr_is_existential(&PlaneExpr)` (`planes.rs`) — walks a compiled plane expression and
  reports whether any leaf reads a legality plane (`PLANE_LEGAL_EXISTS`/`PLANE_LEGAL_ILLEGAL`).
- `split_planes` takes a new `unique_is_card: bool` and declines to fold a leaf/child touching
  legality into the plane-consumed-to-`True` outcome whenever it's false — both at the top-level
  whole-tree shortcut and in the `And` arm's per-child fold. This has to happen at the *source*
  (`split_planes`), not by patching `all_match_known` after the fact in `run_query`: once a filter is
  collapsed to a bare `FilterExpr::True` residual, there is no way to recover *which* printing
  actually matches — the information is gone, not just mis-flagged. `run_query` additionally keeps a
  `plane_expr_is_existential`-based check on its own (`plane_true_for_mode`) as defense-in-depth for
  any caller that builds a `(filter, plane)` pair without going through `split_planes` (tests do this
  directly). Every other, card-invariant plane ignores `unique_is_card` entirely and reaches
  `all_match` exactly as before, for every mode.

### Row selection for `unique=card`: a second correctness hole, found in review

Fixing the mode-aware hole above is not the whole fix. Even restricted to `unique=card` (where it's
legitimate to trust `all_match_known` for the *count*), row emission has its own, separate bug: for a
divergent card, the code picks the card's normal default-preferred printing to *display* without
checking that this specific printing is actually legal in the queried format.

`docs/issues/00664-engine-border-planes.md` (#666, already shipped) documents the actual invariant this
violates, for a different printing-varying field:

> `unique=card` semantics require a *single* printing to satisfy the *whole* filter, not each
> predicate independently satisfied by *some* (possibly different) printing.

That's exactly why `border:` was kept out of `compile_plane` entirely, for *every* mode, not just
`unique=printing`/`artwork` — the doc got this right and this issue's second design should have
re-checked it before assuming existence was enough for row selection too. Confirmed empirically, not
just by inspection: a fixture with printing 0 (preferred, not legal) and printing 1 (non-preferred,
legal) returns printing 0 for `f:modern unique=card` — the wrong one. `frame:white f:modern` makes the
same point concretely: the returned printing for each card must actually have a white frame, not just
be "the card's usual printing" on a card that happens to have *some* white-framed printing somewhere.

**The two exactness properties this needs are already both in the codebase, just not connected.**
`Narrowed::tight` already means the stronger, correct thing for row selection — "true for *every*
printing" (see its own doc comment) — which is exactly why legality's `narrow_rec` arms correctly
report `loose`, not `tight` (see below). The bug is that `compile_plane`/`split_planes`/
`all_match_known` has no equivalent second bit: every existing arm (colors, types, cmc, devotion,
border where it's used at all) happens to be printing-universal, so "compiled successfully" and
"safe to skip per-printing work when picking a row" were always the same fact — until Legality, which
is the first plane where they diverge. `plane_expr_is_existential` already *is* the missing bit
(`existential` = card-exact-only, `!existential` = printing-universal); it was just aimed too
narrowly, at gating whether `split_planes` attempts the fold per mode, rather than being kept around
for row emission regardless of mode.

**The count/candidate-membership fast path is unaffected and doesn't need to change.** The number of
matching *cards* is genuinely mode-independent and existence is genuinely sufficient to compute it —
Step 2's popcount-skip walk (`run_query_streamed_popcount`) stays exactly as fast as measured. Only
*row selection* — picking which printing to attribute to a matching card — needs the extra check, and
only when `plane_expr_is_existential(plane)` is true.

**Proposed fix**: a printing-level evaluator for a compiled `PlaneExpr`, structurally the same idea as
the abandoned first design's `eval_plane_expr_for_printing` (walk the tree once per printing; a
`Plane` leaf outside the legality ranges reads the already-known card-level bit — cheap, since
card-invariant fields don't vary by printing; a `Plane` leaf inside `PLANE_LEGAL_EXISTS`/
`PLANE_LEGAL_ILLEGAL` reads `printing.card_legalities` directly instead, the same check `tri()`
already does per printing). The critical difference from the abandoned design, which is what makes
this cheap rather than a repeat of the earlier performance mistake: that version ran this over *every*
divergent candidate to repair a bulk bitmap; this only needs to run over the printings of cards in the
*emitted page* (bounded by `limit`), never the whole candidate set — the count already came from the
plane without touching this at all. Soundness: whenever the compiled expression is true at the card
level, at least one printing is guaranteed to satisfy it too (card-invariant leaves hold for every
printing by construction, and the shared-witness decline already rules out the one composition shape
where an existentially-true card could lack any single witnessing printing), so the search over a
card's printings always terminates with a match — it just needs to actually run instead of being
skipped by `all_match`.

Two call sites need this: `run_query_streamed_popcount`'s emit loop (currently `(start <
end).then_some(start)` for `Prefer::Default`, unconditionally) and `push_card_matches`'s `Mode::Card`
branch (same unconditional `all_match || ...` shortcut). Both should fall back to the printing-level
walk specifically when the plane touched a legality leaf, in preferred-printing order, taking the
first (or best-`prefer_score`, matching the existing non-default-prefer branch) printing that
satisfies it.

`unique=printing`/`artwork` need no change: they already decline the fold via `unique_is_card` and run
the existing (correct) per-printing `card_pass` walk over their full candidate set, which was never
skipping a per-printing check to begin with.

### A third correctness hole: the plane check and the residual check must be conjoined, not either-or

Found in PR review (not by the test suite — `card_engine/src/tests.rs`'s existing compound test only
covered a legality leaf ANDed with a *card-invariant* sibling, colors/types, never a genuinely
printing-dependent one). `split_planes`'s `And` arm can promote a single-format legality leaf into
`plane` while a real, printing-varying predicate stays behind in the residual — `DateCmp`,
`ArtistMatch`, `FlavorMatch`, printing-level `CollectionCmp`, and anything else that never appears in
`planes.rs` and so can never itself be plane-compiled. `format:A AND date>20200101` (`unique=card`) is
the concrete shape: `format:A` promotes to `legal_exists_A`, `date>20200101` stays as the residual.

The first cut of the row-selection fix checked *only* `eval_plane_expr_for_printing` whenever
`existential_plane` was `Some`, dropping the residual/`all_match` check entirely — so it would happily
pick a printing that's legal in A but fails the date filter (or, symmetrically, undercount by ignoring
that the date-satisfying printing isn't legal). Confirmed with a fixture: printing 0 legal-in-A but
released before the cutoff, printing 1 released after the cutoff but not legal in A — no single
printing satisfies both, correct answer is 0 matches, buggy code returned 1. `card_match_count` (the
counting phase inside `run_query_streamed`, untouched by the first cut of this fix) has the mirror
gap: it only ever consulted `residual_matches`, with no `existential_plane` awareness at all, so the
*count* itself — not just which row gets shown — was wrong for this shape.

Fix: both checks must be satisfied by the *same* printing, not either one independently —
`eval_plane_expr_for_printing(...) && (all_match || residual_matches(...))` in both
`push_card_matches` and (newly) `card_match_count`, which now also takes `cid` and
`existential_plane`. This is sound because `eval_plane_expr_for_printing` was already exact per
printing (it wasn't wrong, just incomplete on its own) — conjoining doesn't change its cost profile,
still bounded by `limit` in `push_card_matches`'s case, and by the candidate set in
`card_match_count`'s case (same cost that path already paid for the residual check alone; the extra
plane check per printing is now the same shape of work already being done for the residual, not a
new order-of-magnitude).

Verified against the real corpus, not just the fixture: 247 oracle_ids in
`benchmarks/bitplanes/corpus.jsonl` are genuinely this conflict shape for `oldschool` legality +
release date (some printing legal, some printing past a cutoff, never the same printing) — 0
violations post-fix, for both `unique=card` and `unique=printing`.

### A performance regression, caught by the broad survey, not the targeted script

The first cut of the conjunction fix routed both the "no existential plane" (overwhelmingly common)
and "existential plane present" cases through one closure-based `satisfies` helper inside
`card_match_count`. Correct either way, but `card_match_count` runs once per *candidate* — for
`run_query_streamed`'s counting phase, that's potentially the whole candidate set, not just the
emitted page — so it's exactly the kind of hot loop where an extra closure indirection and branch,
paid on *every* call regardless of whether `existential_plane` is ever `Some`, can show up in
aggregate even though it changes nothing for the common case.

It did: `banned:modern`/`restricted:vintage` — queries with zero code-path overlap with anything in
this issue (`expected != LEGALITY_LEGAL`, never plane-compilable, always an unindexed full scan, on
`main` and this branch alike) — went from ~200μs to ~230μs, a ~15% regression on a control that
should have been provably unaffected. This is exactly the trap `docs/issues/engine-printing-varying-
plane-repair-pattern.md`'s repair-toolkit history already warned about, and the reason this design
doc's own Acceptance section insists the broad survey is "not optional": the targeted script
(`scripts/bench_legality_divergent.py`) doesn't include a plain unindexed-scan control at the same
`unique=card` shape being exercised by the change, so it had nothing that would have caught this;
the 520-query survey did, immediately, once actually re-run after the conjunction fix (worth noting:
it wasn't re-run automatically — re-running the broad survey after every change in this branch had to
be a deliberate, repeated step, not a one-time acceptance gate).

Fixed by splitting `card_match_count` (and, for consistency, `push_card_matches`'s `Mode::Card`
branch) into two code paths instead of one closure branching on `existential_plane` every call: the
`None` branch is now byte-for-byte the same shape the function had before `existential_plane` existed
at all, and the `Some` branch (only ever reached for `Mode::Card` with a promoted legality leaf) pays
the extra conjunction cost. Re-measured: `banned:modern`/`restricted:vintage` back to ~200μs,
matching a freshly-captured `main` baseline; 0 regressions (>15% and >10μs) across all 24 targeted
configs and the full 520-query survey, re-run back-to-back with a fresh `main` build specifically to
rule out machine-state drift between runs (this session's first comparison used a `main` baseline
captured hours earlier, which briefly looked like it might explain some survey deltas until the
provably-untouched controls proved otherwise).

### A free upgrade to the existing narrowing fallback

`narrow_rec`'s existing `Legality` arm (`legal_candidate_bits`, used whenever a shape declines full
plane consumption) narrows via `legal_exists`/`illegal_exists` directly now — no more OR-ing in the
divergent postings as a safe over-approximation, so the *candidate set itself* is exact, not just a
superset. It still reports `Narrowed::loose`, though, not `tight`: `tight` specifically means true for
*every* printing (see the `Narrowed` struct's doc), and legality is exactly the kind of per-printing-
varying fact that can't make that claim, same reasoning as rarity's own narrowing being `loose`
despite its plane also being exact. (An early version of this arm reported `tight` — caught by the
same mode-aware-`all_match` hole above, since `narrow_rec`'s own opportunistic `compile_plane`
fast-path unconditionally treated a successful compile as `tight` too, before it was made to check
`plane_expr_is_existential` the same way.) `legality_divergent`/`legal_divergent` stay exactly as they
are for the one thing they're still needed for: `filter.rs`'s per-printing `Legality` `tri()` arm
(residual `card_pass` verification), which is unrelated to narrowing and unaffected by any of this.

## Acceptance

1. Baseline on `main` (targeted script `scripts/bench_legality_divergent.py` + broad survey
   `scripts/survey_queries.py`, same corpus/seed as prior work). Memory baseline
   (`--features alloc-counter`) — expect `archive_bytes` to grow by ~172KB (44 new planes) minus the
   now-removed single-polarity `PLANE_LEGAL` block's prior ~86KB, net roughly +86KB. Trivial either
   way, worth confirming the actual delta rather than assuming.
2. Targeted configs: everything from the first design's benchmark (`format:modern id:g t:creature`,
   solo `format:X`/`-format:X` across the legal% spread, deep pagination offsets, `unique=printing`/
   `artwork` unaffected, `banned:`/`restricted:`/absent-format controls) plus the two-format
   `And`/`Or` shapes (`format:A AND format:B` declines to the fallback; `format:A OR format:B`
   promotes normally) and `-(format:X AND t:creature)`-shaped De Morgan cases.
3. **Broad survey is not optional** — the first design's severe regressions were invisible to the
   targeted script and only surfaced there. Re-run after this design lands and confirm no regressions
   anywhere, not just on the previously-affected configs — this design has no fixed repair tax to
   create a narrow-selectivity regression in the first place, so the expectation is a clean win with
   no residual gap at all (unlike the first design's Part 2 cardinality-guard need).
4. Parity tests (done, `card_engine/src/tests.rs`): a divergent-card fixture (mixed legal/illegal
   printings) correct under every polarity/offset/mode reachable; the shared-witness decline for both
   two-distinct-formats *and* same-format-both-polarities, verified against a fallback that still
   produces correct results (not just a decline check); `narrow_rec`'s exact-but-still-`loose`
   `Legality` arms; the mode-aware `all_match` fix exercised through the real `split_planes`/
   `run_query` pipeline (not just direct `narrow_rec`/`run_query`-with-`plane=None` calls, which would
   have missed it); the De Morgan `Not`-of-compound case; a regression test
   (`legality_not_still_declines_with_unnegatable_numeric_sibling`, added after a second reviewer
   flagged this doc's own self-acknowledged gap) that `contains_unnegatable_numeric`'s existing
   cmc/power/toughness guard still declines correctly when composed with a legality leaf under `Not`,
   through both `compile_plane_neg`'s `And` and `Or` arms.
   **Gap found in review**: the existing divergent-card test asserted `unique=card`'s *count* but
   discarded the returned page (`let (total, _) = run_mode("card")`) — it never checked *which*
   printing came back, which is exactly where the row-selection bug (below) was hiding. Total-row-count
   parity and the broad survey (both count-only) couldn't have caught this either; it needs an
   assertion on the actual returned `scryfall_id`, on a fixture where the preferred printing is
   deliberately the *non-matching* one.
5. Total-row-count parity on every config, every run.
6. Re-measure (targeted *and* broad survey) and iterate until clean; open PR linking #667.
7. **Row-selection fix (done)**: `eval_plane_expr_for_printing` (`planes.rs`), used by
   `run_query_streamed_popcount`'s emit loop and `push_card_matches`'s `Mode::Card` branch (both the
   gathered path and both call sites inside `run_query_streamed`) whenever `plane_expr_is_existential`
   is true for the query's plane, replacing the unconditional "pick the default-preferred printing"
   shortcut. New unit tests: a bare-leaf and a compound-`And` divergent-card fixture, `unique=card`,
   asserting the returned `scryfall_id` is the legal printing, not the preferred one (mirrors the
   existing `unique=printing` assertion, which already caught this for that mode). Additionally
   verified against the real benchmark corpus, not just hand-built fixtures — see Results.
8. **Conjunction fix (done)**: found in PR review, see "A third correctness hole" above —
   `eval_plane_expr_for_printing` and the residual check must both hold for the *same* printing, not
   either independently; `card_match_count` needed the same `existential_plane` awareness the first
   cut only gave `push_card_matches`. New unit test: legality leaf ANDed with a genuinely
   printing-dependent `DateCmp` residual, `unique=card`, asserting 0 matches when no single printing
   satisfies both. Verified against the real corpus's 247 oracle_ids that are exactly this conflict
   shape — 0 violations post-fix.
9. **Performance regression fix (done)**: see "A performance regression, caught by the broad survey"
   above — the conjunction fix's first cut cost `banned:modern`/`restricted:vintage` (and likely other
   `run_query_streamed`-counting-phase queries) ~15%, caught only by re-running the full broad survey
   after the conjunction fix, not by the targeted script. Fixed by isolating the no-existential-plane
   path back to its original code shape instead of routing everything through one closure.

## Results

Measured on `benchmarks/bitplanes/corpus.jsonl` (97,206 printings), `main` @ `967a6ca` vs. this branch,
`scripts/bench_legality_divergent.py` (3s window/config). All timings sub-millisecond throughout,
given in μs rather than ms:

| config | main | branch | speedup |
|---|---|---|---|
| `format:modern` | 194μs | 66μs | 2.9x |
| `format:pioneer` | 164μs | 64μs | 2.6x |
| `-format:modern` | 158μs | 57μs | 2.8x |
| `format:modern or format:pioneer` | 210μs | 70μs | 3.0x |
| `format:modern t:creature` | 142μs | 68μs | 2.1x |
| `format:modern c:g t:creature` (cited real usage shape) | 84μs | 66μs | 1.3x |
| `format:modern` @ offset 15000 (total 22264 — genuine deep page) | 208μs | 69μs | 3.0x (flat with offset — Step 2) |
| `format:modern t:creature` @ offset 10000 (total 12251 — genuine deep page; compare to the plain offset-0 row above: flat both sides) | 158μs | 67μs | 2.4x |
| `format:modern t:creature` @ offset 15000 (**past** its own total of 12251) | 89μs | 7μs | not comparable to the row above — an offset past the total hits a different, much cheaper code path (empty page, no scatter/skip/emit), not deep pagination |
| `format:modern t:creature`, `unique=printing`/`artwork` | 143/147μs | 137/136μs | unaffected (correctly not promoted) |
| `banned:modern`/`restricted:vintage`/`c:g` (controls) | 204/199/60μs | 200/197/59μs | unaffected (re-verified post-conjunction-fix) |

An earlier version of this table compared `format:modern t:creature` at offset 15000 against other
rows at offset 0 and made the former look implausibly fast (7μs vs the 60-70μs range everywhere else)
— offset 15000 exceeds that query's own total of 12251, so it was measuring
`run_query_streamed_popcount`'s early-return-on-empty-page path (`page_offset >= total`), not deep
pagination. `scripts/bench_legality_divergent.py`'s `deep-offset` configs are now chosen to stay
within each query's own total (2 of the original 6 rows didn't — a real gap in the benchmark, not the
engine); the past-total case is kept as its own separately-labeled `offset-beyond-total` group so the
two shapes are never conflated again.

Total-row-count parity: 0 mismatches across all 24 targeted configs and a 520-query broad survey
(`scripts/survey_queries.py --seed 667`) run against both builds. Re-measured after the row-selection
fix too: identical speedups, no detectable regression from the extra per-emitted-row check (expected —
it's bounded by `limit`, not the candidate set, and only runs at all for legality-touching queries).

**Row-selection correctness, checked against the real corpus, not just hand-built fixtures** —
total-row-count parity only proves the *count* is right, which is exactly the blind spot that let the
row-selection bug through review once already. Cross-referencing every returned `scryfall_id` against
the corpus's own per-printing `card_legalities`: `oldschool` is this corpus's best real divergent case
(536 of its 556 divergent oracle_ids, `modern` itself happens to have zero in this specific sample).
`f:oldschool unique=card` returns 961 cards, 536 of them genuinely divergent-on-`oldschool`, **0**
returned a printing that isn't actually `oldschool`-legal. Same clean result (0 violations) for
`-f:oldschool` under both `unique=card` and `unique=printing`, and for the compound
`f:oldschool t:creature` under both modes — mirroring the shared-witness-safe compound shape this
issue's own motivating query has.

Memory: archive size grew by 126,208 bytes (123.25 KiB) on this corpus (31,508 cards) — exactly 32 net
new planes (64 new `legal_exists`/`illegal_exists` planes minus the 32 removed single-polarity
`PLANE_LEGAL` planes) × 3,944 bytes/plane (`⌈31508/64⌉ × 8`). The original estimate (~86KB, from a
rougher "44 new planes" guess) undercounted; measuring instead of assuming caught it, same as the
doc's own acceptance criteria asked for. Trivial regardless, against a ~68MB archive.

## Related

- #667 — GitHub issue tracking this
- #634 — where the exactness model, `all_match_known`, and the Step 2 popcount-skip walk were built
- #654 — where the original single-polarity `legal_x` plane and `legal_divergent` postings were
  built; this issue replaces the plane (two exact planes instead of one approximate one) but keeps
  the postings list for `filter.rs`'s per-printing evaluation
- #656 — extends Step 2 to compound residuals and printing/artwork modes; orthogonal to this issue
- #666 — `border:` planes; its design doc already documents the "single printing must satisfy the
  whole filter" invariant that this issue's row-selection bug violated, and deliberately keeps
  `border:` out of `compile_plane` for exactly this reason, for every mode
- #677 — property-based fuzzer for row-selection correctness, split out as follow-on general test
  infrastructure rather than bundled into this PR; the row-selection bug here is its motivating case
- #678 — indexing `banned:`/`restricted:` (currently unindexed full scan, deliberately out of scope
  here — this issue's planes only cover `expected == LEGALITY_LEGAL`)
- [reference-engine-printing-varying-plane-repair-pattern.md](../reference-engine-printing-varying-plane-repair-pattern.md)
  — the repair-based first design, preserved for a future field that can't use this issue's escape
  hatch (query space not finite/enumerable at build time)
