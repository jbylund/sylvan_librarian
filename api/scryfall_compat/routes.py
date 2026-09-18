"""The Scryfall-compatible `/cards/*` routes.

`ScryfallCardsRoutes` is a mixin on `APIResource`: it is a separate class only so that the
compatibility layer lands in its own file rather than growing `api_resource.py`, and it depends on
`_search`, `_run_query` and `_require_setup_complete` from the class it is mixed into.

Two conventions run through every handler here:

- **Every parameter is annotated `str`.** The generic binder coerces by annotation and raises its
  own `400` on a bad value, which would put a non-Scryfall error body on the wire. Parsing the
  values in the handler keeps every failure inside the Scryfall error object.
- **The router is prefix-based.** `_resolve_action` matches the full path first and then falls back
  to the first segment, so the five named sub-routes (`search`, `named`, `autocomplete`, `random`,
  `collection`) register their exact paths and everything else — `/cards`, `/cards/:id`,
  `/cards/:code/:number/:lang`, the five external-id namespaces, and the rulings variants — arrives
  at `scryfall_cards` as up to three positional segments.

What is *not* identical to api.scryfall.com is recorded in
docs/issues/local-scryfall-cards-api.md; the short version is that the corpus is a filtered subset
of Scryfall's, so a card this instance never imported 404s here and resolves there.
"""

from __future__ import annotations

import logging
import operator
import re
from functools import reduce
from typing import TYPE_CHECKING, Any, NamedTuple

import falcon
import orjson

from api.card_processing import EXTRA_IS_TAG
from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.parsing import (
    AttributeNode,
    BinaryOperatorNode,
    NotNode,
    Query,
    RegexValueNode,
    StringValueNode,
    generate_sql_query,
    parse_scryfall_query,
)
from api.parsing.card_query_nodes import fold_accents
from api.parsing.nodes import NaryOperatorNode
from api.scryfall_compat import objects
from api.scryfall_compat.objects import (
    CARD_OBJECT_FIELDS,
    DEFAULT_IMAGE_VERSION,
    MAX_AUTOCOMPLETE_VALUES,
    MAX_COLLECTION_IDENTIFIERS,
    PAGE_SIZE,
    bad_request_error,
    card_list,
    card_to_text,
    catalog_object,
    collection_list,
    error_object,
    not_found_error,
    ruling_object,
    sql_row_to_engine_row,
    to_scryfall_card,
)
from api.scryfall_compat.query_terms import scryfall_term_policy
from api.settings import settings
from api.utils import db_utils
from api.utils.routing import route

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

# Columns every card lookup needs: the blob `to_scryfall_card` reads, plus the id it is re-sorted
# by when a batch comes back in an order the caller did not ask for.
# The columns a card object is built from. Deliberately NOT raw_card_blob: there is one builder
# (objects.to_scryfall_card) and both paths go through it, so the fallback cannot answer differently
# from the engine. The blob is also no longer the card for a multi-face row -- it is the front face
# -- so building from it would silently degrade exactly the cards the merge was written to fix.
_CARD_COLUMNS = (
    "scryfall_id, oracle_id, card_name, card_layout, mana_cost_text, cmc, type_line, oracle_text, "
    "creature_power_text AS power, creature_toughness_text AS toughness, card_colors, "
    "card_color_identity, card_keywords, card_set_code, set_name, collector_number, "
    "card_rarity_int, flavor_text, card_artist AS artist, illustration_id, released_at, "
    "card_legalities, card_border, card_watermark, card_frame_data, card_is_tags, "
    "card_compat_blob, card_faces"
)

# ------------------------------------------------------------------ the by-name key rule
#
# `named?exact=` and a `POST /cards/collection` `{"name"}` identifier are two lookups over one set
# of keys, and they are NOT the same lookup. Measured against api.scryfall.com on 2026-08-31, ONE
# IDENTIFIER PER REQUEST -- a collection response's `data` is not in identifier order, and a batched
# probe silently attributes its answers to the wrong needles:
#
#   {"name":"Delver of Secrets"}                   -> Delver of Secrets // Insectile Aberration
#   {"name":"Insectile Aberration"}                -> the same card (a BACK face names it)
#   {"name":"Delver of Secrets // Insectile ..."}  -> not_found   <- `exact=` answers the card
#   {"name":"Fire // Ice"}                         -> not_found
#   {"name":"Wear // Tear"}                        -> not_found
#   {"name":"Bonecrusher Giant // Stomp"}          -> not_found
#   {"name":"Who // What // When // Where // Why"} -> und/75 (a FIVE-part name IS a key)
#   {"name":"Who"}                                 -> not_found (so is `exact=Who`)
#   {"name":"Elves"}                               -> Elves (ffdn/9), the card named that and not
#                                                     one of the hundreds containing the word
#   {"name":"limduls vault"}                       -> Lim-Dul's Vault (collated)
#   {"name":"Delver of Secrets","set":"mid"}       -> mid/47 (set FILTERS the lookup)
#
# So a card answers to its two FACE names when its name splits in EXACTLY two, and to its whole name
# otherwise -- never both. `exact=` adds the joined name of a two-faced card, and that is the only
# key the two surfaces disagree about.
#
# The ENGINE is the path that ships (`name_key_tier` in card_engine/src/lib.rs). These fragments are
# the SQL fallback saying the same thing, so the two cannot answer differently for a needle either
# of them can reach.


def _collate_name(value: str) -> str:
    """Collate a name: accent-folded, lowercased, every non-alphanumeric character removed.

    This is what Scryfall compares on both name surfaces. Measured on api.scryfall.com, 2026-08-31:
    `exact=delverofsecrets`, `exact=Lightning-Bolt`, `exact=limduls vault`,
    `exact=Kongming Sleeping Dragon` and `exact=whowhatwhenwherewhy` all resolve, as do the same
    spellings as collection identifiers -- and the folded comparison both routes used before
    answered 404 to every one of them. It subsumes trimming: `{"name":"  Lightning Bolt  "}`
    resolves there and did not here, because the collection route compared the string as posted.

    `str.isalnum` per character rather than an ASCII class, matching the engine's
    `char::is_alphanumeric`: the value is accent-folded first, so a character still non-ASCII at
    this point is one NFKD had no base letter for and must be kept, not dropped.

    Args:
        value: A name as the client spelled it.

    Returns:
        Its collated form, which is "" for a value carrying no alphanumeric character at all.
    """
    return "".join(char for char in fold_accents(value.lower()) if char.isalnum())


def _collated_sql(expr: str) -> str:
    """The SQL that collates `expr` the way `_collate_name` collates the needle.

    `[:alnum:]` is the server's character class where the engine uses Rust's
    `char::is_alphanumeric`. The two agree over ASCII, which is all `card_name_folded` holds on this
    corpus -- it is written by `fold_accents` at import. That is the one place the fallback can
    drift from the engine, and it needs a name NFKD cannot reduce to ASCII to do it.
    """
    return f"regexp_replace(lower({expr}), '[^[:alnum:]]', '', 'g')"


_NAME_FRONT = "split_part(card_name_folded, ' // ', 1)"
_NAME_BACK = "split_part(card_name_folded, ' // ', 2)"

# EXACTLY two halves, which is the load-bearing word: a name with more of them has no face keys at
# all. `exact=Who`, `exact=What` and `{"name":"Who"}` are each not_found on api.scryfall.com while
# `Who // What // When // Where // Why` answers und/75 on both surfaces -- the five-part name is the
# key and its parts are not. Part 3 being empty is what distinguishes the two cases.
_NAME_SPLITS_IN_TWO = f"({_NAME_BACK} <> '' AND split_part(card_name_folded, ' // ', 3) = '')"

_FACE_NAME_MATCH = f"(%(collated)s IN ({_collated_sql(_NAME_FRONT)}, {_collated_sql(_NAME_BACK)}))"
_WHOLE_NAME_MATCH = f"({_collated_sql('card_name_folded')} = %(collated)s)"

# A collection identifier's keys: the faces, or the whole name, never both.
_COLLECTION_NAME_MATCH = f"(CASE WHEN {_NAME_SPLITS_IN_TWO} THEN {_FACE_NAME_MATCH} ELSE {_WHOLE_NAME_MATCH} END)"

# `exact=`'s keys: the same set, plus the JOINED name of a two-faced card.
_EXACT_NAME_MATCH = f"({_WHOLE_NAME_MATCH} OR ({_NAME_SPLITS_IN_TWO} AND {_FACE_NAME_MATCH}))"

# A WHOLE-name match beats a FACE match on both surfaces, ahead of prefer_score rather than beside
# it. Without it a needle that is one card's whole name and another's face answers whichever scores
# higher: on this corpus `Lightning Bolt` would resolve "Emeritus of Conflict // Lightning Bolt".
# One expression for both scopes -- a two-faced card matched by a face cannot also carry the needle
# as its whole collated name.
_WHOLE_NAME_FIRST = f"{_WHOLE_NAME_MATCH} DESC, "
# ORDER BY term that puts an address's English printing ahead of every other language. `false`
# sorts before `true`, so a row whose card_lang IS 'en' comes first; a NULL card_lang (no such row
# survives the backfill) sorts last. `_card_at_address` carries the measurements this encodes.
_ENGLISH_FIRST = "card_lang <> 'en', "

# Path segments that name an external id namespace rather than a set code.
_EXTERNAL_ID_NAMESPACES = ("multiverse", "mtgo", "arena", "tcgplayer", "cardmarket")

# Blob keys each namespace matches. Scryfall's MTGO and TCGplayer routes each accept two ids —
# the regular printing's and the foil/etched printing's — and both resolve to the same card.
# Scryfall's `order` vocabulary, which `CardOrdering` covers except for the two below. Built from
# the enum rather than listed, so an ordering added there is accepted here without a second edit;
# the extra member that is not Scryfall's (`cubecobra`) is a harmless superset.
_ORDER_MAP: dict[str, CardOrdering] = {str(member): member for member in CardOrdering}

# The two Scryfall orders with no counterpart. `penny` needs penny_rank lifted out of raw_card_blob
# into a column; `review` is Scryfall-internal with no public input and is not reproducible at all.
# Both fall back to `name`, which is what Scryfall does with an order it does not recognize
# (measured 2026-08-09: it falls back silently), and add a warning saying so.
_SCRYFALL_ONLY_ORDERS = ("penny", "review")

# Scryfall's `dir` vocabulary. `auto` is not resolved here -- it reaches `_search` as AUTO and is
# folded against the ordering there, so this route and /search agree on what auto means.
_DIRECTION_MAP: dict[str, SortDirection] = {
    "asc": SortDirection.ASC,
    "desc": SortDirection.DESC,
    "auto": SortDirection.AUTO,
}

_UNIQUE_MAP: dict[str, UniqueOn] = {
    "cards": UniqueOn.CARD,
    "art": UniqueOn.ARTWORK,
    "prints": UniqueOn.PRINTING,
}

# The same mapping backwards, for the `next_page` echo. Scryfall echoes the RESOLVED mode and
# ordering rather than the raw parameters -- `?order=cubecobra` comes back as `order=name`,
# measured 2026-08-16 -- so a client following the link verbatim pages the SAME result set it was
# shown. Echoing the raw parameter hands it a link whose ordering the server already declined.
_UNIQUE_ECHO: dict[UniqueOn, str] = {member: spelling for spelling, member in _UNIQUE_MAP.items()}


def _echo_query(query: str) -> str:
    """`q` as Scryfall echoes it: lowercased, whitespace runs collapsed, ends trimmed.

    Measured on api.scryfall.com 2026-08-16 -- `E:KHM T:Creature OR T:Land` comes back as
    `e:khm t:creature or t:land`, `a:"Rebecca Guay"` as `a:"rebecca guay"` (inside the quotes
    too), `o:/^Whenever/` as `o:/^whenever/`, `name:Éowyn` as `name:éowyn` (so not ASCII-only),
    and a query with doubled or edge whitespace comes back collapsed and trimmed.

    Every one of those is the SAME query to this parser as well: set codes and names are folded,
    and the query regexes are case-insensitive. The echo changes the spelling and never the page.
    """
    return " ".join(query.lower().split())


def _echo_order(orderby: CardOrdering, raw_order: str) -> str:
    """`order` as Scryfall echoes it: the ordering that was SERVED.

    One exception, and it is measured: an ordering Scryfall recognizes and this server does not
    (`penny`, `review`) comes back spelled as the client sent it, because Scryfall did sort by it.
    Echoing `name` there still round-trips here -- page 2 falls back exactly as page 1 did -- but
    it differs from Scryfall for no gain.
    """
    lowered = raw_order.lower()
    if lowered in _SCRYFALL_ONLY_ORDERS and lowered not in _ORDER_MAP:
        return lowered
    return str(orderby)


class _ExtrasTriggers(NamedTuple):
    """What a query's parse tree says about Scryfall's `include_extras` auto-enable."""

    forced: bool
    """An UNCONDITIONAL trigger term is present: extras are on whatever the caller asked for."""

    sets: tuple[str, ...]
    """The set codes named by `e:`/`s:`/`set:`, lowercased -- the CONDITIONAL trigger."""

    def __or__(self, other: _ExtrasTriggers) -> _ExtrasTriggers:
        """Merge two subtrees. Both kinds of trigger propagate upward, so this is a union."""
        return _ExtrasTriggers(self.forced or other.forced, self.sets + other.sets)


_NO_EXTRAS_TRIGGERS = _ExtrasTriggers(forced=False, sets=())

# Attributes whose mere PRESENCE forces `include_extras=true`, whatever their value:
# `a:`, `wm:` and `layout:`.
_UNCONDITIONAL_EXTRAS_ATTRIBUTES = frozenset({"card_artist", "card_watermark", "card_layout"})

# The VALUE-specific triggers, as {attribute: triggering values}. `t:` binds to `card_types` or
# `card_subtypes` depending on which vocabulary the value is in, and "Token" is in both, so both
# spellings are listed.
#
# `is:` HAS FIVE OF THEM, not one. Every STORED `is:` value the parser supports (32) was probed for
# the `include_extras` echo on 2026-08-16, one query each: `is:extra` (10,818), `is:oversized`
# (726), `is:reserved` (1,477), `is:rebalanced` (221) and `is:glossy` (7) echo true, and the other
# 27 echo false. The negative half is the load-bearing one, because several of the values that echo
# FALSE plainly contain extras and so would look like triggers to a count-based test:
# `is:variation` is 93 bare against 97 with the flag, `is:convention` 63 against 67, `is:judge` 173
# against 176, `is:league` 6 against 18.
#
# `glossy` was MISSED the first time, and the reason is the whole lesson of this rule. It holds no
# extras at all, so the flag cannot move its count, and it was set aside as unfalsifiable. The ECHO
# moves anyway -- the rule is syntactic, not a property of the result set -- so a count-based probe
# cannot measure it and the echo is the only instrument that can.
#
# `border:silver` is here for the same reason and has the cleanest control in the whole rule.
# `border:gold` answers 0 bare and 1,373 with `include_extras=true` -- every gold border is a World
# Championship card, so the whole population is memorabilia -- which is exactly what a NON-trigger
# on this attribute looks like. `border:silver` answers 665 both ways and echoes true unsent, and
# only 108 of those 665 are `is:extra`, so the counts are not coinciding by accident.
# `border:black`, `border:white` and `border:borderless` echo false, as does `frame:` at every
# value.
_VALUE_EXTRAS_TRIGGERS = {
    "card_types": frozenset({"token"}),
    "card_subtypes": frozenset({"token"}),
    "card_is_tags": frozenset({EXTRA_IS_TAG, "glossy", "oversized", "reserved", "rebalanced"}),
    "card_border": frozenset({"silver"}),
}

