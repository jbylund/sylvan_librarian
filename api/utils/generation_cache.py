"""Generation-aware cache proxy for cross-process cache invalidation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cachebox import LRUCache

if TYPE_CHECKING:
    from multiprocessing.sharedctypes import Synchronized


class GenerationCache:
    """Cache proxy that auto-invalidates across workers when a shared generation counter advances.

    Wraps an LRUCache(maxsize=1) as a map from generation → inner cache.
    When the generation advances, the next access creates a fresh inner cache
    and the LRU evicts the old one, freeing its memory.
    """

    def __init__(self, factory: Any, generation: Synchronized) -> None:  # noqa: ANN401
        """Initialize with a cache factory callable and a shared generation counter."""
        self._factory = factory
        self._generation = generation
        self._map: LRUCache = LRUCache(maxsize=1)

    def _current(self) -> Any:  # noqa: ANN401
        gen = self._generation.value
        try:
            return self._map[gen]
        except KeyError:
            cache = self._factory()
            self._map[gen] = cache
            return cache

    def __getitem__(self, key: Any) -> Any:  # noqa: ANN401
        """Return item from the current generation's cache."""
        return self._current()[key]

    def __setitem__(self, key: Any, value: Any) -> None:  # noqa: ANN401
        """Store item in the current generation's cache."""
        self._current()[key] = value

    def __contains__(self, key: object) -> bool:
        """Return whether key exists in the current generation's cache."""
        return key in self._current()

    def get(self, key: Any, default: Any = None) -> Any:  # noqa: ANN401
        """Return item or default if not present in the current generation's cache."""
        try:
            return self[key]
        except KeyError:
            return default

    def clear(self) -> None:
        """Clear the current generation's inner cache."""
        self._current().clear()
