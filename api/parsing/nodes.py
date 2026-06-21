"""AST node classes for query parsing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from base64 import b64encode
from typing import Any


def _node_to_json(obj: object) -> object:
    """Serialize obj if it's a QueryNode, otherwise return it as-is."""
    return obj.to_json() if isinstance(obj, QueryNode) else obj


class QueryContext(dict[str, object]):
    """Parameter accumulator for SQL generation.

    Subclasses dict so existing code that reads `.values()`, iterates, or
    compares with `==` continues to work without change.
    """

    def add(self, value: object) -> str:
        """Register a bound parameter and return its ``%(name)s`` placeholder."""
        b64d = b64encode(str(value).encode()).decode().rstrip("=")
        name = f"p_{type(value).__name__}_{b64d}"
        self[name] = value
        return f"%({name})s"


# AST Classes
class QueryNode(ABC):
    """Base class for all query nodes in the abstract syntax tree (AST)."""

    @abstractmethod
    def to_sql(self: QueryNode, context: QueryContext) -> str:
        """Convert this node to a SQL WHERE clause string representation."""

    @abstractmethod
    def to_human_explanation(self: QueryNode) -> str:
        """Convert this node to a human-readable explanation."""

    def to_json(self) -> dict:
        """Serialize this node to a JSON-compatible dict for the Rust filter engine."""
        return {"node_type": self.__class__.__name__, "kwargs": self.kwargs()}

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        msg = f"{self.__class__.__name__} must implement kwargs"
        raise NotImplementedError(msg)


class LeafNode(QueryNode):
    """Abstract base class for leaf nodes in the AST.

    Not intended to be used directly.
    """


class ValueNode(LeafNode):
    """Represents a value node, such as a string or number, in the AST."""

    value: Any

    def __repr__(self: ValueNode) -> str:
        """Return a string representation of the value node."""
        return f"{self.__class__.__name__}({self.value!r})"

    def __eq__(self: ValueNode, other: object) -> bool:
        """Check equality with another ValueNode based on value."""
        if not isinstance(other, self.__class__):
            return False
        return self.value == other.value

    def __hash__(self: ValueNode) -> int:
        """Return a hash based on the class name and value."""
        return hash((self.__class__.__name__, self.value))

    def to_sql(self: ValueNode, context: QueryContext) -> str:
        """Register this value as a bound parameter and return its placeholder."""
        return context.add(self.value)

    def to_human_explanation(self: ValueNode) -> str:
        """Convert to human-readable explanation."""
        return str(self.value)


class StringValueNode(ValueNode):
    """Represents a string value node, such as 'flying' or 'Lightning Bolt'."""

    def __init__(self: StringValueNode, value: str) -> None:
        """Initialize a StringValueNode with a string value."""
        self.value = value

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"value": self.value}


class NumericValueNode(ValueNode):
    """Represents a numeric value node in the AST."""

    def __init__(self: NumericValueNode, value: float) -> None:
        """Initialize a NumericValueNode with a numeric value."""
        self.value = value

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"value": self.value}


class ManaValueNode(ValueNode):
    """Represents a mana cost value node, such as '{1}{G}' or 'WU'."""

    def __init__(self: ManaValueNode, value: str) -> None:
        """Initialize a ManaValueNode with a mana cost string."""
        self.value = value

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"value": self.value}


class RegexValueNode(ValueNode):
    r"""Represents a regex pattern value node, such as /^{T}:/ or /\spp/."""

    def __init__(self: RegexValueNode, value: str) -> None:
        """Initialize a RegexValueNode with a regex pattern string."""
        self.value = value

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"value": self.value}


