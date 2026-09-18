"""Bind raw request parameters to a handler's typed keyword arguments.

Replaces the per-call introspection in `type_conversions.make_type_converting_wrapper`. A `ParamBinder`
resolves a handler's annotations **once**, at construction, into a fixed plan of
`(name, converter, default)`; binding a request then walks that plan with no signature inspection, no
converter table rebuilt per parameter, and no logging per converted value.

Two behavioral differences from the function it replaces, both deliberate:

- A string that cannot be converted raises `ParamCoercionError` instead of being passed through as the
  raw string. A handler annotated `orderby: CardOrdering` can no longer receive `'nonsense'`.
- Annotations are resolved to real types with `typing.get_type_hints`, so a `str | None` parameter
  works. The old path required the annotation to already be a *string* — it called `.split("|")` on it
  — and raised `AttributeError` on a real `UnionType`. An annotation that cannot be resolved is a
  startup error, not a silently unconverted parameter.

Everything else matches the old calling convention exactly, including the parts that are load-bearing
elsewhere: positional arguments map onto positional parameter names, explicit keyword arguments override
those, non-string values pass through untouched, and unknown names are tolerated (see `bind`).
"""

from __future__ import annotations

import enum
import functools
import inspect
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class UnresolvableAnnotationError(TypeError):
    """A handler annotates a parameter with a name that does not exist at runtime.

    Almost always means the annotation's import sits under `if TYPE_CHECKING:` — which ruff's TC rules
    encourage — while this module needs the real type to build a converter. The fix is to import it at
    runtime in the handler's module, as `api_resource` does for `Sequence`.

    Deliberately fatal at construction rather than degrading: a binder that quietly skipped the
    parameter would reject every request supplying it, which is a far worse way to find out.
    """


def _resolve_hints(func: Callable[..., Any]) -> dict[str, Any]:
    """Resolve func's annotations to real types.

    Args:
        func: The handler whose annotations to resolve.

    Returns:
        Parameter name to resolved type. Empty for callables that carry no annotations at all, such as
        a MagicMock standing in for a handler: registration walks attributes, so it meets whatever is
        on the class, and refusing those would make patching a handler during construction impossible.

    Raises:
        UnresolvableAnnotationError: An annotation names something absent at runtime.
    """
    try:
        return typing.get_type_hints(func)
    except TypeError:
        # Not a module, class, method, or function — nothing to resolve, so nothing to convert.
        return {}
    except NameError as oops:
        msg = (
            f"{getattr(func, '__qualname__', func)}: {oops}. Route handler annotations are resolved at "
            f"registration, so the name must be imported at runtime rather than under TYPE_CHECKING."
        )
        raise UnresolvableAnnotationError(msg) from oops


# Scalars a query string can name directly. Enums are handled structurally rather than listed, so a new
# enum needs no edit here — which the old converter table did require.
_SCALAR_CONVERTERS: dict[Any, Callable[[str], Any]] = {
    str: lambda x: x,
    int: int,
    float: float,
    bool: lambda x: x.lower() in ("true", "1", "yes", "on", "t"),
}


def _convert_to_str_list(raw: str) -> list[str]:
    """Split a comma-separated query value, dropping empty parts."""
    return [part.strip() for part in raw.split(",") if part.strip()]


# Longest client-supplied value echoed back in an error message. The message reaches both the 400 body
# and an INFO log record, so an unbounded value lets one request amplify into a large response and a
# large log line. Long enough to show a plausible mistake in full, short enough to be harmless.
MAX_ECHOED_VALUE_LEN = 80


class ParamCoercionError(ValueError):
    """A request parameter's value is not valid for the type its handler declares.

    Carries the parameter name and, for enums, the accepted values, so a caller can build a useful 4xx
    without the handler restating its own signature. Deliberately not a Falcon error: this module has no
    framework dependency, and internal callers should see a plain exception.
    """

    def __init__(self, param: str, value: str, expected: str, allowed: tuple[str, ...] = ()) -> None:
        """Record which parameter rejected which value, and what it would have accepted.

        Args:
            param: Parameter name as the handler declares it.
            value: The raw string that could not be converted.
            expected: Human-readable name of the declared type.
            allowed: Accepted values, for enums.
        """
        self.param = param
        self.value = value
        self.expected = expected
        self.allowed = allowed
        detail = f" (expected {expected})"
        if allowed:
            detail = f" (expected one of: {', '.join(allowed)})"

        # `value` is kept whole on the attribute for internal callers; only the rendered message, which
        # is what gets reflected and logged, is bounded.
        shown = value
        if len(shown) > MAX_ECHOED_VALUE_LEN:
            shown = shown[:MAX_ECHOED_VALUE_LEN] + "…"

        super().__init__(f"Invalid value for {param!r}: {shown!r}{detail}")


def _unwrap_optional(hint: Any) -> Any:  # noqa: ANN401
    """Return the single non-None member of an Optional hint, else the hint unchanged.

    A query string never carries None, so `str | None` converts exactly like `str`. Unions with more
    than one real member are left alone; they get no converter.
    """
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        members = [m for m in typing.get_args(hint) if m is not type(None)]
        if len(members) == 1:
            return members[0]
    return hint


