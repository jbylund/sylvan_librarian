# Engine: String Operator Optimizations (regex→contains, memmem)

Status: todo, filed as [#734](https://github.com/jbylund/sylvan_librarian/issues/734). Two independent,
behavior-preserving speedups for substring search over text fields (`oracle`/`name`/`flavor`/`artist`).

## Motivation

Unanchored text queries land in the slowest verify bucket. `o:/sacrifice a/` / card / `usd`-order is
~2.1 ms; its match phase alone is ~35 ns/card × 31.5k ≈ 1.1 ms (the rest is the `usd` gather+sort,
which has no precomputed permutation — out of scope here).

## Evidence

`bench_substring_finders` (`card_engine/src/bench_verify_cost.rs`), 31,508 cards, needle
`"sacrifice a"`, all three scanning the same `oracle_text_lower` strings:

| approach | ns/card | vs regex |
|---|---:|---:|
| `regex (?i) is_match` — today's `o:/sacrifice a/` | 35 | 1.00× |
| `str::contains` | 13 | 2.67× |
| `memmem::Finder` (built once) | 10.5 | 3.36× |

All three return 1,391 matches — the substitutions are exact for the metacharacter-free case. The
regex ns/card varies with the needle (35 for `"sacrifice a"`, 48 for `"flying"` in the cluster bench);
the ordering is robust, the absolute multiple is not.

## Optimization 1 — lower metacharacter-free regex to `TextContains`

A slashed regex with no anchors and no metacharacters is a substring search: `o:/sacrifice a/` ≡
`o:"sacrifice a"`. A parser/normalization rewrite (`TextRegex → TextContains`), no engine change.
**2.67×** (35→13 ns/card).

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
