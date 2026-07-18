# Engine: extend fuzz_row_identity to text, collection, and mana predicates

Status: filed 2026-07-18, tracked as [#699](https://github.com/jbylund/sylvan_librarian/issues/699).
Follow-up to [#696](https://github.com/jbylund/sylvan_librarian/issues/696) /
[#698](https://github.com/jbylund/sylvan_librarian/pull/698), which expanded the row-identity
differential fuzzer (`fuzz_row_identity_matches_reference`) to numeric/range/existential predicates
on corpus-like data but deliberately scoped out three predicate families that need string / vocab /
mana setup.

## The gap

The fuzzer's `every returned (card, printing) row satisfies the filter` check (the #676 class) and
its total-parity check cover numeric/range/existential predicates across all query paths and scales.
Three families are still guarded only by their own unit tests, not this differential harness:

1. **Text** — `TextContains` (oracle/name/flavor/artist), `NameMatch`, `ExactName`, `TextExact`
   (already partly via border), `TextRegex`. Exercises the trigram + name-bigram indexes and the
   text-predicate memoization (#624 / #635). `fuzz_store` interns `""` for every text field today.
2. **Collection** — `CollectionCmp` over subtypes / keywords / oracle-tags / art-tags / is-tags /
   frame-data (`CollField`), plus `ArtistMatch`. Membership + the tag index
   (`build_tag_index` / `build_thresholded_tag_index`). Real frequencies are **biased** (subtype
   `human` common, `monkey` rare — see the #698 distribution discussion), which drives selectivity.
3. **Mana** — `ManaCostCmp` and `Devotion`. Lane arithmetic over `ManaCost` (`core`, `hybrids`,
   `devotion`, `cmc`) and the devotion planes. `fuzz_store` sets an all-zero `ManaCost` today.

## Why it matters

A wrong-printing / wrong-row bug (#676 class) in any of these paths would not be caught by the
current fuzzer. Notably `card_rarity`/border/legality are printing-varying and already covered, but
the text-narrowing/memoization, collection-membership, and mana/devotion paths are structurally
different code the fuzzer never reaches.

## Approach (family-by-family)

Reuse the existing harness (`fuzz_check_case` total-parity + pagination + row-identity, and the
cheap `fuzz_check_row_identity`) and the small + corpus-like store passes. Per family: add
`FuzzLeaf` variants, populate `fuzz_store` with realistic data (biased where applicable), **build
the associated narrowing indexes** (load-bearing — an unbuilt index silently narrows to the wrong
set, as the range indexes did in #698), and wire `fuzz_build_filter` / `fuzz_describe`. Land each
family as its own PR to keep them reviewable:

- [ ] **Collection** (this PR first): subtypes / keywords / tags with biased vocab frequencies +
      `ArtistMatch`; build the tag index. Directly addresses the subtype-distribution point.
- [ ] **Text**: names / oracle / flavor / artist strings + trigram / name-bigram / oracle / flavor
      indexes; `TextContains` / `NameMatch` / `ExactName` / `TextExact`. (`TextRegex` optional — it
      has no index path, pure residual, lower value.)
- [ ] **Mana**: populate `ManaCost` (core pips, hybrids, devotion, cmc) + build devotion planes;
      `ManaCostCmp` / `Devotion`.

## Acceptance

Each family's PR: the fuzzer passes clean (regression guard) at small + corpus scale, debug +
release; no new clippy warnings; the reference oracle (`FilterExpr::matches`) and the narrowing
index agree (proven by the differential check itself, which fails if an index is unbuilt or wrong).

## Related

- [00696-engine-fuzz-coverage-expansion.md](00696-engine-fuzz-coverage-expansion.md) — the parent
  fuzzer-expansion issue this continues.
