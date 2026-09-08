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


def _translate_are_escapes(pattern: str) -> str:
    r"""Rewrite the escapes Python's ``re`` cannot spell, and fold each ``\s…`` shorthand to one token.

    ``o:/.../`` is documented against PostgreSQL's ``~*``, whose word-boundary
    escapes are ``\y``/``\Y``/``\m``/``\M``. Python's ``re`` rejects all four
    outright, so measuring a pattern's budget with ``re._parser`` requires the
    same rewrite ``CompiledRegex`` applies before handing the pattern to the
    engine -- keep this table and the one in ``card_engine/src/regex_compat.rs``
    in step, or a pattern the engine runs is one this module cannot cost.

    ``\m``/``\M`` have no Python spelling either and become lookaround, which is
    what the engine does with them. They therefore COST lookaround budget here,
    and that is the honest accounting: they are exactly what puts the pattern on
    the backtracking engine that ``MAX_LOOKAROUNDS_PER_PATTERN`` exists to bound.

    ``\Z`` needs no entry -- Python and ARE agree it is end-of-string.

    THE ``\s…`` SHORTHANDS ARE MEASURED AS ONE TOKEN EACH, not as the expansions
    ``card_engine/src/regex_compat.rs`` substitutes. Python's ``re`` would otherwise read ``\sm``
    as whitespace-then-``m`` and cost a pattern that never runs, and expanding them would charge
    the searcher for a constant this codebase chose: ``\sm`` alone expands to five alternations
    and ten AST nodes, so ``o:/\sm\sm\sm\sm\sm\sm\sm/`` — seven mana symbols in a row, a
    perfectly ordinary query — would exceed both ``MAX_REGEX_AST_NODES`` and
    ``MAX_ALTERNATIONS_PER_PATTERN``. One shorthand is one piece of user-written structure, and
    that is what the budget is bounding.

    Costing it that way is safe because of where the expansions run: every one but ``\smr``
    compiles on the linear engine, which has no backtracking to blow up, and ``\smr``'s
    backreference is bounded at runtime by ``REGEX_BACKTRACK_LIMIT``. Note that this is also what
    lets ``\smr`` through at all — the backreference the engine synthesises for it is never the
    user's, and ``metrics.backreferences > 0`` below would otherwise reject every use of it.

    Only the returned string is measured; the pattern stored on the node is left
    alone, because the SQL path hands it to PostgreSQL, which speaks ARE natively.
    """
    out: list[str] = []
    # Position within the current bracket expression, if any: `None` outside one,
    # else the count of characters consumed since `[`. A `]` in the first position
    # is a literal member (POSIX), not the close, which is why this is a count and
    # not a flag.
    class_pos: int | None = None
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            if i + 1 >= len(pattern):
                out.append("\\")
                break
            nxt = pattern[i + 1]
            if class_pos is not None:
                out.append("\\" + nxt)
                class_pos += 2
                i += 2
                continue
            if nxt == "s":
                suffix = next((sfx for sfx in _SHORTHAND_SUFFIXES if pattern.startswith(sfx, i + 2)), None)
                if suffix is not None:
                    out.append(_SHORTHAND_TOKEN)
                    i += 2 + len(suffix)
                    continue
            out.append(_ARE_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        if class_pos is None:
            if char == "[":
                class_pos = 0
        elif class_pos == 0 and char == "^":
            pass
        elif class_pos == 0 and char == "]":
            class_pos = 1
        elif char == "]":
            class_pos = None
        else:
            class_pos += 1
        out.append(char)
        i += 1
    return "".join(out)


# Scryfall's `\s…` shorthands, LONGEST FIRST so `\smm` is the -X/-X shorthand rather than `\sm`
# followed by a literal `m`. The table mirrors SCRYFALL_SHORTHANDS in
# card_engine/src/regex_compat.rs, which carries the measured expansions and the counts behind
# them; only the spellings matter here, because each one costs a single token.
_SHORTHAND_SUFFIXES = ("mh", "mp", "mm", "mr", "pt", "pp", "m", "s", "c")

# U+E000, the first private-use codepoint: a character `re` parses as an ordinary literal and no
# card text contains, so folding a shorthand to it changes the measured structure and nothing else.
_SHORTHAND_TOKEN = "\ue000"

_ARE_ESCAPES = {
    "y": r"\b",
    "Y": r"\B",
    "m": r"(?<!\w)(?=\w)",
    "M": r"(?<=\w)(?!\w)",
}


def _enforce_pattern_limits(pattern: str) -> None:
    if len(pattern.encode("utf-8")) > MAX_PATTERN_UTF8_BYTES:
        raise QueryBudgetExceeded(kind="regex_pattern")

    try:
        parsed = sre_parser.parse(_translate_are_escapes(pattern), re.IGNORECASE)
    except re.error as exc:
        raise InvalidRegexPatternError(reason=_python_regex_error_reason(exc)) from None

    metrics = _analyze_pattern(parsed)
    if metrics.backreferences > 0 or metrics.conditionals > 0:
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


def _analyze_pattern_op(op: int, av: object, *, depth: int) -> _OpMetrics:
    child_depth = depth + 1 if op not in (sre.AT,) else depth
    if op in (sre.ASSERT, sre.ASSERT_NOT):
        return _OpMetrics(sub=_analyze_pattern(av[1], depth=child_depth), lookarounds=1)
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
        return _OpMetrics(sub=sub, quantifier_bound=(lower, upper), max_explicit_repeat=repeat)
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
    )


def _explicit_numeric_quantifier(lower: int, upper: int) -> bool:
    """True for ``{m}`` / ``{m,n}`` / ``{m,}`` shapes, not for ``*`` / ``+`` / ``?``."""
    if upper == sre.MAXREPEAT:
        return lower > 1
    return True


def _explicit_numeric_quantifier_exceeds_bound(lower: int, upper: int) -> bool:
    """True when a single explicit numeric quantifier exceeds the public bound."""
    if upper == sre.MAXREPEAT:
        # ``{m,}`` with m > 1 is an explicit numeric unbounded quantifier; ``*``/``+`` are not.
        return lower > 1
    return lower > MAX_QUANTIFIER_BOUND or upper > MAX_QUANTIFIER_BOUND