class AttributeNode(LeafNode):
    """Represents an attribute of a card, such as 'cmc' or 'power'."""

    def __init__(self: AttributeNode, attribute_name: str) -> None:
        """Initialize an AttributeNode with the attribute name."""
        self.attribute_name = attribute_name.lower()

    def to_sql(self: AttributeNode, context: QueryContext) -> str:
        """Serialize this attribute node to a SQL column reference."""
        del context
        return f"card.{self.attribute_name}"

    def to_human_explanation(self: AttributeNode) -> str:
        """Convert to human-readable explanation."""
        # This is a simple fallback; CardAttributeNode will override with better logic
        return self.attribute_name.replace("_", " ")

    def __eq__(self: AttributeNode, other: object) -> bool:
        """Check equality with another AttributeNode based on attribute name.

        Args:
            other: The object to compare with.

        Returns:
            True if the objects are equal, False otherwise.
        """
        if not isinstance(other, self.__class__):
            return False
        return self.attribute_name == other.attribute_name

    def __hash__(self: AttributeNode) -> int:
        """Return a hash based on the class name and attribute name."""
        return hash((self.__class__.__name__, self.attribute_name))

    def __repr__(self: AttributeNode) -> str:
        """Return a string representation of the attribute node."""
        return f"{self.__class__.__name__}({self.attribute_name})"


class BinaryOperatorNode(QueryNode):
    """Represents a binary operator node (e.g., '=', '!=', '<', '>', etc.)."""

    def __init__(self: BinaryOperatorNode, lhs: QueryNode, operator: str, rhs: QueryNode) -> None:
        """Initialize a BinaryOperatorNode with left/right operands and an operator.

        Args:
            lhs: The left-hand side operand.
            operator: The binary operator.
            rhs: The right-hand side operand.
        """
        self.lhs = lhs
        self.operator = operator
        self.rhs = rhs
        bin_ops = {
            "-",
            "!=",
            "*",
            "/",
            "+",
            "<",
            "<=",
            "=",
            ">",
            ">=",
            ":",  # special operator that depends on the types of the compared nodes
        }
        if operator not in bin_ops:
            msg = f"Unknown operator: {operator}"
            raise ValueError(msg)

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"lhs": _node_to_json(self.lhs), "op": self.operator, "rhs": _node_to_json(self.rhs)}

    def to_sql(self: BinaryOperatorNode, context: QueryContext) -> str:
        """Serialize this binary operator node to a SQL expression."""
        sql_operator = self.operator
        if sql_operator == ":":
            sql_operator = "="
        return f"({self.lhs.to_sql(context)} {sql_operator} {self.rhs.to_sql(context)})"

    def to_human_explanation(self: BinaryOperatorNode) -> str:
        """Convert to human-readable explanation."""
        # Get explanations from left and right
        lhs_str = self.lhs.to_human_explanation()
        rhs_str = self.rhs.to_human_explanation()

        # Get operator explanation
        operator_map = {
            "=": "is",
            "!=": "is not",
            ">=": "≥",
            "<=": "≤",
            ":": "contains",
            "*": "×",  # noqa: RUF001
            "/": "÷",
        }
        operator_str = operator_map.get(self.operator, self.operator)

        # Default format
        return f"{lhs_str} {operator_str} {rhs_str}"

    def __repr__(self: BinaryOperatorNode) -> str:
        """Return a string representation of the binary operator node."""
        return f"{self.__class__.__name__}({self.lhs}, {self.operator}, {self.rhs})"

    def __eq__(self: BinaryOperatorNode, other: object) -> bool:
        """Check equality with another BinaryOperatorNode based on operands and operator.

        Args:
            other: The object to compare with.

        Returns:
            True if the objects are equal, False otherwise.
        """
        if not isinstance(other, self.__class__):
            return False
        return self.lhs == other.lhs and self.operator == other.operator and self.rhs == other.rhs

    def __hash__(self: BinaryOperatorNode) -> int:
        """Return a hash based on the class name, operands, and operator."""
        return hash((self.__class__.__name__, self.lhs, self.operator, self.rhs))


