"""HTTP helpers shared across outbound clients."""

from __future__ import annotations

import datetime as dt


def make_user_agent() -> str:
    """Identifying User-Agent for outbound HTTP requests (Scryfall rejects HTTP-library defaults)."""
    version = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%d")
    return f"magic-api/{version}"
