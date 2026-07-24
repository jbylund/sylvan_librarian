# Engine: banned:/restricted: legality planes (#678)

Follows [docs/workflows/performance-pr-workflow.md](../workflows/performance-pr-workflow.md).
GitHub: #678. Extends the two-exact-plane design already shipped for `LEGAL`
([00667-engine-legality-divergent-carveout.md](00667-engine-legality-divergent-carveout.md), #667/#676) to the
two `expected` values it deliberately left out of scope.

Supersedes [local-engine-legality-postings.md](local-engine-legality-postings.md)'s proposal to cover
banned/restricted with postings lists — see "Why not postings" below for the measurement that
rules it out. That doc predates #667's plane redesign and is now stale; not deleted, just no
longer the plan.

## Measured problem

`FilterExpr::Legality{expected}` only reaches `compile_plane` when `expected == LEGALITY_LEGAL`
(`planes.rs:840`); `banned:`/`restricted:` (`expected == LEGALITY_BANNED/RESTRICTED`) fall through
the `_ => None` catch-all and always take the unindexed `card_pass`/`tri()` scan, regardless of how
selective the rest of the query is. Per `local-engine-legality-bitplanes.md`'s card-level table (31,508
cards, dedup'd by oracle_id), match counts are tiny everywhere: `banned` ranges 0-91 across the 22
formats, `restricted` is nonzero in only 4 (vintage 51, duel 24, tlr 10, oldschool 10). Measured
directly on this corpus, `banned:modern`/`restricted:vintage` cost ~200μs each — indistinguishable
from (or worse than) a fully-plane-promoted `format:X` query matching tens of thousands of cards,
all of it spent scanning cards that can't possibly match. (These specific queries are also called
out as a *control* in #676's own results table — "zero code-path overlap with anything in that
issue" — confirming they were untouched by #667 and are still exactly this slow today.)

## Where the cost is

Same mechanism as the `LEGAL` case before #667: no `compile_plane` arm, no `narrow_rec` arm, `Full`
card-space scan evaluating `tri()` per card.

## Checking the design's own premise first: is banned/restricted card-invariant?

The GitHub issue explicitly flags this as unverified. Checked against the real blue DB
(`sylvan_blue-postgres-1`, same corpus generation `benchmarks/bitplanes/corpus.jsonl` is drawn
from, 31,508 oracle cards / ~97k printings), grouping every printing's per-format legality by
`card_name` and looking for cards where any format's status set includes `banned`/`restricted`
*and* has more than one distinct value among that card's printings:

```sql
WITH expanded AS (
  SELECT card_name, key AS format, value AS status
  FROM magic.cards, jsonb_each_text(card_legalities)
),
per_card_format AS (
  SELECT card_name, format, array_agg(DISTINCT status) AS statuses
  FROM expanded GROUP BY card_name, format
)
SELECT format, statuses, count(*) FROM per_card_format
WHERE array_length(statuses,1) > 1 AND ('banned' = ANY(statuses) OR 'restricted' = ANY(statuses))
GROUP BY format, statuses ORDER BY count(*) DESC;
```

Result: **`banned` never diverges, in any format.** `restricted` diverges in exactly one format,
`oldschool` — 21 distinct card names, 20 with `{not_legal, restricted}` printings and 1 with
`{legal, not_legal, restricted}`. Concretely:

| card_name | scryfall printing | oldschool | vintage |
|---|---|---|---|
| Ancestral Recall | 30th Anniversary Edition | not_legal | restricted |
| Ancestral Recall | Vintage Championship (×4) | not_legal | restricted |
| Ancestral Recall | Unlimited/Alpha/Beta/CE/ICE | **restricted** | restricted |

This is the *identical shape* #667 already solved for `LEGAL`: `oldschool` (like several other
formats) treats certain non-tournament promo prints (30th Anniversary Edition, Vintage
Championship) as ineligible regardless of the card's usual status — the same divergent-printing
pattern documented in `docs/issues/done/00603-engine-card-printing-split.md` (30A/CE/gold-border prints
driving the existing 556-card `legality_divergent` set). It needs no new machinery: `tri()`
(`filter.rs:1223-1237`) already defers to each printing's own word whenever `card.legality_divergent`
is set, for *any* `expected` value, not just `LEGAL` — that flag and its fallback path are already
status-agnostic. `build_bit_planes` already reads `printings[range]` directly, never the
card-level aggregated word, so it's exact for a divergent card regardless of which status is being
asked about. **Conclusion: no repair pattern, no new divergence tracking — reuse both unchanged.**

## Proposed approach: generalize the existing two-exact-planes mechanism, not a new one

The GitHub issue's own framing offers two branches depending on what the invariance check finds:
"simpler than #667" (ordinary tight narrowing) if card-invariant, or "follow #667's pattern" if
printing-varying. Since `restricted` is printing-varying (for `oldschool`) and `banned` is not, a
design that special-cases per status would need *two different mechanisms* for what's otherwise
the same field. `docs/issues/reference-engine-printing-varying-plane-repair-pattern.md`'s escape-hatch
condition — "the field's entire query space is finite and known at build time" — doesn't care
which `expected` value is being asked about; `expected == LEGALITY_BANNED` against 22 formats is
exactly as finite and precomputable as `expected == LEGALITY_LEGAL` was. So: reuse the *identical*
escape hatch for all three indexable values, uniformly, rather than building a second mechanism for
two of them.

