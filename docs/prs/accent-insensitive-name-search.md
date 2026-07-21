# Fold Diacritics in Fuzzy Name Search

Fixes [#649](https://github.com/jbylund/sylvan_librarian/issues/649).

## What

`name:eowyn` now matches "Éowyn, Fearless Knight" — fuzzy `name:` search folds diacritics on both
the stored name and the search term, so an unaccented query finds accented cards and vice versa.
Exact-match (`!"..."`, `name=`) and regex (`name:/.../`) are unchanged and stay accent-sensitive.
Both parsers also now accept the accent typed *bare* (unquoted) — `name:Éowyn` — not just quoted.

## Why

Fuzzy name search was already case-insensitive but not diacritic-insensitive. A scan of the real
Scryfall corpus found 14 distinct accented base letters in card names (í ú é ü û ō ö â á ï ñ ó ä
à — acute/grave/circumflex/diaeresis/tilde/macron over a/e/i/o/u/n), so this needed general
diacritic folding, not a one-off `é` special case.

Two independent matching engines read `name:` queries — the Postgres SQL path and the Rust
`card_engine` (primary; SQL is a cost-routed fallback per #712) — and both needed to agree on
what counts as "the same name," or they'd silently diverge on which cards a query returns.

## Changes

### `api/parsing/card_query_nodes.py`

- New `fold_accents(value)`: NFKD-decomposes and drops combining marks (stdlib `unicodedata`, no
  new dependency). This is the single source of truth for diacritic folding — every other change
  below consumes its output rather than re-implementing folding.
- `_handle_text_field_pattern_matching`: for `card_name`, matches against `card_name_folded`
  instead of `card_name` and folds the search term before building the LIKE pattern.
- `_rhs_to_json` (`CardBinaryOperatorNode`): folds the value for `card_name` fuzzy (`:`) queries
  before it crosses the PyO3 JSON boundary into Rust, so Rust needs no folding logic of its own.

### `api/card_processing.py`

`preprocess_card()` computes `card_name_folded = fold_accents(card_name.lower())` once per card at
import time.

### `api/db/2026-07-20-01-accent-folded-name.sql`

New migration: adds the `card_name_folded` column (backfilled for existing rows, `NOT NULL`) and a
GIN trigram index on `lower(card_name_folded)`.

### `card_engine/` (Rust)

- New `card_name_folded: InlineStr<61>` field on `OracleCard`/`CardRow`, read directly from the
  `card_name_folded` pydict key (added to `ENGINE_COLUMNS`) — no Rust-side fold computation.
- `name_trigram`, `name_bigrams`, and the `TextSearchField::NameLower` eval/verification path now
  read `card_name_folded` instead of `card_name_lower`. Confirmed via grep that both indexes and
  this field are used *only* by the fuzzy-match path (`TextField::NameLower`/`TextExact`/
  `ExactName` are separate enums reading the unfolded field), so the swap is contained.
- `ARCHIVE_FORMAT_VERSION` bumped for the struct layout change.

### `api/parsing/hand_parser.py`

`_is_word_start`/`_is_word_cont` replace the ASCII-only `_WORD_START`/`_WORD_CONT` frozenset
membership checks with `c in <frozenset> or c.isalpha()`, so bare (unquoted) words can start with
or contain any Unicode letter, not just ASCII. The ASCII fast path is unchanged.

### `api/parsing/pyparsing_based.py`

The bare-word regexes (`word`, `string_value_word`, `hyphenated_condition`'s value, and the
implicit-AND tokenizer's `string_value_tok`) swap ASCII character classes for `\w` /
`[^\W\d]` (Python's `re` is Unicode-aware by default for `str` patterns; `[^\W\d]` is "a word
character that isn't a digit," i.e. letter-or-underscore) — same widening as the hand parser, kept
in parity.

### Tests

- `card_engine/src/tests.rs`: new `accent_folded_name_search_matches_unaccented_query`; existing
  fixture builders/brute-force checks updated to populate/read `card_name_folded` alongside
  `card_name_lower`.
- `api/parsing/tests/test_fold_accents.py` (new): direct coverage of `fold_accents()` against the
  corpus characters above.
- `api/parsing/tests/test_sql_gen.py`, `test_exact_name_search.py`: SQL fragment assertions for
  both the folded fuzzy path and the unfolded exact path, quoted and bare.
- `api/parsing/tests/implicit_and_cases.py`: bare accented word cases, shared by the hand/pyparsing
  parity test and the implicit-AND preprocessing test.
- `api/tests/test_integration_testcontainers.py`: new
  `test_card_search_by_name_folds_accents` against a real Postgres instance with the real
  migration applied — `name:eowyn` finds "Éowyn, Fearless Knight" and only it; exact match
  distinguishes the two spellings. Existing count/set assertions touched by the new fixture card
  updated.
- `api/tests/fixtures/engine_cards.json`, `api/tests/test_engine_property.py`: backfilled
  `card_name_folded` (existing fixture data predates the new required field).

## Correctness

- Exact-match paths (`ExactNameNode`, `name=`) are untouched — they still compare against the raw
  `card_name`/`card_name_lower`, so typing the accent is still required there. This is intentional:
  the issue only asks for the fuzzy path to fold, and exact match keeping full precision is the
  more conservative default.
- Scoped to `card_name` only. `oracle_text`/`flavor_text`/`card_artist` fuzzy search is not folded —
  they're interned strings in Rust (not inline), so folding them would need their own stored
  columns, more surface than this issue asks for.
- Query-time cost is unchanged: the SQL fragment swaps one indexed lowercase column for another
  (`lower(card_name) LIKE` → `lower(card_name_folded) LIKE`); the Rust field is populated once at
  reload, not computed per query.
- The tokenizer widening is deliberately about lexer *acceptance* (which characters can appear in a
  bare word), independent of `fold_accents()`'s Latin-diacritic *folding* scope — a bare word with,
  say, a Cyrillic or CJK character now lexes fine too; it just won't be accent-folded (out of scope,
  same as oracle/flavor text above).

See [docs/issues/00649-accent-insensitive-name-search.md](../issues/00649-accent-insensitive-name-search.md)
for the full design writeup.