class NaryOperatorNode(QueryNode):
    """Base class for n-ary operator nodes (e.g., AND, OR) that take multiple operands."""

    def __init__(self: NaryOperatorNode, operands: list[QueryNode]) -> None:
        """Initialize an NaryOperatorNode with a list of operand nodes."""
        self.operands = operands

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"operands": [_node_to_json(op) for op in self.operands]}

    def to_sql(self: NaryOperatorNode, context: QueryContext) -> str:
        """Serialize this n-ary operator node to a SQL expression."""
        if not self.operands:
            return self._empty_result()
        if len(self.operands) == 1:
            return self.operands[0].to_sql(context)
        inners = f" {self._operator()} ".join(operand.to_sql(context) for operand in self.operands)
        return f"({inners})"

    def _operator(self: NaryOperatorNode) -> str:
        """Return the SQL operator string.

        To be implemented by subclasses.
        """
        raise NotImplementedError

    def _empty_result(self: NaryOperatorNode) -> str:
        """Return the SQL result for empty operands.

        To be implemented by subclasses.
        """
        raise NotImplementedError

    def to_human_explanation(self: NaryOperatorNode) -> str:
        """Convert to human-explanation."""
        if not self.operands:
            return ""
        if len(self.operands) == 1:
            return self.operands[0].to_human_explanation()

        # Get explanations for each operand
        parts = []
        for op in self.operands:
            explanation = op.to_human_explanation()
            # If this is an OrNode and the operand is an AndNode with multiple parts,
            # we need to ensure proper grouping with parentheses
            if isinstance(self, OrNode) and isinstance(op, AndNode) and len(op.operands) > 1:
                # The AndNode will join with " and " but needs parens in OR context
                explanation = f"({explanation})"
            parts.append(explanation)

        return self._join_explanations(parts)

    def _join_explanations(self: NaryOperatorNode, parts: list[str]) -> str:
        """Join explanation parts with the appropriate connector.

        To be implemented by subclasses.
        """
        raise NotImplementedError

    def __repr__(self: NaryOperatorNode) -> str:
        """Return a string representation of the n-ary operator node."""
        return f"{self.__class__.__name__}({', '.join(repr(op) for op in self.operands)})"

    def __eq__(self: NaryOperatorNode, other: object) -> bool:
        """Check equality with another NaryOperatorNode based on operands."""
        if not isinstance(other, self.__class__):
            return False
        return self.operands == other.operands

    def __hash__(self: NaryOperatorNode) -> int:
        """Return a hash based on the class name and operands."""
        return hash((self.__class__.__name__, tuple(self.operands)))


class AndNode(NaryOperatorNode):
    """Represents an AND operation between multiple conditions."""

    def _operator(self: AndNode) -> str:
        """Return the SQL operator for AND."""
        return "AND"

    def _empty_result(self: AndNode) -> str:
        """Return the SQL result for an empty AND (always TRUE)."""
        return "TRUE"

    def _join_explanations(self: AndNode, parts: list[str]) -> str:
        """Join explanation parts with 'and'."""
        return " and ".join(parts)


class OrNode(NaryOperatorNode):
    """Represents an OR operation between multiple conditions."""

    def _operator(self: OrNode) -> str:
        """Return the SQL operator for OR."""
        return "OR"

    def _empty_result(self: OrNode) -> str:
        """Return the SQL result for an empty OR (always FALSE)."""
        return "FALSE"

    def _join_explanations(self: OrNode, parts: list[str]) -> str:
        """Join explanation parts with 'or' and wrap in parentheses."""
        return f"({' or '.join(parts)})"


