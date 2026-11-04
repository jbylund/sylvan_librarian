# Regex: close the engine's decline set so the SQL fallback stops being load-bearing

## Status: proposed

## Problem

`_search` sends a query to PostgreSQL whenever the engine raises
(`api/api_resource.py`, the blanket `except Exception` around `_search_engine`).
That handler is meant as a crash net, but three regex cases reach it on
*ordinary, documented queries*, which makes the SQL path load-bearing for a
feature rather than a backstop:

1. **Lookaround.** `card_engine` compiles query regexes with the `regex` crate,
   which has no lookaround or backreferences by design. `o:/draw (?!two)/`
   fails `Regex::new`, which `build_filter` turns into a `QueryError`.
   Lookahead is on the documented feature list
   (`docs/changelog/2025-02-02-regex-search.md`) and Scryfall answers it: 435
   cards for `o:/draw (?!two)/ t:instant`.
2. **Attributes with no `TextField`.** Regex compiles only onto
   name/oracle/flavor/artist, so `s:/^m1/`, `layout:/^trans/`, `wm:/^guild/`
   and `cn:/^1/` decline — even though the parser emits a `RegexValueNode` for
   all of them and the SQL path answers them with `~*`.
3. **PostgreSQL-only escapes.** ARE spells word boundaries `\y`/`\Y`/`\m`/`\M`
   and end-of-string `\Z`. Scryfall accepts `name:/\yizzet\y/` (20 cards); the
   `regex` crate rejects every one of them.

A fourth case is worse than a decline, because it never raises at all.
`t:/…/` is resolved in Python before the engine sees it: `kwargs()` runs
`self.rhs.value.strip().title()` and emits the result as a **literal subtype**.
So `t:/^drag/` becomes the subtype `"^Drag"` and `t:/goblin|elf/` becomes
`"Goblin|Elf"` — types no card has. The query returns nothing, silently, on both
the engine and SQL paths. Scryfall returns 1,269 cards for `t:/goblin|elf/`.

This one is invisible to the parity suite in
`api/parsing/tests/test_parser_parity.py`: that compares the two *parsers*
against each other, and both mangle it identically.

## Approach

**Two-tier regex compilation** (`card_engine/src/regex_compat.rs`). A new
`CompiledRegex` tries the `regex` crate first and falls back to `fancy_regex`
only for patterns it rejects. Everything that compiles today still compiles on
the linear engine, so the #734 trigram narrowing — which reads the pattern with
`regex_syntax::parse` — is untouched for every pattern that had it. A
backtracking pattern yields no literal factors and scans, which is correct and
already the behavior for any regex without a usable factor.

`fancy_regex` runs under the execution budget the security work already
calibrated (`REGEX_BACKTRACK_LIMIT`, `docs/issues/security-regex-execution-budget.md`),
and exhausting it surfaces as `UnsupportedRegexError` through that work's
thread-local failure latch rather than as a silent non-match. Only the
backtracking arm can reach it: the linear engine has no step budget to exhaust,
which is most of why the two-tier split is worth having — the patterns that were
already cheap never come near the ceiling, and the ones that can are the ones the
#734 trigram narrow cannot narrow.

**ARE escape translation** rewrites `\y`→`\b`, `\Y`→`\B`, `\Z`→`\z` (exact
equivalents, so those stay linear) and `\m`/`\M` to lookaround (no equivalent,
so those go backtracking). Bracket expressions are copied through untouched.

The parse-time budget (`api/parsing/regex_budget.py`) measures patterns with
Python's `re._parser`, which rejects all four word-boundary escapes outright, so
it applies the same rewrite before measuring — otherwise a documented operator
would be turned into a user-visible error by its own security check. It measures
a translated copy and leaves the stored pattern alone, because the SQL path hands
that pattern to PostgreSQL, which speaks ARE natively. `\m`/`\M` therefore spend
lookaround budget, which is the honest accounting: they are exactly what puts a
pattern on the backtracking engine that the cap exists to bound.

**`TextField::TypeLine`** reads the interned `type_line_id` already on
`AOracleCard`, card-level like `Layout` — Scryfall's type line is oracle data.
Regex now compiles onto every string field the store holds.

