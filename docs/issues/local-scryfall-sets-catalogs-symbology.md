# The reference half of the Scryfall API: /sets, /catalog/* and /symbology

Depends on [local-scryfall-cards-api.md](local-scryfall-cards-api.md) (#912). That work covered every
route under `/cards`. This covers what is left: the twenty-six routes that describe Magic rather than
return cards, after which the Scryfall API surface is complete.

| Route | Answers with |
| --- | --- |
| `GET /sets` | List of 1,047 Set objects |
| `GET /sets/:code`, `/sets/:id` | One Set object |
| `GET /sets/tcgplayer/:id` | One Set object, by TCGplayer group id |
| `GET /catalog/:name` | Catalog object, over the twenty documented names |
| `GET /symbology` | List of 84 CardSymbol objects |
| `GET /symbology/parse-mana?cost=` | ManaCost object |

## The decision: mirror, don't derive

Every route above except `parse-mana` is served from a table mirrored off `api.scryfall.com`, not
computed from `magic.cards`. Deriving was the obvious first instinct — `/cards/autocomplete` already
derives its catalog from the corpus — and it does not survive contact with the data.

**A Set object carries eight fields no card carries.** `tcgplayer_id`, `mtgo_code`, `arena_code`,
`icon_svg_uri`, `block`, `block_code`, `parent_set_code`, `printed_size`. `GET /sets/tcgplayer/:id`
is not merely degraded without the first of them; it cannot be implemented at all.

**`card_count` is a count of Scryfall's printings, not of ours.** The corpus is a deliberate subset —
no tokens, funny sets or digital-only printings. A derived `card_count` would report a number no
other Scryfall client agrees with, on an object whose whole purpose is to be compared against theirs.

**A card symbol has no card.** `svg_uri`, `gatherer_alternates`, `loose_variant` and `english` exist
nowhere in the card data.

So the honest choice is between "mirror it and be exact" and "derive it and be internally consistent
but agree with nobody". The compatibility surface exists so a client can change its base URL and
nothing else; mirroring is what makes that true. The cost is stated plainly under Divergences below.

`parse-mana` is the exception and is computed, because it is a pure function of its parameter. That
also means it is the one route here that works before the first import.

## Schema

`api/db/2026-08-11-02-scryfall-sets-catalogs-symbology.sql` adds three tables, each storing the
upstream object whole in `jsonb` and lifting out only what a lookup keys on. Storing the object
rather than mapping it column by column means a field Scryfall adds later is served without a
migration, and it cannot be silently dropped in the mapping.

- `magic.sets` — `id` PK, `code`, `tcgplayer_id`, `position`, `set_object`
- `magic.catalogs` — `name` PK, `entries`
- `magic.card_symbols` — `symbol` PK, `position`, `symbol_object`

**`position` is the non-obvious column.** `/sets` is ordered by `released_at` descending, but sets
sharing a release date come back in an order that is neither alphabetical by code nor by name — the
four sets released 2026-11-13 arrive as `trk, trc, ttrk, sds` — and nothing in the object reproduces
it. Recomputing the ordering would silently reshuffle those groups, so the index is stored. The E2E
check asserts the served order matches upstream position for position, which is the only way to
catch a regression there.

`entries` rather than the obvious `values`, because `VALUES` is a reserved word and the column would
need quoting at every use site.

## Import

`api/scryfall_reference_import.py`, three functions, wired into `_run_import_under_lock` beside the
rulings load through `_import_reference_quietly`. Each is a whole-table replace in one transaction,
for the reason the rulings load is: the upstream response is the entire truth each time.

These are not bulk data — Scryfall publishes them as ordinary API responses — so they go through a
new `ScryfallBulkDataFetcher.fetch_api_json`, which reuses the existing retrying session rather than
opening a second HTTP client with its own behaviour.

Two failure policies, deliberately different:

- **Between the three steps**, a failure is logged and the next step still runs. Nothing downstream
  reads these tables, so one bad upstream endpoint must not cost the other two their refresh, nor
  the corpus its.
- **Within `import_catalogs`**, a single catalog that fails to fetch keeps its previous row rather
  than being written empty. Nineteen fresh catalogs and one stale one is a better answer than
  nineteen fresh and one that claims Magic has no creature types.

Measured against live Scryfall on 2026-08-11: 1,047 sets in 0.6 s, 20 catalogs / 62,187 values in
2.65 s, 84 symbols in 0.1 s.

## parse-mana, and the two rules nothing documents

`api/scryfall_compat/mana.py`. Both of these were measured, not inferred, and both are the kind of
thing a hand-written test would simply re-assert from the implementation:

1. **The normalized cost reorders colored pips into canonical colour order.** `RUW` answers
   `{U}{R}{W}`, not `{R}{U}{W}`. Every canonical ordering — allied pairs, enemy pairs, shards,
   wedges, four-colour runs, WUBRG — turns out to be a walk around the colour wheel at a constant
   step: one step for anything contiguous, two for anything not. Trying step 1 before step 2, from
   starting points in WUBRG order, reproduces Scryfall for all 31 colour combinations.
2. **Emission order is X, generic, colored pips, `{C}`**, regardless of input order, with generic
   summed into one symbol. `2XWU` answers `{X}{2}{W}{U}`; `1{1}` answers `{2}`.

An empty `cost` answers `null`; `cost=0` answers `{0}`. The two look like the same case and are not.
An unparseable fragment is a **422**, not a 400.

`api/tests/test_scryfall_mana.py` pins all of this against 79 goldens captured from the live API,
including all 31 colour subsets written both forwards and backwards.

## Cache headers are mirrored too

Measured on 2026-08-11, these routes do **not** carry the `public, max-age=57600` the card routes do:

| Route | Cache-Control |
| --- | --- |
| `/sets`, `/sets/:code`, `/sets/tcgplayer/:id`, `/catalog/*`, `/symbology` | `public` |
| `/symbology/parse-mana` | `max-age=0, private, must-revalidate` |

Both are mirrored rather than chosen, and both are mildly surprising. Bare `public` with no max-age
leaves freshness to the cache's heuristics, which is weaker than an explicit lifetime; `parse-mana`
is the one deterministic route here and would be perfectly safe to cache hard, yet upstream marks it
private. Matching anyway is the point of the surface: a client that swapped its base URL would
otherwise get a response held for 16 hours where Scryfall revalidates, and would not find out until
it served something stale. `private` does not defeat this service's own `CachingMiddleware` — only
`no-store` does, which is why `/cards/random` uses that — so repeat parses are still answered
in-process.

The first cut of this branch applied the card tier to all six routes. `test_scryfall_reference_routes.py`
now pins each header to its measured upstream value.

## Structure

`ScryfallReferenceRoutes` is a second mixin on `APIResource` beside `ScryfallCardsRoutes`, not an
addition to it. They answer from different places — the corpus and the engine on one side, the
mirrored tables on the other — and share only the response plumbing, which moved into
`ScryfallResponder` so the two surfaces cannot drift on how an error carries its status.

One trap worth recording: `falcon` must be a **runtime** import in a routes module even when it
appears only in annotations. `@route` registration runs every handler's annotations through
`typing.get_type_hints`, which evaluates them for real, so a `TYPE_CHECKING`-only import fails
registration with `NameError` before the app can serve anything. Ruff's TC002 will suggest exactly
that move; the import carries a `noqa` and a comment saying why.

## Divergences from api.scryfall.com

1. **`card_count` counts Scryfall's printings, not this instance's.** A set reporting 135 cards may
   return fewer from `/cards/search?q=e:code` here. Consistent with the corpus divergence the cards
   surface already owns, and the alternative is a number nobody else agrees with.
2. **`/catalog/card-names` and `/catalog/artist-names` list cards and artists this instance may not
   hold**, for the same reason. A client that walks a catalog and fetches each name will 404 on some.
3. **A catalog name Scryfall adds later 404s here until `CATALOG_NAMES` is extended.** The list is
   fixed in code rather than discovered, so that an unknown name is a 404 rather than a silently
   empty catalog.
4. **`uri`, `search_uri` and `icon_svg_uri` on a Set object point at Scryfall**, not at this host —
   the same rule the cards surface follows, since they are part of the payload rather than
   pagination.
5. **`parse-mana` accepts unknown bare letters** the way Scryfall does (`zzz` answers `{Z}{Z}{Z}`
   with mana value 0) but has not been probed exhaustively for exotic braced symbols beyond the
   hybrid, phyrexian and half-mana forms in the goldens.

## Follow-ups worth considering

- Serving `/sets` from the engine rather than SQL. 1,047 rows is small enough that it has not
  mattered, and unlike the card routes there is no fallback path to keep in step.
- A `set:` search operator backed by `magic.sets`, which would let `/cards/search` filter on set
  attributes — `set_type`, `block`, `digital` — that the cards table does not carry.
- Rewriting the self-referencing Set URIs behind the same flag proposed for card URIs in #912.
