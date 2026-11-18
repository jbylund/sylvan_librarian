"""Static regex pattern and query-level limits for public search.

Calibrated in ``docs/issues/security-regex-execution-budget.md`` and
``scripts/regex_limit_survey/``. Enforced on the post-rewrite AST so only
patterns that will actually run as regex are checked (plain literals already
lowered to ``StringValueNode``).

Runtime/engine limits (``backtrack_limit``, request wall clock, SQL timeout) live
elsewhere; this module is parse-time static bounds only.
"""

from __future__ import annotations

import re
import re._constants as sre
import re._parser as sre_parser
from dataclasses import dataclass
from typing import TYPE_CHECKING

from api.parsing.nodes import AndNode, BinaryOperatorNode, NotNode, OrNode, QueryNode, RegexValueNode
from api.parsing.query_budget import InvalidRegexPatternError, QueryBudgetExceeded

if TYPE_CHECKING:
    from api.parsing.nodes import Query

MAX_REGEX_LEAVES_PER_QUERY = 10
MAX_PATTERN_UTF8_BYTES = 256
MAX_LOOKAROUNDS_PER_PATTERN = 4
MAX_ALTERNATIONS_PER_PATTERN = 32
MAX_REGEX_AST_NODES = 64
MAX_REGEX_PARSE_DEPTH = 16
MAX_QUANTIFIER_BOUND = 1024


@dataclass(frozen=True)
class _PatternMetrics:
    nodes: int = 0
    depth: int = 1
    lookarounds: int = 0
    alternations: int = 0
    backreferences: int = 0
    conditionals: int = 0
    quantifier_bounds: tuple[tuple[int, int], ...] = ()
    max_explicit_repeat: int = 1
    # Unbounded repeats (`*`, `+`, `{m,}`) anywhere in the (sub)pattern.
    unbounded_repeats: int = 0
    # Repeats whose body contains an unbounded repeat -- `(a+)+`, `(a*){3}` -- the shape that
    # backtracks exponentially, and that the bounded-product score above cannot see.
    nested_unbounded: int = 0
    # Unbounded repeats inside a lookaround body -- `(?=.*draw)` -- a scan restarted at every
    # position, which is what costs the engine seconds per query and again scores as nothing.
    lookaround_unbounded: int = 0


def validate_regex_patterns(query: Query) -> None:
    """Reject *query* when any regex leaf is ill-formed or over budget."""
    patterns = _collect_regex_patterns(query.root)
    if len(patterns) > MAX_REGEX_LEAVES_PER_QUERY:
        raise QueryBudgetExceeded(kind="regex_leaves")
    for pattern in patterns:
        _enforce_pattern_limits(pattern)


def _python_regex_error_reason(exc: re.error) -> str:
    """Drop the trailing `` at position N`` so the reason reads like Postgres's."""
    message = str(exc)
    if " at position " in message:
        return message.rsplit(" at position ", maxsplit=1)[0]
    return message


def _collect_regex_patterns(node: QueryNode) -> list[str]:
    if isinstance(node, (AndNode, OrNode)):
        out: list[str] = []
        for operand in node.operands:
            out.extend(_collect_regex_patterns(operand))
        return out
    if isinstance(node, NotNode):
        return _collect_regex_patterns(node.operand)
    if isinstance(node, BinaryOperatorNode) and isinstance(node.rhs, RegexValueNode):
        return [node.rhs.value]
    return []


