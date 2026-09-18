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

import re
from typing import TYPE_CHECKING

import cachebox

from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.hand_parser import parse_str_to_query as _parse_str_to_query
from api.parsing.nodes import (
    AndNode,
    BinaryOperatorNode,
    NotNode,
    NumericValueNode,
    OrNode,
    Query,
    RegexValueNode,
    StringValueNode,
    flatten_nested_operations,
)
from api.parsing.set_dates import set_release_date

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
    ("is", "vanilla"): 't:creature o=""',  # empty-oracle equality; -11 subset (Adventure/DFC textless faces + Dryad Arbor)
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
    # is:dfc = gameplay double-faced cards. Scryfall's is:dfc additionally counts art_series /
    # reversible_card / double_faced_token (~2394 art & token entries) that aren't gameplay cards
    # and aren't in our corpus, so the layout union is the correct set for our data.
    ("is", "dfc"): "layout:transform or layout:modal_dfc or layout:meld",
    # Frame-effect (stored in card_frame_data). is:colorshifted == frame:colorshifted exactly (45).
    ("is", "colorshifted"): "frame:colorshifted",
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
    ("is", "bikeland"): "otag:cycle-dual-cycling-land",  # 10, exact
    ("is", "bondland"): "otag:cycle-bondland",  # 10
    ("is", "bounceland"): "otag:bounceland",  # 17, exact
    ("is", "canopyland"): "otag:cycle-horizon-land",  # 6, exact
    ("is", "checkland"): "otag:cycle-checkland",  # 10, exact
    ("is", "creatureland"): "t:land o:become o:creature o:/still a.* land/",
    ("is", "dual"): "otag:cycle-abu-dual-land",  # 10, the ABUR duals, exact
    ("is", "fastland"): "otag:cycle-fastland",  # 10, exact
    ("is", "fetchland"): "otag:cycle-fetchland",  # 10, exact
    ("is", "filterland"): "otag:cycle-hybrid-filterland or otag:cycle-ody-filterland",  # 20 vs 22
    ("is", "gainland"): "otag:gainland",  # 42, self-updating superset of Scryfall's 15
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
}


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
    return Query(root)


# The three `date:` shapes Scryfall honors, zero-padding strict (`date:2021-02` is a month,
# `date:2021-2` is rejected -- measured 2026-09-03). Anything else in a `date:` leaf is a set code
# or an error; `year:` takes neither (`year>=hob` is a 404 there) and is left to its own parser.
_DATE_SHAPE_RE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")


class InvalidDateValueError(ValueError):
    """A `date:` value that is neither a date shape nor the code of an imported set.

    Scryfall's own sentence, curly quotes and lower-cased value included, so the client sees the
    same words for `date>=zzzz` here as there. `year>=hob` is deliberately NOT this error: Scryfall
    404s it, and both parsers already fail it as a plain parse error.

    A value that IS date-shaped but names a month that does not exist (`date:2021-13`) gets
    Scryfall's shorter sentence for that case, `Invalid date “2021-13”`, without the set-code half:
    the shape parsed and only the month was out of range (measured 2026-09-03).
    """

    def __init__(self, value: str, *, malformed_date: bool = False) -> None:
        """Initialize with the offending value; the message is the user-facing sentence."""
        self.value = value
        if malformed_date:
            self.user_message = f"Invalid date \u201c{value.lower()}\u201d"
        else:
            self.user_message = f"Invalid date or unknown set code \u201c{value.lower()}\u201d"
        super().__init__(self.user_message)


_FULL_DATE_PARTS = 3  # YYYY-MM-DD; fewer parts name a window
_DECEMBER = 12


def _partial_date_window(value: str) -> tuple[str, str] | None:
    """The half-open `[lo, hi)` ISO window a bare year or month stands for, or None for a full day.

    Measured against `e:khm` (323 cards, released 2021-02-05) on api.scryfall.com 2026-09-03: `date:2021`
    306 = `date=2021` = `date<=2021`, `date<2021` 0, `date>2021` 18 = `date>=2022`, `date>=2021` 323,
    `date!=2021` 18; and by month `date:2021-02` 305, `date<=2021-01` 0, `date>2021-01` 323 =
    `date>=2021-02`, `date<2021-02` 0. So a partial value is the whole span it names under EVERY
    operator: `=` is inside it, `<=` is before its end, `>` is from its end, `<` is before its start,
    `>=` is from its start, `!=` is outside it.
    """
    parts = value.split("-")
    if len(parts) == _FULL_DATE_PARTS:
        return None
    year = int(parts[0])
    if len(parts) == 1:
        return (f"{year:04d}-01-01", f"{year + 1:04d}-01-01")
    month = int(parts[1])
    if not 1 <= month <= _DECEMBER:
        raise InvalidDateValueError(value, malformed_date=True)
    lo = f"{year:04d}-{month:02d}-01"
    hi = f"{year + 1:04d}-01-01" if month == _DECEMBER else f"{year:04d}-{month + 1:02d}-01"
    return (lo, hi)


