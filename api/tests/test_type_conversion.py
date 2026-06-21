"""Tests for type conversion functionality in API query parameter handling."""

from __future__ import annotations

import multiprocessing
import time
from typing import Any
from unittest.mock import patch

from api.api_resource import APIResource
from api.utils.type_conversions import make_type_converting_wrapper


class TestTypeConversion:
    """Test type conversion for query string parameters."""

    def test_make_type_converting_wrapper_with_typed_function(self) -> None:
        """Test that make_type_converting_wrapper properly converts string arguments to expected types."""

        def test_func(a: int, b: bool = True, c: str = "default") -> dict[str, Any]:
            return {
                "a": a,
                "b": b,
                "c": c,
                "types": {"a": type(a).__name__, "b": type(b).__name__, "c": type(c).__name__},
            }

        wrapped = make_type_converting_wrapper(test_func)

        # Test conversion from strings
        result = wrapped(a="42", b="false", c="world")

        assert result["a"] == 42
        assert result["b"] is False
        assert result["c"] == "world"
        assert result["types"]["a"] == "int"
        assert result["types"]["b"] == "bool"
        assert result["types"]["c"] == "str"

    def test_make_type_converting_wrapper_boolean_conversion(self) -> None:
        """Test various boolean string representations."""

        def bool_func(flag: bool) -> bool:
            return flag

        wrapped = make_type_converting_wrapper(bool_func)

        # Test truthy values
        assert wrapped(flag="true") is True
        assert wrapped(flag="1") is True
        assert wrapped(flag="yes") is True
        assert wrapped(flag="on") is True
        assert wrapped(flag="True") is True  # Case insensitive

        # Test falsy values
        assert wrapped(flag="false") is False
        assert wrapped(flag="0") is False
        assert wrapped(flag="no") is False
        assert wrapped(flag="off") is False
        assert wrapped(flag="") is False

    def test_make_type_converting_wrapper_integer_conversion(self) -> None:
        """Test integer conversion and fallback behavior."""

        def int_func(num: int) -> int:
            return num

        wrapped = make_type_converting_wrapper(int_func)

        # Valid integer conversion
        assert wrapped(num="42") == 42
        assert wrapped(num="-10") == -10

        # Invalid integer should fallback to string
        result = wrapped(num="not_a_number")
        assert result == "not_a_number"

    def test_make_type_converting_wrapper_float_conversion(self) -> None:
        """Test float conversion and fallback behavior."""

        def float_func(val: float) -> float:
            return val

        wrapped = make_type_converting_wrapper(float_func)

        # Valid float conversion
        assert wrapped(val="3.14") == 3.14
        assert wrapped(val="42") == 42.0

        # Invalid float should fallback to string
        result = wrapped(val="not_a_float")
        assert result == "not_a_float"

    def test_make_type_converting_wrapper_mixed_params(self) -> None:
        """Test wrapper with mix of string and non-string parameters."""

        def mixed_func(a: int, b: bool, c: str, d: Any = None) -> dict[str, Any]:
            return {"a": a, "b": b, "c": c, "d": d}

        wrapped = make_type_converting_wrapper(mixed_func)

        # Mix string and non-string params
        result = wrapped(a="10", b=False, c="test", d={"existing": "object"})

        assert result["a"] == 10  # Converted from string
        assert result["b"] is False  # Kept as non-string
        assert result["c"] == "test"  # String stays string
        assert result["d"] == {"existing": "object"}  # Non-string preserved

    def test_action_map_uses_type_converting_wrappers(self) -> None:
        """Test that APIResource action map uses type converting wrappers."""
        with (
            patch("api.api_resource.db_utils.make_pool"),
            patch("api.api_resource.requests.Session"),
        ):
            api_resource = APIResource(
                last_import_time=multiprocessing.Value("d", time.time(), lock=True),
            )

            # Check that import_oracle_tags is wrapped
            assert "import_oracle_tags" in api_resource.action_map

            # The wrapped function should be different from the original
            original_method = api_resource.import_oracle_tags
            wrapped_method = api_resource.action_map["import_oracle_tags"]

            # They should not be the same function object
            assert wrapped_method is not original_method

    def test_make_type_converting_wrapper_preserves_metadata(self) -> None:
        """Test that wrapper preserves original function metadata using functools.update_wrapper."""

        def original_function(param: int = 42) -> str:
            """This is the original docstring for the function.

            Args:
                param: An integer parameter

            Returns:
                A string representation
            """
            return f"result: {param}"

        # Set some additional attributes to test preservation
        original_function.custom_attr = "custom_value"

        wrapped = make_type_converting_wrapper(original_function)

        # Test that metadata is preserved
        assert wrapped.__name__ == original_function.__name__
        assert wrapped.__doc__ == original_function.__doc__
        assert wrapped.__module__ == original_function.__module__
        assert wrapped.__qualname__ == original_function.__qualname__
        assert wrapped.__annotations__ == original_function.__annotations__

        # Custom attributes should also be preserved
        assert hasattr(wrapped, "custom_attr")
        assert wrapped.custom_attr == "custom_value"  # type: ignore[attr-defined]

        # Test that functionality still works
        assert wrapped(param="10") == "result: 10"  # String converted to int

    def test_make_type_converting_wrapper_no_wrapping_needed(self) -> None:
        """Test that functions with no parameters or only self are not wrapped."""

        def no_params_func() -> str:
            return "no params"

        def only_self_func(self) -> str:
            return "only self"

        # Functions that don't need wrapping should return the same function
        wrapped_no_params = make_type_converting_wrapper(no_params_func)
        wrapped_only_self = make_type_converting_wrapper(only_self_func)

        assert wrapped_no_params is no_params_func
        assert wrapped_only_self is only_self_func

    def test_string_boolean_parameters_are_converted(self) -> None:
        """Test that make_type_converting_wrapper converts string booleans to bool."""

        def mock_method(import_cards: bool = True, import_hierarchy: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {
                "import_cards": import_cards,
                "import_hierarchy": import_hierarchy,
                "import_cards_type": type(import_cards).__name__,
                "import_hierarchy_type": type(import_hierarchy).__name__,
            }

        wrapped_mock = make_type_converting_wrapper(mock_method)
        result = wrapped_mock(import_cards="false", import_hierarchy="true")

        assert result["import_cards"] is False
        assert result["import_hierarchy"] is True
        assert result["import_cards_type"] == "bool"
        assert result["import_hierarchy_type"] == "bool"
