# Engine: Arith-Expression Tuple Postings (`power+toughness<4`, `cmc+1<power`)

**Status: done**, shipped for [#743](https://github.com/jbylund/sylvan_librarian/issues/743). Found
via `scripts/survey_queries.py` while checking the survey's remaining slowest queries after #739–#741
landed (same pass as the sibling
[00744-engine-compose-orderby-range-walk.md](./00744-engine-compose-orderby-range-walk.md), which
covers the other half of that list).

## Measured problem

Broad survey (`benchmarks/survey/branch-c6484a3.csv`, 97,206-printing corpus):

| query | unique/orderby | min ms | total |
|---|---|---:|---:|
| `power+toughness<4` | card/rarity | 0.486 | 3,975 |
| `cmc+1<power` | printing/edhrec | 0.374 | 1,199 |

Both are `arith`-shaped (`NumExpr::Arith`, this project's `cmc+1<power`-style DSL extension).
Neither `narrow_rec` nor `is_printing_composable` has any arm matching a `FilterExpr::NumericCmp`
where either side is `Arith(...)` (checked directly — no matches anywhere in `lib.rs`), so every
arith query today is a full, unnarrowed `GatheredScan`: evaluate `NumExpr::eval` per card, `O(n_cards)`
regardless of how selective the predicate actually is.

## Where the cost is

Same "no dedicated index, falls to generic per-candidate evaluation" shape every other predicate in
this survey-driven thread had before it got its own index (watermark, `-usd<c`, etc.) — just for
*combinations* of numeric card fields instead of one field's raw value.

## Proposed approach

Cmc/power/toughness aren't independently free-form — Magic cards draw from a small, real-world-bounded
set of joint values. Checked directly, both against the local corpus export and the live blue DB
(identical count either way):

```sql
select count(*) from (select creature_power, creature_toughness, cmc from magic.cards group by 1, 2, 3) t;
-- 531
```

531 distinct `(power, toughness, cmc)` triples across 31,508 cards. Adding `planeswalker_loyalty`
brings it to 564 — still tiny. Any predicate that's a pure function of these fields can be evaluated
against the ~531–564 distinct combinations directly (microseconds) instead of every card, then
resolved to cards via postings — the same dictionary-encode-then-postings shape `set_codes`/
`watermarks`/rarity already use for a single low-cardinality field, just for a joint tuple.

### Structure

1. **`ArithTupleIndex`** (build-time, once): compute each card's `(cmc, power, toughness, loyalty)`
   key — `(Option<u8>, Option<i8>, Option<i8>, Option<u8>)`. Rust's derived `Hash`/`Eq` on the tuple
   handles the NULL cases directly; no sentinel encoding needed. Intern into a dense `u16` id
   (same pattern as `VocabInterner`), and build postings (tuple-id → card ids) alongside a parallel
   `Vec<TupleKey>` (tuple-id → its own field values, for query-time re-evaluation).
2. **Eligibility check** (mirroring this thread's `bare_range_bounds`/`is_rarity_negation_shape`
   single-source-of-truth precedent — one function both the narrowing arm and any future ranking
   code call, not two copies that can drift): a `NumericCmp{lhs, op, rhs}` qualifies iff every
   `NumField` referenced anywhere in `lhs`/`rhs` — recursively through nested `Arith` — is one of
   `{Cmc, Power, Toughness, Loyalty}`. A reference to any other field disqualifies the *whole*
   expression; falls back to today's scan unchanged.
3. **Narrowing**: evaluate the full `NumExpr` tree against each of the ~531–564 tuple keys — exact,
   not approximate, using the same NULL-propagation `eval_arith` already implements (`Null op
   anything = Null`, `Div` by zero = `Null`). Collect the tuple-ids where the result is `Tri::True`,
   union their postings. Exact, **tight**, card-space — no residual `card_pass` needed for the arith
   part at all.
4. **Negation is free**: re-run the same tiny scan checking for `Tri::False` (or negate the
   comparison op before evaluating) instead of complementing a candidate set. No NULL-inclusion risk
   at all — this shape sidesteps the entire class of problem `00741-engine-negated-range-narrowing.md`
   spent three fixes on, structurally, by recomputing from scratch instead of complementing.

### The one real implementation decision: shared evaluator, or a second one?

`NumExpr::eval`/`eval_arith` (`filter.rs`) already implement exactly the recursion/NULL-propagation
this needs, but operate on `&AOracleCard`/`Option<&APrinting>` (real archived structs), not a bare
tuple. Two honest options:

- **(a)** A small, self-contained evaluator scoped to the tuple's 4 fields (~20–30 lines), backed by
  a differential test asserting agreement with `NumExpr::eval` against every real tuple in the corpus.
- **(b)** Parameterize `field_num`'s fetch step behind a small trait/closure so the real per-card
  path and this tuple-scan path share one evaluator.

This thread's last two commits (the `and_child_rank`/`narrow_rec` unification) are a fresh, direct
lesson that duplicated classification/evaluation logic drifts — (b) is the principled choice if it
doesn't require reshaping `filter.rs` more than this is worth; (a) plus a real differential test (not
just "it runs") is the acceptable fallback if it does. Decide once actually implementing, not here.

**Resolved: (b), and it needed almost no reshaping.** `NumExpr::eval` was already carefully split into
a non-recursive `eval` (`#[inline(always)]`) delegating the `Arith` case to a separate recursive
`eval_arith` — that split is *why* the hot per-card path inlines (see the long comment on the impl: an
`#[inline(always)]` on a self-recursive function is ignored by LLVM). Generalizing the fetch step to a
`Fn(NumField) -> NumVal` closure preserves that split exactly: `eval_with<F>` is still non-recursive
and force-inlined, `eval_arith_with<F>` is still the colder recursive one. The per-card call site
(`FilterExpr::tri`'s `NumericCmp` arm) passes `&|f| field_num(card, printing, f)`; monomorphization +
inlining reproduce the pre-#743 hand-written match byte-for-byte, confirmed by the broad-survey
percentiles holding (p50/p75 unchanged) and every numeric control staying flat. `tri`'s `NumericCmp`
arm and the tuple scan now both go through one shared `numeric_cmp_tri<F>` — a single evaluator, no
second copy of the recursion / NULL-propagation / div-by-zero logic. The tuple entry point
(`eval_arith_tuple_tri`) builds the four-field closure and calls it; an out-of-scope field would be an
`is_arith_tuple_route` bug and trips a `debug_assert!` (CI runs debug).

## Scope / non-goals

- **In scope**: `Cmc`, `Power`, `Toughness`, `Loyalty` — all card-level (not printing-dependent), all
  small bounded integer domains, confirmed low joint cardinality (531–564 distinct combinations).
- **Explicitly out of scope: `EdhrEc`.** Card-level, but **31,444 distinct values across 31,508
  cards** (checked directly) — effectively unique per card. Including it in the joint tuple would
  balloon the dictionary to roughly card-count size and defeat the entire premise.
- **Explicitly out of scope: `PriceUsd`/`PriceEur`/`PriceTix`/`RarityInt`/`CollectorNumberInt`/
  `PreferScore`.** All printing-level (vary per printing, not per card) — a real historical bug shape
  (`usd+1<power`, referenced directly in `field_num`'s own doc comment) mixes a printing-level field
  with a card-level one. Must be excluded from eligibility entirely: an expression mixing one in-scope
  and one out-of-scope field doesn't get to use this path at all, not partially.

## One regression found via benchmarking, and the fix

The first working version narrowed a *broad* arith predicate (`cmc>=power`, ~50% of cards) by
gathering every matching card id from ~280 scattered postings rows into a sorted `Vec` — ~3× the cost
of a bare numeric index's single contiguous slice for the same result size (measured: 17k-card result
at 194 µs vs. a bare numeric's 69 µs). Harmless for a *lone* broad query (still beats the ~450 µs full
scan), but inside an `And` with a selective sibling (`cmc>=power cn:1`, `cmc>=power oracle:search`) the
broad set was built first and then thrown away against the selective side — a real regression
(66 → 171 µs, 133 → 194 µs). Same class as #741's "broad negated range now narrows and regresses."

Fixed with the #636 representation split the codebase already uses: a first pass counts the covered
cards, and a result above `BITS_PROMOTE` becomes a `CardBits` bitmap via an O(count) `scatter_bits`
(no sort, and the word-wise form `And`/`Or` want for a broad set) instead of a sorted vec; sparse
results keep the vec path, now with the `count` reserved up front. After: `cmc>=power cn:1` 66 → 72 µs
(back to parity, within noise), `cmc>=power oracle:search` 133 → 80 µs (now an improvement), and the
negated targets got faster too (their sets exceed `BITS_PROMOTE`). Broad-survey re-run: **zero
regressions >15%** across all 520 queries, `total` parity on every row.

## Measured (`scripts/bench_arith_tuple_postings.py`, 97,206-printing corpus, min µs)

| query | unique/orderby | before | after | change | total (parity) |
|---|---|---:|---:|---|---:|
| `power+toughness<4` | card/rarity | 484 | 105 | **4.6×** | 3,975 |
| `cmc+1<power` | printing/edhrec | 387 | 65 | **6.0×** | 1,199 |
| `power+toughness<4` | card/edhrec | 440 | 83 | **5.3×** | 3,975 |
| `cmc+1<power` | card/edhrec | 395 | 61 | **6.5×** | 414 |
| `-power+toughness<4` | card/rarity | 540 | 142 | **3.8×** | 13,337 |
| `-cmc+1<power` | printing/edhrec | 417 | 116 | **3.6×** | 44,596 |
| `power<toughness` (field-vs-field) | card/edhrec | 389 | 69 | **5.6×** | 4,680 |
| `loyalty>=4` (bare, no dedicated index) | card/edhrec | 211 | 61 | **3.5×** | 219 |
| `power+toughness<4 t:creature` (compound) | card/edhrec | 225 | 87 | **2.6×** | 3,971 |
| `cmc+1<power c:g` (compound) | card/edhrec | 143 | 61 | **2.3×** | 133 |
| `power>4` (control, bare numeric) | card/power | 59 | 58 | flat | 2,248 |
| `cmc>6` (control, bare numeric) | card/cmc | 57 | 56 | flat | 1,344 |
| `usd+1<power` (control, out-of-scope — declines) | card/edhrec | 646 | 657 | flat | 11,861 |
| `t:creature` (control) | card/edhrec | 61 | 60 | flat | 17,317 |

Geometric mean **4.15×** over the ten impacted rows; controls flat; `total` identical across builds
on every row. Broad survey (`scripts/survey_queries.py`, seed 42, 520 queries): both entries left the
top-30 entirely (`power+toughness<4` was #8, `cmc+1<power` #11); p90 182 → 165 µs, p99 580 → 569 µs,
max 1.040 → 1.016 ms; no query regressed.

## Memory (archive layout changed — new `CardIndexes.arith_tuple`)

`QueryEngine.mem_stats()` on the same corpus, `--features alloc-counter` build:

| | before | after | Δ |
|---|---:|---:|---:|
| `archive_bytes` | 71,745,580 | 71,880,648 | +135,068 (+0.19%) |
| `indexes_rkyv_bytes` | 25,731,884 | 25,866,952 | +135,068 (+0.52%) |
| `reload_peak` | 156,192,010 | 156,192,009 | ~0 |

The whole cost of the new index: ~135 KB (564 keys + ~31.5k card-id postings), all in the indexes
component, `reload_peak` unchanged.

## Testing

- New Rust test `arith_tuple_narrowing_matches_reference`: for every op (`<`/`≤`/`>`/`≥`/`=`/`≠`) over
  a bare loyalty compare, field-vs-field (`power<toughness`), and four arith shapes, across four
  corpus-like fixture seeds, both polarities — the narrowing's candidate set must be **tight** and
  byte-identical to the per-card `matches` (i.e. `tri`/`eval`) reference (a decline is only ever the
  >75% breadth cap, asserted). Plus dedicated assertions that `usd+1<power` (in/out-of-scope mix) and
  `usd*2<cmc` (pure out-of-scope) do **not** route and decline to the full scan, that bare
  `cmc<c`/`power>=c` stay on their dedicated numeric arms, and that bare `loyalty<c` (no dedicated
  index) does route.
- The existing `fuzz_row_identity_matches_reference` now also exercises this path end-to-end through
  `run_query` (its `FuzzLeaf::Arith`/`Loyalty` shapes route to the tuple index once the fixture store
  builds `arith_tuple`), including the out-of-scope arith shapes (price-mixed) that must decline.
- `cargo test` (debug + release): 132/132 passed. `pytest api/tests/test_engine_*`: 158/158.
  `cargo clippy`: no new warnings.

## Related

- [00744-engine-compose-orderby-range-walk.md](./00744-engine-compose-orderby-range-walk.md) — sibling
  doc from the same survey pass, covering the `format:commander`/`format:legacy` cluster (#3–6)
  instead of the arith cluster (#8/#15 in `branch-c6484a3.csv`).
- [00741-engine-negated-range-narrowing.md](00741-engine-negated-range-narrowing.md) —
  where the `and_child_rank`/`narrow_rec` single-source-of-truth precedent (relevant to the
  shared-evaluator decision above) came from.
- `docs/workflows/performance-pr-workflow.md` — the process this doc follows.
