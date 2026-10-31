# Remaining `is:` Tag Coverage

[#985](https://github.com/jbylund/sylvan_librarian/issues/985).

Checklist of the `is:` tags Scryfall's syntax page documents that we do **not** resolve on `main`.

Source: `GET /discover_is_tags_from_syntax` (92 tags) minus the 17 `is:` keys in
[`_DERIVED_EXPANSIONS`](../../api/parsing/rewrite.py). That rewrite table is the only path that
resolves `is:` — anything outside it parses cleanly and falls through to a `card_is_tags` JSONB
lookup on an empty column, so it is a **silent zero-result query**, not an error.

Already supported, excluded below: `bear`, `colorshifted`, `dfc`, `flip`, `historic`, `leveler`,
`manland`, `mdfc`, `meld`, `new`, `old`, `outlaw`, `party`, `permanent`, `split`, `transform`,
`vanilla`.

Recovery mechanism per tag (rewrite / build-time bit / dropped bulk field / no source) is
classified in [00713-is-tag-recovery.md](done/00713-is-tag-recovery.md). Note that every entry
there flagged `~` is an unvalidated hypothesis: the naive expansion is frequently ~97–99%, not
exact, so each definition needs a live count-check against Scryfall before it ships.

The endpoint reflects the syntax page, not the full vocabulary — `is:token`, `is:textless`, and
`is:firstprinting` are real filters absent from this list.

## Unsupported (12)

- [ ] `is:alchemy`
- [ ] `is:atypical`
- [ ] `is:brawler`
- [ ] `is:default`
- [ ] `is:digital`
- [ ] `is:duelcommander`
- [ ] `is:funny`
- [ ] `is:newinpauper`
- [ ] `is:oathbreaker`
- [ ] `is:rebalanced`
- [ ] `is:spell`
- [ ] `is:unique`

`is:atypical` / `is:default` are the frame class (Scryfall's "atypical frame" and its complement,
"the default Magic frame"), a rule over a printing's border, frame effects, full-art/textless
flags, promo treatments and finishes -- not a stored tag and not a rewrite. PR #912's engine
answers them with `FilterExpr::Atypical`, the same predicate `prefer:atypical` ranks by, with
`default` as its `Not` (measured 2026-09-03: `is:default` 33,267, `is:atypical` 10,423,
`is:atypical is:default` 0, `is:default -is:atypical` 33,267 -- exact complements per printing).
They stay unchecked here until that lands; a row here would be a second copy of the class that
could drift from the prefer.

## Supported (63 + the promo-type vocabulary below)

Via `_DERIVED_EXPANSIONS` in `api/parsing/rewrite.py`:

- [x] `is:bikeland`
- [x] `is:bondland`
- [x] `is:bounceland`
- [x] `is:canopyland`
- [x] `is:checkland`
- [x] `is:commander`
- [x] `is:companion`
- [x] `is:creatureland`
- [x] `is:dual`
- [x] `is:fastland`
- [x] `is:fetchland`
- [x] `is:filterland`
- [x] `is:frenchvanilla`
- [x] `is:gainland`
- [x] `is:modal`
- [x] `is:painland`
- [x] `is:pathway`
- [x] `is:scryland`
- [x] `is:shadowland`
- [x] `is:shockland`
- [x] `is:slowland`
- [x] `is:storageland`
- [x] `is:surveilland`
- [x] `is:tangoland`
- [x] `is:tricycleland`
- [x] `is:triland`

Via `BOOLEAN_IS_TAGS` in `api/admin_resource.py` (synced from raw_card_blob on every import):

- [x] `is:arena_league`
- [x] `is:booster`
- [x] `is:buyabox`
- [x] `is:convention`
- [x] `is:datestamped`
- [x] `is:etched`
- [x] `is:fnm`
- [x] `is:foil`
- [x] `is:full`
- [x] `is:gamechanger`
- [x] `is:gameday`
- [x] `is:giftbox`
- [x] `is:glossy`
- [x] `is:hires`
- [x] `is:hybrid`
- [x] `is:instore`
- [x] `is:intro_pack`
- [x] `is:judge_gift`
- [x] `is:league`
- [x] `is:masterpiece`
- [x] `is:media_insert`
- [x] `is:meldpart`
- [x] `is:meldresult`
- [x] `is:nonfoil`
- [x] `is:partner`
- [x] `is:phyrexian`
- [x] `is:planeswalker_deck`
- [x] `is:player_rewards`
- [x] `is:prerelease`
- [x] `is:promo`
- [x] `is:release`
- [x] `is:reprint`
- [x] `is:reserved`
- [x] `is:scryfallpreview`
- [x] `is:set_promo`
- [x] `is:spotlight`
- [x] `is:universesbeyond`

`is:meldpart` / `is:meldresult` read the `component` of the card's OWN entry in its `all_parts`
array -- every meld card carries all three entries, so `layout:meld` cannot say which side a
card is. 14 parts and 7 results on api.scryfall.com (2026-09-03), two parts per result.
"Own" is the entry under the card's id, or under its NAME when no entry carries its id: twelve
reprints in the 2026-08-16 bulk (Ragnarok fin/99b, Brisela sld/1336b, Vanille fin/211, ...) list a
sibling printing's ids, which left `is:meldresult` at 20 printings against Scryfall's 24 and
`is:meldpart` at 40 against 48 (`unique=prints`, 2026-09-26).

### Beyond the syntax page

The checklist above is the syntax page's vocabulary, and the page documents about half of what
Scryfall's search accepts: `is:serialized` (292 cards on api.scryfall.com, 2026-09-03),
`is:surgefoil` (1,584), `is:setpromo` (1,381), `is:promopack` (2,599), `is:galaxyfoil` (283),
`is:textured` (92), `is:stepandcompleat` (68) and the whole Final Fantasy family (`is:ffx`, 120
cards / 170 printings) appear nowhere on it, and each was a silent zero here.

Enumerated rather than read off the page on 2026-09-03: all 73,480 printings that can carry
`promo_types` were paged from api.scryfall.com (`-is:booster` and `is:boosterfun`, extras and
variations included), giving 115 distinct members; unioned with the page's 92 `is:` values that
made 221 candidates; every candidate outside the then-supported set was probed as `is:<value>`,
and 78 came back a 200. Each of the following is a `promo_types` member of its own name and is now
a `BOOLEAN_IS_TAGS` row (every one sparse; the largest, `promopack`, is 2,599 cards):

`beginnerbox`, `boosterfun`, `boxtopper`, `brawldeck`, `bringafriend`, `bundle`,
`chocobotrackfoil`, `commanderparty`, `commanderpromo`, `concept`, `confettifoil`, `cosmicfoil`,
`dazzlefoil`, `dossier`, `doubleexposure`, `doublerainbow`, `draculaseries`, `draftweekend`,
`dragonscalefoil`, `duels`, `embossed`, `event`, `facetfoil`, `ffi`, `ffii`, `ffiii`, `ffiv`,
`ffv`, `ffvi`, `ffvii`, `ffviii`, `ffix`, `ffx`, `ffxi`, `ffxii`, `ffxiii`, `ffxiv`, `ffxv`,
`ffxvi`, `firstplacefoil`, `fracturefoil`, `galaxyfoil`, `gilded`, `gleaminggold`,
`godzillaseries`, `halofoil`, `headliner`, `imagine`, `invisibleink`, `japanshowcase`,
`jpwalker`, `magnified`, `manafoil`, `neonink`, `oilslick`, `openhouse`, `playpromo`,
`portrait`, `poster`, `promopack`, `rainbowfoil`, `raisedfoil`, `ravnicacity`, `rebalanced`,
`resale`, `ripplefoil`, `scroll`, `serialized`, `silverfoil`, `silverscroll`, `sldbonus`,
`sourcematerial`, `stamped`, `standardshowdown`, `startercollection`, `starterdeck`,
`stepandcompleat`, `storechampionship`, `surgefoil`, `textured`, `thick`, `tourney`,
`upsidedown`, `vault`, `wizardsplaynetwork`.

#### Six more (2026-10-04)

The enumeration above paged the printings of eight queries, so its candidates were the members
those pages happened to carry. A second sweep probed 619 candidate `is:` values against
api.scryfall.com directly and found six that answer there and were a silent zero here:

| value | what it reads | Scryfall, cards / printings | rows the importer keeps (2026-10-03 bulk) |
| --- | --- | ---: | ---: |
| `is:contentwarning` | the `content_warning` flag | 7 / 28 | 26 |
| `is:premiereshop` | `promo_types` | 6 / 51 | 51 |
| `is:schinesealtart` | `promo_types` | 37 / 61 | 61 |
| `is:setextension` | `promo_types` | 46 / 50 | 50 |
| `is:singularityfoil` | `promo_types` | 1 / 1 | 1 |
| `is:themepack` | `promo_types` | 30 / 33 | 33 |

Each was established in both directions over printings (`unique=prints`): every printing Scryfall
returns carries the member, and the rows of the 2026-10-03 `default_cards` bulk file carrying each
promo type number exactly Scryfall's printings (51, 61, 50, 1, 33; the flag is on 29 rows). All six are `BOOLEAN_IS_TAGS` rows of the existing two shapes -- a top-level
boolean and `promo_types` membership -- so the import syncs them with no other change. The
content-warning rows the importer drops are the three MTGO-only printings (me1/6, me3/5,
prm/35926), by the `paper` filter in `preprocess_card`; the cards are all still found.

Seven spellings Scryfall also accepts are rewrites in `_DERIVED_EXPANSIONS` rather than rows,
because the tag under either spelling is the same tag: `is:setpromo`, `is:mediainsert`,
`is:planeswalkerdeck`, `is:judgegift`, `is:arenaleague`, `is:intropack` (the concatenated
`promo_types` member of a stored underscored key) and `is:rainbow` (183 = `is:rainbowfoil`; it
never appears in `promo_types`). Two more are other columns under an `is:` spelling, exact in
both directions: `is:borderless` = `border:borderless` (3,611) and `is:tombstone` =
`frame:tombstone` (113, a frame effect, not a promo type).

The candidates Scryfall itself rejects as `is:` values -- `acorn`, `oval`, `triangle`, `arena`,
`circle`, `snow`, `devoid`, `legendary`, `inverted`, `lesson`, `enchantment` and the DFC frame
effects -- are deliberately absent: they are `frame_effects`/`security_stamp` members that
`frame:`/`stamp:` reach, and a row would answer where Scryfall refuses.

#### A third sweep (2026-10-04): fields, lists and set types

The same probing of `is:` values found 22 more that answer on api.scryfall.com and were a silent
zero here, and one row that answered the wrong list. Only one is a promo type. Every count below is
api.scryfall.com on 2026-10-04 with extras and variations in (`unique=prints`), against the same
day's `default_cards` bulk file (118,470 rows; Scryfall held five more printings by then), and
each row of the bulk file satisfying a rule was counted by a second, independent model of it.

| value | what it reads | Scryfall | rows the importer keeps |
| --- | --- | ---: | ---: |
| `is:mtgoid` | a `mtgo_id` -- not the foil id | 63,197 | 57,365 |
| `is:arenaid` | an `arena_id` | 19,830 | 16,761 |
| `is:tcgplayer` | a `tcgplayer_id` -- not the etched id | 102,340 | 97,558 |
| `is:cardmarket` | a `cardmarket_id` | 100,433 | 96,437 |
| `is:multiverse` | a non-empty `multiverse_ids` | 69,535 | 64,364 |
| `is:illustration` | an `illustration_id` on the printing or a face | 117,712 | 99,755 |
| `is:image` | an `image_status` other than `missing` | 118,313 | 99,762 |
| `is:placeholderimage` | `image_status: placeholder` | 575 | 569 |
| `is:back` | a `card_back_id` other than the one shared Magic back | 3,331 | 2,606 |
| `is:indicator` | a colour indicator on the printing or a face | 1,006 | 918 |
| `is:fbb` | the sets fbb, bchr, ren, rin and 4bb | 1,001 | 990 |
| `is:tron` | Urza's Mine, Power Plant and Tower, by name | 96 | 78 |
| `is:vergeland` | the ten Verges, by name | 45 | 45 |
| `is:timeshifted` | frame 1997 in tsb or tsr, or `special` in plst | 247 | 247 |
| `is:moonlitland` | the `moonlitland` promo type | 5 | 5 |
| `is:dueldeck` | `set_type = duel_deck` | 1,945 | 1,838 |
| `is:fromthevault` | `set_type = from_the_vault` | 157 | 156 |

The negation of each is the plain complement (a tag is present or it is not): `-is:image` is 162 on
Scryfall and 0 here, `-is:tcgplayer` 16,135 and 2,204.

`is:image` is "not `missing`", which a printing with no status at all would satisfy; every card
object in the bulk files carries a status, so the row requires one.

Seven more spellings are rewrites, as the earlier ones are, because the tag under either
spelling is the same tag: `is:tcgplayerid`, `is:cardmarketid`, `is:multiverseid`,
`is:illustrationid` (each the same list as its target, both polarities), `is:ci` and
`is:colorindicator` (= `is:indicator`, 1,006), and `is:displaycommander` (= `is:thick`: all 97
printings of the thick-stock display commanders carry the `thick` promo type, and no other does).

**`is:scryfallpreview` changes.** The row read `preview.source = 'Scryfall'`, which 325 printings
carry, 321 of them the 2026 `slz` set (whose `source_uri` is the set page or null) and none of which
is in Scryfall's answer: `is:scryfallpreview` is 7 printings there. The four that are in it carry
the card's own page as the source URI; the other three (uma/50, grn/103 and the List reprint of it,
plst/GRN-103) carry no `preview` object at all, so they are named by set and collector number. 7 of
7 on Scryfall's list, 7 here.

