# Engine: expand fuzz_row_identity_matches_reference coverage

Status: filed 2026-07-17, tracked as [#696](https://github.com/jbylund/sylvan_librarian/issues/696).

`fuzz_row_identity_matches_reference` (`card_engine/src/tests.rs:1775`) is a deterministic
property-based regression test (96 seeds × ~16 filter trees per seed, all `#[test]`) that checks
row identity — every `(card, printing)` pair returned by `run_query` must satisfy the filter
individually, not merely belong to a card that satisfies a card-level projection. It caught #676
(wrong-printing-of-a-matching-card in the all_match promotion) and is the canonical place to add
coverage for any predicate or structural concern that `total`-only tests can miss.

Three independent gaps limit its value today:

## 1. Missing predicate coverage

`FuzzLeaf` only covers: Color, Type, Cmc, Rarity, Border, Legality.

Fields absent from `fuzz_store` and `FuzzLeaf` alike:

| Field | Category | Notes |
|---|---|---|
| `power` / `toughness` | NumericCmp (card-level, nullable) | Used in arithmetic exprs too |
| `loyalty` | NumericCmp (card-level, nullable) | |
| `collector_number_int` | NumericCmp (printing-level) | printing-varying like rarity |
| `price_usd` / `price_eur` / `price_tix` | NumericCmp (printing-level, nullable) | stored as integer cents |
| `edhrec_rank` | NumericCmp (card-level, nullable) | |
| `DateCmp` / `YearCmp` | Date predicates | `released_at_int` is in APrinting; stub prints don't set it |
| Arithmetic (`NumExpr::Arith`) | e.g. `cmc+1 < power`, `usd+1 < cmc` | Cross-path exercise (`field_num` on both sides) |

None of these are exercised by the current fuzzer: `fuzz_store` leaves all of
`creature_power`, `creature_toughness`, `planeswalker_loyalty`, `collector_number_int`,
`price_usd/eur/tix`, `edhrec_rank`, and `released_at_int` at their `None`/zero stub defaults, so
any filter using them would trivially match nothing (Null → False for comparisons), and no
`FuzzLeaf` arm generates them.

## 2. Sort column and direction coverage

`fuzz_check_case` hardcodes `orderby = "edhrec"` and `direction = "asc"`. The sort column matters
because:

- `run_query` has a **streaming fast-path** (`run_query_streamed`) triggered when the chosen
  `orderby` has a precomputed sort permutation (`SortPerms::get`) — covers `EdhrecRank`,
  `Cubecobra`, `Cmc`, `Power`, `Toughness`, `Name`. `Rarity` and `PriceUsd` fall through to the
  gathered path (no permutation). The streamed path's printing-selection logic is separate from
  the gathered path's, so a wrong-printing bug there would not be caught by a test using only
  `edhrec` sort.
- The **streamed-popcount fast-path** (`run_query_streamed_popcount`) is triggered only when the
  filter reduces to `FilterExpr::True` (all consumed by `split_planes`) *and* a permutation
  exists *and* `Mode::Card`. The current fuzzer always has a non-True residual after
  `split_planes`, so this path is never reached.
- `direction = "desc"` exercises the inverse permutations (`SortPerms::get_inv`) and the
  `descending` flag in `sort_key_bits`; currently untested.

## 3. Store size / pagination coverage

`fuzz_store` generates 5–15 cards, 1–3 printings each: at most ~45 printings, far below
`STREAM_MIN_MATCHES = 1024`. This means `maybe_broad` is always false, so `run_query_streamed` is
never taken (the permutation check `if maybe_broad && ...` short-circuits). The streamed path
can only be reached by a store that is either large enough on its own (> 1024 cards) or uses
`candidate_cards = None` (no narrowing) and a selectivity low enough that `v.len() > 1024`.

Pagination (`page_offset > 0`) is also never tested — `fuzz_check_case` always passes `0`.

## Proposed expansions

### Predicate additions

1. Add `FuzzLeaf::Power`, `FuzzLeaf::Toughness` (card-level, nullable `i8`; `None` for ~30% of
   cards to generate Null propagation through And/Or/Not).
2. Add `FuzzLeaf::Loyalty` (similar, for planeswalkers; sparser).
3. Add `FuzzLeaf::CollectorNumber` (printing-level `u16`, same nullability).
4. Add `FuzzLeaf::Price` (printing-level `u32` cents; skip eur/tix for now, one is enough to
   exercise the cents representation).
5. Add `FuzzLeaf::Date` (`DateCmp` on `released_at_int`; already set by `store_of` as sequential
   `20200101 - k*10000`).
6. Add `FuzzLeaf::Arith` — a two-operand `NumExpr::Arith` combining one card-level and one
   printing-level field (e.g. `cmc + 1 < collector_number`) to exercise the PDep propagation
   through arithmetic and the field_num-on-both-sides path.
7. Populate these fields in `fuzz_store`: randomise `creature_power` / `creature_toughness` /
   `planeswalker_loyalty` (sparse), `collector_number_int` (sequential per-printing), and
   `price_usd` (small random cents values; some `None`).

### Sort column coverage

8. In `fuzz_check_case`, rotate the sort column from a small random subset — at minimum:
   `edhrec` (current), `name`, `cmc`, `usd` (no permutation, gathered path) — and both
   directions. This exercises `run_query_streamed`, its inverse-permutation variant, and the
   gathered path with a printing-keyed sort.

### Store size / popcount-path coverage

9. Add a separate `fuzz_check_case` variant (or a second loop in the outer test) with a larger
   synthetic store — ~2,000 cards, 1–2 printings each — so `maybe_broad` is true and
   `run_query_streamed` fires. Name/cmc/edhrec orderby all provide permutations; usd does not.
10. Add at least one sub-case where `split_planes` consumes the whole filter to `True` (a pure
    Color+Type query, same as the existing popcount-path unit tests) and orderby has a
    permutation, so `run_query_streamed_popcount` fires under the fuzzer's broader filter
    combinatorics.
11. Add pagination: one pass per seed at `page_offset = LIMIT / 3`, asserting that the returned
    page rows are a subset of the full-result rows and still individually satisfy the filter.

## Out of scope for this issue

- Text predicates (`TextContains`, `NameMatch`, etc.): require string data in the store; adds
  complexity without exercising row-identity-relevant paths (#676's bug was in the
  printing-selection logic, not text matching). File separately if text-predicate coverage becomes
  a concern.
- `ManaCostCmp` / `Devotion`: mana cost plumbing requires additional `fuzz_store` setup that is
  orthogonal to the printing-identity concern; separate issue.
- `CollectionCmp` / `ArtistMatch` / `FlavorMatch`: similarly require vocab/string setup.
