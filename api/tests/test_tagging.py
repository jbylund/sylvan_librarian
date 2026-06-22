"""Tests for tag import module."""

from api.api_resource import APIResource
from api.tag_import import _build_all_ancestors, _build_uuid_to_slug


class TestBuildAllAncestors:
    def test_no_parents_returns_empty(self) -> None:
        tags = [{"id": "u1", "slug": "flying", "parent_ids": []}]
        uuid_to_slug = {"u1": "flying"}
        result = _build_all_ancestors(tags, uuid_to_slug)
        assert result["flying"] == frozenset()

    def test_single_level_parent(self) -> None:
        tags = [
            {"id": "u1", "slug": "evasion", "parent_ids": []},
            {"id": "u2", "slug": "flying", "parent_ids": ["u1"]},
        ]
        uuid_to_slug = {"u1": "evasion", "u2": "flying"}
        result = _build_all_ancestors(tags, uuid_to_slug)
        assert result["flying"] == frozenset({"evasion"})
        assert result["evasion"] == frozenset()

    def test_multi_level_ancestors(self) -> None:
        # dual-land → land-type → permanent
        tags = [
            {"id": "u1", "slug": "permanent", "parent_ids": []},
            {"id": "u2", "slug": "land-type", "parent_ids": ["u1"]},
            {"id": "u3", "slug": "dual-land", "parent_ids": ["u2"]},
        ]
        uuid_to_slug = {"u1": "permanent", "u2": "land-type", "u3": "dual-land"}
        result = _build_all_ancestors(tags, uuid_to_slug)
        assert result["dual-land"] == frozenset({"land-type", "permanent"})
        assert result["land-type"] == frozenset({"permanent"})
        assert result["permanent"] == frozenset()

    def test_cycle_safe(self) -> None:
        # Circular reference should not infinite loop
        tags = [
            {"id": "u1", "slug": "a", "parent_ids": ["u2"]},
            {"id": "u2", "slug": "b", "parent_ids": ["u1"]},
        ]
        uuid_to_slug = {"u1": "a", "u2": "b"}
        result = _build_all_ancestors(tags, uuid_to_slug)
        # Both are ancestors of each other; no crash
        assert "b" in result["a"] or "a" in result["b"]


class TestBuildUuidToSlug:
    def test_maps_id_to_slug(self) -> None:
        tags = [
            {"id": "aaa", "slug": "flying"},
            {"id": "bbb", "slug": "haste"},
        ]
        assert _build_uuid_to_slug(tags) == {"aaa": "flying", "bbb": "haste"}

    def test_empty_list(self) -> None:
        assert _build_uuid_to_slug([]) == {}


class TestAPIResourceEndpoints:
    def test_import_oracle_tags_registered(self) -> None:
        assert hasattr(APIResource, "import_oracle_tags")
        assert callable(APIResource.import_oracle_tags)

    def test_import_art_tags_registered(self) -> None:
        assert hasattr(APIResource, "import_art_tags")
        assert callable(APIResource.import_art_tags)

    def test_old_graphql_methods_removed(self) -> None:
        assert not hasattr(APIResource, "discover_tags_from_scryfall")
        assert not hasattr(APIResource, "discover_tags_from_graphql")
        assert not hasattr(APIResource, "_get_tag_relationships")
        assert not hasattr(APIResource, "_populate_tag_hierarchy")
        assert not hasattr(APIResource, "discover_and_import_all_tags")
        assert not hasattr(APIResource, "update_tagged_cards")