For each of `{LEGAL, BANNED, RESTRICTED}` × 22 formats, precompute at build time:

- `exists(V, F)` = `∃p: status(p, F) == V`
- `absent(V, F)` = `∃p: status(p, F) != V`

`legal_exists`/`illegal_exists` already exist (`PLANE_LEGAL_EXISTS`/`PLANE_LEGAL_ILLEGAL`). This
adds four more blocks of `MAX_FORMATS` (32) planes each: `PLANE_BANNED_EXISTS`,
`PLANE_BANNED_ABSENT`, `PLANE_RESTRICTED_EXISTS`, `PLANE_RESTRICTED_ABSENT`. `compile_plane`'s
`Legality` arm and `compile_plane_neg`'s mirror arm both currently guard on
`*expected == LEGALITY_LEGAL` (`planes.rs:840`, `:855`) — generalized to a single
`status_plane_bases(expected) -> Option<(exists_base, absent_base)>` lookup (`LEGAL`/`BANNED`/
`RESTRICTED` → `Some`, anything else, i.e. `NOT_LEGAL` which the parser never emits directly for a
bare `Legality` leaf, → `None`, unchanged fallback behavior). Every other piece of machinery this
touches is already written generically against "is this plane a legality existence projection,"
not against `LEGAL` specifically, so it needs no new logic, only a widened range check:

- `plane_expr_is_existential` — already keys off "is this plane index in a legality range"; widen
  to all 6 blocks.
- `collect_legality_formats` (shared-witness dedup ahead of `And`) — currently keyed on
  `(format, polarity)`. Since each `(status, polarity, format)` triple maps 1:1 to one specific
  plane index anyway, this simplifies to deduping by the plane index itself: any two *distinct*
  legality-plane indices referenced inside an `And` still need to decline (shared-witness), a
  literal duplicate leaf still collapses to one entry. This is strictly more general than today's
  code (which would otherwise need a third dimension bolted onto the existing tuple) and covers new
  cases the old code couldn't even express, like `banned:modern AND restricted:modern` (two
  distinct existence facts about the same format — a divergent card could satisfy both via
  different printings) or `format:modern AND banned:modern` (same shared-witness exposure,
  different statuses).
- `eval_plane_expr_for_printing`/`legality_plane_shift` (row-selection for `unique=card`) — the
  `(shift, is_illegal)` pair generalizes to `(shift, expected, is_illegal)`, comparing the
  printing's status against the specific `expected` value instead of a hardcoded `LEGALITY_LEGAL`.
- `narrow_rec`'s two `Legality`/`Not(Legality)` arms (`lib.rs:2927`, `:2939`) and
  `legal_candidate_bits` (`lib.rs:2369`, renamed `legality_candidate_bits`, taking `expected`) —
  same `status_plane_bases` lookup instead of the `LEGALITY_LEGAL` guard.
- Mode-aware `all_match` gating, the row-selection printing-level walk, and the conjunction fix in
  `card_match_count`/`push_card_matches` (`lib.rs:3387-3560`) are all already written against
  `plane_expr_is_existential`/`existential_plane`, not against which specific format or status —
  once the range check is widened, these need no changes at all.

### Why not postings (the GitHub issue's other proposed option)

