"""Tests for binding raw request parameters to typed handler arguments."""

from __future__ import annotations

import sys
import textwrap
import types

# Sequence is imported at runtime, not under TYPE_CHECKING, for the same reason api_resource needs it
# that way: annotations are resolved to real types at construction. Exercised by
# test_unresolvable_annotation_is_a_startup_error.
from collections.abc import Sequence  # noqa: TC003
from unittest.mock import MagicMock

import pytest

from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn
from api.utils.param_binding import (
    MAX_ECHOED_VALUE_LEN,
    ParamBinder,
    ParamBindingError,
    ParamCoercionError,
    RepeatedParamError,
    UnresolvableAnnotationError,
    bind_params,
)


def handler(  # noqa: PLR0913 - one parameter per annotation shape the real handlers use
    set_code: str = "",
    *,
    limit: int = 100,
    ratio: float = 1.0,
    verbose: bool = False,
    orderby: CardOrdering = CardOrdering.EDHREC,
    prefer: PreferOrder = PreferOrder.DEFAULT,
    direction: SortDirection = SortDirection.ASC,
    unique: UniqueOn = UniqueOn.CARD,
    q: str | None = None,
    fields: Sequence[str] | None = None,
    injected: object = None,
) -> dict[str, object]:
    """Stand-in for a route handler, covering every annotation shape the real ones use."""
    return {
        "set_code": set_code,
        "limit": limit,
        "ratio": ratio,
        "verbose": verbose,
        "orderby": orderby,
        "prefer": prefer,
        "direction": direction,
        "unique": unique,
        "q": q,
        "fields": fields,
        "injected": injected,
    }


scalar_testcases = {
    "int_from_digits": {"param": "limit", "raw": "7", "expected": 7},
    "float_from_decimal": {"param": "ratio", "raw": "2.5", "expected": 2.5},
    "bool_true_word": {"param": "verbose", "raw": "true", "expected": True},
    "bool_on": {"param": "verbose", "raw": "on", "expected": True},
    "bool_anything_else_is_false": {"param": "verbose", "raw": "maybe", "expected": False},
    "str_passes_through": {"param": "q", "raw": "lightning bolt", "expected": "lightning bolt"},
    "optional_str_is_not_none": {"param": "q", "raw": "", "expected": ""},
    "enum_by_value": {"param": "orderby", "raw": "cmc", "expected": CardOrdering.CMC},
    "enum_other_field": {"param": "unique", "raw": "printing", "expected": UniqueOn.PRINTING},
    "sequence_splits_on_comma": {"param": "fields", "raw": "name,cmc", "expected": ["name", "cmc"]},
    "sequence_strips_and_drops_empties": {"param": "fields", "raw": " name , , cmc ", "expected": ["name", "cmc"]},
}

rejected_testcases = {
    "int_from_letters": {"param": "limit", "raw": "abc"},
    "float_from_letters": {"param": "ratio", "raw": "abc"},
    "enum_unknown_value": {"param": "orderby", "raw": "nonsense"},
    "enum_wrong_case": {"param": "unique", "raw": "CARD"},
    "no_converter_for_annotation": {"param": "injected", "raw": "a string"},
}