**Not rows, because the importer drops every printing they would match.** `is:unset` (1,411 on
Scryfall: the Un-sets, all `funny` or legal nowhere), `is:attractionlights` (135: Attractions,
`funny`), `is:minigame` (55) and `is:vanguard` (117) (legal nowhere), and `is:treasurechest` /
`is:cube` (419: the Magic Online Treasure Chest sets, not on paper). A row for any of them would be
a tag on no printing, so each stays the silent zero it was; add the row when the importer keeps the
printings (`preprocess_card` filters on `legalities`, `games` and `set_type == "funny"`).

**Compared printing by printing**, on set and collector number, against Scryfall's own lists
(`game:paper`, since the importer drops the rest): for `is:back`, `is:indicator`, `is:fbb`, `is:tron`,
`is:vergeland`, `is:timeshifted`, `is:moonlitland`, `is:scryfallpreview`, `is:dueldeck`,
`is:fromthevault`, `is:placeholderimage` and `is:thick`, whole lists; for `-is:illustration`,
`-is:tcgplayer` and `-is:cardmarket`, the whole complements (763, 7,003 and 8,906 printings); for the
seven dense presence tests, the sixteen sets lea, mir, ulg, ons, 9ed, rav, tsp, zen, isd, ktk, m19,
eld, khm, neo, dsk and fdn (4,985 to 6,080 printings each). In every one of those comparisons nothing
here is missing from Scryfall's list and nothing Scryfall lists that the table holds is untagged:
what is on Scryfall's list and not here is a printing the importer dropped (legal nowhere, a `funny`
set, an `X // X` card, not on paper), the 725 of 3,331 `is:back` printings among them.
