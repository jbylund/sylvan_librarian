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
from api.parsing.nodes import AndNode, BinaryOperatorNode, NotNode, OrNode, Query

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


def _expand(node: QueryNode, in_progress: frozenset[tuple[str, str]]) -> QueryNode:
    cls = node.__class__
    if cls is AndNode:
        return AndNode([_expand(op, in_progress) for op in node.operands])
    if cls is OrNode:
        return OrNode([_expand(op, in_progress) for op in node.operands])
    if cls is NotNode:
        return NotNode(_expand(node.operand, in_progress))
    key = _leaf_key(node)
    if key is not None and key in _DERIVED_EXPANSIONS and key not in in_progress:
        # Recurse into the expansion so a definition may itself reference another derived
        # predicate; `in_progress` breaks any (mis)configured cycle (a -> ... -> a).
        subtree = _parse_expansion(_DERIVED_EXPANSIONS[key])
        return _expand(subtree, in_progress | {key})
    return node


def expand_derived_predicates(query: Query) -> Query:
    """Rewrite derived-predicate leaves (frame synonyms, derivable `is:`) into primitive subtrees."""
    return Query(_expand(query.root, frozenset()))
