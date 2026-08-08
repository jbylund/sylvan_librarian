"""Post-parse query rewriting: expand derived predicates into subtrees of primitives.

Applied once at the shared parse seam (`parse_scryfall_query`), so both the production
hand parser and the legacy pyparsing parser get identical treatment: the transform
operates on the common AST, after parsing and before SQL / Rust-engine serialization
(`parse => transform => rest`). Nothing parser-specific lives here.

Each expansion is written as a DSL string and re-parsed with the production parser, so a
definition is expressed in the same language it targets and stays correct by construction
(no hand-built node trees to drift). Every entry is count-validated against Scryfall's
live API before landing -- the naive expansion is frequently ~97-99%, not exact -- with
the rationale and residuals recorded in docs/issues/00713-is-tag-recovery.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.parsing.hand_parser import parse_query as _parse_query
from api.parsing.nodes import (
    AndNode,
    BinaryOperatorNode,
    DirectiveNode,
    NotNode,
    OrNode,
    Query,
    RegexValueNode,
    StringValueNode,
    TrueNode,
    flatten_nested_operations,
)

if TYPE_CHECKING:
    from api.parsing.nodes import QueryNode

# (original alias, lowercased value) -> expansion DSL string. Validated against
# api.scryfall.com on 2026-07-20 (see docs/issues/00713-is-tag-recovery.md).
#
# `frame:modern/old/new` are undocumented-but-live Scryfall aliases (the syntax docs list
# only the numeric frames + frame-effects); mirrored because they see real use. `is:old`
# and `is:new` ARE documented. Note `is:new` is the 2015 frame *only* -- deliberately
# narrower than `frame:new` (every post-"classic" frame) -- and both are mirrored as-is
# rather than reconciled, since diverging from Scryfall would be the real bug.
_DERIVED_EXPANSIONS: dict[tuple[str, str], str] = {
    ("frame", "modern"): "frame:2003",
    ("frame", "old"): "frame:1993 or frame:1997",
    ("frame", "new"): "frame:2003 or frame:2015 or frame:future",
    ("is", "old"): "frame:1993 or frame:1997",
    ("is", "new"): "frame:2015",
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
    ("is", "tangoland"): "otag:cycle-tangoland",  # 10; Scryfall accepts both names
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


def _parse_expansion(dsl: str) -> QueryNode:
    """Parse an expansion DSL string into a subtree (the production parser's output root).

    Uses the production hand parser directly (not `parse_scryfall_query`) so expansion of
    a synonym does not recurse back through this transform; nesting is handled explicitly
    by `_expand` re-walking the result.
    """
    return _parse_query(dsl).root


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
        # Recurse into the expansion so a definition may itself reference another derived
        # predicate; `in_progress` breaks any (mis)configured cycle (a -> ... -> a).
        subtree, _ = _expand(_parse_expansion(_DERIVED_EXPANSIONS[key]), in_progress | {key})
        return subtree, True
    return node, False


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


def _strip_directives(node: QueryNode, found: list[tuple[str, str, bool]], *, nested: bool) -> QueryNode | None:
    """Return `node` with directive leaves removed, appending (name, value, nested) in source order.

    Returns None when the node vanishes entirely (it was a directive, or a compound made only
    of directives); the original object when nothing changed. A directive is removed from the
    structure as if it had never been written — inside an Or it does not make the Or true, and
    a negated directive is still just a directive (Scryfall ignores the negation, measured
    2026-08-07: `-unique:art` dedups by artwork exactly as `unique:art` does).

    `nested` marks directives found under an Or or a negation: a directive always applies to
    the WHOLE search, so one written inside such a group looks scoped but is not — the API
    layer turns the flag into an explicit response warning rather than a silent surprise.
    Parenthesized AND groups do not count: conjunction is flat, so `(t:goblin sort:x) t:elf`
    means exactly `t:goblin sort:x t:elf`.
    """
    cls = node.__class__
    if cls is DirectiveNode:
        found.append((node.name, node.value, nested))
        return None
    if cls in (AndNode, OrNode):
        inner_nested = nested or cls is OrNode
        ops = [_strip_directives(op, found, nested=inner_nested) for op in node.operands]
        kept = [op for op in ops if op is not None]
        if not kept:
            return None
        if len(kept) == 1:
            return kept[0]
        return cls(kept) if kept != list(node.operands) else node
    if cls is NotNode:
        inner = _strip_directives(node.operand, found, nested=True)
        if inner is None:
            return None
        return NotNode(inner) if inner is not node.operand else node
    return node


def extract_directives(query: Query) -> tuple[Query, tuple[tuple[str, str, bool], ...]]:
    """Strip result-shape directives from the filter tree, returning (name, value, nested) triples.

    A directive like `sort:edhrec` constrains presentation, not membership; without this pass a
    query carrying one would serialize with a vestigial residue, making `t:goblin sort:edhrec`
    compare unequal to `t:goblin` despite filtering identically. A query that is nothing but
    directives filters as the empty query does.
    """
    found: list[tuple[str, str, bool]] = []
    root = _strip_directives(query.root, found, nested=False)
    if not found:
        return query, ()
    return Query(root if root is not None else TrueNode()), tuple(found)


# The post-parse rewrite pipeline, applied in order at the shared parse seam. Add future AST
# rewrites to this tuple — both parsers call `rewrite_query`, so a new pass lands in exactly one
# place and is guaranteed identical treatment across parsers (enforced by test_parser_parity).
_REWRITE_PASSES = (expand_derived_predicates, lower_literal_regexes)


def rewrite_query(query: Query) -> Query:
    """Apply every post-parse AST rewrite, in order. The single seam both parsers call.

    Directive extraction runs first so no later pass sees a DirectiveNode, and the collected
    pairs are attached to the final Query afterward because each pass returns a fresh Query.
    Order among the passes is significant: `expand_derived_predicates` runs before
    `lower_literal_regexes` (a synonym may expand into a subtree that itself contains a regex
    or other rewritable leaf), then any future pass appended to `_REWRITE_PASSES`.
    """
    query, directives = extract_directives(query)
    for rewrite_pass in _REWRITE_PASSES:
        query = rewrite_pass(query)
    query.directives = directives
    return query
