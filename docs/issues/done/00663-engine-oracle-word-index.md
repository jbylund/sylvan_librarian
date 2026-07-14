# Engine: word-level inverted index for oracle text search

Status: proposed design, not started. Surfaced 2026-07-10 while investigating
`oracle:token`'s cost (see
[00624-engine-bind-memoized-text-predicates.md](00624-engine-bind-memoized-text-predicates.md)).
No GitHub issue filed yet.

## Problem

`oracle:token` costs ~0.2 ms even though its trigram narrowing is a perfect
positive (3,993-of-3,993 texts, zero false positives for this word): the match
phase still runs a real `.contains()` scan over every candidate, because
`memoize_text_predicates` declines — `token`'s minimum trigram posting
(3,955) is nearly as large as the trigram-intersected candidate set (3,993),
so `memoize_pays`'s 2× safety margin blocks it. More fundamentally, trigram
narrowing is *never* exact for needles longer than 3 characters — it can only
bound a superset, because intersecting several trigrams' posting lists proves
those trigrams are all present somewhere in the text, not that they're
contiguous and in order. Verification is load-bearing, not incidental; no
`memoize_pays` tuning removes it.

Common single-word needles are the worst case: `oracle:creature`'s six
constituent trigrams (`cre`/`rea`/`eat`/`atu`/`tur`/`ure`) are each
73–79% dense in the corpus — trigram narrowing barely narrows at all, and
today's only lever (decline, fall back to a full scan) throws away
information that's actually available at build time.

**Ruled out already** (kernel-benchmarked, `bench_text_search.rs` /
`bench_iter_dispatch.rs`, uncommitted on branch `engine-oracle-memmem`):
`memchr::memmem` measures 1.7–2.4× *slower* than `str::contains` for
oracle-text-length haystacks; `Box<dyn Iterator>` dispatch in the match-phase
loop measures ~1.00× vs. a concrete iterator. Neither is the bottleneck.

## Design: word dictionary + inverted index (exact, for needles > 3 chars)

Tokenize every distinct oracle text (`[a-z0-9']+`) into a **dictionary** of
distinct words, and build an **inverted index**: word → sorted posting list
of dense text ids containing that word (the same dense-text-id granularity
the trigram index already uses, so this plugs into the existing
`expand_text_ids`/CSR pipeline unchanged).

At query time, for a needle Q longer than 3 characters:

1. Scan the dictionary (a few thousand short strings) for words that contain
   Q as a substring — e.g. `"token"` → `{token, tokens, token's, nontoken}`.
2. Union those words' posting lists.

This is **exact**, not a superset — no verification pass, ever. Correctness
argument: tokenization boundaries are whitespace/punctuation, which Q itself
doesn't contain (it's a single fragment), so any occurrence of Q as a literal
substring lies entirely inside exactly one tokenized word. Every word
containing Q is found in step 1, so their union is precisely the set of texts
where Q occurs. Verified directly against the real corpus for `"token"`: the
word-union count (3,953 texts) matches the exhaustive substring-scan count
exactly.

**Scope limits** (both already true of the existing engine, unaffected):
- Multi-word phrases (`"sacrifice a creature"`) span tokenization boundaries
  and aren't attempted here — fall through to the existing trigram path,
  already adequately selective for phrases.
- Needles under 4 characters don't fit the "one word, no boundary" argument
  as cleanly, and tend to be hopelessly unselective anyway (`"a"` matches
  99.9% of the corpus) — unchanged, still declines toward a scan.

### The 3-character case is already solved, for free

A trigram lookup for a needle *exactly* 3 characters long requires no
intersection — there's only one trigram, so there's no adjacency ambiguity to
false-positive on. Reading the existing trigram index's posting list directly
**is already exact** for these needles. So the word dictionary only needs
entries for words longer than 3 characters: a query needle can only match a
dictionary word at least as long as itself, so trigram (len ≤ 3) and the word
index (len > 3) partition the needle-length space cleanly — no overlapping
keys, no double-stored postings, no structures to merge.

### Feeds directly into #634's exactness promotion