class TestCoercion:
    """Raw strings become the types a handler declares."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(scalar_testcases.values()))),
        argvalues=[[v for _, v in sorted(scalar_testcases[name].items())] for name in sorted(scalar_testcases)],
        ids=sorted(scalar_testcases),
    )
    def test_converts_by_annotation(self, expected: object, param: str, raw: str) -> None:
        """Test each annotation shape converts a raw string to the declared type."""
        bound = ParamBinder(handler).bind((), {param: raw})
        assert bound[param] == expected

    def test_defaults_come_from_the_signature(self) -> None:
        """Test parameters absent from the request are filled from their defaults."""
        bound = ParamBinder(handler).bind((), {})
        assert bound["limit"] == 100
        assert bound["orderby"] is CardOrdering.EDHREC
        assert bound["q"] is None

    def test_non_string_values_pass_through_unconverted(self) -> None:
        """Test an injected object reaches the handler untouched.

        This is how falcon_response is supplied, so converting or rejecting it would break every
        handler that writes to the response.
        """
        sentinel = object()
        bound = ParamBinder(handler).bind((), {"injected": sentinel})
        assert bound["injected"] is sentinel

    def test_already_typed_values_are_not_reconverted(self) -> None:
        """Test an internal caller passing real types gets them back unchanged."""
        bound = ParamBinder(handler).bind((), {"limit": 7, "orderby": CardOrdering.CMC})
        assert bound["limit"] == 7
        assert bound["orderby"] is CardOrdering.CMC

    def test_positional_arguments_map_onto_positional_parameters(self) -> None:
        """Test path segments bind to positional parameters in declaration order."""
        bound = ParamBinder(handler).bind(("eoc",), {})
        assert bound["set_code"] == "eoc"


class TestRejection:
    """Values that cannot be converted are refused rather than passed through raw."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(rejected_testcases.values()))),
        argvalues=[[v for _, v in sorted(rejected_testcases[name].items())] for name in sorted(rejected_testcases)],
        ids=sorted(rejected_testcases),
    )
    def test_unconvertible_value_raises(self, param: str, raw: str) -> None:
        """Test an unconvertible string raises instead of reaching the handler as a string."""
        with pytest.raises(ParamCoercionError) as exc_info:
            ParamBinder(handler).bind((), {param: raw})
        assert exc_info.value.param == param
        assert exc_info.value.value == raw

    def test_enum_error_lists_accepted_values(self) -> None:
        """Test the message enumerates what the enum accepts, so a client can correct the request."""
        with pytest.raises(ParamCoercionError) as exc_info:
            ParamBinder(handler).bind((), {"orderby": "nonsense"})
        assert exc_info.value.allowed == tuple(member.value for member in CardOrdering)
        assert "cmc" in str(exc_info.value)

    def test_long_values_are_truncated_in_the_message(self) -> None:
        """Test an oversized value does not become an oversized 400 body and log line.

        The message is both reflected to the client and logged at INFO, so it is bounded; the full
        value stays on the attribute for internal callers.
        """
        raw = "x" * 5000
        with pytest.raises(ParamCoercionError) as exc_info:
            ParamBinder(handler).bind((), {"limit": raw})
        assert exc_info.value.value == raw
        assert len(str(exc_info.value)) < 2 * MAX_ECHOED_VALUE_LEN
        assert str(exc_info.value).endswith("…' (expected int)")

    def test_unknown_string_parameters_are_tolerated(self) -> None:
        """Test query noise is dropped rather than rejected.

        Deliberate for now: rejecting unknown parameters would break links carrying utm_* and similar,
        and per-route strictness needs a route decorator to declare it.
        """
        bound = ParamBinder(handler).bind((), {"utm_source": "twitter", "limt": "5"})
        assert "utm_source" not in bound
        assert "limt" not in bound
        assert bound["limit"] == 100

    def test_unknown_non_string_values_still_reach_the_handler(self) -> None:
        """Test undeclared non-strings pass through, so a handler without **kwargs raises TypeError.

        The previous implementation behaved this way and it is load-bearing: a path-traversal helper was
        inert only because injected keywords it did not declare raised TypeError before it ran.
        """
        sentinel = object()
        bound = ParamBinder(handler).bind((), {"undeclared": sentinel})
        assert bound["undeclared"] is sentinel

    def test_too_many_positional_arguments_raises(self) -> None:
        """Test extra path segments are refused rather than silently ignored."""
        with pytest.raises(ParamBindingError, match="positional arguments"):
            ParamBinder(handler).bind(("eoc", "104", "extra"), {})

    def test_positional_and_keyword_for_one_parameter_raises(self) -> None:
        """Test a path segment colliding with an injected keyword is an error.

        The previous implementation let the keyword win silently, so a request to a path that identified
        nothing still returned 200.
        """
        with pytest.raises(ParamBindingError, match="multiple values"):
            ParamBinder(handler).bind(("eoc",), {"set_code": "blb"})

    def test_binding_errors_are_a_distinct_type_error(self) -> None:
        """The dispatcher catches exactly this subclass, so a handler's own TypeError stays a 500."""
        assert issubclass(ParamBindingError, TypeError)
        assert not issubclass(TypeError, ParamBindingError)