def _converter_for(hint: Any) -> Callable[[str], Any] | None:  # noqa: ANN401
    """Return a converter for an annotation, or None if strings cannot be converted to it.

    None is not an error. `falcon_response: falcon.Response | None` is injected as an object and never
    arrives as a string; making that an import-time failure would reject every handler that accepts a
    response. A string arriving for such a parameter is reported at bind time instead.
    """
    hint = _unwrap_optional(hint)
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return hint
    if hint in _SCALAR_CONVERTERS:
        return _SCALAR_CONVERTERS[hint]
    if typing.get_origin(hint) in (list, tuple, Sequence):
        args = typing.get_args(hint)
        if args and args[0] is str:
            return _convert_to_str_list
    return None


class ParamBinder:
    """A fixed plan for turning one handler's raw parameters into typed keyword arguments."""

    __slots__ = ("_func_name", "_known", "_plan", "_positional_names")

    def __init__(self, func: Callable[..., Any]) -> None:
        """Resolve func's annotations and precompute its binding plan.

        Args:
            func: The handler. Annotations are resolved against its module globals.
        """
        sig = inspect.signature(func)
        hints = _resolve_hints(func)

        plan: list[tuple[str, Callable[[str], Any] | None, str, bool, Any]] = []
        positional: list[str] = []
        for name, param in sig.parameters.items():
            if name == "self" or param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
                positional.append(name)
            hint = hints.get(name, param.annotation)
            has_default = param.default is not inspect.Parameter.empty
            plan.append((name, _converter_for(hint), _describe(hint), has_default, param.default))

        self._func_name = getattr(func, "__qualname__", repr(func))
        self._plan = tuple(plan)
        self._positional_names = tuple(positional)
        self._known = frozenset(name for name, *_ in plan)

    def accepts(self, name: str) -> bool:
        """Return whether the handler declares a parameter by this name.

        Lets a caller inject an object only into the handlers that asked for it. Injecting one
        unconditionally is not an option: a non-string keyword a handler neither declares nor
        absorbs through `**kwargs` reaches it as a TypeError, and most handlers declare neither.

        Args:
            name: The parameter name to look for.

        Returns:
            True when the handler has a parameter of that name.
        """
        return name in self._known

    def bind(self, args: Sequence[Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """Map positional and raw keyword arguments onto typed keyword arguments.

        Args:
            args: Positional values, mapped onto positional parameter names in declaration order.
            kwargs: Keyword values. String values are converted; anything else passes through.

        Returns:
            Keyword arguments ready to splat into the handler.

        Raises:
            TypeError: More positional values than the handler has positional parameters, or a
                positional value collides with a keyword for the same parameter.
            ParamCoercionError: A string value is not valid for its parameter's declared type.
        """
        if len(args) > len(self._positional_names):
            msg = f"{self._func_name}() takes {len(self._positional_names)} positional arguments but {len(args)} were given"
            raise TypeError(msg)
        supplied: dict[str, Any] = dict(zip(self._positional_names, args, strict=False))
        # The previous implementation let a keyword silently overwrite a colliding positional, which
        # meant a stray path segment landing on an injected parameter was discarded and the request
        # still succeeded. Report it instead; a path that does not identify anything should not 200.
        collisions = supplied.keys() & kwargs.keys()
        if collisions:
            msg = f"{self._func_name}() got multiple values for {', '.join(sorted(collisions))}"
            raise TypeError(msg)
        supplied.update(kwargs)

        bound: dict[str, Any] = {}
        for name, converter, expected, has_default, default in self._plan:
            if name in supplied:
                value = supplied[name]
                if type(value) is str:
                    if converter is None:
                        raise ParamCoercionError(name, value, expected)
                    try:
                        bound[name] = converter(value)
                    except (ValueError, TypeError) as oops:
                        raise ParamCoercionError(name, value, expected, _allowed_values(converter)) from oops
                else:
                    bound[name] = value
            elif has_default:
                bound[name] = default

        # Names the handler does not declare. Non-strings pass through so injected keywords still reach
        # handlers that accept them via **kwargs — and still raise TypeError for handlers that do not,
        # which is the existing behavior. Unknown *strings* are query noise and are dropped; per-route
        # strictness needs a route decorator to declare it and is not available yet.
        for name, value in supplied.items():
            if name not in self._known and type(value) is not str:
                bound[name] = value
        return bound


def _describe(hint: Any) -> str:  # noqa: ANN401
    """Render an annotation for an error message."""
    hint = _unwrap_optional(hint)
    return getattr(hint, "__name__", None) or str(hint)


def _allowed_values(converter: Callable[[str], Any]) -> tuple[str, ...]:
    """Return an enum converter's accepted values, or () for anything else."""
    if isinstance(converter, type) and issubclass(converter, enum.Enum):
        return tuple(str(member.value) for member in converter)
    return ()


def bind_params(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap func so callers may pass raw strings, converted per its annotations.

    A drop-in replacement for `type_conversions.make_type_converting_wrapper`. The returned callable
    holds the precomputed binder; `func` itself is untouched, so internal callers and direct tests are
    unaffected.

    Args:
        func: The handler to wrap.

    Returns:
        A callable with func's metadata that converts arguments before delegating.
    """
    binder = ParamBinder(func)

    def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return func(**binder.bind(args, kwargs))

    wrapped = functools.update_wrapper(wrapper, func)
    wrapped.binder = binder  # type: ignore[attr-defined]  # exposed for tests and registration
    return wrapped
