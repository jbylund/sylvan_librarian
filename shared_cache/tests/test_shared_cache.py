"""Unit tests for the shared_cache Rust extension.

Build the wheel first:
    cd shared_cache && maturin develop
"""

from __future__ import annotations

import multiprocessing
import time
from collections import namedtuple

import pytest

sc = pytest.importorskip("shared_cache")
SharedCache = sc.SharedCache

CachedResponse = namedtuple(
    "CachedResponse",
    ["status", "headers", "body", "result_count", "total_cards"],
)

SAMPLE = CachedResponse(
    status="200 OK",
    headers=[("content-type", "application/json"), ("content-encoding", "gzip")],
    body=b'[{"name": "Lightning Bolt"}]',
    result_count=1,
    total_cards=75321,
)

KEY = b"/search?q=lightning+bolt"
OTHER_KEY = b"/search?q=counterspell"


def _make_cache(path, maxsize=1000, n_pages=2, default_ttl=None) -> SharedCache:
    return SharedCache(path=str(path), maxsize=maxsize, n_pages=n_pages, default_ttl=default_ttl)


# Module-level helpers required by cross-process tests (must be picklable).


def _write_to_cache(path, key, response) -> None:
    SharedCache(path=path, maxsize=1000, n_pages=2)[key] = response


def _read_from_cache(path, key, queue) -> None:
    result = SharedCache(path=path, maxsize=1000, n_pages=2).get(key)
    queue.put(None if result is None else (result.body, result.result_count))


def _invalidate_cache(path) -> None:
    SharedCache(path=path, maxsize=1000, n_pages=2).invalidate()