class TestRepeatedParameters:
    """Falcon delivers `?q=a&q=b` as a list; only a Sequence[str] parameter has a meaning for that.

    Before this the list passed through the binder untouched (it is not a str), so `/search?q=a&q=b`
    500ed on `list.encode` inside the parser and `?unique=cards&unique=art` 400ed with the internal
    "unhashable type: 'list'" -- both for a malformed request the binder should have named.
    """

    @pytest.mark.parametrize(
        argnames=["param", "raw"],
        argvalues=[
            ("q", ["a", "b"]),
            ("unique", ["cards", "art"]),
            ("limit", ["1", "2"]),
            ("verbose", ["true", "false"]),
        ],
        ids=["str", "enum", "int", "bool"],
    )
    def test_scalar_given_twice_is_a_structured_400(self, param: str, raw: list[str]) -> None:
        with pytest.raises(RepeatedParamError, match=f"parameter {param!r} was given more than once") as excinfo:
            ParamBinder(handler).bind((), {param: raw})
        # The same family _handle already turns into a 400, so no new except clause is needed there.
        assert isinstance(excinfo.value, ParamCoercionError)
        assert excinfo.value.param == param

    def test_sequence_parameter_accepts_repetition(self) -> None:
        """`?fields=name&fields=cmc,power` means the same as `?fields=name,cmc,power`."""
        bound = ParamBinder(handler).bind((), {"fields": ["name", "cmc,power"]})
        assert bound["fields"] == ["name", "cmc", "power"]

    def test_repeated_unknown_parameter_is_query_noise(self) -> None:
        """`?utm_source=a&utm_source=b` is dropped like the single-valued form, not passed to the handler."""
        bound = ParamBinder(handler).bind((), {"utm_source": ["a", "b"]})
        assert "utm_source" not in bound


class TestAnnotationResolution:
    """Annotations are resolved to real types once, at construction."""

    def test_unresolvable_annotation_is_a_startup_error(self) -> None:
        """Test an annotation hidden behind TYPE_CHECKING fails at construction, not per request.

        Ruff's TC rules move typing-only imports into `if TYPE_CHECKING:`, which leaves the name absent
        at runtime. Resolution has to fail loudly: a binder that skipped the parameter would reject
        every request supplying it, which is a much worse way to discover the problem.
        """
        module = types.ModuleType("_param_binding_type_checking_probe")
        source = textwrap.dedent("""
            from __future__ import annotations
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from collections.abc import Sequence
            def probe(*, fields: Sequence[str] | None = None) -> None: ...
        """)
        exec(source, module.__dict__)  # noqa: S102 - fixture module built to exercise annotation resolution
        sys.modules[module.__name__] = module
        try:
            with pytest.raises(UnresolvableAnnotationError, match="imported at runtime"):
                ParamBinder(module.probe)
        finally:
            del sys.modules[module.__name__]

    def test_callable_without_annotations_binds_without_converting(self) -> None:
        """Test a non-function callable is accepted rather than rejected at registration.

        Registration walks class attributes, so it meets whatever is there — including a MagicMock left
        by a test that patched a handler during construction. `get_type_hints` raises TypeError on those
        where `inspect.signature` tolerated them, so this is a regression guard, not a hypothetical.
        """
        mock_handler = MagicMock()
        bound = ParamBinder(mock_handler).bind((), {"anything": "a string", "obj": object()})
        assert "anything" not in bound  # no annotations, so no converter, so treated as unknown
        assert "obj" in bound  # non-strings still pass through

    def test_binder_is_built_once_not_per_call(self) -> None:
        """Test the wrapper reuses one binder, since per-call introspection was the cost being removed."""
        wrapped = bind_params(handler)
        assert wrapped.binder is wrapped.binder
        assert isinstance(wrapped.binder, ParamBinder)


class TestBindParams:
    """The wrapper is a drop-in for the function it replaces."""

    def test_wrapper_converts_then_delegates(self) -> None:
        """Test the wrapped callable accepts raw strings and the handler receives typed values."""
        result = bind_params(handler)(limit="7", orderby="cmc", fields="name,cmc")
        assert result["limit"] == 7
        assert result["orderby"] is CardOrdering.CMC
        assert result["fields"] == ["name", "cmc"]

    def test_wrapper_preserves_handler_metadata(self) -> None:
        """Test functools.update_wrapper is applied, so registration can still read __name__."""
        wrapped = bind_params(handler)
        assert wrapped.__name__ == handler.__name__
        assert wrapped.__doc__ == handler.__doc__

    def test_handler_itself_is_left_unwrapped(self) -> None:
        """Test the original stays plain, so internal callers and direct tests are unaffected."""
        assert bind_params(handler).__wrapped__ is handler
        assert handler(limit=7)["limit"] == 7
