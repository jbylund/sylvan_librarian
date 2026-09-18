"""Post-parse query rewriting: expand derived predicates into subtrees of primitives.

Applied once at the shared post-parse seam (`post_parse.finalize_query`), so both the
production hand parser and the legacy pyparsing parser get identical treatment: the
transform operates on the common AST, after parsing and before SQL / Rust-engine
serialization (`parse => finalize_query => rest`). Nothing parser-specific lives here.

Each expansion is written as a DSL string and re-parsed with the production parser, so a
definition is expressed in the same language it targets and stays correct by construction
(no hand-built node trees to drift). Every entry is count-validated against Scryfall's
live API before landing -- the naive expansion is frequently ~97-99%, not exact -- with
the rationale and residuals recorded in docs/issues/00713-is-tag-recovery.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cachebox

from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.db_info import BOOLEAN_IS_TAGS
from api.parsing.hand_parser import parse_str_to_query as _parse_str_to_query
from api.parsing.nodes import (
    AndNode,
    BinaryOperatorNode,
    NotNode,
    OrNode,
    Query,
    RegexValueNode,
    StringValueNode,
    flatten_nested_operations,
)

if TYPE_CHECKING:
    from api.parsing.nodes import QueryNode

# (original alias, lowercased value) -> expansion DSL string. Validated against
# api.scryfall.com on 2026-07-20 (see docs/issues/00713-is-tag-recovery.md).
#
# `frame:modern/old/new` are undocumented-but-live Scryfall aliases (the syntax docs list
# only the numeric frames + frame-effects); mirrored because they see real use. `is:old`
# and `is:new` ARE documented, and match their `frame:` counterparts exactly (live count
# 2026-08-22: `is:new` = `frame:new` = 90058, vs `frame:2015` alone at 72564 -- issue #974).
_DERIVED_EXPANSIONS: dict[tuple[str, str], str] = {
    ("frame", "modern"): "frame:2003",
    ("frame", "old"): "frame:1993 or frame:1997",
    ("frame", "new"): "frame:2003 or frame:2015 or frame:future",
    ("is", "old"): "frame:1993 or frame:1997",
    ("is", "new"): "frame:2003 or frame:2015 or frame:future",
    # Type / subtype based. `kw:changeling` (an ability keyword, subtype is Shapeshifter) picks up
    # the all-creature-type cards Scryfall counts. Note party IS creature-restricted while outlaw is
    # NOT (it also matches Kindred non-creature cards carrying an outlaw subtype).
    ("is", "historic"): "t:legendary or t:artifact or t:saga",  # exact
    ("is", "permanent"): "t:creature or t:artifact or t:enchantment or t:land or t:planeswalker or t:battle",  # +2 / 25954
    ("is", "party"): "t:creature (t:cleric or t:rogue or t:warrior or t:wizard or kw:changeling)",  # exact
    ("is", "outlaw"): "t:assassin or t:mercenary or t:pirate or t:rogue or t:warlock or kw:changeling",  # exact
    # NO `is:vanilla` HERE -- it is an ENGINE predicate now, see ENGINE_IS_VALUES below. Two
    # rewrites lived on this line and neither could reach the answer. `t:creature o=""` was
    # `t:creature` exactly (18,753 against Scryfall's own 363) because `o=""` is a tautology on
    # api.scryfall.com as much as here -- `=` on a text column is the same SUBSTRING test `:` is
    # (`o=flying` = `o:flying` = 4,574) and every string contains the empty one. `-o:/./`, the
    # presence regex `has:` already uses negated, answered the same 352 on both sides and stopped
    # 11 short.
    #
    # The 11 are not one class. 12 are the FRONT-FACE class -- `is:vanilla o:/./` is 12 there and
    # all 12 are adventures whose creature FRONT prints nothing while the Instant/Sorcery half does
    # (Beluna's Gatekeeper // Entry Denied) -- and every rewrite expressible here composes
    # predicates over the MERGED row, whose oracle text is the faces' joined. The 13th moves the
    # other way: Dryad Arbor is in `t:creature -o:/./` on both sides and is NOT `is:vanilla` on
    # Scryfall, because a LAND is never vanilla there. +12 - 1 = the 11.
    ("is", "watermark"): "has:watermark",  # Scryfall accepts both spellings; 4,656 = 4,656
    # The intuitive "2/2 for 2" bear. Deliberately NOT exactly Scryfall's is:bear (which is
    # single-faced and includes Vehicles/Spacecraft): vs Scryfall this is +~14 DFC creatures
    # and -4 Vehicles/Spacecraft. Scryfall's exact count isn't cross-verifiable anyway (their
    # DFC/unique face-counting quirk), and this is what people mean by "bear".
    ("is", "bear"): "t:creature pow=2 tou=2 cmc=2",
    # Layout, exact by direct card_layout field correspondence.
    ("is", "split"): "layout:split",
    ("is", "flip"): "layout:flip",
    ("is", "transform"): "layout:transform",
    ("is", "mdfc"): "layout:modal_dfc",
    ("is", "meld"): "layout:meld",
    ("is", "leveler"): "layout:leveler",
    # `is:dfc` is NOT "the gameplay double-faced cards", and the old union was wrong in BOTH
    # directions. Measured on api.scryfall.com 2026-08-16 on two independent axes -- `unique=prints`
    # with `include_extras`+`include_variations`, and the plain default `unique=cards` -- both set
    # differences against the five layouts below are ZERO on both. It EXCLUDES meld, which the old
    # union included: `is:meld is:dfc` is 0 while `is:meld -is:dfc` is every meld printing (72
    # prints / 21 cards). And it INCLUDES the three layouts the old comment set aside as "not
    # gameplay cards and not in our corpus" -- art_series 2,650, double_faced_token 120,
    # reversible_card 81 -- two of which the import demonstrably carries: counting `layout` over the
    # 2026-08-16 all_cards bulk gives art_series 2,650 and double_faced_token 120, agreeing with
    # Scryfall exactly.
    ("is", "dfc"): (
        "layout:transform or layout:modal_dfc or layout:art_series or layout:double_faced_token or "
        "layout:reversible_card"
    ),
    # `tdfc` is `transform` under another name: `is:tdfc -is:transform` and its converse are
    # both empty on api.scryfall.com.
    ("is", "tdfc"): "layout:transform",
    # The rest of the layout family, each pinned in both directions the same way.
    #
    # `is:host` and `is:augmentation` are the SAME predicate -- Unstable's two halves together, not
    # one each. All four differences are empty: `is:host -is:augmentation`, its converse, and each
    # against `layout:host or layout:augment` (46 = 29 + 17). Written out per value rather than
    # aliased, because their equality is a measurement about Scryfall and not a spelling of ours.
    #
    # `is:token` reaches past `layout:token` to the double-faced tokens and to six Wilds of Eldraine
    # Role tokens that ship as `layout:flip` (twoe/15-17, twoc/1-2, plst/TWOE-17). The `t:token`
    # clause is what catches those six, and it cannot over-catch: `t:token -is:token` is empty on
    # Scryfall, so the union is exactly `is:token` and stays so as further odd-layout tokens are
    # printed. `is:token layout:emblem` is 0 -- an emblem is not a token.
    ("is", "artseries"): "layout:art_series",
    ("is", "augmentation"): "layout:host or layout:augment",
    ("is", "host"): "layout:host or layout:augment",
    ("is", "planar"): "layout:planar",
    ("is", "reversible"): "layout:reversible_card",
    ("is", "token"): "layout:token or layout:double_faced_token or t:token",
    # Frame-effect (stored in card_frame_data). is:colorshifted == frame:colorshifted exactly (45).
    ("is", "colorshifted"): "frame:colorshifted",
    ("is", "extendedart"): "frame:extendedart",  # 3,629 = 3,629
    ("is", "showcase"): "frame:showcase",  # 2,213 = 2,213
    # ── Land cycles: one alphabetized segment (per review) ──────────────
    # creatureland/manland keep the oracle-text heuristic: 48/49 vs Scryfall,
    # 0 false positives (the one miss is Alchemy-only and absent here).
    # `o:become` (substring), NOT `o:becomes` -- the looser form also catches
    # Crawling Barrens; the "still a land" clause keeps false positives at 0.
    # Backed by the community cycle/parent tags in Scryfall's oracle-tags
    # bulk export; ancestor propagation makes parent slugs self-updating as
    # new cycles are tagged. Plain parent tags preferred where they exist
    # (bounceland/gainland/shockland per review). Deviations from Scryfall's
    # own is: membership are accepted as community sentiment -- otag:shockland
    # includes Multiversal Passage, otag:gainland reaches newer
    # enters-tapped-gain-life cycles Scryfall's list lacks -- with counts
    # last validated against api.scryfall.com on 2026-08-07.
    ("is", "battleland"): "otag:cycle-tangoland",  # 10
    # The Amonkhet/Hour cycling duals. Scryfall spells them three ways; all three are 10.
    ("is", "bicycleland"): "otag:cycle-dual-cycling-land",  # 10, exact
    ("is", "bikeland"): "otag:cycle-dual-cycling-land",  # 10, exact
    ("is", "bondland"): "otag:cycle-bondland",  # 10
    ("is", "bounceland"): "otag:bounceland",  # 17, exact
    ("is", "canland"): "otag:cycle-horizon-land",  # 6; Scryfall's other spelling of canopyland
    ("is", "canopyland"): "otag:cycle-horizon-land",  # 6, exact
    ("is", "checkland"): "otag:cycle-checkland",  # 10, exact
    ("is", "cycleland"): "otag:cycle-dual-cycling-land",  # 10; third spelling of bikeland
    ("is", "creatureland"): "t:land o:become o:creature o:/still a.* land/",
    ("is", "dual"): "otag:cycle-abu-dual-land",  # 10, the ABUR duals, exact
    ("is", "fastland"): "otag:cycle-fastland",  # 10, exact
    ("is", "fetchland"): "otag:cycle-fetchland",  # 10, exact
    ("is", "filterland"): "otag:cycle-hybrid-filterland or otag:cycle-ody-filterland",  # 20 vs 22
    ("is", "gainland"): "otag:gainland",  # 42, self-updating superset of Scryfall's 15
    ("is", "karoo"): "otag:bounceland",  # 17; Scryfall's other spelling of bounceland
    ("is", "manland"): "t:land o:become o:creature o:/still a.* land/",
    ("is", "painland"): "otag:cycle-painland",  # 10, exact
    ("is", "pathway"): "otag:cycle-pathway",  # 10, exact
    ("is", "scryland"): "otag:cycle-block-ths-scry-land",  # 10, exact
    # shadowland/snarl: the reveal-or-tapped lands that reveal a BASIC LAND
    # TYPE card -- the basic-type regex is what separates them from the
    # Lorwyn-style typal reveal-lands, which reveal a CREATURE-type card and
    # otherwise share the wording. 10, name-verified (5 shadowlands + 5
    # snarls); no cycle tag exists for the SOI half.
    ("is", "shadowland"): "t:land o:/reveal an? (Plains|Island|Swamp|Mountain|Forest)/",
    ("is", "shockland"): "otag:shockland",  # 11, includes Multiversal Passage
    ("is", "slowland"): "otag:cycle-slowland",  # 10, exact
    ("is", "snarl"): "t:land o:/reveal an? (Plains|Island|Swamp|Mountain|Forest)/",  # same family; Scryfall accepts both
    (
        "is",
        "storageland",
    ): "otag:cycle-fem-storage-land or otag:cycle-mmq-storage-land or otag:cycle-tsp-storage-land",  # 15 vs 12
    ("is", "surveilland"): "otag:cycle-dual-surveil-land",  # 10, exact
    ("is", "tangoland"): "otag:cycle-tangoland",  # 10; Scryfall accepts both names
    # Same 10 cards as is:triome below (verified by name) -- another case of Scryfall
    # accepting two names for one cycle, like tangoland/battleland above.
    ("is", "tricycleland"): "otag:tricycle-land",  # 10, exact
    ("is", "triland"): "otag:cycle-ala-shardland or otag:cycle-ktk-wedgeland",  # 10, name-verified
    ("is", "triome"): "otag:cycle-iko-triome or otag:cycle-snc-triland",  # 10, name-verified
    # ── Non-land derivables ──────────────────────────────────────────────
    # Commander eligibility, refined per review: legendary permanents with a
    # printed toughness (creatures, Vehicles, Spacecraft -- toughness>=0, the
    # parser-friendly spelling of toughness>-1; no legendary prints negative
    # toughness and * compares as 0 on both engines) plus Backgrounds, plus
    # rules text granting eligibility outright, MINUS the commander banlist:
    # diffing the eligibility shape against Scryfall's is:commander showed it
    # excludes banned cards (Griselbrand, Golos, Emrakul, Erayo were the
    # over-catch) while keeping 329 casual not-legal legends. Residual is the
    # face-evaluation cluster from docs/issues/00713: back-face legendaries
    # over-match on combined type lines, and face-granted eligibility text
    # under-matches until faces are searchable.
    (
        "is",
        "commander",
    ): '((t:legendary (toughness>=0 or t:background)) or o:"can be your commander") -banned:commander',
    ("is", "companion"): "kw:companion",  # 10, name-verified
    ("is", "class"): "t:class",  # 34, equals Scryfall's paper count exactly
    # is:adventure is LAYOUT semantics by Scryfall's own definition -- it
    # equals `t:adventure or t:omen` there (164 = 164; Omen cards use the
    # adventure layout with an Omen-typed face), so layout is the faithful
    # mirror; the local count carries the usual corpus-policy delta only.
    ("is", "adventure"): "layout:adventure",
    ("is", "frenchvanilla"): "otag:french-vanilla",  # community tag, ~+233 looser than "keywords only"
    # The community tag tracks is:modal far better than the mode-introducing
    # wording did, and is cheaper to evaluate. Scored on Scryfall's corpus
    # against their own is:modal (800 cards, 2026-08-08), otag:modal disagrees
    # on 9 while the 'o:"choose one" or ...' union it replaces disagrees on 197
    # -- and in both directions, catching non-modal choosing ("choose two cards
    # from it") while missing modal cards worded otherwise (Sieges, Confluences).
    # Not an exact mirror of theirs, just a much closer one.
    ("is", "modal"): "otag:modal",
    # ── Set types (the `st:` operator, added alongside) ───────────────────
    # `is:masterpiece` and `is:alchemy` ARE their set types: both set differences against
    # `st:masterpiece` / `st:alchemy` are empty on api.scryfall.com (2026-08-16). `is:funny` is
    # close rather than equal -- 151 cards Scryfall calls funny are not in a funny SET, and 190
    # funny-set cards are not is:funny -- but the funny sets are not imported at all, so the
    # difference is unobservable here and the mapping is what makes the answer an honest zero
    # instead of an unexplained one.
    ("is", "alchemy"): "st:alchemy",
    ("is", "funny"): "st:funny",  # 151/190 residual, unobservable in this corpus
    ("is", "masterpiece"): "st:masterpiece",  # exact
    # ── Eligibility, in the shape is:commander already uses ───────────────
    # Each validated separately against its own live list rather than rewritten to the format
    # filter -- they are strict SUBSETS of `f:oathbreaker` / `f:brawl` / `f:duel`, not equal to
    # them. Measured 2026-08-16: every card Scryfall names is matched (the "is: minus shape"
    # difference is ZERO in all three), and the shapes over-catch by 15 / 27 / 121 on 287 / 2,318
    # / 3,323. Adding the format banlist does not close the gap, so it is recorded rather than
    # papered over -- the same standing the filterland (20 vs 22) and gainland (43 vs 15) entries
    # already have.
    ("is", "oathbreaker"): "t:planeswalker f:oathbreaker",  # +15 / 287
    (
        "is",
        "brawler",
    ): '((t:legendary (toughness>=0 or t:background)) or o:"can be your commander") f:brawl',  # +27 / 2,318
    (
        "is",
        "duelcommander",
    ): '((t:legendary (toughness>=0 or t:background)) or o:"can be your commander") f:duel',  # +121 / 3,323
    # Everything with a castable primary type on some face. Scryfall's own is:spell is FACE-level
    # and this type union is not, so the two differ on the merged type lines: +48 / 31,760 measured
    # against api.scryfall.com on 2026-08-16 (excluding funny sets, which are not imported), with
    # ZERO misses -- a strict superset, and the over-catch is the single-faced Artifact Lands plus
    # Unfinity's Attractions. `-t:land` was the other candidate and is worse in both directions:
    # 173 over, 87 under, because it drops the modal DFCs whose front face is a spell.
    (
        "is",
        "spell",
    ): "t:artifact or t:battle or t:creature or t:enchantment or t:instant or t:kindred or t:planeswalker or t:sorcery",
    # A printing that is not a reprint IS the first printing, exactly -- not approximately. Measured
    # on api.scryfall.com 2026-08-16: `is:firstprinting is:reprint` and `-is:firstprinting
    # -is:reprint` are both empty, so the two partition the printing space; `e:khm` is 425 prints,
    # 26 of them reprints and 399 first printings; `!"Lightning Bolt"` is 64 prints, 61 reprints and
    # 3 first printings. Ties all count -- `!"Forest"` answers with BOTH lea/294 and lea/295 -- which
    # falls out of the complement without a rule of its own. Scryfall accepts both spellings.
    ("is", "firstprinting"): "-is:reprint",
    ("is", "firstprint"): "-is:reprint",
    # -- Mana-symbol classes -------------------------------------------------
    # Both are SYMBOL SET membership, and the sets come from Scryfall's own /symbology (fetched
    # 2026-08-16, filtered to `represents_mana`), not from a shape guess: a symbol is HYBRID when it
    # has two or more non-Phyrexian components, and PHYREXIAN when one of its components is P. The
    # two overlap on the ten two-colour Phyrexian symbols ({G/W/P} ...), which are both, and they
    # part company on {B/P} (Phyrexian, not hybrid -- one colour) and {C/P} (the same).
    #
    # {C/P} is in the symbology but NOT in the `m:` half below: no printed cost carries it
    # (`mana:{C/P}` and `o:"{c/p}"` both answer zero on api.scryfall.com, 2026-08-21), and the
    # mana-symbol validator (#909) rejects it in a query -- `{C/P}` is not one of the shapes it
    # accepts -- so naming it would make the whole expansion a parse error for a term that can
    # match nothing.
    #
    # Verified against api.scryfall.com card for card -- all 603 `is:hybrid` and all 73
    # `is:phyrexian` fetched and diffed against the 2026-08-16 bulk: ZERO cards Scryfall names are
    # missed by either rule, and every extra this corpus would add comes from a set the import does
    # not carry (Unknown Event, Mystery Booster playtest, Heroes of the Realm).
    #
    # `m:` and not a regex over the printed cost: the cost is stored as counted SYMBOLS, so each
    # leaf is an integer compare against the mana vocab, where a regex over the cost string
    # mismatches in both directions (measured: 5 under, 35 over).
    #
    # The `o:` half of `is:phyrexian` is not decoration. Scryfall's rule is the symbol ANYWHERE on
    # the card, not only in the cost -- 36 of its 73 cards carry no Phyrexian symbol in any cost at
    # all (Spellskite, the Souleaters, every `{2}{B/P}: transform` back face) -- and dropping it
    # leaves half the answer behind. `is:hybrid` is cost-only by the same measurement: 216 cards
    # carry a hybrid symbol in their rules text and Scryfall calls none of them hybrid.
    ("is", "hybrid"): (
        "m:{W/U} or m:{W/B} or m:{U/B} or m:{U/R} or m:{B/R} or m:{B/G} or m:{R/G} or m:{R/W} or "
        "m:{G/W} or m:{G/U} or m:{W/U/P} or m:{W/B/P} or m:{U/B/P} or m:{U/R/P} or m:{B/R/P} or "
        "m:{B/G/P} or m:{R/G/P} or m:{R/W/P} or m:{G/W/P} or m:{G/U/P} or m:{2/W} or m:{2/U} or "
        "m:{2/B} or m:{2/R} or m:{2/G} or m:{C/W} or m:{C/U} or m:{C/B} or m:{C/R} or m:{C/G}"
    ),
    ("is", "phyrexian"): (
        "m:{W/P} or m:{U/P} or m:{B/P} or m:{R/P} or m:{G/P} or m:{W/U/P} or m:{W/B/P} or "
        "m:{U/B/P} or m:{U/R/P} or m:{B/R/P} or m:{B/G/P} or m:{R/G/P} or m:{R/W/P} or m:{G/W/P} or "
        'm:{G/U/P} or o:"{w/p}" or o:"{u/p}" or o:"{b/p}" or o:"{r/p}" or o:"{g/p}" or o:"{c/p}" or '
        'o:"{w/u/p}" or o:"{w/b/p}" or o:"{u/b/p}" or o:"{u/r/p}" or o:"{b/r/p}" or o:"{b/g/p}" or '
        'o:"{r/g/p}" or o:"{r/w/p}" or o:"{g/w/p}" or o:"{g/u/p}"'
    ),
    # Spelling aliases of tags the importer stores (db_info.BOOLEAN_IS_TAGS).
    # Aliased rather than stored twice: a second copy of a 3,228-card tag is bytes for nothing.
    ("is", "full"): "is:fullart",
    ("is", "promostamped"): "is:stamped",
    # The stored tag follows Scryfall's own syntax page (`is:judge_gift`); Scryfall accepts the
    # short spelling too, so it aliases on rather than storing the same rows twice.
    ("is", "judge"): "is:judge_gift",
    # The concatenated spellings of six tags stored under their syntax-page key -- the `promo_types`
    # member itself, which Scryfall accepts alongside the underscored form. Measured 2026-09-03,
    # each the same count as its target: `is:setpromo` 1,381, `is:mediainsert` 586,
    # `is:planeswalkerdeck` 180, `is:judgegift` 164, `is:arenaleague` 40, `is:intropack` 40 --
    # every one a 200 there and a warned no-match here, because the table knew only the
    # underscored key. Aliases rather than rows: the tag under either spelling is the same tag.
    ("is", "arenaleague"): "is:arena_league",
    ("is", "intropack"): "is:intro_pack",
    ("is", "judgegift"): "is:judge_gift",
    ("is", "mediainsert"): "is:media_insert",
    ("is", "planeswalkerdeck"): "is:planeswalker_deck",
    ("is", "setpromo"): "is:set_promo",
    # `rainbow` is Scryfall's short spelling of the `rainbowfoil` promo type, not a member of its
    # own: `is:rainbow` and `is:rainbowfoil` are both 183 and both set differences are empty
    # (2026-09-03). It never appears in `promo_types`, so it can only ever be an alias.
    ("is", "rainbow"): "is:rainbowfoil",
    # Two `is:` spellings of columns this parser already has, exact in both directions on
    # 2026-09-03. `is:borderless` = `border:borderless` (3,611, both set differences empty).
    # `is:tombstone` = `frame:tombstone` (113): a frame EFFECT, the one `is:` value of the 78 the
    # 2026-09-03 enumeration recovered that is not a promo type; the importer writes every
    # frame_effects member into card_frame_data, so it reaches the column as the three above do.
    ("is", "borderless"): "border:borderless",
    ("is", "tombstone"): "frame:tombstone",
}

# Scryfall's `has:` family, which asks whether a field is PRESENT rather than what it holds. The
# vocabulary was read off the live API rather than the syntax docs, which list only two of it:
# every candidate was probed on 2026-08-16 and the ones it accepts recorded here.
#
# Two shapes. The boolean half (`has:foil`, `has:booster`, …) is the SAME question `is:` asks and
# answers with the same stored tag, so it rewrites to the `is:` value. The presence half is a
# non-empty test on a text column, which `<field>:/./` already expresses -- an unanchored
# one-character regex over a column matches exactly the rows that have one.
#
# NOT here, and warning for a stated reason: `has:illustration` / `has:stamp` / `has:multiverse` /
# `has:tcgplayer` / `has:cardmarket` / `has:image` / `has:indicator` are presence tests on columns
# with no regex path (ids, interned compat scalars), and `has:attraction_lights` / `has:partner`
# have no stored column at all. Each needs a presence predicate in the engine, not a rewrite.
_HAS_EXPANSIONS: dict[str, str] = {
    # Presence on a regex-capable text column.
    "artist": "artist:/./",
    "flavor": "flavor:/./",
    "watermark": "watermark:/./",
    # The same question `is:` answers, off the same stored tag.
    "booster": "is:booster",
    "etched": "is:etched",
    "foil": "is:foil",
    "glossy": "is:glossy",
    "highres": "is:hires",
    "nonfoil": "is:nonfoil",
    "spotlight": "is:spotlight",
    "story": "is:spotlight",
    # The presence half again, for the one column that grew a predicate instead of a regex path:
    # `has:printedname` is the same question `is:localizedname` asks, and both counts on
    # api.scryfall.com are 31,294.
    "printedname": "is:localizedname",
}
_DERIVED_EXPANSIONS.update({("has", value): dsl for value, dsl in _HAS_EXPANSIONS.items()})


# The `is:` values no rewrite can express and no importer tag holds: the engine answers each from a
# field it already stores. Listed here so `SUPPORTED_IS_VALUES` covers them -- the alternative is a
# predicate that works and still warns that it does not.
#
# `localizedname` is `printed_name_folded_id != NONE_STR`, "this printing carries a printed name",
# which is also what Scryfall means. Measured 2026-08-16: 182 of the printings it matches are
# ENGLISH (om1/66 prints "Rhilex the Accursed" over Agent Venom), so it is not "non-English"; it is
# per-FACE, matching every Japanese transform printing whose printed names live on the faces and not
# at the top level; and `is:localizedname e:dsk` counts 1,917 printings there against the same 1,917
# in the bulk. Like `lang:`, its presence WIDENS the query to the foreign annex -- that is how
# api.scryfall.com answers 31,294 cards for it with no `lang:` term in sight.
#
# `unique` is "this card has been printed in exactly one SET" -- Scryfall's syntax page says so in
# as many words ("cards that have only been in a single set") -- and it is NOT prints=1: 2,847 of
# its own 16,318 have more than one printing. The set count spans every language, verified on the
# 130 cards whose only second set is a foreign-only promo (Salvat, ps11, pmei): Scryfall calls none
# of them unique.
#
# `vanilla` is "a creature whose FRONT FACE prints no rules text". The face scope is why no rewrite
# can hold it: the merged oracle text is the faces' joined, so a blank front hides behind the half
# that prints. Measured 2026-08-17 -- `is:vanilla o:/./` is 12 on api.scryfall.com and all 12 are
# adventures whose creature front is blank, while the four cards with a blank creature BACK behind a
# printing front (Kaslem's Stonetree, Ecstatic Awakener, Chosen of Markov, Skin Invasion) are not
# vanilla there. Three more rules ride on the engine leaf rather than on anything spelled here: the
# creature test is the CARD's rather than that front's (City's Blessing // Elemental is vanilla there
# and its front is not a creature), the text read is the SEARCHABLE form with reminder text stripped
# (Icehide Golem and Infinity Elemental print only reminder text and are vanilla there), and a LAND
# is never vanilla (`is:vanilla t:land` is 0 with and without `include_extras`, which is what
# excludes Dryad Arbor). 352 -> 363, and the 363 is Scryfall's own set card for card.
#
# `flavorname` is "this printing carries a flavor name", the alternate SOLD-AS name (Godzilla, the
# Secret Lair crossovers) -- a presence test on `Printing.flavor_name_id` OR any of its faces'.
# Per-PRINTING, like `localizedname`: Command Tower's sld/1864 row matches and its other 111 do
# not. Measured against api.scryfall.com on 2026-09-01: 476 cards / 661 printings, 15 of them
# carrying the key on their faces alone (vow/341, sld/1807) and 6 of them Japanese rows returned
# with no `lang:` written -- so, like `localizedname`, its presence WIDENS the query to the annex.
# Before it was listed here it parsed, reached the engine as a tag no row carries, and answered a
# no-match for `clive is:flavorname` where Scryfall answers the three alternate-name Clives.
#
# `atypical` and `default` are the FRAME CLASS -- Scryfall's "atypical frame" and its complement,
# "the default Magic frame". Not a stored tag and not a rewrite: the class is a rule over a
# printing's border, frame effects, full-art/textless flags, promo treatments and finishes, and the
# engine already evaluates exactly that rule for `prefer:atypical` (`printing_is_atypical`). PR #912
# adds `FilterExpr::Atypical`, which calls the same function over the same bound ids, so `is:atypical`
# is the predicate the prefer ranks by and the filter and the prefer cannot disagree about which
# printings are the class; `is:default` is its `Not`. Measured 2026-09-03: `is:default` 33,267 and
# `is:atypical` 10,423 over the default corpus, `is:atypical is:default` 0 and
# `is:default -is:atypical` 33,267 -- exact complements per printing, which is what makes `default` a
# `Not` rather than a second class table that could drift from this one. Both were the largest `is:`
# values this parser called unsupported.
ENGINE_IS_VALUES: frozenset[str] = frozenset({"localizedname", "unique", "vanilla", "flavorname", "atypical", "default"})


# Every `is:` value this parser can answer at all: the derivable expansions above, the booleans the
# importer stores on the row, and the two the engine answers from a stored field. Anything else
# reaches the engine as a tag no row carries and comes back as zero results with nothing to say why
# -- see `unsupported_is_warnings`. Reading BOOLEAN_IS_TAGS rather than restating it is what keeps a
# tag added to the importer from being reported unsupported by the parser.
SUPPORTED_IS_VALUES: frozenset[str] = (
    frozenset(BOOLEAN_IS_TAGS)
    | ENGINE_IS_VALUES
    | frozenset(value for alias, value in _DERIVED_EXPANSIONS if alias == "is")
)

# `has:` is a TOTAL ALIAS of `is:`, not the hand-listed subset _HAS_EXPANSIONS above is.
#
# That map was built by probing `has:`-FLAVOURED candidates -- the presence questions, and the
# boolean tags that read like presence questions -- so every value nobody thought to spell against
# `has:` was absent, and this server answers a no-match where api.scryfall.com answers a full list.
# `has:split` is the one that surfaced it (126 there, nothing here); it is not special.
#
# MEASURED against api.scryfall.com on 2026-08-17, over 22 values chosen to span every shape the
# `is:` vocabulary has -- derived layout predicates (`split`, `dfc`, `modal`, `meld`, `flip`,
# `leveler`), computed text predicates (`vanilla`, `frenchvanilla`, `permanent`, `spell`), importer
# booleans (`promo`, `digital`, `reprint`, `funny`, `token`, `extra`, `etched`, `hires`,
# `reserved`, `spotlight`, `masterpiece`) and the two set-shaped ones (`commander`, `firstprint`).
# `is:X` and `has:X` answered the SAME total_cards on all 22, with no disagreement anywhere:
#
#     is:permanent 26220 = has:permanent      is:frenchvanilla 1095 = has:frenchvanilla
#     is:split       126 = has:split          is:indicator      369 = has:indicator
#
# A value that is neither a `has:` presence test nor an `is:` tag is a 400 upstream and a warning
# here -- `has:flying` and `has:goblin` are both bad_request on api.scryfall.com -- so the alias
# widens the vocabulary without widening what counts as valid.
#
# A FALLBACK rather than entries folded into _HAS_EXPANSIONS, so the presence half keeps
# precedence: `has:watermark` asks whether a watermark is PRESENT and must not become
# `is:watermark`.
_DERIVED_EXPANSIONS.update(
    {("has", value): f"is:{value}" for value in SUPPORTED_IS_VALUES if ("has", value) not in _DERIVED_EXPANSIONS}
)

# Every `has:` value this parser can answer. Same contract as SUPPORTED_IS_VALUES, and the same
# consequence for anything outside it: a warning rather than a silent zero. Defined AFTER the
# alias update above, and after SUPPORTED_IS_VALUES, because it now reads both.
SUPPORTED_HAS_VALUES: frozenset[str] = frozenset(_HAS_EXPANSIONS) | SUPPORTED_IS_VALUES


def _leaf_key(node: QueryNode) -> tuple[str, str] | None:
    """Return `(alias, value)` for a `field:value` leaf eligible for rewriting, else None."""
    if not isinstance(node, BinaryOperatorNode) or node.operator != ":":
        return None
    alias = getattr(node.lhs, "original_attribute", None)  # the user-facing prefix, e.g. "frame"
    value = getattr(node.rhs, "value", None)
    if alias is None or not isinstance(value, str):
        return None
    return (alias, value.lower())


@cachebox.cached(cache={})
def _expanded_template(key: tuple[str, str]) -> QueryNode:
    """Fully-expanded AST for one derived-predicate key, computed once per process.

    Parses the expansion with the hand parser only (not ``parse_scryfall_query``), so synonym
    expansion does not recurse through this transform; seeding ``in_progress`` with ``key``
    breaks any cycle. Callers MUST ``_clone_expansion`` the result before splicing it into a
    query tree -- see that function for why a plain ``copy.deepcopy`` is both unsafe and, on
    these small subtrees, measured slower than re-parsing the DSL string outright.
    """
    subtree, _ = _expand(_parse_str_to_query(_DERIVED_EXPANSIONS[key]).root, frozenset({key}))
    return subtree


def _clone_expansion(node: QueryNode) -> QueryNode:
    """Structural copy of a cached expansion subtree, cheap enough to be worth caching at all.

    A later per-request pass mutates leaf nodes in place (case-normalization in
    ``card_query_nodes.to_sql``, e.g. ``self.rhs.value = ...``, ``self.operator = ...``), which
    would otherwise corrupt ``_expanded_template``'s cached result for every future query that
    reuses the same synonym -- so each leaf needs its own node and its own ``rhs`` object.
    ``lhs`` and each value node's own ``.value`` are never reassigned in place downstream, so
    those are shared, not copied.

    Deliberately not ``copy.deepcopy``: measured ~20x slower than this on these subtrees (a
    6-leaf expansion: 32us vs 1.5us) because deepcopy's generic per-object reduce/memo machinery
    dominates on custom classes -- slower, in fact, than just re-parsing the DSL string (17us),
    which is what made caching the parse pointless until this clone replaced the copy step.
    """
    cls = node.__class__
    if cls is AndNode or cls is OrNode:
        return cls([_clone_expansion(op) for op in node.operands])
    if cls is NotNode:
        return NotNode(_clone_expansion(node.operand))
    if isinstance(node, BinaryOperatorNode):
        return type(node)(node.lhs, node.operator, type(node.rhs)(node.rhs.value))
    return node


def _expand(node: QueryNode, in_progress: frozenset[tuple[str, str]]) -> tuple[QueryNode, bool]:
    """Expand derived-predicate leaves in `node`; return `(node, changed)`.

    Returns the *original* node object (and `changed=False`) when no descendant was
    rewritten, so a query containing no synonym — the overwhelming majority — is walked
    once but never rebuilt or re-flattened.
    """
    cls = node.__class__
    if cls is AndNode or cls is OrNode:
        changed = False
        operands = []
        for op in node.operands:
            new_op, op_changed = _expand(op, in_progress)
            operands.append(new_op)
            changed |= op_changed
        return (cls(operands), True) if changed else (node, False)
    if cls is NotNode:
        new_op, changed = _expand(node.operand, in_progress)
        return (NotNode(new_op), True) if changed else (node, False)
    key = _leaf_key(node)
    if key is not None and key in _DERIVED_EXPANSIONS and key not in in_progress:
        return _clone_expansion(_expanded_template(key)), True
    return node, False


def _swap_not_leaves(node: QueryNode) -> tuple[QueryNode, bool]:
    """Replace `not:value` leaves with `NotNode(is:value)`; return `(node, changed)`.

    Reuses the leaf's own operator and rhs untouched -- only `lhs` changes, from the `not`
    FieldInfo to `is`'s -- so the wrapped leaf is indistinguishable from a user-typed
    `is:value` and `expand_derived_predicates` (which runs next) still applies is:'s
    expansion table to it (`not:vanilla` negates the same subtree `is:vanilla` expands to,
    not a raw, never-populated `card_is_tags @> {"vanilla": true}` check).
    """
    cls = node.__class__
    if cls is AndNode or cls is OrNode:
        changed = False
        operands = []
        for op in node.operands:
            new_op, op_changed = _swap_not_leaves(op)
            operands.append(new_op)
            changed |= op_changed
        return (cls(operands), True) if changed else (node, False)
    if cls is NotNode:
        new_op, changed = _swap_not_leaves(node.operand)
        return (NotNode(new_op), True) if changed else (node, False)
    if isinstance(node, BinaryOperatorNode) and isinstance(node.lhs, CardAttributeNode) and node.lhs.original_attribute == "not":
        is_lhs = CardAttributeNode("is", node.lhs.matched_parser_class)
        return NotNode(type(node)(is_lhs, node.operator, node.rhs)), True
    return node, False


def negate_not_prefix(query: Query) -> Query:
    """Rewrite `not:value` leaves into `NotNode(is:value)`.

    Scryfall's docs: `is:` "has a convenient inverted mode `not:` which is the same as
    `-is:`." Runs before `expand_derived_predicates` so a `not:`-spelled derived value
    (`not:vanilla`, `not:new`, ...) gets is:'s expansion table applied underneath the
    negation, same as if the user had written `-is:vanilla` directly.
    """
    root, changed = _swap_not_leaves(query.root)
    if not changed:
        return query
    return flatten_nested_operations(Query(root))


def _regex_plain_literal(pattern: str) -> str | None:
    r"""The exact substring an unanchored, metacharacter-free regex matches, else None.

    A regex made only of literal characters (and escaped punctuation like ``\.``) is a plain
    substring search, so ``o:/sacrifice a/`` == ``o:"sacrifice a"``. Escaped punctuation unescapes
    to its literal; an alphanumeric escape (``\d`` / ``\w`` / ``\b``) is a character class -> None;
    any anchor (``^`` / ``$``) or live metacharacter -> None. Mirrors the engine's ``regex_tier``
    classification (card_engine/src/filter.rs) so the two never disagree about "plain literal".
    """
    out: list[str] = []
    it = iter(pattern)
    for c in it:
        if c == "\\":
            nxt = next(it, None)
            if nxt is None or (nxt.isascii() and nxt.isalnum()):
                return None  # class escape (\d \w \b …) or a dangling backslash
            out.append(nxt)
        elif c in ".*+?()[]{}|^$":
            return None
        else:
            out.append(c)
    return "".join(out) or None  # empty pattern matches everything -> leave it a regex


def _lower_regex_leaves(node: QueryNode) -> None:
    """Rewrite plain-literal regex leaves to substring leaves, in place.

    Only the leaf's ``rhs`` node changes (``RegexValueNode`` -> ``StringValueNode``); the tree
    shape is untouched, so — unlike ``expand_derived_predicates`` — no re-flatten is needed, and
    mutating in place preserves the leaf's concrete class (a card-specific ``BinaryOperatorNode``
    subclass) that rebuilding would drop.
    """
    if isinstance(node, (AndNode, OrNode)):
        for op in node.operands:
            _lower_regex_leaves(op)
    elif isinstance(node, NotNode):
        _lower_regex_leaves(node.operand)
    elif isinstance(node, BinaryOperatorNode) and node.operator == ":" and isinstance(node.rhs, RegexValueNode):
        literal = _regex_plain_literal(node.rhs.value)
        if literal is not None:
            node.rhs = StringValueNode(literal)


def lower_literal_regexes(query: Query) -> Query:
    r"""Rewrite plain-literal regex leaves (``o:/foo/`` -> ``o:foo``) to substring leaves.

    A metacharacter-free, unanchored regex is exactly a substring search, so this is
    behavior-preserving — but the substring form is index-backed (postgres ``gin_trgm_ops`` on the
    SQL path; the engine's trigram / oracle-word narrow) where an arbitrary regex has no index path
    and forces a full scan. Measured ~32x end-to-end on real needles (see
    docs/issues/00734-engine-string-operator-optimizations.md). Runs after
    ``expand_derived_predicates`` so any regex a synonym introduces is lowered too.
    """
    _lower_regex_leaves(query.root)
    return query


def expand_derived_predicates(query: Query) -> Query:
    """Rewrite derived-predicate leaves (frame synonyms, derivable `is:`) into primitive subtrees.

    Only rebuilds when a synonym was actually present; otherwise the query is returned
    untouched. When something was rewritten, re-flatten — a synonym expanding to an And/Or
    subtree inside a compound would otherwise leave non-canonical nesting (`(A AND (B)) AND C`),
    so the result matches the canonical tree of the equivalent hand-written query.
    """
    root, changed = _expand(query.root, frozenset())
    if not changed:
        return query
    return flatten_nested_operations(Query(root))


def _collect_unsupported_is(node: QueryNode, found: list[str]) -> None:
    """Append a warning for every `is:`/`has:` leaf naming a value this server cannot answer."""
    cls = node.__class__
    if cls is AndNode or cls is OrNode:
        for op in node.operands:
            _collect_unsupported_is(op, found)
        return
    if cls is NotNode:
        _collect_unsupported_is(node.operand, found)
        return
    key = _leaf_key(node)
    if key is None:
        return
    alias, value = key
    supported = {"is": SUPPORTED_IS_VALUES, "has": SUPPORTED_HAS_VALUES}.get(alias)
    if supported is not None and value not in supported:
        found.append(
            f"Unsupported term \u201c{alias}:{value}\u201d: this server has no data for that predicate, so it matched no cards.",
        )


def unsupported_is_warnings(query: Query) -> tuple[str, ...]:
    """Warnings for the `is:`/`has:` values in `query` this server cannot answer, in source order.

    A tag no row carries is indistinguishable from a tag every row happens to miss: both come back
    as zero results, and the caller cannot tell an empty answer from an unimplemented predicate.
    Scryfall answers an unknown `is:` value by IGNORING the term and warning (measured 2026-08-16:
    `is:notarealtag e:khm` returns the whole set with "Invalid expression ... was ignored"). This
    server keeps the term -- so the result is a no-match, not a widened one -- and says so, which is
    the honest version of the same courtesy. Whether to adopt the ignore-and-continue policy wholesale
    is a separate decision that touches every operator, not just this one.

    Runs BEFORE `expand_derived_predicates`, which replaces exactly the leaves this reads.
    """
    found: list[str] = []
    _collect_unsupported_is(query.root, found)
    return tuple(found)


def _operand_dedup_key(node: QueryNode) -> tuple:
    """Hashable key for order-insensitive dedup within one AND/OR operand list."""
    cls = node.__class__
    if cls is AndNode or cls is OrNode:
        return (cls.__name__, frozenset(_operand_dedup_key(op) for op in node.operands))
    if cls is NotNode:
        return ("NotNode", _operand_dedup_key(node.operand))
    return ("leaf", hash(node))


def _deduplicate_operand_list(operands: list[QueryNode]) -> list[QueryNode]:
    """Drop duplicate operands, keeping the first (order-insensitive within one compound)."""
    seen: set[tuple] = set()
    unique: list[QueryNode] = []
    for operand in operands:
        key = _operand_dedup_key(operand)
        if key in seen:
            continue
        seen.add(key)
        unique.append(operand)
    return unique


def _normalize_compound_operands(node: QueryNode) -> tuple[QueryNode, bool]:
    """Bottom-up flatten, dedupe, and unwrap singleton And/Or nodes."""
    cls = node.__class__
    if cls is AndNode or cls is OrNode:
        changed = False
        operands: list[QueryNode] = []
        for operand in node.operands:
            normalized, operand_changed = _normalize_compound_operands(operand)
            changed |= operand_changed
            if isinstance(normalized, cls):
                operands.extend(normalized.operands)
                changed = True
            else:
                operands.append(normalized)
        deduped = _deduplicate_operand_list(operands)
        if len(deduped) != len(operands):
            changed = True
        if len(deduped) <= 1:
            return (deduped[0], True) if deduped else (node, changed)
        if not changed:
            return node, False
        return cls(deduped), True
    if cls is NotNode:
        normalized, changed = _normalize_compound_operands(node.operand)
        return (NotNode(normalized), True) if changed else (node, False)
    return node, False


def flatten_and_deduplicate_compounds(query: Query) -> Query:
    """Flatten nested AND/OR chains, drop duplicate operands, unwrap singleton compounds.

    A single bottom-up pass merges same-type children, dedupes with order-insensitive keys
    (``AND(cmc<2, c=w)`` equals ``AND(c=w, cmc<2)`` under a shared OR), then unwraps. No separate
    pre-flatten is needed: ``_normalize_compound_operands`` already merges a same-type child into
    its parent's operand list as part of its own bottom-up walk (the ``isinstance(normalized, cls)``
    branch below), which is exactly what ``flatten_nested_operations`` does -- so calling it first
    would just flatten the same chains twice, rebuilding every node with nothing left to change.
    """
    root, changed = _normalize_compound_operands(query.root)
    if not changed:
        return query
    # Rebuilding must not shed the warnings `rewrite_query` attached earlier in the
    # pipeline — this pass runs after them (in `post_parse.finalize_query`).
    normalized = Query(root)
    normalized.warnings = query.warnings
    return normalized


# The post-parse rewrite pipeline, applied in order at the shared parse seam. Add future AST
# rewrites to this tuple — both parsers call `rewrite_query`, so a new pass lands in exactly one
# place and is guaranteed identical treatment across parsers (enforced by test_parser_parity).
_REWRITE_PASSES = (negate_not_prefix, expand_derived_predicates, lower_literal_regexes)


def rewrite_query(query: Query) -> Query:
    """Apply every post-parse AST rewrite, in order. The single seam both parsers call.

    Order is significant: `unsupported_is_warnings` runs first because it reads the very leaves
    `expand_derived_predicates` replaces; then `negate_not_prefix` (a `not:`-spelled leaf becomes
    `NotNode(is:...)`, so it reads as a plain `is:` leaf to everything after it), then
    `expand_derived_predicates` (a synonym may expand into a subtree that itself contains a
    regex or other rewritable leaf), then `lower_literal_regexes`, then any future pass
    appended to `_REWRITE_PASSES`. The warnings are re-attached afterwards because each pass
    returns a fresh Query.

    ``flatten_and_deduplicate_compounds`` runs later in ``post_parse.finalize_query`` — after
    regex-budget validation — so duplicate identical regex leaves still count toward the public
    leaf limit; it carries the attached warnings across its own rebuild.
    """
    warnings = unsupported_is_warnings(query)
    for rewrite_pass in _REWRITE_PASSES:
        query = rewrite_pass(query)
    query.warnings = warnings
    return query
