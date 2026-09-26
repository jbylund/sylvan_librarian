# Every Scryfall `/cards/*` route, so the base URL is the only thing a client changes

## What this is for

The engine already answers Scryfall's query syntax. `/search` returns it in this project's own
response shape — selected fields, a `cards` array, `limit`/`offset` — which is what the frontend
wants and stays exactly as it is.

A Scryfall-shaped client wants something different: Scryfall's response objects and pagination, plus
the per-card addressings it reaches for alongside search — `/cards/:id`, `/cards/named`,
`/cards/:code/:number`, the external id namespaces, rulings. This adds that as a second surface, so
such a client can be pointed here by changing its base URL and nothing else.

## How

`api/scryfall_compat/` adds every route Scryfall documents under `/cards`, answering with Scryfall's
own response objects, its 175-per-page pagination, and its error bodies:

- `GET /cards`, `/cards/search`, `/cards/named`, `/cards/autocomplete`, `/cards/random`
- `POST /cards/collection`
- `GET /cards/:id`, `/cards/:code/:number(/:lang)`, and the multiverse / mtgo / arena / tcgplayer /
  cardmarket id namespaces
- `GET /cards/:id/rulings` and its four sibling addressings

The handlers are a mixin on `APIResource` — `iter_marked_routes` scans inherited attributes, so they
register like any other route while the compatibility surface stays in files of its own. The
existing router needed no change: it matches a full path before falling back to the first segment,
so the five named sub-routes claim their exact paths and the other nine path shapes reach one
handler as positional segments.

Two things the stored data did not already support:

- **The card object itself.** `raw_card_blob` is snapshotted after the importer adds three keys and
  normalizes `flavor_text`; both are exactly reversible, and `to_scryfall_card` reverses them. This
  nearly needed a second column: the blob for a multi-face row used to be the front face promoted to
  top level, which no reader can undo, because which keys a real card carries at top level depends
  on layout. That was fixed at the source instead — the merged row now stores the card-level object
  with its faces re-attached — so no column holds a duplicate.
- **Rulings.** New `magic.rulings`, loaded from the bulk `rulings` file the fetcher already knew
  about, as a whole-table replace inside one transaction (the file carries no ruling id, and
  rulings are occasionally retracted). Identity being the tuple means the file can repeat one — 37
  of 77,998 entries on 2026-08-11 — so the count the import reports is what Postgres kept, not what
  was sent to it. They are served **newest first** (`published_at DESC`), which is the order
  api.scryfall.com uses: measured 2026-08-12, 16 of 16 cards whose rulings span several dates came
  back newest-first and 0 oldest-first. The tie inside one date is Scryfall's own ruling id and
  cannot be reproduced from the file — see the issue doc's divergence list.

Lookups are index-backed on the 97,808-card corpus, including three folded-name expression indexes
so that `named?exact=`, which matches either face of a `Front // Back` card, plans as a BitmapOr
rather than a sequential scan.

`/cards/random` is exempted from both cache layers: `_run_query` keys on SQL text plus parameters,
which do not vary between draws, and `CachingMiddleware` would have pinned one card per generation.

## Not covered

The corpus remains a filtered subset of Scryfall's — cards illegal in every format, tokens, funny
sets and digital-only printings are never imported — so a card this instance does not hold 404s here
and resolves there. Two `order` values -- `penny` and `review` -- have no counterpart and fall back
to `name` with a response warning, which is what Scryfall does with an order it does not recognize.
Full list of divergences in
[docs/issues/local-scryfall-cards-api.md](../issues/local-scryfall-cards-api.md).
