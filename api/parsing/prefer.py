"""Lift Scryfall's `prefer:` keyword out of the query tree.

`prefer:usd-low` does not filter cards; it chooses which printing of each matching card is returned.
Both front-end parsers read it as an ordinary text condition on the `prefer` pseudo-attribute
(see db_info), and this pass removes it from the tree so it never reaches SQL or the engine. It may
appear once, anywhere in the top-level conjunction; under an `or` or a `-` it has no meaning, so it is
rejected rather than silently dropped.
"""

from __future__ import annotations

from api.parsing.card_query_nodes import CardAttributeNode
from api.parsing.nodes import AndNode, BinaryOperatorNode, NotNode, OrNode, QueryNode, StringValueNode, TrueNode

PREFER_ATTRIBUTE = "prefer"

# Scryfall's spellings of the orderings this engine supports (api.enums.PreferOrder).
PREFER_VALUES = ("default", "oldest", "newest", "usd-low", "usd-high", "promo")

_NOT_TOP_LEVEL = "prefer: must be at the top level of the query, not inside an 'or' or negated"


def prefer_order_name(value: str) -> str:
    """Return the api.enums.PreferOrder value for a query spelling (`usd-low` -> `usd_low`)."""
    return value.replace("-", "_")


def _is_prefer_leaf(node: QueryNode) -> bool:
    return (
        isinstance(node, BinaryOperatorNode)
        and isinstance(node.lhs, CardAttributeNode)
        and node.lhs.attribute_name == PREFER_ATTRIBUTE
    )


def _contains_prefer(node: object) -> bool:
    if isinstance(node, BinaryOperatorNode):
        return _is_prefer_leaf(node) or _contains_prefer(node.lhs) or _contains_prefer(node.rhs)
    if isinstance(node, AndNode | OrNode):
        return any(_contains_prefer(operand) for operand in node.operands)
    if isinstance(node, NotNode):
        return _contains_prefer(node.operand)
    return False


def _prefer_value(node: BinaryOperatorNode) -> str:
    if node.operator not in (":", "=") or not isinstance(node.rhs, StringValueNode):
        msg = f"prefer: takes one of {', '.join(PREFER_VALUES)}"
        raise ValueError(msg)
    value = node.rhs.value.strip().lower()
    if value not in PREFER_VALUES:
        msg = f"Unknown prefer: value {value!r}; expected one of {', '.join(PREFER_VALUES)}"
        raise ValueError(msg)
    return value


def lift_prefer(root: QueryNode) -> tuple[QueryNode, str | None]:
    """Remove a top-level `prefer:` from *root*, returning the remaining tree and its value.

    Raises:
        ValueError: if `prefer:` appears more than once, below the top-level conjunction, or with
            a value or operator it does not take.
    """
    operands = root.operands if isinstance(root, AndNode) else [root]
    lifted = [operand for operand in operands if _is_prefer_leaf(operand)]
    if not lifted:
        if _contains_prefer(root):
            raise ValueError(_NOT_TOP_LEVEL)
        return root, None
    if len(lifted) > 1:
        msg = "prefer: may appear only once in a query"
        raise ValueError(msg)
    value = _prefer_value(lifted[0])
    rest = [operand for operand in operands if operand is not lifted[0]]
    if any(_contains_prefer(operand) for operand in rest):
        raise ValueError(_NOT_TOP_LEVEL)
    if not rest:
        return TrueNode(), value
    return (rest[0] if len(rest) == 1 else AndNode(rest)), value
