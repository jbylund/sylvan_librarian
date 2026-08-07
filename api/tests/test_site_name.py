"""Tests for deriving a display name from the Host header."""

from __future__ import annotations

import pytest

from api.utils.site_name import FALLBACK_SITE_NAME, _hostname_to_site_name, _split_words, hostname_to_site_name

_HOSTNAME_TESTCASES = {
    "tolarian_acade_my": {
        "expected": "Tolarian Academy",
        "raw_host": "tolarian-acade.my",
    },
    "strips_com_tld": {
        "expected": "Sylvan Librarian",
        "raw_host": "sylvan-librarian.com",
    },
    "strips_port": {
        "expected": "Sylvan Librarian",
        "raw_host": "sylvan-librarian.com:443",
    },
    "strips_www_prefix": {
        "expected": "Sylvan Librarian",
        "raw_host": "www.sylvan-librarian.com",
    },
    "strips_subdomain_com": {
        "expected": "Sylvan Librarian",
        "raw_host": "foo.sylvan-librarian.com",
    },
    "strips_subdomain_non_strip_tld": {
        "expected": "Tolarian Academy",
        "raw_host": "foo.tolarian-acade.my",
    },
    "localhost_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "localhost",
    },
    "localhost_with_port_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "localhost:8080",
    },
    "ip_address_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "192.168.1.1",
    },
    "ip_with_port_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "192.168.1.1:5000",
    },
    "empty_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "",
    },
    "invalid_chars_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": 'evil"><script>.com',
    },
    "all_hyphens_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "----",
    },
    "all_dots_returns_fallback": {
        "expected": FALLBACK_SITE_NAME,
        "raw_host": "...",
    },
}


_SPLIT_WORDS_TESTCASES: dict[str, dict] = {
    "whole_word": {
        "s": "apple",
        "expected": ["apple"],
    },
    "two_words": {
        "s": "applepie",
        "expected": ["apple", "pie"],
    },
    "three_words": {
        "s": "applebananacherry",
        "expected": ["apple", "banana", "cherry"],
    },
    "no_split_possible": {
        "s": "xyzqwerty",
        "expected": None,
    },
    "split_prefers_middle": {
        # "the" (len 3) at position 0 and "oak" (len 3) at end; "lion" in the middle is found first
        "s": "thelionoak",
        "expected": ["the", "lion", "oak"],
    },
    "prefers_fewest_words": {
        # Two valid splits: ["abcde", "fghij"] (k=5, center) and ["abc", "de", "fghij"] (k=3).
        # Middle-out tries k=5 first and commits to the 2-word split.
        "s": "abcdefghij",
        "expected": ["abcde", "fghij"],
    },
}

_SMALL_WORDS: frozenset[str] = frozenset(
    ["apple", "pie", "banana", "cherry", "the", "lion", "oak", "abcde", "fghij", "abc", "de", "sylvan", "librarian"]
)


class TestSplitWords:
    """Tests for _split_words() using a controlled word set."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(_SPLIT_WORDS_TESTCASES.values()))),
        argvalues=[[v for k, v in sorted(_SPLIT_WORDS_TESTCASES[name].items())] for name in sorted(_SPLIT_WORDS_TESTCASES)],
        ids=sorted(_SPLIT_WORDS_TESTCASES),
    )
    def test_split_words(self, expected: list[str] | None, s: str) -> None:
        assert _split_words(s, _SMALL_WORDS) == expected


_HOSTNAME_DICT_TESTCASES: dict[str, dict] = {
    "no_dash_splits_into_words": {
        "expected": "Sylvan Librarian",
        "raw_host": "sylvanlibrarian.com",
    },
}


class TestHostnameSiteNameWithDict:
    """Tests for hostname_to_site_name() with a controlled word set patched in."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(_HOSTNAME_DICT_TESTCASES.values()))),
        argvalues=[[v for k, v in sorted(_HOSTNAME_DICT_TESTCASES[name].items())] for name in sorted(_HOSTNAME_DICT_TESTCASES)],
        ids=sorted(_HOSTNAME_DICT_TESTCASES),
    )
    def test_hostname_to_site_name_with_dict(self, monkeypatch: pytest.MonkeyPatch, expected: str, raw_host: str) -> None:
        monkeypatch.setattr("api.utils.site_name._WORDS", _SMALL_WORDS)
        _hostname_to_site_name.cache_clear()
        assert hostname_to_site_name(raw_host) == expected


class TestHostnameSiteName:
    """Tests for hostname_to_site_name()."""

    @pytest.mark.parametrize(
        argnames=sorted(next(iter(_HOSTNAME_TESTCASES.values()))),
        argvalues=[[v for k, v in sorted(_HOSTNAME_TESTCASES[name].items())] for name in sorted(_HOSTNAME_TESTCASES)],
        ids=sorted(_HOSTNAME_TESTCASES),
    )
    def test_hostname_to_site_name(self, expected: str, raw_host: str) -> None:
        assert hostname_to_site_name(raw_host) == expected
