# `is:vanilla` verifies every card and narrows none of them, and the answer is one bit per card

`is:vanilla` stopped being a text query on 2026-08-17 (`37ce298` on #927; `11c0f6b` in the Cloudflare
port). `vanilla` moved out of `_DERIVED_EXPANSIONS` into `ENGINE_IS_VALUES` in `api/parsing/rewrite.py`
and its `src/parser/rewrite.ts` mirror, so it now expands to **nothing**: it reaches the engine as a
`card_is_tags` leaf and `filter.rs` intercepts it as `FilterExpr::VanillaFace`, which reads `card_types`
(creature present, land absent) and then exactly one string — the front face's `oracle_text_id`, or the
card's own oracle text when it has no faces, blank-tested after reminder text comes off. It answers the
same set api.scryfall.com does, set for set.

This doc used to argue that `is:vanilla` was `t:creature AND oracle_text = ''` and wanted an index over
empty oracle text. **The fix is dead and the question is not**, so both halves are rewritten below, and
every number in the old version is gone rather than carried: they described a query plan that no longer
exists.

## The empty-text index would not answer this predicate at all

"Empty oracle text" is neither necessary nor sufficient for `is:vanilla`, and each direction has a
witness measured against api.scryfall.com:

- **Not sufficient.** `Dryad Arbor` prints no rules text and is *not* vanilla, because a land never is.
  `t:creature -o:/./ -is:vanilla` is exactly 1 there, and that is the card.
- **Not necessary.** `Icehide Golem` ("({S} can be paid with one mana from a snow source.)") and
  `Infinity Elemental` ("(This creature has INFINITE POWER.)") both print a non-empty `oracle_text` and
  both are vanilla, because reminder text comes off before the blankness test.

So an index keyed on `oracle_text = ''` would have to be intersected with the type mask and then
*re-verified* against the stripped text anyway — it would narrow to a set that is both missing rows and
holding rows it must not. There is nothing to salvage in that shape. The same disposes of the old
"it would also serve any user-written `o:""` emptiness test" justification: that test is a different
predicate from this one, and no longer shares a plan with it.

## What survives: nothing narrows, and the estimator says exactly that

The predicate has no index of any kind behind it:

- `narrow_rec` (lib.rs) has **no arm** for `VanillaFace`; it falls to the closing `_ => None`, so the
  leaf contributes no candidates and the plan verifies the whole corpus.
- `estimator.rs` answers `unknown(n)` — `lo: 0, est: n / 2, hi: n` — grouped with `SingleSet` and
  `PrintedNamePresent` under the comment "a full-scan verify, and 'unknown' says exactly that".
- `filter.rs` prices it at `TEXT_SCAN_NS100`, the per-card text-scan tier, deliberately: the mask
  rejects most cards before the string read, but the model must not under-charge a predicate on the
  strength of the branch it usually takes.

The shape of the complaint is the one thing the old version of this doc had right — a whole-corpus
verify for a small answer — and the estimate is still far off, just differently: `n / 2` = **19,313**
against a true **696**, 28× over.

## Measured

Native `card_engine` harness over the ten-partition rkyv store the Cloudflare port builds from this
engine (format `2026081703`, **38,626 cards / 116,712 printings**), `release` profile, one whole-corpus
query = the ten partitions summed, `unique=card orderby=edhrec limit=60`, 400 interleaved reps.

**These are not comparable to any other number in `docs/issues/`**, which come from `scripts/bench_*.py`
on the Postgres corpus (31,508 cards / 97,206 printings, and no extras class). Compare them only to each
other. The text half also differs by a hair between the two trees — upstream walks paren depth over the
face string, the port routes the same string through `strip_reminder_text` — so these are one string
read either way, at the port's constants.

The machine was running a concurrent store rebuild throughout (load average ~7). Minima are the least
contended observation of each case and are what the argument rests on; one uncontended interval
produced figures ~5× lower **across every case alike**, so treat the absolute microseconds as an upper
bound and the comparisons between rows as the finding.

| query | matching cards | min | median |
| --- | --: | --: | --: |
| `wm:mps` (postings lookup, for contrast) | 5 | **351.5 µs** | 514.8 µs |
| `o:flying` (trigram narrowing) | 4,849 | 416.2 µs | 777.8 µs |
| whole-corpus floor (match-all) | 38,626 | 1,175.6 µs | 2,720.3 µs |
| `t:creature` (type plane) | 20,098 | 1,176.0 µs | 2,593.5 µs |
| `is:unique` (full-scan verify, one bool) | 20,690 | 1,913.7 µs | 3,316.3 µs |
| **`is:vanilla`** | **696** | **3,232.0 µs** | 4,689.5 µs |
| `is:vanilla -is:extra -is:variation` (as served) | **363** | 2,673.0 µs | 4,797.5 µs |