def _expand_partial_date(leaf: BinaryOperatorNode, lo: str, hi: str) -> QueryNode:
    """Rewrite `date <op> <year|month>` into full-day comparisons over the window `[lo, hi)`.

    Full dates are all the SQL path and the Rust engine can compare: Postgres rejects `'2021'` as a
    date literal outright, and the engine zero-pads `2021` to `20210000`, a day no printing has, so
    `date:2021` matched nothing on one path and errored on the other. The leaf's concrete class is
    kept (`type(leaf)(...)`, as `_swap_not_leaves` does) so the SQL and explanation it generates are
    the ones a user-typed full date gets.
    """

    def cmp(op: str, iso: str) -> QueryNode:
        return type(leaf)(leaf.lhs, op, StringValueNode(iso))

    op = "=" if leaf.operator == ":" else leaf.operator
    if op == "=":
        return AndNode([cmp(">=", lo), cmp("<", hi)])
    if op == "!=":
        return NotNode(AndNode([cmp(">=", lo), cmp("<", hi)]))
    if op == "<":
        return cmp("<", lo)
    if op == "<=":
        return cmp("<", hi)
    if op == ">":
        return cmp(">=", hi)
    if op == ">=":
        return cmp(">=", lo)
    msg = f"Unsupported date operator: {leaf.operator}"
    raise ValueError(msg)


def _resolve_date_leaves(node: QueryNode) -> tuple[QueryNode, bool]:
    """Resolve every `date:` leaf's value to full days; return `(node, changed)`.

    A set code becomes the set's ISO release date in place (only `rhs` changes, as
    `_lower_regex_leaves` does, so the leaf keeps its concrete class). A bare year or month is a
    WINDOW and becomes one or two full-day comparisons -- a new subtree, so the parent is rebuilt
    the way `_swap_not_leaves` rebuilds it. A full `YYYY-MM-DD` is left exactly as delivered.
    """
    cls = node.__class__
    if cls is AndNode or cls is OrNode:
        changed = False
        operands = []
        for op in node.operands:
            new_op, op_changed = _resolve_date_leaves(op)
            operands.append(new_op)
            changed |= op_changed
        return (cls(operands), True) if changed else (node, False)
    if cls is NotNode:
        new_op, changed = _resolve_date_leaves(node.operand)
        return (NotNode(new_op), True) if changed else (node, False)
    if (
        isinstance(node, BinaryOperatorNode)
        and isinstance(node.lhs, CardAttributeNode)
        and node.lhs.attribute_name == "released_at"
        and node.lhs.original_attribute == "date"
        and isinstance(node.rhs, (StringValueNode, NumericValueNode))
    ):
        value = str(node.rhs.value)
        if _DATE_SHAPE_RE.fullmatch(value):
            window = _partial_date_window(value)
            if window is None:
                return node, False
            return _expand_partial_date(node, *window), True
        released_at = set_release_date(value)
        if released_at is None:
            raise InvalidDateValueError(value)
        node.rhs = StringValueNode(released_at)
        return node, True
    return node, False


def resolve_set_code_dates(query: Query) -> Query:
    """Rewrite every `date:` leaf to full-day comparisons: set codes resolve, years and months widen.

    Scryfall resolves a set code in a `date:` value to the set's `released_at` and compares against
    it as a full day, not a window: `date>=hob` = `date>=2026-08-14` = 1200 cards, `date:hob` =
    `date:2026-08-14` = 311, `date<hob` 33203, `date<=hob` 33409, `date>hob` 901 (api.scryfall.com,
    2026-09-03, each code checked against the explicit date `/sets/<code>` reports; likewise
    `date>=3ed` 33581 = `date>=1994-04-11`, `date>=10e` 28976 = `date>=2007-07-13`, `date>=40k`
    16724 = `date>=2022-10-07`, `date>=2x2` 17295 = `date>=2022-07-08`). Uppercase and quoted codes
    resolve; only a set's primary code does (`date>=dar`, Dominaria's arena code, is an error there
    even though `e:dar` is 265).

    A bare year or month is a WINDOW under every operator (see `_partial_date_window` for the
    measurements) and is widened to full-day comparisons here for the same reason a code resolves
    here: both the SQL path and the Rust engine need literal full dates in the AST. Postgres rejects
    `'2021'` as a date literal and the engine compared `date:2021` against the day `20210000`, so
    `date:2021` -- documented syntax -- errored on one path and matched nothing on the other.

    Runs here, at the shared seam, because the parser has no database and both the SQL path and the
    Rust engine need a literal date in the AST -- the engine would otherwise fail the query with
    `bad date: hob`, and Postgres would be asked to compare a `date` column to `'hob'`.
    """
    root, changed = _resolve_date_leaves(query.root)
    if not changed:
        return query
    return flatten_nested_operations(Query(root))


# The post-parse rewrite pipeline, applied in order at the shared parse seam. Add future AST
# rewrites to this tuple — both parsers call `rewrite_query`, so a new pass lands in exactly one
# place and is guaranteed identical treatment across parsers (enforced by test_parser_parity).
_REWRITE_PASSES = (negate_not_prefix, expand_derived_predicates, lower_literal_regexes, resolve_set_code_dates)


def rewrite_query(query: Query) -> Query:
    """Apply every post-parse AST rewrite, in order. The single seam both parsers call.

    Order is significant: `negate_not_prefix` runs first (a `not:`-spelled leaf becomes
    `NotNode(is:...)`, so it reads as a plain `is:` leaf to everything after it), then
    `expand_derived_predicates` (a synonym may expand into a subtree that itself contains a
    regex or other rewritable leaf), then `lower_literal_regexes`, then `resolve_set_code_dates`
    (order-independent of the others -- no expansion produces a `date:` leaf -- and last so the
    only pass that can raise runs after the ones that only rewrite), then any future pass appended
    to `_REWRITE_PASSES`.

    ``flatten_and_deduplicate_compounds`` runs later in ``post_parse.finalize_query`` — after
    regex-budget validation — so duplicate identical regex leaves still count toward the public
    leaf limit.
    """
    for rewrite_pass in _REWRITE_PASSES:
        query = rewrite_pass(query)
    return query
