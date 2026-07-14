# Custom field selection from the engine

## Goal

Let callers of `self._engine.query()` choose which fields come back per card, instead of the
fixed 9-field set `card_to_pydict` always builds today. The existing 9 fields become selectable
entries in the same mechanism, not a separate hardcoded default — `fields=None` just means "the
usual 9." Motivating case for going beyond them: the [per-card page](00495-per-card-pages.md) wants
`illustration_id` (used to build the CDN image URL) and possibly `scryfall_id`, neither of which
the API exposes right now — even though both already live on `ACard`.

## Implemented

`card_engine/src/lib.rs` now has:

- `FIELD_TABLE`: `(name, extractor)` entries for the original 9 fields
  (`name, set_code, collector_number, power, toughness, mana_cost, oracle_text, set_name,
  type_line`) plus `illustration_id`, `scryfall_id`, `price_usd`, and `prefer_score`. There's no
  separate hardcoded path for "old" vs. "new" fields — the original 9 were migrated into the same
  table.
- `DEFAULT_FIELDS`: the original 9 names; `fields=None` resolves to this.
- `resolve_fields(fields: Option<Vec<String>>)`: dedupes requested names (a name requested twice
  is only extracted once — first occurrence wins, order preserved) and validates against
  `FIELD_TABLE`, called once per query call before the per-row loop — so the per-row cost is a
  flat list of closure calls, not a string comparison per field per card.
- `UnknownFieldError(QueryError)`: raised for names outside the vocabulary. Subclasses
  `QueryError` (itself a `ValueError` subclass — see below), so it's automatically caught by
  `_search_engine`'s existing exception handling with no new Python-side branch.
- `illustration_id`/`scryfall_id` come back as real `uuid.UUID` objects, not strings. This uses
  pyo3's built-in `uuid` feature (`uuid = "1.12"` + `pyo3 = { features = ["uuid"] }"`), which
  converts `uuid::Uuid` ↔ Python's `uuid.UUID` and caches the class lookup itself, so there's no
  new per-row Python-import cost. `0` (the null sentinel from `parse_uuid_or_hash`) maps to
  `None`.
- `fields` is threaded through all four card-producing entry points: `query()`,
  `query_hashmap()`, `query_linear()`, and `sample_preferred()` — all default to `None` (today's
  9 fields) so every existing caller is unaffected.
- The 6 collection fields — `card_subtypes` (`Vec<String>`) and `card_keywords`,
  `card_oracle_tags`, `card_art_tags`, `card_is_tags`, `card_frame_data` (all `HashSet<String>`)
  — are now selectable too, each coming back as a Python `list[str]`. The `HashSet`-backed ones
  are sorted before returning (`sorted_strs()`) since `HashSet` iteration order is otherwise
  unspecified; `card_subtypes` is a `Vec` so its insertion order is already deterministic and
  isn't resorted.

As a side effect of this work, `QueryError` (subclasses `ValueError`) was added for malformed
filter/query input generally (bad filter JSON, unbuildable filter expression) and is caught in
`_search_engine` in [api_resource.py](../../../api/api_resource.py), turned into
`falcon.HTTPBadRequest` — though note that `_search_engine`'s only caller wraps it in a blanket
`except Exception` that falls back to the SQL path regardless of exception type (a deliberate
safety net for engine/SQL feature-parity gaps), so this mostly matters for direct callers of
`_search_engine`/`self._engine.query()`.

Field names must map to this fixed, vetted vocabulary rather than being arbitrary passthrough
strings — the archive is read via `access_unchecked` (see the safety comment above `query()` in
`lib.rs`), so an unvalidated field name should never be able to translate into an out-of-bounds
read or a mismatched-type interpretation of the underlying bytes.

## Exposed via `/search?fields=`

Resolved: yes, exposed publicly, not internal-only. `api/api_resource.py` now has:

- `RESULT_FIELD_COLUMNS`: public field name → `magic.cards` column, and `DEFAULT_RESULT_FIELDS`:
  the same 9 names/order as Rust's `DEFAULT_FIELDS`. This is deliberately a *subset* of
  `FIELD_TABLE`, not a mirror of it — not everything the engine can extract needs to be a public
  API field. Currently missing (engine-only): the 6 collection fields (`card_subtypes`,
  `card_keywords`, `card_oracle_tags`, `card_art_tags`, `card_is_tags`, `card_frame_data`) — a
  `fields=card_keywords` request 400s regardless of which path would've served it, since
  `_resolve_result_fields` validates against this smaller vocabulary before either path runs.
  Accepted as fine for now; revisit if a caller actually needs one of them. Every key that *is*
  here must still have a same-named `FIELD_TABLE` entry with matching semantics — there's no
  code-shared single source of truth between Rust and Python, so adding a public field means
  updating both.
- `APIResource._resolve_result_fields()`: validates/dedupes a requested `fields` list (or resolves
  `None` to the default 9), called once in `_search()` before engine/SQL dispatch. This is
  deliberately *before* the dispatch branch — the engine's `UnknownFieldError` gets swallowed by
  `_search`'s blanket `except Exception` fallback-to-SQL, so validating only inside
  `_search_engine` would let a bad field name silently degrade to SQL instead of 400ing.
- `_search_sql()` builds its `SELECT`/result-column lists dynamically from the resolved fields
  instead of the old fixed strings, so the SQL and engine paths return identical shapes for the
  same `fields=` request. Field name → column mapping is 1:1 by design (no computed/derived
  fields yet).
- The field vocabulary is now a public API contract (this was an open question — resolved in
  favor of exposing it rather than keeping it internal-only).

## Open questions

- `card_legalities` (`u64`, 2 bits/format) is still unhandled — unlike the collection fields, it
  needs an actual decoder. [card_engine/src/legality.rs](../../../card_engine/src/legality.rs)
  currently only has an *encode* path (`jsonb_obj_to_legality_bits`, used when loading from
  Postgres); nothing unpacks the bits back into a `{format: status}` dict. Doing so needs the
  `format_shifts()` registry, which is already synced before extraction runs in `query()`, so no
  `FieldExtractor` signature change should be needed — just the decoder itself. Still punted
  until there's a concrete caller.

## Remaining implementation tasks

- [x] Thread `fields` through `_search_engine` in `api_resource.py` — `fields: Sequence[str] |
      None = None`, passed straight to `self._engine.query(fields=fields, ...)`
- [x] Give `_search_sql` the same field-selection capability, so `fields=` behaves identically
      regardless of which path serves the request
- [x] Expose `fields=` on the public `/search` API
- [x] Use it in the [per-card page](00495-per-card-pages.md) fetch — done, requesting
      `illustration_id`/`price_usd`/`prefer_score` for the other-printings grid
- [ ] `card_legalities` decoder (see Open questions) — still punted, no concrete caller yet

## Related

- [00495-per-card-pages.md](00495-per-card-pages.md) — the feature this unblocks; its "Future: combining the
  two fetches" section also depends on engine internals being cheap to call twice server-side.
- [local-engine-drop-lowercase-copies.md](../local-engine-drop-lowercase-copies.md) /
  [00598-engine-collection-vocab-interning.md](00598-engine-collection-vocab-interning.md) — same
  `ACard`/archive machinery, different axis (what's stored vs. what's returned).