Divide by ten for the per-partition figure a single Durable Object pays: **323 µs** for `is:vanilla`.

The controlled comparison is `is:unique`, which takes the *same* acquire path — no narrowing arm,
`unknown(n)`, whole-corpus verify — and differs only in the verify: one bool on the card against a mask
test plus a string read. `is:vanilla` costs **1.7×** it while returning **30× fewer** cards, i.e. while
doing strictly less ordering and paging work downstream — so the 1,318 µs between them is a *lower*
bound on what the verify costs.

Two structural facts sit behind that number, both measured on the same store: **20,073 of the 38,626
cards** (52%) pass the type mask and go on to read a string, and **696** survive the text test — 363 of
them in the default lane, the rest excluded as extras. The served query is not cheaper for narrowing
either: `-is:extra` composes through the `card_is_tags` hybrid index and still leaves nearly every card
to verify.

## The fix is a card-space bit, not a text index

`VanillaFace` is the ideal shape for the cheapest index the engine has:

- **Card-invariant.** Both classifiers already say so — `leaf_compares_printing_field` (filter.rs) and
  `has_printing_varying_leaf` (estimator.rs) answer `false` for it, "faces and their texts are oracle
  data, every printing of the card prints the same ones" — so one bit per *card*, not per printing.
- **Total two-valued.** Its `tri` arm is an unconditional `tri_bool`: never `Null`, never
  `PrintingDep`. That is the property that makes a complement exact, so `-is:vanilla` narrows too.
- **Sparse.** 696 of 38,626 cards, 1.8%.

At that density a card-space postings list of u32 ids is **~2.8 KB** and a bit in `BitPlanes`
([#630](./done/00630-engine-card-bitplanes.md), "card space: transposed low-cardinality dims") is
**~4.7 KB**. Postings are the smaller at 1-in-55; the plane is the one that composes with the type
plane the query already touches. Which of those wins is a bench, not an argument — but either way the
query stops touching card structs.

Two entries have to move with it, and neither is automatic: `never_null` (lib.rs) is deliberately tiny
— today it holds `NameLower`/`NameCollated` and nothing else — and `is_total_two_valued` (estimator.rs)
has no `VanillaFace` arm either. `VanillaFace` qualifies for both by construction, but until it is
listed the `Not` complement stays loose and `-is:vanilla` keeps scanning. Add each with the line of
code that proves totality, which is the `tri_bool` arm itself.

**Rejected alternative: store `vanilla` as an is-tag at import.** It would need no new index kind —
`card_is_tags` is a `HybridTagIndex` with a narrowing arm that already survives `Not` — but is-tags live
on the **printing**, so a card-invariant fact would be written 3× over (116,712 printings against 38,626
cards) and the importer would have to re-derive Scryfall's rule, which is exactly the duplication that
put this predicate in the engine in the first place.

The price is the usual one: a build-time pass, one archive-format bump, and a store rebuild.

## Related but different

- **`is:permanent` is a separate open shape.** It still expands to a 6-way `Or` over `card_types`, and
  it costs **2,679.5 µs** (min, same harness and corpus, 27,829 cards) — more than twice `t:creature`'s
  1,176.0 µs, despite every disjunct alone being a plane. A plane-composable `Or` falling back to the
  general path is its own finding and is not covered here.
- **`o:""` / `ft:""` are not this predicate.** `TextContains` evaluates `s.contains(word)`, and every
  non-null string contains the empty one — so those match nearly everything rather than failing to
  narrow. They were the disambiguation this doc needed when it was about `= ''`; they are now just a
  neighbouring shape.
- **[local-engine-layout-postings.md](./local-engine-layout-postings.md)** is the same family one step
  out: a card-invariant field with no index and no `narrow_rec` arm, deprioritized on traffic weight.

## Status

Not implemented. The predicate is correct and shipped; this is only about the plan it runs under.

`is:` is absent from `REALISTIC_FAMILY_WEIGHTS`, so the realistic-traffic weight of `is:vanilla` is
unmodelled and 0% by construction — the same caveat that deprioritized the layout postings, and the
reason this is a sized proposal rather than scheduled work. What it has that layout does not is that
`is:vanilla` is a commonly-typed Scryfall idiom and the whole predicate collapses to one bit.
