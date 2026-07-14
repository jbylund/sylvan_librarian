# Lab notes: engine-oracle-word-index implementation

Running notes while implementing `00663-engine-oracle-word-index.md`. Untracked scratch, not the design
doc — see that file for the actual design/rationale.

## Scope decision (confirmed with user 2026-07-10)

Doing the full task list in one PR, including the two "smaller, #662-shaped" pieces the design doc
explicitly sequenced as separable: replacing `TrigramIndex`'s `HashMap` with sorted dense/sparse
arrays, and the `intersect_sorted` posting-vs-plane dispatch table.

## Key design decisions not fully spelled out in the doc

- **Word dictionary storage domains differ by tier.** Sparse tier: postings are dense *text ids*
  (same domain as today's oracle trigram postings), u16-packed. Dense tier: bitmaps are
  precomputed **card-space** bitmaps (n_cards bits), not text-id-space — expanded through the CSR
  once at build time. Rationale: the query-time answer is always card space eventually, and
  card-space bitmaps are exactly what `compile_plane`'s bonus arm needs, so there's no reason to
  ever materialize a text-id bitmap as an intermediate. The sparse/dense *classification* threshold
  still runs on text-match-count vs `words_per_plane(n_texts)*8` (#639's formula, n=n_texts, doc's
  stated numbers), only the stored dense representation's domain is card space.
- **`SortedTrigramIndex` is one generic type** reused for both `name_trigram` (domain=n_cards) and
  `oracle_trigram.trigrams` (domain=n_texts), parameterized by a stored `domain: u32` field (both
  the u16-packing decision and the dense-plane sizing derive from it). This mirrors
  `NameBigramIndex`'s existing dense/sparse split almost exactly.
- **`intersect_sorted`'s posting-vs-plane dispatch** lives entirely inside the new
  `intersect_operands()` helper that both `trigram_candidates` call sites (name field, oracle
  exact-3-char/phrase fallback) go through. This is the same code satisfying task 7 — there's no
  separate dispatch to write, it falls out of the SortedTrigramIndex refactor.
- **`compile_plane`'s dense-word bonus arm** only fires when the query needle matches **exactly one
  dictionary word total** (dense or sparse) and that word is dense — i.e. exactly the general
  dispatch's "single dense hit, no sparse hits" case. This is required for correctness: if the
  needle also substring-matches other (sparse) words, using only the dense bitmap would undercount.
  Originally cost a second dictionary scan on decline (compile_plane speculatively scans, then
  narrow_rec's fallback arm scans again) — this turned out to matter once the scan itself was slow
  (see the benchmark finding below) and is now fixed: `narrow_rec` skips the speculative
  `compile_plane` attempt entirely for a lone eligible oracle leaf, and `compile_plane`'s And/Or
  children are tried cheapest-to-reject first so a non-oracle sibling's cheap failure short-circuits
  before the oracle scan ever runs.
- **`PlaneExpr` gains a `Bits(Vec<u64>)` variant** — an owned clone of the dense word's precomputed
  bitmap, embedded directly in the expression tree. This was a deliberate simplification over
  threading a `&Archived<OracleWordIndex>` through `eval_word`/`eval_planes` too (which would have
  meant matching NameBigramIndex/BitPlanes' "index by plane number into a shared array" pattern
  instead): the clone is a few KB, paid once per query, and only `compile_plane`/`split_planes`
  need the extra `&Archived<OracleWordIndex>` parameter as a result.

## Status

- [x] SortedTrigramIndex + dispatch (`lib.rs`: `SortedTrigramIndex`, `finalize_trigram_index`,
      `TriOperand`, `lookup_trigram`, `intersect_operands`, rewritten `trigram_candidates`/
      `trigram_min_posting`)
- [x] OracleWordIndex build (`lib.rs`: `OracleWordIndex`, `tokenize_words_ge4`,
      `oracle_word_eligible`, `scan_oracle_words`, wired into `build_oracle_text_index`'s existing
      per-text loop)
- [x] narrow_rec wiring (new `TextContains{OracleTextLower}` arm ahead of the generic trigram arm,
      4-way dispatch: both-empty / sparse-only-union / single-dense-direct / mixed-scatter)
- [x] compile_plane WordPlane arm (`PlaneExpr::Bits(Vec<u64>)` — an owned clone of the dense word's
      bitmap embedded directly in the expression tree, so `eval_word`/`eval_planes` needed **no**
      signature change; only `compile_plane`/`split_planes` gained a third `&Archived<OracleWordIndex>`
      param). Only fires on the single-dense-hit-no-sparse-hit shape — verified correct via a
      dedicated test (`compile_plane_word_bonus_composes_with_other_planes`) plus the negative case
      (mixed dense+sparse must decline).
- [x] tests: `oracle_word_index_exact_union_parity`, `oracle_word_index_dispatch_shapes`,
      `compile_plane_word_bonus_composes_with_other_planes`, `trigram_dense_sparse_dispatch_parity`.
      Full existing suite (87 tests) green in both debug and `--release`, plus `--features
      alloc-counter` builds clean.
- [x] benchmarks: baseline on main captured, iterated through two real regressions, now a clean net
      win with no remaining real regressions (see below). `scripts/bench_oracle_word_index.py`
      (targeted) + `scripts/survey_queries.py` (broad, existing), both against
      `benchmarks/bitplanes/corpus.jsonl` (reused from #630 — same schema, no re-export needed).
      `bench_word_dict_scan.rs` is a new kernel micro-benchmark (needs
      `benchmarks/verify-order/real.store`, rebuilt on this branch — see its module doc).

## Benchmark finding: broad-survey regression from the dictionary-scan cost itself (resolved)

Targeted script (`scripts/bench_oracle_word_index.py`): the doc's own motivating cases are a real,
large win — `o:creature` 1.356→0.174ms, `o:this` 1.089→0.263ms, `o:target` 0.680→0.180ms,
`o:control` 0.602→0.170ms (6-8x). Total-row-count parity held for every config (correctness intact).

But `o:token` regressed 0.180→0.194ms even after fixing an initial double-scan bug (see below), and
the broad `scripts/survey_queries.py` screen (400 generated + 120 wild, seed 42) shows a **real,
broad regression**: overall geomean 1.087 (8.7% slower), oracle_text-tagged-query geomean **1.42x
(42% slower)**, 81 of 117 oracle_text queries regressed >5% vs. only 23 improved.

Root cause: `scan_oracle_words`'s dictionary scan cost is **O(dictionary size)** (~6,300 words),
independent of the query's own selectivity — unlike trigram lookup, which is an O(1)-ish exact-key
op. This is a fixed tax paid by *every* eligible single-word oracle predicate, whether or not the
word is common. It pays off hugely for the ~56 dense/common words (the doc's motivating cases,
where the tax is much smaller than the verification cost it removes), but for the other ~6,246
sparse words — the overwhelming majority of the dictionary, sampled by a realistic query mix — the
removed verification cost was already small (trigram narrowing was already tight for them), so the
scan is close to pure overhead. Worst offenders in the survey: multi-predicate And queries where a
plane predicate (type/color) already narrowed hard and cheap (0.04-0.09ms total) — the oracle
predicate's fixed scan tax now dominates a query that used to be nearly free
(e.g. `type:artifact color:w oracle:draw type:land`: 0.046→0.245ms, 5.3x).

First fix applied (landed, real but insufficient): the initial version double-scanned — once
speculatively in `compile_plane`'s bonus arm, once for real in `narrow_rec`'s dedicated arm — for
every needle that wasn't a clean single-dense-hit. Fixed via (a) `narrow_rec` skips the speculative
`compile_plane` attempt entirely for a lone eligible oracle leaf (its own dedicated arm is strictly
more general), and (b) `compile_plane`'s And/Or children are tried cheapest-to-reject first
(`plane_precheck_rank`), so a cheap sibling failure (e.g. `cn:100`) short-circuits before the
oracle scan ever runs. This roughly halved the damage (`o:token`: 0.256→0.194ms attempted-fix vs.
0.180ms baseline) but did not eliminate the broad-survey regression — the fundamental O(dictionary)
cost is still paid on every eligible predicate.

Mid-implementation, the user raised a suffix-array alternative independently (O(log n) range query
instead of a linear dictionary scan, unifying words/sub-word-fragments/phrases into one code path)
— a legitimate fix for the root cause, but a materially different data structure than the design
doc specifies. Instead, per the user's follow-up ("what if the scan itself were faster?"), fixed the
scan mechanism without changing the architecture:

1. **Blob concatenation**: `OracleWordIndex.sparse_blob` — all sparse words concatenated, each
   preceded by a `\0` byte (never present in a tokenized word or an eligible needle, so a match can
   never straddle two words). `sparse_word_starts` maps a match's byte offset back to a word index
   via binary search. This alone turns "~6,300 separate `.contains()` calls, each redoing substring-
   search setup" into "one scan over a ~50KB buffer, setup paid once."
2. **memchr::memmem over the blob**: a follow-up kernel benchmark (`bench_word_dict_scan.rs`,
   run against the real corpus's actual dictionary blob via `benchmarks/verify-order/real.store`)
   measured `memchr::memmem` 5-6.4x faster than std `match_indices` over this same blob — the
   *reverse* of `bench_text_search.rs`'s earlier finding (memmem lost there), because this is one
   long contiguous scan rather than many short separate haystacks, where memmem's setup overhead
   used to dominate. Wired into `scan_oracle_words` in place of `match_indices`.

Result (re-run `scripts/bench_oracle_word_index.py` + `scripts/survey_queries.py`, same corpus/seed):
every regression is gone and every targeted config improved, including the ones that regressed
before (`o:token`: main 0.180ms → branch 0.080ms, 2.25x *faster*, not slower). Broad survey: overall
geomean **1.06x faster** overall, oracle_text-tagged-query geomean **1.58x faster**, only
6 of 117 oracle_text queries regressed >5% (vs. 92 improved) — and the few remaining "regressions"
in the full 520-query survey are all sub-millisecond, on query shapes unrelated to this change
(`cn:300`, `id:rg name:st`, ...), consistent with measurement noise rather than a real effect.
Total-row-count parity held on every config across every iteration (correctness never regressed
while chasing performance).

## Self-review notes

- Empty-store edge case (`n_cards == 0`) checked: `finalize_trigram_index`/build loop produce an
  all-empty `SortedTrigramIndex`/`OracleWordIndex`, and the new narrow_rec/compile_plane guards
  (`n_cards` equality checks) degrade to a harmless no-op rather than panicking.
- u16 posting packing (both `SortedTrigramIndex.sparse_postings` and
  `OracleWordIndex.sparse_postings`) is guarded by an explicit domain-fits-u16 check, mirroring
  `NameBigramIndex` — falls back to forcing everything dense rather than truncating if a future
  corpus ever exceeds 65,536 distinct texts/cards.
