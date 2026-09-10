"""What a search-shaped handler tells the response-side middlewares about its result.

CachingMiddleware and QueryLogMiddleware both record a per-request result count and, for the query
log, the time the search itself took. Both used to derive those from the response media alone, and
both derivations were wrong for one path each: `len(media["cards"])` is the number of *fields* once
`shape=columnar` has inverted the rows, and the SQL path's `inner_timings["_children"]` subtree does
not exist on the engine path, which times its query as a top-level `engine_query` span.

The handler knows the row count before it reshapes, so it stashes it on `resp.context`; the readers
here fall back to the media for a response that did not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import falcon

RESULT_COUNT_ATTR = "result_count"


def note_result_count(resp: falcon.Response | None, count: int) -> None:
    """Record the number of result rows a handler is returning, before any reshaping of `cards`."""
    if resp is not None:
        setattr(resp.context, RESULT_COUNT_ATTR, count)


def result_count_of(resp: falcon.Response, media: dict) -> int:
    """The row count the handler stashed, else the length of `cards` in the media."""
    count = getattr(getattr(resp, "context", None), RESULT_COUNT_ATTR, None)
    if isinstance(count, int):
        return count
    return len(media.get("cards") or [])


def span_duration_ms(node: object) -> float | None:
    """The `duration_ms` of one `Timer` node, or None if the node is absent or not a timer node."""
    if not isinstance(node, dict):
        return None
    meta = node.get("_meta")
    if not isinstance(meta, dict):
        return None
    return meta.get("duration_ms")


def search_execute_ms(inner_timings: object) -> float | None:
    """How long the search itself took, whichever path served it.

    The engine path times its query as a top-level `engine_query` span; the SQL path nests
    `execute_query` under the root's `_children`. The old reader knew only the SQL shape, so
    execute_ms was NULL for every engine-served search -- the large majority of them.
    """
    if not isinstance(inner_timings, dict):
        return None
    engine_ms = span_duration_ms(inner_timings.get("engine_query"))
    if engine_ms is not None:
        return engine_ms
    children = inner_timings.get("_children")
    if not isinstance(children, dict):
        return None
    return span_duration_ms(children.get("execute_query"))


def search_fetch_ms(inner_timings: object) -> float | None:
    """The SQL path's fetch span; the engine path has no separate fetch, so None there."""
    if not isinstance(inner_timings, dict):
        return None
    children = inner_timings.get("_children")
    if not isinstance(children, dict):
        return None
    return span_duration_ms(children.get("fetch_results"))
