# Engine: card-level rarity bitplanes (common/uncommon/rare/mythic)

Status: implemented, PR pending. GitHub: #670. Follows
[docs/workflows/performance-pr-workflow.md](../workflows/performance-pr-workflow.md).
Split out of #630 (bitplanes phase 3) after that issue closed with phases 1/2 shipped.
Verification-side follow-on tracked separately: #674.

## Measured problem

`card_rarity_int` (`Printing`, `Option<u8>`, `lib.rs:310`) is printing-level: a card can have
printings at multiple rarities across reprints. Today it's served entirely by `RarityIndex`
(`lib.rs:2042`, `[Vec<u32>; 6]`) — a card-space "any-printing-at-rarity" postings structure,
queried via `rarity_candidates()` (`lib.rs:2088`), which unions the qualifying buckets for
`Eq`/`Lt`/`Le`/`Gt`/`Ge` and declines `Ne` unconditionally ("matches nearly every card, same
convention as `numeric_candidates`"). A union past `MAX_UNION_FRACTION = 0.70` (`lib.rs:2080`)
also declines — the code's own comment cites `rarity<=mythic` (99% of entries) and
`rarity>=uncommon` (69%) as the queries that already sit near or past that ceiling, falling back
to a full per-card residual scan.

Every rarity query above that 70% coverage ceiling, plus every `Ne` comparison, pays the full
`narrow_rec` union-materialization cost (the #618 cost #630 phase 1 already retired for
colors/types) or a full residual scan — regardless of how selective the specific comparison is.

## Where the cost is

Rarity was explicitly deferred out of #630 phase 1/2: it's a `PrintingDep` existence projection at
card level (never exact-consumable the way colors/types/legality are), so it needed its own design
pass rather than reusing the exact-consumption machinery verbatim. `compile_plane` has no rarity
handling today; `NumericLayout`/`numeric_layout` (`planes.rs:428-462`) already has a `_ => None`
catch-all that a comment at `planes.rs:594` explicitly names rarity under ("Other NumericCmp fields
(rarity, price, ...) already decline via `compile_plane`'s catch-all") — i.e. the gap is a known,
named stub, not an oversight to rediscover.

## Proposed approach: 4 of 6 values as planes, extending the existing numeric-range machinery

**Selectivity, measured before committing to scope** (`benchmarks/bitplanes/corpus.jsonl`, 97,206
printings / 31,508 cards — a printing-level attribute rolling up to card level could in principle
flatten toward "every card has one of everything," so this was checked rather than assumed):

| rarity | % printings | % cards w/ ≥1 printing |
|---|---|---|
| common | 28.31% | 33.74% |
| uncommon | 24.28% | 32.43% |
| rare | 37.82% | 34.85% |
| mythic | 9.18% | **8.15%** |
| special | 0.40% | 1.17% |
| bonus | 0.00% | 0.00% |

It doesn't flatten: 91% of cards (28,713/31,508) only ever appear at a single rarity across every
printing they have, so the card-level existence projection stays close to the printing-level
distribution — `mythic` in particular stays a genuinely tight 8.15% of cards, comparable to or
tighter than dimensions already planed (`t:creature` 47% of printings, `f:modern` ~70.7% legal per
#654). `special`/`bonus` are a different story — 1.17% and effectively 0% of cards, a `bonus` plane
would be entirely empty in production — not worth a full ~4 KB plane each.

**Design: plane common/uncommon/rare/mythic, leave special/bonus on the existing postings path.**
`RarityIndex` stays exactly as-is for special/bonus; the 4 planed values are removed from its
domain (or simply left unqueried by the new plane-aware path — see reconciliation below).

**Do not extend `NumericLayout`/`compile_numeric_cmp` — that machinery is exact-consumption-only,
and rarity must never be exact-consumed.** Checked against the composition invariants before writing
any code, per the workflow doc's warning: `compile_numeric_cmp`/`numeric_layout` (`planes.rs:428-585`)
are called from exactly one place, `compile_plane`'s `FilterExpr::NumericCmp` arm
(`planes.rs:720-724`) — and `compile_plane`'s own doc comment (`planes.rs:636-641`) restricts it to
"two-valued card-level nodes... complement (Not) is only sound when the node can never be Null or
PrintingDep." Rarity is genuinely `PrintingDep`: a card with both a common printing and a mythic
printing has *both* bits set in the existence-projection plane simultaneously, which is correct for
narrowing but would be wrong to treat as "this card exactly matches `r:mythic`" the way `compile_plane`
treats a compiled node. Adding a `NumField::RarityInt` arm to `numeric_layout` would silently route
rarity into `compile_plane`'s all_match-promotion path — a real correctness bug (skips per-printing
verification), not just a missed optimization. cmc/power/toughness get away with exact consumption
despite being `Option`-valued because their `None` is `Tri::Null` (a card-level absolute — no printing
of a non-creature card has power), which correctly collapses to "excluded" through the plane by
omission; rarity's multi-valuedness is `PrintingDep` (printings genuinely disagree), which has no
such collapse.

**The narrowing path needs its own function, not a `compile_plane` extension — and it turns out
simpler than the numeric-range machinery, not an extension of it.** Rarity's domain is 6
fully-enumerable values (0-5), not an open-ended numeric range, so there's no need for
`bucket_verdict`'s fully-included/fully-excluded/ambiguous logic or observed-`[min,max]` bounds
tracking at all — every "bucket" is a single known value. A new function (in `lib.rs`, alongside
`rarity_candidates`) reuses `rarity_candidates`'s own bucket-selection (`keep`/`buckets`,
`lib.rs:2092-2100`) to get the set of matching rarity values for `op`/`threshold`, partitions that
set into planed (0-3) vs. postings-only (4-5, special/bonus), OR's the plane words for the planed
subset, and — only when the postings-only subset is non-empty — `scatter_bits`'s (`lib.rs:2249`)
that subset's postings into a bitmap and OR's it in. Both subsets empty is impossible (the op
always selects ≥0 values); postings-only empty is the common case and skips `RarityIndex` entirely.
**This also means `Ne` doesn't need `rarity_candidates`'s unconditional decline carried over**: the
decline exists there because a pure-postings "not equal" union is usually too large to be worth
materializing, but with 4 of 6 values plane-backed, `r!=mythic`'s "keep" set is mostly plane-OR
(cheap) plus a tiny special/bonus scatter — it falls out of the same generic bucket-partition logic
as every other op, no special-casing needed.

**Feeding the planes: build directly from `printings`/`offsets`, no new field on `OracleCard`.**
`build_bit_planes` (`planes.rs:156`) takes only `cards: &[OracleCard]` today — every dimension it
planes (colors/identity/produced_mana/types/legality/cmc/power/toughness) already lives directly on
`OracleCard`. Rarity doesn't; it's aggregated from `printings`+`offsets` into `RarityIndex`
separately (`build_rarity_index`, `lib.rs:2044`, called alongside `build_bit_planes` at
`lib.rs:4345`/`4369`).

The `legality_divergent` precedent doesn't actually transfer here: that field is a genuine *runtime*
dispatch flag, consulted during `Tri` computation on every query touching legality
(`filter.rs:1228`, "trust the card-level word or check this printing's own") — that's why it has to
be a permanent field on `OracleCard`. Rarity has no equivalent runtime need: residual verification
already evaluates `card_rarity_int` printing-natively (`filter.rs:98`,
`printing.map_or(NumVal::PDep, |p| known(p.card_rarity_int...))`), never consulting any card-level
cache. So a card-level rarity summary would only ever be read once, at plane-build time — no reason
to make it permanent.

Instead: broaden `build_bit_planes` to also accept `printings: &[Printing]`/`offsets: &[u32]`, and
compute the per-card OR inline in its existing per-card loop (reusing `build_rarity_index`'s
`mask |= 1 << r`, `lib.rs:2048`, rather than re-deriving it) — decided over the alternative of a
separate `build_rarity_planes(printings, offsets, wpp) -> Vec<u64>` sibling function, because the
blast radius is trivial (checked: only 3 call sites — `lib.rs:4369` and two test fixtures at
`tests.rs:227`/`847` — and all 3 already have `printings`/`offsets` in local scope right next to the
`build_bit_planes(&cards)` call, since `build_rarity_index` is already called alongside it) and
because it keeps one function as the single source of truth for the whole plane layout. A sibling
function would mean a second full pass over all cards plus splicing its output into the middle of
the same `words` buffer at the right plane offset — two functions independently agreeing on
`wpp`/plane-index math for one shared buffer, for no benefit given the data's already there. No
`OracleCard` archive-layout change either way, no permanent extra byte per card, no second copy of
information `RarityIndex` already encodes.

**Narrowing integration**: `narrow_rec`'s rarity arm (today calling `rarity_candidates`, feeding
`Candidates::Cards`) gets a plane-based path for comparisons that resolve fully within the 4 planed
values, falling back to today's postings behavior otherwise (or the reconciliation path above for
mixed comparisons) — the same `loose`/narrowing-only shape `Legality` already has, not exact
consumption (rarity is `PrintingDep`, `card_pass`/printing-level residual eval still verifies which
printings actually match).

