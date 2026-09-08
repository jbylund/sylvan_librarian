# The Six Missing `order` Values, and `dir=auto`

Scryfall sorts by fifteen things. This engine sorts by eight, and `dir=auto` is not implemented at
all. Closing that gap lands in three places at once — the `order=` parameter, the in-query `order:`
directive (#893 builds its vocabulary from the `CardOrdering` enum), and the Scryfall-compatible
`/cards/search` (#912) — because all three resolve to the same enum before reaching a search path.

Everything below marked *measured* was taken from api.scryfall.com on 2026-08-09.

## What blocks it

`orderby_to_col` (`card_engine/src/lib.rs`) maps an order name to `SortCol`, with a catch-all:

```rust
_ => SortCol::EdhrecRank,
```

So adding a `CardOrdering` member and a `sql_orderby` entry without extending `SortCol` makes the
SQL path sort by the new column while the engine silently sorts by edhrec. The two paths would
disagree on identical input, which is the failure `RESULT_FIELD_COLUMNS` already warns about for
result fields. **Every order below is an engine change first and an API change second.**

## `dir=auto` — measured

Scryfall's `auto` is per-order, not a constant. Taken over `q=t:creature s:dom` by comparing the
`auto` page against the `asc` and `desc` pages:

| resolves to `desc` | resolves to `asc` |
| --- | --- |
| `released`, `rarity`, `usd`, `tix`, `eur` | `name`, `set`, `color`, `cmc`, `power`, `toughness`, `edhrec`, `penny`, `artist`, `review` |

Two of these are orders this engine already supports, so **`rarity` and `usd` are wrong today** for
any caller that passes `auto` — they sort ascending where Scryfall sorts descending.

`edhrec` resolves to **ascending**, which is the direction that puts rank 1 (Llanowar Elves, on that
query) first. Rank-descending would surface the least-played cards, so "descending popularity" and
"ascending rank" are the same thing here and the enum wants the latter.

`auto` folds to a concrete direction *before* either search path sees it, so it belongs in `_search`
next to the directive folding, not in the engine. That also means `/search` and `/cards/search` get
it from one place, and `direction:auto` / `dir:auto` come free.

## The six orders

| order | data on the printing | work |
| --- | --- | --- |
| `eur` | `price_eur: Option<u32>` (cents) | one arm each in `orderby_to_col` and `sort_key_bits`, identical to `usd` |
| `tix` | `price_tix: Option<u32>` (cents) | same |
| `released` | `released_at_int: Option<u32>` | see the f32 trap below |
| `color` | `card_colors: u8` bitmask + `TYPE_LAND` | rank function, see below |
| `set` | `card_set_code: InlineStr<8>` | needs a dense rank; string, so nothing to sort on directly |
| `artist` | `card_artist_vid: u16` | needs a dense rank; vid is intern order, not alphabetical |

### The f32 trap on `released`

`sort_key_bits` packs the primary into 32 bits via `f32_sort_bits`, and the comment on `name_rank`
already records the constraint: *"Ranks stay below 2^24 so the f32 sort-key conversion is exact."*

`released_at_int` is `yyyymmdd`, so 20260809 — three orders of magnitude past 2^24 = 16,777,216.
Fed in raw it silently loses roughly a day or two of resolution, which is invisible in every test
that does not compare two adjacent release dates.

`released_at_int` must stay `yyyymmdd` (date and year filters read it), so the sort key packs it
down instead: `y * 372 + (m - 1) * 31 + (d - 1)`, which is strictly order-preserving for valid dates
and tops out near 755,000 for year 2030. Computed inline in `sort_key_bits` — it runs once per
matching row while building the `Match` tuple, not once per comparison.

### The colour ranking — measured

Scryfall's `order=color` is eleven buckets, taken over 923 legendary creatures and lands spanning
every colour shape:

```
W → U → B → R → G → 2-colour → 3-colour → 4-colour → 5-colour → colourless → land
```

Two things worth noting, because neither is what a reader would guess. Multicolour is bucketed by
**how many** colours, not by which — within 2-colour the order is whatever the secondary sort gives,
so all guild pairs tie. And colourless sorts *after* every coloured bucket rather than before,
with lands after that; a naive popcount would put colourless first.

The rank is `0..=10`, computable from the bitmask's popcount, its single set bit for the mono cases,
and a `TYPE_LAND` check. Trivially inside the exact-f32 range.

### `set` and `artist` need a stored rank

Neither can be ordered from what the sort key can reach: a set code is a string, and
`card_artist_vid` is assigned in first-seen order by the interner, not alphabetically. Both follow
the existing `assign_name_ranks` shape — a post-load dense-rank pass writing a `u32` onto the
printing — which means two new fields on `Printing`, inherited by `APrinting` through the rkyv
derive, and a bump of `ARCHIVE_FORMAT_VERSION` so an existing store rebuilds rather than
misreading. Cost is 8 bytes per printing, ~4 MB against the build's ~305 MB floor.

## Deliberately not done

- **`penny`** — `penny_rank` lives only inside `raw_card_blob`. Needs a migration, a column, an
  import change and an engine field: a much larger change for the least-used order in the list.
- **`review`** — Scryfall-internal ordering with no public input. Not reproducible at any cost, and
  the one value on the list that should stay unsupported permanently.

Both keep falling back to `name`, which is what Scryfall itself does with an order it does not
recognize — *measured*: `order=bogus` returns the same page as no `order` at all, silently, with no
error. The only divergence is the `warnings` entry this API adds saying so.

## Order of work

1. `eur`, `tix` — near-free, and they prove the seam.
2. `released`, `color` — computed ranks, no store change.
3. `set`, `artist` — the two stored ranks and the format bump.
4. `SortDirection.AUTO` and the table above, folded in `_search`.

Steps 1, 2 and 4 touch no archived layout, so they can ship without a store rebuild; step 3 forces
one on next boot.