def _enforce_pattern_limits(pattern: str) -> None:
    if len(pattern.encode("utf-8")) > MAX_PATTERN_UTF8_BYTES:
        raise QueryBudgetExceeded(kind="regex_pattern")

    try:
        parsed = sre_parser.parse(pattern, re.IGNORECASE)
    except re.error as exc:
        raise InvalidRegexPatternError(reason=_python_regex_error_reason(exc)) from None

    metrics = _analyze_pattern(parsed)
    if metrics.backreferences > 0 or metrics.conditionals > 0:
        raise QueryBudgetExceeded(kind="regex_pattern")
    if metrics.nested_unbounded > 0 or metrics.lookaround_unbounded > 0:
        raise QueryBudgetExceeded(kind="regex_pattern")
    if metrics.max_explicit_repeat > MAX_QUANTIFIER_BOUND:
        raise QueryBudgetExceeded(kind="regex_pattern")
    if metrics.lookarounds > MAX_LOOKAROUNDS_PER_PATTERN:
        raise QueryBudgetExceeded(kind="regex_pattern")
    if metrics.alternations > MAX_ALTERNATIONS_PER_PATTERN:
        raise QueryBudgetExceeded(kind="regex_pattern")
    for lower, upper in metrics.quantifier_bounds:
        if _explicit_numeric_quantifier_exceeds_bound(lower, upper):
            raise QueryBudgetExceeded(kind="regex_pattern")
    if metrics.nodes > MAX_REGEX_AST_NODES or metrics.depth > MAX_REGEX_PARSE_DEPTH:
        raise QueryBudgetExceeded(kind="regex_pattern")


def _analyze_pattern(code: list[tuple[int, object]], *, depth: int = 1) -> _PatternMetrics:
    acc = _PatternMetrics()
    for op, av in code:
        if op is sre.LITERAL:
            continue
        piece = _analyze_pattern_op(op, av, depth=depth)
        acc = _fold_pattern_metrics(acc, op, piece)
    return acc


@dataclass(frozen=True)
class _OpMetrics:
    sub: _PatternMetrics | None = None
    lookarounds: int = 0
    alternations: int = 0
    backreferences: int = 0
    conditionals: int = 0
    quantifier_bound: tuple[int, int] | None = None
    max_explicit_repeat: int = 1
    unbounded_repeat: int = 0
    nested_unbounded: int = 0
    lookaround_unbounded: int = 0


def _analyze_pattern_op(op: int, av: object, *, depth: int) -> _OpMetrics:
    child_depth = depth + 1 if op not in (sre.AT,) else depth
    if op in (sre.ASSERT, sre.ASSERT_NOT):
        sub = _analyze_pattern(av[1], depth=child_depth)
        return _OpMetrics(sub=sub, lookarounds=1, lookaround_unbounded=sub.unbounded_repeats)
    if op is sre.BRANCH:
        branches = av[1]
        return _OpMetrics(
            sub=_merge_metrics(*(_analyze_pattern(branch, depth=child_depth) for branch in branches)),
            alternations=max(0, len(branches) - 1),
        )
    if op is sre.GROUPREF:
        return _OpMetrics(backreferences=1)
    if op is sre.GROUPREF_EXISTS:
        _group_ref, if_branch, else_branch = av
        branches = [if_branch] if else_branch is None else [if_branch, else_branch]
        return _OpMetrics(
            sub=_merge_metrics(*(_analyze_pattern(branch, depth=child_depth) for branch in branches)),
            conditionals=1,
        )
    if op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
        lower, upper = av[0], av[1]
        sub = _analyze_pattern(av[-1], depth=child_depth)
        repeat = sub.max_explicit_repeat
        if _explicit_numeric_quantifier(lower, upper):
            factor = upper if upper != sre.MAXREPEAT else lower
            repeat = factor * repeat
        return _OpMetrics(
            sub=sub,
            quantifier_bound=(lower, upper),
            max_explicit_repeat=repeat,
            unbounded_repeat=int(upper == sre.MAXREPEAT),
            nested_unbounded=int(sub.unbounded_repeats > 0),
        )
    if op is sre.SUBPATTERN:
        sub = _analyze_pattern(av[-1], depth=child_depth)
        return _OpMetrics(sub=sub, max_explicit_repeat=sub.max_explicit_repeat)
    return _OpMetrics()


