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
