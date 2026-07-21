# Accent-Sensitive Matching for Accented `name:` Queries

[#718](https://github.com/jbylund/sylvan_librarian/issues/718)

Follow-up to [00649-accent-insensitive-name-search.md](00649-accent-insensitive-name-search.md)
(#649, shipped in #716). That change made fuzzy `name:` folding **symmetric**: both the stored
name and the query word are diacritic-folded, so `name:eowyn` and `name:Éowyn` produce the
identical folded search term and match identically.

## Problem

Symmetric folding is correct for the unaccented direction (`name:eowyn` should match both
"Eowyn" and "Éowyn", if both existed) but too permissive for the accented direction. Typing the
accent should narrow the search, not just restate it:

- `name:eowyn` (unaccented) → matches **both** "Eowyn" and "Éowyn".
- `name:Éowyn` (accented) → should match **only** "Éowyn", not "Eowyn".

Not currently reachable with real Scryfall data (no two cards share a base name differing only by
diacritics today), so this is a precision refinement, not a live user-facing bug — lower priority
than #649 was.

## Design

Detecting intent is free: `fold_accents(word) != word` means the user typed at least one accented
character. The interesting part is what to do with that signal without duplicating the indexing
work #716 just added.

**Key insight:** don't build a second index. Decompose an accented query into an `AND` of the
existing folded-fuzzy predicate (which narrows via the existing `name_trigram`/`name_bigrams`
index, built on `card_name_folded`) and a new *literal* (unfolded, still case-insensitive) contains
check against `card_name_lower`. The literal check never needs its own index: in `AND`
composition, `narrow_rec` (`card_engine/src/lib.rs:2781`, `FilterExpr::And` handling at
`lib.rs:3154`) already narrows using whichever children *can* narrow, and verifies non-narrowing
children with a plain per-row check on the resulting (already tiny) candidate set — that's the
existing `every_child_included` / `tight &=` machinery, not something to build. Confirmed by
reading the exhaustive-match guard rails a new `TextSearchField` variant would need to satisfy:

- `narrow_rec` ends in a catch-all `_ => None` (`lib.rs:3306`) — an unindexed variant just can't
  narrow on its own, which is exactly what we want.
- `memoize_text_predicates` (`filter.rs:826`) ends in `_ => {}` (`filter.rs:918`) — no bigram/
  trigram rewrite attempted, falls through to generic per-row evaluation.
- `printing_dependent`'s `TextSearchField` match (`filter.rs:572`) and `text_search_field_value`
  (`filter.rs:241`) *are* exhaustive and need one new arm each — small, mechanical.
- `verify_cost_tier` matches on `FilterExpr::TextContains { .. }` as a whole, not per-field
  (`filter.rs:470`) — no change needed; `TEXT_SCAN_NS100` already correctly prices a per-row scan.

So the total Rust surface is: one new enum variant, two one-line match arms, and a small change to
`build_text_filter` (`filter.rs:1615`) to emit an `And` of two `TextContains` instead of one when
Python signals an accented query — no new index, no `ARCHIVE_FORMAT_VERSION` bump.

## Implementation plan

**Rust (`card_engine/src/filter.rs`):**
1. Add `TextSearchField::NameLowerLiteral`.
2. `text_search_field_value`: `NameLowerLiteral => StrVal::Known(card.card_name_lower.as_str())`.
3. `printing_dependent`: add `NameLowerLiteral` to the `false` (card-level) arm alongside
   `NameLower`.
4. `build_text_filter` (currently: `raw_value.to_lowercase()` → single `TextContains{NameLower,
   word}` for `card_name` + `:`): if the JSON rhs kwargs carries a `literal_value` key (see below),
   emit `FilterExpr::And(vec![TextContains{NameLower, folded_word}, TextContains{NameLowerLiteral,
   literal_word}])` instead.

**Python (`api/parsing/card_query_nodes.py`):**
- `_rhs_to_json` (`CardBinaryOperatorNode`): for `card_name` + `:`, already computes
  `fold_accents(value)`. When it differs from the input, include both:
  `{"value": folded, "literal_value": value}`. Unaccented queries (the common case) omit
  `literal_value` entirely — zero JSON/behavior change there.
- `_handle_text_field_pattern_matching` (SQL path): same detection, compound SQL when accented —
  `(lower(card.card_name_folded) LIKE folded_pattern) AND (lower(card.card_name) LIKE
  literal_pattern)`. Both fragments already exist independently in the codebase (folded fuzzy,
  unfolded exact-ish `LIKE`); this just ANDs them together for this one case. No new SQL
  machinery, no new index — the existing `idx_cards_cardname_folded_lower_trgm` (folded) and
  `idx_cards_cardname_lower_trgm` (unfolded, pre-existing from #470) cover both sides.

## Test plan

Real Scryfall data can't exercise this (no accented/unaccented pair sharing a base name), so
coverage is synthetic:

- `card_engine/src/tests.rs`: extend `accent_folded_name_search_matches_unaccented_query` (or add a
  sibling test) with a *second*, unaccented card sharing the same folded name as the accented
  fixture card. Assert: unaccented query word matches both cards; accented query word matches only
  the accented one.
- `test_sql_gen.py`: assert the compound `AND` SQL and parameters for an accented `name:` query.
- `test_integration_testcontainers.py`: add the unaccented sibling card to `test_data.sql` (or a
  dedicated test), assert `name:Éowyn` (accented) returns only the accented card against a real
  Postgres instance.

## Status

**Open.** Planned as a follow-up PR to #716 rather than amending it — #716 is a clean, reviewable
unit that fully resolves #649 as reported; this is additional precision beyond what was asked for,
with its own wire-format change (Python↔Rust JSON) and test story.
