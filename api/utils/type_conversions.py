"""Type conversions for the API."""

import functools
import inspect
import logging
from typing import Any

from api.enums import CardOrdering, PreferOrder, SortDirection, UniqueOn

logger = logging.getLogger(__name__)


def identity(x: str) -> str:
    """Identity function."""
    return x


def convert_to_bool(x: str) -> bool:
    """Convert a string to a boolean."""
    return x.lower() in ("true", "1", "yes", "on", "t")


def convert_to_str_list(x: str) -> list[str]:
    """Convert a comma-separated query string value into a list of strings."""
    return [part.strip() for part in x.split(",") if part.strip()]


def _convert_string_to_type(str_value: str | None, param_type: Any) -> Any:  # noqa: ANN401
    """Convert a string value to the specified type.

    Args:
        str_value: The string value to convert
        param_type: The target type annotation

    Returns:
        The converted value, or the original string if conversion fails/unsupported
    """
    if str_value is None:
        return None

    converter_map = {
        # enums
        "CardOrdering": CardOrdering,
        "PreferOrder": PreferOrder,
        "SortDirection": SortDirection,
        "UniqueOn": UniqueOn,
        # other stuffs
        "bool": convert_to_bool,
        "float": float,
        "int": int,
        "str": identity,
        "Sequence[str]": convert_to_str_list,
    }
    if isinstance(param_type, str):
        pass
    else:
        param_type = param_type.__name__
    possible_types = [x.strip() for x in param_type.split("|")]
    for ipossible_type in possible_types:
        try:
            converter = converter_map[ipossible_type]
        except KeyError:
            continue
        try:
            converted = converter(str_value)
            logger.info(
                "Converted %s %s to %s %s",
                type(str_value),
                str_value,
                type(converted),
                converted,
            )
            return converted
        except (ValueError, TypeError):
            continue

    logger.warning(
        "Was unable to convert parameter: [%s][%s][%s]: %s",
        type(param_type),
        param_type,
        str(param_type),
        str_value,
    )
    return str_value


def make_type_converting_wrapper(func: callable) -> callable:
    """Create a wrapper that converts string arguments to the types expected by the function.

    Args:
        func: The function to wrap with type conversion

    Returns:
        A new function that converts string arguments to match the function's signature
    """
    sig = inspect.signature(func)

    # Check if function needs type conversion wrapper
    # If signature has no parameters or only has 'self', return function as-is
    params = [p for name, p in sig.parameters.items() if name not in ("self", "_")]
    if not params:
        return func

    def convert_args(**str_kwargs: str) -> dict[str, Any]:
        """Convert string keyword arguments to match function signature types."""
        converted_kwargs = {}

        for param_name, param in sig.parameters.items():
            if param_name in str_kwargs:
                str_value = str_kwargs[param_name]
                converted_kwargs[param_name] = _convert_string_to_type(str_value, param.annotation)
            elif param_name not in ("self", "_"):
                # Parameter not provided, use default if available
                if param.default != inspect.Parameter.empty:
                    converted_kwargs[param_name] = param.default

        return converted_kwargs

    # Positional-or-keyword parameter names, in declaration order — used to map path-segment
    # positional args (e.g. /card/{set_code}/{collector_number}) onto their parameter names so
    # they go through the same string-to-type conversion as query-string keyword args.
    positional_names = [
        name
        for name, p in sig.parameters.items()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]

    def wrapper(*args: Any, **raw_kwargs: Any) -> Any:  # noqa: ANN401
        """Wrapper function that converts arguments and calls the original function."""
        if len(args) > len(positional_names):
            msg = f"{func.__qualname__}() takes {len(positional_names)} positional arguments but {len(args)} were given"
            raise TypeError(msg)
        raw_kwargs = {**dict(zip(positional_names, args, strict=False)), **raw_kwargs}

        # Filter out string parameters that need conversion
        str_params = {k: v for k, v in raw_kwargs.items() if isinstance(v, str)}
        non_str_params = {k: v for k, v in raw_kwargs.items() if not isinstance(v, str)}

        # Convert string parameters
        converted_params = convert_args(**str_params)

        # Merge with non-string parameters (non-string params take precedence)
        final_params = {**converted_params, **non_str_params}

        return func(**final_params)

    # Use functools.update_wrapper to preserve original function metadata
    return functools.update_wrapper(wrapper, func)


def _get_type_name(annotation: Any) -> str:  # noqa: ANN401
    """Convert a type annotation to a readable string.

    Args:
        annotation: The type annotation to convert

    Returns:
        A string representation of the type
    """
    if annotation == inspect.Parameter.empty:
        return "Any"

    # Handle generic types and complex annotations
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    if annotation is None:
        return "None"
    if hasattr(annotation, "__origin__"):
        # Handle generic types like List[str], Dict[str, int], etc.
        origin = annotation.__origin__
        if hasattr(origin, "__name__"):
            return origin.__name__
        return str(origin)

    # Fallback to string representation
    return str(annotation)
