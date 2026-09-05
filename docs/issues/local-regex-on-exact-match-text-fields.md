# Regex on Exact-Match Text Fields (set, layout, cn, …)

Related: [#984](00984-engine-error-taxonomy-retry-worthiness.md) (whether engine declines should
fall back to SQL at all).

## The weirdness

The parser accepts `/…/` regex values on any text-ish attribute — including fields that are
**exact-match only** on both backends (`set`, `layout`, `border`, `watermark`, `cn`). Behavior
then diverges in a way that looks like it works but doesn't mean what the user typed.

### Example: `set:/le.*/`

| Stage | What happens |
|-------|----------------|
| Parse | OK — `RegexValueNode("le.*")` (metacharacters prevent literal lowering) |
| Engine | `RetryableQueryError: build_filter: regex not supported on card_set_code` |
| `_search` fallback | Logs at info, tries SQL |
| SQL | `(card.card_set_code = 'le.*')` — **literal equality**, not `~*` |

So the engine correctly refuses regex on set codes; SQL silently runs the wrong query (match set
code literally equal to the four-character string `le.*`, effectively empty results).

Same shape for `layout:/norm.*/`, `cn:/10.*/`, etc.

### What works today

**Four “pattern search” text fields** — regex is wired on both paths:

| Alias | Column | Engine | SQL |
|-------|--------|--------|-----|
| `name:` | `card_name` | `TextRegex` (+ trigram narrow) | `~*` |
| `o:` / `oracle:` | `oracle_text` | `TextRegex` (+ trigram narrow) | `~*` |
| `ft:` / `flavor:` | `flavor_text` | `TextRegex` (bind → `FlavorMatch`) | `~*` |
| `a:` / `artist:` | `card_artist` | `TextRegex` (bind → `ArtistMatch`) | `~*` |

**Plain literals** on exact-match fields are fine — rewrite lowers `set:/lea/` → `set:lea` before
either backend sees a regex leaf.

The gap is **metacharacter regex** on exact-match fields only.

## Root cause (two places)

1. **Engine** (`card_engine/src/filter.rs` `build_text_filter`): regex compiles only for
   `card_name`, `oracle_text`, `flavor_text`, `card_artist`. Other text attrs hit
   `regex not supported on {attr}` → `RetryableQueryError`.

2. **SQL** (`api/parsing/card_query_nodes.py` `_handle_colon_operator`): `card_set_code`,
   `card_layout`, `card_border`, `card_watermark`, `collector_number` route through the
   exact-match branch (`:` → `=`) **before** `_handle_text_field_pattern_matching`, which is the
   only place that emits `~*` for `RegexValueNode`. Regex RHS never reaches the regex SQL path.

## Why the SQL fallback is worse than a 400

For this failure mode, retrying on PostgreSQL does not implement regex — it implements a different,
unintuitive query. That contradicts the comment on `_search`'s `RetryableQueryError` handler (“the
SQL path resolves all of them correctly on its own”) and the “worth retrying” row in #984's table
(which assumed SQL supports `~*` on any text column).

Other `build_filter` decline sites (`unknown text field`, `text substring not supported`, `bad
date`, `unexpected top-level node type`) appear **unreachable** for parser-produced ASTs today;
this is the main live `RetryableQueryError` case that is both reachable and semantically wrong on
fallback.

## Options (pick one direction)

1. **Reject at parse time** — if `RegexValueNode` appears on an exact-match field (`set`, `layout`,
   `border`, `watermark`, `cn`), raise `InvalidRegexPatternError` / 400 with a clear message
   (“regex not supported on set”). Simplest; matches “these fields are exact-match only.”

2. **Fatal engine error, no SQL retry** — map `regex not supported on {attr}` to
   `FatalQueryError` (or a new subclass) when `attr` is in the exact-match set; `_search` returns
   400. Still allows parse, but stops the silent wrong SQL answer.

3. **Implement regex on these fields** — engine adds `TextRegex` for set/layout/…; SQL routes
   `RegexValueNode` on those attrs to `~*` instead of `=`. Only worth it if product wants Scryfall
   parity for regex-on-set (unclear that anyone uses this).

4. **Do nothing on SQL, fix taxonomy only** — #984 split: declines that SQL cannot faithfully
   serve should not fall back. This case is the concrete counterexample to “always retry.”

Recommendation lean: **(1) or (2)** — exact-match fields are not regex fields; failing closed beats
a empty-result lie. (1) is cheaper and catches the query before engine/SQL.

## Verification queries

```text
set:/le.*/      # engine RetryableQueryError; SQL = 'le.*'
set:/lea/       # lowered to set:lea; both OK
o:/le.*/        # both OK (~* / TextRegex)
t:/creature|instant/  # not regex on either path — type RHS serialized as literal array
```

## Tests to add when fixing

- Parse-time or `_search` 400 for `set:/le.*/` (and at least one sibling field).
- Assert SQL fallback is **not** invoked for that query (or assert no `card_set_code = 'le.*'` if
  testing SQL generation in isolation).
- Keep `set:/lea/` and `o:/le.*/` passing on both paths.