def _fold_pattern_metrics(acc: _PatternMetrics, op: int, piece: _OpMetrics) -> _PatternMetrics:
    nodes = acc.nodes + 1
    max_depth = acc.depth
    lookarounds = acc.lookarounds + piece.lookarounds
    alternations = acc.alternations + piece.alternations
    backreferences = acc.backreferences + piece.backreferences
    conditionals = acc.conditionals + piece.conditionals
    quantifier_bounds = list(acc.quantifier_bounds)
    max_explicit_repeat = acc.max_explicit_repeat
    unbounded_repeats = acc.unbounded_repeats + piece.unbounded_repeat
    nested_unbounded = acc.nested_unbounded + piece.nested_unbounded
    lookaround_unbounded = acc.lookaround_unbounded + piece.lookaround_unbounded

    if piece.quantifier_bound is not None:
        quantifier_bounds.append(piece.quantifier_bound)

    sub = piece.sub
    if sub is not None:
        nodes += sub.nodes
        max_depth = max(max_depth, sub.depth)
        lookarounds += sub.lookarounds
        alternations += sub.alternations
        backreferences += sub.backreferences
        conditionals += sub.conditionals
        quantifier_bounds.extend(sub.quantifier_bounds)
        unbounded_repeats += sub.unbounded_repeats
        nested_unbounded += sub.nested_unbounded
        lookaround_unbounded += sub.lookaround_unbounded
        if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.SUBPATTERN):
            max_explicit_repeat = max(max_explicit_repeat, piece.max_explicit_repeat)
        else:
            max_explicit_repeat = max(max_explicit_repeat, sub.max_explicit_repeat)

    return _PatternMetrics(
        nodes=nodes,
        depth=max_depth,
        lookarounds=lookarounds,
        alternations=alternations,
        backreferences=backreferences,
        conditionals=conditionals,
        quantifier_bounds=tuple(quantifier_bounds),
        max_explicit_repeat=max_explicit_repeat,
        unbounded_repeats=unbounded_repeats,
        nested_unbounded=nested_unbounded,
        lookaround_unbounded=lookaround_unbounded,
    )


def _merge_metrics(*metrics: _PatternMetrics) -> _PatternMetrics:
    if not metrics:
        return _PatternMetrics()
    nodes = sum(m.nodes for m in metrics)
    depth = max(m.depth for m in metrics)
    lookarounds = sum(m.lookarounds for m in metrics)
    alternations = sum(m.alternations for m in metrics)
    backreferences = sum(m.backreferences for m in metrics)
    conditionals = sum(m.conditionals for m in metrics)
    quantifier_bounds = tuple(bound for m in metrics for bound in m.quantifier_bounds)
    max_explicit_repeat = max(m.max_explicit_repeat for m in metrics)
    return _PatternMetrics(
        nodes,
        depth,
        lookarounds,
        alternations,
        backreferences,
        conditionals,
        quantifier_bounds,
        max_explicit_repeat,
        unbounded_repeats=sum(m.unbounded_repeats for m in metrics),
        nested_unbounded=sum(m.nested_unbounded for m in metrics),
        lookaround_unbounded=sum(m.lookaround_unbounded for m in metrics),
    )


def _explicit_numeric_quantifier(lower: int, upper: int) -> bool:
    """True for ``{m}`` / ``{m,n}`` / ``{m,}`` shapes, not for ``*`` / ``+`` / ``?``."""
    if upper == sre.MAXREPEAT:
        return lower > 1
    return True


def _explicit_numeric_quantifier_exceeds_bound(lower: int, upper: int) -> bool:
    """True when a single explicit numeric quantifier exceeds the public bound.

    ``{m,}`` is ``x{m}x*``: its cost is the bounded prefix, so it is judged on ``m`` like any other
    numeric bound (it used to be rejected outright for m > 1). What makes an unbounded repeat
    expensive is nesting it or putting it in a lookaround, and those are checked structurally.
    """
    if upper == sre.MAXREPEAT:
        return lower > MAX_QUANTIFIER_BOUND
    return lower > MAX_QUANTIFIER_BOUND or upper > MAX_QUANTIFIER_BOUND