# The DERIVED terms that force extras on -- the ones `expand_derived_predicates` replaces with a
# subtree, so that by the time this walk runs the spelling the rule reads is gone.
#
# IT IS A MEASURED LIST AND NOT A RULE, and the probe was run to find a rule. All 90 values the
# rewrite expands (77 `is:`, 12 `has:`, 3 `frame:`) were probed one at a time against
# api.scryfall.com on 2026-08-16 -- `<term> or cmc=3` sent with `include_extras=false`, reading the
# resolved flag back out of the `next_page` echo -- and re-probed against a second base
# (`<term> or t:goblin`) with identical verdicts. Twelve fire; the other 78 do not.
#
# EVERY STRUCTURAL HYPOTHESIS IS REFUTED BY THE TABLE, in both directions:
#
#  - NOT "what it expands to". `is:split`, `is:flip`, `is:transform`, `is:tdfc`, `is:meld`,
#    `is:leveler` and `is:adventure` all expand to `layout:`, an unconditional trigger, and Scryfall
#    fires for none of them. `has:artist` expands to `artist:/./` and does not fire either.
#  - NOT "the population contains extras". `is:mdfc` fires with 327 printings and ZERO of them
#    `is:extra`; `is:glossy` fires with 7 and zero. `is:stamped` does NOT fire with 696 extras out
#    of 3,195 -- the largest extras share in the table.
#  - NOT the layout family. `is:mdfc` fires while `is:transform` and `is:meld` do not, and
#    `is:dfc` -- which overlaps both -- fires. (`is:dfc` is NOT the union of the three,
#    measured separately: it excludes meld outright and reaches art_series,
#    double_faced_token and reversible_card. See `_DERIVED_EXPANSIONS`. The refutation stands
#    either way; the union was the wrong name for it.)
#
# So it is Scryfall's own per-value table, and the honest way to mirror it is to copy it down.
# Re-derive it by re-running the probe, not by reasoning about the values.
#
# THE WHOLE MEASUREMENT IS KEPT, not the part this parser can reach today: `_DERIVED_EXPANSIONS`
# defines nine of these twelve and has no `has:` alias at all, so `is:token`, `is:planar`,
# `is:funny`, `is:artseries`, `is:augmentation`, `is:host`, `is:reversible`, `is:watermark`,
# `has:watermark` and `has:glossy` are inert here. Pruning them to today's vocabulary would quietly
# lose the measurement the day one of those values is added, and re-probing is 90 live requests.
_EXTRAS_DERIVED_TRIGGERS = frozenset(
    {
        "has:glossy",  # == is:glossy, and it fires there too
        "has:watermark",  # == is:watermark; `wm:` is an unconditional trigger and this agrees
        "is:artseries",
        "is:augmentation",
        "is:dfc",
        "is:funny",
        "is:host",
        "is:mdfc",
        "is:planar",
        "is:reversible",
        "is:token",
        "is:watermark",
    }
)

# The set-code attribute every spelling of the set operator (`e:`, `s:`, `set:`) rewrites to.
_SET_CODE_ATTRIBUTE = "card_set_code"


def _fold_directives_for_echo(
    parsed: Query,
    *,
    unique: UniqueOn,
    orderby: CardOrdering,
    direction: SortDirection,
    prefer: PreferOrder,
) -> tuple[UniqueOn, CardOrdering, SortDirection, PreferOrder]:
    """The effective result shape after the query's own directives, warnings discarded.

    THE SAME FOLD `_search` RUNS, reached through the same helper rather than reimplemented --
    a second copy of Scryfall's precedence rules is exactly the thing that drifts. It is run
    twice per request (here for the `next_page` echo, and again inside `_search` for the search
    itself) and that is safe because folding is idempotent: a directive SETS a value, so folding
    it over a value it already set changes nothing.

    Warnings are dropped here and taken from the search result instead, so the response reports
    each one once.

    Args:
        parsed: The parsed query, read for its directives.
        unique: The unique mode from the query parameters.
        orderby: The ordering from the query parameters.
        direction: The sort direction from the query parameters.
        prefer: The prefer order from the query parameters.

    Returns:
        The effective (unique, orderby, direction, prefer).
    """
    # Imported at call time, not at module scope: this module is a MIXIN on `APIResource` and is
    # imported by `api_resource` on the way up, so a top-level import here would be a cycle.
    from api.api_resource import _fold_directives  # noqa: PLC0415

    unique, orderby, direction, prefer, _warnings = _fold_directives(
        parsed.directives,
        unique=unique,
        orderby=orderby,
        direction=direction,
        prefer=prefer,
    )
    return unique, orderby, direction, prefer


def _extras_triggers(node: object) -> _ExtrasTriggers:
    """Read Scryfall's `include_extras` auto-enable off a parsed query.

    MEASURED, not inferred -- ~119 serial probes against api.scryfall.com on 2026-08-16, and the
    result contradicts the obvious hypothesis. It is NOT a property of the RESULT SET: `t:creature`,
    `o:draw` and `ft:death` match 1,742 / 358 / 26 extras respectively and every one of them echoes
    `include_extras=false`. It is a SYNTACTIC property of the terms, propagated through `or`, `and`
    and negation alike -- `e:war or e:lea` is true, `(e:lea t:creature) or t:land` is true even
    though LEA's only extra is an enchantment that cannot be in that result set, `-e:lea t:land` is
    true and `-e:war t:land` is false. And it is a FORCE, not a default: an explicit
    `include_extras=false` is overridden, in the echo and in the rows.

    Unconditional triggers: `a:`, `wm:`, `layout:`, `name:/regex/`, `t:token` and `is:extra`
    (`b:` belongs here too -- this parser has no block operator, so the term cannot reach us).
    Each fires on the TERM and not on what it matches: `a:"Wesley Burt"` triggers although
    `a:"Wesley Burt" is:extra` is 0, `name:/zzzqq/` matches nothing and still triggers,
    `layout:normal` triggers. Deliberately NOT triggers, each probed: `t:` at any other value,
    `o:`, `o:/…/`, `t:/…/`, `cn:`, `st:`, `year:`/`date:`, `border:`, `frame:`, `is:` at any other
    value, `name:"literal"`, a bare `name:` word, and `!"Exact"`.

    Conditional trigger: a set term, IFF that set holds at least one `is:extra` printing --
    `QueryEngine.sets_with_extras` is that table. Over 18 measured sets the split is perfect
    (lea/leb/2ed/3ed/sum 1, 4ed/5ed/6ed 2, leg 4, j21 16, hbg 122, unk 506 enable; ust, ice, war,
    unf, por and 7ed hold 0 and none of them does), and six more predicted from the local bulk
    before being measured were all correct. LEA is the instructive one: its single extra is
    Crusade, which carries `content_warning`.

    RANKED on the 57 set probes: this rule 57/57, against 20/57 for a "does the query mention a
    set" rule -- which is wrong on every ordinary modern set. Across the non-set probes that rule
    also missed `name:/…/`, `layout:`, `t:token` and `is:extra`.

    A DERIVED `is:` FIRES ON THE TERM, NOT ON WHAT IT EXPANDED INTO. `expand_derived_predicates`
    replaces `is:split` with `layout:split` before this walk runs and `layout:` is an unconditional
    trigger, so without `derived_from` every layout-derived `is:` would fire and Scryfall fires for
    none of them. `_EXTRAS_DERIVED_TRIGGERS` is the measured list of the twelve that do.

    KNOWN RESIDUAL, in both directions and from one cause: the rewrite lowers a regex with no
    metacharacters to a literal before this walk sees it. So `name:/zzzqq/` reads here as
    `name:"zzzqq"` and does NOT trigger where Scryfall does, and `t:/token/` reads as `t:token`
    and DOES where Scryfall does not. `name:/^z/`, `t:/^token$/` and every other real pattern keep
    their `RegexValueNode` and behave.

    Args:
        node: A parsed query, or any node inside one.

    Returns:
        The unconditional verdict, and the set codes to look up in the engine's table.
    """
    if isinstance(node, Query):
        return _extras_triggers(node.root)
    if isinstance(node, NaryOperatorNode):
        # `and` and `or` alike: a trigger anywhere under either one triggers the whole query.
        return reduce(operator.or_, (_extras_triggers(child) for child in node.operands), _NO_EXTRAS_TRIGGERS)
    if isinstance(node, NotNode):
        # NEGATION DOES NOT CANCEL A TRIGGER, which is the measurement that makes this syntactic
        # rather than semantic: `-e:lea t:land` enables extras and `-e:war t:land` does not, even
        # though neither result set can contain an LEA card.
        return _extras_triggers(node.operand)
    if isinstance(node, BinaryOperatorNode):
        return _extras_triggers_of_term(node)
    return _NO_EXTRAS_TRIGGERS


def _extras_triggers_of_term(node: BinaryOperatorNode) -> _ExtrasTriggers:
    """The trigger verdict for a single comparison term. See `_extras_triggers`."""
    # A LEAF A REWRITE INVENTED ANSWERS FOR THE TERM THE CALLER WROTE, not for itself. This is the
    # `regex_derived` problem one rewrite further along: `expand_derived_predicates` turns
    # `is:split` into `layout:split` before this walk runs, `layout:` is an unconditional trigger,
    # and `is:split` is not one -- 327 there against the 347 `layout:split` answers. Reading the
    # ORIGIN term settles both directions at once, and exactly: nothing this expansion produced can
    # trigger on its own account, and `has:glossy` still fires although the `is:glossy` leaf it
    # leaves behind must not fire as a leaf.
    derived_from = getattr(node, "derived_from", None)
    if derived_from is not None:
        return _ExtrasTriggers(forced=derived_from in _EXTRAS_DERIVED_TRIGGERS, sets=())
    lhs = node.lhs
    if not isinstance(lhs, AttributeNode):
        return _NO_EXTRAS_TRIGGERS
    attribute = lhs.attribute_name
    if attribute in _UNCONDITIONAL_EXTRAS_ATTRIBUTES:
        return _ExtrasTriggers(forced=True, sets=())
    # `name:/…/` triggers and `name:"…"` does not, so the VALUE NODE is what decides -- the
    # attribute is the same either way.
    #
    # `regex_derived` is the second half of that test and not an optional refinement:
    # `lower_literal_regexes` rewrites a metacharacter-free `name:/zzzqq/` into a quoted literal
    # BEFORE this walk ever runs, so without the flag exactly the regexes with no metacharacters
    # in them stop triggering. Measured 2026-08-16: `name:/bolt/` sent with `include_extras=false`
    # answers 175 -- its extras-on count -- where `name:"bolt"` answers 157.
    if attribute == "card_name" and (isinstance(node.rhs, RegexValueNode) or getattr(node.rhs, "regex_derived", False)):
        return _ExtrasTriggers(forced=True, sets=())
    if not isinstance(node.rhs, StringValueNode):
        return _NO_EXTRAS_TRIGGERS
    value = str(node.rhs.value).lower()
    # THE OTHER DIRECTION OF THE SAME LOWERING, and the reason `regex_derived` is read twice here.
    # A VALUE-specific trigger fires only when it was NOT written as a regex: `t:token cmc=3` is 6
    # on api.scryfall.com (extras auto-enabled) where `t:/token/ cmc=3` is 0, and `is:/extra/
    # cmc=3` and `border:/silver/ cmc=3` both answer plain `cmc=3` (22,832) echoing false. After
    # `lower_literal_regexes` each regex form is the same node as its plain spelling, so without
    # the flag this fires on all three.
    if value in _VALUE_EXTRAS_TRIGGERS.get(attribute, frozenset()) and not node.rhs.regex_derived:
        return _ExtrasTriggers(forced=True, sets=())
    if attribute == "card_legalities" and _legality_term_triggers(node, value):
        return _ExtrasTriggers(forced=True, sets=())
    if attribute == _SET_CODE_ATTRIBUTE:
        return _ExtrasTriggers(forced=False, sets=(value,))
    return _NO_EXTRAS_TRIGGERS


def _legality_term_triggers(node: BinaryOperatorNode, value: str) -> bool:
    """`banned:` at any value, and `f:`/`format:`/`legal:` at exactly `premodern`.

    Every legality alias binds to `card_legalities`, so the ALIAS is what separates them.
    Measured on api.scryfall.com 2026-08-16: `banned:legacy`, `banned:vintage`, `banned:modern` and
    `banned:pauper` all echo `include_extras=true` while `restricted:vintage` echoes false; and of
    the 21 format values probed one at a time, `premodern` is the ONLY one that fires (9,187 rows
    echoing true, against false for standard/modern/legacy/pauper/vintage/commander/oldschool/predh
    and the rest). `legal:premodern` fires too and `-f:premodern t:land` fires, so it is the value
    rather than the alias, and negation does not cancel it.
    """
    lhs = node.lhs
    original = getattr(lhs, "original_attribute", None) if isinstance(lhs, AttributeNode) else None
    return original == "banned" or value == "premodern"


def _mentions_is_tag(node: object, tag: str) -> bool:
    """Whether the parsed query names `is:<tag>` anywhere -- under `or`, `and` and negation alike.

    The whole of `include_variations`'s auto-enable rule; see the call site for the measurements
    that make it the whole of it.
    """
    if isinstance(node, Query):
        return _mentions_is_tag(node.root, tag)
    if isinstance(node, NaryOperatorNode):
        return any(_mentions_is_tag(child, tag) for child in node.operands)
    if isinstance(node, NotNode):
        return _mentions_is_tag(node.operand, tag)
    if isinstance(node, BinaryOperatorNode):
        lhs = node.lhs
        if isinstance(lhs, AttributeNode) and lhs.attribute_name == "card_is_tags":
            return isinstance(node.rhs, StringValueNode) and str(node.rhs.value).lower() == tag
    return False


# Scryfall's own wording, down to the typographic apostrophe, so a client that string-matches on
# `details` behaves the same.
#
# The URL is `/docs/reference`, not `/docs/syntax`. Scryfall moved it and this surface kept citing
# the old page in every no-match body, so a client following the link landed somewhere else than
# the one Scryfall sends it to. Re-measured 2026-08-16 on the no-match, the beyond-the-end and the
# random-miss bodies; all three cite `reference`.
_DOCS_REFERENCE = "https://scryfall.com/docs/reference"
_NO_MATCH_DETAILS = (
    "Your query didn’t match any cards. Adjust your search terms or refer to the syntax guide "  # noqa: RUF001
    f"at {_DOCS_REFERENCE}"
)
# Scryfall paginates past the end with a 422, not a 404 -- the query DID match, the page did not.
# Measured 2026-08-16: `e:khm` is two pages, and `page=3`, `page=007`, `page=9999` and a twenty-digit
# page all answer `422 validation_error` with this sentence, while a query that matched nothing
# answers the 404 above at every page. The backtick around `page` is Scryfall's.
_BEYOND_END_DETAILS = (
    "You have paginated beyond the end of these results, reduce your `page` parameter or refer to "
    f"the syntax guide at {_DOCS_REFERENCE}"
)
# The character after `didn` is U+2018, Scryfall's own and verified byte by byte -- it is not the
# ASCII apostrophe it looks like.
_EMPTY_QUERY_DETAILS = "You didn‘t enter anything to search for."  # noqa: RUF001
# Every term in the query was one Scryfall (and now this surface) cannot honor. See query_terms.py.
_ALL_IGNORED_DETAILS = "All of your terms were ignored."
# Scryfall's sentence for a query whose parentheses do not balance, in either direction.
_UNCLOSED_PARENS_DETAILS = "Your search contains unclosed parentheses."
# `/cards/random`'s own miss sentence, which names what it could not do rather than the query.
_RANDOM_NO_MATCH_DETAILS = "0 cards matched this search, a random card could not be returned."
# Scryfall's wording for `/cards/named` with neither parameter -- backticks and no full stop.
_NAMED_MISSING_PARAM_DETAILS = "You must provide a `fuzzy` or `exact` parameter"

# `format=csv` -- MEASURED against api.scryfall.com on 2026-08-16, not derived from the card object.
#
# This used to flatten ~60 card-object fields onto `image_uris_normal` / `prices_usd`-style columns,
# which is a reasonable-looking guess and is not what Scryfall exports. Scryfall's CSV is a SUMMARY:
# eighteen columns, a mixture of identifiers, printed values and prices, several of them named
# differently from the JSON keys they come from (`scryfall_id`, not `id`; `usd_price`, not
# `prices.usd`; a single `multiverse_id`, not the `multiverse_ids` array). The header row's bytes are
# the contract, so they are spelled out rather than built.
_CSV_COLUMNS = (
    "multiverse_id",
    "mtgo_id",
    "set",
    "collector_number",
    "lang",
    "rarity",
    "name",
    "mana_cost",
    "cmc",
    "type_line",
    "artist",
    "usd_price",
    "usd_foil_price",
    "eur_price",
    "tix_price",
    "image_uri",
    "scryfall_uri",
    "scryfall_id",
)

