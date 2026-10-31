"""Tests for tag import module."""

from api.api_resource import APIResource
from api.tag_import import _build_all_ancestors, _build_slug_to_aliases, _build_tag_keys, _build_uuid_to_slug


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


class TestBuildSlugToAliases:
    def test_aliases_are_slugified(self) -> None:
        # The real shape: `loose-lips` carries a spaced alias Scryfall resolves for art:"open mouth".
        tags = [{"id": "u1", "slug": "loose-lips", "aliases": ["open mouth", "mouth open"]}]
        assert _build_slug_to_aliases(tags) == {"loose-lips": frozenset({"open-mouth", "mouth-open"})}

    def test_tags_without_aliases_are_absent(self) -> None:
        tags = [{"id": "u1", "slug": "fire"}, {"id": "u2", "slug": "flying", "aliases": []}]
        assert _build_slug_to_aliases(tags) == {}

    def test_alias_colliding_with_a_slug_is_dropped(self) -> None:
        # An alias that is also a real tag's slug would silently merge two tags; the slug wins.
        tags = [
            {"id": "u1", "slug": "fire", "aliases": ["flying"]},
            {"id": "u2", "slug": "flying", "aliases": []},
        ]
        assert _build_slug_to_aliases(tags) == {}

    def test_alias_claimed_by_two_tags_is_dropped(self) -> None:
        tags = [
            {"id": "u1", "slug": "fire", "aliases": ["open flame"]},
            {"id": "u2", "slug": "campfire", "aliases": ["open-flame"]},
        ]
        assert _build_slug_to_aliases(tags) == {}

    def test_alias_repeated_by_one_tag_is_kept(self) -> None:
        # Two spellings of one alias collapse to the same slug -- not a conflict.
        tags = [{"id": "u1", "slug": "loose-lips", "aliases": ["open mouth", "Open Mouth"]}]
        assert _build_slug_to_aliases(tags) == {"loose-lips": frozenset({"open-mouth"})}


class TestBuildTagKeys:
    def test_slug_ancestors_and_their_aliases(self) -> None:
        # `flames` is an alias of `fire`, which has `campfire` under it. Scryfall resolves the
        # alias before expanding the hierarchy, so art:flames must reach the descendant too.
        tags = [
            {"id": "u1", "slug": "fire", "parent_ids": [], "aliases": ["flames"]},
            {"id": "u2", "slug": "campfire", "parent_ids": ["u1"], "aliases": ["camp fire"]},
        ]
        uuid_to_slug = {"u1": "fire", "u2": "campfire"}
        result = _build_tag_keys(tags, uuid_to_slug)
        assert result["fire"] == frozenset({"fire", "flames"})
        assert result["campfire"] == frozenset({"campfire", "camp-fire", "fire", "flames"})

    def test_no_aliases_is_slug_plus_ancestors(self) -> None:
        tags = [
            {"id": "u1", "slug": "permanent", "parent_ids": []},
            {"id": "u2", "slug": "land-type", "parent_ids": ["u1"]},
        ]
        uuid_to_slug = {"u1": "permanent", "u2": "land-type"}
        result = _build_tag_keys(tags, uuid_to_slug)
        assert result["land-type"] == frozenset({"land-type", "permanent"})
        assert result["permanent"] == frozenset({"permanent"})


class TestAPIResourceEndpoints:
    def test_old_graphql_methods_removed(self) -> None:
        assert not hasattr(APIResource, "discover_tags_from_scryfall")
        assert not hasattr(APIResource, "discover_tags_from_graphql")
        assert not hasattr(APIResource, "_get_tag_relationships")
        assert not hasattr(APIResource, "_populate_tag_hierarchy")
        assert not hasattr(APIResource, "discover_and_import_all_tags")
        assert not hasattr(APIResource, "update_tagged_cards")
