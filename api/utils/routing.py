"""Mark a method as an HTTP route, and collect the marked ones off a class.

The decorator attaches a `RouteSpec` and returns the function unchanged. It deliberately does not
wrap: wrapping would put the request-facing coercer between internal callers and the handler —
`_run_import_under_lock` calling `backfill_prefer_scores()` would be routed through it — and would add
a frame to every traceback for no benefit. `APIResource` already keeps that separation by putting the
wrapper in `action_map` while `self.method` stays plain, and a wrapping decorator would undo it.

Marking flips registration from fail-open to fail-closed. Scanning `dir(self)` for public callables
meant a method was reachable over HTTP unless someone remembered a leading underscore, which
overloaded `_` to mean both "private in Python" and "not HTTP-reachable" — so `setup_schema`, called
from `__init__` and from tests, could not be hidden without lying about its Python visibility. A
marker separates the two. Scanning the *class* rather than the instance additionally means nothing
assigned in `__init__` can become a route.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import TYPE_CHECKING, Any

from api.utils.param_binding import bind_params
from api.utils.type_conversions import _get_type_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# Attribute the marker is stored under. Named rather than inlined because registration reads it off
# arbitrary class attributes, where a typo would silently unregister every route rather than fail.
ROUTE_SPEC_ATTR = "_route_spec"

# Declaring GET implies HEAD: a HEAD response is a GET's headers without its body, and uptime checks
# and `curl -I` rely on it. Callers never list it themselves.
GET_IMPLIES = frozenset({"HEAD"})


@dataclasses.dataclass(frozen=True, slots=True)
class RouteSpec:
    """How one handler is reachable over HTTP.

    Attributes:
        paths: Action-map keys this handler answers to. Defaults to the method's own name, and is
            given explicitly where a handler also answers under a `static/` path.
        methods: Accepted HTTP methods, upper-cased, with HEAD added wherever GET is present.
        advertise: Whether the route belongs in the public route listing. Declared here but not yet
            read — the listing becomes opt-in when the admin handlers move to a child resource.
        ignore_unknown_params: Whether unrecognized query parameters are tolerated. Declared here but
            not yet read — they are still tolerated on every route.
    """

    paths: tuple[str, ...]
    methods: frozenset[str]
    advertise: bool
    ignore_unknown_params: bool


@dataclasses.dataclass(frozen=True, slots=True)
class BoundRoute:
    """A route as registered on a live resource: what to call, and what it declared.

    One entry per path, so everything dispatch needs is reached through a single lookup. These were
    three dicts keyed by the same string, which could disagree — and did: the `static/` aliases had an
    entry in one and not the others.

    Attributes:
        action: The wrapped handler to invoke, with request parameters bound to typed arguments.
        positional_capacity: How many path segments beyond the action word the handler accepts.
        spec: What the handler declared via @route. Carried whole rather than copied field by field,
            so the flags later steps read arrive without another change here.
    """

    action: Callable[..., Any]
    positional_capacity: float
    spec: RouteSpec


def route(
    *,
    methods: Sequence[str] = ("GET",),
    paths: Sequence[str] | None = None,
    advertise: bool = True,
    ignore_unknown_params: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a method as reachable over HTTP.

    Args:
        methods: Accepted HTTP methods. Declaring GET additionally accepts HEAD.
        paths: Action-map keys to answer to. Defaults to the method's own name.
        advertise: Whether the route belongs in the public route listing.
        ignore_unknown_params: Whether unrecognized query parameters are tolerated.

    Returns:
        A decorator that attaches the spec and returns the function unchanged.
    """

    def mark(func: Callable[..., Any]) -> Callable[..., Any]:
        allowed = frozenset(method.upper() for method in methods)
        if "GET" in allowed:
            allowed |= GET_IMPLIES

        declared_paths = (func.__name__,)
        if paths is not None:
            declared_paths = tuple(paths)

        spec = RouteSpec(
            paths=declared_paths,
            methods=allowed,
            advertise=advertise,
            ignore_unknown_params=ignore_unknown_params,
        )
        setattr(func, ROUTE_SPEC_ATTR, spec)
        return func

    return mark


