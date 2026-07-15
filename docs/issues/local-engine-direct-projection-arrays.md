# Engine: direct printing→card / printing→group projection arrays

Status: **`printing_to_card` shipped** 2026-07-14, no GitHub issue yet. Surfaced investigating
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md)'s crossover axis 4
(does `unique=card`'s exact `total` cost too much to make its fast-path win moot?) — turned into a
standalone win independent of that project, landed first since it changed the baseline costs that
doc's crossover math depends on.

**Why build `printing_to_card` before the fastpath project picks a direction, rather than
deferring it alongside `printing_to_global_group`**: it pays for itself either way that project
goes. It has a real (if narrow) win today for `cards_of_printings`' broad-k `Vec` path. It's also
load-bearing for Idea 1 specifically, if that's the direction chosen: Idea 1 walks the order-by
permutation, visiting printings in permutation order rather than printing-id order, so the
monotone-cursor trick (which needs ascending printing ids) is never available there — every
matched printing's card-identity check during that walk would otherwise pay a binary search, the
exact access pattern the direct array wins decisively at. Idea 2 (`PrintingRangeBits`) scatters
into a card bitmap upfront — the "already a bitmap" case benchmarked below, where the direct array
is a wash — so this is neutral to Idea 2, not a loss either way.

## Problem

Before this change, projecting a printing-id set up to card space (`cards_of_printings`,
`lib.rs:2392-2407`) had no materialized `printing_id -> card_id` mapping. It derived the card from
`offsets` (a compact `Vec<(start_printing_idx)>` per card) two different ways depending on size:

- **k ≤ 1024**: `offsets.partition_point(...)` per printing — a binary search, `O(log n_cards)` each.
- **k > 1024**: scatter into a printing-space bitmap, then walk it with `printing_bits_to_card_bits`'s
  monotone cursor (introduced in [#637](https://github.com/jbylund/sylvan_librarian/pull/637),
  which also added the 1024 split) — `O(k + n_cards)`, no per-posting search, but two full passes
  (build the printing bitmap, then walk it) and a `bitmap_card_ids` materialization if a `Vec` is
  needed. Checked #637 for a reason a direct array was rejected in favor of this — found none; it
  reads like the monotone cursor was simply the improvement tried over binary search, not a choice
  made against a direct array specifically.

Prototyping an analogous `groups_of_printings` (artwork-group dedup, for
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md)'s `unique=artwork`
question) hit this same shape, but *without* the monotone-cursor option: a card's local
artwork-group ids aren't ascending within its printing range (they can interleave, e.g. `0, 1, 0,
2`), so the `printing_bits_to_card_bits` trick doesn't transfer. The only projection available was
per-printing binary search — even past k=1024, unlike `cards_of_printings`.

## Finding: a direct array wins when you already have an id list, not when the input is a bitmap

Benchmarked in `card_engine/src/bench_card_dedup.rs`
(`cargo test --release bench_card_dedup -- --ignored --nocapture`), synthetic offsets at real
corpus scale (31,508 cards / ~97-115k printings / ~46-55k groups), all times ns/match, best-of-50:

| k (broad) | `cards_of_printings` (old) | direct array, from id list | `groups_of_printings` (binary search) | direct array, from id list |
|-----------|---------------------------:|----------------------------:|----------------------------------------:|-----------------------------:|
| 7,267 (6%) | 3.84 | 1.40 | 7.63 | 1.31 |
| 60,166 (52%) | 3.74 | 1.79 | 7.94 | 1.65 |
| 95,724 (83%) | 3.19 | 1.55 | 7.30 | 1.54 |

Same k values, but isolating just the "cursor vs. direct array" step when the input is *already a
bitmap* (`Candidates::PrintingBits`, no id list to exploit) — the other two call sites of
`printing_bits_to_card_bits`:

| k (broad) | cursor | direct array lookup |
|-----------|--------:|----------------------:|
| 7,267 (6%) | 2.16 | 2.57 |
| 60,166 (52%) | 1.75 | 1.58 |
| 95,724 (83%) | 1.50 | 1.52 |

These are a wash — the earlier ~2x win isn't "direct array beats the monotone cursor at the lookup
step." It's "when you already have a sorted id list, scattering it straight into the output space
and skipping the intermediate printing-bitmap round-trip is cheaper than building that bitmap just
to re-extract the same ids from it." `cards_of_printings`' broad-k path did the latter (built a
printing bitmap from an id list it already had, purely to hand it to `printing_bits_to_card_bits`)
— that round-trip was the actual waste, not the cursor.

`groups_of_printings`' ~4.5-5.9x win is real, in isolation, for the mechanism it replaces (binary
search per printing — no monotone-cursor option ever existed for groups). **But that mechanism
isn't used anywhere today.** Checked the real `unique=artwork` implementation
(`card_match_count`'s `Mode::Artwork` arm, `lib.rs:3456-3470`, and `run_query_streamed`,
`lib.rs:4025-4092`) — it has no global printing-set-to-group-set projection at all. It computes
counts per candidate *card*, walking that card's own small printing range and OR-ing bits into a
tiny per-card local `seen_words` scratch buffer (typically ~3 printings, cheap regardless).
`groups_of_printings` was invented answering a hypothetical for
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md)'s Idea 1 (walking the
order-by permutation, which *would* need incremental dedup-while-walking) — it is not a fix for
any measured cost in the current architecture, and claiming it would broadly speed up
`unique=artwork` queries today was wrong. The real, already-documented cost differential for
`Mode::Artwork` vs `Mode::Card` is unrelated to dedup: the `all_match_known` short-circuit is
deliberately disabled for `Mode::Artwork` (`lib.rs:4064-4071`, a measured codegen regression when
enabled unconditionally), and `Mode::Artwork` can't return at the first matching printing the way
`Mode::Card` does, since a later printing could introduce a new distinct group. Neither is
something a lookup array changes.

`printing_bits_to_card_bits` **itself stays.** It's still the right (and already-fast) mechanism
for its other two call sites (`Candidates::into_cards`/`into_card_space`'s `PrintingBits` arms),
which start from a bitmap with no id list behind it. Only `cards_of_printings`' broad-k path
stopped calling it, in favor of scattering the id list it already holds directly.

## Shipped: `printing_to_card` in the archive

Added `printing_to_card: Vec<u32>` to `CardIndexes` (`lib.rs`), built once at write time by
`build_printing_to_card(&offsets)` — a single linear pass over `offsets`, no hash table. Persisted
in the archive (not a per-process cache): mmap pages are shared read-only across every worker
process via the OS page cache, so the ~380KB array costs one physical copy total regardless of
worker count, and the O(n_printings) build cost is paid once by the writer, not once per reader.
Bumped `ARCHIVE_FORMAT_VERSION` (`20260725` → `20260726`).

`cards_of_printings` now uses it in **both** branches — not just broad-k:

```rust
fn cards_of_printings(offsets: &AOffsets, printing_to_card: &AOffsets, printing_ids: &[u32]) -> Vec<u32> {
    if printing_ids.len() > 1024 {
        let n_cards = offsets.len().saturating_sub(1);
        let bits = scatter_bits(printing_ids.iter().map(|&p| u32::from(printing_to_card[p as usize])), n_cards);
        return bitmap_card_ids(&bits);
    }
    let mut out: Vec<u32> = Vec::with_capacity(printing_ids.len());
    for &p in printing_ids {
        let card = u32::from(printing_to_card[p as usize]);
        if out.last() != Some(&card) {
            out.push(card);
        }
    }
    out
}
```

The small-k branch keeps its shape (push + adjacent-dedup, no bitmap allocation) but swaps
`partition_point` for the direct array too — unconditionally cheaper, no downside, no need for a
separate crossover decision between the two lookup mechanisms.

`printing_to_global_group`/`groups_of_printings` remain **deferred, not shipped** — see "Finding"
above. Nothing in the current codebase would consume them. Revisit only if/when
[local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) actually commits to
building Idea 1; the benchmark numbers above are recorded there for that future decision.

**Verified, not just argued:**

- `cards_of_printings_matches_naive_projection_across_sizes` (`tests.rs`) — differential test
  against an independent reference oracle (`partition_point` on `offsets`, applied uniformly
  regardless of size — the mechanism the old small-k path itself used), 40 seeds × 9 k values
  straddling the 1024 small/broad split, both branches. Confirmed it actually catches a bug: an
  injected off-by-one in the broad-k lookup failed the test immediately (seed=0, k=1025), reverted
  before landing.
- `cards_of_printings_maps_and_dedups` (existing unit test) still passes unchanged.
- Full suite: **117 passed** (debug and release), 8 ignored (kernel benchmarks). `cargo clippy`:
  37 warnings, identical set to baseline — no new warnings from this change.
- Real benchmark against the shipped implementation (not just the prototype):
  `cards_of_printings` now runs at ~1.7-1.9ns/match across the same real-corpus-shaped sweep,
  confirming the ~2x win holds in the actual code, not just the exploratory version.

## Open questions

- Does the small-k binary search path in `cards_of_printings` still win below some k, or does the
  direct array dominate unconditionally once it exists? **Resolved**: the direct array is
  unconditionally cheaper (O(1) vs O(log n_cards) per lookup, no downside), so both branches now
  use it — the only remaining question the 1024 split answers is bitmap-scatter vs. push+dedup,
  unrelated to the lookup mechanism.
- Quantify the real impact: how often does a real query actually land k in the (1,000, ~25% of
  index] `Vec`-path window for `price_usd`/`collector_number`/`released_at`? Price's own
  distribution rarely does (see fastpath doc's selectivity sweep) — worth checking
  `collector_number`/`released_at` against the real corpus. Not yet done — still open.
- `reload_commit` cost of building `printing_to_card` — expected cheap (one linear pass), not yet
  measured against a real reload.

## Plan

- [x] Differential test: new `cards_of_printings` (both branches) vs. an independent reference
      oracle, byte-identical output — confirmed to catch a real injected bug.
- [x] Add `printing_to_card` to the archive, bump `ARCHIVE_FORMAT_VERSION`.
- [x] Ship `printing_to_card` in `cards_of_printings`, both branches.
- [x] Re-benchmark `cards_of_printings` against the real shipped implementation (not just the
      prototype) — win confirmed (~1.7-1.9ns/match).
- [ ] Measure `reload_commit` cost of building `printing_to_card` against a real reload.
- [ ] Quantify how often real `collector_number`/`released_at` queries land in the Vec-path
      selectivity window, to size the practical impact beyond price (which rarely does).
- [ ] Once real-world impact is quantified, fold the updated baseline numbers into
      [local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md)'s crossover
      axis 4.

## Related

- [local-engine-broad-range-fastpath.md](local-engine-broad-range-fastpath.md) — where this was
  discovered; crossover axis 4 depends on this having landed.
- [#637](https://github.com/jbylund/sylvan_librarian/pull/637) — introduced `cards_of_printings`'
  1024 split and `printing_bits_to_card_bits`'s monotone cursor.