# The image size the CSV links to -- one row is one line and cannot carry a map of six.
_CSV_IMAGE_VERSION = "large"

# WITHOUT a charset, unlike every JSON response on this surface. Scryfall's exactly.
_CSV_CONTENT_TYPE = "text/csv"

# Scryfall names the download after the route, so a browser save-as lands on `search.csv`.
_CSV_CONTENT_DISPOSITION = 'attachment; filename="search.csv"'

# The header carrying the fact the CSV body cannot: is there another page? A JSON client reads
# `has_more` out of the envelope; a CSV client has no envelope, so Scryfall hangs the same boolean
# off a response header. Without it, paginating a CSV export means guessing.
_CSV_HAS_MORE_HEADER = "x-scryfall-has-more"


class _EngineMiss:
    """The engine could not serve this lookup, so the caller should try SQL.

    Distinct from None, which means the engine answered and there is no such card. Collapsing the
    two would let an unloaded store 404 a card that exists.
    """


_ENGINE_MISS = _EngineMiss()

# SQL fallback only: the blob subfields each external-id namespace maps to. The engine path uses its
# own index and never reads these.
_EXTERNAL_ID_COLUMNS: dict[str, tuple[str, ...]] = {
    "multiverse": ("multiverse_ids",),
    "mtgo": ("mtgo_id", "mtgo_foil_id"),
    "arena": ("arena_id",),
    "tcgplayer": ("tcgplayer_id", "tcgplayer_etched_id"),
    "cardmarket": ("cardmarket_id",),
}

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# The STRICTER shape a `/cards/collection` identifier's UUID must have: RFC 4122 VERSION 4.
#
# Not the same rule as `_UUID_RE` above, and the difference is measured rather than assumed
# (api.scryfall.com, 2026-08-16, one identifier per request):
#
#   00000000-0000-4000-8000-000000000000   200, in `not_found`   version 4, variant 8
#   3f2c8e5d-91b7-4a6e-9d12-4f5a9c7e8b01   200, in `not_found`   variant 9
#   00000000-0000-0000-0000-000000000000   400 bad_request       version 0
#   00000000-0000-0000-0000-000000000001   400 bad_request       version 0
#   00000000-0000-4000-0000-000000000000   400 bad_request       variant 0
#   3f2c8e5d-91b7-{0,1,5,6,7,8}a6e-bd12-.  400 bad_request       every other version nibble
#   3f2c8e5d-91b7-4a6e-cd12-4f5a9c7e8b01   400 bad_request       variant c
#
# So it is the SHAPE and not the all-zero value: a nil UUID wearing v4's version and variant nibbles
# is accepted and answered in `not_found`, and a v1 UUID is rejected. A syntactically valid but
# UNKNOWN v4 belongs in `not_found` and must not 400 -- that is what makes this a validation rule
# rather than a lookup one.
#
# `_UUID_RE` stays as it is: it decides which 404 sentence a `/cards/:id` miss gets, and Scryfall
# reads that path segment with the looser rule (`/cards/00000000-0000-0000-0000-000000000000` is a
# card miss, not a bad request). Two rules because Scryfall has two.
_COLLECTION_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")

# The collection identifier keys Scryfall validates as UUIDs, in the order it checks them -- the
# same order `_resolve_identifier` dispatches on, so the key REPORTED is the key that would have
# been used.
_COLLECTION_UUID_KEYS = ("id", "oracle_id", "illustration_id")

# How much of a rejected value Scryfall echoes back: 30 characters, then U+2026. Measured with the
# same requests above -- `not-a-uuid` comes back whole and every 36-character UUID comes back cut at
# exactly 30 with an ellipsis.
_COLLECTION_ECHO_LIMIT = 30

# The two identifier keys Scryfall validates as INTEGERS, in the order it checks them.
_COLLECTION_INTEGER_KEYS = ("mtgo_id", "multiverse_id")

# Identifier keys that identify a card ON THEIR OWN -- one of these makes an identifier valid.
_COLLECTION_SOLE_KEYS = ("id", "mtgo_id", "multiverse_id", "oracle_id", "illustration_id", "name")

# Every key that is part of SOME schema, in the order Scryfall lists them back in its complaint.
# `set` and `collector_number` are here and not above because neither identifies a card alone:
# together they are a printing, and `set` beside `name` scopes a name.
_COLLECTION_SCHEMA_KEYS = (*_COLLECTION_SOLE_KEYS, "set", "collector_number")

# The two batch-level sentences, verbatim from api.scryfall.com (2026-08-16). The count one names
# the bound rather than restating it and answers FOUR different mistakes, so it is written out once.
_COLLECTION_COUNT_DETAILS = "The `identifiers` list must have at least 1 and no more than 75 references."
_COLLECTION_NOT_AN_ARRAY_DETAILS = "The `identifiers` list must be a JSON array."

# Thresholds for the SQL fallback's typo-tolerant `?fuzzy=` stage, which scores with pg_trgm. A
# candidate must score at least the floor, and the best must lead the next distinct card name by at
# least the lead — closer than that and the query does not identify either card, so it is
# `ambiguous` rather than a guess. The floor sits deliberately above pg_trgm's default 0.3
# similarity_threshold, so the index-assisted `%` prefilter always admits a strict superset of what
# the floor keeps.
#
# THESE ARE NO LONGER THE ENGINE'S. The engine scores a different metric — Scryfall's, derived from
# 86 probed needles; see card_engine's `Fuzzy name matching` module comment — with its own fitted
# floor and lead, which it now supplies as the defaults of `fuzzy_card_by_name`. The two paths
# therefore resolve a handful of needles differently (`fuzzy=bolt lightning` is Blightning through
# the engine and Lightning Bolt through pg_trgm, and Scryfall says Blightning). That is deliberate:
# the engine is the path that serves, and matching Scryfall is what this surface is for.
FUZZY_SIMILARITY_FLOOR = 0.4
FUZZY_SIMILARITY_LEAD = 0.05

# A name column with every non-alphanumeric character removed, which is what the containment stage
# matches against (see `_fuzzy_containment_candidates`). NULL folds to '' so a row with no printed
# name simply carries nothing, rather than making the whole predicate NULL. Spelled once, because
# `api/db/2026-08-16-01-unseparated-name-search.sql` indexes this EXACT expression -- an expression
# index only serves a query that repeats it character for character.
_UNSEPARATED = "regexp_replace(lower(coalesce({column}, '')), '[^[:alnum:]]', '', 'g')"


def _unseparated(word: str) -> str:
    """Return `word` with every non-alphanumeric character removed -- `_UNSEPARATED`'s query side.

    Args:
        word: One word of the folded query.

    Returns:
        The word's alphanumeric characters, in order.
    """
    return "".join(char for char in word if char.isalnum())


# Returned by the similarity stage when two names are too close to choose between. A distinct
# object rather than a flag so the caller compares with `is` and cannot confuse it with a row.
_AMBIGUOUS: dict[str, Any] = {"ambiguous": True}

# How long a /cards/* answer may be reused, measured against api.scryfall.com rather than chosen:
# it sends `public, max-age=57600` on search, named, autocomplete and every by-id addressing, and
# the tier rides on its error responses too. These routes sent NO Cache-Control at all, so a CDN in
# front of this service cached none of them -- CachingMiddleware is an internal response cache and
# says nothing to anyone downstream.
#
# `/cards/random` keeps its stronger `no-store` (Scryfall sends `no-cache`): the draw must not be
# replayed by either layer, and no-store is the one that also defeats the internal cache.
_CARDS_CACHE_CONTROL = "public, max-age=57600"


def _set_cards_cache(falcon_response: falcon.Response | None) -> None:
    """Set the /cards/* cache tier.

    Local rather than `api_resource.set_cache_header`, which is the same line of code: these
    routes are a MIXIN ON `APIResource`, so `api_resource` imports this module and importing back
    is a circular import that fails at startup.

    Args:
        falcon_response: The response to write to, or None for an internal caller.
    """
    if falcon_response is not None:
        falcon_response.set_header("Cache-Control", _CARDS_CACHE_CONTROL)


# Hosts an absolute self-URL should address over plain HTTP. Everything else is assumed to be
# reached over TLS, which is what `next_page` has to say for a client to follow it.
_PLAINTEXT_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")  # noqa: S104

# Spelled out rather than falcon.MEDIA_JSON, which omits the charset Scryfall sends.
_JSON_CONTENT_TYPE = "application/json; charset=utf-8"


# Scryfall's three not-found bodies for these routes, measured against api.scryfall.com on
# 2026-08-12. They are worded by the SHAPE of the path rather than by the outcome, and none of them
# is the single string this used to answer with -- which carried a "Please double-check your URI and
# try again." tail Scryfall does not send, so the generic case was wrong as well as the specific
# ones.
#
#   /cards/<not-an-id>, /cards/<namespace>        the path addresses nothing
#   /cards/<id>, /cards/<ns>/<id>, /cards/<code>/<number>(/<lang>)
#                                                 a card miss: the address is well formed
#   the rulings variants                          the same, worded for the routes that take a
#                                                 multiverse id too
#
# `&` rather than `and`, and `multiverse ID` appearing only in the rulings one, are both Scryfall's.
_NOT_ADDRESSABLE_DETAILS = "The requested object or REST method was not found."
_CARD_MISS_DETAILS = "No card found with the given ID or set code and collector number."
_RULINGS_MISS_DETAILS = "No card found with the given ID, multiverse ID, or set code & collector number."


def _miss_details(identifier: str, number: str, suffix: str) -> str:
    """Pick the body a `/cards/...` miss answers with.

    Decided from the segments, not from what the lookup did, because that is how Scryfall words
    them: `/cards/nonsense` and `/cards/<a real id that matches nothing>` are both misses and get
    different sentences.

    `/cards/<x>/rulings` where x is not an id is the subtle one, and it is measured both ways:
    Scryfall reads it as a set code and a collector number that happens to be "rulings", so it
    answers the CARD miss rather than the rulings one.

    Args:
        identifier: First path segment.
        number: Second path segment.
        suffix: Third path segment.

    Returns:
        The `details` string for the 404.
    """
    if not number and not _is_uuid(identifier):
        return _NOT_ADDRESSABLE_DETAILS
    if (number == "rulings" and _is_uuid(identifier)) or suffix == "rulings":
        return _RULINGS_MISS_DETAILS
    return _CARD_MISS_DETAILS


def _is_uuid(value: str) -> bool:
    """Return whether a path segment is shaped like a UUID.

    Args:
        value: The segment to test.

    Returns:
        True when the segment is a canonical 8-4-4-4-12 UUID.
    """
    return bool(_UUID_RE.match(value))


def _echo_identifier_value(value: str) -> str:
    """Truncate a rejected identifier value the way Scryfall echoes it back.

    Args:
        value: The value as sent.

    Returns:
        The value, cut at 30 characters with an ellipsis when longer.
    """
    return f"{value[:_COLLECTION_ECHO_LIMIT]}…" if len(value) > _COLLECTION_ECHO_LIMIT else value


def _collection_schema_error(identifier: dict[str, Any]) -> dict[str, Any] | None:
    """Return the `Invalid identifier schema` error an identifier whose keys name no lookup earns.

    This used to accept ANY object and report it in `not_found`, which reads as harmless and is not:
    a client that sent `{"arena_id": 67330}` -- a plausible mistake, since `arena_id` is a real key
    on a card object and simply not a collection identifier -- was told the card does not exist
    rather than that the request was wrong, and would have gone looking in the wrong place.

    The valid schemas, measured one identifier per request on 2026-08-16: `id`, `mtgo_id`,
    `multiverse_id`, `oracle_id`, `illustration_id`, `name`, and the PAIR `set` + `collector_number`.
    `set` may also ride with `name` (the name-scoped-to-a-set lookup), which is why `name` alone is
    sufficient and `set` alone is not. Key ORDER does not matter (`{collector_number, set}` resolves)
    and unrecognized keys are IGNORED beside a valid schema (`{name, zzz}` resolves) -- `lang` among
    them, which is worth stating outright because it looks like it should work:
    `{set: "khm", collector_number: "40", lang: "ja"}` returns the ENGLISH card.

    The sentence's tail is the RECOGNIZED keys the identifier does carry, which is what makes the
    message useful: `{set}` and `{set, lang}` both say "set" -- you are halfway to a schema -- while
    `{}`, `{arena_id}` and `{nonsense}` all say nothing at all, because none of their keys is part of
    any schema. Every one of those five is a measured string.

    Args:
        identifier: One entry of the request's `identifiers` array.

    Returns:
        A Scryfall error object, or None when the identifier names a lookup.
    """
    if any(identifier.get(key) is not None for key in _COLLECTION_SOLE_KEYS):
        return None
    if identifier.get("set") is not None and identifier.get("collector_number") is not None:
        return None
    present = ", ".join(key for key in _COLLECTION_SCHEMA_KEYS if identifier.get(key) is not None)
    return bad_request_error(f"Invalid identifier schema: {present}")


def _collection_identifier_error(identifiers: list[Any]) -> dict[str, Any] | None:
    """Return the `bad_request` a malformed collection identifier earns, or None when all are well formed.

    ONE error for the whole request, from the FIRST malformed identifier -- measured: a batch of a
    real id followed by the nil UUID 400s and reports the nil one, and the same batch reversed 400s
    and reports it too, so nothing is resolved and no partial List comes back.

    The UUID article is always "An" because all three of those keys start with a vowel, and the
    integer one is always "A" because both of those start with a consonant: Scryfall picks it per
    field rather than per sentence.

    A non-dict entry does not reach here. Scryfall answers `null` or a bare string in the list with
    the COUNT message, as if the list were empty, so the list's SHAPE is validated before any
    identifier's -- see the handler.

    Args:
        identifiers: The request's `identifiers` array, list shape already validated.

    Returns:
        A Scryfall error object, or None.
    """
    for identifier in identifiers:
        schema = _collection_schema_error(identifier)
        if schema is not None:
            return schema
        for key in _COLLECTION_UUID_KEYS:
            if key not in identifier:
                continue
            value = str(identifier[key])
            if _COLLECTION_UUID_RE.match(value):
                break
            return bad_request_error(f"An `{key}` identifier must be a valid UUID: {_echo_identifier_value(value)}")
        for key in _COLLECTION_INTEGER_KEYS:
            if key not in identifier:
                continue
            raw = identifier[key]
            # Scryfall accepts the integer and the string that spells one; anything else earns the
            # integer complaint.
            if isinstance(raw, bool):
                pass
            elif isinstance(raw, int) or str(raw).strip().lstrip("+-").isdigit():
                break
            return bad_request_error(f"A `{key}` identifier must be an integer: {_echo_identifier_value(str(raw))}")
    return None


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    """Parse a Scryfall boolean query parameter.

    Args:
        value: The raw parameter value, or None when absent.
        default: What an absent parameter means.

    Returns:
        The parsed flag; anything other than a recognized true spelling is False.
    """
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: str | None) -> int | None:
    """Parse an integer query parameter or path segment.

    Args:
        value: The raw value, or None when absent.

    Returns:
        The integer, or None when absent or unparseable.
    """
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def _scryfall_page(value: str) -> int:
    """Parse Scryfall's `page`, which never rejects anything.

    Measured one request per row (2026-08-16, `q=e:khm`, a two-page result)::

        page=0  page=-3  page=-0  page=abc  page=  page=0x2  page=1e2   all serve PAGE 1
        page=2.5  page=1.9  page=+2  page=" 2"  page=2abc                truncate at the first
                                                                         non-digit
        page=007                                                         is 7, not octal -- a THIRD
                                                                         page here, so 422

    That is Ruby's `String#to_i` (leading space, optional sign, digits, stop) followed by a clamp to
    1 -- not integer validation. This surface answered `400 "The page parameter must be a positive
    integer."` to the first row above, which is a sentence Scryfall does not own and a rejection it
    does not make. The only 4xx `page` earns is the 422 for a page past the end, and that one needs
    the result count, so it is decided where the count is (`_empty_page_error`).

    Args:
        value: The raw `page` parameter.

    Returns:
        The 1-based page number.
    """
    digits = re.match(r"\s*[-+]?\d*", value or "")
    try:
        page = int(digits.group(0)) if digits else 0
    except ValueError:
        page = 0
    return max(page, 1)


