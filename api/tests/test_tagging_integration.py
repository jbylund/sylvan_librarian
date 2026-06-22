"""Integration tests for tag import functions."""

from unittest.mock import MagicMock, patch

from api.scryfall_bulk_data_fetcher import BulkDataKey
from api.tag_import import _build_uuid_to_slug, import_art_tags, import_oracle_tags

ORACLE_TAGS_FIXTURE = [
    {
        "id": "uuid-flying",
        "slug": "flying",
        "parent_ids": [],
        "child_ids": ["uuid-evasion"],
        "taggings": [
            {"oracle_id": "card-a", "weight": "strong"},
            {"oracle_id": "card-b", "weight": "median"},
        ],
    },
    {
        "id": "uuid-evasion",
        "slug": "evasion",
        "parent_ids": ["uuid-flying"],
        "child_ids": [],
        "taggings": [
            {"oracle_id": "card-a", "weight": "strong"},
        ],
    },
]

ART_TAGS_FIXTURE = [
    {
        "id": "uuid-dragon",
        "slug": "dragon",
        "parent_ids": [],
        "child_ids": [],
        "taggings": [
            {"illustration_id": "illus-x", "weight": "very_strong"},
        ],
    },
]


def _make_mock_conn_pool(tagged_in_db: list[dict] | None = None) -> tuple[MagicMock, MagicMock]:
    """Return a mock conn_pool whose cursor.fetchall() yields the given rows."""
    cursor = MagicMock()
    cursor.__enter__ = lambda _: cursor
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall.return_value = tagged_in_db or []
    cursor.rowcount = 0

    conn = MagicMock()
    conn.__enter__ = lambda _: conn
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    pool = MagicMock()
    pool.connection.return_value.__enter__ = lambda _: conn
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool, cursor


class TestBuildUuidToSlug:
    def test_basic(self) -> None:
        assert _build_uuid_to_slug(ORACLE_TAGS_FIXTURE) == {
            "uuid-flying": "flying",
            "uuid-evasion": "evasion",
        }


class TestImportOracleTags:
    def test_calls_stream_for_oracle_tags(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ORACLE_TAGS_FIXTURE)

        import_oracle_tags(pool, fetcher)

        fetcher.stream_data_for_key.assert_called_once_with(BulkDataKey.ORACLE_TAGS)

    def test_returns_summary(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ORACLE_TAGS_FIXTURE)

        result = import_oracle_tags(pool, fetcher)

        assert result["tags_imported"] == 2
        assert result["cards_with_tags"] == 2  # card-a and card-b
        assert "duration_seconds" in result


class TestAncestorPropagation:
    """Ancestor slugs must be added to each card's tag set at import time.

    The SQL filter (card_oracle_tags @> {'dual-land': True}) only matches cards that carry the
    slug directly.  Without ancestor propagation a card tagged only with 'cycle-abu-dual-land'
    (a child of 'dual-land') would be invisible to an otag:dual-land query.
    """

    def test_child_tagged_card_gets_parent_slug(self) -> None:
        # card-c is only tagged with evasion (child of flying).
        # After import it must also carry 'flying' so otag:flying finds it.
        fixture = [
            {"id": "uuid-flying", "slug": "flying", "parent_ids": [], "child_ids": ["uuid-evasion"], "taggings": []},
            {
                "id": "uuid-evasion",
                "slug": "evasion",
                "parent_ids": ["uuid-flying"],
                "child_ids": [],
                "taggings": [{"oracle_id": "card-c", "weight": "strong"}],
            },
        ]
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(fixture)

        captured: dict = {}

        def capture(conn, id_column, tag_column, id_to_tags) -> tuple[int, int]:
            captured["id_to_tags"] = id_to_tags
            return (0, 0)

        with patch("api.tag_import._sync_card_tags", side_effect=capture), patch("api.tag_import._sync_hierarchy"):
            import_oracle_tags(pool, fetcher)

        card_tags = captured["id_to_tags"]["card-c"]
        assert card_tags.get("evasion") is True
        assert card_tags.get("flying") is True  # ancestor propagated

    def test_direct_tagged_card_unaffected(self) -> None:
        # card-b is only tagged with the root tag flying (no parent).
        # It should have exactly flying and nothing else.
        fixture = [
            {
                "id": "uuid-flying",
                "slug": "flying",
                "parent_ids": [],
                "child_ids": [],
                "taggings": [{"oracle_id": "card-b", "weight": "median"}],
            },
        ]
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(fixture)

        captured: dict = {}

        def capture(conn, id_column, tag_column, id_to_tags) -> tuple[int, int]:
            captured["id_to_tags"] = id_to_tags
            return (0, 0)

        with patch("api.tag_import._sync_card_tags", side_effect=capture), patch("api.tag_import._sync_hierarchy"):
            import_oracle_tags(pool, fetcher)

        assert captured["id_to_tags"]["card-b"] == {"flying": True}


class TestImportArtTags:
    def test_calls_stream_for_art_tags(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ART_TAGS_FIXTURE)

        import_art_tags(pool, fetcher)

        fetcher.stream_data_for_key.assert_called_once_with(BulkDataKey.ART_TAGS)

    def test_returns_summary(self) -> None:
        pool, _ = _make_mock_conn_pool()
        fetcher = MagicMock()
        fetcher.stream_data_for_key.return_value = iter(ART_TAGS_FIXTURE)

        result = import_art_tags(pool, fetcher)

        assert result["tags_imported"] == 1
        assert result["cards_with_tags"] == 1
        assert "duration_seconds" in result
