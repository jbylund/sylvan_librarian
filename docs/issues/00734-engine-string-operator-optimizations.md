# Engine: String Operator Optimizations (regex→contains, memmem)

Status: todo, filed as [#734](https://github.com/jbylund/sylvan_librarian/issues/734). Two independent,
behavior-preserving speedups for substring search over text fields (`oracle`/`name`/`flavor`/`artist`).

## Motivation

The dominant cost of an unanchored regex isn't its per-row constant — it's that **a regex has no
`narrow_rec` arm** (nor a `compile_plane` path), so the plan scans *every* card. The equivalent
`TextContains` narrows through the trigram/oracle-word index to a candidate set first. Lowering the
regex is therefore mostly an **access-path change** (rows visited), with a cheaper per-row scan as a
secondary multiplier.

## Evidence

`bench_substring_finders` (`card_engine/src/bench_verify_cost.rs`), 31,508 cards, needle
`"sacrifice a"` (1,391 actual matches).

Per-row scan cost, all three over the same `oracle_text_lower` strings:

| approach | ns/card | vs regex |
|---|---:|---:|
| `regex (?i) is_match` — today's `o:/sacrifice a/` | 35 | 1.00× |
| `str::contains` | 13 | 2.7× |
| `memmem::Finder` (built once) | 10.5 | 3.4× |

Rows visited, and the two effects combined (match phase):

| path | rows × ns/row | total | speedup |
|---|---|---:|---:|
| regex (full scan) | 31,508 × 35 | ~1,108 µs | 1.0× |
| → `TextContains` (trigram-narrowed) | 2,425 × 14 | ~35 µs | **31.7×** |
| → memmem | 2,425 × 10 | ~24 µs | **45.8×** |

Trigram narrowing cuts `"sacrifice a"` to 2,425 candidates (7.7% of the corpus) — a *loose* superset
the walk then verifies. Both multiples are query-dependent: the row reduction tracks the rarest
trigram's selectivity, and the per-row multiple varies by needle (35 ns for `"sacrifice a"`, 48 ns
for `"flying"`). The ordering is robust; the absolute multiples are not. `o:/sacrifice a/` / card /
`usd`-order is ~2.1 ms end-to-end — this collapses the ~1.1 ms match phase; the `usd` gather+sort
(no precomputed permutation) is a separate cost, out of scope here.

## Optimization 1 — lower metacharacter-free regex to `TextContains`

A slashed regex with no anchors and no metacharacters is a substring search: `o:/sacrifice a/` ≡
`o:"sacrifice a"`. A parser/normalization rewrite (`TextRegex → TextContains`), no engine change.
The win is primarily the access path — the rewritten leaf now narrows through the trigram/oracle-word
index instead of forcing a full scan (~**32×** end-to-end above, of which ~2.7× is the per-row scan).

Caveats:

- **Case folding.** Query regexes carry `(?i)`; `TextContains` runs against the pre-lowercased
  `*_lower` column with a lowercased needle — exactly equivalent for ASCII. Guard/note the Unicode
  fold edge (ß, Turkish İ) where `(?i)` and ASCII-lowercase diverge.
- **Escaped punctuation.** `\.` `\$` are literal characters — unescape into the needle rather than
  bailing to machinery. `regex_tier` already distinguishes these (`\d`/`\w` are classes → machinery).
- **Anchored variants.** `^foo$` → `TextExact`, `^foo` → prefix match: a separate, lower-value
  mapping. The unanchored bare-literal → `TextContains` case is where the win is; do it first.

## Optimization 2 — `memmem::Finder` for the `TextContains` scan

Build a `memchr::memmem::Finder` once, reuse per card — amortizes the Two-Way/prefilter setup across
the corpus, the same trick [`sparse_blob`](../../card_engine/src/lib.rs) uses for the oracle-word
index. **1.26×** over `str::contains`, **3.36×** over regex stacked with (1).

## Sequencing

1. Regex→contains lowering (parser) — the bigger, structurally-clean win; unlocks the memoized
   `OracleMatch`/`NameMatch` set paths for what were regexes.
2. memmem finder for the residual `TextContains` scan.

## Cost model

`regex_tier` (REGEX_MACHINERY 5000 ns×100) and the `TextContains` scan tier (`TEXT_SCAN_NS100` 2300)
already price these apart; a rewrite that changes a leaf's kind gets the cheaper tier automatically.
Re-fit `TEXT_SCAN_NS100` if memmem lands (it drops ~1.3 ns/card, ~200 of the 2300).

## Related

- Seed bench + branch `engine-regex-to-contains` (commit b46fdcb).
- Verify-cost tiers: `card_engine/src/filter.rs` (`verify_cost_tier`, `regex_tier`).
- [#694/#731](00731-engine-compose-universal-evaluator.md) — the range-compose PR that surfaced these.
