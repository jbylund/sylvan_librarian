"""Settings module for runtime configuration."""

from __future__ import annotations

import os

# Statement timeout for the prefer-score backfill, in milliseconds. The backfill rescores the
# whole corpus in a single UPDATE, so its runtime scales with disk speed: on a virtualized
# Docker-for-Mac volume it can exceed a limit that real Linux hardware clears comfortably, and
# the import is then abandoned after the upsert has already succeeded (#876). Raise this in
# environments with slow storage.
DEFAULT_PREFER_SCORE_BACKFILL_TIMEOUT_MS = 120_000


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


def _parse_host_allowlist(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated proxy-host allowlist into a set of exact match keys.

    Each entry is trimmed and lowercased; blanks are dropped. The result is compared against an
    incoming header with an exact, whole-string, case-insensitive match -- deliberately NOT a
    suffix match, which is the classic way an allowlist meant for ``cards.example.com`` also lets
    ``evil-cards.example.com`` through. Include the port when the proxy sends one (the header value
    is matched verbatim).

    Args:
        raw: The raw ``TRUSTED_PROXY_HOSTS`` value, or None when unset.

    Returns:
        The lowercased host keys, empty when unset or blank.
    """
    if not raw:
        return frozenset()
    return frozenset(entry.strip().lower() for entry in raw.split(",") if entry.strip())


def _non_negative_int(name: str, default: int) -> int:
    """Read a non-negative integer from the environment, falling back to a default.

    Raises rather than silently substituting the default on a malformed value: these tune
    timeouts, and quietly ignoring a typo means the operator only finds out when the import
    dies at the limit they thought they had raised.

    Args:
        name: Environment variable to read.
        default: Value to use when the variable is unset or empty.

    Returns:
        The parsed value, or default if the variable is unset or empty.

    Raises:
        ValueError: If the variable is set to something that is not a non-negative integer.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        msg = f"{name} must be a non-negative integer, got: {raw!r}"
        raise ValueError(msg) from None
    if value < 0:
        msg = f"{name} must be a non-negative integer, got: {raw!r}"
        raise ValueError(msg)
    return value


class Settings:
    """Simple settings class for runtime configuration."""

    def __init__(self) -> None:
        """Initialize settings from environment variables."""
        self._enable_cache = _is_truthy(os.environ.get("ENABLE_CACHE", "false"))
        self._enable_engine = _is_truthy(os.environ.get("ENABLE_ENGINE", "true"))
        self._shared_cache_path: str = os.environ.get("SHARED_CACHE_PATH", "/tmp/sylvan.cache")  # noqa: S108
        self._prefer_score_backfill_timeout_ms = _non_negative_int(
            "PREFER_SCORE_BACKFILL_TIMEOUT_MS",
            DEFAULT_PREFER_SCORE_BACKFILL_TIMEOUT_MS,
        )
        self._admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
        # Hosts a reverse proxy in front of this service is allowed to name in `X-Proxy-Host` (and,
        # coupled to it, a scheme in `X-Forwarded-Proto` / `Forwarded`). UNSET IS THE DEFAULT and
        # means "no rewriting proxy in front": both headers are then ignored and a self-URL is built
        # from the request's own host and scheme. That is the correct default for a deployment
        # fronted directly (a real hostname, or workers.dev in the sibling port) rather than by a
        # proxy that rewrites the origin. Set this only when such a proxy really exists -- see
        # `_self_base_url` in scryfall_compat/routes.py for why trusting the header unconditionally
        # is a cache-poisoning vector once `/cards/*` answers are cacheable.
        self._trusted_proxy_hosts: frozenset[str] = _parse_host_allowlist(os.environ.get("TRUSTED_PROXY_HOSTS"))

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
        routes every query to SQL and AppContext.reload_engine never runs. Disable via
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

    @property
    def prefer_score_backfill_timeout_ms(self) -> int:
        """Statement timeout for the prefer-score backfill, in milliseconds.

        Override with PREFER_SCORE_BACKFILL_TIMEOUT_MS. 0 disables the timeout entirely.
        """
        return self._prefer_score_backfill_timeout_ms

    @property
    def admin_password(self) -> str:
        """Shared secret gating every route under the admin mount.

        Read from ADMIN_PASSWORD, generated into env.json on first boot by
        scripts/gen_env_json.sh. Empty means unset -- AdminAuthMiddleware treats that as "reject
        everything" rather than "no password required".
        """
        return self._admin_password

    @admin_password.setter
    def admin_password(self, value: str) -> None:
        """Set the admin password, for tests -- no process re-reads ADMIN_PASSWORD after startup."""
        self._admin_password = value

    @property
    def trusted_proxy_hosts(self) -> frozenset[str]:
        """The `X-Proxy-Host` values a reverse proxy in front of this service is allowed to name.

        Read from TRUSTED_PROXY_HOSTS (comma-separated), lowercased for case-insensitive exact
        matching. Empty means "no proxy in front" -- the safe default -- in which case the proxy
        headers are ignored entirely. See `_self_base_url` in scryfall_compat/routes.py.
        """
        return self._trusted_proxy_hosts

    @trusted_proxy_hosts.setter
    def trusted_proxy_hosts(self, value: str | frozenset[str] | set[str] | list[str]) -> None:
        """Set the allowlist, for tests -- no process re-reads TRUSTED_PROXY_HOSTS after startup.

        Accepts either the raw comma-separated string an operator configures or an already-parsed
        iterable of hosts; both are normalized the same way the environment value is. Accepting the
        iterable form keeps `monkeypatch.setattr(settings, "trusted_proxy_hosts", ...)` symmetric
        with the frozenset the getter returns, so its restore does not re-split a set into letters.
        """
        if isinstance(value, str):
            self._trusted_proxy_hosts = _parse_host_allowlist(value)
        else:
            self._trusted_proxy_hosts = frozenset(entry.strip().lower() for entry in value if entry.strip())


# Global settings instance
settings = Settings()
