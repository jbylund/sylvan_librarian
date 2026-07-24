# Engine: Arith-Expression Tuple Postings (`power+toughness<4`, `cmc+1<power`)

**Status: proposed**, filed as [#743](https://github.com/jbylund/sylvan_librarian/issues/743). Found
via `scripts/survey_queries.py` while checking the survey's remaining slowest queries after #739–#741
landed (same pass as the sibling
[00744-engine-compose-orderby-range-walk.md](00744-engine-compose-orderby-range-walk.md), which
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
   at all — this shape sidesteps the entire class of problem `local-engine-negated-range-narrowing.md`
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

## Expected cost

Not yet measured — a sizing, not a promise, same convention this thread has followed throughout:
~531–564 tuple evaluations (arithmetic on small ints, no memory indirection beyond the dictionary
itself) is the same shape of cost this thread's other newly-narrowed queries dropped to (low tens of
μs down from hundreds). Likely two orders of magnitude off the current ~0.4–0.5ms, if the pattern
holds — measure once built, not asserted here.

## Acceptance

- `power+toughness<4`/card/rarity and `cmc+1<power`/printing/edhrec: must drop from ~0.4–0.5ms into
  the single-digit-to-low-tens-of-μs range.
- New Rust differential test: for a fuzzed sample of `NumericCmp`/`Arith` shapes restricted to the
  four in-scope fields, the tuple-postings narrowing's result set must exactly match the existing
  per-card scan + `Tri::eval` reference (same pattern `fuzz_row_identity_matches_reference` already
  uses) — both polarities, bare and negated.
- A query mixing an in-scope and out-of-scope field (e.g. `usd+1<power`) must correctly decline (fall
  to the existing path) — a dedicated test case, not just documented reasoning.
- New targeted benchmark script (`scripts/bench_arith_tuple_postings.py`, following
  `bench_negated_range_narrowing.py`'s pattern): the two measured queries above, their negated forms,
  at least one compound (`And`'d with an unrelated filter), and controls (a bare non-arith numeric
  comparison, an out-of-scope arith expression) that must hold flat.
- Broad survey re-run: both entries must drop out of the current top-20; nothing else regresses.

## Related

- [00744-engine-compose-orderby-range-walk.md](00744-engine-compose-orderby-range-walk.md) — sibling
  doc from the same survey pass, covering the `format:commander`/`format:legacy` cluster (#3–6)
  instead of the arith cluster (#8/#15 in `branch-c6484a3.csv`).
- [local-engine-negated-range-narrowing.md](local-engine-negated-range-narrowing.md) — where the
  `and_child_rank`/`narrow_rec` single-source-of-truth precedent (relevant to the shared-evaluator
  decision above) came from.
- `docs/workflows/performance-pr-workflow.md` — the process this doc follows.
