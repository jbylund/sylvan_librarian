# Six more `order=` values, and `dir=auto`

## What this adds

`order=` accepted `cmc`, `edhrec`, `name`, `power`, `rarity`, `toughness`, `usd` and `cubecobra`.
It now also accepts `released`, `set`, `artist`, `color`, `eur` and `tix` — Scryfall's vocabulary
minus `penny` and `review`.

`dir=auto` is new. It is not a constant: measured against api.scryfall.com, `auto` means descending
for `released`, `rarity`, `usd`, `tix` and `eur`, and ascending for everything else. Two of those
are orderings that already existed, so `rarity` and `usd` were previously sorting the wrong way for
any caller that asked for `auto`.

All of it lands on `/search`, on `/cards/search`, and on the in-query `order:` / `sort:` /
`direction:` / `dir:` directives at once, because every route resolves to `CardOrdering` and
`SortDirection` before reaching a search path.

## How

The blocker was `orderby_to_col` in the engine, whose catch-all arm sorts by edhrec. An ordering
added to the API and not to `SortCol` does not raise — it makes the engine and the SQL builder
return differently-ordered pages for identical input. So each new ordering is an engine change
first: a `SortCol` variant and a `sort_key_bits` arm, plus

- **`released`** packs `yyyymmdd` down to `y*372 + (m-1)*31 + (d-1)` before the sort key. The key
  rounds through f32, exact only below 2^24; a raw `20260809` is past that and collides dates a day
  or two apart. `released_at_int` itself is unchanged, since the date and year filters read it.
- **`color`** is eleven buckets — `W U B R G`, multicolour by how many colours, colourless, land.
  Measured, because two parts are not what a colour bitmask gives: colourless sorts last rather
  than first, and lands after it.
- **`set` and `artist`** get dense ranks on the printing, assigned post-load like `name_rank`
  already is. Neither sorts from what the key can reach: a set code is a string, and
  `card_artist_vid` is intern order. `ARCHIVE_FORMAT_VERSION` bumps so an existing store rebuilds.

`AUTO` resolves in `_search`, before either path sees it, so the engine and SQL always receive the
same concrete direction and the cache keys on what actually ran.

## Not covered

`penny` needs `penny_rank` lifted out of `raw_card_blob` into a column; `review` is Scryfall-internal
and not reproducible. Both keep falling back to `name`, which is what Scryfall does with an order it
does not recognize — measured: it falls back silently rather than erroring.

Full design and measurements: [docs/issues/local-engine-order-vocabulary.md](../issues/local-engine-order-vocabulary.md).