**Parser: pass type regexes through.** `kwargs()` and `_handle_jsonb_array`
route a `RegexValueNode` on a type attribute to the type line instead of the
containment tests — `type_line ~* …` on the SQL path, `TextField::TypeLine` in
the engine. Bare-literal regexes are unaffected: `lower_literal_regexes`
already rewrites `t:/dragon/` to `t:dragon`, and that is measured identical to
Scryfall (445 = 445; see below).

## Acceptance

Correctness work, so per `docs/workflows/performance-pr-workflow.md` the
acceptance is differential, not a benchmark story.

**Differential, on a real corpus.** 30,658 oracle cards, built from Scryfall's
`oracle_cards` bulk data through this repo's own `preprocess_card`, one row per
oracle id so card-level fields are unambiguous. The reference applies Python's
`re` to the same field the query names and counts distinct oracle ids; it
shares no code with the engine. Where a pattern uses a PostgreSQL-only escape
the reference uses the independent equivalent spelling (`\y` → `\b`), so the
translation is checked against something other than itself.

| query | before | after | reference |
|---|---|---|---|
| `o:/draw (?!two)/` | decline → SQL | 3,630 | 3,630 |
| `o:/(?=.*sacrifice)draw/` | decline → SQL | 247 | 247 |
| `o:/(?<=draw )a card/` | decline → SQL | 2,900 | 2,900 |
| `o:/^\{T\}:/` | decline → SQL | 1,035 | 1,035 |
| `name:/\yizzet\y/` | decline → SQL | 14 | 14 |
| `name:/\mizzet/` | decline → SQL | 14 | 14 |
| `name:/\bizzet\b/` | 14 | 14 | 14 |
| `t:/goblin\|elf/` | **0, silently** | 1,157 | 1,157 |
| `t:/^legendary/` | **0, silently** | 3,926 | 3,926 |
| `t:/dragon.*spirit/` | **0, silently** | 16 | 16 |
| `t:/elf (warrior\|shaman)/` | **0, silently** | 171 | 171 |
| `t:/^drag/` | 0 | 0 | 0 |
| `t:/dragon/` | 402 | 402 | (lowered to `t:dragon`) |

13 of 13 match. The `before` column is measured the same way, with this
branch's engine and the pre-change parser.

**Semantics against Scryfall.** Absolute totals are not comparable — this
repo's import drops digital-only and funny-set cards (measured: 18,889 of
116,694 rows in `default_cards`), so its corpus is a deliberate subset. What
Scryfall settles is what each query *means*, and it confirms every case above
is a real query with real answers: 1,269 for `t:/goblin|elf/`, 4,316 for
`t:/^legendary/`, 435 for `o:/draw (?!two)/ t:instant`, 20 for
`name:/\yizzet\y/`.

It also settles that the bare-literal lowering is safe: `t:/dragon/` and
`t:dragon` return identical totals on Scryfall (445 = 445, likewise
creature/elf/aura/legendary/equipment). And `t:/^legendary/` at 4,316 against
`t:legendary` at 4,348 is what shows the two are not the same predicate — the
anchored regex matches type lines *starting* with the supertype, the lookup
matches it anywhere.

One cost constant is added. `REGEX_BACKTRACK_NS100 = 380_000`, from
`bench_backtrack_engine` (self-contained; no `real.store` needed): lookaround
measures **77x** the linear engine per candidate (6,535 vs 85 ns/card, mean over
negative lookahead / positive lookahead / lookbehind). The engines themselves
are the same speed on patterns both accept — 1.00x, because `fancy_regex`
delegates to `regex` when a pattern needs nothing more — so the tier prices
lookaround, not dispatch. It wants re-fitting against the real corpus with the
calibration bench.

## Not in scope

Closing these declines does not remove the fallback. Three triggers remain, and
none is a capability gap: `ENABLE_ENGINE=false`, an empty store during
hydration, and genuine engine failure. What changes is that the fallback stops
answering queries the engine was expected to answer.

Regex on set code / layout / watermark / artist / collector number is a
**superset of Scryfall**, which rejects all five (`400 All of your terms were
ignored`). This PR keeps them, because the parser already accepts the syntax
and the SQL path already answers it — declining in the engine only re-creates
the fallback. Whether the parser should reject them instead is a separate
question, flagged for the maintainer.
