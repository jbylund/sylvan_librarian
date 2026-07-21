# Accent-insensitive fuzzy name search

## Problem

`name:eowyn` didn't match "Éowyn, Fearless Knight". Fuzzy `name:` search was already
case-insensitive but not diacritic-insensitive: any card whose name carries an accent (é, ñ, ö,
and 11 other accented base letters found across the real Scryfall corpus) was only reachable by
typing the accent.

## Fix

Added `fold_accents()` (NFKD decompose + drop combining marks, stdlib `unicodedata`) as the single
source of truth for diacritic folding, consumed by both matching engines rather than each
re-implementing it:

- A new `card_name_folded` column, computed once at import time in `preprocess_card()` and indexed
  with its own trigram GIN index (`api/db/2026-07-20-01-accent-folded-name.sql`).
- The Rust `card_engine` reads that same column at reload (added to `ENGINE_COLUMNS`) into a new
  `card_name_folded: InlineStr<61>` field — no Rust-side folding logic needed. `name_trigram`/
  `name_bigrams` and the `NameLower` fuzzy-match path now read this field instead of
  `card_name_lower`. `ARCHIVE_FORMAT_VERSION` bumped for the layout change.
- The query word is folded the same way before it reaches either engine
  (`_handle_text_field_pattern_matching` for SQL, `_rhs_to_json` for the Rust JSON boundary), so
  "eowyn" and "Éowyn" both fold to the identical search term.

Exact-match (`!"..."`, `name=`) and regex (`name:/.../`) are untouched and stay accent-sensitive —
typing the accent still gets you exact-accent matching.

See [docs/issues/00649-accent-insensitive-name-search.md](../issues/00649-accent-insensitive-name-search.md)
for the full design writeup, corpus findings, and a known pre-existing lexer limitation
(unquoted-bare-word accents) left out of scope.

## Trade-offs

- Scoped to `card_name` only; `oracle_text`/`flavor_text`/`card_artist` fuzzy search is not folded.
- Query-time cost is unchanged — the SQL fragment swaps one indexed lowercase column for another;
  the Rust field is populated once at reload, not computed per query.

---

## Status

**Resolved — [PR #716](https://github.com/jbylund/sylvan_librarian/pull/716).**

See [PR description](../prs/accent-insensitive-name-search.md) for full details of what shipped.