def _empty_page_error(total_cards: int, page_offset: int) -> dict[str, Any]:
    """The error for a page that came back with no rows -- which is TWO different errors.

    Scryfall separates "your query matched nothing" from "your query matched, but not this far in":
    a query with no results is `404 not_found` at every page, and a page past the end of a result
    that DOES have rows is `422 validation_error` (measured 2026-08-16: `e:khm` is two pages, and
    `page=3` is a 422 while `e:notaset` is a 404 at `page=1` and at `page=3` alike). This surface
    answered 404 to both, which told a paginating client its query had stopped matching.

    Neither body carries `warnings`, even for a query whose terms were ignored -- also measured.

    Args:
        total_cards: How many cards the query matched in total.
        page_offset: The 0-based offset of the page that came back empty.

    Returns:
        The Scryfall error object.
    """
    if total_cards > 0 and page_offset >= total_cards:
        return error_object(code="validation_error", status=422, details=_BEYOND_END_DETAILS)
    return not_found_error(_NO_MATCH_DETAILS)


def _self_base_url(request: falcon.Request | None, request_host: str, path: str) -> str:
    """Build the absolute URL of a route on this host.

    A `next_page` a client cannot follow is worse than no pagination at all, so the scheme is the
    request's own as corrected by `Forwarded` / `X-Forwarded-Proto` — the only signal that knows
    about a TLS-terminating proxy, behind which the request itself arrives as plain `http`. A
    deployment that terminates TLS in front of this service must send one of those headers, as it
    must already for `X-Proxy-Host` to give the right host.

    Guessing `https` from the host name instead was tried and is worse: it silently breaks any
    plain-HTTP deployment on a real hostname, which is a configuration this project supports,
    to paper over one that is misconfigured. The host only decides when there is no request to
    read, which is an internal caller rather than a served request.

    Args:
        request: The request being answered, when the handler has one.
        request_host: Host the request arrived on.
        path: Absolute route path, leading slash included.

    Returns:
        The absolute URL, with no query string.
    """
    host = request_host or "api.scryfall.com"
    if request is not None:
        return f"{request.forwarded_scheme}://{host}{path}"
    scheme = "http" if host.split(":")[0] in _PLAINTEXT_HOSTS else "https"
    return f"{scheme}://{host}{path}"


def _csv_cell(value: str | None) -> str:
    """Render one CSV cell, RFC 4180 with the MINIMAL quoting Scryfall emits.

    Verified on real rows: `Alrund, God of the Cosmos // Hakka, Whispering Raven` is quoted (commas),
    `Henzie ""Toolbox"" Torre` is quoted with its own quotes doubled, `Edward P. Beard, Jr.` is
    quoted, `Legendary Creature — God // Legendary Creature — Bird` is NOT (the em dash and the
    slashes are ordinary bytes), and `Volkan Baǵa` is not either -- non-ASCII is raw UTF-8.

    None and the empty string are DIFFERENT cells, which is the rule a naive writer gets wrong: an
    ABSENT value writes nothing at all (a null price, a printing with no multiverse id) while a value
    that IS the empty string writes `""`. Every basic land is the proof -- Scryfall's JSON gives it
    `"mana_cost": ""` and the CSV row reads `A-Bretagard Stronghold,"",0.0,Land`, two bytes where the
    price columns beside it have none.

    Args:
        value: The cell's text, or None when the card does not carry one.

    Returns:
        The cell as it appears in the document.
    """
    if value is None:
        return ""
    if value == "":
        return '""'
    if not any(char in value for char in ',"\r\n'):
        return value
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _csv_decimal(value: float | None) -> str | None:
    """Render a decimal cell the way the CSV writes one: `60` is `60.0`, `0.5` stays `0.5`.

    Args:
        value: The number, or None.

    Returns:
        The cell text, or None when there is no value.
    """
    if value is None:
        return None
    return f"{value:.1f}" if float(value).is_integer() else str(value)


