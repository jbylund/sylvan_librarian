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
        self._use_sqlglot = _is_truthy(os.environ.get("USE_SQLGLOT_SEARCH", "false"))

    @property
    def enable_cache(self) -> bool:
        """Check if caching is enabled."""
        return self._enable_cache

    @enable_cache.setter
    def enable_cache(self, value: bool) -> None:
        """Set caching enabled state."""
        self._enable_cache = value

    @property
    def use_sqlglot(self) -> bool:
        """Check if sqlglot-based SQL generation is enabled."""
        return self._use_sqlglot

    @use_sqlglot.setter
    def use_sqlglot(self, value: bool) -> None:
        """Set sqlglot SQL generation enabled state."""
        self._use_sqlglot = value


# Global settings instance
settings = Settings()
