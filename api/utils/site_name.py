"""Derive a human display name from the Host header a request arrived on.

The site is deployed under more than one hostname, and each should title itself after the host the
visitor used rather than a hardcoded name. `sylvan-librarian.com` becomes "Sylvan Librarian".

The interesting part is hostnames with no separator. `tolarianacademy.com` has nothing to split on, so
the name is segmented against the system dictionary — which is why the result is cached and why the
lookup degrades to the raw hostname on systems with no word list rather than failing.

Input is a request header, so it is treated as untrusted: length-capped, matched against an allowlist
of hostname characters, and rejected outright for bare IPs and localhost, all of which fall back to
FALLBACK_SITE_NAME.
"""

from __future__ import annotations

import pathlib
import re
import urllib.parse
from functools import lru_cache

FALLBACK_SITE_NAME = "MTG Search"


# TLDs in this set are stripped from the hostname; others are concatenated into the word.
# e.g. sylvan-librarian.com -> "Sylvan Librarian"; tolarian-acade.my -> "Tolarian Academy"
_STRIP_TLDS = frozenset(["app", "biz", "co", "com", "dev", "edu", "gov", "info", "io", "me", "net", "org", "us"])


# Allowlist: only valid hostname characters (letters, digits, hyphens, dots) after urlparse extracts the host.
_SAFE_HOSTNAME_RE = re.compile(r"^[a-z0-9.\-]+$")

_IP_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

_MIN_WORD_LEN = 3


# TODO: supplement with words from https://api.scryfall.com/catalog/word-bank so MtG-specific
# terms (e.g. "sylvan", "planeswalker") are recognized even on systems without a full dictionary.
def _load_word_set() -> frozenset[str]:
    for path in ("/usr/share/dict/american-english", "/usr/share/dict/words"):
        try:
            with pathlib.Path(path).open() as f:
                return frozenset(w.lower() for w in f.read().splitlines() if w.isalpha() and len(w) >= _MIN_WORD_LEN)
        except OSError:
            continue
    return frozenset()


_WORDS: frozenset[str] = _load_word_set()


def _split_words(s: str, words: frozenset[str]) -> list[str] | None:
    """Split s into dictionary words by searching split points from the middle outward.

    Returns a list of words on success, or None if the string cannot be fully partitioned.
    """
    # Only attempt dictionary splitting for purely alphabetic strings.
    if not s.isalpha():
        return None

    @lru_cache(maxsize=4096)
    def _split(sub: str) -> tuple[str, ...] | None:
        if sub in words:
            return (sub,)
        n = len(sub)
        if n < _MIN_WORD_LEN:
            return None
        mid = n // 2
        for k in sorted(range(1, n), key=lambda k: abs(k - mid)):
            left, right = sub[:k], sub[k:]
            if left in words:
                rest = _split(right)
                if rest is not None:
                    return (left, *rest)
            if right in words:
                rest = _split(left)
                if rest is not None:
                    return (*rest, right)
        return None

    res = _split(s)
    return list(res) if res is not None else None


def hostname_to_site_name(raw_host: str) -> str:
    """Derive a display name from a Host header value, falling back to FALLBACK_SITE_NAME."""
    # urlparse requires a scheme; .hostname strips the port and lowercases.
    hostname = urllib.parse.urlparse(f"http://{raw_host}").hostname or ""
    return _hostname_to_site_name(hostname[:64])


@lru_cache(maxsize=256)
def _hostname_to_site_name(hostname: str) -> str:
    if not hostname or hostname == "localhost" or _IP_RE.match(hostname) or not _SAFE_HOSTNAME_RE.match(hostname):
        return FALLBACK_SITE_NAME
    parts = hostname.split(".")[-2:]
    tld = parts[-1].lower()
    name = parts[0] if tld in _STRIP_TLDS else ".".join(parts)
    name = name.replace(".", "").replace("-", " ").strip()
    if " " not in name and _WORDS:
        split = _split_words(name, _WORDS)
        if split is not None:
            name = " ".join(split)
    name = name.title()
    return name if any(c.isalnum() for c in name) else FALLBACK_SITE_NAME