**Nullability**: `card_rarity_int` is `Option<u8>` — some printings carry no rarity.
`build_rarity_index` already skips `None` silently (`lib.rs:2050`); the new `rarity_mask`
aggregation needs the same behavior (a card whose printings are all null-rarity sets no bits,
falling through exactly as today's postings do). Explicit parity-test case, given this repo's
history with Null-semantics bugs in exactly this kind of promotion (#634's implementation caught
two real ones; see also `docs/issues/engine-null-vs-empty-text-parity.md`).

**Two wins beyond the obvious Eq/Ge/Le narrowing**, worth their own benchmark rows:

- `Ne`, unconditionally declined today (`lib.rs:2089-2090`), becomes just as cheap as any other
  bucket combination once the 4 common values are planes (OR the other 3 planed buckets, union in
  special/bonus postings only if involved).
- `MAX_UNION_FRACTION`'s 0.70 ceiling (`lib.rs:2080`) has no equivalent for planes — a plane-OR is
  O(words) regardless of selectivity. The comparisons closest to today's cutoff
  (`rarity<=mythic` at 99%, `rarity>=uncommon` at 69%, both cited in the code comment) are exactly
  where the plane should show the biggest relative win over today's decline-and-scan behavior.

## Acceptance

1. **Baseline on `main`**: targeted script (new `scripts/bench_rarity_planes.py`, modeled on
   `scripts/bench_produces_planes.py`) + `scripts/survey_queries.py` (seed 42, same corpus reused
   from #630/#654/#655/#666/#669 — schema already covers `card_rarity_int`, no re-export needed).
   Memory baseline too (`--features alloc-counter`): this adds 4 planes (~4 KB × 4 ≈ 16 KB) to the
   archive; no `OracleCard` growth, since the planes build directly from `printings`/`offsets`.
2. **Targeted configs**: `r:mythic` / `r:rare` / `r:common` / `r:uncommon` solo, each `CmpOp`
   (mirroring the existing numeric-range op-coverage tests); `r>=rare`, `r<=uncommon` (mixed
   plane+postings reconciliation); `r!=mythic` (the newly-cheap `Ne` case); `r:special`/`r:bonus`
   (must stay on the unchanged postings path — controls, not expected to move); `rarity<=mythic`
   and `rarity>=uncommon` specifically (today's near-ceiling union-fraction cases, called out above
   as the expected biggest wins); compound with an already-planed dimension (`t:creature r:mythic`);
   `-r:common` (negation through the reconciliation path); unrelated controls.
3. **Differential/parity tests**: op-coverage test for the new narrowing function against every
   `CmpOp` (mirrors `plane_parity_color_and_type_ops`'s shape, but checked against
   `rarity_candidates`'s postings result as the reference, not a plane-vs-card-truth comparison —
   rarity narrows, it never claims card-level truth); a dedicated test for the plane+postings
   partition (a comparison spanning both, e.g. `r>=rare`, checked against a brute-force reference);
   a dedicated null-rarity-printing test (mirrors `divergent_legality_defers_to_printings`'s shape —
   a card whose only printings have no rarity at all must match nothing under any planed
   comparison, same as today); a test confirming rarity is never routed through `compile_plane`
   (i.e. `split_planes` never fully consumes a rarity predicate to `FilterExpr::True` — the
   correctness property this whole design depends on).
4. **Total-row-count parity** on every benchmark config, every run.
5. **Queries expected to improve**: any rarity comparison resolvable within the 4 planed values,
   especially `Ne` and the near-70%-ceiling `Ge`/`Le` cases. `special`/`bonus`-only queries are
   controls, not expected to move. No regressions expected — no loose/declining case is being
   removed for the planed values, only added capability.
6. Re-measure and iterate until no regressions remain; open PR per the workflow's step 6 template,
   linking #670.

## Results

**Scope turned out narrower than "rarity queries get faster" — worth stating plainly rather than
overselling.** Solo `Eq` queries on the four planed values (`r:common`/`r:uncommon`/`r:rare`/
`r:mythic`) show ~1.00x, flat. Traced why before treating it as a problem: the old postings path
never declined for a single-bucket `Eq` — it degenerates to a copy of one already-sorted bucket
(`build_rarity_index` builds each bucket in ascending card-id order), so it was already cheap.
Both old and new paths converge on the identical candidate `Vec<u32>` before `card_pass` anyway
(`Candidates::CardBits`'s own `into_cards` calls `bitmap_card_ids`, same materialization step
postings already paid). The dominant, unchanged cost for these queries is `card_pass` itself
walking each candidate's printings — a genuinely more expensive per-candidate operation than a
type/color bitmask test, and this PR doesn't touch it. Filed as its own follow-on:
[#674](https://github.com/jbylund/sylvan_librarian/issues/674) (verification-side short-circuit for
the 91% of cards with a single distinct rarity), explicitly distinguished from #657 there (#657's
elision only helps children `narrow_candidates_exact` already proved exact; rarity is never
exact/tight to begin with, so #657 gives it nothing).

**Second-round finding: the initial `Ne` win didn't reach the way people actually write "not this
rarity."** `-r:common` parses to `Not(Eq(common))`, not `Ne(common)` directly, and `Not` only
narrows through *tight* children — rarity's narrowing is always loose, so `-r:common` declined to a
full scan in both old and new code even after the first round of this change, despite `r!=common`
(the literal, rarely-typed `!=` operator) already showing a 1.78x win. Fixed by giving `-r:x` its
own dedicated `Not` arm in `narrow_rec` — the same pattern `-f:x` already established
(`legal_candidate_bits(..., negate: true)`) — that recomputes narrowing with a logically negated
operator (`negate_op`: `Eq`↔`Ne`, `Lt`↔`Ge`, `Le`↔`Gt`) rather than complementing the existing
candidate bitmap, which would be unsound for the same reason the `NumericCmp` arm's own comment
already flagged (a posted/planed card can have other printings that don't satisfy the comparison,
so bit-complementing would wrongly drop real matches). The `Not(Eq(v)) == Ne(v)` equivalence this
relies on was checked against `filter.rs`'s actual `tri()` implementation before writing the code,
not assumed from boolean-logic intuition alone — `NumericCmp`'s `NumVal::Null` branch short-circuits
to `Tri::Null` before the op-specific comparison runs, for every op including `Ne`, and `Not`'s own
`Tri::Null => Tri::Null` line confirms it never flips to a false match — so the two forms agree on
null-rarity printings too, not just known values.

One test bug caught by this second round, not the benchmark: `not_narrows_only_tight_children` had
asserted `-r:x` *never* narrows as a correctness guard (true before this fix) — updated to assert
the new, correct narrowing behavior instead, verified against the same brute-force superset check
already used for the tight-child case in that test. A test bug of my own also surfaced while
writing the new parity test for the negation arm: constructing "the same predicate, operands
flipped" needs `flip_op(op)` on the flipped side (`field < val` is `val > field`, not `val < field`)
— caught immediately by the new test disagreeing with itself across operand orders, not by manual
inspection.

The real, measured wins are exactly the two shapes predicted, plus the `-r:x` fix:

Targeted (`scripts/bench_rarity_planes.py`, `benchmarks/bitplanes/corpus.jsonl`, 3s window/config,
`min_ms`, 21 configs):

| group | query | unique | before | after | change |
|---|---|---|---|---|---|
| solo | `r:common` | card | 0.168ms | 0.171ms | 0.98x |
| solo | `r:uncommon` | card | 0.173ms | 0.177ms | 0.97x |
| solo | `r:rare` | card | 0.224ms | 0.230ms | 0.98x |
| solo | `r:mythic` | card | 0.112ms | 0.113ms | 0.99x |
| ceiling | `rarity<=mythic` | card | 0.344ms | 0.349ms | 0.99x |
| ceiling | `rarity>=uncommon` | card | 0.346ms | 0.312ms | **1.11x** |
| ne | `r!=mythic` | card | 0.407ms | 0.411ms | 0.99x |
| ne | `r!=common` | card | 0.554ms | 0.312ms | **1.78x** |
| mixed-tail | `r>=rare` | card | 0.258ms | 0.249ms | 1.04x |
| mixed-tail | `-r:common` | card | 0.626ms | 0.364ms | **1.72x** |
| tail-control | `r:special` | card | 0.067ms | 0.069ms | 0.98x |
| tail-control | `r:bonus` | card | 0.003ms | 0.003ms | 0.93x |
| negation | `-r:mythic` | card | 0.482ms | 0.487ms | 0.99x |
| and-combo | `t:creature r:mythic` | card | 0.083ms | 0.084ms | 0.99x |
| and-combo | `f:modern r:mythic` | card | 0.108ms | 0.109ms | 0.98x |
| uniques | `r:mythic` (printing) | printing | 0.150ms | 0.154ms | 0.98x |
| uniques | `r:mythic` (artwork) | artwork | 0.173ms | 0.177ms | 0.98x |
| control | `name:soldier` | card | 0.029ms | 0.030ms | 0.97x |
| control | `cmc>6` | card | 0.057ms | 0.058ms | 0.97x |
| control | `oracle:creature` | card | 0.121ms | 0.123ms | 0.99x |
| control | `c:g` | card | 0.053ms | 0.053ms | 0.99x |

Geomean 1.04x across all 21 configs. Total-row-count parity held on every config, every run.
`-r:common` (1.72x) now matches `r!=common` (1.78x) closely, as expected — verified-identical
predicates. `-r:mythic`/`r!=mythic` both stay flat: "not mythic" narrows to only ~94% of the corpus
(mythic is the rarest planed value at 8.15% of cards, so almost every card has *some* non-mythic
printing), leaving little for narrowing to exclude before `card_pass` regardless — same underlying
"narrowing only pays off when the exclusion is substantial" dynamic as the solo-`Eq` finding above,
not a new phenomenon. The small (1-3%) dips across otherwise-unrelated `control`/`and-combo`/
`uniques` rows are within this corpus's normal run-to-run noise band for sub-millisecond queries,
not attributable to a code path those queries don't exercise (none of them touch `Not`).

Broad survey (`scripts/survey_queries.py`, seed 42, 520 queries): geomean 1.01x (flat), zero
regressions >15%. No rarity-predicate queries happened to land in this particular random sample
(the generator doesn't appear to weight `r:`/`rarity:` filters at all — only `orderby=rarity` sorts
showed up under that tag), so this run serves as a pure regression safety net rather than
demonstrating the win, which the targeted benchmark above covers directly.

Memory (`--features alloc-counter`): `archive_bytes` +15,784 bytes (+0.023%) — matches the
predicted 4 planes × 493 words × 8 bytes almost exactly, unchanged by the `-r:x` fix (pure logic,
no new data). `cards_rkyv_bytes` unchanged, confirming no `OracleCard` growth. `reload_peak`
unchanged.

One real bug caught during the first implementation round, not by the benchmark:
`narrow_fixture_store` (an existing test fixture) rebuilt `RarityIndex` after mutating printing
rarities but not `BitPlanes`, leaving stale (pre-mutation, all-zero) rarity plane bits — caught by
`adaptive_narrowing_run_query_parity` failing (`total=2` vs brute-force `6`) on first test run.
Fixed by rebuilding `indexes.planes` alongside `indexes.rarity` in that fixture.

**Decision: land as-is rather than holding for #674.** Same phasing precedent #630 itself already
established — colors/types bitplanes (#633) didn't move `t:creature` either, until #634 added
exactness propagation on top; shipping the narrowing substrate before the verification-side win
lands is this codebase's normal pattern, not a deviation from it. This PR retires part of the #618
union-materialization cost and closes three real gaps (`Ne` declining, `-r:x` declining via the
tight-only `Not` rule, near-ceiling `Ge`/`Le`) on its own merits, independent of whether/when #674
lands.

## Related

- #670 — GitHub issue tracking this (Phase 3 remainder of #630)
- #630 — where rarity was scoped out of phases 1/2 as narrowing-only, `PrintingDep`
- #618 — the union materialization this retires for the 4 planed values
- #654 — legality bitplanes; `legality_divergent`'s addition to `OracleCard` is the precedent that
  was *considered and rejected* here — it's a runtime dispatch flag rarity has no equivalent of
- #655/#659 — numeric-range bitplanes (`NumericLayout`/`compile_numeric_cmp`); another precedent
  *considered and rejected* — that machinery is wired exclusively into `compile_plane`'s
  exact-consumption path (`planes.rs:720-724`), which rarity must never go through given it's
  `PrintingDep`. Rarity gets its own narrowing-only function instead, feeding `narrow_rec` directly
- #669 — produced-mana bitplanes; closest recent precedent for "extend existing plane machinery to
  one more field," though that one needed zero new reconciliation logic (produced mana is fully
  exact, unlike rarity's partial plane coverage)
