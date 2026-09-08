# A Drop-In `/cards/*`: Every Scryfall Card Route, Same Wire Format

The goal is one sentence: a client written against api.scryfall.com should work against this host
after changing its base URL and nothing else. That means the same paths, the same query parameters,
the same response objects, the same pagination, and the same error bodies.

This doc is the record of what was built, the two places the stored data did not permit an exact
answer and what was done about them, and the divergences a reader should know about before pointing
production traffic here.

## The routes

Every path Scryfall documents under `/cards`:

| Route | Notes |
| --- | --- |
| `GET /cards` | Every card, 175 per page |
| `GET /cards/search` | `q`, `unique`, `order`, `dir`, `page`, `format`, `pretty`, and the three `include_*` flags |
| `GET /cards/named` | `exact` / `fuzzy`, `set`, `format`, `face`, `version`, `pretty` |
| `GET /cards/autocomplete` | Catalog of ≤20 names |
| `GET /cards/random` | Optional `q` |
| `POST /cards/collection` | ≤75 identifiers, `not_found` echoed back |
| `GET /cards/:id` | Scryfall UUID |
| `GET /cards/:code/:number(/:lang)` | Language defaults to `en`, as Scryfall's does |
| `GET /cards/multiverse\|mtgo\|arena\|tcgplayer\|cardmarket/:id` | MTGO also matches `mtgo_foil_id`, TCGplayer also `tcgplayer_etched_id` |
| `GET /cards/:id/rulings` and the four sibling addressings | New `magic.rulings` table |

`format=json` everywhere, plus `csv` on the two list routes and `text` / `image` on the single-card
routes. `format=image` is a 302 to the card's own CDN URL, which is what Scryfall answers with, so
the client ends up at the identical image.

### How they attach to the router

`_resolve_action` matches a full path first and then falls back to the first segment, passing the
rest positionally. So the five named sub-routes register their exact paths and win outright, and
everything else — nine path shapes across one to three segments — arrives at one `cards` handler
that dispatches on the segments. No routing change was needed.

The handlers live in `api/scryfall_compat/` as a mixin on `APIResource` rather than as more methods
on it: `iter_marked_routes` scans inherited attributes, so they register identically, and the
compatibility surface stays in files of its own.

## The two places the data did not permit an exact answer

### 1. The stored blob is not *quite* the object Scryfall sent

`preprocess_card` snapshots `raw_card_blob` *after* it has added `card_name`, `face_name` and
`face_idx`, and after it has normalized absent `flavor_text` to `""`. Both are exactly reversible,
and `to_scryfall_card` reverses them: drop the three keys, and drop `flavor_text` when it is `""` —
a value Scryfall never sends, so absent and empty are one state and the mapping is a bijection.

That is the whole of it, and it is worth recording how close this came to being much more.

This work originally carried a `cards.scryfall_json` column holding the untouched object for
multi-face rows, because the blob for those rows was **the front face promoted to top level**:
`card_faces` gone, `name` and `type_line` the front's, and eleven extra top-level keys. A reader
could not undo that promotion, because which keys a real card carries at top level depends on
layout — a split card has `mana_cost` and `image_uris` there, a transform card does not — so no
strip rule is correct for both.

