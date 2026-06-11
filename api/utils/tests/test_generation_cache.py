"""Tests for GenerationCache."""

from __future__ import annotations

import multiprocessing

import pytest

from api.utils.generation_cache import GenerationCache


@pytest.fixture(name="generation")
def generation_fixture() -> multiprocessing.Value:
    return multiprocessing.Value("i", 0, lock=True)


@pytest.fixture(name="cache")
def cache_fixture(generation: multiprocessing.Value) -> GenerationCache:
    return GenerationCache(factory=dict, generation=generation)


class TestGenerationCacheBasicAccess:
    def test_set_and_get(self, cache: GenerationCache) -> None:
        cache["key"] = "value"
        assert cache["key"] == "value"

    def test_contains_true(self, cache: GenerationCache) -> None:
        cache["key"] = "value"
        assert "key" in cache

    def test_contains_false(self, cache: GenerationCache) -> None:
        assert "missing" not in cache

    def test_get_present(self, cache: GenerationCache) -> None:
        cache["key"] = "value"
        assert cache.get("key") == "value"

    def test_get_missing_returns_default(self, cache: GenerationCache) -> None:
        assert cache.get("missing") is None
        assert cache.get("missing", "fallback") == "fallback"

    def test_getitem_missing_raises(self, cache: GenerationCache) -> None:
        with pytest.raises(KeyError):
            _ = cache["missing"]

    def test_clear_removes_keys(self, cache: GenerationCache) -> None:
        cache["key"] = "value"
        cache.clear()
        assert "key" not in cache


class TestGenerationCacheInvalidation:
    def test_value_inaccessible_after_generation_advances(self, cache: GenerationCache, generation: multiprocessing.Value) -> None:
        cache["key"] = "value"
        assert "key" in cache

        generation.value += 1

        assert "key" not in cache

    def test_new_generation_starts_empty(self, cache: GenerationCache, generation: multiprocessing.Value) -> None:
        cache["key"] = "value"
        generation.value += 1

        with pytest.raises(KeyError):
            _ = cache["key"]

    def test_independent_values_per_generation(self, cache: GenerationCache, generation: multiprocessing.Value) -> None:
        cache["key"] = "gen0"
        generation.value += 1
        cache["key"] = "gen1"

        assert cache["key"] == "gen1"

        generation.value -= 1
        assert cache.get("key") is None  # gen0 cache was evicted by LRU(maxsize=1)

    def test_factory_called_once_per_generation(self, generation: multiprocessing.Value) -> None:
        call_count = 0

        def counting_factory() -> dict:
            nonlocal call_count
            call_count += 1
            return {}

        cache = GenerationCache(factory=counting_factory, generation=generation)

        cache["a"] = 1
        cache["b"] = 2
        _ = cache["a"]

        assert call_count == 1  # one cache created for gen 0

        generation.value += 1
        cache["c"] = 3

        assert call_count == 2  # new cache created for gen 1

    def test_generation_advance_evicts_old_inner_cache(self, generation: multiprocessing.Value) -> None:
        # Populate gen 0, advance to gen 1, then go back — old cache should be gone (LRU evicted it)
        cache = GenerationCache(factory=dict, generation=generation)
        cache["key"] = "gen0_value"

        generation.value += 1
        cache["other"] = "gen1_value"  # causes LRU to evict gen0's cache

        generation.value -= 1
        assert "key" not in cache  # gen0 cache was evicted
