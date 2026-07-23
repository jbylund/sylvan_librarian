# Printing- and Artwork-Space Cardinality Estimates

**Status: superseded, not building.** Filed as
[#711](https://github.com/jbylund/sylvan_librarian/issues/711). This doc's whole premise was
extending the card-space cardinality estimator (below) to printing/artwork space so those modes'
plans could get a cardinality signal for cost-based routing. That premise doesn't hold: the
estimator was never wired into routing at all, in any space. `card_engine/src/estimator.rs:1-5`
says so directly — *"Standalone, SOUND, cheap cardinality estimator... **NOT wired into query
routing**; validated only through the `fuzz_row_identity_matches_reference` harness."*
`00702-engine-plan-selection-layer.md` explains why: an estimate-before-materialize prototype
measured ~15% slower and was abandoned for "materialize-then-route" on the *actual* count — every
plan (card, printing, or artwork mode) now gets an exact cardinality (a popcount, a binary-search
`k`, or a materialized candidate list's real length), never a statistical estimate. Building a
printing/artwork-space sibling to a routing mechanism that was dropped card-space-side too would
solve a problem the shipped design doesn't have.

Follow-on to [00702-engine-plan-selection-layer.md](00702-engine-plan-selection-layer.md).
The estimator shipped in #704 answers **card-space** cardinality only: it composes
over the universe `N = n_cards` and projects every printing-space leaf count *down*
to cards via `project` ([estimator.rs:57](../../../card_engine/src/estimator.rs#L57)).
This issue adds **native printing-space** (and, generalized, **artwork-space**)
estimates — the mirror image, composing over `N = n_printings` and expanding
card-native leaf counts *up*.

## Why

The plan-selection cost model routes each physical plan on cardinality **in that
plan's own operating space** (#702 "Open questions", `00702…md:285`): card-mode
gather/popcount consume card-space, but the `unique=printing` fast paths
(`printing_range_scan_applicable`, [lib.rs:4080](../../../card_engine/src/lib.rs#L4080);
the range/streamed printing paths at lib.rs ~3787/3851/3930) and artwork-mode
selection (scratch at lib.rs ~4293) operate in printing/artwork space. Today those
plans have **no cardinality input** — the estimator is card-only. Projecting the
card-space answer back up through `project` inverse is exactly the lossy step #702
warns doesn't distribute over AND/OR. A native estimate in each space avoids the
round-trip.

## The structural mirror — roles flip

Same recursion shape as `estimate_rec`, universe `N = n_printings`. What changes is
the leaf layer:

- **Printing-native leaves** become the *easy* case. Artist, flavor, date/year,
  price/collector-number ranges, printing-space tags (`art_tags`/`is_tags`/
  `frame_data`), `SetCode`/`Border`/`Watermark` — everything `has_printing_varying_leaf`
  ([estimator.rs:79](../../../card_engine/src/estimator.rs#L79)) currently flags — is
  counted *directly* in printing space. The raw postings/CSR/range count `k` **is**
  the answer (`exact(k)`); no `project` step, no superset widening. This half gets
  simpler and tighter.
- **Card-native leaves** become the *hard* case. `cmc`/`power`/`toughness`, colors,
  types, name, oracle, legality, devotion, rarity — the plane/numeric/name-index
  leaves — produce a card count `d` that must be **expanded up** to a printing-set
  size.

## Card→printing expansion (the core)

A card-native leaf matching `d` cards selects the printing set = union of all
printings of those `d` cards. Its size:

```
est = d · (n_printings / n_cards)          # average fan-out
lo  = d                                     # each card ≥ 1 printing (tight in practice)
hi  = P(d)                                  # top-d prefix sum (see below)
```

`P(d)` is the tight upper bound. Let `m(c)` = printings of card `c`, sorted
descending `m₍₁₎ ≥ m₍₂₎ ≥ …`, and `P(d) = Σᵢ₌₁ᵈ m₍ᵢ₎`. The most printings `d` cards
can cover is when they *are* the `d` most-printed cards. Since `m` is non-increasing,
`P` is **concave**, so `P(d) ≤ d·m₍₁₎` with equality only at `d=1` — a strict
improvement over the naive `d · max_printings_per_card` for every `d>1`, and the gap
blows up fast (5 basic lands ≈ hundreds each; the 6th card is nowhere near). Given
only `d`, `P(d)` is the information-theoretic supremum.

The lower bound `lo = d` is likewise tight in practice: the minimum is achieved when
the matched cards are all single-printing, and there are always far more
single-printing cards than any realistic `d`. (The exact tight lower bound is the
*ascending* prefix sum, but with a huge run of `m=1` cards it collapses to `d`.)

### New stat: the sorted-descending prefix sum

One per fan-out space. Cheap one-pass build from `offsets`
([lib.rs:2311](../../../card_engine/src/lib.rs#L2311); `m(c) = offsets[c+1]-offsets[c]`):
count per card, sort descending, cumulate. Stored raw it's `n_cards` × u32 (~120 kB
at 30k cards), but it **compresses to near-nothing** via the linear tail: let `h` =
count of cards with `m>1`; for `d ≥ h` every added card contributes exactly 1, so

```
P(d) = P(h) + (d − h)    for d ≥ h
```

Store the explicit head (`m>1` prefixes) + `P(h)`; evaluate the tail by formula.
Lookup is O(1)/O(log h). Fits the existing `build_*` pattern in `reload_commit`
(the `CardIndexes { … }` literal, lib.rs ~5229) and the rkyv/mmap bundle.

## AND composition — the payoff: no varying-leaf gate

The card-space estimator gates its AND lower bound (and NOT) on
`has_printing_varying_leaf` ([estimator.rs:207](../../../card_engine/src/estimator.rs#L207))
because card-projection doesn't distribute: two varying children can each admit a
card via *different* printings while no single printing satisfies both, so
`distinct_cards(A∩B)` isn't the intersection of the projections. **In printing space
this trap disappears.** A printing is an atomic row that either satisfies the whole
filter or doesn't, so the AND printing-set is *exactly* `∩` of the children's
printing-sets. Standard Bonferroni on printing-set sizes is directly sound — **no
gate**, `lo = max(0, Σlo_i − (k−1)·N)` unconditionally. NOT is likewise a clean
complement `N − |P|` on the atomic printing rows (no three-valued Null subtlety at
the printing level — nullability is a card-space artifact).

The `[d, P(d)]` expansion feeds this as any other child's bounds: a mixed AND
(card-native ∧ printing-native) expands the card-native side to `[d, P(d)]` printings,
then intersects with the printing-native side's exact `k`. Standard composition, no
special case.

## Artwork space (generalization)

Artwork space sits between card and printing (`n_cards ≤ n_artworks ≤ n_printings`),
with `artwork_groups: Vec<u16>` ([lib.rs:2261](../../../card_engine/src/lib.rs#L2261),
built at lib.rs ~1865) giving artworks-per-card. The expansion machinery is
**identical with a different multiplicity vector**: a generic
`expand_card_count(d, stat)` takes `lo=d`, `hi=P_stat(d)`, `est=d·(Σstat/n_cards)`,
where `stat` is the sorted-descending prefix sum of either `offsets`-widths
(printing space) or `artwork_groups` (artwork space). Two stats, one code path.

**Subtlety — the cross-fan-out projection.** A *printing-native* leaf (exact `k` in
printing space) has no card count, so expanding it into *artwork* space isn't the
card→space path — it's a printing→artwork fan-*in* (`k` printings collapse to ≤ `k`
artworks; many printings share an `illustration_id`). That needs a printing→artwork
ratio/map rather than the multiplicity prefix sum. Recommend **phasing**: printing
space first (all card-native expansions + printing-native exacts), artwork space
second once the printing→artwork projection primitive is decided. Whether artwork
routing even needs printing-native leaves projected (vs. only card-native expanded)
is an open question to settle against real artwork-mode queries.

## Return shape / API

Recursion still composes a single-space `Cardinality { lo, est, hi }`
([estimator.rs:23](../../../card_engine/src/estimator.rs#L23)) — one universe per pass,
per #702's decision that projection doesn't thread through composition. Add sibling
entry points (e.g. `estimate_cardinality_printing`, `…_artwork`) sharing the leaf
dispatch, parameterized by universe + expansion stat, rather than a both-spaces
container threaded through the recursion. The decision site assembles whichever
space(s) a given plan's cost formula consumes.

## Scope / sequencing

1. **Prefix-sum stat + generic `expand_card_count`** — build path, compression,
   fuzz coverage of the stat alone.
2. **Printing-space estimator** — `estimate_cardinality_printing`, card-native
   leaves expand, printing-native leaves exact, ungated AND/NOT. Standalone,
   fuzz-validated against `unique=printing` materialized truth, **unwired**.
3. **Artwork-space estimator** — generalize over the multiplicity stat; resolve the
   printing→artwork cross-projection.
4. **Wire into the cost model** — feed printing/artwork-space estimates to the
   printing/artwork-mode plans' cost formulas (depends on #702 step 3+).

## Validation

Mirror #704's contract in the `fuzz_row_identity_matches_reference` harness: the
`unique=printing` (resp. `unique=artwork`) reference count must always land inside
`[lo, hi]` — a violation is an algebra bug, fail the test. Estimate *accuracy* is a
reported distribution, folded into #702's estimate-regret report once the printing/
artwork-mode plans are cost-modeled. Assert bounds soundness across the same
default/non-default prefer and mode coverage the card-space estimator uses.

## Open questions

- Does artwork routing need printing-native leaves projected into artwork space, or
  only card-native leaves expanded? (Decides whether the printing→artwork primitive
  is in scope here or deferred.)
- Is the `[d, P(d)]` interval tight enough for the printing-mode plans' cost curves,
  or does the regret tail demand a `P`-analog that conditions on *which* cards (a
  leaf correlated with the printing distribution — `is:reprint`, set/rarity filters)?
- Store one merged multiplicity table (printing + artwork prefix sums) or two? Both
  are tiny; merge only if the build path is cleaner.