The column was deleted in favour of fixing the blob at the source ([#894](https://github.com/jbylund/sylvan_librarian/pull/894)):
the merged row now stores the card-level object with its faces re-attached. Auditing what depended
on the promotion turned up exactly one thing, `image_uris`, and every reader of it already coalesced
to `card_faces->0`. Everything else read from the blob — `lang`, `set_type`, `games`, `finishes`,
`frame_effects`, `image_status`, `reserved`, `game_changer` — is card-level and identical either way.

The general form: `raw_card_blob` holds what Scryfall sent and derived data lives in derived
columns. A compatibility layer that needs the original should be able to read the column that is
named after it, rather than carry a second copy because the first one was pre-chewed.

**Deployment note.** Rows are rewritten by an import, not by a migration. Until the first bulk import
after deploy, a multi-face row still holds a promoted front face, and `to_scryfall_card` falls back
to presenting that face as its card — relabeled `object: "card"` under the card's full name rather
than shipped as a `card_face` claiming to be a card. It is the one window in which a payload is not
byte-identical, it affects ~2% of rows, and it closes on its own.

### 2. Rulings did not exist

New `magic.rulings` (oracle_id, source, published_at, comment), loaded from the bulk `rulings` file
the fetcher already knew about, wired into the import sequence after the tag imports.

The load is a whole-table replace in one transaction, not an upsert. The file carries no ruling id —
a row's identity is the tuple itself — and rulings are occasionally retracted, so insert-only would
accumulate withdrawn rows. It uses `DELETE` rather than `TRUNCATE` so readers keep seeing the
previous contents through MVCC instead of blocking on an `ACCESS EXCLUSIVE` lock for the load, and a
failure is logged rather than raised: rulings are the only thing in the import sequence nothing else
reads, and aborting the corpus refresh to save a rulings refresh is the wrong trade.

## Indexes

The identifiers Scryfall routes by live inside `raw_card_blob`, not in columns, so the lookups need
expression indexes. Each partial index predicates on the *same* expression its lookup compares, so
the planner can prove the query implies the predicate — `((blob->>'mtgo_id')::bigint) IS NOT NULL`
rather than `blob ? 'mtgo_id'`, which it cannot connect to the cast.

Verified against the 97,808-card dev corpus:

| Lookup | Plan |
| --- | --- |
| `/cards/mtgo/:id` | Index Scan, `idx_cards_mtgo_id` |
| `/cards/multiverse/:id` | Bitmap Index Scan, `idx_cards_multiverse_ids` (GIN, `jsonb_path_ops`) |
| `/cards/:code/:number/:lang` | Index Scan, `idx_cards_set_collector_lang` |
| `/cards/named?exact=` | BitmapOr across the three folded-name indexes |
| `/cards/named?fuzzy=` (trigram stage) | 5.6 ms |

The set-code index is on `lower(card_set_code)`: Scryfall's set codes are case-insensitive and the
lookup folds the segment. The corpus happens to store them lowercase already, but nothing constrains
it to, and indexing the column instead cost the index — the planner fell back to
`idx_cards_collector_number` and rechecked the set code and language per candidate row.

`/cards/named?exact=` also matches one face of a `Front // Back` card, which is why two `split_part`
expression indexes exist alongside the whole-name one: with all three indexed the lookup's `OR` is a
BitmapOr instead of a sequential scan.

## `/cards/random` must not be cached, twice over

The draw is non-deterministic for a fixed query and parameter set, which is exactly what both cache
layers assume away.

- `_run_query` keys on SQL text plus parameters, both invariant here, so it would have replayed the
  first card forever. The draw goes through `_run_uncached` instead.
- `CachingMiddleware` would have pinned one card as "the" random card for the whole generation. The
  response sets `Cache-Control: no-store`.

Worth recording because the test that catches this **passes on a buggy implementation under the
default suite settings**: with caching off, `_query_cache` holds one entry, so the count query and
the draw evict each other and the bug hides. The test installs the multi-entry cache production has
before drawing.

## Known divergences

Ordered by how likely they are to matter.

1. **The corpus is a filtered subset.** `preprocess_card` drops cards illegal in every format,
   tokens, playtest cards, funny sets and digital-only printings. A card this instance never
   imported 404s here and resolves on Scryfall. This is a policy of the project, not of these
   routes, and it is the largest single difference.
2. **Query syntax parity is tracked separately.** `/cards/search` hands `q` to the existing parser;
   whatever that parser supports is what these routes support. The in-flight parser work applies
   here automatically.
3. **Two `order` values have no counterpart** — `penny` and `review`. `penny_rank` lives only
   inside `raw_card_blob`, and `review` is Scryfall-internal with no public input. Both fall back
   to `name`, which is what Scryfall itself does with an order it does not recognize (measured: it
   falls back silently, no error), plus a `warnings` entry saying so. The other six of Scryfall's
   vocabulary are wired — see local-engine-order-vocabulary.md; `_ORDER_MAP` is built from
   `CardOrdering`, so anything added there is accepted here without a second edit.
4. **`include_extras`, `include_multilingual` and `include_variations` are accepted and ignored.**
   There are no extras in the corpus to include, and every printing and language it holds is
   returned unconditionally.
5. **`format=csv` column parity is best-effort.** The column list is fixed and documented in
   `routes.py` so a header does not change between pages of one result set, but it was not verified
   cell-for-cell against Scryfall's export.
6. **`format=text` is a faithful rendering, not a verified-identical one.**
7. **Card URIs are not rewritten.** `uri`, `rulings_uri` and `prints_search_uri` in a returned card
   still point at api.scryfall.com, because they are part of the payload and rewriting them would
   make it non-identical. `next_page` is the exception — it must address this host or pagination
   does not work, and its scheme comes from `Forwarded` / `X-Forwarded-Proto`. A deployment that
   terminates TLS in front of this service has to send one of those, as it already must for
   `X-Proxy-Host` to give the right host. Guessing `https` from the host name instead was tried
   and rejected: it silently breaks a plain-HTTP deployment on a real hostname — a configuration
   this project supports — to paper over one that is misconfigured.
8. **`GET /cards` at a deep offset costs ~300 ms** (measured at page 501 of 559). The endpoint is
   rarely used and the result is cached per import generation.
9. **Same-day rulings are not in Scryfall's order.** The rulings routes sort `published_at DESC,
   comment`. The descending half is Scryfall's own — measured 2026-08-12, 16 of 16 cards whose
   rulings span several dates came back newest-first and 0 oldest-first — but the tie inside one
   date is Scryfall's internal ruling id, which the bulk file does not carry. None of the file's
   own line order, that order reversed, comment ascending or comment descending reproduced it on
   any of 10 sampled cards, so `comment` is a deterministic stand-in rather than a match. It
   affects 13,847 of the 19,770 cards that have rulings (2026-08-11 dump); the other 5,923 — one
   ruling, or one per date — come back exactly as Scryfall orders them. Closing the rest would mean
   an id only the API exposes, i.e. a per-card request, which is the dependency the bulk import
   exists to avoid.

## Follow-ups worth considering

- Rewriting the self-referencing URIs behind a flag, for deployments that want clients to stay on
  this host across a whole crawl.
- `penny`, the one remaining order worth having. It needs `penny_rank` lifted out of
  `raw_card_blob` into a column of its own, plus an import change and an engine field.
- Verifying the CSV column set against a live Scryfall export.
