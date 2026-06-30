"""Settings module for runtime configuration."""

from __future__ import annotations

import os


def _is_truthy(value: str | None) -> bool:
    """Check if a string value is truthy.

    Args:
        value: String value to check

    Returns:
        True if value is "true", "1", or "yes" (case-insensitive), False otherwise
    """
    if value is None:
        return False
    return value.lower() in ("true", "1", "yes")


class Settings:
    """Simple settings class for runtime configuration."""

    def __init__(self) -> None:
        """Initialize settings from environment variables."""
        self._enable_cache = _is_truthy(os.environ.get("ENABLE_CACHE", "false"))
        self._enable_engine = _is_truthy(os.environ.get("ENABLE_ENGINE", "true"))
        self._shared_cache_path: str = os.environ.get("SHARED_CACHE_PATH", "/tmp/sylvan.cache")  # noqa: S108

    @property
    def enable_cache(self) -> bool:
        """Check if caching is enabled."""
        return self._enable_cache

    @enable_cache.setter
    def enable_cache(self, value: bool) -> None:
        """Set caching enabled state."""
        self._enable_cache = value

    @property
    def enable_engine(self) -> bool:
        """Check if the Rust card filter engine serves searches.

        Enabled by default. When disabled, the engine is fully inert: _search
        routes every query to SQL and _reload_engine never runs. Disable via
        ENABLE_ENGINE=false for environments where the full-table fetch cost is
        unacceptable (e.g. low-memory dev machines).
        """
        return self._enable_engine

    @enable_engine.setter
    def enable_engine(self, value: bool) -> None:
        """Set engine enabled state."""
        self._enable_engine = value

    @property
    def shared_cache_path(self) -> str:
        """Filesystem path for the shared mmap cache file."""
        return self._shared_cache_path


# Global settings instance
settings = Settings()
