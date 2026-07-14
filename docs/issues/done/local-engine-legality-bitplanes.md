# Engine: legality bitplanes (#630 phase 2)

Successor to #630 phase 1 (PR #633, colors/types) and #634 (permuted bitmap order
phase). Supersedes [local-engine-legality-postings.md](local-engine-legality-postings.md) for
the `legal` dimension; banned/restricted stay postings-eligible (unimplemented,
out of scope here — see that doc).

## Problem

Legality filters (`f:modern`, `f:commander`) have no index support at all: the
card pass scans all 31.5k cards checking a 2-bit word. `f:modern` sits at 0.259 ms
on the post-#632 baseline. #634 explicitly cites `f:modern t:creature power>3`
becoming fully index-resolved once this lands.

## Data (card-level, deduplicated by oracle_id, n=31,508, 2026-07-09 blue DB)

| format | legal | legal% | banned | restricted |
|---|---|---|---|---|
| commander | 31,451 | 99.8% | 50 | 0 |
| oathbreaker | 31,450 | 99.8% | 51 | 0 |
| vintage | 31,439 | 99.8% | 11 | 51 |
| legacy | 31,421 | 99.7% | 80 | 0 |
| duel | 31,386 | 99.6% | 91 | 24 |
| modern | 22,264 | 70.7% | 52 | 0 |
| tlr | 18,407 | 58.4% | 62 | 10 |
| penny | 15,060 | 47.8% | 0 | 0 |
| gladiator | 14,903 | 47.3% | 7 | 0 |
| timeless | 14,869 | 47.2% | 0 | 4 |
| competitivebrawl | 14,649 | 46.5% | 8 | 0 |
| pioneer | 14,630 | 46.4% | 31 | 0 |
| brawl | 14,622 | 46.4% | 35 | 0 |
| historic | 14,580 | 46.3% | 77 | 0 |
| predh | 11,514 | 36.5% | 40 | 0 |
| paupercommander | 10,736 | 34.1% | 4 | 0 |
| pauper | 10,703 | 34.0% | 37 | 0 |
| premodern | 5,375 | 17.1% | 28 | 0 |
| standardbrawl | 4,714 | 15.0% | 0 | 0 |
| standard | 4,702 | 14.9% | 10 | 0 |
| future | 4,672 | 14.8% | 10 | 0 |
| alchemy | 3,684 | 11.7% | 1 | 0 |
| oldschool | 737 | 2.3% | 0 | 10 |

Divergent-legality cards (printings disagree on status): **556**, confirmed against
the existing `legality_divergent` flag. All 556 touch every format; for 8 of 22
formats (modern, standard, pioneer, alchemy, penny, future, competitivebrawl,
standardbrawl) the divergence is *entirely* legal vs. not_legal — zero banned
involvement, which rules out patching via banned postings alone.

## Design

### Storage