def max_positional_args(func: Any) -> float:  # noqa: ANN401
    """Return how many positional args `func` accepts; inf if it takes *args.

    Computed once per registered route at construction, not per request. `inspect.signature()`
    follows a `bind_params` wrapper's `__wrapped__` link, so this sees the real handler's signature.

    Args:
        func: The handler, wrapped or not.

    Returns:
        The count, or inf for a handler taking *args.
    """
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return 0.0
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return float("inf")
    return float(sum(1 for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)))


def build_route_table(
    resource: object,
    *,
    prefix: str = "",
    advertise: bool | None = None,
) -> dict[str, BoundRoute]:
    """Bind every marked method on a resource into a path-to-route table.

    The same routine builds the root resource's table and a mounted child's, so a child cannot end up
    registered by a different set of rules than its parent.

    Args:
        resource: Instance whose class carries the markers. Handlers are bound to this instance.
        prefix: Path prefix to register under, for a mounted child. Empty for the root resource.
        advertise: Overrides what each route declared. A mount passes this once rather than trusting
            every handler in the child to carry the flag — forgetting it in one place is then a
            property of the mount, not a hole in one handler.

    Returns:
        Path to the route that answers it.

    Raises:
        RuntimeError: Two methods claim the same path.
    """
    table: dict[str, BoundRoute] = {}
    for attr_name, declared in iter_marked_routes(type(resource)):
        spec = declared
        if advertise is not None and spec.advertise != advertise:
            spec = dataclasses.replace(spec, advertise=advertise)
        handler = getattr(resource, attr_name)
        entry = BoundRoute(
            action=bind_params(handler),
            positional_capacity=max_positional_args(handler),
            spec=spec,
        )
        for path in spec.paths:
            full_path = f"{prefix}/{path}" if prefix else path
            if full_path in table:
                msg = f"Route path {full_path!r} is claimed by both {table[full_path].action.__name__} and {attr_name}"
                raise RuntimeError(msg)
            table[full_path] = entry
    return table


def build_routes_listing(route_table: dict[str, BoundRoute]) -> dict[str, dict[str, Any]]:
    """Build the {route: {doc, args, kwargs}} listing served in 404 responses.

    Only routes that declared themselves advertisable appear. A mounted child is registered with
    advertise=False, so the listing cannot turn the mount into a directory of what is behind it —
    which would undo the boundary while leaving every test passing.

    Depends only on the table's contents, fixed once construction finishes, so it is built once
    there rather than on every 404 (inspect.signature() per route isn't free).

    Args:
        route_table: Path to bound route.

    Returns:
        Route name to its doc, args and kwargs.
    """
    routes = {}
    for endpoint_name, entry in route_table.items():
        if not entry.spec.advertise:
            continue
        wrapped_func = entry.action
        # Get the original function from the wrapper
        original_func = wrapped_func.__wrapped__ if hasattr(wrapped_func, "__wrapped__") else wrapped_func

        # Get function signature
        sig = inspect.signature(original_func)

        # Extract docstring
        doc = original_func.__doc__ or ""

        # Parse arguments
        args = []
        kwargs = {}

        for param_name, param in sig.parameters.items():
            if param_name.startswith("_"):
                continue
            if param_name in ("self", "falcon_response"):
                continue

            param_info = {
                "name": param_name,
                "type": _get_type_name(param.annotation),
            }

            if param.default != inspect.Parameter.empty:
                # It's a keyword argument with default
                kwargs[param_name] = {
                    "type": _get_type_name(param.annotation),
                    "default": param.default,
                }
            else:
                # It's a positional argument
                args.append(param_info)

        routes[endpoint_name] = {
            "doc": doc,
            "args": args,
            "kwargs": kwargs,
        }
    return routes


def iter_marked_routes(cls: type) -> Iterator[tuple[str, RouteSpec]]:
    """Yield the marked methods of a resource class, in a stable order.

    Scans the class, not an instance, so an attribute assigned in `__init__` — a child resource, a
    cache, a session — can never become a route by virtue of being callable.

    Args:
        cls: The resource class to scan.

    Yields:
        Attribute name and the spec its function carries, ordered by attribute name.
    """
    for name in dir(cls):
        spec = getattr(getattr(cls, name, None), ROUTE_SPEC_ATTR, None)
        if isinstance(spec, RouteSpec):
            yield name, spec