Because this result is exact *at narrow time* (no bind-time verification
step, unlike today's `OracleMatch` memoization), `narrow_candidates_exact`
can return `(Some(candidates), true)` for it directly — the same "tight,
card-space" signal that only bitplane predicates get today. That's the input
[00634-engine-permuted-bitmap-order-phase.md](00634-engine-permuted-bitmap-order-phase.md)'s
all-match/popcount-skip promotion gates on, and today `oracle:` predicates can
never reach it (memoization runs *after* that decision is locked in).

Concretely: resolve this **directly in `narrow_candidates_exact` (or
whatever `narrow_rec` arm feeds it), bypassing `memoize_text_predicates`
entirely** for any needle that qualifies (len > 3, no token-boundary
characters — i.e. not a phrase). This isn't a new broadness heuristic to
maintain alongside the existing ones — the bitmap crossover already means
every qualifying needle resolves exactly and cheaply, common or not, so the
routing decision is purely a shape check on the needle (length + "is this a
single fragment"), not a cost/selectivity judgment call. `memoize_text_predicates`
still exists for what it was built for (verifying-then-caching a *loose*
trigram result when narrowing alone doesn't get there); this path just never
enters it, because it never has an unverified result to begin with.

## Posting/bitmap crossover: reusing #639's rule, one level down

This is not a new threshold — it's PR #639's bigram-index
crossover rule (cited again in
[local-engine-legality-bitplanes.md](local-engine-legality-bitplanes.md): "plane costs
`words_per_plane(n)*8` bytes flat; a u16 posting costs 2 bytes/entry;
crossover at ~6.26%"), applied at word and trigram granularity instead of
format/bigram granularity. A sorted posting list of dense text ids costs
`2 bytes × match_count` (u16 ids fit comfortably at this corpus size). A
bitmap costs a flat `n_texts / 8` bytes regardless of match count. Past the
crossover a bitmap is both smaller *and* faster to probe (O(1) bit test vs. a
merge/binary-search step against a large sorted list), so build-time
selection between the two isn't a size/speed tradeoff, it's a pure win in
both dimensions.

Measured against the actual dense-text-id corpus (distinct oracle text by
content, matching `oracle.gids.len()`: n=29,088; crossover at 1,818 texts,
6.25%):

**Word postings** — 56 of 6,302 words longer than 3 characters exceed the
crossover: `creature` 69.4%, `this` 64.5%, `target` 40.0%, `card` 32.8%,
`your` 32.4%, `control` 31.5%, `turn` 30.0%, `whenever` 23.6%, `enters` 22.8%,
`that` 22.0%, `with` 21.5%, `when` 21.2%, ... Total word-index memory:
865.4 KiB as postings-only → 539.6 KiB crossover-optimal (**37.6% smaller**).
The real payoff isn't the memory — it's that these 56 words, which would
otherwise decline to a full scan for being too common to narrow, instead get
an **exact, cheap plane answer**.

**Trigram postings** — 536 of 9,030 distinct trigrams exceed the crossover:
total trigram-index memory 7,230.0 KiB → 3,670.4 KiB (**49.2% smaller**).
`oracle:creature`'s own six trigrams illustrate why this also helps latency,
not just memory: all six (`cre` 76.2%, `rea` 76.8%, `eat` 76.6%, `atu` 75.1%,
`tur` 81.2%, `ure` 75.5%) are above the crossover — as is ` th`/` cr` (leading
space + first two letters), at 82.3%/74.9%. Today's `intersect_sorted`
picks the shortest posting list as a seed and merges progressively against
the rest; when every constituent list is ~75% dense there's no cheap seed to
exploit — it's a genuine multi-way merge over six huge sorted lists. Bitmaps
turn each subsequent membership probe into an O(1) bit test (the general
query-time dispatch rule for this is below, alongside the benchmark that
grounds it).

### Decision: keep #639's threshold as-is for trigrams too, don't lower it preemptively

A kernel benchmark (`bench_posting_intersect.rs`, synthetic sorted id lists
over n=29,088, sizes/ratios bracketing the 1,818-id/6.25% crossover) shows
bitmap-AND beating merge intersection by 10-400x at every size tested,
including right at the crossover point (`1818×1818`: 2,125 ns merge vs. 41 ns
bitmap-AND — the 41 ns may partly be timer-floor noise, doesn't change the
conclusion), and shows galloping (binary-search probe) essentially never
winning over a plain merge in this range — the naive O(a·log b) argument
doesn't survive real cache behavior at these sizes. **Conclusion: don't
implement galloping**, merge is the right fallback whenever a bitmap isn't
available.

```
     a      b       merge     gallop bitmap_and  probe->bm    winner
   200    200         166        500         41         41   bitmap_and
   200   1000         541        792         41         41   bitmap_and
   200   5000        2916       1875         41         41   bitmap_and
   200  15000        8833       3125         41         41   bitmap_and
  1000   1000        1166       4166         41        292   bitmap_and
  1000   2000        1458       5125         41        333   bitmap_and
  1000  10000        5541      12541         41        333   bitmap_and
  1818   1818        2125       9125         41        583   bitmap_and
  1818   3600        2833      11916         41        625   bitmap_and
  1818  10000        5208      22750         41        583   bitmap_and
  1818  22000       16166      20125         41        583   bitmap_and
```

The natural next thought — push the trigram threshold below #639's 6.25% to
capture more of that 10-400x speedup — turns out **not** to hold up once you
trace what still uses trigram intersection after the word index (above)
ships:

- Every needle longer than 3 characters, word or arbitrary fragment, resolves
  through the word/dictionary index instead of trigram — not just literal
  dictionary words. `oracle:creature`, the motivating case for "trigram
  intersection is pathological when every constituent trigram is ~75%+
  dense," is a single-word query — it never reaches trigram intersection at
  all once the core design ships.
- What's left for trigram is exactly-3-char needles (a single posting/bitmap
  read, no intersection to speed up) and multi-word phrases — and phrases are
  already established (earlier in this design discussion) as adequately
  selective via trigram as-is, precisely because they don't hit the
  single-common-word pathology.

So the workload that motivated a lower trigram threshold is the same workload
the word index removes from trigram's plate. Lowering the threshold now would
be optimizing for a problem the other half of this design already solves a
different way — **and it's exactly PR #662's shape**: that PR measured an
equally real, equally isolated-kernel-confirmed primitive-level win
(`eval_plane_bit` vs. `eval_planes`, crossover at 650-770 candidates) and got
closed without merging anyway, because the effect (hundreds of ns) turned out
to be noise against tens-of-µs queries once measured end-to-end (+0.84% mean
delta). The absolute deltas in the table above (tens of ns to ~16µs per
pairwise step) are the same order of magnitude — paying for a second
calibrated threshold and dispatch complexity ahead of evidence it's needed
would repeat that mistake.

**Decision: ship the core word index with #639's threshold unchanged for
trigrams, re-run the broad survey, and only revisit a trigram-specific
threshold if real (not synthetic) phrase-driven trigram queries still show up
slow in that post-core survey.** The per-step merge/probe/AND dispatch table
below is still worth implementing regardless (it's free — a shape check on
already-existing representations, not a new threshold), but *choosing* to
convert more trigrams into bitmaps ahead of that evidence is not.

**This is a second, separate crossover from the storage one above** — not
"which representation to persist for this word/trigram" but "given two
operands about to be intersected, each already either a posting list or a
plane, which combination strategy to run." The same table answers it, as a
plain dispatch on the operand shapes (this subsumes and generalizes the
"all-dense" branch mentioned earlier for `intersect_sorted`):

| operand A | operand B | strategy |
|---|---|---|
| posting | posting | merge (never gallop) |
| posting | plane | probe A's ids into B directly (never merge/gallop against a plane) |
| plane | plane | bitmap AND |

`intersect_sorted`'s shortest-first walk needs this dispatch at *every* step,
not just the fully-dense case: whatever the current (shrinking) candidate
set is, check whether the next list to filter against is a posting or a
plane, and probe vs. merge accordingly. Given a plane never loses to probing
or merging a posting against it in this data, a plane operand should always
be preferred as the filter target over a same-or-larger posting when both
are available — i.e., among a needle's remaining trigrams, filter against
any plane-tier ones before any posting-tier ones, not strictly shortest-first
by raw count.

Both crossovers are the same underlying primitive — *store sparse as a
posting list, dense as a bitmap* — and, per the decision above, both use
#639's rule unchanged. Words and trigrams differ in access pattern
(membership-only vs. multi-value intersection), which is why a lower
trigram-specific threshold looked appealing, but not in which threshold
value to use — the case that would have justified a lower one is exactly
the case the word index removes from trigram's workload.

### Two dictionaries, not one tagged structure

Rather than a single word dictionary with a per-entry sparse/dense tag, split
it into two at build time: a tiny **dense dictionary** (the ~56 words already
identified as bitmaps) and the **sparse dictionary** (the remaining ~6,246,
posting lists). A query needle scans both. Total comparisons are identical
to scanning one combined dictionary — the split doesn't change how many
words get compared against Q, just which array each one lives in.

This is a strict improvement over a tagged sum type:

- No discriminant anywhere — the dense dictionary is `(word, bitmap_ref)`
  pairs, the sparse one is `(word, Vec<u32>)` pairs; each loop body is
  homogeneous, no per-item branch on representation.
- The dense dictionary is tiny regardless of corpus size, so scanning it as
  a separate pass costs nothing extra.
- **The combination strategy falls out of which loop(s) produced hits**,
  with no extra classification pass needed afterward:
  - both empty → no match.
  - dense hits empty → sorted-merge union over the sparse hits only
    (`Candidates::Cards`), the bitmap machinery is never touched.
  - exactly one dense hit, sparse hits empty → return that bitmap directly,
    no allocation, no copy (the common shape for single high-frequency-word
    queries like `o:creature`).
  - otherwise → materialize a scratch bitmap, OR in the dense hits, scatter
    the sparse hits on top (`Candidates::CardBits`).

Build-time cost is unchanged — bucketing a word by the `count > n/16`
crossover check already exists; this just changes which of two arrays it's
appended to instead of which enum variant wraps it.

**The same split applies to trigrams, with one added simplification they get
for free that words don't.** The word dictionary needs a linear
substring-containment scan (a word containing Q can land anywhere in
lexicographic order, so it isn't binary-searchable). Trigram lookup is exact-
key — a needle's constituent 3-byte windows are looked up directly, no
containment scan — so the trigram side of this split can be two *sorted*
arrays (`[u8; 3]` key, dense/sparse per the crossover) with binary search,
replacing today's `HashMap<[u8; 3], Vec<u32>>` (`TrigramIndex`, `lib.rs:772`)
entirely. This isn't primarily a speed claim (not measured against the
current HashMap) — it's that a plain sorted `Vec` is trivially zero-copy
archivable with rkyv, where a `HashMap` needs rkyv's heavier hash-table
support for the same data, and ~9,030 small fixed-width entries binary-
searched is at least competitive with a hash lookup regardless.

### Bonus: common word-bitmaps are plane-eligible for free

Once the ~56 over-threshold words are stored as fixed-size card bitmaps,
they compose naturally with `compile_plane`'s existing AND machinery — add a
`TextContains` arm that returns the precomputed bitmap when `word` is in the
dense dictionary, `None` otherwise (falls through to the sparse word index /
trigram / scan, same as today). `oracle:creature type:artifact` then ANDs
two independently precomputed planes with zero scanning on either side, not
just a fast answer for the oracle predicate alone. Not required for the base
design to pay off; worth doing once the bitmaps exist regardless.

## Sizing

Both structures are sub-4-MiB additions to a 76 MB archive (539.6 KiB word
postings/planes + 3,670.4 KiB trigram postings/planes, crossover-optimal) —
smaller in total than today's naive all-postings trigram index alone
(7,230.0 KiB) would be. This is a case where the crossover is a strict win,
not a size/speed knob to tune. (Measured 2026-07-10 against
`benchmarks/bitplanes/corpus.jsonl`, deduplicated by lowercased oracle-text
content — 29,088 distinct texts, matching the real `oracle.gids.len()`
granularity. Should still be re-checked against `real.store`'s actual
`oracle.gids`/`trigrams` once this is implemented, as a build-time sanity
check, but the corpus dedup here already uses the production dedup key, so no
further reconciliation is expected.)

## Tasks

Ordered so the large, already-de-risked win ships (and gets end-to-end
validated) independently of the smaller, #662-shaped piece:

**Core (word dictionary + inverted index — eliminates verification, large
absolute win on an already-surfaced slow query):**
- [ ] Word dictionary + inverted index (dense text id postings), built in the
      same pass as `build_oracle_text_index`
- [ ] Query-time dictionary substring scan + posting union, wired into
      `narrow_rec`'s `TextContains` arm for needles > 3 chars
- [ ] Split dense/sparse word dictionaries using #639's crossover rule as-is
      (no re-derivation needed — same access shape as bigrams)
- [ ] Query-time dispatch on which loop(s) produced hits (empty/sparse-only/
      single-dense/mixed) per the four cases above
- [ ] Wire the exact result into `narrow_candidates_exact`'s tight/card-space
      signal (#634 promotion path)
- [ ] `compile_plane` `TextContains` arm for the over-threshold word bitmaps
- [ ] `intersect_sorted`: per-step posting-vs-plane dispatch (merge/probe/AND
      per the table above) for the trigram cases that remain (exact-3-char,
      phrases) — free, no new threshold, just using representations that
      already exist
- [ ] Replace `TrigramIndex` (`HashMap<[u8; 3], Vec<u32>>`, `lib.rs:772`) with
      sorted dense/sparse arrays, binary-searched by the 3-byte key — same
      split as the word dictionaries, simpler to archive with rkyv
- [ ] Re-run the broad survey; acceptance: `oracle:token`, `oracle:creature`,
      and Or-combos involving either drop out of the slow tail

**Explicitly not doing yet, per the #662 lesson:** lowering the trigram
storage threshold below #639's. Only reconsider if the post-core survey above
still shows real (phrase-driven) trigram-intersection queries in the slow
tail — the motivating case (`oracle:creature`) is resolved by the word index
itself and won't be evidence for this anymore.

## Related

- [00624-engine-bind-memoized-text-predicates.md](00624-engine-bind-memoized-text-predicates.md) —
  the existing memoize/verify mechanism this supersedes for single-word
  needles; `memoize_pays`'s decline on `token` is what surfaced this
- [00620-engine-flavor-text-narrowing.md](00620-engine-flavor-text-narrowing.md) — the
  distinct-text CSR idiom this design reuses at word/trigram granularity
- [00634-engine-permuted-bitmap-order-phase.md](00634-engine-permuted-bitmap-order-phase.md) —
  #634; the all-match/popcount-skip promotion this design can newly reach
- [00655-engine-numeric-range-planes.md](00655-engine-numeric-range-planes.md),
  [local-engine-legality-postings.md](local-engine-legality-postings.md) — prior
  applications of "threshold and drop to a scan"; this design's contribution
  is replacing "drop" with "represent as a bitmap instead"
- [local-engine-legality-bitplanes.md](local-engine-legality-bitplanes.md) — cites PR
  #639's crossover rule directly (`words_per_plane(n)*8` vs. `2 bytes/entry`,
  ~6.26%); this doc reuses the identical formula unchanged for both words and
  trigrams (see the decision above for why trigrams don't get a separate one)
- [local-engine-union-summary-planes.md](local-engine-union-summary-planes.md) — the
  "membership-only vs. ops-read-many-values" distinction that explains *why*
  trigram intersection is expensive for common words in the first place (even
  though the fix here is routing those words away from trigram, not
  retiering trigram itself)
- PR #662 (closed without merging, 2026-07-10) — `eval_plane_bit`'s
  isolated-kernel win (crossover at 650-770 candidates) didn't survive
  end-to-end measurement (+0.84% mean delta, noise); the direct precedent for
  this doc's decision not to lower the trigram threshold ahead of evidence