Reuse the bigram-index crossover rule (PR #639): plane costs
`words_per_plane(31508)*8 = 3944` bytes flat; a u16 posting costs 2 bytes/entry;
crossover at 1,972 entries (6.26%). Every format's `legal` count clears this
except oldschool (737). Given the marginal saving (~2.5 KB against a >70 MB
archive) isn't worth a second postings/plane split tier, plane `legal` uniformly
for all formats. `banned`/`restricted` stay unindexed for now (unchanged from
today — see the postings doc for that follow-up).

Add a fixed 32-wide plane range (`MAX_FORMATS`, already the shift-registry's
hard cap in `legality.rs`) rather than sizing dynamically off the live format
count — unused format slots are permanently-zero planes, consistent with the
existing `shift: None` "matches nothing" semantics. Total: 32 × 3,944 B ≈ 126 KB.

The divergent set itself (556 cards) is postings, not a 33rd plane —
consistently applying the same crossover rule rather than the plane chosen
for uniformity in an earlier pass of this doc: 556 is well under the 1,972
crossover, `u16` postings cost 1,112 B against the plane's fixed 3,944 B, and
scattering ~556 individual bits into a format's candidate mask measured
identically to OR-ing a plane in (0.96–1.01× across all 21 legality
benchmark configs — the operation is sub-microsecond either way, dwarfed by
the rest of the query). `build_divergent_ids` (`planes.rs`) builds it once,
shared across every format's `legal_candidate_bits` call.

### The divergent-card wrinkle

Colors/types compile through `compile_plane`/`split_planes` — the "exact
consumption" path that drops the node from the residual filter entirely
(residual becomes `FilterExpr::True`, so `card_pass` never re-verifies). That's
safe only because `ColorCmp`/`TypeCmp` are two-valued for every card. `Legality`
is not: it returns `Tri::PrintingDep` for the 556 divergent cards
(`filter.rs:1219-1233`), which is exactly the case the existing `compile_plane`
safety comment already excludes. Routing `Legality` through that path would
require a bit-patching correction after the fact — and an earlier version of
this plan tried exactly that, plus a variant that patched bits from the
`banned` postings list, which the divergence table above rules out (most
formats' divergence never touches `banned`).

Neither is needed. `narrow_rec` (lib.rs) already has a *second*, independent
narrowing mechanism — used today for rarity and the devotion-superset arm
(`lib.rs:2240-2251`) — that only shrinks the candidate list fed into the
iteration loop; it never removes the node from the residual, so the existing,
untouched `card_pass` still verifies every candidate, `Tri::PrintingDep`
included. Two new leaf arms there, built as **superset** (never-false-negative)
candidate masks:

- `f:x` → `legal_x[shift] OR divergent` — every non-divergent truly-legal card
  is included (canonical status is ground truth for them); every divergent
  card is included unconditionally, regardless of its own canonical status, so
  the existing per-printing residual walk always gets a chance to run on it.
- `-f:x` (matched as its own `Not(Legality)` leaf shape, not the generic
  complement) → `NOT(legal_x[shift]) OR divergent` — same superset argument,
  mirrored.

Both arms return `Narrowed::loose` (candidates only, not exact) — matching the
Or/And composition rules already in place (intersection/union of superset sets
is always a safe superset), and matching why the *generic* Not-complement path
in `narrow_rec` would correctly refuse to handle this on its own: it requires a
`tight` child, and the OR'd-with-divergent mask has false positives among
divergent cards by construction.

`legal_x[shift]` itself is built as **"legal AND not divergent"**, not raw
status — a pure two-valued exact predicate (never a false positive) rather
than something merely corrected downstream by the OR. The OR-with-divergent
narrowing formula above produces an identical candidate mask either way (De
Morgan's), so this costs nothing today. It matters for **#634**: that issue's
exactness-flag/all_match promotion wants per-source exact-vs-advisory
classification, and `legal_x` is already exactly the shape it needs (a pure
exact card-space source), with the divergent postings (`build_divergent_ids`)
as the one shared, tiny (556-card) advisory carve-out every legal-format
source needs verified — "popcount the plane, verify only the divergent
intersection, add the two counts" instead of card_pass over the whole
narrowed set. Building that total path is out of scope here (#634's own
machinery doesn't exist yet), but this layout is the natural input to it
rather than something #634 would need to rebuild.

No changes to `filter.rs`'s `Legality` eval, `card_pass`, or the
printing-dependent residual walk — this is a purely additive narrowing layer.

## Tasks

- [x] `pub(crate) const MAX_FORMATS` in `legality.rs` (currently private)
- [x] 32-wide legal-plane range in `planes.rs`/`build_bit_planes` (`legal_x` =
      "legal AND not divergent" — see #634 forward-compat note above); divergent
      cards as a `Vec<u16>` postings list (`build_divergent_ids`), not a 33rd
      plane — below the plane/postings crossover, and measured identical at
      runtime to a plane OR (0.96–1.01× across 21 configs; see Results).
      `ARCHIVE_FORMAT_VERSION` bumped (`CardIndexes` layout changed, archives
      must rebuild)
- [x] Two `narrow_rec` leaf arms in `lib.rs`: `Legality{expected: LEGAL}` and
      `Not(Legality{expected: LEGAL})`, both loose, scattered with the divergent postings
- [x] Parity tests: every format × {legal, -legal} vs. brute-force `tri()`,
      full corpus, with the 556-card divergent set as the dedicated fixture
      (`legal_plane_narrowing_preserves_divergent_printing_correctness`,
      deliberately with the preferred printing on the "wrong" side of the
      status split, to stress the narrowing's superset property)
- [x] Consistency tests: `banned:`/`restricted:`/absent-format unchanged
      (unindexed, same results as before)
- [x] Benchmarks: broad legal% formats (commander/legacy/vintage), mid (modern/
      pioneer/standard), narrow (oldschool/alchemy), negated forms, the #634
      composite (`f:modern t:creature power>3`), divergent-touching control,
      broad-survey regression check — see Results below
- [x] Fixed a regression surfaced by the benchmark: mixed conjunctions where a
      *tighter* exact plane (colors/types) co-occurs with a *broader* legal-format
      residual (e.g. `c:g t:creature f:modern`) were paying an O(residual-popcount)
      materialize-then-retain pass in `run_query`'s plane/candidate intersection.
      Fixed by AND-ing two card-space bitmap candidate sets directly (O(words),
      independent of either side's popcount) instead of materializing one to a
      `Vec` first — a general improvement to the shared intersection path, not
      legality-specific (see `run_query` in `lib.rs`)

## Results

Engine-vs-engine, `scripts/bench_bitplanes.py`, 0.5s timed window/config,
main @ 26fbebf, corpus 97,206 printings / 31,508 cards. All 48 configs: **totals
identical** between baseline and new build (parity check).

| query | before | after | speedup |
|---|---|---|---|
| `f:oldschool` | 0.232 ms | 0.079 ms | **2.93×** |
| `f:alchemy` | 0.206 ms | 0.098 ms | **2.11×** |
| `f:standard` | 0.214 ms | 0.105 ms | **2.05×** |
| `f:pauper` | 0.256 ms | 0.136 ms | **1.88×** |
| `f:pioneer` | 0.286 ms | 0.161 ms | **1.78×** |
| `f:modern` (card) | 0.245 ms | 0.190 ms | 1.29× |
| `f:modern` (printing) | 0.260 ms | 0.201 ms | 1.29× |
| `f:commander`/`f:legacy`/`f:vintage` | ~0.194-0.197 ms | ~0.196-0.200 ms | ~1.0× (99.7%+ legal; almost nothing to narrow) |
| `-f:commander` | 0.247 ms | 0.077 ms | **3.21×** |
| `-f:modern` | 0.301 ms | 0.154 ms | **1.96×** |
| `-f:standard`/`-f:oldschool` | ~0.24-0.27 ms | ~0.24-0.28 ms | ~1.0× (query is itself broad; narrowing can't help) |
| `c:g t:creature f:modern` | 0.095 ms | 0.082 ms | 1.15× |
| `f:modern t:creature power>3` (#634 composite) | 0.133 ms | 0.116 ms | 1.15× |
| `banned:modern`/`restricted:vintage` | ~0.20-0.21 ms | ~0.20-0.21 ms | ~1.0× (unindexed by design, unchanged) |
| non-legality controls (`c:g`, `t:creature`, `r:mythic`, `cmc>6`, …) | — | — | within ±5% noise, re-run confirmed |

Broad formats (commander/legacy/vintage, ≥99.6% legal) don't move — there's
almost nothing to narrow away, and the OR-with-divergent mask is barely
smaller than the full corpus. Everything from modern (70.7% legal) down
gets a real win, growing as legal% shrinks, exactly as expected from the
narrowing-fraction argument. Negation shows the same pattern mirrored.

## Related

- #630 — parent issue; phase 1 (colors/types) shipped as #633
- #634 — cites `f:modern t:creature power>3` as the motivating composite case
- [local-engine-legality-postings.md](local-engine-legality-postings.md) — banned/restricted
  postings, still open, unaffected by this
- [local-format-legality-search.md](local-format-legality-search.md) — separate, unimplemented
  proposal to change `f:x` semantics to "playable" (legal OR restricted); this
  plan keeps today's exact-legal-only semantics unchanged, so that proposal
  would layer on top later, not conflict
