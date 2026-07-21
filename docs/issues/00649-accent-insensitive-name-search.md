# Accent-Insensitive Fuzzy Name Search

[#649](https://github.com/jbylund/sylvan_librarian/issues/649)

`name:eowyn` didn't match "Éowyn, Fearless Knight". Fuzzy `name:` search was already
case-insensitive (`lower(card_name) LIKE ...`) but not diacritic-insensitive, so any card whose
name carries an accent was only reachable by typing the accent.

A corpus scan (`benchmarks/bitplanes/corpus.jsonl`, real Scryfall card names) turned up 14 distinct
accented base letters — í ú é ü û ō ö â á ï ñ ó ä à — across acute, grave, circumflex, diaeresis,
tilde, and macron marks on a/e/i/o/u/n. General diacritic folding is required; a narrow "handle é"
special case would miss most of them.

## Design

Two independent matching engines exist here — a Postgres SQL path and a Rust `card_engine`
(primary; SQL is a cost-routed fallback per #712) — and both need to agree on what counts as "the
same name" or the two would silently diverge on which cards a query returns.

**Single source of truth in Python:** `fold_accents()` in
[card_query_nodes.py](../../api/parsing/card_query_nodes.py) NFKD-decomposes each character and
drops combining marks (stdlib `unicodedata`, no new dependency). Both engines consume its output
rather than each re-implementing folding:

- **Ingest:** `preprocess_card()` computes `card_name_folded = fold_accents(card_name.lower())`
  once per card and stores it as a real column
  ([migration](../../api/db/2026-07-20-01-accent-folded-name.sql)) with its own trigram GIN index.
  The Rust engine reads the same column at reload time (`ENGINE_COLUMNS`) into a new
  `card_name_folded: InlineStr<61>` field, so it needs zero folding logic of its own — it's plumbing,
  not computation. This is why `ARCHIVE_FORMAT_VERSION` bumped
  ([lib.rs:5210](../../card_engine/src/lib.rs#L5210)).
- **Query side:** the fuzzy-match code path folds the search word the same way before it reaches
  either engine — `_handle_text_field_pattern_matching` for SQL, `_rhs_to_json` for the JSON that
  crosses into Rust's `build_text_filter`. Whether the user types "eowyn" or "Éowyn", both fold to
  the same word by the time either engine sees it.
- **Rust internals:** `name_trigram`, `name_bigrams`, and the `TextSearchField::NameLower`
  eval/verification path ([filter.rs](../../card_engine/src/filter.rs)) were repointed from
  `card_name_lower` to `card_name_folded`. Both indexes exist *only* to serve this fuzzy path
  (confirmed via grep — `TextField::NameLower`/`TextExact`/`ExactName` are separate enums reading the
  unfolded field), so the swap is contained.

**What stays accent-sensitive, deliberately:** exact-match (`!"..."`, `name=`) keeps comparing
against the raw `card_name`/`card_name_lower`. Typing the accent still gets you exact-accent
matching; this needed no code change since those paths never touched the folded column. Regex
search (`name:/.../`) is also untouched — folding would change its character-class semantics.

**Scope:** only `card_name`. `oracle_text`/`flavor_text`/`card_artist` fuzzy search is not folded —
doing so would need their own stored folded columns (they're interned strings in Rust, not
inline), which is more surface than this issue asks for.

**Cost:** query-time neutral. The SQL fragment goes from `lower(card_name) LIKE` to
`lower(card_name_folded) LIKE` — same shape, matching functional index. The Rust field is one more
`InlineStr<61>` per card (~61 bytes × ~31.5k cards ≈ 2MB), populated once at reload, not computed
per query.

## Known limitation (not fixed here)

The hand-rolled parser's tokenizer only accepts ASCII in *unquoted* bare words — `name:éowyn`
(no quotes) fails to lex at all (`Unexpected character 'é'`). Quoting works today:
`name:"éowyn"` parses and folds correctly. This is a pre-existing lexer restriction
(`_WORD_START`/`_WORD_CONT` in `hand_parser.py`) affecting all bare non-ASCII tokens, not specific
to this fix, and out of scope here — the reported issue (typing the *unaccented* form) doesn't
require it, since "Eowyn" is plain ASCII.

## Validation

- `card_engine` unit test `accent_folded_name_search_matches_unaccented_query` (cargo test, debug +
  release).
- `test_fold_accents.py` — direct coverage of the fold function against the corpus characters above.
- `test_sql_gen.py` / `test_exact_name_search.py` — SQL fragment assertions for both the fuzzy and
  exact paths.
- `test_integration_testcontainers.py::test_card_search_by_name_folds_accents` — real Postgres,
  real migration, asserts `name:eowyn` finds "Éowyn, Fearless Knight" and only it, and that exact
  match distinguishes the two spellings.

## Status

**Resolved — [PR #716](https://github.com/jbylund/sylvan_librarian/pull/716).**

See [PR description](../prs/accent-insensitive-name-search.md) for full details of what shipped.
