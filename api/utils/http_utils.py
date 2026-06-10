"""HTTP helpers shared across outbound clients."""

from __future__ import annotations

import datetime


def make_user_agent() -> str:
    """Identifying User-Agent for outbound HTTP requests (Scryfall rejects HTTP-library defaults)."""
    version = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d")
    return f"magic-api/{version}"