class NotNode(QueryNode):
    """Represents a NOT operation on a single operand."""

    def __init__(self: NotNode, operand: QueryNode) -> None:
        """Initialize a NotNode with a single operand node."""
        self.operand = operand

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {"operand": _node_to_json(self.operand)}

    def to_sql(self: NotNode, context: QueryContext) -> str:
        """Serialize this NOT node to a SQL expression.

        Plain NOT keeps SQL's three-valued semantics: a NULL operand (e.g.
        -power>2 on a card with no power) stays NULL and the row is excluded.
        This matches Scryfall — filters that require an attribute only ever
        match cards that have it, even under negation — and the Rust engine
        mirrors it with tri-state evaluation in FilterExpr.
        """
        operand_sql = self.operand.to_sql(context)
        return f"NOT ({operand_sql})"

    def to_human_explanation(self: NotNode) -> str:
        """Convert to human-readable explanation."""
        operand_explanation = self.operand.to_human_explanation()
        return f"not ({operand_explanation})"

    def __repr__(self: NotNode) -> str:
        """Return a string representation of the NOT node."""
        return f"Not({self.operand})"

    def __eq__(self: NotNode, other: object) -> bool:
        """Check equality with another NotNode based on operand."""
        if not isinstance(other, NotNode):
            return False
        return self.operand == other.operand

    def __hash__(self: NotNode) -> int:
        """Return a hash based on the operand."""
        return hash(("Not", self.operand))


class TrueNode(LeafNode):
    """Represents an always-true condition, used for empty queries."""

    def kwargs(self) -> dict:
        """Return this node's kwargs dict for Rust engine JSON serialization."""
        return {}

    def to_sql(self: TrueNode, context: QueryContext) -> str:
        """Serialize this node to the SQL literal TRUE."""
        del context
        return "TRUE"

    def __repr__(self: TrueNode) -> str:
        """Return a string representation of the TrueNode."""
        return "TrueNode()"

    def __eq__(self: TrueNode, other: object) -> bool:
        """Check equality with another TrueNode."""
        return isinstance(other, TrueNode)

    def __hash__(self: TrueNode) -> int:
        """Return a hash for TrueNode."""
        return hash("TrueNode")

    def to_human_explanation(self: TrueNode) -> str:
        """Return an empty explanation for the always-true node."""
        return ""


class Query(QueryNode):
    """Top-level query container node for the AST."""

    def __init__(self: Query, root: QueryNode) -> None:
        """Initialize a Query with the root QueryNode."""
        self.root = root

    def to_json(self) -> dict:
        """Delegate to the root node."""
        return self.root.to_json()

    def to_sql(self: Query, context: QueryContext) -> str:
        """Serialize this query to a SQL string."""
        return self.root.to_sql(context)

    def to_human_explanation(self: Query) -> str:
        """Convert to human-readable explanation."""
        return self.root.to_human_explanation()

    def __repr__(self: Query) -> str:
        """Return a string representation of the Query node."""
        return f"Query({self.root})"

    def __eq__(self: Query, other: object) -> bool:
        """Check equality with another Query based on the root node."""
        if not isinstance(other, Query):
            return False
        return self.root == other.root

    def __hash__(self: Query) -> int:
        """Return a hash based on the root node."""
        return hash(("Query", self.root))


def flatten_nested_operations(node: QueryNode) -> QueryNode:
    """Flatten nested AND/OR chains into canonical n-ary form.

    AndNode(a, AndNode(b, c)) → AndNode(a, b, c)
    """
    # the node is class tests are faster than isinstance
    nodecls = node.__class__
    if nodecls is AndNode:
        operands: list[QueryNode] = []
        for operand in node.operands:
            flattened = flatten_nested_operations(operand)
            if isinstance(flattened, AndNode):
                operands.extend(flattened.operands)
            else:
                operands.append(flattened)
        return AndNode(operands)
    if nodecls is OrNode:
        operands = []
        for operand in node.operands:
            flattened = flatten_nested_operations(operand)
            if isinstance(flattened, OrNode):
                operands.extend(flattened.operands)
            else:
                operands.append(flattened)
        return OrNode(operands)
    if nodecls is NotNode:
        return NotNode(flatten_nested_operations(node.operand))
    if nodecls is Query:
        return Query(flatten_nested_operations(node.root))
    return node
