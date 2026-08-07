"""A cache decorator that honours the runtime enable_cache setting.

`cachebox.cached` binds its decision at decoration time, which happens at import. This wraps it so
the setting is consulted per call instead, letting a deployment or a test turn caching off without
reimporting anything. The cached function is built either way, so flipping the setting back on does
not have to rebuild it.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

import cachebox
from cachebox import cached as cachebox_cached

from api.settings import settings


def cached(cache: Any, key: Any = None) -> Any:  # noqa: ANN401
    """Decorator that respects the settings.enable_cache flag at runtime.

    Always creates the cached function, but checks settings at call time
    to determine whether to use the cache or call the original function.
    """
    key_maker = key or cachebox.make_hash_key

    def decorator(func: Any) -> Any:  # noqa: ANN401
        cached_func = cachebox_cached(cache, key_maker=key_maker)(func)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if settings.enable_cache:
                return cached_func(*args, **kwargs)
            return func(*args, **kwargs)

        # Copy attributes from cached_func for compatibility
        wrapper.cache = cache  # type: ignore[attr-defined]
        return wrapper

    return decorator