The issue's premise — postings are "cheaper to build and reason about" given how rare
banned/restricted matches are — was already checked and rejected for exactly this design space in
#676: `eval_planes` costs O(words), not O(popcount), so a plane addressing a 50-card sparse set
costs exactly the same ~4KB and the same word-sweep as one addressing a 31,000-card dense set. The
postings-vs-plane tradeoff only pays off when storage itself scales with cardinality (subtypes/
keywords: ~1,500 distinct values) or a union's materialization cost scales with result size —
neither applies here. Sizing: 4 new blocks × 32 formats × ~3,944 bytes/plane (31,508-card corpus,
same per-plane cost #676 measured) ≈ 505 KB, against a ~68 MB archive — trivial, and it buys exact
narrowing (no threshold/fallback logic) using code that already exists and is already tested for
the `LEGAL` case. A postings design would also need its own, separate `Not`-inversion story (a
postings list isn't tight/complementable any more than a card-invariant plane would be — see
`RarityIndex`'s dedicated "recompute, don't complement" `-r:x` arm) — the plane design gets this for
free via the existing `PLANE_*_ABSENT` block, exactly as `PLANE_LEGAL_ILLEGAL` does today.

## Acceptance

1. Baseline on `main`: targeted script (new `scripts/bench_legality_banned_restricted.py`, modeled
   on `scripts/bench_legality_divergent.py`) + broad survey (`scripts/survey_queries.py`, same seed
   convention as #676). Memory baseline via `--features alloc-counter` — expect archive growth of
   ~505 KB (4 × 32 new planes), confirm the actual delta rather than assume.
2. Targeted configs: `banned:duel` (91, largest banned set) / `restricted:vintage` (51, largest
   restricted set) / `restricted:oldschool` (the printing-divergent case) and their negations;
   compound `banned:modern t:creature`; the decline shapes `format:modern AND banned:modern` and
   `banned:modern AND restricted:modern` (must still produce correct results, same as today, just
   via the (now-generalized) fallback); controls `format:modern`/`c:g` unaffected (also the
   literal queries #676 already used as its own control, so this doubles as a regression check on
   that PR).
3. Broad survey is not optional (per the workflow and per #676's own history of a regression the
   targeted script missed) — re-run after implementation and confirm no regressions anywhere.
4. Parity tests in `card_engine/src/tests.rs`: mirror the `LEGAL`-focused suite for
   `BANNED`/`RESTRICTED` — narrowing (positive/negated), the `oldschool`-shaped divergent-restricted
   fixture (mirroring the real Ancestral Recall data above), shared-witness decline for same-format
   cross-status leaves (`banned:A AND restricted:A`) in addition to the existing cross-format case,
   row-selection correctness (`unique=card`/`printing`) through the real `run_query` pipeline, and
   flipping `legal_plane_declines_banned_restricted_and_absent_format`
   (`card_engine/src/tests.rs:823-844`) to assert narrowing now succeeds (renamed accordingly) while
   `shift: None` (absent format) still correctly declines.
5. Total-row-count parity on every config, every run.
6. Re-measure (targeted + broad) and iterate until clean; open PR linking #678.

## Results

Measured on `benchmarks/bitplanes/corpus.jsonl` (31,508 cards / 97,206 printings),
`main` @ `4e5501c` vs. this branch, `scripts/bench_legality_banned_restricted.py` (3s window/config):

| config | main | branch | speedup |
|---|---|---|---|
| `banned:duel` (91 matches) | 219μs | 47μs | 4.7x |
| `restricted:vintage` (51) | 200μs | 29μs | 6.9x |
| `banned:modern` (52) | 202μs | 30μs | 6.7x |
| `banned:alchemy` (1 match) | 176μs | 5μs | 35x |
| `-banned:duel` | 246μs | 70μs | 3.5x |
| `-restricted:vintage` | 247μs | 70μs | 3.5x |
| `banned:modern t:creature` | 129μs | 15μs | 8.6x |
| `restricted:vintage c:u` | 76μs | 16μs | 4.8x |
| `banned:duel or restricted:vintage` | 322μs | 58μs | 5.6x |
| `restricted:oldschool` (the divergent case, 22 matches) | 186μs | 18μs | 10x |
| `-restricted:oldschool` | 248μs | 73μs | 3.4x |
| `banned:modern`, `unique=printing`/`artwork` | 229/230μs | 59/55μs | 3.9x/4.2x |
| `banned:modern restricted:modern` (shared-witness decline) | 155μs | 7μs | still declines to the fallback, correctly, and the fallback itself is now fast (each leaf individually narrows via its own exact plane before the compound is declined) |
| `format:modern banned:legacy` (cross-format decline) | 169μs | 14μs | same — correct decline, faster fallback |
| `format:modern`/`c:g`/`name:soldier` (controls) | 67/59/33μs | 67/59/33μs | unaffected |

Notably, `banned:modern` (30μs, 0.16% density) ends up *faster* than the already-shipped
`format:modern` (67μs, 70% density), despite using the identical word-sweep mechanism — the
popcount-skip walk (#634/#667) already short-circuits whole zero words, so cost scales with
*nonzero* words, not a fixed `words_per_plane` count. This is direct evidence against the postings
alternative floated during review: the plane path isn't a fixed-cost sweep a postings gather could
beat here, it's already adaptive to sparsity, and it's landing near the apparent per-query floor
(`banned:alchemy`'s 1-match case at 5μs looks close to that floor already).

Broad survey (`scripts/survey_queries.py --seed 678`, 520 queries): 0 systematic regressions. A
diff of every individual query found 9 queries (none touching `banned:`/`restricted:`/`format:`)
shifted >10%/>10μs slower and 7 shifted similarly faster — a symmetric spread consistent with
ordinary run-to-run measurement noise, not a change caused by this PR.

Memory: archive grew by 504,824 bytes (493 KiB) on this corpus, matching the ~505 KiB estimate
(4 new plane blocks × 32 formats × ~3,944 bytes/plane) almost exactly. Trivial against the ~68.5 MB
archive (0.7% growth).

Total-row-count parity: identical across builds for every targeted config.

## Related

- #667/#676 — the two-exact-plane design this generalizes; explicitly scoped banned/restricted out
- `docs/issues/local-engine-legality-postings.md` — superseded proposal (postings-based), kept for history
- `docs/issues/reference-engine-printing-varying-plane-repair-pattern.md` — the escape-hatch condition this
  reuses (finite, enumerable, build-time-precomputable query space)
- `docs/issues/done/00603-engine-card-printing-split.md` — origin of the 556-card `legality_divergent`
  flag and the 30A/CE/gold-border divergent-printing pattern this issue's `oldschool` case matches
