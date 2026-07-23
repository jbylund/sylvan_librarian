# Engine: String Operator Optimizations (regex→contains, memmem)

Status: **done** — both steps shipped (`ec7d26b`, `84a9ca9`), filed as
[#734](https://github.com/jbylund/sylvan_librarian/issues/734). Two independent, behavior-preserving
speedups for substring search over text fields (`oracle`/`name`/`flavor`/`artist`).

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
`o:"sacrifice a"`. The win is primarily the access path — the rewritten leaf now narrows through the
trigram/oracle-word index instead of forcing a full scan (~**32×** end-to-end above, of which ~2.7× is
the per-row scan). Measured end-to-end, `o:/sacrifice a/` / card dropped to the substring form's
**117 µs** (1,391 results, byte-identical) from a full regex scan.

**Shipped** as a **Python post-parse AST pass** (`lower_literal_regexes` in
[`api/parsing/rewrite.py`](../../../api/parsing/rewrite.py)), *not* an engine change — the equivalence is a
property of the regex, so doing it once at the shared parse seam serves **both** consumers of the AST:
the SQL path (postgres `gin_trgm_ops`) and the Rust engine (trigram narrow). It rewrites a plain-literal
`RegexValueNode` → `StringValueNode`, making the AST identical to the substring query. `regex_plain_literal`
mirrors the engine's `regex_tier` classification so the two never disagree. All post-parse rewrites now
compose through one `rewrite_query` pipeline (`_REWRITE_PASSES`) that both parsers call.

Caveats (handled):

- **Case folding.** Query regexes carry `(?i)`; the substring `:` path lowercases its needle against the
  pre-lowercased `*_lower` column — exactly equivalent for ASCII (Unicode fold edge ß/İ noted, negligible
  for oracle text).
- **Escaped punctuation.** `\.` `\$` unescape into the literal needle; an alphanumeric escape
  (`\d`/`\w`/`\b`) is a class → keep the regex.
- **Anchored variants.** `^foo$` → `TextExact`, `^foo` → prefix match: a separate, lower-value mapping,
  deferred. Anchored patterns stay a real regex today.

## Optimization 2 — `memmem::Finder` for the `TextContains` scan

Build a `memchr::memmem::Finder` once, reuse per candidate — amortizes the Two-Way/prefilter setup,
the same trick [`sparse_blob`](../../../card_engine/src/lib.rs) uses for the oracle-word index. **1.26×**
over `str::contains` (bench_substring_finders).

**Shipped** (`84a9ca9`) on the four once-per-query **verify/bind scans** in
[`filter.rs`](../../../card_engine/src/filter.rs) that build `OracleMatch`/`NameMatch`/`ArtistMatch`/
`FlavorMatch` from a substring needle — the `contains` path a memoized substring (or lowered regex)
query actually hits. A small incremental on top of step 1's access-path win.

Deliberately **not** applied to the per-row `matches()` scan (the path taken only when memoize
*declines* — broad/short needles): `str::contains` is already Two-Way there, the path is uncommon, and
a `Finder`-once would need a field on the `TextContains` node (~20 construction sites) out of scale
with a 1.26× on a rare case. Left as a noted follow-up if a decline-heavy workload ever justifies it.

## Sequencing

1. ✅ Regex→contains lowering (Python AST pass, `ec7d26b`) — the bigger, structurally-clean win;
   unlocks the memoized `OracleMatch`/`NameMatch` set paths and trigram narrowing for what were regexes.
2. ✅ memmem finder for the residual `TextContains` verify/bind scans (`84a9ca9`).

## Cost model

Because the lowering happens in Python before serialization, the engine now receives a `TextContains`
(and prices it at the `TEXT_SCAN_NS100` 2300 scan tier / narrows it), never the `REGEX_MACHINERY`
5000-tier regex — so no engine cost-model change was needed for step 1. Re-fit `TEXT_SCAN_NS100` if
memmem (step 2) lands (it drops ~1.3 ns/card, ~200 of the 2300).

## Related

- Seed bench + branch `engine-regex-to-contains` (commit b46fdcb).
- Verify-cost tiers: `card_engine/src/filter.rs` (`verify_cost_tier`, `regex_tier`).
- [#694/#731](../00731-engine-compose-universal-evaluator.md) — the range-compose PR that surfaced these.