def _csv_price(value: object) -> str | None:
    """Render a price cell: the JSON string parsed as a float and printed back.

    The JSON carries two decimal places always (`"60.00"`, `"0.10"`), the CSV does not (`60.0`,
    `0.1`), so this is a float round-trip rather than the string. A null price is the EMPTY cell,
    never `0`.

    Args:
        value: The price as the card object carries it.

    Returns:
        The cell text, or None when the card has no price.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return _csv_decimal(float(value))
    except ValueError:
        return None


def _csv_mana_cost(card: dict[str, Any]) -> str | None:
    """The printed cost, joined across faces when the card object keeps it there.

    A one-image multi-face layout (split, flip, adventure) carries `mana_cost` at top level already
    joined (`{1}{R} // {1}{U}`), so that value is used as it stands. A two-image layout (transform,
    modal_dfc) has no top-level cost and each face carries its own -- and there the join DROPS THE
    EMPTY ONES rather than leaving a separator with nothing after it. Measured 2026-08-16::

        Delver of Secrets // Insectile Aberration   faces {U} + ""       cell `{U}`
        Boggart Trawler // Boggart Bog             faces {2}{B} + ""    cell `{2}{B}`
        Barkchannel Pathway // Tidechannel Pathway faces "" + ""        cell `""`

    Args:
        card: A Scryfall card object.

    Returns:
        The cell text, or None when the card carries no printed cost at all.
    """
    top = card.get("mana_cost")
    if isinstance(top, str):
        return top
    faces = card.get("card_faces") or []
    if not faces:
        return None
    return " // ".join(cost for face in faces if (cost := face.get("mana_cost")))


def _csv_image(card: dict[str, Any]) -> str | None:
    """The `large` image, front face.

    Top-level `image_uris` on every layout that has one; a two-image layout has none, and its FRONT
    face's map is the one the CSV links to.

    Args:
        card: A Scryfall card object.

    Returns:
        The image URL, or None when the card has no image map.
    """
    top = card.get("image_uris") or {}
    if isinstance(top, dict) and top.get(_CSV_IMAGE_VERSION):
        return top[_CSV_IMAGE_VERSION]
    faces = card.get("card_faces") or []
    front = faces[0].get("image_uris") if faces else None
    return front.get(_CSV_IMAGE_VERSION) if isinstance(front, dict) else None


def _csv_row(card: dict[str, Any]) -> str:
    """One card as its CSV row, without the trailing newline.

    Args:
        card: A Scryfall card object.

    Returns:
        The rendered row.
    """
    multiverse_ids = card.get("multiverse_ids") or []
    prices = card.get("prices") or {}
    set_code = card.get("set")
    rarity = card.get("rarity")
    # `scryfall_uri` WITHOUT the tracking query the JSON one carries. Cut at the first `?` rather
    # than parsed: the slug is percent-encoded and may hold anything else, but a scryfall.com card
    # URL has never carried a query of its own.
    scryfall_uri = card.get("scryfall_uri")
    values = (
        str(multiverse_ids[0]) if multiverse_ids else None,
        str(card["mtgo_id"]) if card.get("mtgo_id") is not None else None,
        set_code.upper() if isinstance(set_code, str) else None,
        card.get("collector_number"),
        card.get("lang"),
        # Rarity as its INITIAL, uppercased: common `C`, uncommon `U`, rare `R`, mythic `M`, special
        # `S`, bonus `B`. Derived rather than tabled, so a rarity added later abbreviates the same
        # way instead of silently emitting nothing.
        rarity[0].upper() if isinstance(rarity, str) and rarity else None,
        card.get("name"),
        _csv_mana_cost(card),
        _csv_decimal(card.get("cmc")),
        card.get("type_line"),
        card.get("artist"),
        _csv_price(prices.get("usd")),
        _csv_price(prices.get("usd_foil")),
        _csv_price(prices.get("eur")),
        _csv_price(prices.get("tix")),
        _csv_image(card),
        scryfall_uri.split("?")[0] if isinstance(scryfall_uri, str) else None,
        card.get("id"),
    )
    return ",".join(_csv_cell(value) for value in values)


def _cards_to_csv(cards: Sequence[dict[str, Any]]) -> str:
    """Render a page of cards as Scryfall's CSV document.

    LF line endings and a trailing newline, both measured. The header row is emitted even when the
    page is short; it is never emitted for an empty result, because an empty result is a 404 decided
    before the format is consulted.

    Args:
        cards: The card objects to render.

    Returns:
        The CSV document, header row included.
    """
    lines = [",".join(_CSV_COLUMNS), *(_csv_row(card) for card in cards)]
    return "\n".join(lines) + "\n"


class ScryfallCardsRoutes:
    """The `/cards/*` routes, mixed into `APIResource`.

    Every handler returns the value that becomes the response body, or None after writing the
    response itself (the text and CSV formats, which are not JSON). Errors are returned as
    Scryfall error objects with the matching status rather than raised, so the generic Falcon
    error serializer never sees them.
    """

    # ---------------------------------------------------------------- response plumbing

    def _scryfall_respond(
        self,
        falcon_response: falcon.Response | None,
        payload: dict[str, Any],
        *,
        pretty: bool = False,
    ) -> dict[str, Any] | None:
        """Write a JSON payload, honoring the error status it carries and `pretty`.

        Args:
            falcon_response: The response to write to.
            payload: The Scryfall object to serialize.
            pretty: Whether to emit indented JSON.

        Returns:
            The payload when the caller should let the framework serialize it, or None when this
            method already wrote the body.
        """
        if falcon_response is not None:
            falcon_response.content_type = _JSON_CONTENT_TYPE
            status = payload.get("status") if payload.get("object") == "error" else None
            if isinstance(status, int):
                falcon_response.status = falcon.util.code_to_http_status(status)
            if pretty:
                falcon_response.text = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode()
                return None
        return payload

    def _respond_text(self, falcon_response: falcon.Response | None, body: str, content_type: str) -> None:
        """Write a non-JSON body.

        Args:
            falcon_response: The response to write to.
            body: The rendered document.
            content_type: Its media type.
        """
        if falcon_response is not None:
            falcon_response.content_type = content_type
            falcon_response.text = body

    def _render_card(  # noqa: PLR0913
        self,
        card: dict[str, Any],
        *,
        falcon_response: falcon.Response | None,
        card_format: str,
        face: str,
        version: str,
        pretty: bool,
    ) -> dict[str, Any] | None:
        """Emit one card in the requested format.

        Args:
            card: The Scryfall card object.
            falcon_response: The response to write to.
            card_format: "json", "text" or "image".
            face: "front" or "back".
            version: One of IMAGE_VERSIONS.
            pretty: Whether JSON output is indented.

        Returns:
            The payload to serialize, or None when the body was written here.

        Raises:
            falcon.HTTPFound: For `format=image`, redirecting to the image itself.
        """
        if card_format == "text":
            self._respond_text(falcon_response, card_to_text(card), "text/plain; charset=utf-8")
            return None
        if card_format == "image":
            location = objects.image_uri(card, version=version, face=face)
            if not location:
                return self._scryfall_respond(
                    falcon_response,
                    not_found_error("No image is available for this card in that version."),
                    pretty=pretty,
                )
            raise falcon.HTTPFound(location)
        return self._scryfall_respond(falcon_response, card, pretty=pretty)

    # ---------------------------------------------------------------- card lookups

    def _run_uncached(self, *, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a query that must not be memoized, and return its rows.

        `_run_query` keys its cache on the SQL text and the bound parameters, which is right for
        every lookup here except the random draw: that one is deliberately non-deterministic for a
        fixed query and parameter set, so caching it would replay one card forever.

        `maybe_json` IS THE OTHER HALF OF `_run_query` THIS HAS TO KEEP, and it was missing:
        `generate_sql_query` emits a plain dict for a `card_is_tags @> …` containment test, and
        psycopg refuses to adapt a bare dict ("cannot adapt type 'dict' using placeholder '%s'").
        The count above goes through `_run_query`, which wraps every parameter, so the failure
        was on the DRAW alone — `/cards/random?q=is:reserved` was already a 500 before this route
        gained an extras gate, and every gated draw would carry the same shape.

        Args:
            query: The SQL to run.
            params: Bound parameters.

        Returns:
            The result rows.
        """
        params = {k: db_utils.maybe_json(v) for k, v in params.items()}
        with self.app_context.reader_pool.connection() as conn, conn.cursor() as cursor:
            db_utils.set_statement_timeout(cursor, 10_000)
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def _engine_for_lookup(self) -> object | None:
        """The engine when it can answer, or None when the caller must fall back to SQL.

        Mirrors the three branches `search()` already uses: feature-gated off, store not loaded, or
        ready. SQL is the fallback for when the engine cannot serve, not a peer path -- every route
        below asks here first and only reaches Postgres if this returns None or the engine raises.
        """
        if not settings.enable_engine:
            return None
        try:
            if self.app_context.engine.size() == 0:
                self._trigger_background_reload_if_needed()
                return None
        # An engine that cannot report its size cannot serve, whatever the reason.
        except Exception:  # noqa: BLE001
            return None
        return self.app_context.engine

    def _sets_with_extras(self) -> frozenset[str]:
        """The set codes holding at least one `is:extra` printing, lowercased.

        Scryfall's `include_extras` auto-enable table -- see `_extras_triggers` for what asks it.
        Folded into the archive at build, so this is a read rather than a query; an engine that
        cannot answer gives an empty table, which simply leaves the auto-enable off.

        Returns:
            The set codes, or an empty set when the engine cannot answer.
        """
        engine = self._engine_for_lookup()
        if engine is None:
            return frozenset()
        try:
            return frozenset(code.lower() for code in engine.sets_with_extras())
        # An engine that cannot answer leaves the auto-enable off; it never 500s the search.
        except Exception:
            logger.exception("Engine sets_with_extras failed, leaving include_extras as asked")
            return frozenset()

    def _engine_card(self, fetch: Callable[[Any], dict[str, Any] | None]) -> dict[str, Any] | _EngineMiss | None:
        """Run one engine lookup, or report that the engine could not serve it.

        Returns the card, None for a genuine "no such card", or _ENGINE_MISS when the caller should
        try SQL. Separating the last from the first matters: a store that is not loaded must not
        answer 404 for a card that exists.
        """
        engine = self._engine_for_lookup()
        if engine is None:
            return _ENGINE_MISS
        try:
            row = fetch(engine)
        # Any engine failure is a fallback, never a 500.
        except Exception:
            logger.exception("Engine lookup failed, falling back to SQL")
            return _ENGINE_MISS
        return to_scryfall_card(row) if row else None

    def _card_by_scryfall_id(self, scryfall_id: str) -> dict[str, Any] | None:
        """One card by Scryfall id, from the store when it can answer."""
        found = self._engine_card(lambda e: e.card_by_scryfall_id(str(scryfall_id), list(CARD_OBJECT_FIELDS)))
        if found is not _ENGINE_MISS:
            return found
        return self._fetch_one_card("scryfall_id = %(value)s", {"value": str(scryfall_id)})

    def _card_by_oracle_id(self, oracle_id: str) -> dict[str, Any] | None:
        """The representative printing of one oracle card, from the store when it can answer."""
        engine = self._engine_for_lookup()
        if engine is not None:
            try:
                rows = engine.printings_of_oracle_id(str(oracle_id), list(CARD_OBJECT_FIELDS))
                # Printings are stored in descending default-prefer order, so the first is the
                # representative printing every other by-name path shows.
                return to_scryfall_card(rows[0]) if rows else None
            except Exception:
                logger.exception("Engine oracle-id lookup failed, falling back to SQL")
        return self._fetch_one_card("oracle_id = %(value)s", {"value": str(oracle_id)})

    def _card_by_external_id(self, namespace: str, external_id: int | None) -> dict[str, Any] | None:
        """One card by a marketplace or client id, from the store when it can answer."""
        if external_id is None:
            return None
        found = self._engine_card(
            lambda e: e.card_by_external_id(namespace, int(external_id), list(CARD_OBJECT_FIELDS)),
        )
        if found is not _ENGINE_MISS:
            return found
        columns = _EXTERNAL_ID_COLUMNS.get(namespace, ())
        if not columns:
            return None
        clauses = " OR ".join(f"(raw_card_blob ->> '{column}')::bigint = %(value)s" for column in columns)
        return self._fetch_one_card(f"({clauses})", {"value": external_id})

    def _fetch_one_card(self, where: str, params: dict[str, Any], *, rank_first: str = "") -> dict[str, Any] | None:
        """Fetch the single best printing matching a predicate.

        Ties are broken by prefer_score, so a lookup that spans printings (by name, by oracle id)
        returns the same representative printing the rest of the API would pick.

        Args:
            where: SQL predicate, referencing `card` as the table alias.
            params: Bound parameters for the predicate.
            rank_first: An ORDER BY term applied BEFORE prefer_score, for a caller whose predicate
                admits matches of different qualities. `named?exact=` needs it: it matches either
                face of a "Front // Back" name, and without this a two-faced card whose back face
                carries the name outranks the card actually named that whenever its score is
                higher.

        Returns:
            The card, or None when nothing matched.
        """
        rows = self._run_query(
            query=(
                f"SELECT {_CARD_COLUMNS} FROM magic.cards AS card WHERE {where} "
                f"ORDER BY {rank_first}prefer_score DESC NULLS LAST, released_at DESC LIMIT 1"
            ),
            params=params,
            explain=False,
        )["result"]
        return to_scryfall_card(sql_row_to_engine_row(rows[0])) if rows else None

    def _engine_exact_name(self, folded: str, set_code: str | None) -> dict[str, Any] | None:
        """The exact-name match from the engine, or None when it cannot answer.

        Args:
            folded: The accent-folded, lowercased name.
            set_code: Restrict to this set, or None for any.

        Returns:
            `{"scryfall_id", "card_name"}` for the best match, or None to fall back to SQL.
        """
        engine = self._engine_for_lookup()
        if engine is None:
            return None
        try:
            row = engine.exact_card_by_name(folded, set_code, list(CARD_OBJECT_FIELDS))
        # Any engine failure falls back to SQL; it never 500s.
        except Exception:
            logger.exception("Engine exact name match failed, falling back to SQL")
            return None
        if row is None:
            return None
        # Outside the except ON PURPOSE: a missing key is a SHAPE mismatch between this call and
        # CARD_OBJECT_FIELDS, not an engine that cannot answer, and swallowing it would turn the
        # fast path into a permanent silent fallback. See _fuzzy_similarity_candidate, where
        # exactly that happened.
        return {"scryfall_id": row["scryfall_id"], "card_name": row["name"]}

    def _card_by_illustration_id(self, illustration_id: str) -> dict[str, Any] | None:
        """Return the best printing carrying an illustration id.

        The ENGINE first, like every other identifier `/cards/collection` accepts. This was the one
        left on SQL, and it is a scan there too — `illustration_id` has no index on the table, where
        the engine answers it from a sorted permutation in O(log n).

        Args:
            illustration_id: The illustration UUID.

        Returns:
            The matching printing, or None.
        """
        engine = self._engine_for_lookup()
        if engine is not None:
            try:
                row = engine.card_by_illustration_id(illustration_id, list(CARD_OBJECT_FIELDS))
            # Any engine failure falls back to SQL; it never 500s.
            except Exception:
                logger.exception("Engine illustration lookup failed, falling back to SQL")
            else:
                if row is None:
                    return None
                return self._fetch_one_card("scryfall_id = %(value)s", {"value": str(row["scryfall_id"])})

        return self._fetch_one_card("illustration_id = %(value)s", {"value": illustration_id})

    def _cards_by_ids(self, scryfall_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Fetch cards by scryfall id, preserving the order of the ids given.

        Args:
            scryfall_ids: The ids to fetch.

        Returns:
            The cards, in `scryfall_ids` order; ids that matched nothing are skipped.
        """
        if not scryfall_ids:
            return []
        engine = self._engine_for_lookup()
        if engine is not None:
            try:
                by_id_engine = {}
                for card_id in scryfall_ids:
                    row = engine.card_by_scryfall_id(str(card_id), list(CARD_OBJECT_FIELDS))
                    if row:
                        by_id_engine[str(card_id)] = to_scryfall_card(row)
                return [by_id_engine[i] for i in scryfall_ids if i in by_id_engine]
            # Hydration failure falls back; it does not 500.
            except Exception:
                logger.exception("Engine hydration failed, falling back to SQL")
        rows = self._run_query(
            # A comma-joined string rather than a list: _run_query passes list parameters through
            # maybe_json(), which binds them as jsonb, and jsonb does not cast to uuid[].
            query=(
                f"SELECT {_CARD_COLUMNS} FROM magic.cards AS card WHERE scryfall_id = ANY(string_to_array(%(ids)s, ',')::uuid[])"
            ),
            params={"ids": ",".join(scryfall_ids)},
            explain=False,
        )["result"]
        by_id = {str(row["scryfall_id"]): to_scryfall_card(sql_row_to_engine_row(row)) for row in rows}
        return [by_id[card_id] for card_id in scryfall_ids if card_id in by_id]

    # ---------------------------------------------------------------- GET /cards/search

    @route(paths=("cards/search",))
    def scryfall_cards_search(  # noqa: PLR0913
        self,
        *,
        falcon_response: falcon.Response | None = None,
        request: falcon.Request | None = None,
        request_host: str = "",
        q: str | None = None,
        unique: str = "cards",
        order: str = "name",
        dir: str = "auto",  # noqa: A002  -- Scryfall's parameter name
        page: str = "1",
        format: str = "json",  # noqa: A002  -- Scryfall's parameter name
        pretty: str = "false",
        include_extras: str = "false",
        include_multilingual: str = "false",
        include_variations: str = "false",
        **_: object,
    ) -> dict[str, Any] | None:
        """Search for cards, paginated 175 at a time.

        `include_multilingual` is honored: the default is Scryfall's -- English (canonical)
        printings only -- and `include_multilingual=true` widens the search to foreign printings,
        as does a `lang:` term in the query itself.

        `include_extras` is honored too, and it is the one parameter this route can OVERRULE. The
        default hides the extras class (`-is:extra`, applied to the tree in `_search`); a query
        whose parse tree syntactically carries a trigger term turns it on regardless of what the
        caller sent, in the results and in the `next_page` echo alike. See `_extras_triggers`.

        `include_variations` is honored on the same terms, with a DIFFERENT auto-enable: the
        default hides the printings Scryfall marks `variation` (`-is:variation`, applied in
        `_search`), and the only term that forces it on is the caller's own `is:variation`. No
        set term enables it, and nothing that enables extras does either -- the two gates are
        independent, and a query may cross both.

        In-query directives (`unique:`, `order:`/`sort:`, `dir:`/`direction:`, `prefer:`) reach
        the search through `_search`'s own fold and override the query parameter of the same
        meaning, and `next_page` echoes the values that were SERVED rather than the ones that were
        sent -- so a client following the link verbatim pages the same result set.

        Args:
            falcon_response: The Falcon response to write to.
            request: The Falcon request, read for the scheme `next_page` should use.
            request_host: Host the request arrived on, used to build `next_page`.
            q: The search query.
            unique: Rollup mode -- cards, art or prints.
            order: Sort key.
            dir: Sort direction -- auto, asc or desc.
            page: 1-based page number.
            format: Response format -- json or csv.
            pretty: Whether to indent JSON output.
            include_extras: Whether to include the extras class; a trigger term in `q` forces it.
            include_multilingual: Whether to widen the search to foreign printings.
            include_variations: Whether to include the variation class; `is:variation` in `q`
                forces it.

        Returns:
            A List object of cards, or a Scryfall error object.
        """
        is_pretty = _as_bool(pretty)
        # Before the handler runs, so the tier rides on the 400s raised inside it too -- which is
        # what api.scryfall.com does (an empty-query 400 comes back with the route's own max-age).
        _set_cards_cache(falcon_response)
        if not q or not q.strip():
            return self._scryfall_respond(
                falcon_response,
                bad_request_error(_EMPTY_QUERY_DETAILS, warnings=None),
                pretty=is_pretty,
            )

        page_number = _scryfall_page(page)

        # SCRYFALL'S IGNORE-AND-CONTINUE POLICY, applied to the raw query before anything reads it:
        # the terms this API cannot honor leave the query carrying a warning, and only a query with
        # NOTHING left is a bad request. See query_terms.py for the measurements behind every rule.
        policy = scryfall_term_policy(q)
        if policy.unclosed_parens:
            return self._scryfall_respond(
                falcon_response,
                bad_request_error(_UNCLOSED_PARENS_DETAILS, warnings=None),
                pretty=is_pretty,
            )
        if policy.all_ignored:
            return self._scryfall_respond(
                falcon_response,
                bad_request_error(_ALL_IGNORED_DETAILS, warnings=policy.warnings),
                pretty=is_pretty,
            )
        warnings: list[str] = list(policy.warnings)

        # An unrecognized `unique` is Scryfall's default, SILENTLY: `unique=printing`,
        # `unique=card`, `unique=printings`, `unique=artwork` and `unique=bogus` all come back as
        # the plain unique-by-card answer with no `warnings` key at all (measured 2026-08-16). This
        # surface warned on all five -- and four of them are its own vocabulary, which the in-query
        # `unique:` directive accepts, so the warning announced an inconsistency, not a problem.
        unique_on = _UNIQUE_MAP.get(unique.lower(), UniqueOn.CARD)

        orderby = _ORDER_MAP.get(order.lower())
        if orderby is None:
            if order.lower() in _SCRYFALL_ONLY_ORDERS:
                warnings.append(f"This server cannot sort by {order!r} yet; sorted by name instead.")
            else:
                warnings.append(f"Unrecognized order {order!r}; sorted by name instead.")
            orderby = CardOrdering.NAME

        # An unrecognized direction falls back to AUTO, which is also the parameter's default --
        # Scryfall ignores one it does not know rather than erroring.
        direction = _DIRECTION_MAP.get(dir.lower(), SortDirection.AUTO)

        # THE PARSE THIS ROUTE OWNS, and the only one it needs: `include_extras`'s auto-enable and
        # the `next_page` echo are both properties of the parse TREE, and `_search` takes a string.
        # A parse failure here is the same 400 `_search` would have raised, spelled as a Scryfall
        # error object rather than a Falcon one.
        try:
            parsed = parse_scryfall_query(policy.query)
        except ValueError:
            return self._scryfall_respond(
                falcon_response,
                bad_request_error(f'Failed to parse query: "{q}"', warnings=warnings),
                pretty=is_pretty,
            )

        # THE VALUES THAT WILL BE SERVED. `_search` folds the same directives over the same
        # parameters with the same helper -- deliberately not a second implementation -- so
        # re-folding here is idempotent and exists only because the ECHO needs the answer before
        # the search runs. The fold's warnings are dropped on this side and taken from the search
        # result below, so they are reported exactly once.
        #
        # PRECEDENCE IS MEASURED (api.scryfall.com, 2026-08-16): the DIRECTIVE WINS over the query
        # parameter, in both directions and for every directive -- `q=… unique:prints&unique=cards`
        # answers 387 (prints) and `q=… unique:cards&unique=prints` answers 285 (cards);
        # `q=… order:cmc&order=name` sorts by cmc; `q=… dir:desc&dir=asc` sorts descending. That is
        # `_fold_directives`' documented rule, which is why this is the shared implementation
        # rather than a second one that could drift from `/search`.
        unique_on, orderby, direction, prefer = _fold_directives_for_echo(
            parsed,
            unique=unique_on,
            orderby=orderby,
            direction=direction,
            prefer=PreferOrder.DEFAULT,
        )

        # SCRYFALL FORCES `include_extras`, it does not merely default it: a parse tree carrying a
        # trigger term overrides an explicit `include_extras=false`, in the rows AND in the echo.
        # See `_extras_triggers` for the rule and the measurements behind it.
        triggers = _extras_triggers(parsed)
        forced = triggers.forced
        if not forced and triggers.sets:
            # The one CONDITIONAL trigger, and the only reason this route asks the engine anything
            # before searching: a set term enables extras iff that set holds one. Asked only when
            # the query actually named a set, so an ordinary page never pays for the table.
            forced = not self._sets_with_extras().isdisjoint(triggers.sets)
        effective_extras = forced or _as_bool(include_extras)

        # AND THE SAME STORY FOR `include_variations`, WITH A DIFFERENT TRIGGER RULE -- which is
        # why this is its own walk and not a second reading of `triggers`. Every unconditional
        # EXTRAS trigger was probed and echoes `include_variations=false`: `a:"rebecca guay"
        # t:creature` and `layout:normal cmc=3` echo `extras=true variations=false`, and so do
        # `wm:`, `name:/^z/`, `t:token`, `is:extra`, `is:oversized`, `is:reserved` and
        # `is:rebalanced`. A SET term does not enable it either -- `e:hho` is 21 bare and 23 only
        # once the parameter is sent, though hho auto-enables extras -- so there is no conditional
        # arm here and nothing to ask the engine. The only trigger is the caller's own
        # `is:variation`, and it is a FORCE like the extras ones: `t:creature or is:variation`
        # sent with `include_variations=false` answers 51,566 and echoes true.
        from api.api_resource import VARIATION_IS_TAG  # noqa: PLC0415

        effective_variations = _mentions_is_tag(parsed, VARIATION_IS_TAG) or _as_bool(include_variations)

        try:
            result = self._search(
                query=policy.query,
                orderby=orderby,
                direction=direction,
                fields=["scryfall_id"],
                limit=PAGE_SIZE,
                offset=(page_number - 1) * PAGE_SIZE,
                unique=unique_on,
                prefer=prefer,
                # RESOLVED, never left to a default downstream: false IS Scryfall's default and the
                # auto-enable above is what can override it. Fixing both on the way in keeps
                # "absent" from meaning anything.
                include_extras=effective_extras,
                include_multilingual=_as_bool(include_multilingual),
                include_variations=effective_variations,
            )
        except falcon.HTTPBadRequest as err:
            return self._scryfall_respond(
                falcon_response,
                bad_request_error(str(err.description or "The query could not be parsed."), warnings=warnings),
                pretty=is_pretty,
            )

        warnings.extend(result.get("warnings") or [])
        total_cards = result["total_cards"]
        cards = self._cards_by_ids([str(row["scryfall_id"]) for row in result["cards"]])
        if not cards:
            return self._scryfall_respond(
                falcon_response,
                _empty_page_error(total_cards, (page_number - 1) * PAGE_SIZE),
                pretty=is_pretty,
            )

        has_more = (page_number - 1) * PAGE_SIZE + len(cards) < total_cards
        next_page = None
        if has_more:
            next_page = objects.build_page_url(
                _self_base_url(request, request_host, "/cards/search"),
                {
                    # RESOLVED like the rest: `q=… dir:desc` with no `dir` parameter must echo
                    # `dir=desc`, or page 2 sorts the other way from the page that linked to it.
                    "dir": str(direction),
                    "format": format,
                    # THE RESOLVED VALUE, not the parameter as sent -- the same rule `order` and
                    # `unique` follow below. `q=e:lea&include_extras=false` echoes
                    # `include_extras=true` on api.scryfall.com AND returns the extras; echoing
                    # `false` while serving with them on gives a client a link that contradicts
                    # the page it came from. Measured 2026-08-16 over 57 set probes plus the
                    # unconditional families: the echo agreed with what was served in every one.
                    "include_extras": str(effective_extras).lower(),
                    "include_multilingual": str(_as_bool(include_multilingual)).lower(),
                    "include_variations": str(effective_variations).lower(),
                    # RESOLVED, not raw -- see _UNIQUE_ECHO. This is what keeps the link correct
                    # now that the in-query directives (#893) fold here: `q` echoes verbatim,
                    # directive included, so a `q` saying `order:cmc` next to an `order=name` in
                    # the same URL would page a different result set on page 2 than on page 1.
                    "order": _echo_order(orderby, order),
                    "q": _echo_query(q),
                    "unique": _UNIQUE_ECHO[unique_on],
                },
                page_number + 1,
            )

        # EXACTLY `csv`, lowercase: `format=CSV` serves JSON on api.scryfall.com (measured
        # 2026-08-16), so this comparison is deliberately not case-folded -- the mirror image of the
        # single-card routes, which honour `text`/`image` and ignore `csv`. A `format` a route does
        # not implement is silently JSON there, never an error.
        if format == "csv":
            self._respond_csv(falcon_response, cards, has_more=has_more)
            return None
        return self._scryfall_respond(
            falcon_response,
            card_list(cards, total_cards=total_cards, has_more=has_more, next_page=next_page, warnings=warnings),
            pretty=is_pretty,
        )

    def _respond_csv(
        self,
        falcon_response: falcon.Response | None,
        cards: Sequence[dict[str, Any]],
        *,
        has_more: bool,
    ) -> None:
        """Write a page of cards as Scryfall's CSV document.

        Args:
            falcon_response: The response to write to.
            cards: The page's card objects.
            has_more: Whether another page follows, which the body has no envelope to carry.
        """
        if falcon_response is None:
            return
        falcon_response.content_type = _CSV_CONTENT_TYPE
        falcon_response.set_header("Content-Disposition", _CSV_CONTENT_DISPOSITION)
        # `has_more` has no envelope to live in, so it rides a header, exactly as Scryfall does it.
        # Without it, paginating a CSV export means asking for the JSON first.
        falcon_response.set_header(_CSV_HAS_MORE_HEADER, "true" if has_more else "false")
        falcon_response.text = _cards_to_csv(cards)

    # ---------------------------------------------------------------- GET /cards/named

    @route(paths=("cards/named",))
    def scryfall_cards_named(  # noqa: PLR0913
        self,
        *,
        falcon_response: falcon.Response | None = None,
        exact: str | None = None,
        fuzzy: str | None = None,
        set: str | None = None,  # noqa: A002  -- Scryfall's parameter name
        format: str = "json",  # noqa: A002  -- Scryfall's parameter name
        face: str = "front",
        version: str = DEFAULT_IMAGE_VERSION,
        pretty: str = "false",
        **_: object,
    ) -> dict[str, Any] | None:
        """Return one card by exact or fuzzy name.

        Args:
            falcon_response: The Falcon response to write to.
            exact: A name to match exactly, ignoring case.
            fuzzy: A name to match loosely.
            set: Restrict the search to one set code.
            format: Response format -- json, text or image.
            face: Which face an image request wants.
            version: Which image size an image request wants.
            pretty: Whether to indent JSON output.

        Returns:
            A card object, or a Scryfall error object.
        """
        is_pretty = _as_bool(pretty)
        _set_cards_cache(falcon_response)
        self._require_setup_complete()
        if not (exact or fuzzy):
            return self._scryfall_respond(
                falcon_response,
                bad_request_error(_NAMED_MISSING_PARAM_DETAILS),
                pretty=is_pretty,
            )

        params: dict[str, Any] = {}
        clauses = []
        if set:
            clauses.append("lower(card_set_code) = lower(%(set_code)s)")
            params["set_code"] = set

        if exact:
            # Scryfall's exact match ignores case, diacritics AND punctuation, and matches a single
            # face of a two-faced card as well as the combined "Front // Back" this corpus stores.
            # `_EXACT_NAME_MATCH` is that key set; the block above it carries the measurements, and
            # the two things it corrects here are that the comparison is COLLATED rather than
            # folded, and that a face key exists only when the name splits in EXACTLY two --
            # `split_part(..., 2)` read *Who // What // When // Where // Why* as having a back face
            # named "What", so `exact=What` answered und/75 where Scryfall 404s.
            params["folded"] = fold_accents(exact.strip().lower())
            params["collated"] = _collate_name(exact)
            clauses.append(_EXACT_NAME_MATCH)
            # The ENGINE first, same as the fuzzy stages below and `_cards_by_ids`. This was the
            # last by-name lookup still answering from SQL, and it is the one a scan hurts most:
            # `named?exact=` is a single-card fetch that walked all ~31,700 folded names. It takes
            # the FOLDED needle and collates it itself, so the two paths cannot collate differently.
            card = None
            chosen = self._engine_exact_name(params["folded"], params.get("set_code"))
            if chosen is not None:
                found = self._cards_by_ids([str(chosen["scryfall_id"])])
                card = found[0] if found else None
            if card is None:
                card = self._fetch_one_card(" AND ".join(clauses), params, rank_first=_WHOLE_NAME_FIRST)
            if card is None:
                return self._scryfall_respond(
                    falcon_response,
                    not_found_error(f"No cards found matching “{exact}”"),
                    pretty=is_pretty,
                )
            return self._render_card(
                card,
                falcon_response=falcon_response,
                card_format=format.lower(),
                face=face,
                version=version,
                pretty=is_pretty,
            )

        return self._named_fuzzy(
            fuzzy or "",
            base_clauses=clauses,
            base_params=params,
            falcon_response=falcon_response,
            card_format=format.lower(),
            face=face,
            version=version,
            pretty=is_pretty,
        )

    def _named_fuzzy(  # noqa: PLR0913
        self,
        fuzzy: str,
        *,
        base_clauses: list[str],
        base_params: dict[str, Any],
        falcon_response: falcon.Response | None,
        card_format: str,
        face: str,
        version: str,
        pretty: bool,
    ) -> dict[str, Any] | None:
        """Resolve a fuzzy name: exact, then all-words-present, then typo-tolerant similarity.

        The three stages mirror what Scryfall resolves in practice — `lightning bolt` exactly,
        `bolt` by containment, `lighning bolt` by trigram distance — and each stage that finds more
        than one distinct card name reports `ambiguous` rather than guessing between them.

        Args:
            fuzzy: The name fragment to match.
            base_clauses: Predicates already established (the set filter).
            base_params: Their bound parameters.
            falcon_response: The Falcon response to write to.
            card_format: "json", "text" or "image".
            face: Which face an image request wants.
            version: Which image size an image request wants.
            pretty: Whether to indent JSON output.

        Returns:
            A card object, or a Scryfall error object.
        """
        needle = fold_accents(fuzzy.strip().lower())
        # Separators come off the QUERY side too, so the containment stage compares like with like:
        # "yawgmoth's" is matched as "yawgmoths", which is what "Yawgmoth's Will" reads as with ITS
        # separators gone. A word that was nothing but punctuation drops out entirely.
        words = [stripped for word in re.split(r"[^\w']+", needle) if (stripped := _unseparated(word))]
        if not words:
            return self._scryfall_respond(
                falcon_response,
                bad_request_error(_NAMED_MISSING_PARAM_DETAILS),
                pretty=pretty,
            )

        chosen = self._fuzzy_exact_candidate(needle, base_clauses, base_params)
        if chosen is None:
            candidates = self._fuzzy_containment_candidates(words, base_clauses, base_params)
            if len(candidates) > 1:
                return self._ambiguous(falcon_response, fuzzy, pretty=pretty)
            if candidates:
                chosen = candidates[0]

        if chosen is None:
            chosen = self._fuzzy_similarity_candidate(needle, base_clauses, base_params)
            if chosen is _AMBIGUOUS:
                return self._ambiguous(falcon_response, fuzzy, pretty=pretty)

        if not chosen:
            return self._scryfall_respond(
                falcon_response,
                not_found_error(f"No cards found matching “{fuzzy}”"),
                pretty=pretty,
            )

        # A candidate that already carries its rendered card is used as-is: the similarity stage's
        # engine lane resolved a specific printing -- possibly a foreign one, which the by-id
        # lookups only know canonical printings and could not find again.
        cards = [chosen["card"]] if "card" in chosen else self._cards_by_ids([str(chosen["scryfall_id"])])
        if not cards:
            return self._scryfall_respond(
                falcon_response,
                not_found_error(f"No cards found matching “{fuzzy}”"),
                pretty=pretty,
            )
        return self._render_card(
            cards[0],
            falcon_response=falcon_response,
            card_format=card_format,
            face=face,
            version=version,
            pretty=pretty,
        )

    def _ambiguous(self, falcon_response: falcon.Response | None, name: str, *, pretty: bool) -> dict[str, Any] | None:
        """Emit Scryfall's `ambiguous` error, which is a `not_found` CARRYING a type.

        Measured on api.scryfall.com 2026-08-16, `/cards/named?fuzzy=aust com`:

            {"object":"error","code":"not_found","type":"ambiguous","status":404,
             "details":"Too many cards match ambiguous name “aust com”. Add more words..."}

        This sent `"code":"ambiguous"` with no `type` -- the same 404 with a different body. `code`
        is the coarse class ("this resolved to no one card") and `type` carries the refinement,
        which is Scryfall's split rather than ours.

        Args:
            falcon_response: The Falcon response to write to.
            name: The name that matched more than one card.
            pretty: Whether to indent JSON output.

        Returns:
            The error object.
        """
        return self._scryfall_respond(
            falcon_response,
            error_object(
                code="not_found",
                error_type="ambiguous",
                status=404,
                details=f"Too many cards match ambiguous name “{name}”. Add more words to refine your search.",
            ),
            pretty=pretty,
        )

    def _best_printing(self, where: str, params: dict[str, Any], rank_first: str = "") -> dict[str, Any] | None:
        """Return the id and name of the best-scoring printing matching a predicate.

        Args:
            where: SQL predicate over `magic.cards AS card`.
            params: Bound parameters.
            rank_first: An ORDER BY fragment ranked ABOVE prefer_score, ending in ", ".

        Returns:
            A row with scryfall_id and card_name, or None.
        """
        rows = self._run_query(
            query=(
                f"SELECT scryfall_id, card_name FROM magic.cards AS card WHERE {where} "
                f"ORDER BY {rank_first}prefer_score DESC NULLS LAST, released_at DESC LIMIT 1"
            ),
            params=params,
            explain=False,
        )["result"]
        return rows[0] if rows else None

    def _fuzzy_exact_candidate(
        self,
        needle: str,
        base_clauses: list[str],
        base_params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return the card one of whose names IS the query, separators aside, if there is one.

        Separators do not count here either (see `_fuzzy_containment_candidates`):
        `fuzzy=lightningbolt` answers Lightning Bolt on api.scryfall.com (2026-08-16) rather than
        reporting it ambiguous with "Emeritus of Conflict // Lightning Bolt", which contains the
        same letters. And a PRINTED name that is the query resolves to its printing --
        `fuzzy=blitzschlag` answers the German Lightning Bolt, `fuzzy=ego à deriva` the
        Portuguese Unmoored Ego -- while `exact=` stays scoped to oracle names.

        Args:
            needle: The accent-folded, lowercased query.
            base_clauses: Predicates already established (the set filter).
            base_params: Their bound parameters.

        Returns:
            The matching printing, or None.
        """
        # NO ENGINE FAST PATH HERE, deliberately, and it is the one by-name lookup that keeps
        # answering from SQL. `exact_card_by_name` implements `named?exact=`'s rule -- ORACLE names
        # only, separators intact, either side of a `" // "` join -- and this stage's rule is the
        # other one: separators do not count, and a PRINTED name counts. The two disagree on real
        # queries (`fuzzy=fire` would resolve to "Fire // Ice" through the engine and is not a
        # whole-name match at all under the measured rule), so calling it here would answer a
        # different card, not the same card sooner. `named?exact=` still goes to the engine first.
        params = {**base_params, "needle": _unseparated(needle)}
        oracle = f"{_UNSEPARATED.format(column='card_name_folded')} = %(needle)s"
        printed = f"{_UNSEPARATED.format(column='printed_name_folded')} = %(needle)s"
        clauses = [*base_clauses, f"({oracle} OR {printed})"]
        # An ORACLE name that is the query outranks a PRINTED one that is: `exact=` is scoped to
        # oracle names (measured -- `exact=Ego à Deriva` is a 404 there while `fuzzy=` resolves
        # it), so when both exist the English card is the one the query names.
        return self._best_printing(" AND ".join(clauses), params, rank_first=f"({oracle}) DESC, ")

    def _fuzzy_containment_candidates(
        self,
        words: list[str],
        base_clauses: list[str],
        base_params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return one printing per distinct card name whose NAMES carry every query word.

        Scryfall's containment stage is slacker than a LIKE per word against one column, in two
        ways this reproduces -- both measured against api.scryfall.com on 2026-08-16:

        1. SEPARATORS DO NOT COUNT. A word is matched against the name with every non-alphanumeric
           character removed, so it may span the name's own word boundaries: `fuzzy=goad` is inside
           "Ego à Deriva" ("eg|o a d|eriva"), and `fuzzy=aust com` matches "Manicomio Infausto".
           `_UNSEPARATED` is the SQL side of that fold, and `api/db/2026-08-16-01-unseparated-name
           -search.sql` indexes the identical expression so this stays index-assisted.
        2. THE POOL IS THE PRINTING'S NAMES, NOT ONE NAME. Each word may land in EITHER the oracle
           name or that printing's printed name, independently and in any order -- `fuzzy=red goad`
           takes `red` from "Unmoo|red| Ego" and `goad` from the Portuguese printing's name, and
           `fuzzy=goad red` resolves to the same printing. `magic.cards` is a row per PRINTING, so
           the pool is exactly this row's two name columns.

        The row that answers is the shortest completing printed name (English rows, whose
        `printed_name_folded` is NULL, sort first at length 0), then prefer score. Length rather
        than score alone because a name that spells the query and nothing else is the match the
        query meant: `fuzzy=ego à deriva` is carried by the Portuguese "Ego à Deriva", the Spanish
        "Ego a la deriva" and the Italian "Ego alla Deriva" alike, and Scryfall answers the
        Portuguese one.

        Args:
            words: The folded query, split into words.
            base_clauses: Predicates already established (the set filter).
            base_params: Their bound parameters.

        Returns:
            Up to two rows -- enough to tell "one match" from "ambiguous" without fetching more.
        """
        # The ENGINE first. A LIKE per word is a sequential scan of every folded name; the engine
        # narrows the same predicate through `name_trigram` -- measured 1,303 us against 11 us.
        engine = self._engine_for_lookup()
        if engine is not None and words:
            try:
                rows = engine.cards_containing_all_words(
                    list(words),
                    base_params.get("set_code"),
                    2,
                    list(CARD_OBJECT_FIELDS),
                )
            # Any engine failure falls back to SQL; it never 500s.
            except Exception:
                logger.exception("Engine containment match failed, falling back to SQL")
            else:
                # `else`, not the `try` body: a key error here is a shape mismatch, not an engine
                # failure, and must not be swallowed into a silent fallback.
                return [{"scryfall_id": row["scryfall_id"], "card_name": row["name"]} for row in rows]

        params = dict(base_params)
        clauses = list(base_clauses)
        for index, word in enumerate(words):
            params[f"word_{index}"] = f"%{word}%"
            clauses.append(
                f"({_UNSEPARATED.format(column='card_name_folded')} LIKE %(word_{index})s "
                f"OR {_UNSEPARATED.format(column='printed_name_folded')} LIKE %(word_{index})s)",
            )
        return self._run_query(
            query=(
                "SELECT DISTINCT ON (card_name) card_name, scryfall_id "
                f"FROM magic.cards AS card WHERE {' AND '.join(clauses)} "
                "ORDER BY card_name, length(coalesce(printed_name_folded, '')), "
                "prefer_score DESC NULLS LAST LIMIT 2"
            ),
            params=params,
            explain=False,
        )["result"]

    def _fuzzy_similarity_candidate(
        self,
        needle: str,
        base_clauses: list[str],
        base_params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return the typo-tolerant match, `_AMBIGUOUS` when two names are too close to separate.

        A candidate must clear FUZZY_SIMILARITY_FLOOR, and the best must lead the next distinct
        card name by FUZZY_SIMILARITY_LEAD. The floor sits above pg_trgm's default 0.3 threshold,
        so the index-assisted `%` prefilter is always a strict superset of what the floor admits
        and no decision rests on a row the prefilter dropped.

        Args:
            needle: The accent-folded, lowercased query.
            base_clauses: Predicates already established (the set filter).
            base_params: Their bound parameters.

        Returns:
            The matching printing, `_AMBIGUOUS`, or None.
        """
        # The ENGINE first, like every other lookup on this surface. `fuzzy_name_match`
        # reimplements pg_trgm's similarity() exactly for this, and until now nothing called it:
        # the whole of "Fuzzy Name Match and Autocomplete, Computed Not Stored" was unreachable
        # from the API, which is the same defect the duplicate `_card_by_external_id` had.
        #
        # A set filter still goes to SQL: the engine matches on names alone and has no way to
        # restrict to one set, and answering the unrestricted match would be a different card.
        if not base_clauses:
            engine = self._engine_for_lookup()
            if engine is not None:
                try:
                    # No thresholds: they belong to the engine's own metric, which is not
                    # pg_trgm's, and passing the SQL path's would score one metric by the other's
                    # bar. See FUZZY_SIMILARITY_FLOOR above.
                    status, row = engine.fuzzy_card_by_name(needle, fields=list(CARD_OBJECT_FIELDS))
                # Any engine failure falls back to SQL; it never 500s.
                except Exception:
                    logger.exception("Engine fuzzy match failed, falling back to SQL")
                else:
                    # The key is `scryfall_id`, which is what CARD_OBJECT_FIELDS asks for. It read
                    # `id` before, so every hit raised KeyError INSIDE the try above, was logged as
                    # an engine failure and fell through to SQL -- this fast path had never once
                    # returned. Reading the row in `else` is what makes the next such mismatch a
                    # test failure rather than a silent permanent fallback.
                    if status == "ambiguous":
                        return _AMBIGUOUS
                    if status == "miss":
                        return None
                    if row:
                        # The whole rendered card rides along, not just its id: a foreign-name hit
                        # resolves to the FOREIGN printing ("ego à deriva" materializes the
                        # Portuguese object), and re-fetching by scryfall_id would re-resolve
                        # through the canonical lookups and lose the printing the engine chose.
                        return {"scryfall_id": row["scryfall_id"], "card_name": row["name"], "card": to_scryfall_card(row)}

        params = {**base_params, "needle": needle, "floor": FUZZY_SIMILARITY_FLOOR}
        # `%%` escapes psycopg's placeholder marker: the bare `%` operator would be read as the
        # start of one. OPERATOR(magic.%) is pg_trgm's similarity match, which the folded-name GIN
        # index serves.
        clauses = [*base_clauses, "lower(card_name_folded) OPERATOR(magic.%%) %(needle)s"]
        rows = self._run_query(
            query=(
                "SELECT DISTINCT ON (card_name) card_name, scryfall_id, "
                "magic.similarity(lower(card_name_folded), %(needle)s) AS score "
                f"FROM magic.cards AS card WHERE {' AND '.join(clauses)} "
                "AND magic.similarity(lower(card_name_folded), %(needle)s) >= %(floor)s "
                "ORDER BY card_name, prefer_score DESC NULLS LAST"
            ),
            params=params,
            explain=False,
        )["result"]
        if not rows:
            return None
        ranked = sorted(rows, key=lambda row: row["score"], reverse=True)
        if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < FUZZY_SIMILARITY_LEAD:
            return _AMBIGUOUS
        return ranked[0]

    # ---------------------------------------------------------------- GET /cards/autocomplete

    @route(paths=("cards/autocomplete",))
    def scryfall_cards_autocomplete(
        self,
        *,
        falcon_response: falcon.Response | None = None,
        q: str | None = None,
        pretty: str = "false",
        include_extras: str = "false",  # noqa: ARG002  -- declared so the 404 route listing shows it
        **_: object,
    ) -> dict[str, Any] | None:
        """Return up to 20 card names matching a partial name.

        Args:
            falcon_response: The Falcon response to write to.
            q: The partial name.
            pretty: Whether to indent JSON output.
            include_extras: Accepted, ignored -- Scryfall's own catalog excludes extras
                unconditionally, and so does the engine (`autocomplete_names`).

        Returns:
            A Catalog object of card names.
        """
        is_pretty = _as_bool(pretty)
        _set_cards_cache(falcon_response)
        self._require_setup_complete()
        needle = (q or "").strip()
        min_query_length = 2
        if len(needle) < min_query_length:
            return self._scryfall_respond(falcon_response, catalog_object([]), pretty=is_pretty)
        # FOLDED, like `named?exact=` and the fuzzy stages above. Unfolded, an ASCII query could not
        # reach a name with diacritics -- `q=eowyn` answered an empty catalog where Scryfall answers
        # three Éowyn cards -- and nobody types the accent. Both paths below compare the folded name,
        # so the engine and the SQL fallback keep answering alike.
        needle = fold_accents(needle.lower())

        # The ENGINE first, for the same reason the fuzzy match above now does: `autocomplete` was
        # added by "Fuzzy Name Match and Autocomplete, Computed Not Stored" and nothing called it.
        #
        # AND IT DELIBERATELY DISAGREES WITH THE SQL BELOW NOW. That query orders by
        # `length(card_name)`; api.scryfall.com orders by `pg_trgm` similarity over the COLLATED
        # name and hides extras, which is measured in `autocomplete_names`' own comment (30
        # prefixes, 546 adjacent pairs, zero inversions). The SQL cannot express either half --
        # neither the collation nor the extras class exists as a column -- so it stays what it has
        # always been, the degraded answer for a request the engine could not serve at all.
        engine = self._engine_for_lookup()
        if engine is not None:
            try:
                names = engine.autocomplete(needle, MAX_AUTOCOMPLETE_VALUES)
                return self._scryfall_respond(falcon_response, catalog_object(list(names)), pretty=is_pretty)
            # Any engine failure falls back to SQL; it never 500s.
            except Exception:
                logger.exception("Engine autocomplete failed, falling back to SQL")

        rows = self._run_query(
            query=(
                "SELECT card_name, "
                "min(CASE WHEN lower(card_name_folded) LIKE %(prefix)s THEN 0 ELSE 1 END) AS rank "
                "FROM magic.cards AS card WHERE lower(card_name_folded) LIKE %(needle)s "
                "GROUP BY card_name ORDER BY rank, length(card_name), card_name LIMIT %(limit)s"
            ),
            params={"prefix": f"{needle}%", "needle": f"%{needle}%", "limit": MAX_AUTOCOMPLETE_VALUES},
            explain=False,
        )["result"]
        return self._scryfall_respond(falcon_response, catalog_object([row["card_name"] for row in rows]), pretty=is_pretty)

    # ---------------------------------------------------------------- GET /cards/random

    @route(paths=("cards/random",))
    def scryfall_cards_random(  # noqa: PLR0913
        self,
        *,
        falcon_response: falcon.Response | None = None,
        q: str | None = None,
        format: str = "json",  # noqa: A002  -- Scryfall's parameter name
        face: str = "front",
        version: str = DEFAULT_IMAGE_VERSION,
        pretty: str = "false",
        include_extras: str = "false",
        include_variations: str = "false",
        **_: object,
    ) -> dict[str, Any] | None:
        """Return one random card, optionally restricted by a search query.

        `include_extras` and `include_variations` are honored here exactly as `/cards/search`
        honors them, because api.scryfall.com honors them here -- see the gate below.

        Args:
            falcon_response: The Falcon response to write to.
            q: An optional search query the card must match.
            format: Response format -- json, text or image.
            face: Which face an image request wants.
            version: Which image size an image request wants.
            pretty: Whether to indent JSON output.
            include_extras: Whether the draw may return the extras class; a trigger term in `q`
                forces it on.
            include_variations: Whether the draw may return variation printings.

        Returns:
            A card object, or a Scryfall error object.
        """
        is_pretty = _as_bool(pretty)
        self._require_setup_complete()
        # THE BARE DRAW STAYS UNGATED, deliberately: no `q` means no tree to conjoin onto, and
        # `_apply_extras_default` exempts a `TrueNode` for this exact lane anyway ("it is what the
        # by-name and random lanes search with, and they do their own scoping"). Whether
        # api.scryfall.com's own bare `/cards/random` hides the extras class was NOT established —
        # it echoes nothing, the only observable is the card it returns, and separating a ~10%
        # extras share from zero takes tens of draws. Gating it on the strength of the `q`
        # measurement below would be an inference, and the inference could quietly remove a sixth
        # of the corpus from this endpoint.
        where, params = "TRUE", {}
        if q and q.strip():
            # The same ignore-and-continue policy `/cards/search` runs, because Scryfall runs it
            # here too: `/cards/random?q=subtype:elf` is a 400 "All of your terms were ignored." and
            # not a random elf (measured 2026-08-16). A random card drawn from a query whose only
            # term was silently dropped is a random card from the WHOLE corpus, which is the worst
            # of the available answers.
            policy = scryfall_term_policy(q)
            if policy.unclosed_parens:
                return self._scryfall_respond(
                    falcon_response,
                    bad_request_error(_UNCLOSED_PARENS_DETAILS, warnings=None),
                    pretty=is_pretty,
                )
            if policy.all_ignored:
                return self._scryfall_respond(
                    falcon_response,
                    bad_request_error(_ALL_IGNORED_DETAILS, warnings=policy.warnings),
                    pretty=is_pretty,
                )
            try:
                parsed = parse_scryfall_query(policy.query)
            except ValueError:
                return self._scryfall_respond(
                    falcon_response,
                    bad_request_error(f'Failed to parse query: "{q}"'),
                    pretty=is_pretty,
                )

            # THE SAME TWO GATES `/cards/search` RESOLVES, because api.scryfall.com resolves them
            # on this route too. This draw builds its own SQL instead of going through `_search`,
            # which is exactly how it came to skip them: `_search` is where
            # `_apply_extras_default` runs, and nothing on this path called it. The consequence was
            # one route contradicting the other on the same query -- `/cards/random?q=lightning
            # bolt` could draw the Strixhaven art-series printing (astx/76) that
            # `/cards/search?q=lightning bolt` can never return.
            #
            # MEASURED HERE RATHER THAN ASSUMED FROM `/cards/search`, 2026-08-17, two requests.
            # `t:goblin cmc=0` fires no trigger and its whole population is extras (404 bare
            # against 87 with the flag), so it separates the two hypotheses in one draw:
            #
            #   /cards/random?q=t:goblin cmc=0                      -> 404 "0 cards matched this
            #                                                          search, a random card could
            #                                                          not be returned."
            #   /cards/random?q=t:goblin cmc=0&include_extras=true  -> 200 Goblin // Blood (q07/T12)
            #
            # So the default exclusion applies and the parameter is read -- a random endpoint that
            # silently stopped answering from a sixth of its corpus would have been the worse bug,
            # which is why this was probed before being ported from the sibling route.
            #
            # The auto-enable is the SAME rule and the same helpers, not a second reading of it:
            # `_extras_triggers` for the syntactic force, `_sets_with_extras` for the one
            # conditional trigger, and `_mentions_is_tag` for variations. Both must be read BEFORE
            # either splice, or the `NOT is:variation` this is about to add would itself read as
            # the caller's own `is:variation` term.
            triggers = _extras_triggers(parsed)
            forced = triggers.forced
            if not forced and triggers.sets:
                forced = not self._sets_with_extras().isdisjoint(triggers.sets)

            # Local import for the same circularity reason `/cards/search` imports
            # `VARIATION_IS_TAG` locally. The two splices are imported rather than rebuilt from
            # nodes here: one implementation of "conjoin `-is:extra`" is the whole point, and a
            # second one in this module would be free to drift from the one `_search` applies.
            from api.api_resource import (  # noqa: PLC0415
                VARIATION_IS_TAG,
                _apply_extras_default,
                _apply_variations_default,
            )

            effective_variations = _mentions_is_tag(parsed, VARIATION_IS_TAG) or _as_bool(include_variations)
            _apply_extras_default(parsed, include_extras=forced or _as_bool(include_extras))
            _apply_variations_default(parsed, include_variations=effective_variations)
            where, params = generate_sql_query(parsed)

        # This response must not be cached at either layer. The HTTP cache would pin one card as
        # "the" random card for the generation, and _run_query's cache would do the same a level
        # down -- the draw's SQL text and parameters are identical on every call, so its first
        # result would be replayed forever. Hence no-store here and an uncached draw below.
        if falcon_response is not None:
            falcon_response.set_header("Cache-Control", "no-store")

        # Two statements rather than ORDER BY random(): the count is deterministic, so it can go
        # through the cache, and the offset scan stops as soon as it has one row where a sort would
        # order the whole match set to throw all but one away.
        matched = self._run_query(
            query=f"SELECT count(1) AS total FROM magic.cards AS card WHERE {where}",
            params=params,
            explain=False,
        )["result"][0]["total"]
        if not matched:
            # Scryfall words the random miss differently from the search miss, and says the thing
            # this route was unable to do (measured on `/cards/random?q=e:notaset`).
            return self._scryfall_respond(
                falcon_response,
                not_found_error(_RANDOM_NO_MATCH_DETAILS),
                pretty=is_pretty,
            )

        rows = self._run_uncached(
            query=(
                f"SELECT {_CARD_COLUMNS} FROM magic.cards AS card WHERE {where} "
                "OFFSET floor(random() * %(matched)s)::bigint LIMIT 1"
            ),
            params={**params, "matched": matched},
        )
        if not rows:
            return self._scryfall_respond(
                falcon_response,
                not_found_error(_RANDOM_NO_MATCH_DETAILS),
                pretty=is_pretty,
            )
        return self._render_card(
            to_scryfall_card(sql_row_to_engine_row(rows[0])),
            falcon_response=falcon_response,
            card_format=format.lower(),
            face=face,
            version=version,
            pretty=is_pretty,
        )

    # ---------------------------------------------------------------- POST /cards/collection

    @route(paths=("cards/collection",), methods=("POST",))
    def scryfall_cards_collection(
        self,
        *,
        falcon_response: falcon.Response | None = None,
        request: falcon.Request | None = None,
        pretty: str = "false",
        **_: object,
    ) -> dict[str, Any] | None:
        """Resolve up to 75 card identifiers in one request.

        Args:
            falcon_response: The Falcon response to write to.
            request: The Falcon request, whose JSON body carries the identifiers.
            pretty: Whether to indent JSON output.

        Returns:
            A List object whose `data` holds the cards found and whose `not_found` holds the
            identifiers that resolved to nothing, or a Scryfall error object.
        """
        is_pretty = _as_bool(pretty)
        # A shared cache keys on the URL and this route's answer depends entirely on the BODY,
        # so it is private and always revalidated -- api.scryfall.com sends the same.
        if falcon_response is not None:
            falcon_response.set_header("Cache-Control", "max-age=0, private, must-revalidate")
        self._require_setup_complete()
        try:
            body = request.get_media() if request is not None else None
        except (falcon.MediaMalformedError, falcon.MediaNotFoundError):
            body = None
        identifiers = body.get("identifiers") if isinstance(body, dict) else None
        # Two SEPARATE complaints, and which one you get depends on whether `identifiers` was there
        # at all -- measured 2026-08-16: `{}` answers the COUNT sentence (an absent list is an empty
        # one), `{"identifiers": {}}` answers the ARRAY one. Both are `400 bad_request`, not the
        # `422 validation_error` with wording of its own this surface sent; a client string-matching
        # Scryfall's messages saw neither. A refused request is `no-cache` too, not the route's tier.
        if identifiers is not None and not isinstance(identifiers, list):
            self._set_collection_refused_cache(falcon_response)
            return self._scryfall_respond(falcon_response, bad_request_error(_COLLECTION_NOT_AN_ARRAY_DETAILS), pretty=is_pretty)
        # The count rule covers MORE than the count: an empty list, a missing list, a list past 75,
        # AND a list holding anything that is not an object all answer this one sentence (`[null]`
        # and `["Lightning Bolt"]` both measured). Scryfall validates the list's SHAPE here and the
        # identifiers' shape afterwards.
        if (
            identifiers is None
            or not identifiers
            or len(identifiers) > MAX_COLLECTION_IDENTIFIERS
            or any(not isinstance(identifier, dict) for identifier in identifiers)
        ):
            self._set_collection_refused_cache(falcon_response)
            return self._scryfall_respond(falcon_response, bad_request_error(_COLLECTION_COUNT_DETAILS), pretty=is_pretty)

        # Validation runs over the WHOLE batch before anything is resolved: Scryfall's answer to a
        # malformed identifier is one 400 for the request, not a per-identifier miss, so a batch
        # that carries one must not cost a query either.
        malformed = _collection_identifier_error(identifiers)
        if malformed is not None:
            self._set_collection_refused_cache(falcon_response)
            return self._scryfall_respond(falcon_response, malformed, pretty=is_pretty)

        found: list[dict[str, Any]] = []
        not_found: list[dict[str, Any]] = []
        for identifier in identifiers:
            card = self._resolve_identifier(identifier)
            # ONE ENTRY PER IDENTIFIER, duplicates included. This deduplicated by card id, which
            # looks like a courtesy and breaks the response's contract: `data` is the answer to the
            # list the client sent, so a client submitting a deck list with four copies of a card
            # got three fewer objects back than it had rows to fill. Measured 2026-08-16 -- three
            # identical `{id}` identifiers return three card objects, and 75 identical `{name}`
            # identifiers return 75.
            if card is None:
                not_found.append(identifier)
            else:
                found.append(card)

        return self._scryfall_respond(falcon_response, collection_list(found, not_found), pretty=is_pretty)

    def _set_collection_refused_cache(self, falcon_response: falcon.Response | None) -> None:
        """Mark a REFUSED collection request `no-cache`, not the route's own tier.

        Measured 2026-08-16 across every 400 this route can produce -- the count sentence, the array
        sentence, `Invalid identifier schema`, and the UUID and integer complaints. The successful
        answer keeps `max-age=0, private, must-revalidate`. Nearly the same instruction, genuinely
        two different strings, and this surface exists so the strings are the same.

        Args:
            falcon_response: The response to write to.
        """
        if falcon_response is not None:
            falcon_response.set_header("Cache-Control", "no-cache")

    def _resolve_identifier(self, identifier: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve one collection identifier to a card.

        Args:
            identifier: One entry of the request's `identifiers` array.

        Returns:
            The card it names, or None when nothing matched or the shape is not one Scryfall
            defines.
        """
        if "id" in identifier and _is_uuid(str(identifier["id"])):
            return self._card_by_scryfall_id(str(identifier["id"]))
        if "oracle_id" in identifier and _is_uuid(str(identifier["oracle_id"])):
            return self._card_by_oracle_id(str(identifier["oracle_id"]))
        if "illustration_id" in identifier and _is_uuid(str(identifier["illustration_id"])):
            return self._card_by_illustration_id(str(identifier["illustration_id"]))
        if "mtgo_id" in identifier:
            return self._card_by_external_id("mtgo", _as_int(str(identifier["mtgo_id"])))
        if "multiverse_id" in identifier:
            return self._card_by_multiverse_id(_as_int(str(identifier["multiverse_id"])))
        if "set" in identifier and "collector_number" in identifier:
            # The identifier has no language key, so it is the segment-less `/cards/:code/:number`
            # rule: the English printing where one exists, else the printing that carries the
            # address. `_card_at_address` carries the measurements.
            return self._card_at_address(str(identifier["set"]), str(identifier["collector_number"]), None)
        if "name" in identifier:
            set_code = identifier.get("set")
            return self._card_by_name_identifier(
                str(identifier["name"]),
                str(set_code) if set_code else None,
            )
        return None

    def _card_by_name_identifier(self, name: str, set_code: str | None) -> dict[str, Any] | None:
        """Resolve a collection identifier's `name` -- a NAME LOOKUP with its own keys.

        Not `named?exact=`'s keys, which is why this is its own engine entry point and not a call
        to the one beside it: `{"name":"Delver of Secrets // Insectile Aberration"}` is not_found on
        api.scryfall.com while `exact=` of that same string answers the card. The block above
        `_collate_name` carries the measurements for both.

        Four things the SQL this replaces got wrong, each measured: it never looked at the BACK face
        (`{"name":"Insectile Aberration"}` answers the card there and missed here), it accepted the
        joined name (`{"name":"Fire // Ice"}` is not_found there and answered here), it read a
        five-part name as having a front face (`{"name":"Who"}` is not_found there and answered
        und/75 here), and it compared `card_name` as posted -- so no accent, punctuation or spacing
        difference resolved, and `{"name":"  Lightning Bolt  "}` missed on its own whitespace.

        The ENGINE first, through `_engine_card`, like every other identifier this route accepts:
        a genuine "no such card" from a loaded store IS the answer, and only a store that cannot
        serve falls through to SQL. `_engine_exact_name` conflates those two, which is right for
        `named?exact=` -- there the SQL is a second chance at the same question -- and wrong here,
        where SQL answering a needle the engine reported not_found would be the pre-existing bug
        coming back through the fallback.

        Args:
            name: The identifier's `name` value, as posted.
            set_code: The identifier's `set`, which FILTERS the lookup -- `{"name":"Delver of
                Secrets","set":"mid"}` answers mid/47, the same card in the set asked for, and a
                card with no printing in that set drops out rather than answering another printing.

        Returns:
            The card it names, or None for not_found.
        """
        collated = _collate_name(name)
        # A needle with no alphanumeric character is nobody's name. Answered here rather than as a
        # query, because `%(collated)s = ''` would match any card whose name is punctuation alone.
        if not collated:
            return None
        found = self._engine_card(
            lambda e: e.collection_card_by_name(fold_accents(name.strip().lower()), set_code, list(CARD_OBJECT_FIELDS)),
        )
        if found is not _ENGINE_MISS:
            return found
        clauses = [_COLLECTION_NAME_MATCH]
        params: dict[str, Any] = {"collated": collated}
        if set_code:
            clauses.append("lower(card_set_code) = lower(%(set_code)s)")
            params["set_code"] = set_code
        return self._fetch_one_card(" AND ".join(clauses), params, rank_first=_WHOLE_NAME_FIRST)

    # ---------------------------------------------------------------- GET /cards and /cards/...

    @route()
    def cards(  # noqa: PLR0913
        self,
        identifier: str = "",
        number: str = "",
        suffix: str = "",
        *,
        falcon_response: falcon.Response | None = None,
        request: falcon.Request | None = None,
        request_host: str = "",
        page: str = "1",
        format: str = "json",  # noqa: A002  -- Scryfall's parameter name
        face: str = "front",
        version: str = DEFAULT_IMAGE_VERSION,
        pretty: str = "false",
        **_: object,
    ) -> dict[str, Any] | None:
        """Serve every `/cards/*` route the five named sub-routes do not claim.

        The path shapes, by segment count:

        - `/cards` -- every card, paginated.
        - `/cards/:id` -- one card by Scryfall id.
        - `/cards/:namespace/:id` -- one card by multiverse, MTGO, Arena, TCGplayer or Cardmarket id.
        - `/cards/:id/rulings` -- the rulings for one card.
        - `/cards/:code/:number` -- one card by set code and collector number.
        - `/cards/:code/:number/:lang` -- the same, in one language.
        - `/cards/:namespace/:id/rulings` and `/cards/:code/:number/rulings` -- rulings, addressed
          the same two ways.

        Args:
            identifier: First path segment: a Scryfall id, an external id namespace, or a set code.
            number: Second path segment: an external id, a collector number, or "rulings".
            suffix: Third path segment: a language code or "rulings".
            falcon_response: The Falcon response to write to.
            request: The Falcon request, read for the scheme `next_page` should use.
            request_host: Host the request arrived on, used to build `next_page`.
            page: 1-based page number, for the unfiltered `/cards` listing.
            format: Response format -- json, text or image.
            face: Which face an image request wants.
            version: Which image size an image request wants.
            pretty: Whether to indent JSON output.

        Returns:
            A card, List or Catalog object, or a Scryfall error object.
        """
        is_pretty = _as_bool(pretty)
        _set_cards_cache(falcon_response)
        self._require_setup_complete()

        if not identifier:
            return self._all_cards_page(
                falcon_response=falcon_response,
                request=request,
                request_host=request_host,
                page=page,
                pretty=is_pretty,
            )

        wants_rulings = "rulings" in (number, suffix)
        card = self._resolve_path_card(identifier, number, suffix, wants_rulings=wants_rulings)
        if card is None:
            return self._scryfall_respond(
                falcon_response,
                not_found_error(_miss_details(identifier, number, suffix)),
                pretty=is_pretty,
            )
        if wants_rulings:
            return self._scryfall_respond(falcon_response, self._rulings_for(card), pretty=is_pretty)
        return self._render_card(
            card, falcon_response=falcon_response, card_format=format.lower(), face=face, version=version, pretty=is_pretty
        )

    def _resolve_path_card(self, identifier: str, number: str, suffix: str, *, wants_rulings: bool) -> dict[str, Any] | None:
        """Resolve the card a `/cards/...` path addresses.

        Args:
            identifier: First path segment.
            number: Second path segment.
            suffix: Third path segment.
            wants_rulings: Whether a trailing "rulings" segment was consumed from the path.

        Returns:
            The card, or None when the path addresses nothing.
        """
        # Drop the trailing "rulings" so the rest reads as a plain card address.
        if wants_rulings:
            if suffix == "rulings":
                suffix = ""
            else:
                number, suffix = "", ""

        if identifier in _EXTERNAL_ID_NAMESPACES:
            external_id = _as_int(number)
            if external_id is None:
                return None
            if identifier == "multiverse":
                return self._card_by_multiverse_id(external_id)
            return self._card_by_external_id(identifier, external_id)

        if not number:
            if not _is_uuid(identifier):
                return None
            return self._card_by_scryfall_id(identifier)

        return self._card_at_address(identifier, number, suffix or None)

    def _card_at_address(self, set_code: str, number: str, lang: str | None) -> dict[str, Any] | None:
        """Resolve a set code and collector number, with or without a language, as Scryfall does.

        A NAMED language is exact: `/cards/m15/18/de` is a 404 on api.scryfall.com because no German
        printing carries that address, and a miss stays a miss here. An ABSENT language is NOT
        "English" and NOT "any language" -- it is English where an English printing exists, else the
        printing that does carry the address. Measured 2026-09-11: The Hobbit Eternal prints five of
        its 158 cards only in Dwarvish, and `/cards/hoc/95` answers the `lang: dw` Arcane Signet,
        `/cards/hoc/96` the Dwarvish Mox Amber, `/cards/pmei/2010-1` the Japanese-only Darksteel
        Juggernaut; `POST /cards/collection` finds `{"set":"hoc","collector_number":"95"}` with an
        empty not_found. Wherever an English row exists it is the English row that answers the
        segment-less form, whatever a foreign row sharing the number would score. This hardcoded
        `en` for the absent segment, so every one of those addresses was a 404 that
        `/cards/search?q=!"Arcane Signet"&unique=prints` and `/cards/hoc/95/dw` contradicted.

        ONE query either way. With no language the lang clause drops out and `_ENGLISH_FIRST` goes
        ahead of the prefer ordering, so an English row wins whenever one exists and a foreign row
        answers only when none does. Which printing answers when SEVERAL non-English rows share an
        address and no English one does is not measured; the existing prefer_score / released_at
        order picks among them.

        The filter is the card_lang COLUMN -- the same column the lang: operator compares,
        backfilled for pre-multilingual rows -- not a blob lookup.

        Args:
            set_code: The set code, in any case.
            number: The collector number, as printed.
            lang: The language named by the request, or None when it named none.

        Returns:
            The card, or None when the address resolves to nothing.
        """
        clauses = ["lower(card_set_code) = lower(%(set_code)s)", "collector_number = %(number)s"]
        params: dict[str, Any] = {"set_code": set_code, "number": number}
        if lang is None:
            return self._fetch_one_card(" AND ".join(clauses), params, rank_first=_ENGLISH_FIRST)
        clauses.append("card_lang = %(lang)s")
        params["lang"] = lang.lower()
        return self._fetch_one_card(" AND ".join(clauses), params)

    def _card_by_multiverse_id(self, multiverse_id: int | None) -> dict[str, Any] | None:
        """Fetch a card by Gatherer multiverse id.

        Args:
            multiverse_id: The id to match.

        Returns:
            The card, or None when nothing matched.
        """
        if multiverse_id is None:
            return None
        return self._fetch_one_card(
            "raw_card_blob -> 'multiverse_ids' @> %(value)s::jsonb",
            {"value": str(multiverse_id)},
        )

    def _all_cards_page(
        self,
        *,
        falcon_response: falcon.Response | None,
        request: falcon.Request | None,
        request_host: str,
        page: str,
        pretty: bool,
    ) -> dict[str, Any] | None:
        """Serve one page of the unfiltered `/cards` listing.

        Args:
            falcon_response: The Falcon response to write to.
            request: The Falcon request, read for the scheme `next_page` should use.
            request_host: Host the request arrived on.
            page: 1-based page number.
            pretty: Whether to indent JSON output.

        Returns:
            A List object of cards, or a Scryfall error object.
        """
        # The same never-rejecting `page` as /cards/search -- one rule, because it is one parameter.
        page_number = _scryfall_page(page)
        total = self._run_query(query="SELECT count(1) AS total FROM magic.cards", explain=False)["result"][0]["total"]
        rows = self._run_query(
            query=(
                f"SELECT {_CARD_COLUMNS} FROM magic.cards AS card "
                "ORDER BY card_name, card_set_code, collector_number_int, collector_number "
                "LIMIT %(limit)s OFFSET %(offset)s"
            ),
            params={"limit": PAGE_SIZE, "offset": (page_number - 1) * PAGE_SIZE},
            explain=False,
        )["result"]
        if not rows:
            return self._scryfall_respond(
                falcon_response,
                _empty_page_error(total, (page_number - 1) * PAGE_SIZE),
                pretty=pretty,
            )

        cards = [to_scryfall_card(row) for row in rows]
        has_more = (page_number - 1) * PAGE_SIZE + len(cards) < total
        next_page = None
        if has_more:
            next_page = objects.build_page_url(_self_base_url(request, request_host, "/cards"), {}, page_number + 1)
        return self._scryfall_respond(
            falcon_response,
            card_list(cards, total_cards=total, has_more=has_more, next_page=next_page),
            pretty=pretty,
        )

    def _rulings_for(self, card: dict[str, Any]) -> dict[str, Any]:
        """Build the rulings List object for a card.

        Newest first, which is the order api.scryfall.com serves and NOT the ascending one this
        started with. Measured on 2026-08-12 over the cards whose rulings span more than one date:
        16 of 16 came back `published_at` descending, 0 ascending -- so ascending inverted every
        multi-date card for a client that had changed nothing but its base URL. Three concrete
        examples, as Scryfall returns them: Kindred Discovery 2023-09-01, 2022-06-10, 2022-06-10,
        2017-08-25; Eye of the Storm 2006-02-01, 2006-01-01, 2005-10-01 x3; Diabolic Intent
        2022-10-14 x2, 2013-04-15 x2, 2004-10-04.

        WITHIN one date the order cannot be reproduced from the bulk file, and `comment` is a
        deterministic stand-in rather than a claim to match. Scryfall orders same-date rulings by an
        internal ruling id; the file carries no id, and none of the file's own order, that order
        reversed, comment ascending or comment descending matched on any of 10 sampled cards that
        have a date carrying several rulings. That is most cards -- 13,847 of the 19,770 with
        rulings, against the 2026-08-11 dump -- so the remaining 5,923 (one ruling, or one per date)
        are the ones this now matches exactly. See docs/issues/local-scryfall-cards-api.md.

        Args:
            card: The card whose oracle id the rulings hang off.

        Returns:
            A List object of Ruling objects, empty when the card has none.
        """
        oracle_id = card.get("oracle_id")
        if not oracle_id:
            return card_list([])
        rows = self._run_query(
            query=(
                "SELECT oracle_id, source, published_at, comment FROM magic.rulings "
                "WHERE oracle_id = %(oracle_id)s ORDER BY published_at DESC, comment"
            ),
            params={"oracle_id": str(oracle_id)},
            explain=False,
        )["result"]
        return card_list([ruling_object(row) for row in rows])