class TestGetSet:
    def test_miss_returns_none(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        assert cache.get(KEY) is None

    def test_roundtrip_all_fields(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        result = cache.get(KEY)
        assert result is not None
        assert result.status == "200 OK"
        assert result.body == SAMPLE.body
        assert result.result_count == 1
        assert result.total_cards == 75321

    def test_headers_preserved(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        assert list(cache.get(KEY).headers) == [
            ("content-type", "application/json"),
            ("content-encoding", "gzip"),
        ]

    def test_none_body_roundtrip(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE._replace(body=None)
        assert cache.get(KEY).body is None

    def test_none_counts_roundtrip(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE._replace(result_count=None, total_cards=None)
        result = cache.get(KEY)
        assert result.result_count is None
        assert result.total_cards is None

    def test_getitem_raises_on_miss(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        with pytest.raises(KeyError):
            _ = cache[KEY]

    def test_getitem_returns_on_hit(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        assert cache[KEY].status == "200 OK"

    def test_two_distinct_keys_independent(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE._replace(result_count=1)
        cache[OTHER_KEY] = SAMPLE._replace(result_count=2)
        assert cache.get(KEY).result_count == 1
        assert cache.get(OTHER_KEY).result_count == 2


class TestContainsAndLen:
    def test_contains_hit(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        assert KEY in cache

    def test_contains_miss(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        assert KEY not in cache

    def test_len_starts_at_zero(self, tmp_path):
        assert len(_make_cache(tmp_path / "c.cache")) == 0

    def test_len_increments_on_insert(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        assert len(cache) == 1

    def test_len_stable_on_overwrite(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        cache[KEY] = SAMPLE
        assert len(cache) == 1

    def test_multiple_keys_counted(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        for i in range(5):
            cache[f"k{i}".encode()] = SAMPLE
        assert len(cache) == 5


class TestPopAndInvalidate:
    def test_pop_removes_entry(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        assert cache.pop(KEY) is True
        assert cache.get(KEY) is None

    def test_pop_returns_false_on_miss(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        assert cache.pop(KEY) is False

    def test_invalidate_clears_all(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        for i in range(10):
            cache[f"k{i}".encode()] = SAMPLE
        cache.invalidate()
        assert len(cache) == 0
        assert cache.get(b"k0") is None

    def test_cache_usable_after_invalidate(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        cache.invalidate()
        cache[KEY] = SAMPLE
        assert cache.get(KEY) is not None


class TestTTL:
    def test_entry_available_before_expiry(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache.set(KEY, SAMPLE, ttl=10.0)
        assert cache.get(KEY) is not None

    def test_entry_expired_after_ttl(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache.set(KEY, SAMPLE, ttl=0.01)
        time.sleep(0.05)
        assert cache.get(KEY) is None

    def test_default_ttl_applied(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache", default_ttl=10.0)
        cache[KEY] = SAMPLE
        assert cache.get(KEY) is not None


class TestOverwrite:
    def test_same_content_fast_path(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        cache[KEY] = SAMPLE
        cache[KEY] = SAMPLE
        assert cache.get(KEY).body == SAMPLE.body

    def test_different_content_overwrites(self, tmp_path):
        cache = _make_cache(tmp_path / "c.cache")
        r1 = SAMPLE._replace(body=b"original", result_count=1)
        r2 = SAMPLE._replace(body=b"updated", result_count=2)
        cache[KEY] = r1
        assert cache.get(KEY).body == b"original"
        cache[KEY] = r2
        result = cache.get(KEY)
        assert result.body == b"updated"
        assert result.result_count == 2


class TestCrossProcess:
    def test_entry_visible_across_processes(self, tmp_path):
        path = str(tmp_path / "shared.cache")
        q = multiprocessing.Queue()

        p = multiprocessing.Process(target=_write_to_cache, args=(path, KEY, SAMPLE))
        p.start()
        p.join()

        p = multiprocessing.Process(target=_read_from_cache, args=(path, KEY, q))
        p.start()
        p.join()

        result = q.get(timeout=5)
        assert result is not None
        assert result[0] == SAMPLE.body
        assert result[1] == SAMPLE.result_count

    def test_invalidate_visible_across_processes(self, tmp_path):
        path = str(tmp_path / "shared.cache")
        q = multiprocessing.Queue()

        for fn, args in [
            (_write_to_cache, (path, KEY, SAMPLE)),
            (_invalidate_cache, (path,)),
        ]:
            p = multiprocessing.Process(target=fn, args=args)
            p.start()
            p.join()

        p = multiprocessing.Process(target=_read_from_cache, args=(path, KEY, q))
        p.start()
        p.join()

        assert q.get(timeout=5) is None


class TestRotation:
    def test_cache_survives_many_rotations(self, tmp_path):
        # gen_maxsize = 10 // 2 = 5; triggers multiple rotations
        cache = _make_cache(tmp_path / "c.cache", maxsize=10, n_pages=2)
        for i in range(40):
            cache[f"k{i}".encode()] = SAMPLE._replace(result_count=i)
        for i in range(37, 40):
            result = cache.get(f"k{i}".encode())
            assert result is not None
            assert result.result_count == i

    def test_hot_entries_survive_rotation(self, tmp_path):
        # gen_maxsize = 9 // 3 = 3.
        #
        # Rotation sequence:
        #   Insert hot + filler_{0,1} → page 0 at threshold (3 entries).
        #   Insert filler_2           → rotation 1: page 0 sealed, page 1 active.
        #   get(hot)                  → visited bit set on sealed page 0.
        #   Insert filler_{3,4}       → page 1 at threshold.
        #   Insert filler_5           → rotation 2: page 1 sealed, page 2 active.
        #   Insert filler_{6,7}       → page 2 at threshold.
        #   Insert filler_8           → rotation 3: page 0 recycled; hot (visited=1) survives.
        cache = _make_cache(tmp_path / "c.cache", maxsize=9, n_pages=3)
        hot_key = b"hot"

        fk = lambda i: f"filler_{i}".encode()  # noqa: E731

        cache[hot_key] = SAMPLE._replace(body=b"hot-data", result_count=999)
        cache[fk(0)] = SAMPLE
        cache[fk(1)] = SAMPLE
        cache[fk(2)] = SAMPLE  # triggers rotation 1

        assert cache.get(hot_key) is not None  # sets visited bit

        cache[fk(3)] = SAMPLE
        cache[fk(4)] = SAMPLE
        cache[fk(5)] = SAMPLE  # triggers rotation 2

        cache[fk(6)] = SAMPLE
        cache[fk(7)] = SAMPLE
        cache[fk(8)] = SAMPLE  # triggers rotation 3; hot survives

        result = cache.get(hot_key)
        assert result is not None
        assert result.body == b"hot-data"

        # filler_0 and filler_1 were never accessed while sealed → evicted
        assert cache.get(fk(0)) is None
        assert cache.get(fk(1)) is None
