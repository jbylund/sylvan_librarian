# The rest of the Scryfall API: sets, catalogs and symbology

## What this is for

The `/cards/*` work made every card route answer the way api.scryfall.com does. That left the half of
the API that describes Magic rather than returning cards: what sets exist, what the game's
vocabularies are, and what the mana symbols mean. A client pointed at this host would get its cards
and then 404 on `/sets`.

This adds those twenty-six routes, after which the Scryfall API surface is complete.

## How

- `GET /sets`, `/sets/:code`, `/sets/:id`, `/sets/tcgplayer/:id` — 1,047 Set objects
- `GET /catalog/:name` — all twenty documented catalogs, 62,187 values between them
- `GET /symbology` — 84 CardSymbol objects
- `GET /symbology/parse-mana?cost=` — Scryfall's ManaCost object

All of it except `parse-mana` is **mirrored from Scryfall rather than derived from the corpus**, into
three new tables. That is the decision worth knowing about, and it is not the obvious one: this
project already derives `/cards/autocomplete` from its own cards. It does not survive the data here.
A Set object carries eight fields no card carries — `tcgplayer_id`, `icon_svg_uri`, `block`,
`parent_set_code` among them — and `/sets/tcgplayer/:id` cannot be implemented at all without the
first. `card_count` is a count of Scryfall's printings, and this corpus is a deliberate subset, so a
derived one would report a number no other Scryfall client agrees with. A card symbol has no card to
derive from.

The three loads are whole-table replaces in one transaction each, wired into the existing import
sequence beside the rulings load. They reach Scryfall through a new `fetch_api_json` on the bulk
fetcher, reusing its retry policy rather than opening a second HTTP client. A failure in one of the
three does not stop the other two, and inside the catalog load a single failing catalog keeps its
previous contents rather than being written empty — nineteen fresh catalogs and one stale one beats
one that claims Magic has no creature types.

`parse-mana` is computed, so it answers before the first import. Two of its behaviours are
undocumented and were measured rather than inferred: the normalized cost reorders colored pips into
canonical colour order (`RUW` answers `{U}{R}{W}`), and emission order is X, generic, colored pips,
`{C}` regardless of how the cost was written (`2XWU` answers `{X}{2}{W}{U}`). Both are pinned by 79
goldens captured from the live API, covering all 31 colour combinations written forwards and
backwards.

## Not covered

`card_count` reports Scryfall's number, not this instance's, and `/catalog/card-names` lists cards
this instance may not hold — the mirroring decision, stated plainly. A catalog name Scryfall adds
later 404s here until the list in code is extended, which is deliberate: an unknown name should not
answer as an empty catalog. Full list in
[docs/issues/local-scryfall-sets-catalogs-symbology.md](../issues/local-scryfall-sets-catalogs-symbology.md).
